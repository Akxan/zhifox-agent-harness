"""测试里检查"只有自己的账号能读"：macOS、Linux 上看权限位（600、700），Windows 上这一项不看。

为什么 Windows 上不看：Windows 的文件没有 rwx 这套权限位。os.chmod 在那里只能改"只读"这一个标记，
st_mode 永远是 666 或者 444，和谁能读没有关系。Windows 上"只有自己能读"靠的是访问控制列表（ACL），
由 harness/winacl.py 设置，在 tests/test_winacl.py 里单独测（只在 Windows 上跑）。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

WINDOWS = os.name == "nt"


def has_mode(path: str | os.PathLike, expected: int) -> bool:
    """path 的权限位是不是 expected。Windows 上总是真（见上面）。"""
    if WINDOWS:
        return True
    return stat.S_IMODE(Path(path).stat().st_mode) == expected
