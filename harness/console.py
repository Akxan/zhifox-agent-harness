"""打印到终端（或者管道、文件）的文字用什么编码。

macOS、Linux 上几乎总是 UTF-8，不用管。Windows 上分两种：
  - 输出就是终端窗口（Windows Terminal、PowerShell、cmd）：Python 用的是系统的宽字符接口，中文、表情都显示得出来，也不用管。
  - 输出被接到了管道或者文件（`harness service status > 状态.txt`、后台服务的日志、测试读演示服务器的输出）：
    Python 默认用"系统的 ANSI 代码页"编码——英文 Windows 上是 cp1252，根本没有汉字，第一句中文就抛出
    UnicodeEncodeError，程序直接退出。
所以 Windows 上、输出不是终端的时候，换成 UTF-8（写不出来的字换成 ?，绝不因为一个字退出）。
这和 PowerShell 7、Windows Terminal、git 的默认一样；想要别的编码，照常可以设 PYTHONIOENCODING。
"""

from __future__ import annotations

import os
import sys


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
