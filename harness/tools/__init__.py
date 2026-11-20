"""工具箱：把所有工具收集到一起。

这个助手有三类工具，区别在于"说明书谁写"和"谁来执行"：

  类别                      说明书        执行          例子
  1. 自定义工具             我们写        我们的电脑    todo.py、tasks.py、shell.py
  2. Anthropic 定义的工具   Anthropic     我们的电脑    files.py、memory.py
  3. 服务器工具             Anthropic     Anthropic     web.py
"""

from __future__ import annotations

from collections.abc import Iterator

from ..config import Config
from ..tasks import TaskStore
from .base import Tool, ToolError
from .files import FileEditor, make_file_tool
from .memory import make_memory_tool
from .shell import Shell, make_shell_tool
from .tasks import make_task_tools
from .todo import TodoStore, make_todo_tools
from .web import make_web_tools

__all__ = ["Tool", "ToolError", "ToolRegistry", "build_registry"]


class ToolRegistry:
    def __init__(self, tools: list[Tool]):
        self._tools = {tool.name: tool for tool in tools}

    def specs(self) -> list[dict]:
        """所有工具的说明书，每次请求都会发给模型。"""
        return [tool.spec for tool in self._tools.values()]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())


def build_registry(config: Config, files: FileEditor, todos: TodoStore, shell: Shell, tasks: TaskStore) -> ToolRegistry:
    tools = [
        *make_todo_tools(todos),
        *make_task_tools(tasks),
        make_memory_tool(config.data_dir),
        make_file_tool(files),
        make_shell_tool(shell),
    ]
    if config.web:
        tools += make_web_tools(config.max_searches)
    return ToolRegistry(tools)
