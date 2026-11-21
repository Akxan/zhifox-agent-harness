"""命令行里设置聊天通道：在没有浏览器的服务器上，用这几条命令代替网页上的"通道"那一页。

    harness channels status                       每个平台的情况
    harness channels setup <平台>                 照着步骤建机器人，填 token 或 ID 和密钥（密钥不回显；也可以先放在环境变量里，
                                                  比如 TELEGRAM_BOT_TOKEN、SLACK_BOT_TOKEN，见 platforms.py 的 fields）
    harness channels pair <平台>                  显示配对码，等你从自己的账号发给机器人
    harness channels approval <平台> ask          这个通道里要不要先问你：ask / readonly / edits / all
    harness channels enable|disable <平台>        打开、关掉
    harness channels remove <平台>                删掉这个平台的全部设置

平台是 telegram、discord、slack、feishu（飞书）、dingtalk（钉钉）、qq。
和网页用的是同一套规则（hub.connect 检查凭据，store.py 管配对码和确认方式，guides.py 是建机器人的步骤），
这里只管怎么问、怎么打印。

智狐开着（作为服务在运行）的时候也能用：改的是 channels.json，开着的那个两秒内就会发现（见 hub.py 的 _watch）。
配对时：智狐开着的话，由它去收你发来的配对码，这里只是等着看配对成了没有；没开着的话，这里自己临时连上
平台去收（同一个机器人同一时间只能有一处在收消息），配好就退出。

密钥绝不打印出来：输入时不回显，之后只显示最后 4 位。
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import threading
import time
from typing import IO

from .. import instance
from ..config import APP_NAME, Config
from ..i18n import fill, k, t
from ..permissions import MODES
from .base import ChannelError, Event, Inbound
from .guides import GUIDES
from .hub import FACTORIES, connect
from .platforms import ORDER, fields, label, missing_sdk
from .router import pairing_reply
from .store import ChannelStore, handle


def add_parser(commands: argparse._SubParsersAction) -> None:
    """在 harness 的命令行里加上 channels 这一组命令。"""
    parser = commands.add_parser("channels", help=t("聊天通道（Telegram、Discord、Slack、飞书、钉钉、QQ）：设置、配对、查看状态"))
    actions = parser.add_subparsers(dest="channels_action", required=True, metavar="ACTION")
    actions.add_parser("status", help=t("每个平台的情况"))
    for name, help_text in (
        ("setup", t("照着步骤建机器人，填 token 或 ID 和密钥（密钥不回显）。也可以先放在环境变量里，比如 TELEGRAM_BOT_TOKEN")),
        ("pair", t("显示配对码，等你从自己的账号发给机器人")),
        ("enable", t("打开这个通道")),
        ("disable", t("关掉这个通道")),
        ("remove", t("删掉这个平台的全部设置（token、绑定的账号）")),
    ):
        action = actions.add_parser(name, help=help_text)
        action.add_argument("platform", choices=ORDER)
        if name == "pair":
            action.add_argument("--no-wait", action="store_true", help=t("只显示配对码，不等"))
        if name == "remove":
            action.add_argument("--yes", action="store_true", help=t("不再问一遍"))
    approval = actions.add_parser(
        "approval", help=t("这个通道里要不要先问你：ask 每次都问，readonly 只读的命令不用问，edits 改工作目录里的文件不用问，all 全部自动允许")
    )
    approval.add_argument("platform", choices=ORDER)
    approval.add_argument("mode", choices=list(MODES))


def run(args: argparse.Namespace, config: Config, out: IO[str] | None = None) -> int:
    out = out or sys.stdout
    store = ChannelStore(config.channels_file)
    if store.problem:
        print(store.problem, file=sys.stderr)
        return 2
    action = args.channels_action
    try:
        if action == "status":
            return _status(store, config, out)
        platform = args.platform
        if action == "setup":
            return _setup(store, config, platform, out)
        if action == "pair":
            return _pair(store, config, platform, out, wait=not args.no_wait)
        if action == "approval":
            _require(store, platform)
            store.set_approval(platform, args.mode)
            print(t("{platform} 的确认方式改成了：{mode}", platform=label(platform), mode=t(MODES[args.mode])), file=out)
            if args.mode == "all":
                print(t(ALL_WARNING), file=out)
            return 0
        if action in ("enable", "disable"):
            _require(store, platform)
            store.set_enabled(platform, action == "enable")
            print(t("{platform} 已打开。", platform=label(platform)) if action == "enable" else t("{platform} 已关闭。", platform=label(platform)), file=out)
            return 0
        if action == "remove":
            if not args.yes and not _yes(t("删掉 {platform} 的全部设置（token、绑定的账号）？以前聊过的会话不会删。[y/N] ", platform=label(platform))):
                return 1
            store.remove(platform)
            print(t("已删除 {platform} 的设置。", platform=label(platform)), file=out)
            return 0
    except ChannelError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 2


ALL_WARNING = k("注意：确认是你和模型之间唯一的一道关卡。全部自动允许以后，聊天里的一句话就能让助手在这台机器上执行任何命令，不再问你；网页或文件里藏着误导模型的话，它照做的时候也没人拦着。只在你清楚风险的时候用，用完改回 ask。")


def _require(store: ChannelStore, platform: str) -> None:
    if not store.configured(platform):
        raise ChannelError(t("还没有连接 {platform}：先运行 harness channels setup {id}。", platform=label(platform), id=platform))


def _yes(question: str) -> bool:
    try:
        return input(question).strip().lower() in ("y", "yes", "是", "sí", "si")
    except EOFError:
        return False


def _running(config: Config) -> bool:
    """智狐是不是正在运行（网页界面或者服务）：数据目录被占着就是（见 instance.py）。"""
    return instance.in_use(config.data_dir)


def _when(moment: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(moment)) if moment else "—"


def _status(store: ChannelStore, config: Config, out: IO[str]) -> int:
    running = _running(config)
    print(t("{name}正在运行：聊天通道开着。", name=APP_NAME) if running else t("{name}没有在运行：聊天通道只在 harness serve 或 harness --web 开着的时候工作。", name=APP_NAME), file=out)
    for platform in ORDER:
        info = store.summary(platform)
        print(file=out)
        print(label(platform), file=out)
        if not info["configured"]:
            print("  " + t("还没有设置。运行 harness channels setup {id}。", id=platform), file=out)
            continue
        bot = info["bot"] or {}
        print("  " + t("机器人：{bot}（密钥 {hint}）", bot=bot.get("handle") or "?", hint=info["token_hint"]), file=out)
        print("  " + (t("已打开") if info["enabled"] else t("已关闭")), file=out)
        owner = info["owner"]
        print("  " + (t("主人：{name}（{when} 配对）", name=owner["name"], when=_when(owner["paired_at"])) if owner else t("还没有配对。运行 harness channels pair {id}。", id=platform)), file=out)
        if info["pairing"]:
            print("  " + t("正在配对：配对码 {code}，{when} 过期", code=info["pairing"]["code"], when=_when(info["pairing"]["expires"])), file=out)
        print("  " + t("确认方式：{mode}", mode=t(MODES[info["approval"]])), file=out)
        print("  " + t("最后一次说话：{when}", when=_when(info["last_active"])), file=out)
        if info["ignored"]:
            print("  " + t("忽略了 {n} 条陌生人的消息", n=info["ignored"]), file=out)
    return 0


def _read(field: dict) -> str:
    """一项要填的东西从哪儿来：环境变量（或者 .env 里写的）→ 标准输入（不是终端的时候，方便脚本用管道传进来，
    一行一项，按 fields 的顺序）→ 问你（密钥不回显）。"""
    name = field.get("env", "")
    if name and os.environ.get(name, "").strip():
        return os.environ[name].strip()
    if not sys.stdin.isatty():
        return sys.stdin.readline().strip()
    if field["secret"]:
        return getpass.getpass(t("粘贴 {field}（输入的内容不会显示出来）：", field=t(field["label"]))).strip()
    return input(t("{field}：", field=t(field["label"]))).strip()


def _guide(platform: str, out: IO[str]) -> None:
    """照着 guides.py 印出建机器人的步骤（和网页上向导的第一步是同一份）。"""
    for number, step in enumerate(GUIDES.get(platform, []), start=1):
        print(f"{number}. " + fill(t(step["text"]), {"code": step.get("code", "")}), file=out)
        if step.get("link"):
            print("   " + step["link"], file=out)
        if step.get("copy"):
            print(file=out)
            print(step["copy"], file=out)
            print(file=out)


def _setup(store: ChannelStore, config: Config, platform: str, out: IO[str]) -> int:
    problem = missing_sdk(platform)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    _guide(platform, out)
    given = {field["id"]: _read(field) for field in fields(platform)}
    if not any(given.values()):
        print(t("没有收到 token。"), file=sys.stderr)
        return 1
    bot = connect(store, config, platform, given)
    print(t("已连接机器人 {bot}（密钥 {hint}）。", bot=bot["handle"], hint=store.summary(platform)["token_hint"]), file=out)
    if bot.get("install"):
        print(t("先用这个链接把机器人加到你自己的账号（Add to My Apps），才能和它私聊：{link}", link=bot["install"]), file=out)
    if store.get(platform)["owner"] is None:
        print(t("下一步：运行 harness channels pair {id}，把配对码发给 {bot}。", id=platform, bot=bot["handle"]), file=out)
    return 0


def _pair(store: ChannelStore, config: Config, platform: str, out: IO[str], wait: bool) -> int:
    _require(store, platform)
    before = store.get(platform)["owner"]
    code = store.start_pairing(platform)
    summary = store.summary(platform)
    bot = handle(store.get(platform)["bot"])
    print(t("配对码：{code}（{minutes} 分钟内有效）", code=f"{code['code'][:3]} {code['code'][3:]}", minutes=round((code["expires"] - store.clock()) / 60)), file=out)
    print(t("用你自己的 {platform} 账号，把这 6 位数字发给 {bot}。", platform=label(platform), bot=bot), file=out)
    link = (summary["pairing"] or {}).get("link")
    if link:
        print(t("也可以直接打开：{link}", link=link), file=out)
    if not wait:
        return 0
    if _running(config):
        print(t("{name}正在运行，由它来收你发的配对码。等着……（Ctrl-C 不等了，配对码照样有效）", name=APP_NAME), file=out)
        return _wait_for_owner(store, platform, before, code["expires"], out)
    return _pair_here(store, config, platform, before, code["expires"], out)


def _paired(store: ChannelStore, platform: str, before: dict | None) -> dict | None:
    owner = store.get(platform)["owner"]
    return owner if owner is not None and owner != before else None


def _wait_for_owner(store: ChannelStore, platform: str, before: dict | None, expires: float, out: IO[str]) -> int:
    try:
        while store.clock() < expires:
            store.refresh()
            owner = _paired(store, platform, before)
            if owner is not None:
                print(t("配对成功：{name}", name=owner["name"]), file=out)
                return 0
            if store.pairing(platform) is None:
                print(t("配对码已经作废（猜错的次数太多，或者被取消了）。"), file=sys.stderr)
                return 1
            time.sleep(1)
    except KeyboardInterrupt:
        return 130
    print(t("配对码过期了。"), file=sys.stderr)
    return 1


def _pair_here(store: ChannelStore, config: Config, platform: str, before: dict | None, expires: float, out: IO[str]) -> int:
    """智狐没开着：自己临时连上平台收配对码，配好（或者过期）就断开。"""
    lock = instance.claim(config.data_dir, "channels")  # 占着数据目录，这期间别人启动不了智狐，不会两处同时收消息
    if lock is None:
        return _wait_for_owner(store, platform, before, expires, out)
    channel = FACTORIES[platform](config, store)
    done = threading.Event()

    def deliver(event: Event) -> None:
        if not isinstance(event, Inbound):
            return
        outcome, reply = pairing_reply(store, event, channel.capabilities)
        if reply:
            try:
                channel.send(event.chat, reply)
            except ChannelError as exc:
                print(str(exc), file=sys.stderr)
        if outcome in ("paired", "locked"):
            done.set()

    print(t("正在等你发配对码……（Ctrl-C 退出）"), file=out)
    channel.start(deliver)
    try:
        while not done.wait(1) and store.clock() < expires:
            status = channel.status
            if status.state == "error":
                print(str(status.message), file=sys.stderr)
                return 1
    except KeyboardInterrupt:
        return 130
    finally:
        channel.stop()
        lock.close()
    owner = _paired(store, platform, before)
    if owner is not None:
        print(t("配对成功：{name}", name=owner["name"]), file=out)
        return 0
    print(t("配对码已经作废（猜错的次数太多，或者被取消了）。") if done.is_set() else t("配对码过期了。"), file=sys.stderr)
    return 1
