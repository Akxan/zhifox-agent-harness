r"""智狐是怎么装在这台电脑上的，以及装成工具时，数据和密钥放在哪儿。

有两种运行方式：

  从仓库里运行   git clone（或者下载 zip 解压）下来，在那个文件夹里 uv sync、uv run harness。
                uv sync 把包以"可编辑"的方式装进 .venv：.venv 里只放一个指回仓库的 .pth，代码还是仓库里的 harness/。
                数据和密钥就在仓库里：data/ 和 .env（.gitignore 排除了它们）。这是一直以来的做法，一个字都不变。
  装成了工具     uv tool install git+https://github.com/…（或者 pip install）：包被复制进某个 Python 环境的
                site-packages，仓库里别的东西（pyproject.toml、tests/、evals/、.gitignore）都不在。
                这时候"包的上一层"是 site-packages 本身：数据要是还放在那儿，就写进了安装目录，
                升级（uv tool upgrade）、卸载时连同会话、记忆、密钥一起被删掉。所以改放到每个用户自己的位置（见 user_dir）。

怎么分（只在 from_source 里判断，别处用 config.FROM_SOURCE）：看包的上一层是不是智狐的仓库。两样都满足才算从仓库里运行：
  1. 那里有 pyproject.toml，而且写着 name = "harness-assistant"：是我们自己的项目。wheel 里从来不带 pyproject.toml；
     只看"有没有这个文件"不够：有人把 harness/ 抄进自己的项目，上一层的 pyproject.toml 是他的，不是我们的。
  2. 那一层不叫 site-packages 或 dist-packages（Debian、Ubuntu 的系统 Python 用后一个名字）。装好的包永远在这样一个
     文件夹里，三个系统都一样（Windows 上是 Lib\site-packages；名字不分大小写，所以比小写）。这是一道保险：
     万一哪个包把一份 pyproject.toml 装进了 site-packages，也不会因此把数据写进安装目录。
不看的几样，以及为什么：
  - sys.prefix、VIRTUAL_ENV：uv sync 的 .venv 和 uv tool 的环境都是虚拟环境，从这里看不出区别。
  - 有没有 .git、能不能运行 git：下载 zip 解压出来的仓库没有 .git，照样是从仓库里运行。
  - 和 sysconfig 给的 site-packages 路径比：要处理大小写、符号链接、好几个 site-packages（用户级的、系统的），
    比看名字容易出错，而得到的答案是一样的。
可编辑的安装（uv tool install --editable .、pip install -e .）包还在仓库里，算从仓库里运行：那是在改智狐的人，
数据照旧放在仓库的 data/ 里。

装成工具时的位置（user_dir）：
  macOS    ~/Library/Application Support/ZhiFox      苹果给"程序自己的数据"定的地方。也不在"文稿""桌面"里，
                                                     后台服务读它不用你去"完整磁盘访问权限"里授权（见 service/plan.py）
  Linux    $XDG_DATA_HOME/zhifox，默认 ~/.local/share/zhifox    XDG 规范：变量没设、是空的、不是绝对路径，都按默认的算
  Windows  %LOCALAPPDATA%\ZhiFox                     不用 %APPDATA%（"漫游"的那个）：登录域账号时它会在每次登录、注销时
                                                     整个同步到服务器上。智狐的数据不小（附件的原件、浏览器的配置文件夹和缓存、
                                                     改文件前的留底），漫游会拖慢登录、撑爆配额；而且里面有很多只属于这台电脑的
                                                     东西：占住数据目录的锁、任务计划程序里的后台服务、浏览器的登录状态。
                                                     Python 常用的 platformdirs 默认也是 Local
.env 放在数据目录里（三个系统一样），不放 Linux 的 $XDG_CONFIG_HOME：很多人把 ~/.config 整个放进 git 管理（dotfiles），
密钥放在那里，一次 git push 就公开了；~/.local/share 很少有人这样做。放在一起还有两个好处：数据目录本来就只有你的账号能进
（700，Windows 上是 ACL，见 safe.private_dir），三个系统上的说法也一样（"都在这一个文件夹里"）。
它在数据目录里也碰不到：导出时按名字和路径跳过（export.py），文件工具按路径拒绝（Config.protected_files），
沙箱里整个数据目录都不让碰（sandbox/policy.py）。

文件夹的名字（FOLDER）故意不跟着 config.APP_NAME_LATIN 走：给人看的名字以后可能会改，存数据的文件夹不能跟着搬家，
不然改一次名字，用户的会话、记忆就"不见了"。这和包名、命令一直叫 harness 是一个道理。

HARNESS_DATA_DIR、HARNESS_ENV_FILE 永远优先（见 config.load_config），两种运行方式都一样；两个各管各的：
只设了 HARNESS_DATA_DIR 时，.env 还在默认的位置，和从仓库里运行时一样。
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import tomllib
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

from . import machine

PROJECT_NAME = "harness-assistant"  # pyproject.toml 里的 name，也是 uv tool upgrade、uv tool uninstall 后面写的名字
REPOSITORY = "https://github.com/Akxan/zhifox-agent-harness"  # 一行安装、升级的说明里用它（uv tool install git+…）
INSTALLED_FOLDERS = ("site-packages", "dist-packages")
# 装成工具时数据文件夹的名字（见开头："故意不跟着 APP_NAME_LATIN 走"）。Linux 上按惯例用小写
FOLDER = "ZhiFox"
LINUX_FOLDER = "zhifox"
ENV_NAME = ".env"


def from_source(root: Path) -> bool:
    """root（包的上一层，config.PROJECT_ROOT）是不是智狐的仓库。判断的办法和理由见开头的说明。"""
    if root.name.lower() in INSTALLED_FOLDERS:
        return False
    try:
        with open(root / "pyproject.toml", "rb") as file:
            project = tomllib.load(file).get("project")
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return False
    return isinstance(project, dict) and project.get("name") == PROJECT_NAME


def user_dir(system: str | None = None, environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    """装成工具时，这个用户的数据放在哪儿（见开头的表）。三个参数只给测试用：不给就看这台电脑。"""
    system = system or machine.system()
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else home
    if system == machine.MAC:
        return home / "Library" / "Application Support" / FOLDER
    if system == machine.WINDOWS:
        return _absolute(environ.get("LOCALAPPDATA"), home / "AppData" / "Local", ntpath) / FOLDER
    return _absolute(environ.get("XDG_DATA_HOME"), home / ".local" / "share", posixpath) / LINUX_FOLDER


def _absolute(value: str | None, default: Path, rules: ModuleType) -> Path:
    """环境变量里的一个文件夹：没设、是空的、不是绝对路径（XDG 规范说这种要当作无效、忽略掉），都用默认的。
    rules 是按哪个系统的规矩看"是不是绝对路径"（ntpath 或 posixpath）：在 Mac 上测试 Windows 的情况时也按 Windows 的算。"""
    if value and rules.isabs(value):
        return Path(value)
    return default


def default_data_dir(root: Path, source: bool) -> Path:
    """没设 HARNESS_DATA_DIR 时的数据目录。"""
    return root / "data" if source else user_dir()


def default_env_file(root: Path, source: bool) -> Path:
    """没设 HARNESS_ENV_FILE 时的 .env。不跟着 HARNESS_DATA_DIR 走（见开头的说明）。"""
    return root / ENV_NAME if source else user_dir() / ENV_NAME


def repair(source: bool) -> str:
    """装坏了（找不到 Python、少了文件）时让你运行的那条命令：从仓库里运行是 uv sync（在仓库里运行），
    装成工具是重新装一遍。用 --force 不用 --reinstall：试过（uv 0.13），工具环境里的 python 不见了时，
    --reinstall 直接报"Invalid environment"，--force 会把整个环境重建起来。数据不在工具环境里，重新装不会碰到它。"""
    return "uv sync" if source else f"uv tool install --force git+{REPOSITORY}"


def command(source: bool) -> str:
    """告诉你"运行这条命令"时，智狐的命令怎么写：从仓库里运行是 uv run harness（要在仓库里运行），
    装成工具是 harness（装的时候放进了 PATH，在哪个文件夹里都能用）。
    装成工具时不写 uv run harness：在别人的 uv 项目里运行它，uv 会先替那个项目建 .venv、装依赖。"""
    return "uv run harness" if source else "harness"
