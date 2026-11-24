from __future__ import annotations

import base64
import ipaddress
import socket
import ssl
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

from ..i18n import t
from .websocket_protocol import WebSocketError

MAX_HANDSHAKE = 16_384  # 握手的回答（状态行加上各个头）最多这么多字节
BODY_PREVIEW = 300  # 握手被拒绝时，留下回答正文的这么多字节（HandshakeError.body），给人看为什么


@dataclass(frozen=True)
class Address:
    secure: bool
    host: str
    port: int
    target: str  # 请求行里的路径加查询，比如 /?v=10&encoding=json

    @property
    def authority(self) -> str:
        default = 443 if self.secure else 80
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.port == default else f"{host}:{self.port}"


def parse_url(url: str) -> Address:
    """拆开一个 ws:// 或 wss:// 的网址。明文的 ws:// 只允许连本机。"""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:  # 比如 ws://[::1/ 这样括号没配对的：urlsplit 会直接抛 ValueError，调用的人只认 WebSocketError
        raise WebSocketError(t("不是一个 WebSocket 的网址。")) from None
    scheme = parts.scheme.lower()
    if scheme not in ("ws", "wss") or not parts.hostname:
        raise WebSocketError(t("不是一个 WebSocket 的网址。"))
    if scheme == "ws" and not is_loopback(parts.hostname):
        raise WebSocketError(t("为了安全，只能用加密的 wss:// 连接（ws:// 只允许连这台电脑自己）。"))
    try:
        port = parts.port or (443 if scheme == "wss" else 80)
    except ValueError:
        raise WebSocketError(t("不是一个 WebSocket 的网址。")) from None
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return Address(scheme == "wss", parts.hostname, port, target)


def is_loopback(host: str) -> bool:
    """host 是不是这台电脑自己。只认 localhost 和真正的回环地址（127.0.0.0/8、::1）。

    不能只看"是不是 127. 开头"：127.example.com 是一个普通的域名，可以解析到任何一台机器上，
    那样明文的 ws://（还有 http.trusted 放行的 http://）就会把数据不加密地发到外面去。
    """
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:  # 不是 IP 地址，是一个域名
        return False


def _proxy_for(address: Address) -> str | None:
    """这个地址要不要走代理、走哪一个（和 urllib 一样看 HTTPS_PROXY、NO_PROXY，macOS 上还看系统设置）。连本机永远不走代理。"""
    if is_loopback(address.host):
        return None
    try:
        if urllib.request.proxy_bypass(address.host):
            return None
        proxies = urllib.request.getproxies()
    except Exception:  # noqa: BLE001 —— 读系统的代理设置出了错：当作没有代理
        return None
    return proxies.get("https" if address.secure else "http") or proxies.get("all") or None


# ---- 底下的几样 ------------------------------------------------------------------


def _reason(exc: BaseException) -> str:
    return str(getattr(exc, "strerror", None) or getattr(exc, "reason", None) or exc) or type(exc).__name__


def _quietly_close(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _open(address: Address, timeout: float, context: ssl.SSLContext | None) -> socket.socket:
    """连上服务器（需要的话经过代理），套上 TLS。返回的套接字有超时，读写不会永远卡住。"""
    proxy = _proxy_for(address)
    try:
        if proxy:
            sock = _tunnel(proxy, address, timeout)
        else:
            sock = socket.create_connection((address.host, address.port), timeout=timeout)
    except WebSocketError:
        raise
    except (OSError, ValueError) as exc:
        # ValueError：域名写得不对（a..b 这样的，IDNA 编码失败），或者代理的端口不是数字。
        # 也要换成 WebSocketError：调用的人（网关的重连循环）只认它，漏出去一个 ValueError 会让整个线程退出
        raise WebSocketError(t("连不上 {host}（{error}）", host=address.host, error=_reason(exc))) from None
    sock.settimeout(timeout)
    if not address.secure:
        return sock
    try:
        tls = (context or ssl.create_default_context()).wrap_socket(sock, server_hostname=address.host)
    except (OSError, ssl.SSLError) as exc:
        _quietly_close(sock)
        raise WebSocketError(t("和 {host} 建立加密连接失败（{error}）", host=address.host, error=_reason(exc))) from None
    tls.settimeout(timeout)
    return tls


def _body_start(sock: socket.socket, rest: bytes, headers: dict[str, str], timeout: float) -> bytes:
    """握手被拒绝时，回答正文的开头（平台常在这里写为什么，比如 401 的原因）。

    正文常常和头分成两个包到：头读完的那一刻，正文可能还没来。所以照着 Content-Length 再收一会儿，
    最多 BODY_PREVIEW 个字节、最多 timeout 秒；没有 Content-Length（或者等不到）就用已经收到的那些。
    """
    try:
        wanted = min(int(headers.get("content-length", "")), BODY_PREVIEW)
    except ValueError:
        return rest
    deadline = time.monotonic() + timeout
    while len(rest) < wanted and time.monotonic() < deadline:
        try:
            sock.settimeout(max(0.01, deadline - time.monotonic()))
            chunk = sock.recv(4096)
        except OSError:  # 超时也是 OSError：正文就不要了，状态码已经够说明问题
            break
        if not chunk:
            break
        rest += chunk
    return rest


def _tunnel(proxy: str, address: Address, timeout: float) -> socket.socket:
    """经过 HTTP 代理：先连代理，发 CONNECT host:port，代理回 200 以后，这条连接就是一条通到目标的隧道。"""
    parts = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
    if parts.scheme != "http" or not parts.hostname:
        raise WebSocketError(t("只支持 http:// 的代理（现在设置的是 {scheme}）。", scheme=parts.scheme or "?"))
    sock = socket.create_connection((parts.hostname, parts.port or 8080), timeout=timeout)
    # CONNECT 后面是"主机:端口"，IPv6 的地址自己就带冒号，要加方括号，不然代理分不清哪一段是端口（RFC 9110 §9.3.6）
    host = f"[{address.host}]" if ":" in address.host else address.host
    target = f"{host}:{address.port}"
    lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
    if parts.username:
        user = urllib.parse.unquote(parts.username) + ":" + urllib.parse.unquote(parts.password or "")
        lines.append("Proxy-Authorization: Basic " + base64.b64encode(user.encode()).decode())
    try:
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        status, _, rest = _read_head(sock, address)
    except BaseException:
        _quietly_close(sock)
        raise
    if status != 200 or rest:
        _quietly_close(sock)
        raise WebSocketError(t("代理没有同意连到 {host}（HTTP {status}）", host=address.host, status=status))
    return sock


def _read_head(sock: socket.socket, address: Address) -> tuple[int, dict[str, str], bytes]:
    """读一个 HTTP 回答的状态行和各个头。返回（状态码，头（名字小写），头后面已经收到的字节）。"""
    data = b""
    while b"\r\n\r\n" not in data:
        if len(data) > MAX_HANDSHAKE:
            raise WebSocketError(t("{host} 的回答太长了。", host=address.host))
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            raise WebSocketError(t("等 {host} 回答超时了。", host=address.host)) from None
        except OSError as exc:
            raise WebSocketError(t("连不上 {host}（{error}）", host=address.host, error=_reason(exc))) from None
        if not chunk:
            raise WebSocketError(t("{host} 还没回答就断开了。", host=address.host))
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    if len(head) > MAX_HANDSHAKE:  # 上面的循环只管"别一直读下去"；最后一块里才出现空行时，头本身也可能超了
        raise WebSocketError(t("{host} 的回答太长了。", host=address.host))
    lines = head.decode("iso-8859-1").split("\r\n")
    status_line = lines[0].split(" ", 2)
    if len(status_line) < 2 or not status_line[0].startswith("HTTP/") or not status_line[1].isdigit():
        raise WebSocketError(t("{host} 的回答不是 HTTP。", host=address.host))
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        if colon:
            name = name.strip().lower()
            headers[name] = (headers[name] + ", " if name in headers else "") + value.strip()
    return int(status_line[1]), headers, rest
