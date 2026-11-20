"""定时任务在网页界面里怎么运行：什么时候醒、怎么排队、怎么跑、怎么显示进度。

任务是什么、时间怎么算、存在哪里，在 ../tasks.py。这里管的是"到时间之后"：

    调度线程 ──到时间了──► 提醒：马上弹出来（不调用模型，不花钱，不用排队）
                      └──► agent 任务：排进队里 ──等你这一轮对话结束──► 执行线程跑一轮 ──► 存成一段会话

为什么只在网页界面里运行：任务要在你没看着的时候自己跑，跑的过程要能看到、要你批准的操作要能点，
这些都需要一个一直开着、又能显示东西的地方。终端界面在你打字时一直等着输入，没法同时做这些。

几条规矩，以及为什么：

  和你的对话排队，不同时进行。
    任务和你的对话用的是同一套工具：同一个 shell（"停止"要结束的是正在运行的那条命令）、同一份待办、
    同一个工作目录。两边同时跑，一边的"停止"会把另一边的命令杀掉，两边改同一个文件也会互相覆盖。
    所以任务要先拿到对话的那把锁（WebApp._turn）：你正在对话的话，它等你这一轮结束；它在跑的时候，
    你发消息、换模型、打开会话都会被拦下，并且告诉你是哪个任务、怎么停下它。提醒不用这把锁。

  不打扰你正在看的对话。
    任务用的是新建的适配层和空的历史（见 App.unattended_agent），显示的事件记在它自己的日志里（RunUI），
    不进页面上那段对话的日志。跑完存成一段普通的会话，出现在会话列表里，可以打开、重放、接着聊。

  没人看着的时候不自动放行。
    要你确认的操作（执行命令、改文件……），任务会停下来等你，你选了"全部自动允许"也一样。
    停着的那个确认放在状态里（tasks[].running.confirm），页面上能看到、能答复（/api/task/confirm）。

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
    from .server import WebApp

LATE_SECONDS = 120  # 比预定时间晚了这么久才看到，说明这期间程序没在运行（或者电脑睡着了），算补跑
MAX_SLEEP = 30  # 调度线程最多睡这么久就起来看一眼：电脑睡醒、改了系统时间，都能在半分钟内发现
MIN_SLEEP = 0.5
TURN_POLL = 0.25  # 排队等对话锁时，每隔这么久看一眼这次运行是不是被停下了
PUBLISH_INTERVAL = 0.25  # 运行时的进度最多每秒刷新四次，免得逐字输出时每个字都发一张状态快照
MAX_LOG = 30  # 进度里最多列出这么多次工具调用（最近的）
SUMMARY_CHARS = 120
TITLE_CHARS = 21  # 会话标题里任务名称最多占这么多字（后面还有" · 2026-11-21 09:00"）

WAIT_TURN = k("等你这一轮对话结束")
WAIT_QUEUE = k("排在“{title}”后面")
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
    cancelled: bool = False  # 还没开始就被停下了（或者程序要退出了）
    stop_requested: bool = False
    started_at: float | None = None  # 拿到对话锁、真正开始的时刻
    ui: RunUI | None = None
    agent: Agent | None = None
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

    def __init__(self, web: WebApp, turn: threading.Lock):
        self.web = web
        self.store = web.app.tasks
        self._turn = turn  # 对话的那把锁：任务和你的对话轮流用它
        self._lock = threading.Lock()
        self._enqueuing = threading.Lock()  # "查一下是不是已经在排队、没有就排进去"要一口气做完，免得连点两下排进去两次
        self._queue: list[Pending] = []  # 排队的和正在跑的（正在跑的那个总在最前面）
        self._worker: threading.Thread | None = None
        self._wake = threading.Event()
        self._closed = False
        self._holder: str | None = None  # 正拿着对话锁的那个任务的名称
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
        跑一个任务能发出上千张状态快照，这期间你没在对话，等不到下一轮开始时再清。"""
        self.web.publish_state()
        self.web.ui.forget_old_states()

    def holder(self) -> str | None:
        """正在运行、拿着对话锁的那个任务的名称；没有的话是 None。"""
        with self._lock:
            return self._holder

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

    def _enqueue(self, task: dict, now: float, *, due: float | None, catch_up: bool, missed: int, manual: bool) -> bool:
        """排进队里。已经在排队或者正在跑、或者程序要退出了，返回 False。"""
        with self._enqueuing:
            with self._lock:
                if self._closed or any(pending.task_id == task["id"] for pending in self._queue):
                    return False
            run = new_run(now, "waiting", catch_up=catch_up, missed=missed, manual=manual)
            # 先记下这次运行，再排进队：执行线程一开始就要改这条记录，记录得先在那儿
            if not self.store.add_run(task["id"], run):
                return False
            pending = Pending(task["id"], run["id"], task["title"], now, due, catch_up, missed, manual)
            with self._lock:
                self._queue.append(pending)
                if self._worker is None:
                    self._worker = threading.Thread(target=self._work, daemon=True, name="task-run")
                    self._worker.start()
        self._publish()  # 排进去了：页面上显示"等你这一轮对话结束"
        return True

    # ---- 执行线程 ----------------------------------------------------------

    def _work(self) -> None:
        """一个接一个地跑队里的任务，跑完了就退出（下次有任务排进来时再开一个）。"""
        while True:
            with self._lock:
                pending = self._queue[0] if self._queue and not self._closed else None
                if pending is None:
                    self._worker = None
                    return
            if self._take_turn(pending):
                try:
                    self._run(pending)
                except Exception as exc:  # noqa: BLE001 —— 这一次出了意料之外的错：记下来，接着跑后面的，不能让队伍卡住
                    self._finish(pending, "failed", error=t("这次运行出错了：{error}", error=f"{type(exc).__name__}: {exc}"))
                finally:
                    with self._lock:
                        self._holder = None
                    self._turn.release()
            with self._lock:
                if pending in self._queue:
                    self._queue.remove(pending)
            self._wake.set()
            self._publish()

    def _take_turn(self, pending: Pending) -> bool:
        """等到对话的锁空出来（你这一轮对话结束）再拿过来。等的时候被停下了、或者程序要退出了，返回 False。"""
        while True:
            with self._lock:
                if pending.cancelled or self._closed:
                    return False
            # 不是一直死等：每隔一会儿起来看看是不是被停下了。你这一轮一结束、锁一放开，这里马上就能拿到
            if self._turn.acquire(timeout=TURN_POLL):
                with self._lock:
                    if not (pending.cancelled or self._closed):
                        pending.started_at = self.clock()
                        self._holder = pending.title
                        return True
                self._turn.release()
                return False

    def _run(self, pending: Pending) -> None:
        """跑一次。调用的时候已经拿着对话锁了。"""
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
        ui.on_stop(app.shell.kill_running)  # 停止时连正在运行的命令一起结束（和对话里的"停止"一样）
        agent = app.unattended_agent(ui)
        agent.on_usage = self._progress  # 每调用完一次模型，刷新步数和费用
        conversation = app.store.new(provider.id, app.settings.model)
        conversation.title = f"{short(task['title'], TITLE_CHARS)} · {local_text(now)}"
        conversation.task = task["id"]
        self.store.update_run(task["id"], pending.run_id, status="running", started_at=now)

        ui.begin_turn(task["prompt"])  # 会话里显示的是你写的指令；发给模型的还带着一段说明（见 instruction）
        with self._lock:
            pending.ui, pending.agent = ui, agent
            stop_now = pending.stop_requested
        if stop_now:
            ui.request_stop()  # 在 begin_turn 之后才立旗子：begin_turn 会把旗子放下
        self._progress(now=True)
        run_turn(agent, ui, instruction(task, pending))
        ui.end_turn()

        messages = agent.backend.export()
        problem = ""
        if messages:  # 和平常一样：一点内容都没有（第一次请求就失败了）的不存
            conversation.messages = messages
            conversation.events = ui.export_events()
            conversation.context_tokens = agent.usage.context_tokens
            try:
                app.store.save(conversation)
            except Exception as exc:  # noqa: BLE001 —— 存不上也要把这次运行的结果记下来
                problem = t("这次运行的会话没能保存：{error}", error=str(exc) or type(exc).__name__)
        total, unpriced = estimate(agent.usage_by_model, app.settings.prices)
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
        with self._lock:
            if pending in self._queue:
                self._queue.remove(pending)

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
                pending.log = [*pending.log, {"tool": data["name"], "summary": data["summary"], "ok": None}][-MAX_LOG:]
            elif kind == "tool_result":
                if pending.log and pending.log[-1]["ok"] is None:
                    pending.log[-1]["ok"] = not data["is_error"]
                pending.activity = {"kind": "thinking", "tool": "", "summary": ""}  # 结果交回给模型了，等它想下一步
            elif kind == "confirm":
                question = Message(data["description"], data["key"], data["values"]) if "key" in data else data["description"]
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
        """状态里的 tasks 和 reminders（格式见这个文件开头的说明，以及 server.py 的 publish_state）。"""
        tasks, reminders = self.store.snapshot()
        prices, max_steps = self.web.app.settings.prices, self.web.app.config.max_steps
        with self._lock:
            head = self._queue[0] if self._queue else None
            live: dict[str, dict] = {}
            for index, pending in enumerate(self._queue):
                live.setdefault(pending.task_id, _live_view(pending, index, head, prices, max_steps))
        for task in tasks:
            task.pop("anchor", None)  # 只是算间隔用的，页面用不到
            for run in task["runs"]:
                run.pop("error_key", None)
                run.pop("error_values", None)
            task["running"] = live.get(task["id"])
        return tasks, reminders

    # ---- 页面上的操作（server.py 的 /api/task/...）----------------------------
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

    def run_now(self, task_id: str) -> None:
        """立即运行一次。提醒马上弹出来；agent 任务排进队里（你正在对话的话，等这一轮结束）。不改变原来的计划。"""
        task = self.store.get(task_id)
        if task is None:
            raise TaskRefused(t("这个任务不存在，可能已经被删除了。"))
        now = self.clock()
        if task["kind"] == "reminder":
            self.store.remind(task_id, now, new_run(now, "done", manual=True))
            return
        provider = self.web.app.provider
        if not has_key(provider):
            raise TaskRefused(t("还没有设置 {provider} 的 API 密钥，请先点右上角的“设置”填写。", provider=provider.label))
        if not self._enqueue(task, now, due=None, catch_up=False, missed=0, manual=True):
            raise TaskRefused(t("这个任务已经在排队或者正在运行了。"))

    def stop(self, task_id: str) -> None:
        """停下一个任务：还在排队就不跑了；正在跑就像对话里的"停止"一样（连正在运行的命令一起结束）。"""
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
                pending.cancelled = True
                self._queue.remove(pending)
        if waiting:
            self.store.update_run(task_id, pending.run_id, status="stopped", finished_at=self.clock())
        elif ui is not None:
            ui.request_stop()  # 已经放开了 self._lock 才去找 RunUI（见类的说明）
        # 刚拿到锁、还没来得及建好 RunUI 的，_run 建好之后会看到 stop_requested，马上停
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
        """程序要退出了：不再开始新的运行，排队的不跑了，正在跑的停下（它会自己收尾、存好盘）。"""
        with self._lock:
            self._closed = True
            waiting = [pending for pending in self._queue if pending.started_at is None]
            running = [pending for pending in self._queue if pending.started_at is not None]
            for pending in waiting:
                pending.cancelled = True
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


def _live_view(pending: Pending, index: int, head: Pending | None, prices: dict, max_steps: int) -> dict:
    """状态里的 running：一次排队或者正在运行的情况。调用的人要拿着 Scheduler._lock。"""
    if pending.ui is None or pending.agent is None:
        # 还没开始：排在最前面的在等你这一轮对话结束，后面的在等前面的任务
        summary = t(WAIT_TURN) if index == 0 or head is None else t(WAIT_QUEUE, title=head.title)
        return {
            "run_id": pending.run_id,
            "started_at": pending.started_at or pending.queued_at,
            "step": 0,
            "max_steps": max_steps,
            "cost": 0.0,
            "activity": {"kind": "waiting", "tool": "", "summary": summary},
            "log": [],
            "confirm": None,
        }
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

    说明告诉模型三件事：这一轮不是用户刚刚打的字；用户多半不在，别停下来提问；要确认的操作会等用户回来。
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
        "需要用户确认的操作会停在那里，等他回来批准。</task>\n\n" + task["prompt"]
    )
