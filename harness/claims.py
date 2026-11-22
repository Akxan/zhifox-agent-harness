"""谁正在往哪一段会话里写：同一段会话，同一时间只让一边往里加内容。

一段会话可能有两个"窗口"：你在 Telegram 里和智狐聊的那一段，在网页的会话列表里也看得到、打得开、能接着聊。
两边各自在内存里拿着一份历史，要是同时各聊一轮，后存盘的那一边会把先存的那一轮整个盖掉。

所以每一轮开始之前先"认领"这段会话（take），这一轮结束、存完盘再放开（release）：
  - 网页上发消息时，这段会话正被 Telegram 那边认领着：不等，直接告诉你"正在 Telegram 上进行"；
  - Telegram 那边要开始一轮时，网页正在这段会话里跑一轮：等它跑完（它通常很快就结束），再从磁盘读最新的接着聊。
打开、改名、删除一段正被别处认领着的会话，也会被拦下来。

这里只管"谁拿着"，不管会话的内容；放开以后谁先来谁先拿。
"""

from __future__ import annotations

import threading
import time


class Claims:
    def __init__(self) -> None:
        self._changed = threading.Condition()
        self._held: dict[str, str] = {}  # 会话编号 → 谁拿着（"web"，或者 "telegram:12345" 这样的聊天）

    def take(self, conversation_id: str, who: str, timeout: float = 0.0) -> str | None:
        """认领一段会话。拿到了（或者本来就是自己拿着）返回 None；最多等 timeout 秒还被别人拿着，返回拿着的那一个。"""
        deadline = time.monotonic() + timeout
        with self._changed:
            while (holder := self._held.get(conversation_id)) not in (None, who):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return holder
                self._changed.wait(remaining)
            self._held[conversation_id] = who
            return None

    def release(self, conversation_id: str, who: str) -> None:
        with self._changed:
            if self._held.get(conversation_id) == who:
                del self._held[conversation_id]
                self._changed.notify_all()

    def holder(self, conversation_id: str) -> str | None:
        with self._changed:
            return self._held.get(conversation_id)

    def held(self) -> dict[str, str]:
        """现在谁拿着哪些会话（会话编号 → 谁）。从备份恢复之前看一眼：聊天里正在进行的一轮，不能换掉它底下的数据（见 webui/restore_api.py）。"""
        with self._changed:
            return dict(self._held)
