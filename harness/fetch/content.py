"""取回来的东西 → 交给模型的文字。按回答说的类型（没说、说的是 octet-stream 的，看开头几个字节猜）交给不同的读法：

  网页（text/html、application/xhtml+xml）  readable.py 取出标题、简介、正文
  纯文本（text/*）、JSON、XML、YAML 这些     原样（按 charset.py 解码）
  Word、Excel、PowerPoint                     documents.py（和附件同一套读法）；太大了只下载了一截的不读：压缩包缺了尾巴打不开
  PDF、图片、音频、视频、压缩包……            不读，说清楚为什么、可以怎么办

说明都写给模型看（中文）。
"""

from __future__ import annotations

import codecs
import posixpath
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from .. import documents
from ..media import OFFICE_TYPES
from . import charset, readable
from .client import Fetched

HTML_TYPES = {"text/html", "application/xhtml+xml"}
# text/* 之外，也是文字的那些
TEXT_TYPES = {
    "application/json", "application/xml", "application/javascript", "application/x-javascript", "application/ecmascript",
    "application/x-ndjson", "application/yaml", "application/x-yaml", "application/toml", "application/x-sh", "application/sql",
    "application/graphql", "application/csv",
}  # fmt: skip
_OFFICE = {media_type: suffix for suffix, media_type in OFFICE_TYPES.items()}
_UNKNOWN = {"", "application/octet-stream", "binary/octet-stream", "application/unknown"}


class Unreadable(Exception):
    """取回来了，但读不了（PDF、图片……）。说明是写给模型看的。"""


@dataclass(frozen=True)
class Readable:
    kind: str  # html / text / office
    media_type: str
    title: str
    description: str
    text: str


def extract(fetched: Fetched) -> Readable:
    header = fetched.headers.get("content-type", "")
    media_type = fetched.content_type
    if media_type in _UNKNOWN:
        media_type = _sniff(fetched.body, fetched.url)
    if media_type in HTML_TYPES:
        page = readable.read(charset.decode(fetched.body, header, markup=True, cut=fetched.cut), fetched.url)
        return Readable("html", media_type, page.title, page.description, page.text)
    if media_type.startswith("text/") or media_type in TEXT_TYPES or media_type.endswith(("+json", "+xml")):
        markup = media_type.endswith("xml")
        return Readable("text", media_type, "", "", charset.decode(fetched.body, header, markup=markup, cut=fetched.cut).strip())
    if media_type in _OFFICE or (media_type == "application/zip" and _office_suffix(fetched.url)):
        return _office(fetched, media_type)
    if media_type == "application/pdf":
        raise Unreadable("这是一个 PDF 文件。读网页取不出 PDF 里的文字。可以请用户把它下载下来，作为附件发给你（能读 PDF 的模型可以直接读）。")
    if media_type.startswith(("image/", "audio/", "video/", "font/")):
        raise Unreadable(f"这是一个{_KINDS.get(media_type.split('/', 1)[0], '文件')}（{media_type}），不是网页，读网页不读它。")
    raise Unreadable(f"这不是网页，是 {media_type or '看不出类型'} 的文件，读网页不读它（只读网页、纯文本、JSON 和 Word、Excel、PowerPoint）。")


_KINDS = {"image": "图片", "audio": "音频", "video": "视频", "font": "字体文件"}


def _office(fetched: Fetched, media_type: str) -> Readable:
    if fetched.cut:
        raise Unreadable("这个文件太大了，只下载了前面一截，Word、Excel、PowerPoint 文件缺了后面就打不开。可以请用户下载下来作为附件发给你。")
    name = _file_name(fetched.url) or "download." + _OFFICE.get(media_type, "docx")
    try:
        text = documents.extract_text(name, fetched.body)
    except documents.DocumentError as exc:
        raise Unreadable(str(exc)) from None
    return Readable("office", media_type, name, documents.NOTE, text)


def _sniff(body: bytes, url: str) -> str:
    """回答没说类型（或者说的是 octet-stream）：看开头几个字节猜。猜不出的当作不认识的文件。"""
    head = body[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        return "text/html"
    if head.startswith(b"%pdf-"):
        return "application/pdf"
    if body.startswith(b"PK\x03\x04"):
        suffix = _office_suffix(url)
        return OFFICE_TYPES[suffix] if suffix else "application/zip"
    if b"\x00" not in body[:4096]:
        try:
            # 只看了开头一截，最后半个字不算错（见 charset.decode 的 cut）
            codecs.getincrementaldecoder("utf-8")().decode(body[:4096], final=False)
        except UnicodeDecodeError:
            return "application/octet-stream"
        return "text/plain"
    return "application/octet-stream"


def _file_name(url: str) -> str:
    try:
        return posixpath.basename(unquote(urlsplit(url).path))[:200]
    except ValueError:
        return ""


def _office_suffix(url: str) -> str:
    suffix = posixpath.splitext(_file_name(url))[1].lower().lstrip(".")
    return suffix if suffix in OFFICE_TYPES else ""
