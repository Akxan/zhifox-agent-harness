from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from .. import i18n
from ..app import retry_turn, run_turn
from ..attachments import Attachment, AttachmentError
from ..attachments import parse as parse_attachments
from ..i18n import t
from ..providers import has_key
from ..tools.shell import TOOL_NAME as SHELL_TOOL
from ..undo.journal import Journal, recording

if TYPE_CHECKING:
    from .server import Handler
    from .webapp import WebApp


MAX_MESSAGE_CHARS = 100_000


class ChatActions:
    # ---- 对话 --------------------------------------------------------------

    def send(self, text: str, attachments: list[Attachment] | None = None) -> str:
        """开始一轮对话。成功返回空字符串，否则返回不能开始的原因。"""
        provider = self.app.provider
        if not has_key(provider):
            return t("还没有设置 {provider} 的 API 密钥，请先点右上角的“设置”填写。", provider=provider.label)
        if not self._turn.acquire(blocking=False):
            return self._busy(t("上一轮对话还没有结束"))
        elsewhere = self._claim()
        if elsewhere:
            self._turn.release()
            return elsewhere
        # agent 循环是阻塞的（等模型、等用户确认），所以放到后台线程里跑，
        # 这样服务器还能继续响应浏览器的其他请求，比如"允许"和"停止"。
        threading.Thread(target=self._run_turn, args=(text, attachments or []), daemon=True).start()
        return ""

    def _run_turn(self, text: str, attachments: list[Attachment], again: int | None = None) -> None:
        """跑一轮。again：重新生成时，这一轮开始的位置（见 rewind）。那条用户消息已经在页面上了，不再显示一遍；
        它还在历史里的话原样留着，只重跑模型的那部分（Agent.retry_turn），不在的话（上一次在模型答出第一步之前就失败了）
        用同样的文字和附件再发一次。

        这一轮里文件工具改的文件记在一本账上（见 undo/journal.py），一轮结束时合上，页面上这一轮末尾多一行"改了 N 个文件"。"""
        journal = self.app.changes.start()
        start = None  # 用户消息放在历史的哪个位置（这一轮发了新的用户消息才有）：附上的"文件撤回过"的说明有没有留住，看它
        try:
            self._discard_ok = ""  # 接着聊了：之前那句"再点一次就放弃"不再算数
            self.app.name_conversation(text, attachments)  # 新会话用第一句话当标题（没写字的话用第一个附件的名字）
            backend = self.app.agent.backend
            if again is None:
                attachments = self._keep_originals(attachments)  # 原件存进多媒体库，概要里记上编号
                # 记下这一轮从历史的哪个位置开始：以后编辑这条消息、重新生成回答，从这里倒回去
                self.ui.begin_turn(text, attachments, mark=backend.mark())
            else:
                self.ui.begin_again()
            self.publish_state()  # 让新会话马上出现在列表里
            with recording(journal):
                if again is not None and backend.turn_at(again):
                    retry_turn(self.app.agent, self.ui, again)
                else:
                    start = backend.mark()
                    run_turn(self.app.agent, self.ui, text, attachments, again=again is not None)
        finally:
            try:
                self.ui.end_stream()
                self._finish_changes(journal, start)
                self._save()  # 每一轮结束都存一次盘，所以最多只会丢失正在进行的这一轮
                self.publish_state()
            finally:
                try:
                    # 这一轮结束：屏幕让出来，还按着的鼠标键松开（见 desktop/session.py）。让出屏幕会刷新一次状态，
                    # 所以放在通知页面之前：放在后面的话，页面收到"结束了"马上发来的请求（比如开新对话）会撞上还拿着的锁
                    self.app.chat_desktop.release()
                    self.claims.release(self.app.conversation.id, "web")
                finally:
                    # "这一轮结束了"要等存盘和刷新状态都做完、马上就要放开锁的时候才通知页面。
                    # 如果先通知再存盘，页面一收到通知就发来的请求会撞上还没放开的锁，被误判成"上一轮还没结束"。
                    self.ui.end_turn()
                    self._turn.release()

    def _finish_changes(self, journal: Journal, start: int | None) -> None:
        """一轮结束：合上记账的本子，这一轮改了文件的话发一个 file_changes 事件（页面上那一行"改了 N 个文件"，见 undo/）。
        这一轮的消息前面附过"文件撤回过"的说明、而且那条消息留在了历史里，这段说明就不用再说了（见 undo/notes.py）。"""
        try:
            _, events = self.ui.last_turn_events()
            ran = any(
                (event.get("type") == "tool_call" or (event.get("type") == "subagent" and event.get("step") == "tool_call")) and event.get("name") == SHELL_TOOL
                for event in events
            )
            summary = self.app.changes.finish(journal, commands=ran)
            if summary is not None:
                self.ui.publish("file_changes", **summary)
            if start is not None:
                self.app.changes.notes.settle(self.app.conversation.id, sent=self.app.agent.backend.turn_at(start))
        except Exception as exc:  # noqa: BLE001 —— 这是在一轮收尾时调用的：出了什么错都不能耽误后面的存盘
            self.ui.error(t("没能记下这一轮改了哪些文件（{error}），这一轮的改动没法一键撤回。", error=str(exc) or type(exc).__name__))


def route_message(handler: Handler, web: WebApp, body: dict) -> None:
    text = body.get("text")
    try:
        # 附件逐个检查：个数、大小、格式、编码（见 attachments.py）。有一个不合格，整条消息都不发，
        # 免得用户以为模型看到了其实被丢掉的那个文件
        attachments = parse_attachments(body.get("attachments"))
    except AttachmentError as exc:
        handler._send_json(400, {"error": i18n.message_of(exc)})
        return
    if not isinstance(text, str) or len(text) > MAX_MESSAGE_CHARS or not (text.strip() or attachments):
        handler._send_json(400, {"error": t("消息不能为空，也不能太长")})  # 只发附件、不写字是可以的
        return
    # 当前的模型收不了的附件（DeepSeek 看不了图片，只有 Claude 读得了 PDF），在开始这一轮之前就退回去
    problem = web.app.attachment_problem(attachments)
    if problem:
        handler._send_json(400, {"error": problem})
    else:
        handler._finish(web.send(text.strip(), attachments))


def route_confirm(handler: Handler, web: WebApp, body: dict) -> None:
    confirm_id, allow, feedback = body.get("id"), body.get("allow"), body.get("feedback", "")
    if not isinstance(confirm_id, int) or not isinstance(allow, bool) or not isinstance(feedback, str):
        handler._send_json(400, {"error": t("参数不正确")})
    elif web.ui.answer_confirm(confirm_id, allow, feedback.strip()):
        handler._send_json(200, {"ok": True})
    else:
        handler._send_json(409, {"error": t("这个确认请求已经处理过了")})
