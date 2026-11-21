"""agent 工具：主助手把一项任务交给一个自定义 Agent（子 Agent）的门。第一类工具（自定义工具），说明书是我们写的。

真正做事的在 agents/runner.py：给子 Agent 建一套新的历史、它自己的提示词、它能用的工具和它选的模型，
跑完把回答交回来。这里只有说明书，以及把它包成一个工具。

为什么说明书里不列出有哪些 Agent：说明书在一段对话里不能变（原因见 providers/claude.py），而你随时可能新建、
删掉一个 Agent。所以列表放在用户消息里（<agents> 块，见 agents/catalog.py），说明书永远是下面这一段。
这个工具也一直都在（哪怕一个 Agent 都还没有），中途新建了第一个 Agent，这段对话照样用得上。

结果是给模型看的，所以写的是中文，不进翻译表。
"""

from __future__ import annotations

from collections.abc import Callable

from .base import Tool, ToolOutput, function_tool

TOOL_NAME = "agent"

DESCRIPTION = (
    "把一项任务交给一个 Agent 去做，等它做完，拿回它的回答。Agent 是用户定制的、擅长某一类事的助手，"
    "可用的列在用户消息里的 <agents> 块中，name 填其中一个的名字。"
    "它从头开始：看不到你和用户的对话，也看不到附件，所以 task 里要写清楚背景、具体要做什么、要交回什么。"
    "它用自己的设定、工具和模型做事，修改文件、执行命令照样要用户确认；做完后，它最后的回答会作为这个工具的结果交给你，"
    "由你转告用户。适合要查很多资料、读很多文件、而你只需要结论的事；简单的事自己做更快也更便宜。"
)


def make_agent_tool(run: Callable[[str, str], str | ToolOutput]) -> Tool:
    """run(name, task)：把任务交给名叫 name 的 Agent，返回给模型看的结果（见 agents/runner.py 的 Delegator.run）。"""
    return function_tool(
        name=TOOL_NAME,
        description=DESCRIPTION,
        properties={
            "name": {"type": "string", "description": "Agent 的名字，就是 <agents> 列表里 <name> 的内容"},
            "task": {
                "type": "string",
                "description": "交给它的任务：背景、要做什么、要交回什么。它看不到你们的对话，需要的信息都要写在这里",
            },
        },
        required=["name", "task"],
        function=run,
    )
