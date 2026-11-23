"""一个很小的 HTTPS 帮手：Discord、Slack、QQ、飞书、钉钉的接口都是"发一个 JSON 过去，收一个 JSON 回来"。

只用标准库的 urllib（和 Telegram 的客户端一样，它会照着 HTTPS_PROXY 走代理）。这里多做了几件事，平台的代码不用各写一遍：

  - 请求体用 safe.to_json 编码：模型写的文字里可能有半个表情，直接 json.dumps 会写出对方解析不了的东西
  - 回答不管是 200 还是 4xx，都原样交回去（status、解析好的 JSON、头），由平台的代码判断是什么意思：
    各家报错的方式都不一样（Slack 是 200 加 {"ok": false}，Discord 是 4xx 加 {"code", "message"}）
  - 连不上、超时、回答太大，抛出 NetError。说明里先把 hide 里的每一个密钥换成 <hidden>，可以直接给人看
  - 重定向到另一个域名时，只留下 User-Agent、Accept 这几个头：机器人的 token 不跟着跳到别人的服务器上
    （Slack 的文件下载会重定向）。token 不一定放在 Authorization 里（钉钉放在 x-acs-dingtalk-access-token），
    所以是"只留下认识的几个"，而不是"去掉认识的几个"。重定向到 trusted() 不认的地址（明文的 http://、ftp://）不跟
  - 回答最多读 limit 个字节，超了就不读了：一个出了毛病的服务器不能把内存撑满
  - 上传文件（content 是已经拼好的字节）也走这里：请求体怎么拼见 multipart.py
"""

from __future__ import annotations

import http.client
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from ..config import CHANNEL_TIMEOUT
from ..i18n import t
from ..log import HIDDEN
from ..safe import to_json
from .websocket import is_loopback

MAX_JSON_BYTES = 4_000_000  # 一个 JSON 回答最多这么大
USER_AGENT = "ZhiFox (https://github.com/Akxan/zhifox-agent-harness, 0.1)"


class NetError(Exception):
    """连不上、超时、回答太大或者看不懂。说明可以直接给人看，里面没有密钥。

    status 和 channels.base.ChannelError 的一样：0 是根本没得到回答（连不上、超时、连接被重置，过一会儿再试多半会好），
    None 是得到了、但用不了（太大、文件超过上限、网址写得不对：再试也一样）。平台的代码把它原样交给自己的错误。
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Reply:
    status: int  # HTTP 状态码
    data: Any  # 解析好的 JSON；回答不是 JSON 时是 None
    headers: dict[str, str] = field(default_factory=dict)  # 名字都是小写
    text: str = ""  # 回答正文的开头（看不懂的回答，出错时给人看）

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def retry_after(self) -> float:
        """被限流时（429）对方说的"过多久再来"（秒）。没说是 0。"""
        try:
            seconds = float(self.headers.get("retry-after", "0"))
        except ValueError:  # 也可能写的是一个日期：当作没说
            return 0.0
        # "inf"、"nan" 也能被 float 读出来：一个出了毛病的服务器不能让我们永远睡下去
        return seconds if math.isfinite(seconds) and seconds > 0 else 0.0


def scrub(text: str, hide: tuple[str, ...] | list[str] = ()) -> str:
    """把 text 里出现的每一个密钥换成 <hidden>。"""
    for secret in sorted((item for item in hide if item and len(item) >= 4), key=len, reverse=True):
        text = text.replace(secret, HIDDEN)
    return text


# 换了域名以后还能带着的请求头（urllib 存的名字是首字母大写、其余小写的写法）。别的一律去掉
_PORTABLE_HEADERS = {"User-agent", "Accept", "Accept-language"}


class _KeepSecretsHome(urllib.request.HTTPRedirectHandler):
    """跟着重定向走，但是换了域名就只留下 _PORTABLE_HEADERS；要跳到 trusted() 不认的地址就不跟了。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not trusted(newurl):
            return None  # 不跟：urllib 会把这个 3xx 当成一个出错的回答交回来
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            for name in [*new.headers, *new.unredirected_hdrs]:
                if name.capitalize() not in _PORTABLE_HEADERS:
                    new.remove_header(name)
        return new


_OPENER = urllib.request.build_opener(_KeepSecretsHome)


def trusted(url: str) -> bool:
    """平台给的下载地址能不能去下：只认 https://（明文的 http:// 只认这台电脑自己，测试里的假服务器用）。"""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:  # 比如 http://[::1 这样括号没配对的
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and is_loopback(parts.hostname or ""))


def _host(url: str) -> str:
    """出错说明里写的"连不上谁"：只写主机名。网址的其余部分可能有密钥（Telegram 的 token 就在路径里）。"""
    try:
        return urllib.parse.urlsplit(url).hostname or "?"
    except ValueError:
        return "?"


# 连不上、超时（URLError、TimeoutError 都是 OSError）；网址写得不对（ValueError）；
# 对方回的根本不是 HTTP（http.client 的 BadStatusLine、LineTooLong 这些既不是 OSError 也不会被 urllib 包一层）
_FAILURES = (OSError, ValueError, http.client.HTTPException)


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: Any = None,
    form: dict[str, str] | None = None,
    content: bytes | None = None,
    timeout: float = CHANNEL_TIMEOUT,
    limit: int = MAX_JSON_BYTES,
    hide: tuple[str, ...] | list[str] = (),
) -> Reply:
    """发一个请求。body 是要编码成 JSON 的东西，form 是表单，content 是已经编码好的字节（上传文件用，Content-Type 由调用的人
    写在 headers 里，见 multipart.py）；三者最多给一个。回答原样交回（不管状态码）。"""
    data: bytes | None = None
    sent = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
    if content is not None:
        data = content
        sent.setdefault("Content-Type", "application/octet-stream")
    elif body is not None:
        data = to_json(body).encode("utf-8")
        sent.setdefault("Content-Type", "application/json; charset=utf-8")
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        sent.setdefault("Content-Type", "application/x-www-form-urlencoded")
    try:
        prepared = urllib.request.Request(url, data=data, headers=sent, method=method)  # 网址不对在这里就会抛 ValueError
        with _OPENER.open(prepared, timeout=timeout) as response:
            status, reply_headers, raw = response.status, response.headers, response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        status, reply_headers = exc.code, exc.headers
        try:
            raw = exc.read(limit + 1)
        except _FAILURES:
            raw = b""
    except _FAILURES as exc:
        reason = getattr(exc, "reason", None) or exc
        # 网址写得不对（ValueError）再试也一样；别的（连不上、超时、对方回的不是 HTTP）都算没得到回答
        raise NetError(t("连不上 {host}（{error}）", host=_host(url), error=scrub(str(reason), hide) or type(exc).__name__), None if isinstance(exc, ValueError) else 0) from None
    if len(raw) > limit:
        raise NetError(t("{host} 的回答太大了。", host=_host(url)))
    try:
        parsed = json.loads(raw) if raw.strip() else None
    except ValueError:
        parsed = None
    names = {name.lower(): value for name, value in (reply_headers.items() if reply_headers else [])}
    return Reply(status, parsed, names, scrub(raw[:300].decode("utf-8", "replace"), hide))


def download(url: str, *, headers: dict[str, str] | None = None, limit: int, timeout: float = CHANNEL_TIMEOUT * 3, hide: tuple[str, ...] | list[str] = ()) -> bytes:
    """下载一个文件，最多 limit 个字节。超了、状态码不是 2xx、连不上，都抛出 NetError。"""
    try:
        prepared = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})}, method="GET")
        with _OPENER.open(prepared, timeout=timeout) as response:
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > limit:
                raise NetError(t("这个文件太大了，最多 {size} MB。", size=limit // 1_000_000))
            content = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        raise NetError(t("下载文件失败（HTTP {status}）", status=exc.code), exc.code) from None
    except _FAILURES as exc:
        reason = getattr(exc, "reason", None) or exc
        raise NetError(t("下载文件失败（{error}）", error=scrub(str(reason), hide) or type(exc).__name__), None if isinstance(exc, ValueError) else 0) from None
    if len(content) > limit:
        raise NetError(t("这个文件太大了，最多 {size} MB。", size=limit // 1_000_000))
    return content
