"""每一样存在本地的东西，一行的交互都一样；能编辑的窗口，有没保存的修改时关之前先问。

CLAUDE.md 的规矩："会话、记忆、待办、定时任务、Skill、MCP 服务器……每一行悬停时右边是'在访达中显示 / 编辑 / 删除'
三个按钮（同样的图标、顺序和位置，触屏上一直显示），删除先在原地确认。"以后加了新的一种（比如别的什么列表），
在 ROWS 里加一行，它就会被同样地查一遍。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fake_telegram import TOKEN

from . import checks, steps


@dataclass
class Rows:
    name: str
    row: str  # 一行
    actions: str  # 一行里放按钮的那一格（平时透明，悬停时淡入）
    main: str  # 一行里点了就打开它的那个按钮（待办没有"打开"，是空的）
    hover: str = ""  # 鼠标要移到哪儿按钮才出现（默认就是这一行）


ROWS = [
    Rows("会话", "#conversation-rows .conv", ".conv-actions", ".conv-main"),
    Rows("待办", "#todos > li.todo", ".todo-actions", ""),
    Rows("定时任务", "#task-list > li.task", ".task-actions", ".task-main"),
    Rows("记忆", "#memories > li.memory", ".memory-actions", ".memory-open"),
    Rows("Skill", "#panel-skills li.skill-row", ".skill-actions", ".skill-open"),
    Rows("MCP 服务器", "#panel-mcp li.mcp-server", ".mcp-actions", ".mcp-open"),
    Rows("Agent", "#panel-agents li.agents-row", ".skill-actions", ".skill-open"),
    Rows("聊天通道", "#panel-channels li.chan-row", ".chan-actions", ".chan-open", hover=".chan-main"),
    Rows("不用问的命令", "#commands-list > li.cmd-row", ".cmd-actions", ""),
]


def _show(page, rows: Rows) -> None:
    """把这一种东西的列表打开。"""
    if rows.name in ("会话", "定时任务"):
        steps.nav_tab(page, "chats" if rows.name == "会话" else "tasks")
    elif rows.name == "待办":
        steps.open_side(page)
    elif rows.name == "记忆":
        steps.open_nav(page)
        steps.open_dialog(page, "#memory-open", "memory-dialog")
    elif rows.name == "不用问的命令":
        steps.open_dialog(page, "#model", "settings")
        page.click("#commands-open")
        page.wait(steps.is_open("commands-dialog"), what="不用问的命令窗口打开")
    else:
        tab = {"Skill": "skills", "MCP 服务器": "mcp", "Agent": "agents", "聊天通道": "channels"}[rows.name]
        steps.extensions_tab(page, tab)
    page.wait(f"document.querySelector({rows.row!r})", what=f"{rows.name}的列表")
    steps.idle(page)


def _hide(page) -> None:
    for dialog in ("memory-dialog", "extensions", "commands-dialog"):
        if page.js(steps.is_open(dialog)):
            steps.escape_closes(page, dialog)
    steps.close_side(page)


def _away(page) -> None:
    """把鼠标移到一个不在任何一行上的地方（窗口的标题、或者对话区的空白处）。"""
    for spot in ("dialog[open] .settings-head h2", "#title"):
        if page.js(f"Boolean(document.querySelector({spot!r}))"):
            page.mouse(*page.reach(spot))
            return


@pytest.fixture
def stocked(page):
    """每一种东西都至少有一行：加一条待办、接上一个（假的）Telegram。测试完把它们收掉。"""
    steps.send(page, "[ui:todo] 加一条待办")
    steps.reply(page, "记下了")
    status, _ = page.demo.api("/api/channel/connect", {"platform": "telegram", "token": TOKEN})
    assert status == 200
    assert page.demo.api("/api/commands/add", {"command": "npm test"})[0] == 200
    yield page
    page.demo.api("/api/channel/remove", {"platform": "telegram"})
    page.demo.api("/api/commands/delete", {"command": "npm test"})
    for todo in page.js("[...document.querySelectorAll('#todos > li.todo')].map((row) => Number(row.dataset.id))"):
        page.demo.api("/api/todo/delete", {"id": todo})


def test_every_local_item_has_reveal_edit_delete_on_hover_and_focus(stocked):
    page = stocked
    sizes: dict[str, str] = {}
    for rows in ROWS:
        _show(page, rows)
        assert checks.row_trio(page, rows.row) == [], f"{rows.name}：最后三个按钮应该依次是 在访达中显示 / 编辑 / 删除"
        actions = f"{rows.row} {rows.actions}"
        trash = f"{actions} button:last-child"
        # 平时看不见、点不到
        _away(page)
        page.wait(f"{checks.opacity_of(actions, upto=rows.row)} < 0.05", what=f"{rows.name}的按钮平时藏着")
        # 鼠标移上去：淡入，而且真的点得到（没有被别的东西挡住）
        page.hover(rows.hover and f"{rows.row} {rows.hover}" or rows.row)
        page.wait(f"{checks.opacity_of(actions, upto=rows.row)} > 0.95", what=f"{rows.name}的按钮在悬停时出现")
        page.reach(trash)
        # 按钮后面垫着一段渐变（让文字淡出去）。点在渐变上，应该还是打开这一项，不能是一块点了没反应的地方
        dead = rows.main and page.js(
            f"(() => {{ const bar = document.querySelector({actions!r}), first = [...bar.querySelectorAll('button')].find((b) => b.getClientRects().length);"
            "  const box = bar.getBoundingClientRect(), button = first.getBoundingClientRect();"
            "  if (button.left - box.left < 4) return null;"
            "  const hit = document.elementFromPoint(box.left + 2, button.top + button.height / 2);"
            "  return hit && bar.contains(hit) ? hit.className || hit.tagName : null; })()"
        )
        assert not dead, f"{rows.name}：按钮左边那段渐变挡住了点击（点到的是 {dead}）"
        # 移开以后又藏起来；用键盘移到这一行（焦点在行里）也会出现
        _away(page)
        page.wait(f"{checks.opacity_of(actions, upto=rows.row)} < 0.05")
        page.focus(trash)
        page.wait(f"{checks.opacity_of(actions, upto=rows.row)} > 0.95", what=f"{rows.name}的按钮在键盘焦点移进来时出现")
        page.js("document.activeElement.blur()")
        # 三个按钮在每一种里都一样大
        sizes[rows.name] = page.js(f"[...document.querySelectorAll({actions!r} + ' button')].slice(-3).map((b) => b.offsetWidth + '×' + b.offsetHeight).join(' ')")
        _hide(page)
    assert len(set(sizes.values())) == 1, f"每一种的三个按钮应该一样大：{sizes}"


@pytest.mark.parametrize("which", ["会话", "待办", "记忆", "不用问的命令"])
def test_delete_asks_in_place_and_escape_cancels(stocked, which):
    page = stocked
    rows = next(item for item in ROWS if item.name == which)
    _show(page, rows)
    count = page.js(f"document.querySelectorAll({rows.row!r}).length")
    page.hover(rows.row)
    page.click(f"{rows.row} {rows.actions} button:last-child")
    # 原地换成一句确认（不弹窗）：还在同一个列表里，那一行的位置上
    confirm = f"{rows.row.split(' ')[0]} [class*='confirm']"
    page.wait(f"document.querySelector({confirm!r})", what=f"{which}的删除确认出现在原地")
    # 焦点移进确认里（落在"取消"上），这时按 Esc 就是取消
    page.wait(f"document.activeElement.closest({confirm!r})", what="焦点移进删除确认")
    page.key("Escape")
    page.wait(f"!document.querySelector({confirm!r}) && document.querySelectorAll({rows.row!r}).length === {count}", what="Esc 取消删除")
    if which == "待办":  # 这条待办是测试自己加的：真的删一次，删掉以后列表回到空的样子
        page.hover(rows.row)
        page.click(f"{rows.row} {rows.actions} button:last-child")
        page.wait(f"document.activeElement.closest({confirm!r})")
        page.click(f"{confirm} button.danger")
        page.wait(f"document.querySelectorAll({rows.row!r}).length === {count - 1} && !document.querySelector({confirm!r})", what="待办删掉了")


# 能编辑的窗口：打开、改一点、然后用四种办法想关掉它。每一种都要先提醒一次，再来一次才真的关上
EDITORS = {
    "定时任务": {"open": ["#task-new"], "tab": "tasks", "dialog": "task-editor", "field": "#task-title", "close": "#close-task-editor", "cancel": "#task-cancel", "message": "#task-editor-message"},
    "提示词": {"open": ["#model", "#prompt-open"], "dialog": "prompt-dialog", "field": "#prompt-text", "close": "#close-prompt", "cancel": "#prompt-cancel", "message": "#prompt-message"},
    # 添加框里写了一半，就算有没保存的修改；"完成"在这里就是"取消"那个位置的按钮
    "不用问的命令": {"open": ["#model", "#commands-open"], "dialog": "commands-dialog", "field": "#commands-new", "close": "#close-commands", "cancel": "#commands-done", "message": "#commands-message"},
}
WARNING = "还有没保存的修改，再按一次就放弃。"


def _open_editor(page, editor: dict) -> None:
    if editor.get("tab"):
        steps.nav_tab(page, editor["tab"])
    for button in editor["open"]:
        page.click(button)
    page.wait(steps.is_open(editor["dialog"]), what=f"{editor['dialog']} 打开")
    steps.idle(page)


def _closed(page, editor: dict, what: str) -> None:
    page.wait(f"!{steps.is_open(editor['dialog'])}", what=what)
    steps.idle(page)
    if page.js(steps.is_open("settings")):  # 提示词的窗口是从设置里打开的：设置还开着，关掉它，下一次从头打开
        steps.escape_closes(page, "settings")


def _try_close(page, editor: dict, how: str) -> None:
    if how == "Esc":
        page.key("Escape")
    elif how == "焦点不在窗口里时按 Esc":  # 浏览器直接发 cancel，不经过窗口上的 keydown（见 CLAUDE.md 的"监听 cancel 做保险"）
        page.js("document.activeElement.blur()")
        page.key("Escape")
    elif how == "×":
        page.click(editor["close"])
    elif how == "取消":
        page.click(editor["cancel"])
    elif how == "点外面":
        page.click_at(4, 4)


@pytest.mark.parametrize("name", list(EDITORS))
def test_unsaved_edits_are_not_thrown_away_silently(page, name):
    editor = EDITORS[name]
    steps.calm(page)  # 这里查的是"关不关"，不是动画：几十次开关窗口，每次都等动画太慢
    for how in ("Esc", "焦点不在窗口里时按 Esc", "×", "取消", "点外面"):
        # 没改过：一次就关上
        _open_editor(page, editor)
        _try_close(page, editor, how)
        _closed(page, editor, f"{how}：没改过的时候一次就关上")
        # 改过：第一次只提醒（提醒写在窗口里，不是浮在页面上的 toast），第二次才关上
        _open_editor(page, editor)
        page.click(editor["field"])
        page.type("改了一半")
        _try_close(page, editor, how)
        page.wait(f"document.querySelector({editor['message']!r}).textContent.includes({WARNING!r})", what=f"{how}：提醒出现在窗口里")
        assert page.js(steps.is_open(editor["dialog"])), f"{how}：有没保存的修改，第一次不该关上"
        _try_close(page, editor, how)
        _closed(page, editor, f"{how}：再来一次就关上")


def test_memory_editor_asks_before_dropping_edits(page):
    steps.open_nav(page)
    steps.open_dialog(page, "#memory-open", "memory-dialog")
    page.click("#memories > li .memory-open")
    # 记忆的内容读回来之前，"编辑"按钮已经在了，只是不能点（disabled）：要等它能点再点，
    # 不然慢的机器上点下去什么都不会发生（GitHub 的 macOS 机器上这样失败过）
    page.wait("document.getElementById('memory-edit') && !document.getElementById('memory-edit').disabled", what="记忆读出来、编辑按钮能点")
    page.click("#memory-edit")
    page.wait("!document.getElementById('memory-editor').hidden")
    page.click("#memory-edit-text")
    page.type("改了一半")
    # Esc：先提醒，再按一次回到"看"的样子（窗口还开着）
    page.key("Escape")
    page.wait(f"document.getElementById('memory-foot').textContent.includes({WARNING!r}) || document.querySelector('.memory-body').textContent.includes({WARNING!r})", what="提醒出现在窗口里")
    assert not page.js("document.getElementById('memory-editor').hidden")
    page.key("Escape")
    page.wait("document.getElementById('memory-editor').hidden && document.getElementById('memory-dialog').open", what="回到看的样子")
    # 再改一次，按 ×：同样先提醒，再按一次才关窗口
    page.click("#memory-edit")
    page.wait("!document.getElementById('memory-editor').hidden")
    page.click("#memory-edit-text")
    page.type("又改了一点")
    page.click("#close-memory")
    page.wait("document.getElementById('memory-dialog').open && !document.getElementById('memory-editor').hidden")
    page.click("#close-memory")
    page.wait("!document.getElementById('memory-dialog').open", what="第二次 × 关上")
