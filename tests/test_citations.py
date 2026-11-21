"""联网搜索的回答引用了哪些网页（harness/citations.py），以及它们怎样到达每一种界面。

Claude 联网搜索以后，回答里出自搜索结果的那几句话，API 会单独切成一个 text 块，块上带着 citations
（网址、标题、被引用的原文，还有一个要原样发回去的 encrypted_index）。以前这些引用被丢掉了：用户看不到
哪句话出自哪个网页。现在：
  - 适配层（providers/claude.py 的 _show）在每个带引用的文字块结束时告诉界面（ui.cite）；
  - 网页界面发 cite 事件（编号由程序定，一段回复一数），事件存进会话文件，打开旧会话时照样重放；
  - 终端和聊天通道在那段文字后面写上 [1]，一段结束时列出来源；
  - 子 Agent 把来源附在交回的回答后面（写给模型的，中文、带完整网址），卡片上也列出来；
  - 对话历史（messages）里 API 给的引用一个字都不动：下一次请求原样发回去，前面的历史字节都不变。
这里也查网址的安全：只有 http、https 的网址能走到界面上，javascript:、data: 这类一律丢掉。
"""

from __future__ import annotations

import json

import pytest
from anthropic.types.beta import (
    BetaCitationCharLocation,
    BetaCitationsWebSearchResultLocation,
)
from fake_api import reply, text, tool_use, web_cite, web_search
from fake_openai import chat

from harness import citations, i18n
from harness.agents import examples
from harness.channels import markup
from harness.channels.ui import ChannelUI
from harness.citations import (
    Numbering,
    for_model,
    from_citations,
    insert,
    listing,
    marker,
    site,
)
from harness.config import SOURCE_QUOTE_CHARS, SOURCE_TITLE_CHARS, SOURCE_URL_CHARS
from harness.ui import TerminalUI
from harness.webui.ui import REPLAYABLE, WebUI

WIKI = "https://en.wikipedia.org/wiki/Claude_Shannon"
WIKI_TITLE = "Claude Shannon - Wikipedia"
BRIT = "https://www.britannica.com/biography/Claude-Shannon"
BRIT_TITLE = "Claude Shannon | Biography"


def cited_answer() -> list[dict]:
    """一段典型的联网搜索回答：先搜一次，回答被引用切成几个文字块（第二处同时引了两个网页，其中一个前面引过）。"""
    return [
        *web_search("srvtoolu_1", "claude shannon", [(WIKI, WIKI_TITLE), (BRIT, BRIT_TITLE)]),
        text("Based on the results, "),
        text("Shannon was born in 1916", [web_cite(WIKI, WIKI_TITLE, "Claude Elwood Shannon (April 30, 1916 – February 24, 2001)", "idx-1")]),
        text(" in Petoskey", [web_cite(BRIT, BRIT_TITLE, "born April 30, 1916, Petoskey, Michigan", "idx-2"), web_cite(WIKI, WIKI_TITLE, "Petoskey, Michigan", "idx-3")]),
        text("."),
    ]


def events_of(kind: str, events: list[dict]) -> list[dict]:
    return [event for event in events if event["type"] == kind]


def source(url: str, title: str = "", quote: str = "") -> dict:
    """from_citations 交给界面的一条来源。"""
    return {"url": url, "title": title, "quote": quote}


# ---- 取出来源：from_citations ---------------------------------------------------------------


def test_only_http_and_https_urls_with_a_host_are_kept():
    good = ["https://example.com/a", "http://example.com/b"]
    bad = [
        "javascript:alert(1)", "JavaScript:alert(1)", "data:text/html,<script>alert(1)</script>", "file:///etc/passwd",
        "ftp://example.com/x", "mailto:someone@example.com", "/wiki/relative", "example.com/no-scheme", "https://",
        "https://example.com/has space", "https://example.com/tab\there", "https://example.com/" + "a" * SOURCE_URL_CHARS,
        "", None, 42,
    ]
    found = from_citations([{"url": url, "title": "t", "cited_text": "q"} for url in good + bad])
    assert [item["url"] for item in found] == good


def test_urls_are_trimmed_and_the_length_limit_is_inclusive():
    exact = "https://example.com/" + "a" * (SOURCE_URL_CHARS - len("https://example.com/"))
    assert len(exact) == SOURCE_URL_CHARS
    found = from_citations([{"url": "  https://example.com/x  "}, {"url": exact}, {"url": exact + "b"}])
    assert [item["url"] for item in found] == ["https://example.com/x", exact]


def test_titles_and_quotes_are_cleaned_collapsed_and_shortened():
    long_title, long_quote = "标" * 400, "q" * 400
    (item,) = from_citations([{"url": WIKI, "title": "  Claude\n\nShannon\t- Wiki  ", "cited_text": " born   in\n1916 "}])
    assert item == {"url": WIKI, "title": "Claude Shannon - Wiki", "quote": "born in 1916"}
    (short,) = from_citations([{"url": WIKI, "title": long_title, "cited_text": long_quote}])
    assert len(short["title"]) == SOURCE_TITLE_CHARS and short["title"].endswith("…")
    assert len(short["quote"]) == SOURCE_QUOTE_CHARS and short["quote"].endswith("…")
    # 落单的代理字符（半个表情）写不进 JSON：洗成 �
    (odd,) = from_citations([{"url": WIKI, "title": "bad \ud800 title", "cited_text": "half \udc00 emoji"}])
    assert "\ud800" not in odd["title"] and "�" in odd["title"] and "\udc00" not in odd["quote"]
    json.dumps(odd, ensure_ascii=False).encode("utf-8")  # 能编码，不抛错
    # 没有标题、没有原文：空字符串，不是 None
    (bare,) = from_citations([{"url": WIKI, "title": None}])
    assert bare == {"url": WIKI, "title": "", "quote": ""}


def test_the_same_url_twice_in_one_block_is_kept_once():
    found = from_citations([
        {"url": WIKI, "title": "first", "cited_text": "one"},
        {"url": BRIT, "title": "brit", "cited_text": "two"},
        {"url": WIKI, "title": "second", "cited_text": "three"},
    ])
    assert [(item["url"], item["title"]) for item in found] == [(WIKI, "first"), (BRIT, "brit")]


def test_sdk_objects_work_and_citations_without_a_url_are_ignored():
    web = BetaCitationsWebSearchResultLocation(cited_text="q", encrypted_index="e", title=WIKI_TITLE, type="web_search_result_location", url=WIKI)
    pdf = BetaCitationCharLocation(cited_text="q", document_index=0, document_title="报告.pdf", start_char_index=0, end_char_index=1, type="char_location")
    assert from_citations([pdf, web, {"type": "char_location", "cited_text": "x", "document_index": 0}]) == [source(WIKI, WIKI_TITLE, "q")]
    assert from_citations(None) == [] and from_citations([]) == []


# ---- 编号和写成文字 -----------------------------------------------------------------------


def test_numbering_dedupes_by_url_in_order_of_first_appearance():
    numbering = Numbering()
    marks, new = numbering.add([source(WIKI, WIKI_TITLE, "q1"), source(BRIT, BRIT_TITLE, "q2")])
    assert marks == [{"n": 1, "quote": "q1"}, {"n": 2, "quote": "q2"}]
    assert new == [
        {"n": 1, "url": WIKI, "title": WIKI_TITLE, "site": "en.wikipedia.org"},
        {"n": 2, "url": BRIT, "title": BRIT_TITLE, "site": "britannica.com"},
    ]
    # 后面一处又引了它们，再加一个新的：已有的沿用编号，新的接着数；new 里只有第一次出现的
    marks, new = numbering.add([source(BRIT, BRIT_TITLE, "q3"), source(WIKI, WIKI_TITLE, ""), source("https://c.example/x", "C")])
    assert marks == [{"n": 2, "quote": "q3"}, {"n": 1}, {"n": 3}]  # 没有原文的记号不带 quote
    assert new == [{"n": 3, "url": "https://c.example/x", "title": "C", "site": "c.example"}]
    assert [item["n"] for item in numbering.sources] == [1, 2, 3]


def test_one_mark_per_source_in_a_span_and_missing_titles_fall_back_to_the_site():
    numbering = Numbering()
    marks, new = numbering.add([source("https://www.x.org/p", "", "first"), source("https://www.x.org/p", "", "again")])
    assert marks == [{"n": 1, "quote": "first"}]
    assert new == [{"n": 1, "url": "https://www.x.org/p", "title": "x.org", "site": "x.org"}]
    assert numbering.add([]) == ([], [])


def test_clear_starts_counting_again():
    numbering = Numbering()
    numbering.add([source(WIKI, WIKI_TITLE), source(BRIT, BRIT_TITLE)])
    numbering.clear()
    assert numbering.sources == []
    marks, new = numbering.add([source(BRIT, BRIT_TITLE)])
    assert marks == [{"n": 1}] and new[0]["n"] == 1 and new[0]["url"] == BRIT


def test_site_marker_and_insert():
    assert site("https://www.Example.COM:8080/a?b") == "example.com"
    assert site(WIKI) == "en.wikipedia.org"
    assert site("not a url") == "" and site("https://[::1") == ""
    assert marker([{"n": 1}, {"n": 3, "quote": "x"}]) == "[1][3]" and marker([]) == ""
    # 记号紧跟在最后一个字后面：末尾的空白、换行留在它后面，不让它跑到下一行的开头
    assert insert("claim.\n\n", "[1]") == "claim.[1]\n\n"
    assert insert("claim ", "[2]") == "claim[2] "
    assert insert("claim", "[1]") == "claim[1]"
    assert insert("", "[1]") == "[1]"


def numbered(*items: tuple[str, str]) -> list[dict]:
    numbering = Numbering()
    numbering.add([source(url, title) for url, title in items])
    return numbering.sources


def test_listing_plain_and_markdown():
    sources = numbered((WIKI, WIKI_TITLE), ("https://www.x.org/p", ""))
    # 没有标题时，标题就是网站名：不再重复写一遍
    assert listing(sources) == f"资料来源：\n1. {WIKI_TITLE} — en.wikipedia.org\n2. x.org"
    tricky = numbered(("https://en.wikipedia.org/wiki/Python_(programming_language)", "C++ [draft] notes"))
    assert listing(tricky, markdown=True) == (
        "资料来源：\n1. [C++ (draft) notes](https://en.wikipedia.org/wiki/Python_%28programming_language%29) — en.wikipedia.org"
    )
    # 排成 Telegram 的 HTML 以后是一个完整的链接，网址没被括号截断
    html = markup.to_html(listing(tricky, markdown=True))
    assert '<a href="https://en.wikipedia.org/wiki/Python_%28programming_language%29">C++ (draft) notes</a>' in html


@pytest.mark.parametrize(("language", "heading"), [("en", "Sources:"), ("es", "Fuentes:"), ("zh", "资料来源：")])
def test_the_listing_heading_follows_the_language(monkeypatch, language, heading):
    monkeypatch.setattr(i18n, "current", language)
    assert listing(numbered((WIKI, WIKI_TITLE))).split("\n")[0] == heading


def test_the_text_for_the_model_is_chinese_with_full_urls(monkeypatch):
    monkeypatch.setattr(i18n, "current", "en")  # 界面换成英文，写给模型的照样是中文
    sources = numbered((WIKI, WIKI_TITLE), (BRIT, BRIT_TITLE))
    assert for_model(sources) == f"来源：\n[1] {WIKI_TITLE} — {WIKI}\n[2] {BRIT_TITLE} — {BRIT}"


# ---- 网页界面：从假 API 到 cite 事件 ----------------------------------------------------------


def test_a_cited_answer_becomes_cite_events_after_the_cited_text(start):
    ui = WebUI()
    h = start(reply(*cited_answer()), ui=ui)
    h.agent.run_turn("香农是哪年出生的")
    saved = ui.export_events()
    paragraph = saved[next(index for index, event in enumerate(saved) if event["type"] == "text") :]
    # 连着的文字块是同一段：中间没有 block_end，只在整段结束时有一个；引用的记号紧跟在被引用的那段文字后面
    assert [event["type"] for event in paragraph] == ["text", "cite", "text", "cite", "text", "block_end"]
    assert paragraph[0] == {"type": "text", "text": "Based on the results, Shannon was born in 1916"}
    assert paragraph[1] == {
        "type": "cite",
        "marks": [{"n": 1, "quote": "Claude Elwood Shannon (April 30, 1916 – February 24, 2001)"}],
        "sources": [{"n": 1, "url": WIKI, "title": WIKI_TITLE, "site": "en.wikipedia.org"}],
    }
    assert paragraph[2] == {"type": "text", "text": " in Petoskey"}
    # 第二处：新来源编成 2；前面引过的维基百科沿用 1，sources 里不再发一遍
    assert paragraph[3] == {
        "type": "cite",
        "marks": [{"n": 2, "quote": "born April 30, 1916, Petoskey, Michigan"}, {"n": 1, "quote": "Petoskey, Michigan"}],
        "sources": [{"n": 2, "url": BRIT, "title": BRIT_TITLE, "site": "britannica.com"}],
    }
    assert paragraph[4] == {"type": "text", "text": "."}


def test_answers_without_citations_have_no_cite_events(start):
    ui = WebUI()
    h = start(reply(text("第一段，"), text("第二段。")), ui=ui)
    h.agent.run_turn("你好")
    saved = ui.export_events()
    assert events_of("cite", saved) == []
    assert [event["type"] for event in saved] == ["text", "block_end"]
    # 引用列表是空的文字块也不算引用
    ui = WebUI()
    h = start(reply(text("没有来源。", [])), ui=ui)
    h.agent.run_turn("你好")
    assert events_of("cite", ui.export_events()) == []


def test_the_history_keeps_the_citations_exactly_as_the_api_sent_them(start):
    blocks = cited_answer()
    h = start(reply(*blocks), reply(text("好的。")), reply(text("再见。")), ui=WebUI())
    h.agent.run_turn("香农是哪年出生的")
    h.agent.run_turn("谢谢")
    h.agent.run_turn("再见")
    first, second, third = h.api.requests
    # 下一次请求里，那条回复原样发回去：引用连同 encrypted_index 一个字都没变
    assert second["messages"][1] == {"role": "assistant", "content": blocks}
    assert [citation["encrypted_index"] for citation in second["messages"][1]["content"][4]["citations"]] == ["idx-2", "idx-3"]
    # 只在末尾追加：后一次请求里前面的历史和前一次的字节一样（缓存才命中，思考的签名才对得上）
    shared = len(second["messages"])
    assert json.dumps(third["messages"][:shared], ensure_ascii=False) == json.dumps(second["messages"], ensure_ascii=False)
    assert json.dumps(second["messages"][:1], ensure_ascii=False) == json.dumps(first["messages"], ensure_ascii=False)


def test_numbering_restarts_after_a_tool_call(start):
    ui = WebUI()
    h = start(
        reply(text("先看看待办。", [web_cite(WIKI, WIKI_TITLE, "q")]), tool_use("toolu_1", "todo_list", {}), stop_reason="tool_use"),
        reply(text("看完了。", [web_cite(BRIT, BRIT_TITLE, "b"), web_cite(WIKI, WIKI_TITLE, "w")])),
        ui=ui,
    )
    h.agent.run_turn("看看待办")
    first, second = events_of("cite", ui.export_events())
    assert first["sources"] == [{"n": 1, "url": WIKI, "title": WIKI_TITLE, "site": "en.wikipedia.org"}]
    # 工具调用之后是新的一段回复：从 1 重新数，维基百科这次排第二
    assert second["marks"] == [{"n": 1, "quote": "b"}, {"n": 2, "quote": "w"}]
    assert [item["url"] for item in second["sources"]] == [BRIT, WIKI]


def test_numbering_continues_across_a_paused_turn(start):
    # 服务器端的搜索做到一半（pause_turn），循环原样再请求一次，模型接着写同一段回答。
    # 两次请求之间只有一个 block_end，页面上还是同一段（见 webui/ui.py 的 PARAGRAPH），编号接着数
    ui = WebUI()
    h = start(
        reply(text("第一句", [web_cite(WIKI, WIKI_TITLE, "w1")]), stop_reason="pause_turn"),
        reply(text("，第二句。", [web_cite(BRIT, BRIT_TITLE, "b"), web_cite(WIKI, WIKI_TITLE, "w2")])),
        ui=ui,
    )
    h.agent.run_turn("查一查")
    assert len(h.api.requests) == 2
    saved = ui.export_events()
    assert [event["type"] for event in saved] == ["text", "cite", "block_end", "text", "cite", "block_end"]
    first, second = events_of("cite", saved)
    assert first["marks"] == [{"n": 1, "quote": "w1"}]
    assert second["marks"] == [{"n": 2, "quote": "b"}, {"n": 1, "quote": "w2"}]
    assert second["sources"] == [{"n": 2, "url": BRIT, "title": BRIT_TITLE, "site": "britannica.com"}]


def test_cite_events_are_saved_and_replayed_unchanged(start):
    assert "cite" in REPLAYABLE
    ui = WebUI()
    h = start(reply(*cited_answer()), ui=ui)
    h.agent.run_turn("香农是哪年出生的")
    saved = ui.export_events()
    # 存进会话文件的样子能写成 JSON、读回来一样
    assert json.loads(json.dumps(saved, ensure_ascii=False)) == saved
    fresh = WebUI()
    fresh.load_events(saved)
    replayed = fresh.events_after(0, 0)
    assert replayed[0]["type"] == "reset"
    without_ids = [{key: value for key, value in event.items() if key != "id"} for event in replayed[1:]]
    assert without_ids == saved
    assert events_of("cite", without_ids) == events_of("cite", saved) and len(events_of("cite", saved)) == 2


# ---- 终端 ----------------------------------------------------------------------------------


def test_the_terminal_marks_cited_text_and_lists_the_sources(start, capsys):
    h = start(reply(*cited_answer()), ui=TerminalUI())
    capsys.readouterr()
    h.agent.run_turn("香农是哪年出生的")
    out = capsys.readouterr().out
    assert "Based on the results, Shannon was born in 1916[1] in Petoskey[2][1].\n" in out
    assert out.endswith(
        "资料来源：\n"
        f"  1. {WIKI_TITLE} — en.wikipedia.org\n"
        f"     {WIKI}\n"
        f"  2. {BRIT_TITLE} — britannica.com\n"
        f"     {BRIT}\n"
    )


def test_the_terminal_puts_the_marker_before_a_trailing_newline(start, capsys):
    h = start(reply(text("第一行说的事\n", [web_cite(WIKI, WIKI_TITLE, "q")]), text("第二行。")), ui=TerminalUI())
    capsys.readouterr()
    h.agent.run_turn("说说")
    out = capsys.readouterr().out
    assert "第一行说的事[1]\n第二行。\n资料来源：\n" in out


def test_the_terminal_output_without_citations_is_unchanged(start, capsys):
    h = start(reply(text("你好，"), text("我在。\n\n")), ui=TerminalUI())
    capsys.readouterr()
    h.agent.run_turn("你好")
    out = capsys.readouterr().out
    assert out == "你好，我在。\n\n\n"  # 末尾的换行照样写出来（只是晚一点），再加上一段结束的那个换行
    assert "资料来源" not in out and "[1]" not in out


# ---- 聊天通道 --------------------------------------------------------------------------------


def channel_ui() -> tuple[ChannelUI, list[str]]:
    sent: list[str] = []
    ui = ChannelUI(
        on_text=sent.append,
        on_notice=lambda level, said: None,
        on_confirm=lambda *args: None,
        on_settled=lambda *args: None,
        on_auto=lambda said: None,
    )
    return ui, sent


def test_a_chat_message_carries_markers_and_its_sources(start):
    ui, sent = channel_ui()
    h = start(reply(*cited_answer()), ui=ui)
    h.agent.run_turn("香农是哪年出生的")
    assert sent == [
        (
            "Based on the results, Shannon was born in 1916[1] in Petoskey[2][1].\n\n"
            "资料来源：\n"
            f"1. [{WIKI_TITLE}]({WIKI}) — en.wikipedia.org\n"
            f"2. [{BRIT_TITLE}]({BRIT}) — britannica.com"
        )
    ]
    # 网页上照样有 cite 事件（聊天里的会话在网页上打开时也看得到来源）
    assert len(events_of("cite", ui.export_events())) == 2


def test_chat_numbering_is_per_message(start):
    ui, sent = channel_ui()
    h = start(
        reply(text("先看看待办。", [web_cite(WIKI, WIKI_TITLE, "q")]), tool_use("toolu_1", "todo_list", {}), stop_reason="tool_use"),
        reply(text("看完了。", [web_cite(BRIT, BRIT_TITLE, "b"), web_cite(WIKI, WIKI_TITLE, "w")])),
        reply(text("没有引用的回答。")),
        ui=ui,
    )
    h.agent.run_turn("看看待办")
    h.agent.run_turn("还有吗")
    assert sent[0] == f"先看看待办。[1]\n\n资料来源：\n1. [{WIKI_TITLE}]({WIKI}) — en.wikipedia.org"
    assert sent[1] == (
        f"看完了。[1][2]\n\n资料来源：\n1. [{BRIT_TITLE}]({BRIT}) — britannica.com\n2. [{WIKI_TITLE}]({WIKI}) — en.wikipedia.org"
    )
    assert sent[2] == "没有引用的回答。"  # 上一条的来源不会跟到下一条上


def test_a_long_cited_message_is_split_within_the_platform_limit(start):
    ui, sent = channel_ui()
    urls = [f"https://site{n}.example/page/{n}" for n in range(1, 6)]
    blocks = [text(f"第 {n} 句话说的是一件需要出处的事情，写得长一点好让消息超过上限。 ", [web_cite(url, f"Source {n}", "q")]) for n, url in enumerate(urls, 1)]
    h = start(reply(*blocks), ui=ui)
    h.agent.run_turn("长一点")
    (message,) = sent
    assert markup.length(message) > 120
    pieces = markup.split(message, 120)
    assert len(pieces) > 1 and all(markup.length(piece) <= 120 for piece in pieces)
    joined = "\n".join(pieces)
    for n, url in enumerate(urls, 1):
        assert f"[{n}]" in joined and url in joined and f"Source {n}" in joined


# ---- 子 Agent ------------------------------------------------------------------------------


def test_a_subagent_hands_back_its_sources(start):
    sub_answer = reply(
        *web_search("srvtoolu_s", "shannon", [(WIKI, WIKI_TITLE)]),
        text("香农生于 1916 年", [web_cite(WIKI, WIKI_TITLE, "April 30, 1916")]),
        text("。"),
    )
    h = start(
        reply(tool_use("t1", "agent", {"name": "资料研究员", "task": "查一下香农"}), stop_reason="tool_use"),
        sub_answer,
        reply(text("研究员查到了。")),
    )
    h.app.agents.install(examples.find("researcher").text, replace=True)
    h.agent.run_turn("让资料研究员查一下香农")
    parent_again = h.api.requests[-1]
    (result,) = [
        block for message in parent_again["messages"] if message["role"] == "user" and isinstance(message["content"], list)
        for block in message["content"] if block.get("type") == "tool_result" and block.get("tool_use_id") == "t1"
    ]
    content = result["content"] if isinstance(result["content"], str) else "".join(part.get("text", "") for part in result["content"])
    # 交给主助手的回答里有记号，后面附着来源（写给模型的：中文、完整的网址）
    assert "香农生于 1916 年[1]。" in content
    assert f"来源：\n[1] {WIKI_TITLE} — {WIKI}" in content
    # 卡片上：回答里有记号，来源单独带着（页面列出来），回答本身不重复列一遍
    done = h.ui.subagents[-1]
    assert done[0] == "done" and done[1]["status"] == "done"
    assert done[1]["answer"] == "香农生于 1916 年[1]。"
    assert done[1]["sources"] == [{"n": 1, "url": WIKI, "title": WIKI_TITLE, "site": "en.wikipedia.org"}]
    # 主助手自己这一边没有收到 cite：那是子 Agent 的回答
    assert h.ui.cites == []


def test_a_subagent_without_citations_sends_no_sources(start):
    h = start(
        reply(tool_use("t1", "agent", {"name": "资料研究员", "task": "查一下"}), stop_reason="tool_use"),
        reply(text("没有出处的回答。")),
        reply(text("好。")),
    )
    h.app.agents.install(examples.find("researcher").text, replace=True)
    h.agent.run_turn("交给研究员")
    done = h.ui.subagents[-1][1]
    assert done["answer"] == "没有出处的回答。" and "sources" not in done


# ---- 别家的模型、安全 --------------------------------------------------------------------------


def test_openai_compatible_providers_are_unaffected(start_openai):
    h = start_openai(chat("你好！"))
    h.agent.run_turn("你好")
    assert "".join(value for kind, value in h.ui.streamed if kind == "text") == "你好！"
    assert h.ui.cites == []


def test_unsafe_urls_never_reach_any_interface(start, capsys):
    unsafe = [
        web_cite("javascript:alert(1)", "evil", "q"),
        web_cite("data:text/html,<script>alert(1)</script>", "evil data", "q"),
        web_cite(WIKI, WIKI_TITLE, "q"),
    ]

    def answer() -> dict:
        return reply(text("一句话", unsafe), text("。"))

    ui = WebUI()
    h = start(answer(), ui=ui)
    h.agent.run_turn("说说")
    events = json.dumps(ui.export_events(), ensure_ascii=False)
    assert "javascript:" not in events and "data:text" not in events
    (cite,) = events_of("cite", ui.export_events())
    assert cite["marks"] == [{"n": 1, "quote": "q"}] and [item["url"] for item in cite["sources"]] == [WIKI]

    channel, sent = channel_ui()
    h = start(answer(), ui=channel)
    h.agent.run_turn("说说")
    assert sent == [f"一句话[1]。\n\n资料来源：\n1. [{WIKI_TITLE}]({WIKI}) — en.wikipedia.org"]

    h = start(answer(), ui=TerminalUI())
    capsys.readouterr()
    h.agent.run_turn("说说")
    out = capsys.readouterr().out
    assert "javascript:" not in out and "data:text" not in out and "evil" not in out and "一句话[1]。" in out


def test_only_unsafe_citations_mean_no_cite_at_all(start):
    ui = WebUI()
    h = start(reply(text("一句话", [web_cite("javascript:alert(1)", "evil", "q")])), ui=ui)
    h.agent.run_turn("说说")
    assert events_of("cite", ui.export_events()) == []
    assert citations.from_citations([{"url": "javascript:alert(1)"}]) == []
