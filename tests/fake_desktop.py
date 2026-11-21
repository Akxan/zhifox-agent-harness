"""一台假的"电脑"：扮演 desktop/backend.py 里的 Backend，测试和演示都用它，绝不碰真的屏幕、鼠标和键盘。

它记下每一个动作（events），截图交回两张固定的图（tests/fixtures/ 里编造的一张"桌面"，大图和小图），
"最前面的程序"、"这一点是哪个程序的窗口"按设好的回答。屏幕的大小和 Retina 的倍数都能设，用来测坐标换算。

    fake = FakeDesktop(width=1512, height=982, density=2)
    install(app.desktop, fake)          # 换掉真的后端（conftest 里已经让真的那个永远找不到了）
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from harness.desktop.backend import Shot
from harness.desktop.frame import Box, Screen
from harness.desktop.keys import Chord

FIXTURES = Path(__file__).parent / "fixtures"
FULL = (FIXTURES / "fake-desktop.jpg").read_bytes()
THUMB = (FIXTURES / "fake-desktop-thumb.jpg").read_bytes()

# 演示桌面上那个窗口的位置（按 1280×800 的屏幕画的，见 fixtures/fake-desktop.jpg）
WINDOW = (200, 120, 1060, 600)


class FakeDesktop:
    def __init__(
        self,
        width: int = 1280,
        height: int = 800,
        density: int = 1,
        platform: str = "macos",
        front: tuple[str, str] = ("备忘录", "101"),
        apps: dict[str, str] | None = None,
        app_at: Callable[[int, int], str] | None = None,
    ):
        self.platform = platform
        self.size = Screen(width, height, width * density, height * density)
        self.events: list[tuple] = []
        self.captures: list[tuple[list[Shot], Box | None]] = []
        self.front_app = front
        self.apps = apps if apps is not None else {"101": "备忘录", "9": "Google Chrome"}  # 记号 → 程序（activate 用）
        self.app_at_point = app_at
        self.cursor_at = (0.0, 0.0)
        self.problems: dict[str, str] = {}  # kind → 原因（假装没有权限、没装程序）
        self.activations: list[str] = []
        self.typed: list[str] = []
        self.type_hook: Callable[[str], None] | None = None  # 每打一小段之后调用（测试在这里"点停止"）

    # ---- Backend --------------------------------------------------------------

    def problem(self, kind: str) -> str:
        return self.problems.get(kind, "")

    def checks(self) -> dict:
        return {"screen": "look" not in self.problems, "input": "input" not in self.problems, "host": "终端"}

    def screen(self) -> Screen:
        return self.size

    def capture(self, shots: Sequence[Shot], region: Box | None = None) -> list[bytes]:
        self.captures.append((list(shots), region))
        return [THUMB if shot.width <= 400 else FULL for shot in shots]

    def mouse(self, kind: str, x: int, y: int, button: str = "left", modifiers: tuple[str, ...] = ()) -> None:
        self.events.append(("mouse", kind, x, y, button, tuple(modifiers)))
        self.cursor_at = (float(x), float(y))

    def click(self, x: int, y: int, button: str, count: int, modifiers: tuple[str, ...] = ()) -> None:
        self.events.append(("click", x, y, button, count, tuple(modifiers)))
        self.cursor_at = (float(x), float(y))

    def scroll(self, x: int, y: int, direction: str, amount: int, modifiers: tuple[str, ...] = ()) -> None:
        self.events.append(("scroll", x, y, direction, amount, tuple(modifiers)))
        self.cursor_at = (float(x), float(y))

    def key(self, chord: Chord, kind: str = "tap") -> None:
        self.events.append(("key", chord.shown(), kind))

    def type(self, text: str, halted: Callable[[], bool]) -> None:
        for start in range(0, len(text), 4):
            if halted():
                return
            piece = text[start : start + 4]
            self.typed.append(piece)
            if self.type_hook is not None:
                self.type_hook(piece)
        self.events.append(("type", "".join(self.typed)))

    def cursor(self) -> tuple[float, float]:
        return self.cursor_at

    def front(self) -> tuple[str, str]:
        return self.front_app

    def activate(self, handle: str) -> bool:
        self.activations.append(handle)
        if handle not in self.apps:
            return False
        self.front_app = (self.apps[handle], handle)
        return True

    def app_at(self, x: int, y: int) -> str:
        return self.app_at_point(x, y) if self.app_at_point is not None else self.front_app[0]


def window_or_desktop(x: int, y: int) -> str:
    """演示桌面上：窗口里面是"备忘录"，外面是"访达"（按 1280×800 的屏幕）。"""
    left, top, right, bottom = WINDOW
    return "备忘录" if left <= x < right and top <= y < bottom else "访达"


def install(manager, fake) -> None:
    """把 DesktopManager 的后端换成 fake（manager 只找一次后端，这里直接告诉它"找到的就是这个"）。"""
    manager._backend, manager._reason, manager._detected = fake, "", True
