"""聊天里的一个对话 · 跑一轮（Session 的一组方法，见 session.py 开头）。

  _work       这个聊天的线程：一条一条取出排着的消息，每条跑一轮；没有了就退出（下一条来了，submit 再开一个）
  _turn       一轮：处理不了的类型先说一声 → 下载附件 → 准备会话和助手（_prepare，在 session_conversation.py）→ 这个模型收不收得了 →
              语音先转成文字 → 跑 agent 循环（和网页上同一个 run_turn）→ 存盘、关掉浏览器标签页、放开认领、告诉网页
  _hear       语音转文字（../voice/）：转好了先在聊天里回一句，让你看看听得对不对
  _typing、_attachments、_close_browser、_kill   "正在输入"、下载附件、一轮结束关标签页、/stop 时停下这个聊天自己的命令和调用

logger 在这里（session_confirm.py 也从这里拿）：拆出去的文件不 import 入口，它只能放在最前面用到它的这一个里。
测试换 find 要换在这个模块上：一轮里读它的 _turn 在这里。
为什么单独一个文件：这是一个聊天真正花钱、调用工具的地方；会话和助手怎么来的、确认请求怎么发，各在旁边的文件里。
"""

from __future__ import annotations

import logging
import threading
import time

from ..app import run_turn
from ..attachments import Attachment, AttachmentError, from_bytes, unsupported
from ..config import CHANNEL_TYPING_SECONDS
from ..conversations import make_title
from ..i18n import k, message_of, t
from ..providers import caps, find
from ..voice import VoiceError, listen
from .base import ChannelError, Inbound
from .session_types import SessionPart

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


class SessionTurn(SessionPart):
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
            logger.info("%s: 开始一轮（%s · %s）", self.key, label, agent.backend.model)  # label：上面算好的，这一家找不到时是它的编号
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
