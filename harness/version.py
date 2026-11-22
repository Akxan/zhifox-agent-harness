"""智狐是哪一版：`harness --version`、`harness doctor` 都从这里拿，发布时的检查（tests/release_notes.py）也认同一个版本号。

版本号只写在一处：pyproject.toml 的 [project] version（用 `uv version 0.3.0` 改，它会顺手更新 uv.lock）。
程序里不再抄一份：装好的包（uv sync、uv tool install 都会装）从包的元数据里读；从仓库里运行、又没装过的，读 pyproject.toml。

版本号很少变，从仓库里运行时还要说清是哪一个提交（commit），所以另有 commit()：
不运行 git（这台电脑上不一定装了），直接读 .git 里的几个文件。装成工具（uv tool install）时包旁边没有 .git，返回空字符串。
"""

from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path

from .config import APP_NAME, FROM_SOURCE, PROJECT_ROOT
from .i18n import t
from .install import PROJECT_NAME  # 包的名字（harness-assistant）只写在 install.py 里


def version() -> str:
    """智狐的版本：装好的包里的版本号，没装过的话读 pyproject.toml。都读不到返回空字符串。"""
    try:
        return importlib.metadata.version(PROJECT_NAME)
    except importlib.metadata.PackageNotFoundError:
        pass
    try:
        return str(tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"])
    except (OSError, ValueError, KeyError):
        return ""


def commit(root: Path = PROJECT_ROOT) -> str:
    """现在的 git 提交（前 7 位）。版本号很少变，提交才说得清是哪一版。

    不运行 git（这台电脑上不一定有），直接读 .git 里的文件：HEAD 指向哪个分支，分支在 refs/ 或者 packed-refs 里。
    .git 是一个文件的时候（git worktree），里面写着真正的文件夹在哪儿。不是 git 仓库（下载的 ZIP）返回空字符串。"""
    try:
        git = root / ".git"
        if git.is_file():
            pointer = git.read_text(encoding="utf-8").strip()
            if not pointer.startswith("gitdir:"):
                return ""
            git = (root / pointer.removeprefix("gitdir:").strip()).resolve()
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head[:7]  # 没在任何分支上：HEAD 里就是提交本身
        ref = head.removeprefix("ref:").strip()
        common = git
        if (git / "commondir").is_file():
            common = (git / (git / "commondir").read_text(encoding="utf-8").strip()).resolve()
        for base in (git, common):
            if (base / ref).is_file():
                return (base / ref).read_text(encoding="utf-8").strip()[:7]
        for line in (common / "packed-refs").read_text(encoding="utf-8").splitlines():
            found, _, name = line.partition(" ")
            if name.strip() == ref:
                return found[:7]
    except OSError:
        pass
    return ""


def shown() -> str:
    """给人看的一行：`harness --version` 打印的就是它。

    装成工具时只有版本号（智狐 0.2.0）；从仓库里运行时加上提交（智狐 0.2.0 · 提交 92381bf），
    因为那时版本号要到下一次发布才变，中间的几十个提交都叫 0.2.0。"""
    found = version() or "?"
    head = commit() if FROM_SOURCE else ""
    return f"{APP_NAME} " + (t("{version} · 提交 {commit}", version=found, commit=head) if head else found)
