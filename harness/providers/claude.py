"""Claude：Anthropic 原生接口的适配层。

这里是所有"只有 Claude 才这样"的细节：请求里的参数、流式事件的格式、缓存、思考内容的签名、
服务器端的联网搜索、被拒绝后自动换模型、对话太长时的自动压缩。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Any

import anthropic

try:  # anthropic 的 SDK 用这个库收发网络请求。回复收到一半断线时，抛出来的是它的错误，不是 SDK 自己的
    import httpx2 as transport
except ImportError:  # 旧一些的 SDK 用的是它的前身
    import httpx as transport

from ..attachments import Attachment, wrap_text
from ..citations import from_citations
from ..config import (
    BROWSER_TOOLSET_MODELS,
    COMPUTER_TOOLSET_MODELS,
    ENABLE_COMPACTION,
    ENABLE_FALLBACKS,
    ENABLE_THINKING_DROP,
    FALLBACK_CAPABLE_MODELS,
    Config,
)
from ..display import describe_call
from ..i18n import t
from ..safe import clean, has_bad_number, tame
from ..tools import ToolRegistry
from .base import (
    ProviderError,
    Step,
    StreamUI,
    ToolCall,
    ToolResult,
    Unavailable,
    Usage,
    brief,
    too_large,
)

# 每个工具包有哪些模型用得了（见 use_tools）
TOOLSET_MODELS = {"browser": BROWSER_TOOLSET_MODELS, "computer": COMPUTER_TOOLSET_MODELS}


class AnthropicBackend:
    provider = "anthropic"

    def __init__(self, client: Any, model: str, config: Config, tools: ToolRegistry, system_prompt: str):
        self.client = client
        self.model = model
        self.config = config

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
        self.use_tools(tools)
        self._messages: list[dict] = []

    def use_tools(self, tools: ToolRegistry) -> None:
        """换一套工具。按上面的规矩，只能在一段对话开始之前换（见 agent.py 的 on_start）。

        工具包（Anthropic 定义的一组工具，比如浏览器的 browser_toolset_20260801、电脑的 computer_toolset_20260801）在请求里只占一项：
        同一个包的成员共用一份说明书（Tool.spec），只发一次。这个模型用得了这个包的话，它的"替身"
        （我们自己写的 browser_* 函数工具，见 Tool.stands_in_for）就不发；用不了的话反过来，发替身、不发这个包。
        用不用得了是在这里定下的，一段对话里不再变：这段对话中途换成同一家的另一个模型，说明书也照旧
        （智狐能选的 Claude 模型都支持这两个工具包，见 config.BROWSER_TOOLSET_MODELS、COMPUTER_TOOLSET_MODELS）。
        """
        self._tools = tools
        usable = {name for name, models in TOOLSET_MODELS.items() if self.model in models}
        self.toolsets: set[str] = set()
        specs: list[dict] = []
        for tool in tools:
            if tool.toolset:
                if tool.toolset in usable and tool.toolset not in self.toolsets:
                    self.toolsets.add(tool.toolset)
                    specs.append(tool.spec)
            elif not (tool.stands_in_for and tool.stands_in_for in usable):
                specs.append(tool.spec)
        self.tool_specs = specs

    # ---- 对话历史 ---------------------------------------------------------

    @property
    def messages(self) -> list[dict]:
        return self._messages

    def reset(self) -> None:
        self._messages = []

    def export(self) -> list:
        return self._messages  # 历史里只有普通的字典和字符串，可以直接存成 JSON

    def restore(self, messages: list, dropped: int = 0) -> None:
        self._messages = list(messages)

    def mark(self) -> int:
        return len(self._messages)

    def rollback(self, mark: int) -> None:
        # 只从末尾去掉：留下的每一块思考，它前面的内容都和当初一字不差，签名照样对得上（见 __init__ 里的规矩）
        del self._messages[mark:]

    def dropped(self) -> int:
        return 0  # Claude 的历史从不在本地压缩（压缩在 Anthropic 的服务器上做，历史照样只追加）

    def turn_at(self, mark: int) -> bool:
        # 用户消息有两种：用户说的话，和工具结果（content 里全是 tool_result 块）。开始一轮的是前一种
        if not 0 <= mark < len(self._messages):
            return False
        message = self._messages[mark]
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(message, dict) or message.get("role") != "user" or not isinstance(content, list) or not content:
            return False
        return not all(isinstance(block, dict) and block.get("type") == "tool_result" for block in content)

    def add_user(self, parts: list[str], attachments: Sequence[Attachment] = ()) -> None:
        # 附件放在文字前面：文档建议先给图片和文档、再提问题，模型读得更好。
        # 顺序是图片、PDF、文本文件，然后才是时间说明和用户的话。
        # 缓存不受影响：请求用的是自动缓存（见 step 里的 cache_control），标记落在这条消息的最后一块上，
        # 图片、PDF 块本身也都能被缓存。
        content: list[dict] = []
        for item in attachments:
            if item.kind == "image":
                content.append(
                    {"type": "image", "source": {"type": "base64", "media_type": item.media_type, "data": item.data}}
                )
        for item in attachments:
            if item.kind == "pdf":
                # title：模型回答时能说出"这份 PDF"指的是哪个文件
                source = {"type": "base64", "media_type": item.media_type, "data": item.data}
                content.append({"type": "document", "source": source, "title": item.name})
        content += [{"type": "text", "text": wrap_text(item)} for item in attachments if item.kind == "text"]
        content += [{"type": "text", "text": part} for part in parts]
        self._messages.append({"role": "user", "content": content})

    def add_tool_results(self, results: list[ToolResult]) -> None:
        # 所有结果必须放在同一条消息里，并且这条消息里只有结果。
        # （API 靠"最后一条消息只有工具结果"来判断这是同一轮对话的延续，联网搜索的次数上限按轮累计。）
        # 工具包成员的调用，结果上要写回同一个 toolset_name（API 的规定）。是不是成员，看历史里模型的那次调用
        toolsets = _toolset_names(self._messages[-1] if self._messages else None)
        content = []
        for result in results:
            block: dict[str, Any] = {"type": "tool_result", "tool_use_id": result.id, "content": result.text}
            toolset = toolsets.get(result.id, "")
            parts: list[dict] = []
            if result.images or (toolset and result.state and not result.is_error):
                # 结果里有图片（MCP 的工具、浏览器的截图）：内容写成一串块，文字在前，图片跟在后面。Claude 看得懂图片
                if result.text and not (toolset and result.state_only):
                    parts.append({"type": "text", "text": result.text})
                parts += [
                    {"type": "image", "source": {"type": "base64", "media_type": image.media_type, "data": image.data}}
                    for image in result.images
                ]
                if toolset and result.state and not result.is_error:
                    # 标签页的情况：API 会把它写成一段文字给模型看（Tab Context）。出错的结果上不能带（API 的规定）
                    parts.append({"type": "browser_state", **result.state})
                block["content"] = parts
            if toolset:
                block["toolset_name"] = toolset
            if result.is_error:
                block["is_error"] = True
            content.append(block)
        self._messages.append({"role": "user", "content": content})

    def sends(self, tool: Any) -> bool:
        """这个工具发不发给模型：工具包的成员看这个模型用不用得了这个包（见 use_tools），用得了的话它的替身就不发。"""
        if tool.toolset:
            return tool.toolset in self.toolsets
        return not (tool.stands_in_for and tool.stands_in_for in self.toolsets)

    def can_switch_to(self, model: str) -> bool:
        """能不能换成 model 接着这段对话（备用模型，见 fallback.py）。

        工具说明书在一段对话里不能变（见 __init__ 里的规矩），而发哪些工具包是开始时按那个模型定下的（use_tools）。
        所以换过去的模型必须用得了这段对话里发出去的每一个工具包，不然 API 会拒绝这份说明书。
        反过来没关系：它用得了、这段对话却发的是替身（browser_* 这些普通的函数工具），照样能用。
        思考块的签名不用管：换模型不会让请求失败，新模型读不了的思考块 API 会自己去掉（见 fallback.py 开头）。
        """
        return all(model in TOOLSET_MODELS[name] for name in self.toolsets)

    def tool_names(self) -> list[tuple[str, bool]]:
        """真正发给模型的那些：工具包只算一个（用包名），用得了工具包时它的替身不算。"""
        names: list[tuple[str, bool]] = []
        for tool in self._tools:
            entry = (tool.toolset, True) if tool.toolset else (tool.name, tool.run is not None)
            if self.sends(tool) and entry not in names:
                names.append(entry)
        return names

    # ---- 调用模型 ---------------------------------------------------------

    def step(self, ui: StreamUI) -> Step:
        betas = []
        # 让模型自己决定何时思考、想多深；summarized 表示把思考的摘要也返回给我们显示
        thinking: dict[str, Any] = {"type": "adaptive", "display": "summarized"}
        if ENABLE_THINKING_DROP:
            # 读回来的旧会话里，以前的思考内容可能和现在的对话对不上；让 API 丢掉它们而不是报错
            betas.append("thinking-binding-controls-2026-08-01")
            thinking["block_binding"] = {"prefix_mismatch_behavior": "drop_block"}

        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.config.max_tokens,
            "system": self.system,
            "tools": self.tool_specs,
            "messages": self._messages,
            "thinking": thinking,
            "output_config": {"effort": self.config.effort},
            # 自动缓存到对话的最后一块。循环里每一步都重发整个历史，有缓存便宜很多
            "cache_control": {"type": "ephemeral"},
        }
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

        response, retries = self._send(request, ui)
        step = Step(status="done", usage=_usage(response), stop_reason=response.stop_reason or "", retries=retries)
        # 给运行记录看的（见 trace/）：服务器上用了哪些工具（联网搜索、读网页）、有没有顺便压缩历史
        step.server_tools = dict(Counter(block.name for block in response.content if block.type == "server_tool_use"))
        step.compacted = any(block.type == "compaction" for block in response.content)

        if response.stop_reason == "refusal":
            # 不把被拒绝的回复放进历史：留着的话，后面多半还会被拒
            step.status = "refused"
            step.note = getattr(getattr(response, "stop_details", None), "category", None) or ""
            return step

        # 把模型的回复原样追加进历史。不能只存文字：思考块、工具调用块下次都要原样发回去。
        kept = blocks_to_keep(response.content)
        content = [_plain(block) for block in kept]
        step.tool_calls = [_tool_call(block, data) for block, data in zip(kept, content) if block.type == "tool_use"]
        if content:
            self._messages.append({"role": "assistant", "content": content})

        step.truncated = response.stop_reason == "max_tokens"
        if response.stop_reason == "pause_turn":
            step.status = "paused"
        elif step.tool_calls:
            step.status = "tools"
        return step

    def _send(self, request: dict, ui: StreamUI) -> tuple[Any, int]:
        """发一次请求，返回（完整的回复，重新请求了几次）。"""
        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                # 流式接收：模型一边生成，我们一边显示
                with self.client.beta.messages.stream(**request) as stream:
                    for event in stream:
                        self._show(event, ui)
                    return stream.get_final_message(), attempt - 1
            except ValueError as exc:
                # 极少数情况下，模型生成的工具参数不是合法的 JSON，SDK 解析不了。重新请求一次即可。
                if attempt == attempts:
                    message = t("模型连续 {n} 次返回了解析不了的内容，这一轮没有完成（{error}）。可以再试一次。", n=attempts, error=exc)
                    raise ProviderError(message) from exc
                ui.info(t("模型返回的工具参数格式有误，正在重试……"))
            except anthropic.APIError as exc:
                raise failure(exc)(_explain(exc, self.model)) from exc
            except transport.HTTPError as exc:
                # 连上了、也开始收回复了，但中途断了（网络抖动、电脑休眠、代理超时）。
                # 已经显示出来的半截内容不算数：这一步没有走完，循环会把它撤掉。
                raise ProviderError(t("和 Anthropic 的连接中断了，这一轮没有完成。请检查网络后再试一次。")) from exc
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
        elif event.type == "content_block_start" and event.content_block.type != "text":
            # 一个不是文字的块（思考、工具调用、联网搜索……）开始了：前面那段文字到这里才算写完（见下面）
            ui.end_stream()
        elif event.type == "content_block_stop":  # 一个内容块结束了
            block = event.content_block
            if block.type == "thinking":
                ui.end_stream()
            elif block.type == "text":
                # 文字块结束时先不说"写完了"。联网搜索后带引用的回答，API 会按引用把它切成一串连着的 text 块
                # （"……Se describe como" | "proveedor B2B líder…" | ". Anuncia" | ……）。每块都结束一次的话，
                # 网页把每一小块当成单独的一段去排 Markdown（一句话断成好几行、标点单独一行），聊天通道把每一小块
                # 各发成一条消息。所以连着的文字块算同一段：下一个不是文字的块开始时（上面那一支）、
                # 或者整条回复结束时（stream 的 finally 里），才结束。
                # 这一块引用了网页的话，现在告诉界面：块里的字都已经显示了，记号就标在它们后面。
                # SDK 已经把流式的 citations_delta 攒在了这个块上；历史里存的那一份（_plain）原样不动
                sources = from_citations(getattr(block, "citations", None))
                if sources:
                    ui.cite(sources)
            else:
                notice = block_notice(block)
                if notice is not None:
                    ui.notice(*notice)


def _tool_call(block: Any, data: dict) -> ToolCall:
    """把模型发来的一次工具调用整理成循环能执行的样子。data 是这个块要存进历史的那一份。

    参数里要是有 NaN 或无穷大（比如模型写了 1e999），这次调用不执行，把原因告诉模型：
    这样的数写不成合法的 JSON，留在历史里的话，这段会话之后就既存不了盘、也发不出下一次请求了。
    所以存进历史和显示给用户的那一份，把它们换成说明性的文字。
    """
    try:
        bad = has_bad_number(block.input)
    except RecursionError:  # 嵌套深得不正常：同样不执行
        bad = True
    toolset = getattr(block, "toolset_name", None) or ""
    if not bad:
        return ToolCall(block.id, block.name, clean(block.input), toolset=toolset)
    try:
        data["input"] = tame(block.input)
    except RecursionError:
        data["input"] = {}
    problem = "参数里有 NaN、无穷大这样不是数的数，或者嵌套得太深，这次调用没有执行。请用正常的参数重新调用。"
    return ToolCall(block.id, block.name, data["input"], error=problem, toolset=toolset)


def _toolset_names(message: dict | None) -> dict[str, str]:
    """一条模型的回复里，哪些工具调用是工具包的成员：{调用的 id: 工具包的名字}。"""
    if not message or message.get("role") != "assistant" or not isinstance(message.get("content"), list):
        return {}
    return {
        block["id"]: block["toolset_name"]
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("toolset_name")
    }


def _plain(block: Any) -> dict:
    """把 SDK 的内容块对象变成普通的字典，内容和 SDK 把它发回 API 时用的完全一样。

    历史里只放普通字典有两个好处：可以直接存成 JSON 文件；读回来之后发给 API 的内容
    和当初一字不差（对话历史必须保持原样，原因见 AnthropicBackend.__init__ 里的说明）。
    """
    data = block.to_dict(mode="json")
    data.pop("parsed_output", None)  # SDK 自己加的字段，不属于 API 的内容
    return data


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


# 回复收到一半时服务器发来的错误事件（SSE 的 error）：SDK 把它变成一个状态码是 200 的 APIStatusError，
# 只能看里面写的错误类型。这几种是"暂时用不了"（https://platform.claude.com/docs/en/api/errors）
_TEMPORARY_TYPES = {"overloaded_error", "api_error", "rate_limit_error"}


def failure(exc: anthropic.APIError) -> type[ProviderError]:
    """这个错误算不算"这个模型暂时用不了"（Unavailable：可以换同一家的备用模型接着做，见 fallback.py）。
    算的：过载（529）、服务器出错（5xx）、请求太频繁（429），以及回复到一半时服务器说的这几种。SDK 已经重试过了。"""
    if isinstance(exc, (anthropic.RateLimitError, anthropic.InternalServerError)):
        return Unavailable
    if isinstance(exc, anthropic.APIStatusError) and (exc.status_code >= 500 or _error_type(exc) in _TEMPORARY_TYPES):
        return Unavailable
    return ProviderError


def _error_type(exc: anthropic.APIStatusError) -> str:
    """API 的错误里写的类型（overloaded_error……）。"""
    kind = _error_field(exc, "type")
    return kind if isinstance(kind, str) else ""


def _error_field(exc: anthropic.APIStatusError, name: str) -> object:
    body = exc.body
    error = body.get("error") if isinstance(body, dict) else None
    return error.get(name) if isinstance(error, dict) else None


def _explain(exc: anthropic.APIError, model: str) -> str:
    """把 API 的错误翻译成看得懂的提示。"""
    if isinstance(exc, anthropic.AuthenticationError):
        return t("Claude 的 API 密钥无效，请在设置里重新填写。")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return t("这个 API 密钥没有权限执行该请求：{error}", error=brief(exc.message))
    if isinstance(exc, anthropic.NotFoundError):
        return t("找不到模型 {model}，请换一个模型。", model=model)
    if isinstance(exc, anthropic.RateLimitError):
        return t("请求太频繁，或者超出了账号的用量限制。请稍后再试。")
    if isinstance(exc, anthropic.BadRequestError):
        return t("请求被 API 拒绝：{error}", error=brief(exc.message))
    if isinstance(exc, anthropic.APIStatusError) and exc.status_code == 413:
        return too_large("Claude")
    if isinstance(exc, anthropic.APIStatusError) and exc.status_code < 400 and _error_type(exc):
        # 回复收到一半时服务器发来的错误：HTTP 的状态码是 200，写出来只会误导人，写错误的类型
        said = _error_field(exc, "message")
        return t("API 出错（{kind}）：{error}", kind=_error_type(exc), error=brief(said if isinstance(said, str) and said else exc.message))
    if isinstance(exc, anthropic.APIStatusError):
        return t("API 出错（状态码 {status}）：{error}", status=exc.status_code, error=brief(exc.message))
    if isinstance(exc, anthropic.APIConnectionError):
        return t("连接不上 Anthropic 的服务器，请检查网络。")
    return t("调用 Claude 失败：{error}", error=exc)


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
            count = len(block.content)
            return "result", t("找到 1 条结果") if count == 1 else t("找到 {n} 条结果", n=count)
        code = getattr(block.content, "error_code", None) or "unknown"
        if code == "max_uses_exceeded":
            return "info", t("这条消息的搜索次数已经用完，助手会根据已有的信息回答")
        return "error", t("搜索失败：{code}", code=code)
    if kind == "web_fetch_tool_result":
        code = getattr(block.content, "error_code", None)
        if code == "max_uses_exceeded":
            return "info", t("这条消息读取网页的次数已经用完")
        return ("error", t("读取网页失败：{code}", code=code)) if code else None
    if kind == "compaction":
        return "info", t("对话已经很长，较早的内容被压缩成了摘要")
    if kind == "fallback":
        return "info", t("{rejected} 拒绝了这个请求，已改由 {fallback} 继续", rejected=block.from_.model, fallback=block.to.model)
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
