"""从某一轮分叉成一段新对话：截到哪、复制什么（harness/fork.py），不碰网页。

会话是在这里手写的（和真的会话文件一个样子）：Claude 的历史里有思考块、工具调用和结果；OpenAI 格式的历史压缩过。
要守住的几样：新的一段的 messages 是原来那段在某一轮开头的前缀、events 截到同一个位置；压缩进摘要的、没记位置的不能分叉；
标题和来路；改文件的记录不跟过来。网页那一侧（接口、换过去、原会话一字不变、多媒体和搜索的索引）在 test_fork_web.py。
"""

from __future__ import annotations

import json

import pytest

from harness import fork, i18n
from harness.conversations import TITLE_LIMIT, ConversationStore, origin_of
from harness.fork import ForkError
from harness.providers import PROVIDERS, compaction, saved_before
from harness.providers.claude import AnthropicBackend
from harness.providers.openai_compat import OpenAICompatBackend

CLAUDE, DEEPSEEK = PROVIDERS["anthropic"], PROVIDERS["deepseek"]


def said(words: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": "<context>现在是 2026-11-22 10:00</context>"}, {"type": "text", "text": words}]}


def trace(input_tokens: int, output: int, compaction_request: bool = False) -> dict:
    span = {"kind": "model", "input": input_tokens, "cache_read": 100, "cache_write": 0, "output": output}
    if compaction_request:
        span["compaction"] = True
    return {"type": "trace", "version": 1, "spans": [span], "totals": {}}


def claude_conversation(store: ConversationStore):
    """三轮的 Claude 会话：第一轮带思考，第二轮调用了一次工具、改了文件（之后撤回过），第三轮是最后一轮。位置是 0、2、6。"""
    conversation = store.new("anthropic", "claude-opus-5-5")
    conversation.title = "原来的会话"
    conversation.agent = "资料研究员"
    conversation.context_tokens = 9000
    conversation.messages = [
        said("问一"),
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "想", "signature": "sig-1"}, {"type": "text", "text": "答一"}]},
        said("问二"),
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "str_replace_based_edit_tool", "input": {"command": "create"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "好了"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "答二"}]},
        said("问三"),
        {"role": "assistant", "content": [{"type": "text", "text": "答三"}]},
    ]
    conversation.events = [
        {"type": "user", "text": "问一", "mark": 0, "attachments": [{"kind": "text", "name": "甲.md", "media_type": "text/markdown", "size": 3, "sha256": "a" * 64}]},
        {"type": "turn_start"}, {"type": "thinking", "text": "想"}, {"type": "text", "text": "答一"}, trace(1200, 30), {"type": "turn_end"},
        {"type": "user", "text": "问二", "mark": 2,
         "attachments": [{"kind": "text", "name": "乙.md", "media_type": "text/markdown", "size": 3}, {"kind": "text", "name": "丙.md", "media_type": "text/markdown", "size": 3}]},
        {"type": "turn_start"}, {"type": "tool_call", "name": "str_replace_based_edit_tool", "summary": "create a.txt", "input": {}},
        {"type": "tool_result", "text": "好了", "is_error": False}, {"type": "text", "text": "答二"},
        trace(2000, 40), {"type": "file_changes", "set": "s2", "files": [{"path": "a.txt", "kind": "created"}]}, {"type": "turn_end"},
        {"type": "file_undo", "set": "s2", "action": "undo", "files": [{"index": 0}]},
        {"type": "user", "text": "问三", "mark": 6}, {"type": "turn_start"}, {"type": "text", "text": "答三"}, trace(3000, 50),
    ]  # fmt: skip
    return conversation


@pytest.fixture
def store(tmp_path):
    return ConversationStore(tmp_path / "conversations")


def users(events: list[dict]) -> list[str]:
    return [event["text"] for event in events if event.get("type") == "user"]


# ---- 截到哪 -----------------------------------------------------------------


def test_forking_at_a_reply_keeps_that_turn_and_everything_before_it(store):
    source = claude_conversation(store)
    before = json.dumps(source.__dict__, ensure_ascii=False, sort_keys=True, default=str)

    made = fork.make(source, CLAUDE, store, 1, "reply")
    new = made.conversation

    # messages 是原来那段的前缀，切在第三轮的开头（位置 6）：第二轮的工具调用和结果都在里面
    assert new.messages == source.messages[:6] and new.messages is not source.messages
    assert new.messages[1]["content"][0]["signature"] == "sig-1"  # 思考块原样留着
    # events 截到第三轮的 user 事件之前；改文件的记录（file_changes、file_undo）不跟过来
    assert users(new.events) == ["问一", "问二"]
    assert [event["type"] for event in new.events if event["type"] in fork.LEFT_OUT] == []
    assert len(new.events) == 15 - 2 and new.events[-1] == {"type": "turn_end"}
    # 同一家、同一个模型、同一个角色；压缩掉的条数照原样
    assert (new.provider, new.model, new.agent, new.dropped) == ("anthropic", "claude-opus-5-5", "资料研究员", 0)
    assert new.id != source.id and not new.saved and not new.task and not new.channel
    assert new.title == "原来的会话（分叉）"
    assert new.forked_from == {"id": source.id, "turn": 2, "at": "reply", "title": "原来的会话", "changes": True}
    # 上下文按最后一轮的运行记录估：输入、读缓存、写缓存、输出加起来
    assert new.context_tokens == 2000 + 100 + 0 + 40
    assert (made.draft, made.attachments) == ("", [])
    # 原来那段一点都没变
    assert json.dumps(source.__dict__, ensure_ascii=False, sort_keys=True, default=str) == before


def test_forking_at_the_last_reply_takes_the_whole_conversation(store):
    source = claude_conversation(store)
    new = fork.make(source, CLAUDE, store, 2, "reply").conversation
    assert new.messages == source.messages and users(new.events) == ["问一", "问二", "问三"]
    assert len(new.events) == len(source.events) - 2  # 只少了改文件的那两条
    assert new.context_tokens == 9000  # 整段都带过来了：就是原来的大小


def test_forking_at_a_message_stops_before_it_and_hands_the_message_back(store):
    source = claude_conversation(store)
    made = fork.make(source, CLAUDE, store, 1, "message")
    new = made.conversation
    assert new.messages == source.messages[:2] and users(new.events) == ["问一"]
    assert new.events == source.events[:6]
    assert new.forked_from["at"] == "message" and new.forked_from["turn"] == 2 and new.forked_from["changes"] is False
    assert new.context_tokens == 1200 + 100 + 30
    # 那条消息放回输入框：文字，和附件在多媒体库里的编号（第二排：第一条消息有一排附件）
    assert made.draft == "问二"
    assert made.attachments == [f"{source.id}/1/0", f"{source.id}/1/1"]


def test_forking_at_the_first_message_gives_an_empty_conversation(store):
    source = claude_conversation(store)
    made = fork.make(source, CLAUDE, store, 0, "message")
    assert made.conversation.messages == [] and made.conversation.events == [] and made.conversation.context_tokens == 0
    assert made.draft == "问一" and made.attachments == [f"{source.id}/0/0"]


def test_turns_without_a_mark_cannot_be_forked(store):
    source = claude_conversation(store)
    del source.events[0]["mark"]  # 定时任务自己跑的那一轮、旧版本记下的：没有位置
    for at in ("reply", "message"):
        with pytest.raises(ForkError, match="没有记下它在对话历史里的位置"):
            fork.make(source, CLAUDE, store, 0, at)
    # 下一轮没有位置：不知道这一轮在哪儿结束，在它的回答上分不了；在它自己的消息上可以
    source = claude_conversation(store)
    source.events[6]["mark"] = True  # 被改坏了的也一样（布尔值不算位置）
    with pytest.raises(ForkError, match="找不到这一轮在哪儿结束"):
        fork.make(source, CLAUDE, store, 0, "reply")
    assert fork.make(source, CLAUDE, store, 0, "message").conversation.messages == []


def test_a_mark_that_does_not_start_a_turn_is_refused(store):
    source = claude_conversation(store)
    source.events[15]["mark"] = 4  # 第三轮的位置指着工具结果：不是一轮的开头
    with pytest.raises(ForkError, match="找不到了"):
        fork.make(source, CLAUDE, store, 1, "reply")
    source.events[15]["mark"] = 99  # 历史里根本没有这么多条
    with pytest.raises(ForkError, match="找不到了"):
        fork.make(source, CLAUDE, store, 2, "message")


def test_what_the_page_saw_must_match_the_file(store):
    source = claude_conversation(store)
    assert fork.make(source, CLAUDE, store, 1, "reply", expect=(2, "问二")).conversation.messages == source.messages[:6]
    for expect in ((2, "改过的问二"), (3, "问二"), (None, "问二")):
        with pytest.raises(ForkError, match="对话刚刚变过了"):
            fork.make(source, CLAUDE, store, 1, "reply", expect=expect)
    for index in (-1, 3):
        with pytest.raises(ForkError, match="对话刚刚变过了"):
            fork.make(source, CLAUDE, store, index, "reply")
    with pytest.raises(ForkError, match="参数不正确"):
        fork.make(source, CLAUDE, store, 1, "middle")


# ---- 压缩过的历史（GPT、Gemini、DeepSeek）------------------------------------------


def compacted_conversation(store: ConversationStore):
    """DeepSeek 的会话：一共四轮，前两轮压缩进了摘要。压缩以前位置是 1、3、5、7；压缩掉 2 条（4 条换成 2 条摘要）。"""
    history = [{"role": "system", "content": "提示词"}]
    for number in "一二三四":
        history += [{"role": "user", "content": f"第{number}轮"}, {"role": "assistant", "content": f"答{number}"}]
    folded = compaction.compacted(history, 5, "用户问过第一轮和第二轮。")
    conversation = store.new("deepseek", "deepseek-flash")
    conversation.title = "压缩过的"
    conversation.messages = folded[1:]
    conversation.dropped = len(history) - len(folded)
    conversation.events = []
    for number, mark in zip("一二三四", (1, 3, 5, 7), strict=True):
        conversation.events += [{"type": "user", "text": f"第{number}轮", "mark": mark}, {"type": "turn_start"}, {"type": "text", "text": f"答{number}"}, {"type": "turn_end"}]
    return conversation


def test_turns_folded_into_the_summary_cannot_be_forked(store):
    source = compacted_conversation(store)
    assert source.dropped == 2
    for index in (0, 1):
        for at in ("reply", "message"):
            with pytest.raises(ForkError, match="压缩进了摘要"):
                fork.make(source, DEEPSEEK, store, index, at)


def test_forking_after_the_summary_keeps_it_and_the_count_of_dropped_messages(store):
    source = compacted_conversation(store)
    new = fork.make(source, DEEPSEEK, store, 2, "reply").conversation
    # 摘要那两条和第三轮：仍然是原来那段的前缀，压缩掉的条数照原样带过去
    assert new.messages == source.messages[:4] and compaction.is_summary(new.messages[0])
    assert new.dropped == 2 and users(new.events) == ["第一轮", "第二轮", "第三轮"]
    # 新的一段换上适配层以后，事件里记着的位置照样对得上：第三轮还能倒回去，下一轮从位置 7 开始
    backend = OpenAICompatBackend(None, "deepseek", "DeepSeek", "deepseek-flash", None, [], "提示词")
    backend.restore(new.messages, new.dropped)
    assert backend.turn_at(5) and not backend.turn_at(3) and backend.mark() == 7
    assert fork.floor(backend) == 5

    message = fork.make(source, DEEPSEEK, store, 2, "message").conversation
    assert message.messages == source.messages[:2] and users(message.events) == ["第一轮", "第二轮"]


# ---- 位置的规矩（providers.saved_before）--------------------------------------------


def test_saved_before_follows_the_same_rule_as_turn_at():
    history = [said("一"), {"role": "assistant", "content": [{"type": "text", "text": "答"}]},
               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]}]  # fmt: skip
    backend = AnthropicBackend(None, "claude-opus-5-5", None, [], "提示词")
    backend.restore(history)
    for mark in range(-1, 5):
        cut = saved_before(CLAUDE, history, 0, mark)
        if mark == len(history):
            assert cut == history  # 末尾：整段
        else:
            assert (cut is not None) == backend.turn_at(mark), mark
    assert saved_before(CLAUDE, history, 0, 0) == []

    # OpenAI 格式：保存的历史没有系统提示词；工具结果后面补的那条图片消息不是一轮的开头
    image_note = {"role": "user", "content": [{"type": "text", "text": compaction.TOOL_IMAGES_NOTE}]}
    saved = [{"role": "user", "content": "一"}, {"role": "assistant", "content": "答"}, image_note, {"role": "user", "content": "二"}]
    openai = OpenAICompatBackend(None, "deepseek", "DeepSeek", "deepseek-flash", None, [], "提示词")
    openai.restore(saved)
    for mark in range(7):
        cut = saved_before(DEEPSEEK, saved, 0, mark)
        if mark == openai.mark():
            assert cut == saved
        else:
            assert (cut is not None) == openai.turn_at(mark), mark
    assert saved_before(DEEPSEEK, saved, 0, 4) == saved[:3]


def test_the_floor_is_where_the_first_turn_still_in_the_history_starts():
    claude = AnthropicBackend(None, "claude-opus-5-5", None, [], "提示词")
    assert fork.floor(claude) == 0  # 空的
    claude.restore([said("一"), {"role": "assistant", "content": "答"}])
    assert fork.floor(claude) == 0
    openai = OpenAICompatBackend(None, "deepseek", "DeepSeek", "deepseek-flash", None, [], "提示词")
    openai.restore([{"role": "user", "content": "一"}])
    assert fork.floor(openai) == 1  # 第 0 条是系统提示词


# ---- 标题、来路、上下文 ----------------------------------------------------------


def test_the_title_says_it_is_a_fork_and_stays_within_the_limit():
    assert fork.fork_title("周报") == "周报（分叉）"
    assert fork.fork_title("") == "新对话（分叉）"
    assert fork.fork_title("周报（分叉）") == "周报（分叉）"  # 分叉出来的再分叉：不再叠一个
    long = "很" * TITLE_LIMIT
    title = fork.fork_title(long)
    assert len(title) <= TITLE_LIMIT and title.endswith("…（分叉）") and title.startswith("很很")
    i18n.choose("en")
    try:
        assert fork.fork_title("Weekly report") == "Weekly report (fork)"
        assert fork.fork_title("Weekly report (fork)") == "Weekly report (fork)"
    finally:
        i18n.choose("zh")


def test_where_a_fork_came_from_is_saved_and_checked_when_read(store):
    source = claude_conversation(store)
    store.save(source)
    new = fork.make(source, CLAUDE, store, 1, "reply").conversation
    store.save(new)
    again = store.load(new.id)
    assert again.forked_from == new.forked_from and again.agent == "资料研究员" and again.title == "原来的会话（分叉）"
    assert again.summary()["forked"] is True and "forked" not in store.load(source.id).summary()
    assert [item["id"] for item in store.summaries() if item.get("forked")] == [new.id]

    good = {"id": source.id, "turn": 2, "at": "reply", "title": "原来的会话", "changes": True}
    assert origin_of(good) == good
    assert origin_of({**good, "title": "长" * 100})["title"] == "长" * TITLE_LIMIT
    assert origin_of({**good, "changes": "yes"})["changes"] is False
    for broken in (None, [], "x", {**good, "id": "../../etc"}, {**good, "turn": 0}, {**good, "turn": True}, {**good, "at": "middle"}, {**good, "title": 3}):
        assert origin_of(broken) is None, broken
    # 文件里这一项坏了：当作没有，整段会话照样读得出来
    path = store.directory / f"{new.id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["forked_from"] = {"id": 42}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert store.load(new.id).forked_from is None


def test_the_context_estimate_skips_compaction_requests():
    assert fork.context_of([]) == 0
    assert fork.context_of([{"type": "text", "text": "没有运行记录"}]) == 0
    events = [trace(500, 5), {"type": "trace", "spans": [{"kind": "model", "input": 800, "output": 8}, {"kind": "tool"}, {"kind": "model", "input": 99, "output": 1, "compaction": True}]}]
    assert fork.context_of(events) == 808  # 最后一份记录里，不算压缩那一次的最后一次请求
    assert fork.context_of([{"type": "trace", "spans": [{"kind": "model", "input": "很多", "output": True}]}]) == 0  # 被改坏了的不算
