"""macOS 的后端：screencapture 截图、sips 缩小；鼠标键盘用 CoreGraphics 的 CGEvent。

都是系统自带的：screencapture 和 sips 是命令，CoreGraphics 用 Python 自带的 ctypes 直接调用，不装任何东西。

## 两样权限，都要你自己在系统设置里给

  屏幕录制（"录屏与系统录音"）   截图要它。没有的话，截出来的只有桌面背景，看不到窗口
  辅助功能                        发鼠标键盘事件要它。没有的话，事件会被系统悄悄丢掉

权限是给"启动智狐的那个程序"的：在"终端"里运行 uv run harness，就要给"终端"；在 VS Code 的终端里运行，就要给 VS Code。
智狐只"看一眼"权限给了没有（CGPreflightScreenCaptureAccess、AXIsProcessTrusted，这两个函数不会弹出对话框），
没有的话告诉你去哪里打开，从来不替你请求、也不改系统设置：会弹出系统对话框的那几个函数（CGRequestScreenCaptureAccess……）一个都不调用。
权限也不会因为没给而被"试一下"：截图之前先看权限，没有就不截（不然 screencapture 自己会弹出请求的对话框）。

## 坐标

CGEvent 用的是"点"，左上角是主屏幕的左上角。截图原图是像素（Retina 上是点的两倍），由 frame.py 换算。
只用主屏幕（screencapture -m）：接了几块屏幕的话，模型只看得到、只点得到主屏幕。

## 哪个程序在哪儿（不许碰的程序、确认卡片上的"落在哪个程序的窗口上"）

CGWindowListCopyWindowInfo 列出屏幕上的窗口，从前往后排，每个带着它属于哪个程序（kCGWindowOwnerName、进程号）和位置。
程序的名字不需要任何权限（窗口的标题才需要屏幕录制的权限，我们不用标题）。

把一个程序换回前台（打字之前，见 backend.py）：由进程号找到它的 .app（proc_pidpath），交给 open。
和在程序坞里点它的图标一样，不需要任何权限（用 AppleScript 的 activate 会要"自动化"的权限，所以不用它）。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from ctypes import (
    POINTER,
    c_bool,
    c_char_p,
    c_double,
    c_int,
    c_int32,
    c_int64,
    c_long,
    c_size_t,
    c_uint16,
    c_uint32,
    c_uint64,
    c_ulong,
    c_void_p,
)
from pathlib import Path

from ..config import DESKTOP_COMMAND_TIMEOUT, DESKTOP_SCROLL_LINES
from ..tools.base import ToolError
from ..tools.shell import SECRET_ENV_VARS
from .backend import Shot
from .frame import Box, Screen
from .keys import Chord

FRAMEWORKS = "/System/Library/Frameworks"


class CGPoint(ctypes.Structure):
    _fields_ = [("x", c_double), ("y", c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", c_double), ("height", c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


# 事件的种类（CGEventType）
LEFT_DOWN, LEFT_UP, RIGHT_DOWN, RIGHT_UP, MOVED, LEFT_DRAGGED, RIGHT_DRAGGED = 1, 2, 3, 4, 5, 6, 7
OTHER_DOWN, OTHER_UP, OTHER_DRAGGED = 25, 26, 27
BUTTONS = {"left": 0, "right": 1, "middle": 2}  # CGMouseButton
_EVENTS = {  # (按钮, 动作) → 事件的种类
    ("left", "down"): LEFT_DOWN, ("left", "up"): LEFT_UP, ("left", "drag"): LEFT_DRAGGED,
    ("right", "down"): RIGHT_DOWN, ("right", "up"): RIGHT_UP, ("right", "drag"): RIGHT_DRAGGED,
    ("middle", "down"): OTHER_DOWN, ("middle", "up"): OTHER_UP, ("middle", "drag"): OTHER_DRAGGED,
}
CLICK_STATE = 1  # kCGMouseEventClickState：第几下（双击的第二下写 2，程序才认得是双击）
HID_TAP = 0  # kCGHIDEventTap：从最底层发出去，和真的键盘鼠标一样
SCROLL_LINE = 1  # kCGScrollEventUnitLine
FLAGS = {"shift": 1 << 17, "ctrl": 1 << 18, "alt": 1 << 19, "cmd": 1 << 20}  # kCGEventFlagMask…
UTF8 = 0x08000100  # kCFStringEncodingUTF8
NUMBER_INT = 9  # kCFNumberIntType
ON_SCREEN_ONLY, EXCLUDE_DESKTOP = 1 << 0, 1 << 4  # kCGWindowListOption…

# 键码（Carbon 的 kVK_…，按的是键盘上的位置，和美式键盘的布局一样）
KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15,
    "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29,
    "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44,
    "n": 45, "m": 46, ".": 47, "`": 50,
    "return": 36, "tab": 48, "space": 49, "backspace": 51, "escape": 53, "capslock": 57, "delete": 117,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121, "insert": 114,  # Mac 键盘上没有 Insert：用 Help 的位置
    "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109,
    "f11": 103, "f12": 111, "f13": 105, "f14": 107, "f15": 113, "f16": 106, "f17": 64, "f18": 79, "f19": 80, "f20": 90,
}
MODIFIER_KEYS = {"cmd": 55, "shift": 56, "alt": 58, "ctrl": 59}
TYPE_CHUNK = 16  # 一个事件里放几个字（CGEventKeyboardSetUnicodeString 一次最多 20 个）
TYPE_PAUSE = 0.008  # 两段之间停一下：发得太快，有的程序会漏字

# 启动智狐的那个程序（权限要给它）：系统给每个从程序里启动的进程都带着 __CFBundleIdentifier
HOSTS = {
    "com.apple.Terminal": "终端",
    "com.googlecode.iterm2": "iTerm",
    "com.microsoft.VSCode": "Visual Studio Code",
    "com.todesktop.230313mzl4w4u92": "Cursor",
    "dev.warp.Warp-Stable": "Warp",
    "com.mitchellh.ghostty": "Ghostty",
    "net.kovidgoyal.kitty": "kitty",
    "io.alacritty": "Alacritty",
    "com.jetbrains.pycharm": "PyCharm",
}


def _function(library: ctypes.CDLL, name: str, restype: object, *argtypes: object):
    found = getattr(library, name)
    found.restype = restype
    found.argtypes = list(argtypes)
    return found


class _Quartz:
    """要用到的 CoreGraphics、CoreFoundation 函数，以及它们的参数类型（ctypes 不知道，要一个个写清楚，写错了会崩）。"""

    def __init__(self) -> None:
        cg = ctypes.CDLL(f"{FRAMEWORKS}/CoreGraphics.framework/CoreGraphics")
        cf = ctypes.CDLL(f"{FRAMEWORKS}/CoreFoundation.framework/CoreFoundation")
        services = ctypes.CDLL(f"{FRAMEWORKS}/ApplicationServices.framework/ApplicationServices")
        self.main_display = _function(cg, "CGMainDisplayID", c_uint32)
        self.display_bounds = _function(cg, "CGDisplayBounds", CGRect, c_uint32)
        self.copy_mode = _function(cg, "CGDisplayCopyDisplayMode", c_void_p, c_uint32)
        self.mode_pixel_width = _function(cg, "CGDisplayModeGetPixelWidth", c_size_t, c_void_p)
        self.mode_pixel_height = _function(cg, "CGDisplayModeGetPixelHeight", c_size_t, c_void_p)
        self.mode_release = _function(cg, "CGDisplayModeRelease", None, c_void_p)
        # 只"看一眼"权限，不请求（不会弹出系统的对话框）
        self.can_capture = _function(cg, "CGPreflightScreenCaptureAccess", c_bool)
        self.trusted = _function(services, "AXIsProcessTrusted", c_bool)
        self.event = _function(cg, "CGEventCreate", c_void_p, c_void_p)
        self.location = _function(cg, "CGEventGetLocation", CGPoint, c_void_p)
        self.mouse_event = _function(cg, "CGEventCreateMouseEvent", c_void_p, c_void_p, c_uint32, CGPoint, c_uint32)
        self.key_event = _function(cg, "CGEventCreateKeyboardEvent", c_void_p, c_void_p, c_uint16, c_bool)
        self.set_unicode = _function(cg, "CGEventKeyboardSetUnicodeString", None, c_void_p, c_ulong, POINTER(c_uint16))
        # 不用 CGEventCreateScrollWheelEvent：它的参数个数不定（C 的可变参数），ctypes 在 Apple 芯片上传不对。"2" 这一版参数是固定的
        self.scroll_event = _function(cg, "CGEventCreateScrollWheelEvent2", c_void_p, c_void_p, c_uint32, c_uint32, c_int32, c_int32, c_int32)
        self.set_flags = _function(cg, "CGEventSetFlags", None, c_void_p, c_uint64)
        self.set_field = _function(cg, "CGEventSetIntegerValueField", None, c_void_p, c_uint32, c_int64)
        self.post = _function(cg, "CGEventPost", None, c_uint32, c_void_p)
        self.release = _function(cf, "CFRelease", None, c_void_p)
        self.window_list = _function(cg, "CGWindowListCopyWindowInfo", c_void_p, c_uint32, c_uint32)
        self.array_count = _function(cf, "CFArrayGetCount", c_long, c_void_p)
        self.array_at = _function(cf, "CFArrayGetValueAtIndex", c_void_p, c_void_p, c_long)
        self.dict_get = _function(cf, "CFDictionaryGetValue", c_void_p, c_void_p, c_void_p)
        self.make_string = _function(cf, "CFStringCreateWithCString", c_void_p, c_void_p, c_char_p, c_uint32)
        self.read_string = _function(cf, "CFStringGetCString", c_bool, c_void_p, c_char_p, c_long, c_uint32)
        self.read_number = _function(cf, "CFNumberGetValue", c_bool, c_void_p, c_int, c_void_p)
        self.read_rect = _function(cg, "CGRectMakeWithDictionaryRepresentation", c_bool, c_void_p, POINTER(CGRect))
        self.pid_path = _function(ctypes.CDLL("/usr/lib/libproc.dylib"), "proc_pidpath", c_int, c_int, c_void_p, c_uint32)
        self._keys: dict[str, int] = {}

    def key(self, name: str) -> int:
        """窗口信息里的一个键（CFString）。只建一次，一直留着。"""
        if name not in self._keys:
            self._keys[name] = self.make_string(None, name.encode(), UTF8)
        return self._keys[name]

    def string(self, value: int | None) -> str:
        if not value:
            return ""
        buffer = ctypes.create_string_buffer(1024)
        return buffer.value.decode("utf-8", "replace") if self.read_string(value, buffer, len(buffer), UTF8) else ""

    def number(self, value: int | None) -> int | None:
        if not value:
            return None
        out = c_int()
        return out.value if self.read_number(value, NUMBER_INT, ctypes.byref(out)) else None


class MacBackend:
    platform = "macos"

    def __init__(self, run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
        self._run = run
        self._quartz: _Quartz | None = None

    @property
    def q(self) -> _Quartz:
        if self._quartz is None:
            self._quartz = _Quartz()  # 用到时才加载：只是打开设置看一眼、或者在别的系统上，都不碰 CoreGraphics
        return self._quartz

    # ---- 权限 -------------------------------------------------------------------

    def _can_capture(self) -> bool:
        try:
            return bool(self.q.can_capture())
        except (OSError, AttributeError):  # 太老的系统（10.15 以前）没有这个函数：那时截图也不用权限
            return True

    def _trusted(self) -> bool:
        try:
            return bool(self.q.trusted())
        except (OSError, AttributeError):
            return False

    def problem(self, kind: str) -> str:
        host = self.host() or "启动智狐的那个程序（终端、iTerm、VS Code……）"
        if kind == "look" and not self._can_capture():
            return (
                f"智狐没有截屏的权限，截不了图。请用户在“系统设置 → 隐私与安全性 → 录屏与系统录音”（旧一些的系统里叫“屏幕录制”）"
                f"里打开“{host}”，然后重新启动智狐。"
            )
        if kind == "input" and not self._trusted():
            return f"智狐没有控制鼠标键盘的权限。请用户在“系统设置 → 隐私与安全性 → 辅助功能”里打开“{host}”，然后重新启动智狐。"
        return ""

    def host(self) -> str:
        """权限要给哪个程序。从终端、VS Code 这类程序里启动的，系统给进程带着那个程序的 __CFBundleIdentifier；
        作为后台服务（launchd）运行时没有任何程序启动它，权限要给的就是智狐用的那个 Python 本身，写出它的完整路径
        （是符号链接的话写真正的那个文件：系统设置里加的是它）。"""
        bundle = os.environ.get("__CFBundleIdentifier", "")
        if bundle:
            return HOSTS.get(bundle, bundle)
        return os.path.realpath(sys.executable)

    def checks(self) -> dict:
        """screen：能不能截图；input：能不能发鼠标键盘事件；host：权限要给哪个程序（看不出来是空的）。"""
        return {"screen": self._can_capture(), "input": self._trusted(), "host": self.host()}

    # ---- 屏幕 -------------------------------------------------------------------

    def screen(self) -> Screen:
        q = self.q
        display = q.main_display()
        bounds = q.display_bounds(display)
        width, height = round(bounds.size.width), round(bounds.size.height)
        pixel_width, pixel_height = width, height
        mode = q.copy_mode(display)
        if mode:
            try:
                pixel_width, pixel_height = int(q.mode_pixel_width(mode)) or width, int(q.mode_pixel_height(mode)) or height
            finally:
                q.mode_release(mode)
        return Screen(width, height, pixel_width, pixel_height)

    def capture(self, shots: Sequence[Shot], region: Box | None = None) -> list[bytes]:
        # 临时文件夹只有你自己能读（mkdtemp 建的是 700），用完马上删：截图里可能有任何东西
        with tempfile.TemporaryDirectory(prefix="zhifox-shot-") as folder:
            raw = Path(folder) / "screen.png"
            command = ["/usr/sbin/screencapture", "-x", "-m", "-t", "png"]  # -x 不出声，-m 只截主屏幕
            if region is not None:
                command += ["-R", f"{region.x},{region.y},{region.width},{region.height}"]  # 按点算，截出来是原图的像素
            self._command(command + [str(raw)])
            if not raw.is_file():
                raise ToolError("截图没有成功（screencapture 没有写出图片）。")
            images = []
            for index, shot in enumerate(shots):
                out = Path(folder) / f"{index}.jpg"
                self._command(["/usr/bin/sips", "-s", "format", "jpeg", "-s", "formatOptions", str(shot.quality), "-z", str(shot.height), str(shot.width), str(raw), "--out", str(out)])
                images.append(out.read_bytes())
            return images

    def _command(self, command: list[str]) -> None:
        env = {name: value for name, value in os.environ.items() if name not in SECRET_ENV_VARS}
        try:
            done = self._run(command, capture_output=True, timeout=DESKTOP_COMMAND_TIMEOUT, env=env, stdin=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToolError(f"{Path(command[0]).name} 没有成功：{exc}") from None
        if done.returncode != 0:
            detail = (done.stderr or b"").decode("utf-8", "replace").strip()[:300]
            raise ToolError(f"{Path(command[0]).name} 没有成功（退出码 {done.returncode}）{('：' + detail) if detail else ''}")

    # ---- 鼠标 -------------------------------------------------------------------

    def _post(self, event: int | None, flags: int = 0) -> None:
        if not event:
            raise ToolError("系统没能生成鼠标键盘事件。")
        q = self.q
        try:
            if flags:
                q.set_flags(event, flags)
            q.post(HID_TAP, event)
        finally:
            q.release(event)

    def _mouse_event(self, kind: str, x: float, y: float, button: str, flags: int = 0, clicks: int = 0) -> None:
        q = self.q
        event_type = MOVED if kind == "move" else _EVENTS[(button, kind)]
        event = q.mouse_event(None, event_type, CGPoint(x, y), BUTTONS[button])
        if event and clicks:
            q.set_field(event, CLICK_STATE, clicks)
        self._post(event, flags)

    def mouse(self, kind: str, x: int, y: int, button: str = "left", modifiers: tuple[str, ...] = ()) -> None:
        self._mouse_event(kind, x, y, button, _flags(modifiers), clicks=1 if kind in ("down", "up") else 0)

    def click(self, x: int, y: int, button: str, count: int, modifiers: tuple[str, ...] = ()) -> None:
        flags = _flags(modifiers)
        self._mouse_event("move", x, y, "left", flags)
        for click in range(1, count + 1):  # 双击、三击：第几下要写在事件里，程序才认得出来
            self._mouse_event("down", x, y, button, flags, clicks=click)
            self._mouse_event("up", x, y, button, flags, clicks=click)
            if click < count:
                time.sleep(0.04)

    def scroll(self, x: int, y: int, direction: str, amount: int, modifiers: tuple[str, ...] = ()) -> None:
        self._mouse_event("move", x, y, "left")
        lines = amount * DESKTOP_SCROLL_LINES
        # 第一个轮是上下（正的往上），第二个轮是左右（正的往左）
        vertical = {"up": lines, "down": -lines}.get(direction, 0)
        horizontal = {"left": lines, "right": -lines}.get(direction, 0)
        self._post(self.q.scroll_event(None, SCROLL_LINE, 2, vertical, horizontal, 0), _flags(modifiers))

    def cursor(self) -> tuple[float, float]:
        q = self.q
        event = q.event(None)
        if not event:
            return 0.0, 0.0
        try:
            point = q.location(event)
            return point.x, point.y
        finally:
            q.release(event)

    # ---- 键盘 -------------------------------------------------------------------

    def key(self, chord: Chord, kind: str = "tap") -> None:
        code = KEYCODES.get(chord.key) if chord.key else None
        if chord.key and code is None:
            raise ToolError(f"在 macOS 上按不了 {chord.key} 这个键。")
        q = self.q
        flags = 0
        if kind in ("tap", "down"):
            for name in chord.modifiers:
                flags |= FLAGS[name]
                self._post(q.key_event(None, MODIFIER_KEYS[name], True), flags)
            if code is not None:
                self._post(q.key_event(None, code, True), flags)
        if kind in ("tap", "up"):
            flags = _flags(chord.modifiers)
            if code is not None:
                self._post(q.key_event(None, code, False), flags)
            for name in reversed(chord.modifiers):
                flags &= ~FLAGS[name]
                self._post(q.key_event(None, MODIFIER_KEYS[name], False), flags)

    def type(self, text: str, halted: Callable[[], bool]) -> None:
        q = self.q
        for piece in _pieces(text):
            if halted():
                return
            if piece in ("\n", "\t"):  # 换行、Tab 按真的键：有的程序不认事件里带着的换行符
                self.key(Chord((), "return" if piece == "\n" else "tab"))
                continue
            units = piece.encode("utf-16-le")
            buffer = (c_uint16 * (len(units) // 2)).from_buffer_copy(units)
            for down in (True, False):
                event = q.key_event(None, 0, down)
                if event:
                    q.set_unicode(event, len(buffer), buffer)
                self._post(event)
            time.sleep(TYPE_PAUSE)

    # ---- 窗口 -------------------------------------------------------------------

    def _windows(self) -> list[tuple[str, int, CGRect]]:
        """屏幕上的普通窗口（第 0 层，不算菜单栏、程序坞），从前往后：（程序的名字，进程号，位置）。"""
        q = self.q
        listing = q.window_list(ON_SCREEN_ONLY | EXCLUDE_DESKTOP, 0)
        if not listing:
            return []
        found = []
        try:
            for index in range(q.array_count(listing)):
                info = q.array_at(listing, index)
                if q.number(q.dict_get(info, q.key("kCGWindowLayer"))) != 0:
                    continue
                rect = CGRect()
                bounds = q.dict_get(info, q.key("kCGWindowBounds"))
                if not bounds or not q.read_rect(bounds, ctypes.byref(rect)):
                    continue
                owner = q.string(q.dict_get(info, q.key("kCGWindowOwnerName")))
                pid = q.number(q.dict_get(info, q.key("kCGWindowOwnerPID"))) or 0
                found.append((owner, pid, rect))
        finally:
            q.release(listing)
        return found

    def _safe_windows(self) -> list[tuple[str, int, CGRect]]:
        try:
            return self._windows()
        except (OSError, AttributeError, ValueError):
            return []

    def front(self) -> tuple[str, str]:
        windows = self._safe_windows()
        return (windows[0][0], str(windows[0][1])) if windows else ("", "")

    def activate(self, handle: str) -> bool:
        """由进程号找到程序的 .app，用 open 把它换到最前面（和点程序坞里的图标一样）。"""
        if not handle.isdigit():
            return False
        buffer = ctypes.create_string_buffer(4096)
        try:
            length = self.q.pid_path(int(handle), buffer, len(buffer))
        except (OSError, AttributeError):
            return False
        path = buffer.value.decode("utf-8", "replace") if length > 0 else ""
        bundle = path[: path.find(".app/") + 4] if ".app/" in path else ""
        if not bundle:
            return False
        try:
            self._command(["/usr/bin/open", bundle])
        except ToolError:
            return False
        return True

    def app_at(self, x: int, y: int) -> str:
        for owner, _, rect in self._safe_windows():
            if rect.origin.x <= x < rect.origin.x + rect.size.width and rect.origin.y <= y < rect.origin.y + rect.size.height:
                return owner
        return ""


def _flags(modifiers: tuple[str, ...]) -> int:
    flags = 0
    for name in modifiers:
        flags |= FLAGS[name]
    return flags


def _pieces(text: str) -> list[str]:
    """一段字切成几小段：换行和 Tab 单独一段，别的每段最多 TYPE_CHUNK 个字（按 UTF-16 算，表情占两个）。"""
    pieces: list[str] = []
    current = ""
    for char in text:
        if char in ("\n", "\t"):
            if current:
                pieces.append(current)
                current = ""
            pieces.append(char)
            continue
        if char == "\r":
            continue
        if len((current + char).encode("utf-16-le")) // 2 > TYPE_CHUNK:
            pieces.append(current)
            current = ""
        current += char
    if current:
        pieces.append(current)
    return pieces
