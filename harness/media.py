"""多媒体库：所有会话里发过的截图、图片、PDF、Word / Excel / PowerPoint 和文本文件，集中在一处看、下载、找回出处。

这里做三件事：

  原件   发消息时，每个附件的原件另存一份到 data/media/ 下，文件名是内容的 SHA-256（这种做法叫"内容寻址"）：
         同一个文件发十次只存一份；文件名由内容算出来，浏览器没有办法指定路径。以前 Word、Excel、PowerPoint
         只把取出的文字交给模型，原件就没了；现在原件也留着，随时能下载回来。
         附件的概要（attachments.Attachment.summary）里记着这个编号（sha256），会话文件里只多这一个字段。
  索引   每段保存过的会话里有哪些附件：名字、种类、缩略图、在第几条消息里（Entry）。和搜索索引一样，
         由 ConversationStore 在读入和保存会话时顺手更新（见 conversations.py 的 _remember）：
         打开"多媒体"不用把所有会话再读一遍，删掉的会话也不会还留在库里。
  旧会话 有这个功能之前发的附件没有存原件。图片、PDF 和文本文件的完整内容还在发给模型的历史（messages）里，
         从那里取出来；Word、Excel、PowerPoint 只剩当时取出的文字，就给文字，并注明原件不在了。
         历史只读不改（对话历史只在末尾追加，见 CLAUDE.md），所以旧会话照样能接着聊。

页面怎么找到"这个附件在对话里的哪儿"：一条带附件的用户消息，在页面上是消息上方的一排卡片（.msg-atts）。
条目记着它是这段会话里第几排（turn）的第几张（index），页面数到那一排、那一张，滚过去亮一下。
所以"哪些项算数"必须和页面重放时一模一样，都用 attachments.from_saved 来筛。

附件的编号（id）写成"会话编号/第几排/第几张"，例如 20261120-140359-a1b2c3/2/0。浏览器只能给这个编号，
服务器检查格式、在索引里查到条目，再自己决定读哪个文件：浏览器给不出任何路径。
"""

from __future__ import annotations

import base64
import hashlib
import math
import os
import re
import threading
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import quote

from .attachments import HASH, IMAGE_TYPES, PDF_TYPE, Attachment, from_saved
from .i18n import t
from .safe import private_dir, write_file

PAGE_SIZE = 60  # 页面一次要多少项。往下翻到底时再要下一批
MAX_PAGE = 500
KINDS = ("image", "pdf", "document", "text")  # 多媒体库里的分类：图片、PDF、Word / Excel / PowerPoint、文本和代码

# Office 文件的扩展名和它们的类型。documents.py 能读的就是这几种
OFFICE_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "docm": "application/vnd.ms-word.document.macroEnabled.12",
    "dotx": "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    "xltx": "application/vnd.openxmlformats-officedocument.spreadsheetml.template",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "pptm": "application/vnd.ms-powerpoint.presentation.macroEnabled.12",
    "potx": "application/vnd.openxmlformats-officedocument.presentationml.template",
    "ppsx": "application/vnd.openxmlformats-officedocument.presentationml.slideshow",
}
_IMAGE_SUFFIX = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
# 原件只会存成这几种扩展名，回答浏览器时的 Content-Type 也只看这张表，不信概要里记的类型（会话文件可能被改过）
STORED_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "pdf": PDF_TYPE,
    "txt": "text/plain; charset=utf-8",  # 文本一律按纯文本回答，里面写着 HTML 也不会被当成网页
    **OFFICE_TYPES,
    "bin": "application/octet-stream",  # 扩展名不认识的 Office 文件（正常的页面发不出这种）
}
_SAME_SUFFIX = {"jpg": ("jpg", "jpeg", "jpe", "jfif")}  # 这几种写法是同一种格式，文件名不用改

# 和 conversations.py 里会话编号的格式一样（日期-时间-6 位随机字符）。前导的 0 不算数：一个附件只有一种写法
# 只认 0 到 9：Python 的 \d 连全角数字、阿拉伯文的数字也算，那样同一个附件就有了好几种写法
ID = re.compile(r"([0-9]{8}-[0-9]{6}-[0-9a-f]{6})/(0|[1-9][0-9]{0,5})/(0|[1-9][0-9]?)")
_STORED_NAME = re.compile(r"([0-9a-f]{64})\.([a-z]{3,4})")
_ATTACHMENT_TEXT = re.compile(r'<attachment name="([^"]*)"(?: note="([^"]*)")?>\n(.*)\n</attachment>', re.DOTALL)
_DATA_URL = re.compile(r"data:([\w.+/-]+);base64,([A-Za-z0-9+/]*={0,2})")


def item_id(conversation_id: str, turn: int, index: int) -> str:
    """一个附件的编号：哪段会话、第几排（带附件的第几条消息）、这一排的第几个。格式见 ID。"""
    return f"{conversation_id}/{turn}/{index}"


class MediaError(Exception):
    """附件找不到、或者内容没有保存下来。错误信息可以直接显示给用户。"""


# ---- 原件 -------------------------------------------------------------------


def original_of(item: Attachment) -> tuple[bytes, str]:
    """一个检查过的附件的原件，和存的时候用的扩展名。

    图片存的是发给模型的那一份：页面会把太大的照片缩小、把 HEIC 之类换成 JPEG，它就是"模型看到的那张"。
    文本文件存成 UTF-8（页面读的时候已经把 GBK、UTF-16 都换成了文字）。
    """
    if item.kind == "image":
        return base64.b64decode(item.data), _IMAGE_SUFFIX.get(item.media_type, "bin")
    if item.kind == "pdf":
        return base64.b64decode(item.data), "pdf"
    if item.original:  # Word、Excel、PowerPoint：原样的文件
        suffix = _suffix(item.name)
        return item.original, suffix if suffix in OFFICE_TYPES else "bin"
    return item.text.encode("utf-8"), "txt"


class MediaStore:
    """data/media/：附件的原件，文件名是内容的 SHA-256。目录和文件都只有自己的账号能读（和会话一样）。"""

    def __init__(self, directory: Path):
        self.directory = directory

    def keep(self, attachments: Sequence[Attachment]) -> tuple[list[Attachment], str]:
        """把这些附件的原件存起来，返回（记上了编号的附件，没存成的原因）。

        存不成（磁盘满了、没有权限）不该挡住这条消息：没存成的那几个照原样返回、不带编号，
        以后在多媒体库里退回去从对话历史里取（见 Entry.source），只是 Word 这些就只剩文字了。
        """
        kept, problem = [], ""
        for item in attachments:
            try:
                kept.append(self._keep(item))
            except OSError as exc:
                kept.append(item)
                problem = problem or (str(exc) or type(exc).__name__)
        return kept, problem

    def _keep(self, item: Attachment) -> Attachment:
        content, suffix = original_of(item)
        digest = hashlib.sha256(content).hexdigest()
        path = self.directory / f"{digest}.{suffix}"
        if not path.is_file():  # 同样的内容已经存过：不用再写一遍
            private_dir(self.directory)
            write_file(path, content)  # 先写临时文件再一步换上去，权限 600（见 safe.py）
        # 原件已经在磁盘上了，内存里不用再拿着（一个 Word 文件可能有 25 MB）
        return replace(item, sha256=digest, original=b"")

    def stored(self) -> dict[str, tuple[Path, int]]:
        """存着的每个原件：编号 → （文件，大小）。列一次目录，比一个个去查快。"""
        found: dict[str, tuple[Path, int]] = {}
        try:
            entries = list(os.scandir(self.directory))
        except OSError:  # 还没存过任何东西，目录还不存在
            return found
        for entry in entries:
            match = _STORED_NAME.fullmatch(entry.name)
            if match and match.group(2) in STORED_TYPES:
                try:
                    found[match.group(1)] = (Path(entry.path), entry.stat().st_size)
                except OSError:
                    continue
        return found

    def find(self, digest: str) -> Path | None:
        """一个编号对应的原件。编号的格式不对、或者文件不在了，返回 None。"""
        if not HASH.fullmatch(digest or ""):
            return None  # 只接受 64 个十六进制字符：拼不出别的路径
        for suffix in STORED_TYPES:
            path = self.directory / f"{digest}.{suffix}"
            if path.is_file():
                return path
        return None

    def collect(self, referenced: set[str], candidates: Iterable[str] | None = None, unreadable: Iterable[Path] = ()) -> int:
        """删掉没有会话再用的原件，返回删了几个。

        referenced  还有会话在用的编号（保存过的会话，加上当前这段还没存进文件的）。
        candidates  只考虑这几个（删掉一段会话时，只看它用过的那些）；None 表示看全部（启动时清一遍）。
        unreadable  读不出来的会话文件（损坏了、或者格式不认识）。它们不在索引里，可那是你的会话，
                    也许修好了还要用：里面只要提到了某个编号，那个原件就留着。
        宁可多留，不可错删：拿不准的都不删。
        """
        stored = self.stored()
        doubtful = [digest for digest in (stored if candidates is None else set(candidates)) if digest in stored and digest not in referenced]
        if doubtful:
            for path in unreadable:
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return 0  # 读不了一个可能用到它们的文件：这一次什么都不删
                doubtful = [digest for digest in doubtful if digest not in text]
        for digest in doubtful:
            for suffix in STORED_TYPES:  # 同样的内容以不同的扩展名存过（极少见）：一起删
                (self.directory / f"{digest}.{suffix}").unlink(missing_ok=True)
        if candidates is None:  # 启动时：上次存到一半就中断留下的临时文件，也一起清掉
            for leftover in self.directory.glob("*.tmp") if self.directory.is_dir() else ():
                leftover.unlink(missing_ok=True)
        return len(doubtful)


# ---- 索引 -------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """一段会话里的一个附件。只记显示和找回内容要用的那几项，不含内容本身。"""

    turn: int  # 这段会话里第几条带附件的消息（从 0 数起），也就是页面上第几排附件卡片
    index: int  # 那一排里的第几个
    at: float  # 什么时候发的。有这个功能之前的会话没有记，用会话开始的时间
    kind: str  # 多媒体库里的分类：image / pdf / document / text
    name: str
    media_type: str  # 概要里记的类型（只用来显示）
    size: int  # 概要里记的大小：图片和 PDF 是字节数，文本是字数
    thumb: str = ""  # 图片的小缩略图（data: 地址），已经检查过
    sha256: str = ""  # 原件的编号；有这个功能之前发的没有
    source: tuple[int, int] | None = None  # 没有原件时，内容在对话历史里的位置：第几条消息的第几块
    source_bytes: int = 0  # 从历史里取出的内容有多大（字节）


class MediaIndex:
    """每段保存过的会话里有哪些附件。由 ConversationStore 维护，和磁盘上的会话文件保持一致。

    网页的请求和正在进行的一轮对话在不同的线程里，可能同时读写，所以用一把锁。
    """

    def __init__(self) -> None:
        self._items: dict[str, tuple[str, tuple[Entry, ...]]] = {}  # 会话编号 → （标题，附件）
        self._lock = threading.Lock()
        self.version = 0  # 内容每变一次加一。页面靠它（经过状态里的 media）知道要不要重新取列表

    def put(self, conversation_id: str, title: str, entries: Sequence[Entry]) -> None:
        item = (title, tuple(entries))
        with self._lock:
            if not item[1]:
                # 没有附件的会话不占地方，也不算变化：只是聊了几句，多媒体库里什么都没变
                if self._items.pop(conversation_id, None) is not None:
                    self.version += 1
                return
            if self._items.get(conversation_id) == item:
                return  # 存了一次盘，附件和标题都没变（绝大多数的情况）：不算变化，页面不用重新取
            self._items[conversation_id] = item
            self.version += 1

    def remove(self, conversation_id: str) -> None:
        with self._lock:
            if self._items.pop(conversation_id, None) is not None:
                self.version += 1

    def snapshot(self) -> dict[str, tuple[str, tuple[Entry, ...]]]:
        with self._lock:
            return dict(self._items)

    def hashes(self, conversation_id: str | None = None) -> set[str]:
        """用到的原件编号：一段会话的，或者（不给编号时）所有会话的。"""
        with self._lock:
            groups = [self._items.get(conversation_id, ("", ()))] if conversation_id else list(self._items.values())
        return {entry.sha256 for _, entries in groups for entry in entries if entry.sha256}


def entries_of(events: list, messages: list, started_at: float) -> list[Entry]:
    """一段会话里的附件。events 是界面事件，messages 是发给模型的历史（只在需要找旧附件的内容时才看）。

    started_at：会话开始的时间。有这个功能之前发的附件没有记时间，就算在会话开始的时候：
    附件多半是一开头就发的；而且这个时间不会变，不像"更新时间"每聊一轮就变一次，害得页面每轮都重取列表。
    """
    entries: list[Entry] = []
    turn = 0
    parsed: list[_Message] | None = None  # 历史里带附件的那几条消息，用到时才整理
    pointer = 0
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "user":
            continue
        shown = from_saved(event.get("attachments"), previews=False)  # 和页面重放时一样地筛（见文件开头的说明）
        if not shown:
            continue
        at = event.get("at")
        at = float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) and math.isfinite(at) and at > 0 else started_at
        sources: dict[int, tuple[int, int, int]] = {}
        if any("sha256" not in item for item in shown):
            if parsed is None:
                parsed = _attachment_messages(messages)
            sources, pointer = _match(shown, parsed, pointer)
        for index, item in enumerate(shown):
            where = sources.get(index)
            entries.append(
                Entry(
                    turn=turn,
                    index=index,
                    at=at,
                    kind=kind_of(item),
                    name=item["name"],
                    media_type=item["media_type"],
                    size=item["size"],
                    thumb=item.get("thumb", ""),
                    sha256=item.get("sha256", ""),
                    source=(where[0], where[1]) if where and "sha256" not in item else None,
                    source_bytes=where[2] if where and "sha256" not in item else 0,
                )
            )
        turn += 1
    return entries


def kind_of(item: dict) -> str:
    """概要里的附件属于多媒体库的哪一类。Word 这些在概要里记成文本（交给模型的是取出的文字），看扩展名认出来。"""
    if item["kind"] in ("image", "pdf"):
        return item["kind"]
    return "document" if _suffix(item["name"]) in OFFICE_TYPES else "text"


def _suffix(name: str) -> str:
    stem, dot, suffix = name.rpartition(".")
    return suffix.lower() if dot and stem else ""


# ---- 旧会话：从对话历史里找回内容 ---------------------------------------------
# 两种格式：Claude 的（image / document 块）和 OpenAI 那一套的（image_url / file 块，GPT、Gemini、DeepSeek 共用）。
# 文本文件两边都是一段 <attachment name="…"> 包着的文字（见 attachments.wrap_text）。
# 一条消息里附件的顺序是：图片、PDF、文本，然后才是时间说明（<context>）和用户的话（见 providers/）。


@dataclass
class _Message:
    position: int  # 在历史里是第几条
    images: list[tuple[int, str, str]]  # （第几块，类型，base64）
    pdfs: list[tuple[int, str, str]]  # （第几块，文件名，base64）
    texts: list[tuple[int, str, str]]  # （第几块，文件名，文字）


def _attachment_messages(messages: list) -> list[_Message]:
    found = []
    for position, message in enumerate(list(messages)):  # 先复制一份：正在进行的一轮可能同时在末尾追加
        if not isinstance(message, dict) or message.get("role") != "user" or not isinstance(message.get("content"), list):
            continue
        parsed = _Message(position, [], [], [])
        for number, block in enumerate(message["content"]):
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                if block["text"].startswith("<context>"):
                    break  # 附件到这里就完了，后面是用户自己写的话：里面就算写着 <attachment 也不算
                match = _ATTACHMENT_TEXT.fullmatch(block["text"])
                if match:
                    parsed.texts.append((number, _unescape(match.group(1)), match.group(3)))
            elif kind in ("image", "image_url", "document", "file"):
                content = _block_content(block)
                if content:
                    media_type, data, name = content
                    (parsed.pdfs if media_type == PDF_TYPE else parsed.images).append((number, name if media_type == PDF_TYPE else media_type, data))
        if parsed.images or parsed.pdfs or parsed.texts:
            found.append(parsed)
    return found


def _block_content(block: dict) -> tuple[str, str, str] | None:
    """一块图片或 PDF：（类型，base64，文件名）。认不出来返回 None。"""
    kind = block.get("type")
    if kind in ("image", "document"):  # Claude
        source = block.get("source")
        if isinstance(source, dict) and source.get("type") == "base64" and isinstance(source.get("data"), str):
            media_type = source.get("media_type")
            title = given if isinstance(given := block.get("title"), str) else ""
            if (kind == "image" and media_type in IMAGE_TYPES) or (kind == "document" and media_type == PDF_TYPE):
                return media_type, source["data"], title
        return None
    holder = block.get("image_url") if kind == "image_url" else block.get("file")  # OpenAI 那一套
    if not isinstance(holder, dict):
        return None
    url = holder.get("url") if kind == "image_url" else holder.get("file_data")
    match = _DATA_URL.fullmatch(url) if isinstance(url, str) else None
    if not match:
        return None
    media_type = match.group(1)
    if (kind == "image_url" and media_type in IMAGE_TYPES) or (kind == "file" and media_type == PDF_TYPE):
        name = given if isinstance(given := holder.get("filename"), str) else ""
        return media_type, match.group(2), name
    return None


def _decoded_size(data: str) -> int:
    """base64 解码以后有多少字节。不用真的解码：每 4 个字符是 3 个字节，减去结尾补的 = 号。"""
    return len(data) * 3 // 4 - data[-2:].count("=")


def _unescape(value: str) -> str:
    """wrap_text 转义过的文件名还原回来（顺序和转义时相反：& 最后还原）。"""
    return value.replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _match(shown: list[dict], parsed: list[_Message], start: int) -> tuple[dict[int, tuple[int, int, int]], int]:
    """为一条用户消息里的附件，在历史里找到对应的那条消息。返回（第几个附件 → （消息，块，字节数）），和下一次从哪儿找起。

    界面事件和历史并不是一一对应的：一轮刚开始就出错（比如连不上 API）时，历史里的那条消息被撤回了，
    界面上那条消息却还在。所以不按顺序硬对，而是往后找第一条"对得上"的：图片的个数和大小、
    PDF 和文本文件的名字和大小都一样。找到之后下一次从这条开始找（不跳过它）：同一组文件连发两次
    （比如第一次出错了、又发一次）时，两次都能对到同一份内容，反正内容是一样的。
    往后找不到，再从头找一遍（这时不挪动起点）：被撤回的那一轮如果和后面某一轮发的是同样的文件，
    它会对到后面那条消息，起点跟着跳了过去；夹在中间的那一轮只能往回找。
    """
    for position in [*range(start, len(parsed)), *range(start)]:
        found = _fit(shown, parsed[position])
        if found is not None:
            return found, max(position, start)
    return {}, start


def _fit(shown: list[dict], message: _Message) -> dict[int, tuple[int, int, int]] | None:
    found: dict[int, tuple[int, int, int]] = {}
    images = iter(message.images)
    used: set[int] = set()
    for index, item in enumerate(shown):
        if item["kind"] == "image":
            # 图片没有名字可对，按顺序一张对一张。有原件的那张也要占掉它的那一块，后面的才对得上自己的
            block = next(images, None)
            if item.get("sha256"):
                continue
            if block is None or _decoded_size(block[2]) != item["size"]:
                return None
            found[index] = (message.position, block[0], item["size"])
            continue
        if item.get("sha256"):
            continue  # 有原件的不用找
        pool = message.pdfs if item["kind"] == "pdf" else message.texts
        for number, name, content in pool:
            if number in used or name != item["name"]:
                continue
            size = _decoded_size(content) if item["kind"] == "pdf" else len(content)
            if size != item["size"]:
                continue
            used.add(number)
            found[index] = (message.position, number, size if item["kind"] == "pdf" else len(content.encode("utf-8")))
            break
        else:
            return None
    return found


# ---- 列表和内容 ---------------------------------------------------------------


def _fold(text: str) -> str:
    """比较时不管大小写、全角半角。"""
    return unicodedata.normalize("NFKC", text).casefold()


def listing(
    sources: Iterable[tuple[str, str, Sequence[Entry]]],
    stored: dict[str, tuple[Path, int]],
    kind: str = "all",
    query: str = "",
    cursor: int = 0,
    limit: int = PAGE_SIZE,
) -> dict:
    """多媒体库的一页。sources 是（会话编号，标题，附件）；stored 是磁盘上存着的原件（见 MediaStore.stored）。

    最新的在前面。kind 是分类（all 表示全部），query 在文件名和会话标题里找（每个词都要有）。
    counts 是搜索结果里每一类各有多少个，页面的筛选按钮上显示这些数。cursor 是从第几个开始。
    """
    terms = _fold(query).split()
    rows = []
    for conversation_id, title, entries in sources:
        shown_title = title or t("新对话")
        folded_title = _fold(shown_title)
        for entry in entries:
            if terms and not all(term in _fold(entry.name) or term in folded_title for term in terms):
                continue
            rows.append((entry, conversation_id, shown_title))
    counts = {"all": len(rows), **dict.fromkeys(KINDS, 0)}
    for entry, _, _ in rows:
        counts[entry.kind] += 1
    chosen = rows if kind == "all" else [row for row in rows if row[0].kind == kind]
    # 最新的在前；同一条消息里的几个按原来的顺序
    chosen.sort(key=lambda row: (row[0].at, row[1], row[0].turn, -row[0].index), reverse=True)
    page = chosen[cursor : cursor + limit]
    return {
        "items": [_item(entry, conversation_id, title, stored) for entry, conversation_id, title in page],
        "counts": counts,
        "next": cursor + limit if cursor + limit < len(chosen) else None,
    }


def _item(entry: Entry, conversation_id: str, title: str, stored: dict[str, tuple[Path, int]]) -> dict:
    original = stored.get(entry.sha256) if entry.sha256 else None
    suffix = original[0].suffix[1:] if original else ""
    item = {
        "id": item_id(conversation_id, entry.turn, entry.index),
        "conversation": conversation_id,
        "conversation_title": title,
        "at": entry.at,
        "turn": entry.turn,
        "index": entry.index,
        "kind": entry.kind,
        "name": entry.name,
        # 能下载的那一份是什么：原件看存的扩展名；旧附件看历史里的那一份（Word 这些只剩文字）
        "media_type": STORED_TYPES[suffix].split(";")[0] if original else ("text/plain" if entry.kind in ("text", "document") else entry.media_type),
        "size": original[1] if original else entry.source_bytes or entry.size,
        "original": bool(original),  # 原件在不在。旧会话里的 Word、Excel、PowerPoint 只剩取出的文字，这里是 False
        "available": bool(original) or entry.source is not None,  # 还能不能看、能不能下载
        "download": download_name(entry, suffix if original else ""),
    }
    if entry.thumb:
        item["thumb"] = entry.thumb
    return item


def download_name(entry: Entry, suffix: str) -> str:
    """下载时用的文件名。suffix 是原件存的扩展名；没有原件（从历史里取）时是空的。

    尽量就用发的时候的名字。只有内容和名字对不上时才改：比如 HEIC 照片发出去时换成了 JPEG，
    "照片.heic" 下载回来叫"照片.jpg"；旧会话里的 Word 只剩文字，叫"周报.docx.txt"，一看就知道是什么。
    """
    name = entry.name
    if entry.kind == "document" and suffix not in OFFICE_TYPES:
        return name + ".txt" if suffix in ("", "txt") else name
    wanted = suffix or {"image": _IMAGE_SUFFIX.get(entry.media_type, ""), "pdf": "pdf"}.get(entry.kind, "")
    if not wanted or wanted in ("txt", "bin") or _suffix(name) in _SAME_SUFFIX.get(wanted, (wanted,)):
        return name
    stem = name.rpartition(".")[0] if _suffix(name) else name
    return f"{stem}.{wanted}"


def find(sources: Iterable[tuple[str, str, Sequence[Entry]]], media_id: str) -> tuple[str, Entry]:
    """按编号找到一个附件，返回（会话编号，条目）。编号的格式由调用的人先用 ID 查过。"""
    match = ID.fullmatch(media_id)
    if not match:
        raise MediaError(t("参数不正确"))
    conversation_id, turn, index = match.group(1), int(match.group(2)), int(match.group(3))
    for source_id, _, entries in sources:
        if source_id != conversation_id:
            continue
        for entry in entries:
            if entry.turn == turn and entry.index == index:
                return conversation_id, entry
    raise MediaError(t("找不到这个文件了：它所在的会话可能已经被删除。"))


def content_of(entry: Entry, store: MediaStore, messages: Callable[[], list]) -> tuple[bytes, str, str]:
    """一个附件能下载的内容：（字节，Content-Type，文件名）。原件优先；没有原件的从对话历史里取。

    messages 是一个函数：只有真要从历史里取的时候才去读（可能要把一个很大的会话文件读进来）。
    """
    path = store.find(entry.sha256) if entry.sha256 else None
    if path is not None:
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise MediaError(t("读不出这个文件（{error}）。", error=exc)) from exc
        suffix = path.suffix[1:]
        return content, STORED_TYPES[suffix], download_name(entry, suffix)
    if entry.source is None:
        raise MediaError(t("这个文件的内容没有保存下来。"))
    position, number = entry.source
    try:
        block = messages()[position]["content"][number]
    except (LookupError, TypeError) as exc:
        raise MediaError(t("这个文件的内容没有保存下来。")) from exc
    if not isinstance(block, dict):
        raise MediaError(t("这个文件的内容没有保存下来。"))
    if block.get("type") == "text":
        match = _ATTACHMENT_TEXT.fullmatch(block.get("text") or "")
        if not match:
            raise MediaError(t("这个文件的内容没有保存下来。"))
        return match.group(3).encode("utf-8"), STORED_TYPES["txt"], download_name(entry, "")
    found = _block_content(block)
    if not found:
        raise MediaError(t("这个文件的内容没有保存下来。"))
    media_type, data, _ = found
    try:
        content = base64.b64decode(data)
    except ValueError as exc:
        raise MediaError(t("这个文件的内容没有保存下来。")) from exc
    return content, media_type, download_name(entry, "")


def disposition(name: str) -> str:
    """Content-Disposition：让浏览器把内容当成下载的文件，并且知道它叫什么。

    中文名要按 RFC 5987 写成 filename*=UTF-8''…（百分号编码）。老一些的程序只认 filename="…"，
    所以另外给一个只有 ASCII 的名字：认不出来的字符、引号和反斜杠都换成下划线。
    """
    fallback = "".join(char if 32 <= ord(char) < 127 and char not in '"\\%;' else "_" for char in name) or "file"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"
