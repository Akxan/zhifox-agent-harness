"""窄屏上浮在对话上面的"智狐的浏览器开着""智狐正在控制这台电脑"那一条，不挡住下面的按钮。

窄窗口里顶栏放不下这两条提示，它们改成浮在顶栏下面正中间。以前整条都接点击，盖在下面的按钮（比如"重新生成"）
就点不到了：一个页面测试在 390 宽时点"重新生成"，被前面的测试留下的"浏览器开着"挡住了（每次并行跑都这样）。
现在整条不接点击，只有它的"停止"按钮接。
"""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("pill", ["browser-live", "computer-live"])
def test_a_floating_status_pill_lets_clicks_through_except_its_stop_button(page, pill):
    page.size(390, 800)
    page.js(f"document.getElementById('{pill}').hidden = false")
    events = page.js(
        f"(() => {{ const pill = document.getElementById('{pill}'), stop = pill.querySelector('button');"
        " return [getComputedStyle(pill).position, getComputedStyle(pill).pointerEvents, stop && getComputedStyle(stop).pointerEvents]; })()"
    )
    assert events == ["absolute", "none", "auto"], events
    page.js(f"document.getElementById('{pill}').hidden = true")
