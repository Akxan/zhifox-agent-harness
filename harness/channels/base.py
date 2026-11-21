"""一个聊天通道要做到的几件事，以及在通道和程序之间传来传去的东西。

不管是 Telegram、飞书还是 Slack，对智狐来说都是同一件事：

    收消息  ──► 平台把"谁、在哪个聊天里、说了什么、带了什么文件"交给我们（Inbound）
               有人点了消息下面的按钮，也交给我们（Press）
    发消息  ◄── 我们把要说的话（send）、要你批准的操作（ask，带"允许 / 拒绝"按钮）交给平台

每个平台各写一个类（telegram.py……），实现下面 Channel 的这几个方法；router.py 只认这几个方法，
不知道背后是哪一家。平台之间的差别（能不能放按钮、一条消息多长、能不能显示"正在输入"）写在 Capabilities 里，
router.py 按它决定怎么说话：没有按钮的平台，确认请求后面跟一句"回复 允许 或 拒绝"。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field


class ChannelError(Exception):
    """通道出错了（token 不对、连不上、平台拒绝了请求……）。错误信息可以直接显示给用户，里面绝不含 token。"""


@dataclass(frozen=True)
class Capabilities:
    buttons: bool  # 消息下面能不能放按钮（Telegram 的 inline keyboard）。不能的话，确认请求靠回复文字来答
    max_chars: int  # 一条消息最长多少个字符（按 UTF-16 数，Telegram 就是这样数的）。更长的回复拆成几条发
    typing: bool  # 能不能显示"正在输入"
    files: bool  # 能不能收图片和文件


@dataclass
class InboundFile:
    """聊天里发来的一个文件（照片或者文档）。内容等到真要用的时候才下载（fetch），不在收消息的线程里下。"""

    name: str
    media_type: str  # 平台说的类型。不一定对，attachments.py 会按文件内容再查一遍
    size: int | None  # 平台说的大小（字节）；不知道是 None
    kind: str  # photo（照片，平台已经压缩成 JPEG）或 document（原样发来的文件）
    fetch: Callable[[], bytes]
    thumb: Callable[[], bytes] | None = None  # 小预览图（只有照片有），给网页上的会话显示用


@dataclass
class Inbound:
    """聊天里来了一条消息。"""

    platform: str
    chat: str  # 聊天的编号（写成文字）。私聊时和发消息的人的编号一样
    user: str  # 发消息的人的编号
    name: str  # 发消息的人给人看的名字，比如"Akxan (@akxan)"
    text: str = ""
    files: list[InboundFile] = field(default_factory=list)
    private: bool = True  # 是不是一对一的私聊。群聊里的消息一律不理（群里的每个人都能看到、都能发）
    unsupported: str = ""  # 发来的是处理不了的东西（语音、视频、贴纸……）时，写着是哪一种


@dataclass
class Press:
    """有人点了消息下面的一个按钮。"""

    platform: str
    chat: str
    user: str
    name: str
    data: str  # 按钮上带着的数据，比如 "ok:12"（允许第 12 号确认）
    answer: Callable[[str], None]  # 给点按钮的人回一句小提示（Telegram 会在屏幕上方弹一下）


@dataclass
class Prompt:
    """一个要你批准的操作，发到聊天里的样子由各个平台自己排（见 telegram.py 的 ask）。"""

    id: int  # 确认请求的编号（WebUI 里的那个），按钮上带着它
    title: str  # "智狐想执行一条命令，要你批准"
    detail: str  # 要做的事，原样显示（命令、文件内容……），平台会把它放进代码块
    where: str  # "工作目录：/srv/app"
    allow: str  # 两个按钮上的字
    deny: str
    hint: str  # 没有按钮的平台用的："回复 允许 或 拒绝"


@dataclass
class Status:
    """一个通道现在怎么样。state 是 off / connecting / online / error 之一。"""

    state: str = "off"
    message: str = ""  # 给人看的说明（出错的原因、"正在重试"……），用 t() 写的，发给页面时再按当时的语言译一遍
    since: float = field(default_factory=time.time)


Event = Inbound | Press


class Channel(ABC):
    """一个平台的连接。start 之后，平台那边来的消息和按钮都交给 deliver；stop 之后不再交。

    发消息的几个方法会做网络请求，可能慢、可能失败（抛出 ChannelError）。router.py 在一个单独的线程里
    按顺序调用它们（见 router.Outbox），不会在收消息的线程里等它们。
    """

    platform: str
    capabilities: Capabilities

    @abstractmethod
    def start(self, deliver: Callable[[Event], None]) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @property
    @abstractmethod
    def status(self) -> Status: ...

    @abstractmethod
    def send(self, chat: str, text: str, markdown: bool = False) -> None:
        """发一段话。markdown 为真时 text 是模型写的 Markdown，平台尽量把它排成带格式的样子；太长就拆成几条。"""

    @abstractmethod
    def ask(self, chat: str, prompt: Prompt) -> str:
        """发一个确认请求（带"允许 / 拒绝"按钮）。返回这条消息的编号，答复以后 settle 用它把按钮去掉。"""

    @abstractmethod
    def settle(self, chat: str, handle: str, outcome: str) -> None:
        """确认请求答复了（不管是在聊天里还是在网页上）：去掉按钮，在那条消息上写明结果。"""

    @abstractmethod
    def typing(self, chat: str) -> None:
        """显示"正在输入"。没有这个功能的平台什么都不做。"""

    def set_commands(self, commands: dict[str, str]) -> None:
        """把 /new、/stop 这些命令登记到平台的菜单里（命令 → 一句说明）。没有菜单的平台什么都不做。"""
