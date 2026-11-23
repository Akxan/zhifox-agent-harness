"""假的 GitHub：只回答"检查新版本"问的那一个地址（GET /repos/Akxan/zhifox-agent-harness/releases/latest，见 harness/updates.py）。

测试和演示（tests/demo_web.py）把 HARNESS_UPDATE_API（Config.update_api）指向它，真的 api.github.com 一个请求都收不到。
它记下收到的每一个请求（路径和请求头），测试拿它看"关着的时候一个请求都没发""一天只问一次""没带 cookie、版本号"。

回答什么由测试定：
  version      tag 是 v 加上它；None 表示还没有任何正式版本（回答 404）
  prerelease、draft   这一版是不是预发布、草稿
  status       不是 200 的话照它回答（403 加上 x-ratelimit-remaining: 0 就是被限流了）
  raw          原样回答这些字节（看不懂的回答）
  delay        回答之前先等这么多秒（超时）
  wheel        附件里有没有那个 wheel
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from harness.install import wheel_name
from harness.updates import SLUG

LATEST = f"/repos/{SLUG}/releases/latest"


class FakeGitHub:
    def __init__(self, version: str | None = "0.3.0", *, prerelease: bool = False, draft: bool = False, status: int = 200,
                 raw: bytes | None = None, delay: float = 0.0, wheel: bool = True, headers: dict[str, str] | None = None):
        self.requests: list[dict] = []
        self.answer(version, prerelease=prerelease, draft=draft, status=status, raw=raw, delay=delay, wheel=wheel, headers=headers)
        self._lock = threading.Lock()
        self.server: ThreadingHTTPServer | None = None
        self.base_url = ""

    def answer(self, version: str | None = "0.3.0", *, prerelease: bool = False, draft: bool = False, status: int = 200,
               raw: bytes | None = None, delay: float = 0.0, wheel: bool = True, headers: dict[str, str] | None = None) -> None:
        """以后的请求怎么回答（参数的意思见开头）。"""
        self.version, self.prerelease, self.draft, self.status = version, prerelease, draft, status
        self.raw, self.delay, self.wheel, self.headers = raw, delay, wheel, dict(headers or {})

    @property
    def count(self) -> int:
        return len(self.requests)

    def release(self) -> dict:
        tag = f"v{self.version}"
        assets = [{"name": wheel_name(str(self.version)), "browser_download_url": f"https://example.invalid/{tag}.whl"}] if self.wheel else []
        return {
            "tag_name": tag, "name": f"ZhiFox {self.version}", "draft": self.draft, "prerelease": self.prerelease,
            "published_at": "2026-12-01T09:00:00Z", "assets": assets,
            # 故意给一个不是 github.com 的地址：程序应该自己照着 tag 拼发布页，不用回答里的网址
            "html_url": "https://evil.example/phish", "body": "Release notes",
        }

    def start(self) -> FakeGitHub:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                with fake._lock:
                    fake.requests.append({"path": self.path, "headers": {name.lower(): value for name, value in self.headers.items()}})
                if fake.delay:
                    time.sleep(fake.delay)
                if self.path != LATEST:
                    self._send(404, b'{"message": "Not Found"}')
                elif fake.raw is not None:
                    self._send(fake.status, fake.raw)
                elif fake.status != 200:
                    self._send(fake.status, json.dumps({"message": "API rate limit exceeded"}).encode(), fake.headers)
                elif fake.version is None:
                    self._send(404, b'{"message": "Not Found"}')
                else:
                    self._send(200, json.dumps(fake.release()).encode(), fake.headers)

            def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    for name, value in (headers or {}).items():
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass  # 等太久、客户端已经走了（超时的测试）

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True, name="fake-github").start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
