"""运行日志：在服务器上作为服务运行时（`harness serve`），发生了什么都写到标准错误，systemd 会把它收进 journald。

写日志的规矩：

  - 绝不写密钥。各家的 API key、机器人的 token 先登记在这里（hide），每一行写出去之前把它们换成 <hidden>。
    代码里也尽量不把它们放进日志的话里，这里只是兜底：万一哪个第三方库的报错里带着网址（Telegram 的 token
    就写在网址里），也漏不出去。
  - 不写聊天的内容。只写"谁的消息来了、开始了一轮、花了多久"，你说了什么、助手答了什么在会话文件里。
  - 输出到终端时带颜色和时间；输出不是终端时（交给 systemd、重定向到文件），不带颜色的控制字符，免得日志里
    满是 \\x1b[33m 这样的乱码。在 journald 下（systemd 会设置 JOURNAL_STREAM）每行前面加 <4> 这样的级别标记，
    journalctl -p warning 就能只看警告和错误；时间由 journald 自己记，不再重复写。
  - macOS 的后台服务（launchd）和 Windows 的（任务计划程序）没有 journald，日志写进一个文件（setup 的 file）。
    launchd 只管把输出接到文件上，不管它长多大（任务计划程序连接都不接，见 service/supervise.py），所以这里自己管：写满 SERVICE_LOG_BYTES 就换一个新文件，旧的留 SERVICE_LOG_BACKUPS 份。
    文件里每行带上日期（服务一开就是好几天，只有时间的话分不清是哪天的）。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path
from typing import IO

from .config import SERVICE_LOG_BACKUPS, SERVICE_LOG_BYTES

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


def hidden() -> list[str]:
    """登记过的全部密钥（一份拷贝）。工具的结果里出现它们也要遮住（见 ../redact.py）：日志里藏起来的，模型也不该看到。"""
    with _LOCK:
        return list(_SECRETS)


def redact(text: str) -> str:
    with _LOCK:
        secrets = sorted(_SECRETS, key=len, reverse=True)  # 长的先换：一个密钥是另一个的一部分时，不会只换掉一半
    for secret in secrets:
        text = text.replace(secret, HIDDEN)
    return text


class _Formatter(logging.Formatter):
    def __init__(self, color: bool, journal: bool, dated: bool = False):
        super().__init__()
        self.color = color
        self.journal = journal
        self.dated = dated

    def format(self, record: logging.LogRecord) -> str:
        text = redact(record.getMessage())
        if record.exc_info:
            text += "\n" + redact(self.formatException(record.exc_info))
        if self.journal:
            # journald 把每一行当成一条；异常的几行合成一行，免得只有第一行带着级别
            return f"<{_PRIORITY.get(record.levelno, 6)}>" + text.replace("\n", " | ")
        stamp = self.formatTime(record, "%Y-%m-%d %H:%M:%S" if self.dated else "%H:%M:%S")
        if self.color:
            color = _COLORS.get(record.levelno, "")
            return f"\x1b[2m{stamp}\x1b[0m {color}{text}\x1b[0m" if color else f"\x1b[2m{stamp}\x1b[0m {text}"
        return f"{stamp} {record.levelname.lower()} {text}"


class ServiceLog(logging.handlers.RotatingFileHandler):
    """后台服务（launchd、Windows 的任务计划程序）的日志文件：写满了改名成 .1（原来的 .1 变成 .2……），再开一个新的。

    own_stdio：把这个进程的标准输出、标准错误也指向这个文件。launchd 已经把它们接到了同一个文件上（plist 里的
    StandardOutPath、StandardErrorPath；Windows 上是 supervise.py 接的），可换新文件的时候，它们还指着改了名的旧文件：
    没接住的异常、子进程直接写的东西就进了 service.log.1。所以开文件、换新文件之后，都用 dup2 把 1 号和 2 号
    文件描述符换成新的这一个。测试里不要打开它（会把测试自己的输出也换走）。

    Windows 上还有两件事：
      - 一个文件只要还有人开着，就改不了名（macOS、Linux 上可以）。1 号、2 号也开着这个文件，所以换新文件之前，
        先把它们临时指向"空设备"（os.devnull），改完名再指向新文件；不这样的话，日志永远换不成、一直长下去。
      - 用 pythonw.exe 运行时根本没有标准输出（sys.stdout 是 None，1 号、2 号文件描述符也不存在）。
        这时 dup2 照样能把 1、2 号建出来，再给 sys.stdout、sys.stderr 配上对应的文字流，没接住的异常才写得进日志。
    """

    def __init__(self, path: Path, own_stdio: bool = False):
        self.own_stdio = own_stdio
        if own_stdio:
            _fill_std_fds()
        super().__init__(path, maxBytes=SERVICE_LOG_BYTES, backupCount=SERVICE_LOG_BACKUPS, encoding="utf-8")
        self._adopt()

    def doRollover(self) -> None:
        self._release()
        try:
            super().doRollover()
        finally:
            # 改名失败了（Windows 上别的程序正好在读这个文件）：父类已经关掉了文件、没有再开，这里重新开上，下一行再试着换
            if self.stream is None:
                self.stream = self._open()
            self._adopt()

    def _release(self) -> None:
        """换新文件之前，让 1 号、2 号先放开这个文件（见上面）。"""
        if not self.own_stdio:
            return
        _flush_stdio()
        null = os.open(os.devnull, os.O_WRONLY)
        try:
            for fd in (1, 2):
                os.dup2(null, fd)
        finally:
            os.close(null)

    def _adopt(self) -> None:
        if self.stream is None:
            return
        os.chmod(self.baseFilename, 0o600)  # 日志里没有密钥（写之前都换掉了），但有路径和出错的经过，只给自己看
        if self.own_stdio:
            _flush_stdio()
            for fd in (1, 2):
                os.dup2(self.stream.fileno(), fd)
            # pythonw.exe：本来没有标准输出、标准错误，现在 1、2 号有了，配上文字流（closefd=False：关它不会关掉 1、2 号）
            if sys.stdout is None:
                sys.stdout = open(1, "w", encoding="utf-8", errors="backslashreplace", buffering=1, closefd=False)  # noqa: SIM115
            if sys.stderr is None:
                sys.stderr = open(2, "w", encoding="utf-8", errors="backslashreplace", buffering=1, closefd=False)  # noqa: SIM115


def _fill_std_fds() -> None:
    """0、1、2 号文件描述符有不存在的（pythonw.exe），先让它们指向"空设备"。

    为什么：打开文件时系统给的是最小的那个空着的编号。1 号空着的话，日志文件自己就会拿到 1 号，
    之后"把 1 号换成空设备、再换成新文件"（见 _release、_adopt）就会把日志文件本身换掉。"""
    for fd in (0, 1, 2):
        try:
            os.fstat(fd)
        except OSError:
            null = os.open(os.devnull, os.O_RDWR)
            if null != fd:
                os.dup2(null, fd)
                os.close(null)


def _flush_stdio() -> None:
    """dup2 换掉 1、2 号之前，先把还在缓冲里的写出去（不然会写到换过去的那个文件里，或者丢掉）。没有的（pythonw）跳过。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            try:
                stream.flush()
            except (OSError, ValueError):
                pass


def setup(stream: IO[str] | None = None, level: int = logging.INFO, file: Path | None = None, own_stdio: bool = False) -> None:
    """把 harness 的日志接到 stream（默认标准错误），或者 file 这个文件（launchd、任务计划程序下的后台服务，见 ServiceLog）。
    可以调用多次，后一次换掉前一次的设置。"""
    if file is not None:
        file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handler: logging.Handler = ServiceLog(file, own_stdio)
        handler.setFormatter(_Formatter(color=False, journal=False, dated=True))
    else:
        stream = stream or sys.stderr
        handler = logging.StreamHandler(stream)
        tty = hasattr(stream, "isatty") and stream.isatty()
        handler.setFormatter(_Formatter(color=tty and not os.environ.get("NO_COLOR"), journal=not tty and bool(os.environ.get("JOURNAL_STREAM"))))
    root = logging.getLogger("harness")
    for old in list(root.handlers):
        root.removeHandler(old)
        if isinstance(old, ServiceLog):
            old.close()
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
    if file is not None:
        # 后台服务的日志里，每次开起来先写一行"从这里开始"：status 只列这一次运行的警告和错误（见 service/logs.since_start）
        from .service.logs import START_MARK

        root.info("%s%d ----", START_MARK, os.getpid())
    # 各家的 API key、聊天机器人的密钥可能在环境变量里，先登记上（名单和不让命令看到的是同一份）
    # 用到时才导入：这个文件很底层，别让它一导入就拉进工具
    from .tools.shell import SECRET_ENV_VARS

    for name in sorted(SECRET_ENV_VARS):
        hide(os.environ.get(name))
