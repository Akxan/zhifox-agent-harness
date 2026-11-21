"""后端：真正去截图、发鼠标键盘事件的那一层。这里说清楚它要提供什么，以及在这台电脑上用哪一个。

session.py 只通过下面 Backend 里的这几样东西用它，所以换一个平台、或者测试时换成假的（tests/fake_desktop.py），
上面的几层都不用改。坐标一律是"屏幕的坐标"（见 frame.py）：macOS 上是点，Linux 上是像素，换算在 session 里做完了。

键盘为什么要先"换回前台"：每一步都要你在智狐的页面上点"允许"，一点，浏览器就到了最前面，
接下来打的字、按的键都会进到智狐自己的页面里。所以问你的时候记下最前面的是哪个程序（front），
打字、按键之前把它换回来（activate），换不回来就不打（见 session.py 的 _focus）。

截图为什么由后端自己缩小：Python 自己不会处理图片（不装 Pillow 这类库），缩小、裁剪、转成 JPEG 都要靠这台电脑上现成的程序
（macOS 的 sips，Linux 的 ImageMagick），这件事和"用什么截图"一样，每个平台不同。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from .frame import Box, Screen
from .keys import Chord


@dataclass(frozen=True)
class Shot:
    """要的一张图：多大、JPEG 的质量。"""

    width: int
    height: int
    quality: int


class Backend(Protocol):
    platform: str  # "macos" 或 "x11"：危险的组合键按平台认（见 keys.danger），页面上的说明也不同

    def problem(self, kind: str) -> str:
        """现在能不能做这一类事：kind 是 look（截图）或 input（鼠标键盘）。能做返回空字符串，
        不能做返回写给模型的原因（中文，比如"没有屏幕录制的权限"）。不会为了看一眼去请求权限、弹出系统的对话框。"""
        ...

    def checks(self) -> dict:
        """页面上"设置 → 操作整台电脑"要显示的检查结果（权限给了没有、需要的程序装了没有）。格式见各个后端。"""
        ...

    def screen(self) -> Screen: ...
    def capture(self, shots: Sequence[Shot], region: Box | None = None) -> list[bytes]:
        """截一次屏（region 是屏幕上的一块；None 是整个主屏幕），按 shots 缩放成几张 JPEG。只截一次：几张图是同一个瞬间的。"""
        ...

    def mouse(self, kind: str, x: int, y: int, button: str = "left", modifiers: tuple[str, ...] = ()) -> None:
        """kind：move 移过去、down 按下、up 松开、drag 按着键移过去。button：left、right、middle。"""
        ...

    def click(self, x: int, y: int, button: str, count: int, modifiers: tuple[str, ...] = ()) -> None: ...
    def scroll(self, x: int, y: int, direction: str, amount: int, modifiers: tuple[str, ...] = ()) -> None: ...
    def key(self, chord: Chord, kind: str = "tap") -> None:
        """kind：tap 按一下、down 按住、up 松开。"""
        ...

    def type(self, text: str, halted: Callable[[], bool]) -> None:
        """打一段字。一小段一小段地打，每段之前看一眼 halted()：你点了停止就不再打下去。"""
        ...

    def cursor(self) -> tuple[float, float]: ...
    def front(self) -> tuple[str, str]:
        """最前面的那个程序：（名字，记号）。名字给人看（Linux 上是当前窗口的类名和标题）；记号用来把它换回前台
        （macOS 上是进程号，Linux 上是窗口的编号）。看不出来返回 ("", "")。"""
        ...

    def activate(self, handle: str) -> bool:
        """把 front() 给过记号的那个程序换回最前面。做不到返回 False。"""
        ...

    def app_at(self, x: int, y: int) -> str:
        """屏幕上这一点是哪个程序的窗口。看不出来返回空字符串（Linux 上一直是空的，见 x11.py）。"""
        ...


# 为什么这台电脑上用不了（给页面的固定的词，页面按它显示说明）
WAYLAND = "wayland"
NO_DISPLAY = "no_display"
WINDOWS = "windows"  # Windows 上还没有后端（截图、鼠标键盘要另写一套），先明确地说"暂时不支持"
PLATFORM = "platform"


def detect() -> tuple[Backend | None, str]:
    """这台电脑上用哪个后端。返回（后端，""），用不了的话返回（None，原因）。"""
    if sys.platform == "darwin":
        from .macos import MacBackend

        return MacBackend(), ""
    if sys.platform.startswith("linux"):
        # Wayland 不许一个程序截别的程序的图、替别人发鼠标键盘事件（有 XWayland 也一样：xdotool 碰不到原生的 Wayland 窗口）
        if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or (os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY")):
            return None, WAYLAND
        if not os.environ.get("DISPLAY"):
            return None, NO_DISPLAY
        from .x11 import X11Backend

        return X11Backend(os.environ["DISPLAY"]), ""
    if sys.platform == "win32":
        return None, WINDOWS
    return None, PLATFORM
