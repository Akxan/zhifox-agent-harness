"""网页界面在 agent 这一侧的部分。

agent 循环并不知道自己连着的是终端还是网页，它调用的还是那几个方法：
thinking、text、tool_call、tool_result、confirm、info。

终端界面（../ui.py）把这些调用变成屏幕上的文字。这里则把它们变成一条条"事件"存进日志，
server.py 再把日志里的事件推送给浏览器，浏览器收到后画出界面：

    agent 循环 ──调用──► WebUI ──事件──► server.py ──推送──► 浏览器

反方向（用户点了"允许"或"停止"）则是浏览器发请求给 server.py，server.py 调用这里的
answer_confirm / request_stop。

循环运行在一个后台线程里，浏览器的请求由别的线程处理，所以这里的状态都用一把锁保护。
"""

from __future__ import annotations

import threading
from bisect import bisect_right
from collections.abc import Callable
from typing import Any

from ..display import describe_call

MAX_RESULT_CHARS = 8000  # 工具结果最多给浏览器显示这么多字符（发给模型的不受影响）

# 会话文件里可以重放给页面的事件类型。别的类型（尤其是"状态"和"清空页面"）只能由程序自己发出：
# 文件万一被改坏、或者被人动过手脚，也不能借着重放去冒充它们。
REPLAYABLE = frozenset(
    {"user", "turn_start", "turn_end", "thinking", "text", "block_end", "notice", "tool_call", "tool_result", "confirm"}
)


def latest_state_only(events: list[dict]) -> list[dict]:
    """一批要一起补发给页面的事件里，"状态"只留最后一个。

    状态事件是一张快照（当前模型、用量、会话列表……），新的完全取代旧的。刚打开页面时要把日志
    从头补发一遍，里面攒了很多张旧快照，每张都带着整个会话列表；全发过去，页面也只用得上最后一张。
    聊得久了，略去它们能让刷新页面快很多。其余的事件一个不少，顺序也不变。
    """
    latest = max((index for index, event in enumerate(events) if event["type"] == "state"), default=-1)
    return [event for index, event in enumerate(events) if event["type"] != "state" or index == latest]


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
        # 编号是递增的，但不一定连续（过时的状态会被清掉，见 _drop_stale_states），所以用二分查找来定位
        return self._events[bisect_right(self._events, cursor, key=lambda event: event["id"]) :]

    def _drop_stale_states(self) -> None:
        """把日志里过时的"状态"清掉，只留最后一个。调用的人要先拿到 self._changed 这把锁。

        状态是一张快照，新的完全取代旧的，而每一张都带着整个会话列表。不清的话，一段聊了很久的会话
        会让日志越来越大，白占内存。在每轮对话开始时清：这时上一轮的状态，开着的页面早就收到了。
        """
        latest = max((index for index, event in enumerate(self._events) if event["type"] == "state"), default=-1)
        self._events = [event for index, event in enumerate(self._events) if event["type"] != "state" or index == latest]

    def reset(self) -> None:
        """开始新对话：清空日志，并通知浏览器把页面也清空。"""
        with self._changed:
            self._events.clear()
            self._block_open = False
        self.publish("reset")

    # ---- 保存和恢复（server.py 使用）---------------------------------------

    def export_events(self) -> list[dict]:
        """把当前会话显示过的内容整理成适合保存的样子。

        日志里的事件是为"实时推送"准备的，保存之前要整理一下：
          - 去掉编号（重新打开时会分配新的）；
          - 去掉只是临时显示的东西（侧栏的状态、打开会话时的提示）；
          - 逐字到达的文字合并成整段；
          - 确认请求和它的结果合成一条。
        """
        with self._changed:
            events = list(self._events)
        saved: list[dict] = []
        confirms: dict[int, dict] = {}
        for event in events:
            kind = event["type"]
            if kind in ("state", "reset") or event.get("transient"):
                continue
            if kind == "confirm_done":
                if event["confirm"] in confirms:
                    stopped = event.get("stopped")
                    confirms[event["confirm"]]["outcome"] = (
                        "allowed" if event["allowed"] else "stopped" if stopped else "denied"
                    )
                continue
            entry = {key: value for key, value in event.items() if key != "id"}
            if kind in ("thinking", "text") and saved and saved[-1]["type"] == kind:
                saved[-1]["text"] += entry["text"]
                continue
            if kind == "confirm":
                entry["outcome"] = "stopped"  # 没等到答复就结束了的，按"已中断"算
                confirms[event["id"]] = entry
            saved.append(entry)
        return saved

    def load_events(self, saved: list[dict]) -> None:
        """打开一段保存过的会话：清空页面，再把保存的内容按顺序重新发一遍。"""
        self.reset()
        open_turns = 0
        for entry in saved:
            kind = entry.get("type") if isinstance(entry, dict) else None
            if not isinstance(kind, str) or kind not in REPLAYABLE:
                continue  # 不认识的东西不重放（见 REPLAYABLE 的说明）
            # 编号由这里重新分配，文件里即使带着也不用
            data = {key: value for key, value in entry.items() if key not in ("id", "type", "outcome")}
            event_id = self.publish(kind, **data)
            if kind == "confirm":
                outcome = entry.get("outcome", "stopped")
                self.publish("confirm_done", confirm=event_id, allowed=outcome == "allowed", stopped=outcome == "stopped")
            open_turns += (kind == "turn_start") - (kind == "turn_end")
        if open_turns > 0:
            self.publish("turn_end")  # 保存的内容停在一轮中间：补上结尾，免得页面以为还在进行

    def hint(self, text: str) -> None:
        """一句只显示、不保存的提示。"""
        self.publish("notice", level="info", text=text, transient=True)

    # ---- 一轮对话的开始和结束（server.py 使用）-----------------------------

    def begin_turn(self, user_text: str) -> None:
        with self._changed:
            self._stop_requested = False
            self._drop_stale_states()
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

    def thinking(self, text: str) -> None:
        self._check_stop()
        self._block_open = True
        self.publish("thinking", text=text)

    def text(self, text: str) -> None:
        self._check_stop()
        self._block_open = True
        self.publish("text", text=text)

    def notice(self, level: str, text: str) -> None:
        self._check_stop()
        self.end_stream()
        self.publish("notice", level=level, text=text)

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
