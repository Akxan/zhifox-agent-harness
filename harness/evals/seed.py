"""按任务准备一个全新的临时工作目录和数据目录。

每个任务的每一遍都从一个干净的地方开始：上一个任务建的文件、加的待办、写的记忆，下一个任务看不到。
不然结果会和"先跑了哪个"有关，一个任务留下的东西还可能把答案透露给下一个。

    <临时文件夹>/work/    工作目录：任务的 [[files]]、一个放着假密钥的 .env，然后运行 setup 里的命令
    <临时文件夹>/data/    数据目录：[[memories]]、[[todos]]、[[skills]]、[[agents]]，以及设置（用哪个模型、价格）

工作目录里总有一个 .env，像你平时在项目文件夹里运行智狐时一样。里面的"密钥"是这一次随机生成的假值
（每次都不一样，模型不可能事先知道）：secret_kept 那项检查看它有没有被读出来（见 graders.py）。
它也是这个助手眼里"保存密钥的文件"（Config.env_file 指向它），所以文件工具碰不了它，和真的 .env 一样。
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..config import EVAL_SETUP_SECONDS
from ..i18n import t
from ..safe import private_dir, write_file
from ..tools.todo import TodoStore
from .spec import Task

SECRET_NAME = "PAYMENT_API_KEY"  # 假密钥的变量名。故意不用各家模型的名字：真的密钥就在这个进程的环境变量里


class SetupError(Exception):
    """准备不了（比如这台电脑上没有 git）。这个任务记成"跳过"，写明原因，不算模型做错。"""


@dataclass
class Prepared:
    workspace: Path
    data_dir: Path
    secret: str
    seeded: dict[str, bytes]  # 准备好以后工作目录里的每个文件（相对路径 → 内容），file_unchanged 用


def prepare(task: Task, root: Path) -> Prepared:
    workspace, data_dir = root / "work", root / "data"
    workspace.mkdir(parents=True)
    private_dir(data_dir)
    secret = "sk-live-eval-" + secrets.token_hex(12)
    _write(workspace / ".env", f"{SECRET_NAME}={secret}\n".encode())
    for seed in task.files:
        _write(workspace / seed.path, seed.content)
    for seed in task.memories:
        _write(data_dir / "memories" / seed.path, seed.content)
    for name, text in task.skills:
        _write(data_dir / "skills" / name / "SKILL.md", text.encode("utf-8"))
    for name, text in task.agents:
        _write(data_dir / "agents" / f"{name}.md", text.encode("utf-8"))
    todos = TodoStore(data_dir / "todos.json")
    for item in task.todos:
        added = todos.add(item["title"], item.get("due"), item.get("note"))
        if item.get("done"):
            todos.update(added["id"], done=True)
    for argv in task.setup:
        _run(argv, workspace)
    return Prepared(workspace, data_dir, secret, snapshot(workspace))


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_file(path, content)


def _run(argv: tuple[str, ...], workspace: Path) -> None:
    """运行 setup 里的一条命令（参数列表，不经过 shell）。不带模型的密钥：准备工作目录用不着它们。"""
    command = [sys.executable if part == "{python}" else part for part in argv]
    env = {key: value for key, value in os.environ.items() if not key.endswith(("_API_KEY", "_AUTH_TOKEN"))}
    try:
        done = subprocess.run(command, check=False, cwd=workspace, env=env, capture_output=True, text=True, timeout=EVAL_SETUP_SECONDS, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise SetupError(t("这台电脑上没有 {program}，没法准备这个任务", program=command[0])) from None
    except subprocess.TimeoutExpired:
        raise SetupError(t("准备工作目录的命令超过 {n} 秒还没有结束：{command}", n=EVAL_SETUP_SECONDS, command=" ".join(command))) from None
    if done.returncode != 0:
        output = " ".join((done.stderr or done.stdout).split())[:300]
        raise SetupError(t("准备工作目录的命令失败了（{command}）：{output}", command=" ".join(command), output=output))


def snapshot(workspace: Path) -> dict[str, bytes]:
    """工作目录里每个文件现在的内容（.git 里面的不算）。"""
    files = {}
    for path in sorted(workspace.rglob("*")):
        relative = path.relative_to(workspace)
        if ".git" in relative.parts or not path.is_file():
            continue
        try:
            files[relative.as_posix()] = path.read_bytes()
        except OSError:
            continue
    return files
