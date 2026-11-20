"""本地的假 Anthropic API，只在测试里用。

它按顺序回放事先写好的回复（格式和真实 API 的流式响应一样），并记下收到的每一个请求。
这样不需要 API key、不花钱，就能让真正的 SDK 把整个 agent 循环跑一遍，
还能检查我们发出去的请求长什么样。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def text(value: str) -> dict:
    return {"type": "text", "text": value}


def thinking(value: str, signature: str = "sig-1") -> dict:
    return {"type": "thinking", "thinking": value, "signature": signature}


def tool_use(id: str, name: str, input: dict) -> dict:
    return {"type": "tool_use", "id": id, "name": name, "input": input}


def reply(*blocks: dict, stop_reason: str = "end_turn", stop_details: dict | None = None) -> dict:
    return {"blocks": list(blocks), "stop_reason": stop_reason, "stop_details": stop_details}


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
        elif block["type"] == "thinking":
            start = {"type": "thinking", "thinking": "", "signature": ""}
            deltas = [
                {"type": "thinking_delta", "thinking": block["thinking"]},
                {"type": "signature_delta", "signature": block["signature"]},
            ]
        elif block["type"] == "tool_use":
            start = {**block, "input": {}}
            deltas = [{"type": "input_json_delta", "partial_json": json.dumps(block["input"])}]
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


class FakeAPI:
    def __init__(self, replies: list[dict]):
        self.replies = list(replies)
        self.requests: list[dict] = []  # 收到的请求体
        self.headers: list[dict] = []  # 收到的请求头（键全部小写）
        self.models = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-3-old"]  # 假装服务器上有这些模型

    def start(self) -> FakeAPI:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                api.requests.append(body)
                api.headers.append({key.lower(): value for key, value in self.headers.items()})
                if not api.replies:
                    self.send_error(500, "no scripted reply left")
                    return
                payload = "".join(
                    f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in _events(api.replies.pop(0))
                ).encode()
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

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
        self.server.shutdown()
        self.server.server_close()
