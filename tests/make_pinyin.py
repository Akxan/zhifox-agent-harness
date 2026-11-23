"""重新生成搜索用的汉字拼音表（harness/search_pinyin.txt）：从 Unicode 的 Unihan 数据库出发。

    uv run python tests/make_pinyin.py                  # 下载 Unihan.zip，生成表，写进 harness/search_pinyin.txt
    uv run python tests/make_pinyin.py --zip 某个文件     # 用已经下载好的 Unihan.zip（照样核对 SHA-256）
    uv run python tests/make_pinyin.py --check          # 只生成、不写：和仓库里那份一字不差就退出 0，否则退出 1

程序运行时只读生成好的那个文件，不会再下载、也不会再生成（和 tests/make_icons.py 一样，是开发时用一次的工具）。
同样的输入一定得到同样的输出：下载的版本和它的 SHA-256 都钉死在下面，表里的音节、汉字都按固定的顺序排。
Unicode 出了新版本想跟上时，改 VERSION 和 SHA256，运行一次，把新的表一起提交。

为什么用 Unihan：它是 Unicode 官方的汉字数据库，许可证（Unicode License v3）允许随程序分发，只要带上版权和许可声明
（生成的文件开头就带着，见 NOTICE）。别的拼音库要么是另一种许可证，要么本身也是从 Unihan 和词典里整理来的。

用哪几栏读音（Unihan_Readings.txt 里的字段，格式见 https://www.unicode.org/reports/tr38/ ）：
  kMandarin    每个字"最常用的读音"，覆盖 4 万多个字。表里收哪些字以它为准：有这一栏的才收。
               但它对多音字只给一个（"行"只有 xíng），只靠它的话 yinhang 找不到"银行"。
  kTGHZ2013    《通用规范汉字字典》（2013）：《通用规范汉字表》的 8105 个字，每个读音都列出来。
  kXHC1983     《现代汉语词典》（1983）：1 万多个字的全部读音。
  kHanyuPinlu  《现代汉语频率词典》：常用字在真实文章里出现过的读音。
  后三栏都是现代汉语里实际在用的读音，所以多音字的每个读音都收（"行"有 xing、hang、heng）。
  不用 kHanyuPinyin（《汉语大字典》）：它收了大量古音（"告"在那里还读 jū、gù），收进来只会让搜索对上不相干的字。

怎么处理：
  - 去掉声调（bào → bao），ü 写成 v（lǜ → lv，和拼音输入法的打法一样）。
  - 去掉没有元音的读音（嗯 ń/ňg、呣 m̀、哼 hng、噷 hm、儿化的 r）：这些是叹词，没人会用它们搜索，
    留着反而让关键词有很多种拆法（an 能拆成 a + n），拆法越多越容易对上不相干的字。
  - 去掉 CJK 兼容汉字（U+F900 起的那些）：搜索统一写法（NFKC）时它们会变成对应的统一汉字，表里有统一汉字就够了。

下载下来的东西不可信：放在单独的临时文件夹里，先核对 SHA-256 再打开；只读压缩包里的 Unihan_Readings.txt 一项、
限制大小、不解压到磁盘；每一行都要符合格式，码位、读音的写法对不上的一律跳过。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import tempfile
import unicodedata
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "harness" / "search_pinyin.txt"

VERSION = "18.0.0"
URL = f"https://www.unicode.org/Public/{VERSION}/ucd/Unihan.zip"
SHA256 = "4c93ea9c1f636451729a840978f1667a53886af37ba854fdcce109721c63d43e"
MEMBER = "Unihan_Readings.txt"
MAX_DOWNLOAD = 64 * 1024 * 1024  # Unihan.zip 大约 8 MB，Unihan_Readings.txt 解开大约 9 MB；超过这么多就不对劲了
MAX_MEMBER = 64 * 1024 * 1024

FIELDS = ("kMandarin", "kTGHZ2013", "kXHC1983", "kHanyuPinlu")
LINE = re.compile(r"U\+([0-9A-F]{4,6})\t(k[A-Za-z0-9]+)\t(.+)")
SYLLABLE = re.compile(r"[a-z]{1,8}")
VOWEL = re.compile(r"[aeiouv]")
# 汉字所在的码位：扩展 A 到基本区（U+3400–U+9FFF）、兼容汉字（U+F900–U+FAFF，后面会去掉大多数）、第 2、3 平面（扩展 B 以后）
HAN = ((0x3400, 0x9FFF), (0xF900, 0xFAFF), (0x20000, 0x3FFFF))

# Unicode License v3 的原文（https://www.unicode.org/license.txt ）。许可证要求随数据一起带上这段声明，
# 所以原样写进生成的文件开头。年份跟着 Unicode 发布的那一份走：换 VERSION 时一起核对
NOTICE = """\
UNICODE LICENSE V3

COPYRIGHT AND PERMISSION NOTICE

Copyright © 1991-2026 Unicode, Inc.

NOTICE TO USER: Carefully read the following legal agreement. BY
DOWNLOADING, INSTALLING, COPYING OR OTHERWISE USING DATA FILES, AND/OR
SOFTWARE, YOU UNEQUIVOCALLY ACCEPT, AND AGREE TO BE BOUND BY, ALL OF THE
TERMS AND CONDITIONS OF THIS AGREEMENT. IF YOU DO NOT AGREE, DO NOT
DOWNLOAD, INSTALL, COPY, DISTRIBUTE OR USE THE DATA FILES OR SOFTWARE.

Permission is hereby granted, free of charge, to any person obtaining a
copy of data files and any associated documentation (the "Data Files") or
software and any associated documentation (the "Software") to deal in the
Data Files or Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, and/or sell
copies of the Data Files or Software, and to permit persons to whom the
Data Files or Software are furnished to do so, provided that either (a)
this copyright and permission notice appear with all copies of the Data
Files or Software, or (b) this copyright and permission notice appear in
associated Documentation.

THE DATA FILES AND SOFTWARE ARE PROVIDED "AS IS", WITHOUT WARRANTY OF ANY
KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT OF
THIRD PARTY RIGHTS.

IN NO EVENT SHALL THE COPYRIGHT HOLDER OR HOLDERS INCLUDED IN THIS NOTICE
BE LIABLE FOR ANY CLAIM, OR ANY SPECIAL INDIRECT OR CONSEQUENTIAL DAMAGES,
OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS,
WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION,
ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THE DATA
FILES OR SOFTWARE.

Except as contained in this notice, the name of a copyright holder shall
not be used in advertising or otherwise to promote the sale, use or other
dealings in these Data Files or Software without prior written
authorization of the copyright holder.
"""


def toneless(reading: str) -> str:
    """去掉声调：先拆成"字母 + 声调符号"（NFD），ü 写成 v，再扔掉所有的附加符号（ê 就成了 e）。"""
    spelled = unicodedata.normalize("NFD", reading).replace("u\u0308", "v")  # u 加上分音符（¨）就是 ü
    return "".join(char for char in spelled if not unicodedata.combining(char))


def readings_of(field: str, value: str) -> list[str]:
    """一栏里写着的读音（还带着声调）。各栏的写法见 TR38。"""
    if field == "kMandarin":  # "xíng"；两个值时第一个是大陆的、第二个是台湾的习惯读法
        return value.split()
    if field in ("kTGHZ2013", "kXHC1983"):  # "0442.080:háng 0443.050:hàng"：页码和位置，冒号后面是读音（可以用逗号列几个）
        return [reading for entry in value.split() for reading in entry.rpartition(":")[2].split(",")]
    if field == "kHanyuPinlu":  # "xíng(2943) háng(218)"：括号里是出现的次数
        return [re.sub(r"\(\d+\)$", "", entry) for entry in value.split()]
    return []


def _is_han(code: int) -> bool:
    return any(low <= code <= high for low, high in HAN)


def parse(text: str) -> dict[str, str]:
    """读 Unihan_Readings.txt 的内容，得到 {音节: 读这个音的所有汉字（按码位排好）}。多音字在它的每个音节下各出现一次。"""
    found: dict[int, dict[str, str]] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = LINE.fullmatch(line)
        if match is None or match.group(2) not in FIELDS:
            continue
        code = int(match.group(1), 16)
        if not _is_han(code):
            continue
        found.setdefault(code, {})[match.group(2)] = match.group(3)

    table: dict[str, set[int]] = {}
    for code, fields in found.items():
        char = chr(code)
        # 只收有 kMandarin 的字；兼容汉字搜索时会统一成别的字，不收
        if "kMandarin" not in fields or unicodedata.normalize("NFKC", char) != char:
            continue
        spelled = {toneless(reading) for field, value in fields.items() for reading in readings_of(field, value)}
        for syllable in spelled:
            if SYLLABLE.fullmatch(syllable) and VOWEL.search(syllable):
                table.setdefault(syllable, set()).add(code)
    return {syllable: "".join(chr(code) for code in sorted(codes)) for syllable, codes in sorted(table.items())}


def render(table: dict[str, str]) -> str:
    """写成文件：开头是说明和许可声明（每行以 # 开头），后面每行一个音节、一个制表符、读这个音的所有汉字。"""
    chars = {char for line in table.values() for char in line}
    header = [
        "智狐搜索用的汉字拼音表（不带声调）。由 tests/make_pinyin.py 生成，不要手改；程序在 harness/search_pinyin.py 里读它。",
        "ZhiFox's toneless pinyin table for conversation search. Generated by tests/make_pinyin.py; do not edit by hand.",
        "",
        f"Source: Unicode Han Database (Unihan), Unicode {VERSION}, {MEMBER}",
        f"        {URL}",
        f"        SHA-256 {SHA256}",
        f"Fields: {', '.join(FIELDS)} (only characters that have kMandarin; tones removed, ü written as v,",
        "        readings without a vowel dropped, CJK compatibility ideographs dropped)",
        f"Size:   {len(table)} syllables, {len(chars)} characters, {sum(len(line) for line in table.values())} entries",
        "Format: one syllable per line, a tab, then every character with that reading in code point order.",
        "        A character with several readings appears once under each of them.",
        "",
        "The Unihan data is used under the Unicode License v3:",
        "",
        *NOTICE.splitlines(),
    ]
    lines = [("# " + line).rstrip() for line in header]
    lines += [f"{syllable}\t{chars}" for syllable, chars in table.items()]
    return "\n".join(lines) + "\n"


def read_zip(path: Path) -> str:
    """核对 SHA-256，再从压缩包里读出 Unihan_Readings.txt（只读这一项、不解压到磁盘）。"""
    data = path.read_bytes()
    if len(data) > MAX_DOWNLOAD:
        raise SystemExit(f"{path} 太大了（{len(data)} 字节），不像是 Unihan.zip")
    digest = hashlib.sha256(data).hexdigest()
    if digest != SHA256:
        raise SystemExit(f"{path} 的 SHA-256 是 {digest}，和钉死的 {SHA256} 对不上：换了版本就先改 VERSION 和 SHA256")
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(MEMBER)
        if info.file_size > MAX_MEMBER:
            raise SystemExit(f"{MEMBER} 解开有 {info.file_size} 字节，太大了")
        with archive.open(info) as member:
            raw = member.read(MAX_MEMBER + 1)
    if len(raw) > MAX_MEMBER:
        raise SystemExit(f"{MEMBER} 实际读出来超过 {MAX_MEMBER} 字节")
    return raw.decode("utf-8")


def download(folder: Path) -> Path:
    """把 Unihan.zip 下载进一个单独的空文件夹。"""
    target = folder / "Unihan.zip"
    request = urllib.request.Request(URL, headers={"User-Agent": "make_pinyin.py"})
    with urllib.request.urlopen(request, timeout=120) as response, target.open("wb") as out:
        total = 0
        while chunk := response.read(1 << 16):
            total += len(chunk)
            if total > MAX_DOWNLOAD:
                raise SystemExit(f"下载超过了 {MAX_DOWNLOAD} 字节，停下")
            out.write(chunk)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 Unihan 生成 harness/search_pinyin.txt")
    parser.add_argument("--zip", type=Path, help="已经下载好的 Unihan.zip（不再下载）")
    parser.add_argument("--check", action="store_true", help="只核对：生成的和仓库里那份一样吗")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="unihan-") as folder:
        source = args.zip if args.zip else download(Path(folder))
        text = render(parse(read_zip(source)))
    if args.check:
        same = OUTPUT.exists() and OUTPUT.read_text(encoding="utf-8") == text
        print("一样" if same else f"不一样：{OUTPUT} 需要重新生成")
        return 0 if same else 1
    with OUTPUT.open("w", encoding="utf-8", newline="\n") as out:
        out.write(text)
    print(f"写好了 {OUTPUT}（{len(text.encode('utf-8'))} 字节）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
