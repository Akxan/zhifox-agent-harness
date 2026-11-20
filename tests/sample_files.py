"""测试里用的附件：几个最小的、格式正确的文件，写成浏览器发来的样子（见 harness/attachments.py）。"""

from __future__ import annotations

import base64

# 一张 1×1 像素的 PNG（Claude 文档里的示例图片）
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
PNG_BYTES = base64.b64decode(PNG)
# 服务器只看文件开头的几个字节来认格式，所以 JPEG 只要有个开头就够了
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
PDF_BYTES = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PDF = base64.b64encode(PDF_BYTES).decode()
THUMB = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQ=="  # 页面生成的小预览图


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def image(name: str = "截图.png", data: str = PNG, media_type: str = "image/png", **extra) -> dict:
    return {"kind": "image", "name": name, "media_type": media_type, "data": data, **extra}


def pdf(name: str = "报告.pdf", data: str = PDF, **extra) -> dict:
    return {"kind": "pdf", "name": name, "media_type": "application/pdf", "data": data, **extra}


def document(name: str = "周报.docx", content: bytes = b"", **extra) -> dict:
    """Word、Excel、PowerPoint 文件：浏览器原样编码成 base64 发过来，由助手程序取出文字。"""
    return {"kind": "document", "name": name, "media_type": "application/octet-stream", "data": b64(content), **extra}


def text_file(name: str = "笔记.md", text: str = "第一行\n第二行", **extra) -> dict:
    # 文本文件的 media_type 浏览器说什么都行，服务器不看
    return {"kind": "text", "name": name, "media_type": "text/markdown", "text": text, **extra}


# ---- Office 文件 ------------------------------------------------------------
# .docx、.xlsx、.pptx 都是 zip 压缩包，里面是几份 XML。这里只放取文字时用得到的那几份，
# 结构（文件名、关系表、命名空间）和 Word、Excel、PowerPoint 存出来的一样

W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
R_NS = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
A_NS = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
P_NS = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
S_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def zip_of(parts: dict[str, str | bytes]) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def rels(*targets: tuple[str, str, str]) -> str:
    """关系表。每一项是（编号，类型的最后一段，目标路径）。"""
    items = "".join(f'<Relationship Id="{rid}" Type="{REL}/{kind}" Target="{target}"/>' for rid, kind, target in targets)
    return f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{items}</Relationships>'


def docx(body: str, styles: str = "") -> bytes:
    parts = {
        "_rels/.rels": rels(("rId1", "officeDocument", "word/document.xml")),
        "word/document.xml": f'<?xml version="1.0" encoding="UTF-8"?><w:document {W_NS} {R_NS}><w:body>{body}</w:body></w:document>',
    }
    if styles:
        parts["word/_rels/document.xml.rels"] = rels(("rId1", "styles", "styles.xml"))
        parts["word/styles.xml"] = f'<?xml version="1.0" encoding="UTF-8"?><w:styles {W_NS}>{styles}</w:styles>'
    return zip_of(parts)


def xlsx(sheets: list[tuple[str, str]], shared: list[str] = (), styles: str = "", date1904: bool = False) -> bytes:
    """sheets：（工作表名，<sheetData> 里的内容）。"""
    entries = "".join(f'<sheet name="{name}" sheetId="{n}" r:id="rId{n}"/>' for n, (name, _) in enumerate(sheets, 1))
    pr = '<workbookPr date1904="1"/>' if date1904 else ""
    targets = [(f"rId{n}", "worksheet", f"worksheets/sheet{n}.xml") for n in range(1, len(sheets) + 1)]
    targets += [("rIdS", "sharedStrings", "sharedStrings.xml"), ("rIdT", "styles", "styles.xml")]
    parts = {
        "_rels/.rels": rels(("rId1", "officeDocument", "xl/workbook.xml")),
        "xl/workbook.xml": f'<?xml version="1.0" encoding="UTF-8"?><workbook {S_NS} {R_NS}>{pr}<sheets>{entries}</sheets></workbook>',
        "xl/_rels/workbook.xml.rels": rels(*targets),
        "xl/sharedStrings.xml": f'<?xml version="1.0" encoding="UTF-8"?><sst {S_NS}>' + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>",
        "xl/styles.xml": f'<?xml version="1.0" encoding="UTF-8"?><styleSheet {S_NS}>{styles}</styleSheet>',
    }
    for n, (_, data) in enumerate(sheets, 1):
        parts[f"xl/worksheets/sheet{n}.xml"] = f'<?xml version="1.0" encoding="UTF-8"?><worksheet {S_NS}><sheetData>{data}</sheetData></worksheet>'
    return zip_of(parts)


def slide_xml(*paragraphs: str, placeholder: str = "") -> str:
    ph = f'<p:nvSpPr><p:nvPr><p:ph type="{placeholder}"/></p:nvPr></p:nvSpPr>' if placeholder else ""
    body = "".join(f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>" for text in paragraphs)
    return f'<p:sp>{ph}<p:txBody>{body}</p:txBody></p:sp>'


def pptx(slides: list[str], notes: dict[int, str] | None = None, order: list[int] | None = None) -> bytes:
    """slides：每张幻灯片里的形状（slide_xml 的结果）。order：播放顺序（幻灯片的编号，从 1 开始），默认按顺序。"""
    order = order or list(range(1, len(slides) + 1))
    ids = "".join(f'<p:sldId id="{255 + n}" r:id="rId{n}"/>' for n in order)
    parts = {
        "_rels/.rels": rels(("rId1", "officeDocument", "ppt/presentation.xml")),
        "ppt/presentation.xml": f'<?xml version="1.0" encoding="UTF-8"?><p:presentation {P_NS} {R_NS}><p:sldIdLst>{ids}</p:sldIdLst></p:presentation>',
        "ppt/_rels/presentation.xml.rels": rels(*[(f"rId{n}", "slide", f"slides/slide{n}.xml") for n in range(1, len(slides) + 1)]),
    }
    for n, shapes in enumerate(slides, 1):
        parts[f"ppt/slides/slide{n}.xml"] = f'<?xml version="1.0" encoding="UTF-8"?><p:sld {P_NS} {A_NS}><p:cSld><p:spTree>{shapes}</p:spTree></p:cSld></p:sld>'
        if notes and n in notes:
            parts[f"ppt/slides/_rels/slide{n}.xml.rels"] = rels(("rId1", "notesSlide", f"../notesSlides/notesSlide{n}.xml"))
            shapes_n = slide_xml("3", placeholder="sldNum") + slide_xml(notes[n], placeholder="body")
            parts[f"ppt/notesSlides/notesSlide{n}.xml"] = f'<?xml version="1.0" encoding="UTF-8"?><p:notes {P_NS} {A_NS}><p:cSld><p:spTree>{shapes_n}</p:spTree></p:cSld></p:notes>'
    return zip_of(parts)
