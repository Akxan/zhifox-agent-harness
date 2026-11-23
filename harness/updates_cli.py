"""命令行里管"检查新版本"：在没有浏览器的服务器上，用这几条代替网页上"设置 → 检查新版本"。规矩都在 updates.py。

    harness updates           看一眼：开没开、上一次什么时候问的、有没有新版本、怎么升级。只读缓存，不联网
    harness updates on        打开，并且马上问一次 GitHub（以后开着的智狐每天问一次）
    harness updates off       关上：不再问
    harness updates check     马上问一次（要先打开），不管上一次是什么时候问的

和网页用的是同一份设置（data/settings.json 的 update_check）和同一份缓存（data/updates.json）。
智狐正开着的时候（网页界面、后台服务）它记着的还是原来的设置：在它的网页上打开才马上生效，或者改完重启它。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from typing import IO

from . import instance
from .config import COMMAND, Config
from .i18n import t
from .settings import Settings
from .updates import UNKNOWN, Status, Updater, upgrade

ACTIONS = ("status", "on", "off", "check")


def add_parser(commands: argparse._SubParsersAction) -> None:
    """在 harness 的命令行里加上 updates 这一组命令。"""
    parser = commands.add_parser("updates", help=t("检查新版本（默认关着）：看一眼、打开、关上、马上问一次"))
    actions = parser.add_subparsers(dest="updates_action", metavar="ACTION")
    actions.add_parser("status", help=t("看一眼（不写 ACTION 也是这个）：只读上一次的结果，不联网"))
    actions.add_parser("on", help=t("打开：马上问一次 GitHub，以后每天问一次"))
    actions.add_parser("off", help=t("关上：不再问"))
    actions.add_parser("check", help=t("马上问一次（要先打开）"))


def run(args: argparse.Namespace, config: Config, out: IO[str] | None = None, updater: Updater | None = None) -> int:
    """updater 只给测试用（换一个时钟、现在的版本）；不给就照着这台电脑建一个。"""
    out = out or sys.stdout
    updater = updater or Updater(config, Settings(config.settings_file, config.default_provider, config.default_model))
    action = getattr(args, "updates_action", None) or "status"
    try:
        if action == "on":
            updater.settings.set_update_check(True)
            print(t("已打开：以后开着的智狐每天问一次 GitHub 有没有新版本。现在先问一次……"), file=out)
            _warn_running(config)
            updater.check(force=True)
        elif action == "off":
            updater.settings.set_update_check(False)
            print(t("已关掉：不再去问 GitHub。"), file=out)
            _warn_running(config)
            return 0
        elif action == "check":
            if not updater.enabled:
                print(t("检查新版本是关着的。要问的话先运行 {command} updates on。", command=COMMAND), file=sys.stderr)
                return 1
            updater.check(force=True)
    except OSError as exc:
        print(t("没能保存设置：{error}", error=str(exc)), file=sys.stderr)
        return 1
    return _show(updater.status(), out)


def _warn_running(config: Config) -> None:
    if instance.in_use(config.data_dir):
        print(t("智狐正开着：它记着的还是原来的设置。在它的网页上（设置 → 检查新版本）改才马上生效，或者重启它。"), file=sys.stderr)


def _show(status: Status, out: IO[str]) -> int:
    print(t("现在用的是 {version}。", version=status.current or "?"), file=out)
    if not status.enabled:
        print(t("检查新版本：关着（默认）。打开以后每天向 GitHub 发一个请求，GitHub 看得到你的 IP 地址，别的什么都不发：{command} updates on", command=COMMAND), file=out)
        return 0
    when = datetime.fromtimestamp(status.checked_at).strftime("%Y-%m-%d %H:%M") if status.checked_at else ""
    if status.problem is not None:
        print(t("没能检查：{problem}", problem=status.problem), file=out)
    found = status.newer
    if found is not None:
        print(t("有新版本：智狐 {version}。发布说明：{url}", version=found.version, url=found.page), file=out)
        plan = upgrade(found, UNKNOWN)
        for line in plan.before:
            print("  " + line, file=out)
        print("  " + plan.lead, file=out)
        for command in plan.commands:
            print("    " + command, file=out)
        for line in plan.notes:
            print("  " + line, file=out)
    elif status.problem is None and when:
        print(t("已经是最新的版本"), file=out)
    if when:
        print(t("上一次检查：{time}", time=when), file=out)
    elif status.problem is None:
        print(t("还没有检查过"), file=out)
    return 0
