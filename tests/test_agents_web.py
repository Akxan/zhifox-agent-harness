"""网页里"扩展 → Agent"一页背后的接口（/api/agent/……）、用一个 Agent 开始新对话（/api/new），
以及子 Agent 在页面上的样子：事件流里的 subagent 事件、确认、停止、保存和重放。

和 test_webui.py 一样：真实的 HTTP 服务器，测试代码扮演浏览器，模型是本地的假 API。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import test_webui
from fake_api import reply, text, tool_use
from test_webui import latest_state, saved_file

from harness.agents import examples

# 借用 test_webui 的 fixture：起一个网页服务器，后面连着假的 API，并让"浏览器"连上去
serve = test_webui.serve

FAKE_KEY = "sk-ant-fake-agents-web-key-0123456789"


def agent_file(name: str = "researcher", description: str = "查资料。", body: str = "你是研究员。", **fields: str) -> str:
    lines = [f"name: {name}", f"description: {description}", *(f"{key}: {value}" for key, value in fields.items())]
    return "---\n" + "".join(line + "\n" for line in lines) + "---\n\n" + body + "\n"


def wait_state(s, check, timeout: float = 10) -> dict:
    """等到页面收到的最新状态满足 check。"""
    deadline = time.monotonic() + timeout
    while True:
        state = latest_state(s.browser)
        if check(state):
            return state
        assert time.monotonic() < deadline, "状态一直不对"
        time.sleep(0.05)


def names(state: dict) -> list[str]:
    return [agent["name"] for agent in state["agents"]]


def delegate(name: str = "资料研究员", task: str = "查一下 X", id: str = "t1") -> dict:
    return reply(tool_use(id, "agent", {"name": name, "task": task}), stop_reason="tool_use")


def add_example(s, example_id: str) -> str:
    status, body = s.browser.post("/api/agent/example", {"id": example_id})
    assert status == 200, body
    return body["name"]


def steps(events: list[dict]) -> list[str]:
    return [event["step"] for event in events if event["type"] == "subagent"]


# ---- 列表、查看、新建、修改、删除 ---------------------------------------------------


def test_creating_reading_saving_and_deleting(serve):
    s = serve()
    assert latest_state(s.browser)["agents"] == [] and latest_state(s.browser)["persona"] is None
    status, body = s.browser.post("/api/agent/create", {"text": agent_file(tools="Read, KillShell", color="blue")})
    assert (status, body) == (200, {"ok": True, "name": "researcher"})
    state = wait_state(s, lambda state: names(state) == ["researcher"])
    (row,) = state["agents"]
    assert row["color"] == "blue" and row["tools"] == {"all": False, "count": 1} and row["problems"] == []
    assert row["warnings"][0]["key"] == "智狐没有这些工具，会忽略它们：{names}"  # 发给页面的是原文和值，换语言时照着重译

    # 同名的不能再建一个；不合格的说清楚每一条问题
    status, body = s.browser.post("/api/agent/create", {"text": agent_file()})
    assert status == 409 and "已经有一个叫 researcher" in body["error"]
    status, body = s.browser.post("/api/agent/create", {"text": "---\nname: x\n---\n"})
    assert status == 409 and [problem["key"] for problem in body["problems"]] == ["缺少 {key}。", "正文是空的：在第二行 --- 下面写这个 Agent 的设定和做事的方法。"]

    status, opened = s.browser.post("/api/agent/read", {"name": "researcher"})
    assert status == 200 and opened["text"] == agent_file(tools="Read, KillShell", color="blue")
    assert opened["fields"]["tools"] == {"all": False, "picked": ["Read"], "unknown": ["KillShell"]}
    assert opened["path"].endswith("researcher.md")

    # 打开之后在别处被改过了：不保存，告诉页面"再点一次就覆盖"
    status, body = s.browser.post("/api/agent/save", {"name": "researcher", "text": agent_file(description="新的"), "modified": opened["modified"] - 5})
    assert status == 409 and body["conflict"] is True
    status, body = s.browser.post("/api/agent/save", {"name": "researcher", "text": agent_file(description="新的"), "modified": 0, "force": True})
    assert (status, body["name"]) == (200, "researcher")
    # 改名：文件跟着改
    modified = s.browser.post("/api/agent/read", {"name": "researcher"})[1]["modified"]
    status, body = s.browser.post("/api/agent/save", {"name": "researcher", "text": agent_file("研究员"), "modified": modified})
    assert (status, body["name"]) == (200, "研究员")
    wait_state(s, lambda state: names(state) == ["研究员"])
    assert (s.config.agents_dir / "研究员.md").exists() and not (s.config.agents_dir / "researcher.md").exists()

    assert s.browser.post("/api/agent/delete", {"name": "研究员"}) == (200, {"ok": True})
    wait_state(s, lambda state: state["agents"] == [])
    status, body = s.browser.post("/api/agent/delete", {"name": "研究员"})
    assert status == 409 and "不存在" in body["error"]


def test_parse_and_compose_switch_between_the_form_and_the_text(serve):
    s = serve()
    base = agent_file(tools="Grep, Glob", color="green", permissionMode="plan")
    status, body = s.browser.post("/api/agent/parse", {"text": base})
    assert status == 200 and body["fields"]["tools"]["picked"] == ["Read"] and body["fields"]["color"] == "green"
    assert s.browser.post("/api/agent/parse", {"text": "坏的"}) == (200, {"ok": True, "fields": None})
    fields = {**body["fields"], "tools": ["Read"], "body": "新的正文"}
    status, body = s.browser.post("/api/agent/compose", {"fields": fields, "base": base})
    assert status == 200 and "tools: Grep, Glob" in body["text"] and "permissionMode: plan" in body["text"] and "新的正文" in body["text"]
    status, body = s.browser.post("/api/agent/compose", {"fields": {**fields, "tools": None, "color": ""}})
    assert status == 200 and body["text"] == "---\nname: researcher\ndescription: 查资料。\n---\n\n新的正文\n"


def test_examples_options_and_reveal(serve):
    s = serve()
    status, options = s.browser.post("/api/agent/options")
    assert status == 200 and options["builtin"][:3] == ["Read", "Edit", "Bash"] and "WebSearch" in options["builtin"]
    assert options["colors"] == ["red", "orange", "yellow", "green", "cyan", "blue", "purple", "pink"]
    assert [example["id"] for example in options["examples"]] == ["researcher", "reviewer", "writer"]
    assert not any(example["exists"] for example in options["examples"]) and options["mcp"] == []

    for example in examples.EXAMPLES:
        assert add_example(s, example.id) == example.name
    wait_state(s, lambda state: sorted(names(state)) == sorted(example.name for example in examples.EXAMPLES))
    assert all(example["exists"] for example in s.browser.post("/api/agent/options")[1]["examples"])
    status, body = s.browser.post("/api/agent/example", {"id": "writer"})
    assert status == 409 and "已经有一个叫 写作助手" in body["error"]
    assert s.browser.post("/api/agent/example", {"id": "nope"})[0] == 400

    # 在文件管理器里显示：路径由服务器算，页面只给名字
    status, body = s.browser.post("/api/agent/reveal", {"name": "写作助手"})
    assert status == 200 and Path(body["path"]) == s.config.agents_dir / "写作助手.md" and s.revealed[-1] == s.config.agents_dir / "写作助手.md"
    status, body = s.browser.post("/api/agent/reveal", {})
    assert status == 200 and s.revealed[-1] == s.config.agents_dir
    for bad in ("../写作助手", "missing", "/etc/passwd", ".hidden"):
        assert s.browser.post("/api/agent/reveal", {"name": bad})[0] == 409, bad
    assert len(s.revealed) == 2


def test_refresh_only_publishes_when_the_folder_changed(serve):
    s = serve()
    before = len(s.browser.of_type("state"))
    assert s.browser.post("/api/agent/refresh") == (200, {"ok": True})
    time.sleep(0.2)
    assert len(s.browser.of_type("state")) == before  # 没变：不发
    s.config.agents_dir.mkdir(parents=True, exist_ok=True)
    (s.config.agents_dir / "x.md").write_text(agent_file("x"), encoding="utf-8")  # 在访达里放进来一个
    assert s.browser.post("/api/agent/refresh") == (200, {"ok": True})
    wait_state(s, lambda state: names(state) == ["x"])


def test_importing_from_claude_code(serve):
    s = serve()
    folder = Path.home() / ".claude" / "agents"
    folder.mkdir(parents=True)
    (folder / "code-reviewer.md").write_text(agent_file("code-reviewer", "审查代码。", tools="Read, Grep"), encoding="utf-8")
    (s.config.workspace / ".claude" / "agents").mkdir(parents=True)
    (s.config.workspace / ".claude" / "agents" / "tester.md").write_text(agent_file("tester", "写测试。"), encoding="utf-8")
    status, body = s.browser.post("/api/agent/claude")
    assert status == 200 and body["places"] == ["~/.claude/agents", ".claude/agents"]
    keys = [candidate["key"] for candidate in body["candidates"]]
    assert keys == ["~/.claude/agents/code-reviewer.md", ".claude/agents/tester.md"]
    picks = [{"key": keys[0], "name": "code-reviewer"}, {"key": keys[1], "name": "写测试的"}]
    status, body = s.browser.post("/api/agent/import", {"picks": picks})
    assert status == 200 and [(result["ok"], result["name"]) for result in body["results"]] == [(True, "code-reviewer"), (True, "写测试的")]
    wait_state(s, lambda state: sorted(names(state)) == sorted(["code-reviewer", "写测试的"]))
    assert s.browser.post("/api/agent/import", {"picks": "nope"})[0] == 409


def test_wrong_parameters_are_400(serve):
    s = serve()
    for path, data in (
        ("/api/agent/parse", {"text": 5}),
        ("/api/agent/compose", {"fields": "x"}),
        ("/api/agent/compose", {"fields": {"name": 1, "description": "", "model": "", "color": "", "body": ""}}),
        ("/api/agent/compose", {"fields": {"name": "a", "description": "", "model": "", "color": "", "body": "", "tools": "Read"}}),
        ("/api/agent/compose", {"fields": {"name": "a", "description": "", "model": "", "color": "", "body": ""}, "base": 5}),
        ("/api/agent/create", {"text": None}),
        ("/api/agent/save", {"name": "x", "text": "y", "modified": "yesterday"}),
        ("/api/agent/save", {"name": "x", "text": "y", "modified": 1, "force": "yes"}),
        ("/api/agent/save", {"name": "x", "text": True, "modified": 1}),
        ("/api/agent/example", {"id": 5}),
        ("/api/new", {"agent": 5}),
        ("/api/new", {"agent": ""}),
    ):
        status, body = s.browser.post(path, data)
        assert status == 400, (path, data, body)
    assert s.browser.post("/api/agent/nope")[0] == 404


# ---- 用一个 Agent 开始新对话 -------------------------------------------------------


def test_starting_a_conversation_with_an_agent(serve):
    s = serve(reply(text("润色好了。")))
    add_example(s, "writer")
    status, body = s.browser.post("/api/new", {"agent": "写作助手"})
    assert (status, body) == (200, {"ok": True, "warning": ""})
    state = wait_state(s, lambda state: state["persona"] is not None)
    assert state["persona"] == {"name": "写作助手", "color": "green", "missing": False}
    items = state["capabilities"]["items"]
    assert [item["id"] for item in items if item["status"] == "on"] == []  # tools: []，信息栏里一项都用不了
    assert all(item["tools"] == [] for item in items) and state["capabilities"]["notes"]

    assert s.browser.post("/api/message", {"text": "帮我润色"})[0] == 200
    s.browser.wait_for("turn_end")
    request = s.api.requests[0]
    assert request["system"][0]["text"].startswith("你是一个写作助手")
    state = latest_state(s.browser)
    conversation_id = state["conversation"]
    assert next(item for item in state["conversations"] if item["id"] == conversation_id)["agent"] == "写作助手"
    assert saved_file(s.config, conversation_id)["agent"] == "写作助手"

    # 回到普通的对话
    assert s.browser.post("/api/new", {})[0] == 200
    wait_state(s, lambda state: state["persona"] is None and state["conversation"] != conversation_id)


def test_starting_with_a_missing_or_unusable_agent_is_explained(serve):
    s = serve()
    status, body = s.browser.post("/api/new", {"agent": "不存在的"})
    assert status == 409 and "不存在" in body["error"]
    s.config.agents_dir.mkdir(parents=True, exist_ok=True)
    (s.config.agents_dir / "坏的.md").write_text("---\nname: 坏的\n---\n", encoding="utf-8")
    status, body = s.browser.post("/api/new", {"agent": "坏的"})
    assert status == 409 and body["error"]
    # Agent 用的是另一家的模型、那一家还没有密钥：先说清楚，不换
    s.browser.post("/api/agent/create", {"text": agent_file("深度", model="deepseek/deepseek-flash")})
    status, body = s.browser.post("/api/new", {"agent": "深度"})
    assert status == 409 and "DeepSeek" in body["error"] and "密钥" in body["error"]
    assert latest_state(s.browser)["persona"] is None


# ---- 子 Agent 在页面上 ---------------------------------------------------------------


def test_a_delegation_shows_up_as_subagent_events_and_is_saved(serve):
    s = serve(
        delegate(),
        reply(tool_use("s1", "str_replace_based_edit_tool", {"command": "view", "path": "."}), stop_reason="tool_use"),
        reply(text("X 是一个东西。")),
        reply(text("研究员说 X 是一个东西。")),
    )
    add_example(s, "researcher")
    assert s.browser.post("/api/message", {"text": "交给研究员"})[0] == 200
    s.browser.wait_for("turn_end")
    events = [event for event in s.browser.events if event["type"] == "subagent"]
    assert steps(events)[0] == "start" and steps(events)[-1] == "done"
    assert {"tool_call", "tool_result", "activity"} <= set(steps(events))
    start = events[0]
    assert start["agent"] == "资料研究员" and start["color"] == "blue" and start["task"] == "查一下 X"
    done = events[-1]
    assert done["status"] == "done" and done["answer"] == "X 是一个东西。" and done["steps"] == 2 and done["calls"] == 1
    assert all(event.get("transient") for event in events if event["step"] == "activity")
    # 主助手的那张工具卡片：agent 工具的调用和结果都在
    call = next(event for event in s.browser.events if event["type"] == "tool_call" and event["name"] == "agent")
    assert call["summary"].startswith("agent 资料研究员 · 查一下 X")

    # 存进会话的：进展都在，"在想 / 在写"这种只是此刻样子的不存
    conversation_id = latest_state(s.browser)["conversation"]
    saved = [event for event in saved_file(s.config, conversation_id)["events"] if event["type"] == "subagent"]
    assert steps(saved) == [step for step in steps(events) if step != "activity"]
    # 重新打开：照样重放出来
    s.browser.post("/api/new", {})
    replayed, _ = test_webui.open_conversation(s, conversation_id)
    assert steps(replayed) == steps(saved)


def test_subagent_confirmations_name_the_agent(serve):
    s = serve(
        delegate("代码审查员"),
        reply(tool_use("s1", "run_command", {"command": "echo hi"}), stop_reason="tool_use"),
        reply(text("运行过了。")),
        reply(text("好")),
    )
    status, _ = s.browser.post("/api/agent/create", {"text": examples.find("reviewer").text.replace("tools: Read", "tools: Read, Bash")})
    assert status == 200
    assert s.browser.post("/api/message", {"text": "交给审查员"})[0] == 200
    confirm = s.browser.wait_for("confirm")
    assert confirm["agent"] == "代码审查员" and "echo hi" in confirm["description"]
    assert s.browser.post("/api/confirm", {"id": confirm["id"], "allow": True})[0] == 200
    s.browser.wait_for("turn_end")
    done = [event for event in s.browser.events if event["type"] == "subagent"][-1]
    assert done["status"] == "done" and done["answer"] == "运行过了。"


def test_stop_stops_a_running_subagent(serve):
    s = serve(
        delegate("代码审查员"),
        reply(tool_use("s1", "run_command", {"command": "echo hi"}), stop_reason="tool_use"),
        reply(text("不该走到这里")),
    )
    s.browser.post("/api/agent/create", {"text": examples.find("reviewer").text.replace("tools: Read", "tools: Bash")})
    registered = len(s.ui._stop_callbacks)
    assert s.browser.post("/api/message", {"text": "交给审查员"})[0] == 200
    s.browser.wait_for("confirm")  # 子 Agent 停在一个确认上
    assert len(s.ui._stop_callbacks) == registered + 1  # 它运行的这一会儿，"停止"也会通知它
    assert s.browser.post("/api/stop") == (200, {"ok": True})
    s.browser.wait_for("turn_end")
    done = [event for event in s.browser.events if event["type"] == "subagent"][-1]
    assert done["step"] == "done" and done["status"] == "stopped"
    assert s.browser.of_type("confirm_done")[-1]["stopped"] is True
    assert len(s.api.requests) == 2
    # 主助手的历史里，这次 agent 调用有一个"被中断"的结果；下一轮照常能发
    results = [block for block in s.app.agent.messages[-1]["content"] if block.get("type") == "tool_result"]
    assert results and "用户中断了操作" in results[0]["content"]
    assert len(s.ui._stop_callbacks) == registered  # 停下之后就取消了登记：一直留着的话，每次委派都多一个


def test_api_keys_never_reach_the_agent_answers(serve, monkeypatch):
    s = serve(delegate(), reply(text("好了")), reply(text("转告")))
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    add_example(s, "researcher")
    answers = [
        s.browser.post("/api/agent/options")[1],
        s.browser.post("/api/agent/read", {"name": "资料研究员"})[1],
        s.browser.post("/api/agent/claude")[1],
    ]
    assert s.browser.post("/api/message", {"text": "交给研究员"})[0] == 200
    s.browser.wait_for("turn_end")
    everything = json.dumps(answers, ensure_ascii=False) + json.dumps(s.browser.events, ensure_ascii=False)
    assert FAKE_KEY not in everything and FAKE_KEY[-12:] not in everything


def test_reopening_a_persona_conversation_restores_it(serve):
    s = serve(reply(text("好")))
    add_example(s, "writer")
    s.browser.post("/api/new", {"agent": "写作助手"})
    wait_state(s, lambda state: state["persona"] is not None)
    s.browser.post("/api/message", {"text": "第一句"})
    s.browser.wait_for("turn_end")
    conversation_id = latest_state(s.browser)["conversation"]
    s.browser.post("/api/new", {})
    wait_state(s, lambda state: state["persona"] is None)
    test_webui.open_conversation(s, conversation_id)
    state = wait_state(s, lambda state: state["persona"] is not None)
    assert state["persona"] == {"name": "写作助手", "color": "green", "missing": False}

    # Agent 被删掉之后再打开：退回到普通的对话，页面上说一声；顶上还看得出这段是用它开始的
    s.browser.post("/api/new", {})
    wait_state(s, lambda state: state["persona"] is None)
    assert s.browser.post("/api/agent/delete", {"name": "写作助手"})[0] == 200
    test_webui.open_conversation(s, conversation_id)
    state = wait_state(s, lambda state: state["persona"] is not None)
    assert state["persona"] == {"name": "写作助手", "color": "", "missing": True}
    hints = [event for event in s.browser.of_type("notice") if str(event.get("key", "")).startswith("这段会话是用 Agent")]
    assert hints and hints[-1]["values"]["name"] == "写作助手"
