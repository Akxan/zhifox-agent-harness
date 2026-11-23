"""一次运行自己的标签页：开一个、连上它、挑一个，以及管标签页的四个动作（BrowserSession 的一组方法，见 session.py 开头）。

标签页的编号（tab-1、tab-2……）是这个 session 自己数的，用过的不再用；Chrome 里的编号（targetId）记在 Tab 里，不给模型看。
连上一个标签页（_attach）时把它设成固定的大小、窗口不在最前面也当它有焦点、不弹"选择文件"的窗口。
new_tab、list_tabs、switch_tab、close_tab 的结果只有一个 browser_state（session.py 的 _tabs_result）。
_clean_title 也在这里：标签页的标题是网页自己定的，交给模型之前洗一遍（session.py 的 _state、session_look.py 读页面时都用它）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import BROWSER_VIEWPORT
from ..tools.base import ToolError
from .cdp import CDPError, Connection
from .session_page import Outcome
from .session_types import SessionPart


@dataclass
class Tab:
    id: str  # 给模型看的编号：tab-1、tab-2……这个 session 自己数，用过的不再用
    target: str  # Chrome 里的 targetId（也是这个标签页主框架的 frameId）
    session: str = ""  # 连上这个标签页之后拿到的 sessionId；空的表示还没连
    next_ref: int = 1  # 下一个元素编号从几开始（见文件开头）
    world: int = 0  # 隔离世界的编号（executionContextId）；页面一跳转就作废，用到时重建


class SessionTabs(SessionPart):
    # ---- 标签页：开一个、连上、挑一个 -----------------------------------------------

    def _new_tab_entry(self, target: str) -> Tab:
        self._count += 1
        tab = Tab(id=f"tab-{self._count}", target=target)
        self.tabs[tab.id] = tab
        with self._inbox_lock:
            self._targets.add(target)
        return tab

    def _open_tab(self, connection: Connection) -> Tab:
        target = self.manager.claim_blank()  # Chrome 刚启动时自己开的那个空白页：第一个用浏览器的拿去用
        if not target:
            target = connection.call("Target.createTarget", {"url": "about:blank"}, owner=self)["targetId"]
        tab = self._new_tab_entry(target)
        self.active = tab.id
        self._attach(connection, tab)
        return tab

    def _attach(self, connection: Connection, tab: Tab) -> None:
        if tab.session:
            return
        tab.session = connection.call("Target.attachToTarget", {"targetId": tab.target, "flatten": True}, owner=self)["sessionId"]
        with self._inbox_lock:
            self._cdp_sessions.add(tab.session)
        width, height = BROWSER_VIEWPORT
        for method, params in (
            ("Page.enable", {}),
            # 页面固定成这么大：截图一样大，模型给的坐标也都在这个范围里（见 config.py）
            ("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False}),
            # 窗口不在最前面时，页面照样以为自己有焦点（不然有的输入框点了也不接收文字）
            ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
            # 网页想弹"选择文件"的窗口时，不弹（智狐不替网页上传文件）
            ("Page.setInterceptFileChooserDialog", {"enabled": True}),
        ):
            connection.call(method, params, session=tab.session, owner=self)

    def _tab(self, connection: Connection, tab_id: str | None = None, open_one: bool = True) -> Tab:
        if not self.tabs:
            if not open_one:
                raise ToolError("还没有打开任何网页。先用 navigate 打开一个。")
            return self._open_tab(connection)
        chosen = tab_id or self.active or next(iter(self.tabs))
        tab = self.tabs.get(chosen)
        if tab is None:
            raise ToolError(f"没有 {chosen} 这个标签页。现在开着的有：{', '.join(self.tabs) or '（没有）'}。")
        self._attach(connection, tab)
        return tab

    # ---- 管标签页的四个动作（结果只有 browser_state，见 session.py 的 _tabs_result）----------

    def _do_new_tab(self) -> Outcome:
        connection = self._connection()
        if not self.tabs:
            tab = self._open_tab(connection)
        else:
            target = connection.call("Target.createTarget", {"url": "about:blank"}, owner=self)["targetId"]
            tab = self._new_tab_entry(target)
            self._attach(connection, tab)
            self.active = tab.id
        self._changes.append({"type": "tab_opened", "tab_id": tab.id})
        return self._tabs_result(connection)

    def _do_list_tabs(self) -> Outcome:
        connection = self._connection()
        return self._tabs_result(connection)

    def _do_switch_tab(self, tab_id: str) -> Outcome:
        connection = self._connection()
        tab = self.tabs.get(tab_id)
        if tab is None:
            raise ToolError(f"没有 {tab_id} 这个标签页。现在开着的有：{', '.join(self.tabs) or '（没有）'}。")
        connection.call("Target.activateTarget", {"targetId": tab.target}, owner=self)
        self.active = tab.id
        return self._tabs_result(connection)

    def _do_close_tab(self, tab_id: str) -> Outcome:
        connection = self._connection()
        tab = self.tabs.get(tab_id)
        if tab is None:
            raise ToolError(f"没有 {tab_id} 这个标签页。现在开着的有：{', '.join(self.tabs) or '（没有）'}。")
        # 先从名单上拿掉再关：Chrome 紧接着会说"这个标签页没了"，那时它已经不是我们的，不会被当成"被别人关掉了"
        del self.tabs[tab_id]
        with self._inbox_lock:
            self._targets.discard(tab.target)
            self._cdp_sessions.discard(tab.session)
        if self.active == tab_id:
            self.active = next(reversed(self.tabs), "") if self.tabs else ""
        try:
            connection.call("Target.closeTarget", {"targetId": tab.target}, owner=self)
        except CDPError:
            self.tabs[tab_id] = tab  # 没关掉：放回去
            with self._inbox_lock:
                self._targets.add(tab.target)
                self._cdp_sessions.add(tab.session)
            raise
        return self._tabs_result(connection)


def _clean_title(title: object) -> str:
    """标签页的标题是网页自己定的：去掉换行和控制字符（Anthropic 的格式不许有），太长的截断。"""
    text = "".join(" " if not char.isprintable() or char in "  " else char for char in str(title or ""))
    text = " ".join(text.split())
    return text if len(text) <= 300 else text[:299] + "…"
