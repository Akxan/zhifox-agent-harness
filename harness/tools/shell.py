"""执行 shell 命令 —— 也是自定义工具，而且是权力最大的一个。

命令是模型写的，不能默认信任：每一条都要先经过用户确认（见 permissions.py）才会来到这里。
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

from .base import Tool, function_tool

TOOL_NAME = "run_command"
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
MAX_OUTPUT_CHARS = 20_000

# 不让命令看到这些环境变量，免得密钥被打印出来或发到别处
SECRET_ENV_VARS = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}


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


def make_shell_tool(workspace: Path) -> Tool:
    def run_command(command: str, timeout_seconds: int = DEFAULT_TIMEOUT) -> str:
        timeout = max(1, min(timeout_seconds, MAX_TIMEOUT))
        env = {key: value for key, value in os.environ.items() if key not in SECRET_ENV_VARS}
        process = subprocess.Popen(
            ["/bin/bash", "-c", command],
            cwd=workspace,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,  # 独立的进程组，超时或中断时可以整组结束
        )
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(process)
            output, _ = process.communicate()
            return f"{_clip(output)}\n[命令运行超过 {timeout} 秒，已被终止]"
        except KeyboardInterrupt:
            _kill(process)
            raise
        result = _clip(output).rstrip("\n") or "（没有输出）"
        if process.returncode != 0:
            result += f"\n[退出码 {process.returncode}]"
        return result

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
        function=run_command,
    )
