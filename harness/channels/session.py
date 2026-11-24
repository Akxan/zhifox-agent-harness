"""聊天里的一个对话：一个聊天（比如你和 Telegram 机器人的私聊）接着一段智狐的会话。

每个聊天有自己的一切：

  会话      聊过的内容存成一段普通的会话（会话列表里标着"Telegram"），重启以后接着聊，发 /new 开始新的一段；
            /new 后面跟一个自定义 Agent 的名字，新的一段就用它当角色（和网页上用它开始一段对话一样，见 App.new_conversation）
  助手      自己的适配层、权限（这个通道的那一档）和 shell（见 App.channel_agent）；还有一个"去处"（outbound.Outlet）：
            助手用 send_file 工具发的文件就发到这个聊天里
  队伍      同一个聊天里一次只跑一轮。正在跑的时候又来了消息，排在后面，跑完一轮接着下一轮
  线程      跑这些轮的线程。它和网页上的对话、别的聊天各跑各的，谁也不等谁

一轮是怎么跑的（_turn）：下载附件 → 准备会话和助手（_prepare）→ 跑 agent 循环（和网页上用的是同一个 run_turn）→
存盘 → 告诉网页一声（网页正开着这段会话的话，跟着刷新）。中间写完的每一段文字、要你确认的操作，
由 ChannelUI 交给这里，再经过 router.py 的发送队列发到聊天里。

一段会话可能也在网页上打开着：开始一轮之前先"认领"它（见 ../claims.py），并从磁盘读一遍最新的，
网页上接着聊过、改过名、删掉了，这边都能知道。

这个文件由几块拼成（2026-11-24 按小节拆开，代码一个字没改；别处照旧 from .session import Session, Waiting、delivery_lines，
交出去的名字写在 __all__ 里）：

  session.py               上面这段说明；Session 的状态（__init__）和外面（router.py、hub.py）用的：收消息、/stop、答复确认、/new、
                           /status、页面上的样子、退出；/status 里"发不出去的"那几行（delivery_lines）
  session_turn.py          跑一轮（_turn）：排队的线程、附件、语音、"正在输入"、一轮结束关掉浏览器标签页
  session_conversation.py  这一轮用的会话和助手：准备（_prepare：认领、接着哪一段、建助手）、读、新开、跟上网页上的改动、存盘
  session_confirm.py       确认请求：发到聊天里（_asked）、答复以后改掉那条消息（_settled）；Waiting
  session_types.py         SessionPart：混进 Session 的几组方法要用的东西，只写给 mypy 看

Session 和 App 一样由混进来的基类拼成（SessionTurn、SessionConversation、SessionConfirm）；拆出去的文件只往前 import
（session_confirm.py 从 session_turn.py 拿 logger），不 import 这个文件。测试换名字要换在读它的函数所在的那个模块上：
比如一轮里读 find 的 _turn 在 session_turn.py。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any

from ..config import CHANNEL_QUEUE
from ..conversations import Conversation
from ..i18n import k, t
from ..permissions import MODES
from ..providers import find
from ..tasks import retell
from .base import ChannelError, Inbound
from .outbound import Outlet
from .session_confirm import SessionConfirm, Waiting
from .session_conversation import SessionConversation
from .session_turn import UNSUPPORTED, VOICE_KINDS, SessionTurn, logger
from .ui import ChannelUI

if TYPE_CHECKING:
    from .router import Router

__all__ = [
    "AUTO_SAID",
    "AUTO_SAY",
    "UNSUPPORTED",
    "VOICE_KINDS",
    "Session",
    "Waiting",
    "delivery_lines",
    "logger",
]

# 没问你就放行了一步时，聊天里发的那一行（为什么不问，见 ../permissions_allowance.py 的 Permissions.allowance）
AUTO_SAY = k("已自动允许：{action}")
AUTO_SAID = {"readonly": k("已自动允许（只读）：{action}"), "listed": k("已自动允许（你加的命令）：{action}")}


class Session(SessionTurn, SessionConversation, SessionConfirm):
    def __init__(self, router: Router, platform: str, chat: str, name: str):
        self.router = router
        self.platform = platform
        self.chat = chat
        self.name = name  # 主人给人看的名字
        self.key = f"{platform}:{chat}"  # 认领会话时用的名字（见 ../claims.py）
        self._lock = threading.Lock()
        self._queue: deque[Inbound] = deque()
        self._worker: threading.Thread | None = None
        self._closed = False
        self._stop_asked = False  # 这一轮被 /stop 了（begin_turn 会放下 WebUI 的旗子，所以另记一份，开始以后再立一次）
        self.running = False  # 正在跑一轮
        self.started_at: float | None = None
        self.conversation: Conversation | None = None
        self.agent: Any = None
        self.run: Any = None  # App.channel_agent 给的 UnattendedRun：这个聊天自己的 shell、MCP 调用，以及停下它们的办法
        self.waiting: Waiting | None = None
        self.persona = ""  # /new 某个 Agent：下一段新会话用它当角色（开始以后记在 conversation.agent 里）
        self.ui = ChannelUI(
            on_text=lambda text: router.say(platform, chat, text, markdown=True),
            on_notice=lambda level, text: router.say(platform, chat, text),
            on_confirm=self._asked,
            on_settled=self._settled,
            on_auto=lambda action, why: router.say(platform, chat, t(AUTO_SAID.get(why, AUTO_SAY), action=action)),
        )
        self.ui.on_stop(self._kill)
        self.outlet = Outlet(router, platform, chat, self.ui, lambda: self.name)  # send_file 发的文件发到这个聊天里

    # ---- 外面用的 ------------------------------------------------------------

    def submit(self, inbound: Inbound) -> int | None:
        """收下一条消息。返回它前面还有几轮（0 表示马上开始）；排满了返回 None。"""
        with self._lock:
            if self._closed:
                return None
            ahead = len(self._queue) + (1 if self.running else 0)
            if len(self._queue) >= CHANNEL_QUEUE:
                return None
            self._queue.append(inbound)
            if self._worker is None:
                self._worker = threading.Thread(target=self._work, daemon=True, name=f"chat-{self.key}")
                self._worker.start()
        return ahead

    def stop(self) -> bool:
        """/stop：排着的不跑了，正在跑的停下（连同它正在运行的命令）。返回有没有东西被停下。"""
        with self._lock:
            dropped = bool(self._queue)
            self._queue.clear()
            running = self.running
            if running:
                self._stop_asked = True
        if running:
            self.ui.request_stop()
        return dropped or running

    def answer(self, confirm_id: int, allow: bool, by: str) -> bool:
        """答复正在等着的确认请求（在聊天里点了按钮、回了"允许"，或者在网页上点了）。先到的算数。"""
        waiting = self.waiting
        if waiting is None or waiting.id != confirm_id:
            return False
        # 先记下是谁答的，再交出答复：agent 的线程一拿到答复就会醒来，马上去写"已在网页上允许"还是"已允许"。
        # 记名字要拿着锁、而且只记第一个：晚到的一方不能先把名字改成自己、没答上再改回去——agent 的线程
        # 正好在这个间隙里写结果的话，"已在网页上允许"就会被写成"已允许"
        with self._lock:
            if waiting.by:
                return False
            waiting.by = by
        if not self.ui.answer_confirm(confirm_id, allow, ""):
            with self._lock:
                if waiting.by == by:
                    waiting.by = ""  # 没答上（这个确认已经结束了）：名字也不留
            return False
        return True

    def new_conversation(self, persona: str = "") -> bool:
        """/new：下一条消息开始一段新的会话（用那时设置里选的模型和提示词；persona 是一个 Agent 的名字的话，用它当角色）。
        正在跑的时候不行，返回 False。"""
        with self._lock:
            if self.running:
                return False
            self.conversation = None
            self.agent = self.run = None
            self.persona = persona
        self.ui.reset()
        try:
            self.router.store.remember_chat(self.platform, self.chat, "")
        except ChannelError:
            pass  # 设置文件坏了：记不下来也不要紧，内存里已经换了
        return True

    def holds(self, conversation_id: str) -> bool:
        conversation = self.conversation
        return conversation is not None and conversation.id == conversation_id

    def queued(self) -> int:
        with self._lock:
            return len(self._queue)

    def snapshot(self) -> dict:
        """给页面看的样子（见 hub.py 的 snapshot）。"""
        with self._lock:
            queued, running, started = len(self._queue), self.running, self.started_at
        conversation, waiting, agent = self.conversation, self.waiting, self.agent
        state = "waiting" if waiting is not None else "running" if running else "idle"
        return {
            "chat": self.chat,
            "name": self.name,
            "state": state,
            "title": conversation.title if conversation is not None else "",
            "conversation": conversation.id if conversation is not None and conversation.saved else "",
            "activity": self.ui.activity if running else "",
            "step": agent.usage.requests if agent is not None and running else 0,
            "queued": queued,
            "started_at": started if running else None,
            "undelivered": self.router.undelivered(self.platform, self.chat),  # 还有几条没发到这个聊天里（见 outbox.py）
            # 确认请求上的那句话按现在的界面语言重新译（你可能在它等着的时候换了语言）。delivery：它发到聊天里了没有
            "confirm": {"id": waiting.id, "summary": retell(waiting.summary), "delivery": waiting.delivery, "problem": retell(waiting.problem)} if waiting is not None else None,
        }

    def status_text(self) -> str:
        """/status 的回答。"""
        router = self.router
        app = router.app
        conversation, agent = self.conversation, self.agent
        if agent is not None:
            found = find(agent.backend.provider)
            provider, model = found.label if found is not None else agent.backend.provider, agent.backend.model
        else:
            provider, model = app.provider.label, app.settings.model
        lines: list[str] = [  # 有一行是 t() 再接上一段文字（得到普通的 str），所以不是 list[Message]
            t("模型：{model}", model=f"{provider} · {model}"),
            t("确认方式：{mode}", mode=t(MODES[router.store.get(self.platform)["approval"]])),
            t("会话：{title}", title=conversation.title if conversation is not None and conversation.title else t("新对话")),
        ]
        persona = conversation.agent if conversation is not None else self.persona
        if persona:
            lines.append(t("角色：{agent}", agent=persona))
        with self._lock:
            running, queued = self.running, len(self._queue)
        if self.waiting is not None:
            lines.append(t("现在：等你批准"))
        elif running:
            step = agent.usage.requests if agent is not None else 0
            lines.append(t("现在：正在进行第 {n} 步", n=max(step, 1)) + (f" · {self.ui.activity}" if self.ui.activity else ""))
        else:
            lines.append(t("现在：空闲"))
        if queued:
            lines.append(t("排队：{n} 条消息", n=queued))
        lines.extend(delivery_lines(router.delivery(self.platform), router.undelivered(self.platform, self.chat)))
        spend = app.spending()
        lines.append(t("今天花了 ${today}，本月 ${month}", today=f"{spend['today']:.4f}", month=f"{spend['month']:.4f}"))
        return "\n".join(lines)

    def close(self) -> None:
        """程序要退出了：不再收新消息，排着的不跑了，正在跑的停下（它会自己收尾、存好盘）。"""
        with self._lock:
            self._closed = True
            self._queue.clear()
            running = self.running
            self._stop_asked = running
        if running:
            self.ui.request_stop()

    def join(self, timeout: float) -> bool:
        worker = self._worker
        if worker is not None:
            worker.join(timeout)
            return not worker.is_alive()
        return True


def delivery_lines(summary: dict, here: int) -> list[str]:
    """/status 里"发不出去的"那几行（summary 是 Outbox.summary 给的；here 是这个聊天里还有几条没发出去）。
    都发出去了、也没有丢过，一行都不写。/status 的回答不排在没发出去的那些后面（见 outbox.KINDS），所以你看得到这几行。"""
    lines: list[str] = []
    if here:
        lines.append(t("还没发到这里的消息：{n} 条（连上以后会按顺序补发）", n=here))
    if summary["lost"]:
        lines.append(t("没能发出去的消息：{n} 条", n=summary["lost"]))
    if summary["failed_at"] and (here or summary["lost"]):
        lines.append(t("最后一次失败：{when}（{reason}）", when=_clock(summary["failed_at"]), reason=retell(summary["error"])))
    return lines


def _clock(moment: float) -> str:
    """一个时刻写成 14:05；不是今天的写成 11-22 14:05。"""
    local = time.localtime(moment)
    return time.strftime("%H:%M" if local[:3] == time.localtime()[:3] else "%m-%d %H:%M", local)
