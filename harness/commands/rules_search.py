from __future__ import annotations

from typing import TYPE_CHECKING

from .options import Parsed, allow, names, parse
from .words import Unclear

if TYPE_CHECKING:
    from .rules import Context


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
