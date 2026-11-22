"""网页上的"自定义模型接口"：真实的 HTTP 服务器（server.py）+ 假的"Ollama"（tests/fake_openai.py），不花钱。

测的是 /api/endpoint/…（保存、删除、测试连接、在访达中显示）、状态里的那一节、换模型（/api/model）、
同步模型（/api/models）、价格（/api/price）、换模型的目录（/api/model/catalog）。
密钥只露最后 4 位：发给浏览器的每一个事件、每一个回答里都找不到完整的密钥。
"""

from __future__ import annotations

import json
import os
import threading
from types import SimpleNamespace

import anthropic
import openai
import pytest
import test_webui
from fake_api import FakeAPI, reply, text
from fake_openai import FakeOpenAI, chat
from test_webui import Browser

import harness.webui.reveal as reveal_module
from harness.app import build_app
from harness.providers import make_client
from harness.webui.server import create_server
from harness.webui.ui import WebUI

GATEWAY_KEY = "sk-" + "gateway-web-" + "FAKE" * 6  # 编造的
QWEN = {"id": "qwen3:8b", "vision": False, "tools": True, "pdf": False}


@pytest.fixture
def web(make_config, monkeypatch):
    """网页服务器 + 假 Claude + 假 Ollama。自定义接口用真正的 make_client（和正式程序一样）。"""
    started = []
    revealed: list = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(reveal_module, "reveal_in_file_manager", revealed.append)
    monkeypatch.setattr(reveal_module, "has_file_manager", lambda: True)

    def start(*replies, claude=(), models=("qwen3:8b", "llava:13b"), **server_options):
        ollama = FakeOpenAI(list(replies), models=list(models), **server_options).start()
        claude_api = FakeAPI(list(claude)).start()
        config = make_config()
        ui = WebUI()

        def client_factory(provider):
            if provider.custom:
                return make_client(provider)
            key = os.environ.get(provider.key_env) or "missing"
            if provider.kind == "anthropic":
                return anthropic.Anthropic(api_key=key, base_url=claude_api.base_url, max_retries=0)
            return openai.OpenAI(api_key=key, base_url=ollama.base_url, max_retries=0)

        app = build_app(config, ui, client_factory=client_factory)
        server, url = create_server(app, ui, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append((server, app, ollama, claude_api))
        browser = Browser(url)
        browser.wait_synced()
        return SimpleNamespace(
            app=app, web=server.web, ui=ui, browser=browser, ollama=ollama, claude=claude_api, config=config, revealed=revealed, url=ollama.base_url + "/v1"
        )

    yield start
    for server, app, ollama, claude_api in started:
        server.web.stop()
        app.shell.kill_running()
        server.shutdown()
        server.server_close()
        ollama.stop()
        claude_api.stop()


def post(s, path: str, data: dict | None = None) -> tuple[int, dict]:
    return s.browser.post(path, data)


def form(s, **changes) -> dict:
    return {"name": "Ollama", "base_url": s.url, "api_key": "", "models": [QWEN], **changes}


def endpoints(s) -> dict:
    return server_state(s)["endpoints"]


def server_state(s) -> dict:
    """服务器发出去的最后一个状态（直接看服务器的日志：回答回来时它已经在里面了，浏览器那边可能还在路上）。"""
    return [event for event in s.ui.events_after(0, timeout=0) if event["type"] == "state"][-1]


def everything_sent(s) -> str:
    """发给浏览器的全部事件，连成一段文字。"""
    return json.dumps(s.ui.events_after(0, timeout=0), ensure_ascii=False)


def test_saving_shows_the_endpoint_without_its_key(web):
    s = web()
    assert endpoints(s)["items"] == [] and endpoints(s)["problem"] is None
    status, answer = post(s, "/api/endpoint/save", {"form": form(s, name="网关", api_key=GATEWAY_KEY)})
    assert status == 200 and answer["endpoint"]["id"] == "custom-endpoint"
    (item,) = endpoints(s)["items"]
    assert (item["name"], item["base_url"], item["where"], item["has_key"]) == ("网关", s.url, "local", True)
    assert item["key_hint"] == "…" + GATEWAY_KEY[-4:] and item["models"] == [QWEN]
    assert endpoints(s)["file"].endswith("endpoints.json")
    # 状态里它也是一家提供方（换模型的面板、Agent 的编辑框都看得到），设置里"API 密钥"一节不列它
    provider = next(one for one in server_state(s)["providers"] if one["id"] == "custom-endpoint")
    assert provider["custom"] is True and provider["has_key"] is True and provider["models"] == ["qwen3:8b"]
    assert GATEWAY_KEY not in everything_sent(s) and GATEWAY_KEY not in json.dumps(answer)
    status, found = post(s, "/api/model/catalog")
    assert GATEWAY_KEY not in json.dumps(found)


def test_probing_a_draft_lists_models_and_costs_nothing(web):
    s = web(ollama={"qwen3:8b": {"capabilities": ["completion", "tools"]}, "llava:13b": {"capabilities": ["completion", "vision"]}})
    status, answer = post(s, "/api/endpoint/probe", {"form": {"name": "", "base_url": s.url, "api_key": ""}})
    assert status == 200 and answer["listed"] is True and answer["server"] == "ollama" and answer["where"] == "local"
    assert [(m["id"], m["vision"], m["tools"]) for m in answer["models"]] == [("qwen3:8b", False, True), ("llava:13b", True, False)]
    assert s.ollama.requests == []  # 没发聊天请求
    assert endpoints(s)["items"] == []  # 测试连接不保存任何东西


def test_probing_refuses_plain_http_to_the_internet(web):
    s = web()
    status, answer = post(s, "/api/endpoint/probe", {"form": {"base_url": "http://llm.example.com/v1", "api_key": GATEWAY_KEY}})
    assert status == 409 and "https://" in answer["error"]
    status, answer = post(s, "/api/endpoint/save", {"form": form(s, base_url="http://llm.example.com/v1")})
    assert status == 409 and "https://" in answer["error"]


def test_a_kept_key_never_follows_the_address_elsewhere(web):
    s = web()
    post(s, "/api/endpoint/save", {"form": form(s, api_key=GATEWAY_KEY)})
    other = FakeOpenAI([]).start()
    try:
        status, answer = post(s, "/api/endpoint/probe", {"id": "custom-ollama", "form": {"base_url": other.base_url + "/v1", "api_key": {"keep": True}}})
        assert status == 409 and "重新填" in answer["error"]
        assert other.keys == []  # 一个请求都没发过去
        # 同一个服务器：用存着的密钥去测
        status, answer = post(s, "/api/endpoint/probe", {"id": "custom-ollama", "form": {"base_url": s.url, "api_key": {"keep": True}}})
        assert status == 200 and s.ollama.keys[-1] == GATEWAY_KEY
        status, answer = post(s, "/api/endpoint/save", {"id": "custom-ollama", "form": form(s, name="Ollama 2", api_key={"keep": True})})
        assert status == 200 and s.app.endpoints.get("custom-ollama").api_key == GATEWAY_KEY
    finally:
        other.stop()


def test_switching_to_the_endpoint_and_chatting(web):
    s = web(chat("你好，我是本地的模型。"), claude=[reply(text("我是 Claude"))])
    post(s, "/api/endpoint/save", {"form": form(s)})
    status, answer = post(s, "/api/model", {"provider": "custom-ollama", "model": "qwen3:8b"})
    assert (status, answer) == (200, {"ok": True, "warning": ""})
    state = server_state(s)
    assert (state["provider"], state["model"]) == ("custom-ollama", "qwen3:8b")
    assert state["price"] == {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}
    test_webui.say(s, "你好")
    assert s.ollama.requests[0]["model"] == "qwen3:8b"
    status, found = post(s, "/api/model/catalog")
    group = next(entry for entry in found["catalog"]["groups"] if entry["id"] == "custom-ollama")
    assert group["custom"] is True and group["where"] == "local" and group["switch"] == "continue"
    assert group["models"][0]["free"]["key"] == "本地，不计费"  # 带着原文，页面换语言时能重译
    assert next(entry for entry in found["catalog"]["groups"] if entry["id"] == "anthropic")["switch"] == "leave"
    # 正在用：删不掉
    status, answer = post(s, "/api/endpoint/delete", {"id": "custom-ollama"})
    assert status == 409 and "换一个" in answer["error"]


def test_syncing_from_the_model_switcher(web):
    s = web(models=("qwen3:8b", "llava:13b", "nomic-embed-text"))
    post(s, "/api/endpoint/save", {"form": form(s, models=[QWEN | {"tools": False}])})
    assert post(s, "/api/models", {"provider": "custom-ollama"}) == (200, {"ok": True, "warning": ""})
    endpoint = s.app.endpoints.get("custom-ollama")
    assert [model.id for model in endpoint.models] == ["qwen3:8b", "llava:13b"]  # 向量模型去掉了
    assert endpoint.caps("qwen3:8b").tools is False  # 你改过的保留
    assert endpoint.caps("llava:13b").vision is True  # 新来的照名字猜


def test_a_server_without_a_model_list_says_so(web):
    s = web(listing=False)
    post(s, "/api/endpoint/save", {"form": form(s, models=[])})
    status, answer = post(s, "/api/models", {"provider": "custom-ollama"})
    assert status == 409 and "自己填模型名" in answer["error"]
    status, answer = post(s, "/api/endpoint/probe", {"form": {"base_url": s.url, "api_key": ""}})
    assert status == 200 and answer["listed"] is False and answer["models"] == []


def test_price_from_settings_goes_to_the_endpoint(web):
    s = web()
    post(s, "/api/endpoint/save", {"form": form(s)})
    post(s, "/api/model", {"provider": "custom-ollama", "model": "qwen3:8b"})
    assert post(s, "/api/price", {"model": "qwen3:8b", "price": {"input": 0.4, "output": 0.8}})[1]["ok"]
    assert s.app.endpoints.get("custom-ollama").price_of("qwen3:8b")["input"] == 0.4
    assert "qwen3:8b" not in s.app.settings.prices


def test_delete_reveal_and_bad_requests(web):
    s = web()
    post(s, "/api/endpoint/save", {"form": form(s)})
    assert post(s, "/api/endpoint/reveal")[0] == 200 and s.revealed[-1] == s.config.endpoints_file
    assert post(s, "/api/endpoint/delete", {"id": "custom-ollama"}) == (200, {"ok": True})
    assert post(s, "/api/endpoint/delete", {"id": "custom-ollama"})[0] == 409
    assert post(s, "/api/endpoint/save", {"form": "nope"})[0] == 400
    assert post(s, "/api/endpoint/delete", {})[0] == 400
    assert post(s, "/api/endpoint/hack", {})[0] == 404
    assert post(s, "/api/model", {"provider": "custom-ollama", "model": "x"})[0] == 400  # 已经不在了


def test_saving_waits_for_the_turn_to_end(web):
    """保存会换掉你正在用的模型的连接，所以和换模型一样要等这一轮结束（拿着对话的锁，模拟"这一轮还没结束"）。
    测试连接只是去问一声，不用等。"""
    s = web()
    with s.web._turn:
        status, answer = post(s, "/api/endpoint/save", {"form": form(s)})
        assert status == 409 and answer["error"]
        assert post(s, "/api/endpoint/probe", {"form": {"base_url": s.url, "api_key": ""}})[0] == 200
    assert post(s, "/api/endpoint/save", {"form": form(s)})[0] == 200
