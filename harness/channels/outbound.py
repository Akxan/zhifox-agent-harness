"""往聊天里发文件：send_file 工具（../tools/send.py）背后属于聊天通道的那一半。

工具只管"发哪个文件、问不问你"，不知道背后是哪个平台。它手里拿着一个 Outlet（去处）：这个聊天，由 Session 建好、
经过 App.channel_agent 交给工具。工具问它两件事：

  plan   这个文件在这个平台上怎么发：当照片（聊天里直接显示）还是当文件，或者发不了（太大、不支持的格式、平台不支持）。
         规矩写在各平台的 Capabilities.uploads 里（见 base.Uploads），这里按它算。工具在问你之前就先问一遍：
         明知发不了的，不弹确认卡片，直接告诉模型为什么、该怎么办（"先打包成 zip""先缩小一份"）
  send   真的发。不是直接发，而是排进这个平台的发送队列（router.send_file）、等它发完：助手在发文件之前说的话
         还排在队列里，聊天里的顺序得和它做的一样（先说"截好了"，再出现图片）

这里的话都是写给模型看的（工具的结果），所以是中文、不翻译。平台说的错误（ChannelError，用 t() 写的）换回中文原文。
平台的名字用 i18n.fill 填：和 t() 一样，"飞书"这样的中文名字两边不留空格（"飞书最多能发"），Telegram 这样的留着。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import PurePath
from typing import TYPE_CHECKING, Any

from ..config import CHANNEL_SEND_BYTES, CHANNEL_UPLOAD_TIMEOUT
from ..display import file_size
from ..i18n import Message, fill
from ..tools.base import ToolError
from .base import ChannelError, OutboundFile, Uploads
from .platforms import LABELS

if TYPE_CHECKING:
    from .router import Router

# 等一个文件发完最多等多久：排在它前面的话、上传本身（Slack 要三个请求），都算在里面
WAIT_SECONDS = CHANNEL_UPLOAD_TIMEOUT * 3


def chinese(text: object) -> str:
    """一句用 t() 写的话的中文原文（里面填进去的也是 t() 写的话的，一起换回中文）。普通的文字原样返回。"""
    if not isinstance(text, Message):
        return str(text)
    values = {name: chinese(text.nested[name]) if name in text.nested else value for name, value in text.values.items()}
    return fill(text.key, values)


def plan(uploads: Uploads | None, label: str, name: str, size: int, media_type: str) -> tuple[str, str]:
    """一个文件在这个平台上怎么发。返回（photo 或 document，发不了的原因）；原因是空的表示可以发。label 是平台的名字。"""
    if uploads is None:
        return "", fill("{platform} 暂时不支持让机器人发文件，只能发文字。可以把要给用户看的内容直接写在回答里，或者告诉他文件在这台电脑上的位置。", {"platform": label})
    if size <= 0:
        return "", "这个文件是空的（0 字节），没有可发的内容。"
    if media_type in uploads.photo_types and size <= uploads.photo_bytes:
        return "photo", ""
    extension = PurePath(name).suffix.lower().removeprefix(".")
    if uploads.extensions is not None and extension not in uploads.extensions:
        allowed = "、".join(sorted(uploads.extensions))
        photos = "、".join(sorted(kind.split("/", 1)[1].upper() for kind in uploads.photo_types))
        zipped = "可以先打包成 zip 再发（比如 zip 文件.zip 原文件）。" if "zip" in uploads.extensions else ""
        return "", fill("{platform} 只能发 {photos} 格式的图片，和这几种文件：{allowed}。{zipped}", {"platform": label, "photos": photos, "allowed": allowed, "zipped": zipped})
    limit = min(uploads.file_bytes, CHANNEL_SEND_BYTES)
    if size > limit:
        shrink = "图片可以先缩小一份再发（macOS 上 sips -Z 2000 原图 --out 小图.jpg，Linux 上 convert 原图 -resize 2000x2000 小图.jpg）。" if media_type.startswith("image/") else "可以先压缩、拆成几份，或者告诉用户文件在这台电脑上的位置。"
        return "", fill("文件太大了（{size}），{platform} 最多能发 {limit}。{shrink}", {"size": file_size(size), "platform": label, "limit": file_size(limit), "shrink": shrink})
    return "document", ""


class Outlet:
    """send_file 的去处：一个聊天。Session 建它（每个聊天一个），交给 App.channel_agent，工具发文件时用它。"""

    def __init__(self, router: Router, platform: str, chat: str, ui: Any, person: Callable[[], str]):
        self.router = router
        self.platform = platform
        self.chat = chat
        self.ui = ui  # 这个聊天的 ChannelUI：你发 /stop 时，正在等的发送不再等下去（见 send）
        self._person = person

    @property
    def label(self) -> str:
        """平台的名字（中文原文："Telegram"、"飞书"……）。给人看的时候再 t() 一下。"""
        return LABELS.get(self.platform, self.platform)

    @property
    def person(self) -> str:
        """聊天的另一头：配对过的主人给人看的名字。"""
        return self._person()

    def plan(self, name: str, size: int, media_type: str) -> tuple[str, str]:
        """见上面的 plan。这个平台现在没连着（在网页上关掉了、正在换 token），也发不了。"""
        if not self.router.attached(self.platform):
            return "", fill("{platform} 现在没有连着（可能在网页上关掉了），发不出去。", {"platform": self.label})
        return plan(self.router.capabilities(self.platform).uploads, self.label, name, size, media_type)

    def send(self, outgoing: OutboundFile) -> None:
        """发出去，等它发完。发不出去、被 /stop 了、等太久了，都抛出 ToolError（写给模型看的中文）。"""
        cancelled = threading.Event()
        forget = self.ui.on_stop(cancelled.set)
        try:
            try:
                state = self.router.send_file(self.platform, self.chat, outgoing, cancelled, WAIT_SECONDS)
            except ChannelError as exc:
                raise ToolError(f"没有发出去：{chinese(exc.args[0] if exc.args else exc)}") from None
        finally:
            forget()
        if state == "pending" and cancelled.is_set():
            raise ToolError("用户中断了发送。文件可能已经发出去了，也可能没有。")
        if state == "pending":
            raise ToolError("发送等了太久还没有结果：文件可能还在路上，也可能没发出去。先不要重发，问问用户收到了没有。")
        if state == "skipped":
            raise ToolError("用户中断了发送，文件没有发出去。")
