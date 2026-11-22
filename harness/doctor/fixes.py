"""怎么修：每一句都按这台电脑的系统写。macOS 上是 zsh 的命令，Linux 上是 bash 的，Windows 上是 PowerShell 的。

为什么要分开写：同一件事在两边的写法不一样，抄错了就修不好。
  - 引号：zsh 里 '…' 什么都不展开；PowerShell 里 '…' 也不展开，但单引号本身要写成两个（''），
    而且像 (OI)(CI)F 这种带括号的参数不加引号会被 PowerShell 当成表达式。
  - 环境变量：export NAME=… / unset NAME，在 PowerShell 里是 $env:NAME = '…' / Remove-Item Env:NAME。
  - "只有你自己能读"：chmod 600 / 700；Windows 上没有权限位，是 icacls（和 winacl.py 收紧时用的是同一条命令）。
  - 装东西、打开文件：brew / apt / winget，open -e / nano / notepad。

系统只看 machine.system()（CLAUDE.md 第 9 条：判断系统的地方只有那一处）。测试把它换掉，就能看到另外两个系统上的说法。
这里只写"怎么说"，什么时候算有问题由各个检查自己决定。
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from .. import machine, winacl
from ..config import COMMAND, FROM_SOURCE, PROJECT_ROOT
from ..i18n import Message, t

# PowerShell 里不用加引号的参数：只有字母、数字和这几个符号
_PLAIN = re.compile(r"[A-Za-z0-9_\-./:\\=+,@%]+")


def shell() -> str:
    """要在哪个 shell 里运行：macOS 的终端默认是 zsh，Linux 多半是 bash，Windows 上是 PowerShell。"""
    return {machine.MAC: "zsh", machine.WINDOWS: "PowerShell"}.get(machine.system(), "bash")


def quote(arg: str) -> str:
    """一个参数在这个 shell 里的写法。"""
    if machine.system() == machine.WINDOWS:
        return arg if arg and _PLAIN.fullmatch(arg) else "'" + arg.replace("'", "''") + "'"
    return shlex.quote(arg)


def command(*args: str) -> str:
    return " ".join(quote(str(arg)) for arg in args)


def run(line: str) -> Message:
    """"在 zsh 里运行：…"这样的一句。"""
    return t("在 {shell} 里运行：{command}", shell=shell(), command=line)


def in_project(line: str) -> Message:
    """要在智狐的文件夹里运行的（uv 的命令要找到 pyproject.toml）。

    装成了工具（config.FROM_SOURCE 为假，见 install.py）时没有这个文件夹：PROJECT_ROOT 是 site-packages，
    命令（harness …、uv tool install --force …）在哪儿运行都一样，所以只说在哪个 shell 里运行。"""
    if not FROM_SOURCE:
        return run(line)
    return t("在智狐的文件夹（{path}）里，用 {shell} 运行：{command}", path=PROJECT_ROOT, shell=shell(), command=line)


def harness(*args: str) -> Message:
    """一条 harness … 命令（从仓库运行时是 uv run harness …，见 config.COMMAND）：三个系统上写法一样，只是 shell 的名字不同。"""
    return in_project(command(*COMMAND.split(), *args))


# ---- 只有你自己能读 ----------------------------------------------------------------


def _icacls(path: Path, folder: bool) -> str:
    """和 winacl.restrict 收紧时一模一样的那条 icacls 命令（程序写短名字：在 PowerShell 里 icacls 就是 System32 里的那个）。
    读不出你的 SID 时用 $env:USERNAME（PowerShell 在双引号里会换成你的账号名）。"""
    sid = winacl.current_sid()
    inherit = "(OI)(CI)" if folder else ""
    mine = f"*{sid or 'SID'}:{inherit}F"
    args = ["icacls", *winacl.command(path, folder, sid or "SID")[1:]]
    return " ".join(f'"${{env:USERNAME}}:{inherit}F"' if sid is None and arg == mine else quote(arg) for arg in args)


def private_folder(path: Path) -> Message:
    """让一个文件夹只有你自己的账号能进。"""
    if machine.system() == machine.WINDOWS:
        return run(_icacls(path, folder=True))
    return run(command("chmod", "700", str(path)))


def private_files(paths: list[Path]) -> Message:
    """让这几个文件只有你自己的账号能读。"""
    if machine.system() == machine.WINDOWS:
        return run("; ".join(_icacls(path, folder=False) for path in paths))
    return run(command("chmod", "600", *(str(path) for path in paths)))


def take_back(path: Path) -> Message:
    """文件夹属于别的账号（多半是用 sudo 运行过一次智狐，里面的东西都成了 root 的）：把它要回来。只有 macOS、Linux 上会这样。"""
    return run(f'sudo chown -R "$(whoami)" {quote(str(path))}')


# ---- 环境变量 --------------------------------------------------------------------


def unset(name: str) -> Message:
    """去掉一个环境变量（这一个终端里）。"""
    if machine.system() == machine.WINDOWS:
        line = f"Remove-Item Env:{name}"
    else:
        line = f"unset {name}"
    return t("{run}（写在 shell 的配置文件里的，也要从那里删掉）", run=run(line))


def set_variable(name: str, value: str) -> str:
    """设一个环境变量的那一行命令。PowerShell 里值一律写在单引号里：光写一个路径会被当成要运行的命令。"""
    if machine.system() == machine.WINDOWS:
        return f"$env:{name} = '" + value.replace("'", "''") + "'"
    return f"export {name}={quote(value)}"


# ---- 文件、程序 --------------------------------------------------------------------


def edit(path: Path) -> str:
    """用编辑器打开一个文件的那一行命令。"""
    system = machine.system()
    if system == machine.MAC:
        return command("open", "-e", str(path))
    if system == machine.WINDOWS:
        return command("notepad", str(path))
    return command("nano", str(path))


def copy(source: Path, target: Path) -> str:
    if machine.system() == machine.WINDOWS:
        return command("Copy-Item", str(source), str(target))
    return command("cp", str(source), str(target))


def find_program(name: str) -> str:
    """查一个程序在哪儿的命令：which / Get-Command。"""
    if machine.system() == machine.WINDOWS:
        return command("Get-Command", name)
    return command("which", name)


def add_key(key_env: str, env_file: Path) -> list[Message]:
    """还没有某一家的密钥：两种填法（和第一次启动时终端里的说明一样，见 cli.SETUP_HELP）。"""
    lines = [t("最省事的：运行 {command} --web，在右上角的“设置”里填。", command=COMMAND)]
    example = PROJECT_ROOT / ".env.example"
    if not env_file.exists() and example.is_file():
        lines.append(t("或者先建好 .env：{run}", run=run(copy(example, env_file))))
        lines.append(t("再在里面加一行 {key_env}=你的key：{run}", key_env=key_env, run=run(edit(env_file))))
    else:
        lines.append(t("或者在 .env 里加一行 {key_env}=你的key：{run}", key_env=key_env, run=run(edit(env_file))))
    return lines


def install_uv() -> Message:
    if machine.system() == machine.WINDOWS:
        return run('powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"')
    return run("curl -LsSf https://astral.sh/uv/install.sh | sh")


def install_chrome() -> list[Message]:
    """找不到 Chrome：装一个，或者告诉智狐它在哪儿。"""
    system = machine.system()
    if system == machine.MAC:
        install = run("brew install --cask google-chrome")
        where = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    elif system == machine.WINDOWS:
        install = run("winget install Google.Chrome")
        where = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    else:
        install = run("sudo apt install chromium")
        where = "/usr/bin/chromium"
    return [
        t("装一个 Chrome（Chromium、Edge、Brave 也行）：{run}", run=install),
        t("已经装了、只是不在常见的位置：在“设置 → 浏览器”里填上它的路径，或者 {line}", line=set_variable("HARNESS_CHROME", where)),
    ]


def install_x11_tools(missing: list[str]) -> Message:
    """Linux 的 X11 桌面上操作整台电脑要的几个程序（见 desktop/x11.py）。"""
    packages = []
    if "xdotool" in missing:
        packages.append("xdotool")
    if "capture" in missing or "convert" in missing:
        packages.append("imagemagick")
    return t("装上它们（Debian、Ubuntu 上）：{run}", run=run(command("sudo", "apt", "install", *packages)))
