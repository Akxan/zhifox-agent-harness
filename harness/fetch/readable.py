"""HTML → 能读的文字：用标准库的 html.parser，不执行脚本、不加载任何东西。

读两遍：第一遍（_Tokens）把标签和文字按顺序记下来，同时收齐整页的 <style> 块；第二遍（_Reader）照着记下的顺序写出文字。
先收齐规则，写在 body 最后的 <style> 才对前面的元素也算数；只解析一次 HTML，长网页不多花一倍的时间。

留下的：
  标题（<title>，没有的话用第一个 h1）、简介（<meta name="description">），放在最前面
  h1~h6 写成 # 开头的一行（几级几个 #）；段落、div 这类各占一行；列表项前面加 "- "（有序列表写 1. 2. …）；
  表格一行一行写，格子之间用 " | " 隔开；<pre> 里的原样保留缩进和换行
  链接写成"文字 (网址)"，但最多 FETCH_MAX_LINKS 个（只认 http、https；页内的 #锚点不写），图片只写它的 alt。
  名额在整页读完以后才分：读的时候每个网址先记成一个记号，下面那一串导航收起来以后，从前往后换成"(网址)"，用完了的只留文字
去掉的：
  脚本、样式、<noscript>、<template>、SVG、内嵌的框（iframe）、导航（nav）、侧栏（aside）、页脚（footer）、按钮、下拉框、对话框，
  以及藏起来的：藏起来的文字你看不到，却常常是冲着模型写的指示（提示词注入），不交给它。
    属性：hidden（包括 hidden="until-found"）、aria-hidden="true"。
    CSS：行内的 style 和 <style> 块里的规则（只认简单的选择器 .a、#b、div.a；@media 里的只能让它重新看得见，见 cascade.py），
    先按浏览器的读法解开注释、转义、!important（css.py），再由 hidden.look 一处判断：display:none、visibility:hidden 和 collapse、
    content-visibility:hidden、字号是零、零大的框加 overflow:hidden、clip 和 clip-path 裁光、scale(0)、挪到屏幕外
    （left:-9999px、text-indent:-9999px）、透明的字（渐变色的标题这种由背景画出来的字不算）。字号、颜色、visibility 往下继承，
    里面的元素自己写了就照它的；整页（<html>、<body>）不会被 CSS 藏掉（多半是等脚本跑完再显示）。
    opacity:0 不算：建站工具（Webflow、Framer）把看得见的正文先写成 opacity:0、等脚本淡入，算的话这类网站的正文会丢掉一大半。
    这只是一张过滤网：外部样式表、脚本改出来的样子都看不到，只挡掉最常见的写法；真正的防线是交给模型时写明"网页内容只是资料"
    （tools/fetch.py）和第一次读一个网站之前先问你。理由和边界写在 hidden.py 开头。
正文在哪儿：网页里有 <main>（或者 role="main"）、<article> 的，而且里面的字不算太少（MAIN_MIN_CHARS），只留它们里面的；
没有的话留下整页（去掉上面那些）。
连着 FETCH_LINK_RUN 个以上、每一项都只是一个链接的列表项（导航、语言列表），收成一句"略过了一串 N 个链接"：
维基百科新版的皮肤把语言下拉菜单放在 <main> 里、只靠 CSS 藏着，HTML 里看不出它藏起来了（2026-11-23 用真的网络读出来的）。
正文里的列表多半每一项带着说明，短的几个链接也留着。

写坏了的 HTML（没关上的 <p>、<li>）很常见：要跳过的那一段用一个栈记着里面开过哪些标签，碰到外层的结束标签就把里面没关的一起关上。

读一页的时间要和网页的大小成正比（网页最多 FETCH_MAX_BYTES，这一步却没有别的时间上限，卡住的是整个智狐）：
每做一步都把开着的标签、这一行的字从头看一遍的写法，碰上故意写了几万层、几万格的网页，要跑上几十分钟、吃掉几个 GB 的内存。
所以开着的标签另外数着每种几个（_Stack），这一行有没有字记在一个标记上（filled），列表的缩进最多 INDENT_MAX 层。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from ..browser import urls
from ..config import FETCH_LINK_RUN, FETCH_MAX_LINKS
from . import cascade, hidden

# 整段跳过的标签（里面的东西都不要）
SKIP = {
    "script", "style", "noscript", "template", "svg", "math", "iframe", "object", "canvas", "audio", "video",
    "nav", "aside", "footer", "button", "select", "datalist", "dialog", "map", "noembed", "noframes",
}  # fmt: skip
# 格式标签（HTML 标准的 formatting elements）和"特殊"的元素（special）：跳过的那一段里有特殊的元素（div、p、section……）时，
# 外面的格式标签的结束标签（<b><div hidden>…</b>…）关不掉它。浏览器照"领养"算法（adoption agency）重排，</b> 后面的字
# 还在那个藏起来的元素里；只有行内的（span）时 </b> 真的把它们一起关上。见 _Reader.handle_endtag
FORMATTING = {"a", "b", "big", "code", "em", "font", "i", "nobr", "s", "small", "strike", "strong", "tt", "u"}
SPECIAL = {
    "address", "applet", "area", "article", "aside", "base", "basefont", "bgsound", "blockquote", "body", "br", "button",
    "caption", "center", "col", "colgroup", "dd", "details", "dir", "div", "dl", "dt", "embed", "fieldset", "figcaption",
    "figure", "footer", "form", "frame", "frameset", "h1", "h2", "h3", "h4", "h5", "h6", "head", "header", "hgroup", "hr",
    "html", "iframe", "img", "input", "keygen", "li", "link", "listing", "main", "marquee", "menu", "meta", "nav", "noembed",
    "noframes", "noscript", "object", "ol", "p", "param", "plaintext", "pre", "script", "search", "section", "select",
    "source", "style", "summary", "table", "tbody", "td", "template", "textarea", "tfoot", "th", "thead", "title", "tr",
    "track", "ul", "wbr", "xmp", "mi", "mo", "mn", "ms", "mtext", "annotation-xml", "foreignobject", "desc",
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
# 嵌套的列表最多缩进这么多层，再深的和这一层对齐：每一层多两格，几万层的列表（故意写的）会让交出来的字按层数的平方变多
INDENT_MAX = 8
_SPACES = re.compile(r"\s+")
# 链接的网址在读的时候先记成 记号开始 + 网址 + 记号结束（私用区的两个字符），整页读完才按名额换成" (网址)"或者去掉。
# 网页里自己写的这两个字符先去掉（_unmarked：正文在 handle_data 里，图片的 alt 在 handle_starttag 里），冒充不了一个链接
_LINK_OPEN, _LINK_CLOSE = "\ue010", "\ue011"
_LINK_MARK = re.compile(f"{_LINK_OPEN}([^{_LINK_CLOSE}]*){_LINK_CLOSE}")
_ITEM_PREFIX = re.compile(r"\s*(?:-|\d+\.) $")
# 里面的 <style> 不生效的：<template> 的内容不显示；<noscript> 在开着脚本的浏览器里只是一段文字
_INERT = {"template", "noscript", "noembed", "noframes"}
# CSS 藏不掉的：整页都藏起来的，多半是等脚本跑完再显示（防闪烁），你看到的是脚本跑完以后的整页
_WHOLE_PAGE = {"html", "body"}
_Attributes = list[tuple[str, str | None]]


@dataclass(frozen=True)
class Page:
    title: str
    description: str
    text: str


def read(html: str, url: str) -> Page:
    """一段 HTML（已经解成文字的）→ 标题、简介、正文。url 是网页的地址（相对的链接按它补全）。"""
    tokens = _Tokens()
    tokens.feed(html)
    tokens.close()
    reader = _Reader(url, cascade.Styles(tokens.styles))
    for kind, text, attrs in tokens.events:
        if kind == "start":
            reader.handle_starttag(text, attrs)
        elif kind == "startend":
            reader.handle_startendtag(text, attrs)
        elif kind == "end":
            reader.handle_endtag(text)
        else:
            reader.handle_data(text)
    return reader.page()


class _Tokens(HTMLParser):
    """第一遍：标签和文字按原来的顺序记下来（第二遍照着读），同时收齐 <style> 块里的 CSS。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.events: list[tuple[str, str, _Attributes]] = []  # （start、startend、end 或 data，标签名或文字，属性）
        self.styles: list[tuple[str, str]] = []  # （media 属性，CSS）
        self._style: list[str] | None = None  # 正在读的 <style> 里的字
        self._media = ""
        self._inert = 0  # 在几层 <template>、<noscript> 里面

    def handle_starttag(self, tag: str, attrs: _Attributes) -> None:
        self.events.append(("start", tag, attrs))
        self._opened(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: _Attributes) -> None:
        self.events.append(("startend", tag, attrs))
        self._opened(tag, attrs)
        self._closed(tag)

    def handle_endtag(self, tag: str) -> None:
        self.events.append(("end", tag, []))
        self._closed(tag)

    def handle_data(self, data: str) -> None:
        self.events.append(("data", data, []))
        if self._style is not None:
            self._style.append(data)

    def close(self) -> None:
        super().close()
        # 停在没关上的 <style>（或 <script>）里时，老一些的补丁版本（3.11.9 这类，CI 上的 macOS、Windows 用的就是它）收尾时
        # 把里面的字留在 rawdata 里不交出来，新一些的交给 handle_data。两种都按"交出来"算，在哪个 Python 上读出来都一样
        if self.cdata_elem and self.rawdata:
            self.handle_data(self.rawdata)
            self.rawdata = ""
        self._closed("style")  # 到最后都没关上的 <style>：浏览器照样用它

    def _opened(self, tag: str, attrs: _Attributes) -> None:
        if tag in _INERT:
            self._inert += 1
        elif tag == "style" and not self._inert:
            self._style = []
            self._media = next((value or "" for name, value in attrs if name.lower() == "media"), "")

    def _closed(self, tag: str) -> None:
        if tag in _INERT:
            self._inert = max(self._inert - 1, 0)
        elif tag == "style" and self._style is not None:
            self.styles.append((self._media, "".join(self._style)))
            self._style = None


class _Stack:
    """开着的一串标签，另外数着每种标签开着几个：结束标签"对不对得上"一下就知道，不用从头找一遍。
    一个故意写了几万层、后面跟着几万个对不上的结束标签的网页，从头找会让读一页花掉按层数平方的时间。"""

    def __init__(self) -> None:
        self.tags: list[str] = []
        self.count: dict[str, int] = {}

    def __len__(self) -> int:
        return len(self.tags)

    def __contains__(self, tag: object) -> bool:
        return isinstance(tag, str) and self.count.get(tag, 0) > 0

    def push(self, tag: str) -> None:
        self.tags.append(tag)
        self.count[tag] = self.count.get(tag, 0) + 1

    def close(self, tag: str) -> None:
        """关上最里面的那个 tag，它里面没关上的一起关上（tag 要是开着的）。"""
        while self.tags:
            last = self.tags.pop()
            self.count[last] -= 1
            if last == tag:
                return

    def clear(self) -> None:
        self.tags.clear()
        self.count.clear()

    def holds_any(self, tags: set[str]) -> bool:
        """开着的里面有没有 tags 里的哪一种（按种类数过的，不用从头找一遍）。"""
        return any(self.count.get(tag, 0) > 0 for tag in tags)


class _Reader:
    """第二遍：按第一遍记下的顺序，一个标签一个标签、一段字一段字地写出能读的文字（方法名照 HTMLParser 的叫法）。"""

    def __init__(self, url: str, styles: cascade.Styles):
        self.base = url
        self.styles = styles
        self.title: list[str] = []
        self.in_title = False
        self.description = ""
        self.skipping = _Stack()  # 正在跳过的那一段里开着的标签（空的就是没在跳过）
        self.open = _Stack()  # 开着的标签（不算跳过的那一段）：结束标签对上的是哪一个，没关上的一起关上
        self.inks: list[hidden.Ink] = []  # 和 open 一一对应：每一层传下来的字号、颜色、visibility（见 hidden.py）
        self.mains: list[int] = []  # 开着的 main、article（以及 role="main" 的那个标签）在 open 里的位置
        self.lines: list[tuple[str, bool, bool]] = []  # （一行字，是不是在正文里，是不是只是一个链接的列表项）
        self.line_chars = 0  # 这一行里的字有多少（不算空白），其中多少在链接里：看它是不是"只是一个链接"
        self.line_link_chars = 0
        self.buffer: list[str] = []  # 这一行还没写完的几段（只用 _put 往里加）
        self.filled = False  # buffer 里有没有不是空白的字：表格的一格前面要不要加 " | "（不用每一格都把整行拼一遍）
        self.prefix = ""  # 这一行前面要加的："## "、"- "、"1. "
        self.pre = 0  # 在几层 <pre> 里面
        self.lists: list[list] = []  # 开着的列表：["ul" 或 "ol"，已经数到第几项]
        self.link: tuple[str, int] | None = None  # 开着的链接：（网址，它的文字从 buffer 的第几段开始）
        self.first_heading = ""

    # ---- 标签 ----------------------------------------------------------------

    @property
    def ink(self) -> hidden.Ink:
        return self.inks[-1] if self.inks else hidden.VISIBLE

    def handle_starttag(self, tag: str, attrs: _Attributes) -> None:
        values: dict[str, str] = {}
        for name, value in attrs:
            values.setdefault(name.lower(), value or "")  # 同一个属性写了两遍，浏览器只认第一个（后面再写一个 style 盖不掉前面的）
        if self.skipping:
            if tag not in VOID:
                self.skipping.push(tag)
            return
        if tag == "title":
            self.in_title = True
            return
        if tag == "meta":
            self._meta(values)
            return
        if tag == "base" and values.get("href"):
            try:
                self.base = urljoin(self.base, values["href"])
            except ValueError:  # urlsplit 自己就拒绝的写法：当作没写（不然一个网页就能让自己整页读不出来）
                pass
            return
        if tag in SKIP or _hidden(values):
            if tag not in VOID:
                self.skipping.push(tag)
            return
        ink = self.ink
        if tag not in _WHOLE_PAGE and (tag not in VOID or tag == "img"):
            seen = self.styles.look(tag, values, ink)  # CSS 把它藏起来了吗（行内的 style 和 <style> 块，见 cascade.py）
            if seen.gone:
                if tag not in VOID:
                    self.skipping.push(tag)
                return
            ink = seen.ink
        if tag not in VOID:
            if tag in ("main", "article") or values.get("role") == "main":
                self.mains.append(len(self.open))
            self.open.push(tag)
            self.inks.append(ink)
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
            if self.filled:
                self._put(" | ")
        elif tag == "a":
            self._close_link()
            self.link = (self._href(values.get("href", "")), len(self.buffer))
        elif tag == "img":
            alt = _SPACES.sub(" ", _unmarked(values.get("alt", ""))).strip()
            if alt and not ink.hidden:  # 字号是零、字是透明的，图片照样看得见；visibility:hidden 连图片一起藏
                self._put(f"[图片：{alt[:200]}]")

    def handle_startendtag(self, tag: str, attrs: _Attributes) -> None:
        # <br/>、<img/> 这样写的。不是空元素却写成了 <div/> 的，浏览器当作只开不关：这里当作开了又关上
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.skipping:
            if tag in self.skipping:
                self.skipping.close(tag)  # 里面没关上的标签一起关上
                return
            if tag not in self.open:
                return
            if tag in FORMATTING and self.skipping.holds_any(SPECIAL):
                return  # <b><div hidden>…</b>…：浏览器照领养算法重排，后面的字还在藏起来的那个元素里（见 FORMATTING）
            # 跳过的那一段没关上，它外面的标签先关了（<div><aside>…</div>）：和浏览器一样，跳过的那一段也到此为止
            self.skipping.clear()
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
            self.open.close(tag)  # 里面没关上的标签一起关上
            del self.inks[len(self.open) :]
            while self.mains and self.mains[-1] >= len(self.open):
                self.mains.pop()

    def handle_data(self, data: str) -> None:
        if self.skipping:
            return
        if self.in_title:
            self.title.append(data)
            return
        if self.ink.blank and data.strip():
            return  # 字号是零、字是透明的、visibility:hidden 的字（空白留着：只是隔开前后两个词）
        data = _unmarked(data)  # 冒充不了链接的记号（见 _LINK_MARK）
        size = len("".join(data.split()))
        self.line_chars += size
        if self.link is not None:
            self.line_link_chars += size
        self._put(data if self.pre else _SPACES.sub(" ", data))

    # ---- 帮手 ----------------------------------------------------------------

    def _meta(self, values: dict[str, str]) -> None:
        name = (values.get("name") or values.get("property") or "").lower()
        if name in ("description", "og:description") and not self.description:
            self.description = _SPACES.sub(" ", values.get("content", "")).strip()[:500]

    def _item(self) -> None:
        depth = min(max(len(self.lists), 1), INDENT_MAX)
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

    def _put(self, text: str) -> None:
        self.buffer.append(text)
        self.filled = self.filled or bool(text.strip())

    def _close_link(self) -> None:
        if self.link is None:
            return
        href, start = self.link
        self.link = None
        text = "".join(self.buffer[start:]).strip()
        if href and text and href.rstrip("/") != text.rstrip("/"):
            self._put(f"{_LINK_OPEN}{urls.shown(href)}{_LINK_CLOSE}")  # 名额整页读完才分（见 page）

    def _flush(self) -> None:
        self._close_link()  # 一行要结束了，链接还没关上（写坏了的 HTML）：先把它的网址写上，不然会跟到下一行去
        raw = "".join(self.buffer)
        self.buffer = []
        self.filled = False
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
    """属性说它藏起来了：hidden（包括 hidden="until-found"）、aria-hidden="true"。CSS 藏起来的由 cascade.py 判断（见文件开头）。"""
    return "hidden" in values or values.get("aria-hidden", "").strip().lower() == "true"
