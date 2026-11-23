"""鼠标和键盘（BrowserSession 的一组方法，见 session.py 开头）：click、hover、drag、mouse（按下、松开、移动）、
scroll、scroll_to、type、key、hold_key、form_input。

要操作的位置是一个 Target：一个元素编号（ref，见 page.js），或者窗口里的一个点。_point 把它换成坐标（ref 的话量出元素的中心），
_mouse 发一个鼠标事件，网页收到的和真人的一样（Input.dispatchMouseEvent、dispatchKeyEvent）。
会动到页面的（点击、松开、按回车……）经过 session_page.py 的 _input：做完等页面安静下来，跳转了就告诉模型。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import BROWSER_VIEWPORT
from ..tools.base import ToolError
from . import keys
from .cdp import Connection
from .session_page import Outcome
from .session_tabs import Tab
from .session_types import SessionPart

SCROLL_PIXELS = 100  # 滚轮转一格滚动多少像素


@dataclass(frozen=True)
class Target:
    """要操作的位置：一个元素编号，或者窗口里的一个点（CSS 像素，左上角是 0,0）。"""

    ref: str = ""
    x: int = 0
    y: int = 0

    def describe(self) -> str:
        return self.ref if self.ref else f"({self.x}, {self.y})"


class SessionInput(SessionPart):
    # ---- 要操作的位置、鼠标事件 -----------------------------------------------------

    def _point(self, connection: Connection, tab: Tab, target: Target) -> tuple[int, int, str]:
        """要点的位置。ref 的话量出元素的中心（必要时先滚过去）。返回（x，y，给模型看的一句补充）。"""
        width, height = BROWSER_VIEWPORT
        if not target.ref:
            if not (0 <= target.x < width and 0 <= target.y < height):
                raise ToolError(f"坐标 ({target.x}, {target.y}) 不在窗口里。窗口是 {width}×{height}，左上角是 (0, 0)。")
            return target.x, target.y, ""
        found = self._js(connection, tab, "point", {"ref": target.ref}) or {}
        if found.get("stale"):
            raise ToolError(f"{target.ref} 已经失效或者在这一页上找不到了（页面可能变过）。重新读一遍页面，拿新的编号。")
        if found.get("hidden"):
            raise ToolError(f"{target.ref} 现在看不见（大小是 0），点不到。")
        note = "（它上面好像盖着别的东西，点到的可能不是它，截图看看）" if found.get("covered") else ""
        return int(found["x"]), int(found["y"]), note

    def _mouse(self, connection: Connection, tab: Tab, kind: str, x: int, y: int, button: str = "none", clicks: int = 0, modifiers: int = 0) -> None:
        buttons = {"left": 1, "right": 2, "middle": 4}.get(button, 0) if kind == "mousePressed" else 0
        connection.call(
            "Input.dispatchMouseEvent",
            {"type": kind, "x": x, "y": y, "button": button, "buttons": buttons, "clickCount": clicks, "modifiers": modifiers},
            session=tab.session,
            owner=self,
        )

    # ---- 动作：鼠标 ---------------------------------------------------------------

    def _do_click(self, target: Target, button: str = "left", count: int = 1, modifiers: str = "", tab_id: str | None = None) -> Outcome:
        connection = self._connection()
        tab = self._tab(connection, tab_id, open_one=False)
        x, y, note = self._point(connection, tab, target)
        held = _modifier_bits(modifiers)

        def send() -> None:
            self._mouse(connection, tab, "mouseMoved", x, y, modifiers=held)
            for clicks in range(1, count + 1):
                self._mouse(connection, tab, "mousePressed", x, y, button, clicks, held)
                self._mouse(connection, tab, "mouseReleased", x, y, button, clicks, held)

        moved = self._input(connection, tab, send)
        what = {1: "点了", 2: "双击了", 3: "三击了"}.get(count, "点了") if button == "left" else {"right": "右键点了", "middle": "中键点了"}[button]
        text = f"{what} {target.describe()}{note}。" + ("页面跳转了。" if moved else "")
        return self._finish(connection, tab, text)

    def _do_hover(self, target: Target, tab_id: str | None = None) -> Outcome:
        connection = self._connection()
        tab = self._tab(connection, tab_id, open_one=False)
        x, y, note = self._point(connection, tab, target)
        self._mouse(connection, tab, "mouseMoved", x, y)
        self._sleep(0.2)
        return self._finish(connection, tab, f"鼠标移到了 {target.describe()}{note}。")

    def _do_drag(self, start: Target, target: Target, tab_id: str | None = None) -> Outcome:
        connection = self._connection()
        tab = self._tab(connection, tab_id, open_one=False)
        x0, y0, _ = self._point(connection, tab, start)
        x1, y1, _ = self._point(connection, tab, target)

        def send() -> None:
            self._mouse(connection, tab, "mouseMoved", x0, y0)
            self._mouse(connection, tab, "mousePressed", x0, y0, "left", 1)
            steps = 8
            for step in range(1, steps + 1):
                self._mouse(connection, tab, "mouseMoved", x0 + (x1 - x0) * step // steps, y0 + (y1 - y0) * step // steps, "left")
            self._mouse(connection, tab, "mouseReleased", x1, y1, "left", 1)

        self._input(connection, tab, send)
        return self._finish(connection, tab, f"从 {start.describe()} 拖到了 {target.describe()}。")

    def _do_mouse(self, kind: str, target: Target, tab_id: str | None = None) -> Outcome:
        connection = self._connection()
        tab = self._tab(connection, tab_id, open_one=False)
        x, y, _ = self._point(connection, tab, target)
        if kind == "down":
            self._mouse(connection, tab, "mousePressed", x, y, "left", 1)
            text = f"在 {target.describe()} 按下了左键（还没松开）。"
        elif kind == "up":
            self._input(connection, tab, lambda: self._mouse(connection, tab, "mouseReleased", x, y, "left", 1))
            text = f"在 {target.describe()} 松开了左键。"
        else:
            self._mouse(connection, tab, "mouseMoved", x, y)
            text = f"鼠标移到了 {target.describe()}。"
        return self._finish(connection, tab, text)

    def _do_scroll(self, target: Target | None, direction: str, amount: int = 3, tab_id: str | None = None) -> Outcome:
        if direction not in ("up", "down", "left", "right"):
            raise ToolError("scroll_direction 只能是 up、down、left、right。")
        amount = max(1, min(10, int(amount)))
        connection = self._connection()
        tab = self._tab(connection, tab_id, open_one=False)
        width, height = BROWSER_VIEWPORT
        if target is None:
            x, y = width // 2, height // 2
        else:
            x, y, _ = self._point(connection, tab, target)
        delta = amount * SCROLL_PIXELS
        dx = {"left": -delta, "right": delta}.get(direction, 0)
        dy = {"up": -delta, "down": delta}.get(direction, 0)
        connection.call("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y, "deltaX": dx, "deltaY": dy}, session=tab.session, owner=self)
        self._sleep(0.25)
        word = {"up": "向上", "down": "向下", "left": "向左", "right": "向右"}[direction]
        return self._finish(connection, tab, f"{word}滚动了 {amount} 格。")

    def _do_scroll_to(self, target: Target, tab_id: str | None = None) -> Outcome:
        if not target.ref:
            raise ToolError("scroll_to 要给一个元素编号（ref）。")
        connection = self._connection()
        tab = self._tab(connection, tab_id, open_one=False)
        found = self._js(connection, tab, "scrollTo", {"ref": target.ref}) or {}
        if found.get("stale"):
            raise ToolError(f"{target.ref} 已经失效或者在这一页上找不到了。重新读一遍页面，拿新的编号。")
        self._sleep(0.15)
        return self._finish(connection, tab, f"把 {target.ref} 滚到了窗口中间。")

    # ---- 动作：键盘、表单 -----------------------------------------------------------

    def _do_type(self, text: str, tab_id: str | None = None, ref: str = "", submit: bool = False) -> Outcome:
        """在当前选中的地方输入文字。ref：先选中那个元素（别家模型用的 browser_type）；submit：输完按回车。"""
        if not isinstance(text, str) or not text:
            raise ToolError("没有要输入的文字。")
        connection = self._connection()
        tab = self._tab(connection, tab_id, open_one=False)
        if ref:
            focused = self._js(connection, tab, "focus", {"ref": ref}) or {}
            if focused.get("stale"):
                raise ToolError(f"{ref} 已经失效或者在这一页上找不到了。重新读一遍页面，拿新的编号。")
        connection.call("Input.insertText", {"text": text}, session=tab.session, owner=self)
        self._sleep(0.1)
        moved = False
        if submit:
            enter = keys.parse("Enter")[0]
            moved = self._input(connection, tab, lambda: [connection.call("Input.dispatchKeyEvent", event, session=tab.session, owner=self) for event in keys.events(enter)])
        where = f"在 {ref} 里" if ref else ""
        return self._finish(connection, tab, f"{where}输入了 {len(text)} 个字。" + ("按了回车。" if submit else "") + ("页面跳转了。" if moved else ""))

    def _do_key(self, text: str, repeat: int = 1, tab_id: str | None = None) -> Outcome:
        chords = keys.parse(text)
        repeat = max(1, min(100, int(repeat)))
        connection = self._connection()
        tab = self._tab(connection, tab_id, open_one=False)

        def send() -> None:
            for _ in range(repeat):
                for chord in chords:
                    for event in keys.events(chord):
                        connection.call("Input.dispatchKeyEvent", event, session=tab.session, owner=self)

        moved = self._input(connection, tab, send)
        times = f" {repeat} 次" if repeat > 1 else ""
        return self._finish(connection, tab, f"按了 {text}{times}。" + ("页面跳转了。" if moved else ""))

    def _do_hold_key(self, text: str, duration: float, tab_id: str | None = None) -> Outcome:
        chords = keys.parse(text)
        if len(chords) != 1:
            raise ToolError("hold_key 一次只能按住一组键（比如 shift 或者 ctrl+a）。")
        duration = max(0.0, min(30.0, float(duration)))
        connection = self._connection()
        tab = self._tab(connection, tab_id, open_one=False)
        events = keys.events(chords[0])
        downs = [event for event in events if event["type"] != "keyUp"]
        ups = [event for event in events if event["type"] == "keyUp"]
        for event in downs:
            connection.call("Input.dispatchKeyEvent", event, session=tab.session, owner=self)
        try:
            self._sleep(duration)
        finally:
            for event in ups:
                connection.call("Input.dispatchKeyEvent", event, session=tab.session, owner=self)
        return self._finish(connection, tab, f"按住 {text} {duration:g} 秒后松开了。")

    def _do_form_input(self, target: Target, value: object, tab_id: str | None = None) -> Outcome:
        if not target.ref:
            raise ToolError("form_input 要给一个元素编号（ref）。")
        if not isinstance(value, (str, int, float, bool)):
            raise ToolError("value 只能是文字、数字或者 true / false。")
        connection = self._connection()
        tab = self._tab(connection, tab_id, open_one=False)
        done = self._js(connection, tab, "setValue", {"ref": target.ref, "value": value}) or {}
        if done.get("stale"):
            raise ToolError(f"{target.ref} 已经失效或者在这一页上找不到了。重新读一遍页面，拿新的编号。")
        if done.get("error") == "no-option":
            raise ToolError(f"{target.ref} 这个下拉框里没有“{value}”。可以选的有：" + "、".join(map(str, done.get("options", []))))
        if done.get("error") == "readonly":
            raise ToolError(f"{target.ref} 是只读的或者被禁用了，改不了。")
        if done.get("error"):
            raise ToolError(f"{target.ref} 不是可以填的表单元素（它是 {done.get('role') or '别的东西'}）。")
        if done.get("typing"):
            connection.call("Input.insertText", {"text": str(value)}, session=tab.session, owner=self)
        shown = f"（现在是：{done['shown']}）" if done.get("shown") else ""
        return self._finish(connection, tab, f"把 {target.ref} 设成了 {value!s}{shown}。")


def _modifier_bits(text: str) -> int:
    bits = 0
    for name in str(text or "").replace(" ", "").split("+"):
        if not name:
            continue
        bit = keys.MODIFIERS.get(name.lower())
        if bit is None:
            raise ToolError(f"不认识的修饰键：{name}。可以用 shift、ctrl、alt、cmd，组合写成 ctrl+shift。")
        bits |= bit
    return bits
