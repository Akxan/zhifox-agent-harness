"""聊天通道的总开关：哪些平台开着、它们现在怎么样，以及网页和命令行上的那几个操作。

    channels.json（store.py） ──设置──► ChannelHub ──开、关──► TelegramChannel……（每个平台一个，见 telegram.py）
                                              └── 收到的消息都交给 Router（router.py）

ChannelHub 自己不处理消息，只管"开着的通道和设置文件对得上"（apply）：你在页面上填了 token、关掉了、
换了 token，或者在命令行里改了设置文件，它就把对应的通道开起来、关掉、换一个新的。

通道只在智狐开着的时候运行：网页界面开着（`harness --web`），或者在服务器上作为服务开着（`harness serve`），
和定时任务一样（见 ../webui/lifecycle.py）。终端界面不运行它们。

网页上的操作和命令行（channels/cli.py）用的是同一套规则：检查 token 用 connect，生成配对码、换确认方式
用 store.py 里的方法。这里不另写一份。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from ..claims import Claims
from ..config import CHANNEL_SHUTDOWN_SECONDS, Config
from ..i18n import t
from ..tasks import retell
from .base import Channel, ChannelError
from .platforms import READY, catalog, label
from .router import Router
from .store import ChannelStore
from .telegram import TelegramChannel, check_token

WATCH_SECONDS = 2.0  # 每隔这么久看一眼设置文件有没有被别的程序（命令行）改过

logger = logging.getLogger("harness.channels")


def make_telegram(config: Config, store: ChannelStore) -> Channel:
    entry = store.get("telegram")
    return TelegramChannel(entry["token"], config.telegram_api, offset=entry["offset"], on_offset=lambda offset: store.set_offset("telegram", offset))


FACTORIES: dict[str, Callable[[Config, ChannelStore], Channel]] = {"telegram": make_telegram}


def connect(store: ChannelStore, config: Config, platform: str, token: str) -> dict:
    """检查并保存一个平台的 token（网页上的"连接"和命令行的 setup 都用这一个）。返回机器人的信息。"""
    if platform not in READY:
        raise ChannelError(t("{platform} 还不能用。", platform=label(platform)))
    bot = check_token(token, config.telegram_api)
    store.connect(platform, token.strip(), bot)
    logger.info("%s: 已连接机器人 @%s", platform, bot["username"])
    return bot


class ChannelHub:
    def __init__(self, app: Any, claims: Claims, changed: Callable[[], None] = lambda: None, saved: Callable[[str], None] = lambda _: None):
        self.app = app
        self.config: Config = app.config
        self.store = ChannelStore(app.config.channels_file)
        self._changed = changed
        self.router = Router(app, self.store, claims, changed, saved)
        self.factories = dict(FACTORIES)  # 测试会换掉它，让通道连本地的假服务器
        self._lock = threading.RLock()
        self._running: dict[str, tuple[Channel, str]] = {}  # 开着的通道，和开它时用的 token
        self._started = False
        self._stop_watching = threading.Event()
        self.store.on_change(self._store_changed)

    # ---- 开、关 --------------------------------------------------------------

    def start(self) -> None:
        """开始运行（网页界面或者服务启动时调用一次）：开着的通道都连上，并且开始留意设置文件的变化。"""
        with self._lock:
            self._started = True
        self.apply()
        self._stop_watching.clear()
        threading.Thread(target=self._watch, daemon=True, name="channels-watch").start()

    def _watch(self) -> None:
        while not self._stop_watching.wait(WATCH_SECONDS):
            try:
                self.store.refresh()  # 变了的话会通知 _store_changed
            except Exception:
                logger.exception("看设置文件时出错")

    def _store_changed(self) -> None:
        self.apply()
        self._changed()

    def apply(self) -> None:
        """让开着的通道和设置对得上：该开的开、该关的关、token 换了的换一个。"""
        with self._lock:
            for platform in READY:
                entry = self.store.get(platform)
                wanted = self._started and entry["enabled"] and bool(entry["token"]) and not self.store.problem
                current = self._running.get(platform)
                if current is not None and (not wanted or current[1] != entry["token"]):
                    current[0].stop()
                    del self._running[platform]
                    self.router.detach(platform)
                if wanted and platform not in self._running:
                    channel = self.factories[platform](self.config, self.store)
                    channel.on_status = self._changed  # type: ignore[attr-defined]
                    self.router.attach(platform, channel)
                    channel.start(self.router.deliver)
                    self.router.set_commands(platform)
                    self._running[platform] = (channel, entry["token"])

    def close(self) -> None:
        """退出：关掉所有通道，停下聊天里正在进行的一轮，等它们存好盘。"""
        with self._lock:
            self._started = False
            running = list(self._running.values())
            self._running.clear()
        self._stop_watching.set()
        for channel, _ in running:
            channel.stop()
        self.router.close(CHANNEL_SHUTDOWN_SECONDS)

    def relabel(self) -> None:
        """界面换了语言：聊天软件里命令菜单上的说明也换过去。"""
        for platform in list(self._running):
            self.router.set_commands(platform)

    def channel(self, platform: str) -> Channel | None:
        current = self._running.get(platform)
        return current[0] if current is not None else None

    # ---- 页面和命令行上的操作 ------------------------------------------------
    # 内容不对、现在做不了的，抛出 ChannelError（页面上回答 409，命令行里打印出来）。
    # 设置文件一变，_store_changed 会让通道跟着变、页面跟着刷新，所以这几个方法只管改设置。

    def connect(self, platform: str, token: str) -> dict:
        return connect(self.store, self.config, platform, token)

    def pair(self, platform: str) -> dict:
        self._require(platform)
        return self.store.start_pairing(platform)

    def cancel_pairing(self, platform: str) -> None:
        self.store.cancel_pairing(platform)

    def set_approval(self, platform: str, mode: str) -> None:
        self._require(platform)
        self.store.set_approval(platform, mode)

    def set_enabled(self, platform: str, enabled: bool) -> None:
        self._require(platform)
        self.store.set_enabled(platform, enabled)

    def unbind(self, platform: str) -> None:
        self._require(platform)
        self.store.unbind(platform)
        self.router.forget(platform)  # 聊天里还在进行的、排着的都停下：解除绑定以后谁的话都不再听

    def remove(self, platform: str) -> None:
        """删掉一个平台的全部设置。聊天里正在进行的一轮会停下；以前聊过的会话还在会话列表里。"""
        self.store.remove(platform)
        self.router.forget(platform)

    def confirm(self, platform: str, chat: str, confirm_id: int, allow: bool) -> None:
        if not self.router.confirm(platform, chat, confirm_id, allow):
            raise ChannelError(t("这个确认请求已经处理过了"))

    def stop_chat(self, platform: str, chat: str) -> None:
        if not self.router.stop(platform, chat):
            raise ChannelError(t("这个聊天里现在没有在进行的事。"))

    def holder(self, conversation_id: str) -> str:
        """这段会话正在哪个通道里跑一轮（平台名字）；没有返回空字符串。网页上打开、改名、删除、发消息之前问一声。"""
        return self.router.holder(conversation_id)

    def _require(self, platform: str) -> None:
        if platform not in READY:
            raise ChannelError(t("{platform} 还不能用。", platform=label(platform)))
        if not self.store.get(platform)["token"]:
            raise ChannelError(t("还没有连接 {platform}：先填好机器人的 token。", platform=label(platform)))

    # ---- 状态 --------------------------------------------------------------

    def snapshot(self) -> dict:
        """状态里的 channels：每个平台的设置（不含 token，只有最后 4 位）、连接的情况、各个聊天在做什么，
        以及聊天里等你批准的操作（网页上也能答复）。"""
        platforms, approvals = [], []
        for item in catalog():
            entry: dict = dict(item)
            if item["id"] in READY:
                channel = self.channel(item["id"])
                status = channel.status if channel is not None else None
                entry.update(self.store.summary(item["id"]))
                entry["status"] = {"state": status.state, "message": retell(status.message), "since": status.since} if status else {"state": "off", "message": "", "since": None}
                chats = [session.snapshot() for session in self.router.sessions(item["id"])]
                entry["chats"] = chats
                for chat in chats:
                    if chat["confirm"] is not None:
                        approvals.append({"platform": item["id"], "label": item["label"], "chat": chat["chat"], "name": chat["name"], **chat["confirm"]})
            platforms.append(entry)
        return {"problem": self.store.problem, "file": str(self.store.path), "platforms": platforms, "approvals": approvals}
