"""Linux（X11）的后端：xdotool 发鼠标键盘事件，ImageMagick 截图、裁剪、缩小。

要装的两样（智狐不替你装，页面上会写怎么装）：

  xdotool        鼠标键盘（sudo apt install xdotool）
  ImageMagick    截图用它的 import，缩小、裁剪、转成 JPEG 用它的 convert（新版叫 magick）（sudo apt install imagemagick）
                 装了 scrot 的话，截图也可以用 scrot；缩小还是要 ImageMagick

X11 上不需要什么权限：同一个桌面上的程序本来就能互相截图、发事件（这正是 Wayland 不再允许的原因）。
所以这里的"不许碰的程序"只能按最前面的窗口认（类名和标题，xdotool getactivewindow）：
xdotool 说不出屏幕上任意一点是哪个窗口（除非先把鼠标移过去），app_at 一直是空的。

每个命令都用参数列表启动，不经过 shell；环境里去掉各家模型的密钥（和 run_command 一样，见 tools/shell.py）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from ..config import DESKTOP_COMMAND_TIMEOUT
from ..i18n import zh
from ..tools.base import ToolError
from ..tools.shell import SECRET_ENV_VARS
from .backend import Shot, failed
from .frame import Box, Screen
from .keys import Chord

BUTTONS = {"left": "1", "middle": "2", "right": "3"}
WHEEL = {"up": "4", "down": "5", "left": "6", "right": "7"}
MODIFIER_KEYS = {"ctrl": "ctrl", "alt": "alt", "shift": "shift", "cmd": "super"}
# 统一的名字 → X 的键名（keysym）
KEYSYMS = {
    "return": "Return", "tab": "Tab", "space": "space", "backspace": "BackSpace", "delete": "Delete", "escape": "Escape",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right", "home": "Home", "end": "End",
    "pageup": "Page_Up", "pagedown": "Page_Down", "insert": "Insert", "capslock": "Caps_Lock",
    **{f"f{n}": f"F{n}" for n in range(1, 21)},
    "-": "minus", "=": "equal", "[": "bracketleft", "]": "bracketright", "\\": "backslash", ";": "semicolon",
    "'": "apostrophe", ",": "comma", ".": "period", "/": "slash", "`": "grave",
}
TYPE_CHUNK = 40  # 一次 xdotool type 打几个字：打完一段看一眼要不要停
TYPE_DELAY_MS = "8"


class X11Backend:
    platform = "x11"

    def __init__(self, display: str, run: Callable[..., subprocess.CompletedProcess] = subprocess.run, which: Callable[[str], str | None] = shutil.which):
        self.display = display
        self._run = run
        self._which = which

    # ---- 需要的程序 ---------------------------------------------------------------

    def _tool(self, *names: str) -> str | None:
        for name in names:
            found = self._which(name)
            if found:
                return found
        return None

    def checks(self) -> dict:
        """xdotool、capture（import 或 scrot）、convert（convert 或 magick）：装了没有。"""
        return {
            "xdotool": self._tool("xdotool") is not None,
            "capture": self._tool("import", "scrot") is not None,
            "convert": self._tool("magick", "convert") is not None,
        }

    def problem(self, kind: str) -> str:
        found = self.checks()
        if kind == "look" and not (found["capture"] and found["convert"]):
            # 交给模型的话用 zh()：工具卡片上按界面的语言显示（见 ../i18n.py 开头）。程序报的错是资料，原样填进去
            return zh("这台电脑上没有装 ImageMagick，截不了图。请用户装上它（Debian、Ubuntu 上是 sudo apt install imagemagick），然后再试。")
        if kind == "input" and not found["xdotool"]:
            return zh("这台电脑上没有装 xdotool，没法控制鼠标键盘。请用户装上它（Debian、Ubuntu 上是 sudo apt install xdotool），然后再试。")
        if kind == "input" and not found["capture"]:
            return ""  # 只是截不了图：点击、打字照样能做（小预览图没有而已）
        return ""

    # ---- 运行命令 ---------------------------------------------------------------

    def _command(self, args: list[str], data: bytes | None = None) -> bytes:
        env = {name: value for name, value in os.environ.items() if name not in SECRET_ENV_VARS}
        env["DISPLAY"] = self.display
        try:
            done = self._run(args, input=data, capture_output=True, timeout=DESKTOP_COMMAND_TIMEOUT, env=env, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToolError(zh("{program} 没有成功：{error}", program=Path(args[0]).name, error=exc)) from None
        if done.returncode != 0:
            detail = (done.stderr or b"").decode("utf-8", "replace").strip()[:300]
            raise ToolError(failed(Path(args[0]).name, done.returncode, detail))
        return done.stdout or b""

    def _xdotool(self, *args: str) -> str:
        tool = self._tool("xdotool")
        if tool is None:
            raise ToolError(zh("这台电脑上没有装 xdotool。"))
        return self._command([tool, *args]).decode("utf-8", "replace")

    # ---- 屏幕 -------------------------------------------------------------------

    def screen(self) -> Screen:
        parts = self._xdotool("getdisplaygeometry").split()
        try:
            width, height = int(parts[0]), int(parts[1])
        except (IndexError, ValueError):
            raise ToolError(zh("xdotool 没有说出屏幕有多大。")) from None
        return Screen(width, height, width, height)  # X11 上发事件用的就是像素

    def capture(self, shots: Sequence[Shot], region: Box | None = None) -> list[bytes]:
        convert = self._tool("magick", "convert")
        if convert is None:
            raise ToolError(zh("这台电脑上没有装 ImageMagick，截不了图。"))
        raw = self._grab()
        crop = ["-crop", f"{region.width}x{region.height}+{region.x}+{region.y}", "+repage"] if region is not None else []
        return [self._command([convert, "png:-", *crop, "-resize", f"{shot.width}x{shot.height}!", "-quality", str(shot.quality), "jpeg:-"], raw) for shot in shots]

    def _grab(self) -> bytes:
        """整个屏幕的原图（PNG）。"""
        importer = self._tool("import")
        if importer is not None:
            return self._command([importer, "-window", "root", "png:-"])
        scrot = self._tool("scrot")
        if scrot is None:
            raise ToolError(zh("这台电脑上没有可以截图的程序（ImageMagick 的 import，或者 scrot）。"))
        with tempfile.TemporaryDirectory(prefix="zhifox-shot-") as folder:  # 只有你自己能读，用完就删
            path = Path(folder) / "screen.png"
            self._command([scrot, "-o", str(path)])
            return path.read_bytes()

    # ---- 鼠标 -------------------------------------------------------------------

    def _with_modifiers(self, modifiers: tuple[str, ...], *actions: str) -> None:
        """按着修饰键做几个 xdotool 动作，做完（出错也一样）一定松开。"""
        keys = [MODIFIER_KEYS[name] for name in modifiers]
        if keys:
            self._xdotool("keydown", *keys)
        try:
            self._xdotool(*actions)
        finally:
            if keys:
                self._xdotool("keyup", *reversed(keys))

    # mousemove 故意不带 --sync。
    #
    # 不需要它：一次 xdotool 里的"移过去、再按下"走的是同一条连接，X 服务器按顺序处理，点击一定落在移过去之后；
    # xdotool 退出前（XCloseDisplay）会和服务器来回一次（XSync），等这条连接上的请求都处理完才走，
    # 所以下一个命令（下一次点击、getmouselocation）看到的已经是新的位置。
    #
    # 也不能带：Debian、Ubuntu 装的 xdotool 是 3.20160805.1，它的 --sync 等的是"指针离开原来的地方"。
    # 指针本来就在目标点上时（同一点再点一下、先移过去再按下、拖完在原地松开、在当前位置滚动），
    # 它会干等 500 × 30 毫秒 = 15 秒，正好撞上 DESKTOP_COMMAND_TIMEOUT，这一步就算失败了。
    # 说明书写着"不用移就不等"，但代码里要到上游 2024 年的修复（#463）才是这样。
    #
    # （GitHub 上的 Xvfb 测试里"指针总在屏幕正中间"，原因不在这里，在测试怎么开 Xvfb：见 tests/test_desktop_x11.py 的 display。）
    def mouse(self, kind: str, x: int, y: int, button: str = "left", modifiers: tuple[str, ...] = ()) -> None:
        if kind in ("move", "drag"):
            self._with_modifiers(modifiers, "mousemove", str(x), str(y))
        else:
            self._with_modifiers(modifiers, "mousemove", str(x), str(y), "mousedown" if kind == "down" else "mouseup", BUTTONS[button])

    def click(self, x: int, y: int, button: str, count: int, modifiers: tuple[str, ...] = ()) -> None:
        self._with_modifiers(modifiers, "mousemove", str(x), str(y), "click", "--repeat", str(count), "--delay", "80", BUTTONS[button])

    def scroll(self, x: int, y: int, direction: str, amount: int, modifiers: tuple[str, ...] = ()) -> None:
        self._with_modifiers(modifiers, "mousemove", str(x), str(y), "click", "--repeat", str(amount), "--delay", "30", WHEEL[direction])

    def cursor(self) -> tuple[float, float]:
        found = dict(re.findall(r"(\w+)=(\d+)", self._xdotool("getmouselocation", "--shell")))
        return float(found.get("X", 0)), float(found.get("Y", 0))

    # ---- 键盘 -------------------------------------------------------------------

    def key(self, chord: Chord, kind: str = "tap") -> None:
        names = [MODIFIER_KEYS[name] for name in chord.modifiers]
        if chord.key:
            names.append(KEYSYMS.get(chord.key, chord.key))
        if kind == "tap":
            self._xdotool("key", "--clearmodifiers", "+".join(names))  # --clearmodifiers：你手上按着的键不掺进来
        else:
            self._xdotool("keydown" if kind == "down" else "keyup", *(names if kind == "down" else reversed(names)))

    def type(self, text: str, halted: Callable[[], bool]) -> None:
        text = text.replace("\r", "")
        for start in range(0, len(text), TYPE_CHUNK):
            if halted():
                return
            self._xdotool("type", "--delay", TYPE_DELAY_MS, "--", text[start : start + TYPE_CHUNK])

    # ---- 窗口 -------------------------------------------------------------------

    def front(self) -> tuple[str, str]:
        """当前窗口：（类名和标题，比如 "firefox · 新标签页 — Mozilla Firefox"；窗口的编号）。没有窗口（只有桌面）返回 ("", "")。"""
        try:
            window = self._xdotool("getactivewindow").strip()
        except ToolError:
            return "", ""
        parts = []
        for query in ("getwindowclassname", "getwindowname"):
            try:
                found = self._xdotool(query, window).strip()
            except ToolError:
                continue  # xdotool 太老，没有 getwindowclassname
            if found:
                parts.append(found)
        return " · ".join(parts), window

    def activate(self, handle: str) -> bool:
        if not handle.isdigit():
            return False
        try:
            self._xdotool("windowactivate", handle)  # 要窗口管理器支持（几乎都支持）
        except ToolError:
            try:
                self._xdotool("windowfocus", handle)  # 没有窗口管理器（比如只开了 Xvfb）：至少把键盘焦点给它
            except ToolError:
                return False
        return True

    def app_at(self, x: int, y: int) -> str:
        return ""
