"""你的钩子（harness/hooks/）：在固定的时刻运行你配置的命令。

钩子都是临时目录里的一小段 Python 脚本，用 sys.executable 运行（不写 shell 脚本：Windows 上也要跑得起来）。
脚本把收到的 JSON、工作目录、几个环境变量记进一个文件，再按测试要的样子回答（标准输出、标准错误、退出码）。
助手连的是本地的假 API（fake_api.py），不花钱。要验证的是：

  配置      data/hooks.json 的检查（名字、事件、matcher、命令不经过 shell、不许引用模型的密钥、时限）、读写（600）、
            你手动改了文件下一次就按新的、写坏了一个都不跑
  怎么跑    参数列表、在工作目录里、环境里没有密钥、JSON 从标准输入进去、超时结束掉、输出太多也不卡住
  怎么回答  退出码 0 / 2 / 别的、JSON 里的 decision、permissionDecision、additionalContext、systemMessage、onFailure
  四个时刻  PreToolUse 在确认之后、能拦下、能再问你一次、不能放宽；PostToolUse 接在结果后面（有上限、历史前面不动）；
            UserPromptSubmit 拦下消息（不发请求）、附说明；Stop 只是通知
  在哪儿跑  你的对话、定时任务、聊天通道（runsIn）、子 Agent 的工具调用
  运行记录  每跑一个钩子一段，工具前后的挂在那次调用下面，合计里有钩子的时间
  安全      文件工具碰不了 hooks.json、模型没有能改钩子的工具、导出时参数里的令牌遮住
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import RecordingUI
from fake_api import reply, text, tool_use
from test_agents import install

from harness.config import HOOK_NOTE_CHARS
from harness.export import write_archive
from harness.hooks import HookConfig, HookError, HookRun, Hooks
from harness.hooks.config import from_form, model_key_references, normalize
from harness.hooks.protocol import interpret
from harness.hooks.runner import Launcher, Ran
from harness.providers.base import ToolCall
from harness.trace.text import lines

POSIX = os.name != "nt"


def script(folder: Path, name: str, stdout: str = "", stderr: str = "", code: int = 0, sleep: float = 0) -> tuple[list[str], Path]:
    """写一个钩子脚本：把收到的 JSON、工作目录、几个环境变量一行一个记进 <name>.log，再照着给的样子回答。
    返回（交给 command / args 的参数列表，记录文件）。"""
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / f"{name}.log"
    body = f"""
import json, os, sys, time
data = json.load(sys.stdin)
seen = {{key: os.environ.get(key) for key in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "HARNESS_HOOK_EVENT", "HARNESS_HOOK_NAME", "HARNESS_PROJECT_DIR", "HOOK_TEST_MARK")}}
with open({str(log)!r}, "a", encoding="utf-8") as file:
    file.write(json.dumps({{"payload": data, "cwd": os.getcwd(), "env": seen}}, ensure_ascii=False) + "\\n")
time.sleep({sleep!r})
sys.stdout.write({stdout!r})
sys.stderr.write({stderr!r})
sys.exit({code!r})
"""
    path = folder / f"{name}.py"
    path.write_text(body, encoding="utf-8")
    return [sys.executable, str(path)], log


def calls_of(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def add(h, name: str, event: str, argv: list[str], **fields) -> None:
    h.app.hooks.config.put(name, {"event": event, "command": argv[0], "args": argv[1:], **fields})


def tool_result_block(request: dict, tool_id: str) -> dict:
    for message in request["messages"]:
        if message["role"] == "user" and isinstance(message["content"], list):
            for block in message["content"]:
                if block.get("type") == "tool_result" and block.get("tool_use_id") == tool_id:
                    return block
    raise AssertionError(f"没有 {tool_id} 的结果")


def result_text(block: dict) -> str:
    content = block["content"]
    return content if isinstance(content, str) else "".join(part.get("text", "") for part in content)


def user_text(request: dict, index: int = 0) -> str:
    """请求里第 index 条用户消息的全部文字（附在消息里的说明也在里面）。"""
    users = [message for message in request["messages"] if message["role"] == "user"]
    content = users[index]["content"]
    return content if isinstance(content, str) else "\n".join(part.get("text", "") for part in content if part.get("type") == "text")


ADD_TODO = reply(tool_use("t1", "todo_add", {"title": "买牛奶"}), stop_reason="tool_use")
LIST_TODOS = reply(tool_use("t1", "todo_list", {}), stop_reason="tool_use")
CREATE_FILE = reply(tool_use("t1", "str_replace_based_edit_tool", {"command": "create", "path": "made.txt", "file_text": "hi\n"}), stop_reason="tool_use")


# ---- 配置 -----------------------------------------------------------------------


def test_a_hook_is_checked_before_it_is_saved(tmp_path):
    good = {"event": "PreToolUse", "matcher": " shell | files ,mcp__github__* ", "command": "python3", "args": ["check.py"]}
    assert normalize("no-rm", good) == {"event": "PreToolUse", "matcher": "shell|files|mcp__github__*", "command": "python3", "args": ["check.py"], "enabled": True}
    assert normalize("all", {**good, "matcher": ""})["matcher"] == "*"
    assert "matcher" not in normalize("log", {"event": "Stop", "command": "python3", "matcher": "shell"})  # 只有工具的两个事件看它
    # onFailure: block 只对能拦下的事件留着；runsIn 是全部时不写
    assert "onFailure" not in normalize("x", {"event": "Stop", "command": "python3", "onFailure": "block"})
    assert normalize("x", {"event": "PreToolUse", "command": "python3", "onFailure": "block", "runsIn": ["task"]})["runsIn"] == ["task"]
    assert "runsIn" not in normalize("x", {"event": "PreToolUse", "command": "python3", "runsIn": ["channel", "chat", "task"]})
    bad = [
        ({**good, "event": "SessionStart"}, "event"),
        ({**good, "matcher": "Bash(rm *)"}, "看不懂"),
        ({**good, "command": "python3 check.py", "args": []}, "不经过 shell"),
        ({**good, "command": ""}, "command"),
        ({**good, "args": "check.py"}, "args"),
        ({**good, "timeout": 0}, "timeout"),
        ({**good, "timeout": 10_000}, "timeout"),
        ({**good, "timeout": True}, "timeout"),
        ({**good, "onFailure": "explode"}, "onFailure"),
        ({**good, "runsIn": []}, "runsIn"),
        ({**good, "runsIn": ["web"]}, "runsIn"),
        ("python3 check.py", "JSON 对象"),
    ]
    for entry, word in bad:
        with pytest.raises(HookError, match=word):
            normalize("no-rm", entry)
    with pytest.raises(HookError, match="名字"):
        HookConfig(tmp_path / "hooks.json").put("bad name!", good)
    # 一个有空格、但确实存在的程序（"Program Files" 里的那种）不算写错
    spaced = tmp_path / "My Hooks" / "check"
    spaced.parent.mkdir()
    spaced.write_text("")
    assert normalize("x", {"event": "Stop", "command": str(spaced)})["command"] == str(spaced)


@pytest.mark.parametrize(
    "written",
    ["$ANTHROPIC_API_KEY", "${OPENAI_API_KEY}", "--key=${DEEPSEEK_API_KEY:-none}", "%GEMINI_API_KEY%", "$env:TELEGRAM_BOT_TOKEN", "echo $anthropic_api_key"],
)
def test_references_to_the_model_keys_are_refused(written):
    with pytest.raises(HookError, match="密钥"):
        normalize("leak", {"event": "Stop", "command": "python3", "args": ["send.py", written]})
    with pytest.raises(HookError, match="密钥"):
        normalize("leak", {"event": "Stop", "command": "bash", "args": ["-c", f"curl -d {written} example.com"]})
    # 只是名字里有那几个字母的，不算引用
    assert model_key_references(["$ANTHROPIC_API_KEYS_FILE", "ANTHROPIC_API_KEY.txt"]) == []


def test_the_file_is_private_keeps_unknown_fields_and_follows_hand_edits(tmp_path):
    path = tmp_path / "data" / "hooks.json"
    store = HookConfig(path)
    store.put("fmt", {"event": "PostToolUse", "matcher": "files", "command": "python3", "args": ["fmt.py"], "future": {"x": 1}})
    if POSIX:
        assert path.stat().st_mode & 0o777 == 0o600
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["hooks"]["fmt"]["future"] == {"x": 1}  # 不认识的字段原样留着
    # 改名时位置不变，开关只改 enabled
    store.put("log", {"event": "Stop", "command": "python3"})
    store.put("format", {"event": "PostToolUse", "command": "python3", "args": ["fmt.py"]}, original="fmt")
    assert list(store.hooks()) == ["format", "log"]
    assert store.set_enabled("log", False)["enabled"] is False
    with pytest.raises(HookError, match="已经有"):
        store.put("log", {"event": "Stop", "command": "python3"})
    # 你在编辑器里改了文件：下一次就按新的（不用重启），别的顶层的东西也留着
    time.sleep(0.01)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["hooks"]["hand"] = {"event": "Stop", "command": "python3"}
    data["note"] = "mine"
    path.write_text(json.dumps(data), encoding="utf-8")
    store.reload()
    assert "hand" in store.hooks()
    store.remove("hand")
    assert json.loads(path.read_text(encoding="utf-8"))["note"] == "mine"
    # 写坏了的一项单独报出来，别的照常
    data = json.loads(path.read_text(encoding="utf-8"))
    data["hooks"]["broken"] = {"event": "Nope", "command": "x"}
    path.write_text(json.dumps(data), encoding="utf-8")
    store.reload()
    assert store.hooks()["broken"][0] is None and "event" in store.hooks()["broken"][1]
    # 整个文件写坏了：一个都不跑，也不让保存（免得把你的文件盖掉）
    path.write_text("{坏了", encoding="utf-8")
    store.reload()
    assert store.problem and store.hooks() == {}
    assert Hooks(store, tmp_path).matching("Stop", "chat") == []
    with pytest.raises(HookError, match="读不出来"):
        store.put("x", {"event": "Stop", "command": "python3"})
    assert path.read_text(encoding="utf-8") == "{坏了"


def test_the_form_keeps_masked_arguments(tmp_path):
    old = {"event": "Stop", "command": "python3", "args": ["notify.py", "--token=abcdefghijklmnop", "plain"], "future": 1}
    shown = from_form({"event": "Stop", "command": "python3", "args": ["notify.py", "--token=••••", "plain"]}, old)
    assert shown["args"] == old["args"] and shown["future"] == 1  # 遮住的那一项没改：用原来的
    with pytest.raises(HookError, match="••••"):
        from_form({"event": "Stop", "command": "python3", "args": ["--token=••••x"]}, old)


# ---- 怎么跑、怎么回答 --------------------------------------------------------------


def run_one(tmp_path, event: str, **reply_with) -> tuple[Ran, object, list[dict]]:
    argv, log = script(tmp_path / "hooks", "one", **reply_with)
    store = HookConfig(tmp_path / "hooks.json")
    store.put("one", {"event": event, "command": argv[0], "args": argv[1:], "timeout": reply_with.get("sleep", 0) and 1 or 30})
    ran, outcome = Hooks(store, tmp_path).execute(Launcher(), "one", store.get("one"), {"session_id": "s1"})
    return ran, outcome, calls_of(log)


def test_the_hook_runs_in_the_workspace_without_the_model_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-1234567890")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:bot-secret")
    monkeypatch.setenv("HOOK_TEST_MARK", "kept")
    ran, outcome, (seen,) = run_one(tmp_path, "Stop")
    assert ran.exit == 0 and outcome.result == "ok"
    assert seen["env"] == {
        "ANTHROPIC_API_KEY": None, "TELEGRAM_BOT_TOKEN": None, "HARNESS_HOOK_EVENT": "Stop", "HARNESS_HOOK_NAME": "one",
        "HARNESS_PROJECT_DIR": str(tmp_path), "HOOK_TEST_MARK": "kept",
    }  # fmt: skip
    assert Path(seen["cwd"]).resolve() == tmp_path.resolve()
    assert seen["payload"] == {"hook_event_name": "Stop", "hook_name": "one", "session_id": "s1"}


def test_exit_codes_and_json_answers(tmp_path):
    deny = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "不许"}})
    cases = [
        ("PreToolUse", {"stdout": deny}, ("blocked", "不许")),
        ("PreToolUse", {"stdout": json.dumps({"decision": "block", "reason": "旧写法"})}, ("blocked", "旧写法")),
        ("PreToolUse", {"stderr": "退出码 2 的原因\n", "code": 2}, ("blocked", "退出码 2 的原因")),
        ("PreToolUse", {"stdout": json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask", "permissionDecisionReason": "看看"}})}, ("ask", "看看")),
        ("PreToolUse", {"stdout": json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}})}, ("ok", "")),
        # 写给别的事件的 hookSpecificOutput 不算
        ("PreToolUse", {"stdout": json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "permissionDecision": "deny"}})}, ("ok", "")),
        ("UserPromptSubmit", {"stdout": json.dumps({"decision": "block", "reason": "别问这个"})}, ("blocked", "别问这个")),
        ("UserPromptSubmit", {"stdout": "纯文字也算说明"}, ("ok", "")),
        ("PostToolUse", {"stderr": "结果是生成的", "code": 2}, ("ok", "")),
        ("Stop", {"stdout": json.dumps({"decision": "block", "reason": "不会接着做"})}, ("ok", "")),
        ("PreToolUse", {"stderr": "坏了", "code": 1}, ("error", "")),
        ("PreToolUse", {"stdout": "{不是 JSON"}, ("error", "")),
    ]
    for index, (event, answer, (result, reason)) in enumerate(cases):
        _, outcome, _ = run_one(tmp_path / str(index), event, **answer)
        assert (outcome.result, outcome.reason) == (result, reason), (event, answer, outcome)
    _, outcome, _ = run_one(tmp_path / "ctx", "UserPromptSubmit", stdout="纯文字也算说明\n")
    assert outcome.context == "纯文字也算说明"
    _, outcome, _ = run_one(tmp_path / "post", "PostToolUse", stderr="结果是生成的", code=2)
    assert outcome.context == "结果是生成的"
    _, outcome, _ = run_one(tmp_path / "allow", "PreToolUse", stdout=json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}))
    assert any("allow" in note for note in outcome.ignored)  # 试运行时告诉你：allow 不会放宽
    _, outcome, _ = run_one(tmp_path / "msg", "Stop", stdout=json.dumps({"systemMessage": "记好了"}))
    assert outcome.message == "记好了"
    _, outcome, _ = run_one(tmp_path / "err", "PreToolUse", stderr="坏了\n第二行", code=1)
    assert "1" in outcome.problem and "坏了" in outcome.problem


def test_a_hook_that_fails_can_count_as_a_block(tmp_path):
    ran = Ran(None, "", "", 0.1, "start", "找不到")
    assert interpret("PreToolUse", ran, "block").result == "blocked"
    assert interpret("UserPromptSubmit", Ran(1, "", "x", 0.1), "block").result == "blocked"
    assert interpret("PostToolUse", Ran(1, "", "x", 0.1), "block").result == "error"  # 做完了的事拦不下
    assert interpret("PreToolUse", Ran(None, "", "", 1.0, "timeout"), "continue", 1).result == "timeout"
    # 找不到程序：启动不了，不会卡住
    store = HookConfig(tmp_path / "hooks.json")
    store.put("ghost", {"event": "PreToolUse", "command": str(tmp_path / "no-such-program")})
    ran, outcome = Hooks(store, tmp_path).execute(Launcher(), "ghost", store.get("ghost"), {})
    assert ran.failure == "start" and outcome.result == "error" and "找不到" in outcome.problem


def test_a_slow_hook_is_ended_at_its_time_limit(tmp_path):
    began = time.monotonic()
    ran, outcome, _ = run_one(tmp_path, "PreToolUse", sleep=20)
    assert ran.failure == "timeout" and outcome.result == "timeout" and "1" in outcome.problem
    assert time.monotonic() - began < 10


def test_a_hook_that_floods_its_output_does_not_hang(tmp_path):
    (tmp_path / "hooks").mkdir()
    flood = tmp_path / "hooks" / "flood2.py"
    flood.write_text("import sys\nsys.stdin.read()\nsys.stdout.write('x' * 3_000_000)\nsys.stderr.write('y' * 3_000_000)\n", encoding="utf-8")
    store = HookConfig(tmp_path / "hooks.json")
    store.put("flood", {"event": "PostToolUse", "command": sys.executable, "args": [str(flood)]})
    # 交给它的 JSON 也很大（工具的结果很长）：写标准输入、读输出各在各的线程里，谁也不等谁
    ran, _ = Hooks(store, tmp_path).execute(Launcher(), "flood", store.get("flood"), {"tool_response": "z" * 2_000_000})
    assert ran.exit == 0 and len(ran.stdout) <= 100_000 and len(ran.stderr) <= 100_000


def test_stop_ends_only_the_running_hook_of_this_run(tmp_path):
    argv, _ = script(tmp_path / "hooks", "slow", sleep=30)
    store = HookConfig(tmp_path / "hooks.json")
    store.put("slow", {"event": "PreToolUse", "command": argv[0], "args": argv[1:], "timeout": 60})
    hooks = Hooks(store, tmp_path)
    mine, other = HookRun(hooks, "chat"), HookRun(hooks, "task")
    agent = _FakeAgent()
    threading.Timer(0.5, mine.stop).start()
    other.stop()  # 停别人的：碰不到这一个
    began = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        mine.before_tool(agent, ToolCall("t1", "todo_add", {}), None)
    assert time.monotonic() - began < 10
    assert agent.trace.spans[-1].data["result"] == "stopped"
    assert hooks.last()["slow"]["result"] == "stopped"


class _FakeAgent:
    """只给 HookRun 用的一点点"助手"：权限的档位、模型、界面、运行记录。"""

    def __init__(self):
        from types import SimpleNamespace

        from harness.trace import Tracer

        self.permissions = SimpleNamespace(mode="ask")
        self.backend = SimpleNamespace(provider="anthropic", model="claude-opus-5-5")
        self.ui = RecordingUI()
        self.trace = Tracer()
        self.trace.turn_began()


# ---- PreToolUse -------------------------------------------------------------------


def test_pre_tool_use_blocks_a_call_after_approval(start):
    h = start(ADD_TODO, reply(text("被拦下了。")))
    argv, log = script(h.config.workspace.parent / "hooks", "no-todo", stderr="今天不加待办", code=2)
    add(h, "no-todo", "PreToolUse", argv, matcher="todo")
    h.agent.run_turn("加一条待办")
    assert h.todos.items() == []  # 工具没有执行
    block = tool_result_block(h.api.requests[1], "t1")
    assert block["is_error"] and "被用户的钩子“no-todo”拦下了" in result_text(block) and "今天不加待办" in result_text(block)
    # 你看到的是界面的语言写的那一句
    assert h.ui.results[0][0].startswith("被你的钩子“no-todo”拦下了") and h.ui.results[0][1]
    (seen,) = calls_of(log)
    payload = seen["payload"]
    assert payload["hook_event_name"] == "PreToolUse" and payload["tool_name"] == "todo_add" and payload["tool_group"] == "todo"
    assert payload["tool_input"] == {"title": "买牛奶"} and payload["tool_use_id"] == "t1"
    assert payload["source"] == "chat" and payload["permission_mode"] == "ask" and payload["session_id"] == h.app.conversation.id
    assert payload["model"] == "claude-opus-5-5" and payload["cwd"] == str(h.config.workspace)


def test_pre_tool_use_only_runs_after_you_allowed_the_call(start):
    h = start(CREATE_FILE, reply(text("好吧")), approve=False)
    argv, log = script(h.config.workspace.parent / "hooks", "watch")
    add(h, "watch", "PreToolUse", argv)
    h.agent.run_turn("建个文件")
    assert h.ui.confirmations and calls_of(log) == []  # 你拒绝了：工具不会执行，钩子也不跑
    assert not (h.config.workspace / "made.txt").exists()


def test_allow_from_a_hook_never_skips_a_question(start):
    allow = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}})
    h = start(CREATE_FILE, reply(text("好了")))
    argv, log = script(h.config.workspace.parent / "hooks", "allow", stdout=allow)
    add(h, "allow", "PreToolUse", argv)
    h.agent.run_turn("建个文件")
    assert len(h.ui.confirmations) == 1 and len(calls_of(log)) == 1  # 照常问了你；钩子在你允许之后才跑
    assert (h.config.workspace / "made.txt").exists()


def test_a_hook_can_ask_you_even_when_the_mode_would_not(start):
    ask = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask", "permissionDecisionReason": "待办太多了"}})
    h = start(ADD_TODO, reply(text("你没让加")), approve=False, feedback="先别加")
    argv, _ = script(h.config.workspace.parent / "hooks", "ask", stdout=ask)
    add(h, "ask", "PreToolUse", argv)
    h.agent.permissions.mode = "all"  # 全部自动允许：本来一个都不问
    h.agent.run_turn("加一条待办")
    (question,) = h.ui.confirmations
    assert "你的钩子“ask”要你先确认这一步" in question and "待办太多了" in question and "todo_add" in question
    block = tool_result_block(h.api.requests[1], "t1")
    assert block["is_error"] and "先别加" in result_text(block)
    assert h.agent.trace.last["spans"][1]["approval"] == "refused"


def test_the_matcher_picks_tools_by_capability_name_or_glob(start):
    h = start(LIST_TODOS, reply(text("好")))
    folder = h.config.workspace.parent / "hooks"
    files, files_log = script(folder, "files")
    todo, todo_log = script(folder, "todo")
    glob, glob_log = script(folder, "glob")
    add(h, "files", "PreToolUse", files, matcher="files|shell")
    add(h, "todo", "PreToolUse", todo, matcher="todo")
    add(h, "glob", "PreToolUse", glob, matcher="todo_l*")
    h.agent.run_turn("看看待办")
    assert calls_of(files_log) == [] and len(calls_of(todo_log)) == 1 and len(calls_of(glob_log)) == 1


# ---- PostToolUse ------------------------------------------------------------------


def test_post_tool_use_appends_a_capped_note_to_this_result_only(start):
    note = json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "注意" + "很长" * 5000}})
    h = start(reply(text("先聊一句")), LIST_TODOS, reply(text("看到了")))
    argv, log = script(h.config.workspace.parent / "hooks", "note", stdout=note)
    h.agent.run_turn("你好")
    first = json.dumps(h.agent.messages, ensure_ascii=False)
    add(h, "note", "PostToolUse", argv, matcher="todo")
    h.agent.run_turn("看看待办")
    block = tool_result_block(h.api.requests[2], "t1")
    content = result_text(block)
    assert '<hook_note name="note">' in content and content.index("<hook_note") > 0  # 接在工具真正的结果后面
    note_part = content[content.index("<hook_note") :]
    assert len(note_part) < HOOK_NOTE_CHARS + 100
    # 只在末尾追加：前一轮的历史一个字都没变，请求的开头和上一轮的一样
    assert json.dumps(h.agent.messages[: len(json.loads(first))], ensure_ascii=False) == first
    (seen,) = calls_of(log)
    assert seen["payload"]["tool_response"] == content[: content.index("\n\n<hook_note")]
    assert seen["payload"]["is_error"] is False and isinstance(seen["payload"]["duration_ms"], int)
    assert h.ui.results[-1][0] == content  # 页面上的工具卡片里也看得到补的话


def test_post_tool_use_does_not_run_for_calls_that_never_ran(start):
    h = start(ADD_TODO, reply(text("好")))
    folder = h.config.workspace.parent / "hooks"
    block, _ = script(folder, "block", code=2, stderr="不")
    after, after_log = script(folder, "after")
    add(h, "block", "PreToolUse", block)
    add(h, "after", "PostToolUse", after)
    h.agent.run_turn("加一条")
    assert calls_of(after_log) == []


# ---- UserPromptSubmit 和 Stop -------------------------------------------------------


def test_a_blocked_message_never_reaches_the_model(start):
    h = start(reply(text("不应该被调用")))
    argv, log = script(h.config.workspace.parent / "hooks", "gate", stdout=json.dumps({"decision": "block", "reason": "这个问题不问"}))
    add(h, "gate", "UserPromptSubmit", argv)
    h.agent.run_turn("问一个敏感的问题")
    assert h.api.requests == [] and h.agent.messages == []
    assert any("这条消息被你的钩子“gate”拦下了" in info and "这个问题不问" in info for info in h.ui.infos)
    record = h.agent.trace.last
    assert record["ended"] == "blocked" and record["spans"][0]["kind"] == "hook" and record["spans"][0]["result"] == "blocked"
    assert calls_of(log)[0]["payload"]["prompt"] == "问一个敏感的问题"


def test_user_prompt_submit_can_attach_context_to_this_message(start):
    h = start(reply(text("好")))
    argv, _ = script(h.config.workspace.parent / "hooks", "ctx", stdout="今天是项目的发布日")
    add(h, "ctx", "UserPromptSubmit", argv)
    h.agent.run_turn("今天做什么")
    said = user_text(h.api.requests[0])
    assert '<hook_context name="ctx">\n今天是项目的发布日\n</hook_context>' in said
    assert said.index("<hook_context") < said.index("今天做什么")  # 附在你的话前面，和时间一样


def test_regenerating_a_turn_does_not_run_the_prompt_hook_again(start):
    h = start(reply(text("第一次")), reply(text("第二次")))
    argv, log = script(h.config.workspace.parent / "hooks", "ctx")
    add(h, "ctx", "UserPromptSubmit", argv)
    start_mark = h.agent.backend.mark()
    h.agent.run_turn("你好")
    h.agent.retry_turn(start_mark)
    assert len(calls_of(log)) == 1  # 那条消息原样留在历史里，没有重新发


def test_stop_is_a_notification_with_the_last_answer(start):
    h = start(LIST_TODOS, reply(text("你没有待办。")))
    argv, log = script(h.config.workspace.parent / "hooks", "done", stdout=json.dumps({"systemMessage": "记进日志了"}))
    add(h, "done", "Stop", argv)
    h.agent.run_turn("看看待办")
    (seen,) = calls_of(log)
    assert seen["payload"]["last_assistant_message"] == "你没有待办。" and seen["payload"]["ended"] == "done"
    assert seen["payload"]["stop_hook_active"] is False
    assert any("钩子“done”：记进日志了" in info for info in h.ui.infos)
    assert h.agent.trace.last["spans"][-1]["kind"] == "hook"  # 它的那一段也记在这一轮里


def test_a_broken_hook_warns_you_and_carries_on(start):
    h = start(ADD_TODO, reply(text("加好了")))
    argv, _ = script(h.config.workspace.parent / "hooks", "broken", stderr="Traceback: boom", code=1)
    add(h, "broken", "PreToolUse", argv)
    h.agent.run_turn("加一条")
    assert not tool_result_block(h.api.requests[1], "t1").get("is_error")
    assert any("钩子“broken”出错了" in info and "boom" in info for info in h.ui.infos)


def test_on_failure_block_fails_closed(start):
    h = start(ADD_TODO, reply(text("没加成")))
    add(h, "guard", "PreToolUse", [str(h.config.workspace / "no-such-guard")], onFailure="block")
    h.agent.run_turn("加一条")
    block = tool_result_block(h.api.requests[1], "t1")
    assert block["is_error"] and "按设置当成拦下" in result_text(block)


def test_disabled_hooks_do_not_run(start):
    h = start(LIST_TODOS, reply(text("好")))
    argv, log = script(h.config.workspace.parent / "hooks", "off")
    add(h, "off", "PreToolUse", argv, enabled=False)
    h.agent.run_turn("看看")
    assert calls_of(log) == []


# ---- 在哪儿跑 -----------------------------------------------------------------------


def test_unattended_runs_fire_hooks_by_default_and_runs_in_narrows_it(start):
    h = start(LIST_TODOS, reply(text("好")), LIST_TODOS, reply(text("好")), LIST_TODOS, reply(text("好")))
    folder = h.config.workspace.parent / "hooks"
    every, every_log = script(folder, "every")
    tasks_only, tasks_log = script(folder, "tasks")
    add(h, "every", "PreToolUse", every)
    add(h, "tasks", "PreToolUse", tasks_only, runsIn=["task"])
    h.agent.run_turn("看看")
    task = h.app.unattended_agent(RecordingUI())
    task.agent.run_turn("定时任务：看看")
    channel = h.app.channel_agent(RecordingUI(), "ask", "anthropic", "claude-opus-5-5")
    channel.agent.run_turn("聊天里：看看")
    assert [call["payload"]["source"] for call in calls_of(every_log)] == ["chat", "task", "channel"]
    assert [call["payload"]["source"] for call in calls_of(tasks_log)] == ["task"]


def test_stopping_an_unattended_run_ends_its_hook(start):
    h = start()
    argv, _ = script(h.config.workspace.parent / "hooks", "slow", sleep=30)
    add(h, "slow", "PreToolUse", argv, timeout=60)
    run = h.app.unattended_agent(RecordingUI())
    agent = run.agent
    threading.Timer(0.5, run.stop).start()
    began = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        agent.hooks.before_tool(agent, ToolCall("t1", "todo_list", {}), agent.tools.get("todo_list"))
    assert time.monotonic() - began < 10


def test_subagent_tool_calls_fire_the_tool_hooks(start):
    h = start(
        reply(tool_use("t1", "agent", {"name": "代码审查员", "task": "看看 notes.txt"}), stop_reason="tool_use"),
        reply(tool_use("s1", "str_replace_based_edit_tool", {"command": "view", "path": "notes.txt"}), stop_reason="tool_use"),
        reply(text("看不了。")),
        reply(text("它说看不了。")),
    )
    (h.config.workspace / "notes.txt").write_text("秘密\n", encoding="utf-8")
    install(h, "reviewer", tools="Read")
    folder = h.config.workspace.parent / "hooks"
    argv, log = script(folder, "no-read", stderr="不许读", code=2)
    prompt, prompt_log = script(folder, "prompt")
    add(h, "no-read", "PreToolUse", argv, matcher="files")
    add(h, "prompt", "UserPromptSubmit", prompt)
    h.agent.run_turn("交给代码审查员")
    (seen,) = calls_of(log)
    assert seen["payload"]["agent_type"] == "代码审查员" and seen["payload"]["tool_input"]["path"] == "notes.txt"
    sub = tool_result_block(h.api.requests[2], "s1")
    assert sub["is_error"] and "不许读" in result_text(sub) and "秘密" not in result_text(sub)
    assert len(calls_of(prompt_log)) == 1  # 子 Agent 的"一轮"不是你发的消息：只有你那一条跑了 UserPromptSubmit
    # 运行记录：钩子挂在子 Agent 那次调用下面
    delegated = h.agent.trace.last["spans"][2]  # 前面是 UserPromptSubmit 那一段、第一次请求
    (child_tool,) = [span for span in delegated["children"] if span["kind"] == "tool"]
    assert child_tool["children"][0]["kind"] == "hook" and child_tool["children"][0]["result"] == "blocked"


# ---- 运行记录 -----------------------------------------------------------------------


def test_each_hook_run_is_a_span_in_the_trace(start):
    h = start(LIST_TODOS, reply(text("好")))
    folder = h.config.workspace.parent / "hooks"
    before, _ = script(folder, "before")
    after, _ = script(folder, "after", stdout=json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "补一句"}}))
    prompt, _ = script(folder, "prompt")
    add(h, "prompt", "UserPromptSubmit", prompt)
    add(h, "before", "PreToolUse", before)
    add(h, "after", "PostToolUse", after)
    h.agent.run_turn("看看")
    record = h.agent.trace.last
    spans = record["spans"]
    assert [span["kind"] for span in spans] == ["hook", "model", "tool", "model"]
    assert spans[0]["event"] == "UserPromptSubmit" and spans[0]["name"] == "prompt" and spans[0]["result"] == "ok" and spans[0]["exit"] == 0
    tool = spans[2]
    assert [(child["event"], child["name"]) for child in tool["children"]] == [("PreToolUse", "before"), ("PostToolUse", "after")]
    assert tool["children"][1]["added"] == len("补一句")
    assert all(tool["start"] <= child["start"] <= child["end"] <= tool["end"] for child in tool["children"])
    totals = record["totals"]
    assert totals["hook_runs"] == 3 and totals["hooks_blocked"] == 0 and totals["hooks"] > 0
    assert totals["model"] + totals["tools"] + totals["hooks"] + totals["waiting"] + totals["other"] == record["wall"]
    assert h.app.hooks.last()["after"]["result"] == "ok"
    # 终端的 /trace 也写出来了
    shown = "\n".join(line for line, _ in lines(record))
    assert "钩子 before（PreToolUse）" in shown and "跑了 3 次你的钩子" in shown


# ---- 安全 ----------------------------------------------------------------------


def test_the_model_cannot_read_or_change_the_hooks(start):
    h = start()
    editor = h.agent.permissions.editor
    assert h.config.hooks_file in editor.protected
    h.app.hooks.config.put("x", {"event": "Stop", "command": "python3"})
    for command in ({"command": "view", "path": str(h.config.hooks_file)}, {"command": "create", "path": str(h.config.hooks_file), "file_text": "{}"}):
        with pytest.raises(Exception, match="权限的设置"):
            editor.run(command)
    names = {tool.name for tool in h.agent.tools}
    assert not any("hook" in name.lower() for name in names)  # 没有能加、改钩子的工具
    # 读它的命令照样要问（数据目录里的东西，哪一档都问，除了"全部自动允许"）
    assert h.agent.permissions.review("run_command", {"command": f"cat {h.config.hooks_file}"}) is not None


def test_export_masks_tokens_in_hook_arguments(start, tmp_path):
    import io
    import zipfile
    from datetime import datetime

    h = start()
    h.app.hooks.config.put("notify", {"event": "Stop", "command": "python3", "args": ["notify.py", "--token", "hook-secret-value-123", "--api-key=sk-hookkey-1234567890abcdef"]})
    out = io.BytesIO()
    write_archive(h.config.data_dir, out, datetime(2026, 11, 22, 10, 0))
    with zipfile.ZipFile(out) as archive:
        saved = archive.read("hooks.json").decode()
    assert "hook-secret-value-123" not in saved and "sk-hookkey" not in saved and "notify.py" in saved


# ---- 命令的沙箱 -----------------------------------------------------------------------


def test_hooks_run_in_the_same_sandbox_as_the_commands(start, monkeypatch):
    import fake_sandbox

    fake = fake_sandbox.install(monkeypatch)
    h = start(LIST_TODOS, reply(text("好")))
    argv, log = script(h.config.workspace.parent / "hooks", "boxed")
    add(h, "boxed", "PreToolUse", argv)
    h.app.settings.set_sandbox({"level": "workspace"})
    h.agent.run_turn("看看")
    assert len(calls_of(log)) == 1  # 假沙箱照原样运行：钩子照常跑了
    ((policy, boxed),) = fake.calls
    assert boxed == argv and policy.level == "workspace"
    # 钩子自己的脚本在沙箱里可以读（不然它根本启动不了），只能读、不能写；数据目录照样碰不到
    script_path = Path(argv[1]).resolve()
    assert script_path in policy.read and script_path not in policy.write
    assert any(h.config.data_dir.resolve() == path or h.config.data_dir.resolve() in path.parents for path in policy.deny)
    assert h.agent.trace.last["spans"][1]["children"][0]["sandbox"] == "workspace"
    # 关掉沙箱：照常在外面跑，记录里也没有这一项
    h.app.settings.set_sandbox({"level": "off"})
    h2 = start(LIST_TODOS, reply(text("好")))  # 同一个数据目录：同一个钩子
    h2.agent.run_turn("看看")
    assert len(fake.calls) == 1 and "sandbox" not in h2.agent.trace.last["spans"][1]["children"][0]


def test_a_hook_never_runs_outside_a_sandbox_that_is_on_but_missing(start, monkeypatch):
    import fake_sandbox

    fake_sandbox.install(monkeypatch, problem="没有装 bwrap")
    h = start(ADD_TODO, reply(text("好")))
    folder = h.config.workspace.parent / "hooks"
    soft, soft_log = script(folder, "soft")
    guard, guard_log = script(folder, "guard")
    add(h, "soft", "PreToolUse", soft)
    add(h, "guard", "PreToolUse", guard, onFailure="block")
    h.app.settings.set_sandbox({"level": "workspace"})
    h.agent.run_turn("加一条")  # 待办工具不经过沙箱；它前面的两个钩子要进沙箱
    assert calls_of(soft_log) == [] and calls_of(guard_log) == []  # 一个都没运行
    assert any("钩子“soft”出错了" in info and "没有装 bwrap" in info for info in h.ui.infos)
    block = tool_result_block(h.api.requests[1], "t1")
    assert block["is_error"] and "按设置当成拦下" in result_text(block)  # 管安全的那一个：用不了就当成拦下
    assert h.todos.items() == []
