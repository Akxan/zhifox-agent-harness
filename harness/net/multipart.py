"""上传文件用的 multipart/form-data：手写的编码，加上一个"发出去"的帮手（用的还是 http.py 的 request）。

为什么要它：智狐往聊天里发图片、发文件（send_file 工具，见 ../tools/send.py）时，各家的接口都要求用浏览器上传文件的那种
格式（Telegram 的 sendPhoto、Discord 的 files[n]、飞书的 im/v1/images、钉钉的 media/upload）。标准库的 urllib 只会发
现成的字节，不会拼这种请求体，所以在这里拼。格式是 RFC 7578（https://www.rfc-editor.org/rfc/rfc7578）：

    --边界
    Content-Disposition: form-data; name="chat_id"

    12345
    --边界
    Content-Disposition: form-data; name="photo"; filename="screen.png"
    Content-Type: image/png

    （文件的字节）
    --边界--

几个要注意的地方：
  - 边界是随机的一串字母数字，而且确认它没有出现在任何一段内容里：出现了的话，对方会以为文件在那里就结束了
  - 名字和文件名放在引号里，里面的 " 和换行换成 %22、%0D%0A（RFC 7578 第 2 节、浏览器也是这么做的）：
    文件名是模型给的路径里的最后一段，不能让它在请求头里多写出一行来。非英文的文件名按 UTF-8 原样写（浏览器的做法）
  - 拼好的请求体超过 limit 就不发（NetError）：内容整个在内存里，不能无限大
  - 发出去走 http.request：超时、重定向换了域名就去掉密钥、回答的大小上限、出错说明里遮住 hide 里的密钥，和别的请求一样
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from dataclasses import dataclass

from ..config import CHANNEL_SEND_BYTES, CHANNEL_UPLOAD_TIMEOUT
from ..display import file_size
from ..i18n import t
from . import http

# 请求体比文件本身多出来的那一点（各段的头、边界）最多算这么多；上限是"文件的上限加上它"
OVERHEAD_BYTES = 64_000


@dataclass(frozen=True)
class File:
    """请求里的一个文件。field 是表单里的名字（photo、files[0]……），name 是对方看到的文件名。"""

    field: str
    name: str
    media_type: str
    data: bytes


def _quote(value: str) -> str:
    """放进 Content-Disposition 引号里的一个名字：" 和换行换掉，不能让它结束引号、多写出一行请求头。"""
    return value.replace('"', "%22").replace("\r", "%0D").replace("\n", "%0A")  # 和浏览器一样（WHATWG 的 HTML 标准），只换这三个


def _boundary(parts: Iterable[bytes]) -> str:
    """一个没有出现在任何一段内容里的边界。随机的 32 个十六进制字符，撞上的机会几乎为零，撞上了就换一个。"""
    contents = list(parts)
    while True:
        boundary = "zhifox-" + secrets.token_hex(16)
        marker = boundary.encode("ascii")
        if not any(marker in part for part in contents):
            return boundary


def encode(fields: dict[str, str] | Iterable[tuple[str, str]] = (), files: Iterable[File] = ()) -> tuple[bytes, str]:
    """拼一个 multipart/form-data 的请求体。返回（请求体，Content-Type 头的值）。普通的字段在前、文件在后（顺序和给的一样）。"""
    pairs = list(fields.items()) if isinstance(fields, dict) else list(fields)
    files = list(files)
    values = [(name, str(value).encode("utf-8")) for name, value in pairs]
    boundary = _boundary([value for _, value in values] + [item.data for item in files])
    chunks: list[bytes] = []
    for name, value in values:
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{_quote(name)}"\r\n\r\n'.encode())
        chunks.append(value)
        chunks.append(b"\r\n")
    for item in files:
        media_type = item.media_type if item.media_type and "\r" not in item.media_type and "\n" not in item.media_type else "application/octet-stream"
        head = f'--{boundary}\r\nContent-Disposition: form-data; name="{_quote(item.field)}"; filename="{_quote(item.name)}"\r\nContent-Type: {media_type}\r\n\r\n'
        chunks.append(head.encode("utf-8"))
        chunks.append(item.data)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def post(
    url: str,
    *,
    fields: dict[str, str] | Iterable[tuple[str, str]] = (),
    files: Iterable[File] = (),
    headers: dict[str, str] | None = None,
    timeout: float = CHANNEL_UPLOAD_TIMEOUT,
    limit: int = CHANNEL_SEND_BYTES,
    hide: tuple[str, ...] | list[str] = (),
) -> http.Reply:
    """把表单和文件 POST 出去。回答原样交回（不管状态码，见 http.request）。

    limit 是文件加起来最多多大：超了不发，抛出 NetError（说明里写着上限）。连不上、超时也是 NetError，说明里没有 hide 里的密钥。
    """
    files = list(files)
    size = sum(len(item.data) for item in files)
    if size > limit:
        raise http.NetError(t("文件太大了（{size}），最多 {limit}。", size=file_size(size), limit=file_size(limit)))
    body, content_type = encode(fields, files)
    if len(body) > limit + OVERHEAD_BYTES:
        raise http.NetError(t("文件太大了（{size}），最多 {limit}。", size=file_size(len(body)), limit=file_size(limit)))
    return http.request("POST", url, headers={**(headers or {}), "Content-Type": content_type}, content=body, timeout=timeout, hide=hide)
