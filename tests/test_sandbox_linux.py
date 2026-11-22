"""真的 bubblewrap（bwrap）：只在 Linux 上跑。每一关见 sandbox_cases.py，和 macOS 的是同一组。

本机是 Mac，所以这些只在 CI 的 ubuntu 上跑（.github/workflows/tests.yml 里装好 bubblewrap，
并设 HARNESS_SANDBOX_REQUIRED=1：装了却建不了沙箱时算失败，而不是悄悄跳过）。
在没装 bwrap 的 Linux 上跳过，跳过的说明里写着怎么装。
"""

from __future__ import annotations

import os
import sys

import pytest
from fake_sandbox import REAL_DETECT
from sandbox_cases import CASES, lab, use_real_backend  # noqa: F401  lab 是 fixture

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="bubblewrap 只在 Linux 上有")


@pytest.fixture(autouse=True)
def real_backend(monkeypatch):
    if sys.platform.startswith("linux"):
        problem = REAL_DETECT().problem()
        if problem is not None:
            if os.environ.get("HARNESS_SANDBOX_REQUIRED") == "1":
                pytest.fail(f"CI 要求 bwrap 能用，可是：{problem}")
            pytest.skip(f"这台电脑上 bwrap 用不了：{problem}")
    use_real_backend(monkeypatch)


@pytest.mark.parametrize("case", CASES, ids=[case.__name__.removeprefix("check_") for case in CASES])
def test_bwrap(lab, case):  # noqa: F811
    case(lab)


def test_the_sandbox_cannot_see_other_processes(lab):  # noqa: F811
    # 自己的进程编号：沙箱里只看得到自己（看不到、也杀不掉智狐和别的程序），/proc 里也没有它们的环境变量
    out = lab.shell.run("ls /proc | grep -E '^[0-9]+$' | wc -l")
    assert int(out.strip()) <= 10, out  # bwrap 自己、bash、ls、grep、wc……外面的一个都不在
    assert "[退出码" in lab.shell.run(f"cat /proc/{os.getpid()}/environ")
