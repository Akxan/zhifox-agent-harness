"""撤回和重做：把一轮改的文件恢复回去，或者再放回来。规矩见 undo/__init__.py 的"撤回和重做"。

每个文件现在是什么状态（撤回过没有、撤回过几次、撤回时换下来的是什么），不另外存，从会话的事件里推出来（fold）：
file_undo 事件就是"什么时候、对哪几个文件、做了什么"的记录。清单（manifest.json）只记这一轮本身做了什么，写好以后不再改。

做之前先查一遍（_check），每个文件得出三种结果之一：
  可以做    直接做。
  要你确认  这一轮之后它又被改过（哈希对不上）、后面的几轮也改过它、它在工作目录之外或者在助手自己的数据目录里。
            有一个要确认的，整个请求都先不做，把原因交回去；你点"仍然撤回"（force）再做。
  做不了    保存密钥的文件、留底的文件夹、路径换成了链接、留底没有了或者坏了、当初太大没留底、已经撤回过了……
            这些不做，原因同样交回去。和"要你确认"的一起问你：你点"仍然撤回"时，做得了的做，做不了的跳过。
查和做都拿着文件工具的那把锁（tools/files.py 的 _EDITING）：查完到写进去之间，别的对话、定时任务插不进来改同一个文件。
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..config import UNDO_FILE_BYTES
from ..safe import write_file
from ..tools.files import _EDITING  # 和文件工具排同一个队（见上面）
from . import store
from .store import ChangeStore, fingerprint

ACTIONS = ("undo", "redo")
# 要你确认的原因（页面和终端照着它们写成话）
ASK = ("later", "changed", "missing", "outside", "private")
# 做不了的原因
BLOCK = ("protected", "moved", "directory", "snapshot", "big", "budget", "unreadable", "done", "applied", "failed")


@dataclass
class FileState:
    """一轮里的一个文件现在怎么样了（从事件里推出来，见 fold）。"""

    undone: bool = False
    undos: int = 0  # 撤回过几次：第 n 次撤回之前的样子留在 <格>-undo-<n> 里（见 store.py）
    replaced: dict | None = None  # 最近一次撤回时换下来的那个样子 {"sha256", "size", "mode", "blob"}；那时文件不在是 None


@dataclass
class Issue:
    index: int  # 清单里的第几个文件
    reasons: list[str]  # ASK 或 BLOCK 里的词
    later: int = 0  # 后面还有几轮也改过它（reasons 里有 later 时）
    error: str = ""  # failed 的时候：系统给的原因


@dataclass
class Outcome:
    done: list[dict] = field(default_factory=list)  # file_undo 事件里的 files：做成了的
    ready: list[int] = field(default_factory=list)  # 查过、可以做（或者确认以后可以做）的那几个（只查不做时看它，见 run 的 check）
    asks: list[Issue] = field(default_factory=list)
    blocked: list[Issue] = field(default_factory=list)
    lines: list[tuple[int, str]] = field(default_factory=list)  # 给模型的说明：（第几个文件，做了什么：undo / delete / redo）


def fold(set_id: str, events: Iterable[dict], count: int) -> list[FileState]:
    """按事件推出这一轮的 count 个文件现在的状态。格式不对的事件、不合理的步骤（没撤回过却要重做）跳过。"""
    states = [FileState() for _ in range(count)]
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "file_undo" or event.get("set") != set_id:
            continue
        action = event.get("action")
        for item in event.get("files") if isinstance(event.get("files"), list) else []:
            index = item.get("index") if isinstance(item, dict) else None
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < count:
                continue
            state = states[index]
            if action == "undo" and not state.undone:
                number = item.get("n")
                state.undos = number if isinstance(number, int) and not isinstance(number, bool) and number > state.undos else state.undos + 1
                replaced = item.get("replaced")
                state.replaced = replaced if isinstance(replaced, dict) else None
                state.undone = True
            elif action == "redo" and state.undone:
                state.undone = False
    return states


def sets_in(events: Iterable[dict]) -> list[str]:
    """事件里按先后出现的每一轮改动的编号（没记下任何文件的那几轮不算）。"""
    return [event["set"] for event in events if isinstance(event, dict) and event.get("type") == "file_changes" and isinstance(event.get("set"), str) and event["set"]]


def later_touches(set_id: str, events: Sequence[dict], changes: ChangeStore) -> dict[str, int]:
    """这一轮之后，还有几轮也改过同一个文件（而且那一轮的改动还在，没有撤回）。键是规整过的路径。"""
    order = sets_in(events)
    if set_id not in order:
        return {}
    touched: dict[str, int] = {}
    for later in order[order.index(set_id) + 1 :]:
        manifest = changes.manifest(later)
        if manifest is None:
            continue
        states = fold(later, events, len(manifest["files"]))
        for entry, state in zip(manifest["files"], states, strict=True):
            if not state.undone:
                key = same_path(entry["path"])
                touched[key] = touched.get(key, 0) + 1
    return touched


def same_path(path: str) -> str:
    """比较两个路径时用的写法（Windows 上不分大小写、斜杠统一）。"""
    return os.path.normcase(os.path.normpath(path))


class Restorer:
    def __init__(self, changes: ChangeStore, protected: Iterable[Path], workspace: Path, private: Iterable[Path]):
        self.store = changes
        self.protected = {same_path(str(path.resolve())) for path in protected}
        self.workspace = workspace.resolve()
        self.private = [path.resolve() for path in private]

    def place(self, path: Path) -> str:
        """一个文件在哪儿："" 是工作目录里，"private" 是助手自己的数据目录里，"outside" 是工作目录之外。"""
        if any(path == folder or path.is_relative_to(folder) for folder in self.private):
            return "private"
        if path == self.workspace or path.is_relative_to(self.workspace):
            return ""
        return "outside"

    def run(
        self, action: str, manifest: dict, states: list[FileState], indices: Sequence[int], later: dict[str, int], force: bool, check: bool = False
    ) -> Outcome:
        """撤回（undo）或重做（redo）indices 这几个文件。有要你确认的、做不了的，又不是 force：什么都不做，原因交回去。
        check：只查不做（终端的 /undo 先把要做的列出来问你一句，见 undo/terminal.py）。"""
        files = manifest["files"]
        outcome = Outcome()
        with _EDITING:
            checked = []
            for index in indices:
                reasons, current = self._check(action, manifest["id"], files[index], states[index])
                if any(reason in BLOCK for reason in reasons):
                    outcome.blocked.append(Issue(index, [reason for reason in reasons if reason in BLOCK]))
                    continue
                touches = later.get(same_path(files[index]["path"]), 0) if action == "undo" else 0
                if touches and "changed" in reasons:
                    reasons = ["later" if reason == "changed" else reason for reason in reasons]
                if reasons:
                    outcome.asks.append(Issue(index, reasons, later=touches))
                checked.append((index, current))
            outcome.ready = [index for index, _ in checked]
            if check or ((outcome.asks or outcome.blocked) and not force):
                return outcome
            for index, current in checked:
                try:
                    if action == "undo":
                        outcome.done.append(self._undo(manifest["id"], files[index], states[index], index, current))
                        outcome.lines.append((index, "delete" if files[index]["before"] is None else "undo"))
                    else:
                        outcome.done.append(self._redo(manifest["id"], files[index], states[index], index))
                        outcome.lines.append((index, "redo"))
                except OSError as exc:
                    outcome.blocked.append(Issue(index, ["failed"], error=str(exc) or type(exc).__name__))
            if action == "undo":
                _sweep(files, states, outcome.done)
        return outcome

    # ---- 查 ------------------------------------------------------------------

    def _check(self, action: str, set_id: str, entry: dict, state: FileState) -> tuple[list[str], dict | None]:
        """一个文件能不能做、要不要先问你。返回（原因，它现在的样子）。"""
        if action == "undo" and state.undone:
            return ["done"], None
        if action == "redo" and not state.undone:
            return ["applied"], None
        path = Path(entry["path"])
        problem = self._path_problem(path)
        if problem:
            return [problem], None
        if action == "undo":
            if entry["skipped"]:
                return [entry["skipped"]], None
            if entry["before"] is not None and self.store.get(set_id, store.before(entry["slot"]), entry["before"]["sha256"]) is None:
                return ["snapshot"], None
            expected = entry["after"]
        else:
            replaced = state.replaced
            if replaced is not None and (not replaced.get("blob") or self.store.get(set_id, store.undone(entry["slot"], state.undos), replaced.get("sha256")) is None):
                return ["big" if not replaced.get("blob") else "snapshot"], None
            expected = entry["before"]
        try:
            current = fingerprint(path)
        except OSError:
            return ["unreadable"], None
        reasons = []
        if not _matches(current, expected):
            if action == "undo" and entry["before"] is None and current is None:
                pass  # 这一轮新建的文件已经不在了：撤回本来就是把它删掉，没什么可冲掉的
            elif current is None:
                reasons.append("missing")
            else:
                reasons.append("changed")
        where = self.place(path)
        if where:
            reasons.append(where)
        return reasons, current

    def _path_problem(self, path: Path) -> str:
        """这个位置能不能写。撤回只写清单里记下的那个位置，而且只在它还是"那个位置"的时候写。"""
        if not path.is_absolute():
            return "moved"
        try:
            real = path.resolve()
        except OSError:
            return "moved"
        if same_path(str(real)) != same_path(str(path)):
            return "moved"  # 它自己或者上面的文件夹换成了链接：不跟着链接去写别处
        if same_path(str(real)) in self.protected or self.store.contains(real):
            return "protected"
        if path.is_dir():
            return "directory"
        return ""

    # ---- 做 ------------------------------------------------------------------

    def _undo(self, set_id: str, entry: dict, state: FileState, index: int, current: dict | None) -> dict:
        """恢复成这一轮之前的样子。先把现在的样子留一份底（重做时放回去）。"""
        path, number = Path(entry["path"]), state.undos + 1
        replaced = None
        if current is not None:
            stat = path.stat()
            replaced = {"sha256": current["sha256"], "size": current["size"], "mode": stat.st_mode & 0o7777, "blob": False}
            if current["size"] <= UNDO_FILE_BYTES:
                data = path.read_bytes()
                if store.digest(data) == current["sha256"]:
                    self.store.put(set_id, store.undone(entry["slot"], number), data)
                    replaced["blob"] = True
        if entry["before"] is None:
            path.unlink(missing_ok=True)  # 为它新建的文件夹等这一次的都做完再收拾（见 _sweep）
        else:
            data = self.store.get(set_id, store.before(entry["slot"]), entry["before"]["sha256"])
            if data is None:
                raise OSError("留底不见了")
            _put(path, data, entry["before"].get("mode"))
        return {"index": index, "n": number, "replaced": replaced}

    def _redo(self, set_id: str, entry: dict, state: FileState, index: int) -> dict:
        """把撤回时换下来的那个样子放回去（那时文件不在的话，就让它不在）。"""
        path, replaced = Path(entry["path"]), state.replaced
        if replaced is None:
            path.unlink(missing_ok=True)
        else:
            data = self.store.get(set_id, store.undone(entry["slot"], state.undos), replaced.get("sha256"))
            if data is None:
                raise OSError("留底不见了")
            _put(path, data, replaced.get("mode"))
        return {"index": index}


def _sweep(files: list[dict], states: list[FileState], done: list[dict]) -> None:
    """撤回以后，这一轮为新建的文件新建的文件夹，空了的删掉，从里到外。

    要等这一次的都撤回完了再一起收拾：同一轮里先建了 new/sub/a.txt（为它新建了 new 和 new/sub），
    又建了 new/b.txt（这时 new 已经在了，它没记着 new）。一个一个删的话，删 a 时 new 里还有 b，删 b 时又不知道 new 是这一轮建的，
    最后留下一个空的 new。所以把这一次撤回的、以前撤回过的新建文件记着的文件夹合在一起，最深的先删；不空的（里面有别的东西）留着。
    """
    undone = {item["index"] for item in done} | {index for index, state in enumerate(states) if state.undone}
    folders = {folder for index in undone if files[index]["before"] is None for folder in files[index]["dirs"]}
    for folder in sorted(folders, key=lambda name: len(Path(name).parts), reverse=True):
        try:
            Path(folder).rmdir()
        except OSError:
            pass  # 不空，或者已经不在了


def _matches(current: dict | None, expected: dict | None) -> bool:
    if current is None or expected is None:
        return current is None and expected is None
    return bool(expected.get("sha256")) and current["sha256"] == expected["sha256"]


def _put(path: Path, data: bytes, mode: int | None) -> None:
    """原样写回去：先写临时文件再一步换上（见 safe.write_file），再把权限改回当初的样子（write_file 写出来的是 600）。
    文件夹被删掉了的话，先建回来。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_file(path, data)
    try:
        os.chmod(path, mode if isinstance(mode, int) else 0o644)
    except OSError:
        pass  # 有的文件系统（网络盘、Windows 上的一些盘）不让改权限：内容已经回来了，权限照它的默认
