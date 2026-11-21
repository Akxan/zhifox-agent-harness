"""把模型写的 Markdown 变成聊天软件里能发的样子：拆成不超长的几段，排成 Telegram 认得的 HTML。

为什么不直接发 Markdown：Telegram 的 MarkdownV2 要求把 . ! - ( ) 这些常见的标点全都转义，模型的回复里
到处都是它们，漏一个整条消息就发不出去。它的 HTML 模式只需要转义 & < > 三个字符，规则简单、不会出错，
所以这里只认几样最常用的写法，变成对应的标签，别的原样当文字：

    ```代码块```       → <pre><code>     `行内代码` → <code>
    **加粗**           → <b>             *斜体*      → <i>
    [文字](https://…)  → <a href>        # 标题      → <b>标题</b>
    - 列表             → • 列表

没配对上的记号（比如只有半个 **）原样留着，不会变成没闭合的标签。Telegram 万一还是不认，
telegram.py 会退回纯文字再发一次。

为什么按 UTF-16 数长度：Telegram 说"一条消息最多 4096 个字符"，数的是 UTF-16 的单位，
一个表情符号算两个。按 Python 的 len 数，满是表情的长回复会超出上限被拒。
拆的时候尽量在空行、换行、空格处断开；代码块被拆开时，前一段末尾补上 ```、后一段开头再开一个，两段各自完整。
"""

from __future__ import annotations

import html
import re

FENCE = re.compile(r"^\s*```")
CLOSE = "\n```"


def length(text: str) -> int:
    """按 UTF-16 数有多长。"""
    return len(text.encode("utf-16-le")) // 2


def escape(text: str) -> str:
    return html.escape(text, quote=False)


# ---- 拆开 --------------------------------------------------------------------


def _cut(text: str, limit: int) -> tuple[str, str]:
    """从 text 的开头切下一段（UTF-16 长度不超过 limit），尽量切在空白处。返回（切下的，剩下的）。"""
    if length(text) <= limit:
        return text, ""
    # 先按 UTF-16 找到最多能放到第几个字符（表情占两个单位，不能把它切成两半）
    end, used = 0, 0
    for index, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > limit:
            end = index
            break
    head = text[:end]
    for mark in ("\n\n", "\n", " "):
        at = head.rfind(mark)
        if at > end // 2:  # 断点太靠前的话切出来的一段太短，不如直接在上限处切
            return text[: at + len(mark)], text[at + len(mark) :]
    return head, text[end:]


def split(text: str, limit: int) -> list[str]:
    """拆成每段不超过 limit（UTF-16 单位）的几段。代码块被拆开的地方，两边各自补上围栏。空文字返回空列表。"""
    text = text.strip("\n")
    if not text.strip():
        return []
    pieces: list[str] = []
    buffer = ""  # 正在攒的这一段
    fence = ""  # 攒到这里时在哪个代码块里（它开头的那一行，比如 "```bash"）；不在代码块里是空字符串

    def emit(reopen: bool = True) -> None:
        nonlocal buffer
        body = buffer.rstrip("\n")
        piece = body + CLOSE if fence else body
        if _shown(piece):  # 没有可见的文字（比如只剩一对空的代码块围栏）：不算一段，Telegram 不收空消息
            pieces.append(piece)
        buffer = fence + "\n" if fence and reopen else ""

    for line in text.splitlines(keepends=True):
        toggles = FENCE.match(line) is not None
        after = ("" if fence else line.strip()) if toggles else fence  # 加上这一行以后在不在代码块里
        reserve = len(CLOSE) if after else 0  # 在代码块里的话，给末尾补的 ``` 留位置
        if length(buffer) + length(line) + reserve > limit:
            if buffer.rstrip("\n") != fence:
                emit()
            while length(buffer) + length(line) + reserve > limit:  # 一行就放不下：硬切
                head, line = _cut(line, max(1, limit - length(buffer) - reserve))
                buffer += head
                emit()
        buffer += line
        fence = after
    emit(reopen=False)
    return pieces


def _shown(piece: str) -> bool:
    """这一段排版以后还有没有能看见的文字（去掉代码块的围栏行以后不是空的）。"""
    return any(line.strip() and not FENCE.match(line) for line in piece.split("\n"))


# ---- 排成 HTML ----------------------------------------------------------------

_INLINE = re.compile(
    r"`([^`\n]+)`"  # 行内代码
    r"|\*\*(?=\S)(.+?)(?<=\S)\*\*"  # 加粗
    r"|\[([^\]\n]+)\]\((https?://[^\s)]+)\)"  # 链接（只认 http 和 https）
    r"|(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])"  # 斜体（只认 *…*：_ 在文件名、变量名里太常见）
)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")


def _inline(raw: str) -> str:
    out, last = [], 0
    for match in _INLINE.finditer(raw):
        out.append(escape(raw[last : match.start()]))
        code, bold, label, url, italic = match.groups()
        if code is not None:
            out.append(f"<code>{escape(code)}</code>")
        elif bold is not None:
            out.append(f"<b>{_inline(bold)}</b>")
        elif label is not None:
            out.append(f'<a href="{html.escape(url, quote=True)}">{escape(label)}</a>')
        else:
            out.append(f"<i>{escape(italic)}</i>")
        last = match.end()
    out.append(escape(raw[last:]))
    return "".join(out)


def to_html(text: str) -> str:
    """Markdown → Telegram 的 HTML。结果里的每一个标签都是成对的。"""
    out: list[str] = []
    code: list[str] | None = None  # 正在收集的代码块；不在代码块里是 None
    language = ""
    for line in text.split("\n"):
        if FENCE.match(line):
            if code is None:
                code, language = [], re.sub(r"[^\w+#.-]", "", line.strip()[3:])[:20]
            else:
                attribute = f' class="language-{language}"' if language else ""
                out.append(f"<pre><code{attribute}>{escape(chr(10).join(code))}</code></pre>")
                code = None
            continue
        if code is not None:
            code.append(line)
            continue
        heading = _HEADING.match(line)
        bullet = _BULLET.match(line)
        if heading:
            out.append(f"<b>{_inline(heading.group(1))}</b>")
        elif bullet and not re.fullmatch(r"\s*[-*_]{3,}\s*", line):
            out.append(f"{bullet.group(1)}• {_inline(bullet.group(2))}")
        else:
            out.append(_inline(line))
    if code is not None:  # 代码块没有收尾：照样当代码块
        out.append(f"<pre>{escape(chr(10).join(code))}</pre>")
    return "\n".join(out)
