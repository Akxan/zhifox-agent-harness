"""浏览器的工具在 agent 循环里怎么走：发给模型的说明书、调用怎么分到浏览器、结果怎么交回去。

背后的浏览器换成 FakeSession（只记下要做的动作，按预设回答），模型是本地的假 API（fake_api.py、fake_openai.py）：
测的是"说明书和结果的格式对不对、规矩守没守"，不是 Chrome 本身（那个见 test_browser_live.py）。

几条要守住的规矩：
  - Claude 的浏览器工具包在请求里只占一项，它的"替身"（browser_* 函数）不发；不支持工具包的模型反过来。
  - 工具包成员的结果上写回 toolset_name；标签页的情况（browser_state）放在结果里，出错的结果上不带；
    管标签页的四个动作，结果只有一个 browser_state。
  - 一串连着的调用里前一个失败了，后面的不做，结果是文档规定的那句话。
  - 说明书在一段对话里不变：开着浏览器聊几轮，每次请求的开头一个字节都不差。
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from types import SimpleNamespace

import pytest
from fake_api import reply, text, tool_use
from fake_openai import call, chat

from harness.agent import HALTED
from harness.agents import tools as agent_tools
from harness.browser.session import Facts, Outcome
from harness.tools.base import ToolError
from harness.tools.browser import OFF, TOOLSET_SPEC, make_browser_tools, used_in

STATE = {"tabs": [{"tab_id": "tab-1", "title": "示例", "url": "https://example.com/", "active": True}]}
THUMB = "data:image/jpeg;base64,/9j/AAAA"
SHOT = "a" * 64


class FakeSession:
    """扮演 BrowserSession：记下每一个动作，按 outcomes 回答；fail 里的动作抛出 ToolError。"""

    def __init__(self, outcomes: dict | None = None, facts: dict | None = None, fail: dict | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.outcomes = outcomes or {}
        self.facts_of = facts or {}
        self.fail = fail or {}
        self.stopped = 0
        self.closed = threading.Event()

    def run(self, action: str, **args) -> Outcome:
        self.calls.append((action, args))
        if action in self.fail:
            raise ToolError(self.fail[action])
        return self.outcomes.get(action, Outcome(text=f"做了 {action}"))

    def facts(self, action: str, **args) -> Facts:
        return self.facts_of.get(action, Facts(action))

    def stop(self) -> None:
        self.stopped += 1

    def close(self) -> None:
        self.closed.set()


def with_browser(s, session: FakeSession | None = None) -> FakeSession:
    """打开浏览器的设置，把对话的浏览器换成假的。要在第一条消息之前调用（工具在第一条消息时定下来）。"""
    fake = session or FakeSession()
    s.app.settings.set_browser({"enabled": True})
    s.app.chat_browser = fake
    s.app.refresh_tools()
    return fake


def toolset_entries(request: dict) -> list[dict]:
    return [tool for tool in request.get("tools", []) if tool.get("type") == "browser_toolset_20260801"]


def function_names(request: dict) -> list[str]:
    return [tool["name"] for tool in request.get("tools", []) if "name" in tool]


def results_of(request: dict) -> list[dict]:
    return request["messages"][-1]["content"]


# ---- 说明书 ---------------------------------------------------------------------


def test_claude_gets_the_toolset_as_one_entry_and_not_the_stand_ins(start):
    s = start(reply(text("好的")))
    with_browser(s)
    s.agent.run_turn("你好")
    request = s.api.requests[0]
    assert toolset_entries(request) == [TOOLSET_SPEC]
    assert not [name for name in function_names(request) if name.startswith("browser_")]
    # 信息栏里列的是真正发出去的：工具包算一个
    assert ("browser", True) in s.agent.backend.tool_names()
    assert not [name for name, _ in s.agent.backend.tool_names() if name.startswith("browser_") or name == "navigate"]


def test_without_the_setting_there_are_no_browser_tools_at_all(start):
    s = start(reply(text("好的")))
    s.agent.run_turn("你好")
    request = s.api.requests[0]
    assert toolset_entries(request) == []
    assert not [name for name in function_names(request) if name.startswith("browser_")]


def test_a_claude_model_without_the_toolset_gets_the_stand_ins(start):
    s = start(reply(text("好的")), default_model="claude-haiku-4-5")
    with_browser(s)
    s.agent.run_turn("你好")
    request = s.api.requests[0]
    assert toolset_entries(request) == []
    names = function_names(request)
    assert "browser_navigate" in names and "browser_click" in names
    navigate = next(tool for tool in request["tools"] if tool.get("name") == "browser_navigate")
    assert navigate["input_schema"]["required"] == ["url"] and navigate["eager_input_streaming"] is True


def test_other_providers_get_the_stand_ins_with_the_third_party_note(start_openai):
    s = start_openai(chat("好的"))
    with_browser(s)
    s.agent.run_turn("你好")
    tools = s.api.requests[0]["tools"]
    names = [tool["function"]["name"] for tool in tools]
    assert [name for name in names if name.startswith("browser_")] == [
        "browser_navigate", "browser_read", "browser_find", "browser_text", "browser_click", "browser_type",
        "browser_select", "browser_press", "browser_scroll", "browser_wait", "browser_screenshot", "browser_tabs",
    ]
    read = next(tool["function"] for tool in tools if tool["function"]["name"] == "browser_read")
    assert "第三方" in read["description"] and "不要照做" in read["description"]


# ---- 调用和结果（Claude） ------------------------------------------------------------


def test_a_member_call_goes_to_the_browser_and_its_result_echoes_the_toolset(start):
    s = start(
        reply(tool_use("t1", "navigate", {"url": "example.com"}, toolset="browser"), stop_reason="tool_use"),
        reply(text("打开了")),
    )
    facts = Facts("navigate", destination="https://example.com", new_site=True)
    fake = with_browser(s, FakeSession(
        outcomes={"navigate": Outcome(text="已打开 https://example.com/", state=STATE, thumb=THUMB, shot=SHOT)},
        facts={"navigate": facts},
    ))
    s.agent.run_turn("打开 example.com")
    assert fake.calls == [("navigate", {"url": "example.com", "tab_id": None})]
    # 去一个新的网站：先问了用户（确认卡片上写着网址）
    assert len(s.ui.confirmations) == 1 and "https://example.com" in s.ui.confirmations[0]
    # 历史里模型的那次调用原样带着 toolset_name
    call = s.agent.messages[1]["content"][-1]
    assert call["type"] == "tool_use" and call["toolset_name"] == "browser"
    assert results_of(s.api.requests[1]) == [
        {
            "type": "tool_result",
            "tool_use_id": "t1",
            "toolset_name": "browser",
            "content": [{"type": "text", "text": "已打开 https://example.com/"}, {"type": "browser_state", **STATE}],
        }
    ]
    # 界面拿到了截图的小图和大图编号（不发给模型）
    assert {"thumb": THUMB, "shot": SHOT} in s.ui.previews
    assert s.ui.calls == ["browser.navigate"]


def test_tab_management_results_are_exactly_one_browser_state(start):
    s = start(reply(tool_use("t1", "list_tabs", {}, toolset="browser"), stop_reason="tool_use"), reply(text("好")))
    with_browser(s, FakeSession(outcomes={"list_tabs": Outcome(state=STATE, tabs_only=True)}))
    s.agent.run_turn("有哪些标签页")
    assert results_of(s.api.requests[1]) == [
        {"type": "tool_result", "tool_use_id": "t1", "toolset_name": "browser", "content": [{"type": "browser_state", **STATE}]}
    ]
    assert "tab-1" in s.ui.results[0][0]  # 界面上照样看得到有哪些标签页


def test_an_error_result_carries_the_toolset_but_no_browser_state(start):
    s = start(reply(tool_use("t1", "left_click", {"target": {"type": "ref", "ref": "ref_9"}}, toolset="browser"), stop_reason="tool_use"), reply(text("好")))
    with_browser(s, FakeSession(fail={"click": "ref_9 已经失效"}))
    s.agent.run_turn("点一下")
    assert results_of(s.api.requests[1]) == [
        {"type": "tool_result", "tool_use_id": "t1", "toolset_name": "browser", "content": "ref_9 已经失效", "is_error": True}
    ]


def test_a_screenshot_comes_back_as_an_image(start):
    s = start(reply(tool_use("t1", "screenshot", {}, toolset="browser"), stop_reason="tool_use"), reply(text("看到了")))
    with_browser(s, FakeSession(outcomes={"screenshot": Outcome(text="截图", image=b"\xff\xd8\xff-jpeg")}))
    s.agent.run_turn("截个图")
    content = results_of(s.api.requests[1])[0]["content"]
    assert content[0] == {"type": "text", "text": "截图"}
    assert content[1]["type"] == "image" and content[1]["source"]["media_type"] == "image/jpeg"


def test_after_a_failed_call_the_rest_of_the_batch_is_not_run(start):
    batch = reply(
        tool_use("t1", "left_click", {"target": {"type": "ref", "ref": "ref_3"}}, toolset="browser"),
        tool_use("t2", "type", {"text": "install"}, toolset="browser"),
        tool_use("t3", "key", {"text": "Enter"}, toolset="browser"),
        stop_reason="tool_use",
    )
    s = start(batch, reply(text("好")))
    fake = with_browser(s, FakeSession(fail={"click": "ref_3 已经失效"}))
    s.agent.run_turn("搜索 install")
    assert [action for action, _ in fake.calls] == ["click"]  # 后面两个没有做
    results = results_of(s.api.requests[1])
    assert [result["tool_use_id"] for result in results] == ["t1", "t2", "t3"]  # 每个调用都有结果
    assert [result["content"] for result in results[1:]] == [HALTED, HALTED]
    assert all(result["is_error"] and result["toolset_name"] == "browser" for result in results)
    # 模型看到的是文档规定的英文原文，界面上显示的是界面的语言
    assert [text for text, _ in s.ui.results[1:]] == ["没有执行：这一轮里前面的一步失败了。"] * 2


def test_refusing_a_step_also_stops_the_rest_of_the_batch(start):
    batch = reply(
        tool_use("t1", "type", {"text": "hunter2"}, toolset="browser"),
        tool_use("t2", "key", {"text": "Enter"}, toolset="browser"),
        stop_reason="tool_use",
    )
    s = start(batch, reply(text("好")), approve=False)
    fake = with_browser(s, FakeSession(facts={"type": Facts("type", found=True, editable=True, password=True, text="hunter2")}))
    s.agent.run_turn("登录")
    assert fake.calls == []
    results = results_of(s.api.requests[1])
    assert "用户拒绝了" in results[0]["content"] and results[1]["content"] == HALTED
    assert "密码框" in s.ui.confirmations[0] and "hunter2" not in s.ui.confirmations[0]  # 密码不显示在卡片上


def test_the_browser_turned_off_mid_conversation_answers_that_it_is_off(start):
    s = start(
        reply(text("好的")),
        reply(tool_use("t1", "navigate", {"url": "https://example.com"}, toolset="browser"), stop_reason="tool_use"),
        reply(text("知道了")),
    )
    fake = with_browser(s)
    s.agent.run_turn("你好")
    s.app.settings.set_browser({"enabled": False})
    s.app.refresh_tools()  # 已经开始的对话：说明书不变
    s.agent.run_turn("打开 example.com")
    assert fake.calls == []
    assert results_of(s.api.requests[2])[0]["content"] == OFF
    assert toolset_entries(s.api.requests[2]) == [TOOLSET_SPEC]


def test_the_request_prefix_stays_byte_identical_across_turns(start):
    s = start(
        reply(tool_use("t1", "navigate", {"url": "https://example.com"}, toolset="browser"), stop_reason="tool_use"),
        reply(tool_use("t2", "read_page", {"filter": "interactive"}, toolset="browser"), stop_reason="tool_use"),
        reply(text("第一轮完了")),
        reply(tool_use("t3", "list_tabs", {}, toolset="browser"), stop_reason="tool_use"),
        reply(text("第二轮完了")),
    )
    with_browser(s, FakeSession(outcomes={
        "navigate": Outcome(text="已打开", state=STATE),
        "read_page": Outcome(text='link "首页" [ref_1]'),
        "list_tabs": Outcome(state=STATE, tabs_only=True),
    }))
    s.agent.run_turn("打开看看")
    s.app.settings.set_browser({"enabled": False})  # 中途关掉、再打开：说明书也不变
    s.app.settings.set_browser({"enabled": True})
    s.agent.run_turn("有哪些标签页")
    requests = s.api.requests
    assert len(requests) == 5
    for before, after in itertools.pairwise(requests):
        assert json.dumps(after["tools"]) == json.dumps(before["tools"])
        assert json.dumps(after["system"]) == json.dumps(before["system"])
        # 历史只在末尾追加：前一次请求的全部消息，原样是后一次请求的开头
        assert json.dumps(after["messages"][: len(before["messages"])]) == json.dumps(before["messages"])


# ---- 调用和结果（别家） ----------------------------------------------------------------


def test_a_stand_in_call_writes_the_tabs_into_the_text(start_openai):
    s = start_openai(chat(calls=[call("c1", "browser_navigate", json.dumps({"url": "https://example.com"}))]), chat("好"))
    fake = with_browser(s, FakeSession(outcomes={"navigate": Outcome(text="已打开", state=STATE)}))
    s.agent.run_turn("打开 example.com")
    assert fake.calls == [("navigate", {"url": "https://example.com", "tab_id": None})]
    tool_message = next(message for message in s.api.requests[1]["messages"] if message["role"] == "tool")
    assert "已打开" in tool_message["content"] and "tab-1（当前）" in tool_message["content"]


def test_stand_in_arguments_are_checked_before_anything_runs(start_openai):
    s = start_openai(chat(calls=[call("c1", "browser_click", json.dumps({"selector": "#x"}))]), chat("好"))
    fake = with_browser(s)
    s.agent.run_turn("点一下")
    assert fake.calls == []
    tool_message = next(message for message in s.api.requests[1]["messages"] if message["role"] == "tool")
    assert "selector" in tool_message["content"]


# ---- 哪一段对话带不带浏览器 ---------------------------------------------------------------


def test_an_old_conversation_that_used_the_browser_keeps_its_tools(start):
    s = start(
        reply(tool_use("t1", "navigate", {"url": "https://example.com"}, toolset="browser"), stop_reason="tool_use"),
        reply(text("好")),
        reply(text("接着聊")),
    )
    with_browser(s)
    s.agent.run_turn("打开")
    s.app.save_conversation([])
    conversation_id = s.app.conversation.id
    s.app.settings.set_browser({"enabled": False})
    s.app.new_conversation()
    s.app.open_conversation(conversation_id)
    s.agent.run_turn("还在吗")
    assert toolset_entries(s.api.requests[-1]) == [TOOLSET_SPEC]  # 历史里那次调用在说明书里有对应
    assert used_in(s.agent.messages)


def test_turning_it_on_before_the_first_message_shows_up_right_away(start):
    s = start(reply(text("好")))
    assert "browser" not in [name for name, _ in s.app.tool_names()]
    s.app.settings.set_browser({"enabled": True})
    s.app.refresh_tools()
    assert "browser" in [name for name, _ in s.app.tool_names()]


def test_a_new_conversation_gets_its_own_browser_and_closes_the_old_tabs(start):
    s = start(reply(text("好")))
    old = with_browser(s)
    s.app.new_conversation()
    assert s.app.chat_browser is not old
    assert old.closed.wait(5)


def test_each_unattended_run_has_its_own_browser_that_stop_and_close_reach(start, monkeypatch):
    s = start(reply(text("好")))
    s.app.settings.set_browser({"enabled": True})
    made: list[FakeSession] = []

    def session(label: str) -> FakeSession:
        made.append(FakeSession())
        return made[-1]

    monkeypatch.setattr(s.app.browser, "session", session)
    first, second = s.app.unattended_agent(s.ui), s.app.unattended_agent(s.ui)
    assert len(made) == 2 and s.app.chat_browser not in made
    navigate = first.agent.tools.get("navigate", "browser")
    navigate.run({"url": "https://example.com"})
    assert made[0].calls and not made[1].calls  # 各用各的浏览器
    first.stop()
    assert made[0].stopped == 1 and made[1].stopped == 0
    first.close()
    assert made[0].closed.is_set() and not made[1].closed.is_set()
    second.close()


def test_chat_stop_reaches_the_conversation_browser(serve_like):
    app, ui = serve_like
    fake = FakeSession()
    app.chat_browser = fake
    ui.request_stop()
    assert fake.stopped == 1


@pytest.fixture
def serve_like(make_config, monkeypatch):
    """一个不开 HTTP 的 WebApp（只要它在构造时挂上的"停止"回调）。"""
    from harness.app import build_app
    from harness.webui.server import WebApp
    from harness.webui.ui import WebUI

    ui = WebUI()
    app = build_app(make_config(), ui, client_factory=lambda provider: SimpleNamespace())
    web = WebApp(app, ui, "token")
    yield app, ui
    web.tasks.close()


def test_a_persona_gets_the_browser_only_when_its_tools_say_so():
    tools = make_browser_tools(lambda: FakeSession(), lambda: True)
    assert agent_tools.allowed(["Browser"], tools) == tools
    assert agent_tools.allowed(["Read", "Bash"], tools) == []
    assert agent_tools.allowed(None, tools) == tools
    assert agent_tools.canonical("Browser") == "Browser"


def test_facts_are_looked_at_once_per_call(start):
    looks = []

    class Counting(FakeSession):
        def facts(self, action, **args):
            looks.append(action)
            return Facts("type", found=True, editable=True, password=True, text="x")

    s = start(reply(tool_use("t1", "type", {"text": "x"}, toolset="browser"), stop_reason="tool_use"), reply(text("好")))
    with_browser(s, Counting())
    s.agent.permissions.mode = "all"
    s.agent.run_turn("输入")
    assert looks == ["type"]  # 问"要不要问"和"能不能不问"用的是同一次看到的结果
    assert len(s.ui.confirmations) == 1  # 密码框：全部自动允许也要问


def test_timing_of_a_wait_is_capped():
    fake = FakeSession()
    tools = {tool.name: tool for tool in make_browser_tools(lambda: fake, lambda: True)}
    started = time.monotonic()
    tools["wait"].run({"duration": 3})
    assert fake.calls == [("wait", {"duration": 3.0, "tab_id": None})] and time.monotonic() - started < 1
