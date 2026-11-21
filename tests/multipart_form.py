"""假服务器（fake_telegram.py……）用的：读出一个 multipart/form-data 的请求体。

智狐往聊天里发文件时用的就是这种格式（见 harness/net/multipart.py）。这里照着 RFC 7578 一段一段拆开，
不用标准库的 email 解析器：它会把非英文的文件名按 ASCII 读坏，而真的平台（和浏览器一样）认的是 UTF-8 原样写的文件名。
"""

from __future__ import annotations

import re

_PARAM = re.compile(r'(\w+)="([^"]*)"')


def is_multipart(content_type: str | None) -> bool:
    return (content_type or "").lower().startswith("multipart/form-data")


def parse(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, dict]]:
    """拆开一个请求体。返回（普通的字段 {名字: 值}，文件 {名字: {"filename", "content_type", "data"}}）。格式不对抛出 ValueError。"""
    match = re.search(r"boundary=\"?([^\";]+)\"?", content_type)
    if match is None:
        raise ValueError("没有 boundary")
    marker = b"--" + match.group(1).encode("ascii")
    if not body.startswith(marker) or not body.rstrip(b"\r\n").endswith(marker + b"--"):
        raise ValueError("请求体的开头或结尾不对")
    fields: dict[str, str] = {}
    files: dict[str, dict] = {}
    for chunk in body.split(marker)[1:]:
        if chunk.startswith(b"--"):
            break
        if not chunk.startswith(b"\r\n") or not chunk.endswith(b"\r\n"):
            raise ValueError("一段的前后没有换行")
        head, separator, data = chunk[2:-2].partition(b"\r\n\r\n")
        if not separator:
            raise ValueError("一段没有头")
        headers = {}
        for line in head.decode("utf-8").split("\r\n"):
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        params = dict(_PARAM.findall(headers.get("content-disposition", "")))
        if "name" not in params:
            raise ValueError("一段没有名字")
        if "filename" in params:
            files[params["name"]] = {"filename": params["filename"], "content_type": headers.get("content-type", ""), "data": data}
        else:
            fields[params["name"]] = data.decode("utf-8")
    return fields, files
