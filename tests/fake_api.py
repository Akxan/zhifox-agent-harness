"""本地的假 Anthropic API，只在测试里用。

它按顺序回放事先写好的回复（格式和真实 API 的流式响应一样），并记下收到的每一个请求。
这样不需要 API key、不花钱，就能让真正的 SDK 把整个 agent 循环跑一遍，
还能检查我们发出去的请求长什么样。

几段对话同时进行时（你的对话和几个定时任务一起跑），"按顺序"就说不清是谁先谁后了。这时用 routes：
{"某句话": [回复, ...]}，请求里第一条消息含有这句话的，从它自己的那一串里按顺序取；别的照旧从 replies 里取。
每次运行的第一条消息就是任务的指令，所以用指令里的一句话就能把每个任务的回复分开。

测"停止"：held.held(回复) 是一条先压着不回答的回复（发了头、一个事件都不发，或者连头都不发），api.hold.release() 放行，
压着的时候智狐把连接断开了记在 api.hold.hangups（见 held.py）。keep_alive=True 时按 HTTP/1.1 回答、连接留着给下一个请求用
（真的 API 就是这样；默认是 HTTP/1.0，每回答完一个就关）。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from held import Hold


def text(value: str, citations: list[dict] | None = None) -> dict:
    """一个文字块。citations：这段文字引用的网页（web_cite 的结果），联网搜索后的回答才有。"""
    block = {"type": "text", "text": value}
    if citations is not None:
        block["citations"] = citations
    return block


def web_cite(url: str, title: str | None, cited_text: str, index: str = "enc-idx") -> dict:
    """一条联网搜索的引用（web_search_result_location），和真实的 API 一样带着要原样发回去的 encrypted_index。"""
    return {"type": "web_search_result_location", "url": url, "title": title, "encrypted_index": index, "cited_text": cited_text}


def web_search(id: str, query: str, results: list[tuple[str, str]]) -> list[dict]:
    """一次联网搜索：server_tool_use 和它的结果 web_search_tool_result（results 是 [(网址, 标题)]）。返回两个块。"""
    found = [
        {"type": "web_search_result", "url": url, "title": title, "encrypted_content": f"enc-{n}", "page_age": None}
        for n, (url, title) in enumerate(results)
    ]
    return [
        {"type": "server_tool_use", "id": id, "name": "web_search", "input": {"query": query}},
        {"type": "web_search_tool_result", "tool_use_id": id, "content": found},
    ]


def thinking(value: str, signature: str = "sig-1") -> dict:
    return {"type": "thinking", "thinking": value, "signature": signature}


def tool_use(id: str, name: str, input: dict, raw: str | None = None, toolset: str | None = None) -> dict:
    """一次工具调用。raw 是参数的原始 JSON 文本：想发 Python 自己写不出来的东西（比如 1e999）时用。
    toolset：调用的是工具包的成员（浏览器工具包是 "browser"），真实的 API 会在块上带着 toolset_name。"""
    return {"type": "tool_use", "id": id, "name": name, "input": input, "raw": raw, "toolset": toolset}


def reply(
    *blocks: dict, stop_reason: str = "end_turn", stop_details: dict | None = None, cut_after: int | None = None
) -> dict:
    """一条回复。cut_after 不是 None 时，只发出前面这么多个事件就把连接掐断，模拟回复收到一半断线。"""
    return {"blocks": list(blocks), "stop_reason": stop_reason, "stop_details": stop_details, "cut_after": cut_after}


def failure(status: int, kind: str, message: str) -> dict:
    """一次失败的请求：服务器按真实 API 的格式回答一个错误（比如 413 请求太大）。"""
    return {"status": status, "error": {"type": "error", "error": {"type": kind, "message": message}}}


def stream_error(kind: str, message: str, after: int = 2) -> dict:
    """回复发到一半，服务器发来一个错误事件（SSE 的 event: error，比如 overloaded_error），然后结束。
    HTTP 的状态码是 200：真实的 API 在开始回答以后过载，就是这样告诉你的。after：错误之前先发几个正常的事件。"""
    return {**reply(text("半截")), "stream_error": {"type": "error", "error": {"type": kind, "message": message}}, "error_after": after}


def _events(scripted: dict) -> list[dict]:
    """把一条写好的回复拆成真实 API 会发出的事件序列。"""
    events: list[dict] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 1,
                    "cache_read_input_tokens": 40,
                    "cache_creation_input_tokens": 10,
                },
            },
        }
    ]
    for index, block in enumerate(scripted["blocks"]):
        if block["type"] == "text":
            start = {"type": "text", "text": ""}
            deltas = [{"type": "text_delta", "text": block["text"]}]
            if "citations" in block:
                # 和真实的 API 一样：块开始时 citations 是空的列表，每一条引用是一个 citations_delta，SDK 把它们攒在块上
                start["citations"] = []
                deltas += [{"type": "citations_delta", "citation": citation} for citation in block["citations"]]
        elif block["type"] == "thinking":
            start = {"type": "thinking", "thinking": "", "signature": ""}
            deltas = [
                {"type": "thinking_delta", "thinking": block["thinking"]},
                {"type": "signature_delta", "signature": block["signature"]},
            ]
        elif block["type"] == "tool_use":
            start = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
            if block.get("toolset"):
                start["toolset_name"] = block["toolset"]
            deltas = [{"type": "input_json_delta", "partial_json": block.get("raw") or json.dumps(block["input"])}]
        else:  # 其他类型的块没有增量，整块一次发完
            start, deltas = block, []
        events.append({"type": "content_block_start", "index": index, "content_block": start})
        events += [{"type": "content_block_delta", "index": index, "delta": delta} for delta in deltas]
        events.append({"type": "content_block_stop", "index": index})
    events.append(
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": scripted["stop_reason"],
                "stop_sequence": None,
                "stop_details": scripted["stop_details"],
            },
            "usage": {"output_tokens": 20},
        }
    )
    events.append({"type": "message_stop"})
    return events


def _sse(event: dict) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


class FakeAPI:
    def __init__(self, replies: list[dict], routes: dict[str, list[dict]] | None = None, keep_alive: bool = False):
        self.replies = list(replies)
        self.keep_alive = keep_alive
        self.hold = Hold()  # 压着不回答的那几条回复（见文件开头和 held.py）
        self.routes = {marker: list(items) for marker, items in (routes or {}).items()}
        self.requests: list[dict] = []  # 收到的请求体
        self.headers: list[dict] = []  # 收到的请求头（键全部小写）
        self.peers: list[tuple] = []  # 每个请求是从哪个地址、端口来的：一样的就是复用了同一条连接（keep_alive）
        self.models = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-3-old"]  # 假装服务器上有这些模型
        self._lock = threading.Lock()  # 几个请求同时到时，"看看还有没有、有就取一条"要一口气做完

    def next_reply(self, body: dict) -> dict | None:
        """这个请求该拿到哪一条回复（见文件开头的 routes）。没有了返回 None。"""
        first = json.dumps((body.get("messages") or [None])[0], ensure_ascii=False)
        with self._lock:
            queue = next((items for marker, items in self.routes.items() if marker in first), self.replies)
            return queue.pop(0) if queue else None

    def start(self) -> FakeAPI:
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1" if api.keep_alive else "HTTP/1.0"

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                api.requests.append(body)
                api.headers.append({key.lower(): value for key, value in self.headers.items()})
                api.peers.append(self.client_address)
                scripted = api.next_reply(body)
                if scripted is None:
                    self.send_error(500, "no scripted reply left")
                    return
                if scripted.get("held"):
                    scripted = self._held(scripted)
                    if scripted is None:
                        return
                if scripted.get("status"):
                    payload = json.dumps(scripted["error"]).encode()
                    self.send_response(scripted["status"])
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if scripted.get("stream_error"):
                    events = _events(scripted)[: scripted["error_after"]]
                    payload = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
                    payload += f"event: error\ndata: {json.dumps(scripted['stream_error'])}\n\n"
                    data = payload.encode()
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if scripted.get("cut_after") is not None:
                    # 回复发到一半就断线：用分块传输发出前几个事件，不发结束标记，直接关连接
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("transfer-encoding", "chunked")
                    self.end_headers()
                    for event in _events(scripted)[: scripted["cut_after"]]:
                        data = f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
                        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                    self.wfile.flush()
                    self.close_connection = True
                    return
                payload = "".join(
                    f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in _events(scripted)
                ).encode()
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _held(self, scripted: dict) -> dict | None:
                """先压着（见 held.py）。对方断开了返回 None；放行了的话，头已经发了的把事件接着发完、返回 None，
                头还没发的返回写好的那条回复，照平常的样子发。"""
                if scripted["headers"]:
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("transfer-encoding", "chunked")
                    self.end_headers()
                    self.wfile.flush()
                if not api.hold.wait(self.connection):
                    self.close_connection = True
                    return None
                if not scripted["headers"]:
                    return scripted["then"]
                for event in _events(scripted["then"]):
                    data = _sse(event)
                    self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
                return None

            def do_GET(self) -> None:  # 模型列表（"同步模型"用）
                api.headers.append({key.lower(): value for key, value in self.headers.items()})
                models = [{"type": "model", "id": name, "display_name": name, "created_at": "2026-01-01T00:00:00Z"}
                          for name in api.models]
                payload = json.dumps({"data": models, "has_more": False, "first_id": None, "last_id": None}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self

    def stop(self) -> None:
        self.hold.close()  # 还压着的请求放走，不然它们的线程一直等着
        self.server.shutdown()
        self.server.server_close()
