"""网页上"改了 N 个文件"那一行背后的接口：/api/changes/……（整个功能的规矩见 ../undo/__init__.py）。

  POST /api/changes/diff   一个文件这一轮改了什么（set：一轮的编号，index：清单里的第几个）。只是读留底，不拿对话的锁
  POST /api/changes/undo   撤回（conversation、set；files 是第几个文件的列表，不给是这一轮里还没撤回的全部；
                           force 为真时，要你确认的也照做，做不了的跳过）
  POST /api/changes/redo   重做（同上）

页面只给它在事件里看到的一轮的编号、清单里的第几个文件；写哪个文件、读哪份留底由 undo/changes.py 照着清单算，
和"在访达中显示"那几个接口是同一个规矩（页面最多只能给编号）。模型没有能调用这些的工具。

撤回、重做会改动对话的状态（多一个事件、存一次盘），所以和发消息、倒回最后一轮一样拿对话的锁（WebApp._turn），
认领这段会话（聊天通道正在用的不能撤，见 claims.py）。做成了就发一个 file_undo 事件（开着的每个页面都照着它更新那一行），
记下下一条消息要告诉模型的话（见 undo/notes.py），存盘，再发一次状态（最后一轮的"同时撤回这一轮改的文件"跟着变）。

回答里的 asks（要你确认的）和 blocked（做不了的）：[{"index", "path", "reasons", "later"?, "error"?}]，
reasons 里的词见 undo/restore.py 的 ASK 和 BLOCK，页面照着它们写成话。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..config import UNDO_TURN_FILES
from ..i18n import t
from .ui import said

if TYPE_CHECKING:
    from ..undo.changes import FileChanges, Result
    from .server import WebApp


class ChangesAPI:
    def __init__(self, web: WebApp):
        self.web = web

    @property
    def changes(self) -> FileChanges:
        return self.web.app.changes

    def route(self, action: str, body: dict) -> tuple[int, dict]:
        if action == "diff":
            return self._diff(body)
        if action in ("undo", "redo"):
            return self._act(action, body)
        return 404, {"error": t("没有这个操作")}

    def _diff(self, body: dict) -> tuple[int, dict]:
        error, preview = self.changes.diff(body.get("set"), body.get("index"), self.web.ui.export_events())
        return (409, {"error": error}) if error else (200, {"ok": True, **preview})

    def _act(self, action: str, body: dict) -> tuple[int, dict]:
        conversation, set_id, files, force = body.get("conversation"), body.get("set"), body.get("files"), body.get("force", False)
        bad = not isinstance(conversation, str) or not isinstance(set_id, str) or not isinstance(force, bool)
        bad = bad or not (files is None or (isinstance(files, list) and 0 < len(files) <= UNDO_TURN_FILES))
        if bad:
            return 400, {"error": t("参数不正确")}
        web = self.web
        if not web._turn.acquire(blocking=False):
            return 409, {"error": web._busy()}
        claimed = False
        try:
            if conversation != web.app.conversation.id:
                web.publish_state()  # 页面上看到的已经过时了：先把最新的发过去
                return 409, {"error": t("这一轮不在现在开着的这段对话里了。看一眼最新的内容再试。")}
            elsewhere = web._claim()
            if elsewhere:
                return 409, {"error": elsewhere}
            claimed = True
            result = self.changes.act(action, set_id, web.ui.export_events(), files, force)
            payload = issues(result)
            if result.event is None:
                return 409, {"error": result.error, **payload}
            web.ui.publish("file_undo", **result.event)
            self.changes.notes.add(web.app.conversation.id, result.notes, set_id)
            web._save()
            web.publish_state()
            return 200, {"ok": True, **payload}
        finally:
            if claimed:
                web.claims.release(web.app.conversation.id, "web")
            web._turn.release()


def issues(result: Result) -> dict:
    """回答里的 asks 和 blocked：每个原因的那句话带着原文（said），页面换语言时照着重译。"""
    return {key: [{**item, "why": [said(text) for text in item["why"]]} for item in getattr(result, key)] for key in ("asks", "blocked")}
