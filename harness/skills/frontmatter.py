"""SKILL.md 开头的"frontmatter"：夹在两行 --- 之间的几行 YAML。

    ---
    name: pdf-processing
    description: Extract text and tables from PDF files. Use when the user mentions PDFs.
    metadata:
      author: example-org
      version: "1.0"
    ---
    （下面是正文：写给模型看的做事步骤）

为什么不用 YAML 库，自己写一个小的：

  1. 规范（agentskills.io）里的 frontmatter 只用到 YAML 很小的一部分：几个"名字: 值"，值是一段文字、
     一个"名字 → 文字"的表，最多再有一个列表。为这点东西加一个依赖不值得。
  2. 完整的 YAML 能做的事远比我们想要的多：锚点和别名（&a、*a，可以拿来做"十亿笑声"那种指数膨胀）、
     标签（!!python/object，某些库会照着它构造任意对象）、把 yes / 1.0 / 2026-11-20 悄悄变成布尔、数字、日期。
     Skill 可能来自网上，这些都是不该有的惊喜。
  3. 写得严格一些，对写 Skill 的人反而是帮助：比如 description 里出现"冒号加空格"，正规的 YAML 解析器会报错
     （Claude Code 遇到这种文件会当作什么字段都没写），这里会直接告诉你是第几行、怎么改。

所以这里只认下面这些写法，别的都明确地报错，并指出行号：

  名字: 普通文字            名字: "双引号里的文字"     名字: '单引号里的文字'
  名字: |                   名字: >                    （多行文字：| 保留换行，> 把换行折成空格）
    多行……                    多行……
  名字:                     名字:                      名字: [甲, 乙]          名字: {甲: 1, 乙: 2}
    子名字: 文字              - 甲
                              - 乙

值一律当作文字，不去猜它是不是数字或布尔值（version: 1.0 读出来就是 "1.0"）。

规范之外的字段（比如 Claude Code 自己用的 argument-hint、hooks）我们用不上，写法再复杂也不报错，
原样留在文件里，只是读不出它的值（见 UNREADABLE）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..i18n import t

# 字段名：字母、数字、下划线、点和连字符。规范里的都是 name、allowed-tools 这种样子
_KEY = re.compile(r"([A-Za-z0-9_][A-Za-z0-9_.-]*):(?=\s|$)(.*)$")
_BLOCK_HEADER = re.compile(r"([|>])([+-]?)([1-9]?)([+-]?)\s*(#.*)?$")
_COMMENT = re.compile(r"\s+#.*$")
_HEX = {"x": 2, "u": 4, "U": 8}
_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r",
    "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0", "L": " ", "P": " ",
}


class FrontmatterError(ValueError):
    """frontmatter 写得不对。line 是出错的那一行（从 1 开始，按整个 SKILL.md 数）。"""

    def __init__(self, message: str, line: int):
        super().__init__(message)
        self.line = line


class _Unreadable:
    """规范之外的字段，写法超出了我们认识的范围：不报错，但也不知道它的值。"""

    def __repr__(self) -> str:
        return "UNREADABLE"


UNREADABLE = _Unreadable()


@dataclass
class Frontmatter:
    fields: dict[str, object]  # 字段名 → 文字、文字的列表、"名字 → 文字"的表；空值是 None
    spans: dict[str, tuple[int, int]] = field(default_factory=dict)  # 每个字段占文件里的哪几行（从 0 数，不含结尾）
    body: str = ""  # 第二行 --- 之后的正文
    body_line: int = 0  # 正文从文件的第几行开始（从 0 数）


def split_lines(text: str) -> list[str]:
    """按行切开。Windows 的 \\r\\n 和老式 Mac 的 \\r 都当作换行；开头的 BOM（有的编辑器会加）去掉。"""
    return text.removeprefix("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")


def parse(text: str, known: frozenset[str] = frozenset()) -> Frontmatter:
    """读出 SKILL.md 的 frontmatter。known 是"我们要用的字段"：它们写错了要报错；别的字段宽松处理。"""
    lines = split_lines(text)
    if lines[0].rstrip() != "---":
        raise FrontmatterError(t("SKILL.md 的第一行必须是 ---，下面写 name 和 description，再用一行 --- 结束。"), 1)
    end = next((index for index in range(1, len(lines)) if lines[index].rstrip() == "---"), None)
    if end is None:
        raise FrontmatterError(t("frontmatter 没有结束：缺少第二行 ---。"), 1)

    fields: dict[str, object] = {}
    spans: dict[str, tuple[int, int]] = {}
    index = 1
    while index < end:
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1  # 空行和注释
            continue
        if line[0] in " \t":
            raise FrontmatterError(t("第 {line} 行不该缩进：字段名要顶格写。", line=index + 1), index + 1)
        match = _KEY.match(line)
        if not match:
            raise FrontmatterError(t("第 {line} 行看不懂：应该写成“名字: 值”（冒号后面有一个空格）。", line=index + 1), index + 1)
        key, rest = match.group(1), match.group(2).strip()
        if key in fields:
            raise FrontmatterError(t("第 {line} 行：{key} 写了两次。", line=index + 1, key=key), index + 1)
        # 这个字段的值可以接着写在下面缩进的几行里
        after = index + 1
        while after < end and (not lines[after].strip() or lines[after][0] in " \t"):
            after += 1
        children = [(number + 1, lines[number]) for number in range(index + 1, after)]
        try:
            fields[key] = _value(rest, children, index + 1)
        except FrontmatterError:
            if key in known:
                raise
            fields[key] = UNREADABLE  # 我们用不上的字段：写法再特别也不拦着
        # 这个字段占到它最后一个有字的行为止，后面跟着的空行不算它的。children 里记的行号从 1 数，
        # 正好就是"从 0 数、不含结尾"的终点
        spans[key] = (index, max((number for number, line in children if line.strip()), default=index + 1))
        index = after
    return Frontmatter(fields=fields, spans=spans, body="\n".join(lines[end + 1 :]), body_line=end + 1)


# ---- 一个字段的值 ----------------------------------------------------------


def _value(rest: str, children: list[tuple[int, str]], number: int) -> object:
    """rest 是冒号后面的部分，children 是下面缩进的几行：[(行号, 原文)]。"""
    meaningful = [(n, line) for n, line in children if line.strip() and not line.strip().startswith("#")]
    for n, line in meaningful:
        if "\t" in line[: len(line) - len(line.lstrip())]:
            raise FrontmatterError(t("第 {line} 行的缩进里有制表符（Tab）。YAML 只认空格。", line=n), n)
    if not rest or rest.startswith("#"):
        if not meaningful:
            return None
        if meaningful[0][1].strip() == "-" or meaningful[0][1].strip().startswith("- "):
            return _block_list(meaningful)
        return _block_map(meaningful)
    if rest[0] in "|>":
        return _block_text(rest, children, number)
    if rest[0] in "\"'":
        text, trailing = _quoted(rest, children, number)
        _nothing_after(trailing, number)
        return text
    if rest[0] == "[":
        if meaningful:
            raise FrontmatterError(t("第 {line} 行：[ ] 写的列表要在同一行里写完。", line=number), number)
        return [_flow_item(item, number) for item in _flow_items(rest, "[", "]", number)]
    if rest[0] == "{":
        if meaningful:
            raise FrontmatterError(t("第 {line} 行：{ } 写的表要在同一行里写完。", line=number), number)
        table: dict[str, str] = {}
        for item in _flow_items(rest, "{", "}", number):
            name, colon, value = item.partition(":")
            if not colon or not name.strip():
                raise FrontmatterError(t("第 {line} 行：{ } 里的每一项都要写成“名字: 值”。", line=number), number)
            table[_flow_item(name, number)] = _flow_item(value, number)
        return table
    return _plain(rest, [(n, line) for n, line in children if not line.strip().startswith("#")], number)


def _plain(first: str, more: list[tuple[int, str]], number: int) -> str:
    """不带引号的文字。可以接着写在下面缩进的几行里，读出来时这几行用空格连起来（YAML 的规矩）。"""
    if first[0] in "&*!%@`":
        raise FrontmatterError(t("第 {line} 行：不支持 YAML 的锚点、别名和标签（以 & * ! 开头的写法）。有这些符号的话，请用引号把整段括起来。", line=number), number)
    if first[0] in ",]}" or first.startswith(("- ", "? ", ": ")) or first in ("-", "?", ":"):
        raise FrontmatterError(t("第 {line} 行：值不能以这个符号开头。请用引号把整段括起来。", line=number), number)
    text, breaks = "", 0
    for n, line in [(number, first), *more]:
        piece = _COMMENT.sub("", line).strip()
        if not piece:
            breaks += 1  # 空行：YAML 里它是一个真正的换行
            continue
        if ": " in piece or piece.endswith(":"):
            raise FrontmatterError(t("第 {line} 行：文字里有“冒号加空格”，YAML 会把它当成新的字段。请用引号把整段括起来。", line=n), n)
        text += ("\n" * breaks if breaks else " ") + piece if text else piece
        breaks = 0
    return text


def _block_text(header: str, children: list[tuple[int, str]], number: int) -> str:
    """| 和 > 开头的多行文字。| 原样保留换行；> 把相邻的行用空格连起来，空行才是换行。"""
    match = _BLOCK_HEADER.match(header)
    if not match or (match.group(2) and match.group(4)):
        raise FrontmatterError(t("第 {line} 行：| 或 > 后面只能跟 + 或 -（还可以有一个表示缩进的数字）。", line=number), number)
    style, chomp, width = match.group(1), match.group(2) or match.group(4), match.group(3)
    lines = [line for _, line in children]
    while lines and not lines[-1].strip():
        lines.pop()  # 末尾的空行先放一边，最后按 + / - 决定要不要保留换行
    trailing_blank = len(children) - len(lines)
    if not lines:
        return ""
    # 缩进以第一个有字的行为准（YAML 的规矩）；也可以在 | 后面用一个数字写明
    indent = int(width) if width else next(len(line) - len(line.lstrip(" ")) for line in lines if line.strip())
    if indent < 1:
        raise FrontmatterError(t("第 {line} 行：多行文字要比字段名多缩进一些。", line=number + 1), number + 1)
    texts = []
    for n, line in children[: len(lines)]:
        if line.strip() and len(line) - len(line.lstrip(" ")) < indent:
            raise FrontmatterError(t("第 {line} 行：多行文字的缩进比第一行少，YAML 会认为它已经结束了。", line=n), n)
        texts.append(line[indent:] if line.strip() else "")
    if style == "|":
        text = "\n".join(texts)
    else:
        # 折叠：相邻的两行用空格连起来；空行变成换行；缩进得更深的行（比如一段代码）前后的换行原样保留
        text, previous = texts[0], texts[0]
        for piece in texts[1:]:
            if not piece:
                text += "\n"
            elif not previous:
                text += piece  # 前面是空行，换行已经加过了
            elif piece.startswith(" ") or previous.startswith(" "):
                text += "\n" + piece
            else:
                text += " " + piece
            previous = piece
    if chomp == "-":
        return text
    if chomp == "+":
        return text + "\n" * (1 + trailing_blank)
    return text + "\n"


def _block_list(items: list[tuple[int, str]]) -> list[str]:
    indent = len(items[0][1]) - len(items[0][1].lstrip(" "))
    result = []
    for n, line in items:
        stripped = line.strip()
        if len(line) - len(line.lstrip(" ")) != indent or not (stripped == "-" or stripped.startswith("- ")):
            raise FrontmatterError(t("第 {line} 行：列表的每一项都要以“- ”开头，并且对齐。", line=n), n)
        value = stripped[1:].strip()
        if value.startswith(("- ", "[", "{")) or (value and _KEY.match(value)):
            raise FrontmatterError(t("第 {line} 行：列表里只能放文字，不能再套列表或表。", line=n), n)
        result.append(_scalar(value, n))
    return result


def _block_map(items: list[tuple[int, str]]) -> dict[str, str]:
    """一层"名字: 文字"的表（metadata 就是这样）。再往里套一层的，我们不认。"""
    indent = len(items[0][1]) - len(items[0][1].lstrip(" "))
    table: dict[str, str] = {}
    for n, line in items:
        if len(line) - len(line.lstrip(" ")) != indent:
            raise FrontmatterError(t("第 {line} 行：表里的每一项都要对齐，而且只能有一层。", line=n), n)
        match = _KEY.match(line.strip())
        if not match:
            raise FrontmatterError(t("第 {line} 行看不懂：应该写成“名字: 值”（冒号后面有一个空格）。", line=n), n)
        name, value = match.group(1), match.group(2).strip()
        if name in table:
            raise FrontmatterError(t("第 {line} 行：{key} 写了两次。", line=n, key=name), n)
        if not value or value.startswith("#"):
            raise FrontmatterError(t("第 {line} 行：{key} 没有值。表里的每一项都要写一段文字。", line=n, key=name), n)
        table[name] = _scalar(value, n)
    return table


def _scalar(value: str, number: int) -> str:
    """写在一行里的一个值：带引号的或者不带引号的文字。"""
    if not value:
        return ""
    if value[0] in "\"'":
        text, trailing = _quoted(value, [], number)
        _nothing_after(trailing, number)
        return text
    if value[0] in "[{|>":
        raise FrontmatterError(t("第 {line} 行：这里只能写一段文字。", line=number), number)
    return _plain(value, [], number)


def _quoted(first: str, more: list[tuple[int, str]], number: int) -> tuple[str, str]:
    """带引号的文字，可以跨几行。返回（文字，右引号后面剩下的部分）。

    YAML 里跨行的带引号文字，换行处折成一个空格，空行才是真正的换行（和 > 一样）。
    """
    quote = first[0]
    pieces = [first[1:], *(line for _, line in more)]
    out: list[str] = []
    pending_breaks = 0
    for row, piece in enumerate(pieces):
        if row:
            piece = piece.lstrip(" \t")
            if not piece.strip():
                pending_breaks += 1
                continue
            if out and out[-1] == "\\\n":
                out.pop()  # 行尾的反斜杠：两行直接连起来，中间不加空格
            else:
                out.append("\n" * pending_breaks if pending_breaks else " ")
            pending_breaks = 0
        index = 0
        while index < len(piece):
            char = piece[index]
            if quote == "'" and char == "'":
                if piece[index + 1 : index + 2] == "'":
                    out.append("'")
                    index += 2
                    continue
                return "".join(out).replace("\\\n", ""), piece[index + 1 :]
            if quote == '"' and char == '"':
                return "".join(out).replace("\\\n", ""), piece[index + 1 :]
            if quote == '"' and char == "\\":
                if index + 1 >= len(piece):
                    out.append("\\\n")  # 行尾的反斜杠：下一行紧接着这一行
                    index += 1
                    continue
                code = piece[index + 1]
                if code in _HEX:
                    digits = piece[index + 2 : index + 2 + _HEX[code]]
                    if len(digits) != _HEX[code] or not re.fullmatch(r"[0-9A-Fa-f]+", digits):
                        raise FrontmatterError(t("第 {line} 行：转义写法 \\{code} 后面要跟 {n} 个十六进制数字。", line=number, code=code, n=_HEX[code]), number)
                    point = int(digits, 16)
                    if 0xD800 <= point <= 0xDFFF or point > 0x10FFFF:
                        raise FrontmatterError(t("第 {line} 行：转义出来的不是一个合法的字符。", line=number), number)
                    out.append(chr(point))
                    index += 2 + _HEX[code]
                    continue
                if code not in _ESCAPES:
                    raise FrontmatterError(t("第 {line} 行：不认识的转义写法 \\{code}。要写反斜杠本身，请写两个：\\\\。", line=number, code=code), number)
                out.append(_ESCAPES[code])
                index += 2
                continue
            out.append(char)
            index += 1
        # 这一行结束了、引号还没关：行尾的空格不算（折行时整个换成一个空格）
        while out and out[-1] in (" ", "\t"):
            out.pop()
    raise FrontmatterError(t("第 {line} 行：引号没有配对，文字没有结束。", line=number), number)


def _nothing_after(trailing: str, number: int) -> None:
    if trailing.strip() and not re.match(r"\s+#", trailing):
        raise FrontmatterError(t("第 {line} 行：右引号后面不能再写别的（注释除外）。", line=number), number)


def _flow_items(text: str, opening: str, closing: str, number: int) -> list[str]:
    """[甲, 乙] 或 {甲: 1} 里的各项（按不在引号里的逗号切开）。"""
    items, current, quote, depth = [], "", None, 0
    for index, char in enumerate(text):
        if index == 0:
            continue
        if quote:
            current += char
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            current += char
        elif char in "[{":
            raise FrontmatterError(t("第 {line} 行：[ ] 和 { } 里不能再套一层。", line=number), number)
        elif char == closing:
            depth = index
            break
        elif char == ",":
            items.append(current)
            current = ""
        else:
            current += char
    if not depth:
        raise FrontmatterError(t("第 {line} 行：{opening} 没有配对的 {closing}。", line=number, opening=opening, closing=closing), number)
    _nothing_after(text[depth + 1 :], number)
    items.append(current)
    if not items[-1].strip():
        items.pop()  # 最后一项后面多一个逗号是允许的
    if any(not item.strip() for item in items):
        raise FrontmatterError(t("第 {line} 行：两个逗号之间是空的。", line=number), number)
    return [item.strip() for item in items]


def _flow_item(item: str, number: int) -> str:
    item = item.strip()
    if item and item[0] in "\"'":
        text, trailing = _quoted(item, [], number)
        if trailing.strip():
            raise FrontmatterError(t("第 {line} 行：右引号后面不能再写别的（注释除外）。", line=number), number)
        return text
    if ": " in item or item.startswith(("&", "*", "!")):
        raise FrontmatterError(t("第 {line} 行：[ ] 和 { } 里的文字有特殊符号时，请加上引号。", line=number), number)
    return item


# ---- 写回去 ----------------------------------------------------------------

# 这些写法 YAML 会当成别的东西（布尔值、空值、数字），写成普通文字之前要加引号
_LOOKS_TYPED = re.compile(r"(?i)(true|false|yes|no|on|off|null|~|[-+]?(\d[\d_]*)?\.?\d+([eE][-+]?\d+)?|0x[0-9a-f]+|\.inf|\.nan)")


def scalar(value: str) -> str:
    """把一段文字写成 YAML 里的一个值。能不加引号就不加（读起来干净）；有特殊符号、换行，或者会被当成
    数字和布尔值的，写成双引号的形式（JSON 的字符串写法正好也是合法的 YAML 双引号写法）。"""
    import json

    plain = (
        value
        and value == value.strip()
        and "\n" not in value
        and ": " not in value
        and " #" not in value
        and not value.endswith(":")
        and value[0] not in "&*!%@`|>\"'[]{},#?-:"
        and not _LOOKS_TYPED.fullmatch(value)
        and all(char.isprintable() for char in value)
    )
    return value if plain else json.dumps(value, ensure_ascii=False)
