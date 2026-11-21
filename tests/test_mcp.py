"""MCP 客户端的测试：握手（两代协议）、翻页、结果、取消、超时、工具列表变化、崩溃重启、快照、权限、配置、导入。

和别的测试一样，不连任何真的服务：MCP 服务器是 tests/fake_mcp.py（stdio 的子进程，或者本机的 HTTP 服务器），
模型是 tests/fake_api.py、tests/fake_openai.py。每个测试结束时把启动过的服务器都关掉，不留进程。
"""

from __future__ import annotations

import base64
import json
import re
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import RecordingUI
from fake_api import reply, text, tool_use
from fake_mcp import PNG, FakeMcpHttp
from fake_openai import call as openai_call
from fake_openai import chat
from modes import has_mode

import harness.mcp.client as mcp_client
import harness.mcp.manager as mcp_manager
from harness.cli import handle_command
from harness.imports.places import Env
from harness.mcp import ConfigError, McpConfig, McpManager, Snapshot, importers
from harness.mcp.bridge import claude_schema, exposed_name, portable_schema, render
from harness.mcp.client import McpClient
from harness.mcp.config import (
    MASK,
    display,
    expand,
    from_form,
    hint,
    mask_args,
    mask_text,
    normalize,
)
from harness.mcp.http import header_value
from harness.mcp.log import ServerLog
from harness.permissions import Permissions
from harness.tools import ToolError
from harness.tools.files import TOOL_NAME as FILE_TOOL
from harness.ui import TerminalUI

FAKE = Path(__file__).parent / "fake_mcp.py"
NAME_RULE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def stdio(*args: str, **extra) -> dict:
    """一个用假服务器的 stdio 配置。"""
    return {"command": sys.executable, "args": [str(FAKE), *args], **extra}


def write_config(path: Path, servers: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def settle(manager: McpManager, timeout: float = 20) -> list[dict]:
    """等所有服务器都不再是"连接中"（连上了，或者失败了），返回那时的状态。"""
    deadline = time.monotonic() + timeout
    while True:
        servers = manager.status(started=False)
        if all(server["status"] != "connecting" for server in servers):
            return servers
        assert time.monotonic() < deadline, f"服务器一直在连接中：{servers}"
        time.sleep(0.05)


def wait_until(check, timeout: float = 10, what: str = "条件") -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, f"等不到{what}"
        time.sleep(0.05)


def status_of(manager: McpManager, name: str, started: bool = False) -> dict:
    return next(server for server in manager.status(started) if server["name"] == name)


def records(path: Path) -> list[dict]:
    """假服务器收到的每一条消息（--record 写下的）。"""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def close_managers(monkeypatch):
    """记下每个启动过的管理者，测试结束时都关掉：不能让假服务器的进程留在后台。"""
    started: list[McpManager] = []
    original = McpManager.start

    def start(self):
        started.append(self)
        original(self)

    monkeypatch.setattr(McpManager, "start", start)
    monkeypatch.setattr(mcp_manager, "RESTART_DELAY", 0.1)
    yield
    for manager in started:
        manager.shutdown()


@pytest.fixture
def manager(tmp_path):
    """按给出的配置启动一个管理者，等它们都连好。"""

    def make(servers: dict, wait: bool = True) -> McpManager:
        path = tmp_path / "data" / "mcp.json"
        write_config(path, servers)
        made = McpManager(McpConfig(path))
        made.start()
        if wait:
            settle(made)
        return made

    return make


# ---- 握手：两代协议 ---------------------------------------------------------------


def test_modern_server_is_found_with_discover_and_every_request_carries_meta(manager, tmp_path):
    log = tmp_path / "modern.jsonl"
    m = manager({"fake": stdio("--era", "modern", "--record", str(log))})
    assert status_of(m, "fake")["status"] == "ready"
    assert status_of(m, "fake")["tools"] == 8
    m.call("fake", "echo", {"text": "hi"})

    received = records(log)
    assert received[0]["method"] == "server/discover"  # 新一代：先问一句，不握手
    assert not any(message.get("method") == "initialize" for message in received)
    requests = [message for message in received if "id" in message and "method" in message]
    assert {message["method"] for message in requests} >= {"server/discover", "tools/list", "tools/call"}
    for message in requests:
        meta = message["params"]["_meta"]
        assert meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
        assert meta["io.modelcontextprotocol/clientInfo"]["name"] == "ZhiFox"
        assert meta["io.modelcontextprotocol/clientCapabilities"] == {}  # 我们什么能力都不声明
    detail = m.detail("fake")
    assert (detail["era"], detail["version"]) == ("modern", "2026-07-28")
    assert detail["server_info"] == {"name": "fake-modern", "version": "2.0"}


def test_legacy_server_gets_the_initialize_handshake_in_order(manager, tmp_path):
    log = tmp_path / "legacy.jsonl"
    m = manager({"old": stdio("--era", "legacy", "--record", str(log))})
    assert status_of(m, "old")["status"] == "ready"
    assert m.call("old", "echo", {"text": "老一代"}).text == "老一代"

    methods = [message.get("method") for message in records(log)]
    # 先试新一代（被拒绝），再握手：initialize → initialized → 之后才是正常的请求
    assert methods[0] == "server/discover"
    assert methods.index("initialize") < methods.index("notifications/initialized") < methods.index("tools/list")
    initialize = next(message for message in records(log) if message.get("method") == "initialize")
    assert initialize["params"]["protocolVersion"] == "2025-11-25"
    assert initialize["params"]["capabilities"] == {}
    tools_list = next(message for message in records(log) if message.get("method") == "tools/list")
    assert "_meta" not in (tools_list.get("params") or {})  # 老一代的请求不带新一代的自我介绍
    assert (m.detail("old")["era"], m.detail("old")["version"]) == ("legacy", "2025-11-25")


def test_dual_era_server_is_spoken_to_in_the_modern_way(manager):
    m = manager({"both": stdio("--era", "dual")})
    assert m.detail("both")["era"] == "modern"


def test_unsupported_version_with_only_unknown_versions_fails_clearly(manager):
    m = manager({"future": stdio("--era", "modern", "--supported", "2099-01-01")})
    row = status_of(m, "future")
    assert row["status"] == "failed"
    assert "2099-01-01" in row["error"] and "不认识" in row["error"]


def test_unsupported_modern_version_with_a_legacy_one_falls_back_to_the_handshake(manager):
    # 回了"不支持这个版本"，但它列出的版本里有老一代的：按老一代握手
    m = manager({"mixed": stdio("--era", "dual", "--supported", "2099-01-01")})
    assert status_of(m, "mixed")["status"] == "ready"
    assert m.detail("mixed")["era"] == "legacy"


def test_a_silent_legacy_server_is_reached_after_a_short_probe(manager, monkeypatch):
    # 有的老一代服务器在握手之前收到不认识的请求，干脆不回答。等一小会儿没回音，就改走握手
    monkeypatch.setattr(mcp_client, "PROBE_SECONDS", 0.3)
    m = manager({"quiet": stdio("--era", "legacy", "--silent")})
    assert status_of(m, "quiet")["status"] == "ready"
    assert m.detail("quiet")["era"] == "legacy"


def test_a_slow_starting_modern_server_still_ends_up_modern(manager, monkeypatch):
    # 启动很慢（npx 第一次要下载）的新一代服务器：没等到回音就发了 initialize，它后来回答了 discover，按新一代来
    monkeypatch.setattr(mcp_client, "PROBE_SECONDS", 0.2)
    m = manager({"slowpoke": stdio("--era", "modern", "--delay", "1")})
    assert status_of(m, "slowpoke")["status"] == "ready"
    assert m.detail("slowpoke")["era"] == "modern"


def test_a_missing_command_fails_with_a_helpful_message(manager):
    m = manager({"typo": {"command": "definitely-not-a-command-xyz"}})
    row = status_of(m, "typo")
    assert row["status"] == "failed" and "definitely-not-a-command-xyz" in row["error"] and "which" in row["error"]
    assert any("连接失败" in line["text"] for line in m.detail("typo")["log"])


def test_the_server_stderr_is_kept_as_a_log(manager):
    m = manager({"fake": stdio()})
    lines = [line for line in m.detail("fake")["log"] if line["source"] == "stderr"]
    assert any("fake MCP server (modern) ready" in line["text"] for line in lines)


def test_http_servers_of_both_eras(manager):
    modern, legacy = FakeMcpHttp("modern").start(), FakeMcpHttp("legacy", sse=True, token="tok-1234567890abcdef").start()
    try:
        m = manager({
            "web": {"type": "http", "url": modern.url},
            "old": {"type": "http", "url": legacy.url, "headers": {"Authorization": "Bearer tok-1234567890abcdef"}},
        })
        assert [status_of(m, name)["status"] for name in ("web", "old")] == ["ready", "ready"]
        assert m.call("web", "echo", {"text": "a"}).text == "a"
        assert m.call("old", "echo", {"text": "b"}).text == "b"  # 回答是 SSE，里面先有进度通知，还有服务器反过来问的 ping

        # 新一代：请求头里抄着协议版本和方法名、工具名；没有会话编号
        call = next(headers for method, headers, body in modern.requests if body and body.get("method") == "tools/call")
        assert call["MCP-Protocol-Version"] == "2026-07-28" and call["Mcp-Method"] == "tools/call" and call["Mcp-Name"] == "echo"
        assert "Mcp-Session-Id" not in call
        # 老一代：握手之后每个请求都带着会话编号和选定的版本；服务器问的 ping 我们回答了
        later = [headers for method, headers, body in legacy.requests if body and body.get("method") == "tools/call"]
        assert later[0]["Mcp-Session-Id"] in legacy.sessions and later[0]["MCP-Protocol-Version"] == "2025-11-25"
        wait_until(lambda: any(body and body.get("id") == "srv-1" and "result" in body for _, _, body in legacy.requests), what="对 ping 的回答")
    finally:
        modern.stop()
        legacy.stop()


def test_http_without_the_token_explains_how_to_log_in(manager):
    locked = FakeMcpHttp("modern", token="needed-token-abcdefgh").start()
    try:
        m = manager({"locked": {"type": "http", "url": locked.url}})
        row = status_of(m, "locked")
        assert row["status"] == "failed" and "401" in row["error"] and "Authorization" in row["error"]
    finally:
        locked.stop()


def test_http_server_that_is_not_there(manager):
    m = manager({"gone": {"type": "http", "url": "http://127.0.0.1:9/mcp"}})
    assert status_of(m, "gone")["status"] == "failed"


def test_legacy_http_session_expiry_is_handled_by_shaking_hands_again(manager):
    legacy = FakeMcpHttp("legacy").start()
    try:
        m = manager({"old": {"type": "http", "url": legacy.url}})
        legacy.sessions.clear()  # 服务器把会话忘了：之后带着旧编号来的请求得到 404
        assert m.call("old", "echo", {"text": "again"}).text == "again"
        assert sum(1 for _, _, body in legacy.requests if body and body.get("method") == "initialize") == 2
    finally:
        legacy.stop()


# ---- 工具列表：翻页、检查 ----------------------------------------------------------


def test_tools_are_collected_across_pages(manager):
    m = manager({"paged": stdio("--page-size", "3")})
    assert status_of(m, "paged")["tools"] == 8
    m2 = manager({"paged": stdio("--era", "legacy", "--page-size", "2")})
    assert status_of(m2, "paged")["tools"] == 8


class PagedTransport:
    """不走进程的假传输：按 cursor 给出写好的几页，用来测翻页的细节。"""

    def __init__(self, pages: dict, name: str = "stdio"):
        self.name = name
        self.pages = pages
        self.asked: list = []
        self.headers: list = []

    def request(self, method, params, timeout, stop=None, headers=None):
        self.asked.append((method, (params or {}).get("cursor")))
        self.headers.append(headers or {})
        if method == "tools/call":
            return {"content": [{"type": "text", "text": "ok"}]}
        return self.pages[(params or {}).get("cursor")]


def tool(name: str, schema: dict | None = None) -> dict:
    return {"name": name, "inputSchema": schema or {"type": "object"}}


def test_empty_cursor_is_a_page_and_a_repeated_cursor_stops():
    pages = {
        None: {"tools": [tool("a")], "nextCursor": ""},  # 空字符串也是一个合法的"下一页"
        "": {"tools": [tool("b"), tool("a")], "nextCursor": "x"},  # 重名的只收第一个
        "x": {"tools": [tool("c"), {"name": ""}, {"name": "bad", "inputSchema": "nope"}], "nextCursor": "x"},  # 又给了 x：不再翻
    }
    transport = PagedTransport(pages)
    log = ServerLog()
    client = McpClient(transport, log)
    client.era = "legacy"
    tools, rejected = client.list_tools(5)
    assert [item["name"] for item in tools] == ["a", "b", "c"]
    assert [item["name"] for item in rejected] == ["?", "bad"]
    assert [cursor for _, cursor in transport.asked] == [None, "", "x"]
    assert any("重复" in line["text"] for line in log.lines())


def test_x_mcp_header_is_mirrored_over_http_and_bad_ones_are_rejected():
    good = {"type": "object", "properties": {"region": {"type": "string", "x-mcp-header": "Region"}, "q": {"type": "string"}}}
    on_array = {"type": "object", "properties": {"list": {"type": "array", "items": {"type": "string", "x-mcp-header": "Item"}}}}
    on_number = {"type": "object", "properties": {"n": {"type": "number", "x-mcp-header": "N"}}}
    twice = {"type": "object", "properties": {"a": {"type": "string", "x-mcp-header": "Same"}, "b": {"type": "string", "x-mcp-header": "same"}}}
    pages = {None: {"tools": [tool("sql", good), tool("arr", on_array), tool("num", on_number), tool("dup", twice)]}}
    transport = PagedTransport(pages, name="http")
    client = McpClient(transport, ServerLog())
    client.era = "legacy"
    tools, rejected = client.list_tools(5)
    assert [item["name"] for item in tools] == ["sql"]
    assert sorted(item["name"] for item in rejected) == ["arr", "dup", "num"]
    client.call_tool("sql", {"region": "us-west1", "q": "select 1"}, 5)
    assert transport.headers[-1] == {"Mcp-Param-Region": "us-west1"}
    client.call_tool("sql", {"region": "北京 "}, 5)
    assert transport.headers[-1]["Mcp-Param-Region"] == "=?base64?" + base64.b64encode("北京 ".encode()).decode() + "?="
    # 同样的工具走 stdio 时，这个标记可以不管
    stdio_client = McpClient(PagedTransport(pages), ServerLog())
    stdio_client.era = "legacy"
    assert len(stdio_client.list_tools(5)[0]) == 4


def test_header_values_that_are_not_plain_ascii_are_base64_encoded():
    assert header_value("plain-value") == "plain-value"
    assert header_value("=?base64?abc?=").startswith("=?base64?") and header_value("=?base64?abc?=") != "=?base64?abc?="
    assert header_value("a\nb") == "=?base64?" + base64.b64encode(b"a\nb").decode() + "?="


# ---- 结果 -----------------------------------------------------------------------


def test_results_of_every_kind_become_text_and_images():
    out = render({"content": [{"type": "text", "text": "hello"}, {"type": "image", "data": PNG, "mimeType": "image/jpeg"}]})
    assert out.text.startswith("hello") and "[图片 1：image/png" in out.text  # 以内容为准，不信它说的 jpeg
    assert [image.media_type for image in out.images] == ["image/png"] and out.is_error is False

    assert json.loads(render({"structuredContent": {"sum": 5}}).text) == {"sum": 5}  # 只有结构化结果：写成 JSON
    both = render({"content": [{"type": "text", "text": "5"}], "structuredContent": {"sum": 5}})
    assert both.text == "5"  # 有文字时，结构化结果是重复的，不再写一遍
    assert render({"content": [{"type": "text", "text": "no"}], "isError": True}).is_error is True
    assert render({}).text == "（服务器没有返回任何内容）"

    resource = render({"content": [
        {"type": "resource", "resource": {"uri": "file:///a.txt", "text": "body"}},
        {"type": "resource", "resource": {"uri": "file:///p.png", "mimeType": "image/png", "blob": PNG}},
        {"type": "resource", "resource": {"uri": "file:///z.bin", "mimeType": "application/zip", "blob": "AAAA"}},
        {"type": "resource_link", "uri": "file:///big", "name": "big", "description": "很大"},
        {"type": "audio", "data": "AAAA", "mimeType": "audio/wav"},
        {"type": "hologram"},
    ]})
    assert "[资源 file:///a.txt]\nbody" in resource.text and len(resource.images) == 1
    assert "application/zip" in resource.text and "[资源链接] big file:///big：很大" in resource.text
    assert "听不了" in resource.text and "hologram" in resource.text


def test_bad_or_too_many_images_are_described_instead_of_sent():
    junk = base64.b64encode(b"definitely not an image").decode()
    out = render({"content": [{"type": "image", "data": junk, "mimeType": "image/png"}, {"type": "image", "data": "@@@", "mimeType": "image/png"}]})
    assert out.images == () and "不是 PNG" in out.text
    many = render({"content": [{"type": "image", "data": PNG, "mimeType": "image/png"}] * 6})
    assert len(many.images) == 4 and "一次最多交给模型 4 张" in many.text


def test_very_long_results_are_cut():
    out = render({"content": [{"type": "text", "text": "x" * 150_000}]})
    assert len(out.text) < 101_000 and "只保留了开头" in out.text


def test_calling_tools_on_a_live_server(manager):
    m = manager({"fake": stdio()})
    assert json.loads(m.call("fake", "add", {"a": 2, "b": 3}).text) == {"sum": 5}
    failed = m.call("fake", "fail", {})
    assert failed.is_error and "on purpose" in failed.text
    picture = m.call("fake", "picture", {})
    assert picture.images[0].media_type == "image/png" and base64.b64decode(picture.images[0].data).startswith(b"\x89PNG")
    with pytest.raises(ToolError, match="参数必须是一个 JSON 对象"):
        m.call("fake", "echo", ["not", "a", "dict"])
    with pytest.raises(ToolError, match="Unknown tool"):  # 服务器回答的协议错误，原样告诉模型
        m.call("fake", "nope", {})


def test_a_slow_tool_times_out_with_a_clear_message(manager, tmp_path):
    log = tmp_path / "slow.jsonl"
    m = manager({"fake": stdio("--record", str(log), timeout=1)})
    started = time.monotonic()
    with pytest.raises(ToolError, match="等了 1 秒"):
        m.call("fake", "slow", {"seconds": 5})
    assert time.monotonic() - started < 3
    call_id = next(message["id"] for message in records(log) if message.get("method") == "tools/call")
    # 超时也要告诉服务器"不用做了"
    wait_until(lambda: any(message.get("method") == "notifications/cancelled" and message["params"]["requestId"] == call_id for message in records(log)), what="取消通知")


def stop_soon(manager: McpManager, after: float = 0.4) -> None:
    def later() -> None:
        time.sleep(after)
        manager.cancel_running()

    threading.Thread(target=later, daemon=True).start()


def test_stop_cancels_a_stdio_call(manager, tmp_path):
    log = tmp_path / "cancel.jsonl"
    m = manager({"fake": stdio("--record", str(log))})
    stop_soon(m)
    started = time.monotonic()
    with pytest.raises(ToolError, match="用户中断了这次调用"):
        m.call("fake", "slow", {"seconds": 10})
    assert time.monotonic() - started < 3
    call_id = next(message["id"] for message in records(log) if message.get("method") == "tools/call")
    wait_until(lambda: any(message.get("method") == "notifications/cancelled" and message["params"]["requestId"] == call_id for message in records(log)), what="取消通知")
    assert m.call("fake", "echo", {"text": "still fine"}).text == "still fine"  # 连接没有受影响


def test_stopping_one_run_cancels_only_its_own_mcp_calls(manager):
    """你的对话和几个定时任务同时在跑，各自都可能停在一个很慢的 MCP 调用上。

    点对话里的"停止"只取消对话自己的调用（owner 是 None），停下一个任务也只取消那个任务的：
    每个运行用的快照记着它是谁的（snapshot(owner)），调用就记在谁的名下。
    """
    m = manager({"fake": stdio()})
    task, other = object(), object()
    chat_tool = next(tool for tool in m.snapshot().tools if tool.name.endswith("slow"))
    task_tool = next(tool for tool in m.snapshot(task).tools if tool.name.endswith("slow"))
    other_tool = next(tool for tool in m.snapshot(other).tools if tool.name.endswith("slow"))
    outcome: dict[str, str] = {}

    def call(name: str, tool, seconds: float) -> None:
        try:
            outcome[name] = tool.run({"seconds": seconds}).text or "ok"
        except ToolError as exc:
            outcome[name] = str(exc)

    def running(owner: object) -> bool:
        with m._lock:
            return bool(m._running.get(owner))

    # 对话停下：任务的调用不受影响，照样等到结果
    threads = [threading.Thread(target=call, args=("chat", chat_tool, 10)), threading.Thread(target=call, args=("task", task_tool, 1.5))]
    for thread in threads:
        thread.start()
    wait_until(lambda: running(None) and running(task), what="两边的调用都开始了")
    m.cancel_running()
    threads[0].join(5)
    assert outcome["chat"] == "用户中断了这次调用。" and "task" not in outcome
    threads[1].join(5)
    assert outcome["task"] != "用户中断了这次调用。" and "中断" not in outcome["task"]

    # 反过来：停下一个任务，你的对话和另一个任务的调用都不受影响
    threads = [
        threading.Thread(target=call, args=("chat", chat_tool, 1.5)),
        threading.Thread(target=call, args=("task", task_tool, 10)),
        threading.Thread(target=call, args=("other", other_tool, 1.5)),
    ]
    outcome.clear()
    for thread in threads:
        thread.start()
    wait_until(lambda: running(None) and running(task) and running(other), what="三边的调用都开始了")
    m.cancel_running(task)
    threads[1].join(5)
    assert outcome["task"] == "用户中断了这次调用。" and "chat" not in outcome and "other" not in outcome
    for thread in (threads[0], threads[2]):
        thread.join(5)
    assert "中断" not in outcome["chat"] and "中断" not in outcome["other"]
    assert m._running == {}  # 做完的都从登记里拿掉了

    # 退出程序时不管是谁的都取消
    threads = [threading.Thread(target=call, args=(name, tool, 10)) for name, tool in (("chat", chat_tool), ("task", task_tool))]
    outcome.clear()
    for thread in threads:
        thread.start()
    wait_until(lambda: running(None) and running(task), what="两边的调用都开始了")
    m.cancel_all()
    for thread in threads:
        thread.join(5)
    assert outcome == {"chat": "用户中断了这次调用。", "task": "用户中断了这次调用。"}


def test_stop_cancels_http_calls_the_way_each_era_expects(manager):
    modern, legacy = FakeMcpHttp("modern").start(), FakeMcpHttp("legacy").start()
    try:
        m = manager({"web": {"type": "http", "url": modern.url}, "old": {"type": "http", "url": legacy.url}})
        stop_soon(m)
        with pytest.raises(ToolError, match="用户中断"):
            m.call("web", "slow", {"seconds": 10})
        wait_until(lambda: modern.disconnected, what="新一代：挂断连接")  # 新一代里挂断就是取消
        stop_soon(m)
        with pytest.raises(ToolError, match="用户中断"):
            m.call("old", "slow", {"seconds": 10})
        wait_until(lambda: legacy.logic.cancelled, what="老一代：取消通知")
    finally:
        modern.stop()
        legacy.stop()


# ---- 工具列表变了、进程崩了 ---------------------------------------------------------


@pytest.mark.parametrize("era", ["modern", "legacy"])
def test_list_changed_refreshes_the_tools_and_marks_the_conversation_stale(manager, era):
    m = manager({"fake": stdio("--era", era)})
    m.active = m.snapshot()
    assert status_of(m, "fake", started=True)["stale"] is False
    assert status_of(m, "fake", started=True)["in_use"] == 8
    m.call("fake", "grow", {})
    wait_until(lambda: status_of(m, "fake")["tools"] == 9, what="新的工具列表")
    # 当前这段对话还用着拍快照时的那 8 个；新的那个要等下一段对话
    assert status_of(m, "fake", started=True)["stale"] is True
    assert len(m.active.tools) == 8 and len(m.snapshot().tools) == 9


@pytest.mark.parametrize("era", ["modern", "legacy"])
def test_list_changed_over_http(manager, era):
    fake = FakeMcpHttp(era).start()
    try:
        m = manager({"web": {"type": "http", "url": fake.url}})
        wait_until(lambda: fake.listeners, what="通知连接")
        m.call("web", "grow", {})
        wait_until(lambda: status_of(m, "web")["tools"] == 9, what="新的工具列表")
    finally:
        fake.stop()


def test_a_crashed_server_is_restarted_but_not_forever(manager):
    m = manager({"fake": stdio()})
    for crash in range(1, 4):
        with pytest.raises(ToolError, match="退出码 3"):
            m.call("fake", "crash", {})
        if crash < 3:
            wait_until(lambda: status_of(m, "fake")["status"] == "ready", what="自动重启")
    wait_until(lambda: status_of(m, "fake")["status"] == "failed", what="不再重启")
    time.sleep(0.5)
    row = status_of(m, "fake")
    assert row["status"] == "failed" and "不再自动重启" in row["error"]
    m.restart("fake")  # 你点了"重启"：重新数
    settle(m)
    assert status_of(m, "fake")["status"] == "ready"


def test_shutdown_ends_the_server_processes(manager):
    m = manager({"fake": stdio()})
    process = m._servers["fake"].transport._process
    m.shutdown()
    assert process.poll() is not None


# ---- 名字和参数格式 ----------------------------------------------------------------


def test_tool_names_follow_the_provider_rules_and_stay_unique():
    taken: set[str] = set()
    names = []
    for server, tool_name in [
        ("github", "search.issues"),
        ("my server", "a tool"),
        ("我的工具", "搜索"),
        ("a" * 50, "b" * 80),
        ("a" * 50, "b" * 80 + "c"),
        ("x.y", "echo"),
        ("x_y", "echo"),
    ]:
        name = exposed_name(server, tool_name, taken)
        taken.add(name)
        names.append(name)
    assert all(NAME_RULE.fullmatch(name) for name in names), names
    assert len(set(names)) == len(names)
    assert names[0] == "mcp__github__search_issues"
    assert names[5] == "mcp__x_y__echo" and names[6] == "mcp__x_y__echo_2"


def test_two_servers_with_the_same_tool_names_both_get_in(manager):
    m = manager({"one": stdio(), "two": stdio()})
    names = [tool.name for tool in m.snapshot().tools]
    assert "mcp__one__echo" in names and "mcp__two__echo" in names
    assert m.snapshot().tools[0].spec["description"].startswith("（第三方工具，来自 MCP 服务器“one”")


def test_schemas_are_made_acceptable_for_each_provider():
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "anyOf": [{"required": ["a"]}],
        "properties": {
            "item": {"$ref": "#/$defs/Item", "description": "一项"},
            "additionalProperties": {"type": "string"},  # 一个就叫这个名字的参数，不能被当成关键字去掉
            "node": {"$ref": "#/$defs/Node"},
            "far": {"$ref": "https://example.com/schema.json"},
        },
        "additionalProperties": False,
        "x-vendor": 1,
        "$defs": {
            "Item": {"type": "object", "properties": {"n": {"type": "integer"}}, "additionalProperties": False},
            "Node": {"type": "object", "properties": {"child": {"$ref": "#/$defs/Node"}}},
        },
    }
    claude = claude_schema(schema)
    assert claude["type"] == "object" and "anyOf" not in claude and "$schema" not in claude
    assert claude["properties"]["item"] == {"$ref": "#/$defs/Item", "description": "一项"}  # 里面的原样留给 Claude

    portable = portable_schema(schema)
    text = json.dumps(portable)
    assert "$ref" not in text and "$defs" not in text and "x-vendor" not in text and "$schema" not in text
    assert portable["properties"]["item"] == {"type": "object", "properties": {"n": {"type": "integer"}}, "description": "一项"}
    assert portable["properties"]["additionalProperties"] == {"type": "string"}
    assert portable["properties"]["far"] == {}  # 指向网上的引用：不去取
    assert claude_schema({"type": "array"})["type"] == "object" and claude_schema({})["properties"] == {}
    huge = {"type": "object", "properties": {f"p{index}": {"type": "string", "description": "x" * 100} for index in range(500)}}
    assert claude_schema(huge) == {"type": "object", "properties": {}}


# ---- 交给模型：图片、快照 -----------------------------------------------------------


def write_app_config(make_config, servers: dict) -> None:
    write_config(make_config().mcp_file, servers)


def test_an_image_reaches_claude_inside_the_tool_result(start, make_config):
    write_app_config(make_config, {"fake": stdio()})
    h = start(reply(tool_use("toolu_1", "mcp__fake__picture", {}), stop_reason="tool_use"), reply(text("看到了一个小点。")))
    settle(h.app.mcp)
    h.agent.run_turn("给我看张图")

    result = h.api.requests[1]["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and "is_error" not in result
    assert result["content"][0] == {"type": "text", "text": "here is a picture\n\n[图片 1：image/png，约 1 KB]"}
    assert result["content"][1]["type"] == "image"
    assert result["content"][1]["source"] == {"type": "base64", "media_type": "image/png", "data": PNG}
    assert "eager_input_streaming" not in next(spec for spec in h.api.requests[0]["tools"] if spec["name"] == "mcp__fake__picture")


def test_an_image_reaches_gpt_as_a_follow_up_user_message(start_openai, make_config):
    write_app_config(make_config, {"fake": stdio()})
    h = start_openai(chat(calls=[openai_call("call_1", "mcp__fake__picture", "{}")]), chat("看到了。"))
    settle(h.app.mcp)
    h.app.select_model("openai", "gpt-test")  # 看得懂图片的一家
    h.agent.run_turn("给我看张图")

    messages = h.api.requests[1]["messages"]
    assert messages[-2]["role"] == "tool" and "here is a picture" in messages[-2]["content"]
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"][1] == {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}}


def test_deepseek_gets_a_note_instead_of_the_image(start_openai, make_config):
    write_app_config(make_config, {"fake": stdio()})
    h = start_openai(chat(calls=[openai_call("call_1", "mcp__fake__picture", "{}")]), chat("好的。"))
    settle(h.app.mcp)
    h.agent.run_turn("给我看张图")

    messages = h.api.requests[1]["messages"]
    assert messages[-1]["role"] == "tool" and "看不了图片" in messages[-1]["content"]
    assert not any(isinstance(message.get("content"), list) for message in messages)


def tool_names_sent(request: dict) -> list[str]:
    return [spec["name"] for spec in request["tools"]]


def test_tools_stay_frozen_within_a_conversation_and_update_in_the_next(start, make_config):
    write_app_config(make_config, {"one": stdio()})
    h = start(reply(text("你好")), reply(text("还是你好")), reply(text("新对话")))
    m = h.app.mcp
    settle(m)
    h.agent.run_turn("第一句")

    # 对话进行中：连上一个新的服务器，旧的那个说"工具变了"
    m.save({"name": "two", "transport": "stdio", "command": sys.executable, "args": [str(FAKE)]}, None)
    m.call("one", "grow", {})
    settle(m)
    wait_until(lambda: status_of(m, "one")["tools"] == 9, what="新的工具列表")
    h.agent.run_turn("第二句")

    first, second = h.api.requests[0], h.api.requests[1]
    # 说明书和系统提示词一个字节都不能变：Claude 的缓存和思考内容的校验都靠这个
    assert json.dumps(first["tools"], sort_keys=True) == json.dumps(second["tools"], sort_keys=True)
    assert first["system"] == second["system"]
    assert "mcp__two__echo" not in tool_names_sent(second) and "mcp__one__extra" not in tool_names_sent(second)
    assert any(server["stale"] for server in m.status(started=True))

    h.app.new_conversation()
    assert "mcp__two__echo" in [name for name, _ in h.app.tool_names()]  # 还没开始的对话，信息栏里先列出来
    h.agent.run_turn("新的开始")
    third = tool_names_sent(h.api.requests[2])
    assert "mcp__two__echo" in third and "mcp__one__extra" in third
    assert tool_names_sent(first)[: len(h.app.tools.specs())] == third[: len(h.app.tools.specs())]  # 自己的工具在前，顺序不变


def test_opening_a_saved_conversation_takes_a_fresh_snapshot(start, make_config):
    write_app_config(make_config, {"one": stdio()})
    h = start(reply(text("你好")), reply(text("接着聊")))
    m = h.app.mcp
    settle(m)
    h.agent.run_turn("第一句")
    assert h.app.save_conversation([])
    saved = h.app.conversation.id
    h.app.new_conversation()
    m.save({"name": "two", "transport": "stdio", "command": sys.executable, "args": [str(FAKE)]}, None)
    settle(m)
    h.app.open_conversation(saved)
    assert m.resolve("mcp__two__echo") == ("two", "echo")
    h.agent.run_turn("第二句")
    assert "mcp__two__echo" in tool_names_sent(h.api.requests[1])


def test_a_tool_of_a_removed_server_returns_a_clear_error(start, make_config):
    write_app_config(make_config, {"one": stdio()})
    h = start(
        reply(text("好")),
        reply(tool_use("toolu_1", "mcp__one__echo", {"text": "hi"}), stop_reason="tool_use"),
        reply(text("那个服务器不在了。")),
    )
    m = h.app.mcp
    settle(m)
    h.agent.run_turn("第一句")
    m.remove("one")
    h.agent.run_turn("再调用一次")
    result = h.api.requests[2]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "MCP 服务器“one”已经被删掉了" in result["content"]


# ---- 权限 -----------------------------------------------------------------------


def test_mcp_calls_ask_first_unless_the_tool_is_trusted(start, make_config):
    write_app_config(make_config, {"fake": stdio()})
    h = start(
        reply(tool_use("toolu_1", "mcp__fake__echo", {"text": "hi"}), stop_reason="tool_use"),
        reply(text("说了 hi")),
        reply(tool_use("toolu_2", "mcp__fake__echo", {"text": "again"}), stop_reason="tool_use"),
        reply(text("又说了一遍")),
    )
    m = h.app.mcp
    settle(m)
    h.agent.run_turn("让它说 hi")
    assert len(h.ui.confirmations) == 1
    question = h.ui.confirmations[0]
    assert "fake" in question and "echo" in question and '"text": "hi"' in question
    assert h.ui.auto_allowed_count == 0

    m.set_approval("fake", "echo", "auto")  # 你在页面上把这个工具设成"不用问"
    h.agent.run_turn("再说一遍")
    assert len(h.ui.confirmations) == 1 and h.ui.auto_allowed_count == 1  # 每次都问的模式下也算数，记录里看得出来
    assert h.ui.results[-1] == ("again", False)


def test_a_denied_mcp_call_never_reaches_the_server(start, make_config, tmp_path):
    log = tmp_path / "denied.jsonl"
    write_app_config(make_config, {"fake": stdio("--record", str(log))})
    h = start(reply(tool_use("toolu_1", "mcp__fake__echo", {"text": "hi"}), stop_reason="tool_use"), reply(text("好吧")), approve=False)
    settle(h.app.mcp)
    h.agent.run_turn("让它说 hi")
    assert h.ui.results[-1][1] is True and "用户拒绝" in h.ui.results[-1][0]
    assert not any(message.get("method") == "tools/call" for message in records(log))


def test_a_scheduled_task_run_gets_its_own_snapshot_and_asks_even_for_trusted_tools(start, make_config):
    """定时任务自己运行时也带上 MCP 的工具（开始那一刻的快照）。设成"不用问"的工具是在对话里放开的，
    任务里不算数：任务按它自己的那一档，"每次确认"就照样要问。

    它手里的快照可以和你正在看的这段对话的不一样（这里那段对话的快照是空的），权限照样认得出哪些是 MCP 的工具：
    认的是工具本身，不是对话的快照。认不出来的话，这次调用就会不问就执行。
    """
    write_app_config(make_config, {"fake": stdio()})
    h = start(reply(tool_use("toolu_1", "mcp__fake__echo", {"text": "task"}), stop_reason="tool_use"), reply(text("做完了")))
    m = h.app.mcp
    settle(m)
    m.set_approval("fake", "echo", "auto")
    m.active = Snapshot()  # 你正在看的那段对话还没开始：它的快照里一个 MCP 工具都没有
    ui = RecordingUI()
    agent = h.app.unattended_agent(ui).agent
    assert agent.tools.get("mcp__fake__echo") is not None
    agent.run_turn("定时任务的指令")

    assert "mcp__fake__echo" in tool_names_sent(h.api.requests[0])
    assert len(ui.confirmations) == 1 and "fake" in ui.confirmations[0] and ui.auto_allowed_count == 0
    assert ui.results[-1] == ("task", False)
    assert h.app.mcp.active.tools == []  # 没有动你那段对话的快照


def test_a_task_allowed_to_do_everything_calls_mcp_tools_without_asking(start, make_config):
    # 任务自己选了"全部自动允许"：MCP 的工具也不问（和对话里的"全部自动允许"一样），记录里标着"自动允许"
    write_app_config(make_config, {"fake": stdio()})
    h = start(reply(tool_use("toolu_1", "mcp__fake__echo", {"text": "task"}), stop_reason="tool_use"), reply(text("做完了")))
    settle(h.app.mcp)
    ui = RecordingUI()
    h.app.unattended_agent(ui, "all").agent.run_turn("定时任务的指令")
    assert ui.confirmations == [] and ui.auto_allowed_count == 1 and ui.results[-1] == ("task", False)


def test_an_unrecognized_mcp_name_still_asks():
    # 名字是 mcp__ 开头、却不知道是哪个服务器的：宁可多问一次，也不能不问
    permissions = Permissions(editor=None, mcp=None)  # type: ignore[arg-type]
    assert "mcp__ghost__run" in permissions.review("mcp__ghost__run", {"x": 1})
    assert permissions.auto_allows("mcp__ghost__run", {"x": 1}) is False


def test_server_default_and_per_tool_overrides(manager, make_config):
    m = manager({"fake": stdio()})
    m.active = m.snapshot()
    config = make_config()
    from harness.tools.files import FileEditor

    permissions = Permissions(FileEditor(config.workspace, config.backup_dir), mcp=m)
    assert permissions.review("mcp__fake__add", {"a": 1}) is not None
    assert permissions.auto_allows("mcp__fake__add", {"a": 1}) is False

    m.set_approval("fake", None, "auto")  # 整个服务器都不用问……
    m.set_approval("fake", "echo", "ask")  # ……只有 echo 照样问
    assert permissions.auto_allows("mcp__fake__add", {}) is True
    assert permissions.auto_allows("mcp__fake__echo", {}) is False
    m.set_approval("fake", None, "ask")
    permissions.mode = "all"
    assert permissions.auto_allows("mcp__fake__echo", {}) is True  # "全部自动允许"什么都不问
    # 问不问只是放宽：需要确认的说明照样有，不是 MCP 的工具不受影响
    assert permissions.review("mcp__fake__echo", {}) is not None
    assert permissions.review("todo_list", {}) is None
    assert permissions.review("mcp__fake__echo", "not a dict") is None  # 参数不合法：不会联系服务器


def test_the_model_cannot_touch_mcp_json_in_any_mode(start, make_config):
    write_app_config(make_config, {"fake": {"command": "x", "env": {"TOKEN": "secret-value"}}})
    h = start()
    editor = h.agent.tools.get(FILE_TOOL)
    for mode in ("ask", "edits", "all"):
        h.agent.permissions.mode = mode
        for command in ({"command": "view", "path": str(h.config.mcp_file)}, {"command": "create", "path": str(h.config.mcp_file), "file_text": "{}"}):
            with pytest.raises(ToolError, match="密钥"):
                editor.run(command)
    assert "secret-value" in h.config.mcp_file.read_text(encoding="utf-8")


# ---- 配置和密钥 -------------------------------------------------------------------


def test_display_never_carries_secret_values():
    entry = normalize("gh", {
        "command": "npx",
        "args": ["-y", "server", "--token", "plain-token-value", "--api-key=abc123", "postgres://me:hunter2@db/x", "https://x.io/?token=t0k3n&q=1"],
        "env": {"GITHUB_TOKEN": "ghp_" + "a" * 36, "SHORT": "debug"},
    })
    shown = json.dumps(display(entry), ensure_ascii=False)
    for secret in ("plain-token-value", "abc123", "hunter2", "t0k3n", "ghp_" + "a" * 36, "debug"):
        assert secret not in shown
    assert display(entry)["env"] == [{"name": "GITHUB_TOKEN", "hint": "…aaaa"}, {"name": "SHORT", "hint": ""}]
    assert display(entry)["args"] == ["-y", "server", "--token", MASK, "--api-key=" + MASK, f"postgres://me:{MASK}@db/x", f"https://x.io/?token={MASK}&q=1"]
    web = display(normalize("w", {"type": "http", "url": "https://x.io/mcp", "headers": {"Authorization": "Bearer 0123456789abcdefWXYZ"}}))
    assert web["headers"] == [{"name": "Authorization", "hint": "…WXYZ"}] and "0123456789" not in json.dumps(web)
    assert hint("short") == "" and hint("x" * 15) == "" and hint("x" * 15 + "Q") == "…xxxQ"
    assert mask_text("sk-" + "z" * 20) == MASK
    assert mask_args(["--verbose", "--token", "--x"]) == ["--verbose", "--token", "--x"]  # 后面跟的是另一个选项：不遮


def test_editing_keeps_secrets_that_were_not_retyped():
    old = normalize("gh", {"command": "npx", "args": ["-y", "--token", "keep-me", "--other", "x"], "env": {"A": "a-secret", "B": "b-secret"}, "approval": {"default": "auto"}})
    shown = display(old)
    form = {
        "name": "gh",
        "transport": "stdio",
        "command": "npx",
        "args": shown["args"] + ["--new"],
        "env": [{"name": "A", "keep": "A"}, {"name": "RENAMED", "keep": "B"}, {"name": "C", "value": "c-new"}, {"name": "", "value": ""}],
    }
    entry = from_form(form, old)
    assert entry["args"] == ["-y", "--token", "keep-me", "--other", "x", "--new"]
    assert entry["env"] == {"A": "a-secret", "RENAMED": "b-secret", "C": "c-new"}
    assert entry["approval"] == {"default": "auto"}  # 智狐自己的设置跟着留下

    with pytest.raises(ConfigError, match="遮住的记号"):
        from_form({**form, "args": ["--token", MASK, "changed"]}, old)  # 遮住的地方和原来对不上（位置变了）
    with pytest.raises(ConfigError, match="还没有填值"):
        from_form({**form, "env": [{"name": "NEW"}]}, old)
    with pytest.raises(ConfigError, match="出现了两次"):
        from_form({**form, "env": [{"name": "A", "value": "1"}, {"name": "A", "value": "2"}]}, old)

    web_old = normalize("w", {"type": "http", "url": "https://u:pw-123@x.io/mcp", "headers": {"Authorization": "Bearer t"}})
    web = from_form({"name": "w", "transport": "http", "url": display(web_old)["url"], "headers": [{"name": "Authorization", "keep": "Authorization"}]}, web_old)
    assert web["url"] == "https://u:pw-123@x.io/mcp" and web["headers"] == {"Authorization": "Bearer t"}


def test_references_to_environment_variables(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "from-env")
    entry = expand({"command": "x", "args": ["${MY_TOKEN}", "${MISSING:-fallback}"], "env": {"T": "Bearer ${MY_TOKEN}"}})
    assert entry["args"] == ["from-env", "fallback"] and entry["env"] == {"T": "Bearer from-env"}
    with pytest.raises(ConfigError, match="没有设置"):
        expand({"command": "x", "args": ["${MISSING}"]})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "my-real-key")
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        expand({"command": "x", "env": {"STEAL": "${ANTHROPIC_API_KEY}"}})


def test_child_processes_do_not_see_the_model_keys(manager, monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    out = tmp_path / "env.txt"
    script = tmp_path / "dump.py"
    script.write_text(f"import os, sys\nopen({str(out)!r}, 'w').write(os.environ.get('ANTHROPIC_API_KEY', '') + '|' + os.environ.get('MINE', ''))\nsys.exit(1)\n", encoding="utf-8")
    manager({"dump": {"command": sys.executable, "args": [str(script)], "env": {"MINE": "given"}}})
    wait_until(out.exists, what="子进程写出环境变量")
    assert out.read_text(encoding="utf-8") == "|given"


def test_normalize_rejects_what_we_cannot_use():
    with pytest.raises(ConfigError, match="HTTP\\+SSE"):
        normalize("old", {"type": "sse", "url": "https://x.io/sse"})
    with pytest.raises(ConfigError, match="command"):
        normalize("empty", {})
    with pytest.raises(ConfigError, match="http://"):
        normalize("ftp", {"type": "http", "url": "ftp://x.io"})
    with pytest.raises(ConfigError, match="自动填写"):
        normalize("hdr", {"type": "http", "url": "https://x.io", "headers": {"Mcp-Session-Id": "x"}})
    with pytest.raises(ConfigError, match="换行"):
        normalize("nl", {"type": "http", "url": "https://x.io", "headers": {"X-A": "a\r\nInjected: 1"}})
    kept = normalize("n", {"command": "x", "args": [1, 2.5], "env": {"PORT": 8080}, "disabled": True, "alwaysAllow": ["a"]})
    assert kept["args"] == ["1", "2.5"] and kept["env"] == {"PORT": "8080"}
    assert kept["disabled"] is True and kept["alwaysAllow"] == ["a"]  # 别的程序的设置原样留着


def test_the_config_file(tmp_path):
    path = tmp_path / "data" / "mcp.json"
    write_config(path, {"good": {"command": "x"}, "bad": {"nonsense": 1}})
    config = McpConfig(path)
    servers = config.servers()
    assert servers["good"][0]["command"] == "x" and servers["bad"][0] is None and servers["bad"][1]
    config.put("third", {"command": "y"})
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert list(on_disk["mcpServers"]) == ["good", "bad", "third"]  # 写错了的那个也原样留着
    assert has_mode(path, 0o600)
    with pytest.raises(ConfigError, match="已经有一个"):
        config.put("good", {"command": "z"})
    config.put("renamed", {"command": "x2"}, original="good")
    assert list(json.loads(path.read_text(encoding="utf-8"))["mcpServers"]) == ["renamed", "bad", "third"]  # 改名不挪位置

    config.set_approval("renamed", "t1", "auto")
    config.set_approval("renamed", None, "auto")
    assert json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["renamed"]["approval"] == {"default": "auto"}  # 和默认值一样的就不单独记
    assert config.approval("renamed", "t1") == "auto" and config.approval("third", "t1") == "ask"

    # 你在编辑器里改了文件：改之前重新读，不会被内存里的旧内容盖掉
    data = json.loads(path.read_text(encoding="utf-8"))
    data["mcpServers"]["by-hand"] = {"command": "h"}
    path.write_text(json.dumps(data), encoding="utf-8")
    config.put("fourth", {"command": "f"})
    assert "by-hand" in json.loads(path.read_text(encoding="utf-8"))["mcpServers"]


def test_a_broken_config_file_is_never_overwritten(tmp_path):
    path = tmp_path / "data" / "mcp.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ this is not json", encoding="utf-8")
    config = McpConfig(path)
    assert config.problem and config.servers() == {}
    with pytest.raises(ConfigError, match="读不出来"):
        config.put("x", {"command": "y"})
    assert path.read_text(encoding="utf-8") == "{ this is not json"
    path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    assert config.reload() is True and config.problem == ""


# ---- 导入和粘贴 -------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_importing_from_claude_desktop_and_claude_code(tmp_path, make_config, platform):
    # 不改这台电脑的 sys.platform：换一个 Env 就能假装在另一种系统上（见 imports/places.py）
    home = Path.home()
    workspace = make_config().workspace
    env = Env(home=home, workspace=workspace, platform=platform)
    desktop = home / ("Library/Application Support/Claude" if platform == "darwin" else ".config/Claude") / "claude_desktop_config.json"
    desktop.parent.mkdir(parents=True)
    desktop.write_text(json.dumps({"mcpServers": {
        "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]},
        "github": {"command": "npx", "args": ["-y", "server-github"], "env": {"GITHUB_TOKEN": "ghp_" + "s" * 36}},
    }}), encoding="utf-8")
    (home / ".claude.json").write_text(json.dumps({
        "numStartups": 3,
        "mcpServers": {"user-a": {"type": "stdio", "command": "uvx", "args": ["a"]}},
        "projects": {str(workspace): {"mcpServers": {"local-b": {"type": "http", "url": "https://b.example/mcp"}}}, "/elsewhere": {"mcpServers": {"other": {"command": "o"}}}},
    }), encoding="utf-8")
    (workspace / ".mcp.json").write_text(json.dumps({"mcpServers": {"proj-c": {"command": "c"}, "old-sse": {"type": "sse", "url": "https://c.example/sse"}}}), encoding="utf-8")

    config = McpConfig(make_config().mcp_file)
    config.put("filesystem", {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]})  # 已经有一模一样的
    config.put("github", {"command": "other"})  # 名字被占了，但连的不是同一个

    found = importers.sources(workspace, env)
    listed = {source["id"]: source for source in importers.preview(found, config)}
    claude = ("claude-desktop", "claude-code-user", "claude-code-local", "claude-code-project")
    assert all(listed[source]["found"] for source in claude)
    assert not any(source["found"] for source in listed.values() if source["id"] not in claude)  # 别的工具的文件都没有
    assert listed["claude-code-user"]["tool"] == "Claude Code" and listed["claude-code-user"]["level"] == "用户级"
    assert listed["claude-code-user"]["path"] == "~/.claude.json" and listed["claude-code-project"]["path"] == ".mcp.json"
    desktop_items = {item["name"]: item for item in listed["claude-desktop"]["servers"]}
    assert desktop_items["filesystem"]["duplicate"] is True
    assert desktop_items["github"]["target"] == "github-2" and desktop_items["github"]["duplicate"] is False
    assert "ghp_" not in json.dumps(listed)  # 清单里的密钥也遮住了
    assert [item["name"] for item in listed["claude-code-user"]["servers"]] == ["user-a"]
    assert [item["name"] for item in listed["claude-code-local"]["servers"]] == ["local-b"]  # 只有这个工作目录的
    project = {item["name"]: item for item in listed["claude-code-project"]["servers"]}
    assert project["old-sse"]["problem"] and project["old-sse"]["target"] == ""  # 废弃的连接方式：列出来，但不能导入
    assert project["proj-c"]["problem"] == ""

    sources = {source.id: source for source in found}
    added, skipped = importers.apply(config, sources["claude-desktop"].servers, ["github"])
    assert added == ["github-2"] and skipped == []
    assert config.get("github-2")["env"] == {"GITHUB_TOKEN": "ghp_" + "s" * 36}  # 复制过来的是完整的配置
    added, skipped = importers.apply(config, sources["claude-code-project"].servers, ["old-sse", "missing"])
    assert added == [] and len(skipped) == 2


def test_missing_sources_are_listed_as_not_found(tmp_path, make_config):
    found = importers.sources(make_config().workspace)
    # 登记过的每一处都列出来（页面上"没找到的"），一个都没有找到
    assert {"claude-desktop", "claude-code-user", "claude-code-local", "claude-code-project", "cursor-user"} <= {source.id for source in found}
    assert found and not any(source.found or source.servers or source.problem for source in found)


@pytest.mark.parametrize(
    ("pasted", "names"),
    [
        ('{"mcpServers": {"a": {"command": "x"}}}', ["a"]),
        ('{"servers": {"b": {"type": "http", "url": "https://b.io/mcp"}}}', ["b"]),
        ('{"c": {"command": "x"}, "d": {"command": "y"}}', ["c", "d"]),
        ('"e": {"command": "x", "args": ["1",],},', ["e"]),  # 从文档里复制的一截，外面没有大括号，还多了逗号
        ('{"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github@1.2.0"]}', ["github"]),
        ('{"command": "uvx", "args": ["mcp-server-fetch"]}', ["fetch"]),
        ('{"command": "python", "args": ["/path/to/weather_server.py"]}', ["weather"]),
        ('{"type": "http", "url": "https://mcp.notion.com/mcp"}', ["notion"]),
    ],
)
def test_pasting_json_in_the_shapes_people_copy(pasted, names):
    assert list(importers.parse_pasted(pasted)) == names


def test_pasting_nonsense_explains_what_is_wrong():
    for pasted, problem in (("", "空的"), ("not json", "JSON"), ("[1, 2]", "对象"), ('{"a": 1}', "没有在粘贴的内容里找到")):
        with pytest.raises(ConfigError, match=problem):
            importers.parse_pasted(pasted)


def test_unique_names():
    assert importers.unique_name("a", {"a", "a-2"}) == "a-3"
    assert importers.clean_name("my server / v2") == "my-server-v2"
    assert importers.clean_name("///") == "server"


# ---- 终端里的命令 -----------------------------------------------------------------


def test_terminal_commands_show_mcp_servers_and_tools(start, make_config, capsys):
    write_app_config(make_config, {"fake": stdio(), "typo": {"command": "definitely-not-a-command-xyz"}})
    ui = TerminalUI()
    h = start(ui=ui)
    settle(h.app.mcp)
    capsys.readouterr()
    assert handle_command("/mcp", h.app, ui) is True
    out = capsys.readouterr().out
    assert "fake  [已连接]  8 个工具" in out and "typo  [连接失败]" in out and "definitely-not-a-command-xyz" in out
    assert handle_command("/tools", h.app, ui) is True
    out = capsys.readouterr().out
    # 和网页信息栏是同一份清单（capabilities.py）：一个服务器一行，写着几个工具；工具的名字只在"技术细节"那一行
    assert "● fake · 8  [在用]" in out and "第三方 MCP 服务器提供的工具。" in out
    assert "技术细节：mcp__fake__echo" in out
    assert "○ typo  [关着]" in out and "连接失败" in out


def test_terminal_mcp_command_without_servers(start, capsys):
    ui = TerminalUI()
    h = start(ui=ui)
    capsys.readouterr()
    handle_command("/mcp", h.app, ui)
    assert "还没有接入 MCP 服务器" in capsys.readouterr().out
