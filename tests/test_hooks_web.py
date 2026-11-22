"""你的钩子在网页上（/api/hooks/……，见 webui/hooks_api.py）和命令行里（harness hooks，见 hooks/cli.py）。

和 stdio 的 MCP 服务器一样，保存一个钩子就是让这台电脑在某个时刻运行一条命令，所以：只有带口令的页面能改；
页面上看到的参数遮住了像密钥的部分；不许引用模型的密钥；试运行真的运行它，结果说清楚它会让智狐怎么办。
"""

from __future__ import annotations

import json
import sys

import test_webui
from fake_api import reply, text
from test_hooks import calls_of, script
from test_webui import latest_state, request

from harness.cli import main

serve = test_webui.serve


def form(argv: list[str], event: str = "PreToolUse", **fields) -> dict:
    return {"event": event, "matcher": fields.pop("matcher", ""), "command": argv[0], "args": argv[1:], "enabled": True, **fields}


def test_hooks_need_the_token(serve):
    s = serve()
    host = s.browser.host
    for path in ("/api/hooks/list", "/api/hooks/save", "/api/hooks/test", "/api/hooks/delete"):
        assert request(host, "POST", path, {"name": "x"})[0] == 403
    evil = {"X-Harness-Token": s.browser.token, "Origin": "http://evil.example"}
    assert request(host, "POST", "/api/hooks/save", {"name": "x", "hook": form(["python3"])}, evil)[0] == 403
    assert not s.config.hooks_file.exists()


def test_the_page_can_add_edit_test_and_delete_a_hook(serve, tmp_path):
    s = serve()
    assert latest_state(s.browser)["hooks"] == {"count": 0, "on": 0, "problem": None, "ran": 0}
    status, body = s.browser.post("/api/hooks/list")
    assert status == 200 and body["hooks"] == [] and body["events"] == ["PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"]
    assert {"id": "shell", "icon": "terminal", "label": "执行命令"} in body["groups"] and body["file"] == str(s.config.hooks_file)

    deny = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "测试拦下"}})
    argv, log = script(tmp_path / "hooks", "deny", stdout=deny)
    hook = form([*argv, "--token=abcdefghijklmnopqrst"], matcher="shell", timeout=5, runsIn=["chat", "task"])
    status, body = s.browser.post("/api/hooks/save", {"original": None, "name": " no-rm ", "hook": hook})
    assert status == 200 and body["name"] == "no-rm"
    assert body["hook"]["args"][-1] == "--token=••••"  # 页面上看到的参数遮住了像密钥的部分
    saved = json.loads(s.config.hooks_file.read_text(encoding="utf-8"))["hooks"]["no-rm"]
    assert saved["args"][-1] == "--token=abcdefghijklmnopqrst" and saved["runsIn"] == ["chat", "task"] and saved["timeout"] == 5

    # 编辑：遮住的那一项原样交回来，表示没改
    edited = form([*argv, "--token=••••"], matcher="shell|files", timeout=5)
    status, body = s.browser.post("/api/hooks/save", {"original": "no-rm", "name": "guard", "hook": edited})
    assert status == 200
    saved = json.loads(s.config.hooks_file.read_text(encoding="utf-8"))["hooks"]
    assert list(saved) == ["guard"] and saved["guard"]["args"][-1] == "--token=abcdefghijklmnopqrst"

    # 不合格的说清楚为什么，不保存
    status, body = s.browser.post("/api/hooks/save", {"original": None, "name": "leak", "hook": form(["python3", "$ANTHROPIC_API_KEY"])})
    assert status == 409 and "ANTHROPIC_API_KEY" in body["error"]
    status, body = s.browser.post("/api/hooks/save", {"original": None, "name": "spaces", "hook": form(["python3 check.py"])})
    assert status == 409 and "shell" in body["error"]
    assert s.browser.post("/api/hooks/save", {"original": None, "name": "x", "hook": "not a form"})[0] == 409

    # 关掉、试运行（停用着的也能试）
    assert s.browser.post("/api/hooks/enable", {"name": "guard", "enabled": False})[1]["hook"]["enabled"] is False
    status, body = s.browser.post("/api/hooks/test", {"name": "guard"})
    assert status == 200
    run = body["run"]
    assert run["exit"] == 0 and run["result"] == "blocked" and "测试拦下" in run["verdict"]["text"]
    assert run["verdict"]["key"].startswith("会拦下这次调用")  # 带着原文：换语言时页面就地重译
    (seen,) = calls_of(log)
    assert seen["payload"]["test"] is True and seen["payload"]["tool_name"] == "run_command" and seen["payload"]["hook_event_name"] == "PreToolUse"
    # 你改过的示例：照着它跑（事件名、钩子名还是它自己的）
    status, body = s.browser.post("/api/hooks/test", {"name": "guard", "payload": {"tool_input": {"command": "rm -rf /"}, "hook_event_name": "Stop"}})
    assert status == 200 and calls_of(log)[-1]["payload"]["tool_input"] == {"command": "rm -rf /"}
    assert calls_of(log)[-1]["payload"]["hook_event_name"] == "PreToolUse"
    assert s.browser.post("/api/hooks/test", {"name": "guard", "payload": [1, 2]})[0] == 400

    status, body = s.browser.post("/api/hooks/list")
    (row,) = body["hooks"]
    assert row["last"]["result"] == "blocked" and row["last"]["test"] is True and row["sample"]["tool_group"] == "shell"
    assert latest_state(s.browser)["hooks"]["count"] == 1

    # 在访达中显示：路径由程序算
    assert s.browser.post("/api/hooks/reveal", {}) == (200, {"ok": True, "path": str(s.config.hooks_file)})
    assert s.revealed[-1] == s.config.hooks_file
    assert s.browser.post("/api/hooks/delete", {"name": "guard"})[0] == 200
    assert s.browser.post("/api/hooks/delete", {"name": "guard"})[0] == 409
    assert json.loads(s.config.hooks_file.read_text(encoding="utf-8"))["hooks"] == {}


def test_a_hook_saved_on_the_page_runs_in_the_conversation(serve, tmp_path):
    s = serve(reply(text("不应该被调用")))
    argv, log = script(tmp_path / "hooks", "gate", stdout=json.dumps({"decision": "block", "reason": "页面上加的钩子"}))
    assert s.browser.post("/api/hooks/save", {"original": None, "name": "gate", "hook": form(argv, "UserPromptSubmit")})[0] == 200
    assert s.browser.post("/api/message", {"text": "你好"})[0] == 200
    s.browser.wait_for("turn_end")
    notices = [event for event in s.ui.events_after(0, timeout=0) if event["type"] == "notice"]
    assert any(event.get("key", "").startswith("这条消息被你的钩子") for event in notices)
    trace = next(event for event in s.ui.events_after(0, timeout=0) if event["type"] == "trace")
    assert trace["ended"] == "blocked"
    assert s.api.requests == [] and len(calls_of(log)) == 1


def test_the_command_line_manages_the_same_file(make_config, tmp_path, monkeypatch, capsys):
    config = make_config()
    monkeypatch.setenv("HARNESS_DATA_DIR", str(config.data_dir))
    argv, log = script(tmp_path / "hooks", "cli", stderr="命令行拦下", code=2)
    assert main(["hooks", "add", "cli", "--event", "PreToolUse", "--matcher", "shell", "--on-failure", "block", "--", *argv]) == 0
    saved = json.loads(config.hooks_file.read_text(encoding="utf-8"))["hooks"]["cli"]
    assert saved == {"event": "PreToolUse", "matcher": "shell", "command": sys.executable, "args": argv[1:], "enabled": True, "onFailure": "block"}
    assert main(["hooks", "disable", "cli"]) == 0
    assert json.loads(config.hooks_file.read_text(encoding="utf-8"))["hooks"]["cli"]["enabled"] is False
    capsys.readouterr()
    assert main(["hooks", "list"]) == 0
    listed = capsys.readouterr().out
    assert "○ cli  PreToolUse [shell]" in listed
    assert main(["hooks", "test", "cli"]) == 0
    tested = capsys.readouterr().out
    assert "命令行拦下" in tested and "会拦下这次调用" in tested and len(calls_of(log)) == 1
    # 引用模型密钥的、没写程序的，都不加
    assert main(["hooks", "add", "leak", "--event", "Stop", "--", "python3", "${OPENAI_API_KEY}"]) == 1
    assert main(["hooks", "add", "empty", "--event", "Stop"]) == 2
    assert main(["hooks", "remove", "cli"]) == 0
    assert main(["hooks", "remove", "cli"]) == 1
