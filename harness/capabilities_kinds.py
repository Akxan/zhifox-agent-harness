from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .agents import tools as agent_tools
from .i18n import k
from .mcp.bridge import PREFIX as MCP_PREFIX
from .mcp.bridge import McpTool
from .tools import agent as agent_tool
from .tools import files, shell, skill
from .tools import send as send_tool
from .tools.base import Tool
from .tools.browser import is_browser
from .tools.computer import is_computer


@dataclass
class Rule:
    text: str
    outcome: str  # free / ask / always / never
    sentence: str = field(default="", repr=False)  # 拼"一句话的总结"时用的说法（已经按 outcome 挑好了）


@dataclass
class Approval:
    level: str
    summary: str
    rules: list[Rule]
    note: str = ""


@dataclass
class Action:
    kind: str
    target: str
    label: str


@dataclass
class Capability:
    id: str
    icon: str
    label: str
    description: str
    status: str
    reason: str = ""
    count: int | None = None
    approval: Approval | None = None
    where: str = "local"
    where_text: str = ""
    tools: list[str] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)


@dataclass
class Overview:
    items: list[Capability]
    mode: str
    started: bool
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Kind:
    """一种功能：编号、图标、名字、它能替你做什么（原文，用的时候交给 t()）。"""

    id: str
    icon: str
    label: str
    description: str


# 功能的种类，按页面上的顺序。新加一种工具：在这里加一项、在 _GROUP_OF 里登记它的名字、在 overview 里写它的状态
KINDS = (
    Kind("todo", "todo", k("待办"), k("记下要做的事和截止日期，做完了替你勾掉。")),
    Kind("tasks", "clock", k("定时任务"), k("到时间提醒你，或者按时自己去做一件事，比如每天早上整理待办。")),
    Kind("memory", "bookmark", k("记忆"), k("记住你的偏好和重要的事，以后的对话里还记得。")),
    Kind("files", "file", k("读写文件"), k("读、改工作目录里的文件，也能看图片。")),
    Kind("shell", "terminal", k("执行命令"), k("在这台电脑上运行命令：查东西、跑脚本、装软件。")),
    Kind("web", "globe", k("联网搜索和读网页"), k("搜索最新的信息、读你给的网址，回答里标出来源。")),
    Kind("skill", "skill", k("Skill"), k("照着你装的 Skill 做一类事：里面有步骤、模板和脚本。")),
    Kind("agent", "bot", k("交给子 Agent"), k("把一件费时的事交给你定制的 Agent，它做完把结论交回来。")),
    Kind("browser", "browser", k("浏览器"), k("用智狐自己的 Chrome 打开网页、点按钮、填表单。")),
    Kind("computer", "monitor", k("操作整台电脑"), k("看你的屏幕，替你点鼠标、打字，操作任何程序。")),
    Kind("send", "send", k("把文件发回聊天"), k("在聊天软件里聊的时候，把截图、表格、文档直接发给你。")),
)
_KIND = {kind.id: kind for kind in KINDS}
MCP_KIND = Kind("mcp", "plug", k("MCP 服务器"), k("接入第三方的 MCP 服务器，用上它们提供的工具。"))

# 工具的名字 → 它属于哪种功能（浏览器、电脑、MCP 的工具按种类认，见 group_of）
_GROUP_OF = {
    **{name: "todo" for name in agent_tools.TODO_TOOLS},
    **{name: "tasks" for name in agent_tools.TASK_TOOLS},
    "memory": "memory",
    files.TOOL_NAME: "files",
    shell.TOOL_NAME: "shell",
    "web_search": "web",
    "web_fetch": "web",
    skill.TOOL_NAME: "skill",
    agent_tool.TOOL_NAME: "agent",
    send_tool.TOOL_NAME: "send",
}

LOCAL = k("在这台电脑上执行")


def group_of(tool: Tool) -> str:
    """一个工具属于哪种功能。认不出来的是 "other"（照样列出来，用它自己的名字，不会因为不认识就藏起来）。"""
    if isinstance(tool, McpTool) or tool.name.startswith(MCP_PREFIX):
        return "mcp"
    if is_browser(tool):
        return "browser"
    if is_computer(tool):
        return "computer"
    return _GROUP_OF.get(tool.name, "other")


@dataclass(frozen=True)
class Label:
    """一次工具调用给人看的名字（运行记录用，见 trace/）：属于哪种功能、图标、名字（原文，页面和终端各自交给 t()）。
    MCP 的工具另外带上服务器的名字（你起的，原样显示）。认不出来的工具 label 是空的，显示它自己的名字。"""

    group: str
    icon: str
    label: str
    server: str = ""


def label_of(tool: Tool | None) -> Label:
    """一个工具给人看的名字。和信息栏"智狐能做的事"是同一张表（KINDS），所以两边的叫法一样。"""
    if tool is None:
        return Label("other", "puzzle", "")
    group = group_of(tool)
    if group == "mcp":
        return Label("mcp", MCP_KIND.icon, MCP_KIND.label, tool.server if isinstance(tool, McpTool) else "")
    kind = _KIND.get(group)
    return Label(group, kind.icon, kind.label) if kind is not None else Label("other", "puzzle", "")


def technical_names(tools: Iterable[Tool]) -> list[str]:
    """发给模型的名字。Claude 的工具包在请求里只占一项，写它的类型（browser_toolset_20260801），不一个个列成员。"""
    names: list[str] = []
    for tool in tools:
        name = str(tool.spec.get("type") or tool.toolset) if tool.toolset else tool.name
        if name not in names:
            names.append(name)
    return names
