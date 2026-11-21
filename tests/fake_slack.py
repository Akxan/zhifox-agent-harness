"""本地的假 Slack（Web API 加 Socket Mode），只在测试和演示里用。不连真正的 Slack，不需要真的应用和 token。

Web API 照着真的回答（https://docs.slack.dev/reference/methods）：每个方法是 /api/<方法名>，GET 带查询参数或者 POST 发 JSON，
token 放在 Authorization: Bearer 里。出错时 HTTP 状态码照样是 200，回答 {"ok": false, "error": "…"}；被限流是 429 加 Retry-After。
它较真地检查每一次用的是哪一种 token：发消息、问名字、打开私聊要 Bot token（xoxb-），开 Socket Mode 的连接要 App token（xapp-），
拿错了回 not_allowed_token_type，和真的一样。读的方法（users.info、bots.info）只认查询参数和表单，不看 JSON 请求体：
Slack 的 JSON 请求体是给写的方法用的，客户端把参数放错了地方，这里就像真的一样"找不到这个人"。

发出去的消息也较真地检查（不合格就回 msg_too_long、invalid_blocks，和真的一样）：
  - 一条消息的 text 不超过 4000 个字符。真的 Slack 是"建议不超过 4000、超过 40000 才截断"，这里更严，测试才看得出没拆开
  - 一条消息最多 50 个块；section 的文字不超过 3000 个字符；按钮上的字不超过 75 个；同一条消息里 block_id、action_id 不重复
  - mrkdwn 里的 & < > 必须写成 &amp; &lt; &gt;（<网址|文字>、<@U…> 这类 Slack 自己的写法除外）。没转义的不拒收
    （真的 Slack 也不拒收，只是显示坏了），记进 problems，测试最后检查它是空的

机器人发文件也照着真的三步来（https://docs.slack.dev/messaging/working-with-files）：
  files.getUploadURLExternal（只认表单，和文档一样；length 要是正整数）→ 往它给的 /upload/… 地址 POST 文件的字节
  （收到的字节数要和 length 一样；带着 Authorization 来的记进 problems：这个地址不该收到 token）→ files.completeUploadExternal
  （只认传完了的文件编号，不然 file_not_found；initial_comment 和消息一样检查）。发出去的文件记在 uploads 里。
  files_write = False 时，这两个方法回 missing_scope（needed 是 files:write），和没加这个权限的应用一样。

Socket Mode 是同一个端口上的 WebSocket（apps.connections.open 给的一次性地址 /link/?ticket=…，用过一次就作废）：
连上先发 hello，之后把事件包成信封推过去。每个信封都要等客户端回 {"envelope_id"}（acks 里记着每一个回执、什么时候到的）。
events_api 的信封过了 resend_after 秒还没有回执（或者回执"丢了"，见 lose_acks），就像 Slack 一样再推一遍：
envelope_id 和 event_id 不变，retry_attempt 加一。所以客户端必须认得出同一个事件，只处理一次。

测试代码扮演 Slack 那头的你：user_says（在私聊或者频道里发一条消息，可以带文件）、press（点按钮）、command（用 /zhifox）。
也能让 Slack 做它真的会做的事：要你换一条连接（disconnect）、断网（drop）、不再回 pong（freeze）、
某个方法下一次出错（fail_next）、某个方法慢下来（hold / release）、收回 token（从 bot_tokens、app_tokens 里拿掉）。
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import fake_ws

# 编造的 token。拼起来写：源文件里不出现一个"像真 token"的整串，免得被代码托管平台的密钥扫描误报
BOT_TOKEN = "xoxb" + "-fake-bot-token-" + "for-tests-0123456789"
APP_TOKEN = "xapp" + "-1-fake-app-token-" + "for-tests-ABCDEFGHIJ"
TEAM = "T0ZHIFOX1"
APP_ID = "A0ZHIFOX1"
BOT_ID = "B0ZHIFOX1"  # 机器人自己（bots.info 的编号）
BOT_USER = "U0ZHIFOXBOT"  # 机器人在工作区里的用户编号（auth.test 的 user_id）
OWNER = "U0AKXAN01"  # 主人
OWNER_CHAT = "D0AKXAN01"  # 主人和机器人的私聊
OWNER_NAME = "Akxan"  # users.info 里主人的 display_name：通道交给 router 的名字
STRANGER = "U0STRANGER"
CHANNEL = "C0GENERAL1"  # 工作区里的一个公开频道

FILE_METHODS = {"files.getUploadURLExternal", "files.completeUploadExternal"}
BOT_METHODS = {"auth.test", "bots.info", "users.info", "conversations.open", "chat.postMessage", "chat.update", "chat.postEphemeral", *FILE_METHODS}
# files.getUploadURLExternal 不在这里：文档只写了它收表单
JSON_METHODS = {"auth.test", "conversations.open", "chat.postMessage", "chat.update", "chat.postEphemeral", "apps.connections.open", "files.completeUploadExternal"}
TEXT_LIMIT = 4000
SECTION_LIMIT = 3000
BLOCK_LIMIT = 50
BUTTON_LIMIT = 75
# mrkdwn 里合法的写法：转义好的三个字符，和 Slack 自己的 <…>（链接、@某人、#频道、!here）。剩下的 & < > 都是没转义的
_MRKDWN = re.compile(r"&(?:amp|lt|gt);|<(?:https?://|mailto:|@|#|!)[^<>]*>|[&<>]")


def escape(text: str) -> str:
    """Slack 发来的文字都是这样转义过的。"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def unescape(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def unescaped(text: str) -> list[str]:
    """mrkdwn 里没转义的 & < >（应该是空的）。"""
    return [match.group() for match in _MRKDWN.finditer(text) if len(match.group()) == 1]


def plain(text: str, blocks: list | None) -> str:
    """一条消息看得见的文字：有块就是各个 section、context 里的字，没有就是 text。转义还原，mrkdwn 的记号留着。"""
    if not blocks:
        return unescape(text or "")
    parts: list[str] = []
    for block in blocks:
        if block.get("type") == "section":
            parts.append(block["text"]["text"])
        elif block.get("type") == "context":
            parts.extend(element.get("text", "") for element in block.get("elements", []))
    return unescape("\n".join(parts))


class FakeSlack:
    platform = "slack"

    def __init__(self, resend_after: float = 1.0):
        self.credentials = {"bot_token": BOT_TOKEN, "app_token": APP_TOKEN}
        self.bot = {"id": BOT_USER, "username": "zhifox", "name": "ZhiFox", "app": APP_ID, "link": f"https://slack.com/app_redirect?app={APP_ID}&team={TEAM}"}
        self.owner, self.owner_chat, self.owner_name = OWNER, OWNER_CHAT, OWNER_NAME
        self.secrets = [BOT_TOKEN, APP_TOKEN]
        self.bot_tokens = {BOT_TOKEN}  # Slack 现在认的 Bot token（测试拿掉一个，就是"被收回了"）
        self.app_tokens = {APP_TOKEN}
        self.users = {
            OWNER: {"id": OWNER, "name": "akxan", "real_name": "Akxan Real", "profile": {"display_name": OWNER_NAME, "real_name": "Akxan Real"}},
            STRANGER: {"id": STRANGER, "name": "stranger", "real_name": "Someone Else", "profile": {"display_name": "", "real_name": "Someone Else"}},
            BOT_USER: {"id": BOT_USER, "name": "zhifox", "real_name": "ZhiFox", "is_bot": True, "profile": {"display_name": "ZhiFox"}},
        }
        self.resend_after = resend_after
        self.calls: list[dict] = []  # 每一次 Web API 调用：{"method", "verb", "token"（bot / app / other / none）, "last4"（token 的最后 4 位）, "args"}
        self.sent: list[dict] = []  # chat.postMessage：{"ts", "chat", "text", "blocks", "plain"}
        self.edits: list[dict] = []  # chat.update
        self.ephemeral: list[dict] = []  # chat.postEphemeral：{"chat", "user", "text"}
        self.downloads: list[dict] = []  # 文件下载：{"path", "token"}
        self.files: dict[str, bytes] = {}
        self.files_read = True  # 应用有没有 files:read 权限。没有的话，真的 Slack 下载文件时回的是一个 200 的登录页面
        self.files_write = True  # 应用有没有 files:write 权限（发文件要它）
        self.pending: dict[str, dict] = {}  # 要过上传地址、还没发出去的文件：编号 → {"filename", "length", "ticket", "data"}
        self.upload_requests: list[dict] = []  # 往上传地址发的每一个请求：{"path", "authorization", "size"}
        # 发到聊天里的文件：{"ts", "chat", "file_id", "title", "filename", "data", "initial_comment"}。ts 和消息的 ts 用同一个计数
        self.uploads: list[dict] = []
        self.problems: list[str] = []  # 客户端做错了、但真的 Slack 不会报错的事（没转义的 mrkdwn、看不懂的回执……）
        self.fail_next: dict[str, tuple[int, object, dict]] = {}  # 方法名 → 下一次的回答（状态码，JSON，头）
        self.holds: dict[str, threading.Event] = {}  # 方法名 → 等这个旗子立起来才回答（模拟 Slack 很慢）
        self.envelopes: dict[str, dict] = {}  # 信封编号 → {"envelope", "sends", "sent_at", "acked_at"}
        self.queue: list[str] = []  # 要推的信封
        self.acks: list[tuple[str, float]] = []  # 收到的每一个回执（信封编号，时刻）
        self.lose_acks = 0  # 接下来这么多个回执"在路上丢了"：当作没收到（events_api 的信封过一会儿会再推一遍）
        self.tickets: set[str] = set()  # apps.connections.open 给出去、还没用过的票据
        self.connections = 0
        self._sockets: list[fake_ws.ServerSocket] = []
        self._next_ts = 0
        self._changed = threading.Condition()

    # ---- 启动 ------------------------------------------------------------------

    def start(self, port: int = 0) -> FakeSlack:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _reply(self, status: int, payload: object, headers: dict | None = None) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json; charset=utf-8")
                self.send_header("content-length", str(len(data)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, verb: str) -> None:
                parts = urlsplit(self.path)
                if verb == "GET" and parts.path.rstrip("/") == "/link" and self.headers.get("Upgrade", "").lower() == "websocket":
                    fake._link(self, parse_qs(parts.query))
                    return
                if verb == "GET" and parts.path.startswith(("/files/", "/external/")):
                    fake._download(self, parts.path)
                    return
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                if verb == "POST" and parts.path.startswith("/upload/"):
                    status, answer = fake._receive(parts.path, raw, self.headers)
                    data = answer.encode()
                    self.send_response(status)
                    self.send_header("content-type", "text/plain; charset=utf-8")
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if not parts.path.startswith("/api/"):
                    self._reply(404, {"ok": False, "error": "not_found"})
                    return
                status, payload, headers = fake._api(verb, parts.path.removeprefix("/api/"), parts.query, raw, self.headers)
                self._reply(status, payload, headers)

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
        return {"slack_api": self.base_url + "/api"}

    def factory(self, config, store):
        from harness.channels import slack

        return slack.make(config, store)

    def stop(self) -> None:
        for gate in list(self.holds.values()):
            gate.set()
        with self._changed:
            self._changed.notify_all()
        for sock in list(self._sockets):
            sock.drop()
        self.server.shutdown()
        self.server.server_close()

    # ---- Web API ---------------------------------------------------------------

    def _api(self, verb: str, method: str, query: str, raw: bytes, headers) -> tuple[int, dict, dict]:
        args: dict = {name: values[-1] for name, values in parse_qs(query).items()}
        kind = headers.get("Content-Type", "")
        if raw and kind.startswith("application/x-www-form-urlencoded"):
            args.update({name: values[-1] for name, values in parse_qs(raw.decode()).items()})
        elif raw and kind.startswith("application/json") and method in JSON_METHODS:  # 别的方法：真的 Slack 不看 JSON 请求体
            try:
                body = json.loads(raw)
            except ValueError:
                return 200, {"ok": False, "error": "invalid_json"}, {}
            if not isinstance(body, dict):
                return 200, {"ok": False, "error": "invalid_json"}, {}
            args.update(body)
        auth = headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else ""
        with self._changed:
            self.calls.append({"method": method, "verb": verb, "token": self._kind(token), "last4": token[-4:], "args": args})
            self._changed.notify_all()
            failure = self.fail_next.pop(method, None)
            gate = self.holds.get(method)
        if gate is not None:
            gate.wait(10)
        if failure is not None:
            return failure
        problem = self._authorize(method, token)
        if problem:
            return 200, {"ok": False, "error": problem}, {}
        return self._method(method, args)

    def _kind(self, token: str) -> str:
        if not token:
            return "none"
        if token in self.bot_tokens or token == BOT_TOKEN:
            return "bot"
        if token in self.app_tokens or token == APP_TOKEN:
            return "app"
        return "other"

    def _authorize(self, method: str, token: str) -> str:
        if not token:
            return "not_authed"
        if method == "apps.connections.open":
            if token.startswith("xoxb-"):
                return "not_allowed_token_type"
            return "" if token in self.app_tokens else "invalid_auth"
        if method in BOT_METHODS:
            if token.startswith("xapp-"):
                return "not_allowed_token_type"
            return "" if token in self.bot_tokens else "invalid_auth"
        return "unknown_method"

    def _method(self, method: str, args: dict) -> tuple[int, dict, dict]:
        def ok(**fields) -> tuple[int, dict, dict]:
            return 200, {"ok": True, **fields}, {}

        def error(name: str, **fields) -> tuple[int, dict, dict]:
            return 200, {"ok": False, "error": name, **fields}, {}

        if method == "auth.test":
            return ok(url="https://zhifox-test.slack.com/", team="ZhiFox Test", user="zhifox", team_id=TEAM, user_id=BOT_USER, bot_id=BOT_ID, is_enterprise_install=False)
        if method == "bots.info":
            if args.get("bot") != BOT_ID:
                return error("bot_not_found")
            return ok(bot={"id": BOT_ID, "deleted": False, "name": "ZhiFox", "updated": 1_800_000_000, "app_id": APP_ID, "user_id": BOT_USER, "icons": {}})
        if method == "apps.connections.open":
            ticket = uuid.uuid4().hex
            with self._changed:
                self.tickets.add(ticket)
            return ok(url=f"{self.base_url.replace('http://', 'ws://')}/link/?ticket={ticket}&app_id={APP_ID}")
        if method == "users.info":
            user = self.users.get(str(args.get("user") or ""))
            return ok(user=user) if user is not None else error("user_not_found")
        if method == "conversations.open":
            user = str(args.get("users") or "").split(",")[0]
            if not user:
                return error("users_list_not_supplied")
            if user not in self.users:
                return error("user_not_found")
            return ok(no_op=True, already_open=True, channel={"id": self.dm_of(user)})
        if method in FILE_METHODS:
            return self._files(method, args)
        channel = str(args.get("channel") or "")
        if not channel.startswith(("D", "C")):
            return error("channel_not_found")
        if method == "chat.postMessage":
            problem = self._check(method, args)
            if problem:
                return error("invalid_blocks" if problem != "msg_too_long" else problem, response_metadata={"messages": [problem]})
            with self._changed:
                self._next_ts += 1
                ts = f"{1_800_000_000 + self._next_ts}.{self._next_ts:06d}"
                message = {"ts": ts, "chat": channel, "text": args.get("text") or "", "blocks": args.get("blocks"), "body": args}
                message["plain"] = plain(message["text"], message["blocks"])
                self.sent.append(message)
                self._changed.notify_all()
            return ok(channel=channel, ts=ts, message={"type": "message", "text": message["text"], "user": BOT_USER, "bot_id": BOT_ID, "ts": ts})
        if method == "chat.update":
            ts = str(args.get("ts") or "")
            if not any(message["ts"] == ts and message["chat"] == channel for message in self.sent):
                return error("message_not_found")
            problem = self._check(method, args)
            if problem:
                return error("invalid_blocks", response_metadata={"messages": [problem]})
            with self._changed:
                edit = {"ts": ts, "chat": channel, "text": args.get("text") or "", "blocks": args.get("blocks")}
                edit["plain"] = plain(edit["text"], edit["blocks"])
                self.edits.append(edit)
                self._changed.notify_all()
            return ok(channel=channel, ts=ts, text=edit["text"])
        if method == "chat.postEphemeral":
            self._mrkdwn(method, str(args.get("text") or ""))
            with self._changed:
                self.ephemeral.append({"chat": channel, "user": str(args.get("user") or ""), "text": str(args.get("text") or "")})
                self._changed.notify_all()
            return ok(message_ts=f"{1_800_000_000}.999999")
        return error("unknown_method")

    def _check(self, method: str, args: dict) -> str:
        """像真的 Slack 一样检查一条消息（见开头的说明）。返回不合格的原因，合格是空字符串。"""
        text, blocks = args.get("text"), args.get("blocks")
        if not isinstance(text, str) or (not text and not blocks):
            return "no_text"
        if len(text) > TEXT_LIMIT:
            return "msg_too_long"
        self._mrkdwn(method, text)
        if blocks is None:
            return ""
        if not isinstance(blocks, list) or not blocks or len(blocks) > BLOCK_LIMIT:
            return f"一条消息要有 1 到 {BLOCK_LIMIT} 个块"
        ids: set[str] = set()
        for block in blocks:
            kind = block.get("type") if isinstance(block, dict) else None
            block_id = block.get("block_id") if isinstance(block, dict) else None
            if block_id is not None:
                if block_id in ids or len(block_id) > 255:
                    return f"block_id 重复或者太长：{block_id}"
                ids.add(block_id)
            if kind == "section":
                item = block.get("text") or {}
                if item.get("type") not in ("mrkdwn", "plain_text") or not item.get("text") or len(item["text"]) > SECTION_LIMIT:
                    return f"section 的文字要有 1 到 {SECTION_LIMIT} 个字符（这一段 {len(item.get('text') or '')}）"
                if item["type"] == "mrkdwn":
                    self._mrkdwn(method, item["text"])
            elif kind == "context":
                elements = block.get("elements") or []
                if not 0 < len(elements) <= 10 or any(not element.get("text") for element in elements):
                    return "context 要有 1 到 10 段文字"
                for element in elements:
                    if element.get("type") == "mrkdwn":
                        self._mrkdwn(method, element["text"])
            elif kind == "actions":
                actions: set[str] = set()
                for element in block.get("elements") or []:
                    label = (element.get("text") or {}).get("text") or ""
                    if element.get("type") != "button" or not 0 < len(label) <= BUTTON_LIMIT or len(str(element.get("value") or "")) > 2000:
                        return "按钮不合格"
                    if element.get("style") not in (None, "primary", "danger"):
                        return "按钮的 style 不对"
                    action = str(element.get("action_id") or "")
                    if not action or action in actions:
                        return "action_id 重复或者是空的"
                    actions.add(action)
            else:
                return f"不认识的块：{kind}"
        return ""

    def _mrkdwn(self, method: str, text: str) -> None:
        bad = unescaped(text)
        if bad:
            with self._changed:
                self.problems.append(f"{method}：mrkdwn 里有没转义的 {''.join(sorted(set(bad)))}（{text[:80]!r}）")

    def _files(self, method: str, args: dict) -> tuple[int, dict, dict]:
        """files.getUploadURLExternal 和 files.completeUploadExternal（见文件开头）。"""
        if not self.files_write:
            return 200, {"ok": False, "error": "missing_scope", "needed": "files:write", "provided": "chat:write,im:history,files:read"}, {}
        if method == "files.getUploadURLExternal":
            length = str(args.get("length") or "")
            if not args.get("filename") or not length.isdigit() or int(length) == 0:
                return 200, {"ok": False, "error": "missing_argument" if not args.get("filename") or length in ("", "0") else "invalid_arguments"}, {}
            file_id, ticket = f"F{uuid.uuid4().hex[:10].upper()}", uuid.uuid4().hex
            with self._changed:
                self.pending[file_id] = {"filename": str(args["filename"]), "length": int(length), "ticket": ticket, "data": None}
            return 200, {"ok": True, "upload_url": f"{self.base_url}/upload/v1/{ticket}", "file_id": file_id}, {}
        files = args.get("files")
        if isinstance(files, str):  # 用表单发来的话，files 是一段 JSON 文字
            try:
                files = json.loads(files)
            except ValueError:
                files = None
        if not isinstance(files, list) or not files or not all(isinstance(item, dict) and item.get("id") for item in files):
            return 200, {"ok": False, "error": "invalid_arguments"}, {}
        channel = str(args.get("channel_id") or "")
        if channel and not channel.startswith(("D", "C")):
            return 200, {"ok": False, "error": "channel_not_found"}, {}
        comment = args.get("initial_comment")
        if comment is not None:
            if len(str(comment)) > TEXT_LIMIT:
                return 200, {"ok": False, "error": "msg_too_long"}, {}
            self._mrkdwn("files.completeUploadExternal", str(comment))
        with self._changed:
            entries = [self.pending.get(str(item["id"])) for item in files]
            if any(entry is None or entry["data"] is None for entry in entries):
                return 200, {"ok": False, "error": "file_not_found"}, {}
            done = []
            for item, entry in zip(files, entries, strict=True):
                assert entry is not None
                self.pending.pop(str(item["id"]))
                self._next_ts += 1
                ts = f"{1_800_000_000 + self._next_ts}.{self._next_ts:06d}"
                self.uploads.append({"ts": ts, "chat": channel, "file_id": str(item["id"]), "title": item.get("title"), "filename": entry["filename"],
                                     "data": entry["data"], "initial_comment": comment})
                done.append({"id": str(item["id"]), "title": item.get("title") or entry["filename"]})
            self._changed.notify_all()
        return 200, {"ok": True, "files": done}, {}

    def _receive(self, path: str, raw: bytes, headers) -> tuple[int, str]:
        """往上传地址发来的文件的字节。成功时和真的一样回 200 加 "OK - 字节数"。"""
        ticket = path.rstrip("/").rsplit("/", 1)[-1]
        authorization = headers.get("Authorization")
        with self._changed:
            self.upload_requests.append({"path": path, "authorization": authorization, "size": len(raw)})
            if authorization:
                self.problems.append("往上传地址发文件时带着 Authorization：这个地址不该收到 token")
            entry = next((item for item in self.pending.values() if item["ticket"] == ticket), None)
            if entry is None:
                return 404, "Not Found"
            if len(raw) != entry["length"]:
                return 400, "length does not match"
            entry["data"] = raw
            self._changed.notify_all()
        return 200, f"OK - {len(raw)}"

    def wait_upload(self, predicate: Callable[[dict], bool] = lambda upload: True, timeout: float = 10.0) -> dict:
        """等机器人发出一个满足 predicate 的文件，返回它。"""
        return self.wait_until(lambda: next((upload for upload in self.uploads if predicate(upload)), None), timeout, "机器人发的文件")

    def dm_of(self, user: str) -> str:
        """这个人和机器人的私聊的编号。"""
        return OWNER_CHAT if user == OWNER else "D" + user[1:]

    # ---- 文件 ------------------------------------------------------------------

    def _download(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        auth = handler.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else ""
        with self._changed:
            self.downloads.append({"path": path, "token": self._kind(token)})
            self._changed.notify_all()
        content = self.files.get(path) if token in self.bot_tokens else None
        if content is not None and not self.files_read:
            content = b"<!DOCTYPE html>\n<html><head><title>Slack</title></head><body>Sign in to your workspace</body></html>"
        if content is None:
            handler.send_response(404)
            handler.send_header("content-length", "0")
            handler.end_headers()
            return
        handler.send_response(200)
        handler.send_header("content-type", "application/octet-stream")
        handler.send_header("content-length", str(len(content)))
        handler.end_headers()
        handler.wfile.write(content)

    # ---- Socket Mode -----------------------------------------------------------

    def _link(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        ticket = (query.get("ticket") or [""])[0]
        with self._changed:
            valid = ticket in self.tickets
            self.tickets.discard(ticket)  # 一个地址只能连一次
        if not valid:
            with self._changed:
                self.problems.append("拿一个用过的（或者编造的）地址来连 Socket Mode")
            fake_ws.accept(handler, status=401)
            return
        ws = fake_ws.accept(handler)
        ws.frozen = False  # type: ignore[attr-defined]
        with self._changed:
            self.connections += 1
            self._sockets.append(ws)
            self._changed.notify_all()
        ws.send_json({"type": "hello", "num_connections": 1, "debug_info": {"host": "applink-fake", "build_number": 1, "approximate_connection_time": 18060},
                      "connection_info": {"app_id": APP_ID}})
        threading.Thread(target=self._read, args=(ws,), daemon=True).start()
        while not ws.closed.is_set():
            with self._changed:
                due = self._due(ws)
                if not due:
                    self._changed.wait(0.05)
                    continue
            for envelope_id in due:
                with self._changed:
                    envelope = self.envelopes[envelope_id]["envelope"]
                try:
                    ws.send_json(envelope)
                except OSError:
                    with self._changed:
                        self.envelopes[envelope_id]["sends"] -= 1  # 没发出去：下一条连接上再发
                        self.queue.append(envelope_id)
                    return

    def _due(self, ws: fake_ws.ServerSocket) -> list[str]:
        """这一刻要在这条连接上推的信封（拿着 _changed 的锁调用）。只有最新的那条连接推：Slack 一个信封只推给一条连接。"""
        if ws.frozen or ws is not self._latest():  # type: ignore[attr-defined]
            return []
        now = time.monotonic()
        for envelope_id, entry in self.envelopes.items():
            late = entry["acked_at"] is None and entry["sends"] == 1 and now - entry["sent_at"] > self.resend_after
            if late and entry["envelope"]["type"] == "events_api" and envelope_id not in self.queue:
                entry["envelope"] = {**entry["envelope"], "retry_attempt": 1, "retry_reason": "timeout"}
                self.queue.append(envelope_id)
        due, self.queue = self.queue, []
        for envelope_id in due:
            self.envelopes[envelope_id]["sends"] += 1
            self.envelopes[envelope_id]["sent_at"] = now
        return due

    def _latest(self) -> fake_ws.ServerSocket | None:
        return next((sock for sock in reversed(self._sockets) if not sock.closed.is_set()), None)

    def _read(self, ws: fake_ws.ServerSocket) -> None:
        """收客户端发来的东西：只应该有回执。ping 由 ServerSocket 自动回 pong（freeze 以后不读了，也就不回了）。"""
        while not ws.closed.is_set():
            if ws.frozen:  # type: ignore[attr-defined]
                time.sleep(0.02)
                continue
            try:
                message = ws.recv(timeout=0.2)
            except (ConnectionError, OSError):
                return
            if message is None:
                continue
            try:
                data = json.loads(message)
            except ValueError:
                data = None
            if not isinstance(data, dict) or not isinstance(data.get("envelope_id"), str) or set(data) - {"envelope_id", "payload"}:
                with self._changed:
                    self.problems.append(f"Socket Mode 上收到了看不懂的东西：{message!r:.80}")
                continue
            with self._changed:
                entry = self.envelopes.get(data["envelope_id"])
                if entry is None or entry["sends"] == 0:
                    self.problems.append(f"回执的信封没有推过：{data['envelope_id']}")
                elif self.lose_acks > 0:
                    self.lose_acks -= 1
                else:
                    self.acks.append((data["envelope_id"], time.monotonic()))
                    if entry["acked_at"] is None:
                        entry["acked_at"] = time.monotonic()
                self._changed.notify_all()

    def _push(self, kind: str, payload: dict, accepts_response: bool = False) -> dict:
        envelope: dict = {"envelope_id": str(uuid.uuid4()), "type": kind, "accepts_response_payload": accepts_response, "payload": payload}
        if kind == "events_api":
            envelope.update(retry_attempt=0, retry_reason="")
        with self._changed:
            self.envelopes[envelope["envelope_id"]] = {"envelope": envelope, "sends": 0, "sent_at": 0.0, "acked_at": None}
            self.queue.append(envelope["envelope_id"])
            self._changed.notify_all()
        return envelope

    def socket(self) -> fake_ws.ServerSocket:
        """现在开着的那条 Socket Mode 连接。"""
        sock = self._latest()
        assert sock is not None, "现在没有连着的 Socket Mode 连接"
        return sock

    def disconnect(self, reason: str = "refresh_requested") -> None:
        """Slack 要你换一条连接（refresh_requested、warning），或者 Socket Mode 被关掉了（link_disabled）。"""
        self.socket().send_json({"type": "disconnect", "reason": reason, "debug_info": {"host": "applink-fake"}})

    def drop(self) -> None:
        """网络断了：不打招呼就断开。"""
        self.socket().drop()

    def freeze(self) -> None:
        """这条连接"僵死"了：TCP 还连着，但 Slack 那边什么都不发、什么都不读（ping 也就没有 pong）。"""
        self.socket().frozen = True  # type: ignore[attr-defined]

    def hold(self, method: str) -> None:
        """之后对这个方法的调用都先停着，等 release 才回答（最多 10 秒）。"""
        self.holds[method] = threading.Event()

    def release(self, method: str) -> None:
        gate = self.holds.pop(method, None)
        if gate is not None:
            gate.set()

    # ---- 测试扮演"Slack 那头的你" -------------------------------------------------

    def _ts(self) -> str:
        with self._changed:
            self._next_ts += 1
            return f"{1_800_000_000 + self._next_ts}.{self._next_ts:06d}"

    def user_says(self, text: str = "", user: str | None = None, private: bool = True, chat: str | None = None, wire: bool = False,
                  files: list[tuple[str, str, bytes]] | None = None, external: bool = False, bot_id: str | None = None,
                  subtype: str | None = None, event_id: str | None = None, **_: object) -> dict:
        """你（或者别人）发了一条消息（message.im 事件；private=False 时是频道里的）。

        text 是你打的字，这里像 Slack 一样转义好再推过去；wire=True 时 text 已经是 Slack 推过来的样子（比如带着 <网址|文字>）。
        files 是（文件名，类型，内容）；external=True 时它们是 Google Drive 这类外部的文件（mode 是 external）。
        """
        user = user or OWNER
        ts = self._ts()
        event: dict = {
            "type": "message", "channel": chat or (self.dm_of(user) if private else CHANNEL), "channel_type": "im" if private else "channel",
            "user": user, "text": text if wire else escape(text), "ts": ts, "event_ts": ts, "team": TEAM,
        }
        if files:
            event["subtype"] = "file_share"
            event["files"] = []
            for index, (name, media_type, content) in enumerate(files):
                file_id = f"F{ts.replace('.', '')}{index}"
                path = f"/external/{file_id}/{name}" if external else f"/files/{TEAM}-{file_id}/{name}"
                self.files[path] = content
                event["files"].append({
                    "id": file_id, "name": name, "title": name, "mimetype": media_type, "filetype": name.rsplit(".", 1)[-1], "size": len(content),
                    "mode": "external" if external else "hosted", "is_external": external,
                    "url_private": self.base_url + path, "url_private_download": self.base_url + path + ("" if external else "?download=1"),
                })
        if bot_id:
            event["bot_id"] = bot_id
        if subtype == "message_changed":  # 改过的消息：真的 Slack 把新内容放在 message 里，外面没有 user
            event = {"type": "message", "subtype": "message_changed", "hidden": True, "channel": event["channel"], "channel_type": event["channel_type"],
                     "ts": ts, "event_ts": ts, "message": {**event, "edited": {"user": user, "ts": ts}}, "previous_message": {"type": "message", "text": "旧的"}}
        elif subtype:
            event["subtype"] = subtype
        payload = {"token": "verification-token", "team_id": TEAM, "api_app_id": APP_ID, "event": event, "type": "event_callback",
                   "event_id": event_id or f"Ev{uuid.uuid4().hex[:10].upper()}", "event_time": int(time.time())}
        return self._push("events_api", payload)

    def is_prompt(self, message: dict) -> bool:
        return any(block.get("type") == "actions" for block in message.get("blocks") or [])

    def buttons(self, message: dict) -> list[dict]:
        return [element for block in message.get("blocks") or [] if block.get("type") == "actions" for element in block["elements"]]

    def press(self, message: dict, allow: bool = True, user: str | None = None, chat: str | None = None) -> dict:
        """点了确认请求上的一个按钮（interactive 信封，block_actions）。"""
        allow_button, deny_button = self.buttons(message)
        button = allow_button if allow else deny_button
        block = next(block for block in message["blocks"] if block.get("type") == "actions")
        channel = chat or message["chat"]
        user = user or OWNER
        payload = {
            "type": "block_actions", "user": {"id": user, "username": self.users.get(user, {}).get("name", "someone"), "team_id": TEAM},
            "api_app_id": APP_ID, "token": "verification-token", "trigger_id": uuid.uuid4().hex,
            "container": {"type": "message", "message_ts": message["ts"], "channel_id": channel, "is_ephemeral": False},
            "team": {"id": TEAM, "domain": "zhifox-test"}, "channel": {"id": channel, "name": "directmessage"},
            "message": {"type": "message", "ts": message["ts"], "text": message["text"], "blocks": message["blocks"], "user": BOT_USER, "bot_id": BOT_ID},
            "response_url": "https://hooks.slack.com/actions/T0/1/fake",
            "actions": [{"action_id": button["action_id"], "block_id": block.get("block_id"), "text": button["text"], "value": button["value"],
                         "style": button.get("style"), "type": "button", "action_ts": self._ts()}],
        }
        return self._push("interactive", payload)

    def command(self, text: str = "", user: str | None = None, chat: str | None = None) -> dict:
        """用了 /zhifox（slash_commands 信封）。text 是命令后面的那些字。默认在你和机器人的私聊里用。"""
        user = user or OWNER
        channel = chat or self.dm_of(user)
        payload = {
            "token": "verification-token", "team_id": TEAM, "team_domain": "zhifox-test", "channel_id": channel,
            "channel_name": "directmessage" if channel.startswith("D") else "general", "user_id": user,
            "user_name": self.users.get(user, {}).get("name", "someone"), "command": "/zhifox", "text": text, "api_app_id": APP_ID,
            "is_enterprise_install": "false", "response_url": "https://hooks.slack.com/commands/T0/1/fake", "trigger_id": uuid.uuid4().hex,
        }
        return self._push("slash_commands", payload, accepts_response=True)

    # ---- 等 --------------------------------------------------------------------

    def wait_until(self, condition: Callable[[], object], timeout: float = 10.0, what: str = "条件"):
        deadline = time.monotonic() + timeout
        while True:
            with self._changed:
                value = condition()
                if value:
                    return value
                remaining = deadline - time.monotonic()
                assert remaining > 0, f"等不到{what}"
                self._changed.wait(min(remaining, 0.05))

    def wait_sent(self, predicate: Callable[[dict], bool] = lambda message: True, timeout: float = 10.0, after: int = 0) -> dict:
        try:
            return self.wait_until(lambda: next((message for message in self.sent[after:] if predicate(message)), None), timeout, "机器人发的消息")
        except AssertionError:
            raise AssertionError("等不到机器人发的消息。已经发的：\n" + "\n".join(message["plain"] for message in self.sent)) from None

    def edit_of(self, message: dict) -> dict:
        """确认请求答复以后，那条消息被改成的样子（chat.update）。"""
        return self.wait_until(lambda: next((edit for edit in self.edits if edit["ts"] == message["ts"]), None), what="改掉按钮")

    def acked(self, envelope: dict, timeout: float = 5.0) -> float:
        """等这个信封的回执，返回它到的时刻。"""
        return self.wait_until(lambda: self.envelopes[envelope["envelope_id"]]["acked_at"], timeout, "回执")

    def unacked(self) -> list[str]:
        """推过了、还没有回执的信封。"""
        with self._changed:
            return [envelope_id for envelope_id, entry in self.envelopes.items() if entry["sends"] and entry["acked_at"] is None]

    def methods(self, name: str | None = None) -> list[dict]:
        """Web API 调用的记录（只要某个方法的就给 name）。"""
        with self._changed:
            return [call for call in self.calls if name is None or call["method"] == name]
