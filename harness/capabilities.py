"""智狐能做的事：把模型手里的工具，换成给人看的"功能"清单（网页信息栏里的那一节、终端里的 /tools）。

为什么要有这一层：模型看到的是一个个工具（todo_add、str_replace_based_edit_tool、mcp__files__echo……），
这些名字是写给模型和程序看的，对你没有意义。你想知道的是：
  - 它能替我做哪些事？（一个功能可能是好几个工具，比如"待办"是 todo_add、todo_list……四个）
  - 这段对话里哪些在用、哪些关着、哪些这里根本用不了，为什么？关着的怎么打开？
  - 做之前会不会先问我？在哪儿做（这台电脑上、模型厂商的服务器上、第三方的 MCP 服务器上）？

这里是唯一把"工具"对应到"功能"的地方。页面和终端只照着这里给的画，不自己判断（"同一条规则只写在一处"）：
  - 一个功能在不在这段对话里：看这段对话真正发给模型的那些工具（App.toolbox）。工具在一段对话开始时定下，
    之后不变（原因见 providers/claude.py），所以设置里中途打开的，这里说"下一段对话开始生效"；
  - 不在的为什么不在：设置里关着、这家模型做不了（联网只有 Claude 有，看不了图片的用不了"操作整台电脑"）、
    这段对话的角色（用一个 Agent 开始的对话）没有放开它、这台电脑上做不了……；
  - 会不会先问你：这里不另写一份规矩，而是拿几个有代表性的操作去问 permissions.py（见 _probe）：
    "改工作目录里的一个文件，现在这一档问不问？""ls 呢？"规矩改了，这里的说法自己跟着变。

清单的格式（overview 返回 Overview，里面给人看的话都是 t() 译好的 Message，webui/capabilities_api.py 再换成页面能就地重译的样子）：

  Overview
    items     [Capability, …]，按下面 KINDS 的顺序，MCP 服务器按名字排在"操作整台电脑"后面
    mode      现在的确认方式（ask / readonly / edits / all，名字见 permissions.MODES）
    started   这段对话开始了没有（开始了，工具就定下来了）
    notes     整体的说明（这段对话的角色只放开了一部分、MCP 的工具太多没放进来……）

  Capability（一个功能）
    id        稳定的编号：todo、files……；MCP 服务器是 "mcp:名字"，一个服务器都没有时是 "mcp"
    icon      图标的名字（页面上的 #i-<icon>）
    label     名字（MCP 服务器的是它自己的名字，原样显示）
    description  它能替你做什么
    status    on 这段对话在用 / pending 打开了，下一段对话开始生效 / off 关着（可以打开）/ unavailable 这里用不了
    reason    为什么是这个状态（一句话）；在用的一般是空的，或者补一句"已启用 3 个"
    count     Skill、Agent 有几个能用的，MCP 服务器有几个工具（没有就是 None）
    approval  Approval（要不要先问你），这里用不了的是 None
    where     在哪儿执行：local 这台电脑 / provider 模型厂商的服务器 / mcp-local 这台电脑上的第三方程序 / mcp-remote 第三方的服务器
    where_text  上面那一项给人看的说法
    tools     技术细节：发给模型的名字（Claude 的工具包写包的类型，比如 browser_toolset_20260801）
    actions   [Action, …]：关着的怎么打开、在用的去哪儿管。第一个是最要紧的那个

  Approval
    level     free 都直接做 / some 有的先问 / ask 都先问 / always 都先问，而且哪一档都一样
    summary   一句话（"读工作目录里的文件直接做；改文件前会问你"）
    rules     [Rule, …]：几类有代表性的操作，各自问不问
    note      补充的一句（"子 Agent 的每一步照样按这里的规矩问你"），没有是空的

  Rule        text 这类操作、outcome：free 直接做 / ask 先问你 / always 哪一档都问 / never 根本不做（比如只许读文件的角色去改文件）
  Action      kind：settings（target 是 browser / computer）/ extensions（skills / mcp / agents / channels）/
              models（打开换模型的面板）/ approval / commands / memory / tasks / new（开始新对话）；label 是按钮上的话
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agents import tools as agent_tools
from .browser.session import Facts as BrowserFacts
from .channels import platforms
from .desktop import backend as desktop_backend
from .desktop.keys import DANGEROUS
from .desktop.session import Facts as DesktopFacts
from .i18n import k, t
from .mcp.bridge import PREFIX as MCP_PREFIX
from .mcp.bridge import McpTool, server_prefix
from .mcp.config import transport_of
from .providers import PROVIDERS, caps, find
from .tools import agent as agent_tool
from .tools import files, shell, skill
from .tools import send as send_tool
from .tools import tasks as task_tools
from .tools.base import Tool
from .tools.browser import TOOLSET as BROWSER
from .tools.browser import BrowserTool, is_browser
from .tools.computer import TOOLSET as COMPUTER
from .tools.computer import DesktopTool, is_computer
from .tools.send import SendFacts, SendTool


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
_MCP_DESCRIPTION = k("第三方 MCP 服务器提供的工具。")

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
# 这台电脑上做不了"操作整台电脑"的原因（和页面设置里那一节的说法一样，见 page/scripts/computer.js）
_NO_DESKTOP = {
    desktop_backend.WAYLAND: k("这台电脑用的是 Wayland 桌面：Wayland 不允许一个程序截别的程序的图、替别人点鼠标，所以用不了。登录时换成 X11（Xorg）桌面就可以。"),
    desktop_backend.NO_DISPLAY: k("这台电脑没有图形界面（比如在服务器上），没有屏幕可以操作。"),
    desktop_backend.PLATFORM: k("只能在 macOS 和 Linux（X11 桌面）上用，这台电脑的系统不支持。"),
}


def group_of(tool: Tool) -> str:
    """一个工具属于哪种功能。认不出来的是 "other"（照样列出来，用它自己的名字，不会因为不认识就藏起来）。"""
    if isinstance(tool, McpTool) or tool.name.startswith(MCP_PREFIX):
        return "mcp"
    if is_browser(tool):
        return "browser"
    if is_computer(tool):
        return "computer"
    return _GROUP_OF.get(tool.name, "other")


def technical_names(tools: Iterable[Tool]) -> list[str]:
    """发给模型的名字。Claude 的工具包在请求里只占一项，写它的类型（browser_toolset_20260801），不一个个列成员。"""
    names: list[str] = []
    for tool in tools:
        name = str(tool.spec.get("type") or tool.toolset) if tool.toolset else tool.name
        if name not in names:
            names.append(name)
    return names


# ---- 问不问你：拿几个有代表性的操作去问 permissions.py ---------------------------------


@dataclass(frozen=True)
class Probe:
    """一次假想的工具调用：名字、参数、（需要的话）工具本身。只拿去问 permissions 要不要问你，从来不执行。"""

    name: str
    input: dict
    tool: Any = None


def _probe(permissions: Any, everywhere: Any, probe: Probe) -> str:
    """这一次调用在现在这一档下会怎样：never 根本不做 / free 直接做 / always 哪一档都要问 / ask 先问你。

    和 agent.py 执行一次调用时走的是同一条路：先看工具自己拒不拒绝（Tool.check），再看 review 要不要问、
    auto_allows（这里用 allowance）在这一档下能不能不问。"哪一档都要问"的意思是：换成"全部自动允许"也照样问
    （everywhere 是同样的规矩、模式是 all 的那一份）。
    """
    tool = probe.tool
    if tool is not None and tool.check is not None and tool.check(probe.input):
        return "never"
    if permissions.review(probe.name, probe.input, tool) is None:
        return "free"
    if permissions.allowance(probe.name, probe.input, tool) is not None:
        return "free"
    if everywhere.allowance(probe.name, probe.input, tool) is None:
        return "always"
    return "ask"


_STRICT = ("free", "ask", "always")


def _rule(permissions: Any, everywhere: Any, label: str, probes: list[Probe], free: str, ask: str, never: str = "") -> Rule:
    """一类操作。probes 是这一类里有代表性的几次调用，取最严的那个结果（有一个根本不做，就算根本不做）。
    free、ask、never 是这一类在各种结果下拼进总结里的说法（原文）。"""
    outcomes = [_probe(permissions, everywhere, probe) for probe in probes]
    outcome = "never" if "never" in outcomes else max(outcomes, key=_STRICT.index)
    sentence = {"free": free, "ask": ask, "always": ask, "never": never or ask}[outcome]
    return Rule(t(label), outcome, t(sentence))


def _approval(rules: list[Rule], all_free: str = "", all_ask: str = "", note: str = "") -> Approval:
    """几类操作的结果 → 一句话的总结和一个级别。总结最多两小句："直接做的；要问的"，有根本不做的放在最前面。"""
    free = [rule for rule in rules if rule.outcome == "free"]
    asks = [rule for rule in rules if rule.outcome in ("ask", "always")]
    never = [rule for rule in rules if rule.outcome == "never"]
    if never:
        parts = [never[0].sentence, *[rule.sentence for rule in (asks or free)[:1]]]
    elif free and asks:
        parts = [free[0].sentence, asks[0].sentence]
    elif asks:
        parts = [t(all_ask) if all_ask else asks[0].sentence]
    else:
        parts = [t(all_free or k("直接做，不用问你"))]
    summary = parts[0] if len(parts) == 1 else t("{first}；{second}", first=parts[0], second=parts[1])
    if not asks:
        level = "free"
    elif free:
        level = "some"
    else:
        level = "always" if all(rule.outcome == "always" for rule in asks) else "ask"
    return Approval(level, summary, rules, t(note) if note else "")


# ---- 汇总 -----------------------------------------------------------------------


@dataclass
class _Context:
    """算一份清单要看的东西。网页上你正在看的这段对话，和一次定时任务、聊天通道的运行，各有一份。"""

    app: Any
    agent: Any
    tools: list[Tool]  # 这段对话真正发给模型的工具
    started: bool
    persona: Any
    channel: str  # 这段会话是在哪个聊天通道里开始的（空的是网页上开的）
    in_chat: bool  # 现在是不是真的在聊天软件里（send_file 发得出去）
    unattended: bool  # 定时任务、聊天通道的运行（"操作整台电脑"另有一个开关）
    trusted: tuple[bool, bool]  # 这段对话里（截图，点击打字）不再问。只有你正看着的对话能选，定时任务、聊天通道的运行都是 False
    skills: list[dict]
    agents: list[dict]
    everywhere: Any = None

    def __post_init__(self) -> None:
        permissions = self.agent.permissions
        self.everywhere = permissions.unattended("all")
        self.everywhere.trust_mcp = permissions.trust_mcp

    @property
    def permissions(self) -> Any:
        return self.agent.permissions

    @property
    def provider(self) -> Any:
        return find(self.agent.backend.provider) or PROVIDERS["anthropic"]

    def here(self, group: str) -> list[Tool]:
        return [tool for tool in self.tools if group_of(tool) == group]

    def blocked(self, *samples: Tool) -> bool:
        """这段对话的角色有没有挡住这些工具（一个都挑不出来）。普通的对话、不限制工具的角色都不挡。"""
        persona = self.persona
        return persona is not None and persona.spec.tools is not None and not agent_tools.allowed(persona.spec.tools, samples)

    def persona_reason(self) -> str:
        return t("这段对话的角色「{name}」没有放开它。", name=self.persona.name)

    def rule(self, label: str, probes: list[Probe], free: str, ask: str, never: str = "") -> Rule:
        return _rule(self.permissions, self.everywhere, label, probes, free, ask, never)


def overview(app: Any, run: Any = None, persona: Any = None, in_chat: bool = False, skills: list[dict] | None = None, agents: list[dict] | None = None) -> Overview:
    """一份"智狐能做的事"。

    run：一次定时任务或者聊天通道里的运行（App.unattended_agent / channel_agent 给的）；不给就是网页上你正在看的这段对话。
    persona：那次运行用的角色（只有给了 run 才看它；你正在看的这段对话用 app.persona）。in_chat：那次运行是不是在聊天软件里。
    skills、agents：状态里已经读好的 Skill、Agent 列表（summary() 的样子），给了就不再读一遍文件夹。
    """
    if run is None:
        tools, started = app.toolbox()
        agent, persona, channel = app.agent, app.persona, app.conversation.channel
        unattended, trusted = False, (bool(app.chat_desktop.trust_screenshots), bool(app.chat_desktop.trust_actions))
    else:
        agent = run.agent
        tools = [tool for tool in agent.tools if agent.backend.sends(tool)]
        started, channel, unattended, trusted = True, "chat" if in_chat else "", True, (False, False)
    context = _Context(
        app=app,
        agent=agent,
        tools=tools,
        started=started,
        persona=persona,
        channel=channel,
        in_chat=in_chat,
        unattended=unattended,
        trusted=trusted,
        skills=skills if skills is not None else [item.summary() for item in app.skills.list()],
        agents=agents if agents is not None else [item.summary() for item in app.agents.list()],
    )
    builders: dict[str, Callable[[_Context], Capability]] = {
        "todo": _todo, "tasks": _tasks, "memory": _memory, "files": _files, "shell": _shell, "web": _web,
        "skill": _skill, "agent": _agent, "browser": _browser, "computer": _computer, "send": _send,
    }
    items: list[Capability] = []
    for kind in KINDS:
        items.append(builders[kind.id](context))
        if kind.id == "computer":
            items += _mcp(context)
    items += _others(context)
    notes = _notes(context)
    if not getattr(agent.backend, "tools_supported", True):
        items, notes = _without_tools(context, items), [_no_tools_reason(context), *notes]
    return Overview(items=items, mode=context.permissions.mode, started=started, notes=notes)


def _no_tools_reason(context: _Context) -> str:
    backend = context.agent.backend
    return t("{provider} 的 {model} 用不了工具（函数调用），这段对话里它只能用文字回答。", provider=context.provider.label, model=backend.model)


def _without_tools(context: _Context, items: list[Capability]) -> list[Capability]:
    """模型用不了工具（自定义接口上的一些模型，见 providers/openai_compat.py）：一个工具都没发给它，每一项都用不了，原因是同一个。
    能做的只有换一个能用工具的模型（换模型的面板里每个模型都标着"能用工具""不能用工具"）。"""
    reason = _no_tools_reason(context)
    out = []
    for item in items:
        item.status, item.reason, item.approval, item.count = "unavailable", reason, None, None
        item.tools, item.actions = [], [Action("models", "", t("换模型"))]
        out.append(item)
    return out


def _item(kind_id: str, status: str, reason: str = "", **fields: Any) -> Capability:
    kind = _KIND[kind_id]
    fields.setdefault("where_text", t(LOCAL))
    return Capability(id=kind.id, icon=kind.icon, label=t(kind.label), description=t(kind.description), status=status, reason=reason, **fields)


def _sample(name: str) -> Tool:
    """一个只有名字的工具：拿去问"角色放没放开它"、"问不问你"。从来不执行。"""
    return Tool(spec={"name": name})


def _missing(context: _Context, kind_id: str, *samples: Tool) -> Capability:
    """一个一直都在的功能，这段对话里却没有：多半是角色没放开它。"""
    reason = context.persona_reason() if context.blocked(*samples) else t("这段对话没有带上它。")
    return _item(kind_id, "unavailable", reason)


def _todo(context: _Context) -> Capability:
    here = context.here("todo")
    if not here:
        return _missing(context, "todo", *[_sample(name) for name in agent_tools.TODO_TOOLS])
    rules = [context.rule(k("添加、修改、完成待办"), [Probe("todo_add", {"title": "…"})], k("待办直接记下"), k("改待办前会问你"))]
    return _item("todo", "on", approval=_approval(rules), tools=technical_names(here))


def _tasks(context: _Context) -> Capability:
    here = context.here("tasks")
    if not here:
        return _missing(context, "tasks", *[_sample(name) for name in agent_tools.TASK_TOOLS])
    schedule = {"type": "daily", "time": "09:00"}
    agent_task = Probe(task_tools.ADD, {"title": "…", "kind": "agent", "prompt": "…", "schedule": schedule})
    reminder = Probe(task_tools.ADD, {"title": "…", "kind": "reminder", "prompt": "…", "schedule": schedule})
    rules = [
        context.rule(k("添加只是提醒的任务"), [reminder], k("只是提醒的直接添加"), k("添加提醒前会问你")),
        context.rule(k("添加、删除让助手自己去做的任务"), [agent_task], k("让助手去做的任务直接添加"), k("让助手自己去做的任务先问你")),
    ]
    note = k("模型添加的任务，到时间自己运行时一律每一步都问你；只有你能在任务里放宽。")
    return _item(
        "tasks", "on", approval=_approval(rules, all_free=k("直接添加，不用问你"), note=note), tools=technical_names(here),
        actions=[Action("tasks", "", t("查看定时任务"))],
    )


def _memory(context: _Context) -> Capability:
    here = context.here("memory")
    if not here:
        return _missing(context, "memory", _sample("memory"))
    rules = [context.rule(k("记下、修改、删除记忆"), [Probe("memory", {"command": "view", "path": "/memories"})], k("记忆直接记下"), k("改记忆前会问你"))]
    return _item("memory", "on", approval=_approval(rules), tools=technical_names(here), actions=[Action("memory", "", t("查看记忆"))])


def _files(context: _Context) -> Capability:
    here = context.here("files")
    if not here:
        return _missing(context, "files", _sample(files.TOOL_NAME))
    tool = here[0]
    workspace = context.permissions.editor.workspace
    inside = str(workspace / "zhifox-probe.txt")
    outside = str(workspace.parent / "zhifox-probe.txt") if workspace.parent != workspace else ""

    def view(path: str) -> Probe:
        return Probe(files.TOOL_NAME, {"command": "view", "path": path}, tool)

    def create(path: str) -> Probe:
        return Probe(files.TOOL_NAME, {"command": "create", "path": path, "file_text": ""}, tool)

    rules = [
        context.rule(k("改工作目录里的文件"), [create(inside)], k("改工作目录里的文件直接做"), k("改文件前会问你"), k("只能读，不能改文件")),
        context.rule(k("读工作目录里的文件"), [view(inside)], k("读工作目录里的文件直接做"), k("读文件前会问你")),
    ]
    if outside:
        rules += [
            context.rule(k("读工作目录之外的文件"), [view(outside)], k("读工作目录之外的文件直接做"), k("读工作目录之外的文件前会问你")),
            context.rule(k("改工作目录之外的文件"), [create(outside)], k("改工作目录之外的文件直接做"), k("改工作目录之外的文件前会问你"), k("只能读，不能改文件")),
        ]
    note = k("读助手自己的数据（以前的会话、设置）一律先问你；.env 这类保存密钥的文件碰都不碰。改之前会备份。")
    return _item("files", "on", approval=_approval(rules, all_free=k("读写文件都直接做"), note=note), tools=technical_names(here))


def _shell(context: _Context) -> Capability:
    here = context.here("shell")
    if not here:
        return _missing(context, "shell", _sample(shell.TOOL_NAME))
    rules = [
        context.rule(k("只读的命令（ls、git status……）"), [Probe(shell.TOOL_NAME, {"command": "ls"})], k("只读的命令直接执行"), k("只读的命令也先问你")),
        context.rule(k("别的命令"), [Probe(shell.TOOL_NAME, {"command": "touch zhifox-probe.txt"})], k("命令直接执行"), k("别的命令先问你")),
    ]
    commands = context.permissions.commands
    listed = len(commands.words()) if commands is not None else 0
    note = ""
    if listed and context.permissions.mode == "readonly":
        note = t("你自己加的 {n} 条命令也不用问。", n=listed)
    elif listed and context.permissions.mode in ("ask", "edits"):
        note = t("你自己加的 {n} 条“不用问的命令”，只在“只读的命令不用问”这一档里算数。", n=listed)
    approval = _approval(rules, all_free=k("命令都直接执行"), all_ask=k("每条命令都先问你"))
    approval.note = note
    actions = [Action("approval", "", t("改确认方式")), Action("commands", "", t("不用问的命令"))]
    return _item("shell", "on", approval=approval, tools=technical_names(here), actions=actions)


def _web(context: _Context) -> Capability:
    here = context.here("web")
    provider = context.provider
    where = {"where": "provider", "where_text": t("在 {provider} 的服务器上执行", provider="Anthropic")}
    if here:
        rules = [context.rule(k("联网搜索、读网页"), [Probe("web_search", {"query": "…"})], k("搜索、读网页直接做"), k("搜索前会问你"))]
        return _item("web", "on", approval=_approval(rules), tools=technical_names(here), **where)
    if not context.app.config.web:
        return _item("web", "unavailable", t("联网搜索在启动时关掉了（HARNESS_WEB=0）。"), **where)
    if not provider.web:
        reason = t("只有 Claude 能联网搜索、读网页，现在用的是 {provider}。", provider=provider.label)
        return _item("web", "unavailable", reason, actions=[Action("models", "", t("换模型"))], **where)
    return _missing(context, "web", _sample("web_search"), _sample("web_fetch"))


def _skill(context: _Context) -> Capability:
    here = context.here("skill")
    manage = [Action("extensions", "skills", t("管理 Skill"))]
    if not here:
        return _missing(context, "skill", _sample(skill.TOOL_NAME))
    enabled = [item for item in context.skills if item.get("enabled")]
    usable = sum(1 for item in enabled if not item.get("problems"))
    tools = technical_names(here)
    if not usable:
        if not context.skills:
            reason = t("还没有装 Skill。")
        elif not enabled:
            reason = t("装了 {n} 个，都没有启用。", n=len(context.skills))
        else:
            reason = t("启用着的 {n} 个都有问题，用不了。", n=len(enabled))
        return _item("skill", "off", reason, count=0, tools=tools, actions=manage)
    rules = [context.rule(k("读取启用了的 Skill"), [Probe(skill.TOOL_NAME, {"name": "…"})], k("读取 Skill 直接做"), k("读取 Skill 前会问你"))]
    note = k("Skill 里的脚本照样按“执行命令”的规矩问你。")
    return _item("skill", "on", t("已启用 {n} 个。", n=usable), count=usable, approval=_approval(rules, note=note), tools=tools, actions=manage)


def _agent(context: _Context) -> Capability:
    here = context.here("agent")
    manage = [Action("extensions", "agents", t("管理 Agent"))]
    if not here:
        return _missing(context, "agent", _sample(agent_tool.TOOL_NAME))
    usable = sum(1 for item in context.agents if not item.get("problems"))
    tools = technical_names(here)
    if not usable:
        reason = t("{n} 个 Agent 的文件都有问题，用不了。", n=len(context.agents)) if context.agents else t("还没有自定义 Agent。")
        return _item("agent", "off", reason, count=0, tools=tools, actions=manage)
    rules = [context.rule(k("把任务交给子 Agent"), [Probe(agent_tool.TOOL_NAME, {"name": "…", "task": "…"})], k("交给子 Agent 直接做"), k("交出去之前会问你"))]
    note = k("子 Agent 的每一步照样按这里的规矩问你：交给它不会放开任何东西。")
    return _item("agent", "on", t("可以交给 {n} 个 Agent。", n=usable), count=usable, approval=_approval(rules, note=note), tools=tools, actions=manage)


def _switched(context: _Context, kind_id: str, here: list[Tool], enabled: bool, target: str, approval: Callable[[], Approval], **fields: Any) -> Capability:
    """浏览器和操作整台电脑：设置里的开关、这段对话里有没有（开始时定下），两样合起来是哪种状态。"""
    settings = Action("settings", target, t("在设置里打开"))
    if here and enabled:
        return _item(kind_id, "on", approval=approval(), tools=technical_names(here), actions=[Action("settings", target, t("设置"))], **fields)
    if here:
        reason = t("设置里关掉了。这段对话里它的工具还在，调用时会回答它关着。")
        return _item(kind_id, "off", reason, tools=technical_names(here), actions=[settings], **fields)
    if enabled:
        return _item(kind_id, "pending", t("设置里已经打开，下一段对话开始生效。"), actions=[Action("new", "", t("开始新对话"))], **fields)
    if context.unattended and kind_id == "computer" and context.app.desktop.enabled:
        return _item(kind_id, "off", t("设置里没有允许定时任务和聊天通道使用它。"), actions=[settings], **fields)
    reason = t("设置里关着。打开以后，下一段对话开始可以用。") if context.started else t("设置里关着。打开以后这段对话就能用。")
    return _item(kind_id, "off", reason, actions=[settings], **fields)


def _browser(context: _Context) -> Capability:
    here = context.here("browser")
    sample = BrowserTool(spec={"name": "browser_probe"}, stands_in_for=BROWSER)
    if not here and context.blocked(sample):
        return _item("browser", "unavailable", context.persona_reason())

    def approval() -> Approval:
        def look(facts: BrowserFacts) -> Probe:
            return Probe(BROWSER, {}, BrowserTool(spec={"name": "browser_probe"}, stands_in_for=BROWSER, facts=lambda _: facts))

        reading = [look(BrowserFacts("other"))]
        acting = [
            look(BrowserFacts("navigate", destination="https://example.com/", new_site=True)),
            look(BrowserFacts("type", found=True, editable=True, role="textbox", name="q", text="…")),
            look(BrowserFacts("click", found=True, role="button", name="…", submits=True)),
        ]
        secret = [
            look(BrowserFacts("type", found=True, editable=True, role="textbox", password=True, text="…")),
            look(BrowserFacts("type", found=True, editable=True, role="textbox", card=True, text="…")),
        ]
        rules = [
            context.rule(k("看网页、截图、滚动"), reading, k("看网页、截图直接做"), k("看网页前会问你")),
            context.rule(k("去一个新的网站、输入文字、提交表单"), acting, k("去新网站、输入、提交直接做"), k("去新网站、输入、提交前会问你")),
            context.rule(k("往密码框、银行卡里输入"), secret, k("密码和银行卡直接输入"), k("密码和银行卡每次都问")),
        ]
        note = k("这段对话里允许过的网站，再去不再问。网页想下载的文件一律不下载。")
        return _approval(rules, note=note)

    where = t("在这台电脑上，智狐自己的 Chrome 里")
    return _switched(context, "browser", here, context.app.browser.enabled, "browser", approval, where_text=where)


def _computer(context: _Context) -> Capability:
    here = context.here("computer")
    provider = context.provider
    found, why = context.app.desktop.backend()
    if found is None:
        # 设置里开着的话，工具照样发给了模型（调用时回答"这台电脑上用不了"），技术细节里照样列出来
        return _item("computer", "unavailable", t(_NO_DESKTOP.get(why, _NO_DESKTOP[desktop_backend.PLATFORM])), tools=technical_names(here))
    if provider.kind != "anthropic" and not caps(provider, context.agent.backend.model).vision:
        reason = t("{provider} 看不了图片，用不了这个功能。", provider=provider.label)
        return _item("computer", "unavailable", reason, actions=[Action("models", "", t("换模型"))])
    sample = DesktopTool(spec={"name": "computer_probe"}, stands_in_for=COMPUTER)
    if not here and context.blocked(sample):
        return _item("computer", "unavailable", context.persona_reason())

    def approval() -> Approval:
        attended = not context.unattended
        looks, acts = context.trusted

        def look(action: str, trusted: bool = False, **fields: Any) -> Probe:
            facts = DesktopFacts(action, trusted=trusted, attended=attended, **fields)
            return Probe(COMPUTER, {}, DesktopTool(spec={"name": "computer_probe"}, stands_in_for=COMPUTER, facts=lambda _: facts))

        # 第一条是点击、打字：一句话的总结先说它（"点击、打字直接做；危险的组合键每次都问"）。危险的组合键单独一条：
        # 不管选了哪一档、放开了什么都要问，所以这里永远不会是"都直接做"
        acting = [look("click", acts, point=(10, 10)), look("type", acts, text="…")]
        danger = [look("key", acts, text="cmd+q", danger=DANGEROUS[0][3])]
        rules = [
            context.rule(k("点鼠标、打字、按键"), acting, k("点击、打字不再问（这段对话里）") if acts else k("点击、打字直接做"), k("点击、打字每一步都问")),
            context.rule(k("截图"), [look("screenshot", looks)], k("截图不再问（这段对话里）") if looks else k("截图直接做"), k("截图每次都问")),
            context.rule(k("危险的组合键（退出程序、退出登录……）"), danger, "", k("危险的组合键每次都问")),
        ]
        if attended:
            note = k(
                "确认卡片上可以选这段对话里截图不再问、点击和打字不再问。不再问以后，屏幕上的任何文字都可能带着它去点、去打字；"
                "页面顶上的“停止”随时能叫停。只是等一等、看鼠标在哪儿的不问。"
            )
        else:
            note = k("定时任务和聊天通道按它自己的那一档问，没有“这段对话里不再问”。只是等一等、看鼠标在哪儿的不问。")
        return _approval(rules, all_ask=k("每一步都先问你"), note=note)

    enabled = context.app.desktop.unattended if context.unattended else context.app.desktop.enabled
    return _switched(context, "computer", here, enabled, "computer", approval)


def _send(context: _Context) -> Capability:
    here = context.here("send")
    channels = [Action("extensions", "channels", t("设置聊天通道"))]
    if not here:
        if context.channel and context.blocked(SendTool(spec={"name": send_tool.TOOL_NAME})):
            return _item("send", "unavailable", context.persona_reason())
        return _item("send", "unavailable", t("只有在聊天通道（Telegram、飞书……）里聊天时才有。"), actions=channels)
    if not context.in_chat:
        reason = t("这段会话是在 {platform} 里开始的。在网页上接着聊时发不出去，文件就在这台电脑上。", platform=platforms.label(context.channel))
        return _item("send", "unavailable", reason, tools=technical_names(here))
    workspace: Path = context.permissions.editor.workspace
    facts = SendFacts(workspace / "zhifox-probe.txt", "zhifox-probe.txt", 1, kind="document", label="Telegram", person="…")
    probe = Probe(send_tool.TOOL_NAME, {"path": "zhifox-probe.txt"}, SendTool(spec={"name": send_tool.TOOL_NAME}, facts=lambda _: facts))
    rules = [context.rule(k("把一个文件发到聊天里"), [probe], k("发文件直接做"), k("每次发之前都先问你"))]
    note = k("保存密钥的文件（.env 这类）在任何一档下都不发。")
    return _item("send", "on", approval=_approval(rules, note=note), tools=technical_names(here))


# ---- MCP：每个服务器一行 -------------------------------------------------------------

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


def _others(context: _Context) -> list[Capability]:
    """认不出是哪种功能的工具（以后新加了工具、忘了在这里登记）：照样列出来，用它自己的名字。"""
    return [
        Capability(id=f"other:{tool.name}", icon="puzzle", label=tool.name, description=t("这个工具还没有给人看的说明。"), status="on",
                   where="local" if tool.run is not None else "provider", where_text=t(LOCAL) if tool.run is not None else t("在厂商的服务器上执行"),
                   tools=[tool.name])
        for tool in context.here("other")
    ]


def _notes(context: _Context) -> list[str]:
    notes = []
    persona = context.persona
    if persona is not None and persona.spec.tools is not None:
        if persona.spec.tools:
            notes.append(t("这段对话用 Agent「{name}」当角色：只能用它放开的功能。", name=persona.name))
        else:
            notes.append(t("这段对话用 Agent「{name}」当角色，它不用任何工具。", name=persona.name))
    dropped = context.app.mcp.active.dropped if context.started else 0
    if dropped:
        notes.append(t("MCP 的工具太多，有 {n} 个没有放进这段对话（一段对话最多带 {max} 个）。", n=dropped, max=_max_tools()))
    return notes
