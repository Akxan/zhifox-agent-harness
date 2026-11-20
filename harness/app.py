"""组装：把配置、工具、权限、模型和循环拼成一个可以运行的助手。

终端版和网页版从这里拿到的是同一套东西，区别只在于传进来的界面（ui）不同。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from . import budget, i18n
from .agent import Agent
from .attachments import Attachment, unsupported
from .config import Config
from .conversations import (
    Conversation,
    ConversationError,
    ConversationStore,
    make_title,
)
from .i18n import t
from .ledger import Ledger
from .permissions import Permissions
from .prompt import build_system_prompt
from .providers import (
    PROVIDERS,
    ClientFactory,
    Provider,
    ProviderError,
    has_key,
    list_models,
    make_backend,
    make_client,
)
from .settings import Settings, save_api_key
from .tools import ToolRegistry, build_registry
from .tools.files import FileEditor
from .tools.shell import Shell
from .tools.todo import TodoStore


@dataclass
class App:
    config: Config
    settings: Settings
    agent: Agent
    todos: TodoStore
    tools: ToolRegistry
    shell: Shell
    system_prompt: str
    client_factory: ClientFactory
    store: ConversationStore  # 保存在磁盘上的全部会话
    conversation: Conversation  # 当前正在进行的这一段
    ledger: Ledger  # 花费账本：每一次模型调用花了多少（见 ledger.py）
    warnings: list[str] = field(default_factory=list)  # 做成了、但有一点没做好的事，等界面取走显示（见 take_warnings）

    @property
    def provider(self) -> Provider:
        """当前使用的是哪一家。"""
        return PROVIDERS[self.settings.provider]

    # 换模型、打开会话都要同时换掉好几样东西：循环背后的适配层、当前会话、记住的选择。
    # 规矩是"要么全换，要么都不动"：可能失败的步骤（读会话文件、建新的适配层）先做完，
    # 都成了，再一口气把内存里的东西换掉。不然中途出错会留下"适配层已经是另一家、会话还是原来那段"的
    # 半截状态，下一轮对话就会把原来那段会话的文件写坏。

    def select_model(self, provider_id: str, model: str) -> bool:
        """换模型。返回 True 表示对话可以接着进行，False 表示开始了一段新对话。

        同一家之内换模型，历史的格式不变，可以接着聊。换到另一家时，
        历史的格式不通用，只能从头开始（记忆和待办在磁盘上，不受影响）。
        """
        same_provider = provider_id == self.agent.backend.provider
        if same_provider:
            self.agent.backend.model = model
            self.conversation.model = model
        else:
            backend = self._backend_for(provider_id, model)  # 会失败的先做
            self.agent.backend = backend
            self.conversation = self.store.new(provider_id, model)
            self.agent.usage.context_tokens = 0
        self._remember_choice(provider_id, model)
        return same_provider

    def _backend_for(self, provider_id: str, model: str) -> Any:
        """建一个连着指定模型的新适配层（历史是空的）。"""
        provider = PROVIDERS[provider_id]
        return make_backend(provider, model, self.config, self.tools, self.system_prompt, self.client_factory(provider))

    def _remember_choice(self, provider_id: str, model: str) -> None:
        """把"现在用的是哪个模型"写进设置文件，下次启动还用它。

        写不进去（磁盘满了、没有权限）只是"没记住"，不该让已经完成的切换半途而废，所以接住错误、留一句提醒。
        """
        try:
            self.settings.choose(provider_id, model)
        except OSError as exc:
            self.warnings.append(t("没能把这次选择的模型记下来（{error}），下次启动时用的还是之前记住的那个。", error=exc))

    def take_warnings(self) -> list[str]:
        """取走攒下的提醒（取走就清空）。"""
        taken, self.warnings = self.warnings, []
        return taken

    # ---- 会话 -------------------------------------------------------------

    def new_conversation(self) -> None:
        """开始一段新会话。之前的会话已经在磁盘上，随时可以再打开。"""
        self.agent.reset()
        self.conversation = self.store.new(self.settings.provider, self.settings.model)
        self.agent.usage.context_tokens = 0

    def open_conversation(self, conversation_id: str) -> Conversation:
        """打开一段保存过的会话，接着聊。

        做的事情只有两件：换回这段会话当时用的模型，把它的对话历史交还给适配层。
        下一次请求会把这份历史一起发给模型，模型就"想起"了之前聊过什么。
        """
        conversation = self.store.load(conversation_id)
        if conversation.provider not in PROVIDERS:
            raise ConversationError(t("这段会话用的模型提供方，这个版本的程序已经不支持了。"))
        # 会失败的到这里都做完了（另一家的适配层在下面这一行建好），之后的几步只是在内存里换东西
        same_provider = conversation.provider == self.agent.backend.provider
        backend = self.agent.backend if same_provider else self._backend_for(conversation.provider, conversation.model)
        backend.model = conversation.model
        backend.restore(conversation.messages)
        self.agent.backend = backend
        self.conversation = conversation
        self.agent.usage.context_tokens = conversation.context_tokens
        self._remember_choice(conversation.provider, conversation.model)
        return conversation

    def save_conversation(self, events: list) -> bool:
        """把当前会话写到磁盘。events 是界面上显示过的内容。还没有任何对话内容时不保存。"""
        conversation = self.conversation
        messages = self.agent.backend.export()
        if not messages:
            if not conversation.saved:
                conversation.title = ""  # 第一条消息就失败了：当它没发生过
            return False
        conversation.messages = messages
        conversation.events = events
        conversation.provider, conversation.model = self.settings.provider, self.settings.model
        conversation.context_tokens = self.agent.usage.context_tokens
        self.store.save(conversation)
        return True

    def name_conversation(self, first_message: str, attachments: Sequence[Attachment] = ()) -> None:
        """新会话用用户的第一句话当标题。第一条消息只有附件、没有文字时，用第一个附件的文件名。"""
        if not self.conversation.title:
            self.conversation.title = make_title(first_message) or (make_title(attachments[0].name) if attachments else "")

    def attachment_problem(self, attachments: Sequence[Attachment]) -> str:
        """当前的模型收不收得了这些附件。收不了的话返回说明，收得了返回空字符串。"""
        provider = self.provider
        return unsupported(attachments, provider.label, provider.vision, provider.pdf)

    def _is_unsaved_current(self, conversation_id: str) -> bool:
        """是不是"列在会话栏里、但还没有写到磁盘上"的当前会话（第一轮还没存，或者存盘失败了）。"""
        current = self.conversation
        return conversation_id == current.id and not current.saved and bool(current.title)

    def rename_conversation(self, conversation_id: str, title: str) -> None:
        if self._is_unsaved_current(conversation_id):
            self.conversation.title = make_title(title)  # 磁盘上还没有它：先改内存里的，下次存盘自然带上
            return
        self.store.rename(conversation_id, title)
        if conversation_id == self.conversation.id:
            self.conversation.title = make_title(title)

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
        """会话列表：磁盘上保存的那些，加上刚开始、还没来得及保存的当前会话。"""
        items = self.store.summaries()
        if self.conversation.title and not self.conversation.saved:
            items.insert(0, self.conversation.summary())
        return items

    def set_api_key(self, provider_id: str, key: str) -> None:
        """保存（或删除）一家的 API 密钥，并让它立即生效。"""
        provider = PROVIDERS[provider_id]
        save_api_key(self.config.env_file, provider.key_env, key)
        if provider_id == self.agent.backend.provider:
            self.agent.backend.client = self.client_factory(provider)  # 正在用这一家：换上带新 key 的连接

    def spending(self) -> dict:
        """今天、本月花了多少，预算是多少、超了没有（见 ledger.py、budget.py）。

        这个月的花费刚越过预算时，顺便记下"已经提醒过了"，所以同一个预算只提醒一次。记不下来（磁盘写不进去）
        也不要紧，只是下一次还会再提醒。
        """
        today = self.agent.clock().date()
        month = budget.month_of(today)
        spent = self.ledger.month_total(month)
        amount = self.settings.budget
        if budget.should_alert(spent, amount, self.settings.budget_alert, month):
            try:
                self.settings.set_budget_alert({"month": month, "amount": amount})
            except OSError:
                pass
        return {
            "day": today.isoformat(),
            "today": self.ledger.day_total(today),
            "month": spent,
            "budget": amount,
            "over": bool(amount) and spent >= amount,
            "alert": budget.marker(self.settings.budget_alert, month),
            "error": self.ledger.error,
        }

    def sync_models(self, provider_id: str) -> list[str]:
        """向一家的服务器询问有哪些模型，并把列表记下来。"""
        provider = PROVIDERS[provider_id]
        if not has_key(provider):
            raise ProviderError(t("还没有设置 {provider} 的 API 密钥。", provider=provider.label))
        models = list_models(provider, self.client_factory(provider))
        self.settings.set_models(provider_id, models)
        return models


def build_app(config: Config, ui: Any, client_factory: ClientFactory = make_client) -> App:
    settings = Settings(config.settings_file, config.default_provider, config.default_model)
    if settings.provider not in PROVIDERS:
        settings.provider, settings.model = "anthropic", "claude-opus-5-5"
    i18n.choose(settings.language)  # 界面语言：从这里开始，程序里所有 t() 都译成它

    files = FileEditor(config.workspace, config.backup_dir, protected=[config.env_file])
    todos = TodoStore(config.todo_file)
    shell = Shell(config.workspace)
    tools = build_registry(config, files, todos, shell)
    system_prompt = build_system_prompt(config)

    provider = PROVIDERS[settings.provider]
    backend = make_backend(provider, settings.model, config, tools, system_prompt, client_factory(provider))
    permissions = Permissions(files, private=[config.data_dir])
    agent = Agent(backend=backend, config=config, tools=tools, permissions=permissions, ui=ui)
    # 每调用一次模型记一笔账，价格按那一刻、用你在设置里填的（见 ledger.py）。终端和网页都记
    ledger = Ledger(config.ledger_file)
    agent.on_call = ledger.recorder(lambda: settings.prices)
    store = ConversationStore(config.conversations_dir)
    return App(
        config=config,
        settings=settings,
        agent=agent,
        todos=todos,
        tools=tools,
        shell=shell,
        system_prompt=system_prompt,
        client_factory=client_factory,
        store=store,
        conversation=store.new(settings.provider, settings.model),
        ledger=ledger,
    )


def run_turn(agent: Agent, ui: Any, text: str, attachments: Sequence[Attachment] | None = None) -> None:
    """跑一轮对话。出错后程序不退出，把原因显示出来，可以接着聊。附件只有网页界面会传。"""
    try:
        agent.run_turn(text, attachments)
    except KeyboardInterrupt:
        ui.info(t("已中断。"))
    except ProviderError as exc:
        ui.error(i18n.message_of(exc))
    except Exception as exc:  # noqa: BLE001 —— 故意接住所有异常：哪里出了意料之外的错，都要让用户看到，而不是让程序退出
        ui.error(t("这一轮出错了：{error}", error=f"{type(exc).__name__}: {exc}"))
