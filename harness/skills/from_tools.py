"""从别的工具导入：把你在 Claude Code、Codex、Cursor、Gemini CLI……里已经在用的 Skill 复制一份过来。

Skill 的格式是开放的标准（https://agentskills.io），各家用的是同一种文件夹（里面一个 SKILL.md），
只是放的地方不一样：~/.claude/skills、~/.codex/skills、.github/skills，还有十几个工具共用的 ~/.agents/skills……
全部位置登记在 imports/registry.py（每一处都注明了出自哪家的文档）。

智狐不会自己去读这些地方：只有你在页面上点了"从其他工具导入"，才去看一眼有哪些，
你勾选了的才复制进智狐自己的 Skill 文件夹。复制而不是直接用，是因为两边各改各的不会互相影响，
那边删了、改了，也不会让智狐这边的 Skill 突然变样。

复制的时候和上传一样当作不可信的（skills/bundle.py）：文件夹里的链接、隐藏文件不要，路径、大小、个数都有上限。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .. import pathnames
from ..i18n import t
from ..imports.places import Env
from ..imports.registry import SKILLS, places
from .bundle import MAX_TOTAL_BYTES, Candidate, Files, _add, clean_path, examine
from .store import (
    MAX_DEPTH,
    MAX_SKILL_BYTES,
    SKILL_FILE,
    SkillError,
    read_skill_file,
    walk,
)

MAX_FOUND = 300


def _folders(env: Env) -> list[tuple[str, str, str, Path]]:
    """去哪里找：[(工具的编号, 工具的名字, 给人看的位置, 实际的文件夹)]。"""
    return [(tool.id, tool.name, env.shown(place.path), place.path) for tool, place in places(env, SKILLS)]


def _entries(folder: Path) -> list[os.DirEntry]:
    try:
        with os.scandir(folder) as listing:
            return sorted((entry for entry in listing if not entry.name.startswith(".")), key=lambda entry: entry.name)
    except OSError:
        return []  # 没有这个文件夹


def _real_folder(entry: os.DirEntry) -> Path | None:
    """一个 Skill 的文件夹。这些工具都允许它是一个链接（指向别处的真实文件夹）：导入是你明确要做的事，
    所以这一层跟着链接走；文件夹里面的链接仍然不跟（见 read_folder）。"""
    path = Path(entry.path)
    try:
        return path.resolve(strict=True) if path.is_dir() else None
    except OSError:
        return None


def discover(workspace: Path, env: Env | None = None) -> list[tuple[str, Candidate]]:
    """找出别的工具里的全部 Skill：[(分组的标题, 候选)]。还没有装进来，文件到导入时才读。

    分组的标题是"工具 · 位置"（Claude Code · ~/.claude/skills），候选的 key 是"位置/文件夹名"，导入时页面送回来的就是它。
    """
    env = env or Env.here(workspace)
    result: list[tuple[str, Candidate]] = []
    for tool_id, tool, shown, folder in _folders(env):
        for entry in _entries(folder):
            if len(result) >= MAX_FOUND:
                return result
            real = _real_folder(entry)
            if real is None:
                continue
            key = f"{shown}/{entry.name}"
            group = f"{tool} · {shown}"
            try:
                text, _ = read_skill_file(real)
            except SkillError as exc:
                result.append((group, Candidate(key=key, name=entry.name, original="", problems=[exc.args[0]], tool=tool, tool_id=tool_id)))
                continue
            name, original, meta, problems = examine(text, entry.name)
            candidate = Candidate(key=key, name=name, original=original, meta=meta, problems=problems, listing=walk(real), load=lambda real=real: read_folder(real), tool=tool, tool_id=tool_id)
            result.append((group, candidate))
    return result


def count(env: Env) -> dict[str, tuple[int, int]]:
    """每个工具里有几个 Skill（只数文件夹，不读内容），和 discover 列出来的一样多。给导入页顶上的总览用。"""
    out: dict[str, tuple[int, int]] = {}
    for tool_id, _, _, folder in _folders(env):
        found = sum(_real_folder(entry) is not None for entry in _entries(folder))
        before = out.get(tool_id, (0, 0))
        out[tool_id] = (before[0] + found, before[1])
    return out


def places_looked(workspace: Path, env: Env | None = None) -> list[str]:
    """去过哪些地方（页面上"在这些地方找过"）。"""
    env = env or Env.here(workspace)
    return [shown for _, _, shown, _ in _folders(env)]


def read_folder(folder: Path) -> Files:
    """把本机的一个 Skill 文件夹整个读进来（准备复制）。里面的链接、隐藏文件不要，总量有上限。"""
    files: Files = {}
    seen: set[str] = set()

    def visit(path: Path, prefix: str, depth: int) -> None:
        try:
            with os.scandir(path) as listing:
                entries = sorted(listing, key=lambda entry: entry.name)
        except OSError as exc:
            raise SkillError(t("没能读出 {path}：{error}", path=str(path), error=exc.strerror or exc)) from exc
        for entry in entries:
            if entry.name.startswith(".") or pathnames.entry_linked(entry):  # 包括 Windows 的目录联接
                continue
            relative = prefix + entry.name
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                if depth < MAX_DEPTH:
                    visit(Path(entry.path), relative + "/", depth + 1)
            elif stat.S_ISREG(info.st_mode):
                clean_path(relative)
                # 先看大小再读：一个几 GB 的文件不该先整个读进内存，才发现超了
                if info.st_size + sum(len(item[0]) for item in files.values()) > MAX_TOTAL_BYTES:
                    raise SkillError(t("内容太大了：解开之后最多 {n} MB。", n=MAX_TOTAL_BYTES // 1_000_000))
                descriptor = pathnames.open_nofollow(entry.path, os.O_RDONLY)  # Windows 没有 O_NOFOLLOW，见 pathnames
                with os.fdopen(descriptor, "rb") as handle:
                    data = handle.read(MAX_TOTAL_BYTES + 1)
                _add(files, seen, relative, data, bool(info.st_mode & 0o111))

    visit(folder, "", 1)
    if SKILL_FILE not in files or len(files[SKILL_FILE][0]) > MAX_SKILL_BYTES:
        raise SkillError(t("文件夹里没有 SKILL.md。"))
    return files
