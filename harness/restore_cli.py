"""终端里的 harness restore：没有网页的时候（服务器上）从备份恢复。

    harness restore 备份.zip            覆盖：数据文件夹变成备份时的样子（存着密钥的配置文件留着现在的密钥）
    harness restore 备份.zip --merge    合并：只把现在没有的会话、记忆、Agent、Skill……加进来，现在有的都留着
    harness restore 备份.zip --yes      不问，直接恢复（没有终端、在脚本里运行时必须带上）

和网页上的"从备份恢复…"是同一套检查和做法（见 restore.py）：先打印预览，在终端里输入 yes 才恢复，恢复之前先把现在的数据
自动备份一份。智狐开着的时候不能用：先占住数据目录的锁（instance.py），恢复完才放开，这期间谁也开不了智狐；
占不到就说清楚是谁占着——它正开着的话，用它网页上的那个按钮，那边会先停下定时任务和聊天通道。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import IO

from . import instance, restore
from .config import APP_NAME, Config
from .evals.cli import YES
from .i18n import t
from .restore import RestoreError, size_text, step_text


def add_parser(commands: argparse._SubParsersAction) -> None:
    """在 harness 的命令行里加上 restore 这个命令。"""
    parser = commands.add_parser("restore", help=t("从备份恢复：先给你看里面有什么，输入 yes 才恢复（恢复之前会把现在的数据自动备份一份）"))
    parser.add_argument("archive", metavar="ZIP", type=Path, help=t("导出的备份（zhifox-backup-….zip）"))
    parser.add_argument("--merge", action="store_true", help=t("合并：只把现在没有的会话、记忆、Agent、Skill……加进来，现在有的都留着"))
    parser.add_argument("--yes", action="store_true", help=t("不问，直接恢复（没有终端时必须带上）"))


def run(args: argparse.Namespace, config: Config, out: IO[str] | None = None, ask: Callable[[str], str] = input, interactive: bool | None = None) -> int:
    out = out or sys.stdout
    interactive = sys.stdin.isatty() if interactive is None else interactive
    path = args.archive.expanduser()
    if not path.is_file():
        print(t("找不到这个文件：{path}", path=path), file=sys.stderr)
        return 2
    mode = "merge" if args.merge else "overwrite"
    lock = instance.claim(config.data_dir, "restore")
    if lock is None:
        for line in instance.busy_lines(config.data_dir):
            print(line, file=sys.stderr)
        print(t("{name}开着的时候，在它的网页上恢复：设置 → 从备份恢复…", name=APP_NAME), file=sys.stderr)
        return 1
    try:
        archive = restore.check(path)
        for line in preview_lines(archive, restore.plan(archive, config.data_dir, mode), path, config.data_dir):
            print(line, file=out)
        if not _confirmed(args, out, ask, interactive):
            return 1
        done = restore.apply(archive, config.data_dir, mode, [config.env_file], datetime.now())
        done.commit()
    except RestoreError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        lock.close()
    for line in result_lines(done):
        print(line, file=out)
    return 0


def _confirmed(args: argparse.Namespace, out: IO[str], ask: Callable[[str], str], interactive: bool) -> bool:
    if args.yes:
        return True
    if not interactive:
        print(t("没有终端可以问你，所以没有恢复。看过上面的预览、确认要恢复的话，加上 --yes 再运行一次。"), file=out)
        return False
    try:
        answer = ask(t("确认恢复吗？输入 yes 继续，别的都是取消："))
    except (EOFError, KeyboardInterrupt):  # Ctrl-D、Ctrl-C 都当作"不"
        print(file=out)
        answer = ""
    if answer.strip().lower() in YES:
        return True
    print(t("已取消，什么都没有改。"), file=out)
    return False


def preview_lines(archive: restore.Archive, plan: restore.Plan, path: Path, data_dir: Path) -> list[str]:
    """恢复之前打印的预览：哪个备份、什么时候导出的、每一样东西会怎样、哪些密钥要重新填。"""
    lines = [t("备份：{path}（解开以后 {size}，{n} 个文件）", path=path, size=size_text(archive.total), n=len(archive.entries))]
    if archive.exported_at is not None:
        lines.append(t("导出时间：{time}", time=f"{archive.exported_at.astimezone():%Y-%m-%d %H:%M}"))
    else:
        lines.append(t("看不出是什么时候导出的：压缩包里没有导出时写的说明，可能不是智狐导出的。"))
    lines.append(t("方式：{mode}", mode=mode_text(plan.mode)))
    lines.append("")
    for step in plan.steps:
        if step.part.label:
            lines.append(f"  {t(step.part.label)}：{step_text(step.summary())}")
    lines += key_lines(plan.keys)
    lines += ["", t("恢复之前会把现在的数据自动备份一份，放在 {path}。", path=restore.backups_folder(data_dir))]
    return lines


def mode_text(mode: str) -> str:
    if mode == "merge":
        return t("合并：只把现在没有的加进来，现在有的都留着")
    return t("覆盖：数据文件夹变成备份时的样子（存着密钥的配置文件留着现在的密钥）")


def key_lines(keys: list[dict]) -> list[str]:
    """存着密钥的配置文件各会怎样。"""
    if not keys:
        return []
    lines = ["", t("密钥：")]
    for item in keys:
        where = f"{item['file']}（{t(item['home'])}）" if item["home"] else item["file"]
        if item["state"] == "broken":
            lines.append("  " + t("{file}：现在的这一份读不懂，没有动它（覆盖了就再也找不回里面的密钥）。", file=where))
            continue
        if item["kept"]:
            lines.append("  " + t("{file}：{n} 处沿用现在的密钥。", file=where, n=item["kept"]))
        if item["missing"]:
            lines.append("  " + t("{file}：这几处要重新填：", file=where))
            lines += [f"      {path}" for path in item["missing"]]
        if item["dropped"]:
            lines.append("  " + t("{file}：现在有、备份里没有的这几处密钥，恢复以后就没了（自动备份里也是遮住的）：", file=where))
            lines += [f"      {path}" for path in item["dropped"]]
    return lines


def result_lines(done: restore.Restored) -> list[str]:
    lines = ["", t("恢复好了。")]
    if done.backup is not None:
        lines.append(t("恢复之前的数据备份在：{path}", path=done.backup))
    missing = [item for item in done.keys if item["missing"]]
    if missing:
        lines.append(t("还有密钥要重新填：{files}。", files="、".join(item["file"] for item in missing)))
    lines.append(t("现在可以启动{name}了。", name=APP_NAME))
    return lines
