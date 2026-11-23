from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import (
    BROWSER_SETTLE_SECONDS,
    BROWSER_SHOT_QUALITY,
    BROWSER_THUMB_WIDTH,
    BROWSER_VIEWPORT,
)
from ..tools.base import ToolError
from .cdp import Cancelled, CDPError, Connection
from .session_types import SessionPart

if TYPE_CHECKING:
    from .session_tabs import Tab

PAGE_SCRIPT = (Path(__file__).parent / "page.js").read_text(encoding="utf-8")
WORLD = "zhifox"  # 隔离世界的名字（见 page.js 开头）


@dataclass
class Outcome:
    """一个动作的结果。tools/browser.py 把它变成交给模型的结果（ToolOutput）。"""

    text: str = ""
    image: bytes | None = None  # 要交给模型看的截图（JPEG）。只有 screenshot、zoom 有
    state: dict | None = None  # 标签页的情况（browser_state，格式见 Anthropic 的文档）；没变就是 None
    tabs_only: bool = False  # 管标签页的四个动作：结果只有 state，没有文字（Claude 的工具包规定的）
    shot: str = ""  # 截图大图的编号（存在 data/browser/shots/，点开小图时看）
    thumb: str = ""  # 小预览图（data: 地址），画在对话里的工具卡片上


class SessionPage(SessionPart):
    # ---- 页面里的脚本 ----------------------------------------------------------

    def _js(self, connection: Connection, tab: Tab, function: str, options: dict) -> Any:
        """调用 page.js 里的一个函数，返回它的结果。隔离世界没了（页面跳转过）就重建一个，再试一次。"""
        for attempt in (1, 2):
            if not tab.world:
                tab.world = connection.call(
                    "Page.createIsolatedWorld", {"frameId": tab.target, "worldName": WORLD}, session=tab.session, owner=self
                )["executionContextId"]
                self._evaluate(connection, tab, PAGE_SCRIPT)
            try:
                return self._evaluate(connection, tab, f"globalThis.__zhifox.{function}({json.dumps(options)})")
            except CDPError as exc:
                if attempt == 2 or "context" not in exc.message.lower():
                    raise
                tab.world = 0
            except _ScriptError:
                if attempt == 2:
                    raise ToolError("页面里的脚本出错了，读不出这一页。可以截图看看。") from None
                tab.world = 0
        raise AssertionError("unreachable")

    def _evaluate(self, connection: Connection, tab: Tab, expression: str) -> Any:
        reply = connection.call(
            "Runtime.evaluate",
            {"expression": expression, "contextId": tab.world, "returnByValue": True, "awaitPromise": False},
            session=tab.session,
            owner=self,
        )
        if reply.get("exceptionDetails"):
            raise _ScriptError(str(reply["exceptionDetails"].get("text", "")))
        return (reply.get("result") or {}).get("value")

    # ---- 等待 ---------------------------------------------------------------

    def _sleep(self, seconds: float) -> None:
        if self._halt.wait(max(0.0, seconds)):
            raise Cancelled()

    def _input(self, connection: Connection, tab: Tab, send: Any) -> bool:
        """做一个会动到页面的操作（点击、按键），然后等页面安静下来：点的是链接、按的是回车的话，等新页面加载完。
        返回有没有发生跳转。"""
        started = connection.expect("Page.frameStartedLoading", self._main_frame(tab), owner=self)
        loaded = connection.expect("Page.frameStoppedLoading", self._main_frame(tab), owner=self)
        try:
            send()
            moved = started.wait(0.4) is not None
            if moved:
                tab.world = 0
                loaded.wait(BROWSER_SETTLE_SECONDS)
            else:
                self._sleep(0.1)
            return moved
        finally:
            started.cancel()
            loaded.cancel()

    @staticmethod
    def _main_frame(tab: Tab):
        """只认这个标签页主框架的事件（页面里嵌的 iframe 加载完不算）。主框架的编号就是标签页的 targetId"""
        return lambda params, session: session == tab.session and params.get("frameId") == tab.target

    # ---- 截图 ---------------------------------------------------------------

    def _capture(self, connection: Connection, tab: Tab, clip: dict | None = None, quality: int = BROWSER_SHOT_QUALITY) -> bytes:
        params: dict[str, Any] = {"format": "jpeg", "quality": quality}
        if clip is not None:
            params["clip"] = clip
        if not self.manager.headless:
            connection.call("Page.bringToFront", session=tab.session, owner=self)  # 有窗口时，在后面的标签页截不出图
        data = connection.call("Page.captureScreenshot", params, session=tab.session, owner=self)["data"]
        return base64.b64decode(data)

    def _page_origin(self, connection: Connection, tab: Tab) -> tuple[float, float]:
        """看得到的这一屏，左上角在整个页面里的位置（截图的 clip 按整个页面算）。"""
        metrics = connection.call("Page.getLayoutMetrics", session=tab.session, owner=self)
        view = metrics.get("cssVisualViewport") or {}
        return float(view.get("pageX", 0)), float(view.get("pageY", 0))

    def _preview(self, connection: Connection, tab: Tab, full: bytes | None = None) -> tuple[str, str]:
        """给你看的截图：大图存起来（编号），小图直接给页面（data: 地址）。截不出来（页面没了）就都是空的。"""
        try:
            if full is None:
                full = self._capture(connection, tab)
            x, y = self._page_origin(connection, tab)
            width, height = BROWSER_VIEWPORT
            scale = BROWSER_THUMB_WIDTH / width
            small = self._capture(connection, tab, {"x": x, "y": y, "width": width, "height": height, "scale": scale}, quality=50)
        except (CDPError, KeyError, ValueError):
            return "", ""
        shot = self.manager.shots.save(full)
        return shot, "data:image/jpeg;base64," + base64.b64encode(small).decode()


class _ScriptError(Exception):
    """page.js 里出了错（比如还没装好就被调用了）。"""
