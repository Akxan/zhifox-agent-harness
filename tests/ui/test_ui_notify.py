"""系统通知（page/scripts/notify.js）：页面在后台时，等你确认、一轮回复完了、出错了弹一个系统通知，点它回到那张卡片；
标签页的标题前加标记、图标上加一个点，回到页面就清掉；刷新以后重放出来的不弹；设置里的开关和授权；三种语言下通知的文字。

通知和"页面在后台"都是假的（steps.fake_notifications）：不弹真的系统通知，也不真的把窗口藏起来。
页面在眼前时"不弹"是当场决定的（不等什么），所以卡片、回复一出现，就能断定它没有弹。
"""

from __future__ import annotations

import re
from urllib.parse import unquote

import pytest

from . import checks, steps

FAVICON = "document.querySelector('link[rel=icon]').getAttribute('href')"
ALLOW = "#log .confirm:not(.done) .button.primary"
CJK = re.compile(r"[㐀-鿿]")


def _words(page, code: str, text: str) -> str:
    """text 在 code 这种语言里的样子（中文就是它自己）。"""
    if code == "zh":
        return text
    return page.js(f"JSON.parse(document.getElementById('i18n').textContent).tables[{code!r}][{text!r}]")


def _title(page) -> str:
    return page.js("document.title")


def test_confirm_in_background_notifies_and_clicking_it_comes_back(page):
    steps.fake_notifications(page)
    plain_icon = page.js(FAVICON)
    steps.background(page)
    steps.send(page, "[ui:approve] 后台时要我确认")
    steps.pending_confirm(page)
    page.wait("window.__notes.length === 1", what="弹了一个通知")
    [note] = steps.notes(page)
    assert note["title"] == "智狐在等你确认"
    # 正文是程序给的那句话（空行和缩进收掉了）
    assert note["body"] == "执行命令：\n$ echo zhifox-ui-test"
    assert note["urgent"] and note["tag"].startswith("harness-confirm-")
    # 标签页：标题前写着"等你确认"，图标右上角多了一个点（还是内嵌的 data: 图标，页面的安全策略不用放宽）
    assert _title(page).startswith("● 等你确认 · ")
    dotted = page.js(FAVICON)
    assert dotted != plain_icon and dotted.startswith("data:image/svg+xml,") and "<circle" in unquote(dotted)

    # 点通知：窗口切回来（window.focus），通知收掉，标题和图标恢复原样，那张卡片再响一次它的环
    page.js("document.querySelector('#log .confirm').classList.remove('alert'); window.__notes[0].onclick({ preventDefault() {} })")
    assert page.js("window.__focused") == 1
    page.wait("window.__notes[0].closed && !document.title.startsWith('●')", what="通知收掉、标题恢复")
    assert page.js(FAVICON) == plain_icon
    assert page.js("document.querySelector('#log .confirm').classList.contains('alert')")

    # 回到眼前以后答复、这一轮结束：不再弹
    page.click(ALLOW)
    steps.reply(page, "命令执行完了")
    assert len(steps.notes(page)) == 1
    assert not _title(page).startswith("●")


def test_confirm_in_foreground_does_not_notify_but_the_tab_is_marked_when_you_leave(page):
    steps.fake_notifications(page)
    steps.send(page, "[ui:approve] 就在眼前")
    steps.pending_confirm(page)
    assert steps.notes(page) == []
    assert not _title(page).startswith("●")
    # 卡片还在等的时候切到别处：标签页上标出"等你确认"，但不补弹通知（卡片出现的时候你看见了）
    steps.background(page)
    assert _title(page).startswith("● 等你确认 · ")
    assert steps.notes(page) == []
    steps.background(page, False)
    assert not _title(page).startswith("●")
    page.click(ALLOW)
    steps.reply(page, "命令执行完了")
    assert steps.notes(page) == []


def test_replayed_cards_and_replies_do_not_notify(page):
    # 刷新以后的页面一开始就在后台：重放出来的东西不弹
    steps.fake_notifications(page, keep=True, hidden=True)
    steps.send(page, "[ui:approve] 刷新以后")
    steps.pending_confirm(page)
    page.reload()
    page.wait("document.body.classList.contains('ready') && document.querySelector('#log .confirm:not(.done)')", what="重放完")
    assert page.js("document.hidden") is True
    assert steps.notes(page) == []
    # 那张卡片真的还在等你：标题照现在的样子标上"等你确认"，不算新消息
    assert _title(page).startswith("● 等你确认 · ")

    # 重放完以后的事是实时的：答复以后这一轮在后台做完，照样弹"回复完了"
    page.click(ALLOW)
    steps.reply(page, "命令执行完了")
    page.wait("window.__notes.length === 1", what="弹了一个通知")
    assert steps.notes(page)[0]["title"] == "智狐回复完了"

    # 再刷新一次：做完的那一轮整段重放出来，不弹，标题也不标
    page.reload()
    page.wait("document.body.classList.contains('ready') && document.querySelector('#log .confirm.done')", what="重放完")
    steps.reply(page, "命令执行完了")
    assert steps.notes(page) == []
    assert not _title(page).startswith("●")


def test_turn_end_in_background_notifies_with_the_start_of_the_reply(page):
    steps.fake_notifications(page)
    steps.background(page)
    steps.send(page, "[ui:hello] 后台回复")
    steps.reply(page, "收到第 1 条消息")
    page.wait("window.__notes.length === 1", what="弹了一个通知")
    [note] = steps.notes(page)
    assert note["title"] == "智狐回复完了"
    # 回复开头的一小段：画出来的文字（列表的每一项各占一行），不是 Markdown 原文，太长截短
    assert note["body"].startswith("收到第 1 条消息。")
    assert "\n第一点：" in note["body"] and len(note["body"]) <= 160
    assert note["tag"] == "harness-turn" and not note["urgent"]
    title = _title(page)
    assert title.startswith("● ") and "等你确认" not in title
    assert "<circle" in unquote(page.js(FAVICON))
    # 回到页面：通知收掉，标题恢复
    steps.background(page, False)
    page.wait("window.__notes[0].closed && !document.title.startsWith('●')", what="通知收掉、标题恢复")


def test_error_in_background_notifies_once(page):
    steps.fake_notifications(page)
    steps.background(page)
    steps.send(page, "[ui:flaky] 后台出错")
    page.wait("document.querySelector('#log .notice.error') && !document.body.classList.contains('busy')", 20, "出错、这一轮结束")
    # 出错的那一条说过了，这一轮结束时不再说"回复完了"
    [note] = steps.notes(page)
    assert note["title"] == "智狐出错了"
    shown = page.js("document.querySelector('#log .notice.error').textContent").strip()
    assert note["body"] and shown.startswith(note["body"].rstrip("…"))
    assert _title(page).startswith("● 出错了 · ")


def test_switching_it_off_stops_notifications_but_not_the_tab_marker(page):
    steps.fake_notifications(page)
    steps.open_dialog(page, "#settings-open", "settings")
    page.reach("#notify-enabled")
    assert page.js("document.getElementById('notify-enabled').getAttribute('aria-checked')") == "true"
    assert page.text("#notify-state") == "（开着）"
    page.click("#notify-enabled")
    page.wait("document.getElementById('notify-enabled').getAttribute('aria-checked') === 'false'", what="开关关上")
    assert page.text("#notify-state") == "（关着）"
    assert page.js("document.getElementById('notify-permission').hidden")
    assert page.js("localStorage.getItem('harness.notify')") == "off"
    steps.escape_closes(page, "settings")

    steps.background(page)
    steps.send(page, "[ui:hello] 关了通知")
    steps.reply(page, "收到第 1 条消息")
    assert steps.notes(page) == []
    assert _title(page).startswith("● ")  # 标签页上的标记不看这个开关

    # 记在这个浏览器里：刷新以后还是关着的
    page.reload()
    page.wait("document.body.classList.contains('ready')", what="页面亮相")
    steps.open_dialog(page, "#settings-open", "settings")
    assert page.js("document.getElementById('notify-enabled').getAttribute('aria-checked')") == "false"


@pytest.mark.parametrize("code", ["zh", "en", "es"])
def test_notification_words_follow_the_language(page, code):
    if code != "zh":
        steps.language(page, code)
    steps.fake_notifications(page)
    steps.background(page)
    steps.send(page, "[ui:approve] 换了语言")
    steps.pending_confirm(page)
    page.wait("window.__notes.length === 1", what="弹了一个通知")
    [asked] = steps.notes(page)
    assert asked["title"] == _words(page, code, "智狐在等你确认")
    command = _words(page, code, "执行命令：\n\n    $ {command}").split("\n")[0]
    assert asked["body"] == command + "\n$ echo zhifox-ui-test"
    assert _title(page).startswith("● " + _words(page, code, "等你确认") + " · ")
    page.click(ALLOW)
    steps.reply(page, "命令执行完了")
    page.wait("window.__notes.length === 2", what="又弹了一个")
    done = steps.notes(page)[1]
    assert done["title"] == _words(page, code, "智狐回复完了")
    if code != "zh":  # 除了"智狐"这个名字，通知上的话都翻译了（回复本身是模型写的中文，不算）
        assert not CJK.search(asked["title"].replace("智狐", "") + asked["body"] + done["title"].replace("智狐", ""))


def test_settings_asks_the_browser_and_explains_a_block(page):
    steps.fake_notifications(page, permission="default")
    steps.open_dialog(page, "#settings-open", "settings")
    page.reach("#notify-permission")
    assert page.text("#notify-state") == "（等浏览器允许）"
    assert "允许通知" in page.text("#notify-permission-text")
    # 浏览器只在你点了什么的时候才肯问：点"允许通知"
    page.click("#notify-ask")
    page.wait("window.__asked === 1 && !document.getElementById('notify-try').hidden", what="问过、允许了")
    assert page.text("#notify-state") == "（开着）"
    assert page.js("document.getElementById('notify-ask').hidden")
    assert page.js("document.getElementById('notify-permission').classList.contains('good')")
    assert "通知" in page.text("#notify-permission-more")  # 看不到通知时去系统的哪儿看（按这台电脑是什么系统写）
    # 发一条试试：页面就在眼前也弹
    page.click("#notify-try")
    page.wait("window.__notes.length === 1", what="试的那一条")
    assert steps.notes(page)[0]["title"] == "智狐的通知长这样"

    # 被浏览器拦下了：页面没法再问，写明去哪儿改，没有能按的按钮
    steps.fake_notifications(page, permission="denied")
    page.wait("document.getElementById('notify-permission').classList.contains('warn')", what="换成被拦下的样子")
    assert page.text("#notify-state") == "（被浏览器拦下了）"
    assert "刷新这个页面" in page.text("#notify-permission-text") and "Safari" in page.text("#notify-permission-text")
    assert page.js("document.getElementById('notify-permission-actions').hidden")
    # 关掉再打开：权限已经定了，不再去问
    page.click("#notify-enabled")
    page.click("#notify-enabled")
    assert page.js("window.__asked") == 1

    # 这个浏览器根本没有系统通知：开关点不了，标签页上的标记照样有
    page.js("delete window.Notification; document.dispatchEvent(new Event('visibilitychange'))")
    page.wait("document.getElementById('notify-enabled').disabled", what="开关变灰")
    assert page.text("#notify-state") == "（这个浏览器不支持）"
    assert page.js("document.getElementById('notify-enabled').getAttribute('aria-checked')") == "false"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_permission_notes_are_readable_in_both_themes(page, theme):
    page.js(f"localStorage.setItem('harness.theme', {theme!r})")
    page.reload()
    page.wait(f"document.body.classList.contains('ready') && document.documentElement.dataset.theme === {theme!r}", what="外观换好")
    steps.calm(page)
    steps.open_dialog(page, "#settings-open", "settings")
    for permission, kind in (("default", "ask"), ("granted", "good"), ("denied", "warn")):
        steps.fake_notifications(page, permission=permission)
        page.wait(f"document.getElementById('notify-permission').classList.contains('{kind}')", what=f"{kind} 的样子")
        for selector, ratio in checks.contrast(page, ["#notify-state", "#notify-permission-text", "#notify-permission-more"]):
            if selector == "#notify-permission-more" and kind != "good":
                continue  # 只有"允许了"的时候才有这一行
            assert ratio is not None and ratio >= 4.5, f"{theme}、{kind} 时 {selector} 的对比度只有 {ratio}"


def test_task_list_offer_asks_the_same_permission(page):
    steps.fake_notifications(page, permission="default")
    steps.nav_tab(page, "tasks")
    page.wait("!document.getElementById('task-notify').hidden", what="任务列表里的“开启系统通知”")
    page.click("#task-notify button")
    page.wait("window.__asked === 1 && document.getElementById('task-notify').hidden", what="问过、那句话收起来")
    # 问的是同一次授权：设置里那一节也变成"开着"
    steps.open_dialog(page, "#settings-open", "settings")
    assert page.text("#notify-state") == "（开着）"
    # 设置里关掉了系统通知：任务列表里也不再劝你开
    steps.fake_notifications(page, permission="default")
    page.reach("#notify-enabled")
    page.click("#notify-enabled")
    page.wait("document.getElementById('notify-enabled').getAttribute('aria-checked') === 'false'", what="开关关上")
    assert page.js("document.getElementById('task-notify').hidden")
    assert page.js("window.__asked") == 1
