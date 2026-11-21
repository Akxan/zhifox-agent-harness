"""一个 Agent 能用哪些工具：文件里 tools 一项的写法，和"按它从一套工具里挑出能用的"。

tools 里写的是这些名字（和 Claude Code 的叫法一样，所以那边写好的 Agent 文件原样能用）：

  名字         放开的是                                   智狐里的工具
  Read         读文件（只能看，改不了）                   str_replace_based_edit_tool，只认 view
  Edit         读、写文件                                 str_replace_based_edit_tool
  Bash         执行命令                                   run_command
  WebSearch    联网搜索（只有 Claude 有）                 web_search
  WebFetch     读网页（只有 Claude 有）                   web_fetch
  Memory       长期记忆                                   memory
  TodoWrite    待办                                       todo_add、todo_list、todo_update、todo_delete
  Schedule     定时任务                                   task_add、task_list、task_delete
  Skill        读你装的 Skill                             skill
  Agent        把任务再交给别的 Agent（层数有上限）       agent
  Browser      用智狐的浏览器（设置里开着才有）           浏览器的全部工具（Claude 的工具包成员、别家的 browser_*）
  Computer     操作整台电脑（设置里开着才有）             电脑的全部工具（Claude 的工具包成员、别家的 computer_*）。每一步照样要你批准
  mcp__服务器__工具   一个 MCP 工具
  mcp__服务器         这个 MCP 服务器的全部工具

另外也认 Claude Code 的几个别名（Write、MultiEdit → Edit；Grep、Glob、LS → Read；Task → Agent），
以及智狐里工具的真名（run_command、todo_add……）。都不是的名字算"智狐没有这个工具"：不报错，只提醒，挑的时候忽略。

两条规矩：
  - 不写 tools：和叫它的那一方能用的一样多。写了：只能用写了的，空的（tools: []）就是一个工具都没有。
  - 挑出来的永远是"叫它的那一方手里有的"的一部分，不会更多（见 allowed）。所以一个只许读文件的角色叫来的子 Agent，
    文件里写了 Edit 也改不了文件；没有联网的模型（DeepSeek）用的 Agent，写了 WebSearch 也搜不了。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from ..mcp.bridge import PREFIX as MCP_PREFIX
from ..mcp.bridge import McpTool, server_prefix
from ..tools import agent as agent_tool
from ..tools import files, shell, skill
from ..tools import tasks as task_tools
from ..tools.base import Tool
from ..tools.browser import is_browser
from ..tools.computer import is_computer

AGENT_TOOL = agent_tool.TOOL_NAME  # 把任务交给一个 Agent 的那个工具
TODO_TOOLS = ("todo_add", "todo_list", "todo_update", "todo_delete")
TASK_TOOLS = (task_tools.ADD, task_tools.LIST, task_tools.DELETE)

# 名字 → 它放开的真实工具。顺序就是页面上清单的顺序
BUILTIN: dict[str, tuple[str, ...]] = {
    "Read": (files.TOOL_NAME,),
    "Edit": (files.TOOL_NAME,),
    "Bash": (shell.TOOL_NAME,),
    "WebSearch": ("web_search",),
    "WebFetch": ("web_fetch",),
    "Memory": ("memory",),
    "TodoWrite": TODO_TOOLS,
    "Schedule": TASK_TOOLS,
    "Skill": (skill.TOOL_NAME,),
    "Agent": (AGENT_TOOL,),
    "Browser": (),  # 工具太多、名字两套（见 tools/browser.py），不一个个列：按 is_browser 认（见 allowed）
    "Computer": (),  # 同上，按 is_computer 认
}
ALIASES = {
    "Write": "Edit",
    "MultiEdit": "Edit",
    "NotebookEdit": "Edit",
    "Grep": "Read",
    "Glob": "Read",
    "LS": "Read",
    "NotebookRead": "Read",
    "Task": "Agent",
}
# 工具的真名也认，换成上面的叫法（文件工具的真名放开的是读和写）
_REAL = {real: name for name, reals in BUILTIN.items() for real in reals if name != "Read"}


def canonical(name: str) -> str | None:
    """一个写法 → 上面表里的叫法（MCP 的名字原样返回）。认不出来返回 None。"""
    if name in BUILTIN:
        return name
    if name in ALIASES:
        return ALIASES[name]
    if name in _REAL:
        return _REAL[name]
    if name.startswith(MCP_PREFIX) and len(name) > len(MCP_PREFIX):
        return name
    return None


def unknown(names: Iterable[str]) -> list[str]:
    """智狐没有的那些名字（去掉重复，保持原来的顺序）。"""
    return list(dict.fromkeys(name for name in names if canonical(name) is None))


def meaning(names: list[str] | None) -> frozenset[str] | None:
    """一串名字放开的到底是什么（按叫法去掉重复；Edit 已经包括了 Read）。用来比较两种写法是不是一回事。"""
    if names is None:
        return None
    found = {canonical(name) or name for name in names}
    if "Edit" in found:
        found.discard("Read")
    return frozenset(found)


def form(names: list[str] | None) -> dict:
    """表单里的样子：all 表示"和叫它的那一方一样"；picked 是勾上的（按叫法，MCP 的原样），unknown 是智狐没有的（原样留着）。"""
    if names is None:
        return {"all": True, "picked": [], "unknown": []}
    picked = list(dict.fromkeys(canonical(name) for name in names if canonical(name) is not None))
    if "Edit" in picked and "Read" in picked:
        picked.remove("Read")
    return {"all": False, "picked": picked, "unknown": unknown(names)}


def _read_only(tool: Tool) -> Tool:
    """文件工具的"只许读"版本：同一份说明书（模型认得它），但遇到改文件的命令直接退回，不问用户（见 Tool.check）。"""

    def check(tool_input: dict) -> str | None:
        if tool_input.get("command") in files.WRITE_COMMANDS:
            return "这个 Agent 只能读文件，不能创建或修改文件。需要改的话，把要改什么写进你的回答里，交给叫你的那一方去做。"
        return None

    return replace(tool, check=check)


def allowed(names: list[str] | None, tools: Iterable[Tool]) -> list[Tool]:
    """按 tools 一项，从 tools 这一套里挑出能用的（顺序不变）。只会挑出更少，不会多出任何一个。"""
    tools = list(tools)
    if names is None:
        return tools
    wanted = meaning(names) or frozenset()
    real = {real for name in wanted if name in BUILTIN for real in BUILTIN[name]}
    picked = []
    for tool in tools:
        if tool.name == files.TOOL_NAME and "Edit" not in wanted:
            if "Read" in wanted:
                picked.append(tool if tool.check is not None else _read_only(tool))
            continue
        if tool.name in real or _mcp_allowed(tool, wanted) or ("Browser" in wanted and is_browser(tool)) or ("Computer" in wanted and is_computer(tool)):
            picked.append(tool)
    return picked


def _mcp_allowed(tool: Tool, wanted: frozenset[str]) -> bool:
    if not tool.name.startswith(MCP_PREFIX):
        return False
    if tool.name in wanted:
        return True
    server = tool.server if isinstance(tool, McpTool) else ""
    return bool(server) and (server_prefix(server) in wanted or MCP_PREFIX + server in wanted)


def builtin_names(web: bool) -> list[str]:
    """页面上清单里列的那几样。web：这个程序开没开联网（关了就不列 WebSearch 和 WebFetch）。"""
    return [name for name in BUILTIN if web or name not in ("WebSearch", "WebFetch")]


def has_web(tools: Iterable[Tool]) -> bool:
    """这一套工具里有没有联网搜索（决定提示词里写不写"能联网"那一行）。"""
    return any(tool.name in ("web_search", "web_fetch") for tool in tools)
