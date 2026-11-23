"""智狐能做的事 · 一直带着的几样（见 capabilities.py 开头）：待办、定时任务、记忆、读写文件、执行命令、Skill、交给子 Agent。

这几样只要这段对话带着它的工具就在用（没有的话多半是角色没放开它，见 capabilities_context.py 的 _missing）；
要说清楚的主要是问不问你，拿有代表性的调用去问（见 capabilities_approval.py）。执行命令那一项另外说命令在沙箱的哪一档里运行
（_sandbox_facts、_sandbox_reason）、Windows 上为什么每条都问；Skill 和 Agent 还要看装了几个、能用的有几个。

为什么单独一个文件：和 capabilities_switched.py 那几样不同，这里不看设置里的开关，也不看这家模型做不做得了。
"""

from __future__ import annotations

from typing import Any

from . import commands as commands_rules
from .agents import tools as agent_tools
from .capabilities_approval import Probe, _approval
from .capabilities_context import _Context, _item, _missing, _sample
from .capabilities_kinds import Action, Capability, technical_names
from .i18n import k, t
from .sandbox import LEVELS as SANDBOX_LEVELS
from .tools import agent as agent_tool
from .tools import files, shell, skill
from .tools import tasks as task_tools
from .tools.base import Tool
from .tools.shell import ShellTool


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
    if commands_rules.WINDOWS and (listed or context.permissions.mode == "readonly"):
        # Windows 上命令是 PowerShell，一条都不算只读，你加的也不算（见 commands/__init__.py）：上面的"每条都先问"是真的，这里说为什么
        note = t("Windows 上命令在 PowerShell 里运行，还认不出哪些只是看，所以每条命令都先问你；你加的“不用问的命令”暂时也不起作用。")
    elif listed and context.permissions.mode == "readonly":
        note = t("你自己加的 {n} 条命令也不用问。", n=listed)
    elif listed and context.permissions.mode in ("ask", "edits"):
        note = t("你自己加的 {n} 条“不用问的命令”，只在“只读的命令不用问”这一档里算数。", n=listed)
    facts = _sandbox_facts(here)
    if facts is not None and facts.on:
        # 沙箱开着：在沙箱之外运行一条在哪一档都问（或者这次运行根本不许）。拿一次真的调用去问，规矩改了这里跟着变
        escape = Probe(shell.TOOL_NAME, {"command": "ls", "unsandboxed": True}, here[0])
        rules.append(context.rule(k("在沙箱之外运行一条命令"), [escape], k("在沙箱之外运行直接执行"), k("在沙箱之外运行前，每次都问你"), k("不能在沙箱之外运行")))
    approval = _approval(rules, all_free=k("命令都直接执行"), all_ask=k("每条命令都先问你"))
    approval.note = note
    actions = [Action("approval", "", t("改确认方式")), Action("commands", "", t("不用问的命令")), Action("settings", "sandbox", t("沙箱"))]
    return _item("shell", "on", _sandbox_reason(facts), approval=approval, tools=technical_names(here), actions=actions)


def _sandbox_facts(here: list[Tool]) -> Any:
    """这段对话（或者这次运行）的 run_command 带着的沙箱现在是什么情况（见 sandbox/session.py）；没有沙箱这回事是 None。"""
    tool = next((tool for tool in here if isinstance(tool, ShellTool)), None)
    sandbox = getattr(tool.shell, "sandbox", None) if tool is not None else None
    return sandbox.facts() if sandbox is not None else None


def _sandbox_reason(facts: Any) -> str:
    """"执行命令"那一项的一句话：命令在沙箱里的哪一档运行；开着却用不了的，说清楚命令会被拒绝。沙箱关着的不说。"""
    if facts is None:
        return ""
    if facts.on and facts.problem is not None:
        return t("沙箱是“{level}”，可是这台电脑上用不了，命令会被拒绝执行：{problem}", level=t(SANDBOX_LEVELS[facts.level]), problem=facts.problem)
    if facts.on:
        return t("命令在沙箱里运行：{level}", level=t(SANDBOX_LEVELS[facts.level]))
    if facts.default != "off":
        return t("这次运行没有用沙箱")
    return ""


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
