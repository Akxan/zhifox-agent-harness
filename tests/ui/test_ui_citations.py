"""联网搜索的回答引用的网页：记号、浮出的卡片、末尾的来源列表、复制；以前存下的、被切成好几段的回答怎么重放。

回复的剧本在 tests/demo_scripts.py：[ui:cite] 是一段 API 按引用切成好几个文字块的回答，[ui:pause] 是写到一半停下
（pause_turn）、下一次请求接着写完的回答。以前存下的那段（每个文字块后面都跟着一个 block_end）是 demo_web.py
启动时写进演示的数据目录的，编号是 demo_scripts.FRAGMENTED_ID。

为什么这么查：见 harness/webui/page/scripts/citations.js 和 streaming.js 的 pauseBlock。
"""

from __future__ import annotations

import json

import demo_scripts

from . import checks, steps
from .test_ui_page import CONTRAST

TITLES = [demo_scripts.CITE_A[1], demo_scripts.CITE_C[1], demo_scripts.CITE_B[1]]  # 按第一次被引用的先后编号
SITES = ["perfect-items.example", "directorio-empresas.example", "es.wikipedia.org"]
LINKS = [demo_scripts.CITE_A[0], demo_scripts.CITE_C[0], demo_scripts.CITE_B[0]]

# 这段回复的样子：有几段、几个"复制"、每个记号的编号、来源列表（编号、标题、网站、网址）、链接的 target 和 rel
SHAPE = r"""
(() => {
  const replies = [...document.querySelectorAll('#log .msg.assistant')];
  const last = replies[replies.length - 1];
  const marks = [...last.querySelectorAll('.cite-mark')];
  const items = [...last.querySelectorAll('.cite-sources .cite-link')];
  return {
    replies: replies.length,
    copies: document.querySelectorAll('#log .msg.assistant > .msg-actions').length,  // 每段回复一排按钮（里面是"复制"）
    paragraphs: last.querySelectorAll('.msg-text > p').length,
    marks: marks.map((a) => a.dataset.n),
    inCell: last.querySelectorAll('td .cite-mark').length,
    sources: items.map((a) => [a.querySelector('.cite-n').textContent, a.querySelector('.cite-title').textContent, a.querySelector('.cite-site').textContent, a.href]),
    links: [...marks, ...items].map((a) => [a.target, a.rel, a.protocol]),
    head: (last.querySelector('.cite-head') || {}).textContent,
    text: last.querySelector('.msg-text').textContent,
  };
})()
"""


# "复制"写进剪贴板的东西，在测试里接住，不碰真的剪贴板
CATCH_COPY = (
    "window.__copied = null; Object.defineProperty(navigator.clipboard, 'writeText',"
    " { configurable: true, value: async (text) => { window.__copied = text; } })"
)
COPY = "#log .msg.assistant .msg-actions button:has(use[href='#i-copy'])"  # 回复上的"复制"（最后一个回答旁边还有"重新生成"）


def _shape(page) -> dict:
    return page.js(SHAPE)


def test_a_cited_answer_shows_marks_a_card_and_its_sources_and_replays_the_same(page):
    steps.send(page, "[ui:cite] 找几家马德里的促销品供应商")
    steps.reply(page, "印着公司标志")
    shape = _shape(page)
    # 一整段：API 切成的十个文字块画成一段回复、一个"复制"，没有一块一行
    assert (shape["replies"], shape["copies"]) == (1, 1)
    # 记号紧跟在被引用的文字后面：同一个网页同一个编号，按第一次出现的先后编号；表格的格子里也有
    assert shape["marks"] == ["1", "2", "1", "1", "3"]
    assert shape["inCell"] == 1
    # 编号是样式画的，不是页面上的字：句子还是原来那句，搜索跳转对得上
    assert "B2B 促销品供应商，目录里有一千两百多家企业客户。" in shape["text"]
    # 末尾的来源列表
    assert shape["head"] == "资料来源"
    assert shape["sources"] == [[str(n + 1), TITLES[n], SITES[n], LINKS[n]] for n in range(3)]
    # 每个链接都在新标签页里打开、不带来源页的信息，只有 http、https；不安全的那个网址根本没到页面上
    assert all(link == ["_blank", "noopener noreferrer", "https:"] for link in shape["links"]), shape["links"]
    assert "javascript:" not in page.js("document.getElementById('log').innerHTML")
    assert "No debería aparecer" not in page.js("document.body.textContent")

    # 鼠标停在记号上：浮出卡片，写着网站、标题和被引的原文，整张在窗口里（左右各留 16 像素）
    page.hover("#log .cite-mark")
    page.wait("document.querySelector('.cite-card.open')", what="卡片浮出来")
    steps.idle(page)
    card = page.js(
        "(() => { const card = document.querySelector('.cite-card'), box = card.getBoundingClientRect();"
        " return { site: card.children[0].textContent, title: card.children[1].textContent, quote: card.children[2].textContent,"
        " inside: box.left >= 15 && box.right <= innerWidth - 15 && box.top >= 0 && box.bottom <= innerHeight }; })()"
    )
    assert card == {"site": SITES[0], "title": TITLES[0], "quote": "“Somos el proveedor B2B líder en España de artículos promocionales personalizados.”", "inside": True}
    page.mouse(4, 4)
    page.wait("!document.querySelector('.cite-card.open')", what="鼠标移开，卡片收起")
    # 用键盘移到记号上也浮出来（读屏软件读的是 aria-label：编号、标题、网站）；焦点离开就收起
    page.focus("#log .cite-mark:nth-child(2)")  # 第二处记号里的第二个来源：又是 1 号
    page.wait("document.querySelector('.cite-card.open') && document.querySelector('.cite-card').children[0].textContent === 'perfect-items.example'")
    assert page.js("document.activeElement.getAttribute('aria-label')") == f"1. {TITLES[0]} — {SITES[0]}"
    page.js("document.activeElement.blur()")
    page.wait("!document.querySelector('.cite-card.open')", what="焦点离开，卡片收起")

    # "复制"：纯文字，记号写成 [1]，末尾附上来源的标题和网址
    page.js(CATCH_COPY)
    page.click(COPY)
    copied = page.wait("window.__copied", what="复制了")
    assert "它自称是西班牙领先的 B2B 促销品供应商[1]，目录里有一千两百多家企业客户[2][1]。" in copied
    assert "| Perfect Items | Madrid[1] |" in copied
    assert copied.endswith("资料来源：\n" + "\n".join(f"{n + 1}. {TITLES[n]} — {LINKS[n]}" for n in range(3)))

    # 刷新：重放出来一模一样，不播入场动画
    before = _shape(page)
    page.reload()
    page.wait("document.body.classList.contains('ready') && document.querySelector('#log .cite-sources')", what="重放完")
    assert _shape(page) == before
    assert page.js("document.querySelectorAll('#log .enter').length") == 0
    playing = page.js(
        "document.getAnimations().filter((a) => a.effect.target !== document.getElementById('log')"
        " && document.getElementById('log').contains(a.effect.target)).map((a) => a.effect.target.className)"
    )
    assert playing == [], f"重放出来的旧内容不该播动画：{playing}"

    # 浅色、深色下，记号、标题、网站都看得清（正文的对比度至少 4.5:1）
    targets = ["#log .cite-mark", "#log .cite-title", "#log .cite-site", "#log .cite-head"]
    for theme in ("light", "dark"):
        page.js(f"document.documentElement.dataset.theme = {json.dumps(theme)}")
        for selector, ratio in page.js(f"({CONTRAST})({targets!r})"):
            assert ratio is not None and ratio >= 4.5, f"{theme} 下 {selector} 的对比度只有 {ratio}"
    page.js("document.documentElement.dataset.theme = 'light'")

    # 换语言：在原地翻译，记号和来源不变；复制出来的"资料来源："也跟着换
    page.js(CATCH_COPY)  # 刷新过了：重新接住剪贴板
    for code, heading, label in (("en", "Sources", "Sources:"), ("es", "Fuentes", "Fuentes:")):
        steps.language(page, code)
        assert page.text("#log .cite-head") == heading
        assert _shape(page)["marks"] == before["marks"]
        page.js("window.__copied = null")
        page.click(COPY)
        assert page.wait("window.__copied", what="复制了").split("\n\n")[-1].startswith(label + "\n1. ")


def test_cited_sources_fit_a_phone_screen_in_every_language(page):
    page.size(390, 844)
    steps.calm(page)
    steps.send(page, "[ui:cite] 窄屏上看看来源")
    steps.reply(page, "印着公司标志")
    for code in ("zh", "en", "es"):
        steps.language(page, code)
        assert checks.overflow(page, ["#log .msg.assistant"]) == [], code
        # 很长的标题和网址：省略号，不换到第二行、不把这一栏撑宽
        rows = page.js("[...document.querySelectorAll('#log .cite-link')].map((a) => a.getBoundingClientRect().height)")
        assert rows and max(rows) < 40, rows
        assert page.js("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    # 卡片也不出屏幕
    page.hover("#log .cite-mark", 3)
    page.wait("document.querySelector('.cite-card.open')")
    assert page.js("(() => { const box = document.querySelector('.cite-card').getBoundingClientRect(); return box.left >= 15 && box.right <= innerWidth - 15; })()")


def test_an_answer_paused_halfway_is_one_paragraph_live_and_after_a_reload(page):
    steps.send(page, "[ui:pause] 先说结论")
    steps.reply(page, "印着公司标志的促销品")
    live = _shape(page)
    # 实时收到的是"文字、block_end、（状态）、文字"：还是同一段、一个"复制"，来源接着编号
    assert (live["replies"], live["copies"], live["paragraphs"]) == (1, 1, 1)
    assert live["text"] == "先说结论：Perfect Items 是最像的一家，它卖的是印着公司标志的促销品。"
    assert live["marks"] == ["1", "2", "1"]
    assert [row[0] for row in live["sources"]] == ["1", "2"]
    page.reload()
    page.wait("document.body.classList.contains('ready') && document.querySelector('#log .cite-sources')", what="重放完")
    assert _shape(page) == live


def test_an_old_fragmented_answer_replays_as_one_paragraph(page):
    # 以前存下的联网搜索回答：六个文字块，每块后面一个 block_end，中间什么都没有。以前重放时一块一行、每块一个"复制"
    status, reply = page.demo.api("/api/conversation/open", {"id": demo_scripts.FRAGMENTED_ID})
    assert status == 200, reply
    page.wait("document.querySelector('#log .msg.assistant') && document.getElementById('title').textContent === '联网搜索的旧回答'", what="打开旧会话")
    shape = _shape(page)
    assert (shape["replies"], shape["copies"], shape["paragraphs"]) == (1, 1, 1)
    assert shape["text"] == "Perfect Items (Madrid): es el más similar. Se describe como proveedor B2B líder en España. Anuncia más de 1.200 empresas clientes, y envíos en 24 h."
    assert shape["marks"] == [] and shape["sources"] == []  # 那时还不记来源：没有就不画
    assert page.js("document.querySelectorAll('#log .enter').length") == 0
    # 复制的是整段原文
    page.js(CATCH_COPY)
    page.click(COPY)
    assert page.wait("window.__copied", what="复制了") == shape["text"]
