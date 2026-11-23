"""聊天里来的消息该交给谁：是主人吗？是命令吗？是在答复确认请求吗？还是一句要交给助手的话？

    通道收到一条消息（Inbound）或者一次按钮（Press）
      └─► deliver（在通道收消息的线程里，要快：只分派，不等）
            ├─ 不是私聊                      → 不理（群里的每个人都能看到、都能发）
            ├─ 发消息的人不是主人             → 按配对的规则处理（见 pairing.py），多半是一个字都不回
            ├─ /new /stop /status /help       → 马上处理、马上回答
            ├─ 有确认请求在等，回的是"允许""拒绝" → 当作答复
            └─ 别的                           → 交给这个聊天的 Session，排队跑一轮（见 session.py）

要发到聊天里的话（回答、确认请求、提示，还有助手用 send_file 发的文件）不在这里直接发，而是排进这个平台的发送队列
（outbox.py 的 Outbox），由一个单独的线程按顺序发出去：网络可能慢，agent 循环和收消息的线程都不该等它；顺序也不能乱，
一段回答和紧跟着它的确认请求，在聊天里也得是这个顺序。发不出去的过一会儿再试、还是不行就留着等连上了补发，也在那里。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from ..agents import AgentError
from ..claims import Claims
from ..config import APP_NAME, PAIRING_ATTEMPTS
from ..i18n import message_of, t
from . import pairing
from .base import (
    Capabilities,
    Channel,
    ChannelError,
    Event,
    Inbound,
    OutboundFile,
    Press,
    Prompt,
)
from .outbox import Job, Outbox
from .platforms import label
from .session import Session, Waiting
from .store import ChannelStore

logger = logging.getLogger("harness.channels")

# 有确认请求在等的时候，回这些话就是答复（不分大小写，去掉标点）。三种界面语言的说法都认
ALLOW_WORDS = {"允许", "同意", "可以", "好", "好的", "是", "行", "y", "yes", "ok", "okay", "allow", "approve", "sí", "si", "vale", "permitir"}
DENY_WORDS = {"拒绝", "不", "不行", "不要", "否", "别", "n", "no", "deny", "reject", "denegar", "rechazar"}


def commands() -> dict[str, str]:
    """聊天里能用的命令，以及登记到平台菜单里的说明（按现在的界面语言）。"""
    return {
        "new": t("开始一段新对话（后面可以跟一个 Agent 的名字）"),
        "stop": t("停下正在进行的这一轮"),
        "status": t("现在在做什么、用的哪个模型、花了多少"),
        "help": t("怎么用"),
    }


def help_text(abilities: Capabilities | None = None) -> str:
    """/help 的回答。abilities 是这个平台能做什么：命令怎么写（Slack 是 /zhifox new），确认请求是按钮还是回复文字。"""
    abilities = abilities or Capabilities(buttons=True, max_chars=4000, typing=False, files=True)
    return "\n".join(
        [
            t("直接发消息就行，和在网页上一样。也可以发图片和文件（PDF、Word、Excel、文本……）。"),
            "",
            *(f"{abilities.spell(name)}  {text}" for name, text in commands().items()),
            "",
            t("要你批准的操作会带着“允许 / 拒绝”按钮发过来，命令和工作目录都写在上面，看清楚再点。")
            if abilities.buttons
            else t("要你批准的操作会先发一条消息过来，命令和工作目录都写在上面，看清楚再回复“允许”或“拒绝”。"),
        ]
    )


class Router:
    def __init__(self, app: Any, store: ChannelStore, claims: Claims, changed: Callable[[], None], saved: Callable[[str], None]):
        self.app = app
        self.store = store
        self.claims = claims
        self._changed = changed  # 有变化，页面要刷新（hub.py 接到网页的 publish_state 上）
        self._saved = saved  # 一段会话刚存完盘（网页正开着它的话，跟着刷新）
        self._lock = threading.Lock()
        self._sessions: dict[tuple[str, str], Session] = {}
        self._channels: dict[str, Channel] = {}
        self._outboxes: dict[str, Outbox] = {}

    # ---- 通道接上、断开（hub.py 用）--------------------------------------------

    def attach(self, platform: str, channel: Channel) -> None:
        with self._lock:
            self._channels[platform] = channel
            if platform not in self._outboxes:  # 通道换了（换 token、关掉又打开），发送队列和它留着没发出去的还是那一份
                self._outboxes[platform] = Outbox(platform, lambda: self._channels.get(platform), self.changed)

    def detach(self, platform: str) -> None:
        with self._lock:
            self._channels.pop(platform, None)

    def attached(self, platform: str) -> bool:
        """这个平台现在有没有一个开着的通道。"""
        return platform in self._channels

    def capabilities(self, platform: str) -> Capabilities:
        channel = self._channels.get(platform)
        return channel.capabilities if channel is not None else Capabilities(buttons=False, max_chars=4000, typing=False, files=False)

    def changed(self) -> None:
        self._changed()

    def saved(self, conversation_id: str) -> None:
        self._saved(conversation_id)

    # ---- 往聊天里发（都经过发送队列）------------------------------------------

    def _outbox(self, platform: str) -> Outbox | None:
        with self._lock:
            return self._outboxes.get(platform)

    def _later(self, platform: str, job: Job) -> None:
        """排进发送队列（真正发的时候才去找通道，见 outbox.Job）。"""
        outbox = self._outbox(platform)
        if outbox is not None:
            outbox.put(job)

    def say(self, platform: str, chat: str, text: str, markdown: bool = False) -> None:
        self._later(platform, Job("say", chat, lambda channel: channel.send(chat, str(text), markdown)))

    def report(self, platform: str, chat: str, compose: Callable[[], str]) -> None:
        """/status 的回答：轮到它发的时候才写（compose），写的是那一刻的样子；不排在这个聊天没发出去的那些后面（见 outbox.KINDS）。"""
        self._later(platform, Job("status", chat, lambda channel: channel.send(chat, str(compose()))))

    def ask(self, platform: str, chat: str, prompt: Prompt, waiting: Waiting) -> None:
        def run(channel: Channel) -> None:
            waiting.handle = channel.ask(chat, prompt)

        self._later(platform, Job("ask", chat, run, waiting=waiting))

    def settle(self, platform: str, chat: str, waiting: Waiting, outcome: str) -> None:
        # waiting.handle 是发确认请求的那一步填上的。两步排在同一个队里，到这里时它已经发完了；它没发出去、还留着的话，
        # 这一步也跟着留着，补发时排在它后面（见 outbox.py）
        self._later(platform, Job("settle", chat, lambda channel: channel.settle(chat, waiting.handle, outcome) if waiting.handle else None))

    def send_file(self, platform: str, chat: str, outgoing: OutboundFile, cancelled: threading.Event, timeout: float) -> str:
        """把一个文件排进发送队列，等它发完（send_file 工具要知道发没发出去，见 outbound.py）。

        排在队列里、而不是直接发：助手发文件之前说的话可能还没发出去，聊天里的顺序要和它做的一样。
        连不上这类暂时的失败，发送队列会再试几次（和别的消息一样，见 outbox.py），但不留着以后补发：模型在等结果。
        返回 sent（发出去了）、skipped（还没轮到就被 /stop 了，没有发）、pending（/stop 了或者等了 timeout 秒，
        这时它可能正在上传：不知道发没发出去；之后也不再试）。平台拒绝了、再试几次还是连不上，抛出 ChannelError。
        """
        outbox = self._outbox(platform)
        if outbox is None or not self.attached(platform):
            raise ChannelError(t("{platform} 现在没有连着。", platform=label(platform)))
        finished = threading.Event()
        gave_up = threading.Event()  # 等了 timeout 秒还没结果：发送队列那边也别再试了（模型已经被告知"不知道发没发出去"）
        outcome: dict[str, Any] = {}

        def done(state: str, error: ChannelError | None) -> None:
            outcome.update(state=state, error=error)
            finished.set()

        def stopped() -> bool:
            return cancelled.is_set() or gave_up.is_set()

        outbox.put(Job("file", chat, lambda channel: channel.send_file(chat, outgoing), cancelled=stopped, done=done))
        deadline = time.monotonic() + timeout
        while not finished.wait(0.1):
            if cancelled.is_set() or time.monotonic() > deadline:
                gave_up.set()
                return "pending"
        error = outcome.get("error")
        if error is not None:
            raise error
        return outcome.get("state", "sent")

    def typing(self, platform: str, chat: str) -> None:
        outbox = self._outbox(platform)
        if outbox is not None and platform in self._channels:
            outbox.typing(chat, lambda channel: channel.typing(chat))

    def set_commands(self, platform: str) -> None:
        self._later(platform, Job("menu", "", lambda channel: channel.set_commands(commands())))

    def status_changed(self, platform: str) -> None:
        """这个平台的通道状态变了（hub.py 告诉的）。变成在线：没发出去的马上按顺序补发，正在等着再试的马上试。"""
        channel = self._channels.get(platform)
        outbox = self._outbox(platform)
        if channel is not None and outbox is not None and channel.status.state == "online":
            outbox.resume()

    def delivery(self, platform: str) -> dict:
        """这个平台还有几条没发出去、几条不再补发了、最后一次失败的时间和原因（页面上通道那一行照着写，见 Outbox.summary）。"""
        outbox = self._outbox(platform)
        return outbox.summary() if outbox is not None else {"waiting": 0, "lost": 0, "failed_at": None, "error": ""}

    def undelivered(self, platform: str, chat: str) -> int:
        """这个聊天还有几条没发出去。"""
        outbox = self._outbox(platform)
        return outbox.waiting(chat) if outbox is not None else 0

    def flush(self, timeout: float = 5.0) -> None:
        """等所有平台排着的话都发完（测试里用）。"""
        with self._lock:
            outboxes = list(self._outboxes.values())
        for outbox in outboxes:
            outbox.flush(timeout)

    # ---- 收消息 --------------------------------------------------------------

    def deliver(self, event: Event) -> None:
        """通道交来一条消息或者一次按钮（在通道收消息的线程里调用）。"""
        self.store.refresh()  # 命令行里可能刚生成了配对码、换了确认方式
        outbox = self._outbox(event.platform)
        if outbox is not None:
            outbox.resume()  # 收得到消息，多半也发得出去：没发出去的马上再试
        if isinstance(event, Press):
            self._press(event)
        else:
            self._message(event)

    def _message(self, message: Inbound) -> None:
        if not message.private:
            return  # 群聊：不理，也不回（见 pairing.py 开头的说明）
        platform, text = message.platform, message.text.strip()
        owner = self.store.is_owner(platform, message.user)
        if not owner or (self.store.pairing(platform) and pairing.find_code(text)):
            self._stranger(message)  # 正在配对时主人自己发来配对码（重新配对），也走这里
            return
        command = text.partition(" ")[0]
        command = command.split("@", 1)[0].lower() if command.startswith("/") else ""  # 群里会写成 /new@机器人的名字
        session = self._session(platform, message.chat, message.name)
        if command in ("/start", "/help"):
            self.say(platform, message.chat, help_text(self.capabilities(platform)))
        elif command == "/new":
            self._new(session, message, text.partition(" ")[2].strip())
        elif command == "/stop":
            stopped = session.stop()
            self.say(platform, message.chat, t("好，正在停下。") if stopped else t("现在没有在进行的事。"))
        elif command == "/status":
            self.report(platform, message.chat, session.status_text)
        elif (waiting := _answerable(session)) is not None and _word(text) in ALLOW_WORDS | DENY_WORDS and not message.files:
            allowed = _word(text) in ALLOW_WORDS
            # 用上面拿到的这一个：再读一遍 session.waiting 的话，网页可能刚好在这中间答掉了它（变成 None）
            if not session.answer(waiting.id, allowed, "chat"):
                self.say(platform, message.chat, t("这个确认请求已经处理过了"))
        else:
            self._submit(session, message)
        self.changed()

    def _new(self, session: Session, message: Inbound, persona: str) -> None:
        """/new，或者 /new 某个 Agent 的名字（新的一段用它当角色）。"""
        platform, chat = message.platform, message.chat
        if persona:
            try:
                persona = self.app.agents.usable(persona).name
            except AgentError as exc:
                self.say(platform, chat, t("没法用「{name}」开始：{reason}", name=persona, reason=message_of(exc)))
                return
        if not session.new_conversation(persona):
            self.say(platform, chat, t("这一轮还没结束。先发 {stop} 停下它，再开始新对话。", stop=self.capabilities(platform).spell("stop")))
        elif persona:
            self.say(platform, chat, t("好，开始一段新对话，由「{agent}」来回答。之前的那段还在网页的会话列表里。", agent=persona))
        else:
            self.say(platform, chat, t("好，开始一段新对话。之前的那段还在网页的会话列表里。"))

    def _submit(self, session: Session, message: Inbound) -> None:
        waiting = session.waiting
        ahead = session.submit(message)
        if ahead is None:
            self.say(message.platform, message.chat, t("排着的消息太多了，等前面的处理完再发吧。"))
        elif waiting is not None and self.capabilities(message.platform).buttons:
            self.say(message.platform, message.chat, t("还有一个操作在等你批准：点上面的按钮，或者回复“允许”或“拒绝”。你的这条消息排在它后面。"))
        elif waiting is not None:
            self.say(message.platform, message.chat, t("还有一个操作在等你批准：回复“允许”或“拒绝”。你的这条消息排在它后面。"))
        elif ahead > 0:
            self.say(message.platform, message.chat, t("收到。前面还有 {n} 件事，做完就轮到这一条。", n=ahead))

    def _stranger(self, message: Inbound) -> None:
        """不是主人发来的消息（或者正在配对时发来的配对码）。"""
        outcome, reply = pairing_reply(self.store, message, self.capabilities(message.platform))
        if outcome == "paired":
            # 换了主人：以前那个账号的聊天里还在进行的、排着的，都停下（之后他发的消息也不再理）
            with self._lock:
                old = [key for key in self._sessions if key[0] == message.platform and key[1] != message.chat]
                sessions = [self._sessions.pop(key) for key in old]
            for session in sessions:
                session.close()
            outbox = self._outbox(message.platform)
            if outbox is not None:
                outbox.forget(lambda chat: chat != message.chat)  # 以前那个账号的聊天里没发出去的，也不再发
        if reply:
            self.say(message.platform, message.chat, reply)
        if outcome != "hint":
            self.changed()  # 配对成了、猜错了一次、又忽略了一条：页面上的数字跟着变

    def _press(self, press: Press) -> None:
        """有人点了按钮。只认主人在自己的私聊里点的"允许 / 拒绝"。"""
        kind, _, number = press.data.partition(":")
        session = self._sessions.get((press.platform, press.chat))
        if not self.store.is_owner(press.platform, press.user) or session is None or kind not in ("ok", "no") or not number.isdigit():
            press.answer("")
            return
        if session.answer(int(number), kind == "ok", "chat"):
            press.answer(t("已允许") if kind == "ok" else t("已拒绝"))
        else:
            press.answer(t("这个确认请求已经处理过了"), True)

    def _session(self, platform: str, chat: str, name: str) -> Session:
        with self._lock:
            session = self._sessions.get((platform, chat))
            if session is None:
                session = self._sessions[(platform, chat)] = Session(self, platform, chat, name)
            session.name = name
            return session

    # ---- 网页上的操作（hub.py 转过来的）----------------------------------------

    def confirm(self, platform: str, chat: str, confirm_id: int, allow: bool) -> bool:
        """在网页上答复聊天里的一个确认请求。和聊天里的答复比谁先到，先到的算数。"""
        session = self._sessions.get((platform, chat))
        return session is not None and session.answer(confirm_id, allow, "web")

    def stop(self, platform: str, chat: str) -> bool:
        session = self._sessions.get((platform, chat))
        if session is None or not session.stop():
            return False
        self.say(platform, chat, t("已在网页上停下这一轮。"))
        return True

    def holder(self, conversation_id: str) -> str:
        """这段会话正在哪个通道里跑一轮（给人看的平台名字）；没有在跑返回空字符串。"""
        who = self.claims.holder(conversation_id)
        if who is None or who == "web":
            return ""
        return label(who.partition(":")[0])

    def sessions(self, platform: str) -> list[Session]:
        with self._lock:
            return [session for (name, _), session in self._sessions.items() if name == platform]

    def forget(self, platform: str) -> None:
        """这个平台被删掉了、解除了绑定：停下它的聊天，忘掉它们，没发出去的也不再补发。"""
        with self._lock:
            gone = [key for key in self._sessions if key[0] == platform]
            sessions = [self._sessions.pop(key) for key in gone]
        for session in sessions:
            session.close()
        outbox = self._outbox(platform)
        if outbox is not None:
            outbox.forget(lambda chat: True)

    def close(self, timeout: float) -> None:
        """退出：停下所有聊天里正在进行的一轮，等它们存好盘（最多 timeout 秒），把排着的话发完（不再等着重试，见 Outbox.close）。"""
        with self._lock:
            sessions = list(self._sessions.values())
            outboxes = list(self._outboxes.values())
        for session in sessions:
            session.close()
        deadline = time.monotonic() + timeout
        for session in sessions:
            session.join(max(0.0, deadline - time.monotonic()))
        for outbox in outboxes:
            outbox.close(max(0.5, deadline - time.monotonic()))


def pairing_reply(store: ChannelStore, message: Inbound, abilities: Capabilities | None = None) -> tuple[str, str]:
    """按配对的规则处理一个还不是主人的人发来的消息（见 pairing.py、store.attempt）。返回（发生了什么，要回他的话）。

    聊天里的 Router 和命令行里的 `harness channels pair`（见 cli.py）都用这一个，回的话一模一样。
    """
    platform = message.platform
    if not message.private:
        return "ignore", ""
    try:
        outcome = store.attempt(platform, message.user, message.name, message.text)
    except ChannelError:
        return "ignore", ""  # 设置文件坏了：什么都不做
    if outcome == "paired":
        logger.info("%s: 配对成功，主人是 %s", platform, message.name)
        return outcome, t("配对成功！以后只有你（{name}）能在这里使用{app}。", name=message.name, app=APP_NAME) + "\n\n" + help_text(abilities)
    if outcome == "wrong":
        code = store.pairing(platform)
        left = PAIRING_ATTEMPTS - code["attempts"] if code else 0
        return outcome, t("配对码不对或者已经过期。还可以再试 {n} 次。", n=left)
    if outcome == "locked":
        logger.warning("%s: 配对码猜错的次数太多，已经作废", platform)
        return outcome, t("配对码错的次数太多，已经作废了。请重新生成一个。")
    if outcome == "hint":
        return outcome, t("你好！要开始使用，请把网页上（或者命令行里）显示的 6 位配对码发给我。")
    logger.debug("%s: 忽略了一条陌生人的消息", platform)
    return outcome, ""


def _answerable(session: Session) -> Waiting | None:
    """这个聊天里可以用文字答复的那个确认请求（没有的话是 None）。

    要等它真的发到了聊天里（handle 有了）才算：发送队列里可能还排着它（网络慢、被限流），
    这时你回的"好"是在回上一句话，不能拿来批准一条你还没看到的命令。
    """
    waiting = session.waiting
    return waiting if waiting is not None and waiting.handle else None


def _word(text: str) -> str:
    """把一句很短的回复整理成可以和 ALLOW_WORDS 比的样子：小写，去掉首尾的标点和空白。"""
    return re.sub(r"^[\W_]+|[\W_]+$", "", text.strip().lower())
