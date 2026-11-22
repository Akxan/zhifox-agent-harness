"""智狐自己的 Skill 文件夹：data/skills/。每个 Skill 是其中的一个子文件夹，名字就是 Skill 的 name。

    data/skills/
      pdf/
        SKILL.md          说明（必须有）
        scripts/fill.py   别的文件随意：脚本、参考资料、模板……
      meeting-notes/
        SKILL.md
    data/skills.json      哪些停用了、每个是从哪儿装来的（不放进 Skill 的文件夹里，免得改动别人写的东西）

这里只管"文件夹里有什么"：列出来、读、改、删、启用和停用、把装进来的文件放进去。
从网址、压缩包、Claude Code 那里取得文件是 sources.py 的事；给模型看的列表是 catalog.py 的事。

页面送来的 Skill 名字和文件路径不能全信：可能被改成 "../../.env" 或者一个绝对路径，文件夹里也可能藏着
指向别处的符号链接。所以每次都从 Skill 文件夹出发，一层一层地走到目标，每一层都确认它真的在里面、
不是链接（和 memories.py 的 _locate 是同一套检查）。Windows 上的花招（a.md.、a.md:x、CON、短名字）不认，
目录联接（junction）和符号链接一样算链接（见 pathnames.py）。
"""

from __future__ import annotations

import builtins
import codecs
import json
import os
import secrets
import shutil
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .. import pathnames
from ..i18n import Message, t
from ..safe import clean, clean_text, private_dir, to_json, write_file
from .frontmatter import FrontmatterError, parse
from .spec import KNOWN, Meta, check, name_problem, new_skill, with_name

SKILL_FILE = "SKILL.md"
MAX_SKILLS = 200  # 列表里最多列这么多个
MAX_FILES = 1000  # 一个 Skill 里最多数这么多个文件
MAX_DEPTH = 12  # 最多往下找几层子文件夹
MAX_SKILL_BYTES = 512_000  # SKILL.md 最多读这么多字节。规范建议它不超过 500 行，正常的都远小于这个
MAX_VIEW_BYTES = 256_000  # 在页面上查看别的文件时，最多读这么多
MAX_PATH_CHARS = 1000


class SkillError(Exception):
    """这个 Skill 读不了、改不了或者装不了。错误信息可以直接显示给用户。"""


class BadRequest(SkillError):
    """请求本身不对（缺了参数、类型不对）。正常使用页面时不会出现，网页接口回答 400。"""


class SkillConflict(SkillError):
    """你在编辑的时候，SKILL.md 在磁盘上被改过了。"""


class SkillInvalid(SkillError):
    """SKILL.md 不符合规范。problems 是每一条问题。"""

    def __init__(self, problems: list[Message]):
        super().__init__(problems[0] if problems else t("SKILL.md 不符合规范。"))
        self.problems = problems


@dataclass
class Skill:
    """列表里的一个 Skill。"""

    name: str  # 文件夹的名字
    folder: Path
    meta: Meta = field(default_factory=Meta)
    problems: list[Message] = field(default_factory=list)  # 空的才算合格，才会告诉模型
    enabled: bool = True
    files: int = 0
    size: int = 0
    modified: float = 0.0  # SKILL.md 的修改时间
    source: dict | None = None

    @property
    def usable(self) -> bool:
        """会不会列给模型、模型能不能读它：启用着、并且合格。"""
        return self.enabled and not self.problems

    def summary(self) -> dict:
        """发给页面的那一份（列表里显示的东西，不含正文）。"""
        return {
            "name": self.name,
            "description": self.meta.description,
            "enabled": self.enabled,
            "problems": [described(problem) for problem in self.problems],
            "files": self.files,
            "size": self.size,
            "modified": round(self.modified, 3),
            "source": self.source,
            "manual": self.meta.manual_only,
        }


class SkillStore:
    def __init__(self, data_dir: Path):
        self.root = data_dir / "skills"
        self.state_file = data_dir / "skills.json"
        # 网页上几个请求可能同时改 skills.json（一边停用一个，一边装一个新的）："读出来、改一项、写回去"要一口气做完
        self._lock = threading.Lock()

    # ---- 列出 --------------------------------------------------------------

    def list(self) -> list[Skill]:
        """文件夹里的全部 Skill，按名字排好。以 . 开头的不列（装到一半的临时文件夹、访达生成的 .DS_Store）。"""
        state = self._state()
        found: list[Skill] = []
        try:
            with os.scandir(self.root) as listing:
                entries = sorted(listing, key=lambda entry: entry.name)
        except OSError:
            return []  # 还没有这个文件夹：一个都没有
        for entry in entries:
            if len(found) >= MAX_SKILLS:
                break
            if entry.name.startswith("."):
                continue
            name = clean_text(entry.name)
            skill = Skill(name=name, folder=Path(entry.path), enabled=name not in state["disabled"], source=state["sources"].get(name))
            if pathnames.entry_linked(entry):
                # 链接可以指到任何地方，跟着它走就出了这个文件夹。要用别处的 Skill，请用"导入"复制一份进来
                skill.problems.append(t("这是一个指向别处的链接。为了安全，智狐不读链接；请用“导入”把它复制进来。"))
            elif entry.is_dir(follow_symlinks=False):
                self._fill(skill)
            else:
                continue  # 文件夹里散放的文件（比如说明文字）不是 Skill
            found.append(skill)
        return found

    def get(self, name: object) -> Skill:
        """按名字找到一个 Skill。名字是页面或模型给的，先检查它只是一段名字，不是路径。"""
        folder = self._folder(name)
        state = self._state()
        skill = Skill(name=str(name), folder=folder, enabled=name not in state["disabled"], source=state["sources"].get(name))
        self._fill(skill)
        return skill

    # 这个类里有一个叫 list 的方法：在它后面写 list[...]，类型检查会以为说的是这个方法，所以写成 builtins.list
    def catalog(self) -> builtins.list[tuple[str, str]]:
        """要告诉模型的那些：启用着、合格、没有标成"只在用户点名时用"的。[(名字, 做什么和什么时候用)]"""
        return [(skill.name, skill.meta.listing()) for skill in self.list() if skill.usable and not skill.meta.manual_only]

    def _fill(self, skill: Skill) -> None:
        """读 SKILL.md、数文件，把结果填进 skill。"""
        files = walk(skill.folder)
        skill.files, skill.size = len(files), sum(size for _, size in files)
        try:
            text, info = read_skill_file(skill.folder)
        except SkillError as exc:
            skill.problems.append(exc.args[0])
            return
        skill.modified = info.st_mtime
        skill.meta, problems = check(text, skill.name)
        skill.problems += problems

    # ---- 查看 --------------------------------------------------------------

    def read(self, name: object) -> dict:
        """打开一个 Skill：SKILL.md 的全文、文件列表和读出来的各项信息。"""
        skill = self.get(name)
        try:
            text, info = read_skill_file(skill.folder)
        except SkillError:
            text, info = "", None
        meta = skill.meta
        return {
            **skill.summary(),
            "text": text,
            # 能不能在页面上改：读得出来的能改；根本没有 SKILL.md 的，也可以在页面上写一份。
            # 有、但读不出来（太大、不是 UTF-8、是链接）的不能改：照页面上显示的存回去，原来的内容就没了
            "editable": info is not None or not os.path.lexists(skill.folder / SKILL_FILE),
            "missing": not os.path.lexists(skill.folder / SKILL_FILE),
            "path": str(skill.folder),
            "list": [{"path": path, "size": size} for path, size in walk(skill.folder)],
            "license": meta.license,
            "compatibility": meta.compatibility,
            "metadata": meta.metadata,
            "allowed_tools": meta.allowed_tools,
            "extra": meta.extra,
        }

    def read_file(self, name: object, relative: object) -> dict:
        """在页面上查看 Skill 里的一个文件。太长的只读开头；不是文字的不显示内容。"""
        folder = self._folder(name)
        target = locate(folder, relative)
        data, size = read_bytes(target, MAX_VIEW_BYTES)
        text = as_text(data, MAX_VIEW_BYTES)
        return {"path": relative, "size": size, "text": text if text is not None else "", "binary": text is None, "truncated": size > MAX_VIEW_BYTES}

    def folder_of(self, name: object | None) -> Path:
        """在文件管理器里显示用：一个 Skill 的文件夹（检查过的），或者整个 Skill 文件夹。"""
        if name is None:
            private_dir(self.root)  # 还不存在的话先建一个空的，不然点了没反应
            return self.root
        return self._folder(name)

    # ---- 修改 --------------------------------------------------------------

    def create(self, name: object, description: object, body: object) -> str:
        """按表单新建一个 Skill。返回它的名字。"""
        if not isinstance(name, str) or not isinstance(description, str) or not isinstance(body, str):
            raise BadRequest(t("参数不正确"))
        name, description, body = clean_text(name).strip(), clean_text(description).strip(), clean_text(body)
        problem = name_problem(name)
        if problem:
            raise SkillInvalid([problem])
        text = new_skill(name, description, body)
        self.install({SKILL_FILE: (text.encode(), False)}, name, replace=False, source={"kind": "new"})
        return name

    def save(self, name: object, text: object, modified: float | None, force: bool = False) -> str:
        """改 SKILL.md。返回改完之后的名字：frontmatter 里的 name 改了，文件夹也跟着改名（规范要求两者一样）。

        modified 是你打开它时看到的修改时间。磁盘上的对不上，说明这期间它被别处改过了：不是 force 的话不保存，
        抛出 SkillConflict，免得一声不响地冲掉别人的修改。
        """
        folder = self._folder(name)
        if not isinstance(text, str):
            raise BadRequest(t("参数不正确"))
        text = clean_text(text)
        if len(text.encode()) > MAX_SKILL_BYTES:
            raise SkillError(t("SKILL.md 太长了：最多 {n} KB。较长的参考资料请拆到别的文件里，在 SKILL.md 里写明什么时候去读。", n=MAX_SKILL_BYTES // 1000))
        meta, problems = check(text, None)
        if problems:
            raise SkillInvalid(problems)
        new = meta.name
        target = self.root / new
        if new != name and os.path.lexists(target) and not _same_file(folder, target):
            raise SkillError(t("已经有一个叫 {name} 的 Skill 了。", name=new))
        skill_file = folder / SKILL_FILE
        try:
            current = os.lstat(skill_file).st_mtime
        except FileNotFoundError:
            current = None  # 原来没有 SKILL.md（不合格的 Skill）：现在补上
        if not force and current is not None and (modified is None or abs(current - modified) > 0.001):
            raise SkillConflict(t("SKILL.md 在你编辑的时候被改过了。你的修改还在框里：再点一次“保存”，就用你的版本覆盖。"))
        if current is not None and pathnames.is_link(skill_file):
            raise SkillError(t("SKILL.md 是一个指向别处的链接，为了安全，不在这里修改。"))
        try:
            write_file(skill_file, text)
            if new != name:
                os.rename(folder, target)
        except OSError as exc:
            raise SkillError(t("没能保存这个 Skill：{error}", error=exc.strerror or exc)) from exc
        if new != name:
            self._rename_state(str(name), new)
        return new

    def delete(self, name: object) -> None:
        """删掉一个 Skill（整个文件夹）。"""
        folder = self._folder(name)
        # 先改成一个隐藏的名字，再慢慢删：删到一半出错时，列表里不会留下一个缺胳膊少腿的 Skill
        trash = self.root / f".deleting-{secrets.token_hex(4)}"
        try:
            os.rename(folder, trash)
        except OSError as exc:
            raise SkillError(t("没能删除这个 Skill：{error}", error=exc.strerror or exc)) from exc
        shutil.rmtree(trash, ignore_errors=True)  # rmtree 不跟着链接走：文件夹里的链接只删掉链接本身
        self._forget(str(name))

    def set_enabled(self, name: object, enabled: bool) -> None:
        self._folder(name)  # 确认有这个 Skill
        with self._lock:
            state = self._state()
            disabled = set(state["disabled"])
            (disabled.discard if enabled else disabled.add)(str(name))
            state["disabled"] = sorted(disabled)
            self._write_state(state)

    def install(self, files: dict[str, tuple[bytes, bool]], name: str, replace: bool, source: dict) -> str:
        """把一组文件装成名叫 name 的 Skill。files 是 {相对路径: (内容, 是否可执行)}，路径都已经检查过（见 sources.py）。

        SKILL.md 里的 name 和 name 不一样时（装的时候改了名，或者原来就没写），替它改成一样的。
        replace 为真时替换掉已有的同名 Skill；否则有同名的就不装。
        先把文件全写进一个隐藏的临时文件夹，最后一步改名换上去：中途出错不会留下半个 Skill。
        """
        problem = name_problem(name)
        if problem:
            raise SkillInvalid([problem])
        raw = files.get(SKILL_FILE)
        if raw is None:
            raise SkillError(t("文件夹里没有 SKILL.md。"))
        try:
            text = raw[0].decode("utf-8")
        except UnicodeDecodeError:
            raise SkillError(t("SKILL.md 不是 UTF-8 编码的文字。")) from None
        try:
            front = parse(text, KNOWN)
        except FrontmatterError as exc:
            raise SkillInvalid([exc.args[0]]) from None
        if front.fields.get("name") != name:
            text = with_name(text, name)
            files = {**files, SKILL_FILE: (text.encode(), raw[1])}
        _, problems = check(text, name)
        if problems:
            raise SkillInvalid(problems)
        target = self.root / name
        if os.path.lexists(target) and not replace:
            raise SkillError(t("已经有一个叫 {name} 的 Skill 了。", name=name))

        private_dir(self.root)
        staging = self.root / f".installing-{secrets.token_hex(4)}"
        try:
            for relative, (data, executable) in files.items():
                path = staging.joinpath(*PurePosixPath(relative).parts)
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                # O_EXCL：只写新文件，万一路径里混进了链接也不会写到别处去
                descriptor = pathnames.open_nofollow(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700 if executable else 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
            old = None
            if os.path.lexists(target):
                old = self.root / f".replacing-{secrets.token_hex(4)}"
                os.rename(target, old)
            os.rename(staging, target)
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise SkillError(t("没能安装这个 Skill：{error}", error=exc.strerror or exc)) from exc
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
        with self._lock:
            state = self._state()
            state["sources"][name] = {**source, "at": round(time.time())}
            state["disabled"] = [item for item in state["disabled"] if item != name]  # 新装的默认启用
            self._write_state(state)
        return name

    def exists(self, name: str) -> bool:
        return os.path.lexists(self.root / name)

    # ---- 内部 --------------------------------------------------------------

    def _folder(self, name: object) -> Path:
        """把名字变成文件夹：只能是一段名字（不带 /，不以 . 开头），并且真的是这里面的一个文件夹（不是链接）。"""
        gone = SkillError(t("这个 Skill 不存在，可能已经被删除了。"))
        if not isinstance(name, str) or not 0 < len(name) <= 255 or name.startswith(".") or any(char in name for char in "/\\\0"):
            raise gone
        if pathnames.trick(name):  # Windows 上同一个文件夹的别的写法（pdf.、pdf::$INDEX_ALLOCATION）、设备名
            raise gone
        folder = self.root / name
        try:
            info = os.lstat(folder)
        except OSError:
            raise gone from None
        if pathnames.linked(info):
            raise SkillError(t("这是一个指向别处的链接。为了安全，智狐不读链接；请用“导入”把它复制进来。"))
        if not stat.S_ISDIR(info.st_mode):
            raise gone
        return folder

    def _state(self) -> dict:
        """skills.json：{"disabled": [停用的名字], "sources": {名字: {"kind": 从哪儿来的, ...}}}。坏了就当作空的。"""
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        disabled = data.get("disabled")
        sources = data.get("sources")
        return {
            "disabled": [item for item in disabled if isinstance(item, str)] if isinstance(disabled, list) else [],
            "sources": {key: clean(value) for key, value in sources.items() if isinstance(value, dict)} if isinstance(sources, dict) else {},
        }

    def _write_state(self, state: dict) -> None:
        private_dir(self.state_file.parent)
        write_file(self.state_file, to_json(state, readable=True, indent=2))

    def _rename_state(self, old: str, new: str) -> None:
        with self._lock:
            state = self._state()
            state["disabled"] = [new if item == old else item for item in state["disabled"]]
            if old in state["sources"]:
                state["sources"][new] = state["sources"].pop(old)
            self._write_state(state)

    def _forget(self, name: str) -> None:
        with self._lock:
            state = self._state()
            state["disabled"] = [item for item in state["disabled"] if item != name]
            state["sources"].pop(name, None)
            self._write_state(state)


def described(problem: object) -> dict:
    """一条问题发给页面的样子：原文和填进去的值。页面换语言时照着它们在原地重新翻译（见 i18n.py 的 Message）。"""
    if isinstance(problem, Message):
        return {"key": problem.key, "values": problem.values}
    return {"key": str(problem), "values": {}}


# ---- 文件夹里的文件 --------------------------------------------------------


def walk(folder: Path) -> list[tuple[str, int]]:
    """一个 Skill 里的全部文件：[(相对路径, 字节数)]，SKILL.md 排第一，其余按路径排。

    不跟着链接走，不列以 . 开头的（.DS_Store、.git 之类），最多 MAX_FILES 个、MAX_DEPTH 层。
    """
    found: list[tuple[str, int]] = []

    def visit(path: Path, prefix: str, depth: int) -> None:
        try:
            with os.scandir(path) as listing:
                entries = sorted(listing, key=lambda entry: entry.name)
        except OSError:
            return
        for entry in entries:
            if len(found) >= MAX_FILES:
                return
            if entry.name.startswith(".") or pathnames.entry_linked(entry):
                continue
            relative = prefix + clean_text(entry.name)
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth < MAX_DEPTH:
                        visit(Path(entry.path), relative + "/", depth + 1)
                elif entry.is_file(follow_symlinks=False):
                    found.append((relative, entry.stat(follow_symlinks=False).st_size))
            except OSError:
                continue

    visit(folder, "", 1)
    found.sort(key=lambda item: (item[0] != SKILL_FILE, item[0]))
    return found


def read_skill_file(folder: Path) -> tuple[str, os.stat_result]:
    """读出一个 Skill 的 SKILL.md。返回（全文，文件信息）。"""
    path = folder / SKILL_FILE
    try:
        info = os.lstat(path)
    except OSError:
        raise SkillError(t("文件夹里没有 SKILL.md。")) from None
    if not stat.S_ISREG(info.st_mode):
        raise SkillError(t("SKILL.md 不是一个普通的文件（可能是指向别处的链接）。"))
    if info.st_size > MAX_SKILL_BYTES:
        raise SkillError(t("SKILL.md 太长了：最多 {n} KB。较长的参考资料请拆到别的文件里，在 SKILL.md 里写明什么时候去读。", n=MAX_SKILL_BYTES // 1000))
    data, _ = read_bytes(path, MAX_SKILL_BYTES)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise SkillError(t("SKILL.md 不是 UTF-8 编码的文字。")) from None
    return clean_text(text.removeprefix("﻿")), info


def body_of(text: str) -> str:
    """SKILL.md 去掉 frontmatter 之后的正文。读不出 frontmatter 时返回全文。"""
    try:
        return parse(text).body
    except FrontmatterError:
        return text


def locate(folder: Path, relative: object) -> Path:
    """把相对路径（比如 "scripts/fill.py"）变成 Skill 文件夹里的一个普通文件。不是的话抛出 SkillError。"""
    gone = SkillError(t("这个文件不存在，可能已经被删除了。"))
    if not isinstance(relative, str) or not 0 < len(relative) <= MAX_PATH_CHARS or "\x00" in relative or "\\" in relative:
        raise gone
    path = PurePosixPath(relative)
    # 只认"几段名字用 / 连起来"的写法。绝对路径、..、.、多余的斜杠、隐藏的名字都不认
    if path.is_absolute() or str(path) != relative or not path.parts or any(part.startswith(".") for part in path.parts):
        raise gone
    if pathnames.trick(relative):  # Windows 上同一个文件的别的写法（SKILL.md.、a.md:x、短名字）、设备名（CON）
        raise gone
    base = folder.resolve()
    target = base
    info = None
    for part in path.parts:
        target = target / part
        try:
            info = os.lstat(target)
        except OSError:
            raise gone from None
        if pathnames.linked(info):  # 每一层都不许是链接（包括 Windows 的目录联接）：链接可以指到文件夹外面的任何地方
            raise SkillError(t("这一项是指向别处的链接，为了安全，不在这里打开。"))
    if info is None or not stat.S_ISREG(info.st_mode):
        raise SkillError(t("这不是一个文件。"))
    if not target.resolve().is_relative_to(base):  # 上面已经保证了；再核对一次，万一哪天改坏了
        raise gone
    return target


def read_bytes(path: Path, limit: int) -> tuple[bytes, int]:
    """读一个文件的前 limit+1 个字节。返回（读到的，文件的实际大小）。"""
    try:
        # 不跟着链接走：检查过之后、打开之前，万一它被换成了链接，打开会失败，而不是跟着链接读到别处去
        descriptor = pathnames.open_nofollow(path, os.O_RDONLY)
        with os.fdopen(descriptor, "rb") as handle:
            data = handle.read(limit + 1)
            size = os.fstat(handle.fileno()).st_size
    except FileNotFoundError:
        raise SkillError(t("这个文件不存在，可能已经被删除了。")) from None
    except OSError as exc:
        raise SkillError(t("没能读出这个文件：{error}", error=exc.strerror or exc)) from exc
    return data, size


def as_text(data: bytes, limit: int) -> str | None:
    """字节当文字读，最多读 limit 个字节（read_bytes 多读了一个字节，用来知道后面还有没有）。
    不是文字（有 NUL 字节、或者不是 UTF-8）返回 None。"""
    truncated = len(data) > limit
    data = data[:limit]
    if b"\x00" in data:
        return None
    # 截断处可能正好切在一个汉字中间：final=False 让解码器把那半个字留着不解，而不是报错
    try:
        return clean_text(codecs.getincrementaldecoder("utf-8")("strict").decode(data, final=not truncated))
    except UnicodeDecodeError:
        return None


def _same_file(first: Path, second: Path) -> bool:
    try:
        return os.path.samestat(os.lstat(first), os.lstat(second))
    except OSError:
        return False
