"""终端里的"改了 N 个文件"和 /undo（网页上的样子见 webui/page/scripts/file-changes.js，规矩见 undo/__init__.py）。

这几个事件（file_changes、file_undo）和网页上一样记在这段对话的事件日志里（TerminalUI.log），跟着会话一起存盘（见 chat_cli.py）：
网页打开终端里聊的会话，照样有那一行"改了 N 个文件"，能在那里撤回、重做；终端 /open 打开网页上的会话，/undo 也认得它以前的改动。
每个文件现在的状态由这些事件推出来（restore.fold），和网页是同一个办法。/new、/open 换一段对话时换一个新的（见 chat_cli.py）：
撤回的说明是附在这段对话的下一条消息上的，别的那一段的模型不知道这一段改过什么。

/undo 撤回最近一次改了文件的那一轮：先把要做的列出来，要确认的、做不了的写明为什么，问你一句，答 y 才做。
重做、挑着撤回其中几个文件在网页界面上做。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..i18n import k, t
from ..tools.shell import TOOL_NAME as SHELL_TOOL
from .journal import recording
from .restore import sets_in

KINDS = {"created": k("新增"), "modified": k("修改"), "overwritten": k("覆盖")}


class TerminalChanges:
    def __init__(self, app: Any, ui: Any):
        self.app = app
        self.ui = ui

    def record(self, work: Callable[[], None], sends_message: bool) -> None:
        """跑一轮（work），把这一轮文件工具改的文件记下来，跑完说一句改了哪些。
        sends_message：这一轮发了新的用户消息（重新生成不发）：附在它前面的"文件撤回过"的说明留住了，就不用再说（见 notes.py）。"""
        backend = self.app.agent.backend
        journal, start = self.app.changes.start(), backend.mark()
        try:
            with recording(journal):
                work()
        finally:
            _, events = self.ui.log.last_turn_events()
            ran = any(event.get("name") == SHELL_TOOL and (event.get("type") == "tool_call" or event.get("step") == "tool_call") for event in events)
            summary = self.app.changes.finish(journal, commands=ran)
            if summary is not None:
                self.ui.log.publish("file_changes", **summary)  # 排在这一轮的运行记录之后、结束之前（和网页一样）
                self._show(summary)
            if sends_message:
                self.app.changes.notes.settle(self.app.conversation.id, sent=backend.turn_at(start))

    def _show(self, summary: dict) -> None:
        files = [
            f"{item['path']}（{t(KINDS[item['kind']])}{_counts(item)}）"
            for item in summary["files"]
        ]  # fmt: skip
        # 只写在屏幕上（hint）：会话里记的是上面那个 file_changes 事件，网页照着它画这一行
        if files:
            self.ui.hint(t("改了 {n} 个文件：{files}。输入 /undo 可以撤回。", n=len(files), files=t("、").join(files)))
        if summary["untracked"]:
            self.ui.hint(t("还有 {n} 个文件改了但没有记下（一轮最多记这么多），撤不回来。", n=summary["untracked"]))
        if summary.get("commands"):
            self.ui.hint(t("这一轮还执行过命令：命令改动的文件没法自动撤回。"))

    def undo(self) -> bool:
        """/undo：撤回最近一次改了文件的那一轮。返回 True 表示撤回了（日志里多了一个 file_undo 事件，要存盘）。"""
        events = self.ui.saved_events()
        found = sets_in(events)
        if not found:
            self.ui.error(t("这段对话里还没有可以撤回的文件改动。"))
            return False
        set_id = found[-1]
        changes = self.app.changes
        preview = changes.act("undo", set_id, events, check=True)
        if preview.error and not (preview.ready or preview.blocked):
            self.ui.error(preview.error)
            return False
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
            return False
        ago = _turns_after(events, set_id)
        head = t("撤回上一轮改的文件：") if ago == 0 else t("撤回 {n} 轮之前改的文件：", n=ago)
        if not self.ui.agree(head + "\n" + "\n".join(lines)):
            self.ui.info(t("已取消。"))
            return False
        result = changes.act("undo", set_id, events, [item["index"] for item in preview.ready], force=True)
        if result.event is not None:
            self.ui.log.publish("file_undo", **result.event)
            changes.notes.add(self.app.conversation.id, result.notes, set_id)
            self.ui.info(t("已撤回 {n} 个文件。下一条消息会告诉助手这些文件变了。", n=len(result.event["files"])))
        for item in result.blocked:
            self.ui.error(t("没能撤回 {path}：{why}", path=item["path"], why="；".join(item["why"])))
        if result.event is None and not result.blocked:
            self.ui.error(result.error)
        return result.event is not None


def _turns_after(events: list[dict], set_id: str) -> int:
    """改了这些文件的那一轮之后又跑了几轮（"2 轮之前"）：数它的 file_changes 事件后面有几个 turn_start（重新生成的也算一轮）。"""
    at = next(index for index, event in enumerate(events) if event.get("type") == "file_changes" and event.get("set") == set_id)
    return sum(event.get("type") == "turn_start" for event in events[at + 1 :])


def _counts(item: dict) -> str:
    """"+3 −1" 这一截（算不出来的不写）。"""
    added, removed = item.get("added"), item.get("removed")
    if added is None or removed is None:
        return ""
    return f" +{added} −{removed}"
