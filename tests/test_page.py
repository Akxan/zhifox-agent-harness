"""页面的几条静态检查：只读源码，不用浏览器，查那些一眼就能查出来、出了错却不会有任何报错的问题。
在真正的浏览器里打开页面、实际操作的测试在 tests/ui/（见 README 的"测试"一节）。

页面由 harness/webui/page/ 里的几块拼成（见 page/__init__.py）：这里也查拼法本身——每一块都列进了清单、
只列了一次，放进页面里不会提前结束那一段 <script> 或 <style>，每一块和拼好的主脚本都能通过 node --check。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest

from harness.webui import page, server


def _main_script() -> str:
    scripts = re.findall(r'<script nonce="__NONCE__">(.*?)</script>', page.assemble(), flags=re.DOTALL)
    return next(script for script in scripts if "这个页面做的事情很少" in script)


def test_no_two_functions_share_a_name():
    # 同一个作用域里两个同名的 function，后写的那个会悄悄顶替前面的，所有调用都去了后一个，不会有任何报错。
    # 真出过这种事：画缩略图用的 paint 和标出搜索关键词的 paint 撞了名，粘贴截图时报了一个莫名其妙的错。
    # 主脚本拆成了好几个文件，但拼起来还是同一个作用域，所以查的是拼好的那一整段
    names = re.findall(r"^\s*(?:async\s+)?function\s+(\w+)\s*\(", _main_script(), flags=re.MULTILINE)
    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
    assert duplicates == [], "这些函数名用了不止一次：" + "、".join(duplicates)


def test_every_part_is_listed_once():
    # 清单（page/__init__.py 的 SLOTS）就是拼的顺序。文件夹里多了一个没列的块，它根本不会进页面；
    # 列了两次，同一段脚本会跑两遍（用 const 定义的名字还会让整段脚本跑不起来）
    listed = page.parts()
    twice = sorted(name for name, count in Counter(listed).items() if count > 1)
    assert twice == [], "这些块在清单里写了不止一次：" + "、".join(twice)
    on_disk = {path.relative_to(page.FOLDER).as_posix() for path in page.FOLDER.rglob("*") if path.suffix in (".css", ".js")}
    assert sorted(on_disk - set(listed)) == [], "这些文件没有列进 page/__init__.py 的清单"
    assert sorted(set(listed) - on_disk) == [], "清单里的这些文件不存在"
    assert all(name.startswith("styles/") and name.endswith(".css") for name in page.STYLES)
    assert all(name.startswith("scripts/") and name.endswith(".js") for name in page.SCRIPTS)


def test_the_skeleton_has_each_slot_once_on_its_own_line():
    skeleton = (page.FOLDER / "index.html").read_text(encoding="utf-8")
    for slot in page.SLOTS:
        assert skeleton.count(slot) == 1 and f"\n{slot}\n" in skeleton, slot
    assembled = page.assemble()
    assert not any(slot in assembled for slot in page.SLOTS)
    # 服务器发的就是拼好的这一页
    assert server.PAGE == assembled


def test_every_part_can_be_inlined_into_the_page():
    for name in page.parts():
        text = (page.FOLDER / name).read_text(encoding="utf-8")
        # 每一块都原样放进页面里的一段 <script> 或 <style>：里面要是出现 </script、</style 或 <!--，
        # 浏览器会以为那一段提前结束了（或者进入一种奇怪的解析状态），后面的代码就成了页面上的文字
        assert "</script" not in text.lower() and "</style" not in text.lower() and "<!--" not in text, name
        # 发页面时要填的那几处（__NONCE__、__I18N__……）和拼的时候换掉的占位符，都不能出现在块里
        assert not re.search(r"__[A-Z][A-Z0-9_]*__", text), name
        # 一块接一块时中间空一行：块本身开头和结尾都不留空行，免得拼出来的空行忽多忽少
        assert text.endswith("\n") and not text.endswith("\n\n") and text.split("\n", 1)[0].strip(), name


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("CI 上找不到 node：页面脚本的语法检查没有跑")
        pytest.skip("这台电脑上没有 Node.js，跳过页面脚本的语法检查")
    return node


def test_every_script_passes_node_check(tmp_path):
    # 每一块单独查一遍：写错了一个括号，报错直接指到是哪个文件、第几行。
    # 再把拼好的主脚本整个查一遍：两块里用 const、let 定义了同一个名字，单独查都没事，
    # 拼在一起浏览器就会拒绝运行整段脚本，页面一片空白
    node = _node()
    whole = tmp_path / "main-script.js"
    whole.write_text(_main_script(), encoding="utf-8")
    files = [page.FOLDER / name for name in page.FIRST_PAINT + page.SCRIPTS] + [whole]

    def check(path):
        result = subprocess.run([node, "--check", str(path)], check=False, capture_output=True, text=True, timeout=60, encoding="utf-8")
        return result.returncode, result.stderr

    with ThreadPoolExecutor(max_workers=8) as pool:
        failed = [stderr for code, stderr in pool.map(check, files) if code != 0]
    assert failed == [], "\n\n".join(failed)
