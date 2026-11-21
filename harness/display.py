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
    if name.startswith(("browser.", "browser_")):  # 浏览器：browser.navigate https://…、browser.left_click ref_12
        return short(f"{name} {_browser_detail(tool_input)}".rstrip(), 200)
    return f"{name} {short(json.dumps(tool_input, ensure_ascii=False), 200)}"


def _browser_detail(tool_input: dict) -> str:
    """浏览器的一次调用最要紧的那一点：去哪个网址、点哪个元素、输入什么。"""
    parts = []
    target = tool_input.get("target")
    if isinstance(target, dict):
        parts.append(str(target.get("ref") or f"({target.get('x')}, {target.get('y')})"))
    for key in ("url", "ref", "query", "key", "action", "tab_id", "scroll_direction", "direction", "filter"):
        if tool_input.get(key) not in (None, ""):
            parts.append(str(tool_input[key]))
    if "x" in tool_input and "y" in tool_input:
        parts.append(f"({tool_input['x']}, {tool_input['y']})")
    for key in ("text", "value"):
        if tool_input.get(key) not in (None, ""):
            parts.append(f"“{short(str(tool_input[key]), 60)}”")
    if isinstance(tool_input.get("region"), list):
        parts.append(str(tool_input["region"]))
    if tool_input.get("duration") is not None or tool_input.get("seconds") is not None:
        parts.append(f"{tool_input.get('duration', tool_input.get('seconds'))}s")
    return " ".join(parts)
