"""一段对话手里有哪些工具（App 的一组方法，见 app.py 开头）。

一段对话的工具说明书从头到尾不变（变了 Claude 的缓存就失效，原因见 providers/claude.py），所以工具在这段对话开始时就摆好：
  - fresh_tools：第一条消息发出之前（或者打开一段旧会话时）拍一张 MCP 的快照，连同自己的工具一起换上；
  - conversation_tools：一段对话用哪些工具：基本的那一套（用它自己的 shell）、读网页、浏览器、操作电脑、send_file、
    agent 工具、MCP 的快照；用一个 Agent 当角色的，只留它放开的。浏览器、电脑、读网页是"设置里开着，或者这段对话用过"
    才带上（_wants_fetch、_wants_browser、_wants_computer）；
  - refresh_tools：设置里开、关了其中一样，还没开始的这一段马上换上，开始了的不动；
  - toolbox、tool_names：信息栏"智狐能做的事"（capabilities.py）和终端的 /tools 照着画的那一份。
定时任务和聊天通道的一次运行摆的是同一套、顺序也一样（见 app_runs.py 的 _side_run），两边的工具清单才对得上。

为什么单独一个文件："这段对话真正有哪些工具"只在这里算（toolbox），别处只照着用。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from .agents import CustomAgent
from .agents import tools as agent_tools
from .app_chat import Chat
from .app_types import AppPart
from .fetch.session import FetchSession
from .mcp.bridge import PREFIX as MCP_PREFIX
from .tools import ToolRegistry
from .tools.browser import make_browser_tools
from .tools.browser import used_in as browser_used_in
from .tools.computer import make_computer_tools
from .tools.computer import used_in as computer_used_in
from .tools.fetch import FetchTool, make_fetch_tool
from .tools.fetch import used_in as fetch_used_in
from .tools.send import make_send_tool


class AppTools(AppPart):
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
        # 浏览器和电脑的 session 每次调用时现找：换一段对话时 chat 换上新的（见 app_chat.py 的 _fresh_browser），工具还是这一套
        browser = make_browser_tools(lambda: chat.browser, lambda: self.browser.enabled) if self._wants_browser(chat) else []
        # 操作整台电脑也一样：设置里开着才带上（工具包的说明书每次请求约 4500 个 token），用过它的旧会话也带上
        desktop = make_computer_tools(lambda: chat.desktop, lambda: self.desktop.enabled) if self._wants_computer(chat) else []
        send = [make_send_tool(self.permissions.editor, None)] if channel else []
        # 读网页（见 tools/fetch.py）：和浏览器一样，设置里开着才带上，用过它的旧会话也带上。Claude 开着联网搜索时不发给它
        # （服务器上本来就有一个，见 Backend.sends）。它的那一份状态每次调用时现找：换一段对话时 chat 换上新的（见 app_chat.py 的 _fresh_fetch）
        fetch = [self._fetch_tool(lambda: chat.fetch, lambda: chat.agent.turns)] if self._wants_fetch(chat) else []
        tools = [*chat.tools, *fetch, *browser, *desktop, *send, *([delegate] if delegate else []), *mcp_tools]
        return ToolRegistry(agent_tools.allowed(persona.spec.tools, tools) if persona is not None else tools)

    def _fetch_tool(self, session: Callable[[], FetchSession], turn: Callable[[], object]) -> FetchTool:
        """读网页的工具。开没开每次现看设置；每条消息最多读几次和 Claude 的联网搜索共用一个数（HARNESS_MAX_SEARCHES）。"""
        return make_fetch_tool(session, lambda: self.settings.web_fetch, lambda: self.fetcher, turn, self.config.max_searches)

    def _wants_fetch(self, chat: Chat) -> bool:
        if self.settings.web_fetch:
            return True
        backend = chat.agent.backend
        return fetch_used_in(backend.export()) if backend is not None else False

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

    def refresh_tools(self, chat: Chat | None = None) -> None:
        """设置里开、关了浏览器（或者"操作整台电脑"、读网页）：这段对话还没开始的话，马上换上（信息栏里的工具清单跟着变）；开始了的不动（见 conversation_tools）。"""
        chat = chat or self.chat
        if not chat.agent.backend.export():
            chat.agent.use_tools(self.conversation_tools(chat.persona, chat=chat))

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
