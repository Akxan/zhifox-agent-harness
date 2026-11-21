"""飞书：用官方 SDK（lark-oapi）的长连接收消息，手写的 HTTP 客户端发消息，和一个飞书通道。只处理单聊。

照着官方文档写的（2026 年 11 月看的，都在 https://open.feishu.cn/document/ 下面）：
  长连接            server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case
  收到消息          uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/events/receive（im.message.receive_v1）
  发消息            server-docs/im-v1/message/create；消息的写法 server-docs/im-v1/message-content-description/create_json
  更新卡片          uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/patch
  卡片的按钮回调     uAjLw4CM/ukzMukzMukzM/feishu-cards/card-callback-communication（card.action.trigger）
  下载图片和文件     uAjLw4CM/ukTMukTMukTM/reference/im-v1/message-resource/get
  机器人自己的信息   client-docs/bot-v3/obtain-bot-info
  上传图片          server-docs/im-v1/image/create（https://open.feishu.cn/document/server-docs/im-v1/image/create）
  上传文件          server-docs/im-v1/file/create（https://open.feishu.cn/document/server-docs/im-v1/file/create）
  图片、文件消息     https://open.feishu.cn/document/server-docs/im-v1/message-content-description/create_json

为什么收消息要用 SDK：飞书的长连接不是普通的 JSON，而是 protobuf 编码的帧，还有分包、合包、心跳配置这些，
官方的 SDK（PyPI 上的 lark-oapi，github.com/larksuite/oapi-sdk-python）已经写好了。它是可选的依赖：
`uv sync --extra feishu` 才会装（见 pyproject.toml），没装的时候页面上会说清楚怎么装。它只在这个文件里、
真要连的时候才导入（导入一次要好几秒），智狐别的部分完全不依赖它。
发消息、改卡片、下载文件是普通的 HTTPS + JSON，手写（和别的平台一样，有超时、密钥不进日志）。

SDK 的几个坑（2026 年 11 月的 1.8.0 版），绕开的办法都在 LarkListener 里：
  - 它在导入时就拿好了一个模块级的事件循环（lark_oapi/ws/client.py 里的 loop），在别的线程里跑会出错。
    办法：在我们自己的线程里新建一个事件循环，换掉模块里的那个，并且在这个线程里建 Client
  - 没有公开的"停下"。办法和 SDK 1.8.0 自己的 FeishuChannel.stop 一样：关掉自动重连，在它的事件循环里调用
    _disconnect()，再让事件循环停下
  - 事件的处理函数是在它的事件循环里同步调用的，慢了会堵住心跳：我们的处理只是把消息交给 router，马上返回
  - 卡片回调要在 3 秒内回答，回答里可以带一句小提示（toast）：按钮的答复正好用它

单聊的消息里没有发消息的人的名字。有"读取通讯录"的权限的话问一下（contact/v3/users），没有就显示编号的后几位。

助手用 send_file 发文件（见 ../tools/send.py）：飞书的消息里不能直接带文件，要先传上去换一个编号，再发一条带着编号的消息。
  图片   POST /open-apis/im/v1/images（multipart：image_type=message、image），拿到 image_key，发一条 image 消息。
         最大 10 MB；GIF 不超过 2000×2000，别的不超过 12000×12000。尺寸不合格、飞书认不出格式的，改成当文件发
  文件   POST /open-apis/im/v1/files（multipart：file_type、file_name、file），拿到 file_key，发一条 file 消息。
         最大 30 MB，不收空文件。file_type 按扩展名选（pdf、doc、xls、ppt、mp4、opus），别的都是 stream
  说明   图片和文件消息里放不了文字，另发一条富文本，跟在文件后面
要的权限是"获取与上传图片或文件资源"（im:resource），向导里本来就让你开了。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import PurePath
from typing import Any, Protocol

from .. import log
from ..config import CHANNEL_DOWNLOAD_BYTES, CHANNEL_RETRY_SECONDS, Config
from ..i18n import t
from ..net import http, multipart
from . import markup
from .base import (
    Capabilities,
    ChannelError,
    Event,
    Inbound,
    InboundFile,
    OutboundFile,
    Press,
    Prompt,
    PushChannel,
    Uploads,
    backoff,
    close_loop,
)
from .platforms import missing_sdk

SAFE_LIMIT = 6000  # 一条消息自己拆到这么长（富文本最大 30 KB，中文一个字三个字节，再加上 JSON 的转义）
CARD_DETAIL = 5000  # 确认卡片里放的命令最多这么长，更长的前面几段先各发一条
TOKEN_INVALID = {99991663, 99991661, 99991668}  # tenant_access_token 过期了、不对：换一个再试
UNSUPPORTED = {"audio": "voice", "media": "video", "sticker": "sticker"}
# 能往聊天里发什么（见 base.Uploads）：图片当图片消息发最大 10 MB；别的文件（和更大的图片）当文件消息发，最大 30 MB。
# 飞书文档写的是"10 MB""30 MB"，没说是不是 1024 进位的：按小的算（10_000_000），宁可少发一点，也不要传上去才被拒绝
UPLOADS = Uploads(file_bytes=30_000_000, photo_bytes=10_000_000)
# 上传文件时的 file_type：按扩展名选。这几种之外的都是 stream（"任意二进制文件"）
FILE_TYPES = {"pdf": "pdf", "doc": "doc", "docx": "doc", "xls": "xls", "xlsx": "xls", "ppt": "ppt", "pptx": "ppt", "mp4": "mp4", "opus": "opus"}
# 传图片时飞书说"认不出格式"（234011）、"尺寸太大"（234039）：这张图当不了图片消息，改成当文件发
IMAGE_REFUSED = {234011, 234039}

logger = logging.getLogger("harness.channels.feishu")


class FeishuError(ChannelError):
    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = code


def _explain(code: int, message: str) -> str:
    if code in (10003, 10014, 20002, 1000040345):
        return t("飞书不认这个 App ID 和 App Secret：可能抄错了，或者 App Secret 已经重置过。")
    if code == 99991672 or code == 99991679:
        return t("飞书应用缺少权限：在开放平台的权限管理里开通向导里写的那几项，再发布一个版本。")
    if code == 99991403:
        return t("这个飞书组织本月调用接口的次数用完了。")
    if code == 230002:
        return t("机器人不在这个聊天里。")
    if code == 234006:
        return t("文件太大了，飞书不收：图片最大 10 MB，别的文件最大 30 MB。")
    if code == 234010:
        return t("飞书不收空文件。")
    if code == 234011:
        return t("飞书认不出这张图片的格式。")
    if code == 234039:
        return t("图片的尺寸太大了，飞书不收（GIF 最大 2000×2000，别的最大 12000×12000）。")
    return t("飞书回答了一个错误：{error}", error=f"{code} {message}".strip())


class OpenAPI:
    """飞书开放平台接口的一个极简客户端：管 tenant_access_token，发请求。可以从几个线程同时用。"""

    def __init__(self, app_id: str, secret: str, base: str = "https://open.feishu.cn"):
        self.app_id = app_id
        self.secret = secret
        self.base = base.rstrip("/")
        self._token = ""
        self._expires = 0.0
        self._lock = threading.Lock()
        log.hide(secret)

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(item for item in (self.secret, self._token) if item)

    def token(self, fresh: bool = False) -> str:
        with self._lock:
            if not fresh and self._token and time.time() < self._expires - 300:
                return self._token
            data = self._json("POST", "/open-apis/auth/v3/tenant_access_token/internal", {"app_id": self.app_id, "app_secret": self.secret}, None)
            if data.get("code") != 0 or not data.get("tenant_access_token"):
                raise FeishuError(_explain(int(data.get("code") or 0), str(data.get("msg") or "")), int(data.get("code") or 0))
            self._token = str(data["tenant_access_token"])
            log.hide(self._token)
            self._expires = time.time() + float(data.get("expire") or 7200)
            return self._token

    def _json(self, method: str, path: str, body: Any, token: str | None) -> dict:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            reply = http.request(method, self.base + path, headers=headers, body=body, hide=self.secrets)
        except http.NetError as exc:
            raise FeishuError(t("连不上飞书（{error}）", error=str(exc))) from None
        if isinstance(reply.data, dict):
            return reply.data
        raise FeishuError(t("飞书的回答看不懂（HTTP {status}）", status=reply.status))

    def call(self, method: str, path: str, body: Any = None) -> dict:
        """调用一个接口。返回回答里的 data（或者整个回答，比如 bot/v3/info 的 bot 不在 data 里）；code 不是 0 时抛出 FeishuError。"""
        for attempt in range(2):
            data = self._json(method, path, body, self.token(fresh=attempt > 0))
            code = int(data.get("code") or 0)
            if code in TOKEN_INVALID and attempt == 0:
                continue
            if code != 0:
                raise FeishuError(_explain(code, http.scrub(str(data.get("msg") or ""), self.secrets)), code)
            return data
        return {}

    def upload(self, path: str, fields: dict[str, str], file: multipart.File, key: str) -> str:
        """上传一张图片或一个文件（multipart/form-data），返回回答里 data 的 key 那一项（image_key 或 file_key）。

        和 call 一样：token 被飞书提前作废了（99991663……），换一个新的再传一次。"""
        for attempt in range(2):
            token = self.token(fresh=attempt > 0)
            try:
                reply = multipart.post(self.base + path, fields=fields, files=[file], headers={"Authorization": f"Bearer {token}"}, hide=self.secrets)
            except http.NetError as exc:
                raise FeishuError(t("连不上飞书（{error}）", error=str(exc))) from None
            data = reply.data if isinstance(reply.data, dict) else None
            if data is None:
                raise FeishuError(t("飞书的回答看不懂（HTTP {status}）", status=reply.status))
            code = int(data.get("code") or 0)
            if code in TOKEN_INVALID and attempt == 0:
                continue
            if code != 0:
                raise FeishuError(_explain(code, http.scrub(str(data.get("msg") or ""), self.secrets)), code)
            found = (data.get("data") or {}).get(key) if isinstance(data.get("data"), dict) else None
            if not isinstance(found, str) or not found:
                raise FeishuError(t("飞书的回答看不懂（HTTP {status}）", status=reply.status))
            return found
        return ""

    def download(self, message_id: str, key: str, kind: str) -> bytes:
        """下载聊天里的一张图片或者一个文件（kind 是 image 或 file）。

        和 call 一样，token 被飞书提前作废了也要能下：下载失败时只知道一个 HTTP 状态，分不清是不是 token 的问题，
        所以失败了就换一个新的 token 再试一次（多一次请求，比让你重发一遍图片好）。"""
        url = f"{self.base}/open-apis/im/v1/messages/{message_id}/resources/{key}?type={kind}"
        problem = ""
        for attempt in range(2):
            try:
                return http.download(url, headers={"Authorization": f"Bearer {self.token(fresh=attempt > 0)}"}, limit=CHANNEL_DOWNLOAD_BYTES, hide=self.secrets)
            except http.NetError as exc:
                problem = str(exc)
        raise ChannelError(problem)


def check(config: Config, credentials: dict) -> dict:
    """检查 App ID 和 App Secret：换得到 tenant_access_token、问得到机器人。返回机器人的信息。"""
    app_id, secret = credentials.get("app_id", "").strip(), credentials.get("app_secret", "").strip()
    if not app_id.startswith("cli_"):
        raise ChannelError(t("App ID 应该是 cli_ 开头的（在“凭证与基础信息”里）。"))
    if len(secret) < 8:
        raise ChannelError(t("App Secret 太短了，看看是不是没复制全。"))
    data = OpenAPI(app_id, secret, config.feishu_api).call("GET", "/open-apis/bot/v3/info")
    bot = data.get("bot") if isinstance(data.get("bot"), dict) else {}
    if not bot.get("open_id"):
        raise ChannelError(t("这个飞书应用还没有机器人：在“添加应用能力”里加上机器人，再发布一个版本。"))
    name = str(bot.get("app_name") or app_id)
    return {"id": str(bot["open_id"]), "username": name, "name": name, "at": False, "app": app_id, "link": f"https://applink.feishu.cn/client/bot/open?appId={app_id}"}


# ---- 收消息：SDK 那一侧 --------------------------------------------------------------


class Listener(Protocol):
    """长连接的那一头。真的是 LarkListener（用 SDK），测试里换成一个假的（不用装 SDK）。

    run 一直运行到 stop 被调用（或者再试也没用了）。事件交给 on_event(种类, 内容)：内容是平常的字典，
    样子和飞书文档里事件的 JSON 一样；卡片回调的返回值是给飞书的回答（{"toast": …}）。状态交给 on_state(状态, 说明)。
    返回 "fatal" 表示再连也没用（App ID、App Secret 不对），别的表示断了、可以重连。
    """

    def run(self, on_event: Callable[[str, dict], dict | None], on_state: Callable[[str, str], None]) -> str: ...

    def stop(self) -> None: ...


class LarkListener:
    def __init__(self, app_id: str, secret: str, domain: str = "https://open.feishu.cn"):
        self.app_id = app_id
        self.secret = secret
        self.domain = domain
        self._loop: Any = None
        self._client: Any = None
        self._stopped = threading.Event()

    @staticmethod
    def dispatcher(on_event: Callable[[str, dict], dict | None]) -> Any:
        """SDK 的事件分发器：收到的消息、卡片回调，换成平常的字典交给 on_event；卡片回调的回答包成 SDK 要的那个类。
        单独拿出来，测试可以用真的 SDK 喂一个事件进去，看看两边接得上（不用联网）。"""
        import lark_oapi as lark
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        def plain(data: Any) -> dict:
            try:
                value = json.loads(lark.JSON.marshal(data))
            except (TypeError, ValueError):
                return {}
            return value if isinstance(value, dict) else {}

        def message(data: Any) -> None:
            on_event("im.message.receive_v1", plain(data))

        def card(data: Any) -> Any:
            return P2CardActionTriggerResponse(on_event("card.action.trigger", plain(data)) or {})

        return lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(message).register_p2_card_action_trigger(card).build()

    def run(self, on_event: Callable[[str, dict], dict | None], on_state: Callable[[str, str], None]) -> str:
        import asyncio

        import lark_oapi as lark
        from lark_oapi.ws import client as ws_client
        from lark_oapi.ws.exception import ClientException

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        ws_client.loop = loop  # 换掉 SDK 在导入时拿的那个模块级事件循环（见文件开头）
        handler = self.dispatcher(on_event)
        client = lark.ws.Client(self.app_id, self.secret, event_handler=handler, log_level=lark.LogLevel.ERROR, domain=self.domain)
        client.on_reconnecting = lambda: on_state("connecting", "")
        client.on_reconnected = lambda: on_state("online", "")
        self._loop, self._client = loop, client
        try:
            if self._stopped.is_set():
                return "stopped"
            try:
                loop.run_until_complete(client._connect())  # SDK 的 start() 的前一半；后一半要能停下，所以自己写
            except ClientException as exc:
                on_state("error", _explain(int(getattr(exc, "code", 0) or 0), str(getattr(exc, "msg", "") or exc)))
                return "fatal"
            except Exception as exc:  # noqa: BLE001 —— 连不上（网络、飞书那边忙）：交给调用的人等一会儿再试
                on_state("connecting", t("连不上飞书（{error}）", error=http.scrub(str(exc), (self.secret,)) or type(exc).__name__))
                return "retry"
            on_state("online", "")
            loop.create_task(client._ping_loop())  # 心跳：和 start() 里一样
            loop.run_forever()  # 一直到 stop() 让它停下
            return "stopped"
        finally:
            close_loop(loop)

    def stop(self) -> None:
        import asyncio

        self._stopped.set()
        loop, client = self._loop, self._client
        if loop is None or client is None or loop.is_closed():
            return
        client._auto_reconnect = False  # 不然断开以后它会自己再连上
        try:
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(client._disconnect(), loop).result(timeout=3)
        except Exception:  # 断开时出了错也要让事件循环停下
            logger.debug("feishu: 断开长连接时出错", exc_info=True)
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass



# ---- 通道 ------------------------------------------------------------------------


def make(config: Config, store: Any) -> FeishuChannel:
    credentials = store.get("feishu")["credentials"]
    return FeishuChannel(credentials.get("app_id", ""), credentials.get("app_secret", ""), config.feishu_api)


def _card(title: str, detail: str, where: str, template: str, footer: list | None = None) -> dict:
    """一张飞书卡片（JSON 2.0）。update_multi 是"共享卡片"：只有它能在发出去以后再改（去掉按钮、写上结果）。"""
    elements: list = []
    if detail:
        elements.append({"tag": "markdown", "content": f"```\n{markup.fence_safe(detail)}\n```"})
    if where:
        elements.append({"tag": "markdown", "content": where, "text_size": "notation"})
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        "body": {"elements": elements + (footer or [])},
    }


def _button(text: str, kind: str, answer: str) -> dict:
    return {"tag": "column", "width": "auto", "elements": [
        {"tag": "button", "text": {"tag": "plain_text", "content": text}, "type": kind, "behaviors": [{"type": "callback", "value": {"answer": answer}}]},
    ]}


class FeishuChannel(PushChannel):
    platform = "feishu"
    capabilities = Capabilities(buttons=True, max_chars=SAFE_LIMIT, typing=False, files=True, uploads=UPLOADS)

    def __init__(self, app_id: str, secret: str, base: str = "https://open.feishu.cn", listener: Listener | None = None):
        super().__init__()
        self.api = OpenAPI(app_id, secret, base)
        self._real = listener is None  # 用的是真的 SDK（要检查装了没有）
        self.listener: Listener = listener or LarkListener(app_id, secret, base)
        self._deliver: Callable[[Event], None] | None = None
        self._asked: dict[str, dict] = {}  # 发出去的确认卡片：消息编号 → 卡片（答复以后换掉按钮）
        self._names: dict[str, str] = {}

    # ---- 收 ------------------------------------------------------------------

    def _run(self, deliver: Callable[[Event], None]) -> None:
        if self._real and missing_sdk(self.platform):
            self._trouble("error", missing_sdk(self.platform))
            return
        self._deliver = deliver
        delay = 1.0
        while not self._stopping.is_set():
            outcome = self.listener.run(self._on_event, self._on_state)
            if self._stopping.is_set() or outcome == "fatal":
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

    def _on_event(self, kind: str, data: dict) -> dict | None:
        deliver = self._deliver
        event = data.get("event") if isinstance(data.get("event"), dict) else {}
        if deliver is None or self._stopping.is_set():
            return None
        try:
            if kind == "im.message.receive_v1":
                self._hand_off(lambda: self._receive(event, deliver))  # 问名字要发请求：不在 SDK 的事件循环里做
                return None
            if kind == "card.action.trigger":
                said: list[tuple[str, bool]] = []
                press = self._press(event, said)
                if press is not None:
                    deliver(press)  # router 会马上调用 press.answer，答复的话就是 said 里的那一句
                text, problem = said[-1] if said else ("", False)
                return {"toast": {"type": "warning" if problem else "success", "content": text}} if text else {}
        except Exception:
            logger.exception("feishu: 处理一个事件时出错")
        return None

    def _receive(self, event: dict, deliver: Callable[[Event], None]) -> None:
        inbound = self._message(event)
        if inbound is not None and not self._stopping.is_set():
            deliver(inbound)

    def _message(self, event: dict) -> Inbound | None:
        sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        ids = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
        user, message_id = str(ids.get("open_id") or ""), str(message.get("message_id") or "")
        if sender.get("sender_type") != "user" or not user or not self._first_time(message_id):
            return None
        try:
            content = json.loads(message.get("content") or "{}")
        except ValueError:
            content = {}
        content = content if isinstance(content, dict) else {}
        kind = str(message.get("message_type") or "")
        inbound = Inbound(platform=self.platform, chat=str(message.get("chat_id") or ""), user=user, name=self._name(user), private=message.get("chat_type") == "p2p")
        if kind == "text":
            inbound.text = str(content.get("text") or "")
        elif kind == "image" and content.get("image_key"):
            inbound.files.append(self._file(message_id, str(content["image_key"]), "image", f"image-{message_id[-8:]}.jpg"))
        elif kind == "file" and content.get("file_key"):
            inbound.files.append(self._file(message_id, str(content["file_key"]), "file", str(content.get("file_name") or "file")))
        elif kind == "post":
            inbound.text, images = _post_text(content)
            inbound.files.extend(self._file(message_id, key, "image", f"image-{index + 1}.jpg") for index, key in enumerate(images))
        elif kind in UNSUPPORTED:
            inbound.unsupported = UNSUPPORTED[kind]
        else:
            return None
        if not inbound.text.strip() and not inbound.files and not inbound.unsupported:
            return None
        return inbound

    def _file(self, message_id: str, key: str, kind: str, name: str) -> InboundFile:
        return InboundFile(
            name=name,
            media_type="image/jpeg" if kind == "image" else "application/octet-stream",  # 飞书不说类型；attachments.py 会按内容再认一遍
            size=None,
            kind="photo" if kind == "image" else "document",
            fetch=lambda: self.api.download(message_id, key, kind),
        )

    def _press(self, event: dict, said: list[tuple[str, bool]]) -> Press | None:
        operator = event.get("operator") if isinstance(event.get("operator"), dict) else {}
        action = event.get("action") if isinstance(event.get("action"), dict) else {}
        context = event.get("context") if isinstance(event.get("context"), dict) else {}
        value = action.get("value") if isinstance(action.get("value"), dict) else {}
        user = str(operator.get("open_id") or "")
        return Press(
            platform=self.platform,
            chat=str(context.get("open_chat_id") or ""),
            user=user,
            name=self._names.get(user, user),
            data=str(value.get("answer") or ""),
            answer=lambda text, problem=False: said.append((str(text), bool(problem))),
        )

    def _name(self, user: str) -> str:
        """发消息的人的名字（有读通讯录的权限才问得到，问不到就用编号的后几位）。问过一次就记下来，不论结果。"""
        if user not in self._names:
            fallback = t("飞书用户 {id}", id=user[-6:])
            try:
                found = self.api.call("GET", f"/open-apis/contact/v3/users/{user}?user_id_type=open_id")
                person = (found.get("data") or {}).get("user") or {}
                self._names[user] = str(person.get("name") or fallback)
            except ChannelError:
                self._names[user] = fallback
        return self._names[user]

    # ---- 发 ------------------------------------------------------------------

    def _create(self, chat: str, kind: str, content: dict) -> str:
        data = self.api.call("POST", "/open-apis/im/v1/messages?receive_id_type=chat_id", {"receive_id": chat, "msg_type": kind, "content": json.dumps(content, ensure_ascii=False)})
        return str((data.get("data") or {}).get("message_id") or "")

    def _post(self, chat: str, text: str) -> str:
        """一条富文本消息，内容是一整段 Markdown（md 标签：CommonMark 加 GFM）。"""
        return self._create(chat, "post", {"zh_cn": {"content": [[{"tag": "md", "text": text}]]}})

    def send(self, chat: str, text: str, markdown: bool = False) -> None:
        for piece in markup.split(text, SAFE_LIMIT):
            if markdown:
                self._post(chat, piece)
            else:
                self._create(chat, "text", {"text": piece})

    def send_file(self, chat: str, outgoing: OutboundFile) -> None:
        """发一个文件：图片先传成 image_key、发一条图片消息；别的先传成 file_key、发一条文件消息。说明另发一条，跟在后面。"""
        if outgoing.kind == "photo":
            try:
                key = self.api.upload("/open-apis/im/v1/images", {"image_type": "message"}, multipart.File("image", outgoing.name, outgoing.media_type, outgoing.data), "image_key")
            except FeishuError as exc:
                if exc.code not in IMAGE_REFUSED or len(outgoing.data) > UPLOADS.file_bytes:
                    raise
                logger.info("feishu: 当图片发不出去，改成当文件发（%s）", exc)
            else:
                self._create(chat, "image", {"image_key": key})
                self._caption(chat, outgoing)
                return
        file_type = FILE_TYPES.get(PurePath(outgoing.name).suffix.lower().removeprefix("."), "stream")
        fields = {"file_type": file_type, "file_name": outgoing.name}
        key = self.api.upload("/open-apis/im/v1/files", fields, multipart.File("file", outgoing.name, outgoing.media_type, outgoing.data), "file_key")
        self._create(chat, "file", {"file_key": key})
        self._caption(chat, outgoing)

    def _caption(self, chat: str, outgoing: OutboundFile) -> None:
        """文件的说明：图片、文件消息里放不了文字，另发一条（Markdown 排成富文本，太长照样拆开）。"""
        if outgoing.caption.strip():
            self.send(chat, outgoing.caption.strip(), markdown=True)

    def ask(self, chat: str, prompt: Prompt) -> str:
        """确认请求：一张卡片，标题、代码块里的完整命令、工作目录，下面两个按钮。卡片发不出去的话，退回成一段文字（回复"允许"也行）。"""
        parts = markup.split(prompt.detail, CARD_DETAIL) or [""]
        for part in parts[:-1]:
            self._post(chat, f"```\n{markup.fence_safe(part)}\n```")
        buttons = {"tag": "column_set", "columns": [_button(prompt.allow, "primary", f"ok:{prompt.id}"), _button(prompt.deny, "danger", f"no:{prompt.id}")]}
        card = _card(prompt.title, parts[-1], prompt.where, "orange")
        try:
            handle = self._create(chat, "interactive", {**card, "body": {"elements": [*card["body"]["elements"], buttons]}})
        except FeishuError as exc:
            logger.info("feishu: 卡片发不出去，改成文字（%s）", exc)
            text = "\n\n".join(item for item in (f"**{prompt.title}**", f"```\n{markup.fence_safe(parts[-1])}\n```" if parts[-1] else "", prompt.where, prompt.hint) if item)
            return self._post(chat, text)
        if handle:
            self._asked[handle] = card
        return handle

    def settle(self, chat: str, handle: str, outcome: str) -> None:
        card = self._asked.pop(handle, None)
        if not handle:
            return
        if card is None:
            self._create(chat, "text", {"text": outcome})  # 当时发的是文字：另发一条说明结果
            return
        done = {**card, "header": {**card["header"], "template": "grey"}, "body": {"elements": [*card["body"]["elements"], {"tag": "markdown", "content": f"**{outcome}**"}]}}
        try:
            self.api.call("PATCH", f"/open-apis/im/v1/messages/{handle}", {"content": json.dumps(done, ensure_ascii=False)})
        except FeishuError:
            self._create(chat, "text", {"text": outcome})


def _post_text(content: dict) -> tuple[str, list[str]]:
    """一条富文本（post）消息里的文字和图片。收到的有时外面包着一层语言（zh_cn），有时没有。"""
    if "content" not in content:
        content = next((value for value in content.values() if isinstance(value, dict)), {})
    lines, images = [], []
    title = content.get("title")
    if isinstance(title, str) and title:
        lines.append(title)
    for row in content.get("content") or []:
        words = []
        for node in row if isinstance(row, list) else []:
            if not isinstance(node, dict):
                continue
            if node.get("tag") == "img" and node.get("image_key"):
                images.append(str(node["image_key"]))
            elif node.get("tag") == "a":
                words.append(f"{node.get('text') or ''}（{node.get('href') or ''}）")
            elif isinstance(node.get("text"), str):
                words.append(node["text"])
        lines.append("".join(words))
    return "\n".join(lines).strip(), images
