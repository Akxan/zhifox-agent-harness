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
"""

from __future__ import annotations

import logging
import textwrap
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..agents import AgentError, CustomAgent
from ..app import run_turn
from ..attachments import Attachment, AttachmentError, from_bytes, unsupported
from ..config import CHANNEL_QUEUE, CHANNEL_TYPING_SECONDS
from ..conversations import Conversation, ConversationError, make_title
from ..i18n import k, message_of, t
from ..permissions import MODES
from ..providers import caps, find, has_key
from ..tasks import retell
from ..voice import VoiceError, listen
from .base import ChannelError, Inbound, Prompt
from .outbound import Outlet
from .platforms import LABELS
from .ui import ChannelUI

if TYPE_CHECKING:
    from .router import Router

logger = logging.getLogger("harness.channels")

# 发来的东西是什么，给人看的说法
UNSUPPORTED = {
    "voice": k("语音"),
    "audio": k("音频"),
    "video": k("视频"),
    "video_note": k("视频"),
    "sticker": k("贴纸"),
    "animation": k("动图"),
    "location": k("位置"),
    "contact": k("联系人"),
    "poll": k("投票"),
}


# 语音和音频：Telegram 会交给语音转文字（见 ../voice/），别的平台还没接，收到时说"暂时只支持 Telegram"
VOICE_KINDS = {"voice", "audio"}


# 没问你就放行了一步时，聊天里发的那一行（为什么不问，见 permissions.Permissions.allowance）
AUTO_SAY = k("已自动允许：{action}")
AUTO_SAID = {"readonly": k("已自动允许（只读）：{action}"), "listed": k("已自动允许（你加的命令）：{action}")}


@dataclass
class Waiting:
    """一个发到聊天里、还没答复的确认请求。"""

    id: int  # WebUI 里确认请求的编号，按钮上带着它
    summary: str  # 要做的事（Permissions.review 写的那段话）
    handle: str = ""  # 聊天里那条消息的编号（发出去以后才有）
    by: str = ""  # 谁答复的：chat（在聊天里）或 web（在网页上）


class Session:
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
            # 确认请求上的那句话按现在的界面语言重新译（你可能在它等着的时候换了语言）
            "confirm": {"id": waiting.id, "summary": retell(waiting.summary)} if waiting is not None else None,
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
        lines = [
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

    # ---- 跑一轮 --------------------------------------------------------------

    def _kill(self) -> None:
        """停下这个聊天正在做的事：它正在运行的命令、正在等的 MCP 调用、正在进行的浏览器操作。别人的不碰。"""
        run = self.run
        if run is not None:
            run.stop()

    def _work(self) -> None:
        while True:
            with self._lock:
                if not self._queue or self._closed:
                    self._worker = None
                    self.running = False
                    self.started_at = None
                    return
                inbound = self._queue.popleft()
                self.running, self.started_at = True, time.time()
                self._stop_asked = False
            self.router.changed()
            try:
                self._turn(inbound)
            except Exception:
                logger.exception("%s: 这一轮出错了", self.key)
                self.router.say(self.platform, self.chat, t("这一轮出错了，详细的原因写在智狐的日志里。"))
            finally:
                with self._lock:
                    self.running = False
                self.router.changed()

    def _turn(self, inbound: Inbound) -> None:
        say = lambda text: self.router.say(self.platform, self.chat, text)
        if inbound.unsupported:
            kind = UNSUPPORTED.get(inbound.unsupported, inbound.unsupported)
            if inbound.unsupported in VOICE_KINDS:
                # 语音转文字（见 ../voice/）暂时只接了 Telegram：别的平台收到语音时说清楚，免得以为是坏了
                say(t("还处理不了{kind}：语音转文字暂时只支持 Telegram。可以发文字、图片或者文件（PDF、Word、Excel、文本……）。", kind=t(kind)))
            else:
                say(t("还处理不了{kind}。可以发文字、图片或者文件（PDF、Word、Excel、文本……）。", kind=t(kind)))
            return
        try:
            attachments = self._attachments(inbound)
        except (AttachmentError, ChannelError) as exc:
            say(t("这个文件用不了：{error}", error=message_of(exc)))
            return
        text = inbound.text.strip()
        if not text and not attachments and inbound.audio is None:
            return
        try:
            problem = self._prepare()
        except BaseException:
            if self.conversation is not None:
                self.router.claims.release(self.conversation.id, self.key)  # 不放开的话，这段会话在网页上一直打不开
            raise
        if problem:
            say(problem)
            return
        conversation, agent = self.conversation, self.agent
        assert conversation is not None and agent is not None
        started = time.monotonic()
        try:
            # 收不收得了按这个模型算（自定义接口上同一家的模型能力不一样，见 providers.caps）
            provider = find(agent.backend.provider)
            accepts = caps(provider, agent.backend.model) if provider is not None else None
            label = provider.label if provider is not None else agent.backend.provider
            problem = unsupported(attachments, label, bool(accepts and accepts.vision), bool(accepts and accepts.pdf))
            if problem:
                say(problem)
                return
            # 一段语音：先转成文字（检查、下载、转、记账都在 voice/listen.py）。放在准备好会话之后：
            # 模型那边没有密钥这类"这一轮开始不了"的情况已经说过了，不会白花转文字的钱
            voice, sent = None, text
            if inbound.audio is not None:
                heard = self._hear(inbound)
                if heard is None:
                    return
                voice = heard.seconds
                text = "\n\n".join(part for part in (heard.text, text) if part)  # 语音附带的说明（caption）接在后面
                # 模型拿到的这一份开头写明是听写出来的（写给模型看，所以是中文、不翻译）：里面可能有听错的字
                sent = "（语音转文字）" + text
            if not conversation.title:
                conversation.title = make_title(text) or (make_title(attachments[0].name) if attachments else "")
            agent.permissions.mode = self.router.store.get(self.platform)["approval"]  # 你可能刚在页面上换过
            logger.info("%s: 开始一轮（%s · %s）", self.key, provider.label, agent.backend.model)
            done = threading.Event()
            if self.router.capabilities(self.platform).typing:
                threading.Thread(target=self._typing, args=(done,), daemon=True).start()
            # 记下这一轮从历史的哪个位置开始：网页上打开这段会话时，最后一轮能编辑、重新生成（见 rewind.py）
            self.ui.begin_turn(text, attachments, mark=agent.backend.mark(), voice=voice)
            if self._stop_asked:
                self.ui.request_stop()  # 准备的时候就被 /stop 了：begin_turn 刚把旗子放下，再立起来
            try:
                run_turn(agent, self.ui, sent, attachments)
            finally:
                done.set()
                self.ui.end_turn()
                self._save()
                self._close_browser()
            logger.info("%s: 这一轮结束，用了 %.1f 秒，调用了 %d 次模型", self.key, time.monotonic() - started, agent.usage.requests)
        finally:
            self.router.claims.release(conversation.id, self.key)
            self.router.saved(conversation.id)

    def _hear(self, inbound: Inbound):
        """把这条消息里的语音转成文字（见 ../voice/）。转好了先在聊天里回一句"（语音转文字）……"，让你看看听得对不对；
        转不了（没打开、太长、服务出错了）说清楚为什么，返回 None：这一轮不开始。"""
        say = lambda text: self.router.say(self.platform, self.chat, text)
        audio = inbound.audio
        assert audio is not None
        self.router.typing(self.platform, self.chat)  # 转一段长的要好几秒：先让对方看到"正在输入"
        try:
            heard = listen(self.router.app, audio)
        except VoiceError as exc:
            say(message_of(exc))
            return None
        finally:
            self.router.changed()  # 记了一笔账：页面上的"今天花了多少"跟着变
        if not heard.text:
            say(t("没有听出这段语音里说了什么。可以再说一遍，或者打字发过来。"))
            return None
        logger.info("%s: 转好了一段 %.0f 秒的语音", self.key, heard.seconds or 0)
        say(t("（语音转文字）{words}", words=heard.text))
        return heard

    def _close_browser(self) -> None:
        """一轮结束：关掉这个聊天自己开的浏览器标签页（和定时任务的一次运行一样，见 App._side_run 的 close）。
        下一轮要用浏览器时会开一个新的；你的对话和别的聊天的标签页不碰。"""
        run = self.run
        if run is not None:
            try:
                run.close()
            except Exception:  # 关不掉（浏览器已经被关了、崩了）也不能让这一轮的收尾出错
                logger.debug("%s: 关浏览器标签页时出错", self.key, exc_info=True)

    def _typing(self, done: threading.Event) -> None:
        """一轮还在进行，就隔一会儿发一次"正在输入"。等你批准的时候不发（那时候它不是在"输入"）。"""
        while not done.is_set():
            if self.waiting is None:
                self.router.typing(self.platform, self.chat)
            if done.wait(CHANNEL_TYPING_SECONDS):
                return

    def _attachments(self, inbound: Inbound) -> list[Attachment]:
        """把聊天里发来的文件下载下来，走和网页上一样的检查（见 attachments.from_bytes）。"""
        found = []
        for item in inbound.files:
            content = item.fetch()
            thumb = b""
            if item.thumb is not None:
                try:
                    thumb = item.thumb()
                except ChannelError:
                    thumb = b""  # 预览图下载不了也不要紧，原图在就行
            found.append(from_bytes(item.name, content, thumb))
        return found

    def _prepare(self) -> str:
        """准备好这一轮要用的会话和助手，并认领这段会话。返回不能开始的原因（空字符串表示可以开始）。"""
        app, store = self.router.app, self.router.app.store
        conversation = self.conversation
        if conversation is None:
            saved_id = self.router.store.conversation_of(self.platform, self.chat)
            loaded = self._load(saved_id) if saved_id else None
            if loaded is not None:
                self._adopt(loaded)
            else:
                problem = self._fresh()
                if problem:
                    return problem
        elif conversation.saved and not store.exists(conversation.id):
            self.router.say(self.platform, self.chat, t("之前那段会话已经在网页上删掉了，现在开始一段新的。"))
            problem = self._fresh()
            if problem:
                return problem
        conversation = self.conversation
        assert conversation is not None
        # 认领这段会话。网页上正在这段会话里跑一轮的话，等它跑完（每隔一会儿看看是不是要停了）
        while self.router.claims.take(conversation.id, self.key, timeout=0.5) is not None:
            if self._stop_asked or self._closed:
                return t("已停止。")
        if conversation.saved:
            self._catch_up(conversation)  # 网页上可能刚接着聊过、改过名
        conversation = self.conversation
        assert conversation is not None
        provider = find(conversation.provider)
        if provider is None:  # 这段会话用的自定义接口被删掉了
            self.router.claims.release(conversation.id, self.key)
            return t("这段会话用的模型接口（{provider}）已经被删掉了。发 /new 开始一段新的。", provider=conversation.provider)
        if not has_key(provider):
            self.router.claims.release(conversation.id, self.key)
            return t("还没有设置 {provider} 的 API 密钥，请先在智狐的网页界面（或者 .env 文件）里填写。", provider=provider.label)
        if self.agent is None:
            approval = self.router.store.get(self.platform)["approval"]
            self.run = app.channel_agent(
                self.ui, approval, conversation.provider, conversation.model, self._persona(conversation), outlet=self.outlet, sandbox=self._sandbox,
            )
            self.agent = self.run.agent
            # 交给你的钩子的 session_id：这个聊天此刻的那段会话（/new 以后换成新的一段）
            self.agent.hooks.session = lambda: self.conversation.id if self.conversation is not None else ""
            if conversation.messages:
                self.agent.backend.restore(conversation.messages, conversation.dropped)
            self.agent.usage.context_tokens = conversation.context_tokens
            self.agent.on_usage = self.router.changed
            self.agent.notes.append(_first_note(self.platform, self.router.capabilities(self.platform).buttons))
        return ""

    def _load(self, conversation_id: str) -> Conversation | None:
        """读一段保存过的会话。读不出来、或者它用的那一家现在没有密钥了，返回 None（那就开始一段新的）。"""
        try:
            conversation = self.router.app.store.load(conversation_id)
        except ConversationError:
            return None
        provider = find(conversation.provider)
        return conversation if provider is not None and has_key(provider) else None

    def _adopt(self, conversation: Conversation) -> None:
        """接着这段会话聊：它的事件放回日志，助手等用到时按它的模型、带着它的历史建好。"""
        conversation.channel = conversation.channel or self.platform
        self.conversation = conversation
        self.agent = self.run = None
        self.ui.load_events(conversation.events)

    def _persona(self, conversation: Conversation) -> CustomAgent | None:
        """这段会话的角色（用 /new 某个 Agent 开始的）。它被删掉了、现在不合格的话，说一声，用内置的提示词接着聊。"""
        if not conversation.agent:
            return None
        try:
            return self.router.app.agents.usable(conversation.agent)
        except AgentError as exc:
            self.router.say(self.platform, self.chat, t("这段会话是用 Agent「{name}」开始的，但它现在用不了（{reason}）。接着聊会用内置的提示词和全部工具。", name=conversation.agent, reason=message_of(exc)))
            return None

    def _fresh(self) -> str:
        """开始一段新的会话，用此刻设置里选的那一家、那个模型；/new 了某个 Agent 的话，用它当角色，模型用它选的那个（它写了的话）。"""
        app = self.router.app
        provider, model = app.provider, app.settings.model
        persona = None
        if self.persona:
            try:
                persona = app.agents.usable(self.persona)
            except AgentError as exc:
                self.persona = ""
                return t("Agent「{name}」现在用不了：{reason}", name=self.persona or "?", reason=message_of(exc))
            if persona.spec.model:
                chosen = find(persona.spec.provider)
                if chosen is None:  # 它的文件里写的是一个后来被删掉的自定义接口
                    return t("Agent「{name}」用的模型接口（{provider}）已经不在了。", name=persona.name, provider=persona.spec.provider)
                provider, model = chosen, persona.spec.model_id
        if not has_key(provider):
            return t("还没有设置 {provider} 的 API 密钥，请先在智狐的网页界面（或者 .env 文件）里填写。", provider=provider.label)
        conversation = app.store.new(provider.id, model)
        conversation.agent = persona.name if persona is not None else ""
        self.persona = ""
        conversation.channel = self.platform
        self.conversation = conversation
        self.agent = self.run = None
        self.ui.reset()
        return ""

    def _catch_up(self, conversation: Conversation) -> None:
        """磁盘上的比内存里的新（网页上接着聊过），就换成磁盘上的；只是改了名，就只换标题。"""
        try:
            fresh = self.router.app.store.load(conversation.id)
        except ConversationError:
            return
        if fresh.updated_at > conversation.updated_at:
            self._adopt(fresh)
        else:
            conversation.title = fresh.title

    def _save(self) -> None:
        conversation, agent = self.conversation, self.agent
        if conversation is None or agent is None:
            return
        messages = agent.backend.export()
        if not messages:
            if not conversation.saved:
                conversation.title = ""  # 第一条消息就失败了：当它没发生过
            return
        conversation.messages = messages
        conversation.dropped = agent.backend.dropped()
        conversation.events = self.ui.export_events()
        conversation.provider, conversation.model = agent.backend.provider, agent.backend.model
        conversation.context_tokens = agent.usage.context_tokens
        conversation.channel = self.platform
        try:
            self.router.app.store.save(conversation)
        except Exception as exc:  # noqa: BLE001 —— 存不上要说一声，内容还在内存里，下一轮结束时会再存一次
            self.router.say(self.platform, self.chat, t("保存会话失败：{error}", error=str(exc) or type(exc).__name__))
            return
        try:
            self.router.store.remember_chat(self.platform, self.chat, conversation.id)
        except ChannelError:
            pass  # 设置文件坏了：记不下"接着哪一段"，重启以后会开始一段新的，会话本身已经存好了

    def _sandbox(self) -> str:
        """这个通道现在选的沙箱（每条命令前读一次：你在页面上改了，下一条命令就按新的来）。读不出来时跟着设置走。"""
        try:
            return self.router.store.get(self.platform).get("sandbox", "default")
        except ChannelError:
            return "default"

    # ---- 确认请求 --------------------------------------------------------------

    def _asked(self, confirm_id: int, description: str, agent: str = "") -> None:
        """agent 要你确认一个操作（ChannelUI 拿着它自己的锁时调用的：这里只排队，不等）。
        agent 是哪个子 Agent 在问：消息上写"某某想要"而不是"助手想要"，你知道是谁要动手。"""
        waiting = Waiting(confirm_id, description)
        self.waiting = waiting
        first, _, rest = str(description).partition("\n")
        prompt = Prompt(
            id=confirm_id,
            title=(t("「{agent}」想要", agent=agent) if agent else t("助手想要")) + first,
            detail=_dedent(rest),
            where=t("工作目录：{path}", path=self.router.app.config.workspace),
            allow=t("允许"),
            deny=t("拒绝"),
            hint=t("回复“允许”或“拒绝”。"),
        )
        self.router.ask(self.platform, self.chat, prompt, waiting)
        self.router.changed()
        logger.info("%s: 等你批准（第 %d 号确认）", self.key, confirm_id)

    def _settled(self, confirm_id: int, allowed: bool, stopped: bool) -> None:
        waiting = self.waiting
        if waiting is None or waiting.id != confirm_id:
            return
        self.waiting = None
        if stopped:
            outcome = t("已中断")
        elif waiting.by == "web":
            outcome = t("已在网页上允许") if allowed else t("已在网页上拒绝")
        else:
            outcome = t("已允许") if allowed else t("已拒绝")
        self.router.settle(self.platform, self.chat, waiting, outcome)
        self.router.changed()


def _first_note(platform: str, buttons: bool = True) -> Callable[[list], str]:
    """一段对话的第一条消息前面附一句说明：它在哪个聊天软件里，用户多半在手机上看。

    写给模型看的，所以是中文、不翻译。只附在第一条上（history 是空的时候）：之后的历史只在末尾追加，
    这句说明一直留在第一条消息里，模型每一轮都看得到，系统提示词和缓存都不受影响。

    "能用 send_file 发文件"写在这里，而不是系统提示词里：系统提示词所有的对话共用（网页上的对话没有这个工具），
    改了它，每一段已经聊起来的对话的缓存都会作废。工具的说明书里也写着怎么用，这句只是提醒它：用户在手机上，
    要给他看的图片、文件直接发过去，别只说"文件在某某路径"（他在手机上打不开）。
    """
    name = LABELS.get(platform, platform)
    approve = "他点了按钮才会执行" if buttons else "他回复“允许”才会执行"
    return lambda history: "" if history else (
        f"<channel>这段对话在 {name} 里进行：用户多半在手机上看你的回复。回复尽量简短，"
        f"不要用表格（{name} 里显示不出来），需要列举时用列表。需要用户批准的操作会发到他的聊天里，{approve}。"
        f"要给他看截图、图片或者文件时，用 send_file 发到这个聊天里（他在手机上打不开这台电脑上的路径）。</channel>"
    )


def _dedent(text: str) -> str:
    """去掉每行开头共同的缩进和首尾的空行（Permissions.review 写的说明里，命令和文件内容是缩进了四格的）。"""
    return textwrap.dedent(text).strip("\n")
