"""程序入口：读取配置，然后启动终端界面或网页界面。"""

from __future__ import annotations

import argparse
import sys

from .app import App, build_app, run_turn
from .config import has_credentials, load_config
from .tools.todo import format_todos
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

COMMANDS = {"/exit", "/quit", "/help", "/new", "/todos", "/tools", "/usage"}


def handle_command(command: str, app: App, ui: TerminalUI) -> bool:
    """处理斜杠命令。返回 False 表示要退出程序。"""
    if command in ("/exit", "/quit"):
        return False
    if command == "/help":
        ui.plain(HELP)
    elif command == "/new":
        app.agent.reset()
        ui.info("已开始新对话。")
    elif command == "/todos":
        ui.plain(format_todos(app.todos.items("pending")))
    elif command == "/tools":
        for tool in app.tools:
            where = "本地执行" if tool.run else "服务器执行"
            description = tool.spec.get("description")
            # 自定义工具显示说明的第一句；Anthropic 定义的工具没有我们写的说明，显示它的类型
            summary = description.split("。")[0] + "。" if description else f"Anthropic 定义（{tool.spec['type']}）"
            ui.plain(f"{tool.name}  [{where}]")
            ui.info(f"    {summary}")
    elif command == "/usage":
        usage = app.agent.usage
        ui.plain(
            f"请求 {usage.requests} 次\n"
            f"输入：{usage.input_tokens} token（原价） + {usage.cache_read_tokens}（命中缓存） "
            f"+ {usage.cache_write_tokens}（写入缓存）\n"
            f"输出：{usage.output_tokens} token"
        )
    return True


def run_terminal(config) -> int:
    """终端界面的主循环：读一句、答一句。"""
    ui = TerminalUI(history_file=config.data_dir / "input_history")
    app = build_app(config, ui)
    ui.banner(config)

    while True:
        text = ui.read_input()
        if text is None:
            break
        text = text.strip()
        if not text:
            continue
        if text in COMMANDS:
            if not handle_command(text, app, ui):
                break
            continue
        run_turn(app.agent, ui, text)

    ui.info("再见。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harness", description="Harness 个人助手")
    parser.add_argument("--web", action="store_true", help="使用浏览器里的图形界面，而不是终端界面")
    parser.add_argument("--port", type=int, default=None, help="网页界面使用的端口（默认 8765，被占用时自动换一个）")
    parser.add_argument("--no-browser", action="store_true", help="启动网页界面时不自动打开浏览器")
    args = parser.parse_args(argv)

    config = load_config()
    if not has_credentials():
        print(SETUP_HELP.format(env_file=config.env_file))
        return 1

    if args.web:
        from .webui.server import run_web  # 用到时才导入，终端版不需要它

        return run_web(config, port=args.port, open_browser=not args.no_browser)
    return run_terminal(config)


if __name__ == "__main__":
    sys.exit(main())
