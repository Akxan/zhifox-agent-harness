"""定时任务在网页界面里怎么运行：什么时候醒、怎么排队、怎么跑、怎么显示进度。

任务是什么、时间怎么算、存在哪里，在 ../tasks.py。这里管的是"到时间之后"：

    调度线程 ──到时间了──► 提醒：马上弹出来（不调用模型，不花钱，不用排队）
                      └──► agent 任务：排进队里 ──有空位就开始──► 它自己的线程跑一轮 ──► 存成一段会话
                                          （同时最多跑 MAX_PARALLEL_TASKS 个，见 config.py）

为什么只在网页界面里运行：任务要在你没看着的时候自己跑，跑的过程要能看到、要你批准的操作要能点，
这些都需要一个一直开着、又能显示东西的地方。终端界面在你打字时一直等着输入，没法同时做这些。

几条规矩，以及为什么：

  和你的对话同时进行，谁也不等谁。
    任务不拿对话的那把锁（WebApp._turn）：它跑的时候，你照常发消息、换模型、打开会话；几个任务也可以一起跑。
    能这样做，是因为凡是"会被停下"的东西，每次运行都有自己的一份（见 App.unattended_agent）：
      - 自己的 shell：停下一个任务，只结束它自己正在运行的命令。你点对话里的"停止"碰不到任务，
        停下一个任务也碰不到你的对话和别的任务；
      - 自己的界面（RunUI，见下面）、适配层和历史、权限。
    共用的只有存在磁盘上的东西：待办、任务、记忆、会话、工作目录里的文件。它们各自有锁（TodoStore、TaskStore、
    ConversationStore、tools/files.py、tools/memory.py），每次改动一口气做完，几边同时改也不会互相冲掉。
    要是两边改的是同一个文件的同一处，后改的那一边会发现原文已经变了（str_replace 找不到原文就报错），
    模型看到错误会重新读一遍再改。
    同一个任务不会同时跑两次：它还在排队或者正在跑的时候又到了时间，这一次就算在它头上。

  同时跑的有上限。
    每个运行都在调用模型、花钱，还可能开着命令，一下子跑太多既费钱又盯不过来。所以最多同时跑
    MAX_PARALLEL_TASKS 个（config.py），再到时间的先排着队，页面上写着"排队中"；前面的跑完一个，
    排在最前面的那个就开始。你自己的对话不算在里面。

  不打扰你正在看的对话。
    任务用的是新建的适配层和空的历史，显示的事件记在它自己的日志里（RunUI），不进页面上那段对话的日志。
    跑完存成一段普通的会话，出现在会话列表里，可以打开、重放、接着聊。

  要不要问你，每个任务自己定。
    每个任务记着一档"要不要先问你"（approval：ask / readonly / edits / all，和对话里的几档同名、同义，见 ../permissions.py），
    默认是"每次确认"：要你确认的操作（执行命令、改文件……），任务会停下来等你。停着的那个确认放在状态里
    （tasks[].running.confirm），页面上能看到、能答复（/api/task/confirm）。你在对话里换的那一档碰不到任务。
    放宽只能由你在页面上编辑任务时选；模型用工具添加的任务一律是"每次确认"，它自己改不了（见 ../tools/tasks.py）。
    放宽了的，没问你就放行的那一步照样记下来：会话里标着"自动允许"，进度里那一行的 auto 是 true。

  错过了就补一次。
    程序没开着（或者电脑睡着了）的时候到了时间的，下一次看到时补跑一次，几次错过合成一次，
    记录上标着 catch_up，并记下错过了几次（missed）。

  不设花钱的上限，但每次花了多少都记下来（按这一次自己的用量估算，见 pricing.py）。

状态里每个任务带着 runs（最近的运行，新的在前）和 running（正在排队或者正在运行的那一次，没有就是 null）。
一次运行（runs 里的一项）有这些字段：

  id、status（running / waiting / done / failed / stopped，含义见 ../tasks.py 的 STATUSES）、
  started_at、finished_at（还没结束是 null）、cost（美元；有没有价格的模型、或者只是提醒时是 null）、
  steps（调用了几次模型）、catch_up（是不是补跑的）、missed（补跑时错过了几次）、manual（是不是你点了"立即运行"）、
  conversation（存下来的会话编号，没有就是空字符串）、summary（最后的回答的开头）、error（出错的原因）。

running 的字段见 _live_view。里面给人看的话（activity.summary、log[].summary、confirm.summary）都是写好的整句，
已经说清楚了是哪个工具（比如"run_command $ ls"、"执行命令：$ ls"），页面原样显示，不再在前面加工具的名字。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from .scheduler_actions import SchedulerActions
from .scheduler_loop import LATE_SECONDS, MAX_SLEEP, MIN_SLEEP, SchedulerLoop
from .scheduler_run import (
    APPROVAL_NOTES,
    QUEUED,
    Pending,
    RunUI,
    _live_view,
    instruction,
)
from .scheduler_work import (
    MAX_LOG,
    SUMMARY_CHARS,
    TITLE_CHARS,
    WAIT_CONFIRM,
    SchedulerWork,
)

if TYPE_CHECKING:
    from .server import WebApp

__all__ = [
    "APPROVAL_NOTES",
    "LATE_SECONDS",
    "MAX_LOG",
    "MAX_SLEEP",
    "MIN_SLEEP",
    "PUBLISH_INTERVAL",
    "QUEUED",
    "SUMMARY_CHARS",
    "TITLE_CHARS",
    "WAIT_CONFIRM",
    "Pending",
    "RunUI",
    "Scheduler",
    "Throttle",
    "instruction",
]

PUBLISH_INTERVAL = 0.25  # 运行时的进度最多每秒刷新四次，免得逐字输出时每个字都发一张状态快照


class Throttle:
    """最多每 interval 秒做一次 action。间隔里来的请求合成一次，等间隔到了补上，所以最后的样子一定会发出去。"""

    def __init__(self, action: Callable[[], None], interval: float):
        self._action = action
        self._interval = interval
        self._lock = threading.Lock()
        self._last = 0.0
        self._timer: threading.Timer | None = None

    def __call__(self, now: bool = False) -> None:
        with self._lock:
            if now:
                if self._timer is not None:
                    self._timer.cancel()
                    self._timer = None
            elif self._timer is not None:
                return  # 已经约好了一次，到时候发的是那时最新的样子
            else:
                wait = self._last + self._interval - time.monotonic()
                if wait > 0:
                    self._timer = threading.Timer(wait, self._flush)
                    self._timer.daemon = True
                    self._timer.start()
                    return
            self._last = time.monotonic()
        self._action()

    def _flush(self) -> None:
        with self._lock:
            self._timer = None
            self._last = time.monotonic()
        self._action()


class Scheduler(SchedulerLoop, SchedulerWork, SchedulerActions):
    """网页界面里的定时任务：调度线程、排队、执行、进度，以及页面上的那几个操作（/api/task/...）。

    锁的规矩（违反了会互相等死）：拿着 self._lock 的时候，不调用 RunUI 的方法、不发状态、不碰任务文件。
    RunUI 在等确认时是拿着它自己的锁来通知我们的（见 _observe），所以反过来只能先放开 self._lock 再去找它。
    """

    def __init__(self, web: WebApp):
        self.web = web
        self.store = web.app.tasks
        self.limit = max(1, web.app.config.max_parallel_tasks)  # 最多同时跑几个（见 config.py 的 MAX_PARALLEL_TASKS）
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)  # 队伍空了（全都跑完了）的时候通知等着的人（见 wait_idle）
        self._enqueuing = threading.Lock()  # "查一下是不是已经在排队、没有就排进去"要一口气做完，免得连点两下排进去两次
        self._queue: list[Pending] = []  # 正在跑的和排着队的，按排进来的先后
        self._wake = threading.Event()
        self._closed = False
        self.started_at: float | None = None  # 第一次看时间表的时刻。在这之前就该运行的，都是程序没开着时错过的
        self._progress = Throttle(self._publish, PUBLISH_INTERVAL)
        self.store.on_change(self._changed)

    def clock(self) -> float:
        return self.store.clock()

    def _changed(self) -> None:
        """任务或提醒变了：叫醒调度线程重新算该睡多久，并刷新页面。"""
        self._wake.set()
        self._publish()

    def _publish(self) -> None:
        """刷新页面上的状态。这里发的每一张都只留最新的（见 WebUI.forget_old_states）：
        跑一个任务能发出上千张状态快照，这期间你不一定在对话，等不到下一轮开始时再清。"""
        self.web.publish_state()
        self.web.ui.forget_old_states()

    # ---- 状态 --------------------------------------------------------------

    def snapshot(self) -> tuple[list[dict], list[dict]]:
        """状态里的 tasks 和 reminders（格式见这个文件开头的说明，以及 webapp.py 的 publish_state）。"""
        tasks, reminders = self.store.snapshot()
        prices, max_steps = self.web.app.prices(), self.web.app.config.max_steps
        with self._lock:
            live = {pending.task_id: _live_view(pending, prices, max_steps, self.limit) for pending in self._queue}
        for task in tasks:
            task.pop("anchor", None)  # 只是算间隔用的，页面用不到
            for run in task["runs"]:
                run.pop("error_key", None)
                run.pop("error_values", None)
            task["running"] = live.get(task["id"])
        return tasks, reminders
