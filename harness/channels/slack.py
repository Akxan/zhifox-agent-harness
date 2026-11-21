"""Slack：手写的 Socket Mode（WebSocket）加 Web API 客户端，和一个 Slack 通道。

照着官方文档写的（2026 年 11 月看的）：
  Socket Mode       https://docs.slack.dev/apis/events-api/using-socket-mode
  私聊里的消息       https://docs.slack.dev/reference/events/message.im
  按钮              https://docs.slack.dev/reference/block-kit/block-elements/button-element
  点了按钮           https://docs.slack.dev/reference/interaction-payloads/block_actions-payload
  斜杠命令           https://docs.slack.dev/interactivity/implementing-slash-commands
  mrkdwn 的写法      https://docs.slack.dev/messaging/formatting-message-text
  限流              https://docs.slack.dev/apis/web-api/rate-limits

要两个 token：
  Bot token（xoxb-）       以机器人的身份调 Web API：发消息（chat.postMessage）、改消息（chat.update）、下载文件
  App-level token（xapp-） 只用来开 Socket Mode 的连接（apps.connections.open，要 connections:write 这个权限）

怎么收消息：用 App token 调 apps.connections.open，拿到一个一次性的 wss:// 地址，连上去。Slack 把事件包成一个个
"信封"推过来：{"envelope_id", "type", "payload"}。每个信封都要马上回一个 {"envelope_id"}（ack），不然 Slack 会再推一遍。
  hello            连上了
  events_api       事件。我们只订了 message.im（和机器人的私聊里来了一条消息）
  interactive      有人点了按钮（block_actions，按钮的 value 是 "ok:12"）
  slash_commands   有人用了 /zhifox 命令
  disconnect       Slack 要换一条连接（每隔几个小时一次），或者 Socket Mode 被关掉了（link_disabled）。前一种马上重连

为什么命令是 /zhifox new：Slack 会把所有 / 开头的话都当成它自己的命令拦下来，没登记过的直接报错，发不到机器人这里；
/status 这类名字它自己就有。所以应用的清单里只登记一个 /zhifox（见 guides.py），后面跟着要做的事。

怎么发：chat.postMessage，文字排成 mrkdwn（见 markup.to_mrkdwn）；确认请求是 Block Kit：标题、代码块、工作目录，
加一排两个按钮，答复以后用 chat.update 换成去掉按钮、写着结果的样子。Slack 没有给机器人用的"正在输入"。

Slack 的 Web API 出错时 HTTP 状态码多半还是 200，回答是 {"ok": false, "error": "invalid_auth"}，要看 ok。
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

from .. import log
from ..config import (
    CHANNEL_DOWNLOAD_BYTES,
    CHANNEL_IDLE_SECONDS,
    CHANNEL_RETRY_SECONDS,
    CHANNEL_TIMEOUT,
    Config,
)
from ..i18n import t
from ..net import http
from ..net.websocket import ConnectionClosed, WebSocket, WebSocketError
from ..safe import to_json
from . import markup
from .base import (
    Capabilities,
    ChannelError,
    Event,
    Inbound,
    InboundFile,
    Press,
    Prompt,
    PushChannel,
    backoff,
)
from .guides import SLACK_COMMAND

SAFE_LIMIT = 3000  # 一条消息先拆到这么长（排成 mrkdwn 时 & < > 会变长，留出余量）
MESSAGE_LIMIT = 4000  # 排好以后一条消息最长这么多（Slack 建议 text 不超过 4000 个字符，再长显示时会被截断）
SECTION_LIMIT = 2900  # 一个 section 块里的文字最多 3000 个字符
SECTIONS_PER_MESSAGE = 20  # 一条消息最多 50 个块，确认请求的代码太长时，每条放这么多段
MAX_RETRIES = 3
COMMANDS = ("new", "stop", "status", "help", "start")
# token 坏了、被收回了这类：再试也没用，等你换 token
BAD_TOKEN = {"invalid_auth", "not_authed", "account_inactive", "token_revoked", "token_expired", "not_allowed_token_type"}

logger = logging.getLogger("harness.channels.slack")


class SlackError(ChannelError):
    def __init__(self, message: str, error: str = "", status: int = 0):
        super().__init__(message)
        self.error = error  # Slack 自己的错误名（invalid_auth……）
        self.status = status


def _explain(error: str, method: str, needed: str = "") -> str:
    if error in BAD_TOKEN:
        if method == "apps.connections.open":
            return t("Slack 不认这个 App-level token（xapp- 开头）：可能抄错了，或者它没有 connections:write 这个权限。")
        return t("Slack 不认这个 Bot token（xoxb- 开头）：可能抄错了，或者应用被重新安装过、token 换了。")
    if error == "missing_scope":
        return t("Slack 应用缺少权限 {scope}：按向导里的清单建应用，或者在 OAuth & Permissions 里加上它，再重新安装。", scope=needed or "?")
    if error == "ratelimited":
        return t("发得太快，被 Slack 限流了，过一会儿会自动重试。")
    if error == "channel_not_found":
        return t("Slack 找不到这个聊天。")
    return t("Slack 回答了一个错误：{error}", error=error or "?")


class WebAPI:
    """Slack Web API 的一个极简客户端。可以从几个线程同时用。"""

    def __init__(self, bot_token: str, app_token: str, base: str = "https://slack.com/api"):
        self.bot_token = bot_token
        self.app_token = app_token
        self.base = base.rstrip("/")
        log.hide(bot_token)
        log.hide(app_token)

    @property
    def secrets(self) -> tuple[str, str]:
        return (self.bot_token, self.app_token)

    def call(self, method: str, body: dict | None = None, query: dict | None = None, app: bool = False) -> dict:
        """调用一个方法（有 body 用 POST 发 JSON，没有用 GET 带 query）。返回回答；ok 不是 true 时抛出 SlackError。"""
        token = self.app_token if app else self.bot_token
        url = f"{self.base}/{method}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
        for attempt in range(MAX_RETRIES + 1):
            try:
                reply = http.request("POST" if body is not None or app else "GET", url, headers={"Authorization": f"Bearer {token}"}, body=body if body is not None else ({} if app else None), hide=self.secrets)
            except http.NetError as exc:
                raise SlackError(t("连不上 Slack（{error}）", error=str(exc))) from None
            data = reply.data if isinstance(reply.data, dict) else {}
            if (reply.status == 429 or data.get("error") == "ratelimited") and attempt < MAX_RETRIES:
                time.sleep(min(max(reply.retry_after(), 1.0), 30.0))
                continue
            if data.get("ok") is True:
                return data
            # 错误名原样写进说明（"Slack 回答了一个错误：…"）、进日志：万一里面带着 token（出了毛病的代理把请求头弹了回来），先遮住
            error = http.scrub(str(data.get("error") or (f"HTTP {reply.status}" if not reply.ok else "?")), self.secrets)
            raise SlackError(_explain(error, method, http.scrub(str(data.get("needed") or ""), self.secrets)), error, reply.status)
        return {}


def check(config: Config, credentials: dict) -> dict:
    """检查两个 token：样子对不对、Slack 认不认。认的话返回机器人的信息。"""
    bot, app = credentials.get("bot_token", "").strip(), credentials.get("app_token", "").strip()
    if not bot.startswith("xoxb-"):
        raise ChannelError(t("Bot token 应该是 xoxb- 开头的（在 OAuth & Permissions 页面）。"))
    if not app.startswith("xapp-"):
        raise ChannelError(t("App-level token 应该是 xapp- 开头的（在 Basic Information 页面的 App-Level Tokens 里）。"))
    api = WebAPI(bot, app, config.slack_api)
    me = api.call("auth.test", {})
    info = api.call("bots.info", query={"bot": str(me.get("bot_id", ""))}).get("bot") or {}
    api.call("apps.connections.open", app=True)  # App token 能不能开连接（拿到的地址不用，过一会儿自己就作废了）
    app_id, team = str(info.get("app_id") or ""), str(me.get("team_id") or "")
    return {
        "id": str(me.get("user_id") or ""),
        "username": str(me.get("user") or ""),
        "name": str(info.get("name") or me.get("user") or ""),
        "app": app_id,
        "link": f"https://slack.com/app_redirect?app={app_id}&team={team}" if app_id and team else "",
    }


def make(config: Config, store: Any) -> SlackChannel:
    entry = store.get("slack")
    credentials = entry["credentials"]
    return SlackChannel(credentials.get("bot_token", ""), credentials.get("app_token", ""), config.slack_api)


_LINK = re.compile(r"<((?:https?|mailto):[^|>]+)(?:\|([^>]*))?>")


def from_slack(text: str) -> str:
    """Slack 发来的文字是转义过的（&lt;……），链接写成 <网址|文字>：换回平常的样子交给模型。

    文字就是网址本身（你打了 example.com，Slack 自动加上链接）的，只留文字；你给一段字加了链接的，
    写成 Markdown 的 [文字](网址)：只留文字的话，模型就不知道链接指向哪里了。"""

    def unlink(match: re.Match) -> str:
        url, label = match.group(1), match.group(2)
        if not label or label in (url, url.split("://", 1)[-1], url.removeprefix("mailto:")):
            return label or url
        return f"[{label}]({url})"

    text = _LINK.sub(unlink, text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


class SlackChannel(PushChannel):
    platform = "slack"
    capabilities = Capabilities(buttons=True, max_chars=SAFE_LIMIT, typing=False, files=True, command=SLACK_COMMAND + " {name}")

    def __init__(self, bot_token: str, app_token: str, base: str = "https://slack.com/api"):
        super().__init__()
        self.api = WebAPI(bot_token, app_token, base)
        self._ws: WebSocket | None = None
        self._asked: dict[str, list] = {}  # 发出去的确认请求：消息的 ts → 它的块（答复以后换掉按钮）
        self._names: dict[str, str] = {}  # 用户编号 → 给人看的名字
        self._dms: dict[str, str] = {}  # 用户编号 → 他和机器人的私聊的编号

    # ---- 收 ------------------------------------------------------------------

    def _halt(self) -> None:
        ws = self._ws
        if ws is not None:
            ws.close(1000)

    def _run(self, deliver: Callable[[Event], None]) -> None:
        delay = 1.0
        while not self._stopping.is_set():
            try:
                url = str(self.api.call("apps.connections.open", app=True).get("url") or "")
                ws = WebSocket.connect(url, timeout=CHANNEL_TIMEOUT)
            except SlackError as exc:
                self._trouble("error" if exc.error in BAD_TOKEN or exc.error == "missing_scope" else "connecting", str(exc))
                wait, delay = backoff(delay, CHANNEL_RETRY_SECONDS)
                self._wait_or_stop(CHANNEL_RETRY_SECONDS if exc.error in BAD_TOKEN else wait)
                continue
            except WebSocketError as exc:
                self._trouble("connecting", str(exc))
                wait, delay = backoff(delay, CHANNEL_RETRY_SECONDS)
                self._wait_or_stop(wait)
                continue
            self._ws = ws
            if self._stopping.is_set():
                ws.close(1000)
                return
            healthy = self._listen(ws, deliver)
            self._ws = None
            if healthy:
                delay = 1.0
            elif not self._stopping.is_set():
                wait, delay = backoff(delay, CHANNEL_RETRY_SECONDS)
                self._wait_or_stop(wait)

    def _wait_or_stop(self, seconds: float) -> None:
        self._stopping.wait(seconds)

    def _listen(self, ws: WebSocket, deliver: Callable[[Event], None]) -> bool:
        """收这一条连接上的信封，直到它断开或者 Slack 说要换一条。返回这条连接是不是正常地结束的（换一条就好）。

        这个线程只做两件快的事：回执、看一眼信封里是什么。真正的处理（问名字、打开私聊，都要请求 Slack）交给
        _hand_off 的线程按顺序做：Slack 只等 3 秒回执，前一个信封处理得慢（比如 users.info 正被限流）的时候，
        后面的信封照样马上回执，不然 Slack 会把事件再推一遍，斜杠命令和按钮在 Slack 里会显示出错。

        很久没有动静时发一个 ping：之后收到了任何东西（对方回的 pong 也算）就说明连接还活着，下次安静下来再 ping；
        ping 了 CHANNEL_IDLE_SECONDS 还是什么都没收到，连接多半已经断了（只是 TCP 还不知道），断开重连。
        """
        ping_at: float | None = None  # 发出 ping 的时刻；之后收到过东西就清掉
        try:
            while not self._stopping.is_set():
                raw = ws.recv(timeout=min(1.0, CHANNEL_IDLE_SECONDS / 2))  # 最多等到下一次该看看要不要 ping 的时候
                if ping_at is not None and ws.last_seen > ping_at:
                    ping_at = None
                if raw is None:
                    now = time.monotonic()
                    if ping_at is not None and now - ping_at > CHANNEL_IDLE_SECONDS:
                        ws.abort()
                        self._trouble("connecting", t("连接没有回应，正在重连"))
                        return False
                    if ping_at is None and now - ws.last_seen > CHANNEL_IDLE_SECONDS:
                        ws.ping(b"zhifox")
                        ping_at = now
                    continue
                envelope = _json(raw)
                if envelope is None:
                    continue
                kind = envelope.get("type")
                if envelope.get("envelope_id"):
                    ws.send_text(to_json({"envelope_id": envelope["envelope_id"]}))  # 先回执，再慢慢处理
                if kind == "hello":
                    self._online()
                elif kind == "disconnect":
                    if envelope.get("reason") == "link_disabled":
                        self._trouble("error", t("这个 Slack 应用的 Socket Mode 被关掉了：在应用后台的 Socket Mode 页面把它打开。"))
                        ws.close(1000)
                        self._stopping.wait(CHANNEL_RETRY_SECONDS)
                        return False
                    ws.close(1000)
                    return True
                else:
                    make = self._event(kind, envelope.get("payload"))
                    if make is not None and not self._stopping.is_set():
                        self._hand_off(lambda make=make: self._deliver(make, deliver))
            ws.close(1000)
            return True
        except ConnectionClosed as exc:
            if not self._stopping.is_set():
                self._trouble("connecting", str(exc))
            return exc.code in (1000, 1001)
        except WebSocketError as exc:
            ws.abort()
            self._trouble("connecting", str(exc))
            return False
        except Exception:
            logger.exception("slack: 处理一个信封时出错")
            ws.abort()
            return False

    def _event(self, kind: Any, payload: Any) -> Callable[[], Event | None] | None:
        """看一眼信封里是什么（在收信封的线程里，不联网）。要处理的话返回一个做出事件的函数，交给 _hand_off 的线程去调用。"""
        if not isinstance(payload, dict):
            return None
        if kind == "events_api" and payload.get("type") == "event_callback":
            if not self._first_time(str(payload.get("event_id") or "")):
                return None  # Slack 没等到回执、又推了一遍的（envelope 可能换了，event_id 不变）
            event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
            return lambda: self._message(event)
        if kind == "interactive" and payload.get("type") == "block_actions":
            return lambda: self._press(payload)
        if kind == "slash_commands":
            return lambda: self._command(payload)
        return None

    def _deliver(self, make: Callable[[], Event | None], deliver: Callable[[Event], None]) -> None:
        event = make()
        if event is not None and not self._stopping.is_set():
            deliver(event)

    def _message(self, event: dict) -> Inbound | None:
        if event.get("type") != "message" or event.get("bot_id") or event.get("hidden") or not event.get("user"):
            return None  # 机器人自己（和别的应用）发的、改过的、删掉的
        if event.get("subtype") not in (None, "file_share"):
            return None
        user = str(event["user"])
        inbound = Inbound(
            platform=self.platform,
            chat=str(event.get("channel") or ""),
            user=user,
            name=self._name(user),
            text=from_slack(str(event.get("text") or "")),
            private=event.get("channel_type") == "im",
        )
        for item in event.get("files") or []:
            if not isinstance(item, dict) or item.get("mode") == "external":
                continue  # 外部的文件（Google Drive 这类）：不把 token 发到别人那里
            url = item.get("url_private_download") or item.get("url_private")
            if not isinstance(url, str):
                continue
            mime = str(item.get("mimetype") or "application/octet-stream")
            inbound.files.append(
                InboundFile(
                    name=str(item.get("name") or item.get("title") or "file"),
                    media_type=mime,
                    size=item.get("size") if isinstance(item.get("size"), int) else None,
                    kind="photo" if mime.startswith("image/") else "document",
                    fetch=lambda url=url, mime=mime: self._download(url, mime),
                )
            )
        if not inbound.text and not inbound.files:
            return None
        return inbound

    def _press(self, payload: dict) -> Press | None:
        actions = payload.get("actions") if isinstance(payload.get("actions"), list) else []
        action = actions[0] if actions and isinstance(actions[0], dict) else {}
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        channel = payload.get("channel") if isinstance(payload.get("channel"), dict) else {}
        chat, user_id = str(channel.get("id") or ""), str(user.get("id") or "")
        return Press(
            platform=self.platform,
            chat=chat if chat.startswith("D") else "",  # 只认私聊（D 开头）里的按钮
            user=user_id,
            name=self._name(user_id) if user_id else "",
            data=str(action.get("value") or ""),
            answer=lambda text, problem=False: self._whisper(chat, user_id, text) if problem and text else None,
        )

    def _command(self, payload: dict) -> Inbound | None:
        """/zhifox new → 一条写着 /new 的消息。只认在和机器人的私聊里用的（在别的地方用，回答会被别人看到）。"""
        user = str(payload.get("user_id") or "")
        channel = str(payload.get("channel_id") or "")
        words = str(payload.get("text") or "").strip().split(maxsplit=1)
        first = words[0].lower().lstrip("/") if words else "help"
        text = "/" + first + (" " + words[1] if len(words) > 1 else "") if first in COMMANDS else " ".join(words)
        private = bool(user) and channel.startswith("D") and channel == self._dm(user)
        return Inbound(platform=self.platform, chat=channel, user=user, name=self._name(user), text=text, private=private)

    def _dm(self, user: str) -> str:
        """这个人和机器人的私聊的编号（问一次 conversations.open，记下来）。"""
        if user not in self._dms:
            try:
                channel = self.api.call("conversations.open", {"users": user}).get("channel") or {}
                self._dms[user] = str(channel.get("id") or "")
            except SlackError as exc:
                logger.info("slack: 没能打开私聊（%s）", exc)
                return ""
        return self._dms[user]

    def _name(self, user: str) -> str:
        """一个 Slack 用户给人看的名字（问一次 users.info，记下来）。问不到就用编号。"""
        if user not in self._names:
            try:
                info = self.api.call("users.info", query={"user": user}).get("user") or {}
                profile = info.get("profile") if isinstance(info.get("profile"), dict) else {}
                self._names[user] = str(profile.get("display_name") or info.get("real_name") or info.get("name") or user)
            except SlackError:
                return user
        return self._names[user]

    def _download(self, url: str, mime: str = "") -> bytes:
        """下载一个私有文件。mime 是 Slack 说的类型：用来认出"其实没下载到"（见下面）。"""
        host = urllib.parse.urlsplit(url).hostname or ""
        # 只把 token 带到 Slack 自己的域名（http:// 只有测试里的本机才会通过 trusted）
        if not http.trusted(url) or not (host == "slack.com" or host.endswith(".slack.com") or url.startswith("http://")):
            raise ChannelError(t("没能下载这个文件。"))
        try:
            content = http.download(url, headers={"Authorization": f"Bearer {self.api.bot_token}"}, limit=CHANNEL_DOWNLOAD_BYTES, hide=self.api.secrets)
        except http.NetError as exc:
            raise ChannelError(str(exc)) from None
        # 应用没有 files:read 权限时，Slack 不报错，而是回一个 HTTP 200 的登录页面。不认出来的话，
        # 这个网页会被当成一个文本文件交给模型
        if not mime.startswith("text/html") and content.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")):
            raise ChannelError(_explain("missing_scope", "files", "files:read"))
        return content

    def _whisper(self, chat: str, user: str, text: str) -> None:
        """一句只有 user 看得到的话（按钮已经没用了的时候）。"""
        try:
            self.api.call("chat.postEphemeral", {"channel": chat, "user": user, "text": markup.slack_escape(text)})
        except SlackError:
            pass

    # ---- 发 ------------------------------------------------------------------

    def _post(self, chat: str, text: str, blocks: list | None = None) -> dict:
        body: dict = {"channel": chat, "text": text, "unfurl_links": False, "unfurl_media": False}
        if blocks is not None:
            body["blocks"] = blocks
        return self.api.call("chat.postMessage", body)

    def send(self, chat: str, text: str, markdown: bool = False) -> None:
        for piece in _pieces(text, markdown):
            self._post(chat, piece)

    def ask(self, chat: str, prompt: Prompt) -> str:
        """确认请求：粗体的标题、代码块里的完整命令（一段放不下就分几段）、工作目录，下面两个按钮。

        text 是通知里、不显示块的地方给人看的那一句，它也是 mrkdwn，同样要转义（文件名里可能有 & < >）。"""
        detail = markup.fence_safe(prompt.detail)
        title = markup.slack_escape(prompt.title)
        sections = [{"type": "section", "text": {"type": "mrkdwn", "text": f"```{markup.slack_escape(part)}```"}} for part in _sections(detail)] if detail.strip() else []
        head = [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}}]
        groups = [sections[index : index + SECTIONS_PER_MESSAGE] for index in range(0, len(sections), SECTIONS_PER_MESSAGE)] or [[]]
        for index, group in enumerate(groups[:-1]):
            self._post(chat, title, (head if index == 0 else []) + group)
        blocks = (head if len(groups) == 1 else []) + groups[-1]
        if prompt.where:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": markup.slack_escape(prompt.where)}]})
        actions = {"type": "actions", "block_id": f"confirm-{prompt.id}", "elements": [
            {"type": "button", "action_id": "allow", "style": "primary", "value": f"ok:{prompt.id}", "text": {"type": "plain_text", "text": prompt.allow[:75]}},
            {"type": "button", "action_id": "deny", "style": "danger", "value": f"no:{prompt.id}", "text": {"type": "plain_text", "text": prompt.deny[:75]}},
        ]}
        sent = self._post(chat, title, [*blocks, actions])
        handle = str(sent.get("ts") or "")
        if handle:
            self._asked[handle] = blocks
        return handle

    def settle(self, chat: str, handle: str, outcome: str) -> None:
        blocks = self._asked.pop(handle, None)
        if not handle or blocks is None:
            return
        shown = markup.slack_escape(outcome)
        done = [*blocks, {"type": "section", "text": {"type": "mrkdwn", "text": f"*{shown}*"}}]
        try:
            self.api.call("chat.update", {"channel": chat, "ts": handle, "text": shown, "blocks": done})
        except SlackError:
            self._post(chat, shown)  # 改不了：另发一条说明结果


def _pieces(text: str, markdown: bool) -> list[str]:
    """把要发的话拆成几条、排好。先按 SAFE_LIMIT 拆；排好以后还是超过 MESSAGE_LIMIT 的（满是 & < > 的代码：& 排成 &amp;
    一个变五个），那一段再拆成两半重排，直到每一条都放得下。用 markup.split 拆，代码块拆开的地方照样关上、重新打开。"""
    shape = markup.to_mrkdwn if markdown else markup.slack_escape
    pending, out = markup.split(text, SAFE_LIMIT), []
    while pending:
        piece = pending.pop(0)
        shaped, size = shape(piece), markup.length(piece)
        if markup.length(shaped) <= MESSAGE_LIMIT or size < 2:
            out.append(shaped)
        else:
            pending[:0] = markup.split(piece, size // 2)
    return out


def _sections(detail: str) -> list[str]:
    """把要显示的命令拆成每段排成 mrkdwn 以后不超过 SECTION_LIMIT 的几段（& < > 转义以后会变长）。"""
    pending, out = markup.split(detail, SECTION_LIMIT - 10), []
    while pending:
        part = pending.pop(0)
        if len(markup.slack_escape(part)) + 6 <= SECTION_LIMIT or len(part) < 2:
            out.append(part)
        else:
            middle = len(part) // 2
            pending[:0] = [part[:middle], part[middle:]]
    return out


def _json(raw: str | bytes) -> dict | None:
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None
