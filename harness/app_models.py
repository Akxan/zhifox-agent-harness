"""用哪一家、哪个模型（App 的一组方法，见 app.py 开头）。

  - provider：现在用的是哪一家；FALLBACK：设置里选的那一家不在了时，退回到哪一家的哪个模型；
  - select_model、set_prompt：换模型、改提示词。适配层（Backend，见 providers/）里记着历史、系统提示词和工具说明书，
    能接着用就接着用，不能就用 _backend_for 建一个新的；_remember_choice 把选择记进设置，下次启动还用它；
  - attachment_problem：现在的模型收不收得了这些附件；
  - set_api_key、sync_models：保存密钥，问服务器有哪些模型；
  - 自定义模型接口（见 endpoints/）：存、删，改了以后正在用它的这一段怎么跟着换。
"换了会怎样"（continue / fresh / leave）在 model_catalog.py 里判断，这里是真的去换。

为什么单独一个文件：这些都是在换"助手背后连着的那个模型"；价格和花费只关系到钱，另在 app_prices.py。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from .agents import CustomAgent
from .agents import tools as agent_tools
from .agents.prompt import persona_prompt
from .app_types import AppPart
from .attachments import Attachment, unsupported
from .endpoints import EndpointError
from .i18n import t
from .prompt import build_system_prompt, reset_template, save_template
from .providers import (
    PROVIDERS,
    Provider,
    ProviderError,
    caps,
    continues,
    find,
    has_key,
    list_models,
    make_backend,
    probe_models,
    switch_model,
)
from .settings import save_api_key

if TYPE_CHECKING:
    from .app_chat import Chat

# 设置里选的那一家不在了（自定义接口被删掉了、设置文件被改坏了）时，退回到这一家的这个模型
FALLBACK = ("anthropic", "claude-opus-5-5")


class AppModels(AppPart):
    # ---- 现在用的是哪一家；换模型、改提示词（换掉适配层）-------------------------------

    @property
    def provider(self) -> Provider:
        """当前使用的是哪一家。选中的自定义接口刚被删掉的话（删不掉正在用的，见 delete_endpoint，所以只有手改文件才会这样），
        按内置的默认那一家算。"""
        return find(self.settings.provider) or PROVIDERS["anthropic"]

    # 换模型、打开会话都要同时换掉好几样东西：循环背后的适配层、当前会话、记住的选择。
    # 规矩是"要么全换，要么都不动"：可能失败的步骤（读会话文件、建新的适配层）先做完，
    # 都成了，再一口气把内存里的东西换掉。不然中途出错会留下"适配层已经是另一家、会话还是原来那段"的
    # 半截状态，下一轮对话就会把原来那段会话的文件写坏。

    def select_model(self, provider_id: str, model: str) -> bool:
        """换模型。返回 True 表示对话可以接着进行，False 表示开始了一段新对话。

        同一家之内换模型，历史的格式不变，可以接着聊。换到另一家时，
        历史的格式不通用，只能从头开始（记忆和待办在磁盘上，不受影响）。
        自定义接口上从能用工具的模型换到不能用的（或者反过来），也要从头开始：工具说明书在一段对话里不变（见 providers.continues）。
        """
        provider = find(provider_id)
        if provider is None:
            raise ProviderError(t("这个模型接口已经不在了，可能刚被删掉。"))
        chat = self.chat
        same_provider = continues(chat.agent.backend, provider, model)
        if same_provider:
            switch_model(chat.agent.backend, provider, model)
            chat.conversation.model = model
        else:
            backend = self._backend_for(provider_id, model, chat.persona, channel="")  # 会失败的先做。用 Agent 开始的对话，新的一段还是它
            chat.agent.backend = backend
            chat.prompt_in_use = self.system_prompt
            chat.conversation = self._new(provider_id, model, chat.persona)
            chat.agent.usage.context_tokens = 0
        self._remember_choice(provider_id, model)
        return same_provider

    def set_prompt(self, text: str | None) -> bool:
        """保存用户改的提示词（None 表示恢复内置的）。失败时抛出 PromptError 或 OSError。

        返回 True 表示当前这段也用上了新的：它还一句都没聊，换掉适配层不会丢任何东西。
        已经聊起来的这一段继续用原来的那版（一段对话里提示词不能变，见 prompt.py 开头），从下一段开始用新的。
        """
        if text is None:
            reset_template(self.config)
        else:
            save_template(self.config, text)
        self.system_prompt = build_system_prompt(self.config)
        chat = self.chat
        if chat.agent.backend.export():
            return False
        backend = chat.agent.backend
        chat.agent.backend = self._backend_for(backend.provider, backend.model, chat.persona)
        chat.prompt_in_use = self.system_prompt
        return True

    def _backend_for(self, provider_id: str, model: str, persona: CustomAgent | None = None, channel: str | None = None, chat: Chat | None = None) -> Any:
        """建一个连着指定模型的新适配层（历史是空的）。persona：用哪个 Agent 当角色（None 是普通的对话）。
        channel：这段会话是在哪个聊天通道里开始的（见 app_tools.py 的 conversation_tools），不给就是 chat 那一段的。
        chat：给哪一段对话用（工具照着它摆），不给是你正看着的这一段。
        这一家不在了（自定义接口被删掉了）时抛出 ProviderError。"""
        provider = find(provider_id)
        if provider is None:
            raise ProviderError(t("这个模型接口已经不在了，可能刚被删掉。"))
        tools = self.conversation_tools(persona, channel=channel, chat=chat)
        web = self.config.web and provider.web  # 能不能联网，要看是哪一家
        if persona is None:
            prompt = build_system_prompt(self.config, web=web)
        else:
            # 角色的提示词是它自己的设定加上程序附上的环境说明（见 agents/prompt.py）；它的 tools 没放开联网，就不写"能联网"
            prompt = persona_prompt(self.config, persona, web=web and agent_tools.has_web(tools))
        return make_backend(provider, model, self.config, tools, prompt, self.client_factory(provider))

    def _remember_choice(self, provider_id: str, model: str) -> None:
        """把"现在用的是哪个模型"写进设置文件，下次启动还用它。

        写不进去（磁盘满了、没有权限）只是"没记住"，不该让已经完成的切换半途而废，所以接住错误、留一句提醒。
        """
        try:
            self.settings.choose(provider_id, model)
        except OSError as exc:
            self.warnings.append(t("没能把这次选择的模型记下来（{error}），下次启动时用的还是之前记住的那个。", error=exc))

    # ---- 附件、密钥、模型列表 -----------------------------------------------------

    def attachment_problem(self, attachments: Sequence[Attachment]) -> str:
        """当前的模型收不收得了这些附件。收不了的话返回说明，收得了返回空字符串。
        自定义接口按这个模型算（同一个 Ollama 上有的模型看得懂图，有的看不懂，见 providers.caps）。"""
        provider = self.provider
        accepts = caps(provider, self.settings.model)
        return unsupported(attachments, provider.label, accepts.vision, accepts.pdf)

    def set_api_key(self, provider_id: str, key: str) -> None:
        """保存（或删除）一家的 API 密钥，并让它立即生效。只管内置的几家：自定义接口的密钥在它自己的设置里（见 save_endpoint）。"""
        provider = PROVIDERS[provider_id]
        save_api_key(self.config.env_file, provider.key_env, key)
        for chat in self.chats():
            # 正在用这一家的那几段：换上带新 key 的连接（只是换掉一个属性，后台那一段正在等的这次请求照旧用原来的连接，下一次用新的）
            if provider_id == chat.agent.backend.provider:
                chat.agent.backend.client = self.client_factory(provider)

    def sync_models(self, provider_id: str) -> list[str]:
        """向一家的服务器询问有哪些模型，并把列表记下来。

        自定义接口记的不只是名字：每个模型能做什么也一起猜（见 endpoints/probe.py），你改过的设置保留（见 endpoints/spec.merged）。
        """
        provider = find(provider_id)
        if provider is None:
            raise ProviderError(t("这个模型接口已经不在了，可能刚被删掉。"))
        if provider.endpoint is not None:
            found = probe_models(provider)
            if not found.listed:
                raise ProviderError(t("{provider} 不提供模型列表（/models）。请在设置的“自定义模型接口”里自己填模型名。", provider=provider.label))
            try:
                endpoint = self.endpoints.set_models(provider.id, found.models)
            except EndpointError as exc:
                raise ProviderError(exc.message) from None
            self._endpoint_changed(provider.id)
            return [model.id for model in endpoint.models]
        if not has_key(provider):
            raise ProviderError(t("还没有设置 {provider} 的 API 密钥。", provider=provider.label))
        models = list_models(provider, self.client_factory(provider))
        self.settings.set_models(provider_id, models)
        return models

    # ---- 自定义模型接口（见 endpoints/）-------------------------------------------

    def save_endpoint(self, form: dict, provider_id: str | None = None) -> Any:
        """新建（provider_id 为 None）或者修改一个自定义接口，返回存好的 Endpoint。表单不对时抛出 EndpointError。"""
        endpoint = self.endpoints.save(form, provider_id)
        self._endpoint_changed(endpoint.provider_id)
        return endpoint

    def delete_endpoint(self, provider_id: str) -> None:
        """删掉一个自定义接口。正在用它的话不删：先换一个模型（不然这段对话、下次启动就不知道该用谁了）。"""
        if provider_id in self.providers_in_use():
            raise EndpointError(t("正在用这个接口上的模型。先在换模型的面板里换一个，再删除它。"))
        self.endpoints.delete(provider_id)

    def providers_in_use(self) -> set[str]:
        """正在用的那几家：活着的每一段对话（网页上在后台跑着的也算）的，加上设置里选的那一家。"""
        return {chat.agent.backend.provider for chat in self.chats()} | {self.settings.provider}

    def _endpoint_changed(self, provider_id: str) -> None:
        """一个自定义接口改了。你正看着的这一段在用它的话：

          - 这段对话还没开始：整个换成新的（新的地址、密钥、能力，能不能用工具也马上按新的）；
          - 已经开始了：换上新的连接（地址、密钥可能变了）、新的名字、这个模型看不看得懂图和上下文多大。
            能不能用工具不换：工具说明书在一段对话里不变，从下一段对话开始按新的来。
        网页上在后台跑着的那几段不动（它们正在一轮中间，换掉能力会让这一轮前后对不上）：跑完存了盘、下次打开时就是新的。
        """
        chat = self.chat
        backend = chat.agent.backend
        provider = find(provider_id)
        if backend.provider != provider_id or provider is None:
            return
        if not backend.export():
            chat.agent.backend = self._backend_for(provider_id, backend.model, chat.persona)
            chat.prompt_in_use = self.system_prompt
            self.refresh_tools()
            return
        # 用到时才导入（见 providers/__init__.py 开头）
        from .providers.openai_compat import OpenAICompatBackend

        backend.client = self.client_factory(provider)
        if isinstance(backend, OpenAICompatBackend):  # 自定义接口一律是 OpenAI 格式的适配层（见 providers.make_backend）
            backend.label = provider.label
            backend.stream_usage = provider.endpoint.stream_usage if provider.endpoint is not None else True
        switch_model(backend, provider, backend.model)
