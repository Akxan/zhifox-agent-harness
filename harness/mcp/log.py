"""一个 MCP 服务器的日志：它自己往 stderr 写的话，加上我们这边记下的事（启动了、退出了、出错了）。

服务器连不上、工具调用失败时，原因多半写在它自己的输出里（缺了某个环境变量、端口被占用、密钥不对），
所以把最近的几百行留着，在网页上点开这个服务器就能看到。只留最近的：有的服务器话很多，一直攒下去会把内存占满。
"""

from __future__ import annotations

import threading
import time
from collections import deque

from ..safe import clean_text

MAX_LINES = 300
MAX_LINE_CHARS = 2000


class ServerLog:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lines: deque[dict] = deque(maxlen=MAX_LINES)
        self.revision = 0  # 每多一行加一。页面靠它知道"日志变了，要重新取"

    def add(self, text: str, source: str = "stderr") -> None:
        """记一行。source："stderr" 服务器自己写的，"harness" 我们这边记的。"""
        line = clean_text(text.rstrip("\r\n"))
        if len(line) > MAX_LINE_CHARS:
            line = line[: MAX_LINE_CHARS - 1] + "…"
        with self._lock:
            self._lines.append({"time": time.time(), "source": source, "text": line})
            self.revision += 1

    def note(self, text: str) -> None:
        self.add(text, source="harness")

    def lines(self) -> list[dict]:
        with self._lock:
            return list(self._lines)

    def tail(self, count: int = 5) -> list[str]:
        """最后几行服务器自己写的，连接失败时附在原因后面。"""
        with self._lock:
            return [line["text"] for line in self._lines if line["source"] == "stderr" and line["text"].strip()][-count:]
