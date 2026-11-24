"""终端（控制台）在几个系统上的差别：打印的文字用什么编码（use_utf8），按下 Ctrl-C 的那一刻能做什么（on_ctrl_c）。

打印到终端（或者管道、文件）的文字用什么编码。

macOS、Linux 上几乎总是 UTF-8，不用管。Windows 上分两种：
  - 输出就是终端窗口（Windows Terminal、PowerShell、cmd）：Python 用的是系统的宽字符接口，中文、表情都显示得出来，也不用管。
  - 输出被接到了管道或者文件（`harness service status > 状态.txt`、后台服务的日志、测试读演示服务器的输出）：
    Python 默认用"系统的 ANSI 代码页"编码——英文 Windows 上是 cp1252，根本没有汉字，第一句中文就抛出
    UnicodeEncodeError，程序直接退出。
所以 Windows 上、输出不是终端的时候，换成 UTF-8（写不出来的字换成 ?，绝不因为一个字退出）。
这和 PowerShell 7、Windows Terminal、git 的默认一样；想要别的编码，照常可以设 PYTHONIOENCODING。
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable

# Windows 控制台的两种"请停下"（SetConsoleCtrlHandler 的文档：CTRL_C_EVENT、CTRL_BREAK_EVENT）。关窗口、注销、关机另有编号，不在这里
CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1


def on_ctrl_c(callback: Callable[[], None]) -> Callable[[], None]:
    """按下 Ctrl-C（Windows 上还有 Ctrl+Break）的那一刻，在另一个线程里调用 callback。返回的函数用来取消登记。

    为什么只有 Windows 要这样：终端里的一轮对话在主线程里跑，Ctrl-C 平时变成 KeyboardInterrupt。可 Python 只在主线程
    回到 Python 代码的时候才抛它。macOS、Linux 上 Ctrl-C 是一个信号，会把主线程正在等的系统调用（等网络回答的 recv）
    当场打断，所以不用这里。Windows 上它打断不了正在等网络的 select：模型在想、什么都没发的时候，要等它再开口才停得下来。
    所以 Windows 上另外登记一个控制台的处理函数（SetConsoleCtrlHandler）：系统在一个新的线程里调用它，它调用 callback
    （断开这次请求的连接，见 providers/inflight.py），主线程马上醒来。处理函数返回 False：接着交给下一个处理函数，
    也就是 Python 自己的那一个，KeyboardInterrupt 照样抛出，收尾和以前一样。"""
    if sys.platform != "win32":
        return _nothing
    import ctypes  # 只有 Windows 用到

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handler = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_ulong)(_handler(callback))
    kernel32.SetConsoleCtrlHandler(handler, True)
    done = [False]

    def forget() -> None:
        if not done[0]:
            done[0] = True
            kernel32.SetConsoleCtrlHandler(handler, False)  # handler 被这个函数引用着：取消登记之前不会被回收

    return forget


def _handler(callback: Callable[[], None]) -> Callable[[int], int]:
    """控制台处理函数本身（单独写出来，三个系统上都能测）：是"请停下"就调用 callback；总是返回 0（False），接着交给下一个。"""

    def handle(kind: int) -> int:
        if kind in (CTRL_C_EVENT, CTRL_BREAK_EVENT):
            # 这是系统的线程：出了错也不能让它把异常带出去（主线程照样会收到 KeyboardInterrupt），记进日志就行
            try:
                callback()
            except Exception:
                logging.getLogger("harness.console").exception("按下 Ctrl-C 时要做的事出错了")
        return 0

    return handle


def _nothing() -> None:
    pass


def use_utf8() -> None:
    """程序一开始调用一次（见 cli.main）。只在 Windows、只对不是终端的那几个输出起作用；设过 PYTHONIOENCODING 的不动。"""
    if os.name != "nt" or os.environ.get("PYTHONIOENCODING"):
        return
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue  # pythonw（后台服务）里根本没有标准输出
        try:
            if stream.isatty():
                continue
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass  # 已经关掉了的、不能改的：照原样
