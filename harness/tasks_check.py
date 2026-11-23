from __future__ import annotations

import re
from datetime import datetime
from typing import TypeGuard

from .i18n import t
from .sandbox import CHOICES as SANDBOX_CHOICES
from .sandbox import DEFAULT as SANDBOX_DEFAULT

KINDS = ("agent", "reminder")
MAX_TITLE_CHARS = 80
MAX_PROMPT_CHARS = 10_000
MIN_INTERVAL_MINUTES = 5  # 再短的话，一天就是几百轮对话、几百段会话
MAX_INTERVAL_MINUTES = 7 * 24 * 60

_TIME = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
_AT = re.compile(r"(\d{4})-(\d{2})-(\d{2})T([01]\d|2[0-3]):([0-5]\d)")
# 每种时间表要哪几项（除了 type）
_FIELDS = {"once": {"at"}, "daily": {"time"}, "weekly": {"days", "time"}, "monthly": {"day", "time"}, "interval": {"minutes"}}


class TaskError(Exception):
    """任务的内容不对（时间表写错了、名称是空的……）。错误信息是用 t() 写的、可以直接给用户看的话。"""


class TaskRefused(TaskError):
    """内容没问题，但现在做不了：任务不存在、已经在运行了、还没有密钥……"""


# ---- 检查 --------------------------------------------------------------------


def check_schedule(value: object) -> dict:
    """检查一份时间表，返回整理好的样子（多余的空值去掉，星期几排好序）。不对的话抛出 TaskError。"""
    if not isinstance(value, dict):
        raise TaskError(t("时间表的格式不对。"))
    fields = {key: item for key, item in value.items() if item is not None}  # 模型常把用不到的项填成 null
    kind = fields.pop("type", None)
    if not isinstance(kind, str) or kind not in _FIELDS:
        raise TaskError(t("重复方式（type）只能是 once、daily、weekly、monthly、interval 之一。"))
    extra = sorted(set(fields) - _FIELDS[kind])
    if extra:
        # 多出来的项多半说明写的人想要的是另一种时间表（比如 daily 却给了 days），与其悄悄丢掉，不如说出来
        raise TaskError(t("“{type}”这种时间表用不到 {fields}。", type=kind, fields=", ".join(map(str, extra))))
    if kind == "once":
        return {"type": "once", "at": _check_at(fields.get("at"))}
    if kind == "interval":
        minutes = fields.get("minutes")
        if not _is_int(minutes) or not MIN_INTERVAL_MINUTES <= minutes <= MAX_INTERVAL_MINUTES:
            raise TaskError(
                t("间隔要在 {min} 分钟到 {max} 分钟（7 天）之间。", min=MIN_INTERVAL_MINUTES, max=MAX_INTERVAL_MINUTES)
            )
        return {"type": "interval", "minutes": minutes}
    clock = _check_time(fields.get("time"))
    if kind == "daily":
        return {"type": "daily", "time": clock}
    if kind == "weekly":
        days = fields.get("days")
        if not isinstance(days, list) or not days or not all(_is_int(day) and 1 <= day <= 7 for day in days):
            raise TaskError(t("每周重复要选至少一天：1 是星期一，7 是星期日。"))
        return {"type": "weekly", "days": sorted(set(days)), "time": clock}
    day = fields.get("day")
    if not _is_int(day) or not 1 <= day <= 31:
        raise TaskError(t("每月的日期要在 1 到 31 之间。没有这一天的月份在最后一天运行。"))
    return {"type": "monthly", "day": day, "time": clock}


def _is_int(value: object) -> TypeGuard[int]:  # TypeGuard：返回真时 mypy 就知道它是 int，后面拿它比大小不用再查
    return isinstance(value, int) and not isinstance(value, bool)  # Python 里 True 也算 int，这里要排除


def _check_time(value: object) -> str:
    if not isinstance(value, str) or not _TIME.fullmatch(value):
        raise TaskError(t("时间要写成 24 小时制的 时:分，比如 09:00 或 21:30。"))
    return value


def _check_at(value: object) -> str:
    match = _AT.fullmatch(value) if isinstance(value, str) else None
    try:
        if match is None:
            raise ValueError
        year, month, day, hour, minute = map(int, match.groups())  # _AT 正好五组：年、月、日、时、分
        moment = datetime(year, month, day, hour, minute)  # 2 月 30 日这种不存在的日子在这里报错
        moment.timestamp()
    except (ValueError, OverflowError, OSError):
        raise TaskError(t("日期和时间要写成 2026-11-21T09:00 这样，而且得是真实存在的一天。")) from None
    return match.string  # 就是 value：交给 fullmatch 的那个字符串（对上了，说明它是字符串）


def check_task(value: object) -> dict:
    """检查用户（或者模型）给的一个任务：名称、种类、指令、时间表、是否启用、要不要先问。返回整理好的这几项。"""
    if not isinstance(value, dict):
        raise TaskError(t("参数不正确"))
    title = value.get("title")
    title = " ".join(title.split()) if isinstance(title, str) else ""  # 名称是一行字：换行和连续的空格压成一个
    if not 0 < len(title) <= MAX_TITLE_CHARS:
        raise TaskError(t("任务名称不能为空，最长 {n} 个字。", n=MAX_TITLE_CHARS))
    kind = value.get("kind")
    if kind not in KINDS:
        raise TaskError(t("任务的种类只能是 agent（让助手去做）或 reminder（只是提醒）。"))
    prompt = value.get("prompt")
    prompt = "" if prompt is None else prompt
    if not isinstance(prompt, str) or len(prompt.strip()) > MAX_PROMPT_CHARS:
        raise TaskError(t("内容最长 {n} 个字。", n=MAX_PROMPT_CHARS))
    prompt = prompt.strip()
    if kind == "agent" and not prompt:
        raise TaskError(t("要让助手做什么，写在“指令”里。"))
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise TaskError(t("参数不正确"))
    approval = value.get("approval", "ask")
    if not isinstance(approval, str) or approval not in _approval_modes():
        raise TaskError(t("参数不正确"))
    sandbox = value.get("sandbox", SANDBOX_DEFAULT)
    if not isinstance(sandbox, str) or sandbox not in SANDBOX_CHOICES:
        raise TaskError(t("参数不正确"))
    if kind == "reminder":
        approval = "ask"  # 提醒不调用模型、不用任何工具，没有什么可问的。存成默认值，免得文件里留着一个不起作用的"全部自动允许"
        sandbox = SANDBOX_DEFAULT  # 同样：提醒不运行命令
    schedule = check_schedule(value.get("schedule"))
    return {"title": title, "kind": kind, "prompt": prompt, "schedule": schedule, "enabled": enabled, "approval": approval, "sandbox": sandbox}


def _approval_modes() -> dict:
    """任务可以选的几档"要不要先问你"，就是对话里的那几档。

    定义在 permissions.py（那里讲每一档是什么意思），在这里才导入：permissions.py 也要用这个文件
    （审查添加任务的工具调用），两边都在文件开头导入对方的话，谁先加载谁就拿不到对方。
    """
    from .permissions import MODES

    return MODES
