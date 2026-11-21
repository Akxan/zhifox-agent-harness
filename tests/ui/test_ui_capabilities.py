"""信息栏里的"智狐能做的事"：给人看的功能清单，不是一排工具的真名。

用户原话："这个部分是不是功能远远不够啊？而且直接显示的是命令行，而不是功能。"所以这里要守住：
  - 每一行是一样功能（待办、读写文件、浏览器、每个 MCP 服务器……），写着在不在用、问不问你；工具的真名只在收着的"技术细节"里；
  - 关着的、下一段对话才有的，旁边的按钮直接打开对应的地方（设置里的那一节、扩展里的那一页、要不要先问你），或者开始新对话；
  - 问不问你跟着现在的确认方式变（由助手程序算，页面不另算）；
  - 三种语言、窄屏、深色都要对；刷新重放时不播入场动画，实时变成"在用"的那一行才淡入；键盘能操作，重画以后焦点还在原处。
演示里浏览器和"操作整台电脑"默认开着（见 tests/demo_web.py）：测试关掉了的，结束时打开回去。
"""

from __future__ import annotations

import json
import os

import pytest

from . import checks, steps
from .pilot import PHONE_WIDTH
from .test_ui_page import CONTRAST

# 模型手里的工具的真名：只许出现在"技术细节"里
RAW_NAMES = ["todo_add", "task_add", "str_replace_based_edit_tool", "run_command", "web_search", "web_fetch", "browser_toolset", "computer_toolset", "mcp__"]

ROW = "#caps .caps-item[data-id={id}]"


def row(name: str) -> str:
    return ROW.format(id=json.dumps(name))


def status_of(page, name: str) -> str:
    found = page.js(f"(n => n && [...n.classList].find((c) => /^caps-(on|off|pending|unavailable)$/.test(c)))(document.querySelector({row(name)!r}))") or ""
    return found.removeprefix("caps-")


def wait_status(page, name: str, status: str) -> None:
    page.wait(f"document.querySelector({row(name)!r}) && document.querySelector({row(name)!r}).classList.contains('caps-{status}')", what=f"{name} 变成 {status}")


def browser_settings(page, enabled: bool) -> None:
    status, reply = page.demo.api("/api/browser/settings", {"enabled": enabled})
    assert status == 200, reply


def test_the_section_lists_capabilities_not_tool_names(page):
    steps.open_side(page)
    assert page.text("#caps-title") == "智狐能做的事"
    labels = page.js("[...document.querySelectorAll('#caps-list .caps-label')].map((n) => n.textContent)")
    for wanted in ("待办", "定时任务", "记忆", "读写文件", "执行命令", "联网搜索和读网页", "Skill", "交给子 Agent", "浏览器", "操作整台电脑"):
        assert wanted in labels
    # MCP 服务器一个一行，上面一个小标题；"把文件发回聊天"这里用不了，收在最下面
    assert page.js("document.querySelector('#caps-list .caps-group').textContent") == "MCP 服务器"
    assert page.js(f"Boolean(document.querySelector({row('mcp:files')!r}))")
    assert not page.js("document.getElementById('caps-rest').hidden")
    assert page.js(f"document.querySelector({row('send')!r}).closest('ul').id") == "caps-rest-list"
    # 每一行都有一个小标签：图标加字（状态不只靠颜色）
    chips = page.js("[...document.querySelectorAll('#caps .caps-item')].map((n) => [n.dataset.id, n.querySelector('.caps-chip svg') !== null, n.querySelector('.caps-chip').textContent])")
    assert all(has_icon and text for _, has_icon, text in chips), chips
    assert page.js(f"document.querySelector({row('shell')!r} + ' .caps-chip').textContent") == "先问你"
    assert page.js(f"document.querySelector({row('todo')!r} + ' .caps-chip').textContent") == "直接做"
    assert page.js(f"document.querySelector({row('computer')!r} + ' .caps-chip').textContent") == "每步都问"
    assert page.text("#caps-mode-name") == "每次确认"

    # 点开"读写文件"：问不问你（几类操作各自怎样）、在哪儿执行；技术细节默认收着
    page.click(row("files") + " .caps-head")
    page.wait(f"document.querySelector({row('files')!r} + ' .caps-head').getAttribute('aria-expanded') === 'true'")
    assert "改文件前会问你" in page.text(row("files") + " .caps-summary")
    rules = page.js(f"[...document.querySelectorAll({row('files')!r} + ' .caps-rule')].map((n) => [n.querySelector('.caps-rule-text').textContent, n.querySelector('.caps-outcome').textContent])")
    assert ["读工作目录里的文件", "直接做"] in rules and ["改工作目录里的文件", "先问你"] in rules
    assert page.text(row("files") + " .caps-where") == "在这台电脑上执行"
    assert not page.js(f"document.querySelector({row('files')!r} + ' details.caps-tech').open")
    # 联网搜索在厂商的服务器上执行（原来那条图例的意思）
    page.click(row("web") + " .caps-head")
    page.wait(f"document.querySelector({row('web')!r} + ' .caps-dot-provider')")
    assert "Anthropic" in page.text(row("web") + " .caps-where")

    # 工具的真名：整节里只出现在"技术细节"里（文字和 title、aria-label 都算）
    steps.expand_capabilities(page)
    outside = page.js(
        "(() => { const clone = document.getElementById('caps').cloneNode(true);"
        " clone.querySelectorAll('.caps-tech').forEach((n) => n.remove());"
        " const attributes = [...clone.querySelectorAll('[title], [aria-label]')].map((n) => (n.title || '') + ' ' + (n.getAttribute('aria-label') || ''));"
        " return clone.textContent + ' ' + attributes.join(' '); })()"
    )
    assert not [name for name in RAW_NAMES if name in outside], outside
    inside = page.text(row("files") + " .caps-tech")
    assert "str_replace_based_edit_tool" in inside
    assert "browser_toolset_20260801" in page.text(row("browser") + " .caps-tech")


def test_keyboard_opens_rows_and_focus_survives_a_redraw(page):
    steps.open_side(page)
    page.focus(row("shell") + " .caps-head")
    page.key("Enter")
    page.wait(f"document.querySelector({row('shell')!r} + ' .caps-head').getAttribute('aria-expanded') === 'true'")
    assert page.js(f"document.getElementById(document.querySelector({row('shell')!r} + ' .caps-head').getAttribute('aria-controls')).hidden") is False
    assert page.text(row("shell") + " .caps-summary") == "每条命令都先问你"
    # 换一档确认方式：清单是助手程序按新的一档重新算的，页面重画了这一节，点开的那一行还开着，焦点还在原处
    status, reply = page.demo.api("/api/approval", {"mode": "readonly"})
    assert status == 200, reply
    if os.name == "nt":
        # 演示服务器真的在 Windows 上：命令一条都不算只读，这一行照样"每条都先问"，下面写着为什么
        page.wait(f"document.querySelector({row('shell')!r} + ' .caps-fine') && document.querySelector({row('shell')!r} + ' .caps-fine').textContent.includes('PowerShell')", what="执行命令下面写着 Windows 的原因")
        assert page.text(row("shell") + " .caps-summary") == "每条命令都先问你"
    else:
        page.wait(f"document.querySelector({row('shell')!r} + ' .caps-chip').textContent === '部分先问'", what="执行命令变成部分先问")
        assert "只读的命令直接执行" in page.text(row("shell") + " .caps-summary")
    assert page.text("#caps-mode-name") == "自动允许只读命令"
    assert page.js("document.activeElement.dataset.capFocus") == "shell|head"
    assert page.js(f"document.querySelector({row('shell')!r} + ' .caps-head').getAttribute('aria-expanded')") == "true"
    # 空格键收起来
    page.key("space")
    page.wait(f"document.querySelector({row('shell')!r} + ' .caps-head').getAttribute('aria-expanded') === 'false'")


def test_actions_open_the_right_places(page):
    steps.open_side(page)
    # 顶上的"确认方式"：打开"要不要先问你"
    page.click("#caps-mode")
    page.wait(steps.is_open("approval-dialog"), what="要不要先问你打开")
    steps.escape_closes(page, "approval-dialog")

    # 浏览器关掉：这段对话还没开始，旁边的按钮是"在设置里打开"，点了打开设置、焦点落在浏览器的开关上
    browser_settings(page, False)
    try:
        wait_status(page, "browser", "off")
        assert "这段对话就能用" in page.text(row("browser") + " .caps-brief")
        assert page.text(row("browser") + " .caps-quick") == "在设置里打开"
        page.click(row("browser") + " .caps-quick")
        page.wait(f"{steps.is_open('settings')} && document.activeElement.id === 'browser-enabled'", what="设置打开、焦点在浏览器的开关上")
        steps.escape_closes(page, "settings")

        # 对话开始以后再打开：写着"下一段对话"，按钮是"开始新对话"，点了以后新的这一段就带上浏览器
        steps.send(page, "[ui:todo] 记一条")
        steps.reply(page, "记下了")
        browser_settings(page, True)
        wait_status(page, "browser", "pending")
        assert page.text(row("browser") + " .caps-chip") == "下一段对话"
        assert page.text(row("browser") + " .caps-quick") == "开始新对话"
        page.click(row("browser") + " .caps-quick")
        page.wait("!document.querySelector('#log .msg.user')", what="开始了新对话")
        wait_status(page, "browser", "on")
    finally:
        browser_settings(page, True)

    # 一个连不上的 MCP 服务器：按钮是"管理"，点了打开扩展里的 MCP 那一页
    wait_status(page, "mcp:broken", "off")
    page.click(row("mcp:broken") + " .caps-quick")
    page.wait(f"{steps.is_open('extensions')} && !document.getElementById('panel-mcp').hidden", what="扩展的 MCP 那一页打开")
    steps.idle(page)
    page.key("Escape")  # 打开的是这个服务器的详情：第一下回到列表，第二下关上
    steps.idle(page)
    if page.js(steps.is_open("extensions")):
        steps.escape_closes(page, "extensions")

    # 在用的那几行，详情里有去管的地方：Skill → 扩展里的 Skill 那一页
    page.click(row("skill") + " .caps-head")
    page.click(row("skill") + " .caps-actions button")
    page.wait(f"{steps.is_open('extensions')} && !document.getElementById('panel-skills').hidden", what="扩展的 Skill 那一页打开")
    steps.escape_closes(page, "extensions")


def test_a_persona_shows_what_it_allows_and_why_the_rest_is_off(page):
    # "资料研究员"只放开了读文件和联网（tools: Read, WebSearch, WebFetch）：别的收在"用不了"里，写着是角色没放开
    status, reply = page.demo.api("/api/new", {"agent": "资料研究员"})
    assert status == 200, reply
    steps.open_side(page)
    page.wait("document.querySelector('#caps-notes .caps-note')", what="角色的说明")
    assert "资料研究员" in page.text("#caps-notes")
    wait_status(page, "shell", "unavailable")
    assert status_of(page, "files") == "on" and status_of(page, "web") == "on"
    assert page.js(f"document.querySelector({row('shell')!r}).closest('ul').id") == "caps-rest-list"
    page.click("#caps-rest-toggle")
    page.wait("!document.getElementById('caps-rest-list').hidden")
    assert "资料研究员" in page.text(row("shell") + " .caps-brief")
    # 只许读文件：改文件的那几类写着"不做"
    page.click(row("files") + " .caps-head")
    assert "只能读，不能改文件" in page.text(row("files") + " .caps-summary")
    assert page.js(f"[...document.querySelectorAll({row('files')!r} + ' .caps-outcome-never')].length") >= 1
    # "把文件发回聊天"：只有在聊天通道里才有，按钮去扩展里设置聊天通道
    page.click(row("send") + " .caps-head")
    page.click(row("send") + " .caps-actions button")
    page.wait(f"{steps.is_open('extensions')} && !document.getElementById('panel-channels').hidden", what="扩展的通道那一页打开")
    steps.escape_closes(page, "extensions")


@pytest.mark.parametrize("code", ["en", "es"])
def test_translated_in_place_with_nothing_left_in_chinese(page, code):
    steps.calm(page)
    steps.open_side(page)
    page.click(row("files") + " .caps-head")
    steps.language(page, code)
    assert page.js(f"document.querySelector({row('files')!r} + ' .caps-head').getAttribute('aria-expanded')") == "true"  # 原地换，点开的还开着
    steps.expand_capabilities(page)
    assert checks.untranslated(page, ["#caps"]) == []
    if code == "en":
        assert page.text("#caps-title") == "What 智狐 can do"
        assert page.text(row("files") + " .caps-label") == "Read and edit files"


@pytest.mark.parametrize("code", ["zh", "en", "es"])
def test_nothing_spills_out_at_phone_width(page, code):
    page.size(PHONE_WIDTH, 844)
    steps.calm(page)
    if code != "zh":
        steps.language(page, code)
    steps.open_side(page)
    steps.expand_capabilities(page)
    assert checks.overflow(page, ["#caps"]) == []
    # 名字不在字中间断开：一行放不下时，小标签换到下一行
    assert page.js(f"document.querySelector({row('computer')!r} + ' .caps-label').getClientRects().length") >= 1


def test_dark_contrast(page):
    page.js("localStorage.setItem('harness.theme', 'dark')")
    page.reload()
    page.wait("document.body.classList.contains('ready') && document.documentElement.dataset.theme === 'dark'")
    steps.calm(page)
    steps.open_side(page)
    page.click(row("files") + " .caps-head")
    page.click("#caps-rest-toggle")
    steps.idle(page)
    selectors = [
        "#caps-title", "#caps-count", "#caps-mode-name", ".caps-mode-key", "#caps .caps-label", "#caps .caps-brief", ".caps-level-free",
        ".caps-level-ask", ".caps-level-always", ".caps-state-off", ".caps-state-unavailable", "#caps .caps-quick", "#caps .caps-group",
        "#caps .caps-summary", "#caps .caps-rule-text", "#caps .caps-where", "#caps-rest-label",
    ]
    for selector, ratio in page.js(f"({CONTRAST})({selectors!r})"):
        assert ratio is None or ratio >= 4.5, f"深色下 {selector} 的对比度只有 {ratio}"


def test_replay_does_not_animate_but_a_live_change_does(page):
    steps.open_side(page)
    steps.idle(page)
    # 刷新：清单整个重新画一遍，不播入场动画
    page.reload()
    page.wait("document.body.classList.contains('ready') && document.querySelector('#caps .caps-item')")
    steps.open_side(page)
    assert page.js("document.getAnimations().filter((a) => a.effect.target && a.effect.target.closest && a.effect.target.closest('#caps .caps-item')).length") == 0
    # 实时发生的：浏览器关掉又打开（这段对话还没开始，马上换上），"浏览器"那一行变回在用时淡入一下
    browser_settings(page, False)
    try:
        wait_status(page, "browser", "off")
        page.js("window.__capsSeen = false; window.__capsWatch = new MutationObserver(() => { if (document.getAnimations().some((a) => a.effect.target && a.effect.target.matches && a.effect.target.matches('.caps-item[data-id=\"browser\"]'))) window.__capsSeen = true; }); window.__capsWatch.observe(document.getElementById('caps'), { subtree: true, childList: true, attributes: true })")
        browser_settings(page, True)
        wait_status(page, "browser", "on")
        page.wait("window.__capsSeen", what="浏览器那一行淡入")
    finally:
        page.js("window.__capsWatch && window.__capsWatch.disconnect()")
        browser_settings(page, True)
