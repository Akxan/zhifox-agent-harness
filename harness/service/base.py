"""后台服务的几样基础：系统命令的结果、怎么运行系统命令、服务现在的状态。

launchctl、systemctl、journalctl 都经过 run_command / run_attached 运行。测试把这两个换成会报错的假函数
（见 tests/conftest.py），每个测试再自己给一个假的"系统"：这样开发和测试的时候，绝不会真的在这台电脑上装上、
开起一个服务（那会改动你的系统，还可能对着你真正的数据再开一个智狐）。
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from ..config import SERVICE_COMMAND_SECONDS
from ..i18n import t
from ..tools.shell import SECRET_ENV_VARS


@dataclass(frozen=True)
class Result:
    """一条系统命令的结果。"""

    code: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0

    def message(self) -> str:
        """出错时给人看的那一句：命令自己说的话（多半在标准错误里），没说的话是退出码。"""
        said = (self.err or self.out).strip()
        return said or t("退出码 {code}", code=self.code)


Runner = Callable[[list[str]], Result]
Attached = Callable[[list[str]], int]


def _environment() -> dict[str, str]:
    """系统命令看到的环境变量：去掉各家的密钥（.env 里的密钥启动时已经读进了环境变量，命令用不着它们）。"""
    return {name: value for name, value in os.environ.items() if name not in SECRET_ENV_VARS}


def run_command(args: list[str]) -> Result:
    """运行一个系统命令，把输出收回来。参数列表直接交给系统，不经过 shell：路径里有空格、引号也不会出事。"""
    try:
        done = subprocess.run(
            args, check=False, capture_output=True, text=True, errors="replace", timeout=SERVICE_COMMAND_SECONDS,
            stdin=subprocess.DEVNULL, env=_environment(),
        )
    except FileNotFoundError:
        return Result(127, "", t("找不到命令 {command}", command=args[0]))
    except subprocess.TimeoutExpired:
        return Result(124, "", t("{command} 过了 {seconds} 秒还没有结束", command=args[0], seconds=SERVICE_COMMAND_SECONDS))
    return Result(done.returncode, done.stdout, done.stderr)


def run_attached(args: list[str]) -> int:
    """运行一个系统命令，输出直接显示在你的终端里（journalctl -f 这种一直跟着看的）。按 Ctrl-C 结束。"""
    try:
        return subprocess.run(args, env=_environment(), check=False).returncode
    except FileNotFoundError:
        print(t("找不到命令 {command}", command=args[0]))
        return 127
    except KeyboardInterrupt:
        return 0


@dataclass(frozen=True)
class State:
    """服务现在的样子。

    installed  服务的配置文件（plist / unit）在不在
    loaded     系统的服务管家（launchd / systemd）有没有读进它
    running    正在运行；pid 是它的进程编号
    exit_code、signal  上一次退出时的退出码，或者结束它的信号（不知道的时候是 None）
    enabled    登录时会不会自动启动（systemd 的 enable；launchd 下只要文件在就会）
    restarting 退出以后正等着被重新开起来（systemd 的 activating / auto-restart）
    """

    installed: bool
    loaded: bool = False
    running: bool = False
    pid: int | None = None
    exit_code: int | None = None
    signal: int | None = None
    enabled: bool = False
    restarting: bool = False
