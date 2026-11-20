"""终端界面：显示模型的输出和工具调用，读取用户输入，向用户请求确认。

界面和循环是分开的。循环（agent.py）只调用界面的几个方法，并不关心界面长什么样。
所以同一个循环可以接上不同的界面：这个文件是终端版，webui/ 目录里是网页版。
"""

from __future__ import annotations

import sys
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit import prompt as ask
from prompt_toolkit.history import FileHistory

from .config import APP_NAME
from .display import describe_call, short

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"

YES = {"y", "yes", "是", "好", "允许"}
NO = {"", "n", "no", "否", "不"}


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

    def banner(self, model: str, workspace: Path) -> None:
        self.plain(self._paint(BOLD, f"{APP_NAME} · 个人助手"))
        self.info(f"模型：{model}    工作目录：{workspace}")
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
    # 模型一边生成，适配层（providers/）一边调用下面这几个方法，文字就一小段一小段地显示出来。

    def thinking(self, text: str) -> None:
        self._begin("thinking")
        self._write(self._paint(DIM, text))

    def text(self, text: str) -> None:
        self._begin("text")
        self._write(text)

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

    def notice(self, level: str, text: str) -> None:
        """一行提示。level：tool 服务器工具开始执行，result 它的结果，error 出错，info 其他。"""
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
        self._line(RED if is_error else DIM, f"   ↳ {short(text, 120)}")
