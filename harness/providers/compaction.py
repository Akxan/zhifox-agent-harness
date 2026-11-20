"""GPT、Gemini、DeepSeek 的对话压缩：对话太长时，把较早的部分换成一份摘要。

为什么要压缩
------------
模型没有记忆，每一轮都要把整段对话历史重新发一遍。聊得越久，每一轮发的就越多：越来越贵、越来越慢，
最后超出模型一次能读的上限（上下文窗口），请求被拒绝，这段对话就聊不下去了。

Claude 不用这里的代码：Anthropic 的 API 自己会在服务器上压缩（见 claude.py 和 config.ENABLE_COMPACTION）。
OpenAI 格式的接口没有这个功能，所以智狐自己做，请的还是正在对话的那个模型：

  1. 每次请求之后，看用量里的上下文大小（Usage.context_tokens）。超过了模型上限的 COMPACTION_THRESHOLD，
     就在下一次请求之前先压缩（is_due）。
  2. 找一个干净的切口（find_cut）：切在"开始一轮对话的那条用户消息"前面。工具调用和它的结果在同一轮里，
     所以永远落在切口的同一边，不会留下"有调用、没结果"的历史（API 会拒绝这样的请求）。
     最近的 COMPACTION_KEEP_TURNS 轮（正在进行的这一轮也算）保持原样。
  3. 把切口之前的部分原样发给模型，末尾加一条指令，请它写摘要（summary_request）。
     原样发是为了命中缓存：这一段之前每次请求都发过，开头一字不差，各家都按低得多的缓存价计费。
  4. 用摘要换掉切口之前的部分（compacted）：系统提示词 + 一问一答两条摘要消息 + 最近几轮的原文。
     之后照常只在末尾追加；下一次又太长时再压缩一次，上一份摘要会被合进新的摘要里。

有意的破例
----------
整个程序有一条规矩：对话历史只在末尾追加，前面的内容不改、不删（原因见 claude.py 里 AnthropicBackend.__init__
的说明）。压缩是唯一的例外：它把历史较早的那一段一次性换成摘要，然后又回到只追加。边界是：

  - 只有 OpenAI 格式的适配层这样做。Claude 的历史仍然只追加，它的压缩发生在 Anthropic 的服务器上；
  - 只换切口之前的部分，系统提示词和最近几轮一个字都不动；
  - 只在两次请求之间做。发出去的一次请求，在它回来之前，历史不会变。

代价是一次缓存失效：DeepSeek 这类按"开头相同"来缓存的，压缩之后的第一次请求开头变了，要按原价付一次，
之后新的开头又会被缓存起来。换来的是历史不再无限变长。不压缩的话，每一轮都要重发越来越长的原文，
到了上限，就一个请求也发不出去了。

界面上显示过的内容（会话文件里的 events）不受影响：页面重放、搜索，看到的仍然是完整的对话。
被换成摘要的只是发给模型的那一份（messages）。
"""

from __future__ import annotations

import re

from ..config import (
    COMPACTION_KEEP_TURNS,
    COMPACTION_THRESHOLD,
    CONTEXT_WINDOWS,
    DEFAULT_CONTEXT_WINDOW,
    ENABLE_OPENAI_COMPACTION,
)

# ---- 写给模型看的话 -------------------------------------------------------
# 这些是发给模型的，不是给用户看的，所以不翻译（见 CLAUDE.md）。都写在这一处。

# 请模型写摘要的指令。它作为一条用户消息，接在要压缩的那段对话后面
SUMMARY_REQUEST = """\
（这条消息是助手程序自动发的，不是用户说的话。）
这段对话已经很长了。为了能接着聊下去，到这里为止的对话要换成一份摘要：之后你只能看到这份摘要，看不到原文。请写这份摘要。

要写进去的：
- 用户想做成什么事，提过的要求、偏好和限制。
- 做过的决定和理由；已经做完的、做到一半的、还没开始的事。
- 之后可能用得上的具体信息：文件路径、命令、数字、名字、链接、关键的代码片段，出过的错和解决的办法。
- 用户发过的附件（图片、PDF、文件）：它是什么，里面要紧的内容。原件之后不会再发给你。
- 如果前面已经有一份更早的摘要，把它的内容也合并进来。

要求：
- 用和用户对话时相同的语言写。只写摘要本身：不要调用工具，不要回答问题，也不要接着做别的事。
- 尽量精炼，按要紧的程度取舍，一般不超过 2000 字。"""

# 摘要放进历史时，前后加上这个标签。程序靠它认出"这是上一次压缩留下的摘要"（见 is_summary）
_TAG = "conversation_summary"
# 摘要后面跟着的说明，让模型知道这一段是怎么来的
SUMMARY_NOTE = "（这是助手程序自动放进来的：较早的对话太长，已经换成了上面这份摘要，原文不再发给你。请根据摘要接着下面的对话。）"
# 被压缩的那一段里有附件时，再加这一句。{names} 是附件的称呼，用顿号隔开
ATTACHMENTS_NOTE = "较早的对话里，用户发过这些附件：{names}。原件已经不在对话里了，需要里面的细节时，请用户重新发送。"
# 摘要后面接一条助手的确认。这样历史仍然是用户、助手轮流说话，后面接着的又是一条用户消息：
# 有的模型（比如 DeepSeek 的推理模型）不接受连着两条用户消息
SUMMARY_ACK = "好的，我会根据这份摘要接着之前的对话。"

# 文本附件在历史里的样子是 <attachment name="文件名">……（见 attachments.wrap_text），从这里取出文件名
_ATTACHMENT_NAME = re.compile(r'<attachment name="([^"]*)"')


def context_window(model: str) -> int:
    """一个模型一次最多能读多少 token。不认识的模型按保守的默认值算（见 config.py）。"""
    return CONTEXT_WINDOWS.get(model, DEFAULT_CONTEXT_WINDOW)


def is_due(context_tokens: int, model: str) -> bool:
    """上一次请求占用的上下文已经大到该压缩了吗。context_tokens 是 0 表示还不知道（还没请求过），不算。"""
    return ENABLE_OPENAI_COMPACTION and context_tokens > 0 and context_tokens >= COMPACTION_THRESHOLD * context_window(model)


def is_summary(message: dict) -> bool:
    """这条消息是不是上一次压缩留下的摘要。"""
    content = message.get("content")
    return message.get("role") == "user" and isinstance(content, str) and content.startswith(f"<{_TAG}>")


def find_cut(messages: list[dict], keep_turns: int = COMPACTION_KEEP_TURNS) -> int | None:
    """切口在哪里。返回一个下标：它之前的部分（系统提示词除外）写进摘要，它和它之后的部分原样留着。
    对话还不到 keep_turns 轮、没有可以压缩的内容时，返回 None。

    在 OpenAI 格式的历史里，role 为 user 的消息只有一种来源：用户发来的一条消息（工具结果的 role 是 tool）。
    所以每一条 user 消息都是一轮的开头，切在它前面，工具调用和它的结果一定在同一边。
    上一次压缩留下的摘要不算一轮：它会和更早的内容一起，写进新的摘要。
    """
    starts = [
        index
        for index, message in enumerate(messages)
        if index > 0 and message.get("role") == "user" and not is_summary(message)
    ]
    # 正在进行的这一轮永远保留，所以至少留 1 轮。适配层的 mark / rollback 靠这一点（见 openai_compat.py）
    keep = max(1, keep_turns)
    if len(starts) <= keep:
        return None
    return starts[-keep]


def summary_request(messages: list[dict], cut: int) -> list[dict]:
    """请模型写摘要时发出去的历史：切口之前的部分原样不动（开头和平时的请求一样，才能命中缓存），末尾加上指令。"""
    return [*messages[:cut], {"role": "user", "content": SUMMARY_REQUEST}]


def compacted(messages: list[dict], cut: int, summary: str) -> list[dict]:
    """压缩之后的历史：系统提示词、两条摘要消息、切口和它之后的原文（这两头原样不动）。"""
    parts = [f"<{_TAG}>\n{summary.strip()}\n</{_TAG}>", SUMMARY_NOTE]
    names = attachment_names(messages[1:cut])
    if names:
        parts.append(ATTACHMENTS_NOTE.format(names="、".join(names)))
    return [
        messages[0],
        {"role": "user", "content": "\n\n".join(parts)},
        {"role": "assistant", "content": SUMMARY_ACK},
        *messages[cut:],
    ]


def attachment_names(messages: list[dict]) -> list[str]:
    """一段历史里用户发过的附件，每个一个简短的称呼。

    附件的原件（图片、PDF 的数据，文件的全文）不会留在压缩后的历史里，只留下这样一句提到它们的话。
    PDF 和文本文件在历史里带着文件名；图片只有数据、没有文件名，只能数一数有几张。
    """
    names: list[str] = []
    images = 0
    for message in messages:
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue  # 只有文字的消息，content 是一段字符串，里面没有附件
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image_url":
                images += 1
            elif part.get("type") == "file":
                names.append(str((part.get("file") or {}).get("filename") or "PDF"))
            elif part.get("type") == "text":
                found = _ATTACHMENT_NAME.match(str(part.get("text", "")))
                if found:
                    names.append(found.group(1))
    if images:
        names.append(f"图片 {images} 张")
    return names
