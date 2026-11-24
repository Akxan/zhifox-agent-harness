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
