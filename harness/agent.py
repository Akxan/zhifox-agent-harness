"""Agent 循环 —— 整个 harness 的核心。

模型只会做一件事：读入一段对话，写出下一段回复。它不记得上一次请求，也执行不了任何操作。
让它变成"助手"的是这个循环：

    用户说了一句话
      └─► 把【系统提示词 + 工具说明书 + 到目前为止的全部对话】发给模型
            ├─ 模型回复文字               → 显示给用户，这一轮结束
            └─ 模型说"我想调用工具 X"     → 检查权限 → 执行 → 把结果追加进对话 → 再发给模型
                                                                              （回到上一步，循环）

"下一步做什么"每次都由模型决定，循环只负责照办并把结果告诉它。这就是 agent。

循环在几个时刻告诉运行记录一声（self.trace，见 trace/）："记录点"。每个只占一行，名字说明是哪个时刻：
发出一次请求（request_began）、模型答完这一次（request_ended）、这次请求失败了、改用备用模型再试（request_failed）、
开始一次工具调用（tool_began）、
开始等你答复（approval_asked）、你答复了或者没问就放行了（approval_decided）、这次调用有结果了（tool_ended）、
一轮开始和结束（turn_began、turn_ended）。

你的钩子（self.hooks，见 hooks/）也挂在这几处，各占一行：你的消息进历史之前（UserPromptSubmit）、确认过了马上要执行一个
工具的时候（PreToolUse）、工具做完了（PostToolUse）、一轮答完了（Stop）。钩子只能让事情更严（拦下、再问你一次），
不能放宽确认的规矩；它交给模型的话只附在正在生成的那条消息、那条工具结果里，历史里已有的内容一个字都不动。

工具的结果一产生就先遮一遍密钥（self.redact，见 redact.py）：在它放进结果、交给 PostToolUse 钩子、到 tool_ended 记录点之前。
模型、历史、页面、运行记录、你的钩子看到的都是遮好的那一份。

一步里连着几个只读、又不用问你的调用（读几个文件、列待办……），同时跑（见 parallel.py）：线程里只执行工具本身，
上面这些（界面、记录点、钩子、遮密钥、放进结果）照旧在这个线程里、按调用的顺序一个一个做。别的调用和以前一样一个一个来。

这个循环不关心背后是哪家的模型。和具体厂商打交道的细节（请求怎么发、历史怎么存）
都在 providers/ 目录里，循环只通过 backend 的几个方法使用它们。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from . import fallback
from .attachments import Attachment
from .config import Config
from .hooks import NO_HOOKS, HookRun
from .i18n import t
from .parallel import Batch, Job
from .permissions import Permissions
from .pricing import usage_key
from .providers.base import (
    Backend,
    Step,
    StreamUI,
    ToolCall,
    ToolResult,
    Unavailable,
    Usage,
)
from .redact import Redaction, Redactor, note, redact_data
from .safe import clean_text
from .tools import Tool, ToolError, ToolRegistry
from .tools.base import ToolOutput
from .tools.computer import HALTED as COMPUTER_HALTED
from .tools.computer import TOOLSET as COMPUTER
from .trace import Tracer

logger = logging.getLogger("harness.agent")
WEEKDAYS = "一二三四五六日"
# 同一轮里一串工具包的调用（"点这里、输入、按回车"），前面一个失败了，后面的都不执行，结果写这句话。
# 原文是 Anthropic 的文档规定的（https://platform.claude.com/docs/en/agents-and-tools/tool-use/browser-use-tool 的 Batch actions），一字不改。
# 电脑工具包有它自己的一句（多了一个 computer，见 tools/computer.py 的 HALTED）：后面那个调用是电脑工具包的，就用那一句
HALTED = "Not executed: an earlier action in this turn failed."
HALT_TEXTS = {"computer": COMPUTER_HALTED}
INTERRUPTED = "用户中断了操作，这个工具没有执行完。"  # 被打断、没交出结果的调用补上的结果（见 _run_tools 的 finally）


class UI(StreamUI, Protocol):
    """循环需要界面做的事。终端界面的实现在 ui.py，网页界面的在 webui/ui.py。"""

    def tool_call(self, name: str, tool_input: object) -> None: ...
    # preview：只给人看的东西（浏览器截图的小图和大图编号，见 ToolOutput.preview）；大多数工具没有，是 None
    def tool_result(self, text: str, is_error: bool, preview: dict | None = None) -> None: ...
    # agent：是哪个子 Agent 在问（见 agents/runner.py）；主助手自己问的时候是空的
    def confirm(self, description: str, agent: str = "") -> tuple[bool, str]: ...
    # 刚才那次工具调用本来要问用户，但用户已经设置成自动允许。why 是为什么（见 Permissions.allowance）：
    # "readonly"（只读的命令）、"listed"（你自己加的不用问的命令），别的情况是 "mode"
    def auto_allowed(self, why: str = "mode") -> None: ...


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
        # 每调用完一次模型，把"哪一家、哪个模型、用了多少、什么时候"交出去（记账用，见 ledger.py 和 app_chat.py 的 App._wire）。
        # 循环自己不管钱怎么算、记在哪儿，只负责在每次调用之后说一声。它交回这一次算出来的钱（没有价格是 None），
        # 运行记录照着写，不另算一遍
        self.on_call: Callable[[str, str, Usage, datetime], float | None] | None = None
        # 运行记录（见 trace/）：这一轮每一次请求、每一次工具调用的时刻和数字。一轮结束时把整理好的记录交给 on_trace
        # （网页界面存进会话，终端界面留给 /trace，见 app_chat.py 的 App._wire）。子 Agent 的记录挂在叫它的那次调用下面（见 agents/runner.py）
        self.trace = Tracer()
        self.on_trace: Callable[[dict], None] | None = None
        # 遮住工具结果里像密钥的东西（见 redact.py）。默认就开着（只认环境变量里的密钥和固定的格式）；
        # app.py 换成连着 .env、自定义接口的密钥和设置里那个开关的。None 是不遮
        self.redact: Callable[[str], Redaction] | None = Redactor()
        # 备用模型（见 fallback.py）：给出（哪一家，你选的模型），交回这个模型暂时用不了时按顺序可以改用的同一家的模型。
        # 循环不管这份名单从哪儿来（app.py 让它每次现读设置），只在一次请求遇到 Unavailable 时问它一声。None：不换
        self.fallbacks: Callable[[str, str], list[str]] | None = None
        self._primary = ""  # 这一轮开始时用的模型（你选的那个）；改用了备用模型的话，一轮结束时换回它
        # 你的钩子（见 hooks/）：在下面几个记录点旁边各调用一次。没接上的（测试里自己组装的助手）什么都不做
        self.hooks: HookRun = NO_HOOKS
        # 一段对话的第一条消息发出之前调用（这时历史还是空的）。app_chat.py（App._wire、_fresh_start）用它换上这段对话的工具：
        # 工具在一段对话里不能变（原因见 providers/claude.py），只有在还什么都没发过的时候换才不会出问题
        self.on_start: Callable[[], None] | None = None
        # 上一轮是怎么结束的：done 答完了，refused 被拒绝，truncated 回复被截断，steps 用完了 max_steps 次。
        # 主助手用不上它（这些情况都已经在界面上说了）；把任务交给子 Agent 的那一边要靠它告诉主助手"做完没有"
        self.outcome = ""
        # 这个助手开始过几轮（重新生成也算新的一轮）。"每条消息最多几次"的工具拿它认"是不是新的一轮"（读网页，见 tools/fetch.py）
        self.turns = 0

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

    def run_turn(self, user_text: str, attachments: Sequence[Attachment] | None = None, again: bool = False) -> None:
        """处理用户的一条消息，直到模型给出最终答复。attachments 是随消息发来的附件（已经检查过，见 attachments.py）。
        again：这是在"重新生成"上一次没答出来的那一轮（那条消息没留在历史里，所以整条再发一次），运行记录上标出来。"""
        # 粘贴进来的文字里可能有半个表情这样编码不了的字符。它一旦进了历史，之后每次请求都发不出去，
        # 所以在入口处换掉（见 safe.py）
        user_text = clean_text(user_text)
        self.trace.turn_began(again)  # 记录点：一轮开始（从这里算起，包括下面拍 MCP 快照的时间）
        self.turns += 1
        if self.on_start is not None and not self.backend.export():
            self.on_start()
        start = self.backend.mark()
        self.outcome = ""
        self._finish_turn(start, lambda: self._submit(user_text, attachments or ()))

    def _submit(self, user_text: str, attachments: Sequence[Attachment]) -> bool:
        """把你的消息放进历史。被你的钩子拦下了就什么都不放（为什么由钩子那边告诉你），返回 False。"""
        verdict = self.hooks.prompt(self, user_text, attachments)  # 钩子：UserPromptSubmit
        if verdict.blocked:
            self.outcome = "blocked"
            return False
        # 当前时间每次都不同，所以附在消息里，而不是写进系统提示词（那样会破坏缓存）。钩子附的说明也一样。
        # 只发了附件、没写字的时候，没有"用户的话"这一段（API 不接受空的文字块）
        notes = [text for text in (note(self.messages) for note in self.notes) if text]
        parts = [self._context_note(), *notes, *verdict.context] + ([user_text] if user_text.strip() else [])
        # 附件和文字是同一条消息：出错撤回时一起撤回，不会只剩下半条
        self.backend.add_user(parts, attachments)
        return True

    def retry_turn(self, start: int) -> None:
        """重新生成最后一轮的回答（网页上的"重新生成"、终端里的 /retry，见 rewind.py）。

        start 是这一轮开始时记下的位置（backend.mark()），这一轮的用户消息就在那里（调用的人先用 backend.turn_at 查过）。
        历史退回到这条用户消息之后：用户消息原样留着，连同当时的时间说明和附件，所以这一次请求的开头和上一次
        一字不差，缓存照样命中；模型这一轮写的、做的（思考、回答、工具调用和它们的结果）全部去掉，再照常跑一遍。

        只能是最后一轮。去掉的只是历史的末尾，前面的一个字都不动：要是改了中间的某一轮，它后面的缓存全部作废，
        后面每一块思考的签名也都对不上了，API 会拒绝请求（原因见 providers/claude.py 开头的规矩）。
        """
        self.trace.turn_began(again=True)  # 记录点：一轮开始（重新生成的）
        self.turns += 1
        self.backend.rollback(start + 1)
        self.outcome = ""
        self._finish_turn(start)

    def _finish_turn(self, start: int, submit: Callable[[], bool] | None = None) -> None:
        """跑循环，直到这一轮结束。用户消息已经在 start 这个位置上了；还没放进去的，先由 submit 放（它返回 False 就不跑）。"""
        ended = "error"
        self._primary = self.backend.model
        try:
            if submit is None or submit():
                self._loop(start)
            ended = self.outcome
        except BaseException as exc:
            if isinstance(exc, KeyboardInterrupt):
                ended = "stopped"
            # 出错或被打断时，如果模型还一步都没走完，就撤回这条用户消息，
            # 这样用户重新输入时对话里不会出现两遍。已经走完的步骤则保留在历史里。
            if self.backend.mark() == start + 1:
                self.backend.rollback(start)
            raise
        finally:
            # 这一轮中途改用了备用模型的话，换回你选的那个：下一轮先试它（它可能已经好了）。
            # 只改请求里的 model（自定义接口还有这个模型自己的能力），历史、系统提示词、工具一样都没动过（见 fallback.py）
            if self.backend.model != self._primary:
                fallback.use(self.backend, self._primary)
            self._end_trace(ended)

    def _end_trace(self, ended: str) -> None:
        """一轮结束：把这一轮的运行记录整理好交出去。不管这一轮是怎么结束的（答完了、停下了、出错了）都交。"""
        self.hooks.stopped(self, ended)  # 钩子：Stop（只在答完了的那几种结束时跑，只是通知；它的那一段也记在这一轮里）
        record = self.trace.turn_ended(ended)  # 记录点：一轮结束
        if self.on_trace is None:
            return
        try:
            self.on_trace(record)
        except Exception:
            # 运行记录只是给人看的：交不出去也不能让这一轮（和它可能正在抛出的错误）跟着变。记进日志，别的照旧
            logger.exception("运行记录没能交给界面")

    def _loop(self, start: int) -> None:
        tried = {self.backend.model}  # 这一轮里试过、用不了的模型（换备用模型时不再选它们）
        for _ in range(self.config.max_steps):
            step = self._request(tried)  # 调用一次模型；回复已经由 backend 追加进历史
            cost = self._record(step.usage)
            self.trace.request_ended(step, cost)  # 记录点：模型答完这一次

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

    def _request(self, tried: set[str]) -> Step:
        """发出一次请求。这个模型暂时用不了（Unavailable：过载、服务器出错、限流）而你选了备用模型的话，
        改用同一家的下一个再试，这一轮接下来都用它（见 fallback.py）。没有能换的，错误照常抛出去。

        失败的那次请求什么都没留在历史里（两家的适配层都是收完整个回复才追加），所以换个模型重发的是一字不差的同一段历史、
        同一份系统提示词和工具说明书，只有请求里的 model 不一样。已经显示出来的半截文字留在界面上，和 JSON 格式出错时的重试一样。
        """
        while True:
            standing_in = self._primary if self.backend.model != self._primary else ""  # 用的是备用模型的话，它替的是哪个
            self.trace.request_began(self.backend.provider, self.backend.model, fallback=standing_in)  # 记录点：发出一次请求
            try:
                # 界面外面包一层 watch：第一段输出到的时候记下"首字时间"；
                # 钩子那边也包一层，记下这一次写的字（一轮答完时交给 Stop 钩子）
                return self.backend.step(self.hooks.watch(self.trace.watch(self.ui)))
            except Unavailable as exc:
                replacement = self._backup(tried)
                if replacement is None:
                    raise
                self.trace.request_failed(str(exc))  # 记录点：这次请求失败了，改用备用模型再试
                failed = self.backend.model
                fallback.use(self.backend, replacement)
                tried.add(replacement)
                self.ui.info(fallback.notice(failed, replacement))

    def _backup(self, tried: set[str]) -> str | None:
        """下一个可以改用的备用模型：你排的顺序里第一个这一轮还没试过、换过去工具说明书也照样认的。没有就是 None。"""
        if self.fallbacks is None:
            return None
        for model in self.fallbacks(self.backend.provider, self._primary):
            if model not in tried and fallback.usable(self.backend, model):
                return model
        return None

    def _record(self, usage: Usage) -> float | None:
        """记下一次调用的用量。返回账本算出来的钱（没有接账本、没有价格时是 None），运行记录照着写。"""
        now = self.clock()  # 只看一次钟：本次运行的估算和账本按同一个时刻算价格
        self.usage.add(usage)
        # 按模型分开记。价格分时段的模型（DeepSeek），闲时的用量另记一份，算钱时按闲时价（见 pricing.usage_key）
        self.usage_by_model.setdefault(usage_key(self.backend.model, now, self.backend.provider), Usage()).add(usage)
        cost = None
        if self.on_call is not None:  # 先记账再通知界面：界面刷新时，"今天""本月"已经包括这一次了
            cost = self.on_call(self.backend.provider, self.backend.model, usage, now)
        if self.on_usage is not None:
            self.on_usage()
        return cost

    def _context_note(self) -> str:
        now = self.clock()
        return f"<context>当前时间：{now:%Y-%m-%d %H:%M} 星期{WEEKDAYS[now.weekday()]}</context>"

    # ---- 执行工具 ---------------------------------------------------------

    def _run_tools(self, step: Step) -> None:
        """执行模型要求的工具，并把所有结果按调用的顺序追加进历史。

        工具包的调用（Claude 的浏览器、电脑工具包）常常是一串连着的动作："点搜索框、输入、按回车"，后一个依赖前一个。
        所以按顺序做，其中一个失败了（包括你拒绝了），这一轮后面的调用都不做，结果写 HALTED 那句话（文档的规定）。
        操作电脑的替身工具（GPT、Gemini 用的 computer_*）也一样：你拒绝了"点这个输入框"，后面的"打字"就不能照样打下去，
        字会进到不知道什么地方。

        连着的几个只读、又不用问你的调用（读几个文件、列待办……）一起跑（_batch_at 挑出来，_run_batch 跑，规矩见 parallel.py）；
        别的一个一个来。不管怎么跑，界面看到的、交给模型的都按调用的顺序。
        """
        calls = step.tool_calls
        results: list[ToolResult | None] = [None] * len(calls)  # 按调用的顺序放（一批里的也是），交给模型时就是这个顺序
        halted = False
        try:
            index = 0
            while index < len(calls):
                together = self.config.max_parallel_tools > 1 and not (step.truncated or halted)
                jobs = self._batch_at(calls, index) if together else []
                if len(jobs) > 1:
                    self._run_batch(jobs, results)
                    index += len(jobs)
                    continue
                call = calls[index]
                tool = self.tools.get(call.name, call.toolset)
                self.ui.tool_call(call.shown, call.input)
                self.trace.tool_began(call, tool)  # 记录点：开始一次工具调用
                ran = not (step.truncated or halted)
                took = None  # 工具真的执行了的话，执行了多久（秒）
                if step.truncated:
                    # 回复在生成工具参数的中途被截断，参数不完整，不能执行
                    output = ToolOutput("这次调用的参数在生成中途被截断，没有执行。请重新调用。", is_error=True)
                elif halted:
                    output = ToolOutput(HALT_TEXTS.get(call.toolset, HALTED), is_error=True)
                else:
                    output, took = self._run_one(call)
                    halted = output.is_error and (bool(call.toolset) or self._operates_computer(call))
                self._deliver(index, call, tool, output, took, ran, results)
                index += 1
        finally:
            # 每个工具调用都必须有对应的结果，否则下一次请求会被 API 拒绝。
            # 所以即使中途被打断（Ctrl-C 或界面上的"停止"），也要给没做完的调用补上一条结果，
            # 再把结果追加进历史。写在 finally 里，保证无论怎样退出都会执行
            answered = [result if result is not None else ToolResult(call.id, INTERRUPTED, True) for call, result in zip(calls, results)]
            self.backend.add_tool_results(answered)

    def _deliver(
        self, index: int, call: ToolCall, tool: Tool | None, output: ToolOutput, took: float | None, ran: bool, results: list[ToolResult | None], end: float | None = None
    ) -> None:
        """一次调用有结果了：洗一遍、遮密钥、放进结果、PostToolUse 钩子、记录点、告诉界面。一个一个跑的和一批里的都走这里。
        end：一批里的那一个在线程里执行完的时刻（运行记录用，见 parallel.py）；一个一个跑的是 None（就是此刻）。"""
        text = clean_text(output.text)  # 工具的输出同样不可信（比如文件名里有编码不了的字符）
        # 遮住像密钥的东西（见 redact.py）：放进结果之前、交给钩子之前、下面的记录点之前。
        # 钩子、模型、历史、页面、运行记录看到的都是遮好的这一份
        text, state, redacted = self._redact(text, output.state)
        result = results[index] = ToolResult(call.id, text, output.is_error, output.images, state, output.state_only)
        if took is not None:
            # 钩子：PostToolUse（只在真的执行了的调用上）。它补的话接在这条结果后面，这条结果还没进历史。
            # 结果先放进去再接：钩子跑到一半被停下，这次调用照样交回它真正的结果，不会被当成"没执行完"
            result.text = text = self.hooks.after_tool(self, call, tool, text, output.is_error, took)
        self.trace.tool_ended(text, output.is_error, len(output.images), ran, redacted, end=end)  # 记录点：这次调用有结果了
        # HALTED 那句是写给模型的英文原文（文档规定的），给人看的换成界面的语言；钩子拦下的也另有给人看的一句
        shown = t("没有执行：这一轮里前面的一步失败了。") if text in (HALTED, *HALT_TEXTS.values()) else output.shown or text
        self.ui.tool_result(shown, output.is_error, output.preview)

    # ---- 一起跑的只读调用（规矩见 parallel.py）----------------------------------

    def _batch_at(self, calls: list[ToolCall], start: int) -> list[Job]:
        """从第 start 个调用起，连着的、能一起跑的那几个：工具说这一次只是读（Tool.parallel）、根本不用问你
        （和一个一个跑时同一段判断：_gate 里的 Tool.check 和 permissions.review）、没有 PreToolUse 钩子要看它。
        碰到第一个不满足的就停：后面的要等它做完再看（它可能改了什么，后面的问不问也许就不一样了）。"""
        jobs: list[Job] = []
        for index in range(start, len(calls)):
            call = calls[index]
            tool = self.tools.get(call.name, call.toolset)
            # 先看工具说不说"能"：不说的（命令、浏览器、电脑、MCP……）不往下查，也就不会提前去看浏览器、屏幕上的情况（FactsTool.look）
            if tool is None or tool.parallel is None or call.error or not isinstance(call.input, dict) or not tool.parallel(call.input):
                break
            gate = self._gate(call)
            if isinstance(gate, ToolOutput) or gate[1] is not None or self.hooks.looks_before(call, gate[0]):
                break  # 不用执行就有结果的（参数不对、工具不做）、要问你的、你的钩子要看的：单独跑
            jobs.append(Job(index, call, gate[0]))
        return jobs

    def _run_batch(self, jobs: list[Job], results: list[ToolResult | None]) -> None:
        """一批一起跑。线程里只执行工具（_execute_job）；画卡片、记录点、钩子、遮密钥、放进结果都在这里，按调用的顺序一个一个来：
        第 k 张卡片画出来以后等第 k 个做完，交出它的结果，再画下一张（见 parallel.py 的"给人看的仍然一个一个来"）。"""
        batch = self.trace.batch_began()  # 记录点：下面这几次调用是同一批、同时跑的
        with Batch(jobs, self._execute_job, self.config.max_parallel_tools) as running:
            for job in jobs:
                self.ui.tool_call(job.call.shown, job.call.input)  # 你点了"停止"的话，这里就停下（还没开始的不再开始）
                running.start()  # 第一张卡片画出来以后，整批交给线程；后面几次什么都不做
                try:
                    running.wait(job)
                finally:
                    # 记录点：开始一次工具调用。在它做完以后才记，开始的时刻用线程里读到的那一个，所以几段横条叠在一起。
                    # 等它的时候被打断了（Ctrl-C）也记上：一轮结束时这一段按此刻收尾、标成 stopped，和一个一个跑时一样
                    self.trace.tool_began(job.call, job.tool, start=job.started, batch=batch)
                assert job.output is not None  # wait 正常返回，就是线程把它填好了
                self._deliver(job.index, job.call, job.tool, job.output, job.took, True, results, end=job.finished)

    def _execute_job(self, job: Job) -> None:
        """在线程池的线程里执行一批里的一个调用（见 parallel.py）。只碰 job 和工具本身：不碰界面、不碰记录器（只读一下它的表）。"""
        job.started = self.trace.timer()
        job.output, job.took = self._execute(job.call, job.tool)
        job.finished = self.trace.timer()

    def _redact(self, text: str, state: dict | None) -> tuple[str, dict | None, int]:
        """遮住结果里像密钥的东西（见 redact.py）。返回（文字，浏览器的标签页情况，一共遮了几处）。
        有遮的，文字末尾附一句给模型看的说明；标签页情况里的网址、标题也一样遮（它也交给模型）。"""
        if self.redact is None:
            return text, state, 0
        done = self.redact(text)
        count = done.count
        if state is not None:
            state, more = redact_data(state, self.redact)
            count += more
        text = done.text + note(done) if done.count else text
        return text, state, count

    def _operates_computer(self, call: ToolCall) -> bool:
        """是不是操作电脑的替身工具（computer_*）。"""
        tool = self.tools.get(call.name, call.toolset)
        return tool is not None and tool.stands_in_for == COMPUTER

    def _gate(self, call: ToolCall) -> ToolOutput | tuple[Tool, str | None]:
        """执行之前的第一关，不碰界面：参数能不能用、有没有这个工具、工具自己肯不肯做（Tool.check）、要不要问你（permissions.review）。
        不用执行就有结果的，交回那个结果；不然交回（工具，要问你的话；不用问是 None）。
        一个一个跑（_run_one）和挑出能一起跑的（_batch_at）都用它：问不问你只在这里、按同一套规矩判断。"""
        if call.error:
            return ToolOutput(call.error, is_error=True)  # 模型给的参数没法解析，把问题告诉它
        tool = self.tools.get(call.name, call.toolset)
        if tool is None or tool.run is None:
            return ToolOutput(f"没有可以在本地执行的工具 {call.shown}。", is_error=True)
        refusal = tool.check(call.input) if tool.check is not None and isinstance(call.input, dict) else None
        if refusal:
            return ToolOutput(refusal, is_error=True)  # 这个工具在这里不做这件事（见 Tool.check）：不用问用户，直接告诉模型
        # 权限这一关。把工具本身也给它：MCP 的工具要靠它认出是哪个服务器的
        return tool, self.permissions.review(call.name, call.input, tool)

    def _run_one(self, call: ToolCall) -> tuple[ToolOutput, float | None]:
        """执行一次工具调用，返回结果（给模型看的文字、是否出错、结果里的图片……）和工具执行了多久（秒）。
        没有真的执行（参数不对、你拒绝了、钩子拦下了……）的，时间是 None。"""
        gate = self._gate(call)
        if isinstance(gate, ToolOutput):
            return gate, None
        tool, question = gate
        asked = False
        if question is not None:  # 要问你：看看现在这一档能不能不问
            why = self.permissions.allowance(call.name, call.input, tool)
            if why is not None:
                self.ui.auto_allowed(why)  # 没有问用户就放行了：告诉界面一声（为什么也说上），记录里要看得出来
                self.trace.approval_decided("auto", why)  # 记录点：没问你就放行了
            else:
                refused = self._ask(question)
                if refused is not None:
                    return refused, None
                asked = True

        # 钩子：PreToolUse（过了确认这一关以后）。只能拦下，或者要你再确认一次（刚问过你、你允许了的不再问）
        verdict = self.hooks.before_tool(self, call, tool)
        if verdict.blocked:
            return ToolOutput(verdict.blocked, is_error=True, shown=verdict.shown), None
        if verdict.ask and not asked:
            refused = self._ask(verdict.ask)
            if refused is not None:
                return refused, None
        return self._execute(call, tool)

    def _execute(self, call: ToolCall, tool: Tool) -> tuple[ToolOutput, float]:
        """真的执行这个工具（关卡都过了），返回结果和执行了多久（秒）。一批一起跑的时候，它在线程池的线程里跑（见 parallel.py）。"""
        assert tool.run is not None  # _gate 查过：没有 run 的工具（服务器上的）根本到不了这里
        # 工具出错不会让程序崩溃：错误信息作为结果返回给模型，由它决定怎么办
        began = time.monotonic()  # 工具执行了多久（交给 PostToolUse 钩子的 duration_ms）
        try:
            output = tool.run(call.input)
        except ToolError as exc:
            return ToolOutput(str(exc), is_error=True), time.monotonic() - began
        except Exception as exc:  # noqa: BLE001 —— 故意捕获所有异常：工具里的 bug 不应该让整个程序退出
            return ToolOutput(f"工具内部出错：{type(exc).__name__}: {exc}", is_error=True), time.monotonic() - began
        took = time.monotonic() - began
        if isinstance(output, ToolOutput):
            return (output if output.text or output.state_only else replace(output, text="（没有输出）")), took
        return ToolOutput(output or "（没有输出）"), took

    def _ask(self, question: str) -> ToolOutput | None:
        """问你要不要做这一步。你拒绝了，返回交给模型的结果；允许了返回 None。"""
        self.trace.approval_asked()  # 记录点：开始等你答复
        allowed, feedback = self.ui.confirm(question)
        self.trace.approval_decided("allowed" if allowed else "refused")  # 记录点：你答复了
        if allowed:
            return None
        return ToolOutput("用户拒绝了这个操作。" + (f"用户说：{feedback}" if feedback else ""), is_error=True)
