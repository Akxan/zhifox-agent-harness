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

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from .attachments import Attachment
from .config import Config
from .i18n import t
from .permissions import Permissions
from .pricing import usage_key
from .providers.base import Backend, Step, StreamUI, ToolCall, ToolResult, Usage
from .safe import clean_text
from .tools import ToolError, ToolRegistry
from .tools.base import ToolOutput
from .tools.computer import HALTED as COMPUTER_HALTED
from .tools.computer import TOOLSET as COMPUTER

WEEKDAYS = "一二三四五六日"
# 同一轮里一串工具包的调用（"点这里、输入、按回车"），前面一个失败了，后面的都不执行，结果写这句话。
# 原文是 Anthropic 的文档规定的（https://platform.claude.com/docs/en/agents-and-tools/tool-use/browser-use-tool 的 Batch actions），一字不改。
# 电脑工具包有它自己的一句（多了一个 computer，见 tools/computer.py 的 HALTED）：后面那个调用是电脑工具包的，就用那一句
HALTED = "Not executed: an earlier action in this turn failed."
HALT_TEXTS = {"computer": COMPUTER_HALTED}


class UI(StreamUI, Protocol):
    """循环需要界面做的事。终端界面的实现在 ui.py，网页界面的在 webui/ui.py。"""

    def tool_call(self, name: str, tool_input: object) -> None: ...
    # preview：只给人看的东西（浏览器截图的小图和大图编号，见 ToolOutput.preview）；大多数工具没有，是 None
    def tool_result(self, text: str, is_error: bool, preview: dict | None = None) -> None: ...
    # agent：是哪个子 Agent 在问（见 agents/runner.py）；主助手自己问的时候是空的
    def confirm(self, description: str, agent: str = "") -> tuple[bool, str]: ...
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
        # 每条用户消息前面还要附上的说明。每一个都拿到到目前为止的对话历史，返回要附的文字（不用附就返回空的）。
        # 比如"可用的 Skill 变了"（见 skills/catalog.py）：这类会变的信息和时间一样，不能写进系统提示词
        self.notes: list[Callable[[list], str]] = []
        # 每调用完一次模型，把"哪一家、哪个模型、用了多少、什么时候"交出去（记账用，见 ledger.py 和 app.py）。
        # 循环自己不管钱怎么算、记在哪儿，只负责在每次调用之后说一声
        self.on_call: Callable[[str, str, Usage, datetime], None] | None = None
        # 一段对话的第一条消息发出之前调用（这时历史还是空的）。app.py 用它换上这段对话的工具：
        # 工具在一段对话里不能变（原因见 providers/claude.py），只有在还什么都没发过的时候换才不会出问题
        self.on_start: Callable[[], None] | None = None
        # 上一轮是怎么结束的：done 答完了，refused 被拒绝，truncated 回复被截断，steps 用完了 max_steps 次。
        # 主助手用不上它（这些情况都已经在界面上说了）；把任务交给子 Agent 的那一边要靠它告诉主助手"做完没有"
        self.outcome = ""

    def use_tools(self, tools: ToolRegistry) -> None:
        """换一套工具（循环执行的、和发给模型的说明书一起换）。只在一段对话开始之前用。"""
        self.tools = tools
        self.backend.use_tools(tools)

    @property
    def messages(self) -> list:
        """对话历史。它保存在 backend 里，因为各家模型的历史格式不一样。"""
        return self.backend.messages

    def reset(self) -> None:
        """开始一段新对话。记忆和待办存在磁盘上，不受影响。"""
        self.backend.reset()

    # ---- 一轮对话 ---------------------------------------------------------

    def run_turn(self, user_text: str, attachments: Sequence[Attachment] | None = None) -> None:
        """处理用户的一条消息，直到模型给出最终答复。attachments 是随消息发来的附件（已经检查过，见 attachments.py）。"""
        # 粘贴进来的文字里可能有半个表情这样编码不了的字符。它一旦进了历史，之后每次请求都发不出去，
        # 所以在入口处换掉（见 safe.py）
        user_text = clean_text(user_text)
        if self.on_start is not None and not self.backend.export():
            self.on_start()
        start = self.backend.mark()
        self.outcome = ""
        # 当前时间每次都不同，所以附在消息里，而不是写进系统提示词（那样会破坏缓存）。
        # 只发了附件、没写字的时候，没有"用户的话"这一段（API 不接受空的文字块）
        notes = [text for text in (note(self.messages) for note in self.notes) if text]
        parts = [self._context_note(), *notes] + ([user_text] if user_text.strip() else [])
        # 附件和文字是同一条消息：出错撤回时一起撤回，不会只剩下半条
        self.backend.add_user(parts, attachments or ())
        self._finish_turn(start)

    def retry_turn(self, start: int) -> None:
        """重新生成最后一轮的回答（网页上的"重新生成"、终端里的 /retry，见 rewind.py）。

        start 是这一轮开始时记下的位置（backend.mark()），这一轮的用户消息就在那里（调用的人先用 backend.turn_at 查过）。
        历史退回到这条用户消息之后：用户消息原样留着，连同当时的时间说明和附件，所以这一次请求的开头和上一次
        一字不差，缓存照样命中；模型这一轮写的、做的（思考、回答、工具调用和它们的结果）全部去掉，再照常跑一遍。

        只能是最后一轮。去掉的只是历史的末尾，前面的一个字都不动：要是改了中间的某一轮，它后面的缓存全部作废，
        后面每一块思考的签名也都对不上了，API 会拒绝请求（原因见 providers/claude.py 开头的规矩）。
        """
        self.backend.rollback(start + 1)
        self.outcome = ""
        self._finish_turn(start)

    def _finish_turn(self, start: int) -> None:
        """用户消息已经在 start 这个位置上了：跑循环，直到这一轮结束。"""
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
                self.outcome = "refused"
                if step.note:
                    self.ui.info(t("模型拒绝了这个请求（类别：{category}），这一轮对话已撤销。可以换个说法再试。", category=step.note))
                else:
                    self.ui.info(t("模型拒绝了这个请求，这一轮对话已撤销。可以换个说法再试。"))
                return

            if step.status in ("paused", "compacted"):
                # paused：服务器端的工具（联网搜索）还没做完，原样再请求一次让它接着做。
                # compacted：适配层刚把较早的对话压缩成了摘要（那次调用的用量上面已经记下），现在才发真正的请求
                continue

            if not step.tool_calls:
                self.outcome = "truncated" if step.truncated else "done"
                if step.truncated:
                    self.ui.info(t("回复达到了长度上限，被截断了。"))
                return  # 模型没有要调用的工具了：这一轮结束

            # 执行工具，结果追加进历史，然后回到循环开头，让模型看到结果后决定下一步
            self._run_tools(step)

        self.outcome = "steps"
        self.ui.info(t("这一轮已经调用了 {n} 次模型，先停在这里。输入“继续”可以接着做。", n=self.config.max_steps))

    def _record(self, usage: Usage) -> None:
        now = self.clock()  # 只看一次钟：本次运行的估算和账本按同一个时刻算价格
        self.usage.add(usage)
        # 按模型分开记。价格分时段的模型（DeepSeek），闲时的用量另记一份，算钱时按闲时价（见 pricing.usage_key）
        self.usage_by_model.setdefault(usage_key(self.backend.model, now), Usage()).add(usage)
        if self.on_call is not None:  # 先记账再通知界面：界面刷新时，"今天""本月"已经包括这一次了
            self.on_call(self.backend.provider, self.backend.model, usage, now)
        if self.on_usage is not None:
            self.on_usage()

    def _context_note(self) -> str:
        now = self.clock()
        return f"<context>当前时间：{now:%Y-%m-%d %H:%M} 星期{WEEKDAYS[now.weekday()]}</context>"

    # ---- 执行工具 ---------------------------------------------------------

    def _run_tools(self, step: Step) -> None:
        """依次执行模型要求的工具，并把所有结果追加进历史。

        工具包的调用（Claude 的浏览器、电脑工具包）常常是一串连着的动作："点搜索框、输入、按回车"，后一个依赖前一个。
        所以按顺序做，其中一个失败了（包括你拒绝了），这一轮后面的调用都不做，结果写 HALTED 那句话（文档的规定）。
        操作电脑的替身工具（GPT、Gemini 用的 computer_*）也一样：你拒绝了"点这个输入框"，后面的"打字"就不能照样打下去，
        字会进到不知道什么地方。
        """
        results: list[ToolResult] = []
        halted = False
        try:
            for call in step.tool_calls:
                self.ui.tool_call(call.shown, call.input)
                if step.truncated:
                    # 回复在生成工具参数的中途被截断，参数不完整，不能执行
                    output = ToolOutput("这次调用的参数在生成中途被截断，没有执行。请重新调用。", is_error=True)
                elif halted:
                    output = ToolOutput(HALT_TEXTS.get(call.toolset, HALTED), is_error=True)
                else:
                    output = self._run_one(call)
                    halted = output.is_error and (bool(call.toolset) or self._operates_computer(call))
                text = clean_text(output.text)  # 工具的输出同样不可信（比如文件名里有编码不了的字符）
                results.append(ToolResult(call.id, text, output.is_error, output.images, output.state, output.state_only))
                # HALTED 那句是写给模型的英文原文（文档规定的），给人看的换成界面的语言
                shown = t("没有执行：这一轮里前面的一步失败了。") if text in (HALTED, *HALT_TEXTS.values()) else text
                self.ui.tool_result(shown, output.is_error, output.preview)
        finally:
            # 每个工具调用都必须有对应的结果，否则下一次请求会被 API 拒绝。
            # 所以即使中途被打断（Ctrl-C 或界面上的"停止"），也要给没做完的调用补上一条结果，
            # 再把结果追加进历史。写在 finally 里，保证无论怎样退出都会执行。
            answered = {result.id for result in results}
            for call in step.tool_calls:
                if call.id not in answered:
                    results.append(ToolResult(call.id, "用户中断了操作，这个工具没有执行完。", True))
            self.backend.add_tool_results(results)

    def _operates_computer(self, call: ToolCall) -> bool:
        """是不是操作电脑的替身工具（computer_*）。"""
        tool = self.tools.get(call.name, call.toolset)
        return tool is not None and tool.stands_in_for == COMPUTER

    def _run_one(self, call: ToolCall) -> ToolOutput:
        """执行一次工具调用，返回结果（给模型看的文字、是否出错、结果里的图片……）。"""
        if call.error:
            return ToolOutput(call.error, is_error=True)  # 模型给的参数没法解析，把问题告诉它
        tool = self.tools.get(call.name, call.toolset)
        if tool is None or tool.run is None:
            return ToolOutput(f"没有可以在本地执行的工具 {call.shown}。", is_error=True)
        refusal = tool.check(call.input) if tool.check is not None and isinstance(call.input, dict) else None
        if refusal:
            return ToolOutput(refusal, is_error=True)  # 这个工具在这里不做这件事（见 Tool.check）：不用问用户，直接告诉模型

        # 先过权限这一关
        question = self.permissions.review(call.name, call.input, tool)  # 把工具本身也给它：MCP 的工具要靠它认出是哪个服务器的
        if question is not None:
            if self.permissions.auto_allows(call.name, call.input, tool):
                self.ui.auto_allowed()  # 没有问用户就放行了：告诉界面一声，记录里要看得出来
            else:
                allowed, feedback = self.ui.confirm(question)
                if not allowed:
                    return ToolOutput("用户拒绝了这个操作。" + (f"用户说：{feedback}" if feedback else ""), is_error=True)

        # 工具出错不会让程序崩溃：错误信息作为结果返回给模型，由它决定怎么办
        try:
            output = tool.run(call.input)
        except ToolError as exc:
            return ToolOutput(str(exc), is_error=True)
        except Exception as exc:  # noqa: BLE001 —— 故意捕获所有异常：工具里的 bug 不应该让整个程序退出
            return ToolOutput(f"工具内部出错：{type(exc).__name__}: {exc}", is_error=True)
        if isinstance(output, ToolOutput):
            return output if output.text or output.state_only else replace(output, text="（没有输出）")
        return ToolOutput(output or "（没有输出）")
