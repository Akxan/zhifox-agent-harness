"""几个聊天平台的测试共用的一套组装：真的网页服务器（WebApp + ChannelHub）+ 本地的假 Claude API + 一个平台的假服务器。

test_channels.py 里的 Rig 是 Telegram 专用的（它在这之前就有了）。Discord、Slack、QQ、飞书、钉钉的测试用这一个：
假平台（tests/fake_discord.py……）实现下面 FakePlatform 写的那几样，同一套测试代码就能对着它跑。
测试代码扮演手机上的你（s.say、s.press）和浏览器（s.browser，和 test_webui.py 里的是同一个）。
走的都是真实的代码路径：WebSocket、发送队列、agent 循环、确认、存盘。不连任何真的服务，不花钱。
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from typing import Any, Protocol

import anthropic
from fake_api import FakeAPI
from test_channels import stop_later, until
from test_webui import Browser

import harness.webui.server as server_module
from harness.app import build_app
from harness.channels.store import ChannelStore
from harness.webui.server import create_server
from harness.webui.ui import WebUI


class FakePlatform(Protocol):
    """一个平台的假服务器要提供的东西（鸭子类型：照着写这几样就行，不用继承）。"""

    platform: str  # "discord"……
    credentials: dict  # 存进 channels.json 的凭据（编造的）
    bot: dict  # 存进 channels.json 的机器人信息（和这个平台的 check 返回的一样）
    owner: str  # 主人（手机上的你）在平台上的编号：router 看到的 Inbound.user
    owner_chat: str  # 主人和机器人的私聊在平台上的编号：router 看到的 Inbound.chat
    owner_name: str  # router 看到的主人的名字
    sent: list[dict]  # 机器人发出去的每一条消息，每条至少有 "chat" 和 "plain"（看得见的全部文字）
    secrets: list[str]  # 这个平台的全部密钥（测试最后检查它们没有出现在发给浏览器的任何东西里）

    def config(self) -> dict: ...  # Config 里要改的字段：接口地址指向这个假服务器

    def factory(self, config: Any, store: ChannelStore) -> Any: ...  # 建一个连这个假服务器的通道（时间都调短）

    def user_says(self, text: str = "", user: str | None = None, private: bool = True, **options: Any) -> None: ...

    def is_prompt(self, message: dict) -> bool: ...  # 这条消息是不是一个确认请求（带按钮的那条，或者写着"回复 允许 或 拒绝"的那条）

    def press(self, message: dict, allow: bool = True, user: str | None = None) -> None: ...  # 点确认请求上的按钮（没有按钮的平台不实现）

    def wait_sent(self, predicate: Callable[[dict], bool], timeout: float = 10.0, after: int = 0) -> dict: ...

    def stop(self) -> None: ...


class ChannelRig:
    """connected：先在 channels.json 里写好凭据（像在页面上连接过一样）；paired：再配对好主人。
    同一个测试里再建一个 ChannelRig，用的是同一个数据目录和同一个假平台，就相当于把程序重启了一遍。"""

    def __init__(self, make_config, fake: FakePlatform, replies, approval: str = "ask", paired: bool = True, connected: bool = True, online: bool = True):
        self.fake = fake
        self.api = FakeAPI(list(replies)).start()
        self.config = make_config(**fake.config())
        platform = fake.platform
        if connected:
            store = ChannelStore(self.config.channels_file)
            store.connect(platform, fake.credentials, fake.bot)
            if paired and store.get(platform)["owner"] is None:
                code = store.start_pairing(platform)
                store.attempt(platform, fake.owner, fake.owner_name, code["code"])
            store.set_approval(platform, approval)
        self.ui = WebUI()
        base_url = self.api.base_url

        def client_factory(provider):
            return anthropic.Anthropic(api_key=os.environ.get(provider.key_env) or "missing", base_url=base_url, max_retries=0)

        self.app = build_app(self.config, self.ui, client_factory=client_factory)
        self.server, self.url = create_server(self.app, self.ui, port=0)
        self.web = self.server.web
        self.hub = self.web.channels
        self.hub.factories[platform] = fake.factory
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.browser = Browser(self.url)
        self.browser.wait_synced()
        self.hub.start()
        self.closed = False
        if connected and online:
            until(lambda: self.channel() is not None and self.channel().status.state == "online", what=f"连上假的 {platform}")

    @property
    def platform_id(self) -> str:
        return self.fake.platform

    def channel(self):
        return self.hub.channel(self.fake.platform)

    def say(self, text: str = "", **options) -> int:
        """主人（或者 options 里指定的人）发一条消息。返回发之前机器人已经发了几条，wait_reply 从那以后找。"""
        count = len(self.fake.sent)
        self.fake.user_says(text, **options)
        return count

    def wait_reply(self, after: int, contains: str, timeout: float = 10) -> dict:
        return self.fake.wait_sent(lambda message: contains in message["plain"], timeout=timeout, after=after)

    def asked(self, after: int) -> dict:
        """等一条确认请求，并且等智狐这边记下它已经发出去了（拿到了消息编号；见 router._answerable）。"""
        message = self.fake.wait_sent(self.fake.is_prompt, after=after)
        until(lambda: any(session.waiting is not None and session.waiting.handle for session in self.hub.router.sessions(self.fake.platform)), what="确认请求记下了消息编号")
        return message

    def session(self):
        return until(lambda: self.hub.router.sessions(self.fake.platform), what="聊天")[0]

    def idle(self) -> None:
        """等这个聊天里的事都做完（跑完的一轮存好了盘），排着要发的话也都发出去了。"""
        until(lambda: all(not item.running and not item.queued() and item._worker is None for item in self.hub.router.sessions(self.fake.platform)), what="聊天空闲")
        self.hub.router.flush()

    def sentinel(self) -> None:
        """主人发一个 /help 并等到回答：消息是按顺序处理、按顺序发的，所以在它之前的消息一定都处理完了。"""
        self.wait_reply(self.say("/help"), "直接发消息就行")

    def state(self) -> dict:
        return until(lambda: self.browser.of_type("state"), what="状态")[-1]

    def platform(self) -> dict:
        return next(item for item in self.state()["channels"]["platforms"] if item["id"] == self.fake.platform)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        channel = self.channel()
        self.hub.close()
        if channel is not None and hasattr(channel, "join"):
            channel.join(2)
        self.web.stop()
        self.app.shell.kill_running()
        self.web.wait_idle(5)

        def shutdown_server() -> None:
            self.server.shutdown()
            self.server.server_close()

        stop_later(shutdown_server, self.api.stop)
        # 不管测的是什么，这个平台的密钥都不能出现在发给浏览器的任何事件里
        everything = json.dumps(self.browser.events, ensure_ascii=False)
        for secret in self.fake.secrets:
            assert secret not in everything


def rig_fixture(make_config, monkeypatch, make_fake: Callable[[], FakePlatform]):
    """一个 fixture 的内容：yield 一个 start(*回复, approval=..., paired=..., connected=...)，每调用一次组装出一个 ChannelRig。

    用法（在 tests/test_channels_discord.py 里）：

        @pytest.fixture
        def bot(make_config, monkeypatch):
            yield from rig_fixture(make_config, monkeypatch, lambda: FakeDiscord().start())
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(server_module, "reveal_in_file_manager", lambda path: None)
    monkeypatch.setattr(server_module, "has_file_manager", lambda: True)
    fake = make_fake()
    started: list[ChannelRig] = []

    def start(*replies, **options) -> ChannelRig:
        rig = ChannelRig(make_config, fake, replies, **options)
        started.append(rig)
        return rig

    start.fake = fake  # type: ignore[attr-defined]
    yield start
    try:
        for rig in reversed(started):
            rig.close()
    finally:
        stop_later(fake.stop)

