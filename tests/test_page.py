"""页面脚本的几条静态检查。页面没有自动化的浏览器测试（见 README 的"测试"一节），这里只查那些一眼就能查出来、
出了错却不会有任何报错的问题。"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

PAGE = Path(__file__).parent.parent / "harness" / "webui" / "index.html"


def _main_script() -> str:
    page = PAGE.read_text(encoding="utf-8")
    scripts = re.findall(r'<script nonce="__NONCE__">(.*?)</script>', page, flags=re.DOTALL)
    return next(script for script in scripts if "这个页面做的事情很少" in script)


def test_no_two_functions_share_a_name():
    # 同一个作用域里两个同名的 function，后写的那个会悄悄顶替前面的，所有调用都去了后一个，不会有任何报错。
    # 真出过这种事：画缩略图用的 paint 和标出搜索关键词的 paint 撞了名，粘贴截图时报了一个莫名其妙的错
    names = re.findall(r"^\s*(?:async\s+)?function\s+(\w+)\s*\(", _main_script(), flags=re.MULTILINE)
    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
    assert duplicates == [], "这些函数名用了不止一次：" + "、".join(duplicates)
