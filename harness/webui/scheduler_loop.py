from __future__ import annotations

import threading

from ..tasks import count_due, new_run
from .scheduler_run import Pending
from .scheduler_types import SchedulerPart

LATE_SECONDS = 120  # 比预定时间晚了这么久才看到，说明这期间程序没在运行（或者电脑睡着了），算补跑
MAX_SLEEP = 30  # 调度线程最多睡这么久就起来看一眼：电脑睡醒、改了系统时间，都能在半分钟内发现
MIN_SLEEP = 0.5


class SchedulerLoop(SchedulerPart):
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
