"""后台服务（`harness service …`）的测试：生成的 plist 和 unit、发给 launchctl / systemctl 的每一条命令、
装两次、卸载、被别人占着数据目录时不装、读状态、打开网页、看日志，以及数据目录的锁（instance.py）。

绝不在这台电脑上真的装上、开起一个服务：那会改动你的系统，还可能对着你真正的数据再开一个智狐。
所以这里的"系统"全是假的：FakeLaunchctl、FakeSystemctl 只是记下收到的命令、按测试设好的样子回答；
家目录是临时文件夹（conftest 把 Path.home 换掉了）；conftest 还把真的 run_command、run_attached 和打开浏览器
换成了一调用就报错的函数，万一哪里漏了假的，测试会当场失败，而不是真的去运行 launchctl。

"服务开起来了"在这里是这样假装的：在测试自己的进程里占住数据目录的锁（kind 写成 service），写好 .web-address，
再让假的 launchctl list 报出同一个进程编号。智狐判断"开起来没有"看的正是这三样（见 service/cli.py 的 _up）。

只有最后的 test_the_service_command_line_really_runs 开了一个真的进程：它运行的是 plist 里写的那条命令
（python -P -m harness serve --web --service launchd），但不经过 launchd，只是像 launchd 那样把输出接到日志文件上。
"""

from __future__ import annotations

import argparse
import dataclasses
import io
import json
import logging
import os
import plistlib
import re
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from test_channels import until

from harness import cli, filelock, instance, log
from harness import config as config_module
from harness.channels import cli as channels_cli
from harness.config import SERVICE_LOG_BACKUPS, SERVICE_RESTART_SECONDS
from harness.service import cli as service_cli
from harness.service import launchd, logs, systemd
from harness.service.base import Result
from harness.service.launchd import LABEL, Launchd
from harness.service.plan import Plan, make_plan, problem, protected_folders, shell_only
from harness.service.systemd import UNIT, Systemd
from harness.service.windows import TaskScheduler
from harness.webui import server

ROOT = Path(__file__).resolve().parent.parent
PATH = "/usr/bin:/bin"
TOKEN = "s3cr3t-T0ken_abc"
URL = f"http://127.0.0.1:8765/?token={TOKEN}"
OTHER_PID = 424242  # "另一个进程"的编号：写进 .lock 的说明里，用来假装服务重启以后换了进程


# ---- 假的系统 --------------------------------------------------------------------


def hold(data_dir: Path, pid: int | None = None, kind: str = "service", note: bool = True):
    """像另一个智狐那样占住数据目录：锁住 .lock，写上 {pid, kind}。pid 不给就是这个测试进程自己。
    note=False 是旧版本的智狐：只锁住，不写是谁。返回打开着的文件，关掉就放开了。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    handle = open(data_dir / instance.LOCK_NAME, "a+")  # noqa: SIM115 —— 测试结束时关
    filelock.lock(handle, wait=False)
    handle.truncate(0)
    if note:
        handle.write(json.dumps({"pid": os.getpid() if pid is None else pid, "kind": kind}) + "\n")
        handle.flush()
    return handle


class FakeLaunchctl:
    """假的 launchctl：记下每一条命令（完整的参数列表），按现在的"样子"回答。

    loaded / pid / status 就是 launchctl list 里自己那一行；bootstrap 读进来、bootout 卸下。
    starts 为真时，bootstrap / kickstart 顺便"开起来"一个服务（见文件开头的说明），bootout 让它退出、放开数据目录。
    """

    def __init__(self, data_dir: Path, starts: bool = False):
        self.data_dir = data_dir
        self.starts = starts
        self.calls: list[list[str]] = []
        self.loaded = False
        self.pid: int | None = None
        self.status = 0
        self.lock = None
        self.runs = 0
        self.fail: dict[str, Result] = {}  # 某个子命令要失败时，回答什么

    def __call__(self, args: list[str]) -> Result:
        assert args[0] == "/bin/launchctl", args
        self.calls.append(list(args))
        verb = args[1]
        if verb in self.fail:
            return self.fail[verb]
        if verb == "list":
            rows = ["PID\tStatus\tLabel", "-\t0\tcom.apple.Safari.History", "311\t0\tcom.apple.Finder"]
            if self.loaded:
                rows.append(f"{self.pid or '-'}\t{self.status}\t{LABEL}")
            return Result(0, "\n".join(rows) + "\n")
        if verb in ("bootstrap", "load"):
            self.loaded = True
            self.up()
        elif verb in ("bootout", "unload"):
            if not self.loaded:
                return Result(3, "", "Boot-out failed: 3: No such process")
            self.loaded = False
            self.down()
        elif verb in ("kickstart", "start"):
            if "-k" in args:
                self.down()
            self.up(OTHER_PID if "-k" in args else None)
        return Result(0)

    def up(self, pid: int | None = None) -> None:
        if not self.starts:
            return
        self.down()
        self.runs += 1  # 第一次开起来是这个测试进程自己的编号；以后每次重新开起来都换一个（真的服务也是这样）
        self.pid = pid or (os.getpid() if self.runs == 1 else 500_000 + self.runs)
        self.lock = hold(self.data_dir, self.pid)
        (self.data_dir / ".web-address").write_text(URL + "\n", encoding="utf-8")

    def down(self) -> None:
        if self.lock is not None:
            self.lock.close()
            self.lock = None
        self.pid = None
        (self.data_dir / ".web-address").unlink(missing_ok=True)

    def verbs(self) -> list[list[str]]:
        """除了查状态的 list 以外的命令。"""
        return [call[1:] for call in self.calls if call[1] != "list"]


class FakeSystemctl:
    """假的 systemctl --user 和 journalctl。show 的回答照着现在的样子写；enable --now / start / restart 可以顺便"开起来"。"""

    def __init__(self, data_dir: Path, unit_file: Path, starts: bool = False):
        self.data_dir = data_dir
        self.unit_file = unit_file
        self.starts = starts
        self.calls: list[list[str]] = []
        self.active, self.sub, self.pid = "inactive", "dead", 0
        self.code, self.status = 0, 0
        self.enabled = False
        self.journal = ""
        self.lock = None
        self.runs = 0
        self.fail: dict[str, Result] = {}
        self.attached: list[list[str]] = []

    def __call__(self, args: list[str]) -> Result:
        self.calls.append(list(args))
        if args[0] == "journalctl":
            return Result(0, self.journal)
        assert args[:2] == ["systemctl", "--user"], args
        verb = args[2]
        if verb in self.fail:
            return self.fail[verb]
        if verb == "show":
            load = "loaded" if self.unit_file.is_file() else "not-found"
            return Result(0, "\n".join([
                f"LoadState={load}", f"ActiveState={self.active}", f"SubState={self.sub}", f"MainPID={self.pid}",
                f"ExecMainCode={self.code}", f"ExecMainStatus={self.status}",
                f"UnitFileState={'enabled' if self.enabled else 'disabled'}",
            ]) + "\n")
        if verb == "enable":
            self.enabled = True
            if "--now" in args:
                self.up()
        elif verb == "disable":
            self.enabled = False
            if "--now" in args:
                self.down()
        elif verb == "start":
            self.up()
        elif verb == "stop":
            self.down()
        elif verb == "restart":
            self.down()
            self.up(OTHER_PID)
        return Result(0)

    def attach(self, args: list[str]) -> int:
        self.attached.append(list(args))
        return 0

    def up(self, pid: int | None = None) -> None:
        self.active, self.sub = "active", "running"
        if not self.starts:
            return
        self.down()
        self.active, self.sub = "active", "running"
        self.runs += 1
        self.pid = pid or (os.getpid() if self.runs == 1 else 500_000 + self.runs)
        self.lock = hold(self.data_dir, self.pid)
        (self.data_dir / ".web-address").write_text(URL + "\n", encoding="utf-8")

    def down(self) -> None:
        if self.lock is not None:
            self.lock.close()
            self.lock = None
        self.active, self.sub, self.pid = "inactive", "dead", 0
        (self.data_dir / ".web-address").unlink(missing_ok=True)

    def verbs(self) -> list[list[str]]:
        return [call[2:] for call in self.calls if call[0] == "systemctl" and call[2] != "show"]


class Clock:
    """假的钟：sleep 不真的等，只把时间往前拨；还可以在拨到某一刻时做一件事（"这时服务开起来了"）。"""

    def __init__(self):
        self.now = 0.0
        self.at: list[tuple[float, object]] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        for moment, action in list(self.at):
            if self.now >= moment:
                self.at.remove((moment, action))
                action()


def make_system(manager, home: Path, environ: dict | None = None, **options) -> service_cli.System:
    clock = options.pop("clock", None) or Clock()
    return service_cli.System(
        manager, out=io.StringIO(), err=io.StringIO(), home=home, clock=clock, sleep=clock.sleep,
        environ={"PATH": PATH, "ANTHROPIC_API_KEY": "sk-ant-in-env"} if environ is None else environ,
        start_wait=options.pop("start_wait", 0), stop_wait=options.pop("stop_wait", 0), **options,
    )


def said(system: service_cli.System) -> tuple[str, str]:
    return system.out.getvalue(), system.err.getvalue()


def args_for(*argv: str) -> argparse.Namespace:
    """用真正的命令行定义解析 `harness service …` 的参数。"""
    parser = argparse.ArgumentParser()
    service_cli.add_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(["service", *argv])


@pytest.fixture
def mac(tmp_path, make_config):
    """一台假的 Mac：临时的家目录、假的 launchctl（装上就"开起来"）、智狐的设置。"""
    config = make_config()
    home = tmp_path / "home"
    home.mkdir()
    fake = FakeLaunchctl(config.data_dir, starts=True)
    manager = Launchd(home, fake, uid=501, version=(15, 0))
    system = make_system(manager, home)
    yield type("Mac", (), {"config": config, "home": home, "fake": fake, "manager": manager, "system": system})
    fake.down()


@pytest.fixture
def linux(tmp_path, make_config, monkeypatch):
    """一台假的 Linux：临时的家目录、假的 systemctl 和 journalctl、一个空的 linger 文件夹。"""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    config = make_config()
    home = tmp_path / "home"
    home.mkdir()
    linger = tmp_path / "linger"
    linger.mkdir()
    unit = tmp_path / "xdg" / "systemd" / "user" / UNIT
    fake = FakeSystemctl(config.data_dir, unit, starts=True)
    manager = Systemd(home, fake, fake.attach, "systemctl", "journalctl", user="tester", linger_dir=linger, config_home=tmp_path / "xdg")
    system = make_system(manager, home)
    yield type("Linux", (), {"config": config, "home": home, "fake": fake, "manager": manager, "system": system, "linger": linger})
    fake.down()


@pytest.fixture
def held():
    """测试里占着的锁，测完一起放开。"""
    handles: list = []
    yield handles
    for handle in handles:
        handle.close()


def simple_plan(**changes) -> Plan:
    return Plan(**{
        "python": "/opt/zf/.venv/bin/python", "workspace": Path("/home/me/work"), "data_dir": Path("/home/me/zf/data"),
        "env_file": Path("/home/me/zf/.env"), "path": PATH, "lang": "C.UTF-8", **changes,
    })


# ---- plist（macOS）---------------------------------------------------------------


def test_the_plist_has_exactly_what_launchd_needs(make_config):
    config = make_config()
    plan = make_plan(config, environ={"PATH": PATH, "LANG": "zh_CN.UTF-8"})
    info = plistlib.loads(launchd.render(plan))
    assert set(info) == {
        "Label", "ProgramArguments", "WorkingDirectory", "EnvironmentVariables", "RunAtLoad", "KeepAlive",
        "ThrottleInterval", "StandardOutPath", "StandardErrorPath",
    }
    assert info["Label"] == "io.github.akxan.zhifox"
    # 虚拟环境里的 python，-P：不把当前目录（工作目录，里面是你的文件）放进找模块的地方
    assert info["ProgramArguments"] == [
        os.path.abspath(sys.executable), "-P", "-m", "harness", "--workspace", str(config.workspace), "serve", "--web", "--service", "launchd",
    ]
    assert info["WorkingDirectory"] == str(config.workspace)
    assert info["RunAtLoad"] is True
    assert info["KeepAlive"] == {"SuccessfulExit": False}  # 正常退出（stop、SIGTERM）不再开；崩溃、出错退出才再开
    assert info["ThrottleInterval"] == SERVICE_RESTART_SECONDS
    log_file = str(config.data_dir / "logs" / "service.log")
    assert info["StandardOutPath"] == info["StandardErrorPath"] == log_file
    assert info["EnvironmentVariables"] == {
        "HARNESS_DATA_DIR": str(config.data_dir), "HARNESS_ENV_FILE": str(config.env_file),
        "PATH": PATH, "LANG": "zh_CN.UTF-8", "PYTHONUTF8": "1",
    }


def test_web_and_port_options_change_only_the_command(make_config):
    config = make_config()
    quiet = plistlib.loads(launchd.render(make_plan(config, web=False, environ={})))["ProgramArguments"]
    assert "--web" not in quiet and quiet[-2:] == ["--service", "launchd"]
    ported = plistlib.loads(launchd.render(make_plan(config, port=9123, environ={})))["ProgramArguments"]
    assert ported[ported.index("serve"):] == ["serve", "--web", "--port", "9123", "--service", "launchd"]
    # 没有 PATH、LANG 的时候就不写这两项
    assert set(plistlib.loads(launchd.render(make_plan(config, environ={})))["EnvironmentVariables"]) == {"HARNESS_DATA_DIR", "HARNESS_ENV_FILE", "PYTHONUTF8"}


def test_the_plist_holds_no_secrets_and_is_644(mac):
    """API key、机器人的 token、带密码的代理都不进 plist：密钥留在 .env 里，服务启动时自己读。"""
    mac.system.environ = {
        "PATH": PATH, "ANTHROPIC_API_KEY": "sk-ant-api03-SECRETSECRET", "TELEGRAM_BOT_TOKEN": "123456:SECRETBOT",
        "HTTPS_PROXY": "http://me:proxy-password@proxy.local:3128",
    }
    assert service_cli.run(args_for("install"), mac.config, mac.system) == 0
    content = mac.manager.file.read_bytes()
    for secret in (b"SECRETSECRET", b"SECRETBOT", b"proxy-password", b"token"):
        assert secret not in content.lower() if secret == b"token" else secret not in content
    assert stat.S_IMODE(mac.manager.file.stat().st_mode) == 0o644
    out, err = said(mac.system)
    assert "proxy-password" not in out + err and "SECRET" not in out + err
    assert "HTTPS_PROXY" in err  # 只说名字：它只在终端里设置了，服务读不到


@pytest.mark.skipif(shutil.which("plutil") is None, reason="只有 macOS 上有 plutil")
def test_plutil_accepts_the_plist(tmp_path, make_config):
    workspace = tmp_path / "我的 工作 & <目录>"
    workspace.mkdir()
    path = tmp_path / "zhifox.plist"
    path.write_bytes(launchd.render(make_plan(make_config(workspace=workspace), environ={"PATH": PATH})))
    checked = subprocess.run(["plutil", "-lint", str(path)], capture_output=True, text=True, check=False)
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_the_plist_reads_back_to_the_same_plan(tmp_path, make_config):
    workspace = tmp_path / "个人项目" / "Harness Project"  # 和你的项目一样：有空格，有中文
    workspace.mkdir(parents=True)
    for plan in (
        make_plan(make_config(workspace=workspace), environ={"PATH": PATH, "LANG": "en_US.UTF-8"}),
        make_plan(make_config(workspace=workspace), web=False, port=9000, environ={}),
    ):
        assert launchd.parse(launchd.render(plan)) == plan
    assert launchd.parse(b"not a plist") is None
    assert launchd.parse(plistlib.dumps({"Label": LABEL})) is None  # 不是我们写的样子


# ---- unit（Linux）----------------------------------------------------------------


def test_the_unit_file_is_exactly_this():
    assert systemd.render(simple_plan()) == (
        "[Unit]\n"
        "Description=ZhiFox (智狐)\n"
        "Documentation=https://github.com/Akxan/zhifox-agent-harness#run-it-in-the-background\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "WorkingDirectory=/home/me/work\n"
        'ExecStart="/opt/zf/.venv/bin/python" "-P" "-m" "harness" "--workspace" "/home/me/work" "serve" "--web" "--service" "systemd"\n'
        'Environment="HARNESS_DATA_DIR=/home/me/zf/data"\n'
        'Environment="HARNESS_ENV_FILE=/home/me/zf/.env"\n'
        'Environment="LANG=C.UTF-8"\n'
        f'Environment="PATH={PATH}"\n'
        'Environment="PYTHONUTF8=1"\n'
        "Restart=on-failure\n"
        "RestartSec=10\n"
        "RestartPreventExitStatus=2\n"  # 退出码 2：设置有问题，重启也没用（serve.py 的 EXIT_CONFIG）
        "KillMode=mixed\n"
        "TimeoutStopSec=30\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def test_the_unit_quotes_spaces_percent_dollar_and_quotes():
    """% 是 systemd 的占位符（%h……），ExecStart 里的 $ 是变量（$HOME），" 会结束引号：都要转义，读回来要一字不差。"""
    plan = simple_plan(workspace=Path('/home/me/50% off $HOME "my" dir'), path="/a%b:$c", port=9000, web=False)
    text = systemd.render(plan)
    assert "WorkingDirectory=/home/me/50%% off $HOME \"my\" dir\n" in text
    assert '"--workspace" "/home/me/50%% off $$HOME \\"my\\" dir"' in text
    assert 'Environment="PATH=/a%%b:$c"\n' in text  # Environment= 里的 $ 不是变量，不用写成 $$
    assert '"--port" "9000"' in text and '"--web"' not in text
    assert systemd.parse(text) == plan
    assert systemd.parse(systemd.render(simple_plan())) == simple_plan()
    assert systemd.parse('ExecStart="unbalanced\n') is None
    assert systemd.parse("[Service]\nType=simple\n") is None


def test_paths_that_cannot_be_written_into_a_service_are_refused(tmp_path):
    workspace = tmp_path / "w"
    workspace.mkdir()
    good = simple_plan(python=sys.executable, workspace=workspace)
    assert problem(good) is None
    assert "反斜杠" in problem(dataclasses.replace(good, data_dir=Path("/home/me/a\\b")))
    assert "反斜杠" in problem(dataclasses.replace(good, env_file=Path("/home/me/line\nbreak/.env")))
    assert "找不到 Python" in problem(dataclasses.replace(good, python=str(tmp_path / "no-python")))
    assert "工作目录不存在" in problem(dataclasses.replace(good, workspace=tmp_path / "missing"))
    assert "1 到 65535" in problem(dataclasses.replace(good, port=70000))
    assert problem(dataclasses.replace(good, port=8765)) is None


# ---- 发给 launchctl 的命令 ---------------------------------------------------------


def test_install_on_mac_bootstraps_the_agent_and_waits_until_it_is_up(mac):
    assert service_cli.run(args_for("install"), mac.config, mac.system) == 0
    plist = str(mac.home / "Library" / "LaunchAgents" / f"{LABEL}.plist")
    assert mac.fake.calls[0] == ["/bin/launchctl", "list"]
    assert mac.fake.verbs() == [["enable", f"gui/501/{LABEL}"], ["bootstrap", "gui/501", plist]]
    out, err = said(mac.system)
    assert "已经装好了" in out and plist in out and str(mac.config.data_dir / "logs" / "service.log") in out
    assert f"已经开起来了（进程 {os.getpid()}）" in out and "uv run harness service open" in out
    assert TOKEN not in out + err
    assert (mac.config.data_dir / "logs").is_dir() and stat.S_IMODE((mac.config.data_dir / "logs").stat().st_mode) == 0o700


def test_mac_stop_start_and_restart(mac):
    service_cli.run(args_for("install"), mac.config, mac.system)
    mac.fake.calls.clear()
    assert service_cli.run(args_for("stop"), mac.config, mac.system) == 0
    assert mac.fake.verbs() == [["bootout", f"gui/501/{LABEL}"]]  # 停下就是卸下；plist 还在，下次登录又会开起来
    assert mac.manager.file.is_file() and not instance.in_use(mac.config.data_dir)
    assert "下次登录时会自动启动" in said(mac.system)[0]

    assert service_cli.run(args_for("stop"), mac.config, mac.system) == 0
    assert "本来就没有在运行" in said(mac.system)[0]

    mac.fake.calls.clear()
    assert service_cli.run(args_for("start"), mac.config, mac.system) == 0  # 没读进来：bootstrap
    assert mac.fake.verbs() == [["enable", f"gui/501/{LABEL}"], ["bootstrap", "gui/501", str(mac.manager.file)]]
    mac.fake.calls.clear()
    assert service_cli.run(args_for("start"), mac.config, mac.system) == 0
    assert mac.fake.verbs() == [] and "已经在运行了" in said(mac.system)[0]

    mac.fake.calls.clear()
    assert service_cli.run(args_for("restart"), mac.config, mac.system) == 0
    assert mac.fake.verbs() == [["kickstart", "-k", f"gui/501/{LABEL}"]]
    assert f"已经开起来了（进程 {OTHER_PID}）" in said(mac.system)[0]  # 等到的是新的那个进程


def test_start_when_loaded_but_not_running_kickstarts(mac):
    service_cli.run(args_for("install"), mac.config, mac.system)
    mac.fake.down()  # 退出了，但还读进着（比如你 kill 了它，它正常退出了）
    mac.fake.calls.clear()
    assert service_cli.run(args_for("start"), mac.config, mac.system) == 0
    assert mac.fake.verbs() == [["kickstart", f"gui/501/{LABEL}"]]


def test_older_macos_uses_load_and_unload(mac):
    old = Launchd(mac.home, mac.fake, uid=501, version=(10, 9, 5))
    system = make_system(old, mac.home)
    plist = str(old.file)
    assert service_cli.run(args_for("install"), mac.config, system) == 0
    assert mac.fake.verbs() == [["load", plist]]
    mac.fake.calls.clear()
    assert service_cli.run(args_for("restart"), mac.config, system) == 0
    assert mac.fake.verbs() == [["unload", plist], ["load", plist]]
    mac.fake.calls.clear()
    assert service_cli.run(args_for("uninstall"), mac.config, system) == 0
    assert mac.fake.verbs() == [["unload", plist]] and not old.file.exists()


def test_a_failed_bootstrap_is_reported(mac):
    mac.fake.fail["bootstrap"] = Result(5, "", "Bootstrap failed: 5: Input/output error")
    assert service_cli.run(args_for("install"), mac.config, mac.system) == 1
    _, err = said(mac.system)
    assert "没能启动它：Bootstrap failed: 5: Input/output error" in err and str(mac.manager.file) in err


# ---- 发给 systemctl 的命令 ---------------------------------------------------------


def test_install_on_linux_enables_the_user_unit(linux):
    assert service_cli.run(args_for("install"), linux.config, linux.system) == 0
    assert linux.fake.verbs() == [["daemon-reload"], ["daemon-reload"], ["enable", "--now", UNIT]]
    text = linux.manager.file.read_text(encoding="utf-8")
    assert '"--service" "systemd"' in text and stat.S_IMODE(linux.manager.file.stat().st_mode) == 0o644
    out, err = said(linux.system)
    assert "智狐的后台服务（systemd）：已经装好了" in out and f"已经开起来了（进程 {os.getpid()}）" in out
    assert "journalctl --user -u zhifox" in out
    assert "sudo loginctl enable-linger tester" in err  # 没开 linger：退出登录就会停


def test_linux_stop_start_restart_and_uninstall(linux):
    service_cli.run(args_for("install"), linux.config, linux.system)
    linux.fake.calls.clear()
    assert service_cli.run(args_for("stop"), linux.config, linux.system) == 0
    assert service_cli.run(args_for("start"), linux.config, linux.system) == 0
    assert service_cli.run(args_for("restart"), linux.config, linux.system) == 0
    assert linux.fake.verbs() == [["stop", UNIT], ["start", UNIT], ["restart", UNIT]]
    linux.fake.calls.clear()
    assert service_cli.run(args_for("uninstall"), linux.config, linux.system) == 0
    assert linux.fake.verbs() == [["disable", "--now", UNIT], ["daemon-reload"], ["reset-failed", UNIT]]
    assert not linux.manager.file.exists()
    assert "已经卸载了智狐的后台服务" in said(linux.system)[0]


def test_linux_without_a_user_manager_installs_nothing(linux):
    linux.fake.fail["daemon-reload"] = Result(1, "", "Failed to connect to bus: No medium found")
    assert service_cli.run(args_for("install"), linux.config, linux.system) == 1
    assert not linux.manager.file.exists()
    assert "Failed to connect to bus" in said(linux.system)[1]


def test_linux_linger_is_mentioned_only_when_off(linux):
    (linux.linger / "tester").touch()
    service_cli.run(args_for("install"), linux.config, linux.system)
    assert "enable-linger" not in said(linux.system)[1]
    service_cli.run(args_for("status"), linux.config, linux.system)
    assert "已经开了 linger" in said(linux.system)[0]


# ---- 装两次、卸载 -------------------------------------------------------------------


def test_installing_again_with_the_same_settings_changes_nothing(mac):
    service_cli.run(args_for("install"), mac.config, mac.system)
    written = mac.manager.file.stat().st_mtime_ns
    mac.fake.calls.clear()
    system = make_system(mac.manager, mac.home)
    assert service_cli.run(args_for("install"), mac.config, system) == 0
    assert mac.fake.verbs() == [] and mac.manager.file.stat().st_mtime_ns == written
    assert "已经装好了，设置没有变，正在运行" in said(system)[0]


def test_installing_with_new_settings_replaces_the_old_service(mac):
    service_cli.run(args_for("install"), mac.config, mac.system)
    mac.fake.calls.clear()
    system = make_system(mac.manager, mac.home)
    assert service_cli.run(args_for("install", "--no-web", "--port", "9001"), mac.config, system) == 0
    # 先停下旧的（放开数据目录），再写新的、读进来
    assert mac.fake.verbs() == [["bootout", f"gui/501/{LABEL}"], ["enable", f"gui/501/{LABEL}"], ["bootstrap", "gui/501", str(mac.manager.file)]]
    arguments = plistlib.loads(mac.manager.file.read_bytes())["ProgramArguments"]
    assert "--web" not in arguments and arguments[arguments.index("--port") + 1] == "9001"
    out, _ = said(system)
    assert "换成了这次的设置，重新开起来了" in out and "没有开网页界面" in out


def test_install_repairs_a_service_that_is_not_running(mac):
    """设置没变、但它没在运行（比如一直起不来）：再装一次就是停下、重新读进来。"""
    service_cli.run(args_for("install"), mac.config, mac.system)
    mac.fake.down()
    mac.fake.status = 1
    mac.fake.calls.clear()
    system = make_system(mac.manager, mac.home)
    assert service_cli.run(args_for("install"), mac.config, system) == 0
    assert [verb[0] for verb in mac.fake.verbs()] == ["bootout", "enable", "bootstrap"]
    assert "设置没有变，重新开起来了" in said(system)[0]


def test_install_takes_a_workspace_option(mac, tmp_path):
    elsewhere = tmp_path / "另一个 文件夹"
    elsewhere.mkdir()
    assert service_cli.run(args_for("install", "--workspace", str(elsewhere)), mac.config, mac.system) == 0
    info = plistlib.loads(mac.manager.file.read_bytes())
    assert info["WorkingDirectory"] == str(elsewhere.resolve())
    assert service_cli.run(args_for("install", "--workspace", str(tmp_path / "missing")), mac.config, make_system(mac.manager, mac.home)) == 2


def test_uninstall_removes_the_plist_and_a_stale_address(mac):
    service_cli.run(args_for("install"), mac.config, mac.system)
    mac.fake.starts = False  # 下面让它"被强行结束"：bootout 以后 .web-address 还留着
    address = mac.config.data_dir / ".web-address"
    mac.fake.lock.close()
    mac.fake.lock = None
    address.write_text(URL + "\n", encoding="utf-8")
    mac.fake.calls.clear()
    assert service_cli.run(args_for("uninstall"), mac.config, mac.system) == 0
    assert mac.fake.verbs() == [["bootout", f"gui/501/{LABEL}"]]
    assert not mac.manager.file.exists() and not address.exists()  # 口令也不留着
    out, _ = said(mac.system)
    assert "已经卸载了智狐的后台服务" in out and str(mac.config.data_dir) in out  # 数据和日志都还在


def test_uninstall_when_nothing_is_installed_is_harmless(mac):
    assert service_cli.run(args_for("uninstall"), mac.config, mac.system) == 0
    assert mac.fake.verbs() == [] and "没有装后台服务，不用卸载" in said(mac.system)[0]
    assert service_cli.run(args_for("stop"), mac.config, mac.system) == 0
    assert service_cli.run(args_for("start"), mac.config, mac.system) == 1
    assert service_cli.run(args_for("restart"), mac.config, mac.system) == 1
    assert "还没有装后台服务" in said(mac.system)[1]


# ---- 数据目录被别人占着 ------------------------------------------------------------


def test_install_refuses_while_another_zhifox_uses_the_data_folder(mac, held):
    """你在终端里（或者 Claude Code 里）开着 harness --web：先不装，告诉你是谁、怎么关掉它。"""
    held.append(instance.claim(mac.config.data_dir, "web"))
    assert service_cli.run(args_for("install"), mac.config, mac.system) == 1
    _, err = said(mac.system)
    assert "数据目录正被另一个智狐用着" in err and "harness --web" in err and f"进程 {os.getpid()}" in err
    assert "Ctrl-C" in err and "uv run harness service install" in err
    assert not mac.manager.file.exists() and mac.fake.verbs() == []


def test_start_and_restart_refuse_too(mac, held):
    service_cli.run(args_for("install"), mac.config, mac.system)
    mac.fake.starts = False
    mac.fake.down()
    mac.fake.loaded = False
    held.append(instance.claim(mac.config.data_dir, "serve"))
    mac.fake.calls.clear()
    system = make_system(mac.manager, mac.home)
    assert service_cli.run(args_for("start"), mac.config, system) == 1
    assert service_cli.run(args_for("restart"), mac.config, system) == 1
    assert mac.fake.verbs() == []
    assert "harness serve" in said(system)[1]


def test_an_old_zhifox_without_a_note_gets_the_generic_message(mac, held):
    held.append(hold(mac.config.data_dir, note=False))  # 旧版本只锁住、不写是谁
    assert service_cli.run(args_for("install"), mac.config, mac.system) == 1
    err = said(mac.system)[1]
    assert "数据目录正被另一个智狐用着" in err and "  是" not in err  # 不知道是谁，就不编一个出来


# ---- 读状态 -----------------------------------------------------------------------


def test_launchctl_list_rows_are_read_right(mac):
    mac.manager.file.parent.mkdir(parents=True)
    mac.manager.file.write_bytes(b"x")
    mac.fake.loaded, mac.fake.pid = True, 4242
    state = mac.manager.state()
    assert (state.installed, state.loaded, state.running, state.pid, state.exit_code, state.signal) == (True, True, True, 4242, 0, None)
    mac.fake.pid, mac.fake.status = None, 1
    state = mac.manager.state()
    assert (state.running, state.pid, state.exit_code) == (False, None, 1)
    mac.fake.status = -9  # 负数是结束它的信号
    state = mac.manager.state()
    assert (state.exit_code, state.signal) == (None, 9)
    mac.fake.loaded = False
    assert not mac.manager.state().loaded
    mac.fake.fail["list"] = Result(1, "", "nope")
    assert mac.manager.state() == mac.manager.state().__class__(True, enabled=True)


def test_systemctl_show_is_read_right(linux):
    linux.manager.file.parent.mkdir(parents=True)
    linux.manager.file.write_text("x", encoding="utf-8")
    fake = linux.fake
    fake.active, fake.sub, fake.pid, fake.enabled = "active", "running", 777, True
    state = linux.manager.state()
    assert (state.loaded, state.running, state.pid, state.enabled, state.restarting) == (True, True, 777, True, False)
    fake.active, fake.sub, fake.pid, fake.code, fake.status = "activating", "auto-restart", 0, 1, 1
    state = linux.manager.state()
    assert (state.running, state.restarting, state.exit_code) == (False, True, 1)
    fake.active, fake.sub, fake.code, fake.status = "failed", "failed", 1, 2
    state = linux.manager.state()
    assert (state.running, state.exit_code, state.signal) == (False, 2, None)
    fake.code, fake.status = 2, 9
    assert linux.manager.state().signal == 9
    fake.active, fake.sub, fake.code, fake.status = "inactive", "dead", 0, 0
    state = linux.manager.state()
    assert not state.running and state.exit_code is None and state.enabled


def test_status_when_running_shows_the_port_but_never_the_token(mac):
    service_cli.run(args_for("install"), mac.config, mac.system)
    system = make_system(mac.manager, mac.home)
    assert service_cli.run(args_for("status"), mac.config, system) == 0
    out, err = said(system)
    assert out.startswith(f"智狐的后台服务（launchd）：正在运行（进程 {os.getpid()}）")
    assert "网页：http://127.0.0.1:8765（打开：uv run harness service open）" in out
    assert str(mac.config.workspace) in out and str(mac.config.data_dir) in out and str(mac.manager.file) in out
    assert TOKEN not in out + err and "token" not in out + err


def test_status_shows_the_port_only_when_the_lock_is_the_services(mac, held):
    """服务管家说在运行，但数据目录的锁在别的进程手里（换了进程）：不说网页在哪个端口，那个地址不是它的。"""
    service_cli.run(args_for("install"), mac.config, mac.system)
    mac.fake.lock.close()
    held.append(hold(mac.config.data_dir, pid=OTHER_PID, kind="web"))
    system = make_system(mac.manager, mac.home)
    service_cli.run(args_for("status"), mac.config, system)
    out = said(system)[0]
    assert "8765" not in out and "网页：正在开" in out
    assert f"数据目录正被别的程序占着（在终端里开着的网页界面（harness --web），进程 {OTHER_PID}）" in out


def test_status_when_stopped_lists_the_recent_problems(mac):
    service_cli.run(args_for("install"), mac.config, mac.system)
    mac.fake.down()
    mac.fake.status = 1
    log_file = mac.config.data_dir / "logs" / "service.log"
    log_file.write_text(
        "2026-11-21 09:00:00 info 智狐已作为服务启动。\n"
        "2026-11-21 09:00:01 warning 还没有设置任何聊天通道。\n"
        "Traceback (most recent call last):\n"
        '  File "<frozen runpy>", line 198, in _run_module_as_main\n'
        "ModuleNotFoundError: No module named 'harness'\n"
        "2026-11-21 09:00:02 error 已经有一个智狐的网页界面在运行\n"
        "2026-11-21 09:00:03 info 再见。\n",
        encoding="utf-8",
    )
    system = make_system(mac.manager, mac.home)
    assert service_cli.run(args_for("status"), mac.config, system) == 3
    out = said(system)[0]
    assert "没在运行（上次：退出码 1：数据目录被别人占着，或者端口被占用）" in out
    problems = out[out.index("最近的警告和错误："):].splitlines()[1:4]
    assert problems == [
        "  2026-11-21 09:00:01 warning 还没有设置任何聊天通道。",
        "  ModuleNotFoundError: No module named 'harness'",
        "  2026-11-21 09:00:02 error 已经有一个智狐的网页界面在运行",
    ]
    assert "完整磁盘访问权限" not in out  # 没有被 macOS 拦下的迹象


def test_operation_not_permitted_brings_up_the_mac_folders_hint(mac):
    service_cli.run(args_for("install"), mac.config, mac.system)
    mac.fake.down()
    mac.fake.status = 1
    (mac.config.data_dir / "logs" / "service.log").write_text(
        "PermissionError: [Errno 1] Operation not permitted: '/Users/me/Documents/zhifox/.venv/pyvenv.cfg'\n", encoding="utf-8",
    )
    system = make_system(mac.manager, mac.home)
    service_cli.run(args_for("status"), mac.config, system)
    out = said(system)[0]
    assert "完整磁盘访问权限" in out and os.path.realpath(sys.executable) in out


def test_status_before_installing(mac, held):
    assert service_cli.run(args_for("status"), mac.config, mac.system) == 3
    assert "还没有装上" in said(mac.system)[0] and "uv run harness service install" in said(mac.system)[0]
    held.append(instance.claim(mac.config.data_dir, "web"))
    system = make_system(mac.manager, mac.home)
    service_cli.run(args_for("status"), mac.config, system)
    assert "现在开着的是在终端里开着的网页界面（harness --web）" in said(system)[0]


def test_status_on_linux_shows_journal_problems_and_linger(linux):
    service_cli.run(args_for("install"), linux.config, linux.system)
    linux.fake.journal = "-- No entries --\n2026-11-21T09:00:01+0800 host python[12]: 还没有设置任何聊天通道。\nHint: You are currently not seeing messages from other users.\n"
    system = make_system(linux.manager, linux.home)
    assert service_cli.run(args_for("status"), linux.config, system) == 0
    out = said(system)[0]
    assert "退出登录以后会跟着停下。要一直开着：sudo loginctl enable-linger tester" in out
    assert "  2026-11-21T09:00:01+0800 host python[12]: 还没有设置任何聊天通道。" in out and "No entries" not in out and "Hint:" not in out
    journal = [call for call in linux.fake.calls if call[0] == "journalctl"][-1]
    assert journal == ["journalctl", "--user", "-u", UNIT, "-p", "warning", "-n", "5", "--no-pager", "-o", "short-iso"]


def test_status_explains_systemd_exit_codes(linux):
    service_cli.run(args_for("install"), linux.config, linux.system)
    linux.fake.down()
    linux.fake.active, linux.fake.code, linux.fake.status = "failed", 1, 2
    system = make_system(linux.manager, linux.home)
    assert service_cli.run(args_for("status"), linux.config, system) == 3
    assert "出错停下了（退出码 2：设置有问题（比如 channels.json 坏了），重启也没用，先看日志）" in said(system)[0]


# ---- 等它开起来 -----------------------------------------------------------------


def test_install_waits_for_a_slow_start(mac):
    """launchd 读进来以后，智狐要一两秒才开好网页：等着它，开好了才说"开起来了"。"""
    mac.fake.starts = False
    clock = Clock()
    clock.at.append((2.0, lambda: (setattr(mac.fake, "starts", True), mac.fake.up())))
    system = make_system(mac.manager, mac.home, clock=clock, start_wait=10)
    assert service_cli.run(args_for("install"), mac.config, system) == 0
    assert 2.0 <= clock.now < 10 and "已经开起来了" in said(system)[0]


def test_install_reports_a_service_that_never_comes_up(mac):
    mac.fake.starts = False
    log_file = mac.config.data_dir / "logs" / "service.log"
    log_file.parent.mkdir(parents=True)
    log_file.write_text("ModuleNotFoundError: No module named 'harness'\n", encoding="utf-8")
    system = make_system(mac.manager, mac.home, start_wait=3)
    assert service_cli.run(args_for("install"), mac.config, system) == 1
    _, err = said(system)
    assert "等了 3 秒还没看到它开起来" in err and "No module named 'harness'" in err
    assert "uv run harness service status" in err and str(log_file) in err


# ---- 打开网页 ------------------------------------------------------------------


class Opener:
    def __init__(self, works: bool = True):
        self.works = works
        self.urls: list[str] = []

    def __call__(self, url: str) -> bool:
        self.urls.append(url)
        return self.works


def test_open_reads_the_address_and_never_prints_the_token(mac, held):
    held.append(instance.claim(mac.config.data_dir, "service"))
    (mac.config.data_dir / ".web-address").write_text(URL + "\n", encoding="utf-8")
    opener = Opener()
    system = make_system(mac.manager, mac.home, opener=opener)
    assert service_cli.run(args_for("open"), mac.config, system) == 0
    assert opener.urls == [URL]
    out, err = said(system)
    assert "http://127.0.0.1:8765" in out and TOKEN not in out + err


def test_open_print_only_prints_the_address_with_a_warning(mac, held):
    held.append(instance.claim(mac.config.data_dir, "service"))
    (mac.config.data_dir / ".web-address").write_text(URL + "\n", encoding="utf-8")
    opener = Opener()
    system = make_system(mac.manager, mac.home, opener=opener)
    assert service_cli.run(args_for("open", "--print"), mac.config, system) == 0
    out, err = said(system)
    assert out.strip() == URL and "不要发给别人" in err and opener.urls == []


def test_open_without_a_browser_prints_the_address(mac, held):
    held.append(instance.claim(mac.config.data_dir, "service"))
    (mac.config.data_dir / ".web-address").write_text(URL + "\n", encoding="utf-8")
    system = make_system(mac.manager, mac.home, opener=Opener(works=False))
    assert service_cli.run(args_for("open"), mac.config, system) == 0
    assert URL in said(system)[0] and "没能打开浏览器" in said(system)[0]


def test_open_when_nothing_runs_or_there_is_no_address(mac, held):
    opener = Opener()
    system = make_system(mac.manager, mac.home, opener=opener)
    (mac.config.data_dir).mkdir(parents=True, exist_ok=True)
    (mac.config.data_dir / ".web-address").write_text(URL + "\n", encoding="utf-8")  # 上一次留下的：没人在运行，不算数
    assert service_cli.run(args_for("open"), mac.config, system) == 1
    assert "没有在运行" in said(system)[1]
    (mac.config.data_dir / ".web-address").unlink()
    held.append(instance.claim(mac.config.data_dir, "web"))
    assert service_cli.run(args_for("open"), mac.config, system) == 1
    assert "还没找到网页的地址" in said(system)[1] and opener.urls == []


def test_open_refuses_an_address_that_is_not_zhifoxs(mac, held):
    held.append(instance.claim(mac.config.data_dir, "service"))
    opener = Opener()
    for bad in ("http://evil.example/?token=abc", "http://127.0.0.1:8765/?token=abc&next=http://evil", "file:///etc/passwd"):
        (mac.config.data_dir / ".web-address").write_text(bad + "\n", encoding="utf-8")
        system = make_system(mac.manager, mac.home, opener=opener)
        assert service_cli.run(args_for("open"), mac.config, system) == 1, bad
        assert "不像是智狐的地址" in said(system)[1]
    assert opener.urls == []


def test_open_finds_the_installed_services_data_folder(mac, tmp_path, held, make_config):
    """服务装的时候用的是另一个数据目录（HARNESS_DATA_DIR）：open 照着服务的设置去找，而不是现在终端里的设置。"""
    service_cli.run(args_for("install"), mac.config, mac.system)
    opener = Opener()
    elsewhere = make_config(data_dir=tmp_path / "another-data")
    system = make_system(mac.manager, mac.home, opener=opener)
    assert service_cli.run(args_for("open"), elsewhere, system) == 0
    assert opener.urls == [URL]


def test_open_says_when_the_service_has_no_web_page(mac):
    service_cli.run(args_for("install", "--no-web"), mac.config, mac.system)
    (mac.config.data_dir / ".web-address").unlink(missing_ok=True)
    system = make_system(mac.manager, mac.home, opener=Opener())
    assert service_cli.run(args_for("open"), mac.config, system) == 1
    assert "--no-web" in said(system)[1]


# ---- 日志 --------------------------------------------------------------------


def test_tail_and_problems(tmp_path, monkeypatch):
    path = tmp_path / "service.log"
    path.write_text("".join(f"2026-11-21 10:00:{n:02d} info 第 {n} 行\n" for n in range(60)), encoding="utf-8")
    assert logs.tail(path, 3) == [f"2026-11-21 10:00:{n:02d} info 第 {n} 行" for n in (57, 58, 59)]
    assert logs.tail(path, 0) == [] and logs.tail(tmp_path / "missing", 5) == []
    monkeypatch.setattr(logs, "TAIL_BYTES", 100)  # 只读最后 100 个字节：从一行的中间开始读，那半行不要
    rows = logs.tail(path, 100)
    assert rows and all(re.match(r"2026-11-21 10:00:\d\d info 第 \d+ 行$", row) for row in rows)
    lines = [
        "2026-11-21 10:00:00 info 一切正常",
        "2026-11-21 10:00:01 warning 小心",
        "2026-11-21 10:00:02 error 出错了",
        "2026-11-21 10:00:03 critical 糟了",
        "    raise ValueError('里面的一行，不算')",
        "ValueError: 最后一行算",
        "这一行写着 error 但不是级别",
    ]
    assert logs.problems(lines, 10) == lines[1:4] + [lines[5]]
    assert logs.problems(lines, 2) == [lines[3], lines[5]]


def test_follow_prints_new_lines_and_survives_a_rotation(tmp_path):
    path = tmp_path / "service.log"
    path.write_text("旧的一行\n", encoding="utf-8")
    out = io.StringIO()
    encoded = "狐狸\n".encode()
    steps = [
        lambda: path.open("ab").write("新的一行\n".encode()),
        lambda: path.open("ab").write(encoded[:2]),  # 一个汉字被拆在两次写之间
        lambda: path.open("ab").write(encoded[2:]),
        lambda: (path.rename(tmp_path / "service.log.1"), path.write_text("换了新文件\n", encoding="utf-8")),
        lambda: None,
    ]

    def sleep(_seconds):
        if steps:
            steps.pop(0)()

    rounds = iter(range(8))
    logs.follow(path, out, keep_going=lambda: next(rounds, None) is not None, sleep=sleep)
    assert out.getvalue() == "新的一行\n狐狸\n换了新文件\n"  # 开始之前的那一行不重复打印


def test_logs_command_on_mac(mac):
    system = make_system(mac.manager, mac.home)
    assert service_cli.run(args_for("logs"), mac.config, system) == 0
    assert "还没有日志" in said(system)[0]
    log_file = mac.config.data_dir / "logs" / "service.log"
    log_file.parent.mkdir(parents=True)
    log_file.write_text("".join(f"第 {n} 行\n" for n in range(10)), encoding="utf-8")
    system = make_system(mac.manager, mac.home)
    assert service_cli.run(args_for("logs", "-n", "2"), mac.config, system) == 0
    assert said(system)[0] == "第 8 行\n第 9 行\n"


def test_logs_command_on_linux_hands_over_to_journalctl(linux):
    assert service_cli.run(args_for("logs"), linux.config, linux.system) == 0
    assert service_cli.run(args_for("logs", "-f", "-n", "20"), linux.config, linux.system) == 0
    assert linux.fake.attached == [
        ["journalctl", "--user", "-u", UNIT, "-n", "50", "--no-pager"],
        ["journalctl", "--user", "-u", UNIT, "-n", "20", "--no-pager", "-f"],
    ]


# ---- 数据目录的锁 -------------------------------------------------------------------


def test_claim_writes_who_holds_the_folder(tmp_path):
    data = tmp_path / "data"
    first = instance.claim(data, "service")
    try:
        assert json.loads((data / ".lock").read_text(encoding="utf-8")) == {"pid": os.getpid(), "kind": "service"}
        assert instance.claim(data, "web") is None  # 第二个占不到
        before = (data / ".lock").read_bytes()
        assert instance.in_use(data) and instance.in_use(data)
        assert (data / ".lock").read_bytes() == before  # 看一眼不改内容
        assert instance.holder(data) == instance.Holder(os.getpid(), "service")
    finally:
        first.close()
    assert not instance.in_use(data)
    again = instance.claim(data, "web")  # 放开以后，下一个占到的人换掉那一行
    assert instance.holder(data) == instance.Holder(os.getpid(), "web")
    again.close()
    assert not instance.in_use(tmp_path / "never-used")


@pytest.mark.parametrize("content", ["", "垃圾", "[1, 2]", '{"pid": "1", "kind": "web"}', '{"pid": 1, "kind": "hacker"}'])
def test_holder_ignores_what_it_does_not_understand(tmp_path, content):
    (tmp_path / ".lock").write_text(content, encoding="utf-8")
    assert instance.holder(tmp_path) is None


def test_busy_lines_say_who_and_what_to_do(tmp_path, held):
    data = tmp_path / "data"
    expected = {
        "service": ["正在作为后台服务运行", "uv run harness service open", "uv run harness service stop"],
        "channels": ["harness channels pair 正在等配对码", "Ctrl-C"],
        "serve": ["已经有一个智狐在运行", "harness serve", "systemctl stop"],
        "web": ["已经有一个智狐的网页界面在运行", f"进程 {os.getpid()}", "Ctrl-C"],
    }
    for kind, phrases in expected.items():
        lock = instance.claim(data, kind)
        text = "\n".join(instance.busy_lines(data))
        lock.close()
        for phrase in phrases:
            assert phrase in text, (kind, phrase, text)
    held.append(hold(data, note=False))
    text = "\n".join(instance.busy_lines(data))
    assert "已经有一个智狐的网页界面在运行" in text and "进程" not in text


def test_harness_web_by_hand_points_to_the_service(make_config, capsys, held, monkeypatch):
    """服务开着的时候你又在终端里运行 uv run harness --web：不开第二个，告诉你怎么打开服务的网页、怎么停下它。"""
    config = make_config()
    held.append(instance.claim(config.data_dir, "service"))
    monkeypatch.setattr(server, "_serve", lambda *args: pytest.fail("不该开起第二个"))
    assert server.run_web(config, open_browser=False) == 1
    out = capsys.readouterr().out
    assert f"智狐正在作为后台服务运行（进程 {os.getpid()}）" in out
    assert "uv run harness service open" in out and "uv run harness service stop" in out


def test_harness_serve_by_hand_points_to_the_service(make_config, capsys, held, restore_logs):
    """harness serve 也一样：日志里说清楚是后台服务占着，退出码 1（数据目录被占着）。"""
    from harness.serve import EXIT_BUSY, run_serve

    config = make_config()
    held.append(instance.claim(config.data_dir, "service"))
    assert run_serve(config) == EXIT_BUSY
    err = capsys.readouterr().err
    assert "正在作为后台服务运行" in err and "uv run harness service open" in err


def test_channels_cli_only_looks(make_config, held):
    config = make_config()
    assert channels_cli._running(config) is False
    assert not (config.data_dir / ".lock").exists() or (config.data_dir / ".lock").read_text(encoding="utf-8") == ""
    held.append(instance.claim(config.data_dir, "service"))
    assert channels_cli._running(config) is True
    assert instance.holder(config.data_dir).kind == "service"  # 看一眼没有把说明换成自己的


# ---- 装之前的提醒 -------------------------------------------------------------------


def test_shell_only_lists_names_that_the_service_would_miss(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("HTTPS_PROXY=http://in-file\nexport OPENAI_API_KEY='sk-file'\n", encoding="utf-8")
    plan = simple_plan(env_file=env_file)
    environ = {
        "HTTPS_PROXY": "http://me:pw@proxy",  # .env 里有：服务读得到
        "OPENAI_API_KEY": "sk-file",
        "ANTHROPIC_API_KEY": "sk-ant-only-in-shell",  # 只在终端里
        "https_proxy": "http://lower",
        "HARNESS_MODEL": "claude-sonnet-5-5",
        "HARNESS_DATA_DIR": "/x",  # 服务的设置里本来就写着
        "NO_PROXY": "",  # 空的不算
        "DISCORD_API_BASE": "http://127.0.0.1:1",
        "UNRELATED": "1",
    }
    assert shell_only(plan, environ) == ["ANTHROPIC_API_KEY", "DISCORD_API_BASE", "HARNESS_MODEL", "https_proxy"]


def test_install_warns_about_settings_only_in_the_shell_without_values(mac):
    mac.system.environ = {"PATH": PATH, "ANTHROPIC_API_KEY": "sk-ant-only-in-shell-XYZ"}
    service_cli.run(args_for("install"), mac.config, mac.system)
    out, err = said(mac.system)
    assert "ANTHROPIC_API_KEY 只在终端里设置了" in err and "sk-ant-only-in-shell-XYZ" not in out + err


def test_install_warns_when_there_is_no_api_key_at_all(mac):
    mac.system.environ = {"PATH": PATH}
    service_cli.run(args_for("install"), mac.config, mac.system)
    assert "还没有设置 Claude" in said(mac.system)[1]


def test_protected_mac_folders_are_found(tmp_path):
    home = tmp_path / "home"
    plan = simple_plan(
        python=str(home / "Documents" / "zf" / ".venv" / "bin" / "python"), workspace=home / "Desktop" / "w",
        data_dir=home / "zf-data", env_file=home / "zf-data" / ".env",
    )
    assert protected_folders(plan, home) == [home / "Documents", home / "Desktop"]
    assert protected_folders(simple_plan(), home) == []


def test_install_on_mac_warns_about_protected_folders(mac, monkeypatch):
    documents = mac.home / "Documents"
    workspace = documents / "我的项目"
    workspace.mkdir(parents=True)
    service_cli.run(args_for("install", "--workspace", str(workspace)), mac.config, mac.system)
    err = said(mac.system)[1]
    assert str(documents) in err and "完整磁盘访问权限" in err


# ---- 日志文件（launchd 下）---------------------------------------------------------


@pytest.fixture
def restore_logs():
    root = logging.getLogger("harness")
    saved = (list(root.handlers), root.level, root.propagate)
    with log._LOCK:
        secrets = set(log._SECRETS)
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in saved[0]:
        root.addHandler(handler)
    root.setLevel(saved[1])
    root.propagate = saved[2]
    with log._LOCK:
        log._SECRETS.clear()
        log._SECRETS.update(secrets)


def test_the_service_log_rotates_keeps_dates_and_hides_secrets(tmp_path, monkeypatch, restore_logs):
    monkeypatch.setattr(log, "SERVICE_LOG_BYTES", 400)
    path = tmp_path / "logs" / "service.log"
    log.setup(file=path)
    log.hide("super-secret-token-123")
    logger = logging.getLogger("harness.test")
    logger.warning("token 是 super-secret-token-123")
    first = path.read_text(encoding="utf-8")
    # 第一行是"从这里开始"（status 靠它只列这一次运行的问题，见 service/logs.since_start），后面才是写进去的那一行
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d info ---- zhifox started, pid \d+ ----\n\d{4}-\d\d-\d\d \d\d:\d\d:\d\d warning token 是 <hidden>\n",
        first,
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    for n in range(80):
        logger.info("第 %d 行：写满了就换一个新文件", n)
    names = sorted(item.name for item in path.parent.iterdir())
    assert names == ["service.log", *(f"service.log.{n}" for n in range(1, SERVICE_LOG_BACKUPS + 1))]  # 最多留这么多份
    # 标准库按字符数估计这一行有多长，汉字一个占 3 个字节，所以可能比上限多出一行；不会多出更多
    assert all(item.stat().st_size < 400 + 200 for item in path.parent.iterdir())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600  # 换的新文件也是只给自己看
    assert "第 79 行" in path.read_text(encoding="utf-8")


# ---- 命令行 --------------------------------------------------------------------


def test_main_dispatches_service_commands(monkeypatch, tmp_path, mac):
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "cli-data"))
    monkeypatch.setattr(service_cli, "detect", lambda: mac.system)
    assert cli.main(["service", "status"]) == 3
    assert "智狐的后台服务（launchd）：还没有装上" in said(mac.system)[0]


def test_unsupported_systems_say_so(make_config):
    system = service_cli.System(None, "这个系统还不支持后台服务", out=io.StringIO(), err=io.StringIO())
    assert service_cli.run(args_for("status"), make_config(), system) == 2
    assert "不支持" in system.err.getvalue()


def test_detect_picks_the_manager_for_this_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert isinstance(service_cli.detect().manager, Launchd)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    found = service_cli.detect().manager
    assert isinstance(found, Systemd) and found.systemctl == "/usr/bin/systemctl"
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert service_cli.detect().manager is None and "systemd" in service_cli.detect().unsupported
    monkeypatch.setattr(sys, "platform", "win32")
    assert isinstance(service_cli.detect().manager, TaskScheduler)  # Windows：任务计划程序（见 test_service_windows.py）
    monkeypatch.setattr(sys, "platform", "freebsd14")
    assert service_cli.detect().manager is None and "Windows" in service_cli.detect().unsupported


def test_serve_accepts_the_service_option(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "cli-data"))
    seen = {}

    def fake_run_serve(config, **options):
        seen.update(options)
        return 0

    monkeypatch.setattr("harness.serve.run_serve", fake_run_serve)
    assert cli.main(["serve", "--web", "--service", "launchd"]) == 0
    assert seen == {"web": True, "port": None, "service": "launchd"}
    with pytest.raises(SystemExit):
        cli.main(["serve", "--service", "cron"])


def test_the_conftest_guard_stops_real_commands():
    """保险：测试里漏了假的系统，也不会真的去运行 launchctl。"""
    from harness.service import base

    with pytest.raises(AssertionError):
        base.run_command(["/bin/launchctl", "list"])
    with pytest.raises(AssertionError):
        service_cli.System(None).opener(URL)


# ---- 真的进程：plist 里写的那条命令 ---------------------------------------------------


def test_the_service_command_line_really_runs(tmp_path, make_config):
    """像 launchd 那样运行 plist 里的命令（不经过 launchd）：输出接到日志文件上，工作目录里放一个"埋好的" json.py。

    -P 的作用就在这里：不加的话，Python 会先在当前目录（工作目录）里找模块，导入的是这个 json.py 而不是标准库。
    """
    workspace = tmp_path / "work dir 工作"
    workspace.mkdir()
    (workspace / "json.py").write_text("raise SystemExit('导入了工作目录里埋的 json.py')\n", encoding="utf-8")
    data = tmp_path / "data"
    config = make_config(workspace=workspace, data_dir=data, env_file=tmp_path / "no-such.env")
    plan = make_plan(config, port=0, environ={"PATH": os.environ.get("PATH", "")})
    command = plan.arguments("launchd")
    assert command[:3] == [os.path.abspath(sys.executable), "-P", "-m"]
    log_file = plan.log_file
    log_file.parent.mkdir(parents=True)
    env = {
        **{name: value for name, value in os.environ.items() if not name.startswith("HARNESS_") and name != "PYTHONPATH"},
        **plan.environment(), "HOME": str(tmp_path / "home"), "NO_PROXY": "*", "no_proxy": "*",
    }
    env.pop("JOURNAL_STREAM", None)
    # 不加 -P 会怎样：同一个工作目录里，Python 先导入了埋好的 json.py
    unsafe = subprocess.run([command[0], "-m", "harness", "--help"], cwd=workspace, env=env, capture_output=True, text=True, check=False)
    assert unsafe.returncode != 0 and "埋的 json.py" in unsafe.stderr
    with open(log_file, "ab") as output:
        process = subprocess.Popen(command, cwd=workspace, env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=output)
    address = data / ".web-address"
    try:
        url = until(lambda: process.poll() is None and address.is_file() and address.read_text(encoding="utf-8").strip(), timeout=30, what="网页的地址")
        assert re.fullmatch(r"http://127\.0\.0\.1:\d+/\?token=[\w-]+", url)
        assert stat.S_IMODE(address.stat().st_mode) == 0o600
        assert instance.holder(data) == instance.Holder(process.pid, "service")  # 数据目录的锁上记着：它是后台服务
        process.send_signal(signal.SIGTERM)  # launchctl bootout / 退出登录
        assert process.wait(15) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    text = log_file.read_text(encoding="utf-8")
    assert "埋的 json.py" not in text
    token = url.split("token=", 1)[1]
    assert token not in text  # 口令不进日志
    lines = [line for line in text.splitlines() if line.strip()]
    assert any("已作为服务启动" in line for line in lines) and any("uv run harness service open" in line for line in lines)
    assert all(re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d (info|warning|error) ", line) for line in lines), lines
    assert lines[-1].endswith("再见。")
    assert not address.exists()  # 退出时删掉
    assert stat.S_IMODE(log_file.stat().st_mode) == 0o600


def test_config_has_one_place_for_the_address_and_the_log(make_config):
    config = make_config()
    assert config.web_address_file == config.data_dir / config_module.WEB_ADDRESS
    assert config.service_log == make_plan(config, environ={}).log_file
    assert config.web_address_file == make_plan(config, environ={}).web_address_file


def test_status_lists_only_the_problems_of_the_latest_run():
    # 装好之前那几次失败（比如还没授权读"文稿"时的 Operation not permitted）不该在服务好好运行以后还列出来：
    # 用户装上服务、授了权、服务也开起来了，status 却还写着一串权限错误，看着像是没好（2026-11-21 用户装服务时遇到的）
    from harness.service import logs

    old = [
        "Traceback (most recent call last):",
        "PermissionError: [Errno 1] Operation not permitted: '/Users/x/Documents/p/.venv/pyvenv.cfg'",
        "2026-11-21 15:20:00 error 上一次出过的错",
    ]
    started = [f"2026-11-21 15:27:16 info {logs.START_MARK}97264 ----", "2026-11-21 15:27:16 info 智狐已作为服务启动"]
    assert logs.problems(old + started, 5) == []
    later = ["2026-11-21 15:30:00 warning 这一次的警告"]
    assert logs.problems(old + started + later, 5) == later
    # 还没写过"从这里开始"（开不起来，或者是这个改动之前的日志）：照旧全部都算
    assert len(logs.problems(old, 5)) == 2
