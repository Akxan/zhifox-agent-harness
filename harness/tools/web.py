"""联网搜索 —— 第三类工具：服务器工具。

说明书和执行都在 Anthropic 那边。我们只需要声明"启用这个工具"，
模型想搜索时，搜索直接在服务器上完成，结果和回答一起返回，本地没有任何代码要执行。
所以这里的 Tool 没有 run 函数，也没有给其他厂商用的说明书：这是只有 Claude 才有的能力。

费用：每次搜索除了 token 之外另收 1 美分（每 1,000 次 10 美元），读取网页只收 token 的钱。
"""

from __future__ import annotations

from .base import Tool

# 读取一个网页时，最多把这么多 token 的内容放进对话，免得一个长网页就把上下文撑大
MAX_FETCH_TOKENS = 8000


def make_web_tools(max_uses: int) -> list[Tool]:
    """max_uses：用户每发一条消息，模型最多能搜索（以及读取网页）多少次。0 表示不限制。

    这个上限由 API 强制执行：超过之后，搜索会直接返回一个"次数已用完"的错误，
    模型看到后会根据已有的信息作答。
    """
    search: dict = {"type": "web_search_20250305", "name": "web_search"}
    fetch: dict = {"type": "web_fetch_20250910", "name": "web_fetch", "max_content_tokens": MAX_FETCH_TOKENS}
    if max_uses > 0:
        search["max_uses"] = max_uses
        fetch["max_uses"] = max_uses
    return [Tool(spec=search), Tool(spec=fetch)]
