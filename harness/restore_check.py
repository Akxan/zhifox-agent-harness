from __future__ import annotations

import json
import re
import stat
import unicodedata
import zipfile
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from . import export, pathnames
from .config import (
    RESTORE_MAX_DEPTH,
    RESTORE_MAX_FILE_BYTES,
    RESTORE_MAX_FILES,
    RESTORE_MAX_TOTAL_BYTES,
)
from .export import MANIFEST, MASKED_FILES, NOTE, PARTS, Part, part_of
from .i18n import t

# macOS、Windows 打包时顺手塞进去的东西：跳过，不算"不认识的"
_JUNK = re.compile(r"(^|/)__MACOSX(/|$)|(^|/)(\.DS_Store|Thumbs\.db|desktop\.ini)$")
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


def size_text(size: int) -> str:
    """给人看的大小：12 KB、3.4 MB、1.2 GB（和页面的 formatSize 一样按 1000 进位）。"""
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}".replace(".0 ", " ")
    return f"{size} B"


def _reason(exc: BaseException) -> str:
    return str(getattr(exc, "strerror", None) or exc or type(exc).__name__)[:300]
