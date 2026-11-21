"""网页上"不用问的命令"窗口背后的接口：/api/commands/……。

  POST /api/commands/add      加一条（command）
  POST /api/commands/edit     把一条（old）改成另一条（command），位置不变
  POST /api/commands/delete   删一条（command）
  POST /api/commands/reveal   在文件管理器里显示 data/commands.json（路径由程序算，页面什么都不给）

清单在状态里（state.commands，见 state()），页面照着它画，不另外来要。
只是设置，不拿对话的锁：对话进行中也能改，从下一次要判断的命令开始按新的来（见 ../commands/allowlist.py）。
这几个接口只有带口令的页面能用（和别的接口一样，见 server.py）：模型没有任何能改这个清单的工具。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ..commands.allowlist import CommandList, CommandListError
from ..config import MAX_ALLOWED_COMMAND_CHARS
from ..i18n import t
from ..safe import private_dir
from .ui import said

if TYPE_CHECKING:
    from .server import WebApp


class CommandsAPI:
    def __init__(self, web: WebApp, reveal: Callable[[Path], None]):
        self.web = web
        self._reveal = reveal

    @property
    def store(self) -> CommandList | None:
        return self.web.app.agent.permissions.commands

    def state(self) -> dict:
        """状态里的清单。文件有问题时的那句话带着原文，换语言时页面能重译。"""
        store = self.store
        if store is None:
            return {"entries": [], "file": "", "limit": 0, "chars": MAX_ALLOWED_COMMAND_CHARS, "problem": None}
        summary = store.summary()
        summary["problem"] = said(summary["problem"]) if summary["problem"] else None
        return summary

    def route(self, action: str, body: dict) -> tuple[int, dict]:
        store = self.store
        if store is None:
            return 404, {"error": t("没有这个操作")}
        if action == "reveal":
            return self._reveal_file(store)
        command = body.get("command")
        if action not in ("add", "edit", "delete") or not isinstance(command, str) or len(command) > MAX_ALLOWED_COMMAND_CHARS * 2:
            return 400, {"error": t("参数不正确")}
        try:
            if action == "add":
                entry = store.add(command)
            elif action == "edit":
                old = body.get("old")
                if not isinstance(old, str):
                    return 400, {"error": t("参数不正确")}
                entry = store.replace(old, command)
            else:
                store.remove(command)
                entry = command
        except CommandListError as exc:
            return 409, {"error": str(exc)}
        self.web.publish_state()
        return 200, {"ok": True, "entry": entry}

    def _reveal_file(self, store: CommandList) -> tuple[int, dict]:
        """在文件管理器里显示清单的文件。还没有这个文件（一条都没加过）就打开它将来所在的文件夹。"""
        shown = store.path if store.path.exists() else store.path.parent
        try:
            private_dir(store.path.parent)
            self._reveal(shown)
        except OSError as exc:
            return 409, {"error": t("没能打开文件管理器（{error}）。文件在：{path}", error=exc, path=store.path)}
        return 200, {"ok": True, "path": str(shown)}
