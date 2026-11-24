from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import threading
    from collections import deque
    from typing import Any, Protocol

    from ..conversations import Conversation
    from .base import Inbound
    from .outbound import Outlet
    from .router import Router
    from .session_confirm import Waiting
    from .ui import ChannelUI

    class SessionPart(Protocol):
        router: Router
        platform: str
        chat: str
        name: str
        key: str
        _lock: threading.Lock
        _queue: deque[Inbound]
        _worker: threading.Thread | None = None
        _closed: bool = False
        _stop_asked: bool = False
        running: bool = False
        started_at: float | None = None
        conversation: Conversation | None = None
        agent: Any = None
        run: Any = None
        waiting: Waiting | None = None
        persona: str = ""
        ui: ChannelUI
        outlet: Outlet

        def _prepare(self) -> str: ...
        def _save(self) -> None: ...

else:

    class SessionPart:
        """运行时什么都没有：上面的那些只是写给 mypy 看的类型。"""
