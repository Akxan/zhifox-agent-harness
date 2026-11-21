"""内置的只读命令：每个程序一条规矩，写明哪些用法只是看、哪些用法会改东西。

规矩分三种写法：

  Program(...)        大多数程序：只许用哪些选项（allow），或者哪些选项不许用（deny）；最多几个参数；会不会走遍文件夹。
                      参数和选项的值一律当路径检查（见 paths.py）。
  一个函数            用法复杂的（git 的子命令、sed 和 awk 的脚本、find 的表达式……）：自己看一遍。
  VERSIONS            解释器、编译器只许查版本（python --version）：它们能运行任何代码。

不在这里的程序一律不算只读，包括看起来无害的：
  - 能运行别的程序的：sh、bash、python、xargs、nohup、sudo、su、doas、eval、exec、watch……
  - 能联网的：curl、wget、ping、dig、ssh、nc……读到的东西能被它们送出去（连要查的域名本身都能带走数据）
  - 翻页的：less、more、man（按一个 ! 就能运行命令，而且会等键盘）
  - 会打印环境变量的：env、printenv、set（环境变量里常常有密钥）

tests/test_commands.py 里有一张几百行的表，逐条写着哪些算、哪些不算，以及为什么。改这里的规矩之前先看那张表。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .options import Parsed, allow, deny, names, parse
from .paths import Places
from .words import Unclear


def _no_stage(words: list[str]) -> None:
    raise Unclear("这里不能再套一条命令")


@dataclass
class Context:
    places: Places
    stage: Callable[[list[str]], None] = field(default=_no_stage)  # 判断一段（env、timeout 后面跟的那条命令用）

    def paths(self, words: list[str]) -> None:
        for word in words:
            self.places.check(word)

    def trees(self, roots: list[str], contents: bool, follow: bool = False) -> None:
        for root in roots:
            self.places.check_tree(root, contents=contents, follow=follow)


@dataclass(frozen=True)
class Program:
    allow: frozenset[str] | None = None  # 只许用这些选项（None：除了 deny 里的都行）
    deny: frozenset[str] = frozenset()
    values: frozenset[str] = frozenset()  # 带值的选项
    optional: frozenset[str] = frozenset()  # 值只能紧贴着写的选项
    numbers: bool = False  # -20 这样的数字选项
    operands: int | None = None  # 最多几个参数（None：不限）
    walks: bool = False  # 会不会把参数里的文件夹（没给就是当前文件夹）走一遍、列出里面的名字
    walk_when: frozenset[str] = frozenset()  # 有这些选项之一才走（空：一直走）
    follow: frozenset[str] = frozenset()  # 有这些选项之一，走的时候会跟着符号链接进别的文件夹
    extra: Callable[[Parsed, Context], None] | None = None  # 另外要看的

    def __call__(self, args: list[str], context: Context) -> None:
        parsed = parse(args, self.values, self.optional, self.numbers)
        if self.allow is not None:
            allow(parsed, self.allow)
        deny(parsed, self.deny)
        counted = len(parsed.operands) + len(parsed.late)
        if self.operands is not None and counted > self.operands:
            raise Unclear(f"参数太多（最多 {self.operands} 个）")
        # 只许用名单里选项的，选项都认得（带不带值写清楚了），紧贴着写的值已经分对了；别的程序要另外检查（见 options.py）
        context.paths(parsed.words(attached=self.allow is None))
        if self.walks and (not self.walk_when or parsed.has(*self.walk_when)):
            context.trees(parsed.operands or ["."], contents=False, follow=parsed.has(*self.follow))
        if self.extra is not None:
            self.extra(parsed, context)


ANY = Program()  # 不会写文件、也不会运行别的程序的：什么选项都行，参数照样当路径检查
NO_OPERANDS = Program(operands=0)


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
    """ps 不会写文件，但有几种写法会把别的进程的环境变量打出来（常常有密钥）：BSD 写法里的 e（ps eww、ps auxe），
    macOS 的 -E。-o 里要的字段带 env 的也不行。"""
    parsed = parse(args, names("-o -O -p -u -U -g -G -t -C --format --sort --pid --ppid --user --group --cols --columns --width"))
    deny(parsed, names("-E"))
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


# ---- 读文件的 --------------------------------------------------------------------

SORT_VALUES = names("-k --key -t --field-separator -S --buffer-size -o --output -T --temporary-directory --parallel --batch-size --sort --random-source --compress-program --files0-from")
UNIQ_VALUES = names("-f -s -w --skip-fields --skip-chars --check-chars")
CHECKSUM = Program(deny=names("-c --check"))  # -c：按一个文件里列的名字去读别的文件


def _xxd(args: list[str], context: Context) -> None:
    """xxd 输入 输出：第二个参数是要写的文件。xxd 按开头几个字母认选项（-ps 就是 -p），没法照常拆，
    所以每个不以 - 开头的词都算一个参数（选项的值也算进去：xxd -l 16 a.txt 会多问一次），最多一个；
    -r（把十六进制还原成文件）不许。"""
    counted, started = 0, False
    for word in args:
        if word.startswith("-r") and not started:
            raise Unclear("xxd -r 是在还原文件")
        # 碰到第一个参数以后，xxd 不再认选项：后面不管像不像选项，都是参数（xxd a.txt -c 的 -c 是要写的文件）
        if started or not word.startswith("-") or word == "-":
            started = True
            counted += 1
    if counted > 1:
        raise Unclear("xxd 的第二个参数是要写的文件")
    context.paths(args)


LS_ALLOW = names(
    "-a -A -l -h -R -t -r -S -1 -d -F -i -n -o -g -G -p -s -u -c -U -x -C -m -b -q -Q -k -L -H -e -O -@ -v -X -Z -B -N"
    " --all --almost-all --human-readable --si --recursive --reverse --size --classify --directory --inode --numeric-uid-gid"
    " --group-directories-first --full-time --dereference --color --sort --time --time-style --format --indicator-style"
    " --quoting-style --block-size --hide --ignore --width --tabsize --literal --escape --hyperlink --no-group --author"
    " --context --zero --file-type --ignore-backups --dereference-command-line"
)
# 写成"--ignore 模式"（中间是空格）时，后面那个词是值，不是要列的文件夹。要分清，不然 ls -R --ignore x 会被当成只列 x
LS_VALUES = names("--hide --ignore --width --tabsize --time-style --format --sort --time --quoting-style --indicator-style --block-size")
TREE_ALLOW = names(
    "-a -d -l -f -x -i -q -N -Q -p -u -g -s -h -D -F -v -t -c -U -r -C -n -J -X -L -P -I -H -T"
    " --si --du --dirsfirst --filesfirst --noreport --charset --prune --matchdirs --ignore-case --filelimit --sort --metafirst"
    " --gitignore --nolinks --inodes --device --timefmt --info --icase"
)
TREE_VALUES = names("-L -P -I -H -T --charset --filelimit --sort --timefmt")
DU_ALLOW = names(
    "-s -h -a -c -k -m -g -H -L -P -x -d -b -B -l -t -0 -S -A -I"
    " --max-depth --summarize --human-readable --si --all --total --apparent-size --bytes --block-size --count-links"
    " --exclude --time --threshold --null --separate-dirs --dereference --one-file-system --inodes"
)
DU_VALUES = names("-d -B -t -I --max-depth --block-size --threshold --exclude")
DIFF_ALLOW = names(
    "-u -U -c -C -r -N -q -s -y -w -b -B -i -a -t -T -p -e -d -x -X -W -E -Z -n -F -I"
    " --unified --context --recursive --new-file --unidirectional-new-file --brief --report-identical-files --side-by-side"
    " --ignore-all-space --ignore-space-change --ignore-blank-lines --ignore-case --ignore-tab-expansion --ignore-trailing-space"
    " --ignore-matching-lines --text --expand-tabs --initial-tab --show-c-function --show-function-line --ed --minimal"
    " --exclude --exclude-from --width --strip-trailing-cr --label --color --normal --suppress-common-lines --left-column"
    " --no-dereference --speed-large-files --rcs --suppress-blank-empty --tabsize --from-file --to-file --palette --horizon-lines"
)
# --unified、--context 的值只能写成 --unified=3（GNU 的"可选的值"），写成 --unified 3 时 3 是要比较的文件
DIFF_VALUES = names("-U -C -x -X -W -F -I --exclude --exclude-from --width --label --ignore-matching-lines --show-function-line --tabsize --from-file --to-file --horizon-lines")


def _du(parsed: Parsed, context: Context) -> None:
    # du -s、du -d 0 只算总数，不把下面每个文件夹的名字列出来；别的写法会列，按"列出名字"走一遍
    summary = parsed.has("-s", "--summarize") or parsed.value("-d", "--max-depth") == "0"
    if not summary:
        context.trees(parsed.operands or ["."], contents=False, follow=parsed.has("-L", "--dereference"))


def _diff(parsed: Parsed, context: Context) -> None:
    # 两个文件夹不加 -r 也会比较里面同名的文件；--from-file、--to-file 给的也可能是文件夹。不管加没加 -r，碰到文件夹就走一遍
    roots = [*parsed.operands, *(value for name, value in parsed.options if name in ("--from-file", "--to-file") and value)]
    context.trees(roots, contents=True)


# ---- 搜索的 ----------------------------------------------------------------------

GREP_ALLOW = names(
    "-a -b -c -E -F -G -H -h -i -I -l -L -n -o -P -q -r -R -s -T -u -U -v -V -w -x -y -z -Z -O -S -p -J -M -A -B -C -D -d -e -f -m -NUM"
    " --extended-regexp --fixed-strings --basic-regexp --perl-regexp --regexp --file --ignore-case --no-ignore-case --word-regexp"
    " --line-regexp --null-data --no-messages --invert-match --version --help --max-count --byte-offset --line-number --no-line-number"
    " --line-buffered --with-filename --no-filename --label --only-matching --quiet --silent --binary-files --text --directories --devices"
    " --recursive --dereference-recursive --include --exclude --exclude-from --exclude-dir --include-dir --files-without-match"
    " --files-with-matches --count --initial-tab --null --before-context --after-context --context --color --colour"
    " --group-separator --no-group-separator"
)
GREP_VALUES = names("-A -B -C -D -d -e -f -m --regexp --file --max-count --label --directories --devices --include --exclude --exclude-from --exclude-dir --include-dir --before-context --after-context --group-separator")
PATTERNS = ("-e", "--regexp")


def _search(parsed: Parsed, context: Context, pattern_given: bool, recursive: bool, follow: bool = False) -> None:
    """grep 和 rg 共用：要找的内容（第一个参数或者 -e 的值）只是一段文字，不当路径检查（grep -i secret 很常见）；
    别的参数是要搜的文件。递归的话，每个文件夹都要走一遍；一个都没给，就是搜当前文件夹。"""
    files = parsed.operands if pattern_given else parsed.operands[1:]
    others = [value for name, value in parsed.options if name not in PATTERNS and value is not None]
    context.paths([*files, *others, *parsed.late])
    if recursive:
        context.trees(files or ["."], contents=True, follow=follow)


def _grep(args: list[str], context: Context) -> None:
    parsed = parse(args, GREP_VALUES, numbers=True)
    allow(parsed, GREP_ALLOW)
    # -d 后面只认得 read、skip 两个值是"不递归"。recurse 的缩写（rec）、大写（RECURSE，macOS 不分大小写）都按递归算
    directories = parsed.value("-d", "--directories")
    recursive = parsed.has("-r", "-R", "--recursive", "--dereference-recursive") or (directories is not None and directories not in ("read", "skip"))
    _search(parsed, context, parsed.has("-e", "-f", "--regexp", "--file"), recursive, follow=parsed.has("-R", "--dereference-recursive", "-S"))


RG_ALLOW = names(
    "-i -s -S -w -x -v -n -N -l -c -F -u -U -P -L -H -I -o -0 -a -q -e -f -g -t -T -A -B -C -m -d -j -M -r -E -h -V"
    " --hidden --no-ignore --no-ignore-vcs --no-ignore-parent --no-ignore-global --no-ignore-dot --no-ignore-exclude --no-ignore-files"
    " --follow --json --vimgrep --no-heading --heading --line-number --no-line-number --column --no-column --count --count-matches"
    " --files --files-with-matches --files-without-match --fixed-strings --ignore-case --smart-case --case-sensitive --word-regexp"
    " --line-regexp --invert-match --only-matching --pcre2 --multiline --multiline-dotall --trim --stats --null --quiet --text"
    " --binary --no-messages --byte-offset --passthru --crlf --null-data --one-file-system --type-list --no-config --sort-files"
    " --max-columns-preview --unrestricted --version --help --regexp --file --glob --iglob --type --type-not --after-context"
    " --before-context --context --max-count --max-depth --threads --max-columns --replace --encoding --sort --sortr --color"
    " --colors --max-filesize --context-separator --field-match-separator --path-separator --engine --with-filename --no-filename"
)
RG_VALUES = names(
    "-e -f -g -t -T -A -B -C -m -d -j -M -r -E --regexp --file --glob --iglob --type --type-not --after-context --before-context"
    " --context --max-count --max-depth --threads --max-columns --replace --encoding --sort --sortr --color --colors"
    " --max-filesize --context-separator --field-match-separator --path-separator --engine"
)


def _rg(args: list[str], context: Context) -> None:
    """ripgrep：不带文件夹时搜当前文件夹。--pre（先用一个程序处理每个文件）、-z（调用解压程序）、--hostname-bin 不在名单里。"""
    parsed = parse(args, RG_VALUES)
    allow(parsed, RG_ALLOW)
    if parsed.has("--type-list"):
        return
    _search(parsed, context, parsed.has("-e", "-f", "--regexp", "--file", "--files"), True, follow=parsed.has("-L", "--follow"))


FIND_TESTS = names(
    "-name -iname -path -ipath -wholename -iwholename -regex -iregex -type -xtype -size -empty -mtime -mmin -atime -amin -ctime"
    " -cmin -newer -anewer -cnewer -user -group -uid -gid -nouser -nogroup -perm -links -inum -samefile -maxdepth -mindepth -depth"
    " -xdev -mount -follow -print -print0 -printf -ls -prune -quit -true -false -not -and -or -a -o -readable -writable -executable"
    " -fstype -lname -ilname -noleaf -regextype -daystart -ignore_readdir_race -noignore_readdir_race -warn -nowarn -used -Bmin"
    " -Btime -Bnewer -flags -d -s -x ( ) ! ,"
)
FIND_ONE_VALUE = names(
    "-name -iname -path -ipath -wholename -iwholename -regex -iregex -type -xtype -size -mtime -mmin -atime -amin -ctime -cmin"
    " -newer -anewer -cnewer -user -group -uid -gid -perm -links -inum -samefile -maxdepth -mindepth -printf -fstype -lname -ilname"
    " -regextype -used -Bmin -Btime -Bnewer -flags"
)
FIND_FILES = names("-newer -anewer -cnewer -samefile -Bnewer")  # 值是一个文件（拿来比较时间、是不是同一个），要当路径检查
FIND_BEFORE = names("-H -L -P -E -X -s -x -f -O0 -O1 -O2 -O3")  # 写在路径前面的选项


def _find(args: list[str], context: Context) -> None:
    """find 路径… 表达式。-exec、-execdir、-ok、-delete、-fprint 这类不在名单里（运行命令、删除、写文件）。
    -name 这类的值是模式，不是路径（find src -name .env 只是找名字），不当路径检查。"""
    index = 0
    roots: list[str] = []
    follow = False
    while index < len(args) and args[index] in FIND_BEFORE:
        follow = follow or args[index] == "-L"
        if args[index] == "-f":  # macOS：-f 路径（以 - 开头的路径要这么写）
            if index + 1 >= len(args):
                raise Unclear("-f 后面缺少路径")
            roots.append(args[index + 1])
            index += 1
        index += 1
    while index < len(args) and not (args[index].startswith("-") or args[index] in ("(", ")", "!", ",")):
        roots.append(args[index])
        index += 1
    while index < len(args):
        word = args[index]
        newer = word.startswith("-newer") and len(word) == 8  # -newerXY 参照物
        if word not in FIND_TESTS and not newer:
            raise Unclear(f"find 不许用 {word}")
        follow = follow or word == "-follow"
        if word in FIND_ONE_VALUE or newer:
            if index + 1 >= len(args):
                raise Unclear(f"{word} 后面缺少值")
            if word in FIND_FILES or (newer and word[-1] != "t"):  # -newermt 的值是一个时间，别的 -newerXY 是一个文件
                context.places.check(args[index + 1])
            index += 1
        index += 1
    context.paths(roots)
    context.trees(roots or ["."], contents=False, follow=follow)


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


# ---- 有子命令的 ------------------------------------------------------------------

GIT_READS = {"status", "log", "diff", "show", "shortlog", "blame", "describe", "rev-parse", "ls-files", "branch", "tag", "reflog", "stash", "remote", "version", "whatchanged"}
# 把结果写进文件、运行外部的 diff 程序、运行 gpg 验签名。reflog、stash show 会把选项原样交给 log、diff，所以每个子命令都不许
GIT_DENY = names("--output --ext-diff --show-signature")
GIT_LIST_ONLY = {
    # 这几个子命令带参数就是在建、删、改（git branch 新名字、git tag v1）：只许列出来
    "branch": names(
        "-a --all -r --remotes -v -vv --verbose --list -l --show-current --contains --no-contains --merged --no-merged --points-at"
        " --sort --format --column --no-column --color --no-color -i --ignore-case --abbrev --no-abbrev -q --quiet --omit-empty"
    ),
    "tag": names("-l --list -n --sort --contains --no-contains --merged --no-merged --points-at --format --column --no-column -i --ignore-case --color --omit-empty"),
}


def _git(args: list[str], context: Context) -> None:
    """git 只许几个只是看的子命令。前面的全局选项只许 --no-pager：-c 能改任何设置（core.pager、core.fsmonitor 都能运行命令），
    -C、--git-dir、--work-tree 换到别的仓库（那里的设置可能就是这么写的），-p 强制用翻页程序。"""
    index = 0
    while index < len(args) and args[index] in ("--no-pager", "-P"):
        index += 1
    if args[index:] == ["--version"]:
        return
    if index >= len(args) or args[index] not in GIT_READS:
        raise Unclear("git 只许 status、log、diff、show、branch 这类只是看的子命令")
    command, rest = args[index], args[index + 1 :]
    if command == "version":
        if rest:
            raise Unclear("git version 后面不许跟别的")
        return
    # git show HEAD:.env、git log -- .env：HEAD: 后面的部分也当路径检查（见 paths._candidates）
    parsed = parse(rest, optional=names("-u -O -U -M -C -B -S -G -L -n"))
    deny(parsed, GIT_DENY)
    if command in GIT_LIST_ONLY:
        allow(parsed, GIT_LIST_ONLY[command])
        # 老版本的 git 里，git branch -l 是"建分支时记下 reflog"，不是列出来：branch 只认写全的 --list
        if parsed.operands and not parsed.has("--list", *(["-l"] if command == "tag" else [])):
            raise Unclear(f"git {command} 带名字是在新建或修改")
    elif command == "diff":
        # --no-index 比较任意两个文件夹（不管在不在仓库里）；两边有一个在仓库外面、或者根本不在仓库里时，git 自己就会这么做。
        # 所以给了文件夹的，都按"读里面每个文件"走一遍
        deny(parsed, names("--no-index"))
        context.trees(parsed.operands, contents=True)
    elif command == "status":
        deny(parsed, names("--ignored"))  # 被忽略的文件（.env、data/……）的名字也列出来
    elif command == "ls-files":
        deny(parsed, names("-o --others -i --ignored"))
    elif command == "reflog":
        if parsed.operands and parsed.operands[0] != "show":
            raise Unclear("git reflog 只许 show")  # expire、delete 会删记录
    elif command == "stash":
        if not parsed.operands or parsed.operands[0] not in ("list", "show"):
            raise Unclear("git stash 只许 list、show")  # 光写 git stash 是把改动收起来
    elif command == "remote" and rest:
        raise Unclear("git remote 只许光写")  # -v、get-url 打出的地址里可能带着 token，show 会联网
    context.paths(parsed.words())


SYSTEMCTL_READS = {"status", "is-active", "is-enabled", "is-failed", "is-system-running", "list-units", "list-unit-files", "list-timers", "list-sockets", "list-dependencies"}
SYSTEMCTL_ALLOW = names(
    "--no-pager --user --system -l --full -a --all -n --lines -t --type --state --failed -q --quiet --no-legend -o --output -r"
    " --recursive --plain --reverse --after --before --with-dependencies"
)
SYSTEMCTL_VALUES = names("-n --lines -t --type --state -o --output")


def _systemctl(args: list[str], context: Context) -> None:
    """只许查状态、列出来（光写 systemctl 就是 list-units）。-H（连到别的机器）、-M 不在名单里；
    show、cat 会显示服务的环境变量（常常有密码），也不算。"""
    parsed = parse(args, SYSTEMCTL_VALUES)
    allow(parsed, SYSTEMCTL_ALLOW)
    if parsed.operands and parsed.operands[0] not in SYSTEMCTL_READS:
        raise Unclear("systemctl 只许 status、is-active、list-units 这类查看的子命令")
    context.paths(parsed.words())


JOURNALCTL = Program(
    # 删旧日志、轮换、写游标文件、生成密钥：这些会改东西。长选项连缩写一起不许（--vac 就是 --vacuum-…）
    deny=names(
        "--vacuum-size --vacuum-time --vacuum-files --rotate --flush --sync --relinquish-var --smart-relinquish-var --setup-keys"
        " --update-catalog --cursor-file --new-id128"
    ),
    values=names("-u --unit -t --identifier -p --priority -S --since -U --until -o --output -D --directory --file -M --machine -g --grep --root --image -F --field"),
    # -n、-b 的值可以不写（journalctl -b 就是这次开机），只有紧贴着写的才是值：写成 -n --rotate 时，--rotate 照样是一个选项
    optional=names("-n --lines -b --boot"),
)

DOCKER_READS = {"ps", "images", "logs", "version", "info", "stats", "port"}
DOCKER_GROUPS = {"container": {"ls", "ps", "list", "logs", "port", "stats", "top"}, "image": {"ls", "list"}}


def _docker(args: list[str], context: Context) -> None:
    """docker 只许几个只是看的子命令，前面不许有任何全局选项（-H、--context、--config 会连到别的机器、用别的设置）。
    inspect 不算：它会显示容器的环境变量（常常有密码）。top 后面的词会原样交给容器里的 ps（ps e 会打印环境变量），
    所以 top 只许跟一个容器名。"""
    if not args or args[0].startswith("-"):
        raise Unclear("docker 前面不许有选项")
    if args[0] in DOCKER_GROUPS:
        if len(args) < 2 or args[1] not in DOCKER_GROUPS[args[0]]:
            raise Unclear(f"docker {args[0]} 只许看")
        command, rest = args[1], args[2:]
    elif args[0] in DOCKER_READS or args[0] == "top":
        command, rest = args[0], args[1:]
    else:
        raise Unclear("docker 只许 ps、images、logs 这类只是看的子命令")
    if command == "top" and (len(rest) != 1 or rest[0].startswith("-")):
        raise Unclear("docker top 只许跟一个容器名")
    context.paths(parse(rest).words())


KUBECTL_READS = {"get", "describe", "logs", "top", "version", "api-resources", "api-versions"}
KUBECTL_ALLOW = names(
    "-n --namespace -A --all-namespaces -o --output -l --selector --field-selector -w --watch --watch-only --show-labels --sort-by"
    " --no-headers -L --label-columns --show-kind --chunk-size --context -c --container --all-containers -f --follow --tail --since"
    " --since-time --timestamps -p --previous --prefix --limit-bytes --max-log-requests --request-timeout --containers"
    " --use-protocol-buffers --client --ignore-not-found --output-watch-events"
)
KUBECTL_VALUES = names(
    "-n --namespace -o --output -l --selector --field-selector --sort-by -L --label-columns --chunk-size --context -c --container"
    " --tail --since --since-time --limit-bytes --max-log-requests --request-timeout"
)


def _kubectl(args: list[str], context: Context) -> None:
    """kubectl 只许查看，而且不碰 Secret（kubectl get secret -o yaml 会把密码都打出来）。
    --kubeconfig、--server、--token、-v（调高了会把请求头打出来）、--raw（任意的 API 路径，还能经代理访问集群里的服务）
    这类不在名单里。get 的 -f 是读一个文件，也不在里面。"""
    parsed = parse(args, KUBECTL_VALUES)
    allow(parsed, KUBECTL_ALLOW)
    if not parsed.operands or parsed.operands[0] not in KUBECTL_READS:
        raise Unclear("kubectl 只许 get、describe、logs 这类查看的子命令")
    if parsed.operands[0] != "logs" and parsed.has("-f", "--follow"):
        raise Unclear("kubectl 的 -f 是读文件")
    if any("secret" in word.casefold() for word in parsed.words()):
        raise Unclear("kubectl 碰到了 Secret")
    context.paths(parsed.words()[1:])


def _crontab(args: list[str], context: Context) -> None:
    if args != ["-l"]:
        raise Unclear("crontab 只许 -l")  # 别的写法会换掉、编辑、删除你的定时任务


# ---- 套在别的命令外面的 --------------------------------------------------------------


def _env(args: list[str], context: Context) -> None:
    """env 命令：只许原样运行后面那条命令（它本身得是只读的）。光写 env 会打印所有环境变量（常常有密钥）；
    env 名字=值 命令、env -i、-S 都能改变那条命令的行为。"""
    words = args[1:] if args[:1] == ["--"] else args
    if not words:
        raise Unclear("env 会打印所有环境变量")
    if words[0].startswith("-") or "=" in words[0]:
        raise Unclear("env 改了环境变量")
    context.stage(words)


TIMEOUT_OPTIONS = {"--preserve-status": 0, "--foreground": 0, "-v": 0, "--verbose": 0, "-k": 1, "--kill-after": 1, "-s": 1, "--signal": 1}


def _timeout(args: list[str], context: Context) -> None:
    """timeout 时长 命令：后面那条命令得是只读的。timeout 碰到第一个不是选项的词就不再认选项。"""
    index = 0
    while index < len(args) and args[index].startswith("-"):
        name = args[index].partition("=")[0]
        if name not in TIMEOUT_OPTIONS:
            raise Unclear(f"timeout 不认识的选项 {name}")
        index += 1 + (TIMEOUT_OPTIONS[name] if "=" not in args[index] else 0)
    if index >= len(args) or not _duration(args[index]):
        raise Unclear("timeout 后面要先写时长")
    if index + 1 >= len(args):
        raise Unclear("timeout 后面没有命令")
    context.stage(args[index + 1 :])


def _duration(text: str) -> bool:
    number = text[:-1] if text[-1:] in "smhd" else text
    return number.replace(".", "", 1).isdigit()


# 只许查版本的程序：它们本身能运行任何代码（python -c、node -e），也就只有 --version 这一种用法算只读。
# 查版本时会联网的（terraform version 会去看有没有新版本）不放进来
VERSIONS = {
    "python": [["--version"], ["-V"]], "python3": [["--version"], ["-V"]], "node": [["--version"], ["-v"]],
    "npm": [["--version"], ["-v"]], "pnpm": [["--version"]], "yarn": [["--version"]], "ruby": [["--version"], ["-v"]],
    "rustc": [["--version"], ["-V"]], "cargo": [["--version"], ["-V"]], "uv": [["--version"]], "pip": [["--version"]],
    "pip3": [["--version"]], "gcc": [["--version"]], "clang": [["--version"]], "make": [["--version"]], "cmake": [["--version"]],
    "go": [["version"]], "java": [["-version"], ["--version"]], "deno": [["--version"]], "bun": [["--version"]],
    "php": [["--version"], ["-v"]], "perl": [["--version"], ["-v"]], "nginx": [["-v"], ["-V"]], "psql": [["--version"]],
    "docker-compose": [["--version"]],
}


def _version(program: str) -> Callable[[list[str], Context], None]:
    def check(args: list[str], context: Context) -> None:
        if args not in VERSIONS[program]:
            raise Unclear(f"{program} 只许查版本")

    return check


RULES: dict[str, Callable[[list[str], Context], None]] = {
    # 报告系统的情况
    "pwd": Program(allow=names("-L -P"), operands=0),
    "whoami": NO_OPERANDS, "uptime": NO_OPERANDS, "uname": NO_OPERANDS, "arch": NO_OPERANDS, "nproc": NO_OPERANDS,
    "free": NO_OPERANDS, "lscpu": NO_OPERANDS, "sw_vers": NO_OPERANDS, "tty": NO_OPERANDS, "locale": NO_OPERANDS,
    "id": ANY, "groups": ANY, "w": ANY, "who": ANY, "users": ANY, "last": ANY, "cal": ANY, "df": ANY, "vmstat": ANY,
    "vm_stat": ANY, "iostat": ANY, "pgrep": ANY, "pidof": ANY, "lsof": ANY, "netstat": ANY, "lsblk": ANY,
    "findmnt": ANY, "echo": ANY, "which": ANY, "whereis": ANY, "type": ANY,
    "printf": Program(deny=names("-v")),  # printf -v 变量：bash 4 以上会运行数组下标里的 $(...)
    "ps": _ps,
    "hostname": Program(allow=names("-s -f -d -i -I -a -A --short --fqdn --long --domain --ip-address --all-ip-addresses --alias --all-fqdns"), operands=0),
    "date": Program(
        allow=names("-u --utc --universal -R --rfc-2822 --rfc-email -I --iso-8601 --rfc-3339 -d --date -r --reference -j -f -v -z"),
        values=names("-d --date -r --reference -f -v -z"), optional=names("-I"), extra=_date,
    ),
    "top": Program(allow=names("-b -n -l -o -O -s -d -c -H -i -u -U -p -1 -e"), values=names("-n -l -o -O -s -d -u -U -p"), operands=0, extra=_top),
    "ss": Program(deny=names("-K --kill -D --diag"), values=names("-D --diag -F --filter -f --family -A --query --socket")),
    "sysctl": Program(allow=names("-a -A -n -N -e -h -x -X -d -o --all --values --names"), extra=_sysctl),
    "dmesg": Program(
        allow=names("-T --ctime -H --human -l --level -f --facility -k --kernel -u --userspace -x --decode -L --color -t --notime -r --raw -S --syslog -e --reltime -P --nopager -w --follow -W --follow-new --since --until -J --json --time-format"),
        values=names("-l --level -f --facility --since --until --time-format"), optional=names("-L"),
    ),
    "ip": _ip,
    "ifconfig": Program(allow=names("-a -l -s -v -u -d -L -m"), operands=1),
    # 读文件
    "cat": ANY, "tac": ANY, "nl": ANY, "cut": ANY, "column": ANY, "fold": ANY, "paste": ANY, "join": ANY, "comm": ANY,
    "expand": ANY, "unexpand": ANY, "rev": ANY, "od": ANY, "hexdump": ANY, "strings": ANY, "cmp": ANY, "stat": ANY,
    "realpath": ANY, "readlink": ANY, "basename": ANY, "dirname": ANY,
    "head": Program(numbers=True), "tail": Program(numbers=True),
    "wc": Program(deny=names("--files0-from")),  # 从一个文件（或者管道）里读要数的文件的名字：看不到会读哪些
    "file": Program(deny=names("-C --compile -f --files-from -z -Z --uncompress --uncompress-noreport")),  # -C 写规则文件；-f 从文件里读名字；-z 调用解压程序
    "sort": Program(deny=names("-o --output -T --temporary-directory --compress-program --files0-from"), values=SORT_VALUES),
    "uniq": Program(values=UNIQ_VALUES, operands=1),  # uniq 输入 输出：第二个参数是要写的文件
    "tr": Program(operands=2),
    "xxd": _xxd,
    "md5sum": CHECKSUM, "sha1sum": CHECKSUM, "sha224sum": CHECKSUM, "sha256sum": CHECKSUM, "sha384sum": CHECKSUM,
    "sha512sum": CHECKSUM, "b2sum": CHECKSUM, "shasum": CHECKSUM, "cksum": CHECKSUM, "md5": ANY,
    "ls": Program(allow=LS_ALLOW, values=LS_VALUES, walks=True, walk_when=names("-R --recursive"), follow=names("-L --dereference")),
    "tree": Program(allow=TREE_ALLOW, values=TREE_VALUES, walks=True, follow=names("-l")),  # -o 写文件，-R 在每一层写 00Tree.html：不在名单里
    "du": Program(allow=DU_ALLOW, values=DU_VALUES, extra=_du),
    "diff": Program(allow=DIFF_ALLOW, values=DIFF_VALUES, extra=_diff),  # -l 会调用 pr，不在名单里
    # 搜索
    "grep": _grep, "egrep": _grep, "fgrep": _grep, "rg": _rg, "find": _find,
    # 编辑文字（只许打印出来）
    "sed": _sed, "awk": _awk, "jq": _jq,
    # 有子命令的
    "git": _git, "systemctl": _systemctl, "journalctl": JOURNALCTL, "docker": _docker, "kubectl": _kubectl, "crontab": _crontab,
    # 套在外面的
    "env": _env, "timeout": _timeout,
    **{program: _version(program) for program in VERSIONS},
}
