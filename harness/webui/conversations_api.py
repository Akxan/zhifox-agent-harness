from __future__ import annotations

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
    from .webapp import WebApp


LARGE_CONTEXT_TOKENS = 30_000  # 打开的旧会话超过这么大时，提醒一下接着聊的费用


class ConversationActions:
    def new_conversation(self, agent: str | None = None) -> str:
        """清空对话，开始新的一段。成功返回空字符串，否则返回不能这样做的原因。

        agent：用哪个自定义 Agent 当角色（见 App.new_conversation）。它选的模型是另一家、那一家又还没有密钥的话，先说清楚。
        """
        if not self._turn.acquire(blocking=False):
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
            blocked = self._before_leaving("new:" + (agent or ""))
            if blocked:
                return blocked
            try:
                self.app.new_conversation(agent)
            except AgentError as exc:
                return str(exc)
            self._left()
            self.ui.reset()
            self.publish_state()
        finally:
            self._turn.release()
        return ""

    # ---- 会话 --------------------------------------------------------------

    def open_conversation(self, conversation_id: str) -> str:
        """打开一段保存过的会话。成功返回空字符串，否则返回原因。"""
        if not self._turn.acquire(blocking=False):
            return self._busy()
        try:
            if conversation_id == self.app.conversation.id:
                return ""  # 已经开着了
            blocked = self._held_elsewhere(conversation_id) or self._before_leaving(f"open:{conversation_id}")
            if blocked:
                return blocked
            try:
                conversation = self.app.open_conversation(conversation_id)
            except ConversationError as exc:
                return str(exc)
            self._left()
            self.ui.load_events(conversation.events)
            if conversation.context_tokens >= LARGE_CONTEXT_TOKENS:
                self.ui.hint(
                    t(
                        "这段会话的历史约有 {tokens} 个 token。接着聊的第一条消息需要把它重新发给模型，会按这个量计费。",
                        tokens=i18n.number(conversation.context_tokens),
                    )
                )
            self._show_warnings()
            self.publish_state()
        finally:
            self._turn.release()
        return ""

    def rename_conversation(self, conversation_id: str, title: str) -> str:
        # 改名要读出会话文件、改掉标题、再写回去。如果这时正好有一轮对话结束、也在写同一个文件，
        # 两边会互相覆盖。所以和打开、删除一样，对话进行中不让改。
        if not self._turn.acquire(blocking=False):
            return self._busy()
        try:
            blocked = self._held_elsewhere(conversation_id)
            if blocked:
                return blocked
            try:
                self.app.rename_conversation(conversation_id, title)
            except ConversationError as exc:
                return str(exc)
            self.publish_state()
        finally:
            self._turn.release()
        return ""

    def delete_conversation(self, conversation_id: str) -> str:
        if not self._turn.acquire(blocking=False):
            return self._busy()
        try:
            blocked = self._held_elsewhere(conversation_id)
            if blocked:
                return blocked
            # 这段会话用过的原件。删完以后，别的会话不再用的就从多媒体库里删掉
            used = self.app.store.media().hashes(conversation_id)
            if conversation_id == self.app.conversation.id:
                used |= {entry.sha256 for entry in self._live_media() if entry.sha256}
            try:
                was_current = self.app.delete_conversation(conversation_id)
            except ConversationError as exc:
                return str(exc)
            if was_current:
                self._left()  # 删的就是当前这一段：它有没有存上都无所谓了
                self.ui.reset()
            self._collect_media(used)
            self.publish_state()
        finally:
            self._turn.release()
        return ""

    def reveal_conversation(self, conversation_id: str) -> tuple[str, str]:
        """在文件管理器里显示一段会话的文件；编号为空时显示保存会话的那个文件夹。返回（失败原因，显示的路径）。

        只是看一眼文件在哪儿，不改任何东西，所以不用拿对话的锁，对话进行中也可以点。
        """
        store, current = self.app.store, self.app.conversation
        if conversation_id == current.id and not current.saved:
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
