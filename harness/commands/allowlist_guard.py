"""你加的"不用问的命令"（allowlist.py）的两道关：哪些条目根本加不进来（refusal），对上以后哪些这一次不算（spoiled）。

清单里的一条是前缀：命令开头几个词和它一样就算，后面跟什么参数都行。这很方便（加了 npm test，npm test -- --watch 也不问），
口子也正在这里：后面多出来的参数是模型写的。2026-11-25 的安全复查找到三样，这里一样一样挡：

  1. 运行器（RUNS）。npx、uv run、python 的本事就是"运行后面写的那一个"：加了 uv run，就等于 uv run 任何命令都不问。
     所以运行器后面要紧跟着一个写死的名字才收（npx eslint、uv run pytest、python manage.py check、go run .）；
     写到运行器为止，或者它后面先跟一个选项（npx --package=别的包、uv run --with、python -c：选项能决定运行什么），就不收。
     python -m 模块 例外：模块的名字也是写死的。写死的那个名字本身也照样检查一遍：uv run python 就是光写的 python，
     uv run bash 就是 bash，python -m uv 就是 uv。
     有几样后面写什么都不行（NEVER）：npm exec 写明了名字也拦不住（npm 在后面任何位置都认它自己的选项，--package 换一个包、
     --call 换一条命令），yarn exec 把后面的词当一段 shell 脚本，git config 写进去的设置以后会运行命令……
     有这些子命令的程序（uv、npm、git……）光写程序名、或者子命令前面先写选项（git -c、npm --prefix），也不收：
     后面接上 run、exec、config 就是它们了（代价：pnpm --filter web test、docker compose -f x.yml up 这种也加不进来）。
     先选一个范围再写子命令的（SCOPES：yarn workspace 名字 子命令）去掉范围再看。
     能运行任何命令的程序（RUNNERS：bash、sudo、env、xargs……）不管后面写什么都不收：加了就等于"全部自动允许"。
  2. 内置已经放开了只读用法的程序（BUILTIN：rules.py 的 RULES 里的 find、sed、grep、ls……）。只读的用法不用加就不问；
     加进清单只会多放开内置规则不放的那些：find -exec、sed -i 会运行命令、改文件；grep -r、ls -R 走遍文件夹时，
     内置的规则会先看里面有没有 .env、数据目录，你加的命令只看写出来的那几个路径。所以它们一律加不进来。
     有子命令的（git、systemctl、docker、kubectl）例外：git pull、systemctl restart nginx 是你可能真想放开的改动，
     子命令写死了；后面的参数照第 3 条看。只许查版本的（python --version，rules.VERSIONS）也例外，它们照第 1 条看。
  3. 对上以后，后面多出来的参数（spoiled）：
     - 能让它运行别的命令的选项（SPOILERS：git pull --upload-pack、git rebase -x、make --eval、go test -exec、tar --to-command……）。
       条目里本身就有的也不收：git rebase -x 写到这里，后面那个词就是要运行的命令。
     - 指到工作目录之外、已经存在的地方（npm test --prefix ../别的项目、make check -C ../x、cargo test --manifest-path …）。
       你加的命令只在工作目录里才算（见 paths.at_workspace），就是因为 npm test 跑的是这里的 package.json；
       指到别处，跑的就是那里的了（比如你下载回来的、带着提示词注入的仓库）。路径照 Places 的算法（相对命令运行的目录、
       展开 ~、顺着符号链接），不存在的词（--grep foo 的 foo）不管。条目里你自己写的路径不算（make -C ../shared check）。
     这一次不算，就照内置的规则判断（多半是问你），和没加过一样。

为什么单独一个文件：allowlist.py 管清单存在哪儿、怎么改；这里只管"哪些危险"，表和判断放在一起才好一起看、一起改测试
（tests/test_allowlist_guard.py）。和整个 commands/ 一样，看不懂的一律不算，宁可多问。表里的程序和选项照各自的官方文档写
（每一组上面注明出处）；认不出的运行器、没列进来的选项照样加得进来，所以界面上照旧提醒：只加你放心它后面跟着任何东西的命令。
Windows 上你加的命令一条都不算（见 __init__.py），这里只照 bash 的写法看。
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import pathnames
from ..i18n import k, t
from .options import names
from .paths import Places
from .rules import RULES, VERSIONS
from .words import Unclear

# ---- 不收的原因（给人看，t() 译成界面的语言）--------------------------------------------

ANYTHING = k("“{program}”能运行任何命令，加进来就等于全部自动允许。真要这样，请选“全部自动允许”那一档。")
SCRIPT = k("“{program}”后面要写明运行哪个脚本（比如 python manage.py check）：光写它，或者用 -c、-e，就能运行任何代码。")
PACKAGE = k(
    "“{program}”后面要紧跟着写明运行哪一个（比如 npx eslint、uv run pytest、go run .）：光写到这里，或者它后面先跟一个选项"
    "（选项能决定运行什么），就能运行任何东西。"
)
PREFIX = k("“{entry}”还没写到子命令：后面接上“{runner}”就能运行任何东西。要写明是哪个子命令（比如 npm test、uv sync、git pull），子命令前面也不要写选项。")
NEVER_SAY = k("“{program}”加不进来：{why}")
BUILT_IN = k(
    "“{program}”只是看的用法已经内置了，不用加。加进来只会多放开内置规则不放的那些：find -exec、sed -i 这类会运行命令、改文件，"
    "grep -r、ls -R 这类走遍文件夹时也不再先看里面有没有 .env、助手自己的数据。"
)
SPOILER = k("“{entry}”里有 {option}：这个选项能让它运行别的命令，加不进来。")

# NEVER 里每一样为什么（填进 NEVER_SAY 的 {why}）
W_NPM = k("它在后面任何位置都认自己的选项（npm 的 --package、--call），写明了名字也拦不住它换成别的包、别的命令。npm exec 可以改用 npx 加上名字（比如 npx eslint）。")
W_OPTIONS = k("它在后面任何位置都认自己的选项，写明了名字也拦不住它换成别的包、别的命令。")
W_CREATE = k("npm init 名字（npm create）会下载并运行 create-名字 这个包，npm 还在后面任何位置都认自己的选项。")
W_ANY = k("它会运行后面写的任何命令。")
W_SHELL = k("它把后面的词当成一段 shell 脚本运行。")
W_EVAL = k("它后面是一段要运行的代码。")
W_GIT_CONFIG = k("写进去的设置（core.fsmonitor、core.pager、alias.名字=!命令……）以后会让 git 运行任何命令。")

# ---- 1. 运行器 -------------------------------------------------------------------------

# 能运行任何命令的程序：shell、提权、套在别的命令外面的。加进清单就等于全部自动允许，后面写什么都不收。
# 名字是比较时的样子（见 _names：小写、去掉版本号和 GNU 的 g 前缀）
RUNNERS = frozenset({
    # shell 和 busybox 这类一个程序装着一整套命令的
    "sh", "bash", "zsh", "fish", "dash", "ksh", "mksh", "ash", "yash", "csh", "tcsh", "nu", "xonsh", "elvish", "busybox", "toybox",
    # 提权、换身份
    "sudo", "sudoedit", "doas", "su", "pkexec", "runuser", "run0", "sg",
    # 套在别的命令外面：后面跟的就是要运行的命令（caffeinate 命令、sandbox-exec -p 策略 命令、xvfb-run 命令……）
    "env", "xargs", "nohup", "timeout", "nice", "ionice", "chrt", "taskset", "numactl", "exec", "eval", "source", ".", "command",
    "builtin", "time", "watch", "strace", "ltrace", "valgrind", "gdb", "lldb", "script", "expect", "unbuffer", "chroot", "unshare",
    "nsenter", "setsid", "stdbuf", "flock", "parallel", "caffeinate", "sandbox-exec", "systemd-run", "firejail", "bwrap", "fakeroot",
    "proot", "faketime", "torsocks", "proxychains", "rlwrap", "screen", "tmux", "xvfb-run", "dbus-run-session", "start-stop-daemon",
    "entr", "watchexec", "nix-shell", "dotenv", "env-cmd", "cross-env", "concurrently", "ssh",
})

# 解释器：后面紧跟着一个写死的脚本才收（python manage.py check）。光写它、或者先跟一个选项（-c、-e、--eval），就能运行任何代码
INTERPRETERS = (
    "python", "pypy", "ipython", "node", "nodejs", "ts-node", "tsx", "deno", "bun", "ruby", "irb", "perl", "php", "lua", "luajit",
    "osascript", "rscript", "r", "julia", "tclsh", "wish", "pwsh", "powershell", "jshell", "ghci", "scala", "groovy", "mawk", "nawk",
    "elixir", "erl", "racket", "guile", "sbcl",
)
PYTHONS = frozenset({"python", "pypy"})  # python -m 模块：模块的名字也是写死的

# 运行器：key 是程序名（比较时的样子）加上子命令，值是不收时说的那句话
RUNS: dict[tuple[str, ...], str] = {
    **{(name,): SCRIPT for name in INTERPRETERS},
    # npm（docs.npmjs.com/cli/commands/npx）：npx 包名 后面的参数原样交给它（npm exec 不一样，见 NEVER）
    ("npx",): PACKAGE,
    # pnpm（pnpm.io/cli/dlx、/cli/exec、/cli/create）；pnpx 是 pnpm dlx 的旧名字
    ("pnpx",): PACKAGE, ("pnpm", "dlx"): PACKAGE, ("pnpm", "exec"): PACKAGE, ("pnpm", "create"): PACKAGE,
    # yarn（yarnpkg.com/cli/dlx；yarn create 名字 运行 create-名字）
    ("yarn", "dlx"): PACKAGE, ("yarn", "create"): PACKAGE,
    # bun（bun.sh/docs/cli/bunx、/cli/run、/cli/bun-create）：bunx 是 bun x
    ("bunx",): PACKAGE, ("bun", "x"): PACKAGE, ("bun", "run"): PACKAGE, ("bun", "create"): PACKAGE,
    # deno（docs.deno.com/runtime/reference/cli/）：run、serve 后面的文件可以是网址；deno x（简写 dx）和 npx 一样
    ("deno", "run"): PACKAGE, ("deno", "serve"): PACKAGE, ("deno", "x"): PACKAGE, ("dx",): PACKAGE,
    # uv（docs.astral.sh/uv/reference/cli/）：uvx 是 uv tool run
    ("uv", "run"): PACKAGE, ("uv", "tool", "run"): PACKAGE, ("uvx",): PACKAGE,
    # Python 的别的项目工具：pipx run 包、poetry run / pdm run / hatch run / pipenv run / rye run 命令
    ("pipx", "run"): PACKAGE, ("poetry", "run"): PACKAGE, ("pdm", "run"): PACKAGE, ("hatch", "run"): PACKAGE,
    ("pipenv", "run"): PACKAGE, ("rye", "run"): PACKAGE,
    # conda run（docs.conda.io）、mamba、micromamba 同样；pixi run、pixi exec（pixi.sh/latest/reference/cli/）
    ("conda", "run"): PACKAGE, ("mamba", "run"): PACKAGE, ("micromamba", "run"): PACKAGE, ("pixi", "run"): PACKAGE, ("pixi", "exec"): PACKAGE,
    # go run 包@版本 下载并运行任何模块（pkg.go.dev/cmd/go）
    ("go", "run"): PACKAGE,
    # Ruby：bundle exec 命令（bundler.io）、gem exec 包（RubyGems 3.5 起）
    ("bundle", "exec"): PACKAGE, ("bundler", "exec"): PACKAGE, ("gem", "exec"): PACKAGE,
    # 版本管理器：照这里选的版本运行后面的命令
    ("asdf", "exec"): PACKAGE, ("rbenv", "exec"): PACKAGE, ("pyenv", "exec"): PACKAGE, ("nodenv", "exec"): PACKAGE,
    ("goenv", "exec"): PACKAGE, ("jenv", "exec"): PACKAGE, ("fnm", "exec"): PACKAGE, ("volta", "run"): PACKAGE,
    # 容器：docker run 镜像（docs.docker.com/reference/cli/docker/container/run/）。镜像写死了，选项（-v 挂上整个磁盘、
    # --privileged）在镜像前面，所以要紧跟着镜像的名字；镜像后面的命令在容器里运行
    ("docker", "run"): PACKAGE, ("podman", "run"): PACKAGE, ("docker", "compose", "run"): PACKAGE, ("docker-compose", "run"): PACKAGE,
}

# 后面写什么都不收的
NEVER: dict[tuple[str, ...], str] = {
    # npm 在后面任何位置都认它自己的选项（docs.npmjs.com/cli/commands/npm-exec："npm exec 要用 -- 才能让后面的选项不归 npm"）：
    # npm exec eslint --package=别的包 运行的是别的包里的 eslint，--call 换一条命令。npm init 名字 就是 npm exec create-名字
    ("npm", "exec"): W_NPM, ("npm", "x"): W_NPM,
    ("npm", "init"): W_CREATE, ("npm", "create"): W_CREATE, ("npm", "innit"): W_CREATE,
    ("npm", "explore"): W_ANY,  # npm explore 包 -- 命令
    # yarn exec（yarnpkg.com/cli/exec）、bun exec 把后面的词当一段 shell 脚本；deno eval 后面是代码
    ("yarn", "exec"): W_SHELL, ("bun", "exec"): W_SHELL, ("deno", "eval"): W_EVAL,
    # git（git-scm.com/docs/git-config、git-submodule#foreach、git-bisect#_bisect_run）
    ("git", "config"): W_GIT_CONFIG, ("git", "submodule", "foreach"): W_ANY, ("git", "bisect", "run"): W_ANY,
    # 命令写在 -- 后面、或者写在一个目录、一段配置后面的：mise exec 工具 -- 命令、direnv exec 目录 命令、
    # aws-vault exec 配置 -- 命令、op run -- 命令、doppler run -- 命令
    ("mise", "exec"): W_ANY, ("mise", "x"): W_ANY, ("direnv", "exec"): W_ANY, ("aws-vault", "exec"): W_ANY,
    ("op", "run"): W_ANY, ("doppler", "run"): W_ANY,
    # nix 的新命令在后面任何位置都认自己的选项（nix run 包 --expr …）；nix shell、nix develop 用 --command 运行任何命令
    ("nix", "run"): W_OPTIONS, ("nix", "shell"): W_ANY, ("nix", "develop"): W_ANY,
    # 在每个工作区、每个包里运行后面写的命令：yarn workspaces foreach 命令、lerna exec -- 命令、nx exec -- 命令
    ("yarn", "workspaces", "foreach"): W_ANY, ("lerna", "exec"): W_ANY, ("nx", "exec"): W_ANY,
    # 在容器、集群里运行后面写的任何命令，和 ssh 一样
    ("docker", "exec"): W_ANY, ("podman", "exec"): W_ANY, ("docker", "compose", "exec"): W_ANY, ("docker-compose", "exec"): W_ANY,
    ("kubectl", "exec"): W_ANY, ("kubectl", "run"): W_ANY, ("kubectl", "debug"): W_ANY,
}

_SEQUENCES = sorted([*RUNS, *NEVER], key=len, reverse=True)  # 长的先比：bun x 要先于 bun

# 先选一个范围、再接着写子命令的：yarn workspace 名字 子命令（yarnpkg.com/cli/workspace）。去掉中间这几个词，照"yarn 子命令"看：
# yarn workspace web test 就是 yarn test，yarn workspace web exec 就是 yarn exec。值是范围后面跟着几个名字
SCOPES: dict[tuple[str, ...], int] = {("yarn", "workspace"): 1}

# ---- 2. 内置已经放开了只读用法的程序 -------------------------------------------------------

# 有子命令的：子命令写死了，加进来的多半是你想放开的改动（git pull、systemctl restart nginx），照第 1、3 条看
SUBCOMMANDS = frozenset({"git", "systemctl", "docker", "kubectl"})
BUILTIN = frozenset(name for name in RULES if name not in VERSIONS and name not in SUBCOMMANDS and name not in RUNNERS)

# ---- 3. 能让它运行别的命令的选项 -----------------------------------------------------------


@dataclass(frozen=True)
class Spoils:
    """一个程序（或者它的一个子命令）后面不能有的东西。长选项连缩写一起算（GNU 的 getopt、git、npm 都认缩写），
    短选项也认写在一串里的（-ix 里的 -x）。宁可多认：认错了只是多问一次。"""

    options: frozenset[str] = frozenset()
    go: bool = False  # Go 的写法：-exec 和 --exec 一样，不认缩写，也没有一串短选项
    assignments: bool = False  # make：后面的 NAME=value 会换掉 Makefile 里的变量（SHELL、.SHELLFLAGS、CC……都是要运行的命令）
    colon: bool = False  # 冒号前面没有 / 的词：tar 当成"另一台电脑上的文件"（要用 rsh 去连），mvn 当成"插件:目标"（下载并运行那个插件）
    old_style: bool = False  # tar 的老写法：第一个参数不带 -，也是一串短选项（tar cIf 程序 x.tar）


_GIT = {
    # git-scm.com/docs：--upload-pack、--receive-pack、--exec 换成任何程序（对着本机的仓库时就在这台电脑上运行）。
    # clone、ls-remote 上 -u 是 --upload-pack（push 的 -u 是 --set-upstream，fetch 的是 --update-head-ok，不算）；
    # clone 的 -c / --config 写进新仓库的设置（core.fsmonitor、core.hooksPath）、--template 带进钩子
    "clone": "-u --upload-pack -c --config --template",
    "fetch": "--upload-pack",
    "pull": "--upload-pack",
    "ls-remote": "-u --upload-pack",
    "push": "--receive-pack --exec",
    "archive": "--exec",
    "fetch-pack": "--upload-pack --exec",
    "send-pack": "--receive-pack --exec",
    "rebase": "-x --exec",  # 每一步之后运行一条命令
    "grep": "-O --open-files-in-pager",  # 用任何程序打开找到的文件
    "difftool": "-x --extcmd",
    "filter-branch": "--setup --env-filter --tree-filter --index-filter --parent-filter --msg-filter --commit-filter --tag-name-filter",
    "send-email": "--to-cmd --cc-cmd --header-cmd --sendmail-cmd --smtp-server",
}
_TAR = Spoils(
    # GNU tar（gnu.org/software/tar/manual）：--to-command、-I / --use-compress-program、--checkpoint-action=exec=…、
    # -F / --info-script / --new-volume-script 运行程序，--rsh-command、--rmt-command 换掉连别的电脑用的程序。
    # bsdtar（macOS 自带的）认 --use-compress-program
    names("--to-command --use-compress-program -I --checkpoint-action --info-script --new-volume-script -F --rsh-command --rmt-command"),
    colon=True,
    old_style=True,
)
_MAVEN = Spoils(colon=True)  # mvn test exec:exec -Dexec.executable=…：后面多写一个"插件:目标"，就下载并运行那个插件
SPOILERS: dict[tuple[str, ...], Spoils] = {
    **{("git", command): Spoils(names(options)) for command, options in _GIT.items()},
    # GNU make（gnu.org/software/make/manual）：--eval / -E 把一段字当 Makefile 读（$(shell …) 运行任何命令）；
    # 命令行上的 NAME=value 换掉 Makefile 里的变量
    ("make",): Spoils(names("--eval -E"), assignments=True),
    ("tar",): _TAR, ("bsdtar",): _TAR,
    # go（pkg.go.dev/cmd/go）：-exec 用别的程序运行测试，-toolexec 用别的程序运行编译器，-vettool 换掉 vet，
    # -ldflags 里的 -extld 换掉外部链接程序
    ("go",): Spoils(names("-exec -toolexec -vettool -ldflags"), go=True),
    # cargo（doc.rust-lang.org/cargo/reference/config.html）：--config 能写 target.….runner、build.rustc-wrapper（运行任何程序）；
    # -Z 是还没定下来的功能，看不准
    ("cargo",): Spoils(names("--config -Z")),
    # npm（docs.npmjs.com/cli/using-npm/config）：在任何子命令后面都认自己的设置。--node-options 交给 Node（--import、--require
    # 能先运行一段代码），--script-shell 换掉运行脚本的 shell
    ("npm",): Spoils(names("--node-options --script-shell")),
    # rsync（-e / --rsh 换掉连别的电脑用的程序，--rsync-path 换掉那边运行的程序）；scp、sftp（-S 换掉 ssh，
    # -o ProxyCommand=… 在这台电脑上运行命令，sftp 的 -D 直接运行一个程序当服务端）
    ("rsync",): Spoils(names("-e --rsh --rsync-path")),
    ("scp",): Spoils(names("-S -o")), ("sftp",): Spoils(names("-S -o -D")),
    ("mvn",): _MAVEN, ("mvnw",): _MAVEN,
    # rake（ruby.github.io/rake）：-e / --execute、-p、-E 运行一段 Ruby 代码，-r / --require 先加载一个库
    ("rake",): Spoils(names("-e --execute -p --execute-print -E --execute-continue -r --require")),
    # just（just --help）：--shell、--shell-arg 换掉运行配方的 shell，--command 运行任何命令；名字=值 换掉 justfile 里的变量
    ("just",): Spoils(names("--shell --shell-arg --command -c"), assignments=True),
}

# ---- 名字 ------------------------------------------------------------------------------

_VERSION = re.compile(r"[-_.]?\d+(?:\.\d+)*$")


def _names(word: str) -> set[str]:
    """一个程序名比较时的几种样子：写了完整路径的取最后一段（/usr/bin/python3），不分大小写（macOS 上 PYTHON3 就是 python3），
    去掉版本号（python3.12、pip3、node20 → python、pip、node），去掉 Homebrew 给 GNU 版本加的 g（gsed、gfind、gtimeout）。
    几种样子里有一个在表里就算：多认出一个只会多拒绝一条。"""
    name = os.path.basename(word).casefold().removesuffix(".exe")
    found = {name, _VERSION.sub("", name) or name}
    found |= {one[1:] for one in found if one.startswith("g") and len(one) >= 3}
    return found


# ---- 1、2：哪些条目加不进来 ---------------------------------------------------------------


def refusal(entry: Sequence[str]) -> str | None:
    """一条要加进清单的命令（拆好的词）为什么加不进来（t() 给的一句话）；加得进来是 None。"""
    words = list(entry)
    nested = False
    while words:
        if nested and words[0] in (".", ".."):  # go run .、deno run ..：运行器后面的是这个文件夹，不是 shell 的 . 命令
            return None
        found = _names(words[0])
        if found & RUNNERS:
            return t(ANYTHING, program=words[0])
        if found & BUILTIN:
            return t(BUILT_IN, program=words[0])
        scoped = _unscoped(words, found)
        if scoped is not None:
            words = scoped
            continue
        reason, rest = _runner(words, found)
        if reason:
            return reason
        if not rest:
            return _spoiler_in(words)
        words, nested = rest, True  # 运行器后面写死的那个名字：它本身也是一条命令，接着看（uv run python、python -m uv）
    return None


def _unscoped(words: list[str], found: set[str]) -> list[str] | None:
    """yarn workspace 名字 子命令 → yarn 子命令（见 SCOPES）；还没写到名字的（yarn workspace）→ 光写的 yarn。不是这样的写法是 None。"""
    for scope, names_after in SCOPES.items():
        if scope[0] in found and [word.casefold() for word in words[1 : len(scope)]] == list(scope[1:]):
            return [words[0], *words[len(scope) + names_after :]]
    return None


def _runner(words: list[str], found: set[str]) -> tuple[str, list[str]]:
    """words 是不是从一个运行器开始：（不收的原因，运行器后面写死的那条命令）。不是运行器时两样都是空的。"""
    prefix: tuple[str, ...] | None = None
    for sequence in _SEQUENCES:
        if sequence[0] not in found:
            continue
        at = 1
        while at < len(sequence) and at < len(words) and words[at].casefold() == sequence[at]:
            at += 1
        if at < len(sequence):
            # 还没写到这个子命令：写到这里为止，或者子命令的位置上是一个选项（git -c、uv --quiet），后面就能接上它
            if (at == len(words) or words[at].startswith("-")) and (prefix is None or len(sequence) < len(prefix)):
                prefix = sequence
            continue
        program = " ".join(words[: len(sequence)])
        if sequence in NEVER:
            return t(NEVER_SAY, program=program, why=t(NEVER[sequence])), []
        rest = words[len(sequence) :]
        if len(sequence) == 1 and sequence[0] in PYTHONS and rest[:1] == ["-m"] and len(rest) >= 2 and not rest[1].startswith("-"):
            return "", rest[1:]
        if not rest or rest[0].startswith("-"):
            return t(RUNS[sequence], program=program), []
        return "", rest
    if prefix is not None:
        return t(PREFIX, entry=" ".join(words), runner=" ".join(prefix)), []
    return "", []


def _inner(words: list[str]) -> int:
    """一条加得进来的命令里，运行器后面写死的那条命令从第几个词开始（uv run make check 是 2）；不是运行器是 0。"""
    start = 0
    while start < len(words):
        _, rest = _runner(words[start:], _names(words[start]))
        if not rest:
            return start
        start = len(words) - len(rest)
    return start


def _spoils_of(words: list[str]) -> tuple[Spoils, int] | None:
    """一条命令（从程序名开始）对应 SPOILERS 里的哪一项，以及程序名加子命令占了几个词。"""
    found = _names(words[0])
    for name in found:
        if len(words) > 1 and (name, words[1].casefold()) in SPOILERS:
            return SPOILERS[(name, words[1].casefold())], 2
    for name in found:
        if (name,) in SPOILERS:
            return SPOILERS[(name,)], 1
    return None


def _spoiler_in(words: list[str]) -> str | None:
    """条目本身里就有能让它运行别的命令的选项（git rebase -x 写到这里，后面那个词就是要运行的命令）。"""
    spoils = _spoils_of(words)
    if spoils is None:
        return None
    rules, size = spoils
    args = list(words[size:])
    if rules.old_style and size == 1 and args and not args[0].startswith("-"):
        args[0] = "-" + args[0]
    hit = _hit(rules, args, trailing=False)
    return t(SPOILER, entry=" ".join(words), option=hit) if hit else None


def _hit(rules: Spoils, args: list[str], trailing: bool) -> str:
    """args 里第一个能让它运行别的命令的词；没有是空字符串。trailing：args 是不是对上以后多出来的（变量、冒号只在这时算：
    条目里的 make check CC=clang、tar -czf 主机:x 是你自己写死的）。-- 之后也照样看：宁可多问。"""
    for word in args:
        if word.startswith("-") and word.strip("-") and _named(rules, word):
            return word
        if trailing and rules.assignments and "=" in word and not word.startswith("-"):
            return word
        if trailing and rules.colon and ":" in word.split("/", 1)[0]:
            return word
    return ""


def _named(rules: Spoils, word: str) -> bool:
    """一个像选项的词是不是 rules 里的某一个。下划线当连字符（npm 认 --node_options）。"""
    if rules.go:
        return "-" + word.lstrip("-").partition("=")[0].replace("_", "-") in rules.options
    if word.startswith("--"):
        name = word.partition("=")[0].replace("_", "-")
        return any(option.startswith(name) for option in rules.options if option.startswith("--"))
    return any("-" + char in rules.options for char in word[1:])


# ---- 3：对上以后，后面多出来的参数 ----------------------------------------------------------


def spoiled(entry: Sequence[str], words: Sequence[str], places: Places) -> str:
    """一条你加的命令（entry）对上了 words（这条命令拆成的词，开头就是 entry）：后面多出来的参数会不会让它运行别的命令、
    去工作目录之外运行。会的话返回为什么（给测试和排查看的中文，不显示在界面上），不会是空字符串。"""
    head, command = list(entry), list(words)
    trailing = command[len(head) :]
    if not trailing:
        return ""
    inner = _inner(head)
    spoils = _spoils_of(command[inner:])
    if spoils is not None:
        rules, size = spoils
        args = list(trailing)
        if rules.old_style and size == 1 and len(head) == inner + 1 and not args[0].startswith("-"):
            args[0] = "-" + args[0]  # tar 后面第一个参数是老写法的一串短选项
        hit = _hit(rules, args, trailing=True)
        if hit:
            if hit.startswith("-"):
                return f"后面的 {hit} 能让它运行别的命令"
            if rules.assignments and "=" in hit:
                return f"后面的 {hit} 会换掉 Makefile 里的变量（变量里写的也是要运行的命令）"
            return f"后面的 {hit} 是另一台电脑上的文件，或者要下载、运行的插件"
    for word in trailing:
        if _elsewhere(word, places):
            return f"后面的 {word} 指到工作目录之外已有的地方：那里的脚本、设置不是你允许过的"
    return ""


def _elsewhere(word: str, places: Places) -> bool:
    """这个词（和它里面藏着的每一段路径，见 _spots）是不是一个已经存在、在工作目录之外的地方。看不清指到哪里的也算（宁可多问）。"""
    root = _settled(places.workspace or places.cwd)
    for spot in _spots(word):
        try:
            path = places.resolve(spot)
            if not path.exists():
                continue
        except (Unclear, OSError):
            return True
        if not pathnames.within(path, root, places.windows):
            return True
    return False


def _spots(word: str) -> list[str]:
    """一个词里可能藏着的路径：它本身；用空白、逗号隔开的每一段（--node-options='--require ../x.js'）；每个 = 和 : 后面的部分
    （--prefix=../x、file:///x）；去掉开头的 @ 以后的部分；一个横线的选项里紧贴着写的值（-C../x、-f/abs/Makefile、-sC../x：
    不知道哪个字母带值，每个位置都试）。多试几个只会多问，少试一个就可能漏掉。"""
    found = [word, *(piece for piece in re.split(r"[\s,]+", word) if piece)]
    for text in list(found):
        found.extend(text[at + 1 :] for at, char in enumerate(text) if char in "=:")
    found.extend(text[1:] for text in list(found) if text.startswith("@"))
    found.extend(text[at:] for text in list(found) if text.startswith("-") and not text.startswith("--") for at in range(2, len(text)))
    return [spot for spot in dict.fromkeys(found) if spot.strip("-")]


def _settled(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        return path
