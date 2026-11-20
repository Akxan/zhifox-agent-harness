"""记忆文件夹给"人"开的那扇门：网页界面"记忆"一栏背后的部分。

助手通过 memory 工具读写 data/memories/（见 tools/memory.py），那是给模型用的门。这里是另一扇：
列出文件夹里有哪些文件、读出一个文件、改一个文件（内容和名字）、删掉一个文件，让你看得到助手记住了什么，
记错了的可以改，不想让它记着的可以删掉。
这里做的事模型看不到，它下次查看记忆时只会发现那个文件不在了。

浏览器送来的是列表里给它的相对路径（比如 "projects/zhifox.md"）。这个路径是从网页上来的，不能全信：
它可能被改成 "../../.env"、一个绝对路径，或者文件夹里的某一层其实是指向别处的符号链接。所以每一次都从
记忆文件夹出发，一层一层地走到目标，确认它还在文件夹里、是一个普通文件，才去读或删（见 _locate）。
"""

from __future__ import annotations

import codecs
import os
import stat
from pathlib import Path, PurePosixPath

from .i18n import t
from .safe import clean_text, write_file

MAX_LISTED = 500  # 列表里最多列这么多个。每调用一次模型就发一次状态，文件夹再大也不能每次都扫一大片
MAX_DEPTH = 8  # 最多往下找几层子文件夹
MAX_READ_BYTES = 256_000  # 点开看的时候最多读这么多字节，更长的只显示开头
MAX_PATH_CHARS = 1000
MAX_NAME_CHARS = 200  # 改名时文件名最长多少个字符


class MemoryFileError(Exception):
    """这条记忆读不了、改不了或删不了。错误信息可以直接显示给用户。"""


class MemoryConflict(MemoryFileError):
    """你在编辑的时候，这条记忆在磁盘上被改过了（多半是助手改的）。"""


def list_files(root: Path) -> list[dict]:
    """记忆文件夹里的全部文件：[{"path": "projects/zhifox.md", "size": 字节数, "modified": 秒}]，按路径排好。

    以 . 开头的不列：在访达里打开过文件夹后它自己生成的 .DS_Store，记忆工具改文件时临时写的 .tmp-……，
    模型查看记忆时也看不到它们。
    符号链接也不列：它可能指向文件夹外面，这一栏只管文件夹里真正的文件。文件夹还不存在，就是空的。
    """
    found: list[dict] = []

    def walk(folder: Path, prefix: str, depth: int) -> None:
        try:
            with os.scandir(folder) as listing:
                entries = sorted(listing, key=lambda entry: entry.name.casefold())
        except OSError:
            return  # 不存在、不是文件夹、没有权限：当作空的
        for entry in entries:
            if len(found) >= MAX_LISTED:
                return
            if entry.name.startswith(".") or entry.is_symlink():
                continue
            relative = prefix + entry.name
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth < MAX_DEPTH:
                        walk(Path(entry.path), relative + "/", depth + 1)
                elif entry.is_file(follow_symlinks=False):
                    info = entry.stat(follow_symlinks=False)
                    # clean_text：文件名不是合法的 UTF-8 时，里面会有落单的代理字符，发给浏览器之前换掉（见 safe.py）
                    found.append({"path": clean_text(relative), "size": info.st_size, "modified": round(info.st_mtime, 3)})
            except OSError:
                continue  # 正好在这时被删掉了
    walk(root, "", 1)
    return found


def read(root: Path, relative: object) -> dict:
    """读出一条记忆。返回 {"path", "text", "size", "modified", "truncated", "editable"}；太长的只读开头，truncated 为真。

    editable：能不能在网页上改。只读了开头的不能改（保存时后面那部分就没了）；不是 UTF-8 的也不能改
    （显示的时候认不出的字节换成了"�"，原样存回去就把它们弄坏了）。
    """
    target = _locate(root, relative)
    try:
        # O_NOFOLLOW：_locate 检查过之后、打开之前，万一这个文件被换成了链接，打开会失败，而不是跟着链接读到别处去
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            data = handle.read(MAX_READ_BYTES + 1)
            info = os.fstat(handle.fileno())
    except FileNotFoundError:
        raise MemoryFileError(t("这条记忆不存在，可能已经被删除了。")) from None
    except OSError as exc:
        raise MemoryFileError(t("没能读出这条记忆：{error}", error=exc.strerror or exc)) from exc
    truncated = len(data) > MAX_READ_BYTES
    data = data[:MAX_READ_BYTES]
    if b"\x00" in data:
        raise MemoryFileError(t("这不是文本文件，没法在这里显示。可以打开记忆文件夹查看。"))
    # 截断的地方可能正好切在一个汉字的中间：final=False 让解码器把那半个字留着不解，而不是变成一个"�"
    text = codecs.getincrementaldecoder("utf-8")("replace").decode(data, final=not truncated)
    try:
        exact = not truncated and data.decode("utf-8") == clean_text(text)
    except UnicodeDecodeError:
        exact = False
    return {
        "path": relative,
        "text": clean_text(text),
        "size": info.st_size,
        "modified": round(info.st_mtime, 3),
        "truncated": truncated,
        "editable": exact,
    }


def save(root: Path, relative: object, name: object, text: object, modified: float | None, force: bool = False) -> str:
    """改一条记忆：把内容换成 text，名字换成 name（只改名字，不挪到别的子文件夹）。返回改完之后的相对路径。

    modified 是你打开它时看到的修改时间。磁盘上的对不上，说明这期间它被改过了：不是 force 的话不保存，
    抛出 MemoryConflict，免得一声不响地把助手刚记下的东西冲掉。
    先写内容、再改名：内容是"先写临时文件再一步换上去"（见 safe.write_file），中途出错原来的文件原封不动；
    改名是一步完成的系统调用。
    """
    target = _locate(root, relative)
    name = _valid_name(name)
    if not isinstance(text, str):
        raise MemoryFileError(t("参数不正确"))
    text = clean_text(text)
    if len(text.encode("utf-8")) > MAX_READ_BYTES:
        raise MemoryFileError(t("内容太长了：在这里最多保存 {n} KB，更长的请在记忆文件夹里编辑。", n=MAX_READ_BYTES // 1000))
    destination = target.with_name(name)
    if name != target.name and os.path.lexists(destination) and not _same_file(target, destination):
        raise MemoryFileError(t("这个文件夹里已经有一个叫 {name} 的文件了。", name=name))
    if not force and (modified is None or abs(os.lstat(target).st_mtime - modified) > 0.001):
        raise MemoryConflict(t("这条记忆在你编辑的时候被改过了（可能是助手改的）。你的修改还在框里：再点一次“保存”，就用你的版本覆盖。"))
    try:
        write_file(target, text)
        if name != target.name:
            os.rename(target, destination)  # 只差大小写的改名（a.md → A.md）也走这里：不区分大小写的磁盘上它们是同一个文件
    except OSError as exc:
        raise MemoryFileError(t("没能保存这条记忆：{error}", error=exc.strerror or exc)) from exc
    return str(PurePosixPath(relative).with_name(name))


def file_of(root: Path, relative: object) -> Path:
    """一条记忆在磁盘上的位置（检查过的，出不了记忆文件夹）。给"在访达中显示"用。"""
    return _locate(root, relative)


def _valid_name(name: object) -> str:
    """改名时的新名字：一段名字，不带路径，不以 . 开头（以 . 开头的文件列表里不显示，改了就"消失"了）。"""
    if not isinstance(name, str):
        raise MemoryFileError(t("参数不正确"))
    name = clean_text(name).strip()
    if not name:
        raise MemoryFileError(t("文件名不能为空。"))
    if len(name) > MAX_NAME_CHARS:
        raise MemoryFileError(t("文件名太长了，最多 {n} 个字符。", n=MAX_NAME_CHARS))
    if "/" in name or "\\" in name or name.startswith(".") or any(ord(char) < 32 for char in name):
        raise MemoryFileError(t("文件名里不能有 / 或 \\，也不能以 . 开头。"))
    return name


def _same_file(first: Path, second: Path) -> bool:
    try:
        return os.path.samestat(os.lstat(first), os.lstat(second))
    except OSError:
        return False


def delete(root: Path, relative: object) -> None:
    """删掉一条记忆。它所在的子文件夹因此空了的话，也一起删掉（记忆文件夹本身留着）。"""
    target = _locate(root, relative)
    try:
        target.unlink()  # 删的是这个名字本身：就算它刚刚被换成了链接，删掉的也只是链接，不会是链接指向的文件
    except FileNotFoundError:
        raise MemoryFileError(t("这条记忆不存在，可能已经被删除了。")) from None
    except OSError as exc:
        raise MemoryFileError(t("没能删除这条记忆：{error}", error=exc.strerror or exc)) from exc
    # 模型建的子文件夹（比如 projects/）里最后一个文件删掉之后，空文件夹留着没有用，模型查看记忆时还会看到它。
    # rmdir 只删得掉空文件夹：里面还有别的东西（哪怕只是一个 .DS_Store）就留着，不替你删你没让删的东西
    base, folder = root.resolve(), target.parent
    while folder != base:
        try:
            folder.rmdir()
        except OSError:
            break
        folder = folder.parent


def _locate(root: Path, relative: object) -> Path:
    """把浏览器送来的相对路径变成磁盘上的文件。不是记忆文件夹里的一个普通文件，就抛出 MemoryFileError。"""
    gone = MemoryFileError(t("这条记忆不存在，可能已经被删除了。"))
    if not isinstance(relative, str) or not 0 < len(relative) <= MAX_PATH_CHARS or "\x00" in relative or "\\" in relative:
        raise gone
    path = PurePosixPath(relative)
    # 只认列表里给出的那种写法：几段名字用 / 连起来。绝对路径、..、.、多余的斜杠、隐藏的名字（列表里本来就没有）都不认。
    # PurePosixPath 会把 "a//b"、"a/./b"、"a/" 规整成别的样子，和原文对不上，正好借它认出这些写法
    # （"." 规整之后一段都不剩，所以还要单独查一下是不是空的）
    if path.is_absolute() or str(path) != relative or not path.parts or any(part.startswith(".") for part in path.parts):
        raise gone
    # 从记忆文件夹真正的位置出发（它本身可以是你自己建的链接，比如挪到了同步盘里），一层一层往下走
    base = root.resolve()
    target = base
    for part in path.parts:
        target = target / part
        try:
            info = os.lstat(target)  # lstat：看的是这个名字本身，不跟着链接走
        except OSError:
            raise gone from None
        # 每一层都不许是符号链接：链接可以指到文件夹外面的任何地方，跟着它走就出去了
        if stat.S_ISLNK(info.st_mode):
            raise MemoryFileError(t("这一项是指向别处的链接，为了安全，不在这里打开或删除。"))
    if not stat.S_ISREG(info.st_mode):
        raise MemoryFileError(t("这不是一个文件。文件夹请在文件管理器里处理。"))
    if not target.resolve().is_relative_to(base):  # 上面已经保证了；再核对一次，万一哪天改坏了
        raise gone
    return target
