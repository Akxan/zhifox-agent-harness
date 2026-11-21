"""工具箱：把所有工具收集到一起。

这个助手有三类工具，区别在于"说明书谁写"和"谁来执行"：

  类别                      说明书        执行          例子
  1. 自定义工具             我们写        我们的电脑    todo.py、tasks.py、shell.py、skill.py、agent.py
  2. Anthropic 定义的工具   Anthropic     我们的电脑    files.py、memory.py
  3. 服务器工具             Anthropic     Anthropic     web.py

agent.py（把任务交给子 Agent）不在下面的 build_registry 里：它要知道"是谁在叫"（哪一方的界面、权限、第几层），
所以每一方各做一个，由 app.py 和 agents/runner.py 放进那一方的工具箱。
"""

from __future__ import annotations

from collections.abc import Iterator

from ..config import Config
from ..skills import SkillStore
from ..tasks import TaskStore
from .base import Tool, ToolError
from .files import FileEditor, make_file_tool
from .memory import make_memory_tool
from .shell import Shell, make_shell_tool
from .skill import make_skill_tool
from .tasks import make_task_tools
from .todo import TodoStore, make_todo_tools
from .web import make_web_tools

__all__ = ["Tool", "ToolError", "ToolRegistry", "build_registry"]


class ToolRegistry:
    def __init__(self, tools: list[Tool]):
        self._tools = {tool.key: tool for tool in tools}

    def specs(self) -> list[dict]:
        """所有工具的说明书，每次请求都会发给模型。"""
        return [tool.spec for tool in self._tools.values()]

    def get(self, name: str, toolset: str = "") -> Tool | None:
        """按名字找一个工具。toolset：模型调用的是哪个工具包的成员（见 Tool.toolset）；普通的工具是空的。"""
        return self._tools.get(f"{toolset}:{name}" if toolset else name)

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    def replaced(self, *tools: Tool) -> ToolRegistry:
        """一份新的工具箱：和这一份一样（顺序也一样），只是同名的几个换成传进来的。

        定时任务的每次运行、聊天通道里的每个聊天都用它换上自己的 shell（见 App.unattended_agent）：别的工具照旧共用，
        背后是同一份待办、任务、记忆和文件，各自有锁，几边同时用也不会互相冲掉。
        """
        swap = {tool.key: tool for tool in tools}
        return ToolRegistry([swap.get(tool.key, tool) for tool in self])


def build_registry(
    config: Config, files: FileEditor, todos: TodoStore, shell: Shell, tasks: TaskStore, skills: SkillStore
) -> ToolRegistry:
    tools = [
        *make_todo_tools(todos),
        *make_task_tools(tasks),
        make_memory_tool(config.data_dir),
        make_file_tool(files),
        make_shell_tool(shell),
        # 一直都在，哪怕一个 Skill 都还没装：工具说明书在一段对话里不能变，中途装上第一个 Skill 时才用得上它
        make_skill_tool(skills),
    ]
    if config.web:
        tools += make_web_tools(config.max_searches)
    return ToolRegistry(tools)
