"""QQ：手写的 QQ 机器人开放平台客户端（WebSocket 网关加 HTTP 接口），和一个 QQ 通道。只处理单聊（C2C）。

照着官方文档写的（2026 年 11 月看的，都在 https://bot.q.qq.com/wiki/ 下面）：
  access_token     develop/api-v2/dev-prepare/interface-framework/api-use.html、dev-prepare/access-token.html
  WebSocket        develop/api-v2/dev-prepare/interface-framework/event-emit.html（"WebSocket：不依赖公网服务器部署，
                   适合个人单机使用"。2024 年底那里写过"WebSocket 将逐步下线"，2026 年 3 月以后的版本里这句已经删掉了）
  单聊消息事件      develop/api-v2/server-inter/message/send-receive/event.html（C2C_MESSAGE_CREATE）
  发单聊消息        develop/api-v2/server-inter/message/send-receive/send.html（POST /v2/users/{openid}/messages）
官方的 Python SDK（qq-botpy）2024 年以后就没再更新过，这里不用它。

和 Discord 几乎一样（连接、心跳、Resume 见 gateway.py），区别在这几处：
  - 登录用的不是一个固定的 token，而是用 AppID + AppSecret 换来的 access_token（两个小时过期，快过期时换一个新的）。
    请求头写成 Authorization: QQBot {access_token}
  - intents 是 GROUP_AND_C2C_EVENT（1 << 25）：单聊和群里 @ 机器人的消息都在这一类里，群里的我们不理
  - 回复有讲究："被动回复"带着用户那条消息的编号（msg_id），60 分钟内、每条最多回 4 次；之后（一轮做得很久、
    说了很多句）改成"主动消息"，不带 msg_id，每个用户每分钟最多 20 条。同一个 msg_id 的几次回复用 msg_seq 区分
  - 没有按钮（要申请 markdown、键盘的权限），确认请求靠回复"允许"或"拒绝"；也不排版，Markdown 变成纯文字发
  - 同一条消息可能推两次，按消息的编号去掉重复的

为什么助手不能在 QQ 里发文件（send_file，见 ../tools/send.py；Capabilities.uploads 是 None，工具会直接说"暂时不支持"）：
单聊的富媒体（develop/api-v2/server-inter/message/send-receive/rich-media.html：先 POST /v2/users/{openid}/files，
再发一条 msg_type 7 的消息）要的是一个公网上能访问的网址（url），由 QQ 的服务器自己去下载；直接传文件的字节只有频道能用
（2026 年 11 月看的，开源 SDK botrs 的说明也是这样写的）。智狐运行在你自己的电脑上，没有公网地址；为了发一张截图
把文件放到公网上，等于把它交给了所有知道网址的人。所以不做，等 QQ 的单聊支持直接上传以后再加。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from functools import partial
from typing import Any

from .. import log
from ..config import CHANNEL_DOWNLOAD_BYTES, Config
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
)
from .gateway import FATAL, IDENTIFY_NEXT, RESUME_NEXT, SLOW_DOWN, Fatal, Gateway, Rules

SAFE_LIMIT = 2000  # 一条消息自己拆到这么长（文档没写上限，官方的插件按 5000 拆，这里保守一点）
GROUP_AND_C2C_EVENT = 1 << 25
PASSIVE_REPLIES = 4  # 一条用户消息最多被动回复几次
PASSIVE_SECONDS = 3540  # 被动回复的窗口是 60 分钟，留一分钟余量
MAX_RETRIES = 3
# 回复不了（被动回复的次数用完了、超时了、消息编号过期了）：改成主动消息再发一次
PASSIVE_EXPIRED = {40034128, 40034005, 304103, 40054005}

logger = logging.getLogger("harness.channels.qq")


class QQError(ChannelError):
    def __init__(self, message: str, status: int = 0, code: int = 0):
        super().__init__(message)
        self.status = status
        self.code = code


def _explain(status: int, code: int, message: str) -> str:
    if code in (100016, 100007, 10004) or status == 401:
        return t("QQ 不认这个 AppID 和 AppSecret：可能抄错了，或者 AppSecret 已经重新生成过。")
    if code == 40054013:
        return t("这个 QQ 用户不接收机器人的消息。")
    if status == 429 or code == 40034100:
        return t("发得太快，被 QQ 限流了，过一会儿会自动重试。")
    return t("QQ 回答了一个错误：{error}", error=message or code or status)


class OpenAPI:
    """QQ 机器人开放平台接口的一个极简客户端：管 access_token，发请求。可以从几个线程同时用。"""

    def __init__(self, app_id: str, secret: str, base: str = "https://api.bot.qq.com"):
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

    @property
    def last_token(self) -> str:
        """最近换来的那个 access_token，不管快不快过期（不去问 QQ）。还没换过是空字符串。"""
        with self._lock:
            return self._token

    def token(self, fresh: bool = False) -> str:
        """现在可用的 access_token。还剩不到一分钟就换一个新的（文档说最后 60 秒内去要会给一个新的，旧的还能用 60 秒）。"""
        with self._lock:
            if not fresh and self._token and time.time() < self._expires - 60:
                return self._token
            try:
                reply = http.request("POST", f"{self.base}/app/getAppAccessToken", body={"appId": self.app_id, "clientSecret": self.secret}, hide=self.secrets)
            except http.NetError as exc:
                raise QQError(t("连不上 QQ（{error}）", error=str(exc))) from None
            data = reply.data if isinstance(reply.data, dict) else {}
            if not data.get("access_token"):
                code = int(data.get("code") or 0)
                # 说明要先遮住密钥再给人看：QQ 的说明万一原样复述了请求，里面就有 AppSecret（reply.text 已经遮过，message 还没有）
                raise QQError(_explain(reply.status, code, http.scrub(str(data.get("message") or reply.text), self.secrets)), reply.status, code)
            self._token = str(data["access_token"])
            log.hide(self._token)
            try:
                self._expires = time.time() + float(str(data.get("expires_in") or 7200))  # 文档的例子里它是一个字符串
            except ValueError:
                self._expires = time.time() + 7200
            return self._token

    def call(self, method: str, path: str, body: Any = None) -> Any:
        """调用一个接口，返回解析好的 JSON。access_token 不对（401）时换一个再试一次；被限流时等一等再试。"""
        fresh = False
        for attempt in range(MAX_RETRIES + 1):
            token = self.token(fresh=fresh)
            fresh = False
            try:
                reply = http.request(method, self.base + path, headers={"Authorization": f"QQBot {token}"}, body=body, hide=self.secrets)
            except http.NetError as exc:
                raise QQError(t("连不上 QQ（{error}）", error=str(exc))) from None
            data = reply.data if isinstance(reply.data, dict) else {}
            if reply.status == 401 and attempt == 0:
                fresh = True  # access_token 可能刚好过期了
                continue
            if reply.status == 429 and attempt < MAX_RETRIES:
                time.sleep(min(max(reply.retry_after(), 1.0), 30.0))
                continue
            code = int(data.get("err_code") or data.get("code") or 0) if not reply.ok or "err_code" in data else 0
            if reply.ok and not code:
                return reply.data
            raise QQError(_explain(reply.status, code, http.scrub(str(data.get("message") or reply.text), self.secrets)), reply.status, code)
        return None


def check(config: Config, credentials: dict) -> dict:
    """检查 AppID 和 AppSecret：换得到 access_token、问得到机器人自己是谁。返回机器人的信息。"""
    app_id, secret = credentials.get("app_id", "").strip(), credentials.get("app_secret", "").strip()
    if not app_id.isdigit():
        raise ChannelError(t("AppID 应该是一串数字（在机器人的开发设置里）。"))
    if len(secret) < 8:
        raise ChannelError(t("AppSecret 太短了，看看是不是没复制全。"))
    api = OpenAPI(app_id, secret, config.qq_api)
    me = api.call("GET", "/users/@me")
    if not isinstance(me, dict) or not me.get("id"):
        raise ChannelError(t("QQ 的回答看不懂。"))
    share = me.get("share_url")
    return {"id": str(me["id"]), "username": str(me.get("username") or ""), "name": str(me.get("username") or app_id), "at": False, "link": share if isinstance(share, str) else ""}


def make(config: Config, store: Any) -> QQChannel:
    credentials = store.get("qq")["credentials"]
    return QQChannel(credentials.get("app_id", ""), credentials.get("app_secret", ""), config.qq_api)


def classify(code: int) -> tuple[str, str]:
    """网关的关闭代码 → 接下来怎么办（见 event-emit/websocket.html 的错误码表）。"""
    if code in (4914, 4915):
        return FATAL, t("QQ 说这个机器人已经下线或者被封禁了（关闭代码 {code}）。", code=code)
    if code in (4001, 4002, 4010, 4011, 4012, 4013, 4014):
        return FATAL, t("QQ 拒绝了这次连接（关闭代码 {code}）：看看机器人有没有单聊消息的权限。", code=code)
    if code == 4008:
        return SLOW_DOWN, t("发得太快，被 QQ 限流了，过一会儿会自动重试。")
    if code in (4004, 4006, 4007) or 4900 <= code <= 4913:
        return IDENTIFY_NEXT, ""
    return RESUME_NEXT, ""


class QQChannel(PushChannel):
    platform = "qq"
    capabilities = Capabilities(buttons=False, max_chars=SAFE_LIMIT, typing=False, files=True)

    def __init__(self, app_id: str, secret: str, base: str = "https://api.bot.qq.com"):
        super().__init__()
        self.api = OpenAPI(app_id, secret, base)
        self._replies: dict[str, dict] = {}  # 每个聊天最近一条用户消息：{"msg_id", "at", "seq"}，被动回复用
        self._replies_lock = threading.Lock()
        self._deliver: Callable[[Event], None] | None = None
        self.gateway = Gateway(
            Rules(name="qq", url=self._gateway_url, identify=self._identify, resume=self._resume, classify=self._classify),
            on_dispatch=self._dispatch,
            set_status=lambda state, message: self._online() if state == "online" else self._trouble(state, message),
            stopping=self._stopping,
        )

    # ---- 收 ------------------------------------------------------------------

    def _run(self, deliver: Callable[[Event], None]) -> None:
        self._deliver = deliver
        self.gateway.run()

    def _halt(self) -> None:
        self.gateway.stop()

    def _gateway_url(self) -> str:
        try:
            info = self.api.call("GET", "/gateway")
        except QQError as exc:
            if exc.code in (100016, 100007, 10004):
                raise Fatal(str(exc)) from None
            raise
        if not isinstance(info, dict) or not isinstance(info.get("url"), str):
            raise ChannelError(t("QQ 的回答看不懂。"))
        return info["url"]

    def _login_token(self) -> str:
        """Identify、Resume 里带的 token。

        这两步是在网关的线程里、连接已经开着的时候做的，gateway.py 只接住连接本身的错误（WebSocketError）。
        token 快过期了要换一个、偏偏这时连不上 QQ 的话，QQError 会让收消息的线程整个退出，再也不重连。
        所以换不到新的，就先用手上的那个（_gateway_url 刚用它问过地址，多半还能用）：它真的不能用了，
        QQ 会用 4004 关掉连接，_classify 那时再换。"""
        try:
            return self.api.token()
        except QQError:
            return self.api.last_token

    def _identify(self) -> dict:
        return {"token": f"QQBot {self._login_token()}", "intents": GROUP_AND_C2C_EVENT, "shard": [0, 1], "properties": {}}

    def _resume(self, session_id: str, seq: int) -> dict:
        return {"token": f"QQBot {self._login_token()}", "session_id": session_id, "seq": seq}

    def _classify(self, code: int) -> tuple[str, str]:
        if code == 4004:  # access_token 不对了：下次登录前换一个新的
            try:
                self.api.token(fresh=True)
            except QQError:
                pass
        return classify(code)

    def _dispatch(self, kind: str, data: dict) -> None:
        deliver = self._deliver
        if deliver is None or self._stopping.is_set():
            return
        if kind == "C2C_MESSAGE_CREATE":
            event = self._message(data, private=True)
        elif kind == "GROUP_AT_MESSAGE_CREATE":
            event = self._message(data, private=False)  # 群里 @ 机器人：router 会忽略它
        else:
            return
        if event is not None:
            deliver(event)

    def _message(self, data: dict, private: bool) -> Inbound | None:
        message_id = str(data.get("id") or "")
        if not self._first_time(message_id):
            return None
        author = who if isinstance(who := data.get("author"), dict) else {}
        openid = str(author.get("user_openid") or author.get("member_openid") or author.get("id") or "")
        if not openid:
            return None
        chat = openid if private else str(data.get("group_openid") or "")
        if private and message_id:
            with self._replies_lock:
                self._replies[chat] = {"msg_id": message_id, "at": time.time(), "seq": 0}
        username = author.get("username")
        inbound = Inbound(
            platform=self.platform,
            chat=chat,
            user=openid,
            # 单聊的消息里没有名字，用 openid 的最后几位。str()：t() 给的是 i18n.Message，它会被存进 channels.json 的主人信息里，
            # 而 ChannelStore.get 要 deepcopy 整份设置，Message 复制不了（少了 key、values 两个参数），配对时会出错
            name=str(username) if isinstance(username, str) and username else str(t("QQ 用户 {id}", id=openid[-6:])),
            text=str(data.get("content") or "").strip(),
            private=private,
        )
        for item in data.get("attachments") or []:
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                continue
            kind = str(item.get("content_type") or "")
            if kind == "voice" or kind.startswith("video"):
                inbound.unsupported = inbound.unsupported or ("voice" if kind == "voice" else "video")
                continue
            url = item["url"]
            url = "https:" + url if url.startswith("//") else url
            inbound.files.append(
                InboundFile(
                    name=str(item.get("filename") or "file"),
                    media_type=kind if "/" in kind else "application/octet-stream",
                    size=item.get("size") if isinstance(item.get("size"), int) else None,
                    kind="photo" if kind.startswith("image/") else "document",
                    fetch=partial(self._download, url),  # partial 当场绑住这一个附件的地址（循环变量会变）
                )
            )
        if inbound.files or inbound.text:
            inbound.unsupported = ""
        elif not inbound.unsupported:
            return None
        return inbound

    def _download(self, url: str) -> bytes:
        if not http.trusted(url):
            raise ChannelError(t("没能下载这个文件。"))
        try:
            return http.download(url, limit=CHANNEL_DOWNLOAD_BYTES)
        except http.NetError as exc:
            raise ChannelError(str(exc)) from None

    # ---- 发 ------------------------------------------------------------------

    def _post(self, chat: str, content: str) -> dict:
        """发一条文字。能被动回复就被动回复（带着用户消息的编号），不能了改成主动消息。"""
        body: dict = {"content": content, "msg_type": 0}
        with self._replies_lock:
            reply = self._replies.get(chat)
            passive = reply is not None and reply["seq"] < PASSIVE_REPLIES and time.time() - reply["at"] < PASSIVE_SECONDS
            if passive and reply is not None:
                reply["seq"] += 1
                body.update(msg_id=reply["msg_id"], msg_seq=reply["seq"])
        try:
            sent = self.api.call("POST", f"/v2/users/{chat}/messages", body)
        except QQError as exc:
            if not passive or exc.code not in PASSIVE_EXPIRED:
                raise
            with self._replies_lock:
                self._replies.pop(chat, None)
            sent = self.api.call("POST", f"/v2/users/{chat}/messages", {"content": content, "msg_type": 0})
        return sent if isinstance(sent, dict) else {}

    def send(self, chat: str, text: str, markdown: bool = False) -> None:
        for piece in markup.split(text, SAFE_LIMIT):
            content = markup.to_plain(piece) if markdown else piece
            if content.strip():
                self._post(chat, content)

    def ask(self, chat: str, prompt: Prompt) -> str:
        """确认请求：标题、完整的命令、工作目录，最后一句"回复 允许 或 拒绝"。太长的话前面几段先各发一条。"""
        tail = "\n\n".join(part for part in (prompt.where, prompt.hint) if part)
        room = SAFE_LIMIT - markup.length(prompt.title) - markup.length(tail) - 8
        parts = markup.split(prompt.detail, max(400, room)) or [""]
        for index, part in enumerate(parts[:-1]):
            self._post(chat, (prompt.title + "\n\n" if index == 0 else "") + part)
        last = "\n\n".join(item for item in ((prompt.title if len(parts) == 1 else ""), parts[-1], tail) if item)
        sent = self._post(chat, last)
        return str(sent.get("id") or f"qq-{prompt.id}")  # 没有按钮要去掉；有个编号就说明发出去了

    def settle(self, chat: str, handle: str, outcome: str) -> None:
        if handle:
            self._post(chat, outcome)  # 消息改不了：另发一条说明结果
