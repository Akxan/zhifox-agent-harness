"""把一个程序后面的词分成选项和参数（-n 5、--lines=5、-abc、--），以及按名单检查选项。

几个容易出错的地方，这里都往"多问"的方向处理：

  - GNU 的程序接受长选项的缩写：sort --out=x 就是 --output=x，sed --in-pl 就是 --in-place。
    所以"不许用的长选项"连它的缩写一起不许：给的选项是某个不许用的选项的开头，就算用了它。
    "只许用这些"的名单反过来，只认写全的，缩写一律不认。
  - BSD（macOS）的程序碰到第一个参数就不再认选项：uniq a.txt -c 在 macOS 上把 -c 当成输出文件名。
    所以参数后面出现的、看起来像选项的词，既当选项检查，也当参数算（数个数、检查路径）。
  - 选项要不要带值，影响后面的词是值还是参数。只许用名单里选项的程序，名单里写清楚哪些带值；
    别的程序里不认识的选项当作不带值：这样它后面的词会被当成参数，多检查一遍路径、多算一个参数，只会多问。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .words import Unclear


def names(text: str) -> frozenset[str]:
    """"-a -b --cc" → {"-a", "-b", "--cc"}，写名单用。"""
    return frozenset(text.split())


@dataclass
class Parsed:
    options: list[tuple[str, str | None]] = field(default_factory=list)  # (名字, 值)：("-n", "5")、("--color", None)
    operands: list[str] = field(default_factory=list)
    late: list[str] = field(default_factory=list)  # 参数后面出现的、像选项的词（BSD 会把它们当成参数）
    attached: list[str] = field(default_factory=list)  # 短选项词里可能紧贴着写的值（-m.env 里的 .env、m.env……）

    def has(self, *wanted: str) -> bool:
        return any(name in wanted for name, _ in self.options)

    def value(self, *wanted: str) -> str | None:
        """最后一次给这几个选项的值（没有给过返回 None）。"""
        found = None
        for name, value in self.options:
            if name in wanted:
                found = value
        return found

    def values(self) -> list[str]:
        return [value for _, value in self.options if value is not None]

    def words(self, attached: bool = True) -> list[str]:
        """所有可能是路径的词：参数、选项的值、参数后面像选项的词、短选项词里可能紧贴着的值（attached 为假时不要这一样）。"""
        return [*self.operands, *self.values(), *self.late, *(self.attached if attached else [])]


def parse(words: list[str], values: frozenset[str] = frozenset(), optional: frozenset[str] = frozenset(), numbers: bool = False) -> Parsed:
    """values：带值的选项（-n 5、-n5、--lines 5、--lines=5）。optional：值只能紧贴着写的选项（-Iseconds、--color=auto）。
    numbers：-20 这样的数字算一个选项（head -20、grep -3），记成 ("-NUM", "20")。"""
    parsed = Parsed()
    index = 0
    while index < len(words):
        word = words[index]
        index += 1
        if word == "--":
            parsed.operands.extend(words[index:])
            break
        if not word.startswith("-") or word == "-":
            parsed.operands.append(word)
            continue
        if parsed.operands:
            parsed.late.append(word)
        if word.startswith("--"):
            name, equals, value = word.partition("=")
            if equals:
                parsed.options.append((name, value))
            elif name in values:
                if index >= len(words):
                    raise Unclear(f"{name} 后面缺少值")
                parsed.options.append((name, words[index]))
                index += 1
            else:
                parsed.options.append((name, None))
            continue
        if numbers and word[1:].isdigit():
            parsed.options.append(("-NUM", word[1:]))
            continue
        cluster = word[1:]
        parsed.attached.extend(cluster[at:] for at in range(1, len(cluster)))
        for at, char in enumerate(cluster):
            name, rest = "-" + char, cluster[at + 1 :]
            if name in values:
                if not rest:
                    if index >= len(words):
                        raise Unclear(f"{name} 后面缺少值")
                    rest = words[index]
                    index += 1
                parsed.options.append((name, rest))
                break
            if name in optional:
                parsed.options.append((name, rest or None))
                break
            parsed.options.append((name, None))
    return parsed


def deny(parsed: Parsed, denied: frozenset[str]) -> None:
    """用了 denied 里的选项（长选项连缩写一起算），就抛出 Unclear。"""
    for name, _ in parsed.options:
        if name in denied:
            raise Unclear(f"用了 {name}")
        if name.startswith("--") and len(name) > 2 and any(item.startswith(name) for item in denied if item.startswith("--")):
            raise Unclear(f"用了 {name}（某个不许用的选项的缩写）")


def allow(parsed: Parsed, allowed: frozenset[str]) -> None:
    """用了 allowed 以外的选项，就抛出 Unclear。长选项要写全。"""
    for name, _ in parsed.options:
        if name not in allowed:
            raise Unclear(f"不认识的选项 {name}")
