"""撤回一轮改的文件（harness/undo/）：记账、留底、撤回和重做、冲突、不能碰的位置、上限、存盘以后再撤回、告诉模型。

全部在 tmp_path 里做：工作目录、数据目录（连同一个编造的 .env）、留底的文件夹都是临时的，碰不到真正的 data/ 和 .env。
文件工具用的是真的 FileEditor（改文件的那一段包在 journal.watching 里），所以这里走的是真实的代码路径：
一轮里调用文件工具 → 合上账得到 file_changes 事件 → 照着事件撤回、重做，再把 file_undo 事件记回去
（"每个文件现在是什么状态"只从事件里推出来，见 restore.fold）。

改动的预览（diff.py）、Windows 风格的路径和换行在 test_undo_diff.py 里测。
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
from datetime import datetime
from pathlib import Path

import pytest
from modes import has_mode

from harness import pathnames
from harness.config import UNDO_KEEP_DAYS
from harness.conversations import ConversationStore
from harness.i18n import t
from harness.tools.base import ToolError
from harness.tools.files import FileEditor
from harness.undo import changes as changes_module
from harness.undo import journal as journal_module
from harness.undo import notes as notes_module
from harness.undo import store
from harness.undo.changes import FileChanges
from harness.undo.journal import recording, watching
from harness.undo.notes import Notes
from harness.undo.store import ChangeStore, digest

# Windows 没有 POSIX 的权限位（chmod 只能改"只读"），600 / 700 / 755 这些在那里查不了
posix = pytest.mark.skipif(sys.platform == "win32", reason="Windows 没有 POSIX 的权限位")

NOT_FOUND = "找不到这一轮的改动记录。看一眼最新的内容再试。"


class Rig:
    """一套临时的工作目录、数据目录、文件工具和撤回的入口，和网页、终端里组装的样子一样：
    留底在 data/backups/turns/，data/ 是"助手自己的数据目录"，data/.env 在文件工具的 protected 名单里。"""

    def __init__(self, tmp_path: Path):
        self.work = tmp_path / "work"
        self.data = tmp_path / "data"
        self.work.mkdir()
        self.data.mkdir()
        # 文件工具记下的路径都是展开过链接的（FileEditor.resolve），这里也用展开过的写法来比
        self.work = self.work.resolve()
        self.data = self.data.resolve()
        self.env = self.data / ".env"
        self.root = self.data / "backups" / "turns"
        self.editor = FileEditor(self.work, self.data / "backups", protected=[self.env])
        self.changes = FileChanges(self.root, self.editor, [self.data])
        self.events: list[dict] = []

    def turn(self, *calls: dict, commands: bool = False) -> dict | None:
        """一轮：在记账里依次调用文件工具（出错的照样往下做，和真的循环把错误交回给模型一样），合上账，
        把 file_changes 事件记进 events。返回事件的内容；什么都没改是 None。"""
        journal = self.changes.start()
        with recording(journal):
            for call in calls:
                try:
                    self.editor.run(call)
                except ToolError:
                    pass
        summary = self.changes.finish(journal, commands=commands)
        if summary is not None:
            self.events.append({"type": "file_changes", **summary})
        return summary

    def act(self, action: str, set_id: object, indices: list | None = None, force: bool = False):
        """撤回或重做；做成了的话，像界面一样把 file_undo 事件记进 events。"""
        result = self.changes.act(action, set_id, self.events, indices, force)
        if result.event is not None:
            self.events.append({"type": "file_undo", **result.event})
        return result

    def manifest(self, set_id: str) -> dict | None:
        return self.changes.store.manifest(set_id)

    def tamper(self, set_id: str, change) -> None:
        """手动改一下清单（模拟磁盘上的文件被动过）。改完仍是一份格式合法的清单，否则读的时候就被当成"不在了"。"""
        path = self.root / set_id / store.MANIFEST
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data), encoding="utf-8")
        assert self.manifest(set_id) is not None


@pytest.fixture
def rig(tmp_path) -> Rig:
    return Rig(tmp_path)


def create(path: Path, text: str) -> dict:
    return {"command": "create", "path": str(path), "file_text": text}


def replace(path: Path, old: str, new: str) -> dict:
    return {"command": "str_replace", "path": str(path), "old_str": old, "new_str": new}


def insert(path: Path, line: int, text: str) -> dict:
    return {"command": "insert", "path": str(path), "insert_line": line, "insert_text": text}


def reasons(items: list[dict]) -> list[tuple[int, list[str]]]:
    return [(item["index"], item["reasons"]) for item in items]


def bare(items: list[dict]) -> list[dict]:
    """去掉给人看的那句话（why，见 undo/words.py），只比较原因本身。"""
    return [{key: value for key, value in item.items() if key != "why"} for item in items]


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def names(folder: Path) -> list[str]:
    return sorted(item.name for item in folder.iterdir())


def can_symlink(tmp_path: Path) -> bool:
    probe = tmp_path / "probe-link"
    try:
        probe.symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


# ---- 留底和清单 -------------------------------------------------------------


def test_snapshot_blobs_and_manifest(rig):
    """改之前的样子留在 turns/<编号>/<格>-before，改完的在 <格>-after；清单记着绝对路径、种类、前后的哈希、大小和权限。
    撤回只认这份清单（不认页面送来的路径），所以每一项都要对。"""
    path = rig.work / "a.txt"
    original = b"one\ntwo\n"
    path.write_bytes(original)
    mode = path.stat().st_mode & 0o7777

    summary = rig.turn(replace(path, "two", "TWO"))
    changed = path.read_bytes()  # 文件工具按文字写：Windows 上换行是 \r\n，所以不写死

    set_id = summary["set"]
    assert store.ID.fullmatch(set_id)  # 和会话编号一样的格式：日期-时间-6 位随机字符
    assert summary["files"] == [{"path": "a.txt", "kind": "modified", "added": 1, "removed": 1}]
    assert summary["untracked"] == 0 and "commands" not in summary
    folder = rig.root / set_id
    assert names(folder) == ["0-after", "0-before", "manifest.json"]
    assert (folder / "0-before").read_bytes() == original
    assert (folder / "0-after").read_bytes() == changed
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == {
        "version": store.VERSION,
        "id": set_id,
        "files": [{
            "path": str(path), "slot": 0, "kind": "modified",
            "before": {"sha256": digest(original), "size": len(original), "mode": mode, "blob": True},
            "after": {"sha256": digest(changed), "size": len(changed), "blob": True},
            "dirs": [], "skipped": "",
        }],
    }  # fmt: skip
    assert Path(manifest["files"][0]["path"]).is_absolute()
    assert rig.manifest(set_id) == manifest  # 读回来也过得了格式检查


@posix
def test_snapshots_are_private(rig):
    """留底里是你文件的原文：文件夹 700、文件 600（和会话文件一样，只有自己的账号能读），撤回时留的那份也一样。"""
    path = rig.work / "a.txt"
    path.write_text("secret plan\n")
    summary = rig.turn(replace(path, "plan", "PLAN"))
    rig.act("undo", summary["set"])

    folder = rig.root / summary["set"]
    assert has_mode(rig.root, 0o700) and has_mode(folder, 0o700)  # Windows 上看的是 ACL，不是权限位（见 tests/modes.py）
    assert names(folder) == ["0-after", "0-before", "0-undo-1", "manifest.json"]
    for name in names(folder):
        assert has_mode(folder / name, 0o600), name


@pytest.mark.skipif(os.name == "nt", reason="Windows 上的文件没有可执行这一位：chmod 只能改只读，权限位读回来总是 666")
def test_commands_flag_is_passed_through(rig):
    """这一轮还执行过命令时，事件上带着 commands：页面把"命令改动的文件没法自动撤回"写得醒目一点。"""
    path = rig.work / "a.txt"
    path.write_text("x\n")
    assert rig.turn(replace(path, "x", "y"), commands=True)["commands"] is True


# ---- 记下什么 ---------------------------------------------------------------


def test_kinds_created_modified_overwritten_inserted(rig):
    """新建（连同新建的几层文件夹）、str_replace、create 覆盖已有的文件、insert，各自记成对的种类。
    新建的文件没有"改之前"，记下的是为它新建的文件夹（从外到里），撤回时空了的一起删掉。"""
    modified, overwritten, inserted = rig.work / "m.txt", rig.work / "o.txt", rig.work / "i.txt"
    modified.write_text("alpha\n")
    overwritten.write_text("old\n")
    inserted.write_text("x\ny\n")
    new = rig.work / "new" / "deep" / "n.txt"

    summary = rig.turn(
        create(new, "hello\n"),
        replace(modified, "alpha", "beta"),
        create(overwritten, "brand new\n"),
        insert(inserted, 1, "between"),
    )

    shown = [(item["path"], item["kind"], item["added"], item["removed"]) for item in summary["files"]]
    assert shown == [
        (str(Path("new", "deep", "n.txt")), "created", 1, 0),
        ("m.txt", "modified", 1, 1),
        ("o.txt", "overwritten", 1, 1),
        ("i.txt", "modified", 1, 0),
    ]
    entries = rig.manifest(summary["set"])["files"]
    assert [entry["slot"] for entry in entries] == [0, 1, 2, 3]
    assert entries[0]["before"] is None
    assert entries[0]["dirs"] == [str(rig.work / "new"), str(rig.work / "new" / "deep")]
    assert [entry["dirs"] for entry in entries[1:]] == [[], [], []]
    assert "0-before" not in names(rig.root / summary["set"])
    assert inserted.read_text() == "x\nbetween\ny\n"


def test_kind_after_several_edits_in_one_turn(rig):
    """同一轮里改了又整个覆盖的，算"覆盖"；新建了又改的，还是"新建"（撤回就是删掉它）。留底只留第一次碰到它之前的那一份。"""
    edited = rig.work / "a.txt"
    edited.write_bytes(b"one\n")
    fresh = rig.work / "b.txt"

    summary = rig.turn(
        replace(edited, "one", "ONE"),
        create(edited, "rewritten\n"),
        create(fresh, "first\n"),
        replace(fresh, "first", "second"),
    )

    assert [(item["path"], item["kind"]) for item in summary["files"]] == [("a.txt", "overwritten"), ("b.txt", "created")]
    folder = rig.root / summary["set"]
    assert (folder / "0-before").read_bytes() == b"one\n"  # 不是中间那一版 ONE
    assert (folder / "0-after").read_bytes() == edited.read_bytes() and edited.read_text() == "rewritten\n"
    assert (folder / "1-after").read_bytes() == fresh.read_bytes() and fresh.read_text() == "second\n"


def test_failed_calls_leave_no_entry(rig):
    """文件工具出错（old_str 找不到、文件不存在、insert 的行号不对）时什么都没改：清单里没有它，也不留底，
    否则撤回时会去"恢复"一个根本没动过的文件；它也不占格子（后面的文件接着用这个格号）。"""
    good, other = rig.work / "good.txt", rig.work / "other.txt"
    good.write_text("1\n")
    other.write_text("keep\n")
    missing = rig.work / "ghost" / "nothing.txt"
    later = rig.work / "later.txt"
    later.write_bytes(b"L\n")

    summary = rig.turn(
        replace(good, "1", "2"),
        replace(other, "not there", "x"),
        replace(missing, "a", "b"),
        insert(other, 99, "x"),
        replace(later, "L", "M"),
    )

    assert [item["path"] for item in summary["files"]] == ["good.txt", "later.txt"]
    assert [entry["slot"] for entry in rig.manifest(summary["set"])["files"]] == [0, 1]
    assert names(rig.root / summary["set"]) == ["0-after", "0-before", "1-after", "1-before", "manifest.json"]
    assert (rig.root / summary["set"] / "1-before").read_bytes() == b"L\n"
    assert not missing.parent.exists()
    assert other.read_text() == "keep\n"


def test_turn_without_net_change_leaves_nothing(rig):
    """改了又改回去、整个覆盖成一模一样的内容、只有出错的调用：等于没改。合上账返回 None（页面上不出那一行），留底的文件夹删掉。"""
    path, same = rig.work / "a.txt", rig.work / "same.txt"
    path.write_text("one\n")
    same.write_text("same\n")
    for calls in (
        [replace(path, "one", "ONE"), replace(path, "ONE", "one")],
        [create(same, "same\n")],
        [replace(path, "nowhere", "x"), {"command": "view", "path": str(path)}],
    ):
        journal = rig.changes.start()
        with recording(journal):
            for call in calls:
                try:
                    rig.editor.run(call)
                except ToolError:
                    pass
        assert rig.changes.finish(journal) is None
        assert not (rig.root / journal.id).exists()
    assert path.read_text() == "one\n"


def test_manifest_that_cannot_be_written_shows_nothing(rig, monkeypatch):
    """清单写不进去（磁盘满了……）：撤不回来，就不显示那一行，留了一半的底也删掉。文件工具那一次的修改照样成功。"""
    path = rig.work / "a.txt"
    path.write_text("one\n")

    def full(*args, **kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(ChangeStore, "write_manifest", full)
    journal = rig.changes.start()
    with recording(journal):
        rig.editor.run(replace(path, "one", "ONE"))
    assert rig.changes.finish(journal) is None
    assert not (rig.root / journal.id).exists()
    assert path.read_text() == "ONE\n"


def test_snapshot_failure_never_breaks_the_edit(rig, monkeypatch):
    """留底写不进去时文件工具照样改（不能因为"撤回"这个附加功能让模型的修改失败），只是这个文件撤不回来，清单里没有它。"""
    path = rig.work / "a.txt"
    path.write_text("one\n")

    def full(*args, **kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(ChangeStore, "put", full)
    assert rig.turn(replace(path, "one", "ONE")) is None
    assert path.read_text() == "ONE\n"


# ---- 撤回和重做 -------------------------------------------------------------


def test_undo_restores_exact_bytes_and_redo_puts_them_back(rig):
    """撤回原样恢复每一个字节（Windows 的 \\r\\n 也一样，哪怕文件工具改的时候把它写成了 \\n）；重做把撤回时换下来的放回去。"""
    path = rig.work / "crlf.txt"
    original = b"one\r\ntwo\r\n\xe4\xb8\xad\r\n"
    path.write_bytes(original)
    summary = rig.turn(replace(path, "two", "TWO"))
    changed = path.read_bytes()
    # 文件工具按文字读写，\r\n 读进来变成了 \n（写回去是这个系统的换行）：字节和原来的不一样了
    assert changed != original and path.read_text(encoding="utf-8") == "one\nTWO\n中\n"
    # 预览按行比，不把换行的不同算成整篇都改了（见 diff.lines_of）
    assert (summary["files"][0]["added"], summary["files"][0]["removed"]) == (1, 1)

    undo = rig.act("undo", summary["set"])
    assert undo.error == "" and path.read_bytes() == original
    redo = rig.act("redo", summary["set"])
    assert redo.error == "" and path.read_bytes() == changed


def test_undo_deletes_created_files_and_only_their_empty_new_folders(rig):
    """这一轮新建的文件撤回时删掉，为它新建的文件夹空了的一起删掉；后来放进了别的东西的、本来就有的文件夹不动。重做再建回来。"""
    deep = rig.work / "new" / "deep" / "n.txt"
    shared = rig.work / "shared" / "s.txt"
    existing = rig.work / "existing"
    existing.mkdir()
    inside_existing = existing / "e.txt"

    summary = rig.turn(create(deep, "deep\n"), create(shared, "shared\n"), create(inside_existing, "e\n"))
    (rig.work / "shared" / "yours.txt").write_text("你后来放进去的\n")  # 这一轮之后，别人往这个新文件夹里放了东西

    result = rig.act("undo", summary["set"])

    assert result.error == "" and result.asks == [] and result.blocked == []
    assert not deep.exists() and not (rig.work / "new").exists()
    assert not shared.exists() and (rig.work / "shared" / "yours.txt").exists()
    assert not inside_existing.exists() and existing.is_dir()
    assert [what for _, what in result.notes] == ["delete", "delete", "delete"]

    rig.act("redo", summary["set"])
    assert deep.read_text() == "deep\n" and shared.read_text() == "shared\n" and inside_existing.read_text() == "e\n"


def test_undo_removes_a_new_folder_shared_by_two_created_files(rig):
    """同一轮里新建的两个文件共用一个新建的文件夹：整轮撤回以后，这个文件夹也该不在了（它是这一轮建的，现在空了）。"""
    first = rig.work / "new" / "sub" / "f.txt"
    second = rig.work / "new" / "g.txt"
    summary = rig.turn(create(first, "f\n"), create(second, "g\n"))

    rig.act("undo", summary["set"])

    assert not first.exists() and not second.exists()
    assert not (rig.work / "new").exists()


@posix
def test_undo_and_redo_keep_file_modes(rig):
    """撤回把权限改回这一轮之前的样子（write_file 新写出来的文件是 600，不改回去的话脚本就不能执行了）；
    重做放回撤回时的权限。只改权限不改内容不算"又改过"（看的是哈希）。"""
    path = rig.work / "run.sh"
    path.write_text("echo one\n")
    path.chmod(0o755)
    summary = rig.turn(replace(path, "one", "two"))
    path.chmod(0o700)  # 这一轮之后你改了权限

    undo = rig.act("undo", summary["set"])
    assert undo.asks == [] and path.read_text() == "echo one\n" and mode_of(path) == 0o755
    assert undo.event["files"][0]["replaced"]["mode"] == 0o700

    rig.act("redo", summary["set"])
    assert path.read_text() == "echo two\n" and mode_of(path) == 0o700


def test_undo_redo_undo_again_keeps_a_new_snapshot_each_time(rig):
    """撤回→重做→再撤回：第 n 次撤回之前的样子存在 <格>-undo-<n>，再重做放回的是最近那一份。"""
    path = rig.work / "a.txt"
    path.write_text("one\n")
    summary = rig.turn(replace(path, "one", "ONE"))
    changed = path.read_bytes()
    folder = rig.root / summary["set"]
    manifest = rig.manifest(summary["set"])

    first = rig.act("undo", summary["set"])
    assert first.event["files"][0]["n"] == 1 and path.read_text() == "one\n"
    rig.act("redo", summary["set"])
    assert path.read_text() == "ONE\n"
    second = rig.act("undo", summary["set"])
    assert second.event["files"][0]["n"] == 2 and path.read_text() == "one\n"
    assert (folder / "0-undo-1").read_bytes() == changed and (folder / "0-undo-2").read_bytes() == changed
    rig.act("redo", summary["set"])
    assert path.read_bytes() == changed
    # 清单写好以后不再改："后来撤回过没有"只记在事件里
    assert rig.manifest(summary["set"]) == manifest


def test_undo_event_and_notes(rig):
    """file_undo 事件的内容：哪一轮、做了什么、每个文件第几次撤回和换下来的样子、给人看的路径；notes 是给模型的说明。"""
    edited, created = rig.work / "a.txt", rig.work / "b.txt"
    edited.write_text("one\n")
    summary = rig.turn(replace(edited, "one", "ONE"), create(created, "new\n"))
    edited_now, created_now = edited.read_bytes(), created.read_bytes()
    modes = [edited.stat().st_mode & 0o7777, created.stat().st_mode & 0o7777]

    undo = rig.act("undo", summary["set"])

    assert undo.event == {
        "set": summary["set"],
        "action": "undo",
        "files": [
            {"index": 0, "n": 1, "replaced": {"sha256": digest(edited_now), "size": len(edited_now), "mode": modes[0], "blob": True}},
            {"index": 1, "n": 1, "replaced": {"sha256": digest(created_now), "size": len(created_now), "mode": modes[1], "blob": True}},
        ],
        "paths": ["a.txt", "b.txt"],
    }
    assert undo.notes == [("a.txt", "undo"), ("b.txt", "delete")]
    redo = rig.act("redo", summary["set"], indices=[1])
    assert redo.event == {"set": summary["set"], "action": "redo", "files": [{"index": 1}], "paths": ["b.txt"]}
    assert redo.notes == [("b.txt", "redo")]


def test_state_is_folded_from_events(rig):
    """撤回了几个、全部撤回以后再撤、没撤回过就重做：都看事件推出来的状态，说清楚为什么没做。"""
    first, second = rig.work / "a.txt", rig.work / "b.txt"
    first.write_text("a\n")
    second.write_text("b\n")
    summary = rig.turn(replace(first, "a", "A"), replace(second, "b", "B"))
    set_id = summary["set"]

    assert rig.changes.applied(set_id, rig.events) == 2
    assert rig.changes.last_set(rig.events) == set_id
    assert rig.act("redo", set_id).error == t("这一轮没有撤回过的文件，不用重做。")
    rig.act("undo", set_id, indices=[0])
    assert rig.changes.applied(set_id, rig.events) == 1
    rig.act("undo", set_id)  # 没给 indices：剩下还没撤回的那一个
    assert first.read_text() == "a\n" and second.read_text() == "b\n"
    assert rig.changes.applied(set_id, rig.events) == 0
    assert rig.act("undo", set_id).error == t("这一轮的改动已经全部撤回了。")
    assert rig.changes.last_set([{"type": "user"}]) == ""


# ---- 冲突 --------------------------------------------------------------------


def test_file_changed_after_the_turn_asks_first(rig):
    """这一轮之后文件又被改过（哈希对不上）：撤回会把那些改动一起冲掉，所以先问，什么都不写；点"仍然撤回"（force）才做，
    而且把你后来的那一版留了底，重做能放回来。"""
    path = rig.work / "a.txt"
    path.write_text("one\n")
    summary = rig.turn(replace(path, "one", "ONE"))
    path.write_text("你后来手改的\n")

    asked = rig.act("undo", summary["set"])
    assert asked.event is None and asked.error == t("这几个文件要先确认一下再撤回。")
    assert bare(asked.asks) == [{"index": 0, "path": "a.txt", "reasons": ["changed"]}]
    assert asked.asks[0]["why"] == ["这一轮之后它又被改过（可能是你自己、执行的命令或者别的程序）：撤回会把那些改动一起冲掉。"]
    assert path.read_text() == "你后来手改的\n"
    assert "0-undo-1" not in names(rig.root / summary["set"])

    forced = rig.act("undo", summary["set"], force=True)
    assert forced.event is not None and path.read_text() == "one\n"
    rig.act("redo", summary["set"])
    assert path.read_text() == "你后来手改的\n"


def test_modified_file_deleted_after_the_turn_asks_missing(rig):
    """这一轮改过的文件后来被删了：撤回会把它建回来，先问（missing）。重做放回撤回时的样子，也就是"不在"。"""
    path = rig.work / "sub" / "a.txt"
    path.parent.mkdir()
    path.write_text("one\n")
    summary = rig.turn(replace(path, "one", "ONE"))
    path.unlink()
    path.parent.rmdir()

    asked = rig.act("undo", summary["set"])
    assert reasons(asked.asks) == [(0, ["missing"])] and not path.exists()

    forced = rig.act("undo", summary["set"], force=True)
    assert forced.event["files"][0]["replaced"] is None and path.read_text() == "one\n"
    rig.act("redo", summary["set"])
    assert not path.exists()


def test_created_file_already_gone_needs_no_question(rig):
    """这一轮新建的文件已经不在了：撤回本来就是把它删掉，没什么可冲掉的，不用问。"""
    path = rig.work / "new.txt"
    summary = rig.turn(create(path, "x\n"))
    path.unlink()

    result = rig.act("undo", summary["set"])
    assert result.asks == [] and result.blocked == [] and result.event["files"] == [{"index": 0, "n": 1, "replaced": None}]
    rig.act("redo", summary["set"])
    assert not path.exists()  # 撤回时它就不在，重做也让它不在


def test_later_turns_that_touched_the_file_are_named(rig):
    """后面的几轮也改过同一个文件：撤回的是"这一轮之前"的样子，后面那几轮的改动也会没了，另外说明（later，带着几轮）。
    先把后面那几轮撤回，这一轮就又干净了，不用问。"""
    path, other = rig.work / "a.txt", rig.work / "b.txt"
    path.write_text("v0\n")
    other.write_text("b\n")
    first = rig.turn(replace(path, "v0", "v1"))
    second = rig.turn(replace(path, "v1", "v2"), replace(other, "b", "B"))
    third = rig.turn(replace(path, "v2", "v3"))

    asked = rig.act("undo", first["set"])
    assert bare(asked.asks) == [{"index": 0, "path": "a.txt", "reasons": ["later"], "later": 2}]
    assert asked.asks[0]["why"] == ["后面还有 2 轮也改过它：撤回会把它恢复成这一轮之前的样子，后面那几轮对它的改动也会一起没了。"]
    assert path.read_text() == "v3\n"

    assert rig.act("undo", third["set"]).asks == []
    assert rig.act("undo", second["set"], indices=[0]).asks == []
    assert path.read_text() == "v1\n"
    clean = rig.act("undo", first["set"])
    assert clean.asks == [] and clean.event is not None and path.read_text() == "v0\n"
    assert other.read_text() == "B\n"  # 第二轮里另一个文件没撤回，不受影响


def test_redo_after_the_file_changed_again_asks(rig):
    """撤回以后文件又被改过：重做会把那些改动冲掉，同样先问（changed），force 才做。"""
    path = rig.work / "a.txt"
    path.write_text("one\n")
    summary = rig.turn(replace(path, "one", "ONE"))
    rig.act("undo", summary["set"])
    path.write_text("撤回以后又改的\n")

    asked = rig.act("redo", summary["set"])
    assert asked.error == t("这几个文件要先确认一下再重做。") and reasons(asked.asks) == [(0, ["changed"])]
    assert path.read_text() == "撤回以后又改的\n"
    rig.act("redo", summary["set"], force=True)
    assert path.read_text() == "ONE\n"


# ---- 不能碰的、做不了的 ------------------------------------------------------


def test_protected_file_in_a_tampered_manifest_is_refused(rig):
    """清单被改成指向 .env（文件工具的 protected 名单）：撤回不写它，"仍然撤回"也不写。撤回只写当初记下的位置，
    但当初记下的也可能被人动过，所以每次都再对一遍名单。"""
    path = rig.work / "a.txt"
    path.write_text("one\n")
    summary = rig.turn(replace(path, "one", "ONE"))
    rig.env.write_text("FAKE_KEY=not-a-real-key\n")

    def point_at_env(data):
        data["files"][0]["path"] = str(rig.env)

    rig.tamper(summary["set"], point_at_env)
    for force in (False, True):
        result = rig.act("undo", summary["set"], force=force)
        assert result.event is None and result.error == t("没有能撤回的文件。")
        assert bare(result.blocked) == [{"index": 0, "path": str(rig.env), "reasons": ["protected"]}]
    assert rig.env.read_text() == "FAKE_KEY=not-a-real-key\n"


def test_a_tampered_manifest_cannot_reach_env_by_another_spelling(rig, monkeypatch):
    """清单里换一种写法指向 .env（大小写不同；Windows 上还有数据流、尾巴上的点）：和文件工具一样认得出是同一个文件，不写。"""
    path = rig.work / "a.txt"
    path.write_text("one\n", encoding="utf-8")
    summary = rig.turn(replace(path, "one", "ONE"))
    rig.env.write_text("FAKE_KEY=not-a-real-key\n", encoding="utf-8")
    shouting = str(rig.env.with_name(".ENV"))

    def point_at_env(data):
        data["files"][0]["path"] = shouting

    rig.tamper(summary["set"], point_at_env)
    result = rig.act("undo", summary["set"], force=True)
    assert result.event is None and reasons(result.blocked) == [(0, ["protected"])]  # Linux 上 .ENV 是另一个文件，照样不写：只会多拦
    assert rig.env.read_text(encoding="utf-8") == "FAKE_KEY=not-a-real-key\n"
    # Windows 上的花招（数据流、设备名、尾巴上的点……）：清单是程序写的，不会有这些；有的话当作被人改过，不写（每个系统上都能测）
    monkeypatch.setattr(pathnames, "WINDOWS", True)
    for tricky in (str(rig.env) + "::$DATA", str(rig.work / "CON"), str(rig.work / "a.txt.")):
        rig.tamper(summary["set"], lambda data, tricky=tricky: data["files"][0].update(path=tricky))
        assert reasons(rig.act("undo", summary["set"], force=True).blocked) == [(0, ["moved"])], tricky
    assert rig.env.read_text(encoding="utf-8") == "FAKE_KEY=not-a-real-key\n"


def test_path_inside_the_snapshot_folder_is_refused(rig):
    """撤回从不往留底的文件夹里写（否则能拿一份留底去盖掉另一份，或者盖掉清单）。"""
    path = rig.work / "a.txt"
    path.write_text("one\n")
    summary = rig.turn(replace(path, "one", "ONE"))
    target = rig.root / summary["set"] / store.MANIFEST

    def point_inside(data):
        data["files"][0]["path"] = str(target)

    rig.tamper(summary["set"], point_inside)
    before = target.read_bytes()
    result = rig.act("undo", summary["set"], force=True)
    assert reasons(result.blocked) == [(0, ["protected"])] and target.read_bytes() == before


def test_path_that_became_a_symlink_is_moved(rig, tmp_path):
    """文件本身、或者它上面的文件夹，这一轮之后换成了符号链接：撤回不跟着链接去写别处（moved），"仍然撤回"也不写。"""
    if not can_symlink(tmp_path):
        pytest.skip("这台电脑上建不了符号链接（Windows 上要开发者模式或管理员）")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = elsewhere / "target.txt"

    # 文件换成了链接
    link = rig.work / "a.txt"
    link.write_text("one\n")
    summary = rig.turn(replace(link, "one", "ONE"))
    link.unlink()
    target.write_text("ONE\n")
    link.symlink_to(target)
    result = rig.act("undo", summary["set"], force=True)
    assert reasons(result.blocked) == [(0, ["moved"])] and target.read_text() == "ONE\n"

    # 上面的文件夹换成了链接
    folder = rig.work / "sub"
    folder.mkdir()
    (folder / "b.txt").write_text("bee\n")
    second = rig.turn(replace(folder / "b.txt", "bee", "BEE"))
    real = rig.work / "real-sub"
    folder.rename(real)
    folder.symlink_to(real, target_is_directory=True)
    result = rig.act("undo", second["set"], force=True)
    assert reasons(result.blocked) == [(0, ["moved"])] and (real / "b.txt").read_text() == "BEE\n"


def test_tampered_or_missing_snapshot_is_refused(rig):
    """留底被改过（哈希对不上）或者没有了：不拿一份不知道是什么的东西去盖你的文件（snapshot）。"""
    first, second = rig.work / "a.txt", rig.work / "b.txt"
    first.write_text("a\n")
    second.write_text("b\n")
    summary = rig.turn(replace(first, "a", "A"), replace(second, "b", "B"))
    folder = rig.root / summary["set"]
    (folder / "0-before").write_bytes(b"something else\n")
    (folder / "1-before").unlink()

    result = rig.act("undo", summary["set"], force=True)
    assert reasons(result.blocked) == [(0, ["snapshot"]), (1, ["snapshot"])]
    assert first.read_text() == "A\n" and second.read_text() == "B\n"


def test_redo_without_its_snapshot_is_refused(rig):
    """撤回时留的那份（<格>-undo-<n>）没有了：重做做不了（snapshot），文件保持撤回后的样子。"""
    path = rig.work / "a.txt"
    path.write_text("one\n")
    summary = rig.turn(replace(path, "one", "ONE"))
    rig.act("undo", summary["set"])
    (rig.root / summary["set"] / "0-undo-1").unlink()

    result = rig.act("redo", summary["set"], force=True)
    assert reasons(result.blocked) == [(0, ["snapshot"])] and path.read_text() == "one\n"


def test_already_undone_or_applied_single_file(rig):
    """指定的文件已经撤回过了（done）、还没撤回过就要重做（applied）：做不了，说清楚。"""
    first, second = rig.work / "a.txt", rig.work / "b.txt"
    first.write_text("a\n")
    second.write_text("b\n")
    summary = rig.turn(replace(first, "a", "A"), replace(second, "b", "B"))
    rig.act("undo", summary["set"], indices=[0])

    again = rig.act("undo", summary["set"], indices=[0])
    assert again.error == t("没有能撤回的文件。") and reasons(again.blocked) == [(0, ["done"])]
    redo = rig.act("redo", summary["set"], indices=[1])
    assert redo.error == t("没有能重做的文件。") and reasons(redo.blocked) == [(1, ["applied"])]


@pytest.mark.parametrize("set_id", ["nonsense", "20261121-120000-abcdef", None, 123, ""])
def test_unknown_set_is_an_error(rig, set_id):
    """编号格式不对、不是这段会话里的某一轮、根本不是字符串：一律"找不到"，什么都不做。"""
    path = rig.work / "a.txt"
    path.write_text("one\n")
    rig.turn(replace(path, "one", "ONE"))
    for action in ("undo", "redo"):
        result = rig.changes.act(action, set_id, rig.events)
        assert result.error == t(NOT_FOUND) and result.event is None
    assert rig.changes.diff(set_id, 0, rig.events)[0] == t(NOT_FOUND)
    assert path.read_text() == "ONE\n"


def test_bad_requests_are_errors(rig):
    """不认识的动作、超出范围的第几个、重复的、不是整数的：参数不对，什么都不做。"""
    path = rig.work / "a.txt"
    path.write_text("one\n")
    summary = rig.turn(replace(path, "one", "ONE"))
    assert rig.changes.act("delete", summary["set"], rig.events).error == t(NOT_FOUND)
    for indices in ([1], [-1], [0, 0], [True], ["0"], [0.0]):
        result = rig.changes.act("undo", summary["set"], rig.events, indices)
        assert result.error == t("参数不正确") and result.event is None, indices
    assert path.read_text() == "ONE\n"


def test_cleared_or_broken_manifest_is_gone(rig):
    """留底被清理掉、清单坏了或者格式不对（编号对不上、相对路径、重复的格子……）：都当作"不在了"，撤不回来。"""
    path = rig.work / "a.txt"
    path.write_text("one\n")
    summary = rig.turn(replace(path, "one", "ONE"))
    set_id = summary["set"]
    manifest_path = rig.root / set_id / store.MANIFEST
    good = json.loads(manifest_path.read_text(encoding="utf-8"))
    gone = t("这一轮的留底已经不在了（只保留 {days} 天），撤不回来了。", days=UNDO_KEEP_DAYS)

    def broken(change):
        data = json.loads(json.dumps(good))
        change(data)
        return json.dumps(data)

    entry = good["files"][0]
    for text in (
        "{not json",
        "[]",
        json.dumps({**good, "version": 2}),
        json.dumps({**good, "id": "20200101-000000-000000"}),
        broken(lambda data: data["files"][0].update(path="a.txt")),
        broken(lambda data: data["files"][0].update(kind="deleted")),
        broken(lambda data: data["files"][0].update(skipped="whatever")),
        broken(lambda data: data["files"][0].update(before=None)),  # 改过的文件没有"改之前"
        broken(lambda data: data["files"][0].update(dirs=["relative"])),
        broken(lambda data: data["files"][0]["before"].update(sha256="xyz")),
        broken(lambda data: data["files"][0]["before"].update(size=-1)),
        broken(lambda data: data["files"][0]["before"].update(mode=0o17777)),
        broken(lambda data: data["files"].append(dict(entry))),  # 两项用同一个格子
    ):
        manifest_path.write_text(text, encoding="utf-8")
        assert rig.manifest(set_id) is None, text
        assert rig.changes.act("undo", set_id, rig.events).error == gone
        assert rig.changes.diff(set_id, 0, rig.events)[0] == gone
    manifest_path.unlink()
    assert rig.changes.act("undo", set_id, rig.events).error == gone
    assert rig.changes.applied(set_id, rig.events) == 0
    assert path.read_text() == "ONE\n"


@posix
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root 不受文件夹权限的限制，造不出写不进去的情形")
def test_write_failure_is_reported_for_that_file_only(rig):
    """撤回到一半有一个文件写不进去（文件夹只读）：那一个报 failed（带着系统给的原因），别的照样做，事件里只记做成了的。
    之后能写了再撤回，照样能做。"""
    locked = rig.work / "locked"
    locked.mkdir()
    stuck, free = locked / "a.txt", rig.work / "b.txt"
    stuck.write_text("one\n")
    free.write_text("bee\n")
    summary = rig.turn(replace(stuck, "one", "ONE"), replace(free, "bee", "BEE"))

    locked.chmod(0o500)
    try:
        result = rig.act("undo", summary["set"])
    finally:
        locked.chmod(0o700)
    assert reasons(result.blocked) == [(0, ["failed"])] and result.blocked[0]["error"]
    assert [item["index"] for item in result.event["files"]] == [1]
    assert stuck.read_text() == "ONE\n" and free.read_text() == "bee\n"

    retry = rig.act("undo", summary["set"])
    assert retry.blocked == [] and stuck.read_text() == "one\n"


# ---- 工作目录之外、助手自己的数据目录 ----------------------------------------


def test_file_outside_the_workspace_asks(rig, tmp_path):
    """工作目录之外的文件：当初改它就要你点头，撤回时也一样，先说清楚是哪儿（outside），force 才做。"""
    path = tmp_path / "elsewhere" / "x.txt"
    path.parent.mkdir()
    path.write_text("one\n")
    summary = rig.turn(replace(path, "one", "ONE"))
    assert summary["files"][0]["path"] == str(path.resolve()) and summary["files"][0]["place"] == "outside"

    asked = rig.act("undo", summary["set"])
    assert reasons(asked.asks) == [(0, ["outside"])] and path.read_text() == "ONE\n"
    path.write_text("又改了\n")
    both = rig.act("undo", summary["set"])
    assert reasons(both.asks) == [(0, ["changed", "outside"])]
    rig.act("undo", summary["set"], force=True)
    assert path.read_text() == "one\n"


def test_file_in_the_private_data_dir_asks(rig):
    """助手自己的数据目录（data/）里的文件：撤回前同样先问（private）。"""
    path = rig.data / "notes.md"
    path.write_text("one\n")
    summary = rig.turn(replace(path, "one", "ONE"))
    assert summary["files"][0]["place"] == "private"

    asked = rig.act("undo", summary["set"])
    assert bare(asked.asks) == [{"index": 0, "path": str(path), "reasons": ["private"]}] and path.read_text() == "ONE\n"
    rig.act("undo", summary["set"], force=True)
    assert path.read_text() == "one\n"


# ---- 上限 --------------------------------------------------------------------


def test_big_file_is_changed_but_not_snapshotted(rig, monkeypatch):
    """超过一个文件的上限：照样改，只记大小和哈希（skipped=big），撤回时说做不了；预览说"看不了"。"""
    monkeypatch.setattr(journal_module, "UNDO_FILE_BYTES", 10)
    path = rig.work / "big.txt"
    original = b"x" * 30 + b"\n"
    path.write_bytes(original)
    summary = rig.turn(replace(path, "x" * 30, "y" * 30))

    assert summary["files"] == [{"path": "big.txt", "kind": "modified", "added": None, "removed": None, "skipped": "big"}]
    entry = rig.manifest(summary["set"])["files"][0]
    assert entry["skipped"] == "big" and entry["before"]["blob"] is False and entry["after"]["blob"] is False
    assert entry["before"]["sha256"] == digest(original) and entry["before"]["size"] == len(original)
    assert names(rig.root / summary["set"]) == ["manifest.json"]

    result = rig.act("undo", summary["set"], force=True)
    assert reasons(result.blocked) == [(0, ["big"])] and result.error == t("没有能撤回的文件。")
    assert path.read_text() == "y" * 30 + "\n"
    assert rig.changes.diff(summary["set"], 0, rig.events) == ("", {"rows": [], "more": 0, "binary": False, "unavailable": True})


def test_turn_budget_skips_later_files(rig, monkeypatch):
    """一轮的留底加起来超了上限：后面的文件只记哈希（skipped=budget），撤不回来；前面的照样能撤回。"""
    monkeypatch.setattr(journal_module, "UNDO_TURN_BYTES", 50)
    first, second = rig.work / "a.txt", rig.work / "b.txt"
    first.write_text("a" * 19 + "\n")
    second.write_text("b" * 19 + "\n")
    summary = rig.turn(replace(first, "a" * 19, "A" * 19), replace(second, "b" * 19, "B" * 19))

    assert [item.get("skipped", "") for item in summary["files"]] == ["", "budget"]
    asked = rig.act("undo", summary["set"])
    assert asked.event is None and reasons(asked.blocked) == [(1, ["budget"])]
    assert first.read_text() == "A" * 19 + "\n"  # 有做不了的，又不是 force：先不做，问你
    forced = rig.act("undo", summary["set"], force=True)
    assert [item["index"] for item in forced.event["files"]] == [0] and reasons(forced.blocked) == [(1, ["budget"])]
    assert first.read_text() == "a" * 19 + "\n" and second.read_text() == "B" * 19 + "\n"


def test_error_does_not_claim_nothing_is_undoable_when_something_is(rig, monkeypatch):
    """一个文件超了上限、另一个好好的：没加 force 时什么都没做，但给你的那句话不能说"没有能撤回的文件"。"""
    monkeypatch.setattr(journal_module, "UNDO_TURN_BYTES", 50)
    first, second = rig.work / "a.txt", rig.work / "b.txt"
    first.write_text("a" * 19 + "\n")
    second.write_text("b" * 19 + "\n")
    summary = rig.turn(replace(first, "a" * 19, "A" * 19), replace(second, "b" * 19, "B" * 19))

    asked = rig.act("undo", summary["set"])
    assert asked.event is None
    assert asked.error != t("没有能撤回的文件。")


def test_file_cap_counts_untracked(rig, monkeypatch):
    """一轮里记的文件超了个数上限：多的照样改，不记下来，页面上说还有几个没记下（untracked）。"""
    monkeypatch.setattr(journal_module, "UNDO_TURN_FILES", 2)
    paths = [rig.work / f"{name}.txt" for name in "abcd"]
    for path in paths:
        path.write_text("x\n")
    summary = rig.turn(*(replace(path, "x", "y") for path in paths))

    assert [item["path"] for item in summary["files"]] == ["a.txt", "b.txt"] and summary["untracked"] == 2
    assert all(path.read_text() == "y\n" for path in paths)
    rig.act("undo", summary["set"])
    assert [path.read_text() for path in paths] == ["x\n", "x\n", "y\n", "y\n"]


def test_only_untracked_files_still_reports_them(rig, monkeypatch):
    """一个都没记下（上限是 0）但确实改了：不能当作"什么都没改"，事件里说有几个没记下，编号是空的（没有可撤回的）。"""
    monkeypatch.setattr(journal_module, "UNDO_TURN_FILES", 0)
    path = rig.work / "a.txt"
    path.write_text("x\n")
    journal = rig.changes.start()
    with recording(journal):
        rig.editor.run(replace(path, "x", "y"))
    assert rig.changes.finish(journal) == {"set": "", "files": [], "untracked": 1}
    assert not (rig.root / journal.id).exists()


def test_failed_call_over_the_file_cap_is_not_counted(rig, monkeypatch):
    """超了个数上限以后，一次出错（什么都没改）的调用不该算成"还有 1 个文件没记下"。"""
    monkeypatch.setattr(journal_module, "UNDO_TURN_FILES", 1)
    first, second = rig.work / "a.txt", rig.work / "b.txt"
    first.write_text("x\n")
    second.write_text("keep\n")
    summary = rig.turn(replace(first, "x", "y"), replace(second, "not there", "z"))

    assert second.read_text() == "keep\n"
    assert summary["untracked"] == 0


def test_diff_unavailable_when_snapshots_are_missing_or_too_big(rig, monkeypatch):
    """预览要两份留底都在、都不太大：改之后的那份没了，或者超了 UNDO_DIFF_BYTES，只说改了，看不到改了什么（unavailable）。"""
    path = rig.work / "a.txt"
    path.write_text("one\n")
    summary = rig.turn(replace(path, "one", "ONE"))
    unavailable = ("", {"rows": [], "more": 0, "binary": False, "unavailable": True})

    monkeypatch.setattr(changes_module, "UNDO_DIFF_BYTES", 2)
    assert rig.changes.diff(summary["set"], 0, rig.events) == unavailable
    monkeypatch.undo()
    assert rig.changes.diff(summary["set"], 0, rig.events)[1]["rows"]  # 上限放回去就能看
    (rig.root / summary["set"] / "0-after").unlink()
    assert rig.changes.diff(summary["set"], 0, rig.events) == unavailable
    assert rig.act("undo", summary["set"]).event is not None  # 撤回不用"改之后"那份，照样能做


def test_diff_of_a_turn(rig):
    """展开"改了 N 个文件"时看的预览：改之前 → 这一轮改完，按行；新建的文件全是加上的行。参数不对时说清楚。"""
    edited, created = rig.work / "a.txt", rig.work / "b.txt"
    edited.write_text("1\n2\n3\n")
    summary = rig.turn(replace(edited, "2", "two"), create(created, "x\ny\n"))

    assert rig.changes.diff(summary["set"], 0, rig.events) == ("", {
        "binary": False, "more": 0,
        "rows": [["@", 1, 1, ""], [" ", 1, 1, "1"], ["-", 2, 0, "2"], ["+", 0, 2, "two"], [" ", 3, 3, "3"]],
    })  # fmt: skip
    assert rig.changes.diff(summary["set"], 1, rig.events) == ("", {
        "binary": False, "more": 0, "rows": [["@", 1, 1, ""], ["+", 0, 1, "x"], ["+", 0, 2, "y"]],
    })  # fmt: skip
    for index in (2, -1, True, "0", None):
        assert rig.changes.diff(summary["set"], index, rig.events) == (t("参数不正确"), {})


# ---- 存盘、重新打开以后 ------------------------------------------------------


def test_undo_and_redo_survive_saving_and_reopening(rig, tmp_path):
    """事件存进会话文件、读回来（ConversationStore 真的写磁盘）、换一个新的 FileChanges（程序重启过）：
    照样能撤回和重做，"现在是什么状态"全从读回来的事件里推出来。"""
    edited, created = rig.work / "a.txt", rig.work / "new" / "b.txt"
    edited.write_text("one\n")
    summary = rig.turn(replace(edited, "one", "ONE"), create(created, "new\n"))

    folder = tmp_path / "conversations"
    conversation = ConversationStore(folder).new("anthropic", "claude-opus-5-5")
    conversation.events = [{"type": "user", "text": "改一下 a.txt"}, *rig.events]
    ConversationStore(folder).save(conversation)

    def reopen():
        loaded = ConversationStore(folder).load(conversation.id)
        editor = FileEditor(rig.work, rig.data / "backups", protected=[rig.env])
        return loaded, FileChanges(rig.root, editor, [rig.data])

    loaded, changes = reopen()
    assert changes.applied(summary["set"], loaded.events) == 2
    undo = changes.act("undo", summary["set"], loaded.events)
    assert undo.event is not None and edited.read_text() == "one\n" and not created.exists()
    loaded.events.append({"type": "file_undo", **undo.event})
    ConversationStore(folder).save(loaded)

    loaded, changes = reopen()
    assert changes.applied(summary["set"], loaded.events) == 0
    assert changes.act("undo", summary["set"], loaded.events).error == t("这一轮的改动已经全部撤回了。")
    redo = changes.act("redo", summary["set"], loaded.events)
    assert redo.event is not None and edited.read_text() == "ONE\n" and created.read_text() == "new\n"


# ---- 只记你这一轮的 ----------------------------------------------------------


def test_edits_from_another_thread_are_not_recorded(rig):
    """账记在这一轮的线程上（ContextVar 不会带进新开的线程）：同一时间别的线程（定时任务、聊天通道）用同一个文件工具改的文件不记。"""
    mine, theirs = rig.work / "mine.txt", rig.work / "theirs.txt"
    mine.write_text("m\n")
    theirs.write_text("t\n")
    errors: list[BaseException] = []

    def other_run():
        try:
            rig.editor.run(replace(theirs, "t", "T"))
        except (ToolError, OSError) as exc:  # 线程里的异常不会自己冒出来，交回主线程再判
            errors.append(exc)

    journal = rig.changes.start()
    with recording(journal):
        rig.editor.run(replace(mine, "m", "M"))
        worker = threading.Thread(target=other_run)
        worker.start()
        worker.join()
    summary = rig.changes.finish(journal)

    assert errors == [] and theirs.read_text() == "T\n"
    assert [item["path"] for item in summary["files"]] == ["mine.txt"]


def test_without_a_journal_watching_does_nothing(rig):
    """没有在记账（定时任务、聊天通道、直接调用工具）：watching 什么都不做，文件工具照常工作，也不建留底的文件夹。
    账合上以后（离开 recording）再改的也不记。"""
    path = rig.work / "a.txt"
    rig.editor.run(create(path, "one\n"))
    rig.editor.run(replace(path, "one", "ONE"))
    with watching(path, "create"):
        pass
    assert path.read_text() == "ONE\n" and not rig.root.exists()

    journal = rig.changes.start()
    with recording(journal):
        rig.editor.run(insert(path, 0, "top"))
    end_of_turn = path.read_bytes()
    rig.editor.run(replace(path, "ONE", "after the turn"))
    summary = rig.changes.finish(journal)
    assert summary["files"][0]["added"] == 1
    assert (rig.root / summary["set"] / "0-after").read_bytes() == end_of_turn


# ---- 清理过期的留底 ----------------------------------------------------------


def test_prune_removes_only_old_folders_with_valid_names(tmp_path):
    """按文件夹名里的日期删超过天数的（不看修改时间）；名字不像编号的、日期不合法的、不是文件夹的、符号链接都不动。"""
    root = tmp_path / "turns"
    root.mkdir()
    old = root / "20260101-080000-abcdef"
    recent = root / "20261110-080000-123456"
    odd = root / "not-a-turn"
    bad_date = root / "20261399-000000-abcdef"
    for folder in (old, recent, odd, bad_date):
        folder.mkdir()
        (folder / "manifest.json").write_text("{}")
    plain_file = root / "20250101-000000-fedcba"
    plain_file.write_text("not a folder")
    target = tmp_path / "keep-me"
    target.mkdir()
    (target / "precious.txt").write_text("!")
    link = root / "20250101-000000-aaaaaa"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        link = None

    now = datetime(2026, 11, 21, 12, 0).timestamp()
    assert ChangeStore(root).prune(30, now=now) == 1
    assert not old.exists()
    assert recent.is_dir() and odd.is_dir() and bad_date.is_dir() and plain_file.is_file()
    assert (target / "precious.txt").exists()
    if link is not None:
        assert link.is_symlink()
    assert ChangeStore(tmp_path / "nowhere").prune(30, now=now) == 0


def test_folder_and_store_helpers_refuse_bad_ids(tmp_path):
    """留底的文件名全由程序拼：编号格式不对的一律当作不存在，读写都出不了留底的文件夹。"""
    changes = ChangeStore(tmp_path / "turns")
    assert store.ID.fullmatch(changes.new_id())
    for bad in ("../x", "20261121-120000-ABCDEF", "", None, 5):
        assert changes.folder(bad) is None and changes.manifest(bad) is None
    assert changes.get("../../etc", "passwd") is None
    with pytest.raises(ValueError):
        changes.put("../escape", "0-before", b"x")
    assert not (tmp_path / "escape").exists()


# ---- 告诉模型 ----------------------------------------------------------------


def test_notes_are_settled_only_when_sent():
    """撤回、重做以后附在下一条消息前面的说明：发出去（sent）才算说过；没发出去的下一次再附；按会话分开。"""
    notes = Notes()
    assert notes.text("A") == ""
    notes.add("A", [("a.txt", "undo"), ("b.txt", "delete"), ("c.txt", "unknown")])
    notes.add("B", [("x.txt", "redo")])

    text = notes.text("A")
    assert text.startswith("<file_changes>\n") and text.endswith("\n</file_changes>")
    assert "- a.txt：恢复成了那一轮改之前的内容" in text and "- b.txt：删掉了（它是那一轮新建的）" in text
    assert "c.txt" not in text and "x.txt" not in text  # 不认识的说法不写；别的会话的不写
    assert notes.pending("A") == 2

    notes.settle("A", sent=False)  # 那条消息没留在历史里：下次再附
    assert notes.text("A") == text
    notes.add("A", [("d.txt", "redo")])  # 附上之后、这一轮结束之前又撤回了一个：它还没说过
    notes.settle("A", sent=True)
    assert notes.pending("A") == 1 and "d.txt" in notes.text("A") and "a.txt" not in notes.text("A")
    notes.settle("A", sent=True)
    assert notes.text("A") == "" and notes.pending("A") == 0
    assert "- x.txt：重做了：又恢复成撤回之前的内容" in notes.text("B")


def test_notes_about_a_rewound_turn_are_dropped():
    """撤回了最后一轮改的文件、又把这一轮倒回去（重新生成、编辑）：模型从来没看到过那一轮，关于它的那几句不用再说；
    别的几轮的照样留着。"""
    notes = Notes()
    notes.add("A", [("old.txt", "undo")], "set-old")
    notes.add("A", [("new.txt", "delete")], "set-last")
    notes.text("A")  # 附上过一次、还没确认：drop 以后从头算
    notes.drop("A", ["set-last"])
    assert notes.pending("A") == 1 and "old.txt" in notes.text("A") and "new.txt" not in notes.text("A")
    notes.drop("A", ["set-old"])
    assert notes.pending("A") == 0 and notes.text("A") == ""
    notes.settle("A", sent=True)  # 什么都没附上：不会删掉别的
    assert notes.pending("A") == 0


def test_notes_list_at_most_the_line_cap(monkeypatch):
    """一次撤回了很多文件：说明最多列 UNDO_NOTE_LINES 个，再多的合成一句"还有几个"，免得把消息撑满。"""
    monkeypatch.setattr(notes_module, "UNDO_NOTE_LINES", 2)
    notes = Notes()
    notes.add("A", [(f"{index}.txt", "undo") for index in range(5)])
    text = notes.text("A")
    assert "- 0.txt" in text and "- 1.txt" in text and "- 2.txt" not in text
    assert "- ……还有 3 个文件" in text
    notes.settle("A", sent=True)
    assert notes.pending("A") == 0  # 合起来的那几个也算说过了


def test_undo_result_feeds_the_notes(rig):
    """act 交回的 notes 直接交给 Notes：给模型的那段话里写的是给人看的路径和做了什么。"""
    edited, created = rig.work / "a.txt", rig.work / "b.txt"
    edited.write_text("one\n")
    summary = rig.turn(replace(edited, "one", "ONE"), create(created, "x\n"))
    rig.changes.notes.add("conv", rig.act("undo", summary["set"]).notes)
    rig.changes.notes.add("conv", rig.act("redo", summary["set"], indices=[0]).notes)
    text = rig.changes.notes.text("conv")
    assert "- a.txt：恢复成了那一轮改之前的内容" in text
    assert "- b.txt：删掉了（它是那一轮新建的）" in text
    assert "- a.txt：重做了：又恢复成撤回之前的内容" in text
