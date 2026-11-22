"""网页上"从这里分叉"背后的接口：POST /api/fork，和状态里的 fork（规矩见 ../fork.py）。

  POST /api/fork   conversation：页面看到的那段会话；turn：那一轮的 user 事件在页面的事件日志里的编号；
                   at：reply（在回答上分叉，带着这一轮）/ message（在你的消息上分叉，截到这一轮之前）。
                   做成了回答 {ok, conversation: 新会话的编号, draft: 放回输入框的文字, attachments: 那条消息的附件在多媒体库里的编号}，
                   新的一段已经换上了（和打开一段会话一样：reset、重放、状态），页面不用再打开它。

页面只给编号：哪一轮由这边对着事件日志找（和倒回最后一轮一样，见 rewind_api.py），截到哪、复制什么由 fork.py 照着会话文件算。
编号对不上（另一个标签页刚倒回过、已经换了一段会话）就回答 409，不猜。

分叉会换到另一段会话上，所以和打开一段会话一样（见 conversations_api.py 和 sessions.py）：拿 _switch，再试一下你正看着的那一段的锁。
它没在跑：就在它里面换成分叉出来的那一段（和以前一样）。它在跑一轮：它留在后台接着跑，分叉出来的那一段另外建一个给你看。
原会话只是读：不认领它（正被聊天通道占着也能分叉），读的是存在磁盘上的样子（拿着会话库的锁，见 App.fork_conversation）。
所以这段会话上一次存盘失败了的话不做：磁盘上的和页面上看到的不一样。它正在跑的这一轮还没存盘，只能从之前的那几轮分叉
（选的是这一轮的话，磁盘上对不上，回答"变过了"）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import fork, rewind
from ..conversations import FORK_POINTS, ConversationError
from ..fork import ForkError
from ..i18n import t
from .ui import said

if TYPE_CHECKING:
    from .server import WebApp
    from .sessions import Session


class ForkAPI:
    def __init__(self, web: WebApp):
        self.web = web

    def state(self) -> dict:
        """状态里的 fork：页面照着它画每一轮上的"分叉"、顶上那一行"从哪儿分叉来的"。

          floor     最早还能分叉的位置（见 fork.floor）：mark 比它小的那几轮压缩进了摘要，按钮是灰的
          unmarked  没有记下位置的那一轮为什么不能分叉（换语言时页面照着原文重译）
          origin    这段会话是从哪儿分叉来的（Conversation.forked_from），加上 exists：原会话还在不在（删掉了的，页面只显示文字）。
                    还在的话 title 换成它现在的标题（分叉以后改过名也对得上）。不是分叉出来的是 null
        """
        app = self.web.app
        origin = app.conversation.forked_from
        if origin:
            title = app.store.title_of(origin["id"])
            origin = {**origin, "title": origin["title"] if title is None else title, "exists": title is not None}
        return {"floor": fork.floor(app.agent.backend), "unmarked": said(fork.unmarked()), "origin": origin}

    def route(self, body: dict) -> tuple[int, dict]:
        conversation, turn, at = body.get("conversation"), body.get("turn"), body.get("at")
        if not isinstance(conversation, str) or not isinstance(turn, int) or isinstance(turn, bool) or at not in FORK_POINTS:
            return 400, {"error": t("参数不正确")}
        web = self.web
        if not web._switch.acquire(blocking=False):
            return 409, {"error": web._busy()}
        try:
            viewed = web.sessions.viewed
            with web._holding(viewed) as idle:
                if not idle and not viewed.running:
                    return 409, {"error": web._busy()}  # 它正在被改（换模型、改名……）：等这一下
                return self._fork(viewed, idle, conversation, turn, at)
        finally:
            web._switch.release()

    def _fork(self, viewed: Session, idle: bool, conversation_id: str, turn_id: int, at: str) -> tuple[int, dict]:
        """分叉、换过去。调用的人拿着 _switch；idle 是它也拿着 viewed 的锁（viewed 没在跑）。"""
        web, app = self.web, self.web.app
        current = viewed.chat.conversation
        users = viewed.ui.user_events()
        index = next((number for number, event in enumerate(users) if event["id"] == turn_id), None)
        if conversation_id != current.id or index is None:
            web.publish_state()  # 页面上看到的已经过时了：先把最新的发过去
            return 409, {"error": fork.changed()}
        event = users[index]
        mark = rewind.mark_of(event)
        if mark is None:
            return 409, {"error": fork.unmarked()}
        if viewed.unsaved:
            return 409, {"error": t("这段会话上一次没能保存（{error}）。分叉读的是存在磁盘上的样子，先解决保存的问题再试。", error=viewed.unsaved)}
        if not current.saved:
            return 409, {"error": t("这段对话还没有存盘：第一轮结束以后才能分叉。")}
        text = event.get("text")
        if not isinstance(text, str):
            text = ""
        session = viewed if idle else web._fresh_session()  # 正在跑的那一段留在后台，分叉出来的另外建一段（见文件开头）
        try:
            made = app.fork_conversation(current.id, index, at, expect=(mark, text), chat=session.chat)
        except (ForkError, ConversationError) as exc:
            if session is not viewed:
                app.close_chat(session.chat)
            return 409, {"error": str(exc)}
        # 换到了新的一段上：和打开一段会话一样，页面清空、重放新的一段的事件，再发一次状态（重放到那里结束）
        session.ui.load_events(made.conversation.events)
        if session is not viewed:
            web.sessions.add(session)
            web._left_behind(web._show(session), idle=False)
        web._left(session)
        web._show_warnings()
        web.publish_state()
        return 200, {"ok": True, "conversation": made.conversation.id, "draft": made.draft, "attachments": made.attachments}
