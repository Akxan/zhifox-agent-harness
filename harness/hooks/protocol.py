"""钩子说了什么：退出码和标准输出怎么解释。约定和 Claude Code 的一样（见 __init__.py 的"钩子怎么回答"）。

    退出码 0   成功。标准输出是一个 JSON 对象的话，照着里面的字段办；不是 JSON 的话，只有 UserPromptSubmit
               把它当成附给模型的说明（和 Claude Code 一样），别的事件不看
    退出码 2   "拦下"：标准错误就是原因。PreToolUse 拦下这次调用（原因交给模型），UserPromptSubmit 拦下这条消息
               （原因给你看）；PostToolUse 的工具已经做完了，原因接在结果后面交给模型；Stop 只是通知，原因给你看
    别的退出码  钩子自己出错了：给你看一句提醒，照常继续（onFailure 设成 block 的话当成拦下）。超时、启动不了也一样

这里只解释，不运行、不碰界面：输入是一次运行的经过（runner.Ran），输出是 Outcome，由 engine.py 照着办。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..config import HOOK_NOTE_CHARS, HOOK_REASON_CHARS
from ..i18n import Message, fill, k, t
from ..safe import clean_text
from .config import BLOCKING
from .runner import Ran

NO_REASON = "（钩子没有说明原因）"  # 写给模型、也给你看的；给你看时由 t() 译


@dataclass
class Outcome:
    """一个钩子这一次的结论。

    result   ok 放行（可能附了话）/ blocked 拦下 / ask 要你再确认一次（只有 PreToolUse）/
             error 钩子出错了 / timeout 超时了 / stopped 你点了停止
    reason   拦下、要你确认的原因（钩子自己写的话，原样）
    context  交给模型的补充：UserPromptSubmit 附在你的消息里，PostToolUse 接在工具结果后面（已经截短）
    message  给你看的一句话（systemMessage，或者 Stop 退出码 2 时的标准错误）
    problem  钩子出错的原因（t() 译好的，给你看）
    ignored  钩子说了、但这里不照办的（比如 allow：钩子只能让确认更严，不能放宽），试运行时告诉你
    """

    result: str
    reason: str = ""
    context: str = ""
    message: str = ""
    problem: str = ""
    ignored: list[str] = field(default_factory=list)


def cap(text: str, limit: int) -> str:
    text = clean_text(text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def chinese(text: str) -> str:
    """一句 t() 译好的话的中文原文（写给模型看的保持中文，见 CLAUDE.md 的多语言一节）。"""
    return fill(text.key, text.values) if isinstance(text, Message) else str(text)


def interpret(event: str, ran: Ran, on_failure: str = "continue", timeout: float = 0) -> Outcome:
    if ran.failure == "stopped":
        return Outcome("stopped")
    if ran.failure == "timeout":
        return _failed(event, "timeout", t("超过 {n} 秒还没结束，已经结束掉了", n=_seconds(timeout)), on_failure)
    if ran.failure == "start":
        return _failed(event, "error", ran.problem, on_failure)
    if ran.exit == 2:
        reason = cap(ran.stderr, HOOK_REASON_CHARS)
        if event in BLOCKING:
            return Outcome("blocked", reason=reason or NO_REASON)
        if event == "PostToolUse":
            return Outcome("ok", context=cap(ran.stderr, HOOK_NOTE_CHARS))
        return Outcome("ok", message=reason)
    if ran.exit != 0:
        said = _first_line(ran.stderr) or _first_line(ran.stdout)
        problem = t("退出码 {code}：{line}", code=ran.exit, line=said) if said else t("退出码 {code}", code=ran.exit)
        return _failed(event, "error", problem, on_failure)
    text = ran.stdout.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError as exc:
            return _failed(event, "error", t("输出看起来是 JSON，但解析不了：{error}", error=exc), on_failure)
        if isinstance(data, dict):
            return _from_json(event, data)
    if event == "UserPromptSubmit" and text:
        return Outcome("ok", context=cap(text, HOOK_NOTE_CHARS))
    return Outcome("ok")


def _failed(event: str, result: str, problem: str, on_failure: str) -> Outcome:
    """钩子自己出了错。默认当它没说话；设了 onFailure: block 的、能拦下的事件当成拦下（管安全的钩子坏了时宁可不做）。"""
    if on_failure == "block" and event in BLOCKING:
        return Outcome("blocked", reason=f"钩子没能正常运行（{chinese(problem)}），按设置当成拦下。", problem=problem)
    return Outcome(result, problem=problem)


def _from_json(event: str, data: dict) -> Outcome:
    """退出码 0、标准输出是 JSON 对象。字段和 Claude Code 的一样：decision / reason、hookSpecificOutput、systemMessage。"""
    specific = data.get("hookSpecificOutput")
    if not isinstance(specific, dict) or specific.get("hookEventName", event) != event:
        specific = {}
    message = cap(_text(data.get("systemMessage")), HOOK_REASON_CHARS)
    decision, reason = data.get("decision"), _text(data.get("reason"))
    ignored: list[str] = []
    if data.get("continue") is False:
        ignored.append(k("continue: false 在智狐里不认：钩子只能拦下一次调用或一条消息，不能停下整个助手"))
    if event == "PreToolUse":
        permission = specific.get("permissionDecision")
        why = _text(specific.get("permissionDecisionReason")) or reason
        if permission == "deny" or (permission is None and decision in ("block", "deny")):
            return Outcome("blocked", reason=cap(why, HOOK_REASON_CHARS) or NO_REASON, message=message, ignored=ignored)
        if permission == "ask":
            return Outcome("ask", reason=cap(why, HOOK_REASON_CHARS), message=message, ignored=ignored)
        if permission == "allow" or decision == "approve":
            ignored.append(k("allow 不会放宽确认：钩子只能让确认更严，要不要问你照常按确认的那一档"))
        if "updatedInput" in specific:
            ignored.append(k("updatedInput 不认：钩子不能改工具的参数"))
        return Outcome("ok", message=message, ignored=ignored)
    if event == "UserPromptSubmit":
        if decision == "block":
            return Outcome("blocked", reason=cap(reason, HOOK_REASON_CHARS) or NO_REASON, message=message, ignored=ignored)
        return Outcome("ok", context=cap(_text(specific.get("additionalContext")), HOOK_NOTE_CHARS), message=message, ignored=ignored)
    if event == "PostToolUse":
        notes = [reason if decision == "block" else "", _text(specific.get("additionalContext"))]
        if "updatedToolOutput" in specific or "updatedMCPToolOutput" in specific:
            ignored.append(k("updatedToolOutput 不认：钩子只能在结果后面补充，不能改掉工具的结果"))
        return Outcome("ok", context=cap("\n".join(note for note in notes if note), HOOK_NOTE_CHARS), message=message, ignored=ignored)
    if decision == "block":
        ignored.append(k("Stop 钩子只是通知：decision: block 不会让这一轮接着做"))
    return Outcome("ok", message=message, ignored=ignored)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _first_line(text: str) -> str:
    line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return cap(line, 200)


def _seconds(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"
