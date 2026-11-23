"""每次运行自己的一份：这段对话里允许过哪些网站、这条消息里读了几次网页。

"每次运行"和浏览器的 BrowserSession 一样分：你的每段对话一个（换一段对话、打开一段旧会话就换一个新的，从头再问），
每次定时任务一个，聊天通道里每段会话一个。子 Agent 用叫它的那一方的同一个（它拿到的是同一个工具）：
你在对话里允许过的网站，它去读不用再问；它读的次数也算在你这条消息里。

只记在内存里，不进会话文件：重启以后、重新打开这段会话，第一次读一个网站照样先问你。
几次读网页可能同时进行（子 Agent、同一步里的几个调用），所以拿着一把小锁。
"""

from __future__ import annotations

import threading


class FetchSession:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sites: set[str] = set()
        self._turn: object = None  # 现在数的是哪一轮（见 used、take）
        self._used = 0

    def allowed(self, site: str) -> bool:
        """这段对话里读过（问过你、你允许了，或者本来就不用问）这个网站了吗。"""
        with self._lock:
            return site in self._sites

    def allow(self, site: str) -> None:
        if site:
            with self._lock:
                self._sites.add(site)

    def used(self, turn: object) -> int:
        """turn 这一轮里读了几次。turn 是"这是哪一轮"的记号（Agent.turns）：和上次记下的不一样，就是新的一轮，从 0 数。"""
        with self._lock:
            return self._used if turn == self._turn else 0

    def take(self, turn: object, limit: int) -> bool:
        """在 turn 这一轮里用掉一次。limit 是每一轮最多几次（0 是不限）：到了上限交回 False，什么都不记。"""
        with self._lock:
            if turn != self._turn:
                self._turn, self._used = turn, 0
            if limit > 0 and self._used >= limit:
                return False
            self._used += 1
            return True
