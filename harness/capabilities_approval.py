from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .capabilities_kinds import Approval, Rule
from .i18n import k, t

# ---- 问不问你：拿几个有代表性的操作去问 permissions.py ---------------------------------


@dataclass(frozen=True)
class Probe:
    """一次假想的工具调用：名字、参数、（需要的话）工具本身。只拿去问 permissions 要不要问你，从来不执行。"""

    name: str
    input: dict
    tool: Any = None


def _probe(permissions: Any, everywhere: Any, probe: Probe) -> str:
    """这一次调用在现在这一档下会怎样：never 根本不做 / free 直接做 / always 哪一档都要问 / ask 先问你。

    和 agent.py 执行一次调用时走的是同一条路：先看工具自己拒不拒绝（Tool.check），再看 review 要不要问、
    auto_allows（这里用 allowance）在这一档下能不能不问。"哪一档都要问"的意思是：换成"全部自动允许"也照样问
    （everywhere 是同样的规矩、模式是 all 的那一份）。
    """
    tool = probe.tool
    if tool is not None and tool.check is not None and tool.check(probe.input):
        return "never"
    if permissions.review(probe.name, probe.input, tool) is None:
        return "free"
    if permissions.allowance(probe.name, probe.input, tool) is not None:
        return "free"
    if everywhere.allowance(probe.name, probe.input, tool) is None:
        return "always"
    return "ask"


_STRICT = ("free", "ask", "always")


def _rule(permissions: Any, everywhere: Any, label: str, probes: list[Probe], free: str, ask: str, never: str = "") -> Rule:
    """一类操作。probes 是这一类里有代表性的几次调用，取最严的那个结果（有一个根本不做，就算根本不做）。
    free、ask、never 是这一类在各种结果下拼进总结里的说法（原文）。"""
    outcomes = [_probe(permissions, everywhere, probe) for probe in probes]
    outcome = "never" if "never" in outcomes else max(outcomes, key=_STRICT.index)
    sentence = {"free": free, "ask": ask, "always": ask, "never": never or ask}[outcome]
    return Rule(t(label), outcome, t(sentence))


def _approval(rules: list[Rule], all_free: str = "", all_ask: str = "", note: str = "") -> Approval:
    """几类操作的结果 → 一句话的总结和一个级别。总结最多两小句："直接做的；要问的"，有根本不做的放在最前面。"""
    free = [rule for rule in rules if rule.outcome == "free"]
    asks = [rule for rule in rules if rule.outcome in ("ask", "always")]
    never = [rule for rule in rules if rule.outcome == "never"]
    if never:
        parts = [never[0].sentence, *[rule.sentence for rule in (asks or free)[:1]]]
    elif free and asks:
        parts = [free[0].sentence, asks[0].sentence]
    elif asks:
        parts = [t(all_ask) if all_ask else asks[0].sentence]
    else:
        parts = [t(all_free or k("直接做，不用问你"))]
    summary = parts[0] if len(parts) == 1 else t("{first}；{second}", first=parts[0], second=parts[1])
    if not asks:
        level = "free"
    elif free:
        level = "some"
    else:
        level = "always" if all(rule.outcome == "always" for rule in asks) else "ask"
    return Approval(level, summary, rules, t(note) if note else "")
