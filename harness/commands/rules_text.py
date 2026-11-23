"""只读的命令 · 编辑文字的程序：sed、awk、jq，只许"读进来、改了打印出去"的用法（规矩的写法和总表 RULES 见 rules.py 开头）。

它们都带着一段小程序：sed 的脚本一个字一个字地读，只许几种最简单的命令（_sed_script）；awk、jq 的程序里出现 AWK_FORBIDDEN、
JQ_FORBIDDEN 里的字（运行命令、写文件、读环境变量……）就不算。看不懂的一律不算。

为什么单独一个文件：这三个的判断要读程序的内容，是整个名单里最容易出绕过的地方，放在一起才好一起看、一起改测试。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .options import allow, names, parse
from .words import Unclear

if TYPE_CHECKING:
    from .rules import Context


# ---- 编辑文字的（只许"读进来、改了打印出去"的用法）----------------------------------

# -l 在 GNU 是"每行多长"（带值），在 macOS 是"一行一行地输出"（不带值）：同一个写法两种意思，不许
SED_ALLOW = names("-n -E -r -e -s -u -z --quiet --silent --regexp-extended --expression --separate --unbuffered --null-data --posix --debug --sandbox")
SED_VALUES = names("-e --expression")
SED_COMMANDS = set("pdqQ=lnNgGhHxDP{}!")  # 只是打印、删除（输出里的）、跳行、缓冲区之间搬来搬去


def _sed(args: list[str], context: Context) -> None:
    """sed：-i 会改文件；脚本里的 w（写文件）、r（读别的文件）、e（运行命令）同样不行。
    脚本只许由几种最简单的命令组成：地址（数字、$、/正则/）加上 p、d、q、=、s/…/…/标志、y/…/…/。看不懂的就不算。"""
    parsed = parse(args, SED_VALUES)
    allow(parsed, SED_ALLOW)
    scripts = [value for name, value in parsed.options if name in ("-e", "--expression") and value is not None]
    files = parsed.operands
    if not scripts:
        if not files:
            raise Unclear("sed 没有脚本")
        scripts, files = [files[0]], files[1:]
    for script in scripts:
        _sed_script(script)
    context.paths([*files, *parsed.late])


def _sed_script(script: str) -> None:
    index, size = 0, len(script)

    def bracket(at: int) -> int:
        """[...] 里分隔符不算分隔符（s/[/]/x/ 的 / 在方括号里），返回 ] 后面的位置。
        方括号里的反斜杠，GNU 和 BSD 的理解不一样，碰到就说看不懂。"""
        at += 1
        if at < size and script[at] == "^":
            at += 1
        if at < size and script[at] == "]":
            at += 1
        while at < size:
            char = script[at]
            if char == "\\":
                raise Unclear("sed 的方括号里有反斜杠")
            if char == "[" and at + 1 < size and script[at + 1] in ":.=":
                close = script.find(script[at + 1] + "]", at + 2)
                if close < 0:
                    raise Unclear("sed 的方括号没写完")
                at = close + 2
                continue
            if char == "]":
                return at + 1
            at += 1
        raise Unclear("sed 的方括号没写完")

    def until(at: int, delimiter: str, pattern: bool) -> int:
        """从 at 开始读到下一个没被转义的 delimiter，返回它的位置。pattern：这一段是正则（方括号另算）。"""
        while at < size:
            char = script[at]
            if char == "\\":
                at += 2
                continue
            if pattern and char == "[":
                at = bracket(at)
                continue
            if char == delimiter:
                return at
            at += 1
        raise Unclear("sed 的脚本没写完")

    while index < size:
        char = script[index]
        if char in " ;\t":
            index += 1
            continue
        # 地址：数字、$、/正则/，可以是"开始,结束"，后面可以跟 !
        for _ in range(2):
            if index < size and script[index].isdigit():
                while index < size and script[index].isdigit():
                    index += 1
            elif index < size and script[index] == "$":
                index += 1
            elif index < size and script[index] == "/":
                index = until(index + 1, "/", pattern=True) + 1
            else:
                break
            if index < size and script[index] == ",":
                index += 1
                continue
            break
        if index >= size:
            raise Unclear("sed 的地址后面没有命令")
        char = script[index]
        if char in SED_COMMANDS:
            index += 1
            if char in "qQ":
                while index < size and script[index].isdigit():
                    index += 1
            continue
        if char in "sy":
            if index + 1 >= size or script[index + 1] in "\\\n " or script[index + 1].isalnum():
                raise Unclear("sed 的 s 命令写法看不懂")
            delimiter = script[index + 1]
            end = until(index + 2, delimiter, pattern=char == "s")
            end = until(end + 1, delimiter, pattern=False)
            index = end + 1
            if char == "s":
                while index < size and (script[index] in "gpiIm" or script[index].isdigit()):  # 不许 w（写文件）、e（运行命令）
                    index += 1
            continue
        raise Unclear(f"sed 的脚本里有 {char!r} 命令")


# system() 运行命令，print > 写文件，| 接到别的程序，getline 读别的文件，ENVIRON 读环境变量，改 ARGV 能换掉要读的文件，
# gawk 的 SYMTAB、FUNCTAB 能用拼出来的名字间接拿到上面这些（SYMTAB["ENVI" "RON"]），@ 是 gawk 的加载扩展、间接调用
AWK_FORBIDDEN = ("system", "getline", "|", ">", "close", "fflush", "environ", "argv", "argc", "procinfo", "symtab", "functab", "@")
AWK_VALUES = names("-F -v --field-separator --assign")


def _awk(args: list[str], context: Context) -> None:
    """awk 是一门完整的语言。程序里出现 AWK_FORBIDDEN 里的字（不管在哪儿，比较大小的 > 也算）就不算只读。
    -f（从文件里读程序，看不到内容）和 gawk 的 -i、-l、-o、-p、-d 这类（加载扩展、写文件）不在名单里。"""
    parsed = parse(args, AWK_VALUES)
    allow(parsed, AWK_VALUES)
    if not parsed.operands:
        raise Unclear("awk 没有程序")
    program = parsed.operands[0].casefold()
    for word in AWK_FORBIDDEN:
        if word in program:
            raise Unclear(f"awk 的程序里有 {word}")
    # 名字=值 是给变量赋值，不是文件。只认真正的变量名（字母、数字、下划线），别的含 = 的照样当文件检查
    files = [word for word in parsed.operands[1:] if not _assignment(word)]
    context.paths([*files, *parsed.late])


def _assignment(word: str) -> bool:
    name, equals, _ = word.partition("=")
    return bool(equals) and name.isascii() and name.replace("_", "a").isalnum() and not name[0].isdigit()


JQ_ALLOW = names(
    "-r -j -c -s -n -e -S -a -M -C -R -h -V --raw-output --join-output --compact-output --slurp --null-input --exit-status"
    " --sort-keys --ascii-output --monochrome-output --color-output --tab --indent --raw-input --arg --argjson --seq --stream"
    " --raw-output0 --unbuffered --help --version"
)
JQ_FORBIDDEN = ("env", "import", "include", "modulemeta", "get_", "$__")


def _jq(args: list[str], context: Context) -> None:
    """jq：$ENV 和 env 能读出环境变量（常常有密钥），import/include 从别的文件读代码；--rawfile、--slurpfile 读别的文件，
    -f 从文件里读程序，--args 让后面的参数不再是文件：都不在名单里。--arg 名字 值 带两个值，要单独数。"""
    words: list[str] = []
    index = 0
    while index < len(args):
        if args[index] in ("--arg", "--argjson"):
            if index + 2 >= len(args):
                raise Unclear(f"{args[index]} 后面要有名字和值")
            index += 3
            continue
        words.append(args[index])
        index += 1
    parsed = parse(words, names("--indent"))
    allow(parsed, JQ_ALLOW)
    if parsed.has("-n", "--null-input") and not parsed.operands:
        return
    if not parsed.operands:
        raise Unclear("jq 没有程序")
    program = parsed.operands[0].casefold()
    for word in JQ_FORBIDDEN:
        if word in program:
            raise Unclear(f"jq 的程序里有 {word}")
    context.paths([*parsed.operands[1:], *parsed.late])
