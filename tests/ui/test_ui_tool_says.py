"""工具结果里程序自己写的话（zh()，见 harness/i18n.py 开头）：工具卡片上按界面的语言显示，换语言时在原地重译（不重画卡片），
刷新以后照样；填进去的资料（文件名）原样；以前存下的、没有原文的结果照原样显示，不翻译。模型那一边见 tests/test_tool_says.py。"""

from __future__ import annotations

import json

import demo_scripts

from . import steps

NO_TODO = "没有编号为 {id} 的待办。先用 todo_list 查看现有的待办。"
NO_FILE = "{path} 不存在。"

# 最后两张工具卡片：出错的那一行、点开以后"结果"下面的全文
CARDS = """[...document.querySelectorAll('#log .call')].slice(-2).map((card) => [
  card.querySelector('.tool-outcome').textContent,
  [...card.querySelectorAll('.tool-body pre')].pop().textContent,
])"""


def _said(page, code: str, key: str, **values: str) -> str:
    """一句话在 code 这种语言里的样子（照着页面里的翻译表填）。"""
    text = key if code == "zh" else page.js(f"JSON.parse(document.getElementById('i18n').textContent).tables[{code!r}][{json.dumps(key)}]")
    for name, value in values.items():
        text = text.replace("{" + name + "}", value)
    return text


def _check(page, code: str) -> None:
    (todo_line, todo_text), (file_line, file_text) = page.js(CARDS)
    expected = _said(page, code, NO_TODO, id="999")
    assert todo_line == _first_line(expected) and todo_text == expected
    # 路径是资料：照原样填在译好的句子里（文件名里的中文也不动）
    path = file_text[: -len(_said(page, code, NO_FILE, path=""))]
    assert path.endswith(("ui-says/不存在.txt", "ui-says\\不存在.txt")), file_text  # Windows 上是反斜杠
    assert file_text == _said(page, code, NO_FILE, path=path) and file_line == _first_line(file_text)


def _first_line(text: str) -> str:
    """卡片上出错的那一行只写第一行，太长的截到 120 个字（和页面的 firstLine 一样）。"""
    line = text.strip().split("\n")[0]
    return line if len(line) <= 120 else line[:119] + "…"


def test_our_sentences_in_tool_results_follow_the_interface_language(page):
    steps.send(page, "[ui:says] 删掉 999 号，再看一个不存在的文件")
    steps.reply(page, "这两步都没做成")
    _check(page, "zh")
    page.js("window.__card = document.querySelector('#log .call')")

    for code in ("en", "es", "zh", "en"):
        steps.language(page, code)
        _check(page, code)
        assert page.js("window.__card === document.querySelector('#log .call')"), "换语言时卡片被重画了（应该就地换文字）"

    # 刷新以后重放出来的，照样按现在的语言（事件里带着原文）
    page.reload()
    page.wait("document.body.classList.contains('ready') && document.querySelectorAll('#log .call').length >= 2", what="重放出工具卡片")
    _check(page, "en")
    steps.language(page, "zh")
    _check(page, "zh")


def test_an_old_result_without_its_original_is_shown_as_it_was(page):
    # 以前存下的会话里，结果只有文字（没有 key）：照原样显示，换语言也不动（不去翻译表里找一句一样的）
    status, reply = page.demo.api("/api/conversation/open", {"id": demo_scripts.OLD_RESULT_ID})
    assert status == 200, reply
    page.wait("document.getElementById('title').textContent === '以前存下的工具结果' && document.querySelector('#log .call')", what="打开旧会话")
    for code in ("zh", "en", "es"):
        if code != "zh":
            steps.language(page, code)
        line, full = page.js(CARDS)[-1]
        assert line == demo_scripts.OLD_RESULT and full == demo_scripts.OLD_RESULT
