"""本地的假"OpenAI 格式"服务器，只在测试里用。

GPT、Gemini、DeepSeek 都用这套格式（Chat Completions），所以一个假服务器就能代表它们三家。
和 fake_api.py 一样：按顺序回放事先写好的回复，并记下收到的每一个请求。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def call(id: str, name: str, arguments: str, **extra) -> dict:
    """模型的一次工具调用。arguments 是 JSON 文本（这套格式里参数就是文本）。"""
    return {"id": id, "name": name, "arguments": arguments, "extra": extra}


def chat(
    text: str = "",
    *,
    reasoning: str = "",
    calls: list[dict] | None = None,
    finish: str | None = None,
    usage: dict | None = None,
) -> dict:
    return {
        "text": text,
        "reasoning": reasoning,
        "calls": calls or [],
        "finish": finish or ("tool_calls" if calls else "stop"),
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


def _chunks(scripted: dict) -> list[dict]:
    """把一条写好的回复拆成真实服务器会发出的数据块序列。"""

    def chunk(delta: dict, finish: str | None = None) -> dict:
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "test-model",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    chunks = [chunk({"role": "assistant", "content": ""})]
    if scripted["reasoning"]:
        chunks.append(chunk({"reasoning_content": scripted["reasoning"]}))
    if scripted["text"]:
        half = len(scripted["text"]) // 2
        chunks += [chunk({"content": part}) for part in (scripted["text"][:half], scripted["text"][half:]) if part]
    for index, one in enumerate(scripted["calls"]):
        half = len(one["arguments"]) // 2
        # 第一块带着 id 和名字，后面的块只有参数的片段：真实服务器就是这样把一次调用拆开发的
        first = {"index": index, "id": one["id"], "type": "function", **one["extra"]}
        first["function"] = {"name": one["name"], "arguments": one["arguments"][:half]}
        chunks.append(chunk({"tool_calls": [first]}))
        chunks.append(chunk({"tool_calls": [{"index": index, "function": {"arguments": one["arguments"][half:]}}]}))
    chunks.append(chunk({}, scripted["finish"]))
    chunks.append({**chunk({}), "choices": [], "usage": scripted["usage"]})  # 最后单独一块用量
    return chunks


class FakeOpenAI:
    def __init__(self, replies: list[dict], models: list[str] | None = None):
        self.replies = list(replies)
        self.models = models or ["test-model"]
        self.requests: list[dict] = []
        self.keys: list[str] = []  # 每个请求带来的 API key

    def start(self) -> FakeOpenAI:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def _key_ok(self) -> bool:
                key = self.headers.get("Authorization", "").removeprefix("Bearer ")
                api.keys.append(key)
                if not key.startswith("bad-key"):
                    return True
                self._json(401, {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error"}})
                return False

            def _json(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # 模型列表
                if self._key_ok():
                    data = [{"id": name, "object": "model", "created": 0, "owned_by": "test"} for name in api.models]
                    self._json(200, {"object": "list", "data": data})

            def do_POST(self) -> None:  # 对话
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                if not self._key_ok():
                    return
                api.requests.append(body)
                if not api.replies:
                    self._json(500, {"error": {"message": "no scripted reply left", "type": "server_error"}})
                    return
                lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in _chunks(api.replies.pop(0))]
                payload = "".join([*lines, "data: [DONE]\n\n"]).encode()
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
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
