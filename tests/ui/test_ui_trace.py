"""运行记录（页面里是 harness/webui/page/scripts/trace.js 和 trace-chart.js，背后是 harness/trace/）。

在真的页面上看：一轮结束以后它的回答下面多一个"运行记录"，点开是一张瀑布图（每一次请求、每一次工具调用一行，
子 Agent 的缩进画在交给它的那一步下面，等你确认的那一截画斜线），悬停、键盘都能看每一段的数字，Esc 关上、焦点回去；
"复制为 JSON"复制的是助手程序存下的那一份；刷新页面以后还在，样子一样，而且不再播入场动画；这一轮没有回答（出错了）时，
入口单独放一行，不挤掉"重新生成"的位置；深色下的字够清楚，窄屏上从底下升起来、变成一列。
服务器那一侧（记了什么、存进会话、不碰历史）在 tests/test_trace.py 里测。
"""

from __future__ import annotations

import json

from . import steps
from .test_ui_page import CONTRAST

LANES = "#trace-body .trace-lane"
# 窗口里看得见的样子：每一行的名字和时长、缩进了几层，详情有几段。比较刷新前后用
SHAPE = (
    "JSON.stringify([...document.querySelectorAll('#trace-body .trace-lane')].map((row) => [row.className.replace(/ ?(is|was)-\\S+/g, ''),"
    " row.querySelector('.trace-lane-title').textContent, row.querySelector('.trace-lane-time').textContent])"
    ".concat([document.querySelectorAll('#trace-body .trace-step').length]))"
)
# 演示里 [ui:trace] 那一轮（见 tests/demo_scripts.py 的 trace、tracesub）：主助手 4 次请求、3 次工具调用，
# 子 Agent 2 次请求、1 次工具调用，一共 10 段
TRACE_LANES = 10
# 窗口里要查对比度的字（浅色、深色都要过 4.5:1）
TEXT = ["#trace-sub", "#trace-body .trace-big small", "#trace-body .trace-stats dt", "#trace-body .trace-legend li", "#trace-body .trace-readout",
        "#trace-body .trace-lane-time", "#trace-body .trace-lane-sub", "#trace-body .trace-step-meta", "#trace-body .trace-step-n", "#trace-body .trace-foot"]
# "复制"写进剪贴板的东西，在测试里接住，不碰真的剪贴板
CATCH_COPY = (
    "window.__copied = null; Object.defineProperty(navigator.clipboard, 'writeText',"
    " { configurable: true, value: async (text) => { window.__copied = text; } })"
)


def _traced_turn(page) -> None:
    """跑一轮 [ui:trace]：查待办、执行一条要你批准的命令（这里点"允许"）、交给子 Agent、转告。"""
    steps.send(page, "[ui:trace] 帮我看看待办，再查一下 agent harness")
    steps.pending_confirm(page)
    page.click("#log .confirm .button.primary")
    steps.reply(page, "资料研究员查完了", timeout=40)
    page.wait("document.querySelector('#log .trace-open')", what="这一轮的运行记录出现了")


def test_a_turn_opens_its_trace_as_a_waterfall_and_it_survives_a_reload(page):
    _traced_turn(page)
    # 入口在最后那个回答下面那排按钮里，在"复制"和"重新生成"之间；写着这一轮用了多久、花了多少
    assert page.js("[...document.querySelector('#log .msg.assistant.turn-last .msg-actions').children].map((node) => node.classList.contains('trace-open') ? 'trace' : node.classList.contains('turn-retry') ? 'retry' : 'copy')") == ["copy", "trace", "retry"]
    words = page.text("#log .trace-open")
    assert ("秒" in words or "毫秒" in words) and "$" in words
    assert page.js("document.querySelector('#log .trace-open').classList.contains('trace-new')"), "实时这一轮结束时，入口淡淡地出现"

    steps.open_trace(page)
    assert page.js(f"document.querySelectorAll({LANES!r}).length") == TRACE_LANES
    # 子 Agent 的那三段缩进画在"交给「资料研究员」"下面
    assert page.js("document.querySelectorAll('#trace-body .trace-lane.depth-1').length") == 3
    assert "资料研究员" in page.js("[...document.querySelectorAll('#trace-body .trace-lane.depth-0')].map((row) => row.textContent).join('|')")
    # 等你确认的那一截画在"执行命令"那一行的横条里；模型请求的横条前面淡的那一截是"还没吐出第一个字"
    assert page.js("document.querySelectorAll('#trace-body .trace-lane .trace-wait').length") == 1
    assert page.js("document.querySelectorAll('#trace-body .trace-lane.kind-model .trace-first').length") >= 4
    # 合计：分段的横条四段加起来是整根，图例四项；详情一段一项
    width = page.js("[...document.querySelectorAll('#trace-body .trace-split i')].reduce((sum, seg) => sum + Number(seg.style.getPropertyValue('--size')), 0)")
    assert abs(width - 100) < 0.01
    assert page.js("document.querySelectorAll('#trace-body .trace-legend li').length") == 4
    assert page.js("document.querySelectorAll('#trace-body .trace-step').length") == TRACE_LANES
    # 每一根横条都在时间轴里面，不冲出右边
    assert page.js(
        "[...document.querySelectorAll('#trace-body .trace-bar')].every((bar) => { const track = bar.parentElement.getBoundingClientRect(), box = bar.getBoundingClientRect();"
        " return box.left >= track.left - 0.5 && box.right <= track.right + 0.5; })"
    )

    # 键盘：打开时焦点在第一行；↓ 到下一行，上面那一行字跟着写它的数字；回车跳到下面那一段的详情
    assert page.js("document.activeElement === document.querySelector('#trace-body .trace-lane')")
    page.key("ArrowDown")
    page.wait("document.activeElement === document.querySelectorAll('#trace-body .trace-lane')[1]", what="↓ 到第二行")
    assert "todo_list" in page.text("#trace-body .trace-readout")
    page.key("End")
    page.wait(f"document.activeElement === [...document.querySelectorAll({LANES!r})].pop()", what="End 到最后一行")
    page.key("Enter")
    page.wait(f"document.querySelector('#trace-body .trace-step[data-step=\"{TRACE_LANES - 1}\"]').classList.contains('flash')", what="详情里那一段亮一下")
    # 悬停：上面那一行字写着这一段的数字（执行命令：真名、参数、你允许了、等了多久）
    page.hover(LANES, 3)
    page.wait("document.querySelector('#trace-body .trace-readout').textContent.includes('echo zhifox-trace')", what="悬停时写着数字")
    assert "你允许了" in page.text("#trace-body .trace-readout")

    # 复制为 JSON：助手程序存下的那一份（不带事件的编号和类型），提示写在窗口里
    page.js(CATCH_COPY)
    page.click("#trace-copy")
    page.wait("window.__copied !== null", what="复制了")
    copied = json.loads(page.js("window.__copied"))
    assert copied["version"] == 1 and len(copied["spans"]) == 7 and "id" not in copied and "type" not in copied
    assert copied["totals"]["requests"] == 6 and copied["totals"]["asked"] == 1 and copied["totals"]["allowed"] == 1
    assert page.text("#trace-status")

    # Esc 关上，焦点回到入口上
    steps.escape_closes(page, "trace-dialog")
    assert page.js("document.activeElement.classList.contains('trace-open')")
    steps.open_trace(page)
    before = page.js(SHAPE)
    steps.escape_closes(page, "trace-dialog")

    # 刷新：运行记录跟着这一轮的事件重放出来，入口还在原处，不播入场动画；点开是同一份
    page.reload()
    page.wait("document.body.classList.contains('ready') && document.querySelector('#log .trace-open')", what="重放完")
    assert page.js("document.querySelectorAll('#log .trace-open.trace-new, #log .turn-trace.enter').length") == 0
    playing = page.js(
        "document.getAnimations().filter((a) => document.getElementById('log').contains(a.effect.target) && a.effect.target.closest('.trace-open, .turn-trace')).length"
    )
    assert playing == 0, "重放出来的入口不该播动画"
    steps.open_trace(page)
    assert page.js(SHAPE) == before
    steps.escape_closes(page, "trace-dialog")


def test_a_turn_without_an_answer_gets_its_own_trace_row(page):
    # 第一次请求就失败了（400）：这一轮没有回答。入口单独放一行，"重新生成"照旧紧跟在出错提示下面
    steps.send(page, "[ui:flaky] 试一下")
    page.wait("!document.body.classList.contains('busy') && document.querySelector('#log .notice.error + .turn-actions .turn-retry')", 20, "出错提示下面的重新生成")
    assert page.exists("#log .turn-trace .trace-open")
    steps.open_trace(page)
    assert page.js(f"document.querySelectorAll({LANES!r}).length") == 1
    assert page.js("document.querySelector('#trace-body .trace-lane').classList.contains('is-error')")
    assert "出错了" in page.text("#trace-sub")
    steps.escape_closes(page, "trace-dialog")


def test_the_trace_reads_well_in_dark_mode_and_at_phone_width(page):
    _traced_turn(page)
    steps.open_trace(page)
    for selector, ratio in page.js(f"({CONTRAST})({TEXT!r})"):
        assert ratio is not None and ratio >= 4.5, f"浅色下 {selector} 的对比度只有 {ratio}"
    steps.escape_closes(page, "trace-dialog")
    page.click("#theme")
    page.wait("document.getElementById('appearance').matches(':popover-open')", what="外观面板打开")
    page.click("[data-theme-pick='dark']")
    page.wait("document.documentElement.dataset.theme === 'dark'", what="换成深色")
    page.key("Escape")
    steps.idle(page)
    steps.open_trace(page)
    for selector, ratio in page.js(f"({CONTRAST})({TEXT!r})"):
        assert ratio is not None and ratio >= 4.5, f"深色下 {selector} 的对比度只有 {ratio}"
    steps.escape_closes(page, "trace-dialog")

    # 窄屏：窗口贴着底边、占满宽度；每一行的时间轴在名字下面（一列），横条不冲出去
    page.size(390, 844)
    steps.idle(page)
    steps.open_trace(page)
    box = page.js("(() => { const box = document.getElementById('trace-dialog').getBoundingClientRect(); return [box.left, box.right, box.bottom, innerWidth, innerHeight]; })()")
    assert abs(box[0]) <= 1 and abs(box[1] - box[3]) <= 1 and abs(box[2] - box[4]) <= 1, box
    assert page.js(
        "[...document.querySelectorAll('#trace-body .trace-lane')].every((row) => row.querySelector('.trace-track').getBoundingClientRect().top >= row.querySelector('.trace-lane-name').getBoundingClientRect().bottom - 1)"
    ), "窄屏上时间轴应该在名字下面"
    assert page.js("document.documentElement.scrollWidth <= innerWidth")
    steps.escape_closes(page, "trace-dialog")
