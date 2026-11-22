"""多媒体库：所有会话里发过的附件（规矩见 ../media.py）。

  POST /api/media/list     一页附件（可以按种类筛、按名字找）：route_list 查参数（_media_query），media_list 取
  POST /api/media/reveal   在文件管理器里显示一个附件的原件：浏览器只给编号，路径由服务器算
  POST /api/media/file     一个附件的内容。它把文件的字节发给浏览器，所以和两张截图的大图一样留在 server.py 的 _route 里，
                           安全头和 _send 挨着；这里只有它用的 media_file

MediaActions 混进 WebApp（见 webapp.py）：只有方法，用到的状态都在 WebApp.__init__ 里。
条目来自两处：保存过的会话（ConversationStore 维护的索引），和活着的那几段（内存里的界面事件，_live_media）：
你正看着的这一段，加上网页上在后台跑着的（见 sessions.py）。它们可能还没存盘，以内存为准。
_keep_originals 在一轮开始时存原件，_collect_media 删掉没有会话再用的原件，_media_state 是状态里的 media 一节。

route_* 由 server.py 的 Handler._route 调用（写法和约定见 server.py 开头）。
"""

from __future__ import annotations

import zlib
from typing import TYPE_CHECKING

from .. import media
from ..attachments import Attachment
from ..conversations import ConversationError
from ..i18n import t
from ..media import Entry, MediaError
from ..search import MAX_QUERY_CHARS
from . import reveal

if TYPE_CHECKING:
    from .server import Handler
    from .sessions import Session
    from .webapp import WebApp


class MediaActions:
    """多媒体库：存原件、列出、读出、显示、清理。

    条目来自两处：保存过的会话（ConversationStore 维护的索引），和活着的那几段（内存里的界面事件）。
    活着的以内存为准：它可能还没存盘，或者刚发的附件还没来得及存进文件。
    """

    def _keep_originals(self, attachments: list[Attachment], session: Session | None = None) -> list[Attachment]:
        """把附件的原件存进多媒体库。存不成也照样发这条消息，只是在那一段的页面上提醒一句。"""
        if not attachments:
            return attachments
        kept, problem = self.app.media.keep(attachments)
        if problem:
            ui = (session or self.sessions.viewed).ui
            ui.hint(t("附件的原件没能存进多媒体库（{error}）。消息照常发送，只是以后在多媒体里下载不到原件。", error=problem), level="error")
        return kept

    def _live_media(self, session: Session | None = None) -> tuple[Entry, ...]:
        """一段活着的会话里的附件（不给是你正看着的那一段）。每发一次状态都要用（数一共有多少个），所以每段记着上一次算的结果：
        只有换了会话、有了新消息、历史变长了（旧附件的内容可能要从历史里找）时才重算。"""
        session = session or self.sessions.viewed
        current, events = session.chat.conversation, session.ui.user_events()
        messages = session.chat.agent.backend.messages
        key = (current.id, current.created_at, tuple(event["id"] for event in events), len(messages))
        cached_key, cached = session.media
        if key != cached_key:
            cached = tuple(media.entries_of(events, messages, current.created_at))
            session.media = (key, cached)
        return cached

    def _live_sources(self) -> list[tuple[str, str, tuple[Entry, ...]]]:
        """活着的那几段：（会话编号，标题，附件），你正看着的那一段排在最前面。"""
        viewed = self.sessions.viewed
        sessions = [viewed, *(session for session in self.sessions.all() if session is not viewed)]
        return [(session.id, session.chat.conversation.title, self._live_media(session)) for session in sessions]

    def _media_sources(self) -> list[tuple[str, str, tuple[Entry, ...]]]:
        """（会话编号，标题，附件）：保存过的会话，活着的那几段换成内存里的样子。"""
        live = self._live_sources()
        ids = {cid for cid, _, _ in live}
        sources = [(cid, title, entries) for cid, (title, entries) in self.app.store.media().snapshot().items() if cid not in ids]
        sources += [(cid, title, entries) for cid, title, entries in live if entries]
        return sources

    def _media_state(self) -> dict:
        index, live = self.app.store.media(), self._live_sources()
        ids = {cid for cid, _, _ in live}
        saved = sum(len(entries) for cid, (_, entries) in index.snapshot().items() if cid not in ids)
        # 版本由这几样拼成：保存过的会话变了、换了当前会话、活着的哪一段里多了附件、改了标题，都会让它变
        version = ".".join([str(index.version), *(f"{cid}.{len(entries)}.{zlib.crc32(title.encode('utf-8', 'replace')):08x}" for cid, title, entries in live)])
        return {"count": saved + sum(len(entries) for _, _, entries in live), "version": version}

    def media_list(self, kind: str, query: str, cursor: int, limit: int) -> dict:
        """多媒体库的一页（见 media.listing）。只是读，对话进行中也可以看。"""
        return media.listing(self._media_sources(), self.app.media.stored(), kind, query, cursor, limit)

    def media_file(self, media_id: str) -> tuple[str, tuple[bytes, str, str] | None]:
        """一个附件的内容：（失败的原因，（字节，类型，文件名））。"""
        try:
            conversation_id, entry = media.find(self._media_sources(), media_id)
            live = self.sessions.find(conversation_id)
            if live is not None:
                messages = lambda: live.chat.agent.backend.messages  # 活着的那几段：历史就在内存里
            else:
                messages = lambda: self.app.store.load(conversation_id).messages  # 真要用到才读文件
            return "", media.content_of(entry, self.app.media, messages)
        except (MediaError, ConversationError) as exc:
            return str(exc), None

    def reveal_media(self, media_id: str) -> tuple[str, str]:
        """在文件管理器里显示一个附件的原件。返回（失败原因，显示的路径）。

        浏览器只给编号（格式在路由里查过）。原件的路径由服务器自己算：编号 → 索引里的条目 → 内容的 SHA-256 →
        data/media/ 下的那个文件。和 reveal_conversation 一样只是看一眼，不改任何东西，所以不拿对话的锁。
        """
        try:
            _, entry = media.find(self._media_sources(), media_id)
        except MediaError as exc:
            return str(exc), ""
        target = self.app.media.find(entry.sha256) if entry.sha256 else None
        if target is None:
            return t("这个附件没有单独存下原件：它是在有多媒体库之前发的，内容保存在它所在的会话的文件里。"), ""
        try:
            reveal.reveal_in_file_manager(target)
        except OSError as exc:
            return t("没能打开文件管理器（{error}）。文件在：{path}", error=exc, path=target), ""
        return "", str(target)

    def _collect_media(self, candidates: set[str] | None) -> None:
        """删掉没有会话再用的原件（见 MediaStore.collect）。删不掉只是多占点地方，不影响别的，所以不报错。"""
        store = self.app.store
        # 活着的每一段都算"在用"：后台那一段的第一轮还没存盘，它刚发的附件只在它自己的事件里
        used = store.media().hashes() | {entry.sha256 for _, _, entries in self._live_sources() for entry in entries if entry.sha256}
        try:
            # 读不出来的会话文件不在索引里，它们提到的原件也要留着
            unreadable = [path for path in store.directory.glob("*.json") if not store.exists(path.stem)] if store.directory.is_dir() else []
            self.app.media.collect(used, candidates, unreadable)
        except OSError:
            pass


def route_list(handler: Handler, web: WebApp, body: dict) -> None:
    asked = _media_query(body)
    if asked is None:
        handler._send_json(400, {"error": t("参数不正确")})
    else:
        # 只是读：内存里的索引，加上看一眼原件的目录。对话进行中也可以看
        handler._send_json(200, {"ok": True, **web.media_list(*asked)})


def route_reveal(handler: Handler, web: WebApp, body: dict) -> None:
    media_id = body.get("id")
    if not isinstance(media_id, str) or not media.ID.fullmatch(media_id):
        handler._send_json(400, {"error": t("参数不正确")})
        return
    error, shown = web.reveal_media(media_id)
    if error:
        handler._send_json(409, {"error": error})
    else:
        handler._send_json(200, {"ok": True, "path": shown})


def _media_query(body: dict) -> tuple[str, str, int, int] | None:
    """多媒体库的一次查询：（种类，搜索词，从第几个开始，要几个）。参数不对返回 None。"""
    kind, query, cursor, limit = body.get("kind", "all"), body.get("query", ""), body.get("cursor", 0), body.get("limit", media.PAGE_SIZE)
    if kind not in ("all", *media.KINDS) or not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
        return None
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in (cursor, limit)):
        return None
    if cursor < 0 or not 0 < limit <= media.MAX_PAGE:
        return None
    return kind, query.strip(), cursor, limit
