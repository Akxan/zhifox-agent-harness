"""倒回最后一轮：编辑最后一条消息、重新生成最后一个回答。

两件事做的是同一个动作，"把对话倒回去一点，再跑一遍"，区别只在倒到哪儿：

  重新生成   倒到最后一条用户消息之后。用户消息原样留着（连同当时的时间说明和附件），
             模型这一轮写的、做的（思考、回答、工具调用和结果）全部去掉，然后照原样再请求一次。
  编辑       倒到最后一条用户消息之前。那条消息换成改过的文字（附件留着，也可以去掉几个），当作新的一轮发出去。

为什么只能是最后一轮
--------------------
对话历史有一条规矩：只在末尾追加，前面的内容不改、不删（见 providers/claude.py 开头）。改了中间的某一轮，
它后面的缓存全部作废；Claude 每一块思考的签名都绑着它前面的全部内容，中间一改，后面的思考就通不过 API 的校验。
从末尾去掉一截不一样：留下的每一块，它前面的内容都和当初一字不差。所以只倒最后一轮，前面的一个字都不动。

倒回去的位置从哪儿来
--------------------
每一轮开始时，网页界面把适配层的位置（Backend.mark）记在这一轮的 user 事件上（"mark"），和事件一起存进会话文件。
GPT、Gemini、DeepSeek 的位置是从对话开始数的，被压缩掉的也算在内；会话文件里另外记着压缩掉了多少条
（Conversation.dropped），重新打开、重启之后，记下的位置照样对得上。倒回去之前，先问适配层这个位置上
是不是还留着这一轮的那条用户消息（Backend.turn_at）：

  还在               最常见的情况。重新生成：退到它之后；编辑：退到它之前。
  不在、但位置就是末尾 这一轮在模型答出第一步之前就失败了（或者被拒绝了），循环已经把这条消息撤回了，
                     页面上却还看得到它。重新生成就是"再试一次"：同样的文字、同样的附件（从多媒体库取回原件）再发一次。
  不在、也不是末尾   这一轮已经被压缩进了摘要（见 providers/compaction.py）。倒不回去，按钮是灰的，写着为什么。

没有记位置的那一轮（这个功能之前存的会话、定时任务自己跑的那一轮）也倒不回去。定时任务那一轮发给模型的
指令和页面上显示的不一样（见 webui/scheduler.py 的 instruction），"编辑这条消息"说不清改的是哪一个。

已经做了的事撤销不了
--------------------
倒回去的只是对话。这一轮里执行过的命令、改过的文件、调用过的 MCP 工具、在浏览器里点过的按钮，都已经发生了。
所以这一轮做过可能改动东西的事（effects），倒回去之前在原地问一次，把这些事列出来；只读过东西、或者
没有调用工具的，不用问。你拒绝了的没有执行，不算；智狐自己的待办、文件、记忆、定时任务工具出错了的，什么都没改，也不算。
认不出来的工具（以后新加的，比如操作整个桌面的）一律当作"可能改动了什么"：宁可多问一次。

跟着倒回去的只有一样不是对话的东西：命令的当前目录（见 tools/cwd.py）。它回到这一轮开始时的那个目录
（Shell.current.rewind_turn），重新跑的那一轮和原来那一轮从同一个地方开始，和留下的历史对得上。

花掉的钱
--------
已经花掉的钱就是花掉了：账本（ledger.py）一笔不少地留着；页面信息栏里这段对话的用量和费用也照样算着，
不因为倒回去就减掉。上下文的大小等下一次请求回来时自然更新。
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from dataclasses import dataclass, replace

from . import documents
from .attachments import Attachment, AttachmentError, from_saved, parse, sniff_image
from .config import MAX_REWIND_EFFECTS
from .i18n import t
from .mcp.bridge import PREFIX as MCP_PREFIX
from .media import OFFICE_TYPES, MediaError, MediaStore, content_of, entries_of
from .tools import agent as agent_tool
from .tools import files, skill
from .tools import tasks as task_tools
from .tools.browser import TOOLSET as BROWSER
from .tools.computer import TOOLSET as COMPUTER

# ---- 哪一轮 -------------------------------------------------------------------


@dataclass(frozen=True)
class Turn:
    """最后一轮：它的 user 事件，和这一轮里发生的所有事件。"""

    index: int  # 它的 user 事件是会话事件里的第几个
    mark: int | None  # 这一轮开始时适配层的位置（见文件开头）；没有记的是 None
    # 它的 user 事件在页面的事件日志里的编号（存进文件的事件没有编号，是 None）。页面发请求时带着它，
    # 这边对一下：另一个标签页刚倒回过（倒回去以后整段重放，编号全换了新的），或者已经换了一段会话，就不做
    id: int | None
    text: str  # 用户写的话（页面上显示的样子）
    attachments: list[dict]  # 附件的概要，和页面重放时一样用 from_saved 筛过
    events: list[dict]  # 从它的 user 事件开始，到最后


def last_turn(events: Sequence[dict], start: int = 0) -> Turn | None:
    """events 里的最后一轮。一条用户消息都没有的话返回 None。

    start：events 里第一个事件在整段会话里是第几个（只给了末尾一截的时候用，见 WebUI.last_turn_events）。
    """
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if isinstance(event, dict) and event.get("type") == "user":
            text = given if isinstance(given := event.get("text"), str) else ""
            number = given_id if isinstance(given_id := event.get("id"), int) else None
            return Turn(start + index, mark_of(event), number, text, from_saved(event.get("attachments")), list(events[index:]))
    return None


def mark_of(event: dict) -> int | None:
    """一个 user 事件上记着的位置（这一轮开始时的 Backend.mark）；没记、或者文件被改坏了的是 None。
    倒回最后一轮和从某一轮分叉（fork.py）都按它认。"""
    mark = event.get("mark")
    return mark if isinstance(mark, int) and not isinstance(mark, bool) and mark >= 0 else None


# ---- 这一轮做过什么 -------------------------------------------------------------

# 浏览器里只是"看"的动作：截图、读页面、找元素、滚动、等一会儿、换标签页、去一个网址。
# 去一个网址也算看：它只是打开一个页面（GET），网址本身带走的东西，问不问你在打开之前已经问过了（见 permissions.py）。
# 别的（点击、输入、按键、拖动、填表单）都可能把东西送出去，算"改动了什么"
_BROWSER_LOOKS = {
    "navigate", "screenshot", "zoom", "hover", "mouse_move", "scroll", "scroll_to", "wait",
    "read_page", "find", "get_page_text", "new_tab", "list_tabs", "switch_tab", "close_tab",
}  # fmt: skip
# 别家的模型用的浏览器"替身"工具（见 tools/browser.py 的 PORTABLE）里只是看的那些
_BROWSER_STANDINS = {"browser_navigate", "browser_read", "browser_find", "browser_text", "browser_scroll", "browser_wait", "browser_screenshot", "browser_tabs"}
# 操作整台电脑（tools/computer.py）里只是"看"的：截图、放大看一块、问鼠标在哪儿、等一会儿。别的（点、拖、移动鼠标、滚动、打字、按键）
# 都可能在别的程序里做了什么，算"改动了什么"：桌面上的程序比浏览器里的网页难猜得多，所以比浏览器那边严，移动鼠标和滚动也算
_COMPUTER_LOOKS = {"screenshot", "zoom", "cursor_position", "wait"}
_COMPUTER_STANDINS = {"computer_screenshot", "computer_zoom", "computer_wait"}  # 别家的模型用的替身工具里只是看的
# 页面在确认里单独写一句"这些撤不回来"的两类：操作过整台电脑、在浏览器里点过输入过（见 operated）
_OPERATED = ((COMPUTER, (COMPUTER + ".", COMPUTER + "_")), (BROWSER, (BROWSER + ".", BROWSER + "_")))
# 只是读的工具：读 Skill、列出待办（tools/todo.py）、列出定时任务。交给子 Agent（agent 工具）本身也不算：它自己调用的工具另外看
_READ_ONLY = {skill.TOOL_NAME, "todo_list", task_tools.LIST, agent_tool.TOOL_NAME}
# 智狐自己的这几样工具"要么全做、要么不做"：先把参数查好、找到要改的那一处，才一口气写进去（写文件都是先写临时文件
# 再一步换上去）。所以它们的一次调用出错了，就是什么都没改，不算。执行命令、MCP、浏览器不一样：做到一半出了错，前一半已经做了
_ALL_OR_NOTHING = {files.TOOL_NAME, "memory", "todo_add", "todo_update", "todo_delete", task_tools.ADD, task_tools.DELETE}


def changes(name: str, tool_input: object) -> bool:
    """一次工具调用会不会改动了什么（文件、命令、外面的服务……）。name 是界面上显示的名字（工具包的成员写成 browser.click）。

    认不出来的一律算会：新加的工具没有登记在这里，宁可多问一次。
    """
    if name in _READ_ONLY or name in _BROWSER_STANDINS or name in _COMPUTER_STANDINS:
        return False
    if name.startswith(MCP_PREFIX):
        return True  # 别人写的程序，它做了什么我们看不到（和 permissions.py 一样，不信它说的"只读"）
    if name.startswith(BROWSER + "."):
        return name.removeprefix(BROWSER + ".") not in _BROWSER_LOOKS
    if name.startswith(COMPUTER + "."):
        return name.removeprefix(COMPUTER + ".") not in _COMPUTER_LOOKS
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if name in (files.TOOL_NAME, "memory"):  # 文件工具和记忆工具：只有 view 是读
        return command != "view"
    return True  # 执行命令、改待办、加删定时任务，以及认不出来的


def effects(events: Sequence[dict]) -> list[str]:
    """一轮里做过的、可能改动了什么的事，每件一行（就是页面上工具卡片的那一行摘要）。没有就是空的。"""
    return [summary for _, summary in _executed(events)]


def operated(events: Sequence[dict]) -> list[str]:
    """这些事里有没有操作过整台电脑（"computer"）、在浏览器里点过输入过（"browser"）。确认里要为它们单独说一句：
    在别的程序里、在网页上已经做了的（提交了表单、发出了消息），倒回对话也撤不回来。"""
    names = [name for name, _ in _executed(events)]
    return [kind for kind, prefixes in _OPERATED if any(name.startswith(prefixes) for name in names)]


def _executed(events: Sequence[dict]) -> list[tuple[str, str]]:
    """一轮里真的执行了、可能改动了什么的那些调用：（工具的名字，摘要）。

    你拒绝了的、停下时还没做的不算：它们没有执行。确认请求的结果有两种写法：刚发生的是紧跟着的一个
    confirm_done 事件，存进会话文件以后合进了 confirm 事件的 outcome（见 WebUI.export_events），两种都认。
    "要么全做、要么不做"的那几样（_ALL_OR_NOTHING）出错了的也不算。
    子 Agent 的工具调用（subagent 事件里的 tool_call、tool_result）也算：它是替主助手做的。
    """
    done: list[tuple[str, str]] = []
    pending: tuple[str, bool] | None = None  # 正在等结果的那一次调用：（名字，会不会改动东西）
    asked: dict[int, int] = {}  # 确认请求的编号 → 它在 done 里对应的那一项（还没有答复的）
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        step = event.get("step") if kind == "subagent" else None
        if kind == "tool_call" or step == "tool_call":
            name = given_name if isinstance(given_name := event.get("name"), str) else ""
            summary = given_summary if isinstance(given_summary := event.get("summary"), str) else name
            if changes(name, event.get("input")):
                done.append((name, summary or "?"))
                pending = (name, True)
            else:
                pending = (name, False)
        elif kind == "tool_result" or step == "tool_result":
            if pending is not None and pending[1] and event.get("is_error") is True and pending[0] in _ALL_OR_NOTHING and done:
                done.pop()  # 出错了就是什么都没改
            pending = None
        elif kind == "confirm" and pending is not None and pending[1] and done:
            outcome = event.get("outcome")
            if outcome is None:
                if isinstance(event.get("id"), int):
                    asked[event["id"]] = len(done) - 1
            elif outcome != "allowed":
                done.pop()  # 拒绝了、或者停下了：没有执行
                pending = None
        elif kind == "confirm_done":
            position = asked.pop(confirm, None) if isinstance(confirm := event.get("confirm"), int) else None
            if position is not None and not event.get("allowed") and position == len(done) - 1:
                done.pop()
                pending = None
    return done


def listed(found: list[str]) -> tuple[list[str], int]:
    """要列给你看的那几行（连同"还有几项"那一句，一共最多 MAX_REWIND_EFFECTS 行），和没列出来的还有几项。
    页面拿到的是这两样：最后那句由页面自己写，换语言时能在原地跟着换。"""
    if len(found) <= MAX_REWIND_EFFECTS:
        return found, 0
    return found[: MAX_REWIND_EFFECTS - 1], len(found) - MAX_REWIND_EFFECTS + 1


def shown_effects(found: list[str]) -> list[str]:
    """终端里显示的那几行：listed 列出来的，再多的合成一句"还有几项"。"""
    lines, more = listed(found)
    return [*lines, t("……还有 {n} 项", n=more)] if more else lines


# ---- 附件 ---------------------------------------------------------------------


def attachments_of(turn: Turn, keep: Sequence[int] | None, store: MediaStore, messages: list, started_at: float) -> list[Attachment]:
    """把这一轮的附件重新做出来（编辑、再试一次时要原样再发一次）。keep：留下第几个（None 是全部）。

    原件优先：发消息时每个附件都另存了一份（见 media.MediaStore.keep），按概要里的编号取回来，再走一遍和浏览器送来时
    一样的检查（attachments.parse）。没有原件的（存的时候磁盘出了问题），这一轮的消息还在历史里的话，从历史里取
    （和多媒体库下载旧附件是同一个办法，见 media.content_of）。都取不到就抛出 MediaError，说明是哪一个。
    """
    wanted = range(len(turn.attachments)) if keep is None else keep
    if any(not 0 <= index < len(turn.attachments) for index in wanted):
        raise MediaError(t("参数不正确"))
    if not wanted:
        return []
    entries = entries_of([{"type": "user", "attachments": turn.attachments}], messages, started_at)
    from_history = None  # 原件不在时，按"没有原件"重新对一遍历史（用到时才算）
    rebuilt = []
    for index in wanted:
        summary = turn.attachments[index]
        try:
            try:
                content, content_type, _ = content_of(entries[index], store, lambda: messages)
            except MediaError:
                if not summary.get("sha256"):
                    raise
                if from_history is None:
                    bare = [{key: value for key, value in item.items() if key != "sha256"} for item in turn.attachments]
                    from_history = entries_of([{"type": "user", "attachments": bare}], messages, started_at)
                content, content_type, _ = content_of(from_history[index], store, lambda: messages)
            rebuilt.append(_attachment(summary, content, content_type))
        except (MediaError, AttachmentError, UnicodeDecodeError) as exc:
            raise MediaError(t("附件“{name}”取不回来了（{error}），没法原样再发一次。可以去掉它，或者重新附上文件再发。", name=summary["name"], error=exc)) from exc
    return rebuilt


def _attachment(summary: dict, content: bytes, content_type: str) -> Attachment:
    """原件（或者从历史里取回的内容）→ 附件。种类按概要里记的，不按内容去猜：一个文本文件的开头碰巧写着 %PDF- 也还是文本。"""
    name, kind = summary["name"], summary["kind"]
    data = base64.b64encode(content).decode()
    if kind == "image":
        raw = {"kind": "image", "name": name, "media_type": sniff_image(content), "data": data, "thumb": summary.get("thumb"), "preview": summary.get("preview")}
    elif kind == "pdf":
        raw = {"kind": "pdf", "name": name, "media_type": content_type, "data": data}
    elif content_type in OFFICE_TYPES.values():
        raw = {"kind": "document", "name": name, "data": data}  # Word、Excel、PowerPoint 的原件：重新取出文字
    else:
        raw = {"kind": "text", "name": name, "text": content.decode("utf-8")}
    rebuilt = parse([raw])[0]
    if kind == "text" and raw["kind"] == "text" and _office_name(name):
        rebuilt = replace(rebuilt, note=documents.NOTE)  # 原件不在了、只剩当时取出的文字：说明还是那一句
    return replace(rebuilt, sha256=summary.get("sha256", ""))


def _office_name(name: str) -> bool:
    stem, dot, suffix = name.rpartition(".")
    return bool(dot and stem) and suffix.lower() in OFFICE_TYPES
