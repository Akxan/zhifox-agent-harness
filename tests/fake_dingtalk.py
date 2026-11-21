"""本地的假钉钉：开放平台的 REST 接口、sessionWebhook、Stream 模式的网关，加一个假的 Stream 连接（FakeStreamListener）。
只在测试里用，不连真正的钉钉。

REST 照着钉钉文档里的样子回答（见 harness/channels/dingtalk.py 开头）：换 access_token、往一条消息带来的 sessionWebhook
回复（能让它过期）、主动发单聊消息（oToMessages/batchSend）、换文件的下载地址（messageFiles/download）再下载，
并记下机器人发出去的每一样东西（"via" 写着是从哪条路发的：webhook 或 batch）。
机器人发的图片和文件（send_file）：先传到老接口 /media/upload（multipart/form-data，access_token 在网址里；真的在
oapi.dingtalk.com，这里和别的接口在同一个假服务器上），拿到 media_id，再用 batchSend 发 sampleImageMsg 或 sampleFile。
上传的东西记在 uploads 里，也像真的一样检查：token 不对回 errcode 40014，最大 20 MB，图片只认 jpg、gif、png、bmp，
文件只认 doc、docx、xls、xlsx、ppt、pptx、zip、pdf、rar；sampleFile 的 fileType 只认 xlsx、pdf、zip、rar、doc、docx。

收消息有两种办法，测试自己选：
  - 平常用 FakeStreamListener（实现 dingtalk.Listener，不用装 SDK）：user_says 把一条机器人消息（和钉钉文档里的 JSON 一样的字典）
    直接交给通道，就像 SDK 收到了一样
  - 测真的 SDK（dingtalk-stream）时：POST /v1.0/gateway/connections/open 换票据，再连 /stream 这个 WebSocket，
    user_says 把消息包成 Stream 协议的 CALLBACK 帧发过去，并记下 SDK 回的回执（acks）。use_sdk = True 时 factory 建的通道用真的 SDK
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import fake_ws
from multipart_form import is_multipart, parse

CLIENT_ID = "dingfake0test0client"
CLIENT_SECRET = "FAKE-dingtalk-client-secret-0123456789-abcdefghijkl"  # 编造的，长得像真的
CORP = "dingfakecorp0000000000"
BOT_USER = "$:LWCP_v1:$fakeBotUser000000000000"
OWNER = "manager4321"  # 主人的员工编号（senderStaffId）
OWNER_NAME = "Akxan"  # 钉钉里主人的昵称（senderNick）
STRANGER = "stranger9999"
TOPIC = "/v1.0/im/bot/messages/get"  # 机器人收到消息的那个 Stream 主题
HINT = "回复“允许”或“拒绝”"
MEDIA_LIMIT = 20 * 1024 * 1024  # media/upload：图片和文件最大 20 MB
IMAGE_EXTENSIONS = {"jpg", "jpeg", "gif", "png", "bmp"}
FILE_EXTENSIONS = {"doc", "docx", "xls", "xlsx", "ppt", "pptx", "zip", "pdf", "rar"}
SAMPLE_FILE_TYPES = {"xlsx", "pdf", "zip", "rar", "doc", "docx"}


class FakeDingTalk:
    platform = "dingtalk"

    def __init__(self) -> None:
        self.client_secret = CLIENT_SECRET  # 换掉它，就像在开放平台上重置了 Client Secret
        self.credentials = {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
        self.bot = {"id": CLIENT_ID, "username": CLIENT_ID, "name": CLIENT_ID, "at": False, "app": CLIENT_ID}  # 和 dingtalk.check 返回的一样
        self.owner, self.owner_chat, self.owner_name = OWNER, OWNER, OWNER_NAME  # 单聊的编号就是对方的员工编号
        self.use_sdk = False  # True：factory 建的通道用真的 SDK，经过下面的 Stream 网关收消息
        self.webhooks_work = True  # False：往 sessionWebhook 发的都回"过期了"（钉钉那边提前作废了它）
        self.batch_error: tuple[int, dict] | None = None  # 下一次 batchSend 回的错误（HTTP 状态，内容）
        self.tokens: list[str] = []  # 发出去的每一个 accessToken
        self.sessions: dict[str, dict] = {}  # sessionWebhook 的 session → {"staff", "valid"}
        self.tickets: list[str] = []  # Stream 连接用的票据
        self.requests: list[str] = []  # 每个请求的方法和路径（不含问号后面的）
        self.sent: list[dict] = []  # 机器人发的消息：{"via", "chat", "msgtype", "title", "text", "plain"}
        self.rejected: list[str] = []
        self.batches: list[dict] = []  # batchSend 收到的请求体
        self.files: dict[str, bytes] = {}  # downloadCode → 内容
        # 机器人传上来的图片和文件：{"type": "image" 或 "file", "media_id", "filename", "content_type", "data", "token"}
        self.uploads: list[dict] = []
        self.media: dict[str, dict] = {}  # media_id → 上传的那一项
        self.upload_error: dict | None = None  # 下一次 media/upload 回的错误（内容）
        self.downloads: list[str] = []  # 换过下载地址的 downloadCode
        self.opens: list[dict] = []  # connections/open 收到的请求体（去掉了密钥）
        self.frames: list[dict] = []  # 发给 SDK 的 Stream 帧
        self.acks: list[dict] = []  # SDK 回的回执
        self.sockets: list[fake_ws.ServerSocket] = []
        self.listener: FakeStreamListener | None = None
        self._next = 0
        self._changed = threading.Condition()

    @property
    def secrets(self) -> list[str]:
        """这个平台的全部密钥：Client Secret、accessToken、sessionWebhook 里的 session、Stream 的票据。"""
        return [CLIENT_SECRET, *self.tokens, *self.sessions, *self.tickets]

    # ---- 启动 ------------------------------------------------------------------

    def start(self, port: int = 0) -> FakeDingTalk:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self, method: str) -> None:
                parts = urlsplit(self.path)
                query = {name: values[0] for name, values in parse_qs(parts.query).items()}
                if method == "GET" and parts.path == "/stream" and self.headers.get("Upgrade", "").lower() == "websocket":
                    fake._stream(self, query.get("ticket", ""))
                    return
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                if method == "POST" and parts.path == "/media/upload":
                    status, payload = fake._media(query, self.headers.get("content-type"), raw)
                else:
                    body = json.loads(raw) if raw else None
                    status, payload = fake._api(method, parts.path, query, body, self.headers)
                data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("content-type", "application/octet-stream" if isinstance(payload, bytes) else "application/json; charset=utf-8")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self

    def config(self) -> dict:
        return {"dingtalk_api": self.base_url, "dingtalk_oapi": self.base_url}

    def factory(self, config, store):
        """建一个连这个假钉钉的通道。平常 Stream 连接换成 FakeStreamListener（不用装 SDK）；use_sdk 时用真的 SDK。"""
        from harness.channels.dingtalk import DingTalkChannel

        credentials = store.get("dingtalk")["credentials"]
        listener = None if self.use_sdk else FakeStreamListener(self)
        return DingTalkChannel(credentials.get("client_id", ""), credentials.get("client_secret", ""), config.dingtalk_api, listener=listener, oapi=config.dingtalk_oapi)

    def stop(self) -> None:
        with self._changed:
            self._changed.notify_all()
        if self.listener is not None:
            self.listener.stop()
        for sock in list(self.sockets):
            sock.drop()
        self.server.shutdown()
        self.server.server_close()

    def _number(self) -> int:
        with self._changed:
            self._next += 1
            return self._next

    # ---- 测试用的开关 ------------------------------------------------------------

    def expire_sessions(self) -> None:
        """钉钉那边让发出去的 sessionWebhook 都失效（消息里写的过期时间还没到）。"""
        with self._changed:
            for info in self.sessions.values():
                info["valid"] = False

    # ---- REST ------------------------------------------------------------------

    def _api(self, method: str, path: str, query: dict, body, headers) -> tuple[int, object]:
        with self._changed:
            self.requests.append(f"{method} {path}")
        body = body if isinstance(body, dict) else {}
        if method == "POST" and path == "/v1.0/oauth2/accessToken":
            if body.get("appKey") != CLIENT_ID or body.get("appSecret") != self.client_secret:
                return 400, {"code": "invalidClientIdOrSecret", "message": "无效的clientId或者clientSecret", "requestid": "fake-request"}
            token = secrets.token_hex(16)
            with self._changed:
                self.tokens.append(token)
            return 200, {"accessToken": token, "expireIn": 7200}
        if method == "POST" and path == "/robot/sendBySession":
            return self._webhook(query.get("session", ""), body)
        if method == "POST" and path == "/v1.0/gateway/connections/open":
            return self._open(body)
        if method == "GET" and path.startswith("/files/"):
            content = self.files.get(path.removeprefix("/files/"))
            return (200, content) if content is not None else (404, {"code": "notFound", "message": "not found"})
        if headers.get("x-acs-dingtalk-access-token") not in self.tokens:
            return 401, {"code": "InvalidAuthentication", "message": "不合法的access_token", "requestid": "fake-request"}
        if method == "POST" and path == "/v1.0/robot/oToMessages/batchSend":
            return self._batch(body)
        if method == "POST" and path == "/v1.0/robot/messageFiles/download":
            code = body.get("downloadCode")
            with self._changed:
                self.downloads.append(str(code))
            if body.get("robotCode") != CLIENT_ID or code not in self.files:
                return 400, {"code": "unknownError", "message": "downloadCode 不对"}
            return 200, {"downloadUrl": f"{self.base_url}/files/{code}"}
        return 404, {"code": "notFound", "message": "not found"}

    def _media(self, query: dict, content_type: str | None, raw: bytes) -> tuple[int, object]:
        """老接口的上传（media/upload）：access_token 在网址里，type 是 image、voice、video、file，文件放在 media 里。
        和老接口一样，出错时 HTTP 状态还是 200，错误写在 errcode 里。"""
        token = query.get("access_token", "")
        with self._changed:
            self.requests.append("POST /media/upload")
            failure, self.upload_error = self.upload_error, None
        if failure is not None:
            return 200, failure
        if token not in self.tokens:
            return 200, {"errcode": 40014, "errmsg": "不合法的access_token"}
        if not is_multipart(content_type):
            return 200, {"errcode": 40035, "errmsg": "不合法的参数"}
        try:
            fields, files = parse(content_type or "", raw)
        except ValueError:
            return 200, {"errcode": 40035, "errmsg": "不合法的参数"}
        kind, item = fields.get("type"), files.get("media")
        if kind not in ("image", "file") or item is None:
            return 200, {"errcode": 40035, "errmsg": "不合法的参数"}
        extension = item["filename"].rsplit(".", 1)[-1].lower() if "." in item["filename"] else ""
        if extension not in (IMAGE_EXTENSIONS if kind == "image" else FILE_EXTENSIONS):
            return 200, {"errcode": 40005, "errmsg": "不合法的文件类型"}
        if not item["data"] or len(item["data"]) > MEDIA_LIMIT:
            return 200, {"errcode": 40006, "errmsg": "不合法的文件大小"}
        media_id = "@lA" + secrets.token_hex(10)
        upload = {"type": kind, "media_id": media_id, **item, "token": token}
        with self._changed:
            self.uploads.append(upload)
            self.media[media_id] = upload
            self._changed.notify_all()
        return 200, {"errcode": 0, "errmsg": "ok", "type": kind, "media_id": media_id, "created_at": int(time.time() * 1000)}

    def _record(self, message: dict) -> None:
        with self._changed:
            self.sent.append(message)
            self._changed.notify_all()

    def _reject(self, why: str) -> tuple[int, object]:
        with self._changed:
            self.rejected.append(why)
            self._changed.notify_all()
        return 400, {"code": "invalidParameter", "message": why}

    def _webhook(self, session: str, body: dict) -> tuple[int, object]:
        info = self.sessions.get(session)
        if info is None or not info["valid"] or not self.webhooks_work:
            return 200, {"errcode": 300001, "errmsg": "session is expired"}
        kind = body.get("msgtype")
        if kind == "markdown":
            markdown = body.get("markdown") or {}
            if not isinstance(markdown.get("title"), str) or not markdown["title"] or not isinstance(markdown.get("text"), str):
                return 200, {"errcode": 300005, "errmsg": "markdown 要有 title 和 text"}
            self._record({"via": "webhook", "chat": info["staff"], "msgtype": kind, "title": markdown["title"], "text": markdown["text"], "plain": markdown["text"]})
        elif kind == "text":
            content = (body.get("text") or {}).get("content")
            if not isinstance(content, str) or not content:
                return 200, {"errcode": 300005, "errmsg": "text 要有 content"}
            self._record({"via": "webhook", "chat": info["staff"], "msgtype": kind, "title": "", "text": content, "plain": content})
        else:
            return 200, {"errcode": 300005, "errmsg": f"不认识的 msgtype：{kind}"}
        return 200, {"errcode": 0, "errmsg": "ok"}

    def _batch(self, body: dict) -> tuple[int, object]:
        with self._changed:
            self.batches.append(body)
            failure, self.batch_error = self.batch_error, None
        if failure is not None:
            return failure
        users, key, raw = body.get("userIds"), body.get("msgKey"), body.get("msgParam")
        if body.get("robotCode") != CLIENT_ID:
            return self._reject("robotCode 不对")
        if not isinstance(users, list) or not users or not all(isinstance(user, str) for user in users):
            return self._reject("userIds 要是员工编号的列表")
        if key not in ("sampleText", "sampleMarkdown", "sampleImageMsg", "sampleFile") or not isinstance(raw, str):
            return self._reject("msgKey、msgParam 不对")
        param = json.loads(raw)
        if key in ("sampleImageMsg", "sampleFile"):
            return self._batch_media(users[0], key, param)
        words = param.get("text") if key == "sampleMarkdown" else param.get("content")
        if not isinstance(words, str) or (key == "sampleMarkdown" and not param.get("title")):
            return self._reject("msgParam 的内容不对")
        self._record({"via": "batch", "chat": users[0], "msgtype": "markdown" if key == "sampleMarkdown" else "text",
                      "title": param.get("title", ""), "text": words, "plain": words})
        return 200, {"processQueryKey": "fake-query-key", "invalidStaffIdList": [], "flowControlledStaffIdList": []}

    def _batch_media(self, user: str, key: str, param: dict) -> tuple[int, object]:
        """主动发的图片消息（photoURL 是 media_id）或文件消息（mediaId、fileName、fileType）。media_id 要是传上来过的。"""
        if key == "sampleImageMsg":
            upload = self.media.get(str(param.get("photoURL")))
            if upload is None or upload["type"] != "image" or set(param) != {"photoURL"}:
                return self._reject("sampleImageMsg 的 photoURL 不对")
            message = {"via": "batch", "chat": user, "msgtype": "image", "title": "", "text": "", "plain": "", "upload": upload}
        else:
            upload = self.media.get(str(param.get("mediaId")))
            if upload is None or upload["type"] != "file" or not param.get("fileName") or param.get("fileType") not in SAMPLE_FILE_TYPES:
                return self._reject("sampleFile 的 mediaId、fileName、fileType 不对")
            message = {"via": "batch", "chat": user, "msgtype": "file", "title": "", "text": "", "plain": str(param["fileName"]), "upload": upload,
                       "fileType": param["fileType"]}
        self._record(message)
        return 200, {"processQueryKey": "fake-query-key", "invalidStaffIdList": [], "flowControlledStaffIdList": []}

    # ---- Stream 网关（给真的 SDK 用）------------------------------------------------

    def _open(self, body: dict) -> tuple[int, object]:
        if body.get("clientId") != CLIENT_ID or body.get("clientSecret") != self.client_secret:
            return 401, {"code": "unauthorized", "message": "clientId 或 clientSecret 不对"}
        ticket = "ticket-" + secrets.token_hex(12)
        with self._changed:
            self.tickets.append(ticket)
            self.opens.append({name: value for name, value in body.items() if name != "clientSecret"})
        return 200, {"endpoint": self.base_url.replace("http://", "ws://") + "/stream", "ticket": ticket}

    def _stream(self, handler: BaseHTTPRequestHandler, ticket: str) -> None:
        if ticket not in self.tickets:
            fake_ws.accept(handler, status=403)
            return
        ws = fake_ws.accept(handler)
        with self._changed:
            self.sockets.append(ws)
            sent = len(self.frames)
            self._changed.notify_all()
        threading.Thread(target=self._read, args=(ws,), daemon=True).start()
        while not ws.closed.is_set():
            with self._changed:
                pending = self.frames[sent:]
                if not pending:
                    self._changed.wait(0.05)
                    continue
            for frame in pending:
                try:
                    ws.send_json(frame)
                except OSError:
                    return
                sent += 1

    def _read(self, ws: fake_ws.ServerSocket) -> None:
        while not ws.closed.is_set():
            try:
                message = ws.recv(timeout=0.5)
            except (ConnectionError, OSError):
                return
            if message is None:
                continue
            with self._changed:
                self.acks.append(json.loads(message))
                self._changed.notify_all()

    def hang_up(self) -> None:
        """钉钉那边好好地关上 Stream 连接（发一个关闭帧；SDK 的回答由 _read 收）。SDK 会马上重新换票据、再连。"""
        for sock in list(self.sockets):
            if not sock.closed.is_set():
                sock.send_frame(fake_ws.OP_CLOSE, (1000).to_bytes(2, "big"))

    def stream_push(self, message: dict, message_id: str | None = None) -> str:
        """把一条机器人消息包成 Stream 协议的 CALLBACK 帧，发给连着的 SDK。返回帧的 messageId（回执里会带着它）。"""
        message_id = message_id or f"stream-{self._number()}"
        frame = {
            "specVersion": "1.0",
            "type": "CALLBACK",
            "headers": {"appId": "fake-app", "connectionId": "fake-connection", "contentType": "application/json",
                        "messageId": message_id, "time": str(int(time.time() * 1000)), "topic": TOPIC},
            "data": json.dumps(message, ensure_ascii=False),
        }
        with self._changed:
            self.frames.append(frame)
            self._changed.notify_all()
        return message_id

    # ---- 测试扮演"手机上的你" ------------------------------------------------------

    def _attach(self, listener: FakeStreamListener) -> None:
        with self._changed:
            self.listener = listener
            self._changed.notify_all()

    def message(self, text: str = "", user: str | None = None, private: bool = True, msgtype: str | None = None, msg_id: str | None = None,
                nick: str | None = None, webhook: str | None = None, expired: bool = False, picture: bytes | None = None,
                file: tuple[str, bytes] | None = None, rich: list | None = None) -> dict:
        """一条机器人收到的消息，样子和钉钉文档（orgapp/receive-message）里的一样。

        picture 是图片的内容；file 是（文件名，内容）；rich 是富文本的几段（文字，或者图片的内容）。
        webhook 换掉消息带来的 sessionWebhook；expired 让它写着已经过期。"""
        user = user or OWNER
        number = self._number()
        session = secrets.token_hex(16)
        with self._changed:
            self.sessions[session] = {"staff": user, "valid": True}
        now = int(time.time() * 1000)
        data: dict = {
            "conversationId": f"cidPrivate{user}" if private else "cidGroup0000fake",
            "chatbotCorpId": CORP,
            "chatbotUserId": BOT_USER,
            "msgId": msg_id or f"msgFake{number:06d}",
            "senderNick": nick or (OWNER_NAME if user == OWNER else "陌生人"),
            "isAdmin": user == OWNER,
            "senderStaffId": user,
            "sessionWebhookExpiredTime": now + (-60_000 if expired else 5_400_000),
            "createAt": now,
            "senderCorpId": CORP,
            "conversationType": "1" if private else "2",
            "senderId": f"$:LWCP_v1:${user}",
            "sessionWebhook": webhook or f"{self.base_url}/robot/sendBySession?session={session}",
            "robotCode": CLIENT_ID,
        }
        if not private:
            data.update(conversationTitle="项目群", isInAtList=True, atUsers=[{"dingtalkId": BOT_USER}])

        def code(content: bytes) -> str:
            found = f"download-{secrets.token_hex(6)}"
            self.files[found] = content
            return found

        kind = msgtype or ("picture" if picture is not None else "file" if file is not None else "richText" if rich is not None else "text")
        data["msgtype"] = kind
        if kind == "text":
            data["text"] = {"content": text}
        elif kind == "picture":
            data["content"] = {"downloadCode": code(picture or b""), "pictureDownloadCode": "pic-" + secrets.token_hex(4)}
        elif kind == "file":
            name, content = file or ("file.bin", b"")
            data["content"] = {"downloadCode": code(content), "fileName": name, "spaceId": "21000", "fileId": "fake-file"}
        elif kind == "richText":
            parts = []
            for item in rich or []:
                if isinstance(item, bytes):
                    parts.append({"downloadCode": code(item), "pictureDownloadCode": "pic-" + secrets.token_hex(4), "type": "picture"})
                else:
                    parts.append({"text": item})
            data["content"] = {"richText": parts}
        elif kind == "audio":
            data["content"] = {"duration": "2000", "downloadCode": code(b"amr"), "recognition": "你好"}
        elif kind == "video":
            data["content"] = {"duration": "5", "downloadCode": code(b"mp4"), "videoType": "mp4"}
        return data

    def push(self, data: dict) -> None:
        """一条消息到了：交给通道。用的是假的 Stream 连接就直接交过去；用的是真的 SDK 就从 Stream 网关发过去。"""
        if self.use_sdk:
            self.wait_until(lambda: [sock for sock in self.sockets if not sock.closed.is_set()])
            self.stream_push(data)
            return
        listener = self.wait_until(lambda: self.listener if self.listener is not None and self.listener.on_message is not None else None)
        listener.on_message(data)

    def user_says(self, text: str = "", user: str | None = None, private: bool = True, **options) -> dict:
        """你（或者别人）在单聊（或者群里）发了一条消息。返回这条消息（再推一次同一条时用）。参数见 message。"""
        data = self.message(text, user, private, **options)
        self.push(data)
        return data

    def is_prompt(self, message: dict) -> bool:
        """确认请求：最后一段写着工作目录和"回复允许或拒绝"。（只看后一句不够："还有一个操作在等你批准：回复……"也有这句话。）"""
        return "工作目录：" in message["plain"] and HINT in message["plain"]

    # ---- 等 --------------------------------------------------------------------

    def wait_sent(self, predicate=lambda message: True, timeout: float = 10.0, after: int = 0) -> dict:
        deadline = time.monotonic() + timeout
        with self._changed:
            while True:
                for message in self.sent[after:]:
                    if predicate(message):
                        return message
                remaining = deadline - time.monotonic()
                assert remaining > 0, "等不到机器人发的消息。已经发的：\n" + "\n".join(message["plain"] for message in self.sent) + "\n被拒绝的：\n" + "\n".join(self.rejected)
                self._changed.wait(remaining)

    def wait_until(self, condition, timeout: float = 10.0):
        deadline = time.monotonic() + timeout
        while True:
            with self._changed:
                value = condition()
                if value:
                    return value
                remaining = deadline - time.monotonic()
                assert remaining > 0, "等不到条件成立"
                self._changed.wait(min(remaining, 0.05))


class FakeStreamListener:
    """dingtalk.Listener 的假的那一个：不连钉钉、不用 SDK。run 报一声"连上了"，一直等到 stop；
    这期间 FakeDingTalk.push 把消息交给 run 收到的 on_message。"""

    def __init__(self, fake: FakeDingTalk):
        self.fake = fake
        self.on_message = None
        self.runs = 0
        self._stopped = threading.Event()

    def run(self, on_message, on_state) -> str:
        self.runs += 1
        if self._stopped.is_set():
            return "stopped"
        self.on_message = on_message
        self.fake._attach(self)
        on_state("online", "")
        self._stopped.wait()
        self.on_message = None
        return "stopped"

    def stop(self) -> None:
        self._stopped.set()
