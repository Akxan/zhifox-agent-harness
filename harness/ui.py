"""终端界面：显示模型的输出和工具调用，读取用户输入，向用户请求确认。

界面和循环是分开的。循环（agent.py）只调用界面的几个方法，并不关心界面长什么样。
所以同一个循环可以接上不同的界面：这个文件是终端版，webui/ 目录里是网页版。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit import prompt as ask
from prompt_toolkit.history import FileHistory

from . import citations
from .config import APP_NAME
from .display import describe_call, short
from .i18n import t

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"

# 确认时认得的回答：中文、英文、西班牙语都行
YES = {"y", "yes", "是", "好", "允许", "allow", "s", "si", "sí", "permitir"}
NO = {"", "n", "no", "否", "不", "deny", "rechazar"}


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
        self._held = ""  # 回答末尾还没写出来的空白（见 text）
        self._sources = citations.Numbering()  # 这段回答引用的网页（联网搜索后才有），结束时列出来
        # 这一轮里的工具调用和确认，记成和网页界面一样的事件。/retry、/edit 倒回这一轮之前，
        # 靠它看这一轮做过什么可能改动了电脑的事（见 rewind.effects），要先问你一次
        self.turn_events: list[dict] = []

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
        self.plain(self._paint(BOLD, t("{name} · 个人助手", name=APP_NAME)))
        self.info(t("模型：{model}    工作目录：{workspace}", model=model, workspace=workspace))
        self.info(t("直接输入想说的话；/help 查看命令；Ctrl-C 中断当前回复；Ctrl-D 或 /exit 退出。"))

    # ---- 输入 -------------------------------------------------------------

    def read_input(self, default: str = "") -> str | None:
        """读取用户输入的一行。返回 None 表示用户要退出。default 是预先填好的文字（/edit 把上一条消息填进去，可以直接改）。"""
        self.end_stream()
        try:
            if self._session is not None:
                self._write("\n")
                return self._session.prompt(t("你> "), default=default)
            return input()
        except KeyboardInterrupt:
            return ""  # 在输入时按 Ctrl-C：清空这一行，不退出
        except EOFError:
            return None

    @property
    def interactive(self) -> bool:
        return self._interactive

    def confirm(self, description: str, agent: str = "") -> tuple[bool, str]:
        """请用户确认一个操作。返回（是否允许，用户附带的话）。agent 是哪个子 Agent 在问（主助手自己问时是空的）。"""
        allowed, feedback = self._confirm(description, agent)
        self.turn_events.append({"type": "confirm", "outcome": "allowed" if allowed else "denied"})
        return allowed, feedback

    def agree(self, warning: str) -> bool:
        """问一句"要继续吗"，默认不继续（/retry、/edit 倒回一轮之前用，见 cli.py）。不是交互式终端时不继续。"""
        self._line(YELLOW, "\n⚠️  " + warning)
        if not self._interactive:
            return False
        try:
            return ask(t("继续吗？y 继续；回车取消 > ")).strip().lower() in YES
        except (EOFError, KeyboardInterrupt):
            return False

    def _confirm(self, description: str, agent: str) -> tuple[bool, str]:
        self.end_stream()
        asking = t("「{agent}」想要", agent=agent) if agent else t("助手想要")
        self._write(self._paint(YELLOW, "\n⚠️  " + asking + description) + "\n")
        if not self._interactive:
            self._write("\n")
            return False, "当前不是交互式终端，没有人可以确认。"  # 这句是给模型看的，不用翻译
        try:
            answer = ask(t("允许吗？y 允许；回车拒绝；也可以直接输入你的意见来拒绝 > ")).strip()
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
        # 末尾的空白和换行先留着不写：这段文字要是引用了网页，记号 [1] 要紧跟在最后一个字后面，不能跑到下一行的开头
        chunk = self._held + text
        body = chunk.rstrip()
        self._held = chunk[len(body) :]
        self._write(body)

    def cite(self, sources: list[dict]) -> None:
        """联网搜索的回答引用了网页（见 citations.py）：在刚写完的那段文字后面写上 [1]，这段回答结束时列出来源。"""
        marks, _ = self._sources.add(sources)
        if marks and self._streaming == "text":
            self._write(self._paint(DIM, citations.marker(marks)))

    def _begin(self, kind: str) -> None:
        if self._streaming == kind:
            return
        self.end_stream()
        self._streaming = kind
        if kind == "thinking":
            self._write(self._paint(DIM, "💭 "))

    def end_stream(self) -> None:
        if self._streaming is not None:
            self._write(self._held + "\n")
            self._held = ""
            self._streaming = None
        if self._sources.sources:
            self._write(self._paint(DIM, self._listing()) + "\n")
            self._sources.clear()

    def _listing(self) -> str:
        """来源列成几行：编号、标题、网站，下面一行是完整的网址（大多数终端里按住 ⌘ 或 Ctrl 点它就能打开）。"""
        lines = citations.listing(self._sources.sources).split("\n")
        out = [lines[0]]
        for line, source in zip(lines[1:], self._sources.sources):
            out += [f"  {line}", f"     {source['url']}"]
        return "\n".join(out)

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
        summary = describe_call(name, tool_input)
        self.turn_events.append({"type": "tool_call", "name": name, "summary": summary, "input": tool_input})
        self._line(CYAN, f"🔧 {summary}")

    def tool_result(self, text: str, is_error: bool, preview: dict | None = None) -> None:
        # 浏览器的截图在终端里画不出来，只说一声大图存在哪儿（网页界面里点小图就能看）
        shot = f" · {t('截图已保存')}" if preview and preview.get("shot") else ""
        self._line(RED if is_error else DIM, f"   ↳ {short(text, 120)}{shot}")

    def auto_allowed(self) -> None:
        self.info("   " + t("（自动允许）"))

    # ---- 子 Agent（见 agents/runner.py）------------------------------------

    def subagent(self, step: str, **data: object) -> None:
        """子 Agent 的一步进展：缩进一层显示，看得出是它在做，不是主助手。"""
        name = str(data.get("agent", ""))
        if step == "start":
            self._line(CYAN, "🤖 " + t("交给「{agent}」：{task}", agent=name, task=short(str(data.get("task", "")), 120)))
        elif step == "tool_call":
            self.turn_events.append({"type": "subagent", "step": "tool_call", "name": data.get("name"), "summary": data.get("summary"), "input": data.get("input")})
            self._line(CYAN, f"   🔧 {data.get('summary', '')}")
        elif step == "tool_result":
            self._line(RED if data.get("is_error") else DIM, f"      ↳ {short(str(data.get('text', '')), 100)}")
        elif step == "auto":
            self.info("      " + t("（自动允许）"))
        elif step == "note":
            self.info(f"   （{data.get('text', '')}）")
        elif step == "done":
            status = data.get("status")
            if status == "failed":
                self.error("   " + t("「{agent}」出错了：{error}", agent=name, error=data.get("error", "")))
            elif status == "stopped":
                self.info("   " + t("「{agent}」被中断了。", agent=name))
            else:
                self.info("   " + t("「{agent}」做完了，回答交给了主助手。", agent=name))

    def check_stop(self) -> None:
        pass  # 终端里的"停止"是 Ctrl-C：它直接打断正在运行的代码，不用这里去看

    def on_stop(self, callback: Callable[[], None]) -> Callable[[], None]:
        # 同样的原因：Ctrl-C 会让子 Agent 正在运行的命令一起收到信号（它们在同一个进程组里），不用另外登记
        return lambda: None
