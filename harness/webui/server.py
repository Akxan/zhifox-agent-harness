"""本地 HTTP 服务器：浏览器和 agent 之间的桥。

浏览器只会发这几种请求：

  GET  /               页面本身（index.html）
  GET  /events         事件流。这个连接一直不关，服务器把 WebUI 日志里的新事件不断推过去
  POST /api/message    用户发了一条消息
  POST /api/confirm    用户对一个确认请求点了"允许"或"拒绝"
  POST /api/stop       用户点了"停止"
  POST /api/new        用户点了"新对话"
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
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..app import App, build_app, run_turn
from ..config import Config
from ..pricing import estimate, price_of
from ..providers import CLAUDE_MODELS, PROVIDERS, ProviderError, has_key
from ..settings import key_hint, valid_api_key
from .ui import WebUI

DEFAULT_PORT = 8765
MAX_BODY_BYTES = 1_000_000
MAX_MESSAGE_CHARS = 100_000
KEEPALIVE_SECONDS = 15

PAGE = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")


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
            self.ui.begin_turn(text)
            run_turn(self.app.agent, self.ui, text)
            self.publish_state()
        finally:
            self.ui.end_turn()
            self._turn.release()

    def stop(self) -> None:
        if self._turn.locked():
            self.ui.request_stop()

    def new_conversation(self) -> bool:
        """清空对话。有一轮对话正在进行时返回 False。"""
        if not self._turn.acquire(blocking=False):
            return False
        try:
            self.app.agent.reset()
            self.ui.reset()
            self.publish_state()
        finally:
            self._turn.release()
        return True

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
        """事件流（Server-Sent Events）：连接不关闭，有新事件就写过去。"""
        try:
            # 浏览器断线重连时，会用这个请求头告诉我们它上次收到了第几号事件
            cursor = int(self.headers.get("Last-Event-ID") or 0)
        except ValueError:
            cursor = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                events = self.server.web.ui.events_after(cursor, timeout=KEEPALIVE_SECONDS)
                if not events:
                    self.wfile.write(b": keepalive\n\n")  # 没有新事件时也定期写一点，才能发现对方已经断开
                for event in events:
                    data = json.dumps(event, ensure_ascii=False, default=str)
                    self.wfile.write(f"id: {event['id']}\ndata: {data}\n\n".encode())
                    cursor = event["id"]
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

    print("Harness 个人助手（网页界面）已启动。在浏览器里打开这个地址：\n")
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
