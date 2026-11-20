"""程序入口：把各个部件组装起来，然后进入"读一句、答一句"的主循环。"""

from __future__ import annotations

import sys

import anthropic

from .agent import Agent
from .config import Config, has_credentials, load_config
from .permissions import Permissions
from .prompt import build_system_prompt
from .tools import ToolRegistry, build_registry
from .tools.files import FileEditor
from .tools.todo import TodoStore, format_todos
from .ui import TerminalUI

SETUP_HELP = """\
还没有找到 API key，助手需要它才能调用 Claude。

  1. 打开 https://platform.claude.com ，登录后在 API Keys 页面创建一个 key
     （API 按用量计费，和 Claude 的订阅是分开的，需要先充值）。
  2. 在项目目录里把 .env.example 复制一份，命名为 .env
  3. 把 key 填在 ANTHROPIC_API_KEY= 的后面，保存，然后重新运行。

.env 文件的位置：{env_file}
"""

HELP = """\
/help    显示这段说明
/new     开始一段新对话（记忆和待办不受影响）
/todos   查看未完成的待办
/tools   查看模型可以使用的工具
/usage   查看本次运行的 token 用量
/exit    退出"""


def handle_command(command: str, agent: Agent, todos: TodoStore, tools: ToolRegistry, ui: TerminalUI) -> bool:
    """处理斜杠命令。返回 False 表示要退出程序。"""
    if command in ("/exit", "/quit"):
        return False
    if command == "/help":
        ui.plain(HELP)
    elif command == "/new":
        agent.reset()
        ui.info("已开始新对话。")
    elif command == "/todos":
        ui.plain(format_todos(todos.items("pending")))
    elif command == "/tools":
        for tool in tools:
            where = "本地执行" if tool.run else "服务器执行"
            description = tool.spec.get("description")
            # 自定义工具显示说明的第一句；Anthropic 定义的工具没有我们写的说明，显示它的类型
            summary = description.split("。")[0] + "。" if description else f"Anthropic 定义（{tool.spec['type']}）"
            ui.plain(f"{tool.name}  [{where}]")
            ui.info(f"    {summary}")
    elif command == "/usage":
        usage = agent.usage
        ui.plain(
            f"请求 {usage.requests} 次\n"
            f"输入：{usage.input_tokens} token（原价） + {usage.cache_read_tokens}（命中缓存） "
            f"+ {usage.cache_write_tokens}（写入缓存）\n"
            f"输出：{usage.output_tokens} token"
        )
    return True


COMMANDS = {"/exit", "/quit", "/help", "/new", "/todos", "/tools", "/usage"}


def run_turn(agent: Agent, ui: TerminalUI, text: str) -> None:
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


def build_agent(config: Config, ui: TerminalUI) -> tuple[Agent, TodoStore, ToolRegistry]:
    files = FileEditor(config.workspace, config.backup_dir, protected=[config.env_file])
    todos = TodoStore(config.todo_file)
    tools = build_registry(config, files, todos)
    agent = Agent(
        client=anthropic.Anthropic(),
        config=config,
        tools=tools,
        permissions=Permissions(files),
        ui=ui,
        system_prompt=build_system_prompt(config),
    )
    return agent, todos, tools


def main() -> int:
    config = load_config()
    if not has_credentials():
        print(SETUP_HELP.format(env_file=config.env_file))
        return 1

    ui = TerminalUI(history_file=config.data_dir / "input_history")
    agent, todos, tools = build_agent(config, ui)
    ui.banner(config)

    while True:
        text = ui.read_input()
        if text is None:
            break
        text = text.strip()
        if not text:
            continue
        if text in COMMANDS:
            if not handle_command(text, agent, todos, tools, ui):
                break
            continue
        run_turn(agent, ui, text)

    ui.info("再见。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
