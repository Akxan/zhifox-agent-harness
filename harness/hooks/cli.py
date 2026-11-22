"""命令行里管你的钩子：在没有浏览器的服务器上，用这几条代替网页上的"扩展 → 钩子"。

    harness hooks list                                   列出来
    harness hooks add no-rm --event PreToolUse --matcher shell -- python3 /path/no_rm.py
                                                         加一个（-- 后面是程序和它的参数，一项一个，不经过 shell）
    harness hooks enable no-rm / disable no-rm           打开、关掉
    harness hooks remove no-rm                           删掉
    harness hooks test no-rm                             用示例试运行一次，看它会让智狐怎么办

和网页用的是同一个文件（data/hooks.json）、同一套检查（见 config.py）。智狐开着的时候也能用：它下一次到了那个时刻就按新的来。
"""

from __future__ import annotations

import argparse
import sys
from typing import IO

from ..config import Config
from ..i18n import t
from .config import EVENTS, FAILURES, SOURCES, HookConfig, HookError
from .engine import Hooks


def add_parser(commands: argparse._SubParsersAction) -> None:
    """在 harness 的命令行里加上 hooks 这一组命令。"""
    parser = commands.add_parser("hooks", help=t("你的钩子（在固定的时刻运行你配置的命令）：列出、添加、开关、删除、试运行"))
    actions = parser.add_subparsers(dest="hooks_action", required=True, metavar="ACTION")
    actions.add_parser("list", help=t("列出来"))
    add = actions.add_parser("add", help=t("加一个钩子：-- 后面写程序和它的参数（一项一个，不经过 shell）"))
    add.add_argument("name", metavar="NAME", help=t("名字：字母、数字、下划线、点和连字符"))
    add.add_argument("--event", required=True, choices=EVENTS, help=t("什么时候运行"))
    add.add_argument("--matcher", default="", help=t("只对哪些工具（PreToolUse、PostToolUse）：功能的名字（shell、files……）或工具的名字，用 | 隔开；不写是全部"))
    add.add_argument("--timeout", type=float, default=None, help=t("最多等几秒"))
    add.add_argument("--on-failure", dest="on_failure", choices=FAILURES, default="continue", help=t("钩子自己出错时：continue 照常继续，block 当成拦下"))
    add.add_argument("--runs-in", dest="runs_in", default=",".join(SOURCES), help=t("在哪些运行里生效：chat、task、channel，用逗号隔开"))
    add.add_argument("--disabled", action="store_true", help=t("先不打开"))
    # 程序和参数写在 -- 后面：那之后的一切（包括 -c 这种像选项的）都原样当成参数，不会被当成 harness 自己的选项
    add.add_argument("program", nargs="*", metavar="-- PROGRAM ARGS", help=t("要运行的程序和参数"))
    for action, text in (("enable", t("打开一个")), ("disable", t("关掉一个")), ("remove", t("删掉一个")), ("test", t("用示例试运行一次"))):
        actions.add_parser(action, help=text).add_argument("name", metavar="NAME")


def run(args: argparse.Namespace, config: Config, out: IO[str] | None = None) -> int:
    out = out or sys.stdout
    store = HookConfig(config.hooks_file)
    action = args.hooks_action
    try:
        if action == "list":
            return _list(store, out)
        if action == "add":
            program = args.program[1:] if args.program[:1] == ["--"] else args.program  # -- 本身不是程序的一部分
            if not program:
                print(t("还没写要运行的程序：在 -- 后面写程序和参数，比如 -- python3 check.py"), file=sys.stderr)
                return 2
            entry = {
                "event": args.event,
                "matcher": args.matcher,
                "command": program[0],
                "args": program[1:],
                "enabled": not args.disabled,
                "onFailure": args.on_failure,
                "runsIn": [source.strip() for source in args.runs_in.split(",") if source.strip()],
            }
            if args.timeout is not None:
                entry["timeout"] = args.timeout
            store.put(args.name, entry)
            print(t("加好了：钩子“{name}”（{event}）。它会在你的电脑上运行这个命令：{command}", name=args.name, event=args.event, command=" ".join(program)), file=out)
            return 0
        if action in ("enable", "disable"):
            store.set_enabled(args.name, action == "enable")
            print(t("已打开钩子“{name}”。", name=args.name) if action == "enable" else t("已关掉钩子“{name}”。", name=args.name), file=out)
            return 0
        if action == "remove":
            store.remove(args.name)
            print(t("删掉了：{entry}", entry=args.name), file=out)
            return 0
        if action == "test":
            return _test(store, config, args.name, out)
    except HookError as exc:
        print(exc.args[0] if exc.args else str(exc), file=sys.stderr)
        return 1
    return 2


def _list(store: HookConfig, out: IO[str]) -> int:
    if store.problem:
        print(store.problem, file=sys.stderr)
    hooks = store.hooks()
    if not hooks:
        print(t("还没有钩子。用 harness hooks add 加一个，或者在网页上的“扩展 → 钩子”里加。"), file=out)
    for name, (entry, problem) in hooks.items():
        if entry is None:
            print(f"  ✗ {name}：{problem}", file=out)
            continue
        mark = "●" if entry["enabled"] else "○"
        where = f" [{entry['matcher']}]" if entry.get("matcher") not in (None, "", "*") else ""
        print(f"  {mark} {name}  {entry['event']}{where}  {' '.join([entry['command'], *entry['args']])}", file=out)
    print(t("配置在：{path}。钩子会在你的电脑上运行这些命令，只加你信得过的。", path=store.path), file=out)
    return 0


def _test(store: HookConfig, config: Config, name: str, out: IO[str]) -> int:
    entry = store.get(name)
    if entry is None:
        print(store.hooks().get(name, (None, t("没有找到钩子“{name}”，它可能已经被删掉了。", name=name)))[1], file=sys.stderr)
        return 1
    # 和它在你的对话里跑的时候一样：设置里的沙箱开着就关在里面（见 runner.jailed）
    from ..app import sandbox_layout  # 用到时才导入：app 要导入大半个程序
    from ..sandbox import Sandbox
    from ..settings import Settings

    settings = Settings(config.settings_file, config.default_provider, config.default_model)
    run = Hooks(store, config.workspace).test(name, entry, sandbox=Sandbox(sandbox_layout(config, settings), escape=lambda: False))
    print(t("退出码：{code}，用了 {ms} 毫秒", code="—" if run["exit"] is None else run["exit"], ms=run["ms"]), file=out)
    for label, text in ((t("标准输出"), run["stdout"]), (t("标准错误"), run["stderr"])):
        if text:
            print(f"{label}：\n{text}", file=out)
    print(run["verdict"], file=out)
    for text in run["ignored"]:
        print(f"  · {text}", file=out)
    return 0
