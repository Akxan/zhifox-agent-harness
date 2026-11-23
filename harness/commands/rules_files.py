from __future__ import annotations

from typing import TYPE_CHECKING

from .options import Parsed, names
from .words import Unclear

if TYPE_CHECKING:
    from .rules import Context


# ---- 读文件的 --------------------------------------------------------------------

SORT_VALUES = names("-k --key -t --field-separator -S --buffer-size -o --output -T --temporary-directory --parallel --batch-size --sort --random-source --compress-program --files0-from")
UNIQ_VALUES = names("-f -s -w --skip-fields --skip-chars --check-chars")


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
