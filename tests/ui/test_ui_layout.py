"""窄屏（390 像素宽，常见的手机）上，三种语言下，长长的标题、待办、任务名都不能冲出容器。

CLAUDE.md 的规矩："文字永远不能冲出容器……三种语言都要看（英文、西班牙语通常更长），窄屏也要看。"
检查本身在 checks.overflow 里：每一段看得见的文字，要么在它的框里，要么被省略号收住，要么在一个能横着滚动的框里。
新加的面板、窗口，在下面的 SURFACES 里加一项就会被查到。
"""

from __future__ import annotations

import pytest

from . import checks, steps
from .pilot import PHONE_WIDTH

LONG_MESSAGE = "[ui:todo] 这是一条故意写得特别长的消息，好让会话的标题和对话里的文字都长到放不下：" + "很长的标题" * 8
LONG_TASK = "每周五下午把这一周所有项目的进展、遇到的问题和下周的计划整理成一份周报并发给团队里的每一个人"


def _surfaces(page):
    """(名字, 打开它, 要查的范围, 查完要关上的窗口：窗口的 id，或者一个收起它的函数)。"""
    return [
        ("对话", lambda: None, [".stage"], None),
        # 运行记录：窄屏上从底下升起来，瀑布图变成一列（名字在上，时间轴在下）
        ("运行记录", lambda: steps.open_trace(page), ["#trace-dialog"], "trace-dialog"),
        ("会话栏", lambda: steps.nav_tab(page, "chats"), ["#nav"], None),
        ("定时任务", lambda: steps.nav_tab(page, "tasks"), ["#nav"], None),
        ("多媒体", lambda: steps.nav_tab(page, "media"), ["#nav"], None),
        ("信息栏", lambda: (steps.close_nav(page), steps.open_side(page)), ["#side"], None),
        ("信息栏 · 智狐能做的事（全部点开）", lambda: steps.expand_capabilities(page), ["#side"], None),
        ("设置", lambda: (steps.close_side(page), steps.open_dialog(page, "#settings-open", "settings")), ["#settings"], "settings"),
        ("设置 · 备用模型", lambda: _fallbacks(page), ["#settings"], None),
        # 语音转文字：选了"你自己运行的兼容服务"以后多出来的地址、模型名和那一行说明（左边一列名字，窄屏上改成上下两行）
        ("设置 · 语音转文字（你自己运行的服务）", lambda: steps.voice_local(page), ["#settings"], "settings"),
        # 顶栏上的小面板：窄屏上从底下升起来（见 page/scripts/flyouts.js）
        ("换模型", lambda: steps.open_models(page), ["#models"], lambda: steps.escape_closes_flyout(page, "models", "#model")),
        ("语言", lambda: steps.open_flyout(page, "#language", "languages"), ["#languages"], lambda: steps.escape_closes_flyout(page, "languages", "#language")),
        ("要不要先问你", lambda: steps.open_dialog(page, "#approval", "approval-dialog"), ["#approval-dialog"], "approval-dialog"),
        ("不用问的命令", lambda: _commands(page), ["#commands-dialog"], "commands-dialog"),
        ("沙箱的路径", lambda: _sandbox(page), ["#sandbox-dialog"], "sandbox-dialog"),
        # 从备份恢复的预览：很长的文件名（省略）、两张方式的卡片（窄屏上下排）、每一样会怎样、要重新填的密钥（JSON 里的位置折行）
        ("从备份恢复（预览）", lambda: _restore(page), ["#restore-dialog"], lambda: steps.close_restore(page)),
        # 第一下 Esc 只收起问的那一块（焦点在它里面），第二下才关上设置窗口
        ("设置 · 遮住密钥（关掉之前）", lambda: _redact(page), ["#redact-section"],
         lambda: (page.key("Escape"), page.wait("document.getElementById('redact-confirm').hidden"), steps.escape_closes(page, "settings"))),
        # 自定义模型接口的编辑窗口（叠在设置上面）：很长的地址、模型名，每个模型的三个开关和三个数字
        ("自定义模型接口（编辑）", lambda: _endpoint_editor(page), ["#endpoint-dialog"], lambda: (steps.escape_closes(page, "endpoint-dialog"), steps.escape_closes(page, "settings"))),
        ("扩展 · Skill", lambda: steps.extensions_tab(page, "skills"), ["#extensions"], None),
        ("扩展 · MCP", lambda: steps.extensions_tab(page, "mcp"), ["#extensions"], None),
        ("扩展 · 通道", lambda: steps.extensions_tab(page, "channels"), ["#extensions"], None),
        ("扩展 · Agent", lambda: steps.extensions_tab(page, "agents"), ["#extensions"], None),
        # 钩子：一行里很长的命令要省略、详情里要折行；试运行的输出、表单
        ("扩展 · 钩子", lambda: _hooks(page), ["#extensions"], None),
        ("扩展 · 钩子 · 试运行", lambda: _hook_tested(page), ["#extensions"], None),
        ("扩展 · 钩子 · 编辑", lambda: (page.click("#panel-hooks [data-focus='edit']"), page.wait("document.getElementById('hooks-f-name')")), ["#extensions"], None),
        # 从其他工具导入：很长的路径（~/Library/Application Support/Code/User/……）、换写法的提醒、"没找到的"都要折行
        ("扩展 · MCP · 从其他工具导入", lambda: steps.import_view(page, "mcp"), ["#extensions"], None),
        ("扩展 · Skill · 从其他工具导入", lambda: steps.import_view(page, "skills"), ["#extensions"], None),
        ("扩展 · Agent · 从其他工具导入", lambda: steps.import_view(page, "agents"), ["#extensions"], lambda: steps.close_extensions(page)),
        ("记忆", lambda: (steps.open_nav(page), steps.open_dialog(page, "#memory-open", "memory-dialog")), ["#memory-dialog"], "memory-dialog"),
        ("新建任务", lambda: (steps.nav_tab(page, "tasks"), steps.open_dialog(page, "#task-new", "task-editor")), ["#task-editor"], "task-editor"),
        # 这一轮加过待办（会改动东西）：重新生成之前先在原地确认；编辑框里按回车，确认放在编辑框里
        ("重新生成之前的确认", lambda: (steps.close_nav(page), _retry_confirm(page)), ["#log"], None),
        ("编辑最后一条消息", lambda: _edit_confirm(page), ["#log"], None),
    ]


def _fallbacks(page) -> None:
    """设置里选了两个备用模型：一行一个，序号、模型名、两个按钮，窄屏上模型名省略、不冲出去。"""
    page.demo.api("/api/fallbacks", {"provider": "anthropic", "models": ["claude-sonnet-5-5", "claude-fable-5-2"]})
    steps.open_dialog(page, "#settings-open", "settings")
    page.wait("document.querySelectorAll('#fallback-list .fallback-row').length === 2", what="备用模型的名单画出来")


def _restore(page) -> None:
    """从备份恢复：选了一个文件名很长的备份，备份里还有一个密钥要重新填的 MCP 服务器。只上传、不恢复。"""
    steps.open_restore(page)
    steps.choose_backup(page, steps.backup_bytes(page.demo, missing_key=True), "zhifox-backup-20261120-0930-from-the-old-laptop-before-moving-to-the-new-one.zip")


def _commands(page) -> None:
    """"不用问的命令"窗口，里面有一条很长的命令（命令要整条看得到，所以折行，不能冲出去）。"""
    page.demo.api("/api/commands/add", {"command": "./scripts/check-everything-before-release.sh --with-a-very-long-option-name --and-another-one=value"})
    steps.open_dialog(page, "#approval", "approval-dialog")
    page.click("#approval-commands")
    page.wait(f"{steps.is_open('commands-dialog')} && document.querySelector('#commands-list li.cmd-row')", what="不用问的命令窗口打开")


def _redact(page) -> None:
    """设置里"遮住密钥"一节，关掉之前问的那一块展开着（警告的话和两个按钮，窄屏上按钮折到下一行）。"""
    steps.open_dialog(page, "#settings-open", "settings")
    page.reach("#redact-enabled")
    page.click("#redact-enabled")
    page.wait("!document.getElementById('redact-confirm').hidden", what="关掉之前先问")


def _hooks(page) -> None:
    """"扩展 · 钩子"，里面有一个命令很长的钩子。"""
    steps.save_hook(page, "a-hook-with-a-rather-long-name-for-narrow-screens", "import sys\nsys.stdin.read()\nprint('x' * 300, file=sys.stderr)\n",
                    extra=("--with-a-very-long-option-name", "--and-another-one=value", "/a/very/long/path/that/goes/on/and/on/hooks/check-everything.py"))
    steps.extensions_tab(page, "hooks")
    page.wait("document.querySelector('#panel-hooks li.hooks-row')")


def _hook_tested(page) -> None:
    steps.open_hook(page, "a-hook-with-a-rather-long-name-for-narrow-screens")
    page.click("#panel-hooks [data-focus='run']")
    page.wait("document.querySelector('#panel-hooks .hooks-result')", what="试运行的结果")


def _sandbox(page) -> None:
    """沙箱里额外允许的路径，里面有一条很长的路径（要整条看得到，所以折行，不能冲出去）。"""
    page.demo.api("/api/sandbox/add", {"path": steps.native_path("/srv/a-rather-long-folder-name/with/several/levels/that-goes-on-and-on-and-on-and-on"), "write": True})
    steps.open_dialog(page, "#settings-open", "settings")
    page.click("#sandbox-paths-open")
    page.wait(f"{steps.is_open('sandbox-dialog')} && document.querySelector('#sandbox-list li.sandbox-row')", what="沙箱的路径窗口打开")


def _endpoint_editor(page) -> None:
    """设置 → 自定义模型接口 → 编辑演示里那个假的本机 Ollama，再点"测试连接"，模型列表和结果都画出来。"""
    steps.open_dialog(page, "#settings-open", "settings")
    page.click("#endpoints-list > li.endpoint-row .endpoint-open")
    page.wait(steps.is_open("endpoint-dialog"), what="编辑接口的窗口打开")
    page.click("#endpoint-test")
    page.wait("document.getElementById('endpoint-result').classList.contains('good')", timeout=15, what="测试连接的结果")


def _retry_confirm(page) -> None:
    page.click("#log .turn-retry")
    page.wait("document.querySelector('#log .turn-confirm')", what="重新生成之前的确认")


def _edit_confirm(page) -> None:
    page.click("#input")
    page.key("ArrowUp")
    page.wait("document.activeElement.classList.contains('turn-editor-input')", what="编辑框打开")
    page.key("Enter")
    page.wait("document.querySelector('.turn-editor .turn-confirm')", what="编辑框里的确认")


@pytest.mark.parametrize("code", ["zh", "en", "es"])
def test_no_text_overflow_at_phone_width(page, code):
    page.size(PHONE_WIDTH, 844)
    steps.calm(page)
    status, saved = page.demo.api("/api/task/save", {
        "title": LONG_TASK, "kind": "reminder", "prompt": "提醒的内容也写得很长很长" * 6,
        "schedule": {"type": "daily", "time": "09:00"}, "enabled": False, "approval": "ask",
    })
    assert status == 200, saved
    try:
        if code != "zh":
            steps.language(page, code)
        steps.send(page, LONG_MESSAGE)
        steps.reply(page, "记下了")
        found: dict[str, list[str]] = {}
        for name, show, roots, close in _surfaces(page):
            show()
            steps.idle(page)
            problems = checks.overflow(page, roots)
            if problems:
                found[name] = problems
            if callable(close):
                close()
            elif close:
                steps.escape_closes(page, close)
        assert found == {}, f"{code}：这些地方的文字冲出了容器：\n" + "\n".join(f"{where}:\n  " + "\n  ".join(items) for where, items in found.items())
    finally:
        page.demo.api("/api/task/delete", {"id": saved["id"]})
        for todo in page.js("[...document.querySelectorAll('#todos > li.todo')].map((row) => Number(row.dataset.id))"):
            page.demo.api("/api/todo/delete", {"id": todo})


def test_composer_footer_fits_at_phone_width_in_spanish(page):
    page.size(PHONE_WIDTH, 844)
    steps.calm(page)
    steps.language(page, "es")
    status, reply = page.demo.api("/api/approval", {"mode": "edits"})  # 开关上的字最长的一档之一
    assert status == 200, reply
    steps.send(page, "[ui:hello] hola")
    steps.reply(page, "收到第 1 条消息")
    page.wait("!document.getElementById('foot-cost').hidden", what="花费出现在输入框下面")
    assert checks.sticking_out(page, [".foot"]) == []
    assert page.js("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
