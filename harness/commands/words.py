"""把一条命令拆成词：只认 bash 里最简单、一眼就看得懂的那一小部分，别的一律当作"看不懂"。

命令最后是交给 /bin/bash -c 去运行的（见 tools/shell.py）。bash 的语法很大：变量、命令替换、重定向、通配符、
花括号展开、here-doc……任何一样都能让"看起来"和"实际运行的"不一样。比如

    cat "$(echo .env)"        看起来是读一个奇怪名字的文件，实际读的是 .env
    ls *                      会读哪些文件，要看文件夹里此刻有什么
    echo hi > notes.txt       echo 本身只读，> 却会覆盖一个文件

所以这里不去"理解"这些写法，而是反过来：只接受没有任何特殊含义的字符，碰到一个就停下，说这条命令看不懂。
看不懂的命令不算只读，照常问你。宁可多问一次，也不能把一条会改东西的命令当成只读的放过去。

接受的只有：普通的词；单引号（里面的东西原样不动）；双引号（里面不能有 $ 和反引号，bash 会在双引号里展开它们）；
反斜杠转义一个字符；词开头的 ~（你的主目录）；把几段连起来的 |（管道）、;、&&、||（一段接一段地跑），每一段另外判断；
以及只往 /dev/null 写、或者把一路输出并到另一路的重定向（2>/dev/null、>/dev/null、&>/dev/null、>>/dev/null、2>&1、>&2）。
后两样是 2026-11-23 第一次真跑评测以后加的：三家模型写的只读命令都常常带着它们（du -sk dir 2>/dev/null | sort -rn、
git log -1 && git show HEAD），每次都问你只是在打扰。写到 /dev/null 什么都改不了；几条只读命令一条接一条地跑，也一样只是看
（cd 不在只读的名单里，所以带着 cd 的一串照样问：后面的命令换了目录，这里按原来的目录看路径就对不上了）。
别的重定向（写文件、< 读文件）、放到后台的 &、目标带引号的写法，照旧看不懂。
标准库的 shlex 不懂 bash 在双引号里展开 $ 这一类规矩，所以这里自己写一个小的扫描器，只做上面这几样。
"""

from __future__ import annotations

import unicodedata

from ..config import READONLY_MAX_CHARS


class Unclear(Exception):
    """这条命令看不懂，或者看懂了但它不是只读的。args[0] 是一句给人看的原因（中文，测试和排查用）。"""


# 不在引号里时，碰到这些就说看不懂。每一个在 bash 里都有特殊含义：
#   < 重定向（读文件）                ( ) 子 shell、函数
#   $ 变量、命令替换、算术            ` 命令替换                    { } 花括号展开、命令组
#   * ? [ 通配符（展开成什么要看文件夹里此刻有什么）
# ; & > 在 stages 里单独看：; && || 连起来的几段各自判断，> 只认写到 /dev/null（见开头），单独的 &（放到后台）照旧看不懂
OPERATORS = set("<()$`{}*?[")
DEV_NULL = "/dev/null"
# 重定向的目标后面只能紧跟着这些（或者命令结束）：/dev/null.txt、/dev/null>x 都不是写到 /dev/null
_AFTER_TARGET = set(" \t|;&")
# 双引号里面，bash 照样会展开这两个
IN_DOUBLE_QUOTES = set("$`")


def _strange(char: str) -> bool:
    """看不见、或者看起来和实际不一样的字符：换行、控制字符、零宽字符、从右往左写的标记、不换行的空格……

    换行在 bash 里就是分号（另起一条命令）；零宽字符和方向标记会让屏幕上显示的和实际运行的不一样。
    普通的中文、日文、带音调的字母不算（grep 错误 日志.txt 是很正常的命令）。
    """
    if char in " \t":
        return False
    category = unicodedata.category(char)
    return category[0] in "CZ"  # C：控制、格式（零宽、方向标记）、私用、代理；Z：各种空白和行、段分隔符


def stages(command: object) -> list[list[str]]:  # 什么都可以给：不是字符串的也在下面当作看不懂
    """把命令拆成一段一段（用 | 连起来的），每一段是一串词。看不懂就抛出 Unclear。"""
    if not isinstance(command, str) or not command.strip():
        raise Unclear("命令是空的")
    if len(command) > READONLY_MAX_CHARS:
        raise Unclear("命令太长了")
    for char in command:
        if _strange(char):
            raise Unclear(f"有看不见或者容易看错的字符（U+{ord(char):04X}）")

    found: list[list[str]] = [[]]
    word: list[str] = []
    started = False  # 这个词开始了没有（'' 这样的空引号也算一个词）
    quoted = False  # 这个词里用过引号或者反斜杠（"2">x 的 2 是参数，不是文件描述符）
    index, size = 0, len(command)

    def finish() -> None:
        nonlocal started, quoted
        if started:
            found[-1].append("".join(word))
        word.clear()
        started = quoted = False

    def next_stage(what: str) -> None:
        """| ; && || 处：这一段结束，下一段开始。两边都不能是空的。"""
        finish()
        if not found[-1]:
            raise Unclear(f"{what} 的一边是空的")
        found.append([])

    while index < size:
        char = command[index]
        if char in " \t":
            finish()
        elif char == "'":
            end = command.find("'", index + 1)
            if end < 0:
                raise Unclear("单引号没有配对")
            word.append(command[index + 1 : end])
            started = quoted = True
            index = end
        elif char == '"':
            index = _double_quoted(command, index + 1, word)
            started = quoted = True
        elif char == "\\":
            if index + 1 >= size:
                raise Unclear("命令以反斜杠结尾")
            word.append(command[index + 1])
            started = quoted = True
            index += 1
        elif char == "|":
            following = command[index + 1 : index + 2]
            if following == "&":
                raise Unclear("用了 |&")
            next_stage("||" if following == "|" else "管道")
            if following == "|":
                index += 1
        elif char == ";":
            next_stage("分号")
        elif char == "&":
            following = command[index + 1 : index + 2]
            if following == "&":
                next_stage("&&")
                index += 1
            elif following == ">":  # &>：两路输出一起
                finish()
                index = _redirect(command, index + 2, duplicate=False)
            else:
                raise Unclear("用了 &（放到后台）")
        elif char == ">":
            # 紧挨在 > 前面、没带引号的一个数字是文件描述符（2>…），bash 不把它当参数。只认 1 和 2：别的（3>、0>）看不懂
            if started and not quoted and "".join(word) in tuple("0123456789"):
                if "".join(word) not in ("1", "2"):
                    raise Unclear("重定向了别的文件描述符")
                word.clear()
                started = False
            else:
                finish()
            index = _redirect(command, index + 1, duplicate=True)
        elif char in OPERATORS:
            raise Unclear(f"用了 {char}")
        elif char == "#" and not started:
            raise Unclear("用了 #（注释）")
        elif char == "~" and not started:
            # 词开头的 ~ 是你的主目录（~/x 也是）。~user、~+、~- 是别的意思，不认
            following = command[index + 1 : index + 2]
            if following not in ("", "/", " ", "\t", "|"):
                raise Unclear("用了 ~ 的特殊写法")
            word.append("~")
            started = True
        else:
            word.append(char)
            started = True
        index += 1
    finish()
    if not found[-1]:
        raise Unclear("最后一段是空的")
    for words in found:
        _no_assignment(words[0])
    return found


def _redirect(command: str, index: int, duplicate: bool) -> int:
    """读一个重定向的目标。index 指着 > 或 &> 后面的第一个字符，返回目标最后一个字符的位置。

    只认写到 /dev/null（>>/dev/null 也行），和 duplicate 时 >& 后面的 1、2（把一路输出并到另一路）。
    目标要一字不差、不带引号，后面紧跟着空白、| ; & 或者命令结束；别的（文件、>&-、>(…)、"/dev/null"）一律看不懂。"""
    if command[index : index + 1] == ">":  # >>：追加
        index += 1
    elif duplicate and command[index : index + 1] == "&":  # >&：并到另一路（或者 >&/dev/null，等于 &>）
        index += 1
        if command[index : index + 1] in ("1", "2"):
            if index + 1 < len(command) and command[index + 1] not in _AFTER_TARGET:
                raise Unclear("重定向到了别的文件描述符")
            return index
    while command[index : index + 1] in (" ", "\t") and index < len(command):
        index += 1
    end = index + len(DEV_NULL)
    if command[index:end] != DEV_NULL or (end < len(command) and command[end] not in _AFTER_TARGET):
        raise Unclear("重定向到了 /dev/null 以外的地方")
    return end - 1


def _double_quoted(command: str, index: int, word: list[str]) -> int:
    """读双引号里的内容，返回结尾那个双引号的位置。里面有 $ 或反引号就说看不懂。"""
    while index < len(command):
        char = command[index]
        if char == '"':
            return index
        if char in IN_DOUBLE_QUOTES:
            raise Unclear(f"双引号里用了 {char}")
        if char == "\\" and index + 1 < len(command) and command[index + 1] in '"\\':
            index += 1
            char = command[index]
        word.append(char)
        index += 1
    raise Unclear("双引号没有配对")


def _no_assignment(first: str) -> None:
    """一段的第一个词是 NAME=value 的话，它是在给后面的命令设一个环境变量（PAGER=…、LD_PRELOAD=…都能让程序做别的事）。"""
    name, equals, _ = first.partition("=")
    if equals and name.rstrip("+").replace("_", "a").isalnum() and not name[0].isdigit():
        raise Unclear("设置了环境变量")
