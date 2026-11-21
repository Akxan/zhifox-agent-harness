"""一个自定义 Agent 的文件：格式、每一项的要求、怎么检查、怎么从表单写出来。

格式和 Claude Code 的 subagent 文件一样（https://code.claude.com/docs/en/sub-agents）：开头是 frontmatter
（夹在两行 --- 之间的几行 YAML，用的是 Skill 的那个解析器，见 skills/frontmatter.py），下面的正文是写给这个 Agent 的设定。

    ---
    name: 资料研究员
    description: 查资料、读网页，交回一份带出处的摘要。需要查很多资料、只要结论的时候用。
    tools: Read, WebSearch, WebFetch
    model: inherit
    color: blue
    ---
    你是一个细心的研究员……（正文：它的设定和做事的方法）

  字段          必填   要求
  name          是     1 到 64 个字符：字母（中文也算）、数字、连字符和下划线，以字母或数字开头。也是文件名（name.md）
  description   是     1 到 1024 个字符：它擅长什么、什么时候该把事情交给它。主助手靠这一句决定交不交给它
  tools         否     它能用哪些工具（见 tools.py）。不写：和叫它的那一方一样多；写了：只能用这几个，而且永远不会比叫它的那一方多
  model         否     用哪个模型："提供方/模型"（anthropic/claude-sonnet-5-5、deepseek/deepseek-flash），
                       或者 inherit（不写也一样）：和叫它的那一方用同一个。也认 Claude Code 的 opus、sonnet
  color         否     在界面上的颜色：red、orange、yellow、green、cyan、blue、purple、pink 之一（和 Claude Code 一样的八种）
  正文          是     不能是空的

别的字段（Claude Code 的 permissionMode、hooks……）智狐用不上，原样留在文件里，界面上列出来告诉你"智狐不用"。
tools 里写了智狐没有的工具名，不算错（从 Claude Code 导入的文件常有），只提醒一句，那几个会被忽略。

同一个文件有两种用法（区别见 __init__.py）：主助手把任务交给它（子 Agent），或者你用它开始一段新对话（角色）。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .. import pathnames
from ..i18n import Message, t
from ..providers import CLAUDE_MODELS, PROVIDERS
from ..skills.frontmatter import (
    Frontmatter,
    FrontmatterError,
    parse,
    replace_fields,
    scalar,
    split_lines,
)
from . import tools as agent_tools

SUFFIX = ".md"
MAX_NAME = 64
MAX_DESCRIPTION = 1024
KNOWN = frozenset({"name", "description", "tools", "model", "color"})
COLORS = ("red", "orange", "yellow", "green", "cyan", "blue", "purple", "pink")
INHERIT = "inherit"
# 名字：字母（任何文字都算，所以"资料研究员"可以）、数字、连字符和下划线，以字母或数字开头。
# 它也是文件名，所以不能有空格、斜杠、点这类在路径里有特别意思的字符
NAME = re.compile(r"[^\W_][\w-]*")
# Claude Code 的模型简称 → 智狐里对应的模型。haiku 没有对应的（智狐适配过的 Claude 模型里没有它），按 inherit 处理并提醒
ALIASES = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "fable": "claude-fable-5-2"}
# 只写了模型名、没写提供方时，按开头认出是哪一家
_GUESS = (("claude-", "anthropic"), ("gpt-", "openai"), ("o1", "openai"), ("o3", "openai"), ("o4", "openai"), ("gemini", "gemini"), ("deepseek", "deepseek"))


@dataclass
class Spec:
    """从文件里读出来的、我们要用的信息。读不出来的项留空。"""

    name: str = ""
    description: str = ""
    tools: list[str] | None = None  # 原样写着的工具名（None：没写这一项，和叫它的那一方一样）
    model: str = ""  # 认出来的模型，"提供方/模型"；空的表示 inherit
    model_text: str = ""  # 文件里原样写的那一项（显示用，表单里也靠它判断你改没改）
    color: str = ""
    body: str = ""
    extra: list[str] = field(default_factory=list)  # 智狐不用的字段（原样留在文件里）
    warnings: list[Message] = field(default_factory=list)  # 不影响使用、但该让你知道的（比如有几个工具名智狐没有）

    @property
    def provider(self) -> str:
        return self.model.partition("/")[0]

    @property
    def model_id(self) -> str:
        return self.model.partition("/")[2]


def normal(name: str) -> str:
    """名字统一成 NFC 的写法：访达给的文件名可能是拆开的写法（é 写成 e 加一个附加符号），看上去一样、字节不一样。"""
    return unicodedata.normalize("NFC", name)


def name_problem(name: object) -> Message | None:
    if not isinstance(name, str) or not name.strip():
        return t("name 不能为空。")
    if len(name) > MAX_NAME:
        return t("name 最多 {n} 个字符，现在是 {count} 个。", n=MAX_NAME, count=len(name))
    if not NAME.fullmatch(name):
        return t("name 只能有字母（中文也可以）、数字、连字符（-）和下划线（_），以字母或数字开头，不能有空格。")
    if pathnames.reserved(name):  # 名字就是文件名：Windows 上 CON.md、nul.md、COM¹.md 都是设备，存进去的东西就没了
        return t("{name} 在 Windows 上是设备的名字（CON、NUL、COM1……），不能用作名字。", name=name)
    return None


def resolve_model(value: str) -> tuple[str, Message | None, Message | None]:
    """model 一项写的东西 → （"提供方/模型"或空的，问题，提醒）。空的表示和叫它的那一方用同一个。"""
    text = value.strip()
    if not text or text == INHERIT:
        return "", None, None
    if text in ALIASES:
        return f"anthropic/{ALIASES[text]}", None, None
    if text == "haiku":
        return "", None, t("智狐没有 haiku 这个模型，这个 Agent 会和叫它的那一方用同一个模型。")
    provider, slash, model = text.partition("/")
    if not slash:
        guess = next((owner for prefix, owner in _GUESS if text.startswith(prefix)), None)
        if guess is None:
            return "", t("model 要写成“提供方/模型”（比如 anthropic/claude-sonnet-5-5），或者写 inherit。"), None
        provider, model = guess, text
    if provider not in PROVIDERS:
        return "", t("model 里的 {provider} 不是智狐认识的模型提供方。可以用：{known}。", provider=provider, known="、".join(PROVIDERS)), None
    if not model.strip() or any(char.isspace() for char in model):
        return "", t("model 里的模型名不对：不能是空的，也不能有空格。"), None
    if provider == "anthropic" and model not in CLAUDE_MODELS:
        return f"{provider}/{model}", None, t("{model} 不在智狐适配过的 Claude 模型里（{known}），请求可能会被拒绝。", model=model, known="、".join(CLAUDE_MODELS))
    return f"{provider}/{model}", None, None


def split_tools(value: object) -> list[str] | None:
    """tools 一项 → 工具名的列表。Claude Code 的写法是用逗号隔开的一行（Read, Grep, Bash），也认 YAML 的列表。"""
    if isinstance(value, str):
        return [item for item in re.split(r"[\s,]+", value) if item]
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return None


def check(text: str, stem: str | None, file: str | None = None) -> tuple[Spec, list[Message]]:
    """检查一个 Agent 文件。stem 是文件名去掉 .md（要求和 name 一样；None 表示不查这一条），file 是报错时怎么称呼它。

    返回（读出来的信息，问题列表）。问题列表是空的才算合格，才能用。读不出 frontmatter 时，信息是空的。
    """
    try:
        front = parse(text, KNOWN, file=file or ((stem or "Agent") + SUFFIX))
    except FrontmatterError as exc:
        return Spec(), [exc.args[0]]
    return read(front, stem)


def read(front: Frontmatter, stem: str | None) -> tuple[Spec, list[Message]]:
    fields, problems = front.fields, []
    spec = Spec(extra=sorted(key for key in fields if key not in KNOWN))

    def text_field(key: str, required: bool) -> str:
        value = fields.get(key)
        if value is None:
            if required:
                problems.append(t("缺少 {key}。", key=key))
            return ""
        if not isinstance(value, str):
            problems.append(t("{key} 应该是一段文字。", key=key))
            return ""
        value = value.strip()
        if required and not value:
            problems.append(t("{key} 不能为空。", key=key))
        return value

    spec.name = normal(text_field("name", True))
    if spec.name:
        problem = name_problem(spec.name)
        if problem:
            problems.append(problem)
        elif stem is not None and spec.name != normal(stem):
            problems.append(t("name 是 {name}，但文件叫 {file}。两者要一样。", name=spec.name, file=stem + SUFFIX))
    spec.description = text_field("description", True)
    if len(spec.description) > MAX_DESCRIPTION:
        problems.append(t("{key} 最多 {n} 个字符，现在是 {count} 个。", key="description", n=MAX_DESCRIPTION, count=len(spec.description)))

    if "tools" in fields:
        tools = split_tools(fields["tools"]) if fields["tools"] is not None else []
        if tools is None:
            problems.append(t("tools 应该是用逗号隔开的一串工具名，比如 Read, WebSearch。"))
        else:
            spec.tools = tools
            unknown = agent_tools.unknown(tools)
            if unknown:
                spec.warnings.append(t("智狐没有这些工具，会忽略它们：{names}", names="、".join(unknown)))

    spec.model_text = text_field("model", False)
    model, problem, warning = resolve_model(spec.model_text)
    spec.model = model
    if problem:
        problems.append(problem)
    if warning:
        spec.warnings.append(warning)

    spec.color = text_field("color", False).lower()
    if spec.color and spec.color not in COLORS:
        problems.append(t("color 只能是这几种之一：{colors}。", colors="、".join(COLORS)))
        spec.color = ""

    spec.body = front.body.strip("\n")
    if not spec.body.strip():
        problems.append(t("正文是空的：在第二行 --- 下面写这个 Agent 的设定和做事的方法。"))
    return spec, problems


# ---- 从表单写出来 ------------------------------------------------------------


@dataclass
class Fields:
    """表单里的几项（页面送来的，已经检查过类型）。tools 为 None 表示"和叫它的那一方一样"。"""

    name: str
    description: str
    tools: list[str] | None
    model: str  # "" 表示 inherit
    color: str
    body: str


def compose(fields: Fields, base: str | None = None) -> str:
    """按表单写出一个 Agent 文件。

    base 是原来的文件：给了的话，只改你在表单里改过的那几项，别的（注释、智狐不用的字段、你写的顺序）原样留着；
    哪一项算"改过"，按意思比，不按写法比（比如原来写的 Grep, Glob 和表单里的"读文件"是一回事，就不去动它）。
    """
    lines = {
        "name": f"name: {scalar(fields.name)}",
        "description": f"description: {scalar(fields.description)}",
        "tools": None if fields.tools is None else "tools: " + (", ".join(fields.tools) if fields.tools else "[]"),
        "model": f"model: {scalar(fields.model)}" if fields.model else None,
        "color": f"color: {fields.color}" if fields.color else None,
    }
    if fields.tools and any(not re.fullmatch(r"[\w.-]+", name) for name in fields.tools):
        lines["tools"] = "tools: [" + ", ".join(scalar(name) for name in fields.tools) + "]"  # 有特殊符号的名字：写成列表，每一项单独加引号
    body = fields.body.strip("\n")
    try:
        front = parse(base) if base is not None else None
    except FrontmatterError:
        front = None
    if front is None:
        head = ["---", *(line for line in lines.values() if line is not None), "---"]
        return "\n".join(head) + "\n\n" + body + "\n"
    old, _ = read(front, None)
    changed: dict[str, str | None] = {}
    if old.name != fields.name or "name" not in front.fields:
        changed["name"] = lines["name"]
    if old.description != fields.description.strip() or "description" not in front.fields:
        changed["description"] = lines["description"]
    if agent_tools.meaning(old.tools) != agent_tools.meaning(fields.tools):
        changed["tools"] = lines["tools"]
    if resolve_model(old.model_text)[0] != resolve_model(fields.model)[0] or (old.model_text and not fields.model and "model" in front.fields and old.model_text != INHERIT):
        changed["model"] = lines["model"]
    if old.color != fields.color:
        changed["color"] = lines["color"]
    text = replace_fields(base, changed) if changed else base
    # 正文整个换成表单里的；frontmatter 后面空一行再接正文
    all_lines = split_lines(text)
    end = next(index for index in range(1, len(all_lines)) if all_lines[index].rstrip() == "---")
    newline = "\r\n" if "\r\n" in text else "\n"
    return newline.join(all_lines[: end + 1]) + newline + newline + body.replace("\n", newline) + newline


def fields_of(spec: Spec) -> dict:
    """表单要显示的样子（发给页面）。工具按意思整理好：哪几样是勾上的、哪些名字智狐不认识（原样留着）。"""
    return {
        "name": spec.name,
        "description": spec.description,
        "tools": agent_tools.form(spec.tools),
        "model": spec.model_text if spec.model_text != INHERIT else "",
        "resolved_model": spec.model,
        "color": spec.color,
        "body": spec.body,
    }
