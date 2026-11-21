"""从 Word、Excel、PowerPoint 文件里取文字（harness/documents.py）。

测试用的文件是现拼的（见 sample_files.py 的 docx、xlsx、pptx）：结构和 Office 存出来的一样，
但只放了取文字用得到的那几份 XML，每个测试一眼就能看出文件里有什么、应该取出什么。
"""

from __future__ import annotations

import pytest
from sample_files import (
    PDF_BYTES,
    W_NS,
    b64,
    document,
    docx,
    pdf,
    pptx,
    rels,
    slide_xml,
    xlsx,
    zip_of,
)

from harness import documents
from harness.attachments import AttachmentError, parse, wrap_text
from harness.documents import NOTE, DocumentError, extract_text


def para(text: str, style: str = "", listed: bool = False, outline: int | None = None) -> str:
    settings = f'<w:pStyle w:val="{style}"/>' if style else ""
    settings += '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>' if listed else ""
    settings += f'<w:outlineLvl w:val="{outline}"/>' if outline is not None else ""
    # 段落设置里也有一个 <w:tab>（制表位的定义），它不是正文里的制表符，不能当成文字
    settings += '<w:tabs><w:tab w:val="left" w:pos="720"/></w:tabs>'
    return f"<w:p><w:pPr>{settings}</w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>"


def cell(text: str) -> str:
    return f"<w:tc><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>"


# ---- Word -------------------------------------------------------------------


def test_word_keeps_headings_lists_and_tables():
    # 中文版 Word 里"标题 1"的样式编号就是 "1"，所以按样式的名字（heading 1）认，不按编号
    styles = (
        '<w:style w:type="paragraph" w:styleId="1"><w:name w:val="heading 1"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="a3"><w:name w:val="Title"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/></w:style>'
    )
    table = f"<w:tbl><w:tr>{cell('名称')}{cell('数量')}</w:tr><w:tr>{cell('苹果')}{cell('3')}</w:tr></w:tbl>"
    body = (
        para("年度计划", "a3")
        + para("第一季度", "1")
        + para("大纲级别写在段落上的", outline=2)
        + para("招两个人", listed=True)
        + para("一段引用", "Quote")
        + table
        + para("完")
    )
    assert extract_text("计划.docx", docx(body, styles)) == (
        "# 年度计划\n# 第一季度\n### 大纲级别写在段落上的\n- 招两个人\n一段引用\n\n名称 | 数量\n苹果 | 3\n\n完"
    )


def test_word_text_skips_what_is_not_on_the_page():
    body = (
        "<w:p><w:r><w:t>保留</w:t></w:r>"
        "<w:del><w:r><w:delText>修订时删掉的</w:delText></w:r></w:del>"
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>PAGE</w:instrText></w:r>'
        "<w:r><w:tab/><w:t>制表符</w:t><w:br/><w:t>换行</w:t></w:r>"
        "<w:ins><w:r><w:t>修订时加上的</w:t></w:r></w:ins></w:p>"
        # 文本框在文件里写了两遍（新旧两种写法），只取一遍
        '<w:p><w:r><mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
        "<mc:Choice><w:txbxContent><w:p><w:r><w:t>文本框</w:t></w:r></w:p></w:txbxContent></mc:Choice>"
        "<mc:Fallback><w:txbxContent><w:p><w:r><w:t>文本框</w:t></w:r></w:p></w:txbxContent></mc:Fallback>"
        "</mc:AlternateContent></w:r></w:p>"
        # 内容控件包着的段落照常算
        "<w:sdt><w:sdtPr/><w:sdtContent>" + para("控件里的字") + "</w:sdtContent></w:sdt>"
    )
    assert extract_text("a.docx", docx(body)) == "保留\t制表符\n换行修订时加上的\n文本框\n控件里的字"


def test_word_saved_as_strict_open_xml_is_read_too():
    # "严格 Open XML"格式换了命名空间，标签名不变
    strict = W_NS.replace("http://schemas.openxmlformats.org/wordprocessingml/2006/main", "http://purl.oclc.org/ooxml/wordprocessingml/main")
    content = zip_of(
        {
            "_rels/.rels": rels(("rId1", "officeDocument", "word/document.xml")),
            "word/document.xml": f"<w:document {strict}><w:body><w:p><w:r><w:t>严格格式</w:t></w:r></w:p></w:body></w:document>",
        }
    )
    assert extract_text("a.docx", content) == "严格格式"


def test_the_main_part_is_found_without_the_package_relations():
    content = zip_of({"word/document.xml": f"<w:document {W_NS}><w:body>{para('没有关系表')}</w:body></w:document>"})
    assert extract_text("a.docx", content) == "没有关系表"


# ---- Excel ------------------------------------------------------------------

DATE_STYLES = (
    '<numFmts count="2"><numFmt numFmtId="164" formatCode="yyyy&quot;年&quot;m&quot;月&quot;d&quot;日&quot;"/>'
    '<numFmt numFmtId="165" formatCode="0.0&quot; days&quot;"/></numFmts>'
    '<cellXfs><xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="164"/><xf numFmtId="165"/><xf numFmtId="20"/></cellXfs>'
)


def test_excel_rows_become_tab_separated_lines_sheet_by_sheet():
    sheet1 = (
        '<row r="2"><c r="B2" t="s"><v>0</v></c><c r="C2" t="s"><v>1</v></c><c r="E2" t="s"><v>2</v></c></row>'
        '<row r="3"><c r="B3" t="inlineStr"><is><t>周一</t></is></c><c r="C3"><v>12</v></c>'
        '<c r="D3"><v>0.30000000000000004</v></c><c r="E3" t="b"><v>1</v></c></row>'
        '<row r="4"><c r="B4" t="e"><v>#DIV/0!</v></c><c r="C4" t="str"><f>A1&amp;B1</f><v>公式的结果</v></c></row>'
        '<row r="5"><c r="B5"><v></v></c></row>'  # 空格子、空行都不写
    )
    sheet2 = '<row r="1"><c t="s"><v>3</v></c><c t="s"><v>4</v></c></row>'  # 没写地址的格子按顺序排
    content = xlsx([("预算", sheet1), ("备注", sheet2), ("空表", "")], ["项目", "金额", "完成", "第一列", "第二列"])
    assert extract_text("预算.xlsx", content) == (
        # 最左边整列空着的 A 列去掉；中间空着的 D2 留一个空位，后面的格子才对得上表头
        "## 工作表：预算\n项目\t金额\t\t完成\n周一\t12\t0.3\tTRUE\n#DIV/0!\t公式的结果\n\n## 工作表：备注\n第一列\t第二列"
    )


def test_excel_dates_are_written_as_dates():
    row = (
        '<row r="1"><c r="A1" s="1"><v>46346</v></c><c r="B1" s="2"><v>46346.5</v></c>'
        '<c r="C1" s="3"><v>2.5</v></c><c r="D1" s="4"><v>0.75</v></c><c r="E1" s="9"><v>46346</v></c></row>'
    )
    # 内置的日期格式（14）、自定义的"yyyy年m月d日"；引号里的 days 不算日期格式；只有时间的（20）；没有的样式编号当普通数字
    assert extract_text("a.xlsx", xlsx([("表", row)], styles=DATE_STYLES)) == "## 工作表：表\n2026-11-20\t2026-11-20 12:00\t2.5\t18:00\t46346"
    # 1904 年起算的工作簿（老的 Mac 版 Excel）
    assert extract_text("a.xlsx", xlsx([("表", '<row r="1"><c r="A1" s="1"><v>44884</v></c></row>')], styles=DATE_STYLES, date1904=True)) == (
        "## 工作表：表\n2026-11-20"
    )


def test_excel_shared_strings_join_rich_text_and_skip_phonetic_guides():
    content = xlsx([("表", '<row r="1"><c r="A1" t="s"><v>0</v></c></row>')])
    sst = (
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si>'
        "<r><t>东京</t></r><r><rPr><b/></rPr><t>都</t></r><rPh><t>とうきょう</t></rPh></si></sst>"
    )
    content = zip_of({**unzip(content), "xl/sharedStrings.xml": sst})
    assert extract_text("a.xlsx", content) == "## 工作表：表\n东京都"


def unzip(content: bytes) -> dict[str, bytes]:
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


# ---- PowerPoint -------------------------------------------------------------


def test_slides_follow_the_playback_order_with_their_notes():
    slides = [slide_xml("第一张做的", "要点"), slide_xml("第二张做的"), ""]
    content = pptx(slides, notes={1: "讲的时候慢一点"}, order=[2, 1, 3])
    assert extract_text("a.pptx", content) == (
        # 备注页上的页码（3）不算备注
        "## 幻灯片 1\n第二张做的\n\n## 幻灯片 2\n第一张做的\n要点\n备注：讲的时候慢一点\n\n## 幻灯片 3\n（这一页没有文字）"
    )


# ---- 读不了的 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "error"),
    [
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(100), "可能设了密码，或者是旧版的 Office 格式"),
        (b"just some text", "不是有效的 Word、Excel 或 PowerPoint 文件"),
        (zip_of({"hello.txt": "不是 Office 文件"}), "不是有效的 Word、Excel 或 PowerPoint 文件"),
        (docx(para("截断了"))[:60], "文件可能损坏了"),
        (zip_of({"word/document.xml": "<w:document"}), "文件可能损坏了"),  # XML 写到一半
        (docx("<w:p><w:r><w:drawing/></w:r></w:p>"), "没有找到文字（可能只有图片）"),
        (pptx(["", ""]), "没有找到文字（可能只有图片）"),
    ],
    # 每一种情况起一个固定的名字。不起的话 pytest 拿内容本身当名字，而 zip 里记着打包的时刻：
    # 并行跑测试时（pytest -n），几个进程相差一秒打包，名字就对不上，整轮测试跑不起来
    ids=["旧版或加密的 Office", "纯文字", "不是 Office 的 zip", "截断的 docx", "写到一半的 XML", "只有图片的 docx", "只有图片的 pptx"],
)
def test_files_that_cannot_be_read_say_why(content, error):
    with pytest.raises(DocumentError, match=error):
        extract_text("周报.docx", content)


def test_xml_with_entity_definitions_is_refused():
    # "十亿笑声"：几行实体定义展开成几 GB。Office 文件从来不用 DTD
    bomb = '<?xml version="1.0"?><!DOCTYPE w [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
    content = zip_of({"word/document.xml": bomb + f"<w:document {W_NS}><w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p></w:body></w:document>"})
    with pytest.raises(DocumentError, match="文件可能损坏了"):
        extract_text("a.docx", content)


def test_a_decompression_bomb_is_stopped_at_the_limit(monkeypatch):
    # 压缩后很小、解压后很大。只读到上限多一个字节就停，不会把整份解压出来
    monkeypatch.setattr(documents, "MAX_PART_BYTES", 10_000)
    content = docx(para("字" * 20_000))
    assert len(content) < 3_000
    with pytest.raises(DocumentError, match="文字太多了"):
        extract_text("a.docx", content)


def test_too_much_text_is_refused(monkeypatch):
    monkeypatch.setattr(documents, "MAX_TEXT_CHARS", 100)
    with pytest.raises(DocumentError, match="超过 40 万个字符"):
        extract_text("a.docx", docx(para("字" * 101)))
    rows = "".join(f'<row r="{n}"><c r="A{n}" t="inlineStr"><is><t>{"字" * 30}</t></is></c></row>' for n in range(1, 10))
    with pytest.raises(DocumentError, match="超过 40 万个字符"):
        extract_text("a.xlsx", xlsx([("表", rows)]))


# ---- 和附件的检查连起来 -----------------------------------------------------


def test_an_office_file_becomes_a_text_attachment_with_a_note():
    (item,) = parse([document("周报.docx", docx(para("口令是 ORCHID-913")))])
    assert (item.kind, item.name, item.text, item.size, item.note) == ("text", "周报.docx", "口令是 ORCHID-913", len("口令是 ORCHID-913"), NOTE)
    assert wrap_text(item) == f'<attachment name="周报.docx" note="{NOTE}">\n口令是 ORCHID-913\n</attachment>'
    assert item.summary() == {"kind": "text", "name": "周报.docx", "media_type": "text/plain", "size": item.size}


def test_office_attachment_errors_come_back_to_the_page():
    with pytest.raises(AttachmentError, match="不是有效的 Word、Excel 或 PowerPoint 文件"):
        parse([document("周报.docx", b"just a renamed text file")])
    with pytest.raises(AttachmentError, match="是空的"):
        parse([document("周报.docx", b"")])
    with pytest.raises(AttachmentError, match="Word、Excel、PowerPoint 文件最大 25 MB"):
        parse([document("大.pptx", b"PK" + bytes(documents.MAX_DOCUMENT_BYTES - 1))])


def test_a_pdf_with_a_password_is_refused_with_a_way_out():
    locked = PDF_BYTES.replace(b"trailer<<>>", b"trailer<</Encrypt 5 0 R>>")
    with pytest.raises(AttachmentError) as raised:
        parse([pdf("合同.pdf", data=b64(locked))])
    assert str(raised.value) == "PDF“合同.pdf”设了密码或者加了限制，模型打不开。去掉密码、另存一份再发。"
