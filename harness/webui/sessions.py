"""网页上同时进行的几段对话：每段一个 Session，WebApp 管着一份名单（Sessions）和"你正看着哪一段"。

为什么要这样：以前只有一把对话锁（WebApp._turn），你的这一轮在跑的时候，打开别的会话、开新对话、在别处发消息都得等。
可定时任务和聊天通道早就能和你的对话同时跑了：每次运行各有一份"会被停下"的东西（见 app.py 的 _side_run）。
你的对话也照这个做法：每段对话各有一份（app.py 的 Chat），再加上它自己的事件日志和一把锁，就是这里的 Session。

一段"活着的"对话（Session）：
  chat      助手这一侧的东西（app.py 的 Chat）：Agent（适配层、历史、用量）、会话、shell、浏览器和电脑的 session、
            MCP 的快照和调用记在谁名下
  ui        它自己的事件日志（WebUI）。页面只读你正看着的那一段的日志（见 server.py 的 _send_events）
  lock      这一段的锁：谁拿着它，谁就在这一段里进行一轮（发消息、重新生成），或者正在改它（换模型、改名、撤回文件……）。
            发消息时在请求的线程里拿，这一轮跑完时在跑它的那个线程里放（所以是 Lock，不是 RLock）
  running   正在进行一轮（从开始到 chat_api._run_turn 收尾）。同时最多 config.MAX_PARALLEL_CHATS 段
  unsaved、discard_ok   上一次存盘失败的原因；被提醒过"离开会丢内容"的那个操作（见 WebApp._before_leaving）

什么时候有、什么时候没：
  - 一开始只有一段（build_app 建的那一段），你正看着它。
  - 你正看着的那一段在跑，你打开别的会话、开新对话：它留在名单里接着跑（"在后台"），给你看的换成新建的一段。
    它没在跑的话，和以前一样直接换掉它的内容（还是同一个 Session、同一份日志），名单里还是只有它。
  - 后台的一段跑完了、存好了盘：从名单里拿掉（东西都在磁盘上，再打开就从磁盘读），会话栏上留一个"跑完了""出错了"的标记，
    你打开它的时候去掉。没存上盘的留着（内容只在内存里），等你打开它、或者退出时再存一次。
    第一条消息就没答出来的（磁盘上什么都没有）也留着，标着"出错了"：不然你不在眼前的时候它就悄悄没了，你都不知道为什么。
  - 同一段会话最多只有一个 Session：打开一段正在后台跑的会话，就是换过去看它（事件重放出来，流式输出接着来）。

锁的顺序（只能从前往后拿，拿着后面的不去等前面的）：
  WebApp._switch（RLock）→ WebApp._publishing → Sessions._lock → 每段 WebUI 自己的锁
  - _switch：改动"哪几段活着、正看着哪一段"的操作一个一个来（打开、新对话、删除当前这一段、换到另一家……），
    拿不到就回答"忙"，不等。各段的 Session.lock 只用"试一下"的方式拿（acquire(blocking=False)），所以拿着 _switch 不会卡在别人身上。
  - _publishing：算状态快照、发出去要一口气做完（见 WebApp.publish_state）；换"正看着哪一段"也拿着它，快照里就不会是半截换过去的样子。
  - Sessions._lock：只保护名单、正看着的是谁、几段在跑、没看过的标记。拿着它只做几行赋值，不调用别的东西
    （除了 watching 里读一下那一段 WebUI 的日志）。
  - 要丢掉一段 Session，先拿着它的 lock 再从名单里拿掉；拿到一段的 lock 以后要再看一眼它还在不在名单里（Sessions.alive），
    不在了就当没拿到：不然可能在一段刚被丢掉的会话里开始一轮，谁也看不到它。

WebApp._turn 现在是 AllIdle（见下面）："没有任何一段在跑、也开始不了"。全局的操作（改记忆、改 Skill……）拿它。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from ..app import Chat
    from .ui import WebUI

# 会话栏上的标记（state 里的 live，页面照着画）：
RUNNING = "running"  # 在跑
CONFIRM = "confirm"  # 在等你确认（最显眼）
ERROR = "error"  # 在后台跑完了、出了错（或者没存上盘），你还没看
DONE = "done"  # 在后台跑完了，你还没看


@dataclass(eq=False)
class Session:
    """一段活着的对话（见文件开头）。"""

    chat: Chat
    ui: WebUI
    lock: threading.Lock = field(default_factory=threading.Lock)
    running: bool = False
    unsaved: str = ""  # 上一次存盘失败的原因。空字符串表示内容都在磁盘上了
    discard_ok: str = ""  # 用户已经被提醒过一次"离开会丢内容"的那个操作；紧接着再做一次同样的操作就放行
    # 这一段从事件的哪个编号起成了"你正看着的"（见 server.py 的 _send_events：浏览器断线重连时报上来的编号比它小，
    # 说明浏览器还没收到过这一段的日志，要从头补发）。一开始的那一段是 0
    since: int = 0
    media: tuple = (None, ())  # 这一段里的附件，和算它时的依据（见 media_api._live_media）

    @property
    def id(self) -> str:
        """这一段现在的会话编号（新对话、换到另一家时会变）。"""
        return self.chat.conversation.id


class Sessions:
    """活着的几段对话、你正看着哪一段、后台跑完了你还没看的那几段（见文件开头）。"""

    def __init__(self, first: Session):
        self._lock = threading.Lock()
        self._released = threading.Condition(self._lock)  # 有一段的 lock 放开了、有一段跑完了：等着的人（AllIdle、wait_idle）再看一眼
        self._all: list[Session] = [first]
        self._viewed = first
        self._unseen: dict[str, str] = {}  # 在后台跑完了、你还没打开看的会话：编号 → DONE / ERROR

    # ---- 读 --------------------------------------------------------------------

    @property
    def viewed(self) -> Session:
        with self._lock:
            return self._viewed

    def all(self) -> list[Session]:
        with self._lock:
            return list(self._all)

    def alive(self, session: Session) -> bool:
        with self._lock:
            return session in self._all

    def find(self, conversation_id: str) -> Session | None:
        with self._lock:
            return next((session for session in self._all if session.id == conversation_id), None)

    def watching(self, cursor: int, watched: WebUI | None) -> tuple[WebUI, bool, list[dict], int]:
        """事件流用（见 server.py 的 _send_events）：（你正看着的那一段的日志，要不要从头补发，要发的事件，它被 wake 叫过几次）。

        cursor 是浏览器已经收到的最后一个编号，watched 是这个连接上一次读的是哪一段的日志（刚连上时是 None）。
        换着看了另一段、或者刚连上而 cursor 不是这一段的（比它被看着的起点 since 小，或者比谁都新），要从头补发。
        都在同一把锁里读：换着看的那一刻（view）也拿着它，所以这里读到的事件，编号一定都比换过去以后那一段的 since 小。"""
        with self._lock:
            session = self._viewed
            ui = session.ui
            restart = ui is not watched and (watched is not None or (cursor > 0 and (cursor < session.since or cursor >= ui.next_id())))
            return ui, restart, ui.events_after(0 if restart else cursor, timeout=0), ui.wakes

    def running(self) -> int:
        """正在进行一轮的有几段。"""
        with self._lock:
            return sum(1 for session in self._all if session.running)

    def live(self) -> dict[str, str]:
        """会话栏上的标记：会话编号 → RUNNING / CONFIRM / ERROR / DONE。正看着的那一段也在里面（页面不给它画标记）。"""
        with self._lock:
            sessions, marks = list(self._all), dict(self._unseen)
        viewed = self.viewed
        for session in sessions:
            if session.ui.waiting():
                marks[session.id] = CONFIRM
            elif session.running:
                marks[session.id] = RUNNING
            elif session.unsaved and session is not viewed:
                marks[session.id] = ERROR
        return marks

    # ---- 改 --------------------------------------------------------------------

    def add(self, session: Session) -> None:
        with self._lock:
            self._all.append(session)

    def remove(self, session: Session) -> None:
        """从名单里拿掉（调用的人拿着它的 lock，见文件开头）。正看着的那一段不能拿掉。"""
        with self._lock:
            if session is not self._viewed and session in self._all:
                self._all.remove(session)
            self._released.notify_all()

    def view(self, session: Session) -> Session:
        """换成看 session（它已经在名单里）。返回原来正看着的那一段。调用的人拿着 WebApp._switch 和 _publishing。"""
        with self._lock:
            previous, self._viewed = self._viewed, session
            if session is not previous:
                session.since = session.ui.next_id()
            self._unseen.pop(session.id, None)  # 打开看了：会话栏上"跑完了""出错了"的标记去掉
            return previous

    def view_live(self, conversation_id: str) -> tuple[Session | None, Session]:
        """这段会话正活着（在后台跑着，或者没存上盘）的话，换过去看它。返回（它，原来正看着的那一段）；没活着是（None，正看着的）。
        "找到"和"换过去"在同一把锁里做：它刚好在这一刻跑完、要被拿掉的话（finish），两件事只会有一件先发生。"""
        with self._lock:
            found = next((session for session in self._all if session.id == conversation_id), None)
            previous = self._viewed
            if found is None or found is previous:
                return found, previous
            self._viewed = found
            found.since = found.ui.next_id()
            self._unseen.pop(conversation_id, None)
            return found, previous

    def start(self, session: Session, limit: int) -> bool:
        """这一段要开始一轮（调用的人拿着它的 lock）。已经有 limit 段在跑的话不让开始，返回 False。"""
        with self._lock:
            if sum(1 for other in self._all if other.running) >= limit:
                return False
            session.running = True
            return True

    def finish(self, session: Session, outcome: str) -> bool:
        """这一段的这一轮结束了（调用的人还拿着它的 lock）。不是你正看着的那一段的话：会话栏上留一个标记（outcome：DONE、ERROR，
        空的是不用标，比如你自己停下的），存好了盘的从名单里拿掉。返回拿掉了没有（拿掉了的，调用的人再关掉它的浏览器标签页这些）。"""
        with self._lock:
            session.running = False
            dropped = False
            if session is not self._viewed:
                if outcome:
                    self._unseen[session.id] = outcome
                # 存好了盘才拿掉。没存上的、第一条消息就没答出来（磁盘上什么都没有）的留着：内容只在内存里
                if not session.unsaved and session.chat.conversation.saved and session in self._all:
                    self._all.remove(session)
                    dropped = True
            self._released.notify_all()
            return dropped

    def forget(self, conversation_id: str) -> None:
        """这段会话删掉了：它的标记也不要了。"""
        with self._lock:
            self._unseen.pop(conversation_id, None)

    def unlock(self, session: Session) -> None:
        """放开一段的 lock，告诉等着的人（AllIdle、wait_idle）。"""
        session.lock.release()
        with self._lock:
            self._released.notify_all()

    def wait(self, ready, timeout: float) -> bool:
        """等到 ready() 成立，最多等 timeout 秒（每次有一段的 lock 放开、有一段跑完时再看一眼）。"""
        deadline = time.monotonic() + timeout
        with self._lock:
            while not ready():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._released.wait(remaining)
            return True

    def idle(self) -> bool:
        """没有一段在进行一轮。调用的人拿着 self._lock（见 wait）。"""
        return not any(session.running for session in self._all)


class AllIdle:
    """以前的对话锁 WebApp._turn 现在是这个：拿到它，就是"没有任何一段在进行一轮（或者正在被改），而且拿着它的时候谁也开始不了"。

    给全局的操作用：改记忆、改 Skill，以后的恢复备份……它们原来拿 _turn，是因为"助手这一轮可能正在用它"（比如正读着这条记忆、
    马上要改其中一段）。现在可能有好几段同时在跑，就要它们都停着才做。只关系到某一段的操作（发消息、换模型、改名、撤回文件……）
    拿那一段自己的锁（Session.lock，见 WebApp._holding），不拿这个。

    用法和 threading.Lock 一样：acquire(blocking=False) / release()，也可以 with。必须由拿它的那个线程放开；不能重复拿。
    拿着它的线程再去拿某一段的锁（WebApp._holding）算拿到了：那一段本来就在它手里。
    """

    def __init__(self, switch: threading.RLock, sessions: Sessions):
        self._switch = switch
        self._sessions = sessions
        self._held: list[Session] = []
        self._owner: int | None = None

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self._try():
            return True
        if not blocking:
            return False
        limit = float("inf") if timeout < 0 else timeout
        deadline = time.monotonic() + limit
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            # 等到有一段放开了锁再试（最多等一小会儿再试一次：_switch 放开时不会通知）
            self._sessions.wait(lambda: False, min(remaining, 0.05))
            if self._try():
                return True

    def _try(self) -> bool:
        if not self._switch.acquire(blocking=False):
            return False
        held: list[Session] = []
        for session in self._sessions.all():
            if not session.lock.acquire(blocking=False):
                for taken in held:
                    self._sessions.unlock(taken)
                self._switch.release()
                return False
            held.append(session)
        self._held, self._owner = held, threading.get_ident()
        return True

    def release(self) -> None:
        held, self._held, self._owner = self._held, [], None
        for session in held:
            self._sessions.unlock(session)
        self._switch.release()

    def locked(self) -> bool:
        """有一段正在进行一轮（或者正在被改），或者有人拿着它。"""
        return self._owner is not None or any(session.lock.locked() for session in self._sessions.all())

    def owns(self, session: Session) -> bool:
        """这个线程拿着它，而且 session 的锁就在它手里。"""
        return self._owner == threading.get_ident() and session in self._held

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
