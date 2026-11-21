"""从别的工具导入：把你在 Claude Code、GitHub Copilot、Gemini CLI、OpenCode、Codex……里写好的 Agent 复制一份过来。

各家放在哪儿登记在 imports/registry.py；写法不一样的，先由 imports/agent_formats.py 换成智狐的写法
（和 Claude Code 的 subagent 一样，见 spec.py），再用 spec.py 的 check 检查。换的时候改了什么、丢了什么，
都列在每一张卡片上（changes）。

和 Skill 的导入一样（见 skills/from_tools.py）：智狐不会自己去读这些地方，只有你在页面上点了"从其他工具导入"，
才去看一眼有哪些；你勾选了的才复制进智狐自己的 Agent 文件夹。复制而不是直接用，是因为两边各改各的不会互相影响。
真正导入时按你勾选的"哪个地方的哪个文件"重新读一遍、重新换一遍，页面送来的只有这个编号和名字，不是文件的内容。
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from ..config import MAX_AGENT_BYTES
from ..i18n import Message, t
from ..imports.agent_formats import Converted, convert, stem_of
from ..imports.places import Env
from ..imports.registry import AGENTS, Place, Tool, places
from ..skills.frontmatter import FrontmatterError, parse, replace_fields, scalar
from ..skills.store import described
from . import tools as agent_tools
from .spec import check, name_problem, normal
from .store import AgentError, AgentStore

MAX_FOUND = 300
MAX_DEPTH = 3  # 会读子文件夹的那几家（Claude Code、Copilot、OpenCode），往下最多看这么多层


@dataclass
class _File:
    key: str  # 编号："位置/相对路径"，导入时页面送回来的就是它
    tool: str
    tool_id: str
    place: str  # 给人看的位置
    relative: str  # 相对于那个文件夹的路径（OpenCode 的子文件夹就在这里面）
    path: Path
    form: str
    suffixes: tuple[str, ...]


def _files(env: Env) -> list[_File]:
    found: list[_File] = []
    for tool, place in places(env, AGENTS):
        _visit(found, tool, place, env.shown(place.path), place.path, "", 1)
    return found


def _visit(found: list[_File], tool: Tool, place: Place, shown: str, folder: Path, prefix: str, depth: int) -> None:
    """看一个文件夹：扩展名对得上的是 Agent 的文件；会读子文件夹的那几家（place.deep），再往下看。"""
    try:
        with os.scandir(folder) as listing:
            entries = sorted((entry for entry in listing if not entry.name.startswith(".")), key=lambda entry: entry.name)
    except OSError:
        return  # 没有这个文件夹
    for entry in entries:
        if len(found) >= MAX_FOUND:
            return
        relative = prefix + entry.name
        if any(entry.name.endswith(suffix) for suffix in place.suffixes):
            found.append(_File(f"{shown}/{relative}", tool.name, tool.id, shown, relative, Path(entry.path), place.form, place.suffixes))
        elif place.deep and depth < MAX_DEPTH and entry.is_dir(follow_symlinks=False):
            _visit(found, tool, place, shown, Path(entry.path), relative + "/", depth + 1)


def _read(path: Path) -> str:
    """读一个文件。这些工具都允许它是指向别处的链接；导入是你明确要做的事，所以跟着链接走，但只认普通的文件。"""
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


def _converted(file: _File) -> Converted:
    """读出来，换成智狐的写法。读不出来、换不了的，problem 里写着原因。"""
    try:
        text = _read(file.path)
    except AgentError as exc:
        return Converted("", problem=exc.args[0])
    try:
        return convert(file.form, text, file.relative, file.path, file.tool, file.suffixes)
    except (TypeError, AttributeError, ValueError, KeyError, RecursionError):  # 某一项写错了类型：只让这一个导入不了
        return Converted("", problem=t("这个文件的写法认不出来，智狐读不懂它。"))


def discover(workspace: Path, store: AgentStore, env: Env | None = None) -> list[dict]:
    """找出别的工具里的全部 Agent，给页面勾选：每一项的编号、来自哪个工具哪个位置、名字、说明、问题、改了什么、有没有同名的。"""
    env = env or Env.here(workspace)
    found = []
    for file in _files(env):
        stem = normal(stem_of(file.path.name, file.suffixes))
        base = {"key": file.key, "tool": file.tool, "tool_id": file.tool_id, "place": file.place, "file": file.relative, "group": f"{file.tool} · {file.place}"}
        converted = _converted(file)
        if converted.problem is not None:
            found.append({**base, "name": stem, "description": "", "problems": [described(converted.problem)], "warnings": [], "changes": [], "exists": store.exists(stem)})
            continue
        spec, problems = check(converted.text, None, file=file.path.name)
        name = spec.name or stem
        picked = agent_tools.form(spec.tools)
        found.append(
            {
                **base,
                "name": name,
                "description": spec.description,
                "model": spec.model,
                "color": spec.color,
                "tools": {"all": picked["all"], "count": len(picked["picked"])},
                # name 不合格的，可以在导入时改一个名字，所以那一条不算"装不了"；别的问题导入了也用不了，先修好再来
                "problems": [described(problem) for problem in problems if not _about_name(problem)],
                "bad_name": any(_about_name(problem) for problem in problems),
                "warnings": [described(warning) for warning in spec.warnings],
                "changes": [described(change) for change in converted.changes],
                "extra": spec.extra,
                "exists": bool(name) and name_problem(name) is None and store.exists(name),
            }
        )
    return found


def count(env: Env) -> dict[str, tuple[int, int]]:
    """每个工具里有几个 Agent 文件（只数文件，不读内容）。给导入页顶上的总览用。"""
    out: dict[str, tuple[int, int]] = {}
    for file in _files(env):
        before = out.get(file.tool_id, (0, 0))
        out[file.tool_id] = (before[0] + 1, before[1])
    return out


def places_looked(workspace: Path, env: Env | None = None) -> list[str]:
    """去过哪些地方（页面上"在这些地方找过"）。"""
    env = env or Env.here(workspace)
    return list(dict.fromkeys(env.shown(place.path) for _, place in places(env, AGENTS)))


def import_picked(workspace: Path, store: AgentStore, picks: object, env: Env | None = None) -> list[dict]:
    """导入勾选的那几个。picks 是 [{"key": 编号, "name": 用什么名字, "replace": 有同名的就替换}]。

    返回每一个的结果：{"key", "ok", "name"} 或 {"key", "ok": False, "error"}。一个失败不影响别的。
    """
    if not isinstance(picks, list):
        raise AgentError(t("参数不正确"))
    env = env or Env.here(workspace)
    files = {file.key: file for file in _files(env)}
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
            converted = _converted(files[key])
            if converted.problem is not None:
                raise AgentError(converted.problem)
            results.append({"key": key, "ok": True, "name": store.install(renamed(converted.text, name), replace=replace)})
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
