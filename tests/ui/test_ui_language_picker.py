"""选语言的面板：地球按钮弹出来的那一个（page/scripts/language.js），和外观、换模型是同一种小面板（flyouts.js）。

用户的原话："选择语言的卡片设计也不好看，你设计的优雅点！"以前是一个盖住整页的窗口，三个占满整行的大方框，
选中的那个带粗边框，底下的说明被切掉半截。现在：锚在地球按钮上的轻量面板；每一项大字是这门语言自己的名字，
小字是用界面语言写的名字（界面是中文时，English 下面写"英语"），现在用的那一项打一个细细的勾；
底下那句说明完整地显示；↑↓ 移动、回车选、Esc 收起来，焦点回到地球按钮上；窄屏上从底下升起来。
换语言照旧是原地重译、不刷新（test_ui_language.py 查得更细）。
"""

from __future__ import annotations

from . import checks, steps
from .pilot import PHONE_WIDTH

NOTE = "换的是界面上的文字。助手用什么语言回答，取决于你用什么语言和它说话。"
WORDS = "[...document.querySelectorAll('#language-options .lang-option')].map((node) => [node.querySelector('b').textContent, node.querySelector('small').textContent, node.getAttribute('aria-checked')])"
# 说明那一句整个在面板里、没有被切掉：它的框在面板的框里，自己也没有被裁（scrollHeight 不比看得见的高）
NOTE_WHOLE = (
    "(() => { const note = document.querySelector('#languages .lang-note'), panel = document.getElementById('languages');"
    " const a = note.getBoundingClientRect(), b = panel.getBoundingClientRect();"
    " return a.top >= b.top && a.bottom <= b.bottom + 0.5 && a.bottom <= innerHeight && note.scrollHeight <= note.clientHeight + 1; })()"
)


def test_the_globe_opens_a_light_panel_with_each_language_in_its_own_words(page):
    page.click("#language")
    page.wait(steps.flyout_open("languages"), what="语言面板弹出来")
    steps.idle(page)
    assert page.js("document.querySelector('dialog[open]')") is None, "选语言不再是盖住整页的窗口"
    assert page.js("document.getElementById('language').getAttribute('aria-expanded')") == "true"
    assert page.js(WORDS) == [["中文", "当前语言", "true"], ["English", "英语", "false"], ["Español", "西班牙语", "false"]]
    assert page.js("document.activeElement.lang") == "zh-CN"  # 焦点在现在用的那一项上
    assert page.text("#languages .lang-note") == NOTE
    assert page.js(NOTE_WHOLE), "底下那句说明被切掉了"
    # 现在用的那一项只多一个勾，不加粗框
    current = "document.querySelector('#language-options [aria-checked=true]')"
    assert page.js(f"getComputedStyle({current}).borderTopWidth") == "0px"
    assert page.js(f"getComputedStyle({current}.querySelector('.lang-check')).opacity") == "1"
    assert page.js("getComputedStyle(document.querySelector('#language-options [aria-checked=false] .lang-check')).opacity") == "0"
    steps.escape_closes_flyout(page, "languages", "#language")


def test_the_keyboard_picks_a_language_and_it_changes_in_place(page):
    page.js("window.__alive = true")
    page.focus("#language")
    page.key("Enter")
    page.wait(steps.flyout_open("languages"))
    page.wait("document.activeElement.lang === 'zh-CN'")
    page.key("ArrowDown")
    assert page.js("document.activeElement.lang") == "en"
    page.key("ArrowDown")
    page.key("ArrowDown")  # 到底了再按：回到第一项
    assert page.js("document.activeElement.lang") == "zh-CN"
    page.key("ArrowUp")
    assert page.js("document.activeElement.lang") == "es"
    page.key("ArrowUp")
    for _ in range(3):  # Tab 不出面板
        page.key("Tab")
        assert page.js("document.getElementById('languages').contains(document.activeElement)")
    page.focus("#language-options .lang-option[lang='en']")
    page.key("Enter")
    page.wait(f"document.documentElement.lang === 'en' && !{steps.flyout_open('languages')}", what="换成英文、面板收起")
    assert page.js("document.activeElement.id") == "language"  # 焦点回到地球按钮上
    assert page.js("window.__alive === true"), "换语言时页面刷新了"
    # 再打开：小字换成了英文，English 现在是"当前语言"
    page.key("Enter")
    page.wait(steps.flyout_open("languages"))
    assert page.js(WORDS) == [["中文", "Simplified Chinese", "false"], ["English", "Current language", "true"], ["Español", "Spanish", "false"]]
    assert page.text("#languages .lang-note") == "This changes the interface text. The assistant replies in whatever language you write to it in."
    assert checks.untranslated(page, ["#languages"]) == []
    steps.escape_closes_flyout(page, "languages", "#language")
    steps.language(page, "zh")


def test_on_a_phone_the_language_panel_rises_from_the_bottom(page):
    page.size(PHONE_WIDTH, 844)
    for code in ("es", "zh"):  # 西班牙语的说明最长
        steps.language(page, code)
        steps.open_flyout(page, "#language", "languages")
        box = page.js("(() => { const r = document.getElementById('languages').getBoundingClientRect(); return [r.left, r.right, r.bottom]; })()")
        width, height = page.js("[document.documentElement.clientWidth, innerHeight]")
        assert page.js("document.getElementById('languages').classList.contains('as-sheet')")
        assert abs(box[0]) <= 1 and abs(box[1] - width) <= 1 and abs(box[2] - height) <= 1, f"面板应该贴着底边、占满宽度：{box}"
        assert page.js(NOTE_WHOLE), f"{code}：底下那句说明被切掉了"
        assert checks.overflow(page, ["#languages"]) == []
        steps.escape_closes_flyout(page, "languages", "#language")


def test_with_reduced_motion_the_language_panel_just_appears(page):
    steps.calm(page)
    page.click("#language")
    page.wait(steps.flyout_open("languages"))
    durations = page.js("document.getElementById('languages').getAnimations().map((a) => a.effect.getComputedTiming().duration)")
    assert all(duration <= 1 for duration in durations), durations
