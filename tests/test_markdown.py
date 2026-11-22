"""回复里 Markdown 渲染器（harness/webui/markdown.js）的测试。

渲染器是浏览器里跑的 JavaScript，真正的测试写在 tests/markdown.test.mjs 里，用 Node 自带的测试工具跑。
这里做两件事：让 `uv run pytest` 顺带把它跑一遍（电脑上没有 Node 时跳过；GitHub Actions 的机器上都有 Node，
在那里没有就算失败，免得测试悄悄地不跑了），再查几条一眼就能查出来的规矩。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from harness.webui import server

ROOT = Path(__file__).parent.parent
RENDERER = ROOT / "harness" / "webui" / "markdown.js"
NODE_TESTS = Path(__file__).parent / "markdown.test.mjs"


def test_the_renderer_passes_its_node_tests():
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("CI 上找不到 node：Markdown 渲染器的测试没有跑")
        pytest.skip("这台电脑上没有 Node.js，跳过 Markdown 渲染器的测试")
    result = subprocess.run([node, "--test", str(NODE_TESTS)], check=False, capture_output=True, text=True, timeout=300, cwd=ROOT, encoding="utf-8")
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-2000:]


def test_the_renderer_never_parses_html_or_runs_code():
    # 模型的输出只能变成文字节点和我们自己建的元素。这几样接口会把字符串当成 HTML 或代码，渲染器里一处都不许有
    code = RENDERER.read_text(encoding="utf-8")
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "setTimeout(", "DOMParser", "createContextualFragment"):
        assert forbidden not in code, forbidden
    # setAttribute 只设这几个固定的属性名（不会出现 on... 这类事件属性，也不会有 style、src）
    names = set(re.findall(r"setAttribute\('([^']+)'", code))
    assert names <= {"href", "target", "rel", "title", "start", "type", "disabled", "checked", "scope"}, names
    assert "setAttribute(name" not in code and "setAttribute(key" not in code


def test_the_renderer_can_be_inlined_into_the_page_safely():
    code = RENDERER.read_text(encoding="utf-8")
    # 它被原样填进页面的一个 <script> 里：里面要是出现 </script 或 <!--，浏览器会提前结束这段脚本
    assert "</script" not in code.lower() and "<!--" not in code
    # 页面里要填的那几个占位符也不能出现在它里面
    for placeholder in ("__NONCE__", "__TOKEN__", "__I18N__", "__MARKDOWN__"):
        assert placeholder not in code
    # 页面里给它留了位置：一个带 nonce 的 <script>，排在页面主脚本前面
    page = server.PAGE
    slot = page.index('<script nonce="__NONCE__">__MARKDOWN__</script>')
    assert slot < page.index("// 这个页面做的事情很少")
    assert server.MARKDOWN == code
