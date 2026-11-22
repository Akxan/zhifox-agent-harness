"""Chrome DevTools Protocol（CDP）的客户端，手写的，走一对管道。

CDP 是 Chrome 自己的"遥控协议"：开发者工具（F12 打开的那个面板）就是用它来看页面、点按钮、截图的。
协议本身很简单，一来一回都是 JSON：

    我们发：  {"id": 7, "method": "Page.navigate", "params": {"url": "https://example.com"}}
    它回：    {"id": 7, "result": {"frameId": "…"}}             ← 按 id 对上是哪一个请求的回答
    它也会主动说：{"method": "Page.loadEventFired", "params": {…}}  ← 事件，没有 id

## 为什么走管道，不走端口

常见的做法是让 Chrome 开一个调试端口（--remote-debugging-port=9222），再用 WebSocket 连上去。
问题是这个端口谁都能连：这台电脑上随便哪个程序、你打开的任何一个网页（浏览器可以向 127.0.0.1 发请求），
连上以后就能完全控制这个浏览器。

所以智狐用 --remote-debugging-pipe：Chrome 从第 3 号文件描述符读命令、往第 4 号写回答，每条消息末尾一个 \\0。
这两个描述符是启动 Chrome 时我们亲手交给它的（见 chrome.py），没有端口，别的程序无从连起。
还有一个好处：智狐退出（哪怕是被强行杀掉），管道的这一头就关了，Chrome 读到结尾会自己退出，不会留下没人管的浏览器。

## 会话（session）

一个 Chrome 里有很多"目标"（target）：每个标签页是一个。连接本身是对着整个浏览器的；要操作某个标签页，
先 Target.attachToTarget 拿到一个 sessionId，之后发给这个标签页的命令都带上它（"flatten" 模式，一条连接走到底）。

## 超时和停止

每个请求都有超时：Chrome 卡住了，工具也不会永远等下去。请求还可以记在某个"主人"名下（owner），
cancel(owner) 一次取消这个主人所有还在等的请求：你点"停止"时，只停你自己这一轮的浏览器操作，
定时任务里正在用浏览器的那一次不受影响（它们是不同的主人）。
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import BROWSER_CALL_TIMEOUT
from ..safe import clean, to_json

MAX_MESSAGE_BYTES = 64_000_000  # 一条消息最多这么大（一张整页截图的 base64 也就几 MB）。超过的当作 Chrome 出了问题


class CDPError(Exception):
    """Chrome 回答说这个命令没做成（比如找不到那个标签页、页面已经关了）。"""

    def __init__(self, method: str, message: str, code: int = 0):
        super().__init__(f"{method}: {message}")
        self.method = method
        self.message = message
        self.code = code


class CDPTimeout(CDPError):
    """等了 timeout 秒，Chrome 还没回答。"""


class CDPClosed(CDPError):
    """和 Chrome 的连接断了：它退出了、崩溃了，或者被我们关掉了。"""


class Cancelled(Exception):
    """这个请求还没等到回答，就被它的主人取消了（用户点了"停止"）。"""


@dataclass
class _Pending:
    """一个发出去、还在等回答的请求，或者一个在等某个事件的人。"""

    owner: object
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None
    match: Callable[[str, dict, str], bool] | None = None  # 等事件的才有：这个事件是不是要等的那一个

    def settle(self, result: Any = None, error: BaseException | None = None) -> None:
        if self.done.is_set():
            return
        self.result, self.error = result, error
        self.done.set()


class Connection:
    """一条到 Chrome 的连接。read_fd 读 Chrome 写来的，write_fd 写给 Chrome。几个线程可以同时用它。

    一个后台线程专门读：把回答交给等它的请求，把事件交给订阅了它的人。写的时候拿一把锁，
    免得两个线程同时写、两条消息搅在一起。
    """

    def __init__(self, read_fd: int, write_fd: int, on_close: Callable[[], None] | None = None):
        self._read_fd = read_fd
        self._write_fd = write_fd
        self._on_close = on_close
        self._lock = threading.Lock()  # 保护下面这几样
        self._write_lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, tuple[str, _Pending]] = {}
        self._waiters: list[tuple[str, _Pending]] = []  # 在等某个事件的
        self._listeners: dict[str, list[Callable[[dict, str], None]]] = {}
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name="cdp-reader")
        self._reader.start()

    @property
    def closed(self) -> bool:
        return self._closed

    # ---- 发命令 -------------------------------------------------------------

    def call(
        self,
        method: str,
        params: dict | None = None,
        session: str = "",
        timeout: float = BROWSER_CALL_TIMEOUT,
        owner: object = None,
    ) -> dict:
        """发一个命令，等它的回答。session 是标签页的 sessionId（空的是对着整个浏览器）。

        Chrome 说没做成：CDPError；超时：CDPTimeout；连接断了：CDPClosed；被主人取消：Cancelled。
        """
        pending = _Pending(owner)
        with self._lock:
            if self._closed:
                raise CDPClosed(method, "连接已经断开")
            request_id = self._next_id
            self._next_id += 1
            self._pending[request_id] = (method, pending)
        message: dict[str, Any] = {"id": request_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        try:
            self._write(message)
            if not pending.done.wait(timeout):
                raise CDPTimeout(method, f"等了 {timeout:g} 秒还没有回答")
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
        if pending.error is not None:
            raise pending.error
        return pending.result

    def wait_for(
        self,
        method: str,
        match: Callable[[dict, str], bool] | None = None,
        timeout: float = BROWSER_CALL_TIMEOUT,
        owner: object = None,
    ) -> dict | None:
        """等一个事件（比如页面加载完了）。等到了返回它的参数，超时返回 None。被取消抛 Cancelled。

        要在触发它的命令之前先开始等，否则事件可能在开始等之前就过去了：用 expect() 拿到一个"等待"，
        发完命令再 wait()。
        """
        return self.expect(method, match, owner).wait(timeout)

    def expect(self, method: str, match: Callable[[dict, str], bool] | None = None, owner: object = None) -> Expectation:
        pending = _Pending(owner, match=lambda name, params, session: name == method and (match is None or match(params, session)))
        with self._lock:
            if self._closed:
                raise CDPClosed(method, "连接已经断开")
            self._waiters.append((method, pending))
        return Expectation(self, pending)

    def _forget(self, pending: _Pending) -> None:
        with self._lock:
            self._waiters = [(name, item) for name, item in self._waiters if item is not pending]

    def on(self, method: str, listener: Callable[[dict, str], None]) -> Callable[[], None]:
        """订阅一种事件：每来一个，调用 listener(params, sessionId)。返回取消订阅的函数。

        listener 在读线程里被调用，所以要快，而且不能反过来调用 call()（会等不到自己读的回答）。
        """
        with self._lock:
            self._listeners.setdefault(method, []).append(listener)

        def forget() -> None:
            with self._lock:
                if listener in self._listeners.get(method, []):
                    self._listeners[method].remove(listener)

        return forget

    def cancel(self, owner: object) -> None:
        """取消 owner 名下所有还在等的请求和事件：等的人立刻收到 Cancelled。"""
        with self._lock:
            mine = [pending for _, pending in self._pending.values() if pending.owner is owner and owner is not None]
            mine += [pending for _, pending in self._waiters if pending.owner is owner and owner is not None]
        for pending in mine:
            pending.settle(error=Cancelled())

    def close(self) -> None:
        """关掉连接（我们这一头的两个描述符）。所有还在等的都收到 CDPClosed。"""
        self._shut("连接已经关闭")
        _cancel_read(self._read_fd)
        for fd in (self._write_fd, self._read_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    # ---- 收发 ---------------------------------------------------------------

    def _write(self, message: dict) -> None:
        data = to_json(message).encode() + b"\0"
        with self._write_lock:
            view = memoryview(data)
            try:
                while view:
                    written = os.write(self._write_fd, view)
                    view = view[written:]
            except OSError as exc:  # 管道的那一头已经没人了：Chrome 退出了
                self._shut("Chrome 已经退出")
                raise CDPClosed(message.get("method", "?"), "Chrome 已经退出") from exc

    def _read_loop(self) -> None:
        buffer = bytearray()
        try:
            while True:
                chunk = os.read(self._read_fd, 1 << 16)
                if not chunk:
                    break  # Chrome 关掉了它那一头
                buffer += chunk
                while (end := buffer.find(b"\0")) >= 0:
                    raw, buffer = bytes(buffer[:end]), buffer[end + 1 :]
                    # 一条奇怪的消息不能让读线程停下：那样整条连接就聋了，等的人只能等到超时
                    with contextlib.suppress(Exception):
                        self._dispatch(raw)
                if len(buffer) > MAX_MESSAGE_BYTES:
                    break
        except OSError:
            pass
        self._shut("Chrome 已经退出")

    def _dispatch(self, raw: bytes) -> None:
        try:
            # 页面上的文字什么都可能有（半个表情之类），在进门的地方洗一遍（见 safe.py）
            message = clean(json.loads(raw))
        except (ValueError, RecursionError):
            return  # 读不懂的一条：丢掉，不影响后面的
        if not isinstance(message, dict):
            return
        if "id" in message:
            request_id = message["id"]
            if not isinstance(request_id, int) or isinstance(request_id, bool):
                return  # 我们发出去的编号都是整数：别的样子的回答不是给我们的
            with self._lock:
                entry = self._pending.get(request_id)
            if entry is None:
                return  # 等它的人已经超时走了
            asked, pending = entry  # 当初问的是哪个方法（不叫 method：下面的 method 是事件的名字，可能根本不是字符串）
            error = message.get("error")
            if isinstance(error, dict):
                code = error.get("code")
                code = code if isinstance(code, int) and not isinstance(code, bool) else 0
                pending.settle(error=CDPError(asked, str(error.get("message", "")), code))
            else:
                result = message.get("result")
                pending.settle(result=result if isinstance(result, dict) else {})
            return
        method, params = message.get("method"), message.get("params") or {}
        session = message.get("sessionId") or ""
        if not isinstance(method, str) or not isinstance(params, dict):
            return
        with self._lock:
            listeners = list(self._listeners.get(method, []))
            waiters = [pending for name, pending in self._waiters if name == method]
        for pending in waiters:
            if pending.match is not None and pending.match(method, params, session):
                pending.settle(result=params)
        for listener in listeners:
            with contextlib.suppress(Exception):  # 一个订阅者出错，不能让读线程停下（那样整条连接就聋了）
                listener(params, session)

    def _shut(self, reason: str) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending = [item for _, item in self._pending.values()] + [item for _, item in self._waiters]
            self._waiters = []
        for item in pending:
            item.settle(error=CDPClosed("?", reason))
        if self._on_close is not None:
            with contextlib.suppress(Exception):  # 在读线程里：出了错也只能算了，不能让它带着异常退出
                self._on_close()


class Expectation:
    """一个"等事件"的约定：先登记（Connection.expect），发完命令再等（wait）。"""

    def __init__(self, connection: Connection, pending: _Pending):
        self._connection = connection
        self._pending = pending

    def wait(self, timeout: float) -> dict | None:
        try:
            if not self._pending.done.wait(timeout):
                return None
            if self._pending.error is not None:
                raise self._pending.error
            return self._pending.result
        finally:
            self._connection._forget(self._pending)

    def cancel(self) -> None:
        self._connection._forget(self._pending)


def _cancel_read(fd: int) -> None:
    """Windows 上，读线程正卡在这根管道的 ReadFile 里时，直接关掉它会一直等下去（关闭要等那次读完）。
    先用 CancelIoEx 叫停那次读（Chromium 自己在同一个地方也是这么做的），读线程收到错误就退出了。macOS、Linux 上不用。"""
    if sys.platform != "win32":  # 和 os.name != "nt" 一样；写成 sys.platform，mypy 才知道下面 Windows 才有的名字只在 Windows 上用
        return
    import ctypes
    import msvcrt

    with contextlib.suppress(OSError, ValueError, AttributeError):
        ctypes.windll.kernel32.CancelIoEx(ctypes.c_void_p(msvcrt.get_osfhandle(fd)), None)
