"""状态里的 capabilities 一节：信息栏里"智狐能做的事"要画的东西。

清单本身由 harness/capabilities.py 算（哪些功能在用、为什么关着、问不问你……，规矩只在那里算一份）。
这里只做一件事：把里面给人看的每一句话换成页面认得的样子（webui/ui.py 的 said：译好的文字，加上原文和填进去的值），
页面换语言时照着原文在原地重译，不用再问一遍服务器。MCP 服务器的名字是你起的，原样给（只有 text）。
"""

from __future__ import annotations

from typing import Any

from .. import capabilities
from .ui import said


def page_state(app: Any, skills: list[dict] | None = None, agents: list[dict] | None = None) -> dict:
    """状态里的那一节。skills、agents 是同一份状态里已经读好的列表（不再读一遍文件夹）。"""
    found = capabilities.overview(app, skills=skills, agents=agents)
    return {
        "mode": found.mode,
        "started": found.started,
        "notes": [said(note) for note in found.notes],
        "items": [_item(item) for item in found.items],
    }


def _item(item: capabilities.Capability) -> dict:
    approval = item.approval
    return {
        "id": item.id,
        "icon": item.icon,
        "label": said(item.label),
        "description": said(item.description),
        "status": item.status,
        "reason": said(item.reason) if item.reason else None,
        "count": item.count,
        "approval": None if approval is None else {
            "level": approval.level,
            "summary": said(approval.summary),
            "rules": [{"text": said(rule.text), "outcome": rule.outcome} for rule in approval.rules],
            "note": said(approval.note) if approval.note else None,
        },
        "where": item.where,
        "where_text": said(item.where_text),
        "tools": list(item.tools),
        "actions": [{"kind": action.kind, "target": action.target, "label": said(action.label)} for action in item.actions],
    }
