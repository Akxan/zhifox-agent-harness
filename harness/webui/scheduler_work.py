from __future__ import annotations

import threading
from typing import Any

from ..app import run_turn
from ..display import short
from ..i18n import k, t
from ..pricing import estimate
from ..providers import has_key
from ..tasks import local_text, new_run, said
from .scheduler_run import Pending, RunUI, instruction
from .scheduler_types import SchedulerPart
from .ui import unsaid

MAX_LOG = 30  # 进度里最多列出这么多次工具调用（最近的）
SUMMARY_CHARS = 120
TITLE_CHARS = 21  # 会话标题里任务名称最多占这么多字（后面还有" · 2026-11-21 09:00"）

WAIT_CONFIRM = k("等你批准")


class SchedulerWork(SchedulerPart):
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
                # 变回带着原文的那句话（里面填进去的那几句也一样，见 ui.unsaid）：页面上换了语言，进度里的确认跟着重译
                question = unsaid(data["description"], data.get("key"), data.get("values"))
                if data.get("agent"):  # 是子 Agent 在问：确认上写明是它（问题原样填进去，换语言时里外两层都重译）
                    question = t("「{agent}」：{summary}", agent=data["agent"], summary=question)
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
