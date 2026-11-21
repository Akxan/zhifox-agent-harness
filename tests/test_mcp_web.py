"""网页界面里 MCP 这一页背后的接口：状态里的概况、添加和编辑、删除、启用停用、重启、"问不问你"、导入、粘贴。

和 test_webui.py 一样：真实的 HTTP 服务器，测试代码扮演浏览器。MCP 服务器是 tests/fake_mcp.py。
"""

from __future__ import annotations

import json
import sys
import time

import test_mcp
import test_webui
from fake_api import reply, text, tool_use
from fake_mcp import FakeMcpHttp
from test_mcp import FAKE, stdio, wait_until, write_config
from test_webui import latest_state, request

# 借用另外两个文件里的 fixture（pytest 按模块里的名字找 fixture）：serve 起一个网页服务器，
# close_managers 在每个测试结束时关掉启动过的 MCP 服务器
serve = test_webui.serve
close_managers = test_mcp.close_managers

OK = (200, {"ok": True, "warning": ""})
SECRET_ENV = "super-secret-env-value-123456"
SECRET_TOKEN = "bearer-token-abcdefghijkl-9876"


def mcp_state(s, check, timeout: float = 15) -> dict:
    """等到页面收到的最新状态里，MCP 那一部分满足 check，返回它。"""
    deadline = time.monotonic() + timeout
    while True:
        state = latest_state(s.browser)["mcp"]
        if check(state):
            return state
        assert time.monotonic() < deadline, f"MCP 的状态一直不对：{state}"
        time.sleep(0.05)


def row(state: dict, name: str) -> dict | None:
    return next((server for server in state["servers"] if server["name"] == name), None)


def ready(name: str):
    return lambda state: (row(state, name) or {}).get("status") == "ready"


def new_stdio(name: str, *args: str, **extra) -> dict:
    return {"name": name, "transport": "stdio", "command": sys.executable, "args": [str(FAKE), *args], **extra}


def test_state_shows_the_servers_but_never_their_secrets(serve, make_config):
    web = FakeMcpHttp("modern", token=SECRET_TOKEN).start()
    try:
        write_config(make_config().mcp_file, {
            "local": stdio(env={"MY_TOKEN": SECRET_ENV}),
            "remote": {"type": "http", "url": web.url, "headers": {"Authorization": f"Bearer {SECRET_TOKEN}"}},
        })
        s = serve()
        state = mcp_state(s, lambda state: all(server["status"] == "ready" for server in state["servers"]))
        local = row(state, "local")
        assert local["tools"] == 8 and local["config"]["transport"] == "stdio"
        assert local["config"]["env"] == [{"name": "MY_TOKEN", "hint": "…3456"}]
        assert row(state, "remote")["config"]["headers"] == [{"name": "Authorization", "hint": "…9876"}]
        assert state["started"] is False and state["problem"] is None
        # 对话还没开始：信息栏里先列出"发第一条消息会用上的"工具
        local_row = next(item for item in latest_state(s.browser)["capabilities"]["items"] if item["id"] == "mcp:local")
        assert local_row["status"] == "on" and "mcp__local__echo" in local_row["tools"]

        status, detail = s.browser.post("/api/mcp/detail", {"name": "remote"})
        assert status == 200 and [tool["name"] for tool in detail["server"]["tools"]][:2] == ["echo", "add"]
        assert detail["server"]["tools"][0]["exposed"] == "mcp__remote__echo"
        everything = json.dumps(s.browser.events, ensure_ascii=False) + json.dumps(detail, ensure_ascii=False)
        assert SECRET_ENV not in everything and SECRET_TOKEN not in everything
    finally:
        web.stop()


def test_adding_editing_and_renaming_servers(serve):
    s = serve()
    assert s.browser.post("/api/mcp/save", {"original": None, "server": new_stdio("one", env=[{"name": "TOKEN", "value": SECRET_ENV}])}) == OK
    mcp_state(s, ready("one"))
    saved = json.loads(s.config.mcp_file.read_text())["mcpServers"]["one"]
    assert saved["env"] == {"TOKEN": SECRET_ENV} and saved["args"][-1].endswith("fake_mcp.py")

    assert s.browser.post("/api/mcp/approval", {"name": "one", "tool": "echo", "approval": "auto"}) == OK
    # 改名、不重新填密钥：密钥和"问不问你"的设置都跟着留下
    edit = new_stdio("renamed", env=[{"name": "TOKEN", "keep": "TOKEN"}])
    assert s.browser.post("/api/mcp/save", {"original": "one", "server": edit}) == OK
    state = mcp_state(s, ready("renamed"))
    assert row(state, "one") is None
    saved = json.loads(s.config.mcp_file.read_text())["mcpServers"]
    assert list(saved) == ["renamed"] and saved["renamed"]["env"] == {"TOKEN": SECRET_ENV}
    assert saved["renamed"]["approval"] == {"default": "ask", "tools": {"echo": "auto"}}

    status, body = s.browser.post("/api/mcp/save", {"original": None, "server": {"name": "web", "transport": "http", "url": "ftp://nope"}})
    assert status == 409 and "http://" in body["error"]
    status, body = s.browser.post("/api/mcp/save", {"original": None, "server": new_stdio("renamed")})
    assert status == 409 and "已经有一个" in body["error"]
    status, body = s.browser.post("/api/mcp/save", {"original": None, "server": {"name": "bad name!", "transport": "stdio", "command": "x"}})
    assert status == 409 and "名字" in body["error"]
    assert s.browser.post("/api/mcp/save", {"original": None, "server": "nope"})[0] == 400
    assert s.browser.post("/api/mcp/save", {"original": 5, "server": {}})[0] == 400


def test_enable_restart_and_delete(serve, make_config):
    write_config(make_config().mcp_file, {"fake": stdio()})
    s = serve()
    mcp_state(s, ready("fake"))
    process = s.app.mcp._servers["fake"].transport._process

    assert s.browser.post("/api/mcp/enable", {"name": "fake", "enabled": False}) == OK
    mcp_state(s, lambda state: row(state, "fake")["status"] == "disabled" and row(state, "fake")["enabled"] is False)
    wait_until(lambda: process.poll() is not None, what="进程被关掉")
    assert json.loads(s.config.mcp_file.read_text())["mcpServers"]["fake"]["enabled"] is False

    assert s.browser.post("/api/mcp/enable", {"name": "fake", "enabled": True}) == OK
    mcp_state(s, ready("fake"))
    revision = row(latest_state(s.browser)["mcp"], "fake")["revision"]
    assert s.browser.post("/api/mcp/restart", {"name": "fake"}) == OK
    mcp_state(s, lambda state: ready("fake")(state) and row(state, "fake")["revision"] > revision)

    assert s.browser.post("/api/mcp/delete", {"name": "fake"}) == OK
    mcp_state(s, lambda state: state["servers"] == [])
    assert json.loads(s.config.mcp_file.read_text())["mcpServers"] == {}
    status, body = s.browser.post("/api/mcp/delete", {"name": "fake"})
    assert status == 409 and "没有找到" in body["error"]
    assert s.browser.post("/api/mcp/detail", {"name": "fake"})[0] == 409
    assert s.browser.post("/api/mcp/enable", {"name": "fake", "enabled": "yes"})[0] == 400
    assert s.browser.post("/api/mcp/approval", {"name": "fake", "approval": "sometimes"})[0] == 400
    assert s.browser.post("/api/mcp/restart", {})[0] == 400
    assert s.browser.post("/api/mcp/whatever", {"name": "x"})[0] == 400


def test_approval_changes_show_up_in_the_detail(serve, make_config):
    write_config(make_config().mcp_file, {"fake": stdio()})
    s = serve()
    mcp_state(s, ready("fake"))
    assert s.browser.post("/api/mcp/approval", {"name": "fake", "tool": None, "approval": "auto"}) == OK
    assert s.browser.post("/api/mcp/approval", {"name": "fake", "tool": "crash", "approval": "ask"}) == OK
    state = mcp_state(s, lambda state: row(state, "fake")["approval"] == "auto")
    assert row(state, "fake")["approval"] == "auto"
    tools = {tool["name"]: tool for tool in s.browser.post("/api/mcp/detail", {"name": "fake"})[1]["server"]["tools"]}
    assert tools["echo"]["approval"] == "auto" and tools["crash"]["approval"] == "ask"
    assert tools["picture"]["read_only"] is True and tools["crash"]["destructive"] is True


def test_importing_from_claude_code(serve):
    s = serve()
    from pathlib import Path

    (Path.home()).mkdir(parents=True, exist_ok=True)
    (Path.home() / ".claude.json").write_text(json.dumps({"mcpServers": {"from-code": {"type": "stdio", "command": sys.executable, "args": [str(FAKE)], "env": {"K": SECRET_ENV}}}}))
    status, body = s.browser.post("/api/mcp/scan", {})
    assert status == 200
    sources = {source["id"]: source for source in body["sources"]}
    assert sources["claude-code-user"]["found"] is True and sources["claude-desktop"]["found"] is False
    assert sources["claude-code-user"]["label"] == "Claude Code（用户级）"
    assert [item["name"] for item in sources["claude-code-user"]["servers"]] == ["from-code"]
    assert SECRET_ENV not in json.dumps(body)

    status, body = s.browser.post("/api/mcp/import", {"picks": [{"source": "claude-code-user", "name": "from-code"}, {"source": "nowhere", "name": "x"}]})
    assert status == 200 and body["added"] == ["from-code"] and len(body["skipped"]) == 1
    mcp_state(s, ready("from-code"))
    assert json.loads(s.config.mcp_file.read_text())["mcpServers"]["from-code"]["env"] == {"K": SECRET_ENV}
    assert s.browser.post("/api/mcp/import", {"picks": []})[0] == 400
    assert s.browser.post("/api/mcp/import", {"picks": [{"source": 1}]})[0] == 400


def test_pasting_json_previews_then_adds(serve):
    s = serve()
    pasted = json.dumps({"mcpServers": {"pasted": {"command": sys.executable, "args": [str(FAKE)], "env": {"K": SECRET_ENV}}, "other": {"command": "x"}}})
    status, body = s.browser.post("/api/mcp/paste", {"text": pasted, "names": None})
    assert status == 200 and [item["name"] for item in body["servers"]] == ["pasted", "other"]
    assert SECRET_ENV not in json.dumps(body)
    assert s.app.mcp.config.servers() == {}  # 只是看看，什么都没加
    status, body = s.browser.post("/api/mcp/paste", {"text": pasted, "names": ["pasted"]})
    assert status == 200 and body["added"] == ["pasted"]
    mcp_state(s, ready("pasted"))
    status, body = s.browser.post("/api/mcp/paste", {"text": "not json", "names": None})
    assert status == 409 and "JSON" in body["error"]
    assert s.browser.post("/api/mcp/paste", {"text": 5})[0] == 400


def test_a_hand_edited_config_file_is_picked_up_on_refresh(serve):
    s = serve()
    write_config(s.config.mcp_file, {"by-hand": stdio()})
    assert s.browser.post("/api/mcp/refresh", {}) == (200, {"ok": True})
    mcp_state(s, ready("by-hand"))


def test_reveal_shows_the_config_file(serve):
    s = serve()
    status, body = s.browser.post("/api/mcp/reveal", {"path": "/etc/passwd"})  # 请求里给什么都不看
    assert status == 200 and s.revealed[-1] == s.config.data_dir  # 还没有配置文件：打开数据目录
    write_config(s.config.mcp_file, {})
    status, body = s.browser.post("/api/mcp/reveal", {})
    assert status == 200 and s.revealed[-1] == s.config.mcp_file and body["path"] == str(s.config.mcp_file)


def test_a_broken_config_file_is_reported_and_left_alone(serve, make_config):
    path = make_config().mcp_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ broken", encoding="utf-8")
    s = serve()
    state = latest_state(s.browser)["mcp"]
    assert state["problem"] and "读不出来" in state["problem"]["text"] and state["problem"]["key"]
    status, body = s.browser.post("/api/mcp/save", {"original": None, "server": new_stdio("x")})
    assert status == 409 and "读不出来" in body["error"]
    assert path.read_text() == "{ broken"


def test_mcp_endpoints_need_the_token_and_our_origin(serve):
    s = serve()
    host, token = s.browser.host, s.browser.token
    form = {"original": None, "server": new_stdio("evil")}
    assert request(host, "POST", "/api/mcp/save", form)[0] == 403
    assert request(host, "POST", "/api/mcp/save", form, {"X-Harness-Token": token, "Origin": "http://evil.example"})[0] == 403
    assert request(host, "POST", "/api/mcp/scan", {}, {"X-Harness-Token": "wrong"})[0] == 403
    assert not s.config.mcp_file.exists()


def test_stop_cancels_a_running_mcp_call(serve, make_config):
    write_config(make_config().mcp_file, {"fake": stdio(approval={"default": "auto"})})
    s = serve(reply(tool_use("toolu_1", "mcp__fake__slow", {"seconds": 20}), stop_reason="tool_use"), reply(text("不会用到")))
    mcp_state(s, ready("fake"))
    assert s.browser.post("/api/message", {"text": "慢慢来"})[0] == 200
    s.browser.wait_for("auto_allowed")  # 设成了"不用问"：没有确认卡片，直接开始调用
    time.sleep(0.3)
    started = time.monotonic()
    assert s.browser.post("/api/stop", {}) == (200, {"ok": True})
    result = s.browser.wait_for("tool_result")
    assert result["is_error"] is True and "用户中断了这次调用" in result["text"]
    s.browser.wait_for("turn_end")
    assert time.monotonic() - started < 5
    state = latest_state(s.browser)
    assert state["mcp"]["started"] is True and row(state["mcp"], "fake")["in_use"] == 8
