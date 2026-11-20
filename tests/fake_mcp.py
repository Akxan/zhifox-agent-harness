"""假的 MCP 服务器，只在测试和演示里用。两种用法：

    python tests/fake_mcp.py [选项]     一个 stdio 服务器：从 stdin 读一行一条的 JSON-RPC，往 stdout 写回答
    FakeMcpHttp(...).start()            同一套逻辑，换成 HTTP（在测试进程里起一个本机的服务器）

它两代协议都会说，按选项决定自己是哪一代：
    --era modern   只认新一代（2026-07-28）：每个请求都要带 _meta，没有握手
    --era legacy   只认老一代（2025-11-25）：先 initialize；握手之前收到别的请求，回答"不认识"（或者 --silent 时不理）
    --era dual     两代都认（规范里的 dual-era）

工具（名字都是固定的，测试按名字调用）：
    echo {text}       原样返回文字
    add {a, b}        只返回结构化结果 {"sum": a+b}，没有文字
    fail {}           返回 isError 的结果
    picture {}        返回一张很小的 PNG 和一段文字
    resource {}       返回一个嵌入的资源（文字）和一个资源链接
    slow {seconds}    等一会儿再回答；中途收到取消就不回答了
    crash {}          （只有 stdio）进程直接退出
    grow {}           多出一个工具 extra，并发出"工具列表变了"的通知

--record 文件：把收到的每条消息追加写进这个文件（一行一条 JSON），测试靠它检查我们发了什么（比如取消通知）。
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODERN = "2026-07-28"
LEGACY = ("2025-11-25", "2025-06-18")
# 1×1 像素的透明 PNG
PNG = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
    )
).decode()

BASE_TOOLS = [
    {"name": "echo", "description": "Echo the text back.", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "add", "description": "Add two numbers.", "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}}},
    {"name": "fail", "description": "Always fails.", "inputSchema": {"type": "object"}},
    {"name": "picture", "description": "Return a picture.", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}},
    {"name": "resource", "description": "Return a resource.", "inputSchema": {"type": "object"}},
    {"name": "slow", "description": "Wait a while.", "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}}},
    {"name": "crash", "description": "Exit the process.", "inputSchema": {"type": "object"}, "annotations": {"destructiveHint": True}},
    {"name": "grow", "description": "Add a tool.", "inputSchema": {"type": "object"}},
]


class Logic:
    """协议的逻辑，stdio 和 HTTP 共用。send 是把一条消息发出去的办法（由外面那一层提供）。"""

    def __init__(
        self,
        era: str = "modern",
        page_size: int = 0,
        silent: bool = False,
        record: str | None = None,
        extra_tools: int = 0,
        supported: list[str] | None = None,
    ):
        self.era = era
        self.supported = supported or [MODERN]  # 新一代里支持哪些版本（测"版本对不上"时改成别的）
        self.page_size = page_size
        self.silent = silent
        self.record = record
        self.tools = [dict(tool) for tool in BASE_TOOLS] + [
            {"name": f"tool_{index}", "description": f"Extra tool {index}.", "inputSchema": {"type": "object"}} for index in range(extra_tools)
        ]
        self.initialized = False  # 老一代：收到 notifications/initialized 了没有
        self.legacy_version = ""
        self.cancelled: set = set()
        self.subscriptions: list = []  # 新一代：subscriptions/listen 请求的 id
        self.lock = threading.Lock()

    def note(self, message: dict) -> None:
        if self.record:
            with open(self.record, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(message) + "\n")

    # 返回要发出去的消息（可能是空的：通知，或者故意不回答）
    def handle(self, message: dict, send) -> list[dict]:
        self.note(message)
        method, id = message.get("method"), message.get("id")
        params = message.get("params") or {}
        if method is None:
            return []  # 我们发出去的请求（比如 ping）的回答
        if method == "notifications/cancelled":
            self.cancelled.add(params.get("requestId"))
            return []
        if method == "notifications/initialized":
            self.initialized = True
            return []
        if id is None:
            return []
        meta = params.get("_meta") or {}
        modern = "io.modelcontextprotocol/protocolVersion" in meta
        if method == "initialize":
            if self.era == "modern":
                return [error(id, -32601, "initialize is not supported; this server speaks 2026-07-28 only")]
            requested = params.get("protocolVersion")
            self.legacy_version = requested if requested in LEGACY else LEGACY[0]
            return [result(id, {"protocolVersion": self.legacy_version, "capabilities": {"tools": {"listChanged": True}}, "serverInfo": {"name": "fake-legacy", "version": "1.0"}, "instructions": "Use echo for testing."})]
        if modern and self.era in ("modern", "dual"):
            version = meta["io.modelcontextprotocol/protocolVersion"]
            if version not in self.supported:
                listed = self.supported + (list(LEGACY) if self.era == "dual" else [])
                return [error(id, -32022, "Unsupported protocol version", {"supported": listed, "requested": version})]
            return self.serve(id, method, params, send, modern=True)
        if self.era == "modern":
            return [error(id, -32602, "missing _meta")]
        if not self.initialized and method != "ping":
            if self.silent:
                return []
            return [error(id, -32601, f"Method not found: {method}")]
        return self.serve(id, method, params, send, modern=False)

    def serve(self, id, method: str, params: dict, send, modern: bool) -> list[dict]:
        if method == "server/discover":
            return [result(id, {"resultType": "complete", "supportedVersions": [MODERN, *LEGACY] if self.era == "dual" else [MODERN], "capabilities": {"tools": {"listChanged": True}}, "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "fake-modern", "version": "2.0"}}, "instructions": "Use echo for testing."}, modern)]
        if method == "ping":
            return [result(id, {}, modern)]
        if method == "subscriptions/listen":
            self.subscriptions.append(id)
            return [{"jsonrpc": "2.0", "method": "notifications/subscriptions/acknowledged", "params": {"_meta": {"io.modelcontextprotocol/subscriptionId": id}, "notifications": {"toolsListChanged": True}}}]
        if method == "tools/list":
            start = int(params.get("cursor") or 0)
            size = self.page_size or len(self.tools)
            page = self.tools[start : start + size]
            value: dict = {"tools": page}
            if start + size < len(self.tools):
                value["nextCursor"] = str(start + size)
            if modern:
                value.update({"ttlMs": 1000, "cacheScope": "private"})
            return [result(id, value, modern)]
        if method == "tools/call":
            return self.call(id, params.get("name"), params.get("arguments") or {}, send, modern)
        return [error(id, -32601, f"Method not found: {method}")]

    def call(self, id, name, arguments: dict, send, modern: bool) -> list[dict]:
        def done(content: list | None = None, **extra) -> list[dict]:
            return [result(id, {"content": content or [], **extra}, modern)]

        if name == "echo":
            return done([{"type": "text", "text": str(arguments.get("text", ""))}])
        if name == "add":
            return done([], structuredContent={"sum": arguments.get("a", 0) + arguments.get("b", 0)})
        if name == "fail":
            return done([{"type": "text", "text": "it failed on purpose"}], isError=True)
        if name == "picture":
            return done([{"type": "text", "text": "here is a picture"}, {"type": "image", "data": PNG, "mimeType": "image/png"}])
        if name == "resource":
            return done([
                {"type": "resource", "resource": {"uri": "file:///notes.txt", "mimeType": "text/plain", "text": "resource body"}},
                {"type": "resource_link", "uri": "file:///big.bin", "name": "big.bin"},
            ])
        if name == "slow":
            deadline = time.monotonic() + float(arguments.get("seconds", 5))
            while time.monotonic() < deadline:
                if id in self.cancelled:
                    return []  # 被取消了：按规范不再回答
                time.sleep(0.02)
            return done([{"type": "text", "text": "finally done"}])
        if name == "crash":
            sys.stdout.flush()
            import os

            os._exit(3)
        if name == "grow":
            with self.lock:
                if not any(tool["name"] == "extra" for tool in self.tools):
                    self.tools.append({"name": "extra", "description": "A new tool.", "inputSchema": {"type": "object"}})
            note = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
            if self.subscriptions:
                note["params"] = {"_meta": {"io.modelcontextprotocol/subscriptionId": self.subscriptions[-1]}}
            send(note)
            return done([{"type": "text", "text": "grown"}])
        return [error(id, -32602, f"Unknown tool: {name}")]


def result(id, value: dict, modern: bool = False) -> dict:
    if modern:
        value = {"resultType": "complete", **value}
    return {"jsonrpc": "2.0", "id": id, "result": value}


def error(id, code: int, message: str, data: object = None) -> dict:
    problem: dict = {"code": code, "message": message}
    if data is not None:
        problem["data"] = data
    return {"jsonrpc": "2.0", "id": id, "error": problem}


# ---- stdio ----------------------------------------------------------------------


def serve_stdio(logic: Logic, banner: str) -> None:
    lock = threading.Lock()

    def send(message: dict) -> None:
        with lock:
            sys.stdout.write(json.dumps(message) + "\n")
            sys.stdout.flush()

    def work(message: dict) -> None:
        for out in logic.handle(message, send):
            send(out)

    print(banner, file=sys.stderr, flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        if message.get("method") == "tools/call" and (message.get("params") or {}).get("name") == "slow":
            # 慢的工具放到别的线程里做，这样还能接着读到取消通知。别的按顺序做（握手的顺序不能乱）
            threading.Thread(target=work, args=(message,), daemon=True).start()
        else:
            work(message)
    print("stdin closed, bye", file=sys.stderr, flush=True)


# ---- HTTP -----------------------------------------------------------------------


class FakeMcpHttp:
    """同一套逻辑，走 Streamable HTTP。sse=True 时回答写成服务器推送事件（前面先发一条进度通知）。

    记下收到的每个请求：requests 里是 (方法, 请求头, 请求体)。
    """

    def __init__(self, era: str = "modern", sse: bool = False, token: str | None = None, page_size: int = 0):
        self.logic = Logic(era, page_size)
        self.sse = sse
        self.token = token
        self.sessions: set[str] = set()
        self.requests: list[tuple[str, dict, dict | None]] = []
        self.listeners: list = []  # 开着的通知连接上的 wfile
        self.disconnected: list = []  # slow 被挂断时记下 id

    def start(self) -> FakeMcpHttp:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:
                pass

            def _send_json(self, status: int, payload: dict | None, headers: dict | None = None) -> None:
                body = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                if payload is not None:
                    self.send_header("Content-Type", "application/json")
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self) -> bool:
                if fake.token and self.headers.get("Authorization") != f"Bearer {fake.token}":
                    self._send_json(401, {"error": "unauthorized"})
                    return False
                return True

            def do_DELETE(self) -> None:
                fake.requests.append(("DELETE", dict(self.headers), None))
                fake.sessions.discard(self.headers.get("Mcp-Session-Id", ""))
                self._send_json(200, None)

            def do_GET(self) -> None:
                fake.requests.append(("GET", dict(self.headers), None))
                if not self._authorized():
                    return
                if fake.logic.era == "modern":
                    self._send_json(405, None)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                fake.listeners.append(self.wfile)
                try:
                    while True:
                        time.sleep(0.2)
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except OSError:
                    pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(("POST", dict(self.headers), body))
                if not self._authorized():
                    return
                logic = fake.logic
                meta = (body.get("params") or {}).get("_meta") or {}
                modern = "io.modelcontextprotocol/protocolVersion" in meta
                if modern and logic.era != "legacy":
                    # 新一代：请求头要和请求体对得上
                    if self.headers.get("MCP-Protocol-Version") != meta["io.modelcontextprotocol/protocolVersion"] or self.headers.get("Mcp-Method") != body.get("method"):
                        self._send_json(400, error(body.get("id"), -32020, "header mismatch"))
                        return
                elif body.get("method") != "initialize" and "id" in body and logic.era != "modern":
                    session = self.headers.get("Mcp-Session-Id")
                    if session not in fake.sessions:
                        self._send_json(404 if session else 400, error(body.get("id"), -32000, "Bad Request: No valid session ID provided"))
                        return
                elif logic.era == "legacy" and "id" in body and body.get("method") != "initialize":
                    self._send_json(400, error(body.get("id"), -32000, "Bad Request: Server not initialized"))
                    return
                if "id" not in body or "method" not in body:  # 通知，或者我们回答它的请求
                    logic.handle(body, lambda message: None)
                    self._send_json(202, None)
                    return
                if body.get("method") == "subscriptions/listen":
                    self._listen(body)
                    return
                if body.get("method") == "tools/call" and (body.get("params") or {}).get("name") == "slow":
                    self._slow(body)
                    return
                outs = logic.handle(body, self._push)
                headers = {}
                if body.get("method") == "initialize" and outs and "result" in outs[0]:
                    session = secrets.token_hex(8)
                    fake.sessions.add(session)
                    headers["Mcp-Session-Id"] = session
                if not outs:
                    self._send_json(202, None)
                elif fake.sse:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.end_headers()
                    progress = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}}
                    self.wfile.write(f"data: {json.dumps(progress)}\n\n".encode())
                    if not modern and body.get("method") == "tools/call":
                        # 老一代的服务器可以在事件里反过来问客户端
                        ask = {"jsonrpc": "2.0", "id": "srv-1", "method": "ping"}
                        self.wfile.write(f"event: message\ndata: {json.dumps(ask)}\n\n".encode())
                    for out in outs:
                        self.wfile.write(f"data: {json.dumps(out)}\n\n".encode())
                    self.wfile.flush()
                    self.close_connection = True
                else:
                    status = 400 if "error" in outs[0] and outs[0]["error"]["code"] in (-32022, -32020, -32602) else 200
                    self._send_json(status, outs[0], headers)

            def _push(self, message: dict) -> None:
                for wfile in list(fake.listeners):
                    try:
                        wfile.write(f"data: {json.dumps(message)}\n\n".encode())
                        wfile.flush()
                    except OSError:
                        fake.listeners.remove(wfile)

            def _listen(self, body: dict) -> None:
                outs = fake.logic.handle(body, self._push)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for out in outs:
                    self.wfile.write(f"data: {json.dumps(out)}\n\n".encode())
                self.wfile.flush()
                fake.listeners.append(self.wfile)
                try:
                    while True:
                        time.sleep(0.2)
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except OSError:
                    pass

            def _slow(self, body: dict) -> None:
                seconds = float(((body.get("params") or {}).get("arguments") or {}).get("seconds", 5))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                deadline = time.monotonic() + seconds
                try:
                    while time.monotonic() < deadline:
                        if body["id"] in fake.logic.cancelled:
                            return
                        time.sleep(0.05)
                        self.wfile.write(b": working\n\n")  # 写不进去，就说明客户端挂断了（新一代协议里这就是取消）
                        self.wfile.flush()
                    modern = "io.modelcontextprotocol/protocolVersion" in ((body.get("params") or {}).get("_meta") or {})
                    done = result(body["id"], {"content": [{"type": "text", "text": "finally done"}]}, modern)
                    self.wfile.write(f"data: {json.dumps(done)}\n\n".encode())
                    self.wfile.flush()
                except OSError:
                    fake.disconnected.append(body["id"])
                self.close_connection = True

        class Quiet(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address) -> None:
                pass  # 客户端挂断连接是测试的一部分（取消），不用把异常打印出来

        self.server = Quiet(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--era", choices=("modern", "legacy", "dual"), default="modern")
    parser.add_argument("--page-size", type=int, default=0)
    parser.add_argument("--silent", action="store_true", help="老一代：握手之前收到的请求一概不理")
    parser.add_argument("--record")
    parser.add_argument("--extra-tools", type=int, default=0)
    parser.add_argument("--delay", type=float, default=0, help="启动之前先等这么多秒（模拟 npx 第一次下载）")
    parser.add_argument("--supported", help="新一代里支持的版本，逗号分开（默认只有 2026-07-28）")
    options = parser.parse_args()
    if options.delay:
        time.sleep(options.delay)
    supported = options.supported.split(",") if options.supported else None
    logic = Logic(options.era, options.page_size, options.silent, options.record, options.extra_tools, supported)
    serve_stdio(logic, f"fake MCP server ({options.era}) ready")
