"""Discord：手写的 Gateway（WebSocket）加 REST 客户端，和一个 Discord 通道。

照着官方文档写的（2026 年 11 月看的；旧的 discord.com/developers/docs 会跳到这里）：
  Gateway          https://docs.discord.com/developers/events/gateway
  关闭代码          https://docs.discord.com/developers/topics/opcodes-and-status-codes
  发消息、改消息    https://docs.discord.com/developers/resources/message
  按钮              https://docs.discord.com/developers/components/reference
  回应按钮和命令    https://docs.discord.com/developers/interactions/receiving-and-responding
  上传文件          https://docs.discord.com/developers/reference#uploading-files
                   https://docs.discord.com/developers/resources/message#create-message
  斜杠命令          https://docs.discord.com/developers/interactions/application-commands
  限流              https://docs.discord.com/developers/topics/rate-limits

怎么收消息：问 GET /gateway/bot 要网关的地址，连上 WebSocket，登录时只要 DIRECT_MESSAGES（1 << 12）这一类事件：
机器人只收私聊。读私聊的内容不需要 Message Content 这个"特权"（文档原话：它的限制不包括 "Content in DMs with the app"），
所以后台里不用打开它。连接、心跳、断了重连、Resume，见 gateway.py（QQ 也是这一套）。

收到的东西：
  MESSAGE_CREATE      一条消息。没有 guild_id 的是私聊（机器人进不了群聊式的私信）；自己发的和别的机器人发的不理
  INTERACTION_CREATE  有人点了按钮（type 3，custom_id 是 "ok:12"），或者用了斜杠命令（type 2，/new 这些）。
                      这两种都必须在 3 秒内回应（POST /interactions/{id}/{token}/callback），不然对方会看到"这个交互失败了"：
                      按钮回 type 6（"收到了，消息待会儿再改"），命令回 type 4（一句话）

怎么发：POST /channels/{id}/messages。一条最多 2000 个字符，更长的拆开；allowed_mentions 是空的，模型写了 @everyone
也不会真的去 @ 谁。确认请求带两个按钮（action row 里的两个 button），答复以后 PATCH 那条消息，把按钮去掉、写上结果。
"正在输入"是 POST /channels/{id}/typing，维持 10 秒。

发文件（send_file 工具）：同一个 POST /channels/{id}/messages，只是请求体换成 multipart/form-data：消息本身写成 JSON
放在 payload_json 这一段里，文件放在 files[0]，payload_json 里的 attachments 用编号 0 指着它（文档的写法）。
图片和别的文件走同一条路：Discord 自己会把图片直接显示在聊天里。一个文件最大 20 MiB（文档："The default limit is
20 MiB"，有 Nitro、服务器加成的会更大，机器人按默认的算），整个请求最大 25 MiB；超了 Discord 回 413（错误编号 40005）。
说明写在这条消息的 content 里（最多 2000 个字符），更长的话文件先发，说明再另发几条。

斜杠命令：用 PUT /applications/{应用的编号}/commands 一次登记全部（只在和机器人的私聊里出现，contexts=[1]）。
登记了以后，在私聊里打 /new 会弹出命令的菜单，选了就是一次 type 2 的交互。

token 写在每个请求的 Authorization 头里，出错时给人看的话里一律换掉（hide），日志里也一样（见 ../log.py）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from functools import partial
from typing import Any

from .. import log
from ..config import CHANNEL_DOWNLOAD_BYTES, CHANNEL_SEND_RETRY_AFTER_MAX, Config
from ..i18n import t
from ..net import http, multipart
from ..safe import to_json
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
    pause,
    transient,
)
from .gateway import FATAL, IDENTIFY_NEXT, RESUME_NEXT, Fatal, Gateway, Rules, Wait

LIMIT = 2000  # 一条消息最多这么多个字符
SAFE_LIMIT = 1900  # 自己拆的时候留一点余量（代码块被拆开时要补上 ```）
DIRECT_MESSAGES = 1 << 12  # Gateway 的 intents：只收私聊里的消息
MAX_RETRIES = 3
EPHEMERAL = 1 << 6  # 只有点按钮的那个人看得到的回应
# token 的样子：三段用点隔开的 base64（第一段是机器人编号的 base64）。只检查样子，对不对要问 Discord
_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,40}\.[A-Za-z0-9_-]{4,10}\.[A-Za-z0-9_-]{20,80}")
# Discord 要求的 User-Agent 写法：DiscordBot (网址, 版本)
USER_AGENT = "DiscordBot (https://github.com/Akxan/zhifox-agent-harness, 0.1) ZhiFox"

logger = logging.getLogger("harness.channels.discord")


class DiscordError(ChannelError):
    """status 是 HTTP 状态码（0：没得到回答；None：不是一次请求出的错，见 base.ChannelError）。"""

    def __init__(self, message: str, status: int | None = None, code: int = 0, retry_after: float = 0.0):
        super().__init__(message, status=status, retry_after=retry_after)
        self.code = code  # Discord 自己的错误编号（比如 50007：不能给这个人发私信）


def valid_token(token: str) -> bool:
    return bool(_TOKEN.fullmatch(token))


class RestAPI:
    """Discord REST 接口的一个极简客户端。可以从几个线程同时用。"""

    def __init__(self, token: str, base: str = "https://discord.com/api/v10"):
        self.token = token
        self.base = base.rstrip("/")
        log.hide(token)

    def call(self, method: str, path: str, body: Any = None, auth: bool = True) -> Any:
        """调用一个接口，返回解析好的 JSON（204 没有内容时是 None）。被限流时按 Discord 说的时间等一等再试。"""
        headers = {"User-Agent": USER_AGENT}
        if auth:
            headers["Authorization"] = f"Bot {self.token}"
        return self._answer(lambda: http.request(method, self.base + path, headers=headers, body=body, hide=(self.token,)))

    def upload(self, path: str, payload: dict, file: multipart.File) -> Any:
        """带着一个文件 POST（发文件的那条消息）：payload 是消息本身（写进 payload_json），file 的 field 是 files[0]。
        出错、被限流和 call 一样处理。"""
        headers = {"User-Agent": USER_AGENT, "Authorization": f"Bot {self.token}"}
        fields = {"payload_json": to_json(payload)}
        return self._answer(lambda: multipart.post(self.base + path, fields=fields, files=[file], headers=headers, hide=(self.token,)))

    def _answer(self, send: Callable[[], http.Reply]) -> Any:
        """发一次请求（send），看回答：2xx 返回解析好的 JSON；429 按 Discord 说的时间等一等再发一次；别的抛出 DiscordError。"""
        for attempt in range(MAX_RETRIES + 1):
            try:
                reply = send()
            except http.NetError as exc:
                raise DiscordError(t("连不上 Discord（{error}）", error=str(exc)), exc.status) from None
            data = reply.data if isinstance(reply.data, dict) else {}
            wait = _retry_after(data, reply) if reply.status == 429 else 0.0
            if reply.status == 429 and attempt < MAX_RETRIES:
                pause(min(max(wait or 1.0, 0.5), CHANNEL_SEND_RETRY_AFTER_MAX))
                continue
            if reply.ok:
                return reply.data
            raise DiscordError(_explain(reply.status, int(data.get("code") or 0), http.scrub(str(data.get("message") or reply.text), (self.token,))), reply.status, int(data.get("code") or 0), wait)
        return None


def _retry_after(data: dict, reply: http.Reply) -> float:
    """被限流时 Discord 说的"过多久再来"（秒）：回答里的 retry_after，没有就看 Retry-After 头。"""
    try:
        return max(float(data.get("retry_after") or 0), reply.retry_after())
    except (TypeError, ValueError):
        return reply.retry_after()


def _explain(status: int, code: int, message: str) -> str:
    if status == 413 or code == 40005:
        return t("这个文件太大了，Discord 不让机器人发这么大的文件。")
    if status == 401:
        return t("Discord 不认这个 token：可能抄错了，或者已经在开发者后台点过 Reset Token。")
    if status == 429:
        return t("发得太快，被 Discord 限流了，过一会儿会自动重试。")
    if code == 50007:
        return t("Discord 不让机器人给这个人发私信（他可能关掉了私信，或者和机器人没有共同的服务器）。")
    if status == 403:
        return t("Discord 拒绝了：机器人没有这个权限。")
    return t("Discord 回答了一个错误：{error}", error=message or status)


def check(config: Config, credentials: dict) -> dict:
    """检查 token：样子对不对、Discord 认不认。认的话返回机器人的信息（hub.connect 会把它记进 channels.json）。"""
    token = credentials.get("token", "").strip()
    if not valid_token(token):
        raise ChannelError(t("这不像是一个 Discord 机器人的 token。它是三段用点隔开的字母数字，在开发者后台的 Bot 页面点 Reset Token 得到。"))
    api = RestAPI(token, config.discord_api)
    me = api.call("GET", "/users/@me")
    app = api.call("GET", "/oauth2/applications/@me")
    if not isinstance(me, dict) or not me.get("id") or not isinstance(app, dict) or not app.get("id"):
        raise ChannelError(t("Discord 的回答看不懂。"))
    app_id = str(app["id"])
    return {
        "id": str(me["id"]),
        "username": str(me.get("username") or ""),
        "name": str(me.get("global_name") or me.get("username") or app.get("name") or ""),
        "app": app_id,
        # 把机器人加到你自己的账号（User Install）：加了以后就能和它私聊，不需要共同的服务器
        "install": f"https://discord.com/oauth2/authorize?client_id={app_id}&integration_type=1&scope=applications.commands",
        "link": f"https://discord.com/users/{me['id']}",
    }


def make(config: Config, store: Any) -> DiscordChannel:
    entry = store.get("discord")
    return DiscordChannel(entry["credentials"].get("token", ""), config.discord_api, app_id=(entry["bot"] or {}).get("app", ""))


def classify(code: int) -> tuple[str, str]:
    """Gateway 的关闭代码 → 接下来怎么办（见 opcodes-and-status-codes 那一页的表）。"""
    if code == 4004:
        return FATAL, t("Discord 不认这个 token：可能抄错了，或者已经在开发者后台点过 Reset Token。")
    if code in (4013, 4014):
        return FATAL, t("Discord 不让这个机器人收私聊的消息（intents 不对）。")
    if code in (4010, 4011, 4012):
        return FATAL, t("Discord 拒绝了这次连接（关闭代码 {code}）。", code=code)
    if code in (4003, 4007, 4009):
        return IDENTIFY_NEXT, ""
    return RESUME_NEXT, ""


def _person(user: dict) -> str:
    name, username = user.get("global_name"), user.get("username")
    if isinstance(name, str) and name and isinstance(username, str) and username and name != username:
        return f"{name} (@{username})"
    return str(username or name or user.get("id", ""))


# 能往聊天里发什么（见 base.Uploads）：一个文件最大 20 MiB，图片也是（Discord 自己会把图片显示出来）。
# 按 20 MB 算，比文档的 20 MiB（20 971 520 字节）少一点：给 payload_json 和各段的头留出余量，告诉模型的上限也是整数
UPLOADS = Uploads(file_bytes=20_000_000, photo_bytes=20_000_000, caption_chars=LIMIT)


class DiscordChannel(PushChannel):
    platform = "discord"
    capabilities = Capabilities(buttons=True, max_chars=SAFE_LIMIT, typing=True, files=True, uploads=UPLOADS)

    def __init__(self, token: str, base: str = "https://discord.com/api/v10", app_id: str = ""):
        super().__init__()
        self.api = RestAPI(token, base)
        self.token = token
        self.app_id = app_id
        self.me = ""  # 机器人自己的编号（READY 里有）
        self._asked: dict[str, str] = {}  # 发出去的确认请求：消息编号 → 那条消息的内容（答复以后在后面写上结果）
        self._descriptions: dict[str, str] = {}  # 登记过的命令和说明（用了命令时回的那一句）
        self._deliver: Callable[[Event], None] | None = None
        self.gateway = Gateway(
            Rules(name="discord", url=self._gateway_url, identify=self._identify, resume=self._resume, classify=classify, query="?v=10&encoding=json"),
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
            info = self.api.call("GET", "/gateway/bot")
        except DiscordError as exc:
            if exc.status == 401:
                raise Fatal(str(exc)) from None
            raise
        if not isinstance(info, dict) or not isinstance(info.get("url"), str):
            raise ChannelError(t("Discord 的回答看不懂。"))
        limit = given if isinstance(given := info.get("session_start_limit"), dict) else {}
        if limit.get("remaining") == 0:  # 今天登录的次数用完了：再登录会让 token 作废
            seconds = max(60.0, float(limit.get("reset_after") or 0) / 1000)
            raise Wait(t("今天连接 Discord 的次数用完了，{minutes} 分钟以后再连。", minutes=round(seconds / 60)), seconds)
        return info["url"].rstrip("/") + "/"

    def _identify(self) -> dict:
        return {"token": self.token, "intents": DIRECT_MESSAGES, "properties": {"os": "linux", "browser": "zhifox", "device": "zhifox"}}

    def _resume(self, session_id: str, seq: int) -> dict:
        return {"token": self.token, "session_id": session_id, "seq": seq}

    def _dispatch(self, kind: str, data: dict) -> None:
        deliver = self._deliver
        if kind == "READY":
            user = me if isinstance(me := data.get("user"), dict) else {}
            self.me = str(user.get("id", ""))
            application = app if isinstance(app := data.get("application"), dict) else {}
            self.app_id = self.app_id or str(application.get("id", ""))
            return
        if deliver is None or self._stopping.is_set():
            return
        event: Event | None = None
        if kind == "MESSAGE_CREATE":
            event = self._message(data)
        elif kind == "INTERACTION_CREATE":
            event = self._interaction(data)
        if event is not None:
            deliver(event)

    def _message(self, data: dict) -> Inbound | None:
        author = who if isinstance(who := data.get("author"), dict) else {}
        if not author or author.get("bot") or str(author.get("id", "")) == self.me:
            return None  # 自己发的（Discord 也会推回来）、别的机器人发的：不理
        inbound = Inbound(
            platform=self.platform,
            chat=str(data.get("channel_id", "")),
            user=str(author.get("id", "")),
            name=_person(author),
            text=str(data.get("content") or ""),
            private="guild_id" not in data,
        )
        for item in data.get("attachments") or []:
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                continue
            kind = "photo" if str(item.get("content_type") or "").startswith("image/") else "document"
            url, size = item["url"], item.get("size") if isinstance(item.get("size"), int) else None
            inbound.files.append(
                InboundFile(
                    name=str(item.get("filename") or "file"),
                    media_type=str(item.get("content_type") or "application/octet-stream"),
                    size=size,
                    kind=kind,
                    fetch=partial(self._download, url, size),  # partial 当场绑住这一个附件的（循环变量会变）
                )
            )
        if not inbound.text and not inbound.files:
            if data.get("sticker_items"):
                inbound.unsupported = "sticker"
            else:
                return None  # 系统消息（置顶、通话……）
        return inbound

    def _download(self, url: str, size: Any) -> bytes:
        if isinstance(size, int) and size > CHANNEL_DOWNLOAD_BYTES:
            raise ChannelError(t("这个文件太大了，最多 {size} MB。", size=CHANNEL_DOWNLOAD_BYTES // 1_000_000))
        if not http.trusted(url):
            raise ChannelError(t("没能下载这个文件。"))
        try:
            return http.download(url, limit=CHANNEL_DOWNLOAD_BYTES)  # 附件的地址是签过名的，不用带 token
        except http.NetError as exc:
            raise ChannelError(str(exc)) from None

    def _interaction(self, data: dict) -> Event | None:
        interaction_id, token = str(data.get("id", "")), str(data.get("token", ""))
        user = who if isinstance(who := data.get("user"), dict) else None
        if user is None and isinstance(member := data.get("member"), dict):
            user = who if isinstance(who := member.get("user"), dict) else None
        user = user or {}
        chat = str(data.get("channel_id", ""))
        private = "guild_id" not in data
        payload = given if isinstance(given := data.get("data"), dict) else {}
        if data.get("type") == 3:  # 按钮
            return Press(
                platform=self.platform,
                chat=chat if private else "",
                user=str(user.get("id", "")),
                name=_person(user),
                data=str(payload.get("custom_id") or ""),
                answer=lambda text, problem=False: self._answer(interaction_id, token, text if problem else ""),
            )
        if data.get("type") == 2:  # 斜杠命令
            name = str(payload.get("name") or "")
            if not private:
                self._answer(interaction_id, token, t("只能在和机器人的私聊里用。"))
                return None
            # 必须马上回应。真正的回答由 router 照常发到聊天里，这里只回一行灰色的小字（-# 是 Discord 的"小字"写法）
            self._respond(interaction_id, token, {"type": 4, "data": {"content": "-# " + (self._descriptions.get(name) or "/" + name), "allowed_mentions": {"parse": []}}})
            # 命令带的参数（/new 的 agent）接在后面：和打字写成 "/new 资料研究员" 是一样的
            options = [str(option.get("value")) for option in payload.get("options") or [] if isinstance(option, dict) and option.get("value")]
            text = " ".join(["/" + name, *options])
            return Inbound(platform=self.platform, chat=chat, user=str(user.get("id", "")), name=_person(user), text=text, private=True)
        return None

    def _answer(self, interaction_id: str, token: str, text: str) -> None:
        """回应一次按钮：没有要说的就回 type 6（收到了，不改消息）；有的话回一句只有他看得到的话。"""
        if text:
            self._respond(interaction_id, token, {"type": 4, "data": {"content": text, "flags": EPHEMERAL}})
        else:
            self._respond(interaction_id, token, {"type": 6})

    def _respond(self, interaction_id: str, token: str, body: dict) -> None:
        try:
            self.api.call("POST", f"/interactions/{interaction_id}/{token}/callback", body, auth=False)
        except DiscordError as exc:
            logger.info("discord: 没能回应一次交互（%s）", exc)

    # ---- 发 ------------------------------------------------------------------

    def _post(self, chat: str, content: str, components: list | None = None) -> dict:
        body: dict = {"content": content, "allowed_mentions": {"parse": []}}
        if components is not None:
            body["components"] = components
        sent = self.api.call("POST", f"/channels/{chat}/messages", body)
        return sent if isinstance(sent, dict) else {}

    def send(self, chat: str, text: str, markdown: bool = False) -> None:
        for piece in markup.split(text, SAFE_LIMIT):
            shaped = markup.to_discord(piece) if markdown else piece
            # 排版以后可能变长：代码块里的每个 ``` 都要插进一个看不见的字符（fence_safe），一段代码里这样的地方多了，
            # 1900 个字排完能有 2200 个，Discord 会整条拒绝。还超的话按排好的样子再拆一次（split 照样会补齐代码块的围栏）
            for part in markup.split(shaped, LIMIT) if markup.length(shaped) > LIMIT else [shaped]:
                self._post(chat, part)

    def ask(self, chat: str, prompt: Prompt) -> str:
        """确认请求：粗体的标题、代码块里的完整命令、工作目录，下面两个按钮（太长的话前面几段先各发一条）。"""
        head = f"**{prompt.title}**\n"
        tail = f"\n{prompt.where}" if prompt.where else ""
        room = SAFE_LIMIT - markup.length(head) - markup.length(tail) - 12
        # 先把命令里的 ``` 换成关不上代码块的样子，再按换完的长度拆，为什么要这个顺序：
        #   - 插进去的字符算在长度里，拆出来的每一段放进代码块以后不会超过 2000 个字
        #   - 换完以后命令里没有 ``` 了，split 不会把它们当成代码块的边界，在拆开的地方补上命令里本来没有的 ``` 行
        #     （你要批准的是哪些字，聊天里显示的就是哪些字，一行不多）
        parts = markup.split(markup.fence_safe(prompt.detail), max(400, room)) or [""]
        for index, part in enumerate(parts[:-1]):
            self._post(chat, (head if index == 0 else "") + _block(part))
        content = (head if len(parts) == 1 else "") + (_block(parts[-1]) if parts[-1] else "") + tail
        buttons = [{"type": 1, "components": [
            {"type": 2, "style": 3, "label": prompt.allow[:80], "custom_id": f"ok:{prompt.id}"},
            {"type": 2, "style": 4, "label": prompt.deny[:80], "custom_id": f"no:{prompt.id}"},
        ]}]
        sent = self._post(chat, content, buttons)
        handle = str(sent.get("id", ""))
        if handle:
            self._asked[handle] = content
        return handle

    def settle(self, chat: str, handle: str, outcome: str) -> None:
        # 连不上这类暂时的失败原样抛出，由发送队列再改一次（所以改成了才从 _asked 里拿掉，见 telegram.py 的 settle）
        content = self._asked.get(handle)
        if not handle or content is None:
            return
        try:
            self.api.call("PATCH", f"/channels/{chat}/messages/{handle}", {"content": f"{content}\n\n**{outcome}**", "components": [], "allowed_mentions": {"parse": []}})
        except DiscordError as exc:
            if transient(exc):
                raise
            self._post(chat, outcome)  # 改不了：另发一条说明结果
        self._asked.pop(handle, None)

    def send_file(self, chat: str, outgoing: OutboundFile) -> None:
        """发一个文件：一条带附件的消息（见文件开头）。说明排成 Discord 的样子写在 content 里，放不下的文件先发、说明另发。"""
        caption = outgoing.caption.strip()
        shaped = markup.to_discord(caption) if caption else ""
        inline = shaped if shaped and markup.length(shaped) <= UPLOADS.caption_chars else ""
        payload = {"content": inline, "allowed_mentions": {"parse": []}, "attachments": [{"id": 0, "filename": outgoing.name}]}
        self.api.upload(f"/channels/{chat}/messages", payload, multipart.File("files[0]", outgoing.name, outgoing.media_type, outgoing.data))
        if caption and not inline:
            self.send(chat, caption, markdown=True)

    def typing(self, chat: str) -> None:
        try:
            self.api.call("POST", f"/channels/{chat}/typing")
        except DiscordError:
            pass  # 只是个"正在输入"，没发出去不要紧

    def set_commands(self, commands: dict[str, str]) -> None:
        self._descriptions = dict(commands)
        if not self.app_id:
            return
        body = [{"name": name, "description": text[:100] or name, "type": 1, "contexts": [1], "integration_types": [0, 1]} for name, text in commands.items()]
        for command in body:
            if command["name"] == "new":  # /new 后面可以跟一个 Agent 的名字：在 Discord 里是命令的一个可选参数
                command["options"] = [{"type": 3, "name": "agent", "description": t("用哪个 Agent 当角色（不填就是普通的对话）")[:100], "required": False}]
        try:
            self.api.call("PUT", f"/applications/{self.app_id}/commands", body)
        except DiscordError as exc:
            logger.info("discord: 没能登记斜杠命令（%s）", exc)


def _block(text: str) -> str:
    """一段命令放进代码块。text 已经 fence_safe 过了（见 ask）。"""
    return f"```\n{text}\n```"

