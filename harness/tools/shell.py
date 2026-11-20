"""执行 shell 命令 —— 也是自定义工具，而且是权力最大的一个。

命令是模型写的，不能默认信任：每一条都要先经过用户确认（见 permissions.py）才会来到这里。
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

from .base import Tool, ToolError, function_tool

TOOL_NAME = "run_command"
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
MAX_OUTPUT_CHARS = 20_000

# 不让命令看到各家的 API key，免得密钥被打印出来或发到别处
SECRET_ENV_VARS = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY"}


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

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self._running: subprocess.Popen | None = None
        self._stopped = False  # 正在运行的命令是不是被 kill_running 结束的

    def run(self, command: str, timeout_seconds: int = DEFAULT_TIMEOUT) -> str:
        timeout = max(1, min(timeout_seconds, MAX_TIMEOUT))
        env = {key: value for key, value in os.environ.items() if key not in SECRET_ENV_VARS}
        process = subprocess.Popen(
            ["/bin/bash", "-c", command],
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
        return result

    def kill_running(self) -> None:
        """结束正在运行的命令（如果有）。网页界面的"停止"按钮会调用它。"""
        process = self._running
        if process is not None:
            self._stopped = True
            _kill(process)


def make_shell_tool(shell: Shell) -> Tool:
    return function_tool(
        name=TOOL_NAME,
        description=(
            "在用户的电脑上执行一条 bash 命令，返回它的输出（标准输出和标准错误合在一起）。"
            "每条命令都在工作目录下、全新的 shell 里运行：上一条命令里的 cd 和环境变量不会保留，"
            "需要时写成 `cd 子目录 && 命令`。命令不能等待键盘输入。"
            "每条命令执行前都会请用户确认。只是读写文件时，优先用文件工具。"
        ),
        properties={
            "command": {"type": "string", "description": "要执行的 bash 命令"},
            "timeout_seconds": {
                "type": "integer",
                "description": f"最长运行多少秒，默认 {DEFAULT_TIMEOUT}，最大 {MAX_TIMEOUT}",
            },
        },
        required=["command"],
        function=shell.run,
    )
