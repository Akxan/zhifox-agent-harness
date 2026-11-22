"""Windows 上的"进程组"：作业对象（Job Object）。只在 Windows 上用。

macOS、Linux 上，智狐启动 Chrome、执行一条命令时，让它自成一个进程组（start_new_session / setpgroup），
要结束的时候对整组发信号：它自己、它开的子进程、子进程再开的子进程，一个不剩——哪怕中间那一层已经先退出了。

Windows 没有进程组和信号。taskkill /T 是顺着"谁是谁的父进程"往下找，中间那一层先退出了，下面的就找不到了
（比如 Chrome 的主进程退出以后留下的渲染进程，或者一条命令里 Start-Process 开的程序）。
作业对象才是 Windows 上对应的东西：把一个进程放进作业，它以后开的所有子孙进程都自动在同一个作业里，
TerminateJobObject 一次结束作业里的全部。kill_on_close 的作业在我们这一头的句柄关掉时（包括智狐自己崩溃退出），
系统把里面的进程全部结束——给 Chrome 用正合适：智狐没了，它的浏览器也不该留着。

用 ctypes 直接调 kernel32，不加依赖。每一步失败都只是"少了这道保险"：调用的人照样有 taskkill 兜底。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys

WINDOWS = os.name == "nt"
_EXTENDED_LIMIT_INFORMATION = 9  # JobObjectExtendedLimitInformation
_KILL_ON_JOB_CLOSE = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE


class _Basic(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _Counters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ("Read", "Write", "Other", "ReadBytes", "WriteBytes", "OtherBytes")]


class _Extended(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _Basic),
        ("IoInfo", _Counters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32() -> ctypes.CDLL:
    if sys.platform != "win32":
        # 走不到：只有 Windows 上才会调用它（Job 先看了 WINDOWS）。写成 sys.platform 的判断，类型检查（mypy）才知道
        # 下面的 WinDLL 只在 Windows 上用；万一真走到了，Job 接住这个错误，和以前接住"没有 WinDLL"一样，handle 是 None
        raise OSError("作业对象只有 Windows 上有")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    return kernel32


class Job:
    """一个作业。add 把一个刚启动的进程放进去；terminate 结束里面的所有进程；close 关掉我们的句柄。
    不是 Windows、或者哪一步没成功时，什么都不做（handle 是 None）。"""

    def __init__(self, kill_on_close: bool = False):
        self.handle: int | None = None
        if not WINDOWS:
            return
        try:
            kernel32 = _kernel32()
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return
            if kill_on_close:
                info = _Extended()
                info.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
                if not kernel32.SetInformationJobObject(handle, _EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)):
                    kernel32.CloseHandle(handle)
                    return
            self.handle = handle
            self._kernel32 = kernel32
        except (OSError, AttributeError):
            self.handle = None

    def add(self, process: subprocess.Popen) -> bool:
        """把 process 放进作业。它这时开的子进程（如果有的话）不在里面：所以要在刚启动之后马上调用。"""
        if self.handle is None:
            return False
        # _handle 是 Popen 在 Windows 上才有的私有属性（进程的句柄），typeshed 里没有写它
        return bool(self._kernel32.AssignProcessToJobObject(self.handle, int(process._handle)))  # type: ignore[attr-defined]  # 见上一行

    def terminate(self) -> None:
        """结束作业里的所有进程（包括父进程已经退出了的那些子孙）。"""
        if self.handle is not None:
            self._kernel32.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        """关掉我们这一头的句柄。kill_on_close 的作业，这时里面还在的进程会被系统结束。"""
        if self.handle is not None:
            handle, self.handle = self.handle, None
            self._kernel32.CloseHandle(handle)
