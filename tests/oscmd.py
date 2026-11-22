"""测试里让假模型执行的几条命令，按这台电脑的 shell 写：macOS、Linux 上是 bash，Windows 上是 PowerShell（见 harness/tools/shell.py）。

只有"命令真的要做成某件事"的测试才用这里（建一个文件让测试知道它开始了、一直等到测试放行）。
只看确认卡片上写着什么的测试，命令照样写 bash 的样子：那里不运行，或者运行失败也不影响。
"""

from __future__ import annotations

import os

WINDOWS = os.name == "nt"


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
