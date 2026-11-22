"""评测用的"界面"：不显示任何东西，只把发生的每一件事按顺序记下来，并替用户回答确认。

循环（agent.py）只认界面的那几个方法（UI、StreamUI，加上子 Agent 要的 subagent、check_stop、on_stop），
不关心背后是终端、网页还是这里。所以评测跑的是和你平时一模一样的循环，只是"屏幕"换成了这本记录：

  - 记下的每一件事是一项 {"role": ……}：user（你说的话）、assistant（它说的话，thinking 是它的思考摘要）、
    tool_call / tool_result（调用了什么工具、结果是什么；子 Agent 的带着 by）、confirm（问了你什么、怎么答的）、
    auto_allowed（本来要问、但这一档放行了）、info（循环自己的提示：被截断了、步数用完了……）。
    这和 claude-api 的评测报告用的格式一样（role 是 system | user | assistant | tool_call | tool_result），
    多出来的几种它不认识也不要紧。打分（graders.py）和存下来的记录（results.py）都用它；
  - 要你确认时，按任务写的 answer 回答：approve 一律允许，deny 一律拒绝（"删 build 文件夹"那种任务就是看
    被拒绝以后它会不会换个办法绕过去）。问了几次也记着，approvals 那项检查要用；
  - 一个任务跑得太久（EVAL_TASK_SECONDS），由外面调用 expire：立起"停下"的旗子，下一次循环调用这里的任何方法时
    抛出 KeyboardInterrupt（和网页上点"停止"是同一个办法，见 webui/ui.py 的 request_stop），
    正在运行的命令也一起结束（runner.py 把 shell.kill_running 登记在 on_stop 上）。
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Callable, Sequence
from typing import Any

from ..attachments import Attachment

RESULT_CHARS = 20_000  # 记下的一个工具结果最多这么多字（读了一个大文件的话，记录不至于几兆）


def _pretty(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(value)


class Recorder:
    def __init__(self, answer: str = "approve"):
        self.answer = answer
        self.events: list[dict] = []
        # 每一轮助手说的全部的话（调用工具前后写的都算：它可能先回答、再去存一条记忆）。reply_contains 看它
        self.turns: list[str] = []
        self.timed_out = False
        self._text = ""
        self._thinking = ""
        self._stop = threading.Event()
        self._callbacks: list[Callable[[], None]] = []
        self._lock = threading.Lock()

    # ---- 一轮的开始和结束（runner.py 调用）--------------------------------

    def begin_turn(self, text: str, attachments: Sequence[Attachment] = ()) -> None:
        self._flush()
        event: dict[str, Any] = {"role": "user", "content": text}
        if attachments:
            event["attachments"] = [{"name": item.name, "kind": item.kind} for item in attachments]
        self.events.append(event)
        self.turns.append("")

    def end_turn(self) -> None:
        self._flush()

    def _flush(self) -> None:
        """攒着的一段话写成一项。思考跟着它后面的那一项（这段话，或者接下来的工具调用）。"""
        if self._text.strip():
            event: dict[str, Any] = {"role": "assistant", "content": self._text}
            if self._thinking.strip():
                event["thinking"], self._thinking = self._thinking, ""
            self.events.append(event)
        self._text = ""

    # ---- 停下 ---------------------------------------------------------------

    def expire(self) -> None:
        """时间到了（见文件开头）。"""
        self.timed_out = True
        self.request_stop()

    def request_stop(self) -> None:
        self._stop.set()
        with self._lock:
            callbacks = list(self._callbacks)
        for callback in callbacks:
            with contextlib.suppress(Exception):  # 停不下某一样（进程已经没了）也要接着停别的
                callback()

    def check_stop(self) -> None:
        if self._stop.is_set():
            raise KeyboardInterrupt

    def on_stop(self, callback: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            self._callbacks.append(callback)
        if self._stop.is_set():
            callback()

        def forget() -> None:
            with self._lock:
                if callback in self._callbacks:
                    self._callbacks.remove(callback)

        return forget

    # ---- 适配层逐字交来的东西（StreamUI）-----------------------------------

    def thinking(self, text: str) -> None:
        self.check_stop()
        self._thinking += text

    def text(self, text: str) -> None:
        self.check_stop()
        self._text += text
        if self.turns:
            self.turns[-1] += text

    def cite(self, sources: list[dict]) -> None:
        pass  # 联网搜索的引用：评测里不打分，记录里也不需要

    def notice(self, level: str, text: str) -> None:
        self.check_stop()
        self._flush()
        self.events.append({"role": "info", "level": level, "content": str(text)})

    def end_stream(self) -> None:
        self._flush()

    def info(self, text: str) -> None:
        self._flush()
        self.events.append({"role": "info", "content": str(text)})

    # ---- 工具和确认（UI）---------------------------------------------------

    def tool_call(self, name: str, tool_input: object) -> None:
        self.check_stop()
        self._flush()
        event: dict[str, Any] = {"role": "tool_call", "name": name, "input": tool_input, "content": _pretty(tool_input)}
        if self._thinking.strip():
            event["thinking"], self._thinking = self._thinking, ""
        self.events.append(event)

    def tool_result(self, text: str, is_error: bool, preview: dict | None = None) -> None:
        self.events.append({"role": "tool_result", "content": str(text)[:RESULT_CHARS], "is_error": is_error})
        self.check_stop()

    def confirm(self, description: str, agent: str = "") -> tuple[bool, str]:
        self._flush()
        allowed = self.answer == "approve"
        event = {"role": "confirm", "content": str(description), "answer": "approve" if allowed else "deny"}
        if agent:
            event["by"] = agent
        self.events.append(event)
        self.check_stop()
        return allowed, ""

    def auto_allowed(self, why: str = "mode") -> None:
        self.events.append({"role": "auto_allowed", "why": why})

    # ---- 子 Agent（见 agents/runner.py）------------------------------------

    def subagent(self, step: str, **data: Any) -> None:
        if step != "done":
            self.check_stop()
        agent = str(data.get("agent", ""))
        if step == "start":
            self.events.append({"role": "subagent", "by": agent, "content": str(data.get("task", ""))})
        elif step == "tool_call":
            tool_input = data.get("input")
            self.events.append({"role": "tool_call", "name": data.get("name"), "input": tool_input, "content": _pretty(tool_input), "by": agent})
        elif step == "tool_result":
            self.events.append({"role": "tool_result", "content": str(data.get("text", ""))[:RESULT_CHARS], "is_error": bool(data.get("is_error")), "by": agent})
        elif step == "auto":
            self.events.append({"role": "auto_allowed", "why": data.get("why", "mode"), "by": agent})
        elif step == "done":
            self.events.append({"role": "subagent_done", "by": agent, "status": data.get("status", "")})
