"""程序入口：读取配置，然后启动终端界面或网页界面。"""

from __future__ import annotations

import argparse
import sys

from . import i18n
from .app import App, build_app, run_turn
from .config import APP_NAME, Config, load_config
from .i18n import k, t
from .permissions import MODES
from .pricing import estimate
from .providers import has_key
from .settings import Settings
from .tools.todo import format_todos
from .ui import TerminalUI

SETUP_HELP = k("""\
还没有设置 {label} 的 API 密钥，助手需要它才能调用模型。有两种办法：

  1. 运行 uv run harness --web ，在网页界面右上角的"设置"里填写（最省事）。
  2. 打开 {env_file} ，加上一行：
         {key_env}=你的key

API 密钥在 {key_page} 申请。API 按用量计费，和聊天产品的订阅是分开的。
""")

HELP = k("""\
/help    显示这段说明
/new     开始一段新对话（记忆和待办不受影响）
/todos   查看未完成的待办
/tools   查看模型可以使用的工具
/usage   查看本次运行的 token 用量和估算费用
/model   查看当前使用的模型
/auto    查看或更改"要不要每次都问我"：/auto ask 每次都问，/auto edits 改工作目录里的文件不用问，/auto all 全部自动允许
/exit    退出""")

COMMANDS = {"/exit", "/quit", "/help", "/new", "/todos", "/tools", "/usage", "/model", "/auto"}

# /auto 换成每种模式之后各说一句话，让用户清楚自己放开了什么
AUTO_NOTES = {
    "ask": k("已改成：每次都问。助手要改文件、执行命令之前，都会先请你确认。"),
    "edits": k("已改成：创建和修改工作目录里的文件不再问你。执行命令、读写工作目录之外的文件，照常先问。"),
    "all": k("已改成：全部自动允许。助手改文件、执行命令都不会再问你（保存密钥的 .env 仍然碰不了）。"),
}
AUTO_RISK = k(
    "注意：确认是你和模型之间唯一的一道关卡，现在它关掉了。网页或文件里如果藏着误导模型的话，"
    "它照做的时候不会有人拦着。用完请输入 /auto ask 关掉；这个设置不会保存，下次启动会回到每次都问。"
)


def set_auto(argument: str, app: App, ui: TerminalUI) -> None:
    """/auto：查看或更改"哪些操作不用问就可以做"。"""
    permissions = app.agent.permissions
    if not argument:
        ui.plain(t("现在是：{label}（{mode}）", label=t(MODES[permissions.mode]), mode=permissions.mode))
        ui.info(t("更改：/auto ask 每次都问；/auto edits 改工作目录里的文件不用问；/auto all 全部自动允许。"))
    elif argument not in MODES:
        ui.error(t("没有 {mode} 这种模式。可以用的是：{modes}。", mode=argument, modes=t("、").join(MODES)))
    else:
        permissions.mode = argument
        ui.plain(t(AUTO_NOTES[argument]))
        if argument == "all":
            ui.error(t(AUTO_RISK))


def handle_command(command: str, app: App, ui: TerminalUI) -> bool:
    """处理斜杠命令。返回 False 表示要退出程序。"""
    name, _, argument = command.partition(" ")
    if name == "/auto":
        set_auto(argument.strip(), app, ui)
        return True
    if command in ("/exit", "/quit"):
        return False
    if command == "/help":
        ui.plain(t(HELP))
    elif command == "/new":
        app.agent.reset()
        ui.info(t("已开始新对话。"))
    elif command == "/todos":
        ui.plain(format_todos(app.todos.items("pending")))
    elif command == "/tools":
        for name, local in app.agent.backend.tool_names():
            tool = app.tools.get(name)
            description = tool.spec.get("description") if tool else None
            # 自定义工具显示说明的第一句；Anthropic 定义的工具没有我们写的说明，显示它的类型
            summary = description.split("。")[0] + "。" if description else t("Anthropic 定义（{type}）", type=tool.spec["type"])
            ui.plain(f"{name}  [{t('本地执行') if local else t('服务器执行')}]")
            ui.info(f"    {summary}")
    elif command == "/usage":
        usage = app.agent.usage
        cost, unpriced = estimate(app.agent.usage_by_model, app.settings.prices)
        note = t("（不含 {models}：没有设置价格）", models=t("、").join(unpriced)) if unpriced else ""
        ui.plain(
            t(
                "请求 {requests} 次，联网搜索 {searches} 次\n"
                "输入：{input} token（原价） + {cache_read}（命中缓存） + {cache_write}（写入缓存）\n"
                "输出：{output} token\n"
                "估算费用：${cost}{note}",
                requests=usage.requests,
                searches=usage.searches,
                input=usage.input_tokens,
                cache_read=usage.cache_read_tokens,
                cache_write=usage.cache_write_tokens,
                output=usage.output_tokens,
                cost=f"{cost:.4f}",
                note=note,
            )
        )
    elif command == "/model":
        ui.plain(f"{app.provider.label} · {app.settings.model}")
        ui.info(t("换模型：运行 uv run harness --web ，在网页界面的“设置”里选择。选择会保存下来，终端版也会跟着用。"))
    return True


def run_terminal(config: Config) -> int:
    """终端界面的主循环：读一句、答一句。"""
    ui = TerminalUI(history_file=config.data_dir / "input_history")
    app = build_app(config, ui)
    provider = app.provider
    if not has_key(provider):
        print(t(SETUP_HELP, label=provider.label, env_file=config.env_file, key_env=provider.key_env, key_page=provider.key_page))
        return 1
    ui.banner(f"{provider.label} · {app.settings.model}", config.workspace)

    while True:
        text = ui.read_input()
        if text is None:
            break
        text = text.strip()
        if not text:
            continue
        if text in COMMANDS or text.startswith("/auto "):  # 只有 /auto 可以带参数
            if not handle_command(text, app, ui):
                break
            continue
        run_turn(app.agent, ui, text)

    ui.info(t("再见。"))
    return 0


def main(argv: list[str] | None = None) -> int:
    config = load_config()
    # 先按设置文件定下界面语言，下面的命令行说明（--help）才是这种语言
    i18n.choose(Settings(config.settings_file, config.default_provider, config.default_model).language)
    parser = argparse.ArgumentParser(prog="harness", description=t("{name}：运行在你自己电脑上的个人助手", name=APP_NAME))
    parser.add_argument("--web", action="store_true", help=t("使用浏览器里的图形界面，而不是终端界面"))
    parser.add_argument("--port", type=int, default=None, help=t("网页界面使用的端口（默认 8765，被占用时自动换一个）"))
    parser.add_argument("--no-browser", action="store_true", help=t("启动网页界面时不自动打开浏览器"))
    args = parser.parse_args(argv)
    if args.web:
        from .webui.server import run_web  # 用到时才导入，终端版不需要它

        return run_web(config, port=args.port, open_browser=not args.no_browser)
    return run_terminal(config)


if __name__ == "__main__":
    sys.exit(main())
