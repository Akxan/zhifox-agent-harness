"""终端界面：显示模型的输出和工具调用，读取用户输入，向用户请求确认。

界面和循环是分开的。循环（agent.py）只调用界面的几个方法，并不关心界面长什么样。
所以同一个循环可以接上不同的界面：这个文件是终端版，webui/ 目录里是网页版。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit import prompt as ask
from prompt_toolkit.history import FileHistory

from .config import Config

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"

YES = {"y", "yes", "是", "好", "允许"}
NO = {"", "n", "no", "否", "不"}


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_call(name: str, tool_input: object) -> str:
    """把一次工具调用压缩成一行，显示给用户看。"""
    if not isinstance(tool_input, dict):
        return f"{name} {tool_input!r}"
    if "command" in tool_input and "path" in tool_input:  # 文件工具和记忆工具
        return f"{name} {tool_input['command']} {tool_input['path']}"
    if set(tool_input) >= {"old_path", "new_path"}:  # 记忆工具的重命名
        return f"{name} rename {tool_input['old_path']} → {tool_input['new_path']}"
    if name == "run_command":
        return f"{name} $ {_short(str(tool_input.get('command')), 200)}"
    return f"{name} {_short(json.dumps(tool_input, ensure_ascii=False), 200)}"


def block_notice(block: Any) -> tuple[str, str] | None:
    """模型回复里不是逐字输出的那些内容块（联网搜索、压缩历史、换模型），各用一句话说明。

    返回（级别，文字）。级别有四种："tool" 服务器工具开始执行，"result" 它的结果，
    "error" 它失败了，"info" 其他提示。不需要显示的块返回 None。
    终端界面和网页界面都用这个函数，所以两边显示的内容是一致的。
    """
    kind = block.type
    if kind == "server_tool_use":
        return "tool", describe_call(block.name, block.input)
    if kind == "web_search_tool_result":
        if isinstance(block.content, list):
            return "result", f"找到 {len(block.content)} 条结果"
        return "error", f"搜索失败：{getattr(block.content, 'error_code', '未知错误')}"
    if kind == "web_fetch_tool_result":
        error_code = getattr(block.content, "error_code", None)
        return ("error", f"读取网页失败：{error_code}") if error_code else None
    if kind == "compaction":
        return "info", "对话已经很长，较早的内容被压缩成了摘要"
    if kind == "fallback":
        return "info", f"{block.from_.model} 拒绝了这个请求，已改由 {block.to.model} 继续"
    return None


class TerminalUI:
    def __init__(self, history_file: Path | None = None):
        self._color = sys.stdout.isatty()
        self._interactive = sys.stdin.isatty() and sys.stdout.isatty()
        self._session: PromptSession | None = None
        if self._interactive:
            history = None
            if history_file is not None:
                history_file.parent.mkdir(parents=True, exist_ok=True)
                history = FileHistory(str(history_file))  # 按上方向键可以找回以前输入过的话
            self._session = PromptSession(history=history)
        self._streaming: str | None = None  # 正在逐字输出的内容类型："thinking" 或 "text"

    # ---- 基础输出 ---------------------------------------------------------

    def _paint(self, color: str, text: str) -> str:
        return f"{color}{text}{RESET}" if self._color else text

    def _write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def _line(self, color: str, text: str) -> None:
        self.end_stream()
        self._write(self._paint(color, text) + "\n")

    def info(self, text: str) -> None:
        self._line(DIM, text)

    def error(self, text: str) -> None:
        self._line(RED, text)

    def plain(self, text: str) -> None:
        self._line("", text)

    def banner(self, config: Config) -> None:
        self.plain(self._paint(BOLD, "Harness 个人助手"))
        self.info(f"模型：{config.model}    工作目录：{config.workspace}")
        self.info("直接输入想说的话；/help 查看命令；Ctrl-C 中断当前回复；Ctrl-D 或 /exit 退出。")

    # ---- 输入 -------------------------------------------------------------

    def read_input(self) -> str | None:
        """读取用户输入的一行。返回 None 表示用户要退出。"""
        self.end_stream()
        try:
            if self._session is not None:
                self._write("\n")
                return self._session.prompt("你> ")
            return input()
        except KeyboardInterrupt:
            return ""  # 在输入时按 Ctrl-C：清空这一行，不退出
        except EOFError:
            return None

    def confirm(self, description: str) -> tuple[bool, str]:
        """请用户确认一个操作。返回（是否允许，用户附带的话）。"""
        self.end_stream()
        self._write(self._paint(YELLOW, f"\n⚠️  助手想要{description}"))
        if not self._interactive:
            self._write("\n")
            return False, "当前不是交互式终端，没有人可以确认。"
        try:
            answer = ask("允许吗？y 允许；回车拒绝；也可以直接输入你的意见来拒绝 > ").strip()
        except EOFError:
            return False, ""
        if answer.lower() in YES:
            return True, ""
        return False, "" if answer.lower() in NO else answer

    # ---- 模型的流式输出 ---------------------------------------------------

    def stream_event(self, event: Any) -> None:
        """模型一边生成，这里一边显示。event 是 SDK 解析好的流式事件。"""
        if event.type == "thinking":  # 思考摘要的一小段
            self._begin("thinking")
            self._write(self._paint(DIM, event.thinking))
        elif event.type == "text":  # 回答正文的一小段
            self._begin("text")
            self._write(event.text)
        elif event.type == "content_block_stop":  # 一个内容块结束了
            self._block_finished(event.content_block)

    def _begin(self, kind: str) -> None:
        if self._streaming == kind:
            return
        self.end_stream()
        self._streaming = kind
        if kind == "thinking":
            self._write(self._paint(DIM, "💭 "))

    def end_stream(self) -> None:
        if self._streaming is not None:
            self._write("\n")
            self._streaming = None

    def _block_finished(self, block: Any) -> None:
        if block.type in ("thinking", "text"):
            self.end_stream()
            return
        notice = block_notice(block)
        if notice is None:
            return
        level, text = notice
        if level == "tool":
            self._line(CYAN, f"🌐 {text}")
        elif level == "result":
            self.info(f"   ↳ {text}")
        elif level == "error":
            self.error(f"   ↳ {text}")
        else:
            self.info(f"（{text}）")

    # ---- 工具调用 ---------------------------------------------------------

    def tool_call(self, name: str, tool_input: object) -> None:
        self._line(CYAN, f"🔧 {describe_call(name, tool_input)}")

    def tool_result(self, text: str, is_error: bool) -> None:
        self._line(RED if is_error else DIM, f"   ↳ {_short(text, 120)}")
