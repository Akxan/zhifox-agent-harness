from __future__ import annotations

from ..config import APP_NAME
from ..i18n import t
from ..providers import has_key
from ..tasks import TaskError, TaskRefused, new_run, said
from ..tasks import preview as preview_schedule
from .scheduler_types import SchedulerPart


class SchedulerActions(SchedulerPart):
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
