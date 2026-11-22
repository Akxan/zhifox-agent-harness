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

为什么是混进来的基类，而不是 web.memory.delete(...) 这样另建一个对象：方法的名字照旧是 web.open_conversation(...)，
调用它们的地方（定时任务、聊天通道、别的 *_api.py、测试）一个都不用改；这些方法用到的又都是 WebApp 自己的状态
（self.app、self.ui、self._turn……），放在别的对象上也得一路 self.web. 回来。混进来的类只有方法：没有 __init__、
不存状态，状态都在下面的 __init__ 里定义，一眼看得全。新的一组功能照这样办：方法写进它自己的 *_api.py 的 XxxActions，
加进 WebApp 的基类；要新的状态就在 __init__ 里加上，写明是给谁用的。
（skills_api.py、browser_api.py 这些较早的接口是另一种写法：WebApp 上的一个对象，见 __init__。两种并存，都能用。）

留在这个文件里的：
  - 状态快照（publish_state）：页面要显示的一切。每一组功能的那一节由它们自己算（_media_state、_mcp_state、_rewind_state……）
  - 这段会话归谁（_claim、_held_elsewhere，见 claims.py）：聊天通道正在用的会话，网页上不能写
  - 存盘和离开当前会话（_save、_before_leaving、_left）
  - 对话的锁 self._turn：谁拿着它，谁就在进行一轮对话（或者正在改会动到对话的设置）。拿不到时用 _busy 回答；
    定时任务和聊天通道不拿它（见 scheduler.py、channels/hub.py）
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
from ..media import Entry
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
from .hooks_api import HooksAPI
from .mcp_api import McpActions
from .media_api import MediaActions
from .memory_api import MemoryActions
from .models_api import ModelActions
from .prompt_api import PromptActions
from .reveal import RevealActions
from .rewind_api import RewindActions
from .sandbox_api import SandboxAPI
from .scheduler import Scheduler
from .settings_api import SettingsActions
from .skills_api import SkillsAPI
from .tasks_api import TodoActions
from .ui import WebUI
from .voice_api import VoiceAPI


class WebApp(ChatActions, RewindActions, ConversationActions, MediaActions, MemoryActions, PromptActions, RevealActions, TodoActions, McpActions, ModelActions, SettingsActions):
    """把助手接到网页界面上，并保证同一时间只进行一轮对话。"""

    def __init__(self, app: App, ui: WebUI, token: str, service: str | None = None):
        self.app = app
        self.ui = ui
        self.token = token
        # 智狐是怎么开着的，设置里"后台运行"一节照着它说（见 page/scripts/service.js）：
        # "launchd" / "systemd" 是 harness service install 装的后台服务；"terminal" 是在终端里开的 harness --web；
        # None 是不清楚（在终端或者系统服务里开的 harness serve），那一节就不显示
        self.service = service
        self._turn = threading.Lock()  # 谁拿着这把锁，谁就在进行一轮对话（或者正在改设置）
        # 发状态快照时用的小锁："算出快照"和"发出去"要一口气做完。同步模型、改价格这些不拿对话锁的操作
        # 可能和一轮对话同时发快照，不这样的话，先算的旧快照可能反而后发出去，把新的盖掉。
        self._publishing = threading.Lock()
        self._unsaved = ""  # 当前会话上一次存盘失败的原因。空字符串表示内容都在磁盘上了
        self._discard_ok = ""  # 用户已经被提醒过一次"离开会丢内容"的那个操作；紧接着再做一次同样的操作就放行
        self._memories_shown: list[dict] = []  # 上一次发给页面的记忆列表（见 refresh_memories）
        self._live: tuple[object, tuple[Entry, ...]] = (None, ())  # 当前会话里的附件，和算它时的依据（见 _live_media）
        # Skill 一页的接口。"在文件管理器里显示"每次都按名字现找这个函数（测试会把它换掉）
        self.skills = SkillsAPI(self, lambda path: reveal.reveal_in_file_manager(path))
        self.agents = AgentsAPI(self, lambda path: reveal.reveal_in_file_manager(path))  # Agent 一页的接口，同样的道理
        self.browser = BrowserAPI(self, lambda path: reveal.reveal_in_file_manager(path))  # 设置里"浏览器"一节的接口
        self.computer = ComputerAPI(self)  # 设置里"操作整台电脑"一节的接口
        self.voice = VoiceAPI(self)  # 设置里"语音转文字"一节的接口（聊天里发来的语音，见 ../voice/）
        self.commands = CommandsAPI(self, lambda path: reveal.reveal_in_file_manager(path))  # "不用问的命令"窗口的接口
        self.hooks = HooksAPI(self, lambda path: reveal.reveal_in_file_manager(path))  # "扩展 → 钩子"一页的接口
        self.sandbox = SandboxAPI(self, lambda path: reveal.reveal_in_file_manager(path))  # 设置里"沙箱"一节的接口
        self.endpoints = EndpointsAPI(self, lambda path: reveal.reveal_in_file_manager(path))  # 设置里"自定义模型接口"一节的接口
        self.changes = ChangesAPI(self)  # "改了 N 个文件"那一行：预览、撤回、重做（见 changes_api.py）
        # 定时任务：到时间了自己跑一轮，或者弹出一条提醒。任务不拿 self._turn：它们和你的对话、和彼此同时进行，
        # 各用各的界面和 shell（见 scheduler.py）
        self.tasks = Scheduler(self)
        # 聊天通道（Telegram……）：聊天里的对话和你在网页上的对话同时进行，也不拿 self._turn。
        # 同一段会话不能两边同时写，所以每一轮开始前先认领它（见 claims.py）
        self.claims = Claims()
        self.channels = ChannelHub(app, self.claims, changed=self.publish_state, saved=self.conversation_saved_elsewhere)
        ui.on_stop(app.shell.kill_running)  # 点"停止"时，连正在运行的命令一起结束
        ui.on_stop(app.mcp.cancel_running)  # 也取消正在等的 MCP 工具调用：只是对话自己的，任务的各自管各自的（见 McpManager.call）
        # 还有正在做的浏览器动作：只是这段对话自己的（换一段对话就换一个，所以每次现找）
        ui.on_stop(lambda: app.chat_browser.stop())
        ui.on_stop(lambda: app.chat_desktop.stop())  # 正在操作电脑的那一步也停下（同样只是这段对话自己的）
        ui.on_stop(lambda: app.agent.hooks.stop())  # 正在跑的钩子也结束掉（只是对话自己的那一个，见 hooks/engine.py）
        app.browser.on_change = self.publish_state  # 浏览器开了、关了、忙起来了：页面顶上的提示跟着变
        app.desktop.on_change = self.publish_state  # 有运行开始、停止操作电脑：页面顶上"智狐正在控制这台电脑"跟着变
        app.agent.on_usage = self.publish_state  # 每调用完一次模型，就刷新一次用量和费用
        app.mcp.on_change = self.publish_state  # MCP 服务器连上了、断开了、工具变了，页面上跟着变
        if app.tasks.problem:
            ui.hint(app.tasks.problem, level="error")  # 任务文件读不出来：说一声，原来的文件留在哪儿
        self.publish_state()
        # 上次运行时一轮做到一半就退出了（存了原件、会话却没来得及存盘），留下没人用的原件：启动时清一遍。
        # 这时还没有开始接受请求，不会有哪一轮正在存新的原件
        self._collect_media(None)

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
            # 最后一轮能不能编辑、重新生成（见 _rewind_state）。页面上的按钮照着它画，能不能、为什么不能都由这里说了算
            rewind=self._rewind_state(),
        )

    # ---- 这段会话归谁（网页和聊天通道不能同时写，见 claims.py）------------------

    def _claim(self) -> str:
        """认领当前这段会话（见 claims.py）。它正在聊天通道里跑一轮的话返回原因，不认领。调用的人拿着 self._turn。"""
        conversation = self.app.conversation
        holder = self.claims.take(conversation.id, "web")
        if holder is not None:
            return t("这段会话正在 {platform} 上进行。等那边这一轮结束再发。", platform=self.channels.holder(conversation.id) or holder)
        if conversation.saved and not self._unsaved:
            # 聊天那边可能刚在这段会话里聊过一轮（存到了磁盘上，这里的还是旧的）：先读回来，不然这一轮存盘时会把那一轮盖掉
            try:
                newer = self.app.store.load(conversation.id).updated_at > conversation.updated_at
            except ConversationError:
                newer = False
            if newer:
                self._reload_current()
        return ""

    def _reload_current(self) -> None:
        """从磁盘把当前这段会话重新读一遍，页面跟着重画。调用的人拿着 self._turn。"""
        try:
            conversation = self.app.open_conversation(self.app.conversation.id)
        except ConversationError:
            return
        self.ui.load_events(conversation.events)
        self.publish_state()

    def conversation_saved_elsewhere(self, conversation_id: str) -> None:
        """聊天通道刚把一段会话存了盘。网页正开着它、手上又没事的话，跟着刷新：页面上看到的就是最新的，
        你接着在网页上聊，也不会用一份旧的历史把聊天里的那一轮盖掉。"""
        if conversation_id != self.app.conversation.id or self._unsaved or not self._turn.acquire(blocking=False):
            return
        try:
            if conversation_id == self.app.conversation.id:
                self._reload_current()
        finally:
            self._turn.release()

    def _held_elsewhere(self, conversation_id: str) -> str:
        """这段会话正在聊天通道里跑一轮的话，返回不能打开、改名、删除的原因。"""
        platform = self.channels.holder(conversation_id)
        return t("这段会话正在 {platform} 上进行。等那边这一轮结束再试。", platform=platform) if platform else ""

    # ---- 存盘、离开当前会话 ----------------------------------------------------

    def _save(self) -> None:
        """把当前会话存盘。存不上就在页面上说明原因；内容还在内存里，下一轮结束时会再存一次。"""
        problem = self._try_save()
        if problem:
            self.ui.error(t("保存会话失败：{error}", error=problem))

    def _try_save(self) -> str:
        """存盘。返回失败的原因（成功返回空字符串），并记下当前会话是不是还有没存上的内容。"""
        try:
            self.app.save_conversation(self.ui.export_events())
        except Exception as exc:  # noqa: BLE001 —— 什么错都要接住：这是在一轮对话收尾时调用的，漏出去的话页面永远等不到"结束"
            self._unsaved = str(exc) or type(exc).__name__
        else:
            self._unsaved = ""
            self._discard_ok = ""
        return self._unsaved

    def _before_leaving(self, action: str) -> str:
        """要离开当前会话了（新对话、打开别的会话、换到另一家）。返回不让走的原因；空字符串表示可以走。

        当前会话要是有没存上的内容（上一次存盘失败了），一走这些内容就没了，所以先再存一次。
        还是存不上的话拦一下、把话说清楚；用户紧接着再做一次同样的操作，就当他确认要放弃。
        """
        if not self._unsaved or not self._try_save():
            return ""
        if self._discard_ok == action:
            return ""
        self._discard_ok = action
        return t("当前这段会话还没能保存（{error}），现在离开会丢掉它的内容。可以先解决保存的问题；确实要放弃的话，再点一次。", error=self._unsaved)

    def _left(self) -> None:
        """已经离开了原来的会话：关于它的"没存上"和"确认放弃"都不用再记着。"""
        self._unsaved = self._discard_ok = ""

    def _show_warnings(self) -> None:
        """把助手攒下的提醒（比如"没能记住这次选的模型"）显示出来。只显示，不存进会话。"""
        for text in self.app.take_warnings():
            self.ui.hint(text, level="error")

    # ---- 对话的锁 ------------------------------------------------------------

    BUSY = k("请先等当前这一轮对话结束，或者点“停止”")

    def _busy(self, otherwise: str | None = None) -> str:
        """对话的锁被占着（你的这一轮还没结束，或者正在改设置）时，告诉用户为什么。

        定时任务不拿这把锁，所以任务在跑的时候不会走到这里：你照常发消息、换模型、打开会话。
        """
        return otherwise or t(self.BUSY)

    def stop(self) -> None:
        """对话里的"停止"：只停你的这一轮（连同它正在运行的命令）。

        定时任务的运行各有各的界面和 shell，这里碰不到它们；停任务走 /api/task/stop。
        """
        self.ui.request_stop()

    def wait_idle(self, timeout: float) -> bool:
        """等正在进行的一轮对话结束（包括它收尾时的存盘）。等到了返回 True，超时返回 False。"""
        if not self._turn.acquire(timeout=timeout):
            return False
        self._turn.release()
        return True

    # ---- 工具 ----------------------------------------------------------------

    def refresh_tools(self) -> bool:
        """浏览器开、关了：还没开始的这段对话马上换上新的工具。返回 True 表示这段对话已经开始了，工具不变。"""
        if not self._turn.acquire(blocking=False):
            return True  # 正在进行一轮：这段对话当然已经开始了
        try:
            started = bool(self.app.agent.backend.export())
            if not started:
                self.app.refresh_tools()
            return started
        finally:
            self._turn.release()
