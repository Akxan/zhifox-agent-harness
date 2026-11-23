"""测试里让假模型执行的几条命令，按这台电脑的 shell 写：macOS、Linux 上是 bash，Windows 上是 PowerShell（见 harness/tools/shell.py）。

只有"命令真的要做成某件事"的测试才用这里（建一个文件让测试知道它开始了、一直等到测试放行）。
只看确认卡片上写着什么的测试，命令照样写 bash 的样子：那里不运行，或者运行失败也不影响。
"""

from __future__ import annotations

import base64
import os

WINDOWS = os.name == "nt"


def script_of(argv: list[str]) -> str:
    """一条命令交给 shell 的那段脚本：bash 是 -c 后面的那一项，PowerShell 是 -EncodedCommand 解开以后的样子。
    命令外面包着记目录的那一层（见 harness/tools/cwd.py），所以测试只看命令本身在不在里面、在不在最后。"""
    if "-EncodedCommand" in argv:
        return base64.b64decode(argv[argv.index("-EncodedCommand") + 1]).decode("utf-16-le")
    return argv[-1]


def change_dir(path: str) -> str:
    """换到 path 这个目录（之后的命令从那里开始，见 harness/tools/cwd.py）。"""
    if WINDOWS:
        return f"Set-Location -LiteralPath '{path}'"
    return f"cd '{path}'"


def print_dir() -> str:
    """打出现在在哪个目录。"""
    if WINDOWS:
        return "(Get-Location).Path"
    return "pwd"


def mark_then_sleep(marker: str, seconds: int) -> str:
    """先建一个空文件 marker（测试看到它就知道命令开始了），再睡 seconds 秒。"""
    if WINDOWS:
        return f"New-Item -ItemType File -Force '{marker}' | Out-Null; Start-Sleep -Seconds {seconds}"
    return f"touch {marker} && sleep {seconds}"


def mark_then_wait_for(marker: str, go: str) -> str:
    """先建一个空文件 marker，然后一直等，直到测试建好文件 go 才结束。"""
    if WINDOWS:
        return f"New-Item -ItemType File -Force '{marker}' | Out-Null; while (-not (Test-Path '{go}')) {{ Start-Sleep -Milliseconds 50 }}"
    return f"touch {marker}; while [ ! -f {go} ]; do sleep 0.05; done"
