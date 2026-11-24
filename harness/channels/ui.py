"""聊天里的一段对话用的"界面"。

agent 循环只认那几个方法（thinking、text、tool_call、confirm……，见 ../agent.py 的 UI），不知道自己连着的是
终端、网页还是 Telegram。这里和网页界面用的是同一个类（WebUI）：事件照样记成日志，一轮结束时整理成会话里
保存的样子（export_events），所以聊天里的对话在网页的会话列表里打开，和网页上聊的看起来一样。

区别是这份日志不推给浏览器，而是在几个时刻告诉 session.py 一声，由它把话发到聊天里：

  一段文字写完了（end_stream）      → 把这段文字发出去。模型边想边写，写完一段发一段，不用等整轮结束。
                                      联网搜索的回答引用了网页的（cite），文字里写上 [1]，末尾列出来源
  要你确认一个操作（confirm）       → 发一条带"允许 / 拒绝"按钮的消息，然后等答复（网页上也能答）
  确认答复了（confirm_done）        → 把那条消息上的按钮去掉，写上结果
  没问你就放行了（auto_allowed）    → 发一行"已自动允许：$ 命令"（只读的命令写"已自动允许（只读）"），在聊天里也看得到它做了什么
  提示和出错（info、error）         → 原样发一句
  交给一个子 Agent（subagent）      → 开始、没问你就放行的一步、结束时各发一行简短的进展（它在做的每一步完整地记在会话里）

思考的内容不发（太长，而且只是过程）。工具的输出也不发：完整的在会话里，模型会在回答里说结果。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .. import citations
from ..display import short
from ..i18n import t
from ..webui.ui import WebUI, fresh_ids, unsaid

# 聊天里的确认卡片，按钮上带着确认的编号（ok:编号），点了就答复这个编号的确认（router._press）。这个进程里所有聊天共用一个、
# 从随机的数起（为什么见 webui/ui.py 的 fresh_ids：旧卡片上的"允许"不能撞上新请求的编号）
CONFIRM_IDS = fresh_ids()


class ChannelUI(WebUI):
    def __init__(
        self,
        on_text: Callable[[str], None],
        on_notice: Callable[[str, str], None],
        on_confirm: Callable[[int, str, str], None],
        on_settled: Callable[[int, bool, bool], None],
        on_auto: Callable[[str, str], None],
    ):
        """on_confirm 收到（确认的编号，要做的事，哪个子 Agent 在问——主助手自己问时是空的）。"""
        super().__init__(CONFIRM_IDS)
        self._on_text = on_text
        self._on_notice = on_notice
        self._on_confirm = on_confirm
        self._on_settled = on_settled
        self._on_auto = on_auto
        self._pending = ""  # 正在写、还没发出去的那段文字
        self._chat_sources = citations.Numbering()  # 这段文字引用的网页（联网搜索的回答才有），发出去时列在末尾
        self.activity = ""  # 现在在做什么（最近一次工具调用的一行说明），/status 用
        self.answer = ""  # 最后一次调用工具之后写的文字
        self._replaying = False  # 正在把保存过的会话重放进日志（load_events）：重放出来的确认请求不能再发到聊天里
        self._sub_doing: dict[str, str] = {}  # 每个子 Agent（按这一次交给它的编号）正在调用的工具，"已自动允许"那一行要写是哪一步

    def publish(self, type: str, **data: Any) -> int:
        """记下事件（父类做的），再看看是不是要告诉 session 一声。

        confirm 这个事件是 WebUI.confirm 拿着它自己的锁时发的，所以这里的回调不能做慢的事、更不能反过来找 WebUI：
        session 收到以后只是把"发一条确认请求"排进发送队列（见 router.Outbox），马上返回。
        """
        event_id = super().publish(type, **data)
        if self._replaying:
            return event_id
        if type == "confirm":
            # 交给 session 的是带着原文的那句话（unsaid：和网页的确认卡片同一份原文，里面填进去的几句也在）：
            # 聊天里的卡片按你选的界面语言写，换了语言以后，页面上"在等你批准"、/status 里的那一句也跟着换
            self._on_confirm(event_id, unsaid(data["description"], data.get("key"), data.get("values")), data.get("agent", ""))
        elif type == "confirm_done":
            self._on_settled(data["confirm"], data["allowed"], data.get("stopped", False))
        elif type == "tool_call":
            self.activity = data["summary"]
            self.answer = ""
        elif type == "subagent":
            self._subagent(data)
        return event_id

    def _subagent(self, data: dict) -> None:
        """子 Agent 的一步进展（WebUI.subagent 发的事件）。聊天里只说三件事：交给了谁、哪一步没问你就放行了、做完了没有。"""
        step, name, run = data.get("step"), str(data.get("agent", "")), str(data.get("run", ""))
        if step == "start":
            self._on_notice("info", t("交给「{agent}」：{task}", agent=name, task=short(str(data.get("task", "")), 200)))
        elif step == "tool_call":
            self._sub_doing[run] = str(data.get("summary", ""))
            self.activity = t("「{agent}」：{summary}", agent=name, summary=data.get("summary", ""))
        elif step == "auto":
            self._on_auto(t("「{agent}」：{summary}", agent=name, summary=self._sub_doing.get(run, "")), str(data.get("why", "mode")))
        elif step == "done":
            self._sub_doing.pop(run, None)
            status = data.get("status")
            if status == "failed":
                error = data.get("error")
                self._on_notice("error", t("「{agent}」出错了：{error}", agent=name, error=error.get("text", "") if isinstance(error, dict) else error or ""))
            elif status == "stopped":
                self._on_notice("info", t("「{agent}」被中断了。", agent=name))
            else:
                self._on_notice("info", t("「{agent}」做完了，回答交给了主助手。", agent=name))

    def load_events(self, saved: list[dict]) -> None:
        """接着一段保存过的会话聊：把它的事件放回日志（之后存盘时才不会丢了以前的），但不往聊天里发任何东西。"""
        self._replaying = True
        try:
            super().load_events(saved)
        finally:
            self._replaying = False

    def begin_turn(self, user_text: str, attachments=(), mark: int | None = None, voice: float | None = None) -> None:
        self.activity = ""
        self.answer = ""
        super().begin_turn(user_text, attachments, mark=mark, voice=voice)

    def text(self, text: str) -> None:
        super().text(text)
        self._pending += text
        self.answer += text

    def cite(self, sources: list[dict]) -> None:
        """联网搜索的回答引用了网页（见 citations.py）：网页上照常画（父类发事件），聊天里在那段文字后面写上 [1]，
        这条消息的末尾列出来源。编号一条消息一数：聊天里每段文字各发成一条消息，和网页上的段落不一定一样长"""
        super().cite(sources)
        marks, _ = self._chat_sources.add(sources)
        if marks:
            self._pending = citations.insert(self._pending, citations.marker(marks))

    def end_stream(self) -> None:
        super().end_stream()
        if self._pending.strip():
            if self._chat_sources.sources:
                # 来源跟在这段文字后面、发在同一条消息里。太长的话 router 照常按平台的上限拆成几条（见 markup.split）
                self._pending = self._pending.rstrip() + "\n\n" + citations.listing(self._chat_sources.sources, markdown=True)
            self._on_text(self._pending)
        self._pending = ""
        self._chat_sources.clear()

    def auto_allowed(self, why: str = "mode") -> None:
        super().auto_allowed(why)
        self._on_auto(self.activity, why)

    def info(self, text: str) -> None:
        super().info(text)
        self._on_notice("info", text)

    def error(self, text: str) -> None:
        super().error(text)
        self._on_notice("error", text)

    def notice(self, level: str, text: str) -> None:
        super().notice(level, text)
        self._on_notice(level, text)
