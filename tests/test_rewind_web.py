"""编辑最后一条消息、重新生成最后一个回答：网页这一侧（/api/turn/edit、/api/turn/retry，状态里的 rewind）。

和 test_webui.py 一样：真实的 HTTP 服务器，测试代码扮演浏览器，模型是本地的假 API（不花钱）。
页面发请求时带着它看到的那一轮：会话的编号（conversation）和那一轮的 user 事件的编号（turn），都照着状态里的 rewind。
倒回去以后要对得上的几样：发给模型的请求、事件流（reset → 重放留下的事件 → state → 新的一轮）、
会话文件（messages 和 events 一起截断）、标题、搜索和多媒体的索引、花费账本。
"这一轮做过可能改动东西的事"怎么认，在 test_rewind.py 里逐个测；这里测页面上问不问、问了以后能不能接着做。
"""

from __future__ import annotations

import json
import time
from datetime import datetime

import pytest
import test_webui
from fake_api import reply, text, thinking, tool_use
from fake_openai import call, chat
from sample_files import JPEG_BYTES, PNG_BYTES, b64, image, text_file
from test_webui import (
    Browser,
    act,
    latest_state,
    media_file,
    media_list,
    open_conversation,
    saved_file,
    say,
    send_files,
)
from test_webui import search as search_page

from harness import rewind
from harness.attachments import MAX_ATTACHMENTS
from harness.config import MAX_REWIND_EFFECTS
from harness.providers import compaction
from harness.webui.server import MAX_MESSAGE_CHARS

# 借用 test_webui 的 fixture：起一个网页服务器，后面连着假的 API，并让"浏览器"连上去
serve = test_webui.serve
NOW = datetime(2026, 11, 20, 14, 3)
DONE = (200, {"ok": True})
MISSING = "20200101-000000-abcdef"  # 格式正确、但并不存在的会话编号


@pytest.fixture
def rig(serve, monkeypatch):
    """和 serve 一样，另外交出服务器上的 WebApp（s.web）：测"会话被聊天通道认领着"这类情况要直接碰它。
    助手的钟也固定住：两次请求之间跨过了一分钟，时间说明就不一样了，"一字不差"就比不了。"""
    servers = []
    real = test_webui.create_server

    def capture(app, ui, **options):
        server, url = real(app, ui, **options)
        servers.append(server)
        return server, url

    monkeypatch.setattr(test_webui, "create_server", capture)

    def start(*replies, **options):
        s = serve(*replies, **options)
        s.web = servers[-1].web
        s.app.agent.clock = lambda: NOW
        return s

    return start


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def last_id(s) -> int:
    """日志里最后一个事件的编号。"""
    return s.ui.events_after(0, timeout=0)[-1]["id"]


def last_user_id(s) -> int:
    """日志里最后一个 user 事件的编号。"""
    return [event for event in s.ui.events_after(0, timeout=0) if event["type"] == "user"][-1]["id"]


def wait_event(browser: Browser, kind: str, after: int, timeout: float = 10) -> dict:
    """等一个编号在 after 之后、类型是 kind 的事件。倒回去以后旧的事件会重放一遍，所以不能只看类型。"""
    deadline = time.monotonic() + timeout
    while True:
        found = next((event for event in list(browser.events) if event["id"] > after and event["type"] == kind), None)
        if found is not None:
            return found
        assert time.monotonic() < deadline, f"没有等到编号在 {after} 之后的 {kind} 事件；收到的是 {browser.types()}"
        time.sleep(0.01)


def target(state: dict) -> dict:
    """页面点按钮时带上的：它看到的会话，和最后一轮的 user 事件的编号（都照着这份状态）。"""
    return {"conversation": state["conversation"], "turn": state["rewind"]["turn"]}


def seen(s) -> dict:
    """页面此刻看到的那一轮（照着它收到的最新状态）。"""
    return target(latest_state(s.browser))


def assert_ready(s, state: dict, mark: int, effects: tuple[str, ...] = (), more: int = 0, operated: tuple[str, ...] = ()) -> None:
    """状态里的 rewind：最后一轮能倒回去，在历史里的位置是 mark，做过的事是 effects（另有 more 项没列出来）；
    turn 是它的 user 事件的编号；operated 是其中操作过整台电脑、浏览器的那几类。"""
    shown = state["rewind"]
    assert list(shown) == ["turn", "mark", "ok", "reason", "effects", "more", "operated"] and shown["turn"] == last_user_id(s)
    assert {key: value for key, value in shown.items() if key != "turn"} == {
        "mark": mark, "ok": True, "reason": None, "effects": list(effects), "more": more, "operated": list(operated),
    }  # fmt: skip


def replay_and_turn(browser: Browser, after: int) -> tuple[list[dict], list[dict]]:
    """倒回去以后这个浏览器收到的：（reset 和下一个 state 之间重放的事件，那个 state 之后新的一轮直到 turn_end）。"""
    reset = wait_event(browser, "reset", after)
    state = wait_event(browser, "state", reset["id"])
    end = wait_event(browser, "turn_end", state["id"])
    events = list(browser.events)
    replayed = [event for event in events if reset["id"] < event["id"] < state["id"]]
    live = [event for event in events if state["id"] < event["id"] <= end["id"]]
    return replayed, live


def turn(s, action: str, mark: int, **body) -> tuple[list[dict], list[dict]]:
    """在页面上点"编辑"（edit）或"重新生成"（retry），等新的一轮结束。返回值见 replay_and_turn。

    mark：页面上那一轮在历史里的位置应该是多少（先核对一下，再照着状态里的 rewind 发请求）。"""
    state = latest_state(s.browser)
    assert state["rewind"]["mark"] == mark, state["rewind"]
    after = last_id(s)
    assert s.browser.post(f"/api/turn/{action}", {**target(state), **body}) == DONE
    return replay_and_turn(s.browser, after)


def rewind_state(s) -> dict | None:
    return latest_state(s.browser)["rewind"]


def texts(events: list[dict], kind: str = "text") -> list[str]:
    return [event["text"] for event in events if event["type"] == kind]


def kinds(events: list[dict]) -> list[str]:
    return [event["type"] for event in events]


def content(event: dict) -> dict:
    """事件的内容（编号是重新分配的，不参与比较）。"""
    return {key: value for key, value in event.items() if key not in ("id", "confirm")}


# ---- 重新生成、编辑：请求、事件流、会话文件 -------------------------------------------


def test_regenerating_the_last_answer(rig):
    s = rig(reply(thinking("想", signature="sig-1"), text("答一")), reply(text("旧答二")), reply(text("新答二")))
    say(s, "问一")
    state = say(s, "问二")
    conversation_id = state["conversation"]
    # 状态里说清楚最后一轮能不能倒回去：哪一轮、在历史里的位置、能不能、为什么不能、做过什么
    assert_ready(s, state, mark=2)
    before = state["rewind"]["turn"]
    other = Browser(s.url)  # 另一个开着的标签页
    other.wait_synced()

    after = last_id(s)
    replayed, live = turn(s, "retry", mark=2)

    # 请求和这一轮当初的那一次一字不差（缓存照样命中），旧的回答不在里面
    assert dumps(s.api.requests[2]) == dumps(s.api.requests[1]) and "旧答二" not in dumps(s.api.requests[2])
    # 页面：reset，重放留下的（第一轮、第二轮的用户消息），一个 state 结束重放；之后才是新的一轮（播放入场动画）
    assert kinds(replayed) == ["user", "turn_start", "thinking", "block_end", "text", "block_end", "turn_end", "user"]
    assert texts(replayed, "user") == ["问一", "问二"] and replayed[-1]["mark"] == 2
    assert kinds(live)[0] == "turn_start" and "user" not in kinds(live)  # 用户消息不再显示一遍
    assert texts(live) == ["新答二"] and kinds(live)[-1] == "turn_end"
    # 另一个标签页收到的一模一样，各自原地重画
    other_replayed, other_live = replay_and_turn(other, after)
    assert [content(event) for event in other_replayed] == [content(event) for event in replayed]
    assert [content(event) for event in other_live] == [content(event) for event in live]

    # 会话文件：messages 和 events 一起截断，旧的回答哪儿都没有
    saved = saved_file(s.config, conversation_id)
    assert len(saved["messages"]) == 4 and "旧答二" not in json.dumps(saved, ensure_ascii=False)
    assert texts(saved["events"], "user") == ["问一", "问二"] and texts(saved["events"]) == ["答一", "新答二"]
    # 还可以再来一次：还是同一条消息，但重放过了，它的 user 事件换了新的编号
    state = latest_state(s.browser)
    assert_ready(s, state, mark=2)
    assert state["rewind"]["turn"] == replayed[-1]["id"] != before


def test_editing_the_last_message(rig):
    s = rig(reply(text("一")), reply(text("喜马拉雅山很高。")), reply(text("亚马孙河很长。")))
    say(s, "第一句")
    conversation_id = say(s, "说说喜马拉雅山")["conversation"]

    replayed, live = turn(s, "edit", mark=2, text="  说说亚马孙河  ")

    edited = s.api.requests[-1]
    assert dumps(edited["messages"][:2]) == dumps(s.api.requests[1]["messages"][:2])  # 前面的一条都没动
    assert edited["messages"][2]["content"][-1]["text"] == "说说亚马孙河" and len(edited["messages"]) == 3
    assert "喜马拉雅" not in dumps(edited)
    # 重放的只有第一轮；改过的消息作为新的一轮出现，记着同一个位置
    assert texts(replayed, "user") == ["第一句"] and kinds(replayed)[-1] == "turn_end"
    assert kinds(live)[:2] == ["user", "turn_start"] and live[0]["text"] == "说说亚马孙河" and live[0]["mark"] == 2
    assert texts(live) == ["亚马孙河很长。"]
    assert latest_state(s.browser)["rewind"]["turn"] == live[0]["id"]

    saved = saved_file(s.config, conversation_id)
    assert texts(saved["events"], "user") == ["第一句", "说说亚马孙河"] and len(saved["messages"]) == 4
    assert "喜马拉雅" not in json.dumps(saved, ensure_ascii=False)
    # 搜索跟着变：去掉的那一轮（问的和答的）搜不到了，改过的搜得到
    assert search_page(s, "喜马拉雅") == [] and s.app.store.search("喜马拉雅") == []
    assert [item["id"] for item in search_page(s, "亚马孙")] == [conversation_id]
    assert [item["id"] for item in search_page(s, "第一句")] == [conversation_id]
    assert saved["title"] == "第一句"  # 改的不是第一条：标题不动


def test_editing_the_first_message(rig):
    s = rig(reply(text("旧的回答。")), reply(text("新的回答。")))
    conversation_id = say(s, "第一条")["conversation"]
    assert rewind_state(s)["mark"] == 0

    replayed, live = turn(s, "edit", mark=0, text="改过的第一条")
    assert replayed == []  # 什么都不剩：页面清空，然后是新的一轮
    assert texts(live, "user") == ["改过的第一条"] and texts(live) == ["新的回答。"]
    assert len(s.api.requests[-1]["messages"]) == 1
    saved = saved_file(s.config, conversation_id)
    assert texts(saved["events"], "user") == ["改过的第一条"] and len(saved["messages"]) == 2
    assert latest_state(s.browser)["conversation"] == conversation_id  # 还是这一段，不是新开一段


def test_the_replay_after_a_rewind_and_after_reopening(rig):
    s = rig(reply(text("答一")), reply(text("旧答二")), reply(text("新答二")))
    say(s, "问一")
    conversation_id = say(s, "问二")["conversation"]
    turn(s, "edit", mark=2, text="改过的问二")

    # 这时才打开页面：从头补发，reset 打头，然后是截断后留下的和新的一轮，最后才是 synced 标记
    late = Browser(s.url)
    backlog = late.wait_synced()
    assert backlog == len(late.events)
    shown = [event for event in late.events if event["type"] != "state"]
    assert kinds(shown) == [
        "reset", "user", "turn_start", "text", "block_end", "turn_end", "user", "turn_start", "text", "block_end", "turn_end",
    ]  # fmt: skip
    assert texts(shown, "user") == ["问一", "改过的问二"] and texts(shown) == ["答一", "新答二"]  # 旧的那一轮不在了
    assert kinds(late.events).count("state") == 1  # 过时的状态只留最后一个
    assert late.of_type("state")[0]["rewind"]["turn"] == late.of_type("user")[-1]["id"]  # 新打开的页面拿到的编号也对得上

    # 换到别的会话再打开它（从磁盘读回来）：看到的是同样的内容
    assert s.browser.post("/api/new")[0] == 200
    replayed, state = open_conversation(s, conversation_id)
    assert [content(event) for event in replayed] == [content(event) for event in shown[1:]]
    assert_ready(s, state, mark=2)


def test_rewinding_after_a_restart(rig):
    s = rig(reply(thinking("想", signature="sig-keep"), text("答一")), reply(text("旧答二")))
    say(s, "问一")
    conversation_id = say(s, "问二")["conversation"]
    original = s.api.requests[1]

    restarted = rig(reply(text("新答二")))  # 同一个数据目录：相当于把程序关掉再打开
    _, state = open_conversation(restarted, conversation_id)
    assert_ready(restarted, state, mark=2)
    _, live = turn(restarted, "retry", mark=2)
    assert texts(live) == ["新答二"]
    # 记下的位置照样指着那条消息：请求和重启之前那一次一字不差，第一轮思考的签名原样发了回去
    assert dumps(restarted.api.requests[0]) == dumps(original)
    assert restarted.api.requests[0]["messages"][1]["content"][0]["signature"] == "sig-keep"


def test_regenerating_with_an_openai_format_model(rig, monkeypatch):
    # DeepSeek（GPT、Gemini 也一样）：历史的第 0 条是系统提示词，位置从 1 数起
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    s = rig(others=[chat("答一"), chat(calls=[call("call_1", "todo_add", '{"title": "甲"}')]), chat("旧答二"), chat("新答二")])
    assert s.browser.post("/api/model", {"provider": "deepseek", "model": "deepseek-flash"})[0] == 200
    assert say(s, "问一")["rewind"]["mark"] == 1
    state = say(s, "记一下甲")
    effects = ('todo_add {"title": "甲"}',)  # 记了一条待办：改动了东西
    assert_ready(s, state, mark=3, effects=effects)
    assert s.browser.post("/api/turn/retry", target(state))[1]["effects"] == list(effects)

    _, live = turn(s, "retry", mark=3, confirmed=True)
    assert "".join(texts(live)) == "新答二"  # 这个假服务器把文字分两小段发
    assert dumps(s.other_api.requests[-1]) == dumps(s.other_api.requests[1])
    assert "call_1" not in dumps(s.other_api.requests[-1]) and s.api.requests == []


# ---- 标题 -----------------------------------------------------------------------


def test_editing_the_first_message_also_takes_the_old_text_out_of_the_title(rig):
    # 标题是照着第一句话起的：改了第一句，标题跟着换，倒回去的那句话不留在会话列表里，也搜不到
    s = rig(reply(text("旧的回答。")), reply(text("新的回答。")))
    conversation_id = say(s, "我的密码是 hunter2 帮我记住")["conversation"]
    turn(s, "edit", mark=0, text="帮我记住一件事")
    state = latest_state(s.browser)
    assert state["title"] == "帮我记住一件事"
    assert [(item["id"], item["title"]) for item in state["conversations"]] == [(conversation_id, "帮我记住一件事")]
    assert saved_file(s.config, conversation_id)["title"] == "帮我记住一件事"
    assert search_page(s, "hunter2") == [] and [item["id"] for item in search_page(s, "一件事")] == [conversation_id]


def test_a_title_you_chose_yourself_stays(rig):
    s = rig(reply(text("旧的回答。")), reply(text("新的回答。")))
    conversation_id = say(s, "第一条")["conversation"]
    assert act(s, "/api/conversation/rename", {"id": conversation_id, "title": "我起的名字"})["title"] == "我起的名字"
    turn(s, "edit", mark=0, text="改过的第一条")
    assert latest_state(s.browser)["title"] == "我起的名字"
    assert saved_file(s.config, conversation_id)["title"] == "我起的名字"


def test_a_title_taken_from_an_attachment_follows_the_edit(rig):
    # 第一条消息只发了一张图：标题是图片的名字。改成写了字的，标题用这句话
    s = rig(reply(text("看到了。")), reply(text("还是这张图。")))
    assert send_files(s, image("海边.png"), text="")[0] == 200
    s.browser.wait_for("turn_end")
    assert latest_state(s.browser)["title"] == "海边.png"
    turn(s, "edit", mark=0, text="这是哪里", keep=[0])
    assert latest_state(s.browser)["title"] == "这是哪里"


def test_editing_a_later_message_that_repeats_the_first_keeps_the_title(rig):
    # 标题是从第一条来的：后面一条恰好一字不差，改它也不换标题（见 App.retitle 的 earlier）
    s = rig(reply(text("一")), reply(text("二")), reply(text("三")))
    say(s, "你好")
    say(s, "你好")
    turn(s, "edit", mark=2, text="再见")
    assert latest_state(s.browser)["title"] == "你好"


# ---- 先确认：这一轮做过可能改动东西的事 ---------------------------------------------


def test_the_confirmation_says_the_browser_steps_cannot_be_undone(rig):
    # 这一轮在浏览器里点过：状态和 409 的回答里都带着 operated，页面据此单独写一句"撤不回来"。
    # 这里浏览器是关着的（调用会出错），但点击这种动作出了错也不算没做过：可能已经点下去了
    click = tool_use("toolu_1", "left_click", {"target": {"type": "ref", "ref": "ref_1"}}, toolset="browser")
    s = rig(reply(click, stop_reason="tool_use"), reply(text("点过了。")), reply(text("又点了一次。")))
    state = say(s, "点一下购买")
    assert state["rewind"]["operated"] == ["browser"] and state["rewind"]["effects"] == ["browser.left_click ref_1"]
    status, answer = s.browser.post("/api/turn/retry", seen(s))
    assert status == 409 and answer["operated"] == ["browser"] and answer["effects"] == ["browser.left_click ref_1"]
    _, live = turn(s, "retry", mark=0, confirmed=True)
    assert texts(live) == ["又点了一次。"]


def test_a_turn_that_ran_a_command_asks_before_rewinding(rig):
    s = rig(
        reply(text("一")),
        reply(tool_use("toolu_1", "run_command", {"command": "touch made.txt"}), stop_reason="tool_use"),
        reply(text("建好了。")),
        reply(text("新的回答。")),
    )
    say(s, "问一")
    s.browser.post("/api/message", {"text": "建个文件"})
    s.browser.post("/api/confirm", {"id": s.browser.wait_for("confirm")["id"], "allow": True})
    s.browser.wait_for("turn_end")
    shown = ("run_command $ touch made.txt",)
    assert_ready(s, latest_state(s.browser), mark=2, effects=shown)

    # 没确认：不做，回答里带着要列给你看的那几件事；什么都没变
    requests, after, here = len(s.api.requests), last_id(s), seen(s)
    for action, body in (("retry", here), ("edit", {**here, "text": "别建了"}), ("retry", {**here, "confirmed": False})):
        status, answer = s.browser.post(f"/api/turn/{action}", body)
        assert status == 409 and (answer["effects"], answer["more"]) == (list(shown), 0) and "确认" in answer["error"], answer
    assert len(s.api.requests) == requests and "reset" not in kinds(s.ui.events_after(after, timeout=0))

    # 存进文件、重新打开以后照样认得出来：文件里确认的结果合进了 confirm 的 outcome，重放时又拆成 confirm_done
    conversation_id = latest_state(s.browser)["conversation"]
    assert rewind.effects(saved_file(s.config, conversation_id)["events"]) == list(shown)
    assert s.browser.post("/api/new")[0] == 200
    _, state = open_conversation(s, conversation_id)
    assert_ready(s, state, mark=2, effects=shown)

    _, live = turn(s, "retry", mark=2, confirmed=True)
    assert texts(live) == ["新的回答。"]
    assert dumps(s.api.requests[-1]) == dumps(s.api.requests[1])  # 这一轮的第一次请求，工具调用和结果都去掉了
    assert "toolu_1" not in dumps(s.api.requests[-1])
    assert (s.config.workspace / "made.txt").exists()  # 倒回去的只是对话：建好的文件还在
    assert rewind_state(s)["effects"] == []  # 新的回答没做什么


def test_a_long_list_of_effects_is_cut_short_and_counted(rig):
    # 一步里记了八条待办：列出前几条，剩下的只给个数，"还有几项"那一句由页面自己写（换语言时能在原地跟着换）
    titles = [f"待办{n}" for n in range(8)]
    calls = [tool_use(f"toolu_{n}", "todo_add", {"title": title}) for n, title in enumerate(titles)]
    s = rig(reply(*calls, stop_reason="tool_use"), reply(text("都记下了。")), reply(text("新的回答。")))
    state = say(s, "记八条")
    lines = [f'todo_add {{"title": "{title}"}}' for title in titles]
    shown, more = lines[: MAX_REWIND_EFFECTS - 1], len(lines) - MAX_REWIND_EFFECTS + 1
    assert_ready(s, state, mark=0, effects=tuple(shown), more=more)
    assert (shown, more) == rewind.listed(lines)
    status, answer = s.browser.post("/api/turn/retry", target(state))
    assert status == 409 and (answer["effects"], answer["more"]) == (shown, more)
    _, live = turn(s, "retry", mark=0, confirmed=True)
    assert texts(live) == ["新的回答。"]


def test_auto_allowed_commands_count_too(rig):
    s = rig(reply(tool_use("toolu_1", "run_command", {"command": "true"}), stop_reason="tool_use"), reply(text("好了。")))
    assert s.browser.post("/api/approval", {"mode": "all"}) == DONE  # 全部自动允许：没有确认卡片
    say(s, "跑一下")
    assert s.browser.of_type("confirm") == []
    assert rewind_state(s)["effects"] == ["run_command $ true"]


@pytest.mark.parametrize("case", ["denied", "read", "plain"])
def test_turns_that_changed_nothing_rewind_without_asking(rig, case):
    first = {
        "denied": reply(tool_use("toolu_1", "run_command", {"command": "touch made.txt"}), stop_reason="tool_use"),
        "read": reply(tool_use("toolu_1", "str_replace_based_edit_tool", {"command": "view", "path": "notes.txt"}), stop_reason="tool_use"),
        "plain": reply(text("没用工具。")),
    }[case]
    later = [] if case == "plain" else [reply(text("好的。"))]
    s = rig(first, *later, reply(text("新的回答。")))
    (s.config.workspace / "notes.txt").write_text("笔记", encoding="utf-8")
    s.browser.post("/api/message", {"text": "做点什么"})
    if case == "denied":
        s.browser.post("/api/confirm", {"id": s.browser.wait_for("confirm")["id"], "allow": False})  # 你拒绝了：没有执行
    s.browser.wait_for("turn_end")
    assert_ready(s, latest_state(s.browser), mark=0)

    _, live = turn(s, "retry", mark=0)  # 不用 confirmed
    assert texts(live) == ["新的回答。"]
    assert not (s.config.workspace / "made.txt").exists()


# ---- 不能倒回去的时候 -----------------------------------------------------------


def test_not_while_a_turn_is_running(rig):
    s = rig(
        reply(text("一")),
        reply(tool_use("toolu_1", "run_command", {"command": "true"}), stop_reason="tool_use"),
        reply(text("好了。")),
    )
    say(s, "问一")
    earlier = seen(s)
    s.browser.post("/api/message", {"text": "跑一下"})
    confirm = s.browser.wait_for("confirm")  # 这一轮卡在确认上，还没有结束
    now = seen(s)
    for action, body in (("retry", now), ("edit", {**now, "text": "别跑了"}), ("retry", earlier)):
        status, answer = s.browser.post(f"/api/turn/{action}", body)
        assert status == 409 and "上一轮对话还没有结束" in answer["error"], answer
    s.browser.post("/api/confirm", {"id": confirm["id"], "allow": True})
    s.browser.wait_for("turn_end")
    assert len(s.api.requests) == 3


def test_not_while_a_chat_channel_holds_the_conversation(rig):
    s = rig(reply(text("一")), reply(text("新的一。")))
    conversation_id = say(s, "问一")["conversation"]
    assert s.web.claims.take(conversation_id, "telegram:1") is None  # Telegram 那边正在这段会话里跑一轮

    after = last_id(s)
    s.web.publish_state()
    state = wait_event(s.browser, "state", after)
    assert state["rewind"]["ok"] is False and "Telegram" in state["rewind"]["reason"]["text"]
    assert state["rewind"]["reason"]["key"] == "这段会话正在 {platform} 上进行。等那边这一轮结束再试。"  # 换语言时页面照着原文重译
    for action, body in (("retry", target(state)), ("edit", {**target(state), "text": "改"})):
        status, answer = s.browser.post(f"/api/turn/{action}", body)
        assert status == 409 and "Telegram" in answer["error"], answer
    assert len(s.api.requests) == 1
    assert s.web.claims.holder(conversation_id) == "telegram:1"  # 被拒绝的请求不会把别人的认领放掉

    s.web.claims.release(conversation_id, "telegram:1")
    _, live = turn(s, "retry", mark=0)
    assert texts(live) == ["新的一。"]
    assert s.web.wait_idle(5) and s.web.claims.holder(conversation_id) is None  # 跑完放开


def test_not_while_its_task_is_running(rig, monkeypatch):
    s = rig(reply(text("一")))
    say(s, "问一")
    s.app.conversation.task = "task-1"  # 这段会话是定时任务跑出来的，你在里面接着聊了一句
    monkeypatch.setattr(s.web.tasks, "running", lambda task_id: "每日简报" if task_id == "task-1" else "")
    status, answer = s.browser.post("/api/turn/retry", seen(s))
    assert status == 409 and "定时任务“每日简报”正在运行" in answer["error"]
    real = type(s.web.tasks).running  # 真的那一个：没有这个任务、或者不是任务的会话，都是空的
    assert real(s.web.tasks, "") == "" and real(s.web.tasks, "task-1") == ""
    assert len(s.api.requests) == 1


def test_a_stale_target_is_refused(rig):
    # 页面上看到的那一轮已经不是最后一轮了（聊天通道刚聊过一轮、编号不对、会话不对）
    s = rig(reply(text("一")), reply(text("二")))
    say(s, "问一")
    old = seen(s)  # 第一轮结束时页面看到的
    say(s, "问二")
    now = seen(s)
    assert now["conversation"] == old["conversation"] and now["turn"] != old["turn"]
    stale = [
        ("retry", old),
        ("edit", {**old, "text": "改"}),
        ("retry", {**now, "turn": now["turn"] + 1}),
        ("retry", {**now, "turn": now["turn"] - 1}),  # 不是 user 事件
        ("retry", {**now, "turn": -1}),
        ("retry", {**now, "conversation": MISSING}),
        ("edit", {**now, "conversation": "", "text": "改"}),
    ]
    for action, body in stale:
        after = last_id(s)
        status, answer = s.browser.post(f"/api/turn/{action}", body)
        assert status == 409 and "对话刚刚变过了" in answer["error"], (body, answer)
        fresh = wait_event(s.browser, "state", after)["rewind"]  # 先把最新的发过去
        assert (fresh["turn"], fresh["mark"]) == (now["turn"], 2)
    assert len(s.api.requests) == 2


def test_a_tab_that_missed_a_rewind_is_refused(rig):
    # 编辑、重新生成以后最后一轮在历史里的位置不变，但重放过了，它的 user 事件换了新的编号
    s = rig(reply(text("一")), reply(text("旧二")), reply(text("改过以后的回答。")), reply(text("不该有这一次。")))
    say(s, "问一")
    say(s, "问二")
    tab_a = seen(s)  # 标签页 A 看到的
    turn(s, "edit", mark=2, text="改过的问二")  # 标签页 B 刚把最后一条改掉了
    assert rewind_state(s)["mark"] == 2 and seen(s)["turn"] != tab_a["turn"]
    # 标签页 A 的事件流断过（电脑刚从休眠里醒来），还停在旧画面上，点了"重新生成"
    status, answer = s.browser.post("/api/turn/retry", tab_a)
    assert status == 409 and "对话刚刚变过了" in answer["error"], answer
    assert len(s.api.requests) == 3


def test_a_tab_still_showing_another_conversation_is_refused(rig):
    s = rig(reply(text("X1")), reply(text("X2")), reply(text("Y1")), reply(text("Y2")), reply(text("不该有这一次。")))
    say(s, "x 一")
    tab_a = target(say(s, "x 二"))  # 标签页 A 看着会话 X
    assert s.browser.post("/api/new")[0] == 200  # 标签页 B 换到了新的一段 Y，也聊了两句
    say(s, "y 一")
    state = say(s, "y 二")
    assert state["conversation"] != tab_a["conversation"] and state["rewind"]["mark"] == 2  # 最后一轮的位置恰好一样
    status, answer = s.browser.post("/api/turn/retry", tab_a)  # A 还没收到换会话的事件就点了
    assert status == 409 and "对话刚刚变过了" in answer["error"], answer
    status, answer = s.browser.post("/api/turn/retry", {**tab_a, "turn": state["rewind"]["turn"]})  # 编号对上了、会话不对也不行
    assert status == 409, answer
    assert len(s.api.requests) == 4


def test_nothing_to_rewind_in_an_empty_conversation(rig):
    s = rig()
    state = latest_state(s.browser)
    assert state["rewind"] is None  # 一条用户消息都没有：页面上没有按钮
    status, answer = s.browser.post("/api/turn/retry", {"conversation": state["conversation"], "turn": 1})
    assert status == 409 and "对话刚刚变过了" in answer["error"]


def test_bad_requests_are_rejected(rig):
    s = rig(reply(text("一")))
    say(s, "问一")
    good = seen(s)
    bad = [
        ("retry", {}),
        ("retry", {"mark": 0}),  # 以前的写法
        ("retry", {"turn": good["turn"]}),
        ("retry", {"conversation": good["conversation"]}),
        ("retry", {**good, "conversation": 5}),
        ("retry", {**good, "conversation": None}),
        ("retry", {**good, "turn": str(good["turn"])}),
        ("retry", {**good, "turn": True}),
        ("retry", {**good, "turn": float(good["turn"])}),
        ("retry", {**good, "turn": None}),
        ("retry", {**good, "confirmed": "yes"}),
        ("edit", {**good, "text": 5}),
        ("edit", {**good, "text": "x" * (MAX_MESSAGE_CHARS + 1)}),
        ("edit", {**good, "text": "改", "keep": "0"}),
        ("edit", {**good, "text": "改", "keep": [0, 0]}),
        ("edit", {**good, "text": "改", "keep": [True]}),
        ("edit", {**good, "text": "改", "keep": [0.5]}),
        ("edit", {**good, "text": "改", "keep": list(range(MAX_ATTACHMENTS + 1))}),
    ]
    for action, body in bad:
        assert s.browser.post(f"/api/turn/{action}", body) == (400, {"error": "参数不正确"}), body
    assert s.browser.post("/api/turn/other", good)[0] == 404
    # 格式对、但做不了的：改成空的、附件的序号不存在
    status, answer = s.browser.post("/api/turn/edit", {**good, "text": "   "})
    assert status == 409 and "消息不能为空" in answer["error"]
    status, answer = s.browser.post("/api/turn/edit", {**good, "text": "改", "keep": [0]})
    assert status == 409 and answer["error"] == "参数不正确"
    assert len(s.api.requests) == 1 and s.web.wait_idle(1)  # 锁都放开了


def test_turns_without_a_mark_cannot_be_rewound(rig):
    # 这个功能之前存的会话（没有记位置）、定时任务自己跑的那一轮（发给模型的指令和显示的不一样）
    s = rig(reply(text("一")))
    say(s, "问一")
    store = s.app.store
    for task, reason in (("", "旧版本的智狐"), ("task-1", "定时任务自己跑的那一轮")):
        old = store.new("anthropic", "claude-opus-5-5")
        old.title, old.task = "旧的", task
        old.messages = [{"role": "user", "content": [{"type": "text", "text": "问"}]}, {"role": "assistant", "content": [{"type": "text", "text": "答"}]}]
        old.events = [{"type": "user", "text": "问"}, {"type": "turn_start"}, {"type": "text", "text": "答"}, {"type": "turn_end"}]
        store.save(old)
        _, state = open_conversation(s, old.id)
        shown = state["rewind"]
        assert shown["mark"] is None and shown["ok"] is False and shown["turn"] == last_user_id(s)
        assert reason in shown["reason"]["text"]
        status, answer = s.browser.post("/api/turn/retry", target(state))
        assert status == 409 and "对话刚刚变过了" in answer["error"]  # 没有记位置：无论如何都倒不回去


def test_a_turn_folded_into_a_summary_cannot_be_rewound(rig, monkeypatch):
    # 正常的循环不会把最后一轮压缩掉（正在进行的那一轮永远原样留着，见 compaction.find_cut）。
    # 这里直接写出这样一个会话文件：最后一轮的位置落在摘要上。按钮是灰的，接口也拒绝，不会去动摘要
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    s = rig(reply(text("一")))
    store = s.app.store
    old = store.new("deepseek", "deepseek-flash")
    old.title = "压缩过的"
    history = [
        {"role": "system", "content": "提示词"},
        {"role": "user", "content": "第一轮"},
        {"role": "assistant", "content": "一"},
        {"role": "user", "content": "第二轮"},
        {"role": "assistant", "content": "二"},
    ]
    old.messages = compaction.compacted(history, 3, "用户问过第一轮。")[1:]
    old.dropped = 2
    old.events = [{"type": "user", "text": "第一轮", "mark": 1}, {"type": "turn_start"}, {"type": "text", "text": "一"}, {"type": "turn_end"}]
    store.save(old)

    _, state = open_conversation(s, old.id)
    backend = s.app.agent.backend
    assert backend.dropped() == 2 and not backend.turn_at(1) and backend.mark() > 1
    assert state["rewind"]["mark"] == 1 and state["rewind"]["ok"] is False
    assert "压缩进了摘要" in state["rewind"]["reason"]["text"]
    for action, body in (("retry", target(state)), ("edit", {**target(state), "text": "改"})):
        status, answer = s.browser.post(f"/api/turn/{action}", body)
        assert status == 409 and "压缩进了摘要" in answer["error"], answer
    assert s.other_api.requests == [] and compaction.is_summary(backend.messages[1])


def test_no_key_no_rewind(rig, monkeypatch):
    s = rig(reply(text("一")))
    say(s, "问一")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    status, answer = s.browser.post("/api/turn/retry", seen(s))
    assert status == 409 and "API 密钥" in answer["error"]


# ---- 在模型答出第一步之前就失败了的那一轮：再试一次 -----------------------------------


def test_retrying_a_failed_turn_sends_the_same_message_and_attachments(rig):
    s = rig()  # 没有预设回复：假 API 返回 500
    picture, notes = image(), text_file("笔记.md", "第一行\n第二行")
    assert send_files(s, picture, notes, text="看看这些")[0] == 200
    s.browser.wait_for("turn_end")
    assert "500" in s.browser.of_type("notice")[-1]["text"]
    failed = s.api.requests[0]
    assert s.app.agent.messages == []  # 循环把这条消息撤回了，页面上还看得到
    assert_ready(s, latest_state(s.browser), mark=0)

    s.api.replies.append(reply(text("都看到了。")))
    replayed, live = turn(s, "retry", mark=0)
    # 同样的文字、同样的附件（从多媒体库取回原件）又发了一次
    assert dumps(s.api.requests[1]) == dumps(failed)
    assert texts(replayed, "user") == ["看看这些"] and "user" not in kinds(live)  # 用户消息只有一条
    assert texts(live) == ["都看到了。"]
    events = s.ui.events_after(0, timeout=0)
    assert texts(events, "user") == ["看看这些"]
    (shown,) = [event for event in events if event["type"] == "user"]
    assert [item["name"] for item in shown["attachments"]] == ["截图.png", "笔记.md"] and shown["mark"] == 0
    saved = saved_file(s.config, latest_state(s.browser)["conversation"])
    assert texts(saved["events"], "user") == ["看看这些"] and len(saved["messages"]) == 2


def test_a_failed_regeneration_can_be_tried_again(rig):
    s = rig(reply(text("答一")))
    say(s, "问一")
    _, live = turn(s, "retry", mark=0)  # 没有回复了：这一次失败
    assert "500" in texts(live, "notice")[-1] and texts(live) == []
    assert s.app.agent.messages == []
    assert_ready(s, latest_state(s.browser), mark=0)

    s.api.replies.append(reply(text("又答了一次。")))
    _, live = turn(s, "retry", mark=0)
    assert texts(live) == ["又答了一次。"]
    assert s.api.requests[-1]["messages"][0]["content"][-1]["text"] == "问一"
    assert texts(s.ui.events_after(0, timeout=0), "user") == ["问一"]


# ---- 附件和多媒体库 ---------------------------------------------------------------


def test_editing_a_message_with_attachments_keeps_the_media_library_in_step(rig):
    s = rig(reply(text("看到甲了。")), reply(text("看到乙和丙了。")), reply(text("只看到丙。")))
    assert send_files(s, image("甲.png"), text="第一条")[0] == 200
    s.browser.wait_for("turn_end")
    photo = image("丙.jpg", data=b64(JPEG_BYTES), media_type="image/jpeg")
    assert send_files(s, text_file("乙.txt", "乙的内容"), photo, text="第二条")[0] == 200
    s.browser.wait_for("turn_end")
    conversation_id = latest_state(s.browser)["conversation"]
    listed = [(item["id"], item["name"]) for item in media_list(s)["items"]]
    assert listed == [(f"{conversation_id}/1/0", "乙.txt"), (f"{conversation_id}/1/1", "丙.jpg"), (f"{conversation_id}/0/0", "甲.png")]

    # 改第二条消息，附件只留第二个（丙），乙去掉
    _, live = turn(s, "edit", mark=2, text="第二条改过", keep=[1])
    sent = s.api.requests[-1]["messages"][-1]["content"]
    assert [block["type"] for block in sent] == ["image", "text", "text"] and sent[0]["source"]["data"] == b64(JPEG_BYTES)
    assert sent[-1]["text"] == "第二条改过" and "乙的内容" not in dumps(s.api.requests[-1])
    assert [item["name"] for item in live[0]["attachments"]] == ["丙.jpg"]

    # 第几排还是按带附件的消息数：甲还是第 0 排，丙现在是第 1 排的第 0 张；乙不在了
    def check() -> None:
        listed = [(item["id"], item["name"]) for item in media_list(s)["items"]]
        assert listed == [(f"{conversation_id}/1/0", "丙.jpg"), (f"{conversation_id}/0/0", "甲.png")]
        assert media_file(s, f"{conversation_id}/1/0")[2] == JPEG_BYTES
        assert media_file(s, f"{conversation_id}/0/0")[2] == PNG_BYTES
        assert media_file(s, f"{conversation_id}/1/1")[0] == 409

    check()  # 当前这一段：内存里的样子
    names = [entry.name for entry in s.app.store.media().snapshot()[conversation_id][1]]
    assert names == ["甲.png", "丙.jpg"]  # 存进文件的索引
    assert s.browser.post("/api/new")[0] == 200
    check()  # 换到别的会话以后：索引里的样子


def test_editing_away_every_attachment_needs_some_text(rig):
    s = rig(reply(text("看到了。")))
    assert send_files(s, image(), text="")[0] == 200  # 只发了一张图
    s.browser.wait_for("turn_end")
    status, answer = s.browser.post("/api/turn/edit", {**seen(s), "text": "", "keep": []})
    assert status == 409 and "消息不能为空" in answer["error"]


# ---- 花掉的钱 ---------------------------------------------------------------------


def test_spending_is_never_taken_back(rig):
    s = rig(reply(text("一")), reply(text("旧二")), reply(text("新二")))
    say(s, "问一")
    before = say(s, "问二")
    turn(s, "retry", mark=2)

    state = latest_state(s.browser)
    assert state["usage"]["requests"] == 3 and state["cost"] > before["cost"]  # 信息栏里的用量和费用照样算着
    rows = [json.loads(line) for line in s.config.ledger_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 3  # 账本一笔不少：去掉的那一轮的那一次调用也在
    assert s.app.agent.usage.requests == 3
