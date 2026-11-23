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
  Action      kind：settings（target 是 browser / computer / sandbox）/ extensions（skills / mcp / agents / channels）/
              models（打开换模型的面板）/ approval / commands / memory / tasks / new（开始新对话）；label 是按钮上的话
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .capabilities_approval import Probe
from .capabilities_builtin import _agent, _files, _memory, _shell, _skill, _tasks, _todo
from .capabilities_context import _Context
from .capabilities_kinds import (
    KINDS,
    LOCAL,
    MCP_KIND,
    Action,
    Approval,
    Capability,
    Kind,
    Label,
    Overview,
    Rule,
    group_of,
    label_of,
    technical_names,
)
from .capabilities_mcp import _max_tools, _mcp
from .capabilities_switched import _NO_DESKTOP, _browser, _computer, _send, _web
from .i18n import t

__all__ = [
    "KINDS",
    "LOCAL",
    "MCP_KIND",
    "_NO_DESKTOP",
    "Action",
    "Approval",
    "Capability",
    "Kind",
    "Label",
    "Overview",
    "Probe",
    "Rule",
    "group_of",
    "label_of",
    "overview",
    "technical_names",
]


# ---- 汇总 -----------------------------------------------------------------------


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


def _others(context: _Context) -> list[Capability]:
    """认不出是哪种功能的工具（以后新加了工具、忘了在这里登记）：照样列出来，用它自己的名字。"""
    return [
        Capability(id=f"other:{tool.name}", icon="puzzle", label=tool.name, description=t("这个工具还没有给人看的说明。"), status="on",
                   where="local" if tool.run is not None else "provider", where_text=t(LOCAL) if tool.run is not None else t("在厂商的服务器上执行"),
                   tools=[tool.name])
        for tool in context.here("other")
    ]


def _notes(context: _Context) -> list[str]:
    notes: list[str] = []
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
