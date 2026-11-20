"""组装：把配置、工具、权限和循环拼成一个可以运行的助手。

终端版和网页版从这里拿到的是同一套东西，区别只在于传进来的界面（ui）不同。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import anthropic

from .agent import Agent
from .config import Config
from .permissions import Permissions
from .prompt import build_system_prompt
from .tools import ToolRegistry, build_registry
from .tools.files import FileEditor
from .tools.shell import Shell
from .tools.todo import TodoStore


@dataclass
class App:
    config: Config
    agent: Agent
    todos: TodoStore
    tools: ToolRegistry
    shell: Shell


def build_app(config: Config, ui: Any, client: Any = None) -> App:
    files = FileEditor(config.workspace, config.backup_dir, protected=[config.env_file])
    todos = TodoStore(config.todo_file)
    shell = Shell(config.workspace)
    tools = build_registry(config, files, todos, shell)
    agent = Agent(
        client=client or anthropic.Anthropic(),
        config=config,
        tools=tools,
        permissions=Permissions(files),
        ui=ui,
        system_prompt=build_system_prompt(config),
    )
    return App(config=config, agent=agent, todos=todos, tools=tools, shell=shell)


def run_turn(agent: Agent, ui: Any, text: str) -> None:
    """跑一轮对话，并把各种 API 错误翻译成看得懂的提示。出错后程序不退出，可以接着聊。"""
    try:
        agent.run_turn(text)
    except KeyboardInterrupt:
        ui.info("已中断。")
    except anthropic.AuthenticationError:
        ui.error("API key 无效。请检查 .env 文件里的 ANTHROPIC_API_KEY。")
    except anthropic.PermissionDeniedError as exc:
        ui.error(f"这个 API key 没有权限执行该请求：{exc.message}")
    except anthropic.NotFoundError:
        ui.error(f"找不到模型 {agent.config.model}。请检查 HARNESS_MODEL 的设置。")
    except anthropic.RateLimitError:
        ui.error("请求太频繁，或者超出了账号的用量限制。请稍后再试。")
    except anthropic.BadRequestError as exc:
        ui.error(f"请求被 API 拒绝：{exc.message}")
    except anthropic.APIStatusError as exc:
        ui.error(f"API 出错（状态码 {exc.status_code}）：{exc.message}")
    except anthropic.APIConnectionError:
        ui.error("连接不上 Anthropic 的服务器，请检查网络。")
