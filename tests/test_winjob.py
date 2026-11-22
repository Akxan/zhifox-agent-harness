"""Windows 的作业对象（harness/winjob.py）：结束一个作业，里面的子孙进程一个不剩，中间那一层先退出了也一样。

真的进程只在 Windows 上测（CI 的 windows-latest）；别的系统上只看它什么都不做。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest
from processes import alive

from harness import winjob

needs_windows = pytest.mark.skipif(os.name != "nt", reason="作业对象只有 Windows 上有")


def _until(check, timeout: float = 10, what: str = "条件") -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, f"等不到{what}"
        time.sleep(0.05)


def test_off_windows_a_job_does_nothing(monkeypatch):
    monkeypatch.setattr(winjob, "WINDOWS", False)
    job = winjob.Job(kill_on_close=True)
    assert job.handle is None
    assert job.add(object()) is False  # type: ignore[arg-type]
    job.terminate()
    job.close()


def _parent_that_leaves_a_grandchild(tmp_path):
    """开一个 Python，它再开一个睡 60 秒的孙进程，把孙进程的编号写进文件，然后自己马上退出。"""
    pidfile = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(child.pid))\n"
    )
    return subprocess.Popen([sys.executable, "-c", script]), pidfile


@needs_windows
def test_terminate_ends_grandchildren_even_after_the_parent_is_gone(tmp_path):
    job = winjob.Job()
    process, pidfile = _parent_that_leaves_a_grandchild(tmp_path)
    assert job.add(process)
    process.wait(30)  # 中间那一层已经退出了：taskkill /T 顺着父进程找，已经找不到孙进程了
    _until(lambda: pidfile.is_file() and pidfile.read_text(encoding="utf-8").strip(), what="孙进程的编号")
    grandchild = int(pidfile.read_text(encoding="utf-8"))
    assert alive(grandchild)
    job.terminate()
    _until(lambda: not alive(grandchild), what="孙进程被结束")
    job.close()


@needs_windows
def test_closing_a_kill_on_close_job_ends_everything_in_it(tmp_path):
    job = winjob.Job(kill_on_close=True)
    process, pidfile = _parent_that_leaves_a_grandchild(tmp_path)
    assert job.add(process)
    process.wait(30)
    _until(lambda: pidfile.is_file() and pidfile.read_text(encoding="utf-8").strip(), what="孙进程的编号")
    grandchild = int(pidfile.read_text(encoding="utf-8"))
    job.close()  # 智狐崩溃退出时也是这样：句柄一关，系统结束里面所有的进程
    _until(lambda: not alive(grandchild), what="孙进程被结束")


@needs_windows
def test_an_ordinary_job_leaves_background_programs_alone_when_closed(tmp_path):
    """命令故意留在后台的程序（Start-Process）在命令结束、作业关掉以后照样在，和 macOS、Linux 上一样。"""
    job = winjob.Job()
    process, pidfile = _parent_that_leaves_a_grandchild(tmp_path)
    job.add(process)
    process.wait(30)
    _until(lambda: pidfile.is_file() and pidfile.read_text(encoding="utf-8").strip(), what="孙进程的编号")
    grandchild = int(pidfile.read_text(encoding="utf-8"))
    job.close()
    time.sleep(0.3)
    try:
        assert alive(grandchild)
    finally:
        subprocess.run(["taskkill", "/F", "/PID", str(grandchild)], capture_output=True, check=False)
