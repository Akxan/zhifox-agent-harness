"""会话搜索里的拼音（harness/search_pinyin.py 和 search.py 里的后两档）：全拼、首字母、多音字、排序、
标出来的位置、什么时候不按拼音猜、拼音表什么时候才读、够不够快；还有生成拼音表的 tests/make_pinyin.py 怎么读 Unihan。

别的几档（原样、相近的词、隔字）在 test_search.py 里测。这里不下载任何东西：make_pinyin.py 的解析用一小段照着
Unihan_Readings.txt 的格式写的样本来测。
"""

from __future__ import annotations

import json
import random
import re
import subprocess
import sys
import time
from pathlib import Path

import make_pinyin
import pytest

from harness import search_pinyin
from harness.conversations import ConversationStore
from harness.search import SearchIndex

ROOT = Path(__file__).resolve().parent.parent

# ---- 搭积木用的小函数（和 test_search.py 里的一样） -------------------------


def said(text: str) -> dict:
    return {"type": "user", "text": text}


def answered(text: str) -> dict:
    return {"type": "text", "text": text}


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
    """照浏览器的办法（UTF-16）从文字里切出标出来的每一段。"""
    raw = text.encode("utf-16-le")
    return [raw[first * 2 : last * 2].decode("utf-16-le") for first, last in marks]


def snippet_marks(found: dict) -> list[str]:
    return marked(found["snippet"]["text"], found["snippet"]["marks"])


# ---- 全拼 -----------------------------------------------------------------


@pytest.mark.parametrize("query", ["baogao", "bao gao", "BaoGao", "BAO  GAO", "bao'gao", "bao’gao", "ｂａｏｇａｏ"])
def test_full_pinyin_finds_the_chinese_word(query):
    # 前面有表情（BMP 之外，浏览器里占两格）：标出来的位置要按 UTF-16 数，标的正好是"报告"两个字
    index = index_of(a=[said("😀🦊 周五交报告")], b=[said("周五去爬山")])
    (found,) = index.search(query)
    assert found["id"] == "a" and found["exact"] is False  # 按拼音找到的不算"原样找到"，页面上排在"相近的结果"里
    assert found["snippet"] == {"kind": "user", "text": "😀🦊 周五交报告", "marks": [[8, 10]], "find": "报告"}
    assert snippet_marks(found) == ["报告"]


def test_rare_characters_beyond_the_bmp_are_matched_and_counted_right():
    # 𠳐（U+20CD0，bāng）在扩展 B 里：有这种字的会话改用认得它的那个正则（Document.rare），
    # 它自己能按拼音对上，排在它后面的字标的位置也照样对（它在浏览器里占两格）
    index = index_of(a=[said("这是𠳐报，交报告")], b=[said("交报告")])
    assert index._documents["a"].rare and not index._documents["b"].rare
    (found,) = index.search("bangbao")
    assert found["snippet"]["marks"] == [[2, 5]] and snippet_marks(found) == ["𠳐报"]
    a, b = index.search("baogao")
    assert (a["id"], b["id"]) == ("b", "a")  # 一样相关，新的在前
    assert b["snippet"]["marks"] == [[7, 9]] and snippet_marks(b) == ["报告"]


def test_titles_and_every_occurrence_are_marked():
    index = index_of(a=("报告模板", [said("先写报告，再交报告")]))
    (found,) = index.search("baogao")
    assert marked(found["title"], found["title_marks"]) == ["报告"]
    assert snippet_marks(found) == ["报告", "报告"]  # 内容里对上的每一处都标出来，和原样找到时一样
    assert found["snippet"]["find"] == "报告"  # 页面打开会话以后靠它找到那个位置


def test_every_reading_of_a_polyphonic_character_counts():
    index = index_of(
        bank=[said("去银行取钱")],
        walk=[said("行走的力量")],
        grow=[said("孩子长大了")],
        wall=[said("去长城玩")],
        again=[said("重新开始")],
        weight=[said("这很重要")],
    )
    assert ids(index.search("yinhang")) == ["bank"]  # 行 读 hang
    assert ids(index.search("xingzou")) == ["walk"]  # 行 读 xing
    assert ids(index.search("zhangda")) == ["grow"]
    assert ids(index.search("changcheng")) == ["wall"]
    assert ids(index.search("chongxin")) == ["again"]
    assert ids(index.search("zhongyao")) == ["weight"]
    # 行读哪个音都算，所以 yinxing 也对得上"银行"（宁可多找到，也别漏掉）
    assert ids(index.search("yinxing")) == ["bank"]


def test_one_syllable_or_an_unfinished_one_is_not_enough():
    index = index_of(a=[said("报告写完了，半个小时，办公室")], b=[said("天鹅和企鹅，里额")])
    # 一个音节对得上的字太多，不按拼音找
    assert index.search("bao") == [] and index.search("gao") == []
    # bang 是一个音节：既不对"帮"一个字，也不拆成 ban + g… 去对"半个""办公"
    assert index.search("bang") == []
    # 只认完整的音节：敲到一半的 baog 对不上"报告"
    assert index.search("baog") == []
    # 单独一个元音的音节（a、o、e）要写隔音符号才接得上：lie 不会拆成 li + e 去对"里额"，tiane 也不是"天鹅"
    assert index.search("lie") == [] and index.search("tiane") == []
    assert ids(index.search("tian'e")) == ["b"] and ids(index.search("qi'e")) == ["b"]


def test_separators_decide_where_syllables_end():
    index = index_of(first=[said("先去吃饭")], city=[said("在西安待了三天")])
    # xian 一个音节是"先"（只有一个字，不算），两个音节是"西安"
    assert ids(index.search("xian")) == ["city"]
    assert ids(index.search("xi'an")) == ["city"] == ids(index.search("xi an"))
    # 有好几种拆法时每一种都试：fangan 是 fang'an（方案），也是 fan'gan（反感）
    index = index_of(plan=[said("这个方案不错")], dislike=[said("有点反感")])
    assert set(ids(index.search("fangan"))) == {"plan", "dislike"}
    assert ids(index.search("fang'an")) == ["plan"] and ids(index.search("fan'gan")) == ["dislike"]


def test_long_or_hopelessly_ambiguous_letters_are_not_treated_as_pinyin():
    assert search_pinyin.full("zhonghuarenmingongheguo") is not None
    assert search_pinyin.full("a" * 3) is None  # 拆不成两个以上的音节
    assert search_pinyin.full("xi" * (search_pinyin.PINYIN_MAX // 2 + 1)) is None  # 太长
    # 每一段都有两种拆法：拆法的个数翻着倍地涨，超过 MAX_SPLITS 就不当拼音了
    assert search_pinyin.full("xian" * 4) is not None  # 2⁴ = 16 种
    assert search_pinyin.full("xian" * 5) is None  # 32 种
    assert not search_pinyin.spellable("bao2gao") and not search_pinyin.spellable("报告") and not search_pinyin.spellable("'bao")


# ---- 首字母 ---------------------------------------------------------------


def test_initials_need_three_letters_and_look_only_at_titles():
    index = index_of(
        template=("周报模板", [said("每周五用")]),
        country=("中国人", []),
        report=("报告", [said("这个人说中国人都爱写周报模板")]),
    )
    (found,) = index.search("zbmb")
    assert found["id"] == "template" and found["exact"] is False
    assert marked(found["title"], found["title_marks"]) == ["周报模板"]
    assert found["snippet"] is None  # 只有标题对得上
    # 正文里的"这个人""中国人""周报模板"不算：正文又长又多，首字母处处都对得上
    assert ids(index.search("zgr")) == ["country"]
    # 两个字母的首字母不算：bg 能对上"不过""不管""宾馆"……
    assert index.search("bg") == []
    # zh、ch、sh 只算第一个字母
    assert index.search("zhgr") == []
    assert search_pinyin.initials("b" * (search_pinyin.MAX_INITIALS + 1)) is None


def test_initials_count_rare_characters_too():
    index = index_of(a=("𠳐报告", []))
    (found,) = index.search("bbg")
    assert found["title_marks"] == [[0, 4]] and marked(found["title"], found["title_marks"]) == ["𠳐报告"]


# ---- 什么时候不按拼音猜、排在哪里 ------------------------------------------


def test_a_word_found_as_typed_is_not_read_as_pinyin():
    # "name" 拆得成 na + me（那么）。有会话里原样写着 name，就是在找这个英文词：不再把每段里的"那么"找出来
    english = [said("rename the file, change the name")]
    chinese = [said("那么就这样吧")]
    index = index_of(en=english, zh=chinese)
    assert [(item["id"], item["exact"]) for item in index.search("name")] == [("en", True)]
    # 哪儿都没有原样的 name 时，才按拼音对
    assert [(item["id"], item["exact"]) for item in index_of(zh=chinese).search("name")] == [("zh", False)]
    # 几个关键词连起来当拼音也一样：有会话把 bao 和 gao 都原样找到了，就不再去猜"报告"
    index = index_of(words=[said("bao and gao are names")], report=[said("交报告")])
    assert ids(index.search("bao gao")) == ["words"]
    assert ids(index_of(report=[said("交报告")]).search("bao gao")) == ["report"]


def test_pinyin_ranks_below_similar_words_and_loose_matches():
    # 相近的词（beiging 敲错了一个字母）排在按拼音对上的"北京"前面
    index = index_of(typo=[said("the beiging error")], city=[said("去北京出差")])
    assert ids(index.search("beijing")) == ["typo", "city"]
    # 隔字包含（diagonal 里隔着字有 d、i、a、n）也排在按拼音对上的"地安门"（di + an）前面，哪怕拼音那个更新
    index = index_of(loose=[said("a diagonal line")], gate=[said("地安门")])
    assert ids(index.search("dian")) == ["loose", "gate"]
    # 首字母排在全拼后面：nana 是"娜娜"的全拼，也是"南安牛奥"的首字母
    index = index_of(full=[said("娜娜来了")], initials=("南安牛奥", []))
    assert ids(index.search("nana")) == ["full", "initials"]
    # 同一段会话里两样都对得上时，用严格的那一档
    (found,) = index_of(both=("娜娜", [])).search("nana")
    assert marked(found["title"], found["title_marks"]) == ["娜娜"]


QUERIES = ["报告", "周报 report", "report", "harness", "hraness", "hrns", "name", "the", "log", "dian", "bao gao", "周五 baogao", "xi an"]


@pytest.mark.parametrize("query", QUERIES)
def test_pinyin_only_adds_results_after_the_ones_found_before(query, monkeypatch):
    # 拼音的两档排在原来三档的后面：原来找得到的会话一个不少、先后不变，按拼音找到的只接在后面
    index = index_of(
        en=[said("the name of the report, and harness logs")],
        typo=[said("hraness started")],
        loose=[said("a diagonal line")],
        mixed=("周报 report", [said("周五交报告")]),
        zh=[said("那么，地安门在西安吗？报告写完了，周五交")],
        words=[answered("bao and gao")],
    )
    with_pinyin = index.search(query)
    monkeypatch.setattr(search_pinyin, "full", lambda term: None)
    monkeypatch.setattr(search_pinyin, "initials", lambda term: None)
    before = index.search(query)
    assert with_pinyin[: len(before)] == before
    assert all(item["exact"] is False for item in with_pinyin[len(before) :])


def test_mixed_and_english_queries_work_as_before():
    index = index_of(
        mixed=("周报", [said("this week's report is done")]),
        chinese=[said("热破了，周报写好了")],  # 热破是 re + po，可 report 还剩下 rt，拆不成完整的音节
        english=[said("I need to take the line nine")],
        pinyin=[said("在里呢，他可以，你呢")],  # line 拆得成 li + ne（里呢），take 是 ta + ke（他可），nine 是 ni + ne（你呢）
    )
    assert ids(index.search("周报 report")) == ["mixed"]
    assert ids(index.search("report")) == ["mixed"]
    # 英文词原样找得到时，不再拆成拼音去对汉字
    for word in ("line", "take", "nine"):
        assert [(item["id"], item["exact"]) for item in index.search(word)] == [("english", True)], word


def test_the_pinyin_of_a_whole_phrase_needs_every_term_to_be_letters():
    index = index_of(a=[said("交报告")])
    assert ids(index.search("bao gao")) == ["a"]
    assert index.search("bao 2gao") == [] and index.search("bao 告") == []
    # 拼音连起来的一串和分开的几个关键词都试：分开的每个都要找到（不要求挨着），连起来的要挨着
    index = index_of(apart=[said("报告写在周报里")], together=[said("报告周报")])
    assert set(ids(index.search("baogao zhoubao"))) == {"apart", "together"}


def test_saved_conversations_are_found_by_pinyin(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    conversation = store.new("anthropic", "claude-opus-5-5")
    conversation.title = "周报模板"
    conversation.events = [said("提醒我周五交报告")]
    store.save(conversation)
    (found,) = store.search("baogao")
    assert found["id"] == conversation.id and found["exact"] is False
    assert found["snippet"] == {"kind": "user", "text": "提醒我周五交报告", "marks": [[6, 8]], "find": "报告"}
    assert [item["id"] for item in store.search("zbmb")] == [conversation.id]


# ---- 拼音表：什么时候读、读出来是什么 ---------------------------------------

LAZY_SCRIPT = """
import json
from harness import search_pinyin
from harness.search import SearchIndex

index = SearchIndex()
index.put("a", "周报", [{"type": "user", "text": "提醒我周五交报告，harness 的 report"}], 0.0)
report = {"after_put": search_pinyin.loaded()}
for query in ("报告", "交报", "harness", "report", "周报 report", "HARNESS"):
    index.search(query)
report["after_plain"] = search_pinyin.loaded()
report["pinyin"] = [item["id"] for item in index.search("baogao")]
report["after_pinyin"] = search_pinyin.loaded()
print(json.dumps(report))
"""


def test_the_table_is_read_only_when_a_search_needs_pinyin():
    # 在另一个进程里跑：这个进程里别的测试早就把表读进来了
    done = subprocess.run(
        [sys.executable, "-c", LAZY_SCRIPT], check=False, cwd=ROOT, capture_output=True, text=True, timeout=60, encoding="utf-8"
    )
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    # 收录会话、按中文搜、按原样找得到的英文搜，都不读拼音表
    assert report == {"after_put": False, "after_plain": False, "pinyin": ["a"], "after_pinyin": True}


def test_the_table_in_the_package_is_the_generated_one():
    text = search_pinyin.TABLE.read_text(encoding="utf-8")
    # 开头写着来源、版本、SHA-256 和 Unicode 的许可声明，和生成它的脚本里钉死的一致
    header = [line for line in text.splitlines() if line.startswith("#")]
    assert f"# Source: Unicode Han Database (Unihan), Unicode {make_pinyin.VERSION}, Unihan_Readings.txt" in header
    assert f"#         SHA-256 {make_pinyin.SHA256}" in header
    assert "# UNICODE LICENSE V3" in header and "# COPYRIGHT AND PERMISSION NOTICE" in header
    table = search_pinyin._table()
    assert len(table) > 400 and sum(map(len, table.values())) > 40000
    # 每一行都读得进来（读的时候会跳过格式不对的行：这里要求一行都没跳过）
    assert len(table) == len([line for line in text.splitlines() if line and not line.startswith("#")])
    assert all(re.fullmatch(r"[a-z]+", syllable) and re.search("[aeiouv]", syllable) for syllable in table)
    for char, readings in {"行": {"xing", "hang", "heng"}, "报": {"bao"}, "绿": {"lv", "lu"}, "女": {"nv"}, "长": {"chang", "zhang"}}.items():
        assert {syllable for syllable, chars in table.items() if char in chars} == readings, char
    # 兼容汉字（U+F900 起）不在表里：搜索时它们已经统一成了对应的汉字
    assert not any("\uf900" <= char <= "\ufaff" for chars in table.values() for char in chars)


def test_a_broken_or_missing_table_turns_pinyin_off_instead_of_failing(tmp_path, monkeypatch):
    for content in (None, "# 只有说明\nbao\t报告\nBAD\t报\nbad\tabc\n\n"):
        path = tmp_path / "table.txt"
        if content is not None:
            path.write_text(content, encoding="utf-8")
        monkeypatch.setattr(search_pinyin, "TABLE", path if content is not None else tmp_path / "missing.txt")
        for cached in (search_pinyin._table, search_pinyin._starts, search_pinyin._syllable, search_pinyin.full, search_pinyin.initials):
            cached.cache_clear()
        try:
            table = search_pinyin._table()
            assert table == ({} if content is None else {"bao": "报告"})  # 坏的行跳过
            assert index_of(a=[said("交报告")]).search("baogao") == []
        finally:
            for cached in (search_pinyin._table, search_pinyin._starts, search_pinyin._syllable, search_pinyin.full, search_pinyin.initials):
                cached.cache_clear()


# ---- 生成拼音表的脚本 ------------------------------------------------------

# 照着 Unihan_Readings.txt 的格式写的一小段（字段的写法见 https://www.unicode.org/reports/tr38/ ）
SAMPLE_LINES = [
    "# Unihan_Readings.txt",
    "# Unicode Version 18.0.0",
    "U+884C\tkDefinition\tgo, walk; move, travel",
    "U+884C\tkHanyuPinlu\txíng(2943) háng(218)",
    "U+884C\tkHanyuPinyin\t20811.060:háng,xìng,xíng,hàng,héng",
    "U+884C\tkMandarin\txíng",
    "U+884C\tkTGHZ2013\t131.140:háng 136.100:héng 408.120:xíng",
    "U+884C\tkXHC1983\t0442.080:háng 0443.050:hàng 0460.010:héng 1290.030:xíng",
    "U+544A\tkHanyuPinyin\t10589.120:gào,jū,gù",  # 《汉语大字典》的古音不收
    "U+544A\tkMandarin\tgào",
    "U+4E86\tkHanyuPinlu\tle(30101) liǎo(654) liào(19)",
    "U+4E86\tkMandarin\tle",
    "U+4E86\tkTGHZ2013\t212.080:le 223.010,223.020:liǎo",  # 两个位置用逗号隔开
    "U+7EFF\tkMandarin\tlǜ",  # ü 写成 v
    "U+55EF\tkMandarin\tǹg",  # 没有元音的叹词读音不收；这个字就没有别的读音了
    "U+55EF\tkXHC1983\t0950.010:ń 0950.020:ň 0950.030:ǹ",
    "U+6B38\tkMandarin\t\u00ea\u0304",  # ê̄（ê 加一个声调符号）去掉附加符号是 e
    "U+9F98\tkXHC1983\t1188.010:tà",  # 没有 kMandarin 的字不收
    "U+F900\tkMandarin\tgài",  # 兼容汉字不收
    "U+20CD0\tkMandarin\tbāng",  # 扩展 B 的字照收
    "U+0041\tkMandarin\ta",  # 不是汉字的码位
    "U+4E00\tkMandarin\tYī",  # 写法不对（大写）的读音跳过
    "U+4E01\tkMandarin\tdīng extra-garbage!",  # 写法不对的那一个跳过，别的照收
    "this line is not in the format",
    "U+4E02 kMandarin missing tabs",
]
SAMPLE = "\n".join(SAMPLE_LINES)


def test_the_generator_reads_unihan_the_documented_way():
    assert make_pinyin.parse(SAMPLE) == {
        "bang": "\U00020cd0",
        "ding": "丁",
        "e": "欸",
        "gao": "告",
        "hang": "行",
        "heng": "行",
        "le": "了",
        "liao": "了",
        "lv": "绿",
        "xing": "行",
    }
    assert make_pinyin.toneless("lǜ") == "lv" and make_pinyin.toneless("Zhuāng") == "Zhuang" and make_pinyin.toneless("\u00ea\u0304") == "e"


def test_the_generator_is_reproducible_and_carries_the_license():
    table = make_pinyin.parse(SAMPLE)
    shuffled = list(SAMPLE_LINES)
    random.Random(4).shuffle(shuffled)
    text = make_pinyin.render(table)
    # 同样的输入，行的先后不同也得到一字不差的输出
    assert make_pinyin.render(make_pinyin.parse("\n".join(shuffled))) == text
    lines = text.splitlines()
    data = [line for line in lines if not line.startswith("#")]
    assert data == ["bang\t\U00020cd0", "ding\t丁", "e\t欸", "gao\t告", "hang\t行", "heng\t行", "le\t了", "liao\t了", "lv\t绿", "xing\t行"]
    # 开头带着来源和许可声明的全文（Unicode License v3 要求随数据一起带上）
    assert all(f"# {line}".rstrip() in lines for line in make_pinyin.NOTICE.splitlines())
    assert f"#         {make_pinyin.URL}" in lines
    # 写到的正是程序读的那个文件
    assert search_pinyin.TABLE.resolve() == make_pinyin.OUTPUT.resolve()


# ---- 够不够快 --------------------------------------------------------------


def test_pinyin_search_is_about_as_fast_as_the_other_tiers():
    # 2000 段、每段 3 KB 上下的会话。不给每段预先算拼音，按拼音找就是用编好的正则把内容扫一遍，
    # 和"隔字包含"一样是现找。CI 的机器快慢不一，所以不定死几毫秒，而是和同一批会话上的"隔字包含"比：
    # 各跑几次取最快的一次，拼音不应该比它慢几倍（实测只有它的一半左右）
    rng = random.Random(11)
    pool = "的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面而方后多定行学法所交报告周五写完"
    words = ["the", "harness", "function", "return", "value", "search", "index", "python", "server", "request"]

    def text(size: int) -> str:
        parts: list[str] = []
        while sum(map(len, parts)) < size:
            if rng.random() < 0.7:
                parts.append("".join(rng.choice(pool) for _ in range(rng.randint(5, 30))))
            else:
                parts.append(" ".join(rng.choice(words) for _ in range(rng.randint(3, 10))))
        return "，".join(parts)

    index = SearchIndex()
    for number in range(2000):
        index.put(f"c{number}", f"会话 {number}", [said(text(300)), answered(text(2000)), said(text(700))], float(number))

    def fastest(query: str) -> float:
        best = float("inf")
        for _ in range(3):
            start = time.perf_counter()
            index.search(query)
            best = min(best, time.perf_counter() - start)
        return best

    index.search("baogao")  # 第一次要读拼音表、编正则，不算在里面
    loose, pinyin, phrase = fastest("交了报告"), fastest("baogao"), fastest("bao gao")
    assert index.search("baogao") and index.search("交了报告")  # 两种都真的对上了东西，而不是早早就排除了
    assert pinyin < 3 * loose + 0.05, (pinyin, loose)
    assert phrase < 3 * loose + 0.05, (phrase, loose)
    assert pinyin < 3.0  # 再慢的机器，2000 段会话按拼音搜一次也不该要好几秒
