"""钉钉：用官方 SDK（dingtalk-stream）的 Stream 模式收消息，手写的 HTTP 客户端发消息，和一个钉钉通道。只处理单聊。

照着官方文档写的（2026 年 11 月看的，都在 https://open.dingtalk.com/document/ 下面）：
  Stream 模式      直接看的 SDK 源码（github.com/open-dingtalk/dingtalk-stream-sdk-python）和官方教程
                   （github.com/open-dingtalk/developerpedia 里的 docs/explore/tutorials/stream/bot/python/create-bot.md）
  收到消息          orgapp/receive-message
  回复和主动发      orgapp/types-of-messages-sent-by-robots、orgapp/chatbots-send-one-on-one-chat-messages-in-batches
  下载收到的文件    orgapp/download-the-file-content-of-the-robot-receiving-message
  access_token     orgapp/obtain-the-access_token-of-an-internal-app

为什么收消息要用 SDK：Stream 模式是钉钉自己的一套协议（先用 Client ID、Secret 换一张票据，再连 WebSocket，
每条消息要按它的格式回执），官方的 SDK（PyPI 上的 dingtalk-stream）已经写好了。它是可选的依赖：
`uv sync --extra dingtalk` 才会装，只在这个文件里、真要连的时候才导入。
发消息、下载文件是普通的 HTTPS + JSON，手写（有超时、密钥不进日志）。

怎么发：每条收到的单聊消息都带着一个 sessionWebhook（大约一个半小时内有效），往它 POST 就是回复这个人；
它过期了（一轮做得很久），改用"批量发送单聊消息"的接口（oToMessages/batchSend，要 access_token）主动发。
回复排成钉钉的 markdown（标题、粗体、链接、列表都认）。

为什么确认请求没有按钮：钉钉能回传点击的按钮只有"互动卡片"，要先在卡片平台上建一个模板、拿到模板编号；
不用模板的那种已经不让新应用用了，别的按钮只能打开网址。所以在钉钉里回复"允许"或"拒绝"，网页上也能点。

SDK 的几个坑（2026 年 11 月的 0.24.3 版），绕开的办法都在 StreamListener 里：
  - Client ID、Secret 不对时它只打日志、每 10 秒重试，永远不报错：所以每次连之前我们先自己换一次 access_token 看看
  - 没有"停下"，第一次 cancel 还会被它吞掉：一直 cancel 到它真的停下
  - 它的事件处理是在回执之前等着的（钉钉 3 秒内收不到回执会重推）：处理函数只把消息交给我们自己的线程，马上返回
  - 它的日志会打印连接用的票据：换成我们自己的日志（经过 ../log.py，只记警告以上）
  - 换票据的那一步（open_connection）用的是基类上写死的地址、不设超时：换成我们自己的请求（见 StreamListener._open）
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
from collections.abc import Callable
from typing import Any, Protocol

from .. import log
from ..config import CHANNEL_DOWNLOAD_BYTES, CHANNEL_RETRY_SECONDS, Config
from ..i18n import t
from ..net import http
from . import markup
from .base import (
    Capabilities,
    ChannelError,
    Event,
    Inbound,
    InboundFile,
    Prompt,
    PushChannel,
    backoff,
    close_loop,
)
from .platforms import missing_sdk

SAFE_LIMIT = 3500  # 一条消息自己拆到这么长（文档没写 markdown 的上限，留得保守一点）
UNSUPPORTED = {"audio": "voice", "video": "video"}

logger = logging.getLogger("harness.channels.dingtalk")


class DingTalkError(ChannelError):
    def __init__(self, message: str, code: str = "", status: int = 0):
        super().__init__(message)
        self.code = code
        self.status = status


def _explain(status: int, code: str, message: str) -> str:
    lowered = code.lower()
    if "invalidclientidorsecret" in lowered or "invalidappkey" in lowered or status == 401:
        return t("钉钉不认这个 Client ID 和 Client Secret：可能抄错了，或者 Client Secret 已经重置过。")
    if "forbidden" in lowered or status == 403:
        return t("钉钉拒绝了：应用缺少“企业内机器人发送消息”的权限。")
    if "toofast" in lowered or "flowcontrol" in lowered or status == 429:
        return t("发得太快，被钉钉限流了，过一会儿会自动重试。")
    return t("钉钉回答了一个错误：{error}", error=f"{code} {message}".strip() or status)


def _webhook_ok(url: str) -> bool:
    """收到的消息里带的 sessionWebhook 能不能往里发：只认钉钉自己的域名（测试里是本机）。"""
    host = urllib.parse.urlsplit(url).hostname or ""
    return http.trusted(url) and (host.endswith(".dingtalk.com") or url.startswith("http://"))


class OpenAPI:
    """钉钉开放平台接口的一个极简客户端：管 access_token，发请求。可以从几个线程同时用。"""

    def __init__(self, client_id: str, secret: str, base: str = "https://api.dingtalk.com"):
        self.client_id = client_id
        self.secret = secret
        self.base = base.rstrip("/")
        self._token = ""
        self._expires = 0.0
        self._lock = threading.Lock()
        log.hide(secret)

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(item for item in (self.secret, self._token) if item)

    def _request(self, method: str, url: str, body: Any, headers: dict | None = None) -> http.Reply:
        try:
            return http.request(method, url, headers=headers, body=body, hide=self.secrets)
        except http.NetError as exc:
            raise DingTalkError(t("连不上钉钉（{error}）", error=str(exc))) from None

    def token(self, fresh: bool = False) -> str:
        with self._lock:
            if not fresh and self._token and time.time() < self._expires - 300:
                return self._token
            reply = self._request("POST", f"{self.base}/v1.0/oauth2/accessToken", {"appKey": self.client_id, "appSecret": self.secret})
            data = reply.data if isinstance(reply.data, dict) else {}
            if not reply.ok or not data.get("accessToken"):
                raise DingTalkError(_explain(reply.status, str(data.get("code") or ""), str(data.get("message") or "")), str(data.get("code") or ""), reply.status)
            self._token = str(data["accessToken"])
            log.hide(self._token)
            self._expires = time.time() + float(data.get("expireIn") or 7200)
            return self._token

    def call(self, path: str, body: dict) -> dict:
        reply = self._request("POST", self.base + path, body, {"x-acs-dingtalk-access-token": self.token()})
        data = reply.data if isinstance(reply.data, dict) else {}
        if not reply.ok:
            raise DingTalkError(_explain(reply.status, str(data.get("code") or ""), http.scrub(str(data.get("message") or reply.text), self.secrets)), str(data.get("code") or ""), reply.status)
        return data

    def webhook(self, url: str, body: dict) -> None:
        """往一条消息带来的 sessionWebhook 发一条回复。"""
        reply = self._request("POST", url, body)
        data = reply.data if isinstance(reply.data, dict) else {}
        if not reply.ok or data.get("errcode") not in (0, None):
            raise DingTalkError(_explain(reply.status, str(data.get("errcode") or ""), str(data.get("errmsg") or "")), str(data.get("errcode") or ""), reply.status)

    def download(self, code: str, robot: str) -> bytes:
        data = self.call("/v1.0/robot/messageFiles/download", {"downloadCode": code, "robotCode": robot})
        url = data.get("downloadUrl")
        if not isinstance(url, str) or not http.trusted(url):
            raise ChannelError(t("没能下载这个文件。"))
        try:
            return http.download(url, limit=CHANNEL_DOWNLOAD_BYTES)
        except http.NetError as exc:
            raise ChannelError(str(exc)) from None


def check(config: Config, credentials: dict) -> dict:
    """检查 Client ID 和 Client Secret：换得到 access_token 就是对的。钉钉没有"问机器人自己叫什么"的接口，名字用 Client ID。"""
    client_id, secret = credentials.get("client_id", "").strip(), credentials.get("client_secret", "").strip()
    if len(client_id) < 6 or " " in client_id:
        raise ChannelError(t("Client ID 看起来不对（在“凭证与基础信息”里，以前叫 AppKey）。"))
    if len(secret) < 8:
        raise ChannelError(t("Client Secret 太短了，看看是不是没复制全。"))
    OpenAPI(client_id, secret, config.dingtalk_api).token()
    return {"id": client_id, "username": client_id, "name": client_id, "at": False, "app": client_id}


# ---- 收消息：SDK 那一侧 --------------------------------------------------------------


class Listener(Protocol):
    """Stream 连接的那一头。真的是 StreamListener（用 SDK），测试里换成一个假的（不用装 SDK）。

    run 一直运行到 stop 被调用。收到的每条机器人消息交给 on_message：内容是平常的字典，样子和钉钉文档里
    消息的 JSON 一样（msgtype、text、senderStaffId、sessionWebhook……）。状态交给 on_state(状态, 说明)。
    """

    def run(self, on_message: Callable[[dict], None], on_state: Callable[[str, str], None]) -> str: ...

    def stop(self) -> None: ...


class StreamListener:
    def __init__(self, client_id: str, secret: str, endpoint: str = "https://api.dingtalk.com"):
        self.client_id = client_id
        self.secret = secret
        self.endpoint = endpoint.rstrip("/")
        self._loop: Any = None
        self._task: Any = None
        self._client: Any = None
        self._stopped = threading.Event()

    def run(self, on_message: Callable[[dict], None], on_state: Callable[[str, str], None]) -> str:
        import asyncio

        import dingtalk_stream
        import websockets.exceptions  # noqa: F401 —— 0.24.3 的重连代码用到它却没导入，没导入过会在第一次断线时出错

        quiet = logging.getLogger("harness.channels.dingtalk.sdk")
        quiet.setLevel(logging.WARNING)  # SDK 在 info 这一级打印连接用的票据（见文件开头）：只留警告以上
        listener = self

        class Handler(dingtalk_stream.ChatbotHandler):
            def __init__(self) -> None:
                super().__init__()
                self.logger = quiet

            async def process(self, callback: Any) -> tuple[int, str]:
                on_message(callback.data if isinstance(callback.data, dict) else {})
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

        class Client(dingtalk_stream.DingTalkStreamClient):
            def open_connection(self) -> Any:
                found, state, message = listener._open(self.get_host_ip())
                if not listener._stopped.is_set():
                    on_state(state, message)
                return found

        client = Client(dingtalk_stream.Credential(self.client_id, self.secret), logger=quiet)
        client.register_callback_handler(dingtalk_stream.ChatbotMessage.TOPIC, Handler())
        logging.getLogger("dingtalk_stream.handler").setLevel(logging.WARNING)  # SDK 自己的日志：只留警告以上
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop, self._client = loop, client
        if self._stopped.is_set():
            loop.close()
            return "stopped"
        # stop 关掉连接以后，SDK 会以为断网了，在 error 这一级报"open connection failed""network exception"：
        # 那是我们自己关的，不是故障，不记（不然每次关掉通道、重启智狐，日志里都多两条吓人的错误）
        stopping = self._stopped
        hush = lambda record: not stopping.is_set()
        quiet.addFilter(hush)
        self._task = loop.create_task(client.start())
        try:
            loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass  # 被 stop 取消了
        except Exception:  # SDK 里出了错：当作这一次连接结束了（_run 等一会儿再连）
            logger.warning("dingtalk: Stream 连接出错", exc_info=True)
        finally:
            close_loop(loop)
            quiet.removeFilter(hush)
        return "stopped"

    def _open(self, local_ip: str) -> tuple[dict | None, str, str]:
        """换一张连 Stream 用的票据（POST /v1.0/gateway/connections/open）。返回（票据，状态，说明）：拿到了是（票据，online，""）；
        拿不到时票据是 None（SDK 过 10 秒会再来要），状态是 connecting（网络的问题），或者 error（钉钉拒绝了：多半是
        Client Secret 在开放平台上重置过。SDK 断线以后在它自己里面重连，不会回到 _run 的检查，所以在这里说出来）。

        为什么不用 SDK 自己的 open_connection：它发请求时用的是基类上写死的地址（DingTalkStreamClient.OPEN_CONNECTION_API，
        在子类里改了也不算数），endpoint 指向别处（测试里的假钉钉）时照样去连 api.dingtalk.com；请求还不设超时，
        网络卡住时会一直等下去。所以这一步用我们自己的 http：地址跟着 endpoint，有超时，密钥不进日志。请求的内容和 SDK 0.24.3 的一样。
        """
        import dingtalk_stream
        from dingtalk_stream.version import VERSION_STRING

        if self._stopped.is_set():
            return None, "off", ""
        body = {
            "clientId": self.client_id,
            "clientSecret": self.secret,
            "subscriptions": [{"type": "CALLBACK", "topic": dingtalk_stream.ChatbotMessage.TOPIC}],
            "ua": f"dingtalk-sdk-python/v{VERSION_STRING}-union",
            "localIp": local_ip,
        }
        try:
            reply = http.request("POST", self.endpoint + "/v1.0/gateway/connections/open", body=body, hide=(self.secret,))
        except http.NetError as exc:
            return None, "connecting", t("连不上钉钉（{error}）", error=str(exc))
        data = reply.data if isinstance(reply.data, dict) else {}
        if reply.ok and isinstance(data.get("endpoint"), str) and data.get("ticket"):
            log.hide(str(data["ticket"]))  # 票据就是这一次连接的钥匙
            return {"endpoint": data["endpoint"], "ticket": str(data["ticket"])}, "online", ""
        if 400 <= reply.status < 500 and reply.status != 429:
            return None, "error", _explain(reply.status, str(data.get("code") or ""), http.scrub(str(data.get("message") or ""), (self.secret,)))
        return None, "connecting", t("连不上钉钉，过一会儿会自动重试。")

    def stop(self) -> None:
        import asyncio

        self._stopped.set()
        loop, task = self._loop, self._task
        if loop is None or task is None or loop.is_closed():
            return

        async def halt() -> None:
            websocket = getattr(self._client, "websocket", None)
            if websocket is not None:
                try:
                    await websocket.close()
                except Exception:  # 关不上也要接着往下取消
                    logger.debug("dingtalk: 关 WebSocket 时出错", exc_info=True)
            while not task.done():  # SDK 会吞掉第一次 cancel（见文件开头）：一直取消到它真的停下
                task.cancel()
                await asyncio.sleep(0.05)

        try:
            asyncio.run_coroutine_threadsafe(halt(), loop)
        except RuntimeError:
            pass



# ---- 通道 ------------------------------------------------------------------------


def make(config: Config, store: Any) -> DingTalkChannel:
    credentials = store.get("dingtalk")["credentials"]
    return DingTalkChannel(credentials.get("client_id", ""), credentials.get("client_secret", ""), config.dingtalk_api)


class DingTalkChannel(PushChannel):
    platform = "dingtalk"
    capabilities = Capabilities(buttons=False, max_chars=SAFE_LIMIT, typing=False, files=True)

    def __init__(self, client_id: str, secret: str, base: str = "https://api.dingtalk.com", listener: Listener | None = None):
        super().__init__()
        self.api = OpenAPI(client_id, secret, base)
        self.client_id = client_id
        self._real = listener is None
        self.listener: Listener = listener or StreamListener(client_id, secret, base)
        self._deliver: Callable[[Event], None] | None = None
        self._chats: dict[str, dict] = {}  # 每个聊天最近一条消息带来的：sessionWebhook、过期时间、员工编号、robotCode
        self._chats_lock = threading.Lock()

    # ---- 收 ------------------------------------------------------------------

    def _run(self, deliver: Callable[[Event], None]) -> None:
        if self._real and missing_sdk(self.platform):
            self._trouble("error", missing_sdk(self.platform))
            return
        self._deliver = deliver
        delay = 1.0
        while not self._stopping.is_set():
            try:
                self.api.token()  # SDK 不会告诉我们 Secret 不对：先自己试一下
            except DingTalkError as exc:
                fatal = exc.status in (400, 401, 403)
                self._trouble("error" if fatal else "connecting", str(exc))
                wait, delay = backoff(delay, CHANNEL_RETRY_SECONDS)
                self._stopping.wait(CHANNEL_RETRY_SECONDS if fatal else wait)
                continue
            self.listener.run(self._on_message, self._on_state)
            if self._stopping.is_set():
                return
            wait, delay = backoff(delay, CHANNEL_RETRY_SECONDS)
            self._stopping.wait(wait)

    def _halt(self) -> None:
        self.listener.stop()

    def _on_state(self, state: str, message: str) -> None:
        if state == "online":
            self._online()
        else:
            self._trouble(state, message)

    def _on_message(self, data: dict) -> None:
        deliver = self._deliver
        if deliver is not None and not self._stopping.is_set():
            self._hand_off(lambda: self._receive(data, deliver))

    def _receive(self, data: dict, deliver: Callable[[Event], None]) -> None:
        inbound = self._message(data)
        if inbound is not None and not self._stopping.is_set():
            deliver(inbound)

    def _message(self, data: dict) -> Inbound | None:
        if not self._first_time(str(data.get("msgId") or "")):
            return None
        user = str(data.get("senderStaffId") or data.get("senderId") or "")
        if not user:
            return None
        private = str(data.get("conversationType") or "") == "1"
        chat = user if private else str(data.get("conversationId") or "")
        robot = str(data.get("robotCode") or self.client_id)
        if private:
            webhook = data.get("sessionWebhook")
            with self._chats_lock:
                self._chats[chat] = {
                    "webhook": webhook if isinstance(webhook, str) and _webhook_ok(webhook) else "",
                    "expires": float(data.get("sessionWebhookExpiredTime") or 0) / 1000,
                    "staff": str(data.get("senderStaffId") or ""),
                    "robot": robot,
                }
        inbound = Inbound(platform=self.platform, chat=chat, user=user, name=str(data.get("senderNick") or user), private=private)
        kind = str(data.get("msgtype") or "")
        content = data.get("content") if isinstance(data.get("content"), dict) else {}
        if kind == "text":
            inbound.text = str((data.get("text") or {}).get("content") or "").strip()
        elif kind == "picture" and content.get("downloadCode"):
            inbound.files.append(self._file(str(content["downloadCode"]), robot, "photo", "picture.jpg"))
        elif kind == "file" and content.get("downloadCode"):
            inbound.files.append(self._file(str(content["downloadCode"]), robot, "document", str(content.get("fileName") or "file")))
        elif kind == "richText":
            words = []
            for index, item in enumerate(content.get("richText") or []):
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("text"), str):
                    words.append(item["text"])
                if item.get("downloadCode"):
                    inbound.files.append(self._file(str(item["downloadCode"]), robot, "photo", f"picture-{index + 1}.jpg"))
            inbound.text = "".join(words).strip()
        elif kind in UNSUPPORTED:
            inbound.unsupported = UNSUPPORTED[kind]
        else:
            return None
        if not inbound.text and not inbound.files and not inbound.unsupported:
            return None
        return inbound

    def _file(self, code: str, robot: str, kind: str, name: str) -> InboundFile:
        return InboundFile(name=name, media_type="image/jpeg" if kind == "photo" else "application/octet-stream", size=None, kind=kind, fetch=lambda: self.api.download(code, robot))

    # ---- 发 ------------------------------------------------------------------

    def _post(self, chat: str, text: str, markdown: bool) -> None:
        """回复一句：sessionWebhook 还有效就往它发，过期了改成主动发（要知道这个人的员工编号）。"""
        with self._chats_lock:
            info = dict(self._chats.get(chat) or {})
        title = (markup.to_plain(text).strip().splitlines() or [""])[0][:40] or "ZhiFox"
        if info.get("webhook") and time.time() < info.get("expires", 0) - 60:
            body = {"msgtype": "markdown", "markdown": {"title": title, "text": text}} if markdown else {"msgtype": "text", "text": {"content": text}}
            try:
                self.api.webhook(info["webhook"], body)
                return
            except DingTalkError as exc:
                logger.info("dingtalk: sessionWebhook 发不出去，改成主动发（%s）", exc)
        staff = info.get("staff") or chat
        key, param = ("sampleMarkdown", {"title": title, "text": text}) if markdown else ("sampleText", {"content": text})
        self.api.call("/v1.0/robot/oToMessages/batchSend", {"robotCode": info.get("robot") or self.client_id, "userIds": [staff], "msgKey": key, "msgParam": json.dumps(param, ensure_ascii=False)})

    def send(self, chat: str, text: str, markdown: bool = False) -> None:
        for piece in markup.split(text, SAFE_LIMIT):
            self._post(chat, piece, markdown)

    def ask(self, chat: str, prompt: Prompt) -> str:
        """确认请求：标题、代码块里的完整命令、工作目录，最后一句"回复 允许 或 拒绝"。"""
        tail = "\n\n".join(part for part in (prompt.where, prompt.hint) if part)
        room = SAFE_LIMIT - markup.length(prompt.title) - markup.length(tail) - 20
        parts = markup.split(prompt.detail, max(400, room)) or [""]
        for index, part in enumerate(parts[:-1]):
            self._post(chat, (f"**{prompt.title}**\n\n" if index == 0 else "") + _block(part), markdown=True)
        last = "\n\n".join(item for item in ((f"**{prompt.title}**" if len(parts) == 1 else ""), _block(parts[-1]) if parts[-1] else "", tail) if item)
        self._post(chat, last, markdown=True)
        return f"dingtalk-{prompt.id}"  # 钉钉的消息改不了，也没有按钮要去掉；有个编号就说明发出去了

    def settle(self, chat: str, handle: str, outcome: str) -> None:
        if handle:
            self._post(chat, outcome, markdown=False)


def _block(text: str) -> str:
    return f"```\n{markup.fence_safe(text)}\n```"
