"""YAML 的一个小子集：够读 Goose（~/.config/goose/config.yaml）和 Continue（~/.continue/config.yaml）的配置。

为什么不用 YAML 库：
  1. 智狐只依赖三个包，为了导入两个工具的配置多加一个依赖不值得。
  2. 完整的 YAML 能做的事比"读配置"多得多：锚点和别名（&a、*a，可以拿来做指数膨胀的"十亿笑声"）、
     标签（!!python/object，有的库会照着它构造任意对象）。这两样配置文件里用不上，这里一律明确地报错。
  3. Skill 的 frontmatter 有自己的小解析器（skills/frontmatter.py），但它只认一层（名字: 值），
     而这两个工具的配置要三四层：extensions → 名字 → envs → 变量。

认的写法（缩进只能用空格）：

  名字: 值                     名字:                     名字:                 - 甲
  "带引号的名字": '值'           子名字: 值                - 甲                - 名字: 值
  名字: [甲, "乙", 3]           子名字:                   - 乙                  其他: 值
  名字: {甲: 1, 乙: x}            - 更深的一层
  名字: |                      名字: >-
    保留换行的多行文字             折成一行的多行文字

  # 注释、行尾的 # 注释、开头的 ---、结尾的 ...

值按 YAML 1.2 的规矩认：true / false 是布尔值，整数和小数是数，null、~ 和空着的是 None，别的都是文字
（yes、no、on、off 是文字，不像 YAML 1.1 那样变成布尔值）。加了引号的永远是文字。

不认的写法（锚点 &、别名 *、标签 !、复杂的键 ?、一个文件里好几份文档、跨行的普通文字）都报 YamlError，
写明第几行，读这个文件的地方会把它显示在那个文件下面。
"""

from __future__ import annotations

import re

from ..i18n import t

MAX_DEPTH = 64
_KEY_PLAIN = re.compile(r"([^\s#'\"\[\]{},&*!|>%@`?-][^#]*?|-[^\s#][^#]*?)\s*:(?:\s+|$)")
_INT = re.compile(r"[-+]?[0-9]+")
_FLOAT = re.compile(r"[-+]?(\.[0-9]+|[0-9]+\.[0-9]*|[0-9]+(\.[0-9]*)?[eE][-+]?[0-9]+|\.[0-9]+[eE][-+]?[0-9]+)")
_ESCAPES = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r", "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\"}
_HEX = {"x": 2, "u": 4, "U": 8}


class YamlError(ValueError):
    """写法超出了这个小子集，或者写错了。line 从 1 数。"""

    def __init__(self, message: str, line: int):
        super().__init__(message)
        self.line = line


def loads(text: str) -> object:
    return _Parser(text).document()


class _Parser:
    def __init__(self, text: str):
        self.lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        self.index = 0
        self.depth = 0

    # ---- 行 ----------------------------------------------------------------

    def _peek(self) -> tuple[int, int, str] | None:
        """下一个有内容的行：(下标, 缩进, 去掉缩进的内容)。空行和整行的注释跳过。"""
        while self.index < len(self.lines):
            raw = self.lines[self.index]
            content = raw.lstrip(" ")
            if not content.strip() or content.startswith("#"):
                self.index += 1
                continue
            if content[0] == "\t" or "\t" in raw[: len(raw) - len(content.lstrip(" \t"))]:
                raise YamlError(t("缩进里有制表符：YAML 只能用空格缩进。"), self.index + 1)
            if raw.rstrip() == "...":
                self.index = len(self.lines)  # 文档到这里结束
                return None
            return self.index, len(raw) - len(content), content.rstrip()
        return None

    def document(self) -> object:
        first = self._peek()
        if first is not None and first[2] == "---":
            self.index += 1
        first = self._peek()
        if first is None:
            return None
        value = self._node(first[1])
        rest = self._peek()
        if rest is not None:
            if rest[2] == "---" or rest[2].startswith("--- "):
                raise YamlError(t("一个文件里只能有一份 YAML（这里又出现了 ---）。"), rest[0] + 1)
            raise YamlError(t("这一行的缩进对不上上面的内容。"), rest[0] + 1)
        return value

    def _node(self, indent: int) -> object:
        """从下一行开始、缩进是 indent 的一块：一个表、一个列表，或者单独一个值。"""
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise YamlError(t("嵌套得太深了。"), self.index + 1)
        try:
            number, _, content = self._peek()
            if _is_item(content):
                return self._sequence(indent)
            if _key(content) is not None:
                return self._mapping(indent)
            self.index += 1
            return self._inline(content, number, indent)
        finally:
            self.depth -= 1

    def _mapping(self, indent: int) -> dict:
        out: dict = {}
        while True:
            line = self._peek()
            if line is None or line[1] < indent:
                return out
            number, column, content = line
            if column > indent:
                raise YamlError(t("这一行的缩进对不上上面的内容。"), number + 1)
            if _is_item(content) or content == "---" or content.startswith("--- "):
                return out  # 和名字对齐的列表属于上一层（见 _after_key）；又一份文档由 document 来说
            found = _key(content)
            if found is None:
                raise YamlError(t("看不懂：应该写成“名字: 值”（冒号后面有一个空格）。"), number + 1)
            key, rest = found
            if key in out:
                raise YamlError(t("“{key}”写了两次。", key=key), number + 1)
            self.index += 1
            out[key] = self._after_key(rest, number, indent)

    def _after_key(self, rest: str, number: int, indent: int) -> object:
        """名字后面的值：写在同一行，或者写在下面缩进的几行里。"""
        if rest.strip() and not rest.lstrip().startswith("#"):
            return self._inline(rest.strip(), number, indent)
        following = self._peek()
        if following is None:
            return None
        if following[1] > indent:
            return self._node(following[1])
        if following[1] == indent and _is_item(following[2]):
            return self._sequence(indent)  # 列表可以和它的名字对齐：key:\n- a
        return None

    def _sequence(self, indent: int) -> list:
        out: list = []
        while True:
            line = self._peek()
            if line is None or line[1] < indent:
                return out
            number, column, content = line
            if column > indent:
                raise YamlError(t("这一行的缩进对不上上面的内容。"), number + 1)
            if not _is_item(content):
                return out
            after = content[1:].lstrip(" ")
            if not after or after.startswith("#"):
                self.index += 1
                following = self._peek()
                out.append(self._node(following[1]) if following is not None and following[1] > indent else None)
                continue
            # "- 名字: 值"、"- - 甲"：把这一行改写成从 - 后面那一列开始，按一块新的内容读
            start = column + len(content) - len(after)
            if _key(after) is not None or _is_item(after):
                self.lines[number] = " " * start + after
                out.append(self._node(start))
            else:
                self.index += 1
                out.append(self._inline(after, number, start - 1))

    # ---- 一行里的值 ------------------------------------------------------------

    def _inline(self, text: str, number: int, indent: int) -> object:
        first = text[0]
        if first in "|>":
            return self._block(text, number, indent)
        if first in "[{":
            return self._flow_lines(text, number)
        if first in "&*!?%@`":
            raise YamlError(t("不支持这种写法（{what}）：锚点、别名和标签都读不了，请写成普通的值。", what=text[:20]), number + 1)
        if first in "\"'":
            value, after = _quoted(text, number)
            if after.strip() and not after.strip().startswith("#"):
                raise YamlError(t("引号后面还有别的东西：{rest}", rest=after.strip()[:40]), number + 1)
            self._no_continuation(indent, number)
            return value
        self._no_continuation(indent, number)
        return _plain(_strip_comment(text))

    def _no_continuation(self, indent: int, number: int) -> None:
        """普通的文字不能接着写到下一行（YAML 允许，但很容易写错，这里请你用 | 或者引号）。"""
        following = self._peek()
        if following is not None and following[1] > indent and _key(following[2]) is None and not _is_item(following[2]):
            raise YamlError(t("一段文字写成了好几行：请用 | 开头的多行写法，或者整段加上引号。"), following[0] + 1)

    def _flow_lines(self, text: str, number: int) -> object:
        """[……] 和 {……}：可以写在一行里，也可以跨几行，直到括号配齐。"""
        self.index = number + 1
        joined = _strip_comment(text)
        while _open_brackets(joined) > 0:
            if self.index >= len(self.lines):
                raise YamlError(t("括号没有配齐。"), number + 1)
            joined += " " + _strip_comment(self.lines[self.index].strip())
            self.index += 1
        value, position = _flow(joined, 0, number)
        if joined[position:].strip():
            raise YamlError(t("括号后面还有别的东西：{rest}", rest=joined[position:].strip()[:40]), number + 1)
        return value

    def _block(self, header: str, number: int, indent: int) -> str:
        """| 和 > 开头的多行文字。"""
        match = re.fullmatch(r"([|>])([+-]?)([1-9]?)([+-]?)\s*(#.*)?", header)
        if not match or (match.group(2) and match.group(4)):
            raise YamlError(t("多行文字的开头写得不对：{what}", what=header[:20]), number + 1)
        style, chomp, explicit = match.group(1), match.group(2) or match.group(4), match.group(3)
        self.index = number + 1
        body: list[str] = []
        width = indent + int(explicit) if explicit else None
        while self.index < len(self.lines):
            raw = self.lines[self.index]
            if raw.strip():
                column = len(raw) - len(raw.lstrip(" "))
                if width is None:
                    if column <= indent:
                        break
                    width = column
                if column < width:
                    break
                body.append(raw[width:])
            else:
                body.append("")
            self.index += 1
        while body and body[-1] == "" and chomp != "+":
            body.pop()  # 结尾的空行：先拿掉，再按 chomp 补
        if style == "|":
            text = "\n".join(body)
        else:
            text, previous_blank = "", False
            for line in body:
                if not line:
                    text += "\n"
                    previous_blank = True
                elif line.startswith(" ") or not text or previous_blank or text.endswith("\n"):
                    text += line
                    previous_blank = False
                else:
                    text += " " + line
        if chomp == "-" or not text:
            return text
        return text + "\n"


# ---- 小工具 --------------------------------------------------------------------


def _is_item(content: str) -> bool:
    return content == "-" or content.startswith("- ")


def _key(content: str) -> tuple[str, str] | None:
    """这一行是不是"名字: ……"：是的话返回（名字, 冒号后面的部分）。"""
    if content[0] in "\"'":
        try:
            key, after = _quoted(content, 0)
        except YamlError:
            return None
        if after.startswith(":") and (len(after) == 1 or after[1] in " \t"):
            return key, after[1:]
        return None
    match = _KEY_PLAIN.match(content)
    if not match:
        return None
    return match.group(1).strip(), content[match.end() :]


def _strip_comment(text: str) -> str:
    """去掉行尾的注释："空格 + #" 后面的都是。引号里的 # 不算。"""
    quote = None
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'" and (index == 0 or text[index - 1] in " [{,:"):
            quote = char
        elif char == "#" and (index == 0 or text[index - 1] in " \t"):
            return text[:index].rstrip()
    return text.rstrip()


def _plain(text: str) -> object:
    if text in ("", "~", "null", "Null", "NULL"):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if _INT.fullmatch(text):
        return int(text)
    if _FLOAT.fullmatch(text):
        return float(text)
    return text


def _quoted(text: str, number: int) -> tuple[str, str]:
    """text 以引号开头：返回（引号里的文字，引号后面剩下的）。"""
    quote, out, index = text[0], [], 1
    while index < len(text):
        char = text[index]
        if quote == "'":
            if char == "'":
                if text[index + 1 : index + 2] == "'":
                    out.append("'")
                    index += 2
                    continue
                return "".join(out), text[index + 1 :]
            out.append(char)
            index += 1
            continue
        if char == '"':
            return "".join(out), text[index + 1 :]
        if char == "\\":
            code = text[index + 1 : index + 2]
            if code in _HEX:
                digits = text[index + 2 : index + 2 + _HEX[code]]
                if len(digits) != _HEX[code] or not re.fullmatch(r"[0-9A-Fa-f]+", digits):
                    raise YamlError(t("引号里的转义写得不对：{what}", what=text[index : index + 2 + _HEX[code]]), number + 1)
                out.append(chr(int(digits, 16)))
                index += 2 + _HEX[code]
                continue
            if code not in _ESCAPES:
                raise YamlError(t("引号里的转义写得不对：{what}", what=text[index : index + 2]), number + 1)
            out.append(_ESCAPES[code])
            index += 2
            continue
        out.append(char)
        index += 1
    raise YamlError(t("引号没有结束。"), number + 1)


def _open_brackets(text: str) -> int:
    depth, quote = 0, None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\" and quote == '"':
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        index += 1
    return depth


def _flow(text: str, position: int, number: int, depth: int = 0) -> tuple[object, int]:
    """读一个写在一行里的值（[……]、{……}、带引号的、普通的），返回（值，读到哪儿了）。"""
    if depth > MAX_DEPTH:
        raise YamlError(t("嵌套得太深了。"), number + 1)
    position = _skip(text, position)
    if position >= len(text):
        raise YamlError(t("括号里少了一项。"), number + 1)
    char = text[position]
    if char in "[{":
        closing = "]" if char == "[" else "}"
        items: list = []
        pairs: dict = {}
        position += 1
        while True:
            position = _skip(text, position)
            if position >= len(text):
                raise YamlError(t("括号没有配齐。"), number + 1)
            if text[position] == closing:
                return (items if char == "[" else pairs), position + 1
            if char == "{":
                key, position = _flow(text, position, number, depth + 1)
                position = _skip(text, position)
                if text[position : position + 1] != ":":
                    raise YamlError(t("{……} 里每一项要写成“名字: 值”。"), number + 1)
                position = _skip(text, position + 1)
                value = None
                if position < len(text) and text[position] not in ",}":
                    value, position = _flow(text, position, number, depth + 1)
                if key is None or isinstance(key, list | dict):
                    raise YamlError(t("{……} 里的名字要是一段文字。"), number + 1)
                if str(key) in pairs:
                    raise YamlError(t("“{key}”写了两次。", key=key), number + 1)
                pairs[str(key)] = value
            else:
                value, position = _flow(text, position, number, depth + 1)
                items.append(value)
            position = _skip(text, position)
            if text[position : position + 1] == ",":
                position += 1
            elif text[position : position + 1] != closing:
                raise YamlError(t("括号里的几项之间要用逗号隔开。"), number + 1)
    if char in "\"'":
        value, after = _quoted(text[position:], number)
        return value, len(text) - len(after)
    if char in "&*!?%@`":
        raise YamlError(t("不支持这种写法（{what}）：锚点、别名和标签都读不了，请写成普通的值。", what=text[position : position + 20]), number + 1)
    end = position
    while end < len(text) and text[end] not in ",[]{}" and not (text[end] == ":" and (end + 1 == len(text) or text[end + 1] in " ,]}")):
        end += 1
    return _plain(text[position:end].strip()), end


def _skip(text: str, position: int) -> int:
    while position < len(text) and text[position] in " \t":
        position += 1
    return position
