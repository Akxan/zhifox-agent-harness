"""后台服务要怎么运行：用哪个 Python、在哪个工作目录、数据和密钥在哪儿、开不开网页。

macOS 的 plist 和 Linux 的 unit 都照着同一份 Plan 写（launchd.py、systemd.py），读回来也还原成 Plan，
所以"装的是什么样的服务"只在这里定义一次。

为什么是"虚拟环境里的 python -P -m harness"，而不是 `uv run harness`：
  - 服务每次启动都直接用这个项目的虚拟环境（.venv），不再经过 uv：不会在开机时联网同步依赖，uv 升级了也不影响。
    路径里的符号链接不解开：.venv/bin/python 指向 uv 装的那个 Python，解开以后就找不到虚拟环境了。
  - -P：不把当前目录放进 Python 找模块的地方。服务的当前目录是工作目录，里面是你的文件、助手写的文件；
    不加 -P 的话，那里要是有一个 json.py，智狐启动时导入的就是它，而不是标准库。
    （`uv run harness` 走的是 .venv/bin/harness 这个脚本，本来就不会这样，-P 让两种开法一样安全。）

服务不继承你终端里的环境变量：launchd 和 systemd 开它的时候，你的 .zshrc、.bashrc 都没有运行过。
所以这里明明白白地写进去几项（数据目录、.env 的位置、PATH、语言），别的设置——API key、代理（HTTPS_PROXY）、
HARNESS_MODEL……——放在 .env 里，智狐启动时自己读（见 config.load_env_file）。装的时候，只在终端里设置了、
.env 里没有的，提醒你一句（只说名字，不打印值）。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..config import (
    API_BASES,
    PROJECT_ROOT,
    SERVICE_LOG,
    WEB_ADDRESS,
    Config,
    env_file_names,
)
from ..i18n import t

_PROXIES = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY")
# 服务的设置里明明白白写进去的环境变量（见 Plan.environment），这几样不用提醒
_WRITTEN = {"HARNESS_DATA_DIR", "HARNESS_ENV_FILE", "PATH", "LANG", "PYTHONUTF8"}
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
        """服务要运行的命令（一个参数一项，不经过 shell）。manager 是 launchd 或 systemd，告诉智狐它是作为哪种服务开着的。"""
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


def make_plan(config: Config, web: bool = True, port: int | None = None, python: str | None = None, environ: Mapping[str, str] | None = None) -> Plan:
    """照着现在的设置（工作目录、数据目录、.env 的位置）和命令行上的选项，定下服务怎么运行。"""
    environ = os.environ if environ is None else environ
    return Plan(
        python=os.path.abspath(python or sys.executable),
        workspace=config.workspace,
        data_dir=config.data_dir,
        env_file=config.env_file,
        web=web,
        port=port,
        path=environ.get("PATH", ""),
        lang=environ.get("LANG", ""),
    )


def problem(plan: Plan) -> str | None:
    """装不了的原因（没有就是 None）。"""
    if not os.access(plan.python, os.X_OK):
        return t("找不到 Python：{path}。先在项目文件夹里运行 uv sync。", path=plan.python)
    if not plan.workspace.is_dir():
        return t("工作目录不存在：{path}", path=plan.workspace)
    if plan.port is not None and not 1 <= plan.port <= 65535:
        return t("端口要在 1 到 65535 之间，收到的是 {port}。", port=plan.port)
    for path in (plan.python, plan.workspace, plan.data_dir, plan.env_file):
        text = str(path)
        if any(ord(char) < 32 or char == "\x7f" for char in text) or "\\" in text:
            # 换行会把服务的设置文件拆成两行；systemd 还会把反斜杠当成转义。这种路径很少见，换一个文件夹最省事
            return t("这个路径里有换行、反斜杠这类字符，写不进服务的设置：{path}。换一个文件夹再装。", path=text)
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
    return sorted(name for name in watched - _WRITTEN - in_file if environ.get(name))


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
