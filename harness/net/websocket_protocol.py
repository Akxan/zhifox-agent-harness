from __future__ import annotations

import base64
import hashlib

from ..i18n import t

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # §1.3：所有 WebSocket 实现都用这一个
MAX_CONTROL = 125  # 控制帧的内容最多这么多字节（§5.5）

OP_CONTINUATION, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
DATA_OPS = {OP_CONTINUATION, OP_TEXT, OP_BINARY}
CONTROL_OPS = {OP_CLOSE, OP_PING, OP_PONG}

# 关闭代码（§7.4.1）。1005（没给代码）、1006（没有关闭帧就断了）、1015（TLS 出错）只用来"报告"，不能写进关闭帧
NORMAL, GOING_AWAY, PROTOCOL_ERROR, BAD_DATA, TOO_BIG = 1000, 1001, 1002, 1007, 1009
NO_STATUS, ABNORMAL = 1005, 1006
_RESERVED_CODES = {1004, 1005, 1006, 1015}


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
