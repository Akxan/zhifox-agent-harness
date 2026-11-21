"""换模型的目录（harness/model_catalog.py）：网页上换模型面板背后的 POST /api/model/catalog，和终端的 /model。

目录回答的是几个问题：每一家有没有密钥、能选哪些模型、每个模型能做什么、多少钱、换过去会怎样。
网页和终端都照着同一份目录画，规矩只在 model_catalog.py 里算一份，所以这里分三层测：
  - 网页拿到的 JSON：真实的 HTTP 服务器 + 本地的假 API（和 test_webui.py 一样，不花钱）；
  - 换模型的接口和目录对不对得上：同一家接着聊、换一家开始新的一段（旧的那段还在）、
    聊天通道占着这段会话时同一家也不许换（两边会抢着写同一个文件）、对话进行中目录照样能看但不能换；
  - 终端：money() 的写法、lines() 打印的几行、/model 命令。
密钥只露最后 4 位：目录既发给浏览器，也打在终端上，所以每一层都查一遍完整的密钥没有出现。
"""

from __future__ import annotations

import json

import pytest
import test_webui
from fake_api import reply, text, tool_use
from test_agents_web import agent_file
from test_webui import OK, act, latest_state, saved_ids, say

from harness import model_catalog
from harness.cli import handle_command
from harness.i18n import t
from harness.model_catalog import TEXT_ONLY, WEB, Catalog, Group, Model, Persona
from harness.pricing import price_of
from harness.providers import CLAUDE_MODELS, PROVIDERS
from harness.ui import TerminalUI

# 借用 test_webui 的 fixture：起一个网页服务器，后面连着假的 API，并让"浏览器"连上去
serve = test_webui.serve
CLAUDE_KEY = "test-key"  # serve 默认给 Claude 设置的密钥
DEEPSEEK_KEY = "fake-deepseek-key-0002"
CLAUDE_TAGS = ["能联网", "看得懂图片", "能读 PDF"]


@pytest.fixture
def rig(serve, monkeypatch):
    """和 serve 一样，另外交出服务器上的 WebApp（s.web）：要模拟"聊天通道正占着这段会话"，得直接碰它的 claims。"""
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
        return s

    return start


def catalog(s) -> dict:
    status, body = s.browser.post("/api/model/catalog")
    assert status == 200 and body["ok"] is True, body
    return body["catalog"]


def group(found: dict, provider_id: str) -> dict:
    return next(entry for entry in found["groups"] if entry["id"] == provider_id)


def model(found: dict, provider_id: str, model_id: str) -> dict:
    return next(entry for entry in group(found, provider_id)["models"] if entry["id"] == model_id)


def ids(entry: dict) -> list[str]:
    return [item["id"] for item in entry["models"]]


def current(found: dict) -> list[tuple[str, str]]:
    """打着勾的那些（应该恰好一个）：（哪一家，哪个模型）。"""
    return [(entry["id"], item["id"]) for entry in found["groups"] for item in entry["models"] if item["current"]]


def switches(found: dict) -> dict[str, str]:
    return {entry["id"]: entry["switch"] for entry in found["groups"]}


def tag_keys(item: dict) -> list[str]:
    """标签的原文：页面换语言时照着它重译，所以测原文，不测译好的字。"""
    return [tag["key"] for tag in item["tags"]]


def add_deepseek(s) -> None:
    """填一个 DeepSeek 的假密钥。保存以后服务器马上同步模型列表（假服务器上是 serve 的 other_models）。"""
    assert s.browser.post("/api/key", {"provider": "deepseek", "key": DEEPSEEK_KEY}) == OK


# ---- 目录里有什么 -------------------------------------------------------------


def test_every_provider_is_listed_with_its_key_models_tags_and_prices(serve):
    s = serve()
    found = catalog(s)
    assert [entry["id"] for entry in found["groups"]] == list(PROVIDERS)  # 顺序和 PROVIDERS 一样
    assert (found["provider"], found["model"], found["started"], found["persona"]) == ("anthropic", "claude-opus-5-5", False, None)

    claude = group(found, "anthropic")
    assert claude["label"] == "Claude (Anthropic)"
    assert claude["has_key"] is True and claude["key_hint"] == "…-key"
    assert claude["synced"] is False and ids(claude) == CLAUDE_MODELS  # 没同步过：先用内置的几个
    assert current(found) == [("anthropic", "claude-opus-5-5")]
    for other in found["groups"][1:]:  # 没有密钥、没同步过的：一个模型都没有
        assert (other["has_key"], other["key_hint"], other["synced"], other["models"]) == (False, "", False, [])

    assert all(tag_keys(item) == CLAUDE_TAGS for item in claude["models"])
    opus = model(found, "anthropic", "claude-opus-5-5")
    assert opus["tags"][0] == {"text": "能联网", "key": "能联网", "values": {}}  # 和别的事件一样，带着原文
    assert opus["price"] == price_of("claude-opus-5-5", {}) and opus["offpeak"] is None  # 不分时段
    assert CLAUDE_KEY not in json.dumps(found, ensure_ascii=False)  # 只有最后 4 位


def test_without_web_search_no_model_says_it_can_search(serve):
    s = serve(web=False)  # 联网搜索整个关掉了（config.web）
    assert all(tag_keys(item) == ["看得懂图片", "能读 PDF"] for item in group(catalog(s), "anthropic")["models"])


def test_the_current_model_is_listed_even_when_the_synced_list_lacks_it(serve):
    s = serve()
    s.app.settings.set_models("anthropic", ["claude-sonnet-5-5"])  # 很久以前同步的列表，里面没有现在用的这个
    claude = group(catalog(s), "anthropic")
    assert claude["synced"] is True and ids(claude) == ["claude-opus-5-5", "claude-sonnet-5-5"]


def test_a_saved_key_brings_synced_models_with_offpeak_prices(serve):
    s = serve()
    add_deepseek(s)
    deepseek = group(catalog(s), "deepseek")
    assert deepseek["has_key"] is True and deepseek["key_hint"] == "…0002" and deepseek["synced"] is True
    assert ids(deepseek) == ["deepseek-flash", "deepseek-v4-pro"]
    assert not any(item["current"] for item in deepseek["models"])
    for item in deepseek["models"]:
        assert tag_keys(item) == ["只认文字", "分时段计价"]  # 看不懂图片也读不了 PDF：明说，免得你以为它也行
        assert item["price"] == price_of(item["id"], {})
        assert item["offpeak"] == price_of(item["id"], {}, offpeak=True)
        assert all(item["offpeak"][part] < item["price"][part] for part in item["price"])  # 闲时每一项都更便宜
    assert DEEPSEEK_KEY not in json.dumps(catalog(s), ensure_ascii=False)


def test_entered_prices_win_and_unknown_models_have_none(serve):
    s = serve(other_models=("deepseek-flash", "deepseek-lab"))
    add_deepseek(s)
    lab = model(catalog(s), "deepseek", "deepseek-lab")  # 没有内置价格，你也没填
    assert (lab["price"], lab["offpeak"], tag_keys(lab)) == (None, None, ["只认文字"])

    assert s.browser.post("/api/price", {"model": "claude-sonnet-5-5", "price": {"input": 3, "output": 15}}) == (200, {"ok": True})
    assert model(catalog(s), "anthropic", "claude-sonnet-5-5")["price"] == {"input": 3.0, "output": 15.0, "cache_read": 3.0, "cache_write": 3.0}
    # 自己填了价格就全天按它算：不再有闲时价，"分时段计价"也不标了
    assert s.browser.post("/api/price", {"model": "deepseek-flash", "price": {"input": 1, "output": 2}})[0] == 200
    flash = model(catalog(s), "deepseek", "deepseek-flash")
    assert flash["price"]["input"] == 1.0 and flash["offpeak"] is None and tag_keys(flash) == ["只认文字"]


# ---- 换过去会怎样 -------------------------------------------------------------


def test_switching_elsewhere_is_fresh_until_the_conversation_starts(serve):
    s = serve(reply(text("你好。")))
    found = catalog(s)
    assert found["started"] is False
    assert switches(found) == {"anthropic": "continue", "openai": "fresh", "gemini": "fresh", "deepseek": "fresh"}

    say(s, "你好")
    found = catalog(s)
    assert found["started"] is True  # 聊过了：换到另一家之前，页面要先在原地问一声
    assert switches(found) == {"anthropic": "continue", "openai": "leave", "gemini": "leave", "deepseek": "leave"}

    act(s, "/api/new")
    assert set(switches(catalog(s)).values()) == {"continue", "fresh"}  # 新的一段还没聊


def test_switching_within_a_provider_moves_the_check_mark_and_keeps_the_conversation(serve):
    s = serve(reply(text("你好。")))
    conversation_id = say(s, "你好")["conversation"]
    assert act(s, "/api/model", {"provider": "anthropic", "model": "claude-sonnet-5-5"})["conversation"] == conversation_id
    found = catalog(s)
    assert (found["provider"], found["model"], found["started"]) == ("anthropic", "claude-sonnet-5-5", True)
    assert current(found) == [("anthropic", "claude-sonnet-5-5")]


def test_switching_to_another_provider_starts_anew_and_keeps_the_old_conversation(serve):
    s = serve(reply(text("我是 Claude。")))
    old = say(s, "你是谁")["conversation"]
    add_deepseek(s)
    assert group(catalog(s), "deepseek")["switch"] == "leave"

    state = act(s, "/api/model", {"provider": "deepseek", "model": "deepseek-v4-pro"})
    assert state["conversation"] != old
    assert old in saved_ids(s.config) and old in [item["id"] for item in state["conversations"]]  # 随时可以回去接着聊
    found = catalog(s)
    assert (found["provider"], found["model"], found["started"]) == ("deepseek", "deepseek-v4-pro", False)
    assert current(found) == [("deepseek", "deepseek-v4-pro")]
    assert switches(found) == {"anthropic": "fresh", "openai": "fresh", "gemini": "fresh", "deepseek": "continue"}


def test_no_switch_within_a_provider_while_a_chat_channel_holds_the_conversation(rig):
    s = rig(reply(text("你好。")))
    conversation_id = say(s, "你好")["conversation"]
    assert s.web.claims.take(conversation_id, "telegram:1") is None  # Telegram 那边正在这段会话里跑一轮

    status, body = s.browser.post("/api/model", {"provider": "anthropic", "model": "claude-sonnet-5-5"})
    assert status == 409 and "这段会话正在 Telegram 上进行" in body["error"], body
    assert (s.app.settings.model, s.app.agent.backend.model, s.app.conversation.model) == ("claude-opus-5-5",) * 3
    assert current(catalog(s)) == [("anthropic", "claude-opus-5-5")]
    assert s.web.claims.holder(conversation_id) == "telegram:1"  # 被拒绝的请求不会把别人的认领放掉

    s.web.claims.release(conversation_id, "telegram:1")  # 那边这一轮结束了：现在可以换
    assert act(s, "/api/model", {"provider": "anthropic", "model": "claude-sonnet-5-5"})["conversation"] == conversation_id
    assert current(catalog(s)) == [("anthropic", "claude-sonnet-5-5")]


def test_the_catalog_answers_during_a_turn_but_the_model_cannot_change(serve):
    s = serve(
        reply(tool_use("toolu_1", "run_command", {"command": "true"}), stop_reason="tool_use"),
        reply(text("好。")),
    )
    assert s.browser.post("/api/message", {"text": "跑一下"})[0] == 200
    confirm = s.browser.wait_for("confirm")  # 这一轮卡在确认上，对话的锁还拿着

    found = catalog(s)  # 只读，不拿锁
    assert found["started"] is True and current(found) == [("anthropic", "claude-opus-5-5")]
    status, body = s.browser.post("/api/model", {"provider": "anthropic", "model": "claude-sonnet-5-5"})
    assert status == 409 and "请先等当前这一轮对话结束" in body["error"]

    s.browser.post("/api/confirm", {"id": confirm["id"], "allow": True})
    s.browser.wait_for("turn_end")
    assert catalog(s)["model"] == "claude-opus-5-5" and s.api.requests[1]["model"] == "claude-opus-5-5"


# ---- 角色 ---------------------------------------------------------------------


def test_the_persona_is_named_and_kept_across_providers(serve):
    s = serve(reply(text("好的。")))
    add_deepseek(s)
    assert s.browser.post("/api/agent/create", {"text": agent_file("研究员")})[0] == 200  # 不写 model：跟着现在用的
    assert s.browser.post("/api/agent/create", {"text": agent_file("深度", model="deepseek/deepseek-flash")})[0] == 200

    act(s, "/api/new", {"agent": "研究员"})
    assert catalog(s)["persona"] == {"name": "研究员", "model": ""}
    say(s, "你好")
    act(s, "/api/model", {"provider": "deepseek", "model": "deepseek-v4-pro"})  # 换了一家：新的一段，角色不变
    found = catalog(s)
    assert (found["provider"], found["started"], found["persona"]) == ("deepseek", False, {"name": "研究员", "model": ""})
    assert s.app.conversation.agent == "研究员" and latest_state(s.browser)["persona"]["name"] == "研究员"

    act(s, "/api/new", {"agent": "深度"})  # 它文件里选了模型：用它开始的对话就用那个
    found = catalog(s)
    assert found["persona"] == {"name": "深度", "model": "deepseek/deepseek-flash"}
    assert current(found) == [("deepseek", "deepseek-flash")]
    act(s, "/api/model", {"provider": "anthropic", "model": "claude-sonnet-5-5"})
    found = catalog(s)
    assert found["provider"] == "anthropic" and found["persona"] == {"name": "深度", "model": "deepseek/deepseek-flash"}
    assert s.app.conversation.agent == "深度"


# ---- 终端 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "written"),
    [(4.0, "$4"), (20.0, "$20"), (0.0, "$0"), (0.3, "$0.30"), (1.32, "$1.32"), (0.006, "$0.006"), (0.044, "$0.044"), (0.05, "$0.05"),
     # 你自己填的很便宜的价格：不能写成 "$0."（以前四舍五入到三位、再去掉末尾的 0 就是这样）
     (0.0001, "$0.0001"), (0.0995, "$0.0995"), (2.5, "$2.50")],
)
def test_money(value, written):
    assert model_catalog.money(value) == written


def test_lines_mark_the_current_model_and_say_which_providers_lack_a_key():
    price = {"input": 4.0, "output": 20.0, "cache_read": 0.2, "cache_write": 5.0}
    found = Catalog(
        provider="anthropic",
        model="claude-opus-5-5",
        started=True,
        persona=Persona(name="写作助手", model=""),
        groups=[
            Group("anthropic", "Claude (Anthropic)", True, "…-key", False, "continue", [
                Model("claude-opus-5-5", True, [t(WEB)], price, None),
                Model("claude-later", False, [t(TEXT_ONLY)], None, None),
            ]),
            Group("openai", "GPT (OpenAI)", True, "…abcd", False, "leave", []),
            Group("deepseek", "DeepSeek", False, "", False, "leave", []),
        ],
    )
    out = model_catalog.lines(found)
    texts = [line for line, _ in out]
    assert {style for _, style in out} == {"plain", "info"}
    assert ("Claude (Anthropic)  [已设置密钥 …-key]", "plain") in out
    assert "  ✓ claude-opus-5-5  ·  能联网  ·  $4 / $20" in texts
    assert "    claude-later  ·  只认文字  ·  没有价格" in texts
    assert sum("✓" in line for line in texts) == 1
    # 有密钥、没有模型列表的：告诉你去哪里同步；没有密钥的：只写一行"未设置密钥"，不列模型
    after_openai = texts[texts.index("GPT (OpenAI)  [已设置密钥 …abcd]") + 1]
    assert "还没有模型列表" in after_openai
    deepseek = texts.index("DeepSeek  [未设置密钥]")
    assert not texts[deepseek + 1].startswith("  ")
    assert any("「写作助手」" in line and "角色不变" in line for line in texts)


def test_the_model_command_prints_the_catalog(start, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-terminal-9876")
    ui = TerminalUI()
    h = start(ui=ui)
    capsys.readouterr()
    assert handle_command("/model", h.app, ui) is True  # 返回 True：不是退出
    out = capsys.readouterr().out
    assert "✓ claude-opus-5-5" in out and "$4 / $20" in out and out.count("✓") == 1
    assert "[已设置密钥 …9876]" in out and "sk-ant-fake-terminal-9876" not in out
    assert "DeepSeek  [未设置密钥]" in out and "GPT (OpenAI)  [未设置密钥]" in out
