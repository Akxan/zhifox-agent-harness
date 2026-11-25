"""网址：模型给的地址怎么理解，哪些根本不能打开，两个地址算不算"同一个网站"。

规矩（为什么这样定，见 permissions.py 开头的表）：

  - "back"、"forward"、"reload" 是后退、前进、刷新，不是网址。
  - 没写协议的（"example.com/docs"）当作 https://。
  - file:（你电脑上的文件）、chrome: 和 devtools:（浏览器自己的设置页和调试工具，能改浏览器的行为）、
    javascript:（在页面里执行一段代码）、view-source: 这几种一律不打开，不问你。
  - 别的非 http(s) 地址（about:、data:……）每次都问。
  - http(s) 的地址按"网站"算：去一个这段对话里还没允许过的网站，要问你（点链接到过的不算允许过）。
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from ..i18n import zh

HISTORY = {"back", "forward", "reload"}
# 一律不打开的协议。看的是解析出来的协议，不是字符串开头：大小写、前后空格、"jAvAsCrIpT:" 都一样拦得住
REFUSED_SCHEMES = {"file", "chrome", "chrome-extension", "chrome-untrusted", "devtools", "javascript", "view-source", "filesystem"}
WEB_SCHEMES = {"http", "https"}
MAX_URL_CHARS = 8000


def normalize(url: object) -> str:
    """模型给的地址整理成要打开的样子。history 关键字原样返回。"""
    text = str(url or "").strip()
    if text.lower() in HISTORY:
        return text.lower()
    if not text:
        return ""
    scheme = urlsplit(text).scheme.lower()
    # "localhost:8000"、"example.com:443/x" 会被解析成"协议是 localhost"：像主机名加端口的，当作没写协议
    looks_like_host = bool(scheme) and text[len(scheme) + 1 : len(scheme) + 2].isdigit()
    if not scheme or looks_like_host:
        return "https://" + text.lstrip("/")
    return text


def scheme_of(url: str) -> str:
    return urlsplit(url).scheme.lower()


def refusal(url: str) -> str | None:
    """这个地址根本不打开的话，返回给模型看的原因；可以打开（也许要先问用户）返回 None。
    原因用 zh() 写：模型拿到中文原文，工具卡片上按界面的语言显示（见 ../i18n.py 开头）。"""
    if len(url) > MAX_URL_CHARS:
        return zh("这个地址太长了（{n} 个字符），没有打开。", n=len(url))
    scheme = scheme_of(url)
    if scheme in REFUSED_SCHEMES:
        return zh(
            "不能打开 {scheme}: 开头的地址。智狐的浏览器只用来看网页：本地文件、浏览器自己的设置页和调试工具、"
            "直接执行代码的地址都不打开。需要看本地文件的话，用读文件的工具。",
            scheme=scheme,
        )
    if scheme in WEB_SCHEMES:
        parts = urlsplit(url)
        if not parts.hostname:
            return zh("这个地址里没有网站的名字，没法打开。")
        try:
            parts.port  # noqa: B018 —— 端口写错了（不是数、超出范围）的话，这里会抛出 ValueError
        except ValueError:
            return zh("这个地址里的端口号不对，没法打开。")
    return None


def site_of(url: str) -> str:
    """一个地址属于哪个"网站"：协议加主机名，去掉开头的 www.（www.example.com 和 example.com 算同一个）。

    不是 http(s) 的地址没有网站，返回空字符串。子域名算不同的网站（docs.example.com 和 example.com 不一样）：
    宁可多问一次，也不要把一个公司的所有子域名都当成一家。
    """
    parts = urlsplit(url)
    if parts.scheme.lower() not in WEB_SCHEMES or not parts.hostname:
        return ""
    host = parts.hostname.lower().rstrip(".")
    host = host.removeprefix("www.")
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:  # 端口写错了：这个地址根本打不开（见 refusal），也就不算哪个网站
        return ""
    return f"{parts.scheme.lower()}://{host}{port}"


def is_local(url: str) -> bool:
    """是不是这台电脑或者局域网里的地址（localhost、127.0.0.1、192.168.x.x……）。确认卡片上会特别说一句。"""
    host = (urlsplit(url).hostname or "").lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def shown(url: str, limit: int = 300) -> str:
    """给人看、给模型看的地址：去掉控制字符，太长的截断。"""
    text = "".join(char for char in str(url) if char.isprintable())
    return text if len(text) <= limit else text[: limit - 1] + "…"
