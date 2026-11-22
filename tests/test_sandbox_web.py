"""命令的沙箱接进智狐以后的样子：网页上的接口和状态、对话里一条命令怎么走、定时任务和聊天通道各自的那一档、
子 Agent 用谁的沙箱、"智狐能做的事"怎么说、命令行。

沙箱本身（每个系统的配置怎么写、真的挡不挡得住）在 test_sandbox.py、test_sandbox_macos.py、test_sandbox_linux.py。
这里一律用假的后端（tests/fake_sandbox.py）：不建真的沙箱，只记下"这条命令本该在沙箱里运行"，命令照原样运行。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest
import test_agents
from conftest import RecordingUI
from fake_api import reply, text, tool_use
from fake_sandbox import install as install_fake
from fake_telegram import TOKEN
from test_channels import rigs
from test_webui import latest_state, serve  # noqa: F401  serve 是 fixture

from harness import capabilities, cli
from harness.agents import runner as runner_module
from harness.channels.store import ChannelStore
from harness.settings import Settings
from harness.tools import tasks as task_tools
from harness.tools.shell import TOOL_NAME as SHELL
from harness.tools.shell import Shell, make_shell_tool
from harness.webui import sandbox_api

DAILY = {"type": "daily", "time": "09:00"}

# Windows 上的命令是 PowerShell（没有 touch、>&2 这些写法），而且 Windows 上本来就没有沙箱（见 harness/sandbox/windows.py）
BASH_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="这一项用 bash 的写法运行命令；Windows 上的命令是 PowerShell，沙箱在 Windows 上也用不了")



def until(check, what: str = "", timeout: float = 5):
    deadline = time.monotonic() + timeout
    while True:
        found = check()
        if found:
            return found
        assert time.monotonic() < deadline, f"没有等到：{what}"
        time.sleep(0.01)


def sandbox_state(s) -> dict:
    return latest_state(s.browser)["sandbox"]


def post(s, action: str, **body) -> tuple[int, dict]:
    return s.browser.post(f"/api/sandbox/{action}", body)


def saved_sandbox(config) -> dict:
    return json.loads(config.settings_file.read_text(encoding="utf-8")).get("sandbox", {})


def command(id: str, cmd: str, **extra) -> dict:
    return reply(tool_use(id, SHELL, {"command": cmd, **extra}), stop_reason="tool_use")


def shell_tool(agent):
    return agent.tools.get(SHELL)


# ---- 状态 -----------------------------------------------------------------------


def test_the_state_describes_the_sandbox(serve):  # noqa: F811
    s = serve()
    state = sandbox_state(s)
    assert state["level"] == "off" and state["paths"] == []
    assert [level["id"] for level in state["levels"]] == ["off", "workspace", "network"]
    workspace = state["levels"][1]
    # 名字和说明是程序写好的、带着原文：页面换语言时就地重译
    assert workspace["label"] == {"text": "只在工作目录里", "key": "只在工作目录里", "values": {}}
    assert workspace["text"]["key"].startswith("能读写工作目录") and workspace["text"]["text"] == workspace["text"]["key"]
    assert state["limit"] == sandbox_api.SANDBOX_MAX_PATHS and state["chars"] > 0
    assert state["backend"]["id"] == "fake" and state["backend"]["label"]["text"] == "假沙箱" and state["backend"]["note"] is None
    assert state["problem"] is None


def test_the_state_says_when_this_machine_cannot_sandbox(serve, monkeypatch):  # noqa: F811
    install_fake(monkeypatch, problem="这台电脑上没有装 bubblewrap（bwrap）。")
    s = serve()
    assert sandbox_state(s)["problem"] == {"text": "这台电脑上没有装 bubblewrap（bwrap）。", "key": "这台电脑上没有装 bubblewrap（bwrap）。", "values": {}}


# ---- /api/sandbox/level -----------------------------------------------------------


@pytest.mark.parametrize("level", ["workspace", "network", "off"])
def test_choosing_a_level_is_saved(serve, level):  # noqa: F811
    s = serve()
    if level == "off":
        assert post(s, "level", level="workspace") == (200, {"ok": True})
    assert post(s, "level", level=level) == (200, {"ok": True})
    until(lambda: sandbox_state(s)["level"] == level, what="状态里的那一档")
    assert s.app.settings.sandbox["level"] == level
    assert saved_sandbox(s.config).get("level", "off") == level


@pytest.mark.parametrize("bad", ["yolo", "default", "", None, 1, True], ids=["yolo", "default", "empty", "none", "int", "bool"])
def test_a_bad_level_is_rejected(serve, bad):  # noqa: F811
    s = serve()
    status, body = post(s, "level", level=bad)
    assert status == 400 and body["error"] == "参数不正确"
    assert s.app.settings.sandbox["level"] == "off"


def test_unknown_sandbox_actions_are_not_found(serve):  # noqa: F811
    s = serve()
    assert post(s, "nope")[0] == 404


# ---- /api/sandbox/add、edit、delete、reveal ----------------------------------------


def paths_of(s) -> list[tuple[str, bool]]:
    return [(item["path"], item["write"]) for item in s.app.settings.sandbox["paths"]]


def test_adding_read_only_and_read_write_paths(serve, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home" / ".nvm").mkdir(parents=True)
    tools = tmp_path / "tools"
    tools.mkdir()
    s = serve()
    assert post(s, "add", path=f"  {tools}  ") == (200, {"ok": True, "path": str(tools)})  # 前后的空白去掉，默认只读
    assert post(s, "add", path="~/.nvm/", write=True) == (200, {"ok": True, "path": str(tmp_path / "home" / ".nvm")})  # ~ 展开、末尾的 / 去掉
    missing = tmp_path / "later"
    assert post(s, "add", path=str(missing))[0] == 200  # 还不存在的也能加（以后才建）
    assert paths_of(s) == [(str(tools), False), (str(tmp_path / "home" / ".nvm"), True), (str(missing), False)]
    assert saved_sandbox(s.config)["paths"] == [
        {"path": str(tools), "write": False},
        {"path": str(tmp_path / "home" / ".nvm"), "write": True},
        {"path": str(missing), "write": False},
    ]
    shown = until(lambda: (paths := sandbox_state(s)["paths"]) and len(paths) == 3 and paths, what="状态里的路径")
    assert [item["exists"] for item in shown] == [True, True, False]


@pytest.mark.parametrize(
    "value, message",
    [
        ("", "写一个路径"),
        ("   ", "写一个路径"),
        ("relative/dir", "要写完整的路径"),
        ("./here", "要写完整的路径"),
        ("/a\nb", "要写完整的路径"),
        ("/" + "x" * 1001, "要写完整的路径"),
        (None, "写一个路径"),
        (7, "写一个路径"),
    ],
    ids=["empty", "blank", "relative", "dot", "newline", "too-long", "none", "int"],
)
def test_bad_paths_are_rejected(serve, value, message):  # noqa: F811
    s = serve()
    status, body = post(s, "add", path=value)
    assert status == 400 and message in body["error"]
    assert paths_of(s) == []


def test_the_data_folder_and_the_key_file_can_never_be_added(serve):  # noqa: F811
    s = serve()
    s.config.data_dir.mkdir(parents=True, exist_ok=True)
    for path in (s.config.data_dir, s.config.data_dir / "conversations", s.config.env_file, s.config.mcp_file):
        status, body = post(s, "add", path=str(path), write=True)
        assert status == 400 and "永远碰不到" in body["error"], path
    assert paths_of(s) == []


def test_a_write_flag_that_is_not_a_boolean_is_rejected(serve, tmp_path):  # noqa: F811
    s = serve()
    assert post(s, "add", path=str(tmp_path), write="yes")[0] == 400


def test_duplicates_and_the_limit(serve, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(sandbox_api, "SANDBOX_MAX_PATHS", 2)
    s = serve()
    assert post(s, "add", path=str(tmp_path / "a"))[0] == 200
    status, body = post(s, "add", path=str(tmp_path / "a"), write=True)
    assert status == 409 and "已经有" in body["error"]
    assert post(s, "add", path=str(tmp_path / "b"))[0] == 200
    status, body = post(s, "add", path=str(tmp_path / "c"))
    assert status == 409 and "最多 2 条" in body["error"]
    assert paths_of(s) == [(str(tmp_path / "a"), False), (str(tmp_path / "b"), False)]


def test_editing_keeps_the_position_and_deleting_removes_one(serve, tmp_path):  # noqa: F811
    s = serve()
    a, b, c = (str(tmp_path / name) for name in "abc")
    for path in (a, b, c):
        assert post(s, "add", path=path)[0] == 200
    assert post(s, "edit", old=b, path=str(tmp_path / "b2"), write=True) == (200, {"ok": True, "path": str(tmp_path / "b2")})
    assert paths_of(s) == [(a, False), (str(tmp_path / "b2"), True), (c, False)]
    assert post(s, "edit", old=a, path=a, write=True)[0] == 200  # 只改能不能写
    assert paths_of(s)[0] == (a, True)
    status, body = post(s, "edit", old=a, path=c)  # 改成列表里已有的另一条
    assert status == 409 and "已经有" in body["error"]
    status, body = post(s, "edit", old=str(tmp_path / "gone"), path=str(tmp_path / "x"))
    assert status == 409 and "已经不在列表里" in body["error"]
    status, body = post(s, "edit", old=a, path=str(s.config.env_file))
    assert status == 400 and "永远碰不到" in body["error"]
    assert post(s, "delete", path=a) == (200, {"ok": True, "path": a})
    status, body = post(s, "delete", path=a)
    assert status == 409 and "已经不在列表里" in body["error"]
    assert paths_of(s) == [(str(tmp_path / "b2"), True), (c, False)]
    assert saved_sandbox(s.config)["paths"] == [{"path": str(tmp_path / "b2"), "write": True}, {"path": c, "write": False}]


def test_reveal_only_shows_paths_from_the_list(serve, tmp_path):  # noqa: F811
    s = serve()
    here = tmp_path / "here"
    here.mkdir()
    deep = tmp_path / "not" / "yet" / "there"
    assert post(s, "add", path=str(here))[0] == 200
    assert post(s, "add", path=str(deep))[0] == 200
    assert post(s, "reveal", path=str(here)) == (200, {"ok": True, "path": str(here)})
    # 还不存在的：打开离它最近的、存在的那一层
    assert post(s, "reveal", path=str(deep)) == (200, {"ok": True, "path": str(tmp_path)})
    assert s.revealed == [here, tmp_path]
    for bad in (str(tmp_path), "/etc", None, ["x"]):
        status, body = post(s, "reveal", path=bad)
        assert status == 409 and "已经不在列表里" in body["error"], bad
    assert s.revealed == [here, tmp_path]  # 不在列表里的一个都没打开


# ---- 聊天通道和定时任务自己的那一档 ---------------------------------------------------


@pytest.fixture
def bot(make_config, monkeypatch):
    yield from rigs(make_config, monkeypatch)


def test_choosing_a_channels_sandbox_from_the_page(bot):
    s = bot()
    assert s.platform()["sandbox"] == "default"
    for bad in ("yolo", "", None):
        assert s.browser.post("/api/channel/sandbox", {"platform": "telegram", "level": bad})[0] == 400
    for level in ("off", "workspace", "network", "default"):
        assert s.browser.post("/api/channel/sandbox", {"platform": "telegram", "level": level}) == (200, {"ok": True})
        until(lambda level=level: s.platform()["sandbox"] == level, what=f"页面上的沙箱是 {level}")
        assert ChannelStore(s.config.channels_file).get("telegram")["sandbox"] == level
    assert s.app.settings.sandbox["level"] == "off"  # 设置里的那一档不跟着变


def test_a_channel_that_is_not_connected_cannot_choose_a_sandbox(bot):
    s = bot(connected=False)
    status, body = s.browser.post("/api/channel/sandbox", {"platform": "telegram", "level": "workspace"})
    assert status == 409 and "还没有连接 Telegram" in body["error"]


def save_task(s, **fields) -> tuple[int, dict]:
    body = {"title": "看日志", "kind": "agent", "prompt": "看一眼日志", "schedule": DAILY, "enabled": True, "approval": "ask", **fields}
    return s.browser.post("/api/task/save", body)


def test_a_task_remembers_its_own_sandbox(serve):  # noqa: F811
    s = serve()
    status, body = save_task(s, sandbox="workspace")
    assert status == 200
    task_id = body["id"]
    assert s.app.tasks.get(task_id)["sandbox"] == "workspace"
    until(lambda: any(task["sandbox"] == "workspace" for task in latest_state(s.browser)["tasks"]), what="状态里的任务")
    assert save_task(s, id=task_id, sandbox="off")[0] == 200
    assert s.app.tasks.get(task_id)["sandbox"] == "off"
    assert save_task(s, id=task_id)[0] == 200  # 不带：回到跟随设置
    assert s.app.tasks.get(task_id)["sandbox"] == "default"
    for bad in ("yolo", 1, None):
        status, _ = save_task(s, id=task_id, sandbox=bad)
        assert status == 400, bad
    # 只是提醒的任务不运行命令：存成 default
    status, body = save_task(s, kind="reminder", prompt="喝水", sandbox="off")
    assert status == 200 and s.app.tasks.get(body["id"])["sandbox"] == "default"


def test_tasks_the_model_adds_always_follow_the_settings(make_config):
    from harness.tasks import TaskStore

    store = TaskStore(make_config().tasks_file)
    add = next(tool for tool in task_tools.make_task_tools(store) if tool.name == task_tools.ADD)
    add.run({"kind": "agent", "title": "每天看日志", "prompt": "看日志", "schedule": DAILY})
    (task,) = store.snapshot()[0]
    assert task["sandbox"] == "default"
    # 工具根本没有这个参数：想自己关掉沙箱的，连调用都通不过
    assert "sandbox" not in add.spec["input_schema"]["properties"]
    with pytest.raises(Exception, match="sandbox"):
        add.run({"kind": "agent", "title": "x", "prompt": "y", "schedule": DAILY, "sandbox": "off"})


# ---- 对话里一条命令怎么走 ------------------------------------------------------------


def test_the_card_shows_the_sandbox_and_the_command_runs_inside(start, monkeypatch):
    fake = install_fake(monkeypatch)
    h = start(command("t1", "echo hi"), reply(text("好了")))
    h.app.settings.set_sandbox({"level": "workspace"})
    h.agent.run_turn("跑一下")
    (question,) = h.ui.confirmations
    assert "$ echo hi" in question and "沙箱：只在工作目录里（读写工作目录，不能联网）" in question
    assert h.ui.results[0] == ("hi", False)
    ((policy, argv),) = fake.calls
    assert argv == ["/bin/bash", "-c", "echo hi"] and policy.level == "workspace" and not policy.network
    real = lambda path: Path(os.path.realpath(path))
    assert real(h.config.data_dir) in policy.deny and real(h.config.env_file) in policy.deny
    assert real(h.config.workspace) == policy.workspace and real(h.config.workspace) in policy.write


def test_with_the_sandbox_off_the_card_is_unchanged(start, monkeypatch):
    fake = install_fake(monkeypatch)
    h = start(command("t1", "echo hi"), reply(text("好了")))
    h.agent.run_turn("跑一下")
    assert h.ui.confirmations == ["执行命令：\n\n    $ echo hi"]
    assert fake.calls == []


def test_no_silent_fallback_when_this_machine_cannot_sandbox(start, monkeypatch):
    fake = install_fake(monkeypatch, problem="这台电脑上没有装 bubblewrap（bwrap）。")
    h = start(command("t1", "touch marker"), reply(text("沙箱用不了")))
    h.app.settings.set_sandbox({"level": "workspace"})
    h.agent.run_turn("建个文件")
    assert h.ui.confirmations == []  # 明知不会运行：不弹卡片
    ((result, is_error),) = h.ui.results
    assert is_error and "命令没有执行" in result and "不会悄悄" in result and "没有装 bubblewrap" in result
    assert "unsandboxed" in result  # 你正看着：可以请你放行这一条
    assert not (h.config.workspace / "marker").exists() and fake.calls == []


@BASH_ONLY
def test_running_one_command_outside_always_asks_even_with_everything_allowed(start, monkeypatch):
    fake = install_fake(monkeypatch)
    h = start(command("t1", "touch outside-marker", unsandboxed=True), reply(text("好了")))
    h.app.settings.set_sandbox({"level": "workspace"})
    h.agent.permissions.mode = "all"
    tool = shell_tool(h.agent)
    call = {"command": "touch outside-marker", "unsandboxed": True}
    assert h.agent.permissions.allowance(SHELL, call, tool) is None
    h.agent.run_turn("在沙箱外面建个文件")
    (question,) = h.ui.confirmations
    assert question.startswith("在沙箱之外执行命令（每次都会问）") and "$ touch outside-marker" in question and "只在工作目录里" in question
    assert getattr(question, "always", False) is True  # 卡片上不给"不想每次都问？"
    assert h.ui.auto_allowed_count == 0
    assert (h.config.workspace / "outside-marker").exists() and fake.calls == []  # 放行了：没有进沙箱


def test_running_outside_is_asked_in_the_readonly_mode_too(start, monkeypatch):
    install_fake(monkeypatch)
    h = start(command("t1", "ls", unsandboxed=True), command("t2", "ls"), reply(text("看过了")))
    h.app.settings.set_sandbox({"level": "network"})
    h.agent.permissions.mode = "readonly"
    h.agent.run_turn("看一眼")
    assert len(h.ui.confirmations) == 1 and "在沙箱之外执行命令" in h.ui.confirmations[0]  # 只读的命令，出沙箱也要问
    assert h.ui.auto_whys == ["readonly"]  # 第二条在沙箱里的 ls 照常不问


def test_unsandboxed_means_nothing_when_the_sandbox_is_off(start, monkeypatch):
    fake = install_fake(monkeypatch)
    h = start(command("t1", "echo hi", unsandboxed=True), reply(text("好了")))
    h.agent.permissions.mode = "all"
    h.agent.run_turn("跑一下")
    assert h.ui.confirmations == [] and h.ui.auto_allowed_count == 1  # 和一条普通的命令一样
    assert h.ui.results[0] == ("hi", False) and fake.calls == []


@BASH_ONLY
def test_a_blocked_command_is_explained_to_the_model(start, monkeypatch):
    install_fake(monkeypatch)
    h = start(command("t1", "echo 'cat: x: Operation not permitted' >&2; exit 1"), reply(text("被挡住了")))
    h.app.settings.set_sandbox({"level": "workspace"})
    h.agent.permissions.mode = "all"
    h.agent.run_turn("读一下")
    ((result, is_error),) = h.ui.results
    assert not is_error  # 命令运行了、只是失败了：照常是结果，不是"出错"
    assert "[退出码 1]" in result and "这条命令是在沙箱里运行的" in result and "unsandboxed" in result


# ---- 没人看着的运行：定时任务、聊天通道 -------------------------------------------------


def test_a_task_with_everything_allowed_cannot_leave_the_sandbox(start, monkeypatch):
    fake = install_fake(monkeypatch)
    h = start(command("t1", "touch outside-marker", unsandboxed=True), reply(text("做不到")))
    ui = RecordingUI()
    run = h.app.unattended_agent(ui, "all", "workspace")
    run.agent.run_turn("在沙箱外面建个文件")
    assert ui.confirmations == []  # 不问：直接拒绝
    ((result, is_error),) = ui.results
    assert is_error and "不能在沙箱之外运行命令" in result and "全部自动允许" in result
    assert not (h.config.workspace / "outside-marker").exists() and fake.calls == []
    # 同一个任务里，在沙箱里的命令照样不问（"全部自动允许"）
    assert shell_tool(run.agent).check({"command": "ls"}) is None


def test_a_task_that_asks_can_ask_to_leave_the_sandbox(start, monkeypatch):
    install_fake(monkeypatch)
    h = start(command("t1", "touch outside-marker", unsandboxed=True), reply(text("好了")))
    ui = RecordingUI()
    run = h.app.unattended_agent(ui, "ask", "workspace")
    run.agent.run_turn("在沙箱外面建个文件")
    (question,) = ui.confirmations
    assert question.startswith("在沙箱之外执行命令") and "这个定时任务平时用的是“只在工作目录里”" in question
    assert (h.config.workspace / "outside-marker").exists()


def test_a_task_that_turned_its_sandbox_off_says_so_on_the_card(start, monkeypatch):
    fake = install_fake(monkeypatch)
    h = start(command("t1", "echo hi"), reply(text("好了")))
    h.app.settings.set_sandbox({"level": "workspace"})
    ui = RecordingUI()
    run = h.app.unattended_agent(ui, "ask", "off")
    run.agent.run_turn("跑一下")
    (question,) = ui.confirmations
    assert "沙箱：关（这个定时任务没有用沙箱" in question
    assert fake.calls == []


def test_a_task_following_the_settings_uses_the_settings_level(start, monkeypatch):
    fake = install_fake(monkeypatch)
    h = start(command("t1", "echo hi"), reply(text("好了")))
    h.app.settings.set_sandbox({"level": "network"})
    ui = RecordingUI()
    h.app.unattended_agent(ui, "all").agent.run_turn("跑一下")
    assert ui.confirmations == [] and [policy.level for policy, _ in fake.calls] == ["network"]


def test_a_channel_reads_its_sandbox_before_every_command(start):
    h = start()
    level = ["off"]
    run = h.app.channel_agent(RecordingUI(), "ask", "anthropic", "claude-opus-5-5", sandbox=lambda: level[0])
    sandbox = shell_tool(run.agent).shell.sandbox
    assert sandbox is not h.app.shell.sandbox and sandbox.scope == "channel"
    assert sandbox.facts().level == "off"
    level[0] = "workspace"
    assert sandbox.facts().level == "workspace"
    level[0] = "default"  # 跟随设置
    assert sandbox.facts().level == "off"
    h.app.settings.set_sandbox({"level": "network"})
    assert sandbox.facts().level == "network"
    # 能不能请人放行看这一刻的那一档（聊天通道每一轮会按页面上改过的重新设）
    assert sandbox.facts(True).escape is True
    run.agent.permissions.mode = "all"
    assert sandbox.facts(True).escape is False


# ---- 子 Agent 用叫它的那一方的沙箱 ---------------------------------------------------------


def test_a_subagent_uses_the_callers_sandbox(start, monkeypatch):
    fake = install_fake(monkeypatch)
    shells: list[Shell] = []

    class Recorded(Shell):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            shells.append(self)

    monkeypatch.setattr(runner_module, "Shell", Recorded)
    h = start(
        test_agents.delegate("代码审查员"),
        command("s1", "echo sub"),
        reply(text("跑完了。")),
        reply(text("好")),
    )
    test_agents.install(h, "reviewer", tools="Bash")
    h.app.settings.set_sandbox({"level": "workspace"})
    h.agent.run_turn("交给代码审查员")
    (own,) = shells
    assert own is not h.app.shell and own.sandbox is h.app.shell.sandbox  # 自己的 shell，同一个沙箱
    assert "沙箱：只在工作目录里" in h.ui.confirmations[0] and h.ui.askers == ["代码审查员"]
    assert [argv for _, argv in fake.calls] == [["/bin/bash", "-c", "echo sub"]]


def test_a_subagent_of_a_caller_without_commands_has_no_sandbox(start):
    from harness.tools import ToolRegistry

    h = start()
    parent = h.agent
    assert runner_module._sandbox_of(parent) is h.app.shell.sandbox
    parent.use_tools(ToolRegistry([tool for tool in parent.tools if tool.name != SHELL]))
    assert runner_module._sandbox_of(parent) is None


# ---- 智狐能做的事 ------------------------------------------------------------------------


def shell_item(app, **options) -> capabilities.Capability:
    return next(item for item in capabilities.overview(app, **options).items if item.id == "shell")


def escape_outcome(entry: capabilities.Capability) -> str | None:
    found = [rule.outcome for rule in entry.approval.rules if str(rule.text) == "在沙箱之外运行一条命令"]
    return found[0] if found else None


def test_the_capability_list_says_where_commands_run(start, monkeypatch):
    h = start()
    entry = shell_item(h.app)
    assert entry.reason == "" and escape_outcome(entry) is None  # 沙箱关着：和以前一样
    assert ("settings", "sandbox") in [(action.kind, action.target) for action in entry.actions]
    h.app.settings.set_sandbox({"level": "workspace"})
    entry = shell_item(h.app)
    assert entry.reason == "命令在沙箱里运行：只在工作目录里"
    assert escape_outcome(entry) == "always"  # 你正看着：哪一档都先问你
    h.agent.permissions.mode = "all"
    assert escape_outcome(shell_item(h.app)) == "always"
    install_fake(monkeypatch, problem="没装 bwrap。")
    assert "命令会被拒绝执行" in shell_item(h.app).reason and "没装 bwrap。" in shell_item(h.app).reason


def test_the_capability_list_of_an_unattended_run(start):
    h = start()
    h.app.settings.set_sandbox({"level": "workspace"})
    everything = h.app.unattended_agent(RecordingUI(), "all", "default")
    assert escape_outcome(shell_item(h.app, run=everything)) == "never"  # 没人会来回答：根本不许
    asking = h.app.unattended_agent(RecordingUI(), "ask", "default")
    assert escape_outcome(shell_item(h.app, run=asking)) == "always"
    off = h.app.unattended_agent(RecordingUI(), "ask", "off")
    entry = shell_item(h.app, run=off)
    assert entry.reason == "这次运行没有用沙箱" and escape_outcome(entry) is None


# ---- 说明书在一段对话里不变 ----------------------------------------------------------------


def test_the_tool_spec_has_the_parameter_and_never_changes(start, monkeypatch):
    install_fake(monkeypatch)
    h = start(reply(text("一")), reply(text("二")), reply(text("三")))
    spec = shell_tool(h.agent).spec
    assert spec["input_schema"]["properties"]["unsandboxed"]["type"] == "boolean"
    assert "unsandboxed" not in spec["input_schema"]["required"]
    assert spec == make_shell_tool(Shell(h.config.workspace)).spec  # 有没有沙箱、哪一档，说明书都一样
    h.agent.run_turn("一")
    h.app.settings.set_sandbox({"level": "workspace"})
    h.agent.run_turn("二")
    h.app.settings.set_sandbox({"level": "off"})
    h.agent.run_turn("三")
    first, second, third = h.api.requests
    assert first["tools"] == second["tools"] == third["tools"]


# ---- 命令行 ----------------------------------------------------------------------------


@pytest.fixture
def harness_cli(tmp_path, monkeypatch, capsys):
    data = tmp_path / "cli-data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(data))

    def run(*argv: str) -> tuple[int, str, str]:
        capsys.readouterr()
        code = cli.main(list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    run.data = data  # type: ignore[attr-defined]
    return run


def cli_settings(data: Path) -> dict:
    return Settings(data / "settings.json", "anthropic", "claude-opus-5-5").sandbox


def test_harness_sandbox_on_the_command_line(harness_cli, tmp_path):
    code, out, _ = harness_cli("sandbox")
    assert code == 0 and "这台电脑上的沙箱：假沙箱" in out and "能用。" in out and "设置里选的：关" in out and "还没有" in out
    code, out, _ = harness_cli("sandbox", "level", "workspace")
    assert code == 0 and "沙箱改成了：只在工作目录里" in out
    assert cli_settings(harness_cli.data)["level"] == "workspace"

    tools = tmp_path / "tools"
    code, out, _ = harness_cli("sandbox", "allow", str(tools))
    assert code == 0 and "加好了" in out and "只读" in out
    code, out, _ = harness_cli("sandbox", "allow", str(tmp_path / "rw"), "--write")
    assert code == 0 and "可读写" in out
    code, out, _ = harness_cli("sandbox", "allow", str(tools), "--write")  # 已经有的：换成新的写法
    assert code == 0
    assert cli_settings(harness_cli.data)["paths"] == [{"path": str(tmp_path / "rw"), "write": True}, {"path": str(tools), "write": True}]
    code, _, err = harness_cli("sandbox", "allow", "relative/path")
    assert code == 1 and "要写完整的路径" in err
    code, _, err = harness_cli("sandbox", "allow", str(harness_cli.data / "conversations"))
    assert code == 1 and "永远碰不到" in err

    code, out, _ = harness_cli("sandbox", "status")
    assert code == 0 and "设置里选的：只在工作目录里" in out and f"可读写  {tools}" in out

    code, out, _ = harness_cli("sandbox", "forget", str(tools))
    assert code == 0 and "删掉了" in out
    code, _, err = harness_cli("sandbox", "forget", str(tools))
    assert code == 1 and "列表里没有" in err
    assert cli_settings(harness_cli.data)["paths"] == [{"path": str(tmp_path / "rw"), "write": True}]


def test_harness_sandbox_warns_when_a_level_cannot_run_here(harness_cli, monkeypatch):
    install_fake(monkeypatch, problem="没装 bwrap。")
    code, out, err = harness_cli("sandbox", "level", "network")
    assert code == 0 and "工作目录 + 联网" in out and "命令会被拒绝执行" in err and "没装 bwrap。" in err
    code, out, err = harness_cli("sandbox")
    assert code == 0 and "用不了：没装 bwrap。" in out and "一律拒绝执行" in err


def test_harness_channels_sandbox_on_the_command_line(harness_cli):
    code, _, err = harness_cli("channels", "sandbox", "telegram", "workspace")
    assert code == 1 and "还没有连接 Telegram" in err
    store = ChannelStore(harness_cli.data / "channels.json")
    store.connect("telegram", {"token": TOKEN}, {"id": "1", "username": "zhifox_test_bot", "name": "ZhiFox Test"})
    code, out, _ = harness_cli("channels", "sandbox", "telegram", "workspace")
    assert code == 0 and "沙箱改成了：只在工作目录里" in out
    assert ChannelStore(harness_cli.data / "channels.json").get("telegram")["sandbox"] == "workspace"
    code, out, _ = harness_cli("channels", "sandbox", "telegram", "default")
    assert code == 0 and "跟随设置" in out
    code, out, _ = harness_cli("channels", "status")
    assert code == 0 and "沙箱：跟随设置" in out
    with pytest.raises(SystemExit):
        harness_cli("channels", "sandbox", "telegram", "yolo")
