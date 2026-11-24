"""让假服务器（fake_api.py、fake_openai.py）收下一个请求以后先不回答，测"停止"能不能马上生效（见 harness/providers/inflight.py）。

两种不回答：
  headers=True   回答的头先发出去（200、流式），然后一个事件都不发：模型在想、什么都还没写的样子
  headers=False  连头都不发：请求还在排队、本地的大模型还在加载的样子
压着的时候一直看着这条连接：智狐那边把它断开了（停止）就记下来（hangups），这个请求就此结束；
测试叫 release() 放行一个，才把写好的回复（then）发出去。

不靠固定的等待：压着的线程在 select 上同时等"连接断了"和"放行了"（放行是往一对 socket 里写一个字节叫醒它，
一个字节放行一个请求；先放行、后压上的，压上的那一刻就走），哪个先到算哪个。两样同时到时算"断了"：智狐已经不要了，不再回答。
"""

from __future__ import annotations

import select
import socket
import threading


def held(then: dict, headers: bool = True) -> dict:
    """一条先压着、放行以后才发出 then 的回复（then 是 fake_api.reply 或 fake_openai.chat 写好的回复）。"""
    return {"held": True, "headers": headers, "then": then}


class Hold:
    """一个假服务器上压着的请求：压过几个、对方断开过几个、放行。"""

    def __init__(self) -> None:
        self._changed = threading.Condition()
        self.held = 0  # 一共压过几个请求
        self.hangups = 0  # 压着的时候，对方把连接断开了几次
        # 叫醒压着的线程用的一对 socket：用到时才建（_pair），绝大多数测试根本不压请求。以前每个假服务器一建起来就开一对、
        # 关的时候也不关，一个测试多出四个文件描述符；一个测试进程跑上千个测试，开着的超过一千个，新连接的编号过了 1024，
        # macOS 上 select 就认不了（2026-11-24 CI 上 websocket 的几个测试因此一起失败，见 test_websocket.py）
        self._wake: socket.socket | None = None
        self._waker: socket.socket | None = None
        self._waiting = 0  # 正在 wait 里的线程有几个：最后一个离开时，关上了的这一对由它来关
        self._closed = False

    def _pair(self) -> tuple[socket.socket, socket.socket]:
        with self._changed:
            if self._wake is None or self._waker is None:
                self._wake, self._waker = socket.socketpair()
                self._wake.setblocking(False)  # 几个压着的线程一起醒来时，只有一个拿得到那个字节，别的接着等
            return self._wake, self._waker

    def release(self, count: int = 1) -> None:
        """放行 count 个（正压着的，或者接下来压上的）。"""
        self._pair()[1].send(b"x" * count)

    def wait_held(self, count: int = 1, timeout: float = 30) -> bool:
        """等到一共压过 count 个请求。"""
        with self._changed:
            return self._changed.wait_for(lambda: self.held >= count, timeout)

    def wait_hung_up(self, count: int = 1, timeout: float = 30) -> bool:
        """等到对方一共断开过 count 次。"""
        with self._changed:
            return self._changed.wait_for(lambda: self.hangups >= count, timeout)

    def wait(self, connection: socket.socket) -> bool:
        """压着这条连接上的请求。放行了返回 True；对方先断开了返回 False（记下来）。"""
        wake, _ = self._pair()
        with self._changed:
            self.held += 1
            self._waiting += 1
            self._changed.notify_all()
        try:
            while True:
                readable, _, _ = select.select([connection, wake], [], [])
                if connection in readable:
                    try:
                        data = connection.recv(65536)
                    except OSError:
                        data = b""
                    if not data:
                        with self._changed:
                            self.hangups += 1
                            self._changed.notify_all()
                        return False
                if wake in readable:
                    try:
                        if wake.recv(1):
                            return True
                    except BlockingIOError:
                        continue  # 被别的压着的线程拿走了
        finally:
            with self._changed:
                self._waiting -= 1
                last = self._closed and self._waiting == 0
            if last:
                self._close_pair()

    def close(self) -> None:
        """假服务器停下时：还压着的都放走，再关上那一对 socket。还有线程正在 select 它的话不能马上关，
        由最后一个离开 wait 的线程关（见 wait 的 finally）。"""
        with self._changed:
            self._closed = True
            if self._wake is None:
                return  # 从来没压过：没有要关的
            idle = self._waiting == 0
        self.release(64)
        if idle:
            self._close_pair()

    def _close_pair(self) -> None:
        with self._changed:
            pair, self._wake, self._waker = (self._wake, self._waker), None, None
        for sock in pair:
            if sock is not None:
                sock.close()
