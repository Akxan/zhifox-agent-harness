from __future__ import annotations

from pathlib import Path

from .i18n import t
from .permissions_cards import always
from .sandbox import escape_question
from .sandbox import question as command_question
from .tools import shell
from .tools.shell import ShellTool

# ---- 执行命令 ------------------------------------------------------------------


def _review_command(tool_input: dict, tool: object) -> str:
    """执行一条命令怎么问你：命令本身，打开了沙箱的话加一行沙箱的情况（话在 sandbox/session.py 里写），
    之前的命令 cd 到了别处的话再加一行它在哪个目录里运行。要在沙箱之外运行的，换成另一张带警告的卡片，在任何模式下都问。"""
    command = tool_input.get("command")
    facts = tool.look(tool_input) if isinstance(tool, ShellTool) else None
    question: str  # 都是 t() 给的 Message；sandbox/session.py 那两个函数标的是 str
    if facts is None:
        question = t("执行命令：\n\n    $ {command}", command=command)
        # 认不出是哪个 shell（只给了名字）又带着 unsandboxed：宁可当它要出沙箱。认得出、只是没有沙箱这回事的，照常问
        escapes = _escapes(shell.TOOL_NAME, tool_input, tool)
    elif facts.escapes:
        question, escapes = escape_question(facts, command), True
    else:
        question, escapes = command_question(facts, command), False
    here = _command_dir(tool_input, tool)
    if here is not None and isinstance(tool, ShellTool) and here != tool.shell.workspace:
        # 不是工作目录才写：大多数命令就在工作目录里，卡片和以前一样（聊天通道的确认请求另有一行"工作目录"）。
        # 包一层 t()、把原来那句当作填进去的值，换语言时里外两层都能重译（见 i18n.Message.nested）
        question = t("{question}\n\n在这个目录里运行：{path}（之前的命令 cd 到了这里）", question=question, path=here)
    return always(question) if escapes else question


def _command_dir(tool_input: dict, tool: object) -> Path | None:
    """这条命令真正会在哪个目录里运行（Shell.where：记着的目录不在了、沙箱里看不到，当场回到工作目录，见 tools/cwd.py）。
    卡片上写的、判断"只读的命令"时用的，和真正运行时用的是同一个。

    认不出是哪个 Shell（只给了名字，测试和只按名字判断的地方）：交回 None，按工作目录算。那样的调用方手里没有一个会 cd 的 shell，
    真正执行命令的 Agent 一定会把工具本身交过来（见 agent.py 的 _run_one）。"""
    if not isinstance(tool, ShellTool) or tool.shell is None:
        return None
    return tool.shell.where(tool_input.get("unsandboxed") is True)


def _escapes(tool_name: str, tool_input: object, tool: object) -> bool:
    """这次调用是不是"在沙箱之外执行一条命令"。认不出是哪个 shell 的（只给了名字），带着 unsandboxed 就当它是。"""
    if tool_name != shell.TOOL_NAME or not isinstance(tool_input, dict):
        return False
    if isinstance(tool, ShellTool):
        facts = tool.look(tool_input)
        return facts is not None and facts.escapes
    return tool_input.get("unsandboxed") is True
