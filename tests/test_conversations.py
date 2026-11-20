"""会话的保存和恢复：磁盘上的会话文件、两种历史格式的往返、界面事件的整理和重放。

网页上的操作（新对话、打开、改名、删除）在 test_webui.py 的最后一节里测。
"""

from __future__ import annotations

import json
import re

import pytest
from fake_api import reply, text, thinking, tool_use
from fake_openai import call, chat

from harness.conversations import (
    FORMAT_VERSION,
    TITLE_LIMIT,
    Conversation,
    ConversationError,
    ConversationStore,
    make_title,
)
from harness.providers import ProviderError
from harness.webui.ui import WebUI

TIME_NOTE = "<context>当前时间：2026-11-20 14:03 星期五</context>"  # 测试里的时钟是固定的


# ---- 标题 -----------------------------------------------------------------


def test_title_is_the_first_line_cut_to_forty_characters():
    assert TITLE_LIMIT == 40
    assert make_title("帮我整理笔记") == "帮我整理笔记"
    # 只取第一行（前面的空行不算），多余的空白压成一个空格
    assert make_title("\n\n  帮我   整理\t笔记  \n第二行不算") == "帮我 整理 笔记"
    # 正好 40 个字不动；再多一个字就截断，末尾用省略号顶替，总长还是 40
    assert make_title("字" * 40) == "字" * 40
    assert make_title("字" * 41 + "\n第二行") == "字" * 39 + "…"
    assert make_title("") == "" and make_title(" \n\t\n") == ""


# ---- 会话文件 -------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> ConversationStore:
    return ConversationStore(tmp_path / "conversations")


def put(store: ConversationStore, title: str = "", updated_at: float | None = None, **fields) -> Conversation:
    """往 store 里存一段会话。给了 updated_at 就按这个时间存（不用真实的当前时间），方便测排序。"""
    conversation = store.new("anthropic", "claude-opus-5-5")
    conversation.title = title
    for name, value in fields.items():
        setattr(conversation, name, value)
    if updated_at is None:
        store.save(conversation)
    else:
        conversation.updated_at = updated_at
        store.save(conversation, touch=False)
    return conversation


def ids(store: ConversationStore) -> list[str]:
    return [item["id"] for item in store.summaries()]


def test_a_new_conversation_exists_only_in_memory(store):
    conversation = store.new("deepseek", "deepseek-flash")

    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", conversation.id)  # 日期-时间-随机字符
    assert (conversation.provider, conversation.model, conversation.title) == ("deepseek", "deepseek-flash", "")
    assert conversation.saved is False and conversation.messages == [] and conversation.events == []
    assert conversation.created_at == conversation.updated_at > 0
    assert store.new("deepseek", "deepseek-flash").id != conversation.id
    # 磁盘上什么都没有，列表里也没有
    assert not store.directory.exists()
    assert store.summaries() == [] and not store.exists(conversation.id)
    with pytest.raises(ConversationError, match="不存在"):
        store.load(conversation.id)


def test_a_saved_conversation_loads_back_unchanged(store):
    conversation = store.new("deepseek", "deepseek-flash")
    conversation.title = "暗号"
    conversation.context_tokens = 1234
    conversation.messages = [
        {"role": "user", "content": "暗号是 42"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "todo_list", "arguments": "{}"}},
        ]},
    ]
    conversation.events = [{"type": "user", "text": "暗号是 42"}, {"type": "turn_start"}]
    created = conversation.created_at
    store.save(conversation)

    assert conversation.saved is True and conversation.updated_at >= created
    loaded = store.load(conversation.id)
    assert loaded is not conversation and loaded == conversation  # 每一项都和存进去的一样
    assert store.exists(conversation.id)
    assert store.summaries() == [{
        "id": conversation.id,
        "title": "暗号",
        "provider": "deepseek",
        "model": "deepseek-flash",
        "updated_at": conversation.updated_at,
    }]
    # 目录里只有这一个文件：写的时候先写临时文件再改名，没有留下半成品
    (path,) = store.directory.iterdir()
    assert path.name == f"{conversation.id}.json"
    raw = path.read_text(encoding="utf-8")
    assert "暗号是 42" in raw  # 中文原样写进文件，用编辑器打开就能看懂
    data = json.loads(raw)
    assert data["version"] == FORMAT_VERSION and "saved" not in data

    # 再存一次是更新同一个文件，不是新建一个
    conversation.messages.append({"role": "tool", "tool_call_id": "call_1", "content": "[]"})
    store.save(conversation)
    assert len(list(store.directory.iterdir())) == 1
    assert len(store.load(conversation.id).messages) == 3


def test_list_is_sorted_by_update_time(store):
    oldest = put(store, "最早的", updated_at=100.0)
    newest = put(store, "最新的", updated_at=300.0)
    middle = put(store, updated_at=200.0)  # 没有标题的显示成"新对话"

    assert ids(store) == [newest.id, middle.id, oldest.id]
    assert [item["title"] for item in store.summaries()] == ["最新的", "新对话", "最早的"]

    store.save(oldest)  # 普通的保存会把更新时间改成现在，于是它排到了最前面
    assert oldest.updated_at > 300.0
    assert ids(store) == [oldest.id, newest.id, middle.id]


def test_rename_changes_only_the_title(store):
    older = put(store, "旧名字", updated_at=100.0, messages=[{"role": "user", "content": "你好"}], context_tokens=7)
    newer = put(store, "另一段", updated_at=200.0)

    store.rename(older.id, "  改过的   名字  \n第二行不算")

    renamed = store.load(older.id)
    assert renamed.title == "改过的 名字"
    # 只是改了个名字：更新时间不变，所以它不会跳到列表最前面；内容也原封不动
    assert renamed.updated_at == 100.0 and ids(store) == [newer.id, older.id]
    assert renamed.messages == [{"role": "user", "content": "你好"}] and renamed.context_tokens == 7
    assert [item["title"] for item in store.summaries()] == ["另一段", "改过的 名字"]

    store.rename(older.id, "长" * 100)  # 太长的名字照样截断
    assert store.load(older.id).title == "长" * 39 + "…"
    with pytest.raises(ConversationError, match="不存在"):
        store.rename("20200101-000000-abcdef", "没有这一段")


def test_delete_removes_the_file(store):
    doomed = put(store, "要删的")
    kept = put(store, "留着的")

    store.delete(doomed.id)

    assert ids(store) == [kept.id] and not store.exists(doomed.id)
    assert [path.stem for path in store.directory.iterdir()] == [kept.id]
    with pytest.raises(ConversationError, match="不存在"):
        store.load(doomed.id)
    with pytest.raises(ConversationError, match="不存在"):
        store.delete(doomed.id)  # 删过的不能再删一次
    assert store.load(kept.id) == kept


def test_file_of_points_at_saved_conversations_only(store):
    saved = put(store, "存过的")
    assert store.file_of(saved.id) == store.directory / f"{saved.id}.json"
    assert store.file_of(saved.id).is_file()

    unsaved = store.new("anthropic", "claude-opus-5-5")  # 只在内存里的新会话没有文件
    for missing in (unsaved.id, "", ".", "../x", "20261120-140000-aaaaaa", f"../{saved.id}", f"{saved.id}.json", f"{saved.id}/.."):
        with pytest.raises(ConversationError, match="不存在"):
            store.file_of(missing)


def test_list_survives_a_restart(store):
    first = put(store, "第一段", updated_at=100.0, messages=[{"role": "user", "content": "你好"}])
    second = put(store, "第二段", updated_at=200.0)

    reopened = ConversationStore(store.directory)  # 程序重启：重新扫一遍目录

    assert reopened.summaries() == store.summaries()
    assert ids(reopened) == [second.id, first.id]
    assert reopened.load(first.id) == first

    empty = ConversationStore(store.directory / "还没有这个目录")
    assert empty.summaries() == [] and not empty.directory.exists()  # 只是看一眼，不会顺手建目录


def test_corrupt_files_are_skipped_but_never_deleted(store):
    good = put(store, "好的那段")
    bad = {
        "20261120-140000-aaaaaa.json": b'{"provider": "anthropic", "model": "claude-opus-5-5", "messa',  # 只写了一半
        "20261120-140001-bbbbbb.json": b"[]",  # 是合法的 JSON，但不是一段会话
        "20261120-140002-cccccc.json": b'{"provider": "anthropic", "model": "claude-opus-5-5"}',  # 缺了对话历史
        "20261120-140003-dddddd.json": b"\xff\xfe\x00\x01",  # 根本不是文字
        "20261120-140004-eeeeee.json": b'{"provider": "p", "model": "m", "messages": [], "updated_at": "abc"}',
        # 是一段会话的样子，但某一项的类型不对。原样放行的话，页面会把数字当文字用，或者拿到解析不了的时间
        "20261120-140006-111111.json": b'{"provider": "p", "model": "m", "messages": [], "title": 5}',
        "20261120-140007-222222.json": b'{"provider": ["p"], "model": "m", "messages": []}',
        "20261120-140008-333333.json": b'{"provider": "p", "model": "m", "messages": "abc"}',
        "20261120-140009-444444.json": b'{"provider": "p", "model": "m", "messages": [], "events": {"a": 1}}',
        "20261120-140010-555555.json": b'{"provider": "p", "model": "m", "messages": [], "updated_at": NaN}',
        "20261120-140011-666666.json": b'{"provider": "p", "model": "m", "messages": [], "updated_at": 1e999}',
        "notes.json": b'{"provider": "anthropic", "model": "claude-opus-5-5", "messages": []}',  # 文件名不是会话编号
        "20261120-140005-ffffff.tmp": b'{"provider": "anthropic"',  # 上次保存到一半留下的临时文件
    }
    for name, content in bad.items():
        (store.directory / name).write_bytes(content)

    reopened = ConversationStore(store.directory)

    assert ids(reopened) == [good.id]  # 列表里只有读得出来的那一段
    for name, content in bad.items():
        assert (store.directory / name).read_bytes() == content  # 坏文件原样留着，等用户自己处理
        assert not reopened.exists(name.split(".")[0])
        with pytest.raises(ConversationError):
            reopened.load(name.split(".")[0])


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "x",
        "../x",
        "/etc/passwd",
        "20261120-140000-aaaaaa.json",
        "../20261120-140000-aaaaaa",
        "20261120-140000-aaaaaa/../x",
        "20261120-140000-AAAAAA",  # 随机字符只能是小写的十六进制
        "20261120-140000-aaaaaa\n",
    ],
)
def test_malformed_ids_are_rejected(store, tmp_path, bad_id):
    put(store, "真的那段")
    for operation in (store.load, store.delete, lambda value: store.rename(value, "新名字")):
        with pytest.raises(ConversationError):
            operation(bad_id)
    assert not store.exists(bad_id)
    # 编号会拼进文件名里，所以格式不对的编号连写都不让写，免得写到别的目录去
    outsider = Conversation(id=bad_id, provider="anthropic", model="claude-opus-5-5")
    with pytest.raises(ConversationError, match="格式不对"):
        store.save(outsider)
    assert outsider.saved is False
    assert len(list(tmp_path.rglob("*.json"))) == 1 and list(tmp_path.rglob("*.tmp")) == []


# ---- 对话历史的往返：存盘 → 重启 → 接着聊 ---------------------------------
#
# start / start_openai 每调用一次就组装一个全新的 App。同一个测试里它们用的是同一个数据目录，
# 所以第二次调用相当于把程序关掉再打开。


def through_json(value):
    """按存盘的方式走一遍：写成 JSON 文本再读回来。"""
    return json.loads(json.dumps(value, ensure_ascii=False))


def test_claude_conversation_survives_a_restart(start):
    before = start(
        reply(
            thinking("先记下来", signature="sig-step-1"),
            tool_use("toolu_1", "todo_add", {"title": "交报告", "due": "2026-11-27"}),
            stop_reason="tool_use",
        ),
        reply(thinking("记好了，告诉用户", signature="sig-step-2"), text("记好了。")),
    )
    before.agent.run_turn("提醒我交报告")
    history = before.agent.backend.export()
    assert [message["role"] for message in history] == ["user", "assistant", "user", "assistant"]
    assert through_json(history) == history  # 历史里只有普通的字典、列表和文字，写成 JSON 不丢任何东西
    saved_history = through_json(history)
    before.app.name_conversation("提醒我交报告")
    assert before.app.save_conversation([{"type": "user", "text": "提醒我交报告"}]) is True
    conversation_id = before.app.conversation.id

    after = start(reply(text("你让我提醒你交报告。")))  # 重启
    # 启动时是一段新对话，不会自动打开上次的；上次的在列表里
    assert after.agent.messages == [] and after.app.conversation.id != conversation_id
    assert [(item["id"], item["title"]) for item in after.app.conversation_summaries()] == [
        (conversation_id, "提醒我交报告")
    ]
    opened = after.app.open_conversation(conversation_id)
    assert after.app.conversation is opened and opened.saved is True
    assert (opened.provider, opened.model, opened.title) == ("anthropic", "claude-opus-5-5", "提醒我交报告")
    assert opened.events == [{"type": "user", "text": "提醒我交报告"}]
    assert opened.context_tokens == after.agent.usage.context_tokens == 170  # 侧栏上的"上下文"也恢复了
    after.agent.run_turn("刚才让你记了什么？")

    (request,) = after.api.requests
    # 这次请求的开头就是保存时的历史，逐项相等；新消息追加在最后
    assert request["messages"][:4] == saved_history
    assert request["messages"][4:] == [
        {"role": "user", "content": [{"type": "text", "text": TIME_NOTE}, {"type": "text", "text": "刚才让你记了什么？"}]}
    ]
    # 思考内容连同签名原样发了回去（签名对不上的话，真实的 API 会拒绝请求）
    first_thought, tool_call = request["messages"][1]["content"]
    assert (first_thought["type"], first_thought["thinking"], first_thought["signature"]) == (
        "thinking", "先记下来", "sig-step-1",
    )
    assert (tool_call["type"], tool_call["id"], tool_call["input"]) == (
        "tool_use", "toolu_1", {"title": "交报告", "due": "2026-11-27"},
    )
    assert request["messages"][2]["content"][0]["tool_use_id"] == "toolu_1"
    assert request["messages"][3]["content"][0]["signature"] == "sig-step-2"
    # 重启前最后一次请求里发过的内容，现在一字不差地排在开头，所以 API 那边的缓存还能接着用
    last_before = before.api.requests[-1]
    assert request["messages"][:3] == last_before["messages"]
    assert request["system"] == last_before["system"] and request["tools"] == last_before["tools"]

    # 接着存，更新的是同一个文件
    assert after.app.save_conversation([]) is True
    assert [path.stem for path in after.config.conversations_dir.iterdir()] == [conversation_id]
    assert len(after.app.store.load(conversation_id).messages) == 6

    # 之后的一条消息失败了（假 API 没有回复可用）：只撤回这条新消息，读回来的历史不受影响
    intact = through_json(after.agent.messages)
    with pytest.raises(ProviderError):
        after.agent.run_turn("再问一句")
    assert after.agent.messages == intact


def test_openai_format_conversation_survives_a_restart(start_openai):
    signature = {"google": {"thought_signature": "sig-abc"}}
    arguments = json.dumps({"title": "交报告"}, ensure_ascii=False)
    before = start_openai(
        chat(reasoning="先记下来", calls=[call("call_1", "todo_add", arguments, extra_content=signature)]),
        chat("记好了。", reasoning="已经记好"),
    )
    before.agent.run_turn("提醒我交报告")
    history = before.agent.backend.export()
    # 系统提示词不存：它每次启动都会重新生成
    assert [message["role"] for message in history] == ["user", "assistant", "tool", "assistant"]
    assert through_json(history) == history
    saved_history = through_json(history)
    before.app.name_conversation("提醒我交报告")
    assert before.app.save_conversation([]) is True
    conversation_id = before.app.conversation.id

    after = start_openai(chat("你让我提醒你交报告。"))  # 重启
    assert [message["role"] for message in after.agent.messages] == ["system"]
    opened = after.app.open_conversation(conversation_id)
    assert (opened.provider, opened.model) == ("deepseek", "deepseek-flash")
    assert opened.context_tokens == after.agent.usage.context_tokens == 120
    after.agent.run_turn("刚才让你记了什么？")

    (request,) = after.api.requests
    system, *rest = request["messages"]
    assert system == before.api.requests[0]["messages"][0]  # 重新生成的系统提示词和上次一样
    assert rest[:4] == saved_history
    assert rest[4:] == [{"role": "user", "content": f"{TIME_NOTE}\n\n刚才让你记了什么？"}]
    # 两样各家特有的东西都原样发了回去：思考内容（DeepSeek 要求）和工具调用上的签名（Gemini 要求）
    assert rest[1] == {
        "role": "assistant",
        "content": None,
        "reasoning_content": "先记下来",
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "todo_add", "arguments": arguments},
            "extra_content": signature,
        }],
    }
    assert rest[2]["role"] == "tool" and rest[2]["tool_call_id"] == "call_1"
    assert rest[3] == {"role": "assistant", "content": "记好了。", "reasoning_content": "已经记好"}

    assert after.app.save_conversation([]) is True
    assert [path.stem for path in after.config.conversations_dir.iterdir()] == [conversation_id]
    assert len(after.app.store.load(conversation_id).messages) == 6

    intact = through_json(after.agent.messages)
    with pytest.raises(ProviderError):
        after.agent.run_turn("再问一句")
    assert after.agent.messages == intact  # 系统提示词和读回来的历史都还在


def test_a_failed_first_message_is_not_saved(start):
    h = start()  # 没有预设回复：假 API 返回 500
    h.app.name_conversation("你好\n第二行")
    # 这一轮进行中：会话还没存盘，但已经带着标题出现在列表里
    assert [(item["id"], item["title"]) for item in h.app.conversation_summaries()] == [(h.app.conversation.id, "你好")]
    with pytest.raises(ProviderError):
        h.agent.run_turn("你好\n第二行")

    assert h.app.save_conversation([{"type": "user", "text": "你好"}]) is False
    # 什么都没聊成，就当它没发生过：没有文件，列表里也不留下一段空会话
    assert not h.config.conversations_dir.exists()
    assert h.app.conversation_summaries() == []
    assert h.app.conversation.title == "" and h.app.conversation.saved is False


def test_switching_models_continues_but_switching_providers_starts_over(start):
    h = start(reply(text("你好。")))
    h.agent.run_turn("你好")
    h.app.name_conversation("你好")
    h.app.save_conversation([])
    first = h.app.conversation

    assert h.app.select_model("anthropic", "claude-sonnet-5-5") is True
    assert h.app.conversation is first and first.model == "claude-sonnet-5-5"  # 同一家：还是这一段，只是换了模型

    assert h.app.select_model("deepseek", "deepseek-flash") is False
    fresh = h.app.conversation  # 换了一家：开了一段新的，之前那段留在列表里
    assert fresh is not first and fresh.saved is False and fresh.title == ""
    assert (fresh.provider, fresh.model) == ("deepseek", "deepseek-flash")
    assert h.agent.usage.context_tokens == 0
    assert [item["id"] for item in h.app.conversation_summaries()] == [first.id]


def test_a_conversation_from_an_unknown_provider_cannot_be_opened(start):
    before = start(reply(text("你好。")))
    before.agent.run_turn("你好")
    before.app.name_conversation("你好")
    before.app.save_conversation([])
    path = before.config.conversations_dir / f"{before.app.conversation.id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["provider"] = "一家已经不支持的厂商"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    after = start()
    current = after.app.conversation
    with pytest.raises(ConversationError, match="不支持"):
        after.app.open_conversation(before.app.conversation.id)
    # 打不开就保持原样：当前会话、对话历史、正在用的模型都没动
    assert after.app.conversation is current and after.agent.messages == []
    assert (after.app.settings.provider, after.agent.backend.provider) == ("anthropic", "anthropic")


# ---- 界面事件：整理成保存的样子，再重放出来 --------------------------------


def log(ui: WebUI) -> list[dict]:
    return ui.events_after(0, timeout=0)


def kinds(events: list[dict]) -> list[str]:
    return [event["type"] for event in events]


def test_export_joins_streamed_pieces_into_whole_blocks():
    ui = WebUI()
    ui.begin_turn("你好")
    ui.thinking("想")
    ui.thinking("一下")
    ui.text("你")  # 种类不同：不会接到上面的思考后面
    ui.publish("state", provider="anthropic")  # 夹在中间的状态更新不影响合并
    ui.text("好")
    ui.text("！")
    ui.end_stream()
    ui.text("第二段")  # 前面隔着一个"这段结束了"：是新的一段，不合并
    ui.end_turn()
    live = log(ui)

    assert ui.export_events() == [
        {"type": "user", "text": "你好"},
        {"type": "turn_start"},
        {"type": "thinking", "text": "想一下"},
        {"type": "text", "text": "你好！"},
        {"type": "block_end"},
        {"type": "text", "text": "第二段"},
        {"type": "block_end"},
        {"type": "turn_end"},
    ]
    # 整理的是一份副本：日志本身没有被改动，正连着的浏览器不受影响
    assert log(ui) == live and [event["text"] for event in live if event["type"] == "text"] == ["你", "好", "！", "第二段"]


def test_export_leaves_out_what_is_only_for_the_moment():
    ui = WebUI()
    ui.reset()
    ui.publish("state", provider="anthropic")
    ui.hint("这段会话很长，接着聊会按整段历史计费。")
    ui.begin_turn("你好")
    ui.publish("state", provider="anthropic")
    ui.text("好")
    ui.info("已中断。")
    ui.error("出错了")
    ui.end_turn()

    assert kinds(log(ui)) == [
        "reset", "state", "notice", "user", "turn_start", "state", "text", "block_end", "notice", "notice", "turn_end",
    ]
    exported = ui.export_events()
    # 清空页面的信号、侧栏的状态、打开会话时的提示都不保存；编号也不保存（重新打开时会分配新的）
    assert exported == [
        {"type": "user", "text": "你好"},
        {"type": "turn_start"},
        {"type": "text", "text": "好"},
        {"type": "block_end"},
        {"type": "notice", "level": "info", "text": "已中断。"},
        {"type": "notice", "level": "error", "text": "出错了"},
        {"type": "turn_end"},
    ]


def test_hint_is_shown_but_marked_as_not_to_be_saved():
    ui = WebUI()
    ui.hint("接着聊会按整段历史计费。")

    (event,) = log(ui)
    assert event == {"id": 1, "type": "notice", "level": "info", "text": "接着聊会按整段历史计费。", "transient": True}
    assert ui.export_events() == []


def test_export_folds_each_confirmation_and_its_answer_into_one_entry():
    ui = WebUI()
    ui.begin_turn("建几个文件")
    allowed = ui.publish("confirm", description="运行命令：touch a.txt")
    ui.publish("confirm_done", confirm=allowed, allowed=True, stopped=False)
    denied = ui.publish("confirm", description="运行命令：touch b.txt")
    ui.publish("confirm_done", confirm=denied, allowed=False, stopped=False)
    stopped = ui.publish("confirm", description="运行命令：touch c.txt")
    ui.publish("confirm_done", confirm=stopped, allowed=False, stopped=True)
    ui.publish("confirm", description="运行命令：touch d.txt")  # 一直没有等到答复

    assert ui.export_events() == [
        {"type": "user", "text": "建几个文件"},
        {"type": "turn_start"},
        {"type": "confirm", "description": "运行命令：touch a.txt", "outcome": "allowed"},
        {"type": "confirm", "description": "运行命令：touch b.txt", "outcome": "denied"},
        {"type": "confirm", "description": "运行命令：touch c.txt", "outcome": "stopped"},
        {"type": "confirm", "description": "运行命令：touch d.txt", "outcome": "stopped"},  # 没有答复的按"已中断"算
    ]


def test_load_replays_saved_events_under_new_ids():
    ui = WebUI()
    ui.publish("state", provider="anthropic")
    ui.begin_turn("页面上原来的内容")
    ui.end_turn()
    old_ids = [event["id"] for event in log(ui)]
    saved = [
        {"type": "user", "text": "建几个文件"},
        {"type": "turn_start"},
        {"type": "tool_call", "name": "run_command", "summary": "运行命令：touch a.txt", "input": {"command": "touch a.txt"}},
        {"type": "confirm", "description": "运行命令：touch a.txt", "outcome": "allowed"},
        {"type": "tool_result", "text": "（没有输出）", "is_error": False},
        {"type": "confirm", "description": "运行命令：touch b.txt", "outcome": "denied"},
        {"type": "confirm", "description": "运行命令：touch c.txt", "outcome": "stopped"},
        {"type": "confirm", "description": "运行命令：touch d.txt"},  # 没写结果的也按"已中断"算
        {"type": "turn_end"},
    ]
    untouched = json.loads(json.dumps(saved))

    ui.load_events(saved)

    events = log(ui)
    assert kinds(events) == [
        "reset",  # 先让页面清空，原来的内容不再重放
        "user", "turn_start", "tool_call",
        "confirm", "confirm_done",
        "tool_result",
        "confirm", "confirm_done",
        "confirm", "confirm_done",
        "confirm", "confirm_done",
        "turn_end",
    ]
    # 编号接着往后排，不会和页面上已经收到过的重复
    assert [event["id"] for event in events] == list(range(old_ids[-1] + 1, old_ids[-1] + 1 + len(events)))
    # 每张确认卡片后面都紧跟着它的结果，并且指向的是卡片这次新分配到的编号：没有悬空的确认
    answers = []
    for position, event in enumerate(events):
        if event["type"] == "confirm_done":
            card = events[position - 1]
            assert card["type"] == "confirm" and event["confirm"] == card["id"]
            answers.append((event["allowed"], event["stopped"]))
    assert answers == [(True, False), (False, False), (False, True), (False, True)]
    # "结果"只是保存时用的记号，不发给浏览器；其余内容原样重放
    assert all("outcome" not in event for event in events)
    assert events[3] == {"id": events[3]["id"], **saved[2]}
    assert saved == untouched


def test_load_closes_a_turn_that_was_saved_midway():
    ui = WebUI()
    ui.load_events([
        {"type": "user", "text": "第一轮"},
        {"type": "turn_start"},
        {"type": "text", "text": "答完了"},
        {"type": "block_end"},
        {"type": "turn_end"},
        {"type": "user", "text": "第二轮"},
        {"type": "turn_start"},
        {"type": "text", "text": "说到一半"},
    ])
    # 最后一轮没有结尾：补上一个，免得页面以为助手还在工作
    assert kinds(log(ui)) == [
        "reset", "user", "turn_start", "text", "block_end", "turn_end", "user", "turn_start", "text", "turn_end",
    ]

    ui.load_events([{"type": "user", "text": "完整的一轮"}, {"type": "turn_start"}, {"type": "turn_end"}])
    assert kinds(log(ui)) == ["reset", "user", "turn_start", "turn_end"]  # 本来就完整的不会多补

    ui.load_events([])
    assert kinds(log(ui)) == ["reset"]


def test_load_replays_only_what_belongs_in_a_conversation():
    ui = WebUI()
    ui.load_events([
        {"type": "user", "text": "你好"},
        {"type": "state", "provider": "冒充的"},  # "状态"只能由程序自己发出，不能从文件里重放
        {"type": "reset"},  # "清空页面"也是
        {"type": "confirm_done", "confirm": 1, "allowed": True},  # 确认的结果是跟着 confirm 重新生成的
        {"type": "made_up"},
        {"type": ["user"], "text": "类型不是文字"},
        {"text": "没有类型"},
        "不是字典",
        None,
        {"id": 999, "type": "text", "text": "带着自己的编号"},
    ])

    events = log(ui)
    assert [(event["type"], event.get("text")) for event in events] == [
        ("reset", None), ("user", "你好"), ("text", "带着自己的编号"),
    ]
    assert [event["id"] for event in events] == [1, 2, 3]  # 编号是重新分配的，文件里带的不算
    assert all("provider" not in event for event in events)


def test_export_and_load_round_trip():
    ui = WebUI()
    ui.begin_turn("看看 notes.txt")
    ui.thinking("要读")
    ui.thinking("文件")
    ui.end_stream()
    ui.tool_call("run_command", {"command": "cat notes.txt"})
    card = ui.publish("confirm", description="运行命令：cat notes.txt")
    ui.publish("confirm_done", confirm=card, allowed=True, stopped=False)
    ui.tool_result("第一行\n第二行", False)
    ui.text("里面")
    ui.text("有两行。")
    ui.end_turn()
    saved = ui.export_events()
    assert kinds(saved) == [
        "user", "turn_start", "thinking", "block_end", "tool_call", "confirm", "tool_result", "text", "block_end", "turn_end",
    ]

    reopened = WebUI()  # 另一个程序实例把它读回来
    reopened.load_events(saved)

    # 重放出来的东西再整理一遍，和保存的一模一样：存、读、再存，内容不会走样
    assert reopened.export_events() == saved
    # 浏览器这次看到的和当初相比，只是逐字到达的文字变成了整段
    replayed = [event for event in log(reopened) if event["type"] in ("thinking", "text")]
    assert [(event["type"], event["text"]) for event in replayed] == [("thinking", "要读文件"), ("text", "里面有两行。")]
