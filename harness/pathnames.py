r"""一个路径真正指向哪里：同一个文件的几种写法、Windows 上的花招、链接。

安全检查比较的都是路径：文件工具不碰 .env，记忆的路径出不了记忆文件夹，只读的命令不碰数据目录……
可是"写出来的路径"和"真正打开的文件"不一定是一回事：

  大小写            macOS 和 Windows 默认不分大小写：.ENV 就是 .env
  Unicode 的写法    macOS 不分 é 的两种写法（一个字符，或者 e 加一个音调符号）
  以下只在 Windows 上：
  尾巴上的点和空格   .env.、.env 、.env... 打开的都是 .env（Windows 打开文件之前会把它们去掉）
  数据流            .env::$DATA、file.txt:secret 是这个文件里的一条"数据流"：读的还是这个文件
  短名字（8.3）      PROGRA~1 是 Program Files 的另一个名字，ENV~1 可能就是 .env
  设备名            CON、NUL、AUX、PRN、COM1、LPT1（nul.txt、COM¹ 也算）在任何文件夹里都是设备，不是文件
  网络和设备路径     \\服务器\共享、//服务器/共享 一碰就去连那台主机（还会把你的登录凭据发过去）；
                   \\?\C:\…、\\.\…、\\?\UNC\… 跳过 Windows 平常对路径的整理，上面那些规矩都可能不算数
  盘符的相对写法     C:x 是 C 盘"当前目录"里的 x，\x 是"当前盘"的根目录：要看此刻的状态才知道是哪里

所以这里有三样：
  trick(raw)       模型或页面直接给的一个路径，用没用上面这些花招（只在 Windows 上看）。用了就不认，
                   宁可让它换个普通的写法再来，也不去猜它指的是哪里。普通的大小写不同、/ 和 \ 混用，
                   在 Windows 上天天见，不算花招
  word_trick(raw)  命令里的一个词（不一定是路径）：只挑出看不清指向哪里、一碰就有事的写法。
                   大小写、尾巴上的点、数据流这几种交给 key 统一了再比，不然 findstr /C:文字 这种普通的命令都要问
  key(path)        比较用的写法：上面"同一个文件的不同写法"都变成同一串字
短名字没法只看字符串就知道是谁的别名，key 管不了它：直接给的路径不认它（trick），已经存在的文件
还要按"是不是同一个文件"再比一次（st_dev、st_ino；NTFS 上 st_ino 是文件的编号），调用的人自己做。

还有链接。Windows 的"目录联接"（junction）和符号链接一样能指到别处，可是 stat.S_ISLNK 和 DirEntry.is_symlink
都认不出它；O_NOFOLLOW 在 Windows 上也没有。linked、entry_linked、open_nofollow 补上这两样。

函数都带一个 windows 参数（不给就看现在是不是 Windows），在 macOS、Linux 上也能按 Windows 的规矩测试。
"""

from __future__ import annotations

import errno
import ntpath
import os
import re
import stat
import unicodedata
from pathlib import Path

WINDOWS = os.name == "nt"

# 设备名：在任何文件夹里、带不带扩展名（nul.txt）都指向设备。各个 Windows 版本的规矩不完全一样
# （Windows 11 不再把 nul.txt 当设备），这里按最严的算。¹²³ 是上标的数字：COM¹ 也是一个串口
DEVICES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"} | {f"{kind}{n}" for kind in ("COM", "LPT") for n in "0123456789¹²³"}
)
# Windows 不许出现在文件名里的字符（* ? 还是通配符，< > " 在找文件时也是通配符）。冒号另外看（盘符、数据流）
FORBIDDEN = frozenset('<>"|?*')
# 一段名字最长多少个字符（NTFS 的上限）
MAX_NAME = 255
# 这样开头的（/ 先换成 \ 再看）是网络上的位置或者设备路径：\\服务器\共享、\\?\…、\\.\…，以及 NT 内部的 \??\…
_ELSEWHERE = ("\\\\", "\\??\\")
# 8.3 短名字的样子：~ 加数字，后面要么结束，要么是一个扩展名（PROGRA~1、ENV~1、CHANNE~1.JSO）
_SHORT = re.compile(r"~[0-9]+(\.[^.]*)?$")
# 文件的属性里有这一位，说明它是一个"重解析点"；重解析的种类（tag）里有这一位，说明它代表别处的一个名字
# （符号链接、目录联接都是）。OneDrive 的占位文件也是重解析点，但没有这一位，它就是这个文件本身
_NAME_SURROGATE = 0x20000000
# 打开文件时不跟着链接走。Windows 没有这个旗子（os.O_NOFOLLOW 直接报 AttributeError），见 open_nofollow
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
# 按字节打开。只有 Windows 有这个旗子（见 open_nofollow）
BINARY = getattr(os, "O_BINARY", 0)


def _windows(windows: bool | None) -> bool:
    return WINDOWS if windows is None else windows


def _parts(raw: str) -> tuple[str, list[str]]:
    r"""（开头：盘符、\\服务器\共享……；后面的每一段名字）。按 Windows 的规矩，\ 和 / 都是分隔符。"""
    drive, rest = ntpath.splitdrive(raw)
    return drive, [part for part in re.split(r"[\\/]", rest) if part]


# ---- 花招 ------------------------------------------------------------------


def name_trick(name: str) -> str | None:
    """一段名字（不带分隔符）在 Windows 上会不会被当成别的东西。会的话返回原因，不会返回 None。

    不管现在是什么系统都按 Windows 的规矩看：压缩包、上传的文件夹要用它，在 Windows 上解开会出事的，在哪儿都不收。
    原因是写给模型看的中文（调用的人给用户看时另写一句翻译过的）。
    """
    if name in (".", ".."):
        return None
    if any(ord(char) < 32 for char in name):
        return f"{name!r} 里有控制字符"
    if ":" in name:
        return f"{name} 里有冒号：在 Windows 上这是文件的“数据流”（.env:x 读写的其实是 .env）"
    if FORBIDDEN.intersection(name):
        return f"{name} 里有 Windows 不许用在文件名里的字符（< > \" | ? *）"
    if name[-1] in ". ":
        return f"“{name}”以点或空格结尾：Windows 会把它们去掉（.env. 打开的其实是 .env）"
    if _device(name):
        return f"{name} 是 Windows 的设备名（CON、NUL、COM1……），打开它碰到的是设备，不是文件"
    if _SHORT.search(name):
        return f"{name} 像 Windows 的短名字（PROGRA~1 这种），它可能是另一个文件的别名"
    return None


def archive_trick(name: object, max_parts: int = 64) -> str | None:
    """压缩包里一项的名字（或者页面上传的文件夹里的一个相对路径）能不能解开到一个文件夹里。不能的话返回原因，能的话返回 None。

    这样的名字是从外面来的，一律当作不可信：../../.bashrc、/etc/hosts、C:\\x 会写到文件夹外面去（zip slip）；
    a.md:x、CON、PROGRA~1 在 Windows 上解开就成了别的东西（见 name_trick）。不管现在是什么系统都按最严的看：
    同一个压缩包换到 Windows 上解开也得是安全的。分隔符只认 /（zip 的规矩），反斜杠一律不收。
    Skill 的安装（skills/bundle.py）和从备份恢复（restore.py）都用它。原因是写给程序看的中文，调用的人给用户看时另写一句。
    """
    if not isinstance(name, str) or not 0 < len(name) <= 1000:
        return "名字是空的或者太长"
    if "\\" in name or any(ord(char) < 32 or ord(char) == 127 for char in name):
        return f"{name!r} 里有反斜杠或控制字符"  # 反斜杠在 Windows 上是路径的分隔符
    if name.startswith("/") or re.match(r"[A-Za-z]:", name):
        return f"{name} 是绝对路径"  # 包括 C: 这样的盘符
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return f"{name} 里有空的一段、. 或 .."
    if len(parts) > max_parts:
        return f"{name} 的层数太多"
    if any(len(part.encode("utf-8", "surrogatepass")) > MAX_NAME for part in parts):
        return f"{name} 里有一段名字太长"
    for part in parts:
        reason = name_trick(part)
        if reason:
            return reason
    return None


def trick(raw: str, windows: bool | None = None) -> str | None:
    """模型或页面直接给的一个路径用没用 Windows 的花招（见文件开头）。用了返回原因，没用返回 None。不在 Windows 上永远是 None。"""
    if not _windows(windows):
        return None
    if any(ord(char) < 32 for char in raw):
        return "路径里有控制字符"
    flat = raw.replace("/", "\\")
    if flat.startswith(_ELSEWHERE):
        return f"{raw} 是网络上的位置（\\\\服务器\\共享）或者 Windows 的设备路径（\\\\?\\、\\\\.\\），助手不碰这类路径"
    drive, parts = _parts(raw)
    if drive and flat[len(drive) : len(drive) + 1] != "\\":
        return f"{raw} 是盘符加相对路径（C:x），指的是那个盘上“当前目录”里的东西，看不清是哪里；请写完整的路径（C:\\…）"
    if not drive and flat.startswith("\\"):
        return f"{raw} 以斜杠开头却没有盘符，指的是“当前盘”的根目录；请写完整的路径（C:\\…）"
    for part in parts:
        reason = name_trick(part)
        if reason:
            return reason
    return None


def word_trick(raw: str, windows: bool | None = None) -> str | None:
    """命令里的一个词（不一定是路径），当作路径时是不是一碰就有事、或者看不清指向哪里。不在 Windows 上永远是 None。

    只挑这几种：网络和设备路径、设备名（一碰就有事）；盘符加相对路径、太长的名字（看不清指向哪里）。
    大小写、尾巴上的点和空格、数据流不算：key 会把它们统一成同一个文件再比。
    短名字也不在这里算：HEAD~1 这种普通的写法长得和它一样，要看磁盘上有没有这个名字（见 short_name）。
    """
    if not _windows(windows):
        return None
    flat = raw.replace("/", "\\")
    if flat.startswith(_ELSEWHERE):
        return f"{raw} 是网络上的位置或者 Windows 的设备路径"
    drive = ntpath.splitdrive(raw)[0]
    if len(drive) == 2 and flat[2:3] != "\\":
        return f"{raw} 是盘符加相对路径（C:x），看不清是哪里"
    parts = names(raw, windows=True)
    if any(_device(name) for name in parts):
        return f"{raw} 里有 Windows 的设备名"
    # NTFS 上一段名字最多 255 个字符。更长的根本不会有，可 Windows 上展开、stat 它都不报错（只当作"不存在"），
    # 看不出它是什么；macOS、Linux 上会报"名字太长"。两边都当作看不清
    if any(len(name) > MAX_NAME for name in parts):
        return f"{raw[:80]}… 里有一段名字太长了，看不清是什么"
    return None


def short_name(raw: str, windows: bool | None = None) -> bool:
    """路径里有没有一段长得像 Windows 的短名字（PROGRA~1）。不在 Windows 上永远是 False。"""
    return _windows(windows) and any(_SHORT.search(name) for name in names(raw, windows=True))


def reserved(name: str, windows: bool | None = None) -> bool:
    """name 当文件名用时，在 Windows 上是不是一个设备（CON、nul、COM1……，带扩展名也算）。不在 Windows 上永远是 False。"""
    return _windows(windows) and _device(name)


def _device(name: str) -> bool:
    """一段名字是不是设备名：看第一个点之前的部分（nul.txt、"nul .txt" 都是 NUL），不分大小写。"""
    return name.partition(".")[0].rstrip(" ").upper() in DEVICES


# ---- 比较用的写法 ----------------------------------------------------------


def _bare(part: str) -> str:
    """Windows 上一段名字真正指的那个名字：去掉数据流（:后面的）、尾巴上的点和空格。"""
    if part in (".", ".."):
        return part
    return part.partition(":")[0].rstrip(". ")


def names(raw: str, windows: bool | None = None) -> list[str]:
    r"""路径里的每一段名字（不含盘符和开头的斜杠）。Windows 上 \ 和 / 都是分隔符，每一段是它真正指的那个名字
    （.env::$DATA、.env. 都是 .env）；别的系统上按 / 分开、原样不动。"""
    if not _windows(windows):
        return [part for part in raw.split("/") if part]
    return [_bare(part) for part in _parts(raw)[1]]


def key(path: str | Path, windows: bool | None = None) -> str:
    """比较路径用的写法：同一个文件的几种写法（见文件开头）变成同一串字。分隔符一律是 /。

    在哪儿都不分大小写、不分 Unicode 的两种写法：macOS 默认的磁盘就是这样。Linux 上 .ENV 和 .env 是两个文件，
    这样比会多拦一些，只会多问，不会少问。Windows 上再去掉每一段的数据流和尾巴上的点、空格。
    """
    text = str(path)
    if _windows(windows):
        drive, rest = ntpath.splitdrive(text.replace("/", "\\"))
        parts = [_bare(part) for part in rest.split("\\")]
        # 去掉以后什么都不剩的一段（"..."）当作没有：Windows 整理路径时也是把它去掉
        rest = "\\".join(part for index, part in enumerate(parts) if part or index == 0 or index == len(parts) - 1)
        text = (drive + rest).replace("\\", "/")
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", text).casefold())


def inside(path: str, folder: str) -> bool:
    """path 是不是 folder 本身或者在它里面。两个都是 key 算出来的。"""
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def within(path: Path, folder: Path, windows: bool | None = None) -> bool:
    """和 inside 一样，只是给的是路径：path 是不是 folder 本身或者在它里面（不分大小写，见 key）。"""
    return inside(key(path, windows), key(folder, windows))


# ---- 链接 ------------------------------------------------------------------


def linked(info: os.stat_result) -> bool:
    """lstat 的结果：这个名字本身是不是一个指向别处的链接（符号链接，或者 Windows 的目录联接）。

    目录联接的 st_mode 是"文件夹"，S_ISLNK 认不出它，要看 Windows 才有的属性和重解析的种类（见 _NAME_SURROGATE）。
    """
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT and getattr(info, "st_reparse_tag", 0) & _NAME_SURROGATE)


def is_link(path: str | Path) -> bool:
    """这个名字本身是不是链接（见 linked）。不存在的不算。"""
    try:
        return linked(os.lstat(path))
    except OSError:
        return False


def entry_linked(entry: os.DirEntry, windows: bool | None = None) -> bool:
    """os.scandir 里的一项本身是不是链接。DirEntry.is_symlink 认不出 Windows 的目录联接，在 Windows 上另外 lstat 一次。
    看不清的（正好在这时被删掉了）也当作链接：用到它的地方都是"链接不跟、不列"，这样最保险。"""
    if entry.is_symlink():
        return True
    if not _windows(windows):
        return False
    try:
        return linked(os.lstat(entry.path))
    except OSError:
        return True


def entry_info(entry: os.DirEntry, windows: bool | None = None) -> os.stat_result:
    """os.scandir 里一项的 lstat。Windows 上 DirEntry.stat() 的 st_ino、st_dev 永远是 0（文档这么写的），
    要比"是不是同一个文件"，得真的 lstat 一次。"""
    return os.lstat(entry.path) if _windows(windows) else entry.stat(follow_symlinks=False)


def open_nofollow(path: str | Path, flags: int, mode: int = 0o777) -> int:
    r"""os.open，但不跟着链接走：路径的最后一段是链接时打开失败（OSError），返回文件描述符。

    POSIX 上交给 O_NOFOLLOW。Windows 上没有这个旗子，就在打开前后各看一眼：打开前这个名字本身不能是链接，
    打开后拿到的必须是打开前看到的那个文件（同一个 st_dev、st_ino）。中间被换成了链接，打开的就是链接指向的
    另一个文件，对不上，关掉不用。新建的文件（O_CREAT 并且原来没有）没有"打开前"，只看打开之后它不是链接。

    一律按字节打开（O_BINARY）：Windows 上 os.open 默认是"文本模式"，写的时候把 \n 换成 \r\n、
    读的时候碰到 Ctrl-Z 就当作文件结束，图片、脚本都会被弄坏。别的系统没有这个旗子，也不需要。
    """
    flags |= BINARY
    if NOFOLLOW:
        return os.open(path, flags | NOFOLLOW, mode)
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        if not flags & os.O_CREAT:
            raise
        before = None
    if before is not None and linked(before):
        raise OSError(errno.ELOOP, "这是一个指向别处的链接", str(path))
    descriptor = os.open(path, flags, mode)
    try:
        after = os.fstat(descriptor)
        if before is not None and not os.path.samestat(before, after):
            raise OSError(errno.ELOOP, "打开的时候它被换成了别的文件", str(path))
        if before is None and is_link(path):
            raise OSError(errno.ELOOP, "这是一个指向别处的链接", str(path))
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor
