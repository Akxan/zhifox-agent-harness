"""一次运行用的浏览器：它自己的几个标签页，以及在上面做的每一个动作。

"一次运行"是：你的一段对话、定时任务的一次运行、聊天通道里的一个聊天。它们可能同时在用浏览器
（你在对话里让助手查资料，同时一个定时任务在看天气），所以每一方各有一个 BrowserSession：
各开各的标签页，编号（tab-1、tab-2……）各数各的，谁也看不到、碰不到别人的页面。背后是同一个 Chrome（见 manager.py）。

## 一个动作是怎么做的

    模型说：left_click {"target": {"type": "ref", "ref": "ref_7"}}
      └─► 在页面里找到 ref_7 是哪个元素（page.js 记着），量出它中心点的坐标
            └─► 发鼠标事件：移过去、按下、松开（Input.dispatchMouseEvent）。网页收到的是和真人一样的点击
                  └─► 等一小会儿：点的是链接的话，新页面开始加载，等它加载完
                        └─► 回答"点了 ref_7"，顺便截一张小图给你看（不发给模型）

编号（ref）的做法见 page.js。它们只在这个标签页、这一页上有效：页面一跳转，旧编号就作废，模型要重新读一遍页面。
编号一直往上数、不从头开始，所以跳转之后模型手里的旧 ref_7 不会碰巧指到新页面上的另一个元素。

## 停下

stop() 让正在进行的那个动作马上结束：还在等 Chrome 回答的命令立刻作废（Connection.cancel），
在等页面加载、在 wait 的也立刻醒来。只停这一个 session 的：别的运行照常。

## BrowserSession 由几块拼成

和 App（app.py）、WebApp（webui/webapp.py）是同一种写法：这个文件里是几块都要用的部分，每一组方法写在它自己的文件里，
以基类的形式混进来：

  这个文件          状态（__init__）、对外的四个方法（run、facts、stop、close）、manager 在读线程里交来的事（收件箱）、
                    和 Chrome 的连接、每个动作的收尾（攒下的说明、标签页的情况）
  session_tabs.py   SessionTabs：开一个标签页、连上它、挑一个；管标签页的四个动作。Tab 也在那里
  session_page.py   SessionPage：在页面里做事的基本功：调用 page.js、等页面安静下来、截图。Outcome 也在那里
  session_look.py   SessionLook：去哪一页、看这一页（navigate、截图、放大、读页面、找元素、等）
  session_input.py  SessionInput：鼠标和键盘（点、拖、滚动、打字、按键、填表单）。Target 也在那里
  session_facts.py  SessionFacts：动作之前先看一眼，给 permissions.py。Facts 也在那里
  session_types.py  SessionPart：混进来的几组方法要用的、别的文件里的东西，只写给类型检查（mypy）看

run(action) 照旧按名字找 _do_<action>，不管它写在哪个文件里；别处照旧 from .session import BrowserSession、Facts、Outcome、Target。
混进来的类只有方法，状态只在 __init__ 里。会把 _notes、_changes 整个换掉的 _state、_finish、_tabs_result 也留在这里，
和往里放东西的收件箱（_drain）挨着：一样东西从哪儿来、到哪儿去，在一个文件里看得全（另一个原因见 session_types.py 开头）。
"""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING, Any

from ..i18n import zh
from ..tools.base import ToolError
from . import urls
from .cdp import Cancelled, CDPClosed, CDPError, CDPTimeout, Connection
from .session_facts import Facts, SessionFacts
from .session_input import SessionInput, Target
from .session_look import SessionLook
from .session_page import Outcome, SessionPage, lines
from .session_tabs import SessionTabs, Tab, _clean_title

if TYPE_CHECKING:
    from .manager import BrowserManager

__all__ = ["BrowserSession", "Facts", "Outcome", "Tab", "Target"]


class BrowserSession(SessionTabs, SessionPage, SessionLook, SessionInput, SessionFacts):
    def __init__(self, manager: BrowserManager, label: str):
        self.manager = manager
        self.label = label  # 是谁的（"对话"、任务的名字……），只用来在状态里显示
        self.tabs: dict[str, Tab] = {}  # 按打开的顺序
        self.active = ""
        # 这段对话里已经允许去的网站（见 permissions.py）。只记在这里：换一段对话就是一个新的 session，从头再问
        self.allowed_sites: set[str] = set()
        self._count = 0  # 开过几个标签页（编号 tab-1、tab-2……用过的不再用）
        self._downloads = 0
        self._generation = -1  # 这些标签页属于 Chrome 的第几次启动。Chrome 重启过，它们就都没了
        self._lock = threading.RLock()  # 同一个 session 一次只做一个动作
        self._halt = threading.Event()
        self._changes: list[dict] = []  # 还没报告的变化：页面自己开的新标签页、被拒绝的下载
        self._notes: list[str] = []  # 还没告诉模型的事：页面弹了对话框、浏览器重启过……
        self._reported: object = None  # 上一次报告的标签页情况：没变就不再报告
        self.closed_by_user = False  # 你在页面上点了"停止"关掉了浏览器（见 manager.stop_all）
        self.busy = False
        # Chrome 主动说的事（页面自己开了标签页、标签页被关了、弹了对话框、想下载）是在读线程里收到的。
        # 读线程不能拿 self._lock（做动作的线程正拿着它在等 Chrome 的回答，读线程一等就谁也动不了了），
        # 所以它只把这些事放进收件箱，由做动作的线程在拿着锁的时候处理（见 _drain）。
        # _targets、_cdp_sessions 是"哪些标签页是我的"，读线程要查，所以也放在这把小锁后面
        self._inbox_lock = threading.Lock()
        self._inbox: list[tuple[str, str]] = []
        self._targets: set[str] = set()
        self._cdp_sessions: set[str] = set()

    # ---- 对外 ---------------------------------------------------------------

    def run(self, action: str, **args: Any) -> Outcome:
        """做一个动作。出错抛 ToolError（交给模型的话）。"""
        with self._lock:
            if self.closed_by_user:
                self.closed_by_user = False
                # 交给模型的话用 zh()：模型拿到中文原文，工具卡片上按界面的语言显示（见 ../i18n.py 开头）
                raise ToolError(zh("用户刚才在智狐的页面上停下并关掉了浏览器。先不要再用浏览器：告诉用户你做到了哪一步，等他说可以了再继续。"))
            self._halt.clear()
            self.busy = True
            self.manager.began()
            try:
                self._drain()
                return getattr(self, "_do_" + action)(**args)
            except Cancelled:
                raise ToolError(zh("用户停下了这一步浏览器操作。")) from None
            except CDPTimeout as exc:
                raise ToolError(zh("浏览器没有回应（{reason}）。页面可能卡住了：可以过一会儿再试，或者刷新一下（navigate reload）。", reason=exc.message)) from None
            except CDPClosed:
                raise ToolError(zh("浏览器关掉了（可能是被手动关掉了，或者崩溃了）。再用浏览器的工具时会自动重新打开，之前的标签页没有了。")) from None
            except CDPError as exc:
                raise ToolError(zh("浏览器没能完成这一步：{reason}", reason=exc.message)) from None  # Chrome 说的原因是资料，原样填进去
            finally:
                self.busy = False
                self.manager.ended()

    def facts(self, action: str, **args: Any) -> Facts:
        """要做的这个动作碰的是什么（见 session_facts.py 的 Facts）。不会为了看一眼去打开浏览器。"""
        with self._lock:
            self._drain()
            try:
                return self._facts(action, **args)
            except (CDPError, Cancelled, ToolError, ValueError):
                return Facts(action, reachable=False)

    def stop(self) -> None:
        """停下正在进行的动作（你点了"停止"）。"""
        self._halt.set()
        connection = self.manager.connection_if_running()
        if connection is not None:
            connection.cancel(self)

    def close(self) -> None:
        """这次运行结束了：关掉它自己的标签页。"""
        self.stop()
        with self._lock:
            connection = self.manager.connection_if_running()
            if connection is not None and self._generation == self.manager.generation:
                for tab in list(self.tabs.values()):
                    try:
                        connection.call("Target.closeTarget", {"targetId": tab.target}, timeout=3)
                    except (CDPError, Cancelled):
                        pass
            self._forget_tabs()
        self.manager.forget(self)

    # ---- 由 manager 调用（在读线程里：要快，不能调用 Chrome，不能拿 self._lock） ----------

    def owns(self, target: str) -> bool:
        with self._inbox_lock:
            return target in self._targets

    def owns_session(self, cdp_session: str) -> bool:
        with self._inbox_lock:
            return cdp_session in self._cdp_sessions

    def adopt(self, target: str) -> None:
        """页面自己开了一个新标签页（target=_blank 的链接、window.open）：算这个 session 的，下一个动作时记进来。"""
        self._post("adopt", target, new_target=target)

    def lost(self, target: str) -> None:
        """一个标签页没了（你在浏览器里手动关掉了它）。我们自己关的不算：关之前已经把它从名单上拿掉了。"""
        with self._inbox_lock:
            if target not in self._targets:
                return
            self._targets.discard(target)
            self._inbox.append(("lost", target))

    def note(self, text: str) -> None:
        self._post("note", text)

    def downloaded(self, url: str) -> None:
        """页面想下载一个文件，被拒绝了（智狐的浏览器不下载任何东西，见 manager.py）。"""
        self._post("download", url)

    def _post(self, kind: str, value: str, new_target: str = "") -> None:
        with self._inbox_lock:
            if new_target:
                if new_target in self._targets:
                    return
                self._targets.add(new_target)
            self._inbox.append((kind, value))

    def _drain(self) -> None:
        """处理收件箱里的事（见 __init__）。调用的人拿着 self._lock。"""
        with self._inbox_lock:
            items, self._inbox = self._inbox, []
        for kind, value in items:
            if kind == "adopt":
                tab = self._new_tab_entry(value)
                self._changes.append({"type": "tab_opened", "tab_id": tab.id})
            elif kind == "lost":
                for tab_id, tab in list(self.tabs.items()):
                    if tab.target == value:
                        del self.tabs[tab_id]
                        if self.active == tab_id:
                            self.active = next(reversed(self.tabs), "") if self.tabs else ""
                        self._notes.append(zh("标签页 {tab} 被关掉了（不是你关的，可能是用户手动关的）。", tab=tab_id))
            elif kind == "note":
                self._notes.append(value)
            elif kind == "download":
                self._downloads += 1
                self._changes.append(
                    {"type": "download_failed", "download_id": f"dl-{self._downloads}", "url": urls.shown(value, 1000), "error": "智狐的浏览器不下载文件"}
                )

    def _forget_tabs(self) -> None:
        """这些标签页都不算了（Chrome 重启过、这次运行结束了）。调用的人拿着 self._lock。"""
        self.tabs.clear()
        self.active = ""
        with self._inbox_lock:
            self._targets.clear()
            self._cdp_sessions.clear()
            self._inbox = [item for item in self._inbox if item[0] == "note"]

    # ---- 和 Chrome 的连接 ---------------------------------------------------------

    def _connection(self) -> Connection:
        """拿到和 Chrome 的连接（没开就打开）。Chrome 在两次动作之间重启过的话，旧的标签页都作废。"""
        connection, generation = self.manager.connect()
        if generation != self._generation:
            if self._generation != -1 and self.tabs:
                self._notes.append(zh("浏览器在这期间关掉又重新打开了（闲置太久会自动关掉），之前的标签页都没有了，现在是一个新的空白页。"))
            self._forget_tabs()
            self._reported = None
            self._generation = generation
        return connection

    # ---- 每个动作的收尾：攒下的说明、标签页的情况（browser_state） ----------------------

    def _state(self, connection: Connection, force: bool = False) -> dict | None:
        """现在开着哪些标签页、哪个在前面（Claude 的工具包要的格式）。和上次报告的一样、也没有新变化，返回 None。"""
        self._drain()  # 这个动作做的时候页面开了新标签页、弹了对话框……也算进这一次的结果里
        infos = {info["targetId"]: info for info in connection.call("Target.getTargets", owner=self).get("targetInfos", [])}
        tabs: list[dict] = []
        for tab in self.tabs.values():
            info = infos.get(tab.target, {})
            entry: dict = {"tab_id": tab.id, "title": _clean_title(info.get("title", "")), "url": urls.shown(info.get("url", ""), 1000)}
            if tab.id == self.active:
                entry["active"] = True
            tabs.append(entry)
        if tabs and not any(entry.get("active") for entry in tabs):
            tabs[-1]["active"] = True
            self.active = tabs[-1]["tab_id"]
        changes, self._changes = self._changes, []
        snapshot = json.dumps(tabs, sort_keys=True)
        if not force and not changes and snapshot == self._reported:
            return None
        self._reported = snapshot
        state: dict[str, Any] = {"tabs": tabs}
        if changes:
            # 同一个下载只报一次；新开的标签页要还开着才报
            open_ids = {entry["tab_id"] for entry in tabs}
            kept = [change for change in changes if change["type"] != "tab_opened" or change["tab_id"] in open_ids]
            if kept:
                state["state_changes"] = kept
        return state

    def _finish(self, connection: Connection, tab: Tab | None, text: str, preview: bool = True, image: bytes | None = None) -> Outcome:
        notes, self._notes = self._notes, []
        if notes:
            text = lines(text, *notes)  # 接起来以后每一段还记着原文（见 session_page.lines）
        outcome = Outcome(text=text, image=image)
        if tab is not None and preview and tab.id in self.tabs:
            outcome.shot, outcome.thumb = self._preview(connection, tab, image)
        outcome.state = self._state(connection)
        return outcome

    def _tabs_result(self, connection: Connection) -> Outcome:
        """管标签页的四个动作：结果只有一个 browser_state（见 Anthropic 的文档）。"""
        notes, self._notes = self._notes, []
        state = self._state(connection, force=True) or {"tabs": []}
        return Outcome(text=lines(*notes), state=state, tabs_only=True)
