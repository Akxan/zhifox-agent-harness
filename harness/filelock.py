"""文件锁：几个程序（不只是几个线程）之间说好"这个文件现在归我"。macOS、Linux 和 Windows 一样用。

用在两处：数据目录的 .lock（同一个数据目录只给一个智狐用，见 instance.py），和 channels.json 旁边的锁
（后台服务和命令行里的 `harness channels pair` 会同时改它，见 channels/store.py）。
两处要的都是同一种锁：程序退出（哪怕是崩溃、被强行结束）时由操作系统自动放开，不会"程序没了、锁还在"。

两种系统的做法不一样：
  macOS、Linux  flock：锁的是整个文件，别人照样能读写它的内容，锁只是一个大家都认的约定
  Windows       msvcrt.locking（底下是 LockFile）：锁的是文件里的一段字节，而且是强制的——
                被锁住的那段别的程序读不了。所以锁的不是开头，而是很远的一个位置（LOCK_OFFSET），
                那里根本没有内容：.lock 里写着的"是谁占着"（见 instance.py）别人照样读得到
Windows 没有"共享锁"，看一眼有没有人占着（instance.in_use）就是"试着锁一下，锁到了马上放开"，效果一样。
"""

from __future__ import annotations

import os
import sys
import time
from typing import IO

LOCK_OFFSET = 0x7FFF_FFF0  # Windows 上锁的那一个字节的位置（见上面）：远在文件内容之外，又在 32 位的范围以内
_POLL_SECONDS = 0.05  # Windows 上"等到能锁为止"：msvcrt 的阻塞锁只重试 10 秒就放弃，所以自己一小段一小段地等

# 这个文件里分系统的地方都写成 sys.platform == "win32"（和 os.name == "nt" 是一回事）：类型检查（mypy）只认这种写法，
# 认得出来，它才知道 msvcrt 只在 Windows 的分支里用、fcntl 只在另一个分支里用（见 pyproject.toml 的 [tool.mypy]）
if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class Busy(Exception):
    """别的程序（或者同一个程序里别的打开方式）正锁着它。"""


def lock(handle: IO, wait: bool = True) -> None:
    """锁住 handle 这个打开着的文件。wait 为假时锁不到就抛出 Busy，不等。

    文件系统不支持锁（少数网络盘）时抛出 OSError，由调用的人决定是照常运行还是停下。
    """
    if sys.platform != "win32":
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise Busy from exc
    else:
        fd = handle.fileno()
        while True:
            try:
                _at_offset(fd, msvcrt.LK_NBLCK)
                return
            except OSError as exc:
                # 锁不到时 msvcrt 报的是 EDEADLOCK（36）或者 EACCES（13）：别人锁着。别的错误照原样交出去
                if exc.errno not in (13, 36):
                    raise
                if not wait:
                    raise Busy from exc
            time.sleep(_POLL_SECONDS)


def unlock(handle: IO) -> None:
    """放开 lock 锁住的。没锁着的话什么都不做。（关掉文件也会放开，所以多数时候用不着它。）"""
    if sys.platform != "win32":
        fcntl.flock(handle, fcntl.LOCK_UN)
    else:
        try:
            _at_offset(handle.fileno(), msvcrt.LK_UNLCK)
        except OSError:
            pass


def held_elsewhere(handle: IO) -> bool:
    """是不是有别人锁着（只是看一眼：锁到了马上放开）。文件系统不支持锁时当作没人锁着。"""
    try:
        lock(handle, wait=False)
    except Busy:
        return True
    except OSError:
        return False
    unlock(handle)
    return False


if sys.platform == "win32":  # 只在 Windows 上有（也只在 Windows 上用）：写在这个判断里，mypy 在别的系统上就不查它

    def _at_offset(fd: int, mode: int) -> None:
        """在 LOCK_OFFSET 那一个字节上加锁或者放开。用完把读写的位置挪回原处：
        .lock 是追加着打开的，写"是谁占着"那一行之前位置本来就会挪到末尾，这里只是不留下意外。"""
        position = os.lseek(fd, 0, os.SEEK_CUR)
        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, mode, 1)
        finally:
            os.lseek(fd, position, os.SEEK_SET)
