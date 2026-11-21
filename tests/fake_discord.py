"""本地的假 Discord（REST 接口加 Gateway），只在测试和演示里用。不连真正的 Discord，不需要真的机器人。

REST 照着真的回答：/users/@me、/oauth2/applications/@me、/gateway/bot、发消息、改消息、"正在输入"、登记命令、
回应交互（按钮和斜杠命令），并记下机器人发出去的每一样东西。像真的一样较真的几处（错了就回 400，测试会看到发送失败）：

  - 消息超过 2000 个字符；没有关掉 @ 提醒（allowed_mentions 不是 {"parse": []}）
  - 按钮的字超过 80 个、custom_id 超过 100 个、一行超过 5 个按钮
  - 同一次交互回应了两次（真的会回 40060 "已经回应过了"）；斜杠命令回了只有按钮才能用的 type 6
  - 改一条不存在的消息（10008 Unknown Message）

Gateway 是同一个端口上的 WebSocket（GET /gateway，带 Upgrade: websocket；READY 里给的 resume_gateway_url 是 /resume，
和真的一样是另一个地址）：Hello → Identify 或 Resume → READY / RESUMED → 心跳和 ACK → 事件。
测试代码扮演手机上的你（user_says、press、command），也能让网关做真的会做的事：要你重连（reconnect）、
会话失效（invalid_session）、断网（drop）、用某个代码关掉（close）、不回心跳（ack = False）。
客户端自己用 1000 或 1001 关掉连接时，会话就作废了（真的 Discord 也是这样），之后的 Resume 会被拒绝。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import fake_ws

from harness.net.websocket import OP_CLOSE

TOKEN = "MTIzNDU2Nzg5MDEyMzQ1Njc4.GfakeA.fake-discord-token-for-tests-0123456789"
BOT_ID = "1234567890123456789"
APP_ID = "1234567890123456789"
OWNER = "111222333444555666"
OWNER_CHAT = "999000111222333444"  # 主人和机器人的私聊（DM channel）的编号
OWNER_NAME = "Akxan (@akxan)"
GATEWAY_PATHS = ("/gateway", "/resume")  # /gateway/bot 给的地址、READY 里给的 resume_gateway_url


def utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _bad_components(components: object) -> str:
    """按钮的样子不对的话，返回原因（和真的 Discord 一样会拒绝的那几条）。"""
    if not isinstance(components, list):
        return "components must be a list"
    for row in components:
        if not isinstance(row, dict) or row.get("type") != 1 or not isinstance(row.get("components"), list):
            return "a top-level component must be an action row"
        if len(row["components"]) > 5:
            return "an action row holds at most 5 buttons"
        for button in row["components"]:
            if button.get("type") != 2 or len(str(button.get("label") or "")) > 80 or not 0 < len(str(button.get("custom_id") or "")) <= 100:
                return "invalid button"
    return ""


class FakeDiscord:
    platform = "discord"

    def __init__(self, heartbeat_ms: int = 200, max_identifies: int = 1000):
        self.credentials = {"token": TOKEN}
        self.bot = {"id": BOT_ID, "username": "zhifox_test", "name": "ZhiFox Test", "app": APP_ID, "link": f"https://discord.com/users/{BOT_ID}",
                    "install": f"https://discord.com/oauth2/authorize?client_id={APP_ID}&integration_type=1&scope=applications.commands"}
        self.owner, self.owner_chat, self.owner_name = OWNER, OWNER_CHAT, OWNER_NAME
        self.secrets = [TOKEN]
        self.heartbeat_ms = heartbeat_ms
        self.remaining = max_identifies  # 今天还能 Identify 几次（/gateway/bot 的 session_start_limit.remaining）
        self.sent: list[dict] = []  # 机器人发的消息：{"id", "chat", "content", "components", "plain"}
        self.edits: list[dict] = []
        self.typing: list[str] = []
        self.commands: list | None = None
        self.callbacks: list[dict] = []  # 回应交互：{"id", "token", "type", "data"}
        self.timeline: list[tuple[str, str]] = []  # 发消息、改消息、回应交互的先后：("message" | "edit" | "callback", 编号)
        self.files: dict[str, bytes] = {}
        self.downloads: list[dict] = []  # 被下载过的附件：{"key", "authorization"（下载时带没带 token，应该一直是 None）}
        self.identifies: list[dict] = []
        self.resumes: list[dict] = []
        self.heartbeats = 0
        self.heartbeat_seqs: list[object] = []  # 每次心跳带着的序号
        self.ack = True  # 设成 False：不回心跳的 ACK（让客户端发现连接"僵死"了）
        self.requests: list[str] = []  # 每个 REST 请求的方法和路径
        self.fail_next: dict[str, tuple[int, dict]] = {}  # "POST /channels/x/messages" 这样的键 → 下一次回答的错误
        self.events: list[dict] = []  # 发给机器人的事件（带序号），断线以后 Resume 要补发
        self.session_id = ""
        self.connections = 0
        self.urls: list[str] = []  # 每次连网关用的地址（路径加查询）
        self.closes: list[int | None] = []  # 每条网关连接是客户端用什么代码关掉的（没打招呼就断了是 None）
        self._interactions: dict[str, int] = {}  # 发出去的交互 → 它的 type（2 斜杠命令、3 按钮）
        self._sockets: list[fake_ws.ServerSocket] = []
        self._next_id = 1000
        self._changed = threading.Condition()

    # ---- 启动 ------------------------------------------------------------------

    def start(self, port: int = 0) -> FakeDiscord:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _body(self) -> object:
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                return json.loads(raw) if raw else None

            def _reply(self, status: int, payload: object = None) -> None:
                data = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method: str) -> None:
                parts = urlsplit(self.path)
                path = parts.path
                if method == "GET" and path.rstrip("/") in GATEWAY_PATHS and self.headers.get("Upgrade", "").lower() == "websocket":
                    with fake._changed:
                        fake.urls.append(path + (f"?{parts.query}" if parts.query else ""))
                    fake._gateway(fake_ws.accept(self))
                    return
                if method == "GET" and path.startswith("/cdn/"):
                    key = path.removeprefix("/cdn/")
                    content = fake.files.get(key)
                    if content is None:
                        self._reply(404, {"message": "Not Found"})
                        return
                    with fake._changed:
                        fake.downloads.append({"key": key, "authorization": self.headers.get("Authorization")})
                    self.send_response(200)
                    self.send_header("content-length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                    return
                body = self._body()
                status, payload = fake._api(method, path.removeprefix("/api/v10"), body, self.headers)
                self._reply(status, payload)

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

            def do_PATCH(self) -> None:
                self._handle("PATCH")

            def do_PUT(self) -> None:
                self._handle("PUT")

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self

    def config(self) -> dict:
        return {"discord_api": self.base_url + "/api/v10"}

    def factory(self, config, store):
        from harness.channels.discord import DiscordChannel

        entry = store.get("discord")
        return DiscordChannel(entry["credentials"].get("token", ""), config.discord_api, app_id=(entry["bot"] or {}).get("app", ""))

    def stop(self) -> None:
        with self._changed:
            self._changed.notify_all()
        for sock in list(self._sockets):
            sock.drop()
        self.server.shutdown()
        self.server.server_close()

    def _new_id(self) -> int:
        with self._changed:
            self._next_id += 1
            return self._next_id

    # ---- REST ------------------------------------------------------------------

    def _api(self, method: str, path: str, body: object, headers) -> tuple[int, object]:
        with self._changed:
            self.requests.append(f"{method} {path}")
            failure = self.fail_next.pop(f"{method} {path}", None)
        if failure is not None:
            return failure
        if path.startswith("/interactions/"):  # 回应交互不需要 token
            return self._callback(path, body)
        if headers.get("Authorization") != f"Bot {TOKEN}":
            return 401, {"message": "401: Unauthorized", "code": 0}
        if not headers.get("User-Agent", "").startswith("DiscordBot ("):
            return 400, {"message": "bad user agent", "code": 0}
        if method == "GET" and path == "/users/@me":
            return 200, {"id": BOT_ID, "username": "zhifox_test", "global_name": "ZhiFox Test", "bot": True}
        if method == "GET" and path == "/oauth2/applications/@me":
            return 200, {"id": APP_ID, "name": "ZhiFox Test"}
        if method == "GET" and path == "/gateway/bot":
            return 200, {"url": self.base_url.replace("http://", "ws://") + "/gateway", "shards": 1,
                         "session_start_limit": {"total": 1000, "remaining": self.remaining, "reset_after": 60_000, "max_concurrency": 1}}
        if method == "PUT" and path == f"/applications/{APP_ID}/commands":
            self.commands = body  # type: ignore[assignment]
            return 200, body
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[0] == "channels":
            chat = parts[1]
            if method == "POST" and parts[2] == "typing":
                with self._changed:
                    self.typing.append(chat)
                    self._changed.notify_all()
                return 204, None
            assert isinstance(body, dict)
            content = str(body.get("content") or "")
            if utf16(content) > 2000:
                return 400, {"code": 50035, "message": "Invalid Form Body: content must be 2000 or fewer in length."}
            if body.get("allowed_mentions") != {"parse": []}:
                return 400, {"code": 0, "message": "测试要求每条消息都关掉 @ 提醒"}
            problem = _bad_components(body.get("components") or [])
            if problem:
                return 400, {"code": 50035, "message": f"Invalid Form Body: {problem}"}
            with self._changed:
                if method == "POST" and parts[2] == "messages":
                    if not content.strip():
                        return 400, {"code": 50006, "message": "Cannot send an empty message"}
                    self._next_id += 1
                    message = {"id": str(self._next_id), "chat": chat, "content": content, "components": body.get("components") or [], "plain": content}
                    self.sent.append(message)
                    self.timeline.append(("message", message["id"]))
                    self._changed.notify_all()
                    return 200, {"id": message["id"], "channel_id": chat, "content": content}
                if method == "PATCH" and parts[2] == "messages" and len(parts) == 4:
                    if not any(message["id"] == parts[3] and message["chat"] == chat for message in self.sent):
                        return 404, {"code": 10008, "message": "Unknown Message"}
                    self.edits.append({"id": parts[3], "chat": chat, **body})
                    self.timeline.append(("edit", parts[3]))
                    self._changed.notify_all()
                    return 200, {"id": parts[3], "channel_id": chat, "content": content}
        return 404, {"message": "404: Not Found", "code": 0}

    def _callback(self, path: str, body: object) -> tuple[int, object]:
        """POST /interactions/{编号}/{交互的 token}/callback：每次交互只能回应一次。"""
        _, _, interaction, token, _ = path.split("/", 4)
        body = body if isinstance(body, dict) else {}
        with self._changed:
            kind = self._interactions.get(interaction)
            if kind is None:
                return 404, {"code": 10062, "message": "Unknown interaction"}
            if any(item["id"] == interaction for item in self.callbacks):
                return 400, {"code": 40060, "message": "Interaction has already been acknowledged."}
            if body.get("type") == 6 and kind != 3:  # "收到了，待会儿再改消息"只有按钮这类组件能用
                return 400, {"code": 50035, "message": "Invalid Form Body: interaction response type"}
            if body.get("type") == 4 and not 0 < utf16(str((body.get("data") or {}).get("content") or "")) <= 2000:
                return 400, {"code": 50035, "message": "Invalid Form Body: content"}
            self.callbacks.append({"id": interaction, "token": token, **body})
            self.timeline.append(("callback", interaction))
            self._changed.notify_all()
        return 204, None

    # ---- Gateway ---------------------------------------------------------------

    def _gateway(self, ws: fake_ws.ServerSocket) -> None:
        with self._changed:
            self.connections += 1
            self._sockets.append(ws)
        served = ""
        try:
            served = self._serve(ws)
        finally:
            ws.closed.wait(2)
            ws.drop()
            with self._changed:
                self.closes.append(ws.close_code)
                # 客户端说"不要了"（1000、1001）：这个会话作废，真的 Discord 也是这样。已经换了新会话的不管
                if ws.close_code in (1000, 1001) and served and self.session_id == served:
                    self.session_id = ""
                self._changed.notify_all()

    def _serve(self, ws: fake_ws.ServerSocket) -> str:
        """一条网关连接从头到尾。返回它用的会话编号（没登录成功是空字符串）。"""
        ws.send_json({"op": 10, "d": {"heartbeat_interval": self.heartbeat_ms}})
        first = ws.recv_json(timeout=5)
        if first is None:
            return ""
        data = first.get("d") or {}
        if data.get("token") != TOKEN:
            ws.close(4004, "Authentication failed.")
            return ""
        if first.get("op") == 2:
            with self._changed:
                self.identifies.append(data)
                self.remaining -= 1
                self.session_id = served = f"session-{len(self.identifies)}"
                self.events = []
            ready = self._dispatch(ws, "READY", {"session_id": self.session_id, "resume_gateway_url": self.base_url.replace("http://", "ws://") + "/resume",
                                                 "user": {"id": BOT_ID, "username": "zhifox_test", "bot": True}, "application": {"id": APP_ID}})
            sent = ready["s"]
        elif first.get("op") == 6:
            with self._changed:
                self.resumes.append(data)
                valid = bool(self.session_id) and data.get("session_id") == self.session_id
            if not valid:
                ws.send_json({"op": 9, "d": False})
                ws.recv(timeout=2)
                return ""
            served = str(data.get("session_id"))
            sent = int(data.get("seq") or 0)
            with self._changed:
                missed = [event for event in self.events if event["s"] > sent]
            for event in missed:  # 先把断线期间漏掉的补发过去，再说 RESUMED（和真的一样）
                ws.send_json(event)
                sent = event["s"]
            ws.send_json({"op": 0, "t": "RESUMED", "s": None, "d": {}})
        else:
            ws.close(4003, "Not authenticated.")
            return ""
        reader = threading.Thread(target=self._read, args=(ws,), daemon=True)
        reader.start()
        while not ws.closed.is_set():
            with self._changed:
                pending = [event for event in self.events if event["s"] > sent]
                if not pending:
                    self._changed.wait(0.05)
                    continue
            for event in pending:
                try:
                    ws.send_json(event)
                except OSError:
                    return served
                sent = event["s"]
        return served

    def _read(self, ws: fake_ws.ServerSocket) -> None:
        """一条连接上唯一读客户端消息的线程（关闭握手的回应也由它收：两个线程同时读同一个套接字会把帧读乱）。"""
        while not ws.closed.is_set():
            try:
                message = ws.recv(timeout=0.5)
            except (ConnectionError, OSError):
                ws.closed.set()
                return
            if message is None:
                continue
            payload = json.loads(message)
            if payload.get("op") == 1:
                with self._changed:
                    self.heartbeats += 1
                    self.heartbeat_seqs.append(payload.get("d"))
                    self._changed.notify_all()
                if self.ack:
                    try:
                        ws.send_json({"op": 11})
                    except OSError:
                        return

    def _dispatch(self, ws: fake_ws.ServerSocket | None, kind: str, data: dict) -> dict:
        """一个事件：带上下一个序号，记进 events（Resume 时从这里补发）。ws 不是 None 时马上发出去（READY、RESUMED）。"""
        with self._changed:
            seq = (self.events[-1]["s"] if self.events else 0) + 1
            event = {"op": 0, "t": kind, "s": seq, "d": data}
            if ws is None:
                self.events.append(event)
                self._changed.notify_all()
        if ws is not None:
            with self._changed:
                self.events.append(event)
            ws.send_json(event)
        return event

    def socket(self) -> fake_ws.ServerSocket:
        """现在开着的那条网关连接。"""
        return next(sock for sock in reversed(self._sockets) if not sock.closed.is_set())

    def reconnect(self) -> None:
        """网关说"请重连"（op 7）。"""
        self.socket().send_json({"op": 7, "d": None})

    def invalid_session(self, resumable: bool = False) -> None:
        """网关说"会话失效了"（op 9）。不能 Resume 的话，这个会话就作废了：再拿它来 Resume 会被拒绝。"""
        if not resumable:
            with self._changed:
                self.session_id = ""
        self.socket().send_json({"op": 9, "d": resumable})

    def drop(self) -> None:
        """网络断了：不打招呼就断开。"""
        self.socket().drop()

    def close(self, code: int, reason: str = "") -> None:
        """网关用 code 关掉连接（4004 token 不对、4009 会话超时……）。4007、4009 之后这个会话不能再 Resume。

        只发关闭帧，客户端的回应交给那条连接的读线程去收（不和它抢着读），等它断开。"""
        if code in (4007, 4009):
            with self._changed:
                self.session_id = ""
        sock = self.socket()
        try:
            sock.send_frame(OP_CLOSE, code.to_bytes(2, "big") + reason.encode())
        except OSError:
            pass
        sock.closed.wait(2)
        sock.drop()

    # ---- 测试扮演"手机上的你" ------------------------------------------------------

    def user_says(self, text: str = "", user: str | None = None, private: bool = True, name: str = "Akxan", username: str = "akxan",
                  chat: str | None = None, attachments: list[tuple] | None = None, bot: bool = False, extra: dict | None = None, **_: object) -> dict:
        """你（或者别人）在私聊（或者服务器的频道）里发了一条消息。

        attachments 是（文件名，类型，内容）或者（文件名，类型，内容，Discord 说的大小）；extra 里的字段原样加进消息
        （比如 sticker_items）。"""
        user = user or OWNER
        data: dict = {
            "id": str(self._new_id()),
            "channel_id": chat or (OWNER_CHAT if user == OWNER else f"dm-{user}"),
            "author": {"id": user, "username": username, "global_name": name, "bot": bot},
            "content": text,
            "attachments": [],
            **(extra or {}),
        }
        if not private:
            data["guild_id"] = "guild-1"
        for index, item in enumerate(attachments or []):
            filename, media_type, content = item[:3]
            size = item[3] if len(item) > 3 else len(content)
            key = f"{data['id']}-{index}/{filename}"
            self.files[key] = content
            data["attachments"].append({"id": f"a{index}", "filename": filename, "size": size, "content_type": media_type,
                                        "url": f"{self.base_url}/cdn/{key}"})
        return self._dispatch(None, "MESSAGE_CREATE", data)

    def is_prompt(self, message: dict) -> bool:
        return bool(message.get("components"))

    def buttons(self, message: dict) -> list[dict]:
        return [button for row in message.get("components") or [] for button in row.get("components", [])]

    def _interaction(self, kind: int, data: dict) -> dict:
        number = self._new_id()
        interaction = {"id": f"interaction-{number}", "token": f"itoken-{number}", "type": kind, **data}
        with self._changed:
            self._interactions[interaction["id"]] = kind
        return self._dispatch(None, "INTERACTION_CREATE", interaction)

    def press(self, message: dict, allow: bool = True, user: str | None = None, data: str | None = None, private: bool = True) -> dict:
        """点了消息下面的一个按钮（INTERACTION_CREATE，type 3）。返回这个事件（d.id 是交互的编号）。"""
        allow_button, deny_button = self.buttons(message)
        custom = data or (allow_button if allow else deny_button)["custom_id"]
        person = {"id": user or OWNER, "username": "akxan", "global_name": "Akxan"}
        fields: dict = {"channel_id": message["chat"], "message": {"id": message["id"]}, "data": {"custom_id": custom, "component_type": 2}}
        if private:
            fields["user"] = person
        else:
            fields.update(guild_id="guild-1", member={"user": person})
        return self._interaction(3, fields)

    def command(self, name: str, user: str | None = None, private: bool = True, chat: str | None = None) -> dict:
        """用了一个斜杠命令（INTERACTION_CREATE，type 2）。返回这个事件。"""
        person = {"id": user or OWNER, "username": "akxan", "global_name": "Akxan"}
        fields: dict = {"channel_id": chat or OWNER_CHAT, "data": {"name": name, "type": 1}}
        if private:
            fields["user"] = person
        else:
            fields.update(guild_id="guild-1", member={"user": person})
        return self._interaction(2, fields)

    def callback_of(self, event: dict, timeout: float = 10.0) -> dict:
        """等机器人回应这次交互（按钮或者斜杠命令），返回那次回应。"""
        interaction = event["d"]["id"]
        return self.wait_until(lambda: next((item for item in self.callbacks if item["id"] == interaction), None), timeout)

    def wait_sent(self, predicate=lambda message: True, timeout: float = 10.0, after: int = 0) -> dict:
        deadline = time.monotonic() + timeout
        with self._changed:
            while True:
                for message in self.sent[after:]:
                    if predicate(message):
                        return message
                remaining = deadline - time.monotonic()
                assert remaining > 0, "等不到机器人发的消息。已经发的：\n" + "\n".join(message["plain"] for message in self.sent)
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

    def edit_of(self, message: dict) -> dict:
        return self.wait_until(lambda: next((edit for edit in self.edits if edit["id"] == message["id"]), None))
