"""从 Claude Code 导入：把你在 Claude Code 里已经在用的 Skill 复制一份过来。

Claude Code 的 Skill 放在两个地方：

  ~/.claude/skills/<名字>/SKILL.md           你自己的，所有项目都能用
  <工作目录>/.claude/skills/<名字>/SKILL.md   这个项目的

智狐不会自己去读这两个地方：只有你在页面上点了"从 Claude Code 导入"，才去看一眼有哪些，
你勾选了的才复制进智狐自己的 Skill 文件夹。复制而不是直接用，是因为两边各改各的不会互相影响，
Claude Code 那边删了、改了，也不会让智狐这边的 Skill 突然变样。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from ..i18n import t
from .bundle import MAX_TOTAL_BYTES, Candidate, Files, _add, clean_path, examine
from .store import (
    MAX_DEPTH,
    MAX_SKILL_BYTES,
    SKILL_FILE,
    SkillError,
    read_skill_file,
    walk,
)

MAX_FOUND = 200


def places(workspace: Path) -> list[tuple[str, Path]]:
    """去哪里找：[(给人看的写法, 实际的文件夹)]。"""
    home = Path.home()
    found = [("~/.claude/skills", home / ".claude" / "skills")]
    project = workspace / ".claude" / "skills"
    if project.resolve() != found[0][1].resolve():  # 在家目录里运行时两处是同一个
        found.append((str(Path(".claude") / "skills"), project))
    return found


def discover(workspace: Path) -> list[tuple[str, Candidate]]:
    """找出 Claude Code 的全部 Skill：[(来自哪里, 候选)]。还没有装进来，文件到导入时才读。"""
    result: list[tuple[str, Candidate]] = []
    for label, folder in places(workspace):
        try:
            with os.scandir(folder) as listing:
                entries = sorted(listing, key=lambda entry: entry.name)
        except OSError:
            continue  # 没有这个文件夹
        for entry in entries:
            if len(result) >= MAX_FOUND:
                return result
            if entry.name.startswith("."):
                continue
            # Claude Code 允许 Skill 的文件夹是一个链接（指向别处的真实文件夹）。导入是你明确要做的事，
            # 所以这一层跟着链接走；文件夹里面的链接仍然不跟（见 read_folder）
            path = Path(entry.path)
            try:
                if not path.is_dir():
                    continue
                real = path.resolve(strict=True)
            except OSError:
                continue
            key = f"{label}/{entry.name}"
            try:
                text, _ = read_skill_file(real)
            except SkillError as exc:
                result.append((label, Candidate(key=key, name=entry.name, original="", problems=[exc.args[0]])))
                continue
            name, original, meta, problems = examine(text, entry.name)
            listing = walk(real)
            result.append((label, Candidate(key=key, name=name, original=original, meta=meta, problems=problems, listing=listing, load=lambda real=real: read_folder(real))))
    return result


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
            if entry.name.startswith(".") or entry.is_symlink():
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
                descriptor = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as handle:
                    data = handle.read(MAX_TOTAL_BYTES + 1)
                _add(files, seen, relative, data, bool(info.st_mode & 0o111))

    visit(folder, "", 1)
    if SKILL_FILE not in files or len(files[SKILL_FILE][0]) > MAX_SKILL_BYTES:
        raise SkillError(t("文件夹里没有 SKILL.md。"))
    return files
