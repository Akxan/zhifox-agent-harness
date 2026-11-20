"""定时任务的工具：让用户一句话就能定下"每天早上 9 点提醒我喝水"。

和待办（todo.py）一样是完全自定义的工具：说明书是我们写的，执行的函数也是我们写的。
任务本身怎么保存、时间怎么算，在 ../tasks.py；到时间之后怎么运行，在 ../webui/scheduler.py。

添加和删除"让助手去做"（agent）的任务要先问用户（规则在 ../permissions.py）：这种任务以后每次运行都要花钱，
还会在没人看着的时候自己调用工具，是一件长期有效的事，不能由模型一个人定下来。只是提醒的不花钱，不用问。

模型添加的任务，运行时一律"每次都问"（approval 是 ask），工具也没有能改它的参数：要不要放宽，只能由用户
在页面上编辑任务时自己选。不然被网页上的一句话带偏的模型，就能给自己定一个"到点自动执行任何命令"的任务。
"""

from __future__ import annotations

import json

from ..i18n import Message, fill
from ..tasks import TaskError, TaskStore, chinese, describe, local_text
from .base import Tool, ToolError, function_tool

ADD = "task_add"
LIST = "task_list"
DELETE = "task_delete"

_SCHEDULE = {
    "type": "object",
    "description": (
        "什么时候运行，全部按用户电脑的本地时间。五种写法，按 type 只填对应的几项：\n"
        '只运行一次 {"type": "once", "at": "2026-11-21T09:00"}；\n'
        '每天 {"type": "daily", "time": "09:00"}；\n'
        '每周的某几天 {"type": "weekly", "days": [1, 3, 5], "time": "09:00"}（1=星期一 … 7=星期日）；\n'
        '每月的某一天 {"type": "monthly", "day": 15, "time": "09:00"}（没有这一天的月份在最后一天运行）；\n'
        '每隔一段时间 {"type": "interval", "minutes": 90}（从现在开始数，最短 5 分钟，最长 10080 分钟即 7 天）。\n'
        "用户说的是相对时间（明天、下周一）时，先按消息开头 <context> 里的当前时间换算成具体日期。"
    ),
    "properties": {
        "type": {"type": "string", "enum": ["once", "daily", "weekly", "monthly", "interval"]},
        "at": {"type": "string", "description": "once 用：日期和时间，YYYY-MM-DDTHH:MM"},
        "time": {"type": "string", "description": "daily、weekly、monthly 用：24 小时制的 HH:MM"},
        "days": {"type": "array", "items": {"type": "integer"}, "description": "weekly 用：星期几，1=星期一 … 7=星期日"},
        "day": {"type": "integer", "description": "monthly 用：几号，1 到 31"},
        "minutes": {"type": "integer", "description": "interval 用：每隔多少分钟"},
    },
    "required": ["type"],
}

NOTE = "定时任务只在网页界面（uv run harness --web）开着的时候运行；那时没开着的话，下次打开时补跑一次。"


def _chinese(exc: TaskError) -> str:
    """错误信息的中文原文。写给模型看的话保持中文，不跟着界面语言变（见 CLAUDE.md）。"""
    message = exc.args[0] if exc.args else ""
    if isinstance(message, Message):
        return fill(message.key, message.values)
    return str(message)


def _view(task: dict) -> dict:
    """给模型看的一个任务。时间写成本地时间的文字，它不用自己换算秒数。"""
    view = {
        "id": task["id"],
        "kind": task["kind"],
        "title": task["title"],
        ("prompt" if task["kind"] == "agent" else "text"): task["prompt"],
        "schedule": task["schedule"],
        "when": describe(task["schedule"], say=chinese),
        "enabled": task["enabled"],
        "next_run": None if task["next_run"] is None else local_text(task["next_run"]),
    }
    if task["kind"] == "agent":
        view["approval"] = task["approval"]  # 运行时要确认的操作问不问用户：ask / edits / all（只有用户能改）
    if task["runs"]:
        last = task["runs"][0]
        view["last_run"] = {"at": local_text(last["started_at"]), "status": last["status"], "summary": last["summary"]}
    return view


def _as_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def make_task_tools(store: TaskStore) -> list[Tool]:
    def task_add(kind: str, title: str, schedule: dict, prompt: str = "") -> str:
        try:
            # approval 写死成 ask：模型添加的任务运行时一律每次都问，放宽只能由用户在页面上做（见文件开头的说明）
            fields = {"kind": kind, "title": title, "prompt": prompt, "schedule": schedule, "enabled": True, "approval": "ask"}
            task = store.add(fields)
        except TaskError as exc:
            raise ToolError(_chinese(exc)) from None
        return _as_json({"added": _view(task), "note": NOTE})

    def task_list() -> str:
        tasks, _ = store.snapshot()
        return _as_json({"tasks": [_view(task) for task in tasks], "note": NOTE})

    def task_delete(id: str) -> str:
        try:
            task = store.delete(id)
        except TaskError as exc:
            raise ToolError(_chinese(exc) + "先用 task_list 查看现有的任务。") from None
        return _as_json({"deleted": _view(task)})

    return [
        function_tool(
            name=ADD,
            description=(
                "添加一个定时任务或者重复任务。两种：\n"
                "reminder：到时间只提醒用户一下（在页面上弹出 title 和 prompt 里的话），不调用模型，不花钱。"
                "用户说“提醒我……”时用它。\n"
                "agent：到时间由助手自己跑一轮，把 prompt 当成用户发来的消息去做（比如“看看我的待办，写一份今天的安排”）。"
                "每次运行都要花 API 费用；那时用户多半不在电脑前，所以 prompt 要写成一条完整、不用追问就能做完的指令。"
                "添加 agent 任务会先请用户确认。agent 任务运行时，需要确认的操作（执行命令、改文件……）会停下来等用户批准；"
                "想让它不问就做，只能由用户自己在网页界面的任务列表里编辑这个任务、选择自动允许，你没有办法替用户改，"
                "需要时告诉用户去哪里改。\n"
                "只是要记下一件有截止日期的事、不需要到时间做什么的，用 todo_add。" + NOTE
            ),
            properties={
                "kind": {"type": "string", "enum": ["agent", "reminder"], "description": "agent=到时间让助手去做，reminder=只是提醒"},
                "title": {"type": "string", "description": "任务的名称，简短的一句话，比如“喝水”“每日安排”"},
                "schedule": _SCHEDULE,
                "prompt": {
                    "type": "string",
                    "description": "agent：到时间要助手做什么（必填）。reminder：提醒时显示的话，可以不填（只显示名称）",
                },
            },
            required=["kind", "title", "schedule"],
            function=task_add,
        ),
        function_tool(
            name=LIST,
            description="查看所有定时任务：编号、种类、名称、时间表、下一次运行的时间、最近一次运行的结果。删除任务之前先用它查到编号。",
            properties={},
            required=[],
            function=task_list,
        ),
        function_tool(
            name=DELETE,
            description="删除一个定时任务，以后不再运行。删除 agent 任务会先请用户确认。",
            properties={"id": {"type": "string", "description": "任务的编号（task_list 里的 id）"}},
            required=["id"],
            function=task_delete,
        ),
    ]
