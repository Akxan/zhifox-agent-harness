"""测试里开真的子进程（演示服务器、python -m harness serve……）时，三种系统上不一样的那几件事。

  "请你收好尾退出"  macOS、Linux 上是 SIGTERM（systemctl stop、kill 发的就是它）。Windows 上 SIGTERM 等于直接结束进程，
                    没有收尾的机会；能让程序收尾的是 Ctrl+Break（CTRL_BREAK_EVENT，程序里收到的是 SIGBREAK，见 harness/exits.py），
                    它只能发给"单独一个进程组"里的进程，所以开的时候要带上 CREATE_NEW_PROCESS_GROUP（own_group）。
  "连同子进程一起结束"  macOS、Linux 上对整个进程组发 SIGKILL；Windows 上用 taskkill /T /F（harness.tools.shell.kill_tree）。
"""

from __future__ import annotations

import os
import signal
import subprocess

from harness.tools.shell import kill_tree

WINDOWS = os.name == "nt"


def own_group() -> dict:
    """给 subprocess.Popen 的参数：让子进程自成一组（之后才能对它发"请退出"、连同子进程一起结束）。"""
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def ask_to_exit(process: subprocess.Popen) -> None:
    """请它收好尾退出（见开头）。Windows 上的 Ctrl+Break 只对用 own_group() 开的进程管用。"""
    if WINDOWS:
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        process.send_signal(signal.SIGTERM)


def end_tree(process: subprocess.Popen) -> None:
    """立刻结束它和它开的所有子进程（收尾没成功时的最后一步）。"""
    kill_tree(process.pid)
    if process.poll() is None:
        process.kill()


def alive(pid: int) -> bool | None:
    """Windows 上这个进程还在不在（OpenProcess + GetExitCodeProcess）。不是 Windows 时返回 None，由调用的人自己看。

    为什么不用 os.kill(pid, 0)：Windows 上 0 就是 CTRL_C_EVENT，os.kill 会真的给它（和它那一组）发 Ctrl-C；
    别的数字更糟，是直接结束那个进程。"""
    if not WINDOWS:
        return None
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)
