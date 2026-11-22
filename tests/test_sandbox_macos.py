"""真的 sandbox-exec：只在 macOS 上跑（别的系统跳过，写明原因）。每一关见 sandbox_cases.py。

这些测试真的建沙箱、真的运行命令，但都在临时目录里，不碰你的家目录和智狐的数据；"联网"只连本机的一个临时网页服务器。
智狐自己就在一个沙箱里运行时（比如从别的沙箱里启动的），sandbox-exec 建不了新的沙箱：跳过，原因写在跳过的说明里。
"""

from __future__ import annotations

import sys

import pytest
from fake_sandbox import REAL_DETECT
from sandbox_cases import CASES, lab, use_real_backend  # noqa: F401  lab 是 fixture

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec 只有 macOS 上有")


@pytest.fixture(autouse=True)
def real_backend(monkeypatch):
    if sys.platform == "darwin":
        problem = REAL_DETECT().problem()
        if problem is not None:
            pytest.skip(f"这台 Mac 上 sandbox-exec 用不了：{problem}")
    use_real_backend(monkeypatch)


@pytest.mark.parametrize("case", CASES, ids=[case.__name__.removeprefix("check_") for case in CASES])
def test_sandbox_exec(lab, case):  # noqa: F811
    case(lab)


def test_setuid_programs_cannot_run_inside(lab):  # noqa: F811
    # sudo、ps 这种带特殊权限的程序在沙箱里运行不了（README 里写着）
    out = lab.shell.run("/bin/ps -p $$")
    assert "[退出码" in out or "Operation not permitted" in out
