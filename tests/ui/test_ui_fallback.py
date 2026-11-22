"""备用模型（页面里是 harness/webui/page/scripts/fallback.js，规矩在 harness/fallback.py）。

在真的页面上看：设置里"备用模型"一节，没选的时候是一块空状态；下拉框（select.field，页面自己画的箭头）里只有这一家的
别的模型；加一个、往前挪、去掉，每一步都马上存好，焦点留在看得见的地方（不然 Esc 会直接关掉设置）；
三种语言都翻译了，窄屏上模型名不冲出去，深色下的字够清楚。
再跑一轮 [ui:fallback]（演示里 Opus 5.5 的请求一律回答 529）：对话里说一声改用了 Sonnet 5.5，
运行记录里失败的那次标成出错，用备用模型的那次标着"备用"。服务器那一侧在 tests/test_fallback.py 里测。
"""

from __future__ import annotations

from . import checks, steps
from .test_ui_page import CONTRAST

SONNET, FABLE = "claude-sonnet-5-5", "claude-fable-5-2"
ROWS = "#fallback-list .fallback-row"
ORDER = f"[...document.querySelectorAll({ROWS!r})].map((row) => row.dataset.model)"
TEXT = ["#fallback-section h3 small", "#fallback-section .fallback-hint", "#fallback-list .fallback-name", "#fallback-list .fallback-n", "#fallback-empty"]


def _pick(page, model: str) -> None:
    page.focus("#fallback-add")
    page.js(f"(s => {{ s.value = {model!r}; s.dispatchEvent(new Event('change', {{ bubbles: true }})); }})(document.getElementById('fallback-add'))")


def test_backup_models_are_added_moved_and_removed_in_place(page):
    steps.open_dialog(page, "#settings-open", "settings")
    # 空状态：一句说明，名单藏着；下拉框里只有同一家的别的模型（现在用的 Opus 5.5 不在里面）
    assert page.js(page.visible("#fallback-empty")) and not page.js(page.visible("#fallback-list"))
    assert page.js("[...document.getElementById('fallback-add').options].map((o) => o.value)") == ["", SONNET, FABLE]
    assert "fallback-add" not in " ".join(checks.native_selects(page))

    _pick(page, SONNET)
    page.wait(f"document.querySelectorAll({ROWS!r}).length === 1", what="加上了第一个备用模型")
    assert page.js("document.activeElement.id") == "fallback-add"  # 焦点还在下拉框上，接着能再加
    # 新的一行由状态更新画出来，"已保存"这句要等保存的请求回来才写：机器慢的时候前者先到，所以等它，不直接读
    page.wait("document.getElementById('settings-message').textContent.includes('备用模型已保存')", what="说已保存")
    _pick(page, FABLE)
    page.wait(f"document.querySelectorAll({ROWS!r}).length === 2", what="加上了第二个")
    assert page.js(ORDER) == [SONNET, FABLE]
    assert page.js("document.getElementById('fallback-add').disabled")  # 这一家没有别的可选了
    assert page.js(f"document.querySelector({ROWS!r} + ' [data-action=up]').disabled")  # 第一个不能再往前挪

    # 往前挪：第二个换到第一个，焦点留在它那一行（它的"往前挪"已经不能点了，就放在"去掉"上）
    page.click(f"{ROWS}[data-model='{FABLE}'] [data-action=up]")
    page.wait(f"JSON.stringify({ORDER}) === JSON.stringify([{FABLE!r}, {SONNET!r}])", what="顺序换过来了")
    assert page.js("document.activeElement.dataset.action") == "remove"
    assert page.js("document.activeElement.closest('.fallback-row').dataset.model") == FABLE
    # 刷新以后还是这个顺序（存在服务器上）
    page.reload()
    page.wait("document.body.classList.contains('ready')")
    steps.open_dialog(page, "#settings-open", "settings")
    assert page.js(ORDER) == [FABLE, SONNET]

    # 三种语言（设置开着时换：在原地重译）、窄屏
    for code, lang in (("en", "en"), ("es", "es")):
        page.demo.api("/api/language", {"language": code})
        page.wait(f"document.documentElement.lang === {lang!r}", what=f"换成 {code}")
        steps.idle(page)
        assert checks.untranslated(page, ["#fallback-section"]) == []
    page.size(390, 844)
    steps.idle(page)
    assert checks.overflow(page, ["#fallback-section"]) == []
    page.size(1280, 900)
    page.demo.api("/api/language", {"language": "zh"})
    page.wait("document.documentElement.lang === 'zh-CN'", what="换回中文")

    # 去掉两个：回到空状态，焦点回到下拉框上；Esc 照常关上设置
    page.click(f"{ROWS}[data-model='{FABLE}'] [data-action=remove]")
    page.wait(f"document.querySelectorAll({ROWS!r}).length === 1")
    assert page.js("document.activeElement.closest('.fallback-row').dataset.model") == SONNET
    page.click(f"{ROWS} [data-action=remove]")
    page.wait(page.visible("#fallback-empty"), what="回到空状态")
    assert page.js("document.activeElement.id") == "fallback-add"
    assert "已经不用备用模型了" in page.text("#settings-message")
    steps.escape_closes(page, "settings")


def test_backup_rows_are_readable_in_dark_mode(page):
    page.demo.api("/api/fallbacks", {"provider": "anthropic", "models": [SONNET]})
    page.click("#theme")
    page.click("[data-theme-pick='dark']")
    page.wait("document.documentElement.dataset.theme === 'dark'", what="换成深色")
    page.key("Escape")
    steps.open_dialog(page, "#settings-open", "settings")
    page.wait(f"document.querySelectorAll({ROWS!r}).length === 1")
    steps.calm(page)
    for selector, ratio in page.js(f"({CONTRAST})({TEXT!r})"):
        assert ratio is None or ratio >= 4.5, f"深色下 {selector} 的对比度只有 {ratio}"


def test_an_overloaded_model_hands_the_turn_to_the_backup(page):
    page.demo.api("/api/fallbacks", {"provider": "anthropic", "models": [SONNET]})
    steps.send(page, "[ui:fallback] 你好")
    steps.reply(page, f"这次是 {SONNET} 回答的")
    assert "Opus 5.5 暂时不可用，这一轮接下来改用 Sonnet 5.5。" in page.text("#log")
    page.wait("document.querySelector('#log .trace-open')", what="这一轮的运行记录出现了")
    steps.open_trace(page)
    lanes = "#trace-body .trace-lane"
    assert page.js(f"document.querySelectorAll({lanes!r}).length") == 2
    assert page.js(f"document.querySelectorAll({lanes!r})[0].classList.contains('is-error')")  # 过载的那一次
    assert page.js(f"document.querySelectorAll({lanes!r})[1].querySelector('.trace-lane-sub').textContent") == f"{SONNET}（备用）"
    assert "备用模型：替 claude-opus-5-5" in page.text("#trace-body")
    assert "529" in page.text("#trace-body")  # 失败的原因写在那一步的详情里
    steps.escape_closes(page, "trace-dialog")
