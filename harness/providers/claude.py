"""Claude：Anthropic 原生接口的适配层。

这里是所有"只有 Claude 才这样"的细节：请求里的参数、流式事件的格式、缓存、思考内容的签名、
服务器端的联网搜索、被拒绝后自动换模型、对话太长时的自动压缩。
"""

from __future__ import annotations

from typing import Any

import anthropic

from ..config import (
    ENABLE_COMPACTION,
    ENABLE_FALLBACKS,
    FALLBACK_CAPABLE_MODELS,
    Config,
)
from ..display import describe_call
from ..tools import ToolRegistry
from .base import ProviderError, Step, StreamUI, ToolCall, ToolResult, Usage


class AnthropicBackend:
    provider = "anthropic"

    def __init__(self, client: Any, model: str, config: Config, tools: ToolRegistry, system_prompt: str):
        self.client = client
        self.model = model
        self.config = config
        self._tools = tools

        # 模型没有记忆，所以每次请求都要把这三样完整地发过去：
        #   system    系统提示词
        #   tools     工具说明书
        #   messages  对话历史
        #
        # 重要的规矩：system 和 tools 在一段对话里保持不变，messages 只在末尾追加，
        # 从不修改或删除中间的内容。原因有两个：
        #   1. 缓存。API 会缓存请求的开头部分，下次请求的开头只要一字不差就按很低的价格计费；
        #      开头改动一个字，后面的缓存全部作废。
        #   2. 模型的思考内容（thinking 块）带有签名，签名和它之前的全部对话绑定。
        #      改了前面的内容再把思考发回去，API 会拒绝这个请求。
        self.system = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
        self.tool_specs = tools.specs()
        self._messages: list[dict] = []

    # ---- 对话历史 ---------------------------------------------------------

    @property
    def messages(self) -> list[dict]:
        return self._messages

    def reset(self) -> None:
        self._messages = []

    def mark(self) -> int:
        return len(self._messages)

    def rollback(self, mark: int) -> None:
        del self._messages[mark:]

    def add_user(self, parts: list[str]) -> None:
        self._messages.append({"role": "user", "content": [{"type": "text", "text": part} for part in parts]})

    def add_tool_results(self, results: list[ToolResult]) -> None:
        # 所有结果必须放在同一条消息里，并且这条消息里只有结果。
        # （API 靠"最后一条消息只有工具结果"来判断这是同一轮对话的延续，联网搜索的次数上限按轮累计。）
        content = []
        for result in results:
            block = {"type": "tool_result", "tool_use_id": result.id, "content": result.text}
            if result.is_error:
                block["is_error"] = True
            content.append(block)
        self._messages.append({"role": "user", "content": content})

    def tool_names(self) -> list[tuple[str, bool]]:
        return [(tool.name, tool.run is not None) for tool in self._tools]

    # ---- 调用模型 ---------------------------------------------------------

    def step(self, ui: StreamUI) -> Step:
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.config.max_tokens,
            "system": self.system,
            "tools": self.tool_specs,
            "messages": self._messages,
            # 让模型自己决定何时思考、想多深；summarized 表示把思考的摘要也返回给我们显示
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": self.config.effort},
            # 自动缓存到对话的最后一块。循环里每一步都重发整个历史，有缓存便宜很多
            "cache_control": {"type": "ephemeral"},
        }
        betas = []
        if ENABLE_FALLBACKS and self.model in FALLBACK_CAPABLE_MODELS:
            # 请求被安全分类器误拦时，由 API 自动换一个模型重试
            betas.append("server-side-fallback-2026-07-01")
            request["fallbacks"] = "default"
        if ENABLE_COMPACTION:
            # 对话太长时，由 API 把较早的部分压缩成摘要
            betas.append("compact-2026-01-12")
            request["context_management"] = {"edits": [{"type": "compact_20260112"}]}
        if betas:
            request["betas"] = betas

        response = self._send(request, ui)
        step = Step(status="done", usage=_usage(response))

        if response.stop_reason == "refusal":
            # 不把被拒绝的回复放进历史：留着的话，后面多半还会被拒
            step.status = "refused"
            step.note = getattr(getattr(response, "stop_details", None), "category", None) or ""
            return step

        # 把模型的回复原样追加进历史。不能只存文字：思考块、工具调用块下次都要原样发回去。
        content = blocks_to_keep(response.content)
        if content:
            self._messages.append({"role": "assistant", "content": content})

        step.truncated = response.stop_reason == "max_tokens"
        step.tool_calls = [ToolCall(block.id, block.name, block.input) for block in content if block.type == "tool_use"]
        if response.stop_reason == "pause_turn":
            step.status = "paused"
        elif step.tool_calls:
            step.status = "tools"
        return step

    def _send(self, request: dict, ui: StreamUI) -> Any:
        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                # 流式接收：模型一边生成，我们一边显示
                with self.client.beta.messages.stream(**request) as stream:
                    for event in stream:
                        self._show(event, ui)
                    return stream.get_final_message()
            except ValueError:
                # 极少数情况下，模型生成的工具参数不是合法的 JSON，SDK 解析不了。重新请求一次即可。
                if attempt == attempts:
                    raise
                ui.info("模型返回的工具参数格式有误，正在重试……")
            except anthropic.APIError as exc:
                raise ProviderError(_explain(exc, self.model)) from exc
            finally:
                ui.end_stream()
        raise AssertionError("unreachable")

    @staticmethod
    def _show(event: Any, ui: StreamUI) -> None:
        """把 SDK 的流式事件翻译成界面能显示的东西。"""
        if event.type == "thinking":  # 思考摘要的一小段
            ui.thinking(event.thinking)
        elif event.type == "text":  # 回答正文的一小段
            ui.text(event.text)
        elif event.type == "content_block_stop":  # 一个内容块结束了
            block = event.content_block
            if block.type in ("thinking", "text"):
                ui.end_stream()
            else:
                notice = block_notice(block)
                if notice is not None:
                    ui.notice(*notice)


def _usage(response: Any) -> Usage:
    raw = response.usage
    usage = Usage(
        requests=1,
        input_tokens=raw.input_tokens or 0,
        cache_read_tokens=getattr(raw, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(raw, "cache_creation_input_tokens", 0) or 0,
        output_tokens=raw.output_tokens or 0,
        searches=getattr(getattr(raw, "server_tool_use", None), "web_search_requests", 0) or 0,
    )
    usage.context_tokens = (
        usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens + usage.output_tokens
    )
    # 一次请求在服务器上可能分成好几步（比如连续搜索）。iterations 里是每一步的明细。
    for entry in getattr(raw, "iterations", None) or []:
        tokens = [
            getattr(entry, "input_tokens", 0) or 0,
            getattr(entry, "cache_read_input_tokens", 0) or 0,
            getattr(entry, "cache_creation_input_tokens", 0) or 0,
            getattr(entry, "output_tokens", 0) or 0,
        ]
        if entry.type == "compaction":
            # 压缩历史这一步自己也要花 token，而它不包含在上面的总数里，所以要加上
            usage.input_tokens += tokens[0]
            usage.cache_read_tokens += tokens[1]
            usage.cache_write_tokens += tokens[2]
            usage.output_tokens += tokens[3]
        elif entry.type == "message":
            usage.context_tokens = sum(tokens)  # 最后一步的大小就是对话现在占用的上下文
    return usage


def _explain(exc: anthropic.APIError, model: str) -> str:
    """把 API 的错误翻译成看得懂的提示。"""
    if isinstance(exc, anthropic.AuthenticationError):
        return "Claude 的 API key 无效，请在设置里重新填写。"
    if isinstance(exc, anthropic.PermissionDeniedError):
        return f"这个 API key 没有权限执行该请求：{exc.message}"
    if isinstance(exc, anthropic.NotFoundError):
        return f"找不到模型 {model}，请换一个模型。"
    if isinstance(exc, anthropic.RateLimitError):
        return "请求太频繁，或者超出了账号的用量限制。请稍后再试。"
    if isinstance(exc, anthropic.BadRequestError):
        return f"请求被 API 拒绝：{exc.message}"
    if isinstance(exc, anthropic.APIStatusError):
        return f"API 出错（状态码 {exc.status_code}）：{exc.message}"
    if isinstance(exc, anthropic.APIConnectionError):
        return "连接不上 Anthropic 的服务器，请检查网络。"
    return f"调用 Claude 失败：{exc}"


def block_notice(block: Any) -> tuple[str, str] | None:
    """回复里不是逐字输出的那些内容块（联网搜索、压缩历史、换模型），各用一句话说明。

    返回（级别，文字）。级别有四种："tool" 服务器工具开始执行，"result" 它的结果，
    "error" 它失败了，"info" 其他提示。不需要显示的块返回 None。
    """
    kind = block.type
    if kind == "server_tool_use":
        return "tool", describe_call(block.name, block.input)
    if kind == "web_search_tool_result":
        if isinstance(block.content, list):
            return "result", f"找到 {len(block.content)} 条结果"
        code = getattr(block.content, "error_code", "未知错误")
        if code == "max_uses_exceeded":
            return "info", "这条消息的搜索次数已经用完，助手会根据已有的信息回答"
        return "error", f"搜索失败：{code}"
    if kind == "web_fetch_tool_result":
        code = getattr(block.content, "error_code", None)
        if code == "max_uses_exceeded":
            return "info", "这条消息读取网页的次数已经用完"
        return ("error", f"读取网页失败：{code}") if code else None
    if kind == "compaction":
        return "info", "对话已经很长，较早的内容被压缩成了摘要"
    if kind == "fallback":
        return "info", f"{block.from_.model} 拒绝了这个请求，已改由 {block.to.model} 继续"
    return None


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
