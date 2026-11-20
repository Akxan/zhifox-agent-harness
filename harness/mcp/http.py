"""Streamable HTTP 传输：MCP 服务器是网上（或本机）的一个网址，每条消息是一次 HTTP POST。

    智狐 ──POST 一条请求──► https://example.com/mcp
         ◄── 回答：要么是一个 JSON（application/json），
                    要么是一串"服务器推送事件"（text/event-stream）：先是几条和这个请求有关的通知，最后是回答

和 stdio 不同，这里每个请求有自己的一条连接，所以：
  - 取消一个请求，新一代协议里就是挂断它那条连接（老一代还要另外发一条 notifications/cancelled）。
  - 服务器想主动告诉我们"工具变了"，得有一条一直开着的连接：新一代是一个不结束的 subscriptions/listen 请求，
    老一代是一个 GET 请求（见 listen）。

两代协议在 HTTP 上的区别（为什么有两代，见 client.py）：
  新一代（2026-07-28）  每个请求都带 MCP-Protocol-Version、Mcp-Method（和 Mcp-Name）这几个请求头，没有会话。
  老一代（2025-11-25）  先 initialize，服务器可能发给我们一个 Mcp-Session-Id，之后每个请求都要带上它。

用的是 Python 自带的 http.client，每一个请求头、每一行事件都看得见。认证只做了最简单的那种：
你在请求头里填上 Authorization: Bearer 令牌，每个请求都带上。MCP 规范里的 OAuth 登录流程这里没有做。
"""

from __future__ import annotations

import base64
import http.client
import itertools
import json
import socket
import ssl
import threading
import time
import urllib.request
from collections.abc import Callable
from urllib.parse import unquote, urlsplit

from ..i18n import t
from ..safe import clean, tame, to_json
from . import jsonrpc
from .jsonrpc import McpError, RpcError, Slot
from .log import ServerLog

MAX_BODY_BYTES = 64_000_000  # 一次回答最多读这么多字节
CONNECT_TIMEOUT = 20  # 建立连接最多等这么久
LISTEN_READ_TIMEOUT = 300  # 一直开着的那条连接，这么久没有任何动静就重新连一次
USER_AGENT = "ZhiFox-MCP-Client"
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_TLS: ssl.SSLContext | None = None


class HttpError(McpError):
    """服务器用一个 HTTP 错误状态回答了，而且回答的内容不是 JSON-RPC 的错误。"""

    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


class SessionExpired(McpError):
    """老一代协议的会话过期了（服务器回答 404）。重新 initialize 一次就好。"""


class HttpTransport:
    name = "http"

    def __init__(self, url: str, headers: dict[str, str], log: ServerLog):
        self.url = url
        self.headers = dict(headers)
        self.log = log
        self.modern = True  # 请求按哪一代的规矩发。client.py 弄清对面是哪一代之后会改它
        self.protocol_version = ""  # 请求头 MCP-Protocol-Version 的值；老一代在 initialize 之前不带
        self.session_id: str | None = None  # 老一代的会话编号
        self.on_notification: Callable[[dict], None] = lambda message: None
        self.on_request: Callable[[dict], dict] = lambda message: {}
        self.on_closed: Callable[[str], None] = lambda reason: None  # HTTP 没有"进程退出"，留着只是和 stdio 一样
        self._ids = itertools.count(1)
        self._open: dict[object, list] = {}  # 还在进行的请求：id → [连接]，取消时靠它挂断
        self._lock = threading.Lock()
        self._closing = False
        self._listening: list = []  # 一直开着的那条连接

    # ---- 启动和关闭 ---------------------------------------------------------

    @property
    def alive(self) -> bool:
        return not self._closing

    def start(self) -> None:
        parts = urlsplit(self.url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise McpError(t("地址必须以 http:// 或 https:// 开头：{url}", url=self.url))

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        with self._lock:
            connections = [box for box in self._open.values()] + [self._listening]
            self._open = {}
        for box in connections:
            _hang_up(box)
        if not self.modern and self.session_id:
            # 老一代：告诉服务器这个会话不用了。服务器不支持（405）或者已经连不上，都无所谓
            try:
                connection, target = self._connect(5)
                connection.request("DELETE", target, headers=self._base_headers())
                connection.getresponse().read(1000)
                connection.close()
            except (OSError, http.client.HTTPException, McpError, ValueError):
                pass

    # ---- 发消息 -------------------------------------------------------------

    def send(self, method: str, params: dict | None = None, headers: dict | None = None, timeout: float = 600) -> Slot:
        """发出一个请求，返回等它回答的座位（不等）。真正的收发在一个后台线程里做，这样随时可以挂断。"""
        id = next(self._ids)
        slot, box = Slot(id, method), []
        with self._lock:
            self._open[id] = box
        message = jsonrpc.request(id, method, params)

        def work() -> None:
            try:
                answer, status = self._post(message, headers or {}, box, timeout)
                if answer is None:
                    raise McpError(t("服务器没有回答这个请求（{method}）。", method=method))
                slot.settle(answer, status)
            except McpError as exc:
                slot.fail(exc)
            except (OSError, http.client.HTTPException, ValueError) as exc:
                slot.fail(McpError(self._network_problem(exc)))
            finally:
                with self._lock:
                    self._open.pop(id, None)

        threading.Thread(target=work, daemon=True).start()
        return slot

    def request(self, method: str, params: dict | None, timeout: float, stop: threading.Event | None = None, headers: dict | None = None) -> dict:
        slot = self.send(method, params, headers, timeout)
        try:
            return jsonrpc.wait(slot, timeout, stop)
        except BaseException:
            self.forget(slot, cancel=True)
            raise

    def forget(self, slot: Slot, cancel: bool) -> None:
        """不再等这个请求：挂断它的连接。新一代协议里挂断就是取消；老一代另外发一条取消通知。"""
        with self._lock:
            box = self._open.pop(slot.id, None)
        if box is None:
            return
        _hang_up(box)
        if cancel and not self.modern and not self._closing:
            params = {"requestId": slot.id, "reason": "cancelled by the user or timed out"}
            threading.Thread(target=self._quietly, args=(jsonrpc.notification("notifications/cancelled", params),), daemon=True).start()

    def notify(self, method: str, params: dict | None = None) -> None:
        self._post(jsonrpc.notification(method, params), {}, [], 15)

    def _quietly(self, message: dict) -> None:
        try:
            self._post(message, {}, [], 15)
        except (McpError, OSError, http.client.HTTPException, ValueError):
            pass

    # ---- 一条 POST 的来回 ----------------------------------------------------

    def _post(self, message: dict, extra: dict, box: list, timeout: float) -> tuple[dict | None, int]:
        """发出一条消息。是请求的话，返回（它的回答，状态码）；是通知或回答，返回（None，状态码）。"""
        connection, target = self._connect(timeout + 5)
        box.append(connection.sock)  # 记下底下的那个 socket：读回答的时候 connection.sock 可能已经被 http.client 放开了
        if self._closing:
            _hang_up(box)
            raise McpError(t("连接已经关闭。"))
        headers = {**self._base_headers(), **self._mcp_headers(message), **extra}
        connection.request("POST", target, body=to_json(message).encode(), headers=headers)
        response = connection.getresponse()
        status = response.status
        if message.get("method") == "initialize":
            session = response.getheader("Mcp-Session-Id")
            if session and all(0x21 <= ord(char) <= 0x7E for char in session) and len(session) <= 1000:
                self.session_id = session
        try:
            if "id" not in message or "method" not in message:  # 通知或者回答：服务器应该回答 202，没有内容
                response.read(MAX_BODY_BYTES)
                if status >= 400:
                    raise self._status_problem(status, "")
                return None, status
            if status == 404 and self.session_id and not self.modern:
                raise SessionExpired(t("会话已经过期。"))
            if status in (401, 403):
                raise self._status_problem(status, "")
            kind = (response.getheader("Content-Type") or "").split(";")[0].strip().lower()
            if kind == "text/event-stream" and status < 400:
                return self._read_events(response, message["id"]), status
            body = response.read(MAX_BODY_BYTES)
            try:
                answer = clean(tame(json.loads(body))) if body.strip() else None
            except (ValueError, RecursionError):
                answer = None
            if jsonrpc.kind(answer) == "response":
                return answer, status  # 包括错误：带着状态码交出去，client.py 靠它判断对面是哪一代
            if status >= 400:
                raise self._status_problem(status, body.decode("utf-8", errors="replace"))
            raise McpError(t("服务器的回答看不懂（状态码 {status}，类型 {kind}）。", status=status, kind=kind or "?"))
        finally:
            connection.close()

    def _read_events(self, response: http.client.HTTPResponse, want: object) -> dict:
        """读"服务器推送事件"（SSE），直到等到 id 为 want 的那条回答。中间的通知交给 on_notification。"""
        for message in _events(response):
            kind = jsonrpc.kind(message)
            if kind == "response" and message.get("id") == want:
                return message
            self._dispatch(message, kind)
        raise McpError(t("服务器还没给出回答就关闭了连接。"))

    def _dispatch(self, message: dict, kind: str) -> None:
        if kind == "notification":
            try:
                self.on_notification(message)
            except Exception as exc:  # noqa: BLE001 —— 处理一条通知时的 bug 不能把这次请求也弄丢
                self.log.note(t("处理通知时出错：{error}", error=exc))
        elif kind == "request":
            # 老一代的服务器会在事件里反过来问我们（比如 ping）。回答要另发一个 POST
            try:
                answer = jsonrpc.result(message["id"], self.on_request(message))
            except RpcError as exc:
                answer = jsonrpc.error(message["id"], exc.code, exc.text)
            threading.Thread(target=self._quietly, args=(answer,), daemon=True).start()

    # ---- 一直开着的那条连接：接收"工具变了"之类的通知 ------------------------

    def listen(self, params: dict) -> None:
        """在后台开一条一直不关的连接，服务器有变化时从这里通知我们。断了就隔一会儿重连，直到关闭。

        新一代：POST 一个 subscriptions/listen 请求，它的回答就是一串永远不结束的事件。
        老一代：GET 这个网址。服务器不支持的话回答 405，那就算了。
        """
        threading.Thread(target=self._listen_loop, args=(params,), daemon=True).start()

    def _listen_loop(self, params: dict) -> None:
        pause = 1.0
        while not self._closing:
            box: list = []
            self._listening = box
            try:
                connection, target = self._connect(LISTEN_READ_TIMEOUT)
                box.append(connection.sock)
                if self.modern:
                    message = jsonrpc.request(next(self._ids), "subscriptions/listen", params)
                    headers = {**self._base_headers(), **self._mcp_headers(message)}
                    connection.request("POST", target, body=to_json(message).encode(), headers=headers)
                else:
                    connection.request("GET", target, headers={**self._base_headers(), "Accept": "text/event-stream"})
                response = connection.getresponse()
                kind = (response.getheader("Content-Type") or "").split(";")[0].strip().lower()
                if response.status >= 400 or kind != "text/event-stream":
                    if response.status not in (404, 405):
                        self.log.note(t("服务器不接受通知连接（状态码 {status}），工具有变化时不会自动更新。", status=response.status))
                    return
                pause = 1.0
                for message in _events(response):
                    self._dispatch(message, jsonrpc.kind(message))
            except (OSError, http.client.HTTPException, McpError, ValueError):
                pass
            finally:
                _hang_up(box)
            if self._closing:
                return
            time.sleep(pause)  # 断了：等一会儿再连，越等越久，最多一分钟
            pause = min(pause * 2, 60)

    # ---- 连接和请求头 --------------------------------------------------------

    def _base_headers(self) -> dict[str, str]:
        """每个请求都带的：你配的请求头（比如 Authorization），加上会话编号和协议版本。"""
        headers = {"User-Agent": USER_AGENT, **self.headers, "Accept": "application/json, text/event-stream"}
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        if self.session_id and not self.modern:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _mcp_headers(self, message: dict) -> dict[str, str]:
        """新一代协议把请求体里的方法名和工具名抄一份到请求头里，方便中间的网关不拆开请求体也能分辨请求。"""
        headers = {"Content-Type": "application/json"}
        if self.modern and "method" in message and "id" in message:
            headers["Mcp-Method"] = message["method"]
            params = message.get("params") or {}
            name = params.get("name") if message["method"] in ("tools/call", "prompts/get") else params.get("uri")
            if isinstance(name, str):
                headers["Mcp-Name"] = header_value(name)
        return headers

    def _connect(self, timeout: float) -> tuple[http.client.HTTPConnection, str]:
        """建立到服务器的连接。返回（连接，请求行里写的目标）。系统设置了代理的话走代理。"""
        parts = urlsplit(self.url)
        host, secure = parts.hostname or "", parts.scheme == "https"
        port = parts.port or (443 if secure else 80)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        proxy = _proxy_for(parts.scheme, host)
        connection: http.client.HTTPConnection
        if proxy is not None:
            proxy_host, proxy_port, proxy_auth = proxy
            if secure:  # HTTPS 走代理：先让代理打通一条到目标的隧道（CONNECT），里面再握手加密
                connection = http.client.HTTPSConnection(proxy_host, proxy_port, timeout=CONNECT_TIMEOUT, context=_tls())
                connection.set_tunnel(host, port, headers=proxy_auth)
            else:  # 明文 HTTP 走代理：请求行里写完整的地址
                connection = http.client.HTTPConnection(proxy_host, proxy_port, timeout=CONNECT_TIMEOUT)
                target = self.url
        elif secure:
            connection = http.client.HTTPSConnection(host, port, timeout=CONNECT_TIMEOUT, context=_tls())
        else:
            connection = http.client.HTTPConnection(host, port, timeout=CONNECT_TIMEOUT)
        try:
            connection.connect()
        except (OSError, http.client.HTTPException) as exc:
            raise McpError(self._network_problem(exc)) from exc
        assert connection.sock is not None
        connection.sock.settimeout(timeout)  # 连上之后，读回答可以等得久一些
        return connection, target

    def _network_problem(self, exc: BaseException) -> str:
        host = urlsplit(self.url).netloc
        if isinstance(exc, ssl.SSLCertVerificationError):
            return t("{host} 的 HTTPS 证书验证失败：{error}", host=host, error=exc.verify_message or exc)
        if isinstance(exc, TimeoutError | socket.timeout):
            return t("连接 {host} 超时了。", host=host)
        if isinstance(exc, ConnectionRefusedError):
            return t("{host} 拒绝了连接：那里可能没有在运行服务器。", host=host)
        if isinstance(exc, socket.gaierror):
            return t("找不到 {host} 这个地址，请检查网址和网络。", host=host)
        return t("连接 {host} 失败：{error}", host=host, error=exc)

    def _status_problem(self, status: int, body: str) -> HttpError:
        if status == 401:
            return HttpError(
                status,
                t("服务器要求登录（401）。智狐还不支持 MCP 的 OAuth 登录；如果这个服务器接受令牌，可以在请求头里加上 Authorization: Bearer 你的令牌。"),
            )
        if status == 403:
            return HttpError(status, t("服务器拒绝了这个请求（403）：令牌不对，或者没有权限。"))
        detail = jsonrpc.brief(body)[:200]
        return HttpError(status, t("服务器回答了 HTTP {status}：{detail}", status=status, detail=detail or "—"))


def header_value(value: str) -> str:
    """把一段文字放进请求头。请求头只能放可见的 ASCII 字符，放不下的（中文、首尾空格、换行）按规范写成 base64。"""
    plain = value == value.strip() and all(0x20 <= ord(char) <= 0x7E for char in value)
    if plain and not (value.startswith("=?base64?") and value.endswith("?=")):
        return value
    return "=?base64?" + base64.b64encode(value.encode()).decode() + "?="


def _events(response: http.client.HTTPResponse):
    """把 SSE 的回答拆成一条条 JSON-RPC 消息。

    SSE 的格式很简单：一个事件由几行"字段: 值"组成，空行表示这个事件结束；以冒号开头的是注释（保持连接用的）。
    我们只关心 data 字段，它的内容就是一条 JSON-RPC 消息（一个事件里有几行 data，就把它们用换行接起来）。
    """
    data: list[str] = []
    size = 0
    while True:
        raw = response.readline(MAX_BODY_BYTES)
        if not raw:
            return
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line:
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            if field == "data":
                data.append(value.removeprefix(" "))
                size += len(value)
                if size > MAX_BODY_BYTES:
                    raise McpError(t("服务器发来的一个事件太大了。"))
            continue
        if not data:
            continue
        text, data, size = "\n".join(data), [], 0
        try:
            message = clean(tame(json.loads(text)))
        except (ValueError, RecursionError):
            continue
        yield from (one for one in (message if isinstance(message, list) else [message]) if isinstance(one, dict))


def _hang_up(box: list) -> None:
    """挂断一条连接（可能正有别的线程在上面等着读）。先 shutdown，那个线程会立刻读到"连接断了"。

    box 里放的是连接底下的 socket，不是 http.client 的连接对象：回答要一直读到连接关闭时（SSE 就是这样），
    http.client 会把 socket 交给回答对象、自己的 sock 变成 None，这时只有直接对 socket 动手才挂得断。
    """
    for sock in list(box):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            pass
        try:
            sock.close()
        except (OSError, AttributeError):
            pass


def _tls() -> ssl.SSLContext:
    """验证 HTTPS 证书用的设置。装了 truststore 就用系统自己的证书库（公司内网的证书也认），否则用 Python 自带的。"""
    global _TLS
    if _TLS is None:
        try:
            import truststore

            _TLS = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except ImportError:
            _TLS = ssl.create_default_context()
    return _TLS


def _proxy_for(scheme: str, host: str) -> tuple[str, int, dict[str, str]] | None:
    """这个地址要不要走代理：看环境变量（HTTPS_PROXY 这些）和系统设置。本机的地址永远不走。"""
    if host in _LOOPBACK or host.startswith("127."):
        return None
    try:
        if urllib.request.proxy_bypass(host):
            return None
        raw = urllib.request.getproxies().get(scheme)
    except Exception:  # noqa: BLE001 —— 读系统代理设置出了任何问题，都按"不走代理"处理
        return None
    if not raw:
        return None
    parts = urlsplit(raw if "://" in raw else f"http://{raw}")
    if not parts.hostname:
        return None
    auth = {}
    if parts.username:
        token = base64.b64encode(f"{unquote(parts.username)}:{unquote(parts.password or '')}".encode()).decode()
        auth["Proxy-Authorization"] = f"Basic {token}"
    return parts.hostname, parts.port or 8080, auth
