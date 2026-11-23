"""字节 → 文字：取回来的网页是什么编码。

按这个顺序认（和浏览器差不多，见 WHATWG 的 Encoding 标准 https://encoding.spec.whatwg.org/）：
  1. 开头的 BOM（UTF-8、UTF-16）：有它就是它，别的说法都不算
  2. 回答头 Content-Type 里的 charset=
  3. 网页自己写的：HTML 的 <meta charset="…"> 或者 <meta http-equiv="Content-Type" content="…; charset=…">，
     XML 的 <?xml … encoding="…"?>。只看开头 SNIFF_BYTES 个字节（浏览器也只看开头）
  4. 都没说：是 UTF-8 就按 UTF-8；不是的话按 windows-1252（浏览器对没写编码的西文网页就是这么办的）

几个名字按浏览器的认法换一下：gb2312、gbk 按 gb18030 解（它的超集，写着 gb2312 的网页常常用了 gbk 才有的字）；
iso-8859-1、ascii 按 windows-1252（浏览器也是这样，0x80~0x9F 那几个字符按 1252 画）；shift_jis 按 cp932，euc-kr 按 cp949。
网页里写着 utf-16 的，字节其实是一个字节一个字节的（不然这句 meta 都读不出来），按 UTF-8。
认不出的名字当作没说。解不了的字换成 �，这里从不出错。
"""

from __future__ import annotations

import codecs
import re

SNIFF_BYTES = 4096
FALLBACK = "cp1252"

_ALIASES = {
    "gb2312": "gb18030", "gbk": "gb18030", "x-gbk": "gb18030", "gb_2312-80": "gb18030", "chinese": "gb18030",
    "iso-8859-1": "cp1252", "iso8859-1": "cp1252", "latin1": "cp1252", "latin-1": "cp1252", "l1": "cp1252",
    "us-ascii": "cp1252", "ascii": "cp1252", "windows-1252": "cp1252",
    "shift_jis": "cp932", "shift-jis": "cp932", "sjis": "cp932", "x-sjis": "cp932", "windows-31j": "cp932",
    "euc-kr": "cp949", "ks_c_5601-1987": "cp949",
    "big5": "big5hkscs",
}  # fmt: skip
_BOMS = ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"))

_CHARSET = re.compile(rb"""charset\s*=\s*["']?\s*([A-Za-z0-9_.:\-]+)""", re.IGNORECASE)
_META = re.compile(rb"<meta\b[^>]*>", re.IGNORECASE)
# 一个标签里的属性：名字="值"、名字='值'、名字=值
_ATTRIBUTE = re.compile(rb"""([A-Za-z][A-Za-z0-9_:\-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")
_XML = re.compile(rb"""^\s*<\?xml[^>]*\bencoding\s*=\s*["']([A-Za-z0-9_.:\-]+)["']""", re.IGNORECASE)


def codec_of(label: str | bytes | None) -> str | None:
    """一个编码的名字 → Python 解得了的编码；认不出返回 None。"""
    if not label:
        return None
    name = (label.decode("ascii", "ignore") if isinstance(label, bytes) else label).strip().strip("\"'").lower()
    name = _ALIASES.get(name, name)
    try:
        found = codecs.lookup(name).name
    except LookupError:
        return None
    return "utf-8" if found.startswith("utf-16") else found  # 写在 meta 里、头里的 utf-16：字节其实是一个一个的（见文件开头）


def declared(content_type: str) -> str | None:
    """回答头 Content-Type 里说的编码。"""
    found = _CHARSET.search(content_type.encode("latin-1", "ignore"))
    return codec_of(found.group(1)) if found else None


def sniffed(body: bytes) -> str | None:
    """网页自己写的编码：HTML 的 <meta>、XML 的声明。只看开头。"""
    head = body[:SNIFF_BYTES]
    found = _XML.match(head)
    if found:
        return codec_of(found.group(1))
    for tag in _META.findall(head):
        # 只认两种写法：<meta charset="…">，和 <meta http-equiv="Content-Type" content="text/html; charset=…">。
        # 别的 <meta> 的内容里提到 charset=（一篇讲编码的文章的简介）不算
        values = {name.lower(): next((part for part in rest if part), b"") for name, *rest in _ATTRIBUTE.findall(tag[5:])}
        name = codec_of(values.get(b"charset"))
        if name is None and values.get(b"http-equiv", b"").strip().lower() == b"content-type":
            match = _CHARSET.search(values.get(b"content", b""))
            name = codec_of(match.group(1)) if match else None
        if name is not None:
            return name
    return None


def decode(body: bytes, content_type: str = "", markup: bool = False, cut: bool = False) -> str:
    """按上面的顺序认出编码，解成文字。markup：是 HTML 或 XML（才去看网页自己写的编码）。
    cut：太大了只留了前面一截（见 client.Fetched.cut），最后一个字可能被切成了两半。"""
    for bom, codec in _BOMS:
        if body.startswith(bom):
            return body[len(bom) :].decode(codec, "replace")
    name = declared(content_type) or (sniffed(body) if markup else None)
    if name is not None:
        return body.decode(name, "replace")
    try:
        if not cut:
            return body.decode("utf-8")
        # 切断的：最后半个字不算错（增量的解码器留着它等下文，final=False），前面的照样要是 UTF-8。
        # 没切断的不这么宽容：b"caf\xe9" 是 windows-1252 的 café，不是被切断的 UTF-8
        return codecs.getincrementaldecoder("utf-8")().decode(body, final=False)
    except UnicodeDecodeError:
        return body.decode(FALLBACK, "replace")
