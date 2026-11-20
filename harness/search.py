"""会话搜索：在所有保存过的会话里找东西。标题和内容都找，不要求一字不差。

要解决两个问题。

找得快。不能每敲一个字就把所有会话文件重新读一遍。所以把每段会话里值得搜的文字（标题、你说的话、
助手的回复、工具调用……）抽出来放在内存里，这就是"索引"（SearchIndex）。会话保存、改名、删除时
索引跟着更新，搜索只在内存里进行。

找得到。人记得的往往只是个大概，所以匹配分三档，一档比一档宽松，越严格的结果排得越靠前：

  1. 原样包含   关键词原样出现。不分大小写，全角半角算一样。
  2. 相近的词   5 个字母以上的英文单词允许敲错一两个字母，比如 "hraness" 能找到 "harness"。
                只在这个词在所有会话里都原样找不到时才启用（那多半是敲错了）。
  3. 隔字包含   关键词的每个字按顺序都出现，中间可以隔几个字。
                比如"交报"能找到"交了报告"，"hrns"能找到"harness"。
                这一档最容易对上不相干的东西，所以管得严：英文关键词至少 4 个字母，
                不能跨过空格（也就是只在一个单词里面找），夹进来的字不能超过关键词长度的一半；
                中文关键词至少 2 个字，夹进来的字不能比关键词本身还多。

多个关键词用空格隔开，要全部满足，先后顺序不限，也不要求出现在同一句话里。
排序：每个关键词都原样找到的会话在最前面，然后是用到了"相近的词"的，最后是用到了"隔字包含"的；
同一档里，命中在标题里的排在只在内容里的前面，一样相关时新的会话在前。

没有做的：按拼音搜索（需要一张很大的汉字拼音对照表）。
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass
from itertools import islice

MAX_QUERY_CHARS = 200  # 搜索词最长这么多字符
MAX_TERMS = 8  # 最多几个关键词
MAX_RESULTS = 50  # 最多返回几段会话
RESULT_CHARS = 600  # 每条工具结果只收录开头这么多字符：它们往往很长，又很少是用户想找的，全收进来最占内存
GAP = 3  # "隔字包含"时，相邻两个字之间最多隔几个字
LOOSE_MAX = 32  # 比这更长的关键词不做"隔字包含"：那么长的一串，要么原样找得到，要么就是找不到
SNIPPET_BEFORE, SNIPPET_AFTER = 16, 64  # 摘录时，命中位置的前后各带上多少个字符
MAX_MARKS = 24  # 一段摘录里最多标出几处

# 会话里的文字按来源分成几类。同样是命中，出现在标题里比出现在工具输出里更可能是用户要找的，
# 所以每一类有一个分量，排序时用。
WEIGHTS = {"title": 1.0, "user": 0.9, "assistant": 0.8, "tool": 0.6, "thinking": 0.45, "result": 0.4}
# 三档匹配各自的分数。隔字包含还要再乘上"紧凑程度"（关键词的长度 ÷ 命中的那一段的长度）
EXACT, SIMILAR, LOOSE = 1.0, 0.6, 0.5

# 保存下来的界面事件里，哪些类型的哪个字段是可以搜的文字，各算哪一类
_SEARCHABLE = {
    "user": ("user", "text"),
    "text": ("assistant", "text"),
    "thinking": ("thinking", "text"),
    "tool_call": ("tool", "summary"),
    "tool_result": ("result", "text"),
}
# "相近的词"用的词表只收像英文单词的东西：4 到 16 个字母（可以带下划线），前后不挨着别的字母数字。
# 工具输出里常有哈希值、base64 这样的长串，把它们切开收进来只会把词表撑大、让每次比较变慢。
_WORD = re.compile(r"(?<![a-z0-9_])[a-z][a-z_]{3,15}(?![a-z0-9_])")


class _FoldTable(dict):
    """"统一写法"的对照表，给 str.translate 用：全角变半角，大写变小写。用到哪个字符才算哪个。

    一个字符统一写法之后如果不再是一个字符（比如"㎏"会变成"kg"，"…"会变成三个点），就保持原样。
    这样处理过的文字和原文一定一样长、位置一一对应，在它里面找到的位置可以直接拿去原文里摘录和标记。
    """

    def __missing__(self, code: int) -> str:
        char = chr(code)
        folded = unicodedata.normalize("NFKC", char).lower()
        self[code] = folded if len(folded) == 1 else char
        return self[code]


_FOLD = _FoldTable()


@dataclass(frozen=True)
class Passage:
    """一段文字。shown 是给人看的原文，folded 是用来比较的：统一了大小写和全角半角。

    两者一样长、位置一一对应，所以在 folded 里找到的位置可以直接拿去 shown 里摘录。
    """

    kind: str
    shown: str
    folded: str


@dataclass(frozen=True)
class Document:
    """一段会话里所有可以搜的文字。"""

    id: str
    updated_at: float
    title: Passage
    passages: tuple[Passage, ...]
    letters: frozenset[str]  # 出现过的所有字符，用来快速排除不可能命中的会话
    words: frozenset[str]  # 出现过的英文单词，给"相近的词"用

    @property
    def everywhere(self) -> tuple[Passage, ...]:
        return (self.title, *self.passages)


@dataclass(frozen=True)
class Needle:
    """一个关键词，连同找它要用到的东西。"""

    term: str
    similar: tuple[str, ...]  # 和它相近的词（多数时候没有）
    loose: re.Pattern | None  # "隔字包含"用的正则；关键词太短或太长时没有
    slack: int  # "隔字包含"时最多可以夹进来几个别的字


@dataclass(frozen=True)
class Hit:
    """关键词在一段文字里的一处命中。"""

    passage: Passage
    start: int
    end: int
    grade: float  # 是哪一档命中的：EXACT、SIMILAR，或者不超过 LOOSE 的一个数（越紧凑越大）

    @property
    def score(self) -> float:
        return self.grade * WEIGHTS[self.passage.kind]

    @property
    def loose(self) -> bool:
        return self.grade < SIMILAR


def make_passage(kind: str, text: str) -> Passage:
    # 连续的空白（包括换行）压成一个空格，摘录出来才是整齐的一行。
    # 再把"字母 + 单独的重音符号"这种拆开的写法合成一个字符（NFC；macOS 的文件名常是拆开的），
    # 否则正常输入的关键词对不上它。
    shown = unicodedata.normalize("NFC", " ".join(text.split()))
    folded = shown.translate(_FOLD)
    # 没有需要统一的地方时，两份就是同一段文字，不用在内存里存两遍
    return Passage(kind, shown, shown if folded == shown else folded)


def split_query(query: str) -> list[str]:
    """把搜索词拆成关键词：按空格分开，统一大小写和全角半角，去掉重复的。"""
    terms: list[str] = []
    # 搜索词必须和正文用同一种办法统一写法（make_passage），否则正文里原样就有的字反而对不上：
    # 比如"…"单独统一写法会变成三个点，而正文里因为要保持等长，留下的还是"…"。
    for term in make_passage("query", query[:MAX_QUERY_CHARS]).folded.split():
        if term not in terms:
            terms.append(term)
    return terms[:MAX_TERMS]


class SearchIndex:
    def __init__(self) -> None:
        self._documents: dict[str, Document] = {}
        self._version = 0  # 索引每变一次加一
        # 所有会话里出现过的英文单词，连同算它时索引的版本号。版本对不上就是过期了，要重算
        self._vocabulary: tuple[int, list[str]] | None = None

    def put(self, conversation_id: str, title: str, events: list, updated_at: float = 0.0) -> None:
        """收录（或更新）一段会话。events 是保存下来的界面事件。"""
        # 这些内容是从磁盘上的文件里读回来的。文件万一被改坏了，这里什么都可能出现，
        # 所以每一项都先看类型对不对；对不上的跳过，不能让一个坏文件害得整个程序启动不了。
        passages = []
        for event in events:
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                continue
            kind, name = _SEARCHABLE.get(event["type"], ("", ""))
            if event["type"] == "notice" and event.get("level") == "tool":
                kind, name = "tool", "text"  # 在厂商服务器上执行的工具（比如联网搜索）是以提示的形式记下来的
            value = event.get(name) if kind else None
            if isinstance(value, str) and value.strip():
                passages.append(make_passage(kind, value[:RESULT_CHARS] if kind == "result" else value))
        heading = make_passage("title", title if isinstance(title, str) else "")
        # 字符和单词各记一份"出现过哪些"，用来快速排除。拼起来的这一长串只是临时用一下，不留在内存里
        everything = "\n".join([heading.folded, *(passage.folded for passage in passages)])
        self._documents[conversation_id] = Document(
            id=conversation_id,
            updated_at=updated_at,
            title=heading,
            passages=tuple(passages),
            letters=frozenset(everything),
            words=frozenset(_WORD.findall(everything)),
        )
        self._version += 1

    def remove(self, conversation_id: str) -> None:
        self._documents.pop(conversation_id, None)
        self._version += 1

    def search(self, query: str, limit: int = MAX_RESULTS) -> list[dict]:
        """返回命中的会话，最相关的排在前面。每一项说明命中在哪里，页面用它来显示和标出关键词。"""
        terms = split_query(query)
        # 先记下版本、拍个快照：搜索的同时，另一个线程可能正在保存会话
        version = self._version
        documents = list(self._documents.values())
        if not terms or not documents:
            return []
        needles = [self._needle(term, documents, version) for term in terms]
        found = []
        for document in documents:
            hits = [_find(needle, document) for needle in needles]
            if all(hits):  # 每个关键词都得找到
                # 越严格的排得越靠前：先看是不是每个关键词都原样找到了，再看有没有用到最宽松的"隔字包含"，
                # 然后才是分数；一样相关时，新的在前。页面上也是把"全部原样"和其余的分成两组显示的。
                order = (_all_exact(hits), not any(hit.loose for hit in hits), sum(hit.score for hit in hits) / len(hits))
                found.append((order, document, hits))
        found.sort(key=lambda item: (item[0], item[1].updated_at), reverse=True)
        return [_result(document, needles, hits) for _, document, hits in found[:limit]]

    def _needle(self, term: str, documents: list[Document], version: int) -> Needle:
        similar: tuple[str, ...] = ()
        # 相近的词：只对够长的英文单词做，而且只在它哪儿都原样找不到时才做
        if len(term) >= 5 and _WORD.fullmatch(term) and not any(_contains(document, term) for document in documents):
            similar = tuple(difflib.get_close_matches(term, self._words(documents, version), n=3, cutoff=0.78))
        # 隔字包含：太短的词隔着字去找，几乎什么都能对上（"css"能对上"process"），所以英文至少 4 个字母
        loose, slack = None, 0
        if (4 if term.isascii() else 2) <= len(term) <= LOOSE_MAX:
            loose = re.compile("(?=(" + _loose_pattern(term) + "))")  # 套一层 (?=...)：从每个位置都试一次，重叠的几处都能找出来
            slack = len(term) // 2 if term.isascii() else len(term)
        return Needle(term, similar, loose, slack)

    def _words(self, documents: list[Document], version: int) -> list[str]:
        """这批会话里出现过的所有英文单词。算一次不便宜，所以记下来，索引没变就接着用。"""
        cached = self._vocabulary
        if cached is not None and cached[0] == version:
            return cached[1]
        words = sorted(set().union(*(document.words for document in documents)))
        # 连同版本号一起记：万一算的这会儿索引又变了，这份记录的版本对不上，下次自然会重算
        self._vocabulary = (version, words)
        return words


def _contains(document: Document, word: str) -> bool:
    """这段会话里有没有原样出现这个词。先用"出现过哪些字符"排除，多数会话到这一步就不用再看了。"""
    return document.letters.issuperset(word) and any(word in passage.folded for passage in document.everywhere)


def _loose_pattern(term: str) -> str:
    """"隔字包含"用的正则：每个字后面可以夹着最多 GAP 个别的字，然后必须是下一个字。

    夹着的字里不能有"下一个字"本身，也就是说下一个字总是取最先出现的那一个。这样正则每走一步都只有
    一种走法。如果写成"夹着任意字符"，遇到"哈哈哈哈哈哈"这样一长串重复的字，正则会把每一种隔法都试一遍，
    字每多一个，耗时就翻几倍，足以把整个程序卡住（Python 的正则在计算时不会让出线程）。
    代价是极少数情况下会漏掉一处本来对得上的地方，对"找个大概"来说可以接受。

    夹着的字里也不能有空白：不然"ssh"会对上"class Handler"这样横跨两个单词的地方。
    """
    steps = []
    for char, following in zip(term, [*term[1:], ""]):
        steps.append(re.escape(char))
        if following:
            steps.append(f"[^{re.escape(following)}\\s]{{0,{GAP}}}")
    return "".join(steps)


def _all_exact(hits: list[Hit]) -> bool:
    """是不是每个关键词都原样找到了（而不是靠相近的词或隔字包含）。"""
    return all(hit.grade == EXACT for hit in hits)


def _find(needle: Needle, document: Document, body_only: bool = False) -> Hit | None:
    """在一段会话里找一个关键词，返回最好的那一处；找不到返回 None。

    body_only 为真时不看标题，也不用最宽松的那一档：用在"标题已经对上了，看看内容里有没有可以摘录的"。
    """
    everywhere = document.passages if body_only else document.everywhere
    # 第一档：原样包含；第二档：相近的词
    for word, grade in ((needle.term, EXACT), *((word, SIMILAR) for word in needle.similar)):
        if not document.letters.issuperset(word):
            continue  # 连这几个字符都凑不齐，不用一段段去找了
        hits = [Hit(passage, at, at + len(word), grade) for passage in everywhere if (at := passage.folded.find(word)) >= 0]
        if hits:
            return max(hits, key=lambda hit: hit.score)  # 分数一样时取靠前的那一处
    # 第三档：隔字包含
    if body_only or needle.loose is None or not document.letters.issuperset(needle.term):
        return None
    best = None
    for passage in everywhere:
        span = _tightest(needle, passage.folded)
        if span is None:
            continue
        hit = Hit(passage, span[0], span[1], LOOSE * len(needle.term) / (span[1] - span[0]))
        if best is None or hit.score > best.score:
            best = hit
    return best


def _tightest(needle: Needle, folded: str) -> tuple[int, int] | None:
    """隔字包含：找出最紧凑的那一处。夹进来的字太多（超过 needle.slack）就太牵强了，不算。"""
    best = None
    for match in islice(needle.loose.finditer(folded), 2000):
        start, end = match.span(1)
        if end - start - len(needle.term) > needle.slack:
            continue
        if best is None or end - start < best[1] - best[0]:
            best = (start, end)
    return best


def _result(document: Document, needles: list[Needle], hits: list[Hit]) -> dict:
    title = document.title
    # 关键词最好的命中在标题里时，内容里要是也有，同样摘一段出来给人看
    in_body = [
        found
        for needle, hit in zip(needles, hits)
        if (found := hit if hit.passage.kind != "title" else _find(needle, document, body_only=True))
    ]
    return {
        "id": document.id,
        "exact": _all_exact(hits),
        "title": title.shown,
        "title_marks": _marks(title, needles, hits, 0, len(title.shown), title.shown),
        # 摘录内容里最相关的那一处。只有标题对得上的话就没有摘录
        "snippet": _snippet(max(in_body, key=lambda hit: hit.score), needles, hits) if in_body else None,
    }


def _snippet(hit: Hit, needles: list[Needle], hits: list[Hit]) -> dict:
    """把命中位置前后的一小段文字摘出来。"""
    passage = hit.passage
    start = max(0, hit.start - SNIPPET_BEFORE)
    end = min(len(passage.shown), hit.end + SNIPPET_AFTER)
    lead = "…" if start > 0 else ""
    text = lead + passage.shown[start:end] + ("…" if end < len(passage.shown) else "")
    marks = _marks(passage, needles, hits, start, end, text[len(lead) :])
    return {
        "kind": passage.kind,
        "text": text,
        "marks": [[first + len(lead), last + len(lead)] for first, last in marks],
        "find": passage.shown[hit.start : hit.end],  # 命中的原文。页面打开会话后靠它找到并滚到那个位置
    }


def _marks(passage: Passage, needles: list[Needle], hits: list[Hit], start: int, end: int, shown: str) -> list[list[int]]:
    """这段文字的 [start, end) 范围里，哪些位置该标出来。hits 是每个关键词在这段会话里是怎么命中的。

    返回的位置从 start 算起，并且换算成了浏览器里字符串的下标：JavaScript 的字符串按 UTF-16 计数，
    一个表情符号占两格，而 Python 里只占一格。不换算的话，文字里一有表情，后面标的位置就全偏了。
    """
    window = passage.folded[start:end]
    spans: list[tuple[int, int]] = []
    for needle, hit in zip(needles, hits):
        found = [match.span() for word in (needle.term, *needle.similar) for match in re.finditer(re.escape(word), window)]
        # 隔字的标法只用在"这个关键词在这段会话里确实是靠隔字包含才找到的"时候。
        # 否则会把不相干的地方标出来：搜"git"，回复里原样有"git log"，标题"Debugging it"却被标成了 g…it
        if not found and hit.loose and needle.loose is not None and (loose := _tightest(needle, window)):
            at = loose[0]
            for char in needle.term:  # 只标对上的那几个字，不标中间夹着的
                at = window.index(char, at)
                found.append((at, at + 1))
                at += 1
        spans += found
    merged: list[list[int]] = []
    for first, last in sorted(spans):
        if merged and first <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], last)  # 挨着或重叠的并成一段
        else:
            merged.append([first, last])

    def units(index: int) -> int:
        # surrogatepass：万一文字里有半个表情这样编码不了的字符，也照样数得出来，不让整次搜索失败
        return len(shown[:index].encode("utf-16-le", "surrogatepass")) // 2

    return [[units(first), units(last)] for first, last in merged[:MAX_MARKS]]
