""""只读的命令不用问"那一档：权限怎么放行、对话里怎么标出来、你自己加的命令清单、网页和命令行上怎么改。

一条命令算不算只读，由 harness/commands/ 判断，那张几百行的表在 test_commands.py。
这里测的是它接进来以后的样子：
  - Permissions：这一档只放开"只是看"的（只读的命令、读工作目录之外的文件、你加的命令），别的照常问；
    .env、数据目录在这一档里照样要问；MCP、浏览器、发文件、操作电脑都不受影响。
  - 对话（终端和网页）：没问就放行的那一步标着"为什么"（readonly、listed）。
  - 你自己加的命令（data/commands.json）：加、改、删、拒绝哪些、文件改了重新读、模型碰不到它。
  - 定时任务和聊天通道的那一档在 test_task_runs.py、test_channels.py 里（和它们各自的别的测试放在一起）。
"""

from __future__ import annotations

import json

import pytest
from fake_api import reply, text, tool_use
from modes import has_mode
from test_webui import latest_state, say, serve, set_approval  # noqa: F401  serve 是 fixture

from harness import cli
from harness.commands.allowlist import CommandList, CommandListError, check_entry
from harness.config import MAX_ALLOWED_COMMAND_CHARS, MAX_ALLOWED_COMMANDS
from harness.permissions import MODES, WHY_LISTED, WHY_READONLY, Permissions
from harness.tools.files import TOOL_NAME as FILE_TOOL
from harness.tools.files import FileEditor
from harness.tools.shell import TOOL_NAME as SHELL

# ---- 权限 ---------------------------------------------------------------------


@pytest.fixture
def place(tmp_path):
    """一个工作目录（里面有 .env、data/、src/），以及按它组装的 Permissions（还是"每次都问"）。"""
    workspace = tmp_path / "work"
    data = workspace / "data"
    (workspace / "src").mkdir(parents=True)
    data.mkdir()
    (workspace / ".env").write_text("ANTHROPIC_API_KEY=sk-fake\n", encoding="utf-8")
    (workspace / "src" / "a.py").write_text("print(1)\n", encoding="utf-8")
    (data / "settings.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("外面\n", encoding="utf-8")
    commands = CommandList(data / "commands.json")
    editor = FileEditor(workspace, data / "backups", protected=[workspace / ".env", data / "commands.json"])
    return Permissions(editor, private=[data], commands=commands)


def shell(command: str) -> tuple[str, dict]:
    return SHELL, {"command": command}


def view(path: str) -> tuple[str, dict]:
    return FILE_TOOL, {"command": "view", "path": path}


def test_the_modes_go_from_strict_to_loose():
    assert list(MODES) == ["ask", "readonly", "edits", "all"]
    assert MODES["readonly"] == "只读的命令不用问"


@pytest.mark.parametrize(
    "call, why",
    [
        (shell("ls -la"), WHY_READONLY),
        (shell("git status"), WHY_READONLY),
        (shell("df -h | sort"), WHY_READONLY),
        (shell("cat src/a.py"), WHY_READONLY),
        (shell("cat .env"), None),  # 密钥文件：哪怕只是 cat
        (shell("ls data"), None),  # 助手自己的数据目录
        (shell("cat ~/.ssh/id_rsa"), None),
        (shell("rm -rf build"), None),
        (shell("ls; rm -rf ~"), None),
        (shell("echo hi > f"), None),
        (shell("npm test"), None),  # 不是只读的，也不在你的清单里
        (view("../outside.txt"), WHY_READONLY),  # 读工作目录之外的文件：只是看
        (view("data/settings.json"), None),  # 数据目录：除了"全部自动允许"，读它一律先问
        (view("~/.ssh/id_rsa"), None),
        ((FILE_TOOL, {"command": "create", "path": "new.txt", "file_text": "x"}), None),  # 改文件：这一档不包括
        (("mcp__notes__search", {"query": "x"}), None),  # MCP 的工具：别人写的程序，看不到它做什么
        (("send_file", {"path": "src/a.py"}), None),  # 往外发文件
    ],
)
def test_readonly_mode_lets_through_only_what_just_looks(place, call, why):
    place.mode = "readonly"
    assert place.allowance(*call) == why, call
    assert place.auto_allows(*call) is (why is not None)
    place.mode = "ask"
    assert place.allowance(*call) is None  # 每次都问：一样都不放


def test_read_only_commands_still_ask_in_the_edits_mode(place):
    # 两档管的是不同的两样东西："改文件不用问"不包括只读的命令（它的说明里写着"执行命令仍然每次问"）
    place.mode = "edits"
    assert place.allowance(*shell("ls -la")) is None
    assert place.allowance(FILE_TOOL, {"command": "create", "path": "new.txt", "file_text": "x"}) == "mode"
    place.mode = "all"
    assert place.allowance(*shell("rm -rf build")) == "mode"


def test_your_own_commands_count_only_in_the_readonly_mode(place):
    place.commands.add("npm test")
    place.mode = "readonly"
    assert place.allowance(*shell("npm test")) == WHY_LISTED
    assert place.allowance(*shell("npm test -- --watch")) == WHY_LISTED  # 后面跟什么参数都算
    assert place.allowance(*shell("npm testing")) is None  # 按整个词比
    assert place.allowance(*shell("npm test .env")) is None  # 碰到密钥照样要问
    assert place.allowance(*shell("npm test && rm -rf ~")) is None  # 拆词这一步就拆不出来
    assert place.allowance(*shell("echo x | npm test")) is None  # 管道里不算
    assert place.allowance(*shell("ls")) == WHY_READONLY  # 内置的照样算，原因写的是"只读"
    for mode in ("ask", "edits"):
        place.mode = mode
        assert place.allowance(*shell("npm test")) is None


def test_the_permission_for_a_task_or_a_channel_keeps_the_list(place):
    place.commands.add("make check")
    run = place.unattended("readonly")
    assert run is not place and run.mode == "readonly" and run.commands is place.commands
    assert run.allowance(*shell("make check")) == WHY_LISTED and run.allowance(*shell("ls")) == WHY_READONLY
    assert place.mode == "ask" and place.allowance(*shell("ls")) is None  # 对话用的那一份不受影响


def test_the_list_file_is_off_limits_for_commands_too(place):
    place.mode = "readonly"
    place.commands.add("npm test")
    assert place.allowance(*shell("cat data/commands.json")) is None
    assert place.allowance(*shell("npm test data/commands.json")) is None


# ---- 对话里 ---------------------------------------------------------------------


def test_a_read_only_command_runs_without_asking_and_is_marked(start):
    h = start(
        reply(tool_use("toolu_1", SHELL, {"command": "ls"}), stop_reason="tool_use"),
        reply(tool_use("toolu_2", SHELL, {"command": "touch made.txt"}), stop_reason="tool_use"),
        reply(text("看完了，文件也建了。")),
    )
    (h.config.workspace / "seen.txt").write_text("x\n", encoding="utf-8")
    h.agent.permissions.mode = "readonly"
    h.agent.run_turn("看看有什么，再建个文件")
    assert h.ui.auto_whys == [WHY_READONLY]  # ls 没问
    assert len(h.ui.confirmations) == 1 and "touch made.txt" in h.ui.confirmations[0]  # touch 照常问
    assert "seen.txt" in h.ui.results[0][0] and (h.config.workspace / "made.txt").exists()


def test_the_terminal_says_why_a_step_was_not_asked(start, capsys):
    from harness.ui import TerminalUI

    ui = TerminalUI()
    h = start(reply(tool_use("toolu_1", SHELL, {"command": "pwd"}), stop_reason="tool_use"), reply(text("好了")), ui=ui)
    cli.handle_command("/auto readonly", h.app, ui)
    assert h.agent.permissions.mode == "readonly" and "只读的命令不用问" in capsys.readouterr().out
    h.agent.run_turn("在哪儿")
    assert "（自动允许：只读的命令）" in capsys.readouterr().out


def test_the_web_page_marks_read_only_steps(serve):  # noqa: F811
    s = serve(
        reply(tool_use("toolu_1", SHELL, {"command": "ls"}), stop_reason="tool_use"),
        reply(tool_use("toolu_2", SHELL, {"command": "npm test"}), stop_reason="tool_use"),
        reply(text("都看过了。")),
    )
    assert set_approval(s, "readonly")["approval"] == "readonly"
    assert s.browser.post("/api/commands/add", {"command": "npm test"})[0] == 200
    say(s, "看一眼，再跑测试")
    assert [event["why"] for event in s.browser.of_type("auto_allowed")] == [WHY_READONLY, WHY_LISTED]
    assert s.browser.of_type("confirm") == []


# ---- 你自己加的命令 -----------------------------------------------------------------


def test_the_list_is_added_to_changed_and_removed(tmp_path):
    store = CommandList(tmp_path / "data" / "commands.json")
    assert store.entries() == [] and store.problem == ""
    assert store.add("  npm test  ") == "npm test"  # 两头的空白去掉
    store.add("make check")
    assert store.entries() == ["npm test", "make check"]
    assert store.words() == [["npm", "test"], ["make", "check"]]
    assert store.replace("npm test", "npm run lint") == "npm run lint"
    assert store.entries() == ["npm run lint", "make check"]  # 位置不变
    store.remove("make check")
    assert store.entries() == ["npm run lint"]
    saved = tmp_path / "data" / "commands.json"
    assert json.loads(saved.read_text(encoding="utf-8")) == {"commands": ["npm run lint"]}
    assert has_mode(saved, 0o600)
    # 另一个（命令行里的、重启以后的）读到的是同一份
    assert CommandList(saved).entries() == ["npm run lint"]


@pytest.mark.parametrize(
    "entry, says",
    [
        ("", "写一条命令"),
        ("   ", "写一条命令"),
        ("x" * (MAX_ALLOWED_COMMAND_CHARS + 1), "一条最长"),
        ("npm test; rm -rf ~", "看不懂"),
        ("npm test && make", "看不懂"),
        ("cat a | sh", "看不懂"),
        ("echo $(id)", "看不懂"),
        ("ls *", "看不懂"),
        ("echo 'open", "看不懂"),
        ("FOO=1 npm test", "看不懂"),
        ("bash", "能运行任何命令"),
        ("bash scripts/x.sh", "能运行任何命令"),
        ("/bin/sh -c ls", "能运行任何命令"),
        ("sudo systemctl restart nginx", "能运行任何命令"),
        ("env npm test", "能运行任何命令"),
        ("xargs rm", "能运行任何命令"),
        ("python", "后面要写明运行哪个脚本"),
        ("python -c 'print(1)'", "后面要写明运行哪个脚本"),
        ("node -e x", "后面要写明运行哪个脚本"),
        ("awk", "后面要写明运行哪个脚本"),
    ],
)
def test_entries_that_would_open_everything_are_refused(entry, says):
    with pytest.raises(CommandListError) as caught:
        check_entry(entry)
    assert says in str(caught.value)


@pytest.mark.parametrize("entry", ["npm test", "python manage.py check", "python -m pytest", "./scripts/status.sh", "git status", "make -C build check"])
def test_ordinary_entries_are_fine(entry):
    assert check_entry(entry)


def test_duplicates_and_the_limit(tmp_path):
    store = CommandList(tmp_path / "commands.json")
    store.add("npm test")
    with pytest.raises(CommandListError, match="已经在清单里"):
        store.add("npm  'test'")  # 拆出来的词一样，就是同一条
    store.add("npm run lint")
    with pytest.raises(CommandListError, match="已经在清单里"):
        store.replace("npm run lint", "npm test")
    with pytest.raises(CommandListError, match="已经不在清单里"):
        store.remove("make check")
    with pytest.raises(CommandListError, match="已经不在清单里"):
        store.replace("make check", "make test")
    for index in range(MAX_ALLOWED_COMMANDS - 2):
        store.add(f"tool{index} check")
    with pytest.raises(CommandListError, match="最多"):
        store.add("one more")


def test_the_file_is_read_again_when_it_changes(tmp_path):
    path = tmp_path / "commands.json"
    store = CommandList(path)
    assert store.entries() == []
    # 你在命令行里（另一个程序）加了一条：开着的这个下一次判断时就读到
    CommandList(path).add("npm test")
    assert store.words() == [["npm", "test"]]
    path.write_text(json.dumps({"commands": ["make check", "bash", "rm -rf ~; x", "make check"]}), encoding="utf-8")
    assert store.entries() == ["make check"]  # 不许加的、看不懂的、重复的都没用上
    assert "2 条" in store.problem and "bash" in store.problem
    path.write_text("{坏了", encoding="utf-8")
    assert store.entries() == [] and "坏了" in store.problem
    path.unlink()
    assert store.entries() == [] and store.problem == ""


def test_the_model_can_neither_read_nor_change_the_list(start):
    h = start()
    editor = h.agent.permissions.editor
    assert h.config.commands_file in editor.protected
    for command in ({"command": "view", "path": str(h.config.commands_file)}, {"command": "create", "path": str(h.config.commands_file), "file_text": "{}"}):
        with pytest.raises(Exception, match="权限的设置"):
            editor.run(command)
    names = {tool.name for tool in h.agent.tools}
    assert not any("command" in name and name != SHELL for name in names)  # 没有能改清单的工具


# ---- 网页上 ---------------------------------------------------------------------


def state_after(s, path: str, body: dict) -> dict:
    """做一个改清单的操作，返回它之后的那张状态（页面照着它重画）。"""
    last_id = s.ui.events_after(0, timeout=0)[-1]["id"]
    assert s.browser.post(path, body)[0] == 200
    while True:
        state = s.browser.wait_for("state")
        if state["id"] > last_id:
            return state


def test_the_page_can_manage_the_list(serve):  # noqa: F811
    s = serve()
    state = latest_state(s.browser)["commands"]
    assert state["entries"] == [] and state["limit"] == MAX_ALLOWED_COMMANDS and state["problem"] is None
    assert state["file"] == str(s.config.commands_file)
    assert s.browser.post("/api/commands/add", {"command": " npm test "}) == (200, {"ok": True, "entry": "npm test"})
    status, body = s.browser.post("/api/commands/add", {"command": "bash"})
    assert status == 409 and "能运行任何命令" in body["error"]
    assert s.browser.post("/api/commands/edit", {"old": "npm test", "command": "npm run lint"}) == (200, {"ok": True, "entry": "npm run lint"})
    assert state_after(s, "/api/commands/add", {"command": "make check"})["commands"]["entries"] == ["npm run lint", "make check"]
    assert s.browser.post("/api/commands/delete", {"command": "npm run lint"})[0] == 200
    assert s.app.agent.permissions.commands.entries() == ["make check"]
    # 在访达中显示：路径由程序算，页面什么都不给
    assert s.browser.post("/api/commands/reveal", {}) == (200, {"ok": True, "path": str(s.config.commands_file)})
    assert s.revealed[-1] == s.config.commands_file
    # 参数不对
    for path, body in (("/api/commands/add", {}), ("/api/commands/edit", {"command": "x"}), ("/api/commands/nope", {"command": "x"}), ("/api/commands/add", {"command": 3})):
        assert s.browser.post(path, body)[0] in (400, 404), path


def test_the_page_accepts_the_new_mode(serve):  # noqa: F811
    s = serve()
    assert set_approval(s, "readonly")["approval"] == "readonly"
    assert s.app.agent.permissions.mode == "readonly"


# ---- 命令行 ---------------------------------------------------------------------


def test_harness_commands_on_the_command_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "cli-data"))

    def run(*argv: str) -> tuple[int, str, str]:
        capsys.readouterr()
        code = cli.main(["commands", *argv])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    code, out, _ = run("list")
    assert code == 0 and "还没有" in out
    code, out, _ = run("add", "npm test")
    assert code == 0 and "加好了" in out and "npm test" in out
    code, _, err = run("add", "sudo reboot")
    assert code == 1 and "能运行任何命令" in err
    code, out, _ = run("list")
    assert code == 0 and "  npm test" in out and "commands.json" in out
    code, out, _ = run("remove", "npm test")
    assert code == 0 and "删掉了" in out
    code, _, err = run("remove", "npm test")
    assert code == 1 and "已经不在清单里" in err
    assert CommandList(tmp_path / "cli-data" / "commands.json").entries() == []
