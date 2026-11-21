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
import time
from bisect import bisect_right
from collections.abc import Callable, Sequence
from typing import Any

from ..attachments import Attachment, from_saved
from ..display import describe_call
from ..i18n import Message, t

MAX_RESULT_CHARS = 8000  # 工具结果最多给浏览器显示这么多字符（发给模型的不受影响）
MAX_ANSWER_CHARS = 40_000  # 子 Agent 的回答最多显示这么多（它也会作为工具结果交给主助手，见 agents/runner.py）

# 会话文件里可以重放给页面的事件类型。别的类型（尤其是"状态"和"清空页面"）只能由程序自己发出：
# 文件万一被改坏、或者被人动过手脚，也不能借着重放去冒充它们。
REPLAYABLE = frozenset(
    {
        "user",
        "turn_start",
        "turn_end",
        "thinking",
        "text",
        "block_end",
        "notice",
        "tool_call",
        "tool_result",
        "confirm",
        "auto_allowed",
        "subagent",
    }
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

    def forget_old_states(self) -> None:
        """现在就把过时的"状态"清掉（平时是每轮对话开始时清）。

        定时任务运行时，进度每秒要刷新好几次，每次都是一张新的状态快照；这期间你没在对话，
        等不到下一轮开始时再清，所以由运行任务的那一边每发一次就清一次（见 scheduler.py）。
        """
        with self._changed:
            self._drop_stale_states()

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
            if kind == "user" and "attachments" in data:
                # 附件的概要会变成页面上的预览图和文件名，所以和浏览器送来的一样，重新查一遍（见 attachments.py）
                shown = from_saved(data.pop("attachments"))
                if shown:
                    data["attachments"] = shown
            event_id = self.publish(kind, **data)
            if kind == "confirm":
                outcome = entry.get("outcome", "stopped")
                self.publish("confirm_done", confirm=event_id, allowed=outcome == "allowed", stopped=outcome == "stopped")
            open_turns += (kind == "turn_start") - (kind == "turn_end")
        if open_turns > 0:
            self.publish("turn_end")  # 保存的内容停在一轮中间：补上结尾，免得页面以为还在进行

    def user_events(self) -> list[dict]:
        """当前会话里用户发的每一条消息（日志里的事件本身，不复制）。多媒体库靠它列出当前这段会话里的附件：
        这段会话可能还没存进文件（第一轮还没结束，或者存盘失败了），文件里的就不全。"""
        with self._changed:
            return [event for event in self._events if event["type"] == "user"]

    def hint(self, text: str, level: str = "info") -> None:
        """一句只显示、不保存的提示。"""
        self.publish("notice", level=level, transient=True, **said(text))

    # ---- 一轮对话的开始和结束（server.py 使用）-----------------------------

    def begin_turn(self, user_text: str, attachments: Sequence[Attachment] = ()) -> None:
        with self._changed:
            self._stop_requested = False
            self._drop_stale_states()
        # 附件只放概要（名字、大小、小预览图、原件的编号）：事件会存进会话文件、每次刷新都要重发，放不下几十 MB 的原件。
        # 原件在多媒体库里（见 media.py），也在对话历史里（发给模型用）。
        # at：什么时候发的，多媒体库按它排序、分组（"今天""昨天"）
        extra = {"attachments": [item.summary() for item in attachments], "at": round(time.time(), 3)} if attachments else {}
        self.publish("user", text=user_text, **extra)
        self.publish("turn_start")

    def end_turn(self) -> None:
        self.end_stream()
        self.publish("turn_end")

    # ---- 停止 --------------------------------------------------------------

    def on_stop(self, callback: Callable[[], None]) -> Callable[[], None]:
        """登记一个函数，用户点"停止"时调用它（比如结束正在运行的命令）。返回的函数用来取消登记：
        子 Agent 只在它运行的那一会儿登记（见 agents/runner.py），做完就取消。"""
        with self._changed:
            self._stop_callbacks.append(callback)

        def forget() -> None:
            with self._changed:
                if callback in self._stop_callbacks:
                    self._stop_callbacks.remove(callback)

        return forget

    def request_stop(self) -> None:
        """用户点了"停止"。

        循环运行在另一个线程里，不能从外面直接打断它。所以这里只是立起一面旗子，
        循环下一次调用界面的方法时会看到旗子，由那个方法抛出 KeyboardInterrupt，
        效果和在终端里按 Ctrl-C 一样，agent.py 里处理中断的代码不用改。
        """
        with self._changed:
            self._stop_requested = True
            self._changed.notify_all()  # 如果循环正卡在等用户确认，把它叫醒
            callbacks = list(self._stop_callbacks)  # 抄一份再调用：子 Agent 可能正好在这时取消登记
        for callback in callbacks:
            callback()

    def _check_stop(self) -> None:
        if self._stop_requested:
            raise KeyboardInterrupt

    def check_stop(self) -> None:
        """用户点了"停止"的话抛出 KeyboardInterrupt。子 Agent 逐字输出时用它看一眼（见 agents/runner.py 的 SubUI）。"""
        self._check_stop()

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
        self.publish("notice", level=level, **said(text))

    def end_stream(self) -> None:
        """告诉浏览器：正在逐字输出的那段文字结束了。"""
        if self._block_open:
            self._block_open = False
            self.publish("block_end")

    def tool_call(self, name: str, tool_input: object) -> None:
        self._check_stop()
        self.end_stream()
        self.publish("tool_call", name=name, summary=describe_call(name, tool_input), input=tool_input)

    def auto_allowed(self) -> None:
        """刚才那次工具调用本来要问用户，但用户设置了自动允许。页面会在那张工具卡片上标出来。"""
        self.publish("auto_allowed")

    def tool_result(self, text: str, is_error: bool, preview: dict | None = None) -> None:
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "\n" + t("……（共 {n} 个字符，这里只显示开头）", n=len(text))
        # 浏览器的截图：小图（几 KB 的 data: 地址）放进事件，画在工具卡片上；大图只给编号，点开时再要（见 browser/shots.py）
        shot = shot_fields(preview)
        self.publish("tool_result", text=text, is_error=is_error, **shot)
        self._check_stop()

    def confirm(self, description: str, agent: str = "") -> tuple[bool, str]:
        """请用户确认一个操作：发一个事件让浏览器显示确认卡片，然后等用户点按钮。
        agent 是哪个子 Agent 在问（卡片上写"某某想要"而不是"助手想要"）；主助手自己问时是空的。"""
        self.end_stream()
        with self._changed:
            fields = said(description)
            if agent:
                fields["agent"] = agent
            if getattr(description, "offer", "") in OFFERS:
                # 卡片上多给的一个选择（见 permissions.offering）。只给你正在看的这段对话：子 Agent 问的也是这段对话的电脑
                fields["offer"] = description.offer
            if getattr(description, "always", False) is True:
                fields["always"] = True  # 在任何模式下都要问（见 permissions.always）：页面不放"不想每次都问？"
            confirm_id = self.publish("confirm", description=fields.pop("text"), **fields)
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
        self.publish("notice", level="info", **said(text))

    def subagent(self, step: str, **data: Any) -> None:
        """子 Agent 的一步进展（见 agents/runner.py）。页面把同一个 run 的进展画进同一张"交给 某某"的卡片。

        step：start 开始了（agent、color、model、task）、activity 在想 / 在写（只是此刻的样子，不存进会话）、
        tool_call / tool_result / auto 它的一次工具调用、note 一句提示、done 结束了（status、answer、cost、steps、calls、error）。
        除了 done，每一步都先看一眼要不要停：done 是停下之后才发的，要让页面知道它停了。
        """
        if step != "done":
            self._check_stop()
        self.end_stream()
        fields = dict(data)
        if "text" in fields:
            if step == "tool_result" and len(fields["text"]) > MAX_RESULT_CHARS:
                fields["text"] = fields["text"][:MAX_RESULT_CHARS] + "\n" + t("……（共 {n} 个字符，这里只显示开头）", n=len(fields["text"]))
            fields.update(said(fields.pop("text")))
        if fields.get("error"):
            fields["error"] = said(fields["error"])  # 出错的原因：换语言时页面照着原文重译
        if step == "done" and len(fields.get("answer", "")) > MAX_ANSWER_CHARS:
            fields["answer"] = fields["answer"][:MAX_ANSWER_CHARS] + "\n\n" + t("……（回答太长，这里只显示开头）")
        if step == "activity":
            fields["transient"] = True
        self.publish("subagent", step=step, **fields)

    def error(self, text: str) -> None:
        self.end_stream()
        self.publish("notice", level="error", **said(text))


# 确认卡片上可以多给的选择：screenshots 是"允许，这段对话里截图不再问"（操作整台电脑，见 permissions.py）
OFFERS = frozenset({"screenshots"})
THUMB_PREFIX = "data:image/jpeg;base64,"
MAX_THUMB_CHARS = 60_000  # 小图最多这么大（正常的只有几 KB）。再大的不放进事件：每次刷新页面都要重发


def shot_fields(preview: dict | None) -> dict:
    """工具结果里给页面看的截图：{"thumb": 小图, "shot": 大图的编号}。格式不对的一律不要。"""
    if not preview:
        return {}
    thumb, shot = preview.get("thumb"), preview.get("shot")
    if not isinstance(thumb, str) or not thumb.startswith(THUMB_PREFIX) or len(thumb) > MAX_THUMB_CHARS:
        return {}
    fields = {"thumb": thumb}
    if isinstance(shot, str) and len(shot) == 64 and all(char in "0123456789abcdef" for char in shot):
        fields["shot"] = shot
    return fields


def said(text: str) -> dict:
    """一句要显示在页面上的话，变成事件里的字段。

    除了译好的文字（text），用 t() 写的话还带着原文（key）和填进去的值（values）：用户在页面上换语言时，
    页面照着它们在原地把这句话换成新的语言；保存下来的会话以后换了语言再打开，也一样。
    """
    if isinstance(text, Message):
        return {"text": str(text), "key": text.key, "values": page_values(text)}
    return {"text": text}


def page_values(message: Message) -> dict:
    """一句话填进去的值，给页面的样子：本身也是一句 t() 译好的话的，换成 {"key": 原文, "values": …}（一层层往里），
    页面换语言时照着它把里面那一句也重译（见 i18n.Message 和页面上的 t）。"""
    return {
        name: {"key": message.nested[name].key, "values": page_values(message.nested[name])} if name in message.nested else value
        for name, value in message.values.items()
    }
