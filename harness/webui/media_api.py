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
    from .webapp import WebApp


class MediaActions:
    # ---- 多媒体 ------------------------------------------------------------
    # 多媒体库的条目来自两处：保存过的会话（ConversationStore 维护的索引），和当前这一段（内存里的界面事件）。
    # 当前这一段以内存为准：它可能还没存盘，或者刚发的附件还没来得及存进文件。

    def _keep_originals(self, attachments: list[Attachment]) -> list[Attachment]:
        """把附件的原件存进多媒体库。存不成也照样发这条消息，只是提醒一句。"""
        if not attachments:
            return attachments
        kept, problem = self.app.media.keep(attachments)
        if problem:
            self.ui.hint(t("附件的原件没能存进多媒体库（{error}）。消息照常发送，只是以后在多媒体里下载不到原件。", error=problem), level="error")
        return kept

    def _live_media(self) -> tuple[Entry, ...]:
        """当前会话里的附件。每发一次状态都要用（数一共有多少个），所以记着上一次算的结果：
        只有换了会话、有了新消息、历史变长了（旧附件的内容可能要从历史里找）时才重算。"""
        current, events = self.app.conversation, self.ui.user_events()
        messages = self.app.agent.backend.messages
        key = (current.id, current.created_at, tuple(event["id"] for event in events), len(messages))
        cached_key, cached = self._live
        if key != cached_key:
            cached = tuple(media.entries_of(events, messages, current.created_at))
            self._live = (key, cached)
        return cached

    def _media_sources(self) -> list[tuple[str, str, tuple[Entry, ...]]]:
        """（会话编号，标题，附件）：保存过的会话，当前这一段换成内存里的样子。"""
        current = self.app.conversation
        sources = [(cid, title, entries) for cid, (title, entries) in self.app.store.media().snapshot().items() if cid != current.id]
        live = self._live_media()
        if live:
            sources.append((current.id, current.title, live))
        return sources

    def _media_state(self) -> dict:
        index, current, live = self.app.store.media(), self.app.conversation, self._live_media()
        saved = sum(len(entries) for cid, (_, entries) in index.snapshot().items() if cid != current.id)
        # 版本由这几样拼成：保存过的会话变了、换了当前会话、当前会话里多了附件、当前会话改了标题，都会让它变
        version = f"{index.version}.{current.id}.{len(live)}.{zlib.crc32(current.title.encode('utf-8', 'replace')):08x}"
        return {"count": saved + len(live), "version": version}

    def media_list(self, kind: str, query: str, cursor: int, limit: int) -> dict:
        """多媒体库的一页（见 media.listing）。只是读，对话进行中也可以看。"""
        return media.listing(self._media_sources(), self.app.media.stored(), kind, query, cursor, limit)

    def media_file(self, media_id: str) -> tuple[str, tuple[bytes, str, str] | None]:
        """一个附件的内容：（失败的原因，（字节，类型，文件名））。"""
        try:
            conversation_id, entry = media.find(self._media_sources(), media_id)
            if conversation_id == self.app.conversation.id:
                messages = lambda: self.app.agent.backend.messages  # 当前这一段：历史就在内存里
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
        used = store.media().hashes() | {entry.sha256 for entry in self._live_media() if entry.sha256}
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
