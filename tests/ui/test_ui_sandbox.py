"""命令的沙箱（harness/sandbox/）在页面上：设置里的"沙箱"一节、额外允许的路径的窗口、任务和通道里选哪一档。

规矩：几档的名字和说明、这台电脑上能不能用，都由助手程序给（state.sandbox），页面只照着画；选了马上保存；
用不了的时候明说命令会被拒绝（不能让人以为它在沙箱里）；路径的每一行和别的本地的东西一样（三个按钮、原地删除、原地编辑）。
演示服务器是一个子进程，用的是这台电脑上真的沙箱（macOS 的 sandbox-exec；CI 的 Linux 上看有没有装 bwrap），
所以"用不了"的样子另开一个演示来看（--no-sandbox，见 conftest 的 no_sandbox_demo）。这里只看页面，命令本身在不在沙箱里
由 tests/test_sandbox_*.py 测。
"""

from __future__ import annotations

import json

import pytest
from fake_telegram import TOKEN

from . import checks, steps
from .pilot import PHONE_WIDTH, until
from .test_ui_page import CONTRAST


def _saved(page) -> dict:
    """设置文件里存着的沙箱（服务器真的存下来了）。"""
    path = page.demo.root / "data" / "settings.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return data.get("sandbox", {"level": "off", "paths": []})


def _choose(page, selector: str, value: str) -> None:
    """在一个下拉框里选一项（和用户选的一样，会发出 change）。"""
    page.focus(selector)
    page.js(f"(s => {{ s.value = {value!r}; s.dispatchEvent(new Event('change', {{ bubbles: true }})); }})(document.querySelector({selector!r}))")


def _settings(page) -> None:
    steps.open_dialog(page, "#settings-open", "settings")
    page.wait("!document.getElementById('sandbox-section').hidden && document.getElementById('sandbox-level').options.length === 3", what="设置里的沙箱一节画好")
    page.reach("#sandbox-level")  # 滚到能看见的地方


def _paths(page) -> None:
    _settings(page)
    page.click("#sandbox-paths-open")
    page.wait(f"{steps.is_open('sandbox-dialog')} && !{steps.is_open('settings')}", what="额外允许的路径的窗口打开")
    steps.idle(page)


@pytest.fixture
def bare(no_sandbox_demo, browser):
    """一个标签页，开着"这台电脑上用不了沙箱"的那个演示。和 page 一样，结束时页面里不能有任何错误。"""
    no_sandbox_demo.reset()
    opened = browser.new_page()
    opened.demo = no_sandbox_demo
    try:
        opened.goto(no_sandbox_demo.url)
        opened.wait("document.body.classList.contains('ready')", what="页面亮相")
        yield opened
    finally:
        errors = opened.unexpected_errors()
        opened.close()
    if errors:
        pytest.fail("页面里出了错：\n" + "\n".join(errors))


def test_the_level_is_saved_as_soon_as_it_is_chosen(page):
    _settings(page)
    assert page.js("document.getElementById('sandbox-level').value") == "off"
    assert page.js("[...document.getElementById('sandbox-level').options].map((o) => o.value)") == ["off", "workspace", "network"]
    assert page.text("#sandbox-level-text"), "选的这一档下面要写它是什么意思"
    off_text = page.text("#sandbox-level-text")
    _choose(page, "#sandbox-level", "workspace")
    until(lambda: _saved(page)["level"] == "workspace", what="选了马上存下来")
    page.wait("document.getElementById('sandbox-state').textContent.length > 2 && document.getElementById('settings-message').textContent.length > 0", what="说一声")
    assert page.text("#sandbox-level-text") != off_text, "下面那一句跟着选的那一档变"
    # 这台电脑上用不用得了，两种情况都要说得对：用不了而且选了一档，是醒目的红色；用得了就不出现
    if page.js("!document.getElementById('sandbox-problem').hidden"):
        assert page.js("document.getElementById('sandbox-problem').classList.contains('bad')")
        assert page.js("document.getElementById('settings-message').classList.contains('bad')")
    else:
        assert page.js("document.getElementById('settings-message').classList.contains('good')")
        assert page.text("#sandbox-backend"), "能用的时候写明用的是哪一种沙箱"
    # 刷新以后还是这一档（存在助手程序那边，不是页面上记着）
    steps.escape_closes(page, "settings")
    page.reload()
    page.wait("document.body.classList.contains('ready')")
    _settings(page)
    assert page.js("document.getElementById('sandbox-level').value") == "workspace"
    _choose(page, "#sandbox-level", "off")
    until(lambda: _saved(page)["level"] == "off", what="改回关")


def test_an_unavailable_sandbox_is_said_plainly(bare):
    page = bare
    _settings(page)
    # 没选沙箱：只是提醒一句这台电脑上用不了（琥珀色），写着原因和怎么装
    page.wait("!document.getElementById('sandbox-problem').hidden", what="写明用不了")
    assert "bubblewrap" in page.text("#sandbox-problem")
    assert not page.js("document.getElementById('sandbox-problem').classList.contains('bad')")
    # 选了一档：命令一律会被拒绝。用红色写清楚，顶上的小字、下面说的那一句也都这么说
    _choose(page, "#sandbox-level", "workspace")
    until(lambda: _saved(page)["level"] == "workspace")
    page.wait("document.getElementById('sandbox-problem').classList.contains('bad')", what="变成红色的警告")
    assert "拒绝" in page.text("#sandbox-problem") and "拒绝" in page.text("#sandbox-state")
    assert page.js("document.getElementById('settings-message').classList.contains('bad')")
    steps.escape_closes(page, "settings")
    # 新建任务（跟随设置 → 现在是"只在工作目录里"）：编辑框里也说这个任务的命令会被拒绝
    steps.nav_tab(page, "tasks")
    steps.open_dialog(page, "#task-new", "task-editor")
    assert page.js("document.getElementById('task-sandbox').value") == "default"
    assert page.js("!document.getElementById('task-sandbox-problem').hidden")
    _choose(page, "#task-sandbox", "off")
    page.wait("document.getElementById('task-sandbox-problem').hidden", what="这个任务关掉沙箱：不再说会被拒绝")
    page.key("Escape")  # 改过了：先提醒一次
    page.key("Escape")
    page.wait(f"!{steps.is_open('task-editor')}")
    # 换成英文：这几句都有译文，在原地换过去
    steps.language(page, "en")
    _settings(page)
    assert checks.untranslated(page, ["#sandbox-section"]) == []
    assert "bubblewrap" in page.text("#sandbox-problem")
    steps.escape_closes(page, "settings")


def test_extra_paths_are_added_edited_revealed_and_removed_in_place(page):
    page.allow_errors("/api/sandbox/add")  # 下面故意加两条加不进来的：服务器回答 400，浏览器会在控制台记一笔
    _paths(page)
    # 一条都没有：说一句能加什么
    assert page.exists("#sandbox-list .sandbox-empty")
    # 不能加的：相对路径、智狐自己的数据目录。原因写在窗口里
    page.click("#sandbox-new")
    page.type("relative/folder")
    page.key("Enter")
    page.wait("document.getElementById('sandbox-message').classList.contains('bad')", what="相对路径不行")
    page.js("document.getElementById('sandbox-new').value = ''")
    page.click("#sandbox-new")
    page.type(str(page.demo.root / "data" / "memories"))
    page.click("#sandbox-add")
    page.wait("document.getElementById('sandbox-message').textContent.includes('永远碰不到')", what="数据目录加不进来")
    assert _saved(page)["paths"] == []
    # 加一条只读的（还不存在的路径也能加，标着"现在不存在"），再加一条可读写的
    page.js("document.getElementById('sandbox-new').value = ''")
    page.click("#sandbox-new")
    page.type("~/.zhifox-ui-test-tools")
    page.key("Enter")
    page.wait("document.querySelectorAll('#sandbox-list li.sandbox-row').length === 1", what="加好了")
    first = _saved(page)["paths"][0]
    assert first["write"] is False and first["path"].endswith("/.zhifox-ui-test-tools") and not first["path"].startswith("~")
    assert page.exists("#sandbox-list .sandbox-missing")
    folder = str(page.demo.root)
    page.click("#sandbox-new")
    page.type(folder)
    _choose(page, "#sandbox-new-mode", "write")
    page.click("#sandbox-add")
    page.wait("document.querySelectorAll('#sandbox-list li.sandbox-row').length === 2")
    assert _saved(page)["paths"][1] == {"path": folder, "write": True}
    assert page.js("document.querySelectorAll('#sandbox-list .sandbox-tag.write').length") == 1
    assert checks.row_trio(page, "#sandbox-list > li.sandbox-row") == []
    # 原地编辑：改成只读，保存
    row = "#sandbox-list > li.sandbox-row:nth-child(2)"
    page.hover(row)
    page.click(f"{row} .sandbox-actions button:nth-child(2)")
    page.wait("document.activeElement.classList.contains('sandbox-edit-field')", what="编辑框拿到焦点")
    _choose(page, f"{row} .sandbox-mode", "read")
    page.click(f"{row} .sandbox-edit .mini.primary")
    until(lambda: _saved(page)["paths"][1] == {"path": folder, "write": False}, what="改好了")
    page.wait("!document.querySelector('#sandbox-list .sandbox-edit')")
    # 在访达中显示：演示里只打印路径；窗口里说一声
    page.hover(row)
    page.click(f"{row} .sandbox-actions button:nth-child(1)")
    page.wait("document.getElementById('sandbox-message').textContent.length > 0 && !document.getElementById('sandbox-message').classList.contains('bad')", what="显示了")
    # 原地删除：先确认，Esc 取消；再删一次真的删掉
    page.hover(row)
    page.click(f"{row} .sandbox-actions button:last-child")
    page.wait("document.activeElement.closest('.sandbox-confirm')", what="焦点移进删除确认")
    page.key("Escape")
    page.wait("!document.querySelector('.sandbox-confirm') && document.querySelectorAll('#sandbox-list li.sandbox-row').length === 2")
    for _ in range(2):
        page.hover("#sandbox-list > li.sandbox-row")
        page.click("#sandbox-list > li.sandbox-row .sandbox-actions button:last-child")
        page.wait("document.querySelector('.sandbox-confirm')")
        page.click(".sandbox-confirm button.danger")
        page.wait("!document.querySelector('.sandbox-confirm')")
    until(lambda: _saved(page)["paths"] == [], what="都删掉了")
    page.wait("document.querySelector('#sandbox-list .sandbox-empty')", what="回到空的样子")
    steps.escape_closes(page, "sandbox-dialog")


def test_a_task_keeps_its_own_sandbox(page):
    steps.nav_tab(page, "tasks")
    steps.open_dialog(page, "#task-new", "task-editor")
    assert page.js("!document.getElementById('task-sandbox-field').hidden")
    assert page.js("document.getElementById('task-sandbox').options[0].value") == "default"
    assert "关" in page.js("document.getElementById('task-sandbox').options[0].textContent"), "跟随设置写着现在是哪一档"
    page.click("[data-kind='reminder']")
    page.wait("document.getElementById('task-sandbox-field').hidden", what="只提醒的任务没有沙箱这一项")
    page.click("[data-kind='agent']")
    page.click("#task-title")
    page.type("沙箱里的任务")
    page.click("#task-prompt")
    page.type("看看磁盘还剩多少")
    _choose(page, "#task-sandbox", "network")
    page.wait("document.getElementById('task-sandbox-text').textContent.length > 0")
    page.click("#task-save")
    page.wait(f"!{steps.is_open('task-editor')}", what="存好了")
    tasks = json.loads((page.demo.root / "data" / "tasks.json").read_text(encoding="utf-8"))["tasks"]
    task = next(item for item in tasks if item["title"] == "沙箱里的任务")
    try:
        assert task["sandbox"] == "network"
        # 再打开编辑：还是这一档
        row = f"#task-list > li.task[data-id={json.dumps(task['id'])}]"
        page.wait(f"document.querySelector({row!r})", what="列表里有这个任务")
        page.hover(row)
        page.click(f"{row} .task-actions button:nth-last-child(2)")
        page.wait(f"{steps.is_open('task-editor')} && document.getElementById('task-sandbox').value === 'network'", what="编辑框里还是这一档")
        steps.escape_closes(page, "task-editor")
    finally:
        page.demo.api("/api/task/delete", {"id": task["id"]})


@pytest.fixture
def telegram(page):
    status, reply = page.demo.api("/api/channel/connect", {"platform": "telegram", "token": TOKEN})
    assert status == 200, reply
    yield page
    page.demo.api("/api/channel/remove", {"platform": "telegram"})


def _channel_saved(page) -> dict:
    return json.loads((page.demo.root / "data" / "channels.json").read_text(encoding="utf-8"))["platforms"]["telegram"]


def test_a_channel_keeps_its_own_sandbox(telegram):
    page = telegram
    steps.extensions_tab(page, "channels")
    page.click("#panel-channels li.chan-row[data-platform='telegram'] .chan-open")
    page.wait("document.querySelector('#panel-channels .chan-stepper')", what="向导打开")
    page.click("#panel-channels [data-focus='step:4']")
    page.wait("document.getElementById('chan-sandbox')", what="第 4 步有沙箱这一项")
    assert page.js("document.getElementById('chan-sandbox').value") == "default"
    _choose(page, "#chan-sandbox", "workspace")
    page.wait("document.getElementById('chan-sandbox') && document.getElementById('chan-sandbox').value === 'workspace'")
    # 没点"完成"就想回到列表：先提醒一次
    page.click("#panel-channels .chan-back")
    page.wait("document.querySelector('#panel-channels .chan-help.bad')", what="提醒还没保存")
    assert "沙箱" in page.text("#panel-channels .chan-help")
    page.click("#panel-channels .chan-foot .button.primary")
    until(lambda: _channel_saved(page)["sandbox"] == "workspace", what="存好了")
    page.wait("document.querySelector('#panel-channels li.chan-row[data-platform=\"telegram\"] .chan-meta')", what="回到列表")
    assert "沙箱" in page.text("#panel-channels li.chan-row[data-platform='telegram'] .chan-meta")
    # 英文：向导第 4 步里没有漏翻的
    steps.close_extensions(page)
    steps.language(page, "en")
    steps.extensions_tab(page, "channels")
    page.click("#panel-channels li.chan-row[data-platform='telegram'] .chan-open")
    page.wait("document.querySelector('#panel-channels .chan-stepper')")
    page.click("#panel-channels [data-focus='step:4']")
    page.wait("document.getElementById('chan-sandbox')")
    assert checks.untranslated(page, ["#extensions"]) == []
    steps.close_extensions(page)


def test_what_it_can_do_points_to_the_sandbox_settings(page):
    steps.open_side(page)
    shell = "#caps .caps-item[data-id=\"shell\"]"
    page.wait(f"document.querySelector({shell!r})")
    page.click(f"{shell} .caps-head")
    page.wait(f"[...document.querySelectorAll({shell!r} + ' .caps-actions button')].some((b) => b.textContent === '沙箱')", what="执行命令那一项有去沙箱设置的按钮")
    index = page.js(f"[...document.querySelectorAll({shell!r} + ' .caps-actions button')].findIndex((b) => b.textContent === '沙箱')")
    page.click(f"{shell} .caps-actions button", index)
    page.wait(f"{steps.is_open('settings')} && document.activeElement.id === 'sandbox-level'", what="设置打开、翻到沙箱、焦点在下拉框上")
    steps.escape_closes(page, "settings")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_contrast_in_both_themes(page, theme):
    page.js(f"localStorage.setItem('harness.theme', {theme!r})")
    page.reload()
    page.wait(f"document.body.classList.contains('ready') && document.documentElement.dataset.theme === {theme!r}")
    steps.calm(page)
    page.demo.api("/api/sandbox/add", {"path": str(page.demo.root), "write": True})
    _settings(page)
    selectors = ["#sandbox-section h3", ".sandbox-words b", ".sandbox-lead", "#sandbox-level-text", "#sandbox-backend", ".sandbox-paths-words b", "#sandbox-paths-count"]
    for selector, ratio in page.js(f"({CONTRAST})({selectors!r})"):
        assert ratio is None or ratio >= 4.5, f"{theme} 下 {selector} 的对比度只有 {ratio}"
    page.click("#sandbox-paths-open")
    page.wait(steps.is_open("sandbox-dialog"))
    steps.idle(page)
    selectors = [".sandbox-always-words b", ".sandbox-always-words p", ".sandbox-path", ".sandbox-tag.write", "#sandbox-dialog .hint"]
    for selector, ratio in page.js(f"({CONTRAST})({selectors!r})"):
        assert ratio is None or ratio >= 4.5, f"{theme} 下 {selector} 的对比度只有 {ratio}"
    assert checks.native_selects(page) == []
    steps.escape_closes(page, "sandbox-dialog")


@pytest.mark.parametrize("code", ["zh", "en", "es"])
def test_nothing_spills_out_at_phone_width(page, code):
    page.size(PHONE_WIDTH, 844)
    steps.calm(page)
    if code != "zh":
        steps.language(page, code)
    page.demo.api("/api/sandbox/level", {"level": "workspace"})
    page.demo.api("/api/sandbox/add", {"path": "/srv/a-rather-long-folder-name/with/several/levels/of/nesting/that-goes-on-and-on-and-on", "write": True})
    _settings(page)
    assert checks.overflow(page, ["#sandbox-section"]) == []
    page.click("#sandbox-paths-open")
    page.wait(steps.is_open("sandbox-dialog"))
    steps.idle(page)
    page.focus("#sandbox-list .sandbox-actions button:last-child")  # 按钮都露出来（悬停时的样子）
    assert checks.overflow(page, ["#sandbox-dialog"]) == []
    page.click("#sandbox-list .sandbox-actions button:nth-child(2)")
    page.wait("document.querySelector('.sandbox-edit')")
    assert checks.overflow(page, ["#sandbox-dialog"]) == []
    page.key("Escape")
    page.wait("!document.querySelector('.sandbox-edit')")
    steps.escape_closes(page, "sandbox-dialog")
    steps.nav_tab(page, "tasks")
    steps.open_dialog(page, "#task-new", "task-editor")
    # 先滚到中间：只滚到"刚好露出来"的话，它会停在编辑框底下那条固定的按钮栏后面（CI 的 macOS 上真出过"被盖住"）
    page.js("document.getElementById('task-sandbox').scrollIntoView({ block: 'center' })")
    page.reach("#task-sandbox")
    assert checks.overflow(page, ["#task-sandbox-field"]) == []
    steps.escape_closes(page, "task-editor")
