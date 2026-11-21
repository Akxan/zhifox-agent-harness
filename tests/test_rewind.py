"""倒回最后一轮（编辑最后一条消息、重新生成最后一个回答，见 harness/rewind.py）：循环、适配层和几个小函数。

用真实的 SDK 连本地的假服务器（fake_api.py 是 Claude 的格式，fake_openai.py 是 GPT、Gemini、DeepSeek 共用的格式），
所以这里看的是真正发出去的请求：倒回去以后，前面的内容是不是一字不差（缓存照样命中、思考的签名照样对得上），
去掉的那一轮是不是一点不剩（没有落单的工具调用和结果，API 才不会拒绝）。

网页上的按钮、事件流、索引在 test_rewind_web.py 里测，终端里的 /retry、/edit 在 test_rewind_cli.py 里测。
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import anthropic
import openai
import pytest
from fake_api import FakeAPI, failure, reply, text, thinking, tool_use
from fake_openai import FakeOpenAI, call, chat
from fake_openai import failure as openai_failure
from sample_files import PNG, image, text_file
from test_compaction import BIG, SUMMARY_USAGE, assert_tool_calls_answered

from harness import rewind
from harness.app import build_app, retry_turn, run_turn
from harness.attachments import parse
from harness.config import MAX_REWIND_EFFECTS
from harness.conversations import ConversationError, ConversationStore, make_title
from harness.media import MediaError
from harness.providers import ProviderError, compaction
from harness.providers.base import ToolResult
from harness.tools.base import ToolImage
from harness.webui.ui import WebUI

NOW = datetime(2026, 11, 20, 14, 3)  # 和 conftest 里的 agent 用的是同一个钟
LATER = datetime(2026, 11, 21, 9, 30)


def dumps(value: object) -> str:
    """一字不差地比较：键排好序，中文原样。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def assert_claude_pairs(messages: list[dict]) -> None:
    """Claude 的格式：每个 tool_use 后面紧跟的那条用户消息里正好是它们的结果，每个结果前面都有它的调用。"""
    waiting: set[str] = set()
    for message in messages:
        blocks = message["content"] if isinstance(message["content"], list) else []
        results = {block["tool_use_id"] for block in blocks if block.get("type") == "tool_result"}
        if message["role"] == "assistant":
            assert not waiting, f"这些调用没有结果：{waiting}"
            waiting = {block["id"] for block in blocks if block.get("type") == "tool_use"}
        else:
            assert results == waiting, f"结果和调用对不上：{results} / {waiting}"
            waiting = set()
    assert not waiting, f"这些调用没有结果：{waiting}"


@pytest.fixture(params=["claude", "openai"])
def form(request, start, start_openai):
    """两种格式各跑一遍。answer：一条只有文字的回复；tool：一次工具调用（记一条待办，不用确认）；
    said：一条用户消息里用户自己写的那句话；pairs：检查工具调用和结果是不是成对的。"""
    if request.param == "claude":
        return SimpleNamespace(
            start=start,
            answer=lambda words: reply(text(words)),
            tool=lambda id: reply(tool_use(id, "todo_add", {"title": "买榴莲"}), stop_reason="tool_use"),
            said=lambda message: message["content"][-1]["text"],
            pairs=assert_claude_pairs,
        )
    return SimpleNamespace(
        start=start_openai,
        answer=lambda words: chat(words),
        tool=lambda id: chat(calls=[call(id, "todo_add", '{"title": "买榴莲"}')]),
        said=lambda message: message["content"].split("\n\n")[-1],
        pairs=assert_tool_calls_answered,
    )


# ---- 重新生成：用户消息原样留着，模型这一轮的东西全部去掉 -----------------------------


def test_regenerating_sends_exactly_the_request_that_started_the_turn(form):
    h = form.start(
        form.answer("冰箱里有牛奶。"),
        form.tool("call_old"),  # 这一轮先调用了一次工具
        form.answer("旧的回答：记下了。"),
        form.answer("新的回答：好的。"),
    )
    h.agent.run_turn("冰箱里有什么")
    mark = h.agent.backend.mark()
    h.agent.run_turn("记一下买榴莲")
    started = h.api.requests[1]  # 这一轮的第一次请求
    assert len(h.api.requests) == 3 and h.agent.backend.turn_at(mark)

    h.agent.clock = lambda: LATER  # 钟走了也不要紧：用户消息连同当时的时间说明原样留着
    h.agent.retry_turn(mark)

    again = h.api.requests[3]
    # 整个请求和这一轮当初的第一次一字不差：系统提示词、工具说明书、到这条用户消息为止的历史，所以缓存照样命中
    assert dumps(again) == dumps(started)
    sent = dumps(again)
    assert "call_old" not in sent and "旧的回答" not in sent  # 去掉的那一轮：工具调用、结果、回答都不在了
    form.pairs(again["messages"])
    # 历史里：前面原样，这一轮只剩用户消息和新的回答
    assert form.said(h.agent.messages[mark]) == "记一下买榴莲"
    assert len(h.agent.messages) == mark + 2 and "新的回答：好的。" in dumps(h.agent.messages[-1])
    form.pairs(h.agent.messages)
    assert h.agent.outcome == "done"


def test_regenerating_a_turn_that_used_tools_again_leaves_no_orphans(form):
    # 新的回答也调用了工具：调用和结果照样成对，旧的那一对一点不剩
    h = form.start(form.tool("call_old"), form.answer("旧的。"), form.tool("call_new"), form.answer("新的。"))
    mark = h.agent.backend.mark()
    h.agent.run_turn("记一下")
    h.agent.retry_turn(mark)

    for request in h.api.requests:
        form.pairs(request["messages"])
    assert "call_old" not in dumps(h.api.requests[2:]) and "call_old" not in dumps(h.agent.messages)
    assert "call_new" in dumps(h.agent.messages)
    form.pairs(h.agent.messages)


def test_earlier_thinking_goes_back_byte_for_byte_and_the_regenerated_turns_is_gone(start):
    h = start(
        reply(thinking("先想想第一问", signature="sig-keep"), text("答一")),
        reply(thinking("旧的思考", signature="sig-drop"), tool_use("toolu_old", "todo_add", {"title": "甲"}), stop_reason="tool_use"),
        reply(thinking("看了结果", signature="sig-drop-2"), text("旧答二")),
        reply(thinking("新的思考", signature="sig-new"), text("新答二")),
    )
    h.agent.run_turn("问一")
    kept = dumps(h.agent.messages[1])  # 第一轮的回答，思考块带着签名
    mark = h.agent.backend.mark()
    h.agent.run_turn("问二")
    h.agent.retry_turn(mark)

    again = h.api.requests[-1]
    assert dumps(again) == dumps(h.api.requests[1])  # 和这一轮当初的第一次请求一字不差
    assert again["messages"][-1]["content"][-1]["text"] == "问二"
    # 签名和它前面的全部内容绑在一起：前面一个字都没动，所以原样发回去，API 照样认
    assert dumps(again["messages"][1]) == kept
    block = again["messages"][1]["content"][0]
    assert (block["type"], block["thinking"], block["signature"]) == ("thinking", "先想想第一问", "sig-keep")
    sent = dumps(again)
    for leftover in ("sig-drop", "旧的思考", "toolu_old", "看了结果", "旧答二"):
        assert leftover not in sent, leftover
    assert h.agent.messages[-1]["content"][0]["signature"] == "sig-new"
    assert_claude_pairs(h.agent.messages)


def test_earlier_reasoning_is_kept_for_deepseek(start_openai):
    # DeepSeek 要求把思考内容（reasoning_content）原样发回去：前面一轮的留着，重新生成的那一轮的去掉
    h = start_openai(chat("答一", reasoning="第一轮的推理"), chat("旧答二", reasoning="旧的推理"), chat("新答二", reasoning="新的推理"))
    h.agent.run_turn("问一")
    mark = h.agent.backend.mark()
    h.agent.run_turn("问二")
    h.agent.retry_turn(mark)

    again = h.api.requests[-1]
    assert dumps(again) == dumps(h.api.requests[1])
    assert again["messages"][2] == {"role": "assistant", "content": "答一", "reasoning_content": "第一轮的推理"}
    assert "旧的推理" not in dumps(again) and h.agent.messages[-1]["reasoning_content"] == "新的推理"


# ---- 编辑：连那条用户消息一起倒回去，换成改过的再发 -------------------------------------


def test_editing_replaces_only_the_last_message(form):
    h = form.start(form.answer("一"), form.tool("call_old"), form.answer("记下了榴莲。"), form.answer("记下了山竹。"))
    h.agent.run_turn("第一句")
    before = dumps(h.agent.messages)
    mark = h.agent.backend.mark()
    h.agent.run_turn("记一下买榴莲")
    h.agent.clock = lambda: LATER

    h.agent.backend.rollback(mark)  # 编辑：网页和终端都是这样倒回去、再当作新的一轮发出去
    h.agent.run_turn("记一下买山竹")

    edited = h.api.requests[-1]
    assert dumps(edited["messages"][:mark]) == before  # 前面的一条都没动
    assert dumps(edited["messages"][:mark]) == dumps(h.api.requests[1]["messages"][:mark])
    # 系统提示词、工具说明书、模型和别的参数也都没变
    assert dumps({key: value for key, value in edited.items() if key != "messages"}) == dumps(
        {key: value for key, value in h.api.requests[1].items() if key != "messages"}
    )
    (last,) = edited["messages"][mark:]
    assert last["role"] == "user" and form.said(last) == "记一下买山竹"
    assert "<context>当前时间：2026-11-21 09:30 星期六</context>" in dumps(last)  # 新的一轮：时间说明是现在的
    sent = dumps(edited)
    assert "榴莲" not in sent and "call_old" not in sent and "14:03" not in dumps(last)
    form.pairs(edited["messages"])


# ---- 重新打开、重启之后：记下的位置照样对得上 ------------------------------------------


def launch(config, client) -> tuple:
    """组装一个连着网页界面的助手（每次调用都相当于启动一次程序）。"""
    ui = WebUI()
    app = build_app(config, ui, client_factory=lambda provider: client)
    app.agent.clock = lambda: NOW
    return app, ui


def web_turn(app, ui, words: str) -> None:
    """和网页界面一样跑一轮：在 user 事件上记下这一轮开始的位置，跑完存盘。"""
    app.name_conversation(words)
    ui.begin_turn(words, mark=app.agent.backend.mark())
    run_turn(app.agent, ui, words)
    ui.end_turn()
    assert app.save_conversation(ui.export_events())


def saved_file(config, conversation_id: str) -> dict:
    return json.loads((config.conversations_dir / f"{conversation_id}.json").read_text(encoding="utf-8"))


def test_marks_still_point_at_the_right_message_after_a_restart(make_config):
    api = FakeAPI([reply(thinking("想", signature="sig-1"), text("答一")), reply(text("旧答二")), reply(text("新答二"))]).start()
    try:
        config = make_config()
        client = anthropic.Anthropic(api_key="test-key", base_url=api.base_url, max_retries=0)
        app, ui = launch(config, client)
        web_turn(app, ui, "问一")
        web_turn(app, ui, "问二")
        conversation_id = app.conversation.id
        assert "dropped" not in saved_file(config, conversation_id)  # Claude 的历史从不在本地压缩：不写这一项

        app, ui = launch(config, client)  # 重新启动程序，打开这段会话
        conversation = app.open_conversation(conversation_id)
        turn = rewind.last_turn(conversation.events)
        assert (turn.mark, turn.text) == (2, "问二") and conversation.events[turn.index]["text"] == "问二"
        assert conversation.dropped == 0 and app.agent.backend.turn_at(turn.mark)
        assert app.agent.messages[turn.mark]["content"][-1]["text"] == "问二"
        assert not app.agent.backend.turn_at(1) and not app.agent.backend.turn_at(99)  # 回答、不存在的位置都不是一轮的开头

        retry_turn(app.agent, ui, turn.mark)
        assert dumps(api.requests[2]) == dumps(api.requests[1])  # 重启以后也一字不差
    finally:
        api.stop()


def test_marks_survive_compaction_and_a_restart(make_config):
    # 第一轮带一次工具调用（四条消息），压缩时换成两条摘要：历史短了两条，Conversation.dropped 记着这个数
    api = FakeOpenAI(
        [
            chat(calls=[call("call_1", "todo_list", "{}")]),
            chat("一"),
            chat("二", usage=BIG),
            chat("前面聊了一和二。", usage=SUMMARY_USAGE),  # 第三轮之前先压缩
            chat("旧答三"),
            chat("新答三"),
        ]
    ).start()
    try:
        config = make_config(default_provider="deepseek", default_model="deepseek-flash")
        client = openai.OpenAI(api_key="test-key", base_url=api.base_url, max_retries=0)
        app, ui = launch(config, client)
        for words in ("第一轮", "第二轮", "第三轮"):
            web_turn(app, ui, words)
        backend = app.agent.backend
        # 位置从对话开始数（系统提示词是第 0 条），压缩掉的也算在内
        assert [event["mark"] for event in ui.export_events() if event["type"] == "user"] == [1, 5, 7]
        assert backend.dropped() == 2 and backend.mark() == 9
        assert backend.turn_at(5) and backend.turn_at(7)
        assert not backend.turn_at(1)  # 第一轮已经压缩进了摘要：倒不回去
        conversation_id = app.conversation.id
        assert saved_file(config, conversation_id)["dropped"] == 2
        compacted_request = api.requests[4]  # 第三轮真正的那次请求（压缩之后）

        app, ui = launch(config, client)  # 重启
        conversation = app.open_conversation(conversation_id)
        backend = app.agent.backend
        assert conversation.dropped == 2 and backend.dropped() == 2 and backend.mark() == 9
        turn = rewind.last_turn(conversation.events)
        assert turn.mark == 7 and backend.turn_at(7) and backend.turn_at(5) and not backend.turn_at(1)
        assert backend.messages[7 - 2]["content"].endswith("第三轮")

        retry_turn(app.agent, ui, turn.mark)
        assert dumps(api.requests[5]) == dumps(compacted_request)
        assert app.save_conversation(ui.export_events())
        assert saved_file(config, conversation_id)["dropped"] == 2
    finally:
        api.stop()


def test_a_negative_dropped_count_is_a_broken_file(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    conversation = store.new("deepseek", "deepseek-flash")
    conversation.messages = [{"role": "user", "content": "问"}]
    conversation.dropped = 3
    store.save(conversation)
    assert store.load(conversation.id).dropped == 3
    path = tmp_path / "conversations" / f"{conversation.id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**data, "dropped": -1}), encoding="utf-8")
    with pytest.raises(ConversationError):
        ConversationStore(tmp_path / "conversations").load(conversation.id)


def test_editing_the_first_message_is_saved_even_before_the_new_turn(make_config):
    # 第一条消息倒回去以后历史是空的，可这段会话存过盘：磁盘上要跟着变，不然重新打开时那一轮又回来了
    api = FakeAPI([reply(text("答一"))]).start()
    try:
        config = make_config()
        client = anthropic.Anthropic(api_key="test-key", base_url=api.base_url, max_retries=0)
        app, ui = launch(config, client)
        web_turn(app, ui, "问一")
        conversation_id = app.conversation.id

        app.agent.backend.rollback(0)
        events = ui.rewind(keep_user=False)
        assert events == [] and app.agent.messages == []
        assert app.save_conversation(events) is True
        saved = saved_file(config, conversation_id)
        assert saved["messages"] == [] and saved["events"] == []

        app, ui = launch(config, client)
        assert app.open_conversation(conversation_id).events == [] and app.agent.messages == []
    finally:
        api.stop()


def test_rewinding_the_page_log_keeps_or_drops_the_user_event():
    ui = WebUI()
    for words, mark in (("问一", 0), ("问二", 2)):
        ui.begin_turn(words, mark=mark)
        ui.text("答" + words)
        ui.end_turn()

    kept = ui.rewind(keep_user=True)  # 重新生成：留着那条用户消息
    assert [(event["type"], event.get("text")) for event in kept][-2:] == [("turn_end", None), ("user", "问二")]
    assert kept[-1]["mark"] == 2
    log = ui.events_after(0, timeout=0)
    assert log[0]["type"] == "reset" and [event["type"] for event in log].count("user") == 2
    assert ui.last_turn_events()[0] == len(log) - 1  # 最后一个 user 事件在日志里是第几个

    dropped = ui.rewind(keep_user=False)  # 编辑：连它一起去掉
    assert [event.get("text") for event in dropped if event["type"] == "user"] == ["问一"]
    assert dropped[-1]["type"] == "turn_end"
    ui.begin_again()  # 重新生成时只开始新的一轮，不再显示一遍用户消息
    assert ui.events_after(0, timeout=0)[-1]["type"] == "turn_start"


# ---- 压缩 -------------------------------------------------------------------


def test_the_picture_message_after_tool_results_is_not_a_turn():
    # 工具结果里有图片时，适配层紧跟着结果补一条 role 为 user 的消息（OpenAI 格式里图片只能放在用户消息里）。
    # 它不是一轮的开头：当成一轮的话，正在进行的这一轮会被从中间切开，开头那条用户消息被写进摘要
    pictures = {
        "role": "user",
        "content": [{"type": "text", "text": compaction.TOOL_IMAGES_NOTE}, {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}}],
    }

    def shot(id: str) -> list[dict]:
        uses = [{"id": id, "type": "function", "function": {"name": "browser_screenshot", "arguments": "{}"}}]
        return [{"role": "assistant", "content": None, "tool_calls": uses}, {"role": "tool", "tool_call_id": id, "content": "截好了"}, pictures]

    messages = [
        {"role": "system", "content": "提示词"},
        {"role": "user", "content": "问一"},
        {"role": "assistant", "content": "答一"},
        {"role": "user", "content": "截两张图"},  # 正在进行的这一轮
        *shot("call_1"),
        *shot("call_2"),
    ]
    assert not compaction.is_turn_start(pictures)
    assert compaction.is_turn_start(messages[3]) and compaction.is_turn_start({"role": "user", "content": [{"type": "text", "text": "问"}]})
    assert not compaction.is_turn_start(compaction.compacted(messages, 3, "摘要")[1])  # 摘要也不是
    assert not compaction.is_turn_start(messages[2]) and not compaction.is_turn_start(messages[5])

    # 只留一轮：切口落在这一轮的开头，不落在任何一条图片消息上；两轮都要留的话没有可以压缩的
    assert compaction.find_cut(messages, keep_turns=1) == 3
    assert compaction.find_cut(messages, keep_turns=2) is None
    assert messages.index(pictures) not in {compaction.find_cut(messages, keep_turns=n) for n in range(4)}


def test_the_adapter_does_not_count_the_picture_message_as_a_turn(start_openai):
    h = start_openai()
    assert h.app.select_model("openai", "gpt-6-luna") is False  # GPT 看得了图片：结果里的图片会补成一条用户消息
    backend = h.agent.backend
    mark = backend.mark()
    backend.add_user(["<context>…</context>", "截个图"])
    uses = [{"id": "call_1", "type": "function", "function": {"name": "browser_screenshot", "arguments": "{}"}}]
    backend.messages.append({"role": "assistant", "content": None, "tool_calls": uses})
    backend.add_tool_results([ToolResult("call_1", "截好了", images=(ToolImage("image/png", PNG),))])

    assert backend.messages[-1]["content"][0]["text"] == compaction.TOOL_IMAGES_NOTE
    assert backend.turn_at(mark)
    assert not backend.turn_at(backend.mark() - 1)  # 图片那一条
    assert not backend.turn_at(backend.mark() - 2)  # 工具结果
    assert not backend.turn_at(0)  # 系统提示词


def test_retrying_after_a_failed_summary_tries_to_compact_again(start_openai):
    # 这一轮里压缩失败过，这一轮就不再试（_compaction_failed）。重新生成是新的一次尝试：倒回去时这个记录也清掉
    h = start_openai(
        chat("一"),
        chat("二", usage=BIG),
        openai_failure(503, "overloaded"),  # 第三轮之前该压缩了，但写摘要失败
        chat("旧答三", usage=BIG),  # 这一轮照常答完
        chat("前面聊了一和二。", usage=SUMMARY_USAGE),  # 重新生成：先压缩，这次成了
        chat("新答三"),
    )
    h.agent.run_turn("第一轮")
    h.agent.run_turn("第二轮")
    mark = h.agent.backend.mark()
    h.agent.run_turn("第三轮")
    assert h.agent.backend.dropped() == 0

    h.agent.retry_turn(mark)
    summary_request, last = h.api.requests[4:]
    assert summary_request["messages"][-1]["content"] == compaction.SUMMARY_REQUEST
    assert compaction.is_summary(last["messages"][1]) and last["messages"][-1]["content"].endswith("第三轮")
    assert h.agent.messages[-1]["content"] == "新答三"
    # 这一轮的开头没有被压缩掉：它的位置照样指着它，可以再重新生成一次
    assert h.agent.backend.turn_at(mark) and h.agent.backend.mark() == mark + 2


# ---- 在模型答出第一步之前就失败了的那一轮 ----------------------------------------------


def test_a_turn_that_failed_before_the_first_step_is_sent_again_with_its_attachments(start):
    h = start(failure(529, "overloaded_error", "Overloaded"))
    attachments, problem = h.app.media.keep(parse([image(), text_file("笔记.md", "第一行\n第二行")]))
    assert problem == "" and all(item.sha256 for item in attachments)
    mark = h.agent.backend.mark()
    with pytest.raises(ProviderError):
        h.agent.run_turn("看看这些", attachments)
    failed = h.api.requests[0]
    # 循环把这条消息撤回了（页面上却还看得到它）：位置上没有它，但位置就是末尾
    assert h.agent.messages == [] and h.agent.backend.mark() == mark and not h.agent.backend.turn_at(mark)

    shown = [{"type": "user", "text": "看看这些", "attachments": [item.summary() for item in attachments], "mark": mark}]
    turn = rewind.last_turn(shown)
    again = rewind.attachments_of(turn, None, h.app.media, h.agent.backend.export(), started_at=0)
    assert [(a.kind, a.name, a.media_type, a.data, a.text, a.sha256) for a in again] == [
        (a.kind, a.name, a.media_type, a.data, a.text, a.sha256) for a in attachments
    ]

    h.api.replies.append(reply(text("看到了。")))
    h.agent.run_turn(turn.text, again)
    assert dumps(h.api.requests[1]) == dumps(failed)  # 同样的文字、同样的附件，又发了一次
    assert [message["role"] for message in h.agent.messages] == ["user", "assistant"]  # 没有出现两遍


def test_a_failed_regeneration_leaves_the_message_to_be_sent_again(start):
    h = start(reply(text("答一")), reply(text("旧答二")))  # 重新生成时没有回复了：假 API 返回 500
    h.agent.run_turn("问一")
    mark = h.agent.backend.mark()
    h.agent.run_turn("问二")
    with pytest.raises(ProviderError):
        h.agent.retry_turn(mark)
    # 和别的失败一样：模型一步都没走完，这条消息撤回，下一次是"再试一次"（同样的文字重新发）
    assert len(h.agent.messages) == mark and h.agent.backend.mark() == mark and not h.agent.backend.turn_at(mark)
    h.api.replies.append(reply(text("新答二")))
    h.agent.run_turn("问二")
    assert h.api.requests[-1]["messages"][-1]["content"][-1]["text"] == "问二"
    assert len(h.agent.messages) == mark + 2


def test_attachments_come_back_from_the_history_when_the_original_is_gone(start):
    h = start(reply(text("看到了。")))
    attachments, _ = h.app.media.keep(parse([image(), text_file("笔记.md", "第一行")]))
    h.agent.run_turn("看看", attachments)
    turn = rewind.last_turn([{"type": "user", "text": "看看", "attachments": [item.summary() for item in attachments], "mark": 0}])
    for item in attachments:
        h.app.media.find(item.sha256).unlink()  # 原件没了（比如被手动删掉）

    # 这一轮的消息还在历史里：从历史里取回来，和多媒体库下载旧附件是同一个办法
    again = rewind.attachments_of(turn, None, h.app.media, h.agent.backend.export(), started_at=0)
    assert [(a.kind, a.name, a.data, a.text) for a in again] == [(a.kind, a.name, a.data, a.text) for a in attachments]
    assert again[0].data == PNG and again[0].media_type == "image/png"
    # 只留第二个
    (kept,) = rewind.attachments_of(turn, [1], h.app.media, h.agent.backend.export(), started_at=0)
    assert (kept.name, kept.text) == ("笔记.md", "第一行")
    assert rewind.attachments_of(turn, [], h.app.media, [], started_at=0) == []
    with pytest.raises(MediaError, match="参数不正确"):
        rewind.attachments_of(turn, [2], h.app.media, [], started_at=0)
    # 原件没了、历史里也没有：说清楚是哪一个取不回来
    with pytest.raises(MediaError, match="截图.png"):
        rewind.attachments_of(turn, None, h.app.media, [], started_at=0)


# ---- 标题 ---------------------------------------------------------------------


def test_editing_retitles_only_a_title_made_from_the_old_message(start):
    app = start().app
    photo = {"kind": "image", "name": "海边.png", "media_type": "image/png", "size": 10}

    def edited(title: str, old_text: str, old_attachments: list[dict], new_text: str, new_attachments=()) -> str:
        app.conversation.title = title
        app.retitle(old_text, old_attachments, new_text, list(new_attachments))
        return app.conversation.title

    assert edited("问一", "问一", [], "改过的问一") == "改过的问一"  # 照着旧的那句话起的：跟着换
    assert edited("海边.png", "", [photo], "这是哪里") == "这是哪里"  # 照着附件的名字起的
    assert edited("海边.png", "", [photo], "", parse([image("山里.png")])) == "山里.png"  # 改了以后也只有附件
    assert edited("我起的名字", "问一", [], "改过的问一") == "我起的名字"  # 你自己改过名：不动
    assert edited("", "问一", [], "改过的问一") == ""  # 还没有标题：等这一轮开始时照常起
    long = "很长的一句话" * 20  # 标题是截断过的：照样认得出来
    assert make_title(long) != long and edited(make_title(long), long, [], "短的") == "短的"


# ---- 花掉的钱 -----------------------------------------------------------------


def test_money_already_spent_stays_spent(start):
    h = start(reply(text("一")), reply(text("旧二")), reply(text("新二")))
    h.agent.run_turn("问一")
    mark = h.agent.backend.mark()
    h.agent.run_turn("问二")
    spent = h.app.spending()["today"]
    h.agent.retry_turn(mark)

    # 去掉的那一轮花的钱照样记着：账本里一次调用一行，三次调用三行；这次运行的用量也不减
    rows = [json.loads(line) for line in h.config.ledger_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 3 and sum(row["requests"] for row in rows) == 3
    assert h.agent.usage.requests == 3 and h.agent.usage.output_tokens == 60 and h.agent.usage.input_tokens == 300
    assert h.app.spending()["today"] > spent > 0


# ---- 哪一轮、做过什么 -------------------------------------------------------------


def test_the_last_turn_is_found_from_the_end():
    shot = {"kind": "image", "name": "截图.png", "media_type": "image/png", "size": 10}
    events = [
        {"type": "user", "text": "问一", "mark": 0},
        {"type": "turn_start"},
        {"type": "text", "text": "答一"},
        {"type": "turn_end"},
        {"type": "user", "text": "问二", "mark": 2, "attachments": [shot, {"kind": "exe", "name": "x.exe"}]},
        {"type": "turn_start"},
        {"type": "text", "text": "答二"},
    ]
    turn = rewind.last_turn(events)
    assert (turn.index, turn.mark, turn.id, turn.text, turn.events) == (4, 2, None, "问二", events[4:])  # 存进文件的事件没有编号
    assert turn.attachments == [shot]  # 和页面重放时一样筛过：认不出来的去掉
    assert rewind.last_turn(events[4:], start=4).index == 4  # 只给了末尾一截
    # 页面的事件日志里的：带着编号，页面发请求时拿它来对
    logged = [{**event, "id": number} for number, event in enumerate(events, start=11)]
    assert rewind.last_turn(logged).id == 15
    assert rewind.last_turn([]) is None and rewind.last_turn([{"type": "turn_start"}, "坏的"]) is None
    for bad in (True, -1, "2", 2.0, None):  # 没有记、或者记坏了：倒不回去
        assert rewind.last_turn([{"type": "user", "text": "问", "mark": bad}]).mark is None
    assert rewind.last_turn([{"type": "user", "text": 5}]).text == ""


@pytest.mark.parametrize(
    ("name", "tool_input", "changed"),
    [
        ("run_command", {"command": "ls"}, True),
        ("str_replace_based_edit_tool", {"command": "view", "path": "a.txt"}, False),
        ("str_replace_based_edit_tool", {"command": "create", "path": "a.txt", "file_text": "x"}, True),
        ("str_replace_based_edit_tool", None, True),  # 参数解析不了：认不出来，算会
        ("memory", {"command": "view", "path": "/memories"}, False),
        ("memory", {"command": "str_replace", "path": "/memories/a.md"}, True),
        ("todo_list", {}, False),
        ("todo_add", {"title": "x"}, True),
        ("task_list", {}, False),
        ("task_add", {"title": "x"}, True),
        ("skill", {"name": "pdf"}, False),
        ("agent", {"name": "研究员", "task": "查"}, False),  # 交给子 Agent 本身不算，它自己调用的工具另外看
        ("mcp__github__list_issues", {}, True),  # 别人写的程序：名字像是只读的也算
        ("browser.screenshot", {}, False),
        ("browser.navigate", {"url": "https://example.com"}, False),
        ("browser.left_click", {"ref": "ref_1"}, True),
        ("browser.type", {"text": "你好"}, True),
        ("browser_screenshot", {}, False),  # 别家的模型用的替身
        ("browser_click", {"ref": "ref_1"}, True),
        ("desktop_control", {"action": "click"}, True),  # 以后新加的、没有登记的：宁可多问一次
    ],
)
def test_which_calls_may_have_changed_something(name, tool_input, changed):
    assert rewind.changes(name, tool_input) is changed


def call_event(name: str, summary: str, tool_input: dict) -> dict:
    return {"type": "tool_call", "name": name, "summary": summary, "input": tool_input}


def test_effects_count_only_what_actually_ran():
    command = call_event("run_command", "run_command $ touch a.txt", {"command": "touch a.txt"})
    look = call_event("str_replace_based_edit_tool", "str_replace_based_edit_tool view a.txt", {"command": "view", "path": "a.txt"})
    result = {"type": "tool_result", "text": "（没有输出）", "is_error": False}

    def live(allowed: bool, stopped: bool = False) -> list[dict]:  # 刚发生的：confirm 后面跟着 confirm_done
        return [command, {"type": "confirm", "id": 7, "description": "…"}, {"type": "confirm_done", "confirm": 7, "allowed": allowed, "stopped": stopped}, result]

    def saved(outcome: str) -> list[dict]:  # 存进会话文件以后：结果合进了 confirm 的 outcome
        return [command, {"type": "confirm", "description": "…", "outcome": outcome}, result]

    assert rewind.effects(live(True)) == ["run_command $ touch a.txt"]
    assert rewind.effects(live(False)) == [] and rewind.effects(live(False, stopped=True)) == []
    assert rewind.effects(saved("allowed")) == ["run_command $ touch a.txt"]
    assert rewind.effects(saved("denied")) == [] and rewind.effects(saved("stopped")) == []
    assert rewind.effects([command, {"type": "auto_allowed"}, result]) == ["run_command $ touch a.txt"]  # 没问就放行的也算
    assert rewind.effects([look, result]) == [] and rewind.effects([]) == []
    # 读工作目录外的文件要问：答应了也只是读
    assert rewind.effects([look, {"type": "confirm", "outcome": "allowed"}, result]) == []
    # 第一条命令被拒绝了、第二条答应了：只算第二条
    second = call_event("run_command", "run_command $ rm b.txt", {"command": "rm b.txt"})
    events = [*live(False), second, {"type": "confirm", "id": 9}, {"type": "confirm_done", "confirm": 9, "allowed": True}]
    assert rewind.effects(events) == ["run_command $ rm b.txt"]
    # 子 Agent 替主助手做的也算；读一个 Skill、交给子 Agent 本身不算
    delegated = {"type": "subagent", "step": "tool_call", "name": "run_command", "summary": "run_command $ make", "input": {"command": "make"}}
    handed = call_event("agent", "agent 研究员 · 查", {"name": "研究员", "task": "查"})
    assert rewind.effects([handed, delegated, {"type": "subagent", "step": "tool_result", "text": "ok"}]) == ["run_command $ make"]
    # 认不出来的事件、缺字段的调用不会出错
    assert rewind.effects(["坏的", {"type": "tool_call"}, {"type": "confirm_done"}]) == ["?"]


def test_a_failed_call_to_an_all_or_nothing_tool_changed_nothing():
    # 智狐自己的待办、文件、记忆、定时任务工具先查好参数才写：出错了就是什么都没改（见 rewind._ALL_OR_NOTHING）
    failed = {"type": "tool_result", "text": "没有编号为 99 的待办。", "is_error": True}
    fine = {"type": "tool_result", "text": "好了", "is_error": False}
    update = call_event("todo_update", 'todo_update {"id": 99}', {"id": 99, "done": True})
    edit = call_event("str_replace_based_edit_tool", "str_replace_based_edit_tool str_replace a.txt", {"command": "str_replace", "path": "a.txt"})
    assert rewind.effects([update, failed]) == [] and rewind.effects([edit, failed]) == []
    assert rewind.effects([update, fine]) == ['todo_update {"id": 99}']
    # 执行命令、MCP、浏览器做到一半出错，前一半已经做了：照样算
    command = call_event("run_command", "run_command $ make", {"command": "make"})
    mcp = call_event("mcp__files__write", "mcp__files__write {}", {})
    click = call_event("browser.left_click", "browser.left_click ref_1", {"target": {"type": "ref", "ref": "ref_1"}})
    assert rewind.effects([command, failed, mcp, failed, click, failed]) == ["run_command $ make", "mcp__files__write {}", "browser.left_click ref_1"]
    # 子 Agent 那一层也一样；一个结果只管它前面那一次调用
    sub_call = {"type": "subagent", "step": "tool_call", "name": "todo_add", "summary": "todo_add 交报告", "input": {"title": "交报告"}}
    sub_failed = {"type": "subagent", "step": "tool_result", "text": "出错", "is_error": True}
    assert rewind.effects([sub_call, sub_failed]) == []
    assert rewind.effects([command, fine, update, failed]) == ["run_command $ make"]


def test_long_lists_of_effects_are_shortened():
    found = [f"run_command $ echo {n}" for n in range(10)]
    # 页面拿到的：前几行，和没列出来的还有几项（"还有几项"那一句页面自己写，换语言时能跟着换）。一共最多 MAX_REWIND_EFFECTS 行
    assert rewind.listed(found) == (found[: MAX_REWIND_EFFECTS - 1], 10 - MAX_REWIND_EFFECTS + 1)
    assert rewind.listed(found[:MAX_REWIND_EFFECTS]) == (found[:MAX_REWIND_EFFECTS], 0)
    assert rewind.listed(found[: MAX_REWIND_EFFECTS + 1]) == (found[: MAX_REWIND_EFFECTS - 1], 2)
    assert rewind.listed([]) == ([], 0)
    # 终端里的：同样的几行，再加上那一句
    shown = rewind.shown_effects(found)
    assert len(shown) == MAX_REWIND_EFFECTS and shown[:-1] == found[: MAX_REWIND_EFFECTS - 1]
    rest = 10 - MAX_REWIND_EFFECTS + 1
    assert shown[-1] == f"……还有 {rest} 项" and shown[-1].key == "……还有 {n} 项" and shown[-1].values == {"n": rest}
    assert rewind.shown_effects(found[:MAX_REWIND_EFFECTS]) == found[:MAX_REWIND_EFFECTS]
