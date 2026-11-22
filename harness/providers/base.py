"""各家模型的统一接口。

不同厂商的 API 像是不同的方言：对话历史怎么组织、工具怎么声明、流式数据长什么样，各不相同。
agent 循环（agent.py）不关心这些差别，它只要求每一家都能做下面 Backend 里列出的几件事。
每家的方言封装在各自的文件里：

  claude.py          Claude，使用 Anthropic 的原生接口
  openai_compat.py   GPT、Gemini、DeepSeek，它们都支持 OpenAI 的 Chat Completions 格式

对话历史由 Backend 自己保存，因为各家的格式不通用。这也是换到另一家模型时要开始新对话的原因。
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..attachments import Attachment
from ..i18n import t
from ..tools.base import ToolImage

ERROR_CHARS = 300  # 厂商返回的出错说明最多显示这么多字


class ProviderError(Exception):
    """调用模型失败。错误信息是可以直接显示给用户看的说明。"""


class Unavailable(ProviderError):
    """这个模型暂时用不了：服务器过载（529）、服务器出错（5xx）、请求太频繁（429），SDK 自己重试过以后还是不行。
    等一会儿多半就好，或者换同一家的另一个模型能接着做（备用模型，见 fallback.py）。
    换个模型也没用的错误（密钥不对、请求写错了）仍然是普通的 ProviderError。"""


def brief(message: object) -> str:
    """把厂商返回的出错说明整理成一小段能读的话。

    正常情况下它就是一句话。但请求经过代理或网关时，出错返回的常常是一整个 HTML 网页（"502 Bad Gateway"之类），
    原样显示会在页面上铺满几屏标签。所以：是网页的话，只留下正文里的文字（去掉 <head>、脚本和样式），
    正文是空的才用标题；再把空白压成一个，太长的截断。
    """
    text = str(message)
    if re.search(r"<\s*(!doctype|html|head|body)\b", text, re.IGNORECASE):
        title = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
        body = re.sub(r"(?is)<(head|script|style)\b.*?</\1\s*>", " ", text)
        body = re.sub(r"<[^>]*>", " ", body)
        text = html.unescape(body if body.strip() else title.group(1) if title else "")
    text = " ".join(text.split())
    return text if len(text) <= ERROR_CHARS else text[: ERROR_CHARS - 1] + "…"


def too_large(provider: str) -> str:
    """请求超过了厂商规定的大小（HTTP 413）时给用户看的话。

    平时很难碰到，有了附件就容易了：对话历史每次都要整个重发，里面的图片和 PDF 也一样，
    所以附件一多、聊得一久，请求就会越来越大。
    """
    return t(
        "请求太大，超过了 {provider} 的上限。对话里的图片和 PDF 每次请求都要整份重发：可以开一段新对话，或者把附件缩小一些再发。",
        provider=provider,
    )


@dataclass
class Usage:
    """用量。token 是计费单位，一个汉字大约 1 到 2 个 token。"""

    requests: int = 0
    input_tokens: int = 0  # 没有命中缓存、按原价计费的输入
    cache_read_tokens: int = 0  # 命中缓存的输入，价格低得多
    cache_write_tokens: int = 0  # 这次写入缓存的输入，比原价略贵（只有 Claude 单独收这一项）
    output_tokens: int = 0
    searches: int = 0  # 联网搜索次数，按次另外收费
    context_tokens: int = 0  # 最近一次请求时，这段对话占用的上下文大小

    def add(self, other: Usage) -> None:
        self.requests += other.requests
        self.input_tokens += other.input_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.output_tokens += other.output_tokens
        self.searches += other.searches
        self.context_tokens = other.context_tokens or self.context_tokens


@dataclass
class ToolCall:
    """模型想调用一个工具。"""

    id: str
    name: str
    input: Any  # 解析好的参数
    error: str = ""  # 参数没法解析时的说明；这种调用不会被执行
    toolset: str = ""  # 调用的是哪个工具包的成员（Claude 的浏览器工具包是 "browser"，见 Tool.toolset）；普通的工具是空的

    @property
    def shown(self) -> str:
        """给人看的名字：工具包的成员写成"包名.成员"（browser.left_click），一看就知道是哪儿的"""
        return f"{self.toolset}.{self.name}" if self.toolset else self.name


@dataclass
class ToolResult:
    """工具执行完的结果，要发回给模型。"""

    id: str  # 对应 ToolCall 的 id
    text: str
    is_error: bool = False
    images: tuple[ToolImage, ...] = ()  # 结果里的图片（MCP 的工具、浏览器的截图）。各家怎么交给模型见各自的 add_tool_results
    state: dict | None = None  # 浏览器的标签页情况（见 ToolOutput.state），只有 Claude 的浏览器工具包用
    state_only: bool = False


@dataclass
class Step:
    """调用一次模型之后，循环需要知道的全部信息。"""

    # done    模型答完了，这一轮结束
    # tools   模型要调用工具（见 tool_calls）
    # paused  服务器端的工具（联网搜索）还没做完，原样再请求一次让它接着做
    # refused 模型或安全系统拒绝了这个请求
    # compacted 这一步没有回答，而是把较早的对话压缩成了摘要（见 compaction.py）；循环记下用量，再请求一次
    status: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    truncated: bool = False  # 回复达到长度上限被截断了
    note: str = ""  # 补充说明，比如拒绝的类别
    usage: Usage = field(default_factory=Usage)
    # 下面几项循环不用，只给运行记录看（见 trace/）：这一次请求为什么停下、有没有重试、服务器上做了什么
    stop_reason: str = ""  # API 原样给的停止原因（Claude 的 end_turn、tool_use……，OpenAI 格式的 stop、tool_calls……）
    retries: int = 0  # 这一步在适配层里重新请求了几次（比如工具参数不是合法的 JSON，见 claude.py 的 _send）
    compacted: bool = False  # 这次请求里，服务器顺便把较早的对话压缩成了摘要（Claude 的服务器端压缩）
    server_tools: dict[str, int] = field(default_factory=dict)  # 服务器端的工具用了几次：{"web_search": 2}


class StreamUI(Protocol):
    """适配层在接收模型输出时，需要界面做的事。"""

    def thinking(self, text: str) -> None: ...  # 思考内容的一小段
    def text(self, text: str) -> None: ...  # 回答正文的一小段
    def notice(self, level: str, text: str) -> None: ...  # 一行提示：tool / result / error / info
    def end_stream(self) -> None: ...  # 正在逐字输出的那段文字结束了
    def info(self, text: str) -> None: ...
    # 刚写完的那一小段回答引用了这些网页：[{"url", "title", "quote"}]（citations.from_citations 的结果）。
    # 只有 Claude 联网搜索后的回答才有；编号、怎么显示由界面决定（见 citations.py）
    def cite(self, sources: list[dict]) -> None: ...


class Backend(Protocol):
    """一家模型提供方要实现的全部接口。"""

    provider: str  # 提供方的名字，如 "anthropic"
    model: str
    # 这个模型能不能用工具。内置的几家都能；自定义接口上的模型不一定（见 openai_compat.py），不能的话一个工具都不发
    tools_supported: bool

    @property
    def messages(self) -> list: ...  # 对话历史（各家格式不同）

    def reset(self) -> None: ...  # 清空对话
    def export(self) -> list: ...  # 把对话历史交出来保存（只含能写成 JSON 的普通数据）
    # 换上一份以前保存的对话历史。dropped 是当时 dropped() 的值：交还给它，以前记下的位置（mark）才仍然对得上
    def restore(self, messages: list, dropped: int = 0) -> None: ...
    def mark(self) -> int: ...  # 记下历史现在的位置（中途压缩过历史，这个位置也仍然有效）
    def rollback(self, mark: int) -> None: ...  # 把历史退回到某个位置（只会从末尾去掉内容）
    # 压缩换掉了多少条历史（位置是从对话开始数的，换掉的也算在内）。保存会话时一起存下，见 restore
    def dropped(self) -> int: ...
    # mark 这个位置上，是不是还留着开始一轮对话的那条用户消息：没被撤回、没被压缩进摘要。
    # 倒回最后一轮（编辑、重新生成，见 rewind.py）之前先问它，压缩掉了的那一轮倒不回去
    def turn_at(self, mark: int) -> bool: ...
    # 追加一条用户消息。parts 是几段文字（时间说明、用户的话），attachments 是随消息发来的附件。
    # 附件怎么放进消息由各家自己决定；这一家收不了的附件（比如看不了图片）抛出 ProviderError，什么都不追加
    def add_user(self, parts: list[str], attachments: Sequence[Attachment] = ()) -> None: ...
    def add_tool_results(self, results: list[ToolResult]) -> None: ...  # 追加工具结果
    def step(self, ui: StreamUI) -> Step: ...  # 调用一次模型，边收边显示，并把回复追加进历史
    def tool_names(self) -> list[tuple[str, bool]]: ...  # 这家模型能用的工具：（名字，是否在本地执行）
    def sends(self, tool: Any) -> bool: ...  # 这个工具（Tool）发不发给这家模型（见各家的 use_tools）
    def use_tools(self, tools: Any) -> None: ...  # 换一套工具（ToolRegistry）。只在一段对话开始之前调用，见 agent.py 的 on_start
    # 这段对话能不能换成同一家的 model 接着做（备用模型，见 fallback.py）：系统提示词、工具说明书、历史都不变，
    # 所以要看换过去以后，开始时定下的这套工具说明书它还认不认
    def can_switch_to(self, model: str) -> bool: ...
