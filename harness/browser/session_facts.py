"""动作之前先看一眼（BrowserSession 的一组方法，见 session.py 开头）：要做的这个动作碰的是什么，交给 permissions.py 决定问不问你。

这里只说事实（Facts：是不是密码框、像不像银行卡号、点它会不会提交表单、是不是另一个网站嵌进来的框……），不做决定。
不为了看一眼去打开浏览器：浏览器没开、这次运行还没有标签页，就照实说"看不到"（_live_tab 返回 None）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..tools.base import ToolError
from . import keys, urls
from .cdp import Connection
from .session_input import Target
from .session_tabs import Tab
from .session_types import SessionPart


@dataclass(frozen=True)
class Facts:
    """一个动作要碰的是什么。permissions.py 靠它决定问不问你、怎么问。这里只说事实，不做决定。"""

    action: str  # navigate / click / type / key / form / other
    reachable: bool = True  # 看得到页面吗（False：浏览器没开、还没有标签页，或者问 Chrome 的时候出了错）
    idle: bool = False  # 浏览器没开，或者这次运行还没有标签页：点、输入都无从做起（工具会直接报错）
    page: str = ""  # 现在这一页的地址
    destination: str = ""  # navigate：要去的地址（整理过的）
    new_site: bool = False  # navigate：这段对话里还没去过这个网站
    found: bool = False  # 找到了要操作的元素
    stale: bool = False  # 给的编号已经作废了（这个动作会直接失败，不用问）
    role: str = ""
    name: str = ""
    editable: bool = False  # 是能输入文字的地方
    password: bool = False
    card: bool = False  # 看起来是银行卡号、安全码这类
    submits: bool = False  # 点它会提交表单
    in_form: bool = False
    cross_origin: bool = False  # 落在另一个网站嵌进来的框（iframe）里，看不到里面是什么
    frame: str = ""  # 那个框的地址
    text: str = ""  # type / form：要输入的内容
    typing_keys: bool = False  # key：按的键会打出字、粘贴（见 keys.types_text）
    enter: bool = False  # key：里面有回车；type：输完会按回车


class SessionFacts(SessionPart):
    # ---- 动作之前先看一眼（给 permissions.py） ------------------------------------

    def _live_tab(self) -> tuple[Connection, Tab] | None:
        """浏览器开着、这个 session 有标签页的话，返回连接和当前的标签页；否则 None（不为了看一眼去打开浏览器）。"""
        connection = self.manager.connection_if_running()
        if connection is None or self._generation != self.manager.generation or not self.tabs:
            return None
        tab = self.tabs.get(self.active) or next(iter(self.tabs.values()))
        self._attach(connection, tab)
        return connection, tab

    def _facts(self, action: str, **args: Any) -> Facts:
        live = self._live_tab()
        page = self._location(*live) if live else ""
        if action == "navigate":
            destination = urls.normalize(args.get("url"))
            site = urls.site_of(destination)
            new_site = bool(site) and site not in self.allowed_sites and site != urls.site_of(page)
            return Facts(action, reachable=live is not None, page=page, destination=destination, new_site=new_site)
        if action not in ("click", "type", "key", "form"):
            return Facts(action, reachable=live is not None, page=page)
        if live is None:
            return Facts(action, reachable=False, idle=True)
        connection, tab = live
        target: Target | None = args.get("target")
        if action == "key" or target is None:
            options: dict[str, Any] = {"focus": True}
        elif target.ref:
            options = {"ref": target.ref}
        else:
            options = {"x": target.x, "y": target.y}
        found = self._js(connection, tab, "describe", options) or {}
        extra: dict[str, Any] = {}
        if action == "key":
            try:
                chords = keys.parse(str(args.get("text", "")))
            except ToolError:
                chords = []  # 写错了的键名：这次调用会直接失败，什么都不会按
            extra = {"typing_keys": keys.types_text(chords), "enter": keys.presses_enter(chords)}
        elif action == "type" and args.get("submit"):
            extra = {"enter": True}
        return Facts(
            action,
            page=page,
            found=bool(found.get("found")),
            stale=bool(found.get("stale")),
            role=str(found.get("role") or ""),
            name=str(found.get("name") or ""),
            editable=bool(found.get("editable")),
            password=bool(found.get("password")),
            card=bool(found.get("card")),
            submits=bool(found.get("submits")),
            in_form=bool(found.get("inForm")),
            cross_origin=bool(found.get("crossOrigin")),
            frame=urls.shown(str(found.get("frameUrl") or ""), 200),
            text=str(args.get("value", "")) if action == "form" else str(args.get("text") or "") if action != "click" else target.describe() if target else "",
            **extra,
        )
