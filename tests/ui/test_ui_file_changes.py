"""每一轮末尾的"改了 N 个文件"（页面里是 harness/webui/page/scripts/file-changes.js，背后是 harness/undo/）。

在真的页面上点：那一行出现在这一轮的末尾；点开看每个文件和改了什么；撤回一个文件、整轮撤回、重做；
这一轮之后文件被改过、后面的一轮也改过它时，先在原地说清楚，点"仍然撤回"才做；刷新重放还是那个样子；
重新生成之前勾上"同时撤回这一轮改的文件"；三种语言、窄屏、深色。
服务器那一侧（留底、清单、哈希、事件、存盘）在 tests/test_undo*.py 里测。

演示的剧本是 tests/demo_scripts.py 的 files：第一条消息写 "[ui:files] 名字"，第 1 轮在演示的工作目录里
新建 ui-files/名字/计划.md 和 notes.txt（清单里就是这个顺序：第 0 个、第 1 个），之后每一轮把 notes.txt 的"第二行"改一下。
演示服务器是整个测试会话共用的，所以每个测试用自己的名字（_tag），不会改到同一个文件。
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from . import checks, steps
from .pilot import PHONE_WIDTH

PLAN, NOTES = "计划.md", "notes.txt"
NOTES_TEXT = "第一行\n第二行\n第三行\n"  # 第 1 轮写进 notes.txt 的（和 demo_scripts.files 一致）
MINE = "第一行\n我自己又改了一行\n".encode()  # 测试在这一轮之后自己改一下文件（像是你在编辑器里改的）

# 这一轮结束了、没有正在进行的撤回或重做
IDLE = "!document.body.classList.contains('busy') && !document.querySelector('#log .changes.pending')"
FINISHED = IDLE + " && document.querySelector('#log .changes')"
WARNING = "document.querySelector('#log .changes-warn:not([hidden])')"
# 原地的提醒出现了，焦点在"仍然撤回"（或者"仍然重做"）上
WARNING_FOCUSED = WARNING + " && document.activeElement && document.activeElement.matches('#log .changes-warn-actions .button.primary')"

# 每一轮那一行现在的样子，和它后面每一行撤回、重做的记录（比较刷新前后用）。展开着没有不算：刷新以后都是收着的
SHAPE = r"""
JSON.stringify({
  sets: [...document.querySelectorAll('#log .changes')].map((box) => ({
    undone: box.classList.contains('undone'),
    title: box.querySelector('.changes-title').textContent,
    total: box.querySelector('.changes-total').textContent,
    all: [box.querySelector('.changes-all').dataset.action, box.querySelector('.changes-all').textContent],
    files: [...box.querySelectorAll('li.changes-file')].map((li) => ({
      path: li.querySelector('.changes-path').textContent,
      kind: li.querySelector('.changes-kind').className,
      lines: li.querySelector('.changes-lines').textContent,
      undone: li.classList.contains('undone'),
      state: li.querySelector('.changes-state').hidden ? '' : li.querySelector('.changes-state').textContent,
      act: [li.querySelector('.changes-act').dataset.action, li.querySelector('.changes-act').textContent],
    })),
  })),
  logs: [...document.querySelectorAll('#log .changes-log')].map((node) => {
    const redo = node.querySelector('.changes-log-redo');
    return { action: node.classList.contains('redo') ? 'redo' : 'undo', text: node.querySelector('.changes-log-text').textContent, redo: redo ? !redo.hidden : null };
  }),
  order: [...document.querySelectorAll('#log > *')].map((node) => node.classList.contains('changes') ? 'changes'
    : node.classList.contains('changes-log') ? 'log' : node.matches('.msg.user') ? 'user' : node.matches('.msg.assistant') ? 'assistant' : '').filter(Boolean),
})
"""

# 看得见的那个原地提醒：标题、每个文件一项（路径、为什么、是不是做不了）、下面的按钮
WARNING_SHAPE = r"""
((warn) => warn && {
  head: warn.querySelector('.changes-warn-head').textContent,
  items: [...warn.querySelectorAll('.changes-warn-list li')].map((li) => ({
    path: li.querySelector('.changes-warn-path').textContent,
    why: [...li.querySelectorAll('.changes-warn-why')].map((node) => node.textContent),
    blocked: li.classList.contains('blocked'),
  })),
  buttons: [...warn.querySelectorAll('.changes-warn-actions .button')].map((button) => [button.className, button.textContent]),
})(document.querySelector('#log .changes-warn:not([hidden])'))
"""

# 预览里的每一行：[加的 / 删的 / 没变的, 这一行的字]
DIFF_ROWS = (
    "[...document.querySelectorAll('#log li.changes-file')[{index}].querySelectorAll('.changes-line')]"
    ".map((line) => [['add', 'del', 'same'].find((kind) => line.classList.contains(kind)), line.querySelector('.changes-text').textContent])"
)

# 预览只用 textContent 画：.changes-code 里只有一行一个 div（改动的一行、"第几行"的小标题），一行里只有那几个 span，
# span 里再没有元素。文件里写着 <img>、<b> 也只是字
DIFF_STRUCTURE = r"""
(() => {
  const bad = [];
  for (const code of document.querySelectorAll('#log .changes-code')) {
    for (const row of code.children) {
      if (row.tagName !== 'DIV' || !(row.classList.contains('changes-line') || row.classList.contains('changes-hunk'))) bad.push(row.outerHTML.slice(0, 120));
      if (row.classList.contains('changes-hunk') && row.children.length) bad.push(row.outerHTML.slice(0, 120));
      for (const part of row.classList.contains('changes-line') ? row.children : []) {
        const known = ['changes-no', 'changes-sign', 'sr-only', 'changes-text'].includes(part.className);
        if (part.tagName !== 'SPAN' || !known || part.children.length) bad.push(part.outerHTML.slice(0, 120));
      }
    }
  }
  return bad;
})()
"""

# 重新生成之后的那一轮：页面整个重画过（reset），新的"改了 N 个文件"出现了、这一轮也结束了。
# 认"不是刚才那个元素"，不靠赶上 busy 亮起来的那一下（演示的一轮很快，可能还没看到就结束了）
REGENERATED = IDLE + " && (node => node && node !== window.__changesBefore)(document.querySelector('#log .changes'))"


def shown(tag: str, name: str) -> str:
    """页面上给人看的路径：工作目录里的相对路径，按这台电脑的写法（Windows 上是反斜杠，见 FileEditor.display）。"""
    return os.path.join("ui-files", tag, name)


def _tag(name: str) -> str:
    """这个测试自己的文件夹名。演示的剧本只认 40 个以内的字母、数字和 -（见 demo_scripts.files）。"""
    tag = f"{name}-{uuid.uuid4().hex[:8]}"
    assert len(tag) <= 40, tag
    return tag


def _start(page, tag: str) -> Path:
    """换成"改文件不用问"，发一条 [ui:files] 的消息，等这一轮改完两个文件。返回这段对话的文件夹。

    换档位只为了不用一张张点确认卡片（demo.reset() 每个测试开始前换回"每次都问"）。"""
    status, reply = page.demo.api("/api/approval", {"mode": "edits"})
    assert status == 200, reply
    steps.send(page, f"[ui:files] {tag}")
    steps.reply(page, "第 1 轮的文件改好了")
    page.wait(FINISHED, what="改了文件的那一行出现")
    return page.demo.root / "work" / "ui-files" / tag


def _expect_asks(page) -> None:
    """这个测试里会有要你先确认的撤回：助手程序回答 409（带着 asks，页面照着在原地问你），浏览器会把它记成一条
    "加载失败"。是故意的，只放过这几个接口的 409。"""
    page.allow_errors(*(f"409 (Conflict) {page.demo.origin}/api/{path}" for path in ("changes/undo", "changes/redo", "turn/retry")))


def _shape(page) -> dict:
    return json.loads(page.js(SHAPE))


def _warning(page) -> dict | None:
    return page.js(WARNING_SHAPE)


def _expand(page, box: int = 0) -> None:
    page.click("#log .changes-toggle", box)
    page.wait(f"!document.querySelectorAll('#log .changes-body')[{box}].hidden", what="那一行点开")
    steps.idle(page)


def _diff(page, index: int) -> None:
    """点第 index 个文件（整段对话里的第几个）的名字，等它的预览画好。"""
    page.click("#log .changes-name", index)
    page.wait(f"document.querySelectorAll('#log li.changes-file')[{index}].querySelector('.changes-code')", what="改动的预览出现")
    steps.idle(page)


def _act(page, index: int) -> None:
    """点第 index 个文件那一行上的"撤回"（或者"重做"）。"""
    page.click("#log .changes-act", index)


def _logs(page, n: int) -> None:
    """等撤回、重做的记录有 n 行（做完了，页面也照着 file_undo 事件更新好了）。"""
    page.wait(f"document.querySelectorAll('#log .changes-log').length === {n} && {IDLE}", what=f"第 {n} 行撤回、重做的记录")


def _focused_on(page, selector: str, index: int = 0) -> bool:
    return page.js(f"document.activeElement === document.querySelectorAll({json.dumps(selector)})[{index}]")


def _set_dark(page) -> None:
    page.click("#theme")
    page.wait("document.getElementById('appearance').matches(':popover-open')", what="外观面板打开")
    page.click("[data-theme-pick='dark']")
    page.wait("document.documentElement.dataset.theme === 'dark'", what="换成深色")
    page.key("Escape")
    page.wait("!document.getElementById('appearance').matches(':popover-open')")
    steps.idle(page)


def test_the_change_line_ends_the_turn_and_shows_each_file(page):
    tag = _tag("show")
    folder = _start(page, tag)
    plan_lines = (folder / PLAN).read_text(encoding="utf-8").splitlines()
    notes_lines = (folder / NOTES).read_text(encoding="utf-8").splitlines()
    assert notes_lines == NOTES_TEXT.splitlines()

    # 那一行在这一轮的最后：回答后面；"重新生成"还在回答下面那一排里，没有被挤到那一行后面去
    where = page.js(
        "(() => { const box = document.querySelector('#log .changes'), last = document.querySelector('#log .msg.assistant.turn-last');"
        " return { end: document.getElementById('log').lastElementChild === box, count: document.querySelectorAll('#log .changes').length,"
        "  after: Boolean(last && last.compareDocumentPosition(box) & Node.DOCUMENT_POSITION_FOLLOWING),"
        "  retry: Boolean(last && last.querySelector('.msg-actions .turn-retry')), reply: last ? last.textContent : '' }; })()"
    )
    assert where["end"] and where["count"] == 1 and where["after"] and where["retry"], where
    assert "第 1 轮的文件改好了" in where["reply"]

    # 收着：标题、一共加减了几行、右边一个"撤回"
    shape = _shape(page)["sets"][0]
    assert shape["title"] == "改了 2 个文件" and not shape["undone"]
    assert shape["total"] == f"+{len(plan_lines) + len(notes_lines)}−0"
    assert shape["all"] == ["undo", "撤回"]
    assert page.js("document.querySelector('#log .changes-all').getAttribute('aria-label')") == "撤回这一轮的全部改动"
    assert page.js("document.querySelector('#log .changes-toggle').getAttribute('aria-expanded')") == "false"
    assert page.js("document.querySelector('#log .changes-body').hidden")

    # 点开：每个文件一行，新增的标着"新增"，加了几行；每行一个"撤回"（读屏软件念的是"撤回 + 文件名"）
    _expand(page)
    assert page.js("document.querySelector('#log .changes-toggle').getAttribute('aria-expanded')") == "true"
    assert page.js("document.querySelector('#log .changes').classList.contains('open')")
    files = _shape(page)["sets"][0]["files"]
    assert [file["path"] for file in files] == [shown(tag, PLAN), shown(tag, NOTES)]
    assert [file["kind"] for file in files] == ["changes-kind created"] * 2
    assert page.js("[...document.querySelectorAll('#log .changes-kind')].map((node) => node.textContent)") == ["新增", "新增"]
    assert [file["lines"] for file in files] == [f"+{len(plan_lines)}−0", f"+{len(notes_lines)}−0"]
    assert [file["act"] for file in files] == [["undo", "撤回"]] * 2 and [file["state"] for file in files] == ["", ""]
    labels = page.js(
        "[...document.querySelectorAll('#log .changes-act')].map((button) => button.getAttribute('aria-labelledby').split(' ')"
        ".map((id) => document.getElementById(id).textContent).join(' '))"
    )
    assert labels == [f"撤回 {shown(tag, PLAN)}", f"撤回 {shown(tag, NOTES)}"]
    assert page.text("#log .changes-note") == "这里只有文件工具做的改动；执行命令改动的文件没法自动撤回。"

    # 点文件名：一行一行的预览（新建的文件每一行都是加上的）
    _diff(page, 1)
    assert page.js("document.querySelectorAll('#log .changes-name')[1].getAttribute('aria-expanded')") == "true"
    assert page.js(DIFF_ROWS.format(index=1)) == [["add", line] for line in notes_lines]
    assert page.js("document.querySelectorAll('#log li.changes-file')[1].querySelectorAll('.changes-line.add').length") == len(notes_lines)
    assert page.text("#log li.changes-file.open .changes-diff-caption") == f"这一轮的改动：{shown(tag, NOTES)}"
    _diff(page, 0)
    assert page.js(DIFF_ROWS.format(index=0)) == [["add", line] for line in plan_lines]
    assert page.js(DIFF_STRUCTURE) == []
    # 再点一次收起来
    page.click("#log .changes-name", 1)
    page.wait("document.querySelectorAll('#log .changes-diff')[1].hidden", what="预览收起来")
    assert page.js("document.querySelectorAll('#log .changes-name')[1].getAttribute('aria-expanded')") == "false"


def test_undo_one_file_redo_it_then_the_whole_turn(page):
    tag = _tag("undo")
    folder = _start(page, tag)
    plan, notes = folder / PLAN, folder / NOTES
    saved = plan.read_bytes()
    _expand(page)

    # 撤回一个：这一轮新建的，删掉；那一行写着"已撤回"，标题跟着变；在发生的地方多一行记录，带一个"重做"
    _act(page, 0)
    _logs(page, 1)
    assert not plan.exists() and notes.read_text(encoding="utf-8") == NOTES_TEXT
    shape = _shape(page)
    files = shape["sets"][0]["files"]
    assert files[0]["undone"] and files[0]["state"] == "已撤回" and files[0]["act"] == ["redo", "重做"]
    assert not files[1]["undone"] and files[1]["state"] == ""
    assert shape["sets"][0]["title"] == "改了 2 个文件，撤回了 1 个"
    assert shape["sets"][0]["total"] == "+3−0"  # 撤回了的那个不算
    assert shape["logs"] == [{"action": "undo", "text": f"撤回了 {shown(tag, PLAN)} 的改动", "redo": True}]
    assert shape["order"][-2:] == ["changes", "log"]
    assert _focused_on(page, "#log .changes-act", 0)  # 焦点还在刚点的那个按钮上（字换成了"重做"）

    # 那一行记录上的"重做"：文件原样回来，这个"重做"就收起来了（这一次撤回已经不算数了）
    page.click("#log .changes-log-redo")
    _logs(page, 2)
    assert plan.read_bytes() == saved
    shape = _shape(page)
    assert shape["logs"][0]["redo"] is False
    assert shape["logs"][1] == {"action": "redo", "text": f"重做了 {shown(tag, PLAN)} 的改动", "redo": None}
    assert shape["sets"][0]["title"] == "改了 2 个文件" and not shape["sets"][0]["files"][0]["undone"]
    # 点了的"重做"自己藏起来了：焦点不能留在它上面（Esc 之类的按键就没人接了），交给那一行的开关
    assert _focused_on(page, "#log .changes-toggle")

    # 整轮撤回（右上角那个按钮）：两个都删掉，连同为它们新建的文件夹；整行变成"撤回了"，按钮换成"重做"
    page.click("#log .changes-all")
    _logs(page, 3)
    assert not plan.exists() and not notes.exists() and not folder.exists()
    shape = _shape(page)
    assert shape["sets"][0]["undone"] and shape["sets"][0]["title"] == "撤回了这一轮改的 2 个文件"
    assert shape["sets"][0]["all"] == ["redo", "重做"] and shape["sets"][0]["total"] == ""
    assert page.text("#log .changes-foot-all") == "重做这一轮的全部改动"
    assert shape["logs"][2] == {"action": "undo", "text": f"撤回了 {shown(tag, PLAN)} 等 2 个文件的改动", "redo": True}

    # 再点一次就是整轮重做
    page.click("#log .changes-all")
    _logs(page, 4)
    assert plan.read_bytes() == saved and notes.read_text(encoding="utf-8") == NOTES_TEXT
    shape = _shape(page)
    assert not shape["sets"][0]["undone"] and shape["sets"][0]["title"] == "改了 2 个文件" and shape["sets"][0]["all"] == ["undo", "撤回"]
    assert [log["redo"] for log in shape["logs"]] == [False, None, False, None]


def test_a_file_changed_after_the_turn_asks_in_place_first(page):
    _expect_asks(page)
    tag = _tag("conflict")
    folder = _start(page, tag)
    notes = folder / NOTES
    notes.write_bytes(MINE)  # 这一轮之后你自己又改了它
    _expand(page)

    # 撤回它：先不做，在那一行里原地说清楚；焦点在"仍然撤回"上
    _act(page, 1)
    page.wait(WARNING_FOCUSED, what="原地的提醒出现，焦点在“仍然撤回”上")
    warning = _warning(page)
    assert warning["head"] == "撤回之前先确认一下"
    assert [item["path"] for item in warning["items"]] == [shown(tag, NOTES)] and not warning["items"][0]["blocked"]
    assert any(why.startswith("这一轮之后它又被改过") for why in warning["items"][0]["why"]), warning
    assert warning["buttons"] == [["button primary", "仍然撤回"], ["button", "取消"]]
    assert notes.read_bytes() == MINE and (folder / PLAN).exists()  # 什么都还没做
    assert page.js("document.querySelectorAll('#log .changes-log').length") == 0

    # Esc 收起来，焦点回到点了的那个"撤回"上
    page.key("Escape")
    page.wait("document.querySelector('#log .changes-warn').hidden", what="提醒收起来")
    assert _focused_on(page, "#log .changes-act", 1)
    assert notes.read_bytes() == MINE

    # 再点一次"撤回"、点"仍然撤回"才做：这一轮新建的，删掉（你后来改的那些也一起没了，提醒里说过）
    _act(page, 1)
    page.wait(WARNING_FOCUSED, what="提醒又出现")
    page.click("#log .changes-warn-actions .button.primary")
    _logs(page, 1)
    assert not notes.exists() and (folder / PLAN).exists()
    assert page.js("document.querySelector('#log .changes-warn').hidden")
    assert _focused_on(page, "#log .changes-act", 1)  # 提醒里的按钮没了，焦点交回给"撤回"（现在是"重做"）
    assert _shape(page)["sets"][0]["files"][1]["undone"]

    # 重做放回去的是撤回时换下来的那一份：你自己改过的样子
    page.click("#log .changes-log-redo")
    _logs(page, 2)
    assert notes.read_bytes() == MINE


def test_a_later_turn_that_changed_the_same_file_is_explained(page):
    _expect_asks(page)
    tag = _tag("later")
    folder = _start(page, tag)
    notes = folder / NOTES
    # 第 2 轮之前在 notes.txt 里放几行像 HTML 的字：预览只用 textContent 画，它们原样显示成字，不变成元素
    tricky = '<img src=x onerror="window.__pwned = 1">\n第二行\n<b>粗体</b>\n'
    notes.write_text(tricky, encoding="utf-8", newline="")
    steps.send(page, "再改一次")
    steps.reply(page, "第 2 轮的文件改好了")
    page.wait("document.querySelectorAll('#log .changes').length === 2 && " + IDLE, what="第 2 轮那一行出现")
    after = notes.read_bytes()
    assert "第二行（第 2 轮改过）" in after.decode()

    second = _shape(page)["sets"][1]
    assert second["title"] == "改了 1 个文件" and second["total"] == "+1−1"
    assert [(file["path"], file["kind"]) for file in second["files"]] == [(shown(tag, NOTES), "changes-kind modified")]
    _expand(page, 1)
    assert page.js("document.querySelectorAll('#log .changes')[1].querySelector('.changes-kind').textContent") == "修改"
    _diff(page, 2)
    assert page.js(DIFF_ROWS.format(index=2)) == [
        ["same", '<img src=x onerror="window.__pwned = 1">'], ["del", "第二行"], ["add", "第二行（第 2 轮改过）"], ["same", "<b>粗体</b>"],
    ]
    assert page.js("document.querySelectorAll('#log .changes img, #log .changes b').length") == 0
    assert page.js("window.__pwned === undefined")
    assert page.js(DIFF_STRUCTURE) == []

    # 撤回第 1 轮的 notes.txt：后面那一轮也改过它，先说清楚
    _expand(page, 0)
    _act(page, 1)
    page.wait(WARNING_FOCUSED, what="原地的提醒出现")
    warning = _warning(page)
    assert page.js("document.querySelectorAll('#log .changes')[0].contains(document.querySelector('#log .changes-warn:not([hidden])'))")
    assert any(why.startswith("后面还有 1 轮也改过它") for why in warning["items"][0]["why"]), warning
    page.click("#log .changes-warn-actions .button:not(.primary)")  # 取消
    page.wait("[...document.querySelectorAll('#log .changes-warn')].every((node) => node.hidden)", what="提醒收起来")
    assert _focused_on(page, "#log .changes-act", 1)
    assert notes.read_bytes() == after

    # 仍然撤回：恢复成第 1 轮之前的样子（第 1 轮新建的，所以是删掉），第 2 轮的改动也一起没了
    _act(page, 1)
    page.wait(WARNING_FOCUSED)
    page.click("#log .changes-warn-actions .button.primary")
    _logs(page, 1)
    assert not notes.exists()

    # 再撤回第 2 轮的：它在那一轮之后被删掉了，先问；仍然撤回就按第 2 轮之前的样子把它建回来
    _act(page, 2)
    page.wait(WARNING_FOCUSED, what="第 2 轮那一行里的提醒")
    warning = _warning(page)
    assert page.js("document.querySelectorAll('#log .changes')[1].contains(document.querySelector('#log .changes-warn:not([hidden])'))")
    assert any(why.startswith("这一轮之后它被删掉了") for why in warning["items"][0]["why"]), warning
    page.click("#log .changes-warn-actions .button.primary")
    _logs(page, 2)
    assert notes.read_text(encoding="utf-8") == tricky


def test_reload_replays_the_same_states(page):
    tag = _tag("replay")
    folder = _start(page, tag)
    _expand(page)
    _act(page, 0)  # 撤回 计划.md
    _logs(page, 1)
    page.click("#log .changes-foot-all")  # 最下面的"撤回这一轮的全部改动"：撤回剩下的 notes.txt
    _logs(page, 2)
    page.click("#log .changes-log-redo", 0)  # 第一行记录上的"重做"：计划.md 放回去
    _logs(page, 3)
    before = _shape(page)
    assert before["sets"][0]["title"] == "改了 2 个文件，撤回了 1 个"
    assert [(file["undone"], file["state"]) for file in before["sets"][0]["files"]] == [(False, ""), (True, "已撤回")]
    assert [(log["action"], log["redo"]) for log in before["logs"]] == [("undo", False), ("undo", True), ("redo", None)]
    assert before["order"][-4:] == ["changes", "log", "log", "log"]

    # 刷新：从事件重放出来的一模一样，不播入场动画
    page.reload()
    page.wait("document.body.classList.contains('ready') && document.querySelectorAll('#log .changes-log').length === 3 && " + IDLE, what="重放完")
    assert _shape(page) == before
    assert page.js("document.querySelectorAll('#log .enter').length") == 0

    # 重放出来的按钮照样能用：撤回 notes.txt 的那一行记录上的"重做"
    page.click("#log .changes-log-redo:not([hidden])")
    _logs(page, 4)
    assert (folder / NOTES).read_text(encoding="utf-8") == NOTES_TEXT
    assert _shape(page)["sets"][0]["title"] == "改了 2 个文件"


def test_regenerate_can_undo_the_files_of_the_turn_first(page):
    tag = _tag("retry")
    folder = _start(page, tag)
    # 重新生成：这一轮改过文件，先在原地确认；卡片上多一个"同时撤回"，默认不勾
    page.click("#log .turn-retry")
    page.wait("document.querySelector('#log .turn-confirm .turn-confirm-undo input')", what="确认卡片上的“同时撤回”")
    steps.idle(page)
    assert page.js("document.querySelector('#log .turn-confirm-undo input').checked") is False
    assert page.text("#log .turn-confirm-undo span") == "同时撤回这一轮改的 2 个文件"
    assert page.js("document.querySelector('#log .turn-confirm-issues').hidden")
    page.click("#log .turn-confirm-undo input")
    assert page.js("document.querySelector('#log .turn-confirm-undo input').checked") is True

    # 勾上再确认：先撤回（新建的两个删掉），再倒回去重跑。重跑的那一轮又新建了它们：所以标的是"新增"，不是"覆盖"
    page.js("window.__changesBefore = document.querySelector('#log .changes')")
    page.click("#log .turn-confirm .button.primary")
    page.wait(REGENERATED, 20, "重新生成的一轮改完文件")
    steps.reply(page, "第 1 轮的文件改好了")
    shape = _shape(page)
    assert len(shape["sets"]) == 1 and shape["logs"] == []  # 倒回去的那一轮连同它的那一行一起没了，一起撤回的不留记录
    assert [file["kind"] for file in shape["sets"][0]["files"]] == ["changes-kind created"] * 2
    assert (folder / PLAN).exists() and (folder / NOTES).read_text(encoding="utf-8") == NOTES_TEXT
    assert shape["order"].count("user") == 1

    # 对照：不勾就重新生成，文件还在，重跑的那一轮是把它们覆盖了。先把两个文件都改一下：
    # 写进去的和原来一字不差的不算改过（见 undo/journal.py），那样这一轮就没有那一行了
    for name in (PLAN, NOTES):
        (folder / name).write_bytes(MINE)
    page.click("#log .turn-retry")
    page.wait("document.querySelector('#log .turn-confirm .turn-confirm-undo input')")
    page.js("window.__changesBefore = document.querySelector('#log .changes')")
    page.click("#log .turn-confirm .button.primary")
    page.wait(REGENERATED, 20, "不撤回、直接重新生成的一轮")
    assert [file["kind"] for file in _shape(page)["sets"][0]["files"]] == ["changes-kind overwritten"] * 2


def test_regenerate_explains_a_file_changed_after_the_turn(page):
    _expect_asks(page)
    tag = _tag("retry-conflict")
    folder = _start(page, tag)
    notes = folder / NOTES
    notes.write_bytes(MINE)
    page.click("#log .turn-retry")
    page.wait("document.querySelector('#log .turn-confirm .turn-confirm-undo input')")
    page.click("#log .turn-confirm-undo input")
    page.click("#log .turn-confirm .button.primary")

    # notes.txt 在这一轮之后被改过：先不做，在卡片上说清楚，按钮换成"仍然撤回并重新生成"，焦点在它上面
    page.wait(
        "!document.querySelector('#log .turn-confirm-issues').hidden && document.activeElement === document.querySelector('#log .turn-confirm .button.primary')",
        what="卡片上的说明出现",
    )
    assert page.text("#log .turn-confirm .button.primary") == "仍然撤回并重新生成"
    assert page.text("#log .turn-confirm-issues .changes-warn-path") == shown(tag, NOTES)
    assert page.text("#log .turn-confirm-issues .changes-warn-why").startswith("这一轮之后它又被改过")
    assert notes.read_bytes() == MINE and (folder / PLAN).exists()
    assert not page.js("document.body.classList.contains('busy')")

    # 改了主意不勾了：说明收起来，按钮换回原来的字
    page.click("#log .turn-confirm-undo input")
    page.wait("document.querySelector('#log .turn-confirm-issues').hidden", what="说明收起来")
    assert page.text("#log .turn-confirm .button.primary") == "仍然重新生成"

    # 再勾上：又问一次；点"仍然撤回并重新生成"才做。重跑的那一轮新建的 notes.txt 是剧本里的内容
    page.click("#log .turn-confirm-undo input")
    page.click("#log .turn-confirm .button.primary")
    page.wait("!document.querySelector('#log .turn-confirm-issues').hidden", what="又问一次")
    page.js("window.__changesBefore = document.querySelector('#log .changes')")
    page.click("#log .turn-confirm .button.primary")
    page.wait(REGENERATED, 20, "撤回以后重新生成的一轮")
    assert [file["kind"] for file in _shape(page)["sets"][0]["files"]] == ["changes-kind created"] * 2
    assert notes.read_text(encoding="utf-8") == NOTES_TEXT


def _everything(page, folder: Path) -> None:
    """把这个功能能出现的样子都摆出来：点开那一行、两个预览、一行撤回的记录、原地的提醒，
    再加上重新生成之前的确认卡片（勾了"同时撤回"、卡片上的说明）。查翻译、查文字有没有冲出去时用。"""
    _expect_asks(page)
    _expand(page)
    _diff(page, 0)
    _diff(page, 1)
    _act(page, 0)
    _logs(page, 1)
    (folder / NOTES).write_bytes(MINE)
    _act(page, 1)
    page.wait(WARNING_FOCUSED, what="原地的提醒出现")
    page.click("#log .turn-retry")
    page.wait("document.querySelector('#log .turn-confirm .turn-confirm-undo input')", what="确认卡片出现")
    page.click("#log .turn-confirm-undo input")
    page.click("#log .turn-confirm .button.primary")
    page.wait("!document.querySelector('#log .turn-confirm-issues').hidden", what="卡片上的说明出现")
    steps.idle(page)


def test_translated_in_place_in_english_and_spanish(page):
    steps.calm(page)
    folder = _start(page, _tag("lang"))
    _everything(page, folder)
    zh = _shape(page)

    # 摆好以后原地换语言：每一句都换过来（提醒里"为什么"的那句是助手程序给的，带着原文，也就地重译）
    for code in ("en", "es"):
        steps.language(page, code)
        assert checks.untranslated(page, ["#log"]) == [], code
        assert page.text("#log .changes-title") != zh["sets"][0]["title"]

    # 在西班牙语下新出现的也是译好的：收起确认卡片（焦点这时在语言按钮上，所以点"取消"），在提醒里点"仍然撤回"，多一行记录
    page.click("#log .turn-confirm-actions .button:not(.primary)")
    page.wait("!document.querySelector('#log .turn-confirm')", what="确认卡片收起来")
    page.click("#log .changes-warn-actions .button.primary")
    _logs(page, 2)
    assert checks.untranslated(page, ["#log"]) == []

    # 换回中文：还是原来那几句
    steps.language(page, "zh")
    shape = _shape(page)
    assert shape["sets"][0]["title"] == "撤回了这一轮改的 2 个文件"
    assert [log["text"] for log in shape["logs"]] == [log["text"] for log in zh["logs"]] + [f"撤回了 {zh['sets'][0]['files'][1]['path']} 的改动"]


@pytest.mark.parametrize("code", ["zh", "en", "es"])
def test_fits_at_phone_width(page, code):
    page.size(PHONE_WIDTH, 844)
    steps.calm(page)
    if code != "zh":
        steps.language(page, code)
    # 文件夹的名字长到一行放不下（剧本最多认 40 个字）：路径要么省略号收住，要么折行，不能冲出那一行
    folder = _start(page, _tag("long-folder-name-for-phones"))
    _everything(page, folder)
    cards = ["#log .changes", "#log .changes-log", "#log .turn-confirm"]
    assert checks.overflow(page, cards) == []
    assert checks.sticking_out(page, cards) == []  # 文字以外的：图标、按钮、标签、预览的框


def test_the_file_tools_card_fits_at_phone_width_in_spanish(page):
    # 不是"改了 N 个文件"这一行本身的问题，是同一段对话里文件工具的卡片：在这里查，是因为只有这个剧本用到了文件工具。
    # checks.overflow 查不出它（"Sin preguntar"在它自己的小框里放得下，是那个小框伸出了卡片），所以用 sticking_out
    page.size(PHONE_WIDTH, 844)
    steps.calm(page)
    steps.language(page, "es")
    _start(page, _tag("long-folder-name-for-phones"))
    assert checks.sticking_out(page, ["#log .call"]) == []


# 这一行里的字，在浅色和深色下都要看得清（WCAG 正文 4.5:1）
TEXTS = [
    "#log .changes-title", "#log .changes-total .changes-plus", "#log .changes-total .changes-minus", "#log .changes-kind.created",
    "#log li.changes-file:not(.undone) .changes-path", "#log li.changes-file.undone .changes-path", "#log .changes-state:not([hidden])",
    "#log .changes-lines .changes-plus", "#log .changes-note span", "#log .changes-line.add .changes-text", "#log .changes-hunk",
    "#log .changes-diff-caption", "#log .changes-act", "#log .changes-warn-head", "#log .changes-warn-path", "#log .changes-warn-why",
    "#log .changes-log-text",
]
# 换了外观就要跟着变的几样颜色
COLORS = (
    "[['#log .changes', 'backgroundColor'], ['#log .changes-code', 'backgroundColor'], ['#log .changes-warn', 'backgroundColor'],"
    " ['#log .changes-line.add', 'backgroundColor'], ['#log .changes-title', 'color'], ['#log .changes-kind.created', 'color']]"
    ".map(([selector, name]) => getComputedStyle(document.querySelector(selector))[name])"
)


def test_light_and_dark_are_both_readable(page):
    _expect_asks(page)
    steps.calm(page)
    folder = _start(page, _tag("dark"))
    _expand(page)
    _diff(page, 1)
    _act(page, 0)
    _logs(page, 1)
    (folder / NOTES).write_bytes(MINE)
    _act(page, 1)
    page.wait(WARNING_FOCUSED, what="原地的提醒出现")
    light = page.js(COLORS)
    found = {}
    for theme in ("light", "dark"):
        if theme == "dark":
            _set_dark(page)
            page.wait(WARNING)  # 换外观不碰那一行：提醒还开着
        for selector, ratio in checks.contrast(page, TEXTS):
            if ratio is None or ratio < 4.5:
                found[f"{theme} {selector}"] = ratio
    assert found == {}, f"对比度不够（None 是没找到这个元素）：{found}"
    dark = page.js(COLORS)
    assert all(a != b for a, b in zip(light, dark, strict=True)), list(zip(light, dark, strict=True))
