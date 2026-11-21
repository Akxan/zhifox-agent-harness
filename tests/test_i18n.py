"""多语言的测试：每一句界面上的话都有英文和西班牙语的译文，换语言的接口按约定工作。

界面上的话用中文写在它出现的地方，这句中文同时就是翻译表里的键（见 harness/i18n.py）。
漏翻一句不会出错，只会在那一处露出中文，所以靠这里的测试把漏掉的找出来：
新加了一句话却没有翻译，下面的 test_every_page_text_is_translated 会失败，并列出是哪几句。
"""

from __future__ import annotations

import ast
import html
import json
import re
from pathlib import Path

import pytest

from harness import i18n

PAGE = Path(i18n.__file__).parent / "webui" / "index.html"
CJK = re.compile(r"[㐀-鿿]")
PLACEHOLDER = re.compile(r"\{(\w+)\}")
# 页面上故意不翻译的几处：语言的名字（每种语言都用它自己的文字写）
UNTRANSLATED = {"中文"}


def _main_script(page: str) -> str:
    start = page.index("// 这个页面做的事情很少")
    return page[start : page.index("</script>", start)]


def _without_comments(code: str) -> str:
    """去掉 // 注释（字符串里的 // 不算），足够应付这个页面的写法。"""
    kept = []
    for line in code.splitlines():
        out, quote, index = [], None, 0
        while index < len(line):
            char = line[index]
            if quote:
                out.append(char)
                if char == "\\" and index + 1 < len(line):
                    out.append(line[index + 1])
                    index += 1
                elif char == quote:
                    quote = None
            elif char in "'\"`":
                quote = char
                out.append(char)
            elif line.startswith("//", index):
                break
            else:
                out.append(char)
            index += 1
        kept.append("".join(out))
    return "\n".join(kept)


_LITERAL = r"'((?:[^'\\\n]|\\.)*)'"


def page_keys() -> dict[str, str]:
    """页面里所有要翻译的话，以及每一句是从哪儿找到的（出错时好定位）。"""
    page = PAGE.read_text(encoding="utf-8")
    keys: dict[str, str] = {}
    # 写死在标记里的：带 data-i18n 的元素的文字，带 data-i18n-xxx 的属性，建议卡片要填进输入框的那句话
    markup = re.sub(r"<!--.*?-->", "", page[page.index("<body>") : page.index("// 这个页面做的事情很少")], flags=re.DOTALL)
    for text in re.findall(r"<[a-z0-9]+\b[^>]*\bdata-i18n\b(?!-)[^>]*>([^<]*)<", markup):
        keys.setdefault(html.unescape(text.strip()), "data-i18n")
    for tag in re.findall(r"<[a-z0-9]+\b[^>]*>", markup):
        for name in ("title", "placeholder", "aria-label"):
            if f"data-i18n-{name}" in tag:
                value = re.search(rf'\b{name}="([^"]*)"', tag)
                keys.setdefault(html.unescape(value.group(1)), f"data-i18n-{name}")
        prompt = re.search(r'\bdata-prompt="([^"]*)"', tag)
        if prompt:
            keys.setdefault(html.unescape(prompt.group(1)), "data-prompt")
    # 脚本里交给 t() 和 k() 的
    code = _without_comments(_main_script(page))
    for match in re.finditer(r"\b[tk]\(\s*" + _LITERAL, code):
        keys.setdefault(match.group(1).encode().decode("unicode_escape") if "\\u" in match.group(1) else match.group(1), "script")
    return keys


def python_keys() -> set[str]:
    """程序里所有交给 t() 或者用 k() 标出来的话（只认直接写在括号里的那一句）。"""
    keys = set()
    for file in Path(i18n.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
            first = node.args[0]
            if name in ("t", "k") and isinstance(first, ast.Constant) and isinstance(first.value, str):
                keys.add(first.value)
    return keys


@pytest.mark.parametrize("language", [code for code in i18n.LANGUAGES if code != i18n.DEFAULT])
def test_every_python_text_is_translated(language):
    table = i18n.table(language)
    missing = sorted(key for key in python_keys() if CJK.search(key) and key not in table)
    assert missing == [], f"{i18n.LANGUAGES[language]} 的翻译表里缺这些话（程序里的）：\n" + "\n".join(missing)


def test_every_chinese_text_in_the_script_goes_through_t_or_k():
    # 脚本里写死的中文，要么交给 t() 当场翻译，要么用 k() 标出来、之后再交给 t()。直接写的就翻译不到了
    code = _without_comments(_main_script(PAGE.read_text(encoding="utf-8")))
    wrapped = {match.span(1) for match in re.finditer(r"\b[tk]\(\s*" + _LITERAL, code)}
    stray = [
        match.group(1)
        for match in re.finditer(_LITERAL, code)
        if CJK.search(match.group(1)) and match.span(1) not in wrapped and match.group(1) not in UNTRANSLATED
    ]
    assert stray == [], "这些中文没有交给 t() 或 k()，换了语言也不会变：\n" + "\n".join(stray)


def test_elements_marked_for_translation_hold_only_text():
    # 换语言时，带 data-i18n 的元素整个换成译文（textContent）。它里面要是还有别的元素（图标之类），就会被冲掉
    page = PAGE.read_text(encoding="utf-8")
    markup = re.sub(r"<!--.*?-->", "", page[page.index("<body>") : page.index("// 这个页面做的事情很少")], flags=re.DOTALL)
    for match in re.finditer(r"<([a-z0-9]+)\b[^>]*\bdata-i18n\b(?!-)[^>]*>", markup):
        rest = markup[match.end() :]
        assert rest.index(f"</{match.group(1)}>") <= rest.index("<"), f"{match.group(0)} 里面还有别的元素"


@pytest.mark.parametrize("language", [code for code in i18n.LANGUAGES if code != i18n.DEFAULT])
def test_every_page_text_is_translated(language):
    table = i18n.table(language)
    missing = [key for key in page_keys() if key not in table and CJK.search(key) and key not in UNTRANSLATED]
    assert missing == [], f"{i18n.LANGUAGES[language]} 的翻译表里缺这些话：\n" + "\n".join(missing)


@pytest.mark.parametrize("language", [code for code in i18n.LANGUAGES if code != i18n.DEFAULT])
def test_translations_keep_their_placeholders_and_have_no_chinese(language):
    for key, value in i18n.table(language).items():
        assert isinstance(value, str) and value.strip(), key
        # 句子里会变的部分（{n}、{provider}……）一个都不能少，也不能多
        assert sorted(PLACEHOLDER.findall(value)) == sorted(PLACEHOLDER.findall(key)), (key, value)
        # 品牌名"智狐"保留原样，别的地方不该再有中文
        assert not CJK.search(value.replace("智狐", "")), (key, value)
        # 原文开头结尾的换行和空格（排版用的）要保留
        assert (key.startswith("\n"), key.endswith("\n")) == (value.startswith("\n"), value.endswith("\n")), (key, value)


@pytest.mark.parametrize("language", [code for code in i18n.LANGUAGES if code != i18n.DEFAULT])
def test_translation_tables_have_no_leftovers(language):
    # 翻译表里不该留着已经没人用的话：原文改了、旧的那句就成了死条目，以后谁也不会去更新它
    used = set(page_keys()) | python_keys()
    stale = sorted(set(i18n.table(language)) - used)
    assert stale == [], f"{i18n.LANGUAGES[language]} 的翻译表里有已经没人用的话：\n" + "\n".join(stale)


def test_t_translates_fills_in_values_and_falls_back_to_chinese(monkeypatch):
    monkeypatch.setattr(i18n, "current", "en")
    message = i18n.t("已中断。")
    assert message == i18n.table("en")["已中断。"] and message.key == "已中断。" and message.values == {}
    message = i18n.t("已换成 {provider} · {model}。对话继续。", provider="DeepSeek", model="deepseek-flash")
    assert "DeepSeek" in message and "deepseek-flash" in message and "{" not in message
    assert message.values == {"provider": "DeepSeek", "model": "deepseek-flash"}
    # 查不到的照原样用中文，句子里会变的部分照样填进去
    assert i18n.t("一句表里没有的话：{x}", x=1) == "一句表里没有的话：1"
    # 中文就是原文本身
    monkeypatch.setattr(i18n, "current", "zh")
    assert i18n.t("已中断。") == "已中断。"
    # 值里有花括号也不会被当成要填的空
    assert i18n.t("找到 {n} 段会话", n="{evil}") == "找到 {evil} 段会话"


def test_a_message_inside_a_message_keeps_its_own_original(monkeypatch):
    """"在电脑上点击鼠标（{what}）"里的 what 本身是 t("左键单击")。发给页面时它要带着自己的原文，
    页面换语言时才能连里面那一句一起换掉；只带着译好的文字的话，换成英文以后卡片上还留着"左键单击"。"""
    import copy

    from harness.webui.ui import said

    monkeypatch.setattr(i18n, "current", "en")
    inner = i18n.t("左键单击")
    outer = i18n.t("在电脑上点击鼠标（{what}）：{where}{on}{held}", what=inner, where="(1, 2)", on="", held="")
    assert outer.values["what"] == str(inner) and outer.nested == {"what": inner}  # 程序里照着 values 重填，还是这一句
    fields = said(outer)
    assert fields["values"]["what"] == {"key": "左键单击", "values": {}}  # 页面拿到的是原文，按当前语言重译
    assert fields["values"]["where"] == "(1, 2)"
    again = copy.deepcopy(outer)  # 存进文件、复制的时候 nested 也不丢
    assert again.nested["what"].key == "左键单击"


def test_a_space_beside_a_chinese_platform_name_is_dropped():
    """中文的句子给 {platform} 两边留了空格，是为 Telegram 这样的英文名字留的；填进去的是"飞书"时不要那个空格。
    只管 {platform}：别的值（文件名、机器人的名字）两边的空格留着；句子本身的空格、英文句子里的空格都不动。
    页面里的 t() 照着同一条规矩（见 index.html）。"""
    assert i18n.fill("连接 {platform}", {"platform": "飞书"}) == "连接飞书"
    assert i18n.fill("连接 {platform}", {"platform": "Telegram"}) == "连接 Telegram"
    assert i18n.fill("先在 {platform} 里建一个机器人", {"platform": "钉钉"}) == "先在钉钉里建一个机器人"
    assert i18n.fill("先在 {platform} 里建一个机器人", {"platform": "QQ"}) == "先在 QQ 里建一个机器人"
    assert i18n.fill("Connect {platform} now", {"platform": "飞书"}) == "Connect 飞书 now"
    assert i18n.fill("已连接 {bot}。", {"bot": "智狐测试"}) == "已连接 智狐测试。"  # 机器人的名字：空格留着
    assert i18n.fill("已经有一个叫 {name} 的文件了", {"name": "别的.md"}) == "已经有一个叫 别的.md 的文件了"
    assert i18n.fill("还有 {missing} 好", {}) == "还有 {missing} 好"  # 没给值的照原样留着


def test_numbers_follow_the_interface_language(monkeypatch):
    # 西班牙语里逗号是小数点："123,456" 会被读成一百二十三点几
    for language, expected in (("zh", "123,456"), ("en", "123,456"), ("es", "123\u00a0456")):
        monkeypatch.setattr(i18n, "current", language)
        assert i18n.number(123456) == expected
    assert i18n.number(999) == "999"


def test_page_payload_is_safe_to_embed():
    payload = i18n.page_payload("es")
    data = json.loads(payload)
    assert data["language"] == "es" and set(data["tables"]) == {"en", "es"}
    # 放进 <script type="application/json"> 里，不能有任何东西能提前结束这个标签
    assert "<" not in payload and " " not in payload
    assert json.loads(i18n.page_payload("xx"))["language"] == i18n.DEFAULT  # 不认识的语言按默认的来
