"""Agent 循环 —— 整个 harness 的核心。

模型只会做一件事：读入一段对话，写出下一段回复。它不记得上一次请求，也执行不了任何操作。
让它变成"助手"的是这个循环：

    用户说了一句话
      └─► 把【系统提示词 + 工具说明书 + 到目前为止的全部对话】发给模型
            ├─ 模型回复文字               → 显示给用户，这一轮结束
            └─ 模型说"我想调用工具 X"     → 检查权限 → 执行 → 把结果追加进对话 → 再发给模型
                                                                              （回到上一步，循环）

"下一步做什么"每次都由模型决定，循环只负责照办并把结果告诉它。这就是 agent。

这个循环不关心背后是哪家的模型。和具体厂商打交道的细节（请求怎么发、历史怎么存）
都在 providers/ 目录里，循环只通过 backend 的几个方法使用它们。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from .config import Config
from .permissions import Permissions
from .providers.base import Backend, Step, StreamUI, ToolCall, ToolResult, Usage
from .safe import clean_text
from .tools import ToolError, ToolRegistry

WEEKDAYS = "一二三四五六日"


class UI(StreamUI, Protocol):
    """循环需要界面做的事。终端界面的实现在 ui.py，网页界面的在 webui/ui.py。"""

    def tool_call(self, name: str, tool_input: object) -> None: ...
    def tool_result(self, text: str, is_error: bool) -> None: ...
    def confirm(self, description: str) -> tuple[bool, str]: ...
    def auto_allowed(self) -> None: ...  # 刚才那次工具调用本来要问用户，但用户已经设置成自动允许


class Agent:
    def __init__(
        self,
        backend: Backend,
        config: Config,
        tools: ToolRegistry,
        permissions: Permissions,
        ui: UI,
        clock: Callable[[], datetime] = datetime.now,
    ):
        self.backend = backend  # 当前使用的模型；换模型就是换掉它
        self.config = config
        self.tools = tools
        self.permissions = permissions
        self.ui = ui
        self.clock = clock
        self.usage = Usage()  # 本次运行的累计用量
        self.usage_by_model: dict[str, Usage] = {}  # 按模型分开记，因为各个模型价格不同
        self.on_usage: Callable[[], None] | None = None  # 每调用完一次模型就通知一下（界面用它刷新用量）

    @property
    def messages(self) -> list:
        """对话历史。它保存在 backend 里，因为各家模型的历史格式不一样。"""
        return self.backend.messages

    def reset(self) -> None:
        """开始一段新对话。记忆和待办存在磁盘上，不受影响。"""
        self.backend.reset()

    # ---- 一轮对话 ---------------------------------------------------------

    def run_turn(self, user_text: str) -> None:
        """处理用户的一条消息，直到模型给出最终答复。"""
        # 粘贴进来的文字里可能有半个表情这样编码不了的字符。它一旦进了历史，之后每次请求都发不出去，
        # 所以在入口处换掉（见 safe.py）
        user_text = clean_text(user_text)
        start = self.backend.mark()
        # 当前时间每次都不同，所以附在消息里，而不是写进系统提示词（那样会破坏缓存）
        self.backend.add_user([self._context_note(), user_text])
        try:
            self._loop(start)
        except BaseException:
            # 出错或被打断时，如果模型还一步都没走完，就撤回这条用户消息，
            # 这样用户重新输入时对话里不会出现两遍。已经走完的步骤则保留在历史里。
            if self.backend.mark() == start + 1:
                self.backend.rollback(start)
            raise

    def _loop(self, start: int) -> None:
        for _ in range(self.config.max_steps):
            step = self.backend.step(self.ui)  # 调用一次模型；回复已经由 backend 追加进历史
            self._record(step.usage)

            if step.status == "refused":
                # 被拒绝的内容留在历史里的话，后面多半还会被拒，所以整轮撤销
                self.backend.rollback(start)
                reason = f"（类别：{step.note}）" if step.note else ""
                self.ui.info(f"模型拒绝了这个请求{reason}，这一轮对话已撤销。可以换个说法再试。")
                return

            if step.status == "paused":
                continue  # 服务器端的工具（联网搜索）还没做完，原样再请求一次让它接着做

            if not step.tool_calls:
                if step.truncated:
                    self.ui.info("回复达到了长度上限，被截断了。")
                return  # 模型没有要调用的工具了：这一轮结束

            # 执行工具，结果追加进历史，然后回到循环开头，让模型看到结果后决定下一步
            self._run_tools(step)

        self.ui.info(f"这一轮已经调用了 {self.config.max_steps} 次模型，先停在这里。输入“继续”可以接着做。")

    def _record(self, usage: Usage) -> None:
        self.usage.add(usage)
        self.usage_by_model.setdefault(self.backend.model, Usage()).add(usage)
        if self.on_usage is not None:
            self.on_usage()

    def _context_note(self) -> str:
        now = self.clock()
        return f"<context>当前时间：{now:%Y-%m-%d %H:%M} 星期{WEEKDAYS[now.weekday()]}</context>"

    # ---- 执行工具 ---------------------------------------------------------

    def _run_tools(self, step: Step) -> None:
        """依次执行模型要求的工具，并把所有结果追加进历史。"""
        results: list[ToolResult] = []
        try:
            for call in step.tool_calls:
                self.ui.tool_call(call.name, call.input)
                if step.truncated:
                    # 回复在生成工具参数的中途被截断，参数不完整，不能执行
                    text, is_error = "这次调用的参数在生成中途被截断，没有执行。请重新调用。", True
                else:
                    text, is_error = self._run_one(call)
                text = clean_text(text)  # 工具的输出同样不可信（比如文件名里有编码不了的字符）
                results.append(ToolResult(call.id, text, is_error))
                self.ui.tool_result(text, is_error)
        finally:
            # 每个工具调用都必须有对应的结果，否则下一次请求会被 API 拒绝。
            # 所以即使中途被打断（Ctrl-C 或界面上的"停止"），也要给没做完的调用补上一条结果，
            # 再把结果追加进历史。写在 finally 里，保证无论怎样退出都会执行。
            answered = {result.id for result in results}
            for call in step.tool_calls:
                if call.id not in answered:
                    results.append(ToolResult(call.id, "用户中断了操作，这个工具没有执行完。", True))
            self.backend.add_tool_results(results)

    def _run_one(self, call: ToolCall) -> tuple[str, bool]:
        """执行一次工具调用，返回（给模型看的结果，是否出错）。"""
        if call.error:
            return call.error, True  # 模型给的参数没法解析，把问题告诉它
        tool = self.tools.get(call.name)
        if tool is None or tool.run is None:
            return f"没有可以在本地执行的工具 {call.name}。", True

        # 先过权限这一关
        question = self.permissions.review(call.name, call.input)
        if question is not None:
            if self.permissions.auto_allows(call.name, call.input):
                self.ui.auto_allowed()  # 没有问用户就放行了：告诉界面一声，记录里要看得出来
            else:
                allowed, feedback = self.ui.confirm(question)
                if not allowed:
                    return "用户拒绝了这个操作。" + (f"用户说：{feedback}" if feedback else ""), True

        # 工具出错不会让程序崩溃：错误信息作为结果返回给模型，由它决定怎么办
        try:
            return tool.run(call.input) or "（没有输出）", False
        except ToolError as exc:
            return str(exc), True
        except Exception as exc:  # noqa: BLE001 —— 故意捕获所有异常：工具里的 bug 不应该让整个程序退出
            return f"工具内部出错：{type(exc).__name__}: {exc}", True
