"""网页界面的测试：真实的 HTTP 服务器 + 本地的假 API，由测试代码扮演浏览器。"""

from __future__ import annotations

import http.client
import json
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import anthropic
import pytest
from fake_api import FakeAPI, reply, text, thinking, tool_use

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
def serve(make_config):
    """启动一个连着假 API 的网页服务器，并让"浏览器"连上去。"""
    started = []

    def serve(*replies, **config_overrides):
        api = FakeAPI(list(replies)).start()
        config = make_config(**config_overrides)
        ui = WebUI()
        client = anthropic.Anthropic(api_key="test-key", base_url=api.base_url, max_retries=0)
        app = build_app(config, ui, client=client)
        server, url = create_server(app, ui, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append((api, server, app))
        return SimpleNamespace(api=api, app=app, ui=ui, url=url, config=config, browser=Browser(url))

    yield serve
    for api, server, app in started:
        server.web.stop()
        app.shell.kill_running()
        server.shutdown()
        server.server_close()
        api.stop()


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
    assert s.browser.post("/api/message", {"text": "  你好  "}) == (200, {"ok": True})
    s.browser.wait_for("turn_end")

    assert s.browser.types() == [
        "state",
        "user",
        "turn_start",
        "thinking",
        "block_end",
        "text",
        "block_end",
        "state",
        "turn_end",
    ]
    assert s.browser.of_type("user")[0]["text"] == "你好"
    assert s.browser.of_type("thinking")[0]["text"] == "想一下"
    assert s.browser.of_type("text")[0]["text"] == "你好！"
    assert s.api.requests[0]["messages"][0]["content"][1]["text"] == "你好"
    # 事件编号连续递增，浏览器靠它在断线后接着收
    assert [event["id"] for event in s.browser.events] == list(range(1, 10))


def test_state_reports_todos_usage_and_tools(serve):
    s = serve(
        reply(tool_use("toolu_1", "todo_add", {"title": "交报告", "due": "2026-11-27"}), stop_reason="tool_use"),
        reply(text("记好了。")),
    )
    s.browser.post("/api/message", {"text": "提醒我交报告"})
    s.browser.wait_for("turn_end")

    before, after = s.browser.of_type("state")
    assert before["todos"] == [] and before["usage"]["requests"] == 0
    assert after["todos"] == [{"id": 1, "title": "交报告", "due": "2026-11-27"}]
    assert after["usage"] == {"requests": 2, "input": 200, "cache_read": 80, "cache_write": 20, "output": 40}
    assert {"name": "run_command", "local": True} in after["tools"]
    assert {"name": "web_search", "local": False} in after["tools"]
    assert after["model"] == "claude-opus-5-5" and after["workspace"] == str(s.config.workspace)
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
    delta = SimpleNamespace(type="text", text="半句")
    ui.begin_turn("你好")
    ui.stream_event(delta)
    ui.request_stop()
    with pytest.raises(KeyboardInterrupt):
        ui.stream_event(delta)
    with pytest.raises(KeyboardInterrupt):
        ui.tool_call("todo_list", {})
    ui.end_turn()  # 收尾的方法不会再抛异常
    ui.begin_turn("再来")  # 新的一轮开始时旗子放下
    ui.stream_event(delta)

    types = [event["type"] for event in ui.events_after(0, timeout=0)]
    assert types == ["user", "turn_start", "text", "block_end", "turn_end", "user", "turn_start", "text"]


# ---- 新对话、刷新、出错 ---------------------------------------------------


def test_new_conversation_clears_everything(serve):
    s = serve(reply(text("你好。")))
    s.browser.post("/api/message", {"text": "你好"})
    s.browser.wait_for("turn_end")
    assert s.browser.post("/api/new") == (200, {"ok": True})
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
    assert [event["id"] for event in resumed.events] == [7, 8, 9]


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
