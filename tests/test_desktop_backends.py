"""两个真的后端（macos.py、x11.py）交给系统的到底是什么：命令行、事件的顺序、修饰键、截图怎么缩小和裁剪。

这里一个真的事件都不发、一张真的截图都不截：
  - Linux 的后端换上假的"运行命令"（记下 xdotool 的参数，截图交回一张编造的 PNG）。缩小、裁剪用的 ImageMagick 照样
    真的运行（它只处理那张编造的图，不碰屏幕），所以 convert 的参数写错了这里能看出来。
  - macOS 的后端换上假的 CoreGraphics（_Quartz）：记下"要发哪些事件、带着什么修饰键、第几下"，一个也不交给系统。
    screencapture 换成写一张编造的 PNG；sips 在 macOS 上照样真的运行（同样只处理那张图）。
在真的屏幕上（虚拟的）跑一遍，见 test_desktop_x11.py。
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.desktop import macos
from harness.desktop.backend import Shot
from harness.desktop.frame import Box
from harness.desktop.keys import Chord, parse
from harness.desktop.macos import MacBackend, _pieces
from harness.desktop.x11 import X11Backend
from harness.tools.base import ToolError


def png(width: int, height: int) -> bytes:
    """一张编造的纯色 PNG（只用标准库）。"""
    rows = b"".join(b"\x00" + b"\x40\x80\xc0" * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def jpeg_size(data: bytes) -> tuple[int, int]:
    """JPEG 的宽和高（读 SOF 段）。"""
    assert data[:2] == b"\xff\xd8", "不是 JPEG"
    index = 2
    while index < len(data) - 9:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return width, height
        if marker == 0xFF or 0xD0 <= marker <= 0xD9:
            index += 1 if marker == 0xFF else 2
            continue
        index += 2 + struct.unpack(">H", data[index + 2 : index + 4])[0]
    raise AssertionError("JPEG 里没有 SOF 段")


# ---- Linux（X11） --------------------------------------------------------------------

IMAGEMAGICK = shutil.which("magick") or shutil.which("convert")


class FakeRun:
    """扮演 subprocess.run：xdotool 只记下参数；import 交回一张编造的截图；convert 真的运行（只处理那张图）。"""

    def __init__(self, screen: tuple[int, int] = (1920, 1080), fail: set[str] | None = None):
        self.commands: list[list[str]] = []
        self.envs: list[dict] = []
        self.screen = screen
        self.fail = fail or set()

    def __call__(self, args, input=None, capture_output=True, timeout=None, env=None, check=False, **_):
        self.commands.append(list(args))
        self.envs.append(env or {})
        name = Path(args[0]).name
        if name == "xdotool":
            verb = args[1]
            if verb in self.fail:
                return SimpleNamespace(returncode=1, stdout=b"", stderr=b"no such window")
            out = {"getdisplaygeometry": f"{self.screen[0]} {self.screen[1]}\n", "getmouselocation": "X=12\nY=34\nSCREEN=0\nWINDOW=99\n", "getactivewindow": "4242\n"}
            if verb == "getwindowclassname":
                return SimpleNamespace(returncode=0, stdout=b"firefox\n", stderr=b"")
            if verb == "getwindowname":
                return SimpleNamespace(returncode=0, stdout="新标签页 — Mozilla Firefox\n".encode(), stderr=b"")
            return SimpleNamespace(returncode=0, stdout=out.get(verb, "").encode(), stderr=b"")
        if name == "import":
            return SimpleNamespace(returncode=0, stdout=png(*self.screen), stderr=b"")
        if name in ("convert", "magick"):
            return subprocess.run(args, input=input, capture_output=True, timeout=30, check=False)
        raise AssertionError(f"没想到会运行 {args}")


def x11(run: FakeRun, tools=("xdotool", "import", "convert", "magick")) -> X11Backend:
    paths = {"convert": IMAGEMAGICK, "magick": IMAGEMAGICK}

    def which(name: str) -> str | None:
        if name not in tools:
            return None
        return paths.get(name) or f"/usr/bin/{name}"

    return X11Backend(":99", run=run, which=which)


def xdotool_calls(run: FakeRun) -> list[list[str]]:
    return [command[1:] for command in run.commands if Path(command[0]).name == "xdotool"]


def test_x11_mouse_and_keys_become_xdotool_commands():
    run = FakeRun()
    backend = x11(run)
    backend.click(10, 20, "left", 2, ("ctrl", "shift"))
    backend.mouse("down", 5, 6, "right")
    backend.mouse("drag", 7, 8)
    backend.scroll(1, 2, "down", 3)
    backend.key(Chord(("ctrl",), "return"))
    backend.key(Chord(("cmd", ), "l"), "down")
    backend.key(Chord(("cmd", ), "l"), "up")
    backend.key(parse("ctrl+minus")[0])
    assert xdotool_calls(run) == [
        ["keydown", "ctrl", "shift"],
        ["mousemove", "10", "20", "click", "--repeat", "2", "--delay", "80", "1"],
        ["keyup", "shift", "ctrl"],  # 修饰键反过来松开
        ["mousemove", "5", "6", "mousedown", "3"],
        ["mousemove", "7", "8"],
        ["mousemove", "1", "2", "click", "--repeat", "3", "--delay", "30", "5"],
        ["key", "--clearmodifiers", "ctrl+Return"],
        ["keydown", "super", "l"],
        ["keyup", "l", "super"],
        ["key", "--clearmodifiers", "ctrl+minus"],
    ]
    assert backend.cursor() == (12.0, 34.0)
    # 每个命令都只在虚拟的那块屏幕上，环境里没有各家的密钥
    assert all(env.get("DISPLAY") == ":99" and "ANTHROPIC_API_KEY" not in env for env in run.envs)


def test_x11_modifiers_are_released_even_when_the_click_fails():
    run = FakeRun(fail={"mousemove"})
    with pytest.raises(ToolError, match="xdotool"):
        x11(run).click(1, 1, "left", 1, ("alt",))
    assert xdotool_calls(run)[-1] == ["keyup", "alt"]


def test_x11_types_in_pieces_and_stops_when_halted():
    run = FakeRun()
    backend = x11(run)
    backend.type("a" * 90 + "\r", lambda: False)
    assert xdotool_calls(run) == [["type", "--delay", "8", "--", "a" * 40], ["type", "--delay", "8", "--", "a" * 40], ["type", "--delay", "8", "--", "a" * 10]]
    run.commands.clear()
    pieces = iter([False, True])
    backend.type("-rf / " * 20, lambda: next(pieces))
    assert len(xdotool_calls(run)) == 1  # "--" 让以减号开头的字不被当成参数


def test_x11_front_window_and_activation():
    run = FakeRun()
    backend = x11(run)
    assert backend.front() == ("firefox · 新标签页 — Mozilla Firefox", "4242")
    assert backend.activate("4242") and xdotool_calls(run)[-1] == ["windowactivate", "4242"]
    run.fail = {"windowactivate"}
    assert backend.activate("4242") and xdotool_calls(run)[-1] == ["windowfocus", "4242"]  # 没有窗口管理器：至少给它键盘焦点
    assert not backend.activate("not-a-window")
    run.fail = {"getactivewindow"}
    assert backend.front() == ("", "")
    assert backend.app_at(1, 1) == ""


def test_x11_says_what_is_missing():
    backend = x11(FakeRun(), tools=())
    assert backend.checks() == {"xdotool": False, "capture": False, "convert": False}
    assert "imagemagick" in backend.problem("look") and "xdotool" in backend.problem("input")
    with pytest.raises(ToolError, match="xdotool"):
        backend.screen()
    ok = x11(FakeRun())
    assert ok.checks() == {"xdotool": True, "capture": True, "convert": True}
    assert ok.problem("look") == "" and ok.problem("input") == ""
    assert ok.screen().width == 1920 and ok.screen().density == 1.0


@pytest.mark.skipif(IMAGEMAGICK is None, reason="这台电脑上没有 ImageMagick")
def test_x11_captures_are_scaled_and_cropped_by_imagemagick():
    run = FakeRun(screen=(640, 400))
    backend = x11(run)
    full, thumb = backend.capture([Shot(500, 312, 70), Shot(320, 200, 50)])
    assert jpeg_size(full) == (500, 312) and jpeg_size(thumb) == (320, 200)
    zoomed = backend.capture([Shot(300, 150, 70)], region=Box(10, 20, 200, 100))[0]
    assert jpeg_size(zoomed) == (300, 150)
    convert = [command for command in run.commands if Path(command[0]).name in ("convert", "magick")][-1]
    assert convert[1:] == ["png:-", "-crop", "200x100+10+20", "+repage", "-resize", "300x150!", "-quality", "70", "jpeg:-"]


# ---- macOS ---------------------------------------------------------------------------


class FakeQuartz:
    """扮演 CoreGraphics：记下每个要发的事件（种类、修饰键、第几下……），一个也不交给系统。"""

    def __init__(self) -> None:
        self.made: dict[int, list] = {}
        self.posted: list[list] = []
        self.released: list[int] = []

    def _make(self, *what) -> int:
        number = len(self.made) + 1
        self.made[number] = list(what)
        return number

    def key_event(self, source, code, down):
        return self._make("key", code, down)

    def mouse_event(self, source, kind, point, button):
        return self._make("mouse", kind, (point.x, point.y), button)

    def scroll_event(self, source, units, count, first, second, third):
        return self._make("scroll", units, count, first, second)

    def set_field(self, event, field, value):
        self.made[event].append(("field", field, value))

    def set_flags(self, event, flags):
        self.made[event].append(("flags", flags))

    def set_unicode(self, event, length, buffer):
        self.made[event].append(("text", bytes(buffer)[: length * 2].decode("utf-16-le")))

    def post(self, tap, event):
        assert tap == macos.HID_TAP
        self.posted.append(self.made[event])

    def release(self, event):
        self.released.append(event)


def mac() -> tuple[MacBackend, FakeQuartz]:
    backend = MacBackend(run=lambda *args, **kwargs: pytest.fail("测试里不该运行任何命令"))
    fake = FakeQuartz()
    backend._quartz = fake  # 有了它，真的 CoreGraphics 就不会被加载
    return backend, fake


SHIFT, CMD, ALT = macos.FLAGS["shift"], macos.FLAGS["cmd"], macos.FLAGS["alt"]


def test_macos_double_click_writes_the_click_number_into_each_event():
    backend, fake = mac()
    backend.click(10, 20, "left", 2, ("shift",))
    kinds = [(event[0], event[1]) for event in fake.posted]
    assert kinds == [("mouse", macos.MOVED), ("mouse", macos.LEFT_DOWN), ("mouse", macos.LEFT_UP), ("mouse", macos.LEFT_DOWN), ("mouse", macos.LEFT_UP)]
    clicks = [next((part[2] for part in event if isinstance(part, tuple) and part[0] == "field"), None) for event in fake.posted[1:]]
    assert clicks == [1, 1, 2, 2]  # 双击的第二下写 2，程序才认得是双击
    assert all(("flags", SHIFT) in event for event in fake.posted)
    assert all(event[2] == (10, 20) for event in fake.posted)
    assert len(fake.released) == len(fake.posted)  # 每个事件都放掉了（CFRelease），不漏内存


def test_macos_right_and_middle_buttons_and_drag():
    backend, fake = mac()
    backend.click(1, 1, "right", 1)
    backend.mouse("down", 2, 2, "middle")
    backend.mouse("drag", 3, 3, "middle")
    backend.mouse("up", 4, 4, "middle")
    assert [event[1] for event in fake.posted] == [macos.MOVED, macos.RIGHT_DOWN, macos.RIGHT_UP, macos.OTHER_DOWN, macos.OTHER_DRAGGED, macos.OTHER_UP]
    assert [event[3] for event in fake.posted][-3:] == [2, 2, 2]  # 中键是第 2 号按钮


def test_macos_key_chords_press_modifiers_first_and_release_them_last():
    backend, fake = mac()
    backend.key(parse("cmd+shift+z")[0])
    codes = [(event[1], event[2]) for event in fake.posted]
    assert codes == [(56, True), (55, True), (6, True), (6, False), (55, False), (56, False)]  # ⇧、⌘ 按下，Z，松开 Z、⌘、⇧
    assert ("flags", CMD | SHIFT) in fake.posted[2] and ("flags", CMD | SHIFT) in fake.posted[3]
    assert ("flags", SHIFT) in fake.posted[4]  # 松开 ⌘ 之后只剩 ⇧
    fake.posted.clear()
    backend.key(Chord(("alt",), "f4"), "down")
    backend.key(Chord(("alt",), "f4"), "up")
    assert [(event[1], event[2]) for event in fake.posted] == [(58, True), (118, True), (118, False), (58, False)]
    with pytest.raises(ToolError):
        backend.key(Chord((), "f21"))


def test_macos_typing_sends_unicode_pieces_and_real_return_keys():
    backend, fake = mac()
    backend.type("你好\n😀 world", lambda: False)
    texts = [part[1] for event in fake.posted for part in event if isinstance(part, tuple) and part[0] == "text"]
    returns = [event for event in fake.posted if event[1] == macos.KEYCODES["return"]]
    assert texts == ["你好", "你好", "😀 world", "😀 world"]  # 每段一个按下、一个松开
    assert len(returns) == 2  # 换行按的是真的回车键


def test_macos_typing_stops_when_halted():
    backend, fake = mac()
    answers = iter([False, True])
    backend.type("a" * 40, lambda: next(answers))
    assert len(fake.posted) == 2  # 只打了第一段


def test_macos_scroll_direction_and_lines():
    backend, fake = mac()
    backend.scroll(5, 5, "down", 2)
    backend.scroll(5, 5, "left", 1, ("shift",))
    scrolls = [event for event in fake.posted if event[0] == "scroll"]
    lines = macos.DESKTOP_SCROLL_LINES
    assert scrolls[0][:5] == ["scroll", macos.SCROLL_LINE, 2, -2 * lines, 0]
    assert scrolls[1][:5] == ["scroll", macos.SCROLL_LINE, 2, 0, lines] and ("flags", SHIFT) in scrolls[1]


def test_text_is_cut_into_utf16_pieces():
    assert _pieces("ab\ncd\te\r") == ["ab", "\n", "cd", "\t", "e"]
    long = "字" * 40
    assert [len(piece) for piece in _pieces(long)] == [16, 16, 8]
    assert all(len(piece.encode("utf-16-le")) // 2 <= macos.TYPE_CHUNK for piece in _pieces("😀" * 20))


def test_macos_says_where_to_grant_the_permissions(monkeypatch):
    backend, _ = mac()
    monkeypatch.setenv("__CFBundleIdentifier", "com.apple.Terminal")
    monkeypatch.setattr(backend, "_can_capture", lambda: False)
    monkeypatch.setattr(backend, "_trusted", lambda: False)
    assert "录屏与系统录音" in backend.problem("look") and "“终端”" in backend.problem("look")
    assert "辅助功能" in backend.problem("input")
    assert backend.checks() == {"screen": False, "input": False, "host": "终端"}
    monkeypatch.setenv("__CFBundleIdentifier", "org.example.Unknown")
    assert backend.host() == "org.example.Unknown"


def test_macos_activation_goes_through_the_app_bundle():
    commands = []
    backend = MacBackend(run=lambda command, **kwargs: commands.append(command) or SimpleNamespace(returncode=0, stderr=b""))

    def pid_path(pid, buffer, size):  # 扮演 libproc 的 proc_pidpath：进程号 → 它的可执行文件
        path = b"/Applications/Notes.app/Contents/MacOS/Notes"
        buffer.value = path
        return len(path)

    backend._quartz = SimpleNamespace(pid_path=pid_path)
    assert backend.activate("123")
    assert commands == [["/usr/bin/open", "/Applications/Notes.app"]]
    assert not backend.activate("not-a-pid")


@pytest.mark.skipif(sys.platform != "darwin" or not Path("/usr/bin/sips").exists(), reason="sips 只在 macOS 上有")
def test_macos_captures_are_scaled_by_sips():
    """screencapture 换成写一张编造的 PNG（绝不截真的屏幕），sips 真的运行：参数写错了这里能看出来。"""
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[0].endswith("screencapture"):
            Path(command[-1]).write_bytes(png(640, 400))
            return SimpleNamespace(returncode=0, stderr=b"")
        assert command[0] == "/usr/bin/sips"
        return subprocess.run(command, capture_output=True, timeout=30, check=False)

    backend = MacBackend(run=run)
    full, thumb = backend.capture([Shot(500, 312, 70), Shot(320, 200, 50)], region=Box(1, 2, 300, 200))
    assert jpeg_size(full) == (500, 312) and jpeg_size(thumb) == (320, 200)
    assert commands[0][:5] == ["/usr/sbin/screencapture", "-x", "-m", "-t", "png"]
    assert commands[0][5:7] == ["-R", "1,2,300,200"]
