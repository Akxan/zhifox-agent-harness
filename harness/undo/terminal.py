"""终端里的"改了 N 个文件"和 /undo（网页上的样子见 webui/page/scripts/file-changes.js，规矩见 undo/__init__.py）。

终端不存会话（关掉就没了），所以这里只在内存里记着这段对话的那几个事件（file_changes、file_undo），
用来推出每个文件的状态（restore.fold），和网页上用会话里的事件是同一个办法。/new 开始新的一段时换一个新的（见 cli.py）：
撤回的说明是附在这段对话的下一条消息上的，新的一段的模型不知道上一段改过什么。

/undo 撤回最近一次改了文件的那一轮：先把要做的列出来，要确认的、做不了的写明为什么，问你一句，答 y 才做。
重做、挑着撤回其中几个文件在网页界面上做。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..i18n import k, t
from ..tools.shell import TOOL_NAME as SHELL_TOOL
from .journal import recording

KINDS = {"created": k("新增"), "modified": k("修改"), "overwritten": k("覆盖")}


class TerminalChanges:
    def __init__(self, app: Any, ui: Any):
        self.app = app
        self.ui = ui
        self.events: list[dict] = []  # 这次运行里的 file_changes、file_undo 事件
        self.turns = 0  # 跑过几轮（"2 轮之前"要用）
        self.last: tuple[str, int] | None = None  # 最近一次改了文件的那一轮：（改动的编号，它是第几轮）

    def record(self, work: Callable[[], None], sends_message: bool) -> None:
        """跑一轮（work），把这一轮文件工具改的文件记下来，跑完说一句改了哪些。
        sends_message：这一轮发了新的用户消息（重新生成不发）：附在它前面的"文件撤回过"的说明留住了，就不用再说（见 notes.py）。"""
        backend = self.app.agent.backend
        journal, start = self.app.changes.start(), backend.mark()
        try:
            with recording(journal):
                work()
        finally:
            self.turns += 1
            events = self.ui.turn_events
            ran = any(event.get("name") == SHELL_TOOL and (event.get("type") == "tool_call" or event.get("step") == "tool_call") for event in events)
            summary = self.app.changes.finish(journal, commands=ran)
            if summary is not None:
                self.events.append({"type": "file_changes", **summary})
                if summary["set"]:
                    self.last = (summary["set"], self.turns)
                self._show(summary)
            if sends_message:
                self.app.changes.notes.settle(self.app.conversation.id, sent=backend.turn_at(start))

    def _show(self, summary: dict) -> None:
        files = [
            f"{item['path']}（{t(KINDS[item['kind']])}{_counts(item)}）"
            for item in summary["files"]
        ]  # fmt: skip
        if files:
            self.ui.info(t("改了 {n} 个文件：{files}。输入 /undo 可以撤回。", n=len(files), files=t("、").join(files)))
        if summary["untracked"]:
            self.ui.info(t("还有 {n} 个文件改了但没有记下（一轮最多记这么多），撤不回来。", n=summary["untracked"]))
        if summary.get("commands"):
            self.ui.info(t("这一轮还执行过命令：命令改动的文件没法自动撤回。"))

    def undo(self) -> None:
        """/undo：撤回最近一次改了文件的那一轮。"""
        if self.last is None:
            self.ui.error(t("这段对话里还没有可以撤回的文件改动。"))
            return
        set_id, number = self.last
        changes = self.app.changes
        preview = changes.act("undo", set_id, self.events, check=True)
        if preview.error and not (preview.ready or preview.blocked):
            self.ui.error(preview.error)
            return
        asks = {item["index"]: item for item in preview.asks}
        lines = []
        for item in preview.ready:
            what = t("删掉：这是那一轮新建的") if item["kind"] == "created" else t("恢复成那一轮之前的样子")
            lines.append(f"    {item['path']}（{what}）")
            lines += [f"      ⚠ {reason}" for reason in asks.get(item["index"], {}).get("why", [])]
        for item in preview.blocked:
            lines.append(f"    ✕ {item['path']}：{'；'.join(item['why'])}" + t("（不撤回）"))
        if not preview.ready:
            self.ui.error(t("没有能撤回的文件：") + "\n" + "\n".join(lines))
            return
        ago = self.turns - number
        head = t("撤回上一轮改的文件：") if ago == 0 else t("撤回 {n} 轮之前改的文件：", n=ago)
        if not self.ui.agree(head + "\n" + "\n".join(lines)):
            self.ui.info(t("已取消。"))
            return
        result = changes.act("undo", set_id, self.events, [item["index"] for item in preview.ready], force=True)
        if result.event is not None:
            self.events.append({"type": "file_undo", **result.event})
            changes.notes.add(self.app.conversation.id, result.notes, set_id)
            self.ui.info(t("已撤回 {n} 个文件。下一条消息会告诉助手这些文件变了。", n=len(result.event["files"])))
        for item in result.blocked:
            self.ui.error(t("没能撤回 {path}：{why}", path=item["path"], why="；".join(item["why"])))
        if result.event is None and not result.blocked:
            self.ui.error(result.error)


def _counts(item: dict) -> str:
    """"+3 −1" 这一截（算不出来的不写）。"""
    added, removed = item.get("added"), item.get("removed")
    if added is None or removed is None:
        return ""
    return f" +{added} −{removed}"
