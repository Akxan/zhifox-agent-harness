"""Agent 循环 —— 整个 harness 的核心。

模型只会做一件事：读入一段对话，写出下一段回复。它不记得上一次请求，也执行不了任何操作。
让它变成"助手"的是这个循环：

    用户说了一句话
      └─► 把【系统提示词 + 工具说明书 + 到目前为止的全部对话】发给模型
            ├─ 模型回复文字               → 显示给用户，这一轮结束
            └─ 模型说"我想调用工具 X"     → 检查权限 → 执行 → 把结果追加进对话 → 再发给模型
                                                                              （回到上一步，循环）

"下一步做什么"每次都由模型决定，循环只负责照办并把结果告诉它。这就是 agent。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from .config import Config
from .permissions import Permissions
from .tools import ToolError, ToolRegistry

WEEKDAYS = "一二三四五六日"


class UI(Protocol):
    """循环需要界面做的事。终端界面的实现在 ui.py。"""

    def stream_event(self, event: Any) -> None: ...
    def end_stream(self) -> None: ...
    def tool_call(self, name: str, tool_input: object) -> None: ...
    def tool_result(self, text: str, is_error: bool) -> None: ...
    def confirm(self, description: str) -> tuple[bool, str]: ...
    def info(self, text: str) -> None: ...


@dataclass
class Usage:
    """累计用量。token 是计费单位，一个汉字大约 1 到 2 个 token。"""

    requests: int = 0
    input_tokens: int = 0  # 没有命中缓存、按原价计费的输入
    cache_read_tokens: int = 0  # 命中缓存的输入，价格低得多
    cache_write_tokens: int = 0  # 这次写入缓存的输入，比原价略贵
    output_tokens: int = 0

    def add(self, usage: Any) -> None:
        self.requests += 1
        self.input_tokens += usage.input_tokens or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.output_tokens += usage.output_tokens or 0


class Agent:
    def __init__(
        self,
        client: Any,
        config: Config,
        tools: ToolRegistry,
        permissions: Permissions,
        ui: UI,
        system_prompt: str,
        clock: Callable[[], datetime] = datetime.now,
    ):
        self.client = client
        self.config = config
        self.tools = tools
        self.permissions = permissions
        self.ui = ui
        self.clock = clock
        self.usage = Usage()

        # 模型没有记忆，所以每次请求都要把这三样完整地发过去：
        #   system    系统提示词
        #   tools     工具说明书
        #   messages  对话历史
        #
        # 重要的规矩：system 和 tools 在一次会话里保持不变，messages 只在末尾追加，
        # 从不修改或删除中间的内容。原因有两个：
        #   1. 缓存。API 会缓存请求的开头部分，下次请求的开头只要一字不差就按一折左右计费；
        #      开头改动一个字，后面的缓存全部作废。
        #   2. 模型的思考内容（thinking 块）带有签名，签名和它之前的全部对话绑定。
        #      改了前面的内容再把思考发回去，API 会拒绝这个请求。
        self.system = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
        self.tool_specs = tools.specs()
        self.messages: list[dict] = []

    def reset(self) -> None:
        """开始一段新对话。记忆和待办存在磁盘上，不受影响。"""
        self.messages = []

    # ---- 一轮对话 ---------------------------------------------------------

    def run_turn(self, user_text: str) -> None:
        """处理用户的一条消息，直到模型给出最终答复。"""
        turn_start = len(self.messages)
        self.messages.append(
            {
                "role": "user",
                "content": [
                    # 当前时间每次都不同，所以附在消息里，而不是写进系统提示词（那样会破坏缓存）
                    {"type": "text", "text": self._context_note()},
                    {"type": "text", "text": user_text},
                ],
            }
        )
        try:
            self._loop(turn_start)
        except BaseException:
            # 出错或被 Ctrl-C 打断时，如果模型还一步都没走完，就撤回这条用户消息，
            # 这样用户重新输入时对话里不会出现两遍。已经走完的步骤则保留在历史里。
            if len(self.messages) == turn_start + 1:
                self.messages.pop()
            raise

    def _loop(self, turn_start: int) -> None:
        for _ in range(self.config.max_steps):
            response = self._call_model()

            if response.stop_reason == "refusal":
                # 安全系统拒绝了请求。留在历史里的话后面多半还会被拒，所以整轮撤销。
                del self.messages[turn_start:]
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None)
                reason = f"（类别：{category}）" if category else ""
                self.ui.info(f"模型拒绝了这个请求{reason}，这一轮对话已撤销。可以换个说法再试。")
                return

            # 把模型的回复原样追加进历史。不能只存文字：思考块、工具调用块下次都要原样发回去。
            content = blocks_to_keep(response.content)
            if content:
                self.messages.append({"role": "assistant", "content": content})

            if response.stop_reason == "pause_turn":
                continue  # 服务器端的工具（联网搜索）还没做完，原样再发一次请求让它接着做

            tool_uses = [block for block in content if block.type == "tool_use"]
            if not tool_uses:
                if response.stop_reason == "max_tokens":
                    self.ui.info("回复达到了长度上限，被截断了。")
                return  # 模型没有要调用的工具了：这一轮结束

            truncated = response.stop_reason == "max_tokens"
            results, interrupted = self._run_tools(tool_uses, truncated)
            # 每个工具调用都必须有对应的结果，并且所有结果放在同一条消息里发回去
            self.messages.append({"role": "user", "content": results})
            if interrupted:
                raise KeyboardInterrupt

        self.ui.info(f"这一轮已经调用了 {self.config.max_steps} 次模型，先停在这里。输入“继续”可以接着做。")

    # ---- 调用模型 ---------------------------------------------------------

    def _call_model(self) -> Any:
        request: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "system": self.system,
            "tools": self.tool_specs,
            "messages": self.messages,
            # 让模型自己决定何时思考、想多深；summarized 表示把思考的摘要也返回给我们显示
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": self.config.effort},
            # 自动缓存到对话的最后一块。循环里每一步都重发整个历史，有缓存便宜很多
            "cache_control": {"type": "ephemeral"},
        }
        betas = []
        if self.config.fallbacks:
            # 请求被安全分类器误拦时，由 API 自动换一个模型重试
            betas.append("server-side-fallback-2026-07-01")
            request["fallbacks"] = "default"
        if self.config.compaction:
            # 对话太长时，由 API 把较早的部分压缩成摘要
            betas.append("compact-2026-01-12")
            request["context_management"] = {"edits": [{"type": "compact_20260112"}]}
        if betas:
            request["betas"] = betas

        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                # 流式接收：模型一边生成，我们一边显示
                with self.client.beta.messages.stream(**request) as stream:
                    for event in stream:
                        self.ui.stream_event(event)
                    response = stream.get_final_message()
                break
            except ValueError:
                # 极少数情况下，模型生成的工具参数不是合法的 JSON，SDK 解析不了。重新请求一次即可。
                if attempt == attempts:
                    raise
                self.ui.info("模型返回的工具参数格式有误，正在重试……")
            finally:
                self.ui.end_stream()

        self.usage.add(response.usage)
        return response

    def _context_note(self) -> str:
        now = self.clock()
        return f"<context>当前时间：{now:%Y-%m-%d %H:%M} 星期{WEEKDAYS[now.weekday()]}</context>"

    # ---- 执行工具 ---------------------------------------------------------

    def _run_tools(self, tool_uses: list, truncated: bool) -> tuple[list[dict], bool]:
        """依次执行模型要求的工具，返回（结果列表，是否被用户中断）。"""
        results = []
        interrupted = False
        for block in tool_uses:
            self.ui.tool_call(block.name, block.input)
            if interrupted:
                text, is_error = "用户中断了操作，这个工具没有执行。", True
            elif truncated:
                # 回复在生成工具参数的中途被截断，参数不完整，不能执行
                text, is_error = "这次调用的参数在生成中途被截断，没有执行。请重新调用。", True
            else:
                try:
                    text, is_error = self._run_one(block)
                except KeyboardInterrupt:
                    interrupted = True
                    text, is_error = "用户中断了这个操作。", True
            self.ui.tool_result(text, is_error)
            result = {"type": "tool_result", "tool_use_id": block.id, "content": text}
            if is_error:
                result["is_error"] = True
            results.append(result)
        return results, interrupted

    def _run_one(self, block: Any) -> tuple[str, bool]:
        """执行一次工具调用，返回（给模型看的结果，是否出错）。"""
        tool = self.tools.get(block.name)
        if tool is None or tool.run is None:
            return f"没有可以在本地执行的工具 {block.name}。", True

        # 先过权限这一关
        question = self.permissions.review(block.name, block.input)
        if question is not None:
            allowed, feedback = self.ui.confirm(question)
            if not allowed:
                return "用户拒绝了这个操作。" + (f"用户说：{feedback}" if feedback else ""), True

        # 工具出错不会让程序崩溃：错误信息作为结果返回给模型，由它决定怎么办
        try:
            return tool.run(block.input) or "（没有输出）", False
        except ToolError as exc:
            return str(exc), True
        except Exception as exc:  # noqa: BLE001 —— 故意捕获所有异常：工具里的 bug 不应该让整个程序退出
            return f"工具内部出错：{type(exc).__name__}: {exc}", True


def blocks_to_keep(content: list) -> list:
    """模型的回复里，哪些内容块要留在历史里。绝大多数情况是全部。

    唯一的例外：回复写到一半被安全分类器拦下，由后备模型接着写完。这时内容里会有一个
    fallback 块作为分界线。按 API 的规定，分界线之前、被拦下的那个模型留下的思考和
    工具调用是半成品，不能再发回去（也不能执行）；文字和分界线本身要保留。
    """
    boundary = max((i for i, block in enumerate(content) if block.type == "fallback"), default=None)
    if boundary is None:
        return list(content)
    finished = {getattr(block, "tool_use_id", None) for block in content[:boundary]}
    kept = []
    for index, block in enumerate(content):
        if index < boundary:
            if block.type in ("thinking", "redacted_thinking", "tool_use"):
                continue
            if block.type == "server_tool_use" and block.id not in finished:
                continue  # 没有拿到结果的服务器工具调用
        kept.append(block)
    return kept
