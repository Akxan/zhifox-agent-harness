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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..agent import Agent
from ..app import run_turn
from ..config import APP_NAME
from ..display import short
from ..i18n import Message, k, t
from ..pricing import estimate
from ..providers import has_key
from ..tasks import (
    TaskError,
    TaskRefused,
    chinese,
    count_due,
    describe,
    local_text,
    new_run,
    retell,
    said,
)
from ..tasks import preview as preview_schedule
from .ui import WebUI

if TYPE_CHECKING:
    from ..tools.shell import Shell
    from .server import WebApp

LATE_SECONDS = 120  # 比预定时间晚了这么久才看到，说明这期间程序没在运行（或者电脑睡着了），算补跑
MAX_SLEEP = 30  # 调度线程最多睡这么久就起来看一眼：电脑睡醒、改了系统时间，都能在半分钟内发现
MIN_SLEEP = 0.5
PUBLISH_INTERVAL = 0.25  # 运行时的进度最多每秒刷新四次，免得逐字输出时每个字都发一张状态快照
MAX_LOG = 30  # 进度里最多列出这么多次工具调用（最近的）
SUMMARY_CHARS = 120
TITLE_CHARS = 21  # 会话标题里任务名称最多占这么多字（后面还有" · 2026-11-21 09:00"）

QUEUED = k("排队中：同时最多运行 {n} 个任务")
WAIT_CONFIRM = k("等你批准")


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


class Scheduler:
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

    # ---- 调度线程 ----------------------------------------------------------

    def start(self) -> None:
        """启动调度线程（网页界面启动时调用一次）。测试不启动它，直接调用 tick。"""
        threading.Thread(target=self._loop, daemon=True, name="tasks").start()

    def _loop(self) -> None:
        while True:
            with self._lock:
                if self._closed:
                    return
            self._wake.clear()  # 先清再看：看的过程中有了变化，下面的 wait 会马上返回，再看一遍
            try:
                delay = self.tick()
            except Exception:  # noqa: BLE001 —— 调度线程不能死：哪一次出了错（比如磁盘满了写不进去），过一会儿接着看
                delay = MAX_SLEEP
            self._wake.wait(delay)

    def tick(self) -> float:
        """看一眼时间表：到时间的提醒弹出来，到时间的 agent 任务排进队里。返回到下一次该看的时候还有几秒。"""
        now = self.clock()
        if self.started_at is None:
            self.started_at = now
        for task in self.store.due(now):
            if self._find(task["id"]) is not None:
                continue  # 已经在排队或者正在跑：这一次算在它头上（它开始时会把下一次往后排）
            due = task["next_run"]
            catch_up = due < self.started_at or now - due > LATE_SECONDS
            missed = count_due(task["schedule"], due, now, task["anchor"]) if catch_up else 0
            if task["kind"] == "reminder":
                self.store.remind(task["id"], now, new_run(now, "done", catch_up=catch_up, missed=missed))
            else:
                self._enqueue(task, now, due=due, catch_up=catch_up, missed=missed, manual=False)
        with self._lock:
            busy = {pending.task_id for pending in self._queue}
        upcoming = self.store.next_due(busy)
        if upcoming is None:
            return MAX_SLEEP
        return min(MAX_SLEEP, max(MIN_SLEEP, upcoming - self.clock()))

    def _find(self, task_id: str) -> Pending | None:
        with self._lock:
            return next((pending for pending in self._queue if pending.task_id == task_id), None)

    def running(self, task_id: str) -> str:
        """这个任务正在运行的话，返回它的标题；没在运行（包括还在排队）返回空字符串。"""
        pending = self._find(task_id) if task_id else None
        return pending.title if pending is not None and pending.started_at is not None else ""

    def _enqueue(self, task: dict, now: float, *, due: float | None, catch_up: bool, missed: int, manual: bool) -> Pending | None:
        """排进队里，有空位的话马上开始。返回排进去的那一个；已经在排队或者正在跑、或者程序要退出了，返回 None。"""
        with self._enqueuing:
            with self._lock:
                if self._closed or any(pending.task_id == task["id"] for pending in self._queue):
                    return None
            run = new_run(now, "waiting", catch_up=catch_up, missed=missed, manual=manual)
            # 先记下这次运行，再排进队：执行线程一开始就要改这条记录，记录得先在那儿
            if not self.store.add_run(task["id"], run):
                return None
            pending = Pending(task["id"], run["id"], task["title"], now, due, catch_up, missed, manual)
            with self._lock:
                self._queue.append(pending)
        self._dispatch()
        self._publish()  # 排进去了：页面上显示"正在准备"，或者"排队中"
        return pending

    # ---- 执行 --------------------------------------------------------------

    def _dispatch(self) -> None:
        """有空位的话，让排在最前面的几个开始运行，每个一个线程。排进来一个、跑完一个的时候都来这里看一眼。"""
        with self._lock:
            if self._closed:
                return
            free = self.limit - sum(1 for pending in self._queue if pending.started_at is not None)
            starting = [pending for pending in self._queue if pending.started_at is None][: max(0, free)]
            for pending in starting:
                pending.started_at = self.clock()  # 在锁里就标上"开始了"：同时来看的另一个线程不会把它再开一遍
        for pending in starting:
            threading.Thread(target=self._work, args=(pending,), daemon=True, name="task-run").start()

    def _work(self, pending: Pending) -> None:
        """一个运行的线程：跑完这一次就退出，并让排着队的下一个开始。"""
        try:
            self._run(pending)
        except Exception as exc:  # noqa: BLE001 —— 这一次出了意料之外的错：记下来，不能让它占着的位子一直空不出来
            self._finish(pending, "failed", error=t("这次运行出错了：{error}", error=f"{type(exc).__name__}: {exc}"))
        finally:
            self._forget(pending)
            self._wake.set()
            self._dispatch()  # 空出一个位子：排在最前面的那个可以开始了
            self._publish()

    def _run(self, pending: Pending) -> None:
        """跑一次（在这次运行自己的线程里）。"""
        app = self.web.app
        task = self.store.get(pending.task_id)
        if task is None:
            return  # 排队的时候被删掉了
        now = pending.started_at or self.clock()
        if task["kind"] == "reminder":
            # 排队的时候被改成了"只是提醒"：那就提醒一下
            run = new_run(now, "done", catch_up=pending.catch_up, missed=pending.missed, manual=pending.manual)
            run["id"] = pending.run_id
            self._forget(pending)
            self.store.remind(task["id"], now, run)
            return
        self.store.advance(task["id"], now)  # 时间表上到了的那一次（几次）就算是这一次跑过了
        provider = app.provider
        if not has_key(provider):
            self._finish(pending, "failed", error=t("还没有设置 {provider} 的 API 密钥，这次没有运行。", provider=provider.label))
            return

        ui = RunUI(lambda kind, data, event_id: self._observe(pending, kind, data, event_id))
        # 这次运行自己的助手、shell 和 MCP 调用。权限、沙箱按这个任务自己的那一档；之后你再改它，只影响以后的运行
        unattended = app.unattended_agent(ui, task["approval"], task.get("sandbox", "default"))
        agent, shell = unattended.agent, unattended.shell
        # 停止时连它正在运行的命令、正在等的 MCP 调用一起结束。只是它自己的，你的对话和别的任务不受影响
        ui.on_stop(unattended.stop)
        agent.on_usage = self._progress  # 每调用完一次模型，刷新步数和费用
        conversation = app.store.new(agent.backend.provider, agent.backend.model)  # 和这次运行用的模型一致，哪怕你刚刚换了
        conversation.title = f"{short(task['title'], TITLE_CHARS)} · {local_text(now)}"
        conversation.task = task["id"]
        agent.hooks.session = lambda: conversation.id  # 交给你的钩子的 session_id：这次运行存成的那段会话
        self.store.update_run(task["id"], pending.run_id, status="running", started_at=now)

        ui.begin_turn(task["prompt"])  # 会话里显示的是你写的指令；发给模型的还带着一段说明（见 instruction）
        with self._lock:
            pending.ui, pending.agent, pending.shell = ui, agent, shell
            stop_now = pending.stop_requested
        if stop_now:
            ui.request_stop()  # 在 begin_turn 之后才立旗子：begin_turn 会把旗子放下
        self._progress(now=True)
        try:
            run_turn(agent, ui, instruction(task, pending))
        finally:
            unattended.close()  # 这次运行自己开的浏览器标签页关掉（别的运行的不碰）
        ui.end_turn()

        messages = agent.backend.export()
        problem = ""
        if messages:  # 和平常一样：一点内容都没有（第一次请求就失败了）的不存
            conversation.messages = messages
            conversation.dropped = agent.backend.dropped()
            conversation.events = ui.export_events()
            conversation.context_tokens = agent.usage.context_tokens
            try:
                app.store.save(conversation)
            except Exception as exc:  # noqa: BLE001 —— 存不上也要把这次运行的结果记下来
                problem = t("这次运行的会话没能保存：{error}", error=str(exc) or type(exc).__name__)
        total, unpriced = estimate(agent.usage_by_model, app.prices())
        if ui.stopped:
            status, error = "stopped", ""
        elif ui.failure is not None:
            status, error = "failed", ui.failure
        elif not messages:
            status, error = "failed", ui.last_info or t("这次运行没有留下任何内容。")
        else:
            status, error = "done", ""
        self._finish(
            pending,
            status,
            error=error or problem,
            cost=None if unpriced else round(total, 6),
            steps=agent.usage.requests,
            conversation=conversation.id if conversation.saved else "",
            summary=short(ui.answer, SUMMARY_CHARS),
        )

    def _forget(self, pending: Pending) -> None:
        with self._idle:
            if pending in self._queue:
                self._queue.remove(pending)
            if not self._queue:
                self._idle.notify_all()

    def _finish(self, pending: Pending, status: str, error: str = "", **fields: Any) -> None:
        """一次运行结束了。先从队里拿掉、再写记录：页面收到的那张快照里，"正在运行"和"做完了"不会同时出现。"""
        self._forget(pending)
        self.store.update_run(pending.task_id, pending.run_id, status=status, finished_at=self.clock(), **said(error), **fields)

    def _observe(self, pending: Pending, kind: str, data: dict, event_id: int) -> None:
        """RunUI 每发一个事件就来这里：更新进度。

        等确认的那个事件（confirm）是 RunUI 拿着它自己的锁时发的，所以这里绝不能反过来去碰 RunUI。
        """
        status = ""
        with self._lock:
            if kind == "thinking":
                pending.activity = {"kind": "thinking", "tool": "", "summary": ""}
            elif kind == "text":
                pending.activity = {"kind": "writing", "tool": "", "summary": ""}
            elif kind == "tool_call":
                pending.tool = data["name"]
                pending.activity = {"kind": "tool", "tool": data["name"], "summary": data["summary"]}
                entry = {"tool": data["name"], "summary": data["summary"], "ok": None, "auto": False}
                pending.log = [*pending.log, entry][-MAX_LOG:]
            elif kind == "auto_allowed":
                # 这一步本来要问你，按这个任务的那一档没问就放行了。进度里标出来，会话里也记着（RunUI 照常记下这个事件）
                if pending.log:
                    pending.log[-1]["auto"] = True
                    pending.log[-1]["why"] = str(data.get("why", "mode"))  # 只读的命令、你加的命令，页面上写得更具体
            elif kind == "tool_result":
                if pending.log and pending.log[-1]["ok"] is None:
                    pending.log[-1]["ok"] = not data["is_error"]
                pending.activity = {"kind": "thinking", "tool": "", "summary": ""}  # 结果交回给模型了，等它想下一步
            elif kind == "subagent" and data.get("step") == "tool_call":
                # 任务把一件事交给了子 Agent（见 agents/runner.py），它正在调用工具：进度里写明是它在做
                summary: str = t("「{agent}」：{summary}", agent=data.get("agent", ""), summary=data.get("summary", ""))
                pending.activity = {"kind": "tool", "tool": data.get("name", ""), "summary": summary}
            elif kind == "confirm":
                question = Message(data["description"], data["key"], data["values"]) if "key" in data else data["description"]
                if data.get("agent"):  # 是子 Agent 在问：确认上写明是它
                    question = t("「{agent}」：{summary}", agent=data["agent"], summary=retell(question))
                pending.confirm = {"id": event_id, "tool": pending.tool, "summary": question}
                pending.activity = {"kind": "waiting", "tool": pending.tool, "summary": t(WAIT_CONFIRM)}
                status = "waiting"
            elif kind == "confirm_done":
                pending.confirm = None
                summary = pending.log[-1]["summary"] if pending.log else ""
                pending.activity = {"kind": "tool", "tool": pending.tool, "summary": summary}
                status = "running"
            else:
                return
        if status:
            # 停下来等你、或者你答复了：马上让页面知道（写记录会触发一次刷新）
            self.store.update_run(pending.task_id, pending.run_id, status=status)
        else:
            self._progress()

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

    # ---- 页面上的操作（tasks_api.py 的 /api/task/...）----------------------------
    # 内容不对抛出 TaskError（回答 400），现在做不了抛出 TaskRefused（回答 409）。

    def save(self, task_id: str | None, fields: dict) -> str:
        """新建（task_id 为 None）或者修改一个任务。返回它的编号。"""
        task = self.store.add(fields) if task_id is None else self.store.update(task_id, fields)
        return task["id"]

    def toggle(self, task_id: str, enabled: bool) -> None:
        self.store.set_enabled(task_id, enabled)

    def delete(self, task_id: str) -> None:
        """删掉一个任务。它正在排队就不跑了；正在跑就停下来（已经做了的照样存成会话）。"""
        if self.store.get(task_id) is None:
            raise TaskRefused(t("这个任务不存在，可能已经被删除了。"))
        self._stop(task_id)
        self.store.delete(task_id)

    def run_now(self, task_id: str) -> bool:
        """立即运行一次，不改变原来的计划。提醒马上弹出来；agent 任务有空位就开始，没有就排队。

        返回 True 表示在排队（同时运行的已经到了上限），页面据此告诉你"排好了"还是"开始了"。
        """
        task = self.store.get(task_id)
        if task is None:
            raise TaskRefused(t("这个任务不存在，可能已经被删除了。"))
        now = self.clock()
        if task["kind"] == "reminder":
            self.store.remind(task_id, now, new_run(now, "done", manual=True))
            return False
        provider = self.web.app.provider
        if not has_key(provider):
            raise TaskRefused(t("还没有设置 {provider} 的 API 密钥，请先点右上角的“设置”填写。", provider=provider.label))
        pending = self._enqueue(task, now, due=None, catch_up=False, missed=0, manual=True)
        if pending is None:
            raise TaskRefused(t("这个任务已经在排队或者正在运行了。"))
        with self._lock:
            return pending.started_at is None

    def stop(self, task_id: str) -> None:
        """停下一个任务：还在排队就不跑了；正在跑就像对话里的"停止"一样，连它正在运行的命令一起结束。
        只停这一个：你的对话和别的任务各有各的界面和 shell，碰不到。"""
        if not self._stop(task_id):
            raise TaskRefused(t("这个任务现在没有在运行。"))

    def _stop(self, task_id: str) -> bool:
        with self._lock:
            pending = next((item for item in self._queue if item.task_id == task_id), None)
            if pending is None:
                return False
            pending.stop_requested = True
            ui, waiting = pending.ui, pending.started_at is None
            if waiting:
                self._queue.remove(pending)
        if waiting:
            self.store.update_run(task_id, pending.run_id, status="stopped", finished_at=self.clock())
        elif ui is not None:
            ui.request_stop()  # 已经放开了 self._lock 才去找 RunUI（见类的说明）
        # 刚开始、还没来得及建好 RunUI 的，_run 建好之后会看到 stop_requested，马上停
        return True

    def confirm(self, task_id: str, confirm_id: int, allow: bool, feedback: str) -> None:
        """答复一次运行停在那里等的确认。"""
        with self._lock:
            pending = next((item for item in self._queue if item.task_id == task_id), None)
            ui = pending.ui if pending is not None else None
            waiting = pending.confirm if pending is not None else None
        if ui is None or waiting is None or waiting["id"] != confirm_id or not ui.answer_confirm(confirm_id, allow, feedback):
            raise TaskRefused(t("这个确认请求已经处理过了"))

    def dismiss(self, reminder_id: str | None) -> None:
        self.store.dismiss(reminder_id)

    def preview(self, schedule: object) -> list[float]:
        return preview_schedule(schedule, self.clock())

    def close(self) -> None:
        """程序要退出了：不再开始新的运行，排队的不跑了，正在跑的停下（它们会自己收尾、存好盘，见 wait_idle）。"""
        with self._lock:
            self._closed = True
            waiting = [pending for pending in self._queue if pending.started_at is None]
            running = [pending for pending in self._queue if pending.started_at is not None]
            for pending in waiting:
                self._queue.remove(pending)
            for pending in running:
                pending.stop_requested = True
            uis = [pending.ui for pending in running if pending.ui is not None]
        self._wake.set()
        for pending in waiting:
            try:
                note = said(t("{name}退出了，这次没有运行。", name=APP_NAME))
                self.store.update_run(pending.task_id, pending.run_id, status="stopped", finished_at=self.clock(), **note)
            except (OSError, TaskError):
                pass  # 退出时写不进去也不要紧：下次启动时它会被当成"没跑完"，到时间的会补跑
        for ui in uis:
            ui.request_stop()
        with self._idle:
            self._idle.notify_all()  # 队伍可能已经空了（只有排队的、没有正在跑的）

    def wait_idle(self, timeout: float) -> bool:
        """等所有正在跑的运行结束（包括它们收尾时的存盘）。等到了返回 True，超时返回 False。退出程序之前用。"""
        with self._idle:
            return self._idle.wait_for(lambda: not self._queue, timeout)


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
