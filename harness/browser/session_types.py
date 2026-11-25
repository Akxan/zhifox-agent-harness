"""SessionPart：混进 BrowserSession 的每一组方法（SessionTabs、SessionPage……）能用到的东西，写给类型检查（mypy）看。

和 app_types.py、webui/webapp_types.py 是同一个道理（详细的说明见 app_types.py 开头）：BrowserSession 由几组方法拼成
（见 session.py 开头），单看 SessionLook 这个类，mypy 不知道 self 上有 tabs、_connection、_finish……。这里是一个 Protocol，
列出"别的文件里定义、这一组要用"的状态（值在 BrowserSession.__init__ 里）和方法的签名（实现在写明的文件里，签名对不上 mypy 会报）。
运行时 SessionPart 是下面那个空的类。

混进来的方法会整个换掉的状态要写一个值（active：session_tabs.py 开、换、关标签页时 self.active = ...；
_guard：session_look.py 的 navigate 装上、撤掉守卫），
不然 mypy 把那次赋值当成那一组自己新定义了一个属性（原因见 app_types.py 开头）。_notes、_changes、_reported、_generation
也会被整个换掉，但换掉它们的方法都在 session.py 里（BrowserSession 自己），用不着写值；要是把那几个方法搬进混进来的类，
就得在这里给几个列表写值，而 ruff 不许类上的列表写默认值（RUF012）。这是它们留在 session.py 的另一个原因。

只列出被别的文件用到的；新加一组方法时照 app_types.py 开头的三步办。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable
    from typing import Any, Protocol

    from ..config import BROWSER_SHOT_QUALITY
    from .cdp import Connection
    from .manager import BrowserManager
    from .redirects import Guard
    from .session_page import Outcome
    from .session_tabs import Tab

    class SessionPart(Protocol):
        """混进 BrowserSession 的一组方法能用到的东西。值和说明在 BrowserSession.__init__ 里，实现在各自写明的文件里。"""

        # ---- 状态（BrowserSession.__init__，session.py）----
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

        # 混进来的方法会整个换掉的（见文件开头）：session_tabs.py、session_look.py（navigate 时的守卫，见 redirects.py）
        active: str = ""
        _guard: Guard | None = None

        # ---- session.py（BrowserSession）----
        def _connection(self) -> Connection: ...
        def _state(self, connection: Connection, force: bool = False) -> dict | None: ...
        def _finish(self, connection: Connection, tab: Tab | None, text: str, preview: bool = True, image: bytes | None = None) -> Outcome: ...
        def _tabs_result(self, connection: Connection) -> Outcome: ...

        # ---- session_tabs.py（SessionTabs）----
        def _attach(self, connection: Connection, tab: Tab) -> None: ...
        def _tab(self, connection: Connection, tab_id: str | None = None, open_one: bool = True) -> Tab: ...

        # ---- session_page.py（SessionPage）----
        def _js(self, connection: Connection, tab: Tab, function: str, options: dict) -> Any: ...
        def _sleep(self, seconds: float) -> None: ...
        def _input(self, connection: Connection, tab: Tab, send: Any) -> bool: ...
        @staticmethod
        def _main_frame(tab: Tab) -> Callable[[dict, str], bool]: ...
        def _capture(self, connection: Connection, tab: Tab, clip: dict | None = None, quality: int = BROWSER_SHOT_QUALITY) -> bytes: ...
        def _page_origin(self, connection: Connection, tab: Tab) -> tuple[float, float]: ...

        # ---- session_look.py（SessionLook）----
        def _location(self, connection: Connection, tab: Tab) -> str: ...

else:

    class SessionPart:
        """运行时什么都没有：上面的那些只是写给 mypy 看的类型。"""
