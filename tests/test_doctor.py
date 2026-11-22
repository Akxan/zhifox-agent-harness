"""harness doctor（自检）的测试：每一项"好""有问题""没用上"各是什么时候，修法按系统写对了没有，密钥绝不出现在结果里，
查不了的那一项不让整个自检崩掉，自检本身什么都不改，退出码。

这里的电脑都是"干净"的：没有密钥（conftest 把各家的环境变量设成了空的）、找得到 uv（which 换成假的）、
没有服务管家（给一个假的 System），各个检查再按测试的需要一样一样加上去。要看另外两个系统上的说法时，
把 machine.system 换掉（判断系统的地方只有那一处）；Windows 的访问控制列表用假的 icacls（winacl.runner）。
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path

import fake_sandbox
import pytest
from fake_desktop import FakeDesktop

from harness import cli, i18n, instance, machine, winacl
from harness.channels.store import ChannelStore
from harness.config import PROJECT_ROOT, Config
from harness.desktop import backend as desktop_backend
from harness.doctor import OK, PROBLEM, UNUSED, environment, fixes
from harness.doctor import cli as doctor_cli
from harness.doctor import examine as real_examine
from harness.doctor.keys import ADDRESSES
from harness.doctor.text import NAME_COLUMNS, columns, lines
from harness.safe import private_dir, write_file
from harness.service import cli as service_cli
from harness.service.base import State
from harness.service.plan import make_plan
from harness.settings import Settings
from harness.tasks import TaskStore
from harness.ui import TerminalUI

WINDOWS_HOST = os.name == "nt"
POSIX_ONLY = pytest.mark.skipif(WINDOWS_HOST, reason="Windows 上没有 600、700 这种权限位（那里看访问控制列表，见下面假装成 Windows 的那几个测试）")
SID = "S-1-5-21-1-2-3-1001"
NO_SERVICE = service_cli.System(None, "这台测试的电脑上没有服务管家")
CJK = re.compile(r"[㐀-鿿]")


# 自检正在运行时非空（见 examine）。只有这时候"改权限"才算测试失败：在真的 Windows 上，测试准备数据目录时
# （private_dir、write_file、ChannelStore……）本来就会收紧权限，那是测试自己做的，不是自检
_EXAMINING: list[bool] = []


def examine(*args, **kwargs):
    """自检本身。运行期间假的 icacls 只许读（见 _icacls）；终端里的 harness doctor 和 /doctor 也换成走这里（见 clean）。"""
    _EXAMINING.append(True)
    try:
        return real_examine(*args, **kwargs)
    finally:
        _EXAMINING.pop()


def _icacls(sddl: str | None, calls: list | None = None):
    """假的 icacls：whoami 回答 SID；icacls /save 把 sddl 写进它要的那个文件（UTF-16，和真的一样）。sddl 是 None 时 icacls 失败。
    别的 icacls 命令（会改权限的那些）在自检运行时一律算测试失败：自检只许读。测试自己准备数据目录时照样"成功"。"""

    def runner(args):
        if calls is not None:
            calls.append(args)
        if args[0].lower().endswith("whoami.exe"):
            return 0, f'"pc\\me","{SID}"\r\n'
        if "/save" in args:
            if sddl is None:
                return 5, "拒绝访问。"
            Path(args[args.index("/save") + 1]).write_bytes(f"data\r\n{sddl}\r\n".encode("utf-16-le"))
            return 0, ""
        if _EXAMINING:
            raise AssertionError(f"自检只许读，不许改：{args}")
        return 0, ""  # 测试在准备数据目录（真的 Windows 上 private_dir 会收紧权限）

    return runner


ONLY_ME = f"D:PAI(A;OICI;FA;;;{SID})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"


@pytest.fixture
def clean(monkeypatch):
    """一台干净的电脑：找得到 uv，没有改过的接口地址、没有 HARNESS_CHROME。
    Windows 上读访问控制列表的 icacls 换成假的（conftest 的那个不写 /save 的文件），回答"只有你自己"。"""
    monkeypatch.setattr(environment, "which", lambda name: f"/usr/local/bin/{name}" if name == "uv" else None)
    for name in (*ADDRESSES, "HARNESS_CHROME", "HARNESS_DATA_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(winacl, "runner", _icacls(ONLY_ME))
    monkeypatch.setattr(doctor_cli, "examine", examine)  # 终端里的两条路也只许读
    monkeypatch.setattr(cli, "doctor_examine", examine)
    winacl.forget()


def run(config: Config, system: service_cli.System = NO_SERVICE):
    report = examine(config, system)
    return report, {check.id: check for check in report.checks()}


def problems(report) -> list[str]:
    return [check.id for check in report.checks() if check.status == PROBLEM]


def settings_of(config: Config) -> Settings:
    return Settings(config.settings_file, config.default_provider, config.default_model)


def all_text(report) -> str:
    """给人看的表格和 --json，拼在一起：查"密钥在不在里面"时两种都查。"""
    return "\n".join(text for text, _ in lines(report)) + "\n" + json.dumps(report.to_dict(), ensure_ascii=False)


# ---- 好、有问题、没用上 -------------------------------------------------------------------


def test_a_fresh_computer_has_exactly_one_problem_the_missing_key(make_config, clean):
    config = make_config()
    report, found = run(config)
    assert problems(report) == ["key.anthropic"]  # 没配置的功能都只是"没用上"
    key = found["key.anthropic"]
    assert "没有密钥" in key.detail and any("uv run harness --web" in line for line in key.fix)
    for name in ("key.openai", "key.gemini", "key.deepseek", "endpoints", "data_dir", "env_file", "service", "mcp", "channels", "hooks", "tasks", "sandbox", "computer"):
        assert found[name].status == UNUSED, name
    for name in ("python", "zhifox", "system", "uv", "workspace", "holder", "model", "proxy", "addresses", "web_search"):
        assert found[name].status == OK, name
    assert report.exit_code == 1
    assert [section.id for section in report.sections] == ["environment", "folders", "occupancy", "keys", "features", "extensions", "network"]


def test_with_a_key_and_private_folders_everything_is_fine(make_config, clean, monkeypatch):
    config = make_config()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-0000000000000000")
    private_dir(config.data_dir)
    write_file(config.env_file, "SOMETHING=1\n")
    report, found = run(config)
    assert problems(report) == [] and report.exit_code == 0
    assert found["key.anthropic"].status == OK and "在环境变量里" in found["key.anthropic"].detail
    assert found["data_dir"].status == OK and found["env_file"].status == OK
    assert "设置了 1 项" in found["env_file"].detail


def test_json_has_the_documented_shape(make_config, clean):
    data = run(make_config())[0].to_dict()
    assert data["format"] == 1 and data["system"] == machine.system() and data["language"] == "zh"
    assert set(data["summary"]) == {"ok", "problem", "unused"} and data["summary"]["problem"] == 1
    check = data["sections"][0]["checks"][0]
    assert set(check) == {"id", "label", "status", "detail", "fix", "facts"} and check["id"] == "python"
    json.dumps(data)  # 只有普通的值


# ---- 密钥绝不出现 ----------------------------------------------------------------------


def test_no_key_and_not_even_its_last_four_characters_show_up(tmp_path, monkeypatch, capsys, clean):
    """在临时的 .env 里放一个编造的密钥，走真的命令行（load_config 把它读进环境变量）：表格和 --json 里都找不到它，连最后 4 位也找不到。
    顺便放上别处的密钥：聊天机器人的 token、自定义接口的密钥、MCP 服务器的环境变量、钩子的参数、代理地址里的密码。"""
    anthropic = "sk-ant-api03-FAKEfakeFAKEfake1234567890abcdefQx7Z"
    openai_key = "sk-proj-FAKEfakeFAKEfake0987654321Op5R"
    env = tmp_path / "secret.env"
    env.write_text(f"ANTHROPIC_API_KEY={anthropic}\n", encoding="utf-8")
    monkeypatch.setenv("HARNESS_ENV_FILE", str(env))
    monkeypatch.delenv("ANTHROPIC_API_KEY")  # 不然 conftest 设的空值会盖住 .env（这正是下面另一个测试查的那种情况）
    monkeypatch.setenv("OPENAI_API_KEY", openai_key)
    data = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(data))
    monkeypatch.setenv("HTTPS_PROXY", "http://alice:pr0xyPassWd9@proxy.example:8080")
    monkeypatch.setattr(service_cli, "detect", lambda: NO_SERVICE)
    bot_token = "123456789:AAFakeBotTokenFakeBotTokenFakeCh4n"
    ChannelStore(data / "channels.json").connect("telegram", {"token": bot_token}, {"id": 1, "username": "zhifox_test_bot", "name": "智狐测试"})
    endpoint_key = "endpoint-secret-FAKE-FAKE-FAKE-En8D"
    write_file(data / "endpoints.json", json.dumps({"endpoints": [{"id": "ollama", "name": "Ollama", "base_url": "http://localhost:11434/v1", "api_key": endpoint_key}]}))
    mcp_secret = "mcp-secret-FAKE-FAKE-FAKE-Mc9P"
    write_file(data / "mcp.json", json.dumps({"mcpServers": {"files": {"command": sys.executable, "env": {"API_TOKEN": mcp_secret}}}}))
    hook_secret = "hook-secret-FAKE-FAKE-FAKE-Hk3Q"
    write_file(data / "hooks.json", json.dumps({"hooks": {"check": {"event": "Stop", "command": sys.executable, "args": [f"--token={hook_secret}"]}}}))

    capsys.readouterr()
    cli.main(["doctor"])
    text = capsys.readouterr().out
    cli.main(["doctor", "--json"])
    raw = capsys.readouterr().out
    report = json.loads(raw)
    assert raw.isascii()  # 中文写成 \uXXXX：Windows PowerShell 5.1 按代码页读管道，UTF-8 的中文到那里会成乱码
    for secret in (anthropic, openai_key, bot_token, endpoint_key, mcp_secret, hook_secret, "pr0xyPassWd9"):
        for output in (text, raw):
            assert secret not in output
            assert secret[-4:] not in output, secret  # 网页上的 key_hint 那种"最后 4 位"也不行
    assert "alice" not in text and "http://proxy.example:8080" in text  # 代理只写到端口
    keys = {check["id"]: check for section in report["sections"] for check in section["checks"]}
    assert keys["key.anthropic"]["status"] == OK and keys["key.anthropic"]["facts"]["source"] == "env_file"
    assert "有密钥（在 .env 里）" in text and "有密钥（在环境变量里）" in text


def test_an_empty_variable_hiding_the_key_in_env_is_a_problem(make_config, clean, monkeypatch):
    """.env 只补环境变量里"没有"的：环境变量设成了空的（export ANTHROPIC_API_KEY=），.env 里的密钥就读不进来。"""
    config = make_config()
    write_file(config.env_file, "ANTHROPIC_API_KEY=sk-ant-from-the-file-0000\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    check = run(config)[1]["key.anthropic"]
    assert check.status == PROBLEM and "是空的" in check.detail
    assert check.fix == [fixes.unset("ANTHROPIC_API_KEY")]
    # 环境变量里的和 .env 里的不一样：能用，但改 .env 不起作用，说一声
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-the-shell-1111")
    check = run(config)[1]["key.anthropic"]
    assert check.status == OK and "改 .env 不起作用" in check.detail
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-the-file-0000")
    check = run(config)[1]["key.anthropic"]
    assert check.status == OK and check.facts["source"] == "env_file"


def test_the_selected_model_and_custom_endpoints(make_config, clean, monkeypatch):
    config = make_config()
    private_dir(config.data_dir)
    write_file(config.endpoints_file, json.dumps({"endpoints": [{"id": "ollama", "name": "Ollama", "base_url": "http://localhost:11434/v1"}]}))
    settings_of(config).choose("custom-ollama", "qwen3:8b")
    found = run(config)[1]
    assert found["model"].status == OK and "Ollama" in found["model"].detail and "qwen3:8b" in found["model"].detail
    assert found["endpoints"].status == OK and "1 个" in found["endpoints"].detail and "这台电脑" in found["endpoints"].detail
    assert found["key.anthropic"].status == UNUSED  # 现在用的不是它
    settings_of(config).choose("custom-gone", "x")  # 接口删掉了
    found = run(config)[1]
    assert found["model"].status == PROBLEM and "已经不在了" in found["model"].detail
    write_file(config.endpoints_file, "{ not json")
    assert run(config)[1]["endpoints"].status == PROBLEM


# ---- 目录的权限 ---------------------------------------------------------------------


@POSIX_ONLY
def test_folders_and_files_others_can_read_are_problems(make_config, clean, monkeypatch):
    config = make_config()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-0000000000000000")
    private_dir(config.data_dir)
    write_file(config.channels_file, "{}")
    write_file(config.env_file, "A=1\n")
    config.data_dir.chmod(0o755)
    config.channels_file.chmod(0o644)
    config.env_file.chmod(0o644)
    report, found = run(config)
    assert problems(report) == ["data_dir", "env_file"]
    data = found["data_dir"]
    assert "755" in data.detail and "channels.json" in data.detail
    assert fixes.private_folder(config.data_dir) in data.fix and fixes.private_files([config.channels_file]) in data.fix
    assert fixes.command("chmod", "700", str(config.data_dir)) in data.fix[0]
    assert "644" in found["env_file"].detail and fixes.command("chmod", "600", str(config.env_file)) in found["env_file"].fix[0]
    # 照着修好以后就好了
    config.data_dir.chmod(0o700)
    config.channels_file.chmod(0o600)
    config.env_file.chmod(0o600)
    assert problems(run(config)[0]) == []


@POSIX_ONLY
def test_a_data_folder_owned_by_another_account(make_config, clean, monkeypatch):
    """用 sudo 运行过一次的话，数据目录成了 root 的：自己改不动，修法是把它要回来。"""
    config = make_config()
    private_dir(config.data_dir)
    real = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real + 1)
    check = run(config)[1]["data_dir"]
    assert check.status == PROBLEM and "sudo" in check.detail
    assert any('sudo chown -R "$(whoami)"' in line for line in check.fix)


def _fake_icacls(monkeypatch, sddl: str | None, calls: list | None = None):
    """假装是 Windows，icacls 是假的（见 _icacls）。"""
    monkeypatch.setattr(machine, "system", lambda: machine.WINDOWS)
    monkeypatch.setattr(winacl, "WINDOWS", True)
    monkeypatch.setattr(winacl, "runner", _icacls(sddl, calls))
    winacl.forget()


def test_on_windows_the_access_list_is_read_and_the_fixes_are_powershell(make_config, clean, monkeypatch):
    config = make_config()
    config.data_dir.mkdir()
    config.env_file.write_text("A=1\n", encoding="utf-8")
    calls: list = []
    _fake_icacls(monkeypatch, f"D:AI(A;OICIID;FA;;;{SID})(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)(A;OICIID;0x1200a9;;;BU)", calls)
    found = run(config)[1]
    data = found["data_dir"]
    assert data.status == PROBLEM and "BU" in data.detail
    assert data.fix[0].startswith("在 PowerShell 里运行：icacls ")
    assert f"'*{SID}:(OI)(CI)F'" in data.fix[0] and "/inheritance:r" in data.fix[0]  # PowerShell 里带括号的参数要加引号
    assert found["env_file"].status == PROBLEM and f"'*{SID}:F'" in found["env_file"].fix[0]
    assert all("/save" in call or call[0].lower().endswith("whoami.exe") for call in calls)  # 只读：没有 /grant、/inheritance
    # 只有你、SYSTEM、管理员（还有 Python 3.13 加的 OWNER RIGHTS）：好
    _fake_icacls(monkeypatch, ONLY_ME + "(A;OICI;FA;;;OW)")
    found = run(config)[1]
    assert found["data_dir"].status == OK and "SYSTEM" in found["data_dir"].detail
    # 查不了（U 盘、网络盘）：也算有问题，那样的盘上谁都能读
    _fake_icacls(monkeypatch, None)
    assert "查不了" in run(config)[1]["data_dir"].detail


# ---- 修法按系统写 ---------------------------------------------------------------------


@pytest.mark.parametrize("system", [machine.MAC, machine.LINUX, machine.WINDOWS])
def test_fixes_are_written_for_each_system(monkeypatch, tmp_path, system):
    monkeypatch.setattr(machine, "system", lambda: system)
    monkeypatch.setattr(winacl, "runner", lambda args: (0, f'"pc\\me","{SID}"'))
    winacl.forget()
    folder = tmp_path / "my data"
    shell = {machine.MAC: "zsh", machine.LINUX: "bash", machine.WINDOWS: "PowerShell"}[system]
    assert fixes.shell() == shell
    assert fixes.run("x").startswith(f"在 {shell} 里运行：")
    chrome = "\n".join(fixes.install_chrome())
    keys = "\n".join(fixes.add_key("OPENAI_API_KEY", tmp_path / ".env"))
    if system == machine.WINDOWS:
        assert fixes.private_folder(folder).endswith(f"icacls '{folder}' /inheritance:r /grant:r '*{SID}:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /q")
        assert fixes.unset("OPENAI_API_KEY").startswith("在 PowerShell 里运行：Remove-Item Env:OPENAI_API_KEY")
        assert "winget install Google.Chrome" in chrome and "$env:HARNESS_CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'" in chrome
        assert "Copy-Item" in keys and "notepad" in keys
        assert "install.ps1" in fixes.install_uv() and fixes.find_program("npx") == "Get-Command npx"
        assert fixes.quote("it's") == "'it''s'"
    else:
        assert fixes.private_folder(folder).endswith(f"chmod 700 '{folder}'")
        assert fixes.private_files([folder / "a.json"]).endswith(f"chmod 600 '{folder / 'a.json'}'")
        assert fixes.unset("OPENAI_API_KEY").startswith(f"在 {shell} 里运行：unset OPENAI_API_KEY")
        assert "export HARNESS_CHROME=" in chrome and "cp " in keys
        assert "install.sh" in fixes.install_uv() and fixes.find_program("npx") == "which npx"
        if system == machine.MAC:
            assert "brew install --cask google-chrome" in chrome and "open -e" in keys
        else:
            assert "sudo apt install chromium" in chrome and "nano" in keys
    # uv run harness 的命令三个系统上一样，只是在哪个 shell 里
    assert fixes.harness("service", "logs").endswith(f"用 {shell} 运行：uv run harness service logs")


def test_a_missing_uv_and_chrome_get_their_fixes_on_windows(make_config, clean, monkeypatch):
    config = make_config()
    monkeypatch.setattr(environment, "which", lambda name: None)
    monkeypatch.setattr(machine, "system", lambda: machine.WINDOWS)
    settings_of(config).set_browser({"enabled": True})
    from harness.browser import chrome

    monkeypatch.setattr(chrome, "find_chrome", lambda override="": None)
    found = run(config)[1]
    assert found["uv"].status == PROBLEM and "install.ps1" in found["uv"].fix[0]
    assert found["browser"].status == PROBLEM and "winget install Google.Chrome" in found["browser"].fix[0]
    settings_of(config).set_browser({"enabled": False})
    assert run(config)[1]["browser"].status == UNUSED  # 关着的找不到也不算问题


# ---- 这台电脑上能不能用 ------------------------------------------------------------------


def test_sandbox(make_config, clean, monkeypatch):
    config = make_config()
    assert "能用" in run(config)[1]["sandbox"].detail  # 关着：没用上，顺便说一句能不能用
    settings_of(config).set_sandbox({"level": "workspace"})
    check = run(config)[1]["sandbox"]
    assert check.status == OK and "只在工作目录里" in check.detail
    fake_sandbox.install(monkeypatch, problem="这台电脑上没有装 bubblewrap")
    check = run(config)[1]["sandbox"]
    assert check.status == PROBLEM and "没有装 bubblewrap" in check.detail and "uv run harness sandbox level off" in check.fix[-1]
    monkeypatch.setattr(machine, "system", lambda: machine.WINDOWS)
    assert "WSL" in run(config)[1]["sandbox"].fix[0]  # Windows 上说可以去 WSL 里用


def test_a_task_that_wants_the_sandbox_counts_even_when_the_setting_is_off(make_config, clean, monkeypatch):
    config = make_config()
    TaskStore(config.tasks_file).add({"title": "每天整理", "kind": "agent", "prompt": "整理一下", "schedule": {"type": "daily", "time": "09:00"}, "sandbox": "workspace"})
    fake_sandbox.install(monkeypatch, problem="这台电脑上没有装 bubblewrap")
    check = run(config)[1]["sandbox"]
    assert check.status == PROBLEM and "每天整理" in check.detail


def test_computer_use(make_config, clean, monkeypatch):
    config = make_config()
    found = run(config)[1]
    assert found["computer"].status == UNUSED and "只能在 macOS 和 Linux" in found["computer"].detail  # conftest：这台"电脑"上用不了
    settings_of(config).set_computer({"enabled": True})
    assert run(config)[1]["computer"].status == PROBLEM
    mac = FakeDesktop()
    mac.problems = {"look": "没有截屏的权限"}
    monkeypatch.setattr(desktop_backend, "detect", lambda: (mac, ""))
    check = run(config)[1]["computer"]
    assert check.status == PROBLEM and "录屏与系统录音" in check.detail and "“终端”" in check.fix[0]
    mac.problems = {}
    assert run(config)[1]["computer"].status == OK

    class X11(FakeDesktop):
        def checks(self) -> dict:
            return {"xdotool": False, "capture": True, "convert": False}

    monkeypatch.setattr(desktop_backend, "detect", lambda: (X11(platform="x11"), ""))
    check = run(config)[1]["computer"]
    assert check.status == PROBLEM and "sudo apt install xdotool imagemagick" in check.fix[0]


class FakeManager:
    """一个假的服务管家：只会回答现在的样子。"""

    name = "systemd"

    def __init__(self, state: State, plan=None):
        self._state, self._plan, self.file = state, plan, Path("/fake/zhifox.service")

    def state(self) -> State:
        return self._state

    def read_plan(self):
        return self._plan


class BrokenManager(FakeManager):
    def __init__(self):
        super().__init__(State(False))

    def state(self) -> State:
        raise AssertionError("测试里不许运行真的系统命令")


def test_a_service_that_cannot_be_checked_does_not_crash_the_doctor(make_config, clean):
    config = make_config()
    report, found = run(config, service_cli.System(BrokenManager()))
    assert found["service"].status == PROBLEM and "查不了" in found["service"].detail
    assert "uv run harness service status" in found["service"].fix[0]
    assert len(report.sections) == 7 and "mcp" in found  # 别的照常看完
    # 不给假系统：这台电脑真的服务管家，conftest 把运行系统命令的函数换成了一调用就失败的。照样不崩
    report = examine(config)
    service = next(check for check in report.checks() if check.id == "service")
    assert service.status in (PROBLEM, UNUSED)
    if sys.platform == "darwin":
        assert "查不了" in service.detail


def test_service_states(make_config, clean):
    config = make_config()
    plan = make_plan(config, environ={})

    def service(state: State, environ: dict | None = None):
        return run(config, service_cli.System(FakeManager(state, plan), environ=environ or {}))[1]["service"]

    assert service(State(False)).status == UNUSED  # 没装
    check = service(State(True, loaded=True, running=True, pid=4242, enabled=True))
    assert check.status == OK and "4242" in check.detail
    assert service(State(True, loaded=True, enabled=True)).status == UNUSED  # 装了，你自己停下的
    check = service(State(True, loaded=True, exit_code=2))
    assert check.status == PROBLEM and "出错" in check.detail and any("uv run harness service logs" in line for line in check.fix)
    # 只在终端里设置了的密钥：后台服务读不到（只写名字，不写值）
    shell_only = {"OPENAI_API_KEY": "sk-only-in-the-shell-Zz9Y"}
    report = run(config, service_cli.System(FakeManager(State(True, loaded=True, running=True, pid=1), plan), environ=shell_only))[0]
    check = next(item for item in report.checks() if item.id == "service")
    assert check.status == PROBLEM and "OPENAI_API_KEY" in check.detail and "Zz9Y" not in all_text(report)
    assert any("uv run harness service restart" in line for line in check.fix)
    check = run(config, service_cli.System(FakeManager(State(True, loaded=True), None)))[1]["service"]
    assert check.status == PROBLEM and "看不懂" in check.detail  # 服务的设置文件被手改坏了


def test_who_holds_the_data_folder(make_config, clean):
    config = make_config()
    assert run(config)[1]["holder"].detail == "现在没有别的智狐开着"
    handle = instance.claim(config.data_dir, "web")
    try:
        before = (config.data_dir / instance.LOCK_NAME).read_bytes()
        check = run(config)[1]["holder"]
        assert check.status == OK and "harness --web" in check.detail and str(os.getpid()) in check.detail
        assert (config.data_dir / instance.LOCK_NAME).read_bytes() == before  # 只看一眼，不动"是谁占着"那一行
    finally:
        handle.close()


# ---- 扩展 ---------------------------------------------------------------------------


def test_extensions_find_missing_programs_and_unpaired_channels(make_config, clean):
    config = make_config()
    private_dir(config.data_dir)
    write_file(config.mcp_file, json.dumps({"mcpServers": {
        "good": {"command": sys.executable},
        "gone": {"command": "zhifox-no-such-program"},
        "off": {"command": "zhifox-no-such-program", "enabled": False},
        "web": {"url": "https://example.com/mcp"},
        "bad": {"url": "ftp://example.com"},
    }}))
    write_file(config.hooks_file, json.dumps({"hooks": {
        "check": {"event": "Stop", "command": sys.executable},
        "lost": {"event": "Stop", "command": "zhifox-no-such-program"},
    }}))
    ChannelStore(config.channels_file).connect("telegram", {"token": "123:FAKE"}, {"id": 1, "username": "zhifox_test_bot"})
    TaskStore(config.tasks_file).add({"title": "提醒喝水", "kind": "reminder", "schedule": {"type": "daily", "time": "10:00"}})
    found = run(config)[1]
    mcp = found["mcp"]
    assert mcp.status == PROBLEM and "gone" in mcp.detail and "bad" in mcp.detail and "off" not in mcp.detail
    assert "5 个，开着 3 个" in mcp.detail and mcp.facts["missing"] == ["gone"]
    hooks = found["hooks"]
    assert hooks.status == PROBLEM and "lost" in hooks.detail and "check" not in hooks.detail
    channels = found["channels"]
    assert channels.status == PROBLEM and "还没有配对" in channels.detail and "uv run harness channels pair telegram" in channels.fix[0]
    assert found["tasks"].status == OK and "现在都没开着" in found["tasks"].detail
    # 配对好了、程序都在：好
    data = json.loads(config.channels_file.read_text(encoding="utf-8"))
    data["platforms"]["telegram"]["owner"] = {"id": "42", "name": "我", "paired_at": 1}
    write_file(config.channels_file, json.dumps(data))
    write_file(config.mcp_file, json.dumps({"mcpServers": {"good": {"command": sys.executable}}}))
    write_file(config.hooks_file, json.dumps({"hooks": {"check": {"event": "Stop", "command": sys.executable}}}))
    found = run(config)[1]
    assert [found[name].status for name in ("mcp", "channels", "hooks", "tasks")] == [OK, OK, OK, OK]


def test_the_doctor_changes_nothing(make_config, clean, tmp_path):
    """自检只看不改：跑完以后数据目录和 .env 一个字节、一个权限都没变。坏掉的任务文件也不会被改名（TaskStore 会那样做）。"""
    config = make_config()
    private_dir(config.data_dir)
    write_file(config.tasks_file, "{ broken")
    write_file(config.settings_file, json.dumps({"provider": "anthropic", "model": "claude-opus-5-5"}))
    write_file(config.mcp_file, json.dumps({"mcpServers": {"x": {"command": "zhifox-no-such-program"}}}))
    write_file(config.env_file, "A=1\n")

    def snapshot() -> dict:
        return {
            str(path.relative_to(tmp_path)): (stat.S_IMODE(path.stat().st_mode), path.stat().st_mtime_ns, path.read_bytes() if path.is_file() else b"")
            for path in sorted(tmp_path.rglob("*"))
        }

    before = snapshot()
    found = run(config)[1]
    examine(config, NO_SERVICE)
    assert snapshot() == before
    assert found["tasks"].status == PROBLEM and "读不出来" in found["tasks"].detail
    assert config.tasks_file.is_file() and not list(config.data_dir.glob("*.broken.json"))


# ---- 网络（只看设置） -------------------------------------------------------------------


def test_network_settings_without_sending_anything(make_config, clean, monkeypatch):
    config = make_config(web=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://bob:hunter2hunter2@proxy.example:3128/path?x=1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:9999")
    found = run(config)[1]
    assert found["proxy"].status == OK and "http://proxy.example:3128" in found["proxy"].detail and "hunter2" not in found["proxy"].detail
    assert found["addresses"].status == OK and "ANTHROPIC_BASE_URL = http://localhost:9999" in found["addresses"].detail
    assert found["web_search"].status == UNUSED
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://api.example.com/v1")  # 明文、又在外面：密钥会以明文发过去
    check = run(config)[1]["addresses"]
    assert check.status == PROBLEM and "ANTHROPIC_BASE_URL" in check.detail and "ANTHROPIC_BASE_URL" in check.fix[0]


# ---- 命令行、终端、语言 ------------------------------------------------------------------


def test_exit_code_on_the_command_line(tmp_path, monkeypatch, capsys, clean):
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(service_cli, "detect", lambda: NO_SERVICE)
    assert cli.main(["doctor"]) == 1  # 没有密钥
    out = capsys.readouterr().out
    assert "✗ 有问题" in out and "1 项有问题" in out and "\033[" not in out  # 不是终端：没有颜色
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-0000000000000000")
    assert cli.main(["doctor"]) == 0
    capsys.readouterr()
    assert cli.main(["doctor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["problem"] == 0


def test_the_doctor_slash_command_in_the_terminal(start, capsys, monkeypatch):
    ui = TerminalUI()
    s = start(ui=ui)
    monkeypatch.setattr(service_cli, "detect", lambda: NO_SERVICE)
    capsys.readouterr()
    assert cli.handle_command("/doctor", s.app, ui) is True
    out = capsys.readouterr().out
    assert "智狐自检" in out and "Claude (Anthropic)" in out and "→" in out
    assert "/doctor" in cli.HELP and "/doctor" in cli.COMMANDS


def test_the_table_is_in_the_interface_language(make_config, clean, monkeypatch, tmp_path):
    config = make_config()
    for language, word in (("en", "Not used"), ("es", "Sin usar")):
        monkeypatch.setattr(i18n, "current", language)
        text = "\n".join(line for line, _ in lines(examine(config, NO_SERVICE)))
        assert word in text
        # 路径（这台 Mac 上项目在"个人项目"里）和测试自己写的中文（假沙箱的名字、假系统的说明）不算
        for path in (str(PROJECT_ROOT), sys.executable, str(tmp_path), "假沙箱", NO_SERVICE.unsupported):
            text = text.replace(path, "")
        leftovers = sorted({line for line in text.splitlines() if CJK.search(line.replace("智狐", ""))})
        assert leftovers == [], f"{language}：还有没翻译的中文：{leftovers}"


def test_the_columns_line_up_with_chinese_names(make_config, clean):
    """汉字在终端里占两格：按格数对齐，"现在用的模型"和"uv"后面的那一列从同一格开始。"""
    report = examine(make_config(), NO_SERVICE)
    rows = dict(lines(report))
    starts = set()
    for check in report.checks():
        if columns(str(check.label)) > NAME_COLUMNS or not check.detail:
            continue
        row = next(text for text in rows if text.endswith(str(check.detail)) and str(check.label) in text)
        starts.add(columns(row[: len(row) - len(str(check.detail))]))
    assert len(starts) == 1, starts


def test_fixes_follow_how_zhifox_was_installed(monkeypatch):
    # 装成了工具（config.FROM_SOURCE 为假）时没有"智狐的文件夹"，命令也不是 uv run harness（见 install.py）
    monkeypatch.setattr(fixes, "FROM_SOURCE", False)
    monkeypatch.setattr(fixes, "COMMAND", "harness")
    line = str(fixes.harness("service", "restart"))
    assert "harness service restart" in line and "uv run" not in line and str(PROJECT_ROOT) not in line
    assert "uv run" not in " ".join(map(str, fixes.add_key("ANTHROPIC_API_KEY", Path("/nowhere/.env"))))
    monkeypatch.setattr(fixes, "FROM_SOURCE", True)
    monkeypatch.setattr(fixes, "COMMAND", "uv run harness")
    line = str(fixes.harness("service", "restart"))
    assert "uv run harness service restart" in line and str(PROJECT_ROOT) in line
