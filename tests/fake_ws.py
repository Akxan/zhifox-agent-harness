"""本地的假 WebSocket 服务器那一头，只在测试和演示里用。

假的 Discord、Slack、QQ（tests/fake_discord.py……）都是一个普通的 HTTP 服务器（BaseHTTPRequestHandler）：
平台的 REST 接口照常回答 JSON；有人带着 Upgrade: websocket 来 GET 网关的地址时，在同一个连接上调用 accept(handler)，
握手（RFC 6455 §4.2），之后用返回的 ServerSocket 收发帧。客户端（harness/net/websocket.py）走的是真实的代码路径。

ServerSocket 会像真的服务器一样较真：客户端发来的帧必须带掩码（不带就记下来，测试会检查），控制帧不能分片；
收到 ping 自动回 pong；收到关闭帧回一个关闭帧。测试想故意做坏事（发半帧、发带掩码的帧、不回关闭帧），
用 send_frame / send_raw 直接写字节。
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler

from harness.net.websocket import (
    GUID,
    OP_BINARY,
    OP_CLOSE,
    OP_CONTINUATION,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    frame,
    mask,
)


def accept(handler: BaseHTTPRequestHandler, status: int = 101, accept_key: str | None = None, extra: dict | None = None) -> ServerSocket:
    """在 handler 的连接上完成握手（status 不是 101 时回一个错误，测试握手失败用）。返回 ServerSocket。"""
    key = handler.headers.get("Sec-WebSocket-Key", "")
    assert handler.headers.get("Upgrade", "").lower() == "websocket"
    assert "upgrade" in handler.headers.get("Connection", "").lower()
    assert handler.headers.get("Sec-WebSocket-Version") == "13"
    assert len(base64.b64decode(key)) == 16  # §4.1：16 个随机字节的 base64
    handler.close_connection = True
    if status != 101:
        body = b'{"error": "denied"}'
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        handler.wfile.flush()
        return ServerSocket(handler.connection, handler.path, dict(handler.headers), opened=False)
    expected = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    lines = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Accept: {accept_key if accept_key is not None else expected}",
        *(f"{name}: {value}" for name, value in (extra or {}).items()),
    ]
    handler.wfile.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    handler.wfile.flush()
    return ServerSocket(handler.connection, handler.path, dict(handler.headers))


class ServerSocket:
    def __init__(self, sock: socket.socket, path: str, headers: dict, opened: bool = True):
        self.sock = sock
        self.path = path
        self.headers = headers
        self.opened = opened
        self._buffer = bytearray()
        self._send_lock = threading.Lock()
        self.unmasked = 0  # 客户端发来的、没带掩码的帧（应该一直是 0）
        self.masks: list[bytes] = []  # 客户端每一帧用的掩码（应该每一帧都不一样，攻击者猜不到）
        self.pings: list[bytes] = []  # 客户端发来的 ping
        self.pongs: list[bytes] = []  # 客户端回的 pong
        self.close_code: int | None = None  # 客户端发来的关闭代码（还没收到是 None）
        self.close_reason = ""
        self.closed = threading.Event()
        self.received: list[str | bytes] = []  # 收到的每一条完整消息

    # ---- 发 --------------------------------------------------------------------

    def send_raw(self, data: bytes) -> None:
        with self._send_lock:
            self.sock.sendall(data)

    def send_frame(self, opcode: int, payload: bytes = b"", fin: bool = True, rsv: int = 0, key: bytes | None = None) -> None:
        data = bytearray(frame(opcode, payload, fin=fin, key=key))
        data[0] |= (rsv & 0x7) << 4
        self.send_raw(bytes(data))

    def send_text(self, text: str) -> None:
        self.send_frame(OP_TEXT, text.encode())

    def send_json(self, value: object) -> None:
        self.send_text(json.dumps(value, ensure_ascii=False))

    def ping(self, data: bytes = b"hi") -> None:
        self.send_frame(OP_PING, data)

    def close(self, code: int = 1000, reason: str = "", wait: float = 2.0) -> None:
        """发关闭帧，等客户端回一个（最多 wait 秒），再断开。"""
        try:
            self.send_frame(OP_CLOSE, code.to_bytes(2, "big") + reason.encode())
        except OSError:
            pass
        deadline = time.monotonic() + wait
        while self.close_code is None and time.monotonic() < deadline:
            try:
                if self.recv(timeout=max(0.01, deadline - time.monotonic())) is None and self.closed.is_set():
                    break
            except (ConnectionError, OSError):
                break
        self.drop()

    def drop(self) -> None:
        """不打招呼，直接断开 TCP（模拟网络断了）。"""
        self.closed.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    # ---- 收 --------------------------------------------------------------------

    def _read(self, size: int, deadline: float) -> bytes:
        while len(self._buffer) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError:
                raise TimeoutError from None
            if not chunk:
                self.closed.set()
                raise ConnectionError("客户端断开了")
            self._buffer += chunk
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def recv_frame(self, timeout: float = 5.0) -> tuple[bool, int, bytes]:
        """收一帧，原样返回（FIN，操作码，去掉掩码以后的内容）。"""
        deadline = time.monotonic() + timeout
        first, second = self._read(2, deadline)
        size = second & 0x7F
        if size == 126:
            size = int.from_bytes(self._read(2, deadline), "big")
        elif size == 127:
            size = int.from_bytes(self._read(8, deadline), "big")
        key = self._read(4, deadline) if second & 0x80 else None
        if key is None:
            self.unmasked += 1
        else:
            self.masks.append(key)
        payload = self._read(size, deadline)
        return bool(first & 0x80), first & 0x0F, mask(payload, key) if key else payload

    def recv(self, timeout: float = 5.0) -> str | bytes | None:
        """收一条完整的消息（拼好分片）。ping 自动回 pong；收到关闭帧回一个关闭帧并返回 None。超时返回 None。"""
        parts: list[bytes] = []
        kind = 0
        deadline = time.monotonic() + timeout
        while True:
            try:
                fin, opcode, payload = self.recv_frame(max(0.01, deadline - time.monotonic()))
            except TimeoutError:
                return None
            if opcode == OP_PING:
                self.pings.append(payload)
                self.send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                self.pongs.append(payload)
                continue
            if opcode == OP_CLOSE:
                self.close_code = int.from_bytes(payload[:2], "big") if len(payload) >= 2 else 1005
                self.close_reason = payload[2:].decode("utf-8", "replace")
                try:
                    self.send_frame(OP_CLOSE, payload[:2])
                except OSError:
                    pass
                self.drop()
                return None
            if opcode in (OP_TEXT, OP_BINARY):
                kind = opcode
            elif opcode != OP_CONTINUATION:
                raise AssertionError(f"客户端发来了不认识的操作码 {opcode}")
            parts.append(payload)
            if fin:
                data = b"".join(parts)
                message: str | bytes = data.decode() if kind == OP_TEXT else data
                self.received.append(message)
                return message

    def recv_json(self, timeout: float = 5.0) -> dict | None:
        message = self.recv(timeout)
        return json.loads(message) if message is not None else None
