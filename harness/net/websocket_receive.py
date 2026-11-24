"""WebSocket · 收（WebSocket 的一组方法，见 websocket.py 开头）：等数据、拼帧、回 pong、对方关闭、对方违反协议。

  recv              等下一条完整的消息；超时返回 None。随时都可以超时：半帧留在缓冲里，下次接着收
  _fill、_readable  再收一些字节。_readable 在 macOS、Linux 上用 poll（为什么见它的说明），Windows 上用 select，
                    而且只等一小段（_WINDOWS_SLICE：那里别的线程 shutdown 叫不醒卡在 select 里的线程）
  _parse、_handle   取出一帧、处理一帧：掩码、大小上限、分片、控制帧、UTF-8（见 websocket.py 开头那几条规矩）
  _closed_by_peer、_fail、_broken   对方先关、对方违反协议（我们用相应的代码关）、没有关闭帧就断了（1006）

同一时间只有一个线程在读（_read_lock，在 websocket.py 的 __init__ 里建）；发（_send、_send_close）在 websocket.py，
收到 ping、对方关闭时这里调用它们。测试换 WINDOWS、_WINDOWS_SLICE 要换在这个模块上（读它们的 _fill 在这里）。
为什么单独一个文件：收是这个客户端里最长、最容易被一个出了毛病（或者不怀好意）的服务器拖垮的部分，单独放着才好一条一条核对。
"""

from __future__ import annotations

import math
import os
import select
import socket
import ssl
import sys
import time

from ..i18n import t
from .websocket_connect import _quietly_close, _reason
from .websocket_protocol import (
    _RESERVED_CODES,
    ABNORMAL,
    BAD_DATA,
    CONTROL_OPS,
    DATA_OPS,
    MAX_CONTROL,
    NO_STATUS,
    NORMAL,
    OP_CLOSE,
    OP_CONTINUATION,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    PROTOCOL_ERROR,
    TOO_BIG,
    ConnectionClosed,
    WebSocketError,
)
from .websocket_types import WebSocketPart

WINDOWS = os.name == "nt"
_WINDOWS_SLICE = 0.2  # Windows 上一次最多在 select 里等多久（见 _fill）


def _readable(sock: socket.socket, timeout: float | None) -> bool:
    """等 sock 有数据可读（或者对方关了、出错了），最多等 timeout 秒（None 是一直等）。

    macOS、Linux 上用 poll，不用 select.select：select 只认编号小于 1024 的文件描述符（FD_SETSIZE），
    一个进程里开着的文件一多（后台服务一直开着，连着几个 MCP 服务器和 Chrome；测试里一个进程跑上千个测试），
    新连接的编号就会超过 1024，select 直接抛 ValueError，上面会把它当成"被别的线程关掉了"去 recv，最后报成超时
    （2026-11-24 macOS 的 CI 上真的出过：五个 websocket 测试一起失败）。poll 没有这个上限，也不多开一个文件。
    Windows 上没有 poll；那里的 select 认的是套接字的句柄、按个数算上限，不看编号，照旧用它。"""
    if sys.platform == "win32":
        ready, _, _ = select.select([sock], [], [], timeout)
        return bool(ready)
    poller = select.poll()
    poller.register(sock, select.POLLIN | select.POLLPRI)  # 对方关了、出错了（POLLHUP、POLLERR）poll 总会报，不用另外登记
    return bool(poller.poll(None if timeout is None else max(0, math.ceil(timeout * 1000))))  # 毫秒，往上取整：不早醒


class WebSocketReceive(WebSocketPart):
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
            # Windows 上，别的线程 shutdown 这个套接字（close、abort，见 websocket.py 的 _finish）不会让正卡在 select 里的这个线程醒过来，
            # 所以那里只等一小段（_WINDOWS_SLICE），醒来回到 _read 的循环里看一眼是不是已经关了，没关接着等
            step = wait if not WINDOWS else _WINDOWS_SLICE if wait is None else min(wait, _WINDOWS_SLICE)
            try:
                ready = _readable(sock, step)
            except (OSError, ValueError):  # 套接字已经被别的线程关掉了
                ready = True
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
