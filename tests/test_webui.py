"""网页界面的测试：真实的 HTTP 服务器 + 本地的假 API，由测试代码扮演浏览器。"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import stat
import subprocess
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import anthropic
import openai
import pytest
from fake_api import FakeAPI, reply, text, thinking, tool_use
from fake_openai import FakeOpenAI, chat
from sample_files import (
    JPEG_BYTES,
    PDF,
    PDF_BYTES,
    PNG,
    PNG_BYTES,
    THUMB,
    b64,
    document,
    image,
    pdf,
    text_file,
    xlsx,
)

import harness.webui.server as server_module
from harness import memories
from harness.app import build_app
from harness.attachments import (
    MAX_IMAGE_BYTES,
    MAX_PDF_BYTES,
    MAX_PREVIEW_CHARS,
    MAX_TEXT_CHARS,
    MAX_THUMB_CHARS,
    from_saved,
)
from harness.conversations import ConversationStore
from harness.documents import NOTE as NOTE_OFFICE
from harness.search import MAX_QUERY_CHARS
from harness.webui.server import (
    LARGE_CONTEXT_TOKENS,
    MAX_BODY_BYTES,
    MAX_MESSAGE_BODY_BYTES,
    create_server,
)
from harness.webui.ui import MAX_RESULT_CHARS, WebUI, latest_state_only


class Browser:
    """扮演浏览器：一边在后台读事件流，一边发请求。

    读事件流的方式和浏览器里的 EventSource 一样：每条消息由几行"字段: 值"组成，空行表示一条消息结束；
    "id:" 行给出这条消息的编号，浏览器记住最后一个编号，断线重连时用 Last-Event-ID 请求头告诉服务器。
    所以这里也记着 last_event_id，并且检查服务器有没有守约定（见 problems）。
    """

    def __init__(self, url: str, last_event_id: str | int | None = None):
        parts = urlsplit(url)
        self.host = parts.netloc
        self.token = parse_qs(parts.query)["token"][0]
        self.events: list[dict] = []
        self.synced_after: int | None = None  # 收到 synced 标记时，已经收到了多少个事件
        self.last_event_id = None if last_event_id is None else str(last_event_id)  # 和 EventSource 的 lastEventId 一样
        self.problems: list[str] = []  # 事件流不守约定的地方。后台线程里断言失败不会让测试失败，所以先记下来
        self._arrived = threading.Condition()
        self._checked = 0  # wait_for 已经看过的事件数
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self) -> None:
        headers = {} if self.last_event_id is None else {"Last-Event-ID": self.last_event_id}
        connection = http.client.HTTPConnection(self.host, timeout=30)
        connection.request("GET", f"/events?token={self.token}", headers=headers)
        try:
            response = connection.getresponse()
            kind = response.getheader("Content-Type", "")
            if not kind.startswith("text/event-stream"):
                self.problems.append(f"事件流的 Content-Type 是 {kind!r}，浏览器的 EventSource 会直接拒收")
            fields: dict[str, str] = {}
            for raw in response:
                line = raw.decode("utf-8").rstrip("\r\n")
                if line.startswith(":"):
                    continue  # 注释行（保持连接用的 keepalive）
                if line:
                    name, _, value = line.partition(":")
                    fields[name] = value.removeprefix(" ")
                    continue
                if "data" in fields:  # 空行：一条消息结束，交给 _dispatch
                    self._dispatch(fields)
                fields = {}
        except OSError:
            pass  # 测试结束，服务器关了

    def _dispatch(self, fields: dict[str, str]) -> None:
        with self._arrived:
            if "id" in fields:
                self.last_event_id = fields["id"]
            if fields.get("event") == "synced":
                self.synced_after = len(self.events)
                if "id" in fields:
                    # 标记要是带着编号，浏览器会把它当成"最后收到的那一个"，下次重连时报给服务器一个日志里没有的编号
                    self.problems.append(f"synced 标记带着编号 {fields['id']}")
            elif "event" not in fields:
                event = json.loads(fields["data"])
                if fields.get("id") != str(event["id"]):
                    self.problems.append(f"事件 {event['id']} 的 id 行是 {fields.get('id')!r}：断线重连时浏览器报不出正确的位置")
                self.events.append(event)
            self._arrived.notify_all()

    def _check(self) -> None:
        assert not self.problems, "事件流不符合约定：" + "；".join(self.problems)

    def wait_synced(self, timeout: float = 10) -> int:
        """等到服务器说"已有的事件补发完了"，返回在那之前收到了多少个事件。"""
        deadline = time.monotonic() + timeout
        with self._arrived:
            while self.synced_after is None:
                self._check()
                remaining = deadline - time.monotonic()
                assert remaining > 0, f"没有等到 synced 标记；收到的是 {self.types()}"
                self._arrived.wait(remaining)
            self._check()
            return self.synced_after

    def wait_for(self, type: str, timeout: float = 10) -> dict:
        """等到下一个指定类型的事件出现，并返回它。"""
        deadline = time.monotonic() + timeout
        with self._arrived:
            while True:
                self._check()
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
        # 和页面里的 fetch 发的一样：浏览器给同源的 POST 一律带上 Origin
        headers = {"X-Harness-Token": self.token, "Origin": f"http://{self.host}", "Content-Type": "application/json"}
        return request(self.host, "POST", path, data, headers)


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
    revealed: list = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")  # 默认只有 Claude 的 key 是设置好的
    # 测试里绝不真的去打开访达：把"在文件管理器里显示"换成只记下路径
    monkeypatch.setattr(server_module, "reveal_in_file_manager", revealed.append)

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
        browser = Browser(url)
        browser.wait_synced()  # 等"浏览器"连上、把已有的事件收完再开始，之后发生的每个事件它都会原样收到
        return SimpleNamespace(
            api=api, other_api=other_api, app=app, ui=ui, url=url, config=config, browser=browser, revealed=revealed
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
    # 冒充本机、但端口或协议不对的来源，也一样拒绝
    port = host.split(":")[1]
    for origin in (f"http://127.0.0.1:{int(port) + 1}", f"https://127.0.0.1:{port}", "http://127.0.0.1", "null"):
        assert request(host, "POST", "/api/stop", {}, {"X-Harness-Token": token, "Origin": origin})[0] == 403, origin

    time.sleep(0.2)
    assert s.api.requests == []
    assert s.app.agent.messages == []


def test_the_page_itself_is_allowed_in(serve):
    # 真实页面发的每一个 POST 都带着 Origin（浏览器自动加的）。上面拒绝的都是别处来的，这里确认自己的能进来：
    # 用 127.0.0.1 打开的页面，和用 localhost 打开的页面
    s = serve(reply(text("不应该被调用")))
    host, token = s.browser.host, s.browser.token
    port = host.split(":")[1]
    for name in ("127.0.0.1", "localhost"):
        headers = {"X-Harness-Token": token, "Host": f"{name}:{port}", "Origin": f"http://{name}:{port}"}
        assert request(host, "POST", "/api/stop", {}, headers) == (200, {"ok": True}), name
        assert request(host, "GET", f"/?token={token}", headers={"Host": f"{name}:{port}"})[0] == 200
    # 两边对不上（用 localhost 打开、请求却说来自 127.0.0.1）也算同一台电脑上的同一个程序，放行
    headers = {"X-Harness-Token": token, "Host": f"localhost:{port}", "Origin": f"http://127.0.0.1:{port}"}
    assert request(host, "POST", "/api/stop", {}, headers) == (200, {"ok": True})


def test_page_is_served_with_a_strict_content_policy(serve):
    s = serve()
    status, page = request(s.browser.host, "GET", f"/?token={s.browser.token}")

    assert status == 200
    assert "<title>智狐</title>" in page["text"]
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
        "state",  # 这一轮刚开始：新会话带着标题出现在会话列表里
        "thinking",
        "block_end",
        "text",
        "block_end",
        "state",  # 模型一答完，用量和费用就更新一次
        "state",  # 会话存盘之后再刷新一次
        "turn_end",  # 最后才是"这一轮结束了"：收到它时锁已经要放开，可以马上发下一个请求
    ]
    assert s.browser.of_type("user")[0]["text"] == "你好"
    assert s.browser.of_type("thinking")[0]["text"] == "想一下"
    assert s.browser.of_type("text")[0]["text"] == "你好！"
    assert s.api.requests[0]["messages"][0]["content"][1]["text"] == "你好"
    # 事件编号连续递增，浏览器靠它在断线后接着收
    assert [event["id"] for event in s.browser.events] == list(range(1, 12))


def test_state_reports_todos_usage_and_tools(serve):
    s = serve(
        reply(tool_use("toolu_1", "todo_add", {"title": "交报告", "due": "2026-11-27"}), stop_reason="tool_use"),
        reply(text("记好了。")),
    )
    s.browser.post("/api/message", {"text": "提醒我交报告"})
    s.browser.wait_for("turn_end")

    # 状态一共发了五次：启动时、这一轮开始时、两次调用模型之后各一次、存盘之后
    before, started, during, _, after = s.browser.of_type("state")
    assert before["todos"] == [] and before["usage"]["requests"] == 0 and before["cost"] == 0
    assert started["usage"]["requests"] == 0 and started["title"] == "提醒我交报告"
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
    # 重放出来的和当初实时收到的一样，只有"状态"例外：它是快照，旧的已经被新的取代，只补发最新的一份
    latest = s.browser.of_type("state")[-1]
    assert reloaded.events == [event for event in s.browser.events if event["type"] != "state" or event is latest]
    assert kinds(reloaded.events) == ["user", "turn_start", "thinking", "block_end", "text", "block_end", "state", "turn_end"]

    # 每条事件的 id 行和它的编号一致（_dispatch 里检查），浏览器记住的是最后一条的编号
    assert s.browser.last_event_id == str(s.browser.events[-1]["id"]) == reloaded.last_event_id

    resumed = Browser(s.url, last_event_id=6)  # 断线重连：只补发没收到的（其中过时的状态同样略去）
    resumed.wait_for("turn_end")
    assert [event["id"] for event in resumed.events] == [7, 8, 10, 11]
    assert kinds(resumed.events) == ["text", "block_end", "state", "turn_end"]


def test_the_stream_marks_where_the_backlog_ends(serve):
    s = serve(reply(text("一")), reply(text("二")))
    assert s.browser.wait_synced() == 1  # 刚启动：日志里只有最初的那个状态
    assert s.browser.post("/api/message", {"text": "第一句"})[0] == 200
    s.browser.wait_for("turn_end")

    late = Browser(s.url)  # 这时才打开页面：先收到补发的旧内容，然后是 synced 标记
    backlog = late.wait_synced()
    assert kinds(late.events[:backlog]) == ["user", "turn_start", "text", "block_end", "state", "turn_end"]
    assert backlog == len(late.events)  # 标记之后还没有任何新事件

    # 标记之后收到的，都是此刻正在发生的事件
    assert s.browser.post("/api/message", {"text": "第二句"})[0] == 200
    late.wait_for("turn_end")  # 补发的那一轮的结尾
    late.wait_for("turn_end")  # 刚发生的这一轮的结尾
    assert kinds(late.events[backlog:])[:2] == ["user", "turn_start"]
    assert late.events[backlog]["text"] == "第二句"
    # 标记本身不是日志里的事件：它不带编号（_dispatch 里检查），所以浏览器记住的仍是最后一个真实事件的编号
    assert late.last_event_id == str(late.events[-1]["id"])

    # 断线重连、没有落下任何事件：用浏览器自己记住的编号去连，什么都不会重发，标记照样会发
    resumed = Browser(s.url, last_event_id=late.last_event_id)
    assert resumed.wait_synced() == 0 and resumed.events == []


def test_the_log_does_not_pile_up_old_states():
    ui = WebUI()
    ui.publish("state", n=1)
    for turn in range(3):  # 三轮对话，每轮中间和结尾各有一次状态更新
        ui.begin_turn(f"第 {turn} 句")
        ui.publish("state", n=10 * turn + 2)
        ui.text("答")
        ui.publish("state", n=10 * turn + 3)
        ui.end_turn()

    log = ui.events_after(0, timeout=0)
    # 每轮开始时，之前的状态只留最后一个：日志里是"上一轮结尾的那个 + 这一轮的两个"，不会越积越多
    assert [event["n"] for event in log if event["type"] == "state"] == [13, 22, 23]
    # 对话本身的内容一条不少
    assert [event["text"] for event in log if event["type"] == "user"] == ["第 0 句", "第 1 句", "第 2 句"]
    assert [event["type"] for event in log].count("turn_end") == 3
    # 编号不再连续，但"接着第几号往后发"仍然算得对：包括指向已经被清掉的编号、和日志之外的编号
    ids = [event["id"] for event in log]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    for cursor in range(ids[-1] + 1):
        assert [event["id"] for event in ui.events_after(cursor, timeout=0)] == [i for i in ids if i > cursor]
    assert [event["id"] for event in ui.events_after(ids[-1] + 50, timeout=0)] == ids  # 比日志还新：对不上了，全部重发


def test_only_the_latest_state_is_worth_resending():
    ui = WebUI()
    ui.publish("state", n=1)
    ui.publish("user", text="问")
    ui.publish("state", n=2)
    ui.publish("text", text="答")
    ui.publish("state", n=3)
    ui.publish("turn_end")

    def brief(events: list[dict]) -> list[tuple]:
        return [(event["id"], event["type"], event.get("n")) for event in events]

    # 日志本身一个不少
    everything = ui.events_after(0, timeout=0)
    assert [event["id"] for event in everything] == [1, 2, 3, 4, 5, 6]
    # 补发时：旧的状态已经被最新的取代，不用再发；其余的事件一个不少，顺序不变
    assert brief(latest_state_only(everything)) == [(2, "user", None), (4, "text", None), (5, "state", 3), (6, "turn_end", None)]
    assert brief(latest_state_only(ui.events_after(4, timeout=0))) == [(5, "state", 3), (6, "turn_end", None)]
    # 没有状态、或者什么都没有，也不出错
    assert brief(latest_state_only(everything[1:2])) == [(2, "user", None)]
    assert latest_state_only([]) == []


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

    assert status == 200 and "同步模型列表失败" in body["warning"] and "API 密钥无效" in body["warning"]
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
    assert status == 409 and "还没有设置 DeepSeek 的 API 密钥" in body["error"]  # 没有 key 不能换过去

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
    # Claude 和 DeepSeek 都有内置价格。DeepSeek 分高峰和闲时（闲时半价），两个价格都发给页面
    assert state["unpriced"] == []
    assert state["price"] == {"input": 0.3, "output": 1.2, "cache_read": 0.006, "cache_write": 0.3}
    assert state["price_offpeak"] == {"input": 0.15, "output": 0.6, "cache_read": 0.003, "cache_write": 0.15}
    claude_cost = (100 * 4 + 40 * 0.2 + 10 * 5 + 20 * 20) / 1_000_000
    # 这一轮是 100 个输入 token、20 个输出 token。落在高峰还是闲时，要看测试机器的时区，两种都对
    peak, offpeak = (100 * 0.3 + 20 * 1.2) / 1_000_000, (100 * 0.15 + 20 * 0.6) / 1_000_000
    assert state["cost"] in (round(claude_cost + peak, 4), round(claude_cost + offpeak, 4))

    # 自己填了价格，全天都按它算（不再分时段）
    assert s.browser.post("/api/price", {"model": "deepseek-flash", "price": {"input": 3, "output": 12}})[0] == 200
    state = s.browser.wait_for("state")
    assert state["unpriced"] == [] and state["price"]["input"] == 3 and state["price_offpeak"] is None
    assert state["cost"] == round(claude_cost + (100 * 3 + 20 * 12) / 1_000_000, 4)
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

    assert status == 409 and "还没有设置 Claude (Anthropic) 的 API 密钥" in body["error"]
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


# ---- 会话：保存、列表、打开、改名、删除 -----------------------------------

OK = (200, {"ok": True, "warning": ""})
MISSING = "20200101-000000-abcdef"  # 格式正确、但并不存在的会话编号


def say(s, text: str) -> dict:
    """发一条消息，等这一轮结束，返回结束时的状态。"""
    assert s.browser.post("/api/message", {"text": text})[0] == 200
    s.browser.wait_for("turn_end")
    return latest_state(s.browser)


def act(s, path: str, data: dict | None = None) -> dict:
    """做一个马上就能完成的操作（新对话、改名、删除、换模型），返回它之后的状态。"""
    last_id = s.ui.events_after(0, timeout=0)[-1]["id"]  # 操作之前的最后一个事件
    assert s.browser.post(path, data) == OK
    while True:
        state = s.browser.wait_for("state")
        if state["id"] > last_id:
            return state


def open_conversation(s, conversation_id: str) -> tuple[list[dict], dict]:
    """打开一段保存过的会话。返回（页面清空之后重放出来的事件，之后的状态）。"""
    last_id = s.ui.events_after(0, timeout=0)[-1]["id"]
    assert s.browser.post("/api/conversation/open", {"id": conversation_id}) == OK
    while True:
        reset = s.browser.wait_for("reset")
        if reset["id"] > last_id:
            break
    state = s.browser.wait_for("state")
    return [event for event in s.browser.events if reset["id"] < event["id"] < state["id"]], state


def kinds(events: list[dict]) -> list[str]:
    return [event["type"] for event in events]


def listed(state: dict) -> list[tuple[str, str]]:
    """会话列表里的（编号，标题），按显示的顺序。"""
    return [(item["id"], item["title"]) for item in state["conversations"]]


def saved_ids(config) -> list[str]:
    """磁盘上有哪些会话文件。"""
    return sorted(path.stem for path in config.conversations_dir.glob("*.json"))


def saved_file(config, conversation_id: str) -> dict:
    return json.loads((config.conversations_dir / f"{conversation_id}.json").read_text(encoding="utf-8"))


def test_a_conversation_is_listed_at_once_and_saved_after_its_first_turn(serve):
    s = serve(
        reply(tool_use("toolu_1", "run_command", {"command": "true"}), stop_reason="tool_use"),
        reply(text("跑完了。")),
    )
    start = s.browser.wait_for("state")
    # 启动时是一段还没有内容的新对话：没有标题，也不在列表里
    assert start["title"] == "" and start["conversations"] == []
    conversation_id = start["conversation"]

    s.browser.post("/api/message", {"text": "帮我跑一下 true\n（第二行不进标题）"})
    s.browser.wait_for("turn_start")
    started = s.browser.wait_for("state")
    confirm = s.browser.wait_for("confirm")  # 这一轮卡在确认上，还没有结束
    # 第一轮刚开始，这段会话就带着标题出现在列表里了，但还没有写盘
    assert started["conversation"] == conversation_id and started["title"] == "帮我跑一下 true"
    (item,) = started["conversations"]
    assert item == {
        "id": conversation_id,
        "title": "帮我跑一下 true",
        "provider": "anthropic",
        "model": "claude-opus-5-5",
        "updated_at": item["updated_at"],
    }
    assert listed(latest_state(s.browser)) == [(conversation_id, "帮我跑一下 true")]
    assert not s.config.conversations_dir.exists()

    s.browser.post("/api/confirm", {"id": confirm["id"], "allow": True})
    s.browser.wait_for("turn_end")

    # 一轮结束：磁盘上有了这段会话的文件，编号没变
    after = latest_state(s.browser)
    assert saved_ids(s.config) == [conversation_id]
    saved = saved_file(s.config, conversation_id)
    assert (saved["title"], saved["provider"], saved["model"]) == ("帮我跑一下 true", "anthropic", "claude-opus-5-5")
    assert saved["context_tokens"] == 170 and saved["created_at"] <= saved["updated_at"]
    # messages 是发给模型的历史，原样保存；events 是页面上显示过的内容
    assert saved["messages"] == s.app.agent.messages
    assert [message["role"] for message in saved["messages"]] == ["user", "assistant", "user", "assistant"]
    assert kinds(saved["events"]) == ["user", "turn_start", "tool_call", "confirm", "tool_result", "text", "block_end"]
    assert saved["events"][3] == {
        "type": "confirm",
        "description": confirm["description"],
        "key": confirm["key"],  # 原文和填进去的值也存着：以后换了语言再打开，页面能把这句话换成新的语言
        "values": confirm["values"],
        "outcome": "allowed",
    }
    assert after["conversation"] == conversation_id
    assert after["conversations"] == [{**item, "updated_at": saved["updated_at"]}]


def test_each_new_conversation_gets_its_own_file_and_the_newest_is_listed_first(serve):
    s = serve(reply(text("一")), reply(text("二")), reply(text("三")))
    said = say(s, "第一段")
    first = said["conversation"]
    first_file = (s.config.conversations_dir / f"{first}.json").read_bytes()
    assert said["usage"]["context"] > 0

    blank = act(s, "/api/new")
    # 新对话在说第一句话之前不进列表，也不写盘；第一段还在列表里
    assert blank["conversation"] != first and blank["title"] == ""
    assert blank["usage"]["context"] == 0  # 侧栏的"当前对话占用的上下文"也从零开始，不留着上一段的数
    assert listed(blank) == [(first, "第一段")] and saved_ids(s.config) == [first]

    state = say(s, "第二段")
    second = state["conversation"]
    assert second == blank["conversation"]
    assert listed(state) == [(second, "第二段"), (first, "第一段")]  # 新的在前
    assert saved_ids(s.config) == sorted([first, second])
    # 两段各存各的：第二段里没有第一段的内容，第一段的文件也没有被动过
    assert [message["role"] for message in s.api.requests[1]["messages"]] == ["user"]
    assert kinds(saved_file(s.config, second)["events"]) == ["user", "turn_start", "text", "block_end"]
    assert saved_file(s.config, second)["events"][0]["text"] == "第二段"
    assert (s.config.conversations_dir / f"{first}.json").read_bytes() == first_file

    # 在同一段里接着聊：更新的是同一个文件，不会多出一段
    state = say(s, "第二段的第二句")
    assert listed(state) == [(second, "第二段"), (first, "第一段")] and saved_ids(s.config) == sorted([first, second])
    assert len(saved_file(s.config, second)["messages"]) == 4
    assert kinds(saved_file(s.config, second)["events"]) == [
        "user", "turn_start", "text", "block_end", "turn_end", "user", "turn_start", "text", "block_end",
    ]


def test_opening_a_conversation_replays_it_and_restores_its_history(serve):
    s = serve(
        reply(thinking("要建文件"), tool_use("toolu_1", "run_command", {"command": "touch made.txt"}),
              stop_reason="tool_use"),
        reply(text("建好了。")),
        reply(text("这是第二段。")),
        reply(text("刚才建的是 made.txt。")),
    )
    s.browser.post("/api/message", {"text": "建个文件"})
    confirm = s.browser.wait_for("confirm")
    s.browser.post("/api/confirm", {"id": confirm["id"], "allow": True})
    s.browser.wait_for("turn_end")
    first = latest_state(s.browser)["conversation"]
    # 浏览器当初看到的内容（侧栏的状态不算）
    original = [event for event in s.browser.events if event["type"] != "state"]
    history = saved_file(s.config, first)["messages"]
    act(s, "/api/new")
    second = say(s, "另一段")["conversation"]

    replayed, state = open_conversation(s, first)

    # 页面先清空，然后把第一段重新画出来：事件的种类、顺序、内容都和当初一样
    assert kinds(replayed) == [
        "user", "turn_start", "thinking", "block_end", "tool_call", "confirm", "confirm_done", "tool_result",
        "text", "block_end", "turn_end",
    ]

    def content(event: dict) -> dict:  # 编号是重新分配的，不参与比较
        return {key: value for key, value in event.items() if key not in ("id", "confirm")}

    assert [content(event) for event in replayed] == [content(event) for event in original]
    # 确认卡片带着它的结果一起回来，结果指向的是卡片的新编号：页面上不会留下一张还在等答复的卡片
    card, done = replayed[5], replayed[6]
    assert card["id"] != confirm["id"]
    assert (done["confirm"], done["allowed"], done["stopped"]) == (card["id"], True, False)
    assert s.browser.post("/api/confirm", {"id": card["id"], "allow": True})[0] == 409  # 它只是记录，不能再答复
    assert (state["conversation"], state["title"]) == (first, "建个文件")
    assert listed(state) == [(second, "另一段"), (first, "建个文件")]  # 只是打开看看，不改变顺序
    assert s.app.agent.messages == history

    # 再点一次已经打开的会话：什么都不发生
    before = s.ui.events_after(0, timeout=0)
    assert s.browser.post("/api/conversation/open", {"id": first}) == OK
    assert s.ui.events_after(0, timeout=0) == before

    # 接着聊：请求里带着第一段的全部历史，新消息排在最后
    state = say(s, "刚才建的文件叫什么？")
    request = s.api.requests[-1]
    assert request["messages"][: len(history)] == history
    (new_message,) = request["messages"][len(history) :]
    assert new_message["role"] == "user" and new_message["content"][1]["text"] == "刚才建的文件叫什么？"
    # 存回的是第一段自己的文件；它刚更新过，所以排到了最前面；标题还是最初的那一句
    saved = saved_file(s.config, first)
    assert len(saved["messages"]) == len(history) + 2 and saved["title"] == "建个文件"
    assert kinds(saved["events"]) == [
        "user", "turn_start", "thinking", "block_end", "tool_call", "confirm", "tool_result", "text", "block_end",
        "turn_end", "user", "turn_start", "text", "block_end",
    ]
    assert listed(state) == [(first, "建个文件"), (second, "另一段")]
    assert len(saved_file(s.config, second)["messages"]) == 2


def test_reopened_confirmations_show_how_they_ended(serve):
    def command(id: str, name: str) -> dict:
        return reply(tool_use(id, "run_command", {"command": f"touch {name}"}), stop_reason="tool_use")

    s = serve(command("toolu_1", "a.txt"), reply(text("建好了。")), command("toolu_2", "b.txt"), reply(text("那就不建。")),
              command("toolu_3", "c.txt"), reply(text("好的。")))
    s.browser.post("/api/message", {"text": "建 a.txt"})
    s.browser.post("/api/confirm", {"id": s.browser.wait_for("confirm")["id"], "allow": True})  # 允许
    s.browser.wait_for("turn_end")
    s.browser.post("/api/message", {"text": "建 b.txt"})
    s.browser.post("/api/confirm", {"id": s.browser.wait_for("confirm")["id"], "allow": False})  # 拒绝
    s.browser.wait_for("turn_end")
    s.browser.post("/api/message", {"text": "建 c.txt"})
    s.browser.wait_for("confirm")
    s.browser.post("/api/stop")  # 还没答复就点了"停止"
    s.browser.wait_for("turn_end")
    first = latest_state(s.browser)["conversation"]

    assert [event["outcome"] for event in saved_file(s.config, first)["events"] if event["type"] == "confirm"] == [
        "allowed", "denied", "stopped",
    ]
    act(s, "/api/new")
    replayed, _ = open_conversation(s, first)

    cards = [event for event in replayed if event["type"] == "confirm"]
    answers = [event for event in replayed if event["type"] == "confirm_done"]
    assert len(cards) == 3 and all(
        name in card["description"] for name, card in zip(("a.txt", "b.txt", "c.txt"), cards, strict=True)
    )
    assert [answer["confirm"] for answer in answers] == [card["id"] for card in cards]  # 三张卡片都有结果
    assert [(answer["allowed"], answer["stopped"]) for answer in answers] == [(True, False), (False, False), (False, True)]
    assert kinds(replayed).count("turn_start") == kinds(replayed).count("turn_end") == 3
    assert replayed[-1]["type"] == "turn_end"  # 页面不会以为助手还在工作

    # 被中断的那一轮也在历史里（工具调用都补上了结果），所以接着聊不会出错
    say(s, "继续")
    assert [message["role"] for message in s.api.requests[-1]["messages"]] == [
        "user", "assistant", "user", "assistant",  # 建 a.txt：允许
        "user", "assistant", "user", "assistant",  # 建 b.txt：拒绝
        "user", "assistant", "user",  # 建 c.txt：中断
        "user",  # 继续
    ]
    assert s.browser.of_type("notice")[-1]["level"] != "error"


def test_saved_conversations_are_there_after_a_restart(serve):
    s = serve(reply(thinking("记一下", signature="sig-keep"), text("暗号记住了。")))
    first = say(s, "记住暗号是 42")["conversation"]
    history = saved_file(s.config, first)["messages"]

    restarted = serve(reply(text("暗号是 42。")))  # 同一个数据目录：相当于把程序关掉再打开
    start = restarted.browser.wait_for("state")
    # 启动时是一段新对话，不会自动打开上次的；上次的在列表里等着
    assert start["conversation"] != first and start["title"] == ""
    assert listed(start) == [(first, "记住暗号是 42")]
    assert restarted.app.agent.messages == [] and restarted.browser.types() == ["state"]

    replayed, state = open_conversation(restarted, first)
    assert [(event["type"], event.get("text")) for event in replayed] == [
        ("user", "记住暗号是 42"), ("turn_start", None), ("thinking", "记一下"), ("block_end", None),
        ("text", "暗号记住了。"), ("block_end", None), ("turn_end", None),
    ]
    assert (state["conversation"], state["title"], state["usage"]["context"]) == (first, "记住暗号是 42", 170)

    say(restarted, "暗号是多少")
    (request,) = restarted.api.requests
    assert request["messages"][:2] == history
    assert request["messages"][1]["content"][0]["signature"] == "sig-keep"  # 思考内容的签名原样发了回去
    assert request["messages"][2]["content"][1]["text"] == "暗号是多少"
    assert saved_ids(restarted.config) == [first]  # 还是那一个文件
    assert len(saved_file(restarted.config, first)["messages"]) == 4


def test_renaming_a_conversation(serve):
    s = serve(reply(text("一")), reply(text("二")), reply(text("三")))
    first = say(s, "第一段")["conversation"]
    act(s, "/api/new")
    state = say(s, "第二段")
    second = state["conversation"]
    times = {item["id"]: item["updated_at"] for item in state["conversations"]}

    # 给不是当前的那一段改名：名字变了，更新时间和顺序都不变
    state = act(s, "/api/conversation/rename", {"id": first, "title": "  旧的那段  "})
    assert listed(state) == [(second, "第二段"), (first, "旧的那段")]
    assert {item["id"]: item["updated_at"] for item in state["conversations"]} == times
    assert state["title"] == "第二段"
    assert saved_file(s.config, first)["title"] == "旧的那段"

    # 给当前这一段改名：页面顶上的标题也跟着变
    state = act(s, "/api/conversation/rename", {"id": second, "title": "新的那段"})
    assert state["title"] == "新的那段" and listed(state) == [(second, "新的那段"), (first, "旧的那段")]
    assert {item["id"]: item["updated_at"] for item in state["conversations"]} == times

    # 太长的名字被截断
    state = act(s, "/api/conversation/rename", {"id": first, "title": "长" * 60})
    assert listed(state)[1] == (first, "长" * 39 + "…")

    # 接着聊：自己起的名字不会被覆盖回"第一句话"
    state = say(s, "第二段的第二句")
    assert state["title"] == "新的那段" and saved_file(s.config, second)["title"] == "新的那段"

    for bad in ({"id": first}, {"id": first, "title": "   "}, {"id": first, "title": "长" * 201}, {"id": first, "title": 5},
                {"id": 5, "title": "名字"}):
        assert s.browser.post("/api/conversation/rename", bad) == (400, {"error": "参数不正确"})
    assert saved_file(s.config, first)["title"] == "长" * 39 + "…"


def test_deleting_conversations(serve):
    s = serve(reply(text("一")), reply(text("二")), reply(text("三")))
    first = say(s, "第一段")["conversation"]
    act(s, "/api/new")
    second = say(s, "第二段")["conversation"]
    resets = len(s.browser.of_type("reset"))

    # 删掉不是当前的那一段：它从列表和磁盘上消失，页面上正在看的这一段不受影响
    state = act(s, "/api/conversation/delete", {"id": first})
    assert listed(state) == [(second, "第二段")] and state["conversation"] == second
    assert saved_ids(s.config) == [second]
    assert len(s.browser.of_type("reset")) == resets and len(s.app.agent.messages) == 2
    status, body = s.browser.post("/api/conversation/delete", {"id": first})
    assert status == 409 and "不存在" in body["error"]  # 删过的不能再删

    # 删掉当前这一段：页面清空，换成一段全新的对话
    assert s.app.agent.usage.context_tokens > 0
    assert s.browser.post("/api/conversation/delete", {"id": second}) == OK
    s.browser.wait_for("reset")
    state = s.browser.wait_for("state")
    assert state["conversations"] == [] and state["title"] == ""
    assert state["usage"]["context"] == 0
    assert state["conversation"] not in (first, second)
    assert saved_ids(s.config) == [] and s.app.agent.messages == []
    late = Browser(s.url)  # 这时才打开页面的浏览器，看不到被删掉的内容
    late.wait_for("state")
    assert late.types() == ["reset", "state"]

    # 之后说的话属于新的一段，请求里没有被删掉的历史
    state = say(s, "第三段")
    assert [message["role"] for message in s.api.requests[-1]["messages"]] == ["user"]
    assert listed(state) == [(state["conversation"], "第三段")] and saved_ids(s.config) == [state["conversation"]]


def test_new_and_deleted_conversations_start_clean_with_other_providers(serve, monkeypatch):
    # 上面几条都是用 Claude 跑的。GPT、Gemini、DeepSeek 走的是另一个适配层，它清空历史的写法不一样
    # （系统提示词是历史里的第一条，清空时要留着它），所以单独测一遍
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    s = serve(others=[chat("一。"), chat("二。"), chat("三。")])
    act(s, "/api/model", {"provider": "deepseek", "model": "deepseek-flash"})
    say(s, "第一段")
    assert [message["role"] for message in s.other_api.requests[-1]["messages"]] == ["system", "user"]

    def fresh_start(state: dict) -> None:
        """这一段是从头开始的：请求里只有系统提示词和这一句，存下来的也只有这一问一答。"""
        request = s.other_api.requests[-1]["messages"]
        assert [message["role"] for message in request] == ["system", "user"]  # 上一段的历史不在
        assert "个人助手" in request[0]["content"]  # 系统提示词还在
        saved = saved_file(s.config, state["conversation"])
        assert [message["role"] for message in saved["messages"]] == ["user", "assistant"]

    act(s, "/api/new")
    state = say(s, "第二段")
    fresh_start(state)

    # 删掉当前这一段，同样从头开始
    act(s, "/api/conversation/delete", {"id": state["conversation"]})
    fresh_start(say(s, "第三段"))


def test_conversations_cannot_be_opened_renamed_or_deleted_during_a_turn(serve):
    s = serve(
        reply(text("一")),
        reply(tool_use("toolu_1", "run_command", {"command": "true"}), stop_reason="tool_use"),
        reply(text("好。")),
    )
    first = say(s, "第一段")["conversation"]
    current = act(s, "/api/new")["conversation"]
    s.browser.post("/api/message", {"text": "跑一下"})
    confirm = s.browser.wait_for("confirm")  # 这一轮卡在确认上
    resets = len(s.browser.of_type("reset"))

    for path, data in (
        ("/api/conversation/open", {"id": first}),
        ("/api/conversation/rename", {"id": first, "title": "改个名"}),  # 改名要改写文件，会和轮末的存盘撞上
        ("/api/conversation/rename", {"id": current, "title": "改个名"}),
        ("/api/conversation/delete", {"id": first}),
        ("/api/conversation/delete", {"id": current}),
    ):
        status, body = s.browser.post(path, data)
        assert status == 409 and "等当前这一轮对话结束" in body["error"]
    assert saved_ids(s.config) == [first] and saved_file(s.config, first)["title"] == "第一段"

    s.browser.post("/api/confirm", {"id": confirm["id"], "allow": True})
    s.browser.wait_for("turn_end")
    # 这一轮没有受到打扰：页面没有被清空，内容存进了它自己的那一段
    state = latest_state(s.browser)
    assert len(s.browser.of_type("reset")) == resets
    assert listed(state) == [(current, "跑一下"), (first, "第一段")] and saved_ids(s.config) == sorted([first, current])
    assert [message["role"] for message in s.api.requests[-1]["messages"]] == ["user", "assistant", "user"]
    # 结束之后就可以了
    replayed, state = open_conversation(s, first)
    assert state["conversation"] == first and kinds(replayed) == ["user", "turn_start", "text", "block_end", "turn_end"]


def test_unknown_and_malformed_conversation_ids_are_refused(serve):
    s = serve(reply(text("一")))
    first = say(s, "第一段")["conversation"]
    before = s.ui.events_after(0, timeout=0)
    file_before = (s.config.conversations_dir / f"{first}.json").read_bytes()

    for action, extra in (("open", {}), ("delete", {}), ("rename", {"title": "新名字"})):
        path = f"/api/conversation/{action}"
        # 编号的格式是对的，但没有这段会话
        status, body = s.browser.post(path, {"id": MISSING, **extra})
        assert status == 409 and "不存在" in body["error"]
        # 编号的格式就不对（包括想借着它摸到别的目录去的）：同样被拒绝，并且说得出原因
        for bad in ("", "x", "../x", "/etc/passwd", f"../conversations/{first}", f"{first}.json", f"{first}\n", "9" * 5000):
            status, body = s.browser.post(path, {"id": bad, **extra})
            assert status == 409 and ("不存在" in body["error"] or "格式不对" in body["error"])
        # 编号根本不是一段文字
        for bad in (None, 5, True, [first], {"id": first}):
            assert s.browser.post(path, {"id": bad, **extra}) == (400, {"error": "参数不正确"})
        assert s.browser.post(path, extra) == (400, {"error": "参数不正确"})  # 没有给编号
    assert s.browser.post("/api/conversation/explode", {"id": first}) == (400, {"error": "参数不正确"})  # 没有这种操作

    # 这些请求什么都没有改动：页面没有收到任何新事件，文件原封不动，也没有在别处多出文件
    assert s.ui.events_after(0, timeout=0) == before
    assert (s.config.conversations_dir / f"{first}.json").read_bytes() == file_before
    assert saved_ids(s.config) == [first] and s.app.conversation.id == first
    others = [path for path in s.config.data_dir.rglob("*") if path.is_file() and path not in (s.config.settings_file, s.config.ledger_file)]
    assert [path.name for path in others] == [f"{first}.json"]


def test_a_failed_first_message_leaves_no_conversation_behind(serve):
    s = serve()  # 没有预设回复：假 API 返回 500
    s.browser.post("/api/message", {"text": "你好"})
    s.browser.wait_for("turn_end")

    assert "500" in s.browser.of_type("notice")[0]["text"]
    states = s.browser.of_type("state")
    conversation_id = states[0]["conversation"]
    assert listed(states[1]) == [(conversation_id, "你好")]  # 这一轮进行中，它在列表里出现过
    # 可是什么都没聊成：没有会话文件，列表里也不留下一段空会话
    assert states[-1]["conversations"] == [] and states[-1]["title"] == ""
    assert not s.config.conversations_dir.exists()

    # 再试一次成功了：还是这一段，用这次的话当标题
    s.api.replies.append(reply(text("你好！")))
    state = say(s, "再试一次")
    assert listed(state) == [(conversation_id, "再试一次")] and saved_ids(s.config) == [conversation_id]
    assert [message["role"] for message in saved_file(s.config, conversation_id)["messages"]] == ["user", "assistant"]


def test_a_failed_save_is_reported_and_does_not_block_the_next_turn(serve):
    s = serve(reply(text("一")), reply(text("二")))
    s.config.data_dir.mkdir(parents=True, exist_ok=True)
    s.config.conversations_dir.write_text("这里本该是个目录", encoding="utf-8")  # 让存盘失败

    state = say(s, "第一句")
    notice = s.browser.of_type("notice")[-1]
    assert notice["level"] == "error" and "保存会话失败" in notice["text"]
    assert s.browser.types()[-1] == "turn_end"  # 这一轮照常结束了
    assert listed(state) == [(state["conversation"], "第一句")]  # 对话还在内存里，只是没有存到磁盘上

    s.config.conversations_dir.unlink()  # 问题解决之后，下一轮就存上了，前面的内容也没有丢
    state = say(s, "第二句")
    assert saved_ids(s.config) == [state["conversation"]]
    assert len(saved_file(s.config, state["conversation"])["messages"]) == 4


def test_the_next_request_can_follow_turn_end_immediately(serve, monkeypatch):
    real_save = ConversationStore.save

    def slow_save(self, conversation, touch=True):
        time.sleep(0.3)  # 假装磁盘很慢
        real_save(self, conversation, touch)

    monkeypatch.setattr(ConversationStore, "save", slow_save)
    s = serve(reply(text("一")), reply(text("二")))
    s.browser.post("/api/message", {"text": "第一句"})
    s.browser.wait_for("turn_end")
    # "这一轮结束了"是存完盘、马上要放开锁的时候才发出来的。页面一收到它就可以发下一个请求，
    # 不会因为还在存盘而被当成"上一轮对话还没有结束"
    assert s.browser.post("/api/message", {"text": "第二句"}) == OK
    s.browser.wait_for("turn_end")
    assert len(s.api.requests) == 2 and len(saved_file(s.config, s.app.conversation.id)["messages"]) == 4


def test_opening_a_conversation_switches_back_to_its_provider(serve, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-12345678")
    s = serve(
        reply(text("我是 Claude。")),
        reply(text("还是 Claude。")),
        others=[chat("我是 DeepSeek。", reasoning="打个招呼"), chat("还是 DeepSeek。")],
    )
    # 先换到 DeepSeek 聊一段
    act(s, "/api/model", {"provider": "deepseek", "model": "deepseek-flash"})
    state = say(s, "你是谁")
    deepseek = state["conversation"]
    assert saved_file(s.config, deepseek)["provider"] == "deepseek"
    assert saved_file(s.config, deepseek)["context_tokens"] == 120

    # 切回 Claude：换了一家，开始一段新对话；DeepSeek 那一段留在列表里
    state = act(s, "/api/model", {"provider": "anthropic", "model": "claude-opus-5-5"})
    assert state["provider"] == "anthropic" and state["conversation"] != deepseek
    assert [(item["id"], item["provider"], item["model"]) for item in state["conversations"]] == [
        (deepseek, "deepseek", "deepseek-flash")
    ]
    state = say(s, "你又是谁")
    claude = state["conversation"]
    assert [(item["id"], item["provider"]) for item in state["conversations"]] == [
        (claude, "anthropic"), (deepseek, "deepseek"),
    ]

    # 打开 DeepSeek 那一段：厂商和模型都换回它当时用的
    replayed, state = open_conversation(s, deepseek)
    assert (state["provider"], state["model"], state["conversation"]) == ("deepseek", "deepseek-flash", deepseek)
    assert state["usage"]["context"] == 120
    assert "web_search" not in [tool["name"] for tool in state["tools"]]  # 工具列表也换成了这一家的
    assert json.loads(s.config.settings_file.read_text(encoding="utf-8"))["provider"] == "deepseek"
    # 逐字到达的回答，重放时是完整的一段
    assert [(event["type"], event.get("text")) for event in replayed if event["type"] in ("user", "thinking", "text")] == [
        ("user", "你是谁"), ("thinking", "打个招呼"), ("text", "我是 DeepSeek。"),
    ]
    assert replayed[-1]["type"] == "turn_end"

    # 接着聊：请求发往 OpenAI 格式的服务器，带着这一段的历史
    say(s, "接着说")
    assert len(s.api.requests) == 1  # Claude 那边没有再收到请求
    first_request, second_request = s.other_api.requests
    assert second_request["model"] == "deepseek-flash"
    assert [message["role"] for message in second_request["messages"]] == ["system", "user", "assistant", "user"]
    assert second_request["messages"][:2] == first_request["messages"]
    assert second_request["messages"][2] == {
        "role": "assistant", "content": "我是 DeepSeek。", "reasoning_content": "打个招呼",
    }

    # 反过来也一样：打开 Claude 那一段，又回到 Claude
    _, state = open_conversation(s, claude)
    assert (state["provider"], state["model"], state["conversation"]) == ("anthropic", "claude-opus-5-5", claude)
    assert "web_search" in [tool["name"] for tool in state["tools"]]
    say(s, "接着说")
    assert len(s.other_api.requests) == 2
    assert [message["role"] for message in s.api.requests[1]["messages"]] == ["user", "assistant", "user"]
    assert saved_ids(s.config) == sorted([claude, deepseek])


def test_opening_a_large_conversation_shows_a_cost_hint_that_is_not_saved(serve):
    s = serve(reply(text("一")), reply(text("二")))
    first = say(s, "第一段")["conversation"]
    path = s.config.conversations_dir / f"{first}.json"

    def reopen(context_tokens: int) -> tuple[list[dict], dict]:
        """把磁盘上这段会话记录的大小改掉，然后从一段新对话里重新打开它。"""
        act(s, "/api/new")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["context_tokens"] = context_tokens
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return open_conversation(s, first)

    replayed, state = reopen(LARGE_CONTEXT_TOKENS - 1)
    assert kinds(replayed) == ["user", "turn_start", "text", "block_end", "turn_end"]  # 还不算大：没有提示
    assert state["usage"]["context"] == LARGE_CONTEXT_TOKENS - 1

    replayed, state = reopen(LARGE_CONTEXT_TOKENS)
    assert kinds(replayed) == ["user", "turn_start", "text", "block_end", "turn_end", "notice"]
    hint = replayed[-1]
    # 提示排在重放的内容之后，带着"只显示、不保存"的记号
    assert (hint["level"], hint["transient"]) == ("info", True)
    assert f"{LARGE_CONTEXT_TOKENS:,}" in hint["text"] and "计费" in hint["text"]
    assert state["usage"]["context"] == LARGE_CONTEXT_TOKENS

    # 接着聊一轮，存盘：文件里没有这条提示
    say(s, "接着聊")
    saved = saved_file(s.config, first)
    assert kinds(saved["events"]) == [
        "user", "turn_start", "text", "block_end", "turn_end", "user", "turn_start", "text", "block_end",
    ]
    assert "transient" not in json.dumps(saved["events"]) and "计费" not in json.dumps(saved["events"], ensure_ascii=False)
    assert saved["context_tokens"] == 170  # 换成了这次请求实际的大小


def test_switching_models_is_recorded_in_the_conversation(serve):
    s = serve(reply(text("你好。")), reply(text("二")), reply(text("还在。")))
    state = say(s, "记住暗号是 42")
    first = state["conversation"]
    before = saved_file(s.config, first)
    assert before["model"] == "claude-opus-5-5"

    # 同一家之内换模型：还是这一段会话，文件里记下了新模型
    state = act(s, "/api/model", {"provider": "anthropic", "model": "claude-sonnet-5-5"})
    assert state["conversation"] == first
    assert [(item["id"], item["model"]) for item in state["conversations"]] == [(first, "claude-sonnet-5-5")]
    after = saved_file(s.config, first)
    assert after["model"] == "claude-sonnet-5-5" and after["messages"] == before["messages"]
    assert after["title"] == before["title"] and saved_ids(s.config) == [first]

    # 另开一段，换回 opus 聊；再打开第一段时，模型跟着换回它自己的 sonnet
    act(s, "/api/new")
    act(s, "/api/model", {"provider": "anthropic", "model": "claude-opus-5-5"})
    second = say(s, "第二段")["conversation"]
    assert s.api.requests[1]["model"] == "claude-opus-5-5"
    assert (saved_file(s.config, second)["model"], saved_file(s.config, first)["model"]) == (
        "claude-opus-5-5", "claude-sonnet-5-5",
    )
    _, state = open_conversation(s, first)
    assert (state["provider"], state["model"]) == ("anthropic", "claude-sonnet-5-5")
    say(s, "暗号是多少")
    assert s.api.requests[2]["model"] == "claude-sonnet-5-5"
    assert [message["role"] for message in s.api.requests[2]["messages"]] == ["user", "assistant", "user"]
    assert json.loads(s.config.settings_file.read_text(encoding="utf-8"))["model"] == "claude-sonnet-5-5"


# ---- 搜索 -----------------------------------------------------------------
# 怎样匹配、怎样排序在 test_search.py 里测。这里只测接口：请求能不能到、结果是不是跟着会话变。


def search(s, query: str) -> list[dict]:
    status, body = s.browser.post("/api/search", {"query": query})
    assert status == 200 and body["ok"] is True
    return body["results"]


def test_search_finds_conversations_by_title_and_content(serve):
    s = serve(
        reply(text("蓝莓放冰箱能放一周。")),
        reply(tool_use("toolu_1", "todo_add", {"title": "交季度报告", "due": "2026-11-27"}), stop_reason="tool_use"),
        reply(text("记下了。")),
    )
    first = say(s, "蓝莓怎么保存")["conversation"]
    act(s, "/api/new")
    state = say(s, "提醒我周五交报告")
    second = state["conversation"]
    times = {item["id"]: item["updated_at"] for item in state["conversations"]}
    events_before = s.ui.events_after(0, timeout=0)

    # 每个结果是会话列表里的那几项，加上命中的情况：标题里哪几个字对上了，内容里摘出来的一小段
    (found,) = search(s, "蓝莓")
    assert found == {
        "id": first,
        "title": "蓝莓怎么保存",
        "provider": "anthropic",
        "model": "claude-opus-5-5",
        "updated_at": times[first],
        "exact": True,
        "title_marks": [[0, 2]],
        "snippet": {"kind": "user", "text": "蓝莓怎么保存", "marks": [[0, 2]], "find": "蓝莓"},
    }
    # 助手回复里的话
    (found,) = search(s, "冰箱")
    assert found["id"] == first and found["title_marks"] == []
    assert found["snippet"] == {"kind": "assistant", "text": "蓝莓放冰箱能放一周。", "marks": [[3, 5]], "find": "冰箱"}
    # 工具调用里的内容：用户说的是"交报告"，"季度"两个字只出现在助手调用工具时填的参数里
    (found,) = search(s, "季度报告")
    assert found["id"] == second and found["snippet"]["kind"] == "tool" and found["snippet"]["find"] == "季度报告"

    # 多个关键词，顺序无关
    assert [item["id"] for item in search(s, "报告 周五")] == [second]
    assert search(s, "蓝莓 报告") == []  # 这两个词不在同一段会话里
    # 记得不太准也行："周五报告"中间其实还隔着一个"交"字。结果里说明了这不是原样找到的
    (found,) = search(s, "周五报告")
    assert found["id"] == second and found["exact"] is False and found["title_marks"] == [[3, 5], [6, 8]]
    assert found["snippet"] is None  # 标题已经对上了，内容里又没有原样的，就不硬摘一段出来
    assert search(s, "哪儿都没有的词") == []

    # 搜索只是查，不改动任何东西：没有产生事件，会话的顺序和时间也没变
    assert s.ui.events_after(0, timeout=0) == events_before
    assert {item["id"]: item["updated_at"] for item in latest_state(s.browser)["conversations"]} == times


def test_search_rejects_requests_it_cannot_answer(serve):
    s = serve(reply(text("一")))
    first = say(s, "蓝莓怎么保存")["conversation"]
    host, token = s.browser.host, s.browser.token

    # 和别的接口一样：没有口令不行，口令放在地址里也不算
    assert request(host, "POST", "/api/search", {"query": "蓝莓"})[0] == 403
    assert request(host, "POST", f"/api/search?token={token}", {"query": "蓝莓"})[0] == 403
    assert request(host, "POST", "/api/search", {"query": "蓝莓"}, {"X-Harness-Token": "wrong"})[0] == 403
    # 别的网站发来的请求（带着它自己的 Origin）同样被拒绝
    evil = {"X-Harness-Token": token, "Origin": "http://evil.example"}
    assert request(host, "POST", "/api/search", {"query": "蓝莓"}, evil)[0] == 403
    assert request(host, "GET", f"/api/search?token={token}")[0] == 404  # 只接受 POST

    # 搜索词必须是一段不太长的文字
    too_long = "莓" * (MAX_QUERY_CHARS + 1)
    for bad in ({}, {"query": None}, {"query": 5}, {"query": True}, {"query": ["蓝莓"]}, {"query": {"a": 1}}, {"query": too_long}):
        status, body = s.browser.post("/api/search", bad)
        assert status == 400 and set(body) == {"error"}
    # 正好到上限可以
    assert s.browser.post("/api/search", {"query": "莓" * MAX_QUERY_CHARS}) == (200, {"ok": True, "results": []})
    assert [item["id"] for item in search(s, "蓝莓" + " " * (MAX_QUERY_CHARS - 2))] == [first]
    # 空的搜索词不算错，只是什么都找不到
    for empty in ("", "   ", "\n\t　"):
        assert s.browser.post("/api/search", {"query": empty}) == (200, {"ok": True, "results": []})


def test_search_works_during_a_turn_and_sees_it_once_it_is_saved(serve):
    s = serve(
        reply(text("蓝莓放冰箱。")),
        reply(tool_use("toolu_1", "run_command", {"command": "true"}), stop_reason="tool_use"),
        reply(text("草莓也一样。")),
    )
    first = say(s, "蓝莓怎么保存")["conversation"]
    current = act(s, "/api/new")["conversation"]
    s.browser.post("/api/message", {"text": "那草莓呢，先跑个命令"})
    confirm = s.browser.wait_for("confirm")  # 这一轮卡在确认上

    # 搜索只读内存里的索引，不改任何东西，所以对话进行中也可以搜（不像打开、改名、删除那样要等）
    assert [item["id"] for item in search(s, "蓝莓")] == [first]
    # 正在进行的这一轮还没有存盘，所以还搜不到
    assert search(s, "草莓") == [] and search(s, "run_command") == []

    s.browser.post("/api/confirm", {"id": confirm["id"], "allow": True})
    s.browser.wait_for("turn_end")
    # 这一轮一结束就存了盘，马上能搜到：用户说的、助手答的、运行过的命令都在里面
    (found,) = search(s, "草莓")
    assert found["id"] == current and found["title"] == "那草莓呢，先跑个命令"
    assert [item["id"] for item in search(s, "草莓也一样")] == [current]
    (found,) = search(s, "run_command")
    assert found["id"] == current and found["snippet"]["kind"] == "tool"
    assert found["snippet"]["text"] == "run_command $ true"
    # 接着聊下去，新说的话也跟着进了索引
    s.api.replies.append(reply(text("樱桃要放保鲜层。")))
    say(s, "樱桃呢")
    assert [item["id"] for item in search(s, "保鲜层")] == [current]


def test_search_follows_renames_and_deletes(serve):
    s = serve(reply(text("一")), reply(text("二")))
    first = say(s, "蓝莓怎么保存")["conversation"]
    act(s, "/api/new")
    second = say(s, "草莓怎么保存")["conversation"]
    assert [item["id"] for item in search(s, "怎么保存")] == [second, first]  # 一样相关，新的在前

    act(s, "/api/conversation/rename", {"id": first, "title": "水果笔记"})
    (found,) = search(s, "水果笔记")
    assert (found["id"], found["title"], found["title_marks"]) == (first, "水果笔记", [[0, 4]])
    # 标题改了，内容没变：按当初说过的话还能找到，只是不再算"标题里有"
    (found,) = search(s, "蓝莓")
    assert found["id"] == first and found["title_marks"] == [] and found["snippet"]["kind"] == "user"
    assert [item["id"] for item in search(s, "怎么保存")] == [second, first]

    act(s, "/api/conversation/delete", {"id": first})
    assert search(s, "蓝莓") == [] and search(s, "水果笔记") == []
    assert [item["id"] for item in search(s, "怎么保存")] == [second]
    # 删掉当前这一段也一样
    act(s, "/api/conversation/delete", {"id": second})
    assert search(s, "怎么保存") == [] and search(s, "草莓") == []


# ---- 在文件管理器里显示会话的文件 ---------------------------------------------
# 这是唯一一个"页面点一下，电脑上就启动一个程序"的接口，所以重点测它打开的只能是会话自己的文件。


def test_revealing_a_conversation_shows_its_own_file(serve):
    s = serve(reply(text("一")), reply(text("二")))
    first = say(s, "第一段")["conversation"]
    act(s, "/api/new")
    second = say(s, "第二段")["conversation"]
    folder = s.config.conversations_dir

    # 页面只给编号，路径是服务器自己算出来的
    assert s.browser.post("/api/conversation/reveal", {"id": first}) == (200, {"ok": True, "path": str(folder / f"{first}.json")})
    assert s.browser.post("/api/conversation/reveal", {"id": second}) == (200, {"ok": True, "path": str(folder / f"{second}.json")})
    # 编号为空：显示保存会话的那个文件夹
    assert s.browser.post("/api/conversation/reveal", {"id": ""}) == (200, {"ok": True, "path": str(folder)})
    assert s.revealed == [folder / f"{first}.json", folder / f"{second}.json", folder]
    # 只是看一眼文件在哪儿：会话本身和列表都没有变化
    assert listed(latest_state(s.browser)) == [(second, "第二段"), (first, "第一段")]


def test_reveal_never_shows_anything_but_a_saved_conversation(serve):
    s = serve(reply(text("一")))
    first = say(s, "第一段")["conversation"]
    (s.config.data_dir / "settings.json").write_text("{}", encoding="utf-8")  # 会话文件夹外面的一个文件
    host, token = s.browser.host, s.browser.token

    # 不是已保存会话的编号（包括想借编号拼出别处路径的）一律不认
    for bad in ("20261120-140000-aaaaaa", "../settings", f"../conversations/{first}", "/etc/passwd", f"{first}.json", f"{first}/..", "."):
        status, body = s.browser.post("/api/conversation/reveal", {"id": bad})
        assert status == 409 and "不存在" in body["error"], bad
    for malformed in ({}, {"id": 5}, {"id": None}, {"id": [first]}, {"path": "/etc/passwd"}):
        assert s.browser.post("/api/conversation/reveal", malformed) == (400, {"error": "参数不正确"})
    assert s.revealed == []

    # 请求里多带一个路径也没用：服务器根本不看它
    status, body = s.browser.post("/api/conversation/reveal", {"id": first, "path": "/etc/passwd", "file": "/etc/passwd"})
    assert status == 200 and body["path"] == str(s.config.conversations_dir / f"{first}.json")
    assert s.revealed == [s.config.conversations_dir / f"{first}.json"]

    # 没带口令、或者是别的网站发起的请求：和其他接口一样被拒绝，什么都不会打开
    assert request(host, "POST", "/api/conversation/reveal", {"id": first})[0] == 403
    assert request(host, "POST", "/api/conversation/reveal", {"id": first}, {"X-Harness-Token": "wrong"})[0] == 403
    evil = {"X-Harness-Token": token, "Origin": "http://evil.example"}
    assert request(host, "POST", "/api/conversation/reveal", {"id": first}, evil)[0] == 403
    assert len(s.revealed) == 1


def test_reveal_explains_when_there_is_nothing_to_show(serve, monkeypatch):
    command = tool_use("toolu_1", "run_command", {"command": "true"})
    s = serve(reply(command, stop_reason="tool_use"), reply(text("好。")), reply(command, stop_reason="tool_use"), reply(text("好。")))

    # 还没有保存过任何会话：连文件夹都还不存在
    status, body = s.browser.post("/api/conversation/reveal", {"id": ""})
    assert status == 409 and "还没有保存过会话" in body["error"]

    # 第一轮还在进行的会话已经出现在列表里，但还没有写到磁盘上
    s.browser.post("/api/message", {"text": "跑一下"})
    confirm = s.browser.wait_for("confirm")
    current = latest_state(s.browser)["conversation"]
    status, body = s.browser.post("/api/conversation/reveal", {"id": current})
    assert status == 409 and "还没有保存" in body["error"]
    assert s.revealed == []
    s.browser.post("/api/confirm", {"id": confirm["id"], "allow": True})
    s.browser.wait_for("turn_end")

    # 存过盘之后就可以了，而且对话进行中也能点：它只是看一眼，不拿对话的那把锁
    s.browser.post("/api/message", {"text": "再跑一下"})
    confirm = s.browser.wait_for("confirm")
    assert s.browser.post("/api/conversation/reveal", {"id": current})[0] == 200
    assert s.revealed == [s.config.conversations_dir / f"{current}.json"]

    # 文件管理器打不开时，把文件在哪儿告诉用户，让他自己去找
    def broken(path):
        raise OSError("找不到 open 这个命令")

    monkeypatch.setattr(server_module, "reveal_in_file_manager", broken)
    status, body = s.browser.post("/api/conversation/reveal", {"id": current})
    assert status == 409 and "找不到 open 这个命令" in body["error"] and f"{current}.json" in body["error"]
    s.browser.post("/api/confirm", {"id": confirm["id"], "allow": True})
    s.browser.wait_for("turn_end")


def test_the_file_manager_command_never_goes_through_a_shell(monkeypatch, tmp_path):
    calls = []

    class FakeProcess:
        returncode = 0

        def __init__(self, command, **options):
            calls.append((command, options))

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(server_module.subprocess, "Popen", FakeProcess)
    plain = tmp_path / "20261120-140000-aaaaaa.json"
    tricky = tmp_path / "a b; rm -rf ~ $(whoami) `id`.json"  # 文件名里全是 shell 的特殊字符
    for file in (plain, tricky):
        file.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(server_module.sys, "platform", "darwin")
    server_module.reveal_in_file_manager(plain)
    server_module.reveal_in_file_manager(tricky)
    server_module.reveal_in_file_manager(tmp_path)
    monkeypatch.setattr(server_module.sys, "platform", "linux")
    server_module.reveal_in_file_manager(plain)
    server_module.reveal_in_file_manager(tmp_path)

    assert [command for command, _ in calls] == [
        ["open", "-R", str(plain)],  # macOS：在访达里选中这个文件
        ["open", "-R", str(tricky)],  # 整个路径是一个参数，里面的字符不会被当成命令
        ["open", str(tmp_path)],  # 文件夹：直接打开
        ["xdg-open", str(tmp_path)],  # Linux：打开文件所在的文件夹
        ["xdg-open", str(tmp_path)],
    ]
    assert all(not options.get("shell") for _, options in calls)

    # 命令报告失败时要让调用的人知道
    FakeProcess.returncode = 1
    with pytest.raises(OSError, match="退出码 1"):
        server_module.reveal_in_file_manager(plain)

    # 有的文件管理器会一直开着不退出：等不到它结束不算失败
    def still_running(self, timeout=None):
        raise subprocess.TimeoutExpired("xdg-open", timeout)

    monkeypatch.setattr(FakeProcess, "wait", still_running)
    server_module.reveal_in_file_manager(plain)


# ---- 记忆 -----------------------------------------------------------------
# 信息栏的"记忆"一栏：列出 data/memories/ 里的文件、点开看内容、删掉一条、在文件管理器里打开文件夹。
# 浏览器送来的路径不能全信，所以重点测"出不了记忆文件夹"。

GONE = "不存在"


def memory_state(s) -> list[dict]:
    """让服务器发一次新的状态（把"要不要每次都问"设成原样），返回里面的记忆列表。"""
    return set_approval(s, "ask")["memories"]


def shown_memories(s, expected: list[str]) -> list[str]:
    """等页面收到的最新状态里，记忆列表变成 expected（最多等 5 秒）。

    服务器是先发状态、再回答请求的，但状态走的是另一条连接（事件流），测试读它的线程可能还没收到。
    这里只等、不催：要是删除之后服务器根本没发状态，这里会等到超时，测试照样失败。
    """
    deadline = time.monotonic() + 5
    while (paths := [item["path"] for item in latest_state(s.browser)["memories"]]) != expected and time.monotonic() < deadline:
        time.sleep(0.01)
    return paths


def write(path, text: str = "记着", data: bytes | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if data is None:
        path.write_text(text, encoding="utf-8")
    else:
        path.write_bytes(data)
    return path


def test_memories_are_listed_in_the_state(serve):
    s = serve()
    root = s.config.memory_dir
    assert root.is_dir()  # 记忆工具在组装的时候就把文件夹建好了
    assert latest_state(s.browser)["memories"] == []  # 还没记过任何东西

    write(root / "user.md", "喜欢简短的回答")
    write(root / "projects" / "zhifox.md", "智狐")
    write(root / "projects" / "deep" / "a.md", "a")
    write(root / "Banana.md", "b")  # 大小写不同的名字照样按字母排，不是大写的全排在前面
    # 隐藏的文件和文件夹不列：访达的 .DS_Store、记忆工具改文件时的临时文件、.git 之类
    write(root / ".DS_Store", data=b"\x00\x01")
    write(root / ".tmp-123-abc", "写了一半")
    write(root / ".hidden" / "secret.md", "藏起来的")
    # 指向文件夹外面的链接不列（文件和文件夹都是）
    outside = s.config.data_dir.parent / "outside"
    write(outside / "secret.txt", "外面的秘密")
    os.symlink(outside / "secret.txt", root / "link.md")
    os.symlink(outside, root / "linkdir")

    items = memory_state(s)
    assert [item["path"] for item in items] == ["Banana.md", "projects/deep/a.md", "projects/zhifox.md", "user.md"]
    user = items[-1]
    assert user["size"] == len("喜欢简短的回答".encode()) and user["modified"] == round((root / "user.md").stat().st_mtime, 3)
    assert set(user) == {"path", "size", "modified"}  # 状态里只有名字、大小和时间，不带内容

    # 文件夹整个不见了（比如你手动删了）：列表就是空的，不出错
    shutil.rmtree(root)
    assert memory_state(s) == []


def test_a_memory_the_model_writes_shows_up_without_polling(serve):
    s = serve(
        reply(tool_use("toolu_1", "memory", {"command": "create", "path": "/memories/user.md", "file_text": "喜欢简短的回答"}), stop_reason="tool_use"),
        reply(text("记住了。")),
    )
    s.browser.post("/api/message", {"text": "记住：我喜欢简短的回答"})
    s.browser.wait_for("turn_end")
    assert s.browser.of_type("confirm") == []  # 记忆只动助手自己的文件，不用问
    states = s.browser.of_type("state")
    assert states[0]["memories"] == []
    # 这一轮还没结束、模型写完记忆之后再调用一次模型时发的那次状态里就有了：页面不用自己去轮询
    during = next(state for state in states if state["usage"]["requests"] == 2)
    assert during["id"] < s.browser.of_type("turn_end")[0]["id"]
    assert [item["path"] for item in during["memories"]] == ["user.md"]
    assert [item["path"] for item in states[-1]["memories"]] == ["user.md"]
    assert states[-1]["memories"][0]["size"] == len("喜欢简短的回答".encode())
    status, body = s.browser.post("/api/memory/read", {"path": "user.md"})
    assert status == 200 and body["text"] == "喜欢简短的回答"


def test_reading_a_memory(serve):
    s = serve()
    root = s.config.memory_dir
    write(root / "projects" / "zhifox.md", "# 智狐\n标志是一只小狐狸。\n")
    write(root / "empty.md", "")
    write(root / "binary.md", data=b"abc\x00def")

    status, body = s.browser.post("/api/memory/read", {"path": "projects/zhifox.md"})
    assert status == 200
    assert body == {
        "ok": True,
        "path": "projects/zhifox.md",
        "text": "# 智狐\n标志是一只小狐狸。\n",
        "size": len("# 智狐\n标志是一只小狐狸。\n".encode()),
        "modified": round((root / "projects" / "zhifox.md").stat().st_mtime, 3),
        "truncated": False,
        "editable": True,
    }
    status, body = s.browser.post("/api/memory/read", {"path": "empty.md"})
    assert status == 200 and body["text"] == "" and body["size"] == 0 and body["truncated"] is False

    # 太长的只读开头。截断的地方正好切在一个汉字中间：那半个字不要，也不变成"�"
    limit = memories.MAX_READ_BYTES
    long = "a" * (limit - 1) + "狐" + "b" * 100  # "狐" 占 3 个字节，从第 limit-1 个字节开始，跨过了截断的位置
    write(root / "long.md", long)
    status, body = s.browser.post("/api/memory/read", {"path": "long.md"})
    assert status == 200 and body["truncated"] is True and body["size"] == len(long.encode())
    assert body["text"] == "a" * (limit - 1) and "�" not in body["text"]
    assert body["editable"] is False  # 只读出了开头：在网页上改了再存，后面那部分就没了
    write(root / "gbk.md", data="旧的备忘".encode("gbk"))  # 不是 UTF-8：显示时认不出的字节换成了"�"，存回去就坏了
    status, body = s.browser.post("/api/memory/read", {"path": "gbk.md"})
    assert status == 200 and body["editable"] is False

    # 不是文本文件、不存在、是个文件夹：都说清楚原因，不返回内容
    status, body = s.browser.post("/api/memory/read", {"path": "binary.md"})
    assert status == 409 and "不是文本文件" in body["error"]
    status, body = s.browser.post("/api/memory/read", {"path": "nope.md"})
    assert status == 409 and GONE in body["error"]
    status, body = s.browser.post("/api/memory/read", {"path": "projects"})
    assert status == 409 and "不是一个文件" in body["error"]


def test_deleting_a_memory(serve):
    s = serve()
    root = s.config.memory_dir
    write(root / "user.md")
    write(root / "projects" / "deep" / "a.md")
    write(root / "notes" / "b.md")
    write(root / "notes" / ".DS_Store", data=b"\x00")

    assert s.browser.post("/api/memory/delete", {"path": "user.md"}) == OK
    assert not (root / "user.md").exists()
    # 删完服务器马上发一次状态，列表跟着变
    assert shown_memories(s, ["notes/b.md", "projects/deep/a.md"]) == ["notes/b.md", "projects/deep/a.md"]
    status, body = s.browser.post("/api/memory/delete", {"path": "user.md"})
    assert status == 409 and GONE in body["error"]  # 删过的不能再删

    # 子文件夹里最后一个文件删掉之后，空了的子文件夹一层层跟着删掉，记忆文件夹本身留着
    assert s.browser.post("/api/memory/delete", {"path": "projects/deep/a.md"}) == OK
    assert not (root / "projects").exists() and root.is_dir()
    # 文件夹里还有别的东西（哪怕只是 .DS_Store）：不替你删
    assert s.browser.post("/api/memory/delete", {"path": "notes/b.md"}) == OK
    assert (root / "notes" / ".DS_Store").exists()
    assert shown_memories(s, []) == [] and root.is_dir()


def save_memory(s, path, name, text, **extra):
    modified = extra.pop("modified", None)
    if modified is None:
        modified = s.browser.post("/api/memory/read", {"path": path})[1]["modified"]
    return s.browser.post("/api/memory/save", {"path": path, "name": name, "text": text, "modified": modified, **extra})


def test_editing_a_memory(serve):
    s = serve()
    root = s.config.memory_dir
    write(root / "projects" / "zhifox.md", "# 智狐\n标志是一只小狐狸。\n")

    # 只改内容：名字不变，文件原地换成新的内容，列表跟着更新
    assert save_memory(s, "projects/zhifox.md", "zhifox.md", "# 智狐\n英文名 ZhiFox。\n") == (200, {"ok": True, "path": "projects/zhifox.md"})
    assert (root / "projects" / "zhifox.md").read_text(encoding="utf-8") == "# 智狐\n英文名 ZhiFox。\n"
    assert [item["size"] for item in memory_state(s)] == [len("# 智狐\n英文名 ZhiFox。\n".encode())]

    # 改名字（顺便改内容）：还在原来的子文件夹里，旧名字不在了
    assert save_memory(s, "projects/zhifox.md", "智狐.md", "改过的") == (200, {"ok": True, "path": "projects/智狐.md"})
    assert not (root / "projects" / "zhifox.md").exists()
    assert (root / "projects" / "智狐.md").read_text(encoding="utf-8") == "改过的"
    assert [item["path"] for item in memory_state(s)] == ["projects/智狐.md"]

    # 名字不合格、和别的文件重名、内容太长：都说清楚原因，文件不动
    write(root / "projects" / "别的.md", "别的")
    refused = [
        ("", "文件名不能为空"),
        ("  ", "文件名不能为空"),
        ("a/b.md", "不能有 / 或"),
        ("a\\b.md", "不能有 / 或"),
        (".hidden.md", "不能以 . 开头"),
        ("x" * 201, "最多 200 个字符"),
        ("别的.md", "已经有一个叫 别的.md 的文件了"),
    ]
    for name, error in refused:
        status, body = save_memory(s, "projects/智狐.md", name, "新的")
        assert status == 409 and error in body["error"] and body["conflict"] is False, name
    status, body = save_memory(s, "projects/智狐.md", "智狐.md", "字" * (memories.MAX_READ_BYTES // 3 + 1))
    assert status == 409 and "内容太长了" in body["error"]
    assert (root / "projects" / "智狐.md").read_text(encoding="utf-8") == "改过的"

    # 浏览器给的路径照样逐层检查，出不了记忆文件夹
    write(s.config.data_dir / "secret.md", "不能被改")
    status, body = save_memory(s, "../secret.md", "secret.md", "改掉", modified=0)
    assert status == 409 and GONE in body["error"]
    assert (s.config.data_dir / "secret.md").read_text(encoding="utf-8") == "不能被改"

    # 参数不对
    for bad in ({"modified": True}, {"modified": "1"}, {"force": "yes"}):
        body = {"path": "projects/智狐.md", "name": "智狐.md", "text": "x", "modified": 1.0, **bad}
        assert s.browser.post("/api/memory/save", body)[0] == 400, bad
    assert s.browser.post("/api/memory/save", {"path": "projects/智狐.md", "name": "智狐.md", "modified": 1.0})[0] == 400


def test_saving_a_memory_that_changed_meanwhile_asks_before_overwriting(serve):
    s = serve()
    path = s.config.memory_dir / "user.md"
    write(path, "喜欢简短的回答")
    opened = s.browser.post("/api/memory/read", {"path": "user.md"})[1]["modified"]

    # 你在编辑的时候，助手（或者你在别处）改了这个文件
    write(path, "喜欢简短的回答。住在塞维利亚")
    os.utime(path, (opened + 5, opened + 5))
    status, body = save_memory(s, "user.md", "user.md", "你的版本", modified=opened)
    assert status == 409 and body["conflict"] is True and "再点一次" in body["error"]
    assert path.read_text(encoding="utf-8") == "喜欢简短的回答。住在塞维利亚"  # 没有一声不响地冲掉

    # 确认要覆盖
    assert save_memory(s, "user.md", "user.md", "你的版本", modified=opened, force=True) == (200, {"ok": True, "path": "user.md"})
    assert path.read_text(encoding="utf-8") == "你的版本"


def test_memories_cannot_be_edited_during_a_turn(serve):
    s = serve(reply(tool_use("toolu_1", "run_command", {"command": "true"}), stop_reason="tool_use"), reply(text("好。")))
    write(s.config.memory_dir / "user.md", "喜欢简短的回答")
    s.browser.post("/api/message", {"text": "跑一下"})
    confirm = s.browser.wait_for("confirm")
    status, body = save_memory(s, "user.md", "user.md", "改掉")
    assert status == 409 and "等当前这一轮对话结束" in body["error"]
    assert (s.config.memory_dir / "user.md").read_text(encoding="utf-8") == "喜欢简短的回答"
    s.browser.post("/api/confirm", {"id": confirm["id"], "allow": True})
    s.browser.wait_for("turn_end")
    assert save_memory(s, "user.md", "user.md", "改掉")[0] == 200


def test_showing_one_memory_file(serve, monkeypatch):
    s = serve()
    root = s.config.memory_dir
    write(root / "projects" / "zhifox.md")
    assert s.browser.post("/api/memory/show", {"path": "projects/zhifox.md"}) == (200, {"ok": True, "path": str(root.resolve() / "projects" / "zhifox.md")})
    assert s.revealed == [root.resolve() / "projects" / "zhifox.md"]
    # 记忆文件夹以外的、不存在的：不打开
    for path in ("../settings.json", "/etc/passwd", "nope.md"):
        status, body = s.browser.post("/api/memory/show", {"path": path})
        assert status == 409 and GONE in body["error"], path
    assert s.browser.post("/api/memory/show", {})[0] == 400
    assert len(s.revealed) == 1


def test_memory_paths_cannot_escape_the_memory_folder(serve):
    s = serve()
    root, data = s.config.memory_dir, s.config.data_dir
    write(root / "user.md", "我的记忆")
    write(root / "projects" / "zhifox.md")
    write(root / ".DS_Store", data=b"\x00")
    settings = write(data / "settings.json", "{}")
    env = write(s.config.workspace / ".env", "ANTHROPIC_API_KEY=sk-secret")
    outside = write(data.parent / "outside" / "secret.txt", "外面的秘密")
    os.symlink(outside, root / "link.md")  # 记忆文件夹里一个指向外面的文件链接
    os.symlink(outside.parent, root / "linkdir")  # 和一个指向外面的文件夹链接
    host, token = s.browser.host, s.browser.token

    bad_paths = [
        "../settings.json", "../../work/.env", str(settings), str(env), str(outside), "/etc/passwd",
        ".", "..", "", "a/../user.md", "projects/../user.md", "projects//zhifox.md", "./user.md", "projects/",
        "/user.md", ".DS_Store", "x\\y", "..\\settings.json", "a\x00b", "user.md\x00", "9" * 5000,
    ]
    for action in ("read", "delete"):
        for bad in bad_paths:
            status, body = s.browser.post(f"/api/memory/{action}", {"path": bad})
            assert status == 409 and GONE in body["error"], (action, bad, body)
        for link in ("link.md", "linkdir/secret.txt"):
            status, body = s.browser.post(f"/api/memory/{action}", {"path": link})
            assert status == 409 and "链接" in body["error"], (action, link, body)
            assert "外面的秘密" not in json.dumps(body, ensure_ascii=False)
        # 路径根本不是一段文字，或者没给：参数不对
        for malformed in ({"path": None}, {"path": 5}, {"path": ["user.md"]}, {"path": {"p": 1}}, {}):
            assert s.browser.post(f"/api/memory/{action}", malformed) == (400, {"error": "参数不正确"})
    assert s.browser.post("/api/memory/explode", {"path": "user.md"}) == (400, {"error": "参数不正确"})

    # 没带口令、或者是别的网站发起的请求：和其他接口一样被拒绝
    for action in ("read", "delete"):
        assert request(host, "POST", f"/api/memory/{action}", {"path": "user.md"})[0] == 403
        assert request(host, "POST", f"/api/memory/{action}", {"path": "user.md"}, {"X-Harness-Token": "wrong"})[0] == 403
        evil = {"X-Harness-Token": token, "Origin": "http://evil.example"}
        assert request(host, "POST", f"/api/memory/{action}", {"path": "user.md"}, evil)[0] == 403

    # 外面的东西一样都没动：文件和内容都在，链接本身也还在，记忆也没被删
    assert settings.read_text(encoding="utf-8") == "{}" and env.read_text(encoding="utf-8") == "ANTHROPIC_API_KEY=sk-secret"
    assert outside.read_text(encoding="utf-8") == "外面的秘密"
    assert (root / "link.md").is_symlink() and (root / "linkdir").is_symlink()
    assert (root / "user.md").read_text(encoding="utf-8") == "我的记忆" and (root / ".DS_Store").exists()
    assert (root / "projects" / "zhifox.md").exists()


def test_memories_cannot_be_deleted_during_a_turn(serve):
    s = serve(reply(tool_use("toolu_1", "run_command", {"command": "true"}), stop_reason="tool_use"), reply(text("好。")))
    write(s.config.memory_dir / "user.md", "喜欢简短的回答")
    s.browser.post("/api/message", {"text": "跑一下"})
    confirm = s.browser.wait_for("confirm")  # 这一轮卡在确认上

    # 模型可能正在改这条记忆，所以这时不让删；查看只是读，可以
    status, body = s.browser.post("/api/memory/delete", {"path": "user.md"})
    assert status == 409 and "等当前这一轮对话结束" in body["error"]
    assert (s.config.memory_dir / "user.md").exists()
    status, body = s.browser.post("/api/memory/read", {"path": "user.md"})
    assert status == 200 and body["text"] == "喜欢简短的回答"

    s.browser.post("/api/confirm", {"id": confirm["id"], "allow": True})
    s.browser.wait_for("turn_end")
    assert s.browser.post("/api/memory/delete", {"path": "user.md"}) == OK  # 结束之后就可以了
    assert not (s.config.memory_dir / "user.md").exists()


def test_revealing_the_memory_folder(serve, monkeypatch):
    s = serve(reply(tool_use("toolu_1", "run_command", {"command": "true"}), stop_reason="tool_use"), reply(text("好。")))
    root = s.config.memory_dir
    host = s.browser.host

    # 页面什么都不用给，打开哪个文件夹由服务器定；请求里多带一个路径也没用
    assert s.browser.post("/api/memory/reveal", {}) == (200, {"ok": True, "path": str(root)})
    assert s.browser.post("/api/memory/reveal", {"path": "/etc/passwd", "id": "../.."}) == (200, {"ok": True, "path": str(root)})
    assert s.revealed == [root, root]

    # 文件夹不在了：先建一个空的（只有自己的账号能进），再打开
    shutil.rmtree(root)
    assert s.browser.post("/api/memory/reveal", {}) == (200, {"ok": True, "path": str(root)})
    assert root.is_dir() and stat.S_IMODE(root.stat().st_mode) == 0o700
    assert s.revealed == [root, root, root]

    # 只是打开来看看，对话进行中也能点
    s.browser.post("/api/message", {"text": "跑一下"})
    confirm = s.browser.wait_for("confirm")
    assert s.browser.post("/api/memory/reveal", {})[0] == 200
    assert len(s.revealed) == 4

    # 文件管理器打不开时，把文件夹在哪儿告诉用户
    def broken(path):
        raise OSError("找不到 open 这个命令")

    monkeypatch.setattr(server_module, "reveal_in_file_manager", broken)
    status, body = s.browser.post("/api/memory/reveal", {})
    assert status == 409 and "找不到 open 这个命令" in body["error"] and str(root) in body["error"]
    s.browser.post("/api/confirm", {"id": confirm["id"], "allow": True})
    s.browser.wait_for("turn_end")

    # 没带口令、或者是别的网站发起的请求：什么都不会打开
    monkeypatch.setattr(server_module, "reveal_in_file_manager", s.revealed.append)
    assert request(host, "POST", "/api/memory/reveal", {})[0] == 403
    evil = {"X-Harness-Token": s.browser.token, "Origin": "http://evil.example"}
    assert request(host, "POST", "/api/memory/reveal", {}, evil)[0] == 403
    assert len(s.revealed) == 4


def test_changes_made_outside_show_up_when_the_page_asks(serve):
    # 你在访达里删了、改了记忆文件，程序不会知道。页面刚连上、或者切回窗口时来问一声，变了才发新的状态
    s = serve()
    root = s.config.memory_dir

    def refresh(data: dict | None = None) -> dict | None:
        """请服务器看一眼记忆文件夹。发了新的状态就返回它，没发返回 None。"""
        last_id = s.ui.events_after(0, timeout=0)[-1]["id"]
        assert s.browser.post("/api/memory/refresh", data or {}) == (200, {"ok": True})
        newer = [event for event in s.ui.events_after(last_id, timeout=0) if event["type"] == "state"]
        if not newer:
            return None
        while True:  # 等"浏览器"也收到它
            state = s.browser.wait_for("state")
            if state["id"] > last_id:
                return state

    assert refresh() is None  # 没变：什么都不发，切换多少次窗口，日志都不会变长

    write(root / "在访达里加的.md", "手写的")
    assert [item["path"] for item in refresh()["memories"]] == ["在访达里加的.md"]
    assert refresh() is None  # 已经发过了，不再重复

    (root / "在访达里加的.md").unlink()
    assert refresh({"path": "../settings.json"})["memories"] == []  # 请求里带什么都不看
    assert request(s.browser.host, "POST", "/api/memory/refresh", {})[0] == 403


def test_listing_memories_directly(tmp_path, monkeypatch):
    assert memories.list_files(tmp_path / "missing") == []  # 还不存在的文件夹就是空的
    write(tmp_path / "not-a-folder", "x")
    assert memories.list_files(tmp_path / "not-a-folder") == []

    # 文件再多，也只列到上限为止，免得每次发状态都扫一大片
    root = tmp_path / "memories"
    monkeypatch.setattr(memories, "MAX_LISTED", 5)
    for index in range(8):
        write(root / f"{index}.md")
    assert [item["path"] for item in memories.list_files(root)] == ["0.md", "1.md", "2.md", "3.md", "4.md"]

    # 太深的子文件夹不往下找
    monkeypatch.setattr(memories, "MAX_DEPTH", 2)
    deep = tmp_path / "deep"
    write(deep / "a" / "one.md")
    write(deep / "a" / "b" / "two.md")
    assert [item["path"] for item in memories.list_files(deep)] == ["a/one.md"]


# ---- 自动允许 -------------------------------------------------------------
# 用户可以放宽"要不要每次都问"：ask 每次都问（默认），edits 改工作目录里的文件不用问，all 全部自动允许。


def set_approval(s, mode: str) -> dict:
    """更改"要不要每次都问"，返回更改之后的状态。"""
    last_id = s.ui.events_after(0, timeout=0)[-1]["id"]
    assert s.browser.post("/api/approval", {"mode": mode}) == (200, {"ok": True})
    while True:
        state = s.browser.wait_for("state")
        if state["id"] > last_id:
            return state


def without_states(browser: Browser) -> list[str]:
    return [type for type in browser.types() if type != "state"]


def test_approval_starts_at_ask_and_only_accepts_the_three_modes(serve):
    s = serve()
    assert latest_state(s.browser)["approval"] == "ask"

    for mode in ("edits", "all", "ask", "all"):
        assert set_approval(s, mode)["approval"] == mode  # 页面马上收到新的状态
        assert s.app.agent.permissions.mode == mode

    # 别的值一律不收，现在的模式不变，也不发新的状态
    log = s.ui.events_after(0, timeout=0)
    for bad in (
        {"mode": "everything"}, {"mode": "ALL"}, {"mode": " all"}, {"mode": ""}, {"mode": 1}, {"mode": True},
        {"mode": None}, {"mode": ["all"]}, {"mode": {"all": 1}}, {},
    ):
        assert s.browser.post("/api/approval", bad) == (400, {"error": "参数不正确"})
    assert s.app.agent.permissions.mode == "all" and s.ui.events_after(0, timeout=0) == log

    # 没带口令的、别的网站发起的请求：和其他接口一样，根本到不了这里
    set_approval(s, "ask")
    host, token = s.browser.host, s.browser.token
    assert request(host, "POST", "/api/approval", {"mode": "all"})[0] == 403
    evil = {"X-Harness-Token": token, "Origin": "http://evil.example"}
    assert request(host, "POST", "/api/approval", {"mode": "all"}, evil)[0] == 403
    assert s.app.agent.permissions.mode == "ask"


def test_the_approval_mode_is_forgotten_when_the_program_restarts(serve):
    s = serve()
    set_approval(s, "all")
    act(s, "/api/model", {"provider": "anthropic", "model": "claude-sonnet-5-5"})  # 让设置文件写一次

    # 设置文件里没有这一项：放开之后忘了关，比每次多点一下危险得多，所以故意不记
    saved = json.loads(s.config.settings_file.read_text(encoding="utf-8"))
    assert set(saved) == {"provider", "model", "models", "prices", "language"}
    restarted = serve()  # 同一个数据目录：相当于把程序关掉再打开
    assert latest_state(restarted.browser)["approval"] == "ask"
    assert restarted.app.agent.permissions.mode == "ask"
    assert latest_state(restarted.browser)["model"] == "claude-sonnet-5-5"  # 别的设置是记住了的


def test_auto_allowed_calls_are_marked_live_and_when_the_conversation_is_reopened(serve):
    s = serve(
        reply(
            tool_use("toolu_1", "run_command", {"command": "touch made.txt"}),
            tool_use("toolu_2", "todo_list", {}),
            stop_reason="tool_use",
        ),
        reply(text("建好了。")),
        reply(text("这是另一段。")),
    )
    set_approval(s, "all")
    first = say(s, "建个文件")["conversation"]

    assert (s.config.workspace / "made.txt").exists()
    assert s.browser.of_type("confirm") == []  # 没有弹出确认卡片
    # "自动允许"紧跟在它说的那次工具调用后面、结果之前。本来就不用问的操作（查看待办）没有这个标记
    shown = ["user", "turn_start", "tool_call", "auto_allowed", "tool_result", "tool_call", "tool_result", "text", "block_end"]
    assert without_states(s.browser) == [*shown, "turn_end"]
    (mark,) = s.browser.of_type("auto_allowed")
    assert set(mark) == {"id", "type"}  # 不带任何数据：说的就是它前面那一次调用

    # 存进了会话文件：以后翻看记录时，看得出哪些操作是没问就做了的
    saved = saved_file(s.config, first)["events"]
    assert kinds(saved) == shown and {"type": "auto_allowed"} in saved
    act(s, "/api/new")
    say(s, "另一段")
    replayed, _ = open_conversation(s, first)
    assert kinds(replayed) == [*shown, "turn_end"]


def test_approval_can_be_changed_while_a_confirmation_is_waiting(serve):
    s = serve(
        reply(
            tool_use("toolu_1", "run_command", {"command": "touch first.txt"}),
            tool_use("toolu_2", "run_command", {"command": "touch second.txt"}),
            stop_reason="tool_use",
        ),
        reply(text("好的。")),
    )
    s.browser.post("/api/message", {"text": "建两个文件"})
    card = s.browser.wait_for("confirm")

    # 对话进行中也可以改（换模型、填密钥这些设置这时候是改不了的）
    assert s.browser.post("/api/model", {"provider": "anthropic", "model": "claude-sonnet-5-5"})[0] == 409
    assert set_approval(s, "all")["approval"] == "all"
    # 已经显示出来、正在等答复的那一张卡片不受影响：它还在等，什么都没有执行
    time.sleep(0.2)
    assert s.browser.of_type("confirm_done") == [] and not (s.config.workspace / "first.txt").exists()
    assert s.browser.post("/api/confirm", {"id": card["id"], "allow": False, "feedback": "这个不要"})[0] == 200
    s.browser.wait_for("turn_end")

    # 第一个按用户的答复没有执行；第二个是改了之后才轮到的，不再问
    assert not (s.config.workspace / "first.txt").exists() and (s.config.workspace / "second.txt").exists()
    assert without_states(s.browser) == [
        "user", "turn_start", "tool_call", "confirm", "confirm_done", "tool_result",
        "tool_call", "auto_allowed", "tool_result", "text", "block_end", "turn_end",
    ]
    first, second = last_results(SimpleNamespace(messages=s.api.requests[1]["messages"]))
    assert first["is_error"] is True and "这个不要" in first["content"] and "is_error" not in second

    # 改回"每次都问"之后，下一次又会问了
    set_approval(s, "ask")
    assert s.app.agent.permissions.review("run_command", {"command": "true"}) is not None
    assert not s.app.agent.permissions.auto_allows("run_command", {"command": "true"})


def test_edits_mode_in_the_page_still_asks_before_running_commands(serve):
    s = serve(
        reply(
            tool_use("toolu_1", "str_replace_based_edit_tool", {"command": "create", "path": "a.txt", "file_text": "内容"}),
            tool_use("toolu_2", "run_command", {"command": "touch b.txt"}),
            stop_reason="tool_use",
        ),
        reply(text("好的。")),
    )
    set_approval(s, "edits")
    s.browser.post("/api/message", {"text": "写个文件，再执行个命令"})
    card = s.browser.wait_for("confirm")  # 等到的第一张卡片是命令的：写文件那一步没有问

    assert "touch b.txt" in card["description"]
    assert (s.config.workspace / "a.txt").read_text(encoding="utf-8") == "内容"
    assert s.browser.post("/api/confirm", {"id": card["id"], "allow": True})[0] == 200
    s.browser.wait_for("turn_end")
    assert without_states(s.browser) == [
        "user", "turn_start", "tool_call", "auto_allowed", "tool_result",
        "tool_call", "confirm", "confirm_done", "tool_result", "text", "block_end", "turn_end",
    ]


# ---- 界面语言 -------------------------------------------------------------------
# 换语言不刷新页面：服务器记下新的选择、发一次新的状态，页面在原地把文字换掉。
# 服务器发给页面的话（提示、确认卡片上的说明）除了译好的文字，还带着原文和填进去的值，页面换语言时照着它们重译。


def page_language(s) -> dict:
    """页面里塞进去的那份翻译数据。"""
    page = request(s.browser.host, "GET", f"/?token={s.browser.token}")[1]["text"]
    return json.loads(page.split('<script type="application/json" id="i18n">')[1].split("</script>")[0])


def test_switching_the_language_keeps_the_page_and_is_remembered(serve, make_config):
    s = serve(reply(text("一")))
    assert latest_state(s.browser)["language"] == "zh"
    data = page_language(s)
    assert data["language"] == "zh" and set(data["tables"]) == {"en", "es"}
    assert data["tables"]["en"]["新对话"] == "New conversation"

    say(s, "第一句")
    resets = len(s.browser.of_type("reset"))
    state = act(s, "/api/language", {"language": "en"})
    assert state["language"] == "en"
    assert len(s.browser.of_type("reset")) == resets  # 没有清空页面：对话原样留着，只是换了语言
    assert json.loads(s.config.settings_file.read_text(encoding="utf-8"))["language"] == "en"
    assert page_language(s)["language"] == "en"  # 之后打开的页面直接就是英文

    for bad in ({"language": "fr"}, {"language": 5}, {}):
        assert s.browser.post("/api/language", bad) == (400, {"error": "Invalid parameters"})  # 出错提示也已经是英文了

    # 重启之后还是英文
    app = build_app(s.config, WebUI())
    assert app.settings.language == "en"


def test_messages_from_the_program_carry_their_original_text(serve, monkeypatch):
    s = serve(
        reply(tool_use("toolu_1", "run_command", {"command": "ls {x}"}), stop_reason="tool_use"),
        reply(text("好。")),
        others=[chat("嗨。")],
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    s.browser.post("/api/message", {"text": "跑一下"})
    confirm = s.browser.wait_for("confirm")
    # 确认卡片：页面显示译好的文字，同时知道原文和填进去的值；值里的花括号原样保留，不会被当成要填的空
    assert confirm["description"] == "执行命令：\n\n    $ ls {x}"
    assert confirm["key"] == "执行命令：\n\n    $ {command}" and confirm["values"] == {"command": "ls {x}"}
    s.browser.post("/api/confirm", {"id": confirm["id"], "allow": True})
    s.browser.wait_for("turn_end")

    # 换成英文以后，程序新发出的提示是英文的，也带着原文
    act(s, "/api/language", {"language": "en"})
    act(s, "/api/model", {"provider": "deepseek", "model": "deepseek-flash"})
    notice = [event for event in s.browser.of_type("notice") if event.get("key", "").startswith("已换成")][-1]
    assert notice["text"] == "Switched to DeepSeek · deepseek-flash. It's a different provider, so a new conversation starts."
    assert notice["key"] == "已换成 {provider} · {model}。换了一家，开始新对话。"
    assert notice["values"] == {"provider": "DeepSeek", "model": "deepseek-flash"}

    # 保存下来的会话里也留着原文：以后换了语言再打开，页面照样能把这些话换成新的语言
    first = next(item for item in latest_state(s.browser)["conversations"] if item["title"] == "跑一下")
    saved = saved_file(s.config, first["id"])
    (entry,) = [event for event in saved["events"] if event["type"] == "confirm"]
    assert entry["key"] == "执行命令：\n\n    $ {command}" and entry["values"] == {"command": "ls {x}"}
    replayed, _ = open_conversation(s, first["id"])
    (card,) = [event for event in replayed if event["type"] == "confirm"]
    assert card["key"] == entry["key"] and card["values"] == entry["values"]


# ---- 附件 -----------------------------------------------------------------


def send_files(s, *attachments, text: str = "看看这些") -> tuple[int, dict]:
    return s.browser.post("/api/message", {"text": text, "attachments": list(attachments)})


def test_attachments_are_checked_before_anything_is_sent(serve):
    s = serve()
    png_header = b"\x89PNG\r\n\x1a\n"
    too_big_png = b64(png_header + bytes(MAX_IMAGE_BYTES - len(png_header) + 1))
    bad_encoding = "附件“截图.png”的内容编码不正确（应该是 base64）。"
    unsupported_image = "图片“截图.png”的格式不支持，只能是 PNG、JPEG、GIF 或 WebP。"
    malformed = "附件的格式不正确。"
    rejected = [
        ([image()] * 11, "一条消息最多带 10 个附件。"),
        ([image(data="不是 base64!")], bad_encoding),
        ([image(data=PNG[:-1])], bad_encoding),  # 少了结尾的补齐
        ([image(data=PNG[:40] + "\n" + PNG[40:])], bad_encoding),  # 夹着换行：API 同样不接受
        ([image(data="")], "附件“截图.png”是空的。"),
        ([image(media_type="image/bmp")], unsupported_image),
        ([image(data=b64(b"BM" + bytes(64)))], unsupported_image),  # 说是 PNG，内容其实是 BMP
        ([image(data=too_big_png)], "图片“截图.png”太大了，一张图片最大 7.5 MB。"),
        ([pdf(data=b64(b"%PDF-" + bytes(MAX_PDF_BYTES - 4)))], "PDF“报告.pdf”太大了，一个 PDF 最大 25 MB。"),
        ([pdf(data=b64(b"just some text"))], "“报告.pdf”不是有效的 PDF 文件。"),
        ([text_file("长.txt", "字" * (MAX_TEXT_CHARS + 1))], "文本文件“长.txt”太长了，一个文件最多 40 万个字符。"),
        ([{**pdf(), "media_type": "image/png"}], malformed),
        ([{"kind": "video", "name": "a.mp4", "data": PNG}], malformed),
        ([image(name="")], malformed),
        ([image(name="  /  ")], malformed),  # 只有路径分隔符，去掉以后什么都不剩
        ([text_file(text=None)], malformed),
        (["截图.png"], malformed),
    ]
    for attachments, error in rejected:
        assert send_files(s, *attachments) == (400, {"error": error}), error
    assert s.browser.post("/api/message", {"text": "看看", "attachments": "截图.png"}) == (400, {"error": malformed})
    # 有一个不合格，整条消息都不发：哪怕别的附件都没问题
    assert send_files(s, image(), pdf(data="坏的"))[0] == 400
    # 没写字也没有附件，还是不行
    assert s.browser.post("/api/message", {"text": " ", "attachments": []}) == (400, {"error": "消息不能为空，也不能太长"})

    assert s.api.requests == [] and s.app.agent.messages == []
    assert "user" not in s.browser.types()  # 一轮都没有开始


def test_a_message_with_attachments_from_the_page_to_the_model_and_back(serve):
    s = serve(reply(text("一张截图、一张照片、一份报告和一些笔记。")), reply(text("另一段。")))
    photo = image("拍的照片.png", data=b64(JPEG_BYTES), thumb=THUMB)  # 扩展名说是 PNG，内容其实是 JPEG
    notes = text_file("C:\\Users\\me\\笔记.md")  # 带着路径的名字
    status, _ = send_files(s, image("季度报表截图.png", thumb=THUMB), photo, pdf(thumb=THUMB), notes, text="  ")
    assert status == 200
    s.browser.wait_for("turn_end")

    # 页面收到的是概要：名字、类型、大小、预览图，没有内容本身
    (user,) = s.browser.of_type("user")
    assert user["text"] == ""
    assert user["attachments"] == [
        {"kind": "image", "name": "季度报表截图.png", "media_type": "image/png", "size": len(PNG_BYTES), "thumb": THUMB},
        {"kind": "image", "name": "拍的照片.png", "media_type": "image/jpeg", "size": len(JPEG_BYTES), "thumb": THUMB},
        {"kind": "pdf", "name": "报告.pdf", "media_type": "application/pdf", "size": len(PDF_BYTES)},  # PDF 不要预览图
        {"kind": "text", "name": "笔记.md", "media_type": "text/plain", "size": len("第一行\n第二行")},  # 路径去掉了
    ]
    # 发给模型的是完整的内容，而且以文件的真实格式为准：说的格式和实际的对不上，API 会拒绝整个请求
    content = s.api.requests[0]["messages"][0]["content"]
    assert [block["type"] for block in content] == ["image", "image", "document", "text", "text"]
    assert content[0]["source"]["data"] == PNG
    assert content[1]["source"] == {"type": "base64", "media_type": "image/jpeg", "data": b64(JPEG_BYTES)}
    assert content[2]["source"]["data"] == PDF and content[2]["title"] == "报告.pdf"
    assert content[3]["text"].startswith('<attachment name="笔记.md">')
    # 没写字：新会话用第一个附件的名字当标题
    state = latest_state(s.browser)
    first = state["conversation"]
    assert state["title"] == "季度报表截图.png"

    # 存下来的会话：事件里只有概要，完整的内容只在对话历史里
    saved = saved_file(s.config, first)
    assert saved["title"] == "季度报表截图.png"
    assert saved["events"][0] == {"type": "user", "text": "", "attachments": user["attachments"]}
    assert PNG not in json.dumps(saved["events"]) and PNG in json.dumps(saved["messages"])

    # 刷新页面：附件跟着用户的话一起重放
    reloaded = Browser(s.url)
    reloaded.wait_for("turn_end")
    assert reloaded.of_type("user") == [user]

    # 换一段再打开它：附件也在。会话文件要是被改过，不认识的项和不合格的预览图不会被放进页面
    act(s, "/api/new")
    say(s, "另一段")
    path = s.config.conversations_dir / f"{first}.json"
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["events"][0]["attachments"] += [
        {"kind": "exe", "name": "x.exe", "media_type": "x", "size": 1},
        {"kind": "image", "name": "坏的.png", "media_type": "image/png", "size": True},
        {"kind": "image", "name": "x.png", "media_type": "image/png", "size": 1, "thumb": "javascript:alert(1)"},
    ]
    path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
    replayed, _ = open_conversation(s, first)
    (again,) = [event for event in replayed if event["type"] == "user"]
    survivor = {"kind": "image", "name": "x.png", "media_type": "image/png", "size": 1}
    assert again["attachments"] == [*user["attachments"], survivor]
    assert s.app.agent.messages[0]["content"][0]["source"]["data"] == PNG  # 接着聊时，模型还看得到这些附件

    # 搜索能按附件的名字找到这段会话，算作"你说的话"；附件的内容（base64）和预览图不会被搜到
    (found,) = search(s, "笔记")
    assert found["id"] == first
    assert found["snippet"] == {"kind": "user", "text": "笔记.md", "marks": [[0, 2]], "find": "笔记"}
    assert [item["id"] for item in search(s, "拍的照片")] == [first]
    assert search(s, PNG[:12]) == [] and search(s, "base64") == []


def test_bad_thumbnails_are_dropped_and_the_message_still_goes(serve):
    s = serve(reply(text("收到。")))
    thumbs = [
        THUMB,
        "javascript:alert(1)",
        "data:image/svg+xml;base64,PHN2Zz4=",  # SVG 里可以带脚本，不要
        'data:image/png;base64,AAAA"onerror="x',
        "data:image/png;base64," + "A" * MAX_THUMB_CHARS,  # 太长
        42,
    ]
    status, _ = send_files(s, *(image(f"{index}.png", thumb=thumb) for index, thumb in enumerate(thumbs)))
    assert status == 200
    s.browser.wait_for("turn_end")
    (user,) = s.browser.of_type("user")
    assert [item.get("thumb") for item in user["attachments"]] == [THUMB, None, None, None, None, None]
    assert user["text"] == "看看这些"
    assert len(s.api.requests[0]["messages"][0]["content"]) == len(thumbs) + 2  # 图片一张不少地发了出去


def test_an_office_file_reaches_the_model_as_text_and_the_page_as_a_file(serve):
    s = serve(reply(text("预算是 1284.5。")))
    sheet = '<row r="1"><c r="A1" t="inlineStr"><is><t>预算</t></is></c><c r="B1"><v>1284.5</v></c></row>'
    assert send_files(s, document("预算.xlsx", xlsx([("第一季度", sheet)])), text="多少钱？")[0] == 200
    s.browser.wait_for("turn_end")
    words = "## 工作表：第一季度\n预算\t1284.5"
    (user,) = s.browser.of_type("user")
    assert user["attachments"] == [{"kind": "text", "name": "预算.xlsx", "media_type": "text/plain", "size": len(words)}]
    content = s.api.requests[0]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": f'<attachment name="预算.xlsx" note="{NOTE_OFFICE}">\n{words}\n</attachment>'}
    # 读不了的说清楚原因，一轮都不开始
    error = "“旧.xls”打不开：它可能设了密码，或者是旧版的 Office 格式（.doc、.xls、.ppt）。去掉密码、或者另存为 .docx、.xlsx、.pptx 或 PDF 再发。"
    assert send_files(s, document("旧.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(64))) == (400, {"error": error})
    assert len(s.api.requests) == 1


def test_a_larger_preview_is_kept_for_opening_the_image(serve, tmp_path):
    # 点开缩略图看的大图：完整的原图只在发给模型的历史里，页面拿不到，所以页面另外送一张大一些的预览来存着。
    # 它和小缩略图一样只能是图片的 data: 地址；只是长度上限大一些
    s = serve(reply(text("收到。")))
    preview = "data:image/jpeg;base64," + "A" * (MAX_THUMB_CHARS + 4)  # 比缩略图的上限长，但在预览图的上限之内
    too_long = "data:image/jpeg;base64," + "A" * MAX_PREVIEW_CHARS
    status, _ = send_files(s, image("大.png", thumb=THUMB, preview=preview), image("太大.png", preview=too_long),
                           image("坏.png", preview="javascript:alert(1)"))
    assert status == 200
    s.browser.wait_for("turn_end")
    (user,) = s.browser.of_type("user")
    assert [item.get("preview") for item in user["attachments"]] == [preview, None, None]
    assert all("preview" not in str(block) for block in s.api.requests[0]["messages"][0]["content"])  # 不发给模型
    # 存下来的会话重新打开时，预览图还在
    assert from_saved(user["attachments"])[0]["preview"] == preview


def test_only_messages_may_carry_a_large_body(serve):
    s = serve(reply(text("好大一张图。")))
    big = PNG_BYTES + bytes(1_500_000)  # 加上 base64 的膨胀，请求体有 2MB 多，超过了别的请求的上限
    status, _ = send_files(s, image("大图.png", data=b64(big)))
    assert status == 200
    s.browser.wait_for("turn_end")
    assert s.api.requests[0]["messages"][0]["content"][0]["source"]["data"] == b64(big)

    # 别的请求还是最多 1MB；回答 413 之前把请求体读掉了，页面能收到这个回答
    status, body = s.browser.post("/api/search", {"query": "x" * (MAX_BODY_BYTES + 1)})
    assert (status, body) == (413, {"error": "请求太大：一次最多发送约 1MB 的内容"})

    # 发消息也有上限
    connection = http.client.HTTPConnection(s.browser.host, timeout=30)
    headers = {"X-Harness-Token": s.browser.token, "Origin": f"http://{s.browser.host}", "Content-Type": "application/json"}
    connection.request("POST", "/api/message", body=b" " * (MAX_MESSAGE_BODY_BYTES + 1), headers=headers)
    response = connection.getresponse()
    assert response.status == 413
    assert json.loads(response.read()) == {"error": "请求太大：一条消息连同附件最多发送约 64MB 的内容"}
    connection.close()
    assert len(s.api.requests) == 1


def test_each_provider_says_what_it_can_take_and_the_rest_is_refused(serve, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    s = serve(others=[chat("读完了。")])
    flags = {item["id"]: (item["vision"], item["pdf"]) for item in latest_state(s.browser)["providers"]}
    assert flags == {"anthropic": (True, True), "openai": (True, True), "gemini": (True, False), "deepseek": (False, False)}

    act(s, "/api/model", {"provider": "deepseek", "model": "deepseek-flash"})
    refused = (400, {"error": "当前的模型（DeepSeek）看不了图片。可以在设置里换成 Claude、GPT 或 Gemini。"})
    assert send_files(s, image()) == refused
    no_pdf = "当前的模型（DeepSeek）读不了 PDF。可以在设置里换成 Claude 或 GPT，或者把需要的文字复制进来。"
    assert send_files(s, pdf()) == (400, {"error": no_pdf})
    assert s.other_api.requests == [] and "user" not in s.browser.types()  # 一轮都没有开始
    act(s, "/api/model", {"provider": "openai", "model": "gpt-6-luna"})

    # 文本文件哪一家都能收
    assert send_files(s, text_file())[0] == 200
    s.browser.wait_for("turn_end")
    (request,) = s.other_api.requests
    assert request["model"] == "gpt-6-luna" and request["messages"][1]["content"][0]["type"] == "text"
