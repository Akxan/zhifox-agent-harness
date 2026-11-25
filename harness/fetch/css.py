"""读 CSS 的几样小工具：读网页时认"藏起来的字"用（哪些写法算藏起来见 hidden.py，一个元素身上是哪几条见 cascade.py）。

这里只管写法，不管意思。浏览器怎么读一段 CSS（CSS Syntax Level 3），我们只照着做用得到的几步，因为网页可以故意换着写法
绕过一个简单的正则（display:/**/none、display:\\6e one，浏览器照样当成 display:none）：
  注释 /* … */：换成一个空格。注释本来就把两边隔开，disp/**/lay 是两个词，不是 display；引号里的 /* 不是注释。
  转义：\\6e 是十六进制的 n（紧跟着的一个空白是转义的一部分），\\: 就是冒号本身，\\n 就是字母 n。
  声明：按顶层的 ; 分开（括号、方括号、引号里的不算，url(data:…;base64,…) 是一个值），每一条按第一个顶层的 : 分成名字和值；
    值的末尾可以是 !important（叹号后面可以有空白，大小写都行）。名字和值都换成小写：我们只比关键字和数，不看字体名。
  样式表：一条一条的"选择器 { 声明 }"，外面可以套 @media、@supports、@layer……（rules）。
  选择器：只拆出最后一段（这条规则真正落在谁身上），记下它的标签、class、id，是不是"简单的"，和浏览器排先后用的"具体程度"。
写坏了的 CSS 和浏览器一样处理：没关上的括号、引号、注释吃到最后，看不懂的一条整条不算。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

# 字符串（没关上的到行末为止）、转义、注释：这几样里面的 { } ; : 都不算数
_STRING = r'"(?:\\.|[^"\\\n])*"?|\'(?:\\.|[^\'\\\n])*\'?'
_COMMENT = re.compile(rf"{_STRING}|\\.|/\*.*?(?:\*/|\Z)", re.DOTALL)
_SIGNIFICANT = re.compile(rf"{_STRING}|\\.|[()\[\]{{}};,:]", re.DOTALL)
_OPENING, _CLOSING = frozenset("([{"), frozenset(")]}")
_ESCAPE = re.compile(r"\\(?:([0-9a-fA-F]{1,6})(?:\r\n|[ \t\r\n\f])?|(\n)|(.))", re.DOTALL)
_IMPORTANT = re.compile(r"!\s*important\s*$", re.IGNORECASE)
# 选择器里的一个名字（标签、class、id、伪类）：字母数字、- _、非 ASCII 的字，和转义
_NAME = re.compile(r"(?:[\w-]|[^\x00-\x7f]|\\[0-9a-fA-F]{1,6}(?:\r\n|[ \t\r\n\f])?|\\[^\n\r\f0-9a-fA-F])+")
_COMBINATOR = frozenset(" \t\r\n\f>+~")
# 写成一个冒号的老式伪元素（:before 和 ::before 是一回事）
_OLD_PSEUDO_ELEMENTS = frozenset({"before", "after", "first-line", "first-letter"})


def without_comments(text: str) -> str:
    """去掉注释，每一段换成一个空格（见文件开头）。"""
    return _COMMENT.sub(lambda found: " " if found.group().startswith("/*") else found.group(), text)


def unescape(text: str) -> str:
    """解开 CSS 的转义：\\6e → n，\\: → :。0、代理区、超出 Unicode 的换成 U+FFFD（和浏览器一样）。"""

    def one(found: re.Match[str]) -> str:
        if found.group(1):
            code = int(found.group(1), 16)
            return chr(code) if 0 < code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else "\ufffd"
        if found.group(2):
            return ""  # 反斜杠加换行：字符串里的续行
        return found.group(3)

    return _ESCAPE.sub(one, text)


def split(text: str, separator: str) -> list[str]:
    """按顶层的 separator（";"、","、":"）切开：括号、方括号、花括号、引号里的，转义过的，都不算。"""
    pieces: list[str] = []
    start = depth = 0
    for found in _SIGNIFICANT.finditer(text):
        token = found.group()
        if token in _OPENING:
            depth += 1
        elif token in _CLOSING:
            depth = max(depth - 1, 0)
        elif token == separator and depth == 0:
            pieces.append(text[start : found.start()])
            start = found.end()
    pieces.append(text[start:])
    return pieces


@dataclass(frozen=True)
class Declaration:
    name: str  # 小写的属性名
    value: str  # 小写、空白收成一个空格、去掉了 !important
    important: bool


def declarations(text: str) -> list[Declaration]:
    """一段声明（style="…" 的内容，或者一条规则 {} 里面的）→ 一条一条，按写的先后。自定义属性（--x）不要。"""
    found: list[Declaration] = []
    for piece in split(without_comments(text), ";"):
        before, *rest = split(piece, ":")
        if not rest:
            continue  # 没有冒号：不是声明（也可能是嵌套的规则），整条不算
        name = unescape(before).strip().lower()
        if not name or name.startswith("--"):
            continue
        value = unescape(piece[len(before) + 1 :])  # 第一个顶层的冒号后面的全部（值里还可以有冒号）
        important = _IMPORTANT.search(value)
        if important:
            value = value[: important.start()]
        value = " ".join(value.split()).lower()
        if value:
            found.append(Declaration(name, value, bool(important)))
    return found


# ---- 样式表 ------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    selectors: str  # 选择器那一段（还没拆）
    body: str  # {} 里面的声明
    conditional: bool  # 套在说不准用不用得上的条件里（@media (max-width…)、@supports、@container……）


def media(query: str) -> str:
    """@media 或 <style media> 的条件，在一块普通的屏幕上："always"（all、screen）、"never"（只给打印的）、"maybe"（要看屏幕多宽这一类）。"""
    answers: set[str] = set()
    for one in split(query.lower(), ","):
        one = " ".join(one.split())
        one = one.removeprefix("only ")
        if one in ("", "all", "screen"):
            answers.add("always")
        elif one in ("print", "speech") or one.startswith(("print ", "speech ")):
            answers.add("never")
        else:
            answers.add("maybe")
    if "always" in answers:
        return "always"
    return "never" if answers == {"never"} else "maybe"


def rules(sheet: str, conditional: bool = False) -> Iterator[Rule]:
    """一整段样式表 → 一条一条的规则（套在 @media 这类里面的也拆出来，标上 conditional）。

    @layer 里的照常算（它只排先后，不是条件）；只给打印的 @media 不要；@font-face、@keyframes、@page 这类里面不是给元素的规则，跳过。
    """
    yield from _walk(without_comments(sheet), conditional)


def _walk(text: str, conditional: bool) -> Iterator[Rule]:
    start = depth = parens = opened = 0
    for found in _SIGNIFICANT.finditer(text):
        token = found.group()
        if token == "{":
            if depth == 0:
                opened = found.end()
            depth += 1
        elif token == "}":
            if depth == 0:
                start = found.end()  # 多出来的 }：跳过
                continue
            depth -= 1
            if depth == 0:
                yield from _block(text[start : opened - 1], text[opened : found.start()], conditional)
                start = found.end()
        elif depth == 0 and token == "(":
            parens += 1
        elif depth == 0 and token == ")":
            parens = max(parens - 1, 0)
        elif token == ";" and depth == 0 and parens == 0:
            start = found.end()  # @import、@charset 这类一句话的，没有 {}
    if depth:
        yield from _block(text[start : opened - 1], text[opened:], conditional)  # 没关上的块吃到最后


def _block(prelude: str, body: str, conditional: bool) -> Iterator[Rule]:
    prelude = prelude.strip()
    if not prelude.startswith("@"):
        yield Rule(prelude, body, conditional)
        return
    found = _NAME.match(prelude, 1)
    if not found:
        return
    name, condition = unescape(found.group()).lower(), prelude[found.end() :]
    if name == "layer":
        yield from _walk(body, conditional)
    elif name == "media":
        answer = media(condition)
        if answer != "never":
            yield from _walk(body, conditional or answer == "maybe")
    elif name in ("supports", "container", "scope", "document", "-moz-document", "starting-style"):
        yield from _walk(body, True)


# ---- 选择器 ------------------------------------------------------------------------


@dataclass(frozen=True)
class Selector:
    """一个选择器的最后一段（规则真正落在的那个元素），和整个选择器的几样说法。"""

    tag: str  # 小写的标签名，没写（或者写的 *）是 ""
    classes: tuple[str, ...]
    ids: tuple[str, ...]
    simple: bool  # 只有一段，而且只有标签、class、id：.a、#b、div.a（没有 [属性]、:hover、外层）
    specificity: tuple[int, int, int]  # （id 几个，class、属性、伪类几个，标签、伪元素几个）：浏览器排先后用的


def selectors(text: str) -> list[Selector]:
    """逗号分开的一串选择器 → 用得上的那几个。落在伪元素（::before）上的、看不懂的（&、命名空间）不要。"""
    found = []
    for piece in split(text, ","):
        selector = _selector(piece.strip())
        if selector is not None:
            found.append(selector)
    return found


def _selector(text: str) -> Selector | None:
    if not text:
        return None
    ids = classes = types = 0
    parts = 1
    tag, conditions = "", False  # 这几样只记最后一段的
    own_classes: list[str] = []
    own_ids: list[str] = []
    fresh = True  # 这一段还什么都没读到
    i = 0
    while i < len(text):
        char = text[i]
        if char in _COMBINATOR:
            while i < len(text) and text[i] in _COMBINATOR:
                i += 1
            if fresh:
                return None  # 开头就是组合符：嵌套规则里的 > .a，我们不看嵌套
            parts += 1
            tag, own_classes, own_ids, conditions, fresh = "", [], [], False, True
            continue
        fresh = False
        if char == "*" and not text.startswith("|", i + 1):
            i += 1
        elif char in ".#":
            name = _NAME.match(text, i + 1)
            if not name:
                return None
            if char == ".":
                own_classes.append(unescape(name.group()))
                classes += 1
            else:
                own_ids.append(unescape(name.group()))
                ids += 1
            i = name.end()
        elif char == "[":
            end = _closing(text, i)
            if end < 0:
                return None
            conditions, classes, i = True, classes + 1, end
        elif char == ":":
            element = text.startswith("::", i)
            name = _NAME.match(text, i + (2 if element else 1))
            if not name:
                return None
            word = unescape(name.group()).lower()
            i = name.end()
            if i < len(text) and text[i] == "(":
                i = _closing(text, i)
                if i < 0:
                    return None
            if element or word in _OLD_PSEUDO_ELEMENTS:
                return None  # 规则落在 ::before 这类伪元素上，不是元素本身
            conditions = True
            classes += 0 if word == "where" else 1
        else:
            name = _NAME.match(text, i)
            if not name or tag or own_classes or own_ids or conditions:
                return None  # &、|、标签名写在了中间……看不懂的整个不算
            tag = unescape(name.group()).lower()
            types += 1
            i = name.end()
    if fresh:
        return None  # 末尾是组合符
    return Selector(tag, tuple(own_classes), tuple(own_ids), parts == 1 and not conditions, (ids, classes, types))


def _closing(text: str, start: int) -> int:
    """text[start] 是 ( 或 [：它对应的那个右括号后面的位置。没关上交回 -1。"""
    depth = 0
    for found in _SIGNIFICANT.finditer(text, start):
        token = found.group()
        if token in _OPENING:
            depth += 1
        elif token in _CLOSING:
            depth -= 1
            if depth == 0:
                return found.end()
    return -1
