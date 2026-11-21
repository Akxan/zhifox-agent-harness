"""多媒体库的测试：原件怎样存（内容寻址、去重、权限），索引里的每一项对应页面上的哪一张卡片，
旧会话的附件怎样从对话历史里找回来，列表的筛选、搜索和翻页，下载时用什么名字，以及没人用的原件什么时候删。

网页上的接口（/api/media/list、/api/media/file）在 test_webui.py 的"多媒体"一节里测。
"""

from __future__ import annotations

import hashlib
import stat
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from urllib.parse import unquote

import pytest
from sample_files import (
    JPEG_BYTES,
    PDF_BYTES,
    PNG_BYTES,
    THUMB,
    b64,
    document,
    docx,
    image,
    pdf,
    text_file,
    xlsx,
)

from harness import media
from harness.attachments import Attachment, from_saved, parse
from harness.conversations import ConversationStore
from harness.media import (
    ID,
    OFFICE_TYPES,
    Entry,
    MediaError,
    MediaIndex,
    MediaStore,
    content_of,
    disposition,
    entries_of,
    listing,
)
from harness.providers.claude import AnthropicBackend
from harness.providers.openai_compat import OpenAICompatBackend
from harness.webui.ui import WebUI

CONVERSATION = "20261120-140359-a1b2c3"
OTHER = "20261121-090000-d4e5f6"
TIME_NOTE = "<context>当前时间：2026-11-20 14:03 星期五</context>"
SHEET = '<row r="1"><c r="A1" t="inlineStr"><is><t>预算</t></is></c><c r="B1"><v>1284.5</v></c></row>'
WORKBOOK = xlsx([("第一季度", SHEET)])
WORDS = "## 工作表：第一季度\n预算\t1284.5"  # 从 WORKBOOK 里取出的文字（见 test_documents.py）
REPORT = docx("<w:p><w:r><w:t>本周完成了多媒体库</w:t></w:r></w:p>")
NOTES = "第一行\n第二行"  # sample_files.text_file 的默认内容
# 和 PNG_BYTES 一样长、开头一样的另一张"图片"：服务器只看开头几个字节认格式，大小又一样，只有内容能分出它们
OTHER_PNG = PNG_BYTES[:8] + bytes(len(PNG_BYTES) - 8)


# ---- 搭积木用的小函数 -----------------------------------------------------


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def checked(*raw: dict) -> list[Attachment]:
    """浏览器送来的附件，经过和网页服务器一样的检查（attachments.parse）。"""
    return parse(list(raw))


def sent(*attachments: Attachment, at: object = None, text: str = "") -> dict:
    """页面上的一条用户消息，和 WebUI.begin_turn 发出、存进会话文件的一样。at 为 None 表示有这个功能之前的会话，没记时间。"""
    event = {"type": "user", "text": text, "attachments": [item.summary() for item in attachments]}
    if at is not None:
        event["at"] = at
    return event


def history(provider: str, *turns: Sequence[Attachment], words: str = "看看") -> list[dict]:
    """用真实的适配层拼出对话历史（存进会话文件的样子）。每一轮是用户的一条消息（带着这些附件）和助手的一句回答。

    这样测的就是两家真实的格式（Claude 的 image / document 块，OpenAI 那一套的 image_url / file 块），
    而不是测试里自己想象的格式。
    """
    if provider == "claude":
        backend = AnthropicBackend(None, "claude-opus-5-5", None, [], "系统提示词")
        answer = {"role": "assistant", "content": [{"type": "text", "text": "好的。"}]}
    else:
        backend = OpenAICompatBackend(None, "openai", "GPT", "gpt-6-luna", None, [], "系统提示词", vision=True, pdf=True)
        answer = {"role": "assistant", "content": "好的。"}
    for attachments in turns:
        backend.add_user([TIME_NOTE, words], attachments)
        backend.messages.append(dict(answer))
    return backend.export()


def entry(name: str, kind: str = "image", **fields) -> Entry:
    """手写的一个条目。没给的字段用一个合理的默认值。"""
    media_type = {"image": "image/png", "pdf": "application/pdf"}.get(kind, "text/plain")
    values = {"turn": 0, "index": 0, "at": 100.0, "kind": kind, "name": name, "media_type": media_type, "size": 10}
    return Entry(**{**values, **fields})


def names(page: dict) -> list[str]:
    return [item["name"] for item in page["items"]]


def no_history() -> list:
    pytest.fail("有原件的附件不该去读对话历史（会话文件可能很大）")


# ---- 原件：存、去重、权限 ---------------------------------------------------


def test_originals_are_stored_once_under_the_hash_of_their_content(tmp_path, monkeypatch):
    store = MediaStore(tmp_path / "data" / "media")
    items = checked(
        image(thumb=THUMB),
        image("拍的照片.png", data=b64(JPEG_BYTES)),  # 扩展名说是 PNG，内容其实是 JPEG：按内容存成 .jpg
        pdf(),
        text_file("代码.py", "print('你好')"),
        document("周报.docx", REPORT),
        document("预算.XLSX", WORKBOOK),  # 扩展名大写也认得
    )
    kept, problem = store.keep(items)

    assert problem == ""
    # 文件名是内容的 SHA-256：图片和 PDF 是解码后的字节，文本是 UTF-8 的文字，Word、Excel 是原样的文件
    originals = {
        sha(PNG_BYTES): ("png", PNG_BYTES),
        sha(JPEG_BYTES): ("jpg", JPEG_BYTES),
        sha(PDF_BYTES): ("pdf", PDF_BYTES),
        sha("print('你好')".encode()): ("txt", "print('你好')".encode()),
        sha(REPORT): ("docx", REPORT),
        sha(WORKBOOK): ("xlsx", WORKBOOK),
    }
    assert [item.sha256 for item in kept] == list(originals)
    assert sorted(path.name for path in store.directory.iterdir()) == sorted(f"{d}.{s}" for d, (s, _) in originals.items())
    for digest, (suffix, content) in originals.items():
        path = store.directory / f"{digest}.{suffix}"
        assert path.read_bytes() == content
        assert mode(path) == 0o600  # 和会话一样是私人的内容：只有自己的账号能读
        assert store.find(digest) == path
    assert mode(store.directory) == 0o700 and mode(store.directory.parent) == 0o700
    assert store.stored() == {digest: (store.directory / f"{digest}.{s}", len(c)) for digest, (s, c) in originals.items()}

    # 记上了编号，别的都没变；Office 的原件已经在磁盘上，内存里不再拿着（一个 Word 文件可能有 25 MB）
    assert kept == [replace(item, sha256=digest) for item, digest in zip(items, originals)]
    assert items[4].original == REPORT and all(item.original == b"" for item in kept)
    assert [item.summary()["sha256"] for item in kept] == list(originals)

    # 同样的内容再发一次（换了名字也一样）：不再写一遍，编号和文件都是原来那个
    writes = []
    monkeypatch.setattr(media, "write_file", lambda path, content: writes.append(path))
    (again,), problem = store.keep(checked(image("另一个名字.png")))
    assert writes == [] and problem == ""
    assert again.sha256 == sha(PNG_BYTES) and store.find(again.sha256) == store.directory / f"{sha(PNG_BYTES)}.png"
    assert len(list(store.directory.iterdir())) == len(originals)


def test_an_office_file_with_an_unknown_suffix_is_kept_as_plain_bytes(tmp_path):
    # 文字是按内容取出来的，名字说什么都行（正常的页面发不出这种）。原件照样留着，
    # 只是存成 .bin、按 application/octet-stream 回答：不认识的扩展名不去猜它是什么
    store = MediaStore(tmp_path / "media")
    (kept,), _ = store.keep(checked(document("周报.odt", REPORT)))
    path = store.find(kept.sha256)
    assert path == store.directory / f"{sha(REPORT)}.bin" and path.read_bytes() == REPORT

    (found,) = entries_of([sent(kept, at=5.0)], [], 1.0)
    assert content_of(found, store, no_history) == (REPORT, "application/octet-stream", "周报.odt")


def test_originals_that_cannot_be_stored_do_not_stop_the_message(tmp_path, monkeypatch):
    # 存不成（磁盘满了、没有权限）不该挡住这条消息：没存成的照原样返回、不带编号，原因交给调用的人去提醒
    (tmp_path / "media").write_text("这里本该是个目录", encoding="utf-8")
    store = MediaStore(tmp_path / "media")
    items = checked(image(), document("周报.docx", REPORT))
    kept, problem = store.keep(items)
    assert problem  # 说得出原因
    assert kept == items and [item.sha256 for item in kept] == ["", ""]
    assert all("sha256" not in item.summary() for item in kept)

    # 只有一部分没存上：存上的有编号，没存上的照原样，原因是第一个出错的那个
    store = MediaStore(tmp_path / "ok")
    real, calls = media.write_file, []

    def flaky(path, content):
        calls.append(path)
        if len(calls) == 2:
            raise OSError(28, "No space left on device")
        real(path, content)

    monkeypatch.setattr(media, "write_file", flaky)
    kept, problem = store.keep(checked(image(), pdf(), text_file()))
    assert [bool(item.sha256) for item in kept] == [True, False, True]
    assert "No space left on device" in problem
    assert sorted(store.stored()) == sorted([sha(PNG_BYTES), sha(NOTES.encode())])


def test_the_store_only_knows_files_it_could_have_written(tmp_path):
    store = MediaStore(tmp_path / "media")
    assert store.stored() == {} and store.find(sha(PNG_BYTES)) is None  # 还没存过任何东西，目录都还没有
    (kept,), _ = store.keep(checked(image()))
    digest = kept.sha256
    for stranger in ("笔记.txt", f"{digest.upper()}.png", f"{digest}.exe", f"{digest}.png.1a2b3c4d.tmp", f"{digest[:-1]}.png"):
        (store.directory / stranger).write_bytes(b"x")
    assert list(store.stored()) == [digest]
    # 编号只能是 64 个十六进制字符：拼不出别的路径
    for bad in ("", "../media/" + digest, digest.upper(), digest[:-1], f"{digest}\n", "/etc/passwd"):
        assert store.find(bad) is None, bad


def test_the_hash_travels_in_the_summary_and_only_a_real_hash_is_kept():
    digest = sha(PNG_BYTES)
    (plain,) = checked(image())
    assert "sha256" not in plain.summary()
    assert replace(plain, sha256=digest).summary()["sha256"] == digest

    # 会话文件可能被改过：编号不是 64 个小写的十六进制字符的，就当没有（这一项还在，只是成了"旧附件"）
    base = {"kind": "image", "name": "a.png", "media_type": "image/png", "size": 3}
    assert from_saved([{**base, "sha256": digest}]) == [{**base, "sha256": digest}]
    assert from_saved([{**base, "sha256": digest, "preview": THUMB}], previews=False) == [{**base, "sha256": digest}]
    for bogus in (digest.upper(), digest[:-1], digest + "0", "../" + digest[3:], f"{digest}\n", "g" * 64, 5, None, [digest]):
        assert from_saved([{**base, "sha256": bogus}]) == [base], bogus


# ---- 索引：一段会话里有哪些附件 ---------------------------------------------


def test_new_attachments_are_indexed_from_their_summaries_alone(tmp_path):
    store = MediaStore(tmp_path / "media")
    kept, _ = store.keep(checked(image(thumb=THUMB), pdf(), text_file(), document("预算.xlsx", WORKBOOK)))
    events = [
        {"type": "user", "text": "只是说话"},  # 没带附件的消息不算一排
        sent(*kept[:2], at=1000.5),
        {"type": "text", "text": "回答"},
        sent(*kept[2:], at=2000.0),
    ]
    # 都有原件：用不着对话历史（给一份空的也一样）
    assert entries_of(events, [], started_at=1.0) == [
        Entry(0, 0, 1000.5, "image", "截图.png", "image/png", len(PNG_BYTES), thumb=THUMB, sha256=sha(PNG_BYTES)),
        Entry(0, 1, 1000.5, "pdf", "报告.pdf", "application/pdf", len(PDF_BYTES), sha256=sha(PDF_BYTES)),
        Entry(1, 0, 2000.0, "text", "笔记.md", "text/plain", len(NOTES), sha256=sha(NOTES.encode())),
        # Word、Excel 在概要里记成文本（交给模型的是取出的文字），在多媒体库里按扩展名归到"文档"
        Entry(1, 1, 2000.0, "document", "预算.xlsx", "text/plain", len(WORDS), sha256=sha(WORKBOOK)),
    ]

    # 时间不像样的（改坏了的文件），一律算在会话开始的时候
    for bad in (True, "1000", float("nan"), float("inf"), 0, -5, [1]):
        assert entries_of([sent(kept[0], at=bad)], [], started_at=7.0)[0].at == 7.0, bad
    assert entries_of([sent(kept[0])], [], started_at=7.0)[0].at == 7.0
    assert entries_of([sent(kept[0], at=3)], [], started_at=7.0)[0].at == 3.0


def test_rows_are_counted_the_way_the_page_counts_them():
    # 页面靠"第几排的第几张"找到附件在对话里的位置，所以哪些项算数必须和页面重放时一样：
    # 都用 from_saved 来筛，坏掉的项不占位置，一项都不剩的消息不算一排
    good = {"kind": "image", "name": "好的.png", "media_type": "image/png", "size": 3, "sha256": "a" * 64}
    later = {"kind": "pdf", "name": "后来的.pdf", "media_type": "application/pdf", "size": 4, "sha256": "b" * 64}
    broken = [
        {"kind": "exe", "name": "x.exe", "media_type": "x", "size": 1},
        {"kind": "image", "name": "坏的.png", "media_type": "image/png", "size": True},
        {"kind": "image", "name": "  /  ", "media_type": "image/png", "size": 1},
        "截图.png",
    ]
    events = [
        {"type": "user", "text": "全是坏的", "attachments": broken},
        {"type": "user", "text": "没带附件"},
        {"type": "user", "text": "空的", "attachments": []},
        {"type": "user", "text": "不是列表", "attachments": "截图.png"},
        {"type": "user", "text": "一坏一好", "attachments": [broken[0], good]},
        "不是事件",
        {"type": "user", "text": "", "attachments": [later]},
    ]
    entries = entries_of(events, [], 1.0)
    assert [(item.turn, item.index, item.name) for item in entries] == [(0, 0, "好的.png"), (1, 0, "后来的.pdf")]

    # 页面重放的就是 WebUI.load_events 发出来的事件：数一数带附件的消息，每一项都对得上
    ui = WebUI()
    ui.load_events(events)
    rows = [event["attachments"] for event in ui.events_after(0, timeout=0) if event["type"] == "user" and event.get("attachments")]
    assert len(rows) == 2
    for item in entries:
        assert rows[item.turn][item.index]["name"] == item.name


@pytest.mark.parametrize("provider", ["claude", "openai"])
def test_old_attachments_are_found_in_the_history(tmp_path, provider):
    # 有这个功能之前发的附件：概要里没有编号，原件也不在。图片、PDF 和文本的完整内容还在发给模型的历史里，
    # Word、Excel 只剩当时取出的文字。按用户排的顺序发（文本在前），历史里的顺序却是图片、PDF、文本：
    # 不能按位置硬对，要按种类、名字和大小找
    items = checked(
        text_file("代码.py", "print('你好')"),
        pdf(),
        image(thumb=THUMB),
        document("预算.xlsx", WORKBOOK),
        image("照片.heic", data=b64(JPEG_BYTES)),  # 页面把 HEIC 换成了 JPEG 才发的
    )
    events = [{"type": "user", "text": "先打个招呼"}, sent(*items, text="看看")]
    messages = history(provider, (), items)  # 第一轮只有文字，第二轮带着附件
    entries = entries_of(events, messages, started_at=42.0)

    # 历史里第三条（第 2 号）是带附件的那条消息。两家的块的顺序一样：图片、图片、PDF、文本、文本
    assert [(item.index, item.kind, item.name, item.source) for item in entries] == [
        (0, "text", "代码.py", (2, 3)),
        (1, "pdf", "报告.pdf", (2, 2)),
        (2, "image", "截图.png", (2, 0)),
        (3, "document", "预算.xlsx", (2, 4)),
        (4, "image", "照片.heic", (2, 1)),
    ]
    assert all(item.turn == 0 and item.at == 42.0 and item.sha256 == "" for item in entries)
    assert entries[2].thumb == THUMB

    # 内容原样取回来；下载用的名字跟着内容走
    store = MediaStore(tmp_path / "media")
    assert [content_of(item, store, lambda: messages) for item in entries] == [
        ("print('你好')".encode(), "text/plain; charset=utf-8", "代码.py"),
        (PDF_BYTES, "application/pdf", "报告.pdf"),
        (PNG_BYTES, "image/png", "截图.png"),
        (WORDS.encode(), "text/plain; charset=utf-8", "预算.xlsx.txt"),  # 原件不在了，只剩文字：一看名字就知道
        (JPEG_BYTES, "image/jpeg", "照片.jpg"),
    ]
    assert [item.source_bytes for item in entries] == [
        len("print('你好')".encode()), len(PDF_BYTES), len(PNG_BYTES), len(WORDS.encode()), len(JPEG_BYTES),
    ]


def test_a_turn_that_was_rolled_back_is_listed_but_has_no_content(tmp_path):
    # 一轮刚开始就出错（比如连不上 API）时，历史里的那条消息被撤回了，页面上的那条消息却还在
    lost = checked(text_file("丢了.txt", "没发出去"))
    kept = checked(image())
    events = [sent(*lost), sent(*kept)]
    messages = history("claude", kept)
    first, second = entries_of(events, messages, 1.0)
    assert (first.turn, first.name, first.source) == (0, "丢了.txt", None)
    assert (second.turn, second.name, second.source) == (1, "截图.png", (0, 0))

    page = listing([(CONVERSATION, "标题", [first, second])], {})
    assert {item["name"]: item["available"] for item in page["items"]} == {"丢了.txt": False, "截图.png": True}
    with pytest.raises(MediaError, match="没有保存下来"):
        content_of(first, MediaStore(tmp_path / "media"), lambda: messages)


def test_the_same_files_sent_again_after_a_failure_find_the_same_content(tmp_path):
    # 第一次出错被撤回了，又原样发了一次、成功了；后来又发了第三次。三次都能对到内容（反正内容是一样的）
    items = checked(image(), text_file())
    events = [sent(*items), sent(*items), sent(*items)]
    messages = history("claude", items, items)
    entries = entries_of(events, messages, 1.0)
    store = MediaStore(tmp_path / "media")
    assert [item.turn for item in entries] == [0, 0, 1, 1, 2, 2]
    assert [content_of(item, store, lambda: messages)[0] for item in entries] == [PNG_BYTES, NOTES.encode()] * 3


def test_a_rolled_back_turn_does_not_hide_the_files_of_the_turns_after_it(tmp_path):
    # 第一次发 x.txt 失败了（撤回），接着发了 y.txt，后来又把 x.txt 发了一次。
    # 撤回的那一排往后找，对到了第三排的内容（一样的内容，没问题），可中间那一排的 y.txt 也必须还能找到
    x, y = checked(text_file("x.txt", "xxx")), checked(text_file("y.txt", "yyy"))
    events = [sent(*x), sent(*y), sent(*x)]
    messages = history("claude", y, x)
    entries = entries_of(events, messages, 1.0)
    store = MediaStore(tmp_path / "media")
    assert [item.name for item in entries] == ["x.txt", "y.txt", "x.txt"]
    assert [item.source is not None for item in entries] == [True, True, True]
    assert [content_of(item, store, lambda: messages)[0] for item in entries] == [b"xxx", b"yyy", b"xxx"]


def test_when_only_some_originals_were_kept_the_rest_find_their_own_content(tmp_path, monkeypatch):
    # 存原件时磁盘满了：第一张存上了，第二张没有（见 MediaStore.keep）。第二张要从历史里取，
    # 而且取到的必须是它自己，不能是同一条消息里的另一张
    store = MediaStore(tmp_path / "media")
    items = checked(image("第一张.png"), image("第二张.png", data=b64(OTHER_PNG)))
    real = media.write_file

    def only_the_first(path, content):
        if content != PNG_BYTES:
            raise OSError("满了")
        real(path, content)

    monkeypatch.setattr(media, "write_file", only_the_first)
    kept, problem = store.keep(items)
    assert [bool(item.sha256) for item in kept] == [True, False] and problem == "满了"

    messages = history("claude", items)
    first, second = entries_of([sent(*kept)], messages, 1.0)
    assert content_of(first, store, no_history)[0] == PNG_BYTES
    assert second.source == (0, 1)
    assert content_of(second, store, lambda: messages) == (OTHER_PNG, "image/png", "第二张.png")


@pytest.mark.parametrize("provider", ["claude", "openai"])
def test_words_the_user_typed_are_never_taken_for_an_attachment(tmp_path, provider):
    # 用户自己在消息里贴了一段 <attachment> 标签。它在时间说明（<context>）之后，是用户说的话，不是附件：
    # 哪怕前面有一排被撤回的附件，名字和大小正好对得上，也不能把用户的话当成那个文件的内容
    typed = '<attachment name="密码.txt">\nhunter2\n</attachment>'
    lost = checked(text_file("密码.txt", "hunter2"))
    shot = checked(image())
    messages = history(provider, shot, words=typed)
    first, second = entries_of([sent(*lost), sent(*shot, text=typed)], messages, 1.0)
    assert first.source is None
    assert second.source == (0, 0)
    with pytest.raises(MediaError):
        content_of(first, MediaStore(tmp_path / "media"), lambda: messages)


# ---- 索引：版本 -------------------------------------------------------------


def test_the_index_version_changes_only_when_something_changed():
    # 页面靠版本（经过状态里的 media）知道要不要重新取列表。每存一次盘都变的话，开着的多媒体一栏每一轮都要重取
    index = MediaIndex()
    shots = [entry("a.png", sha256="a" * 64), entry("b.pdf", "pdf", index=1)]
    index.put(CONVERSATION, "标题", shots)
    assert index.version == 1
    index.put(CONVERSATION, "标题", list(shots))  # 又存了一次盘，附件和标题都没变
    assert index.version == 1
    index.put(CONVERSATION, "新标题", shots)  # 改了名：列表里显示的标题变了
    assert index.version == 2
    index.put(CONVERSATION, "新标题", [*shots, entry("c.md", "text", turn=1, sha256="c" * 64)])
    assert index.version == 3
    index.put(OTHER, "另一段", [entry("a.png", sha256="a" * 64)])
    assert index.version == 4

    assert index.hashes() == {"a" * 64, "c" * 64}  # 没有原件的（旧附件）不算
    assert index.hashes(CONVERSATION) == {"a" * 64, "c" * 64} and index.hashes(OTHER) == {"a" * 64}
    assert index.hashes("20200101-000000-abcdef") == set()
    assert set(index.snapshot()) == {CONVERSATION, OTHER}

    index.remove("20200101-000000-abcdef")  # 没有的会话：什么都没变
    assert index.version == 4
    index.remove(OTHER)
    assert index.version == 5 and set(index.snapshot()) == {CONVERSATION}
    index.put(CONVERSATION, "新标题", [])  # 附件都没了（比如会话文件被改过）：不占地方
    assert index.version == 6 and index.snapshot() == {}
    # 快照是一份拷贝：拿着它的人不会看到之后的变化，也改不到索引
    snapshot = index.snapshot()
    snapshot["x"] = ("", ())
    assert index.snapshot() == {}


def test_saving_a_conversation_without_attachments_is_not_a_change():
    # 绝大多数会话没有附件，每一轮结束都会存一次盘（见 ConversationStore._remember）。它们不在多媒体库里，
    # 存多少次都不该让版本变：否则开着多媒体一栏时，随便聊一句页面就要重新取一次列表
    index = MediaIndex()
    index.put(CONVERSATION, "只是聊天", [])
    index.put(CONVERSATION, "只是聊天", [])
    index.put(CONVERSATION, "改了名字", [])
    assert index.version == 0 and index.snapshot() == {}


def test_the_conversation_store_keeps_the_index_in_step_with_its_files(tmp_path):
    originals = MediaStore(tmp_path / "media")
    kept, _ = originals.keep(checked(image(thumb=THUMB), document("周报.docx", REPORT)))
    store = ConversationStore(tmp_path / "conversations")
    conversation = store.new("anthropic", "claude-opus-5-5")
    conversation.title = "看图"
    conversation.events = [sent(*kept, at=1000.0), {"type": "turn_start"}, {"type": "text", "text": "看到了。"}]
    conversation.messages = history("claude", kept)
    store.save(conversation)

    index = store.media()
    title, entries = index.snapshot()[conversation.id]
    assert title == "看图" and [(item.name, item.kind) for item in entries] == [("截图.png", "image"), ("周报.docx", "document")]
    assert index.hashes() == {sha(PNG_BYTES), sha(REPORT)}
    version = index.version
    store.save(conversation)  # 又存了一次盘，什么都没变
    assert index.version == version

    # 重新启动：从磁盘上读回来的索引一模一样（不用再把原件读一遍）
    assert ConversationStore(tmp_path / "conversations").media().snapshot() == index.snapshot()

    store.rename(conversation.id, "新名字")
    assert index.version == version + 1 and index.snapshot()[conversation.id][0] == "新名字"
    store.delete(conversation.id)
    assert index.version == version + 2 and index.snapshot() == {} and index.hashes() == set()

    # 损坏的文件和列表里一样不进索引（它提到的原件由 collect 留着，见下面）
    (tmp_path / "conversations" / f"{OTHER}.json").write_text('{"坏了": ', encoding="utf-8")
    assert ConversationStore(tmp_path / "conversations").media().snapshot() == {}


# ---- 列表：筛选、搜索、排序、翻页 ---------------------------------------------


def two_conversations() -> list[tuple[str, str, list[Entry]]]:
    return [
        (CONVERSATION, "季度报告", [
            entry("截图.png", at=100.0),
            entry("Report.PDF", "pdf", index=1, at=100.0),
            entry("预算.xlsx", "document", turn=1, at=200.0),
        ]),
        (OTHER, "", [entry("notes.md", "text", at=300.0)]),  # 没有标题的会话，和会话列表里一样叫"新对话"
    ]


def test_the_listing_is_newest_first_and_counts_every_kind():
    page = listing(two_conversations(), {})
    # 最新的在前；同一条消息里的几个按原来的顺序
    assert names(page) == ["notes.md", "预算.xlsx", "截图.png", "Report.PDF"]
    assert page["counts"] == {"all": 4, "image": 1, "pdf": 1, "document": 1, "text": 1}
    assert page["next"] is None
    assert [item["conversation_title"] for item in page["items"]] == ["新对话", "季度报告", "季度报告", "季度报告"]

    # 按种类筛：每一类各有多少个照样数全部的（页面的筛选按钮上要显示这些数）
    only_pdf = listing(two_conversations(), {}, kind="pdf")
    assert names(only_pdf) == ["Report.PDF"] and only_pdf["counts"] == page["counts"]
    assert names(listing(two_conversations(), {}, kind="document")) == ["预算.xlsx"]
    assert listing([], {}) == {"items": [], "counts": {"all": 0, "image": 0, "pdf": 0, "document": 0, "text": 0}, "next": None}


def test_search_looks_at_file_names_and_conversation_titles():
    def found(query: str, **options) -> list[str]:
        return names(listing(two_conversations(), {}, query=query, **options))

    assert found("report") == ["Report.PDF"]  # 不管大小写
    assert found("ＲＥＰＯＲＴ") == ["Report.PDF"]  # 也不管全角半角
    assert found("季度") == ["预算.xlsx", "截图.png", "Report.PDF"]  # 会话标题对上了：这段会话里的都算
    assert found("季度 预算") == ["预算.xlsx"]  # 每个词都要有：一个在标题里，一个在文件名里也行
    assert found("季度 notes") == []
    assert found("新对话") == ["notes.md"]  # 没有标题的会话，按显示出来的名字找
    assert found("") == found("   ") == ["notes.md", "预算.xlsx", "截图.png", "Report.PDF"]
    # 搜索结果里每一类各有多少个
    assert listing(two_conversations(), {}, query="季度")["counts"] == {"all": 3, "image": 1, "pdf": 1, "document": 1, "text": 0}
    assert found("季度", kind="image") == ["截图.png"]


def test_the_listing_comes_a_page_at_a_time():
    many = [(CONVERSATION, "很多图", [entry(f"{n}.png", turn=n, at=float(n)) for n in range(5)])]
    first = listing(many, {}, limit=2)
    assert names(first) == ["4.png", "3.png"] and first["next"] == 2
    second = listing(many, {}, cursor=first["next"], limit=2)
    assert names(second) == ["2.png", "1.png"] and second["next"] == 4
    last = listing(many, {}, cursor=second["next"], limit=2)
    assert names(last) == ["0.png"] and last["next"] is None
    assert listing(many, {}, cursor=5, limit=2)["items"] == [] and listing(many, {}, cursor=99)["next"] is None
    assert listing(many, {}, limit=5)["next"] is None  # 正好取完：没有下一页
    assert all(page["counts"]["all"] == 5 for page in (first, second, last))


def test_each_item_says_what_you_would_download(tmp_path):
    store = MediaStore(tmp_path / "media")
    kept, _ = store.keep(checked(
        image(thumb=THUMB),
        image("照片.heic", data=b64(JPEG_BYTES)),
        document("周报.docx", REPORT),
        text_file("代码.py", "print('你好')"),
    ))
    fresh = entries_of([sent(*kept, at=500.0)], [], 1.0)
    legacy_items = checked(document("周报.docx", REPORT), image("照片.heic", data=b64(JPEG_BYTES)))
    legacy = entries_of([sent(*legacy_items), sent(*checked(pdf("丢了.pdf")))], history("claude", legacy_items), 50.0)
    gone = replace(fresh[0], turn=9, sha256=sha("原件被删掉了".encode()))  # 记着编号，可原件已经不在了
    page = listing([(CONVERSATION, "", fresh), (OTHER, "旧会话", [*legacy, gone])], store.stored())
    items = {(item["conversation"], item["turn"], item["index"]): item for item in page["items"]}

    assert items[(CONVERSATION, 0, 0)] == {
        "id": f"{CONVERSATION}/0/0",
        "conversation": CONVERSATION,
        "conversation_title": "新对话",
        "at": 500.0,
        "turn": 0,
        "index": 0,
        "kind": "image",
        "name": "截图.png",
        "media_type": "image/png",
        "size": len(PNG_BYTES),
        "original": True,
        "available": True,
        "download": "截图.png",
        "thumb": THUMB,
    }

    def shown(key) -> tuple:
        item = items[key]
        return item["kind"], item["media_type"], item["size"], item["original"], item["available"], item["download"]

    # 有原件的：类型和大小看存着的那一份
    assert shown((CONVERSATION, 0, 1)) == ("image", "image/jpeg", len(JPEG_BYTES), True, True, "照片.jpg")
    assert shown((CONVERSATION, 0, 2)) == ("document", OFFICE_TYPES["docx"], len(REPORT), True, True, "周报.docx")
    assert shown((CONVERSATION, 0, 3)) == ("text", "text/plain", len("print('你好')".encode()), True, True, "代码.py")
    # 旧附件：能下载的是历史里的那一份。Word 只剩取出的文字，大小也是那些文字的
    words = content_of(legacy[0], store, lambda: history("claude", legacy_items))[0]
    assert shown((OTHER, 0, 0)) == ("document", "text/plain", len(words), False, True, "周报.docx.txt")
    assert shown((OTHER, 0, 1)) == ("image", "image/jpeg", len(JPEG_BYTES), False, True, "照片.jpg")
    # 内容找不回来的：照样列出来（页面上那条消息还在），只是标明看不了
    assert shown((OTHER, 1, 0)) == ("pdf", "application/pdf", len(PDF_BYTES), False, False, "丢了.pdf")
    assert shown((OTHER, 9, 0)) == ("image", "image/png", len(PNG_BYTES), False, False, "截图.png")
    assert "thumb" not in items[(OTHER, 0, 0)]


def test_download_names_follow_the_content():
    def named(name: str, kind: str, suffix: str, media_type: str = "") -> str:
        return media.download_name(entry(name, kind, **({"media_type": media_type} if media_type else {})), suffix)

    assert named("照片.heic", "image", "jpg") == "照片.jpg"  # HEIC 发出去时换成了 JPEG
    assert named("照片.JPEG", "image", "jpg") == "照片.JPEG"  # 同一种格式的另一种写法：不改
    assert named("拍的照片.png", "image", "", "image/jpeg") == "拍的照片.jpg"  # 旧附件：看历史里的类型
    assert named("截图", "image", "png") == "截图.png"  # 没有扩展名的补上
    assert named("扫描件", "pdf", "") == "扫描件.pdf"
    assert named("周报.docx", "document", "docx") == "周报.docx"
    assert named("周报.docx", "document", "") == "周报.docx.txt"  # 只剩文字
    assert named("代码.py", "text", "txt") == "代码.py" and named("README", "text", "") == "README"
    assert named("周报.odt", "text", "bin") == "周报.odt"


# ---- 内容 -------------------------------------------------------------------


def test_content_comes_from_the_original_first_and_says_when_it_is_gone(tmp_path):
    store = MediaStore(tmp_path / "media")
    page = '<script>alert("x")</script>'
    kept, _ = store.keep(checked(image(), text_file("页面.html", page)))
    shot, html = entries_of([sent(*kept, at=5.0)], [], 1.0)
    # 有原件就用原件，不去读对话历史；文本一律按纯文本回答，里面写着 HTML 也不会被当成网页
    assert content_of(shot, store, no_history) == (PNG_BYTES, "image/png", "截图.png")
    assert content_of(html, store, no_history) == (page.encode(), "text/plain; charset=utf-8", "页面.html")

    # 原件不在了、历史里也找不到：说清楚，不报别的错
    store.find(shot.sha256).unlink()
    with pytest.raises(MediaError, match="没有保存下来"):
        content_of(shot, store, no_history)
    # 旧附件指着的位置在历史里已经不是附件了（文件被改过）：同样说清楚
    messages = history("claude", checked(image()))
    for where in ((0, 9), (9, 0), (1, 0), (0, 1)):
        moved = replace(shot, sha256="", source=where)
        with pytest.raises(MediaError, match="没有保存下来"):
            content_of(moved, store, lambda: messages)
    broken = [{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "坏"}}]}]
    with pytest.raises(MediaError, match="没有保存下来"):
        content_of(replace(shot, sha256="", source=(0, 0)), store, lambda: broken)


def test_the_download_header_carries_any_name_in_plain_ascii():
    # 中文名要按 RFC 5987 写成 filename*=UTF-8''…；老一些的程序只认 filename="…"，另给一个只有 ASCII 的。
    # 响应头只能是 ASCII（http.server 按 latin-1 编码，中文会直接出错），引号、分号也不能把这一行断开
    for name in ("季度 报告.pdf", 'a"b.txt', "a;b.txt", "a\\b.txt", "100%.png", "naïve.png", '季度 报告;"终".docx', "report.pdf"):
        header = disposition(name)
        assert header.isascii() and "\n" not in header and "\r" not in header
        prefix, fallback, rest = header.split('"')
        assert prefix == "attachment; filename="
        assert not set(fallback) & set('"\\;%') and fallback.isprintable() and len(fallback) == len(name)
        assert rest.startswith("; filename*=UTF-8''")
        encoded = rest.removeprefix("; filename*=UTF-8''")
        assert not set(encoded) & set(' ";\\,') and unquote(encoded) == name
    assert disposition("report.pdf") == "attachment; filename=\"report.pdf\"; filename*=UTF-8''report.pdf"


# ---- 编号 -------------------------------------------------------------------


def test_media_ids_cannot_name_anything_but_an_attachment():
    # 浏览器只能给"会话编号/第几排/第几张"。拼不出路径，而且一个附件只有一种写法
    for good in (f"{CONVERSATION}/0/0", f"{CONVERSATION}/12/9", f"{CONVERSATION}/999999/99"):
        assert ID.fullmatch(good), good
    bad = [
        "", "../x", "/etc/passwd", "../../etc/passwd", "x/0/0", f"../{CONVERSATION}/0/0", f"/{CONVERSATION}/0/0",
        f"{CONVERSATION}/../../secret", f"{CONVERSATION}\\0\\0", f"{CONVERSATION}/0/0.json",
        f"{CONVERSATION}/00/0", f"{CONVERSATION}/0/01", f"{CONVERSATION}/+1/0", f"{CONVERSATION}/ 1/0", f"{CONVERSATION}/-1/0",
        f"{CONVERSATION}/0/0/0", f"{CONVERSATION}/0", f"{CONVERSATION}//0", f"{CONVERSATION}/0/100", f"{CONVERSATION}/1000000/0",
        f"{CONVERSATION}/0/0\n", f"\n{CONVERSATION}/0/0", "20261120-140359-A1B2C3/0/0", "2026112-140359-a1b2c3/0/0",
    ]
    sources = [(CONVERSATION, "标题", [entry("a.png")])]
    for value in bad:
        assert not ID.fullmatch(value), value
        with pytest.raises(MediaError):
            media.find(sources, value)
    assert media.find(sources, f"{CONVERSATION}/0/0") == (CONVERSATION, entry("a.png"))
    for missing in (f"{OTHER}/0/0", f"{CONVERSATION}/1/0", f"{CONVERSATION}/0/1"):
        with pytest.raises(MediaError, match="找不到这个文件了"):
            media.find(sources, missing)


def test_media_ids_use_plain_digits_only():
    # Python 的 \d 不加 re.ASCII 时也认阿拉伯-印度数字这类"数字"："1٠" 和 "10" 会被当成同一排，
    # 一个附件就不止一种写法了
    for value in (f"{CONVERSATION}/1٠/0", f"{CONVERSATION}/0/1٣", "٢٠٢٦١١٢٠-١٤٠٣٥٩-a1b2c3/0/0", f"{CONVERSATION}/1１/0"):
        assert not ID.fullmatch(value), value


# ---- 清理：没有会话再用的原件 -------------------------------------------------


def stored_three(tmp_path) -> tuple[MediaStore, str, str, str]:
    store = MediaStore(tmp_path / "media")
    kept, _ = store.keep(checked(image(), pdf(), text_file()))
    return (store, *(item.sha256 for item in kept))


def test_unused_originals_are_removed_and_used_ones_kept(tmp_path):
    store, png, _, _ = stored_three(tmp_path)
    (store.directory / "笔记.txt").write_text("不是原件", encoding="utf-8")
    assert store.collect({png}) == 2
    assert list(store.stored()) == [png]
    assert (store.directory / "笔记.txt").exists()  # 不是这里存的文件，不动
    assert store.collect({png}) == 0

    # 同样的内容以不同的扩展名存过（极少见）：一起删
    (store.directory / f"{png}.bin").write_bytes(PNG_BYTES)
    assert store.collect(set()) == 1 and store.stored() == {}
    assert MediaStore(tmp_path / "没有这个目录").collect(set()) == 0 and not (tmp_path / "没有这个目录").exists()


def test_originals_mentioned_by_a_damaged_conversation_are_kept(tmp_path):
    # 读不出来的会话文件不在索引里，可那是你的会话，也许修好了还要用：里面提到的原件都留着
    store, png, pdf_, txt = stored_three(tmp_path)
    damaged = tmp_path / f"{OTHER}.json"
    damaged.write_text('{"events": [{"type": "user", "attachments": [{"sha256": "' + pdf_ + '"', encoding="utf-8")
    assert store.collect(set(), unreadable=[damaged]) == 2
    assert list(store.stored()) == [pdf_]

    # 有一个可能用到它们的文件读不了：这一次什么都不删（宁可多留，不可错删）
    store, png, pdf_, txt = stored_three(tmp_path / "again")
    folder = tmp_path / "again" / f"{CONVERSATION}.json"
    folder.mkdir()
    assert store.collect(set(), unreadable=[damaged, folder]) == 0
    assert sorted(store.stored()) == sorted([png, pdf_, txt])


def test_deleting_one_conversation_only_looks_at_what_it_used(tmp_path):
    store, png, pdf_, txt = stored_three(tmp_path)
    assert store.collect({png}, candidates=[pdf_, png, "不是编号", sha("没存过".encode())]) == 1
    assert sorted(store.stored()) == sorted([png, txt])  # 没人用的 txt 不在这次要看的里面，先留着


def test_leftover_temporary_files_are_cleared_on_a_full_sweep_only(tmp_path):
    # 上次存到一半就中断留下的临时文件。删一段会话时只看它用过的原件，启动时清一遍才顺手清掉这些
    store, png, pdf_, txt = stored_three(tmp_path)
    leftover = store.directory / f"{png}.png.1a2b3c4d.tmp"
    leftover.write_bytes(b"half")
    store.collect({png, pdf_, txt}, candidates=[txt])
    assert leftover.exists()
    assert store.collect({png, pdf_, txt}) == 0
    assert not leftover.exists() and sorted(store.stored()) == sorted([png, pdf_, txt])
