"""本地的假 Telegram Bot API，只在测试和演示里用。不连真正的 Telegram，不需要真的机器人。

它像真的 Bot API 一样回答 getMe、getUpdates（长轮询，带 offset）、sendMessage、editMessageText、
answerCallbackQuery、sendChatAction、setMyCommands、getFile 和文件下载，并记下机器人发出去的每一条消息。
机器人发的图片和文件（sendPhoto、sendAnimation、sendDocument，multipart/form-data 上传）记在 uploads 里，
也像真的一样检查：照片最大 10 MB、别的 50 MB（超了回答 413），说明最多 1024 个字符，HTML 要合格。
测试代码扮演手机上的你：user_says 发一条消息、press 点一个按钮、wait_sent 等机器人回话。

网络出毛病也能演（trouble）：某个方法接下来的几次，一个字都不回就把连接关掉（像真实日志里的 "Connection reset by peer"）、
回答 500、429 带着 retry_after、403（你把机器人停用了）……用完了就照常回答。演示的"手机"页面上可以用 /control/trouble 打开。

它还会像 Telegram 一样检查机器人发来的 HTML：只认那几个标签、必须成对、去掉标签以后不能超过 4096 个字符
（按 UTF-16 数）。不合格就回答 400，和真的一样。这样测试能发现"排版排坏了"和"太长没拆开"。

演示（tests/demo_web.py）里，打开它的根地址 / 是一个很简陋的"手机"页面：能打字、能点按钮。
"""

from __future__ import annotations

import html
import json
import threading
import time
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from urllib.parse import urlsplit

from multipart_form import is_multipart, parse

TOKEN = "123456789:AAH-fake-token-for-tests-0123456789abcd"
ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "tg-spoiler", "span", "blockquote"}


def utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class _Checker(HTMLParser):
    """像 Telegram 一样检查 HTML：标签只能是那几种、必须成对；顺便算出去掉标签以后的文字。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.problem = ""
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag not in ALLOWED_TAGS:
            self.problem = self.problem or f"Unsupported start tag \"{tag}\""
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.problem = self.problem or f"Unmatched end tag \"{tag}\""
        else:
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def visible(text: str, parse_mode: str | None) -> tuple[str, str]:
    """（去掉标签以后的文字，问题）。问题是空字符串表示没问题。"""
    if parse_mode != "HTML":
        return text, ""
    checker = _Checker()
    checker.feed(text)
    checker.close()
    if checker.stack and not checker.problem:
        checker.problem = f"Unclosed tag \"{checker.stack[-1]}\""
    return "".join(checker.text), checker.problem


class FakeTelegram:
    def __init__(self, token: str = TOKEN, username: str = "zhifox_test_bot", max_poll: float = 1.0):
        self.token = token
        self.username = username
        self.max_poll = max_poll  # 长轮询最多真的等这么久（测试不想等 25 秒）
        self.updates: list[dict] = []
        self.sent: list[dict] = []  # 机器人发出去的消息（sendMessage），每条带着 message_id
        self.edits: list[dict] = []
        self.answers: list[dict] = []  # answerCallbackQuery
        self.actions: list[dict] = []  # sendChatAction
        self.commands: list | None = None
        self.files: dict[str, bytes] = {}
        # 机器人发的图片和文件：{"method", "chat_id", "field", "filename", "content_type", "data", "caption", "parse_mode", "message_id"}。
        # message_id 和 sent 里的消息共用一个计数，比一比就知道聊天里谁先谁后
        self.uploads: list[dict] = []
        self.calls: list[str] = []  # 收到的每个请求的方法名
        self.requests: list[str] = []  # 收到的每个请求的完整路径（检查 token 有没有被送到别处）
        self.offsets: list[int] = []  # getUpdates 带来的 offset
        self.fail_next: dict[str, dict] = {}  # 方法名 → 下一次回答的错误（用一次就没了）
        # 方法名 → 接下来几次各怎么出错（见 trouble），一次用掉一个
        self.troubles: dict[str, list[dict]] = {}
        self.poll_timeouts: list[float] = []  # getUpdates 带来的 timeout（长轮询要在服务器那边等多久）
        self._next_update = 1
        self._next_message = 100
        self._changed = threading.Condition()

    # ---- 启动 --------------------------------------------------------------

    def start(self, port: int = 0) -> FakeTelegram:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}"
                path = urlsplit(self.path).path
                if path.startswith("/control/"):
                    self._reply(200, fake._control(path.removeprefix("/control/"), json.loads(body)))
                    return
                fake.requests.append(path)
                prefix, _, method = path.rpartition("/")
                if prefix != f"/bot{fake.token}":
                    self._reply(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
                    return
                trouble = fake._take_trouble(method)
                if trouble is not None and trouble.get("reset"):
                    # 一个字都不回就关掉连接：客户端看到的是"对方关掉了连接"（和真实日志里的连接重置一样，都是 OSError）
                    self.close_connection = True
                    return
                if trouble is not None:
                    self._reply(int(trouble.get("error_code", 500)), {"ok": False, **trouble})
                    return
                if is_multipart(self.headers.get("content-type")):
                    try:
                        fields, files = parse(self.headers["content-type"], body)
                    except ValueError as exc:
                        self._reply(400, {"ok": False, "error_code": 400, "description": f"Bad Request: {exc}"})
                        return
                    status, payload = fake._upload(method, fields, files)
                else:
                    status, payload = fake._api(method, json.loads(body))
                self._reply(status, payload)

            def do_GET(self) -> None:
                path = urlsplit(self.path).path
                if path == "/":
                    page = PHONE.replace("__BOT__", html.escape(fake.username)).encode()
                    self.send_response(200)
                    self.send_header("content-type", "text/html; charset=utf-8")
                    self.send_header("content-length", str(len(page)))
                    self.end_headers()
                    self.wfile.write(page)
                    return
                if path == "/control/log":
                    self._reply(200, fake._log())
                    return
                fake.requests.append(path)
                prefix = f"/file/bot{fake.token}/"
                if not path.startswith(prefix) or path.removeprefix(prefix) not in fake.files:
                    self.send_response(404)
                    self.end_headers()
                    return
                content = fake.files[path.removeprefix(prefix)]
                self.send_response(200)
                self.send_header("content-length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def _reply(self, status: int, payload: dict) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except OSError:
                    pass

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self

    def stop(self) -> None:
        with self._changed:
            self._changed.notify_all()
        self.server.shutdown()
        self.server.server_close()

    # ---- 网络出毛病 ----------------------------------------------------------

    def trouble(self, method: str, *failures: dict | str) -> None:
        """method 接下来的几次各怎么出错（按顺序，一次用掉一个；用完了照常回答）。每一个是：
          "reset"                      一个字都不回就关掉连接（连接被重置）
          500、502……                    Telegram 出了故障
          {"error_code": 429, "parameters": {"retry_after": 1}, "description": "Too Many Requests"}  被限流
          {"error_code": 403, "description": "Forbidden: bot was blocked by the user"}            你把机器人停用了
        数字会写成 {"error_code": 数字, "description": "Internal Server Error"}。"""
        shaped: list[dict] = []
        for failure in failures:
            if failure == "reset":
                shaped.append({"reset": True})
            elif isinstance(failure, int):
                shaped.append({"error_code": failure, "description": "Internal Server Error" if failure >= 500 else f"Error {failure}"})
            elif isinstance(failure, dict):
                shaped.append(dict(failure))
        with self._changed:
            self.troubles.setdefault(method, []).extend(shaped)

    def _take_trouble(self, method: str) -> dict | None:
        with self._changed:
            waiting = self.troubles.get(method)
            if not waiting:
                return None
            self.calls.append(method)  # 出错的那几次也算它被调用过：测试数得出一共试了几次
            self._changed.notify_all()
            return waiting.pop(0)

    # ---- Bot API ----------------------------------------------------------

    def _api(self, method: str, params: dict) -> tuple[int, dict]:
        with self._changed:
            self.calls.append(method)
            failure = self.fail_next.pop(method, None)
        if failure is not None:
            return failure.get("error_code", 400), {"ok": False, **failure}
        if method == "getMe":
            return 200, {"ok": True, "result": {"id": int(self.token.split(":")[0]), "is_bot": True, "first_name": "ZhiFox Test", "username": self.username}}
        if method == "getUpdates":
            with self._changed:
                self.poll_timeouts.append(float(params.get("timeout") or 0))
            return 200, {"ok": True, "result": self._updates(int(params.get("offset") or 0), float(params.get("timeout") or 0))}
        if method in ("sendMessage", "editMessageText"):
            text, problem = visible(params.get("text", ""), params.get("parse_mode"))
            if problem:
                return 400, {"ok": False, "error_code": 400, "description": f"Bad Request: can't parse entities: {problem}"}
            if utf16(text) > 4096:
                return 400, {"ok": False, "error_code": 400, "description": "Bad Request: message is too long"}
            if not text.strip():
                return 400, {"ok": False, "error_code": 400, "description": "Bad Request: message text is empty"}
            with self._changed:
                if method == "editMessageText":
                    self.edits.append(dict(params))
                    self._changed.notify_all()
                    return 200, {"ok": True, "result": {"message_id": params.get("message_id")}}
                self._next_message += 1
                message = {**params, "message_id": self._next_message, "plain": text}
                self.sent.append(message)
                self._changed.notify_all()
            return 200, {"ok": True, "result": {"message_id": message["message_id"], "chat": {"id": params.get("chat_id")}, "text": text}}
        if method == "answerCallbackQuery":
            with self._changed:
                self.answers.append(dict(params))
                self._changed.notify_all()
            return 200, {"ok": True, "result": True}
        if method == "sendChatAction":
            with self._changed:
                self.actions.append(dict(params))
            return 200, {"ok": True, "result": True}
        if method == "setMyCommands":
            self.commands = params.get("commands")
            return 200, {"ok": True, "result": True}
        if method == "getFile":
            file_id = params.get("file_id")
            if file_id not in self.files:
                return 400, {"ok": False, "error_code": 400, "description": "Bad Request: invalid file_id"}
            return 200, {"ok": True, "result": {"file_id": file_id, "file_size": len(self.files[file_id]), "file_path": file_id}}
        return 404, {"ok": False, "error_code": 404, "description": "Not Found"}

    UPLOAD_FIELDS: ClassVar[dict] = {"sendPhoto": ("photo", 10_000_000), "sendAnimation": ("animation", 50_000_000), "sendDocument": ("document", 50_000_000)}

    def _upload(self, method: str, fields: dict[str, str], files: dict[str, dict]) -> tuple[int, dict]:
        """sendPhoto、sendAnimation、sendDocument（文件用 multipart/form-data 传上来）。"""
        with self._changed:
            self.calls.append(method)
            failure = self.fail_next.pop(method, None)
        if failure is not None:
            return failure.get("error_code", 400), {"ok": False, **failure}
        if method not in self.UPLOAD_FIELDS:
            return 404, {"ok": False, "error_code": 404, "description": "Not Found"}
        field, limit = self.UPLOAD_FIELDS[method]
        file = files.get(field)
        if file is None or not fields.get("chat_id"):
            return 400, {"ok": False, "error_code": 400, "description": f"Bad Request: there is no {field} in the request"}
        if len(file["data"]) > limit:
            return 413, {"ok": False, "error_code": 413, "description": "Request Entity Too Large"}
        caption, problem = visible(fields.get("caption", ""), fields.get("parse_mode"))
        if problem:
            return 400, {"ok": False, "error_code": 400, "description": f"Bad Request: can't parse entities: {problem}"}
        if utf16(caption) > 1024:
            return 400, {"ok": False, "error_code": 400, "description": "Bad Request: message caption is too long"}
        with self._changed:
            self._next_message += 1
            upload = {"method": method, "chat_id": fields["chat_id"], "field": field, **file, "caption": fields.get("caption", ""),
                      "parse_mode": fields.get("parse_mode"), "plain_caption": caption, "message_id": self._next_message}
            self.uploads.append(upload)
            self._changed.notify_all()
        return 200, {"ok": True, "result": {"message_id": upload["message_id"], "chat": {"id": fields["chat_id"]}, field: {"file_id": f"sent-{upload['message_id']}"}}}

    def wait_upload(self, predicate=lambda upload: True, timeout: float = 30.0) -> dict:
        """等机器人发出一个满足 predicate 的文件，返回它。"""
        return self.wait_until(lambda: next((upload for upload in self.uploads if predicate(upload)), None), timeout)

    def _updates(self, offset: float, timeout: float) -> list[dict]:
        deadline = time.monotonic() + min(timeout, self.max_poll)
        with self._changed:
            self.offsets.append(int(offset))
            # offset 之前的都确认过了：删掉（真的 Telegram 也是这样）
            self.updates = [update for update in self.updates if update["update_id"] >= offset]
            while not self.updates:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._changed.wait(remaining)
            return list(self.updates)

    # ---- 测试扮演"手机上的你" -------------------------------------------------

    def _push(self, update: dict) -> dict:
        with self._changed:
            update["update_id"] = self._next_update
            self._next_update += 1
            self.updates.append(update)
            self._changed.notify_all()
        return update

    def user_says(
        self,
        text: str = "",
        user: int = 111,
        first_name: str = "Akxan",
        username: str = "akxan",
        chat: int | None = None,
        private: bool = True,
        photo: bytes | None = None,
        photo_sizes: list[tuple[int, bytes]] | None = None,
        document: tuple[str, str, bytes] | None = None,
        voice: tuple[bytes, int] | None = None,
        audio: tuple[str, str, bytes, int] | None = None,
        extra: dict | None = None,
    ) -> dict:
        """你（或者别人）给机器人发了一条消息。document 是（文件名，类型，内容）。
        voice 是一段按住说话录的语音（内容，秒数），audio 是一个音频文件（文件名，类型，内容，秒数）：
        和真的 Telegram 一样带着时长、类型和大小（https://core.telegram.org/bots/api#voice 、#audio）。

        photo_sizes：一张照片的几种尺寸，[(长边的像素, 内容), ...]，从小到大（真的 Telegram 就是这样给的）。
        只给 photo 的话，是 90 和 800 两种尺寸、内容相同的一张。
        """
        chat_id = chat if chat is not None else user
        message: dict = {
            "message_id": self._next_update + 1000,
            "from": {"id": user, "is_bot": False, "first_name": first_name, "username": username},
            "chat": {"id": chat_id, "type": "private" if private else "group"},
            "date": int(time.time()),
        }
        if photo is not None and photo_sizes is None:
            photo_sizes = [(90, photo), (800, photo)]
        if photo_sizes is not None:
            message["photo"] = []
            for edge, content in photo_sizes:
                file_id = f"photo-{edge}-{self._next_update}"
                self.files[file_id] = content
                # 横着拍的照片：长边是宽
                size = {"file_id": file_id, "file_unique_id": file_id, "width": edge, "height": edge * 3 // 4, "file_size": len(content)}
                message["photo"].append(size)
            if text:
                message["caption"] = text
        elif document is not None:
            name, media_type, content = document
            file_id = f"doc-{self._next_update}"
            self.files[file_id] = content
            message["document"] = {"file_id": file_id, "file_unique_id": file_id, "file_name": name, "mime_type": media_type, "file_size": len(content)}
            if text:
                message["caption"] = text
        elif voice is not None:
            content, seconds = voice
            file_id = f"voice-{self._next_update}"
            self.files[file_id] = content
            message["voice"] = {"file_id": file_id, "file_unique_id": file_id, "duration": seconds, "mime_type": "audio/ogg", "file_size": len(content)}
            if text:
                message["caption"] = text
        elif audio is not None:
            name, media_type, content, seconds = audio
            file_id = f"audio-{self._next_update}"
            self.files[file_id] = content
            message["audio"] = {"file_id": file_id, "file_unique_id": file_id, "duration": seconds, "file_name": name, "mime_type": media_type, "file_size": len(content)}
            if text:
                message["caption"] = text
        else:
            message["text"] = text
        message.update(extra or {})
        return self._push({"message": message})

    def press(self, sent: dict, data: str, user: int = 111) -> dict:
        """点了机器人发来的那条消息下面的一个按钮（data 是按钮上带的数据，比如 ok:12）。"""
        query = {
            "id": f"query-{self._next_update}",
            "from": {"id": user, "is_bot": False, "first_name": "Akxan", "username": "akxan"},
            "message": {"message_id": sent["message_id"], "chat": {"id": int(sent["chat_id"]), "type": "private"}},
            "data": data,
        }
        return self._push({"callback_query": query})

    def wait_sent(self, predicate=lambda message: True, timeout: float = 30.0, after: int = 0) -> dict:
        """等机器人发出一条满足 predicate 的消息（只看第 after 条以后的），返回它。"""
        deadline = time.monotonic() + timeout
        with self._changed:
            while True:
                for message in self.sent[after:]:
                    if predicate(message):
                        return message
                remaining = deadline - time.monotonic()
                assert remaining > 0, "等不到机器人发的消息。已经发的：\n" + "\n".join(message["plain"] for message in self.sent)
                self._changed.wait(remaining)

    def wait_until(self, condition, timeout: float = 30.0):
        deadline = time.monotonic() + timeout
        while True:
            with self._changed:
                value = condition()
                if value:
                    return value
                remaining = deadline - time.monotonic()
                assert remaining > 0, "等不到条件成立"
                self._changed.wait(min(remaining, 0.05))

    def buttons(self, message: dict) -> list[dict]:
        markup = message.get("reply_markup") or {}
        return [button for row in markup.get("inline_keyboard", []) for button in row]

    # ---- 演示里的"手机"页面用的 ----------------------------------------------

    def _control(self, action: str, body: dict) -> dict:
        if action == "say":
            self.user_says(str(body.get("text", "")))
        elif action == "trouble":  # 演示里：接下来几次发消息出错（{"method": "sendMessage", "failures": ["reset", 500, …]}）
            self.trouble(str(body.get("method") or "sendMessage"), *(body.get("failures") or []))
        elif action == "calm":  # 没用完的 trouble 都不要了（页面测试收尾时用，免得留给下一个测试）
            with self._changed:
                self.troubles.clear()
        elif action == "press":
            message = next((item for item in self.sent if item["message_id"] == body.get("message_id")), None)
            if message is not None:
                self.press(message, str(body.get("data", "")))
        return {"ok": True}

    def _log(self) -> dict:
        with self._changed:
            edited = {edit["message_id"]: edit for edit in self.edits}
            shown = []
            for message in self.sent:
                edit = edited.get(message["message_id"])
                text = visible(edit["text"], edit.get("parse_mode"))[0] if edit else message["plain"]
                shown.append({"id": message["message_id"], "text": text, "buttons": [] if edit else self.buttons(message)})
            mine = [update["message"].get("text", "") for update in self.updates if "message" in update]
        return {"messages": shown, "typing": bool(self.actions and time.time() - self.actions[-1].get("_at", 0) < 5), "pending": mine}


PHONE = """<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>假的 Telegram · @__BOT__</title>
<style>
body { margin: 0; font: 15px/1.45 -apple-system, system-ui, sans-serif; background: #e7ebf0; display: flex; justify-content: center; }
main { width: min(440px, 100vw); height: 100vh; display: flex; flex-direction: column; background: #f4f6f8; }
header { padding: 12px 16px; background: #fff; border-bottom: 1px solid #dde2e8; font-weight: 600; }
header small { display: block; font-weight: 400; color: #7a8591; }
#log { flex: 1; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 8px; }
.bubble { max-width: 85%; padding: 8px 11px; border-radius: 12px; background: #fff; white-space: pre-wrap; overflow-wrap: anywhere; box-shadow: 0 1px 1px rgba(0,0,0,.06); }
.me { align-self: flex-end; background: #dcf4c4; }
.keys { display: flex; gap: 6px; margin-top: 8px; }
.keys button { flex: 1; padding: 7px; border: 0; border-radius: 8px; background: #e8f1fb; color: #2a6fb8; font: inherit; cursor: pointer; }
form { display: flex; gap: 8px; padding: 10px; background: #fff; border-top: 1px solid #dde2e8; }
input { flex: 1; padding: 9px 12px; border: 1px solid #cfd6de; border-radius: 18px; font: inherit; }
form button { padding: 0 16px; border: 0; border-radius: 18px; background: #2a8ae6; color: #fff; font: inherit; }
</style>
<main>
<header>@__BOT__<small>假的 Telegram：只连着本机的演示，消息不会发到任何地方</small></header>
<div id="log"></div>
<form id="form"><input id="text" placeholder="发一条消息（比如配对码）" autocomplete="off"><button>发送</button></form>
</main>
<script>
const log = document.getElementById('log'), text = document.getElementById('text');
const mine = [];
let drawn = '';
async function refresh() {
  const data = await (await fetch('/control/log')).json();
  const print = JSON.stringify([data.messages, mine]);
  if (print === drawn) return;
  drawn = print;
  const bubbles = [];
  for (const item of mine) bubbles.push({ at: item.at, node: Object.assign(document.createElement('div'), { className: 'bubble me', textContent: item.text }) });
  for (const message of data.messages) {
    const node = Object.assign(document.createElement('div'), { className: 'bubble', textContent: message.text });
    if (message.buttons.length) {
      const keys = node.appendChild(Object.assign(document.createElement('div'), { className: 'keys' }));
      for (const button of message.buttons) {
        const key = keys.appendChild(Object.assign(document.createElement('button'), { textContent: button.text }));
        key.onclick = () => fetch('/control/press', { method: 'POST', body: JSON.stringify({ message_id: message.id, data: button.callback_data }) });
      }
    }
    bubbles.push({ at: message.id, node });
  }
  log.replaceChildren(...bubbles.sort((a, b) => a.at - b.at).map((item) => item.node));
  log.scrollTop = log.scrollHeight;
}
let order = 0;
document.getElementById('form').onsubmit = async (event) => {
  event.preventDefault();
  if (!text.value.trim()) return;
  const data = await (await fetch('/control/log')).json();
  mine.push({ text: text.value, at: (data.messages.length ? data.messages[data.messages.length - 1].id : 0) + 0.5 + (order++) / 1000 });
  await fetch('/control/say', { method: 'POST', body: JSON.stringify({ text: text.value }) });
  text.value = '';
  refresh();
};
setInterval(refresh, 500);
refresh();
</script>
"""
