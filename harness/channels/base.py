"""一个聊天通道要做到的几件事，以及在通道和程序之间传来传去的东西。

不管是 Telegram、飞书还是 Slack，对智狐来说都是同一件事：

    收消息  ──► 平台把"谁、在哪个聊天里、说了什么、带了什么文件"交给我们（Inbound）
               有人点了消息下面的按钮，也交给我们（Press）
    发消息  ◄── 我们把要说的话（send）、要你批准的操作（ask，带"允许 / 拒绝"按钮）交给平台
    发文件  ◄── 助手用 send_file 工具把这台电脑上的一个文件发过去（send_file）：图片尽量当照片发，聊天里直接看得到

每个平台各写一个类（telegram.py……），实现下面 Channel 的这几个方法；router.py 只认这几个方法，
不知道背后是哪一家。平台之间的差别（能不能放按钮、一条消息多长、能不能显示"正在输入"）写在 Capabilities 里，
router.py 按它决定怎么说话：没有按钮的平台，确认请求后面跟一句"回复 允许 或 拒绝"。
能不能发文件、多大、哪几种，写在 Capabilities.uploads 里（Uploads）；不能发的平台（QQ）是 None。
"""

from __future__ import annotations

import logging
import queue
import random
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import CHANNEL_SEEN_MESSAGES
from ..i18n import t


class ChannelError(Exception):
    """通道出错了（token 不对、连不上、平台拒绝了请求……）。错误信息可以直接显示给用户，里面绝不含 token。"""


# 能当照片发的图片（按文件内容认出来的，见 attachments.sniff_image）
IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


@dataclass(frozen=True)
class Uploads:
    """一个平台能往聊天里发什么文件（send_file 工具）。数字是平台文档写的上限，见各平台模块里的 UPLOADS。"""

    file_bytes: int  # 当普通文件发，一个最多多大
    photo_bytes: int = 0  # 图片当照片发（聊天里直接显示出来）最多多大；更大的当普通文件发。0：图片也只能当文件发
    photo_types: frozenset[str] = IMAGE_TYPES  # 哪几种图片能当照片发
    # 只能发这几种扩展名的文件（小写，不带点；钉钉只认几种）。None：什么文件都行
    extensions: frozenset[str] | None = None
    caption_chars: int = 0  # 说明能和文件放在同一条消息里的话，最多多少个字符（UTF-16）；更长的、或者是 0，另发一条


@dataclass
class OutboundFile:
    """要发到聊天里的一个文件。kind 是 photo（当照片发，聊天里直接显示）或 document（当文件发）。"""

    name: str
    data: bytes
    media_type: str  # 按内容认出来的图片格式（image/png……），不是图片的是 application/octet-stream
    kind: str
    caption: str = ""  # 附在文件下面的一句说明（模型写的 Markdown）


@dataclass(frozen=True)
class Capabilities:
    buttons: bool  # 消息下面能不能放按钮（Telegram 的 inline keyboard）。不能的话，确认请求靠回复文字来答
    max_chars: int  # 一条消息最长多少个字符（按 UTF-16 数，Telegram 就是这样数的）。更长的回复拆成几条发
    typing: bool  # 能不能显示"正在输入"
    files: bool  # 能不能收图片和文件
    # 在这个平台上，/new 这些命令怎么写。大多数平台就是 /new；Slack 会把所有 / 开头的话都当成它自己的命令拦下来，
    # 所以只能登记一个 /zhifox，命令写成 /zhifox new（见 slack.py）
    command: str = "/{name}"
    uploads: Uploads | None = None  # 能往聊天里发什么文件（send_file 工具）；None：这个平台暂时不支持

    def spell(self, name: str) -> str:
        """命令 name 在这个平台上的写法，比如 "/new" 或 "/zhifox new"。"""
        return self.command.replace("{name}", name)


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
class InboundAudio:
    """聊天里发来的一段语音（按住说话录的）或者一个音频文件。模型听不了声音：要先转成文字（见 ../voice/），
    转出来的文字才当作你说的话。内容和文件一样，等到真要用的时候才下载（fetch）。暂时只有 Telegram 会给。"""

    name: str  # 文件名（带扩展名：上传去转文字时，服务器靠它认格式）
    media_type: str  # 平台说的类型，比如 audio/ogg
    size: int | None  # 平台说的大小（字节）；不知道是 None
    duration: int  # 平台说的时长（秒）；不知道是 0
    kind: str  # voice（语音消息）或 audio（音频文件）
    fetch: Callable[[], bytes]


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
    audio: InboundAudio | None = None  # 发来的是一段语音或者音频（只有 Telegram 会给，见 ../voice/）


@dataclass
class Press:
    """有人点了消息下面的一个按钮。"""

    platform: str
    chat: str
    user: str
    name: str
    data: str  # 按钮上带着的数据，比如 "ok:12"（允许第 12 号确认）
    # 给点按钮的人回一句小提示：answer(话, problem)。Telegram、飞书会在屏幕上弹一下；Discord、Slack 没有这种小提示，
    # 只在 problem 为真（比如"这个确认请求已经处理过了"）时另发一句只有他看得到的话，答复成功时不多说（按钮会被换成结果）。
    # 话是空字符串表示"不用提示"（陌生人点的、看不懂的按钮）。Discord 要求 3 秒内一定回应，所以每次按钮都会调用它一次
    answer: Callable[..., None]


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
    # 状态变了就通知一声（hub.py 建好通道后设上，用来刷新页面；PushChannel 和 TelegramChannel 在 __init__ 里先设成 None）。
    # 这里只是声明有这个属性（不赋值，什么都不改），hub 给它赋值时 mypy 才认得
    on_status: Callable[[], None] | None

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

    def send_file(self, chat: str, outgoing: OutboundFile) -> None:
        """把一个文件发到聊天里（大小、种类已经按 capabilities.uploads 检查过，见 outbound.plan）。说明太长、和文件放不进
        同一条消息的，文件之后另发一条。发不出去抛出 ChannelError。不支持的平台（uploads 是 None）用这里的：直接说不支持。"""
        raise ChannelError(t("这个平台暂时不支持发文件。"))


class PushChannel(Channel):
    """Discord、Slack、QQ、飞书、钉钉共用的那一部分：平台顺着一条一直开着的连接把消息推过来（不是 Telegram 那样一次次去问）。

    子类只要写 _run（在自己的线程里连上平台、收消息、断了就重连，直到 stopping 被设上）和 _halt（让 _run 尽快醒过来退出，
    比如关掉 WebSocket），以及发消息的那几个方法。状态、线程、"同一条消息推了两次"这些在这里。
    """

    def __init__(self) -> None:
        self._status = Status("off")
        self._status_lock = threading.Lock()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._seen: OrderedDict[str, None] = OrderedDict()  # 最近处理过的消息编号
        self._seen_lock = threading.Lock()
        self.on_status: Callable[[], None] | None = None  # 状态变了就通知一声（hub.py 用它刷新页面）
        self._inbox: queue.Queue | None = None  # 收到、还没处理的事件（见 _hand_off）

    @property
    def status(self) -> Status:
        with self._status_lock:
            return self._status

    def _set_status(self, state: str, message: str = "") -> None:
        with self._status_lock:
            if self._status.state == state and self._status.message == message:
                return
            self._status = Status(state, message)
        log = logging.getLogger(f"harness.channels.{self.platform}")
        if state == "error":
            log.warning("%s: %s", self.platform, message)
        else:
            log.info("%s: %s%s", self.platform, state, f" ({message})" if message else "")
        if self.on_status is not None:
            self.on_status()

    def start(self, deliver: Callable[[Event], None]) -> None:
        self._stopping.clear()
        self._set_status("connecting")
        self._thread = threading.Thread(target=self._guarded, args=(deliver,), daemon=True, name=self.platform)
        self._thread.start()

    def _guarded(self, deliver: Callable[[Event], None]) -> None:
        try:
            self._run(deliver)
        except Exception as exc:  # 不该发生；发生了也要让页面上看得到，而不是悄悄没了
            logging.getLogger(f"harness.channels.{self.platform}").exception("%s: 收消息的线程出错了", self.platform)
            if not self._stopping.is_set():
                self._set_status("error", str(exc) or type(exc).__name__)

    def stop(self) -> None:
        self._stopping.set()
        try:
            self._halt()
        finally:
            self._set_status("off")
            with self._seen_lock:
                inbox, self._inbox = self._inbox, None  # 再 start 的话会新建一个
            if inbox is not None:
                inbox.put(None)

    def join(self, timeout: float) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    @abstractmethod
    def _run(self, deliver: Callable[[Event], None]) -> None: ...

    def _halt(self) -> None:
        """stop 时调用：让 _run 尽快醒过来退出。"""

    def _online(self) -> None:
        """连上了。停下以后才连上的（stop 和连接同时发生）不算：页面上应该一直是"已关闭"。"""
        if not self._stopping.is_set():
            self._set_status("online")

    def _trouble(self, state: str, message: str) -> None:
        if not self._stopping.is_set():
            self._set_status(state, message)

    def _first_time(self, message_id: str) -> bool:
        """这条消息是不是第一次见到（平台偶尔会把同一条推两次）。没有编号的当作第一次。"""
        if not message_id:
            return True
        with self._seen_lock:
            if message_id in self._seen:
                return False
            self._seen[message_id] = None
            while len(self._seen) > CHANNEL_SEEN_MESSAGES:
                self._seen.popitem(last=False)
            return True

    def _hand_off(self, job: Callable[[], None]) -> None:
        """飞书、钉钉的 SDK 要求事件的处理函数马上返回（不然会堵住它的心跳，对方等不到回执还会再推一遍）。
        所以真正的处理（下载、问名字、交给 router）放到这个通道自己的一个线程里，按收到的顺序一件件做。"""
        with self._seen_lock:
            if self._inbox is None:
                self._inbox = queue.Queue()
                threading.Thread(target=self._drain, args=(self._inbox,), daemon=True, name=f"{self.platform}-inbox").start()
            self._inbox.put(job)

    def _drain(self, jobs: queue.Queue) -> None:
        while True:
            job = jobs.get()
            if job is None:
                return
            if self._stopping.is_set():
                continue  # 停下以后才轮到的：不处理
            try:
                job()
            except Exception:
                logging.getLogger(f"harness.channels.{self.platform}").exception("%s: 处理一条消息时出错", self.platform)

    def typing(self, chat: str) -> None:
        """大多数平台没有给机器人用的"正在输入"：什么都不做。"""


def backoff(delay: float, ceiling: float) -> tuple[float, float]:
    """重连要等多久：（这一次等多久，下一次的基数）。每次翻倍，到 ceiling 为止；这一次再随机少一点，
    免得很多客户端在同一时刻一起重连（平台那边出了故障、恢复的那一刻）。"""
    return delay * random.uniform(0.7, 1.0), min(delay * 2, ceiling)


def close_loop(loop: Any) -> None:
    """关掉一个 SDK 用过的事件循环（飞书、钉钉的 Listener 在自己的线程里建的那个）。SDK 在里面留着几个还没结束的任务
    （清缓存的定时任务、心跳、正在处理的消息）：先取消它们、等它们收完尾（最多 2 秒），再关。直接关的话，
    asyncio 会在日志里报一句"Task was destroyed but it is pending!"。"""
    import asyncio

    try:
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.wait(pending, timeout=2))
        loop.close()
    except RuntimeError:
        pass
