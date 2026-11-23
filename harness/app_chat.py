"""一段对话（Chat）：它装着什么，怎么建起来，换一段会话时哪些东西跟着换新的，不用了怎么收掉（App 的一组方法，见 app.py 开头）。

  - Chat：一段对话在助手这一侧用的全部东西（每一样是什么，见下面的类）。终端只有一个，网页上同时进行的几段各有一个；
  - new_chat：准备一段新对话（适配层、自己的 shell、浏览器和电脑的 session、MCP 调用记在谁名下），
    _wire 给它的助手接上大家共用的那几样（Skill 和 Agent 的目录、记账、遮密钥、备用模型、运行记录、钩子），
    _fresh_start 在第一条消息发出之前换上这段对话的工具、命令回到工作目录；
  - _fresh_browser、_fresh_desktop、_fresh_fetch：换到另一段会话上时（新对话、打开、分叉，见 app_conversations.py），
    浏览器的标签页、"这段对话里截图不再问"、读网页允许过的网站都只属于原来那段，换上新的；
  - close_chat：一段对话不再用了（网页上在后台跑完、存好了盘），关掉它的标签页、让出屏幕。

为什么单独一个文件："这一段的 xxx 在哪儿、是哪来的"是读 App 时最常问的问题，Chat 装着什么和它是怎么建的放在一起看。
它手里的工具怎么摆在 app_tools.py；它接着的那段会话（内容、标题、存盘）在 app_conversations.py。
定时任务和聊天通道的一次运行不是 Chat，另在 app_runs.py。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from .agent import Agent
from .agents import CustomAgent
from .agents.catalog import notifier as agent_notifier
from .app_models import FALLBACK
from .app_types import AppPart
from .browser.session import BrowserSession
from .conversations import Conversation
from .desktop import manager as desktop_labels
from .desktop.session import DesktopSession
from .fetch.session import FetchSession
from .hooks import HookRun
from .mcp import Snapshot
from .prompt import build_system_prompt
from .providers import PROVIDERS, find, make_backend
from .sandbox import Sandbox
from .skills.catalog import notifier
from .tools import ToolRegistry
from .tools.shell import Shell, make_shell_tool
from .tools.skill import TOOL_NAME as SKILL_TOOL


@dataclass
class Chat:
    """你的一段对话在助手这一侧用的全部东西（见文件开头）。网页上同时进行的几段，各有一个（见 webui/sessions.py）。

    凡是"会被停下"的都各用各的，和定时任务的每次运行一样（见 app_runs.py 的 _side_run）：点这一段的"停止"，只结束它自己的命令、
    它自己名下的 MCP 调用、它自己的浏览器和电脑的动作、它自己正在跑的钩子。共用的只有磁盘上的东西（各有各的锁）、
    权限（"要不要先问你"那一档是全局的一个，见 App.permissions）、遮密钥和备用模型的设置、花费账本。
    """

    agent: Agent  # 这段对话的助手：适配层（历史）、用量、运行记录、钩子都在它上面
    conversation: Conversation  # 这段会话
    shell: Shell  # 它自己的 shell
    tools: ToolRegistry  # 它自己的那一套基本工具：和 App.tools 一样，只是 run_command 用的是它自己的 shell
    browser: BrowserSession  # 它用的浏览器：它自己的标签页、这段对话里允许过的网站。换一段对话就换一个
    desktop: DesktopSession  # 它的电脑操作："这段对话里截图不再问""点击、打字不再问"记在它上面。换一段对话就换一个
    # 它读网页（见 fetch/）的那一份：这段对话里允许过的网站、这条消息里读了几次。换一段对话就换一个
    fetch: FetchSession = field(default_factory=FetchSession)
    owner: object | None = None  # 它的 MCP 调用记在谁名下（见 McpManager.call）：一开始的那一段是 None，后来开的每段各一个
    # 这段对话的适配层用的是哪一版提示词。和 App.system_prompt 不一样，说明用户改过了：
    # 这一段照旧用它开始时的那版，下一段新对话（或者打开另一段会话）时换上新的
    prompt_in_use: str = ""
    # 这段对话是用哪个 Agent 当角色开始的（见 agents/ 开头的说明）；普通的对话是 None。
    # 它在这段对话开始时读好，之后不再变：你中途改了那个 Agent 的文件，从下一段用它开始的对话起才算数
    persona: CustomAgent | None = None
    # 这段对话开始时拍下的 MCP 快照（见 app_tools.py 的 fresh_tools）。正看着它的时候，McpManager.active 就是它（信息栏和 MCP 一页按它画）
    mcp: Snapshot = field(default_factory=Snapshot)


class AppChats(AppPart):
    # ---- 建一段对话 -------------------------------------------------------------

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
        if provider is None:  # 设置里选的自定义接口不在了：用内置的默认那一家（和 app_conversations.py 的 new_conversation 一样）
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
        # 真正发第一条消息时还会再换一次，带上那一刻的 MCP 快照（见 app_tools.py 的 fresh_tools）
        agent.use_tools(self.conversation_tools(None, chat=chat))
        agent.on_start = lambda: self._fresh_start(chat)

    def _fresh_start(self, chat: Chat) -> None:
        """一段新对话的第一条消息发出之前（agent.on_start）：换上这段对话的工具（见 app_tools.py 的 fresh_tools），命令从工作目录开始。

        目录在这里回到工作目录，而不是在"新对话"的每一个入口（new_conversation、换到另一家的 select_model、终端的 /new……）
        各写一遍：凡是历史是空的、要发第一条消息的，都会走到这里，新加的入口也不会漏（见 tools/cwd.py）。"""
        self.fresh_tools(chat)
        chat.shell.current.reset()

    # ---- 换到另一段会话上：只属于原来那段的换新的；不用了收掉 ---------------------------

    def _fresh_desktop(self, chat: Chat) -> None:
        """换了一段对话："这段对话里截图不再问"不再算数，还按着的鼠标键松开，屏幕让出来。新的这一段用一个新的。"""
        old, chat.desktop = chat.desktop, self.desktop.session(desktop_labels.CHAT, attended=True)
        old.close()

    def _fresh_fetch(self, chat: Chat) -> None:
        """换了一段对话：读网页时"允许过的网站"不再算数，新的这一段从头再问。"""
        chat.fetch = FetchSession()

    def _fresh_browser(self, chat: Chat) -> None:
        """换了一段对话：上一段的浏览器标签页关掉，"允许过的网站"也不再算数。新的这一段用一个新的。"""
        old, chat.browser = chat.browser, self.browser.session("chat")
        threading.Thread(target=old.close, daemon=True).start()  # 关标签页要和 Chrome 来回几次：不让换会话等它

    def close_chat(self, chat: Chat) -> None:
        """一段对话不再用了（网页上一段在后台跑完、存好了盘，见 webui/sessions.py）：关掉它的浏览器标签页，屏幕让出来。"""
        chat.desktop.close()
        threading.Thread(target=chat.browser.close, daemon=True).start()  # 和 _fresh_browser 一样：不让别人等它
