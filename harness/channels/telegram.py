"""Telegram：手写的 Bot API 客户端（只用标准库），和一个 Telegram 通道。

Bot API 很朴素：每个操作都是一次 HTTPS 请求，https://api.telegram.org/bot<token>/<方法名>，
请求和回答都是 JSON。回答长这样：{"ok": true, "result": …}，出错时是 {"ok": false, "error_code": 401, "description": "…"}。

用到的几个方法：

  getMe             检查 token 对不对，顺便拿到机器人的 @username
  getUpdates        收消息。用的是"长轮询"：请求发过去，Telegram 那边有新消息就马上回答，没有就等着，
                    最多等 timeout 秒再回答一个空列表，然后我们再发下一个。这样不需要公网地址：
                    是我们去问 Telegram，而不是 Telegram 来找我们（那种叫 webhook，要一个公网能访问的网址）。
                    offset 是"我已经处理到第几条了"：带着它去问，Telegram 就不再发它之前的，并把它们删掉。
  sendMessage       发消息。文字排成 HTML（见 markup.py），可以带一排按钮（inline keyboard）
  editMessageText   改一条已经发出去的消息：确认请求答复以后，把按钮去掉，写上结果
  answerCallbackQuery  有人点了按钮：回他一声（不回的话，他的按钮会一直转圈）
  sendChatAction    显示"正在输入"，持续 5 秒
  getFile           聊天里发来的图片和文件：先问到它在 Telegram 服务器上的路径，再下载
  setMyCommands     把 /new、/stop 这些命令登记到聊天输入框旁边的菜单里
  sendPhoto         助手用 send_file 发一张图片：当照片发，聊天里直接显示（最大 10 MB；宽加高不超过 10000、长宽比不超过 20）
  sendAnimation     发一张 GIF：当动图发，聊天里会动（最大 50 MB）
  sendDocument      发别的文件，以及太大、尺寸不合格当不了照片的图片（最大 50 MB）
                    这三个都是用 multipart/form-data 上传（见 ../net/multipart.py），说明（caption）最多 1024 个字符
                    （https://core.telegram.org/bots/api#sending-files、#sendphoto、#sendanimation、#senddocument）

token 是这个机器人的钥匙，它就写在每个请求的网址里。所以出错时给人看的话里一律把它换掉（_redact），
不打印网址，日志里也不出现（见 ../log.py）。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from .. import log
from ..config import (
    CHANNEL_DOWNLOAD_BYTES,
    CHANNEL_POLL_SECONDS,
    CHANNEL_RETRY_SECONDS,
    CHANNEL_THUMB_BYTES,
    CHANNEL_THUMB_EDGE,
    CHANNEL_TIMEOUT,
    VOICE_MAX_BYTES,
)
from ..i18n import t
from ..net import http, multipart
from . import markup
from .base import (
    Capabilities,
    Channel,
    ChannelError,
    Event,
    Inbound,
    InboundAudio,
    InboundFile,
    OutboundFile,
    Press,
    Prompt,
    Status,
    Uploads,
)

LIMIT = 4096  # 一条消息最多这么多个字符（UTF-16 单位，按排版之后的文字算）
SAFE_LIMIT = 4000  # 自己拆的时候留一点余量：排版用的符号去掉以后只会更短，但 Telegram 数法的细节没有写清楚
# token 的样子：机器人的编号、冒号、一串字母数字。只检查样子，对不对要问 getMe
_TOKEN = re.compile(r"\d{5,16}:[A-Za-z0-9_-]{30,64}")
MAX_RETRIES = 3  # 被限流（429）时最多等几次再重试

logger = logging.getLogger("harness.channels.telegram")


class TelegramError(ChannelError):
    """Telegram 回答了一个错误，或者连不上。code 是 HTTP 状态码（连不上是 0）。"""

    def __init__(self, message: str, code: int = 0, retry_after: float = 0.0, description: str = ""):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after
        self.description = description  # Telegram 自己的英文说明（判断是不是"HTML 解析不了"用）


def valid_token(token: str) -> bool:
    return bool(_TOKEN.fullmatch(token))


class BotAPI:
    """Telegram Bot API 的一个极简客户端。每个方法一次 HTTPS 请求，可以从几个线程同时用（每次请求各开一个连接）。"""

    def __init__(self, token: str, base: str = "https://api.telegram.org", timeout: float = CHANNEL_TIMEOUT):
        self.token = token
        self.base = base.rstrip("/")
        self.timeout = timeout
        log.hide(token)

    def _redact(self, text: str) -> str:
        return text.replace(self.token, "<token>") if self.token else text

    def call(self, method: str, params: dict | None = None, timeout: float | None = None) -> Any:
        """调用一个方法，返回 result。出错抛出 TelegramError（说明里没有 token）。"""
        request = urllib.request.Request(
            f"{self.base}/bot{self.token}/{method}",
            data=json.dumps(params or {}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                payload = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read() or b"{}")
            except (ValueError, OSError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            payload.setdefault("error_code", exc.code)
        except (OSError, ValueError) as exc:  # 连不上、超时、回答不是 JSON（URLError、TimeoutError 都是 OSError）
            reason = getattr(exc, "reason", None) or exc
            raise TelegramError(t("连不上 Telegram（{error}）", error=self._redact(str(reason)) or type(exc).__name__)) from None
        return self._result(payload)

    def upload(self, method: str, params: dict, file: multipart.File) -> Any:
        """用 multipart/form-data 调用一个方法（sendPhoto、sendDocument……：要把文件本身传上去）。返回 result，出错和 call 一样。"""
        fields = {name: value if isinstance(value, str) else json.dumps(value) for name, value in params.items()}
        try:
            reply = multipart.post(f"{self.base}/bot{self.token}/{method}", fields=fields, files=[file], hide=(self.token,))
        except http.NetError as exc:
            raise TelegramError(t("连不上 Telegram（{error}）", error=self._redact(str(exc)))) from None
        payload = reply.data if isinstance(reply.data, dict) else {}
        if not reply.ok:
            payload = {**payload, "ok": False}
            payload.setdefault("error_code", reply.status)
            payload.setdefault("description", reply.text)
        return self._result(payload)

    def _result(self, payload: Any) -> Any:
        """Telegram 的回答 → result；不是 ok 的抛出 TelegramError（说明里没有 token）。"""
        if not isinstance(payload, dict):
            raise TelegramError(t("Telegram 的回答看不懂。"))
        if payload.get("ok"):
            return payload.get("result")
        code = int(payload.get("error_code") or 0)
        description = self._redact(str(payload.get("description") or ""))
        parameters = payload.get("parameters") if isinstance(payload.get("parameters"), dict) else {}
        retry_after = float(parameters.get("retry_after") or 0)
        raise TelegramError(_explain(code, description), code, retry_after, description)

    def download(self, file_id: str, limit: int = CHANNEL_DOWNLOAD_BYTES) -> bytes:
        """下载聊天里发来的一个文件。超过 limit 个字节时抛出 TelegramError。"""
        info = self.call("getFile", {"file_id": file_id})
        path = info.get("file_path") if isinstance(info, dict) else None
        if not isinstance(path, str) or not path:
            raise TelegramError(t("没能下载这个文件。"))
        if int(info.get("file_size") or 0) > limit:
            raise TelegramError(t("这个文件太大了，最多 {size} MB。", size=limit // 1_000_000))
        try:
            with urllib.request.urlopen(f"{self.base}/file/bot{self.token}/{path}", timeout=self.timeout * 3) as response:
                content = response.read(limit + 1)
        except (OSError, ValueError) as exc:
            reason = getattr(exc, "reason", None) or exc
            raise TelegramError(t("下载文件失败（{error}）", error=self._redact(str(reason)) or type(exc).__name__)) from None
        if len(content) > limit:
            raise TelegramError(t("这个文件太大了，最多 {size} MB。", size=limit // 1_000_000))
        return content


def _explain(code: int, description: str) -> str:
    """把 Telegram 的错误换成给人看的话。"""
    lowered = description.lower()
    if code == 401 or code == 404:
        return t("Telegram 不认这个 token：可能抄错了，或者已经在 @BotFather 那里作废了。")
    if code == 409 and "webhook" in lowered:
        return t("这个机器人设置了 webhook（消息会推到别的网址），所以收不到消息。先在别处取消 webhook，或者换一个机器人。")
    if code == 409:
        return t("另一个程序正在用这个机器人收消息（同一个 token 同时只能有一处在收）。先关掉那一个，或者在 @BotFather 那里换一个 token。")
    if code == 413 or "too large" in lowered:
        return t("这个文件太大了，Telegram 不让机器人发这么大的文件。")
    if "file is too big" in lowered:
        return t("这个文件太大了，Telegram 的机器人最多只能下载 20 MB 的文件。")
    if code == 429:
        return t("发得太快，被 Telegram 限流了，过一会儿会自动重试。")
    if code == 403:
        return t("Telegram 拒绝了：你可能把这个机器人停用（Block）了。")
    return t("Telegram 回答了一个错误：{error}", error=description or code)


def check_token(token: str, base: str = "https://api.telegram.org") -> dict:
    """检查一个 token：样子对不对、Telegram 认不认。认的话返回机器人的信息 {"id", "username", "name"}。"""
    token = token.strip()
    if not valid_token(token):
        raise ChannelError(t("这不像是一个 Telegram 机器人的 token。它应该长这样：123456789:AAH…（一串数字、冒号、再一串字母数字）。"))
    me = BotAPI(token, base).call("getMe")
    if not isinstance(me, dict) or not me.get("username"):
        raise ChannelError(t("Telegram 的回答看不懂。"))
    return {"id": str(me.get("id", "")), "username": str(me["username"]), "name": str(me.get("first_name") or me["username"])}


def _person(sender: dict) -> str:
    """一个 Telegram 用户给人看的名字：名、姓，有用户名的话加上 (@用户名)。"""
    name = " ".join(part for part in (sender.get("first_name"), sender.get("last_name")) if isinstance(part, str) and part)
    username = sender.get("username")
    if isinstance(username, str) and username:
        return f"{name} (@{username})" if name else f"@{username}"
    return name or str(sender.get("id", ""))


# 能往聊天里发什么（见 base.Uploads）：照片 10 MB，别的文件（动图也是）50 MB，说明 1024 个字符
UPLOADS = Uploads(file_bytes=50_000_000, photo_bytes=10_000_000, caption_chars=1024)


class TelegramChannel(Channel):
    platform = "telegram"
    capabilities = Capabilities(buttons=True, max_chars=SAFE_LIMIT, typing=True, files=True, uploads=UPLOADS)

    def __init__(
        self,
        token: str,
        base: str = "https://api.telegram.org",
        offset: int = 0,
        on_offset: Callable[[int], None] | None = None,
        poll_seconds: float = CHANNEL_POLL_SECONDS,
    ):
        self.api = BotAPI(token, base)
        self.offset = offset  # 下一次 getUpdates 从第几条开始（之前的都处理过了）
        self._on_offset = on_offset  # 读到了新的位置就告诉一声，记进 channels.json，重启以后接着读
        self.poll_seconds = poll_seconds
        self._status = Status("off")
        self._status_lock = threading.Lock()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._asked: dict[str, str] = {}  # 发出去的确认请求：消息编号 → 那条消息的 HTML（答复以后在它后面写上结果）
        self.on_status: Callable[[], None] | None = None  # 状态变了就通知一声（hub.py 用它刷新页面）

    # ---- 状态 ----------------------------------------------------------------

    @property
    def status(self) -> Status:
        with self._status_lock:
            return self._status

    def _set_status(self, state: str, message: str = "") -> None:
        with self._status_lock:
            if self._status.state == state and self._status.message == message:
                return
            self._status = Status(state, message)
        if state == "error":
            logger.warning("telegram: %s", message)
        else:
            logger.info("telegram: %s%s", state, f" ({message})" if message else "")
        if self.on_status is not None:
            self.on_status()

    # ---- 收消息 --------------------------------------------------------------

    def start(self, deliver: Callable[[Event], None]) -> None:
        self._stopping.clear()
        self._set_status("connecting")
        self._thread = threading.Thread(target=self._poll, args=(deliver,), daemon=True, name="telegram")
        self._thread.start()

    def stop(self) -> None:
        """不再收消息。正在等着的那次长轮询最多再过 poll_seconds 秒自己结束（线程是后台的，不挡着程序退出）。"""
        self._stopping.set()
        self._set_status("off")

    def join(self, timeout: float) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _poll(self, deliver: Callable[[Event], None]) -> None:
        delay = 1.0
        while not self._stopping.is_set():
            try:
                updates = self.api.call(
                    "getUpdates",
                    {"offset": self.offset, "timeout": int(self.poll_seconds), "allowed_updates": ["message", "callback_query"]},
                    timeout=self.poll_seconds + CHANNEL_TIMEOUT,
                )
            except TelegramError as exc:
                if self._stopping.is_set():
                    break
                if exc.code in (401, 404):
                    self._set_status("error", str(exc))
                    wait = CHANNEL_RETRY_SECONDS  # token 不对，重试也没用；换了 token 会换一个新的通道
                elif exc.code == 429:
                    wait = max(1.0, exc.retry_after)
                else:
                    self._set_status("error" if exc.code else "connecting", str(exc))
                    wait, delay = delay, min(delay * 2, CHANNEL_RETRY_SECONDS)
                self._stopping.wait(wait)
                continue
            delay = 1.0
            if self._stopping.is_set():
                break  # 停下以后才回来的这一批不处理（也不往后挪 offset）：下次开着的时候 Telegram 会再发一遍
            self._set_status("online")
            if not isinstance(updates, list) or not updates:
                continue
            for update in updates:
                if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
                    continue
                self.offset = max(self.offset, update["update_id"] + 1)
                try:
                    event = self._event(update)
                    if event is not None:
                        deliver(event)
                except Exception:
                    logger.exception("telegram: 处理一条消息时出错")
            if self._on_offset is not None:
                try:
                    self._on_offset(self.offset)
                except Exception:
                    logger.exception("telegram: 没能记下读到的位置")

    def _event(self, update: dict) -> Event | None:
        """把 Telegram 的一条 update 变成 Inbound 或 Press。不认识的返回 None。"""
        query = update.get("callback_query")
        if isinstance(query, dict):
            sender = query.get("from") if isinstance(query.get("from"), dict) else {}
            message = query.get("message") if isinstance(query.get("message"), dict) else {}
            chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
            query_id = str(query.get("id", ""))
            return Press(
                platform=self.platform,
                chat=str(chat.get("id", "")),
                user=str(sender.get("id", "")),
                name=_person(sender),
                data=str(query.get("data") or ""),
                answer=lambda text, problem=False: self._answer_press(query_id, text),
            )
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        sender = message.get("from") if isinstance(message.get("from"), dict) else None
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        if sender is None or sender.get("is_bot"):
            return None  # 频道里转发来的、别的机器人发的：不理
        inbound = Inbound(
            platform=self.platform,
            chat=str(chat.get("id", "")),
            user=str(sender.get("id", "")),
            name=_person(sender),
            text=str(message.get("text") or message.get("caption") or ""),
            private=chat.get("type") == "private",
        )
        photos = message.get("photo")
        if isinstance(photos, list) and photos:
            sizes = [size for size in photos if isinstance(size, dict) and size.get("file_id")]
            if sizes:
                biggest, preview = sizes[-1], _preview_size(sizes)  # Telegram 按从小到大的顺序给
                inbound.files.append(
                    InboundFile(
                        name=f"photo-{message.get('message_id', 0)}.jpg",
                        media_type="image/jpeg",
                        size=biggest.get("file_size"),
                        kind="photo",
                        fetch=lambda file=biggest["file_id"]: self.api.download(file),
                        thumb=(lambda file=preview["file_id"]: self.api.download(file, CHANNEL_THUMB_BYTES)) if len(sizes) > 1 else None,
                    )
                )
        document = message.get("document")
        if isinstance(document, dict) and document.get("file_id"):
            inbound.files.append(
                InboundFile(
                    name=str(document.get("file_name") or "file"),
                    media_type=str(document.get("mime_type") or "application/octet-stream"),
                    size=document.get("file_size"),
                    kind="document",
                    fetch=lambda file=document["file_id"]: self.api.download(file),
                )
            )
        inbound.audio = _audio(message, self.api)
        if not inbound.text and not inbound.files and inbound.audio is None:
            for kind in ("voice", "audio", "video", "video_note", "sticker", "animation", "location", "contact", "poll"):
                if kind in message:
                    inbound.unsupported = kind
                    break
            else:
                return None  # 进群、改名这类系统消息
        return inbound

    def _answer_press(self, query_id: str, text: str) -> None:
        try:
            self.api.call("answerCallbackQuery", {"callback_query_id": query_id, "text": text[:190]})
        except TelegramError:
            pass  # 只是按钮上的一个小提示，没回上也不要紧

    # ---- 发消息 --------------------------------------------------------------

    def _call(self, method: str, params: dict) -> Any:
        """调用一个方法；被限流时按 Telegram 说的时间等一等再试（最多几次）。"""
        for attempt in range(MAX_RETRIES + 1):
            try:
                return self.api.call(method, params)
            except TelegramError as exc:
                if exc.code != 429 or attempt == MAX_RETRIES:
                    raise
                time.sleep(min(max(exc.retry_after, 1.0), 30.0))
        return None

    def _send_html(self, chat: str, html: str, plain: str, buttons: list | None = None) -> dict:
        params: dict = {"chat_id": chat, "text": html, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        try:
            return self._call("sendMessage", params)
        except TelegramError as exc:
            if exc.code != 400 or "parse" not in exc.description.lower():
                raise
            # 排版出了问题（不该发生，但万一）：去掉格式，按纯文字再发一次
            params.pop("parse_mode")
            params["text"] = plain
            return self._call("sendMessage", params)

    def send(self, chat: str, text: str, markdown: bool = False) -> None:
        for piece in markup.split(text, SAFE_LIMIT):
            if markdown:
                self._send_html(chat, markup.to_html(piece), piece)
            else:
                self._call("sendMessage", {"chat_id": chat, "text": piece, "link_preview_options": {"is_disabled": True}})

    def ask(self, chat: str, prompt: Prompt) -> str:
        """确认请求：标题、要做的事（放在代码块里，原样显示，一个字都不省）、工作目录，下面两个按钮。

        要做的事太长、一条放不下的话，前面几段先各发一条，带按钮的那条放在最后：你看完了才轮到按按钮。
        """
        head = f"<b>{markup.escape(prompt.title)}</b>\n"
        tail = f"\n{markup.escape(prompt.where)}" if prompt.where else ""
        room = SAFE_LIMIT - markup.length(prompt.title) - markup.length(prompt.where) - 8
        parts = markup.split(prompt.detail, max(500, room)) or [""]
        for index, part in enumerate(parts[:-1]):
            first = head if index == 0 else ""
            self._send_html(chat, f"{first}<pre>{markup.escape(part)}</pre>", (prompt.title + "\n" if index == 0 else "") + part)
        last = parts[-1]
        html = (head if len(parts) == 1 else "") + (f"<pre>{markup.escape(last)}</pre>" if last else "") + tail
        plain = (prompt.title + "\n" if len(parts) == 1 else "") + last + ("\n" + prompt.where if prompt.where else "")
        buttons = [[{"text": prompt.allow, "callback_data": f"ok:{prompt.id}"}, {"text": prompt.deny, "callback_data": f"no:{prompt.id}"}]]
        sent = self._send_html(chat, html, plain, buttons)
        handle = str(sent.get("message_id", "")) if isinstance(sent, dict) else ""
        if handle:
            self._asked[handle] = html
        return handle

    def settle(self, chat: str, handle: str, outcome: str) -> None:
        html = self._asked.pop(handle, None)
        if not handle or html is None:
            return
        text = f"{html}\n\n<b>{markup.escape(outcome)}</b>"
        try:
            # 不带 reply_markup：这样改完以后按钮就没了，不会再被点
            self._call("editMessageText", {"chat_id": chat, "message_id": int(handle), "text": text, "parse_mode": "HTML"})
        except (TelegramError, ValueError):
            self._call("sendMessage", {"chat_id": chat, "text": outcome})  # 改不了（比如消息太旧）：另发一条说明结果

    def send_file(self, chat: str, outgoing: OutboundFile) -> None:
        """发一个文件：图片当照片（GIF 当动图），别的当文件。说明放进 caption（太长的另发一条）。

        当照片发被拒绝了（Telegram 说尺寸不合格 PHOTO_INVALID_DIMENSIONS、处理不了这张图 IMAGE_PROCESS_FAILED，都是 400）：
        改成当文件发一次，图片照样到了，只是不直接显示。"""
        caption = outgoing.caption.strip()
        inline = caption if caption and markup.length(caption) <= UPLOADS.caption_chars else ""  # 原文不超，排版去掉符号以后更不会超
        if outgoing.kind == "photo" and outgoing.media_type == "image/gif":
            method, field = "sendAnimation", "animation"
        elif outgoing.kind == "photo":
            method, field = "sendPhoto", "photo"
        else:
            method, field = "sendDocument", "document"
        try:
            self._upload(chat, method, field, outgoing, inline)
        except TelegramError as exc:
            if method == "sendDocument" or exc.code != 400 or not any(word in exc.description.lower() for word in ("photo", "image", "dimension", "animation")):
                raise
            logger.info("telegram: 当照片发不出去，改成当文件发（%s）", exc)
            self._upload(chat, "sendDocument", "document", outgoing, inline)
        if caption and not inline:
            self.send(chat, caption, markdown=True)

    def _upload(self, chat: str, method: str, field: str, outgoing: OutboundFile, caption: str) -> None:
        params: dict = {"chat_id": chat}
        if caption:
            params.update(caption=markup.to_html(caption), parse_mode="HTML")
        file = multipart.File(field, outgoing.name, outgoing.media_type, outgoing.data)
        for attempt in range(MAX_RETRIES + 1):
            try:
                self.api.upload(method, params, file)
                return
            except TelegramError as exc:
                if exc.code == 400 and caption and "parse" in exc.description.lower() and "parse_mode" in params:
                    params = {"chat_id": chat, "caption": caption}  # 说明的排版出了问题：去掉格式再发一次
                    continue
                if exc.code != 429 or attempt == MAX_RETRIES:
                    raise
                time.sleep(min(max(exc.retry_after, 1.0), 30.0))

    def typing(self, chat: str) -> None:
        try:
            self.api.call("sendChatAction", {"chat_id": chat, "action": "typing"})
        except TelegramError:
            pass  # 只是个"正在输入"，没发出去不要紧

    def set_commands(self, commands: dict[str, str]) -> None:
        try:
            self.api.call("setMyCommands", {"commands": [{"command": name, "description": text[:250]} for name, text in commands.items()]})
        except TelegramError as exc:
            logger.info("telegram: 没能登记命令菜单（%s）", exc)


def _audio(message: dict, api: BotAPI) -> InboundAudio | None:
    """消息里的语音（voice，按住说话录的，OGG 格式）或者音频文件（audio）。都没有返回 None。

    两种都带着时长（duration，秒）和可选的类型、大小（https://core.telegram.org/bots/api#voice 、#audio）。
    下载的上限是语音转文字的上限（VOICE_MAX_BYTES，比 Telegram 自己的 20 MB 不会更大）。
    """
    for kind in ("voice", "audio"):
        item = message.get(kind)
        if not isinstance(item, dict) or not item.get("file_id"):
            continue
        media_type = str(item.get("mime_type") or ("audio/ogg" if kind == "voice" else "application/octet-stream"))
        number = message.get("message_id", 0)
        name = str(item.get("file_name") or "") if kind == "audio" else ""
        return InboundAudio(
            name=name or (f"voice-{number}.ogg" if kind == "voice" else f"audio-{number}"),
            media_type=media_type,
            size=max(0, _number(item.get("file_size"))) or None,
            duration=max(0, _number(item.get("duration"))),
            kind=kind,
            fetch=lambda file=item["file_id"]: api.download(file, VOICE_MAX_BYTES),
        )
    return None


def _preview_size(sizes: list[dict]) -> dict:
    """网页上的预览图用哪一种尺寸：长边不小于 CHANNEL_THUMB_EDGE 的里面最小的一张；都不够大（或者不知道多大）就用最大的
    （超过 CHANNEL_THUMB_BYTES 下载不了的话没有预览图，原图照样在）。

    不用最小的那张：它只有 90 像素左右，页面上放大到两三百个点宽，看起来是糊的。也不用最大的：
    预览图会存进会话的事件里，每次打开会话都要发给页面，太大了白白占地方。
    """
    for size in sizes:
        edge = max(_number(size.get("width")), _number(size.get("height")))
        if edge >= CHANNEL_THUMB_EDGE:
            return size
    return sizes[-1]


def _number(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
