"""Discord 式的网关：Discord 的 Gateway 和 QQ 机器人的 WebSocket 用的是同一套做法，写一遍，两边共用。

一次连接是这样的（Discord 的说明：https://docs.discord.com/developers/events/gateway ；
QQ 的说明：https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html ）：

    连上网关的 WebSocket
      ◄── Hello（op 10）：里面写着心跳的间隔 heartbeat_interval（毫秒）
      ──► Identify（op 2）：机器人的 token、要收哪几类事件（intents）。这就是"登录"
          或者 Resume（op 6）：上一次断开之前的会话编号和收到的最后一个序号，平台会把这期间漏掉的事件补发过来
      ◄── Dispatch（op 0）：READY（登录成功，带着会话编号）、RESUMED，以及真正的事件（MESSAGE_CREATE……）。
          每个事件带着一个序号 s，心跳和 Resume 都要带上收到的最后一个
      ──► 每隔 heartbeat_interval 发一次心跳（op 1），对方回 Heartbeat ACK（op 11）。第一次心跳要先等
          "间隔 × 一个 0 到 1 之间的随机数"：很多机器人同时重连时，心跳不会挤在同一刻
      ◄── op 1：平台要你马上发一次心跳；op 7：请你重连（接着用 Resume）；op 9：会话失效了（d 为真还能 Resume，
          为假要重新 Identify）

几件要小心的事：

  僵死的连接   发下一次心跳时，上一次的还没有回 ACK：连接多半已经断了（只是 TCP 还不知道）。这时不用关闭握手，
               直接断开，用一个不是 1000、1001 的代码（那两个会让平台把会话作废），然后 Resume
  登录的次数   Discord 一天只许 Identify 1000 次，超了会把 token 作废。所以能 Resume 就 Resume，
               重连的间隔每次翻倍、最多 GATEWAY_RETRY_SECONDS 秒；token 不对这类"再试也没用"的关闭代码，直接停下不再连
  关闭代码     每家各有一张表（哪些能 Resume、哪些要重新 Identify、哪些别再连了），由调用的人给（classify）

这里只管连接本身：收到的事件交给 on_dispatch，状态交给 set_status。一个连接一个线程，心跳也在这个线程里发
（recv 最多等到下一次该发心跳的时候），没有第二个线程，不用担心两边同时写。
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..config import CHANNEL_TIMEOUT, GATEWAY_RETRY_SECONDS
from ..i18n import t
from ..net.websocket import ConnectionClosed, WebSocket, WebSocketError
from ..safe import to_json
from .base import ChannelError, backoff

# 操作码（两家一样）
DISPATCH, HEARTBEAT, IDENTIFY, RESUME, RECONNECT, INVALID_SESSION, HELLO, HEARTBEAT_ACK = 0, 1, 2, 6, 7, 9, 10, 11
KEEP_SESSION = 4900  # 我们自己断开、还想 Resume 时用的关闭代码（不是 1000、1001 就行）

# classify 的结果：这个关闭代码之后该怎么做
RESUME_NEXT = "resume"  # 重连，接着用 Resume
IDENTIFY_NEXT = "identify"  # 重连，重新登录
FATAL = "fatal"  # 别再连了（token 不对、权限不够……），等你改了设置
SLOW_DOWN = "slow"  # 发得太快了，多等一会儿再 Resume

logger = logging.getLogger("harness.channels.gateway")


class Wait(ChannelError):
    """现在连不了，要等 seconds 秒（比如 Discord 今天的登录次数用完了）。"""

    def __init__(self, message: str, seconds: float):
        super().__init__(message)
        self.seconds = seconds


class Fatal(ChannelError):
    """再试也没用（token 不对……），停下，等你改了设置。"""


@dataclass
class Rules:
    """一家平台的规矩。"""

    name: str  # 写日志用的名字
    url: Callable[[], str]  # 问平台要网关的地址（每次全新连接之前调用；可能抛出 ChannelError、Wait、Fatal）
    identify: Callable[[], dict]  # Identify 的 d（QQ 的 token 会过期，所以每次现取）
    resume: Callable[[str, int], dict]  # Resume 的 d（会话编号，最后一个序号）
    classify: Callable[[int], tuple[str, str]]  # 关闭代码 → （怎么办，给人看的原因）
    query: str = ""  # 连网关时网址后面加的参数（Discord 是 ?v=10&encoding=json）


class Gateway:
    def __init__(self, rules: Rules, on_dispatch: Callable[[str, dict], None], set_status: Callable[[str, str], None], stopping: threading.Event):
        self.rules = rules
        self.on_dispatch = on_dispatch
        self.set_status = set_status
        self.stopping = stopping
        self.session_id = ""
        self.seq: int | None = None
        self.resume_url = ""
        self.user: dict = {}  # READY 里机器人自己的信息
        self._ws: WebSocket | None = None
        self._lock = threading.Lock()

    def stop(self) -> None:
        """从别的线程调用：关掉连接（用 1000：我们是真的不要了，会话作废也没关系），run 会醒过来退出。"""
        with self._lock:
            ws = self._ws
        if ws is not None:
            ws.close(1000)

    def run(self) -> None:
        """连上、收事件、断了就重连，直到 stopping 被设上，或者遇到了"再试也没用"的情况。"""
        delay = 1.0
        while not self.stopping.is_set():
            resuming = bool(self.session_id and self.seq is not None)
            try:
                url = (self.resume_url if resuming and self.resume_url else self.rules.url()) + self.rules.query
                ws = WebSocket.connect(url, timeout=CHANNEL_TIMEOUT)
            except Fatal as exc:
                self.set_status("error", str(exc))
                return
            except Wait as exc:
                self.set_status("error", str(exc))
                self.stopping.wait(exc.seconds)
                continue
            except (ChannelError, WebSocketError) as exc:
                self.set_status("connecting", str(exc))
                wait, delay = backoff(delay, GATEWAY_RETRY_SECONDS)
                self.stopping.wait(wait)
                continue
            with self._lock:
                self._ws = ws
            if self.stopping.is_set():
                ws.close(1000)
                return
            try:
                outcome, why, ready = self._session(ws, resuming)
            finally:
                with self._lock:
                    self._ws = None
            if ready:
                delay = 1.0  # 这一次登录成功过：下一次重连从 1 秒开始
            if self.stopping.is_set():
                return
            if outcome == FATAL:
                self.set_status("error", why)
                return
            if outcome == IDENTIFY_NEXT:
                self.session_id, self.seq, self.resume_url = "", None, ""
            self.set_status("connecting", why)
            if outcome == SLOW_DOWN:
                self.stopping.wait(60)
                continue
            wait, delay = backoff(delay, GATEWAY_RETRY_SECONDS)
            self.stopping.wait(wait)

    def _send(self, ws: WebSocket, op: int, data: object) -> None:
        ws.send_text(to_json({"op": op, "d": data}))

    def _session(self, ws: WebSocket, resuming: bool) -> tuple[str, str, bool]:
        """一次连接从头到尾。返回（接下来怎么办，原因，这次有没有登录成功过）。"""
        ready = False
        try:
            hello = self._decode(ws.recv(timeout=CHANNEL_TIMEOUT))
            if hello is None or hello.get("op") == RECONNECT:
                ws.close(KEEP_SESSION)
                return RESUME_NEXT, "", False
            if hello.get("op") != HELLO:
                ws.close(KEEP_SESSION)
                return RESUME_NEXT, t("网关先发来的不是 Hello"), False
            interval = float((hello.get("d") or {}).get("heartbeat_interval") or 41_250) / 1000
            if resuming:
                self._send(ws, RESUME, self.rules.resume(self.session_id, self.seq or 0))
            else:
                self._send(ws, IDENTIFY, self.rules.identify())
            acked = True
            due = time.monotonic() + interval * random.random()
            while not self.stopping.is_set():
                now = time.monotonic()
                if now >= due:
                    if not acked:  # 上一次心跳没人回：连接僵死了
                        ws.abort()
                        return RESUME_NEXT, t("心跳没有回应，正在重连"), ready
                    self._send(ws, HEARTBEAT, self.seq)
                    acked, due = False, now + interval
                message = self._decode(ws.recv(timeout=min(1.0, max(0.05, due - now))))
                if message is None:
                    continue
                op = message.get("op")
                if op == DISPATCH:
                    if isinstance(message.get("s"), int):
                        self.seq = message["s"]
                    kind, data = str(message.get("t") or ""), message.get("d")
                    data = data if isinstance(data, dict) else {}
                    if kind == "READY":
                        self.session_id = str(data.get("session_id") or "")
                        self.resume_url = str(data.get("resume_gateway_url") or "")
                        self.user = me if isinstance(me := data.get("user"), dict) else {}
                        ready = True
                        self.set_status("online", "")
                    elif kind == "RESUMED":
                        ready = True
                        self.set_status("online", "")
                    try:
                        self.on_dispatch(kind, data)
                    except Exception:
                        logger.exception("%s: 处理一个事件时出错", self.rules.name)
                elif op == HEARTBEAT:
                    self._send(ws, HEARTBEAT, self.seq)
                elif op == HEARTBEAT_ACK:
                    acked = True
                elif op == RECONNECT:
                    ws.close(KEEP_SESSION)
                    return RESUME_NEXT, "", ready
                elif op == INVALID_SESSION:
                    resumable = message.get("d") is True
                    ws.close(KEEP_SESSION)
                    if not resumable:
                        self.stopping.wait(random.uniform(1, 5))  # 重新登录之前等一小会儿
                    return (RESUME_NEXT if resumable else IDENTIFY_NEXT), "", ready
            ws.close(1000)
            return RESUME_NEXT, "", ready
        except ConnectionClosed as exc:
            outcome, why = self.rules.classify(exc.code)
            return outcome, why, ready
        except WebSocketError as exc:
            ws.abort()
            return RESUME_NEXT, str(exc), ready
        except ChannelError as exc:  # 登录要用的东西现在拿不到（QQ 换 access_token 时连不上）：断开，等一会儿重连
            ws.abort()
            return RESUME_NEXT, str(exc), ready

    def _decode(self, raw: str | bytes | None) -> dict | None:
        if raw is None:
            return None
        try:
            message = json.loads(raw)
        except ValueError:
            return None
        return message if isinstance(message, dict) else None
