"""定时任务和重复任务：任务是什么、什么时候该运行、存在哪里。

两种任务：

  agent     到时间后，助手自己跑一轮：把"指令"当成一条用户消息，像平常一样调用模型和工具。
            每次运行都要花 API 费用（每一次花了多少都记下来，见 webui/scheduler.py）。
  reminder  到时间后，只在页面上弹出一条提醒。不调用模型，不花钱。

agent 任务还记着"要不要先问你"（approval）：和对话里的几档同名、同义（ask / readonly / edits / all，见 permissions.py 的 MODES），
默认 ask。只有你在页面上编辑任务时能放宽；模型用工具添加的一律是 ask（见 tools/tasks.py）。
还记着命令的沙箱（sandbox）：default 跟随设置 / off / workspace / network（见 sandbox/），默认 default。
模型添加的任务一律是 default，工具没有这个参数：要不要给一个任务关掉沙箱，只能由你在页面上决定。

时间表，全部按这台电脑的本地时间：

  {"type": "once", "at": "2026-11-21T09:00"}             只运行一次
  {"type": "daily", "time": "09:00"}                      每天
  {"type": "weekly", "days": [1, 3, 5], "time": "09:00"}  每周的这几天（1 是星期一，7 是星期日）
  {"type": "monthly", "day": 31, "time": "09:00"}         每月的这一天；没有这一天的月份（比如 2 月）在最后一天
  {"type": "interval", "minutes": 90}                     每隔多久一次，从创建（或者上一次运行）的时刻算起，
                                                          最短 5 分钟，最长 7 天

为什么要强调"本地时间"：用户说"每天 9 点"，指的是他墙上的钟。夏令时开始和结束的那天，墙上的钟会跳过一个小时、
或者把一个小时走两遍，按"距离上一次正好 24 小时"去算就会差一个小时。所以这里每次都按日历重新算
"下一个 9 点是哪一刻"（见 next_after），跳过去的时刻顺延到跳过之后，走两遍的时刻只算第一遍。

这个文件只管"是什么、什么时候"。到时间之后怎么排队、怎么执行、怎么显示进度，在 webui/scheduler.py：
任务只在网页界面开着的时候运行，终端界面不运行它们。
"""

from __future__ import annotations

from .tasks_check import (
    KINDS,
    MAX_INTERVAL_MINUTES,
    MAX_PROMPT_CHARS,
    MAX_TITLE_CHARS,
    MIN_INTERVAL_MINUTES,
    TaskError,
    TaskRefused,
    check_schedule,
    check_task,
)
from .tasks_records import STATUSES, UNFINISHED, new_id, new_run, retell, said
from .tasks_schedule import (
    MAX_COUNTED,
    PAST,
    WEEKDAY_NAMES,
    chinese,
    count_due,
    describe,
    local_moment,
    local_text,
    next_after,
    preview,
    upcoming,
)
from .tasks_store import (
    FORMAT_VERSION,
    INTERRUPTED,
    MAX_REMINDERS,
    MAX_RUNS,
    MAX_TASKS,
    TaskStore,
)

__all__ = [
    "FORMAT_VERSION",
    "INTERRUPTED",
    "KINDS",
    "MAX_COUNTED",
    "MAX_INTERVAL_MINUTES",
    "MAX_PROMPT_CHARS",
    "MAX_REMINDERS",
    "MAX_RUNS",
    "MAX_TASKS",
    "MAX_TITLE_CHARS",
    "MIN_INTERVAL_MINUTES",
    "PAST",
    "STATUSES",
    "UNFINISHED",
    "WEEKDAY_NAMES",
    "TaskError",
    "TaskRefused",
    "TaskStore",
    "check_schedule",
    "check_task",
    "chinese",
    "count_due",
    "describe",
    "local_moment",
    "local_text",
    "new_id",
    "new_run",
    "next_after",
    "preview",
    "retell",
    "said",
    "upcoming",
]
