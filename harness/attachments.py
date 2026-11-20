"""附件：用户随消息一起发来的图片、PDF 和文本文件。

网页上可以粘贴截图、选择或拖进文件。浏览器把文件读出来，图片、PDF 和 Office 文件编码成 base64，
文本文件直接给文字，和消息一起发过来。Word、Excel、PowerPoint 文件在这里取出文字（见 documents.py），
之后就和文本文件一样，哪一家模型都能读。这里做三件事：

  检查     浏览器送来的东西不能全信（页面有 bug、文件改过扩展名），所以每一项都在这里查一遍：
           个数、大小、格式、编码。不合格的整条消息退回去，并说明是哪个文件、哪里不行。
  转换     检查过的附件变成 Attachment。它交给模型的适配层（providers/），由那边按各家的格式放进消息里；
           适配层只用到这里的 Attachment 和 wrap_text，不用自己再检查。
  概要     界面上显示附件不需要它的全部内容，只要名字、大小和一张小预览图（summary）。
           概要会写进事件和保存的会话里；完整的内容只在对话历史（messages）里，搜索和页面都碰不到。
           发出去之前，原件另外存一份到多媒体库（见 media.py），概要里记着它的编号（sha256），
           以后在"多媒体"里能看、能下载，Word、Excel、PowerPoint 的原件也不会丢。

为什么大小卡在这几个数：
  - 图片：Claude 的 API 规定每张图片的 base64 不超过 10 MB。base64 是"每 3 个字节写成 4 个字符"，
    所以原始文件最多 7.5 MB。
  - PDF：Claude 一次请求最多 128 MB，并且对话里的每个 PDF 每次请求都要整份重发，25 MB 已经很大了。
  - 文本：40 万个字符大约是 10 万到 40 万个 token，再长就会挤占整个上下文，也很贵。
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from . import documents
from .i18n import t
from .safe import clean_text

MAX_ATTACHMENTS = 10  # 一条消息最多带几个附件
MAX_IMAGE_BYTES = 7_500_000  # 原始大小。base64 之后正好 10 MB，不超过 Claude 对单张图片的限制
MAX_PDF_BYTES = 25_000_000
MAX_TEXT_CHARS = 400_000
MAX_THUMB_CHARS = 150_000  # 页面自己生成的小预览图（data: 地址）最长这么多字符
MAX_PREVIEW_CHARS = 600_000  # 点开看的大图（长边 1280 像素的 JPEG，一般一两百 KB）
MAX_NAME_CHARS = 200

KINDS = ("image", "pdf", "text", "document")  # document：Word、Excel、PowerPoint，检查时就变成了 text
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")  # 各家都支持的四种图片格式
PDF_TYPE = "application/pdf"
TEXT_TYPE = "text/plain"  # 文本文件的类型浏览器说了不算（.py、.md 各报各的），一律记成这个

# 预览图只能是这几种图片的 data: 地址。它会被页面放进 <img src>，所以格式卡死：不是 data: 开头、
# 或者夹着 base64 以外的字符的，一律不要（不报错，只是不显示预览）
_THUMB = re.compile(r"data:image/(?:jpeg|png|webp);base64,[A-Za-z0-9+/]+={0,2}")
# 原件在多媒体库里的编号：内容的 SHA-256，64 个十六进制字符（见 media.py）
HASH = re.compile(r"[0-9a-f]{64}")

# 文件开头的几个字节（"魔数"）能看出它真正是什么格式。扩展名改过的文件很常见（.png 其实是 JPEG），
# 而 API 发现"说的格式"和"实际的格式"对不上会直接拒绝整个请求，所以以文件内容为准
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


class AttachmentError(ValueError):
    """附件不合格。错误信息可以直接显示给用户。"""


@dataclass(frozen=True)
class Attachment:
    """一个检查过的附件。"""

    kind: str  # image / pdf / text
    name: str  # 文件名（不含路径）
    media_type: str
    size: int  # 图片和 PDF 是原始字节数，文本是字符数
    data: str = ""  # 图片和 PDF 的内容，base64（不带 data: 前缀）
    text: str = ""  # 文本文件的内容
    thumb: str = ""  # 图片的小预览（data: 地址），只给页面看，不发给模型
    preview: str = ""  # 点开看的大图（data: 地址）。完整的原图只在发给模型的历史里，页面拿不到，所以另存一张
    note: str = ""  # 交给模型时附在文件名旁边的说明，比如"这是从 Word 里取出的文字"（见 wrap_text）
    sha256: str = ""  # 原件存进多媒体库之后的编号（见 media.MediaStore.keep）
    # Word、Excel、PowerPoint 的原始文件。交给模型的只是取出的文字（text），原件只留到存进多媒体库为止
    original: bytes = field(default=b"", repr=False, compare=False)

    def summary(self) -> dict:
        """界面上显示这个附件要用的那几项。不含内容本身。"""
        shown = {"kind": self.kind, "name": self.name, "media_type": self.media_type, "size": self.size}
        if self.thumb:
            shown["thumb"] = self.thumb
        if self.preview:
            shown["preview"] = self.preview
        if self.sha256:
            shown["sha256"] = self.sha256
        return shown


def parse(raw: object) -> list[Attachment]:
    """检查浏览器送来的附件列表，返回检查过的附件。不合格时抛出 AttachmentError。没有附件（None）返回空列表。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AttachmentError(t("附件的格式不正确。"))
    if len(raw) > MAX_ATTACHMENTS:
        raise AttachmentError(t("一条消息最多带 {n} 个附件。", n=MAX_ATTACHMENTS))
    return [_parse_one(item) for item in raw]


def _parse_one(item: object) -> Attachment:
    if not isinstance(item, dict) or item.get("kind") not in KINDS:
        raise AttachmentError(t("附件的格式不正确。"))
    name = clean_name(item.get("name"))
    if not name:
        raise AttachmentError(t("附件的格式不正确。"))
    kind = item["kind"]

    if kind == "text":
        text = item.get("text")
        if not isinstance(text, str):
            raise AttachmentError(t("附件的格式不正确。"))
        if len(text) > MAX_TEXT_CHARS:
            raise AttachmentError(t("文本文件“{name}”太长了，一个文件最多 40 万个字符。", name=name))
        text = clean_text(text)
        return Attachment("text", name, TEXT_TYPE, len(text), text=text)

    media_type = item.get("media_type")
    if kind == "document":
        return _parse_document(name, item.get("data"))
    if kind == "image" and media_type not in IMAGE_TYPES:
        raise AttachmentError(t("图片“{name}”的格式不支持，只能是 PNG、JPEG、GIF 或 WebP。", name=name))
    if kind == "pdf" and media_type != PDF_TYPE:
        raise AttachmentError(t("附件的格式不正确。"))
    data = item.get("data")
    content = _decode(name, data)

    if kind == "pdf":
        if len(content) > MAX_PDF_BYTES:
            raise AttachmentError(t("PDF“{name}”太大了，一个 PDF 最大 25 MB。", name=name))
        if b"%PDF-" not in content[:1024]:  # PDF 文件的开头（前面允许有少量别的字节）
            raise AttachmentError(t("“{name}”不是有效的 PDF 文件。", name=name))
        # 设了密码（或者限制了打印、复制）的 PDF 里有一个 /Encrypt 字典。模型的接口只收不加密的 PDF，
        # 发过去会换回一句英文报错，不如在这里说清楚
        if b"/Encrypt" in content:
            raise AttachmentError(t("PDF“{name}”设了密码或者加了限制，模型打不开。去掉密码、另存一份再发。", name=name))
        return Attachment("pdf", name, PDF_TYPE, len(content), data=data)

    if len(content) > MAX_IMAGE_BYTES:
        raise AttachmentError(t("图片“{name}”太大了，一张图片最大 7.5 MB。", name=name))
    actual = sniff_image(content)
    if not actual:
        raise AttachmentError(t("图片“{name}”的格式不支持，只能是 PNG、JPEG、GIF 或 WebP。", name=name))
    thumb, preview = valid_thumb(item.get("thumb")), valid_thumb(item.get("preview"), MAX_PREVIEW_CHARS)
    return Attachment("image", name, actual, len(content), data=data, thumb=thumb, preview=preview)


def _parse_document(name: str, data: object) -> Attachment:
    """Word、Excel、PowerPoint 文件：解码，取出文字，变成一个文本附件。"""
    content = _decode(name, data)
    if len(content) > documents.MAX_DOCUMENT_BYTES:
        raise AttachmentError(t("“{name}”太大了，Word、Excel、PowerPoint 文件最大 25 MB。", name=name))
    try:
        text = clean_text(documents.extract_text(name, content))
    except documents.DocumentError as exc:
        raise AttachmentError(str(exc)) from exc
    return Attachment("text", name, TEXT_TYPE, len(text), text=text, note=documents.NOTE, original=content)


def _decode(name: str, data: object) -> bytes:
    if not isinstance(data, str):
        raise AttachmentError(t("附件的格式不正确。"))
    try:
        # validate=True：base64 以外的字符（包括换行）一律不接受，而不是悄悄跳过。
        # API 那边同样严格，这里放过去的话，到了那边整个请求都会被拒绝
        content = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttachmentError(t("附件“{name}”的内容编码不正确（应该是 base64）。", name=name)) from exc
    if not content:
        raise AttachmentError(t("附件“{name}”是空的。", name=name))
    return content


def sniff_image(content: bytes) -> str:
    """从文件开头的几个字节看出图片的真实格式。不是那四种之一的返回空字符串。"""
    for signature, media_type in _SIGNATURES:
        if content.startswith(signature):
            return media_type
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return ""


def clean_name(value: object) -> str:
    """整理文件名：只留最后一段（去掉路径），空白压成一个空格，太长就截断但保留扩展名。不是文字的返回空字符串。

    文件名会显示在页面上，也会写进发给模型的内容里（<attachment name="...">），所以不能带着路径或换行。
    """
    if not isinstance(value, str):
        return ""
    name = " ".join(re.split(r"[\\/]", clean_text(value))[-1].split())
    if len(name) <= MAX_NAME_CHARS:
        return name
    stem, dot, extension = name.rpartition(".")
    if dot and stem and len(extension) <= 10:
        return stem[: MAX_NAME_CHARS - len(extension) - 2] + "…." + extension
    return name[: MAX_NAME_CHARS - 1] + "…"


def valid_thumb(value: object, limit: int = MAX_THUMB_CHARS) -> str:
    """合格的预览图（小缩略图或点开看的大图）原样返回，否则返回空字符串（不显示预览，但不影响发送）。"""
    if isinstance(value, str) and len(value) <= limit and _THUMB.fullmatch(value):
        return value
    return ""


def unsupported(attachments: Sequence[Attachment], provider: str, vision: bool, pdf: bool) -> str:
    """当前的模型收不了这些附件的话，返回说明；收得了返回空字符串。provider 是给用户看的厂商名字。

    文本文件哪一家都能收：它在发给模型时就是一段文字。
    """
    if not vision and any(item.kind == "image" for item in attachments):
        return t("当前的模型（{provider}）看不了图片。可以在设置里换成 Claude、GPT 或 Gemini。", provider=provider)
    if not pdf and any(item.kind == "pdf" for item in attachments):
        return t("当前的模型（{provider}）读不了 PDF。可以在设置里换成 Claude 或 GPT，或者把需要的文字复制进来。", provider=provider)
    return ""


def wrap_text(attachment: Attachment) -> str:
    """文本文件交给模型时的样子：用 <attachment> 标签包起来，标上文件名，模型才分得清哪些是文件的内容。

    从 Office 文件里取出的文字另外带一句说明（note），让模型知道图片和排版没有了，不要假装看到了。
    名字里的引号等字符要转义，否则文件名里的一个 " 就能提前结束这个属性。（这段文字是写给模型看的，所以不翻译。）
    """
    name = _escape(attachment.name)
    note = f' note="{_escape(attachment.note)}"' if attachment.note else ""
    return f'<attachment name="{name}"{note}>\n{attachment.text}\n</attachment>'


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def from_saved(value: object, previews: bool = True) -> list[dict]:
    """保存的会话里记着的附件概要，整理成可以重放给页面的样子。不合格的项去掉。

    会话文件是普通的 JSON，可能被改坏或被人动过。预览图会放进页面的 <img src>，名字会显示出来，
    所以和浏览器送来的一样，每一项都重新查一遍，只留认识的几个字段。
    previews 为 False 时不要点开看的大图（多媒体库的索引用不到它，它又是最长的一项，查一遍要花时间）。
    哪些项留下、哪些去掉，和 previews 无关：多媒体库靠"第几项"在对话里找到对应的那张卡片，两边必须数得一样。
    """
    if not isinstance(value, list):
        return []
    shown = []
    for item in value:
        if not isinstance(item, dict) or item.get("kind") not in KINDS:
            continue
        name, size, media_type = clean_name(item.get("name")), item.get("size"), item.get("media_type")
        if not name or not isinstance(media_type, str) or not isinstance(size, int) or isinstance(size, bool) or size < 0:
            continue
        entry = {"kind": item["kind"], "name": name, "media_type": media_type[:100], "size": size}
        if item["kind"] == "image":
            fields = (("thumb", MAX_THUMB_CHARS), ("preview", MAX_PREVIEW_CHARS)) if previews else (("thumb", MAX_THUMB_CHARS),)
            for name_of_field, limit in fields:
                if value := valid_thumb(item.get(name_of_field), limit):
                    entry[name_of_field] = value
        if isinstance(item.get("sha256"), str) and HASH.fullmatch(item["sha256"]):
            entry["sha256"] = item["sha256"]
        shown.append(entry)
    return shown
