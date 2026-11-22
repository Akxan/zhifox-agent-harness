"""同一个数据目录，同一时间只给一个智狐用：占住它、看一眼有没有人占着、是谁占着。

为什么只能有一个：两个同时运行的程序各自在内存里记着会话列表，看不见对方的改动。同一段会话，后存盘的会把
先存的那一轮盖掉；一边删掉的会话，另一边一存盘又冒出来。聊天通道也一样：同一个机器人两处同时收消息，
一条消息会被回两次。

怎么占：数据目录里有一个 .lock 文件，谁用操作系统的文件锁锁住它，谁就占着（macOS、Linux 是 flock，Windows 是
LockFile，见 filelock.py）。锁由操作系统管着，程序退出时（哪怕是崩溃、被 kill -9、被任务管理器结束）自动放开，
不会出现"程序没了、锁还在"的情况。

是谁占着：占到以后，往 .lock 里写一行 {"pid": 进程编号, "kind": 哪一种}。别人占不到的时候读它，
就能说清楚是谁占着、该怎么办（"智狐正作为后台服务运行：打开它的网页用 harness service open"），
而不是只说一句"已经有一个在运行"。这一行只是给人看的说明，有没有人占着只看锁：
程序崩溃以后文件里还留着旧的那一行，但锁已经放开了，下一个占到的人会把它换掉。

kind 有这几种：
  web       harness --web（在终端里开的网页界面）
  serve     harness serve（在终端里、或者 systemd 的系统服务里开的）
  service   harness service install 装的后台服务（launchd、systemd 的用户服务，或者 Windows 任务计划程序里的任务）
  channels  harness channels pair（智狐没开着的时候，命令行自己临时连上平台收配对码）
  restore   harness restore（从备份恢复：恢复完才放开，这期间谁也开不了智狐，见 restore_cli.py）
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from . import filelock
from .config import APP_NAME, COMMAND
from .i18n import t
from .safe import private_dir

LOCK_NAME = ".lock"
KINDS = ("web", "serve", "service", "channels", "restore")


@dataclass(frozen=True)
class Holder:
    """占着数据目录的那个程序。"""

    pid: int
    kind: str


def claim(data_dir: Path, kind: str = "web") -> IO | None:
    """占住数据目录。占到了返回锁文件（要一直留着不关，关掉就放开了），已经被别人占了返回 None。

    数据目录还没有的话，在这里建好，和别处一样只有你的账号能进（safe.private_dir）：网页界面、harness serve 启动时
    第一个碰数据目录的就是这里。装成工具时（见 install.py）它是一个全新的文件夹，沿途还可能要建 ~/.local/share 这样的上级。"""
    if not data_dir.is_dir():
        private_dir(data_dir)
    handle = open(data_dir / LOCK_NAME, "a+")  # noqa: SIM115 —— 故意不用 with：这个文件要开着直到程序退出
    try:
        filelock.lock(handle, wait=False)
    except filelock.Busy:
        handle.close()
        return None
    except OSError:
        return handle  # 少数文件系统（比如某些网络盘）不支持这种锁：那就不拦了，照常启动
    try:
        handle.truncate(0)  # 换掉上一个人留下的那一行（打开方式是追加，写的位置总在末尾，也就是开头）
        handle.write(json.dumps({"pid": os.getpid(), "kind": kind}) + "\n")
        handle.flush()
    except OSError:
        pass  # 写不进去只是少了说明，锁照样是占着的
    return handle


def in_use(data_dir: Path) -> bool:
    """有没有别的程序占着这个数据目录。只是看一眼：试着锁一下，马上放开，不改文件里的内容。"""
    path = data_dir / LOCK_NAME
    if not path.is_file():
        return False
    try:
        with open(path, "a") as handle:  # 追加着打开：不会清掉里面"是谁占着"的那一行（Windows 上加锁要能写的打开方式）
            return filelock.held_elsewhere(handle)
    except OSError:
        return False


def holder(data_dir: Path) -> Holder | None:
    """占着数据目录的是谁（读 .lock 里的那一行）。只在 in_use 为真、或者 claim 没占到时才有意义；
    旧版本的智狐不写这一行，那时返回 None。"""
    try:
        data = json.loads((data_dir / LOCK_NAME).read_text(encoding="utf-8").strip() or "null")
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("pid"), int) or data.get("kind") not in KINDS:
        return None
    return Holder(data["pid"], data["kind"])


def busy_lines(data_dir: Path) -> list[str]:
    """数据目录被占着、这一次启动不了的时候，说清楚是谁占着、要怎么办（几行话，按顺序打印或者写进日志）。"""
    found = holder(data_dir)
    if found is not None and found.kind == "service":
        return [
            t("{name}正在作为后台服务运行（进程 {pid}），用的是同一个数据目录：{path}", name=APP_NAME, pid=found.pid, path=data_dir),
            t("打开它的网页：{command} service open。想在这里另开一个，先停下服务：{command} service stop。", command=COMMAND),
        ]
    if found is not None and found.kind == "channels":
        return [
            t("harness channels pair 正在等配对码（进程 {pid}），占着这个数据目录：{path}", pid=found.pid, path=data_dir),
            t("配对成功或者在它那里按 Ctrl-C 之后，再启动。"),
        ]
    if found is not None and found.kind == "restore":
        return [
            t("harness restore 正在从备份恢复（进程 {pid}），占着这个数据目录：{path}", pid=found.pid, path=data_dir),
            t("等它恢复完再启动。"),
        ]
    if found is not None and found.kind == "serve":
        return [
            t("已经有一个{name}在运行，用的是同一个数据目录：{path}", name=APP_NAME, path=data_dir),
            t("是在终端里或者 systemd 里开着的 harness serve（进程 {pid}）。先把它停下：在它的终端里按 Ctrl-C，或者 systemctl stop。", pid=found.pid),
        ]
    lines = [t("已经有一个{name}的网页界面在运行，用的是同一个数据目录：{path}", name=APP_NAME, path=data_dir)]
    if found is not None:
        lines.append(t("占着它的是进程 {pid}。", pid=found.pid))
    lines.append(t("两个一起运行会互相覆盖对方保存的会话。请直接用已经打开的那个页面，或者先到它的终端里按 Ctrl-C 把它关掉。"))
    return lines
