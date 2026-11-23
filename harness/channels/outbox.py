"""发送队列：一个平台一个，一个线程按顺序把要说的话发到聊天里；发不出去的过一会儿再试，还是不行就留着，等连上了补发。

为什么要一个单独的线程：网络可能慢，agent 循环和收消息的线程都不该等它；顺序也不能乱，一段回答和紧跟着它的确认请求，
在聊天里也得是这个顺序。router.py 把所有要发到聊天里的都排进这里（Job）。

发失败了怎么办。以前失败一次只记一行"发送失败"，那段回答就没了，聊天里什么都看不到。真实的日志里（2026-11-22、23），
Telegram 的连接大约每十分钟被对方重置一次、半分钟内恢复，正好落在这半分钟里的回答就这样丢了。现在：

  1. 先分清是哪一种失败（base.retry_delay，判断只写在那一处）：
       暂时的   连接被重置、被拒绝、超时、域名解析失败、平台出了故障（5xx）、被限流（429；平台说了过多久再来就听它的）
       一直的   别的 4xx（请求不对、你把机器人停用了、消息太长……）、token 不对：再试也一样，只会白白多发几次
  2. 暂时的：等一会儿再试，最多 CHANNEL_SEND_RETRIES 次，间隔从 CHANNEL_SEND_RETRY_SECONDS 起每次翻倍（1、2、4、8 秒）。
     这期间后面排着的都等着：同一个聊天里的顺序不能乱。等的时候通道重新连上了（resume）、程序要退出了（close），马上醒过来
  3. 试完了还是不行：回答、确认请求、确认的结果不丢掉，按聊天留在内存里（每个聊天最多 CHANNEL_UNDELIVERED_MAX 条），
     这个聊天之后要发的也排在它们后面，不插队。下面几种时候按顺序再发一次：
       - 通道重新连上了（状态变成"在线"，hub.py 告诉 router，router 调 resume）
       - 聊天里又来了消息（收得到，多半也发得出去）
       - 通道一直显示在线、只是发的那一下失败了（Telegram 的长轮询一直通，发消息那一下被重置）：每隔
         CHANNEL_UNDELIVERED_RETRY_SECONDS 秒
     没发出去的确认请求，在它上面记一笔（Waiting.delivery），页面上写明"还没发到聊天里"：这一轮一直在等它，网页上照样能答复。
     页面上通道那一行、聊天里的 /status 都写着还有几条没发出去、最后一次失败是什么时候、为什么（summary）
  4. 一直的失败，或者留着的太多了、挤掉的最早那一条：不再试，日志里写一行，页面上算进"没能发出去"（lost）

几种要发的东西不一样（KINDS）：

  say、ask、settle   回答和提示、确认请求、把确认请求改成"已允许"：会再试，最后不行就留着补发。
                     settle 是改一条已经发出去的消息。它不当作"过时了就算了"：结果写在那条消息上，聊天里才看得出这一步是谁、
                     怎么答的，按钮也才会去掉（留着的按钮点了只会说"已经处理过了"）；晚一点改，改出来的照样是对的。
                     它排在它那条确认请求后面：确认请求还留着的话它也跟着留着，补发时先发确认请求、再改成结果
  file               send_file 发的文件：会再试，但不留着补发。模型在等"发出去了没有"，要如实告诉它（见 router.send_file）；
                     这个聊天还有没发出去的话先试着补发它们，补不出去就不发这个文件（不然它会跑到前面那些话的前面）
  status             /status 的回答：会再试，不留着（过一会儿就不对了），也不排在这个聊天没发出去的那些后面：
                     它要说的正是"还有几条没发出去"；内容在真正发的时候才写（Job.work 里），写的是那一刻的样子
  typing、menu       "正在输入"、登记命令菜单：只试一次（"正在输入"过五秒就没意义了；菜单下次启动、换语言时会再登记）

再试可能会重复，这是有意的取舍：请求其实已经到了平台、只是回答在路上丢了（连接正好在这时被重置），我们分不清这两种，
再发一次，聊天里就会多一条一样的；一段要拆成几条发的长回答，中间某一条失败了，再试时前面已经发出去的那几条也会再发一遍。
宁可偶尔多一条，也不要悄悄丢掉一段回答。改一条消息（settle）再试不会多出东西：改成同一个样子，平台最多说一声"没有变化"。

停下和退出：
  - 聊天里的 /stop、网页上的"停止"不等这里：停下一轮靠的是 session.py，和发送无关。send_file 等着的那一下马上不等了，
    还没轮到的文件不再发，正在等着再试的也不再试（Job.cancelled）
  - 退出时（close）不再等着重试：排着的每样只再试一次，留着的也再试一次，最多等 close 给的那么久；还是发不出去的写进日志
    （进程要结束了，内存里的留不住）。平台的代码被限流时等的那一下（base.pause）也马上醒过来
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .. import config
from ..i18n import t
from .base import Channel, ChannelError, pausing, retry_delay
from .platforms import label

logger = logging.getLogger("harness.channels")

# 每一种要发的东西：（失败了再不再试，再试也不行的话要不要留着、等连上了补发）。为什么这样分，见文件开头
KINDS: dict[str, tuple[bool, bool]] = {
    "say": (True, True),
    "ask": (True, True),
    "settle": (True, True),
    "file": (True, False),
    "status": (True, False),
    "typing": (False, False),
    "menu": (False, False),
}

_RESUME = object()  # 排进队列的记号：把留着的按顺序再发一次


@dataclass
class Job:
    """排进发送队列的一样东西。work 拿到通道去发：真正发的时候才去找通道，这期间换了 token、通道重开了，用的是新的那一个。"""

    kind: str  # KINDS 里的一种
    chat: str  # 发到哪个聊天；不属于哪个聊天的（登记命令菜单）是空的
    work: Callable[[Channel], object]
    waiting: Any = None  # ask：那个确认请求（session.Waiting）。没发到聊天里时在它上面记一笔，页面上照着写
    cancelled: Callable[[], bool] | None = None  # file：/stop 了、等的人不等了，就不再试
    done: Callable[[str, ChannelError | None], None] | None = None  # file：告诉等着的人结果（sent、skipped、failed）


class Outbox:
    def __init__(self, platform: str, channel: Callable[[], Channel | None], changed: Callable[[], None] = lambda: None):
        self.platform = platform
        self._channel = channel  # 现在这个平台开着的通道（没开着是 None）
        self._changed = changed  # 留着的、没能发出去的数变了：页面要刷新。不能拿着 _lock 调用（页面算状态时会来问 summary）
        self._queue: queue.Queue[object] = queue.Queue()
        self._lock = threading.Lock()  # 管下面这几样：发送的线程在改，页面、/status 在读
        self._typing: set[str] = set()  # 已经排着一次"正在输入"的聊天：不重复排
        self._kept: dict[str, deque[Job]] = {}  # 每个聊天没发出去、留着补发的，按顺序
        self._lost = 0  # 不再补发了的（一直的失败、留着的太多了挤掉的）
        self._failed_at: float | None = None  # 最后一次没发出去是什么时候（time.time()）
        self._failure: object = ""  # 和为什么（t() 写的，页面上按当时的语言再译一遍）
        self._next_try = 0.0  # 定时把留着的再发一次：下一次在什么时候（time.monotonic()）
        self._closing = threading.Event()
        self._wake = threading.Event()  # 正在等着再试的时候叫醒它：通道重新连上了、要退出了
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"outbox-{platform}")
        self._thread.start()

    # ---- 外面用的 ------------------------------------------------------------

    def put(self, job: Job) -> None:
        self._queue.put(job)

    def typing(self, chat: str, work: Callable[[Channel], object]) -> None:
        """排一次"正在输入"。这个聊天已经排着一次、还没轮到的话不再排（每 4 秒来一次，网络慢的时候会越积越多）。"""
        with self._lock:
            if chat in self._typing:
                return
            self._typing.add(chat)
        self._queue.put(Job("typing", chat, work))

    def resume(self) -> None:
        """通道（重新）连上了、聊天里又来了消息：正在等着再试的马上试，留着的马上按顺序再发一次。"""
        self._wake.set()
        with self._lock:
            kept = bool(self._kept)
        if kept:
            self._queue.put(_RESUME)

    def summary(self) -> dict:
        """页面上、/status 里写的：还有几条没发出去（waiting），几条不再补发了（lost），最后一次失败的时间和原因。"""
        with self._lock:
            return {"waiting": sum(len(kept) for kept in self._kept.values()), "lost": self._lost, "failed_at": self._failed_at, "error": self._failure}

    def waiting(self, chat: str) -> int:
        """这个聊天还有几条没发出去。"""
        with self._lock:
            return len(self._kept.get(chat) or ())

    def forget(self, chat: Callable[[str], bool]) -> None:
        """不再补发这些聊天里留着的（解除绑定、换了主人、删掉了这个平台：它们不该再发到那个聊天里去）。"""
        with self._lock:
            gone = [name for name in self._kept if chat(name)]
            dropped = sum(len(self._kept.pop(name)) for name in gone)
        if dropped:
            logger.info("%s: %d 条没发出去的消息不再补发（聊天已经解除绑定）", self.platform, dropped)
            self._changed()

    def flush(self, timeout: float) -> bool:
        """等排着的都做完（最多等 timeout 秒）。测试和退出时用。留着补发的不算：它们要等通道连上。"""
        done = threading.Event()
        self._queue.put(done.set)
        return done.wait(timeout)

    def close(self, timeout: float) -> None:
        """退出：不再等着重试。排着的、留着的都再试一次（最多等 timeout 秒），还是发不出去的写进日志。"""
        self._closing.set()
        self._wake.set()
        self._queue.put(_RESUME)
        self.flush(timeout)
        left = self.summary()["waiting"]
        if left:
            logger.warning("%s: 退出时还有 %d 条消息没发到聊天里", self.platform, left)
        self._queue.put(None)

    # ---- 发送的线程 ----------------------------------------------------------

    def _run(self) -> None:
        pausing.wake = self._closing  # 平台的代码被限流时等的那一下（base.pause）：退出时马上醒过来
        while True:
            try:
                item = self._queue.get(timeout=self._idle())
            except queue.Empty:
                item = _RESUME if self._due() else False
            if item is None:
                return
            try:
                if item is _RESUME:
                    self._resume()
                elif isinstance(item, Job):
                    self._deliver(item)
                elif callable(item):
                    item()  # flush 的记号
            except Exception:  # 不该发生：记进日志，发送的线程不能停
                logger.exception("%s: 发送时出错", self.platform)
            if self._due():
                self._resume()

    def _idle(self) -> float | None:
        """队列空着的时候最多等多久：有留着的，到下一次定时再试的时候醒来；没有就一直等。"""
        with self._lock:
            if not self._kept:
                return None
            return max(0.05, self._next_try - time.monotonic())

    def _due(self) -> bool:
        """到了定时再试留着的那一刻：通道显示在线才试（不在线的话，等它重新连上时 resume 会马上试）。"""
        with self._lock:
            if not self._kept or time.monotonic() < self._next_try:
                return False
            self._next_try = time.monotonic() + config.CHANNEL_UNDELIVERED_RETRY_SECONDS
        channel = self._channel()
        return channel is not None and channel.status.state == "online"

    def _deliver(self, job: Job) -> None:
        """发一样新排进来的东西。"""
        retry, keep = KINDS[job.kind]
        if job.kind == "typing":
            with self._lock:
                self._typing.discard(job.chat)  # 轮到它了（发不发都一样）：之后可以再排下一次
        if job.cancelled is not None and job.cancelled():
            self._finish(job, "skipped")
            return
        if job.kind != "status" and self.waiting(job.chat):  # 这个聊天还有没发出去的：排在它们后面，不插队
            if keep:
                self._keep(job, None)
                return
            if job.kind != "file":
                return  # "正在输入"：前面的话都还没到，没意义
            self._resume(job.chat)
            behind = self.waiting(job.chat)
            if behind:
                summary = self.summary()
                why = ChannelError(t("前面还有 {n} 条消息没发到聊天里（{reason}），所以这个文件也没有发。", n=behind, reason=summary["error"] or "?"))
                self._give_up(job, why)
                return
        error = self._send(job, config.CHANNEL_SEND_RETRIES if retry else 0)
        if error is None:
            self._finish(job, "sent")
        elif job.cancelled is not None and job.cancelled():
            # 等着重试的时候被 /stop 了：最后那一次对方答了"不行"的，就是没发出去（skipped）；
            # 根本没得到回答的（连接被重置），说不准它到了没有（pending）
            self._finish(job, "pending" if error.status == 0 else "skipped")
        elif keep and retry_delay(error) is not None:
            self._keep(job, error)
        else:
            self._give_up(job, error)

    def _send(self, job: Job, retries: int) -> ChannelError | None:
        """发一次；暂时的失败最多再试 retries 次（间隔每次翻倍）。返回最后一次的错误，发出去了返回 None。"""
        delay = config.CHANNEL_SEND_RETRY_SECONDS
        tries = 0
        while True:
            channel = self._channel()
            if channel is None:  # 这个平台现在关着（或者正在换 token）：不在这里干等，留着等它开起来
                return ChannelError(t("{platform} 现在没有连着。", platform=label(self.platform)), status=0)
            try:
                job.work(channel)
                return None
            except ChannelError as exc:
                error = exc
            except Exception:  # 平台的代码里有 bug：记进日志，不再试（再试也一样）
                logger.exception("%s: 发送时出错", self.platform)
                return ChannelError(t("发送时出错了，详细的原因写在智狐的日志里。"))
            wait = retry_delay(error)
            if wait is None or tries >= retries or self._closing.is_set() or (job.cancelled is not None and job.cancelled()):
                return error
            tries += 1
            seconds = max(wait, delay)
            delay *= 2
            logger.warning("%s: 发送失败，%s 秒后再试（第 %d/%d 次）：%s", self.platform, f"{seconds:g}", tries, retries, error)
            if not self._nap(seconds, job):
                return error

    def _nap(self, seconds: float, job: Job) -> bool:
        """等着再试。通道重新连上了、要退出了，马上醒过来；send_file 被 /stop 了返回 False（不再试）。"""
        self._wake.clear()
        deadline = time.monotonic() + seconds
        while True:
            if job.cancelled is not None and job.cancelled():
                return False
            left = deadline - time.monotonic()
            if left <= 0 or self._closing.is_set():
                return True
            # 发文件时每 0.1 秒看一眼它是不是被停下了（停下的旗子在 router 那边，不能叫醒这里）
            if self._wake.wait(min(left, 0.1) if job.cancelled is not None else left):
                return not (job.cancelled is not None and job.cancelled())

    def _keep(self, job: Job, error: ChannelError | None) -> None:
        """留着，等连上了补发。error 是空的：不是它自己没发出去，是这个聊天前面还有没发出去的，它排在后面。"""
        with self._lock:
            kept = self._kept.setdefault(job.chat, deque())
            kept.append(job)
            if not self._next_try:
                self._next_try = time.monotonic() + config.CHANNEL_UNDELIVERED_RETRY_SECONDS
            if error is not None:
                self._failed_at, self._failure = time.time(), _reason(error)
            dropped = kept.popleft() if len(kept) > config.CHANNEL_UNDELIVERED_MAX else None
            if dropped is not None:
                self._lost += 1
            count = len(kept)
        if job.waiting is not None:
            job.waiting.delivery, job.waiting.problem = "waiting", _reason(error) if error is not None else self.summary()["error"]
        if dropped is not None:
            if dropped.waiting is not None:
                dropped.waiting.delivery = "failed"
            logger.warning("%s: 没发出去的消息太多了（超过 %d 条），最早的一条不再补发", self.platform, config.CHANNEL_UNDELIVERED_MAX)
        if error is not None:
            logger.warning("%s: 发送失败，先留着，连上以后按顺序补发（这个聊天有 %d 条没发出去）：%s", self.platform, count, error)
        self._changed()

    def _resume(self, only: str | None = None) -> None:
        """把留着的按顺序再发一次（only：只发这一个聊天的）。每个聊天从最早的一条起，一条发不出去就停下，后面的接着等。"""
        with self._lock:
            chats = [only] if only is not None else list(self._kept)
        sent, failed = 0, False
        for chat in chats:
            while True:
                with self._lock:
                    kept = self._kept.get(chat)
                    job = kept[0] if kept else None
                    if kept is not None and job is None:
                        del self._kept[chat]  # 空了（别处刚拿走）：不留一个空的，免得定时器一直醒
                if kept is None or job is None:
                    break
                error = self._send(job, 0)
                if error is not None and retry_delay(error) is not None:
                    with self._lock:
                        self._failed_at, self._failure = time.time(), _reason(error)
                    logger.warning("%s: 留着的消息还是发不出去（这个聊天有 %d 条）：%s", self.platform, self.waiting(chat), error)
                    failed = True
                    break
                with self._lock:
                    if kept and kept[0] is job:
                        kept.popleft()
                    if not kept and self._kept.get(chat) is kept:
                        del self._kept[chat]
                if error is None:
                    sent += 1
                    self._finish(job, "sent")
                else:
                    self._give_up(job, error)
        with self._lock:
            self._next_try = time.monotonic() + config.CHANNEL_UNDELIVERED_RETRY_SECONDS if self._kept else 0.0
            left = sum(len(kept) for kept in self._kept.values())
        if sent:
            logger.info("%s: 补发了 %d 条留着的消息（还有 %d 条没发出去）", self.platform, sent, left)
        if sent or failed:
            self._changed()  # 页面上的数字、"最后一次失败"的时间跟着变

    def _finish(self, job: Job, state: str) -> None:
        if job.waiting is not None:
            job.waiting.delivery, job.waiting.problem = "", ""
        if job.done is not None:
            job.done(state, None)

    def _give_up(self, job: Job, error: ChannelError) -> None:
        """不再试了：一直的失败，或者是不留着补发的那几种。"""
        _, keep = KINDS[job.kind]
        if job.done is not None:
            logger.warning("%s: 发送失败，文件没有发出去：%s", self.platform, error)
            job.done("failed", error)
            return
        if not keep:  # /status 的回答写进警告；"正在输入"、命令菜单只是顺带的，记一行普通的
            logger.log(logging.WARNING if job.kind == "status" else logging.INFO, "%s: 发送失败，不再重试（%s）：%s", self.platform, job.kind, error)
            return
        with self._lock:
            self._lost += 1
            self._failed_at, self._failure = time.time(), _reason(error)
        if job.waiting is not None:
            job.waiting.delivery, job.waiting.problem = "failed", _reason(error)
        logger.warning("%s: 发送失败，不再重试：%s", self.platform, error)
        self._changed()


def _reason(error: ChannelError) -> object:
    """页面上、/status 里写的"为什么"：t() 写的那句话本身（换了语言能重译），不是 str。"""
    return error.args[0] if error.args else str(error)
