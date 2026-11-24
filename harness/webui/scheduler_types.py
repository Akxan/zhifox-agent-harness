"""SchedulerPart：混进 Scheduler 的每一组方法（SchedulerLoop、SchedulerWork、SchedulerActions）能用到的东西，写给类型检查（mypy）看。

和 ../app_types.py、server_types.py 是同一个道理（为什么、怎么写，见 ../app_types.py 开头）：状态是 scheduler.py 里 Scheduler.__init__
的字段，clock、_publish 在 scheduler.py，_find、_enqueue 在 scheduler_loop.py，_dispatch 在 scheduler_work.py；单看一组，mypy 不知道
self 上有这些。
  - 状态只写类型；混进来的方法会整个换掉的（tick 里的 self.started_at = now、close 里的 self._closed = True）写一个值，原因见 ../app_types.py。
  - 方法只写签名，实现在别的文件里；签名改了、这里没跟着改，mypy 会报 [override]。
  - 这些只在类型检查时存在：运行时 SchedulerPart 是下面那个空的类，Scheduler 的行为、MRO 里的方法和不拆开的时候一模一样。

新加一组方法时：class SchedulerXxx(SchedulerPart)，加进 Scheduler 的基类；mypy 报"没有这个属性"时，照着它的定义在下面补一行。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import threading
    from typing import Protocol

    from ..tasks import TaskStore
    from .scheduler import Throttle
    from .scheduler_run import Pending
    from .server import WebApp

    class SchedulerPart(Protocol):
        web: WebApp
        store: TaskStore
        limit: int
        _lock: threading.Lock
        _idle: threading.Condition
        _enqueuing: threading.Lock
        _queue: list[Pending]
        _wake: threading.Event
        _closed: bool = False
        started_at: float | None = None
        _progress: Throttle

        def clock(self) -> float: ...
        def _publish(self) -> None: ...
        def _find(self, task_id: str) -> Pending | None: ...
        def _enqueue(self, task: dict, now: float, *, due: float | None, catch_up: bool, missed: int, manual: bool) -> Pending | None: ...
        def _dispatch(self) -> None: ...

else:

    class SchedulerPart:
        """运行时什么都没有：上面的那些只是写给 mypy 看的类型。"""
