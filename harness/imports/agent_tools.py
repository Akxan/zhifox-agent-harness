"""各家 Agent 文件里工具的叫法 → 智狐的叫法（agents/tools.py 的 Read、Edit、Bash……）。

同一件事各家叫法不同："执行命令"在 Claude Code 里叫 Bash，在 GitHub Copilot 里叫 execute，在 Gemini CLI 里叫
run_shell_command，在 Kiro 里叫 shell，在 Factory 里叫 Execute。导入时照下面的表换成智狐的叫法；
表里没有的（智狐没有这个工具）不搬，列出来告诉你。

换的时候只会更少，不会更多：一个工具认不出来，就当它没有，不会为了"大概是这个意思"多放开什么。
写着"全部"的（*、all）换成不写 tools，意思是"和叫它的那一方一样多"，和 Claude Code 不写 tools 是一回事。

MCP 的工具各家也写得不一样：
  Claude Code、Qwen Code      mcp__服务器__工具、mcp__服务器
  GitHub Copilot、VS Code     服务器/工具、服务器/*
  Kiro、Amazon Q              @服务器/工具、@服务器
  Gemini CLI                  mcp_服务器_工具（服务器和工具之间也是一个下划线，分不开，所以不搬）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..agents.tools import canonical
from ..mcp.bridge import server_prefix

_READ, _EDIT, _BASH = ("Read",), ("Edit",), ("Bash",)
_WEB = ("WebSearch", "WebFetch")

# 叫法 → 智狐的叫法。查的时候不分大小写
VOCABULARIES: dict[str, dict[str, tuple[str, ...]]] = {
    # https://docs.github.com/en/copilot/reference/custom-agents-configuration （别名）和 VS Code 的工具集
    # https://code.visualstudio.com/docs/agents/reference/custom-agent-file-reference
    "copilot": {
        "execute": _BASH, "shell": _BASH, "bash": _BASH, "powershell": _BASH, "runcommands": _BASH, "runinterminal": _BASH, "terminal": _BASH,
        "read": _READ, "readfile": _READ, "codebase": _READ, "search": _READ, "grep": _READ, "glob": _READ, "usages": _READ, "problems": _READ, "changes": _READ,
        "edit": _EDIT, "editfiles": _EDIT, "write": _EDIT, "multiedit": _EDIT, "notebookedit": _EDIT, "new": _EDIT,
        "web": _WEB, "fetch": ("WebFetch",), "websearch": ("WebSearch",), "webfetch": ("WebFetch",),
        "agent": ("Agent",), "task": ("Agent",), "runsubagent": ("Agent",), "custom-agent": ("Agent",),
        "todo": ("TodoWrite",), "todos": ("TodoWrite",), "todowrite": ("TodoWrite",),
    },
    # https://geminicli.com/docs/core/subagents/ （工具的名字见 https://geminicli.com/docs/tools/ ）
    "gemini": {
        "read_file": _READ, "read_many_files": _READ, "list_directory": _READ, "glob": _READ, "search_file_content": _READ, "grep_search": _READ,
        "write_file": _EDIT, "replace": _EDIT, "edit": _EDIT,
        "run_shell_command": _BASH,
        "google_web_search": ("WebSearch",), "web_search": ("WebSearch",), "web_fetch": ("WebFetch",),
        "save_memory": ("Memory",), "write_todos": ("TodoWrite",),
    },
    # https://qwenlm.github.io/qwen-code-docs/en/users/features/sub-agents/
    "qwen": {
        "read_file": _READ, "read_many_files": _READ, "list_directory": _READ, "glob": _READ, "grep_search": _READ, "search_file_content": _READ,
        "write_file": _EDIT, "edit": _EDIT, "replace": _EDIT,
        "run_shell_command": _BASH,
        "web_fetch": ("WebFetch",), "web_search": ("WebSearch",),
        "save_memory": ("Memory",), "todo_write": ("TodoWrite",), "task": ("Agent",), "skill": ("Skill",),
    },
    # https://opencode.ai/docs/agents/ 、https://opencode.ai/docs/migrate-v1 （V1 的 tools 和 V2 的 permissions 用的动作名）
    "opencode": {
        "read": _READ, "glob": _READ, "grep": _READ, "list": _READ, "lsp": _READ,
        "write": _EDIT, "edit": _EDIT, "patch": _EDIT, "multiedit": _EDIT,
        "bash": _BASH, "shell": _BASH,
        "webfetch": ("WebFetch",), "websearch": ("WebSearch",),
        "todowrite": ("TodoWrite",), "todoread": ("TodoWrite",), "task": ("Agent",), "subagent": ("Agent",), "skill": ("Skill",),
    },
    # https://docs.factory.com/cli/configuration/custom-droids （类别和工具编号）
    "factory": {
        "read-only": _READ, "read": _READ, "ls": _READ, "grep": _READ, "glob": _READ,
        "edit": _EDIT, "create": _EDIT, "applypatch": _EDIT,
        "execute": _BASH,
        "web": _WEB, "websearch": ("WebSearch",), "fetchurl": ("WebFetch",),
        "todowrite": ("TodoWrite",), "task": ("Agent",), "skill": ("Skill",),
    },
    # https://kiro.dev/docs/custom-agents/configuration-reference/ 、https://aws.github.io/amazon-q-developer-cli/agent-format.html
    "kiro": {
        "read": _READ, "fs_read": _READ, "glob": _READ, "grep": _READ,
        "write": _EDIT, "fs_write": _EDIT,
        "shell": _BASH, "execute_bash": _BASH,
        "web": _WEB, "web_search": ("WebSearch",), "web_fetch": ("WebFetch",),
        "subagent": ("Agent",), "todo_list": ("TodoWrite",),
        "@builtin": ("Read", "Edit", "Bash", "WebSearch", "WebFetch"),
    },
}
ALL = {"*", "all", "@all"}
_MCP_ALL = {"@mcp", "mcp", "mcp_*"}
_NOT_MCP = {"vscode", "browser"}  # VS Code 里这两个开头的是编辑器自己的工具，不是 MCP 服务器


@dataclass
class Translation:
    tools: list[str] | None  # 换好的（None：不写 tools，和叫它的那一方一样）
    changed: list[tuple[str, str]] = field(default_factory=list)  # [(原来的叫法, 换成的)]
    dropped: list[str] = field(default_factory=list)  # 智狐没有、没有搬过来的


def translate(names: list[str] | None, vocabulary: str) -> Translation:
    """把一串工具名换成智狐的叫法。names 是 None（文件里没写）的，原样是 None。"""
    if names is None:
        return Translation(None)
    words = VOCABULARIES.get(vocabulary, {})
    out: list[str] = []
    result = Translation(out)
    for name in names:
        name = name.strip()
        if not name:
            continue
        if name.lower() in ALL:
            return Translation(None, result.changed, result.dropped)
        mapped = _one(name, words)
        if mapped is None:
            result.dropped.append(name)
            continue
        if list(mapped) != [name]:
            result.changed.append((name, ", ".join(mapped)))
        out += [item for item in mapped if item not in out]
    return result


def _one(name: str, words: dict[str, tuple[str, ...]]) -> tuple[str, ...] | None:
    lower = name.lower()
    if lower in words:
        return words[lower]
    if canonical(name) is not None:
        return (name,)  # 已经是智狐（Claude Code）的叫法
    if lower in _MCP_ALL:
        return None
    match = re.fullmatch(r"@?([\w.\-]+)/(\*|[\w.\-]+)", name)
    if match:  # 服务器/工具（Copilot、VS Code）、@服务器/工具（Kiro）
        server, tool = match.groups()
        if server.lower() in words:  # VS Code 的 edit/editFiles、web/fetch 这种：前面一段是类别，后面一段认得的话更准
            return words.get(tool.lower(), words[server.lower()])
        if server.lower() in _NOT_MCP:
            return None
        return (server_prefix(server),) if tool == "*" else (f"{server_prefix(server)}__{re.sub(r'[^A-Za-z0-9_-]', '_', tool)}",)
    match = re.fullmatch(r"@([\w.\-]+)", name)
    if match:
        return (server_prefix(match.group(1)),)
    return None
