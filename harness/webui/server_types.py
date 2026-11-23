from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from email.message import Message
    from io import BufferedIOBase
    from typing import Protocol

    from .server import Server

    class HandlerPart(Protocol):
        server: Server
        headers: Message
        command: str
        rfile: BufferedIOBase
        wfile: BufferedIOBase

        _responded: bool = False

        def send_response(self, code: int, message: str | None = None) -> None: ...
        def send_header(self, keyword: str, value: str) -> None: ...
        def end_headers(self) -> None: ...

        def _send_json(self, status: int, payload: dict) -> None: ...
        def _send_text(self, status: int, text: str) -> None: ...
        def _send(self, status: int, content_type: str, body: bytes, headers: dict | None = None) -> None: ...

else:

    class HandlerPart:
        """运行时什么都没有：上面的那些只是写给 mypy 看的类型。"""
