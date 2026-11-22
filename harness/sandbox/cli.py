"""命令行里管命令的沙箱：在没有浏览器的服务器上，用这几条代替网页上"设置 → 沙箱"。

    harness sandbox                         看一眼：这台电脑用哪一种沙箱、能不能用、设置里选的哪一档、额外允许的路径
    harness sandbox level workspace         对话用哪一档（off / workspace / network），也是定时任务、聊天通道"跟随设置"时的那一档
    harness sandbox allow ~/.nvm [--write]  加一条额外允许的路径（默认只读）
    harness sandbox forget ~/.nvm           删一条
    harness sandbox check                   真的在沙箱里试几样，看它挡不挡得住（不联网、不改你的文件）

聊天通道自己的那一档用 harness channels sandbox <平台> <default|off|workspace|network>（见 channels/cli.py）。
和网页用的是同一份设置（data/settings.json）、同一套检查。智狐开着的时候也能用：下一条命令就按新的来。
"""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import sys
import threading
from pathlib import Path
from typing import IO

from ..config import SANDBOX_MAX_PATHS, Config
from ..i18n import t
from ..settings import Settings, path_is_absolute
from . import backend as backends
from .policy import LEVEL_TEXT, LEVELS, inside, real
from .session import Layout, Sandbox


def add_parser(commands: argparse._SubParsersAction) -> None:
    """在 harness 的命令行里加上 sandbox 这一组命令。"""
    parser = commands.add_parser("sandbox", help=t("命令的沙箱：看状态、选哪一档、额外允许的路径、试一下挡不挡得住"))
    actions = parser.add_subparsers(dest="sandbox_action", metavar="ACTION")
    actions.add_parser("status", help=t("看一眼（不写 ACTION 也是这个）"))
    level = actions.add_parser("level", help=t("对话用哪一档，也是定时任务、聊天通道“跟随设置”时的那一档"))
    level.add_argument("level", choices=list(LEVELS))
    allow = actions.add_parser("allow", help=t("加一条额外允许的路径（默认只读）"))
    allow.add_argument("path", help=t("完整的路径，从 / 或者 ~ 开始"))
    allow.add_argument("--write", action="store_true", help=t("能读写（不加就是只读）"))
    forget = actions.add_parser("forget", help=t("删一条额外允许的路径"))
    forget.add_argument("path", help=t("要删的那一条，和列出来的一字不差"))
    actions.add_parser("check", help=t("在沙箱里试几样：写工作目录、读家目录、读智狐的数据、连本机的一个端口"))


def run(args: argparse.Namespace, config: Config, out: IO[str] | None = None) -> int:
    out = out or sys.stdout
    settings = Settings(config.settings_file, config.default_provider, config.default_model)
    action = args.sandbox_action or "status"
    try:
        if action == "status":
            return _status(settings, out)
        if action == "level":
            settings.set_sandbox({"level": args.level})
            print(t("沙箱改成了：{level}。{detail}", level=t(LEVELS[args.level]), detail=t(LEVEL_TEXT[args.level])), file=out)
            problem = backends.detect().problem() if args.level != "off" else None
            if problem is not None:
                print(t("注意：这台电脑上现在用不了沙箱，命令会被拒绝执行，直到修好它：{problem}", problem=problem), file=sys.stderr)
            return 0
        if action == "allow":
            return _allow(settings, config, args.path, args.write, out)
        if action == "forget":
            paths = settings.sandbox["paths"]
            wanted = os.path.normpath(os.path.expanduser(args.path))
            kept = [item for item in paths if item["path"] not in (args.path, wanted)]
            if len(kept) == len(paths):
                print(t("列表里没有 {path}。", path=args.path), file=sys.stderr)
                return 1
            settings.set_sandbox({"paths": kept})
            print(t("删掉了：{entry}", entry=args.path), file=out)
            return 0
        if action == "check":
            return _check(settings, config, out)
    except OSError as exc:
        print(t("没能保存设置：{error}", error=str(exc)), file=sys.stderr)
        return 1
    return 2


def _status(settings: Settings, out: IO[str]) -> int:
    found = backends.detect()
    problem = found.problem()
    level = settings.sandbox["level"]
    print(t("这台电脑上的沙箱：{label}", label=t(found.label)), file=out)
    if problem is None:
        print("  " + t("能用。"), file=out)
    else:
        print("  " + t("用不了：{problem}", problem=problem), file=out)
    if found.note:
        print("  " + t(found.note), file=out)
    print(t("设置里选的：{level}。{detail}", level=t(LEVELS[level]), detail=t(LEVEL_TEXT[level])), file=out)
    if level != "off" and problem is not None:
        print(t("注意：选了沙箱、这台电脑上却用不了，命令一律拒绝执行（不会悄悄在沙箱之外运行）。修好它，或者 harness sandbox level off。"), file=sys.stderr)
    paths = settings.sandbox["paths"]
    if paths:
        print(t("额外允许的路径："), file=out)
        for item in paths:
            print(f"  {t('可读写') if item['write'] else t('只读')}  {item['path']}", file=out)
    else:
        print(t("额外允许的路径：还没有。"), file=out)
    return 0


def _allow(settings: Settings, config: Config, path: str, write: bool, out: IO[str]) -> int:
    if not path_is_absolute(path.strip()):
        print(t("要写完整的路径（从 / 或者 ~ 开始），最长 {n} 个字。", n=1000), file=sys.stderr)
        return 1
    path = os.path.normpath(os.path.expanduser(path.strip()))
    forbidden = [real(place) for place in (config.data_dir, *config.protected_files)]
    if inside(real(path), forbidden):
        print(t("这是智狐自己的数据（或者保存密钥的文件）：在沙箱里永远碰不到它，加进来也不会生效。"), file=sys.stderr)
        return 1
    paths = [item for item in settings.sandbox["paths"] if item["path"] != path]
    if len(paths) >= SANDBOX_MAX_PATHS:
        print(t("最多 {n} 条。先删掉一些用不到的再加。", n=SANDBOX_MAX_PATHS), file=sys.stderr)
        return 1
    settings.set_sandbox({"paths": [*paths, {"path": path, "write": write}]})
    print(t("加好了：{path}（{mode}）", path=path, mode=t("可读写") if write else t("只读")), file=out)
    return 0


def _check(settings: Settings, config: Config, out: IO[str]) -> int:
    """真的在沙箱里运行几条什么都不改的命令，看结果对不对。设置里是"关"的话，按"只在工作目录里"那一档试。"""
    from ..app import sandbox_layout
    from ..tools.shell import Shell

    found = backends.detect()
    problem = found.problem()
    if problem is not None:
        print(t("这台电脑上用不了沙箱：{problem}", problem=problem), file=sys.stderr)
        return 1
    level = settings.sandbox["level"] if settings.sandbox["level"] != "off" else "workspace"
    base = sandbox_layout(config, settings)
    layout = Layout(base.workspace, base.private, base.protected, base.extra, lambda: level)
    shell = Shell(config.workspace, Sandbox(layout, escape=lambda: False))
    print(t("在沙箱里试一下（{level}）：", level=t(LEVELS[level])), file=out)
    marker = f".zhifox-sandbox-check-{secrets.token_hex(4)}"
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    threading.Thread(target=_accept, args=(listener,), daemon=True).start()
    probes = [
        (t("在工作目录里写一个文件"), f"touch {marker} && rm {marker}", True),
        (t("读家目录"), f"ls {_quote(Path.home())}", False),
        (t("读智狐的数据目录"), f"ls {_quote(config.data_dir)}", False),
        (t("连这台电脑上的一个端口（127.0.0.1）"), f"exec 3<>/dev/tcp/127.0.0.1/{port}", level == "network"),
    ]
    wrong = 0
    try:
        for label, command, expected in probes:
            worked = "[退出码" not in shell.run(command, timeout_seconds=10)
            ok = worked == expected
            wrong += not ok
            outcome = (t("能") if worked else t("不能")) + ("" if ok else "  " + t("（不对！应该是“{expected}”）", expected=t("能") if expected else t("不能")))
            print(f"  {'✓' if ok else '✗'} {label}：{outcome}", file=out)
    finally:
        listener.close()
    print(t("都对。") if not wrong else t("有 {n} 项不对：沙箱没有按设置挡住，先别依赖它。", n=wrong), file=out if not wrong else sys.stderr)
    return 0 if not wrong else 1


def _accept(listener: socket.socket) -> None:
    try:
        while True:
            connection, _ = listener.accept()
            connection.close()
    except OSError:
        pass


def _quote(path: Path) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"
