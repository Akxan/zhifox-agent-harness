from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from . import pathnames, restore_keys
from .export import MASKED_FILES, PARTS, Part
from .i18n import k, t
from .restore_check import (
    _JUNK,
    SMALL,
    Archive,
    Entry,
    RestoreError,
    _count_json,
    _items,
)
from .safe import to_json

MODES = ("overwrite", "merge")
# 恢复以后要重新填的密钥，在哪儿填（页面和终端照着说）
KEY_HOMES = {
    "mcp.json": k("扩展 → MCP"),
    "channels.json": k("扩展 → 通道"),
    "endpoints.json": k("设置 → 自定义模型接口"),
    "hooks.json": k("扩展 → 钩子"),
}
# 每一样东西会怎样，写成一句话（终端和页面都照着它说，见 step_text）
ACTIONS = {
    "replace": k("换成备份里的：{backup}（现在：{current}）"),
    "add": k("用备份里的：{backup}（现在没有）"),
    "clear": k("清空：备份里没有（现在：{current}）"),
    "keep": k("留着现在的"),
    "merge": k("加进来 {added} 个，{kept} 个现在已经有了、留着现在的"),
}
# 不数个数的那几样（设置、提示词、账本……）：说"有"太别扭，换一种说法
PLAIN = {"replace": k("换成备份里的"), "add": k("用备份里的（现在没有）"), "clear": k("清空：备份里没有")}


# ---- 现在的数据文件夹 ------------------------------------------------------------


def _target(data_dir: Path, part: Part) -> Path:
    return data_dir.joinpath(*PurePosixPath(part.path.removesuffix("/")).parts)


def _files_on_disk(folder: Path) -> list[str]:
    """一个文件夹里的所有文件（相对它的路径），不跟着链接走，跳过 .DS_Store 这类。"""
    found: list[str] = []
    for here, folders, names in os.walk(folder):
        folders[:] = [name for name in folders if not pathnames.is_link(Path(here) / name)]
        for name in names:
            relative = (Path(here) / name).relative_to(folder).as_posix()
            if not _JUNK.search(relative) and not name.endswith(".tmp"):
                found.append(relative)
    return found


def _count_on_disk(data_dir: Path, part: Part) -> int | None:
    """现在的数据文件夹里这一样有几个。没有返回 -1（和"有，但不数"的 None 分开）。"""
    target = _target(data_dir, part)
    if not os.path.lexists(target):
        return -1
    if part.folder:
        return len(_items(part, (part.path + name for name in _files_on_disk(target)))) if target.is_dir() else -1
    if not part.count:
        return None
    try:
        return _count_json(json.loads(target.read_text(encoding="utf-8")), part) if target.stat().st_size <= SMALL else None
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None


def _current_config(data_dir: Path, name: str) -> tuple[bool, Any]:
    """现在的一个存着密钥的配置文件：（有没有，解析好的内容）。有、但读不懂的，内容是 None。"""
    path = data_dir / name
    if not os.path.lexists(path):
        return False, restore_keys.MISSING
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return True, None
    return True, data if isinstance(data, dict) else None


# ---- 两种方式各会怎样 ------------------------------------------------------------


@dataclass
class Step:
    """一样东西要怎么办。action：
      replace  换成备份里的       add    现在没有，用备份里的       clear  备份里没有，清空（覆盖时）
      keep     留着现在的         merge  现在没有的加进来（合并时）
    """

    part: Part
    action: str
    backup: int | None = None  # 备份里有几个（None：有，但不数）
    current: int | None = None  # 现在有几个（-1：没有）
    entries: list[Entry] = field(default_factory=list)  # 要写的
    added: int = 0  # 合并：加进来几个
    kept: int = 0  # 合并：撞上了、留着现在的几个
    content: bytes | None = None  # 存着密钥的配置文件：填好现在的密钥以后的内容

    def summary(self) -> dict:
        part = self.part
        return {"id": part.id, "label": part.label, "unit": part.unit, "action": self.action, "backup": self.backup,
                "current": None if self.current == -1 else self.current, "added": self.added, "kept": self.kept}


@dataclass
class Plan:
    mode: str
    steps: list[Step]
    keys: list[dict]  # 存着密钥的配置文件各会怎样（见 _keys_report）
    size: int  # 要写多少字节

    def summary(self) -> dict:
        return {"steps": [step.summary() for step in self.steps if step.part.label], "keys": self.keys}


def plan(archive: Archive, data_dir: Path, mode: str) -> Plan:
    """照着现在的数据文件夹，算出这种方式要做的事。什么都不写。"""
    if mode not in MODES:
        raise RestoreError(t("参数不正确"))
    steps: list[Step] = []
    keys: list[dict] = []
    for part in PARTS:
        if not part.restore:
            continue
        entries = archive.of(part)
        step = Step(part, "", archive.counts.get(part.id), _count_on_disk(data_dir, part), entries)
        here = step.current != -1
        if part.path in MASKED_FILES:
            _plan_secrets(step, archive, data_dir, mode, keys)
        elif mode == "overwrite":
            step.action = "replace" if entries and here else "add" if entries else "clear" if here else ""
        elif entries and part.folder:
            _plan_merge(step, data_dir)
        elif entries:
            step.action, step.entries = ("keep", []) if here else ("add", entries)
        if step.action:
            steps.append(step)
    size = sum(len(step.content) if step.content is not None else sum(entry.size for entry in step.entries) for step in steps if step.action != "keep")
    return Plan(mode, steps, keys, size)


def _plan_merge(step: Step, data_dir: Path) -> None:
    """合并一个文件夹：现在没有的加进来。Skill 按整个文件夹比，别的按一个个文件比；"有没有"问磁盘
    （macOS、Windows 上 Notes.md 和 notes.md 是同一个文件，磁盘说了算）。"""
    part = step.part
    adding: list[Entry] = []
    if part.by == "folder":
        groups: dict[str, list[Entry]] = {}
        for entry in step.entries:
            groups.setdefault(entry.name[len(part.path) :].split("/", 1)[0], []).append(entry)
        for name, members in groups.items():
            if os.path.lexists(data_dir / part.path / name):
                step.kept += 1
            else:
                step.added += 1
                adding += members
    else:
        for entry in step.entries:
            if os.path.lexists(data_dir.joinpath(*entry.name.split("/"))):
                step.kept += 1
            else:
                step.added += 1
                adding.append(entry)
    step.entries = adding
    step.action = "merge"


def _plan_secrets(step: Step, archive: Archive, data_dir: Path, mode: str, keys: list[dict]) -> None:
    """存着密钥的配置文件：占位符不盖掉现在的密钥（见 restore_keys.py）。"""
    name = step.part.path
    here, current = _current_config(data_dir, name)
    report = {"file": name, "home": KEY_HOMES.get(name, ""), "state": "", "kept": 0, "missing": [], "dropped": []}
    if name not in archive.masked:
        step.action = "keep" if here else ""  # 备份里没有：留着现在的，不因为恢复丢掉密钥
        return
    if here and current is None:
        step.action, report["state"] = "keep", "broken"  # 现在的读不懂：不动它，覆盖了就再也找不回里面的密钥
    elif here and mode == "merge":
        step.action = "keep"
        return
    else:
        filled, found = restore_keys.fill(archive.masked[name], current)
        step.action = "replace" if here else "add"
        step.content = (to_json(filled, readable=True, indent=2) + "\n").encode()
        report.update(state="filled" if here else "new", kept=len(found.kept),
                      missing=[restore_keys.label(path) for path in found.missing], dropped=[restore_keys.label(path) for path in found.dropped])
    if report["state"] == "broken" or report["missing"] or report["dropped"] or report["kept"]:
        keys.append(report)


def count_text(unit: str, count: int | None) -> str:
    """几个（"12 段会话"）；不数的写"有"。"""
    return t(unit, n=count) if unit and count is not None else t("有")


def step_text(step: dict) -> str:
    """一样东西会怎样，写成一句话（step 是 Step.summary 的样子）。返回的是 t() 译好的话，带着原文：页面换语言时照着它重译。"""
    if not step["unit"] and step["action"] in PLAIN:
        return t(PLAIN[step["action"]])
    return t(
        ACTIONS[step["action"]],
        backup=count_text(step["unit"], step["backup"]),
        current=count_text(step["unit"], step["current"]),
        added=step["added"],
        kept=step["kept"],
    )


def preview(archive: Archive, data_dir: Path) -> dict:
    """给页面和终端看的：导出的时间、里面有什么、两种方式各会怎样、哪些密钥要重新填。什么都不写。"""
    return {
        "exported_at": archive.exported_at.astimezone().isoformat(timespec="seconds") if archive.exported_at else None,
        "foreign": archive.foreign,
        "files": len(archive.entries),
        "bytes": archive.total,
        "parts": [
            {"id": part.id, "label": part.label, "unit": part.unit, "count": archive.counts[part.id], "restore": part.restore}
            for part in PARTS
            if part.id in archive.counts and part.label
        ],
        "modes": {mode: plan(archive, data_dir, mode).summary() for mode in MODES},
        "left_out": archive.left_out,
    }
