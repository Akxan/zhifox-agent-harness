"""程序入口：读取配置，然后启动终端界面、网页界面，或者在服务器上作为服务运行。

    harness                       终端界面
    harness --web                 网页界面（在浏览器里打开）
    harness serve [--web]         服务器上的服务：定时任务和聊天通道，没有浏览器（见 serve.py）
    harness channels ...          设置聊天通道（见 channels/cli.py）
    harness commands ...          你自己加的"不用问的命令"（见 commands/cli.py）
    harness hooks ...             你的钩子：在固定的时刻运行你配置的命令（见 hooks/cli.py）
    harness sandbox ...           命令的沙箱：状态、哪一档、额外允许的路径、试一下（见 sandbox/cli.py）
    harness service ...           装成后台服务：登录时自动启动、出错退出后自己重启（见 service/）
    harness eval ...              评测：让智狐做一组真实的小事并打分（默认是不花钱的演习，见 evals/）
    harness doctor [--json]       自检：一屏看清智狐在这台电脑上的情况（不联网，不改任何东西，见 doctor/）
    harness restore 备份.zip       从备份恢复：先看预览，输入 yes 才恢复（见 restore_cli.py）
    harness updates [on|off|check] 检查新版本（默认关着）：看一眼、打开、关上、马上问一次 GitHub（见 updates_cli.py）

--workspace 指定工作目录（助手读写文件、执行命令的地方），不给的话是现在所在的目录。
"""

from __future__ import annotations

import argparse
import dataclasses
import signal
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from . import (
    capabilities,
    console,
    i18n,
    instance,
    model_catalog,
    restore_cli,
    updates_cli,
    version,
)
from .app import App, build_app
from .channels import cli as channels_cli
from .commands import cli as commands_cli
from .config import APP_NAME, Config, load_config
from .doctor import cli as doctor_cli
from .doctor import examine as doctor_examine
from .doctor import text as doctor_text
from .evals import cli as eval_cli
from .exits import EXIT_SIGNALS
from .hooks import cli as hooks_cli
from .i18n import k, t
from .permissions import MODES
from .pricing import estimate
from .providers import has_key
from .sandbox import cli as sandbox_cli
from .service import cli as service_cli
from .settings import Settings
from .tools.todo import format_todos
from .trace import text as trace_text
from .updates import Updater, news_line

# 终端界面（prompt_toolkit，连带 asyncio，导入要几十毫秒）只有 run_terminal 用，在那里用到时才导入：
# harness service status、harness hooks list 这些小命令和网页界面都用不到它。这里只给类型标注用
if TYPE_CHECKING:
    from .chat_cli import TerminalChat
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
/new     开始一段新对话（记忆和待办不受影响；这一段已经存好了，/open 能再打开）
/history 列出最近的会话（网页上聊的也在里面）；/history 加上几个词，是在所有会话里搜索
/open    打开一段会话接着聊：/open 编号（/history 列出来的），或者 /open 会话的编号
/todos   查看未完成的待办
/tools   查看智狐能做的事：哪些在用、会不会先问你、关着的怎么打开
/trace   查看最后一轮的运行记录：调用了几次模型、用了哪些工具、每一步花了多久、多少钱
/usage   查看本次运行的 token 用量和估算费用
/model   查看各家的模型：现在用的是哪个、能做什么、多少钱、哪一家还没有密钥
/mcp     查看接入的 MCP 服务器（添加和管理在网页界面的"扩展"里）
/auto    查看或更改"要不要每次都问我"：/auto ask 每次都问，/auto readonly 只读的命令不用问，/auto edits 改工作目录里的文件不用问，/auto all 全部自动允许
/retry   重新生成最后一个回答（只去掉这一轮里模型写的、做的，你的消息原样再发一次）
/edit    改最后一条消息再发：上一条会填在输入行里，改完按回车；也可以直接写 /edit 新的文字
/undo    撤回最近一轮改的文件（文件工具做的改动；命令改动的文件没法自动撤回），先列出来问你
/doctor  自检：Python、目录的权限、哪几家有密钥、浏览器、沙箱、后台服务……哪些好、哪些有问题、怎么修
/exit    退出""")

# 只是看看、改改设置的命令（handle_command 处理）。动到这段对话本身的（/new、/history、/open、/retry、/edit、/undo、/exit）
# 由 chat_cli.py 的 TerminalChat 处理，见 run_terminal
COMMANDS = {"/help", "/todos", "/tools", "/trace", "/usage", "/model", "/mcp", "/auto", "/doctor"}

# MCP 服务器的几种状态，给人看的说法
MCP_STATUS = {"connecting": k("连接中"), "ready": k("已连接"), "failed": k("连接失败"), "disabled": k("已停用")}

# /auto 换成每种模式之后各说一句话，让用户清楚自己放开了什么
AUTO_NOTES = {
    "ask": k("已改成：每次都问。助手要改文件、执行命令之前，都会先请你确认。"),
    "readonly": k(
        "已改成：只读的命令不用问。ls、cat、grep、df、git status 这类只是看的命令，以及读工作目录之外的文件，不再问你；"
        "会改动东西的命令、改文件、读密钥和助手自己的数据，照常先问。你自己加的不用问的命令见 harness commands list。"
    ),
    "edits": k("已改成：创建和修改工作目录里的文件不再问你。执行命令、读写工作目录之外的文件，照常先问。"),
    "all": k("已改成：全部自动允许。助手改文件、执行命令（打开了操作整台电脑的话，还有点击、打字）都不会再问你（保存密钥的 .env 仍然碰不了，危险的组合键照样问）。"),
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
        ui.info(t("更改：/auto ask 每次都问；/auto readonly 只读的命令不用问；/auto edits 改工作目录里的文件不用问；/auto all 全部自动允许。"))
    elif argument not in MODES:
        ui.error(t("没有 {mode} 这种模式。可以用的是：{modes}。", mode=argument, modes=t("、").join(MODES)))
    else:
        permissions.mode = argument
        ui.plain(t(AUTO_NOTES[argument]))
        if argument == "all":
            ui.error(t(AUTO_RISK))


# "智狐能做的事"里每一项的状态，在终端里的说法（网页上的样子见 page/scripts/capabilities.js）
CAPABILITY_STATUS = {"on": k("在用"), "pending": k("下一段对话开始生效"), "off": k("关着"), "unavailable": k("用不了")}
CAPABILITY_MARKS = {"on": "●", "pending": "◐", "off": "○", "unavailable": "–"}


def show_capabilities(app: App, ui: TerminalUI) -> None:
    """/tools：智狐能做的事，和网页信息栏里的同一份清单（见 capabilities.py）。在用的写会不会先问你，别的写为什么。"""
    found = capabilities.overview(app)
    ui.plain(t("智狐能做的事（现在的确认方式：{mode}）", mode=t(MODES[found.mode])))
    for note in found.notes:
        ui.info(note)
    for item in found.items:
        count = f" · {item.count}" if item.count else ""
        ui.plain(f"{CAPABILITY_MARKS[item.status]} {item.label}{count}  [{t(CAPABILITY_STATUS[item.status])}]")
        ui.info(f"    {item.description}")
        if item.status == "on" and item.approval is not None:
            ui.info(f"    {item.approval.summary}")
        if item.reason:
            ui.info(f"    {item.reason}")
        if item.tools:
            ui.info("    " + t("技术细节：{names}", names=", ".join(item.tools)))
    ui.info(t("浏览器、操作整台电脑、Skill、MCP、Agent 在网页界面（uv run harness --web）的“设置”和“扩展”里打开、管理。"))


def handle_command(command: str, app: App, ui: TerminalUI) -> bool:
    """处理只是看看、改改设置的斜杠命令（COMMANDS）。返回 True（不退出：退出是 /exit，见 run_terminal）。"""
    name, _, argument = command.partition(" ")
    if name == "/auto":
        set_auto(argument.strip(), app, ui)
        return True
    if command == "/help":
        ui.plain(t(HELP))
    elif command == "/todos":
        ui.plain(format_todos(app.todos.items("pending")))
    elif command == "/tools":
        show_capabilities(app, ui)
    elif command == "/trace":
        # 最后一轮的运行记录（见 trace/）：和网页上"运行记录"那个窗口是同一份数，这里写成字
        for text, style in trace_text.lines(ui.last_trace):
            (ui.plain if style == "plain" else ui.info)(text)
    elif command == "/usage":
        usage = app.agent.usage
        cost, unpriced = estimate(app.agent.usage_by_model, app.prices())
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
        # 账本里的数（见 ledger.py）：不随重启清零，网页界面里花的、定时任务花的也都在里面
        spend = app.spending()
        ui.plain(t("今天 ${today}，本月 ${month}", today=f"{spend['today']:.4f}", month=f"{spend['month']:.4f}"))
        if spend["budget"]:
            ui.plain(t("每月预算 ${budget}", budget=f"{spend['budget']:.2f}") + (t("（已超出）") if spend["over"] else ""))
    elif command == "/mcp":
        servers = app.mcp.status(started=False)
        if not servers:
            ui.info(t("还没有接入 MCP 服务器。在网页界面（uv run harness --web）的“扩展 → MCP”里添加，或者编辑 {path}。", path=app.config.mcp_file))
        for server in servers:
            ui.plain(f"{server['name']}  [{t(MCP_STATUS.get(server['status'], server['status']))}]  " + t("{n} 个工具", n=server["tools"]))
            if server["error"]:
                ui.info(f"    {server['error']}")
        if servers:
            ui.info(t("新连上的服务器从下一段新对话（/new）开始可以用。"))
    elif command == "/model":
        # 和网页上换模型的面板是同一份目录（见 model_catalog.py）：每一家有没有密钥、能选的模型、能做什么、价格
        for text, style in model_catalog.lines(model_catalog.catalog(app)):
            (ui.plain if style == "plain" else ui.info)(text)
    elif command == "/doctor":
        # 和 harness doctor 是同一份自检（见 doctor/）：有问题的那几行是红的
        for text, style in doctor_text.lines(doctor_examine(app.config)):
            {"plain": ui.plain, "info": ui.info, "error": ui.error}[style](text)
    return True


def run_terminal(config: Config) -> int:
    """终端界面：占住数据目录（占到了才存会话，见 chat_cli.py 开头），然后读一句、答一句。"""
    from .ui import TerminalUI  # 用到时才导入（见文件开头）

    # 同一个数据目录同一时间只给一个智狐用（instance.py）：占到了，这段对话每一轮存盘、能打开保存过的会话；
    # 网页界面、后台服务开着的时候占不到，照常能聊，只是不存（启动时说清楚是谁占着）
    lock = instance.claim(config.data_dir, "terminal")
    try:
        return _chat(config, TerminalUI(history_file=config.data_dir / "input_history"), saving=lock is not None)
    finally:
        if lock is not None:
            lock.close()


def _chat(config: Config, ui: TerminalUI, saving: bool) -> int:
    from .chat_cli import TerminalChat, busy_lines  # 用到时才导入（和 TerminalUI 一样）

    app = build_app(config, ui)
    provider = app.provider
    if not has_key(provider):
        print(t(SETUP_HELP, label=provider.label, env_file=config.env_file, key_env=provider.key_env, key_page=provider.key_page))
        app.close()
        return 1
    ui.banner(f"{provider.label} · {app.settings.model}", config.workspace)
    # 检查新版本（默认关着，见 updates.py）：启动时只看上一次的结果，有新版本就说一行。打开着、上一次又是一天以前，
    # 就在后台问一次（不耽误你打字），问到了在下一次输入之前说
    updater = Updater(config, app.settings)
    known = updater.status().newer
    if known is not None:
        ui.info(news_line(known, updater.current))
    if updater.due():
        updater.check_soon()
    chat = TerminalChat(app, ui, saving)
    if not saving:
        first, *rest = busy_lines(config.data_dir)
        ui.error(first)
        for line in rest:
            ui.info(line)

    # 普通的 kill（SIGTERM：关机、注销、systemctl、别的程序叫它退出时发的就是它）和 Windows 上的 Ctrl+Break（见 exits.py）
    # 不该直接把它杀掉：正在进行的一轮当作按了 Ctrl-C 停下（agent.py 收尾，工具调用照样有结果），存好盘再退出。
    # 信号处理只能在主线程里接；收到第一个之后换回默认的处理：真想马上结束，再发一次就行
    stopping = threading.Event()

    def interrupted(signum: int, frame: object) -> None:
        stopping.set()
        for number in EXIT_SIGNALS:
            signal.signal(number, signal.SIG_DFL)
        raise KeyboardInterrupt

    main_thread = threading.current_thread() is threading.main_thread()
    previous = {number: signal.signal(number, interrupted) for number in EXIT_SIGNALS} if main_thread else {}
    try:
        while not stopping.is_set():
            found = updater.take_news()  # 后台问到了新版本：在这一次输入之前说（见上面的 check_soon）
            if found is not None:
                ui.info(news_line(found, updater.current))
            try:
                text = ui.read_input()
            except KeyboardInterrupt:
                continue  # 读输入时收到了 SIGTERM（真的终端里 read_input 自己接住 Ctrl-C，换成空的一行）
            if text is None:  # Ctrl-D：和 /exit 一样，没存上的先再存一次
                if chat.leave("/exit"):
                    break
                continue
            text = text.strip()
            if not text:
                continue
            try:
                if not _dispatch(text, app, ui, chat):
                    break
            except KeyboardInterrupt:
                # 一轮对话里的 Ctrl-C 由 app.run_turn 接住；这里接的是别处（列出会话、存盘……）被打断的
                if not stopping.is_set():
                    ui.info(t("已中断。"))
    finally:
        for number in previous:
            signal.signal(number, signal.SIG_IGN)  # 正在收尾：这时再来一个 SIGTERM 不要打断存盘
        try:
            chat.close()  # 还有没存上的（被 SIGTERM 打断的那一轮、上一次没存上的），最后再存一次
        finally:
            app.close()  # 关掉启动过的 MCP 服务器
            for number, handler in previous.items():
                signal.signal(number, handler)

    ui.info(t("再见。"))
    return 0


def _dispatch(text: str, app: App, ui: TerminalUI, chat: TerminalChat) -> bool:
    """处理输入的一行。返回 False 表示要退出。"""
    name, _, argument = text.partition(" ")
    argument = argument.strip()
    if text == "/retry" or name == "/edit":  # /edit 可以带上新的文字
        chat.rewind(name, argument)
    elif text == "/undo":
        chat.undo()
    elif text == "/new":
        chat.new()
    elif name == "/history":  # 可以带上要搜的词
        chat.history(argument)
    elif name == "/open":
        chat.open(argument)
    elif text in ("/exit", "/quit"):
        return not chat.leave("/exit")
    elif text in COMMANDS or name == "/auto":  # /auto 可以带参数
        return handle_command(text, app, ui)
    else:
        chat.ask(text)
    return True


def main(argv: list[str] | None = None) -> int:
    console.use_utf8()  # Windows 上输出接到管道或者文件时也用 UTF-8，打印中文不会出错（见 console.py）
    config = load_config()
    # 先按设置文件定下界面语言，下面的命令行说明（--help）才是这种语言
    i18n.choose(Settings(config.settings_file, config.default_provider, config.default_model).language)
    parser = argparse.ArgumentParser(prog="harness", description=t("{name}：运行在你自己电脑上的个人助手", name=APP_NAME))
    parser.add_argument("--web", action="store_true", help=t("使用浏览器里的图形界面，而不是终端界面"))
    parser.add_argument("--port", type=int, default=None, help=t("网页界面使用的端口（默认 8765，被占用时自动换一个）"))
    parser.add_argument("--no-browser", action="store_true", help=t("启动网页界面时不自动打开浏览器"))
    parser.add_argument("--workspace", type=Path, default=None, help=t("工作目录：助手读写文件、执行命令的地方（默认是现在所在的目录）"))
    # 版本号只在 harness/version.py 里算：装成工具时是 0.2.0，从仓库里运行时还带着提交（那时几十个提交都叫 0.2.0）
    parser.add_argument("--version", action="version", version=version.shown(), help=t("显示智狐的版本，然后退出"))
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    serve = commands.add_parser("serve", help=t("在服务器上一直运行：定时任务和聊天通道，不开浏览器"))
    serve.add_argument("--web", dest="serve_web", action="store_true", help=t("同时开着网页界面（只监听 127.0.0.1，用 SSH 隧道访问）"))
    serve.add_argument("--port", dest="serve_port", type=int, default=None, help=t("网页界面使用的端口（默认 8765，被占用时自动换一个）"))
    serve.add_argument("--service", dest="serve_service", choices=("launchd", "systemd", "taskscheduler"), default=None, help=t("作为后台服务运行（harness service install 写进服务的设置里，不用自己加）"))
    channels_cli.add_parser(commands)
    commands_cli.add_parser(commands)
    hooks_cli.add_parser(commands)
    sandbox_cli.add_parser(commands)
    service_cli.add_parser(commands)
    eval_cli.add_parser(commands)
    doctor_cli.add_parser(commands)
    restore_cli.add_parser(commands)
    updates_cli.add_parser(commands)
    # "harness hooks add 名字 --event … -- 程序 参数"：-- 后面的原样交给那个命令。自己先在第一个 -- 处切开，再交给 argparse：
    # Python 3.11 的 argparse 不认子命令里一个位置参数后面跟着的 --（3.12 起才认），钩子的命令在 3.11 上就加不进去（GitHub 的测试抓到的）
    argv = list(sys.argv[1:] if argv is None else argv)
    after: list[str] | None = None
    if "--" in argv:
        cut = argv.index("--")
        argv, after = argv[:cut], argv[cut + 1 :]
    args = parser.parse_args(argv)
    args.after_dashes = after
    if after is not None and not (args.command == "hooks" and getattr(args, "hooks_action", None) == "add"):
        parser.error(t("只有 harness hooks add 认 -- 后面的内容"))
    if args.workspace is not None:
        workspace = args.workspace.expanduser().resolve()
        if not workspace.is_dir():
            print(t("工作目录不存在：{path}", path=workspace), file=sys.stderr)
            return 2
        config = dataclasses.replace(config, workspace=workspace)
    if args.command == "serve":
        from .serve import run_serve  # 用到时才导入

        return run_serve(config, web=args.serve_web, port=args.serve_port, service=args.serve_service)
    if args.command == "channels":
        return channels_cli.run(args, config)
    if args.command == "commands":
        return commands_cli.run(args, config)
    if args.command == "hooks":
        return hooks_cli.run(args, config)
    if args.command == "sandbox":
        return sandbox_cli.run(args, config)
    if args.command == "service":
        return service_cli.run(args, config)
    if args.command == "eval":
        return eval_cli.run(args, config)
    if args.command == "doctor":
        return doctor_cli.run(args, config)
    if args.command == "restore":
        return restore_cli.run(args, config)
    if args.command == "updates":
        return updates_cli.run(args, config)
    if args.web:
        from .webui.server import run_web  # 用到时才导入，终端版不需要它

        return run_web(config, port=args.port, open_browser=not args.no_browser)
    return run_terminal(config)


if __name__ == "__main__":
    sys.exit(main())
