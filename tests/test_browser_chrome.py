"""chrome.py 的测试：找到 Chrome、启动参数、启动（管道在 3、4 号上，密钥不交给它，自成一个进程组）、关闭。

不用真的 Chrome：tests/fake_chrome.py 是一个假装成 Chrome 的小脚本，经过真正的 chrome.launch()（os.posix_spawn）启动，
说的也是真正的管道协议，所以描述符怎么摆、环境变量怎么传、进程组怎么关，走的都是真实的代码路径。
用真的 Chrome 的测试在别处（要这台电脑上装着 Chrome）。

关闭的几个测试看的是"整个进程组"：假 Chrome 启动时先起一个孙进程（剧本里的 pidfile），测试检查它也被结束了。
测试结束时把所有还活着的进程都杀掉，不留在后台。
"""

from __future__ import annotations

import contextlib
import os
import stat
import time
from pathlib import Path

import fake_chrome
import pytest

from harness.browser import chrome
from harness.browser.cdp import CDPClosed
from harness.tools.shell import SECRET_ENV_VARS


def wait_until(check, timeout: float = 5, what: str = "条件") -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, f"等不到{what}"
        time.sleep(0.02)


def exists(pid: int) -> bool:
    """这个进程还在不在。已经退出、只是还没被收走的（僵尸）算不在了：它的父进程死了以后，由系统去收，
    有的环境（比如没有 init 的容器）收得很慢甚至不收，所以 Linux 上看一眼 /proc 里它的状态。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True  # 没有 /proc（macOS）：launchd 很快就会收走它
    return state != "Z"


@pytest.fixture
def launch(tmp_path):
    """用假 Chrome 走一遍真正的 chrome.launch()。剧本的写法见 tests/fake_chrome.py；pidfile=True 表示要一个孙进程。"""
    started: list[chrome.Chrome] = []
    pidfiles: list[Path] = []

    def run(headless: bool = True, on_close=None, **script) -> chrome.Chrome:
        if script.get("pidfile") is True:
            pidfiles.append(tmp_path / f"grandchild-{len(pidfiles)}.pid")
            script["pidfile"] = str(pidfiles[-1])
        started.append(
            chrome.launch(
                fake_chrome.executable(tmp_path), tmp_path / "profile", headless, on_close=on_close, extra_args=[fake_chrome.script_arg(tmp_path, **script)]
            )
        )
        return started[-1]

    yield run
    for browser in started:
        browser.kill()  # 对整个进程组：还活着的孙进程也在里面
        wait_until(lambda running=browser: not running.alive(), what="假 Chrome 退出")  # 顺便收掉，不留僵尸
        browser.connection.close()


def grandchild(browser: chrome.Chrome, tmp_path: Path, index: int = 0) -> int:
    """假 Chrome 起的那个孙进程的进程号（它在回答第一个命令之前就写好了）。"""
    browser.connection.call("Browser.getVersion", timeout=5)
    pid = int((tmp_path / f"grandchild-{index}.pid").read_text())
    assert exists(pid)
    return pid


# ---- 启动 ---------------------------------------------------------------------


def test_launch_talks_over_the_pipes_on_fds_3_and_4(launch):
    browser = launch()
    assert browser.connection.call("Browser.getVersion", timeout=5)["product"] == "FakeChrome/1.0"
    fds = browser.connection.call("Fake.process", timeout=5)["fds"]
    assert fds["3"]["kind"] == "fifo" and fds["4"]["kind"] == "fifo"
    assert [fds[str(fd)]["kind"] for fd in (0, 1, 2)] == ["chr", "chr", "chr"]  # 标准输入输出都接到 /dev/null
    # 我们这一头的两个描述符（"不继承"的）没有漏给 Chrome：漏过去的话，Chrome 退出时我们读不到结尾
    ours = {browser.connection._read_fd, browser.connection._write_fd}
    child = {int(fd) for fd in fds}
    assert not ours & child
    # 除了 0～4，Chrome 只可能拿到测试进程本来就标成"可以继承"的描述符（比如启动 pytest 的程序留下的）
    inheritable = set()
    for fd in range(256):
        with contextlib.suppress(OSError):
            if os.get_inheritable(fd):
                inheritable.add(fd)
    assert child - {0, 1, 2, 3, 4} <= inheritable


def test_launch_gives_chrome_its_own_process_group(launch):
    browser = launch()
    process = browser.connection.call("Fake.process", timeout=5)
    assert process["pid"] == browser.pid  # sh 用 exec 换成了 python：进程号没变
    assert process["pgid"] == browser.pid  # 自成一组：关的时候对整组发信号
    assert process["ppid"] == os.getpid()
    assert os.getpgid(0) != browser.pid


def test_launch_removes_api_keys_from_chromes_environment(launch, monkeypatch):
    for name in SECRET_ENV_VARS:
        monkeypatch.setenv(name, f"secret-{name.lower()}")
    monkeypatch.setenv("HARNESS_FAKE_CHROME_MARK", "kept")
    browser = launch()
    environ = browser.connection.call("Fake.environ", timeout=5)["environ"]
    assert not SECRET_ENV_VARS & set(environ)
    assert not any(value.startswith("secret-") for value in environ.values())
    assert environ["HARNESS_FAKE_CHROME_MARK"] == "kept"  # 别的环境变量照常传过去


def test_launch_passes_chromes_flags_first_and_extra_args_last(launch, tmp_path):
    browser = launch(headless=True)
    argv = browser.connection.call("Fake.argv", timeout=5)["argv"]
    expected = chrome.arguments(fake_chrome.executable(tmp_path), tmp_path / "profile", True)[1:]
    assert argv[: len(expected)] == expected
    assert len(argv) == len(expected) + 1 and argv[-1].startswith(fake_chrome.SCRIPT_FLAG)


def test_launch_makes_the_profile_folder_private(launch, tmp_path):
    (tmp_path / "profile").mkdir(mode=0o755)
    launch()
    assert stat.S_IMODE((tmp_path / "profile").stat().st_mode) == 0o700


def test_a_failed_launch_does_not_leak_pipes(tmp_path):
    before = fake_chrome.open_pipes()
    with pytest.raises(OSError):
        chrome.launch(tmp_path / "no-such-chrome", tmp_path / "profile", True)
    assert fake_chrome.open_pipes() == before


# ---- 关闭 ---------------------------------------------------------------------


def test_close_asks_chrome_to_quit_first(launch, tmp_path):
    record = tmp_path / "record.jsonl"
    browser = launch(record=str(record))
    browser.connection.call("Browser.getVersion", timeout=5)
    started = time.monotonic()
    browser.close()
    assert time.monotonic() - started < 1.5  # 它自己走了，不用等到发信号
    assert [message["method"] for message in fake_chrome.records(record)][-1] == "Browser.close"
    assert not browser.alive()
    assert browser.connection.closed


def test_close_ends_the_whole_process_group_when_chrome_will_not_quit(launch, tmp_path):
    browser = launch(ignore_close=True, pidfile=True)
    child = grandchild(browser, tmp_path)
    browser.close(wait=1.0)  # Browser.close 不管用 → 对整组发 SIGTERM
    assert not browser.alive()
    wait_until(lambda: not exists(child), what="孙进程退出")


def test_close_falls_back_to_sigkill(launch, tmp_path):
    browser = launch(ignore_close=True, ignore_term=True, pidfile=True)
    child = grandchild(browser, tmp_path)
    started = time.monotonic()
    browser.close(wait=0.6)  # 连 SIGTERM 也不理 → 最后 SIGKILL
    assert time.monotonic() - started < 3
    assert not browser.alive()
    wait_until(lambda: not exists(child), what="孙进程退出")


def test_kill_ends_the_whole_group_at_once(launch, tmp_path):
    browser = launch(ignore_term=True, pidfile=True)
    child = grandchild(browser, tmp_path)
    browser.kill()
    wait_until(lambda: not browser.alive(), what="Chrome 退出")
    wait_until(lambda: not exists(child), what="孙进程退出")


def test_a_polite_close_also_ends_children_left_behind(launch, tmp_path):
    browser = launch(pidfile=True)
    child = grandchild(browser, tmp_path)
    browser.close()
    assert not browser.alive()
    wait_until(lambda: not exists(child), timeout=1, what="孙进程退出")


def test_chrome_quits_by_itself_when_our_end_of_the_pipe_closes(launch):
    # 智狐被强行杀掉时，管道的这一头由系统关掉，效果和这里一样。这也说明我们写给 Chrome 的那一头没有漏给它自己：
    # 漏过去的话，它自己拿着一个写的口子，永远读不到结尾
    browser = launch()
    browser.connection.call("Browser.getVersion", timeout=5)
    browser.connection.close()
    wait_until(lambda: not browser.alive(), what="Chrome 自己退出")


def test_a_crashing_chrome_closes_the_connection_and_calls_on_close_once(launch):
    closes = []
    browser = launch(on_close=lambda: closes.append(1))
    browser.connection.call("Browser.getVersion", timeout=5)
    with pytest.raises(CDPClosed):
        browser.connection.call("Fake.exit", timeout=5)
    assert browser.connection.closed
    wait_until(lambda: not browser.alive(), what="Chrome 退出")
    browser.connection.close()
    assert closes == [1]


# ---- 找 Chrome ----------------------------------------------------------------


def runnable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_find_chrome_prefers_the_setting_over_the_environment(tmp_path, monkeypatch):
    setting, env = runnable(tmp_path / "setting-chrome"), runnable(tmp_path / "env-chrome")
    monkeypatch.setenv(chrome.PATH_ENV, str(env))
    assert chrome.find_chrome(str(setting)) == setting
    assert chrome.find_chrome(f"  {setting}  ") == setting  # 设置里多打的空格不算
    assert chrome.find_chrome("") == env
    assert chrome.find_chrome("   ") == env


def test_a_given_but_missing_chrome_is_none_not_something_else(tmp_path, monkeypatch):
    # 用户既然指定了，就该知道它不对：不悄悄换成环境变量里的、或者系统里装的那一个
    monkeypatch.setenv(chrome.PATH_ENV, str(runnable(tmp_path / "env-chrome")))
    assert chrome.find_chrome(str(tmp_path / "no-such-chrome")) is None
    monkeypatch.setenv(chrome.PATH_ENV, str(tmp_path / "also-missing"))
    assert chrome.find_chrome("") is None


def test_a_file_that_cannot_run_or_a_folder_is_not_chrome(tmp_path):
    plain = tmp_path / "plain"
    plain.write_text("", encoding="utf-8")
    plain.chmod(0o644)
    assert chrome.find_chrome(str(plain)) is None
    assert chrome.find_chrome(str(tmp_path)) is None


def test_on_linux_commands_on_the_path_come_before_fixed_locations(tmp_path, monkeypatch):
    found = runnable(tmp_path / "bin" / "chromium")
    monkeypatch.delenv(chrome.PATH_ENV, raising=False)
    monkeypatch.setattr(chrome.platform, "system", lambda: "Linux")
    monkeypatch.setattr(chrome.shutil, "which", lambda name: str(found) if name == "chromium" else None)
    assert chrome.find_chrome() == found


def test_needs_headless_only_on_linux_without_a_display(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(chrome.platform, "system", lambda: "Darwin")
    assert chrome.needs_headless() is False
    monkeypatch.setattr(chrome.platform, "system", lambda: "Linux")
    assert chrome.needs_headless() is True
    monkeypatch.setenv("DISPLAY", ":0")
    assert chrome.needs_headless() is False
    monkeypatch.delenv("DISPLAY")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert chrome.needs_headless() is False


def test_arguments_use_the_pipe_and_the_own_profile(tmp_path):
    executable, profile = tmp_path / "chrome", tmp_path / "profile"
    args = chrome.arguments(executable, profile, headless=False)
    assert args[0] == str(executable)  # argv[0]
    assert "--remote-debugging-pipe" in args
    assert f"--user-data-dir={profile}" in args
    assert not any(arg.startswith("--remote-debugging-port") for arg in args)  # 不开端口：谁都能连的口子
    assert "--headless=new" not in args
    assert args[-1] == "about:blank"
    headless = chrome.arguments(executable, profile, headless=True)
    assert "--headless=new" in headless
    assert set(headless) - set(args) == {"--headless=new"}
