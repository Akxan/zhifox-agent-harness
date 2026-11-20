"""联网搜索 —— 第三类工具：服务器工具。

说明书和执行都在 Anthropic 那边。我们只需要声明"启用这个工具"，
模型想搜索时，搜索直接在服务器上完成，结果和回答一起返回，本地没有任何代码要执行。
所以这里的 Tool 没有 run 函数。
"""

from __future__ import annotations

from .base import Tool


def make_web_tools() -> list[Tool]:
    return [
        Tool(spec={"type": "web_search_20260209", "name": "web_search"}),
        Tool(spec={"type": "web_fetch_20260209", "name": "web_fetch"}),
    ]
