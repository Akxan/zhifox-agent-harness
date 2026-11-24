"""命令的当前目录：run_command 记着下一条命令从哪个目录开始，上一条命令里的 cd 会保留到下一条。

为什么要记
----------
以前每条命令都在工作目录下、全新的 shell 里运行，上一条命令里的 cd 不算数。模型常常忘了这一点：上一条刚 cd 进了 sub，
下一条直接写 make，结果在工作目录里跑了。Claude Code 的 Bash 工具是记着目录的，模型也习惯了这样。
所以每个 Shell（你的每段对话、每次定时任务、每个聊天、每个子 Agent 各有一个，见 tools/shell.py）带着一个 CurrentDir：
一开始是工作目录，每条命令结束时停在哪个目录，下一条就从那里开始。

只记目录，不记别的：环境变量、alias、shell 的函数照旧不保留（说明书里写着）。每条命令仍然是一个新的 shell，
上一条命令留下的东西（export、set -e、后台的作业）不会悄悄影响下一条，每条命令自己说了算。

怎么知道一条命令结束时在哪个目录（wrap、take）
----------------------------------------------
在命令前面包一层：命令结束时（正常结束、出错、exit 都算），shell 往标准输出打一行"记号 + 目录 + 记号"，
我们从输出里把这一行取出来、去掉，模型看到的输出和以前一样。记号每条命令现抽一个随机数，命令的输出里碰巧有它的机会可以不计。

  bash        trap '…' EXIT：在命令之前设好，命令里的 exit、语法错误也会走到它；退出码原样交回（exit $保存的退出码）。
              打的是 $PWD（经过符号链接 cd 进去的，下一条命令里还是这个写法，比如 /tmp，不是 /private/tmp）。
              只在标准输出还是交给我们的那个管道时才打：命令里 exec > log 把输出改到了文件里，就不往你的文件里写这一行
              （那这一条就当作不知道停在哪儿，留在原来的目录）。
              代价：bash 报错时说的行号多 1（"line 2: foo: command not found"），因为命令前面多了 trap 那一行。
              把 trap 和命令写在同一行可以不多这一行，但命令有语法错误时，bash 会把整行连同 trap 一起打出来给模型看。
  PowerShell  try { 命令 } finally { 打 (Get-Location -PSProvider FileSystem).ProviderPath }。exit、抛出的错误都会走 finally。
              -PSProvider FileSystem：命令 Set-Location 到了注册表（HKLM:）这类地方，记的是最后一个文件夹；
              ProviderPath 是真正的路径（自己建的 PSDrive 会换回它指向的文件夹）。
              退出码：PowerShell 的规矩是"最后一条命令成功就是 0，不成功就是 1，除非写了 exit"。我们在 finally 里多做了几件事，
              它们会成为"最后一条"，所以先记下进 finally 时的 $?，结束时不成功就 exit 1，和以前一模一样。
              命令前面本来就有一行设输出编码的（见 shell.py），param()、using 这类必须写在最前面的东西以前就用不了，现在也一样。

模型的命令可以读到这层包装（trap -p），自己打一个假的记号，或者干脆 cd 到任何地方：这不要紧。记下来的目录只决定
下一条命令从哪儿开始，而下一条命令照样要问你（卡片上写着它在哪个目录里运行），判断"只读的命令"时也按这个目录算
（见 permissions.py），沙箱照样挡着（见 sandbox/）。一条命令本来就能 cd 到哪儿再做事，记着目录并没有多给它什么。
只有一处不一样：以前 cd x && npm test 是一条命令，照常问你；现在 cd x 问过你一次（卡片上只是一条什么都不做的 cd），
之后在 x 里"只读的命令不用问"那一档放行的命令就都不问了。有几样命令做什么要看所在的文件夹里的设置（你加的命令、git、
查版本），它们只在工作目录里（git 是同一个仓库里）才不问，见 commands/paths.py 的 at_workspace、same_repository。

什么时候不记、什么时候回到工作目录
----------------------------------
  - 命令超时、被你停下、被结束掉了：留在原来的目录（它停在哪儿说不准）。没打出记号的（exec 换掉了 shell、被信号杀掉）也一样。
  - 记着的目录不在了（被删掉了），或者沙箱开着、命令不能从那里开始（只能从你给沙箱的地方开始，见 Sandbox.reaches）：回到工作目录，
    这条命令的结果后面说一声，模型就知道它不在原来以为的地方了。
  - 开始一段新对话：从工作目录开始（App 在第一条消息发出之前 reset）。
  - 重新打开一段保存过的会话、分叉出一段新的：也从工作目录开始，不去恢复当时停在哪儿（那个目录现在可能已经不在了，
    也可能早就不是你想让它待的地方）。历史里跑过命令的话，第一条命令的结果后面说一声"之前 cd 到的目录不再算数"。
  - 子 Agent：它有自己的 Shell，从工作目录开始，不跟着叫它的那一方的目录：它的历史是空的，和一段新对话一样；
    交给它的任务里该写清楚在哪儿做（叫它的那一方知道自己在哪儿）。它的 cd 也不会影响叫它的那一方。

和"问不问你"的关系：问你时看的是哪个目录，就在哪个目录里运行
------------------------------------------------------------
卡片上写的目录、判断"只读的命令"时相对路径从哪儿算、沙箱里从哪儿开始，用的都是 where()。目录不在了，where() 当场回到工作目录，
所以问你、判断、运行看的是同一个地方。问过你以后、真正运行之前目录才不在了（别的程序刚删掉它）：不换个地方悄悄运行，
这一条不执行，告诉模型重新执行一次（下一次会按工作目录重新问你）。

倒回最后一轮（编辑、重新生成，见 ../rewind.py）
---------------------------------------------
每一轮开始时记下当时的目录（start_turn），倒回最后一轮时回到那里（rewind_turn）：重新跑的那一轮和原来那一轮从同一个目录开始，
和留下的历史对得上。只能倒最后一轮，所以只要记一个。
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from ..i18n import zh

# 记号的样子：前缀加一段随机的十六进制数，只有字母、数字和下划线，放进 bash 的单引号、PowerShell 的字符串里都不用转义
_PREFIX = "__ZHIFOX_CWD_"


def marker() -> str:
    """这一条命令用的记号（每条命令现抽一个）。"""
    return f"{_PREFIX}{secrets.token_hex(8)}__"


def wrap(command: str, mark: str, windows: bool) -> str:
    """把命令包一层：结束时打出"记号 + 目录 + 记号"（为什么这样写见文件开头）。"""
    if windows:
        return (
            "try {\n"
            f"{command}\n"
            "} finally {\n"
            "$__zhifox_ok = $?\n"
            f'[Console]::Out.Write("`n{mark}" + (Get-Location -PSProvider FileSystem).ProviderPath + "{mark}`n")\n'
            "}\n"
            "if (-not $__zhifox_ok) { exit 1 }"
        )
    trap = f'__zhifox_status=$?; [ -p /dev/stdout ] && printf "\\n%s%s%s\\n" {mark} "$PWD" {mark}; exit $__zhifox_status'
    return f"trap '{trap}' EXIT\n{command}"


def take(output: str, mark: str) -> tuple[str, str | None]:
    """从输出里取出结尾打的那一行：交回（去掉它以后的输出，目录）。没有完整的一行（命令没走到那里），目录是 None。

    找的是最后一对记号：命令开在后台的程序可能在 shell 结束以后还往输出里写（它们也拿着这个管道），那一行就不在最后了。"""
    end = output.rfind(mark)
    if end < 0:
        return output, None
    start = output.rfind(mark, 0, end)
    if start < 0 or start + len(mark) > end:
        return output, None
    place = output[start + len(mark) : end]
    cut_from = start - 1 if start > 0 and output[start - 1] == "\n" else start  # 打的时候前面加了一个换行
    cut_to = end + len(mark)
    if output[cut_to : cut_to + 1] == "\n":
        cut_to += 1
    return output[:cut_from] + output[cut_to:], place


def commands_in(events: Iterable[object]) -> bool:
    """一段会话的事件里，主助手有没有执行过命令（子 Agent 的不算：它的目录本来就不跟着主助手）。重新打开时要不要说一声看它。"""
    from .shell import TOOL_NAME  # shell.py 也导入这个文件：在这里才导入

    return any(isinstance(event, dict) and event.get("type") == "tool_call" and event.get("name") == TOOL_NAME for event in events)


class CurrentDir:
    """一个 Shell 记着的"下一条命令从哪个目录开始"（规矩见文件开头）。几个线程会碰它（跑命令的、网页上倒回一轮的），所以有一把小锁。"""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self._path = workspace  # 下一条命令从这里开始
        self._turn = workspace  # 这一轮开始时在哪儿（倒回最后一轮时回到这里）
        self._note = ""  # 下一条命令的结果后面要说的一句（回到了工作目录、为什么）
        self.began: Path | None = None  # 最近一条命令是从哪个目录开始的（交给钩子的 command_cwd，见 hooks/engine.py）
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """记着的目录（不检查它还在不在：要用来运行命令的，用 where）。"""
        with self._lock:
            return self._path

    def where(self, reaches: Callable[[Path], bool] = lambda path: True) -> Path:
        """下一条命令真正从哪儿开始。记着的目录不在了、reaches 说它不行（沙箱里看不到），就当场回到工作目录，并记下要说的一句。
        问你之前（permissions.py）和运行之前（Shell.run）都看它：第一次看就换过来了，两次看到的是同一个地方。"""
        with self._lock:
            here = self._path
            if here == self.workspace:
                return here
            try:
                there = here.is_dir()
            except OSError:  # 名字太长、没有权限……看不了就当它不在
                there = False
            if not there:
                self._back(zh("上一条命令结束时所在的目录 {path} 已经不在了（可能被删掉了）", path=here))
            elif not reaches(here):
                self._back(zh("之前的命令 cd 到了 {path}，可是这次运行开着沙箱，命令只能从工作目录和额外允许的路径里开始", path=here))
            return self._path

    def _back(self, reason: str) -> None:
        self._path = self.workspace
        # 这几句是写给模型看的中文（zh()），接在命令的结果后面；工具卡片上按界面的语言显示（见 ../i18n.py 开头）
        self._note = zh("{reason}，所以这条命令是在工作目录 {workspace} 里运行的。", reason=reason, workspace=self.workspace)

    def moved(self, place: str | None) -> None:
        """一条命令正常结束了，place 是它最后打出的目录（见 take）。看不懂的（空的、相对路径、有 NUL）不记，留在原来的目录。"""
        if not place or "\x00" in place:
            return
        found = Path(place)
        if not found.is_absolute():
            return
        with self._lock:
            self._path = found

    def gone(self, before: Path) -> str:
        """问你的时候还在 before，真正运行之前它不在了（见文件开头）：这一条不执行时交给模型的话。之前记下的那一句不用再说了。"""
        with self._lock:
            self._note = ""
        return zh(
            "命令没有执行：问用户的时候它要在 {before} 里运行，可是在真正运行之前那个目录不在了（或者沙箱里看不到了）。"
            "之后的命令从工作目录 {workspace} 开始运行；需要的话请重新执行这条命令。",
            before=before, workspace=self.workspace,
        )

    def take_note(self) -> str:
        """取走要在这条命令的结果后面说的那一句（取走就清空）。没有就是空字符串。"""
        with self._lock:
            note, self._note = self._note, ""
        return zh("\n[注意：{note}]", note=note) if note else ""

    def reset(self, reopened: bool = False) -> None:
        """回到工作目录：开始一段新对话、换到另一段会话时。reopened：这段会话的历史里执行过命令（重新打开的、分叉出来的），
        第一条命令的结果后面说一声，模型才知道之前 cd 到的地方不再算数。"""
        with self._lock:
            self._path = self._turn = self.workspace
            self._note = (
                zh("这段对话是重新打开的（或者是从别的对话分叉出来的），命令从工作目录 {workspace} 重新开始运行，之前 cd 到的目录不再算数。", workspace=self.workspace)
                if reopened
                else ""
            )

    def start_turn(self) -> None:
        """一轮开始：记下这一轮从哪个目录开始（倒回最后一轮时回到这里）。"""
        with self._lock:
            self._turn = self._path

    def rewind_turn(self) -> None:
        """倒回最后一轮（编辑、重新生成）：回到这一轮开始时的目录，重新跑的那一轮和原来那一轮从同一个地方开始。"""
        with self._lock:
            self._path = self._turn
