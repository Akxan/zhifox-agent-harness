"""macOS：用 launchd 的 LaunchAgent 把智狐装成后台服务。

LaunchAgent 是"你登录以后、以你的身份运行的后台程序"（LaunchDaemon 才是开机就以 root 运行的，我们用不着它）。
每个 LaunchAgent 是 ~/Library/LaunchAgents/ 里的一个 plist 文件（XML 写的设置），这几项决定了它的样子：

    Label                名字，倒过来写的域名（io.github.akxan.zhifox），launchctl 用它指代这个服务
    ProgramArguments     要运行的命令，一个参数一项。launchd 直接执行它，不经过 shell
    WorkingDirectory     从哪个文件夹开始运行（工作目录）
    EnvironmentVariables 环境变量（见 plan.py：只有路径和开关，没有密钥）
    RunAtLoad            launchd 读到这个文件就开起来：登录时，和装好的那一刻
    KeepAlive            退出了要不要再开。{SuccessfulExit: false} 是"不是正常退出才再开"：崩溃了、被 kill -9 了、
                         出错退出（退出码不是 0）都会被重新开起来；你让它停下时，它收到 SIGTERM、收好尾、
                         以退出码 0 退出（见 serve.py），就不会马上又被开起来
    ThrottleInterval     两次启动之间最少隔多少秒（见 config.SERVICE_RESTART_SECONDS）
    StandardOutPath、StandardErrorPath  输出写到哪个文件。launchd 只管写，不管它长多大，
                         所以智狐自己按大小换新文件（见 log.py 的 ServiceLog）

launchctl 的几条命令（macOS 10.10 起的写法，更老的系统用 load / unload / start）：

    bootstrap gui/<uid> <plist>        读进这个文件（RunAtLoad 让它马上开起来）
    bootout gui/<uid>/<label>          停下并卸下。文件还在，下次登录又会读进来
    kickstart [-k] gui/<uid>/<label>   现在就开起来；-k 是正在运行的话先停下再开，也就是重启
    enable gui/<uid>/<label>           解除"禁用"（有人用 launchctl disable 禁用过的话，bootstrap 会失败）
    list                               每个服务一行：进程编号、上一次的退出状态、名字

gui/<uid> 是"你登录的这个图形界面会话"，uid 是你的账号的编号（id -u）。
"""

from __future__ import annotations

import os
import platform
import plistlib
import re
from pathlib import Path
from typing import IO

from ..config import SERVICE_RESTART_SECONDS
from ..safe import private_dir, write_file
from . import logs
from .base import Result, Runner, State
from .plan import Plan, from_arguments

LABEL = "io.github.akxan.zhifox"
LAUNCHCTL = "/bin/launchctl"  # 写完整的路径：服务的 PATH 是装的时候抄的，不让它决定运行哪个 launchctl
_NUMBER = re.compile(r"-?\d+")


def render(plan: Plan) -> bytes:
    """这个服务的 plist。键按字母排（plistlib 默认如此），同样的设置写出来一字不差，重装时才能看出"没有变"。"""
    log = str(plan.log_file)
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": plan.arguments("launchd"),
            "WorkingDirectory": str(plan.workspace),
            "EnvironmentVariables": plan.environment(),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": SERVICE_RESTART_SECONDS,
            "StandardOutPath": log,
            "StandardErrorPath": log,
        },
        fmt=plistlib.FMT_XML,
    )


def parse(data: bytes) -> Plan | None:
    """从 plist 读回 Plan。不是我们写的样子（手改坏了）时返回 None。"""
    try:
        info = plistlib.loads(data)
    except Exception:  # noqa: BLE001 —— plistlib 坏文件时抛的异常种类很多（ExpatError、ValueError、InvalidFileException……）
        return None
    if not isinstance(info, dict):
        return None
    return from_arguments(info.get("ProgramArguments"), info.get("WorkingDirectory"), info.get("EnvironmentVariables"))


def _mac_version() -> tuple[int, ...]:
    numbers = [int(part) for part in platform.mac_ver()[0].split(".") if part.isdigit()]
    return tuple(numbers) or (99,)  # 读不出来就当作新系统


class Launchd:
    """macOS 的服务管家。runner 运行 launchctl（测试换成假的，见 base.py）。"""

    name = "launchd"

    def __init__(self, home: Path, runner: Runner, uid: int | None = None, version: tuple[int, ...] | None = None):
        self.file = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        self.run = runner
        # launchd 只在 macOS 上用（见 service/cli.py 的 detect），Windows 上没有 os.getuid：mypy 按 Windows 查时不知道这一点
        self.uid = os.getuid() if uid is None else uid  # type: ignore[attr-defined, unused-ignore]  # 见上一行
        self.legacy = (version or _mac_version()) < (10, 10)  # 10.10 以前没有 bootstrap / bootout / kickstart
        self.domain = f"gui/{self.uid}"
        self.target = f"{self.domain}/{LABEL}"

    # ---- 文件 --------------------------------------------------------------

    render = staticmethod(render)

    def read(self) -> bytes | None:
        try:
            return self.file.read_bytes()
        except OSError:
            return None

    def read_plan(self) -> Plan | None:
        data = self.read()
        return parse(data) if data is not None else None

    def write(self, plan: Plan, content: bytes) -> None:
        """写 plist（权限 644：launchd 要求它不能被别人改，读是可以的；里面没有密钥）。先建好日志的文件夹：
        launchd 在开程序之前就要打开 StandardOutPath，文件夹不在的话程序根本开不起来。"""
        private_dir(plan.log_file.parent)
        self.file.parent.mkdir(parents=True, exist_ok=True)
        write_file(self.file, content)
        self.file.chmod(0o644)

    def delete(self) -> None:
        self.file.unlink(missing_ok=True)

    # ---- 状态 --------------------------------------------------------------

    def state(self) -> State:
        """launchctl list 的表格里找自己那一行：第一列是进程编号（没在运行是 -），第二列是上一次的退出状态
        （负数是结束它的信号，-15 就是 SIGTERM），第三列是名字。"""
        installed = self.file.is_file()
        listed = self.run([LAUNCHCTL, "list"])
        if listed.ok:
            for line in listed.out.splitlines():
                parts = line.split("\t") if "\t" in line else line.split()
                if len(parts) < 3 or parts[2].strip() != LABEL:
                    continue
                pid = int(parts[0]) if parts[0].strip().isdigit() else None
                status = int(parts[1]) if _NUMBER.fullmatch(parts[1].strip()) else None
                return State(
                    installed, loaded=True, running=pid is not None, pid=pid, enabled=installed,
                    exit_code=status if status is not None and status >= 0 else None,
                    signal=-status if status is not None and status < 0 else None,
                )
        return State(installed, enabled=installed)

    def stoppable(self, state: State) -> bool:
        return state.loaded

    # ---- 开、停 ------------------------------------------------------------

    def load(self) -> Result:
        """读进 plist：RunAtLoad 让它马上开起来。"""
        if self.legacy:
            return self.run([LAUNCHCTL, "load", str(self.file)])
        self.run([LAUNCHCTL, "enable", self.target])  # 没被禁用过时什么都不做；失败了下面的 bootstrap 会说为什么
        return self.run([LAUNCHCTL, "bootstrap", self.domain, str(self.file)])

    def start(self, state: State) -> Result:
        if not state.loaded:
            return self.load()
        if self.legacy:
            return self.run([LAUNCHCTL, "start", LABEL])
        return self.run([LAUNCHCTL, "kickstart", self.target])

    def stop(self) -> Result:
        """停下（launchd 先发 SIGTERM，等它收尾退出）。plist 还在：下次登录时又会开起来。"""
        if self.legacy:
            return self.run([LAUNCHCTL, "unload", str(self.file)])
        return self.run([LAUNCHCTL, "bootout", self.target])

    def restart(self, state: State) -> Result:
        if not state.loaded:
            return self.load()
        if self.legacy:
            self.stop()
            return self.load()
        return self.run([LAUNCHCTL, "kickstart", "-k", self.target])

    def remove(self) -> Result:
        """卸载之前先停下。launchd 里"停下"就是卸下，所以和 stop 一样。"""
        return self.stop()

    def after_delete(self) -> None:
        """删掉 plist 以后不用再做什么（systemd 要 daemon-reload）。"""

    # ---- 日志 --------------------------------------------------------------

    def log_place(self, plan: Plan) -> str:
        return str(plan.log_file)

    def show_logs(self, plan: Plan, lines: int, follow: bool, out: IO[str]) -> int:
        return logs.show(plan.log_file, lines, follow, out)

    def problems(self, plan: Plan, count: int) -> list[str]:
        return logs.recent_problems(plan.log_file, count)
