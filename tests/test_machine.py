"""machine.py：助手程序所在的电脑是哪种系统。网页的状态里带着它（platform），页面照着它说"访达""文件资源管理器"还是"文件管理器"。"""

from __future__ import annotations

import pytest

from harness import machine


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("darwin", "mac"), ("win32", "windows"), ("linux", "linux"), ("freebsd14", "linux")],
)
def test_the_system_is_named_from_sys_platform(monkeypatch, platform, expected):
    # 只有这三个名字：页面只认识它们。别的类 Unix（FreeBSD……）和 Linux 一样，文件管理器是桌面环境自带的那个
    monkeypatch.setattr(machine.sys, "platform", platform)
    assert machine.system() == expected
    assert expected in (machine.MAC, machine.WINDOWS, machine.LINUX)
