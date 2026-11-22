"""这台电脑用哪一种沙箱。看操作系统是哪一种的地方只有这一处（detect），别处都只通过 Backend 用它。

  macOS    sandbox-exec（macos.py）
  Linux    bubblewrap，命令叫 bwrap（linux.py）；没装就是"用不了"，并告诉你怎么装
  Windows  暂时没有（windows.py 里写了为什么）
  别的类 Unix 系统（FreeBSD……）  也没有：说"这个系统上用不了"

测试时 conftest 把 detect 换成一个假的（tests/fake_sandbox.py）：不建真的沙箱，只记下"这条命令本该在沙箱里运行"。
真的后端只在专门的测试里用（tests/test_sandbox_macos.py、tests/test_sandbox_linux.py）。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass

from ..i18n import Message, k, t
from . import linux, macos, windows
from .policy import Policy


@dataclass(frozen=True)
class Backend:
    id: str  # sandbox-exec / bwrap / none
    label: str  # 给人看的名字（原文，用的时候交给 t()）
    note: str  # 补充的一句（原文）：macOS 的"已经过时"、Linux 的 localhost 是怎么回事、Windows 上可以怎么办
    problem: Callable[[], Message | None]  # 现在能不能用：能用返回 None，不能用返回原因
    command: Callable[[Policy, list[str]], list[str]]  # 在沙箱里运行 argv 的完整命令
    symptoms: tuple[str, ...]  # 被挡住时命令的输出里常见的几句话（见 Sandbox.explain）
    forget: Callable[[], None] = lambda: None  # 忘掉"能不能用"的结果，下次重新试


def _unsupported(argv: list[str]) -> list[str]:
    raise RuntimeError("这个系统上没有沙箱")


def _never(policy: Policy, argv: list[str]) -> list[str]:
    return _unsupported(argv)


MACOS = Backend("sandbox-exec", macos.LABEL, macos.NOTE, macos.problem, macos.command, macos.SYMPTOMS, macos.forget)
LINUX = Backend("bwrap", linux.LABEL, linux.NOTE, linux.problem, linux.command, linux.SYMPTOMS, linux.forget)
WINDOWS = Backend("none", windows.LABEL, windows.NOTE, windows.problem, _never, windows.SYMPTOMS)
OTHER = Backend("none", k("这个系统"), "", lambda: t("这个系统（{system}）上暂时没有沙箱：智狐只会用 macOS 的 sandbox-exec 和 Linux 的 bubblewrap。", system=sys.platform), _never, ())


def detect() -> Backend:
    """这台电脑用哪一个。"""
    if sys.platform == "darwin":
        return MACOS
    if sys.platform.startswith("linux"):
        return LINUX
    if sys.platform == "win32":
        return WINDOWS
    return OTHER
