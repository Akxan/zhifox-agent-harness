"""终端界面：显示模型的输出和工具调用，读取用户输入，向用户请求确认。

界面和循环是分开的。循环（agent.py）只调用界面的几个方法，并不关心界面长什么样。
所以同一个循环可以接上不同的界面：这个文件是终端版，webui/ 目录里是网页版。

终端也记一份和网页一模一样的事件日志（log）：屏幕上的字打出去就完了，用不着它；可会话要存盘（见 chat_cli.py），
网页打开一段会话时是靠重放 events 把它画出来的（见 webui/ui.py 的 load_events），所以终端里的每一轮也要留下同样的事件：
你的消息（带着倒回去用的位置 mark）、回答、工具卡片、确认和结果、运行记录。日志用的就是网页的那个类（WebUI），
事件长什么样、存之前怎么整理（export_events）只写在一处，两边存出来的文件一样，定时任务和聊天通道也是这么做的。
循环调用的每个方法在这里先写到屏幕上，再交给日志记一笔。只有 info、error 要分一下：斜杠命令（/help、/usage……）
也用它们往屏幕上写，那些不是对话的内容，所以只在一轮进行中（begin_turn 到 end_turn）才记。
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit import prompt as ask
from prompt_toolkit.history import FileHistory

from . import citations
from .attachments import Attachment
from .config import APP_NAME
from .display import describe_call, short
from .i18n import k, retell, t
from .webui.ui import WebUI, confirm_fields

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"

# 确认时认得的回答：中文、英文、西班牙语都行
YES = {"y", "yes", "是", "好", "允许", "allow", "s", "si", "sí", "permitir"}
NO = {"", "n", "no", "否", "不", "deny", "rechazar"}

# 没问你就放行的那一步后面标的一句。只读的命令、你自己加的命令写得更具体（为什么不问，见 permissions.Permissions.allowance）
AUTO_NOTE = k("（自动允许）")
AUTO_NOTES = {"readonly": k("（自动允许：只读的命令）"), "listed": k("（自动允许：你加的命令）")}


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
        # 这段对话的事件日志（见文件开头）。存盘时整理成会话里的 events；/retry、/edit 从这里找最后一轮，
        # 看这一轮做过什么可能改动了电脑的事（见 rewind.effects）；/undo 从这里推出每个文件现在的状态
        self.log = WebUI()
        self._in_turn = False  # 正在进行一轮（begin_turn 到 end_turn）：这时的 info、error 是对话的内容，记进日志
        self.last_trace: dict | None = None  # 最后一轮的运行记录（见 trace/），/trace 把它写出来

    def trace(self, record: dict) -> None:
        """一轮结束时循环交过来的运行记录：记进日志（存进会话，网页上点开看），也留着给 /trace。"""
        self.last_trace = record
        self.log.trace(record)

    # ---- 一轮的开始和结束、整段换掉（chat_cli.py 用）----------------------------

    def begin_turn(self, text: str, attachments: Sequence[Attachment] = (), mark: int | None = None) -> None:
        """一轮开始：日志里记下你的消息。mark 是这一轮开始时适配层的位置（Backend.mark），以后 /retry、/edit
        （还有网页上的编辑、重新生成）从这里倒回去（见 rewind.py）。消息已经在屏幕上了（是你自己打的），不再写一遍。"""
        self._in_turn = True
        self.log.begin_turn(text, attachments, mark=mark)

    def begin_again(self) -> None:
        """重新生成：你的那条消息还在日志里（倒回去时留着它），只开始新的一轮。"""
        self._in_turn = True
        self.log.begin_again()

    def end_turn(self) -> None:
        self.end_stream()
        self._in_turn = False
        self.log.end_turn()

    def saved_events(self) -> list[dict]:
        """这段对话显示过的内容，整理成会话文件里 events 的样子（和网页存的一样，见 WebUI.export_events）。"""
        return self.log.export_events()

    def load(self, events: list[dict]) -> None:
        """打开了一段保存过的会话：日志换成它的事件（之后接着记，存盘时以前的一个不少）。屏幕上不重放，
        最后几条消息由 chat_cli.py 写出来。/trace 照着它的最后一轮。"""
        self.log.load_events(events)
        traces = [event for event in self.log.export_events() if event.get("type") == "trace"]
        self.last_trace = {key: value for key, value in traces[-1].items() if key != "type"} if traces else None

    def reset(self) -> None:
        """开始新的一段对话：日志清空。"""
        self.log.reset()
        self.last_trace = None

    # ---- 基础输出 ---------------------------------------------------------

    def _paint(self, color: str, text: str) -> str:
        return f"{color}{text}{RESET}" if self._color else text

    def _write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def _line(self, color: str, text: str) -> None:
        self._flush()
        self._write(self._paint(color, text) + "\n")

    def info(self, text: str) -> None:
        self._line(DIM, text)
        if self._in_turn:
            self.log.info(text)

    def error(self, text: str) -> None:
        self._line(RED, text)
        if self._in_turn:
            self.log.error(text)

    def hint(self, text: str) -> None:
        """一行只在终端里显示、不记进会话的提示（和网页的 WebUI.hint 一样）。比如"改了 N 个文件"那一行：
        记进会话的是 file_changes 事件，网页照着它画成那一行，用不着再存一句话。"""
        self._line(DIM, text)

    def plain(self, text: str) -> None:
        self._line("", text)

    def banner(self, model: str, workspace: Path) -> None:
        self.plain(self._paint(BOLD, t("{name} · 个人助手", name=APP_NAME)))
        self.info(t("模型：{model}    工作目录：{workspace}", model=model, workspace=workspace))
        self.info(t("直接输入想说的话；/help 查看命令；Ctrl-C 中断当前回复；Ctrl-D 或 /exit 退出。"))

    # ---- 输入 -------------------------------------------------------------

    def read_input(self, default: str = "") -> str | None:
        """读取用户输入的一行。返回 None 表示用户要退出。default 是预先填好的文字（/edit 把上一条消息填进去，可以直接改）。"""
        self._flush()
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
        """请用户确认一个操作。返回（是否允许，用户附带的话）。agent 是哪个子 Agent 在问（主助手自己问时是空的）。

        日志里记下和网页一样的一张卡片（confirm）和它的结果（confirm_done）。卡片上多给的那个选择（"这段对话里截图不再问"，
        见 permissions.offering）终端里没有，不记：网页打开时不该画出一个你当时没见过的按钮。"""
        self.end_stream()
        fields = confirm_fields(description, agent)
        fields.pop("offer", None)
        confirm_id = self.log.publish("confirm", **fields)
        try:
            allowed, feedback = self._confirm(description, agent)
        except KeyboardInterrupt:  # 在问的时候按了 Ctrl-C：这一轮停下，卡片记成"已中断"
            self.log.publish("confirm_done", confirm=confirm_id, allowed=False, stopped=True)
            raise
        self.log.publish("confirm_done", confirm=confirm_id, allowed=allowed, stopped=False)
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
        self._flush()
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
        self.log.thinking(text)

    def text(self, text: str) -> None:
        self._begin("text")
        # 末尾的空白和换行先留着不写：这段文字要是引用了网页，记号 [1] 要紧跟在最后一个字后面，不能跑到下一行的开头
        chunk = self._held + text
        body = chunk.rstrip()
        self._held = chunk[len(body) :]
        self._write(body)
        self.log.text(text)  # 日志里记原样的文字（空白也在）：网页照着它画 Markdown

    def cite(self, sources: list[dict]) -> None:
        """联网搜索的回答引用了网页（见 citations.py）：在刚写完的那段文字后面写上 [1]，这段回答结束时列出来源。"""
        marks, _ = self._sources.add(sources)
        if marks and self._streaming == "text":
            self._write(self._paint(DIM, citations.marker(marks)))
        self.log.cite(sources)  # 网页一段一数编号（见 WebUI.cite），和这里的不一定一样，各数各的

    def _begin(self, kind: str) -> None:
        if self._streaming == kind:
            return
        self._flush()
        self._streaming = kind
        if kind == "thinking":
            self._write(self._paint(DIM, "💭 "))

    def end_stream(self) -> None:
        """循环说"正在逐字输出的这一段写完了"：屏幕上收个尾，日志里也记一笔（block_end）。"""
        self._flush()
        self.log.end_stream()

    def _flush(self) -> None:
        """屏幕上正在逐字写的那一段先收个尾（换行、列出来源），后面要另起一行写别的。

        只管屏幕，不碰日志：从思考换到写回答、写一行提示之前都要收尾，可网页那边只在循环说"写完了"时才记 block_end
        （从思考换到回答时 OpenAI 格式的几家不说），这里多记一个，两边存下的事件就对不上了。"""
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
        """一行提示。level：tool 服务器工具开始执行，result 它的结果，error 出错，info 其他。
        日志里记一个 notice（上面）；屏幕上只用 _line 写，不经过 info、error：那两个在一轮里也会记，就记了两遍。"""
        self.log.notice(level, text)
        if level == "tool":
            self._line(CYAN, f"🌐 {text}")
        elif level == "result":
            self._line(DIM, f"   ↳ {text}")
        elif level == "error":
            self._line(RED, f"   ↳ {text}")
        else:
            self._line(DIM, f"（{text}）")

    # ---- 工具调用 ---------------------------------------------------------

    def tool_call(self, name: str, tool_input: object) -> None:
        self._line(CYAN, f"🔧 {describe_call(name, tool_input)}")
        self.log.tool_call(name, tool_input)

    def tool_result(self, text: str, is_error: bool, preview: dict | None = None) -> None:
        # 浏览器的截图在终端里画不出来，只说一声大图存在哪儿（网页界面里点小图就能看）
        shot = f" · {t('截图已保存')}" if preview and preview.get("shot") else ""
        # 程序自己写的那几句话（zh() 给的）译成界面的语言；命令的输出、文件的内容原样（见 ../i18n.py 开头）
        self._line(RED if is_error else DIM, f"   ↳ {short(retell(text), 120)}{shot}")
        self.log.tool_result(text, is_error, preview)  # 日志里的小图照样留着：网页打开时画在工具卡片上

    def auto_allowed(self, why: str = "mode") -> None:
        self._line(DIM, "   " + t(AUTO_NOTES.get(why, AUTO_NOTE)))
        self.log.auto_allowed(why)

    # ---- 子 Agent（见 agents/runner.py）------------------------------------

    def subagent(self, step: str, **data: object) -> None:
        """子 Agent 的一步进展：缩进一层显示，看得出是它在做，不是主助手。日志里记下完整的一步，网页画成一张"交给 某某"的卡片。"""
        self.log.subagent(step, **data)
        name = str(data.get("agent", ""))
        if step == "start":
            self._line(CYAN, "🤖 " + t("交给「{agent}」：{task}", agent=name, task=short(str(data.get("task", "")), 120)))
        elif step == "tool_call":
            self._line(CYAN, f"   🔧 {data.get('summary', '')}")
        elif step == "tool_result":
            self._line(RED if data.get("is_error") else DIM, f"      ↳ {short(retell(data.get('text', '')), 100)}")
        elif step == "auto":
            self._line(DIM, "      " + t(AUTO_NOTES.get(str(data.get("why", "")), AUTO_NOTE)))
        elif step == "note":
            self._line(DIM, f"   （{data.get('text', '')}）")
        elif step == "done":
            # 这几行只写到屏幕上（_line）：日志里已经记了这一步（上面），网页照着它画在卡片上
            status = data.get("status")
            if status == "failed":
                self._line(RED, "   " + t("「{agent}」出错了：{error}", agent=name, error=data.get("error", "")))
            elif status == "stopped":
                self._line(DIM, "   " + t("「{agent}」被中断了。", agent=name))
            else:
                self._line(DIM, "   " + t("「{agent}」做完了，回答交给了主助手。", agent=name))

    def check_stop(self) -> None:
        pass  # 终端里的"停止"是 Ctrl-C：它直接打断正在运行的代码，不用这里去看

    def on_stop(self, callback: Callable[[], None]) -> Callable[[], None]:
        # 同样的原因：Ctrl-C 会让子 Agent 正在运行的命令一起收到信号（它们在同一个进程组里），不用另外登记
        return lambda: None
