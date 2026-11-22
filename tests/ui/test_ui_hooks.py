"""扩展 · 钩子（页面在 harness/webui/page/scripts/hooks*.js，背后是 harness/hooks/ 和 webui/hooks_api.py）。

在真的页面上看：一个钩子都没有时讲清楚四个时刻、给一个例子，并且写明"钩子会在这台电脑上运行命令"；
通过表单添加（明显没填的在表单里说，助手程序退回来的错也写在表单底下，不弹 toast），改过没保存就想离开先提醒一次；
打开一个钩子能试运行（示例 JSON 可以先改；写坏了在原地说），看到它会让智狐怎么办、退出码和输出；列表上开关、原地确认删除。
最后是钩子真的跑起来：拦下一条命令时，工具卡片上写着被你的钩子拦下了，运行记录里它是挂在那次调用下面的一段；
拦下你的一条消息时，消息不发给模型，这一轮的运行记录写着"被你的钩子拦下了"。
钩子的脚本都是一小段 Python，用跑测试的这个 Python 运行（steps.save_hook），Windows 上也一样。
"""

from __future__ import annotations

import json
import sys

from . import steps
from .test_ui_page import CONTRAST

ROW = "#panel-hooks li.hooks-row"
# 工具执行之前：把命令拦下，原因是"no <命令>"
DENY = "import json, sys\ncall = json.load(sys.stdin)\nprint('no ' + call['tool_input'].get('command', ''), file=sys.stderr)\nsys.exit(2)\n"
# 你发出消息时：一律拦下
QUIET = "import sys\nsys.stdin.read()\nprint('quiet hours', file=sys.stderr)\nsys.exit(2)\n"
# 把试运行的示例换成 value（一个 JavaScript 表达式），像你在框里改过一样
PAYLOAD = "(() => {{ const area = document.getElementById('hooks-payload'); area.value = {value}; area.dispatchEvent(new Event('input')); }})()"


def test_the_empty_page_explains_hooks_and_warns_that_they_run_commands(page):
    steps.extensions_tab(page, "hooks")
    page.wait("document.querySelector('#panel-hooks .hooks-empty')", what="空的样子")
    assert page.js("document.querySelectorAll('#panel-hooks .hooks-events dt').length") == 4
    text = page.text("#panel-hooks")
    for event in ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"):
        assert event in text
    assert "在这台电脑上运行命令" in text and "模型也没有任何能添加或修改钩子的工具" in text
    assert page.js("document.getElementById('count-hooks').textContent") == ""
    # 标签页按方向键走：钩子是最后一个
    page.focus("#tab-skills")
    page.key("End")
    page.wait("document.activeElement.id === 'tab-hooks' && !document.getElementById('panel-hooks').hidden")


def test_a_hook_is_added_through_the_form_tested_switched_off_and_deleted(page):
    script = page.demo.root / "hook-scripts"
    script.mkdir(exist_ok=True)
    (script / "deny.py").write_text(DENY, encoding="utf-8")
    steps.extensions_tab(page, "hooks")
    page.click("#panel-hooks .hooks-empty .button.primary")
    page.wait("document.activeElement === document.getElementById('hooks-f-name')", what="表单打开，焦点在名字上")
    # 明显没填的：在表单底下说，焦点回到那一格
    page.click("#hooks-f-save")
    page.wait("document.getElementById('hooks-f-message').textContent.includes('先给这个钩子起个名字')", what="没填名字")
    assert page.js("document.getElementById('hooks-f-message').classList.contains('bad')")
    page.wait("document.activeElement === document.getElementById('hooks-f-name')")
    page.type("no-echo")
    # 时刻默认是"工具执行之前"；只对"执行命令"生效
    assert page.js("document.getElementById('hooks-f-event').value") == "PreToolUse"
    page.click("#hooks-f-matcher .hooks-pick input[value='shell']")
    page.click("#hooks-f-command")
    page.type(sys.executable)
    page.click("#hooks-f-args")
    page.type(str(script / "deny.py"))
    page.click("#hooks-f-save")
    page.wait("document.querySelector('#panel-hooks .hooks-test')", what="保存以后打开这个钩子")
    assert page.text("#panel-hooks .hooks-title-name") == "no-echo"
    page.wait("document.getElementById('count-hooks').textContent === '1'", what="标签上写着有 1 个")
    status, listing = page.demo.api("/api/hooks/list")
    assert status == 200 and listing["hooks"][0]["hook"]["matcher"] == "shell"

    # 试运行：示例是一条 ls -la；钩子拦下它，原因写在最显眼的那一句里
    payload = json.loads(page.js("document.getElementById('hooks-payload').value"))
    assert payload["tool_name"] == "run_command" and payload["tool_input"] == {"command": "ls -la"} and payload["test"] is True
    page.click("#panel-hooks [data-focus='run']")
    page.wait("document.querySelector('#panel-hooks .hooks-result')", what="试运行的结果")
    assert "会拦下这次调用" in page.text("#panel-hooks .hooks-verdict") and "no ls -la" in page.text("#panel-hooks .hooks-verdict")
    assert "退出码 2" in page.text("#panel-hooks .hooks-run-facts")
    assert "no ls -la" in page.text("#panel-hooks .hooks-out")
    # 改一改示例再跑：原因跟着变
    page.js(PAYLOAD.format(value="JSON.stringify({tool_input: {command: 'pwd'}})"))
    page.click("#panel-hooks [data-focus='run']")
    page.wait("document.querySelector('#panel-hooks .hooks-verdict') && document.querySelector('#panel-hooks .hooks-verdict').textContent.includes('no pwd')", what="改过的示例")
    # 写坏的 JSON：在原地说，不发出去
    page.js(PAYLOAD.format(value="'{oops'"))
    page.click("#panel-hooks [data-focus='run']")
    page.wait("document.querySelector('#panel-hooks .hooks-test-status.bad')", what="JSON 写坏了")
    assert "解析不了" in page.text("#panel-hooks .hooks-test-status")

    # 回到列表：这一行写着上次是试运行、拦下了
    page.click("#panel-hooks .hooks-back")
    page.wait(f"document.querySelector({ROW!r})", what="回到列表")
    last = page.text(f"{ROW} .hooks-last")
    assert "试运行" in last and "拦下" in last
    # 开关：关掉以后配置文件里也是关着的
    page.click(f"{ROW} .hooks-switch")
    page.wait(f"document.querySelector('{ROW} .hooks-switch').getAttribute('aria-checked') === 'false'", what="关掉")
    assert page.demo.api("/api/hooks/list")[1]["hooks"][0]["hook"]["enabled"] is False
    # 删除：原地确认，Esc 取消；再来一次真的删掉，回到空的样子
    page.hover(ROW)
    page.click(f"{ROW} .hooks-actions button:last-child")
    page.wait("document.activeElement.closest('#panel-hooks .hooks-confirm')", what="原地确认删除，焦点在取消上")
    page.key("Escape")
    page.wait(f"!document.querySelector('#panel-hooks .hooks-confirm') && document.querySelector({ROW!r})", what="Esc 取消删除")
    page.hover(ROW)
    page.click(f"{ROW} .hooks-actions button:last-child")
    page.wait("document.querySelector('#panel-hooks .hooks-confirm button.danger')")
    page.click("#panel-hooks .hooks-confirm button.danger")
    page.wait("document.querySelector('#panel-hooks .hooks-empty')", what="删掉以后回到空的样子")
    assert page.demo.api("/api/hooks/list")[1]["hooks"] == []


def test_the_form_shows_what_the_assistant_refused_and_asks_before_dropping_edits(page):
    page.allow_errors("/api/hooks/save")  # 故意存一个不合格的：助手程序回答 409
    steps.calm(page)
    steps.extensions_tab(page, "hooks")
    page.click("#panel-hooks [data-focus='add']")
    page.wait("document.activeElement === document.getElementById('hooks-f-name')")
    page.type("bad name")
    page.click("#hooks-f-command")
    page.type("python3")
    page.click("#hooks-f-save")
    # 助手程序退回来的话写在表单底下（不是浮在页面上的提示：会被对话框的暗底挡住）
    page.wait("document.getElementById('hooks-f-message').textContent.includes('名字只能用')", what="名字不合格")
    assert page.js("document.getElementById('toast').classList.contains('show')") is False
    # 换成"一轮答完时"："对哪些工具""钩子出错时"两块藏起来
    page.js("(() => { const pick = document.getElementById('hooks-f-event'); pick.value = 'Stop'; pick.dispatchEvent(new Event('change', { bubbles: true })); })()")
    page.wait("document.getElementById('hooks-f-matcher').hidden && document.getElementById('hooks-f-failure-field').hidden", what="工具那一块藏起来")
    assert "只是通知" in page.text("#hooks-f-event-help")
    # 改过没保存：Esc 先提醒，再按一次才回到列表
    page.click("#hooks-f-name")
    page.key("Escape")
    page.wait("document.getElementById('hooks-f-message').textContent.includes('还有没保存的修改')", what="Esc：先提醒")
    assert page.js("Boolean(document.getElementById('hooks-f-name'))")
    page.key("Escape")
    page.wait("!document.getElementById('hooks-f-name') && document.querySelector('#panel-hooks .hooks-empty')", what="再按一次回到列表")
    # 右上角的 ×、点外面也一样：先提醒，再来一次才关上对话框
    for how in ("×", "点外面"):
        page.click("#panel-hooks [data-focus='add']")
        page.wait("document.activeElement === document.getElementById('hooks-f-name')")
        page.type("half")
        (page.click("#close-extensions") if how == "×" else page.click_at(4, 4))
        page.wait("document.getElementById('hooks-f-message').textContent.includes('还有没保存的修改')", what=f"{how}：先提醒")
        assert page.js(steps.is_open("extensions")), f"{how}：有没保存的修改，第一次不该关上"
        (page.click("#close-extensions") if how == "×" else page.click_at(4, 4))
        page.wait(f"!{steps.is_open('extensions')}", what=f"{how}：再来一次就关上")
        steps.extensions_tab(page, "hooks")


def test_a_blocking_hook_shows_on_the_tool_card_and_in_the_run_trace(page):
    steps.save_hook(page, "no-echo", DENY)
    steps.send(page, "[ui:approve] 跑一条命令")
    steps.pending_confirm(page)
    page.click("#log .confirm .button.primary")  # 确认在前：你允许了，钩子才跑（它只能让事情更严）
    steps.reply(page, "不执行了")
    assert "被你的钩子“no-echo”拦下了" in page.text("#log .call .tool-outcome") or "被你的钩子“no-echo”拦下了" in page.text("#log .call")
    steps.open_trace(page)
    # 钩子的那一段挂在"执行命令"下面（缩进一层），图例里多了"你的钩子在跑"，详情里写着拦下了、退出码 2
    page.wait("document.querySelector('#trace-body .trace-lane.kind-hook.depth-1')", what="钩子的那一段")
    assert "no-echo" in page.text("#trace-body .trace-lane.kind-hook")
    assert page.js("Boolean(document.querySelector('#trace-body .trace-legend .seg-hooks'))")
    step = page.text("#trace-body .trace-step.kind-hook")
    assert "拦下" in step and "退出码 2" in step and "PreToolUse" in step
    assert "跑了 1 次你的钩子，其中 1 次拦下了" in page.text("#trace-body .trace-hint")
    steps.escape_closes(page, "trace-dialog")


def test_a_hook_can_hold_back_your_message(page):
    steps.save_hook(page, "quiet", QUIET, event="UserPromptSubmit", matcher="")
    steps.send(page, "[ui:hello] 你好")
    page.wait("[...document.querySelectorAll('#log .notice')].some((node) => node.textContent.includes('被你的钩子“quiet”拦下了'))", what="说明消息被拦下了")
    page.wait("!document.body.classList.contains('busy') && document.querySelector('#log .trace-open')", what="这一轮结束，有运行记录")
    assert page.js("document.querySelectorAll('#log .msg.assistant').length") == 0, "被拦下的消息不发给模型，也就没有回答"
    steps.open_trace(page)
    assert "被你的钩子拦下了" in page.text("#trace-sub")
    assert page.js("document.querySelectorAll('#trace-body .trace-lane.kind-hook').length") == 1
    steps.escape_closes(page, "trace-dialog")


# 要查对比度的字（浅色、深色都要过 4.5:1）：列表的一行、打开一个以后的设置和试运行的结果
LIST_TEXT = ["#panel-hooks .hooks-lead", "#panel-hooks .hooks-intro .hooks-note.warn span", "#panel-hooks .hooks-event", "#panel-hooks .hooks-meta", "#panel-hooks .hooks-last"]
DETAIL_TEXT = ["#panel-hooks .hooks-facts dt", "#panel-hooks .hooks-raw", "#panel-hooks .hooks-card > .hooks-small", "#panel-hooks .hooks-chip", "#panel-hooks .hooks-run-facts", "#panel-hooks .hooks-out"]


def test_the_hooks_page_reads_well_in_light_and_dark(page):
    steps.save_hook(page, "no-echo", DENY)
    for theme in ("light", "dark"):
        if theme == "dark":
            page.click("#theme")
            page.wait("document.getElementById('appearance').matches(':popover-open')", what="外观面板打开")
            page.click("[data-theme-pick='dark']")
            page.wait("document.documentElement.dataset.theme === 'dark'", what="换成深色")
            page.key("Escape")
            steps.idle(page)
        steps.open_hook(page, "no-echo")
        page.click("#panel-hooks [data-focus='run']")
        page.wait("document.querySelector('#panel-hooks .hooks-result')", what="试运行的结果")
        steps.idle(page)
        for selector, ratio in page.js(f"({CONTRAST})({DETAIL_TEXT!r})"):
            assert ratio is not None and ratio >= 4.5, f"{theme}：{selector} 的对比度只有 {ratio}"
        page.click("#panel-hooks .hooks-back")
        page.wait("document.querySelector('#panel-hooks li.hooks-row .hooks-last')", what="回到列表")
        steps.idle(page)
        for selector, ratio in page.js(f"({CONTRAST})({LIST_TEXT!r})"):
            assert ratio is not None and ratio >= 4.5, f"{theme}：{selector} 的对比度只有 {ratio}"
        steps.close_extensions(page)
