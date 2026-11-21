r"""一条只读的命令会碰到哪些路径，碰到的是不是不该不问就读的地方。

"只读"不等于"读什么都行"。有三种地方，命令就算只是看，也照常问你：

  1. 智狐自己的密钥文件（.env、mcp.json、channels.json）和"不用问的命令"清单（commands.json）。
     文件工具根本读不了它们；命令也一样，不能因为是 cat 就放过去。
  2. 智狐的数据目录（data/：以前的会话、记忆、设置）。和文件工具一样：除了"全部自动允许"，读它一律先问。
  3. 看起来存着密钥的地方：~/.ssh、~/.aws 这类文件夹，.env、私钥、凭据、shell 的历史记录这类文件。
     这一条是猜的（按名字），猜不全，所以只用来多拦一些，从不用来放行。

怎么判断"碰到"：命令里的每一个词都当作可能的路径（选项的值、git 的 HEAD:路径、@文件 也算），
展开 ~、相对于工作目录、顺着符号链接找到真正的位置，再和上面三种比。多当成路径只会让命令多问一次，
少当成一个就可能漏掉一个，所以宁多勿少。macOS 的文件名不分大小写（.ENV 就是 .env），也不分 Unicode 的
两种写法（é 可以是一个字符，也可以是 e 加一个音调），所以比较之前两样都先统一。
同一个文件的另一个名字（硬链接）按"是不是同一个文件"（inode）比，只对密钥文件做（数据目录里的文件太多）。

会把整个文件夹读一遍的命令（grep -r、rg、diff 两个文件夹），文件夹里面包含上面这些地方也不行，
还要先看一遍里面有没有像密钥的文件、指到别的文件夹去的符号链接，看不完（太大）也不行。
只列出名字的（find、ls -R、du、tree），列出数据目录里有哪些文件也不行（和文件工具一样，看数据目录要先问）；
跟着符号链接走的（ls -RL、find -L）同样要先看一遍有没有指到别处去的链接。

还有两类路径根本不去看：macOS 的 /net/主机名 一碰就会去连那台主机（自动挂载），判断时 stat 一下都等于联网；
/dev 下是设备，读一下可能有副作用。Windows 上对应的是 \\服务器\共享（一碰就去连，还会把登录凭据发过去）、
\\.\、\\?\ 这类设备路径，和 CON、NUL、COM1 这些在哪个文件夹里都是设备的名字（见 pathnames.py）。

Windows 上同一个文件还有别的写法：.env.、.env::$DATA 都是 .env（比较时由 pathnames.key 统一）；
ENV~1 这种短名字也可能是它（只看字符串认不出来，磁盘上真有这个名字就问）。
"""

from __future__ import annotations

import fnmatch
import os
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .. import pathnames
from ..config import READONLY_SCAN_ENTRIES
from .words import Unclear

# 看起来存着密钥的文件（比较文件名，不分大小写）
SECRET_FILES = (
    ".env", ".env.*", "*.env", ".envrc", ".netrc", "_netrc", ".pgpass", ".my.cnf", ".npmrc", ".pypirc", ".git-credentials",
    ".dockercfg", ".vault-token", ".htpasswd", "*credential*", "*secret*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore",
    "*.jks", "*.kdbx", "*.gpg", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", "shadow", "gshadow", "master.passwd",
    "sudoers", "environ", "*_history", ".lesshst", "cookies", "cookies.sqlite", "login data", "wp-config.php",
)
# 看起来存着密钥的文件夹：路径里连着出现这几段就算（不分大小写）
SECRET_DIRS = (
    (".ssh",), (".gnupg",), (".aws",), (".azure",), (".kube",), (".docker",), (".password-store",), ("keychains",),
    ("keyrings",), (".config", "gcloud"), (".config", "gh"), (".config", "hub"), (".terraform.d",),
)
# Windows 上另外几处：gcloud、gpg、GitHub CLI 放在 AppData 里；Windows 自己的凭据管理器、DPAPI 的主密钥、
# Vault；PowerShell 的命令历史（PSReadLine）
WINDOWS_SECRET_DIRS = (
    ("appdata", "roaming", "gcloud"), ("appdata", "roaming", "gnupg"), ("appdata", "roaming", "github cli"),
    ("microsoft", "credentials"), ("microsoft", "protect"), ("microsoft", "vault"), ("psreadline",),
)
# 一碰就有事的地方：/dev 下是设备；/net、/Network 是 macOS 的自动挂载（ls /net/某个域名 会去连那台主机）。
# Windows 上的见 _untouchable
UNTOUCHABLE = ("/dev", "/net", "/network")
PROC_SECRETS = ("environ", "mem", "kmem", "kcore", "pagemap")


def _untouchable(folded: str, windows: bool) -> bool:
    r"""一个 pathnames.key 算出来的路径是不是一碰就有事的地方。Windows 上：以 // 开头的（\\服务器\共享、\\.\设备、\\?\…，
    映射的网络盘展开之后也是这样），和 \??\ 开头的设备路径。"""
    if windows and folded.startswith(("//", "/??/")):
        return True
    return any(pathnames.inside(folded, item) for item in UNTOUCHABLE)


@dataclass
class Places:
    """一条命令运行的地方，以及哪些地方不能不问就读。"""

    workspace: Path
    protected: list[Path] = field(default_factory=list)  # 密钥文件、"不用问的命令"清单
    private: list[Path] = field(default_factory=list)  # 智狐的数据目录
    home: Path = field(default_factory=lambda: Path(os.path.expanduser("~")))
    windows: bool = pathnames.WINDOWS  # 按不按 Windows 的规矩看（测试里可以在别的系统上打开）

    def __post_init__(self) -> None:
        self._protected = [self._folded(path.resolve()) for path in self.protected]
        self._private = [self._folded(path.resolve()) for path in self.private]
        home = self.home.resolve()
        self._secret_dirs = [self._folded(home.joinpath(*parts)) for parts in _secret_dirs(self.windows)]
        self._inodes = set()
        for path in self.protected:
            try:
                info = os.stat(path)
            except OSError:
                continue
            self._inodes.add((info.st_dev, info.st_ino))

    # ---- 一个路径 ------------------------------------------------------------

    def _folded(self, path: str | Path) -> str:
        """比较路径用的写法：不分大小写，Unicode 统一成同一种写法，Windows 上同一个文件的几种写法也统一（见 pathnames.key）。"""
        return pathnames.key(path, self.windows)

    def resolve(self, raw: str) -> Path:
        """命令里的一个词，当作路径时指的是哪里（展开 ~、相对于工作目录、顺着符号链接）。"""
        # 先只按字面看一遍（不碰磁盘）：\\某个主机\共享、/net/某个主机 光是 stat 一下就会去连它，CON 一打开就是设备
        reason = pathnames.word_trick(raw, self.windows)
        if reason:
            raise Unclear(reason)
        if raw == "~" or raw.startswith("~/") or (self.windows and raw.startswith("~\\")):
            raw = str(self.home) + raw[1:]
        path = Path(raw)
        if not path.is_absolute():
            path = self.workspace / path
        if _untouchable(self._folded(os.path.normpath(path)), self.windows):
            raise Unclear(f"{raw} 是设备或者网络上的位置")
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError, ValueError) as exc:  # 符号链接绕成了圈、名字太长、有不能用的字符……
            raise Unclear(f"看不清 {raw} 指的是哪里") from exc
        # 展开之后还是一个链接：没能顺着它走到头（绕成了圈、指向的地方看不了）。Windows 上 resolve 碰到这种情况
        # 不报错，原样交回来（macOS、Linux 上会报错，走上面那一支）
        if pathnames.is_link(resolved):
            raise Unclear(f"看不清 {raw} 指的是哪里（链接没能走到头）")
        return resolved

    def check(self, raw: str) -> None:
        """命令里的一个词。当作路径时，它指向不该不问就读的地方，就抛出 Unclear。"""
        for candidate in _candidates(raw):
            self._check_one(candidate)

    def _check_one(self, raw: str) -> None:
        _secret_name(raw, self.windows)  # 先看写出来的样子（~/.ssh/x），再看顺着符号链接找到的真正位置
        path = self.resolve(raw)
        folded = self._folded(path)
        if any(pathnames.inside(folded, item) for item in self._protected):
            raise Unclear(f"{raw} 是保存密钥的文件")
        if any(pathnames.inside(folded, item) for item in self._private):
            raise Unclear(f"{raw} 在智狐自己的数据目录里")
        if any(pathnames.inside(folded, item) for item in self._secret_dirs):
            raise Unclear(f"{raw} 在存着密钥的文件夹里")
        if _untouchable(folded, self.windows):
            raise Unclear(f"{raw} 是设备或者网络上的位置")
        if folded.startswith("/proc/") and path.name.casefold() in PROC_SECRETS:
            raise Unclear(f"{raw} 是进程的内存或环境变量")
        _secret_name(str(path), self.windows)
        # 像短名字（ENV~1）、磁盘上又真有这个名字的：它可能是别的什么文件的另一个名字，只看字符串认不出是谁的。
        # 磁盘上没有的（HEAD~1 这种 git 的写法）什么都读不到，不用问
        if pathnames.short_name(raw, self.windows) and os.path.lexists(path):
            raise Unclear(f"{raw} 像 Windows 的短名字，看不清它是谁的别名")
        try:
            info = os.stat(path)
        except OSError:
            return  # 不存在、看不了：命令会报错，什么都读不到
        self._same_file(info, raw)

    def _same_file(self, info: os.stat_result, raw: str) -> None:
        if (info.st_dev, info.st_ino) in self._inodes:
            raise Unclear(f"{raw} 就是保存密钥的文件（另一个名字）")

    # ---- 一个文件夹 ----------------------------------------------------------

    def check_tree(self, raw: str, contents: bool, follow: bool = False) -> None:
        """一个会被整个走一遍的文件夹（grep -r、find……）。contents：会不会读里面每个文件的内容（grep -r 会，find 不会）；
        follow：会不会跟着符号链接走进别的文件夹（ls -RL、find -L）。

        里面包含数据目录、存着密钥的文件夹，就不行；会读内容的、会跟着链接走的，还要先看一遍里面
        （像密钥的名字、指向文件夹的符号链接、密钥文件的另一个名字），看不完（太大）也不行。
        """
        self.check(raw)
        path = self.resolve(raw)
        try:
            if not path.is_dir():
                return
        except OSError as exc:
            raise Unclear(f"看不清 {raw} 是什么") from exc
        folded = self._folded(path)
        watched = [*self._private, *self._secret_dirs, *(self._protected if contents else [])]
        if any(pathnames.inside(item, folded) for item in watched):
            raise Unclear(f"{raw} 里面有不该不问就读的地方")
        if contents or follow:
            self._scan(path, raw)

    def _scan(self, root: Path, raw: str) -> None:
        """把文件夹看一遍（不跟着符号链接走）：有像密钥的名字、指向文件夹的符号链接、密钥文件的另一个名字、看不完，都抛出 Unclear。

        Windows 的目录联接（junction）和符号链接一样算：DirEntry.is_symlink 认不出它，要靠 pathnames.entry_linked；
        DirEntry.stat 在 Windows 上没有 st_ino，比"是不是同一个文件"要用 pathnames.entry_info。
        """
        seen = 0
        pending = [root]
        while pending:
            folder = pending.pop()
            try:
                entries = list(os.scandir(folder))
            except OSError as exc:
                raise Unclear(f"看不了 {folder} 里面有什么") from exc
            for entry in entries:
                seen += 1
                if seen > READONLY_SCAN_ENTRIES:
                    raise Unclear(f"{raw} 里面的东西太多，看不完")
                try:
                    _secret_name(entry.path, self.windows)
                    if pathnames.entry_linked(entry, self.windows):
                        target = Path(entry.path).resolve()
                        if target.is_dir():
                            raise Unclear(f"{raw} 里面有指向别的文件夹的符号链接")
                        self._check_one(str(target))
                    elif entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    else:
                        self._same_file(pathnames.entry_info(entry, self.windows), entry.path)
                except (OSError, RuntimeError) as exc:
                    raise Unclear(f"看不清 {entry.path}") from exc


def _candidates(raw: str) -> list[str]:
    """一个词里可能藏着的路径：它本身；每个 = 和 : 后面的部分（--file=x、-o=jsonpath-file=x、HEAD:x）；
    去掉开头的 @ 以后的部分（GNU binutils 的 strings @文件 会把那个文件里的词当成参数读进来）。"""
    if not raw or raw == "-":
        return []
    found = [raw]
    for separator in "=:":
        for text in list(found):
            found.extend(text[at + 1 :] for at, char in enumerate(text) if char == separator)
    found.extend(text[1:] for text in list(found) if text.startswith("@"))
    return [item for item in dict.fromkeys(found) if item and item != "-"]


def _secret_dirs(windows: bool) -> tuple[tuple[str, ...], ...]:
    return SECRET_DIRS + WINDOWS_SECRET_DIRS if windows else SECRET_DIRS


def _secret_name(raw: str, windows: bool | None = None) -> None:
    """路径里有一段的名字像是存着密钥的（.env、id_rsa、.ssh……），就抛出 Unclear。
    Windows 上每一段先换成它真正指的名字（.env::$DATA、.env. 都是 .env，见 pathnames.names）。"""
    windows = pathnames.WINDOWS if windows is None else windows
    split = pathnames.names(raw, windows=True) if windows else PurePosixPath(raw).parts
    parts = [unicodedata.normalize("NFC", part).casefold() for part in split]
    for part in parts:
        if any(fnmatch.fnmatchcase(part, pattern) for pattern in SECRET_FILES):
            raise Unclear(f"{raw} 看起来存着密钥")
    for pattern in _secret_dirs(windows):
        size = len(pattern)
        if any(tuple(parts[index : index + size]) == pattern for index in range(len(parts) - size + 1)):
            raise Unclear(f"{raw} 在存着密钥的文件夹里")
    # .git 下面的 config（.git/config、.git/modules/x/config、.git/worktrees/x/config）：远程仓库的地址里常常带着 token
    if ".git" in parts[:-1] and parts[-1:] == ["config"]:
        raise Unclear(f"{raw} 是 git 的设置（远程地址里可能带着 token）")
