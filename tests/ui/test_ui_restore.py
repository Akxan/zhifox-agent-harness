"""从备份恢复在页面上（page/scripts/restore.js）：选文件 → 预览 → 确认 → 完成的提示，中途关窗口先问，出错写在窗口里。

真的恢复会换掉演示的数据，所以另开一个演示（restore_demo，只有这里用）；只上传、不恢复的几个用大家共用的那个。
检查压缩包、密钥不覆盖、自动备份这些规矩由 tests/test_restore*.py 测，这里只看页面：每一步画得对不对、
提示写在窗口里、恢复以后页面不用刷新就照着新的数据画（会话、记忆、语言都跟着变），之后还能接着聊。
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from . import steps
from .conftest import Demo, finish_page, server_log, show_demo


@pytest.fixture(scope="module")
def restore_demo(tmp_path_factory, browser, artifacts):
    """另一个演示服务器：真的恢复会换掉它的数据，不能和别的测试共用。"""
    server = Demo(tmp_path_factory.mktemp("ui-demo-restore"))
    yield server
    server.stop()
    (artifacts / server_log("demo-server-restore")).write_text("".join(server.output), encoding="utf-8")


@pytest.fixture
def own(request, restore_demo, browser, artifacts):
    """开着 restore_demo 的一个标签页（和 page 一样，结束时页面里不能有任何错误）。"""
    restore_demo.reset()
    opened = browser.new_page()
    opened.demo = restore_demo
    try:
        opened.emulate()
        show_demo(opened)
        yield opened
    finally:
        finish_page(request, opened, artifacts)


def _with_memory(raw: bytes, name: str, text: str) -> bytes:
    """在一份备份里多放一条记忆：恢复以后，它出现在记忆里，就说明真的换成了备份里的。"""
    source, out = zipfile.ZipFile(io.BytesIO(raw)), io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            target.writestr(info, source.read(info))
        target.writestr("memories/" + name, text)
    return out.getvalue()


def test_pick_preview_confirm_and_done_without_reloading(own):
    page = own
    backup = _with_memory(steps.backup_bytes(page.demo), "从备份来的.md", "备份里的一条记忆")
    count = sum(name.startswith("memories/") and not name.endswith("/") for name in zipfile.ZipFile(io.BytesIO(backup)).namelist())
    page.js("window.__notReloaded = true")  # 恢复以后页面不刷新：这个记号一直在
    steps.open_restore(page)
    assert page.js("document.getElementById('restore-dialog').contains(document.activeElement)")  # 焦点在窗口里
    steps.choose_backup(page, backup)
    # 预览：文件名、导出的时间，两种方式，每一样会怎样（话是助手程序写好的）
    assert page.text("#restore-body .restore-file-name") == "zhifox-backup-20261120-0930.zip"
    assert "导出" in page.text("#restore-body .restore-file-facts")
    assert page.js("document.querySelector('#restore-body .restore-mode.on input').value") == "overwrite"
    memories = page.js("[...document.querySelectorAll('#restore-body .restore-step')].find((row) => row.querySelector('.restore-step-name').textContent === '记忆').querySelector('.restore-step-what').textContent")
    assert "换成备份里的" in memories and f"{count} 条记忆" in memories
    # 换语言（窗口开着时顶栏的按钮点不到：像在另一个标签页里换了一样，从状态里收到新的语言）：
    # 窗口留着，里面的话在原地换成西班牙语（每一样会怎样那句话也是）
    assert page.demo.api("/api/language", {"language": "es"})[0] == 200
    page.wait("document.documentElement.lang === 'es'", what="换成西班牙语")
    assert page.js(steps.is_open("restore-dialog"))
    assert page.text("#restore-title") == "Restaurar desde una copia"
    assert "Se sustituye por lo de la copia" in page.text("#restore-body .restore-steps")
    assert page.text("#restore-body .restore-mode:first-child b") == "Sobrescribir"
    # 覆盖之前在原地再问一次；默认停在"返回"上
    page.click("#restore-actions [data-focus='go']")
    page.wait("document.querySelector(\"#restore-actions [data-focus='sure']\")", what="覆盖之前的确认")
    assert page.js("document.activeElement.textContent") == "Volver"
    assert page.js("document.getElementById('restore-message').classList.contains('bad')")
    page.click("#restore-actions [data-focus='sure']")
    page.wait("document.querySelector('#restore-body .restore-done')", 60, "恢复好了")
    steps.idle(page)
    # 恢复好了：备份里的设置是中文，页面跟着换回中文；恢复前的自动备份在哪儿、可以在访达中显示
    page.wait("document.documentElement.lang === 'zh-CN'", what="恢复的设置里的语言")
    assert page.text("#restore-body .restore-done-title") == "恢复好了"
    assert "zhifox-before-restore-" in page.text("#restore-body .restore-saved-path")
    page.click("#restore-body .restore-saved .icon-button")
    page.wait("document.getElementById('restore-message').textContent.includes('zhifox-before-restore-')", what="显示了自动备份")
    assert page.js("window.__notReloaded === true")
    status, memory = page.demo.api("/api/memory/read", {"path": "从备份来的.md"})
    assert status == 200 and memory["text"] == "备份里的一条记忆"
    page.click("#restore-actions .button.primary")
    page.wait(f"!{steps.is_open('restore-dialog')}", what="窗口关上")
    # 恢复以后还能接着聊：事件流、模型的连接都换到了新的程序上
    steps.send(page, "[ui:hello] 恢复以后还在吗")
    steps.reply(page, "")
    assert page.js("window.__notReloaded === true")


def test_closing_after_the_upload_asks_first(page):
    raw = steps.backup_bytes(page.demo)
    for close in ("Escape", "#close-restore", "#restore-actions .cancel"):
        steps.open_restore(page)
        steps.choose_backup(page, raw)
        # 第一下：只在窗口里提醒（浮在页面上的提示会被暗底挡住），窗口还开着
        if close == "Escape":
            page.key("Escape")
        else:
            page.click(close)
        page.wait("document.getElementById('restore-message').classList.contains('bad')", what="先提醒一次")
        assert page.js(steps.is_open("restore-dialog"))
        assert "再按一次" in page.text("#restore-message")
        # 第二下才关上
        if close == "Escape":
            page.key("Escape")
        else:
            page.click(close)
        page.wait(f"!{steps.is_open('restore-dialog')}", what="第二下关上")
        steps.idle(page)
    # 点窗口外面的暗处也一样先问
    steps.open_restore(page)
    steps.choose_backup(page, raw)
    page.click_at(4, 4)
    page.wait("document.getElementById('restore-message').classList.contains('bad')", what="点外面也先提醒一次")
    assert page.js(steps.is_open("restore-dialog"))
    # 换一种方式算是"动过了"：提醒收起来，要重新提醒
    page.click("#restore-body .restore-mode:last-child")
    page.wait("!document.getElementById('restore-message').textContent", what="动过以后提醒收起来")
    assert page.text("#restore-actions [data-focus='go']") == "合并进来"
    steps.close_restore(page)


def test_nothing_uploaded_closes_at_once_and_a_bad_file_says_why(page):
    page.allow_errors("/api/restore/upload")  # 故意传一个不是压缩包的文件：助手程序回答 409
    steps.open_restore(page)
    steps.escape_closes(page, "restore-dialog")  # 还什么都没选：直接关上
    steps.open_restore(page)
    page.js(
        "(() => { const box = new DataTransfer(); box.items.add(new File(['这不是压缩包'], 'notes.zip', { type: 'application/zip' }));"
        " const input = document.getElementById('restore-file'); input.files = box.files; input.dispatchEvent(new Event('change')); })()"
    )
    page.wait("document.querySelector('#restore-body .restore-note.bad')", 20, "出错写在窗口里")
    assert "zip" in page.text("#restore-body .restore-note.bad")
    assert page.js("document.getElementById('restore-dialog').contains(document.activeElement)")
    page.click("#restore-actions .button.primary")  # 重新选一个
    page.wait("document.querySelector('#restore-body .restore-drop')", what="回到选文件")
    steps.escape_closes(page, "restore-dialog")


def test_a_backup_too_big_for_the_page_is_stopped_before_uploading(page):
    steps.open_restore(page)
    page.wait("'ready' in document.getElementById('restore-dialog').dataset", what="知道上传最多多大")
    limit = page.demo.api("/api/restore/info")[1]["max_bytes"]
    # 不真的做一个几 GB 的文件：File 的 size 只看它说自己有多大
    page.js(
        "(() => { const file = new File(['x'], 'huge.zip'); Object.defineProperty(file, 'size', { value: " + json.dumps(limit + 1) + " });"
        " const box = new DataTransfer(); box.items.add(file); const input = document.getElementById('restore-file');"
        " Object.defineProperty(input, 'files', { value: [file], configurable: true }); input.dispatchEvent(new Event('change')); })()"
    )
    page.wait("document.querySelector('#restore-body .restore-note.bad')", what="上传之前就拦下")
    assert "harness restore" in page.text("#restore-body .restore-note.bad")
    steps.escape_closes(page, "restore-dialog")
