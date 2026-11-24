"""本地的假"OpenAI 格式"服务器，只在测试里用。

GPT、Gemini、DeepSeek 都用这套格式（Chat Completions），所以一个假服务器就能代表它们三家。
和 fake_api.py 一样：按顺序回放事先写好的回复，并记下收到的每一个请求。
测"停止"也一样：held.held(chat(...)) 先压着不回答，api.hold.release() 放行（见 held.py）；keep_alive=True 按 HTTP/1.1 回答。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from held import Hold


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


def failure(status: int = 500, message: str = "server error", code: str | None = None) -> dict:
    """一次失败的请求：服务器不发流式数据，直接返回这个状态码和出错说明。
    code：错误里的 code（比如 429 的 insufficient_quota：账号额度用完了，和"请求太频繁"不是一回事）。"""
    return {"error": {"status": status, "message": message, "code": code}}


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
    """replies 是按顺序回放的回复，models 是 /models 列出的模型名。

    下面几个选项模拟"照着 OpenAI 格式做、但没做全"的服务器（自定义模型接口，见 harness/endpoints/）：
      listing=False         没有 /models（回答 404），模型名要自己填
      tools=False           请求里带着 tools 就回答 400（Ollama 对用不了工具的模型就是这样说的）
      stream_options=False  请求里带着 stream_options 就回答 400（老版本的 llama.cpp、一些网关）
      images=False          用户消息里有图片就回答 400
      ollama={模型名: {...}} 像 Ollama 那样多回答 /api/version 和 /api/show（{...} 是 /api/show 的回答）
      lmstudio=[...]        像 LM Studio 那样多回答 /api/v0/models（[...] 是它的 data）
      redirect="地址"        /models 回答 307，转到这个地址（智狐不该跟过去）
      listing_extra={模型名: {...}}  /models 里每个模型多带的字段（vLLM 的 max_model_len 这类）
    每个请求的请求头都记在 headers 里（测别的几家的密钥有没有被带过来）。
    """

    def __init__(
        self,
        replies: list[dict],
        models: list[str] | None = None,
        *,
        listing: bool = True,
        tools: bool = True,
        stream_options: bool = True,
        images: bool = True,
        ollama: dict[str, dict] | None = None,
        lmstudio: list[dict] | None = None,
        redirect: str = "",
        listing_extra: dict[str, dict] | None = None,
        keep_alive: bool = False,
    ):
        self.replies = list(replies)
        self.keep_alive = keep_alive
        self.hold = Hold()  # 压着不回答的那几条回复（见 held.py）
        self.models = models or ["test-model"]
        self.requests: list[dict] = []
        self.keys: list[str] = []  # 每个请求带来的 API key
        self.headers: list[dict[str, str]] = []  # 每个请求的请求头（名字都是小写）
        self.paths: list[str] = []  # 每个请求的路径
        self.listing, self.tools, self.stream_options, self.images = listing, tools, stream_options, images
        self.ollama, self.lmstudio, self.redirect = ollama, lmstudio, redirect
        self.listing_extra = listing_extra or {}

    def start(self) -> FakeOpenAI:
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1" if api.keep_alive else "HTTP/1.0"

            def _key_ok(self) -> bool:
                api.headers.append({name.lower(): value for name, value in self.headers.items()})
                api.paths.append(self.path)
                key = self.headers.get("Authorization", "").removeprefix("Bearer ")
                api.keys.append(key)
                if not key.startswith("bad-key"):
                    return True
                self._json(401, {"error": {"message": f"Incorrect API key provided: {key}", "type": "invalid_request_error"}})
                return False

            def _json(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _missing(self) -> None:
                self._json(404, {"error": {"message": f"404 page not found: {self.path}", "type": "not_found"}})

            def do_GET(self) -> None:  # 模型列表（还有 Ollama、LM Studio 自己的几个地址）
                if not self._key_ok():
                    return
                if self.path == "/api/version":
                    return self._json(200, {"version": "0.12.0"}) if api.ollama is not None else self._missing()
                if self.path == "/api/v0/models":
                    return self._json(200, {"object": "list", "data": api.lmstudio}) if api.lmstudio is not None else self._missing()
                if api.redirect:
                    self.send_response(307)
                    self.send_header("location", api.redirect)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                if not api.listing:
                    return self._missing()
                data = [{"id": name, "object": "model", "created": 0, "owned_by": "test", **api.listing_extra.get(name, {})} for name in api.models]
                self._json(200, {"object": "list", "data": data})

            def do_POST(self) -> None:  # 对话
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                if not self._key_ok():
                    return
                if self.path == "/api/show":
                    shown = (api.ollama or {}).get(body.get("model"))
                    return self._json(200, shown) if shown is not None else self._missing()
                refusal = api._refusal(body)
                if refusal:
                    api.requests.append(body)
                    return self._json(400, {"error": {"message": refusal, "type": "invalid_request_error"}})
                api.requests.append(body)
                if not api.replies:
                    self._json(500, {"error": {"message": "no scripted reply left", "type": "server_error"}})
                    return
                scripted = api.replies.pop(0)
                if scripted.get("held"):
                    scripted = self._held(scripted)
                    if scripted is None:
                        return
                if "error" in scripted:
                    problem = scripted["error"]
                    error = {"message": problem["message"], "type": "server_error"}
                    if problem.get("code"):
                        error["code"] = problem["code"]
                    self._json(problem["status"], {"error": error})
                    return
                lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in _chunks(scripted)]
                payload = "".join([*lines, "data: [DONE]\n\n"]).encode()
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _held(self, scripted: dict) -> dict | None:
                """先压着（见 held.py）。对方断开了返回 None；放行了的话，头已经发了的把数据块接着发完、返回 None，
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
                for line in [*(f"data: {json.dumps(chunk)}\n\n" for chunk in _chunks(scripted["then"])), "data: [DONE]\n\n"]:
                    data = line.encode()
                    self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
                return None

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self

    def _refusal(self, body: dict) -> str:
        """这个请求会不会被拒绝（模拟没做全的服务器，见 __init__ 的说明）。拒绝的话返回出错说明。"""
        if not self.stream_options and "stream_options" in body:
            return "json: unknown field \"stream_options\""
        if not self.tools and body.get("tools"):
            return f"registry.ollama.ai/library/{body.get('model')} does not support tools"
        if not self.images:
            for message in body.get("messages", []):
                content = message.get("content")
                if isinstance(content, list) and any(part.get("type") == "image_url" for part in content if isinstance(part, dict)):
                    return "image input is not supported by this model"
        return ""

    def stop(self) -> None:
        self.hold.close()  # 还压着的请求放走，不然它们的线程一直等着
        self.server.shutdown()
        self.server.server_close()
