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

写给模型看的话（工具的结果、出错的原因）保持中文，可同一句话也显示在页面的工具卡片上、终端里。这样的话用 zh() 写：

    raise ToolError(zh("没有编号为 {id} 的待办。先用 todo_list 查看现有的待办。", id=id))

它交出去的就是中文原文（填好了值），模型、历史里存的和直接写一句中文一个字节都不差；同时它和 t() 给的一样记着原文和值，
界面要显示时用 retell() 照着原文译成界面的语言（见 agent.py 的 _deliver、webui/ui.py 的 tool_result）。
填进去的值（文件名、命令的输出……）是原样的资料，从来不查翻译表：只有写在 zh() 括号里的那一句是"我们的话"，
所以命令输出里恰好有一句和翻译表里一样的中文，也不会被译掉。
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
    需要的话可以照着它们换成别的语言再译一遍（网页上换语言时就是这样做的）。

    填进去的值本身也可能是一句 t() 译好的话（"在电脑上点击鼠标（{what}）"里的 what 是 t("左键单击")）。
    values 里放的是它译好的文字（程序里照着 values 重填一遍，得到的还是这一句）；它的原文另外记在 nested 里，
    发给页面时一起带上（见 webui/ui.py 的 said），页面换语言时连里面那一句也换掉，不会只换了外面一层。"""

    key: str
    values: dict[str, Any]
    nested: dict[str, Message]

    def __new__(cls, text: str, key: str, values: dict[str, Any], nested: dict[str, Message] | None = None) -> Self:
        message = super().__new__(cls, text)
        message.key = key
        message.values = values
        message.nested = nested or {}
        return message

    # copy.deepcopy 和 pickle 重建它时要给 __new__ 的参数。不写的话只给文字，少了 key 和 values，会出错
    # （一个用 t() 生成的名字存进 channels.json 时就是这样：store 读出来要 deepcopy）。
    # str 的这个方法只交回一样（文字），这里要多交回三样：mypy 认为子类不该改它的返回类型，所以 ignore
    def __getnewargs__(self) -> tuple[str, str, dict[str, Any], dict[str, Message]]:  # type: ignore[override]  # 见上面
        return str(self), self.key, self.values, self.nested


@cache
def table(language: str) -> dict[str, str]:
    """一种语言的翻译表。中文是原文本身，没有表。"""
    if language == DEFAULT or language not in LANGUAGES:
        return {}
    return json.loads((_LOCALES / f"{language}.json").read_text(encoding="utf-8"))


# 平台的名字（{platform}）旁边的一个空格，两边都是汉字时去掉。中文的句子给 {platform} 两边留了空格，是为 Telegram 这样的
# 英文名字留的（"连接 Telegram"）；填进去的是"飞书"时，"连接 飞书"就多了一个空格。只管 {platform}：别的值（文件名、
# 机器人的名字）两边的空格正好把它和句子隔开，留着。句子本身的空格也不动
_CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_FILL = re.compile(rf"(?:(?<=[{_CJK}]) (?=\{{platform\}}))?\{{(\w+)\}}(?:(?<=\{{platform\}}) (?=[{_CJK}]))?")
_CJK_AT_START = re.compile(rf"^[{_CJK}]")
_CJK_AT_END = re.compile(rf"[{_CJK}]$")


def fill(text: str, values: dict[str, Any]) -> str:
    """把句子里的 {名字} 换成对应的值。只认这一种写法，值里的花括号原样保留，不会被再填一次。"""

    def one(match: re.Match) -> str:
        whole, name = match.group(0), match.group(1)
        if name not in values:
            return whole
        value = str(values[name])
        before = " " if whole.startswith(" ") and not _CJK_AT_START.search(value) else ""
        after = " " if whole.endswith(" ") and not _CJK_AT_END.search(value) else ""
        return before + value + after

    return _FILL.sub(one, text)


def t(text: str, /, **values: Any) -> Message:
    """把一句中文原文译成当前的界面语言。text 只能按位置给（/）：句子里可以有叫 {text} 的值（retell 重译工具结果时就会遇到）。"""
    plain = {name: value if isinstance(value, (int, float)) else str(value) for name, value in values.items()}
    nested = {name: value for name, value in values.items() if isinstance(value, Message)}
    return Message(fill(table(current).get(text, text), plain), text, plain, nested)


def zh(text: str, /, **values: Any) -> Message:
    """一句写给模型看、也会显示给人看的话（工具交回的结果、出错的原因，见文件开头）：交回的 Message 就是填好了值的中文原文，
    另外和 t() 给的一样记着原文（key）和值（values），界面上用 retell() 译成界面的语言。

    值本身也是一句 zh() 或 t() 的话时，填进去的是它的中文原文，它的原文记在 nested 里，界面上连它一起重译。
    text 只能按位置给（/）：句子里可以有叫 {text} 的值。"""
    nested = {name: _in_chinese(value) for name, value in values.items() if isinstance(value, Message)}
    plain = {name: str(nested[name]) if name in nested else value if isinstance(value, (int, float)) else str(value) for name, value in values.items()}
    return Message(fill(text, plain), text, plain, nested)


def chinese(text: object) -> str:
    """一句话的中文原文。t() 给的 Message（已经按界面语言译好了）换回中文，里面填进去的那几句也一样；交回的仍然记着原文，
    和 zh() 给的一样，界面上照样能译。普通的文字原样交回。手里是一句译好的话、却要交给模型时用它（比如 TaskError 的原因）。"""
    return _in_chinese(text) if isinstance(text, Message) else str(text)


def _in_chinese(message: Message) -> Message:
    return zh(message.key, **{name: message.nested.get(name, value) for name, value in message.values.items()})


def joined(separator: str, *parts: str) -> str:
    """几段话用 separator 接起来（空的不算）。都是普通的文字就是普通的 join；有一段是 zh() 或 t() 写的话，就接成一句 zh()：
    模型拿到的和直接接起来一字不差，每一段的原文都还在，界面上照样能译（用 + 或 join 接成普通字符串的话，原文就丢了）。
    接成的那句话（"{first}…{rest}"）里没有我们写的字，翻译表里不用有它：页面和 retell 查不到译文时照原样填。"""
    kept = [part for part in parts if part]
    if not any(isinstance(part, Message) for part in kept):
        return separator.join(kept)
    text = kept[-1]
    for part in reversed(kept[:-1]):
        text = zh("{first}" + separator + "{rest}", first=part, rest=text)
    return text


def retell(text: object) -> str:
    """一句话按现在的界面语言再写一遍（用户可能刚换了语言，或者这句话是 zh() 给的中文原文）。
    t() 和 zh() 给的 Message 照着原文重译，里面填进去的那几句也一样；普通的文字原样交回（资料从来不翻译），None 是空字符串。"""
    if isinstance(text, Message):
        return t(text.key, **{name: retell(text.nested[name]) if name in text.nested else value for name, value in text.values.items()})
    return "" if text is None else str(text)


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
