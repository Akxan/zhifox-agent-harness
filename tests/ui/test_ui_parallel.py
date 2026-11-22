"""几段对话同时进行（page/scripts/live.js、styles/live.css）：你正看着的那一段在跑时，打开别的会话、开新对话，它留在后台接着跑。

查的是页面这一侧：会话栏上每一行的标记（正在进行 / 等你确认 / 出错了 / 有新回复）、后台那一段悬停时的"停止"、
主区域顶上那条"在等你确认"、换过去看一段写到一半的回复、后台的提醒（系统通知、标签页的标记、页面上的一句）、
到了上限不发、三种语言、窄屏和深浅两套外观、减少动态效果。程序这一侧（几段同时跑、上限、只停一段）在 tests/test_parallel_chats.py。

要让一段对话"在后台、正在跑"，又不能赌时机（演示的回复几十毫秒就到了确认那一步，快的机器上来不及换走），这里用一个钩子当闸门：
一条 UserPromptSubmit 钩子（Live.gate），消息里写着 [gate:名字]、闸门文件还在的时候，它就一直等着（这一段在跑、还没问模型）；
测试删掉闸门文件（Live.release），它才往下走。所以"等你确认""出错了""有新回复"可以在你正看着别的会话时才发生，
"正在进行"可以一直停在那里。钩子只是测试的工具：演示服务器每个测试开始前删掉 hooks.json（见 conftest.py 的 reset）。

conftest 每个测试开始前只停你正看着的那一段、再开一段新对话；后台的那几段它不管。所以每个测试结束时（Live.quiet）
打开所有的闸门、停下每一段还在跑的、等它们都收完尾，后台跑完了的那几段打开看一眼（去掉会话栏上的标记），
不让它们留到下一个测试里（比如标签页上一直标着"等你确认"）。服务器上现在的样子从另开的一条事件流里读（_snapshot）。
"""

from __future__ import annotations

import itertools
import json
import urllib.request

import pytest

from . import checks, steps
from .pilot import PHONE_WIDTH, until

ALLOW = "#log .confirm:not(.done) .button.primary"
ROWS = "#conversation-rows .conv"
MARKS = ("running", "confirm", "error", "done")
WORDS = {"running": "正在进行", "confirm": "等你确认", "error": "出错了", "done": "有新回复"}
# _stock 起的标题：故意不含上面那几个词。checks.untranslated 把会话标题里的字都算作"认得的"，标题里要是写着"出错了"，
# 没翻译的标记也会被当成标题的一部分放过去
LABELS = {"running": "一直没停的那段", "confirm": "要批准的那段", "error": "会失败的那段", "done": "后台答完的那段"}

# 闸门：消息里写着 [gate:名字]、文件夹里还有这个名字的文件，就一直等（最多 100 秒，钩子自己的时限是 120 秒）。
# 读的是交给钩子的 JSON 里的 prompt（你写的那句话，见 hooks/engine.py 的 prompt）。按字节读：Windows 上标准输入不一定是 UTF-8
GATE = r"""import json, re, sys, time
from pathlib import Path
said = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}").get("prompt", "")
found = re.search(r"\[gate:(\w+)\]", said)
gate = Path(sys.argv[1]) / found.group(1) if found else None
deadline = time.monotonic() + 100
while gate is not None and gate.exists() and time.monotonic() < deadline:
    time.sleep(0.05)
"""

# 页面上出现过的每一句 toast（同一句连着出现只记一次）
RECORD_TOASTS = (
    "window.__toasts = [];"
    "new MutationObserver(() => { const box = document.getElementById('toast');"
    "  if (box.classList.contains('show') && window.__toasts[window.__toasts.length - 1] !== box.textContent) window.__toasts.push(box.textContent); })"
    ".observe(document.getElementById('toast'), { attributes: true, childList: true, characterData: true, subtree: true });"
)

# 一边输出一边记下最后那条回复的长度，和它有没有带着 streaming（正在输出）
WATCH_REPLY = (
    "window.__lengths = []; window.__streamed = false;"
    "window.__watch = new MutationObserver(() => {"
    "  const node = [...document.querySelectorAll('#log .msg.assistant')].pop();"
    "  if (!node) return;"
    "  window.__streamed = window.__streamed || node.classList.contains('streaming');"
    "  const size = node.textContent.length;"
    "  if (window.__lengths[window.__lengths.length - 1] !== size) window.__lengths.push(size);"
    "});"
    "window.__watch.observe(document.getElementById('log'), { subtree: true, childList: true, characterData: true, attributes: true });"
)

_SEQ = itertools.count(1)  # 同一个演示服务器上，每个测试起的标题都不一样（会话栏按标题找那一行）


def _row(title: str) -> str:
    """会话栏里标题是 title 的那一行（按"打开"按钮上的 title 属性找，它就是会话的完整标题）。"""
    return f"{ROWS}:has(> .conv-main[title={json.dumps(title, ensure_ascii=False)}])"


def _q(selector: str) -> str:
    """一个 JavaScript 表达式：选中的第一个元素。"""
    return f"document.querySelector({json.dumps(selector, ensure_ascii=False)})"


def _translation(page, code: str, text: str) -> str:
    if code == "zh":
        return text
    return page.js(f"JSON.parse(document.getElementById('i18n').textContent).tables[{code!r}][{json.dumps(text, ensure_ascii=False)}]")


def _snapshot(demo) -> tuple[dict, bool]:
    """服务器此刻的样子：另开一条事件流（和页面刚连上时一样），读到 synced 为止。返回（最后那张 state，你正看着的那一段
    是不是正在一轮中间）。state 里有 live（会话编号 → 标记）、parallel（上限和几段在跑）、conversations（会话列表）。

    后台的几段，标记是准的：它们一轮结束时会再发一次状态（见 sessions_api.py 的 _settle）。你正看着的那一段一轮结束以后没有再发，
    最后那张 state 里它还写着 running（页面不看它自己的标记，看不出来），所以它在不在跑按它的日志算：最后一个 turn_start 后面有没有 turn_end。"""
    request = urllib.request.Request(f"{demo.origin}/events?token={demo.token}", headers={"Accept": "text/event-stream"})
    state: dict = {}
    busy = False
    with urllib.request.urlopen(request, timeout=15) as stream:
        for raw in stream:
            line = raw.decode("utf-8").rstrip("\r\n")
            if line == "event: synced":
                break
            if line.startswith("data: "):
                event = json.loads(line[len("data: "):])
                kind = event.get("type")
                if kind == "state":
                    state = event
                elif kind in ("turn_start", "turn_end", "reset"):
                    busy = kind == "turn_start"
    return state, busy


def _behind(state: dict) -> dict[str, str]:
    """后台那几段的标记（你正看着的那一段不算，见 _snapshot）。"""
    return {conversation: mark for conversation, mark in state.get("live", {}).items() if conversation != state.get("conversation") and mark}


class Live:
    """一个测试里的几段对话：起标题、开新对话、闸门、读服务器的状态、最后收尾。"""

    def __init__(self, page):
        self.page = page
        self.gates = page.demo.root / "ui-gates"
        self.gates.mkdir(exist_ok=True)
        self._hooked = False

    def name(self, words: str) -> str:
        """一条消息（也是会话的标题：不超过 40 个字）。后面带一个编号，同一个演示服务器上不会重复。"""
        return f"{words} #{next(_SEQ)}"

    def fresh(self) -> None:
        """点"新对话"，等页面换成一段空的新对话。正在跑的那一段留在后台。"""
        page = self.page
        narrow = page.js(f"{steps.NARROW_NAV} && document.body.classList.contains('nav-closed')")
        page.click("#new-compact" if narrow else "#new")
        page.wait(
            "!document.querySelector('#log .msg') && document.getElementById('main').classList.contains('fresh') && !document.body.classList.contains('busy')",
            what="换成一段空的新对话",
        )
        steps.idle(page)

    def gate(self, name: str) -> None:
        """关上一道闸门：之后写着 [gate:name] 的消息，发出去以后停在"你的钩子"那一步，直到 release(name)。"""
        (self.gates / name).write_text("closed", encoding="utf-8")
        if not self._hooked:
            steps.save_hook(self.page, "ui-gate", GATE, extra=(str(self.gates),), event="UserPromptSubmit", matcher="", timeout=120)
            self._hooked = True

    def release(self, name: str) -> None:
        (self.gates / name).unlink(missing_ok=True)

    def state(self) -> dict:
        return _snapshot(self.page.demo)[0]

    def id_of(self, title: str) -> str:
        found = [item["id"] for item in self.state()["conversations"] if item["title"] == title]
        assert len(found) == 1, f"会话列表里应该正好有一段叫“{title}”：{found}"
        return found[0]

    def wait_mark(self, title: str, mark: str, timeout: float = 20) -> None:
        """等会话栏里这一行带上这个标记。"""
        self.page.wait(f"{_q(_row(title) + ' .conv-live-tag.' + mark)}", timeout, f"“{title}”标上 {mark}")

    def quiet(self) -> None:
        """收尾（见文件开头）：打开所有的闸门，停下每一段还在跑的，等它们都收完尾；后台跑完了的打开看一眼，标记去掉。"""
        for gate in self.gates.iterdir():
            gate.unlink(missing_ok=True)
        demo = self.page.demo

        def settled() -> bool:
            state, busy = _snapshot(demo)
            running = [conversation for conversation, mark in _behind(state).items() if mark in ("running", "confirm")]
            if busy:
                running.append(state["conversation"])
            for conversation in running:
                demo.api("/api/stop", {"conversation": conversation})
            return not running

        until(settled, timeout=30, what="每一段都停下、收完尾")
        for conversation in _behind(_snapshot(demo)[0]):  # 剩下的都是后台跑完了的（出错了、有新回复）
            _open(demo, conversation)
        leftover = _behind(_snapshot(demo)[0])
        assert leftover == {}, f"收尾以后后台还有标记：{leftover}"


def _open(demo, conversation: str) -> None:
    """打开一段会话（直接调接口）。正好有别的请求在换着看（比如页面刚发来的）会回答"请先等"：等一下再试；
    别的错误（比如这段会话不在磁盘上）照样算失败。"""
    answers: list = []

    def opened() -> bool:
        answers.append(demo.api("/api/conversation/open", {"id": conversation}))
        return answers[-1][0] == 200 or "请先等" not in str(answers[-1][1])

    until(opened, timeout=15, what="打开后台跑完的那一段")
    assert answers[-1][0] == 200, f"打开后台跑完的那一段（{conversation}）失败：{answers[-1]}"


@pytest.fixture
def live(page):
    helper = Live(page)
    try:
        yield helper
    finally:
        helper.quiet()


def _titled(page, sent: str) -> str:
    """发出第一条消息以后，等顶上的标题换成它（状态比消息晚一点到），返回标题：太长的服务器截短了（40 个字，末尾一个省略号）。"""
    return page.wait(
        f"(t => t && t !== '新对话' && {json.dumps(sent, ensure_ascii=False)}.startsWith(t.replace(/…$/, '')) && t)(document.getElementById('title').textContent)",
        what="标题换成第一句话",
    )


def _failing(live: Live, words: str) -> tuple[str, str]:
    """一段存过盘的 [ui:flaky] 对话（第一句失败了一次，点"重新生成"答上了），第二句写在闸门后面：放行以后它一定失败一次。
    返回（标题，闸门的名字）。为什么不直接让第一句在后台失败：第一条消息就失败的会话不存盘（app.save_conversation 当它没发生过），
    会话栏上根本没有它那一行，它的"出错了"却留在状态的 live 里，打开它又说"这段会话不存在"（2026-11-22 写这些测试时看到的样子，
    是程序那一侧的事：sessions.finish 记下标记、又把这一段从名单里拿掉）"""
    page = live.page
    title = live.name(f"[ui:flaky] {words}")
    steps.send(page, title)
    page.wait("!document.body.classList.contains('busy') && document.querySelector('#log .notice.error + .turn-actions .turn-retry')", 20, "第一次失败，有重新生成")
    page.click("#log .turn-actions .turn-retry")
    steps.reply(page, "这次成功了（第 1 条消息）")
    gate = f"fail{next(_SEQ)}"
    live.gate(gate)
    steps.send(page, f"[gate:{gate}] 再问一句")
    return _titled(page, title), gate


def _stock(live: Live, marks=MARKS, long: bool = False) -> dict[str, str]:
    """会话栏上每种标记各一段（marks 里的那几种），你正看着的是一段空的新对话。返回 标记 → 那一段的标题。

    出错了：一段 [ui:flaky] 的对话，第二句在闸门后面，换走以后才放行（见 _failing）；有新回复：[ui:hello] 也在闸门后面，在后台答完；
    等你确认：[ui:approve] 停在确认上再换走；正在进行：[ui:hello] 停在闸门上（这一段一直在跑，测试结束时才放行）。
    同时在跑的最多两段（等你确认、正在进行），不到上限。long：标题故意写得很长（看窄屏上省略号收不收得住）"""
    page = live.page
    tail = "，标题故意写得很长很长，好看看它在窄屏上会不会冲出会话栏" if long else ""
    titles: dict[str, str] = {}
    for mark in ("error", "done", "confirm", "running"):
        if mark not in marks:
            continue
        if mark == "error":
            title, gate = _failing(live, LABELS[mark] + tail)
        else:
            script = {"done": "[ui:hello]", "confirm": "[ui:approve]", "running": "[ui:hello]"}[mark]
            gate = "" if mark == "confirm" else mark
            words = live.name(f"{script}{f' [gate:{gate}]' if gate else ''} {LABELS[mark]}{tail}")
            if gate:
                live.gate(gate)
            steps.send(page, words)
            title = _titled(page, words)
        if mark == "confirm":
            steps.pending_confirm(page)
        live.fresh()
        if mark in ("error", "done"):
            live.release(gate)
        live.wait_mark(title, mark)
        titles[mark] = title
    return titles


# ---- 1. 会话栏上的标记、顶上那条提示 ----------------------------------------------------


def test_a_conversation_waiting_in_the_background_is_marked_and_the_alert_takes_you_back(live):
    page = live.page
    page.js(RECORD_TOASTS)
    title = live.name("[ui:approve] 在后台要批准")  # 标题里不写"等你确认"这些标记上的词（见 LABELS）
    steps.send(page, title)
    steps.pending_confirm(page)
    # 这一段正停在确认上："新对话"照样能点（以前在跑的时候是灰的）
    assert not page.js("document.getElementById('new').disabled")
    live.fresh()
    assert page.text("#title") == "新对话"
    row = _row(title)

    # 会话栏：那一行标着"等你确认"，左边一道竖条；你正看着的这一段没有标记
    live.wait_mark(title, "confirm")
    assert page.text(f"{row} .conv-live-tag") == "等你确认"
    assert page.js(f"{_q(row)}.classList.contains('live-confirm')")
    assert page.js(f"getComputedStyle({_q(row)}, '::before').content") == '""'
    assert not page.exists(f"{ROWS}.active .conv-live-tag")
    # 顶上那条提示：写着它的标题，点一下打开它
    page.wait("!document.getElementById('chat-alert').hidden", what="顶上出现“在等你确认”")
    assert page.text("#chat-alert-text") == f"「{title}」在等你确认"
    # 换着看的那一下不算新消息：它在你换走之前就在等了，不再说一遍
    assert page.js("window.__toasts") == []

    # 悬停那一行：第一个按钮是"停下这一段"，然后是"在访达中显示"；改名、删除这时做不了，藏起来
    page.hover(row)
    page.wait(f"{checks.opacity_of(row + ' .conv-actions', upto=ROWS)} > 0.95", what="悬停时按钮出现")
    shown = page.js(
        f"[...{_q(row)}.querySelectorAll('.conv-actions button')].filter((b) => b.getClientRects().length)"
        ".map((b) => [b.className, b.querySelector('use').getAttribute('href'), b.getAttribute('aria-label')])"
    )
    assert [href for _, href, _ in shown] == ["#i-stop", "#i-folder"], shown
    assert "conv-stop" in shown[0][0] and shown[0][2] == "停下这一段"
    page.reach(f"{row} .conv-stop")

    # 点顶上那条提示：回到那一段，确认卡片重放出来、还在等你；答复以后这一轮照常做完
    page.click("#chat-alert")
    page.wait(f"document.getElementById('title').textContent === {json.dumps(title, ensure_ascii=False)}", what="回到那一段")
    steps.pending_confirm(page)
    assert "echo zhifox-ui-test" in page.text("#log .confirm pre")
    page.wait("document.getElementById('chat-alert').hidden", what="提示收起来（它就在眼前了）")
    assert not page.exists(f"{row} .conv-live-tag")
    assert page.js(f"{_q(row)}.classList.contains('active')")
    page.click(ALLOW)
    steps.reply(page, "命令执行完了")
    assert page.js("window.__toasts") == []


# ---- 2. 换过去看一段写到一半的回复 ------------------------------------------------------


def test_switching_back_to_a_reply_halfway_through_keeps_it_streaming(live):
    page = live.page
    # [midgate:名字]：演示的假模型把回复发到一半就停下，等闸门打开再接着发（见 tests/demo_web.py）。这样换走、换回来的时候
    # 回复一定正写到一半，不用赶在它写完之前点完（慢的机器上赶不上：换回来的时候它已经写完了，或者还没开始）
    gate = live.gates / "half"
    gate.write_text("closed", encoding="utf-8")
    title = live.name("[ui:hello] [midgate:half] 换回来接着看")
    steps.send(page, title)
    page.wait("[...document.querySelectorAll('#log .msg.assistant')].some((node) => node.textContent.length > 0)", what="回复开始出现")
    live.fresh()  # 换成一段空的新对话：写了一半的那一段留在后台，停在闸门上
    page.js(WATCH_REPLY)
    page.click(f"{_row(title)} .conv-main")
    page.wait(f"document.getElementById('title').textContent === {json.dumps(title, ensure_ascii=False)}", what="换回那一段")
    # 重放出来的是写了一半的那一段：有字，还没写完，还在"正在输出"
    page.wait("[...document.querySelectorAll('#log .msg.assistant')].some((node) => node.textContent.length > 0)", what="写了一半的回复重放出来")
    assert "详细的说明在" not in page.text("#log .msg.assistant"), "闸门还关着：回复应该只写了一半"
    gate.unlink()  # 闸门打开：后一半接着一小段一小段地来
    steps.reply(page, "详细的说明在")
    lengths = [size for size in page.js("window.__watch.disconnect(), window.__lengths") if size]
    # 重放出来的是写了一半的那一段（不是整段），之后一小段一小段接着长，最后是完整的回复
    assert len(lengths) >= 3 and lengths == sorted(lengths), f"回复应该接着一小段一小段地出现，实际的长度变化：{lengths}"
    assert lengths[0] < lengths[-1], f"重放出来的应该是写了一半的回复：{lengths}"
    assert page.js("window.__streamed"), "接着写的时候，那条回复带着 streaming（正在输出）"
    assert page.text("#log .msg.assistant").startswith("收到第 1 条消息。")
    assert page.js("document.querySelectorAll('#log .msg.assistant ol li').length") == 2
    assert not page.exists("#log .msg.assistant.streaming"), "写完了就不再是正在输出的样子"
    assert page.js("document.querySelectorAll('#log .msg.assistant').length") == 1, "同一段回复不能画成两条"


# ---- 3. 后台的提醒 ----------------------------------------------------------------------


def test_a_conversation_that_starts_waiting_in_the_background_notifies_but_a_replay_does_not(live):
    page = live.page
    steps.fake_notifications(page, keep=True, hidden=True)  # 刷新以后的页面一开始就在后台（看重放弹不弹）
    title = live.name("[ui:approve] [gate:ask] 后台才问")
    live.gate("ask")
    steps.send(page, title)
    live.fresh()
    live.wait_mark(title, "running")
    assert page.text(f"{_row(title)} .conv-live-tag") == "正在进行"
    assert page.js("document.getElementById('chat-alert').hidden")

    # 页面在后台、你正看着别的会话时，它开始等你确认：弹一条一直留着的通知，标签页上标出"等你确认"
    steps.background(page)
    live.release("ask")
    page.wait("window.__notes.length === 1", what="弹了一个通知")
    [note] = steps.notes(page)
    assert note["title"] == f"「{title}」在等你确认"
    assert note["body"] == "点这里打开这段对话，在里面答复。"
    assert note["urgent"] and note["tag"] == "harness-chat-confirm-" + live.id_of(title)
    assert page.js("document.title").startswith("● 等你确认 · ")
    live.wait_mark(title, "confirm")

    # 点这条通知：窗口回来，打开那一段，确认卡片就在里面
    page.js("window.__notes[0].onclick({ preventDefault() {} })")
    assert page.js("window.__focused") == 1
    page.wait(f"document.getElementById('title').textContent === {json.dumps(title, ensure_ascii=False)} && !document.title.startsWith('●')", what="打开那一段")
    steps.pending_confirm(page)
    assert page.js("window.__notes[0].closed")

    # 再换走，然后刷新（页面一开始就在后台）：它还在等，但这是现在的样子，不是新消息，不再弹；标签页照样标着
    live.fresh()
    page.reload()
    page.wait("document.body.classList.contains('ready') && !document.getElementById('chat-alert').hidden", what="重放完、顶上那条提示在")
    live.wait_mark(title, "confirm")
    assert page.js("document.hidden") is True
    assert steps.notes(page) == []
    assert page.js("document.title").startswith("● 等你确认 · ")
    # 回到眼前：标签页上的标记去掉（顶上那条提示还在）
    steps.background(page, False)
    page.wait("!document.title.startsWith('●')", what="标签页的标记去掉")
    assert not page.js("document.getElementById('chat-alert').hidden")


def test_a_conversation_that_starts_waiting_while_the_page_is_in_front_says_so_on_the_page(live):
    page = live.page
    steps.fake_notifications(page)
    page.js(RECORD_TOASTS)
    title = live.name("[ui:approve] [gate:front] 眼前问")
    live.gate("front")
    steps.send(page, title)
    live.fresh()
    live.wait_mark(title, "running")
    live.release("front")
    # 页面就在眼前、你正看着别的会话：页面上说一声（toast），顶上那条提示也出来；不弹系统通知，标签页不标
    page.wait(f"window.__toasts.includes({json.dumps(f'「{title}」在等你确认', ensure_ascii=False)})", what="页面上说了一声")
    page.wait("!document.getElementById('chat-alert').hidden", what="顶上出现“在等你确认”")
    page.wait(f"document.getElementById('announce').textContent === {json.dumps(f'「{title}」在等你确认', ensure_ascii=False)}", what="读屏软件也播报一句")
    assert steps.notes(page) == []
    assert not page.js("document.title").startswith("●")


def test_background_errors_and_replies_notify_once_and_opening_one_clears_its_mark(live):
    page = live.page
    steps.fake_notifications(page)
    failing, fail = _failing(live, "后台出错")
    live.fresh()
    live.wait_mark(failing, "running")
    answering = live.name("[ui:hello] [gate:answer] 后台答完")
    live.gate("answer")
    steps.send(page, answering)
    live.fresh()
    live.wait_mark(answering, "running")

    steps.background(page)
    live.release(fail)
    live.wait_mark(failing, "error")
    page.wait("window.__notes.length === 1", what="出错的通知")
    live.release("answer")
    live.wait_mark(answering, "done")
    page.wait("window.__notes.length === 2", what="答完的通知")
    failed, answered = steps.notes(page)
    assert (failed["title"], failed["body"], failed["urgent"]) == (f"「{failing}」出错了", "点这里打开这段对话。", False)
    assert (answered["title"], answered["body"], answered["urgent"]) == (f"「{answering}」回复完了", "点这里打开这段对话。", False)
    assert failed["tag"] == "harness-chat-" + live.id_of(failing)
    # 标签页：出错了比"做完了"要紧，一直标着出错
    assert page.js("document.title").startswith("● 出错了 · ")
    assert page.text(f"{_row(failing)} .conv-live-tag") == "出错了"
    assert page.text(f"{_row(answering)} .conv-live-tag") == "有新回复"
    assert page.js("document.getElementById('chat-alert').hidden"), "顶上那条提示只给等你确认的"

    # 回到眼前，打开答完的那一段：回复整段在里面，它的标记去掉；出错的那一段还标着
    steps.background(page, False)
    page.click(f"{_row(answering)} .conv-main")
    page.wait(f"document.getElementById('title').textContent === {json.dumps(answering, ensure_ascii=False)}", what="打开答完的那一段")
    steps.reply(page, "收到第 1 条消息")
    page.wait(f"!{_q(_row(answering) + ' .conv-live-tag')}", what="打开看过的那一段不再标着")
    assert page.text(f"{_row(failing)} .conv-live-tag") == "出错了"
    assert len(steps.notes(page)) == 2


# ---- 4. 后台那一段的"停止" ---------------------------------------------------------------


def test_stopping_a_background_conversation_leaves_the_one_you_are_looking_at_alone(live):
    page = live.page
    behind = live.name("[ui:approve] 在后台被停下")
    front = live.name("[ui:approve] 眼前这一段")
    steps.send(page, behind)
    steps.pending_confirm(page)
    live.fresh()
    steps.send(page, front)
    steps.pending_confirm(page)
    row = _row(behind)
    live.wait_mark(behind, "confirm")
    assert not page.js("document.getElementById('chat-alert').hidden")

    page.hover(row)
    page.click(f"{row} .conv-stop")
    # 你自己停下的不标"出错了""有新回复"：那一行回到平常的样子，顶上那条提示也收起来
    page.wait(f"{_q(row)} && !{_q(row)}.className.includes('live-') && !{_q(row + ' .conv-live-tag')}", what="停下的那一段不再标着")
    page.wait("document.getElementById('chat-alert').hidden", what="提示收起来")
    stopped = live.id_of(behind)
    until(lambda: stopped not in _behind(live.state()), what="服务器上它也不再标着")
    # 眼前这一段不受影响：卡片还在等你，答复以后照常做完
    assert page.js("document.body.classList.contains('busy')")
    assert page.js("document.querySelectorAll('#log .confirm:not(.done) .button:not(:disabled)').length") == 2
    page.click(ALLOW)
    steps.reply(page, "命令执行完了")
    # 回去看停下的那一段：卡片写着"已中断"
    page.click(f"{row} .conv-main")
    page.wait(f"document.getElementById('title').textContent === {json.dumps(behind, ensure_ascii=False)} && document.querySelector('#log .confirm.done')", what="打开停下的那一段")
    assert page.text("#log .confirm.done .outcome") == "已中断"


# ---- 5. 到了上限 ------------------------------------------------------------------------


def test_at_the_limit_a_new_message_is_kept_and_not_sent(live):
    page = live.page
    limit = live.state()["parallel"]["max"]
    assert limit == 3, "演示用的是默认的上限（config.MAX_PARALLEL_CHATS）"
    for n in range(limit):
        steps.send(page, live.name(f"[ui:approve] 同时进行的第 {n + 1} 段"))
        steps.pending_confirm(page)
        live.fresh()
    page.wait("document.getElementById('chat-alert-text').textContent === '3 段对话在等你确认'", what="顶上数着几段在等你")
    page.js(RECORD_TOASTS)
    page.click("#input")
    page.type("[ui:hello] 第四段")
    page.wait("document.getElementById('input').value === '[ui:hello] 第四段'", what="打的字进了输入框")
    page.key("Enter")
    expected = "已经有 3 段对话在同时进行，这是上限。等其中一段结束，或者先在会话栏上停下一段，再发。"
    page.wait(f"window.__toasts.includes({json.dumps(expected, ensure_ascii=False)})", what="说明为什么不发")
    # 没发出去：对话里没有这条消息，写的字留在输入框里，服务器上还是三段在跑
    assert not page.exists("#log .msg.user")
    assert page.js("document.getElementById('input').value") == "[ui:hello] 第四段"
    assert not page.js("document.body.classList.contains('busy')")
    assert live.state()["parallel"]["running"] == limit


# ---- 6. 三种语言 ------------------------------------------------------------------------


def test_marks_and_the_alert_translate_in_place(live):
    page = live.page
    titles = _stock(live)
    page.hover(_row(titles["running"]))  # "停下这一段"也要翻译（悬停才出现，checks 照样会看它的 aria-label）
    page.js("window.__alive = true")
    for code in ("en", "es", "zh"):
        steps.language(page, code)
        assert page.js("window.__alive === true"), "换语言时页面刷新了"
        for mark, title in titles.items():
            assert page.text(f"{_row(title)} .conv-live-tag") == _translation(page, code, WORDS[mark]), f"{code}：{mark} 的标记"
        alert = _translation(page, code, "「{title}」在等你确认").replace("{title}", titles["confirm"])
        assert page.text("#chat-alert-text") == alert
        stop = page.js(f"{_q(_row(titles['running']) + ' .conv-stop')}.getAttribute('aria-label')")
        assert stop == _translation(page, code, "停下这一段")
        if code != "zh":
            assert checks.untranslated(page, ["#nav", "#chat-alert"]) == []


# ---- 7. 窄屏、深浅两套外观 --------------------------------------------------------------

# 在同一个位置的三条提示：定时任务、聊天通道的那两条照它们自己的写法临时显示出来（一句长长的话），落下来的动画直接播完
# （量的是落定以后的样子），量完原样藏回去。一口气做完、中间不让出线程，所以状态来了也不会在量到一半时重画。返回每条的边
_STACKED = r"""
((which) => {
  const ids = { task: 'task-alert', chan: 'chan-alert' };
  const texts = { task: 'task-alert-text', chan: 'chan-alert-text' };
  const words = { task: '定时任务“每周五下午整理一周的进展并发给团队”在等你确认', chan: 'Telegram 上的一个请求在等你确认：执行一条很长很长的命令' };
  const saved = which.map((name) => [name, document.getElementById(ids[name]).hidden, document.getElementById(texts[name]).textContent]);
  for (const name of which) {
    const node = document.getElementById(ids[name]);
    node.hidden = false;
    document.getElementById(texts[name]).textContent = words[name];
    node.getAnimations().forEach((animation) => animation.finish());
  }
  const boxes = ['chat-alert', ...which.map((name) => ids[name])].map((id) => {
    const box = document.getElementById(id).getBoundingClientRect();
    return { id, top: box.top, bottom: box.bottom, left: box.left, right: box.right };
  });
  for (const [name, hidden, text] of saved) {
    document.getElementById(ids[name]).hidden = hidden;
    document.getElementById(texts[name]).textContent = text;
  }
  return { boxes, width: document.documentElement.clientWidth };
})
"""


def _stacked(page, which: list[str]) -> list[str]:
    """顶上那条"在等你确认"和定时任务、聊天通道的提示一起出现时，有没有叠在一起、伸出窗口。"""
    found = page.js(f"({_STACKED})({json.dumps(which)})")
    boxes, width, problems = found["boxes"], found["width"], []
    for box in boxes:
        if box["left"] < -0.5 or box["right"] > width + 0.5:
            problems.append(f"{box['id']} 伸出了窗口：{box['left']:.0f}..{box['right']:.0f}（窗口 {width}）")
    for one, other in itertools.combinations(boxes, 2):
        if one["top"] < other["bottom"] - 0.5 and other["top"] < one["bottom"] - 0.5:
            problems.append(f"{one['id']} 和 {other['id']} 叠在一起：{one['top']:.0f}..{one['bottom']:.0f} / {other['top']:.0f}..{other['bottom']:.0f}")
    return problems


@pytest.mark.parametrize("code", ["zh", "en", "es"])
def test_marks_and_the_alert_fit_at_phone_width(live, code):
    page = live.page
    titles = _stock(live, long=True)
    page.size(PHONE_WIDTH, 844)
    steps.calm(page)
    if code != "zh":
        steps.language(page, code)
    page.wait("!document.getElementById('chat-alert').hidden", what="顶上那条提示")
    steps.close_nav(page)
    found = {
        "顶上的提示": checks.overflow(page, ["#chat-alert"]) + checks.sticking_out(page, ["#chat-alert"]),
        "三条提示叠在一起": _stacked(page, ["task", "chan"]) + _stacked(page, ["chan"]) + _stacked(page, ["task"]),
    }
    steps.open_nav(page)
    found["会话栏"] = checks.overflow(page, ["#nav"]) + checks.sticking_out(page, [ROWS])
    # 悬停后台那一段：右边多了两个按钮，标题和标记让出位置，照样不冲出去
    for mark in ("running", "confirm"):
        page.hover(_row(titles[mark]))
        found[f"会话栏（悬停{WORDS[mark]}的那一行）"] = checks.overflow(page, [_row(titles[mark])]) + checks.sticking_out(page, [_row(titles[mark])])
    found = {where: items for where, items in found.items() if items}
    assert found == {}, f"{code}：\n" + "\n".join(f"{where}:\n  " + "\n  ".join(items) for where, items in found.items())
    assert page.js("document.documentElement.scrollWidth <= document.documentElement.clientWidth")


# 文字和它背后真正看到的底色的对比度（WCAG）。checks.contrast 只找第一层不透明的底、跳过半透明的那几层，
# 标记的底正是半透明的同色浅底（--ok-soft 这些），跳过它会高估；颜色也可能是 color-mix() 算出来的（计算值写成 color(srgb …)）。
# 所以这里每一种颜色都画到一个 1×1 的画布上读回 sRGB，再从第一层不透明的底往上一层层叠上半透明的底（悬停的那一行、标记自己的底）
_CONTRAST = r"""
((selectors) => {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 1;
  const pen = canvas.getContext('2d', { willReadFrequently: true });
  const rgba = (color) => {
    pen.clearRect(0, 0, 1, 1);
    pen.fillStyle = '#000';
    pen.fillStyle = color;
    pen.fillRect(0, 0, 1, 1);
    const [r, g, b, a] = pen.getImageData(0, 0, 1, 1).data;
    return [r, g, b, a / 255];
  };
  const luminance = ([r, g, b]) => {
    const linear = (c) => (c /= 255) <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
  };
  return selectors.map((selector) => {
    const node = document.querySelector(selector);
    if (!node) return [selector, null];
    const layers = [];
    for (let at = node; at; at = at.parentElement) {
      const color = rgba(getComputedStyle(at).backgroundColor);
      if (color[3] > 0) layers.push(color);
      if (color[3] > 0.9) break;
    }
    let paper = [255, 255, 255];
    for (const [r, g, b, a] of layers.reverse()) paper = [r * a + paper[0] * (1 - a), g * a + paper[1] * (1 - a), b * a + paper[2] * (1 - a)];
    const ink = luminance(rgba(getComputedStyle(node).color)), back = luminance(paper);
    return [selector, Math.round(((Math.max(ink, back) + 0.05) / (Math.min(ink, back) + 0.05)) * 100) / 100];
  });
})
"""


def _contrast(page, selectors: list[str]) -> list[tuple[str, float | None]]:
    return [(selector, ratio) for selector, ratio in page.js(f"({_CONTRAST})({json.dumps(selectors, ensure_ascii=False)})")]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_marks_and_the_alert_read_well_in_both_themes(live, theme):
    page = live.page
    page.js(f"localStorage.setItem('harness.theme', {theme!r})")
    page.reload()
    page.wait(f"document.body.classList.contains('ready') && document.documentElement.dataset.theme === {theme!r}", what="外观换好")
    titles = _stock(live)
    steps.calm(page)
    tags = {mark: f"{_row(title)} .conv-live-tag.{mark} span" for mark, title in titles.items()}
    # 10.5px 的小字：平时、悬停那一行时（底更深一点）都要过 4.5:1
    low = [(where, ratio) for where, ratio in _contrast(page, [*tags.values(), "#chat-alert-text"]) if ratio is None or ratio < 4.5]
    for mark, title in titles.items():
        page.hover(_row(title))
        page.wait(f"getComputedStyle({_q(_row(title))}).backgroundColor !== 'rgba(0, 0, 0, 0)'", what="悬停的那一行换了底色")
        low += [(f"悬停时 {where}", ratio) for where, ratio in _contrast(page, [tags[mark]]) if ratio is None or ratio < 4.5]
    assert low == [], f"{theme}：这些字的对比度不到 4.5:1：{low}"
    # 默认宽度下（两边的栏都展开）也不冲出去、不叠在一起
    assert checks.overflow(page, ["#nav", "#chat-alert"]) == []
    assert _stacked(page, ["task", "chan"]) == []


# ---- 8. 减少动态效果 --------------------------------------------------------------------


def test_the_alert_drops_straight_down(live):
    page = live.page
    _stock(live, marks=("confirm",))
    page.wait("!document.getElementById('chat-alert').hidden", what="顶上那条提示")
    steps.idle(page)
    # 再显示一次（落下来的动画从头播），停在几个时刻量它的中心：一直在正中间，只是从上面落下来。
    # 以前用的是共用的 drop（只写了 translateY），播的时候 translateX(-50%) 也被插值，整条从右边斜着滑进来
    seen = page.js(
        "(() => { const alert = document.getElementById('chat-alert');"
        " alert.hidden = true; void alert.offsetWidth; alert.hidden = false;"
        " const drop = alert.getAnimations().find((a) => a.animationName); drop.pause();"
        " const at = () => { const box = alert.getBoundingClientRect(); return [box.left + box.width / 2, box.top]; };"
        " const frames = [0, 100, 200, 300].map((time) => { drop.currentTime = time; return at(); });"
        " drop.finish(); return { frames, end: at() }; })()"
    )
    end_x, end_y = seen["end"]
    assert all(abs(x - end_x) < 1 for x, _ in seen["frames"]), f"落下来的时候横着动了：{seen}"
    tops = [y for _, y in seen["frames"]] + [end_y]
    assert tops == sorted(tops) and tops[0] < end_y, f"应该是从上面落下来：{seen}"


def _dots(titles: dict[str, str]) -> list[str]:
    return [f"{_row(titles['running'])} .conv-live-dot", f"{_row(titles['confirm'])} .conv-live-dot", "#chat-alert .chat-alert-dot"]


def test_the_dots_hold_still_when_motion_is_reduced(live):
    page = live.page
    titles = _stock(live, marks=("confirm", "running"))
    breathing = "(n => n.getAnimations().some((a) => a.playState === 'running' && a.effect.getComputedTiming().iterations === Infinity))"
    still = "(n => n.getAnimations().every((a) => a.playState !== 'running') && getComputedStyle(n).opacity === '1' && getComputedStyle(n).transform === 'none')"
    for dot in _dots(titles):
        page.wait(f"{breathing}({_q(dot)})", what=f"{dot} 在呼吸")
    # 在外观面板里选"减少"（系统没有要求也一样）：点不再呼吸，停在看得清的样子（不透明、原来的大小）
    page.click("#theme")
    page.click("[data-motion-pick='reduce']")
    page.wait("document.documentElement.dataset.motion === 'reduce'")
    page.key("Escape")
    for dot in _dots(titles):
        page.wait(f"{still}({_q(dot)})", what=f"{dot} 停下来")
    # 之后新画的（会话列表重画、换语言）也不动
    steps.language(page, "en")
    for dot in _dots(titles):
        page.wait(f"{still}({_q(dot)})", what=f"换语言以后 {dot} 也不动")

