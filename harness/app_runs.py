"""定时任务的一次运行、聊天通道里的一个聊天：只属于它的那几样东西（App 的一组方法，见 app.py 开头）。

它们和你的对话同时进行，所以凡是"会被停下"的都各用各的：自己的 shell、自己名下的 MCP 调用、自己的浏览器和电脑的 session、
自己那一档权限和沙箱，花的钱记在 task 或 channel 名下。_side_run 是两边共用的那一套，unattended_agent（定时任务）和
channel_agent（聊天通道）各填上自己的那几样。交回去的是一个 UnattendedRun：助手，加上停下（stop）和收尾（close）。
它们不拿网页上的任何一把锁（见 webui/scheduler.py、channels/session.py）。

为什么单独一个文件：你的对话（Chat，见 app_chat.py）和这两种"没人看着的运行"建起来的方式不一样，分开看不会混；
共用的只有工具的摆法（和 app_tools.py 的 conversation_tools 一样）和大家共用的那几样（权限、遮密钥、备用模型、账本）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .agent import Agent
from .agents import CustomAgent
from .agents import tools as agent_tools
from .agents.catalog import notifier as agent_notifier
from .app_types import AppPart
from .desktop import manager as desktop_labels
from .fetch.session import FetchSession
from .hooks import HookRun
from .sandbox import DEFAULT as SANDBOX_DEFAULT
from .sandbox import Sandbox
from .skills.catalog import notifier
from .tools import ToolRegistry
from .tools.browser import make_browser_tools
from .tools.computer import make_computer_tools
from .tools.send import make_send_tool
from .tools.shell import Shell, make_shell_tool


@dataclass
class UnattendedRun:
    """定时任务的一次运行用的东西（见 App.unattended_agent）。"""

    agent: Agent
    shell: Shell  # 这次运行自己的 shell
    stop: Callable[[], None]  # 停下这次运行正在做的事：结束它正在运行的命令、取消它正在等的 MCP 调用、停下浏览器和电脑的动作。别人的不碰
    close: Callable[[], None] = lambda: None  # 这次运行结束了：关掉它自己开的浏览器标签页，把屏幕让出来


class AppRuns(AppPart):
    # ---- 定时任务的一次运行、聊天通道里的一个聊天 ----------------------------------------

    def unattended_agent(self, ui: Any, approval: str = "ask", sandbox: str = SANDBOX_DEFAULT) -> UnattendedRun:
        """给定时任务的一次运行准备一个助手，以及只属于这次运行的那几样东西（见 UnattendedRun）。

        定时任务和你的对话、和别的任务同时运行（见 webui/scheduler.py），所以凡是"会被停下"的东西都各用各的：
          - shell 是新建的：停下这次运行（调用的人把 run.stop 登记到停止上），只结束它自己正在运行的命令；
            你点对话里的"停止"、停下另一个任务，也碰不到它；
          - 适配层是新建的，历史是空的：任务从头开始，不带着你正在聊的内容，也不往你的对话里写东西；
          - 用的是此刻设置里选中的那一家、那个模型。之后你再换模型，只影响以后的运行；
          - 权限是这个任务自己的那一档（approval，见 Permissions.unattended），和你在对话里选的哪一档无关；
          - 沙箱也是这个任务自己的那一档（sandbox：default 跟随设置、off、workspace、network，见 sandbox/）；
          - MCP 的工具是开始运行那一刻拍的快照（和一段对话一样，见 app_tools.py 的 fresh_tools），而且记在这次运行自己的名下：
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
            （见 app_conversations.py 的 new_conversation），提示词换成它的设定，工具只有它放开的那些。
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
        # 聊天通道里的会话还有 send_file（发到这个聊天里）。放的位置和网页上打开这段会话时一样（见 app_tools.py 的 conversation_tools）：
        # 两边的工具清单一字不差，在网页上接着聊也照样命中缓存
        own_send = [make_send_tool(self.permissions.editor, outlet)] if source == "channel" else []
        # 读网页：这次运行自己的一份（允许过的网站、这条消息里读了几次）。聊天通道里一段会话跑很多轮，一直用这一份：
        # 这段会话里允许过的网站不再问。放的位置和网页上一样（见 app_tools.py 的 conversation_tools），两边的工具清单才对得上
        fetch_session = FetchSession()
        own_fetch = [self._fetch_tool(lambda: fetch_session, lambda: agent.turns)] if self.settings.web_fetch else []
        tools = ToolRegistry([*self.tools.replaced(make_shell_tool(shell)), *own_fetch, *own_browser, *own_desktop, *own_send])
        backend = self._backend_for(provider_id, model, persona)
        agent = Agent(backend=backend, config=self.config, tools=tools, permissions=permissions, ui=ui, clock=self.agent.clock)
        # 也能把事情交给子 Agent：它的 agent 工具由它自己来"叫"（确认按这次运行的那一档，停止只停这次运行，见 agents/runner.py）
        delegate = self.delegator.tool_for(agent)
        # 这一次运行也是"一段对话"：用此刻已经连上的 MCP 服务器的工具拍一张快照，跑完之前不再变。
        # 不改 self.mcp.active：那是你正在看的这段对话的快照，信息栏和 MCP 一页按它显示
        everything = [*tools, *([delegate] if delegate else []), *self.mcp.snapshot(owner).tools]
        # 用一个 Agent 当角色的：只留它放开的工具（和网页上的 conversation_tools 一样，见 app_tools.py）
        agent.use_tools(ToolRegistry(agent_tools.allowed(persona.spec.tools, everything) if persona is not None else everything))
        # 花的钱也记进账本，来源写成 task 或 channel，花费明细里就能和对话分开看。每次运行各有一个助手，各自记各自的
        agent.on_call = self.ledger.recorder(self.prices, source=source)
        agent.redact = self.agent.redact  # 遮密钥：和你的对话同一个（同一份已知的密钥、同一个开关，见 redact.py）
        agent.fallbacks = self.fallbacks  # 备用模型和你的对话用同一份设置（每次现读，见 app.py 的 build_app）
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
