"""本地的假 QQ 机器人开放平台（换 access_token、REST 接口、WebSocket 网关），只在测试和演示里用。
不连真正的 QQ，不需要真的机器人，AppID 和 AppSecret 都是编造的。

一个端口上什么都有，照着官方文档（https://bot.q.qq.com/wiki/ ，见 harness/channels/qq.py 开头）回答：

  POST /app/getAppAccessToken   用 AppID + AppSecret 换 access_token。expires_in 和文档的例子一样是一个字符串。
                                还剩不到 60 秒时再来要，给一个新的（旧的在它自己的有效期里照样能用）；
                                AppSecret 不对，回 HTTP 200 加 {"code": 100016}（真的也是这样，不是 401）
  GET  /users/@me、/gateway     要带 Authorization: QQBot {access_token}，token 不对或者过期了回 401
  POST /v2/users/{openid}/messages
                                发单聊消息。带 msg_id 的是"被动回复"：msg_id 必须是这个用户发来的一条消息，
                                60 分钟以内、每条最多回 4 次（超了回 40034128），同一个 msg_id 的 msg_seq 不能重复
                                （重复回 40054005）。不带 msg_id 的是"主动消息"，照收
  GET  /websocket/（Upgrade）   网关：Hello → Identify 或 Resume → READY / RESUMED → 心跳和 ACK → 事件

测试代码扮演手机上的你（user_says：单聊或者群里 @ 机器人，可以带附件，可以让同一条推两次），
也能让平台做真的会做的事：access_token 提前作废（expire）、被动回复过期（passive_seconds = 0）、
要你重连（reconnect）、会话失效（invalid_session）、断网（drop）、用某个代码关掉（close）、不回心跳（ack = False），
以及某一次请求出错（fail_next 是下一次，scripts 能挑第几次）。

真的平台会拒绝、而测试里本不该出现的请求（往群里发消息、msg_id 不是这个用户的、空消息、Identify 的参数不对……），
除了回一个错误，还记在 problems 里。tests/test_channels_qq.py 的每个测试结束时都会检查它是空的。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import fake_ws

from harness.net.websocket import OP_CLOSE

APP_ID = "102345678"
APP_SECRET = "fakeQQappSecret0123456789abcdefg"  # 编造的，长得像真的（32 位）
BOT_ID = "14551234567890123456"
BOT_NAME = "智狐测试"
SHARE_URL = f"https://qun.qq.com/qunpro/robot/qunshare?robot_appid={APP_ID}&robot_uin=3889001234"
OWNER = "E4F4AEA33253A2797FB897C50B81D7ED"  # 主人在这个机器人眼里的 user_openid（每个机器人看到的都不一样）
OWNER_NAME = f"QQ 用户 {OWNER[-6:]}"  # 单聊的消息里没有名字，qq.py 用 openid 的最后 6 位
STRANGER = "0A1B2C3D4E5F60718293A4B5C6D7E8F9"
GROUP = "C9F778FE6ADF9D1D1DBE395BF744A33A"
HINT = "回复“允许”或“拒绝”。"  # 确认请求的最后一句（session.py 的 Prompt.hint）
INTENTS = 1 << 25  # GROUP_AND_C2C_EVENT
PASSIVE_REPLIES = 4
EXPIRED, DUPLICATE = 40034128, 40054005
CHINA = timezone(timedelta(hours=8))


class FakeQQ:
    platform = "qq"

    def __init__(self, heartbeat_ms: int = 200, token_seconds: int = 7200):
        self.credentials = {"app_id": APP_ID, "app_secret": APP_SECRET}
        self.bot = {"id": BOT_ID, "username": BOT_NAME, "name": BOT_NAME, "at": False, "link": SHARE_URL}  # 和 qq.check 返回的一样
        self.owner = self.owner_chat = OWNER  # 单聊的"聊天编号"就是对方的 openid
        self.owner_name = OWNER_NAME
        self.secrets = [APP_SECRET]  # 发出去的每一个 access_token 也会加进来
        self.heartbeat_ms = heartbeat_ms
        self.token_seconds = token_seconds  # 新发的 access_token 能用多久
        self.expires_as_text = True  # expires_in 写成字符串（文档的例子就是 "7200"）
        self.tokens: dict[str, float] = {}  # 还能用的 access_token → 什么时候过期（time.monotonic）
        self.current = ""  # 最近发的那个 access_token
        self.issued = 0
        self.passive_seconds = 3600.0  # 被动回复的窗口。设成 0：之后的被动回复都算过期
        self.sent: list[dict] = []  # 机器人发出去的消息：{"id", "chat", "plain", "msg_id", "msg_seq", "token"}
        self.rejected: list[dict] = []  # 因为被动回复的规矩被拒绝的：{"chat", "plain", "msg_id", "msg_seq", "code"}
        self.messages: dict[str, dict] = {}  # 用户发给机器人的单聊消息：id → {"user", "at", "seqs"}
        self.problems: list[str] = []  # 真的平台会拒绝、测试里本不该出现的请求
        self.files: dict[str, bytes] = {}
        self.identifies: list[dict] = []
        self.resumes: list[dict] = []
        self.heartbeats = 0
        self.ack = True  # 设成 False：不回心跳的 ACK（让客户端发现连接"僵死"了）
        self.requests: list[str] = []  # 每个请求的方法和路径
        self.refused: list[str] = []  # 因为 access_token 不对被 401 的请求
        self.fail_next: dict[str, tuple[int, dict]] = {}  # "POST /v2/users/x/messages" 这样的键 → 下一次回答的错误
        # 同样的键 → 接下来几次各怎么回答：None 是照常回答，(状态码, 内容) 是这一次回答的错误。比 fail_next 能挑"第几次"出错
        self.scripts: dict[str, list[tuple[int, dict] | None]] = {}
        self.events: list[dict] = []  # 发给机器人的事件（带序号），断线以后 Resume 要补发
        self.session_id = ""
        self.connections = 0
        self.last_message_id = ""
        self._sockets: list[fake_ws.ServerSocket] = []
        self._next_id = 0
        self._changed = threading.Condition()

    # ---- 启动 ------------------------------------------------------------------

    def start(self, port: int = 0) -> FakeQQ:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _body(self) -> object:
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                return json.loads(raw) if raw else None

            def _reply(self, status: int, payload: object = None) -> None:
                data = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method: str) -> None:
                path = urlsplit(self.path).path
                if method == "GET" and path.rstrip("/") == "/websocket" and self.headers.get("Upgrade", "").lower() == "websocket":
                    fake._gateway(fake_ws.accept(self))
                    return
                if method == "GET" and path.startswith("/cdn/"):
                    content = fake.files.get(path.removeprefix("/cdn/"))
                    if content is None:
                        self._reply(404, {"message": "not found"})
                        return
                    self.send_response(200)
                    self.send_header("content-length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                    return
                status, payload = fake._api(method, path, self._body(), self.headers)
                self._reply(status, payload)

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
        return {"qq_api": self.base_url}

    def factory(self, config, store):
        from harness.channels import qq

        return qq.make(config, store)  # 和真的一样建（心跳的间隔由假网关的 Hello 定，已经很短了）

    def stop(self) -> None:
        with self._changed:
            self._changed.notify_all()
        for sock in list(self._sockets):
            sock.drop()
        self.server.shutdown()
        self.server.server_close()

    # ---- access_token ------------------------------------------------------------

    def _token(self, body: object) -> tuple[int, dict]:
        given = body if isinstance(body, dict) else {}
        if given.get("appId") != APP_ID or given.get("clientSecret") != APP_SECRET:
            return 200, {"code": 100016, "message": "invalid appid or secret"}
        with self._changed:
            now = time.monotonic()
            left = self.tokens.get(self.current, 0.0) - now
            if left <= 60:  # 没有、作废了、或者快过期了：换一个新的
                self.issued += 1
                self.current = f"fake-qq-access-token-{self.issued:04d}-c0ffee0123456789"
                self.tokens[self.current] = now + self.token_seconds
                self.secrets.append(self.current)
                left = self.token_seconds
            expires: int | str = str(int(left)) if self.expires_as_text else int(left)
            return 200, {"access_token": self.current, "expires_in": expires}

    def expire(self) -> None:
        """现在的 access_token 提前作废了（比如它在别的地方被刷新掉了）：再用它的请求都回 401。"""
        with self._changed:
            self.tokens.pop(self.current, None)

    def _valid(self, authorization: str) -> bool:
        kind, _, token = authorization.partition(" ")
        with self._changed:
            return kind == "QQBot" and self.tokens.get(token, 0.0) > time.monotonic()

    # ---- REST ------------------------------------------------------------------

    def _api(self, method: str, path: str, body: object, headers) -> tuple[int, object]:
        with self._changed:
            self.requests.append(f"{method} {path}")
            failure = self.fail_next.pop(f"{method} {path}", None)
            script = self.scripts.get(f"{method} {path}")
            if failure is None and script:
                failure = script.pop(0)
        if failure is not None:
            return failure
        if method == "POST" and path == "/app/getAppAccessToken":
            return self._token(body)
        if not self._valid(headers.get("Authorization", "")):
            with self._changed:
                self.refused.append(f"{method} {path}")
            return 401, {"code": 11244, "message": "token not exist or expire"}
        if method == "GET" and path == "/users/@me":
            return 200, {"id": BOT_ID, "username": BOT_NAME, "avatar": "https://thirdqq.qlogo.cn/0", "share_url": SHARE_URL}
        if method == "GET" and path == "/gateway":
            return 200, {"url": self.base_url.replace("http://", "ws://") + "/websocket/"}
        parts = path.strip("/").split("/")
        if method == "POST" and len(parts) == 4 and parts[:2] == ["v2", "users"] and parts[3] == "messages":
            return self._send(parts[2], body if isinstance(body, dict) else {}, headers.get("Authorization", "").partition(" ")[2])
        if method == "POST" and parts[:2] == ["v2", "groups"]:
            self.problems.append(f"往群里发了消息：{path}")  # 群里的消息一律不理，更不回
        return 404, {"code": 404, "message": "not found"}

    def _error(self, code: int, message: str) -> tuple[int, dict]:
        return 400, {"code": code, "err_code": code, "message": message, "trace_id": "fake-trace"}

    def _send(self, chat: str, body: dict, token: str) -> tuple[int, dict]:
        content, msg_id, msg_seq = body.get("content"), body.get("msg_id"), body.get("msg_seq")
        if not isinstance(content, str) or not content.strip() or body.get("msg_type") != 0:
            self.problems.append(f"消息的内容或者类型不对：{body!r}")
            return self._error(50006, "消息内容为空或者类型不对")
        record = {"chat": chat, "plain": content, "msg_id": msg_id, "msg_seq": msg_seq}
        with self._changed:
            if msg_id is not None:  # 被动回复：按规矩查一遍
                seq = msg_seq if isinstance(msg_seq, int) else 1  # 文档：不填默认是 1
                original = self.messages.get(str(msg_id))
                if original is None or original["user"] != chat:
                    self.problems.append(f"msg_id 不是这个用户发来的消息：{msg_id}")
                    return self._error(40034024, "msg_id 无效")
                code = 0
                if time.time() - original["at"] > self.passive_seconds:
                    code, why = EXPIRED, "被动回复已过期"
                elif len(original["seqs"]) >= PASSIVE_REPLIES:
                    code, why = EXPIRED, "被动回复次数已用完"
                elif seq in original["seqs"]:
                    code, why = DUPLICATE, "消息被去重，请检查请求 msg_seq"
                if code:
                    self.rejected.append({**record, "code": code})
                    self._changed.notify_all()
                    return self._error(code, why)
                original["seqs"].add(seq)
            self._next_id += 1
            message = {"id": f"sent-{self._next_id}", **record, "token": token}
            self.sent.append(message)
            self._changed.notify_all()
        return 200, {"id": message["id"], "timestamp": datetime.now(CHINA).isoformat(timespec="seconds")}

    # ---- 网关 --------------------------------------------------------------------

    def _gateway(self, ws: fake_ws.ServerSocket) -> None:
        with self._changed:
            self.connections += 1
            self._sockets.append(ws)
        ws.send_json({"op": 10, "d": {"heartbeat_interval": self.heartbeat_ms}})
        first = ws.recv_json(timeout=5)
        if first is None:
            ws.drop()
            return
        data = first.get("d") if isinstance(first.get("d"), dict) else {}
        if not self._valid(str(data.get("token") or "")):
            ws.close(4004, "invalid token")
            return
        if first.get("op") == 2:
            if data.get("intents") != INTENTS or data.get("shard") != [0, 1]:
                self.problems.append(f"Identify 的参数不对：{data!r}")
                ws.close(4013, "invalid intent")
                return
            with self._changed:
                self.identifies.append(data)
                self.session_id = f"qq-session-{len(self.identifies)}"
                self.events = []
            ready = self._dispatch(ws, "READY", {"version": 1, "session_id": self.session_id,
                                                 "user": {"id": BOT_ID, "username": BOT_NAME, "bot": True, "status": 1}, "shard": [0, 1]})
            sent = ready["s"]
        elif first.get("op") == 6:
            with self._changed:
                self.resumes.append(data)
                valid = data.get("session_id") == self.session_id
            if not valid:
                ws.send_json({"op": 9, "d": False})
                ws.recv(timeout=2)
                return
            sent = int(data.get("seq") or 0)
            with self._changed:
                missed = [event for event in self.events if event["s"] > sent]
            for event in missed:  # 先把断线期间漏掉的补发过去，再说 RESUMED
                ws.send_json(event)
                sent = event["s"]
            ws.send_json({"op": 0, "t": "RESUMED", "s": sent, "d": ""})  # QQ 的 RESUMED，d 是一个空字符串
        else:
            ws.close(4001, "invalid opcode")
            return
        threading.Thread(target=self._read, args=(ws,), daemon=True).start()
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
                    return
                sent = event["s"]

    def _read(self, ws: fake_ws.ServerSocket) -> None:
        """这条连接上唯一读的线程（close 也靠它收客户端回的关闭帧，免得两个线程抢着读同一个 socket）。"""
        while not ws.closed.is_set():
            try:
                message = ws.recv(timeout=0.5)
            except (ConnectionError, OSError):
                return
            if message is None:
                continue
            payload = json.loads(message)
            if payload.get("op") == 1:
                with self._changed:
                    self.heartbeats += 1
                    self._changed.notify_all()
                if self.ack:
                    try:
                        ws.send_json({"op": 11})
                    except OSError:
                        return
            elif payload.get("op") in (2, 6):
                self.problems.append("同一条连接上又登录了一次")

    def _dispatch(self, ws: fake_ws.ServerSocket | None, kind: str, data: dict) -> dict:
        """一个事件：带上下一个序号，记进 events（Resume 时从这里补发）。ws 不是 None 时马上发出去（READY）。"""
        with self._changed:
            seq = (self.events[-1]["s"] if self.events else 0) + 1
            event = {"op": 0, "id": f"{kind}:{seq}", "t": kind, "s": seq, "d": data}
            self.events.append(event)
            if ws is None:
                self._changed.notify_all()
        if ws is not None:
            ws.send_json(event)
        return event

    def socket(self) -> fake_ws.ServerSocket:
        """现在开着的那条网关连接。"""
        return next(sock for sock in reversed(self._sockets) if not sock.closed.is_set())

    def reconnect(self) -> None:
        """网关说"请重连"（op 7）。"""
        self.socket().send_json({"op": 7})

    def invalid_session(self, resumable: bool = False) -> None:
        self.socket().send_json({"op": 9, "d": resumable})

    def drop(self) -> None:
        """网络断了：不打招呼就断开。"""
        self.socket().drop()

    def close(self, code: int, reason: str = "") -> None:
        """用 code 关掉连接：发关闭帧，等客户端回一个（读的线程会收到它并断开），最多等 2 秒。"""
        sock = self.socket()
        sock.send_frame(OP_CLOSE, code.to_bytes(2, "big") + reason.encode())
        sock.closed.wait(2)
        sock.drop()

    # ---- 测试扮演"手机上的你" ------------------------------------------------------

    def user_says(self, text: str = "", user: str | None = None, private: bool = True, attachments: list[tuple[str, str, bytes]] | None = None,
                  twice: bool = False, username: str | None = None, group: str = GROUP, **_: object) -> dict:
        """你（或者别人）在和机器人的单聊里（private=False：在群里 @ 机器人）发了一条消息。
        attachments 是（文件名，content_type，内容）：content_type 和 QQ 的一样，图片是 image/png 这样，别的是 file、voice、video/mp4。
        twice：同一条消息推两次（真的会这样）。返回事件的 d。"""
        user = user or OWNER
        with self._changed:
            self._next_id += 1
            message_id = f"ROBOT1.0_fake{self._next_id:06d}"
            self.last_message_id = message_id
            if private:
                self.messages[message_id] = {"user": user, "at": time.time(), "seqs": set()}
        author: dict = {"user_openid": user} if private else {"id": user, "member_openid": user}
        if username:
            author["username"] = username
        data: dict = {
            "id": message_id,
            "author": author,
            "content": text if private else f" {text}",  # 群里 @ 机器人的消息，去掉 @ 以后前面留着一个空格
            "timestamp": datetime.now(CHINA).isoformat(timespec="seconds"),
            "attachments": [],
        }
        if not private:
            data["group_openid"] = group
        for index, (filename, media_type, content) in enumerate(attachments or []):
            key = f"{message_id}-{index}/{filename}"
            self.files[key] = content
            data["attachments"].append({"content_type": media_type, "filename": filename, "size": len(content), "url": f"{self.base_url}/cdn/{key}"})
        kind = "C2C_MESSAGE_CREATE" if private else "GROUP_AT_MESSAGE_CREATE"
        self._dispatch(None, kind, data)
        if twice:
            self._dispatch(None, kind, json.loads(json.dumps(data)))
        return data

    def is_prompt(self, message: dict) -> bool:
        """一个确认请求的最后一条：以那句提示结尾，并且写着工作目录。
        只看提示不够：/help 的回答、"还有一个操作在等你批准"也带着"回复“允许”或“拒绝”"这几个字。"""
        return message["plain"].endswith(HINT) and "工作目录：" in message["plain"]

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
