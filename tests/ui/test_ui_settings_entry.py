"""顶栏上有一个看得见的"设置"按钮（齿轮），⌘, / Ctrl+, 也能打开设置；模型按钮只管换模型。

以前设置只藏在顶栏中间的模型按钮里（"Claude (Anthropic) · claude-opus-5-5"），看起来只是"现在用的模型"，
用户找不到设置在哪儿（"我的设置在哪里 没看见啊"）。后来加了齿轮，两个按钮却打开同一个窗口（"现在的设置和模型切换的入口
是一样的，能否分开？"），所以模型按钮改成打开专门的换模型面板（见 test_ui_models.py），设置里只留一句"现在用的是哪个"。
"""

from __future__ import annotations

from . import steps


def test_the_gear_opens_settings_and_the_model_button_does_not(page):
    assert page.js("!!document.getElementById('settings-open') && !document.getElementById('settings-open').hidden")
    page.click("#settings-open")
    page.wait(steps.is_open("settings"), what="设置窗口打开")
    # 设置里的"当前模型"只写现在用的是哪个，换模型的按钮打开顶上的面板
    assert page.text("#settings-model-name") == "Claude (Anthropic) · claude-opus-5-5"
    assert not page.exists("#pick-provider") and not page.exists("#apply-model")
    page.click("#settings-model-switch")
    page.wait(f"!{steps.is_open('settings')} && {steps.flyout_open('models')}", what="设置关上、换模型的面板弹出来")
    steps.escape_closes_flyout(page, "models", "#model")
    # 模型按钮：打开的是换模型的面板，不是设置
    steps.open_models(page)
    assert not page.js(steps.is_open("settings"))
    steps.escape_closes_flyout(page, "models", "#model")


def test_command_comma_opens_settings_and_the_hint_says_so(page):
    mac = page.js("/Mac|iPhone|iPad/.test(navigator.platform)")
    page.key("meta+," if mac else "ctrl+,")
    page.wait(steps.is_open("settings"), what="快捷键打开设置")
    steps.escape_closes(page, "settings")
    shortcut = "⌘," if mac else "Ctrl+,"
    assert page.js("document.getElementById('settings-open').title") == f"设置（{shortcut}）"
    # 换了语言，提示照样带着快捷键（翻译页面时不能把它冲掉）
    steps.language(page, "en")
    assert page.js("document.getElementById('settings-open').title") == f"Settings ({shortcut})"
    assert page.js("document.getElementById('settings-open').getAttribute('aria-label')") == "Settings"
    steps.language(page, "zh")
