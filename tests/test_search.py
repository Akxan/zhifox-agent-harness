"""会话搜索的测试：拆关键词、统一写法、三档匹配、排序、摘录，以及会话库怎样让索引跟着文件走。

网页上的搜索接口（/api/search）在 test_webui.py 的最后一节里测。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from harness.conversations import Conversation, ConversationStore
from harness.search import (
    GAP,
    LOOSE_MAX,
    MAX_MARKS,
    MAX_QUERY_CHARS,
    MAX_RESULTS,
    MAX_TERMS,
    RESULT_CHARS,
    SNIPPET_AFTER,
    SNIPPET_BEFORE,
    WEIGHTS,
    SearchIndex,
    make_passage,
    split_query,
)

# ---- 搭积木用的小函数 -----------------------------------------------------
# 索引收录的是保存下来的界面事件。下面几个函数各造一种事件，名字就是"谁做了什么"。


def said(text: str) -> dict:
    """用户说的一句话。"""
    return {"type": "user", "text": text}


def answered(text: str) -> dict:
    """助手的一段回复。"""
    return {"type": "text", "text": text}


def thought(text: str) -> dict:
    return {"type": "thinking", "text": text}


def called(summary: str) -> dict:
    """一次工具调用。可以搜的是 summary：界面上显示的那一行（工具名加参数）。"""
    return {"type": "tool_call", "name": summary.split()[0], "summary": summary, "input": {}}


def returned(text: str) -> dict:
    return {"type": "tool_result", "text": text, "is_error": False}


def index_of(**conversations) -> SearchIndex:
    """建一个索引。每个参数是一段会话：参数名当编号，值是事件列表，或者（标题，事件列表）。越靠后的越新。"""
    index = SearchIndex()
    for age, (conversation_id, content) in enumerate(conversations.items()):
        title, events = content if isinstance(content, tuple) else ("", content)
        index.put(conversation_id, title, events, updated_at=float(age))
    return index


def ids(results: list[dict]) -> list[str]:
    return [item["id"] for item in results]


def marked(text: str, marks: list[list[int]]) -> list[str]:
    """照浏览器的办法，从文字里切出标出来的每一段。JavaScript 的字符串按 UTF-16 计数，所以这里也这样切。"""
    raw = text.encode("utf-16-le")
    return [raw[first * 2 : last * 2].decode("utf-16-le") for first, last in marks]


# ---- 拆关键词、统一写法 ---------------------------------------------------


def test_query_is_split_into_folded_unique_terms():
    assert (MAX_QUERY_CHARS, MAX_TERMS) == (200, 8)
    # 按空白拆开：空格、制表符、换行、全角空格都算；前后的空白不算
    assert split_query("  交报告\t周五\n下午　全角空格  ") == ["交报告", "周五", "下午", "全角空格"]
    # 统一写法：大写变小写，全角的字母和数字变成半角的
    assert split_query("Harness ＡＰＩ １２３") == ["harness", "api", "123"]
    # 统一写法之后重复的只留一个，顺序按第一次出现的算
    assert split_query("b Harness a HARNESS ｈａｒｎｅｓｓ b") == ["b", "harness", "a"]
    # 什么都没有
    assert split_query("") == [] and split_query(" \n\t　") == []
    # 关键词太多：只要前 8 个
    assert split_query(" ".join(f"词{number}" for number in range(20))) == [f"词{number}" for number in range(8)]
    # 太长：只看前 200 个字符
    assert split_query("a" * 300) == ["a" * 200]
    assert split_query("a" * 198 + " bcdef") == ["a" * 198, "b"]


def test_query_and_text_are_folded_the_same_way():
    # 有些字符统一写法后长度会变（"…"会变成三个点，"㎏"会变成 kg）。正文为了保持等长，把它们原样留着，
    # 所以搜索词里的也必须原样留着；不然照着正文一个字一个字敲进去，反而找不到
    assert split_query("稍等… 5㎏ 30℃") == ["稍等…", "5㎏", "30℃"]
    index = index_of(a=[said("稍等…马上好，重 5㎏，温度 30℃")])
    for query in ("稍等…", "…", "5㎏", "㎏", "30℃", "稍等…马上好"):
        (found,) = index.search(query)
        assert found["exact"] is True and found["snippet"]["find"] == query
    # 同一句话里别的字照常统一写法
    assert ids(index.search("稍等… ５㎏")) == ["a"]


def test_passage_keeps_the_text_as_written_next_to_a_folded_copy():
    passage = make_passage("user", "  你好！\n\n这是 ＡＰＩ　的\tＶ２ 版（测试） ")
    # 给人看的那一份：只把连续的空白压成一个空格，字还是原来的字（全角的标点、字母都不动）
    assert passage.kind == "user" and passage.shown == "你好！ 这是 ＡＰＩ 的 Ｖ２ 版（测试）"
    # 用来比较的那一份：小写、半角，和原文一样长、位置一一对应
    assert passage.folded == "你好! 这是 api 的 v2 版(测试)"
    assert len(passage.folded) == len(passage.shown)

    # 有些字符统一写法后长度会变："㎏"会变成两个字母，"İ"转成小写会多出一个符号，"…"会变成三个点。
    # 这些字保持原样，别的字照常处理，两份文字的位置才对得上
    for written, folded in (
        ("重 5㎏ 的 ＢＯＸ", "重 5㎏ 的 box"),
        ("İstanbul ＡＢＣ", "İstanbul abc"),
        ("稍等…ＯＫ", "稍等…ok"),
    ):
        passage = make_passage("text", written)
        assert (passage.shown, passage.folded) == (written, folded)
        assert len(passage.folded) == len(passage.shown)

    assert make_passage("title", "") == make_passage("title", " \n\t ")
    assert (make_passage("title", "").shown, make_passage("title", "").folded) == ("", "")


# ---- 第一档：原样包含 -----------------------------------------------------


def test_exact_match_ignores_case_and_width():
    index = index_of(
        a=[said("Ｈａｒｎｅｓｓ 是包在模型外面的那层程序")],
        b=[said("今天天气不错")],
        c=[answered("可以看 README 里关于 Harness 的说明")],
    )
    for query in ("harness", "HARNESS", "HarNess", "ｈａｒｎｅｓｓ", "ＨＡＲＮＥＳＳ"):
        results = index.search(query)
        assert ids(results) == ["a", "c"] and all(item["exact"] for item in results)
        # 摘录里是原文的写法，命中的原文也是
        assert [item["snippet"]["find"] for item in results] == ["Ｈａｒｎｅｓｓ", "Harness"]
        for item in results:
            assert marked(item["snippet"]["text"], item["snippet"]["marks"]) == [item["snippet"]["find"]]
    # 反过来也一样：正文是半角大写，搜索词是全角小写
    assert ids(index.search("ｒｅａｄｍｅ")) == ["c"]
    # "包含"就是包含，不要求是一个完整的词
    assert ids(index.search("arnes")) == ["a", "c"]
    assert index.search("天气不好") == [] and index.search("完全没有") == []


# ---- 第二档：相近的词 -----------------------------------------------------


def test_a_misspelled_word_finds_the_one_it_was_meant_to_be():
    index = index_of(a=[said("harness 是什么意思")], b=[said("今天天气不错")])
    for typo in ("hraness", "harnesss", "harmess", "HRANESS"):  # 两个字母敲反了、多敲了一个、敲错了一个
        (found,) = index.search(typo)
        assert found["id"] == "a" and found["exact"] is False  # 找到了，但要说明不是原样找到的
        snippet = found["snippet"]
        assert snippet["find"] == "harness" and marked(snippet["text"], snippet["marks"]) == ["harness"]

    # 只在这个词哪儿都原样找不到时才去猜。一旦真有会话里是这个写法，就只认原样的
    index.put("c", "", [said("我把 harness 敲成了 hraness")], updated_at=9.0)
    (found,) = index.search("hraness")
    assert found["id"] == "c" and found["exact"] is True and found["snippet"]["find"] == "hraness"


def test_similar_words_need_five_letters_and_plain_words():
    index = index_of(a=[said("word 和 words 都在这里，部署在 kubernetes 上")])
    # 四个字母的词太短：差一个字母就可能是另一个词了，所以不猜
    assert index.search("wrod") == []
    # 四个字母的词少敲了一个字母时，是靠"隔字包含"找到的，不是靠猜：标出来的只是对上的那几个字母。
    # （要是猜成了 "words"，标出来的会是整个词）
    (found,) = index.search("wrds")
    assert found["snippet"]["find"] == "words" and marked(found["snippet"]["text"], found["snippet"]["marks"]) == ["w", "rds"]
    # 五个字母起才猜
    (found,) = index.search("wrods")
    assert found["exact"] is False and found["snippet"]["find"] == "words"
    assert marked(found["snippet"]["text"], found["snippet"]["marks"]) == ["words"]
    # 带标点或汉字的不是"英文单词"，不猜。去掉多出来的那个字符就猜得到，说明差别只在这里
    assert ids(index.search("kubernetse")) == ["a"]
    assert index.search("kubernetse!") == [] and index.search("kubernetse词") == []


def test_similar_words_come_from_everything_indexed_right_now():
    index = index_of(a=[said("harness 是什么")])
    assert ids(index.search("hraness")) == ["a"]  # 这一次搜索把"出现过的词"算了一遍并记了下来
    # 之后收录的新会话里的词也要认得：记下来的那份词表必须跟着作废重算
    index.put("b", "", [said("部署到 kubernetes 上")], updated_at=5.0)
    (found,) = index.search("kubernetse")
    assert found["id"] == "b" and found["snippet"]["find"] == "kubernetes"
    # 标题里的词也算
    index.put("c", "关于 postgres", [], updated_at=6.0)
    (found,) = index.search("postgers")
    assert found["id"] == "c" and found["title_marks"] == [[3, 11]] and found["snippet"] is None
    # 删掉之后就猜不到它头上了
    index.remove("a")
    assert index.search("hraness") == []


# ---- 第三档：隔字包含 -----------------------------------------------------


def test_letters_in_order_with_small_gaps_still_match():
    index = index_of(a=[said("harness 是什么")], b=[said("提醒我周五下午交报告")], c=[said("交了报告就走")])

    # 少敲了几个字母
    (found,) = index.search("hrns")
    assert found["id"] == "a" and found["exact"] is False
    snippet = found["snippet"]
    # 命中的原文是从第一个字到最后一个字的那一段；标出来的只有对上的那几个字，不包括中间夹着的
    assert snippet["find"] == "harnes" and marked(snippet["text"], snippet["marks"]) == ["h", "rn", "s"]

    # 中文：记得的几个字中间还隔着别的字
    (found,) = index.search("周五报告")
    assert found["id"] == "b" and found["exact"] is False
    snippet = found["snippet"]
    assert snippet["find"] == "周五下午交报告" and marked(snippet["text"], snippet["marks"]) == ["周五", "报告"]

    # 原样有的排在前面，隔着字对上的排在后面
    assert [(item["id"], item["exact"]) for item in index.search("交报")] == [("b", True), ("c", False)]


def test_loose_matches_must_stay_tight():
    assert GAP == 3
    # 中间夹的字不能比关键词本身还多，否则就太牵强了
    index = index_of(tight=[said("交了报告")], wide=[said("交了一份报告")])  # 夹了 1 个字；夹了 3 个字（关键词才 2 个字）
    assert ids(index.search("交报")) == ["tight"]
    # 相邻的两个字之间最多隔 3 个字。下面两段夹的字加起来都不算多，差别只在某一处隔了 3 个还是 4 个
    index = index_of(three=[said("一二三甲乙丙四五六")], four=[said("一二三甲乙丙丁四五六")])
    assert ids(index.search("一二三四五六")) == ["three"]


def test_very_short_terms_are_only_matched_exactly():
    index = index_of(a=[said("harness 交了报告")])
    # 英文不到 3 个字母、中文不到 2 个字：隔着字去找几乎什么都能对上，所以只认原样包含。
    # "hr" 要是也隔着字找，就会对上 "har"
    assert index.search("hr") == [] and ids(index.search("ha")) == ["a"]
    assert index.search("叫") == [] and ids(index.search("交")) == ["a"]
    # 英文 3 个字母起、中文 2 个字起，可以隔着字找
    assert [(item["id"], item["exact"]) for item in index.search("hns")] == [("a", False)]
    assert [(item["id"], item["exact"]) for item in index.search("交报")] == [("a", False)]


def test_the_tightest_loose_match_wins():
    # 同一句话里有好几处都对得上：取夹的字最少的那一处
    index = index_of(a=[said("先是 axbxc 后来又有 abxc 和 axxbc")])
    assert index.search("abc")[0]["snippet"]["find"] == "abxc"
    # 分散在好几句话里也一样
    index = index_of(a=[said("这里是 axbxc"), said("这里是 abxc"), said("这里是 axxbc")])
    assert index.search("abc")[0]["snippet"]["text"] == "这里是 abxc"
    # 几处互相重叠时也要挑得出来：从第一个 a 开始也对得上（a--ab-cd），但从第二个 a 开始的那一处更紧凑
    index = index_of(a=[said("a--ab-cd")])
    assert index.search("abcd")[0]["snippet"]["find"] == "ab-cd"


STALL_SCRIPT = """
import json, time
from harness.search import SearchIndex

index = SearchIndex()
index.put("plain", "", [{"type": "user", "text": "哈" * 25}], 0.0)
index.put("broken", "", [{"type": "user", "text": ("哈" * 30 + "嘿") * 20}], 1.0)
index.put("tail", "", [{"type": "user", "text": "哈" * 300 + "嘿"}], 2.0)

def run(query):
    return [[item["id"], item["exact"]] for item in index.search(query)]

start = time.perf_counter()
report = {"loose": run("哈" * 31), "none": run("哈" * 20 + "嘿嘿"), "long": run("哈" * 60)}
report["seconds"] = time.perf_counter() - start
print(json.dumps(report))
"""


def test_repeated_characters_do_not_stall_the_search():
    # 一长串重复的字，配上一个也是重复的字、但原样对不上的搜索词。"隔字包含"的正则如果写成"两个字之间随便夹
    # 几个什么字都行"，遇到这种输入就会把"夹哪几个"的每一种可能都试一遍，花的时间随长度成倍地涨，足以把整个程序
    # 卡死（正则运行的时候，别的线程也动不了）。现在的写法是"下一个字总是取最先出现的那个"，每一步只有一种走法。
    # 这个测试放到另一个进程里跑：万一哪天又卡住了，到时间就算失败，不会连累整套测试一起卡住。
    root = Path(__file__).resolve().parent.parent
    done = subprocess.run(
        [sys.executable, "-c", STALL_SCRIPT], check=False, cwd=root, capture_output=True, text=True, timeout=5
    )
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    # 31 个"哈"：tail 里原样就有；broken 里每 30 个就被一个"嘿"隔开，是隔着一个字对上的；plain 只有 25 个，不算
    assert report["loose"] == [["tail", True], ["broken", False]]
    # 20 个"哈"后面跟两个"嘿"：哪儿都凑不出来。要紧的是能很快得出"没有"这个结论
    assert report["none"] == []
    # 60 个"哈"：超过了隔字包含的长度上限，只认原样有的
    assert report["long"] == [["tail", True]]
    assert report["seconds"] < 2


def test_very_long_terms_are_only_matched_exactly():
    assert LOOSE_MAX == 32
    # 中间隔着一个"嘿"。32 个字的关键词还可以隔着字找，再长就只认原样包含了：
    # 那么长的一串，要么原样找得到，要么就是找不到
    index = index_of(a=[said("哈" * 16 + "嘿" + "哈" * 17)])
    assert [(item["id"], item["exact"]) for item in index.search("哈" * 32)] == [("a", False)]
    assert index.search("哈" * 33) == []
    assert [(item["id"], item["exact"]) for item in index.search("哈" * 17)] == [("a", True)]


# ---- 多个关键词 -----------------------------------------------------------


def test_every_term_must_be_found_somewhere_in_any_order():
    index = index_of(
        report=("周报", [said("提醒我周五下午交报告"), answered("记下了，到时候提醒你")]),
        hike=[said("周五去爬山")],
    )
    assert ids(index.search("周五")) == ["hike", "report"]  # 一样相关，新的在前
    # 两个词都要有
    assert ids(index.search("周五 报告")) == ["report"] == ids(index.search("报告  周五"))
    # 不要求挨着，也不要求在同一句话里：这三个词一个在用户的话里，一个在助手的回复里，一个在标题里
    assert ids(index.search("交报告 记下了 周报")) == ["report"]
    # 有一个词哪儿都找不到，这段会话就不算
    assert index.search("周五 报告 爬山") == [] and index.search("周五 哪儿都没有") == []


def test_one_fuzzy_term_makes_the_whole_result_fuzzy():
    index = index_of(a=[said("周五交了报告")])
    assert [item["exact"] for item in index.search("周五 报告")] == [True]
    # "交报"是隔着一个字对上的：每个关键词都原样找到才算"原样"
    (found,) = index.search("周五 交报")
    assert found["exact"] is False
    assert marked(found["snippet"]["text"], found["snippet"]["marks"]) == ["周五交", "报"]


# ---- 排序 -----------------------------------------------------------------


def test_where_a_match_is_decides_its_rank():
    # 分量从大到小：标题、用户的话、助手的回复、工具调用、思考、工具结果
    assert sorted(WEIGHTS, key=WEIGHTS.get, reverse=True) == ["title", "user", "assistant", "tool", "thinking", "result"]
    index = index_of(  # 故意按分量从大到小的顺序放进去：越靠后越新，所以结果的顺序只能是分量决定的
        in_title=("蓝莓计划", [said("开始吧")]),
        in_user=[said("买点蓝莓")],
        in_reply=[answered("蓝莓放冰箱")],
        in_call=[called('todo_add {"title": "买蓝莓"}')],
        in_thinking=[thought("用户想买蓝莓")],
        in_result=[returned("蓝莓 2 盒")],
    )
    results = index.search("蓝莓")
    assert ids(results) == ["in_title", "in_user", "in_reply", "in_call", "in_thinking", "in_result"]
    assert all(item["exact"] for item in results)
    kinds = [item["snippet"] and item["snippet"]["kind"] for item in results]
    assert kinds == [None, "user", "assistant", "tool", "thinking", "result"]


def test_ties_go_to_the_newer_conversation_and_the_best_place_counts():
    index = index_of(older=[said("买点蓝莓")], newer=[said("蓝莓买了吗")])
    assert ids(index.search("蓝莓")) == ["newer", "older"]
    # 同一段会话里有好几处：按最好的那一处算。这里它在用户的话里也有，所以排在只有回复里有的前面
    index = index_of(both=[returned("蓝莓 2 盒"), said("周五买蓝莓")], reply_only=[answered("蓝莓放冰箱")])
    results = index.search("蓝莓")
    assert ids(results) == ["both", "reply_only"] and results[0]["snippet"]["kind"] == "user"


def test_exact_matches_come_before_fuzzy_ones():
    # 页面把结果分成"找到的"和"相近的"两组显示，所以原样找到的必须整体排在前面，
    # 哪怕它只是出现在分量最轻的工具结果里，而模糊对上的是分量最重的标题
    index = index_of(
        loose_wide=[said("周五下午交报告")],
        loose_tight=[said("周五的报告")],
        exact_in_result=[returned("周五报告.docx")],
        loose_in_title=("周五的报告", []),
    )
    assert [(item["id"], item["exact"]) for item in index.search("周五报告")] == [
        ("exact_in_result", True),
        ("loose_in_title", False),  # 模糊的那一组里：标题里的在前
        ("loose_tight", False),  # 然后是夹的字少的
        ("loose_wide", False),
    ]
    # 关键词长一些时，标题里的模糊命中算出来的分数会超过工具结果里的原样命中；顺序仍然是原样的在前
    index = index_of(exact_in_result=[returned("这里提到了 harness")], loose_in_title=("har-ness 的说明", []))
    assert [(item["id"], item["exact"]) for item in index.search("harness")] == [
        ("exact_in_result", True),
        ("loose_in_title", False),
    ]


# ---- 摘录和标出来的位置 ---------------------------------------------------


def test_snippet_shows_what_surrounds_the_match():
    assert (SNIPPET_BEFORE, SNIPPET_AFTER) == (16, 64)
    before, after = "前" * 30, "后" * 100
    index = index_of(
        middle=[answered(before + "蓝莓" + after)],
        start=[answered("蓝莓" + after)],
        end=[answered(before + "蓝莓")],
        short=[answered("买点\n蓝莓  吧")],
    )
    snippets = {item["id"]: item["snippet"] for item in index.search("蓝莓")}
    # 命中的地方前面带 16 个字、后面带 64 个字；被截掉的那一头用省略号表示
    assert snippets["middle"]["text"] == "…" + "前" * 16 + "蓝莓" + "后" * 64 + "…"
    assert snippets["start"]["text"] == "蓝莓" + "后" * 64 + "…"
    assert snippets["end"]["text"] == "…" + "前" * 16 + "蓝莓"
    assert snippets["short"]["text"] == "买点 蓝莓 吧"  # 换行和连续的空格压成了一个空格
    for snippet in snippets.values():
        assert snippet["kind"] == "assistant" and snippet["find"] == "蓝莓"
        assert marked(snippet["text"], snippet["marks"]) == ["蓝莓"]
    # 开头多了一个省略号，标的位置也跟着往后挪了一格
    assert snippets["middle"]["marks"] == [[17, 19]] and snippets["start"]["marks"] == [[0, 2]]


def test_marks_count_the_way_the_browser_does():
    # JavaScript 的字符串按 UTF-16 计数，一个表情符号占两格。位置要是按 Python 的数法给，表情后面的就全标偏了
    index = index_of(a=("😀 周报", [said("好的 😀😀 交报告 👍 再交报告")]))
    (found,) = index.search("报告")
    snippet = found["snippet"]
    assert snippet["text"] == "好的 😀😀 交报告 👍 再交报告"
    assert snippet["marks"] == [[9, 11], [17, 19]]  # 按 Python 的数法是 [7, 9] 和 [14, 16]
    assert marked(snippet["text"], snippet["marks"]) == ["报告", "报告"]
    # 标题也一样
    (found,) = index.search("周报")
    assert found["title"] == "😀 周报" and found["title_marks"] == [[3, 5]]
    assert marked(found["title"], found["title_marks"]) == ["周报"]


def test_touching_and_overlapping_marks_are_merged():
    index = index_of(a=[said("周五下午去报告诉求")])
    (found,) = index.search("周五 下午 报告 告诉")
    # "周五"和"下午"挨着，"报告"和"告诉"重叠：各并成一段，页面上才不会标得支离破碎
    assert marked(found["snippet"]["text"], found["snippet"]["marks"]) == ["周五下午", "报告诉"]
    assert found["snippet"]["marks"] == [[0, 4], [5, 8]]


def test_a_snippet_marks_only_so_many_places():
    index = index_of(a=[said("啊 " * 60)])
    (found,) = index.search("啊")
    marks = found["snippet"]["marks"]
    assert len(marks) == MAX_MARKS == 24 and marks[:2] == [[0, 1], [2, 3]]


def test_title_matches_still_get_a_snippet_when_the_text_has_it_too():
    index = index_of(
        both=("蓝莓计划", [said("开始吧"), answered("先买蓝莓")]),
        title_only=("蓝莓笔记", [said("随便聊聊")]),
    )
    results = {item["id"]: item for item in index.search("蓝莓")}
    assert set(results["both"]) == {"id", "exact", "title", "title_marks", "snippet"}
    # 标题对上了，内容里也有：照样摘一段出来
    assert results["both"]["title"] == "蓝莓计划" and results["both"]["title_marks"] == [[0, 2]]
    assert results["both"]["snippet"] == {"kind": "assistant", "text": "先买蓝莓", "marks": [[2, 4]], "find": "蓝莓"}
    # 只有标题对得上：没有摘录
    assert results["title_only"]["title_marks"] == [[0, 2]] and results["title_only"]["snippet"] is None

    # 标题里是隔着字对上的：标出来的同样只是对上的那几个字
    index = index_of(a=("提醒我周五下午交报告", [said("好")]))
    (found,) = index.search("周五报告")
    assert found["exact"] is False and marked(found["title"], found["title_marks"]) == ["周五", "报告"]
    # 标题里没有的词，标题上什么都不标
    index = index_of(a=("周报", [said("提醒我交报告")]))
    assert index.search("交报告")[0]["title_marks"] == []

    # 标题已经对上了的时候，内容里只摘录原样有的；隔着字才对得上的那种太牵强，不拿出来给人看
    index = index_of(a=("周五报告", [said("周五的报告写完了")]))
    (found,) = index.search("周五报告")
    assert found["exact"] is True and found["title_marks"] == [[0, 4]] and found["snippet"] is None


def test_the_snippet_comes_from_the_best_place():
    # 同一个词有好几处时，摘录分量最重的那一处
    index = index_of(a=[returned("蓝莓 2 盒"), thought("要买蓝莓"), said("周五买蓝莓")])
    assert index.search("蓝莓")[0]["snippet"]["kind"] == "user"
    # 分量一样时，摘录靠前的那一处
    index = index_of(a=[said("第一句有蓝莓"), said("第二句也有蓝莓")])
    assert index.search("蓝莓")[0]["snippet"]["text"] == "第一句有蓝莓"
    # 几个关键词落在不同的地方：摘录的是其中分量最重的那一处，别的关键词如果也在这一段里，一起标出来
    index = index_of(a=[returned("草莓 3 盒"), said("蓝莓和草莓都要")])
    snippet = index.search("草莓 蓝莓")[0]["snippet"]
    assert snippet["kind"] == "user" and marked(snippet["text"], snippet["marks"]) == ["蓝莓", "草莓"]
    # 一个词只在工具结果里，另一个只在用户的话里：摘录用户的那一句
    index = index_of(a=[returned("草莓 3 盒"), said("蓝莓要两盒")])
    for query in ("草莓 蓝莓", "蓝莓 草莓"):
        snippet = index.search(query)[0]["snippet"]
        assert (snippet["kind"], snippet["text"], snippet["find"]) == ("user", "蓝莓要两盒", "蓝莓")
        assert marked(snippet["text"], snippet["marks"]) == ["蓝莓"]


# ---- 收录哪些内容 ---------------------------------------------------------


def test_only_what_was_said_and_done_is_searchable():
    index = index_of(
        a=(
            "标题",
            [
                said("用户的话甲"),
                {"type": "turn_start"},
                thought("思考的内容乙"),
                {"type": "block_end"},
                {
                    "type": "tool_call",
                    "name": "run_command",
                    "summary": "run_command $ ls 丙目录",
                    "input": {"command": "ls 丙目录", "note": "只在参数里的丁"},
                },
                {"type": "confirm", "description": "执行命令：ls 戊", "outcome": "allowed"},
                returned("结果里的己"),
                answered("回复里的庚"),
                {"type": "notice", "level": "info", "text": "提示里的辛"},
                {"type": "turn_end"},
            ],
        )
    )

    def where(query: str) -> str | None:
        found = index.search(query)
        return found[0]["snippet"]["kind"] if found else None

    # 收录的：用户的话、助手的回复、思考、工具调用在界面上显示的那一行、工具结果
    assert [where(query) for query in "甲乙丙己庚"] == ["user", "thinking", "tool", "result", "assistant"]
    # 不收录的：工具调用的原始参数、确认卡片、提示
    assert [where(query) for query in "丁戊辛"] == [None, None, None]
    assert index.search("turn_start") == [] and index.search("allowed") == []


def test_long_tool_results_are_indexed_only_at_the_start():
    assert RESULT_CHARS == 2000
    index = index_of(
        inside=[returned("甲" * (RESULT_CHARS - 2) + "尾巴")],  # 正好在前 2000 个字符之内
        outside=[returned("甲" * (RESULT_CHARS - 1) + "尾巴")],  # "巴"字落在了外面
        long_talk=[said("甲" * 5000 + "尾巴")],  # 只有工具结果会被截短，说的话再长也全部收录
    )
    assert ids(index.search("尾巴")) == ["long_talk", "inside"]


def test_damaged_data_is_skipped_not_fatal():
    # 这些内容是从磁盘上的文件里读回来的。文件万一被改坏了，事件列表里什么都可能出现：
    # 坏的跳过，好的照常收录。要是在这里出错，整个程序就启动不了了
    index = SearchIndex()
    index.put(
        "a",
        "标题",
        [
            None,
            5,
            "一段字符串",
            ["一个列表"],
            {"text": "没有类型乙"},
            {"type": "user"},  # 没有文字
            {"type": "user", "text": 123},  # 文字不是字符串
            {"type": "text", "text": None},
            {"type": "tool_call", "summary": ["乙"]},
            {"type": ["不能当字典键的类型"], "text": "乙"},
            {"type": {"也": "不行"}, "text": "乙"},
            {"type": 7, "text": "乙"},
            {"type": "user", "text": "  \n "},  # 只有空白
            {"type": "user", "text": "好的数据甲"},
        ],
    )
    (found,) = index.search("甲")
    assert found["id"] == "a" and found["snippet"]["text"] == "好的数据甲"
    assert index.search("乙") == [] and index.search("123") == []

    # 标题不是字符串：当作没有标题
    for title in (None, 5, ["标题"]):
        index.put("b", title, [said("丙")])
        (found,) = index.search("丙")
        assert found["id"] == "b" and found["title"] == "" and found["title_marks"] == []


# ---- 上限，以及索引怎样跟着变 ---------------------------------------------


def test_results_are_capped_at_the_most_relevant_fifty():
    assert MAX_RESULTS == 50
    index = SearchIndex()
    for number in range(60):
        index.put(f"c{number}", "", [said("蓝莓")], updated_at=float(number))
    # 一样相关时新的在前，所以留下的是最新的 50 段
    assert ids(index.search("蓝莓")) == [f"c{number}" for number in range(59, 9, -1)]
    assert ids(index.search("蓝莓", limit=3)) == ["c59", "c58", "c57"]


def test_index_follows_puts_and_removes():
    index = SearchIndex()
    assert index.search("蓝莓") == []  # 空的索引
    index.put("a", "旧标题", [said("买蓝莓")], updated_at=1.0)
    assert index.search("") == [] and index.search(" \n ") == []  # 空的搜索词

    # 同一个编号再收录一次是更新：旧的内容搜不到了，也不会变成两段
    index.put("a", "新标题", [said("买草莓")], updated_at=2.0)
    assert index.search("蓝莓") == [] and index.search("旧标题") == []
    assert ids(index.search("草莓")) == ["a"] == ids(index.search("新标题"))
    assert ids(index.search("买")) == ["a"]

    index.remove("a")
    assert index.search("草莓") == [] and index.search("新标题") == []
    index.remove("a")  # 已经不在了，再删一次也不出错
    index.remove("从来没有过")


# ---- 会话库：索引跟着文件走 -----------------------------------------------


@pytest.fixture
def store(tmp_path) -> ConversationStore:
    return ConversationStore(tmp_path / "conversations")


def keep(store: ConversationStore, title: str, *events: dict, updated_at: float | None = None) -> Conversation:
    """往 store 里存一段会话。给了 updated_at 就按这个时间存（不用真实的当前时间），方便测排序。"""
    conversation = store.new("anthropic", "claude-opus-5-5")
    conversation.title = title
    conversation.messages = [{"role": "user", "content": "（发给模型的历史，搜索不看这里）"}]
    conversation.events = list(events)
    if updated_at is None:
        store.save(conversation)
    else:
        conversation.updated_at = updated_at
        store.save(conversation, touch=False)
    return conversation


def test_saved_conversations_can_be_searched(store):
    assert store.search("蓝莓") == []  # 一段会话都没有
    first = keep(store, "周报", said("提醒我周五下午交报告"), answered("记下了"), updated_at=100.0)
    second = keep(store, "爬山", said("周五去爬山"), updated_at=200.0)

    # 每一项是会话列表里要显示的基本信息，加上命中的情况
    (found,) = store.search("交报告")
    assert found == {
        "id": first.id,
        "title": "周报",
        "provider": "anthropic",
        "model": "claude-opus-5-5",
        "updated_at": 100.0,
        "exact": True,
        "title_marks": [],
        "snippet": {"kind": "user", "text": "提醒我周五下午交报告", "marks": [[7, 10]], "find": "交报告"},
    }
    assert ids(store.search("周五")) == [second.id, first.id]  # 一样相关，新的在前
    assert ids(store.search("爬山")) == [second.id]
    assert store.search("") == [] and store.search("哪儿都没有") == []
    # 搜的是界面上显示过的内容，不是发给模型的那份历史
    assert store.search("发给模型的历史") == []

    # 还没保存的会话（只在内存里）搜不到
    draft = store.new("anthropic", "claude-opus-5-5")
    draft.title, draft.events = "草稿", [said("买蓝莓")]
    assert store.search("蓝莓") == [] and store.search("草稿") == []
    store.save(draft)
    assert ids(store.search("蓝莓")) == [draft.id]


def test_search_follows_rename_and_delete(store):
    conversation = keep(store, "旧名字", said("买蓝莓"), updated_at=100.0)
    other = keep(store, "别的", said("买草莓"), updated_at=200.0)

    store.rename(conversation.id, "  新名字  ")
    assert store.search("旧名字") == []
    (found,) = store.search("新名字")
    assert (found["id"], found["title"], found["title_marks"]) == (conversation.id, "新名字", [[0, 3]])
    assert found["updated_at"] == 100.0  # 改名不算更新，排序不受影响
    assert ids(store.search("蓝莓")) == [conversation.id]  # 标题改了，内容还在
    assert ids(store.search("买")) == [other.id, conversation.id]

    store.delete(conversation.id)
    assert store.search("蓝莓") == [] and store.search("新名字") == []
    assert ids(store.search("买")) == [other.id]


def test_deleted_conversations_do_not_crowd_out_the_rest(store):
    # 搜索最多返回 50 段。删掉的会话如果还留在索引里，就会占着名额，把还在的挤出去
    survivor = keep(store, "留下的", said("买蓝莓"), updated_at=1.0)
    doomed = [keep(store, f"第 {number} 段", said("买蓝莓"), updated_at=100.0 + number) for number in range(MAX_RESULTS)]
    assert len(store.search("蓝莓")) == MAX_RESULTS and survivor.id not in ids(store.search("蓝莓"))  # 新的在前，它排不进去
    for conversation in doomed:
        store.delete(conversation.id)
    assert ids(store.search("蓝莓")) == [survivor.id]


def test_search_survives_a_restart_and_damaged_files(store):
    first = keep(store, "周报", said("提醒我周五下午交报告"), updated_at=100.0)
    # 一个根本读不出来的文件，和一个读得出来、但里面的事件被改得乱七八糟的文件
    (store.directory / "20200101-000000-aaaaaa.json").write_text("{ 这不是完整的 JSON 蓝莓", encoding="utf-8")
    odd = {
        "provider": "anthropic",
        "model": "claude-opus-5-5",
        "title": "改坏了的",
        "updated_at": 50,
        "messages": [],
        "events": [None, 7, {"type": ["不是字符串"], "text": "蓝莓"}, {"type": "user", "text": ["蓝莓"]}, said("还读得出来的蓝莓")],
    }
    (store.directory / "20200101-000000-bbbbbb.json").write_text(json.dumps(odd, ensure_ascii=False), encoding="utf-8")

    reopened = ConversationStore(store.directory)  # 相当于重启程序：索引是照着文件重新建起来的
    (found,) = reopened.search("交报告")
    assert (found["id"], found["title"], found["updated_at"]) == (first.id, "周报", 100.0)
    # 坏文件没有妨碍启动；里面读得出来的那一句照样能搜到，读不出来的那个文件不在索引里
    (found,) = reopened.search("蓝莓")
    assert found["id"] == "20200101-000000-bbbbbb" and found["snippet"]["text"] == "还读得出来的蓝莓"
    assert (found["title"], found["updated_at"]) == ("改坏了的", 50.0)


def test_untitled_conversations_show_the_same_name_as_in_the_list(store):
    conversation = keep(store, "", said("买蓝莓"))
    (found,) = store.search("蓝莓")
    # 会话列表里没有标题的会话显示成"新对话"，搜索结果里也一样（而不是一行空白）
    assert found["id"] == conversation.id and found["title_marks"] == []
    assert found["title"] == "新对话" == store.summaries()[0]["title"]
