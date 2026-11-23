from __future__ import annotations

import copy
import json
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import APP_NAME
from .i18n import k, t
from .safe import private_dir, to_json, write_file
from .tasks_check import TaskError, TaskRefused, check_task
from .tasks_records import (
    UNFINISHED,
    _brief,
    _is_time,
    _restore_reminder,
    _restore_run,
    new_id,
    said,
)
from .tasks_schedule import PAST, next_after

FORMAT_VERSION = 1
MAX_TASKS = 100  # 任务最多这么多个。模型一时糊涂、一口气加几百个的话，在这里拦住
MAX_RUNS = 20  # 每个任务记住最近这么多次运行
MAX_REMINDERS = 50  # 还没关掉的提醒最多留这么多条，更早的自动丢掉
_ID = re.compile(r"[0-9a-f]{8}")
INTERRUPTED = k("{name}在这次运行结束之前退出了。")


# ---- 保存 --------------------------------------------------------------------


class TaskStore:
    """所有任务，以及还没关掉的提醒。保存在一个 JSON 文件里（data/tasks.json）。

    网页界面里好几个线程会同时用它：调度线程（看谁到时间了）、执行任务的线程（记下运行的结果）、
    处理请求的线程（你在页面上改任务），还有对话中的模型（用工具加任务）。所以每个操作都在一把锁里做完。

    终端界面也能通过工具加任务，而且可以和网页界面同时开着。两个程序各自在内存里记着一份，
    所以每次用之前看一眼文件有没有被别人改过（_refresh），改过就重新读，免得把对方刚加的任务覆盖掉。
    """

    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock  # 现在是什么时刻。测试里换成一个假的钟，不用真的等到 9 点
        self.problem = ""  # 文件读不出来时的说明，网页界面启动时显示一次
        self._lock = threading.RLock()
        self._listeners: list[Callable[[], None]] = []
        self._tasks: list[dict] = []
        self._reminders: list[dict] = []
        self._stamp: tuple | None = None  # 上一次读或写之后，文件的修改时间和大小
        with self._lock:
            self._read(starting=True)

    def on_change(self, callback: Callable[[], None]) -> None:
        """登记一个函数，任务或提醒有了变化就调用它（网页界面用它刷新页面、叫醒调度线程）。"""
        self._listeners.append(callback)

    def _notify(self) -> None:
        # 在锁外面调用：听的人可能要反过来读任务（刷新页面），在锁里调用容易互相等死
        for callback in self._listeners:
            callback()

    # ---- 读写文件 --------------------------------------------------------

    def _file_stamp(self) -> tuple | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _refresh(self) -> None:
        """文件在我们上一次读写之后被别的程序改过的话，重新读一遍。调用的人要先拿到锁。"""
        if self._file_stamp() != self._stamp:
            self._read(starting=False)

    def _read(self, starting: bool) -> None:
        self._stamp = self._file_stamp()
        if self._stamp is None:
            self._tasks, self._reminders = [], []
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):  # RecursionError：嵌套了上千层的 JSON
            data = None
        if not isinstance(data, dict):
            self._set_aside()
            return
        tasks = saved_tasks if isinstance(saved_tasks := data.get("tasks"), list) else []
        reminders = saved_reminders if isinstance(saved_reminders := data.get("reminders"), list) else []
        # 一个一个检查：文件可能被手动改过，坏掉的那一个丢掉，别的照常用，不至于一处写错就全没了
        self._tasks = [task for task in map(self._restore_task, tasks) if task is not None]
        self._reminders = [item for item in map(_restore_reminder, reminders) if item is not None]
        if starting:
            # 刚启动就看到"正在运行"的记录，说明上一次程序在它跑完之前就退出了（崩溃、强制退出、断电）
            for task in self._tasks:
                for run in task["runs"]:
                    if run["status"] in UNFINISHED:
                        run.update(status="failed", **said(t(INTERRUPTED, name=APP_NAME)))

    def _set_aside(self) -> None:
        """文件读不出来：把它改名留着（里面也许还有能救回来的内容），从空的开始。"""
        backup = self.path.with_name(self.path.stem + ".broken.json")
        try:
            self.path.replace(backup)
        except OSError:
            backup = self.path
        self.problem = t("任务文件读不出来，可能已经损坏。原来的文件留在 {path}，现在从空的任务列表开始。", path=backup)
        self._tasks, self._reminders = [], []
        self._stamp = self._file_stamp()

    def _restore_task(self, raw: object) -> dict | None:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not _ID.fullmatch(raw["id"]):
            return None
        try:
            checked = check_task(raw)
        except TaskError:
            return None
        created, next_run, anchor = raw.get("created_at"), raw.get("next_run"), raw.get("anchor")
        if not _is_time(created) or not (next_run is None or _is_time(next_run)) or not (anchor is None or _is_time(anchor)):
            return None
        runs = saved_runs if isinstance(saved_runs := raw.get("runs"), list) else []
        return {
            "id": raw["id"],
            **checked,
            "created_at": float(created),
            "next_run": None if next_run is None else float(next_run),
            "anchor": None if anchor is None else float(anchor),
            "runs": [run for run in map(_restore_run, runs) if run is not None][:MAX_RUNS],
        }

    def _save(self) -> None:
        """写回文件。调用的人要先拿到锁。"""
        private_dir(self.path.parent)
        data = {"version": FORMAT_VERSION, "tasks": self._tasks, "reminders": self._reminders}
        write_file(self.path, to_json(data, readable=True, indent=1))
        self._stamp = self._file_stamp()

    # ---- 查看 -----------------------------------------------------------

    def snapshot(self) -> tuple[list[dict], list[dict]]:
        """全部任务和还没关掉的提醒（都是拷贝，随便改）。出错原因按现在的界面语言重新译一遍。"""
        with self._lock:
            self._refresh()
            tasks, reminders = copy.deepcopy(self._tasks), copy.deepcopy(self._reminders)
        for task in tasks:
            for run in task["runs"]:
                if run.get("error_key"):
                    run["error"] = t(run["error_key"], **run["error_values"])
        return tasks, reminders

    def get(self, task_id: object) -> dict | None:  # 什么都可以给（模型给的参数可能缺了、类型不对），找不到就是 None
        with self._lock:
            self._refresh()
            task = self._find(task_id)
            return None if task is None else copy.deepcopy(task)

    def _find(self, task_id: object) -> dict | None:
        return next((task for task in self._tasks if task["id"] == task_id), None)

    def _get(self, task_id: object) -> dict:
        task = self._find(task_id)
        if task is None:
            raise TaskRefused(t("这个任务不存在，可能已经被删除了。"))
        return task

    def due(self, now: float) -> list[dict]:
        """到时间了、还没有运行的任务（拷贝）。"""
        with self._lock:
            self._refresh()
            return [
                copy.deepcopy(task)
                for task in self._tasks
                if task["enabled"] and task["next_run"] is not None and task["next_run"] <= now
            ]

    def next_due(self, skip: set[str]) -> float | None:
        """最早的下一次运行时刻；skip 里的任务不算（它们已经在排队或者正在运行）。调度线程用它决定睡多久。"""
        with self._lock:
            self._refresh()
            times = [
                task["next_run"]
                for task in self._tasks
                if task["enabled"] and task["next_run"] is not None and task["id"] not in skip
            ]
        return min(times, default=None)

    # ---- 增删改 ---------------------------------------------------------

    def add(self, fields: object) -> dict:
        """新建一个任务。返回它（拷贝）。"""
        task = check_task(fields)
        now = self.clock()
        with self._lock:
            self._refresh()
            if len(self._tasks) >= MAX_TASKS:
                raise TaskRefused(t("任务最多 {n} 个。先删掉一些用不到的再加。", n=MAX_TASKS))
            task.update(id=self._unused_id(), created_at=now, anchor=now, runs=[])
            task["next_run"] = self._first_run(task, now)
            self._tasks.append(task)
            self._save()
            saved = copy.deepcopy(task)
        self._notify()
        return saved

    def update(self, task_id: str, fields: object) -> dict:
        """改一个任务。改完重新算下一次运行的时刻。"""
        checked = check_task(fields)
        now = self.clock()
        with self._lock:
            self._refresh()
            task = self._get(task_id)
            # 先在一份拷贝上算好，算不出来（比如新选的时间已经过了）就什么都不改
            updated = {**task, **checked}
            if checked["schedule"] != task["schedule"]:
                updated["anchor"] = now  # 换了时间表：间隔从现在重新数
                updated["next_run"] = self._first_run(updated, now)
            else:
                # 只改了名称、指令：时间表没变，下一次照旧。一次性的任务已经过了时间，也不因为改了个名字就报错
                updated["next_run"] = self._upcoming(updated, now)
            task.update(updated)
            self._save()
            saved = copy.deepcopy(task)
        self._notify()
        return saved

    def set_enabled(self, task_id: str, enabled: bool) -> dict:
        """打开或关掉一个任务。关掉期间错过的不补；重新打开时从现在往后算。"""
        now = self.clock()
        with self._lock:
            self._refresh()
            task = self._get(task_id)
            task["enabled"] = enabled
            task["next_run"] = self._upcoming(task, now)
            self._save()
            saved = copy.deepcopy(task)
        self._notify()
        return saved

    def delete(self, task_id: str) -> dict:
        with self._lock:
            self._refresh()
            task = self._get(task_id)
            self._tasks.remove(task)
            self._save()
        self._notify()
        return task

    def _unused_id(self) -> str:
        while True:
            task_id = new_id()
            if self._find(task_id) is None:
                return task_id

    def _first_run(self, task: dict, now: float) -> float | None:
        """新的时间表的第一次。一次性任务的时间已经过了的话，告诉用户（多半是日期选错了）。"""
        moment = next_after(task["schedule"], now, anchor=task["anchor"])
        if moment is None:
            raise TaskError(t(PAST))
        return moment if task["enabled"] else None

    def _upcoming(self, task: dict, now: float) -> float | None:
        if not task["enabled"]:
            return None
        return next_after(task["schedule"], now, anchor=task["anchor"])

    # ---- 运行的记录（webui/scheduler.py 用）-------------------------------

    def add_run(self, task_id: str, run: dict) -> bool:
        """记下一次新的运行（排在最前面）。任务已经不在了返回 False。"""
        with self._lock:
            self._refresh()
            task = self._find(task_id)
            if task is None:
                return False
            task["runs"] = [run, *task["runs"]][:MAX_RUNS]
            self._save()
        self._notify()
        return True

    def update_run(self, task_id: str, run_id: str, **changes: Any) -> bool:
        """改一次运行的记录（开始了、停在确认上了、做完了……）。任务或这条记录已经不在了返回 False。"""
        with self._lock:
            self._refresh()
            task = self._find(task_id)
            run = None if task is None else next((run for run in task["runs"] if run["id"] == run_id), None)
            if run is None:
                return False
            run.update(changes)
            self._save()
        self._notify()
        return True

    def advance(self, task_id: str, now: float) -> None:
        """一次运行开始了：时间表上已经到了的那一次（或者几次）就算是它跑过了，下一次往后排。

        立即运行（手动）的时候，要是计划的时间还没到，计划不变；要是计划的时间已经到了（比如正好在排队），
        这一次就算数，不会紧接着再跑一遍。
        """
        with self._lock:
            self._refresh()
            task = self._find(task_id)
            if task is None or task["next_run"] is None or task["next_run"] > now:
                return
            task["anchor"] = now  # 间隔从这一次运行开始数
            task["next_run"] = self._upcoming(task, now)
            self._save()
        self._notify()

    def remind(self, task_id: str, now: float, run: dict) -> dict | None:
        """提醒到时间了：记下这次运行、放进"还没关掉的提醒"里，并把下一次往后排。一口气做完、只写一次文件。"""
        with self._lock:
            self._refresh()
            task = self._find(task_id)
            if task is None:
                return None
            if task["next_run"] is not None and task["next_run"] <= now:
                task["anchor"] = now
                task["next_run"] = self._upcoming(task, now)
            run.update(status="done", finished_at=now, summary=_brief(task["prompt"] or task["title"]))
            existing = next((item for item in task["runs"] if item["id"] == run["id"]), None)
            if existing is None:
                task["runs"] = [run, *task["runs"]][:MAX_RUNS]
            else:
                existing.update(run)  # 排过队的那一条（任务在排队期间从"让助手做"改成了"只是提醒"）
            reminder = {
                "id": new_id(),
                "task_id": task_id,
                "title": task["title"],
                "text": task["prompt"],
                "at": now,
                "catch_up": run["catch_up"],
            }
            self._reminders = [reminder, *self._reminders][:MAX_REMINDERS]
            self._save()
        self._notify()
        return reminder

    def dismiss(self, reminder_id: str | None) -> None:
        """关掉一条提醒；reminder_id 为 None 时全部关掉。已经关掉了的再关一次也不算错（比如页面上连点了两下）。"""
        with self._lock:
            self._refresh()
            kept = [] if reminder_id is None else [item for item in self._reminders if item["id"] != reminder_id]
            if len(kept) == len(self._reminders):
                return
            self._reminders = kept
            self._save()
        self._notify()
