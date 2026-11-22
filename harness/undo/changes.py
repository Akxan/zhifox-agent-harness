"""撤回一轮改的文件：界面（网页、终端）只和这里打交道。记账在 journal.py，撤回和重做在 restore.py，
预览在 diff.py，告诉模型在 notes.py，留底存在哪儿在 store.py；整个功能的规矩见 undo/__init__.py。

页面和终端交给这里的只有：一轮的编号（事件里记着的）、清单里的第几个文件、要不要"仍然撤回"。
要写哪个路径、读哪份留底，都由这里照着清单算。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..config import UNDO_DIFF_BYTES, UNDO_KEEP_DAYS
from ..i18n import t
from ..tools.files import FileEditor
from . import diff, store
from .journal import Journal
from .notes import Notes
from .restore import ACTIONS, Issue, Restorer, fold, later_touches, sets_in
from .store import ChangeStore
from .words import explain


@dataclass
class Result:
    """撤回、重做的结果。"""

    error: str = ""  # 一个都没做的原因（给终端、给页面上的提示用）；做成了至少一个是空的
    event: dict | None = None  # 要发的 file_undo 事件的内容（做成了至少一个才有）
    # 要你确认的、做不了的：[{"index", "path", "reasons", "later"?, "error"?, "why"}]。why 是每个原因的一句话（见 words.py）
    asks: list[dict] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)
    ready: list[dict] = field(default_factory=list)  # 只查不做时：可以做的那几个 [{"index", "path", "kind"}]
    notes: list[tuple[str, str]] = field(default_factory=list)  # 给模型的说明：（给人看的路径，做了什么）


class FileChanges:
    def __init__(self, root: Path, editor: FileEditor, private: Sequence[Path] = ()):
        self.store = ChangeStore(root)
        self.editor = editor
        self.restorer = Restorer(self.store, editor.protected, editor.workspace, private)
        self.notes = Notes()

    # ---- 记账 ------------------------------------------------------------------

    def start(self) -> Journal:
        """一轮开始：开一本新账（用 journal.recording 把它挂到这一轮的线程上）。"""
        return Journal(self.store)

    def finish(self, journal: Journal, commands: bool = False) -> dict | None:
        """一轮结束：合上账。返回 file_changes 事件的内容；什么都没改是 None。
        commands：这一轮还执行过命令（页面上把"命令改动的文件没法自动撤回"写得醒目一点）。"""
        try:
            summary = journal.finish(self.editor.display, self.restorer.place)
        except OSError:
            journal.store.remove(journal.id)
            return None  # 清单写不进去（磁盘满了……）：撤不回来，不如不显示那一行
        if summary is not None and commands:
            summary["commands"] = True
        return summary

    def prune(self) -> int:
        """删掉过期的留底（启动时调用一次）。"""
        return self.store.prune(UNDO_KEEP_DAYS)

    # ---- 状态 ------------------------------------------------------------------

    def applied(self, set_id: str, events: Sequence[dict]) -> int:
        """这一轮改的文件里，有几个现在还是改过的样子（没有撤回）、能撤回。留底已经不在了是 0。

        每次发状态都要问一次（最后一轮的"同时撤回"，见 WebApp._rewind_state），所以文件的个数看事件里的那一行，
        不去读清单，只看一眼它还在不在。"""
        found = next((event for event in events if isinstance(event, dict) and event.get("type") == "file_changes" and event.get("set") == set_id), None)
        if not set_id or found is None or not isinstance(found.get("files"), list) or not self.store.has(set_id):
            return 0
        files = found["files"]
        # 当初没留底的（太大……）撤不回来，不算：一个都撤不回来时，倒回去的确认卡片上就不放"同时撤回"
        return sum(not state.undone and not (isinstance(item, dict) and item.get("skipped")) for item, state in zip(files, fold(set_id, events, len(files)), strict=True))

    def last_set(self, turn_events: Sequence[dict]) -> str:
        """一轮的事件里那一轮改动的编号（没改文件是空的）。"""
        found = sets_in(turn_events)
        return found[-1] if found else ""

    # ---- 撤回、重做 ------------------------------------------------------------

    def act(
        self, action: str, set_id: object, events: Sequence[dict], indices: Sequence[int] | None = None, force: bool = False, check: bool = False
    ) -> Result:
        """撤回（undo）或重做（redo）一轮改的文件。indices 是清单里的第几个，None 是这一轮里所有能做的。

        events 是这段会话到现在为止的事件（推出每个文件现在的状态，见 restore.fold）。set_id 必须是其中某一轮的编号。
        要你确认的、做不了的，又不是 force：什么都不做，把原因交回去（Result.asks、blocked）。
        check：只查不做，把会做的列在 Result.ready 里（终端先列出来问你一句）。
        """
        if action not in ACTIONS or not isinstance(set_id, str) or set_id not in sets_in(events):
            return Result(error=t("找不到这一轮的改动记录。看一眼最新的内容再试。"))
        manifest = self.store.manifest(set_id)
        if manifest is None:
            return Result(error=t("这一轮的留底已经不在了（只保留 {days} 天），撤不回来了。", days=UNDO_KEEP_DAYS))
        files = manifest["files"]
        states = fold(set_id, events, len(files))
        if indices is None:
            indices = [index for index, state in enumerate(states) if state.undone == (action == "redo")]
            if not indices:
                return Result(error=t("这一轮的改动已经全部撤回了。") if action == "undo" else t("这一轮没有撤回过的文件，不用重做。"))
        elif len(set(indices)) != len(indices) or not all(isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(files) for index in indices):
            return Result(error=t("参数不正确"))
        later = later_touches(set_id, events, self.store) if action == "undo" else {}
        outcome = self.restorer.run(action, manifest, states, list(indices), later, force, check)
        shown = [self.editor.display(Path(entry["path"])) for entry in files]
        result = Result(
            asks=[self._issue(action, issue, shown) for issue in outcome.asks],
            blocked=[self._issue(action, issue, shown) for issue in outcome.blocked],
            notes=[(shown[index], what) for index, what in outcome.lines],
            ready=[{"index": index, "path": shown[index], "kind": files[index]["kind"]} for index in outcome.ready],
        )
        if check:
            return result
        if outcome.done:
            result.event = {"set": set_id, "action": action, "files": outcome.done, "paths": [shown[item["index"]] for item in outcome.done]}
        elif result.asks or (not force and len(result.blocked) < len(indices)):  # 做不了的之外还有做得了的：问你要不要做那些
            result.error = t("这几个文件要先确认一下再撤回。") if action == "undo" else t("这几个文件要先确认一下再重做。")
        elif result.blocked:
            result.error = t("没有能撤回的文件。") if action == "undo" else t("没有能重做的文件。")
        return result

    @staticmethod
    def _issue(action: str, issue: Issue, shown: list[str]) -> dict:
        item: dict = {"index": issue.index, "path": shown[issue.index], "reasons": issue.reasons}
        if issue.later:
            item["later"] = issue.later
        if issue.error:
            item["error"] = issue.error
        item["why"] = explain(action, item)
        return item

    # ---- 预览 ------------------------------------------------------------------

    def diff(self, set_id: object, index: object, events: Sequence[dict]) -> tuple[str, dict]:
        """这一轮对第 index 个文件做了什么（改之前 → 这一轮改完），一行一行的（格式见 diff.py）。返回（不能看的原因，预览）。"""
        if not isinstance(set_id, str) or set_id not in sets_in(events):
            return t("找不到这一轮的改动记录。看一眼最新的内容再试。"), {}
        manifest = self.store.manifest(set_id)
        if manifest is None:
            return t("这一轮的留底已经不在了（只保留 {days} 天），撤不回来了。", days=UNDO_KEEP_DAYS), {}
        files = manifest["files"]
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(files):
            return t("参数不正确"), {}
        entry = files[index]
        old = b"" if entry["before"] is None else self._readable(set_id, entry["before"], store.before(entry["slot"]))
        new = self._readable(set_id, entry["after"], store.after(entry["slot"]))
        if old is None or new is None:
            # 太大没留底、超了上限、留底坏了：只能说改了，看不到改了什么
            return "", {"rows": [], "more": 0, "binary": False, "unavailable": True}
        return "", diff.preview(old, new)

    def _readable(self, set_id: str, state: dict, name: str) -> bytes | None:
        if not state.get("blob") or state["size"] > UNDO_DIFF_BYTES:
            return None
        return self.store.get(set_id, name, state["sha256"])
