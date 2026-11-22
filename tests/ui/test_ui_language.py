"""三种语言：在原地换（不刷新，对话、滚动位置、写了一半的话、开着的面板都留着），换完以后界面上不剩没翻译的中文。"""

from __future__ import annotations

import pytest

from . import checks, steps


def _translation(page, code: str, text: str) -> str:
    return page.js(f"JSON.parse(document.getElementById('i18n').textContent).tables[{code!r}][{text!r}]")


def test_switching_language_keeps_everything_in_place(page):
    steps.send(page, "[ui:long] 来一段很长的")
    steps.reply(page, "第 40 段")
    steps.open_side(page)
    # 往上翻一些（真的滚轮），离开最底下：这时"回到最新"的按钮会出现
    x, y = page.reach("#chat")
    page.call("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0, "deltaY": -500})
    page.wait("document.getElementById('to-bottom').classList.contains('show')", what="离开了最底下")
    page.wait("(() => { const top = document.getElementById('chat').scrollTop; return new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done(document.getElementById('chat').scrollTop === top)))); })()", what="滚动停下来")
    page.click("#input")
    page.type("写了一半的话")
    page.js("window.__alive = true; window.__first = document.querySelector('#log > *')")
    before = page.js("[document.getElementById('chat').scrollTop, document.querySelectorAll('#log > *').length]")

    for code in ("en", "es", "zh"):
        steps.language(page, code)
        assert page.js("window.__alive === true"), "换语言时页面刷新了"
        assert page.js("window.__first === document.querySelector('#log > *')"), "对话被重新画了一遍（应该就地换文字）"
        top, count = page.js("[document.getElementById('chat').scrollTop, document.querySelectorAll('#log > *').length]")
        assert count == before[1]
        assert abs(top - before[0]) <= 2, f"换成 {code} 以后滚动位置变了：{before[0]} → {top}"
        assert page.js("document.getElementById('to-bottom').classList.contains('show')")
        assert page.js("document.getElementById('input').value") == "写了一半的话"
        assert page.js("document.body.classList.contains('side-open')"), "开着的信息栏被收起来了"
        expected = "新对话" if code == "zh" else _translation(page, code, "新对话")
        assert page.text("#new span") == expected
        if code != "zh":
            assert checks.untranslated(page) == []


def _sweep(page) -> dict[str, list[str]]:
    """把界面上能打开的地方都打开看一遍，每处记下没翻译的中文。"""
    found: dict[str, list[str]] = {}

    def look(name: str, roots: list[str] | None = None) -> None:
        problems = checks.untranslated(page, roots)
        if problems:
            found[name] = problems

    # 对话里：工具卡片、确认卡片、"正在工作"
    steps.send(page, "[ui:approve] 看看确认卡片")
    steps.pending_confirm(page)
    look("对话（等你确认）")
    page.click("#log .confirm .button.primary")
    steps.reply(page, "命令执行完了")
    look("对话（答复以后）")
    # 这一轮的运行记录：合计、瀑布图（悬停一行，上面那一行字写着它的数字）、逐段的详情
    steps.open_trace(page)
    page.hover("#trace-body .trace-lane", 1)
    look("运行记录", ["#trace-dialog"])
    steps.escape_closes(page, "trace-dialog")

    # 编辑、重新生成（这一轮执行过命令，所以都要先确认）：重新生成之前的确认、编辑框、编辑框里的确认
    page.click("#log .turn-retry")
    page.wait("document.querySelector('#log .turn-confirm')")
    steps.idle(page)
    look("重新生成之前的确认")
    page.key("Escape")
    page.click("#input")
    page.key("ArrowUp")
    page.wait("document.activeElement.classList.contains('turn-editor-input')")
    steps.idle(page)
    look("编辑最后一条消息")
    page.key("Enter")
    page.wait("document.querySelector('.turn-editor .turn-confirm')")
    steps.idle(page)
    look("编辑框里的确认")
    page.key("Escape")  # 回到修改
    page.key("Escape")  # 没改过：直接关上
    page.wait("!document.querySelector('.turn-editor')", what="编辑框关上")

    steps.open_side(page)
    look("信息栏", ["#side"])
    steps.expand_capabilities(page)
    look("信息栏 · 智狐能做的事（全部点开）", ["#caps"])
    page.wait("!document.getElementById('spend').hidden", what="花费那一块出现（刚才那一轮记了账）")
    steps.open_dialog(page, "#spend-open", "spend-dialog")
    page.wait("document.getElementById('spend-body').children.length > 0")
    steps.idle(page)
    look("花费明细", ["#spend-dialog"])
    steps.escape_closes(page, "spend-dialog")
    steps.close_side(page)

    steps.open_dialog(page, "#settings-open", "settings")
    look("设置", ["#settings"])
    # 自定义模型接口：编辑窗口（叠在设置上面），测试连接以后的结果和猜出来的能力
    page.click("#endpoints-list > li.endpoint-row .endpoint-open")
    page.wait(steps.is_open("endpoint-dialog"), what="编辑接口的窗口打开")
    page.click("#endpoint-test")
    page.wait("document.getElementById('endpoint-result').classList.contains('good')", timeout=15)
    steps.idle(page)
    look("自定义模型接口（编辑）", ["#endpoint-dialog"])
    steps.escape_closes(page, "endpoint-dialog")
    page.demo.api("/api/commands/add", {"command": "npm test"})
    page.click("#commands-open")
    page.wait(f"{steps.is_open('commands-dialog')} && document.querySelector('#commands-list li.cmd-row')", what="不用问的命令窗口打开")
    steps.idle(page)
    look("不用问的命令", ["#commands-dialog"])
    page.hover("#commands-list li.cmd-row")
    page.click("#commands-list li.cmd-row .cmd-actions button:last-child")
    page.wait("document.querySelector('.cmd-confirm')")
    look("不用问的命令（删除确认）", ["#commands-dialog"])
    page.click("#commands-list .cmd-confirm button.danger")
    page.wait("!document.querySelector('#commands-list li.cmd-row')")
    steps.escape_closes(page, "commands-dialog")
    # 沙箱：设置里的那一节（选了一档），额外允许的路径的窗口（一条路径、原地编辑、删除确认）
    page.demo.api("/api/sandbox/level", {"level": "workspace"})
    page.demo.api("/api/sandbox/add", {"path": steps.SANDBOX_EXTRA, "write": True})
    steps.open_dialog(page, "#settings-open", "settings")
    page.wait("document.getElementById('sandbox-level').value === 'workspace'")
    look("设置 · 沙箱", ["#sandbox-section"])
    page.click("#sandbox-paths-open")
    page.wait(f"{steps.is_open('sandbox-dialog')} && document.querySelector('#sandbox-list li.sandbox-row')", what="沙箱的路径窗口打开")
    steps.idle(page)
    look("沙箱的路径", ["#sandbox-dialog"])
    page.hover("#sandbox-list li.sandbox-row")
    page.click("#sandbox-list li.sandbox-row .sandbox-actions button:nth-child(2)")
    page.wait("document.querySelector('.sandbox-edit')")
    look("沙箱的路径（编辑）", ["#sandbox-dialog"])
    page.key("Escape")
    page.wait("!document.querySelector('.sandbox-edit')")
    page.hover("#sandbox-list li.sandbox-row")
    page.click("#sandbox-list li.sandbox-row .sandbox-actions button:last-child")
    page.wait("document.querySelector('.sandbox-confirm')")
    look("沙箱的路径（删除确认）", ["#sandbox-dialog"])
    page.click("#sandbox-list .sandbox-confirm button.danger")
    page.wait("document.querySelector('#sandbox-list .sandbox-empty')")
    look("沙箱的路径（空的）", ["#sandbox-dialog"])
    steps.escape_closes(page, "sandbox-dialog")
    page.demo.api("/api/sandbox/level", {"level": "off"})
    steps.open_dialog(page, "#approval", "approval-dialog")
    look("要不要先问你", ["#approval-dialog"])
    page.click("#approval-options .option[data-mode='all']")
    look("要不要先问你（警告）", ["#approval-dialog"])
    steps.escape_closes(page, "approval-dialog")
    page.click("#theme")
    page.wait("document.getElementById('appearance').matches(':popover-open')")
    look("外观", ["#appearance"])
    page.key("Escape")
    steps.open_flyout(page, "#language", "languages")
    look("语言", ["#languages"])  # "中文"这个选项照原样写（每种语言用它自己的文字），在 checks.ALLOWED_WORDS 里
    steps.escape_closes_flyout(page, "languages", "#language")
    steps.open_models(page)
    look("换模型", ["#models"])
    page.click("#models-menu [role=menuitemradio][data-model='claude-sonnet-5-5']")  # 同一家：马上换，底下写一句
    page.wait("document.getElementById('models-status').classList.contains('good')", what="换好了")
    look("换模型（换好以后）", ["#models"])
    steps.escape_closes_flyout(page, "models", "#model")

    for tab in ("skills", "mcp", "channels", "agents", "hooks"):
        steps.extensions_tab(page, tab)
        look(f"扩展 · {tab}", ["#extensions"])
    # 钩子：一行、打开一个（试运行的结果，包括"它说了、但这里不照办的"）、编辑的表单
    steps.save_hook(page, "allow-all", "import json, sys\nsys.stdin.read()\nprint(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'allow'}, 'systemMessage': 'hello'}))\n")
    steps.extensions_tab(page, "hooks")
    page.wait("document.querySelector('#panel-hooks li.hooks-row')")
    page.hover("#panel-hooks li.hooks-row")
    look("扩展 · 钩子（一行）", ["#extensions"])
    steps.open_hook(page, "allow-all")
    page.click("#panel-hooks [data-focus='run']")
    page.wait("document.querySelector('#panel-hooks .hooks-result')")
    look("扩展 · 钩子（试运行）", ["#extensions"])
    page.click("#panel-hooks [data-focus='edit']")
    page.wait("document.getElementById('hooks-f-name')")
    look("扩展 · 钩子（编辑）", ["#extensions"])
    page.click("#hooks-f-cancel")
    page.wait("document.querySelector('#panel-hooks .hooks-test')")
    page.key("Escape")  # 在下一层：Esc 先回到列表，再按一次才关上对话框
    page.wait("document.querySelector('#panel-hooks li.hooks-row')", what="回到钩子的列表")
    steps.escape_closes(page, "extensions")
    # 从其他工具导入：三页各一个（顶上的总览、每一组、换写法的提醒、点开的"没找到的"）
    for tab in ("mcp", "skills", "agents"):
        steps.import_view(page, tab)
        look(f"扩展 · {tab} · 从其他工具导入", ["#extensions"])
    steps.close_extensions(page)

    steps.open_nav(page)
    steps.open_dialog(page, "#memory-open", "memory-dialog")
    page.wait("document.querySelectorAll('#memories > li').length > 0")
    look("记忆", ["#memory-dialog"])
    page.click("#memories > li .memory-open")
    page.wait("document.getElementById('memory-text').textContent.length > 0")
    look("记忆（看一条）", ["#memory-dialog"])
    steps.escape_closes(page, "memory-dialog")

    steps.nav_tab(page, "tasks")
    look("定时任务", ["#nav"])
    page.click("#task-list > li.kind-agent .task-main")
    page.wait("document.getElementById('task-detail').open")
    steps.idle(page)
    look("任务详情", ["#task-detail"])
    steps.escape_closes(page, "task-detail")
    steps.open_dialog(page, "#task-new", "task-editor")
    look("新建任务", ["#task-editor"])
    steps.escape_closes(page, "task-editor")

    steps.nav_tab(page, "media")
    look("多媒体", ["#nav"])
    page.click("#media-list button[class*='open']")
    page.wait("document.getElementById('viewer').open")
    steps.idle(page)
    look("查看器", ["#viewer"])
    steps.escape_closes(page, "viewer")
    steps.nav_tab(page, "chats")
    return found


@pytest.mark.parametrize("code", ["en", "es"])
def test_no_untranslated_chinese_anywhere(page, code):
    steps.calm(page)
    steps.language(page, code)
    found = _sweep(page)
    assert found == {}, "这些地方还有没翻译的中文（真是用户的内容的话，把选择器加进 checks.USER_CONTENT 或 NAMES）：\n" + "\n".join(
        f"{where}:\n  " + "\n  ".join(items) for where, items in found.items()
    )
