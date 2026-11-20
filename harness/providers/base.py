"""各家模型的统一接口。

不同厂商的 API 像是不同的方言：对话历史怎么组织、工具怎么声明、流式数据长什么样，各不相同。
agent 循环（agent.py）不关心这些差别，它只要求每一家都能做下面 Backend 里列出的几件事。
每家的方言封装在各自的文件里：

  claude.py          Claude，使用 Anthropic 的原生接口
  openai_compat.py   GPT、Gemini、DeepSeek，它们都支持 OpenAI 的 Chat Completions 格式

对话历史由 Backend 自己保存，因为各家的格式不通用。这也是换到另一家模型时要开始新对话的原因。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class ProviderError(Exception):
    """调用模型失败。错误信息是可以直接显示给用户看的说明。"""


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


@dataclass
class ToolResult:
    """工具执行完的结果，要发回给模型。"""

    id: str  # 对应 ToolCall 的 id
    text: str
    is_error: bool = False


@dataclass
class Step:
    """调用一次模型之后，循环需要知道的全部信息。"""

    # done    模型答完了，这一轮结束
    # tools   模型要调用工具（见 tool_calls）
    # paused  服务器端的工具（联网搜索）还没做完，原样再请求一次让它接着做
    # refused 模型或安全系统拒绝了这个请求
    status: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    truncated: bool = False  # 回复达到长度上限被截断了
    note: str = ""  # 补充说明，比如拒绝的类别
    usage: Usage = field(default_factory=Usage)


class StreamUI(Protocol):
    """适配层在接收模型输出时，需要界面做的事。"""

    def thinking(self, text: str) -> None: ...  # 思考内容的一小段
    def text(self, text: str) -> None: ...  # 回答正文的一小段
    def notice(self, level: str, text: str) -> None: ...  # 一行提示：tool / result / error / info
    def end_stream(self) -> None: ...  # 正在逐字输出的那段文字结束了
    def info(self, text: str) -> None: ...


class Backend(Protocol):
    """一家模型提供方要实现的全部接口。"""

    provider: str  # 提供方的名字，如 "anthropic"
    model: str

    @property
    def messages(self) -> list: ...  # 对话历史（各家格式不同）

    def reset(self) -> None: ...  # 清空对话
    def export(self) -> list: ...  # 把对话历史交出来保存（只含能写成 JSON 的普通数据）
    def restore(self, messages: list) -> None: ...  # 换上一份以前保存的对话历史
    def mark(self) -> int: ...  # 记下历史现在的位置
    def rollback(self, mark: int) -> None: ...  # 把历史退回到某个位置（只会从末尾去掉内容）
    def add_user(self, parts: list[str]) -> None: ...  # 追加一条用户消息
    def add_tool_results(self, results: list[ToolResult]) -> None: ...  # 追加工具结果
    def step(self, ui: StreamUI) -> Step: ...  # 调用一次模型，边收边显示，并把回复追加进历史
    def tool_names(self) -> list[tuple[str, bool]]: ...  # 这家模型能用的工具：（名字，是否在本地执行）
