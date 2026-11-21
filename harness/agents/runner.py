"""把一项任务交给子 Agent：这一层做的事，就是 agent 工具（tools/agent.py）被调用时发生的一切。

    主助手调用 agent(name, task)
      └─► 读出这个 Agent 的文件（它的设定、能用的工具、用的模型）
            └─► 建一个新的 Agent 循环（agent.py 里的同一个类）：
                  - 历史是空的：它只看得到 task，看不到主助手和你的对话
                  - 系统提示词是它自己的设定，加上程序附上的环境说明（见 prompt.py）
                  - 工具是主助手手里那一套的一部分（见 tools.allowed）：永远不会比主助手多
                  - 模型是它文件里写的那一个，没写就和主助手用同一个
            └─► 跑一轮，直到它不再调用工具
      ◄── 它最后写的那段话，加上一句"它做了什么"，作为工具的结果交回给主助手

子 Agent 的意义是"上下文隔离"：它可能读了二十个网页、几十个文件，这些都留在它自己的历史里，
主助手的历史里只多了一段结论。主助手的上下文因此不会被塞满，之后的每一轮也不用为那些原文付钱。

几条规矩，以及为什么：

  确认照常问，档位跟着叫它的那一方。
    子 Agent 的每一次工具调用都经过同一个 Permissions（主助手的那一份对象本身），所以问不问、问什么都和主助手一样：
    你的对话里就按你在页面上选的那一档；定时任务里就按那个任务自己的那一档。确认卡片上写明是哪个 Agent 在问。

  会被停下的东西各用各的。
    和定时任务的每次运行一样（见 App.unattended_agent）：子 Agent 有自己的 shell、自己名下的 MCP 调用。
    你点"停止"时，叫它的那一方会通知它（ui.on_stop），它只结束自己正在运行的命令、只取消自己的 MCP 调用，
    然后整个停下来——主助手那边的"停止"本来就在进行，接着它也会停。
    MCP 的工具重新拍一张快照、记在子 Agent 名下，但只留下主助手手里本来就有的那几个（同一个服务器的同一个工具）。

  花的钱照样记账。
    每次调用模型记一笔，来源写成 subagent（见 ledger.py），花费明细里能和对话、定时任务分开看。
    它的用量也加进叫它的那一方的合计里（不算"调用了几次模型"、不算上下文大小），所以对话里显示的费用包括它。

  层数和步数都有上限。
    AGENT_MAX_DEPTH（config.py）决定能交几层：默认 1，子 Agent 手里没有 agent 工具，不能再往下交。
    AGENT_MAX_STEPS 决定它最多调用几次模型：到了就停下，把做到哪儿交回去。

它在叫它的那一方的线程里运行（一次工具调用就是一次函数调用），所以不需要新的锁。
"""

from __future__ import annotations

import dataclasses
import secrets
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from ..agent import UI, Agent
from ..config import AGENT_MAX_DEPTH, AGENT_MAX_RESULT_CHARS, AGENT_MAX_STEPS, Config
from ..display import describe_call
from ..i18n import Message, fill
from ..ledger import Ledger
from ..mcp import McpManager
from ..mcp.bridge import PREFIX as MCP_PREFIX
from ..mcp.bridge import McpTool
from ..pricing import estimate, usage_key
from ..providers import PROVIDERS, ClientFactory, ProviderError, has_key, make_backend
from ..providers.base import Usage
from ..skills import SkillStore
from ..skills.catalog import notifier as skill_notifier
from ..tools import ToolRegistry
from ..tools import shell as shell_tools
from ..tools.agent import make_agent_tool
from ..tools.base import Tool, ToolError, ToolOutput
from ..tools.shell import Shell, make_shell_tool
from . import tools as agent_tools
from .catalog import notifier as agent_notifier
from .prompt import subagent_prompt
from .store import AgentError, AgentInvalid, AgentStore, CustomAgent


class ParentUI(UI, Protocol):
    """叫子 Agent 的那一方的界面，除了 agent.py 里的那几样，还要能做这三件事。

    终端界面（../ui.py）、网页界面（webui/ui.py，定时任务的 RunUI 也是它）和 SubUI 自己都有。
    """

    def subagent(self, step: str, **data: Any) -> None: ...  # 子 Agent 的一步进展：start、activity、tool_call、tool_result、auto、note、done
    def check_stop(self) -> None: ...  # 用户点了"停止"的话抛出 KeyboardInterrupt
    def on_stop(self, callback: Callable[[], None]) -> Callable[[], None]: ...  # 点"停止"时也调用它；返回的函数用来取消登记


class SubUI:
    """子 Agent 用的界面。它自己不显示任何东西，把进展交给叫它的那一方的界面，由那边画成一张"交给 某某"的卡片。

    逐字输出的每一小段都不往上传（一段回答几百小段，全发给页面太多了）：只在"在想""在写"变了的时候说一声，
    但每一小段都看一眼要不要停——不然子 Agent 写一段长回答的时候，你点"停止"要等它写完才停得下来。
    """

    def __init__(self, parent: ParentUI, run: str, name: str):
        self.parent = parent
        self.run = run  # 这一次交给它的编号：页面靠它把进展放进对的那张卡片
        self.name = name
        self.answer = ""  # 最后一次调用工具之后写的文字，就是它交回去的回答
        self.calls: Counter[str] = Counter()  # 它调用了哪些工具、各几次
        self.infos: list[str] = []  # 循环自己的提示（截断了、步数用完了、被拒绝了），由 Delegator 换成给主助手的话
        self._doing = ""

    def _pulse(self, doing: str) -> None:
        self.parent.check_stop()
        if doing != self._doing:
            self._doing = doing
            self.parent.subagent("activity", run=self.run, agent=self.name, doing=doing)

    def thinking(self, text: str) -> None:
        self._pulse("thinking")

    def text(self, text: str) -> None:
        self.answer += text
        self._pulse("writing")

    def notice(self, level: str, text: str) -> None:
        self.parent.check_stop()
        self.parent.subagent("note", run=self.run, agent=self.name, level=level, text=text)

    def end_stream(self) -> None:
        pass

    def info(self, text: str) -> None:
        self.infos.append(text)

    def tool_call(self, name: str, tool_input: object) -> None:
        self.answer = ""
        self._doing = "tool"
        self.calls[name] += 1
        self.parent.subagent("tool_call", run=self.run, agent=self.name, name=name, summary=describe_call(name, tool_input), input=tool_input)

    def tool_result(self, text: str, is_error: bool) -> None:
        self.parent.subagent("tool_result", run=self.run, agent=self.name, text=text, is_error=is_error)

    def confirm(self, description: str, agent: str = "") -> tuple[bool, str]:
        # 确认卡片照常画在页面上，只是写明是哪个 Agent 在问（再往下一层的，写的是最里面那一个）
        return self.parent.confirm(description, agent=agent or self.name)

    def auto_allowed(self) -> None:
        self.parent.subagent("auto", run=self.run, agent=self.name)

    # 再往下一层的子 Agent（层数上限调大了才会有）：它的进展、停止原样交给上一层
    def subagent(self, step: str, **data: Any) -> None:
        self.parent.subagent(step, **data)

    def check_stop(self) -> None:
        self.parent.check_stop()

    def on_stop(self, callback: Callable[[], None]) -> Callable[[], None]:
        return self.parent.on_stop(callback)


class Delegator:
    """给每个会叫子 Agent 的一方做一个 agent 工具，并在它被调用时把子 Agent 跑起来。"""

    def __init__(
        self,
        config: Config,
        store: AgentStore,
        skills: SkillStore,
        mcp: McpManager,
        ledger: Ledger,
        prices: Callable[[], dict[str, dict]],
        client_factory: ClientFactory,
    ):
        self.config = config
        self.store = store
        self.skills = skills
        self.mcp = mcp
        self.ledger = ledger
        self.prices = prices
        self.client_factory = client_factory

    def tool_for(self, parent: Agent, depth: int = 0) -> Tool | None:
        """parent 用的 agent 工具（它交出去的任务，由 parent 来"叫"）。depth 是 parent 自己在第几层：主助手是 0。
        到了层数上限就没有这个工具（返回 None）。"""
        if depth >= AGENT_MAX_DEPTH:
            return None
        return make_agent_tool(lambda name, task: self.run(parent, depth, name, task))

    # ---- 交出去一项任务 ------------------------------------------------------

    def run(self, parent: Agent, depth: int, name: str, task: str) -> ToolOutput:
        task = task.strip()
        if not task:
            raise ToolError("task 是空的。写清楚要它做什么、交回什么。")
        agent = self._find(name)
        provider_id, model = self._model(parent, agent)
        sub_config = dataclasses.replace(self.config, max_steps=AGENT_MAX_STEPS)

        # 会被停下的东西各用各的（见文件开头）。工具先挑好，再看有没有联网的（提示词里那一行要看它）
        own_shell, owner = Shell(self.config.workspace), object()
        tools = self._tools(parent, agent, own_shell, owner)
        provider = PROVIDERS[provider_id]
        web = self.config.web and provider.web and agent_tools.has_web(tools)
        prompt = subagent_prompt(self.config, agent, web)
        backend = make_backend(provider, model, sub_config, ToolRegistry([]), prompt, self.client_factory(provider))
        run_id = secrets.token_hex(4)
        ui = SubUI(parent.ui, run_id, agent.name)
        sub = Agent(backend=backend, config=sub_config, tools=ToolRegistry([]), permissions=parent.permissions, ui=ui, clock=parent.clock)
        # 它还能不能再往下交：叫它的那一方手里有 agent 工具、它自己的 tools 放开了、层数也没到上限
        nested = self.tool_for(sub, depth + 1) if parent.tools.get(agent_tools.AGENT_TOOL) is not None else None
        if nested is not None:
            tools = agent_tools.allowed(agent.spec.tools, [*tools[: self._mcp_start(tools)], nested, *tools[self._mcp_start(tools) :]])
        sub.use_tools(ToolRegistry(tools))
        sub.on_call = self._recorder(parent)
        if sub.tools.get("skill") is not None:
            sub.notes.append(skill_notifier(self.skills))  # 它也能用 Skill：第一条消息带上 Skill 的目录
        if sub.tools.get(agent_tools.AGENT_TOOL) is not None:
            sub.notes.append(agent_notifier(self.store, lambda: sub.tools))

        def stop() -> None:
            own_shell.kill_running()
            self.mcp.cancel_running(owner)

        within = parent.ui.run if isinstance(parent.ui, SubUI) else ""
        parent.ui.subagent(
            "start", run=run_id, agent=agent.name, color=agent.spec.color, model=model, provider=provider_id, task=task,
            depth=depth + 1, within=within,
        )
        forget = parent.ui.on_stop(stop)
        try:
            sub.run_turn(task)
        except KeyboardInterrupt:
            self._done(parent, sub, ui, "stopped")
            raise  # 叫它的那一方也在停：让它照常停下（它会给这次工具调用补上"被中断"的结果）
        except ProviderError as exc:
            return self._failed(parent, sub, ui, exc.args[0] if exc.args else str(exc))
        except Exception as exc:  # noqa: BLE001 —— 子 Agent 里出了什么错都不该让主助手的这一轮崩掉：作为出错的结果交回去
            return self._failed(parent, sub, ui, f"{type(exc).__name__}: {exc}")
        finally:
            forget()
        return self._result(parent, sub, ui, agent, model)

    def _find(self, name: str) -> CustomAgent:
        try:
            return self.store.usable(name)
        except AgentInvalid as exc:
            raise ToolError(f"Agent「{name}」的文件有问题（{exc.problems[0]}），暂时不能用。可以请用户在“扩展 → Agent”里修好它。") from None
        except AgentError:
            names = [entry for entry, _ in self.store.catalog()]
            available = f"现在可以交给的有：{'、'.join(names)}。" if names else "现在一个 Agent 都没有。"
            raise ToolError(f"没有叫 {name} 的 Agent。{available}") from None

    def _model(self, parent: Agent, agent: CustomAgent) -> tuple[str, str]:
        """它用哪一家的哪个模型：文件里写了就用写的，没写（inherit）就和叫它的那一方一样。"""
        if not agent.spec.model:
            return parent.backend.provider, parent.backend.model
        provider_id, model = agent.spec.provider, agent.spec.model_id
        provider = PROVIDERS[provider_id]
        # 和叫它的那一方是同一家的话，密钥一定是有的（不然那一方也跑不起来）；另一家要看一眼
        if provider_id != parent.backend.provider and not has_key(provider):
            raise ToolError(f"Agent「{agent.name}」用的是 {provider.label} 的 {model}，但用户还没有设置 {provider.label} 的 API 密钥，所以交不出去。可以自己做，或者请用户去设置里填上密钥。")
        return provider_id, model

    def _tools(self, parent: Agent, agent: CustomAgent, own_shell: Shell, owner: object) -> list[Tool]:
        """它能用的工具：叫它的那一方手里有的、它的 tools 放开了的（还不含 agent 工具，见 run）。"""
        own = []
        for tool in parent.tools:
            if tool.name == agent_tools.AGENT_TOOL or tool.name.startswith(MCP_PREFIX):
                continue
            own.append(make_shell_tool(own_shell) if tool.name == shell_tools.TOOL_NAME else tool)
        # MCP 的工具重新拍一张、记在它自己名下，只留下叫它的那一方本来就有的（同一个服务器的同一个工具）
        had = {tool.name: (tool.server, tool.remote) for tool in parent.tools if isinstance(tool, McpTool)}
        mine = [tool for tool in self.mcp.snapshot(owner).tools if isinstance(tool, McpTool) and had.get(tool.name) == (tool.server, tool.remote)]
        return agent_tools.allowed(agent.spec.tools, [*own, *mine])

    @staticmethod
    def _mcp_start(tools: list[Tool]) -> int:
        """MCP 的工具从第几个开始（agent 工具放在它们前面，和主助手的顺序一样）。"""
        return next((index for index, tool in enumerate(tools) if tool.name.startswith(MCP_PREFIX)), len(tools))

    def _recorder(self, parent: Agent) -> Callable[[str, str, Usage, datetime], None]:
        """每调用一次模型：记一笔账（来源 subagent），再加进叫它的那一方的合计里。"""
        record = self.ledger.recorder(self.prices, source="subagent")

        def on_call(provider: str, model: str, usage: Usage, moment: datetime) -> None:
            record(provider, model, usage, moment)
            # 不算"调用了几次模型"（那是叫它的那一方自己的步数，定时任务的进度靠它），也不碰上下文的大小
            spent = dataclasses.replace(usage, requests=0, context_tokens=0)
            parent.usage.add(spent)
            parent.usage_by_model.setdefault(usage_key(model, moment), Usage()).add(spent)
            if parent.on_usage is not None:
                parent.on_usage()

        return on_call

    # ---- 交回去 --------------------------------------------------------------

    def _cost(self, sub: Agent) -> float | None:
        total, unpriced = estimate(dict(sub.usage_by_model), self.prices())
        return None if unpriced else round(total, 6)

    def _done(self, parent: Agent, sub: Agent, ui: SubUI, status: str, answer: str = "", error: str | Message = "") -> None:
        parent.ui.subagent(
            "done", run=ui.run, agent=ui.name, status=status, answer=answer, error=error,
            cost=self._cost(sub), steps=sub.usage.requests, calls=sum(ui.calls.values()),
        )

    def _failed(self, parent: Agent, sub: Agent, ui: SubUI, error: str | Message) -> ToolOutput:
        """出错了。卡片上的那句话跟着界面的语言（error 是 t() 写的话时，换语言能在原地重译）；交给模型的用中文原文。"""
        self._done(parent, sub, ui, "failed", error=error)
        chinese = fill(error.key, error.values) if isinstance(error, Message) else error
        return ToolOutput(text=f"Agent「{ui.name}」出错了，没有做完：{chinese}", is_error=True)

    def _result(self, parent: Agent, sub: Agent, ui: SubUI, agent: CustomAgent, model: str) -> ToolOutput:
        answer, outcome = ui.answer.strip(), sub.outcome
        did = f"它调用了 {sub.usage.requests} 次模型（{model}）"
        if ui.calls:
            did += "，用了这些工具：" + "、".join(f"{name} {count} 次" for name, count in ui.calls.most_common())
        else:
            did += "，没有用工具"
        if outcome == "refused":
            self._done(parent, sub, ui, "refused")
            note = f"（{ui.infos[-1]}）" if ui.infos else ""
            return ToolOutput(text=f"Agent「{agent.name}」的请求被模型拒绝了，没有结果{note}。可以换个说法再交给它，或者自己做。", is_error=True)
        if not answer:
            self._done(parent, sub, ui, "limit" if outcome == "steps" else "empty")
            reason = f"它用完了 {AGENT_MAX_STEPS} 次模型调用还没做完。" if outcome == "steps" else "它没有写出回答。"
            return ToolOutput(text=f"Agent「{agent.name}」没有交回内容：{reason}{did}。", is_error=True)
        shown = answer
        if len(answer) > AGENT_MAX_RESULT_CHARS:
            answer = answer[:AGENT_MAX_RESULT_CHARS] + f"\n……（回答太长，后面的 {len(answer) - AGENT_MAX_RESULT_CHARS} 个字没有交回）"
        if outcome == "steps":
            head = f"Agent「{agent.name}」用完了 {AGENT_MAX_STEPS} 次模型调用，还没做完就停下了。下面是它停下时写的内容"
        elif outcome == "truncated":
            head = f"Agent「{agent.name}」的回答达到了长度上限，被截断了。下面是它写出来的部分"
        else:
            head = f"Agent「{agent.name}」做完了这项任务。下面是它交回的回答（它看不到你和用户的对话）"
        self._done(parent, sub, ui, {"steps": "limit", "truncated": "truncated"}.get(outcome, "done"), answer=shown)
        return ToolOutput(text=f"{head}：\n\n{answer}\n\n——{did}。")
