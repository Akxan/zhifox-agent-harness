"""commands/ 的测试：一条 shell 命令是不是"只读的"（不问就可以运行）。

主体是一张大表：每一行是一条命令和它该得到的结论（算只读 / 不算），id 写着为什么。
表里有三种行：

  ok(...)      算只读：只是看，不改东西、不运行别的程序、不联网、不碰密钥和数据目录。
  no(...)      不算只读。
  strict(...)  其实只是看，但现在照样要问（往"多问"那边偏的代价）。写进表里是为了让人看得到这些代价；
               以后要放宽的话，把它改成 ok。
  bypass(...)  找到的漏洞：分类器现在说"只读"，实际上会写文件、运行命令、读到密钥或者数据目录。
               期望写的是正确的结论（不算只读），所以分类器修好之前这些行会失败。id 以"绕过："开头。

所有路径都对着一个临时的工作目录（见 spot）：里面有 .env、data/（智狐的数据目录）、src/、.git/config、
指向 .env 和 data/ 的符号链接、硬链接；还有一个假的主目录（.ssh/id_rsa）。分类器只判断，从不运行命令。
"""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path

import pytest
from conftest import NO_SYMLINKS, symlinks_work

from harness import commands as commands_module
from harness.commands import Places, Unclear, classify
from harness.commands import paths as paths_module
from harness.commands.options import allow, deny, names, parse
from harness.commands.words import stages
from harness.config import READONLY_MAX_CHARS

# ---- 工作目录 -----------------------------------------------------------------

# _build 里符号链接的名字（mixed 是里面藏着一个链接的文件夹）。建不了链接的电脑上（见 conftest 的 symlinks_work），
# 表里提到它们的行跳过：没有链接，这些行要测的东西就不存在了
LINKS = ("envlink", "datalink", "innocent.txt", "mixed", "sshlink")


def _build(root: Path) -> Places:
    """搭一个像样的工作目录和主目录，返回对应的 Places。"""
    ws, home = root / "ws", root / "home"
    for folder in ("src", "docs", "data/conversations", ".git", "mixed", "linked"):
        (ws / folder).mkdir(parents=True)
    (ws / ".env").write_text("ANTHROPIC_API_KEY=sk-fake\n", encoding="utf-8")
    for name in ("mcp.json", "channels.json", "commands.json"):
        (ws / "data" / name).write_text("{}\n", encoding="utf-8")
    (ws / "data" / "notes.md").write_text("私人笔记\n", encoding="utf-8")
    (ws / "data" / "conversations" / "c1.json").write_text("{}\n", encoding="utf-8")
    (ws / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (ws / "src" / "a.txt").write_text("a\nb\n", encoding="utf-8")
    (ws / "src" / "b.txt").write_text("b\nc\n", encoding="utf-8")
    (ws / "src" / "config.json").write_text('{"name": "x"}\n', encoding="utf-8")
    (ws / "docs" / "readme.md").write_text("# 说明\n", encoding="utf-8")
    (ws / ".git" / "config").write_text('[remote "origin"]\n\turl = https://user:token@example.invalid/x.git\n', encoding="utf-8")
    (ws / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (ws / "mixed" / "ok.txt").write_text("ok\n", encoding="utf-8")
    # 符号链接：名字看起来无害，指向的地方不能不问就读。建不了链接的电脑上不建，用到它们的行跳过（见 LINKS）
    if symlinks_work():
        (ws / "envlink").symlink_to(ws / ".env")
        (ws / "datalink").symlink_to(ws / "data")
        (ws / "innocent.txt").symlink_to(ws / "data" / "notes.md")
        (ws / "mixed" / "inner").symlink_to(Path("..") / "data")  # 文件夹里藏着一个指向 data/ 的链接
    # 硬链接：另一个名字，同一个文件（Windows 上也建得了）
    os.link(ws / ".env", ws / "linked" / "notes.txt")
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n", encoding="utf-8")
    (home / ".aws").mkdir()
    (home / ".aws" / "credentials").write_text("[default]\n", encoding="utf-8")
    (home / ".bashrc").write_text("alias ll='ls -l'\n", encoding="utf-8")
    (home / "notes.txt").write_text("主目录里的普通文件\n", encoding="utf-8")
    if symlinks_work():
        (ws / "sshlink").symlink_to(home / ".ssh")
    # 工作目录旁边：同一个项目的另一份副本（也有 .env），以及一份只有 data/ 的"双胞胎"（没有像密钥的名字）
    (root / "copy").mkdir()
    (root / "copy" / ".env").write_text("ANTHROPIC_API_KEY=sk-old\n", encoding="utf-8")
    (root / "twin" / "data").mkdir(parents=True)
    (root / "twin" / "data" / "notes.md").write_text("另一份\n", encoding="utf-8")
    (root / "empty").mkdir()
    protected = [ws / ".env", ws / "data" / "mcp.json", ws / "data" / "channels.json", ws / "data" / "commands.json"]
    return Places(ws, protected=protected, private=[ws / "data"], home=home)


@pytest.fixture(scope="module")
def spot(tmp_path_factory):
    """(places, fill)：fill 把命令里的 {WS}、{HOME}、{ROOT} 换成真正的路径。"""
    root = tmp_path_factory.mktemp("cmds").resolve()
    places = _build(root)

    def fill(command: str) -> str:
        return command.replace("{WS}", str(places.cwd)).replace("{HOME}", str(places.home)).replace("{ROOT}", str(root))

    return places, fill


def ok(command: str, why: str):
    return pytest.param(command, True, id=why)


def no(command: str, why: str):
    return pytest.param(command, False, id=why)


# 只在 macOS、Linux 上成立的几行（Places 在 Windows 上按 Windows 的规矩看路径，见 harness/pathnames.py）
POSIX_DEVICES = "Windows 上没有 /dev、/proc：这些路径在那里只是当前盘上的普通文件夹，读它们不碰设备"
AUX_DEVICE = "Windows 上 aux 是设备名（AUX）：当作路径时看不清是什么，要问"


def posix(param, reason: str):
    """这一行只在 macOS、Linux 上成立：Windows 上跳过，写明为什么。"""
    return pytest.param(*param.values, id=param.id, marks=pytest.mark.skipif(os.name == "nt", reason=reason))


def strict(command: str, why: str):
    return pytest.param(command, False, id="保守：" + why)


def bypass(command: str, why: str):
    return pytest.param(command, False, id="绕过：" + why)


# ---- 表 -----------------------------------------------------------------------

QUOTING = [
    ok("cat 'src/a.txt'", "单引号"),
    ok('cat "src/a.txt"', "双引号"),
    ok("cat 'a b'", "单引号里的空格"),
    ok(r'cat "a\"b"', "双引号里转义的双引号"),
    ok(r"cat a\ b", "反斜杠转义空格"),
    ok("cat '$HOME'", "单引号里的 $ 原样不动"),
    ok("echo '$(rm -rf ~)'", "单引号里的命令替换只是文字"),
    ok("echo '`rm -rf ~`'", "单引号里的反引号只是文字"),
    ok(r"echo \$HOME", "反斜杠转义的 $ 只是文字"),
    ok(r"echo \;", "反斜杠转义的分号只是文字"),
    ok("echo 'it''s'", "两段单引号连成一个词"),
    ok("echo ''", "空的单引号"),
    ok('echo ""', "空的双引号"),
    ok("ls a#b", "词中间的 # 不是注释"),
    ok("ls ~", "~ 是主目录"),
    ok("ls ~/", "~/ 也是"),
    ok("ls\tsrc", "制表符分隔"),
    ok("grep 错误 src/a.txt", "中文参数"),
    ok("echo café", "带音调的字母"),
    ok("ls src；rm", "全角分号只是文件名的一部分"),
    no('cat "$HOME"', "双引号里的变量会展开"),
    no('echo "$(rm -rf ~)"', "双引号里的命令替换"),
    no('echo "`id`"', "双引号里的反引号"),
    strict(r'echo "\$HOME"', "双引号里转义的 $ 也不认"),
    no("cat .e\\nv", "反斜杠拼出来的 .env"),
    no("cat '.e''nv'", "引号拼出来的 .env"),
    no('cat ".env"', "双引号里的 .env"),
    no("echo 'open", "单引号没配对"),
    no('echo "open', "双引号没配对"),
    no("echo trailing\\", "以反斜杠结尾"),
]

OPERATORS = [
    no("cat $(echo .env)", "命令替换"),
    no("cat `echo .env`", "反引号"),
    no("echo $HOME", "变量"),
    no("echo ${HOME}", "花括号变量"),
    no("ls; rm -rf ~", "分号"),
    no("ls && rm x", "&&"),
    no("ls || rm x", "||"),
    no("ls & rm x", "放到后台"),
    no("ls |& cat", "|&"),
    no("echo hi > f", "重定向写文件"),
    no("echo hi >> f", "追加写文件"),
    no("cat < .env", "重定向读文件"),
    no("cat <<< x", "here-string"),
    no("cat <(ls)", "进程替换"),
    no("ls 2>&1", "重定向文件描述符"),
    no("ls >/dev/null", "重定向到 /dev/null 也不认"),
    no("(ls)", "子 shell"),
    no("{ ls; }", "命令组"),
    no("ls *", "通配符 *"),
    no("ls ?", "通配符 ?"),
    no("ls [ab]", "通配符 ["),
    no("ls a{b,c}", "花括号展开"),
    no("ls # x", "注释"),
    no("ls ~root", "~user"),
    no("ls ~+", "~+"),
    no("ls ~-", "~-"),
    no("", "空命令"),
    no("   ", "只有空格"),
    no("ls |", "管道右边是空的"),
    no("| ls", "管道左边是空的"),
    no("! ls", "! 不是程序"),
    no("time ls", "time 是关键字"),
    no("exec ls", "exec"),
    no("eval ls", "eval"),
    no(". ./x", "source"),
    no("sudo ls", "sudo"),
    no("nohup ls", "nohup"),
    no("xargs ls", "xargs"),
    no("command ls", "command"),
    strict("cd src", "cd 不在名单里"),
]

INVISIBLE = [
    no("ｌｓ", "全角的 ls"),
    no("сat src/a.txt", "西里尔字母的 с"),
    no("ls；rm -rf ~", "全角分号不分隔命令：整个是一个不认识的程序"),
    no("ls\u200b", "零宽空格"),
    no("ls\u200d -la", "零宽连接符"),
    no("cat \u202esrc/a.txt", "从右往左的标记"),
    no("ls\u2066 -la", "方向隔离"),
    no("ls\u00a0-la", "不换行的空格"),
    no("ls\u3000-la", "全角空格"),
    no("ls\u2028", "行分隔符"),
    no("ls\ufeff", "BOM"),
    no("ls\nrm -rf ~", "换行"),
    no("ls\r", "回车"),
    no("ls\x0b", "垂直制表符"),
    no("ls\x00", "NUL"),
    no("ls\x1b[2J", "终端的转义序列"),
    posix(ok("echo " + "a" * (READONLY_MAX_CHARS - 5), "正好是长度上限"), "Windows 上一段超过 255 个字符的词当作看不清的路径（pathnames.word_trick）；那里的命令本来也一律要问"),
    no("echo " + "a" * (READONLY_MAX_CHARS - 4), "超过长度上限"),
]

WRAPPERS = [
    no("FOO=1 ls", "设置环境变量"),
    no("PAGER=sh git log", "PAGER"),
    no("LD_PRELOAD=/tmp/x.so ls", "LD_PRELOAD"),
    no("a+=1 ls", "+="),
    no("_X=1 ls", "下划线开头的名字"),
    ok("ls FOO=1", "参数里的 = 不是设置环境变量"),
    ok("env ls", "env 原样运行只读命令"),
    ok("env -- ls -la", "env --"),
    ok("env timeout 5 ls", "env 套 timeout"),
    no("env", "env 打印所有环境变量"),
    no("env -i ls", "env -i"),
    no("env -u PATH ls", "env -u"),
    no("env -S 'ls -l'", "env -S"),
    no("env FOO=1 ls", "env 设置变量"),
    no("env env", "env 里的 env"),
    no("env sh -c ls", "env 运行 shell"),
    no("env rm x", "env 运行 rm"),
    no("env cat .env", "env 后面那条命令照样检查路径"),
    no("printenv", "printenv"),
    ok("timeout 5 ls", "timeout"),
    ok("timeout 5s cat src/a.txt", "带单位的时长"),
    ok("timeout 0.5 ls", "小数的时长"),
    ok("timeout -s KILL 5 ls", "timeout -s"),
    ok("timeout --signal=KILL 5 ls", "timeout --signal="),
    ok("timeout -k 1 5 ls", "timeout -k"),
    ok("timeout --preserve-status 5 ls", "timeout --preserve-status"),
    ok("timeout 5 timeout 3 ls", "两层 timeout"),
    no("timeout 5 rm x", "timeout 后面是 rm"),
    no("timeout 5", "timeout 后面没有命令"),
    no("timeout ls", "timeout 没有时长"),
    no("timeout --sig=KILL 5 ls", "timeout 的缩写不认"),
    no("timeout 5 FOO=1 ls", "timeout 后面设置变量"),
    no("timeout 5 sh -c ls", "timeout 运行 shell"),
    no("timeout 1e3 ls", "看不懂的时长"),
    no("timeout 5 cat .env", "timeout 后面那条命令照样检查路径"),
]

PIPELINES = [
    ok("cat src/a.txt | grep a | wc -l", "三段都只读"),
    ok("ls | sort | uniq -c", "ls 排序计数"),
    ok("git log --oneline | head -5", "git 接 head"),
    posix(ok("ps aux | grep python", "ps 接 grep"), AUX_DEVICE),
    ok("journalctl -u nginx | tail -n 20", "journalctl 接 tail"),
    posix(ok("ps aux | awk '{print $2}'", "接 awk"), AUX_DEVICE),
    ok("cat src/config.json | jq .", "接 jq"),
    ok("cat src/a.txt | sed -n 2p", "接 sed"),
    no("cat src/a.txt | sh", "接 sh"),
    no("grep -r foo . | sh", "grep 整个工作目录再接 sh"),
    no("grep -r foo src | sh", "接 sh"),
    no("cat src/a.txt | python3", "接 python"),
    no("ls | xargs rm", "接 xargs"),
    no("cat .env | wc -l", "第一段读 .env"),
    no("cat src/a.txt | tee out", "接 tee"),
    no("echo x | sort -o out", "最后一段写文件"),
    no("ls | env", "最后一段打印环境变量"),
]

SYSTEM = [
    ok("pwd", "pwd"),
    ok("pwd -P", "pwd -P"),
    no("pwd x", "pwd 不带参数"),
    ok("whoami", "whoami"),
    no("whoami x", "whoami 不带参数"),
    ok("uname -a", "uname"),
    no("uname -a x", "uname 不带参数"),
    ok("uptime", "uptime"),
    no("uptime x", "uptime 不带参数"),
    ok("nproc", "nproc"),
    ok("free -h", "free"),
    strict("free -s 1", "free -s 的值被当成参数"),
    ok("lscpu", "lscpu"),
    no("lscpu -s /x", "lscpu 不带参数"),
    ok("sw_vers", "sw_vers"),
    ok("tty", "tty"),
    ok("locale -a", "locale"),
    no("locale x", "locale 不带参数"),
    ok("arch", "arch"),
    no("arch -x86_64 ls", "macOS 的 arch 会运行后面的程序"),
    ok("id", "id"),
    ok("id -u", "id -u"),
    ok("groups", "groups"),
    ok("users", "users"),
    ok("w", "w"),
    ok("who", "who"),
    ok("last", "last"),
    ok("cal", "cal"),
    ok("df -h", "df"),
    ok("vmstat", "vmstat"),
    ok("vm_stat", "vm_stat"),
    ok("iostat", "iostat"),
    posix(ok("ps aux", "ps aux"), AUX_DEVICE),
    ok("ps -ef", "ps -ef"),
    ok("pgrep -l python", "pgrep"),
    ok("pidof nginx", "pidof"),
    ok("lsof -i :8080", "lsof 的端口"),
    no("lsof +D data", "lsof 列数据目录里打开的文件"),
    ok("netstat -an", "netstat"),
    ok("lsblk", "lsblk"),
    ok("findmnt", "findmnt"),
    ok("echo hi", "echo"),
    ok(r"printf '%s\n' x", "printf"),
    ok("which git", "which"),
    ok("whereis ls", "whereis"),
    ok("type ls", "type"),
    ok("hostname", "hostname"),
    ok("hostname -f", "hostname -f"),
    no("hostname newname", "hostname 改名字"),
    no("hostname -F file", "hostname 从文件里改名字"),
    ok("date", "date"),
    ok("date +%F", "date 格式"),
    ok("date -u '+%Y-%m-%d %H:%M'", "date 带空格的格式"),
    ok("date -d yesterday +%F", "GNU date -d"),
    ok("date -j 0101", "macOS date -j 只换算"),
    no("date 0613162785", "date 设置系统时间"),
    no("date -s 2020-01-01", "date -s"),
    no("date --set=2020-01-01", "date --set"),
    no("date -f .env", "date -f 读 .env"),
    no("date -f %Y 2030", "macOS 不带 -j 的 -f 是设置时间"),
    ok("top -b -n 1", "Linux top 打印一次"),
    ok("top -l 1", "macOS top 打印一次"),
    no("top", "top 会一直等键盘"),
    no("top -b", "top -b 没有次数"),
    no("top -b -n x", "top 的次数不是数"),
    no("top -l 1 extra", "top 不带参数"),
    ok("ss -tlnp", "ss"),
    no("ss -K dst 1.2.3.4", "ss -K 关掉连接"),
    no("ss -tK", "ss 合在一起的 -K"),
    no("ss --kill", "ss --kill"),
    no("ss --ki", "ss --kill 的缩写"),
    no("ss -D out", "ss -D 写文件"),
    no("ss --diag=out", "ss --diag"),
    no("ss -F .env", "ss -F 读 .env"),
    ok("sysctl -a", "sysctl -a"),
    ok("sysctl -n kern.hostname", "sysctl 读一项"),
    no("sysctl kernel.hostname=x", "sysctl 改设置"),
    no("sysctl -w kern.x=1", "sysctl -w"),
    no("sysctl -p", "sysctl -p 从文件加载"),
    no("sysctl --system", "sysctl --system"),
    ok("dmesg -T", "dmesg"),
    ok("dmesg --level=err", "dmesg --level"),
    no("dmesg -c", "dmesg -c 清空"),
    no("dmesg -C", "dmesg -C 清空"),
    no("dmesg --clear", "dmesg --clear"),
    no("dmesg -n 1", "dmesg -n 改控制台级别"),
    ok("ip addr", "ip addr"),
    ok("ip -br link", "ip -br"),
    ok("ip -4 -o addr show", "ip addr show"),
    ok("ip route get 1.1.1.1", "ip route get"),
    ok("ip neigh", "ip neigh"),
    no("ip", "光写 ip"),
    no("ip link set eth0 down", "ip link set"),
    no("ip l s eth0 down", "ip 的缩写 s 是 set"),
    no("ip addr add 1.2.3.4/24 dev eth0", "ip addr add"),
    no("ip -b cmds.txt", "ip -b 从文件读命令"),
    no("ip -batch cmds.txt", "ip -batch"),
    no("ip -force addr", "ip -force"),
    no("ip netns exec x sh", "ip netns exec"),
    no("ip route flush all", "ip route flush"),
    ok("ifconfig", "ifconfig"),
    ok("ifconfig -a", "ifconfig -a"),
    ok("ifconfig en0", "ifconfig 一块网卡"),
    no("ifconfig en0 down", "ifconfig 关网卡"),
    no("ifconfig en0 inet 1.2.3.4", "ifconfig 改地址"),
]

FILES = [
    ok("cat src/a.txt", "cat"),
    ok("cat -n src/a.txt", "cat -n"),
    ok("tac src/a.txt", "tac"),
    ok("nl src/a.txt", "nl"),
    ok("head -n 5 src/a.txt", "head -n"),
    ok("head -20 src/a.txt", "head -20"),
    ok("tail -f src/a.txt", "tail -f"),
    ok("tail -n +2 src/a.txt", "tail -n +2"),
    ok("cut -d, -f1 src/a.txt", "cut"),
    ok("column -t src/a.txt", "column"),
    ok("fold -w 10 src/a.txt", "fold"),
    ok("paste src/a.txt src/b.txt", "paste"),
    ok("join src/a.txt src/b.txt", "join"),
    ok("comm src/a.txt src/b.txt", "comm"),
    ok("expand src/a.txt", "expand"),
    ok("unexpand src/a.txt", "unexpand"),
    ok("rev src/a.txt", "rev"),
    ok("od -c src/a.txt", "od"),
    ok("hexdump -C src/a.txt", "hexdump"),
    ok("strings src/a.txt", "strings"),
    ok("cmp src/a.txt src/b.txt", "cmp"),
    ok("stat src/a.txt", "stat"),
    ok("realpath src/a.txt", "realpath"),
    ok("readlink src/a.txt", "readlink"),
    ok("basename src/a.txt", "basename"),
    ok("dirname src/a.txt", "dirname"),
    ok("md5 src/a.txt", "macOS md5"),
    ok("cat /etc/hosts", "系统里普通的文件"),
    ok("wc -l src/a.txt", "wc"),
    no("wc --files0-from=list", "wc 从文件里读要数的文件名"),
    no("wc --files0=list", "wc --files0-from 的缩写"),
    ok("file src/a.txt", "file"),
    no("file -C -m magic", "file -C 写编译好的规则"),
    no("file --compile", "file --compile"),
    no("file -f list", "file -f 从文件里读名字"),
    no("file --files-from list", "file --files-from"),
    ok("sort -u src/a.txt", "sort"),
    ok("sort -k2 -t, src/a.txt", "sort -k -t"),
    ok("sort -t ' ' -k 2 src/a.txt", "sort 分隔符是空格"),
    no("sort -o out src/a.txt", "sort -o 写文件"),
    no("sort -oout src/a.txt", "sort -o 紧贴着写"),
    no("sort -uo out src/a.txt", "sort -o 和别的合在一起"),
    no("sort --output=out src/a.txt", "sort --output"),
    no("sort --out=x src/a.txt", "sort --output 的 GNU 缩写"),
    no("sort src/a.txt -o out", "参数后面的 -o（GNU 照样认）"),
    no("sort -T /tmp src/a.txt", "sort -T 写临时文件"),
    no("sort --compress-program=sh src/a.txt", "sort --compress-program 运行程序"),
    no("sort --comp=sh src/a.txt", "sort --compress-program 的缩写"),
    no("sort --files0-from=list", "sort --files0-from"),
    no("sort --random-source=.env -R src/a.txt", "sort --random-source 读 .env"),
    ok("uniq -c src/a.txt", "uniq"),
    ok("uniq -f 1 src/a.txt", "uniq -f"),
    no("uniq src/a.txt out", "uniq 的第二个参数是要写的文件"),
    no("uniq src/a.txt -c", "BSD 的 uniq 把参数后面的 -c 当成输出文件"),
    no("uniq -- src/a.txt -c", "-- 后面的 -c 是输出文件"),
    ok("tr a-z A-Z", "tr"),
    ok("tr -d x", "tr -d"),
    no("tr a b c", "tr 最多两个参数"),
    ok("xxd src/a.txt", "xxd"),
    strict("xxd -l 16 src/a.txt", "xxd 按开头认选项，分不清哪个词是值：选项的值也算一个参数"),
    strict("xxd -s 4 -l 8 src/a.txt", "xxd 的 -s、-l 的值也算参数"),
    no("xxd src/a.txt out", "xxd 的第二个参数是要写的文件"),
    no("xxd src/a.txt -c", "xxd 参数后面的 -c 是输出文件"),
    ok("sha256sum src/a.txt", "sha256sum"),
    ok("sha1sum src/a.txt", "sha1sum"),
    ok("b2sum src/a.txt", "b2sum"),
    ok("shasum -a 256 src/a.txt", "shasum -a"),
    ok("cksum src/a.txt", "cksum"),
    no("sha256sum -c sums", "-c 按清单去读别的文件"),
    no("sha512sum --check sums", "--check"),
    no("sha256sum --ch sums", "--check 的缩写"),
    no("md5sum -bc sums", "-c 和别的合在一起"),
    no("shasum -c sums", "shasum -c"),
    no("tac .env", "tac 读 .env"),
    no("fold data/notes.md", "fold 读数据目录"),
    strict("basename .env", "basename 不读文件，但名字照样当路径看"),
]

PATHS = [
    no("cat .env", ".env"),
    no("cat ./.env", "./.env"),
    no("cat src/../.env", "绕回来的 .env"),
    no("cat '{WS}/.env'", "绝对路径的 .env"),
    no("cat .ENV", "大写的 .ENV"),
    no("cat .Env", "大小写混着的 .Env"),
    no("cat .env.local", ".env.local"),
    no("cat prod.env", "prod.env"),
    no("cat .envrc", ".envrc"),
    no("head -c 100 .env", "head 读 .env"),
    no("grep KEY .env", "grep 读 .env"),
    no("cat data/notes.md", "数据目录里的文件"),
    no("cat ./data/notes.md", "./data"),
    no("cat '{WS}/data/notes.md'", "绝对路径的数据目录"),
    no("cat DATA/notes.md", "大写的 DATA"),
    no("cat Data/Conversations/c1.json", "大小写混着的数据目录"),
    no("cat src/../data/notes.md", "绕回来的数据目录"),
    no("ls data", "列数据目录"),
    no("ls data/", "列数据目录（带斜杠）"),
    no("ls -la data/conversations", "列数据目录的子文件夹"),
    no("head data/mcp.json", "mcp.json"),
    no("cat data/channels.json", "channels.json"),
    no("cat data/commands.json", "commands.json"),
    no("stat data", "stat 数据目录"),
    no("du -sh data", "du 数据目录"),
    no("cat ~/.ssh/id_rsa", "~/.ssh/id_rsa"),
    no("ls ~/.ssh", "列 ~/.ssh"),
    no("ls -la ~/.ssh/", "列 ~/.ssh/"),
    no("cat '{HOME}/.ssh/id_rsa'", "绝对路径的私钥"),
    no("cat ~/.SSH/ID_RSA", "大写的私钥路径"),
    no("cat ~/.aws/credentials", "~/.aws"),
    no("cat ~/.zsh_history", "shell 的历史记录"),
    no("cat ~/.netrc", ".netrc"),
    no("cat ~/.git-credentials", ".git-credentials"),
    ok("cat ~/.bashrc", "主目录里普通的文件"),
    ok("cat ~/notes.txt", "主目录里的笔记"),
    ok("ls '{HOME}'", "列主目录（绝对路径）"),
    ok("ls -la ~", "列主目录（带隐藏文件）"),
    no("cat .git/config", ".git/config 里的地址可能带着 token"),
    no("cat .GIT/Config", "大写的 .git/config"),
    no("cat ./.git/../.git/config", "绕回来的 .git/config"),
    ok("cat .git/HEAD", ".git 里别的文件"),
    no("cat server.pem", "*.pem"),
    no("cat id_ed25519.pub", "id_ed25519*"),
    no("cat my_secret.txt", "*secret*"),
    no("cat aws_credentials.json", "*credential*"),
    no("cat keys/prod.key", "*.key"),
    no("cat backup.kdbx", "密码库"),
    no("cat /etc/shadow", "/etc/shadow"),
    no("cat /proc/self/environ", "自己的环境变量"),
    no("cat /proc/1/environ", "别的进程的环境变量"),
    posix(no("cat /proc/self/mem", "进程的内存"), POSIX_DEVICES),
    posix(no("cat /dev/sda", "设备文件"), POSIX_DEVICES),
    posix(no("cat /dev/stdin", "/dev/stdin"), POSIX_DEVICES),
    posix(no("head -c 10 /dev/urandom", "/dev/urandom"), POSIX_DEVICES),
    posix(no("cat /dev/tcp/example.com/80", "/dev/tcp"), POSIX_DEVICES),
    no("cat envlink", "指向 .env 的符号链接"),
    no("cat innocent.txt", "名字无害、指向数据目录的符号链接"),
    no("cat datalink/notes.md", "指向数据目录的文件夹链接"),
    no("ls datalink", "列指向数据目录的链接"),
    no("ls sshlink", "列指向 ~/.ssh 的链接"),
    no("cat sshlink/id_rsa", "经过链接读私钥"),
    no("cat mixed/inner/notes.md", "文件夹里的链接"),
    no("cat linked/notes.txt", "硬链接：同一个文件的另一个名字"),
    no("grep --file=.env x src/a.txt", "--file= 的值"),
    no("grep -f.env x src/a.txt", "-f 紧贴着写的值"),
    no("grep -e KEY .env", "grep -e 之后的文件"),
    no("diff --from-file=data/notes.md src/a.txt", "diff --from-file"),
    no("diff -X.env src/a.txt src/b.txt", "diff -X 紧贴着写"),
    no("sort --random-source=data/notes.md src/a.txt", "sort --random-source 读数据目录"),
    strict("ls -la .env", "只看大小和时间也要问"),
    strict("readlink envlink", "只看链接指向哪里也要问"),
    ok("grep -i secret src/a.txt", "要找的词只是文字，不当路径看"),
]

RECURSIVE = [
    ok("grep -r TODO src", "grep -r 一个干净的文件夹"),
    ok("grep -rn TODO src docs", "grep -r 两个文件夹"),
    ok("grep -R TODO src", "grep -R"),
    ok("grep -r -e TODO src", "grep -r -e"),
    ok("grep --directories=recurse TODO src", "--directories=recurse"),
    ok("grep -d recurse TODO src", "-d recurse"),
    ok("egrep -r 'a|b' src", "egrep -r"),
    ok("fgrep x src/a.txt", "fgrep"),
    no("grep -r TODO .", "grep -r 整个工作目录"),
    no("grep -rn TODO", "grep -r 不写文件夹就是当前文件夹"),
    no("grep -r -e TODO", "grep -r -e 不写文件夹"),
    no("egrep -r x .", "egrep -r 整个工作目录"),
    no("grep -r TODO ~", "grep -r 主目录"),
    no("grep -r TODO ..", "grep -r 上一层"),
    no("grep -R TODO mixed", "grep -R 跟着链接走进数据目录"),
    no("grep -r TODO mixed", "文件夹里有指向别处的链接"),
    no("grep -d recurse TODO .", "-d recurse 整个工作目录"),
    no("grep --directories=recurse TODO .", "--directories=recurse 整个工作目录"),
    no("grep -r TODO ../copy", "另一份副本里有 .env"),
    ok("rg TODO src", "rg 一个干净的文件夹"),
    ok("rg -n TODO src docs", "rg 两个文件夹"),
    ok("rg --files src", "rg --files"),
    ok("rg --type-list", "rg --type-list"),
    ok("rg -e TODO src", "rg -e"),
    no("rg TODO", "rg 不写文件夹"),
    no("rg TODO .", "rg 整个工作目录"),
    no("rg --files", "rg --files 整个工作目录"),
    no("rg -L TODO mixed", "rg -L 跟着链接"),
    no("rg --hidden TODO ../copy", "rg 另一份副本"),
    no("rg --pre cat TODO src", "rg --pre 运行程序"),
    no("rg -z TODO src", "rg -z 运行解压程序"),
    no("rg --search-zip TODO src", "rg --search-zip"),
    no("rg --hostname-bin=sh TODO src", "rg --hostname-bin"),
    no("rg --ignore-file .env TODO src", "rg --ignore-file"),
    ok("diff -r src docs", "diff -r 两个干净的文件夹"),
    ok("diff -u src/a.txt src/b.txt", "diff 两个文件"),
    no("diff -r . ../copy", "diff -r 整个工作目录"),
    no("diff -r src mixed", "diff -r 文件夹里有链接"),
    no("diff -r src ../copy", "diff -r 另一份副本"),
    no("diff --recursive src ../copy", "diff --recursive"),
    no("diff -l src/a.txt src/b.txt", "diff -l 会调用 pr"),
    ok("find src", "find 一个干净的文件夹"),
    ok("find src -name '*.py'", "find -name"),
    ok("find src -type f -mtime -1", "find -type -mtime"),
    ok("find src -maxdepth 2 -type d", "find -maxdepth"),
    ok("find src -name x -print0", "find -print0"),
    ok("find src '(' -name a -o -name b ')'", "find 的括号"),
    ok("find src ! -name x", "find !"),
    ok("find -E src -regex '.*py'", "macOS find -E"),
    ok("find -f src -name x", "macOS find -f"),
    ok("find src -name x -quit", "find -quit"),
    ok(r"find src -printf '%p\n'", "find -printf"),
    ok("find src -newermt 2024-01-01", "find -newerXY"),
    no("find", "find 不写文件夹"),
    no("find .", "find 整个工作目录"),
    no("find . -name x", "find 整个工作目录找名字"),
    no("find ~ -name x", "find 主目录"),
    no("find / -name x", "find 根目录"),
    no("find src -delete", "find -delete"),
    no("find src -name x -delete", "find 条件加 -delete"),
    no("find src -exec rm '{}' +", "find -exec"),
    no("find src -execdir ls ';'", "find -execdir"),
    no("find src -ok rm '{}' ';'", "find -ok"),
    no("find src -okdir rm '{}' ';'", "find -okdir"),
    no("find src -fprint out", "find -fprint"),
    no("find src -fprintf out x", "find -fprintf"),
    no("find src -fprint0 out", "find -fprint0"),
    no("find src -fls out", "find -fls"),
    no("find src -newer data/notes.md", "find -newer 数据目录里的文件"),
    no("find src -samefile .env", "find -samefile .env"),
    no("find src -files0-from list", "find -files0-from"),
    no("find -D tree src", "find -D"),
    no("find datalink", "find 指向数据目录的链接"),
    no("find src data", "find 两个文件夹，其中一个是数据目录"),
    ok("find src -name .env", "find 的名字模式只是模式，不当路径看"),
    ok("ls -R src", "ls -R 一个干净的文件夹"),
    ok("ls -laR src", "ls -R 和别的合在一起"),
    ok("ls -R mixed", "ls -R 不跟着链接走"),
    no("ls -R", "ls -R 当前文件夹"),
    no("ls -R .", "ls -R 整个工作目录"),
    no("ls -R ~", "ls -R 主目录"),
    no("ls --recursive .", "ls --recursive"),
    no("ls --recur src", "ls 的缩写不认"),
    no("ls -R --ignore x", "--ignore 带值：要列的其实是当前文件夹"),
    ok("tree src", "tree"),
    ok("tree -L 2 src", "tree -L"),
    ok("tree -a -I node_modules src", "tree -I"),
    no("tree", "tree 当前文件夹"),
    no("tree .", "tree 整个工作目录"),
    no("tree ~", "tree 主目录"),
    no("tree -o out src", "tree -o 写文件"),
    no("tree -R src", "tree -R 写 00Tree.html"),
    no("tree --fromfile list", "tree --fromfile"),
    ok("du -sh .", "du -s 只算总数"),
    ok("du -s src", "du -s"),
    ok("du -d 0 .", "du -d 0"),
    ok("du --max-depth=0 .", "du --max-depth=0"),
    ok("du -h src", "du 一个干净的文件夹"),
    no("du", "du 当前文件夹"),
    no("du .", "du 整个工作目录"),
    no("du -a .", "du -a 整个工作目录"),
    no("du ~", "du 主目录"),
    no("du --files0-from=list", "du --files0-from"),
    no("du -X list src", "du -X"),
    strict("du -d 1 .", "只列一层也不行"),
]

SED_AWK_JQ = [
    ok("sed -n '1,5p' src/a.txt", "sed 打印几行"),
    ok("sed 's/a/b/g' src/a.txt", "sed 替换"),
    ok(r"sed -E 's/(a+)/<\1>/g' src/a.txt", "sed 分组"),
    ok("sed -n '/start/,/end/p' src/a.txt", "sed 正则地址"),
    ok("sed '$d' src/a.txt", "sed $d"),
    ok("sed -e 's/a/b/' -e 's/c/d/' src/a.txt", "sed 两个 -e"),
    ok("sed 'y/abc/xyz/' src/a.txt", "sed y"),
    ok("sed '1!G;h;$!d' src/a.txt", "sed 倒序"),
    ok("sed -n '$=' src/a.txt", "sed 数行数"),
    ok("sed 's|/usr|/opt|g' src/a.txt", "sed 换分隔符"),
    ok("sed 5q src/a.txt", "sed q"),
    no("sed -i 's/a/b/' src/a.txt", "sed -i"),
    no("sed -i.bak 's/a/b/' src/a.txt", "sed -i.bak"),
    no("sed -I '' 's/a/b/' src/a.txt", "macOS sed -I"),
    no("sed --in-place 's/a/b/' src/a.txt", "sed --in-place"),
    no("sed --in-pl 's/a/b/' src/a.txt", "sed --in-place 的缩写"),
    no("sed 's/a/b/' src/a.txt -i", "GNU 会认参数后面的 -i"),
    no("sed -n 'w out' src/a.txt", "sed w"),
    no("sed 's/a/b/w out' src/a.txt", "sed s///w"),
    no("sed 's/a/b/gw out' src/a.txt", "sed s///gw"),
    no("sed 's/a/b/e' src/a.txt", "sed s///e 运行命令"),
    no("sed '1e id' src/a.txt", "sed e 运行命令"),
    no("sed 'r .env' src/a.txt", "sed r 读 .env"),
    no("sed '1r /etc/passwd' src/a.txt", "sed r 读别的文件"),
    no("sed 'R src/b.txt' src/a.txt", "sed R"),
    no("sed 'W out' src/a.txt", "sed W"),
    no("sed 'a hello' src/a.txt", "sed a"),
    no("sed -f script.sed src/a.txt", "sed -f 看不到脚本"),
    no("sed src/a.txt", "sed 的脚本看不懂"),
    no("sed", "sed 没有脚本"),
    no("sed p .env", "sed 读 .env"),
    no("sed -n p data/notes.md", "sed 读数据目录"),
    no("sed --expression=p .env", "sed --expression 之后读 .env"),
    strict("sed -n '1~2p' src/a.txt", "GNU 的步长地址不认"),
    ok("awk '{print $1}' src/a.txt", "awk 打印一列"),
    ok("awk -F, '{print $2}' src/a.txt", "awk -F"),
    ok("awk -F: '{print $1}' src/a.txt", "awk -F:"),
    ok("awk -v n=2 '{print $n}' src/a.txt", "awk -v"),
    ok("awk 'NR==1' src/a.txt", "awk 条件"),
    ok("awk '{print}' FS=, src/a.txt", "awk 参数里的赋值"),
    ok("awk 'END{print NR}' src/a.txt", "awk END"),
    no("""awk 'BEGIN{system("rm -rf ~")}'""", "awk system"),
    no("""awk '{print > "out"}' src/a.txt""", "awk 写文件"),
    no("""awk '{print >> "out"}' src/a.txt""", "awk 追加写文件"),
    no("""awk '{print | "sh"}' src/a.txt""", "awk 接到 sh"),
    no("""awk 'BEGIN{"date" | getline d}'""", "awk 运行命令读结果"),
    no("""awk 'BEGIN{getline l < ".env"; print l}'""", "awk getline 读文件"),
    no("""awk 'BEGIN{print ENVIRON["HOME"]}'""", "awk ENVIRON"),
    no("""awk 'BEGIN{ARGV[1]=".env"} {print}' src/a.txt""", "awk 改 ARGV"),
    no("""awk '@load "filefuncs"; BEGIN{}'""", "gawk @load"),
    no("""awk 'BEGIN{print PROCINFO["pid"]}'""", "gawk PROCINFO"),
    no("""awk 'BEGIN{close("x")}'""", "awk close"),
    no("awk -f prog.awk src/a.txt", "awk -f 看不到程序"),
    no("awk -e '{print}' src/a.txt", "gawk -e"),
    no("awk --source='{print}' src/a.txt", "gawk --source"),
    no("awk -l filefuncs '{print}'", "gawk -l 加载扩展"),
    no("awk -i inplace '{print}' src/a.txt", "gawk -i inplace 改文件"),
    no("awk '{print}' .env", "awk 读 .env"),
    no("awk '{print}' data/notes.md", "awk 读数据目录"),
    no("awk '{print}' src/a.txt -f x", "参数后面的 -f"),
    no("awk", "awk 没有程序"),
    strict("awk '$1 > 3' src/a.txt", "比较大小的 > 也不认"),
    ok("jq . src/config.json", "jq ."),
    ok("jq -r '.name' src/config.json", "jq -r"),
    ok("jq -n '1+1'", "jq -n"),
    ok("jq -n", "jq -n 不带程序"),
    ok("jq --arg k v '.[$k]' src/config.json", "jq --arg"),
    ok("jq --argjson n 1 '.a + $n' src/config.json", "jq --argjson"),
    ok("jq -c '.[]' src/config.json", "jq -c"),
    ok("jq --arg x .env . src/config.json", "--arg 的值是字符串，不是要读的文件"),
    no("jq -n env", "jq env"),
    no("jq -n '$ENV.HOME'", "jq $ENV"),
    no("""jq 'import "x" as y; .' src/config.json""", "jq import"),
    no("""jq 'include "x"; .' src/config.json""", "jq include"),
    no("jq -n '$__loc__'", "jq $__loc__"),
    no("jq -n 'get_search_list'", "jq get_search_list"),
    no("jq --rawfile x .env -n '$x'", "jq --rawfile"),
    no("jq --slurpfile x src/config.json -n '$x'", "jq --slurpfile"),
    no("jq -f prog.jq src/config.json", "jq -f"),
    no("jq --from-file prog.jq", "jq --from-file"),
    no("jq --args -n '$ARGS' a b", "jq --args"),
    no("jq -L dir -n 1", "jq -L"),
    no("jq . .env", "jq 读 .env"),
    no("jq . data/mcp.json", "jq 读数据目录"),
    no("jq", "jq 没有程序"),
    no("jq --arg x", "jq --arg 缺值"),
    strict("jq .env src/config.json", "字段名里有 env"),
]

GIT = [
    ok("git status", "git status"),
    ok("git status -sb", "git status -sb"),
    ok("git status --porcelain", "git status --porcelain"),
    ok("git --no-pager status", "git --no-pager"),
    ok("git -P log", "git -P"),
    ok("git log --oneline -n 5", "git log"),
    ok("git log -5 --stat", "git log -5"),
    ok("git log -p -- src/a.txt", "git log -p 一个文件"),
    ok("git log --format='%h %an %s'", "git log --format"),
    ok("git log --no-ext-diff", "--no-ext-diff 不是 --ext-diff"),
    ok("git diff", "git diff"),
    ok("git diff --stat", "git diff --stat"),
    ok("git diff --cached", "git diff --cached"),
    ok("git diff HEAD~1 -- src", "git diff 一个文件夹"),
    ok("git show HEAD", "git show"),
    ok("git show HEAD:src/a.txt", "git show 版本:文件"),
    ok("git show --stat HEAD", "git show --stat"),
    ok("git shortlog -sn", "git shortlog"),
    ok("git blame src/a.txt", "git blame"),
    ok("git describe --tags", "git describe"),
    ok("git rev-parse HEAD", "git rev-parse"),
    ok("git rev-parse --show-toplevel", "git rev-parse --show-toplevel"),
    ok("git ls-files", "git ls-files"),
    ok("git whatchanged -n 1", "git whatchanged"),
    ok("git branch", "git branch"),
    ok("git branch -a", "git branch -a"),
    ok("git branch -vv", "git branch -vv"),
    ok("git branch --list 'feat*'", "git branch --list 模式"),
    ok("git branch --show-current", "git branch --show-current"),
    ok("git tag", "git tag"),
    ok("git tag -l 'v*'", "git tag -l"),
    ok("git tag --list", "git tag --list"),
    ok("git stash list", "git stash list"),
    ok("git stash show -p", "git stash show"),
    ok("git reflog", "git reflog"),
    ok("git reflog show", "git reflog show"),
    ok("git remote", "git remote 只列名字"),
    ok("git version", "git version"),
    ok("git --version", "git --version"),
    ok("git --no-pager --version", "git --no-pager --version"),
    no("git", "光写 git"),
    no("git --no-pager", "只有全局选项"),
    no("git -c core.pager=sh log", "git -c 改设置"),
    no("git -c core.fsmonitor=x status", "git -c core.fsmonitor"),
    no("git -C .. status", "git -C 换仓库"),
    no("git --git-dir=x log", "git --git-dir"),
    no("git --work-tree=x status", "git --work-tree"),
    no("git -p log", "git -p 翻页"),
    no("git --paginate log", "git --paginate"),
    no("git --exec-path=x log", "git --exec-path"),
    no("git version --build-options", "git version 后面不许跟别的"),
    no("git config --list", "git config"),
    no("git config user.name x", "git config 改设置"),
    no("git grep -O sh x", "git grep -O 运行程序"),
    no("git push", "git push"),
    no("git commit -m x", "git commit"),
    no("git checkout .", "git checkout"),
    no("git reset --hard", "git reset"),
    no("git clean -fd", "git clean"),
    no("git fetch", "git fetch"),
    no("git pull", "git pull"),
    no("git log --output=x", "git log --output"),
    no("git log --outp=x", "git log --output 的缩写"),
    no("git diff --output=x", "git diff --output"),
    no("git diff --ext-diff", "git diff --ext-diff"),
    no("git diff --ext", "git diff --ext-diff 的缩写"),
    no("git show --ext-diff HEAD", "git show --ext-diff"),
    no("git log -p -- .env", "git log 一个 .env"),
    no("git show HEAD:.env", "git show 版本:.env"),
    no("git show HEAD:data/notes.md", "git show 版本:数据目录"),
    no("git show 'HEAD@{1}:.env'", "git show 带 @{} 的版本"),
    no("git show :.env", "git show 暂存区里的 .env"),
    no("git diff -O.env", "git diff -O 紧贴着写"),
    no("git blame .env", "git blame .env"),
    no("git blame --contents .env src/a.txt", "git blame --contents .env"),
    no("git status --ignored", "git status --ignored"),
    no("git status --ign", "--ignored 的缩写"),
    no("git status --ignored=matching", "--ignored=matching"),
    no("git ls-files -o", "git ls-files -o"),
    no("git ls-files --others", "git ls-files --others"),
    no("git ls-files -io", "git ls-files -i 合在一起"),
    no("git ls-files --ignored", "git ls-files --ignored"),
    no("git branch new", "git branch 新名字"),
    no("git branch -d x", "git branch -d"),
    no("git branch -D x", "git branch -D"),
    no("git branch -m a b", "git branch -m"),
    no("git branch -l x", "老版本的 git branch -l 是新建"),
    no("git branch --set-upstream-to=origin/x", "git branch --set-upstream-to"),
    no("git branch -u origin/x", "git branch -u"),
    no("git branch --edit-description", "git branch --edit-description"),
    no("git tag v1", "git tag 新名字"),
    no("git tag -d v1", "git tag -d"),
    no("git tag -a v1 -m x", "git tag -a"),
    no("git stash", "光写 git stash 是收起改动"),
    no("git stash pop", "git stash pop"),
    no("git stash drop", "git stash drop"),
    no("git stash push", "git stash push"),
    no("git reflog expire --all", "git reflog expire"),
    no("git reflog delete 'HEAD@{1}'", "git reflog delete"),
    no("git remote add x y", "git remote add"),
    no("git remote get-url origin", "git remote get-url"),
    no("git remote show origin", "git remote show 会联网"),
    no("git remote set-url origin x", "git remote set-url"),
    strict("git branch --contains HEAD", "--contains 的值被当成了分支名"),
]

SERVICES = [
    ok("systemctl status nginx", "systemctl status"),
    ok("systemctl --no-pager status nginx", "systemctl --no-pager"),
    ok("systemctl is-active nginx", "systemctl is-active"),
    ok("systemctl is-enabled nginx", "systemctl is-enabled"),
    ok("systemctl list-units --failed", "systemctl list-units"),
    ok("systemctl --user list-timers", "systemctl --user"),
    ok("systemctl status -n 20 nginx", "systemctl status -n"),
    no("systemctl restart nginx", "systemctl restart"),
    no("systemctl stop nginx", "systemctl stop"),
    no("systemctl enable nginx", "systemctl enable"),
    no("systemctl daemon-reload", "systemctl daemon-reload"),
    no("systemctl show nginx", "systemctl show 会显示环境变量"),
    no("systemctl cat nginx", "systemctl cat"),
    no("systemctl edit nginx", "systemctl edit"),
    no("systemctl -H host status", "systemctl -H 连到别的机器"),
    no("systemctl --host=host status", "systemctl --host"),
    no("systemctl -M box status", "systemctl -M"),
    ok("systemctl", "光写 systemctl 就是 list-units"),
    ok("journalctl -u nginx -n 50 --no-pager", "journalctl"),
    ok("journalctl -f", "journalctl -f"),
    ok("journalctl -xeu nginx", "journalctl -xeu"),
    ok("journalctl --since today", "journalctl --since"),
    ok("journalctl -p err -b 0", "journalctl -b 0"),
    ok("journalctl --disk-usage", "journalctl --disk-usage"),
    ok("journalctl -o json -n 1", "journalctl -o json"),
    no("journalctl --vacuum-size=1M", "journalctl --vacuum-size"),
    no("journalctl --vac=1", "--vacuum 的缩写"),
    no("journalctl --vacuum-time 1d", "journalctl --vacuum-time"),
    no("journalctl --rotate", "journalctl --rotate"),
    no("journalctl --rot", "--rotate 的缩写"),
    no("journalctl --flush", "journalctl --flush"),
    no("journalctl --sync", "journalctl --sync"),
    no("journalctl --relinquish-var", "journalctl --relinquish-var"),
    no("journalctl --cursor-file=x", "journalctl --cursor-file 写文件"),
    no("journalctl --setup-keys", "journalctl --setup-keys"),
    no("journalctl --update-catalog", "journalctl --update-catalog"),
    no("journalctl --file=.env", "journalctl --file=.env"),
    no("journalctl --file .env", "journalctl --file .env"),
    no("journalctl -D data", "journalctl -D 数据目录"),
    no("journalctl --root=data", "journalctl --root 数据目录"),
    ok("journalctl -b", "-b 后面可以不带值（这次开机）"),
    ok("docker ps", "docker ps"),
    ok("docker ps -a", "docker ps -a"),
    ok("docker ps --format '{{.Names}}'", "docker ps --format"),
    ok("docker images", "docker images"),
    ok("docker logs --tail 50 web", "docker logs"),
    ok("docker logs -f web", "docker logs -f"),
    ok("docker container ls", "docker container ls"),
    ok("docker image ls", "docker image ls"),
    ok("docker stats --no-stream", "docker stats"),
    ok("docker version", "docker version"),
    ok("docker info", "docker info"),
    ok("docker port web", "docker port"),
    ok("docker top web", "docker top"),
    no("docker", "光写 docker"),
    no("docker run alpine", "docker run"),
    no("docker exec web sh", "docker exec"),
    no("docker rm web", "docker rm"),
    no("docker rmi x", "docker rmi"),
    no("docker inspect web", "docker inspect 会显示环境变量"),
    no("docker -H tcp://x ps", "docker -H"),
    no("docker --context x ps", "docker --context"),
    no("docker --config x ps", "docker --config"),
    no("docker container rm web", "docker container rm"),
    no("docker container exec web sh", "docker container exec"),
    no("docker image rm x", "docker image rm"),
    no("docker image prune", "docker image prune"),
    no("docker system prune", "docker system prune"),
    no("docker cp web:/x .", "docker cp"),
    strict("docker compose ps", "compose 不认"),
    ok("kubectl get pods", "kubectl get"),
    ok("kubectl get pods -n kube-system -o wide", "kubectl get -n -o"),
    ok("kubectl get deploy -A", "kubectl get -A"),
    ok("kubectl describe pod web", "kubectl describe"),
    ok("kubectl logs deploy/web --tail 100", "kubectl logs"),
    ok("kubectl logs -f web", "kubectl logs -f"),
    ok("kubectl top pods", "kubectl top"),
    ok("kubectl version --client", "kubectl version"),
    ok("kubectl api-resources", "kubectl api-resources"),
    ok("kubectl get pods -o jsonpath='{.items[*].metadata.name}'", "kubectl jsonpath"),
    no("kubectl get secrets", "kubectl get secrets"),
    no("kubectl get secret db -o yaml", "kubectl get secret -o yaml"),
    no("kubectl get SECRETS", "大写的 SECRETS"),
    no("kubectl describe secret db", "kubectl describe secret"),
    no("kubectl get --raw /api/v1/namespaces/default/secrets", "kubectl --raw secrets"),
    no("kubectl --kubeconfig x get pods", "kubectl --kubeconfig"),
    no("kubectl --token x get pods", "kubectl --token"),
    no("kubectl -s https://x get pods", "kubectl -s"),
    no("kubectl --server=https://x get pods", "kubectl --server"),
    no("kubectl -v 9 get pods", "kubectl -v 会打印请求头"),
    no("kubectl get -f x.yaml", "kubectl get -f 读文件"),
    no("kubectl get -k dir", "kubectl get -k"),
    no("kubectl delete pod web", "kubectl delete"),
    no("kubectl exec web -- sh", "kubectl exec"),
    no("kubectl apply -f x.yaml", "kubectl apply"),
    no("kubectl edit deploy web", "kubectl edit"),
    no("kubectl port-forward web 80", "kubectl port-forward"),
    no("kubectl proxy", "kubectl proxy"),
    no("kubectl cp web:/x x", "kubectl cp"),
    no("kubectl get pods -o go-template-file=data/notes.md", "kubectl 模板文件在数据目录里"),
    ok("crontab -l", "crontab -l"),
    no("crontab", "光写 crontab"),
    no("crontab -e", "crontab -e"),
    no("crontab -r", "crontab -r 删掉所有定时任务"),
    no("crontab file", "crontab 换掉定时任务"),
    no("crontab -l -u root", "crontab -l 只许单独用"),
]

PROGRAMS = [
    ok("python --version", "python --version"),
    ok("python3 -V", "python3 -V"),
    ok("node -v", "node -v"),
    ok("npm --version", "npm --version"),
    ok("go version", "go version"),
    ok("java -version", "java -version"),
    ok("rustc --version", "rustc --version"),
    ok("nginx -V", "nginx -V"),
    no("terraform version", "terraform 查版本时会联网看有没有新版本"),
    no("python", "python 交互"),
    no("python -c 'print(1)'", "python -c"),
    no("python3 script.py", "python 运行脚本"),
    no("node -e 1", "node -e"),
    no("npm install", "npm install"),
    no("npm test", "npm test"),
    no("make", "make"),
    no("make --version extra", "make --version 后面不许跟别的"),
    no("nginx -s stop", "nginx -s stop"),
    no("perl -e 1", "perl -e"),
    no("uv run x", "uv run"),
    no("pip install x", "pip install"),
    no("go run .", "go run"),
    no("bash -c ls", "bash"),
    no("sh -c ls", "sh"),
    no("zsh", "zsh"),
    no("less src/a.txt", "less 能运行命令"),
    no("more src/a.txt", "more"),
    no("man ls", "man"),
    no("vi src/a.txt", "vi"),
    no("curl https://example.com", "curl 联网"),
    no("wget https://example.com", "wget 联网"),
    no("ping example.com", "ping 联网"),
    no("ssh host", "ssh"),
    no("nc host 80", "nc"),
    no("dig example.com", "dig"),
    no("rm -rf ~", "rm"),
    no("mv a b", "mv"),
    no("cp a b", "cp"),
    no("touch x", "touch"),
    no("mkdir x", "mkdir"),
    no("chmod 777 x", "chmod"),
    no("tee x", "tee"),
    no("dd if=src/a.txt of=x", "dd"),
    no("kill 1", "kill"),
    no("open .", "macOS open"),
    no("osascript -e x", "osascript"),
]

# 找到的漏洞：现在被当成只读，实际上不是。每一行写明在哪个平台上、实际会做什么。
BYPASSES = [
    # 运行命令
    bypass("printf -v 'x[$(touch src/pwned)]' 1", "Linux 的 bash 4 以上：printf -v 的数组下标里的命令替换会运行"),
    # 写文件
    bypass("xxd -ps src/a.txt src/out", "xxd 按开头认选项：-ps 就是 -p，src/out 是要写的文件"),
    bypass("xxd -bits src/a.txt src/out", "xxd 的 -bits 就是 -b，第二个参数照样是要写的文件"),
    bypass("echo 2321 | xxd -r -ps - src/out", "xxd -r 把管道送来的十六进制还原成任意内容写进文件"),
    bypass("sed -l 'w src/out' p src/a.txt", "macOS 的 sed -l 不带值：'w src/out' 才是脚本，会写文件"),
    bypass("sed -l 'r .env' p src/a.txt", "macOS 的 sed -l 不带值：'r .env' 才是脚本，会读 .env"),
    bypass("sed 's/[/]/p/w src/p' src/a.txt", "macOS 的 sed 在方括号里不认分隔符：后面是 w src/p，会写文件"),
    bypass("git reflog --output=src/x", "git reflog 就是 git log -g：--output 写文件"),
    bypass("git reflog show --output=src/x", "git reflog show --output 写文件"),
    bypass("git stash show -p --output=src/x", "git stash show 把选项交给 git diff：--output 写文件"),
    bypass("git stash list --output=src/x", "git stash list 把选项交给 git log：--output 写文件"),
    bypass("git stash show --ext-diff", "git stash show --ext-diff 运行外部的 diff 程序"),
    bypass("journalctl -n --cursor-file=src/x", "journalctl 的 -n 值可以不写：--cursor-file 照样生效，写文件"),
    bypass("journalctl -b --vacuum-size=1M", "journalctl 的 -b 值可以不写：--vacuum-size 照样生效，删日志"),
    bypass("journalctl --lines --rotate", "journalctl 的 --lines 值可以不写：--rotate 照样生效"),
    # 读到密钥、数据目录
    bypass("file -m.env src/a.txt", "file -m 的值紧贴着写没被当成路径：.env 的每一行打在警告里"),
    bypass("hexdump -f.env src/a.txt", "hexdump -f 的值紧贴着写：.env 的内容打在错误里"),
    bypass("strings @data/notes.md", "GNU strings 把 @文件 里的词当参数读进来，出错时逐个打出来"),
    bypass("grep -d RECURSE KEY .", "macOS 的 grep -d 不分大小写：整个工作目录（.env、data/）都搜一遍"),
    bypass("grep --directories=RECURSE KEY .", "macOS 的 grep --directories 不分大小写"),
    bypass("grep -d rec KEY .", "GNU grep 认 -d 的值的缩写：rec 就是 recurse"),
    bypass("""awk 'BEGIN{for(k in SYMTAB["ENVI" "RON"]) print k "=" SYMTAB["ENVI" "RON"][k]}'""", "gawk 的 SYMTAB 拼出 ENVIRON，打印环境变量"),
    bypass("""awk 'BEGIN{SYMTAB["AR" "GV"][1]=".e" "nv"} {print}' src/a.txt""", "gawk 的 SYMTAB 拼出 ARGV，把要读的文件换成 .env"),
    bypass("ps eww", "ps e 显示每个进程的环境变量（和 /proc/*/environ 一样）"),
    bypass("ps -E", "macOS 的 ps -E 显示每个进程的环境变量"),
    bypass("ps auxe", "BSD 写法的 e 显示环境变量"),
    bypass("docker top web e", "docker top 把后面的词交给 ps：e 显示容器里进程的环境变量"),
    bypass("git remote -v", "git remote -v 把远程地址里的 token 打出来（和 .git/config 一样）"),
    bypass("git remote --verbose", "git remote --verbose 同上"),
    bypass("git diff --no-index src .", "git diff --no-index 把整个工作目录（.env、data/）和 src 比一遍，内容全打出来"),
    bypass("git diff ../empty .", "有一边在仓库外面时 git diff 自动变成 --no-index，整个工作目录的内容全打出来"),
    bypass("diff . ../copy", "两个文件夹不加 -r 也会比较同名的文件：两个 .env 的内容都打出来"),
    bypass("diff --from-file=. ../copy", "--from-file 是文件夹时同上"),
    bypass("diff -r --from-file=. ../twin", "--from-file 的文件夹没被当成要走遍的：data/ 里的内容打出来"),
    bypass("diff -r --unified . ../twin", "GNU 和 macOS 的 --unified 值只能用 = 写：. 其实是要比较的文件夹，data/ 的内容打出来"),
    bypass("grep -r KEY linked", "硬链接到 .env 的文件，走遍文件夹时只看名字认不出来"),
    bypass("kubectl get --raw /api/v1/namespaces/default/s%65crets", "%65 就是 e：kubectl 解码之后请求的就是 secrets"),
    bypass("kubectl get pods -o=jsonpath-file=data/notes.md", "-o= 后面又有一个 =：只拆了第一个 =，数据目录里的文件没认出来"),
    # 列出数据目录里的文件名（跟着符号链接走进 data/）
    bypass("ls -RL mixed", "ls -L 跟着链接走进 data/，列出里面的文件名"),
    bypass("find -L mixed", "find -L 同上"),
    bypass("find mixed -follow", "find -follow 同上"),
    bypass("tree -l mixed", "tree -l 同上"),
    bypass("du -aL mixed", "du -L 同上"),
]

TABLE = [*QUOTING, *OPERATORS, *INVISIBLE, *WRAPPERS, *PIPELINES, *SYSTEM, *FILES, *PATHS, *RECURSIVE, *SED_AWK_JQ, *GIT, *SERVICES, *PROGRAMS, *BYPASSES]


@pytest.mark.parametrize("command, readonly", TABLE)
def test_classify(spot, command, readonly):
    if not symlinks_work() and any(name in command for name in LINKS):
        pytest.skip(NO_SYMLINKS)
    places, fill = spot
    verdict = classify(fill(command), places)
    assert verdict.readonly is readonly, verdict.reason or "被当成了只读"


def test_the_table_is_large_and_has_no_duplicates():
    commands = [param.values[0] for param in TABLE]
    assert len(commands) >= 200
    assert len(commands) == len(set(commands)), [item for item in commands if commands.count(item) > 1]


def test_verdicts_say_why_and_where_from(spot):
    places, _ = spot
    accepted = classify("ls -la", places)
    assert accepted == accepted.__class__(True, "", "builtin")
    refused = classify("rm -rf ~", places)
    assert not refused.readonly and "rm" in refused.reason and refused.source == ""


# ---- 你自己加的命令 -------------------------------------------------------------

LISTED = [["npm", "test"], ["make", "check"]]


@pytest.mark.parametrize(
    "command, readonly",
    [
        ok("npm test", "完全一样"),
        ok("npm test --watch", "后面多几个词也算（前缀）"),
        ok("npm test src", "后面的词照样检查路径：src 没问题"),
        ok("make check", "第二条"),
        no("npm testing", "不是同一个词"),
        no("npm", "比清单里的短"),
        no("npm tes", "半个词"),
        no("npm test .env", "允许的是这条命令，不是让它去读 .env"),
        no("npm test data/notes.md", "也不能读数据目录"),
        no("npm test ~/.ssh/id_rsa", "也不能读私钥"),
        no("cat src/a.txt | npm test", "不能出现在管道里"),
        no("npm test | cat", "管道的第一段也不行"),
        no("FOO=1 npm test", "前面设置环境变量"),
        no("npm test; rm -rf ~", "分号"),
        no("env npm test", "套在 env 里面不算"),
        no("timeout 5 npm test", "套在 timeout 里面不算"),
        no("npm  install", "别的子命令"),
    ],
)
def test_listed_commands(spot, command, readonly):
    places, _ = spot
    verdict = classify(command, places, listed=LISTED)
    assert verdict.readonly is readonly, verdict.reason
    if readonly:
        assert verdict.source == "listed"


def test_on_windows_nothing_is_readonly(spot, monkeypatch):
    """Windows 上的命令是 PowerShell：拆词和名单都看不准它，一条都不算只读，你加的也不算（见 harness/commands/__init__.py）。"""
    places, _ = spot
    monkeypatch.setattr(commands_module, "WINDOWS", True)
    for command in ("ls", "ls -la", "git status", "npm test", "Get-ChildItem", "cat src/a.txt", "dir"):
        verdict = classify(command, places, listed=LISTED)
        assert not verdict.readonly and "PowerShell" in verdict.reason, command


def test_listed_entries_never_widen_builtin_paths(spot):
    """清单是空的、或者有一条空的，都不会让别的命令变成只读。"""
    places, _ = spot
    assert not classify("rm -rf x", places, listed=[[]]).readonly
    assert not classify("rm -rf x", places, listed=[]).readonly
    assert classify("ls", places, listed=[[]]).source == "builtin"


# ---- 别的路径问题 ----------------------------------------------------------------


def test_decomposed_unicode_reaches_the_same_folder_on_macos(tmp_path):
    """绕过：macOS 的文件系统（APFS）不分 NFC 和 NFD 两种写法，"año" 和"a、n、U+0303（组合用的波浪号）、o"是同一个文件夹。
    工作目录的路径里有带音调的字母时（西班牙语的用户很常见），用另一种写法的绝对路径就能读到数据目录。"""
    workspace = tmp_path / unicodedata.normalize("NFC", "año")
    (workspace / "data").mkdir(parents=True)
    (workspace / "data" / "notes.md").write_text("私人笔记\n", encoding="utf-8")
    places = Places(workspace, private=[workspace / "data"], home=tmp_path / "home")
    decomposed = unicodedata.normalize("NFD", str(workspace))
    assert decomposed != str(workspace)
    assert not classify(f"cat '{decomposed}/data/notes.md'", places).readonly


def test_scanning_a_huge_folder_gives_up(tmp_path, monkeypatch):
    """会读每个文件内容的命令（grep -r），文件夹太大看不完时照常问。"""
    monkeypatch.setattr(paths_module, "READONLY_SCAN_ENTRIES", 3)
    workspace = tmp_path / "ws"
    (workspace / "small").mkdir(parents=True)
    (workspace / "big").mkdir()
    for index in range(2):
        (workspace / "small" / f"{index}.txt").write_text("x", encoding="utf-8")
    for index in range(4):
        (workspace / "big" / f"{index}.txt").write_text("x", encoding="utf-8")
    places = Places(workspace, home=tmp_path / "home")
    assert classify("grep -r x small", places).readonly
    verdict = classify("grep -r x big", places)
    assert not verdict.readonly and "太多" in verdict.reason


def test_missing_protected_files_are_fine(tmp_path):
    """protected 里的文件还不存在（比如还没有 channels.json）时，照样能判断，而且照样不许读那个名字。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    places = Places(workspace, protected=[workspace / "data" / "channels.json"], private=[workspace / "data"], home=tmp_path / "home")
    assert classify("ls", places).readonly
    assert not classify("cat data/channels.json", places).readonly


def test_symlink_loops_do_not_crash(symlinks, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "loop").symlink_to(workspace / "loop")
    places = Places(workspace, home=tmp_path / "home")
    for command in ("cat loop", "grep -r x loop", "ls -R loop"):
        assert classify(command, places).readonly is False


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("grep -r x " + "a" * 300, id="grep -r 名字太长"),
        pytest.param("ls -R " + "a" * 300, id="ls -R 名字太长"),
        pytest.param("find " + "a" * 300, id="find 名字太长"),
        pytest.param("du " + "a" * 300, id="du 名字太长"),
    ],
)
def test_odd_paths_never_crash(tmp_path, command):
    """classify 从不抛出异常：看不清的路径（名字太长、没有权限……）一律当作看不懂，照常问。
    现在会失败：check_tree 里的 Path.is_dir() 在 Python 3.12 上碰到 ENAMETOOLONG、EACCES 会把 OSError 抛出来。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    places = Places(workspace, home=tmp_path / "home")
    assert classify(command, places).readonly is False


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root 不受文件权限限制")
def test_unreadable_folders_never_crash(tmp_path):
    """同上：没有权限看的文件夹。现在会失败（PermissionError 从 check_tree 里抛出来）。"""
    workspace = tmp_path / "ws"
    (workspace / "locked" / "inner").mkdir(parents=True)
    places = Places(workspace, home=tmp_path / "home")
    (workspace / "locked").chmod(0)
    try:
        for command in ("grep -r x locked", "ls -R locked/inner", "find locked/inner"):
            assert classify(command, places).readonly is False, command
    finally:
        (workspace / "locked").chmod(0o700)


# ---- 拆词 -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command, expected",
    [
        ("ls -la | grep x", [["ls", "-la"], ["grep", "x"]]),
        ("ls|grep x", [["ls"], ["grep", "x"]]),
        ("cat 'a b' \"c d\" e\\ f", [["cat", "a b", "c d", "e f"]]),
        ("echo '' \"\"", [["echo", "", ""]]),
        ("echo a''b \"c\"d", [["echo", "ab", "cd"]]),
        (r'echo "a\"b" "c\\d" "e\nf"', [["echo", 'a"b', "c\\d", "e\\nf"]]),
        (r"echo \a\'\"", [["echo", "a'\""]]),
        ("ls ~ ~/x", [["ls", "~", "~/x"]]),
        ("ls a~b ''~", [["ls", "a~b", "~"]]),
        ("ls a#b", [["ls", "a#b"]]),
        ("ls\tsrc  -l", [["ls", "src", "-l"]]),
        ("ls 'a|b' 'a;b' '$HOME'", [["ls", "a|b", "a;b", "$HOME"]]),
        ("ls FOO=1", [["ls", "FOO=1"]]),
        ("grep 错误 日志.txt", [["grep", "错误", "日志.txt"]]),
    ],
)
def test_stages_splits_words(command, expected):
    assert stages(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        None, 1, "", " \t ", "ls;", "ls &", "a || b", "a |& b", "ls |", "| ls", "a | | b", "ls >x", "ls <x", "ls $x", "ls `x`",
        "ls (x)", "ls {a,b}", "ls *", "ls ?", "ls [a]", "# x", "ls #x", "ls ~x", "ls ~+", 'ls ~"x"', "'open", '"open', "end\\",
        '"$x"', '"`x`"', '"\\$x"', "FOO=1 ls", "ls | FOO=1 cat", "a+=1 ls", "ls\n", "ls\u200b", "ls\u00a0x",
        "x" * (READONLY_MAX_CHARS + 1),
    ],
)
def test_stages_refuses(command):
    with pytest.raises(Unclear):
        stages(command)


# ---- 选项 -------------------------------------------------------------------------


def test_parse_values_in_every_spelling():
    values = names("-n --lines")
    for words in (["-n", "5"], ["-n5"], ["--lines", "5"], ["--lines=5"]):
        parsed = parse([*words, "file"], values)
        assert [value for _, value in parsed.options] == ["5"] and parsed.operands == ["file"], words


def test_parse_clusters_numbers_and_optional_values():
    assert parse(["-abc"]).options == [("-a", None), ("-b", None), ("-c", None)]
    assert parse(["-an5"], names("-n")).options == [("-a", None), ("-n", "5")]
    assert parse(["-20", "f"], numbers=True).options == [("-NUM", "20")]
    assert parse(["-20"]).options == [("-2", None), ("-0", None)]
    assert parse(["-Iseconds"], optional=names("-I")).options == [("-I", "seconds")]
    optional = parse(["-I", "x"], optional=names("-I"))
    assert optional.options == [("-I", None)] and optional.operands == ["x"]


def test_parse_double_dash_and_lone_dash():
    parsed = parse(["-a", "--", "-x", "y"])
    assert parsed.options == [("-a", None)] and parsed.operands == ["-x", "y"] and parsed.late == []
    assert parse(["-"]).operands == ["-"]


def test_parse_counts_late_options_as_operands_too():
    """BSD 的程序碰到第一个参数就不再认选项：uniq a.txt -c 里的 -c 在 macOS 上是输出文件。"""
    parsed = parse(["a.txt", "-c", "--x=1"])
    assert parsed.operands == ["a.txt"]
    assert parsed.late == ["-c", "--x=1"]
    assert parsed.has("-c") and parsed.value("--x") == "1"
    assert parsed.words() == ["a.txt", "1", "-c", "--x=1"]


def test_parse_refuses_missing_values():
    for words in (["-n"], ["--lines"], ["-an"]):
        with pytest.raises(Unclear):
            parse(words, names("-n --lines"))


def test_deny_catches_gnu_abbreviations():
    denied = names("--output -o")
    for words in (["--output=x"], ["--out=x"], ["--o"], ["-o", "x"], ["-so", "x"]):
        with pytest.raises(Unclear):
            deny(parse(words, names("-o")), denied)
    deny(parse(["--output-indicator-new=x"]), denied)  # 一个真的、别的选项，不是 --output 的缩写
    deny(parse(["--", "--output"]), denied)  # -- 后面的是参数
    deny(parse(["-"]), denied)


def test_allow_needs_full_names():
    allowed = names("--recursive -r")
    allow(parse(["--recursive", "-r"]), allowed)
    for words in (["--recur"], ["-R"], ["--recursive=yes", "-x"]):
        with pytest.raises(Unclear):
            allow(parse(words), allowed)
