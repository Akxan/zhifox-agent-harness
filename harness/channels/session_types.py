"""SessionPart：混进 Session 的每一组方法（SessionTurn、SessionConversation、SessionConfirm）能用到的东西，写给类型检查（mypy）看。

和 ../app_types.py 是同一个道理（为什么、怎么写，见那里开头）：状态是 session.py 里 Session.__init__ 的字段，_prepare、_save 在
session_conversation.py、由 session_turn.py 的 _turn 调用；单看一组，mypy 不知道 self 上有这些。
  - 状态只写类型；混进来的方法会整个换掉的（self.conversation = …、self.agent = self.run = None、self.waiting = None、
    self.running = False……）写一个值，原因见 ../app_types.py。
  - 方法只写签名，实现在别的文件里；签名改了、这里没跟着改，mypy 会报 [override]。
  - 这些只在类型检查时存在：运行时 SessionPart 是下面那个空的类，Session 的行为、MRO 里的方法和不拆开的时候一模一样。

../browser/session_types.py 里也有一个 SessionPart，那是浏览器的 BrowserSession 的，和这个无关。
新加一组方法时：class SessionXxx(SessionPart)，加进 Session 的基类；mypy 报"没有这个属性"时，照着它的定义在下面补一行。
"""

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
