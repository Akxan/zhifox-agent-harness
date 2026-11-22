"""备用模型（harness/fallback.py）的测试：一个模型暂时用不了时，这一轮接下来改用同一家的另一个。

和别的测试一样，走真实的 SDK 连本地的假服务器（fake_api.py、fake_openai.py），不花钱。conftest 里的客户端
max_retries=0，所以假服务器回答一次 529，SDK 就直接把错误交上来（真实的程序里 SDK 会先自己重试两次）。要验证的是：

  什么时候换  过载（529）、服务器出错（5xx）、请求太频繁（429）、回复到一半服务器说过载；密钥不对、请求写错了、
              账号额度用完了（OpenAI 的 insufficient_quota）不换。
  换成哪个    你排的顺序里第一个这一轮还没试过、用得了这段对话的工具包的；只在同一家里挑；没设就不换。
  怎么换      历史、系统提示词、工具说明书一个字都不变，只有请求里的 model 变了；失败的那次什么都没留在历史里；
              这一轮接下来都用备用的，下一轮换回你选的那个；对话里说一声（三种语言）。
  记下来      运行记录里失败的那次是 error（写着原因），用备用模型的几次标着 fallback；账本按实际用的模型记钱。
  设置        检查存的名单；网页上的 /api/fallbacks 只认这一家能选的模型。
"""

from __future__ import annotations

import json
import re
import time

import pytest
import test_endpoints_chat
import test_webui
from conftest import RecordingUI
from fake_api import failure, reply, stream_error, text, tool_use
from fake_openai import chat
from fake_openai import failure as openai_failure
from test_agents import delegate, install
from test_endpoints_chat import GEMMA, LLAVA, PROVIDER, QWEN
from test_webui import latest_state

from harness import fallback, i18n
from harness.providers.base import ProviderError, Unavailable
from harness.settings import Settings
from harness.trace.text import lines

serve = test_webui.serve  # 借用网页服务器的组装（fixture）
rig = test_endpoints_chat.rig  # 连着一个假的自定义接口的助手（fixture）

OPUS, SONNET, FABLE = "claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2"
CJK = re.compile(r"[㐀-鿿]")
OVERLOADED = failure(529, "overloaded_error", "Overloaded")


def todo_call(id: str = "t1") -> dict:
    return reply(tool_use(id, "todo_list", {}), stop_reason="tool_use")


def with_backups(s, provider: str = "anthropic", models: list[str] | None = None):
    s.app.settings.set_fallbacks(provider, models if models is not None else [SONNET])
    return s


def ledger_rows(s) -> list[dict]:
    return [json.loads(line) for line in s.config.data_dir.joinpath("ledger.jsonl").read_text().splitlines()]


# ---- 什么时候换 -------------------------------------------------------------


@pytest.mark.parametrize(
    "problem",
    [OVERLOADED, failure(500, "api_error", "Internal server error"), failure(429, "rate_limit_error", "Too many requests"), stream_error("overloaded_error", "Overloaded")],
    ids=["529", "500", "429", "mid-stream"],
)
def test_a_temporary_failure_switches_to_the_backup_for_the_rest_of_the_turn(start, problem):
    s = with_backups(start(problem, todo_call(), reply(text("好了"))))
    s.agent.run_turn("看看待办")

    requests = s.api.requests
    assert [request["model"] for request in requests] == [OPUS, SONNET, SONNET]  # 这一轮接下来都用备用的
    # 换模型只换了请求里的 model：历史（失败的那次什么都没留下）、系统提示词、工具说明书一字不差
    assert requests[0]["messages"] == requests[1]["messages"]
    assert requests[0]["system"] == requests[1]["system"] == requests[2]["system"]
    assert requests[0]["tools"] == requests[1]["tools"] == requests[2]["tools"]
    assert "Opus 5.5 暂时不可用，这一轮接下来改用 Sonnet 5.5。" in s.ui.infos
    assert s.agent.messages[-1]["content"][-1]["text"] == "好了"
    # 一轮结束，换回你选的那个；这段会话记着的也还是它
    assert s.agent.backend.model == OPUS and s.app.conversation.model == OPUS


def test_the_next_turn_tries_your_model_again(start):
    s = with_backups(start(OVERLOADED, reply(text("一")), reply(text("二"))))
    s.agent.run_turn("第一句")
    s.agent.run_turn("第二句")
    assert [request["model"] for request in s.api.requests] == [OPUS, SONNET, OPUS]


@pytest.mark.parametrize(
    "problem",
    [failure(400, "invalid_request_error", "bad request"), failure(401, "authentication_error", "invalid x-api-key"), failure(404, "not_found_error", "no such model")],
    ids=["400", "401", "404"],
)
def test_errors_another_model_would_not_fix_are_not_retried(start, problem):
    s = with_backups(start(problem, reply(text("不该走到这里"))))
    with pytest.raises(ProviderError) as caught:
        s.agent.run_turn("你好")
    assert not isinstance(caught.value, Unavailable)
    assert len(s.api.requests) == 1 and s.agent.messages == []  # 没有换模型重试；那条消息照旧撤回


def test_without_backups_the_error_is_shown_as_before(start):
    s = start(OVERLOADED, reply(text("不该走到这里")))
    with pytest.raises(Unavailable):
        s.agent.run_turn("你好")
    assert len(s.api.requests) == 1 and s.agent.messages == []
    assert not any("暂时不可用" in str(info) for info in s.ui.infos)


def test_only_models_of_the_same_provider_are_used(start):
    # 备用模型是 DeepSeek 的：现在用的是 Claude，接不上这段对话，所以不换（见 fallback.py 开头）
    s = with_backups(start(OVERLOADED, reply(text("不该走到这里"))), "deepseek", ["deepseek-flash"])
    with pytest.raises(Unavailable):
        s.agent.run_turn("你好")
    assert len(s.api.requests) == 1


def test_every_backup_is_tried_once_in_order(start):
    s = with_backups(start(OVERLOADED, OVERLOADED, OVERLOADED, reply(text("不该走到这里"))), models=[SONNET, FABLE])
    with pytest.raises(Unavailable):
        s.agent.run_turn("你好")
    assert [request["model"] for request in s.api.requests] == [OPUS, SONNET, FABLE]
    assert s.agent.messages == [] and s.agent.backend.model == OPUS


def test_your_current_model_in_the_list_is_skipped(start):
    s = with_backups(start(OVERLOADED, reply(text("好了"))), models=[OPUS, SONNET])
    s.agent.run_turn("你好")
    assert [request["model"] for request in s.api.requests] == [OPUS, SONNET]


def test_a_backup_that_cannot_use_the_conversations_toolsets_is_skipped(start):
    s = with_backups(start(OVERLOADED, reply(text("好了"))), models=["claude-3-old", SONNET])
    backend = s.agent.backend
    # 这段对话发的是 Claude 的浏览器工具包（开始时按 Opus 5.5 定下的）：旧模型用不了它，换过去说明书就不对了
    s.agent.on_start = None  # 不让第一条消息之前重新摆工具（那会按现在的工具重新算一遍 toolsets）
    backend.toolsets = {"browser"}
    assert backend.can_switch_to(SONNET) and not backend.can_switch_to("claude-3-old")
    s.agent.run_turn("你好")
    assert [request["model"] for request in s.api.requests] == [OPUS, SONNET]


# ---- 记下来 -----------------------------------------------------------------


def test_the_trace_marks_the_failure_and_the_backup_requests(start):
    s = with_backups(start(OVERLOADED, todo_call(), reply(text("好了"))))
    s.agent.run_turn("看看待办")
    record = s.agent.trace.last
    failed, first, _tool, second = record["spans"]
    assert failed["status"] == "error" and failed["model"] == OPUS and "529" in failed["note"] and "cost" not in failed
    assert "fallback" not in failed
    assert first["model"] == SONNET and first["fallback"] == OPUS and second["fallback"] == OPUS
    assert record["ended"] == "done" and record["totals"]["requests"] == 3
    # 终端的 /trace 也写出来
    text_lines = [line for line, _ in lines(record)]
    assert any("备用模型（替 claude-opus-5-5）" in line for line in text_lines)
    assert any("529" in line for line in text_lines)


def test_the_ledger_prices_the_backup_requests_at_the_backups_price(start):
    s = with_backups(start(OVERLOADED, reply(text("好了"))))
    s.agent.run_turn("你好")
    rows = ledger_rows(s)
    assert [row["model"] for row in rows] == [SONNET]  # 失败的那次没有用量，不记账
    assert rows[0]["cost"] == s.agent.trace.last["spans"][1]["cost"]
    assert rows[0]["cost"] == round((100 * 2 + 40 * 0.1 + 10 * 2.5 + 20 * 10) / 1_000_000, 6)  # Sonnet 5.5 的价格


def test_tasks_channels_and_subagents_use_the_same_setting(start):
    s = with_backups(start(delegate(), OVERLOADED, reply(text("查到了")), reply(text("任务做完"))))
    install(s)
    run = s.app.unattended_agent(RecordingUI())
    assert run.agent.fallbacks is s.agent.fallbacks
    run.agent.run_turn("<task>交给研究员</task>")
    # 主助手（Opus）把事情交给子 Agent；子 Agent 用的也是 Opus，过载了，改用 Sonnet；主助手接着用 Opus
    assert [request["model"] for request in s.api.requests] == [OPUS, OPUS, SONNET, OPUS]


# ---- OpenAI 格式的几家 ------------------------------------------------------


def test_openai_compatible_providers_fall_back_too(start_openai):
    s = start_openai(openai_failure(503, "The server is overloaded"), chat("好了"))
    with_backups(s, "deepseek", ["deepseek-v4-pro"])
    s.agent.run_turn("你好")
    assert [request["model"] for request in s.api.requests] == ["deepseek-flash", "deepseek-v4-pro"]
    assert s.api.requests[0]["messages"] == s.api.requests[1]["messages"]
    assert s.api.requests[0]["tools"] == s.api.requests[1]["tools"]
    assert "deepseek-flash 暂时不可用，这一轮接下来改用 deepseek-v4-pro。" in s.ui.infos
    assert s.agent.backend.model == "deepseek-flash"


def test_a_rate_limit_falls_back_but_an_empty_quota_does_not(start_openai):
    s = start_openai(openai_failure(429, "Rate limit reached"), chat("好了"))
    with_backups(s, "deepseek", ["deepseek-v4-pro"])
    s.agent.run_turn("你好")
    assert [request["model"] for request in s.api.requests] == ["deepseek-flash", "deepseek-v4-pro"]

    s = start_openai(openai_failure(429, "You exceeded your current quota", code="insufficient_quota"), chat("不该走到这里"))
    with_backups(s, "deepseek", ["deepseek-v4-pro"])
    with pytest.raises(ProviderError) as caught:
        s.agent.run_turn("你好")
    assert not isinstance(caught.value, Unavailable)  # 额度用完是整个账号的事，换哪个模型都一样
    assert len(s.api.requests) == 1


def test_a_custom_endpoint_falls_back_to_its_own_models_by_the_switching_rule(rig):
    # 自定义接口（endpoints/）：备用模型是同一个接口上的别的模型；和换模型的面板同一条规矩（providers.continues）：
    # 用不了工具的（gemma2:2b）接不上这段对话，跳过；换上的那个连能力一起换（llava 看得懂图），一轮结束换回来
    # 自定义接口的客户端是正式的那个（endpoints/client.make，SDK 自己先重试两次）：三次都失败了才轮到备用模型
    busy = [openai_failure(503, "The server is overloaded")] * 3
    h = rig(*busy, chat("备用的答了"), models=(QWEN, GEMMA, LLAVA))
    with_backups(h, PROVIDER, ["gemma2:2b", "llava:13b"])
    h.agent.run_turn("你好")
    assert [request["model"] for request in h.server.requests] == ["qwen3:8b"] * 3 + ["llava:13b"]
    assert h.server.requests[0]["messages"] == h.server.requests[-1]["messages"]
    assert (h.agent.backend.model, h.agent.backend.vision) == ("qwen3:8b", False)  # 换回了你选的那个，能力也换回来
    assert not fallback.usable(h.agent.backend, "gemma2:2b") and fallback.usable(h.agent.backend, "llava:13b")


# ---- 设置和名字 -------------------------------------------------------------


def test_the_saved_list_is_checked(tmp_path):
    assert fallback.fallback_settings(None) == {}
    assert fallback.fallback_settings(["claude-sonnet-5-5"]) == {}
    saved = {
        "anthropic": [SONNET, SONNET, " ", 3, FABLE, "claude-opus-5", "claude-haiku-5-5"],  # 重复的、不是字的去掉，最多 3 个
        "deepseek": [],  # 空的不留
        "Not A Provider!": [SONNET],
        "openai": "gpt-5",
    }
    assert fallback.fallback_settings(saved) == {"anthropic": [SONNET, FABLE, "claude-opus-5"]}

    settings = Settings(tmp_path / "settings.json", "anthropic", OPUS)
    assert settings.fallbacks == {}
    settings.save()
    assert "fallbacks" not in json.loads((tmp_path / "settings.json").read_text())  # 没设的人，设置文件和以前一样
    settings.set_fallbacks("anthropic", [SONNET])
    assert Settings(tmp_path / "settings.json", "anthropic", OPUS).fallbacks == {"anthropic": [SONNET]}
    settings.set_fallbacks("anthropic", [])
    assert Settings(tmp_path / "settings.json", "anthropic", OPUS).fallbacks == {}


def test_model_names_and_the_notice_in_every_language():
    assert fallback.label("claude-opus-5-5") == "Opus 5.5"
    assert fallback.label("claude-opus-5") == "Opus 5"
    assert fallback.label("deepseek-flash") == "deepseek-flash"
    for language in ("en", "es"):
        i18n.choose(language)
        said = str(fallback.notice(OPUS, SONNET))
        assert "Opus 5.5" in said and "Sonnet 5.5" in said and not CJK.search(said)
    i18n.choose("zh")


def test_the_settings_endpoint(serve):
    s = serve()
    status, body = s.browser.post("/api/fallbacks", {"provider": "anthropic", "models": [SONNET, FABLE]})
    assert status == 200 and body["fallbacks"]["chosen"] == {"anthropic": [SONNET, FABLE]}
    assert s.app.settings.fallbacks == {"anthropic": [SONNET, FABLE]}
    # 存好以后发一次新的状态（回答和状态走两条路，状态可能晚一点到）
    deadline = time.monotonic() + 5
    while latest_state(s.browser)["fallbacks"]["chosen"] != {"anthropic": [SONNET, FABLE]}:
        assert time.monotonic() < deadline, "状态里没有新的备用模型"
        time.sleep(0.01)
    assert latest_state(s.browser)["fallbacks"]["max"] == 3

    for bad in (
        {"provider": "anthropic", "models": ["deepseek-flash"]},  # 另一家的模型
        {"provider": "anthropic", "models": [SONNET, SONNET]},
        {"provider": "anthropic", "models": [SONNET, FABLE, OPUS, "claude-3-old"]},  # 太多
        {"provider": "nobody", "models": []},
        {"provider": "anthropic", "models": "claude-sonnet-5-5"},
    ):
        status, body = s.browser.post("/api/fallbacks", bad)
        assert status == 400 and body["error"]
    assert s.app.settings.fallbacks == {"anthropic": [SONNET, FABLE]}  # 出错的一次都没存

    status, _ = s.browser.post("/api/fallbacks", {"provider": "anthropic", "models": []})
    assert status == 200 and s.app.settings.fallbacks == {}
