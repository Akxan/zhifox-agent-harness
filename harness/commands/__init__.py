"""一条 shell 命令是不是"只读的"：只是看，不改任何东西，也不把东西送出去。

"只读的命令不用问"那一档（见 ../permissions.py 的 MODES）靠这里决定哪些命令可以不问就运行。
用的人多半是在聊天软件里远程看服务器（df -h、systemctl status nginx、journalctl -u nginx -n 50），
每条都问太烦，全部放开又太危险：中间这一档只放开"看"。

判断的办法是保守的：认得出来、确定只是看的，才算只读；拿不准的一律不算，照常问你。
多问一次的代价是你多点一下；少问一次的代价可能是一条 rm -rf。所以这里的每一步都往"多问"那边偏：

  1. 拆词（words.py）：只认最简单的写法。变量（$HOME）、命令替换（$(...)、反引号）、重定向（> 文件）、
     通配符（*）、分号和 &&、看不见的字符……碰到一个就说看不懂。这些都会让"看起来"和"实际运行的"不一样。
  2. 认程序（rules.py）：每个程序一条规矩，写明哪些用法只是看。不在名单里的程序一律不算，
     哪怕它看起来无害；在名单里的，用了不认识的选项也不算。
  3. 看路径（paths.py）：就算是 cat，读 .env、智狐的数据目录、~/.ssh 这类地方也照常问。
  4. 管道（a | b）只在每一段都是只读的时候才算。你自己加的命令（allowlist.py）只在单独一条时才算，不能出现在管道里：
     cat 脚本 | python 你允许过的东西，python 就会运行管道送进来的任何代码。

这里说"算只读"，只是说不问你就可以运行；它照样经过确认之外的所有规矩（命令看不到 API 密钥，见 tools/shell.py），
对话里也照样标着"自动允许（只读）"，你看得到它做了什么。

Windows 上一条都不算只读，你自己加的"不用问的命令"也不算，每条都照常问你。因为那里的命令是 PowerShell（见 tools/shell.py），
这里的拆词、名单都是照着 bash 写的，换成 PowerShell 就看不准了：它有自己的一套写法——@(…) 和 $(…)、{ 脚本块 }、
& 调用、反引号转义、--% 之后原样传、Get-Content 能读 HKLM: 和 Env: 这类"驱动器"、几乎每个命令都有别名（ls、cat、rm 都是）……
按 bash 的规矩看着无害的一条，在 PowerShell 里可能是另一回事。拿不准的一律问，所以整个关掉，不去猜。
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .paths import Places
from .rules import RULES, Context
from .words import Unclear, stages

__all__ = ["Places", "Unclear", "Verdict", "classify"]

WINDOWS = os.name == "nt"  # 测试里换成 True，看 Windows 上的样子


@dataclass(frozen=True)
class Verdict:
    readonly: bool
    reason: str = ""  # 为什么不算只读（给测试和排查看的中文，不显示在界面上）
    source: str = ""  # 算只读的话，是因为内置的名单（"builtin"）还是你自己加的（"listed"）


def classify(command: object, places: Places, listed: Iterable[Sequence[str]] = ()) -> Verdict:
    """command 这条命令可不可以不问就运行。listed 是你自己加的命令，每条拆成了词（见 allowlist.py）。"""
    if WINDOWS:
        return Verdict(False, reason="Windows 上的命令是 PowerShell，这里看不准它的写法，每条都问（见开头的说明）")
    try:
        found = stages(command)
        if len(found) == 1:
            words = found[0]
            for entry in listed:
                if entry and words[: len(entry)] == list(entry):
                    Context(places).paths(words[1:])  # 你允许的是这条命令，不是让它去读 .env
                    return Verdict(True, source="listed")
        context = Context(places)
        context.stage = lambda words: _stage(words, context)
        for words in found:
            _stage(words, context)
    except Unclear as exc:
        return Verdict(False, reason=str(exc.args[0]) if exc.args else "")
    except (OSError, RuntimeError, ValueError) as exc:  # 看文件夹时出了错（名字太长、没有权限……）：看不清就不算
        return Verdict(False, reason=f"看路径时出错：{exc}")
    return Verdict(True, source="builtin")


def _stage(words: list[str], context: Context) -> None:
    rule = RULES.get(words[0])
    if rule is None:
        raise Unclear(f"{words[0]} 不在只读命令的名单里")
    rule(words[1:], context)
