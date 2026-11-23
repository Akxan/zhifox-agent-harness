"""会话：新建、打开、分叉、存盘、起标题、改名、删除、会话栏里的列表（App 的一组方法，见 app.py 开头）。

一段会话（Conversation，见 conversations.py）是存在磁盘上的那一份：历史、事件、标题、用的是哪一家的哪个模型。
这里管的是"一段对话（Chat，见 app_chat.py）现在接着的是哪段会话"，以及会话本身的增删改：
  - 换到另一段上（new_conversation、open_conversation、fork_conversation，后两个都走 _continue）要同时换掉好几样：
    适配层、会话、记住的选择、浏览器和电脑的 session、工具。规矩是"要么全换，要么都不动"（见 app_models.py 里
    select_model 前面的那段说明）：会失败的先做完，都成了，再一口气在内存里换掉；
  - 存盘、起标题、改名、删除照着磁盘上的会话库（ConversationStore）做；还开着的那几段（你正看着的、网页上在后台的）
    在内存里的那份跟着改，下次存盘才不会改回去。还没存过盘的会话也列在会话栏里（conversation_summaries）。

为什么单独一个文件："会话"这件事的全部在这里，和"用哪个模型"（app_models.py）、"手里有哪些工具"（app_tools.py）分开看。
"""

from __future__ import annotations

from collections.abc import Sequence

from . import fork
from .agents import AgentError, CustomAgent
from .app_chat import Chat
from .app_models import FALLBACK
from .app_types import AppPart
from .attachments import Attachment
from .conversations import Conversation, ConversationError, make_title
from .endpoints import PREFIX as ENDPOINT_PREFIX
from .i18n import t
from .providers import PROVIDERS, Provider, continues, find, switch_model
from .tools.cwd import commands_in


class AppConversations(AppPart):
    # ---- 会话 -------------------------------------------------------------

    def new_conversation(self, agent: str | None = None, chat: Chat | None = None) -> None:
        """开始一段新会话。之前的会话已经在磁盘上，随时可以再打开。

        agent：用哪个自定义 Agent 当角色（见 agents/ 开头的说明）。这段对话的系统提示词换成它的设定，工具只有它放开的，
        模型用它选的那个（它写了的话；这个选择也会记成"现在用的模型"，和打开一段旧会话一样）。
        这个 Agent 不存在或者不合格时抛出 AgentError，什么都不换。
        chat：在哪一段里开始（它原来的那段会话就离开了），不给是你正看着的这一段。
        """
        chat = chat or self.chat
        persona = self.agents.usable(agent) if agent else None  # 会失败的先做
        if persona is not None and persona.spec.model:
            provider_id, model = persona.spec.provider, persona.spec.model_id
        else:
            provider_id, model = self.settings.provider, self.settings.model
        provider = find(provider_id)
        if provider is None:  # 设置里选的自定义接口不在了（手改了 endpoints.json）：用内置的默认那一家
            provider_id, model = FALLBACK
            provider = PROVIDERS[provider_id]
        backend = chat.agent.backend
        if (
            persona is not None
            or chat.persona is not None
            or chat.prompt_in_use != self.system_prompt
            or not continues(backend, provider, model)
        ):
            # 提示词不一样了（换了角色、用户改过提示词、换了一家）：新的一段用新的。提示词存在适配层里，
            # 所以换一个新建的（历史本来就要清空）。自定义接口上能不能用工具不一样了，也换一个新建的（见 providers.continues）
            chat.agent.backend = self._backend_for(provider_id, model, persona, channel="", chat=chat)
            chat.prompt_in_use = self.system_prompt
        else:
            chat.agent.reset()
            switch_model(backend, provider, model)
        chat.persona = persona
        chat.conversation = self._new(provider_id, model, persona)
        chat.agent.usage.context_tokens = 0
        self._fresh_browser(chat)
        self._fresh_desktop(chat)
        self._fresh_fetch(chat)
        # 手里的工具换成新的这一段会用的（它还没开始，换了不丢任何东西）：角色只放开的那些、上一段中途打开的浏览器……
        # 上一段是网页上打开的聊天通道会话的话，新的这一段不在聊天软件里，不带 send_file。
        # 信息栏里"智狐能做的事"照着手里的工具画（见 app_tools.py 的 toolbox），不换的话第一条消息之前列的还是上一段的
        self.refresh_tools(chat)
        if persona is not None and persona.spec.model:
            self._remember_choice(provider_id, model)

    def _new(self, provider_id: str, model: str, persona: CustomAgent | None) -> Conversation:
        live = {chat.conversation.id for chat in self.chats()} if self.chat is not None else set()
        conversation = self.store.new(provider_id, model)
        while conversation.id in live:  # 同一秒里开的另一段还没存盘（store.new 只查磁盘）：编号撞上了就再抽一个
            conversation = self.store.new(provider_id, model)
        conversation.agent = persona.name if persona is not None else ""
        return conversation

    def open_conversation(self, conversation_id: str, chat: Chat | None = None) -> Conversation:
        """打开一段保存过的会话，接着聊。chat：在哪一段里打开（它原来的那段会话就离开了），不给是你正看着的这一段。

        做的事情只有两件：换回这段会话当时用的模型，把它的对话历史交还给适配层。
        下一次请求会把这份历史一起发给模型，模型就"想起"了之前聊过什么。
        """
        return self._continue(self.store.load(conversation_id), chat or self.chat)

    def fork_conversation(
        self, conversation_id: str, index: int, at: str, expect: tuple[int | None, str] | None = None, chat: Chat | None = None
    ) -> fork.Fork:
        """从一段保存过的会话的第 index 轮（从 0 数）分叉成一段新的（规矩见 fork.py），存好盘，在 chat 里换到新的一段上接着聊
        （chat 不给是你正看着的这一段）。
        原来那段一字不动：只是从磁盘读它（ConversationStore.load 拿着会话库的锁，聊天通道正在写它也读不到半截）。

        新的一段有内容才存盘；在第一轮的消息上分叉的是空的，和新对话一样先只在内存里（会话栏里照样列出来）。
        分叉不了抛出 ForkError，会话读不出来、它用的那一家不在了抛出 ConversationError：这两种都什么都不换。
        """
        source = self.store.load(conversation_id)
        made = fork.make(source, self._provider_of(source), self.store, index, at, expect)
        if made.conversation.messages or made.conversation.events:
            self.store.save(made.conversation)
        self._continue(made.conversation, chat or self.chat)
        return made

    @staticmethod
    def _provider_of(conversation: Conversation) -> Provider:
        """一段会话用的那一家。不在了（自定义接口被删掉了、这个版本不支持了）时抛出 ConversationError，说明怎么办。"""
        provider = find(conversation.provider)
        if provider is None and conversation.provider.startswith(ENDPOINT_PREFIX):
            raise ConversationError(t("这段会话用的是自定义模型接口（{provider}），它已经被删掉了。在设置里重新添加一个同名的接口，就能接着聊。", provider=conversation.provider))
        if provider is None:
            raise ConversationError(t("这段会话用的模型提供方，这个版本的程序已经不支持了。"))
        return provider

    def _continue(self, conversation: Conversation, chat: Chat) -> Conversation:
        """在 chat 里换到这段会话上接着聊：换回它用的模型，把它的历史交还给适配层（打开一段会话、分叉出一段新的都走这里）。"""
        provider = self._provider_of(conversation)
        # 用 Agent 开始的会话：照着这个 Agent 现在的文件接着聊（和内置的提示词一样，用的是现在的那一版）。
        # 它被删掉了、或者现在不合格的话，退回到普通的对话，并且说一声
        persona = None
        if conversation.agent:
            try:
                persona = self.agents.usable(conversation.agent)
            except AgentError as exc:
                self.warnings.append(t("这段会话是用 Agent「{name}」开始的，但它现在用不了（{reason}）。接着聊会用内置的提示词和全部工具。", name=conversation.agent, reason=exc))
        # 会失败的到这里都做完了（另一家的适配层在下面这一行建好），之后的几步只是在内存里换东西
        same_provider = continues(chat.agent.backend, provider, conversation.model)
        # 同一家、提示词也没改过、前后都不是角色，就接着用现在的适配层；否则新建一个（带着现在的提示词）
        reuse = same_provider and chat.prompt_in_use == self.system_prompt and persona is None and chat.persona is None
        backend = chat.agent.backend if reuse else self._backend_for(conversation.provider, conversation.model, persona, channel=conversation.channel, chat=chat)
        switch_model(backend, provider, conversation.model)
        backend.restore(conversation.messages, conversation.dropped)
        chat.agent.backend = backend
        chat.prompt_in_use = self.system_prompt
        chat.persona = persona
        self._fresh_browser(chat)
        self._fresh_desktop(chat)
        self._fresh_fetch(chat)
        chat.conversation = conversation  # 先换上：它的工具要看它是不是在聊天通道里开始的（见 app_tools.py 的 conversation_tools）
        self.fresh_tools(chat)  # 接着聊也算这一次运行里"开始"的一段对话：用此刻的 MCP 工具，之后不再变
        # 命令从工作目录重新开始，不恢复当时停在哪儿；历史里执行过命令的，第一条命令的结果里说一声（见 tools/cwd.py）
        chat.shell.current.reset(reopened=commands_in(conversation.events))
        chat.agent.usage.context_tokens = conversation.context_tokens
        self._remember_choice(conversation.provider, conversation.model)
        return conversation

    # ---- 存盘、标题、改名、删除、会话栏的列表 ------------------------------------------

    def save_conversation(self, events: list, chat: Chat | None = None) -> bool:
        """把一段会话写到磁盘（chat 不给是你正看着的这一段）。events 是界面上显示过的内容。还没有任何对话内容时不保存。

        存过盘的会话，历史空了也照样存：那是把第一条消息倒回去了（编辑第一条消息，见 rewind.py），
        磁盘上要跟着变，不然重新打开时，倒回去的那一轮又回来了。
        模型记的是这一段的适配层此刻用的那个：几段对话同时进行时，设置里"现在用的模型"是你正看着的那一段的，不一定是这一段的。
        """
        chat = chat or self.chat
        conversation, backend = chat.conversation, chat.agent.backend
        messages = backend.export()
        if not messages and not conversation.saved:
            conversation.title = ""  # 第一条消息就失败了：当它没发生过
            return False
        conversation.messages = messages
        conversation.dropped = backend.dropped()
        conversation.events = events
        conversation.provider, conversation.model = backend.provider, backend.model
        conversation.context_tokens = chat.agent.usage.context_tokens
        self.store.save(conversation)
        return True

    def name_conversation(self, first_message: str, attachments: Sequence[Attachment] = (), chat: Chat | None = None) -> None:
        """新会话用用户的第一句话当标题。第一条消息只有附件、没有文字时，用第一个附件的文件名。"""
        conversation = (chat or self.chat).conversation
        if not conversation.title:
            conversation.title = make_title(first_message) or (make_title(attachments[0].name) if attachments else "")

    def retitle(
        self, old_text: str, old_attachments: Sequence[dict], text: str, attachments: Sequence[Attachment], earlier: Sequence[dict] = (),
        chat: Chat | None = None,
    ) -> None:
        """编辑了一条消息（见 rewind.py）：标题要是就是照着它原来的样子起的（你没改过名），跟着换成照新的样子起的。
        不然倒回去的那句话还留在会话列表里，也还搜得到。old_attachments 是原来那条消息的附件概要。

        earlier 是它前面的那些用户消息（user 事件）。其中有一条起出来的标题也是这一个（比如第一条和这一条都是"你好"），
        标题就是从前面那条来的，不动它。
        """
        def auto(message: str, summaries: Sequence[dict]) -> str:
            first = summaries[0].get("name", "") if summaries and isinstance(summaries[0], dict) else ""
            return make_title(message) or make_title(first if isinstance(first, str) else "")

        chat = chat or self.chat
        title = chat.conversation.title
        if not title or title != auto(old_text, old_attachments):
            return
        for event in earlier:
            words, summaries = event.get("text"), event.get("attachments")
            if auto(words if isinstance(words, str) else "", summaries if isinstance(summaries, list) else []) == title:
                return
        chat.conversation.title = ""
        self.name_conversation(text, attachments, chat=chat)

    def _is_unsaved_current(self, conversation_id: str) -> bool:
        """是不是"列在会话栏里、但还没有写到磁盘上"的当前会话（第一轮还没存，或者存盘失败了）。"""
        current = self.conversation
        return conversation_id == current.id and not current.saved and bool(current.title)

    def rename_conversation(self, conversation_id: str, title: str) -> None:
        """改名。这段会话正开着（你正看着的，或者网页上在后台的另一段）的话，它在内存里的标题也跟着改：下次存盘时不会改回去。"""
        live = [chat.conversation for chat in self.chats() if chat.conversation.id == conversation_id]
        if live and not live[0].saved and live[0].title:
            live[0].title = make_title(title)  # 磁盘上还没有它：先改内存里的，下次存盘自然带上
            return
        self.store.rename(conversation_id, title)
        for conversation in live:
            conversation.title = make_title(title)

    def delete_conversation(self, conversation_id: str) -> bool:
        """删除一段会话。返回 True 表示删的是当前这一段（这时会自动开始一段新的）。"""
        if self._is_unsaved_current(conversation_id):
            self.new_conversation()  # 磁盘上没有东西可删，丢掉内存里的这一段就是删除
            return True
        self.store.delete(conversation_id)
        if conversation_id != self.conversation.id:
            return False
        self.new_conversation()
        return True

    def conversation_summaries(self) -> list[dict]:
        """会话列表：磁盘上保存的那些，加上刚开始、还没来得及保存的那几段（你正看着的，和网页上在后台跑着的），新的在前。"""
        items = self.store.summaries()
        fresh = [chat.conversation for chat in self.chats() if chat.conversation.title and not chat.conversation.saved]
        for conversation in sorted(fresh, key=lambda item: item.updated_at):
            items.insert(0, conversation.summary())
        return items
