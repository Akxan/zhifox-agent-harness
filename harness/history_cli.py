"""终端里 /history 和 /open 写出来的那几行：只管怎么写，不碰会话（存盘、打开在 chat_cli.py）。

和 trace/text.py、model_catalog.lines 一样，每个函数交回一串（文字，样子），样子是 plain（正常）或者 info（灰色），
终端照着写。网页上的会话栏、搜索结果、打开以后的样子由页面自己画（见 webui/page/scripts/），这里是同样的内容在终端里的说法：
列出来的会话、搜索的结果（和网页用的是同一个索引，见 search.py），打开以后的最后几条消息。
"""

from __future__ import annotations

import time
from collections.abc import Sequence

from .channels.platforms import label as platform_label
from .config import APP_NAME, TERMINAL_RECAP_CHARS, TERMINAL_RECAP_MESSAGES
from .conversations import Conversation
from .display import short
from .i18n import t
from .providers import find

Lines = list[tuple[str, str]]


def when(moment: float) -> str:
    """一个时刻，按这台电脑的本地时间写：2026-11-23 14:03（和语言无关，三种语言都看得懂）。"""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(moment))


def model_of(provider_id: str, model: str) -> str:
    """"Claude · claude-opus-5-5"：哪一家、哪个模型。这一家不在了（自定义接口被删掉了）就写它的编号。"""
    provider = find(provider_id)
    return f"{provider.label if provider is not None else provider_id} · {model}"


def _marks(item: dict) -> str:
    """网页会话栏上的那几个小标记（见 page/scripts/conversations.js）：定时任务跑出来的、在哪个聊天通道里聊的、
    用哪个 Agent 当角色的（写它的名字）、分叉出来的。"""
    marks: list[str] = []
    if item.get("task"):
        marks.append(t("定时任务"))
    if item.get("channel"):
        marks.append(platform_label(item["channel"]))
    if item.get("agent"):
        marks.append(item["agent"])
    if item.get("forked"):
        marks.append(t("分叉出来的"))
    return "".join(f" [{mark}]" for mark in marks)


def _row(number: int, item: dict, current: str) -> Lines:
    """列表里的一段：第一行编号和标题（正开着的那一段标出来），第二行灰色的模型和时间。"""
    here = t("（正开着）") if item["id"] == current else ""
    return [
        (f"{number:>3}. {item['title']}{_marks(item)}{here}", "plain"),
        (f"     {model_of(item.get('provider', ''), item.get('model', ''))} · {when(item.get('updated_at', 0))}", "info"),
    ]


def listing(items: Sequence[dict], current: str) -> Lines:
    """最近的会话（/history）：新的在前。current 是正开着的那一段的编号。"""
    if not items:
        return [(t("还没有保存过的会话。聊上一句，这段对话就会存下来。"), "info")]
    lines: Lines = [(t("最近的会话（/open 编号 打开；/history 加上词是搜索）："), "info")]
    for number, item in enumerate(items, 1):
        lines += _row(number, item, current)
    return lines


def found(results: Sequence[dict], query: str, current: str) -> Lines:
    """搜索的结果（/history 加上词）：最相关的在前，每一段下面再写一行命中的那一小段（只有标题对上的没有）。"""
    if not results:
        return [(t("没有找到含有“{query}”的会话。正在进行的这一段要等第一轮存了盘才搜得到。", query=query), "info")]
    lines: Lines = [(t("含有“{query}”的会话（/open 编号 打开）：", query=query), "info")]
    for number, item in enumerate(results, 1):
        lines += _row(number, item, current)
        snippet = item.get("snippet")
        if isinstance(snippet, dict) and isinstance(snippet.get("text"), str):
            lines.append((f"     {short(snippet['text'], TERMINAL_RECAP_CHARS)}", "info"))
    return lines


def messages(events: Sequence[dict]) -> list[tuple[str, str]]:
    """一段会话里说过的话，按先后：（谁，说了什么），谁是 user 或者 assistant。只看你的消息和助手的回答：
    工具卡片、确认、思考这些只在网页上画。助手一轮里写的几段（中间调用了工具）合成一条，你发的附件写上名字。"""
    said: list[list[str]] = []
    reply: list[str] | None = None
    for event in events:
        kind = event.get("type") if isinstance(event, dict) else None
        if kind == "user":
            reply = None
            words = event["text"] if isinstance(event.get("text"), str) else ""
            names = [item["name"] for item in event.get("attachments") or [] if isinstance(item, dict) and isinstance(item.get("name"), str)]
            if names:
                words = f"{words} " + t("（附件：{names}）", names=t("、").join(names))
            said.append(["user", words])
        elif kind == "text" and isinstance(event.get("text"), str):
            if reply is None:
                reply = ["assistant", ""]
                said.append(reply)
            reply[1] += event["text"]
        elif kind == "tool_call" and reply is not None:
            reply[1] += "\n"  # 调用工具前后是两段话
    return [(who, words) for who, words in said if words.strip()]


def recap(conversation: Conversation, events: Sequence[dict]) -> Lines:
    """/open 打开以后写的几行：打开的是哪一段（标题、模型、最后聊的时间），最后几条消息，让你知道聊到哪儿了。"""
    model = model_of(conversation.provider, conversation.model)
    title = conversation.title or t("新对话")
    lines: Lines = [(t("已打开：{title}（{model}，{time}）", title=title, model=model, time=when(conversation.updated_at)), "plain")]
    shown = messages(events)
    earlier = len(shown) - TERMINAL_RECAP_MESSAGES
    if earlier > 0:
        lines.append((t("  ……前面还有 {n} 条消息", n=earlier), "info"))
    for who, words in shown[-TERMINAL_RECAP_MESSAGES:]:
        name = t("你") if who == "user" else APP_NAME
        lines.append((t("  {who}：{words}", who=name, words=short(words, TERMINAL_RECAP_CHARS)), "info" if who == "user" else "plain"))
    lines.append((t("接着输入就能继续这段对话。"), "info"))
    return lines
