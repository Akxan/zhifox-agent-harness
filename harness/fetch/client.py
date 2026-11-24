"""真的去取一个网页：自己解析域名、检查每一个地址、连到检查过的那一个上，自己跟着跳转。

为什么不用 urllib（net/http.py 用的那个）：
  - urllib 自己解析域名、自己连接，我们插不进"连之前看一眼地址"这一步。先查一遍、再交给 urllib 也不行：
    它连的时候会再解析一次，第二次的答案可以不一样（DNS 重绑定：第一次给公网地址，第二次给 127.0.0.1）。
    所以这里解析一次、查一遍，然后连到查过的那个地址上；TLS 照样按域名验证证书（server_hostname），Host 头也写域名。
  - urllib 自己跟着跳转。跳转的目的地是网站定的，每一跳都要从头再查一遍，所以我们自己跟，最多 FETCH_MAX_REDIRECTS 次。
  - urllib 会照着 HTTP(S)_PROXY 走代理。走代理时是代理去解析、去连，我们管不住它连到哪儿，所以读网页不走代理。

发出去的请求：一个 GET，User-Agent 是 ZhiFox/版本号，不带 Cookie、不带认证、不带任何密钥，也不说你用什么语言
（Accept-Language 也是关于你的信息）。回答最多读 FETCH_MAX_BYTES 个字节（压缩过的按解压以后的算）。

时间：整个过程（连同每一次跳转）最多 FETCH_TIMEOUT 秒。每次收数据的超时只管"这一下等多久"，一个每隔几秒发一个字节的网站
能让它一直不超时，所以每一次连接另有一个"看门的"（_Watchdog）：到了截止的时刻就把连接掐断，正在等的那一下马上出错。

测试用的接缝：Fetcher 的 resolve（怎么解析域名）和 policy（哪些地址不能去）可以换。测试里的假网站在 127.0.0.1 上，
真的规矩一定会拦下它，所以测试换一个"只放行这一个假网站"的 policy。设置和模型都碰不到 Fetcher：
它只在 App.fetcher 上（见 app.py），程序里从来不换。真的规矩另有测试，只用地址本身测（tests/test_fetch_guard.py）。
"""

from __future__ import annotations

import http.client
import socket
import ssl
import threading
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin

from ..browser import urls
from ..config import FETCH_MAX_BYTES, FETCH_MAX_REDIRECTS, FETCH_TIMEOUT
from . import guard

Resolver = Callable[[str, int], list[str]]  # （主机名，端口）→ 解析出来的地址
Policy = Callable[[str], "str | None"]  # 一个地址 → 不能去的原因；能去是 None

REDIRECTS = {301, 302, 303, 307, 308}
CHUNK = 64 * 1024
ACCEPT = "text/html,application/xhtml+xml,text/plain;q=0.9,application/json;q=0.9,*/*;q=0.5"


class FetchError(Exception):
    """没读到。说明是写给模型看的（中文），也照样显示在工具卡片上。"""


@dataclass(frozen=True)
class Fetched:
    """取回来的东西。"""

    url: str  # 最后落在的网址（跳转过的话是跳到的那一个）
    status: int
    headers: dict[str, str]  # 名字都是小写
    body: bytes  # 解压过的
    cut: bool = False  # 太大了，只留了前 FETCH_MAX_BYTES 个字节
    hops: tuple[str, ...] = ()  # 跳转之前经过的网址（第一个是你要读的那个）

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def system_resolve(host: str, port: int) -> list[str]:
    """用系统的解析函数（getaddrinfo）解析一个域名，交回去重以后的地址（顺序照系统给的）。"""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def user_agent() -> str:
    from ..version import version  # 用到时才导入：version 要读包的元数据

    return f"ZhiFox/{version() or '0'}"


class _Watchdog:
    """到了截止的时刻掐断连接（为什么要它见文件开头）。fired 为真说明是它掐断的：那就是超时，不是网站出了错。"""

    def __init__(self, seconds: float):
        self.fired = False
        self.sock: socket.socket | None = None  # 现在的那个连接（握手以后换成 TLS 的那一个）
        self._timer = threading.Timer(max(seconds, 0.0), self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self) -> None:
        self.fired = True
        sock = self.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)  # shutdown 才能叫醒正卡在 recv 里的那一下（光 close 在 Linux 上叫不醒）
            except OSError:
                pass

    def cancel(self) -> None:
        self._timer.cancel()


@dataclass
class Fetcher:
    """取网页的那一个（规矩见文件开头）。resolve、policy 是测试用的接缝，程序里一直是默认的。"""

    resolve: Resolver | None = None  # 不给就是 system_resolve（每次现找：测试的 conftest 把它换成"不许解析真的域名"）
    policy: Policy = guard.refusal
    timeout: float = FETCH_TIMEOUT
    limit: int = FETCH_MAX_BYTES
    redirects: int = FETCH_MAX_REDIRECTS
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    def get(self, url: str) -> Fetched:
        """取一个（整理过的）网址。不读的抛出 guard.Refused，没读到的抛出 FetchError，都说明为什么。"""
        deadline = self.clock() + self.timeout
        hops: list[str] = []
        current = url
        for _ in range(self.redirects + 1):
            try:
                target = guard.parse(current)
                addresses = self._addresses(target)
            except guard.Refused as exc:
                if not hops:
                    raise
                raise guard.Refused(f"{_shown(hops[-1])} 跳转到了 {_shown(current)}。{exc}") from None
            status, headers, body, cut = self._exchange(target, addresses, deadline)
            location = headers.get("location", "").strip()
            if status in REDIRECTS and location:
                hops.append(target.url)
                try:
                    current = urljoin(target.url, location)
                except ValueError:  # urlsplit 自己就拒绝的写法（方括号里不是 IP 地址……）：下一圈的 parse 说清楚为什么不读
                    current = location
                continue
            return Fetched(target.url, status, headers, body, cut, tuple(hops))
        raise FetchError(f"跳转了 {self.redirects} 次还没到，没有再跟下去（最后一次跳到 {_shown(current)}）。")

    def _addresses(self, target: guard.Target) -> list[str]:
        """要连的地址：每一个都查过。有一个不能去就整个不读：一个域名同时指向公网和局域网，多半有问题。"""
        if target.literal:
            found = [target.host]
        else:
            try:
                found = (self.resolve or system_resolve)(target.host, target.port)
            except (OSError, UnicodeError) as exc:
                raise FetchError(f"找不到 {target.host}（{exc}）。检查一下网址，或者这台电脑现在连不上网。") from None
        if not found:
            raise FetchError(f"找不到 {target.host}。")
        for address in found:
            why = self.policy(address)
            if why is not None:
                where = target.host if target.literal else f"{target.host}（解析到 {address}）"
                raise guard.Refused(
                    f"没有读：{where} 是{why}。读网页只读公网上的网站，这台电脑、局域网里的地址一律不读；"
                    "要看本机或者局域网里的网页，请用户打开浏览器的功能。"
                )
        return found

    def _exchange(self, target: guard.Target, addresses: list[str], deadline: float) -> tuple[int, dict[str, str], bytes, bool]:
        """连上去、发请求、读回答（跳转的回答不读正文）。"""
        watchdog = _Watchdog(deadline - self.clock())
        connection = http.client.HTTPConnection(target.host, target.port)
        try:
            connection.sock = self._connect(target, addresses, deadline, watchdog)  # http.client 看到有 sock 就不再自己连
            headers = {
                "Host": target.host_header,
                "User-Agent": user_agent(),
                "Accept": ACCEPT,
                "Accept-Encoding": "gzip, deflate",
                "Connection": "close",
            }
            connection.request("GET", target.path, headers=headers)
            response = connection.getresponse()
            names = {name.lower(): value for name, value in response.getheaders()}
            body, cut = (b"", False) if response.status in REDIRECTS else self._read(response, names)
            if watchdog.fired:
                # 看门的掐断以后，读的那一边看到的常常只是"对方关了连接"（头读完了、正文读完了），不是出错：
                # 那时读到的只是一截，不能当成完整的回答交出去
                raise TimeoutError
            return response.status, names, body, cut
        except (OSError, http.client.HTTPException) as exc:
            if watchdog.fired or isinstance(exc, TimeoutError):
                raise FetchError(f"{target.host} 太久没有回答完（最多等 {self.timeout:g} 秒），没有读到。") from None
            raise FetchError(f"和 {target.host} 的连接出了问题（{type(exc).__name__}: {exc}）。") from None
        finally:
            watchdog.cancel()
            connection.close()

    def _connect(self, target: guard.Target, addresses: list[str], deadline: float, watchdog: _Watchdog) -> socket.socket:
        """连到查过的地址上（一个连不上换下一个），https 的再按域名握手、验证证书。"""
        problem: OSError | None = None
        for address in addresses:
            left = deadline - self.clock()
            if left <= 0:
                break
            try:
                sock = socket.create_connection((address, target.port), timeout=left)
            except OSError as exc:
                problem = exc
                continue
            watchdog.sock = sock
            if target.scheme != "https":
                return sock
            try:
                secure = ssl.create_default_context().wrap_socket(sock, server_hostname=target.host)
            except ssl.SSLCertVerificationError as exc:
                sock.close()
                raise FetchError(f"{target.host} 的证书验证不通过（{exc.verify_message}），没有读。") from None
            except OSError:
                sock.close()
                raise
            watchdog.sock = secure
            return secure
        if problem is None or isinstance(problem, TimeoutError):
            raise TimeoutError
        raise FetchError(f"连不上 {target.host}（{problem}）。检查一下网址，或者这个网站现在打不开。")

    def _read(self, response: http.client.HTTPResponse, headers: dict[str, str]) -> tuple[bytes, bool]:
        """读正文，最多 self.limit 个字节（解压以后的）。read1 每次只收一下，看门的到点掐断时马上停下。"""
        encoding = headers.get("content-encoding", "identity").strip().lower()
        if encoding in ("gzip", "x-gzip"):
            inflater: zlib._Decompress | None = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            inflater = zlib.decompressobj()
        elif encoding in ("", "identity"):
            inflater = None
        else:
            raise FetchError(f"网站用 {encoding} 压缩了内容，读网页解不开它。")
        declared = headers.get("content-length", "")
        if inflater is None and declared.isdigit() and int(declared) > self.limit:
            raise FetchError(f"这个文件太大了（约 {int(declared) / 1_000_000:.1f} MB），读网页最多读 {self.limit / 1_000_000:g} MB。")
        parts: list[bytes] = []
        size = 0
        while size <= self.limit:
            chunk = response.read1(CHUNK)
            if not chunk:
                if inflater is not None:
                    parts.append(inflater.flush()[: self.limit + 1 - size])
                break
            if inflater is not None:
                try:
                    chunk = inflater.decompress(chunk, self.limit + 1 - size)  # 解压炸弹：最多解出还差的那么多
                except zlib.error:
                    raise FetchError("网站发来的压缩内容是坏的，解不开。") from None
            parts.append(chunk)
            size += len(chunk)
        body = b"".join(parts)
        return body[: self.limit], len(body) > self.limit


def _shown(url: str) -> str:
    return urls.shown(url)
