"""定时任务 · 一次运行的记录、一条提醒（每个字段的意思见 webui/scheduler.py 开头）。

  STATUSES、UNFINISHED   一次运行可能处于的状态
  new_run                新的一次运行的记录；new_id 是任务、运行、提醒共用的编号
  said                   给人看的话连同原文（key、values）一起存下，换了语言还能重新译（retell，写在 i18n.py 里）
  _restore_run、_restore_reminder  从 data/tasks.json 读回来的时候一条条检查：坏掉的那一条丢掉，别的照常用

为什么单独一个文件：记录长什么样，TaskStore 和 webui/scheduler.py 都要用；读回来的检查又和锁、存盘无关。
"""

from __future__ import annotations

import math
import secrets
from typing import TypeGuard

from .i18n import Message
from .tasks_check import _is_int

# 一次运行在保存的记录里可能处于的状态。
#   waiting  还没开始：在排队（同时运行的任务到了上限，见 config.py 的 MAX_PARALLEL_TASKS），
#            或者已经开始了、正停在一个要你批准的操作上
#   running  正在运行          done     做完了          failed   出错了（原因在 error 里）
#   stopped  被停下了（你点了停止，或者程序退出了）
#   missed   保留给"错过了、没有补上"的情况。现在错过的会补跑一次（见 webui/scheduler.py），所以还用不到
STATUSES = ("running", "waiting", "done", "failed", "stopped", "missed")
UNFINISHED = ("running", "waiting")


def new_id() -> str:
    return secrets.token_hex(4)


def new_run(now: float, status: str, *, catch_up: bool = False, missed: int = 0, manual: bool = False) -> dict:
    """一次运行的记录。字段的意思见 webui/scheduler.py 开头的说明。"""
    return {
        "id": new_id(),
        "status": status,
        "started_at": now,
        "finished_at": None,
        "cost": None,
        "steps": 0,
        "catch_up": catch_up,
        "missed": missed,
        "manual": manual,
        "conversation": "",
        "summary": "",
        "error": "",
    }


def said(text: str) -> dict:
    """一句给用户看的话，存进记录时连同原文一起存（error_key、error_values），换了语言还能重新译。"""
    if isinstance(text, Message):
        return {"error": str(text), "error_key": text.key, "error_values": text.values}
    return {"error": str(text), "error_key": "", "error_values": {}}


def _brief(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _is_time(value: object) -> TypeGuard[float]:  # 返回真时 mypy 就知道它是数，可以交给 float()
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _restore_run(raw: object) -> dict | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or raw.get("status") not in STATUSES:
        return None
    if not _is_time(raw.get("started_at")):
        return None
    run = new_run(float(raw["started_at"]), raw["status"])
    run["id"] = raw["id"]
    finished, cost = raw.get("finished_at"), raw.get("cost")
    run["finished_at"] = float(finished) if _is_time(finished) else None
    run["cost"] = float(cost) if _is_time(cost) else None
    for key in ("steps", "missed"):
        run[key] = raw[key] if _is_int(raw.get(key)) and raw[key] >= 0 else 0
    for key in ("catch_up", "manual"):
        run[key] = raw.get(key) is True
    for key in ("conversation", "summary", "error", "error_key"):
        if isinstance(raw.get(key), str):
            run[key] = raw[key]
    values = raw.get("error_values")
    if run.get("error_key") and isinstance(values, dict) and all(isinstance(value, (str, int, float)) for value in values.values()):
        run["error_values"] = values
    elif run.get("error_key"):
        run["error_key"] = ""  # 原文的值对不上，就只用存下来的那句话，不重新译了
    return run


def _restore_reminder(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    texts = all(isinstance(raw.get(key), str) for key in ("id", "task_id", "title", "text"))
    if not texts or not _is_time(raw.get("at")):
        return None
    return {
        "id": raw["id"],
        "task_id": raw["task_id"],
        "title": raw["title"],
        "text": raw["text"],
        "at": float(raw["at"]),
        "catch_up": raw.get("catch_up") is True,
    }
