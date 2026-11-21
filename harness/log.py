"""运行日志：在服务器上作为服务运行时（`harness serve`），发生了什么都写到标准错误，systemd 会把它收进 journald。

写日志的规矩：

  - 绝不写密钥。各家的 API key、机器人的 token 先登记在这里（hide），每一行写出去之前把它们换成 <hidden>。
    代码里也尽量不把它们放进日志的话里，这里只是兜底：万一哪个第三方库的报错里带着网址（Telegram 的 token
    就写在网址里），也漏不出去。
  - 不写聊天的内容。只写"谁的消息来了、开始了一轮、花了多久"，你说了什么、助手答了什么在会话文件里。
  - 输出到终端时带颜色和时间；输出不是终端时（交给 systemd、重定向到文件），不带颜色的控制字符，免得日志里
    满是 \\x1b[33m 这样的乱码。在 journald 下（systemd 会设置 JOURNAL_STREAM）每行前面加 <4> 这样的级别标记，
    journalctl -p warning 就能只看警告和错误；时间由 journald 自己记，不再重复写。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import IO

_SECRETS: set[str] = set()
_LOCK = threading.Lock()
HIDDEN = "<hidden>"
MIN_SECRET = 8  # 太短的不算密钥（不然 "a" 这样的值会把日志里所有的 a 都换掉）

# journald 认的级别标记（和 syslog 一样）
_PRIORITY = {logging.DEBUG: 7, logging.INFO: 6, logging.WARNING: 4, logging.ERROR: 3, logging.CRITICAL: 2}
_COLORS = {logging.WARNING: "\x1b[33m", logging.ERROR: "\x1b[31m", logging.CRITICAL: "\x1b[31;1m"}


def hide(secret: str | None) -> None:
    """登记一个密钥：之后的日志里出现它，就换成 <hidden>。"""
    if secret and len(secret) >= MIN_SECRET:
        with _LOCK:
            _SECRETS.add(secret)


def redact(text: str) -> str:
    with _LOCK:
        secrets = sorted(_SECRETS, key=len, reverse=True)  # 长的先换：一个密钥是另一个的一部分时，不会只换掉一半
    for secret in secrets:
        text = text.replace(secret, HIDDEN)
    return text


class _Formatter(logging.Formatter):
    def __init__(self, color: bool, journal: bool):
        super().__init__()
        self.color = color
        self.journal = journal

    def format(self, record: logging.LogRecord) -> str:
        text = redact(record.getMessage())
        if record.exc_info:
            text += "\n" + redact(self.formatException(record.exc_info))
        if self.journal:
            # journald 把每一行当成一条；异常的几行合成一行，免得只有第一行带着级别
            return f"<{_PRIORITY.get(record.levelno, 6)}>" + text.replace("\n", " | ")
        stamp = self.formatTime(record, "%H:%M:%S")
        if self.color:
            color = _COLORS.get(record.levelno, "")
            return f"\x1b[2m{stamp}\x1b[0m {color}{text}\x1b[0m" if color else f"\x1b[2m{stamp}\x1b[0m {text}"
        return f"{stamp} {record.levelname.lower()} {text}"


def setup(stream: IO[str] | None = None, level: int = logging.INFO) -> None:
    """把 harness 的日志接到 stream（默认标准错误）。可以调用多次，后一次换掉前一次的设置。"""
    stream = stream or sys.stderr
    handler = logging.StreamHandler(stream)
    tty = hasattr(stream, "isatty") and stream.isatty()
    handler.setFormatter(_Formatter(color=tty and not os.environ.get("NO_COLOR"), journal=not tty and bool(os.environ.get("JOURNAL_STREAM"))))
    root = logging.getLogger("harness")
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
    # 各家的 API key、聊天机器人的密钥可能在环境变量里，先登记上（名单和不让命令看到的是同一份）
    # 用到时才导入：这个文件很底层，别让它一导入就拉进工具
    from .tools.shell import SECRET_ENV_VARS

    for name in sorted(SECRET_ENV_VARS):
        hide(os.environ.get(name))
