"""网页界面的测试：真实的 HTTP 服务器 + 本地的假 API，由测试代码扮演浏览器。"""

from __future__ import annotations

import http.client
import json
import os
import stat
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import anthropic
import openai
import pytest
from fake_api import FakeAPI, reply, text, thinking, tool_use
from fake_openai import FakeOpenAI, chat

from harness.app import build_app
from harness.webui.server import create_server
from harness.webui.ui import MAX_RESULT_CHARS, WebUI


class Browser:
    """扮演浏览器：一边在后台读事件流，一边发请求。"""

    def __init__(self, url: str, last_event_id: int | None = None):
        parts = urlsplit(url)
        self.host = parts.netloc
        self.token = parse_qs(parts.query)["token"][0]
        self.events: list[dict] = []
        self._arrived = threading.Condition()
        self._checked = 0  # wait_for 已经看过的事件数
        threading.Thread(target=self._listen, args=(last_event_id,), daemon=True).start()

    def _listen(self, last_event_id: int | None) -> None:
        headers = {} if last_event_id is None else {"Last-Event-ID": str(last_event_id)}
        connection = http.client.HTTPConnection(self.host, timeout=30)
        connection.request("GET", f"/events?token={self.token}", headers=headers)
        try:
            for line in connection.getresponse():
                if line.startswith(b"data: "):
                    with self._arrived:
                        self.events.append(json.loads(line[6:]))
                        self._arrived.notify_all()
        except OSError:
            pass  # 测试结束，服务器关了

    def wait_for(self, type: str, timeout: float = 10) -> dict:
        """等到下一个指定类型的事件出现，并返回它。"""
        deadline = time.monotonic() + timeout
        with self._arrived:
            while True:
                while self._checked < len(self.events):
                    event = self.events[self._checked]
                    self._checked += 1
                    if event["type"] == type:
                        return event
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"没有等到 {type} 事件；收到的是 {self.types()}")
                self._arrived.wait(remaining)

    def types(self) -> list[str]:
        return [event["type"] for event in self.events]

    def of_type(self, type: str) -> list[dict]:
        return [event for event in self.events if event["type"] == type]

    def post(self, path: str, data: dict | None = None) -> tuple[int, dict]:
        return request(self.host, "POST", path, data, {"X-Harness-Token": self.token})


def request(host: str, method: str, path: str, data: dict | None = None, headers: dict | None = None):
    connection = http.client.HTTPConnection(host, timeout=10)
    body = None if data is None and method == "GET" else json.dumps(data or {})
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    raw = response.read().decode()
    connection.close()
    if response.getheader("Content-Type", "").startswith("application/json"):
        return response.status, json.loads(raw)
    return response.status, {"text": raw, "headers": dict(response.getheaders())}


@pytest.fixture
def serve(make_config, monkeypatch):
    """启动一个网页服务器，后面连着两个假 API（Claude 的和 OpenAI 格式的），并让"浏览器"连上去。"""
    started = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")  # 默认只有 Claude 的 key 是设置好的

    def serve(*replies, others=(), other_models=("deepseek-flash", "deepseek-v4-pro"), **config_overrides):
        api = FakeAPI(list(replies)).start()
        other_api = FakeOpenAI(list(others), models=list(other_models)).start()
        config = make_config(**config_overrides)
        ui = WebUI()

        def client_factory(provider):
            # 和真实程序一样：每次都按环境变量里当前的 key 创建连接
            key = os.environ.get(provider.key_env) or "missing"
            if provider.kind == "anthropic":
                return anthropic.Anthropic(api_key=key, base_url=api.base_url, max_retries=0)
            return openai.OpenAI(api_key=key, base_url=other_api.base_url, max_retries=0)

        app = build_app(config, ui, client_factory=client_factory)
        server, url = create_server(app, ui, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append((api, other_api, server, app))
        return SimpleNamespace(
            api=api, other_api=other_api, app=app, ui=ui, url=url, config=config, browser=Browser(url)
        )

    yield serve
    for api, other_api, server, app in started:
        server.web.stop()
        app.shell.kill_running()
        server.shutdown()
        server.server_close()
        api.stop()
        other_api.stop()


def last_results(agent) -> list[dict]:
    return agent.messages[-1]["content"]


# ---- 安全 -----------------------------------------------------------------


def test_requests_without_the_token_are_rejected(serve):
    s = serve(reply(text("不应该被调用")))
    host, token = s.browser.host, s.browser.token

    assert request(host, "GET", "/")[0] == 403
    assert request(host, "GET", "/?token=wrong")[0] == 403
    assert request(host, "GET", "/events")[0] == 403
    assert request(host, "POST", "/api/message", {"text": "hi"})[0] == 403
    # POST 的口令必须放在请求头里；放在地址里不算
    assert request(host, "POST", f"/api/message?token={token}", {"text": "hi"})[0] == 403
    # 口令正确，但请求是从别的网站发起的
    evil = {"X-Harness-Token": token, "Origin": "http://evil.example"}
    assert request(host, "POST", "/api/message", {"text": "hi"}, evil)[0] == 403
    # 口令正确，但访问时用的不是本机地址（DNS 重绑定攻击）
    assert request(host, "GET", f"/?token={token}", headers={"Host": "evil.example"})[0] == 403

    time.sleep(0.2)
    assert s.api.requests == []
    assert s.app.agent.messages == []


def test_page_is_served_with_a_strict_content_policy(serve):
    s = serve()
    status, page = request(s.browser.host, "GET", f"/?token={s.browser.token}")

    assert status == 200
    assert "Harness 个人助手" in page["text"]
    assert "__NONCE__" not in page["text"]
    policy = page["headers"]["Content-Security-Policy"]
    nonce = policy.split("script-src 'nonce-")[1].split("'")[0]
    assert f'<script nonce="{nonce}">' in page["text"]
    assert "default-src 'none'" in policy and "frame-ancestors 'none'" in policy
    assert "unsafe-inline" not in policy
    # 页面不引用任何外部资源
    assert 'src="http' not in page["text"] and 'href="http' not in page["text"]


def test_bad_requests_are_rejected(serve):
    s = serve()
    assert s.browser.post("/api/message", {"text": "   "})[0] == 400
    assert s.browser.post("/api/message", {"text": 5})[0] == 400
    assert s.browser.post("/api/confirm", {"id": "1", "allow": True})[0] == 400
    assert s.browser.post("/api/nothing")[0] == 404
    status, _ = request(
        s.browser.host, "POST", "/api/message", headers={"X-Harness-Token": s.browser.token, "Content-Length": "x"}
    )
    assert status == 413


# ---- 对话 -----------------------------------------------------------------


def test_message_round_trip(serve):
    s = serve(reply(thinking("想一下"), text("你好！")))
    assert s.browser.post("/api/message", {"text": "  你好  "})[0] == 200
    s.browser.wait_for("turn_end")

    assert s.browser.types() == [
        "state",
        "user",
        "turn_start",
        "thinking",
        "block_end",
        "text",
        "block_end",
        "state",  # 模型一答完，用量和费用就更新一次
        "state",
        "turn_end",
    ]
    assert s.browser.of_type("user")[0]["text"] == "你好"
    assert s.browser.of_type("thinking")[0]["text"] == "想一下"
    assert s.browser.of_type("text")[0]["text"] == "你好！"
    assert s.api.requests[0]["messages"][0]["content"][1]["text"] == "你好"
    # 事件编号连续递增，浏览器靠它在断线后接着收
    assert [event["id"] for event in s.browser.events] == list(range(1, 11))


def test_state_reports_todos_usage_and_tools(serve):
    s = serve(
        reply(tool_use("toolu_1", "todo_add", {"title": "交报告", "due": "2026-11-27"}), stop_reason="tool_use"),
        reply(text("记好了。")),
    )
    s.browser.post("/api/message", {"text": "提醒我交报告"})
    s.browser.wait_for("turn_end")

    states = s.browser.of_type("state")
    before, during, after = states[0], states[1], states[-1]
    assert before["todos"] == [] and before["usage"]["requests"] == 0 and before["cost"] == 0
    assert during["usage"]["requests"] == 1  # 第一次调用模型之后、这一轮还没结束时，用量就已经更新了
    assert after["todos"] == [{"id": 1, "title": "交报告", "due": "2026-11-27"}]
    assert after["usage"] == {
        "requests": 2, "input": 200, "cache_read": 80, "cache_write": 20, "output": 40, "searches": 0, "context": 170,
    }
    # 费用按 claude-opus-5-5 的价格估算：输入 4、命中缓存 0.2、写入缓存 5、输出 20（美元 / 百万 token）
    assert after["cost"] == round((200 * 4 + 80 * 0.2 + 20 * 5 + 40 * 20) / 1_000_000, 4)
    assert after["unpriced"] == []
    assert {"name": "run_command", "local": True} in after["tools"]
    assert {"name": "web_search", "local": False} in after["tools"]
    assert (after["provider"], after["model"]) == ("anthropic", "claude-opus-5-5")
    assert after["workspace"] == str(s.config.workspace)
    # 待办不需要确认，所以没有确认事件；工具调用和结果成对出现
    assert s.browser.of_type("confirm") == []
    (call,) = s.browser.of_type("tool_call")
    assert call["name"] == "todo_add" and call["input"] == {"title": "交报告", "due": "2026-11-27"}
    assert s.browser.of_type("tool_result")[0]["is_error"] is False


def test_allowing_a_command_runs_it(serve):
    s = serve(
        reply(tool_use("toolu_1", "run_command", {"command": "touch made.txt"}), stop_reason="tool_use"),
        reply(text("建好了。")),
    )
    s.browser.post("/api/message", {"text": "建个文件"})
    confirm = s.browser.wait_for("confirm")

    assert "touch made.txt" in confirm["description"]
    assert not (s.config.workspace / "made.txt").exists()  # 用户点头之前什么都不做
    assert s.browser.post("/api/confirm", {"id": confirm["id"], "allow": True}) == (200, {"ok": True})
    s.browser.wait_for("turn_end")

    assert (s.config.workspace / "made.txt").exists()
    done = s.browser.of_type("confirm_done")[0]
    assert (done["confirm"], done["allowed"], done["stopped"]) == (confirm["id"], True, False)
    assert s.browser.of_type("tool_result")[0]["is_error"] is False


def test_denying_a_command_passes_the_reason_to_the_model(serve):
    s = serve(
        reply(tool_use("toolu_1", "run_command", {"command": "touch made.txt"}), stop_reason="tool_use"),
        reply(text("好的。")),
    )
    s.browser.post("/api/message", {"text": "建个文件"})
    confirm = s.browser.wait_for("confirm")
    s.browser.post("/api/confirm", {"id": confirm["id"], "allow": False, "feedback": " 先别建 "})
    s.browser.wait_for("turn_end")

    assert not (s.config.workspace / "made.txt").exists()
    result = s.api.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and result["content"] == "用户拒绝了这个操作。用户说：先别建"
    assert s.browser.of_type("confirm_done")[0]["allowed"] is False


def test_a_confirmation_can_only_be_answered_once(serve):
    s = serve(
        reply(tool_use("toolu_1", "run_command", {"command": "true"}), stop_reason="tool_use"),
        reply(text("好。")),
    )
    s.browser.post("/api/message", {"text": "跑一下"})
    confirm = s.browser.wait_for("confirm")

    assert s.browser.post("/api/confirm", {"id": confirm["id"] + 99, "allow": True})[0] == 409  # 编号不对
    # 确认期间不能发新消息，也不能开新对话
    assert s.browser.post("/api/message", {"text": "另一句"})[0] == 409
    assert s.browser.post("/api/new")[0] == 409
    assert s.browser.post("/api/confirm", {"id": confirm["id"], "allow": True})[0] == 200
    s.browser.wait_for("turn_end")
    assert s.browser.post("/api/confirm", {"id": confirm["id"], "allow": True})[0] == 409  # 已经答复过
    assert len(s.api.requests) == 2


# ---- 停止 -----------------------------------------------------------------


def test_stop_while_waiting_for_confirmation(serve):
    s = serve(
        reply(
            tool_use("toolu_1", "run_command", {"command": "touch a.txt"}),
            tool_use("toolu_2", "run_command", {"command": "touch b.txt"}),
            stop_reason="tool_use",
        ),
        reply(text("好的。")),
    )
    s.browser.post("/api/message", {"text": "建两个文件"})
    s.browser.wait_for("confirm")
    assert s.browser.post("/api/stop") == (200, {"ok": True})
    s.browser.wait_for("turn_end")

    assert list(s.config.workspace.iterdir()) == []
    assert s.browser.of_type("confirm_done")[0]["stopped"] is True
    assert s.browser.of_type("notice")[-1]["text"] == "已中断。"
    # 两个工具调用都补上了结果，历史仍然合法
    assert [result["tool_use_id"] for result in last_results(s.app.agent)] == ["toolu_1", "toolu_2"]
    assert all(result["is_error"] for result in last_results(s.app.agent))
    # 停止之后可以接着聊
    assert s.browser.post("/api/message", {"text": "算了"})[0] == 200
    s.browser.wait_for("turn_end")
    assert [m["role"] for m in s.api.requests[1]["messages"]] == ["user", "assistant", "user", "user"]


def test_stop_kills_a_running_command(serve):
    s = serve(reply(tool_use("toolu_1", "run_command", {"command": "sleep 30; touch late.txt"}), stop_reason="tool_use"))
    s.browser.post("/api/message", {"text": "等一会儿"})
    confirm = s.browser.wait_for("confirm")
    s.browser.post("/api/confirm", {"id": confirm["id"], "allow": True})
    s.browser.wait_for("confirm_done")
    while s.app.shell._running is None:  # 等命令真正跑起来
        time.sleep(0.02)

    started = time.monotonic()
    s.browser.post("/api/stop")
    s.browser.wait_for("turn_end")

    assert time.monotonic() - started < 5
    assert not (s.config.workspace / "late.txt").exists()
    result = s.browser.of_type("tool_result")[0]
    assert result["is_error"] is True and "被用户停止" in result["text"]
    assert len(s.api.requests) == 1  # 停止之后没有再去调用模型
    assert last_results(s.app.agent)[0]["tool_use_id"] == "toolu_1"


def test_stop_flag_interrupts_at_the_next_ui_call():
    ui = WebUI()
    ui.begin_turn("你好")
    ui.text("半句")
    ui.request_stop()
    with pytest.raises(KeyboardInterrupt):
        ui.text("后半句")
    with pytest.raises(KeyboardInterrupt):
        ui.thinking("想")
    with pytest.raises(KeyboardInterrupt):
        ui.tool_call("todo_list", {})
    ui.end_turn()  # 收尾的方法不会再抛异常
    ui.begin_turn("再来")  # 新的一轮开始时旗子放下
    ui.text("好")

    types = [event["type"] for event in ui.events_after(0, timeout=0)]
    assert types == ["user", "turn_start", "text", "block_end", "turn_end", "user", "turn_start", "text"]


# ---- 新对话、刷新、出错 ---------------------------------------------------


def test_new_conversation_clears_everything(serve):
    s = serve(reply(text("你好。")))
    s.browser.post("/api/message", {"text": "你好"})
    s.browser.wait_for("turn_end")
    assert s.browser.post("/api/new")[0] == 200
    s.browser.wait_for("reset")
    s.browser.wait_for("state")

    assert s.app.agent.messages == []
    # 这时才打开页面的浏览器，看不到上一段对话
    late = Browser(s.url)
    late.wait_for("state")
    assert late.types() == ["reset", "state"]


def test_reloading_the_page_replays_the_conversation(serve):
    s = serve(reply(thinking("想"), text("答")))
    s.browser.post("/api/message", {"text": "问"})
    s.browser.wait_for("turn_end")

    reloaded = Browser(s.url)  # 刷新页面：从头重放
    reloaded.wait_for("turn_end")
    assert reloaded.events == s.browser.events

    resumed = Browser(s.url, last_event_id=6)  # 断线重连：只补发没收到的
    resumed.wait_for("turn_end")
    assert [event["id"] for event in resumed.events] == [7, 8, 9, 10]


def test_api_errors_are_shown_and_do_not_block_the_next_turn(serve):
    s = serve()  # 没有预设回复：假 API 返回 500
    s.browser.post("/api/message", {"text": "你好"})
    s.browser.wait_for("turn_end")

    notice = s.browser.of_type("notice")[0]
    assert notice["level"] == "error" and "500" in notice["text"]
    assert s.app.agent.messages == []
    assert s.browser.post("/api/message", {"text": "再试一次"})[0] == 200
    s.browser.wait_for("turn_end")


def test_long_tool_results_are_shortened_for_display_only(serve):
    s = serve(
        reply(tool_use("toolu_1", "str_replace_based_edit_tool", {"command": "view", "path": "big.txt"}),
              stop_reason="tool_use"),
        reply(text("看完了。")),
    )
    (s.config.workspace / "big.txt").write_text("很长的一行\n" * 5000, encoding="utf-8")
    s.browser.post("/api/message", {"text": "看看 big.txt"})
    s.browser.wait_for("turn_end")

    shown = s.browser.of_type("tool_result")[0]["text"]
    sent_to_model = s.api.requests[1]["messages"][-1]["content"][0]["content"]
    assert len(shown) < MAX_RESULT_CHARS + 100 and "这里只显示开头" in shown
    assert len(sent_to_model) > 30_000  # 模型拿到的是完整内容


# ---- 设置：密钥、模型、价格 -----------------------------------------------


def latest_state(browser: Browser) -> dict:
    deadline = time.monotonic() + 5
    while not browser.of_type("state"):  # 刚连上时，第一个状态事件可能还在路上
        assert time.monotonic() < deadline, "没有收到状态事件"
        time.sleep(0.01)
    return browser.of_type("state")[-1]


def provider_state(browser: Browser, provider_id: str) -> dict:
    return next(p for p in latest_state(browser)["providers"] if p["id"] == provider_id)


def test_saving_a_key_stores_it_syncs_models_and_never_shows_it(serve):
    s = serve()
    s.config.env_file.write_text("# 我的笔记\nHARNESS_EFFORT=high\nDEEPSEEK_API_KEY=old-key-0000\n", encoding="utf-8")
    assert provider_state(s.browser, "deepseek")["has_key"] is False

    status, body = s.browser.post("/api/key", {"provider": "deepseek", "key": "  sk-test-12345678  "})
    assert (status, body) == (200, {"ok": True, "warning": ""})
    s.browser.wait_for("state")
    s.browser.wait_for("state")

    # 写进了 .env：只替换这一行，别的内容原样保留；文件只有自己能读
    assert s.config.env_file.read_text(encoding="utf-8") == (
        "# 我的笔记\nHARNESS_EFFORT=high\nDEEPSEEK_API_KEY=sk-test-12345678\n"
    )
    assert stat.S_IMODE(s.config.env_file.stat().st_mode) == 0o600
    # 模型列表自动同步了，用的是刚保存的 key
    deepseek = provider_state(s.browser, "deepseek")
    assert deepseek["has_key"] is True and deepseek["key_hint"] == "…5678"
    assert deepseek["models"] == ["deepseek-flash", "deepseek-v4-pro"]
    assert s.other_api.keys == ["sk-test-12345678"]
    # 完整的 key 不出现在发给浏览器的任何事件里
    assert "sk-test-12345678" not in json.dumps(s.browser.events)


def test_a_wrong_key_is_saved_but_reported(serve):
    s = serve()
    status, body = s.browser.post("/api/key", {"provider": "deepseek", "key": "bad-key-123"})

    assert status == 200 and "同步模型列表失败" in body["warning"] and "API key 无效" in body["warning"]
    s.browser.wait_for("state")
    s.browser.wait_for("state")
    assert provider_state(s.browser, "deepseek")["has_key"] is True
    assert provider_state(s.browser, "deepseek")["models"] == []


def test_malformed_keys_are_rejected(serve):
    s = serve()
    for bad in ("有 空格 的 key", "short", "line1\nline2-abcdefg", 12345):
        assert s.browser.post("/api/key", {"provider": "deepseek", "key": bad})[0] == 400
    assert s.browser.post("/api/key", {"provider": "nobody", "key": "sk-test-12345678"})[0] == 400
    assert not s.config.env_file.exists()


def test_deleting_a_key(serve):
    s = serve()
    s.browser.post("/api/key", {"provider": "deepseek", "key": "sk-test-12345678"})
    assert s.browser.post("/api/key", {"provider": "deepseek", "key": ""})[0] == 200

    assert "DEEPSEEK_API_KEY" not in s.config.env_file.read_text(encoding="utf-8")
    assert not os.environ.get("DEEPSEEK_API_KEY")
    time.sleep(0.2)
    deepseek = provider_state(s.browser, "deepseek")
    assert deepseek["has_key"] is False and deepseek["models"] == []


def test_syncing_claude_models_keeps_only_supported_ones(serve):
    s = serve()
    assert provider_state(s.browser, "anthropic")["models"] == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2"]
    assert s.browser.post("/api/models", {"provider": "anthropic"})[0] == 200
    s.browser.wait_for("state")
    s.browser.wait_for("state")
    # 假服务器上有 claude-opus-5-5、claude-sonnet-5-5 和一个老模型；老模型被过滤掉了
    assert provider_state(s.browser, "anthropic")["models"] == ["claude-opus-5-5", "claude-sonnet-5-5"]

    status, body = s.browser.post("/api/models", {"provider": "gemini"})  # 没有 key 的不能同步
    assert status == 409 and "还没有设置" in body["error"]


def test_switching_to_another_provider_starts_a_new_conversation(serve):
    s = serve(reply(text("我是 Claude。")), others=[chat("我是 DeepSeek。")])
    s.browser.post("/api/message", {"text": "你是谁"})
    s.browser.wait_for("turn_end")

    status, body = s.browser.post("/api/model", {"provider": "deepseek", "model": "deepseek-flash"})
    assert status == 409 and "还没有设置 DeepSeek 的 API key" in body["error"]  # 没有 key 不能换过去

    s.browser.post("/api/key", {"provider": "deepseek", "key": "sk-test-12345678"})
    assert s.browser.post("/api/model", {"provider": "deepseek", "model": "deepseek-flash"})[0] == 200
    s.browser.wait_for("reset")
    assert "开始新对话" in s.browser.wait_for("notice")["text"]

    s.browser.post("/api/message", {"text": "你是谁"})
    s.browser.wait_for("turn_end")
    (request,) = s.other_api.requests
    assert request["model"] == "deepseek-flash"
    assert [message["role"] for message in request["messages"]] == ["system", "user"]  # 之前和 Claude 的对话没有带过来
    state = latest_state(s.browser)
    assert (state["provider"], state["model"]) == ("deepseek", "deepseek-flash")
    assert [tool["name"] for tool in state["tools"]] == [
        "todo_add", "todo_list", "todo_update", "todo_delete", "memory", "str_replace_based_edit_tool", "run_command",
    ]  # 没有联网搜索
    # Claude 的那一轮有内置价格；DeepSeek 没有价格，所以被列为"没有计入"
    assert state["unpriced"] == ["deepseek-flash"] and state["price"] is None
    claude_cost = (100 * 4 + 40 * 0.2 + 10 * 5 + 20 * 20) / 1_000_000
    assert state["cost"] == round(claude_cost, 4)

    # 填上价格之后就计入了：这一轮是 100 个输入 token、20 个输出 token
    assert s.browser.post("/api/price", {"model": "deepseek-flash", "price": {"input": 0.3, "output": 1.2}})[0] == 200
    state = s.browser.wait_for("state")
    assert state["unpriced"] == [] and state["price"]["input"] == 0.3
    assert state["cost"] == round(claude_cost + (100 * 0.3 + 20 * 1.2) / 1_000_000, 4)
    assert s.browser.post("/api/price", {"model": "deepseek-flash", "price": {"input": -1, "output": 1}})[0] == 400
    assert s.browser.post("/api/price", {"model": "deepseek-flash", "price": {"input": 1}})[0] == 400


def test_switching_within_a_provider_keeps_the_conversation(serve):
    s = serve(reply(text("你好。")), reply(text("还记得。")))
    s.browser.post("/api/message", {"text": "记住暗号是 42"})
    s.browser.wait_for("turn_end")

    assert s.browser.post("/api/model", {"provider": "anthropic", "model": "claude-sonnet-5-5"})[0] == 200
    assert "对话继续" in s.browser.wait_for("notice")["text"]
    assert "reset" not in s.browser.types()

    s.browser.post("/api/message", {"text": "暗号是多少"})
    s.browser.wait_for("turn_end")
    second = s.api.requests[1]
    assert second["model"] == "claude-sonnet-5-5"
    assert [message["role"] for message in second["messages"]] == ["user", "assistant", "user"]
    # 选择保存了下来，下次启动还是它
    assert json.loads(s.config.settings_file.read_text(encoding="utf-8"))["model"] == "claude-sonnet-5-5"


def test_messages_need_a_key_first(serve, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    s = serve(reply(text("不应该被调用")))
    status, body = s.browser.post("/api/message", {"text": "你好"})

    assert status == 409 and "还没有设置 Claude (Anthropic) 的 API key" in body["error"]
    assert s.api.requests == [] and s.app.agent.messages == []


def test_settings_cannot_change_during_a_turn(serve):
    s = serve(
        reply(tool_use("toolu_1", "run_command", {"command": "true"}), stop_reason="tool_use"),
        reply(text("好。")),
    )
    s.browser.post("/api/message", {"text": "跑一下"})
    confirm = s.browser.wait_for("confirm")

    assert s.browser.post("/api/model", {"provider": "anthropic", "model": "claude-sonnet-5-5"})[0] == 409
    assert s.browser.post("/api/key", {"provider": "deepseek", "key": "sk-test-12345678"})[0] == 409
    assert not s.config.env_file.exists()
    s.browser.post("/api/confirm", {"id": confirm["id"], "allow": True})
    s.browser.wait_for("turn_end")
    assert s.api.requests[1]["model"] == "claude-opus-5-5"
