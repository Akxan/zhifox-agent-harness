"""会话：新对话、打开、改名、删除、在文件管理器里显示一段会话，在所有会话里搜索。

  POST /api/new                    开始新对话（带上 agent 的话，用这个自定义 Agent 当角色，见 agents/）
  POST /api/conversation/open      打开一段保存过的会话
  POST /api/conversation/rename    改名
  POST /api/conversation/delete    删除（它用过、别的会话又不再用的附件原件跟着从多媒体库删掉）
  POST /api/conversation/reveal    在文件管理器里显示它的文件（编号为空时显示保存会话的文件夹）
  POST /api/search                 搜索（只读内存里的索引，见 search.py）

ConversationActions 混进 WebApp（见 webapp.py）：只有方法，用到的状态都在 WebApp.__init__ 里。
几段对话可以同时进行（见 sessions.py）：你正看着的那一段在跑的时候，打开别的会话、开新对话，它留在后台接着跑，
给你看的换成新建的一段；它没在跑的话，和以前一样直接换掉它的内容。打开一段正在后台跑的会话，就是换过去看它。
新对话、打开、改名、删除都拿 _switch（"哪几段活着、正看着哪一段"一次只改一处），再拿要动的那一段的锁；
正在跑一轮的那一段不能改名、删除。聊天通道正在用的会话不能动（见 claims.py）；
离开一段会话之前先看它存上了没有（WebApp._before_leaving）。显示、搜索只是读，不拿锁，对话进行中也能做。

route_* 由 server.py 的 Handler._route 调用（写法和约定见 server.py 开头）。
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from .. import i18n
from ..agents import AgentError
from ..conversations import ConversationError
from ..i18n import t
from ..providers import find, has_key
from ..search import MAX_QUERY_CHARS
from . import reveal

if TYPE_CHECKING:
    from .server import Handler
    from .sessions import Session
    from .webapp import WebApp


LARGE_CONTEXT_TOKENS = 30_000  # 打开的旧会话超过这么大时，提醒一下接着聊的费用


class ConversationActions:
    """新对话、打开、改名、删除、显示一段会话。"""

    def new_conversation(self, agent: str | None = None) -> str:
        """开始新的一段对话。成功返回空字符串，否则返回不能这样做的原因。

        agent：用哪个自定义 Agent 当角色（见 App.new_conversation）。它选的模型是另一家、那一家又还没有密钥的话，先说清楚。
        你正看着的那一段在跑的话，它留在后台接着跑，新的这一段另外建一个（见 sessions.py）。
        """
        if not self._switch.acquire(blocking=False):
            return self._busy()
        try:
            if agent:
                try:
                    persona = self.app.agents.usable(agent)
                except AgentError as exc:
                    return str(exc)
                provider = (find(persona.spec.provider) if persona.spec.model else None) or self.app.provider
                if not has_key(provider):
                    return t("「{agent}」用的是 {provider} 的模型，还没有设置 {provider} 的 API 密钥。", agent=persona.name, provider=provider.label)
            current = self.sessions.viewed
            with self._holding(current) as idle:
                if not idle and not current.running:
                    return self._busy()  # 它正在被改（换模型、改名……）：等这一下
                if not idle:
                    return self._start_beside(current, agent)
                blocked = self._before_leaving(current, "new:" + (agent or ""))
                if blocked:
                    return blocked
                try:
                    self.app.new_conversation(agent)
                except AgentError as exc:
                    return str(exc)
                self._left(current)
                current.ui.reset()
                self.publish_state()
        finally:
            self._switch.release()
        return ""

    def _start_beside(self, running: Session, agent: str | None) -> str:
        """你正看着的那一段（running）在跑：它留在后台，新建一段给你看。调用的人拿着 _switch。"""
        session = self._fresh_session()
        if agent:
            try:
                self.app.new_conversation(agent, chat=session.chat)
            except AgentError as exc:
                self.app.close_chat(session.chat)
                return str(exc)
        session.ui.reset()
        self.sessions.add(session)
        self._left_behind(self._show(session), idle=False)
        self._show_warnings()
        self.publish_state()
        return ""

    def open_conversation(self, conversation_id: str) -> str:
        """打开一段会话。成功返回空字符串，否则返回原因。

        它正在后台跑着（或者没存上盘、还留在内存里）的话，换过去看它：事件从头重放出来，流式输出接着来。
        别的从磁盘读。你正看着的那一段在跑的话，它留在后台接着跑，打开的这一段另外建一个；没在跑的话直接换掉它的内容。
        """
        if not self._switch.acquire(blocking=False):
            return self._busy()
        try:
            current = self.sessions.viewed
            if conversation_id == current.id:
                return ""  # 已经开着了
            blocked = self._held_elsewhere(conversation_id)
            if blocked:
                return blocked
            with self._holding(current) as idle:
                if not idle and not current.running:
                    return self._busy()
                if idle:
                    blocked = self._before_leaving(current, f"open:{conversation_id}")
                    if blocked:
                        return blocked
                found, _ = self._show_live(conversation_id)
                if found is not None:
                    self._left_behind(current, idle)  # 没在跑的丢掉（内容在磁盘上），在跑的留在后台
                    self._show_warnings()
                    self.publish_state()
                    return ""
                return self._open_saved(conversation_id, current, idle)
        finally:
            self._switch.release()

    def _open_saved(self, conversation_id: str, current: Session, idle: bool) -> str:
        """从磁盘打开一段会话。current 没在跑（idle，调用的人拿着它的锁）就在它里面打开，在跑就新建一段。调用的人拿着 _switch。"""
        session = current if idle else self._fresh_session()
        try:
            conversation = self.app.open_conversation(conversation_id, chat=session.chat)
        except ConversationError as exc:
            if session is not current:
                self.app.close_chat(session.chat)
            return str(exc)
        session.ui.load_events(conversation.events)
        if session is not current:
            self.sessions.add(session)
            self._left_behind(self._show(session), idle=False)
        else:
            self.sessions.forget(conversation_id)  # 后台跑完、你还没看的标记：现在看了
        self._left(session)
        if conversation.context_tokens >= LARGE_CONTEXT_TOKENS:
            session.ui.hint(
                t(
                    "这段会话的历史约有 {tokens} 个 token。接着聊的第一条消息需要把它重新发给模型，会按这个量计费。",
                    tokens=i18n.number(conversation.context_tokens),
                )
            )
        self._show_warnings()
        self.publish_state()
        return ""

    def rename_conversation(self, conversation_id: str, title: str) -> str:
        # 改名要读出会话文件、改掉标题、再写回去。如果这时正好有一轮对话结束、也在写同一个文件，
        # 两边会互相覆盖。所以正在跑一轮的那一段不让改；别的会话随时可以改（拿着 _switch，它这时也不会被打开、开始一轮）。
        if not self._switch.acquire(blocking=False):
            return self._busy()
        try:
            blocked = self._held_elsewhere(conversation_id)
            if blocked:
                return blocked
            live = self.sessions.find(conversation_id)
            with self._holding(live) if live is not None else contextlib.nullcontext(True) as free:
                if not free:
                    return self._busy()
                try:
                    self.app.rename_conversation(conversation_id, title)
                except ConversationError as exc:
                    return str(exc)
            self.publish_state()
        finally:
            self._switch.release()
        return ""

    def delete_conversation(self, conversation_id: str) -> str:
        if not self._switch.acquire(blocking=False):
            return self._busy()
        try:
            blocked = self._held_elsewhere(conversation_id)
            if blocked:
                return blocked
            live = self.sessions.find(conversation_id)
            if live is not None and live is not self.sessions.viewed:
                return self._delete_background(live)
            viewed = self.sessions.viewed
            with self._holding(viewed) if live is viewed else contextlib.nullcontext(True) as free:
                if not free:
                    return self._busy()
                # 这段会话用过的原件。删完以后，别的会话不再用的就从多媒体库里删掉
                used = self.app.store.media().hashes(conversation_id)
                if live is viewed:
                    used |= {entry.sha256 for entry in self._live_media(viewed) if entry.sha256}
                try:
                    was_current = self.app.delete_conversation(conversation_id)
                except ConversationError as exc:
                    return str(exc)
                if was_current:
                    self._left(viewed)  # 删的就是你正看着的这一段：它有没有存上都无所谓了
                    viewed.ui.reset()
            self.sessions.forget(conversation_id)
            self._collect_media(used)
            self.publish_state()
        finally:
            self._switch.release()
        return ""

    def _delete_background(self, live: Session) -> str:
        """删掉一段还活着、但你没在看的会话（在后台跑完了、可是没存上盘的那种；正在跑的不能删）。调用的人拿着 _switch。"""
        with self._holding(live) as free:
            if not free:
                return self._busy()
            conversation = live.chat.conversation
            used = self.app.store.media().hashes(conversation.id) | {entry.sha256 for entry in self._live_media(live) if entry.sha256}
            if conversation.saved:
                try:
                    self.app.store.delete(conversation.id)
                except ConversationError as exc:
                    return str(exc)
            self._discard(live)
        self.sessions.forget(conversation.id)
        self._collect_media(used)
        self.publish_state()
        return ""

    def reveal_conversation(self, conversation_id: str) -> tuple[str, str]:
        """在文件管理器里显示一段会话的文件；编号为空时显示保存会话的那个文件夹。返回（失败原因，显示的路径）。

        只是看一眼文件在哪儿，不改任何东西，所以不用拿对话的锁，对话进行中也可以点。
        """
        store, live = self.app.store, self.sessions.find(conversation_id) if conversation_id else None
        if live is not None and not live.chat.conversation.saved:
            return t("这段会话还没有保存：第一轮对话结束后才会写到磁盘上。"), ""
        try:
            # 路径是由编号算出来的（编号的格式在 file_of 里检查过），浏览器没有办法指定别的路径
            target = store.file_of(conversation_id) if conversation_id else store.directory
        except ConversationError as exc:
            return str(exc), ""
        if not target.exists():
            return (t("还没有保存过会话，文件夹还不存在。") if not conversation_id else t("这段会话的文件已经不在了。")), ""
        try:
            reveal.reveal_in_file_manager(target)
        except OSError as exc:
            return t("没能打开文件管理器（{error}）。文件在：{path}", error=exc, path=target), ""
        return "", str(target)


def route_new(handler: Handler, web: WebApp, body: dict) -> None:
    agent = body.get("agent")
    if agent is not None and (not isinstance(agent, str) or not 0 < len(agent) <= 200):
        handler._send_json(400, {"error": t("参数不正确")})
    else:
        handler._finish(web.new_conversation(agent))


def route_conversation(handler: Handler, web: WebApp, path: str, body: dict) -> None:
    conversation_id, title = body.get("id"), body.get("title")
    action = path.removeprefix("/api/conversation/")
    if not isinstance(conversation_id, str):
        handler._send_json(400, {"error": t("参数不正确")})
    elif action == "open":
        handler._finish(web.open_conversation(conversation_id))
    elif action == "delete":
        handler._finish(web.delete_conversation(conversation_id))
    elif action == "reveal":
        error, shown = web.reveal_conversation(conversation_id)
        if error:
            handler._send_json(409, {"error": error})
        else:
            handler._send_json(200, {"ok": True, "path": shown, "opened": reveal.has_file_manager()})
    elif action == "rename" and isinstance(title, str) and 0 < len(title.strip()) <= 200:
        handler._finish(web.rename_conversation(conversation_id, title.strip()))
    else:
        handler._send_json(400, {"error": t("参数不正确")})


def route_search(handler: Handler, web: WebApp, body: dict) -> None:
    query = body.get("query")
    if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
        handler._send_json(400, {"error": t("搜索词必须是一段文字，最长 {n} 个字符", n=MAX_QUERY_CHARS)})
    else:
        # 只读内存里的索引，不改任何东西，所以对话进行中也可以搜
        handler._send_json(200, {"ok": True, "results": web.app.store.search(query)})
