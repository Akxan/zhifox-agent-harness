"""留底存在哪儿：data/backups/turns/<编号>/，一轮一个文件夹。

  manifest.json      这一轮改了哪些文件（Journal.finish 写一次，之后不再改，见 undo/__init__.py 的"撤回的记录"）
  <格>-before        第几格的文件改之前的样子（新建的文件没有这一份）
  <格>-after         它这一轮改完时的样子（预览改动用，撤回不用它）
  <格>-undo-<n>      第 n 次撤回之前它的样子（重做时放回去）

"格"（slot）是这一轮里记下的第几个文件，从 0 数起，写在清单里。文件名全由程序用编号和几个整数拼出来，
从不用页面送来的、事件里记着的字符串，所以出不了这个文件夹。编号的格式和会话编号一样（日期-时间-6 位随机字符），
读写之前先查格式，查不过的一律当作不存在。

留底按天数清理（config.UNDO_KEEP_DAYS，启动时清一次，见 prune）：会话里那一行"改了 N 个文件"还在，
只是撤回时说"留底已经清理掉了"。
"""

from __future__ import annotations

import hashlib
import re
import secrets
import shutil
import time
from datetime import datetime
from pathlib import Path

from ..safe import loads_strict, private_dir, to_json, write_file

VERSION = 1
MANIFEST = "manifest.json"
ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")
KINDS = ("created", "modified", "overwritten")  # 新建、修改（str_replace、insert）、整个覆盖（create 一个已有的文件）
SKIPPED = ("", "big", "budget", "unreadable")  # 为什么没留底：太大、这一轮的留底超了上限、读不出来
_HEX = re.compile(r"[0-9a-f]{64}")
_CHUNK = 1 << 20


def before(slot: int) -> str:
    return f"{slot}-before"


def after(slot: int) -> str:
    return f"{slot}-after"


def undone(slot: int, number: int) -> str:
    return f"{slot}-undo-{number}"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint(path: Path) -> dict | None:
    """一个文件现在的样子：{"sha256", "size"}；不存在是 None。读不了（没有权限、是个文件夹）抛出 OSError。
    大文件一段一段地读，不整个放进内存。"""
    hasher, size = hashlib.sha256(), 0
    try:
        with path.open("rb") as file:
            while chunk := file.read(_CHUNK):
                hasher.update(chunk)
                size += len(chunk)
    except FileNotFoundError:
        return None
    return {"sha256": hasher.hexdigest(), "size": size}


class ChangeStore:
    def __init__(self, root: Path):
        self.root = root

    def new_id(self) -> str:
        return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"

    def folder(self, set_id: object) -> Path | None:
        """一轮的文件夹。编号格式不对返回 None。"""
        if not isinstance(set_id, str) or not ID.fullmatch(set_id):
            return None
        return self.root / set_id

    def contains(self, path: Path) -> bool:
        """path 在不在留底的文件夹里（撤回不往这里写，见 restore.py）。"""
        root = self.root.resolve()
        return path == root or path.is_relative_to(root)

    # ---- 留底 ----------------------------------------------------------------

    def put(self, set_id: str, name: str, data: bytes) -> None:
        folder = self.folder(set_id)
        if folder is None:
            raise ValueError(set_id)
        private_dir(self.root)
        private_dir(folder)
        write_file(folder / name, data)

    def get(self, set_id: str, name: str, sha256: str | None = None) -> bytes | None:
        """一份留底的内容。不在了、读不了、和记下的哈希对不上（被改过、坏了），都是 None。"""
        folder = self.folder(set_id)
        if folder is None:
            return None
        try:
            data = (folder / name).read_bytes()
        except OSError:
            return None
        if sha256 is not None and digest(data) != sha256:
            return None
        return data

    def drop(self, set_id: str, name: str) -> None:
        folder = self.folder(set_id)
        if folder is not None:
            (folder / name).unlink(missing_ok=True)

    def remove(self, set_id: str) -> None:
        """整轮的留底都不要了（这一轮最后什么都没改）。"""
        folder = self.folder(set_id)
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)

    # ---- 清单 ----------------------------------------------------------------

    def write_manifest(self, set_id: str, manifest: dict) -> None:
        folder = self.folder(set_id)
        if folder is None:
            raise ValueError(set_id)
        private_dir(self.root)
        private_dir(folder)
        write_file(folder / MANIFEST, to_json(manifest, readable=True))

    def has(self, set_id: object) -> bool:
        """这一轮的清单还在不在（没被清理掉）。不读它，只看一眼。"""
        folder = self.folder(set_id)
        return folder is not None and (folder / MANIFEST).is_file()

    def manifest(self, set_id: object) -> dict | None:
        """读一轮的清单，格式查一遍。不在了（清理掉了）、读不出来、格式不对，都是 None。"""
        folder = self.folder(set_id)
        if folder is None:
            return None
        try:
            data = loads_strict((folder / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if _valid_manifest(data, set_id) else None

    def prune(self, days: int, now: float | None = None) -> int:
        """删掉超过 days 天的留底。按文件夹名里的日期算（不看修改时间：备份、同步软件会改它）。返回删了几轮。"""
        if not self.root.is_dir():
            return 0
        cutoff = (time.time() if now is None else now) - days * 86400
        removed = 0
        for folder in self.root.iterdir():
            if not folder.is_dir() or folder.is_symlink() or not ID.fullmatch(folder.name):
                continue
            try:
                made = datetime.strptime(folder.name[:15], "%Y%m%d-%H%M%S").timestamp()
            except ValueError:
                continue
            if made < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
        return removed


def _valid_state(value: object, blob_required: bool = False) -> bool:
    """清单里"改之前""改之后"的一项：{"sha256", "size", "blob"}（改之前的还有 "mode"）。"""
    if not isinstance(value, dict):
        return False
    sha, size, blob = value.get("sha256"), value.get("size"), value.get("blob")
    if not isinstance(blob, bool) or (blob_required and not blob):
        return False
    if not isinstance(sha, str) or not (sha == "" or _HEX.fullmatch(sha)):
        return False
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return False
    mode = value.get("mode")
    return mode is None or (isinstance(mode, int) and not isinstance(mode, bool) and 0 <= mode <= 0o7777)


def _valid_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    path, slot, kind, dirs = entry.get("path"), entry.get("slot"), entry.get("kind"), entry.get("dirs", [])
    if not isinstance(path, str) or not path or not Path(path).is_absolute():
        return False
    if not isinstance(slot, int) or isinstance(slot, bool) or slot < 0:
        return False
    if kind not in KINDS or entry.get("skipped", "") not in SKIPPED:
        return False
    # 为它新建的文件夹只能是它上面的文件夹：撤回时会删掉空了的这些，清单被人改过也删不到别处去
    if not isinstance(dirs, list) or not all(isinstance(item, str) and Path(item) in Path(path).parents for item in dirs):
        return False
    state = entry.get("before")
    if (kind == "created") != (state is None) or (state is not None and not _valid_state(state)):
        return False
    return _valid_state(entry.get("after"))


def _valid_manifest(data: object, set_id: object) -> bool:  # set_id 只拿来和清单里写的比一比
    if not isinstance(data, dict) or data.get("version") != VERSION or data.get("id") != set_id:
        return False
    files = data.get("files")
    if not isinstance(files, list) or not all(_valid_entry(entry) for entry in files):
        return False
    slots = [entry["slot"] for entry in files]
    return len(set(slots)) == len(slots)
