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

from . import fallback, i18n
from .agent import Agent
from .agents import AgentStore, CustomAgent
from .agents.runner import Delegator
from .app_chat import AppChats, Chat
from .app_conversations import AppConversations
from .app_models import FALLBACK, AppModels
from .app_prices import AppPrices, merged_prices
from .app_runs import AppRuns, UnattendedRun
from .app_tools import AppTools
from .attachments import Attachment
from .browser.manager import BrowserManager
from .browser.session import BrowserSession
from .commands.allowlist import CommandList
from .config import Config
from .conversations import Conversation, ConversationStore
from .desktop.manager import DesktopManager
from .desktop.session import DesktopSession
from .endpoints import EndpointStore
from .fetch.client import Fetcher
from .hooks import HookConfig, Hooks
from .i18n import t
from .ledger import Ledger
from .mcp import McpConfig, McpManager
from .media import MediaStore
from .permissions import Permissions
from .prompt import build_system_prompt
from .providers import ClientFactory, ProviderError, find, make_client, use_custom
from .redact import Redactor
from .sandbox import Layout, Sandbox
from .settings import Settings
from .skills import SkillStore
from .tasks import TaskStore
from .tools import ToolRegistry, build_registry
from .tools.files import FileEditor
from .tools.shell import Shell
from .tools.todo import TodoStore
from .undo.changes import FileChanges

__all__ = ["FALLBACK", "App", "Chat", "UnattendedRun", "build_app", "merged_prices", "retry_turn", "run_turn", "sandbox_layout"]


@dataclass
class App(AppChats, AppConversations, AppTools, AppModels, AppPrices, AppRuns):
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
    # 读网页时真正去取的那一个（见 fetch/client.py）。程序里从来不换；测试换成一个能连本地假网站的（不在设置里，模型也碰不到）
    fetcher: Fetcher = field(default_factory=Fetcher)
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
        那一段自己的东西一样都不动，后台的那一段照常跑。信息栏、MCP 一页照着 McpManager.active 画，跟着换。
        设置里"现在用的模型"也换成它用的那个（和打开一段会话一样）：页面顶上的模型按钮、新对话、定时任务都照着它。"""
        with self._viewing:
            self.chat = chat
            self.mcp.active = chat.mcp
        self._remember_choice(chat.conversation.provider, chat.conversation.model)

    def persona_info(self) -> dict | None:
        """当前这段对话的角色（页面顶上显示它的名字）：名字、颜色；它现在用不了的话 missing 为真。普通的对话是 None。"""
        name = self.conversation.agent
        if not name:
            return None
        persona = self.persona if self.persona is not None and self.persona.name == name else None
        return {"name": name, "color": persona.spec.color if persona else "", "missing": persona is None}

    def take_warnings(self) -> list[str]:
        """取走攒下的提醒（取走就清空）。"""
        taken, self.warnings = self.warnings, []
        return taken

    def close(self) -> None:
        """程序退出前的收尾：关掉启动过的 MCP 服务器进程和浏览器，还按着的鼠标键松开。"""
        self.mcp.shutdown()
        self.browser.close()
        for chat in self.chats():
            chat.desktop.close()


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
