"""编辑最后一条消息、重新生成最后一个回答（页面里是 harness/webui/page/scripts/rewind.js，背后是 harness/rewind.py）。

在真的页面上点：按钮在哪儿、点得到；原地的编辑框（↑ 打开、Esc 先提醒、Enter 发送）；这一轮执行过命令时先在原地确认；
倒回去以后整段重画、刷新重放还是那个样子；另一个标签页跟着变，它改到一半的字放回它自己的输入框。
服务器那一侧（请求的开头一字不差、事件流、会话文件、索引）在 tests/test_rewind_web.py 里测。
"""

from __future__ import annotations

import json

from . import steps

# 对话里看得见的样子：每条用户消息的文字、每个回答的开头。比较倒回去前后、两个标签页、刷新前后用
SHAPE = (
    "JSON.stringify({ users: [...document.querySelectorAll('#log > .msg.user')].map((n) => n.textContent),"
    " replies: [...document.querySelectorAll('#log > .msg.assistant .msg-text')].map((n) => n.textContent.slice(0, 40)),"
    " calls: document.querySelectorAll('#log .call').length })"
)
IDLE_TURN = "!document.body.classList.contains('busy') && document.querySelector('#log .turn-retry')"


def _shape(page) -> dict:
    return json.loads(page.js(SHAPE))


def _wait_turn(page, what: str) -> None:
    """等新的一轮开始、再结束（按钮重新摆好）。"""
    page.wait("document.body.classList.contains('busy')", 10, what + "开始了")
    page.wait(IDLE_TURN, 20, what + "结束了")


def test_regenerate_and_edit_the_last_turn_in_place(page):
    steps.send(page, "[ui:hello] 你好")
    steps.reply(page, "收到第 1 条消息")
    page.wait(IDLE_TURN, what="按钮摆好")
    # "重新生成"在最后一个回答下面那一排里，一直看得见；"编辑"浮在最后一条消息左边，悬停才出现、不占位置
    assert page.js("document.querySelector('#log .msg.assistant.turn-last .msg-actions .turn-retry') !== null")
    page.wait("getComputedStyle(document.querySelector('#log .msg.assistant.turn-last .msg-actions')).opacity === '1'", what="最后一个回答下面那排按钮一直亮着")
    width = page.js("document.querySelector('#log > .msg.user').offsetWidth")  # 排版的宽度（不受入场动画的缩放影响）
    page.hover("#log > .msg.user")
    page.wait("getComputedStyle(document.querySelector('.turn-edit-open')).opacity === '1'", what="悬停时编辑按钮出现")
    assert page.js("document.querySelector('#log > .msg.user').offsetWidth") == width  # 没有把消息挤动

    # 重新生成：同一条消息，模型再答一次；对话里还是一问一答
    page.click("#log .turn-retry")
    _wait_turn(page, "重新生成")
    shape = _shape(page)
    assert shape["users"] == ["[ui:hello] 你好"] and len(shape["replies"]) == 1 and "收到第 1 条消息" in shape["replies"][0]

    # 输入框空着时按 ↑：最后一条消息原地变成编辑框，焦点在里面，光标在最后
    page.click("#input")
    page.key("ArrowUp")
    page.wait("document.activeElement.classList.contains('turn-editor-input')", what="编辑框打开、拿到焦点")
    assert page.js("document.activeElement.value") == "[ui:hello] 你好"
    assert page.js("document.activeElement.selectionStart === document.activeElement.value.length")
    assert page.js("document.querySelector('#log > .msg.user').hidden")
    # 改了以后按 Esc：先提醒一次，再按一次才放弃；放弃以后消息原样回来，焦点回到"编辑"上
    page.type("，还有一句")
    page.key("Escape")
    page.wait("document.querySelector('.turn-editor-note.warn')", what="提醒还没发送")
    assert page.exists(".turn-editor")
    page.key("Escape")
    page.wait("!document.querySelector('.turn-editor')", what="编辑框关上")
    assert page.js("!document.querySelector('#log > .msg.user').hidden && document.querySelector('#log > .msg.user').textContent") == "[ui:hello] 你好"
    assert page.js("document.activeElement.classList.contains('turn-edit-open')")
    # 没改就按 Esc：直接关上
    page.key("Enter")
    page.wait("document.activeElement.classList.contains('turn-editor-input')", what="在编辑按钮上按回车也能打开")
    page.key("Escape")
    page.wait("!document.querySelector('.turn-editor')", what="没改过：一次就关上")

    # 改好了按 Enter：换掉最后一条消息，当作新的一轮发出去
    page.click("#input")
    page.key("ArrowUp")
    page.wait("document.activeElement.classList.contains('turn-editor-input')")
    page.js("document.activeElement.select()")
    page.type("[ui:hello] 改过的问题")
    page.key("Enter")
    steps.reply(page, "收到第 1 条消息")
    page.wait(IDLE_TURN, what="编辑以后的一轮结束")
    edited = _shape(page)
    assert edited["users"] == ["[ui:hello] 改过的问题"] and len(edited["replies"]) == 1

    # 刷新：重放出来的和刚才一样，不播入场动画
    page.reload()
    page.wait("document.body.classList.contains('ready') && " + IDLE_TURN, what="重放完")
    assert _shape(page) == edited
    assert page.js("document.querySelectorAll('#log .enter').length") == 0


def test_a_turn_that_ran_a_command_asks_before_rewinding(page):
    steps.send(page, "[ui:approve] 执行一条命令")
    steps.pending_confirm(page)
    page.click("#log .confirm .button.primary")
    steps.reply(page, "命令执行完了")
    page.wait(IDLE_TURN)

    # 重新生成：先在原地列出这一轮做过的事、说清楚撤不回来；焦点在"仍然重新生成"上，Esc 收起来、焦点回到按钮上
    page.click("#log .turn-retry")
    page.wait("document.activeElement && document.activeElement.closest('.turn-confirm')", what="确认卡片出现、拿到焦点")
    assert page.js("[...document.querySelectorAll('.turn-confirm-list li')].map((li) => li.textContent)") == ["run_command $ echo zhifox-ui-test"]
    assert page.text(".turn-confirm-head") == "倒回去的只是对话"
    assert not page.js("document.body.classList.contains('busy')")  # 还没做
    page.key("Escape")
    page.wait("!document.querySelector('.turn-confirm')", what="确认收起来")
    assert page.js("document.activeElement.classList.contains('turn-retry')")

    # 确认以后才做：同样的消息再发一次，模型又要执行那条命令（照样先问你）
    page.click("#log .turn-retry")
    page.wait("document.querySelector('.turn-confirm')")
    page.click(".turn-confirm .button.primary")
    steps.pending_confirm(page)
    page.click("#log .confirm:not(.done) .button.primary")
    steps.reply(page, "命令执行完了")
    page.wait(IDLE_TURN)
    assert _shape(page)["users"] == ["[ui:approve] 执行一条命令"] and page.js("document.querySelectorAll('#log .confirm').length") == 1

    # 编辑：确认放在编辑框里，替掉下面那排按钮；Esc 回到修改，焦点回到输入框
    page.click("#input")
    page.key("ArrowUp")
    page.wait("document.activeElement.classList.contains('turn-editor-input')")
    page.type("，再来一次")
    page.key("Enter")
    page.wait("document.querySelector('.turn-editor .turn-confirm') && document.activeElement.closest('.turn-confirm')", what="编辑框里的确认")
    assert page.js("document.querySelector('.turn-editor-foot').hidden")
    page.key("Escape")
    page.wait("!document.querySelector('.turn-editor .turn-confirm') && document.activeElement.classList.contains('turn-editor-input')", what="回到修改")
    page.key("Enter")
    page.wait("document.querySelector('.turn-editor .turn-confirm')")
    page.click(".turn-editor .turn-confirm .button.primary")
    steps.pending_confirm(page)
    page.click("#log .confirm:not(.done) .button.primary")
    steps.reply(page, "命令执行完了")
    page.wait(IDLE_TURN)
    assert _shape(page)["users"] == ["[ui:approve] 执行一条命令，再来一次"]


def test_another_tab_follows_and_keeps_its_unsent_edit(page, browser):
    steps.send(page, "[ui:hello] 两个标签页")
    steps.reply(page, "收到第 1 条消息")
    page.wait(IDLE_TURN)
    other = browser.new_page()
    try:
        other.goto(page.demo.url)
        other.wait("document.body.classList.contains('ready') && " + IDLE_TURN, what="另一个标签页画好")
        # 另一个标签页正在改这条消息，改到一半
        other.click("#input")
        other.key("ArrowUp")
        other.wait("document.activeElement.classList.contains('turn-editor-input')")
        other.type("（改到一半）")
        # 这边重新生成：那边的编辑框收起来，改了的字放回它的输入框，对话和这边一样
        page.click("#log .turn-retry")
        _wait_turn(page, "重新生成")
        other.wait("!document.querySelector('.turn-editor') && " + IDLE_TURN, what="另一个标签页跟着重画")
        assert other.js("document.getElementById('input').value") == "[ui:hello] 两个标签页（改到一半）"
        assert other.text("#toast") == "对话刚刚变了，改到一半的消息放回了下面的输入框。"
        assert _shape(other) == _shape(page)
        assert other.unexpected_errors() == []
    finally:
        other.close()


def test_a_turn_that_failed_gets_its_own_regenerate_below_the_error(page):
    # 这一轮在模型答出第一步之前就失败了（演示的 [ui:flaky] 第一次一定失败）：没有回答，"重新生成"单独放在出错提示下面；
    # 点它就是同样的话再发一次，消息不会在对话里出现两遍
    steps.send(page, "[ui:flaky] 第一次会失败")
    page.wait("!document.body.classList.contains('busy') && document.querySelector('#log .notice.error + .turn-actions .turn-retry')", 20, "出错提示下面单独一排的重新生成")
    assert not page.exists("#log .msg.assistant")
    page.click("#log .turn-actions .turn-retry")
    steps.reply(page, "这次成功了（第 1 条消息）")
    page.wait(IDLE_TURN)
    assert _shape(page)["users"] == ["[ui:flaky] 第一次会失败"]
    assert not page.exists("#log .turn-actions") and not page.exists("#log .notice.error")
