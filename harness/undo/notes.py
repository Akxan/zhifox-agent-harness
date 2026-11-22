"""撤回、重做以后告诉模型一声：附在这段会话的下一条消息前面（为什么见 undo/__init__.py 的"撤回以后告诉模型"）。

按会话分开攒着：你在 A 里撤回了，再去 B 聊，B 的模型不用知道；回到 A 接着聊时，A 的下一条消息带上。
发出去才算说过：那条消息在模型答出第一步之前就失败了（循环会把它从历史里撤回，见 Agent._finish_turn），
这段话留着，下一次再附上。所以分两步：text 只是看一眼要附什么，settle 在这一轮结束时确认它真的留在了历史里。
只在内存里：程序重启以后就没有了（模型下一次用到那个文件时，看一眼也就知道了）。

写给模型看的，所以是中文原文，不进翻译表。
"""

from __future__ import annotations

import threading

from ..config import UNDO_NOTE_LINES

# 每个文件做了什么，给模型的说法
SAID = {
    "undo": "恢复成了那一轮改之前的内容",
    "delete": "删掉了（它是那一轮新建的）",
    "redo": "重做了：又恢复成撤回之前的内容",
}


class Notes:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lines: dict[str, list[tuple[str, str]]] = {}  # 会话编号 → 还没告诉模型的那几行：（哪一轮的改动，这一行）
        self._offered: dict[str, int] = {}  # 会话编号 → 最近一次附上了前几行（还没确认发出去）

    def add(self, conversation_id: str, changes: list[tuple[str, str]], set_id: str = "") -> None:
        """记下这次做了什么：（给人看的路径，SAID 里的哪一种）。set_id 是哪一轮的改动（那一轮被倒回去时用，见 drop）。"""
        with self._lock:
            self._lines.setdefault(conversation_id, []).extend((set_id, f"- {path}：{SAID[what]}") for path, what in changes if what in SAID)

    def drop(self, conversation_id: str, set_ids: list[str]) -> None:
        """这几轮被倒回去了（编辑、重新生成，见 rewind.py）：关于它们的还没说的那几行不用说了，模型本来就不知道它们改过文件。"""
        with self._lock:
            kept = [item for item in self._lines.get(conversation_id, ()) if item[0] not in set_ids]
            if kept:
                self._lines[conversation_id] = kept
            else:
                self._lines.pop(conversation_id, None)
            self._offered.pop(conversation_id, None)

    def text(self, conversation_id: str) -> str:
        """这段会话的下一条消息要附的那段话；没有就是空的。"""
        with self._lock:
            lines = [line for _, line in self._lines.get(conversation_id, ())]
            self._offered[conversation_id] = len(lines)
        if not lines:
            return ""
        shown = lines[:UNDO_NOTE_LINES]
        if len(lines) > len(shown):
            shown.append(f"- ……还有 {len(lines) - len(shown)} 个文件")
        return (
            "<file_changes>\n用户在界面上撤回（或者重做）了之前对文件的改动，下面这些文件现在和你上次看到的不一样了：\n"
            + "\n".join(shown)
            + "\n你记得的这些文件的内容已经过时；再用到它们之前，先重新查看。\n</file_changes>"
        )

    def settle(self, conversation_id: str, sent: bool) -> None:
        """一轮结束：附上的那段话留在了历史里（sent），就不用再说了；没留住的下次再附。"""
        with self._lock:
            offered = self._offered.pop(conversation_id, 0)
            if sent and offered:
                del self._lines.setdefault(conversation_id, [])[:offered]
                if not self._lines[conversation_id]:
                    del self._lines[conversation_id]

    def pending(self, conversation_id: str) -> int:
        """还有几行没告诉模型（测试和页面用不上，终端的 /undo 之后说一句）。"""
        with self._lock:
            return len(self._lines.get(conversation_id, ()))
