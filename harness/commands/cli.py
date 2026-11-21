"""命令行里管你自己加的"不用问的命令"：在没有浏览器的服务器上，用这几条代替网页上"设置 → 不用问的命令"。

    harness commands list                     列出来
    harness commands add "npm test"           加一条（以这几个词开头的命令，在"只读的命令不用问"那一档里不再问）
    harness commands remove "npm test"        删一条

和网页用的是同一份清单、同一套检查（见 allowlist.py）。智狐开着的时候也能用：它下一次判断时就会读到新的。
"""

from __future__ import annotations

import argparse
import sys
from typing import IO

from ..config import Config
from ..i18n import t
from .allowlist import CommandList, CommandListError


def add_parser(commands: argparse._SubParsersAction) -> None:
    """在 harness 的命令行里加上 commands 这一组命令。"""
    parser = commands.add_parser("commands", help=t("你自己加的“不用问的命令”（在“只读的命令不用问”那一档里算）：列出、添加、删除"))
    actions = parser.add_subparsers(dest="commands_action", required=True, metavar="ACTION")
    actions.add_parser("list", help=t("列出来"))
    add = actions.add_parser("add", help=t("加一条：以这几个词开头的命令不再问（后面跟什么参数都算）"))
    # 参数名不能叫 command：harness 自己的子命令（serve、channels、commands）已经叫这个名字了
    add.add_argument("entry", metavar="COMMAND", help=t("一条命令，比如 \"npm test\"（要用引号括起来）"))
    remove = actions.add_parser("remove", help=t("删一条"))
    remove.add_argument("entry", metavar="COMMAND", help=t("要删的那一条，和列出来的一字不差"))


def run(args: argparse.Namespace, config: Config, out: IO[str] | None = None) -> int:
    out = out or sys.stdout
    store = CommandList(config.commands_file)
    action = args.commands_action
    try:
        if action == "list":
            entries = store.entries()
            if store.problem:
                print(store.problem, file=sys.stderr)
            if not entries:
                print(t("还没有。常见的只读命令（ls、cat、grep、df、git status……）已经内置，不用加；加的是你信得过、但不是只读的命令，比如 npm test。"), file=out)
            for entry in entries:
                print(f"  {entry}", file=out)
            print(t("清单在：{path}。只在“只读的命令不用问”那一档里算（对话里 /auto readonly，聊天通道里 harness channels approval <平台> readonly）。", path=store.path), file=out)
            return 0
        if action == "add":
            entry = store.add(args.entry)
            print(t("加好了：以“{entry}”开头的命令，在“只读的命令不用问”那一档里不再问。后面跟什么参数都算，碰到 .env、助手自己的数据照样要问。", entry=entry), file=out)
            return 0
        if action == "remove":
            store.remove(args.entry)
            print(t("删掉了：{entry}", entry=args.entry), file=out)
            return 0
    except CommandListError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 2
