from __future__ import annotations

from typing import TYPE_CHECKING

from ..i18n import t
from ..imports.overview import overview
from ..imports.places import Env
from ..imports.registry import MCP
from ..mcp import ConfigError
from ..mcp import importers as mcp_imports
from ..safe import private_dir
from . import reveal
from .ui import said

if TYPE_CHECKING:
    from .server import Handler


class McpActions:
    def _mcp_state(self) -> dict:
        """MCP 服务器的概况。密钥只有名字和最后几位（见 mcp/config.py 的 display），工具的详情点开时另外取。"""
        mcp = self.app.mcp
        started = bool(self.app.agent.backend.export())  # 这段对话发过消息了没有：发过了，工具就定下来了
        servers = mcp.status(started)
        for item in servers:
            item["error"] = said(item["error"]) if item["error"] else None
        problem = mcp.config.problem
        return {
            "servers": servers,
            "problem": said(problem) if problem else None,
            "file": str(mcp.config.path),
            "started": started,
            "dropped": mcp.active.dropped if started else 0,
        }

    # ---- MCP --------------------------------------------------------------
    # 这些操作不拿对话的锁：对话进行中也可以添加、停用、重启服务器。正在进行的这段对话用的是开始时拍下的
    # 快照（见 mcp/manager.py），不受影响；快照里的服务器被停掉了，调用它的工具会得到一个说清楚原因的错误。
    #
    # 保存一个 stdio 服务器，就是让这台电脑去运行你填的那个命令。所以守这几条：只有带着口令的这个页面能做；
    # 命令用参数列表启动、不经过 shell；模型没有任何工具能添加或修改服务器（mcp.json 文件工具也碰不了）。

    def mcp_detail(self, name: str) -> tuple[str, dict]:
        try:
            return "", self.app.mcp.detail(name)
        except ConfigError as exc:
            return str(exc), {}

    def mcp_change(self, action, *args) -> str:
        """改 MCP 的配置（保存、删除、启用、重启、改"问不问"）。成功返回空字符串，否则返回原因。"""
        try:
            action(*args)
        except ConfigError as exc:
            return str(exc)
        except OSError as exc:
            return t("保存配置文件失败：{error}", error=exc)
        return ""

    def mcp_scan(self) -> dict:
        """去读别的工具（Claude Desktop、Claude Code、Cursor、VS Code、Codex……）的配置，列出可以导入的服务器（密钥遮住）。
        只在你点了"从其他工具导入"时才读。tools 是顶上的总览：每个工具里三样各有几个（见 imports/overview.py）。"""
        env = Env.here(self.app.config.workspace)
        found = mcp_imports.sources(env.workspace, env)
        return {"sources": mcp_imports.preview(found, self.app.mcp.config), "tools": overview(env, {MCP: mcp_imports.counted(found)})}

    def mcp_import(self, picks: list[tuple[str, str]]) -> tuple[str, list[str], list[str]]:
        """导入勾选的那些。配置的内容在这里重新从文件里读，页面只给了"哪个来源的哪个名字"。"""
        found = {source.id: source for source in mcp_imports.sources(self.app.config.workspace)}
        added, skipped = [], []
        for source_id in dict.fromkeys(source for source, _ in picks):
            source = found.get(source_id)
            names = [name for source, name in picks if source == source_id]
            if source is None or not source.servers:
                skipped += [t("没有找到“{name}”。", name=name) for name in names]
                continue
            try:
                more, missed = mcp_imports.apply(self.app.mcp.config, source.servers, names)
            except (ConfigError, OSError) as exc:
                return str(exc), added, skipped
            added, skipped = added + more, skipped + missed
        self.app.mcp.sync(restart=frozenset(added))
        return "", added, skipped

    def mcp_paste(self, text: str, names: list[str] | None) -> tuple[str, dict]:
        """粘贴的 JSON：names 为 None 时只是看看里面有什么；给了 names 就把这几个加进来。"""
        try:
            servers = mcp_imports.parse_pasted(text)
            if names is None:
                listed = mcp_imports.preview([mcp_imports.pasted(servers)], self.app.mcp.config)
                return "", {"servers": listed[0]["servers"]}
            added, skipped = mcp_imports.apply(self.app.mcp.config, servers, names)
        except (ConfigError, OSError) as exc:
            return str(exc), {}
        self.app.mcp.sync(restart=frozenset(added))
        return "", {"added": added, "skipped": skipped}

    def mcp_reveal(self) -> tuple[str, str]:
        """在文件管理器里显示 mcp.json（还没有的话打开数据目录）。路径是程序自己定的，浏览器什么都指定不了。"""
        target = self.app.config.mcp_file
        if not target.exists():
            target = self.app.config.data_dir
            private_dir(target)
        try:
            reveal.reveal_in_file_manager(target)
        except OSError as exc:
            return t("没能打开文件管理器（{error}）。文件在：{path}", error=exc, path=target), ""
        return "", str(target)


def route_mcp(handler: Handler, action: str, body: dict) -> None:
    web = handler.server.web
    mcp = web.app.mcp
    name = body.get("name")
    bad = lambda: handler._send_json(400, {"error": t("参数不正确")})
    if action == "refresh":  # 页面打开了 MCP 这一页：配置文件被你手动改过的话，照着新的同步
        mcp.refresh()
        handler._send_json(200, {"ok": True})
    elif action == "scan":
        handler._send_json(200, {"ok": True, **web.mcp_scan()})
    elif action == "reveal":
        error, shown = web.mcp_reveal()
        handler._answer(error, path=shown)
    elif action == "import":
        picks = body.get("picks")
        if not isinstance(picks, list) or not picks or len(picks) > 200 or not all(
            isinstance(pick, dict) and isinstance(pick.get("source"), str) and isinstance(pick.get("name"), str) for pick in picks
        ):
            bad()
            return
        error, added, skipped = web.mcp_import([(pick["source"], pick["name"]) for pick in picks])
        handler._answer(error, added=added, skipped=skipped)
    elif action == "paste":
        text, names = body.get("text"), body.get("names")
        if not isinstance(text, str) or not (names is None or (isinstance(names, list) and all(isinstance(item, str) for item in names))):
            bad()
            return
        error, data = web.mcp_paste(text, names)
        handler._answer(error, **data)
    elif action == "save":
        original, form = body.get("original"), body.get("server")
        if not (original is None or isinstance(original, str)) or not isinstance(form, dict):
            bad()
            return
        handler._finish(web.mcp_change(mcp.save, form, original))
    elif not isinstance(name, str):
        bad()
    elif action == "detail":
        error, detail = web.mcp_detail(name)
        handler._answer(error, server=detail)
    elif action == "delete":
        handler._finish(web.mcp_change(mcp.remove, name))
    elif action == "restart":
        handler._finish(web.mcp_change(mcp.restart, name))
    elif action == "enable" and isinstance(body.get("enabled"), bool):
        handler._finish(web.mcp_change(mcp.set_enabled, name, body["enabled"]))
    elif action == "approval" and body.get("approval") in ("ask", "auto") and (body.get("tool") is None or isinstance(body.get("tool"), str)):
        handler._finish(web.mcp_change(mcp.set_approval, name, body.get("tool"), body["approval"]))
    else:
        bad()
