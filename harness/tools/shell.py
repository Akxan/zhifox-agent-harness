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
  - 结束一条命令：Windows 没有进程组和信号。每条命令放进一个作业对象（winjob.py），结束时结束整个作业，
    它和它启动的所有子孙进程一个不剩；作业没建成时退一步用 taskkill /T /F。
  - CREATE_NO_WINDOW：作为后台服务运行时（没有控制台），每条命令不会闪出一个黑色的窗口。

设置里打开了沙箱的话，命令还会被关进沙箱里运行（见 sandbox/）：确认是第一道关卡，沙箱是第二道。

每个 Shell 记着下一条命令从哪个目录开始（cd 会保留到下一条，见 cwd.py）：命令外面包一层，结束时打出它停在哪个目录，
我们从输出里取出来、去掉，模型看到的输出和以前一样。
"""

from __future__ import annotations

import base64
import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import winjob
from ..i18n import zh
from ..sandbox import Facts, Sandbox
from .base import FactsTool, ToolError, function_tool
from .cwd import CurrentDir, marker, take, wrap

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


# 命令行、说明书里写哪一种 shell 看它（测试里换成 True / False，看别的系统上的样子）。真的去调用只有某个系统才有的
# 东西（killpg、CREATE_NO_WINDOW）的地方另外写成 sys.platform 的判断：两者是一回事，但类型检查（mypy）只认
# sys.platform，认得出来才知道那几行只在哪个系统上走（见 pyproject.toml 的 [tool.mypy]）
WINDOWS = os.name == "nt"

# 启动子进程时给 creationflags 的：Windows 上是 CREATE_NO_WINDOW（作为后台服务运行时不闪出黑色的窗口），别的系统上没有这个标志，是 0。
# MCP 的 stdio 服务器（mcp/stdio.py）也用它
if sys.platform == "win32":
    NO_WINDOW = subprocess.CREATE_NO_WINDOW
else:
    NO_WINDOW = 0
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
    if sys.platform == "win32":  # 见 WINDOWS 那里：这里真的要分系统调用（killpg、SIGKILL 只有 macOS、Linux 有）
        subprocess.run(
            [system32("taskkill.exe"), "/T", "/F", "/PID", str(pid)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _kill(process: subprocess.Popen, job: winjob.Job | None = None) -> None:
    """连同它启动的子进程一起结束。Windows 上结束它所在的作业（见 winjob.py：中间一层先退出了，下面的也跑不掉），
    作业没建成时退一步用 taskkill /T。"""
    if job is not None and job.handle is not None:
        job.terminate()
    else:
        kill_tree(process.pid)
    if WINDOWS and process.poll() is None:
        process.kill()  # 都没能结束它（极少见）：至少结束它自己


class Shell:
    """执行命令，记着下一条命令从哪个目录开始（current，见 cwd.py），并记住正在运行的那个进程，方便从外面把它结束掉。"""

    def __init__(self, workspace: Path, sandbox: Sandbox | None = None):
        self.workspace = workspace
        self.sandbox = sandbox  # 这次运行的沙箱（见 sandbox/session.py）；None 是没有沙箱这回事（测试、旧的调用方）
        self.current = CurrentDir(workspace)  # 下一条命令从哪个目录开始（一开始是工作目录）
        self._running: subprocess.Popen | None = None
        self._job: winjob.Job | None = None  # Windows：正在运行的命令所在的作业
        self._stopped = False  # 正在运行的命令是不是被 kill_running 结束的

    def where(self, unsandboxed: bool = False) -> Path:
        """下一条命令真正从哪个目录开始（记着的目录不在了、沙箱里看不到，就回到工作目录，见 cwd.py 的 where）。
        卡片上写的、判断"只读的命令"时用的（permissions.py）、真正运行时用的，都是它。unsandboxed：这一条要在沙箱之外运行。"""
        sandbox = self.sandbox

        def reaches(path: Path) -> bool:
            if sandbox is None:
                return True
            facts = sandbox.facts(unsandboxed)
            if not facts.on or facts.escapes or facts.problem is not None:
                return True  # 不在沙箱里运行（或者根本不会运行，见 check）：哪儿都能开始
            return sandbox.reaches(path)

        return self.current.where(reaches)

    def run(self, command: str, timeout_seconds: int = DEFAULT_TIMEOUT, unsandboxed: bool = False) -> str:
        timeout = max(1, min(timeout_seconds, MAX_TIMEOUT))
        before = self.current.path
        here = self.where(unsandboxed)
        if here != before:
            # 问你的时候还在，现在不在了（见 cwd.py 的"问你时看的是哪个目录"）：不换个地方悄悄运行
            raise ToolError(self.current.gone(before))
        env = {key: value for key, value in os.environ.items() if key not in SECRET_ENV_VARS}
        if not WINDOWS:
            # bash 照着 PWD 认"现在在哪"（和真正的目录对得上才用它）：经过符号链接 cd 进去的，下一条命令里还是那个写法
            env["PWD"] = str(here)
        mark = marker()
        argv = command_line(wrap(command, mark, WINDOWS))  # bash，Windows 上是 PowerShell（见 command_line）；包的那一层见 cwd.py
        # 沙箱开着就包进去；开着却用不了，这里抛出 ToolError、什么都不运行（不会悄悄在沙箱之外运行，见 sandbox/session.py）
        jail = self.sandbox.enter(argv, env, unsandboxed, cwd=here) if self.sandbox is not None else None
        if jail is not None:
            argv, env = jail.argv, jail.env
        process = subprocess.Popen(
            argv,
            cwd=here,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            # macOS、Linux 上按系统的设置（几乎都是 UTF-8）；Windows 上 PowerShell 已经被设成输出 UTF-8（见 command_line）
            encoding="utf-8" if WINDOWS else None,
            errors="replace",
            start_new_session=not WINDOWS,  # 独立的进程组，超时或中断时可以整组结束
            creationflags=NO_WINDOW,
        )
        self.current.began = here
        # Windows 上的"进程组"：这条命令和它开的子孙进程都在这个作业里。不设"关掉就结束"：
        # 命令正常结束以后，它故意留在后台的程序（Start-Process）照样留着，和 macOS、Linux 上一样
        job = winjob.Job()
        job.add(process)
        self._running, self._job = process, job
        self._stopped = False
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(process, job)
            output, _ = process.communicate()
            output, _ = take(output, mark)  # 超时：留在原来的目录（它停在哪儿说不准），记号那一行照样去掉
            # 输出是资料，原样填进去；后面的说明是我们的话（zh()），工具卡片上按界面的语言显示（见 ../i18n.py 开头）
            return zh("{output}\n[命令运行超过 {seconds} 秒，已被终止]{note}", output=_clip(output), seconds=timeout, note=self.current.take_note())
        except KeyboardInterrupt:  # 终端里按了 Ctrl-C
            _kill(process, job)
            raise
        finally:
            self._running, self._job = None, None
            job.close()
        output, place = take(output, mark)
        if self._stopped:
            shown = _clip(output).rstrip() or zh("（没有输出）")
            raise ToolError(zh("命令还没运行完，就被用户停止了。在那之前的输出：\n{output}{note}", output=shown, note=self.current.take_note()))
        self.current.moved(place)  # 下一条命令从它停下的目录开始
        result = _clip(output).rstrip("\n") or zh("（没有输出）")
        failed = process.returncode != 0
        tail = {
            "code": zh("\n[退出码 {code}]", code=process.returncode) if failed else "",
            "sandbox": jail.explain(output) if failed and jail is not None else "",  # 像是被沙箱挡住的：告诉模型是怎么回事、能怎么办
            "note": self.current.take_note(),
        }
        if not any(tail.values()):
            return result  # 只有输出（资料）：原样交回
        # 输出后面接着我们的几句说明：包成一句 zh()，输出原样填在里面（见上面超时的那一句）
        return zh("{output}{code}{sandbox}{note}", output=result, **tail)

    def facts(self, tool_input: dict) -> Facts | None:
        """这次调用在沙箱这件事上是什么情况（permissions.py 照着它写卡片）。没有沙箱这回事时是 None。"""
        if self.sandbox is None:
            return None
        return self.sandbox.facts(tool_input.get("unsandboxed") is True)

    def check(self, tool_input: dict) -> str | None:
        """明知不会运行的（沙箱开着却用不了、这次运行不许在沙箱之外运行）：直接告诉模型原因，不弹确认卡片。"""
        if self.sandbox is None:
            return None
        return self.sandbox.refusal(tool_input.get("unsandboxed") is True)

    def kill_running(self) -> None:
        """结束正在运行的命令（如果有）。网页界面的"停止"按钮会调用它。"""
        process, job = self._running, self._job
        if process is not None:
            self._stopped = True
            _kill(process, job)


def describe() -> tuple[str, str]:
    """工具的说明和 command 参数的说明。按这台电脑用的 shell 写：一段对话里这台电脑不会变，说明书也就不变。"""
    if not WINDOWS:
        return (
            (
                "在用户的电脑上执行一条 bash 命令，返回它的输出（标准输出和标准错误合在一起）。"
                "每条命令都在一个新的 shell 里运行，只有当前目录会接着用：一段对话的第一条命令在工作目录下运行，"
                "之后每条命令从上一条命令结束时所在的目录开始（cd 会保留）；环境变量（export）、alias、函数不会保留，"
                "需要时写在同一条命令里。文件工具的相对路径仍然从工作目录算起。命令不能等待键盘输入。"
                "每条命令执行前都会请用户确认。只是读写文件时，优先用文件工具。"
            ),
            "要执行的 bash 命令",
        )
    name = shell_name()
    return (
        (
            f"在用户的 Windows 电脑上执行一条 {name} 命令，返回它的输出（标准输出和标准错误合在一起）。"
            "这里没有 bash：请写 PowerShell 的命令（Get-ChildItem、Get-Content、Select-String……），路径用反斜杠或者正斜杠都可以。"
            "每条命令都在一个新的 PowerShell 里运行，只有当前目录会接着用：一段对话的第一条命令在工作目录下运行，"
            "之后每条命令从上一条命令结束时所在的目录开始（Set-Location 会保留）；$env: 变量、函数、别名不会保留，"
            "需要时写在同一条命令里。文件工具的相对路径仍然从工作目录算起。命令不能等待键盘输入（Read-Host、确认提示都会失败）。"
            "每条命令执行前都会请用户确认。只是读写文件时，优先用文件工具。"
        ),
        f"要执行的 {name} 命令",
    )


# 说明书里关于沙箱的那一句，和 unsandboxed 参数的说明。一直都在（沙箱关着也在）：一段对话里说明书不能变，
# 你在设置里中途打开、关掉沙箱，说明书照旧，变的只是命令怎么运行
SANDBOX_NOTE = (
    "用户可能打开了沙箱：命令在沙箱里运行时，只能读写工作目录，读不到家目录里别的东西，可能不能联网，"
    "HOME 和 TMPDIR 是临时的空文件夹；被沙箱挡住时，结果里会说明。"
)
UNSANDBOXED = (
    "只在命令被沙箱挡住、而且确实必须在沙箱之外运行时才设为 true：这会单独请用户放行，每次都问。"
    "沙箱关着时没有作用。不要为了省事使用。"
)


@dataclass(frozen=True)
class ShellTool(FactsTool):
    """run_command。带着它的 Shell：叫出来的子 Agent 用同一个沙箱（见 agents/runner.py）。"""

    shell: Any = field(default=None, compare=False, repr=False)


def make_shell_tool(shell: Shell) -> ShellTool:
    description, command_description = describe()  # bash 还是 PowerShell，按这台电脑写（见上面）
    base = function_tool(
        name=TOOL_NAME,
        description=description + SANDBOX_NOTE,
        properties={
            "command": {"type": "string", "description": command_description},
            "timeout_seconds": {
                "type": "integer",
                "description": f"最长运行多少秒，默认 {DEFAULT_TIMEOUT}，最大 {MAX_TIMEOUT}",
            },
            "unsandboxed": {"type": "boolean", "description": UNSANDBOXED},
        },
        required=["command"],
        function=shell.run,
    )
    return ShellTool(spec=base.spec, run=base.run, portable=base.portable, check=shell.check, facts=shell.facts, shell=shell)
