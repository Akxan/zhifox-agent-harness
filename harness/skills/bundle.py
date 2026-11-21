"""从外面拿来的一组文件：压缩包、页面上传的文件夹、网上下载的东西。装进 Skill 文件夹之前，先在这里过一遍。

这些文件一律当作不可信的：压缩包可以是故意做坏的。常见的几种坏法，这里都拦下：

  路径穿越（zip slip）   文件名写成 ../../.bashrc 或 /etc/hosts，解压时写到文件夹外面去
  符号链接               先放一个指向 ~/.ssh 的链接，再往"链接里面"写文件
  设备文件、管道         解压出来的不是文件，读写它会卡住或者碰到硬件
  压缩炸弹               几 KB 的压缩包解开有几个 GB，把磁盘塞满
  文件太多               几十万个小文件，光是建出来就要很久
  大小写撞名             A.txt 和 a.txt 在 macOS 默认的磁盘上是同一个文件，后一个会悄悄盖掉前一个
  Windows 的怪名字        a.md:x、SKILL.md.、CON、PROGRA~1……在 Windows 上解开就成了别的东西（见 clean_path）

整理好之后是一个普通的字典：{"相对路径": (内容, 是不是可执行文件)}。内容都已经读进内存（总共有上限），
之后真正写进磁盘的是 store.py 的 install，它只写新文件、不跟着链接走。
"""

from __future__ import annotations

import base64
import binascii
import io
import re
import stat
import unicodedata
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from ..i18n import Message, t
from ..pathnames import name_trick
from .frontmatter import FrontmatterError, parse
from .spec import KNOWN, MAX_NAME, NAME, Meta, check, with_name
from .store import MAX_DEPTH, SKILL_FILE, BadRequest, SkillError, SkillStore, described

Files = dict[str, tuple[bytes, bool]]

MAX_FILES = 1000  # 一次最多装这么多个文件
MAX_TOTAL_BYTES = 50_000_000  # 解开之后一共最多这么大
MAX_ENTRIES = 20_000  # 压缩包里最多有这么多项（包括用不上的）。再多就不看了，免得光是列目录就花很久
MAX_CANDIDATES = 100  # 一个仓库里最多认出这么多个 Skill
# 别的系统顺手塞进压缩包里的东西：macOS 的 __MACOSX/ 和 .DS_Store，Windows 的 Thumbs.db，git 的 .git/。直接跳过
_JUNK = re.compile(r"(^|/)(__MACOSX|\.git)(/|$)|(^|/)(\.DS_Store|Thumbs\.db|desktop\.ini)$")


def clean_path(name: object) -> str:
    """检查一个相对路径能不能放进 Skill 文件夹。能的话原样返回，不能的话抛出 SkillError。"""
    bad = SkillError(t("里面有不安全的文件路径：{path}。为了安全，什么都没有安装。", path=str(name)[:200]))
    if not isinstance(name, str) or not 0 < len(name) <= 1000:
        raise bad
    if "\\" in name or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise bad  # 反斜杠在 Windows 上是路径的分隔符；控制字符不该出现在文件名里
    if name.startswith("/") or re.match(r"[A-Za-z]:", name):
        raise bad  # 绝对路径（包括 C: 这样的盘符）
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts) or len(parts) > MAX_DEPTH + 1:
        raise bad
    if any(len(part.encode("utf-8", "surrogatepass")) > 255 for part in parts):
        raise bad
    # 在 Windows 上解开会变成别的东西的名字：a.md:x（数据流）、SKILL.md.（尾巴上的点会被去掉，和 SKILL.md 撞名）、
    # CON、nul.txt（设备）、PROGRA~1（短名字，可能是另一个文件的别名）、* ? < > " |。不管现在是什么系统都不收：
    # 同一个压缩包、同一个 Skill 文件夹，换到 Windows 上也得是安全的（见 pathnames.name_trick）
    if any(name_trick(part) for part in parts):
        raise bad
    return name


def _add(files: Files, seen: set[str], path: str, data: bytes, executable: bool) -> None:
    """往结果里加一个文件，顺便查重和数总量。"""
    key = unicodedata.normalize("NFC", path).casefold()
    if key in seen:
        raise SkillError(t("里面有两个只差大小写（或写法）的同名文件：{path}。在 macOS 上它们会互相覆盖，所以没有安装。", path=path))
    seen.add(key)
    files[path] = (data, executable)
    if len(files) > MAX_FILES:
        raise SkillError(t("文件太多了：最多 {n} 个。", n=MAX_FILES))
    if sum(len(item[0]) for item in files.values()) > MAX_TOTAL_BYTES:
        raise SkillError(t("内容太大了：解开之后最多 {n} MB。", n=MAX_TOTAL_BYTES // 1_000_000))


def from_zip(data: bytes, scope: str = "", strip_top: bool = False) -> tuple[Files, str]:
    """读一个 zip 压缩包（.skill 文件也是 zip）。返回（文件，压缩包的注释）。

    scope 不为空时只取这个子文件夹里的（路径相对于它）；strip_top 为真时先去掉最外面那一层文件夹
    （GitHub 打包的压缩包里，所有东西都在"仓库名-分支名/"下面）。范围之外的项完全不看，
    这样从一个大仓库里装其中一个 Skill 时，仓库别处的东西不会碍事。
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        entries = archive.infolist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, EOFError):
        raise SkillError(t("这不是一个能打开的 zip 压缩包。")) from None
    if len(entries) > MAX_ENTRIES:
        raise SkillError(t("压缩包里的东西太多了：最多 {n} 项。", n=MAX_ENTRIES))
    prefix = scope.strip("/") + "/" if scope.strip("/") else ""
    files: Files = {}
    seen: set[str] = set()
    for entry in entries:
        name = entry.filename
        if strip_top:
            name = name.partition("/")[2]
        if not name.startswith(prefix):
            continue
        name = name[len(prefix) :]
        if not name or _JUNK.search(name):
            continue
        mode = entry.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if entry.is_dir() or kind == stat.S_IFDIR:
            clean_path(name.rstrip("/"))  # 文件夹本身不用建，但名字同样要检查
            continue
        if kind == stat.S_IFLNK:
            raise SkillError(t("压缩包里有符号链接（{path}）。链接可能指向你电脑上别的地方，为了安全，什么都没有安装。", path=name[:200]))
        if kind not in (0, stat.S_IFREG):  # 0：在 Windows 上打的包，不带文件类型，按普通文件算
            raise SkillError(t("压缩包里有不是普通文件的东西（{path}），比如设备文件或管道。为了安全，什么都没有安装。", path=name[:200]))
        if entry.flag_bits & 0x1:
            raise SkillError(t("压缩包是加密的，没法打开。"))
        clean_path(name)
        if entry.file_size > MAX_TOTAL_BYTES:
            raise SkillError(t("内容太大了：解开之后最多 {n} MB。", n=MAX_TOTAL_BYTES // 1_000_000))
        try:
            with archive.open(entry) as handle:
                # 压缩包里写的大小可能是假的：最多读到"写的大小 + 1"，多出来就说明它在撒谎
                content = handle.read(entry.file_size + 1)
        except (zipfile.BadZipFile, OSError, ValueError, EOFError, NotImplementedError, RuntimeError):
            raise SkillError(t("压缩包已经损坏，读不出 {path}。", path=name[:200])) from None
        if len(content) != entry.file_size:
            raise SkillError(t("压缩包已经损坏，读不出 {path}。", path=name[:200]))
        _add(files, seen, name, content, bool(mode & 0o111))
    comment = archive.comment.decode("ascii", "replace") if archive.comment else ""
    return files, comment


def from_upload(items: object) -> Files:
    """页面上传的一个文件夹：[{"path": "pdf/SKILL.md", "data": "base64……"}]。"""
    if not isinstance(items, list) or not items:
        raise BadRequest(t("没有收到文件。"))
    if len(items) > MAX_FILES:
        raise SkillError(t("文件太多了：最多 {n} 个。", n=MAX_FILES))
    files: Files = {}
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("data"), str):
            raise BadRequest(t("参数不正确"))
        path = item.get("path")
        if isinstance(path, str) and _JUNK.search(path):
            continue
        clean_path(path)
        try:
            content = base64.b64decode(item["data"], validate=True)
        except (binascii.Error, ValueError):
            raise SkillError(t("文件 {path} 的内容没能读出来。", path=str(path)[:200])) from None
        _add(files, seen, path, content, False)  # 浏览器不告诉我们文件能不能执行
    return files


def decode(data: object) -> bytes:
    """页面送来的一个 base64 编码的文件。"""
    if not isinstance(data, str):
        raise BadRequest(t("参数不正确"))
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise SkillError(t("文件的内容没能读出来。")) from None


# ---- 认出里面的 Skill ------------------------------------------------------


@dataclass
class Candidate:
    """一组文件里认出来的一个 Skill，还没有装。"""

    key: str  # 它在这组文件里的位置（SKILL.md 所在的文件夹，"" 表示最外层）
    name: str  # 准备用的名字：原来的 name 合格就用它，否则由文件夹名或原来的名字改写一个合格的
    original: str  # SKILL.md 里原来写的 name（可能是空的，也可能不合格）
    meta: Meta = field(default_factory=Meta)
    problems: list[Message] = field(default_factory=list)  # 按 name 装进来之后还剩的问题：有的话装不了
    listing: list[tuple[str, int]] = field(default_factory=list)  # [(相对路径, 字节数)]
    load: Callable[[], Files] = dict  # 取出全部文件。压缩包的已经在内存里；本机文件夹的到安装时才读
    tool: str = ""  # 从别的工具导入的：那个工具的名字（产品名）和编号（见 imports/registry.py）
    tool_id: str = ""

    def preview(self, store: SkillStore) -> dict:
        """给页面确认用的那一份。"""
        return {
            "key": self.key,
            "name": self.name,
            "original": self.original,
            "description": self.meta.description,
            "license": self.meta.license,
            "compatibility": self.meta.compatibility,
            "problems": [described(problem) for problem in self.problems],
            "files": [{"path": path, "size": size} for path, size in self.listing[:200]],
            "count": len(self.listing),
            "size": sum(size for _, size in self.listing),
            "exists": store.exists(self.name),
            "tool": self.tool,
            "tool_id": self.tool_id,
        }


def slug(text: str) -> str:
    """把随便一段文字改写成合格的 Skill 名字："My PDF Tool" → "my-pdf-tool"。一个能用的字都没有时用 "skill"。"""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:MAX_NAME].strip("-")
    return text or "skill"


def examine(text: str, folder: str) -> tuple[str, str, Meta, list[Message]]:
    """看一份要装进来的 SKILL.md：返回（准备用的名字，原来的名字，信息，问题）。

    规范要求 name 和文件夹同名。装进来的时候文件夹由我们来建，所以只要 name 本身合格就行；
    name 不合格或者没写（Claude Code 允许不写，默认用文件夹名），就改写一个合格的，装的时候写进 SKILL.md。
    """
    try:
        front = parse(text, KNOWN)
    except FrontmatterError as exc:
        return slug(folder), "", Meta(), [exc.args[0]]
    original = front.fields.get("name")
    original = original if isinstance(original, str) else ""
    name = original if NAME.fullmatch(original) and len(original) <= MAX_NAME else slug(folder or original)
    meta, problems = check(with_name(text, name) if original != name else text, name)
    meta.name = name
    return name, original, meta, problems


def candidates(files: Files) -> list[Candidate]:
    """一组文件里有哪几个 Skill：每个 SKILL.md 所在的文件夹算一个。按"浅的在前、再按路径"排好。"""
    roots = sorted(
        {str(PurePosixPath(path).parent) if "/" in path else "" for path in files if PurePosixPath(path).name == SKILL_FILE},
        key=lambda root: (root.count("/") + bool(root), root),
    )
    found = []
    for root in roots[:MAX_CANDIDATES]:
        prefix = root + "/" if root else ""
        own = {path[len(prefix) :]: value for path, value in files.items() if path.startswith(prefix)}
        try:
            text = own[SKILL_FILE][0].decode("utf-8").removeprefix("﻿")
        except UnicodeDecodeError:
            text = None
        folder = PurePosixPath(root).name
        if text is None:
            name, original, meta, problems = slug(folder), "", Meta(), [t("SKILL.md 不是 UTF-8 编码的文字。")]
        else:
            name, original, meta, problems = examine(text, folder)
        listing = sorted(((path, len(value[0])) for path, value in own.items()), key=lambda item: (item[0] != SKILL_FILE, item[0]))
        found.append(Candidate(key=root, name=name, original=original, meta=meta, problems=problems, listing=listing, load=lambda own=own: own))
    return found
