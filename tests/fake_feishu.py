"""本地的假飞书：开放平台的 REST 接口，加一个假的长连接（FakeLarkListener）。只在测试里用，不连真正的飞书。

REST 照着飞书文档里的样子回答（都在 https://open.feishu.cn/document/ 下面，见 harness/channels/feishu.py 开头）：
换 tenant_access_token、问机器人自己的信息、发消息（text、post、interactive）、改卡片、下载消息里的图片和文件、
问通讯录里一个人叫什么，并记下机器人发出去的每一样东西。和真的一样较真：消息的 content 必须是 JSON 字符串、
富文本和卡片的结构不对就拒绝、超过 30 KB 回 230025；token 会"过期"（expire_tokens 以后用旧的回 99991663）；
通讯录可以没有权限（contacts = False）；能让下一条某种消息发失败（fail）。

长连接在真的程序里是飞书 SDK（lark-oapi）那一侧的事。测试里不装 SDK：FakeLarkListener 实现 feishu.Listener，
测试代码用 user_says、press 把事件（和飞书文档里的 JSON 一样的字典，也就是 SDK 交给我们的样子）交给通道，
就像 SDK 从长连接上收到了一样。真的 SDK 能不能把飞书推来的 JSON 变成这样的字典，test_channels_feishu.py 里
另有测试（装了 SDK 才跑）。
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

APP_ID = "cli_a5fake0test0app1"
APP_SECRET = "FAKEfeishuAPPsecret0123456789xyz"  # 编造的，长得像真的（32 位）
BOT_OPEN_ID = "ou_b0t0fake0000000000000000000001"
BOT_NAME = "智狐测试"
TENANT = "fake-tenant"
OWNER = "ou_5ad573a6411d72b8305fda3a9c15c70e"  # 主人的 open_id
OWNER_CHAT = "oc_d9e5b0b8e2c34f7a9b6c1d2e3f4a5b6c"  # 主人和机器人的单聊
OWNER_NAME = "Akxan"  # 通讯录里主人的名字
STRANGER = "ou_0000000000000000000000000stranger"

POST_TAGS = {"text", "a", "at", "img", "media", "emotion", "md", "hr", "code_block"}
CARD_LIMIT = 30 * 1024  # 富文本和卡片的 content 最多 30 KB（文本消息是 150 KB）
TEXT_LIMIT = 150 * 1024


def post_text(content: dict) -> str:
    """一条富文本（发出去的样子：外面包着一层语言）看得见的文字。"""
    lines = []
    for body in content.values():
        if body.get("title"):
            lines.append(body["title"])
        for row in body["content"]:
            lines.append("".join(str(node.get("text") or "") for node in row))
    return "\n".join(lines)


def card_elements(card: dict) -> list[dict]:
    """卡片里所有的组件（拆开分栏），按顺序。"""
    found: list[dict] = []

    def walk(elements: list) -> None:
        for element in elements:
            found.append(element)
            for column in element.get("columns") or []:
                walk(column.get("elements") or [])

    walk(card.get("body", {}).get("elements") or [])
    return found


def card_text(card: dict) -> str:
    lines = [card["header"]["title"]["content"]]
    for element in card_elements(card):
        if element.get("tag") == "markdown":
            lines.append(element["content"])
        elif element.get("tag") == "button":
            lines.append(element["text"]["content"])
    return "\n".join(lines)


class FakeFeishu:
    platform = "feishu"

    def __init__(self) -> None:
        self.app_secret = APP_SECRET  # 换掉它，就像在开放平台上重置了 App Secret
        self.credentials = {"app_id": APP_ID, "app_secret": APP_SECRET}
        self.bot = {"id": BOT_OPEN_ID, "username": BOT_NAME, "name": BOT_NAME, "at": False, "app": APP_ID,
                    "link": f"https://applink.feishu.cn/client/bot/open?appId={APP_ID}"}  # 和 feishu.check 返回的一样
        self.owner, self.owner_chat, self.owner_name = OWNER, OWNER_CHAT, OWNER_NAME
        self.has_bot = True  # False：这个应用还没加"机器人"能力
        self.contacts = True  # False：应用没有读通讯录的权限
        self.directory = {OWNER: OWNER_NAME}  # 通讯录
        self.tokens: list[str] = []  # 发出去的每一个 tenant_access_token
        self._valid: set[str] = set()
        self.token_requests = 0
        self.rejected_tokens: list[str] = []  # 用过期的 token 来的请求（路径）
        self.requests: list[str] = []  # 每个请求的方法和路径（不含问号后面的）
        self.sent: list[dict] = []  # 机器人发的消息：{"id", "chat", "msg_type", "content", "plain", "card"（卡片才有）}
        self.edits: list[dict] = []  # 改过的卡片：{"id", "card", "plain"}
        self.rejected: list[str] = []  # 被拒绝的消息（为什么），等不到消息时一起打印出来
        self.downloads: list[tuple[str, str, str, str]] = []  # (消息编号, 文件的 key, type, 用的 token)
        self.lookups: list[str] = []  # 问过通讯录的 open_id
        self.files: dict[tuple[str, str], tuple[str, bytes]] = {}  # (消息编号, key) → (image 或 file, 内容)
        self.fail_next: dict[str, dict] = {}  # 消息的类型（text、post、interactive）或 "PATCH" → 下一次回答的错误
        self.toasts: list[dict | None] = []  # 点按钮以后通道给飞书的回答
        self.listener: FakeLarkListener | None = None
        self._next = 0
        self._changed = threading.Condition()

    @property
    def secrets(self) -> list[str]:
        """这个平台的全部密钥：App Secret 和发出去的每一个 tenant_access_token。"""
        return [APP_SECRET, *self.tokens]

    # ---- 启动 ------------------------------------------------------------------

    def start(self, port: int = 0) -> FakeFeishu:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self, method: str) -> None:
                parts = urlsplit(self.path)
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                body = json.loads(raw) if raw else None
                query = {name: values[0] for name, values in parse_qs(parts.query).items()}
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

            def do_PATCH(self) -> None:
                self._handle("PATCH")

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self

    def config(self) -> dict:
        return {"feishu_api": self.base_url}

    def factory(self, config, store):
        """建一个连这个假飞书的通道：长连接换成 FakeLarkListener（不用装 SDK）。"""
        from harness.channels.feishu import FeishuChannel

        credentials = store.get("feishu")["credentials"]
        return FeishuChannel(credentials.get("app_id", ""), credentials.get("app_secret", ""), config.feishu_api, listener=FakeLarkListener(self))

    def stop(self) -> None:
        with self._changed:
            self._changed.notify_all()
        if self.listener is not None:
            self.listener.stop()
        self.server.shutdown()
        self.server.server_close()

    # ---- 测试用的开关 ------------------------------------------------------------

    def expire_tokens(self) -> None:
        """飞书那边让发出去的 token 都失效（通道里记着的还没到期）：之后用它们的请求回 99991663。"""
        with self._changed:
            self._valid.clear()

    def fail(self, kind: str, code: int = 230099, message: str = "Failed to create card content, ext=ErrCode: 200861") -> None:
        """下一条 kind 类型的消息（text、post、interactive）或者下一次改卡片（PATCH）回一个错误。"""
        with self._changed:
            self.fail_next[kind] = {"code": code, "msg": message}

    # ---- REST ------------------------------------------------------------------

    def _api(self, method: str, path: str, query: dict, body, headers) -> tuple[int, object]:
        with self._changed:
            self.requests.append(f"{method} {path}")
        if method == "POST" and path == "/open-apis/auth/v3/tenant_access_token/internal":
            return self._token(body or {})
        token = headers.get("Authorization", "").removeprefix("Bearer ")
        with self._changed:
            if token not in self._valid:
                self.rejected_tokens.append(path)
                code = 99991663 if token in self.tokens else 99991661
                return 400, {"code": code, "msg": "Invalid access token for authorization. Please make a request with token attached."}
        if method == "GET" and path == "/open-apis/bot/v3/info":
            if not self.has_bot:
                return 200, {"code": 0, "msg": "ok", "bot": {"activate_status": 0, "app_name": BOT_NAME, "open_id": ""}}
            return 200, {"code": 0, "msg": "ok", "bot": {"activate_status": 2, "app_name": BOT_NAME, "avatar_url": "https://example.invalid/avatar.png",
                                                         "ip_white_list": [], "open_id": BOT_OPEN_ID}}
        if method == "POST" and path == "/open-apis/im/v1/messages":
            return self._create(query, body or {})
        parts = path.strip("/").split("/")
        if parts[:4] == ["open-apis", "im", "v1", "messages"] and len(parts) == 5 and method == "PATCH":
            return self._patch(parts[4], body or {})
        if parts[:4] == ["open-apis", "im", "v1", "messages"] and len(parts) == 7 and parts[5] == "resources" and method == "GET":
            return self._resource(parts[4], parts[6], query.get("type", ""), token)
        if parts[:4] == ["open-apis", "contact", "v3", "users"] and len(parts) == 5 and method == "GET":
            return self._contact(parts[4], query)
        return 404, {"code": 404, "msg": "404 page not found"}

    def _token(self, body: dict) -> tuple[int, object]:
        with self._changed:
            self.token_requests += 1
            if body.get("app_id") != APP_ID or body.get("app_secret") != self.app_secret:
                return 400, {"code": 10014, "msg": "app secret invalid"}
            token = "t-" + secrets.token_hex(20)
            self.tokens.append(token)
            self._valid.add(token)
        return 200, {"code": 0, "msg": "ok", "tenant_access_token": token, "expire": 7200}

    def _reject(self, why: str, code: int = 230001) -> tuple[int, object]:
        with self._changed:
            self.rejected.append(why)
            self._changed.notify_all()
        return 400, {"code": code, "msg": why}

    def _create(self, query: dict, body: dict) -> tuple[int, object]:
        if query.get("receive_id_type") != "chat_id":
            return self._reject("receive_id_type 应该是 chat_id", 99992402)
        kind, raw, chat = body.get("msg_type"), body.get("content"), body.get("receive_id")
        if kind not in ("text", "post", "interactive") or not isinstance(raw, str) or not isinstance(chat, str):
            return self._reject(f"看不懂的消息：{body}")
        if len(raw.encode()) > (TEXT_LIMIT if kind == "text" else CARD_LIMIT):
            return self._reject("The length of the message content reaches its limit", 230025)
        try:
            content = json.loads(raw)
        except ValueError:
            return self._reject("content 不是 JSON")
        problem = self._check(kind, content)
        if problem:
            return self._reject(problem)
        with self._changed:
            failure = self.fail_next.pop(kind, None)
            if failure is not None:
                return 400, failure
            self._next += 1
            message = {"id": f"om_fake{self._next:06d}", "chat": chat, "msg_type": kind, "content": content}
            message["plain"] = content["text"] if kind == "text" else post_text(content) if kind == "post" else card_text(content)
            if kind == "interactive":
                message["card"] = content
            self.sent.append(message)
            self._changed.notify_all()
        return 200, {"code": 0, "msg": "success", "data": {"message_id": message["id"], "chat_id": chat, "msg_type": kind}}

    @staticmethod
    def _check(kind: str, content: object) -> str:
        """消息的内容合不合飞书的格式（见 message-content-description）。合格返回空字符串。"""
        if not isinstance(content, dict):
            return "content 应该是一个对象"
        if kind == "text":
            return "" if isinstance(content.get("text"), str) and content["text"].strip() else "文本消息没有文字"
        if kind == "post":
            for language, body in content.items():
                if language not in ("zh_cn", "en_us", "ja_jp") or not isinstance(body, dict) or not isinstance(body.get("content"), list):
                    return "富文本的结构不对"
                for row in body["content"]:
                    if not isinstance(row, list) or not all(isinstance(node, dict) and node.get("tag") in POST_TAGS for node in row):
                        return "富文本的段落不对"
                    for node in row:
                        if node["tag"] == "md" and (len(row) != 1 or not isinstance(node.get("text"), str)):
                            return "md 标签要自己占一个段落"
            return "" if content else "富文本是空的"
        if content.get("schema") != "2.0" or (content.get("config") or {}).get("update_multi") is not True:
            return "卡片要用 JSON 2.0、并且是共享卡片（update_multi），发出去以后才能改"
        header = content.get("header") or {}
        if not isinstance((header.get("title") or {}).get("content"), str) or not isinstance(header.get("template"), str):
            return "卡片的标题不对"
        for element in card_elements(content):
            if element.get("tag") not in ("markdown", "column_set", "column", "button", "hr", "div"):
                return f"卡片里不认识的组件：{element.get('tag')}"
            if element.get("tag") == "button":
                behaviors = element.get("behaviors") or []
                if not behaviors or behaviors[0].get("type") != "callback" or not isinstance(behaviors[0].get("value"), dict):
                    return "按钮要用回传交互（callback）"
        return ""

    def _patch(self, message_id: str, body: dict) -> tuple[int, object]:
        with self._changed:
            original = next((message for message in self.sent if message["id"] == message_id), None)
            failure = self.fail_next.pop("PATCH", None)
        if failure is not None:
            return 400, failure
        if original is None or original["msg_type"] != "interactive":
            return self._reject("只有卡片消息能改", 230001)
        try:
            card = json.loads(body.get("content") or "")
        except ValueError:
            return self._reject("content 不是 JSON")
        problem = self._check("interactive", card)
        if problem:
            return self._reject(problem)
        with self._changed:
            self.edits.append({"id": message_id, "chat": original["chat"], "card": card, "plain": card_text(card)})
            self._changed.notify_all()
        return 200, {"code": 0, "msg": "success", "data": {}}

    def _resource(self, message_id: str, key: str, kind: str, token: str) -> tuple[int, object]:
        with self._changed:
            found = self.files.get((message_id, key))
            self.downloads.append((message_id, key, kind, token))
        if found is None or found[0] != kind:
            return 400, {"code": 234003, "msg": "File not in msg."}
        return 200, found[1]

    def _contact(self, user: str, query: dict) -> tuple[int, object]:
        with self._changed:
            self.lookups.append(user)
        if query.get("user_id_type") != "open_id":
            return 400, {"code": 99992402, "msg": "field validation failed"}
        if not self.contacts:
            return 400, {"code": 99991672, "msg": "Access denied. One of the following scopes is required: [contact:user.base:readonly]."}
        if user not in self.directory:
            return 400, {"code": 41050, "msg": "no user authority error"}
        return 200, {"code": 0, "msg": "success", "data": {"user": {"open_id": user, "name": self.directory[user], "en_name": ""}}}

    # ---- 长连接：测试扮演"手机上的你" ------------------------------------------------

    def _attach(self, listener: FakeLarkListener) -> None:
        with self._changed:
            self.listener = listener
            self._changed.notify_all()

    def push(self, kind: str, payload: dict) -> dict | None:
        """长连接上来了一个事件：交给通道（就像 SDK 收到以后交过去一样），返回通道给飞书的回答。"""
        listener = self.wait_until(lambda: self.listener if self.listener is not None and self.listener.on_event is not None else None)
        return listener.on_event(kind, payload)

    def _header(self, kind: str) -> dict:
        with self._changed:
            self._next += 1
            number = self._next
        return {"event_id": f"ev_fake{number:06d}", "event_type": kind, "create_time": str(int(time.time() * 1000)), "token": "",
                "app_id": APP_ID, "tenant_key": TENANT}

    def _key(self, prefix: str) -> str:
        with self._changed:
            self._next += 1
            return f"{prefix}_v3_fake{self._next:06d}"

    def event(self, text: str = "", user: str | None = None, private: bool = True, chat: str | None = None, kind: str | None = None,
              image: bytes | None = None, file: tuple[str, bytes] | None = None, post: dict | None = None, message_id: str | None = None,
              sender_type: str = "user") -> dict:
        """一个 im.message.receive_v1 事件，样子和飞书文档里的一样（SDK 交给我们的就是这样的字典）。

        image 是图片的内容；file 是（文件名，内容）；post 是收到的富文本（{"title", "content": [[节点…]…]}），
        里面的 {"tag": "img", "image": 内容} 会换成真的样子（image_key）。"""
        user = user or OWNER
        message_id = message_id or self._key("om").replace("_v3", "")
        kind = kind or ("image" if image is not None else "file" if file is not None else "post" if post is not None else "text")
        content: dict
        if kind == "text":
            content = {"text": text}
        elif kind == "image":
            key = self._key("img")
            self.files[(message_id, key)] = ("image", image or b"")
            content = {"image_key": key}
        elif kind == "file":
            name, data = file or ("file.bin", b"")
            key = self._key("file")
            self.files[(message_id, key)] = ("file", data)
            content = {"file_key": key, "file_name": name}
        elif kind == "post":
            rows = []
            for row in (post or {}).get("content") or []:
                nodes = []
                for node in row:
                    if node.get("tag") == "img" and "image" in node:
                        key = self._key("img")
                        self.files[(message_id, key)] = ("image", node["image"])
                        node = {"tag": "img", "image_key": key, "width": 300, "height": 300}
                    nodes.append(node)
                rows.append(nodes)
            content = {"title": (post or {}).get("title", ""), "content": rows}
        elif kind == "audio":
            content = {"file_key": self._key("file"), "duration": 2000}
        elif kind == "media":
            content = {"file_key": self._key("file"), "image_key": self._key("img"), "file_name": "clip.mp4", "duration": 3000}
        elif kind == "sticker":
            content = {"file_key": self._key("file")}
        else:
            content = {}
        stamp = str(int(time.time() * 1000))
        message = {
            "message_id": message_id, "root_id": "", "parent_id": "", "create_time": stamp, "update_time": stamp,
            "chat_id": chat or (OWNER_CHAT if user == OWNER else "oc_" + user.removeprefix("ou_")),
            "chat_type": "p2p" if private else "group", "message_type": kind, "content": json.dumps(content, ensure_ascii=False),
            "user_agent": "Mozilla/5.0 Lark/7.30.0",
        }
        if not private:
            message["mentions"] = [{"key": "@_user_1", "id": {"open_id": BOT_OPEN_ID, "union_id": "on_bot"}, "name": BOT_NAME, "tenant_key": TENANT}]
        sender = {"sender_id": {"open_id": user, "union_id": "on_" + user.removeprefix("ou_")}, "sender_type": sender_type, "tenant_key": TENANT}
        return {"schema": "2.0", "header": self._header("im.message.receive_v1"), "event": {"sender": sender, "message": message}}

    def user_says(self, text: str = "", user: str | None = None, private: bool = True, **options) -> str:
        """你（或者别人）在单聊（或者群里）发了一条消息。返回消息的编号（再推一次同一条时用）。参数见 event。"""
        payload = self.event(text, user, private, **options)
        self.push("im.message.receive_v1", payload)
        return payload["event"]["message"]["message_id"]

    def is_prompt(self, message: dict) -> bool:
        """确认请求：带按钮的卡片；卡片发不出去时退回的那段文字（写着工作目录和"回复允许或拒绝"）。"""
        if message["msg_type"] == "interactive":
            return bool(self.buttons(message))
        return "工作目录：" in message["plain"] and "回复“允许”或“拒绝”" in message["plain"]

    def buttons(self, message: dict) -> list[dict]:
        return [element for element in card_elements(message.get("card") or {}) if element.get("tag") == "button"]

    def card_action(self, message: dict, answer: str, user: str | None = None) -> dict:
        """一个 card.action.trigger 事件：有人点了 message 这张卡片上的按钮（按钮回传的是 {"answer": answer}）。"""
        event = {
            "operator": {"tenant_key": TENANT, "open_id": user or OWNER, "union_id": "on_x"},
            "token": "c-" + secrets.token_hex(8),
            "action": {"tag": "button", "value": {"answer": answer}},
            "host": "im_message",
            "context": {"open_message_id": message["id"], "open_chat_id": message["chat"]},
        }
        return {"schema": "2.0", "header": self._header("card.action.trigger"), "event": event}

    def press(self, message: dict, allow: bool = True, user: str | None = None, answer: str | None = None) -> dict | None:
        """点了卡片上的"允许"或"拒绝"。返回通道给飞书的回答（{"toast": …}，飞书会在屏幕上弹一下）。"""
        if answer is None:
            values = [button["behaviors"][0]["value"]["answer"] for button in self.buttons(message)]
            answer = next(value for value in values if value.startswith("ok:" if allow else "no:"))
        result = self.push("card.action.trigger", self.card_action(message, answer, user))
        with self._changed:
            self.toasts.append(result)
        return result

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

    def edit_of(self, message: dict) -> dict:
        """确认请求答复以后，那张卡片被改成的样子。"""
        return self.wait_until(lambda: next((edit for edit in self.edits if edit["id"] == message["id"]), None))


class FakeLarkListener:
    """feishu.Listener 的假的那一个：不连飞书、不用 SDK。run 报一声"连上了"，一直等到 stop；
    这期间 FakeFeishu.push 把事件交给 run 收到的 on_event。"""

    def __init__(self, fake: FakeFeishu):
        self.fake = fake
        self.on_event = None
        self.runs = 0
        self._stopped = threading.Event()

    def run(self, on_event, on_state) -> str:
        self.runs += 1
        if self._stopped.is_set():
            return "stopped"
        self.on_event = on_event
        self.fake._attach(self)
        on_state("online", "")
        self._stopped.wait()
        self.on_event = None
        return "stopped"

    def stop(self) -> None:
        self._stopped.set()
