"""从某一轮分叉成一段新对话："从第 3 轮换个思路重来"，原来那段一字不动。

两种分叉
--------
  在回答上分叉（at="reply"）       新的一段里是从开头到这一轮结束的全部内容：你的消息、模型的思考和回答、
                                   工具调用和结果。接着发一条消息，就是"到这里为止都留着，下面换个方向"。
  在你的消息上分叉（at="message"）  新的一段截到这一轮开始之前；那条消息（文字和附件）放回输入框，改了再发，
                                   就是"这一轮换个问法"。在第一轮上这样分叉，新的一段是空的：它和新对话一样，
                                   先只在内存里，第一轮结束才存盘。

为什么不违反"只追加"
--------------------
对话历史的规矩是只在末尾追加：前面的内容不改、不删（见 providers/claude.py 开头）。分叉没有改动任何一段历史：
  - 原来那段一个字节都不动：只是读它存在磁盘上的文件；
  - 新的一段的 messages 是原来那段的一个前缀，切在某一轮的开头。这和倒回最后一轮是同一个道理（见 rewind.py）：
    从末尾去掉一截，留下的每一块，它前面的内容都和当初一字不差，Claude 思考内容的签名照样对得上；
  - 之后新的一段照常只在末尾追加。
所以新的一段用同一家的同一个模型、同一个角色接着聊（provider、model、agent 原样带过去），思考块原样留着。

切口在哪儿
----------
每一轮的 user 事件上记着这一轮开始时历史的位置（mark，见 rewind.py）。切口只落在这些位置上：
  在你的消息上分叉  切在这一轮自己的 mark；
  在回答上分叉      切在下一轮的 mark（这一轮的结尾就是下一轮的开头）；已经是最后一轮的，就是整段历史。
位置是不是"一轮的开头、还在历史里"，交给适配层的规矩去认（providers.saved_before，和 Backend.turn_at 同一笔账）：
  - GPT、Gemini、DeepSeek 的历史压缩过（见 providers/compaction.py）：切口落在已经写进摘要的那一段，分不了，
    和倒回最后一轮同样的规矩。切口在摘要之后的，新的一段带着那两条摘要，dropped（压缩掉了多少条）原样带过去：
    它的 messages 仍然是原来那段的前缀，事件里每一轮记着的 mark 也就照样对得上（新的一段里同样能编辑、重新生成最后一轮）；
  - 没有记 mark 的那一轮（定时任务自己跑的那一轮、旧版本记下的会话）不能在那里分叉：不知道它在历史里的哪儿。
events 截到同一个位置：这一轮的运行记录（trace）、子 Agent 的卡片、确认请求都跟着这一轮走。

不跟过来的
----------
  改了哪些文件、撤回和重做（file_changes、file_undo 事件）  不带过去。留底（data/backups/turns/）按一轮的编号存，
      磁盘上的文件也只有一份：两段会话都能撤回同一轮的话，一边撤回了，另一边那一行还写着"没撤回"（每个文件现在的状态，
      是从这段会话自己的事件里推出来的，见 undo/restore.fold），两边就对不上了。所以撤回只在原会话里做，留底也不复制；
      新的一段在顶上说一句"这几轮改过文件，要撤回请回原会话"（forked_from 的 changes）。撤回以后还没告诉模型的那几句
      （undo/notes.py）也只留在原会话：分叉点之后磁盘上的文件又变过的话，新的一段里的模型不知道，和别的程序改了文件一样，
      它再用到时会重新看一眼。
  定时任务、聊天通道的标记（task、channel）  新的一段是普通的网页会话：不算那个任务跑出来的，也不在那个聊天里，
      所以没有 send_file（只有在聊天通道里开始的会话才有，见 App.conversation_tools）。
  花掉的钱  账本照旧，一笔不少。新的一段占多大的上下文，按它最后一轮的运行记录估一个（没有记录就是 0，等下一次请求回来）。

正在进行的一轮不能分叉：会话文件里只有结束了、存过盘的那几轮，分叉只读文件（ConversationStore.load 拿着会话库的锁，
读不到写了一半的）；页面上也只在结束了的轮上放按钮。正被聊天通道占着的会话也能分叉：只是读它，不往里写。

这里只管"截到哪、复制什么"，不碰网页（网页那一侧见 webui/fork_api.py，换过去接着聊见 App.fork_conversation）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .attachments import from_saved
from .conversations import FORK_POINTS, TITLE_LIMIT, Conversation, ConversationStore
from .i18n import t
from .media import item_id
from .providers import saved_before
from .rewind import mark_of

# 不跟着分叉过去的事件（为什么见开头的"不跟过来的"）
LEFT_OUT = frozenset({"file_changes", "file_undo"})
# 估上下文大小时，一次模型请求的这几样 token 加起来（见 trace/recorder.py 的 TOKENS）
_CONTEXT_PARTS = ("input", "cache_read", "cache_write", "output")


class ForkError(Exception):
    """分叉不了。错误信息可以直接显示给用户。"""


@dataclass(frozen=True)
class Turn:
    """会话里的一轮，从它的 user 事件看。"""

    position: int  # 它的 user 事件是 events 里的第几个
    mark: int | None  # 这一轮开始时历史的位置（见 rewind.mark_of）；没记的是 None
    text: str  # 你写的话（页面上显示的样子）


@dataclass(frozen=True)
class Fork:
    conversation: Conversation  # 新的一段（还没存盘）
    draft: str  # 在你的消息上分叉时，放回输入框的那段文字；在回答上分叉是空的
    # 那条消息的附件在多媒体库里的编号（原会话里的，见 media.item_id）。页面照着它取回原件，放进输入框
    attachments: list[str]


def turns(events: Sequence[Any]) -> list[Turn]:
    """一段会话里的每一轮（每个 user 事件一轮）。"""
    found = []
    for position, event in enumerate(events):
        if isinstance(event, dict) and event.get("type") == "user":
            found.append(Turn(position, mark_of(event), event.get("text") if isinstance(event.get("text"), str) else ""))
    return found


def unmarked() -> str:
    """没有记下位置的那一轮为什么不能分叉。"""
    return t("这一轮没有记下它在对话历史里的位置（定时任务自己跑的那一轮、旧版本的智狐记下的会话都是这样），不能在这里分叉。")


def compacted() -> str:
    return t("这一轮已经压缩进了摘要（对话太长时较早的内容会写成摘要），不能在这里分叉了。")


def changed() -> str:
    return t("对话刚刚变过了，这一轮已经对不上。看一眼最新的内容再试。")


def floor(backend: Any) -> int:
    """当前这段对话里，最早还能分叉的位置：比它早的都压缩进了摘要。页面拿它和每一轮的 mark 比，把分不了的按钮画成灰的
    （真正分不分得了，由 make 照着会话文件再查一遍）。没有压缩过的就是第一轮的位置（Claude 是 0，OpenAI 格式的是 1）。

    从压缩掉了多少条（dropped，也就是内存里第 0 条的位置）往后找第一个"一轮的开头"：压缩过的话，前面是系统提示词和两条摘要，
    只走几步。"""
    mark, end = backend.dropped(), backend.mark()
    while mark < end and not backend.turn_at(mark):
        mark += 1
    return mark


def fork_title(title: str) -> str:
    """新的一段的标题：原来的标题加上"（分叉）"。太长的话截原来的标题，后缀留着（会话列表里一眼认得出）。
    从分叉出来的会话再分叉，不再叠一个后缀。"""
    base = title or str(t("新对话"))
    suffix = str(t("{title}（分叉）", title="")).strip()
    if suffix and base.endswith(suffix):
        return base
    full = str(t("{title}（分叉）", title=base))
    if len(full) > TITLE_LIMIT:
        keep = max(1, len(base) - (len(full) - TITLE_LIMIT) - 1)
        full = str(t("{title}（分叉）", title=base[:keep].rstrip() + "…"))
    return full


def make(source: Conversation, provider: Any, store: ConversationStore, index: int, at: str, expect: tuple[int | None, str] | None = None) -> Fork:
    """从 source（读自磁盘的一段会话）的第 index 轮（从 0 数）分叉，做好新的一段（还没存盘）。分叉不了抛出 ForkError。

    provider：source 用的那一家（providers.find 找到的；找不到的由调用的人先拦下）。
    expect：页面上看到的那一轮的（mark，文字）。和文件里的第 index 轮对不上（另一个标签页刚倒回过、聊天通道刚改过……）就不做。
    """
    if at not in FORK_POINTS:
        raise ForkError(t("参数不正确"))
    found = turns(source.events)
    if not 0 <= index < len(found):
        raise ForkError(changed())
    turn = found[index]
    if expect is not None and (turn.mark, turn.text) != expect:
        raise ForkError(changed())
    if turn.mark is None:
        raise ForkError(unmarked())

    def before(mark: int) -> list:
        kept = saved_before(provider, source.messages, source.dropped, mark)
        if kept is None:
            # 压缩过的历史里对不上，多半是这一轮写进了摘要；没压缩过还对不上，是文件和事件对不上了
            raise ForkError(compacted() if source.dropped else t("这一轮在对话历史里找不到了，不能在这里分叉。"))
        return kept

    start = before(turn.mark)  # 这一轮本身还在历史里（两种分叉都要查）
    following = found[index + 1] if index + 1 < len(found) else None
    if at == "message":
        messages, events = start, list(source.events[: turn.position])
    elif following is None:
        messages, events = list(source.messages), list(source.events)
    elif following.mark is None:
        raise ForkError(t("下一轮没有记下它在对话历史里的位置，找不到这一轮在哪儿结束，不能在这里分叉。"))
    else:
        messages, events = before(following.mark), list(source.events[: following.position])

    kept = [event for event in events if not (isinstance(event, dict) and event.get("type") in LEFT_OUT)]
    conversation = store.new(source.provider, source.model)
    conversation.title = fork_title(source.title)
    conversation.agent = source.agent
    conversation.messages = messages
    conversation.dropped = source.dropped  # 前缀里的位置照原来的数法（见开头的"切口在哪儿"）
    conversation.events = kept
    conversation.context_tokens = context_of(kept) if len(messages) < len(source.messages) else source.context_tokens
    conversation.forked_from = {
        "id": source.id, "turn": index + 1, "at": at, "title": source.title, "changes": len(kept) < len(events),
    }  # fmt: skip
    if at != "message":
        return Fork(conversation, "", [])
    return Fork(conversation, turn.text, attachment_ids(source, found, index))


def attachment_ids(source: Conversation, found: Sequence[Turn], index: int) -> list[str]:
    """第 index 轮那条消息的附件在多媒体库里的编号。"第几排"按 from_saved 筛过的数，和多媒体库、页面重放时一样（见 media.entries_of）。"""
    rows = sum(bool(from_saved(source.events[turn.position].get("attachments"), previews=False)) for turn in found[:index])
    shown = from_saved(source.events[found[index].position].get("attachments"), previews=False)
    return [item_id(source.id, rows, number) for number in range(len(shown))]


def context_of(events: Sequence[Any]) -> int:
    """按最后一轮的运行记录（trace 事件）估这段对话占多大的上下文：那一轮最后一次模型请求的输入和输出加起来。
    压缩历史的那次请求不算（它发的是要压缩的那一段，不是整段对话）。没有记录的是 0。"""
    for event in reversed(events):
        if not isinstance(event, dict) or event.get("type") != "trace" or not isinstance(event.get("spans"), list):
            continue
        for span in reversed(event["spans"]):
            if isinstance(span, dict) and span.get("kind") == "model" and span.get("compaction") is not True:
                return sum(value for part in _CONTEXT_PARTS if isinstance(value := span.get(part), int) and not isinstance(value, bool) and value > 0)
        return 0
    return 0
