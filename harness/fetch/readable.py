"""HTML → 能读的文字：用标准库的 html.parser 一遍走完，不执行脚本、不加载任何东西。

留下的：
  标题（<title>，没有的话用第一个 h1）、简介（<meta name="description">），放在最前面
  h1~h6 写成 # 开头的一行（几级几个 #）；段落、div 这类各占一行；列表项前面加 "- "（有序列表写 1. 2. …）；
  表格一行一行写，格子之间用 " | " 隔开；<pre> 里的原样保留缩进和换行
  链接写成"文字 (网址)"，但最多 FETCH_MAX_LINKS 个（只认 http、https；页内的 #锚点不写），图片只写它的 alt。
  名额在整页读完以后才分：读的时候每个网址先记成一个记号，下面那一串导航收起来以后，从前往后换成"(网址)"，用完了的只留文字
去掉的：
  脚本、样式、<noscript>、<template>、SVG、内嵌的框（iframe）、导航（nav）、侧栏（aside）、页脚（footer）、按钮、下拉框、对话框，
  以及藏起来的（hidden、aria-hidden="true"、style 里写着 display:none 或 visibility:hidden）：
  藏起来的文字你看不到，却常常是冲着模型写的指示（提示词注入），不交给它。
正文在哪儿：网页里有 <main>（或者 role="main"）、<article> 的，而且里面的字不算太少（MAIN_MIN_CHARS），只留它们里面的；
没有的话留下整页（去掉上面那些）。
连着 FETCH_LINK_RUN 个以上、每一项都只是一个链接的列表项（导航、语言列表），收成一句"略过了一串 N 个链接"：
维基百科新版的皮肤把语言下拉菜单放在 <main> 里、只靠 CSS 藏着，HTML 里看不出它藏起来了（2026-11-23 用真的网络读出来的）。
正文里的列表多半每一项带着说明，短的几个链接也留着。

写坏了的 HTML（没关上的 <p>、<li>）很常见：要跳过的那一段用一个栈记着里面开过哪些标签，碰到外层的结束标签就把里面没关的一起关上。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from ..browser import urls
from ..config import FETCH_LINK_RUN, FETCH_MAX_LINKS

# 整段跳过的标签（里面的东西都不要）
SKIP = {
    "script", "style", "noscript", "template", "svg", "math", "iframe", "object", "canvas", "audio", "video",
    "nav", "aside", "footer", "button", "select", "datalist", "dialog", "map",
}  # fmt: skip
# 没有结束标签的
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
# 自己占一行（或者几行）的
BLOCK = {
    "p", "div", "section", "article", "main", "header", "blockquote", "pre", "ul", "ol", "li", "dl", "dt", "dd",
    "table", "tr", "thead", "tbody", "tfoot", "caption", "figure", "figcaption", "address", "details", "summary",
    "form", "fieldset", "legend", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "br", "body", "html",
}  # fmt: skip
HEADINGS = {f"h{level}": level for level in range(1, 7)}
MAIN_MIN_CHARS = 200  # <main>、<article> 里至少有这么多字才只留它们（太少的多半是一个小卡片，正文在别处）
_SPACES = re.compile(r"\s+")
# 链接的网址在读的时候先记成 记号开始 + 网址 + 记号结束（私用区的两个字符），整页读完才按名额换成" (网址)"或者去掉。
# 网页里自己写的这两个字符先去掉（_unmarked：正文在 handle_data 里，图片的 alt 在 handle_starttag 里），冒充不了一个链接
_LINK_OPEN, _LINK_CLOSE = "\ue010", "\ue011"
_LINK_MARK = re.compile(f"{_LINK_OPEN}([^{_LINK_CLOSE}]*){_LINK_CLOSE}")
_ITEM_PREFIX = re.compile(r"\s*(?:-|\d+\.) $")
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)


@dataclass(frozen=True)
class Page:
    title: str
    description: str
    text: str


def read(html: str, url: str) -> Page:
    """一段 HTML（已经解成文字的）→ 标题、简介、正文。url 是网页的地址（相对的链接按它补全）。"""
    reader = _Reader(url)
    reader.feed(html)
    reader.close()
    return reader.page()


class _Reader(HTMLParser):
    def __init__(self, url: str):
        super().__init__(convert_charrefs=True)
        self.base = url
        self.title: list[str] = []
        self.in_title = False
        self.description = ""
        self.skipping: list[str] = []  # 正在跳过的那一段里开着的标签（空的就是没在跳过）
        self.open: list[str] = []  # 开着的标签（不算跳过的那一段）：结束标签对上的是哪一个，没关上的一起关上
        self.mains: list[int] = []  # 开着的 main、article（以及 role="main" 的那个标签）在 open 里的位置
        self.lines: list[tuple[str, bool, bool]] = []  # （一行字，是不是在正文里，是不是只是一个链接的列表项）
        self.line_chars = 0  # 这一行里的字有多少（不算空白），其中多少在链接里：看它是不是"只是一个链接"
        self.line_link_chars = 0
        self.buffer: list[str] = []  # 这一行还没写完的几段
        self.prefix = ""  # 这一行前面要加的："## "、"- "、"1. "
        self.pre = 0  # 在几层 <pre> 里面
        self.lists: list[list] = []  # 开着的列表：["ul" 或 "ol"，已经数到第几项]
        self.link: tuple[str, int] | None = None  # 开着的链接：（网址，它的文字从 buffer 的第几段开始）
        self.first_heading = ""

    # ---- 标签 ----------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): value or "" for name, value in attrs}
        if self.skipping:
            if tag not in VOID:
                self.skipping.append(tag)
            return
        if tag == "title":
            self.in_title = True
            return
        if tag == "meta":
            self._meta(values)
            return
        if tag == "base" and values.get("href"):
            self.base = urljoin(self.base, values["href"])
            return
        if tag in SKIP or _hidden(values):
            if tag not in VOID:
                self.skipping.append(tag)
            return
        if tag not in VOID:
            if tag in ("main", "article") or values.get("role") == "main":
                self.mains.append(len(self.open))
            self.open.append(tag)
        if tag in BLOCK:
            self._flush()
        if tag in HEADINGS:
            self.prefix = "#" * HEADINGS[tag] + " "
        elif tag in ("ul", "ol"):
            self.lists.append([tag, 0])
        elif tag == "li":
            self._item()
        elif tag == "pre":
            self.pre += 1
        elif tag in ("td", "th"):
            if "".join(self.buffer).strip():
                self.buffer.append(" | ")
        elif tag == "a":
            self._close_link()
            self.link = (self._href(values.get("href", "")), len(self.buffer))
        elif tag == "img":
            alt = _SPACES.sub(" ", _unmarked(values.get("alt", ""))).strip()
            if alt:
                self.buffer.append(f"[图片：{alt[:200]}]")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # <br/>、<img/> 这样写的。不是空元素却写成了 <div/> 的，浏览器当作只开不关：这里当作开了又关上
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.skipping:
            if tag in self.skipping:
                while self.skipping and self.skipping.pop() != tag:
                    pass  # 里面没关上的标签一起关上
                return
            if tag not in self.open:
                return
            # 跳过的那一段没关上，它外面的标签先关了（<div><aside>…</div>）：和浏览器一样，跳过的那一段也到此为止
            self.skipping = []
        if tag == "title":
            self.in_title = False
            return
        if tag == "a":
            self._close_link()
        elif tag in ("ul", "ol") and self.lists:
            self.lists.pop()
        elif tag == "pre" and self.pre:
            self._flush()
            self.pre -= 1
        if tag in BLOCK:
            self._flush()
        if tag in HEADINGS or tag == "li":
            self.prefix = ""  # 空的标题、空的列表项：前缀不能留给下一行
        if tag in self.open:
            while self.open.pop() != tag:
                pass  # 里面没关上的标签一起关上
            while self.mains and self.mains[-1] >= len(self.open):
                self.mains.pop()

    def handle_data(self, data: str) -> None:
        if self.skipping:
            return
        if self.in_title:
            self.title.append(data)
            return
        data = _unmarked(data)  # 冒充不了链接的记号（见 _LINK_MARK）
        size = len("".join(data.split()))
        self.line_chars += size
        if self.link is not None:
            self.line_link_chars += size
        self.buffer.append(data if self.pre else _SPACES.sub(" ", data))

    # ---- 帮手 ----------------------------------------------------------------

    def _meta(self, values: dict[str, str]) -> None:
        name = (values.get("name") or values.get("property") or "").lower()
        if name in ("description", "og:description") and not self.description:
            self.description = _SPACES.sub(" ", values.get("content", "")).strip()[:500]

    def _item(self) -> None:
        depth = max(len(self.lists), 1)
        if self.lists and self.lists[-1][0] == "ol":
            self.lists[-1][1] += 1
            mark = f"{self.lists[-1][1]}. "
        else:
            mark = "- "
        self.prefix = "  " * (depth - 1) + mark

    def _href(self, href: str) -> str:
        """链接的完整网址。不写的（页内锚点、javascript:、mailto:……）交回空的。"""
        href = href.strip()
        if not href or href.startswith("#"):
            return ""
        try:
            full = urljoin(self.base, href)
            scheme = urlsplit(full).scheme.lower()
        except ValueError:
            return ""
        return full if scheme in ("http", "https") else ""

    def _close_link(self) -> None:
        if self.link is None:
            return
        href, start = self.link
        self.link = None
        text = "".join(self.buffer[start:]).strip()
        if href and text and href.rstrip("/") != text.rstrip("/"):
            self.buffer.append(f"{_LINK_OPEN}{urls.shown(href)}{_LINK_CLOSE}")  # 名额整页读完才分（见 page）

    def _flush(self) -> None:
        self._close_link()  # 一行要结束了，链接还没关上（写坏了的 HTML）：先把它的网址写上，不然会跟到下一行去
        raw = "".join(self.buffer)
        self.buffer = []
        in_main = bool(self.mains)
        # 只是一个链接的列表项："- 语言 (网址)" 这样，几乎所有的字都在链接里（见 page 里收起来的那一步）
        only_link = bool(_ITEM_PREFIX.match(self.prefix)) and self.line_chars > 0 and self.line_link_chars >= 0.9 * self.line_chars
        self.line_chars = self.line_link_chars = 0
        if self.pre:
            for line in raw.strip("\n").split("\n"):
                self.lines.append((line.rstrip(), in_main, False))
        else:
            line = _SPACES.sub(" ", raw).strip()
            if line:
                if self.prefix == "# " and not self.first_heading:
                    self.first_heading = _LINK_MARK.sub("", line)
                if self.prefix.startswith("#") and self.lines and self.lines[-1][0]:
                    self.lines.append(("", in_main, False))  # 标题前面空一行，读起来分得清一节一节的
                self.lines.append((self.prefix + line, in_main, only_link))
                self.prefix = ""

    def page(self) -> Page:
        self._flush()
        main = [(line, link) for line, inside, link in self.lines if inside]
        chosen = main if sum(len(line) for line, _ in main) >= MAIN_MIN_CHARS else [(line, link) for line, _, link in self.lines]
        kept = _fold_link_runs(chosen)
        budget = [FETCH_MAX_LINKS]  # 名额从前往后分：收起来的导航不占

        def shown(found: re.Match) -> str:
            if budget[0] <= 0:
                return ""
            budget[0] -= 1
            return f" ({found.group(1)})"

        text = re.sub(r"\n{3,}", "\n\n", _LINK_MARK.sub(shown, "\n".join(kept))).strip()
        title = _SPACES.sub(" ", "".join(self.title)).strip() or self.first_heading
        return Page(title=title[:300], description=self.description, text=text)


def _fold_link_runs(lines: list[tuple[str, bool]]) -> list[str]:
    """连着 FETCH_LINK_RUN 个以上只是一个链接的列表项，换成一句（见文件开头）。别的行原样。"""
    kept: list[str] = []
    run: list[str] = []

    def end_run() -> None:
        if len(run) >= FETCH_LINK_RUN:
            kept.append(f"（这里略过了一串 {len(run)} 个链接：多半是导航、语言列表这一类）")
        else:
            kept.extend(run)
        run.clear()

    for line, only_link in lines:
        if only_link:
            run.append(line)
            continue
        end_run()
        kept.append(line)
    end_run()
    return kept


def _unmarked(text: str) -> str:
    """去掉网页自己写的链接记号（见 _LINK_MARK）。放进 buffer 的每一段网页上的字都要先经过它。"""
    return text.replace(_LINK_OPEN, "").replace(_LINK_CLOSE, "")


def _hidden(values: dict[str, str]) -> bool:
    """这个元素藏起来了吗：你在网页上看不到的字，不交给模型（见文件开头）。"""
    if "hidden" in values or values.get("aria-hidden", "").strip().lower() == "true":
        return True
    return bool(_HIDDEN_STYLE.search(values.get("style", "")))
