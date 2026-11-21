"""Windows 上"只有你自己的账号能读"是怎么做到的。

macOS、Linux 上，数据目录是 700、里面的文件是 600（见 safe.py），别的账号进不去、读不了。
Windows 没有这套权限位：os.chmod 在那里只能改"只读"这一个标记，0o600 写下去什么都没变。
Windows 管"谁能读"的是每个文件、文件夹上的访问控制列表（ACL）。

默认的 ACL 是从上一层文件夹继承来的，要看东西放在哪儿：
  - 放在你的用户文件夹里（C:\\Users\\你\\…）：继承来的就只有你、SYSTEM 和管理员，别的账号本来就读不了。
  - 放在别处（C:\\code\\、D:\\work\\ 这种自己建的文件夹）：C:\\ 下新建的文件夹默认给"Authenticated Users"
    修改权限、"Users"读权限——这台电脑上的任何账号都能打开你的会话、记忆和 .env 里的 API key。
所以不能指望默认的样子。这里做的是：对数据目录和 .env，断开继承（/inheritance:r），只留下三项：
你自己的账号（按 SID，不按名字，改名、域账号都不会认错）、SYSTEM、Administrators，都是完全控制。
数据目录加上 (OI)(CI)：里面以后新建的文件、文件夹都继承这一份，所以 safe.write_file 写的每个文件不用再一个个设。

为什么留着 SYSTEM 和 Administrators：它们本来就能读（管理员可以夺取任何文件的所有权），去掉它们挡不住谁，
反而会让备份、杀毒软件、Windows 自己的索引报错。这和 macOS、Linux 上 root 读得到 600 的文件是一样的。

为什么用 icacls 而不是 ctypes 调 SetSecurityInfo：icacls 是每台 Windows 都有的系统程序（System32 里），
一条命令就说清楚了要的是什么样子，出了问题你也能自己运行 icacls 看；ctypes 写安全描述符要几十行，读的人很难看懂。
它是一个子进程，所以每个文件夹在一次运行里只设一次（_done），不会每写一个文件就运行一次。

设不成功（不是 NTFS 的盘、U 盘、网络盘，或者 icacls 出错）：照常运行，在日志里写一句警告，不拦着你用。
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

WINDOWS = os.name == "nt"
SYSTEM_SID = "S-1-5-18"  # NT AUTHORITY\SYSTEM
ADMINISTRATORS_SID = "S-1-5-32-544"  # BUILTIN\Administrators
_TIMEOUT_SECONDS = 15

logger = logging.getLogger("harness.winacl")

Runner = Callable[[list[str]], tuple[int, str]]


def _run(args: list[str]) -> tuple[int, str]:
    """运行一个系统命令（不经过 shell），返回退出码和输出。CREATE_NO_WINDOW：后台服务里运行时不闪出黑色的窗口。"""
    try:
        done = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


# 测试换成假的（见 tests/conftest.py：默认什么都不做；tests/test_winacl.py 记下收到的命令）
runner: Runner = _run
_done: set[str] = set()
_sid: str | None = None
_lock = threading.Lock()


def _system32(program: str) -> str:
    """系统程序的完整路径：不让 PATH 里一个同名的程序冒充它。"""
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / program)


def current_sid() -> str | None:
    """你这个账号的 SID（S-1-5-21-…）。whoami /user 的 CSV 输出里第二列就是它。拿不到时返回 None。"""
    global _sid
    if _sid is None:
        code, out = runner([_system32("whoami.exe"), "/user", "/fo", "csv", "/nh"])
        if code == 0:
            for part in out.strip().split(","):
                part = part.strip().strip('"')
                if part.startswith("S-1-"):
                    _sid = part
                    break
    return _sid


def command(path: Path, folder: bool, sid: str) -> list[str]:
    """把 path 设成只有 sid、SYSTEM、Administrators 能用的那条 icacls 命令。文件夹的三项都带 (OI)(CI)：往下继承。"""
    inherit = "(OI)(CI)" if folder else ""
    grants = [f"*{who}:{inherit}F" for who in (sid, SYSTEM_SID, ADMINISTRATORS_SID)]
    # /inheritance:r 去掉从上一层继承来的那些（"Users""Authenticated Users"……）；/grant:r 换掉这几项原来的设置；
    # /q 成功时不打印"已处理 1 个文件"
    return [_system32("icacls.exe"), str(path), "/inheritance:r", "/grant:r", *grants, "/q"]


def restrict(path: Path, folder: bool) -> bool:
    """让 path 只有你自己的账号（和 SYSTEM、管理员）能用。不是 Windows 时什么都不做。设成了（或者这次运行里已经设过）返回 True。"""
    if not WINDOWS:
        return True
    key = os.path.normcase(os.path.abspath(path))
    with _lock:
        if folder and key in _done:  # 只记文件夹：同名的文件可能是刚换上的新文件（.env.tmp 每次都是新的）
            return True
        sid = current_sid()
        if sid is None:
            logger.warning("没能读出当前账号的 SID（whoami /user），%s 的访问权限没有收紧", path)
            return False
        code, out = runner(command(path, folder, sid))
        if code != 0:
            logger.warning("没能收紧 %s 的访问权限（icacls 退出码 %s）：%s", path, code, out.strip()[:300])
            return False
        if folder:
            _done.add(key)
        return True


def forget() -> None:
    """忘掉"已经设过"的记录（测试用：每个测试从头开始）。"""
    global _sid
    with _lock:
        _done.clear()
        _sid = None
