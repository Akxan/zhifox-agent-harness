"""从 Word、Excel、PowerPoint 文件里取出文字，让每一家模型都能读。

为什么要自己取：四家模型的接口都不收 .docx、.xlsx、.pptx（Claude 和 GPT 收 PDF、图片和文字，DeepSeek 只收文字）。
这三种文件其实是 zip 压缩包，里面是几份 XML，正文就写在 XML 里。所以用标准库的 zipfile 打开，把文字按原来的
顺序取出来，当成一个文本文件交给模型。哪一家模型都能读；代价是图片、图表和排版没有了，交给模型的内容里会注明
这一点（见 NOTE 和 attachments.wrap_text），免得它以为自己看到了全部。

取出来的样子：
  Word        一段一行；标题前面加 #（几级标题几个 #）；列表项前面加 "- "；表格一行一行写，格子之间用 " | " 隔开
  Excel       每张工作表一节，格子之间用制表符隔开（TSV）；日期按单元格的格式还原成 2026-11-20 这样的写法
  PowerPoint  每张幻灯片一节，按播放的顺序；有演讲者备注的跟在后面

文件是用户自己选的，但里面的内容可能是任何人做的，所以：
  - 压缩包可能是"解压炸弹"（几 KB 解压出几 GB）。每一份 XML 解压后的大小、加起来的总量都有上限，
    读的时候也最多读到上限，不信压缩包里自己写的大小；
  - XML 可能带 DTD 和实体定义（"十亿笑声"攻击：几行实体定义展开成几 GB 的文字）。Office 文件从来不用 DTD，
    见到就当作文件损坏。

只认标签的"本地名"（去掉命名空间的那部分）：Word 另存为"严格 Open XML"时命名空间不一样，标签名是一样的。
"""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
import zlib
from datetime import datetime, timedelta
from xml.etree import ElementTree

from .i18n import t

MAX_DOCUMENT_BYTES = 25_000_000  # 文件本身（压缩后）最大多少，和 PDF 一样
MAX_PART_BYTES = 50_000_000  # 压缩包里的一份 XML 解压后最大多少
MAX_TOTAL_BYTES = 200_000_000  # 一个文件里读的所有 XML 解压后加起来最多多少
MAX_TEXT_CHARS = 400_000  # 取出来的文字最多多少个字符，和文本文件一样（见 attachments.py）

# 能读的扩展名：普通文件、带宏的文件（m）、模板（t）、PowerPoint 的放映文件（s），里面的结构都一样
SUFFIXES = (".docx", ".docm", ".dotx", ".xlsx", ".xlsm", ".xltx", ".pptx", ".pptm", ".potx", ".ppsx")

# 交给模型时附在文件名旁边的说明（写给模型看的，所以不翻译）
NOTE = "从 Office 文件里取出的文字：图片、图表和排版没有保留"

# 旧版 Office（.doc、.xls、.ppt）和加了密码的新版 Office 文件都是这种"复合文档"格式，开头是这 8 个字节
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# Word 里的这些部分不是正文：段落和文字的格式设置、被修订删掉的字、域代码
# Fallback：文本框在文件里写了两遍（新旧两种写法，旧版 Word 读后一种），只取前一种，不然字会重复
_WORD_SKIP = {"pPr", "rPr", "delText", "instrText", "fldChar", "del", "moveFrom", "Fallback"}
# 包在段落、表格外面的"壳"（内容控件、自定义 XML），里面的东西照常算正文
_WORD_WRAPPERS = {"sdt", "sdtContent", "customXml", "smartTag", "ins", "moveTo"}

# Excel 内置的日期、时间格式的编号（其余编号是数字、百分比、货币等）
_EXCEL_DATES = set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59))


class DocumentError(ValueError):
    """文件读不了。错误信息可以直接显示给用户。"""


def extract_text(name: str, content: bytes) -> str:
    """取出 Word、Excel 或 PowerPoint 文件里的文字。读不了时抛出 DocumentError，说明原因和怎么办。"""
    if content.startswith(_OLE):
        raise DocumentError(
            t(
                "“{name}”打不开：它可能设了密码，或者是旧版的 Office 格式（.doc、.xls、.ppt）。"
                "去掉密码、或者另存为 .docx、.xlsx、.pptx 或 PDF 再发。",
                name=name,
            )
        )
    if not content.startswith(b"PK"):
        raise DocumentError(t("“{name}”不是有效的 Word、Excel 或 PowerPoint 文件。", name=name))
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            package = _Package(archive, name)
            main = package.main_part()
            kind = _local(package.xml(main).tag)
            if kind == "document":
                text = _word(package, main)
            elif kind == "workbook":
                text = _excel(package, main)
            elif kind == "presentation":
                text = _slides(package, main)
            else:
                raise DocumentError(t("“{name}”不是有效的 Word、Excel 或 PowerPoint 文件。", name=name))
    except DocumentError:
        raise
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError, ElementTree.ParseError,
            ValueError, KeyError, IndexError) as exc:
        # 压缩包坏了、用了不支持的压缩方式、压缩包本身设了密码、XML 写错了、里面的数字或编号不对
        raise DocumentError(t("“{name}”打不开，文件可能损坏了。", name=name)) from exc

    text = re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in text.splitlines())).strip()
    if not text:
        raise DocumentError(t("“{name}”里没有找到文字（可能只有图片）。可以另存为 PDF 发给 Claude 或 GPT，或者截图发过来。", name=name))
    if len(text) > MAX_TEXT_CHARS:
        raise DocumentError(_too_long(name))
    return text


def _too_long(name: str) -> str:
    return t("“{name}”里的文字太多了，超过 40 万个字符。可以只把需要的部分复制进来。", name=name)


# ---- 压缩包 -----------------------------------------------------------------


class _Package:
    """打开的 Office 文件：按名字读里面的 XML，顺带记账，解压的总量不能超过上限。"""

    def __init__(self, archive: zipfile.ZipFile, name: str):
        self.archive, self.name, self.used = archive, name, 0
        self.names = set(archive.namelist())

    def __contains__(self, part: str) -> bool:
        return part in self.names

    def read(self, part: str) -> bytes:
        damaged = t("“{name}”打不开，文件可能损坏了。", name=self.name)
        if part not in self.names:
            raise DocumentError(damaged)
        info = self.archive.getinfo(part)
        if info.file_size > MAX_PART_BYTES:
            raise DocumentError(_too_long(self.name))
        with self.archive.open(info) as stream:
            data = stream.read(MAX_PART_BYTES + 1)  # 不信压缩包里写的大小：最多读到上限多一个字节
        self.used += len(data)
        if len(data) > MAX_PART_BYTES or self.used > MAX_TOTAL_BYTES:
            raise DocumentError(_too_long(self.name))
        if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
            raise DocumentError(damaged)
        return data

    def xml(self, part: str) -> ElementTree.Element:
        return ElementTree.fromstring(self.read(part))

    def main_part(self) -> str:
        """正文在哪一份 XML 里：看压缩包根上的关系表（_rels/.rels），没有的话用三种文件的默认位置。"""
        for kind, path in self.rels("").values():
            if kind.endswith("/officeDocument") and path in self:
                return path
        for path in ("word/document.xml", "xl/workbook.xml", "ppt/presentation.xml"):
            if path in self:
                return path
        raise DocumentError(t("“{name}”不是有效的 Word、Excel 或 PowerPoint 文件。", name=self.name))

    def rels(self, part: str) -> dict[str, tuple[str, str]]:
        """一份 XML 的关系表：编号 → （关系的类型，指向的那份 XML 在压缩包里的路径）。

        关系表写在旁边的 _rels/文件名.rels 里，路径是相对这份 XML 所在的目录写的。
        """
        folder, _, file = part.rpartition("/")
        table = f"{folder}/_rels/{file}.rels" if folder else f"_rels/{file}.rels" if file else "_rels/.rels"
        if table not in self:
            return {}
        found = {}
        for rel in self.xml(table):
            target = rel.get("Target") or ""
            if _local(rel.tag) != "Relationship" or rel.get("TargetMode") == "External" or not target:
                continue
            path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(folder, target))
            found[rel.get("Id") or ""] = (rel.get("Type") or "", path)
        return found

    def related(self, part: str, kind: str, default: str = "") -> str:
        """part 指向的、某一种类型的那份 XML（比如工作簿的"共享字符串表"）。找不到返回 default。"""
        for rel_kind, path in self.rels(part).values():
            if rel_kind.endswith("/" + kind) and path in self:
                return path
        return default if default in self else ""


def _local(tag: object) -> str:
    """去掉命名空间的标签名：{http://...}p → p"""
    return tag.rpartition("}")[2] if isinstance(tag, str) else ""


def _attr(element: ElementTree.Element, name: str) -> str:
    """按本地名取属性（w:val、val 都算）。"""
    for key, value in element.attrib.items():
        if key == name or key.endswith("}" + name):
            return value
    return ""


def _relation_id(element: ElementTree.Element) -> str:
    """r:id 属性。不能按本地名找：幻灯片列表里同一个元素上还有一个不带命名空间的 id。"""
    return next((value for key, value in element.attrib.items() if key.startswith("{") and key.endswith("}id")), "")


def _children(element: ElementTree.Element, name: str):
    return (child for child in element if _local(child.tag) == name)


# ---- Word -------------------------------------------------------------------


def _word(package: _Package, main: str) -> str:
    levels = _heading_levels(package, package.related(main, "styles", "word/styles.xml"))
    body = next(_children(package.xml(main), "body"), None)
    lines: list[str] = []
    if body is not None:
        _word_blocks(body, levels, lines)
    return "\n".join(lines)


def _word_blocks(parent: ElementTree.Element, levels: dict[str, int], lines: list[str]) -> None:
    for element in parent:
        name = _local(element.tag)
        if name == "p":
            lines.append(_word_paragraph(element, levels))
        elif name == "tbl":
            lines.append("")
            for row in _word_kids(element, "tr"):
                cells = [" ".join(filter(None, (_word_text(p) for p in cell.iter() if _local(p.tag) == "p"))) for cell in _word_kids(row, "tc")]
                lines.append(" | ".join(cell.strip() for cell in cells))
            lines.append("")
        elif name in _WORD_WRAPPERS:
            _word_blocks(element, levels, lines)


def _word_kids(element: ElementTree.Element, name: str):
    """直接的子元素，穿过内容控件之类的"壳"。"""
    for child in element:
        if _local(child.tag) == name:
            yield child
        elif _local(child.tag) in _WORD_WRAPPERS:
            yield from _word_kids(child, name)


def _word_paragraph(paragraph: ElementTree.Element, levels: dict[str, int]) -> str:
    text = _word_text(paragraph).strip()
    if not text:
        return ""
    settings = next(_children(paragraph, "pPr"), None)
    level, listed = 0, False
    if settings is not None:
        style = next(_children(settings, "pStyle"), None)
        level = levels.get(_attr(style, "val"), 0) if style is not None else 0
        outline = next(_children(settings, "outlineLvl"), None)
        if outline is not None and _attr(outline, "val").isdigit() and int(_attr(outline, "val")) < 6:
            level = int(_attr(outline, "val")) + 1
        listed = next(_children(settings, "numPr"), None) is not None
    if level:
        return "#" * level + " " + text
    return "- " + text if listed else text


def _word_text(element: ElementTree.Element) -> str:
    pieces: list[str] = []

    def walk(node: ElementTree.Element) -> None:
        for child in node:
            name = _local(child.tag)
            if name in _WORD_SKIP:
                continue
            if name == "t":
                pieces.append(child.text or "")
            elif name == "tab":
                pieces.append("\t")
            elif name in ("br", "cr"):
                pieces.append("\n")
            elif name == "noBreakHyphen":
                pieces.append("-")
            else:
                walk(child)

    walk(element)
    return "".join(pieces)


def _heading_levels(package: _Package, styles: str) -> dict[str, int]:
    """段落样式的编号 → 第几级标题。

    样式的编号随语言变（中文版 Word 里"标题 1"的编号可能就是 "1"），但内置样式的名字永远是英文的 heading 1，
    所以按名字认；样式里直接写了大纲级别的也算。
    """
    if not styles:
        return {}
    levels = {}
    for style in package.xml(styles):
        if _local(style.tag) != "style" or _attr(style, "type") != "paragraph":
            continue
        name_element = next(_children(style, "name"), None)
        name = _attr(name_element, "val").lower() if name_element is not None else ""
        found = re.fullmatch(r"heading ([1-6])", name)
        level = int(found.group(1)) if found else 1 if name == "title" else 0
        settings = next(_children(style, "pPr"), None)
        outline = next(_children(settings, "outlineLvl"), None) if settings is not None else None
        if not level and outline is not None and _attr(outline, "val").isdigit() and int(_attr(outline, "val")) < 6:
            level = int(_attr(outline, "val")) + 1
        if level:
            levels[_attr(style, "styleId")] = level
    return levels


# ---- Excel ------------------------------------------------------------------


def _excel(package: _Package, main: str) -> str:
    workbook = package.xml(main)
    sheets = package.rels(main)
    date1904 = any(_local(item.tag) == "workbookPr" and _attr(item, "date1904") in ("1", "true") for item in workbook)
    shared = _shared_strings(package, package.related(main, "sharedStrings", "xl/sharedStrings.xml"))
    dates = _date_styles(package, package.related(main, "styles", "xl/styles.xml"))
    sections, used = [], 0
    for sheet in workbook.iter():
        if _local(sheet.tag) != "sheet":
            continue
        kind, path = sheets.get(_relation_id(sheet), ("", ""))
        if not kind.endswith("/worksheet") or path not in package:
            continue  # 图表工作表、对话框工作表里没有格子
        rows = _sheet_rows(package, path, shared, dates, date1904, MAX_TEXT_CHARS - used)
        if rows:
            sections.append(f"## 工作表：{_attr(sheet, 'name')}\n" + "\n".join(rows))
            used += len(sections[-1])
    return "\n\n".join(sections)


def _shared_strings(package: _Package, path: str) -> list[str]:
    """共享字符串表：Excel 把格子里的文字集中存在这里，格子里只写编号。"""
    if not path:
        return []
    strings = []
    for item in package.xml(path):
        if _local(item.tag) != "si":
            continue
        strings.append(_cell_text(item))
    return strings


def _cell_text(item: ElementTree.Element) -> str:
    """一段（可能分成几截、带格式的）文字。跳过注音（rPh）：日文里汉字上面的假名，不是正文。"""
    pieces = []
    for child in item:
        name = _local(child.tag)
        if name == "t":
            pieces.append(child.text or "")
        elif name == "r":
            pieces += [part.text or "" for part in child if _local(part.tag) == "t"]
    return "".join(pieces)


def _date_styles(package: _Package, path: str) -> set[int]:
    """哪些单元格样式是日期或时间。Excel 里日期存的是数字（1900 年以来的第几天），只有样式说明它是日期。"""
    if not path:
        return set()
    root = package.xml(path)
    custom = {}
    for formats in _children(root, "numFmts"):
        for item in _children(formats, "numFmt"):
            if _attr(item, "numFmtId").isdigit():
                custom[int(_attr(item, "numFmtId"))] = _attr(item, "formatCode")
    styles = []
    for cell_styles in _children(root, "cellXfs"):
        styles += [int(_attr(item, "numFmtId") or 0) for item in _children(cell_styles, "xf")]

    def is_date(number: int) -> bool:
        if number in _EXCEL_DATES:
            return True
        # 自定义格式：去掉引号里的文字、[红色] 这样的方括号和转义的字符，剩下的里面有年月日时分秒就是日期
        code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", custom.get(number, ""))
        return bool(re.search(r"[ymdhs]", code, re.IGNORECASE)) and code.lower() != "general"

    return {index for index, number in enumerate(styles) if is_date(number)}


def _sheet_rows(package: _Package, path: str, shared: list[str], dates: set[int], date1904: bool, room: int) -> list[str]:
    """一张工作表的每一行，格子之间用制表符隔开。整张表最左边的空列去掉，中间的空格子保留（位置才对得上）。"""
    data = package.read(path)
    rows: list[dict[int, str]] = []
    used = 0
    for _, element in ElementTree.iterparse(io.BytesIO(data)):  # 边读边丢，大表也不会占太多内存
        if _local(element.tag) != "row":
            continue
        cells: dict[int, str] = {}
        column = -1
        for cell in _children(element, "c"):
            reference = _attr(cell, "r")
            column = _column(reference) if reference else column + 1
            value = _cell_value(cell, shared, dates, date1904)
            if value:
                cells[column] = value
                used += len(value) + 1
        element.clear()
        if cells:
            rows.append(cells)
        if used > room:
            raise DocumentError(_too_long(package.name))
    if not rows:
        return []
    first = min(min(cells) for cells in rows)
    return ["\t".join(cells.get(column, "") for column in range(first, max(cells) + 1)) for cells in rows]


def _column(reference: str) -> int:
    """单元格地址里的列：A → 0，Z → 25，AA → 26。"""
    number = 0
    letters = re.match(r"[A-Za-z]*", reference)
    assert letters is not None  # * 连一个字母都没有也算对上（得到空串），所以一定有结果
    for letter in letters.group(0).upper():
        number = number * 26 + ord(letter) - ord("A") + 1
    return max(number - 1, 0)


def _cell_value(cell: ElementTree.Element, shared: list[str], dates: set[int], date1904: bool) -> str:
    kind = _attr(cell, "t") or "n"
    raw = next((child.text or "" for child in cell if _local(child.tag) == "v"), "")
    if kind == "s":
        value = shared[int(raw)] if raw.isdigit() and int(raw) < len(shared) else ""
    elif kind == "inlineStr":
        value = "".join(_cell_text(item) for item in _children(cell, "is"))
    elif kind == "b":
        value = "TRUE" if raw == "1" else "FALSE" if raw == "0" else raw
    elif kind in ("str", "e", "d"):
        value = raw  # 公式算出的文字、错误（#DIV/0!）、ISO 格式写好的日期
    else:
        try:
            number = float(raw)
        except ValueError:
            value = raw
        else:
            style = _attr(cell, "s")
            is_date = style.isdigit() and int(style) in dates
            value = _excel_date(number, date1904) if is_date else _number(number)
    return " ".join(value.split()) if "\t" in value or "\n" in value else value.strip()


def _number(number: float) -> str:
    """按人读的样子写数字：12.0 写成 12，0.30000000000000004 写成 0.3（Excel 显示的也是这样）。"""
    if number.is_integer() and abs(number) < 1e15:
        return str(int(number))
    return f"{number:.15g}"


def _excel_date(serial: float, date1904: bool) -> str:
    """Excel 的日期数字 → 2026-11-20、2026-11-20 14:30 或 14:30。"""
    base = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)
    try:
        moment = base + timedelta(seconds=round(serial * 86400))
    except OverflowError:
        return _number(serial)
    clock = moment.strftime("%H:%M:%S" if moment.second else "%H:%M")
    if 0 <= serial < 1:
        return clock  # 只有时间
    if moment.hour == moment.minute == moment.second == 0:
        return moment.strftime("%Y-%m-%d")
    return moment.strftime("%Y-%m-%d ") + clock


# ---- PowerPoint -------------------------------------------------------------


def _slides(package: _Package, main: str) -> str:
    slides = package.rels(main)
    order = [_relation_id(item) for item in package.xml(main).iter() if _local(item.tag) == "sldId"]
    sections = []
    for number, relation in enumerate(order, 1):
        kind, path = slides.get(relation, ("", ""))
        if not kind.endswith("/slide") or path not in package:
            continue
        lines = _paragraphs(package.xml(path))
        notes_path = package.related(path, "notesSlide")
        notes = _notes(package.xml(notes_path)) if notes_path else []
        if notes:
            lines.append("备注：" + "\n".join(notes))
        sections.append(f"## 幻灯片 {number}\n" + ("\n".join(lines) if lines else "（这一页没有文字）"))
    if all(section.endswith("（这一页没有文字）") for section in sections):
        return ""  # 一个字都没有，当作"没有找到文字"
    return "\n\n".join(sections)


def _paragraphs(root: ElementTree.Element) -> list[str]:
    """幻灯片里每个文本框、表格格子的每一段，按它们在文件里的顺序。"""
    lines = []
    for paragraph in root.iter():
        if _local(paragraph.tag) != "p" or "drawingml" not in paragraph.tag:  # 只要 a:p（DrawingML 的段落）
            continue
        text = "".join(
            (piece.text or "") if _local(piece.tag) == "t" else "\n" for piece in paragraph.iter() if _local(piece.tag) in ("t", "br")
        ).strip()
        if text:
            lines.append(text)
    return lines


def _notes(root: ElementTree.Element) -> list[str]:
    """备注页上"正文"那个框里的字。备注页上还有页码和幻灯片的缩略图，那些不要。"""
    lines = []
    for shape in root.iter():
        if _local(shape.tag) != "sp":
            continue
        placeholder = next((item for item in shape.iter() if _local(item.tag) == "ph"), None)
        if placeholder is not None and _attr(placeholder, "type") == "body":
            lines += _paragraphs(shape)
    return lines
