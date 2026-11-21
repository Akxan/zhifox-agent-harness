"""附在用户消息里的"目录"：告诉模型现在有哪些 Skill、哪些 Agent 可以用。

为什么放在用户消息里，而不是系统提示词里：一段对话里，系统提示词和工具说明书必须一字不差（缓存靠它；
Claude 之前的思考内容也和它绑在一起，见 providers/claude.py），而你随时可能装上一个 Skill、新建一个 Agent。
所以目录和当前时间（agent.py 的 <context>）一样，附在用户的消息里：

  - 一段对话的第一条消息前面附上当时的目录（比如 <skills> 块）；一个可用的都没有就不附。
  - 之后每条消息发出之前，和"模型最近一次看到的那份"比一比；变了，就在这条消息前面附上新的一份。
  - "模型最近一次看到的那份"不另外记，直接从对话历史里找：历史只在末尾追加，里面有什么模型就看到过什么。
    这样打开一段旧会话、某一轮出错被撤回，都不用额外处理。唯一的例外是 Claude 的自动压缩：
    压缩之前的内容 API 不再发给模型，那里面的目录也就不算看到过了。

这套做法 Skill（skills/catalog.py）和 Agent（agents/catalog.py）各用一份，区别只是标签和那几句话，
所以写成一个类，两边各自造一个。Claude Code 也是这样做的：可用的 Skill、Agent 列表作为附加信息放进消息里。
"""

from __future__ import annotations

import html
from dataclasses import dataclass


@dataclass(frozen=True)
class Catalog:
    """一种目录。tag 是外面那层标签（skills → <skills>…</skills>），list_tag 是列表的标签，item 是每一项的标签。

    first、changed、none 是三种情况下写在列表前面的话（写给模型看的，所以是中文）：第一次给它看、
    之后变了、全都没有了。认目录的时候也靠它们：只认开头是这三句之一的，用户随手贴的同名标签不算。
    after 是"它前面可能紧挨着的那几个结束标签"：OpenAI 格式把几段文字用空行连成一段，
    目录跟在时间说明（</context>）后面，或者跟在前一份目录后面。
    """

    tag: str
    list_tag: str
    item: str
    first: str
    changed: str
    none: str
    after: tuple[str, ...] = ("</context>",)

    @property
    def open(self) -> str:
        return f"<{self.tag}>"

    @property
    def close(self) -> str:
        return f"</{self.tag}>"

    def render(self, entries: list[tuple[str, str]]) -> str:
        """目录本身。名字和说明里的 < > & 换成转义的写法：说明是别人写的，里面要是有 </skills>，
        不能让它把这个块提前结束。"""
        lines = [f"<{self.list_tag}>"]
        for name, description in entries:
            lines += [
                f"<{self.item}>",
                f"<name>{html.escape(name, quote=False)}</name>",
                f"<description>{html.escape(description, quote=False)}</description>",
                f"</{self.item}>",
            ]
        lines.append(f"</{self.list_tag}>")
        return "\n".join(lines)

    def note(self, entries: list[tuple[str, str]], history: list) -> str:
        """这条用户消息前面要不要附上目录，附什么。不用附的时候返回空字符串。"""
        seen = self.last_seen(history)
        if not entries:
            return "" if seen in (None, "") else f"{self.open}\n{self.none}\n{self.close}"
        current = self.render(entries)
        if current == seen:
            return ""
        return f"{self.open}\n{self.first if seen is None else self.changed}\n{current}\n{self.close}"

    def last_seen(self, history: list) -> str | None:
        """模型最近一次看到的目录（render 的结果）。None：还没给它看过；""：最近一次告诉它的是"一个都没有"。"""
        seen: str | None = None
        for message in history:
            if not isinstance(message, dict):
                continue
            if message.get("role") == "assistant" and _compacted(message):
                seen = None  # 压缩之前的内容模型已经看不到了
            if message.get("role") != "user":
                continue
            for text in _texts(message.get("content")):
                block = self._block(text)
                if block is not None:
                    seen = block
        return seen

    def _block(self, text: str) -> str | None:
        """从一段文字里找出我们附上的目录。只认我们自己写的样子（开头是三句话之一）。"""
        list_open, list_close = f"<{self.list_tag}>", f"</{self.list_tag}>"
        for start in self._starts(text):
            rest = text[start + len(self.open) + 1 :]
            if rest.startswith(self.none):
                return ""
            if rest.startswith((self.first, self.changed)) and list_open in rest and list_close in rest:
                return rest[rest.index(list_open) : rest.index(list_close) + len(list_close)]
        return None

    def _starts(self, text: str) -> list[int]:
        # Claude 那边目录是单独的一块文字；OpenAI 格式把几段文字用空行连成一段，目录紧跟在时间说明（或者前一份目录）后面
        found = [0] if text.startswith(self.open + "\n") else []
        for tag in self.after:
            marker = tag + "\n\n" + self.open + "\n"
            index = text.find(marker)
            if index >= 0:
                found.append(index + len(tag) + 2)
        return found


def _texts(content: object) -> list[str]:
    """一条用户消息里的文字。Claude 的格式是一串内容块，OpenAI 格式只有文字时直接是一个字符串。"""
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [block["text"] for block in content if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)]
    return []


def _compacted(message: dict) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(isinstance(block, dict) and block.get("type") == "compaction" for block in content)
