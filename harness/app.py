"""组装：把配置、工具、权限、模型和循环拼成一个可以运行的助手。

终端版和网页版从这里拿到的是同一套东西，区别只在于传进来的界面（ui）不同。

你的一段对话用的东西（助手、会话、shell、浏览器和电脑的 session……）装在一个 Chat 里。终端界面只有一个；
网页上可以几段对话同时进行（见 webui/sessions.py），每段一个。App.chat 是你正看着的那一段，
App.agent、App.conversation、App.shell、App.chat_browser、App.chat_desktop 都是它的，读它们的代码不用管有几段。
会在后台接着跑的那几段，调用这里的方法时把它们自己的 Chat 传进来（chat=…），不要用 App.agent 这些。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import budget, fallback, i18n
from .agent import Agent
from .agents import AgentError, AgentStore, CustomAgent
from .agents import tools as agent_tools
from .agents.catalog import notifier as agent_notifier
from .agents.prompt import persona_prompt
from .agents.runner import Delegator
from .attachments import Attachment, unsupported
from .browser.manager import BrowserManager
from .browser.session import BrowserSession
from .commands.allowlist import CommandList
from .config import Config
from .conversations import (
    Conversation,
    ConversationError,
    ConversationStore,
    make_title,
)
from .desktop import manager as desktop_labels
from .desktop.manager import DesktopManager
from .desktop.session import DesktopSession
from .endpoints import PREFIX as ENDPOINT_PREFIX
from .endpoints import EndpointError, EndpointStore
from .hooks import HookConfig, HookRun, Hooks
from .i18n import t
from .ledger import Ledger
from .mcp import McpConfig, McpManager, Snapshot
from .mcp.bridge import PREFIX as MCP_PREFIX
from .media import MediaStore
from .permissions import Permissions
from .pricing import price_of as builtin_price
from .prompt import build_system_prompt, reset_template, save_template
from .providers import (
    PROVIDERS,
    ClientFactory,
    Provider,
    ProviderError,
    caps,
    continues,
    find,
    has_key,
    list_models,
    make_backend,
    make_client,
    probe_models,
    switch_model,
    use_custom,
)
from .redact import Redactor
from .sandbox import DEFAULT as SANDBOX_DEFAULT
from .sandbox import Layout, Sandbox
from .settings import Settings, save_api_key
from .skills import SkillStore
from .skills.catalog import notifier
from .tasks import TaskStore
from .tools import ToolRegistry, build_registry
from .tools.browser import make_browser_tools
from .tools.browser import used_in as browser_used_in
from .tools.computer import make_computer_tools
from .tools.computer import used_in as computer_used_in
from .tools.files import FileEditor
from .tools.send import make_send_tool
from .tools.shell import Shell, make_shell_tool
from .tools.skill import TOOL_NAME as SKILL_TOOL
from .tools.todo import TodoStore
from .undo.changes import FileChanges

# 设置里选的那一家不在了（自定义接口被删掉了、设置文件被改坏了）时，退回到这一家的这个模型
FALLBACK = ("anthropic", "claude-opus-5-5")


def merged_prices(settings: Settings, endpoints: EndpointStore) -> dict[str, dict]:
    """算钱用的价格表：你在设置里给内置模型填的（按模型名），加上自定义接口上每个模型的（按"接口/模型"，见 pricing.usage_key）。
    两边的名字不会撞上：自定义接口的带着 custom- 的前缀。"""
    return {**settings.prices, **endpoints.prices()}


@dataclass
class UnattendedRun:
    """定时任务的一次运行用的东西（见 App.unattended_agent）。"""

    agent: Agent
    shell: Shell  # 这次运行自己的 shell
    stop: Callable[[], None]  # 停下这次运行正在做的事：结束它正在运行的命令、取消它正在等的 MCP 调用、停下浏览器和电脑的动作。别人的不碰
    close: Callable[[], None] = lambda: None  # 这次运行结束了：关掉它自己开的浏览器标签页，把屏幕让出来


@dataclass
class Chat:
    """你的一段对话在助手这一侧用的全部东西（见文件开头）。网页上同时进行的几段，各有一个（见 webui/sessions.py）。

    凡是"会被停下"的都各用各的，和定时任务的每次运行一样（见 _side_run）：点这一段的"停止"，只结束它自己的命令、
    它自己名下的 MCP 调用、它自己的浏览器和电脑的动作、它自己正在跑的钩子。共用的只有磁盘上的东西（各有各的锁）、
    权限（"要不要先问你"那一档是全局的一个，见 App.permissions）、遮密钥和备用模型的设置、花费账本。
    """

    agent: Agent  # 这段对话的助手：适配层（历史）、用量、运行记录、钩子都在它上面
    conversation: Conversation  # 这段会话
    shell: Shell  # 它自己的 shell
    tools: ToolRegistry  # 它自己的那一套基本工具：和 App.tools 一样，只是 run_command 用的是它自己的 shell
    browser: BrowserSession  # 它用的浏览器：它自己的标签页、这段对话里允许过的网站。换一段对话就换一个
    desktop: DesktopSession  # 它的电脑操作："这段对话里截图不再问""点击、打字不再问"记在它上面。换一段对话就换一个
    owner: object | None = None  # 它的 MCP 调用记在谁名下（见 McpManager.call）：一开始的那一段是 None，后来开的每段各一个
    # 这段对话的适配层用的是哪一版提示词。和 App.system_prompt 不一样，说明用户改过了：
    # 这一段照旧用它开始时的那版，下一段新对话（或者打开另一段会话）时换上新的
    prompt_in_use: str = ""
    # 这段对话是用哪个 Agent 当角色开始的（见 agents/ 开头的说明）；普通的对话是 None。
    # 它在这段对话开始时读好，之后不再变：你中途改了那个 Agent 的文件，从下一段用它开始的对话起才算数
    persona: CustomAgent | None = None
    # 这段对话开始时拍下的 MCP 快照（见 fresh_tools）。正看着它的时候，McpManager.active 就是它（信息栏和 MCP 一页按它画）
    mcp: Snapshot = field(default_factory=Snapshot)


@dataclass
class App:
    config: Config
    settings: Settings
    todos: TodoStore
    tasks: TaskStore  # 定时任务（只在网页界面开着的时候运行，见 webui/scheduler.py）
    tools: ToolRegistry  # 基本的工具（一开始的那一段对话、定时任务、聊天通道都从它换上自己的 shell）
    system_prompt: str  # 新的一段对话要用的系统提示词（用户可以改，见 prompt.py 和 set_prompt）
    client_factory: ClientFactory
    store: ConversationStore  # 保存在磁盘上的全部会话
    media: MediaStore  # 发过的附件的原件（多媒体库，见 media.py）
    ledger: Ledger  # 花费账本：每一次模型调用花了多少（见 ledger.py）
    skills: SkillStore  # 装好的 Skill（data/skills/）
    mcp: McpManager  # 接入的 MCP 服务器（见 mcp/）
    agents: AgentStore  # 自定义 Agent（data/agents/，见 agents/）
    delegator: Delegator  # 给每个会叫子 Agent 的一方做 agent 工具，并把子 Agent 跑起来（见 agents/runner.py）
    browser: BrowserManager  # 智狐自己的那个 Chrome（见 browser/）
    desktop: DesktopManager  # 操作整台电脑（见 desktop/）：进阶选项，默认关着
    hooks: Hooks  # 你的钩子（data/hooks.json，见 hooks/）：每次运行各用一个 HookRun，配置和"上一次跑得怎么样"是共用的这一份
    sandbox_layout: Layout  # 命令的沙箱要用的几个地方（见 sandbox/）：每次运行的 Sandbox 都照着它建
    endpoints: EndpointStore  # 自定义模型接口（data/endpoints.json，见 endpoints/）。providers.find / every 也看得到它们
    changes: FileChanges  # 每一轮文件工具改了哪些文件，和撤回、重做（见 undo/）。只有你的对话记账，定时任务和聊天通道不记
    # 你的对话共用的权限：几段对话同时进行时也是这一个，所以"要不要先问你"那一档是全局的（见 webui/settings_api.py）
    permissions: Permissions
    redact: Any = None  # 遮密钥（见 redact.py）：你的每段对话、定时任务、聊天通道、子 Agent 都用这同一个
    fallbacks: Callable[[str, str], list[str]] | None = None  # 备用模型的名单（见 fallback.py），每次现读设置
    chat: Chat = None  # type: ignore[assignment]  # 你正看着的这一段（见 Chat）。build_app 建好以后才有
    # 此刻活着的每一段（包括正看着的这一段）。网页界面换成它自己的那份名单（见 webui/sessions.py）；终端只有 chat 这一段
    live: Callable[[], Sequence[Chat]] | None = None
    warnings: list[str] = field(default_factory=list)  # 做成了、但有一点没做好的事，等界面取走显示（见 take_warnings）
    # 换"正看着哪一段"和设 McpManager.active 时拿的小锁：后台那一段刚拍完快照的同一刻你换了一段，不能把它的快照装到你正看着的这一段上
    _viewing: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ---- 你正看着的这一段（见 Chat）-----------------------------------------------

    @property
    def agent(self) -> Agent:
        return self.chat.agent

    @property
    def conversation(self) -> Conversation:
        return self.chat.conversation

    @conversation.setter
    def conversation(self, conversation: Conversation) -> None:
        self.chat.conversation = conversation

    @property
    def shell(self) -> Shell:
        return self.chat.shell

    @property
    def chat_browser(self) -> BrowserSession:
        return self.chat.browser

    @chat_browser.setter
    def chat_browser(self, session: BrowserSession) -> None:
        self.chat.browser = session  # 测试换成假的浏览器用

    @property
    def chat_desktop(self) -> DesktopSession:
        return self.chat.desktop

    @chat_desktop.setter
    def chat_desktop(self, session: DesktopSession) -> None:
        self.chat.desktop = session

    @property
    def persona(self) -> CustomAgent | None:
        return self.chat.persona

    @persona.setter
    def persona(self, persona: CustomAgent | None) -> None:
        self.chat.persona = persona

    @property
    def prompt_in_use(self) -> str:
        return self.chat.prompt_in_use

    @prompt_in_use.setter
    def prompt_in_use(self, text: str) -> None:
        self.chat.prompt_in_use = text

    def chats(self) -> list[Chat]:
        """此刻活着的每一段对话（终端只有正看着的这一段）。"""
        return list(self.live()) if self.live is not None else [self.chat]

    def view(self, chat: Chat) -> None:
        """换成看另一段（网页上同时进行几段对话时，见 webui/sessions.py）。只是换"正看着哪一段"：
        那一段自己的东西一样都不动，后台的那一段照常跑。信息栏、MCP 一页照着 McpManager.active 画，跟着换。"""
        with self._viewing:
            self.chat = chat
            self.mcp.active = chat.mcp

    def new_chat(self, ui: Any, shell: Shell | None = None) -> Chat:
        """准备一段新对话要用的全部东西（见 Chat）：此刻设置里选的那一家、那个模型，一段空的会话。

        build_app 用它建一开始的那一段（shell 是工具箱里 run_command 用的那一个）；网页上同时进行几段对话时，
        后开的每一段也用它建（见 webui/sessions.py）：自己的 shell、浏览器和电脑的 session、MCP 调用的名下。
        权限、遮密钥、备用模型、记账、钩子的配置都是共用的同一份。ui 是这一段自己的界面（网页上是它自己的事件日志）。
        """
        first = shell is not None
        shell = shell or Shell(self.config.workspace, Sandbox(self.sandbox_layout))
        tools = self.tools if first else self.tools.replaced(make_shell_tool(shell))
        provider_id, model = self.settings.provider, self.settings.model
        provider = find(provider_id)
        if provider is None:  # 设置里选的自定义接口不在了：用内置的默认那一家（和 new_conversation 一样）
            provider_id, model = FALLBACK
            provider = PROVIDERS[provider_id]
        prompt = build_system_prompt(self.config, web=self.config.web and provider.web)
        backend = make_backend(provider, model, self.config, tools, prompt, self.client_factory(provider))
        agent = Agent(backend=backend, config=self.config, tools=tools, permissions=self.permissions, ui=ui)
        if self.chat is not None:
            agent.clock = self.chat.agent.clock  # 和你正看着的那一段同一个钟（测试会把它固定住）
        chat = Chat(
            agent=agent,
            conversation=self._new(provider_id, model, None),
            shell=shell,
            tools=tools,
            browser=self.browser.session("chat"),
            desktop=self.desktop.session(desktop_labels.CHAT, attended=True),
            owner=None if first else object(),
            prompt_in_use=self.system_prompt,
        )
        self._wire(chat)
        return chat

    def _wire(self, chat: Chat) -> None:
        """给一段对话的助手接上共用的那几样：Skill 和 Agent 的目录、记账、遮密钥、备用模型、运行记录、钩子、撤回文件的说明。"""
        agent = chat.agent
        # 每条消息发出之前看一眼：可用的 Skill 和模型上次看到的不一样，就把新的目录附在这条消息前面（见 skills/catalog.py）。
        # 用一个 tools 里没有 Skill 的 Agent 当角色的对话，手里没有 skill 工具：不附（附了它也读不了，只会去调用一个不存在的工具）。
        # 用不了工具的模型（自定义接口上的一些模型）也一样：工具一个都没发给它
        skill_note = notifier(self.skills)
        agent.notes.append(lambda history: skill_note(history) if agent.tools.get(SKILL_TOOL) is not None and agent.backend.tools_supported else "")
        # 自定义 Agent（见 agents/）：主助手每条消息之前也看一眼有哪些 Agent 可以交给（只有手里有 agent 工具时才附）
        agent.notes.append(agent_notifier(self.agents, lambda: agent.tools if agent.backend.tools_supported else ToolRegistry([])))
        # 你撤回、重做了文件以后，这段会话的下一条消息前面附一段说明，模型才知道它记得的文件内容过时了（见 undo/notes.py）
        agent.notes.append(lambda history: self.changes.notes.text(chat.conversation.id))
        # 每调用一次模型记一笔账，价格按那一刻、用你在设置里填的（见 ledger.py）。终端和网页都记
        agent.on_call = self.ledger.recorder(self.prices)
        if self.redact is not None:
            agent.redact = self.redact
        agent.fallbacks = self.fallbacks
        # 每一轮结束时的运行记录（见 trace/）：网页界面存进会话的事件里，终端界面留着给 /trace。界面没有这一项（测试用的界面）就不交
        agent.on_trace = getattr(agent.ui, "trace", None)
        # 你的钩子：交给钩子的 session_id 是这一段的会话编号（不是你正看着的那一段的），沙箱是这一段自己的
        agent.hooks = HookRun(self.hooks, "chat", session=lambda: chat.conversation.id, sandbox=lambda: chat.shell.sandbox)
        # 第一条消息之前，手里的工具先按"这段对话会用的"摆好（带上 agent 工具）：信息栏列出来的才对得上。
        # 真正发第一条消息时还会再换一次，带上那一刻的 MCP 快照（见 fresh_tools）
        agent.use_tools(self.conversation_tools(None, chat=chat))
        agent.on_start = lambda: self.fresh_tools(chat)

    @property
    def provider(self) -> Provider:
        """当前使用的是哪一家。选中的自定义接口刚被删掉的话（删不掉正在用的，见 delete_endpoint，所以只有手改文件才会这样），
        按内置的默认那一家算。"""
        return find(self.settings.provider) or PROVIDERS["anthropic"]

    def prices(self) -> dict[str, dict]:
        """算钱用的价格表（见 merged_prices）。"""
        return merged_prices(self.settings, self.endpoints)

    def price_of(self, provider_id: str, model: str, offpeak: bool = False) -> dict[str, float] | None:
        """一个模型的价格：自定义接口上的看 endpoints.json（本机和内网的默认不花钱），内置的看你填的和内置的价格表。"""
        provider = find(provider_id)
        if provider is not None and provider.endpoint is not None:
            return provider.endpoint.price_of(model)
        return builtin_price(model, self.settings.prices, offpeak=offpeak)

    def set_price(self, provider_id: str, model: str, price: dict | None) -> None:
        """设置里"价格"一节：自定义接口上的模型记进 endpoints.json，内置的记进 settings.json。"""
        provider = find(provider_id)
        if provider is not None and provider.endpoint is not None:
            self.endpoints.set_price(provider.id, model, price)
        else:
            self.settings.set_price(model, price)

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
        channel：这段会话是在哪个聊天通道里开始的（见 conversation_tools），不给就是 chat 那一段的。
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

    def _new(self, provider_id: str, model: str, persona: CustomAgent | None) -> Conversation:
        live = {chat.conversation.id for chat in self.chats()} if self.chat is not None else set()
        conversation = self.store.new(provider_id, model)
        while conversation.id in live:  # 同一秒里开的另一段还没存盘（store.new 只查磁盘）：编号撞上了就再抽一个
            conversation = self.store.new(provider_id, model)
        conversation.agent = persona.name if persona is not None else ""
        return conversation

    def _remember_choice(self, provider_id: str, model: str) -> None:
        """把"现在用的是哪个模型"写进设置文件，下次启动还用它。

        写不进去（磁盘满了、没有权限）只是"没记住"，不该让已经完成的切换半途而废，所以接住错误、留一句提醒。
        """
        try:
            self.settings.choose(provider_id, model)
        except OSError as exc:
            self.warnings.append(t("没能把这次选择的模型记下来（{error}），下次启动时用的还是之前记住的那个。", error=exc))

    def unattended_agent(self, ui: Any, approval: str = "ask", sandbox: str = SANDBOX_DEFAULT) -> UnattendedRun:
        """给定时任务的一次运行准备一个助手，以及只属于这次运行的那几样东西（见 UnattendedRun）。

        定时任务和你的对话、和别的任务同时运行（见 webui/scheduler.py），所以凡是"会被停下"的东西都各用各的：
          - shell 是新建的：停下这次运行（调用的人把 run.stop 登记到停止上），只结束它自己正在运行的命令；
            你点对话里的"停止"、停下另一个任务，也碰不到它；
          - 适配层是新建的，历史是空的：任务从头开始，不带着你正在聊的内容，也不往你的对话里写东西；
          - 用的是此刻设置里选中的那一家、那个模型。之后你再换模型，只影响以后的运行；
          - 权限是这个任务自己的那一档（approval，见 Permissions.unattended），和你在对话里选的哪一档无关；
          - 沙箱也是这个任务自己的那一档（sandbox：default 跟随设置、off、workspace、network，见 sandbox/）；
          - MCP 的工具是开始运行那一刻拍的快照（和一段对话一样，见 fresh_tools），而且记在这次运行自己的名下：
            停下这次运行，只取消它自己正在等的 MCP 调用，你的对话和别的任务的不受影响（见 McpManager.call）。
        别的工具和对话共用：背后是同一份待办、任务、记忆和工作目录里的文件，它们各自有锁（见 TodoStore、TaskStore、
        tools/files.py、tools/memory.py），几边同时改也不会互相冲掉。
        """
        return self._side_run(ui, approval, self.settings.provider, self.settings.model, source="task", sandbox=lambda: sandbox)

    def channel_agent(
        self, ui: Any, approval: str, provider_id: str, model: str, persona: CustomAgent | None = None, outlet: Any = None,
        sandbox: Callable[[], str] = lambda: SANDBOX_DEFAULT,
    ) -> UnattendedRun:
        """给聊天通道里的一个聊天（比如你和 Telegram 机器人的私聊）准备一个助手，以及只属于这个聊天的那几样东西。

        和定时任务的一次运行是同一个做法（见上面的 unattended_agent），区别只有几处：
          - 用的是这个聊天自己那段会话的那一家、那个模型（provider_id、model），历史由调用的人交还给它（见 channels/session.py）；
          - 权限是这个通道自己的那一档（channels.json 里的 approval），和网页对话、定时任务的都无关；
          - 沙箱也是这个通道自己的那一档（channels.json 里的 sandbox）。sandbox 每条命令前读一次：你在页面上改了马上算数；
          - 花的钱记在 channel 名下；
          - 在聊天里发 /new 某个 Agent 的名字，这段会话就用它当角色（persona）：和网页上用它开始一段对话一样
            （见 new_conversation），提示词换成它的设定，工具只有它放开的那些。
          - 多一个 send_file 工具：把这台电脑上的文件发到这个聊天里（outlet 是这个聊天，见 channels/outbound.py 和 tools/send.py）。
        在聊天里发 /stop，只结束这个聊天自己的命令、MCP 调用和浏览器操作；网页上的"停止"、别的聊天、定时任务都碰不到它。
        每一轮结束时调用 run.close，关掉这个聊天自己的浏览器标签页（和定时任务的一次运行一样）。
        """
        return self._side_run(ui, approval, provider_id, model, source="channel", persona=persona, outlet=outlet, sandbox=sandbox)

    def _side_run(
        self, ui: Any, approval: str, provider_id: str, model: str, source: str, persona: CustomAgent | None = None, outlet: Any = None,
        sandbox: Callable[[], str] = lambda: SANDBOX_DEFAULT,
    ) -> UnattendedRun:
        """定时任务和聊天通道共用的那一套：自己的 shell、自己名下的 MCP 调用、自己那一档权限、自己那一档沙箱、记在 source 名下的账。"""
        permissions = self.permissions.unattended(approval)
        # 沙箱：这次运行自己的那一档。没人看着的运行，选了"全部自动允许"就不许请人放行（在沙箱之外运行一条），
        # 别的几档照样问你（见 sandbox/session.py）。看的是此刻的模式：聊天通道每一轮会按你在页面上改过的重新设
        escape = lambda: permissions.mode != "all"
        shell = Shell(self.config.workspace, Sandbox(self.sandbox_layout, own=sandbox, escape=escape, scope=source))
        owner = object()  # 这次运行的 MCP 调用都记在它名下（见 McpManager.call）
        # 浏览器也是这次运行自己的：自己的标签页，你的对话和别的任务看不到、碰不到（见 browser/session.py）。
        # 放在一个格子里：聊天通道里一段会话要跑很多轮，每一轮结束都 close（关掉自己的标签页），下一轮换一个新的 session，
        # 工具还是同一套（一段对话里工具说明书不变）。定时任务只跑一轮，换上的那个新的没人用，随着这次运行一起丢掉
        browser = [self.browser.session(source)]
        own_browser = make_browser_tools(lambda: browser[0], lambda: self.browser.enabled) if self.browser.enabled else []
        # 操作整台电脑：只有你在设置里另外打开了"允许定时任务和聊天通道使用"才有（默认没有）。有的话按这次运行自己的那一档问
        # （任务、通道选了"全部自动允许"才不问；危险的组合键怎么都要问，见 permissions.py）。session 不是 attended 的：
        # 没有"这段对话里不再问"。和浏览器一样放在格子里，每一轮结束换一个新的
        desktop = [self.desktop.session(desktop_labels.TASK if source == "task" else desktop_labels.CHANNEL)]
        own_desktop = make_computer_tools(lambda: desktop[0], lambda: self.desktop.unattended) if self.desktop.unattended else []
        # 聊天通道里的会话还有 send_file（发到这个聊天里）。放的位置和网页上打开这段会话时一样（见 conversation_tools）：
        # 两边的工具清单一字不差，在网页上接着聊也照样命中缓存
        own_send = [make_send_tool(self.permissions.editor, outlet)] if source == "channel" else []
        tools = ToolRegistry([*self.tools.replaced(make_shell_tool(shell)), *own_browser, *own_desktop, *own_send])
        backend = self._backend_for(provider_id, model, persona)
        agent = Agent(backend=backend, config=self.config, tools=tools, permissions=permissions, ui=ui, clock=self.agent.clock)
        # 也能把事情交给子 Agent：它的 agent 工具由它自己来"叫"（确认按这次运行的那一档，停止只停这次运行，见 agents/runner.py）
        delegate = self.delegator.tool_for(agent)
        # 这一次运行也是"一段对话"：用此刻已经连上的 MCP 服务器的工具拍一张快照，跑完之前不再变。
        # 不改 self.mcp.active：那是你正在看的这段对话的快照，信息栏和 MCP 一页按它显示
        everything = [*tools, *([delegate] if delegate else []), *self.mcp.snapshot(owner).tools]
        # 用一个 Agent 当角色的：只留它放开的工具（和网页上的 conversation_tools 一样）
        agent.use_tools(ToolRegistry(agent_tools.allowed(persona.spec.tools, everything) if persona is not None else everything))
        # 花的钱也记进账本，来源写成 task 或 channel，花费明细里就能和对话分开看。每次运行各有一个助手，各自记各自的
        agent.on_call = self.ledger.recorder(self.prices, source=source)
        agent.redact = self.agent.redact  # 遮密钥：和你的对话同一个（同一份已知的密钥、同一个开关，见 redact.py）
        agent.fallbacks = self.fallbacks  # 备用模型和你的对话用同一份设置（每次现读，见 build_app）
        # 每一轮的运行记录（见 trace/）交给这次运行自己的界面：存进这次运行（这个聊天）的会话里，和网页上的对话一样
        agent.on_trace = getattr(ui, "trace", None)
        # 你的钩子也照样跑（runsIn 里有 task / channel 的那些，默认都有，理由见 hooks/__init__.py）。
        # 这次运行自己一份：停下这次运行只结束它自己正在跑的钩子。会话编号由调用的人在知道以后填上（见 scheduler.py、channels/session.py）
        agent.hooks = HookRun(self.hooks, source, sandbox=lambda: shell.sandbox)
        # 也能用 Skill：历史是空的话，第一条消息就带上此刻的 Skill 目录（见 skills/catalog.py）
        agent.notes.append(notifier(self.skills))
        agent.notes.append(agent_notifier(self.agents, lambda: agent.tools))  # 有哪些 Agent 可以交给，也一样

        def stop() -> None:
            shell.kill_running()
            agent.hooks.stop()
            self.mcp.cancel_running(owner)
            browser[0].stop()
            desktop[0].stop()

        def close() -> None:
            browser[0].close()
            browser[0] = self.browser.session(source)
            desktop[0].close()  # 屏幕让出来，还按着的鼠标键松开
            desktop[0] = self.desktop.session(desktop[0].label)

        return UnattendedRun(agent=agent, shell=shell, stop=stop, close=close)

    def take_warnings(self) -> list[str]:
        """取走攒下的提醒（取走就清空）。"""
        taken, self.warnings = self.warnings, []
        return taken

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
        # 手里的工具换成新的这一段会用的（它还没开始，换了不丢任何东西）：角色只放开的那些、上一段中途打开的浏览器……
        # 上一段是网页上打开的聊天通道会话的话，新的这一段不在聊天软件里，不带 send_file。
        # 信息栏里"智狐能做的事"照着手里的工具画（见 toolbox），不换的话第一条消息之前列的还是上一段的
        self.refresh_tools(chat)
        if persona is not None and persona.spec.model:
            self._remember_choice(provider_id, model)

    def open_conversation(self, conversation_id: str, chat: Chat | None = None) -> Conversation:
        """打开一段保存过的会话，接着聊。chat：在哪一段里打开（它原来的那段会话就离开了），不给是你正看着的这一段。

        做的事情只有两件：换回这段会话当时用的模型，把它的对话历史交还给适配层。
        下一次请求会把这份历史一起发给模型，模型就"想起"了之前聊过什么。
        """
        chat = chat or self.chat
        conversation = self.store.load(conversation_id)
        provider = find(conversation.provider)
        if provider is None and conversation.provider.startswith(ENDPOINT_PREFIX):
            raise ConversationError(t("这段会话用的是自定义模型接口（{provider}），它已经被删掉了。在设置里重新添加一个同名的接口，就能接着聊。", provider=conversation.provider))
        if provider is None:
            raise ConversationError(t("这段会话用的模型提供方，这个版本的程序已经不支持了。"))
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
        chat.conversation = conversation  # 先换上：它的工具要看它是不是在聊天通道里开始的（见 conversation_tools）
        self.fresh_tools(chat)  # 接着聊也算这一次运行里"开始"的一段对话：用此刻的 MCP 工具，之后不再变
        chat.agent.usage.context_tokens = conversation.context_tokens
        self._remember_choice(conversation.provider, conversation.model)
        return conversation

    # ---- 工具 -------------------------------------------------------------

    def fresh_tools(self, chat: Chat | None = None) -> None:
        """换上这段对话要用的工具：自己的工具，加上此刻已经连上的 MCP 服务器的工具。chat 不给是你正看着的这一段。

        MCP 的那部分是一张"快照"，这段对话从头到尾都用它（原因见 mcp/manager.py）。
        新对话的第一条消息发出之前调用（agent.on_start），打开一段旧会话时也调用一次。
        快照里的工具发出的调用记在这一段的名下（chat.owner）："停止"只取消这一段自己的。
        """
        chat = chat or self.chat
        shot = self.mcp.snapshot(chat.owner)
        with self._viewing:
            chat.mcp = shot
            if chat is self.chat:
                self.mcp.active = shot  # 信息栏和 MCP 一页照着你正看着的这一段画；后台那一段的快照只记在它自己身上
        chat.agent.use_tools(self.conversation_tools(chat.persona, shot.tools, chat=chat))

    def conversation_tools(self, persona: CustomAgent | None, mcp_tools: Sequence[Any] = (), channel: str | None = None, chat: Chat | None = None) -> ToolRegistry:
        """一段对话用的工具：自己的工具、浏览器、agent 工具（把任务交给子 Agent，一直都在，见 tools/agent.py）、MCP 的快照。
        用一个 Agent 当角色的对话，只留它的 tools 放开的那些（见 agents/tools.py）。chat 不给是你正看着的这一段。

        浏览器的工具和 MCP 一样算在"这段对话开始时拍的快照"里：设置里开着才带上（Claude 的浏览器工具包每次请求
        要多发六千多个 token 的说明书，没开的人不该为它付钱），开始以后再开、再关，说明书都不变（见 tools/browser.py）。
        打开一段用过浏览器的旧会话，也照样带上，历史里的那些调用才对得上。

        channel：这段会话是在哪个聊天通道里开始的（不给就看 chat 那一段）。是的话带上 send_file：在网页上接着聊一段 Telegram 里的会话，
        工具清单和在 Telegram 里时一样（缓存照样命中），只是它发不出去（没有去处，见 tools/send.py）。普通的对话没有它。"""
        chat = chat or self.chat
        channel = chat.conversation.channel if channel is None else channel
        delegate = self.delegator.tool_for(chat.agent)
        # 浏览器和电脑的 session 每次调用时现找：换一段对话时 chat 换上新的（见 _fresh_browser），工具还是这一套
        browser = make_browser_tools(lambda: chat.browser, lambda: self.browser.enabled) if self._wants_browser(chat) else []
        # 操作整台电脑也一样：设置里开着才带上（工具包的说明书每次请求约 4500 个 token），用过它的旧会话也带上
        desktop = make_computer_tools(lambda: chat.desktop, lambda: self.desktop.enabled) if self._wants_computer(chat) else []
        send = [make_send_tool(self.permissions.editor, None)] if channel else []
        tools = [*chat.tools, *browser, *desktop, *send, *([delegate] if delegate else []), *mcp_tools]
        return ToolRegistry(agent_tools.allowed(persona.spec.tools, tools) if persona is not None else tools)

    def _wants_browser(self, chat: Chat) -> bool:
        if self.browser.enabled:
            return True
        backend = chat.agent.backend
        return browser_used_in(backend.export()) if backend is not None else False

    def _wants_computer(self, chat: Chat) -> bool:
        if self.desktop.enabled:
            return True
        backend = chat.agent.backend
        return computer_used_in(backend.export()) if backend is not None else False

    def _fresh_desktop(self, chat: Chat) -> None:
        """换了一段对话："这段对话里截图不再问"不再算数，还按着的鼠标键松开，屏幕让出来。新的这一段用一个新的。"""
        old, chat.desktop = chat.desktop, self.desktop.session(desktop_labels.CHAT, attended=True)
        old.close()

    def _fresh_browser(self, chat: Chat) -> None:
        """换了一段对话：上一段的浏览器标签页关掉，"允许过的网站"也不再算数。新的这一段用一个新的。"""
        old, chat.browser = chat.browser, self.browser.session("chat")
        threading.Thread(target=old.close, daemon=True).start()  # 关标签页要和 Chrome 来回几次：不让换会话等它

    def close_chat(self, chat: Chat) -> None:
        """一段对话不再用了（网页上一段在后台跑完、存好了盘，见 webui/sessions.py）：关掉它的浏览器标签页，屏幕让出来。"""
        chat.desktop.close()
        threading.Thread(target=chat.browser.close, daemon=True).start()  # 和 _fresh_browser 一样：不让别人等它

    def refresh_tools(self, chat: Chat | None = None) -> None:
        """设置里开、关了浏览器（或者"操作整台电脑"）：这段对话还没开始的话，马上换上（信息栏里的工具清单跟着变）；开始了的不动（见 conversation_tools）。"""
        chat = chat or self.chat
        if not chat.agent.backend.export():
            chat.agent.use_tools(self.conversation_tools(chat.persona, chat=chat))

    def persona_info(self) -> dict | None:
        """当前这段对话的角色（页面顶上显示它的名字）：名字、颜色；它现在用不了的话 missing 为真。普通的对话是 None。"""
        name = self.conversation.agent
        if not name:
            return None
        persona = self.persona if self.persona is not None and self.persona.name == name else None
        return {"name": name, "color": persona.spec.color if persona else "", "missing": persona is None}

    def toolbox(self) -> tuple[list[Any], bool]:
        """这段对话的工具：真正发给模型的那些 Tool（这家模型用不了的不算，见 Backend.sends），和这段对话开始了没有。

        对话已经开始了，就是这段对话实际在用的那一套。还没开始的话，是"现在发第一条消息会用上的"：
        这时手里的 MCP 工具还是上一段对话的快照，所以换成此刻的（新连上的服务器也在里面，见 fresh_tools）。
        信息栏里"智狐能做的事"（capabilities.py）和终端的 /tools 都照着它。
        """
        backend = self.agent.backend
        tools = [tool for tool in self.agent.tools if backend.sends(tool)]
        if backend.export():
            return tools, True
        fresh = self.mcp.snapshot().tools
        if self.persona is not None:
            fresh = agent_tools.allowed(self.persona.spec.tools, fresh)
        own = [tool for tool in tools if not tool.name.startswith(MCP_PREFIX)]
        return own + [tool for tool in fresh if backend.sends(tool)], False

    def tool_names(self) -> list[tuple[str, bool]]:
        """这段对话的工具的名字：（名字，是否在本地执行）。工具包只算一个（用包名）。"""
        names: list[tuple[str, bool]] = []
        for tool in self.toolbox()[0]:
            entry = (tool.toolset, True) if tool.toolset else (tool.name, tool.run is not None)
            if entry not in names:
                names.append(entry)
        return names

    def close(self) -> None:
        """程序退出前的收尾：关掉启动过的 MCP 服务器进程和浏览器，还按着的鼠标键松开。"""
        self.mcp.shutdown()
        self.browser.close()
        for chat in self.chats():
            chat.desktop.close()

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
            summaries = event.get("attachments") if isinstance(event.get("attachments"), list) else []
            if auto(event.get("text") if isinstance(event.get("text"), str) else "", summaries) == title:
                return
        chat.conversation.title = ""
        self.name_conversation(text, attachments, chat=chat)

    def attachment_problem(self, attachments: Sequence[Attachment]) -> str:
        """当前的模型收不收得了这些附件。收不了的话返回说明，收得了返回空字符串。
        自定义接口按这个模型算（同一个 Ollama 上有的模型看得懂图，有的看不懂，见 providers.caps）。"""
        provider = self.provider
        accepts = caps(provider, self.settings.model)
        return unsupported(attachments, provider.label, accepts.vision, accepts.pdf)

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

    def set_api_key(self, provider_id: str, key: str) -> None:
        """保存（或删除）一家的 API 密钥，并让它立即生效。只管内置的几家：自定义接口的密钥在它自己的设置里（见 save_endpoint）。"""
        provider = PROVIDERS[provider_id]
        save_api_key(self.config.env_file, provider.key_env, key)
        for chat in self.chats():
            # 正在用这一家的那几段：换上带新 key 的连接（只是换掉一个属性，后台那一段正在等的这次请求照旧用原来的连接，下一次用新的）
            if provider_id == chat.agent.backend.provider:
                chat.agent.backend.client = self.client_factory(provider)

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
        backend.client = self.client_factory(provider)
        backend.label = provider.label
        backend.stream_usage = provider.endpoint.stream_usage if provider.endpoint is not None else True
        switch_model(backend, provider, backend.model)


def build_app(config: Config, ui: Any, client_factory: ClientFactory = make_client) -> App:
    settings = Settings(config.settings_file, config.default_provider, config.default_model)
    i18n.choose(settings.language)  # 界面语言：从这里开始，程序里所有 t() 都译成它
    # 自定义模型接口（见 endpoints/）：交给 providers 的登记表，之后 find / every 都看得到它们
    endpoints = EndpointStore(config.endpoints_file)
    use_custom(endpoints)
    if find(settings.provider) is None:
        settings.provider, settings.model = FALLBACK

    # mcp.json 和 .env 一样不让文件工具碰：里面有 MCP 服务器的密钥，而且改了它就能让智狐下次启动时运行任意程序。
    # channels.json 也是：里面有聊天机器人的 token（见 channels/store.py），改了它还能把机器人绑到别人的账号上。
    # commands.json（你自己加的"不用问的命令"）也是：改了它，模型就能让自己想运行的命令不再问你（见 commands/allowlist.py）。
    # hooks.json（你的钩子）也是：改了它，就能让智狐在固定的时刻运行任何命令（见 hooks/）
    # endpoints.json（自定义模型接口）也是：里面有密钥，改了它的地址还能让你的对话发到别人的服务器上
    commands = CommandList(config.commands_file)
    protected = config.protected_files
    files = FileEditor(config.workspace, config.backup_dir, protected=protected)
    todos = TodoStore(config.todo_file)
    tasks = TaskStore(config.tasks_file)
    # 命令的沙箱（见 sandbox/）：数据目录和保护的文件在沙箱里永远碰不到；对话用设置里的那一档，你正看着，可以请你放行一条
    layout = sandbox_layout(config, settings)
    shell = Shell(config.workspace, Sandbox(layout))
    skills = SkillStore(config.data_dir)
    tools = build_registry(config, files, todos, shell, tasks, skills)
    system_prompt = build_system_prompt(config)
    mcp = McpManager(McpConfig(config.mcp_file))
    permissions = Permissions(files, private=[config.data_dir], tasks=tasks, mcp=mcp, commands=commands)
    # 每调用一次模型记一笔账（见 ledger.py 和 App._wire）。终端和网页都记
    ledger = Ledger(config.ledger_file)

    def prices() -> dict[str, dict]:
        return merged_prices(settings, endpoints)

    agents = AgentStore(config.agents_dir)
    delegator = Delegator(config, agents, skills, mcp, ledger, prices, client_factory)
    store = ConversationStore(config.conversations_dir)
    browser = BrowserManager(config.browser_dir, settings)
    desktop = DesktopManager(config.desktop_dir, settings)
    changes = FileChanges(config.undo_dir, files, private=[config.data_dir])
    changes.prune()  # 过期的留底（只留 UNDO_KEEP_DAYS 天）启动时删掉
    app = App(
        config=config,
        settings=settings,
        todos=todos,
        tasks=tasks,
        tools=tools,
        system_prompt=system_prompt,
        client_factory=client_factory,
        store=store,
        media=MediaStore(config.media_dir),
        ledger=ledger,
        skills=skills,
        mcp=mcp,
        agents=agents,
        delegator=delegator,
        browser=browser,
        desktop=desktop,
        hooks=Hooks(HookConfig(config.hooks_file), config.workspace),
        sandbox_layout=layout,
        endpoints=endpoints,
        changes=changes,
        permissions=permissions,
        # 工具的结果先遮一遍密钥再交给模型（见 redact.py）：认 .env 里的、自定义接口的密钥，开没开每次现看设置。
        # 你的每段对话、定时任务、聊天通道、子 Agent 都用这同一个（见 _wire、_side_run 和 agents/runner.py）
        redact=Redactor(
            config.env_file,
            known=lambda: [("自定义接口的密钥", endpoint.api_key) for endpoint in endpoints.list() if endpoint.api_key],
            enabled=lambda: settings.redact_secrets,
        ),
        # 模型暂时用不了时改用的同一家的备用模型（见 fallback.py）：每次现读设置，你在设置里改了，下一次就按新的来
        fallbacks=lambda provider, model: fallback.candidates(settings, provider, model),
    )
    # 一开始的那一段对话：它的 run_command 就是工具箱里的那一个（shell）。网页上同时进行的别的几段由 webui/sessions.py 另外建
    app.chat = app.new_chat(ui, shell=shell)
    mcp.start()  # 每个启用的服务器在自己的后台线程里连接，这里不等
    return app


def sandbox_layout(config: Config, settings: Settings) -> Layout:
    """命令的沙箱要用的几个地方（见 sandbox/session.py）。设置里的那一档和额外允许的路径每条命令前现读。
    命令行的 harness sandbox check 也用它，和真的运行时一模一样。"""
    return Layout(
        workspace=config.workspace,
        private=(config.data_dir,),
        protected=tuple(config.protected_files),
        extra=lambda: settings.sandbox["paths"],
        default=lambda: settings.sandbox["level"],
    )


def run_turn(agent: Agent, ui: Any, text: str, attachments: Sequence[Attachment] | None = None, again: bool = False) -> None:
    """跑一轮对话。出错后程序不退出，把原因显示出来，可以接着聊。附件只有网页界面会传。
    again：重新生成一轮没答出来的（见 Agent.run_turn）。"""
    _guarded(ui, lambda: agent.run_turn(text, attachments, again))


def retry_turn(agent: Agent, ui: Any, start: int) -> None:
    """重新生成最后一轮的回答（见 Agent.retry_turn）。出错时和 run_turn 一样，说明原因、不退出。"""
    _guarded(ui, lambda: agent.retry_turn(start))


def _guarded(ui: Any, work: Callable[[], None]) -> None:
    try:
        work()
    except KeyboardInterrupt:
        ui.info(t("已中断。"))
    except ProviderError as exc:
        ui.error(i18n.message_of(exc))
    except Exception as exc:  # noqa: BLE001 —— 故意接住所有异常：哪里出了意料之外的错，都要让用户看到，而不是让程序退出
        ui.error(t("这一轮出错了：{error}", error=f"{type(exc).__name__}: {exc}"))
