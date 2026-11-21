"""顶栏上有一个看得见的"设置"按钮（齿轮），⌘, / Ctrl+, 也能打开设置。

以前设置只藏在顶栏中间的模型按钮里（"Claude (Anthropic) · claude-opus-5-5"），看起来只是"现在用的模型"，
用户找不到设置在哪儿（"我的设置在哪里 没看见啊"）。
"""

from __future__ import annotations

from . import steps


def test_the_gear_in_the_top_bar_opens_settings(page):
    assert page.js("!!document.getElementById('settings-open') && !document.getElementById('settings-open').hidden")
    page.click("#settings-open")
    page.wait(steps.is_open("settings"), what="设置窗口打开")
    steps.escape_closes(page, "settings")
    # 模型按钮照样能打开
    page.click("#model")
    page.wait(steps.is_open("settings"), what="设置窗口打开（模型按钮）")
    steps.escape_closes(page, "settings")


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
