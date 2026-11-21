"""“要不要先问你”的几档，和“不用问的命令”窗口。

  - 对话里的开关、定时任务的编辑器、聊天通道的向导，三处列的是同一套：同样的几档、同样的顺序、同样的名字和说明
    （都照着 scripts/approval-modes.js 画），新加的"自动允许只读命令"三处都能选。
  - 选了它以后，对话里只读的命令不弹确认卡片，工具卡片上写着"自动允许（只读）"。
  - "不用问的命令"窗口：加、改、删都在原地；不合格的写在窗口里（窗口开着时，浮在页面上的提示会被暗底挡住）。
"""

from __future__ import annotations

import json

from fake_telegram import TOKEN

from . import steps
from .test_ui_channel_rows import _phone_says

MODES = ["ask", "readonly", "edits", "all"]


def _choices(page, selector: str, attribute: str) -> list[list[str]]:
    """一组选项里每一项的（是哪一档，名字，说明）。"""
    return page.js(
        f"[...document.querySelectorAll({json.dumps(selector)})].map((button) => [button.dataset.{attribute},"
        " button.querySelector('b').textContent, button.querySelector('span').textContent])"
    )


def test_the_three_selectors_list_the_same_modes_in_the_same_order(page):
    steps.calm(page)
    steps.open_dialog(page, "#approval", "approval-dialog")
    chat = _choices(page, "#approval-options .option", "mode")
    steps.escape_closes(page, "approval-dialog")

    steps.nav_tab(page, "tasks")
    steps.open_dialog(page, "#task-new", "task-editor")
    task = _choices(page, "#task-approval-options .task-approval-option", "approval")
    steps.escape_closes(page, "task-editor")
    steps.close_nav(page)

    status, _ = page.demo.api("/api/channel/connect", {"platform": "telegram", "token": TOKEN})
    assert status == 200
    try:
        status, paired = page.demo.api("/api/channel/pair", {"platform": "telegram"})
        assert status == 200
        _phone_says(page, paired["code"])
        steps.extensions_tab(page, "channels")
        page.wait("document.querySelector('#panel-channels li.chan-row .chan-meta') && !/配对/.test(document.querySelector('#panel-channels li.chan-row .chan-line').textContent)", timeout=20, what="配对好了")
        page.click("#panel-channels li.chan-row .chan-open")
        page.wait("document.querySelectorAll('#panel-channels .chan-mode').length > 0", what="向导的第 4 步：确认方式")
        channel = _choices(page, "#panel-channels .chan-mode", "mode")

        assert [mode for mode, _, _ in chat] == MODES, chat
        assert chat == task == channel, "三处列的几档要一模一样"
        assert chat[1][1] == "自动允许只读命令" and "ls" in chat[1][2] and "仍然每次问" in chat[1][2]

        # 在向导里选它、点"完成"：通道那一行写着这一档
        page.click("#panel-channels .chan-mode[data-mode='readonly']")
        page.wait("document.querySelector(\"#panel-channels .chan-mode[data-mode='readonly']\").getAttribute('aria-checked') === 'true'")
        page.click("#panel-channels [data-focus='done']")
        page.wait("(document.querySelector('#panel-channels li.chan-row .chan-meta') || {}).textContent.includes('自动允许只读命令')", what="通道那一行写着这一档")
    finally:
        page.demo.api("/api/channel/remove", {"platform": "telegram"})


def test_read_only_commands_run_without_a_card_and_say_why(page):
    steps.open_dialog(page, "#approval", "approval-dialog")
    page.click("#approval-options .option[data-mode='readonly']")  # 不像"全部自动允许"那样要再确认一步
    page.wait("!document.getElementById('approval-dialog').open && document.getElementById('approval').className === 'approval readonly'", what="换成只读这一档")
    assert page.text("#approval-label") == "自动允许只读命令"
    steps.send(page, "[ui:approve] 跑一条只读的命令")  # 剧本里的命令是 echo，只是打印
    steps.reply(page, "命令执行完了")
    assert page.js("document.querySelector('#log .confirm')") is None, "只读的命令不该弹确认卡片"
    assert page.text("#log .call .auto-tag") == "自动允许（只读）"
    # 刷新以后（重放保存的事件）照样写着为什么
    page.reload()
    page.wait("document.body.classList.contains('ready') && document.querySelector('#log .call .auto-tag')", what="重放完")
    assert page.text("#log .call .auto-tag") == "自动允许（只读）"


def test_a_task_can_choose_the_readonly_mode(page):
    steps.nav_tab(page, "tasks")
    steps.open_dialog(page, "#task-new", "task-editor")
    page.click("#task-editor [data-kind='agent']")
    page.click("#task-title")
    page.type("每天看一眼服务器")
    page.click("#task-prompt")
    page.type("看看磁盘和服务的状态")
    page.click("#task-approval-options [data-approval='readonly']")
    assert page.js("document.querySelector(\"#task-approval-options [data-approval='readonly']\").getAttribute('aria-checked')") == "true"
    assert page.js("document.getElementById('task-approval-warning').hidden"), "只有全部放开才有那条红色的警告"
    page.click("#task-save")
    page.wait("!document.getElementById('task-editor').open", what="保存以后窗口关上")
    row = "[...document.querySelectorAll('#task-list > li.task')].find((row) => row.textContent.includes('每天看一眼服务器'))"
    page.wait(f"{row} && {row}.querySelector('.task-mode.readonly')", what="任务那一行带着这一档的标签")
    task_id = page.js(f"{row}.dataset.id")
    try:
        assert page.js(f"{row}.querySelector('.task-mode').textContent") == "自动允许只读命令"
        page.click(f"#task-list > li.task[data-id='{task_id}'] .task-main")
        page.wait("document.getElementById('task-detail').open")
        assert "只是看的命令和读文件直接做" in page.text("#task-detail-body")
        steps.escape_closes(page, "task-detail")
    finally:
        page.demo.api("/api/task/delete", {"id": task_id})


def _open_commands(page) -> None:
    steps.open_dialog(page, "#settings-open", "settings")
    page.click("#commands-open")
    page.wait("document.getElementById('commands-dialog').open && !document.getElementById('settings').open", what="不用问的命令窗口打开")
    steps.idle(page)


def test_commands_are_added_edited_and_removed_in_place(page):
    _open_commands(page)
    assert page.text("#commands-list") == "还没有。比如可以加上 npm test、make check。"
    assert page.js("document.activeElement.id") == "commands-new"  # 打开时焦点就在添加框里

    # 不合格的：原因写在窗口里（助手程序回答 409，这个测试里是故意的）
    page.allow_errors("/api/commands/add")
    page.type("sudo reboot")
    page.key("Enter")
    page.wait("document.getElementById('commands-message').textContent.includes('能运行任何命令')", what="不许加的原因写在窗口里")
    assert page.js("document.getElementById('commands-message').classList.contains('bad')")
    assert page.js("document.querySelectorAll('#commands-list li.cmd-row').length") == 0

    # 加一条
    page.js("document.getElementById('commands-new').value = ''")
    page.click("#commands-new")
    page.type("npm test")
    page.click("#commands-add")
    page.wait("document.querySelector('#commands-list li.cmd-row .cmd-text') && document.querySelector('#commands-list li.cmd-row .cmd-text').textContent === 'npm test'", what="列表里出现这一条")
    assert "加好了" in page.text("#commands-message") and page.js("document.getElementById('commands-new').value") == ""
    page.wait("document.getElementById('commands-state').textContent.includes('1')", what="设置里那一节写着有几条")
    try:
        # 在原地改：改了一半按 Esc，先提醒；再按一次才放弃
        page.hover("#commands-list li.cmd-row")
        page.click("#commands-list li.cmd-row .cmd-actions button:nth-child(2)")
        page.wait("document.activeElement.classList.contains('cmd-edit-field')", what="编辑框拿到焦点")
        page.type(" -- --watch")
        page.key("Escape")
        page.wait("document.getElementById('commands-message').textContent.includes('还有没保存的修改')", what="先提醒")
        assert page.js("document.getElementById('commands-dialog').open") and page.js("Boolean(document.querySelector('.cmd-edit-field'))")
        page.key("Escape")
        page.wait("!document.querySelector('.cmd-edit-field') && document.getElementById('commands-dialog').open", what="放弃修改，窗口还开着")
        assert page.text("#commands-list .cmd-text") == "npm test"
        # 再改一次，按回车保存
        page.hover("#commands-list li.cmd-row")
        page.click("#commands-list li.cmd-row .cmd-actions button:nth-child(2)")
        page.wait("document.activeElement.classList.contains('cmd-edit-field')")
        page.js("document.activeElement.select()")
        page.type("npm run lint")
        page.key("Enter")
        page.wait("(document.querySelector('#commands-list .cmd-text') || {}).textContent === 'npm run lint'", what="改好了")
        # 删除：原地确认，Esc 取消，再点一次真的删
        page.hover("#commands-list li.cmd-row")
        page.click("#commands-list li.cmd-row .cmd-actions button:last-child")
        page.wait("document.activeElement.closest('.cmd-confirm')", what="焦点移进删除确认")
        page.key("Escape")
        page.wait("!document.querySelector('.cmd-confirm') && document.getElementById('commands-dialog').open", what="Esc 取消删除")
        page.hover("#commands-list li.cmd-row")
        page.click("#commands-list li.cmd-row .cmd-actions button:last-child")
        page.click("#commands-list .cmd-confirm button.danger")
        page.wait("document.querySelectorAll('#commands-list li.cmd-row').length === 0", what="删掉了")
    finally:
        page.demo.api("/api/commands/delete", {"command": "npm test"})
        page.demo.api("/api/commands/delete", {"command": "npm run lint"})


def test_the_approval_dialog_leads_to_the_commands(page):
    steps.open_dialog(page, "#approval", "approval-dialog")
    page.click("#approval-commands")
    page.wait("document.getElementById('commands-dialog').open && !document.getElementById('approval-dialog').open", what="从这里也能打开")
    steps.escape_closes(page, "commands-dialog")  # 什么都没改：一次就关上
