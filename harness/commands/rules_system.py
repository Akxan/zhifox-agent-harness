"""只读的命令 · 报告系统情况的程序里，要自己看一遍的那几个（规矩的写法和总表 RULES 见 rules.py 开头）。

  date、top、sysctl  Program 的 extra：date 带参数是在设系统时间，top 只许"打印一次就退出"，sysctl 名字=值 是在改内核的设置
  ps                 有几种写法会把别的进程的环境变量打出来（常常有密钥）
  ip                 只许"看"的几个动词，而且要写全（ip 认缩写：ip link s 是 set）

为什么单独一个文件：这几样都只是看这台电脑的情况，不读你的文件；同一类里简单的那些（whoami、uname、df……）直接写在 RULES 里。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .options import Parsed, deny, names, parse
from .words import Unclear

if TYPE_CHECKING:
    from .rules import Context


# ---- 报告系统情况的 --------------------------------------------------------------


def _date(parsed: Parsed, context: Context) -> None:
    # date 后面跟一个不以 + 开头的参数（date 0613162785）是设置系统时间。-j（macOS）表示"不设置，只换算"
    if not parsed.has("-j") and any(not word.startswith("+") for word in [*parsed.operands, *parsed.late]):
        raise Unclear("date 的这种写法会设置系统时间")


def _top(parsed: Parsed, context: Context) -> None:
    # 只许"打印一次就退出"的用法：Linux 上 top -b -n 1，macOS 上 top -l 1。别的用法会一直等键盘
    if not (parsed.has("-l") or (parsed.has("-b") and parsed.has("-n"))):
        raise Unclear("top 只许用 -b -n 次数（Linux）或 -l 次数（macOS）")
    for name in ("-n", "-l"):
        value = parsed.value(name)
        if value is not None and not value.isdigit():
            raise Unclear(f"top 的 {name} 要是一个数")


def _sysctl(parsed: Parsed, context: Context) -> None:
    if any("=" in word for word in parsed.words()):
        raise Unclear("sysctl 名字=值 是在改内核的设置")


def _ps(args: list[str], context: Context) -> None:
    """ps 不会写文件，但有几种写法会把别的进程的环境变量打出来（常常有密钥，智狐自己的进程里就有 .env 里的）：
    BSD 写法（不带横线）里的 e（ps eww、ps auxe，Linux）和 E（ps Ewww，macOS 把它当成 -E），macOS 的 -E。
    -o 里要的字段带 env 的也不行。

    不带横线、又不是哪个选项的值的词，就是 BSD 写法的一串选项：里面有 e 或 E（不分大小写），不管还夹着什么（ps eww1），都不算。"""
    parsed = parse(args, names("-o -O -p -u -U -g -G -t -C --format --sort --pid --ppid --user --group --cols --columns --width"))
    deny(parsed, names("-E"))
    for word in parsed.operands:
        if "e" in word.casefold():
            raise Unclear("ps 的 BSD 写法里的 e、E 会打印进程的环境变量")
    for word in args:
        if not word.startswith("-") and "e" in word and word.isalpha():
            raise Unclear("ps 的 e 会打印进程的环境变量")
    for name, value in parsed.options:
        if name in ("-o", "-O", "--format") and value is not None and "env" in value.casefold():
            raise Unclear("ps 要打印环境变量")
    context.paths(parsed.words())


IP_OPTIONS = names("-4 -6 -br -brief -c -color -d -details -s -stats -statistics -j -json -p -pretty -o -oneline -h -human -human-readable")
IP_OBJECTS = names("a addr address l link r ro route n neigh neighbor neighbour ru rule m maddr maddress")
IP_VERBS = names("show list ls lst get")


def _ip(args: list[str], context: Context) -> None:
    """ip addr、ip -br link、ip route get 1.1.1.1……只许"看"的几个动词，而且要写全（ip 认缩写：ip link s 是 set）。
    ip 的选项是一个短横线的长名字（-brief），而且 -b 是 -batch（从文件里读一串命令来执行），所以选项按整个词比。
    -r（把地址反查成域名）会去问 DNS，不在名单里。"""
    index = 0
    while index < len(args) and args[index].startswith("-"):
        if args[index] not in IP_OPTIONS:
            raise Unclear(f"ip 不认识的选项 {args[index]}")
        index += 1
    rest = args[index:]
    if not rest or rest[0] not in IP_OBJECTS:
        raise Unclear("ip 只许看地址、网卡、路由、邻居、规则")
    if len(rest) > 1 and rest[1] not in IP_VERBS:
        raise Unclear("ip 只许 show、list、get")
    context.paths(rest[2:])
