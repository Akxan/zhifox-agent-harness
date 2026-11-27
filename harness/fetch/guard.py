"""读网页能去哪儿：网址怎么理解，哪些 IP 地址一律不去（规矩和理由见 fetch/__init__.py）。

这里只看"字"：网址的写法、一个 IP 地址属于哪一段。解析域名、真的连上去在 client.py，它对解析出来的每一个地址都问一遍 refusal。

为什么自己列一张表，而不用 ipaddress 自带的 is_private、is_global：那几个的结果在 Python 的版本之间变过
（3.11 和 3.13 对 100.64/10、组播、64:ff9b::/96 的说法不一样），而这是一道安全的关卡，哪个版本上都要一模一样。
IPv4 是"列出不能去的"；IPv6 反过来，只认公网单播的 2000::/3，再从里面去掉几段特殊用途的：IPv6 里特殊的段太多，
列不全的话，宁可不去。

给模型看的原因写成中文（写给模型看的不进翻译表）。
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

from ..browser import urls

SCHEMES = ("http", "https")
DEFAULT_PORTS = {"http": 80, "https": 443}

LOOPBACK = "这台电脑自己"
PRIVATE = "局域网里的地址"
LINK_LOCAL = "链路本地地址（云服务器的元数据接口就在这里）"
SHARED = "运营商级 NAT 的共享地址（有的云服务器的元数据接口在这里）"
MULTICAST = "组播地址"
RESERVED = "保留的、不在公网上的地址"
UNSPECIFIED = "“未指定”的地址"
SPECIAL = "特殊用途的地址（文档、测试、协议用的）"

# 不能去的 IPv4 段。依据是 IANA 的特殊用途地址表（https://www.iana.org/assignments/iana-ipv4-special-registry/）
BLOCKED_V4 = tuple(
    (ipaddress.IPv4Network(network), why)
    for network, why in (
        ("0.0.0.0/8", UNSPECIFIED),  # "这个网络"，0.0.0.0 在很多系统上就是连本机
        ("10.0.0.0/8", PRIVATE),
        ("100.64.0.0/10", SHARED),  # 阿里云的元数据 100.100.100.200 在这里
        ("127.0.0.0/8", LOOPBACK),
        ("169.254.0.0/16", LINK_LOCAL),  # AWS、GCP、Azure 的元数据 169.254.169.254 在这里
        ("172.16.0.0/12", PRIVATE),
        ("192.0.0.0/24", SPECIAL),  # 协议用的，Oracle 云的元数据 192.0.0.192 在这里
        ("192.0.2.0/24", SPECIAL),
        ("192.88.99.0/24", SPECIAL),  # 已经废弃的 6to4 中继
        ("192.168.0.0/16", PRIVATE),
        ("198.18.0.0/15", SPECIAL),  # 网络设备的测试
        ("198.51.100.0/24", SPECIAL),
        ("203.0.113.0/24", SPECIAL),
        ("224.0.0.0/4", MULTICAST),
        ("240.0.0.0/4", RESERVED),  # 包括 255.255.255.255
    )
)

# IPv6 只认公网单播（2000::/3）。::1、::、fe80::/10、fc00::/7（AWS 的元数据 fd00:ec2::254 在这里）、ff00::/8 组播……都在它外面
GLOBAL_V6 = ipaddress.IPv6Network("2000::/3")
# 2000::/3 里面也不能去的
BLOCKED_V6 = tuple(
    (ipaddress.IPv6Network(network), why)
    for network, why in (
        ("2001::/23", SPECIAL),  # 协议用的，包括 Teredo（2001::/32，里面藏着的 IPv4 是打乱过的，没法按它判断）
        ("2001:db8::/32", SPECIAL),  # 文档里的例子
        ("3fff::/20", SPECIAL),  # 文档里的例子（2024 年新加的）
    )
)
# 藏着一个 IPv4 的几种 IPv6 写法：按里面那个 IPv4 判断（::ffff:127.0.0.1 就是 127.0.0.1）
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
_V4_COMPATIBLE = ipaddress.IPv6Network("::/96")  # 早就废弃的"IPv4 兼容"写法 ::a.b.c.d，:: 和 ::1 也在这里

# 主机名的最后一段是数（或者 0x 开头的十六进制）：这是 IP 地址的某种写法。只认标准的 a.b.c.d，
# 别的（2130706433、0x7f.1、127.1、0177.0.0.1）系统的解析函数会认成 127.0.0.1，干脆不读
_NUMERIC_LABEL = re.compile(r"(?:0x[0-9a-f]*|[0-9]+)", re.IGNORECASE)
# 换成 ASCII 以后的域名只能有这些字符（下划线不合规矩，但真有网站这么起名）
_HOST = re.compile(r"[a-z0-9_.\-]+", re.IGNORECASE)
# 路径和 ? 后面那一截里原样留着的字符（别的，比如空格、汉字，按 UTF-8 写成 %XX；已经写成 %XX 的不再写一遍）
_PATH_SAFE = "/%:@!$&'()*+,;=-._~"
_QUERY_SAFE = _PATH_SAFE + "?"


class Refused(ValueError):
    """这个网址不读。说明是写给模型看的。"""


def refusal(address: str) -> str | None:
    """一个 IP 地址能不能去：不能去返回原因，能去返回 None。看不懂的一律不去。"""
    try:
        # IPv6 的链路本地地址可能带着 %en0 这样的网卡名：ipaddress 自己认得（还是按 IPv6 判断）。不能先按 % 切开只看前半截：
        # "8.8.8.8%x.attacker.test" 不是 IP 地址，系统会把整串当成域名去解析（见 parse）
        ip = ipaddress.ip_address(address)
    except ValueError:
        return RESERVED
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_v4(ip)
        if embedded is not None:
            return _refusal_v4(embedded)
        if ip not in GLOBAL_V6:
            return _why_v6(ip)
        return next((why for network, why in BLOCKED_V6 if ip in network), None)
    return _refusal_v4(ip)


def _refusal_v4(ip: ipaddress.IPv4Address) -> str | None:
    return next((why for network, why in BLOCKED_V4 if ip in network), None)


def _embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip in _NAT64 or (ip in _V4_COMPATIBLE and int(ip) > 1):  # :: 和 ::1 不算（它们是"未指定"和这台电脑自己）
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def _why_v6(ip: ipaddress.IPv6Address) -> str:
    """2000::/3 之外的 IPv6 地址，给模型看的原因说得具体一点（不能去是一定的）。"""
    if ip.is_loopback:
        return LOOPBACK
    if ip.is_unspecified:
        return UNSPECIFIED
    if ip.is_link_local:
        return LINK_LOCAL
    if ip.is_multicast:
        return MULTICAST
    if ip in ipaddress.IPv6Network("fc00::/7") or ip.is_site_local:
        return PRIVATE
    return RESERVED


@dataclass(frozen=True)
class Target:
    """整理好的一个网址：要连到哪儿、请求行里写什么。"""

    url: str  # 完整的网址（# 后面的部分去掉了，它不发给网站）
    scheme: str  # http / https
    host: str  # 解析、证书、Host 头用的名字：汉字域名已经换成 xn-- 的写法，IPv6 不带方括号
    port: int
    path: str  # 请求行里的那一截：路径和 ? 后面的
    literal: bool  # 主机本身就是一个 IP 地址（不用解析）

    @property
    def host_header(self) -> str:
        name = f"[{self.host}]" if ":" in self.host else self.host
        return name if self.port == DEFAULT_PORTS[self.scheme] else f"{name}:{self.port}"


def normalize(url: object) -> str:
    """模型给的网址整理成要读的样子：前后的空白去掉，没写协议的当作 https://（和浏览器一样，见 browser/urls.py）。
    urlsplit 自己就拒绝的写法（方括号里不是 IP 地址的……）原样交回，由 parse 说清楚为什么不读：在这里抛出去，整轮就断了。"""
    try:
        return urls.normalize(url)
    except ValueError:
        return str(url or "").strip()


def parse(url: str) -> Target:
    """检查一个（整理过的）网址，交回要连的地方。不读的抛出 Refused，说明为什么。不解析域名。"""
    if len(url) > urls.MAX_URL_CHARS:
        raise Refused(f"这个网址太长了（{len(url)} 个字符），没有读。")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise Refused("这个网址的写法不对（主机名或者端口号看不懂），没有读。") from None
    scheme = parts.scheme.lower()
    if scheme not in SCHEMES:
        raise Refused("读网页只读 http:// 和 https:// 开头的网址。本地文件用读文件的工具；别的地址可以试试浏览器。")
    if "@" in parts.netloc:
        raise Refused("网址里带着用户名或者密码（@ 前面的那一段），没有读：读网页不替任何网站登录。")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise Refused("这个网址里没有网站的名字，没法读。")
    if "%" in host:
        # % 后面的一段是 IPv6 的网卡名（[fe80::1%25en0]），只对链路本地地址有意义，那些本来就不去。
        # 别的写法更不能放过："8.8.8.8%x.attacker.test" 看着像公网的 8.8.8.8，连的时候系统却把整串当成域名再解析一遍，
        # 对方的 DNS 答 127.0.0.1 就连到这台电脑上了（查的和连的不是同一个）
        raise Refused(f"网站的名字“{host}”里有 %，没有读。")
    literal = is_ip(host)
    if not literal:
        shown = host
        try:
            # 汉字域名换成 xn-- 的写法：解析、证书、Host 头都用它。全角的数字、中文的句号（。）也在这一步换成 ASCII，
            # 所以下面几样检查都在换过以后做（"１２７。０。０。１"换完就是 127.0.0.1）
            host = host.encode("idna").decode("ascii").rstrip(".")
        except UnicodeError:
            raise Refused(f"网站的名字“{shown}”看不懂，没有读。") from None
        literal = is_ip(host)
        if not literal and _NUMERIC_LABEL.fullmatch(host.rsplit(".", 1)[-1]):
            raise Refused(f"“{shown}”是 IP 地址的一种不常见的写法，没有读。请写成 1.2.3.4 这样的标准写法。")
        if not literal and not _HOST.fullmatch(host):
            raise Refused(f"网站的名字“{shown}”里有不该有的字符，没有读。")
    port = port or DEFAULT_PORTS[scheme]
    path = quote(parts.path or "/", safe=_PATH_SAFE)
    query = quote(parts.query, safe=_QUERY_SAFE) if parts.query else ""
    netloc = (f"[{host}]" if ":" in host else host) + ("" if port == DEFAULT_PORTS[scheme] else f":{port}")
    return Target(
        url=urlunsplit((scheme, netloc, path, query, "")),
        scheme=scheme,
        host=host,
        port=port,
        path=path + (f"?{query}" if query else ""),
        literal=literal,
    )


def is_ip(host: str) -> bool:
    """host 是不是一个照标准写法写的 IP 地址。client.py 只把这样的交给 socket：别的写法 socket 会自己再解析一遍。"""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def site_of(url: str) -> str:
    """一个网址属于哪个"网站"（问不问你按它算）：和浏览器同一个算法（见 browser/urls.py 的 site_of）。"""
    return urls.site_of(url)
