"""配对：怎么认出"这是主人"。

机器人的名字谁都能搜到，谁都能给它发消息。可它能在你的电脑上执行命令，所以只能听你一个人的。
做法和蓝牙耳机配对差不多：

  1. 你在网页上（或者命令行里）点"配对"，智狐生成一个 6 位数字的配对码，十分钟内有效；
  2. 你用自己的 Telegram 账号把这个数字发给机器人；
  3. 数字对上了，发消息的这个账号就记成"主人"（只记账号的编号，不记别的），配对码随即作废。

之后只有主人发来的消息会被处理。别人发来的：
  - 不在配对的那十分钟里：一个字都不回。机器人看起来就像没开着，不透露它背后是什么、在谁的电脑上。
  - 在配对的那十分钟里：发来的是 6 位数字但不对，回一句中性的"配对码不对或者已经过期"（你自己输错了也需要知道）；
    别的话回一次"请发送配对码"，之后不再回。猜错满 PAIRING_ATTEMPTS 次，这个配对码就作废了，
    要你重新生成：6 位数字只有一百万种，不限次数的话可以一个个试。

配对码是用 secrets 生成的随机数（不是 random：那个能被推算出来），比较时用 compare_digest（不会因为
"前几位对了比较得更久"而泄露信息）。这个文件只管规则，配对码存在哪儿、谁是主人，在 store.py。
"""

from __future__ import annotations

import re
import secrets
import unicodedata

CODE_DIGITS = 6
# 认得出这几种写法："123456"、"123 456"（照着页面上的样子抄的）、"/start 123456"（点了页面上的"在 Telegram 里打开"，
# Telegram 会替你发这一句）。别的都不算配对码
_CODE = re.compile(r"(?:/start\s+)?([0-9]{3})\s?([0-9]{3})")


def new_code() -> str:
    """一个新的配对码：6 位数字，开头可以是 0。"""
    return f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"


def find_code(text: str) -> str | None:
    """一条消息是不是在发配对码。是的话返回那 6 位数字，不是返回 None。"""
    # 中文输入法常打出全角的"１２３４５６"：先换成普通的数字。只认 0-9（\d 还认别的文字里的数字，那些永远对不上）
    match = _CODE.fullmatch(unicodedata.normalize("NFKC", text).strip())
    return match.group(1) + match.group(2) if match else None


def valid(code: object) -> bool:
    """是不是一个合格的配对码（正好 6 位数字）。从文件里读出来的要查一下：文件可能被手改过。"""
    return isinstance(code, str) and len(code) == CODE_DIGITS and code.isascii() and code.isdigit()


def same(given: str, expected: str) -> bool:
    return secrets.compare_digest(given.encode(), expected.encode())
