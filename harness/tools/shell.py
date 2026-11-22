"""执行 shell 命令 —— 也是自定义工具，而且是权力最大的一个。

命令是模型写的，不能默认信任：每一条都要先经过用户确认（见 permissions.py）才会来到这里。
设置里打开了沙箱的话，命令还会被关进沙箱里运行（见 sandbox/）：确认是第一道关卡，沙箱是第二道。
"""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..sandbox import Facts, Sandbox
from .base import FactsTool, ToolError, function_tool

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


def _kill(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)  # 连同它启动的子进程一起结束
    except ProcessLookupError:
        pass


class Shell:
    """在工作目录里执行命令，并记住正在运行的那个进程，方便从外面把它结束掉。"""

    def __init__(self, workspace: Path, sandbox: Sandbox | None = None):
        self.workspace = workspace
        self.sandbox = sandbox  # 这次运行的沙箱（见 sandbox/session.py）；None 是没有沙箱这回事（测试、旧的调用方）
        self._running: subprocess.Popen | None = None
        self._stopped = False  # 正在运行的命令是不是被 kill_running 结束的

    def run(self, command: str, timeout_seconds: int = DEFAULT_TIMEOUT, unsandboxed: bool = False) -> str:
        timeout = max(1, min(timeout_seconds, MAX_TIMEOUT))
        env = {key: value for key, value in os.environ.items() if key not in SECRET_ENV_VARS}
        argv = ["/bin/bash", "-c", command]
        # 沙箱开着就包进去；开着却用不了，这里抛出 ToolError、什么都不运行（不会悄悄在沙箱之外运行，见 sandbox/session.py）
        jail = self.sandbox.enter(argv, env, unsandboxed) if self.sandbox is not None else None
        if jail is not None:
            argv, env = jail.argv, jail.env
        process = subprocess.Popen(
            argv,
            cwd=self.workspace,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,  # 独立的进程组，超时或中断时可以整组结束
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
            if jail is not None:
                result += jail.explain(output)  # 像是被沙箱挡住的：告诉模型是怎么回事、能怎么办
        return result

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
        process = self._running
        if process is not None:
            self._stopped = True
            _kill(process)


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
    base = function_tool(
        name=TOOL_NAME,
        description=(
            "在用户的电脑上执行一条 bash 命令，返回它的输出（标准输出和标准错误合在一起）。"
            "每条命令都在工作目录下、全新的 shell 里运行：上一条命令里的 cd 和环境变量不会保留，"
            "需要时写成 `cd 子目录 && 命令`。命令不能等待键盘输入。"
            "每条命令执行前都会请用户确认。只是读写文件时，优先用文件工具。" + SANDBOX_NOTE
        ),
        properties={
            "command": {"type": "string", "description": "要执行的 bash 命令"},
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
