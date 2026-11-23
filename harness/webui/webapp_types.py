"""WebAppPart：混进 WebApp 的每一组方法（XxxActions）能用到的 WebApp 的东西，写给类型检查（mypy）看。

为什么要有这个文件：WebApp 由几组方法拼成（见 webapp.py 开头），每一组写在自己的 *_api.py 里，是一个只有方法的类。
这些方法里到处是 self.app、self.sessions、self._save(...)、self._holding(...)：状态是在 WebApp.__init__ 里才有的，
方法可能写在 webapp.py，也可能写在另一组里（chat_api 的 send 要用 sessions_api 的 _take）。单看 ChatActions 这个类，
mypy 不知道 self 上有这些，只能报"没有这个属性"；用 Any 糊过去或者整个文件不查，又等于这些文件没人查。

办法：ChatActions 这些类都继承 WebAppPart。它是一个 Protocol，列出"别的文件里定义、这一组要用"的状态和方法的类型：
  - 状态只写类型，值照旧在 WebApp.__init__ 里赋（说明也写在那里）。mypy 会拿 __init__ 里的赋值和这里的类型对一遍。
  - 方法只写签名，函数体是 ...。真正的实现在 webapp.py 或某一组里，mypy 会把实现当成对这一行的覆盖来检查：
    签名改了、这里没跟着改，会报 [override]；这里写了、哪儿都没实现，建 WebApp 的地方会报 [abstract]
    （Protocol 里空的方法体算"还没实现"）。所以这里的签名不会悄悄和实现对不上。
  - 这些只在类型检查时存在：运行时 WebAppPart 是下面那个空的类，WebApp 的行为、MRO 里的方法和以前一模一样。

只列出被别的文件用到的：一组方法只在自己的文件里互相调用的，不用写进来；WebApp 自己（webapp.py）用到各组的方法，
它本来就继承了它们，也不用写。新加一组方法（XxxActions）时：
  1. class XxxActions(WebAppPart)，加进 WebApp 的基类；
  2. 用到了 WebApp 的状态或者别的文件里的方法，mypy 报"没有这个属性"时，照着那个属性或者方法的定义在下面补一行
     （状态补类型、方法补签名，放进对应的那一节）；
  3. 新的状态照旧在 WebApp.__init__ 里赋值、写明是给谁用的。

和 skills_api.py 那种"WebApp 上的一个对象"（self.web.xxx）不一样：那些类拿着一个 WebApp，类型直接写 WebApp 就行，不用这个文件。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import contextlib
    import threading
    from typing import Protocol

    from ..app import App
    from ..attachments import Attachment
    from ..channels.hub import ChannelHub
    from ..claims import Claims
    from ..media import Entry
    from .scheduler import Scheduler
    from .sessions import AllIdle, Session, Sessions
    from .ui import EventIds, WebUI

    class WebAppPart(Protocol):
        """混进 WebApp 的一组方法能用到的东西。值和说明在 WebApp.__init__ 里，实现在各自写明的文件里。"""

        # ---- 状态（WebApp.__init__）----
        app: App
        sessions: Sessions
        claims: Claims
        channels: ChannelHub
        tasks: Scheduler
        _ids: EventIds
        _switch: threading.RLock
        _turn: AllIdle
        _publishing: threading.Lock
        _memories_shown: list[dict]

        @property
        def ui(self) -> WebUI: ...

        # ---- webapp.py ----
        def publish_state(self) -> None: ...
        def _busy(self, otherwise: str | None = None) -> str: ...
        def _claim(self, session: Session | None = None) -> str: ...
        def _held_elsewhere(self, conversation_id: str) -> str: ...
        def _save(self, session: Session | None = None) -> None: ...
        def _try_save(self, session: Session | None = None) -> str: ...
        def _before_leaving(self, session: Session, action: str) -> str: ...
        def _left(self, session: Session) -> None: ...
        def _show_warnings(self) -> None: ...
        def refresh_tools(self) -> bool: ...

        # ---- sessions_api.py（SessionActions）----
        def _fresh_session(self) -> Session: ...
        def _holding(self, session: Session) -> contextlib.AbstractContextManager[bool]: ...
        def _viewed_idle(self) -> contextlib.AbstractContextManager[bool]: ...
        def _take(self, session: Session) -> bool: ...
        def _too_many(self) -> str: ...
        def _parallel_limit(self) -> int: ...
        def _show(self, session: Session) -> Session: ...
        def _show_live(self, conversation_id: str) -> tuple[Session | None, Session]: ...
        def _left_behind(self, previous: Session, idle: bool) -> None: ...
        def _discard(self, session: Session) -> None: ...
        def _settle(self, session: Session) -> None: ...

        # ---- chat_api.py（ChatActions）----
        def _run_turn(self, session: Session, text: str, attachments: list[Attachment], again: int | None = None) -> None: ...

        # ---- media_api.py（MediaActions）----
        def _keep_originals(self, attachments: list[Attachment], session: Session | None = None) -> list[Attachment]: ...
        def _live_media(self, session: Session | None = None) -> tuple[Entry, ...]: ...
        def _collect_media(self, candidates: set[str] | None) -> None: ...

else:

    class WebAppPart:
        """运行时什么都没有：上面的那些只是写给 mypy 看的类型。"""
