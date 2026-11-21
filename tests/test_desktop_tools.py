"""操作整台电脑的工具在 agent 循环里怎么走：发给模型的说明书、调用怎么落到电脑上、结果怎么交回去、问不问你。

模型是本地的假 API（fake_api.py、fake_openai.py），电脑是 FakeDesktop（fake_desktop.py）：测的是"说明书和结果的格式对不对、
规矩守没守"，不碰真的屏幕。几条要守住的规矩：
  - Claude 的电脑工具包在请求里只占一项 {"type": "computer_toolset_20260801"}，替身（computer_*）不发；不支持工具包的模型反过来；
    看不了图片的模型（DeepSeek）一个都拿不到。
  - 工具包成员的结果上写回 toolset_name: "computer"；截图是一张图片。
  - 一串连着的调用里前一个失败了（包括你拒绝了），后面的不做，结果是文档给电脑工具包规定的那句话。
  - 每一步都问，"全部自动允许"也不放开；只有截图可以"这段对话里不再问"；等一等、看鼠标在哪儿不问。
  - 定时任务、聊天通道：设置里另外打开了才有这些工具，有了也每一步都问。屏幕同一时间只归一个运行。
  - 说明书在一段对话里不变：每次请求的开头一个字节都不差。
"""

from __future__ import annotations

import itertools
import json

import openai
import pytest
from conftest import RecordingUI
from fake_api import reply, text, tool_use
from fake_desktop import FakeDesktop, install
from fake_openai import FakeOpenAI, call, chat

from harness import agent as agent_module
from harness.agents import tools as agent_tools
from harness.app import build_app
from harness.desktop import session as session_module
from harness.tools.computer import (
    HALTED,
    OFF,
    TOOLSET_SPEC,
    make_computer_tools,
    used_in,
)

SCREENSHOT = tool_use("s1", "screenshot", {}, toolset="computer")


@pytest.fixture(autouse=True)
def no_settle(monkeypatch):
    monkeypatch.setattr(session_module, "DESKTOP_SETTLE_SECONDS", 0)


@pytest.fixture
def start_gpt(make_config):
    """和 start_openai 一样，但用的是 GPT（看得懂图片）。"""
    servers: list[FakeOpenAI] = []

    def start(*replies, approve: bool = True):
        api = FakeOpenAI(list(replies)).start()
        servers.append(api)
        config = make_config(default_provider="openai", default_model="gpt-5")
        ui = RecordingUI(approve)
        client = openai.OpenAI(api_key="test-key", base_url=api.base_url, max_retries=0)
        app = build_app(config, ui, client_factory=lambda provider: client)
        from types import SimpleNamespace

        return SimpleNamespace(agent=app.agent, app=app, api=api, ui=ui, config=config)

    yield start
    for api in servers:
        api.stop()


def with_computer(s, fake: FakeDesktop | None = None, **settings) -> FakeDesktop:
    """打开"操作整台电脑"，换上假的电脑。要在第一条消息之前调用（工具在第一条消息时定下来）。"""
    fake = fake or FakeDesktop()
    install(s.app.desktop, fake)
    s.app.settings.set_computer({"enabled": True, **settings})
    s.app.refresh_tools()
    return fake


def toolset_entries(request: dict) -> list[dict]:
    return [tool for tool in request.get("tools", []) if tool.get("type") == "computer_toolset_20260801"]


def function_names(request: dict) -> list[str]:
    return [tool["name"] for tool in request.get("tools", []) if "name" in tool]


def results_of(request: dict) -> list[dict]:
    return request["messages"][-1]["content"]


# ---- 说明书 ---------------------------------------------------------------------


def test_claude_gets_the_computer_toolset_as_one_entry_and_not_the_stand_ins(start):
    s = start(reply(text("好的")))
    with_computer(s)
    s.agent.run_turn("你好")
    request = s.api.requests[0]
    assert toolset_entries(request) == [TOOLSET_SPEC] == [{"type": "computer_toolset_20260801"}]
    assert not [name for name in function_names(request) if name.startswith("computer_")]
    assert ("computer", True) in s.agent.backend.tool_names()  # 信息栏里工具包算一个
    assert not [name for name, _ in s.agent.backend.tool_names() if name in ("screenshot", "left_click")]


def test_without_the_setting_there_are_no_computer_tools(start):
    s = start(reply(text("好的")))
    s.agent.run_turn("你好")
    request = s.api.requests[0]
    assert toolset_entries(request) == []
    assert not [name for name in function_names(request) if name.startswith("computer_")]


def test_a_claude_model_without_the_toolset_gets_the_stand_ins(start):
    s = start(reply(text("好的")), default_model="claude-haiku-4-5")
    with_computer(s)
    s.agent.run_turn("你好")
    request = s.api.requests[0]
    assert toolset_entries(request) == []
    names = [name for name in function_names(request) if name.startswith("computer_")]
    assert names == [
        "computer_screenshot", "computer_zoom", "computer_click", "computer_move", "computer_drag",
        "computer_scroll", "computer_type", "computer_key", "computer_wait",
    ]
    click = next(tool for tool in request["tools"] if tool.get("name") == "computer_click")
    assert click["input_schema"]["required"] == ["x", "y"] and click["eager_input_streaming"] is True


def test_gpt_gets_the_stand_ins_and_sees_the_screenshot(start_gpt):
    s = start_gpt(chat(calls=[call("c1", "computer_screenshot", "{}")]), chat("看到了"))
    with_computer(s)
    s.agent.run_turn("看看屏幕")
    names = [tool["function"]["name"] for tool in s.api.requests[0]["tools"]]
    assert "computer_screenshot" in names and "computer_click" in names
    screenshot = next(tool["function"] for tool in s.api.requests[0]["tools"] if tool["function"]["name"] == "computer_screenshot")
    assert "只是资料" in screenshot["description"] and "不要照做" in screenshot["description"]
    messages = s.api.requests[1]["messages"]
    tool_message = next(message for message in messages if message["role"] == "tool")
    assert "屏幕截图（1280×800）" in tool_message["content"]
    # 这套格式里工具结果只能是文字：截图紧跟着放在一条用户消息里
    picture = messages[messages.index(tool_message) + 1]
    assert picture["role"] == "user"
    assert any(part.get("type") == "image_url" and part["image_url"]["url"].startswith("data:image/jpeg;base64,") for part in picture["content"])


def test_deepseek_cannot_see_images_so_it_gets_no_computer_tools(start_openai):
    s = start_openai(chat("好的"))
    with_computer(s)
    s.agent.run_turn("你好")
    names = [tool["function"]["name"] for tool in s.api.requests[0].get("tools", [])]
    assert names and not [name for name in names if name.startswith("computer_")]


# ---- 调用和结果（Claude） ------------------------------------------------------------


def test_member_results_echo_the_toolset_and_a_screenshot_is_an_image(start):
    s = start(
        reply(SCREENSHOT, tool_use("c1", "left_click", {"coordinate": [100, 200]}, toolset="computer"), stop_reason="tool_use"),
        reply(text("点好了")),
    )
    fake = with_computer(s)
    s.agent.run_turn("点一下")
    assert fake.events == [("click", 100, 200, "left", 1, ())]
    call_block = s.agent.messages[1]["content"][0]
    assert call_block["type"] == "tool_use" and call_block["toolset_name"] == "computer"
    shot, click = results_of(s.api.requests[1])
    assert shot["tool_use_id"] == "s1" and shot["toolset_name"] == "computer" and "is_error" not in shot
    assert shot["content"][0]["type"] == "text" and "1280×800" in shot["content"][0]["text"]
    assert shot["content"][1]["type"] == "image" and shot["content"][1]["source"]["media_type"] == "image/jpeg"
    assert click == {"type": "tool_result", "tool_use_id": "c1", "content": "在 (100, 200) 点了一下。", "toolset_name": "computer"}
    # 界面上每一步都有一张小预览图（不发给模型）
    assert all(preview and preview["thumb"].startswith("data:image/jpeg;base64,") for preview in s.ui.previews)
    assert s.ui.calls == ["computer.screenshot", "computer.left_click"]


def test_a_denied_step_stops_the_rest_of_the_batch_with_the_computer_text(start):
    batch = reply(
        tool_use("c1", "left_click", {"coordinate": [10, 10]}, toolset="computer"),
        tool_use("c2", "type", {"text": "hunter2"}, toolset="computer"),
        tool_use("c3", "key", {"text": "Return"}, toolset="computer"),
        stop_reason="tool_use",
    )
    s = start(batch, reply(text("好")), approve=False)
    fake = with_computer(s)
    s.agent.run_turn("登录")
    assert fake.events == [] and len(s.ui.confirmations) == 1  # 拒绝了第一步，后面两步不再问
    results = results_of(s.api.requests[1])
    assert "用户拒绝了" in results[0]["content"]
    assert [result["content"] for result in results[1:]] == [HALTED, HALTED]
    assert HALTED == "Not executed: an earlier computer action in this turn failed."
    assert agent_module.HALTED == "Not executed: an earlier action in this turn failed."  # 浏览器的那句不变
    assert all(result["is_error"] and result["toolset_name"] == "computer" for result in results)
    assert [text_ for text_, _ in s.ui.results[1:]] == ["没有执行：这一轮里前面的一步失败了。"] * 2


def test_a_failed_step_also_stops_the_rest(start):
    batch = reply(
        tool_use("c1", "left_click", {"coordinate": [5000, 10]}, toolset="computer"),
        tool_use("c2", "type", {"text": "x"}, toolset="computer"),
        stop_reason="tool_use",
    )
    s = start(batch, reply(text("好")))
    fake = with_computer(s)
    s.agent.run_turn("点")
    first, second = results_of(s.api.requests[1])
    assert "不在截图里" in first["content"] and second["content"] == HALTED
    assert fake.events == [] and s.ui.confirmations == []  # 坐标不对：根本不会做，不用问


def test_a_stand_in_denied_step_stops_the_rest_too(start_gpt):
    s = start_gpt(
        chat(calls=[call("c1", "computer_click", json.dumps({"x": 5, "y": 5})), call("c2", "computer_type", json.dumps({"text": "秘密"}))]),
        chat("好"),
        approve=False,
    )
    fake = with_computer(s)
    s.agent.run_turn("打字")
    assert fake.events == [] and len(s.ui.confirmations) == 1  # 拒绝了"点这个输入框"，"打字"连问都不问
    tools = [message for message in s.api.requests[1]["messages"] if message["role"] == "tool"]
    assert "用户拒绝了" in tools[0]["content"] and "Not executed" in tools[1]["content"]


def test_turned_off_mid_conversation_answers_that_it_is_off(start):
    s = start(reply(text("好的")), reply(SCREENSHOT, stop_reason="tool_use"), reply(text("知道了")))
    fake = with_computer(s)
    s.agent.run_turn("你好")
    s.app.settings.set_computer({"enabled": False})
    s.app.refresh_tools()  # 已经开始的对话：说明书不变
    s.agent.run_turn("截个图")
    assert fake.captures == [] and s.ui.confirmations == []
    assert results_of(s.api.requests[2])[0]["content"] == OFF
    assert toolset_entries(s.api.requests[2]) == [TOOLSET_SPEC]


# ---- 问不问你 ---------------------------------------------------------------------


def test_every_input_action_asks_even_when_everything_is_auto_allowed(start):
    actions = [
        tool_use("a1", "left_click", {"coordinate": [1, 1]}, toolset="computer"),
        tool_use("a2", "double_click", {"coordinate": [1, 1]}, toolset="computer"),
        tool_use("a3", "left_click_drag", {"start_coordinate": [1, 1], "coordinate": [9, 9]}, toolset="computer"),
        tool_use("a4", "mouse_move", {"coordinate": [3, 3]}, toolset="computer"),
        tool_use("a5", "scroll", {"scroll_direction": "down", "scroll_amount": 2}, toolset="computer"),
        tool_use("a6", "type", {"text": "hi"}, toolset="computer"),
        tool_use("a7", "key", {"text": "ctrl+s"}, toolset="computer"),
        tool_use("a8", "hold_key", {"text": "shift", "duration": 0}, toolset="computer"),
        tool_use("a9", "left_mouse_down", {}, toolset="computer"),
        tool_use("a10", "left_mouse_up", {}, toolset="computer"),
        tool_use("a11", "zoom", {"region": [0, 0, 100, 100]}, toolset="computer"),
        SCREENSHOT,
    ]
    s = start(reply(*actions, stop_reason="tool_use"), reply(text("好")))
    with_computer(s)
    s.agent.permissions.mode = "all"
    s.agent.run_turn("做吧")
    assert len(s.ui.confirmations) == len(actions)
    assert s.ui.auto_allowed_count == 0


def test_wait_and_cursor_position_do_not_ask(start):
    s = start(
        reply(tool_use("w1", "wait", {"duration": 0}, toolset="computer"), tool_use("p1", "cursor_position", {}, toolset="computer"), stop_reason="tool_use"),
        reply(text("好")),
    )
    fake = with_computer(s)
    fake.cursor_at = (40.0, 30.0)
    s.agent.run_turn("等一下")
    assert s.ui.confirmations == []
    assert [result["content"] for result in results_of(s.api.requests[1])] == ["等了 0 秒。", "X=40, Y=30"]


def test_screenshots_ask_until_trusted_for_this_conversation(start):
    s = start(reply(SCREENSHOT, stop_reason="tool_use"), reply(text("好")), reply(tool_use("s2", "screenshot", {}, toolset="computer"), stop_reason="tool_use"), reply(text("好")))
    with_computer(s)
    s.agent.run_turn("截图")
    assert len(s.ui.confirmations) == 1
    question = s.ui.confirmations[0]
    assert getattr(question, "offer", "") == "screenshots"  # 卡片上多给一个"这段对话里截图不再问"
    assert "屏幕上的一切" in question and "备忘录" in question
    s.app.chat_desktop.trust_screenshots = True
    s.agent.run_turn("再截一张")
    assert len(s.ui.confirmations) == 1 and s.ui.auto_allowed_count == 1  # 没问，卡片上标着"自动允许"


def test_trusting_screenshots_never_lets_clicks_through(start):
    s = start(reply(tool_use("c1", "left_click", {"coordinate": [1, 1]}, toolset="computer"), stop_reason="tool_use"), reply(text("好")))
    with_computer(s)
    s.app.chat_desktop.trust_screenshots = True
    s.agent.permissions.mode = "all"
    s.agent.run_turn("点")
    assert len(s.ui.confirmations) == 1 and not getattr(s.ui.confirmations[0], "offer", "")
    assert "左键单击" in s.ui.confirmations[0] and "落在：备忘录" in s.ui.confirmations[0]


def test_dangerous_keys_carry_a_warning_on_the_card(start):
    s = start(reply(tool_use("k1", "key", {"text": "cmd+q"}, toolset="computer"), stop_reason="tool_use"), reply(text("好")))
    with_computer(s)
    s.agent.run_turn("退出")
    assert "⌘Q" in s.ui.confirmations[0] and "退出最前面的那个程序" in s.ui.confirmations[0]


def test_retina_clicks_show_both_coordinates_on_the_card(start):
    s = start(reply(tool_use("c1", "left_click", {"coordinate": [665, 432]}, toolset="computer"), stop_reason="tool_use"), reply(text("好")))
    fake = with_computer(s, FakeDesktop(width=1512, height=982, density=2))
    s.agent.run_turn("点")
    assert "(665, 432)" in s.ui.confirmations[0] and "(756, 491)" in s.ui.confirmations[0]
    assert fake.events == [("click", 756, 491, "left", 1, ())]


def test_typed_text_is_shown_and_newlines_are_flagged(start):
    s = start(reply(tool_use("t1", "type", {"text": "第一行\n第二行"}, toolset="computer"), stop_reason="tool_use"), reply(text("好")))
    with_computer(s)
    s.agent.run_turn("打字")
    card = s.ui.confirmations[0]
    assert "第一行" in card and "第二行" in card and "换行" in card and "打到：备忘录" in card


def test_blocked_apps_are_refused_without_asking(start):
    s = start(reply(SCREENSHOT, stop_reason="tool_use"), reply(text("好")))
    fake = with_computer(s, blocked=["备忘录"])
    s.agent.run_turn("截图")
    assert s.ui.confirmations == [] and fake.captures == []
    assert "不许碰" in results_of(s.api.requests[1])[0]["content"]


# ---- 定时任务、聊天通道 ---------------------------------------------------------------


def test_unattended_runs_get_no_computer_tools_unless_allowed(start):
    s = start(reply(text("好")))
    with_computer(s)
    run = s.app.unattended_agent(s.ui, "all")
    assert run.agent.tools.get("screenshot", "computer") is None
    assert not [tool for tool in run.agent.tools if tool.stands_in_for == "computer"]
    run.close()
    channel = s.app.channel_agent(s.ui, "all", "anthropic", "claude-opus-5-5")
    assert channel.agent.tools.get("screenshot", "computer") is None
    channel.close()


def test_allowed_unattended_runs_still_ask_every_step(start):
    s = start(reply(SCREENSHOT, tool_use("c1", "left_click", {"coordinate": [5, 5]}, toolset="computer"), stop_reason="tool_use"), reply(text("好")))
    fake = with_computer(s, unattended=True)
    s.app.chat_desktop.trust_screenshots = True  # 你在对话里放开的截图，任务里不算数
    run = s.app.unattended_agent(s.ui, "all")  # 这个任务选了"全部自动允许"
    assert run.agent.tools.get("screenshot", "computer") is not None
    run.agent.run_turn("<task>看一眼屏幕</task>")
    assert len(s.ui.confirmations) == 2 and s.ui.auto_allowed_count == 0
    assert fake.events == [("click", 5, 5, "left", 1, ())]
    run.close()
    assert s.app.desktop.free_for(s.app.chat_desktop)  # 这次运行结束：屏幕让出来了


def test_the_screen_belongs_to_one_run_at_a_time(start):
    s = start(
        reply(SCREENSHOT, stop_reason="tool_use"),
        reply(text("对话里截了一张")),
        reply(tool_use("t1", "screenshot", {}, toolset="computer"), stop_reason="tool_use"),
        reply(text("任务没截成")),
    )
    fake = with_computer(s, unattended=True)
    s.agent.run_turn("截图")  # 对话占着屏幕（网页上要等这一轮结束才让出来，见 server.py）
    asked = len(s.ui.confirmations)
    run = s.app.unattended_agent(s.ui, "ask")
    run.agent.run_turn("<task>截图</task>")
    result = results_of(s.api.requests[3])[0]
    assert result["is_error"] and "你的对话" in result["content"]
    assert len(s.ui.confirmations) == asked  # 做不成的事不问你
    assert len(fake.captures) == 1
    run.close()
    s.app.chat_desktop.release()


def test_each_run_has_its_own_session_that_stop_and_close_reach(start):
    s = start(reply(text("好")))
    with_computer(s, unattended=True)
    first, second = s.app.unattended_agent(s.ui), s.app.unattended_agent(s.ui)
    one = first.agent.tools.get("screenshot", "computer")
    two = second.agent.tools.get("screenshot", "computer")
    one.run({})
    assert not s.app.desktop.free_for(s.app.chat_desktop)
    first.stop()
    first.close()
    assert s.app.desktop.free_for(s.app.chat_desktop)
    two.run({})  # 另一个运行照常
    second.close()


# ---- 说明书在一段对话里不变 -----------------------------------------------------------------


def test_the_request_prefix_stays_byte_identical_across_turns(start):
    s = start(
        reply(SCREENSHOT, stop_reason="tool_use"),
        reply(tool_use("c1", "left_click", {"coordinate": [10, 10]}, toolset="computer"), stop_reason="tool_use"),
        reply(text("第一轮完了")),
        reply(tool_use("k1", "key", {"text": "Return"}, toolset="computer"), stop_reason="tool_use"),
        reply(text("第二轮完了")),
    )
    with_computer(s)
    s.agent.run_turn("看看再点")
    s.app.chat_desktop.release()
    s.app.settings.set_computer({"enabled": False})  # 中途关掉、再打开：说明书也不变
    s.app.settings.set_computer({"enabled": True})
    s.app.refresh_tools()
    s.agent.run_turn("按回车")
    requests = s.api.requests
    assert len(requests) == 5
    for before, after in itertools.pairwise(requests):
        assert json.dumps(after["tools"]) == json.dumps(before["tools"])
        assert json.dumps(after["system"]) == json.dumps(before["system"])
        assert json.dumps(after["messages"][: len(before["messages"])]) == json.dumps(before["messages"])


def test_an_old_conversation_that_used_the_computer_keeps_its_tools(start):
    s = start(reply(SCREENSHOT, stop_reason="tool_use"), reply(text("好")), reply(text("接着聊")))
    with_computer(s)
    s.agent.run_turn("截图")
    s.app.save_conversation([])
    conversation_id = s.app.conversation.id
    s.app.settings.set_computer({"enabled": False})
    s.app.new_conversation()
    s.app.open_conversation(conversation_id)
    s.agent.run_turn("还在吗")
    assert toolset_entries(s.api.requests[-1]) == [TOOLSET_SPEC]
    assert used_in(s.agent.messages)


def test_used_in_recognises_both_formats():
    assert used_in([{"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": "screenshot", "toolset_name": "computer", "input": {}}]}])
    assert used_in([{"role": "assistant", "tool_calls": [{"function": {"name": "computer_click", "arguments": "{}"}}]}])
    assert not used_in([{"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": "screenshot", "toolset_name": "browser", "input": {}}]}])
    assert not used_in([{"role": "user", "content": "computer_click"}, "junk"])


def test_a_new_conversation_gets_a_fresh_session(start):
    s = start(reply(text("好")))
    with_computer(s)
    old = s.app.chat_desktop
    old.trust_screenshots = True
    s.app.new_conversation()
    assert s.app.chat_desktop is not old and s.app.chat_desktop.trust_screenshots is False


def test_a_persona_gets_the_computer_only_when_its_tools_say_so(start):
    s = start(reply(text("好")))
    tools = make_computer_tools(lambda: s.app.chat_desktop, lambda: True)
    assert agent_tools.allowed(["Computer"], tools) == tools
    assert agent_tools.allowed(["Browser", "Read"], tools) == []
    assert agent_tools.allowed(None, tools) == tools
    assert agent_tools.canonical("Computer") == "Computer"
