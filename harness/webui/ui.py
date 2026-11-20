"""网页界面在 agent 这一侧的部分。

agent 循环并不知道自己连着的是终端还是网页，它调用的还是那几个方法：
stream_event、tool_call、tool_result、confirm、info。

终端界面（../ui.py）把这些调用变成屏幕上的文字。这里则把它们变成一条条"事件"存进日志，
server.py 再把日志里的事件推送给浏览器，浏览器收到后画出界面：

    agent 循环 ──调用──► WebUI ──事件──► server.py ──推送──► 浏览器

反方向（用户点了"允许"或"停止"）则是浏览器发请求给 server.py，server.py 调用这里的
answer_confirm / request_stop。

循环运行在一个后台线程里，浏览器的请求由别的线程处理，所以这里的状态都用一把锁保护。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from ..ui import block_notice, describe_call

MAX_RESULT_CHARS = 8000  # 工具结果最多给浏览器显示这么多字符（发给模型的不受影响）


class WebUI:
    def __init__(self) -> None:
        # 这把锁保护下面所有的状态；有新事件、新答复或停止请求时，用它唤醒正在等待的线程
        self._changed = threading.Condition()
        # 当前对话的事件日志。浏览器刷新页面后，靠重放这份日志把整个对话重新画出来
        self._events: list[dict] = []
        self._next_id = 1
        self._block_open = False  # 是否有一段文字正在逐字输出
        self._stop_requested = False
        self._stop_callbacks: list[Callable[[], None]] = []
        self._waiting_confirm: int | None = None  # 正在等用户答复的确认请求的编号
        self._confirm_answer: tuple[bool, str] | None = None

    # ---- 事件日志（server.py 使用）-----------------------------------------

    def publish(self, type: str, **data: Any) -> int:
        """往日志里追加一个事件，并通知所有正在等新事件的浏览器连接。返回事件编号。"""
        with self._changed:
            event = {"id": self._next_id, "type": type, **data}
            self._next_id += 1
            self._events.append(event)
            self._changed.notify_all()
            return event["id"]

    def events_after(self, cursor: int, timeout: float) -> list[dict]:
        """返回编号大于 cursor 的事件。暂时没有的话最多等 timeout 秒。"""
        with self._changed:
            if not self._newer(cursor):
                self._changed.wait(timeout)
            return self._newer(cursor)

    def _newer(self, cursor: int) -> list[dict]:
        if not self._events:
            return []
        if cursor >= self._next_id:  # 浏览器记住的编号比我们的还新，说明对不上了，全部重发
            cursor = 0
        first = self._events[0]["id"]
        return self._events[max(0, cursor - first + 1) :]

    def reset(self) -> None:
        """开始新对话：清空日志，并通知浏览器把页面也清空。"""
        with self._changed:
            self._events.clear()
            self._block_open = False
        self.publish("reset")

    # ---- 一轮对话的开始和结束（server.py 使用）-----------------------------

    def begin_turn(self, user_text: str) -> None:
        with self._changed:
            self._stop_requested = False
        self.publish("user", text=user_text)
        self.publish("turn_start")

    def end_turn(self) -> None:
        self.end_stream()
        self.publish("turn_end")

    # ---- 停止 --------------------------------------------------------------

    def on_stop(self, callback: Callable[[], None]) -> None:
        """登记一个函数，用户点"停止"时调用它（比如结束正在运行的命令）。"""
        self._stop_callbacks.append(callback)

    def request_stop(self) -> None:
        """用户点了"停止"。

        循环运行在另一个线程里，不能从外面直接打断它。所以这里只是立起一面旗子，
        循环下一次调用界面的方法时会看到旗子，由那个方法抛出 KeyboardInterrupt，
        效果和在终端里按 Ctrl-C 一样，agent.py 里处理中断的代码不用改。
        """
        with self._changed:
            self._stop_requested = True
            self._changed.notify_all()  # 如果循环正卡在等用户确认，把它叫醒
        for callback in self._stop_callbacks:
            callback()

    def _check_stop(self) -> None:
        if self._stop_requested:
            raise KeyboardInterrupt

    # ---- agent 循环调用的方法（和 TerminalUI 是同一套）---------------------

    def stream_event(self, event: Any) -> None:
        self._check_stop()
        if event.type == "thinking":
            self._block_open = True
            self.publish("thinking", text=event.thinking)
        elif event.type == "text":
            self._block_open = True
            self.publish("text", text=event.text)
        elif event.type == "content_block_stop":
            block = event.content_block
            if block.type in ("thinking", "text"):
                self.end_stream()
            else:
                notice = block_notice(block)
                if notice is not None:
                    self.publish("notice", level=notice[0], text=notice[1])

    def end_stream(self) -> None:
        """告诉浏览器：正在逐字输出的那段文字结束了。"""
        if self._block_open:
            self._block_open = False
            self.publish("block_end")

    def tool_call(self, name: str, tool_input: object) -> None:
        self._check_stop()
        self.end_stream()
        self.publish("tool_call", name=name, summary=describe_call(name, tool_input), input=tool_input)

    def tool_result(self, text: str, is_error: bool) -> None:
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + f"\n……（共 {len(text)} 个字符，这里只显示开头）"
        self.publish("tool_result", text=text, is_error=is_error)
        self._check_stop()

    def confirm(self, description: str) -> tuple[bool, str]:
        """请用户确认一个操作：发一个事件让浏览器显示确认卡片，然后等用户点按钮。"""
        self.end_stream()
        with self._changed:
            confirm_id = self.publish("confirm", description=description)
            self._waiting_confirm = confirm_id
            self._confirm_answer = None
            while self._confirm_answer is None and not self._stop_requested:
                self._changed.wait()
            answer = self._confirm_answer
            self._waiting_confirm = None
        if answer is None:  # 等待期间用户点了"停止"
            self.publish("confirm_done", confirm=confirm_id, allowed=False, stopped=True)
            raise KeyboardInterrupt
        allowed, feedback = answer
        self.publish("confirm_done", confirm=confirm_id, allowed=allowed, stopped=False)
        return allowed, feedback

    def answer_confirm(self, confirm_id: int, allowed: bool, feedback: str) -> bool:
        """浏览器送来了用户的答复。返回 False 表示这个确认请求已经不在等待了。"""
        with self._changed:
            if self._waiting_confirm != confirm_id or self._confirm_answer is not None:
                return False
            self._confirm_answer = (allowed, feedback)
            self._changed.notify_all()
            return True

    def info(self, text: str) -> None:
        self.end_stream()
        self.publish("notice", level="info", text=text)

    def error(self, text: str) -> None:
        self.end_stream()
        self.publish("notice", level="error", text=text)
