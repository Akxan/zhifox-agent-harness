"""从备份恢复：把导出的压缩包（见 export.py）放回数据文件夹。

页面上的"从备份恢复…"（webui/restore_api.py）和终端的 harness restore（restore_cli.py）都走这里，分三步：

  check(压缩包)                  看一遍压缩包能不能用。有一项不对就整个拒绝（RestoreError），什么都不写
  preview(压缩包, 数据文件夹)     给你看：导出的时间、里面有什么、两种方式各会怎样、哪些密钥要重新填。也什么都不写
  apply(...)                     真的恢复：先把现在的数据导出一份（恢复前的自动备份），再写

压缩包是不可信的输入，可能是故意做坏的。check 把每一项都过一遍：
  路径      ../、绝对路径、C: 这样的盘符、反斜杠、Windows 上会变成别的东西的名字（a:b、CON、PROGRA~1……，见 pathnames.archive_trick）
  种类      符号链接、设备文件、管道、加密的项、不认识的压缩方法
  大小      压缩炸弹：一共多大、一个多大、多少项（config.RESTORE_*）。压缩包里写的大小可能是假的，解的时候按实际读到的再核对一遍
  名字      只认 export.PARTS 里列的那些（白名单，和导出写在一起）。.env、browser/profile、.web-token、.lock 这类一律不收
  撞名      只差大小写或 Unicode 写法的两项（在 macOS、Windows 上是同一个文件），一个名字既是文件又是文件夹
README-backup.txt 和 backup-manifest.json 只读不写。macOS、Windows 打包时顺手塞进去的 .DS_Store、__MACOSX/、Thumbs.db 跳过。

两种方式：
  overwrite（覆盖）  数据文件夹变成备份时的样子：PARTS 里的每一样都换成备份里的，备份里没有的那几样清空。
                     例外是存着密钥的配置文件（export.MASKED_FILES）：备份里没有就留着现在的，绝不因为恢复丢掉你现在的密钥
  merge（合并）      只往里加：会话按编号、记忆和 Agent 按文件名、Skill 按文件夹、多媒体按内容，现在没有的加进来，撞上了留着现在的。
                     整份的文件（设置、待办、定时任务、账本、MCP……）现在有就留着现在的，没有才用备份里的：两份设置一项一项拼起来，
                     会拼出一份谁都没用过的设置（钩子指着另一台电脑上的脚本、聊天通道配对的是另一个人……）

密钥（见 restore_keys.py）：存着密钥的配置文件在备份里是遮过的。占位符绝不盖掉现在的真密钥：同一处现在有，就留着现在的；
现在没有的照样写进去，告诉你去哪儿重新填。现在的文件读不懂的，不动它（覆盖了就再也找不回里面的密钥）。

怎么写（overwrite）：先在数据文件夹里的一个临时文件夹（.restore-xxxx/，导出不带它）里把新的内容摆好，再一样一样换上去，
换下来的旧东西先放在旁边。中途哪一步出了错，换回去；全部换好了、调用的人确认程序照着新的数据开起来了（Restored.commit），
才删掉旧的。merge 只加文件，出错时把加进来的删掉。所以要么整个恢复成功，要么原样不动（断电这种情况除外：那时还有自动备份）。
每个文件都用 safe.write_stream 写（临时文件、fsync、再换上去，权限 600；Windows 上靠文件夹的 ACL，见 safe.private_dir）。

谁也不能在恢复的时候写数据文件夹：网页上由 restore_api.py 先停下对话、定时任务、聊天通道；终端里先占住数据目录的锁（instance.py）。
"""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import shutil
import stat
import time
import unicodedata
import zipfile
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from . import export, pathnames, restore_keys
from .config import (
    RESTORE_BACKUPS_DIR,
    RESTORE_KEEP_BACKUPS,
    RESTORE_MAX_DEPTH,
    RESTORE_MAX_FILE_BYTES,
    RESTORE_MAX_FILES,
    RESTORE_MAX_TOTAL_BYTES,
)
from .export import MANIFEST, MASKED_FILES, NOTE, PARTS, STAGING_PREFIX, Part, part_of
from .i18n import k, t
from .safe import private_dir, to_json, write_stream

MODES = ("overwrite", "merge")
# macOS、Windows 打包时顺手塞进去的东西：跳过，不算"不认识的"
_JUNK = re.compile(r"(^|/)__MACOSX(/|$)|(^|/)(\.DS_Store|Thumbs\.db|desktop\.ini)$")
# 恢复前的自动备份（在 data/restore-backups/ 里）的文件名。页面上"在访达中显示"只认这个样子、而且真的在那儿的文件
BEFORE = re.compile(r"zhifox-before-restore-\d{8}-\d{6}(-\d{1,3})?\.zip")
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
COMPRESSIONS = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA}
SMALL = 20_000_000  # 要整个读进来看一眼的文件（存着密钥的配置、数个数的 JSON）最大这么大
_ZIP_ERRORS = (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, NotImplementedError, RuntimeError, ValueError, zlib.error)


class RestoreError(Exception):
    """压缩包不能用，或者现在恢复不了。str(exc) 是给人看的原因（已经按界面语言译好）。"""


# ---- 看一遍压缩包 ------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """压缩包里要恢复的一个文件。name 是相对数据文件夹的路径（分隔符是 /）。"""

    name: str
    part: Part
    info: zipfile.ZipInfo

    @property
    def size(self) -> int:
        return self.info.file_size


@dataclass
class Archive:
    """看过的一个压缩包：要恢复的文件，和导出时写的说明。"""

    path: Path
    entries: list[Entry]
    exported_at: datetime | None  # 什么时候导出的（manifest 里写的；旧的备份没有 manifest，用 README 那一项的时间）
    foreign: bool  # 没有导出时写的说明：可能不是智狐导出的（自己打的包也能恢复，只要里面都是认识的东西）
    masked: dict[str, Any]  # 存着密钥的配置文件：名字 → 解析好的内容（遮过的）
    counts: dict[str, int | None]  # 每一样东西有几个（None：有，但不数）
    left_out: list[str] = field(default_factory=list)  # 导出时没有带上的（manifest 里写的）
    _by: dict[str, list[Entry]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        for entry in self.entries:
            self._by.setdefault(entry.part.id, []).append(entry)

    @property
    def total(self) -> int:
        return sum(entry.size for entry in self.entries)

    def of(self, part: Part) -> list[Entry]:
        return self._by.get(part.id, [])


def check(path: Path) -> Archive:
    """看一遍压缩包（见文件开头）。不能用的话抛出 RestoreError，原因写在里面。"""
    try:
        with zipfile.ZipFile(path) as archive:
            return _check(path, archive)
    except RestoreError:
        raise
    except (*_ZIP_ERRORS, OSError) as exc:
        raise RestoreError(t("这不是一个能打开的 zip 压缩包（{error}）。", error=_reason(exc))) from None


def _check(path: Path, archive: zipfile.ZipFile) -> Archive:
    infos = archive.infolist()
    if len(infos) > RESTORE_MAX_FILES:
        raise RestoreError(t("压缩包里的东西太多了：最多 {n} 项。", n=f"{RESTORE_MAX_FILES:,}"))
    entries: list[Entry] = []
    seen: set[str] = set()
    manifest_raw: bytes | None = None
    note: zipfile.ZipInfo | None = None
    total = 0
    for info in infos:
        name = info.filename
        if _JUNK.search(name):
            continue
        kind = stat.S_IFMT(info.external_attr >> 16)
        folder = info.is_dir() or kind == stat.S_IFDIR
        if pathnames.archive_trick(name.removesuffix("/") if folder else name, RESTORE_MAX_DEPTH):
            raise RestoreError(t("压缩包里有不安全的路径：{path}。为了安全，什么都没有恢复。", path=name[:200]))
        if kind == stat.S_IFLNK:
            raise RestoreError(t("压缩包里有符号链接（{path}）。链接可能指向你电脑上别的地方，为了安全，什么都没有恢复。", path=name[:200]))
        if not folder and kind not in (0, stat.S_IFREG):  # 0：在 Windows 上打的包，不带文件类型，按普通文件算
            raise RestoreError(t("压缩包里有不是普通文件的东西（{path}），比如设备文件或管道。为了安全，什么都没有恢复。", path=name[:200]))
        if info.flag_bits & 0x1:
            raise RestoreError(t("压缩包是加密的，没法打开。"))
        if folder:
            if not _known_folder(name.removesuffix("/")):
                raise _unknown(name)
            continue
        if info.compress_type not in COMPRESSIONS:
            raise RestoreError(t("压缩包里的 {path} 用的压缩方法认不出来，没法解开。", path=name[:200]))
        if name in (NOTE, MANIFEST):  # 导出时写的说明：读，不写进数据文件夹
            if name == MANIFEST and info.file_size <= SMALL:
                manifest_raw = _read(archive, info)
            note = info if name == NOTE else note
            continue
        part = part_of(name)
        # 存着密钥的文件（.env、.env.local、xxx.env）在哪一层都不收：导出从来不带它们（见 export.plan），备份里有就是被改动过
        if part is None or export.is_secret_file(name.rsplit("/", 1)[-1]):
            raise _unknown(name)
        total += info.file_size
        if info.file_size > RESTORE_MAX_FILE_BYTES or total > RESTORE_MAX_TOTAL_BYTES:
            raise RestoreError(t("备份解开以后太大了：一个文件最多 {each}，一共最多 {total}。", each=size_text(RESTORE_MAX_FILE_BYTES), total=size_text(RESTORE_MAX_TOTAL_BYTES)))
        key = _key(name)
        if key in seen:
            raise RestoreError(t("压缩包里有两个只差大小写（或写法）的同名文件：{path}。在 macOS、Windows 上它们会互相覆盖，所以什么都没有恢复。", path=name[:200]))
        seen.add(key)
        entries.append(Entry(name, part, info))
    for entry in entries:  # 一个名字既是文件、又是别的文件所在的文件夹：解不开
        if any(_key(str(parent)) in seen for parent in PurePosixPath(entry.name).parents if str(parent) != "."):
            raise RestoreError(t("压缩包里有两个只差大小写（或写法）的同名文件：{path}。在 macOS、Windows 上它们会互相覆盖，所以什么都没有恢复。", path=entry.name[:200]))
    if not entries:
        raise RestoreError(t("压缩包里没有能恢复的东西。它是智狐导出的备份吗？"))

    manifest = export.read_manifest(manifest_raw) if manifest_raw is not None else None
    if manifest is not None and manifest["format"] > export.MANIFEST_FORMAT:
        raise RestoreError(t("这个备份是更新版本的智狐导出的（格式 {format}），这个版本还不认识。先把智狐升级到最新再恢复。", format=manifest["format"]))
    exported = manifest["exported_at"] if manifest else None
    if exported is None and note is not None:
        try:
            exported = datetime(*note.date_time)  # 旧的备份没有 manifest：README 那一项的时间就是导出的时刻（见 export.write_archive）
        except ValueError:
            exported = None
    masked: dict[str, Any] = {}
    for entry in entries:
        if entry.name in MASKED_FILES:
            data = _json(archive, entry)
            if not isinstance(data, dict):
                raise RestoreError(t("备份里的 {name} 读不懂：它应该是导出时遮过密钥的配置文件。为了安全，什么都没有恢复。", name=entry.name))
            masked[entry.name] = data
    found = Archive(path, entries, exported, manifest is None and note is None, masked, {}, manifest["left_out"] if manifest else [])
    found.counts = {part.id: _count_packed(archive, found.of(part), part) for part in PARTS if found.of(part)}
    return found


def _known_folder(name: str) -> bool:
    """压缩包里单独的一项"文件夹"：在某一样东西里面（conversations/2026），或者是它的上一层（browser/ 之于 browser/shots/）。"""
    return part_of(name + "/x") is not None or any(part.folder and part.path.startswith(name + "/") for part in PARTS)


def _unknown(name: str) -> RestoreError:
    last = name.removesuffix("/").rsplit("/", 1)[-1]
    if export.is_secret_file(last):
        return RestoreError(t("备份里有存着密钥的文件（{path}）：它不该在备份里，恢复也不会写它。为了安全，什么都没有恢复。", path=name[:200]))
    if (name.removesuffix("/") + "/").startswith("browser/profile/"):
        return RestoreError(t("备份里有浏览器的配置文件夹（{path}）：里面是登录过的网站的 cookie，不会从备份恢复。为了安全，什么都没有恢复。", path=name[:200]))
    return RestoreError(t("备份里有不认识的东西：{path}。可能是更新版本的智狐导出的，或者被改动过。为了安全，什么都没有恢复。", path=name[:200]))


def _key(name: str) -> str:
    """比较用的名字：macOS、Windows 上不分大小写，macOS 不分 Unicode 的两种写法（见 pathnames.key）。"""
    return unicodedata.normalize("NFC", name).casefold()


def _read(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int = SMALL) -> bytes:
    """把压缩包里的一项整个读出来（只给小文件用）。写的大小是假的、内容坏了，都算损坏。"""
    if info.file_size > limit:
        raise RestoreError(t("备份里的 {name} 太大了，不像是智狐写的。为了安全，什么都没有恢复。", name=info.filename[:200]))
    try:
        with archive.open(info) as handle:
            content = handle.read(info.file_size + 1)
    except (*_ZIP_ERRORS, OSError):
        raise RestoreError(t("压缩包已经损坏，读不出 {path}。", path=info.filename[:200])) from None
    if len(content) != info.file_size:
        raise RestoreError(t("压缩包已经损坏，读不出 {path}。", path=info.filename[:200]))
    return content


def _json(archive: zipfile.ZipFile, entry: Entry) -> Any:
    try:
        return json.loads(_read(archive, entry.info).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None


def _count_packed(archive: zipfile.ZipFile, entries: list[Entry], part: Part) -> int | None:
    """备份里这一样有几个。"""
    if part.folder:
        return len(_items(part, [entry.name for entry in entries]))
    if not part.count:
        return None
    try:
        return _count_json(_json(archive, entries[0]), part)
    except RestoreError:
        return None


def _items(part: Part, names: Iterable[str]) -> set[str]:
    """一个文件夹里的"几个"：by 是 folder 的数子文件夹（一个 Skill 一个），别的数文件。"""
    inside = (name[len(part.path) :] for name in names)
    return {name.split("/", 1)[0] if part.by == "folder" else name for name in inside}


def _count_json(data: Any, part: Part) -> int | None:
    value = data.get(part.count) if isinstance(data, dict) else None
    return len(value) if isinstance(value, (list, dict)) else None


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


# ---- 恢复 ----------------------------------------------------------------------


@dataclass
class Restored:
    """恢复好了、还没收尾：换下来的旧东西还放在旁边（staging 里）。

    commit：调用的人确认程序照着新的数据开起来了，删掉旧的。rollback：开不起来，换回原来的样子（恢复前的自动备份留着）。"""

    mode: str
    backup: Path | None  # 恢复前的自动备份（数据文件夹是空的就没有）
    keys: list[dict]
    steps: list[Step]
    staging: Path
    swapped: list[list] = field(default_factory=list)  # 覆盖：[原来的位置, 旧的挪到了哪儿（没有是 None）, 新的放上去了没有]
    created: list[Path] = field(default_factory=list)  # 合并：加进来的文件和文件夹

    def commit(self) -> None:
        shutil.rmtree(self.staging, ignore_errors=True)  # rmtree 不跟着链接走：里面的链接只删掉链接本身

    def rollback(self) -> None:
        _undo(self)
        shutil.rmtree(self.staging, ignore_errors=True)

    def summary(self) -> dict:
        return {
            "mode": self.mode,
            "backup": str(self.backup) if self.backup else None,
            "backup_name": self.backup.name if self.backup else None,
            "steps": [step.summary() for step in self.steps if step.part.label],
            "keys": self.keys,
        }


def apply(archive: Archive, data_dir: Path, mode: str, secrets_files: Iterable[Path] = (), moment: datetime | None = None) -> Restored:
    """恢复（见文件开头）。调用的人保证这时没有别人在写数据文件夹。做不成抛出 RestoreError，数据文件夹原样不动。
    secrets_files 是导出时另外要挡住的文件（.env，见 export.plan）。返回的 Restored 要 commit 或者 rollback。"""
    moment = moment or datetime.now()
    private_dir(data_dir)
    work = plan(archive, data_dir, mode)
    _check_links(data_dir, work)
    current = export.plan(data_dir, secrets_files)
    _check_room(data_dir, work.size + current.size)
    try:
        backup = backup_first(data_dir, secrets_files, moment, current.count)
    except OSError as exc:
        raise RestoreError(t("没能先把现在的数据备份一份（{error}），所以没有恢复。", error=_reason(exc))) from None
    staging = data_dir / f"{STAGING_PREFIX}{secrets.token_hex(4)}"
    done = Restored(mode, backup, work.keys, work.steps, staging)
    try:
        private_dir(staging / "new")
        private_dir(staging / "old")
        with zipfile.ZipFile(archive.path) as packed:
            (_overwrite if mode == "overwrite" else _merge)(packed, work, data_dir, done)
    except BaseException as exc:
        done.rollback()
        if isinstance(exc, RestoreError):
            raise
        if isinstance(exc, (*_ZIP_ERRORS, OSError)):
            raise RestoreError(t("没能恢复（{error}）。数据文件夹已经换回原来的样子。", error=_reason(exc))) from None
        raise
    return done


def _overwrite(packed: zipfile.ZipFile, work: Plan, data_dir: Path, done: Restored) -> None:
    new, old = done.staging / "new", done.staging / "old"
    made: set[Path] = {new}
    for step in work.steps:  # 先把新的内容摆好
        if step.action not in ("replace", "add"):
            continue
        if step.content is not None:
            target = new / step.part.path
            _folders(target.parent, made)
            write_stream(target, io.BytesIO(step.content), len(step.content))
        else:
            for entry in step.entries:
                _extract(packed, entry, new.joinpath(*entry.name.split("/")), made)
    for step in work.steps:  # 再一样一样换上去，换下来的放在旁边
        if step.action == "keep":
            continue
        relative = PurePosixPath(step.part.path.removesuffix("/"))
        target = data_dir.joinpath(*relative.parts)
        record: list = [target, None, False]
        done.swapped.append(record)
        if os.path.lexists(target):
            aside = old.joinpath(*relative.parts)
            aside.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.rename(target, aside)
            record[1] = aside
        if step.action != "clear":
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.rename(new.joinpath(*relative.parts), target)
            record[2] = True


def _merge(packed: zipfile.ZipFile, work: Plan, data_dir: Path, done: Restored) -> None:
    made: set[Path] = set()
    for step in work.steps:
        if step.action == "add" and step.content is not None:
            target = data_dir / step.part.path
            done.created.append(target)
            write_stream(target, io.BytesIO(step.content), len(step.content))
        elif step.action == "add":
            for entry in step.entries:
                target = data_dir.joinpath(*entry.name.split("/"))
                done.created.append(target)
                _extract(packed, entry, target, made)
        elif step.action == "merge" and step.part.by == "folder":
            # 一个 Skill 一个文件夹：先在旁边摆好整个文件夹，再一步换上去，不会出现半个 Skill
            groups: dict[str, list[Entry]] = {}
            for entry in step.entries:
                groups.setdefault(entry.name[len(step.part.path) :].split("/", 1)[0], []).append(entry)
            for name, members in groups.items():
                for entry in members:
                    _extract(packed, entry, done.staging.joinpath("new", *entry.name.split("/")), made)
                target = data_dir.joinpath(*PurePosixPath(step.part.path).parts, name)
                _note_new_folders(data_dir, target, done)
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.rename(done.staging.joinpath("new", *PurePosixPath(step.part.path).parts, name), target)
                done.created.append(target)
        elif step.action == "merge":
            for entry in step.entries:
                target = data_dir.joinpath(*entry.name.split("/"))
                _note_new_folders(data_dir, target, done)
                done.created.append(target)
                _extract(packed, entry, target, made)


def _note_new_folders(data_dir: Path, target: Path, done: Restored) -> None:
    """合并时要新建的文件夹（target 上面还不存在的最上面那一层）记下来：撤销时连它一起删，不留下一个空的 skills/、memories/。"""
    for folder in reversed(target.relative_to(data_dir).parents[:-1]):
        if not os.path.lexists(data_dir / folder):
            done.created.append(data_dir / folder)
            return


def _undo(done: Restored) -> None:
    """换回原来的样子。尽量做：有一步做不成也接着做别的。"""
    undone = done.staging / "undone"
    for target, aside, placed in reversed(done.swapped):
        try:
            if placed:  # 换上去的新东西挪开（之后和 staging 一起删掉），原来的才能放回去
                undone.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.rename(target, undone / secrets.token_hex(4))
            if aside is not None:
                os.rename(aside, target)
        except OSError:
            pass
    done.swapped.clear()
    for path in reversed(done.created):
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass
    done.created.clear()


def _folders(folder: Path, made: set[Path]) -> None:
    """建好一个文件为止的文件夹（700）。Windows 上它们从上一层（safe.private_dir 设过的）继承只有你能用的 ACL。"""
    if folder in made:
        return
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    made.add(folder)


def _extract(packed: zipfile.ZipFile, entry: Entry, target: Path, made: set[Path]) -> None:
    """解开一个文件，写到 target。压缩包里写的大小可能是假的：最多读那么多，读到的不一样多就算损坏。"""
    _folders(target.parent, made)
    try:
        with packed.open(entry.info) as source:
            written = write_stream(target, source, entry.size)
    except (*_ZIP_ERRORS, OSError) as exc:
        raise RestoreError(t("没能恢复 {path}（{error}）。数据文件夹已经换回原来的样子。", path=entry.name[:200], error=_reason(exc))) from None
    if written != entry.size:
        target.unlink(missing_ok=True)
        raise RestoreError(t("压缩包已经损坏，读不出 {path}。", path=entry.name[:200]))
    try:
        stamp = time.mktime((*entry.info.date_time, 0, 0, -1))  # 修改时间照着导出时的（记忆的列表按它排）
        os.utime(target, (stamp, stamp))
    except (OSError, OverflowError, ValueError):
        pass
    if os.name != "nt" and entry.part.id == "skills" and entry.info.external_attr >> 16 & 0o100:
        os.chmod(target, 0o700)  # Skill 里的脚本：导出时能执行，恢复回来也能执行（仍然只有你自己的账号能用）


def _check_links(data_dir: Path, work: Plan) -> None:
    """要写、要换掉的地方不能是链接（替身、Windows 的目录联接）：写进去就写到数据文件夹外面去了，换掉的是链接本身。
    导出也不带链接（见 export.plan），所以备份里本来就不会有它们。"""
    checked: dict[Path, bool] = {}

    def linked(path: Path) -> bool:
        if path not in checked:
            checked[path] = pathnames.is_link(path)
        return checked[path]

    for step in work.steps:
        if step.action == "keep":
            continue
        places = [_target(data_dir, step.part)] if step.action != "merge" else [data_dir.joinpath(*entry.name.split("/")) for entry in step.entries]
        for place in places:
            for path in (place, *place.relative_to(data_dir).parents):
                full = path if path.is_absolute() else data_dir / path
                if full != data_dir and linked(full):
                    raise RestoreError(t("数据文件夹里的 {path} 是一个链接（替身），指向别处。为了安全，什么都没有恢复；先把它换成一个普通的文件夹。", path=full.relative_to(data_dir).as_posix()))


def _check_room(data_dir: Path, need: int) -> None:
    try:
        free = shutil.disk_usage(data_dir).free
    except OSError:
        return
    if free < need + 50_000_000:  # 再多留一点余地：磁盘满了，别的程序也要出错
        raise RestoreError(t("磁盘空间不够：恢复（连同恢复前的自动备份）大约要 {need}，现在只剩 {free}。", need=size_text(need), free=size_text(free)))


def backup_first(data_dir: Path, secrets_files: Iterable[Path], moment: datetime, count: int | None = None) -> Path | None:
    """恢复之前，先把现在的数据导出一份，放在 data/restore-backups/ 里（和"导出全部数据"是同一种压缩包，密钥同样遮住）。
    数据文件夹是空的就不用了，返回 None。最多留 RESTORE_KEEP_BACKUPS 份，更早的删掉。"""
    if count is None:
        count = export.plan(data_dir, secrets_files).count
    if not count:
        return None
    folder = data_dir / RESTORE_BACKUPS_DIR
    private_dir(folder)
    base = f"zhifox-before-restore-{moment:%Y%m%d-%H%M%S}"
    name, number = f"{base}.zip", 1
    while os.path.lexists(folder / name):
        number += 1
        name = f"{base}-{number}.zip"
    target = folder / name
    temp = folder / f"{name}.{secrets.token_hex(4)}.tmp"
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(descriptor, "wb") as out:
            export.write_archive(data_dir, out, moment, secrets_files)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    kept = sorted((path for path in folder.iterdir() if BEFORE.fullmatch(path.name) and path.is_file()), key=lambda path: (path.stat().st_mtime, path.name))
    for stale in kept[:-RESTORE_KEEP_BACKUPS]:
        stale.unlink(missing_ok=True)
    return target


def backups_folder(data_dir: Path) -> Path:
    return data_dir / RESTORE_BACKUPS_DIR


def size_text(size: int) -> str:
    """给人看的大小：12 KB、3.4 MB、1.2 GB（和页面的 formatSize 一样按 1000 进位）。"""
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}".replace(".0 ", " ")
    return f"{size} B"


def _reason(exc: BaseException) -> str:
    return str(getattr(exc, "strerror", None) or exc or type(exc).__name__)[:300]
