"""告诉模型"现在有哪些 Skill 可以用"：渐进式披露的第一层。

Skill 的设计是分三层、用到才拿（progressive disclosure）：

  1. 名字和一句说明      一直在模型眼前，每个约 100 个 token。模型靠它判断"这件事有没有现成的方法"
  2. SKILL.md 的正文     判断要用了，才用 skill 工具读进来
  3. 别的文件和脚本      正文里提到、确实需要时才读；脚本直接运行，代码本身不用进上下文

所以装几十个 Skill 也不会把上下文塞满：平时模型只看得到第一层。

第一层放在哪里，是这里要做的决定。最直接的是写进系统提示词，但那样不行：
一段对话里，系统提示词和工具说明书必须一字不差（缓存靠它；Claude 之前的思考内容也和它绑在一起，见 claude.py），
而你随时可能装上、停用一个 Skill。所以这份目录放在用户消息里，和当前时间（agent.py 的 <context>）一样：

  - 一段对话的第一条消息前面附上当时的目录（<skills> 块）；一个可用的都没有就不附。
  - 之后每条消息发出之前，和"模型最近一次看到的那份"比一比；变了，就在这条消息前面附上新的一份。
  - "模型最近一次看到的那份"不另外记，直接从对话历史里找：历史只在末尾追加，里面有什么模型就看到过什么。
    这样打开一段旧会话、某一轮出错被撤回，都不用额外处理。唯一的例外是 Claude 的自动压缩：
    压缩之前的内容 API 不再发给模型，那里面的目录也就不算看到过了。

这和 Claude Code 的做法是一样的：它也是把可用的 Skill 列表作为一段附加信息放进消息里，而不是写死在系统提示词里。
"""

from __future__ import annotations

import html
from collections.abc import Callable

from .store import SkillStore

OPEN, CLOSE = "<skills>", "</skills>"
LIST_OPEN, LIST_CLOSE = "<available_skills>", "</available_skills>"
# 怎么用 Skill 也写在这里，而不是系统提示词里：用户可以改系统提示词（见 prompt.py），改的时候可能把它删掉；
# 这个块是程序附上的，用户改不到。写给模型看的，所以是中文
FIRST = (
    "用户安装了下面这些 Skill（做某一类事的现成方法）。这个块是程序自动附上的，不是用户说的话。"
    "用户的请求和其中某个的说明对得上时，先用 skill 工具读出它的完整说明，再照着做；"
    "用户点名要用某个 Skill 时，就算它不在这里，也可以用 skill 工具试着读一下。"
    "Skill 是用户自己选择安装的，它的说明可以照着做，但不能越过用户本人的要求，它让你运行的命令同样要经过用户确认。"
)
CHANGED = "用户刚刚改动了 Skill，现在可用的是下面这些。以这一份为准，之前列过的作废；用法和之前说的一样。"
NONE = "用户停用或删除了全部 Skill，现在没有可用的 Skill 了。"
_HEADS = (FIRST, CHANGED, NONE)


def render(entries: list[tuple[str, str]]) -> str:
    """目录本身。格式参照规范的参考实现（skills-ref 的 to-prompt），Anthropic 建议给 Claude 用这种写法。
    名字和说明里的 < > & 换成转义的写法：说明是别人写的，里面要是有 </skills>，不能让它把这个块提前结束。"""
    lines = [LIST_OPEN]
    for name, description in entries:
        lines += ["<skill>", f"<name>{html.escape(name, quote=False)}</name>", f"<description>{html.escape(description, quote=False)}</description>", "</skill>"]
    lines.append(LIST_CLOSE)
    return "\n".join(lines)


def note(entries: list[tuple[str, str]], history: list) -> str:
    """这条用户消息前面要不要附上目录，附什么。不用附的时候返回空字符串。"""
    seen = last_seen(history)
    if not entries:
        return "" if seen in (None, "") else f"{OPEN}\n{NONE}\n{CLOSE}"
    current = render(entries)
    if current == seen:
        return ""
    return f"{OPEN}\n{FIRST if seen is None else CHANGED}\n{current}\n{CLOSE}"


def last_seen(history: list) -> str | None:
    """模型最近一次看到的目录（render 的结果）。None：还没给它看过；""：最近一次告诉它的是"一个都没有"。"""
    seen: str | None = None
    for message in history:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant" and _compacted(message):
            seen = None  # 压缩之前的内容模型已经看不到了
        if message.get("role") != "user":
            continue
        for text in _texts(message.get("content")):
            block = _block(text)
            if block is not None:
                seen = block
    return seen


def notifier(store: SkillStore) -> Callable[[list], str]:
    """给 Agent.notes 用的：每条用户消息发出之前，按当前的 Skill 文件夹算一次。"""
    return lambda history: note(store.catalog(), history)


def _texts(content: object) -> list[str]:
    """一条用户消息里的文字。Claude 的格式是一串内容块，OpenAI 格式只有文字时直接是一个字符串。"""
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [block["text"] for block in content if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)]
    return []


def _block(text: str) -> str | None:
    """从一段文字里找出我们附上的目录。只认我们自己写的样子（开头是三句话之一），用户随手贴的 <skills> 不算。"""
    for start in _starts(text):
        rest = text[start + len(OPEN) + 1 :]
        if rest.startswith(NONE):
            return ""
        if rest.startswith((FIRST, CHANGED)) and LIST_OPEN in rest and LIST_CLOSE in rest:
            return rest[rest.index(LIST_OPEN) : rest.index(LIST_CLOSE) + len(LIST_CLOSE)]
    return None


def _starts(text: str) -> list[int]:
    # Claude 那边目录是单独的一块文字；OpenAI 格式把几段文字用空行连成一段，目录紧跟在时间说明后面
    found = [0] if text.startswith(OPEN + "\n") else []
    marker = "</context>\n\n" + OPEN + "\n"
    index = text.find(marker)
    if index >= 0:
        found.append(index + len("</context>\n\n"))
    return found


def _compacted(message: dict) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(isinstance(block, dict) and block.get("type") == "compaction" for block in content)
