"""读网页（harness/fetch/）测试用的东西：一个本地的假网站，和一个只放行它的 Fetcher。

为什么要"只放行它的 Fetcher"：假网站只能在 127.0.0.1 上，而真的规矩一定会拦下 127.0.0.1（这正是我们要的）。
所以测内容、跳转、上限这些的时候，换一个 policy：只有 127.0.0.1 放行，别的地址照真的规矩（guard.refusal）判断；
再换一个假的 DNS：测试里起的几个名字（site.test……）解析到 127.0.0.1，private.test 这种解析到局域网的地址。
真的规矩本身另有测试，只拿地址本身测（test_fetch_guard.py），真的 DNS 在 conftest 里整个不许用。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from harness.fetch import guard
from harness.fetch.client import Fetcher

LOCAL = "127.0.0.1"
# 一个回答：（状态码，头，正文），或者一个自己写回答的函数（慢慢发、断开连接……）
Route = tuple[int, dict[str, str], bytes] | Callable[[BaseHTTPRequestHandler], None]


def page(body: str | bytes, status: int = 200, content_type: str = "text/html; charset=utf-8", **headers: str) -> tuple[int, dict[str, str], bytes]:
    data = body.encode("utf-8") if isinstance(body, str) else body
    return status, {"Content-Type": content_type, **{name.replace("_", "-"): value for name, value in headers.items()}}, data


def redirect(location: str, status: int = 302) -> tuple[int, dict[str, str], bytes]:
    return status, {"Location": location}, b""


class FakeSite:
    """本地的假网站。routes：路径（不带 ? 后面的）→ 回答。记下收到的每个请求（路径和头）。"""

    def __init__(self, routes: dict[str, Route] | None = None):
        self.routes: dict[str, Route] = dict(routes or {})
        self.requests: list[dict] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                site.requests.append({"path": self.path, "headers": {name.lower(): value for name, value in self.headers.items()}})
                route = site.routes.get(self.path.split("?", 1)[0])
                if route is None:
                    route = page("<h1>Not found</h1>", status=404)
                if callable(route):
                    route(self)
                    return
                status, headers, body = route
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                if not any(name.lower() == "content-length" for name in headers):
                    self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer((LOCAL, 0), Handler)
        self._server.daemon_threads = True

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def url(self, path: str = "/", host: str = "site.test") -> str:
        return f"http://{host}:{self.port}{path}"

    def start(self) -> FakeSite:
        # poll_interval 小一点：stop 时 shutdown 要等它转完一圈（默认半秒，每个测试的收尾都要多等半秒）
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True, name="fake-site").start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def fetcher(*hosts: str, extra: dict[str, list[str]] | None = None, **options: object) -> Fetcher:
    """连本地假网站的 Fetcher：hosts 里的名字都解析到 127.0.0.1；extra 是另外几个名字 → 地址（测跳转到局域网）。
    放行的只有 127.0.0.1，别的地址照真的规矩判断。resolved 记着每次解析了谁（看 DNS 只问了一次）。"""
    table = {host: [LOCAL] for host in hosts} | dict(extra or {})
    resolved: list[str] = []

    def resolve(host: str, port: int) -> list[str]:
        resolved.append(host)
        if host not in table:
            raise OSError(f"假的 DNS 里没有 {host}")
        return list(table[host])

    def policy(address: str) -> str | None:
        return None if address == LOCAL else guard.refusal(address)

    made = Fetcher(resolve=resolve, policy=policy, **options)  # options：测试只传 timeout、limit、redirects
    made.resolved = resolved  # 只在测试里看

    return made
