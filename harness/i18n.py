"""界面的多语言：中文、英文、西班牙语。

做法很朴素：界面上的每一句话都用中文写在它出现的地方，外面包一层 t()，这句中文同时就是它在翻译表里的"键"。
翻译表在 harness/locales/ 下，一种语言一个 JSON 文件，内容是 {"中文原文": "译文"}。

    t("已中断。")                                          # 英文界面下是 "Stopped."
    t("已换成 {provider} · {model}。对话继续。", provider="DeepSeek", model="deepseek-flash")

查不到译文的就照原样显示中文，所以漏翻一句不会出错，只会在那一处露出中文。tests/test_i18n.py 会把漏掉的找出来。

网页和这里用的是同一份翻译表：助手程序发出页面时把表塞进页面里（page_payload），页面自己的话由页面翻译；
助手程序发给页面的话（确认卡片上的说明、提示），除了译好的文字，还带着原文和填进去的值（Message 的 key 和 values），
这样用户在页面上换语言时，已经显示出来的这些话也能在原地换成新的语言。

模型的回答不受这里影响：它用什么语言回答，取决于用户用什么语言和它说话。
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any, Self

LANGUAGES = {"zh": "中文", "en": "English", "es": "Español"}
DEFAULT = "zh"

# 当前的界面语言。这是一个只给一个人用的本地程序，整个程序用同一种语言就够了；
# 启动时按设置文件定下来，用户在网页上换语言时改它（见 App.set_language）。
current = DEFAULT

_LOCALES = Path(__file__).parent / "locales"
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


class Message(str):
    """一句已经译好的话。它就是一个普通的字符串，另外记着原文（key）和填进去的值（values），
    需要的话可以照着它们换成别的语言再译一遍（网页上换语言时就是这样做的）。"""

    key: str
    values: dict[str, Any]

    def __new__(cls, text: str, key: str, values: dict[str, Any]) -> Self:
        message = super().__new__(cls, text)
        message.key = key
        message.values = values
        return message


@cache
def table(language: str) -> dict[str, str]:
    """一种语言的翻译表。中文是原文本身，没有表。"""
    if language == DEFAULT or language not in LANGUAGES:
        return {}
    return json.loads((_LOCALES / f"{language}.json").read_text(encoding="utf-8"))


def fill(text: str, values: dict[str, Any]) -> str:
    """把句子里的 {名字} 换成对应的值。只认这一种写法，值里的花括号原样保留，不会被再填一次。"""
    return _PLACEHOLDER.sub(lambda match: str(values[match.group(1)]) if match.group(1) in values else match.group(0), text)


def t(text: str, **values: Any) -> Message:
    """把一句中文原文译成当前的界面语言。"""
    plain = {name: value if isinstance(value, (int, float)) else str(value) for name, value in values.items()}
    return Message(fill(table(current).get(text, text), plain), text, plain)


def k(text: str) -> str:
    """只是做个记号：这是一句以后会交给 t() 的原文（比如先放进一张表里，用的时候再译）。
    测试靠这个记号找到这些句子，检查它们有没有译文。"""
    return text


def number(value: int) -> str:
    """按当前界面语言写一个整数。中文和英文是 123,456；西班牙语里逗号是小数点，千位之间用空格：123 456。"""
    text = f"{value:,}"
    return text.replace(",", "\u00a0") if current == "es" else text  # 不换行的空格：数字不会在中间断开


def message_of(exc: BaseException) -> str:
    """异常带着的那句话。用 t() 写的会原样取回（还记着原文和填进去的值），别的转成普通文字。"""
    if len(exc.args) == 1 and isinstance(exc.args[0], Message):
        return exc.args[0]
    return str(exc)


def choose(language: str) -> str:
    """换界面语言。不认识的语言按默认的来。返回实际用上的语言。"""
    global current
    current = language if language in LANGUAGES else DEFAULT
    return current


def page_payload(language: str) -> str:
    """给页面用的那一份：当前语言，加上全部翻译表。

    它会被原样放进页面里的 <script type="application/json"> 中。浏览器不会执行这种"脚本"，只是一段数据，
    但其中如果出现 "</script>"，标签就会被提前结束，所以把 < 等几个字符都写成 \\u 转义的形式。
    """
    data = {
        "language": language if language in LANGUAGES else DEFAULT,
        "tables": {code: table(code) for code in LANGUAGES if code != DEFAULT},
    }
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    for char in ("<", ">", "&", " ", " "):
        text = text.replace(char, f"\\u{ord(char):04x}")
    return text
