"""GPT、Gemini、DeepSeek 的压缩历史（见 harness/providers/compaction.py）：真实的 openai SDK + 本地的假服务器。

测试里的 agent 用的是 deepseek-flash，它的上下文上限是 100 万 token，用到 70% 就压缩。
假服务器回复里的用量是写好的，所以不用真的发几十万字，把用量写成 80 万就能让下一次请求之前压缩。
"""

from __future__ import annotations

import json

import openai
import pytest
from fake_openai import FakeOpenAI, call, chat, failure
from sample_files import image, pdf, text_file

from harness import config as settings
from harness.app import build_app, run_turn
from harness.attachments import parse
from harness.providers import ProviderError, compaction
from harness.webui.ui import WebUI

BIG = {"prompt_tokens": 800_000, "completion_tokens": 200, "total_tokens": 800_200}  # 超过了 100 万的 70%
NEAR = {"prompt_tokens": 690_000, "completion_tokens": 200, "total_tokens": 690_200}  # 还差一点
# 写摘要那次请求的用量：输入 5 万（其中 4.9 万命中缓存），输出 3000，其中 1000 是思考
SUMMARY_USAGE = {
    "prompt_tokens": 50_000,
    "completion_tokens": 3_000,
    "total_tokens": 53_000,
    "prompt_cache_hit_tokens": 49_000,
    "prompt_cache_miss_tokens": 1_000,
    "completion_tokens_details": {"reasoning_tokens": 1_000},
}
STARTED = "对话已经很长，正在请模型把较早的内容写成摘要……"
DONE = "较早的对话已经压缩成摘要，之后每一轮发给模型的内容少了约 {n} 个 token。"


@pytest.fixture
def start(start_openai):
    return start_openai


def say(h, *texts: str) -> None:
    for text in texts:
        h.agent.run_turn(text)


def roles(messages: list[dict]) -> list[str]:
    return [message["role"] for message in messages]


def assert_tool_calls_answered(messages: list[dict]) -> None:
    """每个工具调用后面紧跟着它的结果，每个结果前面都有它的调用。少了哪一样，API 都会拒绝这个请求。"""
    waiting: set[str] = set()
    for message in messages:
        if message["role"] == "tool":
            assert message["tool_call_id"] in waiting, message
            waiting.discard(message["tool_call_id"])
            continue
        assert not waiting, f"这些调用没有结果：{waiting}"
        waiting = {one["id"] for one in message.get("tool_calls") or []}
    assert not waiting, f"这些调用没有结果：{waiting}"


# ---- 什么时候压缩 ---------------------------------------------------------


def test_a_long_conversation_is_compacted_before_the_next_request(start):
    h = start(chat("一"), chat("二", usage=BIG), chat("前面聊了一和二。", usage=SUMMARY_USAGE), chat("三"))
    say(h, "第一轮", "第二轮")
    before = list(h.agent.messages)  # 系统提示词、两轮问答
    say(h, "第三轮")

    _, second, summary_request, third = h.api.requests
    # 写摘要的请求：要压缩的那一段原样不动，末尾加一条指令。开头和上一次请求一模一样（工具说明书也一样），
    # 所以这一段能命中缓存
    assert summary_request["messages"] == [*before[:3], {"role": "user", "content": compaction.SUMMARY_REQUEST}]
    assert summary_request["messages"][:3] == second["messages"][:3]
    assert summary_request["tools"] == second["tools"]
    assert summary_request["model"] == "deepseek-flash" and summary_request["stream"] is True

    # 真正的请求：系统提示词原样、两条摘要消息、最近两轮（上一轮和这一轮）的原文
    system, summary, ack, *recent = third["messages"]
    assert system == before[0]
    assert summary["role"] == "user"
    assert summary["content"] == (
        "<conversation_summary>\n前面聊了一和二。\n</conversation_summary>\n\n" + compaction.SUMMARY_NOTE
    )
    assert ack == {"role": "assistant", "content": compaction.SUMMARY_ACK}
    assert recent[:2] == before[3:5]
    assert roles(recent) == ["user", "assistant", "user"] and recent[2]["content"].endswith("第三轮")
    assert h.agent.messages == [*third["messages"], {"role": "assistant", "content": "三"}]

    # 摘要不显示成回答；界面上先说"正在压缩"，做完再说少了多少：输入 5 万，减去摘要本身 2000（3000 里 1000 是思考）
    assert "".join(text for kind, text in h.ui.streamed if kind == "text") == "一二三"
    assert h.ui.infos == [STARTED, "较早的对话已经压缩成摘要，之后每一轮发给模型的内容少了约 48,000 个 token。"]
    assert h.ui.infos[1].key == DONE and h.ui.infos[1].values == {"n": "48,000"}  # 带着原文，页面换语言时能重译


def test_the_summary_call_is_counted_like_any_other(start):
    h = start(chat("一"), chat("二", usage=BIG), chat("摘要", usage=SUMMARY_USAGE), chat("三"))
    say(h, "第一轮", "第二轮", "第三轮")

    usage = h.agent.usage
    assert usage.requests == 4  # 三次回答，加一次写摘要
    assert usage.input_tokens == 100 + 800_000 + 1_000 + 100
    assert usage.cache_read_tokens == 49_000
    assert usage.output_tokens == 20 + 200 + 3_000 + 20
    assert usage.context_tokens == 120  # 对话的大小是最后一次真正的请求量出来的，不是写摘要那次的
    # 按模型分开记的用量里也有它（算费用用的就是这一份）
    (key,) = h.agent.usage_by_model
    assert key.removesuffix("@offpeak") == "deepseek-flash"
    assert h.agent.usage_by_model[key].requests == 4


def test_no_compaction_below_the_threshold(start):
    h = start(chat("一", usage=NEAR), chat("二", usage=NEAR), chat("三", usage=NEAR), chat("四"))
    say(h, "第一轮", "第二轮", "第三轮", "第四轮")

    assert len(h.api.requests) == 4
    assert all(compaction.SUMMARY_REQUEST not in json.dumps(request, ensure_ascii=False) for request in h.api.requests)
    assert roles(h.agent.messages) == ["system"] + ["user", "assistant"] * 4
    assert h.ui.infos == []


def test_nothing_to_compact_while_every_turn_is_recent(start):
    # 超过了阈值，但对话只有两轮，都是要原样保留的"最近几轮"：没有可以压缩的，不发写摘要的请求
    h = start(chat("一", usage=BIG), chat("二", usage=BIG))
    say(h, "第一轮", "第二轮")
    assert len(h.api.requests) == 2 and h.ui.infos == []


def test_compaction_can_be_turned_off(start, monkeypatch):
    monkeypatch.setattr(compaction, "ENABLE_OPENAI_COMPACTION", False)
    h = start(chat("一"), chat("二", usage=BIG), chat("三", usage=BIG))
    say(h, "第一轮", "第二轮", "第三轮")

    assert len(h.api.requests) == 3
    assert roles(h.api.requests[2]["messages"]) == ["system"] + ["user", "assistant"] * 2 + ["user"]
    assert h.ui.infos == []


def test_the_window_is_the_current_models(start):
    # 10 万 token 对 deepseek-flash（100 万）还早，对不认识的模型（按 12.8 万算）已经过了 70%
    middle = {"prompt_tokens": 100_000, "completion_tokens": 200, "total_tokens": 100_200}
    assert compaction.context_window("deepseek-flash") == 1_000_000
    assert compaction.context_window("some-new-model") == settings.DEFAULT_CONTEXT_WINDOW == 128_000
    h = start(chat("一"), chat("二", usage=middle), chat("摘要"), chat("三"))
    say(h, "第一轮", "第二轮")

    assert h.app.select_model("deepseek", "deepseek-chat") is True  # 同一家里换模型：对话继续
    say(h, "第三轮")
    summary_request, third = h.api.requests[2:]
    assert summary_request["model"] == "deepseek-chat"  # 摘要也是由换上的那个模型写的
    assert summary_request["messages"][-1]["content"] == compaction.SUMMARY_REQUEST
    assert compaction.is_summary(third["messages"][1]) and third["model"] == "deepseek-chat"


# ---- 切在哪里、换成什么 ---------------------------------------------------


def test_compaction_never_separates_tool_calls_from_their_results(start):
    h = start(
        chat(calls=[call("call_1", "todo_add", '{"title": "甲"}')]),
        chat("记下了甲。"),
        chat("不客气。"),
        chat(calls=[call("call_2", "todo_list", "{}")], usage=BIG),  # 第三轮的第一步就超过了阈值
        chat("摘要", usage=SUMMARY_USAGE),  # 于是第二步之前先压缩：在一轮的中间
        chat("你有一条待办：甲。"),
    )
    say(h, "记一下甲", "谢谢", "我有什么待办")

    summary_request, last = h.api.requests[4:]
    # 第一轮整轮写进摘要：工具调用和它的结果都在里面
    assert roles(summary_request["messages"]) == ["system", "user", "assistant", "tool", "assistant", "user"]
    # 留下的是上一轮和正在进行的这一轮，这一轮的工具调用和结果都在
    assert roles(last["messages"]) == ["system", "user", "assistant", "user", "assistant", "user", "assistant", "tool"]
    assert last["messages"][-2]["tool_calls"][0]["id"] == "call_2" and last["messages"][-1]["tool_call_id"] == "call_2"
    for request in h.api.requests:
        assert_tool_calls_answered(request["messages"])
    assert_tool_calls_answered(h.agent.messages)


def test_a_second_compaction_folds_in_the_first_summary(start):
    h = start(
        chat("一"),
        chat("二", usage=BIG),
        chat("摘要甲", usage=SUMMARY_USAGE),
        chat("三", usage=BIG),
        chat("摘要乙", usage=SUMMARY_USAGE),
        chat("四"),
    )
    say(h, "第一轮", "第二轮", "第三轮", "第四轮")

    second_summary_request, last = h.api.requests[4:]
    # 上一份摘要不算一轮：它和第二轮一起写进新的摘要
    assert compaction.is_summary(second_summary_request["messages"][1])
    assert "摘要甲" in second_summary_request["messages"][1]["content"]
    assert roles(second_summary_request["messages"]) == ["system", "user", "assistant", "user", "assistant", "user"]
    # 历史里永远只有一份摘要，后面是最近两轮
    assert sum(compaction.is_summary(message) for message in last["messages"]) == 1
    assert "摘要乙" in last["messages"][1]["content"] and "摘要甲" not in last["messages"][1]["content"]
    assert [message["content"].split("\n\n")[-1] for message in last["messages"][3::2]] == ["第三轮", "第四轮"]


def test_attachments_in_the_compacted_part_become_a_short_mention(start):
    h = start(chat("都看到了。"), chat("二", usage=BIG), chat("用户发了一张图、一份报告和一个文本文件。"), chat("三"))
    assert h.app.select_model("openai", "gpt-6-luna") is False  # GPT 看得了图片、读得了 PDF（还是同一个假服务器）
    h.agent.run_turn("看看这些", parse([text_file("a.txt", "hello"), image(), pdf()]))
    say(h, "第二轮", "第三轮")

    summary_request, last = h.api.requests[2:]
    # 写摘要时，模型还看得到原件，可以在摘要里写下里面的内容
    assert [part["type"] for part in summary_request["messages"][1]["content"]] == ["image_url", "file", "text", "text"]
    # 压缩之后原件不在了，只留下一句提到它们的话（图片没有文件名，只说有几张）
    sent = json.dumps(last["messages"], ensure_ascii=False)
    assert "image_url" not in sent and "file_data" not in sent and "hello" not in sent
    assert last["messages"][1]["content"].endswith(
        compaction.ATTACHMENTS_NOTE.format(names="报告.pdf、a.txt、图片 1 张")
    )


def test_where_the_cut_goes():
    system = {"role": "system", "content": "提示词"}

    def turn(n: int) -> list[dict]:
        return [{"role": "user", "content": f"问{n}"}, {"role": "assistant", "content": f"答{n}"}]

    messages = [system, *turn(1), *turn(2), *turn(3)]
    assert compaction.find_cut(messages, keep_turns=2) == 3  # 切在第二轮的开头
    assert compaction.find_cut(messages, keep_turns=1) == 5
    assert compaction.find_cut(messages, keep_turns=0) == 5  # 正在进行的那一轮总要留着
    assert compaction.find_cut(messages, keep_turns=3) is None  # 三轮都要留：没有可以压缩的
    assert compaction.find_cut([system], keep_turns=1) is None
    # 上一次留下的摘要不算一轮
    folded = compaction.compacted(messages, 3, "摘要")
    assert compaction.find_cut(folded, keep_turns=2) is None
    assert compaction.find_cut([*folded, *turn(4)], keep_turns=2) == 5


# ---- 出了问题 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("bad_reply", "reason"),
    [
        (failure(503, "overloaded"), "503"),
        (chat(""), "模型没有写出摘要。"),
        (chat(calls=[call("call_9", "run_command", '{"command": "rm -rf ~"}')]), "模型没有写出摘要。"),  # 只调用了工具
    ],
)
def test_a_failed_summary_keeps_the_conversation_going(start, bad_reply, reason):
    h = start(
        chat("一"),
        chat("二", usage=BIG),
        bad_reply,  # 第三轮之前该压缩了，但写摘要失败
        chat(calls=[call("call_1", "todo_list", "{}")], usage=BIG),  # 这一轮照常进行
        chat("三", usage=BIG),  # 同一轮里的下一步不再试
        chat("摘要", usage=SUMMARY_USAGE),  # 下一轮再试，这次成了
        chat("四"),
    )
    say(h, "第一轮", "第二轮", "第三轮")

    failed, step_one, step_two = h.api.requests[2:]
    assert failed["messages"][-1]["content"] == compaction.SUMMARY_REQUEST
    assert roles(step_one["messages"]) == ["system"] + ["user", "assistant"] * 2 + ["user"]  # 历史原样，没被压缩
    assert roles(step_two["messages"])[-2:] == ["assistant", "tool"]  # 紧接着就是真正的请求，没有再试一次
    error = h.ui.infos[1]
    assert h.ui.infos[0] == STARTED and error.startswith("没能把较早的对话压缩成摘要") and reason in error
    assert h.ui.calls == ["todo_list"]  # 写摘要时模型调用的工具不会被执行
    assert h.agent.usage.requests == 4  # 失败的那次请求没有返回用量

    say(h, "第四轮")
    summary_request, last = h.api.requests[5:]
    assert summary_request["messages"][-1]["content"] == compaction.SUMMARY_REQUEST
    assert compaction.is_summary(last["messages"][1])
    assert_tool_calls_answered(last["messages"])


def test_a_request_that_fails_after_compacting_is_undone_cleanly(start):
    # 第一轮带一次工具调用，被压缩掉的就是四条消息，换成两条摘要：历史变短了，循环记下的位置要跟着换算
    h = start(
        chat(calls=[call("call_1", "todo_list", "{}")]),
        chat("一"),
        chat("二", usage=BIG),
        chat("摘要", usage=SUMMARY_USAGE),  # 摘要之后没有回复了：假服务器返回 500
    )
    say(h, "第一轮", "第二轮")
    with pytest.raises(ProviderError, match="500"):
        h.agent.run_turn("第三轮")

    # 摘要留着（它本身没问题），这一轮的用户消息被撤回，不会在重试时出现两遍；写摘要的钱也记上了
    assert roles(h.agent.messages) == ["system", "user", "assistant", "user", "assistant"]
    assert compaction.is_summary(h.agent.messages[1])
    assert h.agent.usage.requests == 4 and h.agent.usage.cache_read_tokens == 49_000

    h.api.replies.append(chat("三"))
    h.agent.run_turn("再说一次第三轮")
    retried = h.api.requests[-1]["messages"]
    assert [message["content"].split("\n\n")[-1] for message in retried if message["role"] == "user"][1:] == [
        "第二轮",
        "再说一次第三轮",
    ]


def test_undoing_a_refused_turn_after_compacting_in_its_middle(start):
    # 一轮进行到一半时压缩了，然后这一轮被拒绝：整轮撤销，前面的摘要和上一轮原样留着
    h = start(
        chat(calls=[call("call_1", "todo_list", "{}")]),
        chat("一"),
        chat("二"),
        chat(calls=[call("call_2", "todo_list", "{}")], usage=BIG),
        chat("摘要", usage=SUMMARY_USAGE),
        chat("", finish="content_filter"),
        chat("好的。"),
    )
    say(h, "第一轮", "第二轮", "第三轮")
    assert "拒绝" in h.ui.infos[-1]
    assert roles(h.agent.messages) == ["system", "user", "assistant", "user", "assistant"]
    assert compaction.is_summary(h.agent.messages[1]) and h.agent.messages[3]["content"].endswith("第二轮")

    say(h, "换个问题")
    assert roles(h.api.requests[-1]["messages"]) == ["system", "user", "assistant", "user", "assistant", "user"]


# ---- 保存和重新打开 -------------------------------------------------------


def test_a_saved_conversation_keeps_what_the_user_saw_and_continues_from_the_summary(make_config):
    api = FakeOpenAI(
        [
            chat("蓝莓放冰箱能放一周。"),
            chat("二", usage=BIG),
            chat("用户问过蓝莓怎么保存：冰箱里一周。", usage=SUMMARY_USAGE),
            chat("三"),
            chat("四"),
        ]
    ).start()
    try:
        config = make_config(default_provider="deepseek", default_model="deepseek-flash")
        client = openai.OpenAI(api_key="test-key", base_url=api.base_url, max_retries=0)

        def launch() -> tuple:
            ui = WebUI()
            return build_app(config, ui, client_factory=lambda provider: client), ui

        app, ui = launch()
        for text in ("蓝莓怎么保存", "第二轮", "第三轮"):
            app.name_conversation(text)
            ui.begin_turn(text)
            run_turn(app.agent, ui, text)
            ui.end_turn()
            assert app.save_conversation(ui.export_events())
        conversation_id = app.conversation.id
        saved = json.loads((config.conversations_dir / f"{conversation_id}.json").read_text(encoding="utf-8"))

        # messages 是发给模型的那一份：已经压缩过，第一轮只剩在摘要里
        assert compaction.is_summary(saved["messages"][0])
        assert "蓝莓放冰箱能放一周" not in json.dumps(saved["messages"], ensure_ascii=False)
        # events 是界面上显示过的全部内容：第一轮的问答还在，压缩时的两句提示也在（带着原文，换语言能重译）
        assert [event["text"] for event in saved["events"] if event["type"] == "user"] == ["蓝莓怎么保存", "第二轮", "第三轮"]
        assert "蓝莓放冰箱能放一周。" in [event["text"] for event in saved["events"] if event["type"] == "text"]
        assert [event["key"] for event in saved["events"] if event["type"] == "notice"] == [STARTED, DONE]
        # 搜索看的也是 events：压缩掉的那一轮照样搜得到
        assert [item["id"] for item in app.store.search("冰箱")] == [conversation_id]

        # 重新启动程序，打开这段会话：页面上重放出完整的对话，模型那边接着压缩后的历史聊
        app, ui = launch()
        conversation = app.open_conversation(conversation_id)
        assert app.agent.messages[1:] == saved["messages"]
        ui.load_events(conversation.events)
        assert "蓝莓放冰箱能放一周。" in [event.get("text") for event in ui.events_after(0, timeout=0)]
        run_turn(app.agent, ui, "第四轮")
        last = api.requests[-1]["messages"]
        assert compaction.is_summary(last[1])
        assert [message["content"].split("\n\n")[-1] for message in last if message["role"] == "user"][1:] == [
            "第二轮",
            "第三轮",
            "第四轮",
        ]
        assert len(api.requests) == 5  # 打开旧会话之后没有再压缩一次
    finally:
        api.stop()
