"""联网搜索的回答引用了哪些网页：从 API 的引用里取出来、编上号、写成给人看的几行字。

Claude 用 web_search 搜过以后，回答里出自搜索结果的那几句话，API 会单独切成一个 text 块，块上带一个 citations 列表。
每一条引用（类型是 web_search_result_location）有网址 url、网页标题 title、被引用的那段原文 cited_text（最多 150 个字），
还有一个要原样发回给 API 的 encrypted_index。流式输出时，引用是 content_block_delta 里的 citations_delta，
SDK 把它们攒在那个块上，块结束（content_block_stop）时一起拿到。见
https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool 和
https://platform.claude.com/docs/en/build-with-claude/citations 。文档还要求：直接把回答给用户看时，要带上原始来源。

这个文件只管"给人看"的那一份：
  from_citations  一个文字块的引用 → 几条来源（只留 http、https 的网址；标题、引文洗过、截短）
  Numbering       一段回答里给来源编号：同一个网址只编一个号，按第一次出现的先后从 1 数起
  site            网址 → 显示用的网站名（en.wikipedia.org）
  marker、insert  [1][2] 这样的文字记号，插在那段文字后面（终端、聊天通道、子 Agent 的回答用）
  listing         一段回答的来源写成几行字（终端、聊天通道用）："资料来源：\n1. 标题 — 网站"
  for_model       同样的几行，写给模型看（子 Agent 交回的回答里）：中文、带完整的网址

对话历史（messages）里的引用一个字都不动：API 要求原样发回去（见 providers/claude.py 的 _plain），这里只是另抄一份给界面。
"一段回答"从哪儿算起、到哪儿结束，由各个界面自己定：网页见 webui/ui.py 的 PARAGRAPH，终端见 ui.py，聊天见 channels/ui.py。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from .config import SOURCE_QUOTE_CHARS, SOURCE_TITLE_CHARS, SOURCE_URL_CHARS
from .i18n import t
from .safe import clean_text


def _brief(text: Any, limit: int) -> str:
    """一段给人看的短文字：洗掉落单的代理字符，连续的空白压成一个空格，太长的截短加"…"。不是文字的当成空的。"""
    if not isinstance(text, str):
        return ""
    text = " ".join(clean_text(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _web_url(url: Any) -> str:
    """能放进页面链接里的网址：只认 http、https，要有主机名，不能太长。别的返回空字符串。"""
    if not isinstance(url, str):
        return ""
    url = clean_text(url).strip()
    if not url or len(url) > SOURCE_URL_CHARS or any(char <= " " for char in url):
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    return url if parts.scheme in ("http", "https") and parts.hostname else ""


def site(url: str) -> str:
    """网址 → 显示用的网站名：主机名，去掉开头的 www.。"""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return host.removeprefix("www.")


def from_citations(citations: Any) -> list[dict]:
    """一个文字块上的引用（SDK 的对象，或者同样字段的字典）→ 来源 [{"url", "title", "quote"}]。

    只要带网址的那种（联网搜索的 web_search_result_location）；文档、PDF 里的引用没有网址，这里不管。
    同一个块里同一个网址引了两次的，只留第一次。
    """
    sources: list[dict] = []
    seen: set[str] = set()
    for citation in citations or []:
        field = citation.get if isinstance(citation, dict) else lambda name, item=citation: getattr(item, name, None)
        url = _web_url(field("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        sources.append({"url": url, "title": _brief(field("title"), SOURCE_TITLE_CHARS), "quote": _brief(field("cited_text"), SOURCE_QUOTE_CHARS)})
    return sources


class Numbering:
    """一段回答里的来源编号。同一个网址只编一个号，按第一次出现的先后从 1 数起。

    一段回答结束时 clear()，下一段又从 1 数。
    """

    def __init__(self) -> None:
        self._numbers: dict[str, int] = {}
        self.sources: list[dict] = []  # 这一段到现在为止的来源，按编号排：{"n", "url", "title", "site"}

    def clear(self) -> None:
        self._numbers = {}
        self.sources = []

    def add(self, cited: list[dict]) -> tuple[list[dict], list[dict]]:
        """一小段文字引用了这些来源（from_citations 的结果）。返回（marks，new）：

        marks  这一处要标的记号 [{"n", "quote"}]：一个来源一个，没有引文的不带 quote
        new    这一段里第一次出现的来源 [{"n", "url", "title", "site"}]：页面只需要收到一次
        """
        marks: list[dict] = []
        new: list[dict] = []
        for source in cited:
            url = source["url"]
            number = self._numbers.get(url)
            if number is None:
                number = self._numbers[url] = len(self._numbers) + 1
                where = site(url)
                entry = {"n": number, "url": url, "title": source.get("title") or where, "site": where}
                self.sources.append(entry)
                new.append(entry)
            if all(mark["n"] != number for mark in marks):
                marks.append({"n": number, "quote": source["quote"]} if source.get("quote") else {"n": number})
        return marks, new


def marker(marks: list[dict]) -> str:
    """文字里的记号：[1][3]。"""
    return "".join(f"[{mark['n']}]" for mark in marks)


def insert(text: str, mark: str) -> str:
    """把记号接在文字的最后一个字后面：末尾的空白和换行留在记号后面，不然记号会跑到下一行的开头。"""
    body = text.rstrip()
    return body + mark + text[len(body) :]


def _link(source: dict) -> str:
    """Markdown 的链接 [标题](网址)。标题里的方括号会提前结束链接的文字、网址里的圆括号会提前结束网址
    （维基百科的网址常有括号）：分别换成圆括号和百分号的写法，意思不变。"""
    title = source["title"].replace("[", "(").replace("]", ")")
    url = source["url"].replace("(", "%28").replace(")", "%29")
    return f"[{title}]({url})"


def listing(sources: list[dict], markdown: bool = False) -> str:
    """一段回答的来源写成几行字："资料来源：\n1. 标题 — 网站"。markdown：标题写成链接（聊天通道会把它排成能点的样子）。"""
    lines = [t("资料来源：")]
    for source in sources:
        title = _link(source) if markdown else source["title"]
        lines.append(f"{source['n']}. {title} — {source['site']}" if source["site"] and source["site"] != source["title"] else f"{source['n']}. {title}")
    return "\n".join(lines)


def for_model(sources: list[dict]) -> str:
    """同样的来源，写给模型看（子 Agent 交回的回答，见 agents/runner.py）：中文，带完整的网址，主助手转述时能照着写出处。"""
    return "\n".join(["来源："] + [f"[{source['n']}] {source['title']} — {source['url']}" for source in sources])
