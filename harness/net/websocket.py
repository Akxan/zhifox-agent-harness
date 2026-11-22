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
import hashlib
import ipaddress
import os
import select
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

from ..config import CHANNEL_TIMEOUT, WEBSOCKET_CLOSE_SECONDS, WEBSOCKET_MAX_BYTES
from ..i18n import t

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # §1.3：所有 WebSocket 实现都用这一个
MAX_HANDSHAKE = 16_384  # 握手的回答（状态行加上各个头）最多这么多字节
MAX_CONTROL = 125  # 控制帧的内容最多这么多字节（§5.5）
BODY_PREVIEW = 300  # 握手被拒绝时，留下回答正文的这么多字节（HandshakeError.body），给人看为什么

OP_CONTINUATION, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
DATA_OPS = {OP_CONTINUATION, OP_TEXT, OP_BINARY}
CONTROL_OPS = {OP_CLOSE, OP_PING, OP_PONG}

# 关闭代码（§7.4.1）。1005（没给代码）、1006（没有关闭帧就断了）、1015（TLS 出错）只用来"报告"，不能写进关闭帧
NORMAL, GOING_AWAY, PROTOCOL_ERROR, BAD_DATA, TOO_BIG = 1000, 1001, 1002, 1007, 1009
NO_STATUS, ABNORMAL = 1005, 1006
_RESERVED_CODES = {1004, 1005, 1006, 1015}
WINDOWS = os.name == "nt"
_WINDOWS_SLICE = 0.2  # Windows 上一次最多在 select 里等多久（见 _fill）


class WebSocketError(Exception):
    """连不上、握手失败、协议出错。说明可以直接给人看：里面不含网址的查询部分（那里常有一次性的票据）。"""


class HandshakeError(WebSocketError):
    """服务器没有同意升级成 WebSocket（status 是它回的 HTTP 状态码，比如 401）。body 是回答正文的开头。"""

    def __init__(self, message: str, status: int = 0, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class ConnectionClosed(WebSocketError):
    """连接关上了。code 是关闭代码（对方没给是 1005，连关闭帧都没有就断了是 1006），reason 是对方写的原因。"""

    def __init__(self, code: int, reason: str = ""):
        super().__init__(t("连接关上了（{code}{reason}）", code=code, reason=f"：{reason}" if reason else ""))
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class Address:
    secure: bool
    host: str
    port: int
    target: str  # 请求行里的路径加查询，比如 /?v=10&encoding=json

    @property
    def authority(self) -> str:
        default = 443 if self.secure else 80
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.port == default else f"{host}:{self.port}"


def parse_url(url: str) -> Address:
    """拆开一个 ws:// 或 wss:// 的网址。明文的 ws:// 只允许连本机。"""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:  # 比如 ws://[::1/ 这样括号没配对的：urlsplit 会直接抛 ValueError，调用的人只认 WebSocketError
        raise WebSocketError(t("不是一个 WebSocket 的网址。")) from None
    scheme = parts.scheme.lower()
    if scheme not in ("ws", "wss") or not parts.hostname:
        raise WebSocketError(t("不是一个 WebSocket 的网址。"))
    if scheme == "ws" and not is_loopback(parts.hostname):
        raise WebSocketError(t("为了安全，只能用加密的 wss:// 连接（ws:// 只允许连这台电脑自己）。"))
    try:
        port = parts.port or (443 if scheme == "wss" else 80)
    except ValueError:
        raise WebSocketError(t("不是一个 WebSocket 的网址。")) from None
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return Address(scheme == "wss", parts.hostname, port, target)


def is_loopback(host: str) -> bool:
    """host 是不是这台电脑自己。只认 localhost 和真正的回环地址（127.0.0.0/8、::1）。

    不能只看"是不是 127. 开头"：127.example.com 是一个普通的域名，可以解析到任何一台机器上，
    那样明文的 ws://（还有 http.trusted 放行的 http://）就会把数据不加密地发到外面去。
    """
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:  # 不是 IP 地址，是一个域名
        return False


def accept_key(key: str) -> str:
    """§4.2.2：服务器应该回的 Sec-WebSocket-Accept。"""
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def mask(data: bytes, key: bytes) -> bytes:
    """用 4 字节的 key 逐字节异或（§5.3）。整段当成一个大整数一次异或，比一个字节一个字节地做快得多。"""
    if not data:
        return b""
    repeated = (key * (len(data) // 4 + 1))[: len(data)]
    return (int.from_bytes(data, "big") ^ int.from_bytes(repeated, "big")).to_bytes(len(data), "big")


def frame(opcode: int, payload: bytes, fin: bool = True, key: bytes | None = None) -> bytes:
    """排出一帧。key 是掩码（客户端发的一定要有）；None 表示不带掩码（只有服务器这样发，测试里的假服务器用）。"""
    head = bytearray([(0x80 if fin else 0) | opcode])
    marker = 0x80 if key is not None else 0
    size = len(payload)
    if size < 126:
        head.append(marker | size)
    elif size < 1 << 16:
        head.append(marker | 126)
        head += size.to_bytes(2, "big")
    else:
        head.append(marker | 127)
        head += size.to_bytes(8, "big")
    if key is None:
        return bytes(head) + payload
    return bytes(head) + key + mask(payload, key)


def _proxy_for(address: Address) -> str | None:
    """这个地址要不要走代理、走哪一个（和 urllib 一样看 HTTPS_PROXY、NO_PROXY，macOS 上还看系统设置）。连本机永远不走代理。"""
    if is_loopback(address.host):
        return None
    try:
        if urllib.request.proxy_bypass(address.host):
            return None
        proxies = urllib.request.getproxies()
    except Exception:  # noqa: BLE001 —— 读系统的代理设置出了错：当作没有代理
        return None
    return proxies.get("https" if address.secure else "http") or proxies.get("all") or None


class WebSocket:
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

    # ---- 收 --------------------------------------------------------------------

    def recv(self, timeout: float | None = None) -> str | bytes | None:
        """等下一条完整的消息：文字返回 str，二进制返回 bytes。等了 timeout 秒还没有返回 None（None 表示一直等）。

        ping 在这里自动回 pong；关闭帧在这里回应，然后抛出 ConnectionClosed。还没收完的半帧留在缓冲里，
        下次接着收，所以超时随时都可以发生，不会把数据弄乱。
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._read_lock:
            while True:
                if self._peer_closed.is_set():
                    raise ConnectionClosed(self.close_code or ABNORMAL, self.close_reason)
                parsed = self._parse()
                if parsed is None:
                    if not self._fill(deadline):
                        return None
                    continue
                message = self._handle(*parsed)
                if message is not None:
                    return message

    def _fill(self, deadline: float | None) -> bool:
        """再收一些字节放进缓冲。到了 deadline 还没有返回 False。连接断了抛出 ConnectionClosed。"""
        sock = self._sock
        pending = isinstance(sock, ssl.SSLSocket) and sock.pending() > 0  # TLS 已经解密好、还没取走的
        if not pending:
            wait = None if deadline is None else max(0.0, deadline - time.monotonic())
            # Windows 上，别的线程 shutdown 这个套接字（close、abort，见 _finish）不会让正卡在 select 里的这个线程醒过来，
            # 所以那里只等一小段（_WINDOWS_SLICE），醒来回到 _read 的循环里看一眼是不是已经关了，没关接着等
            step = wait if not WINDOWS else _WINDOWS_SLICE if wait is None else min(wait, _WINDOWS_SLICE)
            try:
                ready, _, _ = select.select([sock], [], [], step)
            except (OSError, ValueError):  # 套接字已经被别的线程关掉了
                ready = [sock]
            if not ready:
                # Windows 上只是一小段到了，还没到时候：回去看一眼再等。（wait 不是 None 时 step 也不是，写出来是给类型检查看的）
                return WINDOWS and (wait is None or (step is not None and step < wait))
        try:
            chunk = sock.recv(65_536)
        except TimeoutError:
            raise self._broken(t("对方半天没有把一帧发完")) from None
        except ssl.SSLWantReadError:
            return True  # 只收到了半条 TLS 记录：下一轮再收
        except OSError as exc:
            raise self._broken(_reason(exc)) from None
        if not chunk:
            raise self._broken("")
        self._buffer += chunk
        self.last_seen = time.monotonic()
        return True

    def _broken(self, why: str) -> ConnectionClosed:
        """连接没有经过关闭握手就断了（1006）。"""
        self._peer_closed.set()
        if self.close_code is None:
            self.close_code, self.close_reason = ABNORMAL, why
        _quietly_close(self._sock)
        return ConnectionClosed(self.close_code, self.close_reason)

    def _parse(self) -> tuple[bool, int, int, bytes] | None:
        """从缓冲里取出一个完整的帧：（FIN，RSV 三位，操作码，内容）。还不完整返回 None。"""
        buffer = self._buffer
        if len(buffer) < 2:
            return None
        first, second = buffer[0], buffer[1]
        size, at = second & 0x7F, 2
        if size == 126:
            if len(buffer) < 4:
                return None
            size, at = int.from_bytes(buffer[2:4], "big"), 4
        elif size == 127:
            if len(buffer) < 10:
                return None
            size, at = int.from_bytes(buffer[2:10], "big"), 10
        masked = bool(second & 0x80)
        if masked:
            self._fail(PROTOCOL_ERROR, t("服务器发来的帧带着掩码（规定不能带）"))
        if size > self.max_size:  # 看到长度就拒绝：不把它读进内存
            self._fail(TOO_BIG, t("一条消息太大了（{size} 字节）", size=size))
        if len(buffer) < at + size:
            return None
        payload = bytes(buffer[at : at + size])
        del buffer[: at + size]
        return bool(first & 0x80), (first >> 4) & 0x7, first & 0x0F, payload

    def _handle(self, fin: bool, rsv: int, opcode: int, payload: bytes) -> str | bytes | None:
        """处理一帧。拼好一条完整的消息时返回它，别的情况（控制帧、分片的中间）返回 None。"""
        if rsv:
            self._fail(PROTOCOL_ERROR, t("帧里用了没有商量过的扩展位"))
        if opcode in CONTROL_OPS:
            if not fin or len(payload) > MAX_CONTROL:
                self._fail(PROTOCOL_ERROR, t("控制帧不能分片，也不能超过 125 个字节"))
            if opcode == OP_PING:
                try:
                    self._send(OP_PONG, payload)
                except WebSocketError:
                    pass  # 回不了 pong：多半连接已经坏了，下一次读的时候会发现
            elif opcode == OP_CLOSE:
                self._closed_by_peer(payload)
            return None
        if opcode not in DATA_OPS:
            self._fail(PROTOCOL_ERROR, t("不认识的操作码 {opcode}", opcode=opcode))
        if opcode == OP_CONTINUATION:
            if not self._fragments and not self._fragment_op:
                self._fail(PROTOCOL_ERROR, t("没有开头的续帧"))
        elif self._fragment_op:
            self._fail(PROTOCOL_ERROR, t("上一条分片的消息还没完，又来了一条新的"))
        else:
            self._fragment_op = opcode
        self._fragment_size += len(payload)
        if self._fragment_size > self.max_size:
            self._fail(TOO_BIG, t("一条消息太大了（{size} 字节）", size=self._fragment_size))
        self._fragments.append(payload)
        if not fin:
            return None
        data, kind = b"".join(self._fragments), self._fragment_op
        self._fragments, self._fragment_op, self._fragment_size = [], 0, 0
        if kind == OP_TEXT:
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError:
                self._fail(BAD_DATA, t("文字消息不是合法的 UTF-8"))
        return data

    def _closed_by_peer(self, payload: bytes) -> None:
        """对方发来关闭帧：记下代码和原因，回一个关闭帧（我们还没发过的话），断开。"""
        if len(payload) == 1:
            self._fail(PROTOCOL_ERROR, t("关闭帧的内容不对"))
        code, reason = NO_STATUS, ""
        if len(payload) >= 2:
            code = int.from_bytes(payload[:2], "big")
            try:
                reason = payload[2:].decode("utf-8")
            except UnicodeDecodeError:
                self._fail(BAD_DATA, t("关闭帧的原因不是合法的 UTF-8"))
            if code < 1000 or code in _RESERVED_CODES or 1016 <= code < 3000 or code >= 5000:
                self._fail(PROTOCOL_ERROR, t("关闭代码 {code} 不合规定", code=code))
        if self.close_code is None:
            self.close_code, self.close_reason = code, reason
        self._send_close(NORMAL if code == NO_STATUS else code)
        self._peer_closed.set()
        _quietly_close(self._sock)
        raise ConnectionClosed(code, reason)

    def _fail(self, code: int, why: str) -> None:
        """对方违反了协议：用 code 关闭连接（§7.1.7，不等对方回应），抛出 ConnectionClosed。"""
        if self.close_code is None:
            self.close_code, self.close_reason = code, why
        self._send_close(code, why)
        self._peer_closed.set()
        _quietly_close(self._sock)
        raise ConnectionClosed(code, why)

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


# ---- 底下的几样 ------------------------------------------------------------------


def _reason(exc: BaseException) -> str:
    return str(getattr(exc, "strerror", None) or getattr(exc, "reason", None) or exc) or type(exc).__name__


def _quietly_close(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _open(address: Address, timeout: float, context: ssl.SSLContext | None) -> socket.socket:
    """连上服务器（需要的话经过代理），套上 TLS。返回的套接字有超时，读写不会永远卡住。"""
    proxy = _proxy_for(address)
    try:
        if proxy:
            sock = _tunnel(proxy, address, timeout)
        else:
            sock = socket.create_connection((address.host, address.port), timeout=timeout)
    except WebSocketError:
        raise
    except (OSError, ValueError) as exc:
        # ValueError：域名写得不对（a..b 这样的，IDNA 编码失败），或者代理的端口不是数字。
        # 也要换成 WebSocketError：调用的人（网关的重连循环）只认它，漏出去一个 ValueError 会让整个线程退出
        raise WebSocketError(t("连不上 {host}（{error}）", host=address.host, error=_reason(exc))) from None
    sock.settimeout(timeout)
    if not address.secure:
        return sock
    try:
        tls = (context or ssl.create_default_context()).wrap_socket(sock, server_hostname=address.host)
    except (OSError, ssl.SSLError) as exc:
        _quietly_close(sock)
        raise WebSocketError(t("和 {host} 建立加密连接失败（{error}）", host=address.host, error=_reason(exc))) from None
    tls.settimeout(timeout)
    return tls


def _body_start(sock: socket.socket, rest: bytes, headers: dict[str, str], timeout: float) -> bytes:
    """握手被拒绝时，回答正文的开头（平台常在这里写为什么，比如 401 的原因）。

    正文常常和头分成两个包到：头读完的那一刻，正文可能还没来。所以照着 Content-Length 再收一会儿，
    最多 BODY_PREVIEW 个字节、最多 timeout 秒；没有 Content-Length（或者等不到）就用已经收到的那些。
    """
    try:
        wanted = min(int(headers.get("content-length", "")), BODY_PREVIEW)
    except ValueError:
        return rest
    deadline = time.monotonic() + timeout
    while len(rest) < wanted and time.monotonic() < deadline:
        try:
            sock.settimeout(max(0.01, deadline - time.monotonic()))
            chunk = sock.recv(4096)
        except OSError:  # 超时也是 OSError：正文就不要了，状态码已经够说明问题
            break
        if not chunk:
            break
        rest += chunk
    return rest


def _tunnel(proxy: str, address: Address, timeout: float) -> socket.socket:
    """经过 HTTP 代理：先连代理，发 CONNECT host:port，代理回 200 以后，这条连接就是一条通到目标的隧道。"""
    parts = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
    if parts.scheme != "http" or not parts.hostname:
        raise WebSocketError(t("只支持 http:// 的代理（现在设置的是 {scheme}）。", scheme=parts.scheme or "?"))
    sock = socket.create_connection((parts.hostname, parts.port or 8080), timeout=timeout)
    # CONNECT 后面是"主机:端口"，IPv6 的地址自己就带冒号，要加方括号，不然代理分不清哪一段是端口（RFC 9110 §9.3.6）
    host = f"[{address.host}]" if ":" in address.host else address.host
    target = f"{host}:{address.port}"
    lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
    if parts.username:
        user = urllib.parse.unquote(parts.username) + ":" + urllib.parse.unquote(parts.password or "")
        lines.append("Proxy-Authorization: Basic " + base64.b64encode(user.encode()).decode())
    try:
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        status, _, rest = _read_head(sock, address)
    except BaseException:
        _quietly_close(sock)
        raise
    if status != 200 or rest:
        _quietly_close(sock)
        raise WebSocketError(t("代理没有同意连到 {host}（HTTP {status}）", host=address.host, status=status))
    return sock


def _read_head(sock: socket.socket, address: Address) -> tuple[int, dict[str, str], bytes]:
    """读一个 HTTP 回答的状态行和各个头。返回（状态码，头（名字小写），头后面已经收到的字节）。"""
    data = b""
    while b"\r\n\r\n" not in data:
        if len(data) > MAX_HANDSHAKE:
            raise WebSocketError(t("{host} 的回答太长了。", host=address.host))
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            raise WebSocketError(t("等 {host} 回答超时了。", host=address.host)) from None
        except OSError as exc:
            raise WebSocketError(t("连不上 {host}（{error}）", host=address.host, error=_reason(exc))) from None
        if not chunk:
            raise WebSocketError(t("{host} 还没回答就断开了。", host=address.host))
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    if len(head) > MAX_HANDSHAKE:  # 上面的循环只管"别一直读下去"；最后一块里才出现空行时，头本身也可能超了
        raise WebSocketError(t("{host} 的回答太长了。", host=address.host))
    lines = head.decode("iso-8859-1").split("\r\n")
    status_line = lines[0].split(" ", 2)
    if len(status_line) < 2 or not status_line[0].startswith("HTTP/") or not status_line[1].isdigit():
        raise WebSocketError(t("{host} 的回答不是 HTTP。", host=address.host))
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        if colon:
            name = name.strip().lower()
            headers[name] = (headers[name] + ", " if name in headers else "") + value.strip()
    return int(status_line[1]), headers, rest
