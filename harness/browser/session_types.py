from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable
    from typing import Any, Protocol

    from ..config import BROWSER_SHOT_QUALITY
    from .cdp import Connection
    from .manager import BrowserManager
    from .session_page import Outcome
    from .session_tabs import Tab

    class SessionPart(Protocol):
        """混进 BrowserSession 的一组方法能用到的东西。"""

        manager: BrowserManager
        tabs: dict[str, Tab]
        allowed_sites: set[str]
        _count: int
        _generation: int
        _halt: threading.Event
        _changes: list[dict]
        _inbox_lock: threading.Lock
        _targets: set[str]
        _cdp_sessions: set[str]

        active: str = ""

        def _connection(self) -> Connection: ...
        def _state(self, connection: Connection, force: bool = False) -> dict | None: ...
        def _finish(self, connection: Connection, tab: Tab | None, text: str, preview: bool = True, image: bytes | None = None) -> Outcome: ...
        def _tabs_result(self, connection: Connection) -> Outcome: ...

        def _attach(self, connection: Connection, tab: Tab) -> None: ...
        def _tab(self, connection: Connection, tab_id: str | None = None, open_one: bool = True) -> Tab: ...

        def _js(self, connection: Connection, tab: Tab, function: str, options: dict) -> Any: ...
        def _sleep(self, seconds: float) -> None: ...
        def _input(self, connection: Connection, tab: Tab, send: Any) -> bool: ...
        @staticmethod
        def _main_frame(tab: Tab) -> Callable[[dict, str], bool]: ...
        def _capture(self, connection: Connection, tab: Tab, clip: dict | None = None, quality: int = BROWSER_SHOT_QUALITY) -> bytes: ...
        def _page_origin(self, connection: Connection, tab: Tab) -> tuple[float, float]: ...

        def _location(self, connection: Connection, tab: Tab) -> str: ...

else:

    class SessionPart:
        """运行时什么都没有：上面的那些只是写给 mypy 看的类型。"""
