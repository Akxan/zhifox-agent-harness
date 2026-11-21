"""每个工具找到了几样：导入页顶上的总览，和"没找到的"那一栏。

导入页打开的是某一样（MCP、Skill 或 Agent）的清单，但顶上要告诉你每个工具里三样各找到了几个：
"Cursor · 3 个 MCP 服务器 · 2 个 Skill"，点另外两样就换到那一页去导入。正在看的那一样，数目由那一页的清单给
（known，免得把同一批文件读两遍）；另外两样只数一数，不读内容：Skill 数有 SKILL.md 的文件夹，Agent 数文件，
MCP 要读一遍配置文件才知道有几个服务器（这几个文件都不大）。

没找到任何东西的工具也列出来（"没找到的"），带着去过哪些位置、官方文档的链接：你可以知道智狐认得哪些工具，
以及为什么没找到（比如你的配置放在了别处）。
"""

from __future__ import annotations

from .places import Env
from .registry import KINDS, MCP, SKILLS, TOOLS

Counts = dict[str, tuple[int, int]]  # 工具的编号 → (找到了几样, 几个文件读不出来)


def overview(env: Env, known: dict[str, Counts] | None = None) -> list[dict]:
    """[{"id", "name", "docs", "blurb", "kinds", "counts": {种类: 几样}, "problems", "found", "looked": {种类: [位置]}}]，按登记的顺序。"""
    # 放在这里而不是文件开头：这三个模块都要用 imports 包里的东西，写在开头就成了互相导入
    from ..agents import from_tools as agents
    from ..mcp import importers as mcp
    from ..skills import from_tools as skills

    counters = {MCP: mcp.count, SKILLS: skills.count}
    known = dict(known or {})
    counts = {kind: known[kind] if kind in known else counters.get(kind, agents.count)(env) for kind in KINDS}
    listed = []
    for tool in TOOLS:
        here = {kind: counts[kind].get(tool.id, (0, 0)) for kind in tool.kinds}
        found, problems = {kind: value[0] for kind, value in here.items()}, sum(value[1] for value in here.values())
        looked: dict[str, list[str]] = {}
        for place in tool.where(env):
            looked.setdefault(place.kind, [])
            shown = env.shown(place.path) + ("/" if place.folder else "")
            if shown not in looked[place.kind]:
                looked[place.kind].append(shown)
        listed.append({
            "id": tool.id, "name": tool.name, "docs": tool.docs, "blurb": tool.blurb, "kinds": list(tool.kinds),
            "counts": found, "problems": problems, "found": bool(problems or any(found.values())), "looked": looked,
        })  # fmt: skip
    return listed
