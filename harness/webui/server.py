"""本地 HTTP 服务器：浏览器和 agent 之间的桥。

浏览器只会发这几种请求：

  GET  /               页面本身（index.html）
  GET  /events         事件流。这个连接一直不关，服务器把 WebUI 日志里的新事件不断推过去。
                       刚连上时先补发已有的事件，然后发一个 synced 标记，后面才是实时的事件
  POST /api/message    用户发了一条消息
  POST /api/confirm    用户对一个确认请求点了"允许"或"拒绝"
  POST /api/stop       用户点了"停止"
  POST /api/new        用户点了"新对话"
  POST /api/conversation/open、rename、delete   打开、重命名、删除一段保存过的会话
  POST /api/conversation/reveal                 在系统的文件管理器里显示一段会话的文件
  POST /api/search     在所有保存过的会话里搜索（标题和内容）
  POST /api/model      用户换了模型
  POST /api/key        用户填写或删除了一家的 API key
  POST /api/models     用户点了"同步模型"
  POST /api/price      用户填写了一个模型的价格

安全。这个服务器能让助手在你的电脑上执行命令，所以必须保证只有你自己打开的那个页面能用它。
你平时访问的任何网站，都可以让浏览器向 127.0.0.1 发请求，所以光靠"只在本机"是不够的：

  1. 只监听 127.0.0.1，局域网里的其他电脑连不上。
  2. 每次启动生成一个随机口令（token），写在打开页面用的地址里。所有请求都必须带上它；
     别的网站不知道口令，请求会被拒绝。
  3. 检查 Host 和 Origin 请求头，拒绝从别的网站发起的请求。
  4. 页面带有内容安全策略（CSP）：只运行我们自己的脚本，不加载任何外部资源。
"""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..app import App, build_app, run_turn
from ..config import APP_NAME, Config
from ..conversations import ConversationError
from ..pricing import estimate, price_of
from ..providers import CLAUDE_MODELS, PROVIDERS, ProviderError, has_key
from ..search import MAX_QUERY_CHARS
from ..settings import key_hint, valid_api_key
from .ui import WebUI, latest_state_only

DEFAULT_PORT = 8765
MAX_BODY_BYTES = 1_000_000
MAX_MESSAGE_CHARS = 100_000
KEEPALIVE_SECONDS = 15
LARGE_CONTEXT_TOKENS = 30_000  # 打开的旧会话超过这么大时，提醒一下接着聊的费用

PAGE = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")


def reveal_in_file_manager(path: Path) -> None:
    """在系统的文件管理器里显示一个文件或文件夹（macOS 上是访达）。失败时抛出 OSError。

    这是整个服务器里唯一一处"页面点一下，电脑上就启动一个程序"的地方，所以守两条规矩：
    路径只能是程序自己算出来的（见 WebApp.reveal_conversation，浏览器只能给会话编号，给不了路径）；
    命令用参数列表交给系统，不经过 shell，文件名里有什么字符都不会被当成命令的一部分。
    """
    if sys.platform == "darwin":
        command = ["open", "-R", str(path)] if path.is_file() else ["open", str(path)]  # -R：在访达里选中这个文件，而不是打开它
    else:
        # Linux 上没有通用的"选中某个文件"的办法，退一步，打开它所在的文件夹
        command = ["xdg-open", str(path if path.is_dir() else path.parent)]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if process.wait(timeout=5) != 0:
            raise OSError(f"{command[0]} 没有成功（退出码 {process.returncode}）")
    except subprocess.TimeoutExpired:
        pass  # 有的文件管理器会一直开着不退出，那也算打开了，不用等它


class WebApp:
    """把助手接到网页界面上，并保证同一时间只进行一轮对话。"""

    def __init__(self, app: App, ui: WebUI, token: str):
        self.app = app
        self.ui = ui
        self.token = token
        self._turn = threading.Lock()  # 谁拿着这把锁，谁就在进行一轮对话（或者正在改设置）
        ui.on_stop(app.shell.kill_running)  # 点"停止"时，连正在运行的命令一起结束
        app.agent.on_usage = self.publish_state  # 每调用完一次模型，就刷新一次用量和费用
        self.publish_state()

    def publish_state(self) -> None:
        """把页面要显示的状态（当前模型、各家的情况、待办、用量、费用）作为一个事件发给浏览器。

        注意这里永远不包含 API key 本身，只有"有没有设置"和最后 4 位。
        """
        agent, settings, config = self.app.agent, self.app.settings, self.app.config
        usage = agent.usage
        cost, unpriced = estimate(agent.usage_by_model, settings.prices)
        self.ui.publish(
            "state",
            provider=settings.provider,
            model=settings.model,
            workspace=str(config.workspace),
            conversation=self.app.conversation.id,
            title=self.app.conversation.title,
            conversations=self.app.conversation_summaries(),
            providers=[
                {
                    "id": provider.id,
                    "label": provider.label,
                    "has_key": has_key(provider),
                    "key_hint": key_hint(provider.key_env),
                    "key_page": provider.key_page,
                    "models": self._models(provider.id),
                }
                for provider in PROVIDERS.values()
            ],
            price=price_of(settings.model, settings.prices),
            todos=[
                {"id": item["id"], "title": item["title"], "due": item["due"]}
                for item in self.app.todos.items("pending")
            ],
            usage={
                "requests": usage.requests,
                "input": usage.input_tokens,
                "cache_read": usage.cache_read_tokens,
                "cache_write": usage.cache_write_tokens,
                "output": usage.output_tokens,
                "searches": usage.searches,
                "context": usage.context_tokens,
            },
            cost=round(cost, 4),
            unpriced=unpriced,
            tools=[{"name": name, "local": local} for name, local in agent.backend.tool_names()],
        )

    def _models(self, provider_id: str) -> list[str]:
        """一家可以选择的模型：同步到的列表；Claude 在同步之前先用内置的几个。"""
        models = list(self.app.settings.models.get(provider_id) or (CLAUDE_MODELS if provider_id == "anthropic" else []))
        if provider_id == self.app.settings.provider and self.app.settings.model not in models:
            models.insert(0, self.app.settings.model)
        return models

    # ---- 对话 --------------------------------------------------------------

    def send(self, text: str) -> str:
        """开始一轮对话。成功返回空字符串，否则返回不能开始的原因。"""
        provider = self.app.provider
        if not has_key(provider):
            return f"还没有设置 {provider.label} 的 API key，请先点右上角的“设置”填写。"
        if not self._turn.acquire(blocking=False):
            return "上一轮对话还没有结束"
        # agent 循环是阻塞的（等模型、等用户确认），所以放到后台线程里跑，
        # 这样服务器还能继续响应浏览器的其他请求，比如"允许"和"停止"。
        threading.Thread(target=self._run_turn, args=(text,), daemon=True).start()
        return ""

    def _run_turn(self, text: str) -> None:
        try:
            self.app.name_conversation(text)  # 新会话用第一句话当标题
            self.ui.begin_turn(text)
            self.publish_state()  # 让新会话马上出现在列表里
            run_turn(self.app.agent, self.ui, text)
        finally:
            try:
                self.ui.end_stream()
                self._save()  # 每一轮结束都存一次盘，所以最多只会丢失正在进行的这一轮
                self.publish_state()
            finally:
                # "这一轮结束了"要等存盘和刷新状态都做完、马上就要放开锁的时候才通知页面。
                # 如果先通知再存盘，页面一收到通知就发来的请求会撞上还没放开的锁，被误判成"上一轮还没结束"。
                self.ui.end_turn()
                self._turn.release()

    def _save(self) -> None:
        try:
            self.app.save_conversation(self.ui.export_events())
        except OSError as exc:
            self.ui.error(f"保存会话失败：{exc}")

    def stop(self) -> None:
        if self._turn.locked():
            self.ui.request_stop()

    def new_conversation(self) -> bool:
        """清空对话。有一轮对话正在进行时返回 False。"""
        if not self._turn.acquire(blocking=False):
            return False
        try:
            self.app.new_conversation()
            self.ui.reset()
            self.publish_state()
        finally:
            self._turn.release()
        return True

    # ---- 会话 --------------------------------------------------------------

    def open_conversation(self, conversation_id: str) -> str:
        """打开一段保存过的会话。成功返回空字符串，否则返回原因。"""
        if not self._turn.acquire(blocking=False):
            return self.BUSY
        try:
            if conversation_id == self.app.conversation.id:
                return ""  # 已经开着了
            try:
                conversation = self.app.open_conversation(conversation_id)
            except ConversationError as exc:
                return str(exc)
            self.ui.load_events(conversation.events)
            if conversation.context_tokens >= LARGE_CONTEXT_TOKENS:
                self.ui.hint(
                    f"这段会话的历史约有 {conversation.context_tokens:,} 个 token。"
                    "接着聊的第一条消息需要把它重新发给模型，会按这个量计费。"
                )
            self.publish_state()
        finally:
            self._turn.release()
        return ""

    def rename_conversation(self, conversation_id: str, title: str) -> str:
        # 改名要读出会话文件、改掉标题、再写回去。如果这时正好有一轮对话结束、也在写同一个文件，
        # 两边会互相覆盖。所以和打开、删除一样，对话进行中不让改。
        if not self._turn.acquire(blocking=False):
            return self.BUSY
        try:
            try:
                self.app.rename_conversation(conversation_id, title)
            except ConversationError as exc:
                return str(exc)
            self.publish_state()
        finally:
            self._turn.release()
        return ""

    def delete_conversation(self, conversation_id: str) -> str:
        if not self._turn.acquire(blocking=False):
            return self.BUSY
        try:
            try:
                was_current = self.app.delete_conversation(conversation_id)
            except ConversationError as exc:
                return str(exc)
            if was_current:
                self.ui.reset()
            self.publish_state()
        finally:
            self._turn.release()
        return ""

    def reveal_conversation(self, conversation_id: str) -> tuple[str, str]:
        """在文件管理器里显示一段会话的文件；编号为空时显示保存会话的那个文件夹。返回（失败原因，显示的路径）。

        只是看一眼文件在哪儿，不改任何东西，所以不用拿对话的锁，对话进行中也可以点。
        """
        store, current = self.app.store, self.app.conversation
        if conversation_id == current.id and not current.saved:
            return "这段会话还没有保存：第一轮对话结束后才会写到磁盘上。", ""
        try:
            # 路径是由编号算出来的（编号的格式在 file_of 里检查过），浏览器没有办法指定别的路径
            target = store.file_of(conversation_id) if conversation_id else store.directory
        except ConversationError as exc:
            return str(exc), ""
        if not target.exists():
            return "还没有保存过会话，文件夹还不存在。" if not conversation_id else "这段会话的文件已经不在了。", ""
        try:
            reveal_in_file_manager(target)
        except OSError as exc:
            return f"没能打开文件管理器（{exc}）。文件在：{target}", ""
        return "", str(target)

    # ---- 设置 --------------------------------------------------------------
    # 下面几个操作都会改动助手的状态，所以和对话共用一把锁：对话进行中不能改。
    # 返回值的约定和 send 一样：空字符串表示成功，否则是失败的原因。

    BUSY = "请先等当前这一轮对话结束，或者点“停止”"

    def select_model(self, provider_id: str, model: str) -> str:
        if not has_key(PROVIDERS[provider_id]):
            return f"还没有设置 {PROVIDERS[provider_id].label} 的 API key"
        if not self._turn.acquire(blocking=False):
            return self.BUSY
        try:
            continued = self.app.select_model(provider_id, model)
            if not continued:
                self.ui.reset()  # 换了一家：历史格式不通用，页面也清空
            label = PROVIDERS[provider_id].label
            self.ui.info(f"已换成 {label} · {model}。" + ("对话继续。" if continued else "换了一家，开始新对话。"))
            if continued:
                self._save()  # 这段会话以后用新模型，记下来
            self.publish_state()
        finally:
            self._turn.release()
        return ""

    def set_api_key(self, provider_id: str, key: str) -> tuple[str, str]:
        """保存或删除一家的 API key。返回（失败原因，提醒）。

        保存成功后顺便同步一次模型列表；如果同步失败（比如 key 不对），key 仍然保存了，
        原因放在"提醒"里告诉用户。
        """
        if not self._turn.acquire(blocking=False):
            return self.BUSY, ""
        warning = ""
        try:
            self.app.set_api_key(provider_id, key)
            if key:
                try:
                    self.app.sync_models(provider_id)
                except ProviderError as exc:
                    warning = f"key 已保存，但同步模型列表失败：{exc}"
            else:
                self.app.settings.models.pop(provider_id, None)
                self.app.settings.save()
            self.publish_state()
        finally:
            self._turn.release()
        return "", warning

    def sync_models(self, provider_id: str) -> str:
        try:
            self.app.sync_models(provider_id)
        except ProviderError as exc:
            return str(exc)
        self.publish_state()
        return ""

    def set_price(self, model: str, price: dict | None) -> None:
        if price is None:
            self.app.settings.prices.pop(model, None)
        else:
            self.app.settings.prices[model] = price
        self.app.settings.save()
        self.publish_state()


class Server(ThreadingHTTPServer):
    daemon_threads = True  # 事件流的连接一直开着；退出程序时不必等它们
    web: WebApp


class Handler(BaseHTTPRequestHandler):
    server: Server

    def log_message(self, format: str, *args: object) -> None:
        pass  # 不在终端里打印每一个请求

    # ---- 路由 --------------------------------------------------------------

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if not self._allowed(parse_qs(url.query).get("token", [""])[0]):
            return
        if url.path == "/":
            self._send_page()
        elif url.path == "/events":
            self._send_events()
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._allowed(self.headers.get("X-Harness-Token", "")):
            return
        body = self._read_json()
        if body is None:
            return
        web = self.server.web
        path = urlsplit(self.path).path

        if path == "/api/message":
            text = body.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_MESSAGE_CHARS:
                self._send_json(400, {"error": "消息不能为空，也不能太长"})
            else:
                self._finish(web.send(text.strip()))
        elif path == "/api/confirm":
            confirm_id, allow, feedback = body.get("id"), body.get("allow"), body.get("feedback", "")
            if not isinstance(confirm_id, int) or not isinstance(allow, bool) or not isinstance(feedback, str):
                self._send_json(400, {"error": "参数不正确"})
            elif web.ui.answer_confirm(confirm_id, allow, feedback.strip()):
                self._send_json(200, {"ok": True})
            else:
                self._send_json(409, {"error": "这个确认请求已经处理过了"})
        elif path == "/api/stop":
            web.stop()
            self._send_json(200, {"ok": True})
        elif path == "/api/new":
            self._finish("" if web.new_conversation() else WebApp.BUSY)
        elif path.startswith("/api/conversation/"):
            conversation_id, title = body.get("id"), body.get("title")
            action = path.removeprefix("/api/conversation/")
            if not isinstance(conversation_id, str):
                self._send_json(400, {"error": "参数不正确"})
            elif action == "open":
                self._finish(web.open_conversation(conversation_id))
            elif action == "delete":
                self._finish(web.delete_conversation(conversation_id))
            elif action == "reveal":
                error, shown = web.reveal_conversation(conversation_id)
                if error:
                    self._send_json(409, {"error": error})
                else:
                    self._send_json(200, {"ok": True, "path": shown})
            elif action == "rename" and isinstance(title, str) and 0 < len(title.strip()) <= 200:
                self._finish(web.rename_conversation(conversation_id, title.strip()))
            else:
                self._send_json(400, {"error": "参数不正确"})
        elif path == "/api/search":
            query = body.get("query")
            if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
                self._send_json(400, {"error": f"搜索词必须是一段文字，最长 {MAX_QUERY_CHARS} 个字符"})
            else:
                # 只读内存里的索引，不改任何东西，所以对话进行中也可以搜
                self._send_json(200, {"ok": True, "results": web.app.store.search(query)})
        elif path == "/api/model":
            provider, model = body.get("provider"), body.get("model")
            if provider not in PROVIDERS or not isinstance(model, str) or not 0 < len(model.strip()) <= 200:
                self._send_json(400, {"error": "参数不正确"})
            else:
                self._finish(web.select_model(provider, model.strip()))
        elif path == "/api/key":
            provider, key = body.get("provider"), body.get("key")
            if provider not in PROVIDERS or not isinstance(key, str):
                self._send_json(400, {"error": "参数不正确"})
            elif key.strip() and not valid_api_key(key.strip()):
                self._send_json(400, {"error": "这不像是一个 API key：里面不能有空格、换行或中文"})
            else:
                error, warning = web.set_api_key(provider, key.strip())
                self._finish(error, warning=warning)
        elif path == "/api/models":
            provider = body.get("provider")
            if provider not in PROVIDERS:
                self._send_json(400, {"error": "参数不正确"})
            else:
                self._finish(web.sync_models(provider))
        elif path == "/api/price":
            model, price = body.get("model"), body.get("price")
            if not isinstance(model, str) or not model or not _valid_price(price):
                self._send_json(400, {"error": "价格必须是不小于 0 的数字，输入和输出两项必填"})
            else:
                web.set_price(model, price)
                self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"error": "not found"})

    def _finish(self, error: str, warning: str = "") -> None:
        """按"空字符串表示成功"的约定回复浏览器。"""
        if error:
            self._send_json(409, {"error": error})
        else:
            self._send_json(200, {"ok": True, "warning": warning})

    # ---- 安全检查 ----------------------------------------------------------

    def _allowed(self, supplied_token: str) -> bool:
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origin = self.headers.get("Origin")
        ok = (
            self.headers.get("Host") in hosts
            and (origin is None or origin in {f"http://{host}" for host in hosts})
            and secrets.compare_digest(supplied_token.encode(), self.server.web.token.encode())
        )
        if not ok:
            self._send_text(403, "链接无效或已经过期。请使用终端里显示的完整地址重新打开。")
        return ok

    # ---- 响应 --------------------------------------------------------------

    def _send_page(self) -> None:
        # nonce 是一个一次性的随机数：只有带着它的 <script> 和 <style> 才会被浏览器执行，
        # 这样即使页面里混进了别的脚本（比如模型输出里夹带的），浏览器也不会运行。
        nonce = secrets.token_urlsafe(16)
        policy = (
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        self._send(
            200,
            "text/html; charset=utf-8",
            PAGE.replace("__NONCE__", nonce).encode(),
            {"Content-Security-Policy": policy, "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"},
        )

    def _send_events(self) -> None:
        """事件流（Server-Sent Events）：连接不关闭，有新事件就写过去。

        刚连上时，先把日志里已有的事件补发一遍，然后发一个名叫 synced 的标记，意思是
        "旧的补完了，后面的都是此刻正在发生的"。页面靠它区分两种内容：补发的旧内容直接画出来，
        实时的新内容才播放入场动画。标记不带编号、也不进日志，所以不影响断线重连时
        "从第几号接着发"的计算。
        """
        try:
            # 浏览器断线重连时，会用这个请求头告诉我们它上次收到了第几号事件
            cursor = int(self.headers.get("Last-Event-ID") or 0)
        except ValueError:
            cursor = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        caught_up = False  # 已有的事件补发完了没有
        try:
            while True:
                # 第一次不等待：有多少旧事件就发多少，紧接着发标记
                wait = KEEPALIVE_SECONDS if caught_up else 0
                events = self.server.web.ui.events_after(cursor, timeout=wait)
                if not events and caught_up:
                    self.wfile.write(b": keepalive\n\n")  # 没有新事件时也定期写一点，才能发现对方已经断开
                if events:
                    cursor = events[-1]["id"]
                if not caught_up:
                    events = latest_state_only(events)  # 补发的这一批里，过时的状态不用发
                for event in events:
                    data = json.dumps(event, ensure_ascii=False, default=str)
                    self.wfile.write(f"id: {event['id']}\ndata: {data}\n\n".encode())
                if not caught_up:
                    caught_up = True
                    self.wfile.write(b"event: synced\ndata: {}\n\n")
        except OSError:
            pass  # 浏览器关掉了页面

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY_BYTES:
            self._send_json(413, {"error": "请求太大"})
            return None
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            self._send_json(400, {"error": "请求格式不正确"})
            return None
        return body

    def _send_json(self, status: int, payload: dict) -> None:
        self._send(status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode())

    def _send_text(self, status: int, text: str) -> None:
        self._send(status, "text/plain; charset=utf-8", text.encode())

    def _send(self, status: int, content_type: str, body: bytes, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def _valid_price(price: object) -> bool:
    """价格要么是 None（删除自己填的价格），要么是一组不小于 0 的数字。"""
    if price is None:
        return True
    if not isinstance(price, dict) or not {"input", "output"} <= set(price) <= {"input", "output", "cache_read"}:
        return False
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 100_000 for v in price.values())


def create_server(app: App, ui: WebUI, port: int | None = None, token: str | None = None) -> tuple[Server, str]:
    """创建服务器（还没有开始运行）。返回（服务器，在浏览器里打开的地址）。"""
    if port is not None:
        server = Server(("127.0.0.1", port), Handler)  # 指定了端口：被占用就直接报错
    else:
        try:
            server = Server(("127.0.0.1", DEFAULT_PORT), Handler)
        except OSError:
            server = Server(("127.0.0.1", 0), Handler)  # 默认端口被占用：让系统挑一个空闲的
    server.web = WebApp(app, ui, token or secrets.token_urlsafe(16))
    return server, f"http://127.0.0.1:{server.server_address[1]}/?token={server.web.token}"


def run_web(config: Config, port: int | None = None, open_browser: bool = True) -> int:
    ui = WebUI()
    app = build_app(config, ui)
    try:
        server, url = create_server(app, ui, port)
    except OSError as exc:
        print(f"无法在端口 {port} 上启动：{exc}")
        return 1

    print(f"{APP_NAME}（网页界面）已启动。在浏览器里打开这个地址：\n")
    print(f"    {url}\n")
    provider = app.provider
    print(f"模型：{provider.label} · {app.settings.model}    工作目录：{config.workspace}")
    if not has_key(provider):
        print(f"还没有设置 {provider.label} 的 API key：打开页面后，点右上角的“设置”填写。")
    print("地址里的 token 是这次运行的口令，不要发给别人。按 Ctrl-C 退出。")
    if open_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.web.stop()
        app.shell.kill_running()
        server.server_close()
    print("\n再见。")
    return 0
