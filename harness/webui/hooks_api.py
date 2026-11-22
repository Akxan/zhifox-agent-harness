"""网页上"扩展 → 钩子"一页背后的接口：/api/hooks/……。

  POST /api/hooks/list     全部钩子（给页面看的样子：参数里像密钥的部分遮住）、上一次跑得怎么样、表单要的几样东西
  POST /api/hooks/save     添加或修改一个（original 是改之前的名字，新加的是 null；hook 是表单）
  POST /api/hooks/delete   删一个（name）
  POST /api/hooks/enable   打开、关掉一个（name、enabled）
  POST /api/hooks/test     试运行一个（name；payload 是你在页面上改过的示例 JSON，可以不给）：真的运行它的命令
  POST /api/hooks/reveal   在文件管理器里显示 data/hooks.json（路径由程序算，页面什么都不给）

保存、试运行都会让这台电脑运行你配置的命令，所以和 stdio 的 MCP 服务器一样：只有带口令的页面能用（和别的接口一样，
见 server.py），命令用参数列表启动、不经过 shell、环境里没有模型的密钥（见 hooks/）。模型没有任何能改钩子的工具。
只是设置，不拿对话的锁：对话进行中也能改，从下一次到了那个时刻起按新的来。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import HOOK_MAX, HOOK_MAX_TIMEOUT, HOOK_NOTE_CHARS, HOOK_TIMEOUT
from ..hooks import EVENTS, HookError, Hooks
from ..hooks.config import SOURCES, display, from_form
from ..i18n import t
from ..safe import private_dir
from .ui import said

if TYPE_CHECKING:
    from .server import WebApp

MAX_PAYLOAD_KEYS = 200


class HooksAPI:
    def __init__(self, web: WebApp, reveal: Callable[[Path], None]):
        self.web = web
        self._reveal = reveal

    @property
    def hooks(self) -> Hooks:
        return self.web.app.hooks

    def summary(self) -> dict:
        """状态里的一小段：有几个钩子（扩展的标签上那个数）、文件有没有问题、最近一次有钩子跑完是什么时候。
        完整的列表打开那一页时再要；ran 变了（某个钩子刚跑过），开着那一页的话页面重新要一次，"上一次"那一行跟着变。"""
        config = self.hooks.config
        config.reload()
        hooks = config.hooks()
        return {
            "count": len(hooks),
            "on": sum(1 for entry, _ in hooks.values() if entry is not None and entry["enabled"]),
            "problem": said(config.problem) if config.problem else None,
            "ran": max((last["at"] for last in self.hooks.last().values()), default=0),
        }

    def listing(self) -> dict:
        config = self.hooks.config
        config.reload()
        last = self.hooks.last()
        rows = []
        for name, (entry, problem) in config.hooks().items():
            row: dict[str, Any] = {"name": name, "problem": said(problem) if problem else None, "hook": None, "last": last.get(name)}
            if entry is not None:
                row["hook"] = display(entry)
                row["sample"] = self.hooks.sample(entry)
            rows.append(row)
        return {
            "hooks": rows,
            "problem": said(config.problem) if config.problem else None,
            "file": str(config.path),
            "events": list(EVENTS),
            "sources": list(SOURCES),
            "groups": _groups(),
            "timeout": HOOK_TIMEOUT,
            "max_timeout": HOOK_MAX_TIMEOUT,
            "note_chars": HOOK_NOTE_CHARS,
            "limit": HOOK_MAX,
        }

    def route(self, action: str, body: dict) -> tuple[int, dict]:
        if action == "list":
            return 200, {"ok": True, **self.listing()}
        if action == "reveal":
            return self._reveal_file()
        name = body.get("name")
        if not isinstance(name, str) or not name or len(name) > 200:
            return 400, {"error": t("参数不正确")}
        config = self.hooks.config
        try:
            if action == "save":
                original = body.get("original")
                if original is not None and not isinstance(original, str):
                    return 400, {"error": t("参数不正确")}
                old = config.get(original) if original else None
                entry = config.put(name, from_form(body.get("hook"), old), original)
                if original and original != name:
                    self.hooks.rename(original, name)
                self.web.publish_state()
                return 200, {"ok": True, "name": name.strip(), "hook": display(entry)}
            if action == "delete":
                config.remove(name)
                self.hooks.forget(name)
                self.web.publish_state()
                return 200, {"ok": True}
            if action == "enable":
                enabled = body.get("enabled")
                if not isinstance(enabled, bool):
                    return 400, {"error": t("参数不正确")}
                entry = config.set_enabled(name, enabled)
                self.web.publish_state()
                return 200, {"ok": True, "hook": display(entry)}
            if action == "test":
                return self._test(name, body.get("payload"))
        except HookError as exc:
            return 409, {"error": exc.args[0] if exc.args else str(exc)}
        return 404, {"error": t("没有这个操作")}

    def _test(self, name: str, payload: object) -> tuple[int, dict]:
        """试运行：用示例（或者你改过的那一份）真的跑一次它的命令。停用着的也能试（先试好了再打开）。"""
        config = self.hooks.config
        config.reload()
        entry = config.get(name)
        if entry is None:
            problem = config.hooks().get(name, (None, ""))[1]
            return 409, {"error": problem or t("没有找到钩子“{name}”，它可能已经被删掉了。", name=name)}
        if payload is not None and (not isinstance(payload, dict) or len(payload) > MAX_PAYLOAD_KEYS):
            return 400, {"error": t("示例应该是一个 JSON 对象。")}
        run = self.hooks.test(name, entry, payload)
        run["ignored"] = [said(text) for text in run["ignored"]]
        run["verdict"] = said(run["verdict"])
        return 200, {"ok": True, "run": run, "last": self.hooks.last().get(name)}

    def _reveal_file(self) -> tuple[int, dict]:
        """在文件管理器里显示钩子的配置文件。还没有这个文件（一个都没加过）就打开它将来所在的文件夹。"""
        path = self.hooks.config.path
        shown = path if path.exists() else path.parent
        try:
            private_dir(path.parent)
            self._reveal(shown)
        except OSError as exc:
            return 409, {"error": t("没能打开文件管理器（{error}）。文件在：{path}", error=exc, path=path)}
        return 200, {"ok": True, "path": str(shown)}


def _groups() -> list[dict]:
    """matcher 里能写的"功能"：和信息栏"智狐能做的事"是同一张表（capabilities.KINDS），名字是原文，页面自己译。"""
    from ..capabilities import (  # 用到时才导入：capabilities 要导入大半个程序
        KINDS,
        MCP_KIND,
    )

    return [{"id": kind.id, "icon": kind.icon, "label": kind.label} for kind in (*KINDS, MCP_KIND)]
