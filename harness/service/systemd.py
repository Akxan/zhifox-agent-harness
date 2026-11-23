"""Linux：用 systemd 的"用户服务"把智狐装成后台服务。

systemd 管着 Linux 上几乎所有一直开着的程序。每个程序一份 unit 文件（INI 的写法），用户自己的放在
~/.config/systemd/user/ 里，由"你这个账号的 systemd"（systemctl --user）管，不用 root，也碰不到系统的服务。
docs/zh-CN/background-and-servers.md 里"部署到服务器"那一节用的是系统服务（/etc/systemd/system/，要 root、可以指定用哪个账号运行），
这里是给自己的电脑用的那一种。unit 里这几行决定了它的样子：

    WorkingDirectory=   从哪个文件夹开始运行（工作目录）
    ExecStart=          要运行的命令。每个参数加上双引号（路径里有空格也没事），systemd 直接执行它，不经过 shell
    Environment=        环境变量（见 plan.py：只有路径和开关，没有密钥）
    Restart=on-failure  出错退出（退出码不是 0、被信号杀掉）了再开；你 stop 它、它好好地退出了就不再开
    RestartSec=         隔多少秒再开（见 config.SERVICE_RESTART_SECONDS）
    RestartPreventExitStatus=2   退出码 2 是"设置有问题"（见 serve.py），重启也没用，不再重试，等你修好
    KillMode=mixed      停下时先只给智狐发 SIGTERM，由它自己停下它开的命令、存好盘；到时间还没退出的才一起 SIGKILL
    WantedBy=default.target      enable 以后，你登录时（开了 linger 的话是开机时）就开起来

几条命令：
    systemctl --user daemon-reload           改了 unit 文件以后，让 systemd 重新读一遍
    systemctl --user enable --now zhifox     登录时自动启动，并且现在就开起来
    systemctl --user disable --now zhifox    不再自动启动，并且现在就停下
    systemctl --user start|stop|restart zhifox
    journalctl --user -u zhifox -f           日志（智狐写到标准错误的，systemd 收进了 journald）

linger：默认情况下，你退出登录（最后一个 SSH 会话断开）时，systemd 会把你这个账号的服务一起停掉。
`sudo loginctl enable-linger 你的用户名` 以后，开机就开起来，没人登录也一直开着（在服务器上要这样）。
"""

from __future__ import annotations

import getpass
import os
import shlex
from pathlib import Path
from typing import IO

from ..config import (
    APP_NAME,
    APP_NAME_LATIN,
    EXIT_CONFIG,
    SERVICE_RESTART_SECONDS,
    SERVICE_STOP_SECONDS,
)
from ..safe import write_file
from .base import Attached, Result, Runner, State
from .plan import Plan, from_arguments

UNIT = "zhifox.service"
LINGER_DIR = Path("/var/lib/systemd/linger")  # 开了 linger 的账号，在这里各有一个同名的空文件
DOCUMENTATION = "https://github.com/Akxan/zhifox-agent-harness#run-it-in-the-background"


def _escape(value: str) -> str:
    """unit 文件里的 % 是占位符（%h 是家目录、%u 是用户名……），原样的 % 要写成 %%。"""
    return value.replace("%", "%%")


def _quote(value: str, command: bool = False) -> str:
    """加上双引号：里面的 \\ 和 " 前面加反斜杠。ExecStart 里的 $ 是变量（$HOME），原样的 $ 要写成 $$；Environment= 里不是。"""
    text = _escape(value).replace("\\", "\\\\").replace('"', '\\"')
    return '"' + (text.replace("$", "$$") if command else text) + '"'


def render(plan: Plan) -> str:
    """这个服务的 unit 文件。同样的设置写出来一字不差，重装时才能看出"没有变"。"""
    lines = [
        "[Unit]",
        f"Description={_escape(f'{APP_NAME_LATIN} ({APP_NAME})')}",
        f"Documentation={DOCUMENTATION}",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={_escape(str(plan.workspace))}",
        "ExecStart=" + " ".join(_quote(argument, command=True) for argument in plan.arguments("systemd")),
        *(f"Environment={_quote(f'{name}={value}')}" for name, value in sorted(plan.environment().items())),
        "Restart=on-failure",
        f"RestartSec={SERVICE_RESTART_SECONDS}",
        f"RestartPreventExitStatus={EXIT_CONFIG}",
        "KillMode=mixed",
        f"TimeoutStopSec={SERVICE_STOP_SECONDS}",
        "",
        "[Install]",
        "WantedBy=default.target",
    ]
    return "\n".join(lines) + "\n"


def _unquote(value: str, command: bool = False) -> list[str]:
    parts = shlex.split(value)  # 双引号和里面的 \\、\" 和 systemd 的写法一样
    if command:
        parts = [part.replace("$$", "$") for part in parts]
    return [part.replace("%%", "%") for part in parts]


def parse(text: str) -> Plan | None:
    """从 unit 文件读回 Plan。只认我们自己写的那几行；看不懂的返回 None。"""
    arguments: list[str] = []
    workspace = ""
    environment: dict[str, str] = {}
    try:
        for line in text.splitlines():
            key, _, value = line.partition("=")
            if key == "ExecStart":
                arguments = _unquote(value, command=True)
            elif key == "WorkingDirectory":
                workspace = value.replace("%%", "%")
            elif key == "Environment":
                for item in _unquote(value):
                    name, _, setting = item.partition("=")
                    environment[name] = setting
    except ValueError:  # 引号没配对：不是我们写的
        return None
    return from_arguments(arguments, workspace, environment)


class Systemd:
    """Linux 的服务管家。runner 运行 systemctl、journalctl 并收回输出，attached 让输出直接显示在终端里（测试都换成假的）。"""

    name = "systemd"

    def __init__(
        self, home: Path, runner: Runner, attached: Attached, systemctl: str = "systemctl", journalctl: str = "journalctl",
        user: str | None = None, linger_dir: Path = LINGER_DIR, config_home: Path | None = None,
    ):
        configured = os.environ.get("XDG_CONFIG_HOME")
        base = config_home or (Path(configured) if configured and os.path.isabs(configured) else home / ".config")
        self.file = base / "systemd" / "user" / UNIT
        self.run = runner
        self.attached = attached
        self.systemctl = systemctl
        self.journalctl = journalctl
        self.user = user or getpass.getuser()
        self.linger_dir = linger_dir

    def _ctl(self, *arguments: str) -> Result:
        return self.run([self.systemctl, "--user", *arguments])

    # ---- 文件 --------------------------------------------------------------

    render = staticmethod(render)

    def read(self) -> str | None:
        try:
            return self.file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def read_plan(self) -> Plan | None:
        text = self.read()
        return parse(text) if text is not None else None

    def write(self, plan: Plan, content: str) -> None:
        """写 unit 文件（权限 644，里面没有密钥）。"""
        self.file.parent.mkdir(parents=True, exist_ok=True)
        write_file(self.file, content)
        self.file.chmod(0o644)

    def delete(self) -> None:
        self.file.unlink(missing_ok=True)

    def precheck(self) -> Result:
        """连得上你这个账号的 systemd 吗（有的环境没有：容器、没开 systemd 的 WSL、用 su 切过来的账号）。"""
        return self._ctl("daemon-reload")

    # ---- 状态 --------------------------------------------------------------

    def state(self) -> State:
        shown = self._ctl("show", UNIT, "--no-pager", "--property=LoadState,ActiveState,SubState,MainPID,ExecMainCode,ExecMainStatus,UnitFileState")
        installed = self.file.is_file()
        if not shown.ok:
            return State(installed)
        values = dict(line.split("=", 1) for line in shown.out.splitlines() if "=" in line)

        def number(name: str) -> int:
            value = values.get(name, "").strip()
            return int(value) if value.isdigit() else 0

        pid = number("MainPID") or None
        active = values.get("ActiveState", "")
        code, status = number("ExecMainCode"), number("ExecMainStatus")  # code：1 是自己退出的，2、3 是被信号结束的
        return State(
            installed,
            loaded=values.get("LoadState") == "loaded",
            running=active in ("active", "reloading", "deactivating") and pid is not None,
            pid=pid,
            exit_code=status if code == 1 else None,
            signal=status if code in (2, 3) else None,
            enabled=values.get("UnitFileState", "") in ("enabled", "enabled-runtime", "linked", "linked-runtime"),
            restarting=active == "activating" and values.get("SubState") == "auto-restart",
        )

    def stoppable(self, state: State) -> bool:
        return state.running or state.restarting

    def lingering(self) -> bool:
        return (self.linger_dir / self.user).exists()

    # ---- 开、停 ------------------------------------------------------------

    def load(self) -> Result:
        """读进新写的 unit，设成登录时自动启动，并且现在就开起来。"""
        reloaded = self._ctl("daemon-reload")
        return self._ctl("enable", "--now", UNIT) if reloaded.ok else reloaded

    def start(self, state: State) -> Result:
        return self._ctl("start", UNIT)

    def stop(self) -> Result:
        """停下（systemd 先发 SIGTERM，等它收尾退出）。还是 enable 的：下次登录时又会开起来。"""
        return self._ctl("stop", UNIT)

    def restart(self, state: State) -> Result:
        return self._ctl("restart", UNIT)

    def remove(self) -> Result:
        """卸载之前：不再自动启动，并且现在就停下。"""
        return self._ctl("disable", "--now", UNIT)

    def after_delete(self) -> None:
        """删掉 unit 文件以后，让 systemd 忘掉它（失败了也不要紧，下次 daemon-reload 时它自己会忘）。"""
        self._ctl("daemon-reload")
        self._ctl("reset-failed", UNIT)

    # ---- 日志 --------------------------------------------------------------

    def log_place(self, plan: Plan) -> str:
        return f"journalctl --user -u {UNIT.removesuffix('.service')}"

    def show_logs(self, plan: Plan, lines: int, follow: bool, out: IO[str]) -> int:
        command = [self.journalctl, "--user", "-u", UNIT, "-n", str(lines), "--no-pager"]
        return self.attached([*command, "-f"] if follow else command)

    def problems(self, plan: Plan, count: int) -> list[str]:
        found = self.run([self.journalctl, "--user", "-u", UNIT, "-p", "warning", "-n", str(count), "--no-pager", "-o", "short-iso"])
        if not found.ok:
            return []
        # "-- No entries --"、"Hint: …" 是 journalctl 自己的说明，不是日志
        return [line for line in found.out.splitlines() if line.strip() and not line.startswith(("-- ", "Hint:"))][-count:]
