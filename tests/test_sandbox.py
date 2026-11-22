"""命令的沙箱：规矩（policy.py）、各个系统的写法（macos.py 的 SBPL、linux.py 的 bwrap 参数、windows.py）、一次运行的沙箱（session.py）。

这里不建真的沙箱：只看生成出来的配置和参数对不对、"能不能用"怎么判断。真的沙箱在 test_sandbox_macos.py（这台 Mac）和
test_sandbox_linux.py（CI 里的 ubuntu），网页、接口、命令行、和助手接起来的样子在 test_sandbox_web.py。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fake_sandbox import REAL_DETECT, install
from modes import has_mode

from harness.i18n import Message
from harness.sandbox import backend as backends
from harness.sandbox import linux, macos, policy, windows
from harness.sandbox.policy import HOME_SECRETS, HOME_STARTUP, build, real, resolve
from harness.sandbox.session import Facts, Layout, Sandbox, question
from harness.tools.base import ToolError
from harness.tools.shell import Shell, make_shell_tool
from harness.trace.recorder import Tracer
from harness.trace.text import _row

# ---- 规矩 ------------------------------------------------------------------

# Windows 上的命令是 PowerShell（没有 touch、>&2 这些写法），而且 Windows 上本来就没有沙箱（见 harness/sandbox/windows.py）
BASH_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="这一项用 bash 的写法运行命令；Windows 上的命令是 PowerShell，沙箱在 Windows 上也用不了")
# macOS（SBPL）和 Linux（bubblewrap）的后端怎么写参数：用的文件夹名里故意带着引号、换行、反斜杠，Windows 上的文件名不许有这些
POSIX_NAMES = pytest.mark.skipif(sys.platform == "win32", reason="这一项测 macOS、Linux 的沙箱后端，用的文件夹名里带引号和换行：Windows 上的文件名不能有这些，那里也没有沙箱")



@pytest.fixture
def place(tmp_path):
    """一个工作目录（里面有 data/ 和 .env），一个假的家目录（有 .ssh、.zshrc），这次运行的 HOME 和临时文件夹。"""
    workspace = tmp_path / "work"
    (workspace / "data").mkdir(parents=True)
    (workspace / ".env").write_text("KEY=x\n")
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".zshrc").write_text("# rc\n")
    scratch = tmp_path / "scratch"
    (scratch / "home").mkdir(parents=True)
    (scratch / "tmp").mkdir()
    return workspace, home, scratch


def make(place, level="workspace", extra=(), program=()):
    workspace, home, scratch = place
    return build(
        level=level, workspace=workspace, extra=list(extra), private=[workspace / "data"], protected=[workspace / ".env"],
        home=scratch / "home", temp=scratch / "tmp", real_home=home, program=program,
    )


def test_levels_and_following_the_settings():
    assert list(policy.LEVELS) == ["off", "workspace", "network"]
    assert policy.CHOICES == ("default", "off", "workspace", "network")
    assert resolve("default", "network") == "network"
    assert resolve("off", "workspace") == "off"
    assert resolve("workspace", "off") == "workspace"
    assert resolve("default", "什么") == "workspace"  # 认不出来的按最严的那一档（关以外）


def test_what_a_command_can_reach(place):
    workspace, home, scratch = place
    found = make(place)
    assert found.write == (real(workspace), real(scratch / "home"), real(scratch / "tmp"))
    assert found.read == ()
    # 数据目录、.env、家目录里放密钥的地方：读写都不行
    assert real(workspace / "data") in found.deny and real(workspace / ".env") in found.deny
    assert all(real(home) / name in found.deny for name in HOME_SECRETS)
    # 开终端时会自动运行的文件：在能写的地方里也只能读
    assert all(real(home) / name in found.readonly for name in HOME_STARTUP)
    assert not found.network and make(place, "network").network


def test_extra_paths(place, tmp_path):
    workspace = place[0]
    tools, shared = tmp_path / "tools", tmp_path / "shared"
    found = make(place, extra=[{"path": str(tools), "write": False}, {"path": str(shared), "write": True}, {"path": str(workspace / "data" / "x"), "write": True}])
    assert real(tools) in found.read and real(shared) in found.write
    # 数据目录里面的：加了也不算
    assert all(not path.is_relative_to(real(workspace / "data")) for path in (*found.read, *found.write))


def test_a_secret_place_is_open_only_when_you_add_it_yourself(place):
    _, home, _ = place
    # 整个家目录加进来：~/.ssh 照样挡着
    whole = make(place, extra=[{"path": str(home), "write": True}])
    assert real(home) in whole.write and real(home) / ".ssh" in whole.deny
    # 单独加了 ~/.ssh：不挡它（别的照样挡）
    ssh = make(place, extra=[{"path": str(home / ".ssh"), "write": False}])
    assert real(home) / ".ssh" not in ssh.deny and real(home) / ".aws" in ssh.deny
    # 单独加了 ~/.zshrc 可读写：它就能写
    rc = make(place, extra=[{"path": str(home / ".zshrc"), "write": True}])
    assert real(home) / ".zshrc" not in rc.readonly


def test_zhifox_own_program_is_read_only(place):
    workspace, _, _ = place
    (workspace / "harness").mkdir()
    found = make(place, program=[workspace / "harness"])
    assert found.program == (real(workspace / "harness"),) and real(workspace / "harness") in found.readonly
    paths = policy.program_paths()
    assert real(Path(policy.__file__).parent.parent) in paths  # 包本身
    assert real(sys.prefix) in paths  # 正在用的 Python 环境


def test_the_environment_inside(place, tmp_path):
    scratch = place[2]
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    found = make(place, program=[venv])
    env = found.env({
        "HOME": "/Users/me", "TMPDIR": "/var/x", "PATH": os.pathsep.join([str(venv / "bin"), "/usr/bin", ""]), "VIRTUAL_ENV": str(venv),
        "SSH_AUTH_SOCK": "/tmp/agent", "XDG_CONFIG_HOME": "/Users/me/.config", "LANG": "C",
    })
    assert env["HOME"] == str(real(scratch / "home"))
    assert env["TMPDIR"] == env["TMP"] == env["TEMP"] == str(real(scratch / "tmp"))
    assert env["PATH"] == os.pathsep.join(["/usr/bin", ""])  # 智狐自己的 Python 环境去掉了
    assert "VIRTUAL_ENV" not in env and "SSH_AUTH_SOCK" not in env and "XDG_CONFIG_HOME" not in env
    assert env["LANG"] == "C"


# ---- macOS：SBPL --------------------------------------------------------------


ODD = 'a "quote" (paren) \\back; slash=1\n换行 中文 $HOME `x`'


@POSIX_NAMES
def test_the_profile_never_contains_a_path(place, tmp_path):
    workspace, _, _ = place
    odd = tmp_path / ODD
    odd.mkdir()
    found = make(place, extra=[{"path": str(odd), "write": True}])
    text, params = macos.profile(found)
    # 路径只在参数里，原样；配置的文字里一个路径都没有，所以奇怪的文件名改不了规则
    assert str(real(odd)) in params.values() and str(real(workspace)) in params.values()
    for name, value in params.items():
        if not name.startswith("SYSTEM_"):  # 系统目录是写死的几个；你的工作目录、你加的路径一个都不在文字里
            assert value not in text
    assert '(param "WRITE_0")' in text


def test_the_profile_denies_by_default_and_denies_last(place):
    text, params = macos.profile(make(place))
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines[:3] == ["(version 1)", "(deny default)", '(import "system.sb")']
    allow_at = max(index for index, line in enumerate(lines) if line.startswith("(allow file-read*"))
    deny_at = min(index for index, line in enumerate(lines) if line.startswith("(deny file-read* file-write*"))
    assert deny_at > allow_at  # 同一个操作有好几条规则时，后面的说了算：永远碰不到的写在最后
    assert "network" not in text  # 不能联网：一条网络的规则都没有
    assert any(name.startswith("DENY_") and value.endswith("data") for name, value in params.items())


def test_an_empty_group_is_not_written(place):
    # (allow file-read*) 后面什么都不跟，意思是"哪儿都能读"：没有你加的只读路径时，不写这条
    text, params = macos.profile(make(place))
    assert "READ_" not in text and not any(name.startswith("READ_") for name in params)
    for line in text.splitlines():
        if line.startswith(("(allow file-read*", "(deny file-")):
            assert "(subpath" in line, line


def test_the_network_level(place):
    text, _ = macos.profile(make(place, "network"))
    assert "(allow network-outbound (remote ip))" in text and "(system-network)" in text
    assert "unix-socket" not in text  # Unix 套接字照样不行


def test_the_mac_command_line(place):
    found = make(place)
    argv = macos.command(found, ["/bin/bash", "-c", "ls"])
    assert argv[:2] == [macos.SANDBOX_EXEC, "-p"]
    assert argv[-3:] == ["/bin/bash", "-c", "ls"]
    assert all(arg.startswith("-D") and "=" in arg for arg in argv[3:-3])


def test_the_mac_probe(monkeypatch):
    macos.forget()
    monkeypatch.setattr(macos, "SANDBOX_EXEC", "/no/such/sandbox-exec")
    found = macos.problem()
    assert isinstance(found, Message) and "/no/such/sandbox-exec" in found
    # 记一会儿：再问不会再试
    monkeypatch.setattr(macos, "_try", lambda: pytest.fail("不该再试一次"))
    assert macos.problem() is found
    macos.forget()


@BASH_ONLY
def test_the_mac_probe_when_sandbox_exec_refuses(monkeypatch, tmp_path):
    macos.forget()
    program = tmp_path / "sandbox-exec"
    program.write_text("#!/bin/sh\necho 'sandbox_apply: Operation not permitted' >&2\nexit 71\n")
    program.chmod(0o755)
    monkeypatch.setattr(macos, "SANDBOX_EXEC", str(program))
    found = macos.problem()
    assert found is not None and "sandbox_apply: Operation not permitted" in found
    macos.forget()


# ---- Linux：bwrap ---------------------------------------------------------------


def _pairs(args: list[str], flag: str) -> list[tuple[str, str]]:
    return [(args[index + 1], args[index + 2]) for index, arg in enumerate(args) if arg == flag]


@POSIX_NAMES
def test_the_bwrap_arguments(place, tmp_path):
    workspace = place[0]
    tools = tmp_path / ODD
    tools.mkdir()
    (workspace / "harness").mkdir()
    found = make(place, extra=[{"path": str(tools), "write": False}], program=[workspace / "harness"])
    args = linux.arguments(found)
    assert args[:3] == ["--die-with-parent", "--unshare-pid", "--unshare-ipc"]
    assert "--unshare-net" in args  # 只在工作目录里：自己的一套网络
    assert (str(real(workspace)), str(real(workspace))) in _pairs(args, "--bind")
    assert (str(real(tools)), str(real(tools))) in _pairs(args, "--ro-bind-try")  # 路径原样，一个参数一个
    # 智狐自己的程序：在工作目录里面也再挂一次只读的，盖在上面
    bind_at = args.index(str(real(workspace)))
    ro_at = max(index for index, arg in enumerate(args) if arg == str(real(workspace / "harness")))
    assert ro_at > bind_at and args[ro_at - 2] == "--ro-bind"
    # 永远碰不到的：文件夹盖上一个空的只读的，文件换成 /dev/null，写在能读写的后面
    data = str(real(workspace / "data"))
    assert args[args.index("--tmpfs", args.index("/tmp") + 1) + 1] == data
    assert args[args.index(data) + 1 : args.index(data) + 3] == ["--remount-ro", data]
    assert ("/dev/null", str(real(workspace / ".env"))) in _pairs(args, "--ro-bind")
    assert args.index(data) > bind_at
    assert args[-2:] == ["--chdir", str(real(workspace))]


def test_bwrap_covers_a_key_file_that_does_not_exist_yet(place):
    workspace, _, _ = place
    (workspace / ".env").unlink()
    args = linux.arguments(make(place))
    # 还没有 .env：照样盖上 /dev/null，不然命令可以新建一个，智狐下次启动时会读它
    assert ("/dev/null", str(real(workspace / ".env"))) in _pairs(args, "--ro-bind")


def test_bwrap_does_not_mount_what_is_invisible_anyway(place):
    home = place[1]
    args = linux.arguments(make(place))
    # 家目录不在能读写的地方：~/.ssh 本来就看不到，不用再盖
    assert str(real(home) / ".ssh") not in args


def test_bwrap_with_network(place):
    args = linux.arguments(make(place, "network"))
    assert "--unshare-net" not in args and "--unshare-pid" in args


def test_the_linux_command_line(place, monkeypatch):
    monkeypatch.setattr(linux, "bwrap", lambda: "/usr/bin/bwrap")
    argv = linux.command(make(place), ["/bin/bash", "-c", "ls"])
    assert argv[0] == "/usr/bin/bwrap" and argv[-4:] == ["--", "/bin/bash", "-c", "ls"]


def test_the_linux_probe_without_bwrap(monkeypatch):
    linux.forget()
    monkeypatch.setattr(linux, "bwrap", lambda: None)
    found = linux.problem()
    assert found is not None and "bubblewrap" in found and "sudo apt install bubblewrap" in found and "sudo dnf install bubblewrap" in found
    linux.forget()


def test_the_linux_probe_when_user_namespaces_are_off(monkeypatch):
    linux.forget()
    monkeypatch.setattr(linux, "bwrap", lambda: "/usr/bin/bwrap")

    def refuse(argv, **kwargs):
        assert argv[0] == "/usr/bin/bwrap" and "--unshare-net" in argv
        return subprocess.CompletedProcess(argv, 1, "", "bwrap: setting up uid map: Permission denied\n")

    monkeypatch.setattr(linux.subprocess, "run", refuse)
    found = linux.problem()
    assert found is not None and "setting up uid map: Permission denied" in found and "用户命名空间" in found
    linux.forget()


# ---- Windows 和别的系统 ----------------------------------------------------------


def test_windows_says_why_there_is_no_sandbox():
    found = windows.problem()
    assert isinstance(found, Message) and "AppContainer" in found
    with pytest.raises(RuntimeError):
        backends.WINDOWS.command(None, ["x"])  # 永远不会去运行
    assert backends.WINDOWS.id == "none" and "WSL" in windows.NOTE


@pytest.mark.parametrize("platform, expected", [("darwin", "sandbox-exec"), ("linux", "bwrap"), ("win32", "none"), ("freebsd14", "none")])
def test_which_backend_each_system_uses(monkeypatch, platform, expected):
    monkeypatch.setattr(backends.sys, "platform", platform)
    found = REAL_DETECT()  # conftest 把 detect 换成了假的：这里看真的那一个
    assert found.id == expected
    if expected == "none":
        assert found.problem() is not None  # 没有沙箱的系统：明说用不了


# ---- 一次运行的沙箱 ----------------------------------------------------------------


@pytest.fixture
def layout(place):
    workspace, home, _ = place
    level = {"value": "workspace"}
    found = Layout(workspace=workspace, private=(workspace / "data",), protected=(workspace / ".env",), extra=list, default=lambda: level["value"], home=lambda: home)
    return found, level


def test_the_level_of_a_run(layout):
    found, level = layout
    assert Sandbox(found).level() == "workspace"
    assert Sandbox(found, own=lambda: "off").level() == "off"
    level["value"] = "off"
    assert Sandbox(found).level() == "off"
    assert Sandbox(found, own=lambda: "network").level() == "network"


def test_the_scratch_folders_are_private_and_shared_within_a_run(layout, monkeypatch):
    fake = install(monkeypatch)
    found, _ = layout
    box = Sandbox(found)
    first = box.enter(["true"], {})
    second = box.enter(["true"], {})
    assert first.env["HOME"] == second.env["HOME"]
    home = Path(first.env["HOME"])
    assert home.is_dir() and has_mode(home.parent, 0o700)  # Windows 上看的是 ACL（见 tests/modes.py）
    assert len(fake.calls) == 2
    assert Sandbox(found).enter(["true"], {}).env["HOME"] != first.env["HOME"]  # 另一次运行，另一个


def test_refusals_are_for_the_model_and_in_chinese(layout, monkeypatch):
    found, _ = layout
    install(monkeypatch, problem="没有装")
    message = Sandbox(found, scope="task").refusal()
    assert message.startswith("命令没有执行：这个定时任务的沙箱是“只在工作目录里”") and "没有装" in message and "不会悄悄" in message
    assert "unsandboxed" in message  # 能请人放行：告诉它有这条路
    assert "unsandboxed" not in Sandbox(found, escape=lambda: False).refusal()
    with pytest.raises(ToolError):
        Sandbox(found).enter(["true"], {})


def test_leaving_the_sandbox(layout, monkeypatch):
    fake = install(monkeypatch)
    found, _ = layout
    assert Sandbox(found).enter(["true"], {}, unsandboxed=True) is None  # 问过你、放行了：不包
    assert fake.calls == []
    stuck = Sandbox(found, escape=lambda: False, scope="channel")
    assert "这个聊天通道不能在沙箱之外运行命令" in stuck.refusal(unsandboxed=True)
    assert stuck.refusal() is None  # 在沙箱里照常运行


def test_explaining_a_block(layout, monkeypatch):
    install(monkeypatch)
    found, _ = layout
    jail = Sandbox(found).enter(["true"], {})
    assert jail.explain("ls: x: No such file") == ""  # 不像是沙箱挡住的：不多嘴
    note = jail.explain("cat: /x: Operation not permitted")
    assert note.startswith("\n[这条命令是在沙箱里运行的（只在工作目录里") and "unsandboxed" in note
    stuck = Sandbox(found, escape=lambda: False, scope="task").enter(["true"], {})
    assert "不能请用户放行" in stuck.explain("Operation not permitted")


def test_the_card_line(layout):
    on = Facts(level="network", default="workspace", scope="chat")
    assert question(on, "ls").endswith("沙箱：工作目录 + 联网（读写工作目录，能联网）")
    off = Facts(level="off", default="workspace", scope="channel")
    assert "沙箱：关（这个聊天通道没有用沙箱" in question(off, "ls")
    never = Facts(level="off", default="off", scope="chat")
    assert question(never, "ls") == "执行命令：\n\n    $ ls"
    assert question(never, "ls").key == "执行命令：\n\n    $ {command}"  # 从来没打开过的人：和以前一字不差


# ---- 接进 Shell --------------------------------------------------------------------


@BASH_ONLY
def test_a_shell_without_a_sandbox_is_unchanged(tmp_path):
    shell = Shell(tmp_path)
    assert shell.facts({"command": "ls"}) is None and shell.check({"command": "ls"}) is None
    assert shell.run("echo hi") == "hi"


@BASH_ONLY
def test_the_shell_wraps_refuses_and_explains(layout, monkeypatch, place):
    fake = install(monkeypatch)
    found, _ = layout
    workspace = place[0]
    shell = Shell(workspace, Sandbox(found))
    home = shell.run("echo $HOME")  # 假的后端照原样运行，只是记下来；HOME 已经换成这次运行自己的
    assert fake.calls[0][1] == ["/bin/bash", "-c", "echo $HOME"] and home == str(fake.calls[0][0].home)
    out = shell.run("echo 'cat: x: Operation not permitted'; exit 1")
    assert "[退出码 1]" in out and "[这条命令是在沙箱里运行的" in out
    fake.problem = "坏了"
    marker = workspace / "marker"
    with pytest.raises(ToolError, match="不会悄悄"):
        shell.run(f"touch {marker}")
    assert not marker.exists()  # 什么都没运行


def test_the_trace_records_the_sandbox(layout, monkeypatch, place):
    install(monkeypatch)
    found, level = layout
    tool = make_shell_tool(Shell(place[0], Sandbox(found)))

    def span_of(tool_input):
        tracer = Tracer()
        tracer.tool_began(SimpleNamespace(shown="run_command", input=tool_input), tool)
        return tracer.spans[-1].data

    assert span_of({"command": "ls"})["sandbox"] == "workspace"
    assert span_of({"command": "ls", "unsandboxed": True})["sandbox"] == "outside"
    level["value"] = "off"
    assert "sandbox" not in span_of({"command": "ls"})  # 从来没打开过：记录里没有这一项
    # 终端的 /trace 也写着
    span = {"kind": "tool", "start": 0, "end": 10, "name": "run_command", "label": "执行命令", "detail": "$ ls", "result": "ok", "sandbox": "outside"}
    assert "在沙箱之外运行（你放行的）" in _row(span, 0, 10)
