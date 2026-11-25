"""假的 Chrome，只在测试里用：它假装是用 --remote-debugging-pipe 启动起来的 Chrome。

真的 Chrome 从第 3 号文件描述符读命令、往第 4 号写回答，每条消息是一段 JSON，末尾一个 \\0（见 harness/browser/cdp.py）。
这个脚本做同样的事，按一份"剧本"来回答。这样不用装 Chrome，就能让真正的 chrome.launch()、Connection、
BrowserManager、BrowserSession 把启动、对话、关闭走一遍。

## 怎么启动它

chrome.launch() 把"Chrome 的路径"同时当作 argv[0]，后面紧跟着 Chrome 自己的参数（--remote-debugging-pipe……），
最后才是 extra_args。所以不能直接把 sys.executable 交给它：python 会把 --remote-debugging-pipe 当成自己的选项。
executable(文件夹) 在那里写一小段 sh 脚本，转手用当前的 Python 运行这个文件，Chrome 的参数原样传过来（这里不理它们）。
为什么是 sh 而不是在本文件开头写 #!python：这个仓库的路径里有空格和中文，shebang 行在空格处就被截断了；
sh 脚本里可以把路径好好地加上引号。sh 用 exec 换成 python，进程号不变，所以 launch() 拿到的进程号就是这个脚本的。

Windows 上没有 sh，写的是一个 .cmd（cmd.exe 运行它，它再运行 python）。真的 Chrome 在 Windows 上不从 3、4 号读写，
而是从 --remote-debugging-io-pipes=读,写 这两个继承来的句柄（见 harness/browser/chrome.py）：这里照样从那儿读写。
cmd.exe 把能继承的句柄原样传给 python，句柄的数值也不变。launch() 拿到的进程号是 cmd.exe 的，关的时候 taskkill /T 连同 python 一起结束。

    executable = fake_chrome.executable(tmp_path)
    started = chrome.launch(executable, profile, True, extra_args=[fake_chrome.script_arg(tmp_path, silent=["Page.navigate"])])

## 剧本（--fake-script=文件.json，所有字段都可以不写）

    results       {方法: 结果}                 这个方法回答什么（覆盖内置的回答）
    errors        {方法: {code, message}}      这个方法回答一个错误
    silent        [方法, ...]                  收到这些方法不回答（测超时）
    events        {方法: [消息, ...]}          回答这个方法之后，紧接着发这些事件（{"method", "params", "sessionId"}）
    startup       [消息, ...]                  一启动就发的事件
    record        文件路径                      把收到的每条命令追加写进去（一行一条 JSON），测试靠它检查我们发了什么
    pidfile       文件路径                      启动时先起一个一直睡着的子进程（对 launch() 来说是孙进程），把它的进程号写进去：
                                               测"关掉的时候整个进程组都没了"
    ignore_close  true                         收到 Browser.close 照样回答，但不退出（像一个卡住的 Chrome）
    ignore_term   true                         不理 SIGTERM（只有 SIGKILL 才能结束它）
    redirects     {网址: 网址}                  服务器的跳转（302）：去前一个，服务器让它去后一个
    page_redirects {网址: 网址}                 页面自己的跳转（像 <meta http-equiv="refresh" content="0;url=…">）：
                                               这一页"加载完"以后，页面自己去后一个

## 内置的回答（让 BrowserManager、BrowserSession 能走完常见的流程）

    Browser.getVersion      {"product": "FakeChrome/1.0", ...}
    Browser.close           回答，然后退出（除非 ignore_close）
    Target.getTargets       现在开着的标签页（一启动就有一个 about:blank，编号 BLANK，和真的 Chrome 一样）
    Target.createTarget     新开一个标签页（T1、T2……）：先发 Target.targetCreated，再回答
    Target.attachToTarget   {"sessionId": "S-<targetId>"}；没有这个标签页时回答错误
    Target.closeTarget      关掉它：先回答，再发 Target.targetDestroyed（真的 Chrome 关标签页是异步的，也是先回答）
    Target.getTargetInfo    那个标签页的情况
    Page.navigate           改掉标签页的地址，回答 loaderId，再发这个标签页的 frameStartedLoading、frameStoppedLoading。
                            剧本里有跳转的，跟着跳完（没开 Fetch 时谁也拦不住，和真的 Chrome 一样）
    Fetch.enable / disable  这个标签页的文档请求要不要先停下来问（Fetch.requestPaused）。开着的时候 Page.navigate 先不回答，
                            照真的 Chrome 的顺序来（见 harness/browser/redirects.py 开头）：每一跳先停下来，等
                            Fetch.continueRequest（下一跳或者落地，这时才回答 Page.navigate）或 Fetch.failRequest
                            （Aborted：标签页留在原来那一页；别的原因：换上错误页，地址是被拦的网址），回答 errorText。
                            页面自己的跳转在"加载完"之前先发 Page.frameScheduledNavigation，"加载完"以后才去。
                            Fetch.disable 放行还停着的请求
    Page.captureScreenshot  几个字节的"JPEG"（base64）
    Page.getLayoutMetrics   {"cssVisualViewport": {"pageX": 0, "pageY": 0, ...}}
    别的方法                {}

还有几个测试专用的方法（真的 Chrome 没有）：

    Fake.environ   {"environ": 这个进程的环境变量}                     测"密钥没有交给 Chrome"
    Fake.argv      {"argv": 收到的命令行参数}
    Fake.process   {"pid", "pgid", "ppid", "fds": {描述符: {kind, dev, ino}}}   测"管道在 3、4 号上，别的没有漏过来"
    Fake.emit      先把 params.events 里的消息都发出去，再回答：调用返回的时候，这些事件一定已经被读线程处理过了
    Fake.exit      不回答，直接退出（测"Chrome 崩溃了"）

测试里还会用到这里的几个小工具：executable()、script_arg()、records()（读 record 写下的命令）、
open_pipes()（测试进程自己开着几根管道，查"有没有漏关"）。
"""

from __future__ import annotations

import base64
import itertools
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
SCRIPT_FLAG = "--fake-script="
JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"  # 截图的"内容"：harness 不解码它，只存、只转 base64
_counter = itertools.count(1)
LATER = object()  # builtin() 交回它：这个命令现在先不回答（Fetch 开着时的 Page.navigate，等跳转有了结果再回答）


# ---- 给测试用的小工具 ----------------------------------------------------------


WINDOWS = os.name == "nt"


def executable(folder: Path) -> Path:
    """在 folder 里写一个能交给 chrome.launch() 的"Chrome"（见文件开头：为什么是一段 sh 脚本，Windows 上是 .cmd）。"""
    if WINDOWS:
        path = folder / "fake-chrome.cmd"
        if not path.exists():
            path.write_text(f'@"{sys.executable}" "{HERE}" %*\n', encoding="oem")  # cmd.exe 按控制台的代码页读 .cmd：路径里有中文也认得
        return path
    path = folder / "fake-chrome"
    if not path.exists():
        path.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(HERE))} "$@"\n', encoding="utf-8")
        path.chmod(0o755)
    return path


def script_arg(folder: Path, **script) -> str:
    """把剧本写进 folder 里的一个新文件，返回要放进 extra_args 的那个参数。"""
    path = folder / f"fake-chrome-script-{next(_counter)}.json"
    path.write_text(json.dumps(script), encoding="utf-8")
    return SCRIPT_FLAG + str(path)


def records(path: Path) -> list[dict]:
    """假 Chrome 收到的每一条命令（剧本里 record 写下的）。"""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def open_pipes() -> set[int]:
    """调用它的这个进程（测试进程）开着的管道：前后各数一次，就知道有没有漏关的。"""
    found = set()
    for fd in range(1024):
        try:
            if stat.S_ISFIFO(os.fstat(fd).st_mode):
                found.add(fd)
        except OSError:
            continue
    return found


# ---- 协议的逻辑 -------------------------------------------------------------------


class Logic:
    """收到一条命令，决定发回去哪些消息。不碰描述符，所以也能在测试进程里单独用。"""

    def __init__(self, script: dict | None = None, argv: list[str] | None = None):
        self.script = script or {}
        self.argv = argv or []
        self.targets: dict[str, dict] = {"BLANK": _info("BLANK", "about:blank")}
        self.created = 0
        self.loads = 0
        self.done = False  # 该退出了（收到 Browser.close、Fake.exit）
        self.fetching: set[str] = set()  # 开了 Fetch 的标签页会话
        self.paused: dict[str, dict] = {}  # 停下来等答复的请求：requestId → {session, target, url, navigation}
        self.jobs = 0

    def note(self, message: dict) -> None:
        record = self.script.get("record")
        if record:
            # 每条都立刻写完、关上：回答发出去之前，测试就一定能读到这一行
            with open(record, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(message) + "\n")

    def handle(self, message: dict) -> list[dict]:
        self.note(message)
        method = str(message.get("method", ""))
        params = message.get("params") or {}
        session = message.get("sessionId") or ""
        id = message.get("id")
        if method == "Fake.exit":
            self.done = True
            return []
        if method in self.script.get("silent", []):
            return []
        before: list[dict] = []
        after: list[dict] = []
        errors = self.script.get("errors", {})
        results = self.script.get("results", {})
        if method in errors:
            reply = {"id": id, "error": errors[method]}
        elif method in results:
            reply = {"id": id, "result": results[method]}
        else:
            outcome = self.builtin(method, params, session, before, after, id)
            if outcome is LATER:
                return [*before, *after]
            reply = {"id": id, "error": outcome[1]} if isinstance(outcome, tuple) else {"id": id, "result": outcome}
        if session:
            reply["sessionId"] = session
        return [*before, reply, *after, *self.script.get("events", {}).get(method, [])]

    def builtin(self, method: str, params: dict, session: str, before: list, after: list, id: object = None) -> object:
        """内置的回答。返回结果（字典），或者 (None, 错误) 表示回答一个错误，或者 LATER（先不回答）。
        before / after 里放要在回答前后发的事件。"""
        if method == "Browser.getVersion":
            return {"protocolVersion": "1.3", "product": "FakeChrome/1.0", "userAgent": "FakeChrome", "jsVersion": "0"}
        if method == "Browser.close":
            self.done = not self.script.get("ignore_close")
            return {}
        if method == "Target.getTargets":
            return {"targetInfos": list(self.targets.values())}
        if method == "Target.createTarget":
            self.created += 1
            target = f"T{self.created}"
            self.targets[target] = _info(target, str(params.get("url", "about:blank")))
            before.append({"method": "Target.targetCreated", "params": {"targetInfo": self.targets[target]}})
            return {"targetId": target}
        target = str(params.get("targetId", ""))
        if method in ("Target.attachToTarget", "Target.closeTarget", "Target.getTargetInfo", "Target.activateTarget"):
            if target not in self.targets:
                return None, {"code": -32602, "message": "No target with given id found"}
            if method == "Target.attachToTarget":
                return {"sessionId": f"S-{target}"}
            if method == "Target.closeTarget":
                del self.targets[target]
                after.append({"method": "Target.targetDestroyed", "params": {"targetId": target}})
                return {"success": True}
            if method == "Target.getTargetInfo":
                return {"targetInfo": self.targets[target]}
            return {}
        if method == "Page.navigate":
            target = session.removeprefix("S-")
            url = str(params.get("url", ""))
            self.loads += 1
            loader = f"L{self.loads}"
            after.append(_frame_event("Page.frameStartedLoading", session, target))
            if session in self.fetching:
                self._pause(after, session, target, url, navigation={"id": id, "loader": loader})
                return LATER
            hops = 0
            while hops < 20 and (url in self.script.get("redirects", {}) or url in self.script.get("page_redirects", {})):
                url = self.script.get("redirects", {}).get(url) or self.script["page_redirects"][url]
                hops += 1
            if target in self.targets:
                self.targets[target]["url"] = url
            after.append(_frame_event("Page.frameStoppedLoading", session, target))
            return {"frameId": target, "loaderId": loader}
        if method == "Fetch.enable":
            self.fetching.add(session)
            return {}
        if method == "Fetch.disable":
            self.fetching.discard(session)
            for request, item in list(self.paused.items()):
                if item["session"] == session:
                    del self.paused[request]
                    self._go_on(after, request, item)  # 停用时还停着的请求放行（和真的 Chrome 一样）
            return {}
        if method in ("Fetch.continueRequest", "Fetch.failRequest"):
            request = str(params.get("requestId", ""))
            item = self.paused.pop(request, None)
            if item is None:
                return None, {"code": -32602, "message": "Invalid InterceptionId."}
            if method == "Fetch.continueRequest":
                self._go_on(after, request, item)
            else:
                self._refuse(after, item, str(params.get("errorReason", "")))
            return {}
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(JPEG).decode()}
        if method == "Page.getLayoutMetrics":
            return {"cssVisualViewport": {"pageX": 0, "pageY": 0, "clientWidth": 1280, "clientHeight": 800}}
        if method == "Fake.environ":
            return {"environ": dict(os.environ)}
        if method == "Fake.argv":
            return {"argv": self.argv}
        if method == "Fake.process":
            return {"pid": os.getpid(), "pgid": None if WINDOWS else os.getpgid(0), "ppid": os.getppid(), "fds": _open_fds()}
        if method == "Fake.emit":
            before.extend(params.get("events", []))
            return {}
        return {}

    # ---- Fetch 域：停下来的请求（顺序照着真的 Chrome，见 harness/browser/redirects.py 开头） ----------

    def _pause(self, after: list, session: str, target: str, url: str, navigation: dict | None, redirected: str = "") -> None:
        """主框架要去 url：Fetch 开着就停下来问（Fetch.requestPaused），关着就直接去。
        navigation 是还没回答的那个 Page.navigate（页面自己的跳转没有）；redirected 是引起这一跳的那个请求。"""
        self.jobs += 1
        request = f"job-{self.jobs}"
        item = {"session": session, "target": target, "url": url, "navigation": navigation}
        if session not in self.fetching:
            self._go_on(after, request, item)
            return
        self.paused[request] = item
        params = {"requestId": request, "request": {"url": url, "method": "GET", "headers": {}}, "frameId": target, "resourceType": "Document"}
        if redirected:
            params["redirectedRequestId"] = redirected
        after.append({"method": "Fetch.requestPaused", "params": params, "sessionId": session})

    def _go_on(self, after: list, request: str, item: dict) -> None:
        """放行：服务器让它跳的，去下一跳；不然这一页落地了（这时才回答 Page.navigate），页面自己要跳的再去跳。"""
        session, target, url, navigation = item["session"], item["target"], item["url"], item["navigation"]
        following = self.script.get("redirects", {}).get(url)
        if following:
            self._pause(after, session, target, following, navigation, redirected=request)
            return
        if target in self.targets:
            self.targets[target]["url"] = url
        if navigation is not None:
            after.append({"id": navigation["id"], "result": {"frameId": target, "loaderId": navigation["loader"]}, "sessionId": session})
        later = self.script.get("page_redirects", {}).get(url)
        if not later:
            after.append(_frame_event("Page.frameStoppedLoading", session, target))
            return
        # 像 meta refresh：加载完之前先说"排好了一次跳转"，说完"加载完了"才去跳
        after.append(_frame_event("Page.frameScheduledNavigation", session, target, delay=0, reason="metaTagRefresh", url=later))
        after.append(_frame_event("Page.frameStoppedLoading", session, target))
        after.append(_frame_event("Page.frameRequestedNavigation", session, target, reason="metaTagRefresh", url=later, disposition="currentTab"))
        after.append(_frame_event("Page.frameStartedLoading", session, target))
        self._pause(after, session, target, later, navigation=None)

    def _refuse(self, after: list, item: dict, reason: str) -> None:
        """Fetch.failRequest：Aborted 是这次跳转作罢，标签页留在原来那一页；别的原因 Chrome 换上一张错误页，地址是被拦的网址。"""
        session, target, navigation = item["session"], item["target"], item["navigation"]
        if reason != "Aborted" and target in self.targets:
            self.targets[target]["url"] = item["url"]
        if navigation is not None:
            error = "net::ERR_ABORTED" if reason == "Aborted" else "net::ERR_BLOCKED_BY_CLIENT"
            after.append({"id": navigation["id"], "result": {"frameId": target, "loaderId": navigation["loader"], "errorText": error}, "sessionId": session})
        after.append(_frame_event("Page.frameStoppedLoading", session, target))


def _frame_event(method: str, session: str, target: str, **params: object) -> dict:
    return {"method": method, "params": {"frameId": target, **params}, "sessionId": session}


def _info(target: str, url: str) -> dict:
    return {"targetId": target, "type": "page", "title": "", "url": url, "attached": False, "browserContextId": "CTX"}


def _open_fds() -> dict[str, dict]:
    """这个进程开着的描述符，每个是什么（管道、字符设备……）以及它的 dev、ino：测试拿它和自己那一头的管道比对。"""
    found = {}
    for fd in range(256):
        try:
            info = os.fstat(fd)
        except OSError:
            continue
        kind = "fifo" if stat.S_ISFIFO(info.st_mode) else "chr" if stat.S_ISCHR(info.st_mode) else "reg" if stat.S_ISREG(info.st_mode) else "other"
        found[str(fd)] = {"kind": kind, "dev": info.st_dev, "ino": info.st_ino}
    return found


# ---- 收发：读 3 号，写 4 号 --------------------------------------------------------


def _send(fd: int, messages: list[dict]) -> None:
    """几条消息一次写出去：读的那一头多半会在同一块里读到它们（真的 Chrome 也常常连着发）。"""
    data = b"".join(json.dumps(message).encode() + b"\0" for message in messages)
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def serve(logic: Logic, read_fd: int = 3, write_fd: int = 4) -> None:
    """一直读命令、写回答，直到读到结尾（对面关掉了管道）或者 logic 说该退出了。"""
    buffer = b""
    try:
        _send(write_fd, logic.script.get("startup", []))
        while not logic.done:
            chunk = os.read(read_fd, 1 << 16)
            if not chunk:
                return  # 和真的 Chrome 一样：管道那一头没人了，就自己退出
            buffer += chunk
            while b"\0" in buffer and not logic.done:
                raw, _, buffer = buffer.partition(b"\0")
                out = logic.handle(json.loads(raw))
                if out:
                    _send(write_fd, out)
    except BrokenPipeError:
        return  # 对面已经不读了


def main() -> None:
    script: dict = {}
    for argument in sys.argv[1:]:
        if argument.startswith(SCRIPT_FLAG):
            script = json.loads(Path(argument[len(SCRIPT_FLAG) :]).read_text(encoding="utf-8"))
    if script.get("pidfile"):
        # 先起孙进程，再改信号：SIG_IGN 会被子进程继承，那样它也杀不掉了
        sleeper = [sys.executable, "-c", "import time; time.sleep(30)"] if WINDOWS else ["sleep", "30"]
        child = subprocess.Popen(sleeper, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        temp = script["pidfile"] + ".tmp"
        Path(temp).write_text(str(child.pid), encoding="utf-8")
        os.replace(temp, script["pidfile"])
    if script.get("ignore_term"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    serve(Logic(script, sys.argv[1:]), *_pipes())


def _pipes() -> tuple[int, int]:
    """从哪儿读、往哪儿写：macOS、Linux 上是 3、4 号；Windows 上是 --remote-debugging-io-pipes 给的两个句柄。"""
    if not WINDOWS:
        return 3, 4
    import msvcrt

    for argument in sys.argv[1:]:
        if argument.startswith("--remote-debugging-io-pipes="):
            read, write = (int(part) for part in argument.split("=", 1)[1].split(","))
            return msvcrt.open_osfhandle(read, os.O_RDONLY), msvcrt.open_osfhandle(write, os.O_WRONLY)
    raise SystemExit("没有 --remote-debugging-io-pipes")


if __name__ == "__main__":
    main()
