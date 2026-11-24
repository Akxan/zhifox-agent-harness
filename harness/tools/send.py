"""send_file：把这台电脑上的一个文件发到正在聊天的聊天软件里 —— 第一类工具（说明书我们写，在我们的电脑上执行）。

为什么要它：你在 Telegram 里说"截屏看下你在做啥"，助手能截图（run_command 里的 screencapture）、能看图（文件工具
view 一张图片），却只能回一句"我没法把图片发到 Telegram"。有了它，截图、生成的表格、写好的文档直接出现在聊天里。

它在哪儿有。工具说明书在一段对话里不能变（变了缓存就作废，见 providers/claude.py），所以看的是这段会话从哪儿开始的
（Conversation.channel，开始时定下，之后不变）：
  聊天通道里的会话            有。手里拿着这个聊天的"去处"（channels/outbound.py 的 Outlet），发到那里
  在网页上打开的聊天通道会话   也有，同一份说明书：工具清单和在聊天里时一字不差（缓存照样命中，历史里的调用也对得上），
                              只是没有去处，调用时回答"这段对话现在不在聊天软件里，发不出去"
  网页上的普通对话、定时任务   没有。它们从来不在聊天软件里：带着它，每次请求白白多发一份说明书，模型还可能想去用它
说明书里不写是哪个平台、上限多大（各平台不一样，写进去说明书就不是同一份了）：发不了的时候，结果里会说清楚为什么。

一次调用怎么走（agent.py 的 _run_one）：
  check   先看一眼（facts）：不在聊天里、文件不存在、是保存密钥的文件、太大了、平台不收这种文件……
          明知发不了的直接告诉模型原因，不弹确认卡片让你点
  review  要不要问你（permissions_review.py 的 Permissions._review_send）：每次都问，"全部自动允许"除外。卡片上写着文件名、大小、发到哪里；
          助手自己的数据目录里的文件，卡片上带着提醒
  run     再读一遍文件，和问你时看到的对一对（位置、大小、格式都没变才发），交给去处发出去，等它发完
路径的解析和文件工具一样（FileEditor.resolve：相对路径从工作目录起，.. 和符号链接都展开），保存密钥的文件也是同一份名单。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..attachments import sniff_image
from ..channels.base import OutboundFile
from ..config import CHANNEL_SEND_BYTES
from ..display import file_size
from ..i18n import message_of, zh
from .base import FactsTool, ToolError, check_input, function_tool
from .files import FileEditor

TOOL_NAME = "send_file"
OCTET = "application/octet-stream"
# 没有去处（网页上打开的聊天通道会话）时的回答。这个文件里写给模型的话都用 zh()：模型拿到中文原文，工具卡片上按界面的语言显示
# （见 ../i18n.py 开头；zh() 和界面语言无关，所以可以在这里先写好）
NOT_IN_CHAT = zh("这段对话现在不在聊天软件里，发不出去。它是在聊天通道里开始的，用户现在是在智狐的网页上接着聊：文件就在这台电脑上，告诉他文件的位置就行。")

DESCRIPTION = (
    "把这台电脑上的一个文件发到你和用户正在用的聊天软件里（Telegram、Discord、Slack、飞书、钉钉……），他在手机上直接就能看到。"
    "图片（PNG、JPEG、GIF、WebP）会尽量作为照片显示在聊天里，别的文件作为文件发送。"
    "用户想看截图、图表、生成的文档或表格时用它，不要只告诉他文件在哪儿：他在手机上打不开这台电脑上的路径。"
    "相对路径以工作目录为起点。每次发送前都会请用户确认（他选了“全部自动允许”时除外）；保存密钥的文件不能发。"
    "各个平台对文件的大小和种类有限制，发不了的时候结果里会说明原因和办法（比如先缩小图片、先打包成 zip）。"
    "只有在聊天软件里进行的对话才能用。"
)
PROPERTIES = {
    "path": {"type": "string", "description": "要发送的文件的路径"},
    "caption": {"type": "string", "description": "可选：附在文件下面的一句说明，可以用 Markdown"},
}


class Outlet(Protocol):
    """文件发到哪儿去（一个聊天）。真的那个在 channels/outbound.py。"""

    label: str  # 平台的名字（中文原文）
    person: str  # 聊天的另一头：主人的名字

    def plan(self, name: str, size: int, media_type: str) -> tuple[str, str]: ...  # （photo 或 document，发不了的原因）

    def send(self, outgoing: object) -> None: ...  # 发不出去抛出 ToolError（写给模型看的中文）


@dataclass(frozen=True)
class SendFacts:
    """一次调用要发的是什么（问你之前看一眼，permissions_review.py 的 _review_send 照着它写确认卡片）。"""

    path: Path | None  # 展开以后的路径；参数不对时是 None
    shown: str = ""  # 给人看的路径（工作目录里的写相对路径）
    size: int = 0
    media_type: str = OCTET  # 按文件开头的几个字节认出来的图片格式；不是图片的是 application/octet-stream
    kind: str = ""  # photo（当照片发）或 document（当文件发）
    caption: str = ""
    label: str = ""  # 发到哪个平台（中文原文）
    person: str = ""  # 发给谁
    refusal: str = ""  # 不会去发的原因（写给模型看的）；空的表示可以发


class SendTool(FactsTool):
    """permissions.py 靠这个类认出它（和浏览器、电脑的工具一样）。"""


def make_send_tool(editor: FileEditor, outlet: Outlet | None) -> SendTool:
    """outlet 是这个聊天（聊天通道里的一轮）；None 表示这段对话现在不在聊天软件里（网页上打开的聊天通道会话）。"""
    made = function_tool(TOOL_NAME, DESCRIPTION, PROPERTIES, ["path"], lambda **_: "")  # 只借它的说明书（格式和别的自定义工具一样）
    schema = made.spec["input_schema"]

    def facts(tool_input: dict) -> SendFacts:
        if outlet is None:
            return SendFacts(None, refusal=NOT_IN_CHAT)
        try:
            args = check_input(schema, tool_input)
            path = editor.resolve(args.get("path"))
        except ToolError as exc:
            return SendFacts(None, refusal=message_of(exc))
        caption = str(args.get("caption") or "").strip()
        label, person = outlet.label, outlet.person
        shown = editor.display(path)
        problem = _unreadable(editor, path)
        if problem:
            return SendFacts(path, shown, caption=caption, label=label, person=person, refusal=problem)
        try:
            size = path.stat().st_size
            with path.open("rb") as file:
                media_type = sniff_image(file.read(16)) or OCTET
        except OSError as exc:
            return SendFacts(path, shown, caption=caption, label=label, person=person, refusal=zh("读不了 {path}：{error}", path=path, error=exc.strerror or exc))
        kind, problem = outlet.plan(path.name, size, media_type)
        return SendFacts(path, shown, size, media_type, kind, caption, label, person, problem)

    def check(tool_input: dict) -> str | None:
        return tool.look(tool_input).refusal or None

    def run(tool_input: dict) -> str:
        found: SendFacts = tool.look(tool_input)
        if found.refusal or found.path is None or outlet is None:
            raise ToolError(found.refusal or NOT_IN_CHAT)
        data = _read_again(editor, tool_input, found)
        outlet.send(OutboundFile(found.path.name, data, found.media_type, found.kind, found.caption))
        # 两种各写一整句（而不是把"文件"填进去）：翻译表的键是中文原文，"文件"在表里已经是标签页的名字，同一句不能有两个意思
        values = {"name": found.path.name, "size": file_size(found.size), "platform": found.label}
        if found.kind == "photo":
            return zh("已经把 {name}（图片，作为照片显示在聊天里，{size}）发到了 {platform} 的聊天里，用户现在就能看到它。", **values)
        return zh("已经把 {name}（文件，{size}）发到了 {platform} 的聊天里，用户现在就能看到它。", **values)

    tool = SendTool(spec=made.spec, run=run, portable=made.portable, check=check, facts=facts)
    return tool


def _unreadable(editor: FileEditor, path: Path) -> str:
    """这个路径为什么不能发（写给模型看的）；能发的话是空字符串。"""
    if editor.is_protected(path):
        return zh("这个文件保存着密钥，不能发出去。")
    if not path.exists():
        return zh("{path} 不存在。", path=path)
    if path.is_dir():
        return zh("{path} 是一个文件夹。要发整个文件夹，可以先打包成 zip（比如 zip -r 文件夹.zip 文件夹），再发这个 zip。", path=path)
    if not path.is_file():
        return zh("{path} 不是一个普通的文件。", path=path)
    return ""


def _read_again(editor: FileEditor, tool_input: dict, found: SendFacts) -> bytes:
    """动手之前再读一遍：问你的时候看到的是哪个文件、多大、什么格式，发出去的就得是它。

    在你点"允许"和真正发出去之间，文件可能被别的程序改了、换了（符号链接改指到了别处）：那就不发，让模型重新调用一次，
    你会看到新的样子再决定。"""
    try:
        path = editor.resolve(tool_input.get("path"))
    except ToolError as exc:
        raise ToolError(message_of(exc)) from None
    if path != found.path or editor.is_protected(path):
        raise ToolError(zh("这个路径指向的文件在确认之后换了，没有发。请重新调用一次。"))
    try:
        with path.open("rb") as file:
            data = file.read(CHANNEL_SEND_BYTES + 1)
    except OSError as exc:
        raise ToolError(zh("读不了 {path}：{error}", path=path, error=exc.strerror or exc)) from None
    if len(data) != found.size or (sniff_image(data[:16]) or OCTET) != found.media_type:
        raise ToolError(zh("文件在确认之后变了（大小或者格式和确认时不一样），没有发。请重新调用一次，让用户看到它现在的样子。"))
    return data
