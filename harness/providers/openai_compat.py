"""GPT、Gemini、DeepSeek：OpenAI 的 Chat Completions 格式的适配层。

这三家（以及很多别的厂商）都支持 OpenAI 定下的这套格式，只是服务器地址不同，
所以一份代码可以服务三家。和 Claude 的原生接口相比，主要的不同：

  - 系统提示词是历史里的第一条消息（role 为 system），不是单独的参数。
  - 工具调用不是"内容块"，而是助手消息上的 tool_calls 字段；参数是一段 JSON 文本，要自己解析。
  - 每个工具结果是单独的一条消息（role 为 tool），而不是合在一条消息里。
  - 没有服务器端的联网搜索；缓存是各家自动做的、不用我们管。
  - API 不会替我们压缩历史。对话太长时，由智狐自己请模型把较早的部分写成摘要（见 compaction.py）。
  - 用户消息带图片时，内容是一个列表，图片写成 data: 地址；这套格式里没有各家通用的 PDF 写法。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import openai

from .. import i18n
from ..attachments import Attachment, unsupported, wrap_text
from ..config import Config
from ..i18n import t
from ..safe import clean, clean_text, loads_strict
from ..tools import ToolRegistry
from . import compaction
from .base import (
    ProviderError,
    Step,
    StreamUI,
    ToolCall,
    ToolResult,
    Usage,
    brief,
    too_large,
)


class OpenAICompatBackend:
    def __init__(
        self,
        client: Any,
        provider: str,
        label: str,
        model: str,
        config: Config,
        tools: ToolRegistry,
        system_prompt: str,
        *,
        vision: bool = False,
        pdf: bool = False,
    ):
        self.client = client
        self.provider = provider
        self.label = label  # 给用户看的名字，如 "DeepSeek"
        self.model = model
        self.config = config
        self.vision, self.pdf = vision, pdf  # 这一家能不能收图片、PDF（见 providers/__init__.py 的 PROVIDERS）
        self.use_tools(tools)
        self._system = {"role": "system", "content": system_prompt}
        self._start_history([])

    def use_tools(self, tools: ToolRegistry) -> None:
        """换一套工具。和 Claude 一样，只在一段对话开始之前换（见 agent.py 的 on_start）。

        全靠截图的工具（Tool.vision，比如操作整台电脑）只给看得懂图片的这几家：看不了图的模型拿到了也只能闭着眼睛乱点。
        """
        self._local_tools = [tool for tool in tools if self.sends(tool)]
        # 各家通用的"函数"格式的工具说明书
        self.tool_specs = [{"type": "function", "function": tool.portable} for tool in self._local_tools]

    # ---- 对话历史 ---------------------------------------------------------

    @property
    def messages(self) -> list[dict]:
        return self._messages

    def _start_history(self, messages: list) -> None:
        """换上一份历史（新对话、打开旧会话），和压缩有关的几项记录也从头开始。"""
        self._messages: list[dict] = [self._system, *messages]
        # 压缩会把前面的一段换成两条摘要消息，历史因此变短。为了让压缩之前记下的位置（mark）在压缩之后
        # 仍然指向同一条消息，把压缩掉了多少条记在这里，mark 和 rollback 换算时用
        self._dropped = 0
        # 上一次请求时这段对话占了多大的上下文，决定下一次请求之前要不要压缩（见 compaction.is_due）。
        # 0 表示还不知道：新对话、刚打开的旧会话、刚压缩完，都要等下一次请求的用量回来
        self._context_tokens = 0
        # 这一轮里压缩已经失败过：同样的原因多半还会失败，这一轮不再试，等用户发下一条消息
        self._compaction_failed = False

    def reset(self) -> None:
        self._start_history([])

    def export(self) -> list:
        return self._messages[1:]  # 系统提示词不用存，每次启动都会重新生成

    def restore(self, messages: list, dropped: int = 0) -> None:
        self._start_history(messages)
        # 这份历史当初已经被压缩掉了多少条（保存会话时记下的，见 dropped）。接着往下数，
        # 以前记下的位置（会话里每一轮开头的 mark，见 rewind.py）重新打开之后才仍然指向同一条消息
        self._dropped = max(0, dropped)

    def mark(self) -> int:
        # 位置按"从对话开始数的第几条"算，被压缩掉的也算在内。这样循环在一轮开头记下的位置，
        # 即使这一轮中间压缩过，仍然指向同一条消息
        return self._dropped + len(self._messages)

    def rollback(self, mark: int) -> None:
        # 第一条是系统提示词，永远保留。退回的位置不会落进被压缩掉的那一段：循环只会退回到这一轮的开头，
        # 而正在进行的这一轮，压缩时总是原样留着（见 compaction.find_cut）；倒回最后一轮之前也先用 turn_at 查过
        index = max(mark - self._dropped, 1)
        if index < len(self._messages):
            del self._messages[index:]
            self._compaction_failed = False  # 退回去了一截：之后是新的一次尝试，压缩可以再试

    def dropped(self) -> int:
        return self._dropped

    def turn_at(self, mark: int) -> bool:
        # 压缩以后，开头是系统提示词和一问一答两条摘要；被压缩掉的那一轮，它的位置落在这几条上或者更前面
        index = mark - self._dropped
        return 1 <= index < len(self._messages) and compaction.is_turn_start(self._messages[index])

    def add_user(self, parts: list[str], attachments: Sequence[Attachment] = ()) -> None:
        self._compaction_failed = False  # 新的一轮：上一轮没压缩成的话，这一轮可以再试
        text = "\n\n".join(parts)
        if not attachments:
            self._messages.append({"role": "user", "content": text})  # 只有文字时用最简单的写法：一段文字
            return
        # 网页那边发送之前已经查过当前的模型收不收得了。这里再把一次关：真的漏过来了，
        # 宁可当场说清楚，也不要把几 MB 的图片发给一个看不了图的模型，换回一句看不懂的报错
        problem = unsupported(attachments, self.label, self.vision, self.pdf)
        if problem:
            raise ProviderError(problem)
        # 和 Claude 一样，附件在前、文字在后：先是图片，然后是 PDF、文本文件，最后是时间说明和用户的话
        content: list[dict] = [
            {"type": "image_url", "image_url": {"url": f"data:{item.media_type};base64,{item.data}"}}
            for item in attachments
            if item.kind == "image"
        ]
        # PDF 是 OpenAI 自己的写法（"file" 内容块），不是这套格式通用的，所以只有 pdf=True 的那家会走到这里
        content += [
            {"type": "file", "file": {"filename": item.name, "file_data": f"data:application/pdf;base64,{item.data}"}}
            for item in attachments
            if item.kind == "pdf"
        ]
        content += [{"type": "text", "text": wrap_text(item)} for item in attachments if item.kind == "text"]
        content.append({"type": "text", "text": text})
        self._messages.append({"role": "user", "content": content})

    def add_tool_results(self, results: list[ToolResult]) -> None:
        pictures = []
        for result in results:
            # 这套格式里没有"出错"标记，所以把它写在文字里，让模型知道
            text = f"[出错] {result.text}" if result.is_error else result.text
            if result.images and not self.vision:
                text += f"\n（结果里有 {len(result.images)} 张图片，{self.label} 看不了图片，没有交给它。）"
            elif result.images:
                pictures += result.images
            self._messages.append({"role": "tool", "tool_call_id": result.id, "content": text})
        if pictures:
            # 这套格式里，工具结果只能是文字。图片只能放在用户消息里，所以紧跟着结果追加一条，说明它们是哪儿来的。
            # 它的 role 也是 user，但不是一轮的开头：压缩找切口时靠开头这句话认出它（见 compaction.is_turn_start）
            content: list[dict] = [{"type": "text", "text": compaction.TOOL_IMAGES_NOTE}]
            content += [{"type": "image_url", "image_url": {"url": f"data:{image.media_type};base64,{image.data}"}} for image in pictures]
            self._messages.append({"role": "user", "content": content})

    def sends(self, tool: Any) -> bool:
        """这个工具发不发给模型：要有各家通用的说明书、在本地执行；全靠截图的只给看得懂图片的（见 use_tools）。"""
        return tool.portable is not None and tool.run is not None and (self.vision or not tool.vision)

    def tool_names(self) -> list[tuple[str, bool]]:
        return [(tool.name, True) for tool in self._local_tools]

    # ---- 调用模型 ---------------------------------------------------------

    def _request(self, messages: list[dict]) -> dict[str, Any]:
        """一次流式请求的参数。写摘要的请求也用它：两者只有历史的末尾不同，开头一样才能命中缓存。"""
        request: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},  # 让最后一块数据带上 token 用量
        }
        if self.tool_specs:
            request["tools"] = self.tool_specs
        return request

    def step(self, ui: StreamUI) -> Step:
        cut = self._compaction_cut()
        if cut is not None:
            usage = self._compact(ui, cut)
            if usage is not None:
                # 写摘要本身也调用了一次模型。把它当作单独的一步交回给循环：循环照常记下它的用量（算进费用），
                # 再调用一次 step，那一次才是真正的请求。不和真正的请求合成一步，是因为那一次万一失败，
                # 写摘要已经花掉的钱也要记上
                return Step(status="compacted", usage=usage)

        text, reasoning, finish, raw_usage = "", "", None, None
        calls: dict[int, dict] = {}  # 工具调用是一小段一小段到达的，按序号拼起来
        try:
            with self.client.chat.completions.create(**self._request(self._messages)) as stream:
                for chunk in stream:
                    if chunk.usage is not None:
                        raw_usage = chunk.usage
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta
                    # 有的模型（如 DeepSeek 的思考模式）会把思考过程放在这个额外的字段里
                    # 模型的输出也要先洗一遍（见 safe.py）：这些文字接下来要存进历史、发回给 API
                    thought = getattr(delta, "reasoning_content", None)
                    if thought:
                        thought = clean_text(thought)
                        reasoning += thought
                        ui.thinking(thought)
                    if delta.content:
                        piece_text = clean_text(delta.content)
                        text += piece_text
                        ui.text(piece_text)
                    for piece in delta.tool_calls or []:
                        _merge_tool_call(calls, piece)
                    finish = choice.finish_reason or finish
        except openai.APIError as exc:
            raise ProviderError(_explain(exc, self.label, self.model)) from exc
        finally:
            ui.end_stream()

        step = Step(status="done", usage=_usage(raw_usage), truncated=finish == "length", stop_reason=finish or "")
        self._context_tokens = step.usage.context_tokens  # 下一次请求之前，看它决定要不要先压缩
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
                # 用严格的解析：NaN、无穷大这类"不是数的数"也算参数不合法（它们写不回合法的 JSON）
                step.tool_calls.append(ToolCall(call["id"], call["function"]["name"], loads_strict(raw)))
            except (ValueError, RecursionError):
                problem = json.dumps({"error": "参数不是合法的 JSON", "INVALID_JSON": raw}, ensure_ascii=False)
                step.tool_calls.append(ToolCall(call["id"], call["function"]["name"], None, error=problem))
        if step.tool_calls:
            step.status = "tools"
        return step

    # ---- 压缩历史（为什么、怎么做、边界在哪，见 compaction.py 开头的说明）--------

    def _compaction_cut(self) -> int | None:
        """现在该不该先压缩：该的话返回切口的位置（见 compaction.find_cut），不该返回 None。"""
        if self._compaction_failed or not compaction.is_due(self._context_tokens, self.model):
            return None
        return compaction.find_cut(self._messages)

    def _compact(self, ui: StreamUI, cut: int) -> Usage | None:
        """把切口之前的对话换成摘要。返回写摘要那次请求的用量；没做成时返回 None，对话照原样继续。

        这是"历史只在末尾追加"的有意破例：较早的那一段被一次性换掉，之后又回到只追加。
        """
        # 写摘要要等一会儿（几秒到几十秒），先说一声，不然用户只看到界面停住了。
        # 用户这时已经点了"停止"的话，网页界面的 notice 会当场抛出中断，这次请求就不发了
        ui.notice("info", t("对话已经很长，正在请模型把较早的内容写成摘要……"))
        try:
            summary, usage, summary_tokens = self._summarize(compaction.summary_request(self._messages, cut))
        except Exception as exc:  # noqa: BLE001 —— 故意接住所有异常：摘要是为了省钱、防止超长，没写成不该让这一轮跟着失败
            self._compaction_failed = True
            reason = i18n.message_of(exc) if isinstance(exc, ProviderError) else f"{type(exc).__name__}: {exc}"
            ui.notice(
                "error",
                t(
                    "没能把较早的对话压缩成摘要（{error}）。这一轮照常继续，下一轮再试；对话太长、请求被拒绝的话，可以开一段新对话。",
                    error=reason,
                ),
            )
            return None

        before = len(self._messages)
        self._messages = compaction.compacted(self._messages, cut, summary)
        self._dropped += before - len(self._messages)
        self._context_tokens = 0  # 现在有多大，要等下一次请求的用量回来才知道
        # 之后每一轮少发多少：写摘要那次请求的输入，大致就是被换掉的那一段（另外还有系统提示词、工具说明书和
        # 一句指令，所以这个数略微偏大）；换进来的是摘要本身。是个约数
        saved = usage.input_tokens + usage.cache_read_tokens - summary_tokens
        # 这里用 info 而不是 notice：网页界面的 notice 会先看用户有没有点"停止"，点了就当场抛出中断，
        # 这一步的用量就记不上了。摘要已经写好、钱已经花了，先把这一步交回给循环，"停止"留到下一次请求时生效
        if saved > 0:
            ui.info(t("较早的对话已经压缩成摘要，之后每一轮发给模型的内容少了约 {n} 个 token。", n=i18n.number(saved)))
        else:
            ui.info(t("较早的对话已经压缩成摘要。"))
        return usage

    def _summarize(self, messages: list[dict]) -> tuple[str, Usage, int]:
        """请模型写摘要。返回（摘要，这次请求的用量，摘要本身占了多少 token）。

        和平时的请求走同一个接口、带同样的工具说明书（开头一样才能命中缓存），只是输出不显示在界面上。
        模型要是只调用了工具、没写出文字，就算没写成：这些工具调用不会被执行。
        """
        text, raw_usage = "", None
        try:
            with self.client.chat.completions.create(**self._request(messages)) as stream:
                for chunk in stream:
                    if chunk.usage is not None:
                        raw_usage = chunk.usage
                    if chunk.choices and chunk.choices[0].delta.content:
                        text += clean_text(chunk.choices[0].delta.content)  # 摘要要放进历史，同样先洗一遍
        except openai.APIError as exc:
            raise ProviderError(_explain(exc, self.label, self.model)) from exc
        if not text.strip():
            raise ProviderError(t("模型没有写出摘要。"))
        usage = _usage(raw_usage)
        # 这次请求的大小不是这段对话的大小，不能拿来更新"对话占了多少上下文"：填 0，循环记用量时就不会动它
        usage.context_tokens = 0
        # 思考的内容（有的模型有）也算在输出里，但不会留在历史里，所以"摘要本身多大"要把它减掉
        details = getattr(raw_usage, "completion_tokens_details", None)
        reasoning = getattr(details, "reasoning_tokens", None) or 0
        return text, usage, max(0, usage.output_tokens - reasoning)


def _merge_tool_call(calls: dict[int, dict], piece: Any) -> None:
    """把流里到达的一小段工具调用，合并到按序号保存的完整调用里。"""
    call = calls.setdefault(piece.index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
    if piece.id:
        call["id"] = clean_text(piece.id)
    if piece.function is not None:
        call["function"]["name"] += clean_text(piece.function.name or "")
        call["function"]["arguments"] += clean_text(piece.function.arguments or "")
    # 标准字段之外的东西（比如 Gemini 的签名）也要留着，之后原样发回去
    for key, value in (piece.model_extra or {}).items():
        if value is not None:
            call[key] = clean(value)


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
        return t("{provider} 的 API 密钥无效，请在设置里重新填写。", provider=label)
    if isinstance(exc, openai.PermissionDeniedError):
        return t("这个 API 密钥没有权限执行该请求：{error}", error=brief(exc.message))
    if isinstance(exc, openai.NotFoundError):
        return t("{provider} 找不到模型 {model}，或者这个模型不支持当前的接口。请换一个模型。", provider=label, model=model)
    if isinstance(exc, openai.RateLimitError):
        return t("{provider} 提示请求太频繁，或者账号余额、额度不足。", provider=label)
    if isinstance(exc, openai.BadRequestError):
        return t("请求被 {provider} 拒绝：{error}", provider=label, error=brief(exc.message))
    if isinstance(exc, openai.APIStatusError) and exc.status_code == 413:
        return too_large(label)
    if isinstance(exc, openai.APIStatusError):
        return t("{provider} 的 API 出错（状态码 {status}）：{error}", provider=label, status=exc.status_code, error=brief(exc.message))
    if isinstance(exc, openai.APIConnectionError):
        return t("连接不上 {provider} 的服务器，请检查网络。", provider=label)
    return t("调用 {provider} 失败：{error}", provider=label, error=exc)
