"""GPT、Gemini、DeepSeek：OpenAI 的 Chat Completions 格式的适配层。

这三家（以及很多别的厂商）都支持 OpenAI 定下的这套格式，只是服务器地址不同，
所以一份代码可以服务三家。和 Claude 的原生接口相比，主要的不同：

  - 系统提示词是历史里的第一条消息（role 为 system），不是单独的参数。
  - 工具调用不是"内容块"，而是助手消息上的 tool_calls 字段；参数是一段 JSON 文本，要自己解析。
  - 每个工具结果是单独的一条消息（role 为 tool），而不是合在一条消息里。
  - 没有服务器端的联网搜索，没有自动压缩历史，缓存是各家自动做的、不用我们管。
"""

from __future__ import annotations

import json
from typing import Any

import openai

from ..config import Config
from ..tools import ToolRegistry
from .base import ProviderError, Step, StreamUI, ToolCall, ToolResult, Usage


class OpenAICompatBackend:
    def __init__(
        self, client: Any, provider: str, label: str, model: str, config: Config, tools: ToolRegistry, system_prompt: str
    ):
        self.client = client
        self.provider = provider
        self.label = label  # 给用户看的名字，如 "DeepSeek"
        self.model = model
        self.config = config
        self._local_tools = [tool for tool in tools if tool.portable is not None and tool.run is not None]
        # 各家通用的"函数"格式的工具说明书
        self.tool_specs = [{"type": "function", "function": tool.portable} for tool in self._local_tools]
        self._system = {"role": "system", "content": system_prompt}
        self._messages: list[dict] = [self._system]

    # ---- 对话历史 ---------------------------------------------------------

    @property
    def messages(self) -> list[dict]:
        return self._messages

    def reset(self) -> None:
        self._messages = [self._system]

    def mark(self) -> int:
        return len(self._messages)

    def rollback(self, mark: int) -> None:
        del self._messages[max(mark, 1) :]  # 第一条是系统提示词，永远保留

    def add_user(self, parts: list[str]) -> None:
        self._messages.append({"role": "user", "content": "\n\n".join(parts)})

    def add_tool_results(self, results: list[ToolResult]) -> None:
        for result in results:
            # 这套格式里没有"出错"标记，所以把它写在文字里，让模型知道
            text = f"[出错] {result.text}" if result.is_error else result.text
            self._messages.append({"role": "tool", "tool_call_id": result.id, "content": text})

    def tool_names(self) -> list[tuple[str, bool]]:
        return [(tool.name, True) for tool in self._local_tools]

    # ---- 调用模型 ---------------------------------------------------------

    def step(self, ui: StreamUI) -> Step:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages,
            "stream": True,
            "stream_options": {"include_usage": True},  # 让最后一块数据带上 token 用量
        }
        if self.tool_specs:
            request["tools"] = self.tool_specs

        text, reasoning, finish, raw_usage = "", "", None, None
        calls: dict[int, dict] = {}  # 工具调用是一小段一小段到达的，按序号拼起来
        try:
            with self.client.chat.completions.create(**request) as stream:
                for chunk in stream:
                    if chunk.usage is not None:
                        raw_usage = chunk.usage
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta
                    # 有的模型（如 DeepSeek 的思考模式）会把思考过程放在这个额外的字段里
                    thought = getattr(delta, "reasoning_content", None)
                    if thought:
                        reasoning += thought
                        ui.thinking(thought)
                    if delta.content:
                        text += delta.content
                        ui.text(delta.content)
                    for piece in delta.tool_calls or []:
                        _merge_tool_call(calls, piece)
                    finish = choice.finish_reason or finish
        except openai.APIError as exc:
            raise ProviderError(_explain(exc, self.label, self.model)) from exc
        finally:
            ui.end_stream()

        step = Step(status="done", usage=_usage(raw_usage), truncated=finish == "length")
        if finish == "content_filter":
            step.status = "refused"
            step.note = "content_filter"
            return step

        # 把模型的回复追加进历史，收到什么就存什么。两条各家特有的规矩都靠"原样存回"来满足：
        #   DeepSeek：思考内容（reasoning_content）必须在后续请求里发回去，否则报 400；
        #   Gemini：工具调用上附带的签名（extra_content）必须原样发回去，否则报 400。
        ordered = [calls[index] for index in sorted(calls)]
        # 只调用工具、没有说话时，content 按这套格式的规定填 null
        message: dict[str, Any] = {"role": "assistant", "content": text if text or not ordered else None}
        if reasoning:
            message["reasoning_content"] = reasoning
        for number, call in enumerate(ordered):
            call["id"] = call["id"] or f"call_{len(self._messages)}_{number}"  # 个别厂商不给 id，自己补一个
        if ordered:
            message["tool_calls"] = ordered
        self._messages.append(message)

        for call in ordered:
            raw = call["function"]["arguments"] or "{}"
            try:
                step.tool_calls.append(ToolCall(call["id"], call["function"]["name"], json.loads(raw)))
            except ValueError:
                problem = json.dumps({"error": "参数不是合法的 JSON", "INVALID_JSON": raw}, ensure_ascii=False)
                step.tool_calls.append(ToolCall(call["id"], call["function"]["name"], None, error=problem))
        if step.tool_calls:
            step.status = "tools"
        return step


def _merge_tool_call(calls: dict[int, dict], piece: Any) -> None:
    """把流里到达的一小段工具调用，合并到按序号保存的完整调用里。"""
    call = calls.setdefault(piece.index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
    if piece.id:
        call["id"] = piece.id
    if piece.function is not None:
        call["function"]["name"] += piece.function.name or ""
        call["function"]["arguments"] += piece.function.arguments or ""
    # 标准字段之外的东西（比如 Gemini 的签名）也要留着，之后原样发回去
    for key, value in (piece.model_extra or {}).items():
        if value is not None:
            call[key] = value


def _usage(raw: Any) -> Usage:
    if raw is None:  # 有的服务不返回用量
        return Usage(requests=1)
    prompt = raw.prompt_tokens or 0
    completion = raw.completion_tokens or 0
    details = getattr(raw, "prompt_tokens_details", None)
    # 命中缓存的输入：OpenAI 的格式放在 prompt_tokens_details 里，DeepSeek 另有一个自己的字段
    cached = getattr(details, "cached_tokens", None) or getattr(raw, "prompt_cache_hit_tokens", None) or 0
    cached = min(cached, prompt)
    return Usage(
        requests=1,
        input_tokens=prompt - cached,
        cache_read_tokens=cached,
        output_tokens=completion,
        context_tokens=prompt + completion,
    )


def _explain(exc: openai.APIError, label: str, model: str) -> str:
    """把 API 的错误翻译成看得懂的提示。"""
    if isinstance(exc, openai.AuthenticationError):
        return f"{label} 的 API key 无效，请在设置里重新填写。"
    if isinstance(exc, openai.PermissionDeniedError):
        return f"这个 API key 没有权限执行该请求：{exc.message}"
    if isinstance(exc, openai.NotFoundError):
        return f"{label} 找不到模型 {model}，或者这个模型不支持当前的接口。请换一个模型。"
    if isinstance(exc, openai.RateLimitError):
        return f"{label} 提示请求太频繁，或者账号余额、额度不足。"
    if isinstance(exc, openai.BadRequestError):
        return f"请求被 {label} 拒绝：{exc.message}"
    if isinstance(exc, openai.APIStatusError):
        return f"{label} 的 API 出错（状态码 {exc.status_code}）：{exc.message}"
    if isinstance(exc, openai.APIConnectionError):
        return f"连接不上 {label} 的服务器，请检查网络。"
    return f"调用 {label} 失败：{exc}"
