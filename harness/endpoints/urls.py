"""自定义接口的地址：哪些写法能用，明文的 http:// 只许留在你自己的网络里。

为什么要管：对话的全文、附件、工具的结果都会发到这个地址去，填了密钥的话密钥也会发过去。走 https:// 的话
一路上是加密的；走 http:// 就是明文，经过的每一台路由器、公共 Wi-Fi 都看得到。本机（localhost）和内网
（家里、公司的局域网，Tailscale 这类虚拟内网）里的服务器——Ollama、LM Studio、vLLM 默认都只开 http——
数据不出你的网络，明文也可以接受；别的地方一律要 https://。

怎么判断"在你自己的网络里"：只看地址本身，不去查 DNS。查 DNS 的话，检查时和真正连接时查到的可能不一样
（DNS 重绑定），离线时也查不了；只看写法，保存时、读回来时、连接之前，三处判断的结果永远一样。认的是：

  - localhost 和 *.localhost（RFC 6761 规定只能指向本机）
  - 本机、内网的 IP：127.0.0.0/8、::1、10/8、172.16/12、192.168/16、100.64/10（运营商级 NAT，Tailscale 用的就是它）、
    fc00::/7（IPv6 的内网地址）；还有 0.0.0.0（Ollama 的文档里常这样写，连过去就是本机）
  - 只能在局域网里解析的名字：*.local（mDNS，比如 mac-studio.local）、*.internal、*.home.arpa（这两个是专门留给内网的），
    *.lan、*.corp、*.home（不会被分配出去的常用写法），以及没有点的单个名字（gpu-box，只有内网的 DNS 认得）

公司的网关如果用的是真实的域名（llm.example.com），哪怕它解析到内网地址，也要 https://：这种网关一般都有证书；
真没有的话，直接填它的 IP。链路本地地址（169.254/16、fe80::/10）不算内网：云服务器上 169.254.169.254 是元数据服务，
把对话发过去没有任何用处。

其他要求：地址里不能带用户名和密码（https://user:pass@host 会把密码明文写进设置文件、日志和出错信息；密钥请填在密钥一栏），
不能带 ? 后面的参数和 # 后面的片段（OpenAI 的 SDK 拼地址时会把它们弄乱）。末尾的 / 去掉，其余原样保留。
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..i18n import Message, t

MAX_URL_CHARS = 500

# 本机和内网的 IP 段（见开头的说明）
_PRIVATE = [
    ipaddress.ip_network(net)
    for net in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "0.0.0.0/32", "::1/128", "fc00::/7", "::/128")
]
_LOOPBACK = [ipaddress.ip_network(net) for net in ("127.0.0.0/8", "::1/128", "0.0.0.0/32", "::/128")]
# 只在局域网里才解析得到的名字的后缀
_LAN_SUFFIXES = (".local", ".internal", ".home.arpa", ".lan", ".corp", ".home")

# 地址在哪儿：本机 / 内网 / 外面（要 https）
HERE, LAN, REMOTE = "local", "lan", "remote"


class UrlError(ValueError):
    """地址不能用。说明是一句译好的话（Message），可以直接给人看。"""

    def __init__(self, message: Message):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Checked:
    url: str  # 整理好的地址（去掉了末尾的 /）
    where: str  # HERE / LAN / REMOTE
    origin: str  # 协议、主机和端口（http://localhost:11434）：换了它，旧的密钥不跟着过去（见 store.py）

    @property
    def local(self) -> bool:
        """数据留在你自己的网络里（本机或内网）。"""
        return self.where != REMOTE


def where_is(host: str) -> str:
    """一个主机名在哪儿：HERE（这台电脑）、LAN（你的内网）、REMOTE（外面）。只看写法，不查 DNS。"""
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return HERE
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])  # fe80::1%en0 这种带网卡名的写法
    except ValueError:
        if "." not in host or host.endswith(_LAN_SUFFIXES):
            return LAN
        return REMOTE
    mapped = getattr(address, "ipv4_mapped", None)  # ::ffff:10.0.0.1 按里面的 IPv4 算
    if mapped is not None:
        address = mapped
    if any(address in net for net in _LOOPBACK if net.version == address.version):
        return HERE
    if any(address in net for net in _PRIVATE if net.version == address.version):
        return LAN
    return REMOTE


def check(url: object) -> Checked:
    """检查一个接口地址。能用的话返回整理好的样子，不能用抛出 UrlError（说明为什么、怎么改）。"""
    if not isinstance(url, str) or not url.strip():
        raise UrlError(t("请填接口地址，比如 http://localhost:11434/v1。"))
    text = url.strip()
    if len(text) > MAX_URL_CHARS:
        raise UrlError(t("接口地址太长了（最多 {n} 个字符）。", n=MAX_URL_CHARS))
    if any(char.isspace() or ord(char) < 32 for char in text):
        raise UrlError(t("接口地址里不能有空格或者换行。"))
    try:
        parts = urlsplit(text)
        port = parts.port  # 端口写错了（不是数字、超过 65535）在这里抛出 ValueError
    except ValueError:
        raise UrlError(t("这不是一个能用的地址：{url}", url=text)) from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise UrlError(t("接口地址要以 https:// 开头（本机和内网的服务器也可以用 http://）。"))
    host = parts.hostname or ""
    if not host:
        raise UrlError(t("接口地址里没有服务器的名字：{url}", url=text))
    if parts.username is not None or parts.password is not None:
        raise UrlError(t("接口地址里不能带用户名和密码。密钥请填在下面的“API 密钥”里。"))
    if parts.query or parts.fragment or text.endswith(("?", "#")):
        raise UrlError(t("接口地址里不能有 ? 或 # 后面的部分。"))
    where = where_is(host)
    if scheme == "http" and where == REMOTE:
        raise UrlError(
            t(
                "{host} 不在这台电脑或者你的内网里，要用 https://。明文的 http:// 会让对话和密钥在网上裸奔：只有本机（localhost）、内网的地址（192.168.x.x、10.x.x.x、*.local……）才能用它。",
                host=host,
            )
        )
    shown_host = f"[{host}]" if ":" in host else host  # IPv6 的地址要加方括号
    origin = f"{scheme}://{shown_host}" + (f":{port}" if port is not None else "")
    path = parts.path.rstrip("/")
    return Checked(url=origin + path, where=where, origin=origin)


def root_of(url: str) -> str:
    """接口地址去掉末尾的 /v1：Ollama 的 /api/show、LM Studio 的 /api/v0/models 在这一层下面（见 probe.py）。"""
    return url.removesuffix("/v1")
