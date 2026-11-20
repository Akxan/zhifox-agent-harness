"""会话的保存和读取。

每段会话是 data/conversations/ 下的一个 JSON 文件，里面有三样东西：

  基本信息   标题、用的是哪家的哪个模型、创建和更新时间
  messages   发给模型的对话历史。格式因厂商而异，这里原样保存，读回来就能接着聊
  events     界面上显示过的内容，用来把页面重新画出来

模型自己没有记忆，所以"接着上次聊"的全部秘密就是：把上次的 messages 读回来，下次请求时一起发过去。
"""

from __future__ import annotations

import json
import math
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .search import SearchIndex

FORMAT_VERSION = 1
TITLE_LIMIT = 40

# 会话编号的格式：日期-时间-6 位随机字符，如 20261120-140359-a1b2c3。
# 编号会用在文件名里，所以读写之前一定先检查格式，不接受别的写法（防止拼出别处的路径）。
_ID_PATTERN = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")


class ConversationError(Exception):
    """会话读不出来或不存在。错误信息可以直接显示给用户。"""


@dataclass
class Conversation:
    id: str
    provider: str
    model: str
    title: str = ""
    created_at: float = 0.0  # 时间都是"从 1970 年起的秒数"，方便排序，也方便页面换算成本地时间
    updated_at: float = 0.0
    context_tokens: int = 0  # 最近一次请求时这段会话占用的上下文，打开旧会话时用来提示费用
    messages: list = field(default_factory=list)
    events: list = field(default_factory=list)
    saved: bool = False  # 有没有写到磁盘上（新会话在第一轮对话完成之前只在内存里）

    def summary(self) -> dict:
        """会话列表里要显示的那几项，不含正文。"""
        return {
            "id": self.id,
            "title": self.title or "新对话",
            "provider": self.provider,
            "model": self.model,
            "updated_at": self.updated_at,
        }


def make_title(text: str) -> str:
    """用用户的第一句话当标题：取第一行，太长就截断。"""
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    title = " ".join(first_line.split())
    return title if len(title) <= TITLE_LIMIT else title[: TITLE_LIMIT - 1] + "…"


class ConversationStore:
    def __init__(self, directory: Path):
        self.directory = directory
        # 列表页只需要标题和时间。启动时把每个文件扫一遍记在内存里，之后只在保存、删除时更新，
        # 这样刷新列表不用每次都把所有会话的正文读一遍。
        self._summaries: dict[str, dict] = {}
        # 搜索要看正文。同样是启动时把可以搜的文字抽出来放在内存里（见 search.py），之后跟着保存和删除更新。
        self._index = SearchIndex()
        if directory.is_dir():
            for path in directory.glob("*.json"):
                try:
                    conversation = self._read(path.stem)
                except ConversationError:
                    continue  # 损坏的文件不列出来，也不删除，留给用户自己处理
                self._remember(conversation)

    def new(self, provider: str, model: str) -> Conversation:
        """开一段新会话。它先只存在于内存里，等第一轮对话完成后才写到磁盘。"""
        now = time.time()
        conversation_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + "-" + secrets.token_hex(3)
        return Conversation(id=conversation_id, provider=provider, model=model, created_at=now, updated_at=now)

    def summaries(self) -> list[dict]:
        """所有已保存的会话，最近更新的排在前面。"""
        return sorted(self._summaries.values(), key=lambda item: item["updated_at"], reverse=True)

    def exists(self, conversation_id: str) -> bool:
        return conversation_id in self._summaries

    def load(self, conversation_id: str) -> Conversation:
        if conversation_id not in self._summaries:
            raise ConversationError("这段会话不存在，可能已经被删除了。")
        return self._read(conversation_id)

    def save(self, conversation: Conversation, touch: bool = True) -> None:
        """写到磁盘。touch 为 False 时不改"更新时间"（比如只是改了标题，不应该让它跳到列表最前面）。"""
        if touch:
            conversation.updated_at = time.time()
        data = asdict(conversation)
        del data["saved"]
        data["version"] = FORMAT_VERSION
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(conversation.id)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)  # 先写临时文件再替换：中途断电也不会留下半个文件
        conversation.saved = True
        self._remember(conversation)

    def rename(self, conversation_id: str, title: str) -> None:
        conversation = self.load(conversation_id)
        conversation.title = make_title(title)
        self.save(conversation, touch=False)

    def delete(self, conversation_id: str) -> None:
        if conversation_id not in self._summaries:
            raise ConversationError("这段会话不存在，可能已经被删除了。")
        self._path(conversation_id).unlink(missing_ok=True)
        del self._summaries[conversation_id]
        self._index.remove(conversation_id)

    def search(self, query: str) -> list[dict]:
        """在所有保存过的会话里搜索，标题和内容都找。

        返回的每一项是列表里要显示的基本信息，加上命中的情况（见 search.py 里的 _result）。
        """
        results = []
        for item in self._index.search(query):
            summary = self._summaries.get(item["id"])
            if summary is None:
                continue  # 搜索的同时，这一段刚好被删掉了
            # 标题用索引里的那一份（标出关键词的位置是按它算的）；没有标题的会话和列表里一样显示成"新对话"
            results.append({**summary, **item, "title": item["title"] or summary["title"]})
        return results

    def _remember(self, conversation: Conversation) -> None:
        """把一段会话记进内存里的两份目录：列表用的基本信息，和搜索用的文字。"""
        self._summaries[conversation.id] = conversation.summary()
        self._index.put(conversation.id, conversation.title, conversation.events, conversation.updated_at)

    def _path(self, conversation_id: str) -> Path:
        if not _ID_PATTERN.fullmatch(conversation_id):
            raise ConversationError("会话编号的格式不对。")
        return self.directory / f"{conversation_id}.json"

    def _read(self, conversation_id: str) -> Conversation:
        path = self._path(conversation_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            conversation = Conversation(
                id=conversation_id,
                provider=data["provider"],
                model=data["model"],
                title=data.get("title", ""),
                created_at=float(data.get("created_at", 0)),
                updated_at=float(data.get("updated_at", 0)),
                context_tokens=int(data.get("context_tokens", 0)),
                messages=data["messages"],
                events=data.get("events", []),
                saved=True,
            )
        except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
            raise ConversationError("这段会话的文件读不出来，可能已经损坏。") from exc
        # 文件是普通的 JSON，可能被手动改过。这几项的类型只要有一项不对，就当作损坏的文件处理，
        # 免得后面的代码和页面拿到意料之外的东西（比如标题是个数字，页面拿它当文字用就会出错）。
        texts_ok = all(isinstance(value, str) for value in (conversation.provider, conversation.model, conversation.title))
        lists_ok = isinstance(conversation.messages, list) and isinstance(conversation.events, list)
        times_ok = math.isfinite(conversation.created_at) and math.isfinite(conversation.updated_at)
        if not (texts_ok and lists_ok and times_ok):
            raise ConversationError("这段会话的文件读不出来，可能已经损坏。")
        return conversation
