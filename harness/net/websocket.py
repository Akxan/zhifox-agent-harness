"""手写的 WebSocket 客户端（RFC 6455），只用标准库的 socket、ssl 和 select。

为什么要自己写：Discord 的 Gateway、Slack 的 Socket Mode、QQ 机器人的网关都是 WebSocket，是机器人主动连过去、
一直开着的一条连接，平台有消息就顺着它推过来，所以不需要公网地址。要用的只是协议里很小的一部分，
自己写一遍，每一步在做什么都看得见，也不多装一个依赖。协议的原文：https://www.rfc-editor.org/rfc/rfc6455

一条 WebSocket 连接是怎么来的：

    1. 普通的 TCP 连接（wss:// 再套一层 TLS，和 HTTPS 一样）。有 HTTPS_PROXY 的话，先连代理，用 CONNECT 打一条隧道
    2. 握手：发一个 HTTP/1.1 的 GET，带着 Upgrade: websocket 和一个随机的 Sec-WebSocket-Key（§4.1）。
       服务器回 101，Sec-WebSocket-Accept 必须等于 base64(sha1(key + 一个固定的 GUID))：这证明对方真的懂
       WebSocket，而不是一个普通的 HTTP 服务器把请求原样弹了回来（§4.2.2）
    3. 之后两边互发"帧"（§5.2）。第一个字节是 FIN（这是不是一条消息的最后一帧）和操作码：
       1 文字、2 二进制、0 续帧、8 关闭、9 ping、10 pong；第二个字节是"带没带掩码"和长度
       （126、127 表示后面再跟 2 个、8 个字节的真正长度）

几条必须守的规矩（不守的话，服务器会断开，或者我们会被骗）：

  掩码      客户端发的每一帧都要用一个随机的 4 字节掩码逐字节异或（§5.3）。这防的是中间的缓存代理被"投毒"：
            攻击者没法预测发出去的字节长什么样。反过来，服务器发来的帧不能带掩码，带了就是协议错误
  分片      一条长消息可以拆成几帧：第一帧 FIN=0，后面是操作码 0 的续帧，最后一帧 FIN=1（§5.4）。
            控制帧（关闭、ping、pong）可以插在分片中间，但它们自己不能分片，也不能超过 125 个字节（§5.5）
  ping/pong 对方发来 ping，要尽快回一个内容一样的 pong（§5.5.2）。这里收到就回，不用调用的人操心
  关闭      想断开时先发一个关闭帧（2 字节的代码 + 原因），等对方也回一个关闭帧，再断开 TCP（§7）。
            对方先发的话，原样回一个。代码的意思见 §7.4（1000 正常、1001 走开了、1002 协议错误……）
  文字      操作码 1 的消息必须是合法的 UTF-8，不是的话用 1007 关闭（§8.1）
  扩展      我们不要任何扩展（比如压缩），所以 RSV 那三位必须是 0，服务器也不能在握手里塞给我们一个扩展

为了不被一个出了毛病（或者不怀好意）的服务器拖垮：一条消息最大 max_size 个字节（超了用 1009 关闭，
而且是看到长度就拒绝，不会先把它读进内存）；握手的回答最多 16 KB；每一次读写都有超时。
明文的 ws:// 只允许连本机（测试里的假服务器），连外面一律用 wss://。

用法：一个线程循环调用 recv（超时返回 None，调用的人借这个机会做心跳），别的线程可以同时调用 send_text；
发送有锁，帧不会交错。close 可以从任何线程调用。
"""

from __future__ import annotations

import base64
import os
import socket
import ssl
import threading
import time

from ..config import CHANNEL_TIMEOUT, WEBSOCKET_CLOSE_SECONDS, WEBSOCKET_MAX_BYTES
from ..i18n import t
from .websocket_connect import (
    BODY_PREVIEW,
    MAX_HANDSHAKE,
    Address,
    _body_start,
    _open,
    _proxy_for,
    _quietly_close,
    _read_head,
    _reason,
    _tunnel,
    is_loopback,
    parse_url,
)
from .websocket_protocol import (
    ABNORMAL,
    BAD_DATA,
    CONTROL_OPS,
    DATA_OPS,
    GOING_AWAY,
    GUID,
    MAX_CONTROL,
    NO_STATUS,
    NORMAL,
    OP_BINARY,
    OP_CLOSE,
    OP_CONTINUATION,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    PROTOCOL_ERROR,
    TOO_BIG,
    ConnectionClosed,
    HandshakeError,
    WebSocketError,
    accept_key,
    frame,
    mask,
)
from .websocket_receive import WINDOWS, WebSocketReceive

__all__ = [
    "ABNORMAL",
    "BAD_DATA",
    "BODY_PREVIEW",
    "CONTROL_OPS",
    "DATA_OPS",
    "GOING_AWAY",
    "GUID",
    "MAX_CONTROL",
    "MAX_HANDSHAKE",
    "NORMAL",
    "NO_STATUS",
    "OP_BINARY",
    "OP_CLOSE",
    "OP_CONTINUATION",
    "OP_PING",
    "OP_PONG",
    "OP_TEXT",
    "PROTOCOL_ERROR",
    "TOO_BIG",
    "WINDOWS",
    "Address",
    "ConnectionClosed",
    "HandshakeError",
    "WebSocket",
    "WebSocketError",
    "_proxy_for",
    "_tunnel",
    "accept_key",
    "frame",
    "is_loopback",
    "mask",
    "parse_url",
]


class WebSocket(WebSocketReceive):
    """一条已经握手成功的 WebSocket 连接。用 WebSocket.connect 建。"""

    def __init__(self, sock: socket.socket, max_size: int = WEBSOCKET_MAX_BYTES, timeout: float = CHANNEL_TIMEOUT, buffered: bytes = b""):
        self._sock = sock
        self.max_size = max_size
        self.timeout = timeout
        self._buffer = bytearray(buffered)  # 收到了、还没拼成完整一帧的字节
        self._send_lock = threading.Lock()
        self._read_lock = threading.Lock()  # 同一时间只有一个线程在读
        self._fragments: list[bytes] = []  # 正在拼的一条分片消息
        self._fragment_op = 0
        self._fragment_size = 0
        self._close_sent = False
        self._peer_closed = threading.Event()  # 收到了对方的关闭帧（或者连接已经断了）
        self.close_code: int | None = None
        self.close_reason = ""
        self.last_seen = time.monotonic()  # 最后一次收到对方的任何东西（包括 pong），判断连接是不是还活着用

    # ---- 建立连接 --------------------------------------------------------------

    @classmethod
    def connect(
        cls,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = CHANNEL_TIMEOUT,
        max_size: int = WEBSOCKET_MAX_BYTES,
        context: ssl.SSLContext | None = None,
    ) -> WebSocket:
        address = parse_url(url)
        sock = _open(address, timeout, context)
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            lines = [
                f"GET {address.target} HTTP/1.1",
                f"Host: {address.authority}",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
            ]
            for name, value in (headers or {}).items():
                if "\r" in name or "\n" in name or "\r" in value or "\n" in value:
                    raise WebSocketError(t("请求头里不能有换行。"))
                lines.append(f"{name}: {value}")
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
            status, reply, rest = _read_head(sock, address)
            if status != 101:
                body = _body_start(sock, rest, reply, timeout).decode("utf-8", "replace")[:BODY_PREVIEW]
                raise HandshakeError(t("{host} 没有同意建立 WebSocket 连接（HTTP {status}）", host=address.host, status=status), status, body)
            if reply.get("upgrade", "").lower() != "websocket" or "upgrade" not in reply.get("connection", "").lower():
                raise HandshakeError(t("{host} 的回答不是 WebSocket 的握手。", host=address.host), status)
            if reply.get("sec-websocket-accept") != accept_key(key):
                raise HandshakeError(t("{host} 的握手回答对不上（Sec-WebSocket-Accept 不对）。", host=address.host), status)
            if reply.get("sec-websocket-extensions") or reply.get("sec-websocket-protocol"):
                raise HandshakeError(t("{host} 要用一个我们没有要求的扩展或子协议。", host=address.host), status)
        except BaseException:
            _quietly_close(sock)
            raise
        return cls(sock, max_size=max_size, timeout=timeout, buffered=rest)

    # ---- 发 --------------------------------------------------------------------

    def send_text(self, text: str) -> None:
        self._send(OP_TEXT, text.encode("utf-8"))

    def send_binary(self, data: bytes) -> None:
        self._send(OP_BINARY, bytes(data))

    def ping(self, data: bytes = b"") -> None:
        self._send(OP_PING, data[:MAX_CONTROL])

    def _send(self, opcode: int, payload: bytes) -> None:
        if opcode in CONTROL_OPS and len(payload) > MAX_CONTROL:
            raise WebSocketError(t("控制帧最多 125 个字节。"))
        data = frame(opcode, payload, key=os.urandom(4))
        with self._send_lock:
            # 我们发过关闭帧，或者连接已经断了（对方关的、abort 的、读的时候发现断了的）：不再往套接字里写，
            # 直接告诉调用的人连接关了、是哪个代码，而不是一句看不懂的"Bad file descriptor"
            if opcode != OP_CLOSE and (self._close_sent or self._peer_closed.is_set()):
                raise ConnectionClosed(self.close_code or ABNORMAL, self.close_reason)
            if opcode == OP_CLOSE:
                if self._close_sent:
                    return
                self._close_sent = True
            try:
                self._sock.sendall(data)
            except OSError as exc:
                self._peer_closed.set()
                raise WebSocketError(t("发送失败（{error}）", error=_reason(exc))) from None

    def _send_close(self, code: int, reason: str = "") -> None:
        # 原因最多 123 个字节（加上 2 字节的代码正好 125）。按字节截断可能切在一个汉字的中间，
        # 对方会因为"原因不是合法的 UTF-8"再回一个 1007，所以截完把半个字丢掉
        text = reason.encode("utf-8")[: MAX_CONTROL - 2].decode("utf-8", "ignore").encode("utf-8")
        payload = b"" if code == NO_STATUS else code.to_bytes(2, "big") + text
        try:
            self._send(OP_CLOSE, payload)
        except WebSocketError:
            pass  # 连接已经坏了：没发出去也就算了

    # ---- 关闭 ------------------------------------------------------------------

    def close(self, code: int = NORMAL, reason: str = "", timeout: float = WEBSOCKET_CLOSE_SECONDS) -> None:
        """关闭握手：发一个关闭帧，等对方回一个（最多 timeout 秒），再断开。从哪个线程调用都行，调用几次都行。

        有别的线程正在 recv 的话，对方的回应由它收（它会抛出 ConnectionClosed）；这里只是等着，到时间就硬断开，
        那个线程也会跟着醒过来。没有人在读的话，这里自己把对方的回应读掉。
        """
        if self.close_code is None:
            self.close_code, self.close_reason = code, reason
        self._send_close(code, reason)
        if self._read_lock.acquire(timeout=0):
            try:
                deadline = time.monotonic() + timeout
                while not self._peer_closed.is_set() and time.monotonic() < deadline:
                    try:
                        parsed = self._parse()
                        if parsed is None:
                            if not self._fill(deadline):
                                break
                            continue
                        self._handle(*parsed)  # 关闭之后还到的数据帧：丢掉
                    except WebSocketError:
                        break
            finally:
                self._read_lock.release()
        else:
            self._peer_closed.wait(timeout)
        self._peer_closed.set()
        self._finish()

    def abort(self) -> None:
        """不打招呼，直接断开（比如心跳没有回应，连接已经"僵死"了）。正在 recv 的线程会马上醒过来。"""
        if self.close_code is None:
            self.close_code, self.close_reason = ABNORMAL, ""
        self._peer_closed.set()
        self._finish()

    def _finish(self) -> None:
        """从别的线程断开连接。有线程正卡在 recv（select）里的话，只 shutdown，不 close：shutdown 会让它马上醒过来，
        由它自己在它的线程里 close（见 _broken）。在这里 close 的话，操作系统可能立刻把这个文件描述符的号码分给
        别的连接，那个线程就会接着在别人的连接上等，几秒甚至一直醒不过来。"""
        if self._read_lock.acquire(timeout=0):
            try:
                _quietly_close(self._sock)
            finally:
                self._read_lock.release()
        else:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    @property
    def closed(self) -> bool:
        return self._peer_closed.is_set()
