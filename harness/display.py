"""把工具调用变成一行给人看的文字。终端界面、网页界面和各家的适配层都会用到。"""

from __future__ import annotations

import json


def short(text: str, limit: int) -> str:
    """压成一行，太长就截断。"""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_call(name: str, tool_input: object) -> str:
    """把一次工具调用压缩成一行，显示给用户看。"""
    if not isinstance(tool_input, dict):
        return f"{name} {tool_input!r}"
    if "command" in tool_input and "path" in tool_input:  # 文件工具和记忆工具
        return f"{name} {tool_input['command']} {tool_input['path']}"
    if set(tool_input) >= {"old_path", "new_path"}:  # 记忆工具的重命名
        return f"{name} rename {tool_input['old_path']} → {tool_input['new_path']}"
    if name == "run_command":
        return f"{name} $ {short(str(tool_input.get('command')), 200)}"
    if name == "agent":  # 交给一个子 Agent：agent 资料研究员 · 任务的开头
        return short(f"{name} {tool_input.get('name')} · {' '.join(str(tool_input.get('task') or '').split())}", 200)
    if name == "skill":  # 读一个 Skill：skill pdf，或者 skill pdf references/FORMS.md
        return short(f"{name} {tool_input.get('name')} {tool_input.get('file') or ''}".rstrip(), 200)
    return f"{name} {short(json.dumps(tool_input, ensure_ascii=False), 200)}"
