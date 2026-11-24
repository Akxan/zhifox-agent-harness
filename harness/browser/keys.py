"""按键的名字 → Chrome 的键盘事件。

模型说"按 Enter"、"按 ctrl+a"、"按 Backspace Backspace"（空格隔开的是先后按几个键，+ 连起来的是一起按）。
Chrome 要的是更底层的东西：这个键叫什么（key）、在键盘上是哪个位置（code）、老式的键码（windowsVirtualKeyCode），
按下时会不会打出一个字（text）。这里把常用的键都列出来，名字不分大小写，也认 xdotool 的叫法（Return、BackSpace……），
因为模型在别的工具里常这么写。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..i18n import zh
from ..tools.base import ToolError

# 修饰键：按住它们的时候，CDP 的事件里要带一个"哪些修饰键按着"的数（每个键占一位）
MODIFIERS = {"alt": 1, "option": 1, "ctrl": 2, "control": 2, "meta": 4, "cmd": 4, "command": 4, "super": 4, "win": 4, "shift": 8}
_MODIFIER_KEYS = {1: ("Alt", "AltLeft", 18), 2: ("Control", "ControlLeft", 17), 4: ("Meta", "MetaLeft", 91), 8: ("Shift", "ShiftLeft", 16)}

# 名字 → (key, code, 键码, 按下时打出的字)
_NAMED: dict[str, tuple[str, str, int, str]] = {
    "enter": ("Enter", "Enter", 13, "\r"),
    "tab": ("Tab", "Tab", 9, ""),
    "backspace": ("Backspace", "Backspace", 8, ""),
    "delete": ("Delete", "Delete", 46, ""),
    "escape": ("Escape", "Escape", 27, ""),
    "space": (" ", "Space", 32, " "),
    "arrowup": ("ArrowUp", "ArrowUp", 38, ""),
    "arrowdown": ("ArrowDown", "ArrowDown", 40, ""),
    "arrowleft": ("ArrowLeft", "ArrowLeft", 37, ""),
    "arrowright": ("ArrowRight", "ArrowRight", 39, ""),
    "home": ("Home", "Home", 36, ""),
    "end": ("End", "End", 35, ""),
    "pageup": ("PageUp", "PageUp", 33, ""),
    "pagedown": ("PageDown", "PageDown", 34, ""),
    "insert": ("Insert", "Insert", 45, ""),
    **{f"f{n}": (f"F{n}", f"F{n}", 111 + n, "") for n in range(1, 13)},
}
_ALIASES = {
    "return": "enter", "kp_enter": "enter", "esc": "escape", "back_space": "backspace", "del": "delete",
    "up": "arrowup", "down": "arrowdown", "left": "arrowleft", "right": "arrowright",
    "page_up": "pageup", "page_down": "pagedown", "prior": "pageup", "next": "pagedown", "spacebar": "space",
}
_PUNCTUATION = {
    "-": ("Minus", 189), "=": ("Equal", 187), "[": ("BracketLeft", 219), "]": ("BracketRight", 221), "\\": ("Backslash", 220),
    ";": ("Semicolon", 186), "'": ("Quote", 222), ",": ("Comma", 188), ".": ("Period", 190), "/": ("Slash", 191), "`": ("Backquote", 192),
}
# 编辑类的快捷键：CDP 发的键盘事件不会自动变成"全选""复制"这些编辑命令，要在事件里点名
_COMMANDS = {"a": "selectAll", "c": "copy", "x": "cut", "v": "paste", "z": "undo", "y": "redo"}


@dataclass(frozen=True)
class Key:
    key: str
    code: str
    keycode: int
    text: str


def _one(name: str) -> Key:
    lowered = name.lower()
    lowered = _ALIASES.get(lowered, lowered)
    if lowered in _NAMED:
        return Key(*_NAMED[lowered])
    if len(name) == 1:
        char = name
        if char.isalpha() and char.isascii():
            return Key(char, f"Key{char.upper()}", ord(char.upper()), char)
        if char.isdigit():
            return Key(char, f"Digit{char}", ord(char), char)
        if char in _PUNCTUATION:
            code, keycode = _PUNCTUATION[char]
            return Key(char, code, keycode, char)
        return Key(char, "", 0, char)  # 别的字（汉字、表情……）：没有对应的键位，照样能打出来
    # 交给模型的话用 zh()：模型拿到中文原文，工具卡片上按界面的语言显示（见 ../i18n.py 开头）。按键的名字是模型写的，原样填进去
    raise ToolError(zh("不认识的按键：{name}。常用的有 Enter、Tab、Escape、Backspace、ArrowDown、PageDown，组合键写成 ctrl+a。", name=name))


@dataclass(frozen=True)
class Chord:
    """一起按的一组键：几个修饰键加一个普通键（"ctrl+shift+t"）。"""

    modifiers: int
    key: Key | None  # 只按修饰键（"shift"）时是 None
    held: tuple[int, ...]  # 按下的修饰键（每个一位），按这个顺序按下、反过来松开

    @property
    def command(self) -> str:
        if self.key is None or not self.modifiers & (2 | 4):
            return ""
        return _COMMANDS.get(self.key.key.lower(), "")


def parse(text: str) -> list[Chord]:
    """'ctrl+a Backspace' → 两组。写错了的名字抛 ToolError（交给模型，它会改）。"""
    chords = []
    for part in text.split():
        names = [name for name in part.split("+") if name]
        if not names:
            raise ToolError(zh("按键写得不对：{keys}", keys=part))
        *mods, last = names
        modifiers, held = 0, []
        for name in mods:
            bit = MODIFIERS.get(name.lower())
            if bit is None:
                raise ToolError(zh("一组键里只能有一个普通键，前面的都要是修饰键（ctrl、shift、alt、cmd）：{keys}", keys=part))
            modifiers |= bit
            held.append(bit)
        key = None
        last_bit = MODIFIERS.get(last.lower())
        if last_bit is not None:  # 只按修饰键（"shift"、"ctrl+shift"）
            modifiers |= last_bit
            held.append(last_bit)
        else:
            key = _one(last)
        chords.append(Chord(modifiers, key, tuple(held)))
    if not chords:
        raise ToolError(zh("没有要按的键。"))
    return chords


def events(chord: Chord) -> list[dict]:
    """按一组键要发给 Chrome 的事件（Input.dispatchKeyEvent 的参数），按顺序发。"""
    out: list[dict] = []
    state = 0
    for bit in chord.held:
        state |= bit
        key, code, keycode = _MODIFIER_KEYS[bit]
        out.append({"type": "rawKeyDown", "key": key, "code": code, "windowsVirtualKeyCode": keycode, "modifiers": state})
    if chord.key is not None:
        pressed = chord.key  # 不叫 key：上下两个循环里的 key 是修饰键的名字（字符串），这里是一个 Key
        down: dict = {"type": "keyDown", "key": pressed.key, "code": pressed.code, "windowsVirtualKeyCode": pressed.keycode, "modifiers": state}
        # 按着 Ctrl / Cmd 的时候不打出字来（ctrl+a 是全选，不是在输入框里写一个 a）
        if pressed.text and not state & (1 | 2 | 4):
            down["text"] = pressed.text.upper() if state & 8 and pressed.text.isalpha() else pressed.text
        else:
            down["type"] = "rawKeyDown"
        if chord.command:
            down["commands"] = [chord.command]
        out.append(down)
        out.append({"type": "keyUp", "key": pressed.key, "code": pressed.code, "windowsVirtualKeyCode": pressed.keycode, "modifiers": state})
    for bit in reversed(chord.held):
        key, code, keycode = _MODIFIER_KEYS[bit]
        state &= ~bit
        out.append({"type": "keyUp", "key": key, "code": code, "windowsVirtualKeyCode": keycode, "modifiers": state})
    return out


def types_text(chords: list[Chord]) -> bool:
    """这几组键会不会打出字来（字母、数字、空格……）或者粘贴、剪切。回车另算（见 presses_enter）。
    permissions.py 靠它判断要不要问：在输入框里这就是输入；不在输入框里，字母键可能触发网页的快捷键。"""
    for chord in chords:
        if chord.key is None:
            continue
        if chord.command in ("paste", "cut"):
            return True
        if chord.key.text and chord.key.key != "Enter" and not chord.modifiers & (1 | 2 | 4):
            return True
    return False


def presses_enter(chords: list[Chord]) -> bool:
    return any(chord.key is not None and chord.key.key == "Enter" for chord in chords)
