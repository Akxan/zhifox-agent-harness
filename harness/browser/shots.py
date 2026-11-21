"""截图的大图：存在 data/browser/shots/ 里，按内容的 SHA-256 起名。

对话里的工具卡片上放的是一张很小的预览图（几 KB，直接写在事件里，会话文件里也有它）。点开它时，
页面拿着编号来要大图（/api/browser/shot），由这里读出来。大图不写进事件：每刷新一次页面，整段会话的事件都要重发一遍，
几十张大图会把它拖得很慢。

大图只留 BROWSER_SHOT_DAYS 天（config.py），旧的删掉。删了以后会话里还看得到小图，点开时说一声"大图已经过期"。
按内容起名的好处：同一张截图（页面没变，连截两次）只存一份；页面给的编号也只能是 64 个十六进制字符，拼不出别的路径。
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from pathlib import Path

from ..config import BROWSER_SHOT_DAYS
from ..safe import private_dir, write_file

SHOT_ID = re.compile(r"[0-9a-f]{64}")
PRUNE_EVERY = 3600  # 最多一小时清一次旧的


class ShotStore:
    def __init__(self, folder: Path, clock=time.time):
        self.folder = folder
        self.clock = clock
        self._lock = threading.Lock()
        self._pruned = 0.0

    def save(self, jpeg: bytes) -> str:
        """存一张截图，返回它的编号。存不下（磁盘满了）返回空字符串：只是点开时看不到大图，不影响别的。"""
        shot_id = hashlib.sha256(jpeg).hexdigest()
        try:
            private_dir(self.folder)
            path = self.folder / f"{shot_id}.jpg"
            if not path.exists():
                write_file(path, jpeg)
            self.prune()
        except OSError:
            return ""
        return shot_id

    def read(self, shot_id: str) -> bytes | None:
        if not SHOT_ID.fullmatch(shot_id):
            return None
        path = self.folder / f"{shot_id}.jpg"
        try:
            return path.read_bytes() if path.is_file() and not path.is_symlink() else None
        except OSError:
            return None

    def prune(self, force: bool = False) -> int:
        """删掉超过 BROWSER_SHOT_DAYS 天的大图。返回删了几张。"""
        now = self.clock()
        with self._lock:
            if not force and now - self._pruned < PRUNE_EVERY:
                return 0
            self._pruned = now
        cutoff = now - BROWSER_SHOT_DAYS * 86400
        removed = 0
        if not self.folder.is_dir():
            return 0
        for path in self.folder.glob("*.jpg"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed
