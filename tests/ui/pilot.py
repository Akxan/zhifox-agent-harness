"""页面测试用的"驾驶员"：开一个无界面的 Chrome，像人一样打开页面、点、按键、输入，再读出页面上的情况。

不用 Playwright、Selenium 这类库：智狐自己已经有一个手写的 CDP 客户端（harness/browser/cdp.py，走管道，
不开端口）和启动 Chrome 的代码（chrome.py），这里直接拿来用，只在上面加几样测试要用的东西：

  - 点击、悬停、按键都是真的鼠标和键盘事件（Input.dispatchMouseEvent / dispatchKeyEvent），和真人操作一样
    会经过"这个位置上最上面的是谁"：按钮被别的东西盖住、或者还没淡入（pointer-events: none），点下去就不会生效，
    测试能发现这类问题。用 element.click() 就发现不了。
  - 等待一律是"等某个条件成立"：把一段判断写成 JavaScript 表达式，在页面里每 25 毫秒看一次，成立了马上返回，
    到时间还不成立就报错，并说清楚在等什么。不写"睡两秒再看"：快的机器上白等，慢的机器上不够。
  - 页面里出的每一个错都记下来：脚本异常（Runtime.exceptionThrown）、console.error、浏览器自己的报错和安全策略
    （CSP）拦下的东西（Log.entryAdded）。测试结束时一条都不能有（见 conftest.py 的 page）。

加新的检查时，常用的就是这几个：page.js(表达式) 读值，page.wait(表达式) 等条件，page.click(选择器) 点，
page.key("Escape") 按键，page.type("文字") 输入。选择器和表达式都是普通的 CSS / JavaScript。
"""

from __future__ import annotations

import base64
import contextlib
import json
import threading
import time
from pathlib import Path

from harness.browser import chrome, keys
from harness.browser.cdp import Connection

DEFAULT_SIZE = (1280, 860)  # 普通的笔记本窗口：两边的栏并排展开，信息栏收着（窗口不到 1400 宽时默认收起）
PHONE_WIDTH = 390  # 窄屏检查用的宽度（常见的手机）


class PageError(AssertionError):
    """页面里的东西不是测试以为的样子：等的条件一直不成立、要点的元素找不到……"""


class Browser:
    """一个专门给测试用的无界面 Chrome。整个测试会话共用一个，每个测试开一个新的标签页。"""

    # 窗口大小由每个标签页自己设（Emulation.setDeviceMetricsOverride），这几个参数让截图、坐标在每台机器上都一样。
    # 最后一个是说"这是一台有鼠标的电脑"：无界面的 Chrome 在没有显示器的 Linux（CI 的机器）上可能认为自己没有鼠标，
    # (hover: hover) 不成立，页面就按触屏的样子画（行右边的按钮一直显示），悬停的检查全都不对了
    ARGS = (
        "--force-device-scale-factor=1",
        "--hide-scrollbars",
        "--blink-settings=primaryHoverType=2,availableHoverTypes=2,primaryPointerType=4,availablePointerTypes=4",
    )

    def __init__(self, executable: Path, profile: Path):
        self.process = chrome.launch(executable, profile, headless=True, extra_args=list(self.ARGS))
        self.connection: Connection = self.process.connection

    def new_page(self) -> Page:
        """开一个新标签页。每个标签页在自己的"浏览器上下文"里（像一个隐身窗口）：浏览器里存着的东西
        （外观、动效、两边的栏开着还是收着）都是空的，每个测试都从默认的样子开始，关掉时一起扔掉。"""
        context = self.connection.call("Target.createBrowserContext", {"disposeOnDetach": True})["browserContextId"]
        target = self.connection.call("Target.createTarget", {"url": "about:blank", "browserContextId": context})["targetId"]
        session = self.connection.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        return Page(self.connection, target, session, context)

    def close(self) -> None:
        self.process.close()


class Page:
    """一个标签页。"""

    def __init__(self, connection: Connection, target: str, session: str, context: str = ""):
        self.connection = connection
        self.target = target
        self.session = session
        self.context = context
        self.errors: list[str] = []  # 页面里出的错（测试结束时必须是空的）
        self.console: list[str] = []  # 所有的控制台输出，测试失败时存下来
        self.allowed: list[str] = []  # 这个测试预料之中的错（里面含有这些字的不算）
        self._lock = threading.Lock()
        self._forget = [
            connection.on("Runtime.exceptionThrown", self._on_exception),
            connection.on("Runtime.consoleAPICalled", self._on_console),
            connection.on("Log.entryAdded", self._on_log),
        ]
        for method in ("Runtime.enable", "Log.enable", "Page.enable"):
            self.call(method)
        # 标签页在后台也当作"有焦点"：:focus-within、focus 事件和真人用的时候一样
        self.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
        self.size(*DEFAULT_SIZE)

    # ---- 底层 ----------------------------------------------------------------

    def call(self, method: str, params: dict | None = None, timeout: float = 15) -> dict:
        return self.connection.call(method, params, session=self.session, timeout=timeout)

    def _mine(self, session: str) -> bool:
        return session == self.session

    def _record(self, line: str, error: bool) -> None:
        with self._lock:
            self.console.append(line)
            if error:
                self.errors.append(line)

    def _on_exception(self, params: dict, session: str) -> None:
        if not self._mine(session):
            return
        details = params.get("exceptionDetails") or {}
        exception = details.get("exception") or {}
        where = f"{details.get('url', '')}:{details.get('lineNumber', '?')}"
        self._record(f"[exception] {exception.get('description') or details.get('text')} @ {where}", True)

    def _on_console(self, params: dict, session: str) -> None:
        if not self._mine(session):
            return
        kind = params.get("type", "log")
        words = " ".join(str(arg.get("value", arg.get("description", ""))) for arg in params.get("args") or [])
        self._record(f"[console.{kind}] {words}", kind in ("error", "assert"))

    def _on_log(self, params: dict, session: str) -> None:
        if not self._mine(session):
            return
        entry = params.get("entry") or {}
        level, source, words = entry.get("level", ""), entry.get("source", ""), entry.get("text", "")
        # 安全策略（CSP）拦下的东西 source 是 security；浏览器自己的报错 level 是 error（比如一个请求失败了）
        self._record(f"[{source}.{level}] {words} {entry.get('url', '')}".rstrip(), level == "error" or source == "security")

    def unexpected_errors(self) -> list[str]:
        with self._lock:
            return [line for line in self.errors if not any(allowed in line for allowed in self.allowed)]

    def allow_errors(self, *fragments: str) -> None:
        """这个测试里预料之中的错（比如故意让一个请求失败）：含有这些字的不算。"""
        self.allowed.extend(fragments)

    def close(self) -> None:
        for forget in self._forget:
            forget()
        with contextlib.suppress(Exception):  # 关不掉就算了，Chrome 最后会整个关掉
            self.connection.call("Target.closeTarget", {"targetId": self.target}, timeout=5)
            if self.context:
                self.connection.call("Target.disposeBrowserContext", {"browserContextId": self.context}, timeout=5)

    # ---- 打开、读、等 ----------------------------------------------------------

    def goto(self, url: str) -> None:
        loaded = self.connection.expect("Page.loadEventFired", lambda params, session: session == self.session)
        self.call("Page.navigate", {"url": url})
        if loaded.wait(20) is None:
            raise PageError(f"页面 20 秒还没加载完：{url}")

    def reload(self) -> None:
        loaded = self.connection.expect("Page.loadEventFired", lambda params, session: session == self.session)
        self.call("Page.reload", {"ignoreCache": True})
        if loaded.wait(20) is None:
            raise PageError("刷新以后 20 秒还没加载完")

    def js(self, expression: str, timeout: float = 15):
        """在页面里算一个 JavaScript 表达式，返回它的值（要能转成 JSON）。表达式是 Promise 的，等它完成。"""
        reply = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True, "userGesture": True},
            timeout=timeout,
        )
        if "exceptionDetails" in reply:
            details = reply["exceptionDetails"]
            raise PageError(f"页面里的表达式出错了：{(details.get('exception') or {}).get('description') or details.get('text')}\n{expression}")
        return (reply.get("result") or {}).get("value")

    def wait(self, condition: str, timeout: float = 10, what: str = ""):
        """等 condition（一个 JavaScript 表达式）的值为真，返回这个值。超时报错，写明在等什么。

        在页面里每 25 毫秒算一次，算的时候出错当作"还不成立"（比如元素还没出现时读它的属性）。
        """
        script = (
            "new Promise((resolve) => {"
            f"  const deadline = performance.now() + {timeout * 1000:.0f};"
            "  const tick = () => {"
            "    let value, problem = '';"
            f"    try {{ value = ({condition}); }} catch (error) {{ problem = String(error); }}"
            "    if (value) resolve({ ok: true, value });"
            "    else if (performance.now() > deadline) resolve({ ok: false, problem });"
            "    else setTimeout(tick, 25);"
            "  };"
            "  tick();"
            "})"
        )
        result = self.js(script, timeout=timeout + 10)
        if not result or not result.get("ok"):
            problem = (result or {}).get("problem")
            raise PageError(f"等了 {timeout:g} 秒，{what or '条件'}还是不成立：{condition}" + (f"（{problem}）" if problem else ""))
        return result.get("value")

    def exists(self, selector: str) -> bool:
        return bool(self.js(f"Boolean(document.querySelector({json.dumps(selector)}))"))

    def text(self, selector: str) -> str:
        return self.js(f"(document.querySelector({json.dumps(selector)}) || {{}}).textContent || ''")

    def visible(self, selector: str) -> str:
        """一个 JavaScript 表达式：这个选择器的元素在页面上看得见（有大小，没有被藏起来）。给 wait() 用。"""
        return f"(n => n && n.getClientRects().length > 0 && getComputedStyle(n).visibility !== 'hidden')(document.querySelector({json.dumps(selector)}))"

    # ---- 鼠标和键盘 ----------------------------------------------------------

    def reach(self, selector: str, index: int = 0, timeout: float = 5) -> tuple[float, float]:
        """等元素能被鼠标点到，返回它中心的坐标。和真人一样，要等到：

          - 它在页面上、看得见（先滚到能看见的地方，只在需要时滚，尽量不动别处的滚动位置）；
          - 它不动了：它自己和上面几层都没有正在播的动画（滑进来、淡入的播完了），连着两帧量到的位置一样；
          - 中心那一点上最上面的就是它（没有被别的东西盖住）。

        "等动画播完"不只是为了点得准：真人不会在抽屉滑到一半时就点下一个按钮。实测过（Chrome 157，390 宽）：
        几十毫秒之内连着关会话栏、开信息栏、再关上，顶栏右边那几个按钮会一直点不到（没有任何样式说它们不能点，
        改一下它们的样式就又好了，像是 Chrome 记住了 inert 还没撤掉时的样子）。那是人做不到的速度，所以测试也不那样点。
        到时间还不行就报错，写明是找不到、还在动，还是被谁盖住了。
        """
        result = self.js(
            "new Promise((resolve) => {"
            f"  const deadline = performance.now() + {timeout * 1000:.0f};"
            "  let last = '', scrolled = false;"
            "  const moving = (node) => {"
            "    for (let at = node; at; at = at.parentElement) {"
            "      if (at.getAnimations().some((a) => a.playState === 'running' && Number.isFinite(a.effect.getComputedTiming().endTime))) return true;"
            "    }"
            "    return false;"
            "  };"
            "  const tick = () => {"
            f"    const node = document.querySelectorAll({json.dumps(selector)})[{index}];"
            "    let why = 'missing';"
            "    if (node) {"
            "      if (!scrolled) { node.scrollIntoView({ block: 'nearest', inline: 'nearest' }); scrolled = true; }"
            "      const box = node.getBoundingClientRect();"
            "      const at = [box.left, box.top, box.width, box.height].map((n) => Math.round(n * 2)).join(',');"
            "      why = 'moving';"
            "      if (box.width && box.height && at === last && !moving(node)) {"
            "        const x = box.left + box.width / 2, y = box.top + box.height / 2, top = document.elementFromPoint(x, y);"
            "        if (top && (top === node || node.contains(top))) { resolve({ ok: true, x, y }); return; }"
            "        why = 'covered by ' + (top ? top.outerHTML.slice(0, 160) : 'nothing');"
            "      }"
            "      if (!box.width || !box.height) why = 'not visible';"
            "      last = at;"
            "    }"
            "    if (performance.now() > deadline) resolve({ ok: false, why });"
            "    else requestAnimationFrame(tick);"
            "  };"
            "  tick();"
            "})",
            timeout=timeout + 10,
        )
        if not result.get("ok"):
            raise PageError(f"{selector}（第 {index + 1} 个）点不到：{result.get('why')}")
        return result["x"], result["y"]

    def mouse(self, x: float, y: float) -> None:
        self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})

    def hover(self, selector: str, index: int = 0) -> None:
        self.mouse(*self.reach(selector, index))

    def click(self, selector: str, index: int = 0, timeout: float = 5) -> None:
        """在元素的中心真的按一下鼠标（先等它能被点到，见 reach）。"""
        self.click_at(*self.reach(selector, index, timeout))

    def click_at(self, x: float, y: float) -> None:
        self.mouse(x, y)
        for kind in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left", "buttons": 1 if kind == "mousePressed" else 0, "clickCount": 1})

    def key(self, name: str) -> None:
        """按一个键或组合键，写法和浏览器工具一样："Escape"、"Enter"、"shift+Tab"、"ctrl+a"。"""
        for chord in keys.parse(name):
            for event in keys.events(chord):
                self.call("Input.dispatchKeyEvent", event)

    def type(self, text: str) -> None:
        """在有焦点的输入框里打字（像输入法那样一次插进去，会触发 input 事件）。"""
        self.call("Input.insertText", {"text": text})

    def focus(self, selector: str) -> None:
        self.js(f"document.querySelector({json.dumps(selector)}).focus()")

    # ---- 窗口和外观 ----------------------------------------------------------

    def size(self, width: int, height: int) -> None:
        self.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})

    def emulate(self, **features: str) -> None:
        """模拟系统设置，比如 emulate(prefers_color_scheme="dark", prefers_reduced_motion="reduce")。不给的恢复成默认。

        "减少动态效果"不给的话一律当成"没要求减少"，而不是跟着这台机器：GitHub 的 macOS 机器系统里开着"减少动态效果"，
        页面照着它不播动画（这是对的），查"实时的变化会淡入"的测试就在那里失败了，在本机却通过。每个测试从同一个样子开始。
        """
        features.setdefault("prefers_reduced_motion", "no-preference")
        self.call("Emulation.setEmulatedMedia", {"features": [{"name": name.replace("_", "-"), "value": value} for name, value in features.items()]})

    def screenshot(self, path: Path) -> None:
        data = self.call("Page.captureScreenshot", {"format": "png"}, timeout=20)["data"]
        path.write_bytes(base64.b64decode(data))

    def html(self) -> str:
        return self.js("document.documentElement.outerHTML") or ""


def until(condition, timeout: float = 10, what: str = "条件"):
    """在 Python 这边等一个条件成立（比如服务器上的文件写好了）。页面里的条件用 Page.wait。"""
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value:
            return value
        if time.monotonic() > deadline:
            raise PageError(f"等了 {timeout:g} 秒，{what}还是不成立")
        time.sleep(0.05)
