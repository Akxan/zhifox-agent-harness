"""Agent Skills 规范：一个 Skill 合不合格。

规范在 https://agentskills.io/specification，Anthropic 的 Claude、Claude Code 和越来越多别家的工具都用它。
一个 Skill 就是一个文件夹，里面至少有一个 SKILL.md：开头是 frontmatter（见 frontmatter.py），下面是正文。

  字段            必填   要求
  name            是     1 到 64 个字符；只能有小写字母、数字和连字符；不能以连字符开头或结尾，也不能连着写两个；
                         和所在的文件夹同名
  description     是     1 到 1024 个字符：这个 Skill 做什么、什么时候该用它
  license         否     许可证的名字，或者附带的许可证文件
  compatibility   否     1 到 500 个字符：需要什么环境（哪个产品、要装什么软件、要不要联网）
  metadata        否     "名字 → 文字"的表，放规范之外的信息（作者、版本……）
  allowed-tools   否     预先允许它用的工具，用空格隔开（实验性的）

另外认两个 Claude Code 的字段，因为它们改变"什么时候该用"：
  when_to_use                补充说明什么时候用，接在 description 后面一起给模型看
  disable-model-invocation   true 表示"只在用户点名时才用"，不主动列给模型

allowed-tools 在 Claude Code 里的意思是"用这些工具时不用再问用户"。智狐不这样做：
Skill 是从别处装进来的文字，不能替你决定放开什么。它想运行的命令照样要你确认（见 permissions.py）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import pathnames
from ..i18n import Message, t
from .frontmatter import (
    Frontmatter,
    FrontmatterError,
    parse,
    replace_fields,
    scalar,
    split_lines,
)

MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_COMPATIBILITY = 500
MAX_LISTING = 1536  # Claude Code 给模型看的说明（description 加 when_to_use）最多这么长，再长的截断

NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")

STANDARD = frozenset({"name", "description", "license", "compatibility", "metadata", "allowed-tools"})
CLAUDE_CODE = frozenset({"when_to_use", "disable-model-invocation"})
KNOWN = STANDARD | CLAUDE_CODE
_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}


@dataclass
class Meta:
    """从 SKILL.md 读出来的、我们要用的信息。读不出来的项留空。"""

    name: str = ""
    description: str = ""
    license: str = ""
    compatibility: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)
    when_to_use: str = ""
    manual_only: bool = False  # disable-model-invocation: true
    extra: list[str] = field(default_factory=list)  # 规范之外、我们也不用的字段（原样留在文件里）

    def listing(self) -> str:
        """给模型看的那一句"做什么、什么时候用"。"""
        text = " ".join(part for part in (self.description, self.when_to_use) if part)
        return text if len(text) <= MAX_LISTING else text[: MAX_LISTING - 1] + "…"


def name_problem(name: object) -> Message | None:
    """一个 Skill 的名字合不合规范。合格返回 None，否则返回说明。"""
    if not isinstance(name, str) or not name:
        return t("name 不能为空。")
    if len(name) > MAX_NAME:
        return t("name 最多 {n} 个字符，现在是 {count} 个。", n=MAX_NAME, count=len(name))
    if not NAME.fullmatch(name):
        return t("name 只能有小写字母、数字和连字符（-），不能以连字符开头或结尾，也不能连着写两个连字符。")
    if pathnames.reserved(name):  # 名字就是文件夹名：Windows 上 con、nul、com1 在哪儿都是设备，建不出这个文件夹
        return t("{name} 在 Windows 上是设备的名字（CON、NUL、COM1……），不能用作名字。", name=name)
    return None


def check(text: str, folder: str | None) -> tuple[Meta, list[Message]]:
    """检查一份 SKILL.md。folder 是它所在文件夹的名字（规范要求 name 和它一样；None 表示不查这一条）。

    返回（读出来的信息，问题列表）。问题列表是空的才算合格。读不出 frontmatter 时，信息是空的。
    """
    try:
        front = parse(text, KNOWN)
    except FrontmatterError as exc:
        return Meta(), [exc.args[0]]
    return read(front, folder)


def read(front: Frontmatter, folder: str | None) -> tuple[Meta, list[Message]]:
    fields, problems = front.fields, []
    meta = Meta(extra=sorted(key for key in fields if key not in KNOWN))

    def text_field(key: str, required: bool, limit: int | None) -> str:
        value = fields.get(key)
        if value is None:
            if required:
                problems.append(t("缺少 {key}。", key=key))
            return ""
        if not isinstance(value, str):
            problems.append(t("{key} 应该是一段文字。", key=key))
            return ""
        value = value.strip()
        if not value:
            problems.append(t("{key} 不能为空。", key=key))
        elif limit and len(value) > limit:
            problems.append(t("{key} 最多 {n} 个字符，现在是 {count} 个。", key=key, n=limit, count=len(value)))
        return value

    meta.name = text_field("name", True, None)
    if meta.name:
        problem = name_problem(meta.name)
        if problem:
            problems.append(problem)
        elif folder is not None and meta.name != folder:
            problems.append(t("name 是 {name}，但文件夹叫 {folder}。规范要求两者一样。", name=meta.name, folder=folder))
    meta.description = text_field("description", True, MAX_DESCRIPTION)
    meta.license = text_field("license", False, None)
    meta.compatibility = text_field("compatibility", False, MAX_COMPATIBILITY)
    meta.when_to_use = text_field("when_to_use", False, None)

    metadata = fields.get("metadata")
    if isinstance(metadata, dict):
        meta.metadata = dict(metadata)
    elif metadata is not None:
        problems.append(t("metadata 应该是一张“名字: 文字”的表。"))

    tools = fields.get("allowed-tools")
    if isinstance(tools, str):
        meta.allowed_tools = tools.split()  # 规范的写法：用空格隔开
    elif isinstance(tools, list):
        meta.allowed_tools = [item for item in tools if item]  # Claude Code 也接受 YAML 的列表
    elif tools is not None:
        problems.append(t("allowed-tools 应该是用空格隔开的一串工具名。"))

    manual = fields.get("disable-model-invocation")
    if isinstance(manual, str) and manual.lower() in _TRUE | _FALSE:
        meta.manual_only = manual.lower() in _TRUE
    elif manual is not None:
        problems.append(t("disable-model-invocation 只能是 true 或 false。"))
    return meta, problems


def new_skill(name: str, description: str, body: str) -> str:
    """按表单里填的三样东西写出一份 SKILL.md。"""
    body = body.strip("\n")
    return f"---\nname: {scalar(name)}\ndescription: {scalar(description)}\n---\n\n{body}\n" if body else (
        f"---\nname: {scalar(name)}\ndescription: {scalar(description)}\n---\n"
    )


def with_name(text: str, name: str) -> str:
    """把 SKILL.md 里的 name 换成 name（没有的话加上），别的内容一个字都不动。

    装进来的 Skill 改名时用：规范要求 name 和文件夹同名，文件夹改了，name 也要跟着改。
    """
    front = parse(text)  # 调用的人已经检查过，这里读得出来
    line = f"name: {scalar(name)}"
    if "name" in front.spans:
        return replace_fields(text, {"name": line})
    # 原来没写 name：加在最前面（第一行 --- 的下面），和规范里的例子一样排在 description 之前
    lines = split_lines(text)
    lines.insert(1, line)
    return ("\r\n" if "\r\n" in text else "\n").join(lines)
