"""按键：模型写的 "ctrl+s"、"Return"、"alt+Tab" → 统一的写法；哪些组合键特别危险。

模型写的是 xdotool 的叫法（Anthropic 的文档和参考实现都在 Linux 上用 xdotool）：Return、BackSpace、Page_Down、super……
也常写成别的样子（Enter、Backspace、cmd）。这里把它们都整理成同一套名字（Chord），
macOS 和 Linux 的后端再各自换成自己的键码（macos.py、x11.py）。名字不分大小写。

  "ctrl+shift+t"   一组一起按的键：前面的都是修饰键，最后一个是普通键
  "ctrl+a Delete"  空格隔开的是先后按的几组
  "shift"          只按修饰键也行（hold_key 常这样用）

修饰键：ctrl、alt（macOS 上就是 option ⌥）、shift、cmd（macOS 的 ⌘；Linux 上是 super，也就是 Windows 键）。
模型写 super、meta、win、command 的，都当作 cmd。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..i18n import k, zh
from ..tools.base import ToolError

MODIFIERS = {
    "ctrl": "ctrl", "control": "ctrl", "ctl": "ctrl",
    "alt": "alt", "option": "alt", "opt": "alt", "altgr": "alt",
    "shift": "shift",
    "cmd": "cmd", "command": "cmd", "super": "cmd", "meta": "cmd", "win": "cmd", "windows": "cmd",
}
ORDER = ("ctrl", "alt", "shift", "cmd")  # 按下的顺序（松开反过来）

# 有名字的键：统一的名字 → 认得的写法
_NAMED = {
    "return": ("return", "enter", "kp_enter"),
    "tab": ("tab",),
    "space": ("space", "spacebar"),
    "backspace": ("backspace", "back_space"),
    "delete": ("delete", "del", "forwarddelete"),
    "escape": ("escape", "esc"),
    "up": ("up", "arrowup"),
    "down": ("down", "arrowdown"),
    "left": ("left", "arrowleft"),
    "right": ("right", "arrowright"),
    "home": ("home",),
    "end": ("end",),
    "pageup": ("pageup", "page_up", "prior"),
    "pagedown": ("pagedown", "page_down", "next"),
    "insert": ("insert",),
    "capslock": ("capslock", "caps_lock"),
    **{f"f{n}": (f"f{n}",) for n in range(1, 21)},
}
NAMES = {alias: name for name, aliases in _NAMED.items() for alias in aliases}
# 标点：xdotool 的名字 → 那个字
PUNCTUATION = {
    "minus": "-", "equal": "=", "bracketleft": "[", "bracketright": "]", "backslash": "\\", "semicolon": ";",
    "apostrophe": "'", "comma": ",", "period": ".", "slash": "/", "grave": "`",
}
CHARACTERS = set("abcdefghijklmnopqrstuvwxyz0123456789-=[]\\;',./`")


@dataclass(frozen=True)
class Chord:
    """一起按的一组键。modifiers 按 ORDER 排好；key 是普通键（统一的名字，或者一个小写字母、数字、标点），只按修饰键时是空的。"""

    modifiers: tuple[str, ...]
    key: str

    def shown(self) -> str:
        return "+".join([*self.modifiers, self.key] if self.key else self.modifiers)


def modifiers(text: object) -> tuple[str, ...]:
    """点击、滚动时按着的修饰键（"shift"、"ctrl+shift"）。空的就是没有。写错了抛 ToolError。"""
    if text is None or text == "":
        return ()
    if not isinstance(text, str):
        # 交给模型的话用 zh()：工具卡片上按界面的语言显示（见 ../i18n.py 开头）。按键的名字是模型写的，原样填进去
        raise ToolError(zh("text（同时按着的修饰键）要写成 shift、ctrl+shift 这样的文字。"))
    found = set()
    for name in text.replace(" ", "").split("+"):
        if not name:
            continue
        canonical = MODIFIERS.get(name.lower())
        if canonical is None:
            raise ToolError(zh("不认识的修饰键：{name}。可以用 shift、ctrl、alt、super（macOS 上的 cmd），组合写成 ctrl+shift。", name=name))
        found.add(canonical)
    return tuple(name for name in ORDER if name in found)


def _key(name: str) -> tuple[str, bool]:
    """一个普通键 → （统一的名字，要不要按着 shift）。大写字母当作 shift 加小写字母（xdotool 也是这样）。"""
    lowered = name.lower()
    if lowered in NAMES:
        return NAMES[lowered], False
    if lowered in PUNCTUATION:
        return PUNCTUATION[lowered], False
    if len(name) == 1 and lowered in CHARACTERS:
        return lowered, name.isupper()
    raise ToolError(zh(
        "不认识的按键：{name}。常用的有 Return、Tab、Escape、BackSpace、Delete、Up、Down、Page_Down、F1……，"
        "组合键写成 ctrl+s。要打出一段文字（包括汉字、表情）请用 type。",
        name=name,
    ))


def parse(text: object) -> list[Chord]:
    """'ctrl+a Delete' → 两组。写错了的名字抛 ToolError（交给模型，它会改）。"""
    if not isinstance(text, str) or not text.strip():
        raise ToolError(zh("text 要写要按的键，比如 Return、ctrl+s、alt+Tab。"))
    chords = []
    for part in text.split():
        names = [name for name in part.split("+") if name]
        if not names:
            raise ToolError(zh("按键写得不对：{keys}", keys=part))
        *mods, last = names
        held = set(modifiers("+".join(mods))) if mods else set()
        key = ""
        if last.lower() in MODIFIERS:
            held.add(MODIFIERS[last.lower()])
        else:
            key, shifted = _key(last)
            if shifted:
                held.add("shift")
        chords.append(Chord(tuple(name for name in ORDER if name in held), key))
    return chords


# ---- 危险的组合键 ------------------------------------------------------------------------
# 按下去就收不回来的：退出程序、强制退出、注销、锁屏、关窗口、删文件、清倒废纸篓。确认卡片上会带着一句警告。
# 每一条：（哪个平台，修饰键，键，警告）。平台是 macos、x11，或者 any（两边都算）

DANGEROUS: list[tuple[str, frozenset[str], str, str]] = [
    ("macos", frozenset({"cmd"}), "q", k("⚠ ⌘Q 会退出最前面的那个程序，没保存的内容可能会丢。")),
    ("macos", frozenset({"cmd", "alt"}), "escape", k("⚠ ⌘⌥Esc 会打开“强制退出应用程序”的窗口。")),
    ("macos", frozenset({"cmd", "shift"}), "q", k("⚠ ⌘⇧Q 会退出登录：所有程序都会被关掉。")),
    ("macos", frozenset({"cmd", "shift", "alt"}), "q", k("⚠ ⌘⌥⇧Q 会马上退出登录，不再询问：所有程序都会被关掉。")),
    ("macos", frozenset({"ctrl", "cmd"}), "q", k("⚠ ⌃⌘Q 会锁上屏幕。")),
    ("macos", frozenset({"cmd"}), "w", k("⚠ ⌘W 会关掉当前的窗口或标签页。")),
    ("macos", frozenset({"cmd", "alt"}), "w", k("⚠ ⌘⌥W 会关掉这个程序的所有窗口。")),
    ("macos", frozenset({"cmd"}), "backspace", k("⚠ 在访达里，⌘⌫ 会把选中的文件移到废纸篓。")),
    ("macos", frozenset({"cmd", "shift"}), "backspace", k("⚠ ⌘⇧⌫ 会清倒废纸篓：里面的文件会被永久删除。")),
    ("macos", frozenset({"cmd", "shift", "alt"}), "backspace", k("⚠ ⌘⌥⇧⌫ 会马上清倒废纸篓，不再询问：里面的文件会被永久删除。")),
    ("macos", frozenset({"cmd", "alt"}), "backspace", k("⚠ 在访达里，⌘⌥⌫ 会直接删除选中的文件（不进废纸篓）。")),
    ("any", frozenset({"ctrl", "alt"}), "delete", k("⚠ Ctrl+Alt+Del 通常会打开注销、关机的菜单，有的系统会直接重启。")),
    ("x11", frozenset({"ctrl", "alt"}), "backspace", k("⚠ Ctrl+Alt+Backspace 在有的系统上会直接关掉整个图形界面，所有程序都没了。")),
    ("x11", frozenset({"alt"}), "f4", k("⚠ Alt+F4 会关掉当前的窗口。")),
    ("x11", frozenset({"ctrl"}), "q", k("⚠ Ctrl+Q 会退出很多程序，没保存的内容可能会丢。")),
    ("x11", frozenset({"ctrl"}), "w", k("⚠ Ctrl+W 会关掉当前的窗口或标签页。")),
    ("x11", frozenset({"cmd"}), "l", k("⚠ Super+L 会锁上屏幕。")),
    ("x11", frozenset({"shift"}), "delete", k("⚠ 在文件管理器里，Shift+Delete 会直接删除文件（不进回收站）。")),
    *[("x11", frozenset({"ctrl", "alt"}), f"f{n}", k("⚠ Ctrl+Alt+F1……F12 会切换到另一个控制台，图形界面会看不见。")) for n in range(1, 13)],
]


def danger(chords: list[Chord], platform: str) -> str:
    """这几组键里最先碰到的那个危险组合的警告（还没翻译的原文，给 t()）；都不危险返回空字符串。"""
    for chord in chords:
        held = frozenset(chord.modifiers)
        for where, mods, key, warning in DANGEROUS:
            if where in ("any", platform) and held == mods and chord.key == key:
                return warning
    return ""
