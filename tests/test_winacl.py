"""Windows 上"只有自己的账号能读"（harness/winacl.py）的测试。

前半部分在每个系统上都跑：假装是 Windows，看发给 icacls 的是什么命令、什么时候发、出错了怎么办。
后半部分只在 Windows 上跑（CI 的 windows-latest）：对着临时目录运行真的 icacls，再读回来看 ACL 是不是那个样子。
"""

from __future__ import annotations

import os
import subprocess

import pytest

from harness import safe, settings, winacl

SID = "S-1-5-21-1-2-3-1001"


@pytest.fixture
def windows(monkeypatch):
    """假装是 Windows，记下发出去的每一条命令。"""
    calls: list[list[str]] = []

    def runner(args):
        calls.append(args)
        if args[0].lower().endswith("whoami.exe"):
            return 0, f'"pc\\me","{SID}"\r\n'
        return 0, ""

    monkeypatch.setattr(winacl, "WINDOWS", True)
    monkeypatch.setattr(winacl, "runner", runner)
    winacl.forget()
    return calls


def test_a_folder_keeps_only_me_system_and_administrators_and_passes_it_down(windows, tmp_path):
    assert winacl.restrict(tmp_path / "data", folder=True)
    whoami, icacls = windows
    assert whoami[1:] == ["/user", "/fo", "csv", "/nh"]
    assert icacls[0].lower().endswith(os.path.join("system32", "icacls.exe"))  # 完整路径：PATH 里同名的程序冒充不了
    assert icacls[1:] == [
        str(tmp_path / "data"), "/inheritance:r",
        "/grant:r", f"*{SID}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F",
        "/q",
    ]


def test_a_file_gets_the_same_three_without_inheritance_flags(windows, tmp_path):
    winacl.restrict(tmp_path / ".env.tmp", folder=False)
    assert windows[-1][3:7] == ["/grant:r", f"*{SID}:F", "*S-1-5-18:F", "*S-1-5-32-544:F"]


def test_each_folder_is_set_once_per_run_but_files_every_time(windows, tmp_path):
    for _ in range(3):
        winacl.restrict(tmp_path / "data", folder=True)
        winacl.restrict(tmp_path / ".env.tmp", folder=False)  # 每次都是刚写好的新文件，要重新设
    icacls = [call for call in windows if call[0].lower().endswith("icacls.exe")]
    assert [call[1] for call in icacls] == [str(tmp_path / "data")] + [str(tmp_path / ".env.tmp")] * 3
    assert sum(call[0].lower().endswith("whoami.exe") for call in windows) == 1  # SID 只问一次


def test_when_icacls_fails_it_warns_and_carries_on(windows, monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(winacl, "runner", lambda args: (0, f'"pc\\me","{SID}"') if "whoami" in args[0] else (5, "拒绝访问。"))
    assert winacl.restrict(tmp_path / "data", folder=True) is False
    assert "icacls" in caplog.text and "拒绝访问" in caplog.text
    assert winacl.restrict(tmp_path / "data", folder=True) is False  # 没设成的不记下，下次还试


def test_without_a_sid_nothing_is_changed(windows, monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(winacl, "runner", lambda args: (1, "whoami 不在"))
    assert winacl.restrict(tmp_path / "data", folder=True) is False
    assert "SID" in caplog.text


def test_on_macos_and_linux_it_does_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(winacl, "WINDOWS", False)
    monkeypatch.setattr(winacl, "runner", lambda args: pytest.fail("不是 Windows 时不运行任何命令"))
    assert winacl.restrict(tmp_path, folder=True)


def test_private_dir_restricts_every_folder_it_makes(windows, tmp_path):
    (tmp_path / "old").mkdir()
    safe.private_dir(tmp_path / "old" / "data" / "logs")
    folders = [call[1] for call in windows if call[0].lower().endswith("icacls.exe")]
    assert folders == [str(tmp_path / "old" / "data"), str(tmp_path / "old" / "data" / "logs")]  # 本来就在的 old 不归我们管
    safe.private_dir(tmp_path / "old")  # 指名要的那一个，已经存在也收紧
    assert windows[-1][1] == str(tmp_path / "old")


def test_saving_a_key_restricts_the_new_env_before_it_replaces_the_old_one(windows, monkeypatch, tmp_path):
    env = tmp_path / ".env"
    seen: list[bool] = []
    real = winacl.restrict

    def watch(path, folder):
        seen.append(path.name == ".env.tmp" and path.is_file() and not env.exists())
        return real(path, folder)

    monkeypatch.setattr(winacl, "restrict", watch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings.save_api_key(env, "ANTHROPIC_API_KEY", "sk-ant-test-0000")
    assert seen == [True]  # 设的是临时文件，那时 .env 还没换上
    assert env.read_bytes() == b"ANTHROPIC_API_KEY=sk-ant-test-0000\n"  # Windows 上也不写成 \r\n


# ---- 真的 Windows ---------------------------------------------------------------

needs_windows = pytest.mark.skipif(os.name != "nt", reason="icacls 和访问控制列表只有 Windows 上有")


def _acl(path) -> str:
    return subprocess.run([winacl._system32("icacls.exe"), str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace", check=True).stdout


@needs_windows
def test_real_icacls_leaves_only_three_entries(monkeypatch, tmp_path):
    monkeypatch.setattr(winacl, "runner", winacl._run)
    winacl.forget()
    folder = tmp_path / "data"
    folder.mkdir()
    assert winacl.restrict(folder, folder=True)
    sid = winacl.current_sid()
    assert sid and sid.startswith("S-1-5-")
    listing = _acl(folder)
    lines = [line for line in listing.splitlines()[:-1] if line.strip()]  # 最后一行是"成功处理了 1 个文件"
    assert len(lines) == 3, listing  # 只剩三项：继承来的 Users、Authenticated Users 都没有了
    assert "(OI)(CI)" in listing and "(I)" not in listing  # 往下继承、自己不再继承上一层的
    # 里面新建的文件继承这三项
    (folder / "x.json").write_text("{}", encoding="utf-8")
    inner = _acl(folder / "x.json")
    assert len([line for line in inner.splitlines()[:-1] if line.strip()]) == 3, inner


@needs_windows
def test_real_private_dir_and_env_file(monkeypatch, tmp_path):
    monkeypatch.setattr(winacl, "runner", winacl._run)
    winacl.forget()
    safe.private_dir(tmp_path / "data")
    safe.write_file(tmp_path / "data" / "token", "abc\n")
    assert (tmp_path / "data" / "token").read_bytes() == b"abc\n"
    assert "(I)" in _acl(tmp_path / "data" / "token")  # 继承自数据目录
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings.save_api_key(tmp_path / ".env", "OPENAI_API_KEY", "sk-test-1234")
    listing = _acl(tmp_path / ".env")
    assert "(I)" not in listing and len([line for line in listing.splitlines()[:-1] if line.strip()]) == 3, listing
