"""通配符：照 bash 的规矩，在命令真正运行的那个目录里先展开，再拿展开出来的每一个路径去判断。

为什么要展开，而不是一律看不懂：模型找"最占地方的文件夹"时几乎总是写 du -sk */ | sort -rn（2026-11-23 第一次真跑评测，
三家模型都这样写，每次都被问）。光看字面不知道 */ 会变成哪些文件夹，但在命令运行之前看一眼文件夹里有什么，
就和 bash 马上要做的一模一样了：展开出来的是 src/、docs/ 就照常判断；有 data/、.env、指向它们的链接，照样问你。

和 bash 对齐的几条（words.py 先挡掉了对不齐的写法：区间 [a-z]、取反 [^a]、字符类 [:alpha:]、花括号、extglob）：
  - 一个都对不上时，原样交给程序（bash 默认没开 nullglob、failglob）：ls nothing* 的 nothing* 就是一个普通的参数
  - * ? [..] 不匹配名字开头的点（dotglob 默认关着）：Python 的 glob 也是这样
  - .* 在 macOS 自带的 bash 3.2 里也对上 . 和 ..（bash 5.2 起默认不了，Linux 上两种都有）：补上它们一起查，宁可多查
  - 结果排好序（bash 按语言环境排，这里按字符排：先后不影响判断，展开出来的是哪些才影响）
  - ~ 开头的先换成主目录再展开，和 bash 先展开 ~ 一样
  - 展开出来像选项的名字（一个叫 -delete 的文件）照样交给规则当选项看：find * 在那种文件夹里就是 find -delete …
展开出来太多（READONLY_GLOB_MAX）就不一个个查了，照常问你。
在命令运行之前看的文件夹，运行的那一刻可能已经变了：中间隔着的只是几毫秒，而这一条命令里的每一段又都只读，
不会自己在中间造出一个新名字来；别的程序恰好在这几毫秒里改动这个文件夹的情形，这里不管。
"""

from __future__ import annotations

import fnmatch
import glob
import os
import re

from ..config import READONLY_GLOB_MAX
from .paths import Places
from .words import Glob, Unclear

_MAGIC = re.compile(r"[*?[]")  # 展开的样子里还有没有通配符（照字面的已经写成 [*] 这样，也算：宁可多当成通配符）


def expand(words: list[str], places: Places) -> list[str]:
    """把一段命令里要展开的词（Glob）换成展开出来的路径。展开不了的抛出 Unclear。"""
    result: list[str] = []
    for word in words:
        result.extend(_expand(word, places) if isinstance(word, Glob) else [word])
        if len(result) > READONLY_GLOB_MAX:
            raise Unclear(f"通配符展开出来超过 {READONLY_GLOB_MAX} 个路径")
    return result


def _expand(word: Glob, places: Places) -> list[str]:
    pattern = word.pattern
    if pattern == "~" or pattern.startswith("~/"):
        pattern = glob.escape(str(places.home)) + pattern[1:]
    trailing = pattern.endswith("/")
    parts = pattern.rstrip("/").split("/")
    last, base = parts[-1], "/".join(parts[:-1])
    if any(part.startswith(".") and _MAGIC.search(part) for part in parts[:-1]):
        raise Unclear("中间一层是以点开头的通配符（bash 3.2 里会对上 . 和 ..，这里看不准）")
    found = set(glob.glob(pattern, root_dir=places.cwd))
    if last.startswith(".") and _MAGIC.search(last):
        if _MAGIC.search(base):
            raise Unclear("以点开头的通配符前面还有通配符（bash 3.2 里每一层都会对上 . 和 ..，这里看不准）")
        literal_base = os.path.dirname(str(word).rstrip("/"))  # 前面没有通配符：照字面就是它
        for dot in (".", ".."):
            if fnmatch.fnmatchcase(dot, last):
                found.add(os.path.join(literal_base, dot) + ("/" if trailing else ""))
    return sorted(found) if found else [str(word)]
