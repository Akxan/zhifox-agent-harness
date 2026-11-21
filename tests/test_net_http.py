"""harness/net/http.py 的测试："发一个 JSON 过去，收一个 JSON 回来"的帮手，和下载文件。

对着本地真的 HTTP 服务器（ThreadingHTTPServer，127.0.0.1 上随便一个端口），走的是 urllib 真实的代码路径：
请求怎么编码、4xx 怎么原样交回来、太大的回答怎么拒绝、连不上时说明里有没有漏出密钥、
重定向到别的主机时 token 会不会跟过去（127.0.0.1 和 localhost 是两个不同的主机名，正好用来测"换了域名"）。
也有一个只会胡乱回答的 TCP 服务器，测"对方回的根本不是 HTTP"。不连外面的任何服务。
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from harness.net import http
from harness.net.http import NetError, Reply

SECRET = "xoxb-SECRET-0123456789"  # 写在网址、头和回答里：出错的说明里不能有它


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    """这些测试只连本机：不管这台电脑上设了什么代理都不走。_OPENER 是导入时建好的（那时就读了代理设置），
    改环境变量影响不到它，所以换一个不带代理、其余一模一样（同一个重定向处理）的。"""
    monkeypatch.setattr(http, "_OPENER", urllib.request.build_opener(urllib.request.ProxyHandler({}), http._KeepSecretsHome))


def answer(handler: BaseHTTPRequestHandler, status: int, payload: object = None, *, raw: bytes | None = None, headers: dict | None = None) -> None:
    """回一个完整的回答：payload 编码成 JSON（或者 raw 原样），带上 Content-Length。"""
    data = raw if raw is not None else (b"" if payload is None else json.dumps(payload).encode())
    handler.send_response(status)
    if raw is None and payload is not None:
        handler.send_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def redirect(location: str, status: int = 302):
    return lambda handler: answer(handler, status, raw=b"", headers={"Location": location})


class Site:
    """本地的假服务器。routes 按路径写回答：一个函数 fn(handler)。记下每一个请求（方法、路径、头、正文）。"""

    def __init__(self):
        self.routes: dict = {}
        self.requests: list[SimpleNamespace] = []
        self.release = threading.Event()  # 故意卡住不回答的路由等它（测试结束时放开）
        site = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                site.requests.append(SimpleNamespace(method=self.command, path=self.path, headers=self.headers, body=body))
                route = site.routes.get(urlsplit(self.path).path)
                try:
                    if route is None:
                        answer(self, 404, {"message": "Not Found"})
                    else:
                        route(self)
                except OSError:
                    self.close_connection = True  # 客户端已经放弃了（超时、嫌太大）：这正是有的测试想要的

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def url(self, path: str, host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}{path}"

    def stop(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def site():
    made = Site()
    yield made
    made.stop()


class Garbage:
    """一个只会胡乱回答的 TCP 服务器：读完请求头，回 reply（可以是空的：什么也不说就断开），然后断开。"""

    def __init__(self, reply: bytes):
        self.reply = reply
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\r\n\r\n" not in data and (chunk := conn.recv(4096)):
                    data += chunk
                try:
                    conn.sendall(self.reply)
                except OSError:
                    pass

    def stop(self) -> None:
        self.listener.close()


@pytest.fixture
def garbage():
    made: list[Garbage] = []

    def make(reply: bytes) -> Garbage:
        made.append(Garbage(reply))
        return made[-1]

    yield make
    for item in made:
        item.stop()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


GARBAGE_REPLIES = {
    "not-http": b"SSH-2.0-OpenSSH_9.6\r\n\r\n",  # http.client 的 BadStatusLine：既不是 OSError，urllib 也不包一层
    "endless-header": b"HTTP/1.1 200 OK\r\nX-Long: " + b"a" * 70_000 + b"\r\n\r\n",  # LineTooLong
    "hang-up": b"",  # 什么也不说就断开（RemoteDisconnected）
}


# ---- request：回答原样交回来 ------------------------------------------------------


def test_a_2xx_reply_is_parsed(site):
    site.routes["/api/users/@me"] = lambda handler: answer(handler, 200, {"id": "1", "name": "智狐"}, headers={"X-RateLimit-Remaining": "4"})
    reply = http.request("GET", site.url("/api/users/@me"), headers={"Authorization": "Bot fake-token"})
    assert reply.ok
    assert reply.status == 200
    assert reply.data == {"id": "1", "name": "智狐"}
    assert reply.headers["x-ratelimit-remaining"] == "4"
    assert reply.headers["content-type"] == "application/json"
    assert all(name == name.lower() for name in reply.headers)
    assert json.loads(reply.text) == reply.data
    sent = site.requests[-1]
    assert sent.method == "GET"
    assert sent.body == b""
    assert sent.headers["Authorization"] == "Bot fake-token"
    assert sent.headers["User-Agent"] == http.USER_AGENT
    assert sent.headers["Accept"] == "application/json"
    assert "Content-Type" not in sent.headers  # 没有正文就不写正文的类型


@pytest.mark.parametrize(
    "status, payload, data",
    [
        (400, {"code": 50035, "message": "Invalid Form Body"}, {"code": 50035, "message": "Invalid Form Body"}),
        (401, {"message": "401: Unauthorized", "code": 0}, {"message": "401: Unauthorized", "code": 0}),
        (404, {"message": "Unknown Channel", "code": 10003}, {"message": "Unknown Channel", "code": 10003}),
        (500, b"Internal Server Error", None),
        (503, b"", None),
    ],
)
def test_4xx_and_5xx_replies_are_returned_not_raised(site, status, payload, data):
    """各家报错的方式都不一样，由平台的代码判断：这里只管原样交回去。"""
    if isinstance(payload, bytes):
        site.routes["/x"] = lambda handler: answer(handler, status, raw=payload)
    else:
        site.routes["/x"] = lambda handler: answer(handler, status, payload)
    reply = http.request("POST", site.url("/x"), body={"content": "你好"})
    assert reply.status == status
    assert not reply.ok
    assert reply.data == data
    if isinstance(payload, bytes):
        assert reply.text == payload.decode()


def test_a_429_says_how_long_to_wait(site):
    site.routes["/limited"] = lambda handler: answer(handler, 429, {"retry_after": 1.5, "global": False}, headers={"Retry-After": "2"})
    reply = http.request("POST", site.url("/limited"), body={})
    assert reply.status == 429
    assert reply.retry_after() == 2.0
    assert reply.data["retry_after"] == 1.5


@pytest.mark.parametrize(
    "value, seconds",
    [("2.5", 2.5), ("0", 0.0), ("-3", 0.0), ("soon", 0.0), ("Wed, 21 Oct 2015 07:28:00 GMT", 0.0), ("inf", 0.0), ("nan", 0.0), ("1e999", 0.0), (None, 0.0)],
)
def test_retry_after_is_a_sane_number_of_seconds(value, seconds):
    """读不懂的、负的、无穷大的都当成没说：一个出了毛病的服务器不能让我们永远睡下去。"""
    reply = Reply(429, None, {} if value is None else {"retry-after": value})
    assert reply.retry_after() == seconds


@pytest.mark.parametrize("raw", [b"<html>oops</html>", b"", b"   \n", b'{"broken": '], ids=["html", "empty", "blank", "cut-json"])
def test_a_body_that_is_not_json_gives_data_none(site, raw):
    site.routes["/page"] = lambda handler: answer(handler, 200, raw=raw)
    reply = http.request("GET", site.url("/page"))
    assert reply.ok
    assert reply.data is None
    assert reply.text == raw.decode()


def test_the_text_of_a_reply_is_short_and_hides_secrets(site):
    site.routes["/echo"] = lambda handler: answer(handler, 400, raw=f"bad token {SECRET} ".encode() + b"x" * 1000)
    reply = http.request("GET", site.url("/echo"), hide=(SECRET,))
    assert SECRET not in reply.text
    assert reply.text.startswith("bad token <hidden> ")
    assert len(reply.text) <= 300


# ---- request：请求怎么编码 -------------------------------------------------------


def test_a_json_body_survives_half_an_emoji_and_nan(site):
    """模型写的文字里可能有半个表情（落单的代理字符）：直接 json.dumps(ensure_ascii=False).encode() 会失败，NaN 会写出不合法的 JSON。"""
    site.routes["/send"] = lambda handler: answer(handler, 200, {"ok": True})

    def refuse(name):
        raise ValueError(f"{name} 不是合法的 JSON")

    reply = http.request("POST", site.url("/send"), body={"text": "半个表情\ud83d", "score": float("nan"), "list": [1, 2]})
    assert reply.data == {"ok": True}
    sent = site.requests[-1]
    assert sent.headers["Content-Type"] == "application/json; charset=utf-8"
    sent.body.decode("ascii")  # 全部转义成 \uXXXX：只有 ASCII，任何一边编解码都不会出错
    received = json.loads(sent.body, parse_constant=refuse)  # 有 NaN、Infinity 的话 parse_constant 会抛错
    assert received["text"].startswith("半个表情")
    assert received["list"] == [1, 2]
    assert received["score"] == "NaN"


def test_the_callers_headers_win(site):
    site.routes["/patch"] = lambda handler: answer(handler, 200, {})
    http.request("PATCH", site.url("/patch"), body={"a": 1}, headers={"Content-Type": "application/merge-patch+json", "User-Agent": "Custom/1.0", "Accept": "*/*"})
    sent = site.requests[-1]
    assert sent.method == "PATCH"
    assert sent.headers["Content-Type"] == "application/merge-patch+json"
    assert sent.headers["User-Agent"] == "Custom/1.0"
    assert sent.headers["Accept"] == "*/*"
    assert json.loads(sent.body) == {"a": 1}


def test_a_form_body_is_url_encoded(site):
    site.routes["/token"] = lambda handler: answer(handler, 200, {"access_token": "t"})
    form = {"grant_type": "client_credentials", "note": "a b&c=d 中文"}
    reply = http.request("POST", site.url("/token"), form=form)
    assert reply.data == {"access_token": "t"}
    sent = site.requests[-1]
    assert sent.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert {name: values[0] for name, values in parse_qs(sent.body.decode()).items()} == form


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_other_methods_go_through(site, method):
    site.routes["/thing"] = lambda handler: answer(handler, 204, raw=b"")
    reply = http.request(method, site.url("/thing"))
    assert (reply.status, reply.data, site.requests[-1].method) == (204, None, method)


# ---- request：连不上、超时、太大、看不懂 --------------------------------------------


def test_connection_refused_is_a_net_error_without_the_secret():
    url = f"http://127.0.0.1:{free_port()}/bot{SECRET}/getMe?token={SECRET}"
    with pytest.raises(NetError) as info:
        http.request("GET", url, hide=(SECRET,))
    assert "127.0.0.1" in str(info.value)
    assert SECRET not in str(info.value)


@pytest.mark.parametrize("url", [f"bot{SECRET}/getMe", f"ftp-ish:{SECRET}", "http://[::1/x", ""])
def test_a_bad_url_is_a_net_error_without_the_secret(url):
    """网址不对时 urllib 抛的是 ValueError，说明里带着整个网址：要换成 NetError，网址里的密钥也不能漏出去。"""
    with pytest.raises(NetError) as info:
        http.request("GET", url, hide=(SECRET,))
    assert SECRET not in str(info.value)


def test_a_server_that_does_not_answer_times_out(site):
    site.routes["/slow"] = lambda handler: site.release.wait(5)
    start = time.monotonic()
    with pytest.raises(NetError):
        http.request("GET", site.url(f"/slow?token={SECRET}"), timeout=0.2, hide=(SECRET,))
    assert time.monotonic() - start < 2


def test_a_body_that_stops_halfway_times_out(site):
    def stall(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "100")
        handler.end_headers()
        handler.wfile.write(b'{"partial": ')
        handler.wfile.flush()
        site.release.wait(5)

    site.routes["/stall"] = stall
    start = time.monotonic()
    with pytest.raises(NetError):
        http.request("GET", site.url("/stall"), timeout=0.2)
    assert time.monotonic() - start < 2


@pytest.mark.parametrize("status", [200, 400])
def test_a_reply_bigger_than_the_limit_is_refused(site, status):
    site.routes["/big"] = lambda handler: answer(handler, status, {"blob": "x" * 2000})
    with pytest.raises(NetError):
        http.request("GET", site.url("/big"), limit=1000)
    assert http.request("GET", site.url("/big"), limit=3000).status == status  # 没超就照常


@pytest.mark.parametrize("name", list(GARBAGE_REPLIES))
def test_a_reply_that_is_not_http_is_a_net_error(garbage, name):
    server = garbage(GARBAGE_REPLIES[name])
    with pytest.raises(NetError):
        http.request("GET", f"http://127.0.0.1:{server.port}/x", timeout=2)


# ---- 重定向 --------------------------------------------------------------------


CREDENTIALS = {"Authorization": f"Bearer {SECRET}", "Cookie": "d=session", "x-acs-dingtalk-access-token": SECRET}


def test_a_redirect_to_another_host_drops_every_credential(site):
    """Slack 的文件下载会重定向到 CDN：机器人的 token 不能跟过去。钉钉的 token 不在 Authorization 里，也不能跟过去。"""
    site.routes["/files/a.png"] = redirect(site.url("/cdn/a.png", host="localhost"))
    site.routes["/cdn/a.png"] = lambda handler: answer(handler, 200, raw=b"PNG...")
    assert http.download(site.url("/files/a.png"), headers=CREDENTIALS, limit=1000) == b"PNG..."
    first, landing = site.requests[-2:]
    assert first.headers["Authorization"] == f"Bearer {SECRET}"  # 第一跳是带着的
    assert landing.path == "/cdn/a.png"
    assert landing.headers["Host"].startswith("localhost")
    for name in CREDENTIALS:
        assert name not in landing.headers, name
    assert landing.headers["User-Agent"] == http.USER_AGENT  # 不是密钥的照常带着


def test_a_json_request_redirected_to_another_host_drops_credentials_too(site):
    site.routes["/api/old"] = redirect(site.url("/api/new", host="localhost"), status=301)
    site.routes["/api/new"] = lambda handler: answer(handler, 200, {"moved": True})
    reply = http.request("GET", site.url("/api/old"), headers=CREDENTIALS)
    assert reply.data == {"moved": True}
    landing = site.requests[-1]
    assert landing.headers["Accept"] == "application/json"
    for name in CREDENTIALS:
        assert name not in landing.headers, name


def test_a_redirect_on_the_same_host_keeps_the_token(site):
    site.routes["/api/old"] = redirect("/api/new")  # 相对的地址：还是同一个主机
    site.routes["/api/new"] = lambda handler: answer(handler, 200, {"moved": True})
    reply = http.request("GET", site.url("/api/old"), headers=CREDENTIALS)
    assert reply.data == {"moved": True}
    landing = site.requests[-1]
    assert landing.path == "/api/new"
    assert landing.headers["Authorization"] == f"Bearer {SECRET}"
    assert landing.headers["x-acs-dingtalk-access-token"] == SECRET


@pytest.mark.parametrize("target", ["http://example.com/steal", "ftp://127.0.0.1/file"])
def test_a_redirect_to_an_untrusted_address_is_not_followed(site, monkeypatch, target):
    """明文的 http://（不是本机）、ftp:// 都不跟：要是跟了，就是把请求不加密地发到外面去。"""
    connect = socket.create_connection

    def only_this_machine(address, *args, **kwargs):
        assert address[0] == "127.0.0.1", f"不该去连 {address}"
        return connect(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", only_this_machine)
    site.routes["/away"] = redirect(target)
    reply = http.request("GET", site.url("/away"), headers=CREDENTIALS)
    assert reply.status == 302  # 原样交回这个 302，由调用的人决定
    with pytest.raises(NetError) as info:
        http.download(site.url("/away"), limit=1000)
    assert "302" in str(info.value)


def test_the_redirect_rules_for_https():
    """这几种 https 的情况没法在本机起真的服务器，直接问重定向的处理器。"""
    handler = http._KeepSecretsHome()
    original = urllib.request.Request("https://slack.com/api/files.info", headers={"Authorization": f"Bearer {SECRET}", "X-Custom-Token": SECRET, "Accept": "application/json"})
    elsewhere = handler.redirect_request(original, None, 302, "Found", {}, "https://files-edge.slack-cdn.com/a.png")
    assert not elsewhere.has_header("Authorization")
    assert not elsewhere.has_header("X-custom-token")
    assert elsewhere.get_header("Accept") == "application/json"
    same = handler.redirect_request(original, None, 302, "Found", {}, "https://slack.com/files/a.png")
    assert same.get_header("Authorization") == f"Bearer {SECRET}"
    assert handler.redirect_request(original, None, 302, "Found", {}, "http://slack.com/files/a.png") is None  # 从 https 降到明文：不跟


# ---- download ------------------------------------------------------------------


def test_download_returns_the_bytes(site):
    content = bytes(range(256)) * 4
    site.routes["/file"] = lambda handler: answer(handler, 200, raw=content)
    assert http.download(site.url("/file"), limit=len(content)) == content  # 正好等于上限：可以
    assert site.requests[-1].headers["User-Agent"] == http.USER_AGENT


def test_download_fails_fast_when_content_length_is_over_the_limit(site):
    def huge(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "10000000")
        handler.end_headers()
        handler.wfile.write(b"x" * 10)
        handler.wfile.flush()
        site.release.wait(5)  # 剩下的永远不来：看到长度就该放弃，不能等着读

    site.routes["/huge"] = huge
    start = time.monotonic()
    with pytest.raises(NetError):
        http.download(site.url("/huge"), limit=1000, timeout=3)
    assert time.monotonic() - start < 1


def test_download_stops_reading_at_the_limit_without_content_length(site):
    def unannounced(handler):
        handler.send_response(200)
        handler.send_header("Connection", "close")  # 没有 Content-Length：正文一直到断开为止
        handler.end_headers()
        handler.wfile.write(b"x" * 5000)
        handler.close_connection = True

    site.routes["/stream"] = unannounced
    with pytest.raises(NetError):
        http.download(site.url("/stream"), limit=1000)
    assert http.download(site.url("/stream"), limit=5000) == b"x" * 5000


@pytest.mark.parametrize("status", [403, 404, 500])
def test_download_of_an_http_error_is_a_net_error(site, status):
    site.routes["/gone"] = lambda handler: answer(handler, status, {"message": "nope"})
    with pytest.raises(NetError) as info:
        http.download(site.url("/gone"), limit=1000)
    assert str(status) in str(info.value)


def test_download_that_cannot_connect_hides_secrets():
    with pytest.raises(NetError) as info:
        http.download(f"http://127.0.0.1:{free_port()}/file/bot{SECRET}/a.png", limit=1000, hide=(SECRET,))
    assert SECRET not in str(info.value)
    with pytest.raises(NetError) as info:
        http.download(f"bot{SECRET}/a.png", limit=1000, hide=(SECRET,))
    assert SECRET not in str(info.value)


@pytest.mark.parametrize("name", list(GARBAGE_REPLIES))
def test_download_of_a_reply_that_is_not_http_is_a_net_error(garbage, name):
    server = garbage(GARBAGE_REPLIES[name])
    with pytest.raises(NetError):
        http.download(f"http://127.0.0.1:{server.port}/file", limit=1000, timeout=2)


# ---- trusted、scrub ------------------------------------------------------------


@pytest.mark.parametrize(
    "url, ok",
    [
        ("https://cdn.discordapp.com/attachments/1/2/a.png", True),
        ("HTTPS://files.slack.com/x", True),
        ("http://127.0.0.1:8080/x", True),
        ("http://127.1.2.3/x", True),
        ("http://localhost/x", True),
        ("http://LOCALHOST:9/x", True),
        ("http://[::1]:80/x", True),
        ("http://example.com/x", False),
        ("http://127.example.com/x", False),  # 127. 开头的域名可以解析到任何地方
        ("http://127.0.0.1.nip.io/x", False),
        ("http://localhost.example.com/x", False),
        ("http://10.0.0.1/x", False),
        ("ftp://127.0.0.1/x", False),
        ("file:///etc/passwd", False),
        ("javascript:alert(1)", False),
        ("//example.com/x", False),
        ("http://[::1/x", False),
        ("", False),
    ],
)
def test_trusted_accepts_https_and_plain_http_only_to_this_machine(url, ok):
    assert http.trusted(url) is ok


def test_scrub_replaces_every_secret_longest_first():
    assert http.scrub("token=abcd-1234 again abcd-1234 and abcd", ["abcd", "abcd-1234"]) == "token=<hidden> again <hidden> and <hidden>"
    assert http.scrub("a short one: ab", ["ab", "", None]) == "a short one: ab"  # 太短、空的不算密钥（不然会把正常的字都换掉）
    assert http.scrub("nothing here", ()) == "nothing here"
