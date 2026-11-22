"""本地的假"语音转文字"服务，只在测试和演示里用。不连真正的 OpenAI，一分钱都不花。

它像 OpenAI 的转写接口一样回答 POST …/audio/transcriptions（multipart/form-data：file、model、response_format），
回答 {"text": …, "usage": {"type": "duration", "seconds": N}}（https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create）。
也像真的一样检查：要有 file 和 model、文件名要带能认的扩展名、不超过 25 MB；key 不是 None 时要带着
Authorization: Bearer <key>，不对回答 401。收到的每个请求都记下来（头、字段、文件），测试拿来检查发出去的是什么。

replies 是一串事先写好的回答（按顺序用，用完了回答默认的 text）：{"status": 500, "body": {...}} 这样。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from multipart_form import is_multipart, parse

FORMATS = ("flac", "mp3", "mp4", "mpeg", "mpga", "m4a", "ogg", "wav", "webm")
MAX_BYTES = 25 * 1024 * 1024


def error(status: int, message: str, kind: str = "invalid_request_error") -> dict:
    """一个出错的回答（OpenAI 的格式）。"""
    return {"status": status, "body": {"error": {"message": message, "type": kind, "param": None, "code": None}}}


class FakeTranscribe:
    def __init__(self, key: str | None = "sk-test-openai-key", text: str = "明天早上八点提醒我开会", seconds: float | None = 7.0):
        self.key = key  # None：不检查 Authorization（扮演你自己运行的、不要密钥的兼容服务）
        self.text = text
        self.seconds = seconds
        self.replies: list[dict] = []
        self.requests: list[dict] = []  # {"path", "headers", "fields", "files"}
        self._lock = threading.Lock()

    def start(self) -> FakeTranscribe:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                path = urlsplit(self.path).path
                headers = {name.lower(): value for name, value in self.headers.items()}
                fields: dict = {}
                files: dict = {}
                if is_multipart(headers.get("content-type")):
                    try:
                        fields, files = parse(headers["content-type"], body)
                    except ValueError as exc:
                        self._reply(400, error(400, f"bad multipart: {exc}")["body"])
                        return
                with fake._lock:
                    fake.requests.append({"path": path, "headers": headers, "fields": fields, "files": files})
                    scripted = fake.replies.pop(0) if fake.replies else None
                if not path.endswith("/audio/transcriptions"):
                    self._reply(404, error(404, "Not found")["body"])
                    return
                if fake.key is not None and headers.get("authorization") != f"Bearer {fake.key}":
                    self._reply(401, error(401, "Incorrect API key provided.", "invalid_api_key")["body"])
                    return
                if scripted is not None:
                    self._reply(scripted["status"], scripted["body"])
                    return
                file = files.get("file")
                if file is None or not fields.get("model"):
                    self._reply(400, error(400, "file and model are required")["body"])
                    return
                if file["filename"].rsplit(".", 1)[-1].lower() not in FORMATS:
                    self._reply(400, error(400, f"Invalid file format. Supported formats: {list(FORMATS)}")["body"])
                    return
                if len(file["data"]) > MAX_BYTES:
                    self._reply(413, error(413, "Maximum content size limit exceeded.")["body"])
                    return
                payload: dict = {"text": fake.text}
                if fake.seconds is not None:
                    payload["usage"] = {"type": "duration", "seconds": fake.seconds}
                self._reply(200, payload)

            def _reply(self, status: int, payload: dict) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except OSError:
                    pass

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
