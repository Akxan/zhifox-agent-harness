"""WebSocketPart：混进 WebSocket 的那一组方法（WebSocketReceive）能用到的东西，写给类型检查（mypy）看。

和 ../app_types.py 是同一个道理（为什么、怎么写，见那里开头）：状态是 websocket.py 里 WebSocket.__init__ 的字段，_send、_send_close
在 websocket.py；单看 WebSocketReceive，mypy 不知道 self 上有这些。
  - 状态只写类型；收的那一组会整个换掉的（拼完一条消息时的 self._fragments, … = [], 0, 0，close_code、close_reason、last_seen）
    写一个值，原因见 ../app_types.py。_fragments 的值是 []：不写的话 mypy 报 Cannot determine type；ruff 的 RUF012 不许类上有
    可变的默认值，所以那一行带着 noqa——这个类运行时根本不存在，这个 [] 谁也不会拿去用。
  - 方法只写签名，实现在 websocket.py；签名改了、这里没跟着改，mypy 会报 [override]。
  - 这些只在类型检查时存在：运行时 WebSocketPart 是下面那个空的类，WebSocket 的行为、MRO 里的方法和不拆开的时候一模一样。

新加一组方法时：class WebSocketXxx(WebSocketPart)，加进 WebSocket 的基类；mypy 报"没有这个属性"时，照着它的定义在下面补一行。
"""

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
