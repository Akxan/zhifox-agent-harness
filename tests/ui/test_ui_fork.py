"""从某一轮分叉成一段新对话（页面里是 harness/webui/page/scripts/fork.js，背后是 harness/fork.py）。

在真的页面上点：每一轮结束以后两个"分叉"在哪儿、点得到；点了直接换到新的一段、内容对；原会话的文件一个字节都没变；
顶上那一行能点回原会话；在消息上分叉的，那条消息放回输入框。服务器那一侧（前缀、事件、压缩过的、编号对不上）在
tests/test_fork_web.py 和 tests/test_fork.py 里测。

页面的状态包在页面自己的作用域里，测试拿不到会话的编号：按标题认（每个测试的第一句话不一样），会话文件在演示的数据目录里。
"""

from __future__ import annotations

import json
from pathlib import Path

from . import steps
from .pilot import until

SHAPE = (
    "JSON.stringify({ users: [...document.querySelectorAll('#log > .msg.user')].map((n) => n.textContent),"
    " replies: document.querySelectorAll('#log > .msg.assistant').length })"
)
IDLE = "!document.body.classList.contains('busy')"
SHOWN = "!document.getElementById('fork-origin').hidden"


def _shape(page) -> dict:
    return json.loads(page.js(SHAPE))


def _saved(page, title: str) -> Path:
    """标题是 title 的那段会话的文件（等它存好）。"""
    folder = page.demo.root / "data" / "conversations"

    def find() -> Path | None:
        for path in folder.glob("*.json"):
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("title") == title:
                    return path
            except (OSError, ValueError):
                continue  # 正好在写：下一次再看
        return None

    return until(find, timeout=10, what=f"“{title}”存好")


def _two_turns(page, first: str) -> Path:
    """一段两轮的对话，第一句是 first。返回它的文件。"""
    steps.send(page, first)
    steps.reply(page, "收到第 1 条消息")
    steps.send(page, "第二个问题")
    steps.reply(page, "收到第 2 条消息")
    page.wait(IDLE + " && document.querySelectorAll('#log .fork-reply').length === 2", what="两轮上都放好了分叉")
    return _saved(page, first)


def _opened(page, title: str, forked: bool) -> None:
    """等页面换到标题是 title 的那段会话（顶上那一行出现了或者收起来了），这一轮的动画播完。"""
    page.wait(f"document.getElementById('title').textContent === {json.dumps(title)} && {SHOWN if forked else '!' + SHOWN} && {IDLE}", 15, f"换到“{title}”")
    steps.idle(page)


def test_fork_from_a_reply_and_go_back(page):
    first = "[ui:hello] 在回答上分叉"
    source = _two_turns(page, first)
    before = source.read_bytes()
    # 每一轮的回答下面那排按钮里，"分叉"紧挨着"复制"；你的消息左边悬停才出现，不把消息挤动
    assert page.js("[...document.querySelectorAll('#log .msg.assistant .msg-actions')].every((row) => row.children[1] && row.children[1].classList.contains('fork-reply'))")
    width = page.js("document.querySelector('#log > .msg.user').offsetWidth")
    page.hover("#log > .msg.user")
    page.wait("getComputedStyle(document.querySelector('#log > .msg.user > .fork-msg')).opacity === '1'", what="悬停时分叉按钮出现")
    assert page.js("document.querySelector('#log > .msg.user').offsetWidth") == width
    # 最后一条消息左边有"编辑"：分叉排在它左边，不叠在一起
    page.hover("#log > .msg.user", 1)
    page.wait("getComputedStyle(document.querySelector('.turn-edit-open')).opacity === '1'")
    assert page.js(
        "(() => { const a = document.querySelectorAll('#log > .msg.user')[1];"
        " const f = a.querySelector('.fork-msg').getBoundingClientRect(), e = a.querySelector('.turn-edit-open').getBoundingClientRect();"
        " return f.right <= e.left; })()"
    )

    # 在第一轮的回答上分叉：直接换到新的一段，里面只有第一轮；顶上写着从哪儿来的；会话栏里它带着分叉的小图标
    page.hover("#log > .msg.assistant")
    page.click("#log > .msg.assistant .fork-reply")
    forked = first + "（分叉）"
    _opened(page, forked, True)
    assert _shape(page) == {"users": [first], "replies": 1}
    assert "第 1 轮的回答分叉出来" in page.text("#fork-origin") and page.exists("#fork-origin .fork-origin-open")
    assert page.js("document.querySelector('#conversation-rows .conv.active .conv-fork') !== null")
    assert page.js("document.activeElement === document.getElementById('input')")
    assert source.read_bytes() == before  # 原会话一个字节都没变
    saved = json.loads(_saved(page, forked).read_text(encoding="utf-8"))
    assert saved["forked_from"]["id"] == source.stem and saved["forked_from"]["turn"] == 1

    # 接着聊：新的一段从第一轮之后接着说（剧本数的是这段对话里你说了几次）
    steps.send(page, "换个方向")
    steps.reply(page, "收到第 2 条消息")
    assert _shape(page) == {"users": [first, "换个方向"], "replies": 2}
    assert source.read_bytes() == before

    # 点"打开原会话"：回到原来那段，两轮都在，顶上那一行收起来
    page.click("#fork-origin .fork-origin-open")
    _opened(page, first, False)
    assert _shape(page) == {"users": [first, "第二个问题"], "replies": 2}

    # 再打开分叉出来的那一段、刷新：重放出来还是那个样子，按钮照样在，不播入场动画
    page.click(f"#conversation-rows .conv-main[title={json.dumps(forked, ensure_ascii=False)}]")
    _opened(page, forked, True)
    page.reload()
    page.wait(f"document.body.classList.contains('ready') && document.querySelectorAll('#log .fork-reply').length === 2 && {SHOWN}", what="重放完")
    assert page.js("document.querySelectorAll('#log .enter').length") == 0


def test_fork_from_a_message_puts_it_back_in_the_input(page):
    first = "[ui:hello] 在消息上分叉"
    source = _two_turns(page, first)
    page.hover("#log > .msg.user", 1)
    page.click("#log > .msg.user .fork-msg", 1)
    _opened(page, first + "（分叉）", True)
    # 截到第二轮之前：只剩第一轮；第二条消息放回了输入框，焦点在输入框里，光标在最后
    assert _shape(page) == {"users": [first], "replies": 1}
    page.wait("document.getElementById('input').value === '第二个问题'", what="那条消息放回输入框")
    assert page.js("(() => { const box = document.getElementById('input'); return document.activeElement === box && box.selectionStart === box.value.length; })()")
    assert "第 2 轮之前分叉出来" in page.text("#fork-origin")

    # 原会话删掉以后，顶上那一行只剩文字，不能点了
    status, answer = page.demo.api("/api/conversation/delete", {"id": source.stem})
    assert status == 200, answer
    page.wait("document.querySelector('#fork-origin .fork-origin-gone') && !document.querySelector('#fork-origin .fork-origin-open')", what="原会话删掉了")


def test_the_attachments_of_that_message_come_back_too(page):
    # 那条消息带着附件：从原会话的多媒体库里取回原件，和你自己选的文件一样放进输入框（粘贴一个文件，就像从别处复制过来）
    first = "[ui:hello] 附件也放回去"
    steps.send(page, first)
    steps.reply(page, "收到第 1 条消息")
    page.js(
        "(() => { const files = new DataTransfer(); files.items.add(new File(['第一行\\n第二行'], '分叉笔记.md', { type: 'text/markdown' }));"
        " document.getElementById('input').dispatchEvent(new ClipboardEvent('paste', { clipboardData: files, bubbles: true, cancelable: true })); })()"
    )
    page.wait("document.querySelector('#tray .att') && document.getElementById('tray').textContent.includes('分叉笔记.md')", what="附件进了输入框")
    steps.send(page, "看看这个文件")
    steps.reply(page, "收到第 2 条消息")
    page.wait(IDLE + " && document.getElementById('tray').hidden && document.querySelectorAll('#log .fork-msg').length === 2", what="发出去了")
    page.hover("#log > .msg.user", 1)
    page.click("#log > .msg.user .fork-msg", 1)
    _opened(page, first + "（分叉）", True)
    page.wait("document.getElementById('input').value === '看看这个文件' && document.getElementById('tray').textContent.includes('分叉笔记.md')", what="文字和附件都放回输入框")
    assert page.js("document.querySelectorAll('#tray .att').length") == 1 and _shape(page)["users"] == [first]


def test_the_buttons_and_the_note_follow_the_language(page):
    # 按钮上的说明（title、aria-label）和顶上那一行都在原地换语言（语言的检查把对话里的消息当作用户的内容，不看这几个按钮）
    first = "[ui:hello] 换语言"
    _two_turns(page, first)
    page.click("#log > .msg.assistant .fork-reply")
    _opened(page, first + "（分叉）", True)
    steps.language(page, "en")
    labels = "[...document.querySelectorAll('#log .fork-open')].map((n) => n.title + ' | ' + n.getAttribute('aria-label'))"
    assert page.js(labels) == ["Fork from this message (edit and resend) | Fork from this message (edit and resend)", "Fork from here (keep this turn) | Fork from here (keep this turn)"]
    assert page.text("#fork-origin .fork-origin-text") == f"Forked from “{first}” after the reply in turn 1"
    assert page.text("#fork-origin .fork-origin-open") == "Open the original"
    steps.language(page, "es")
    assert page.js("document.querySelector('#log .fork-reply').title") == "Bifurcar desde aquí (con este turno)"
    assert page.text("#fork-origin .fork-origin-open") == "Abrir la original"
    assert page.js("document.querySelector('#conversation-rows .conv.active .conv-fork').title") == "Bifurcada"


def test_no_fork_while_a_turn_is_running(page):
    first = "[ui:approve] 分叉要等这一轮结束"
    steps.send(page, first)
    steps.pending_confirm(page)
    page.click("#log .confirm .button.primary")
    steps.reply(page, "命令执行完了")
    steps.send(page, "再执行一次")
    steps.pending_confirm(page)
    # 这一轮还没结束：它自己还没有"分叉"；点前一轮的分叉，说清楚要先等，不换会话
    assert page.js("document.querySelectorAll('#log .fork-reply').length") == 1
    page.hover("#log > .msg.assistant:has(.fork-reply)")
    page.click("#log > .msg.assistant .fork-reply")
    page.wait("document.getElementById('toast').classList.contains('show') && document.getElementById('toast').textContent.includes('等这一轮对话结束')", what="说要先等这一轮结束")
    assert page.text("#title") == first and page.js("document.getElementById('fork-origin').hidden")
    page.click("#log .confirm:not(.done) .button.primary")
    steps.reply(page, "命令执行完了")
