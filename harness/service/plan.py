"""后台服务要怎么运行：用哪个 Python、在哪个工作目录、数据和密钥在哪儿、开不开网页。

macOS 的 plist、Linux 的 unit、Windows 的任务（XML）都照着同一份 Plan 写（launchd.py、systemd.py、windows.py），
读回来也还原成 Plan，所以"装的是什么样的服务"只在这里定义一次。

为什么是"虚拟环境里的 python -P -m harness"，而不是 `uv run harness`：
  - 服务每次启动都直接用这个项目的虚拟环境（.venv），不再经过 uv：不会在开机时联网同步依赖，uv 升级了也不影响。
    路径里的符号链接不解开：.venv/bin/python 指向 uv 装的那个 Python，解开以后就找不到虚拟环境了。
  - -P：不把当前目录放进 Python 找模块的地方。服务的当前目录是工作目录，里面是你的文件、助手写的文件；
    不加 -P 的话，那里要是有一个 json.py，智狐启动时导入的就是它，而不是标准库。
    （`uv run harness` 走的是 .venv/bin/harness 这个脚本，本来就不会这样，-P 让两种开法一样安全。）

装成工具（uv tool install，见 install.py）时照样是这个写法，Python 照样取 sys.executable：这时它是 uv 给这个工具建的
虚拟环境里的那个（macOS、Linux 上是 ~/.local/share/uv/tools/harness-assistant/bin/python，Windows 上是
%APPDATA%\\uv\\tools\\harness-assistant\\Scripts\\python.exe，旁边也有 pythonw.exe），harness 包就装在那个环境里，
-m harness 找得到它。不改用 ~/.local/bin/harness 那个命令：它只是一个转去运行这个 Python 的小脚本（Windows 上是一个 .exe），
多一层什么都没换来，Windows 上还会多开一个控制台程序、弹出黑色的窗口。-P 照样要：服务的当前目录照样是你的工作目录。
uv tool upgrade 在原地换掉这个环境里的包，路径不变，所以升级以后服务的设置不用重装，运行 harness service restart 换上新的就行。

服务不继承你终端里的环境变量：launchd 和 systemd 开它的时候，你的 .zshrc、.bashrc 都没有运行过。
所以这里明明白白地写进去几项（数据目录、.env 的位置、PATH、语言），别的设置——API key、代理（HTTPS_PROXY）、
HARNESS_MODEL……——放在 .env 里，智狐启动时自己读（见 config.load_env_file）。装的时候，只在终端里设置了、
.env 里没有的，提醒你一句（只说名字，不打印值）。

Windows 不一样：任务计划程序开它的时候，给的是你这个账号自己的那一套环境变量（"系统属性 → 环境变量"里的，
新开的终端看到的也是这一套），PATH 本来就是全的，也没有 LANG 这回事。所以 Windows 上不抄 PATH 和 LANG
（Windows 的 PATH 动不动上千个字，抄进命令行里又长、又会过时），只写数据目录、.env 的位置和 PYTHONUTF8。
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..config import (
    API_BASES,
    FROM_SOURCE,
    PROJECT_ROOT,
    REPAIR,
    SERVICE_LOG,
    WEB_ADDRESS,
    Config,
    env_file_names,
)
from ..i18n import t

_PROXIES = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY")
# 服务的设置里明明白白写进去的环境变量（见 Plan.environment），这几样不用提醒。
# Windows 上的 supervise.py 也只肯替智狐设置这几样
WRITTEN = frozenset({"HARNESS_DATA_DIR", "HARNESS_ENV_FILE", "PATH", "LANG", "PYTHONUTF8"})
TASK_SCHEDULER = "taskscheduler"  # Windows 的任务计划程序（见 windows.py）：--service 后面写的就是这个词
# macOS 的"文稿""桌面""下载"和 iCloud 云盘：后台程序要读这些文件夹，系统可能会拦下来，或者先弹窗问你（隐私与安全性）
MAC_PROTECTED = ("Desktop", "Documents", "Downloads", "Library/Mobile Documents")


@dataclass(frozen=True)
class Plan:
    """一个后台服务的全部设置。"""

    python: str  # 虚拟环境里的 Python 的绝对路径（不解开符号链接）
    workspace: Path  # 工作目录：助手读写文件、执行命令的地方
    data_dir: Path  # 数据目录：会话、记忆、设置……
    env_file: Path  # 保存 API key 的 .env（服务启动时读它，密钥不写进服务的设置）
    web: bool = True  # 开不开网页界面
    port: int | None = None  # 网页界面的端口；None 是默认的 8765，被占用了自动换一个
    path: str = ""  # 服务里的 PATH：装的时候终端里的那一份，让助手执行的命令找得到同样的程序
    lang: str = ""  # 服务里的 LANG（语言和字符集）

    @property
    def log_file(self) -> Path:
        return self.data_dir / SERVICE_LOG

    @property
    def web_address_file(self) -> Path:
        return self.data_dir / WEB_ADDRESS

    def arguments(self, manager: str) -> list[str]:
        """服务要运行的命令（一个参数一项，不经过 shell）。manager 是 launchd、systemd 或 taskscheduler，
        告诉智狐它是作为哪种服务开着的。"""
        command = [self.python, "-P", "-m", "harness", "--workspace", str(self.workspace), "serve"]
        if self.web:
            command.append("--web")
        if self.port is not None:
            command += ["--port", str(self.port)]
        return [*command, "--service", manager]

    def environment(self) -> dict[str, str]:
        """服务里的环境变量。都是路径和开关，没有密钥。"""
        values = {"HARNESS_DATA_DIR": str(self.data_dir), "HARNESS_ENV_FILE": str(self.env_file), "PYTHONUTF8": "1"}
        if self.path:
            values["PATH"] = self.path
        if self.lang:
            values["LANG"] = self.lang
        return values


def make_plan(
    config: Config, web: bool = True, port: int | None = None, python: str | None = None,
    environ: Mapping[str, str] | None = None, manager: str = "",
) -> Plan:
    """照着现在的设置（工作目录、数据目录、.env 的位置）和命令行上的选项，定下服务怎么运行。
    manager 是 taskscheduler 时不抄 PATH 和 LANG（见开头的说明）。

    python 是智狐自己用的那个 Python。Windows 上是 .venv\\Scripts\\python.exe（有控制台的那一个），
    任务计划程序直接开的是它旁边的 pythonw.exe（不弹窗口，见 windowless 和 windows.py）。"""
    environ = os.environ if environ is None else environ
    inherits = manager == TASK_SCHEDULER
    return Plan(
        python=os.path.abspath(python or sys.executable),
        workspace=config.workspace,
        data_dir=config.data_dir,
        env_file=config.env_file,
        web=web,
        port=port,
        path="" if inherits else environ.get("PATH", ""),
        lang="" if inherits else environ.get("LANG", ""),
    )


def windowless(python: str) -> str:
    """python.exe 旁边那个不开窗口的 pythonw.exe（Windows 的虚拟环境里两个都有）。

    python.exe 是"控制台程序"：任务计划程序直接开它的话，登录时会弹出一个黑色的命令行窗口，关掉窗口智狐也跟着退出。
    pythonw.exe 是同一个 Python，只是不要控制台，所以不弹窗口。
    路径是 Windows 的写法（有反斜杠）时按 Windows 的规矩拆；在 macOS、Linux 上测试时路径是 / 写的，照样拆得开。"""
    module = ntpath if "\\" in python else posixpath
    return module.join(module.dirname(python), "pythonw.exe")


def problem(plan: Plan, manager: str = "") -> str | None:
    """装不了的原因（没有就是 None）。manager 是 taskscheduler 时按 Windows 的规矩查（见 _path_problem）。"""
    if not os.access(plan.python, os.X_OK):
        if FROM_SOURCE:
            return t("找不到 Python：{path}。先在项目文件夹里运行 uv sync。", path=plan.python)
        return t("找不到 Python：{path}。重新装一次智狐：{command}", path=plan.python, command=REPAIR)
    if manager == TASK_SCHEDULER and not os.path.isfile(windowless(plan.python)):
        if FROM_SOURCE:
            return t("找不到 {path}（Windows 上的后台服务用它运行，不弹窗口）。先在项目文件夹里运行 uv sync。", path=windowless(plan.python))
        return t("找不到 {path}（Windows 上的后台服务用它运行，不弹窗口）。重新装一次智狐：{command}", path=windowless(plan.python), command=REPAIR)
    if not plan.workspace.is_dir():
        return t("工作目录不存在：{path}", path=plan.workspace)
    if plan.port is not None and not 1 <= plan.port <= 65535:
        return t("端口要在 1 到 65535 之间，收到的是 {port}。", port=plan.port)
    for path in (plan.python, plan.workspace, plan.data_dir, plan.env_file):
        reason = _path_problem(str(path), manager)
        if reason:
            return reason
    return None


def _path_problem(text: str, manager: str) -> str | None:
    """一个路径能不能原样写进服务的设置。各家的设置文件怕的字符不一样：

    换行（和别的控制字符）谁都写不进去：会把设置文件拆成两行。
    launchd、systemd：反斜杠不行（systemd 把它当成转义）。macOS、Linux 的路径里本来就很少有反斜杠，换一个文件夹最省事。
    任务计划程序：反斜杠正是 Windows 路径的分隔符，当然可以。不行的是 %：任务计划程序会把命令行里的 %NAME%
    当成环境变量展开，而且没有办法转义（C:\\50%off%\\ 里的 %off% 要是正好是一个环境变量，路径就变了）。
    双引号本来就不能出现在 Windows 的路径里，出现了就是哪里不对。"""
    control = any(ord(char) < 32 or char == "\x7f" for char in text)
    if manager != TASK_SCHEDULER:
        if control or "\\" in text:
            return t("这个路径里有换行、反斜杠这类字符，写不进服务的设置：{path}。换一个文件夹再装。", path=text)
        return None
    if control:
        return t("这个路径里有换行这类字符，写不进服务的设置：{path}。换一个文件夹再装。", path=text)
    if "%" in text or '"' in text:
        return t("这个路径里有 % 或者双引号，任务计划程序会把 % 当成环境变量展开：{path}。换一个文件夹再装。", path=text)
    return None


def shell_only(plan: Plan, environ: Mapping[str, str] | None = None) -> list[str]:
    """只在终端的环境变量里设置了、.env 里没有的那些（只要名字）。服务启动时读不到它们。"""
    environ = os.environ if environ is None else environ
    from ..providers import PROVIDERS  # 用到时才导入

    watched = {provider.key_env for provider in PROVIDERS.values()}
    watched |= {name for name in _PROXIES} | {name.lower() for name in _PROXIES}
    watched |= {"TELEGRAM_API_BASE", *(env for _, env, _ in API_BASES)}
    watched |= {name for name in environ if name.startswith("HARNESS_")}
    in_file = env_file_names(plan.env_file)
    return sorted(name for name in watched - WRITTEN - in_file if environ.get(name))


def protected_folders(plan: Plan, home: Path) -> list[Path]:
    """项目、工作目录、数据目录里，落在 macOS 会拦着后台程序的那几个文件夹（文稿、桌面、下载、iCloud 云盘）里的。"""
    roots = [home / name for name in MAC_PROTECTED]
    found = []
    for path in (PROJECT_ROOT, Path(plan.python).parent, plan.workspace, plan.data_dir, plan.env_file.parent):
        for root in roots:
            if (path == root or root in path.parents) and root not in found:
                found.append(root)
    return found


def from_arguments(arguments: object, workspace: object, environment: object) -> Plan | None:
    """从装好的服务的设置里读回 Plan（status、open、logs 要知道它的数据目录、开没开网页）。看不懂的返回 None。"""
    if not isinstance(arguments, list) or not arguments or not all(isinstance(item, str) for item in arguments):
        return None
    if not isinstance(environment, dict) or not isinstance(workspace, str) or "serve" not in arguments:
        return None
    data_dir, env_file = environment.get("HARNESS_DATA_DIR"), environment.get("HARNESS_ENV_FILE")
    if not isinstance(data_dir, str) or not isinstance(env_file, str):
        return None
    port = None
    if "--port" in arguments[:-1]:
        value = arguments[arguments.index("--port") + 1]
        port = int(value) if value.isdigit() else None
    if "--workspace" in arguments[:-1]:
        workspace = arguments[arguments.index("--workspace") + 1]
    return Plan(
        python=arguments[0], workspace=Path(workspace), data_dir=Path(data_dir), env_file=Path(env_file),
        web="--web" in arguments, port=port, path=str(environment.get("PATH", "")), lang=str(environment.get("LANG", "")),
    )
