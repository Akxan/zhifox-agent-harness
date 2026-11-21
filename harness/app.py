"""组装：把配置、工具、权限、模型和循环拼成一个可以运行的助手。

终端版和网页版从这里拿到的是同一套东西，区别只在于传进来的界面（ui）不同。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import budget, i18n
from .agent import Agent
from .agents import AgentError, AgentStore, CustomAgent
from .agents import tools as agent_tools
from .agents.catalog import notifier as agent_notifier
from .agents.prompt import persona_prompt
from .agents.runner import Delegator
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
from .mcp import McpConfig, McpManager
from .mcp.bridge import PREFIX as MCP_PREFIX
from .media import MediaStore
from .permissions import Permissions
from .prompt import build_system_prompt, reset_template, save_template
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
from .skills import SkillStore
from .skills.catalog import notifier
from .tasks import TaskStore
from .tools import ToolRegistry, build_registry
from .tools.files import FileEditor
from .tools.shell import Shell, make_shell_tool
from .tools.skill import TOOL_NAME as SKILL_TOOL
from .tools.todo import TodoStore


@dataclass
class UnattendedRun:
    """定时任务的一次运行用的东西（见 App.unattended_agent）。"""

    agent: Agent
    shell: Shell  # 这次运行自己的 shell
    stop: Callable[[], None]  # 停下这次运行正在做的事：结束它正在运行的命令、取消它正在等的 MCP 调用。别人的不碰


@dataclass
class App:
    config: Config
    settings: Settings
    agent: Agent
    todos: TodoStore
    tasks: TaskStore  # 定时任务（只在网页界面开着的时候运行，见 webui/scheduler.py）
    tools: ToolRegistry
    shell: Shell
    system_prompt: str  # 新的一段对话要用的系统提示词（用户可以改，见 prompt.py 和 set_prompt）
    client_factory: ClientFactory
    store: ConversationStore  # 保存在磁盘上的全部会话
    conversation: Conversation  # 当前正在进行的这一段
    media: MediaStore  # 发过的附件的原件（多媒体库，见 media.py）
    ledger: Ledger  # 花费账本：每一次模型调用花了多少（见 ledger.py）
    skills: SkillStore  # 装好的 Skill（data/skills/）
    mcp: McpManager  # 接入的 MCP 服务器（见 mcp/）
    agents: AgentStore  # 自定义 Agent（data/agents/，见 agents/）
    delegator: Delegator  # 给每个会叫子 Agent 的一方做 agent 工具，并把子 Agent 跑起来（见 agents/runner.py）
    warnings: list[str] = field(default_factory=list)  # 做成了、但有一点没做好的事，等界面取走显示（见 take_warnings）
    # 当前这段对话的适配层用的是哪一版提示词。和 system_prompt 不一样，说明用户改过了：
    # 这一段照旧用它开始时的那版，下一段新对话（或者打开另一段会话）时换上新的
    prompt_in_use: str = ""
    # 当前这段对话是用哪个 Agent 当角色开始的（见 agents/ 开头的说明）；普通的对话是 None。
    # 它在这段对话开始时读好，之后不再变：你中途改了那个 Agent 的文件，从下一段用它开始的对话起才算数
    persona: CustomAgent | None = None

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
            backend = self._backend_for(provider_id, model, self.persona)  # 会失败的先做。用 Agent 开始的对话，新的一段还是它
            self.agent.backend = backend
            self.prompt_in_use = self.system_prompt
            self.conversation = self._new(provider_id, model, self.persona)
            self.agent.usage.context_tokens = 0
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
        if self.agent.backend.export():
            return False
        backend = self.agent.backend
        self.agent.backend = self._backend_for(backend.provider, backend.model, self.persona)
        self.prompt_in_use = self.system_prompt
        return True

    def _backend_for(self, provider_id: str, model: str, persona: CustomAgent | None = None) -> Any:
        """建一个连着指定模型的新适配层（历史是空的）。persona：用哪个 Agent 当角色（None 是普通的对话）。"""
        provider = PROVIDERS[provider_id]
        tools = self.conversation_tools(persona)
        web = self.config.web and provider.web  # 能不能联网，要看是哪一家
        if persona is None:
            prompt = build_system_prompt(self.config, web=web)
        else:
            # 角色的提示词是它自己的设定加上程序附上的环境说明（见 agents/prompt.py）；它的 tools 没放开联网，就不写"能联网"
            prompt = persona_prompt(self.config, persona, web=web and agent_tools.has_web(tools))
        return make_backend(provider, model, self.config, tools, prompt, self.client_factory(provider))

    def _new(self, provider_id: str, model: str, persona: CustomAgent | None) -> Conversation:
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

    def unattended_agent(self, ui: Any, approval: str = "ask") -> UnattendedRun:
        """给定时任务的一次运行准备一个助手，以及只属于这次运行的那几样东西（见 UnattendedRun）。

        定时任务和你的对话、和别的任务同时运行（见 webui/scheduler.py），所以凡是"会被停下"的东西都各用各的：
          - shell 是新建的：停下这次运行（调用的人把 run.stop 登记到停止上），只结束它自己正在运行的命令；
            你点对话里的"停止"、停下另一个任务，也碰不到它；
          - 适配层是新建的，历史是空的：任务从头开始，不带着你正在聊的内容，也不往你的对话里写东西；
          - 用的是此刻设置里选中的那一家、那个模型。之后你再换模型，只影响以后的运行；
          - 权限是这个任务自己的那一档（approval，见 Permissions.unattended），和你在对话里选的哪一档无关；
          - MCP 的工具是开始运行那一刻拍的快照（和一段对话一样，见 fresh_tools），而且记在这次运行自己的名下：
            停下这次运行，只取消它自己正在等的 MCP 调用，你的对话和别的任务的不受影响（见 McpManager.call）。
        别的工具和对话共用：背后是同一份待办、任务、记忆和工作目录里的文件，它们各自有锁（见 TodoStore、TaskStore、
        tools/files.py、tools/memory.py），几边同时改也不会互相冲掉。
        """
        shell = Shell(self.config.workspace)
        owner = object()  # 这次运行的 MCP 调用都记在它名下（见 McpManager.call）
        tools = self.tools.replaced(make_shell_tool(shell))
        backend = self._backend_for(self.settings.provider, self.settings.model)
        permissions = self.agent.permissions.unattended(approval)
        agent = Agent(backend=backend, config=self.config, tools=tools, permissions=permissions, ui=ui, clock=self.agent.clock)
        # 任务也能把事情交给子 Agent：它的 agent 工具由它自己来"叫"（确认按这个任务的那一档，停止只停这次运行，见 agents/runner.py）
        delegate = self.delegator.tool_for(agent)
        # 任务的这一次运行也是"一段对话"：用此刻已经连上的 MCP 服务器的工具拍一张快照，跑完之前不再变。
        # 不改 self.mcp.active：那是你正在看的这段对话的快照，信息栏和 MCP 一页按它显示
        agent.use_tools(ToolRegistry([*tools, *([delegate] if delegate else []), *self.mcp.snapshot(owner).tools]))
        # 定时任务花的钱也记进账本，来源写成 task，花费明细里就能和对话分开看。每次运行各有一个助手，各自记各自的
        agent.on_call = self.ledger.recorder(lambda: self.settings.prices, source="task")
        # 任务也能用 Skill：它的历史是空的，所以第一条消息就带上此刻的 Skill 目录（见 skills/catalog.py）
        agent.notes.append(notifier(self.skills))
        agent.notes.append(agent_notifier(self.agents, lambda: agent.tools))  # 有哪些 Agent 可以交给，也一样

        def stop() -> None:
            shell.kill_running()
            self.mcp.cancel_running(owner)

        return UnattendedRun(agent=agent, shell=shell, stop=stop)

    def take_warnings(self) -> list[str]:
        """取走攒下的提醒（取走就清空）。"""
        taken, self.warnings = self.warnings, []
        return taken

    # ---- 会话 -------------------------------------------------------------

    def new_conversation(self, agent: str | None = None) -> None:
        """开始一段新会话。之前的会话已经在磁盘上，随时可以再打开。

        agent：用哪个自定义 Agent 当角色（见 agents/ 开头的说明）。这段对话的系统提示词换成它的设定，工具只有它放开的，
        模型用它选的那个（它写了的话；这个选择也会记成"现在用的模型"，和打开一段旧会话一样）。
        这个 Agent 不存在或者不合格时抛出 AgentError，什么都不换。
        """
        persona = self.agents.usable(agent) if agent else None  # 会失败的先做
        if persona is not None and persona.spec.model:
            provider_id, model = persona.spec.provider, persona.spec.model_id
        else:
            provider_id, model = self.settings.provider, self.settings.model
        backend = self.agent.backend
        if persona is not None or self.persona is not None or self.prompt_in_use != self.system_prompt or provider_id != backend.provider:
            # 提示词不一样了（换了角色、用户改过提示词、换了一家）：新的一段用新的。提示词存在适配层里，
            # 所以换一个新建的（历史本来就要清空）
            self.agent.backend = self._backend_for(provider_id, model, persona)
            self.prompt_in_use = self.system_prompt
        else:
            self.agent.reset()
            backend.model = model
        self.persona = persona
        self.conversation = self._new(provider_id, model, persona)
        self.agent.usage.context_tokens = 0
        if persona is not None and persona.spec.model:
            self._remember_choice(provider_id, model)

    def open_conversation(self, conversation_id: str) -> Conversation:
        """打开一段保存过的会话，接着聊。

        做的事情只有两件：换回这段会话当时用的模型，把它的对话历史交还给适配层。
        下一次请求会把这份历史一起发给模型，模型就"想起"了之前聊过什么。
        """
        conversation = self.store.load(conversation_id)
        if conversation.provider not in PROVIDERS:
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
        same_provider = conversation.provider == self.agent.backend.provider
        # 同一家、提示词也没改过、前后都不是角色，就接着用现在的适配层；否则新建一个（带着现在的提示词）
        reuse = same_provider and self.prompt_in_use == self.system_prompt and persona is None and self.persona is None
        backend = self.agent.backend if reuse else self._backend_for(conversation.provider, conversation.model, persona)
        backend.model = conversation.model
        backend.restore(conversation.messages)
        self.agent.backend = backend
        self.prompt_in_use = self.system_prompt
        self.persona = persona
        self.fresh_tools()  # 接着聊也算这一次运行里"开始"的一段对话：用此刻的 MCP 工具，之后不再变
        self.conversation = conversation
        self.agent.usage.context_tokens = conversation.context_tokens
        self._remember_choice(conversation.provider, conversation.model)
        return conversation

    # ---- 工具 -------------------------------------------------------------

    def fresh_tools(self) -> None:
        """换上这段对话要用的工具：自己的工具，加上此刻已经连上的 MCP 服务器的工具。

        MCP 的那部分是一张"快照"，这段对话从头到尾都用它（原因见 mcp/manager.py）。
        新对话的第一条消息发出之前调用（agent.on_start），打开一段旧会话时也调用一次。
        """
        shot = self.mcp.snapshot()
        self.mcp.active = shot
        self.agent.use_tools(self.conversation_tools(self.persona, shot.tools))

    def conversation_tools(self, persona: CustomAgent | None, mcp_tools: Sequence[Any] = ()) -> ToolRegistry:
        """一段对话用的工具：自己的工具、agent 工具（把任务交给子 Agent，一直都在，见 tools/agent.py）、MCP 的快照。
        用一个 Agent 当角色的对话，只留它的 tools 放开的那些（见 agents/tools.py）。"""
        delegate = self.delegator.tool_for(self.agent)
        tools = [*self.tools, *([delegate] if delegate else []), *mcp_tools]
        return ToolRegistry(agent_tools.allowed(persona.spec.tools, tools) if persona is not None else tools)

    def persona_info(self) -> dict | None:
        """当前这段对话的角色（页面顶上显示它的名字）：名字、颜色；它现在用不了的话 missing 为真。普通的对话是 None。"""
        name = self.conversation.agent
        if not name:
            return None
        persona = self.persona if self.persona is not None and self.persona.name == name else None
        return {"name": name, "color": persona.spec.color if persona else "", "missing": persona is None}

    def tool_names(self) -> list[tuple[str, bool]]:
        """界面上"模型可以使用的工具"：（名字，是否在本地执行）。

        对话已经开始了，就是这段对话实际在用的那一套。还没开始的话，列的是"现在发第一条消息会用上的"：
        这时手里的还是上一段对话的快照，新连上的服务器还不在里面。
        """
        names = self.agent.backend.tool_names()
        if self.agent.backend.export():
            return names
        own = [(name, local) for name, local in names if not name.startswith(MCP_PREFIX)]
        fresh = self.mcp.snapshot().tools
        if self.persona is not None:
            fresh = agent_tools.allowed(self.persona.spec.tools, fresh)
        return own + [(tool.name, True) for tool in fresh]

    def close(self) -> None:
        """程序退出前的收尾：关掉启动过的 MCP 服务器进程。"""
        self.mcp.shutdown()

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

    # mcp.json 和 .env 一样不让文件工具碰：里面有 MCP 服务器的密钥，而且改了它就能让智狐下次启动时运行任意程序
    files = FileEditor(config.workspace, config.backup_dir, protected=[config.env_file, config.mcp_file])
    todos = TodoStore(config.todo_file)
    tasks = TaskStore(config.tasks_file)
    shell = Shell(config.workspace)
    skills = SkillStore(config.data_dir)
    tools = build_registry(config, files, todos, shell, tasks, skills)
    system_prompt = build_system_prompt(config)

    provider = PROVIDERS[settings.provider]
    prompt = build_system_prompt(config, web=config.web and provider.web)
    backend = make_backend(provider, settings.model, config, tools, prompt, client_factory(provider))
    mcp = McpManager(McpConfig(config.mcp_file))
    permissions = Permissions(files, private=[config.data_dir], tasks=tasks, mcp=mcp)
    agent = Agent(backend=backend, config=config, tools=tools, permissions=permissions, ui=ui)
    # 每条消息发出之前看一眼：可用的 Skill 和模型上次看到的不一样，就把新的目录附在这条消息前面（见 skills/catalog.py）。
    # 用一个 tools 里没有 Skill 的 Agent 当角色的对话，手里没有 skill 工具：不附（附了它也读不了，只会去调用一个不存在的工具）
    skill_note = notifier(skills)
    agent.notes.append(lambda history: skill_note(history) if agent.tools.get(SKILL_TOOL) is not None else "")
    # 每调用一次模型记一笔账，价格按那一刻、用你在设置里填的（见 ledger.py）。终端和网页都记
    ledger = Ledger(config.ledger_file)
    agent.on_call = ledger.recorder(lambda: settings.prices)
    # 自定义 Agent（见 agents/）：主助手每条消息之前也看一眼有哪些 Agent 可以交给（只有手里有 agent 工具时才附）
    agents = AgentStore(config.agents_dir)
    agent.notes.append(agent_notifier(agents, lambda: agent.tools))
    delegator = Delegator(config, agents, skills, mcp, ledger, lambda: settings.prices, client_factory)
    store = ConversationStore(config.conversations_dir)
    app = App(
        config=config,
        settings=settings,
        agent=agent,
        todos=todos,
        tasks=tasks,
        tools=tools,
        shell=shell,
        system_prompt=system_prompt,
        prompt_in_use=system_prompt,
        client_factory=client_factory,
        store=store,
        conversation=store.new(settings.provider, settings.model),
        media=MediaStore(config.media_dir),
        ledger=ledger,
        skills=skills,
        mcp=mcp,
        agents=agents,
        delegator=delegator,
    )
    # 第一条消息之前，手里的工具先按"这段对话会用的"摆好（带上 agent 工具）：信息栏列出来的才对得上。
    # 真正发第一条消息时还会再换一次，带上那一刻的 MCP 快照（见 fresh_tools）
    agent.use_tools(app.conversation_tools(None))
    agent.on_start = app.fresh_tools
    mcp.start()  # 每个启用的服务器在自己的后台线程里连接，这里不等
    return app


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
