"""智狐能做的事 · MCP 服务器，每个服务器一行（见 capabilities.py 开头）。

一个服务器都没有时，是"MCP 服务器"这件事本身的一行（_mcp_overall）；有的话每个服务器一行（_server）：连上了没有、有几个工具、
放没放进这段对话（一段对话最多带几个 MCP 工具，见 mcp/manager.py）、角色挡没挡住它、哪几个工具设成了不用问。
对话中途删掉的服务器照样列一行，不藏起来。

为什么单独一个文件：MCP 不是 KINDS 里的一种，而是每个服务器一行：名字是你起的，在哪儿执行要看它怎么连（本机的程序还是远处的服务器）。
"""

from __future__ import annotations

from typing import Any

from .capabilities_approval import _approval
from .capabilities_context import _Context
from .capabilities_kinds import (
    LOCAL,
    MCP_KIND,
    Action,
    Capability,
    Rule,
    technical_names,
)
from .i18n import k, t
from .mcp.bridge import PREFIX as MCP_PREFIX
from .mcp.bridge import McpTool, server_prefix
from .mcp.config import transport_of
from .tools.base import Tool

# ---- MCP：每个服务器一行 -------------------------------------------------------------

_MCP_DESCRIPTION = k("第三方 MCP 服务器提供的工具。")
_MCP_STATES = {"connecting": k("正在连接……"), "disabled": k("已停用。")}


def _mcp(context: _Context) -> list[Capability]:
    app = context.app
    servers = app.mcp.status(context.started)
    here = context.here("mcp")
    listed = {server["name"] for server in servers}
    # 对话中途删掉的服务器：它的工具还在这段对话里（还发给模型，调用会失败），照样列一行，不藏起来
    gone = sorted({tool.server for tool in here if isinstance(tool, McpTool) and tool.server not in listed})
    rows = [_server(context, server, [tool for tool in here if isinstance(tool, McpTool) and tool.server == server["name"]]) for server in servers]
    for name in gone:
        tools = [tool for tool in here if isinstance(tool, McpTool) and tool.server == name]
        rows.append(_mcp_row(name, "off", t("这个服务器已经删掉了。这段对话里调用它的工具会失败。"), where="mcp-local", count=len(tools), tools=technical_names(tools)))
    unknown = [tool for tool in here if not isinstance(tool, McpTool)]  # 名字是 mcp__ 开头、却认不出是哪个服务器的（不该有）
    if unknown:
        rows.append(_mcp_row("mcp__", "on", where="mcp-local", count=len(unknown), tools=technical_names(unknown)))
    if rows:
        return rows
    persona = context.persona
    if persona is not None and persona.spec.tools is not None and not any(name.startswith(MCP_PREFIX) for name in persona.spec.tools):
        return [_mcp_overall("unavailable", context.persona_reason())]
    return [_mcp_overall("off", t("还没有接入 MCP 服务器。"), actions=[Action("extensions", "mcp", t("添加服务器"))])]


def _mcp_overall(status: str, reason: str, **fields: Any) -> Capability:
    """一个服务器都没有时的那一行：MCP 这件事本身。"""
    return Capability(id="mcp", icon=MCP_KIND.icon, label=t(MCP_KIND.label), description=t(MCP_KIND.description), status=status, reason=reason,
                      where="mcp-local", where_text=t(LOCAL), **fields)


def _mcp_row(name: str, status: str, reason: str = "", where: str = "mcp-local", **fields: Any) -> Capability:
    """一个 MCP 服务器的一行。名字是你起的，原样显示（不翻译）。"""
    where_text = t("在第三方的服务器上执行") if where == "mcp-remote" else t("在这台电脑上运行的第三方程序里执行")
    fields.setdefault("actions", [Action("extensions", "mcp", t("管理"))])
    return Capability(id=f"mcp:{name}", icon=MCP_KIND.icon, label=name, description=t(_MCP_DESCRIPTION), status=status, reason=reason,
                      where=where, where_text=where_text, **fields)


def _server(context: _Context, server: dict, here: list[Tool]) -> Capability:
    name = server["name"]
    entry = context.app.mcp.config.get(name) or {}
    where = "mcp-remote" if transport_of(entry) in ("http", "sse") else "mcp-local"

    def row(status: str, reason: str = "", **fields: Any) -> Capability:
        return _mcp_row(name, status, reason, where=where, **fields)

    ready = server["status"] == "ready"
    if here:
        tools = technical_names(here)
        if not ready:
            return row("off", t("连接断开了。这段对话里调用它的工具会失败。"), count=len(here), tools=tools)
        asks = [tool for tool in here if context.permissions.allowance(tool.name, {}, tool) is None]
        free = len(here) - len(asks)
        rules = []
        if free:
            rules.append(Rule(t("{n} 个工具设成了不用问", n=free), "free", t("{n} 个工具不用问", n=free)))
        if asks:
            rules.append(Rule(t("{n} 个工具调用前先问你", n=len(asks)), "ask", t("其余的先问你") if free else t("每次调用都先问你")))
        note = k("工具来自第三方：它说自己“只读”也没法验证，所以默认每次都问。可以在“扩展 → MCP”里按工具放开。")
        reason = t("服务器上的工具变了，下一段对话用新的。") if server.get("stale") else ""
        return row("on", reason, count=len(here), approval=_approval(rules, all_free=k("直接调用，不用问你"), note=note), tools=tools)
    if ready and _persona_blocks_server(context, name):
        return row("unavailable", context.persona_reason(), count=server["tools"])
    if server["status"] == "failed":
        error = server.get("error") or ""
        return row("off", t("连接失败：{error}", error=error) if error else t("连接失败。"))
    if not ready:
        return row("off", t(_MCP_STATES.get(server["status"], k("已停用。"))))
    if not server["tools"]:
        return row("off", t("这个服务器没有提供工具。"), count=0)
    if context.started:
        return row("pending", t("刚连上，下一段对话开始可以用。"), count=server["tools"])
    return row("off", t("它的工具没有放进这段对话（一段对话最多带 {n} 个 MCP 工具）。", n=_max_tools()), count=server["tools"])


def _persona_blocks_server(context: _Context, server: str) -> bool:
    """这段对话的角色有没有挡住这个服务器：整个服务器（mcp__服务器）和其中的哪个工具（mcp__服务器__工具）都没放开。
    放开整个服务器的写法由 agents/tools.py 认（拿一个这个服务器的工具去问它）；只放开了其中几个工具的，看名字的开头。"""
    if not context.blocked(McpTool(spec={"name": f"{server_prefix(server)}__probe"}, server=server, remote="probe")):
        return False
    return not any(name.startswith(server_prefix(server) + "__") for name in context.persona.spec.tools)


def _max_tools() -> int:
    from .mcp.manager import MAX_SNAPSHOT_TOOLS

    return MAX_SNAPSHOT_TOOLS
