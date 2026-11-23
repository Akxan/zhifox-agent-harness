"""自检的前三节：环境（Python、智狐的版本、系统）、目录（工作目录、数据目录、.env）、占用（数据目录现在被谁占着）。

"只有你自己能读"怎么看：
  - macOS、Linux 上看权限位和属于谁。数据目录要 700，.env 和数据目录里存密钥的那几个文件要 600（safe.py 就是这样建的）；
    属于别的账号的（多半是用 sudo 运行过一次，东西都成了 root 的）自己改不动，也算有问题。
  - Windows 上看访问控制列表（winacl.others）：除了你、SYSTEM、管理员，还有谁能用。查不了的（U 盘、网络盘不是 NTFS）
    也算有问题：那样的盘上谁都能读。
只看不改：修法写在 fix 里，由你自己决定要不要运行。
"""

from __future__ import annotations

import os
import platform
import shutil
import stat
import sys
from pathlib import Path

from .. import instance, machine, winacl
from ..config import COMMAND, FROM_SOURCE, PROJECT_ROOT, Config, env_file_names
from ..i18n import Message, k, t
from ..version import (  # 智狐的版本和提交只在 harness/version.py 里算（harness --version 也用它）
    commit,
    version,
)
from . import fixes
from .report import OK, PROBLEM, UNUSED, Check, Section

MIN_PYTHON = (3, 11)

# 占着数据目录的是哪一种（instance.py 的 kind），给人看的说法。和 service/cli.py 的 WHO 是同样的几句，
# 只有 service 那一句不同：那边是站在"另一个后台服务"的角度说的
HOLDERS = {
    "web": k("在终端里开着的网页界面（harness --web）"),
    "serve": k("在终端里或者 systemd 里开着的 harness serve"),
    "service": k("后台服务（harness service）"),
    "channels": k("harness channels pair（正在等配对码）"),
    "restore": k("harness restore（正在从备份恢复）"),
    "terminal": k("终端界面（harness）"),
}

# 用来找程序的函数（测试换成假的，免得看这台电脑上装没装 uv）
which = shutil.which


# ---- 环境 ------------------------------------------------------------------------


def system_label() -> str:
    """这台电脑的系统，给人看的：macOS 15.6 · arm64。"""
    system, arch = machine.system(), platform.machine()
    if system == machine.MAC:
        name = f"macOS {platform.mac_ver()[0]}".strip()
    elif system == machine.WINDOWS:
        name = f"Windows {platform.release()} ({platform.version()})"
    else:
        try:
            name = platform.freedesktop_os_release().get("PRETTY_NAME") or f"Linux {platform.release()}"
        except OSError:
            name = f"{platform.system()} {platform.release()}"
    return f"{name} · {arch}" if arch else name


def environment() -> Section:
    python = ".".join(map(str, sys.version_info[:3]))
    venv = Path(sys.prefix).resolve() == (PROJECT_ROOT / ".venv").resolve()
    if not FROM_SOURCE:
        where = t("{path}（装成了工具）", path=sys.executable)  # uv tool 自己的环境，没有"项目的 .venv"
    else:
        where = t("{path}（项目的 .venv）", path=sys.executable) if venv else t("{path}（不是项目的 .venv）", path=sys.executable)
    checks = [
        Check(
            "python", "Python", OK if sys.version_info >= MIN_PYTHON else PROBLEM, f"{python} · {where}",
            [] if sys.version_info >= MIN_PYTHON else [t("智狐要 Python 3.11 或者更新的版本。用 uv 运行（uv run harness）会自动用对的那个。")],
            {"version": python, "executable": sys.executable, "project_venv": venv},
        )
    ]
    found, head = version(), commit()
    shown = t("{version} · 提交 {commit}", version=found or "?", commit=head) if head else (found or "?")
    checks.append(Check("zhifox", t("智狐"), OK, shown, facts={"version": found, "commit": head}))
    checks.append(Check("system", t("系统"), OK, system_label(), facts={"system": machine.system(), "platform": platform.platform()}))
    uv = which("uv")
    if uv:
        checks.append(Check("uv", "uv", OK, uv, facts={"path": uv}))
    else:
        detail = t("找不到 uv：这份说明里的命令（uv run harness …）都要用它。")
        checks.append(Check("uv", "uv", PROBLEM, detail, [fixes.install_uv(), t("装好以后另开一个终端窗口。")], {"path": ""}))
    return Section("environment", t("环境"), checks)


# ---- 目录 ------------------------------------------------------------------------


def _windows() -> bool:
    return machine.system() == machine.WINDOWS


def _mode(path: Path) -> str:
    return oct(stat.S_IMODE(path.stat().st_mode))[2:]


def _foreign(path: Path) -> bool:
    """属于别的账号（macOS、Linux 上）。"""
    getuid = getattr(os, "getuid", None)
    return getuid is not None and path.stat().st_uid != getuid()


def _shared(path: Path) -> bool:
    """别的账号也能读、能进（权限位里给了同组的人或者所有人）。"""
    return bool(stat.S_IMODE(path.stat().st_mode) & 0o077)


def _acl_problem(path: Path) -> Message | None:
    """Windows 上：path 的访问控制列表有没有问题。没问题返回 None。"""
    found = winacl.others(path)
    if found is None:
        return t("查不了它的访问权限（icacls 没能读出来）：U 盘、网络盘和不是 NTFS 的盘上没有访问控制，这台电脑上谁都能读。")
    if found:
        return t("这台电脑上别的账号也能读它：{who}", who=t("、").join(found))
    return None


def _workspace(config: Config) -> Check:
    path = config.workspace
    facts = {"path": str(path)}
    if not path.is_dir():
        return Check("workspace", t("工作目录"), PROBLEM, t("{path} 不存在", path=path), [t("换一个工作目录：{command} --workspace 文件夹", command=COMMAND)], facts)
    if not os.access(path, os.W_OK):
        detail = t("{path}：你的账号写不了这里，助手建不了文件、改不了文件", path=path)
        return Check("workspace", t("工作目录"), PROBLEM, detail, [t("换一个工作目录：{command} --workspace 文件夹", command=COMMAND)], facts)
    return Check("workspace", t("工作目录"), OK, str(path), facts=facts)


def _data_dir(config: Config) -> Check:
    path = config.data_dir
    label = t("数据目录")
    named = bool(os.environ.get("HARNESS_DATA_DIR"))
    facts: dict = {"path": str(path), "exists": path.exists(), "from_env": named}
    where = t("{path}（HARNESS_DATA_DIR 指定的）", path=path) if named else str(path)
    if not path.exists():
        return Check("data_dir", label, UNUSED, t("{where}：还没有建。第一次保存东西时会建好，只有你自己的账号能进", where=where), facts=facts)
    if not path.is_dir():
        return Check("data_dir", label, PROBLEM, t("{where}：这不是一个文件夹", where=where), [t("把它挪走，或者用 HARNESS_DATA_DIR 换一个数据目录。")], facts)
    problems: list[str] = []
    fix: list[str] = []
    if _windows():
        found = _acl_problem(path)
        if found is not None:
            problems.append(found)
            fix.append(fixes.private_folder(path))
    else:
        facts["mode"] = _mode(path)
        if _foreign(path):
            problems.append(t("它属于另一个账号（是不是用 sudo 运行过智狐？），你自己改不动里面的东西"))
            fix.append(fixes.take_back(path))
        if _shared(path):
            problems.append(t("别的账号也能进：权限是 {mode}，应该是 700", mode=facts["mode"]))
            fix.append(fixes.private_folder(path))
        # 存密钥的那几个文件（channels.json、mcp.json……）：数据目录是 700 的话别人本来就进不来，这里再看一眼，免得哪天目录的权限被改宽了
        loose = [item for item in config.protected_files if item.parent == path and item.is_file() and _shared(item)]
        if loose:
            problems.append(t("这几个存密钥的文件别的账号也能读（应该是 600）：{names}", names=", ".join(item.name for item in loose)))
            fix.append(fixes.private_files(loose))
            facts["loose_files"] = [item.name for item in loose]
    if not problems and not os.access(path, os.W_OK):
        problems.append(t("你的账号写不了它：会话、设置都存不下来"))
        fix.append(fixes.take_back(path) if not _windows() else fixes.private_folder(path))
    if problems:
        return Check("data_dir", label, PROBLEM, t("{where}：{problems}", where=where, problems=t("；").join(problems)), fix, facts)
    private = t("只有你自己的账号（和 SYSTEM、管理员）能用") if _windows() else t("只有你自己的账号能进（700）")
    return Check("data_dir", label, OK, t("{where} · {private}", where=where, private=private), facts=facts)


def _env_file(config: Config) -> Check:
    path = config.env_file
    named = bool(os.environ.get("HARNESS_ENV_FILE"))
    where = t("{path}（HARNESS_ENV_FILE 指定的）", path=path) if named else str(path)
    facts: dict = {"path": str(path), "exists": path.is_file(), "from_env": named}
    if not path.is_file():
        return Check("env_file", ".env", UNUSED, t("{where}：还没有。在网页的“设置”里填密钥时会建好它", where=where), facts=facts)
    names = len(env_file_names(path))  # 只数有几项，名字和值都不碰
    facts["entries"] = names
    problems: list[str] = []
    fix: list[str] = []
    if _windows():
        found = _acl_problem(path)
        if found is not None:
            problems.append(found)
            fix.append(fixes.private_files([path]))
    else:
        facts["mode"] = _mode(path)
        if _foreign(path):
            problems.append(t("它属于另一个账号（是不是用 sudo 运行过智狐？）"))
            fix.append(fixes.take_back(path))
        if _shared(path):
            problems.append(t("别的账号也能读里面的密钥：权限是 {mode}，应该是 600", mode=facts["mode"]))
            fix.append(fixes.private_files([path]))
    if problems:
        return Check("env_file", ".env", PROBLEM, t("{where}：{problems}", where=where, problems=t("；").join(problems)), fix, facts)
    private = t("只有你自己的账号（和 SYSTEM、管理员）能用") if _windows() else t("只有你自己的账号能读（600）")
    return Check("env_file", ".env", OK, t("{where} · 设置了 {n} 项 · {private}", where=where, n=names, private=private), facts=facts)


def folders(config: Config) -> Section:
    return Section("folders", t("目录"), [_workspace(config), _data_dir(config), _env_file(config)])


# ---- 占用 ------------------------------------------------------------------------


def occupancy(config: Config) -> Section:
    """数据目录现在被谁占着（instance.py）。只看不改：试着锁一下马上放开，不动 .lock 里写着的是谁。

    被占着不算问题（你开着网页界面，它就占着）；知道是谁，"打不开第二个"的时候才知道该去关哪一个。"""
    label = t("数据目录的占用")
    if not instance.in_use(config.data_dir):
        return Section("occupancy", t("占用"), [Check("holder", label, OK, t("现在没有别的智狐开着"), facts={"in_use": False})])
    found = instance.holder(config.data_dir)
    if found is None:
        detail = t("有一个智狐开着（旧版本，没写是哪一种）")
        facts: dict = {"in_use": True}
    else:
        detail = t("{who}开着，进程 {pid}", who=t(HOLDERS[found.kind]), pid=found.pid)
        facts = {"in_use": True, "kind": found.kind, "pid": found.pid}
    return Section("occupancy", t("占用"), [Check("holder", label, OK, detail, facts=facts)])
