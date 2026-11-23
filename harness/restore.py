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

## 由几个文件拼成

  restore.py         这个文件：真的恢复（apply、Restored：覆盖、合并、出错换回去）和恢复前的自动备份（backup_first）
  restore_check.py   check：看一遍压缩包（上面那几样查什么都在那里；上限 config.RESTORE_* 也在那里读）
  restore_plan.py    plan、preview：现在的数据文件夹里有什么、两种方式各会怎样，每一样写成一句话（step_text）
  restore_keys.py    存着密钥的配置文件：占位符不盖掉现在的密钥

三步一步用一步：restore_plan 用 restore_check 的，这个文件用前两个的，前两个都不 import 这个文件。
别处照旧 from .restore import …（__all__）；测试要换某个名字，换在读它的那个文件上（比如上限换 restore_check 的）。
"""

from __future__ import annotations

import io
import os
import re
import secrets
import shutil
import time
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from . import export, pathnames
from .config import (
    RESTORE_BACKUPS_DIR,
    RESTORE_KEEP_BACKUPS,
)
from .export import STAGING_PREFIX
from .i18n import t
from .restore_check import (
    _ZIP_ERRORS,
    COMPRESSIONS,
    SMALL,
    Archive,
    Entry,
    RestoreError,
    _reason,
    check,
    size_text,
)
from .restore_plan import (
    ACTIONS,
    KEY_HOMES,
    MODES,
    PLAIN,
    Plan,
    Step,
    _target,
    count_text,
    plan,
    preview,
    step_text,
)
from .safe import private_dir, write_stream

__all__ = [
    "ACTIONS",
    "BEFORE",
    "COMPRESSIONS",
    "KEY_HOMES",
    "MODES",
    "PLAIN",
    "SMALL",
    "Archive",
    "Entry",
    "Plan",
    "RestoreError",
    "Restored",
    "Step",
    "apply",
    "backup_first",
    "backups_folder",
    "check",
    "count_text",
    "plan",
    "preview",
    "size_text",
    "step_text",
]

# 恢复前的自动备份（在 data/restore-backups/ 里）的文件名。页面上"在访达中显示"只认这个样子、而且真的在那儿的文件
BEFORE = re.compile(r"zhifox-before-restore-\d{8}-\d{6}(-\d{1,3})?\.zip")


# ---- 恢复（第三步；前两步在 restore_check.py、restore_plan.py）--------------------


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
