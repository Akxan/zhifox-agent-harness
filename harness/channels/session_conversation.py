"""聊天里的一个对话 · 这一轮用的会话和助手（Session 的一组方法，见 session.py 开头）。

  _prepare     一轮开始之前：没有会话的话，接着上次那一段（channels.json 里记着）或者新开一段；认领这段会话（网页上正在跑就等它）；
               从磁盘跟上网页上的改动；这一家没了、没有密钥的说清楚；助手（App.channel_agent）用到时才建，接着聊的把历史放回去
  _load、_adopt、_fresh、_catch_up   读一段保存过的、接着它聊、新开一段（/new 某个 Agent 的用它当角色）、换成磁盘上更新的那份
  _persona     这段会话的角色现在还能不能用
  _save        一轮结束存盘，记下这个聊天接着哪一段
  _sandbox     这个通道现在选的沙箱（每条命令前读一次）
  _first_note  第一条消息前面附的那句说明（写给模型看的，中文、不翻译；为什么不放进系统提示词，见它的说明）

为什么单独一个文件：会话从哪儿来、存到哪儿去、和网页上开着的同一段会话怎么不打架（../claims.py），和"一轮怎么跑"是两件事。
"""

from __future__ import annotations

from collections.abc import Callable

from ..agents import AgentError, CustomAgent
from ..conversations import Conversation, ConversationError
from ..i18n import message_of, t
from ..providers import find, has_key
from ..tools.cwd import commands_in
from .base import ChannelError
from .platforms import LABELS
from .session_types import SessionPart


class SessionConversation(SessionPart):
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
                # 接着一段聊过的会话（重启以后、网页上接着聊过以后）：这个 shell 是新的，命令从工作目录开始。
                # 历史里执行过命令的，第一条命令的结果里说一声之前 cd 到的地方不再算数（见 tools/cwd.py）
                self.run.shell.current.reset(reopened=commands_in(conversation.events))
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
