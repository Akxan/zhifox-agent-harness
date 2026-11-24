"""run_command 记着当前目录：上一条命令里的 cd 保留到下一条（规矩见 harness/tools/cwd.py）。

这里测：
  - 包的那一层和取回目录（wrap、take）：记号去掉以后的输出和以前一模一样，退出码原样；
  - 真的 shell 里：cd 接着用、环境变量不接着用、超时和停止留在原来的目录、目录不在了回到工作目录并说一声、
    问过你以后目录才不在了就不执行、exec > 文件 不往你的文件里写记号；
  - 判断"只读的命令"和确认卡片照着命令真正运行的目录（cd data 以后的 ls 不能当成"列工作目录"放过去）；
  - 沙箱：从哪个目录开始、沙箱里看不到的目录回到工作目录、bwrap 的 --chdir；
  - 一段对话里怎么走：新对话、重新打开、子 Agent、钩子的 command_cwd、倒回最后一轮（网页和终端）。

真的要执行的命令按这台电脑的 shell 写（tests/oscmd.py：macOS、Linux 是 bash，Windows 是 PowerShell）。
PowerShell 的那一层另外在装了 pwsh 的 macOS、Linux 上也跑一遍（GitHub 的三种机器上都装着 PowerShell 7），
真正的 Windows PowerShell 5.1 只在 CI 的 windows 上跑。
"""

from __future__ import annotations

import base64
import shutil
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_agents
import test_rewind_cli
import test_rewind_web
from fake_api import reply, text, tool_use
from fake_sandbox import install as install_fake
from oscmd import change_dir, mark_then_sleep, mark_then_wait_for, print_dir
from test_channels import rigs
from test_hooks import add, calls_of, script
from test_webui import act, open_conversation, say, serve, set_approval  # noqa: F401  serve 是 fixture

from harness import i18n
from harness.hooks import HookConfig, Hooks
from harness.commands.allowlist import CommandList
from harness.permissions import WHY_LISTED, WHY_READONLY, Permissions
from harness.sandbox import linux
from harness.sandbox.policy import build, real
from harness.sandbox.session import Layout, Sandbox
from harness.tools import cwd as cwd_module
from harness.tools import shell as shell_module
from harness.tools.base import ToolError
from harness.tools.cwd import CurrentDir, commands_in, marker, take, wrap
from harness.tools.files import FileEditor
from harness.tools.shell import TOOL_NAME as SHELL
from harness.tools.shell import Shell, make_shell_tool

rig = test_rewind_web.rig  # 网页服务器加上 s.web（倒回一轮要用）
terminal = test_rewind_cli.terminal  # 终端的主循环，输入换成写好的几行

needs_bash = pytest.mark.skipif(shell_module.WINDOWS, reason="这一项用 bash 的写法；Windows 上的命令是 PowerShell，见下面 PowerShell 的那几个")
needs_windows = pytest.mark.skipif(not shell_module.WINDOWS, reason="Windows PowerShell 5.1（或者 pwsh）真的运行只在 Windows 上测")
WINDOWS = shell_module.WINDOWS


def same(found: str | Path, expected: Path) -> bool:
    """两个路径是不是同一个目录：macOS 的 /var 和 /private/var、Windows 的短名字和大小写都不算不同。"""
    return Path(str(found).strip()).resolve() == expected.resolve()


def cmd(id: str, command: str) -> dict:
    return reply(tool_use(id, SHELL, {"command": command}), stop_reason="tool_use")


def result_of(api, tool_id: str) -> str:
    """最后一次带着 tool_id 这次调用的结果的请求里，那段结果的文字。"""
    for request in reversed(api.requests):
        for message in request["messages"]:
            if message["role"] == "user" and isinstance(message["content"], list):
                for block in message["content"]:
                    if block.get("type") == "tool_result" and block.get("tool_use_id") == tool_id:
                        return test_agents.result_text(block)
    raise AssertionError(f"没有 {tool_id} 的结果")


def set_env(name: str, value: str) -> str:
    return f"$env:{name} = '{value}'" if WINDOWS else f"export {name}={value}"


def show_env(name: str) -> str:
    return f'Write-Output "[$env:{name}]"' if WINDOWS else f'echo "[${name}]"'


# ---- 包的那一层、取回目录 ---------------------------------------------------------------


def test_the_marker_line_is_taken_out_and_the_output_is_as_before():
    mark = marker()
    assert mark.startswith("__ZHIFOX_CWD_") and mark != marker()  # 每条命令现抽一个
    # 命令的输出以换行结尾、不以换行结尾、什么都没有：去掉那一行以后都和没包之前一模一样
    for output in ("hi\n", "no newline", ""):
        assert take(f"{output}\n{mark}/w/sub{mark}\n", mark) == (output, "/w/sub")
    # 后台的程序在 shell 结束以后还往输出里写：那一行不在最后，照样找得到、去得掉
    assert take(f"early\n\n{mark}/w{mark}\nlate\n", mark) == ("early\nlate\n", "/w")
    # 路径里有换行也照样取回来（夹在两个记号中间）
    assert take(f"x\n{mark}/w/a\nb{mark}\n", mark) == ("x", "/w/a\nb")
    # 没走到那里（被结束掉了）、只有半截：输出原样，目录不知道
    for output in ("hi\n", f"hi\n{mark}/w/su"):
        assert take(output, mark) == (output, None)


def test_the_bash_wrapper_traps_exit_and_keeps_the_command_as_it_is():
    mark = marker()
    command = "cd 'a b' && echo \"$HOME\"; exit 3"
    script = wrap(command, mark, windows=False)
    first, rest = script.split("\n", 1)
    assert rest == command  # 命令一个字都不变，放在 trap 那一行后面（语法错误、exit 都会走到 trap）
    assert first.startswith("trap '") and first.endswith("' EXIT") and first.count(mark) == 2
    assert "exit $__zhifox_status" in first and "[ -p /dev/stdout ]" in first and '"$PWD"' in first


def test_the_powershell_wrapper_keeps_the_command_and_the_exit_code_rule(monkeypatch):
    """Windows 上包好的命令整个编码以后交给 PowerShell（每个系统上都能测：只看拼出来的参数，不运行）。"""
    monkeypatch.setattr(shell_module, "WINDOWS", True)
    monkeypatch.setattr(shell_module.shutil, "which", lambda name: None)
    mark = marker()
    command = 'Set-Location "C:\\Program Files"; Write-Output "你好 $env:PATH"'
    args = shell_module.command_line(wrap(command, mark, windows=True))
    script = base64.b64decode(args[5]).decode("utf-16-le")
    prelude, body = script.split("\n", 1)
    assert "OutputEncoding" in prelude  # 设输出编码的那一行还在最前面
    assert body.startswith("try {\n" + command + "\n} finally {\n")  # 命令原样放在 try 里
    assert "$__zhifox_ok = $?" in body and "(Get-Location -PSProvider FileSystem).ProviderPath" in body and body.count(mark) == 2
    assert body.endswith("if (-not $__zhifox_ok) { exit 1 }")  # "最后一条不成功就是 1"，和没包之前一样


def test_only_commands_of_the_main_assistant_count_as_having_run():
    assert commands_in([{"type": "tool_call", "name": SHELL, "input": {"command": "cd x"}}])
    assert not commands_in([{"type": "user", "text": "hi"}, {"type": "tool_call", "name": "todo_add"}])
    assert not commands_in([{"type": "subagent", "step": "tool_call", "name": SHELL}])  # 子 Agent 的目录本来就不跟着主助手


def test_the_current_dir_by_itself(tmp_path):
    current = CurrentDir(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    for nonsense in (None, "", "relative/dir", "/a\x00b"):
        current.moved(nonsense)
        assert current.path == tmp_path  # 看不懂的不记
    current.moved(str(sub))
    assert current.where() == sub and current.take_note() == ""
    current.start_turn()
    current.moved(str(tmp_path))
    current.rewind_turn()
    assert current.path == sub  # 回到这一轮开始时
    current.reset()
    assert current.path == tmp_path and current.take_note() == ""
    current.reset(reopened=True)
    assert "重新打开" in current.take_note() and current.take_note() == ""  # 只说一次


# ---- 真的 shell ---------------------------------------------------------------------------


@pytest.fixture
def ws(tmp_path):
    workspace = tmp_path / "work"
    (workspace / "sub" / "inner").mkdir(parents=True)
    return workspace


def test_cd_carries_over_and_environment_variables_do_not(ws):
    shell = Shell(ws)
    assert shell.run(f"{change_dir('sub')}; {set_env('ZF_SEEN', 'yes')}; {show_env('ZF_SEEN')}") == "[yes]"
    assert same(shell.current.path, ws / "sub")
    assert same(shell.run(print_dir()), ws / "sub")  # 下一条从 sub 开始
    assert shell.run(show_env("ZF_SEEN")) == "[]"  # 环境变量不接着用
    assert same(shell.run(f"{change_dir('inner')}; {print_dir()}"), ws / "sub" / "inner")
    assert same(shell.run(f"{change_dir('..')}; {print_dir()}"), ws / "sub")


def test_the_exit_code_is_kept_and_the_directory_still_moves(ws):
    shell = Shell(ws)
    out = shell.run(f"{change_dir('sub')}; Write-Output out; exit 3" if WINDOWS else f"{change_dir('sub')}; echo out; exit 3")
    assert out == "out\n[退出码 3]"  # 记号那一行去掉了，退出码原样
    assert same(shell.current.path, ws / "sub")  # exit 也走到了打目录的那一步
    assert shell.run("$null" if WINDOWS else "true") == "（没有输出）"


@needs_bash
def test_bash_failures_keep_their_exit_codes(ws):
    shell = Shell(ws)
    assert shell.run("cd sub; false").endswith("[退出码 1]") and same(shell.current.path, ws / "sub")
    assert "[退出码 2]" in shell.run("cd ..; if then")  # 语法错误：这一条什么都没运行，目录不变
    assert same(shell.current.path, ws / "sub")
    assert "[退出码 127]" in shell.run("no_such_program_zf")
    assert shell.run("printf 'no newline'") == "no newline"
    assert shell.run("cd ..; (sleep 0.3; echo late) & echo early") == "early\nlate"  # 后台的输出在记号之后
    assert same(shell.current.path, ws)


@needs_bash
def test_a_command_that_sends_its_output_to_a_file_does_not_get_the_marker(ws):
    shell = Shell(ws)
    shell.run("cd sub")
    assert shell.run("exec > out.txt; echo mine; cd inner") == "（没有输出）"
    assert (ws / "sub" / "out.txt").read_text() == "mine\n"  # 你的文件里只有命令自己写的
    assert same(shell.current.path, ws / "sub")  # 不知道它停在哪儿：留在原来的目录


@needs_bash
def test_a_logical_path_through_a_symlink_is_kept(ws, tmp_path):
    (tmp_path / "link").symlink_to(ws / "sub", target_is_directory=True)
    shell = Shell(ws)
    shell.run(f"cd '{tmp_path / 'link'}'")
    assert shell.run("pwd") == str(tmp_path / "link")  # 不是换成真正的路径：和模型 cd 时写的一样


def test_a_timeout_stays_where_it_was(ws):
    shell = Shell(ws)
    out = shell.run(f"{change_dir('sub')}; {mark_then_sleep(str(ws / 'started'), 30)}", timeout_seconds=1)
    assert "超过 1 秒" in out and "__ZHIFOX_CWD_" not in out
    assert shell.current.path == ws  # 停在哪儿说不准：留在原来的目录


def test_a_stopped_command_stays_where_it_was(ws):
    shell = Shell(ws)
    started, go = ws / "started", ws / "go"
    failure: list[BaseException] = []

    def run() -> None:
        try:
            shell.run(f"{change_dir('sub')}; {mark_then_wait_for(str(started), str(go))}", timeout_seconds=60)
        except ToolError as exc:
            failure.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.monotonic() + 30
    while not started.exists():
        assert time.monotonic() < deadline, "命令一直没开始"
        time.sleep(0.05)
    shell.kill_running()
    worker.join(30)
    assert failure and "被用户停止了" in str(failure[0]) and "__ZHIFOX_CWD_" not in str(failure[0])
    assert shell.current.path == ws


def test_a_directory_that_is_gone_falls_back_to_the_workspace(ws):
    shell = Shell(ws)
    shell.run(change_dir("sub"))
    shutil.rmtree(ws / "sub")
    # 问你之前先看一眼（permissions.py 就是这样）：当场回到工作目录，卡片上、运行时都是它
    assert shell.where() == ws
    out = shell.run(print_dir())
    first, note = out.split("\n", 1)
    assert same(first, ws)
    assert note.startswith("[注意：上一条命令结束时所在的目录") and "已经不在了" in note and str(ws) in note
    assert "已经不在了" not in shell.run(print_dir())  # 只说一次


def test_a_directory_that_goes_away_after_you_were_asked_is_not_used(ws):
    shell = Shell(ws)
    shell.run(change_dir("sub"))
    marker_file = ws / "ran"
    assert same(shell.where(), ws / "sub")  # 问你的时候还在
    shutil.rmtree(ws / "sub")
    with pytest.raises(ToolError, match="命令没有执行") as caught:
        shell.run(f"New-Item -ItemType File '{marker_file}'" if WINDOWS else f"touch '{marker_file}'")
    assert "重新执行" in str(caught.value)
    assert not marker_file.exists()  # 没有换个地方悄悄运行
    assert shell.current.path == ws and "已经不在了" not in shell.run(print_dir())  # 那一句已经在拒绝里说过了


@needs_bash
def test_a_subshell_cd_does_not_count(ws):
    # PowerShell 没有这回事：Set-Location 在 & { } 里也改的是整个会话的位置
    shell = Shell(ws)
    assert same(shell.run("(cd sub); pwd"), ws) and same(shell.current.path, ws)


# ---- PowerShell 的那一层：装了 pwsh 的 macOS、Linux 上也跑一遍 --------------------------------


PWSH = shutil.which("pwsh")
needs_pwsh = pytest.mark.skipif(PWSH is None or WINDOWS, reason="这台电脑上没有 PowerShell 7（pwsh）；Windows 上见上面真的那几个")


@needs_pwsh
def test_the_powershell_wrapper_on_pwsh(ws, monkeypatch):
    monkeypatch.setattr(shell_module, "WINDOWS", True)  # 命令行、包的那一层都按 Windows 的写：PowerShell
    shell = Shell(ws)
    assert shell.run("Set-Location sub; $env:ZF_SEEN = 'yes'; Write-Output in") == "in"
    assert same(shell.current.path, ws / "sub")
    assert same(shell.run("(Get-Location).Path"), ws / "sub") and shell.run('Write-Output "[$env:ZF_SEEN]"') == "[]"
    assert shell.run("Set-Location inner; Write-Output out; exit 3") == "out\n[退出码 3]"
    assert same(shell.current.path, ws / "sub" / "inner")
    assert "[退出码 1]" in shell.run("Set-Location ..; Get-Item no-such-file-zf")  # 最后一条不成功：1，和没包之前一样
    assert same(shell.current.path, ws / "sub")
    assert "[退出码 1]" in shell.run("Set-Location ..; throw 'boom'") and same(shell.current.path, ws)  # 抛出的错误也走 finally
    assert shell.run("& bash -c 'exit 5'; Write-Output after") == "after"  # 最后一条成功：0


@needs_windows
def test_powershell_on_windows_keeps_its_exit_code_rule(ws):
    shell = Shell(ws)
    assert "[退出码 1]" in shell.run("Get-Item no-such-file-zf")
    assert shell.run("cmd /c exit 5; Write-Output after") == "after"
    assert "[退出码 1]" in shell.run("Set-Location sub; throw 'boom'") and same(shell.current.path, ws / "sub")


# ---- 问不问你：照着命令真正运行的目录 ------------------------------------------------------


@pytest.fixture
def guard(tmp_path):
    """一个工作目录（里面有 data/、src/），按它组装的 Permissions（"只读的命令不用问"那一档），一个 Shell 和它的工具。"""
    workspace = tmp_path / "work"
    data = workspace / "data"
    (workspace / "src").mkdir(parents=True)
    data.mkdir()
    (data / "settings.json").write_text("{}\n", encoding="utf-8")
    (workspace / "src" / "a.py").write_text("print(1)\n", encoding="utf-8")
    editor = FileEditor(workspace, data / "backups", protected=[workspace / ".env"])
    permissions = Permissions(editor, private=[data])
    permissions.mode = "readonly"
    shell = Shell(workspace)
    return SimpleNamespace(workspace=workspace, data=data, permissions=permissions, shell=shell, tool=make_shell_tool(shell))


def allowance(guard, command: str) -> str | None:
    return guard.permissions.allowance(SHELL, {"command": command}, guard.tool)


def test_read_only_commands_are_judged_where_they_run(guard):
    assert allowance(guard, "ls") == WHY_READONLY  # 在工作目录里列一下：只是看
    guard.shell.current.moved(str(guard.data))  # 上一条命令 cd data（cd 本身照常问过你）
    assert allowance(guard, "ls") is None  # 列的是智狐自己的数据目录：要问
    assert allowance(guard, "cat settings.json") is None
    guard.shell.current.moved(str(guard.workspace.parent))  # cd .. 到了工作目录外面
    assert allowance(guard, "cat work/data/settings.json") is None  # 还是数据目录里的文件
    guard.shell.current.moved(str(guard.workspace / "src"))
    assert allowance(guard, "cat a.py") == WHY_READONLY and allowance(guard, "ls") == WHY_READONLY  # 普通的地方照样不用问
    secrets = guard.workspace / "credentials"
    secrets.mkdir()
    guard.shell.current.moved(str(secrets))
    assert allowance(guard, "ls") is None  # 目录本身看起来存着密钥：在那里什么命令都不算只读
    # 只给了名字、认不出是哪个 shell 的（测试、只按名字判断的地方）：按工作目录算，和以前一样
    assert guard.permissions.allowance(SHELL, {"command": "cat data/settings.json"}) is None
    assert guard.permissions.allowance(SHELL, {"command": "cat src/a.py"}) == WHY_READONLY


def test_after_a_cd_somewhere_else_commands_that_follow_that_folders_settings_ask_again(guard, tmp_path):
    """绕过：以前每条命令都在工作目录里运行，cd 别处再做事得写在同一条命令里（cd x && npm test），这一条照常问你。
    记着目录以后，只要你答应过一条看起来什么都不做的 cd，之后在那里不用问的命令就都不问了。有几样命令做什么要看它所在的
    那个文件夹里的设置：你加的\"不用问的命令\"（npm test 跑的是那里 package.json 写的脚本，./scripts/x.sh 是那里的脚本）、
    git（别的仓库的 .git/config 里 core.fsmonitor、gpg.program 能运行任何命令：git -C 就是因为这个不许的）、
    查版本（yarn 照着那里的 .yarnrc 的 yarn-path 运行一个 js 文件，rustup、asdf 这类照着那里的设置换一个程序）。
    它们只在工作目录里才不问；git 在工作目录的子文件夹里、用的还是同一个仓库时照样不问。"""
    commands = CommandList(guard.data / "commands.json")
    commands.add("npm test")
    commands.add("./scripts/status.sh")
    guard.permissions.commands = commands
    (guard.workspace / ".git").mkdir()
    (guard.workspace / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    for command in ("npm test", "./scripts/status.sh"):
        assert allowance(guard, command) == WHY_LISTED, command
    assert allowance(guard, "git status") == WHY_READONLY and allowance(guard, "yarn --version") == WHY_READONLY
    guard.shell.current.moved(str(guard.workspace / "src"))  # 工作目录的子文件夹：还是同一个仓库
    assert allowance(guard, "git log -1") == WHY_READONLY
    # 下载回来的别人的项目（自己带着 .git/）：cd 过去问过你一次
    elsewhere = tmp_path / "downloaded"
    (elsewhere / ".git").mkdir(parents=True)
    (elsewhere / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (elsewhere / ".git" / "config").write_text("[core]\n\tfsmonitor = touch pwned\n", encoding="utf-8")
    (elsewhere / "package.json").write_text('{"scripts": {"test": "touch pwned"}}\n', encoding="utf-8")
    nested = guard.workspace / "vendor" / "lib"  # 工作目录里面另一个仓库（解压出来的、git clone 下来的）
    (nested / ".git").mkdir(parents=True)
    for place in (elsewhere, nested):
        guard.shell.current.moved(str(place))
        for command in ("npm test", "./scripts/status.sh", "git status", "git log -1", "yarn --version", "env git status"):
            assert allowance(guard, command) is None, (place, command)
        assert allowance(guard, "ls") == WHY_READONLY  # 只是看的照样不用问
    guard.shell.current.moved(str(guard.workspace / "src"))
    assert allowance(guard, "npm test") is None  # 你加的命令：只在工作目录里（在子文件夹里 ./scripts/x.sh 也是另一个文件）


def test_a_gone_directory_is_judged_as_the_workspace_it_will_run_in(guard):
    folder = guard.workspace / "gone"
    folder.mkdir()
    guard.shell.current.moved(str(folder))
    shutil.rmtree(folder)
    # 那个目录不在了：命令会在工作目录里运行，所以按工作目录判断（data/settings.json 在那里是数据目录）
    assert allowance(guard, "cat data/settings.json") is None
    assert guard.shell.current.path == guard.workspace


def test_the_card_says_where_the_command_runs(guard):
    question = guard.permissions.review(SHELL, {"command": "ls"}, guard.tool)
    assert question == "执行命令：\n\n    $ ls"  # 在工作目录里：和以前一字不差
    src = guard.workspace / "src"
    guard.shell.current.moved(str(src))
    question = guard.permissions.review(SHELL, {"command": "ls"}, guard.tool)
    assert question == f"执行命令：\n\n    $ ls\n\n在这个目录里运行：{src}（之前的命令 cd 到了这里）"
    # 换语言时里外两层都能重译
    assert question.key == "{question}\n\n在这个目录里运行：{path}（之前的命令 cd 到了这里）"
    assert question.nested["question"].key == "执行命令：\n\n    $ {command}"
    i18n.choose("en")
    try:
        english = guard.permissions.review(SHELL, {"command": "ls"}, guard.tool)
    finally:
        i18n.choose("zh")
    assert english == f"run a command:\n\n    $ ls\n\nRuns in this folder: {src} (an earlier command cd'd here)"


def test_the_card_outside_the_sandbox_still_says_where_and_always_asks(guard, monkeypatch, tmp_path):
    install_fake(monkeypatch)
    layout = Layout(
        workspace=guard.workspace, private=(guard.data,), protected=(), extra=list, default=lambda: "workspace", home=lambda: tmp_path / "home",
    )
    shell = Shell(guard.workspace, Sandbox(layout))
    tool = make_shell_tool(shell)
    shell.current.moved(str(guard.workspace / "src"))
    question = guard.permissions.review(SHELL, {"command": "ls", "unsandboxed": True}, tool)
    assert "在沙箱之外执行命令" in question and "在这个目录里运行：" in question
    assert getattr(question, "always", False)  # 在任何模式下都问：包了一层也照样标着
    sandboxed = guard.permissions.review(SHELL, {"command": "ls"}, tool)
    assert "沙箱：只在工作目录里" in sandboxed and "在这个目录里运行：" in sandboxed and not getattr(sandboxed, "always", False)


# ---- 沙箱 -----------------------------------------------------------------------------------


@pytest.fixture
def jail(tmp_path, monkeypatch):
    """一个开着沙箱（"只在工作目录里"，假的后端：只记下来、照原样运行）的 Shell。"""
    fake = install_fake(monkeypatch)
    workspace = tmp_path / "work"
    (workspace / "data").mkdir(parents=True)
    (workspace / "sub").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    level, extra = {"value": "workspace"}, []
    layout = Layout(
        workspace=workspace, private=(workspace / "data",), protected=(workspace / ".env",),
        extra=lambda: extra, default=lambda: level["value"], home=lambda: tmp_path / "home",
    )
    sandbox = Sandbox(layout)
    return SimpleNamespace(fake=fake, workspace=workspace, outside=outside, level=level, extra=extra, sandbox=sandbox, shell=Shell(workspace, sandbox))


def test_what_the_sandbox_reaches(jail):
    sandbox, workspace = jail.sandbox, jail.workspace
    assert sandbox.reaches(workspace) and sandbox.reaches(workspace / "sub")
    assert not sandbox.reaches(jail.outside)  # 你没给的地方
    assert not sandbox.reaches(workspace / "data")  # 永远碰不到的地方
    jail.extra.append({"path": str(jail.outside), "write": False})
    assert sandbox.reaches(jail.outside)  # 只读的额外路径：能从那里开始，只是写不了
    jail.level["value"] = "off"
    assert sandbox.reaches(jail.outside / "nowhere")  # 沙箱关着：哪儿都行


def test_the_sandbox_starts_the_command_in_the_current_directory(jail):
    jail.shell.run(change_dir("sub"))
    jail.shell.run(print_dir())
    first, second = jail.fake.calls
    assert first[0].start == real(jail.workspace) and second[0].start == real(jail.workspace / "sub")
    assert second[0].write[0] == real(jail.workspace)  # 能写的地方照旧是工作目录


def test_a_directory_the_sandbox_cannot_see_falls_back(jail):
    # 上一条是在沙箱之外运行的（你放行过的那一条），cd 到了沙箱里看不到的地方
    assert jail.shell.run(change_dir(str(jail.outside)), unsandboxed=True) == "（没有输出）"
    assert same(jail.shell.current.path, jail.outside)
    assert same(jail.shell.where(unsandboxed=True), jail.outside)  # 下一条也在沙箱之外：照样从那里开始
    # 下一条在沙箱里：问你之前先看一眼（permissions.py 就是这样），当场回到工作目录，卡片上、运行时都是它
    assert jail.shell.where() == jail.workspace
    out = jail.shell.run(print_dir())
    first, note = out.split("\n", 1)
    assert same(first, jail.workspace) and "命令只能从工作目录和额外允许的路径里开始" in note
    assert jail.fake.calls[-1][0].start == real(jail.workspace)


@pytest.mark.skipif(sys.platform == "win32", reason="bwrap 的参数里用的是 macOS、Linux 的路径写法；Windows 上没有沙箱")
def test_bwrap_changes_into_the_current_directory(tmp_path):
    workspace = tmp_path / "work"
    (workspace / "sub").mkdir(parents=True)
    scratch = tmp_path / "scratch"
    (scratch / "home").mkdir(parents=True)
    (scratch / "tmp").mkdir()

    def make(cwd: Path | None):
        return build(
            level="workspace", workspace=workspace, extra=[], private=[], protected=[], home=scratch / "home", temp=scratch / "tmp",
            real_home=tmp_path / "home", cwd=cwd,
        )

    assert linux.arguments(make(None))[-2:] == ["--chdir", str(real(workspace))]
    assert linux.arguments(make(workspace / "sub"))[-2:] == ["--chdir", str(real(workspace / "sub"))]


# ---- 一段对话里 -------------------------------------------------------------------------------


def test_a_new_conversation_and_a_reopened_one_start_in_the_workspace(serve):  # noqa: F811
    s = serve(
        cmd("t1", change_dir("sub")), reply(text("进去了。")),
        cmd("t2", print_dir()), reply(text("在工作目录。")),
        cmd("t3", print_dir()), reply(text("又回到了工作目录。")),
    )
    (s.config.workspace / "sub").mkdir()
    set_approval(s, "all")
    conversation_id = say(s, "进 sub")["conversation"]
    assert same(s.app.shell.current.path, s.config.workspace / "sub")

    act(s, "/api/new")
    say(s, "现在在哪")  # 新对话：从工作目录开始，什么都不用说
    assert same(result_of(s.api, "t2"), s.config.workspace) and "注意" not in result_of(s.api, "t2")

    open_conversation(s, conversation_id)  # 重新打开：也从工作目录开始，不恢复当时停在哪儿
    assert s.app.shell.current.path == s.config.workspace
    say(s, "现在在哪")
    first, note = result_of(s.api, "t3").split("\n", 1)
    assert same(first, s.config.workspace) and "重新打开" in note and "不再算数" in note


@pytest.fixture
def bot(make_config, monkeypatch):
    yield from rigs(make_config, monkeypatch)


def test_a_chat_channel_keeps_the_folder_between_turns_and_starts_over_after_a_restart(bot):
    s = bot(cmd("t1", change_dir("sub")), reply(text("进去了。")), cmd("t2", print_dir()), reply(text("在 sub 里。")), approval="all")
    (s.config.workspace / "sub").mkdir()
    s.wait_reply(s.say("进 sub"), "进去了")
    s.idle()
    s.wait_reply(s.say("现在在哪"), "在 sub 里")  # 同一个聊天的下一轮：接着用同一个 shell
    s.idle()
    assert same(result_of(s.api, "t2"), s.config.workspace / "sub")
    s.close()
    again = bot(cmd("t3", print_dir()), reply(text("回到了工作目录。")), approval="all")  # 同一个数据目录：重启
    again.wait_reply(again.say("现在呢"), "回到了工作目录")
    again.idle()
    first, note = result_of(again.api, "t3").split("\n", 1)
    assert same(first, s.config.workspace) and "重新打开" in note  # 接着一段旧会话：从工作目录开始，并说一声


def test_a_subagent_starts_in_the_workspace_and_does_not_move_its_caller(start):
    h = start(
        cmd("t0", change_dir("sub")),
        test_agents.delegate("代码审查员", task="看看你在哪", id="t1"),
        cmd("s1", f"{change_dir('..')}; {print_dir()}"),
        reply(text("我在工作目录的上一层。")),
        cmd("t2", print_dir()),
        reply(text("好")),
    )
    (h.config.workspace / "sub").mkdir()
    test_agents.install(h, "reviewer", tools="Bash")
    h.agent.run_turn("进 sub，再交给代码审查员")
    assert same(result_of(h.api, "s1"), h.config.workspace.parent)  # 子 Agent 从工作目录开始（不是 sub）
    assert same(result_of(h.api, "t2"), h.config.workspace / "sub")  # 它的 cd 不影响叫它的那一方


def test_hooks_are_told_where_the_command_runs(start):
    h = start(cmd("t1", change_dir("sub")), cmd("t2", print_dir()), reply(text("好")))
    (h.config.workspace / "sub").mkdir()
    folder = h.config.workspace.parent / "hooks"
    pre, pre_log = script(folder, "pre")
    post, post_log = script(folder, "post")
    add(h, "pre", "PreToolUse", pre, matcher="shell")
    add(h, "post", "PostToolUse", post, matcher="shell")
    h.agent.run_turn("进 sub 再看看")
    before = [call["payload"] for call in calls_of(pre_log)]
    after = [call["payload"] for call in calls_of(post_log)]
    assert [same(payload["command_cwd"], where) for payload, where in zip(before, (h.config.workspace, h.config.workspace / "sub"))] == [True, True]
    assert [same(payload["command_cwd"], where) for payload, where in zip(after, (h.config.workspace, h.config.workspace / "sub"))] == [True, True]
    assert all(payload["cwd"] == str(h.config.workspace) for payload in before + after)  # cwd 照旧是工作目录（钩子在那里运行）


def test_a_hook_test_run_has_command_cwd_too(tmp_path):
    # 页面上"试运行"用的示例和真的调用长得一样（见 hooks/engine.py 的 sample）
    hooks = Hooks(HookConfig(tmp_path / "hooks.json"), tmp_path)
    assert hooks.sample({"event": "PreToolUse", "matcher": "shell"})["command_cwd"] == str(tmp_path)
    assert "command_cwd" not in hooks.sample({"event": "PreToolUse", "matcher": "todo"})


def test_regenerating_on_the_page_starts_where_that_turn_started(rig):
    s = rig(
        cmd("t1", change_dir("sub")), reply(text("进去了。")),
        cmd("t2", change_dir("inner")), reply(text("又进了一层。")),
        cmd("t3", print_dir()), reply(text("在 sub 里。")),
    )
    (s.config.workspace / "sub" / "inner").mkdir(parents=True)
    set_approval(s, "all")
    say(s, "进 sub")
    say(s, "再进一层")
    assert same(s.app.shell.current.path, s.config.workspace / "sub" / "inner")
    mark = test_rewind_web.rewind_state(s)["mark"]
    _, live = test_rewind_web.turn(s, "retry", mark, confirmed=True)  # 这一轮执行过命令：倒回去之前确认过
    assert test_rewind_web.texts(live) == ["在 sub 里。"]
    assert same(result_of(s.api, "t3"), s.config.workspace / "sub")  # 和原来那一轮一样从 sub 开始，不是 inner


def test_retrying_in_the_terminal_starts_where_that_turn_started(terminal, make_config):
    workspace = make_config().workspace
    (workspace / "sub" / "inner").mkdir(parents=True)
    api, out, _ = terminal(
        ["/auto all", "进 sub", "再进一层", "/retry"],
        cmd("t1", change_dir("sub")), reply(text("进去了。")),
        cmd("t2", change_dir("inner")), reply(text("又进了一层。")),
        cmd("t3", print_dir()), reply(text("在 sub 里。")),
        interactive=True,  # 这一轮执行过命令：/retry 先问一次，替你答 y
    )
    assert "在 sub 里。" in out
    assert same(result_of(api, "t3"), workspace / "sub")


def test_the_marker_never_reaches_the_model_or_the_page(serve):  # noqa: F811
    s = serve(cmd("t1", f"{change_dir('sub')}; {print_dir()}"), reply(text("好")))
    (s.config.workspace / "sub").mkdir()
    set_approval(s, "all")
    say(s, "进去看看")
    assert cwd_module._PREFIX not in str(s.api.requests) and cwd_module._PREFIX not in str(s.ui.events_after(0, timeout=0))
    assert same(result_of(s.api, "t1"), s.config.workspace / "sub")
