"""操作整台电脑在页面上的样子：确认卡片上多给的"这段对话里……不再问"、设置里的说明、信息栏里的"问不问你"。

用户问过："操作电脑为什么每一步都问，没有让用户给权限的选项呢？"所以这里要守住：
  - 截图的卡片上是"这段对话里截图不再问"，点击、打字的卡片上是"这段对话里的点击、打字不再问"，后者下面一句话讲清风险
    （屏幕上的字可能把它带偏、危险的组合键照样问、"停止"一直在）；选了以后打字、按键不再弹卡片，工具卡片上写着为什么没问；
  - 危险的组合键（⌘Q）照样弹卡片：不给"不再问"，也不放"不想每次都问？"；
  - 设置里写明怎么放开、哪些放开了也照样问（举的组合键按这台电脑的系统写），放开了的这段对话能在这里收回；
  - 信息栏里那一行跟着变："每次确认"时是"先问你"，"全部自动允许"或者放开了之后是"部分先问"（危险的组合键还问）；
  - 窄屏、三种语言下，卡片上那颗长按钮和那句话都不冲出卡片。

演示里"操作整台电脑"是开着的，背后是一台假的电脑（tests/fake_desktop.py），绝不碰真的屏幕、鼠标和键盘。
发一句含"演示电脑"的话，助手按 demo_web.py 的 computer_reply 走：截图 → 点输入框、打字 → ⌘S、截图 → ⌘Q。
"""

from __future__ import annotations

import pytest

from . import checks, steps
from .pilot import PHONE_WIDTH
from .test_ui_capabilities import row

CARD = "#log .confirm:not(.done)"
CHIP = f"document.querySelector({row('computer')!r} + ' .caps-chip').textContent"
ASK = "演示电脑：在备忘录里记一笔"


def answered(page, n: int) -> None:
    """等到答复过的卡片有 n 张（下一张可能马上就出来，不能等"没有待答复的卡片"）。"""
    page.wait(f"document.querySelectorAll('#log .confirm.done').length === {n}", 20, f"第 {n} 张卡片答复完")


def test_cards_offer_to_stop_asking_and_dangerous_keys_never_do(page):
    steps.open_side(page)
    page.wait(f"{CHIP} === '先问你'", what="信息栏里：每次确认时电脑那一行是先问你")
    steps.send(page, ASK)

    # 1. 截图：卡片上多一个"截图不再问"，没有那句风险的提醒；也有"不想每次都问？"（换成全部自动允许就不问了）
    steps.pending_confirm(page)
    assert page.text(CARD + " .confirm-trust") == "允许，这段对话里截图不再问"
    assert not page.exists(CARD + " .confirm-trust-note") and page.exists(CARD + " .link")
    page.click(CARD + " .button.primary")
    answered(page, 1)

    # 2. 点击：卡片上是"点击、打字不再问"，下面一句提醒，按钮用 aria-describedby 指着它
    steps.pending_confirm(page)
    assert "在电脑上点击鼠标" in page.text(CARD + " pre")
    assert page.text(CARD + " .confirm-trust") == "允许，这段对话里的点击、打字不再问"
    note = page.text(CARD + " .confirm-trust-note")
    assert "屏幕上的任何文字" in note and "危险的组合键" in note and "停止" in note
    assert page.js(
        f"document.querySelector({CARD!r} + ' .confirm-trust').getAttribute('aria-describedby') === document.querySelector({CARD!r} + ' .confirm-trust-note').id"
    )
    page.click(CARD + " .confirm-trust")
    answered(page, 2)

    # 3. 打字、⌘S 没有弹卡片：工具卡片上写着为什么没问
    page.wait("[...document.querySelectorAll('#log .call .auto-tag')].filter((n) => n.textContent === '自动允许（这段对话里不再问）').length === 2", 20, "打字、⌘S 标着自动允许")

    # 4. 截图照样问（截图是另一种，没选过）
    steps.pending_confirm(page)
    assert page.text(CARD + " .confirm-trust") == "允许，这段对话里截图不再问"
    page.click(CARD + " .button.primary")
    answered(page, 3)

    # 5. ⌘Q：照样问，带着警告；不给"不再问"，也没有"不想每次都问？"
    steps.pending_confirm(page)
    assert "⌘Q" in page.text(CARD + " pre") and "退出最前面的那个程序" in page.text(CARD + " pre")
    assert not page.exists(CARD + " .confirm-trust") and not page.exists(CARD + " .link")
    page.click(CARD + " .button:not(.primary)")  # 拒绝
    steps.reply(page, "这一步不做了")
    assert page.js("document.querySelectorAll('#log .confirm-trust, #log .confirm-trust-note').length") == 0  # 答复过的卡片只留结果

    # 信息栏：这段对话里放开了点击、打字，电脑那一行变成"部分先问"（危险的组合键还问）
    page.wait(f"{CHIP} === '部分先问'", what="放开以后是部分先问")
    page.click(row("computer") + " .caps-head")
    page.wait(f"document.querySelector({row('computer')!r} + ' .caps-head').getAttribute('aria-expanded') === 'true'")
    assert page.text(row("computer") + " .caps-summary") == "点击、打字不再问（这段对话里）；截图每次都问"

    # 设置里写着这段对话放开了什么，可以收回；收回以后焦点留在看得见的开关上
    page.click("#settings-open")
    page.wait(steps.is_open("settings"), what="设置窗口打开")
    steps.idle(page)
    page.wait("!document.getElementById('computer-trusted').hidden", what="设置里写着放开了什么")
    assert page.text("#computer-trusted-text") == "这段对话里，点击和打字不再问你。"
    page.click("#computer-untrust")
    page.wait("document.getElementById('computer-trusted').hidden && document.activeElement.id === 'computer-enabled'", what="收回以后藏起来、焦点在开关上")
    assert page.text("#settings-message") == "这段对话里又会每一步都先问你了。"
    steps.escape_closes(page, "settings")
    page.wait(f"{CHIP} === '先问你'", what="收回以后又是先问你")


def test_the_settings_explain_how_it_asks_with_keys_for_this_system(page):
    page.click("#settings-open")
    page.wait(steps.is_open("settings"), what="设置窗口打开")
    steps.idle(page)
    rules = page.text("#computer-card .browser-rules")
    assert "默认每一步都要你批准" in rules and "这段对话里的点击、打字不再问" in rules and "全部自动允许" in rules
    assert "屏幕上的任何文字" in rules and "停止" in rules
    # 演示里的假电脑是 macOS：举 Mac 的组合键（Windows、Linux 上写 Alt+F4、Ctrl+Alt+Del）
    assert page.text("#computer-rule-keys") == "⌘Q、⌘⌥Esc 这类按下去收不回来的组合键：不管怎么放开都照样问，卡片上带着警告"
    assert page.js("document.getElementById('computer-trusted').hidden")  # 这段对话里什么都没放开
    assert page.text("#computer-state") == "（开着）"
    assert "选了“全部自动允许”的不再问" in page.text("#computer-section")
    steps.escape_closes(page, "settings")
    steps.language(page, "en")
    page.click("#settings-open")
    page.wait(steps.is_open("settings"), what="设置窗口打开")
    steps.idle(page)
    assert page.text("#computer-rule-keys").startswith("Shortcuts you can't take back, like ⌘Q or ⌘⌥Esc")
    assert checks.untranslated(page, ["#computer-section"]) == []


def test_the_capabilities_row_follows_the_mode(page):
    steps.open_side(page)
    page.wait(f"{CHIP} === '先问你'", what="每次确认：先问你")
    status, reply = page.demo.api("/api/approval", {"mode": "all"})
    assert status == 200, reply
    page.wait(f"{CHIP} === '部分先问'", what="全部自动允许：部分先问（危险的组合键还问）")
    page.click(row("computer") + " .caps-head")
    page.wait(f"document.querySelector({row('computer')!r} + ' .caps-head').getAttribute('aria-expanded') === 'true'")
    assert page.text(row("computer") + " .caps-summary") == "点击、打字直接做；危险的组合键每次都问"
    status, reply = page.demo.api("/api/approval", {"mode": "edits"})
    assert status == 200, reply
    page.wait(f"{CHIP} === '先问你'", what="改文件不用问那一档不放开电脑")


@pytest.mark.parametrize("code", ["zh", "en", "es"])
def test_the_long_trust_button_fits_at_phone_width(page, code):
    page.size(PHONE_WIDTH, 844)
    steps.calm(page)
    if code != "zh":
        steps.language(page, code)
    steps.send(page, ASK)
    steps.pending_confirm(page)
    page.click(CARD + " .button.primary")  # 截图
    answered(page, 1)
    steps.pending_confirm(page)
    page.wait(f"document.querySelector({CARD!r} + ' .confirm-trust-note')", what="点击的卡片")
    assert checks.overflow(page, ["#log .confirm"]) == []
    fits = page.js(
        f"(() => {{ const card = document.querySelector({CARD!r}).getBoundingClientRect();"
        f" return [...document.querySelectorAll({CARD!r} + ' .confirm-trust, ' + {CARD!r} + ' .confirm-trust-note')]"
        " .every((node) => { const box = node.getBoundingClientRect(); return box.left >= card.left - 0.5 && box.right <= card.right + 0.5; }); })()"
    )
    assert fits, "卡片上的按钮或者那句提醒冲出了卡片"
    if code != "zh":
        assert checks.untranslated(page, ["#log .confirm"]) == []
    page.demo.api("/api/stop")
