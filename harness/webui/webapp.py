"""WebApp：把助手接到网页界面上。server.py 的 Handler 收到请求、查完口令和参数，交给这里去做。

WebApp 由几块拼成。这个文件里是几组功能都要用的部分；每一组功能的方法写在它自己的文件里，以基类的形式混进来：

  ChatActions          chat_api.py           发消息：一轮对话在后台跑、收尾
  RewindActions        rewind_api.py         倒回最后一轮（编辑最后一条消息、重新生成）
  ConversationActions  conversations_api.py  新对话、打开、改名、删除、显示一段会话
  MediaActions         media_api.py          多媒体库
  MemoryActions        memory_api.py         记忆
  PromptActions        prompt_api.py         提示词
  RevealActions        reveal.py             在文件管理器里显示助手的数据文件
  TodoActions          tasks_api.py          待办
  McpActions           mcp_api.py            MCP 服务器
  ModelActions         models_api.py         换模型、密钥、同步模型列表、价格
  SettingsActions      settings_api.py       语言、问不问、遮密钥、预算、花费明细、导出之前的信息
  SessionActions       sessions_api.py       几段对话同时进行：新建一段、换着看、停下、一轮收尾、退出

为什么是混进来的基类，而不是 web.memory.delete(...) 这样另建一个对象：方法的名字照旧是 web.open_conversation(...)，
调用它们的地方（定时任务、聊天通道、别的 *_api.py、测试）一个都不用改；这些方法用到的又都是 WebApp 自己的状态
（self.app、self.ui、self.sessions……），放在别的对象上也得一路 self.web. 回来。混进来的类只有方法：没有 __init__、
不存状态，状态都在下面的 __init__ 里定义，一眼看得全。新的一组功能照这样办：方法写进它自己的 *_api.py 的 XxxActions，
加进 WebApp 的基类；要新的状态就在 __init__ 里加上，写明是给谁用的。
每个 XxxActions 都继承 WebAppPart（webapp_types.py）：单看那个类，类型检查不知道 self 上有 app、sessions、_save……，
WebAppPart 把这些写给它看（运行时是空的）。用到了别的文件里的状态或方法，就在那里补一行，做法见那个文件开头。
（skills_api.py、browser_api.py 这些较早的接口是另一种写法：WebApp 上的一个对象，见 __init__。两种并存，都能用。）

留在这个文件里的：
  - 状态快照（publish_state）：页面要显示的一切。每一组功能的那一节由它们自己算（_media_state、_mcp_state、_rewind_state……）
  - 这段会话归谁（_claim、_held_elsewhere，见 claims.py）：聊天通道正在用的会话，网页上不能写
  - 存盘和离开一段会话（_save、_before_leaving、_left）
  - 锁（规矩见 sessions.py 开头）。网页上可以几段对话同时进行，每段一个 Session，各有一把锁（Session.lock）：
    在这一段里进行一轮、改它（换模型、改名、撤回文件……）拿它自己的；拿不到时用 _busy 回答。
    self._turn 不再是"那一把对话锁"，而是 AllIdle：没有任何一段在跑才拿得到，全局的操作（改记忆、改 Skill……）拿它。
    self._switch：改动"哪几段活着、正看着哪一段"的操作一个一个来。定时任务和聊天通道这几把都不拿（见 scheduler.py、channels/hub.py）
  - self.ui 是你正看着的那一段的事件日志（换着看的时候跟着换）；后台那几段的在各自的 Session.ui 里
"""

from __future__ import annotations

import threading

from .. import machine, memories
from .. import prompt as prompts
from ..app import App
from ..channels.hub import ChannelHub
from ..claims import Claims
from ..conversations import ConversationError
from ..i18n import k, t
from ..pricing import estimate
from ..providers import every
from . import fallback_api, reveal
from .agents_api import AgentsAPI
from .browser_api import BrowserAPI
from .capabilities_api import page_state as capabilities_state
from .changes_api import ChangesAPI
from .chat_api import ChatActions
from .commands_api import CommandsAPI
from .computer_api import ComputerAPI
from .conversations_api import ConversationActions
from .endpoints_api import EndpointsAPI
from .fork_api import ForkAPI
from .hooks_api import HooksAPI
from .mcp_api import McpActions
from .media_api import MediaActions
from .memory_api import MemoryActions
from .models_api import ModelActions
from .prompt_api import PromptActions
from .restore_api import RestoreAPI
from .reveal import RevealActions
from .rewind_api import RewindActions
from .sandbox_api import SandboxAPI
from .scheduler import Scheduler
from .sessions import AllIdle, Session, Sessions
from .sessions_api import SessionActions
from .settings_api import SettingsActions
from .skills_api import SkillsAPI
from .tasks_api import TodoActions
from .ui import WebUI
from .updates_api import UpdatesAPI
from .voice_api import VoiceAPI


class WebApp(
    ChatActions, RewindActions, ConversationActions, SessionActions, MediaActions, MemoryActions, PromptActions, RevealActions, TodoActions,
    McpActions, ModelActions, SettingsActions,
):
    """把助手接到网页界面上：几段对话可以同时进行（每段一个 Session，见 sessions.py），同一段同一时间只进行一轮。"""

    def __init__(self, app: App, ui: WebUI, token: str, service: str | None = None):
        self.app = app
        self.token = token
        # 智狐是怎么开着的，设置里"后台运行"一节照着它说（见 page/scripts/service.js）：
        # "launchd" / "systemd" 是 harness service install 装的后台服务；"terminal" 是在终端里开的 harness --web；
        # None 是不清楚（在终端或者系统服务里开的 harness serve），那一节就不显示
        self.service = service
        # 你的对话：一开始只有 build_app 建的那一段（ui 是它的事件日志），你正看着它。后开的几段的事件编号和它从同一个数往下排
        first = Session(app.chat, ui)
        self.sessions = Sessions(first)
        self._ids = ui.ids
        app.live = lambda: [session.chat for session in self.sessions.all()]  # App 也要知道有哪几段活着（删接口、换密钥、退出时）
        # 改动"哪几段活着、正看着哪一段"的操作一个一个来（打开、新对话、删除、换模型……）。用 sessions.take_switch 拿：
        # 等一小会儿（上一个还在收尾），还拿不到回答"忙"
        self._switch = threading.RLock()
        # 以前的对话锁：现在是"没有任何一段在跑"（见 sessions.py 的 AllIdle）。改记忆、改 Skill 这些全局的操作拿它
        self._turn = AllIdle(self._switch, self.sessions)
        # 发状态快照时用的小锁："算出快照"和"发出去"要一口气做完。同步模型、改价格这些不拿对话锁的操作
        # 可能和一轮对话同时发快照，不这样的话，先算的旧快照可能反而后发出去，把新的盖掉。
        # 换"正看着哪一段"也拿着它（见 _show）：快照里不会一半是原来那一段的、一半是新的这一段的
        self._publishing = threading.Lock()
        self._memories_shown: list[dict] = []  # 上一次发给页面的记忆列表（见 refresh_memories）
        # Skill 一页的接口。"在文件管理器里显示"每次都按名字现找这个函数（测试会把它换掉）
        self.skills = SkillsAPI(self, lambda path: reveal.reveal_in_file_manager(path))
        self.agents = AgentsAPI(self, lambda path: reveal.reveal_in_file_manager(path))  # Agent 一页的接口，同样的道理
        self.browser = BrowserAPI(self, lambda path: reveal.reveal_in_file_manager(path))  # 设置里"浏览器"一节的接口
        self.computer = ComputerAPI(self)  # 设置里"操作整台电脑"一节的接口
        self.voice = VoiceAPI(self)  # 设置里"语音转文字"一节的接口（聊天里发来的语音，见 ../voice/）
        # 设置里"检查新版本"一节的接口（默认关着，见 ../updates.py）。后台线程由 lifecycle.py 开和关
        self.updates = UpdatesAPI(self)
        self.commands = CommandsAPI(self, lambda path: reveal.reveal_in_file_manager(path))  # "不用问的命令"窗口的接口
        self.hooks = HooksAPI(self, lambda path: reveal.reveal_in_file_manager(path))  # "扩展 → 钩子"一页的接口
        self.sandbox = SandboxAPI(self, lambda path: reveal.reveal_in_file_manager(path))  # 设置里"沙箱"一节的接口
        self.endpoints = EndpointsAPI(self, lambda path: reveal.reveal_in_file_manager(path))  # 设置里"自定义模型接口"一节的接口
        self.changes = ChangesAPI(self)  # "改了 N 个文件"那一行：预览、撤回、重做（见 changes_api.py）
        self.fork = ForkAPI(self)  # 从某一轮分叉成一段新对话（见 fork_api.py）
        self.restore = RestoreAPI(self, lambda path: reveal.reveal_in_file_manager(path))  # 设置里"从备份恢复…"的接口
        # 定时任务：到时间了自己跑一轮，或者弹出一条提醒。任务不拿 self._turn：它们和你的对话、和彼此同时进行，
        # 各用各的界面和 shell（见 scheduler.py）
        self.tasks = Scheduler(self)
        # 聊天通道（Telegram……）：聊天里的对话和你在网页上的对话同时进行，也不拿 self._turn。
        # 同一段会话不能两边同时写，所以每一轮开始前先认领它（见 claims.py）
        self.claims = Claims()
        self.channels = ChannelHub(app, self.claims, changed=self.publish_state, saved=self.conversation_saved_elsewhere)
        # 点"停止"时，连这一段正在运行的命令、正在等的 MCP 调用、浏览器和电脑的动作、钩子一起停下；用量变了刷新状态（见 _arm）
        self._arm(first)
        app.browser.on_change = self.publish_state  # 浏览器开了、关了、忙起来了：页面顶上的提示跟着变
        app.desktop.on_change = self.publish_state  # 有运行开始、停止操作电脑：页面顶上"智狐正在控制这台电脑"跟着变
        app.mcp.on_change = self.publish_state  # MCP 服务器连上了、断开了、工具变了，页面上跟着变
        if app.tasks.problem:
            ui.hint(app.tasks.problem, level="error")  # 任务文件读不出来：说一声，原来的文件留在哪儿
        self.publish_state()
        # 上次运行时一轮做到一半就退出了（存了原件、会话却没来得及存盘），留下没人用的原件：启动时清一遍。
        # 这时还没有开始接受请求，不会有哪一轮正在存新的原件
        self._collect_media(None)

    @property
    def ui(self) -> WebUI:
        """你正看着的那一段的事件日志（换着看的时候跟着换，见 sessions.py）。"""
        return self.sessions.viewed.ui

    # ---- 状态快照 ------------------------------------------------------------

    def publish_state(self) -> None:
        """把页面要显示的状态（当前模型、各家的情况、待办、用量、费用）作为一个事件发给浏览器。

        注意这里永远不包含 API key 本身，只有"有没有设置"和最后 4 位。
        """
        with self._publishing:
            self._publish_state()

    def _publish_state(self) -> None:
        agent, settings, config = self.app.agent, self.app.settings, self.app.config
        usage = agent.usage
        cost, unpriced = estimate(agent.usage_by_model, self.app.prices())
        price = self.app.price_of(settings.provider, settings.model)
        offpeak = self.app.price_of(settings.provider, settings.model, offpeak=True)
        self._memories_shown = memories.list_files(config.memory_dir)
        tasks, reminders = self.tasks.snapshot()
        skills, agents = self.skills.summaries(), self.agents.summaries()
        self.ui.publish(
            "state",
            provider=settings.provider,
            model=settings.model,
            workspace=str(config.workspace),
            conversation=self.app.conversation.id,
            title=self.app.conversation.title,
            conversations=self.app.conversation_summaries(),
            # 几段对话同时进行（见 sessions.py）：会话编号 → running 在跑 / confirm 等你确认 / error 在后台出了错 / done 在后台跑完了、你还没看。
            # 正看着的那一段也在里面（页面不给它画标记）。parallel：最多同时几段、现在几段在跑（页面照着它决定能不能再发）
            live=self.sessions.live(),
            parallel={"max": self._parallel_limit(), "running": self.sessions.running()},
            providers=[self._provider_state(provider) for provider in every()],
            # 自定义模型接口（设置里"自定义模型接口"一节，见 endpoints_api.py）：密钥只有最后 4 位
            endpoints=self.endpoints.state(),
            price=price,
            price_offpeak=offpeak if offpeak != price else None,  # 分时段计价的模型（DeepSeek）的闲时价；不分时段的是空的
            fallbacks=fallback_api.state(self.app),  # 每一家的备用模型（按顺序，见 fallback.py）；没设的不出现
            todos=[
                {"id": item["id"], "title": item["title"], "due": item["due"]}
                for item in self.app.todos.items("pending")
            ],
            # 记忆文件夹里有哪些文件（只有名字、大小和修改时间，内容要看时另外读）。每次发状态都重新看一遍文件夹，
            # 所以模型在这一轮里记下或删掉的，下一次刷新状态时页面上就跟着变了，页面不用自己去轮询
            memories=self._memories_shown,
            # 多媒体库里一共有多少个附件，和一个"版本"。版本变了，开着的多媒体一栏就重新取一次列表；页面不用轮询
            media=self._media_state(),
            # 装好的 Skill（名字、说明、启用没有、有没有问题）。和记忆一样每次都重新看一遍文件夹
            skills=skills,
            # 自定义 Agent（名字、说明、模型、颜色、几样工具、有没有问题），和 Skill 一样每次都重新看一遍文件夹
            agents=agents,
            # 当前这段对话是用哪个 Agent 开始的（顶上显示它的名字）；普通的对话是 null
            persona=self.app.persona_info(),
            prompt_custom=prompts.load_template(config) != prompts.DEFAULT_TEMPLATE,  # 提示词改过没有（设置里显示）
            usage={
                "requests": usage.requests,
                "input": usage.input_tokens,
                "cache_read": usage.cache_read_tokens,
                "cache_write": usage.cache_write_tokens,
                "output": usage.output_tokens,
                "searches": usage.searches,
                "context": usage.context_tokens,
            },
            cost=round(cost, 4),
            unpriced=unpriced,
            spend=self.app.spending(),  # 今天、本月花了多少，预算超了没有（这些记在账本里，重启也不会清零）
            # 信息栏里"智狐能做的事"：每个功能在这段对话里用不用得了、为什么、问不问你、在哪儿执行、关着的怎么打开
            # （由 capabilities.py 算，格式见那里的开头）。页面只照着画
            capabilities=capabilities_state(self.app, skills, agents),
            mcp=self._mcp_state(),
            approval=agent.permissions.mode,  # 要不要每次都问：ask / readonly / edits / all
            commands=self.commands.state(),  # 你自己加的"不用问的命令"（见 commands_api.py）
            hooks=self.hooks.summary(),  # 你的钩子：有几个、文件有没有问题（完整的列表打开那一页时再要，见 hooks_api.py）
            sandbox=self.sandbox.state(),  # 命令的沙箱：哪一档、额外允许的路径、这台电脑上能不能用（见 sandbox_api.py）
            redact_secrets=settings.redact_secrets,  # 工具的结果先遮一遍密钥再交给模型（见 redact.py），设置里能关
            language=settings.language,  # 界面语言：zh / en / es。和页面现在用的不一样时，页面会在原地换过去
            # 定时任务：每个任务的设置、最近的运行、正在运行的那一次的进度（格式见 scheduler.py 开头的说明），
            # 以及已经弹出、你还没关掉的提醒
            tasks=tasks,
            reminders=reminders,
            # 聊天通道：每个平台的设置和连接情况、各个聊天在做什么、聊天里等你批准的操作（格式见 channels/hub.py 的 snapshot）。
            # 机器人的 token 不在里面，只有最后 4 位
            channels=self.channels.snapshot(),
            # 这台电脑能不能打开文件管理器。服务器上不能：页面上"在访达中显示"之后改成告诉你文件在哪儿
            file_manager=reveal.has_file_manager(),
            platform=machine.system(),  # 这台电脑是 mac / windows / linux：页面照着它说"访达""文件资源管理器"还是"文件管理器"（见 machine.py）
            service=self.service,  # 智狐是作为后台服务开着的，还是跟着终端（见 __init__ 里的说明）
            # 浏览器：开没开、在不在运行、正在操作没有、找到的 Chrome、配置文件夹（见 browser_api.py）
            browser=self.browser.state(),
            # 操作整台电脑：开没开、这台电脑上能不能用、权限、谁正在操作、截图留几天（见 computer_api.py）
            computer=self.computer.state(),
            # 语音转文字：设置、实际会用哪一种、有没有 OpenAI 的密钥（只说有没有）、每个模型每分钟多少钱（见 voice_api.py）
            voice=self.voice.state(),
            # 检查新版本：开没开、上一次什么时候问的、没问成的原因、有新版本时是哪一版和怎么升级（见 updates_api.py）
            updates=self.updates.state(),
            # 最后一轮能不能编辑、重新生成（见 _rewind_state）。页面上的按钮照着它画，能不能、为什么不能都由这里说了算
            rewind=self._rewind_state(),
            # 每一轮上的"分叉"能不能点、这段会话是从哪儿分叉来的（见 fork_api.py）
            fork=self.fork.state(),
        )

    # ---- 这段会话归谁（网页和聊天通道不能同时写，见 claims.py）------------------

    def _claim(self, session: Session | None = None) -> str:
        """认领一段会话（不给是你正看着的那一段，见 claims.py）。它正在聊天通道里跑一轮的话返回原因，不认领。调用的人拿着这一段的锁。"""
        session = session or self.sessions.viewed
        conversation = session.chat.conversation
        holder = self.claims.take(conversation.id, "web")
        if holder is not None:
            return t("这段会话正在 {platform} 上进行。等那边这一轮结束再发。", platform=self.channels.holder(conversation.id) or holder)
        if conversation.saved and not session.unsaved:
            # 聊天那边可能刚在这段会话里聊过一轮（存到了磁盘上，这里的还是旧的）：先读回来，不然这一轮存盘时会把那一轮盖掉
            try:
                newer = self.app.store.load(conversation.id).updated_at > conversation.updated_at
            except ConversationError:
                newer = False
            if newer:
                self._reload_current(session)
        return ""

    def _reload_current(self, session: Session | None = None) -> None:
        """从磁盘把一段会话重新读一遍（不给是你正看着的那一段），页面跟着重画。调用的人拿着这一段的锁。"""
        session = session or self.sessions.viewed
        try:
            conversation = self.app.open_conversation(session.id, chat=session.chat)
        except ConversationError:
            return
        session.ui.load_events(conversation.events)
        self.publish_state()

    def conversation_saved_elsewhere(self, conversation_id: str) -> None:
        """聊天通道刚把一段会话存了盘。网页正开着它、手上又没事的话，跟着刷新：页面上看到的就是最新的，
        你接着在网页上聊，也不会用一份旧的历史把聊天里的那一轮盖掉。"""
        session = self.sessions.viewed
        if conversation_id != session.id or session.unsaved or not self._switch.acquire(blocking=False):
            return
        try:
            with self._holding(session) as idle:
                if idle and session is self.sessions.viewed and conversation_id == session.id:
                    self._reload_current(session)
        finally:
            self._switch.release()

    def _held_elsewhere(self, conversation_id: str) -> str:
        """这段会话正在聊天通道里跑一轮的话，返回不能打开、改名、删除的原因。"""
        platform = self.channels.holder(conversation_id)
        return t("这段会话正在 {platform} 上进行。等那边这一轮结束再试。", platform=platform) if platform else ""

    # ---- 存盘、离开一段会话 ----------------------------------------------------

    def _save(self, session: Session | None = None) -> None:
        """把一段会话存盘（不给是你正看着的那一段）。存不上就在它的页面上说明原因；内容还在内存里，下一轮结束时会再存一次。"""
        session = session or self.sessions.viewed
        problem = self._try_save(session)
        if problem:
            session.ui.error(t("保存会话失败：{error}", error=problem))

    def _try_save(self, session: Session | None = None) -> str:
        """存盘。返回失败的原因（成功返回空字符串），并记下这一段是不是还有没存上的内容。"""
        session = session or self.sessions.viewed
        try:
            self.app.save_conversation(session.ui.export_events(), chat=session.chat)
        except Exception as exc:  # noqa: BLE001 —— 什么错都要接住：这是在一轮对话收尾时调用的，漏出去的话页面永远等不到"结束"
            session.unsaved = str(exc) or type(exc).__name__
        else:
            session.unsaved = ""
            session.discard_ok = ""
        return session.unsaved

    def _before_leaving(self, session: Session, action: str) -> str:
        """要离开 session 里的这段会话了（新对话、打开别的会话、换到另一家）。返回不让走的原因；空字符串表示可以走。

        它要是有没存上的内容（上一次存盘失败了），一走这些内容就没了，所以先再存一次。
        还是存不上的话拦一下、把话说清楚；用户紧接着再做一次同样的操作，就当他确认要放弃。
        """
        if not session.unsaved or not self._try_save(session):
            return ""
        if session.discard_ok == action:
            return ""
        session.discard_ok = action
        return t("当前这段会话还没能保存（{error}），现在离开会丢掉它的内容。可以先解决保存的问题；确实要放弃的话，再点一次。", error=session.unsaved)

    def _left(self, session: Session) -> None:
        """已经离开了原来的会话：关于它的"没存上"和"确认放弃"都不用再记着。"""
        session.unsaved = session.discard_ok = ""

    def _show_warnings(self) -> None:
        """把助手攒下的提醒（比如"没能记住这次选的模型"）显示出来。只显示，不存进会话。"""
        for text in self.app.take_warnings():
            self.ui.hint(text, level="error")

    # ---- 忙 ------------------------------------------------------------------

    BUSY = k("请先等当前这一轮对话结束，或者点“停止”")

    def _busy(self, otherwise: str | None = None) -> str:
        """要用的锁被占着（这一段的这一轮还没结束，或者正在改设置）时，告诉用户为什么。

        定时任务不拿这些锁，所以任务在跑的时候不会走到这里：你照常发消息、换模型、打开会话。
        """
        return otherwise or t(self.BUSY)

    # ---- 工具 ----------------------------------------------------------------

    def refresh_tools(self) -> bool:
        """浏览器开、关了：你正看着的这段对话还没开始的话，马上换上新的工具。返回 True 表示这段对话已经开始了，工具不变。
        后台那几段都在跑，早就开始了，不动。"""
        session = self.sessions.viewed
        with self._holding(session) as idle:
            if not idle:
                return True  # 正在进行一轮：这段对话当然已经开始了
            started = bool(session.chat.agent.backend.export())
            if not started:
                self.app.refresh_tools(session.chat)
            return started
