"""智狐自己的 Agent 文件夹：data/agents/。一个 Agent 一个 Markdown 文件，文件名就是它的 name。

    data/agents/
      资料研究员.md
      code-reviewer.md

这里只管"文件夹里有什么"：列出来、读、新建、改、删、把导入的和示例放进去。
检查一个文件合不合格是 spec.py 的事；从别的工具找文件是 from_tools.py 的事。

页面送来的名字不能全信：可能被改成 "../../.env" 或者一个绝对路径，文件夹里也可能藏着指向别处的链接。
所以名字只能是一段（不带斜杠、不以点开头），文件必须真的是这个文件夹里的一个普通文件，不是链接
（和 skills/store.py、memories.py 是同一套检查；Windows 的目录联接也算链接，见 pathnames.py）。

几个网页请求可能同时来（一边保存、一边删除），子 Agent 也会同时来读。写文件都是"先写临时文件再换上去"
（见 safe.write_file），读的那一边永远看到完整的文件；"先看有没有同名的、再写"这种两步的操作拿着 self._lock 做完。
"""

from __future__ import annotations

import os
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .. import pathnames
from ..config import MAX_AGENT_BYTES, MAX_AGENTS
from ..i18n import Message, t
from ..safe import clean_text, private_dir, write_file
from ..skills.frontmatter import FrontmatterError, parse
from ..skills.store import SkillError, as_text, described, read_bytes
from . import tools as agent_tools
from .spec import KNOWN, SUFFIX, Spec, check, fields_of, name_problem, normal


class AgentError(Exception):
    """这个 Agent 读不了、改不了或者用不了。错误信息可以直接显示给用户。"""


class BadRequest(AgentError):
    """请求本身不对（缺了参数、类型不对）。正常使用页面时不会出现，网页接口回答 400。"""


class AgentConflict(AgentError):
    """你在编辑的时候，文件在磁盘上被改过了。"""


class AgentInvalid(AgentError):
    """文件不符合要求。problems 是每一条问题。"""

    def __init__(self, problems: list[Message]):
        super().__init__(problems[0] if problems else t("这个 Agent 文件不符合要求。"))
        self.problems = problems


@dataclass
class CustomAgent:
    """列表里的一个 Agent。"""

    name: str  # 文件名去掉 .md
    path: Path
    spec: Spec = field(default_factory=Spec)
    problems: list[Message] = field(default_factory=list)  # 空的才算合格，才能用
    modified: float = 0.0
    size: int = 0

    @property
    def usable(self) -> bool:
        return not self.problems

    def summary(self) -> dict:
        """发给页面的那一份（列表里显示的东西，不含正文）。"""
        spec = self.spec
        picked = agent_tools.form(spec.tools)
        return {
            "name": self.name,
            "description": spec.description,
            "model": spec.model,  # 认出来的"提供方/模型"；空的表示和叫它的那一方一样
            "color": spec.color,
            # 能用几样工具：all 表示和叫它的那一方一样多，否则是勾上的几样（每个 MCP 工具、每个服务器各算一样）
            "tools": {"all": picked["all"], "count": len(picked["picked"])},
            "problems": [described(problem) for problem in self.problems],
            "warnings": [described(warning) for warning in spec.warnings],
            "modified": round(self.modified, 3),
            "size": self.size,
        }


class AgentStore:
    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.Lock()

    # ---- 列出 --------------------------------------------------------------

    def list(self) -> list[CustomAgent]:
        """文件夹里的全部 Agent，按名字排好。以 . 开头的不列（写到一半的临时文件、访达生成的 .DS_Store），不是 .md 的也不列。"""
        try:
            with os.scandir(self.root) as listing:
                entries = sorted(listing, key=lambda entry: normal(entry.name))
        except OSError:
            return []  # 还没有这个文件夹：一个都没有
        found = []
        for entry in entries:
            if len(found) >= MAX_AGENTS:
                break
            if entry.name.startswith(".") or not entry.name.endswith(SUFFIX) or len(entry.name) == len(SUFFIX):
                continue
            agent = CustomAgent(name=normal(clean_text(entry.name[: -len(SUFFIX)])), path=Path(entry.path))
            if pathnames.entry_linked(entry):
                agent.problems.append(t("这是一个指向别处的链接。为了安全，智狐不读链接；请用“导入”把它复制进来。"))
            elif entry.is_file(follow_symlinks=False):
                self._fill(agent)
            else:
                continue
            found.append(agent)
        return found

    def get(self, name: object) -> CustomAgent:
        """按名字找到一个 Agent（不管合不合格）。名字是页面或模型给的，先检查它只是一段名字，不是路径。"""
        path = self._file(name)
        agent = CustomAgent(name=normal(str(name)), path=path)
        self._fill(agent)
        return agent

    def usable(self, name: object) -> CustomAgent:
        """一个能用的 Agent。没有、或者不合格的，抛出 AgentError，说清楚为什么。"""
        agent = self.get(name)
        if agent.problems:
            raise AgentInvalid(agent.problems)
        return agent

    def catalog(self) -> list[tuple[str, str]]:
        """告诉模型的那些：合格的。[(名字, 它擅长什么、什么时候交给它)]"""
        return [(agent.name, agent.spec.description) for agent in self.list() if agent.usable]

    def _fill(self, agent: CustomAgent) -> None:
        try:
            text, info = self._read_text(agent.path)
        except AgentError as exc:
            agent.problems.append(exc.args[0])
            return
        agent.modified, agent.size = info.st_mtime, info.st_size
        agent.spec, problems = check(text, agent.name)
        agent.problems += problems

    # ---- 查看 --------------------------------------------------------------

    def read(self, name: object) -> dict:
        """打开一个 Agent：全文、表单里的各项、问题。"""
        agent = self.get(name)
        try:
            text, _ = self._read_text(agent.path)
        except AgentError:
            text = ""
        return {
            **agent.summary(),
            "text": text,
            "fields": self.fields(text),  # 读不出 frontmatter 时是 None：只能在原文里改
            "model_text": agent.spec.model_text,
            "extra": agent.spec.extra,
            "editable": bool(text) or agent.size == 0,
            "path": str(agent.path),
        }

    @staticmethod
    def fields(text: str) -> dict | None:
        """一份原文在表单里的样子（读不出 frontmatter 就是 None）。"""
        try:
            parse(text, KNOWN)
        except FrontmatterError:
            return None
        spec, _ = check(text, None)
        return fields_of(spec)

    def file_of(self, name: object | None) -> Path:
        """在文件管理器里显示用：一个 Agent 的文件（检查过的），或者整个 Agent 文件夹。"""
        if name is None:
            private_dir(self.root)  # 还不存在的话先建一个空的，不然点了没反应
            return self.root
        return self._file(name)

    # ---- 修改 --------------------------------------------------------------

    def create(self, text: object) -> str:
        """新建一个 Agent（表单写出来的、或者你在原文里写的）。返回它的名字。"""
        text = self._checked(text)
        spec, problems = check(text, None)
        if problems:
            raise AgentInvalid(problems)
        with self._lock:
            if self._exists(spec.name):
                raise AgentError(t("已经有一个叫 {name} 的 Agent 了。", name=spec.name))
            self._write(self._path(spec.name), text)
        return spec.name

    def save(self, name: object, text: object, modified: float | None, force: bool = False) -> str:
        """改一个 Agent。返回改完之后的名字：name 改了，文件也跟着改名。

        modified 是你打开它时看到的修改时间。磁盘上的对不上，说明这期间它被别处改过了：不是 force 的话不保存，
        抛出 AgentConflict，免得一声不响地冲掉别人的修改。
        """
        path = self._file(name)
        text = self._checked(text)
        spec, problems = check(text, None)
        if problems:
            raise AgentInvalid(problems)
        new = spec.name
        with self._lock:
            current = os.lstat(path).st_mtime
            if not force and (modified is None or abs(current - modified) > 0.001):
                raise AgentConflict(t("这个 Agent 在你编辑的时候被改过了。你的修改还在框里：再点一次“保存”，就用你的版本覆盖。"))
            target = self._path(new)
            renamed = new != normal(str(name))
            if renamed and self._exists(new) and not _same_file(path, target):
                raise AgentError(t("已经有一个叫 {name} 的 Agent 了。", name=new))
            try:
                write_file(path, text)
                if renamed:
                    os.rename(path, target)
            except OSError as exc:
                raise AgentError(t("没能保存这个 Agent：{error}", error=exc.strerror or exc)) from exc
        return new

    def delete(self, name: object) -> None:
        path = self._file(name)
        with self._lock:
            try:
                path.unlink()
            except OSError as exc:
                raise AgentError(t("没能删除这个 Agent：{error}", error=exc.strerror or exc)) from exc

    def install(self, text: str, replace: bool = False) -> str:
        """放进一个从别处来的 Agent（从 Claude Code 导入的、内置的示例）。text 里的 name 已经是要用的名字。
        replace 为真时替换掉同名的；否则有同名的就不放。返回它的名字。"""
        spec, problems = check(self._checked(text), None)
        if problems:
            raise AgentInvalid(problems)
        with self._lock:
            if self._exists(spec.name) and not replace:
                raise AgentError(t("已经有一个叫 {name} 的 Agent 了。", name=spec.name))
            target = self._path(spec.name)
            if pathnames.is_link(target):
                target.unlink()  # 替换一个链接：只换掉链接本身，不顺着它去改别处的文件
            self._write(target, text)
        return spec.name

    def exists(self, name: str) -> bool:
        return self._exists(normal(name))

    # ---- 内部 --------------------------------------------------------------

    def _path(self, name: str) -> Path:
        return self.root / (name + SUFFIX)

    def _exists(self, name: str) -> bool:
        return os.path.lexists(self._path(name))

    def _file(self, name: object) -> Path:
        """把名字变成文件：只能是一段合格的名字，并且真的是这个文件夹里的一个普通文件（不是链接）。"""
        gone = AgentError(t("这个 Agent 不存在，可能已经被删除了。"))
        if not isinstance(name, str) or name_problem(normal(name)) is not None:
            raise gone
        path = self._path(normal(name))
        try:
            info = os.lstat(path)
        except OSError:
            raise gone from None
        if pathnames.linked(info):
            raise AgentError(t("这是一个指向别处的链接。为了安全，智狐不读链接；请用“导入”把它复制进来。"))
        if not stat.S_ISREG(info.st_mode):
            raise gone
        return path

    def _read_text(self, path: Path) -> tuple[str, os.stat_result]:
        try:
            info = os.lstat(path)
        except OSError:
            raise AgentError(t("这个 Agent 不存在，可能已经被删除了。")) from None
        if not stat.S_ISREG(info.st_mode):
            raise AgentError(t("这个 Agent 的文件不是一个普通的文件（可能是指向别处的链接）。"))
        if info.st_size > MAX_AGENT_BYTES:
            raise AgentError(t("这个 Agent 的文件太长了：最多 {n} KB。", n=MAX_AGENT_BYTES // 1000))
        try:
            data, _ = read_bytes(path, MAX_AGENT_BYTES)
        except SkillError as exc:
            raise AgentError(exc.args[0]) from None
        text = as_text(data, MAX_AGENT_BYTES)
        if text is None:
            raise AgentError(t("这个 Agent 的文件不是 UTF-8 编码的文字。"))
        return text.removeprefix("﻿"), info

    @staticmethod
    def _checked(text: object) -> str:
        if not isinstance(text, str):
            raise BadRequest(t("参数不正确"))
        text = clean_text(text)
        if len(text.encode()) > MAX_AGENT_BYTES:
            raise AgentError(t("这个 Agent 的文件太长了：最多 {n} KB。", n=MAX_AGENT_BYTES // 1000))
        return text

    def _write(self, path: Path, text: str) -> None:
        private_dir(self.root)
        try:
            write_file(path, text)
        except OSError as exc:
            raise AgentError(t("没能保存这个 Agent：{error}", error=exc.strerror or exc)) from exc


def _same_file(first: Path, second: Path) -> bool:
    """两个路径是不是同一个文件（不区分大小写的文件系统上，只改了大小写的新名字就是它自己）。"""
    try:
        return os.path.samestat(os.lstat(first), os.lstat(second))
    except OSError:
        return False
