from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..agent import Agent
from ..config import APP_NAME
from ..i18n import k, t
from ..pricing import estimate
from ..tasks import chinese, describe, local_text, retell
from .ui import WebUI

if TYPE_CHECKING:
    from ..tools.shell import Shell

QUEUED = k("排队中：同时最多运行 {n} 个任务")


class RunUI(WebUI):
    """一次运行用的界面。

    和对话的界面是同一种（WebUI）：事件记成日志，结束时整理成会话里保存的样子（export_events），
    "停止"、"等确认"的做法也一样。区别是它的日志不推给页面，只是每发一个事件，顺便告诉调度器一声，
    调度器据此更新进度（正在想、正在写、正在调用哪个工具、停在哪个确认上）。
    """

    def __init__(self, observe: Callable[[str, dict, int], None]):
        super().__init__()
        self._observe = observe
        self.answer = ""  # 最后一次调用工具之后写的文字，就是这一轮的回答
        self.failure: str | None = None  # 出错时显示的那句话
        self.last_info: str | None = None  # 最后一句提示（比如"模型拒绝了这个请求"）

    def publish(self, type: str, **data: Any) -> int:
        event_id = super().publish(type, **data)
        if type == "tool_call":
            self.answer = ""
        elif type == "text":
            self.answer += data["text"]
        self._observe(type, data, event_id)
        return event_id

    def error(self, text: str) -> None:
        self.failure = text
        super().error(text)

    def info(self, text: str) -> None:
        self.last_info = text
        super().info(text)

    @property
    def stopped(self) -> bool:
        return self._stop_requested


@dataclass
class Pending:
    """排着队、或者正在运行的一次。"""

    task_id: str
    run_id: str
    title: str
    queued_at: float
    due: float | None  # 原定的时间；立即运行的是 None
    catch_up: bool
    missed: int
    manual: bool
    stop_requested: bool = False
    started_at: float | None = None  # 轮到它、开始运行的时刻；还在排队是 None
    ui: RunUI | None = None
    agent: Agent | None = None
    shell: Shell | None = None  # 这次运行自己的 shell（见 App.unattended_agent）
    tool: str = ""  # 正在调用的工具
    activity: dict = field(default_factory=lambda: {"kind": "starting", "tool": "", "summary": ""})
    log: list = field(default_factory=list)
    confirm: dict | None = None


def _live_view(pending: Pending, prices: dict, max_steps: int, limit: int) -> dict:
    """状态里的 running：一次排队或者正在运行的情况。调用的人要拿着 Scheduler._lock。

      run_id、started_at（开始的时刻；还在排队的是排进来的时刻）、step（调用了几次模型）、max_steps、
      cost（到现在为止的估算费用，美元；模型没有价格时是 null）、
      activity（在做什么：kind 是 waiting 排队或等你、starting 正在准备、thinking、writing、tool；
               tool 是正在调用的工具；summary 是给人看的一句话）、
      log（最近的工具调用：tool、summary、ok 成功没有（还没结果是 null）、auto 是不是没问你就放行的）、
      confirm（停着等你答复的那个确认：id、tool、summary；没有是 null）
    """
    if pending.started_at is None:  # 还在排队：同时运行的到了上限，等前面的跑完一个
        activity = {"kind": "waiting", "tool": "", "summary": t(QUEUED, n=limit)}
        return {"run_id": pending.run_id, "started_at": pending.queued_at, "step": 0, "max_steps": max_steps, "cost": 0.0,
                "activity": activity, "log": [], "confirm": None}
    if pending.ui is None or pending.agent is None:  # 轮到它了，正在准备（读任务、建适配层）
        return {"run_id": pending.run_id, "started_at": pending.started_at, "step": 0, "max_steps": max_steps, "cost": 0.0,
                "activity": {"kind": "starting", "tool": "", "summary": ""}, "log": [], "confirm": None}
    agent = pending.agent
    total, unpriced = estimate(dict(agent.usage_by_model), prices)
    confirm = pending.confirm
    return {
        "run_id": pending.run_id,
        "started_at": pending.started_at,
        "step": agent.usage.requests,
        "max_steps": max_steps,
        "cost": None if unpriced else round(total, 6),
        "activity": {**pending.activity, "summary": retell(pending.activity["summary"])},
        "log": [dict(entry) for entry in pending.log],
        # 确认卡片上的那句话按现在的界面语言重新译（你可能在它停着的时候换了语言）
        "confirm": None if confirm is None else {"id": confirm["id"], "tool": confirm["tool"], "summary": retell(confirm["summary"])},
    }


def instruction(task: dict, pending: Pending) -> str:
    """发给模型的那条消息：任务的指令，前面加一段说明。写给模型看的，所以是中文、不翻译。

    说明告诉模型三件事：这一轮不是用户刚刚打的字；用户多半不在，别停下来提问；哪些操作会等用户回来批准
    （按这个任务的那一档说实话：放开了的，模型要知道这一步不会再有人把关）。
    补跑的还要说清楚原定是什么时候，不然"看看今天上午有什么安排"在下午补跑时，模型会按现在的时间去理解。
    """
    when = describe(task["schedule"], say=chinese)
    if pending.manual:
        how = "用户刚刚在任务列表里点了“立即运行”"
    elif pending.catch_up and pending.due is not None:
        how = f"原定 {local_text(pending.due)} 运行，那时{APP_NAME}没有开着，现在补上一次"
        if pending.missed > 1:
            how += f"（这期间一共错过了 {pending.missed} 次，只补这一次）"
    else:
        how = "到了预定的时间，由程序自动发起"
    return (
        f"<task>这一轮是定时任务“{task['title']}”（{when}），{how}。用户此刻可能不在电脑前："
        "不要停下来问他问题，按下面的指令把事情做完，最后用几句话交代做了什么、结果怎样。"
        f"{APPROVAL_NOTES.get(task.get('approval', 'ask'), APPROVAL_NOTES['ask'])}</task>\n\n" + task["prompt"]
    )


# 按任务的那一档，告诉模型哪些操作会停下来等用户（写给模型看的，中文、不翻译）
APPROVAL_NOTES = {
    "ask": "需要用户确认的操作会停在那里，等他回来批准。",
    "readonly": "这个任务设置成了只读的命令不用问：只是看的命令（ls、cat、grep、df、git status……）和读文件会直接执行；会改动东西的命令、改文件等别的需要确认的操作会停在那里，等他回来批准。",
    "edits": "这个任务设置成了改工作目录里的文件不用问：改文件会直接执行；执行命令等别的需要确认的操作会停在那里，等他回来批准。",
    "all": "这个任务设置成了全部自动允许：执行命令、改文件都会直接执行，不会再有人确认，所以只做指令要求的事，拿不准的不要做。",
}
