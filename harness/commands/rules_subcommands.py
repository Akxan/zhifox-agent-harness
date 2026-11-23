"""只读的命令 · 有子命令的程序：git、systemctl、docker、kubectl、crontab（规矩的写法和总表 RULES 见 rules.py 开头）。

每个只许几个"只是看"的子命令（GIT_READS、SYSTEMCTL_READS……），前面的全局选项大多不许（git -c、docker -H 能换设置、连别的机器）；
会打出环境变量、密码、带 token 的地址的（systemctl show、docker inspect、kubectl 碰到 Secret、git remote -v）也不算。

为什么单独一个文件：这几个先认子命令、再按子命令分开判断，写法和别的程序不一样。
journalctl 照内容属于这里，因为只用一个 Program 就写完了（JOURNALCTL），写在 rules.py（见那里开头）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .options import allow, deny, names, parse
from .words import Unclear

if TYPE_CHECKING:
    from .rules import Context


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
