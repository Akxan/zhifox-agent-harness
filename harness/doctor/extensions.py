"""自检里的"扩展"一节：MCP 服务器、聊天通道、钩子、定时任务，各有几个、开着几个，开着的能不能运行。

只读文件，不连任何服务器、不启动任何程序：
  - MCP 服务器、钩子要运行的程序在不在（按 PATH 找，和真的启动时找的是同一处，Windows 上连 .cmd 一起找）。
    最常见的问题就是这个：配置是从别处抄来的，写着 npx、uvx，这台电脑上却没装。
  - 聊天通道：开着的平台填全了没有、配对了没有、要的 SDK 装了没有（飞书、钉钉，见 channels/platforms.py）。
  - 定时任务的文件读不读得出来。TaskStore 在文件坏了的时候会把它改名留着（_set_aside），自检不能动你的文件，
    所以先自己看一眼是不是合法的 JSON，是了才交给它。

读的都是现成的那几个类（McpConfig、ChannelStore、HookConfig、TaskStore），它们建的时候只读不写。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from ..channels import platforms
from ..channels.store import ChannelStore
from ..config import Config
from ..hooks.config import HookConfig
from ..hooks.runner import argv_of
from ..i18n import t
from ..mcp.config import McpConfig
from ..tasks import TaskStore
from . import fixes
from .report import OK, PROBLEM, UNUSED, Check, Section


def program_found(command: str, cwd: Path | None = None, path: str | None = None) -> bool:
    """一个程序找不找得到：写了路径的看文件在不在，光写名字的按 PATH 找（Windows 上 shutil.which 也认 PATHEXT：npx 就是 npx.cmd）。"""
    program = os.path.expanduser(command)
    if os.path.isabs(program):
        return Path(program).is_file()
    if "/" in program or os.sep in program:
        return ((cwd or Path.cwd()) / program).is_file()
    return shutil.which(program, path=path) is not None


def _broken(found: dict[str, tuple[dict | None, str]]) -> list[str]:
    return [f"{name}（{problem}）" for name, (entry, problem) in found.items() if entry is None]


# ---- MCP --------------------------------------------------------------------------


def mcp(config: Config) -> Check:
    label = t("MCP 服务器")
    store = McpConfig(config.mcp_file)
    edit = t("在网页界面的“扩展 → MCP”里改，或者编辑 {path}", path=config.mcp_file)
    if store.problem:
        return Check("mcp", label, PROBLEM, store.problem, [edit], {"count": 0})
    servers = store.servers()
    enabled = [(name, entry) for name, (entry, _) in servers.items() if entry is not None and entry["enabled"]]
    facts = {"count": len(servers), "enabled": len(enabled)}
    if not servers:
        return Check("mcp", label, UNUSED, t("没有"), facts=facts)
    problems = _broken(servers)
    missing = []
    for name, entry in enabled:
        if "command" not in entry:
            continue
        cwd = Path(entry["cwd"]).expanduser() if entry.get("cwd") else None
        if not program_found(entry["command"], cwd, entry.get("env", {}).get("PATH")):
            missing.append((name, entry["command"]))
    count = t("{n} 个，开着 {on} 个", n=len(servers), on=len(enabled))
    if not problems and not missing:
        return Check("mcp", label, OK, count, facts=facts)
    fix = []
    if problems:
        fix.append(edit)
    for name, command in missing:
        problems.append(t("{name} 要运行的 {command} 找不到", name=name, command=command))
        fix.append(t("装上 {command}，或者在配置里写出它的完整路径（{find} 查得到在哪儿）", command=command, find=fixes.find_program(command)))
    facts["missing"] = [name for name, _ in missing]
    return Check("mcp", label, PROBLEM, t("{count}。{problems}", count=count, problems=t("；").join(problems)), fix, facts)


# ---- 聊天通道 -------------------------------------------------------------------------


def channel_entries(store: ChannelStore) -> list[tuple[str, dict]]:
    """设置过的平台：（给人看的名字，那一份设置）。认不出的平台（旧版本、写错了）不算。"""
    return [(platforms.label(name), store.get(name)) for name in store.known() if name in platforms.LABELS]


def channels(config: Config, store: ChannelStore, running: bool) -> Check:
    """running：现在有没有开着的智狐（网页界面、harness serve、后台服务）。通道只在那时候收消息。"""
    label = t("聊天通道")
    if store.problem:
        return Check("channels", label, PROBLEM, store.problem, [t("修好或者删掉 {path}", path=config.channels_file)], {"count": 0})
    known = [name for name in store.known() if name in platforms.LABELS]
    enabled = [name for name in known if store.get(name)["enabled"]]
    facts = {"count": len(known), "enabled": len(enabled), "platforms": known}
    if not known:
        return Check("channels", label, UNUSED, t("没有"), facts=facts)
    problems, fix = [], []
    for name in enabled:
        shown = platforms.label(name)
        if not store.configured(name):
            problems.append(t("{platform} 开着，可是要填的还没填全", platform=shown))
            fix.append(fixes.harness("channels", "setup", name))
        elif store.get(name)["owner"] is None:
            problems.append(t("{platform} 还没有配对：谁发消息都不会处理", platform=shown))
            fix.append(fixes.harness("channels", "pair", name))
        missing = platforms.missing_sdk(name)
        if missing:
            problems.append(t("{platform} 要的 SDK 还没装", platform=shown))
            fix.append(fixes.in_project(platforms.install_command(name)))
    names = t("、").join(platforms.label(name) for name in enabled)
    count = t("{n} 个，开着 {on} 个：{names}", n=len(known), on=len(enabled), names=names) if names else t("{n} 个，开着 {on} 个", n=len(known), on=0)
    if problems:
        return Check("channels", label, PROBLEM, t("{count}。{problems}", count=count, problems=t("；").join(problems)), fix, facts)
    if enabled and not running:
        count = t("{count}。只在网页界面、harness serve 或者后台服务开着时收消息，现在都没开着", count=count)
    return Check("channels", label, OK, count, facts=facts)


# ---- 钩子 --------------------------------------------------------------------------


def hooks(config: Config) -> Check:
    label = t("钩子")
    store = HookConfig(config.hooks_file)
    edit = t("看看是哪里写错了：{run}", run=fixes.harness("hooks", "list"))
    if store.problem:
        return Check("hooks", label, PROBLEM, store.problem, [edit], {"count": 0})
    found = store.hooks()
    enabled = [(name, entry) for name, (entry, _) in found.items() if entry is not None and entry["enabled"]]
    facts = {"count": len(found), "enabled": len(enabled)}
    if not found:
        return Check("hooks", label, UNUSED, t("没有"), facts=facts)
    problems = _broken(found)
    fix = [edit] if problems else []
    missing = [(name, entry["command"]) for name, entry in enabled if not program_found(argv_of(entry["command"], entry["args"], config.workspace)[0], config.workspace)]
    for name, command in missing:
        problems.append(t("{name} 要运行的 {command} 找不到", name=name, command=command))
        fix.append(t("装上 {command}，或者写出它的完整路径（{find} 查得到在哪儿）", command=command, find=fixes.find_program(command)))
    facts["missing"] = [name for name, _ in missing]
    count = t("{n} 个，开着 {on} 个", n=len(found), on=len(enabled))
    if problems:
        return Check("hooks", label, PROBLEM, t("{count}。{problems}", count=count, problems=t("；").join(problems)), fix, facts)
    return Check("hooks", label, OK, count, facts=facts)


# ---- 定时任务 -----------------------------------------------------------------------


def task_list(config: Config) -> tuple[list[dict], str]:
    """全部任务和读文件时的问题。文件坏了的时候不交给 TaskStore（它会把文件改名留着），只说一声。"""
    path = config.tasks_file
    if not path.is_file():
        return [], ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        return [], t("任务文件 {path} 读不出来（{error}）", path=path, error=type(exc).__name__)
    if not isinstance(data, dict):
        return [], t("任务文件 {path} 读不出来（{error}）", path=path, error="tasks")
    tasks, _ = TaskStore(path).snapshot()
    return tasks, ""


def tasks(config: Config, found: list[dict], problem: str, running: bool) -> Check:
    label = t("定时任务")
    if problem:
        return Check("tasks", label, PROBLEM, problem, [t("智狐下次启动时会把它改名留着、从空的任务列表开始；想救回里面的任务，先修好它。")], {"count": 0})
    enabled = [task for task in found if task["enabled"]]
    facts = {"count": len(found), "enabled": len(enabled), "agent": sum(1 for task in found if task["kind"] == "agent")}
    if not found:
        return Check("tasks", label, UNUSED, t("没有"), facts=facts)
    count = t("{n} 个，开着 {on} 个", n=len(found), on=len(enabled))
    if enabled and not running:
        count = t("{count}。只在网页界面、harness serve 或者后台服务开着时运行，现在都没开着", count=count)
    return Check("tasks", label, OK, count, facts=facts)


def extensions(config: Config, store: ChannelStore, found: list[dict], problem: str, running: bool) -> Section:
    return Section("extensions", t("扩展"), [mcp(config), channels(config, store, running), hooks(config), tasks(config, found, problem, running)])
