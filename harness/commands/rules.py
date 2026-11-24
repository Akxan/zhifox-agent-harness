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

## 由几个文件拼成

这个文件是入口：上面说的写法（Context、Program，以及只许查版本的 VERSIONS），和把每个程序对上它那条规矩的总表 RULES。
"自己看一遍"的那些函数和它们用的选项表，按程序的种类写在同一前缀的几个文件里：

  rules_system.py       报告系统情况的：date、top、sysctl、ps、ip
  rules_files.py        读文件的：xxd、ls、tree、du、diff（还有 sort、uniq 的选项表）
  rules_search.py       搜索的：grep、rg、find
  rules_text.py         编辑文字的（只许"读进来、改了打印出去"）：sed、awk、jq
  rules_subcommands.py  有子命令的：git、systemctl、docker、kubectl、crontab
  rules_wrappers.py     套在别的命令外面的：env、timeout

它们只 import options.py、words.py，只在 if TYPE_CHECKING 里 import 这里的 Context（给类型注解用）：运行时 import 这个文件，
就成了互相导入（这里要 import 它们的函数来拼 RULES）。所以用 Program(...) 写、要起名字的规矩（ANY、NO_OPERANDS、CHECKSUM、
JOURNALCTL）都在这个文件里，拆出去的文件里一个 Program 都不建。
加一个程序：简单的在 RULES 里写一行 Program(...)；要自己看一遍的，函数写进它那一类的 rules_*.py，再在 RULES 里登记。
别处照旧 from .rules import RULES, Context（原来的每一个公开名字都写在 __all__ 里）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .options import Parsed, allow, deny, names, parse
from .paths import Places
from .rules_files import (
    DIFF_ALLOW,
    DIFF_VALUES,
    DU_ALLOW,
    DU_VALUES,
    LS_ALLOW,
    LS_VALUES,
    SORT_VALUES,
    TREE_ALLOW,
    TREE_VALUES,
    UNIQ_VALUES,
    _diff,
    _du,
    _xxd,
)
from .rules_search import (
    FIND_BEFORE,
    FIND_FILES,
    FIND_ONE_VALUE,
    FIND_TESTS,
    GREP_ALLOW,
    GREP_VALUES,
    PATTERNS,
    RG_ALLOW,
    RG_VALUES,
    _find,
    _grep,
    _rg,
)
from .rules_subcommands import (
    DOCKER_GROUPS,
    DOCKER_READS,
    GIT_DENY,
    GIT_LIST_ONLY,
    GIT_READS,
    KUBECTL_ALLOW,
    KUBECTL_READS,
    KUBECTL_VALUES,
    SYSTEMCTL_ALLOW,
    SYSTEMCTL_READS,
    SYSTEMCTL_VALUES,
    _crontab,
    _docker,
    _git,
    _kubectl,
    _systemctl,
)
from .rules_system import (
    IP_OBJECTS,
    IP_OPTIONS,
    IP_VERBS,
    _date,
    _ip,
    _ps,
    _sysctl,
    _top,
)
from .rules_text import (
    AWK_FORBIDDEN,
    AWK_VALUES,
    JQ_ALLOW,
    JQ_FORBIDDEN,
    SED_ALLOW,
    SED_COMMANDS,
    SED_VALUES,
    _awk,
    _jq,
    _sed,
)
from .rules_wrappers import TIMEOUT_OPTIONS, _env, _timeout
from .words import Unclear

__all__ = [
    "ANY",
    "AWK_FORBIDDEN",
    "AWK_VALUES",
    "CHECKSUM",
    "DIFF_ALLOW",
    "DIFF_VALUES",
    "DOCKER_GROUPS",
    "DOCKER_READS",
    "DU_ALLOW",
    "DU_VALUES",
    "FIND_BEFORE",
    "FIND_FILES",
    "FIND_ONE_VALUE",
    "FIND_TESTS",
    "GIT_DENY",
    "GIT_LIST_ONLY",
    "GIT_READS",
    "GREP_ALLOW",
    "GREP_VALUES",
    "IP_OBJECTS",
    "IP_OPTIONS",
    "IP_VERBS",
    "JOURNALCTL",
    "JQ_ALLOW",
    "JQ_FORBIDDEN",
    "KUBECTL_ALLOW",
    "KUBECTL_READS",
    "KUBECTL_VALUES",
    "LS_ALLOW",
    "LS_VALUES",
    "NO_OPERANDS",
    "PATTERNS",
    "RG_ALLOW",
    "RG_VALUES",
    "RULES",
    "SED_ALLOW",
    "SED_COMMANDS",
    "SED_VALUES",
    "SORT_VALUES",
    "SYSTEMCTL_ALLOW",
    "SYSTEMCTL_READS",
    "SYSTEMCTL_VALUES",
    "TIMEOUT_OPTIONS",
    "TREE_ALLOW",
    "TREE_VALUES",
    "UNIQ_VALUES",
    "VERSIONS",
    "Context",
    "Program",
]


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
# 下面两条照内容属于"读文件的"（rules_files.py）和"有子命令的"（rules_subcommands.py），因为是 Program 的实例写在这里（见文件开头）
CHECKSUM = Program(deny=names("-c --check"))  # -c：按一个文件里列的名字去读别的文件

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
        # 查版本也会照着所在文件夹里的设置换一个程序来运行（yarn 的 yarn-path，rustup、asdf、pyenv 这类）：只在工作目录里才算（见 paths.at_workspace）
        if not context.places.at_workspace():
            raise Unclear(f"{program} 不在工作目录里运行：那里的设置可能让它运行别的程序")

    return check


RULES: dict[str, Callable[[list[str], Context], None]] = {
    # 报告系统的情况（自己看一遍的函数在 rules_system.py）
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
    # 读文件（rules_files.py）
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
    # 搜索（rules_search.py）
    "grep": _grep, "egrep": _grep, "fgrep": _grep, "rg": _rg, "find": _find,
    # 编辑文字（只许打印出来，rules_text.py）
    "sed": _sed, "awk": _awk, "jq": _jq,
    # 有子命令的（rules_subcommands.py）
    "git": _git, "systemctl": _systemctl, "journalctl": JOURNALCTL, "docker": _docker, "kubectl": _kubectl, "crontab": _crontab,
    # 套在外面的（rules_wrappers.py）
    "env": _env, "timeout": _timeout,
    **{program: _version(program) for program in VERSIONS},
}
