"""GPT、Gemini、DeepSeek 共用的适配层的测试：真实的 openai SDK + 本地的假服务器。"""

from __future__ import annotations

import json

import openai
import pytest
from fake_openai import FakeOpenAI, call, chat
from sample_files import PDF, PNG, document, docx, image, pdf, text_file

from harness.attachments import parse
from harness.documents import NOTE as NOTE_OFFICE
from harness.providers import PROVIDERS, ProviderError, list_models

FILE_TOOL = "str_replace_based_edit_tool"


@pytest.fixture
def start(start_openai):
    """这个文件里的测试都用 DeepSeek（OpenAI 格式）的 agent。"""
    return start_openai


def test_plain_answer_sends_expected_request(start):
    h = start(chat("你好！"))
    h.agent.run_turn("你好")

    assert h.ui.streamed == [("text", "你"), ("text", "好！")]  # 文字是一小段一小段到的
    (request,) = h.api.requests
    assert request["model"] == "deepseek-flash"
    assert request["stream"] is True and request["stream_options"] == {"include_usage": True}
    # 系统提示词是第一条消息；用户消息是一段文字，开头带着当前时间
    system, user = request["messages"]
    assert system["role"] == "system" and "个人助手" in system["content"]
    assert user == {"role": "user", "content": "<context>当前时间：2026-11-20 14:03 星期五</context>\n\n你好"}
    # 工具用各家通用的"函数"格式声明；联网搜索是 Anthropic 服务器上的功能，这里没有
    assert all(tool["type"] == "function" for tool in request["tools"])
    names = [tool["function"]["name"] for tool in request["tools"]]
    assert names == [
        "todo_add", "todo_list", "todo_update", "todo_delete", "task_add", "task_list", "task_delete", "memory", FILE_TOOL,
        "run_command", "skill",
        "web_fetch",  # 读网页：这家没有 Anthropic 服务器上的联网工具，用智狐自己的（见 tools/fetch.py）
        "agent",  # agent：把任务交给子 Agent，一直都在（见 tools/agent.py）
    ]
    memory = request["tools"][7]["function"]
    assert "rename" in memory["parameters"]["properties"]["command"]["enum"] and "/memories" in memory["description"]
    assert "additionalProperties" not in json.dumps(request["tools"])
    assert h.agent.backend.tool_names() == [(name, True) for name in names]
    # 历史：系统提示词、用户、助手
    assert [message["role"] for message in h.agent.messages] == ["system", "user", "assistant"]
    assert h.agent.messages[-1] == {"role": "assistant", "content": "你好！"}


def test_tool_call_runs_and_result_goes_back(start):
    arguments = json.dumps({"title": "交报告", "due": "2026-11-27"}, ensure_ascii=False)
    h = start(chat(calls=[call("call_1", "todo_add", arguments)]), chat("记好了。"))
    h.agent.run_turn("提醒我下周五交报告")

    assert [item["title"] for item in h.todos.items()] == ["交报告"]  # 分两段到达的参数被正确拼了起来
    first, second = h.api.requests
    _, _, assistant, result = second["messages"]
    assert assistant == {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "todo_add", "arguments": arguments}}],
    }
    assert result["role"] == "tool" and result["tool_call_id"] == "call_1"
    assert '"title": "交报告"' in result["content"]
    assert second["messages"][:2] == first["messages"]  # 只追加，不改动前面的内容


def test_reasoning_and_provider_extras_are_sent_back_unchanged(start):
    signature = {"google": {"thought_signature": "sig-abc"}}
    h = start(
        chat(reasoning="先查一下待办", calls=[call("call_1", "todo_list", "{}", extra_content=signature)]),
        chat("没有待办。"),
    )
    h.agent.run_turn("我有什么待办？")

    assert ("thinking", "先查一下待办") in h.ui.streamed
    assistant = h.api.requests[1]["messages"][2]
    assert assistant["reasoning_content"] == "先查一下待办"  # DeepSeek 要求把思考内容发回去
    assert assistant["tool_calls"][0]["extra_content"] == signature  # Gemini 要求把签名发回去


def test_each_tool_result_is_its_own_message(start):
    h = start(
        chat(calls=[call("call_1", "todo_add", '{"title": "甲"}'), call("call_2", "todo_update", '{"id": 9, "done": true}')]),
        chat("好了。"),
    )
    h.agent.run_turn("加一条，再把 9 号标成完成")

    first, second = h.api.requests[1]["messages"][3:]
    assert (first["role"], first["tool_call_id"]) == ("tool", "call_1")
    assert (second["role"], second["tool_call_id"]) == ("tool", "call_2")
    assert second["content"].startswith("[出错] ") and "没有编号为 9 的待办" in second["content"]


def test_unparseable_arguments_are_reported_not_executed(start):
    h = start(chat(calls=[call("call_1", "run_command", '{"command": "touch x.txt"')]), chat("我重新来。"))
    h.agent.run_turn("建个文件")

    assert not (h.config.workspace / "x.txt").exists()
    assert h.ui.confirmations == []
    result = h.api.requests[1]["messages"][-1]
    assert "INVALID_JSON" in result["content"] and result["tool_call_id"] == "call_1"


def test_commands_still_need_confirmation(start):
    h = start(
        chat(calls=[call("call_1", "run_command", '{"command": "touch made.txt"}')]),
        chat("好的。"),
        approve=False,
        feedback="先别建",
    )
    h.agent.run_turn("建个文件")

    assert not (h.config.workspace / "made.txt").exists()
    assert "touch made.txt" in h.ui.confirmations[0]
    assert h.api.requests[1]["messages"][-1]["content"] == "[出错] 用户拒绝了这个操作。用户说：先别建"


def test_usage_counts_cached_tokens_in_both_styles(start):
    openai_style = {"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050,
                    "prompt_tokens_details": {"cached_tokens": 600}}
    deepseek_style = {"prompt_tokens": 2000, "completion_tokens": 70, "total_tokens": 2070,
                      "prompt_cache_hit_tokens": 1500, "prompt_cache_miss_tokens": 500}
    h = start(chat(calls=[call("call_1", "todo_list", "{}")], usage=openai_style), chat("没有。", usage=deepseek_style))
    h.agent.run_turn("待办？")

    usage = h.agent.usage
    assert (usage.requests, usage.input_tokens, usage.cache_read_tokens) == (2, 400 + 500, 600 + 1500)
    assert (usage.output_tokens, usage.cache_write_tokens, usage.searches) == (120, 0, 0)
    assert usage.context_tokens == 2070
    # DeepSeek 分时段计价，闲时的用量记在"@offpeak"下面。测试的时钟是本地时间，是不是闲时要看机器在哪个时区，所以只比模型名
    assert [key.removesuffix("@offpeak") for key in h.agent.usage_by_model] == ["deepseek-flash"]


def test_truncated_and_filtered_replies(start):
    h = start(chat("写到一半", finish="length"))
    h.agent.run_turn("写篇长文")
    assert "截断" in h.ui.infos[-1]
    assert [message["role"] for message in h.agent.messages] == ["system", "user", "assistant"]

    h = start(chat("", finish="content_filter"), chat("当然。"))
    h.agent.run_turn("一个会被拦下的请求")
    assert "拒绝" in h.ui.infos[-1]
    assert [message["role"] for message in h.agent.messages] == ["system"]  # 整轮撤销，只剩系统提示词
    h.agent.run_turn("换个问题")
    assert [message["role"] for message in h.api.requests[1]["messages"]] == ["system", "user"]


def test_errors_are_explained(start):
    h = start(key="bad-key")
    with pytest.raises(ProviderError, match="DeepSeek 的 API 密钥无效"):
        h.agent.run_turn("你好")
    assert [message["role"] for message in h.agent.messages] == ["system"]  # 用户消息被撤回

    h = start()  # 没有预设回复：假服务器返回 500
    with pytest.raises(ProviderError, match="500"):
        h.agent.run_turn("你好")


def test_interrupt_during_tools_keeps_history_valid(start):
    h = start(
        chat(calls=[call("call_1", "run_command", '{"command": "touch a.txt"}'),
                    call("call_2", "run_command", '{"command": "touch b.txt"}')]),
        chat("好的，停下了。"),
    )

    def interrupt(description):
        raise KeyboardInterrupt

    h.ui.confirm = interrupt
    with pytest.raises(KeyboardInterrupt):
        h.agent.run_turn("建两个文件")

    # 两个工具调用都补上了结果
    assert [m.get("tool_call_id") for m in h.agent.messages[-2:]] == ["call_1", "call_2"]
    h.agent.run_turn("算了")
    assert [m["role"] for m in h.api.requests[1]["messages"]] == ["system", "user", "assistant", "tool", "tool", "user"]


def test_model_list_is_filtered(make_config):
    api = FakeOpenAI([], models=["models/gemini-4-neon", "gemini-3.8-flash", "text-embedding-005", "imagen-5"]).start()
    try:
        client = openai.OpenAI(api_key="test-key", base_url=api.base_url, max_retries=0)
        assert list_models(PROVIDERS["gemini"], client) == ["gemini-3.8-flash", "gemini-4-neon"]
        api.models = ["gpt-6-luna", "gpt-6.2-astra", "text-embedding-3-large", "tts-1", "whisper-1"]
        assert list_models(PROVIDERS["openai"], client) == ["gpt-6-luna", "gpt-6.2-astra"]

        bad = openai.OpenAI(api_key="bad-key", base_url=api.base_url, max_retries=0)
        with pytest.raises(ProviderError, match="API 密钥无效"):
            list_models(PROVIDERS["deepseek"], bad)
    finally:
        api.stop()


def test_switching_models_and_providers(start):
    h = start(chat("你好。"), chat("还是我。"), chat("新的开始。"))
    h.agent.run_turn("你好")

    assert h.app.select_model("deepseek", "deepseek-v4-pro") is True  # 同一家：对话继续
    h.agent.run_turn("你是谁")
    assert h.api.requests[1]["model"] == "deepseek-v4-pro"
    assert len(h.api.requests[1]["messages"]) == 4  # 上一轮的内容还在

    assert h.app.select_model("gemini", "gemini-4-neon") is False  # 换了一家：新对话
    assert h.agent.backend.provider == "gemini"
    h.agent.run_turn("你好")
    assert [message["role"] for message in h.api.requests[2]["messages"]] == ["system", "user"]
    # 选择被保存了下来；用量按模型分开记
    saved = json.loads(h.config.settings_file.read_text(encoding="utf-8"))
    assert (saved["provider"], saved["model"]) == ("gemini", "gemini-4-neon")
    assert sorted(key.removesuffix("@offpeak") for key in h.agent.usage_by_model) == ["deepseek-flash", "deepseek-v4-pro", "gemini-4-neon"]
    assert h.agent.usage.requests == 3


# ---- 附件 -----------------------------------------------------------------

NOTE = "<context>当前时间：2026-11-20 14:03 星期五</context>"


def test_gpt_gets_images_as_data_urls_before_the_words(start):
    h = start(chat("一张很小的图。"), chat("不客气。"))
    assert h.app.select_model("openai", "gpt-6-luna") is False  # 换到 GPT（还是同一个假服务器）
    h.agent.run_turn("看看这个", parse([text_file("a.txt", "hello"), image()]))

    user = h.api.requests[0]["messages"][1]
    # 带附件时内容是一个列表：图片在前，然后是文本文件，最后是时间说明和用户的话
    assert user == {
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}},
            {"type": "text", "text": '<attachment name="a.txt">\nhello\n</attachment>'},
            {"type": "text", "text": f"{NOTE}\n\n看看这个"},
        ],
    }
    # 没有附件的消息还是一段文字，和以前一样
    h.agent.run_turn("谢谢")
    assert h.api.requests[1]["messages"][3] == {"role": "user", "content": f"{NOTE}\n\n谢谢"}
    assert h.api.requests[1]["messages"][:3] == h.api.requests[0]["messages"] + [h.agent.messages[2]]


def test_gpt_gets_pdfs_as_file_parts(start):
    # OpenAI 自己的写法：{"type": "file", "file": {"filename", "file_data"}}，file_data 是 data: 地址
    h = start(chat("一份报告。"))
    h.app.select_model("openai", "gpt-6-luna")
    h.agent.run_turn("", parse([pdf(), image()]))
    assert h.api.requests[0]["messages"][1]["content"][:2] == [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}},
        {"type": "file", "file": {"filename": "报告.pdf", "file_data": f"data:application/pdf;base64,{PDF}"}},
    ]


def test_office_files_reach_every_provider_as_their_text(start):
    # DeepSeek 只认文字，Word 文件在助手程序里取出文字以后，它也能读。文字旁边注明图片和排版没有了
    body = '<w:p><w:r><w:t>口令是 ORCHID-913</w:t></w:r></w:p>'
    h = start(chat("读完了。"))
    h.agent.run_turn("", parse([document("周报.docx", docx(body))]))
    assert h.api.requests[0]["messages"][1]["content"][0] == {
        "type": "text",
        "text": f'<attachment name="周报.docx" note="{NOTE_OFFICE}">\n口令是 ORCHID-913\n</attachment>',
    }


def test_text_files_work_with_every_provider(start):
    h = start(chat("读完了。"))  # DeepSeek 看不了图片，但文本文件就是一段文字
    h.agent.run_turn("", parse([text_file()]))
    assert h.api.requests[0]["messages"][1]["content"] == [
        {"type": "text", "text": '<attachment name="笔记.md">\n第一行\n第二行\n</attachment>'},
        {"type": "text", "text": NOTE},
    ]


def test_models_that_cannot_take_an_attachment_refuse_it_before_sending(start):
    h = start()
    with pytest.raises(ProviderError, match="看不了图片") as raised:
        h.agent.run_turn("看图", parse([image()]))
    assert str(raised.value) == "当前的模型（DeepSeek）看不了图片。可以在设置里换成 Claude、GPT 或 Gemini。"

    h.app.select_model("gemini", "gemini-4-pro")  # Gemini 看得了图片，但读不了 PDF
    with pytest.raises(ProviderError) as raised:
        h.agent.run_turn("读一下", parse([pdf()]))
    assert str(raised.value) == "当前的模型（Gemini (Google)）读不了 PDF。可以在设置里换成 Claude 或 GPT，或者把需要的文字复制进来。"

    assert h.api.requests == []  # 一个请求都没有发出去
    assert h.agent.backend.export() == []  # 历史里什么都没留下
