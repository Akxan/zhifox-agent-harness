from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .agents import tools as agent_tools
from .capabilities_approval import Probe, _rule
from .capabilities_kinds import _KIND, LOCAL, Capability, Rule, group_of
from .i18n import t
from .providers import PROVIDERS, find
from .tools.base import Tool


@dataclass
class _Context:
    """算一份清单要看的东西。网页上你正在看的这段对话，和一次定时任务、聊天通道的运行，各有一份。"""

    app: Any
    agent: Any
    tools: list[Tool]  # 这段对话真正发给模型的工具
    started: bool
    persona: Any
    channel: str  # 这段会话是在哪个聊天通道里开始的（空的是网页上开的）
    in_chat: bool  # 现在是不是真的在聊天软件里（send_file 发得出去）
    unattended: bool  # 定时任务、聊天通道的运行（"操作整台电脑"另有一个开关）
    trusted: tuple[bool, bool]  # 这段对话里（截图，点击打字）不再问。只有你正看着的对话能选，定时任务、聊天通道的运行都是 False
    skills: list[dict]
    agents: list[dict]
    everywhere: Any = None

    def __post_init__(self) -> None:
        permissions = self.agent.permissions
        self.everywhere = permissions.unattended("all")
        self.everywhere.trust_mcp = permissions.trust_mcp

    @property
    def permissions(self) -> Any:
        return self.agent.permissions

    @property
    def provider(self) -> Any:
        return find(self.agent.backend.provider) or PROVIDERS["anthropic"]

    def here(self, group: str) -> list[Tool]:
        return [tool for tool in self.tools if group_of(tool) == group]

    def blocked(self, *samples: Tool) -> bool:
        """这段对话的角色有没有挡住这些工具（一个都挑不出来）。普通的对话、不限制工具的角色都不挡。"""
        persona = self.persona
        return persona is not None and persona.spec.tools is not None and not agent_tools.allowed(persona.spec.tools, samples)

    def persona_reason(self) -> str:
        return t("这段对话的角色「{name}」没有放开它。", name=self.persona.name)

    def rule(self, label: str, probes: list[Probe], free: str, ask: str, never: str = "") -> Rule:
        return _rule(self.permissions, self.everywhere, label, probes, free, ask, never)


def _item(kind_id: str, status: str, reason: str = "", **fields: Any) -> Capability:
    kind = _KIND[kind_id]
    fields.setdefault("where_text", t(LOCAL))
    return Capability(id=kind.id, icon=kind.icon, label=t(kind.label), description=t(kind.description), status=status, reason=reason, **fields)


def _sample(name: str) -> Tool:
    """一个只有名字的工具：拿去问"角色放没放开它"、"问不问你"。从来不执行。"""
    return Tool(spec={"name": name})


def _missing(context: _Context, kind_id: str, *samples: Tool) -> Capability:
    """一个一直都在的功能，这段对话里却没有：多半是角色没放开它。"""
    reason = context.persona_reason() if context.blocked(*samples) else t("这段对话没有带上它。")
    return _item(kind_id, "unavailable", reason)
