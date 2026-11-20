"""测试里用的附件：几个最小的、格式正确的文件，写成浏览器发来的样子（见 harness/attachments.py）。"""

from __future__ import annotations

import base64

# 一张 1×1 像素的 PNG（Claude 文档里的示例图片）
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
PNG_BYTES = base64.b64decode(PNG)
# 服务器只看文件开头的几个字节来认格式，所以 JPEG 只要有个开头就够了
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
PDF_BYTES = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PDF = base64.b64encode(PDF_BYTES).decode()
THUMB = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQ=="  # 页面生成的小预览图


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def image(name: str = "截图.png", data: str = PNG, media_type: str = "image/png", **extra) -> dict:
    return {"kind": "image", "name": name, "media_type": media_type, "data": data, **extra}


def pdf(name: str = "报告.pdf", data: str = PDF, **extra) -> dict:
    return {"kind": "pdf", "name": name, "media_type": "application/pdf", "data": data, **extra}


def text_file(name: str = "笔记.md", text: str = "第一行\n第二行", **extra) -> dict:
    # 文本文件的 media_type 浏览器说什么都行，服务器不看
    return {"kind": "text", "name": name, "media_type": "text/markdown", "text": text, **extra}
