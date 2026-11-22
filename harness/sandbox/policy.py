"""沙箱的规矩：有哪几档、一条命令在沙箱里能读写哪些地方、哪些地方永远碰不到。

这里只算"哪些路径"，不管"怎么挡住"：macOS 上怎么写成 sandbox-exec 的配置、Linux 上怎么写成 bwrap 的参数，
在 macos.py、linux.py 里，它们都照着这里给的 Policy 写。所以两个系统上的规矩是同一份，只是做法不同。

一条命令在沙箱里（"只在工作目录里"和"工作目录 + 联网"两档都是这样，区别只在能不能联网）：

  能读写    工作目录；这次运行自己的 HOME 和临时文件夹（见下面）；你在设置里加的"可读写"的路径
  只能读    运行程序要用的系统目录（/usr、/bin、/System、Homebrew……，每个系统各自列在后端里）；你加的"只读"的路径
  在能读写的地方里也只能读
            智狐自己的程序（harness/、.venv、pyproject.toml、uv.lock、.git）：改了它，下次启动时改过的代码就在沙箱之外运行了；
            开终端、登录时会自动运行的文件（~/.zshrc、~/Library/LaunchAgents……）：同样的道理
  永远碰不到（读、写都不行，在工作目录里面也一样）
            智狐自己的数据目录（data/：以前的会话、设置、记忆、机器人的 token……）、.env（各家的 API key）、
            文件工具保护的那几个文件；家目录里放密钥的地方（~/.ssh、~/.aws……，见 HOME_SECRETS）
  别的地方  看不到：家目录里别的东西、别的硬盘、/tmp……

为什么 HOME 换成一个空的文件夹：命令照样以你的身份运行，git、npm、pip 这些程序启动时会去读家目录里的配置
（~/.gitconfig、~/.npmrc），写缓存（~/.npm、~/.cache）。家目录读不到时，有的程序直接报错退出（git 遇到
"Operation not permitted" 就是这样），有的写不了缓存装不上东西。给它一个这次运行自己的、空的 HOME，它们就照常工作，
又碰不到你真正的家目录。这个文件夹在系统的临时目录里，这次运行（一段对话、一次定时任务……）里的命令共用，用完删掉。

"家目录里放密钥的地方"为什么要单独挡：工作目录可能就是家目录（在服务器上 harness serve --workspace ~），
或者你把整个家目录加进了"额外允许的路径"，那时 ~/.ssh 也在里面。除非你把这个地方本身单独加进来，它照样挡着。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ..config import PROJECT_ROOT
from ..i18n import k

# 几档。名字给人看（页面、卡片、命令行都用它），顺序是从松到严以外的"关、严、宽"：设置里按这个顺序列
LEVELS = {
    "off": k("关"),
    "workspace": k("只在工作目录里"),
    "network": k("工作目录 + 联网"),
}
# 每一档一句话的说明（页面上选它的时候显示，卡片上也用）
LEVEL_TEXT = {
    "off": k("命令和以前一样直接在这台电脑上运行，只靠“要不要先问你”这一道关卡。"),
    "workspace": k("能读写工作目录，能读运行程序要用的系统目录；读不到家目录里别的东西，碰不到智狐自己的数据和 .env；不能联网，连这台电脑上的服务也连不上。远程使用时推荐。"),
    "network": k("和“只在工作目录里”一样，只是能联网（下载、git pull、调用接口）。这台电脑上的 Unix 套接字（Docker、ssh-agent）照样连不上。"),
}
# 卡片上那一行的简短说法
LEVEL_SHORT = {
    "workspace": k("读写工作目录，不能联网"),
    "network": k("读写工作目录，能联网"),
}
# 定时任务、聊天通道另外多一项：跟着设置里的那一档走（默认就是它）
DEFAULT = "default"
CHOICES = (DEFAULT, *LEVELS)

# 家目录里放密钥的地方：在任何一档、能读写的地方里面也碰不到（除非你单独把它本身加进"额外允许的路径"）
HOME_SECRETS = (
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".netrc", ".git-credentials", ".npmrc", ".pypirc",
    ".config/gh", ".config/gcloud", ".config/hub", ".password-store", ".local/share/keyrings", "Library/Keychains",
)
# 家目录里开终端、登录时会自动运行的文件：在能读写的地方里面也只能读（理由同上：改了它，以后就在沙箱之外运行）
HOME_STARTUP = (
    ".bashrc", ".bash_profile", ".bash_login", ".profile", ".bash_logout", ".zshrc", ".zprofile", ".zshenv", ".zlogin", ".zlogout",
    ".config/fish", ".config/autostart", ".config/systemd", ".config/environment.d", "Library/LaunchAgents",
)
# 智狐自己的程序（在 PROJECT_ROOT 下面的这几样）。包本身和正在用的 Python 环境另外算（见 program_paths）
PROGRAM_FILES = ("harness", ".venv", "pyproject.toml", "uv.lock", ".git", ".python-version")


def resolve(own: str, default: str) -> str:
    """一次运行实际用哪一档：定时任务、通道自己选了就用自己的，选的是"跟随设置"就用设置里的。不认识的按"关"以外最严的算。"""
    level = default if own == DEFAULT else own
    return level if level in LEVELS else "workspace"


def real(path: Path | str) -> Path:
    """展开 ~ 和符号链接以后的真实路径。macOS 的沙箱按真实路径比（/tmp 是 /private/tmp），Linux 的 bwrap 也要真实路径。"""
    return Path(os.path.realpath(os.path.expanduser(str(path))))


def program_paths() -> list[Path]:
    """智狐自己的程序在哪儿：包所在的文件夹、正在用的 Python 环境、项目里那几个决定装什么、运行什么的文件。"""
    package = Path(__file__).resolve().parent.parent
    places = [package, Path(sys.prefix), *(PROJECT_ROOT / name for name in PROGRAM_FILES)]
    found: list[Path] = []
    for place in places:
        if place.exists() and real(place) not in found:
            found.append(real(place))
    return found


@dataclass(frozen=True)
class Policy:
    """一条命令在沙箱里能碰哪些地方（全是真实路径）。后端照着它写配置；read 里不含系统目录，那是各个后端自己的。"""

    level: str  # workspace 或 network（"关"的时候根本没有 Policy）
    workspace: Path
    read: tuple[Path, ...]  # 只读：你加的只读的路径
    write: tuple[Path, ...]  # 能读写：工作目录、这次运行的 HOME 和临时文件夹、你加的可读写的路径
    readonly: tuple[Path, ...]  # 在能读写的地方里也只能读：智狐自己的程序、开终端时会运行的文件
    deny: tuple[Path, ...]  # 读写都不行：数据目录、.env、保护的文件、家目录里放密钥的地方
    home: Path  # 沙箱里的 HOME
    temp: Path  # 沙箱里的 TMPDIR
    program: tuple[Path, ...] = ()  # 智狐自己的程序（也在 readonly 里）：命令的 PATH 里去掉它们

    @property
    def network(self) -> bool:
        return self.level == "network"

    def env(self, env: dict[str, str]) -> dict[str, str]:
        """命令在沙箱里看到的环境变量：HOME 和临时文件夹换成这次运行自己的；指向真正家目录里的那几个（XDG_*、ssh-agent）去掉，
        不然程序会照着它们去找，碰壁报错。

        PATH 里智狐自己的 Python 环境（uv run 会把 .venv/bin 放在最前面）也去掉：那是智狐的程序，不是你的工具，
        而且它背后的 Python 多半装在家目录里（~/.local/share/uv），沙箱里运行不了，python3 会直接失败。
        去掉以后，python3 用的是系统的或者 Homebrew 的那个。"""
        dropped = {"SSH_AUTH_SOCK", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"}
        changed = {name: value for name, value in env.items() if name not in dropped}
        if changed.get("VIRTUAL_ENV") and inside(real(changed["VIRTUAL_ENV"]), self.program):
            del changed["VIRTUAL_ENV"]
        if "PATH" in changed:
            kept = [entry for entry in changed["PATH"].split(os.pathsep) if not entry or not inside(real(entry), self.program)]
            changed["PATH"] = os.pathsep.join(kept)
        temp = str(self.temp)
        changed.update(HOME=str(self.home), TMPDIR=temp, TMP=temp, TEMP=temp)
        return changed


def inside(path: Path, roots: Iterable[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def build(
    level: str,
    workspace: Path,
    extra: Iterable[dict],
    private: Iterable[Path],
    protected: Iterable[Path],
    home: Path,
    temp: Path,
    real_home: Path,
    program: Iterable[Path] = (),
) -> Policy:
    """照着上面的规矩，算出这一次的 Policy。extra 是设置里"额外允许的路径"（{"path", "write"}），
    private 是数据目录，protected 是文件工具保护的文件（.env 这些），real_home 是你真正的家目录。"""
    workspace = real(workspace)
    forbidden = [real(path) for path in (*private, *protected)]
    read: list[Path] = []
    write: list[Path] = [workspace, real(home), real(temp)]
    named: list[Path] = []  # 你单独加进来的路径：家目录里放密钥的地方，只有被你单独加了，才不挡
    for item in extra:
        path = real(item["path"])
        if inside(path, forbidden):
            continue  # 数据目录、.env：加了也不算（设置里也加不进来，见 settings.sandbox_settings）
        named.append(path)
        target = write if item.get("write") else read
        if path not in target:
            target.append(path)
    home_root = real(real_home)
    secrets = [home_root / name for name in HOME_SECRETS]
    deny = [*forbidden, *(path for path in secrets if not any(path == mine or mine.is_relative_to(path) for mine in named))]
    startup = [home_root / name for name in HOME_STARTUP if (home_root / name) not in named]
    program = [real(path) for path in program]
    readonly = [*program, *startup]
    return Policy(
        level=level,
        workspace=workspace,
        read=tuple(_unique(read)),
        write=tuple(_unique(write)),
        readonly=tuple(_unique(path for path in readonly if path not in forbidden)),
        deny=tuple(_unique(deny)),
        home=real(home),
        temp=real(temp),
        program=tuple(_unique(program)),
    )


def _unique(paths: Iterable[Path]) -> list[Path]:
    seen: list[Path] = []
    for path in paths:
        if path not in seen:
            seen.append(path)
    return seen
