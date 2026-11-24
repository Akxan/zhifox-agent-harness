from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import socket
    import threading
    from typing import Protocol

    class WebSocketPart(Protocol):
        _sock: socket.socket
        max_size: int
        timeout: float
        _buffer: bytearray
        _read_lock: threading.Lock
        _fragments: list[bytes] = []  # noqa: RUF012
        _fragment_op: int = 0
        _fragment_size: int = 0
        _peer_closed: threading.Event
        close_code: int | None = None
        close_reason: str = ""
        last_seen: float = 0.0

        def _send(self, opcode: int, payload: bytes) -> None: ...
        def _send_close(self, code: int, reason: str = "") -> None: ...

else:

    class WebSocketPart:
        """运行时什么都没有：上面的那些只是写给 mypy 看的类型。"""
