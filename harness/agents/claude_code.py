"""从 Claude Code 导入：把你在 Claude Code 里已经在用的 subagent 复制一份过来。

Claude Code 的 subagent 放在两个地方，一个 Agent 一个 Markdown 文件，格式和智狐的一样（见 spec.py）：

  ~/.claude/agents/<名字>.md            你自己的，所有项目都能用
  <工作目录>/.claude/agents/<名字>.md    这个项目的

和 Skill 的导入一样（见 skills/claude_code.py）：智狐不会自己去读这两个地方，只有你在页面上点了"从 Claude Code 导入"，
才去看一眼有哪些；你勾选了的才复制进智狐自己的 Agent 文件夹。复制而不是直接用，是因为两边各改各的不会互相影响。
真正导入时按你勾选的"哪个地方的哪个文件"重新读一遍，页面送来的只有这个编号和名字，不是文件的内容。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from ..config import MAX_AGENT_BYTES
from ..i18n import Message, t
from ..skills.frontmatter import FrontmatterError, parse, replace_fields, scalar
from ..skills.store import described
from . import tools as agent_tools
from .spec import SUFFIX, check, name_problem, normal
from .store import AgentError, AgentStore

MAX_FOUND = 200


def places(workspace: Path) -> list[tuple[str, Path]]:
    """去哪里找：[(给人看的写法, 实际的文件夹)]。"""
    home = Path.home()
    found = [("~/.claude/agents", home / ".claude" / "agents")]
    project = workspace / ".claude" / "agents"
    if project.resolve() != found[0][1].resolve():  # 在家目录里运行时两处是同一个
        found.append((str(Path(".claude") / "agents"), project))
    return found


def _files(workspace: Path) -> list[tuple[str, str, Path]]:
    """[(编号, 来自哪里, 文件)]。编号是"来自哪里/文件名"，导入时页面送回来的就是它。"""
    result = []
    for label, folder in places(workspace):
        try:
            with os.scandir(folder) as listing:
                entries = sorted(listing, key=lambda entry: entry.name)
        except OSError:
            continue  # 没有这个文件夹
        for entry in entries:
            if len(result) >= MAX_FOUND:
                return result
            if entry.name.startswith(".") or not entry.name.endswith(SUFFIX):
                continue
            result.append((f"{label}/{entry.name}", label, Path(entry.path)))
    return result


def _read(path: Path) -> str:
    """读一个文件。Claude Code 允许它是指向别处的链接；导入是你明确要做的事，所以跟着链接走，但只认普通的文件。"""
    try:
        real = path.resolve(strict=True)
        info = os.stat(real)
    except OSError:
        raise AgentError(t("这个 Agent 不存在，可能已经被删除了。")) from None
    if not stat.S_ISREG(info.st_mode):
        raise AgentError(t("这个 Agent 的文件不是一个普通的文件（可能是指向别处的链接）。"))
    if info.st_size > MAX_AGENT_BYTES:
        raise AgentError(t("这个 Agent 的文件太长了：最多 {n} KB。", n=MAX_AGENT_BYTES // 1000))
    try:
        return real.read_text(encoding="utf-8").removeprefix("﻿")
    except (OSError, UnicodeDecodeError):
        raise AgentError(t("这个 Agent 的文件不是 UTF-8 编码的文字。")) from None


def discover(workspace: Path, store: AgentStore) -> list[dict]:
    """找出 Claude Code 的全部 Agent，给页面勾选：每一项的编号、来自哪里、名字、说明、问题、有没有同名的。"""
    found = []
    for key, label, path in _files(workspace):
        stem = normal(path.name[: -len(SUFFIX)])
        try:
            text = _read(path)
        except AgentError as exc:
            found.append({"key": key, "place": label, "file": path.name, "name": stem, "description": "", "problems": [described(exc.args[0])], "warnings": [], "exists": store.exists(stem)})
            continue
        spec, problems = check(text, None, file=path.name)
        name = spec.name or stem
        picked = agent_tools.form(spec.tools)
        found.append(
            {
                "key": key,
                "place": label,
                "file": path.name,
                "name": name,
                "description": spec.description,
                "model": spec.model,
                "color": spec.color,
                "tools": {"all": picked["all"], "count": len(picked["picked"])},
                # name 不合格的，可以在导入时改一个名字，所以那一条不算"装不了"；别的问题导入了也用不了，先修好再来
                "problems": [described(problem) for problem in problems if not _about_name(problem)],
                "bad_name": any(_about_name(problem) for problem in problems),
                "warnings": [described(warning) for warning in spec.warnings],
                "extra": spec.extra,
                "exists": bool(name) and name_problem(name) is None and store.exists(name),
            }
        )
    return found


def import_picked(workspace: Path, store: AgentStore, picks: object) -> list[dict]:
    """导入勾选的那几个。picks 是 [{"key": 编号, "name": 用什么名字, "replace": 有同名的就替换}]。

    返回每一个的结果：{"key", "ok", "name"} 或 {"key", "ok": False, "error"}。一个失败不影响别的。
    """
    if not isinstance(picks, list):
        raise AgentError(t("参数不正确"))
    files = {key: path for key, _, path in _files(workspace)}
    results = []
    for pick in picks:
        if not isinstance(pick, dict) or not isinstance(pick.get("key"), str) or not isinstance(pick.get("name"), str):
            raise AgentError(t("参数不正确"))
        key, name, replace = pick["key"], normal(pick["name"].strip()), pick.get("replace") is True
        try:
            if key not in files:
                raise AgentError(t("没有找到“{name}”。", name=key))
            problem = name_problem(name)
            if problem:
                raise AgentError(problem)
            text = _read(files[key])
            results.append({"key": key, "ok": True, "name": store.install(renamed(text, name), replace=replace)})
        except AgentError as exc:
            results.append({"key": key, "ok": False, "error": str(exc), "problems": [described(problem) for problem in getattr(exc, "problems", [])]})
    return results


def renamed(text: str, name: str) -> str:
    """把文件里的 name 换成 name（没写的话加上），别的一个字都不动。"""
    try:
        front = parse(text)
    except FrontmatterError:
        return text  # 读不出来：原样交给 install，它会说出哪一行不对
    if front.fields.get("name") == name:
        return text
    return replace_fields(text, {"name": f"name: {scalar(name)}"})


def _about_name(problem: Message) -> bool:
    """这条问题是不是只关于 name（缺了、不合格）：导入时换个名字就好了。"""
    return isinstance(problem, Message) and (problem.key.startswith("name ") or problem.values.get("key") == "name")
