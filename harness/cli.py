"""程序入口：读取配置，然后启动终端界面或网页界面。"""

from __future__ import annotations

import argparse
import sys

from .app import App, build_app, run_turn
from .config import Config, load_config
from .pricing import estimate
from .providers import has_key
from .tools.todo import format_todos
from .ui import TerminalUI

SETUP_HELP = """\
还没有设置 {label} 的 API key，助手需要它才能调用模型。有两种办法：

  1. 运行 uv run harness --web ，在网页界面右上角的"设置"里填写（最省事）。
  2. 打开 {env_file} ，加上一行：
         {key_env}=你的key

API key 在 {key_page} 申请。API 按用量计费，和聊天产品的订阅是分开的。
"""

HELP = """\
/help    显示这段说明
/new     开始一段新对话（记忆和待办不受影响）
/todos   查看未完成的待办
/tools   查看模型可以使用的工具
/usage   查看本次运行的 token 用量和估算费用
/model   查看当前使用的模型
/exit    退出"""

COMMANDS = {"/exit", "/quit", "/help", "/new", "/todos", "/tools", "/usage", "/model"}


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
        for name, local in app.agent.backend.tool_names():
            tool = app.tools.get(name)
            description = tool.spec.get("description") if tool else None
            # 自定义工具显示说明的第一句；Anthropic 定义的工具没有我们写的说明，显示它的类型
            summary = description.split("。")[0] + "。" if description else f"Anthropic 定义（{tool.spec['type']}）"
            ui.plain(f"{name}  [{'本地执行' if local else '服务器执行'}]")
            ui.info(f"    {summary}")
    elif command == "/usage":
        usage = app.agent.usage
        cost, unpriced = estimate(app.agent.usage_by_model, app.settings.prices)
        note = f"（不含 {'、'.join(unpriced)}：没有设置价格）" if unpriced else ""
        ui.plain(
            f"请求 {usage.requests} 次，联网搜索 {usage.searches} 次\n"
            f"输入：{usage.input_tokens} token（原价） + {usage.cache_read_tokens}（命中缓存） "
            f"+ {usage.cache_write_tokens}（写入缓存）\n"
            f"输出：{usage.output_tokens} token\n"
            f"估算费用：${cost:.4f}{note}"
        )
    elif command == "/model":
        ui.plain(f"{app.provider.label} · {app.settings.model}")
        ui.info("换模型：运行 uv run harness --web ，在网页界面的“设置”里选择。选择会保存下来，终端版也会跟着用。")
    return True


def run_terminal(config: Config) -> int:
    """终端界面的主循环：读一句、答一句。"""
    ui = TerminalUI(history_file=config.data_dir / "input_history")
    app = build_app(config, ui)
    provider = app.provider
    if not has_key(provider):
        print(
            SETUP_HELP.format(
                label=provider.label, env_file=config.env_file, key_env=provider.key_env, key_page=provider.key_page
            )
        )
        return 1
    ui.banner(f"{provider.label} · {app.settings.model}", config.workspace)

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
    if args.web:
        from .webui.server import run_web  # 用到时才导入，终端版不需要它

        return run_web(config, port=args.port, open_browser=not args.no_browser)
    return run_terminal(config)


if __name__ == "__main__":
    sys.exit(main())
