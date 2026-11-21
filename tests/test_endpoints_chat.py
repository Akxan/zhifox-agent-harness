"""自定义模型接口上的对话：真实的 openai SDK + 本地的假"Ollama"（tests/fake_openai.py），不花钱。

测的是：整个对话循环和工具调用照常走；没做全的服务器（不认 stream_options、模型用不了工具）退一步、说清楚、记下来；
上下文上限、价格按这个模型自己的；别的几家的密钥一个都不带过去；换模型时同一条规矩（continue / fresh / leave）；
换模型的目录和"智狐能做的事"照着它画。
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import anthropic
import pytest
from conftest import RecordingUI
from fake_api import FakeAPI, reply, text
from fake_openai import FakeOpenAI, call, chat
from sample_files import image

from harness import capabilities, model_catalog
from harness.app import build_app
from harness.attachments import parse
from harness.conversations import ConversationError
from harness.endpoints import EndpointError, EndpointStore
from harness.endpoints.client import NO_KEY
from harness.providers import ProviderError, find, make_client, resolve
from harness.settings import Settings

PROVIDER = "custom-ollama"
QWEN = {"id": "qwen3:8b", "vision": False, "tools": True, "pdf": False}
LLAVA = {"id": "llava:13b", "vision": True, "tools": True, "pdf": False}
GEMMA = {"id": "gemma2:2b", "vision": False, "tools": False, "pdf": False}


@pytest.fixture
def rig(make_config):
    """一个连着假 Ollama 的智狐。endpoints.json 和设置在启动之前写好，就像你在设置里加过、选过它一样。

    内置的几家（Claude）连另一个假服务器：自定义接口用的是真正的 make_client（别的几家的密钥不带过去、不跟重定向），
    所以这里测到的就是正式程序发出去的请求。
    """
    servers = []

    def start(*replies, models=(QWEN,), api_key="", select=True, base_url=None, claude=(), name="Ollama", **server_options):
        server = FakeOpenAI(list(replies), models=[model["id"] for model in models], **server_options).start()
        claude_api = FakeAPI(list(claude)).start()
        servers.extend([server, claude_api])
        config = make_config()
        store = EndpointStore(config.endpoints_file)
        store.save({"name": name, "base_url": base_url or server.base_url + "/v1", "api_key": api_key, "models": list(models)})
        if select:
            Settings(config.settings_file, "anthropic", "claude-opus-5-5").choose(PROVIDER, models[0]["id"])
        claude_client = anthropic.Anthropic(api_key="test-key", base_url=claude_api.base_url, max_retries=0)
        ui = RecordingUI()
        app = build_app(config, ui, client_factory=lambda provider: make_client(provider) if provider.custom else claude_client)
        app.agent.clock = lambda: datetime(2026, 11, 20, 14, 3)
        return SimpleNamespace(app=app, agent=app.agent, server=server, claude=claude_api, ui=ui, config=config)

    yield start
    for server in servers:
        server.stop()


def tool_names(request: dict) -> list[str]:
    return [tool["function"]["name"] for tool in request.get("tools", [])]


# ---- 对话 ------------------------------------------------------------------


def test_a_full_turn_goes_to_the_endpoint(rig):
    h = rig(chat("你好，我在你的电脑上。"))
    assert h.agent.backend.provider == PROVIDER and h.agent.backend.model == "qwen3:8b"
    h.agent.run_turn("你好")

    (request,) = h.server.requests
    assert h.server.paths[-1] == "/v1/chat/completions"
    assert request["model"] == "qwen3:8b" and request["stream_options"] == {"include_usage": True}
    assert request["messages"][0]["role"] == "system" and request["messages"][-1]["content"].endswith("你好")
    assert "todo_add" in tool_names(request) and "run_command" in tool_names(request)
    assert h.server.keys == [NO_KEY]  # 没填密钥：放一个占位的，不去拿 OPENAI_API_KEY
    assert "".join(piece for kind, piece in h.ui.streamed if kind == "text") == "你好，我在你的电脑上。"
    # 本机的模型不花钱：账本里记的是 0 元（有价格），不是"没有价格"
    assert list(h.agent.usage_by_model) == ["custom-ollama/qwen3:8b"]
    report = h.app.ledger.report(datetime(2026, 11, 20).date(), 30)
    spent = report["periods"]["all"]
    assert spent["models"][0]["provider"] == PROVIDER and spent["models"][0]["cost"] == 0 and not spent["unpriced"]


def test_tool_calls_run_and_their_results_go_back(rig):
    arguments = json.dumps({"title": "交报告", "due": "2026-11-27"}, ensure_ascii=False)
    h = rig(chat(calls=[call("call_1", "todo_add", arguments)]), chat("记好了。"))
    h.agent.run_turn("提醒我下周五交报告")
    assert [item["title"] for item in h.app.todos.items()] == ["交报告"]
    first, second = h.server.requests
    assert second["messages"][-1]["role"] == "tool" and "交报告" in second["messages"][-1]["content"]
    assert second["messages"][: len(first["messages"])] == first["messages"]  # 只在末尾追加


def test_other_providers_keys_never_reach_the_endpoint(rig, monkeypatch):
    """OpenAI 的 SDK 会自己去环境变量里找密钥和请求头：这些都不能跟着请求发到你填的地址去。"""
    leaks = {
        "OPENAI_API_KEY": "sk-" + "openai-should-not-leak",
        "OPENAI_ADMIN_KEY": "sk-" + "admin-should-not-leak",
        "OPENAI_ORG_ID": "org-should-not-leak",
        "OPENAI_PROJECT_ID": "proj-should-not-leak",
        "OPENAI_CUSTOM_HEADERS": "X-Leak: header-should-not-leak\nAuthorization: Bearer env-auth-should-not-leak",
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "ANTHROPIC_API_KEY": "sk-" + "ant-should-not-leak",
        "DEEPSEEK_API_KEY": "sk-" + "deepseek-should-not-leak",
    }
    for name, value in leaks.items():
        monkeypatch.setenv(name, value)
    h = rig(chat("好的"), api_key="endpoint-own-key-1234")
    h.agent.run_turn("你好")
    h.app.sync_models(PROVIDER)

    assert h.server.keys and set(h.server.keys) == {"endpoint-own-key-1234"}
    sent = json.dumps(h.server.headers)
    for value in leaks.values():
        for piece in value.split("\n"):
            assert piece.split(": ")[-1] not in sent, piece
    assert not any(name in headers for headers in h.server.headers for name in ("openai-organization", "openai-project", "x-leak"))
    assert h.server.paths[0].startswith("/v1/")  # 用的是你填的地址，不是 OPENAI_BASE_URL


# ---- 没做全的服务器：退一步 ----------------------------------------------------


def test_a_model_without_tools_answers_in_text_and_it_is_remembered(rig):
    h = rig(chat("我只能用文字回答。"), chat("还是文字。"), models=(GEMMA | {"tools": True},), tools=False)
    h.agent.run_turn("帮我建个文件")

    refused, answered = h.server.requests
    assert refused["tools"] and "tools" not in answered  # 被拒绝的那一次服务器没处理，退一步再请求一次
    assert answered["messages"] == refused["messages"]  # 历史一个字都没改
    assert any("用不了工具" in info for info in h.ui.infos)
    assert h.agent.backend.tools_supported is False
    assert find(PROVIDER).endpoint.caps("gemma2:2b").tools is False  # 记进了 endpoints.json
    assert EndpointStore(h.config.endpoints_file).get(PROVIDER).caps("gemma2:2b").tools is False

    # "智狐能做的事"：每一项都用不了，原因说清楚，给一个"换模型"
    overview = capabilities.overview(h.app)
    assert overview.items and all(item.status == "unavailable" for item in overview.items)
    assert all("用不了工具" in item.reason and item.actions[0].kind == "models" for item in overview.items)
    assert "用不了工具" in overview.notes[0]

    # 下一段对话一开始就不发工具，不再碰一次壁
    h.app.new_conversation()
    h.agent.run_turn("再来")
    assert len(h.server.requests) == 3 and "tools" not in h.server.requests[2]
    assert "<skills>" not in json.dumps(h.server.requests[2]["messages"], ensure_ascii=False)  # Skill 的目录也不附


def test_once_tools_were_used_a_refusal_is_a_real_error(rig):
    h = rig(chat(calls=[call("call_1", "todo_list", "{}")]), chat("没有待办。"), chat("不会到这里"))
    h.agent.run_turn("我有什么待办")
    h.server.tools = False  # 服务器后来不认工具了（比如换了启动参数）：这段对话已经用过工具，不悄悄退
    with pytest.raises(ProviderError) as caught:
        h.agent.run_turn("再看看")
    assert "does not support tools" in str(caught.value)
    assert h.agent.backend.tools_supported is True
    assert find(PROVIDER).endpoint.caps("qwen3:8b").tools is True


def test_a_server_without_stream_options_still_works(rig):
    h = rig(chat("好的"), chat("又好了"), stream_options=False)
    h.agent.run_turn("你好")
    refused, answered = h.server.requests
    assert "stream_options" in refused and "stream_options" not in answered
    assert any("stream_options" in info for info in h.ui.infos)
    assert find(PROVIDER).endpoint.stream_usage is False
    h.app.new_conversation()
    h.agent.run_turn("再来")
    assert len(h.server.requests) == 3 and "stream_options" not in h.server.requests[2]


def test_built_in_providers_never_quietly_drop_tools(start_openai):
    """内置的三家被拒绝了就是真的出错了（工具的说明书写坏了之类），照常报错，不悄悄绕过去。"""
    from fake_openai import failure

    h = start_openai(failure(400, "Invalid schema for function 'todo_add'"))
    with pytest.raises(ProviderError):
        h.agent.run_turn("你好")
    assert len(h.api.requests) == 1 and h.agent.backend.tools_supported is True


def test_images_follow_the_models_own_flag(rig):
    h = rig(chat("看到了一张图。"), models=(QWEN, LLAVA))
    picture = parse([image()])
    assert "看不了图片" in h.app.attachment_problem(picture)
    h.app.select_model(PROVIDER, "llava:13b")  # 同一个接口、都能用工具：接着聊
    assert h.app.attachment_problem(picture) == "" and h.agent.backend.vision is True
    h.agent.run_turn("这是什么", picture)
    content = h.server.requests[0]["messages"][-1]["content"]
    assert content[0]["type"] == "image_url"


def test_the_models_own_context_window_decides_when_to_compact(rig):
    small = QWEN | {"context": 2000}
    h = rig(chat("一"), chat("二", usage={"prompt_tokens": 1500, "completion_tokens": 20, "total_tokens": 1520}), chat("摘要"), chat("三"), models=(small,))
    assert h.agent.backend.context_window == 2000
    for message in ("第一轮", "第二轮", "第三轮"):
        h.agent.run_turn(message)
    # 1520 超过了 2000 的 70%：第三轮之前先写了一次摘要（内置的表里不认识这个模型，按 128k 算的话根本不会压缩）
    assert len(h.server.requests) == 4
    assert h.server.requests[2]["messages"][-1]["content"].startswith("（这条消息是助手程序自动发的")


# ---- 价格 ------------------------------------------------------------------


def test_a_paid_gateway_is_priced_by_what_you_entered(rig):
    gateway = {"id": "deepseek-v4-pro", "vision": False, "tools": True, "pdf": False, "price": {"input": 1.0, "output": 2.0}}
    h = rig(chat("好"), models=(gateway,), base_url=None)
    h.agent.run_turn("你好")
    report = h.app.ledger.report(datetime(2026, 11, 20).date(), 30)
    # 100 个输入、20 个输出：(100 × 1 + 20 × 2) / 100 万
    assert report["periods"]["all"]["models"][0]["cost"] == pytest.approx(140 / 1_000_000)
    # 同名的内置模型照样按 DeepSeek 自己的价格算，不会被本机这一个改掉
    assert h.app.price_of("deepseek", "deepseek-v4-pro")["input"] == 1.32
    assert h.app.price_of(PROVIDER, "deepseek-v4-pro")["input"] == 1.0


def test_changing_a_price_from_settings_writes_to_the_endpoint(rig):
    h = rig()
    h.app.set_price(PROVIDER, "qwen3:8b", {"input": 0.1, "output": 0.2})
    assert EndpointStore(h.config.endpoints_file).get(PROVIDER).price_of("qwen3:8b")["output"] == 0.2
    assert "qwen3:8b" not in h.app.settings.prices  # 不写进内置模型的价格表
    h.app.set_price(PROVIDER, "qwen3:8b", None)
    assert h.app.price_of(PROVIDER, "qwen3:8b")["input"] == 0  # 回到默认：本机的不花钱


# ---- 换模型 -----------------------------------------------------------------


def test_switching_rules_and_the_catalog(rig):
    h = rig(chat("一"), models=(QWEN, LLAVA, GEMMA), claude=[reply(text("我是 Claude"))])
    found = model_catalog.catalog(h.app)
    group = next(entry for entry in found.groups if entry.id == PROVIDER)
    assert group.custom and group.where == "local" and group.has_key and group.key_hint == ""
    assert [model.id for model in group.models] == ["qwen3:8b", "llava:13b", "gemma2:2b"]
    tags = {model.id: [str(tag) for tag in model.tags] for model in group.models}
    assert tags["qwen3:8b"] == ["只认文字", "能用工具", "本地"]
    assert tags["llava:13b"] == ["看得懂图片", "能用工具", "本地"]
    assert tags["gemma2:2b"] == ["只认文字", "不能用工具", "本地"]
    assert all(str(model.free) == "本地，不计费" and model.price["input"] == 0 for model in group.models)
    # 还没聊：都能直接换过去
    assert group.switch == "continue" and {model.id: model.switch for model in group.models}["gemma2:2b"] == "fresh"

    h.agent.run_turn("你好")
    found = model_catalog.catalog(h.app)
    group = next(entry for entry in found.groups if entry.id == PROVIDER)
    switch = {model.id: model.switch or group.switch for model in group.models}
    # 同一个接口、都能用工具：接着聊；能不能用工具不一样：先问一声，再开始新的一段
    assert switch == {"qwen3:8b": "continue", "llava:13b": "continue", "gemma2:2b": "leave"}
    assert next(entry for entry in found.groups if entry.id == "anthropic").switch == "leave"

    first = h.app.conversation.id
    assert h.app.select_model(PROVIDER, "llava:13b") is True and h.app.conversation.id == first
    assert h.app.select_model(PROVIDER, "gemma2:2b") is False and h.app.conversation.id != first
    assert h.agent.backend.tools_supported is False and h.agent.backend.tool_specs == []
    assert h.app.select_model("anthropic", "claude-opus-5-5") is False  # 换回内置的：另一家
    assert h.agent.backend.provider == "anthropic"
    assert h.app.select_model(PROVIDER, "qwen3:8b") is False  # 再换回来：也是另一家


def test_lines_for_the_terminal(rig):
    h = rig(models=(QWEN,))
    printed = "\n".join(line for line, _ in model_catalog.lines(model_catalog.catalog(h.app)))
    assert "Ollama  [自定义接口 · 这台电脑]" in printed and "qwen3:8b" in printed and "本地，不计费" in printed


def test_agent_files_and_other_programs_can_name_an_endpoint(rig):
    from harness.agents.spec import resolve_model

    rig(select=False)
    assert resolve_model("custom-ollama/qwen3:8b") == ("custom-ollama/qwen3:8b", None, None)
    assert resolve_model("custom-ollama/meta-llama/Llama-3.1-8B")[0] == "custom-ollama/meta-llama/Llama-3.1-8B"
    _, problem, _ = resolve_model("custom-nothing/x")
    assert problem and "custom-ollama" in problem  # 列出能用的里面有它
    # 别的程序（命令行的 --model、评测）用 providers.resolve：同一种写法，交回 Provider 和模型名
    provider, model = resolve("custom-ollama/meta-llama/Llama-3.1-8B")
    assert (provider.id, provider.custom, model) == ("custom-ollama", True, "meta-llama/Llama-3.1-8B")
    assert resolve("anthropic/claude-opus-5-5")[0].id == "anthropic"
    for bad in ("custom-nothing/x", "custom-ollama", "custom-ollama/", "qwen3"):
        with pytest.raises(ProviderError):
            resolve(bad)


# ---- 改、删 ----------------------------------------------------------------


def test_the_endpoint_in_use_cannot_be_deleted(rig):
    h = rig()
    with pytest.raises(EndpointError):
        h.app.delete_endpoint(PROVIDER)
    h.app.select_model("anthropic", "claude-opus-5-5")
    h.app.delete_endpoint(PROVIDER)
    assert find(PROVIDER) is None


def test_a_conversation_on_a_deleted_endpoint_explains_itself(rig):
    h = rig(chat("一"), claude=[reply(text("好"))])
    h.agent.run_turn("你好")
    h.app.save_conversation([{"type": "user", "text": "你好"}])
    saved = h.app.conversation.id
    h.app.select_model("anthropic", "claude-opus-5-5")
    h.app.delete_endpoint(PROVIDER)
    with pytest.raises(ConversationError) as caught:
        h.app.open_conversation(saved)
    assert "custom-ollama" in str(caught.value) and "删掉" in str(caught.value)
    with pytest.raises(ProviderError):
        h.app.select_model(PROVIDER, "qwen3:8b")


def test_editing_the_endpoint_in_use_takes_effect(rig, make_config):
    moved = FakeOpenAI([chat("新地址")], models=["qwen3:8b"]).start()
    try:
        h = rig(chat("旧地址"))
        # 还没开始聊：整个换成新的（地址、能力都马上按新的）
        h.app.save_endpoint({"name": "Ollama", "base_url": moved.base_url + "/v1", "api_key": "", "models": [QWEN | {"tools": False}]}, PROVIDER)
        assert h.agent.backend.tools_supported is False
        h.agent.run_turn("你好")
        assert h.server.requests == [] and len(moved.requests) == 1 and "tools" not in moved.requests[0]
    finally:
        moved.stop()


def test_the_choice_falls_back_when_the_endpoint_is_gone(make_config):
    config = make_config()
    Settings(config.settings_file, "anthropic", "claude-opus-5-5").choose("custom-gone", "x")
    app = build_app(config, RecordingUI(), client_factory=lambda provider: None)
    assert (app.settings.provider, app.settings.model) == ("anthropic", "claude-opus-5-5")


# ---- 评测（harness eval）--------------------------------------------------------


def test_an_eval_can_target_a_local_endpoint(make_config, tmp_path, monkeypatch):
    """harness eval run --model custom-ollama/qwen3:8b：评一个本地模型。本地的接口不花钱，所以"真的运行"也是 0 元；
    每一遍的数据目录里都抄了一份 endpoints.json（不然 build_app 不认得这个接口）。"""
    from test_evals import SIMPLE, cli, write_task

    from harness.evals import results

    monkeypatch.delenv("CI", raising=False)
    server = FakeOpenAI([chat("你好！我在本地。")], models=["qwen3:8b"]).start()
    try:
        config = make_config()
        EndpointStore(config.endpoints_file).save({"name": "Ollama", "base_url": server.base_url + "/v1", "api_key": "", "models": [QWEN]})
        write_task(tmp_path / "tasks", "hello", SIMPLE)
        code, out = cli(config, "run", "--real", "--yes", "--model", "custom-ollama/qwen3:8b", "--tasks-dir", str(tmp_path / "tasks"), interactive=False)
        assert code == 0, out
        assert "$0" in out  # 估算：本地的模型不花钱
        (request,) = server.requests
        assert request["model"] == "qwen3:8b" and set(server.keys) == {NO_KEY}
        run = results.find_run(config.data_dir / "evals", "latest")
        assert run.meta["provider"] == "custom-ollama" and run.rows[0]["passed"] and run.rows[0]["cost"] == 0
        rows = [json.loads(line) for line in config.ledger_file.read_text().splitlines()]
        assert [(row["provider"], row["source"], row["cost"]) for row in rows] == [("custom-ollama", "eval", 0)]
    finally:
        server.stop()
