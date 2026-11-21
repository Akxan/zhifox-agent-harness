"""各家 Agent 文件 → 智狐的 Agent 文件（和 Claude Code 的 subagent 一样，见 agents/spec.py），以及改了什么、丢了什么。

"一份设定 + 能用哪些工具 + 用哪个模型"这件事各家都有，写法各不一样：

  工具              文件                                 名字从哪来          设定写在哪           工具
  Claude Code       .claude/agents/*.md                  name                正文                 Read, Bash……（和智狐一样）
  Cursor            .cursor/agents/*.md                  name，没写用文件名  正文                 没有 tools，只有 readonly
  GitHub Copilot    .github/agents/*.agent.md            文件名（name 只是显示的名字）  正文      execute、read、edit、服务器/工具……
  Gemini CLI        .gemini/agents/*.md                  name                正文                 read_file、run_shell_command……
  Qwen Code         .qwen/agents/*.md                    name                正文                 read_file、run_shell_command……
  OpenCode          .opencode/agents/*.md                文件名              正文                 tools: {write: false}、permissions
  Factory           .factory/droids/*.md                 name                正文                 Read、Execute、"read-only"……
  Codex             .codex/agents/*.toml                 name                developer_instructions   没有，features 里能关掉命令
  Kiro              .kiro/agents/*.json 或 *.md           name，没写用文件名  prompt 或正文        read、write、shell、@服务器……
  Amazon Q          .amazonq/cli-agents/*.json           name，没写用文件名  prompt               fs_read、execute_bash、@服务器……

Claude Code 的文件原样复制（格式本来就一样，智狐不用的字段原样留在文件里、界面上标出来）。别家的按下面的办法重写一份：
  - 只写智狐认得的几项：name、description、tools、model、color，加上正文。别的字段不搬，列出来告诉你。
  - name 不合智狐的规矩（有空格、斜杠）的，改成一个合格的；实在改不出来的，导入时让你起个名字。
  - tools 照 agent_tools.py 的表换成智狐的叫法，认不出来的不搬（只会更少、不会更多）。
  - model 认得的照搬，认不出来的（"Claude Sonnet 4.5 (copilot)" 这种显示的名字）改成"和叫它的那一方一样"。
  - 别家"不用问就调用"的设置（Kiro 的 allowedTools 这些）一律不搬：智狐照样每一步按你选的那一档问你。

各家文档：
  Claude Code     https://code.claude.com/docs/en/sub-agents
  Cursor          https://cursor.com/docs/subagents
  GitHub Copilot  https://docs.github.com/en/copilot/reference/custom-agents-configuration
  VS Code         https://code.visualstudio.com/docs/agents/reference/custom-agent-file-reference
  Gemini CLI      https://geminicli.com/docs/core/subagents/
  Qwen Code       https://qwenlm.github.io/qwen-code-docs/en/users/features/sub-agents/
  OpenCode        https://opencode.ai/docs/agents/ 、https://opencode.ai/docs/migrate-v1
  Factory         https://docs.factory.com/cli/configuration/custom-droids
  Codex           https://learn.chatgpt.com/docs/agent-configuration/subagents
  Kiro            https://kiro.dev/docs/custom-agents/configuration-reference/
  Amazon Q        https://aws.github.io/amazon-q-developer-cli/agent-format.html
"""

from __future__ import annotations

import json
import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..agents.spec import (
    COLORS,
    INHERIT,
    MAX_NAME,
    Fields,
    compose,
    name_problem,
    normal,
    resolve_model,
    split_tools,
)
from ..config import MAX_AGENT_BYTES
from ..i18n import Message, t
from ..skills.frontmatter import split_lines
from . import yaml_lite
from .agent_tools import translate

# 智狐认得的工具里，"一般的 Agent 都会用到"的那几样。别家的文件只是关掉了其中几样时，按它写出剩下的
_EVERYDAY = ("Read", "Edit", "Bash", "WebSearch", "WebFetch", "TodoWrite", "Skill", "Agent")
# 别家"不用问就调用"的设置
_NEVER_ASK = ("allowedTools", "toolsSettings", "autoApprove", "permissionMode", "approvalMode")


@dataclass
class Converted:
    text: str  # 智狐写法的全文（还没检查合不合格，见 agents/spec.py 的 check）
    changes: list[Message] = field(default_factory=list)  # 改了什么、丢了什么
    problem: Message | None = None  # 导入不了的原因


def stem_of(file: str, suffixes: tuple[str, ...]) -> str:
    """文件名去掉扩展名。.agent.md 这样的两段扩展名整个去掉。"""
    for suffix in sorted(suffixes, key=len, reverse=True):
        if file.endswith(suffix):
            return file[: -len(suffix)]
    return file.rsplit(".", 1)[0]


def fitting_name(name: str) -> str:
    """改写成合格的名字："Security Auditor" → "Security-Auditor"，"team/reviewer" → "team-reviewer"。"""
    name = re.sub(r"[^\w-]+", "-", normal(name).strip()).strip("-_")
    return name[:MAX_NAME].rstrip("-_")


def convert(form: str, text: str, file: str, path: Path, tool: str, suffixes: tuple[str, ...] = (".md",)) -> Converted:
    """把一个文件换成智狐的写法。file 是文件名（OpenCode 的子文件夹也带着，比如 team/reviewer.md）。"""
    stem = stem_of(file, suffixes)
    if form == "claude":
        return Converted(text)
    if form == "codex":
        return _codex(text, stem, tool)
    if form in ("amazonq", "kiro") and file.endswith(".json"):
        return _kiro_json(text, stem, path, tool)
    return _markdown(form, text, stem, tool)


# ---- 带 frontmatter 的 Markdown ---------------------------------------------------


def _split(text: str) -> tuple[str, str] | None:
    """（frontmatter 的那几行, 正文）。没有 frontmatter 的返回 None。"""
    lines = split_lines(text)
    if not lines or lines[0].rstrip() != "---":
        return None
    end = next((index for index in range(1, len(lines)) if lines[index].rstrip() == "---"), None)
    if end is None:
        return None
    return "\n".join(lines[1:end]), "\n".join(lines[end + 1 :])


def _markdown(form: str, text: str, stem: str, tool: str) -> Converted:
    parts = _split(text)
    if parts is None:
        return Converted(text, problem=t("开头没有 frontmatter：第一行要是 ---，下面写 name 和 description，再用一行 --- 结束。"))
    try:
        fields = yaml_lite.loads(parts[0]) or {}
    except yaml_lite.YamlError as exc:
        return Converted(text, problem=t("frontmatter 第 {line} 行看不懂：{error}", line=exc.line + 1, error=exc.args[0]))
    if not isinstance(fields, dict):
        return Converted(text, problem=t("frontmatter 应该是几行“名字: 值”。"))
    fields = {str(key): value for key, value in fields.items()}
    changes: list[Message] = []
    if form == "gemini" and fields.get("kind") == "remote":
        return Converted(text, problem=t("这是一个远程的 Agent（kind: remote，在别的服务器上运行），智狐用不了。"))

    name = _name(form, fields, stem, changes)
    tools = _markdown_tools(form, fields, tool, changes)
    model = _model(fields.get("model"), changes)
    color = _color(fields.get("color"), changes)
    used = {"name", "description", "tools", "model", "color"}
    if form == "cursor":
        used.add("readonly")
    if form == "opencode":
        used |= {"permissions", "permission"}
    _never_ask(fields, tool, changes)
    _dropped(fields, used | set(_NEVER_ASK), changes)
    description = fields.get("description")
    body = parts[1]
    return Converted(_compose(name, description, tools, model, color, body), changes)


def _name(form: str, fields: dict, stem: str, changes: list[Message]) -> str:
    """用哪个名字。Copilot 和 OpenCode 的"真名"是文件名（Copilot 的 name 只是显示的名字）；别家先看 name。"""
    written = fields.get("name")
    written = written.strip() if isinstance(written, str) else ""
    wanted = written if written and form not in ("opencode",) else stem
    if form == "copilot" and written and name_problem(normal(written)) is not None:
        wanted = stem  # 显示的名字有空格：用文件名（Copilot 自己也是按文件名认它）
        changes.append(t("{old} 只是显示的名字，用了文件名 {name}。", old=written, name=fitting_name(stem)))
    if not written and form not in ("opencode", "copilot"):
        changes.append(t("文件里没有写 name，用了文件名 {name}。", name=fitting_name(stem)))
    name = normal(wanted)
    if name_problem(name) is not None and fitting_name(name) and name_problem(fitting_name(name)) is None:
        changes.append(t("名字 {old} 不合智狐的规矩（只能有字母、数字、- 和 _），改成了 {new}。", old=name, new=fitting_name(name)))
        name = fitting_name(name)
    return name


def _markdown_tools(form: str, fields: dict, tool: str, changes: list[Message]) -> list[str] | None:
    if form == "cursor":
        if fields.get("readonly") is True:
            changes.append(t("{tool} 里它是只读的（readonly），导入后只能读文件和查资料：{names}", tool=tool, names="Read, WebSearch, WebFetch"))
            return ["Read", "WebSearch", "WebFetch"]
        return None
    if form == "opencode":
        return _opencode_tools(fields, tool, changes)
    raw = fields.get("tools")
    if raw is None:
        return None
    names = split_tools(raw) if not isinstance(raw, list) else [str(item) for item in raw if isinstance(item, str | int | float)]
    if names is None:
        changes.append(t("tools 的写法认不出来，没有搬过来：这个 Agent 导入后一个工具都不能用，可以导入后再编辑。"))
        return []
    return _translated(names, form, changes)


def _translated(names: list[str], vocabulary: str, changes: list[Message]) -> list[str] | None:
    result = translate(names, "copilot" if vocabulary == "cursor" else vocabulary)
    if result.changed:
        changes.append(t("工具换成了智狐的叫法：{pairs}", pairs="; ".join(f"{old} → {new}" for old, new in result.changed)))
    if result.dropped:
        changes.append(t("智狐没有这些工具，没有搬过来：{names}", names=", ".join(result.dropped)))
    if result.tools == [] and names:
        changes.append(t("原来的工具智狐一个都没有：这个 Agent 导入后不能用任何工具，可以导入后再编辑。"))
    return result.tools


def _opencode_tools(fields: dict, tool: str, changes: list[Message]) -> list[str] | None:
    """OpenCode 默认什么工具都能用，文件里写的是关掉哪几个：V1 的 tools: {write: false}、permission: {edit: deny}，
    V2 的 permissions: [{action, resource, effect: deny}]。有关掉的，就按智狐的叫法写出剩下的那几样。"""
    off: list[str] = []
    tools = fields.get("tools")
    if isinstance(tools, dict):
        off += [str(name) for name, value in tools.items() if value is False or value == "false"]
    permission = fields.get("permission")
    if isinstance(permission, dict):
        off += [str(name) for name, value in permission.items() if value == "deny"]
    rules = fields.get("permissions")
    for rule in rules if isinstance(rules, list) else []:
        if isinstance(rule, dict) and rule.get("effect") == "deny" and rule.get("resource") in (None, "*", "**") and isinstance(rule.get("action"), str):
            off.append(rule["action"])
    if not off:
        return None
    result = translate(off, "opencode")
    removed = set(result.tools or [])
    picked = [name for name in _EVERYDAY if name not in removed]
    changes.append(t("{tool} 里关掉的 {names} 在智狐里也不放开；别的写成了：{picked}", tool=tool, names=", ".join(off), picked=", ".join(picked)))
    return picked


def _model(value: object, changes: list[Message]) -> str:
    if isinstance(value, list):
        value = next((item for item in value if isinstance(item, str)), None)  # VS Code 可以写好几个，按顺序试
    if not isinstance(value, str) or not value.strip() or value.strip() == INHERIT:
        return ""
    text = value.strip()
    if "#" in text:  # OpenCode：provider/model#variant
        text = text.split("#", 1)[0]
    if text.startswith("google/"):
        text = "gemini/" + text.removeprefix("google/")
    _, problem, _ = resolve_model(text)
    if problem is not None:
        changes.append(t("model 写的是 {model}，智狐认不出来，改成了和叫它的那一方用同一个模型。", model=value.strip()))
        return ""
    if text != value.strip():
        changes.append(t("model 换成了智狐的写法：{old} → {new}", old=value.strip(), new=text))
    return text


def _color(value: object, changes: list[Message]) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    if value.strip().lower() in COLORS:
        return value.strip().lower()
    changes.append(t("颜色 {color} 智狐没有，没有搬过来。", color=value.strip()))
    return ""


def _never_ask(fields: dict, tool: str, changes: list[Message]) -> None:
    # permissionMode（Claude Code 的写法，Qwen Code 也认）、approvalMode（Qwen Code）写 default、plan 的就是"照常问"
    found = [key for key in _NEVER_ASK if fields.get(key) not in (None, "", [], {}, "default", "plan", "manual")]
    if found:
        changes.append(t("{tool} 里“不用问就调用”的设置（{fields}）没有搬过来：智狐照样每一步按你选的那一档问你。", tool=tool, fields=", ".join(found)))


def _dropped(fields: dict, used: set[str], changes: list[Message]) -> None:
    extra = [key for key in fields if key not in used]
    if extra:
        changes.append(t("智狐用不上、没有搬过来的字段：{fields}", fields=", ".join(extra)))


def _compose(name: str, description: object, tools: list[str] | None, model: str, color: str, body: str) -> str:
    description = " ".join(description.split()) if isinstance(description, str) else ""
    return compose(Fields(name=name, description=description, tools=tools, model=model, color=color, body=body))


# ---- Codex 的 TOML -------------------------------------------------------------


def _codex(text: str, stem: str, tool: str) -> Converted:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return Converted(text, problem=t("不是合法的 TOML：{error}", error=exc))
    changes: list[Message] = []
    body = data.get("developer_instructions")
    if not isinstance(body, str) or not body.strip():
        return Converted(text, problem=t("没有 developer_instructions：Codex 的 Agent 把设定写在这一项里。"))
    name = _name("codex", data, stem, changes)
    model = _model(data.get("model"), changes)
    features = data.get("features") if isinstance(data.get("features"), dict) else {}
    tools = None
    if features.get("shell_tool") is False:
        tools = [name for name in _EVERYDAY if name != "Bash"]
        changes.append(t("{tool} 里关掉了执行命令（features.shell_tool），在智狐里也不放开：{picked}", tool=tool, picked=", ".join(tools)))
    _dropped(data, {"name", "description", "developer_instructions", "model"}, changes)
    return Converted(_compose(name, data.get("description"), tools, model, "", body), changes)


# ---- Kiro、Amazon Q 的 JSON ------------------------------------------------------


def _kiro_json(text: str, stem: str, path: Path, tool: str) -> Converted:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return Converted(text, problem=t("不是合法的 JSON：第 {line} 行第 {column} 列，{error}", line=exc.lineno, column=exc.colno, error=exc.msg))
    if not isinstance(data, dict):
        return Converted(text, problem=t("最外层应该是一个对象"))
    changes: list[Message] = []
    prompt = data.get("prompt")
    if isinstance(prompt, str) and prompt.startswith("file://"):
        prompt, problem = _prompt_file(path, prompt.removeprefix("file://"))
        if problem is not None:
            return Converted(text, problem=problem)
        changes.append(t("设定是从 {file} 读进来的，导入后写在这个 Agent 的文件里。", file=data["prompt"].removeprefix("file://")))
    if not isinstance(prompt, str) or not prompt.strip():
        return Converted(text, problem=t("没有 prompt：这个 Agent 的设定是空的。"))
    name = _name("kiro", data, stem, changes)
    tools = data.get("tools")
    if isinstance(tools, list):
        picked = _translated([item for item in tools if isinstance(item, str)], "kiro", changes)
    else:
        # 没写、或者写成了认不出来的样子：不知道它原来能用什么，就先什么都不放开（只会更少，不会更多）
        picked = []
        changes.append(t("原文件里的 tools 没写或者认不出来：导入后先不放开任何工具，需要的话导入后再编辑。"))
    model = _model(data.get("model"), changes)
    _never_ask(data, tool, changes)
    _dropped(data, {"name", "description", "prompt", "tools", "model", "$schema", *_NEVER_ASK}, changes)
    return Converted(_compose(name, data.get("description"), picked, model, "", prompt), changes)


def _prompt_file(agent: Path, relative: str) -> tuple[str, Message | None]:
    """file:// 指向的设定文件。只读这个 Agent 文件所在的文件夹里面的，别处的不读（让你自己复制过来）。"""
    folder = agent.parent.resolve()
    target = (folder / relative).resolve() if not os.path.isabs(relative) else Path(relative).resolve()
    if folder not in target.parents:
        return "", t("设定写在别处的文件里（{file}），智狐只读 Agent 文件旁边的文件：请把设定复制进来再导入。", file=relative)
    try:
        info = os.stat(target)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_AGENT_BYTES:
            raise OSError
        return target.read_text(encoding="utf-8").removeprefix("﻿"), None
    except (OSError, UnicodeDecodeError):
        return "", t("读不出设定的文件 {file}。", file=relative)
