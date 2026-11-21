"""对话：回复逐字出现、刷新后整段重放（不播入场动画）；确认卡片的允许、拒绝和 Esc；浏览器的每一步带一张小截图。"""

from __future__ import annotations

from . import steps

# 一边回复一边记下最后那条回复的长度：长度一点点变大，才说明是逐字出现的
WATCH_REPLY = (
    "window.__lengths = [];"
    "window.__entered = false;"
    "window.__watch = new MutationObserver(() => {"
    "  const node = [...document.querySelectorAll('#log .msg.assistant')].pop();"
    "  if (!node) return;"
    "  window.__entered = window.__entered || node.classList.contains('enter');"
    "  const size = node.textContent.length;"
    "  if (window.__lengths[window.__lengths.length - 1] !== size) window.__lengths.push(size);"
    "});"
    "window.__watch.observe(document.getElementById('log'), { subtree: true, childList: true, characterData: true });"
)


def test_reply_streams_then_replays_without_entry_animations(page):
    page.js(WATCH_REPLY)
    steps.send(page, "[ui:hello] 你好")
    steps.reply(page, "收到第 1 条消息")
    lengths = page.js("window.__watch.disconnect(), window.__lengths")
    assert len(lengths) >= 4 and lengths == sorted(lengths), f"回复应该一小段一小段地出现，实际的长度变化：{lengths}"
    assert page.js("window.__entered"), "实时出现的回复要播入场动画（带 enter）"
    # 回复按 Markdown 画：列表是真的列表，网址是能点的链接（只认 http / https）
    assert page.js("document.querySelectorAll('#log .msg.assistant ol li').length") == 2
    assert page.js("[...document.querySelectorAll('#log .msg.assistant a')].map((a) => a.href)") == ["https://example.com/zhifox-ui"]
    assert page.js("document.querySelector('#log .thinking')") is not None  # 思考过程是一块能展开的
    before = page.js("[...document.querySelectorAll('#log > *')].map((node) => node.className.replace(' enter', '') + ':' + node.textContent)")

    # 刷新：服务器把这段对话的事件重发一遍，页面整段画出来，不逐条播入场动画
    page.reload()
    page.wait("document.body.classList.contains('ready') && document.querySelector('#log .msg.assistant')", what="重放完")
    after = page.js("[...document.querySelectorAll('#log > *')].map((node) => node.className + ':' + node.textContent)")
    assert after == before
    assert page.js("document.querySelectorAll('#log .enter').length") == 0
    playing = page.js(
        "document.getAnimations().filter((a) => a.effect.target !== document.getElementById('log')"
        " && document.getElementById('log').contains(a.effect.target)).map((a) => a.effect.target.className)"
    )
    assert playing == [], f"重放出来的旧内容不该一条条播动画：{playing}"
    assert page.js("document.getElementById('empty').hidden")  # 有内容了，不是空白页


def test_approval_card_allow(page):
    steps.send(page, "[ui:approve] 执行一条命令")
    steps.pending_confirm(page)
    assert "echo zhifox-ui-test" in page.text("#log .confirm pre")
    page.wait("document.getElementById('working-text').textContent === '等待你确认'")
    page.click("#log .confirm .button.primary")
    page.wait("document.querySelector('#log .confirm.done .outcome.allowed')", what="卡片上写着已允许")
    steps.reply(page, "命令执行完了")
    assert page.js("document.querySelector('#log .call .status').classList.contains('ok')")
    assert page.js("document.activeElement.id") == "input"  # 答复以后焦点回到输入框，接着就能打字


def test_approval_card_escape_does_not_answer_and_deny_sends_the_reason(page):
    steps.send(page, "[ui:approve] 这次拒绝")
    steps.pending_confirm(page)
    # Esc 不能替你答复：焦点在哪儿都一样（卡片的按钮上、原因的输入框里、页面上）
    for where in ("#log .confirm .button.primary", "#log .confirm .reason", None):
        if where:
            page.focus(where)
        else:
            page.js("document.activeElement.blur()")
        page.key("Escape")  # 答复是同步开始的（按钮马上变灰），所以按完马上看就知道它有没有被答复
        assert page.js("document.querySelectorAll('#log .confirm:not(.done) .button:not(:disabled)').length") == 2, f"焦点在 {where} 时按 Esc，卡片不该被答复"
        assert page.js("document.body.classList.contains('busy')")
    # 拒绝时写的原因会交给助手：在原因框里回车就是拒绝
    page.click("#log .confirm .reason")
    page.type("不需要执行")
    page.key("Enter")
    page.wait("document.querySelector('#log .confirm.done .outcome.denied')", what="卡片上写着已拒绝")
    steps.reply(page, "好的，不执行了")
    assert page.js("document.querySelector('#log .call').classList.contains('failed')")
    result = page.js("[...document.querySelectorAll('#log .call .tool-body pre')].pop().textContent")
    assert "不需要执行" in result


def test_approval_mode_needs_a_second_step_and_escape_keeps_asking(page):
    # "全部自动允许"要多确认一步；这时按 Esc 关掉窗口，档位不变，还是每次都问
    steps.open_dialog(page, "#approval", "approval-dialog")
    page.click("#approval-options .option[data-mode='all']")
    page.wait("!document.getElementById('approval-warning').hidden && document.activeElement.id === 'approval-confirm'")
    steps.escape_closes(page, "approval-dialog")
    assert page.js("document.getElementById('approval').className") == "approval ask"
    steps.send(page, "[ui:approve] 还是要问")
    steps.pending_confirm(page)
    page.click("#log .confirm .button.primary")
    steps.reply(page, "命令执行完了")

    # 真的确认开启以后：不再弹卡片，工具卡片上标着"自动允许"
    steps.open_dialog(page, "#approval", "approval-dialog")
    page.click("#approval-options .option[data-mode='all']")
    page.click("#approval-confirm")
    page.wait("!document.getElementById('approval-dialog').open && document.getElementById('approval').className === 'approval all'")
    page.click("#new")
    page.wait("!document.querySelector('#log .msg')", what="新对话是空的")
    steps.send(page, "[ui:approve] 不用问了")
    steps.reply(page, "命令执行完了")
    assert page.js("document.querySelectorAll('#log .confirm').length") == 0
    assert page.text("#log .call .auto-tag") == "自动允许"


def test_browser_steps_show_a_thumbnail(page):
    # 演示里的"演示商店"：助手用智狐自己启动的 Chrome（真的，无界面）打开本机的一个小网页，读、点、输入、截图
    steps.send(page, "用浏览器看看演示商店里有什么")
    answered = 0
    while True:  # 去新网站、往网页上输入都要先问你：一张张允许
        state = page.wait(
            "document.querySelector('#log .confirm:not(.done) .button.primary:not(:disabled)') ? 'confirm'"
            " : [...document.querySelectorAll('#log .msg.assistant')].some((node) => node.textContent.includes('看完了')) ? 'done' : ''",
            40,
            "下一张确认卡片或者最后的回复",
        )
        if state == "done":
            break
        page.click("#log .confirm:not(.done) .button.primary")
        page.wait("!document.querySelector('#log .confirm:not(.done) .button.primary:not(:disabled)')")
        answered += 1
    assert answered == 2  # 打开一个新网站、在搜索框里输入
    steps.reply(page, "看完了")
    thumbs = page.js("[...document.querySelectorAll('#log .browser-step img')].map((img) => [img.complete, img.naturalWidth, img.src.slice(0, 22)])")
    assert len(thumbs) >= 3
    assert all(done and width > 0 and src == "data:image/jpeg;base64" for done, width, src in thumbs), thumbs
    # 顶栏上写着浏览器正在被智狐控制
    page.wait("!document.getElementById('browser-live').hidden")
    assert page.text("#browser-live-text") == "浏览器正在被智狐控制"
