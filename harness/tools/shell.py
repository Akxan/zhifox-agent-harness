"""执行 shell 命令 —— 也是自定义工具，而且是权力最大的一个。

命令是模型写的，不能默认信任：每一条都要先经过用户确认（见 permissions.py）才会来到这里。

用哪个 shell：macOS、Linux 上是 bash（/bin/bash -c）。Windows 上没有 bash，用的是每台 Windows 都有的 PowerShell
（装了 PowerShell 7，也就是 pwsh，就用它；没有就用系统自带的 Windows PowerShell 5.1）。工具的说明和系统提示词里
都写明是哪一种，模型才知道该写 `Get-ChildItem` 还是 `ls -la`。

Windows 上的几处不同：
  - 命令用 -EncodedCommand 交给 PowerShell（UTF-16 的 base64）：命令行上的引号在 Windows 上要经过两层解析
    （C 运行库的规则，再加上 PowerShell 自己的），写进 -Command 很容易被拆错；编码以后一个字都不会变。
  - 命令前面先把 PowerShell 的输出编码设成 UTF-8，我们按 UTF-8 读：不然中文 Windows 上输出是 GBK，读出来是乱码。
  - -NoProfile：不运行你的 $PROFILE（那里可能打印东西、改提示符，每条命令都多花时间）。执行策略不改，照你的设置。
  - 结束一条命令：Windows 没有进程组和信号，用 taskkill /T /F 结束它和它启动的所有子进程。
  - CREATE_NO_WINDOW：作为后台服务运行时（没有控制台），每条命令不会闪出一个黑色的窗口。
"""

from __future__ import annotations

import base64
import os
import shutil
import signal
import subprocess
from pathlib import Path

from .base import Tool, ToolError, function_tool

TOOL_NAME = "run_command"
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
MAX_OUTPUT_CHARS = 20_000

# 不让命令看到各家的 API key 和聊天机器人的密钥，免得被打印出来或发到别处。
# 机器人的那几个和 channels/platforms.py 里各平台 fields 的 env 一一对应（tests/test_channels_platforms.py 会对一遍）
SECRET_ENV_VARS = {
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY",
    "TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN",
    "FEISHU_APP_SECRET", "DINGTALK_CLIENT_SECRET", "QQ_APP_SECRET",
}


def _clip(output: str) -> str:
    """输出太长时只保留开头和结尾，中间省略。"""
    if len(output) <= MAX_OUTPUT_CHARS:
        return output
    half = MAX_OUTPUT_CHARS // 2
    omitted = len(output) - MAX_OUTPUT_CHARS
    return f"{output[:half]}\n……（中间省略了 {omitted} 个字符）……\n{output[-half:]}"


WINDOWS = os.name == "nt"
# PowerShell 在每条命令前面先做的事：输出一律是 UTF-8（见开头的说明）。$OutputEncoding 管的是 PowerShell 交给别的程序的文字
_UTF8_PRELUDE = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); $OutputEncoding = [Console]::OutputEncoding\n"


def system32(program: str) -> str:
    """Windows 自带程序的完整路径（taskkill、icacls……）：不让 PATH 里一个同名的程序冒充它。"""
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / program)


def powershell() -> str:
    """Windows 上用哪个 PowerShell：装了 PowerShell 7（pwsh）就用它，不然用每台 Windows 都有的 Windows PowerShell 5.1。"""
    return shutil.which("pwsh") or system32(r"WindowsPowerShell\v1.0\powershell.exe")


def shell_name() -> str:
    """给模型看的 shell 的名字（工具说明和系统提示词里用）。"""
    if not WINDOWS:
        return "bash"
    return "PowerShell 7（pwsh）" if shutil.which("pwsh") else "Windows PowerShell 5.1"


def command_line(command: str) -> list[str]:
    """运行 command 的参数列表（不经过别的 shell）。"""
    if not WINDOWS:
        return ["/bin/bash", "-c", command]
    encoded = base64.b64encode((_UTF8_PRELUDE + command).encode("utf-16-le")).decode("ascii")
    return [powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def kill_tree(pid: int) -> None:
    """结束一个进程和它启动的所有子进程。macOS、Linux 上它自成一个进程组（start_new_session），对整组发 SIGKILL；
    Windows 上用 taskkill /T（连同子进程）/F（强制）。已经结束了的话什么都不做。"""
    if WINDOWS:
        subprocess.run(
            [system32("taskkill.exe"), "/T", "/F", "/PID", str(pid)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _kill(process: subprocess.Popen) -> None:
    kill_tree(process.pid)  # 连同它启动的子进程一起结束
    if WINDOWS and process.poll() is None:
        process.kill()  # taskkill 没能结束它（极少见）：至少结束它自己


class Shell:
    """在工作目录里执行命令，并记住正在运行的那个进程，方便从外面把它结束掉。"""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self._running: subprocess.Popen | None = None
        self._stopped = False  # 正在运行的命令是不是被 kill_running 结束的

    def run(self, command: str, timeout_seconds: int = DEFAULT_TIMEOUT) -> str:
        timeout = max(1, min(timeout_seconds, MAX_TIMEOUT))
        env = {key: value for key, value in os.environ.items() if key not in SECRET_ENV_VARS}
        process = subprocess.Popen(
            command_line(command),
            cwd=self.workspace,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            # macOS、Linux 上按系统的设置（几乎都是 UTF-8）；Windows 上 PowerShell 已经被设成输出 UTF-8（见 command_line）
            encoding="utf-8" if WINDOWS else None,
            errors="replace",
            start_new_session=not WINDOWS,  # 独立的进程组，超时或中断时可以整组结束
            creationflags=subprocess.CREATE_NO_WINDOW if WINDOWS else 0,
        )
        self._running = process
        self._stopped = False
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(process)
            output, _ = process.communicate()
            return f"{_clip(output)}\n[命令运行超过 {timeout} 秒，已被终止]"
        except KeyboardInterrupt:  # 终端里按了 Ctrl-C
            _kill(process)
            raise
        finally:
            self._running = None
        if self._stopped:
            raise ToolError(f"命令还没运行完，就被用户停止了。在那之前的输出：\n{_clip(output).rstrip() or '（没有输出）'}")
        result = _clip(output).rstrip("\n") or "（没有输出）"
        if process.returncode != 0:
            result += f"\n[退出码 {process.returncode}]"
        return result

    def kill_running(self) -> None:
        """结束正在运行的命令（如果有）。网页界面的"停止"按钮会调用它。"""
        process = self._running
        if process is not None:
            self._stopped = True
            _kill(process)


def describe() -> tuple[str, str]:
    """工具的说明和 command 参数的说明。按这台电脑用的 shell 写：一段对话里这台电脑不会变，说明书也就不变。"""
    if not WINDOWS:
        return (
            (
                "在用户的电脑上执行一条 bash 命令，返回它的输出（标准输出和标准错误合在一起）。"
                "每条命令都在工作目录下、全新的 shell 里运行：上一条命令里的 cd 和环境变量不会保留，"
                "需要时写成 `cd 子目录 && 命令`。命令不能等待键盘输入。"
                "每条命令执行前都会请用户确认。只是读写文件时，优先用文件工具。"
            ),
            "要执行的 bash 命令",
        )
    name = shell_name()
    return (
        (
            f"在用户的 Windows 电脑上执行一条 {name} 命令，返回它的输出（标准输出和标准错误合在一起）。"
            "这里没有 bash：请写 PowerShell 的命令（Get-ChildItem、Get-Content、Select-String……），路径用反斜杠或者正斜杠都可以。"
            "每条命令都在工作目录下、全新的 PowerShell 里运行：上一条命令里的 Set-Location 和 $env: 变量不会保留，"
            "需要时写成 `Set-Location 子目录; 命令`。命令不能等待键盘输入（Read-Host、确认提示都会失败）。"
            "每条命令执行前都会请用户确认。只是读写文件时，优先用文件工具。"
        ),
        f"要执行的 {name} 命令",
    )


def make_shell_tool(shell: Shell) -> Tool:
    description, command_description = describe()
    return function_tool(
        name=TOOL_NAME,
        description=description,
        properties={
            "command": {"type": "string", "description": command_description},
            "timeout_seconds": {
                "type": "integer",
                "description": f"最长运行多少秒，默认 {DEFAULT_TIMEOUT}，最大 {MAX_TIMEOUT}",
            },
        },
        required=["command"],
        function=shell.run,
    )
