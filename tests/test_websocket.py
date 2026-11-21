"""手写的 WebSocket 客户端（harness/net/websocket.py）的测试。

服务器那一头是一个普通的 ThreadingHTTPServer，在同一条连接上用 tests/fake_ws.py 握手、收发帧，
所以客户端走的全是真实的代码路径：TCP、握手、掩码、拼分片、ping/pong、关闭握手、超时。
测试也会故意扮演一个坏服务器：回错的 Sec-WebSocket-Accept、发带掩码的帧、用不合规定的关闭代码、
只发一个声称有 1 TB 的帧头、不回关闭帧、直接断网……看客户端是不是都照 RFC 6455 应对。
代理那一节自己写了一个很小的 HTTP CONNECT 代理。全部只连本机，不花钱。

不用固定的 sleep 等事情发生：要等的用 until 轮询；要证明"很快就失败了"的，量一下用了多久。
"""

from __future__ import annotations

import base64
import json
import os
import queue
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import fake_ws
import pytest

from harness.net import websocket
from harness.net.websocket import (
    OP_BINARY,
    OP_CLOSE,
    OP_CONTINUATION,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    Address,
    ConnectionClosed,
    HandshakeError,
    WebSocket,
    WebSocketError,
    accept_key,
    frame,
    is_loopback,
    mask,
    parse_url,
)

SECRET = "SECRET-ticket-0123456789"  # 放在网址的查询部分里：任何一句出错的说明里都不能有它


# ---- 测试用的小工具 -------------------------------------------------------------


def until(condition, timeout: float = 3, what: str = "条件"):
    """等到 condition() 返回真值，并返回它。和 test_channels.until 一样；这里单独写一份，
    免得只为了它把整个聊天通道和网页服务器都导入进来（这个文件只测 harness/net）。"""
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value:
            return value
        assert time.monotonic() < deadline, f"等不到{what}"
        time.sleep(0.005)


class Background:
    """在另一个线程里调用 fn，之后用 result() 取回它的返回值（它抛出的异常会在 result() 里原样抛出）。"""

    def __init__(self, fn, *args, **kwargs):
        self._value = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(fn, args, kwargs), daemon=True)
        self._thread.start()

    def _run(self, fn, args, kwargs) -> None:
        try:
            self._value = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 —— 交给 result() 抛给测试
            self._error = exc

    def running(self) -> bool:
        return self._thread.is_alive()

    def result(self, timeout: float = 3):
        self._thread.join(timeout)
        assert not self._thread.is_alive(), "后台的线程没有按时结束"
        if self._error is not None:
            raise self._error
        return self._value


def closed(call) -> ConnectionClosed:
    """call() 必须抛出 ConnectionClosed；返回它，好检查代码和原因。"""
    with pytest.raises(ConnectionClosed) as info:
        call()
    return info.value


def close_frame_from(peer: fake_ws.ServerSocket, timeout: float = 2) -> tuple[int, bytes]:
    """服务器那一头：一直读到客户端发来的关闭帧，返回（代码，原因的原始字节）。中间的 pong 之类跳过。"""
    while True:
        _, opcode, payload = peer.recv_frame(timeout)
        if opcode == OP_CLOSE:
            return int.from_bytes(payload[:2], "big"), payload[2:]


def free_port() -> int:
    """一个现在没人在听的端口：连过去会被拒绝。"""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def no_network(*args, **kwargs):
    raise AssertionError("这一步不应该建立任何连接")


def handshake_head(handler: BaseHTTPRequestHandler, *extra: str) -> bytes:
    """一个正确的 101 回答（测试自己写回答、想在后面接点别的东西时用）。"""
    key = handler.headers["Sec-WebSocket-Key"]
    lines = ["HTTP/1.1 101 Switching Protocols", "Upgrade: websocket", "Connection: Upgrade", f"Sec-WebSocket-Accept: {accept_key(key)}", *extra]
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


class Server:
    """本地的 WebSocket 服务器那一头。

    默认每来一个连接就用 fake_ws.accept 握手（参数在 options 里：status、accept_key、extra），把 ServerSocket
    放进 sockets，然后一直拿着这条连接，直到测试结束（处理请求的函数一返回，http.server 就会把连接关掉）。
    想自己写回答（太长的头、不是 HTTP、握手后面紧跟着帧……）时，把 respond 设成一个函数 respond(handler)。
    """

    def __init__(self):
        self.options: dict = {}
        self.respond = None
        self.requests: list[SimpleNamespace] = []  # 每个请求：请求行、路径、头
        self.sockets: queue.Queue[fake_ws.ServerSocket] = queue.Queue()
        self.done = threading.Event()
        self.clients: list[WebSocket] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                server.requests.append(SimpleNamespace(line=self.requestline, path=self.path, headers=self.headers))
                self.close_connection = True
                if server.respond is not None:
                    try:
                        server.respond(self)
                        self.wfile.flush()
                    except OSError:
                        pass  # 客户端已经放弃、断开了：这正是有的测试想要的
                    return
                peer = fake_ws.accept(self, **server.options)
                if not peer.opened:
                    return
                server.sockets.put(peer)
                server.done.wait(10)

            def log_message(self, *args: object) -> None:
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http.daemon_threads = True
        self.port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()  # 关的时候不用等 0.5 秒

    def url(self, path: str = "/gw?v=10") -> str:
        return f"ws://127.0.0.1:{self.port}{path}"

    def connect(self, path: str = "/gw?v=10", **options) -> WebSocket:
        options.setdefault("timeout", 2)
        client = WebSocket.connect(self.url(path), **options)
        self.clients.append(client)
        return client

    def open(self, path: str = "/gw?v=10", **options) -> tuple[WebSocket, fake_ws.ServerSocket]:
        """连上，返回（客户端，服务器那一头）。"""
        client = self.connect(path, **options)
        return client, self.sockets.get(timeout=2)

    def stop(self) -> None:
        self.done.set()
        for client in self.clients:
            client.abort()
        self.http.shutdown()
        self.http.server_close()


@pytest.fixture
def server():
    srv = Server()
    yield srv
    srv.stop()


# ---- 握手 ----------------------------------------------------------------------


def test_the_handshake_request_follows_rfc_6455(server):
    _, peer = server.open("/gw?v=10&encoding=json", headers={"Authorization": "Bot fake-token", "User-Agent": "ZhiFox-test"})
    request = server.requests[0]
    assert request.line == "GET /gw?v=10&encoding=json HTTP/1.1"
    assert request.headers["Host"] == f"127.0.0.1:{server.port}"  # 不是默认端口：Host 里要写上
    assert request.headers["Upgrade"] == "websocket"
    assert request.headers["Connection"] == "Upgrade"
    assert request.headers["Sec-WebSocket-Version"] == "13"
    assert len(base64.b64decode(request.headers["Sec-WebSocket-Key"], validate=True)) == 16  # §4.1：16 个随机字节
    assert request.headers["Authorization"] == "Bot fake-token"  # 调用的人给的头原样带上
    assert request.headers["User-Agent"] == "ZhiFox-test"
    assert peer.path == "/gw?v=10&encoding=json"
    server.open()
    assert server.requests[1].headers["Sec-WebSocket-Key"] != request.headers["Sec-WebSocket-Key"]  # 每次都是新的随机数


def test_urls_are_split_into_address_host_header_and_target():
    assert parse_url("wss://gateway.discord.gg/?v=10&encoding=json") == Address(True, "gateway.discord.gg", 443, "/?v=10&encoding=json")
    assert parse_url("wss://gateway.discord.gg").target == "/"  # 没有路径：请求行里写 /
    assert parse_url("wss://example.com/x").authority == "example.com"  # 默认端口不写进 Host
    assert parse_url("wss://example.com:8443/x").authority == "example.com:8443"
    assert parse_url("ws://localhost/").port == 80
    assert parse_url("ws://localhost/").authority == "localhost"
    assert parse_url("ws://[::1]:9000/a").authority == "[::1]:9000"  # IPv6 的地址在 Host 里带方括号
    assert parse_url("WSS://Example.COM/Path").host == "example.com"
    assert parse_url("WSS://Example.COM/Path").target == "/Path"


def test_only_real_loopback_addresses_count_as_this_machine():
    for host in ("localhost", "LOCALHOST", "127.0.0.1", "127.1.2.3", "::1"):
        assert is_loopback(host), host
    # 127. 开头的域名可以解析到任何一台机器上：明文的 ws:// 不能因为名字长得像就放行
    for host in ("127.example.com", "127.0.0.1.nip.io", "localhost.example.com", "10.0.0.1", "example.com", "::2", ""):
        assert not is_loopback(host), host


@pytest.mark.parametrize(
    "name, value",
    [("X-Test", "a\r\nInjected: 1"), ("X-Test", "a\nInjected: 1"), ("X-Test", "a\rb"), ("X-Bad\r\nInjected", "1")],
    ids=["crlf-value", "lf-value", "cr-value", "crlf-name"],
)
def test_header_with_a_line_break_is_refused(server, name, value):
    with pytest.raises(WebSocketError):
        WebSocket.connect(server.url(), headers={name: value}, timeout=2)
    assert server.requests == []  # 请求一个字节都没有发出去，更不会多出一个 Injected 头


def test_a_wrong_accept_key_is_refused(server):
    server.options = {"accept_key": base64.b64encode(b"x" * 20).decode()}  # 看起来像，但不是 base64(sha1(key + GUID))
    with pytest.raises(HandshakeError) as info:
        WebSocket.connect(server.url(), timeout=2)
    assert info.value.status == 101


def test_a_refused_upgrade_reports_the_status_and_body(server):
    server.options = {"status": 401}
    with pytest.raises(HandshakeError) as info:
        WebSocket.connect(server.url(), timeout=2)
    assert info.value.status == 401
    assert json.loads(info.value.body) == {"error": "denied"}


def test_the_body_of_a_refusal_is_kept_even_when_it_arrives_after_the_head(server):
    """正文常常和头分两个包到：头读完的那一刻正文还没来，也要照着 Content-Length 把它等来。"""
    body = b'{"code": 4004, "message": "Authentication failed."}'

    def respond(handler):
        handler.wfile.write(f"HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode())
        handler.wfile.flush()
        time.sleep(0.05)  # 让头先单独到达客户端
        handler.wfile.write(body)

    server.respond = respond
    with pytest.raises(HandshakeError) as info:
        WebSocket.connect(server.url(), timeout=2)
    assert info.value.status == 403
    assert info.value.body == body.decode()


def test_a_refusal_without_a_body_does_not_wait(server):
    server.respond = lambda handler: (handler.wfile.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"), server.done.wait(5))
    start = time.monotonic()
    with pytest.raises(HandshakeError) as info:
        WebSocket.connect(server.url(), timeout=2)
    assert (info.value.status, info.value.body) == (404, "")
    assert time.monotonic() - start < 1


@pytest.mark.parametrize("extra", [{"Sec-WebSocket-Extensions": "permessage-deflate"}, {"Sec-WebSocket-Protocol": "chat"}], ids=["extension", "subprotocol"])
def test_an_extension_or_subprotocol_we_did_not_ask_for_is_refused(server, extra):
    server.options = {"extra": extra}
    with pytest.raises(HandshakeError):
        WebSocket.connect(server.url(), timeout=2)


def test_a_101_without_the_upgrade_headers_is_refused(server):
    def respond(handler):
        key = handler.headers["Sec-WebSocket-Key"]
        handler.wfile.write(f"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: {accept_key(key)}\r\n\r\n".encode())

    server.respond = respond
    with pytest.raises(HandshakeError):
        WebSocket.connect(server.url(), timeout=2)


@pytest.mark.parametrize("padding, finished", [(17_000, True), (1_000_000, False)], ids=["17KB-head", "endless-head"])
def test_a_handshake_reply_over_16_kb_is_refused(server, padding, finished):
    """17 KB：空行在最后一块里，读的循环来不及发现超长，头本身也要检查；没有尽头的头：读到 16 KB 就不读了。"""
    server.respond = lambda handler: handler.wfile.write(handshake_head(handler, f"X-Padding: {'a' * padding}")[: None if finished else -4])
    with pytest.raises(WebSocketError) as info:
        WebSocket.connect(server.url(), timeout=2)
    assert not isinstance(info.value, HandshakeError)


@pytest.mark.parametrize("reply", [b"SSH-2.0-OpenSSH_9.6\r\n\r\n", b"", b"HTTP/1.1 101 Switching"], ids=["not-http", "closed", "cut-off"])
def test_a_reply_that_is_not_http_or_stops_early_is_refused(server, reply):
    server.respond = lambda handler: handler.wfile.write(reply)
    with pytest.raises(WebSocketError):
        WebSocket.connect(server.url(), timeout=2)


def test_a_server_that_never_answers_times_out(server):
    server.respond = lambda handler: server.done.wait(5)
    start = time.monotonic()
    with pytest.raises(WebSocketError):
        WebSocket.connect(server.url(), timeout=0.2)
    assert time.monotonic() - start < 1.5


def test_frames_sent_together_with_the_handshake_are_not_lost(server):
    """服务器常把 101 和第一帧（比如 Discord 的 Hello）一起发：头后面多收到的字节要留着，当成第一帧的开头。"""

    def respond(handler):
        handler.wfile.write(handshake_head(handler) + frame(OP_TEXT, b'{"op": 10}') + frame(OP_BINARY, b"\x01\x02"))
        handler.wfile.flush()
        server.done.wait(5)

    server.respond = respond
    client = server.connect()
    assert client.recv(2) == '{"op": 10}'
    assert client.recv(2) == b"\x01\x02"


@pytest.mark.parametrize("url", ["ws://example.com/gateway", "ws://127.example.com/", "ws://10.0.0.1:8080/", "ws://localhost.example.com/"])
def test_plain_ws_to_another_machine_is_refused_before_connecting(monkeypatch, url):
    monkeypatch.setattr(socket, "create_connection", no_network)
    with pytest.raises(WebSocketError):
        WebSocket.connect(url)


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1/", "https://example.com/", "wss://", "ws:///path", "ws://127.0.0.1:99999/", "ws://127.0.0.1:port/", "ws://[::1/", "gateway.discord.gg", ""],
)
def test_bad_urls_are_refused_before_connecting(monkeypatch, url):
    monkeypatch.setattr(socket, "create_connection", no_network)
    with pytest.raises(WebSocketError):
        WebSocket.connect(url)


def test_a_host_name_that_cannot_be_encoded_is_a_websocket_error(monkeypatch):
    """a..b 这样的名字在 IDNA 编码时抛 UnicodeError（一种 ValueError）：调用的人只认 WebSocketError，漏出去会让重连的线程退出。"""
    monkeypatch.setattr(websocket, "_proxy_for", lambda address: None)
    for url in ("wss://a..b/", f"wss://{'a' * 64}.com/"):
        with pytest.raises(WebSocketError):
            WebSocket.connect(url, timeout=1)


def _failing_connections(server: Server):
    """各种连不上、握手失败的情况，网址里都带着 ?ticket=SECRET。每一项是（名字，准备，网址，超时）。"""

    def options(**values):
        return lambda: setattr(server, "options", values)

    def respond(fn):
        return lambda: setattr(server, "respond", fn)

    query = f"/gw?v=10&ticket={SECRET}"
    local = f"ws://127.0.0.1:{server.port}{query}"
    return [
        ("refused", lambda: None, f"ws://127.0.0.1:{free_port()}{query}", 2),
        ("wrong-accept", options(accept_key="nope"), local, 2),
        ("401", options(status=401), local, 2),
        ("extension", options(extra={"Sec-WebSocket-Extensions": "permessage-deflate"}), local, 2),
        ("too-long", respond(lambda handler: handler.wfile.write(handshake_head(handler, "X-Pad: " + "a" * 20_000))), local, 2),
        ("not-http", respond(lambda handler: handler.wfile.write(b"nope\r\n\r\n")), local, 2),
        ("timeout", respond(lambda handler: server.done.wait(5)), local, 0.2),
        ("plain-remote", lambda: None, f"ws://example.com{query}", 2),
        ("bad-port", lambda: None, f"ws://127.0.0.1:99999{query}", 2),
        ("bad-ipv6", lambda: None, f"ws://[::1{query}", 2),
    ]


def test_error_messages_never_contain_the_query_string(server):
    for name, prepare, url, timeout in _failing_connections(server):
        server.options, server.respond = {}, None
        prepare()
        with pytest.raises(WebSocketError) as info:
            WebSocket.connect(url, timeout=timeout)
        error = info.value
        for text in (str(error), repr(error), getattr(error, "body", "")):
            assert SECRET not in text, f"{name}: {text}"


# ---- 帧 ------------------------------------------------------------------------


def test_frame_layout_matches_the_examples_in_the_rfc():
    # §5.7 的例子
    assert frame(OP_TEXT, b"Hello") == bytes([0x81, 0x05]) + b"Hello"
    assert frame(OP_TEXT, b"Hello", key=bytes([0x37, 0xFA, 0x21, 0x3D])) == bytes([0x81, 0x85, 0x37, 0xFA, 0x21, 0x3D, 0x7F, 0x9F, 0x4D, 0x51, 0x58])
    assert frame(OP_TEXT, b"Hel", fin=False) == bytes([0x01, 0x03]) + b"Hel"
    assert frame(OP_CONTINUATION, b"lo") == bytes([0x80, 0x02]) + b"lo"
    assert frame(OP_PING, b"Hello")[:2] == bytes([0x89, 0x05])
    assert frame(OP_PONG, b"Hello", key=b"abcd")[:2] == bytes([0x8A, 0x85])
    assert frame(OP_BINARY, bytes(256))[:4] == bytes([0x82, 0x7E, 0x01, 0x00])
    assert frame(OP_BINARY, bytes(65536))[:10] == bytes([0x82, 0x7F, 0, 0, 0, 0, 0, 1, 0, 0])
    # 长度三种写法的分界
    assert frame(OP_BINARY, bytes(125))[1] == 125
    assert frame(OP_BINARY, bytes(126))[1:4] == bytes([126, 0, 126])
    assert frame(OP_BINARY, bytes(65535))[1:4] == bytes([126, 0xFF, 0xFF])
    assert frame(OP_BINARY, bytes(65536), key=b"abcd")[1] == 0x80 | 127
    assert len(frame(OP_BINARY, bytes(65536), key=b"abcd")) == 2 + 8 + 4 + 65536
    # §1.3 的例子
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_masking_is_a_byte_by_byte_xor_and_its_own_inverse():
    key = b"\x01\x80\xfe\x7f"
    data = bytes(range(256)) * 3 + b"xyz"  # 长度不是 4 的倍数
    masked = mask(data, key)
    assert masked == bytes(byte ^ key[index % 4] for index, byte in enumerate(data))
    assert mask(masked, key) == data
    assert mask(b"", key) == b""
    assert mask(b"\x00\x00", key) == b"\x01\x80"  # 开头的 0 字节不会因为"当成大整数"而丢掉


def test_every_client_frame_is_masked_with_a_fresh_key(server):
    client, peer = server.open()
    for index in range(20):
        client.send_text(f"消息 {index}")
    client.send_binary(b"\x00\x01")
    client.ping(b"are you there")
    assert [peer.recv(2) for _ in range(21)] == [f"消息 {index}" for index in range(20)] + [b"\x00\x01"]
    assert peer.recv_frame(2) == (True, OP_PING, b"are you there")
    assert peer.unmasked == 0
    assert len(peer.masks) == 22
    assert len(set(peer.masks)) == 22  # 每一帧一个新的随机掩码


def test_a_ping_longer_than_125_bytes_is_cut(server):
    client, peer = server.open()
    client.ping(b"x" * 200)
    assert peer.recv_frame(2) == (True, OP_PING, b"x" * 125)


@pytest.mark.parametrize("size", [0, 1, 125, 126, 65535, 65536, 300_000])
def test_payload_lengths_round_trip_in_both_directions(server, size):
    client, peer = server.open()
    data = os.urandom(size)
    receiving = Background(peer.recv_frame, 3)  # 大的帧在对方读之前会把发送缓冲塞满：两头要同时进行
    client.send_binary(data)
    assert receiving.result() == (True, OP_BINARY, data)
    sending = Background(peer.send_frame, OP_BINARY, data)
    received = client.recv(3)
    sending.result()
    assert isinstance(received, bytes)
    assert received == data
    text = "字" * (size // 3)
    sending = Background(peer.send_text, text)
    assert client.recv(3) == text
    sending.result()


def test_messages_come_back_as_str_or_bytes(server):
    client, peer = server.open()
    peer.send_frame(OP_TEXT, b"")
    peer.send_frame(OP_BINARY, b"")
    peer.send_frame(OP_BINARY, b"ab", fin=False)
    peer.send_frame(OP_CONTINUATION, b"cd", fin=False)
    peer.send_frame(OP_CONTINUATION, b"ef")
    peer.send_frame(OP_TEXT, "你".encode()[:2], fin=False)  # 一个汉字被切在两帧之间：拼起来以后才检查 UTF-8
    peer.send_frame(OP_CONTINUATION, "你".encode()[2:] + "好".encode())
    assert client.recv(2) == ""
    assert client.recv(2) == b""
    assert client.recv(2) == b"abcdef"
    assert client.recv(2) == "你好"


def test_a_message_of_exactly_max_size_is_fine(server):
    client, peer = server.open(max_size=10)
    peer.send_frame(OP_BINARY, b"0123456789")
    peer.send_frame(OP_TEXT, b"01234", fin=False)
    peer.send_frame(OP_CONTINUATION, b"56789")
    assert client.recv(2) == b"0123456789"
    assert client.recv(2) == "0123456789"


def test_a_ping_inside_a_fragmented_message_is_answered_and_the_message_still_assembles(server):
    client, peer = server.open()
    peer.send_frame(OP_TEXT, "分片".encode(), fin=False)
    peer.send_frame(OP_PING, b"p1")
    peer.send_frame(OP_CONTINUATION, b"-", fin=False)
    peer.send_frame(OP_PONG, b"unsolicited")  # 没人要的 pong：忽略就好（§5.5.3）
    peer.send_frame(OP_CONTINUATION, "消息".encode())
    assert client.recv(2) == "分片-消息"
    assert peer.recv_frame(2) == (True, OP_PONG, b"p1")  # 内容和 ping 一样
    assert peer.unmasked == 0


def test_a_ping_while_idle_is_answered_without_returning_a_message(server):
    client, peer = server.open()
    peer.ping(b"")
    peer.ping(b"y" * 125)
    assert client.recv(0.1) is None  # ping 不是一条消息
    assert peer.recv_frame(2) == (True, OP_PONG, b"")
    assert peer.recv_frame(2) == (True, OP_PONG, b"y" * 125)


def _raw(first: int, payload: bytes = b"x") -> bytes:
    """不经过 frame()，直接排一帧（第一个字节随便写：RSV 位、不存在的操作码）。"""
    return bytes([first, len(payload)]) + payload


# 服务器违反协议的各种做法 → 客户端应该用哪个代码关闭（§7.4.1）
VIOLATIONS = {
    "masked-frame": ([frame(OP_TEXT, b"hi", key=b"abcd")], 1002),
    "rsv1": ([_raw(0x80 | 0x40 | OP_TEXT)], 1002),
    "rsv2": ([_raw(0x80 | 0x20 | OP_TEXT)], 1002),
    "rsv3": ([_raw(0x80 | 0x10 | OP_BINARY)], 1002),
    "opcode-3": ([_raw(0x80 | 0x3)], 1002),
    "opcode-7": ([_raw(0x80 | 0x7)], 1002),
    "opcode-0xB": ([_raw(0x80 | 0xB)], 1002),
    "opcode-0xF": ([_raw(0x80 | 0xF)], 1002),
    "fragmented-ping": ([frame(OP_PING, b"x", fin=False)], 1002),
    "fragmented-close": ([frame(OP_CLOSE, (1000).to_bytes(2, "big"), fin=False)], 1002),
    "long-ping": ([frame(OP_PING, bytes(126))], 1002),
    "long-pong": ([frame(OP_PONG, bytes(200))], 1002),
    "continuation-without-start": ([frame(OP_CONTINUATION, b"x")], 1002),
    "text-inside-fragmented": ([frame(OP_TEXT, b"a", fin=False), frame(OP_TEXT, b"b")], 1002),
    "binary-inside-fragmented": ([frame(OP_TEXT, b"a", fin=False), frame(OP_BINARY, b"b")], 1002),
    "bad-utf8": ([frame(OP_TEXT, b"\xff\xfe")], 1007),
    "overlong-utf8": ([frame(OP_TEXT, b"\xc0\xaf")], 1007),
    "surrogate-in-utf8": ([frame(OP_TEXT, b"\xed\xa0\x80")], 1007),
    "utf8-cut-at-the-end": ([frame(OP_TEXT, "你".encode()[:2], fin=False), frame(OP_CONTINUATION, b"!")], 1007),
    "close-with-one-byte": ([frame(OP_CLOSE, b"\x03")], 1002),
    "close-reason-bad-utf8": ([frame(OP_CLOSE, (1000).to_bytes(2, "big") + b"\xff")], 1007),
}


@pytest.mark.parametrize("name", list(VIOLATIONS))
def test_a_protocol_violation_closes_the_connection_with_the_right_code(server, name):
    frames, code = VIOLATIONS[name]
    client, peer = server.open()
    for data in frames:
        peer.send_raw(data)
    error = closed(lambda: client.recv(2))
    assert error.code == code
    assert client.closed
    assert client.close_code == code
    assert close_frame_from(peer)[0] == code  # 先告诉对方为什么（§7.1.7），再断开
    assert closed(lambda: client.recv(0)).code == code  # 之后再收也是一样的回答
    with pytest.raises(ConnectionClosed):
        client.send_text("还在吗")


@pytest.mark.parametrize("header", [bytes([0x82, 127]) + (1 << 40).to_bytes(8, "big"), bytes([0x81, 126]) + (200).to_bytes(2, "big")], ids=["64-bit", "16-bit"])
def test_a_frame_declared_too_big_is_refused_from_its_header_alone(server, header):
    """服务器只发了一个帧头（声称后面有 1 TB 或者 200 字节）：看到长度就该用 1009 拒绝，不能等着把内容读完。"""
    client, peer = server.open(max_size=100)
    peer.send_raw(header)
    start = time.monotonic()
    error = closed(lambda: client.recv(3))
    assert error.code == 1009
    assert time.monotonic() - start < 1
    assert close_frame_from(peer)[0] == 1009


def test_a_fragmented_message_over_max_size_is_refused(server):
    client, peer = server.open(max_size=10)
    peer.send_frame(OP_TEXT, b"123456", fin=False)
    peer.send_frame(OP_CONTINUATION, b"123456")  # 每一帧都不大，加起来超了
    assert closed(lambda: client.recv(2)).code == 1009
    assert close_frame_from(peer)[0] == 1009


# ---- 超时 ----------------------------------------------------------------------


def test_recv_returns_none_on_timeout(server):
    client, peer = server.open()
    start = time.monotonic()
    assert client.recv(0.05) is None
    assert client.recv(0) is None
    assert time.monotonic() - start < 0.5
    peer.send_text("后来到的")
    assert client.recv(2) == "后来到的"


def test_a_frame_arriving_in_pieces_is_kept_across_timeouts(server):
    client, peer = server.open()
    text = "一段分好几次才到的消息。" * 20  # 超过 125 个字节：长度用 16 位写，切口会落在长度的两个字节之间
    data = frame(OP_TEXT, text.encode())
    for start, end in [(0, 1), (1, 3), (3, 40), (40, len(data) - 1)]:
        peer.send_raw(data[start:end])
        # 每一次都等到这几个字节确实进了客户端的缓冲、recv 也超时返回了，再发下一块
        until(lambda end=end: client.recv(0.01) is None and len(client._buffer) == end, what="半帧留在缓冲里")
    peer.send_raw(data[-1:])
    assert client.recv(2) == text
    assert client.recv(0.01) is None


def test_a_fragmented_message_survives_a_timeout_between_its_frames(server):
    client, peer = server.open()
    peer.send_frame(OP_TEXT, b"ab", fin=False)
    until(lambda: client.recv(0.01) is None and not client._buffer, what="第一帧被收走")
    peer.send_frame(OP_CONTINUATION, b"cd")
    assert client.recv(2) == "abcd"


def test_last_seen_moves_when_a_pong_arrives(server):
    client, peer = server.open()
    client.ping(b"alive?")
    assert peer.recv_frame(2) == (True, OP_PING, b"alive?")
    sent = time.monotonic()
    assert client.last_seen <= sent  # 不是 <：Windows 上 Python 3.12 以前的时钟一格是 15.6 毫秒，两次读到的可能是同一格
    peer.send_frame(OP_PONG, b"alive?")
    until(lambda: client.recv(0.01) is None and client.last_seen >= sent, what="last_seen 跟着 pong 更新")


# ---- 关闭 ----------------------------------------------------------------------


def test_close_without_a_reader_waits_for_the_servers_answer(server):
    client, peer = server.open()
    closing = Background(client.close, 1000, "再见", 3)
    code, reason = close_frame_from(peer)
    assert (code, reason.decode()) == (1000, "再见")
    assert closing.running()  # 还在等服务器回关闭帧
    peer.send_text("关闭之后才到的消息")  # 回关闭帧之前到的数据帧：读掉、丢掉
    answered = time.monotonic()
    peer.send_frame(OP_CLOSE, code.to_bytes(2, "big"))
    closing.result()
    assert time.monotonic() - answered < 1  # 收到回答就返回，没有等满 3 秒
    assert client.closed
    assert closed(lambda: client.recv(0.1)).code == 1000
    with pytest.raises(ConnectionClosed):
        client.send_text("还在吗")
    with pytest.raises(ConnectionError):
        peer.recv_frame(1)  # 客户端已经断开了 TCP


def test_close_while_another_thread_is_reading_gives_up_after_its_timeout(server):
    client, peer = server.open()
    reader = Background(client.recv)  # 没有超时：一直等
    until(lambda: client._read_lock.locked(), what="读的线程开始等")
    start = time.monotonic()
    client.close(1000, timeout=0.2)  # 服务器一直不回
    assert 0.15 <= time.monotonic() - start < 1
    assert closed(reader.result).code == 1000  # 读的线程也醒过来了
    assert close_frame_from(peer)[0] == 1000


def test_close_while_another_thread_is_reading_returns_when_the_server_answers(server):
    client, peer = server.open()
    reader = Background(client.recv)
    until(lambda: client._read_lock.locked(), what="读的线程开始等")
    answering = Background(peer.recv, 3)  # 假服务器收到关闭帧就回一个同样代码的
    start = time.monotonic()
    client.close(4000, "换一条连接", timeout=3)
    assert time.monotonic() - start < 1
    assert closed(reader.result).code == 4000  # 服务器的回答是读的线程收到的
    assert answering.result() is None
    assert (peer.close_code, peer.close_reason) == (4000, "换一条连接")


def test_a_close_from_the_server_is_echoed_and_reported(server):
    client, peer = server.open()
    peer.send_frame(OP_CLOSE, (4004).to_bytes(2, "big") + "认证失败".encode())
    error = closed(lambda: client.recv(2))
    assert (error.code, error.reason) == (4004, "认证失败")
    assert close_frame_from(peer) == (4004, b"")  # 回同一个代码
    assert client.closed
    assert (client.close_code, client.close_reason) == (4004, "认证失败")
    assert closed(lambda: client.recv(0)).code == 4004
    with pytest.raises(ConnectionClosed):
        client.send_text("x")
    client.close()  # 已经关了：什么也不做，也不报错


def test_an_empty_close_frame_is_reported_as_1005(server):
    client, peer = server.open()
    peer.send_frame(OP_CLOSE, b"")
    assert closed(lambda: client.recv(2)).code == 1005
    assert close_frame_from(peer)[0] == 1000  # 1005 不能写进关闭帧：回一个 1000


@pytest.mark.parametrize("code", [1000, 1001, 1003, 1007, 1008, 1011, 1012, 1014, 3000, 4000, 4999])
def test_legal_close_codes_are_passed_on(server, code):
    client, peer = server.open()
    peer.send_frame(OP_CLOSE, code.to_bytes(2, "big"))
    assert closed(lambda: client.recv(2)).code == code
    assert close_frame_from(peer)[0] == code


@pytest.mark.parametrize("code", [0, 999, 1004, 1005, 1006, 1015, 1016, 2999, 5000, 65535])
def test_close_codes_that_are_illegal_on_the_wire_are_a_protocol_error(server, code):
    client, peer = server.open()
    peer.send_frame(OP_CLOSE, code.to_bytes(2, "big"))
    assert closed(lambda: client.recv(2)).code == 1002
    assert close_frame_from(peer)[0] == 1002


def test_a_long_close_reason_is_cut_on_a_character_boundary(server):
    """原因最多 123 个字节。按字节硬切会切在一个汉字中间，对方会因为原因不是合法的 UTF-8 再报一个错。"""
    client, peer = server.open()
    reason = "a" + "一个很长的关闭原因" * 20
    reading = Background(close_frame_from, peer)
    client.close(1000, reason, timeout=0.2)
    code, raw = reading.result()
    assert code == 1000
    assert len(raw) <= 123
    assert raw.decode("utf-8") == reason[:41]  # 1 + 40 × 3 = 121 个字节；再多一个字就超了


def test_on_windows_a_blocked_reader_waits_in_short_slices(server, monkeypatch):
    """Windows 上 shutdown 叫不醒卡在 select 里的线程（见 _fill）：那里一小段一小段地等，每段之间看一眼关了没有。
    在每个系统上都能测：假装是 Windows，读的线程照样很快醒过来；一小段到了不算超时，有数据照样读得到。"""
    monkeypatch.setattr(websocket, "WINDOWS", True)
    monkeypatch.setattr(websocket, "_WINDOWS_SLICE", 0.05)
    client, peer = server.open()
    assert client.recv(0.2) is None  # 好几段加起来才到时候：返回"没有消息"，不是出错
    later = Background(client.recv, 2)
    time.sleep(0.15)  # 读的线程已经等过了好几段
    peer.send_text("隔了几段才到")
    assert later.result() == "隔了几段才到"
    reader = Background(client.recv)
    until(lambda: client._read_lock.locked(), what="读的线程开始等")
    start = time.monotonic()
    client.abort()
    assert closed(reader.result).code == 1006
    assert time.monotonic() - start < 0.5


def test_abort_wakes_a_blocked_reader_at_once(server):
    client, peer = server.open()
    reader = Background(client.recv)
    until(lambda: client._read_lock.locked(), what="读的线程开始等")
    start = time.monotonic()
    client.abort()
    assert closed(reader.result).code == 1006
    assert time.monotonic() - start < 0.5
    assert client.closed
    with pytest.raises(ConnectionError):
        peer.recv_frame(1)  # 没有关闭帧，TCP 直接断了
    with pytest.raises(ConnectionClosed):
        client.send_text("x")


def test_a_tcp_drop_without_a_close_frame_is_1006(server):
    client, peer = server.open()
    reader = Background(client.recv)
    until(lambda: client._read_lock.locked(), what="读的线程开始等")
    peer.drop()
    assert closed(reader.result).code == 1006
    assert client.closed
    assert closed(lambda: client.recv(0)).code == 1006
    with pytest.raises(ConnectionClosed):
        client.send_text("x")
    start = time.monotonic()
    client.close(timeout=2)  # 连接已经断了：马上返回，不报错
    assert time.monotonic() - start < 0.5


def test_close_is_idempotent_and_sends_a_single_close_frame(server):
    client, peer = server.open()
    closers = [Background(client.close, 1000, "", 0.2) for _ in range(4)]  # 几个线程同时关
    for closer in closers:
        closer.result()
    client.close()
    client.abort()
    frames = []
    with pytest.raises(ConnectionError):
        while True:
            frames.append(peer.recv_frame(1))
    assert [opcode for _, opcode, _ in frames] == [OP_CLOSE]


# ---- 并发 ----------------------------------------------------------------------


def test_many_senders_at_once_never_interleave_frames(server):
    """8 个线程同时发，另一个线程同时在收（服务器不停地 ping，收的线程回 pong，也在写）。每一条都要完整到达。"""
    client, peer = server.open()
    expected = []
    for sender in range(8):
        batch = []
        for index in range(40):
            size = 70_000 if index == 7 else (sender * 37 + index * 101) % 3000  # 夹几条要用 64 位长度的
            batch.append(f"{sender}-{index}:" + "字" * size)
        expected.append(batch)
    total = sum(len(batch) for batch in expected)

    stop = threading.Event()

    def keep_receiving():
        while not stop.is_set():
            assert client.recv(0.02) is None  # 服务器不发消息，只发 ping

    def keep_pinging():
        count = 0
        while not stop.is_set():
            peer.ping(f"p{count}".encode())
            count += 1
            time.sleep(0.002)
        return count

    receiver = Background(keep_receiving)
    pinger = Background(keep_pinging)
    collecting = Background(lambda: [peer.recv(5) for _ in range(total)])
    until(lambda: peer.pongs, what="收的线程开始回 pong")  # 发的线程开始之前，回 pong 的那条路已经在跑了
    senders = [Background(lambda batch=batch: [client.send_text(text) for text in batch]) for batch in expected]
    for sender in senders:
        sender.result(10)
    received = collecting.result(10)
    stop.set()
    pinged = pinger.result()
    receiver.result()
    assert sorted(received) == sorted(text for batch in expected for text in batch)
    for batch in expected:  # 同一个线程发的，按发的顺序到
        assert [text for text in received if text.split(":")[0].split("-")[0] == batch[0].split("-")[0]] == batch
    assert peer.unmasked == 0
    assert peer.pongs  # 收的线程确实在同时回 pong
    assert set(peer.pongs) <= {f"p{count}".encode() for count in range(pinged)}


# ---- 代理 ----------------------------------------------------------------------


class FakeProxy:
    """一个很小的 HTTP 代理：收一个 CONNECT，回 answer；回的是 200，就把两边的字节原样互相转发（这就是"隧道"）。

    answer 是 None 时什么也不回，直接断开。requests 里记着每个 CONNECT 的请求（状态行和头，原样的文字）。
    """

    def __init__(self, answer: bytes | None = b"HTTP/1.1 200 Connection established\r\n\r\n"):
        self.answer = answer
        self.requests: list[str] = []
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            self.requests.append(head.decode())
            if self.answer is None:
                return
            conn.sendall(self.answer)
            if not self.answer.startswith(b"HTTP/1.1 200"):
                return
            host, port = head.split(b" ")[1].decode().rsplit(":", 1)
            with socket.create_connection((host.strip("[]"), int(port))) as upstream:
                upstream.sendall(rest)
                there = threading.Thread(target=self._pipe, args=(conn, upstream), daemon=True)
                there.start()
                self._pipe(upstream, conn)
                there.join(5)

    @staticmethod
    def _pipe(source: socket.socket, target: socket.socket) -> None:
        try:
            while chunk := source.recv(65536):
                target.sendall(chunk)
        except OSError:
            pass
        try:
            target.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def stop(self) -> None:
        self.listener.close()


@pytest.fixture
def proxy():
    made: list[FakeProxy] = []

    def make(answer: bytes | None = b"HTTP/1.1 200 Connection established\r\n\r\n") -> FakeProxy:
        made.append(FakeProxy(answer))
        return made[-1]

    yield make
    for item in made:
        item.stop()


def test_which_proxy_to_use(monkeypatch):
    proxies = {"https": "http://proxy.example:3128", "http": "http://plain.example:8080"}
    bypass: set[str] = set()
    monkeypatch.setattr(urllib.request, "getproxies", lambda: proxies)
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: host in bypass)
    secure = Address(True, "gateway.discord.gg", 443, "/")
    assert websocket._proxy_for(secure) == "http://proxy.example:3128"  # wss:// 看 HTTPS_PROXY
    assert websocket._proxy_for(Address(False, "example.com", 80, "/")) == "http://plain.example:8080"
    for host in ("127.0.0.1", "localhost", "::1"):
        assert websocket._proxy_for(Address(True, host, 443, "/")) is None  # 连本机永远不走代理
    bypass.add("gateway.discord.gg")
    assert websocket._proxy_for(secure) is None  # NO_PROXY 里写了它
    bypass.clear()
    proxies.clear()
    proxies["all"] = "http://all.example:1"
    assert websocket._proxy_for(secure) == "http://all.example:1"
    proxies.clear()
    assert websocket._proxy_for(secure) is None

    def broken():
        raise OSError("读不了系统的代理设置")

    monkeypatch.setattr(urllib.request, "getproxies", broken)
    assert websocket._proxy_for(secure) is None  # 读不出来：当作没有代理


def test_proxy_settings_come_from_the_environment(monkeypatch):
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("NO_PROXY", "internal.example")
    assert websocket._proxy_for(Address(True, "gateway.discord.gg", 443, "/")) == "http://proxy.example:3128"
    assert websocket._proxy_for(Address(True, "chat.internal.example", 443, "/")) is None


def test_a_connection_through_an_http_proxy_tunnel(server, proxy, monkeypatch):
    tunnel = proxy()
    monkeypatch.setattr(websocket, "_proxy_for", lambda address: f"http://bot%40home:p%3Ass@127.0.0.1:{tunnel.port}")
    client, peer = server.open("/gw?v=10")
    lines = tunnel.requests[0].split("\r\n")
    assert lines[0] == f"CONNECT 127.0.0.1:{server.port} HTTP/1.1"
    assert f"Host: 127.0.0.1:{server.port}" in lines
    assert f"Proxy-Authorization: Basic {base64.b64encode(b'bot@home:p:ss').decode()}" in lines  # 网址里的 %40、%3A 先还原
    assert server.requests[0].line == "GET /gw?v=10 HTTP/1.1"  # 握手照常：隧道对两头是透明的
    peer.send_text("隧道那头的问候")
    assert client.recv(2) == "隧道那头的问候"
    client.send_text("回话")
    assert peer.recv(2) == "回话"
    assert peer.unmasked == 0


def test_a_proxy_without_scheme_or_password(server, proxy, monkeypatch):
    tunnel = proxy()
    monkeypatch.setattr(websocket, "_proxy_for", lambda address: f"127.0.0.1:{tunnel.port}")  # 没写 http:// 也当成 http
    client, peer = server.open()
    assert not any(line.lower().startswith("proxy-authorization") for line in tunnel.requests[0].split("\r\n"))
    peer.send_text("ok")
    assert client.recv(2) == "ok"


@pytest.mark.parametrize(
    "answer",
    [b"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic\r\n\r\n", b"HTTP/1.1 502 Bad Gateway\r\n\r\n", None, b"SOCKS?\r\n\r\n"],
    ids=["407", "502", "hang-up", "not-http"],
)
def test_a_proxy_that_says_no(server, proxy, monkeypatch, answer):
    tunnel = proxy(answer)
    monkeypatch.setattr(websocket, "_proxy_for", lambda address: f"http://user:SECRETPASS@127.0.0.1:{tunnel.port}")
    with pytest.raises(WebSocketError) as info:
        WebSocket.connect(server.url(f"/gw?ticket={SECRET}"), timeout=2)
    assert type(info.value) is WebSocketError
    if answer and answer.startswith(b"HTTP/1.1 407"):
        assert "407" in str(info.value)
    assert "SECRETPASS" not in str(info.value) and SECRET not in str(info.value)
    assert server.requests == []  # 没有到达目标服务器


@pytest.mark.parametrize("url", ["https://proxy.example:443", "socks5://127.0.0.1:1080", "http://"])
def test_only_plain_http_proxies_are_supported(monkeypatch, url):
    monkeypatch.setattr(socket, "create_connection", no_network)
    with pytest.raises(WebSocketError):
        websocket._tunnel(url, Address(True, "gateway.discord.gg", 443, "/"), 1)


@pytest.mark.parametrize("url", [f"http://user:SECRETPASS@127.0.0.1:{free_port()}", "http://user:SECRETPASS@127.0.0.1:99999"], ids=["refused", "bad-port"])
def test_a_proxy_that_cannot_be_reached(server, monkeypatch, url):
    monkeypatch.setattr(websocket, "_proxy_for", lambda address: url)
    with pytest.raises(WebSocketError) as info:
        WebSocket.connect(server.url(), timeout=1)
    assert "SECRETPASS" not in str(info.value)


def test_the_connect_target_of_an_ipv6_address_has_brackets(proxy):
    tunnel = proxy(b"HTTP/1.1 403 Forbidden\r\n\r\n")
    with pytest.raises(WebSocketError):
        websocket._tunnel(f"http://127.0.0.1:{tunnel.port}", parse_url("wss://[2001:db8::1]/gateway"), 2)
    lines = tunnel.requests[0].split("\r\n")
    assert lines[0] == "CONNECT [2001:db8::1]:443 HTTP/1.1"
    assert "Host: [2001:db8::1]:443" in lines
