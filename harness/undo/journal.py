"""一轮里记下文件工具改了哪些文件（"这一轮的账"）。为什么、记什么、不记什么，见 undo/__init__.py。

用法（界面那一侧）：

    journal = changes.start()               # 一轮开始：开一本新账
    with recording(journal):                # 这一轮在这个线程里做的事都记在它上面
        run_turn(...)
    summary = changes.finish(journal)       # 合上账：写清单，返回页面上那一行要的东西（什么都没改是 None）

文件工具那一侧（tools/files.py）只有一句：改文件的时候包在 watching(path, command) 里。
没有在记账（定时任务、聊天通道、测试里直接调用工具）的时候，watching 什么都不做。

账记在 ContextVar 上，而不是 FileEditor 上：文件工具整个程序只有一份，你的对话和几个定时任务同时在用它，
每一轮在自己的线程里跑（ContextVar 不会带进新开的线程），所以"现在是谁在改"看这个线程就知道了。
交给子 Agent 的事在同一个线程里做（见 agents/runner.py），算在这一轮里：它是替主助手做的。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from ..config import UNDO_DIFF_BYTES, UNDO_FILE_BYTES, UNDO_TURN_BYTES, UNDO_TURN_FILES
from . import diff, store
from .store import ChangeStore, fingerprint

_CURRENT: ContextVar[Journal | None] = ContextVar("harness_undo_journal", default=None)


@dataclass
class _Entry:
    """这一轮碰过的一个文件。"""

    slot: int  # 留底的文件名用它（见 store.py）
    path: Path
    kind: str  # created / modified / overwritten（见 store.KINDS）
    before: dict | None  # 改之前：{"sha256", "size", "mode", "blob"}；本来不存在是 None
    after: dict | None = None  # 这一轮最后一次改完：{"sha256", "size", "blob"}
    dirs: list[str] = field(default_factory=list)  # 为了新建它而新建的文件夹（从外到里）。撤回时空了的一起删掉
    skipped: str = ""  # 为什么没留底（见 store.SKIPPED）。没留底的撤不回来
    fresh: bool = True  # 是不是刚才这一次才记上的：这一次什么都没改（比如 old_str 没找到）的话，当它没来过


class Journal:
    def __init__(self, changes_store: ChangeStore):
        self.store = changes_store
        self.id = changes_store.new_id()
        self.entries: dict[Path, _Entry] = {}
        self.untracked: set[Path] = set()  # 超过一轮的上限、没有记下的文件
        self._over: dict[Path, dict | None] = {}  # 超了上限的那一次改之前的样子：真改了才算"没记下"（出错了的不算）
        self.kept = 0  # 已经留了多少字节的底（这一轮的上限是 UNDO_TURN_BYTES）

    # ---- 文件工具改文件的前后（调用的人拿着 tools/files.py 的 _EDITING）----------------

    def before(self, path: Path, command: str) -> None:
        entry = self.entries.get(path)
        if entry is not None:  # 这一轮里改过它了：改之前的样子已经留着，不再留第二份
            entry.fresh = False
            if command == "create" and entry.kind == "modified":
                entry.kind = "overwritten"
            return
        if path.is_dir():
            return  # 文件工具自己会说"这是一个目录"，什么都不改
        if len(self.entries) >= UNDO_TURN_FILES:
            if path not in self.untracked:
                self._over[path] = fingerprint(path)
            return
        slot = len(self.entries)
        entry = _Entry(slot=slot, path=path, kind="modified", before=None)
        try:
            entry.before = self._keep_before(slot, path)
        except OSError:
            entry.before, entry.skipped = {"sha256": "", "size": 0, "mode": None, "blob": False}, "unreadable"
        if entry.before is None:
            entry.kind = "created"
            entry.dirs = _missing_dirs(path)
        else:
            entry.skipped = entry.skipped or entry.before.pop("skipped", "")
            entry.kind = "overwritten" if command == "create" else "modified"
        self.entries[path] = entry

    def after(self, path: Path) -> None:
        if path in self._over:
            if fingerprint(path) != self._over.pop(path):
                self.untracked.add(path)
            return
        entry = self.entries.get(path)
        if entry is None:
            return
        try:
            entry.after = self._keep_after(entry)
        except OSError:
            entry.after = None
        if entry.fresh and _same(entry.before, entry.after):
            self._forget(entry)  # 刚记上、又什么都没改（出错了，或者写进去的和原来一字不差）
            return
        entry.fresh = False
        entry.dirs = [folder for folder in entry.dirs if Path(folder).is_dir()]

    # ---- 合上账 ----------------------------------------------------------------

    def finish(self, display: Callable[[Path], str], place: Callable[[Path], str]) -> dict | None:
        """一轮结束：写清单，返回页面上"改了 N 个文件"那一行要的东西（file_changes 事件的内容）。

        最后改回了原样的文件不算（改了又改回去，等于没改）。什么都没改、也没有超了上限没记下的，
        把这一轮的留底删掉，返回 None。display 把路径写成给人看的样子，place 说它在不在工作目录里（见 FileChanges.place）。
        """
        kept = [entry for entry in self.entries.values() if entry.after is not None and not _same(entry.before, entry.after)]
        for entry in self.entries.values():
            if entry not in kept:
                self._drop_blobs(entry)
        if not kept and not self.untracked:
            self.store.remove(self.id)
            return None
        files, shown = [], []
        for entry in kept:
            added, removed, binary = self._counts(entry)
            files.append({
                "path": str(entry.path), "slot": entry.slot, "kind": entry.kind, "before": entry.before, "after": entry.after,
                "dirs": entry.dirs, "skipped": entry.skipped,
            })  # fmt: skip
            item = {"path": display(entry.path), "kind": entry.kind, "added": added, "removed": removed}
            if binary:
                item["binary"] = True
            if entry.skipped:
                item["skipped"] = entry.skipped
            where = place(entry.path)
            if where:
                item["place"] = where
            shown.append(item)
        if files:
            self.store.write_manifest(self.id, {"version": store.VERSION, "id": self.id, "files": files})
        else:
            self.store.remove(self.id)
        return {"set": self.id if files else "", "files": shown, "untracked": len(self.untracked)}

    # ---- 辅助 ------------------------------------------------------------------

    def _keep_before(self, slot: int, path: Path) -> dict | None:
        """改之前的样子留一份底。不存在返回 None；太大、超了这一轮的上限的，只记下大小和哈希（skipped 写着为什么）。"""
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        mode = stat.st_mode & 0o7777
        if stat.st_size > UNDO_FILE_BYTES or self.kept + stat.st_size > UNDO_TURN_BYTES:
            found = fingerprint(path) or {"sha256": "", "size": stat.st_size}
            return {**found, "mode": mode, "blob": False, "skipped": "big" if stat.st_size > UNDO_FILE_BYTES else "budget"}
        data = path.read_bytes()
        self.store.put(self.id, store.before(slot), data)
        self.kept += len(data)
        return {"sha256": store.digest(data), "size": len(data), "mode": mode, "blob": True}

    def _keep_after(self, entry: _Entry) -> dict | None:
        """这一次改完的样子（每改一次都换掉上一份）。只给预览改动用：放不下就只记哈希，撤回照样能做。"""
        try:
            size = entry.path.stat().st_size
        except FileNotFoundError:
            return None
        previous = entry.after["size"] if entry.after and entry.after.get("blob") else 0
        if size > UNDO_FILE_BYTES or self.kept - previous + size > UNDO_TURN_BYTES:
            self.store.drop(self.id, store.after(entry.slot))
            self.kept -= previous
            found = fingerprint(entry.path)
            return None if found is None else {**found, "blob": False}
        data = entry.path.read_bytes()
        self.store.put(self.id, store.after(entry.slot), data)
        self.kept += len(data) - previous
        return {"sha256": store.digest(data), "size": len(data), "blob": True}

    def _counts(self, entry: _Entry) -> tuple[int | None, int | None, bool]:
        """加了几行、删了几行（页面上的 +3 −1）。不是文本的、太大的、没留底的算不出来，是 None。"""
        old = b"" if entry.before is None else self._blob(entry.before, store.before(entry.slot))
        new = self._blob(entry.after, store.after(entry.slot))
        if old is None or new is None:
            return None, None, False
        counted = diff.counts(old, new)
        if counted is None:
            return None, None, True
        return counted[0], counted[1], False

    def _blob(self, state: dict | None, name: str) -> bytes | None:
        if not state or not state.get("blob") or state["size"] > UNDO_DIFF_BYTES:
            return None
        return self.store.get(self.id, name, state["sha256"])

    def _forget(self, entry: _Entry) -> None:
        self._drop_blobs(entry)
        del self.entries[entry.path]

    def _drop_blobs(self, entry: _Entry) -> None:
        for state, name in ((entry.before, store.before(entry.slot)), (entry.after, store.after(entry.slot))):
            if state and state.get("blob"):
                self.store.drop(self.id, name)
                self.kept -= state["size"]


@contextmanager
def recording(journal: Journal) -> Iterator[Journal]:
    """这一轮在这个线程里做的事，都记在 journal 上。"""
    token = _CURRENT.set(journal)
    try:
        yield journal
    finally:
        _CURRENT.reset(token)


@contextmanager
def watching(path: Path, command: str) -> Iterator[None]:
    """文件工具改 path 的前后（调用的人拿着 tools/files.py 的 _EDITING）。没有在记账就什么都不做。

    改之前留底、改之后记下新的样子都不能让这次修改失败：留底的文件夹写不进去，这个文件就当作"没留底"，照样改。
    """
    journal = _CURRENT.get()
    if journal is None:
        yield
        return
    try:
        journal.before(path, command)
    except OSError:
        pass  # 写不进留底（磁盘满了……）：照样改，只是这一个撤不回来（清单里没有它）
    try:
        yield
    finally:
        try:
            journal.after(path)
        except OSError:
            pass


def _same(first: dict | None, second: dict | None) -> bool:
    """两个样子是不是一样：都不存在，或者哈希相同。没算出哈希的（读不出来）不算一样。"""
    if first is None or second is None:
        return first is None and second is None
    return bool(first.get("sha256")) and first["sha256"] == second.get("sha256")


def _missing_dirs(path: Path) -> list[str]:
    """path 上面还不存在的文件夹，从外到里。"""
    missing = []
    parent = path.parent
    while parent != parent.parent and not parent.exists():
        missing.append(str(parent))
        parent = parent.parent
    return list(reversed(missing))
