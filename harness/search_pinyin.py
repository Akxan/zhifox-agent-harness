"""会话搜索里的拼音：用拼音找汉字。search.py 在"原样包含""相近的词""隔字包含"之后多两档，怎么对都在这里算。

  全拼     关键词是一串完整的音节，对上读这几个音、挨在一起的几个汉字：baogao、bao gao、BaoGao、bao'gao 都找到"报告"。
           至少两个音节，也就是至少两个字：一个音节对得上的字太多（bao 能对上"包保报宝抱……"），
           bang、long 这样一个音节的英文词也会对上一大片不相干的汉字。
           只认完整的音节：敲到 baog 还对不上"报告"，敲完 baogao 才对上。要是让最后一个音节只敲开头也算，
           bang 就能拆成 ban + g…，对上"半个""办公"，比找不到更让人摸不着头脑。
  首字母   每个字母是一个字的拼音的第一个字母：zbmb 找到"周报模板"。zh、ch、sh 也只算第一个字母（"中国"是 zg）。
           至少 3 个字母，而且只在标题里找。两个字母的首字母对上的东西太多（bg 能对上"不过""不管""宾馆"……）；
           正文又长又多，3 个字母也几乎处处都有（zgr 对得上每一段里的"这个人"）。标题短，也是人最记得住的。

多音字的每个读音都算："行"读 xing、hang、heng，所以 yinhang 找到"银行"，xingzou 找到"行走"。
隔音符号（'，中文输入法里常打出来的 ’ 也算）和空格是音节的分界：xi'an 只能是"西安"，不会是"先"。

做法：不给每段会话预先算好拼音（几千段长会话的拼音，比原文还占地方），而是反过来，把关键词变成一个正则：
每个音节换成"读这个音的所有汉字"组成的字符集合，baogao 变成 [包保报宝抱…][高告搞稿糕…]。正则在 C 里跑，
扫一遍 1000 万字也只要几十毫秒，和"隔字包含"一样现找现用。
一串字母可能有好几种拆法（xian 是"先"也是"西安"，fangan 是 fang'an 也是 fan'gan），每一种都写成正则里的一条岔路，
同样开头的岔路共用一段。拆法太多（超过 MAX_SPLITS）的不当拼音：那样的一串多半本来就不是拼音，硬拆只会对上一堆不相干的字。

为什么有 narrow 和 wide 两个正则：扩展 B 以后的生僻字在 U+FFFF 之上，Python 的正则遇到这样的字符集合不能用位图，
只能一个一个比，慢一百多倍。几乎没有哪段会话里有这种字，所以平常用不带它们的 narrow；会话里确实有（Document.rare）
才用带它们的 wide。wide 里的生僻字前面先查一下"在不在第 2、3 平面"，大多数字一步就排除了，所以也不算太慢。

汉字的读音来自 Unicode 的 Unihan 数据库，由 tests/make_pinyin.py 生成成 search_pinyin.txt（来源、版本、许可声明写在它开头）。
这张表有 4 万多个字，只在第一次需要按拼音找的时候才读（_table）：启动、平常按中文或者英文搜索都用不到它。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache, lru_cache
from pathlib import Path

TABLE = Path(__file__).with_name("search_pinyin.txt")
PINYIN_MAX = 40  # 字母比这还多的关键词不当拼音：太长的一串，按拼音拆出来的拆法和要编的正则都太大
MIN_SYLLABLES = 2  # 全拼至少几个音节
MIN_INITIALS = 3  # 首字母至少几个字母
MAX_INITIALS = 12  # 首字母最多几个字母：没人会敲十几个首字母，而每个字母都是几千个字的集合，编起来不便宜
MAX_SPLITS = 16  # 一串字母最多接受几种拆法

# 可以当拼音的关键词：字母，中间可以夹着隔音符号和空格（关键词本身不会有空格；几个关键词连成一句时有，见 search.py）
_SPELLING = re.compile(r"[a-z]+(?:['’ ]+[a-z]+)*")
_SEPARATORS = "'’ "
# 只有一个元音的音节（啊 a、哦 o、饿 e）不能直接接在别的音节后面，要接得先写隔音符号（tian'e 是"天鹅"）。
# 不然 bao 会被拆成 ba + o，对上"吧哦"；lie 拆成 li + e，对上"里额"：敲的人想的明明是一个音节。
# 别的以元音开头的音节（an、ao、er……）照样可以接：fangan 要能找到"方案"、xian 要能找到"西安"，没人会去敲隔音符号
_LONE = frozenset({"a", "o", "e"})
_RARE = "\U00020000-\U0003ffff"  # 扩展 B 以后的汉字：第 2、3 平面
_HAN = re.compile(f"[\u3400-\u9fff{_RARE}]")  # 表里的字都在这几段里（兼容汉字已经去掉了，见 tests/make_pinyin.py）
_RARE_HAN = re.compile(f"[{_RARE}]")


@dataclass(frozen=True)
class Spelled:
    """一个拼音关键词编成的正则。两个对的是同样的字，只是 narrow 不认扩展 B 以后的生僻字，快得多。"""

    narrow: re.Pattern[str]
    wide: re.Pattern[str]

    def pattern(self, rare: bool) -> re.Pattern[str]:
        return self.wide if rare else self.narrow


def spellable(term: str) -> bool:
    """这个关键词能不能当拼音试一试。只看写法，用不到拼音表，所以很便宜。"""
    letters = sum(char.isalpha() for char in term)
    return 2 <= letters <= PINYIN_MAX and _SPELLING.fullmatch(term) is not None


def han_in(letters: frozenset[str]) -> tuple[bool, bool]:
    """一段会话里出现过的字符里，有没有汉字、有没有扩展 B 以后的生僻字。收录会话时算一次，用不到拼音表。"""
    chars = "".join(letters)
    return _HAN.search(chars) is not None, _RARE_HAN.search(chars) is not None


def loaded() -> bool:
    """拼音表读进来了没有（测试用：平常的搜索不该读它）。"""
    return _table.cache_info().currsize > 0


@lru_cache(maxsize=256)
def full(term: str) -> Spelled | None:
    """全拼：把关键词编成正则；拆不成至少两个完整的音节（或者拆法太多）就是 None。

    记下最近编过的：边打字边搜时，同一个关键词会被搜好几次（列表刷新、会话存盘以后再搜一遍）。
    """
    if not spellable(term):
        return None
    letters, stops = _letters(term)
    table = _table()
    paths = _splits(letters, stops, table)
    if not paths:
        return None
    return Spelled(re.compile(_branches(paths, wide=False)), re.compile(_branches(paths, wide=True)))


@lru_cache(maxsize=256)
def initials(term: str) -> re.Pattern[str] | None:
    """首字母：每个字母换成"拼音以它开头的所有汉字"。只用在标题上，标题都很短，所以只编一个认得生僻字的 wide。"""
    if not (MIN_INITIALS <= len(term) <= MAX_INITIALS and term.isascii() and term.isalpha()):
        return None
    starts = _starts()
    if any(letter not in starts for letter in term):
        return None  # 没有哪个音节以 i、u、v 开头
    return re.compile("".join(starts[letter] for letter in term))


@cache
def _table() -> dict[str, str]:
    """{音节: 读这个音的所有汉字}。第一次用到时才读文件。

    两个线程同时第一次用到时可能各读一遍，结果一样，不碍事，所以不加锁。
    文件的每一行都检查一遍：音节只许是小写字母，后面只许是汉字（文件万一坏了，也不会把奇怪的东西编进正则）。
    读不到文件（安装坏了）就当没有拼音，搜索照常能用。
    """
    table: dict[str, str] = {}
    try:
        text = TABLE.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return table
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        syllable, _, chars = line.partition("\t")
        if syllable.isascii() and syllable.isalpha() and syllable.islower() and chars and len(_HAN.findall(chars)) == len(chars):
            table[syllable] = chars
    return table


@cache
def _starts() -> dict[str, str]:
    """{字母: 拼音以这个字母开头的所有汉字，编好的字符集合}，首字母用。"""
    gathered: dict[str, set[str]] = {}
    for syllable, chars in _table().items():
        gathered.setdefault(syllable[0], set()).update(chars)
    return {letter: _charset("".join(sorted(chars)), wide=True) for letter, chars in sorted(gathered.items())}


@cache
def _syllable(syllable: str, wide: bool) -> str:
    return _charset(_table()[syllable], wide)


def _letters(term: str) -> tuple[str, frozenset[int]]:
    """去掉分隔符的字母，和分隔符所在的位置（音节必须在这些位置断开）。"""
    letters: list[str] = []
    stops: set[int] = set()
    for char in term:
        if char in _SEPARATORS:
            stops.add(len(letters))
        else:
            letters.append(char)
    return "".join(letters), frozenset(stops)


def _splits(letters: str, stops: frozenset[int], syllables: dict[str, str]) -> list[tuple[str, ...]]:
    """把一串字母拆成音节的所有拆法（至少 MIN_SYLLABLES 个音节）。拆法太多时返回空的，当它不是拼音。"""
    size = len(letters)
    longest = max(map(len, syllables), default=0)

    def ends(at: int) -> list[int]:
        """从第 at 个字母起，下一个音节可以在哪儿结束。不能跨过分隔符。"""
        found = []
        for end in range(at + 1, min(size, at + longest) + 1):
            if any(at < stop < end for stop in stops):
                break
            piece = letters[at:end]
            if piece in _LONE and at > 0 and at not in stops:
                continue  # 单独一个元音的音节只能在开头，或者紧跟在分隔符后面
            if piece in syllables:
                found.append(end)
        return found

    # 先从后往前算出"从这里起能不能正好拆完"，往下找的时候只走拆得完的路：
    # 不然一串前面有很多种拆法、最后却拆不完的字母，会把每一种走法都试一遍
    reach = [False] * (size + 1)
    reach[size] = True
    following = [ends(at) for at in range(size)]
    for at in range(size - 1, -1, -1):
        reach[at] = any(reach[end] for end in following[at])

    paths: list[tuple[str, ...]] = []

    def walk(at: int, path: tuple[str, ...]) -> bool:
        """往下拆；拆法已经太多时返回 False，一路退出去。"""
        if at == size:
            if len(path) >= MIN_SYLLABLES:
                paths.append(path)
            return len(paths) <= MAX_SPLITS
        return all(walk(end, (*path, letters[at:end])) for end in following[at] if reach[end])

    if size == 0 or not walk(0, ()):
        return []
    return paths


def _branches(paths: list[tuple[str, ...]], wide: bool) -> str:
    """把几种拆法写成一个正则：同样的音节开头的共用一段，后面再分岔。"""
    groups: dict[str, list[tuple[str, ...]]] = {}
    for path in paths:
        groups.setdefault(path[0], []).append(path[1:])
    # 每种拆法都正好用完所有字母，所以同一个开头的几条路，要么都到头了，要么都还有下文
    parts = [_syllable(syllable, wide) + (_branches(rests, wide) if rests[0] else "") for syllable, rests in groups.items()]
    return parts[0] if len(parts) == 1 else "(?:" + "|".join(parts) + ")"


def _charset(chars: str, wide: bool) -> str:
    """一串汉字写成正则的字符集合。挨着的码位写成一段（a-z 那样），正则短一些、编得快一些。

    narrow 只放 BMP 里的字（没有的话写成一个永远对不上的 (?!)）；wide 把生僻字放在另一条岔路上，
    前面先查"是不是第 2、3 平面的字"：大多数字在这一步就排除了，不用和几千个生僻字一个个比。
    """
    common = "".join(char for char in chars if char <= "\uffff")
    rare = "".join(char for char in chars if char > "\uffff")
    narrow = f"[{_ranges(common)}]" if common else "(?!)"
    if not wide or not rare:
        return narrow
    return f"(?:{narrow}|(?=[{_RARE}])[{_ranges(rare)}])"


def _ranges(chars: str) -> str:
    codes = sorted(set(map(ord, chars)))
    parts = []
    start = 0
    for index in range(1, len(codes) + 1):
        if index == len(codes) or codes[index] != codes[index - 1] + 1:
            first, last = chr(codes[start]), chr(codes[index - 1])
            parts.append(first if first == last else f"{first}-{last}")
            start = index
    return "".join(parts)
