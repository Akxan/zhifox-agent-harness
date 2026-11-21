"""send_file：助手把这台电脑上的文件发到聊天里（截图、表格、文档）。

前面几节不联网：multipart 的编码、"这个文件在这个平台上怎么发"（outbound.plan）、工具本身（看一眼、再读一遍）、
问不问你（permissions）。中间一节是 Telegram 的客户端对着本地的假 Telegram（tests/fake_telegram.py）。
最后一节把整个程序组装起来（test_channels.py 的 Rig）：手机上的你说"截屏看看"，助手调用 send_file，
确认卡片发到聊天里，你点"允许"，假 Telegram 收到一张照片。别的平台的测试在各自的 test_channels_*.py 里。
不连任何真的服务，不花钱。
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fake_api import reply, text, tool_use
from fake_telegram import TOKEN, FakeTelegram
from multipart_form import parse
from sample_files import JPEG_BYTES, PDF_BYTES, PNG_BYTES
from test_channels import OWNER, rigs, tool_results, until, visible_end

from harness.channels.base import IMAGE_TYPES, ChannelError, OutboundFile, Uploads
from harness.channels.outbound import chinese, plan
from harness.channels.qq import QQChannel
from harness.channels.telegram import UPLOADS as TELEGRAM_UPLOADS
from harness.channels.telegram import TelegramChannel
from harness.i18n import t
from harness.net import http, multipart
from harness.permissions import Permissions
from harness.tools.base import ToolError
from harness.tools.files import FileEditor
from harness.tools.send import NOT_IN_CHAT, TOOL_NAME, make_send_tool
from harness.webui.ui import WebUI

GIF_BYTES = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"


# ---- multipart/form-data ------------------------------------------------------------


def test_multipart_round_trips_fields_and_files():
    body, content_type = multipart.encode(
        {"chat_id": "111", "caption": "说明 & <b>"},
        [multipart.File("photo", "屏幕 截图.png", "image/png", PNG_BYTES)],
    )
    fields, files = parse(content_type, body)
    assert fields == {"chat_id": "111", "caption": "说明 & <b>"}
    assert files == {"photo": {"filename": "屏幕 截图.png", "content_type": "image/png", "data": PNG_BYTES}}  # 中文文件名按 UTF-8 原样写


def test_multipart_names_cannot_break_out_of_their_header():
    # 文件名是模型给的路径的最后一段：里面的引号、换行不能结束引号、多写出一行请求头
    evil = 'a".png\r\nContent-Type: text/html\r\n\r\n<script>'
    body, content_type = multipart.encode({}, [multipart.File("document", evil, "image/png\r\nX-Evil: 1", b"data")])
    head = body.split(b"\r\n\r\n", 1)[0].decode()
    assert head.count("\r\n") == 2  # 边界、Content-Disposition、Content-Type：一行不多
    assert 'filename="a%22.png%0D%0AContent-Type: text/html%0D%0A%0D%0A<script>"' in head
    assert "Content-Type: application/octet-stream" in head  # 类型里有换行：不用它
    _, files = parse(content_type, body)
    assert files["document"]["data"] == b"data"


def test_the_boundary_never_appears_in_the_content(monkeypatch):
    tokens = iter(["aaaa", "bbbb"])
    monkeypatch.setattr(multipart.secrets, "token_hex", lambda size: next(tokens))
    body, content_type = multipart.encode({}, [multipart.File("f", "x.bin", "", b"--zhifox-aaaa inside")])
    assert content_type.endswith("boundary=zhifox-bbbb")  # 第一个撞上了内容：换了一个
    assert parse(content_type, body)[1]["f"]["data"] == b"--zhifox-aaaa inside"


def test_a_too_big_upload_is_refused_before_connecting():
    # 地址是一个连不上的端口：真的去连了的话，说的会是"连不上"
    with pytest.raises(http.NetError, match="文件太大了"):
        multipart.post("http://127.0.0.1:9/upload", files=[multipart.File("f", "big.bin", "", b"x" * 2000)], limit=1000)


def test_upload_errors_never_contain_the_secret():
    with pytest.raises(http.NetError) as caught:
        multipart.post(f"http://127.0.0.1:9/bot{TOKEN}/sendPhoto", files=[multipart.File("photo", "a.png", "image/png", PNG_BYTES)], hide=(TOKEN,))
    assert TOKEN not in str(caught.value) and "127.0.0.1" in str(caught.value)


# ---- 怎么发（outbound.plan）-----------------------------------------------------------


def test_images_go_as_photos_and_everything_else_as_documents():
    uploads = Uploads(file_bytes=50_000_000, photo_bytes=10_000_000)
    assert plan(uploads, "Telegram", "a.png", 1000, "image/png") == ("photo", "")
    assert plan(uploads, "Telegram", "a.pdf", 1000, "application/octet-stream") == ("document", "")
    # 当照片发太大的图片：当文件发（图片照样到了，只是不直接显示）
    assert plan(uploads, "Telegram", "big.png", 20_000_000, "image/png") == ("document", "")
    kind, problem = plan(uploads, "Telegram", "huge.png", 60_000_000, "image/png")
    assert kind == "" and "文件太大了（60.0 MB），Telegram 最多能发 50.0 MB" in problem and "sips" in problem  # 告诉它怎么缩小
    assert "空的" in plan(uploads, "Telegram", "empty.txt", 0, "application/octet-stream")[1]


def test_a_platform_that_only_takes_some_kinds_of_files_says_which():
    uploads = Uploads(file_bytes=20_000_000, photo_bytes=20_000_000, photo_types=frozenset({"image/png", "image/jpeg"}), extensions=frozenset({"pdf", "zip"}))
    assert plan(uploads, "钉钉", "a.PDF", 10, "application/octet-stream") == ("document", "")
    kind, problem = plan(uploads, "钉钉", "notes.txt", 10, "application/octet-stream")
    assert kind == "" and "钉钉只能发 JPEG、PNG 格式的图片，和这几种文件：pdf、zip" in problem and "打包成 zip" in problem
    assert plan(uploads, "钉钉", "a.webp", 10, "image/webp")[0] == ""  # 当不了照片，也不在能发的文件里


def test_a_platform_without_uploads_says_it_cannot_send_files():
    assert QQChannel.capabilities.uploads is None  # QQ 只能发它自己去下载的公网网址（见 qq.py）
    kind, problem = plan(None, "QQ", "a.png", 10, "image/png")
    assert kind == "" and "QQ 暂时不支持让机器人发文件" in problem


def test_platform_errors_are_handed_to_the_model_in_chinese(monkeypatch):
    from harness import i18n

    monkeypatch.setattr(i18n, "current", "en")
    message = t("连不上 {host}（{error}）", host="api.telegram.org", error=t("这个文件太大了，Telegram 不让机器人发这么大的文件。"))
    assert not str(message).startswith("连不上")  # 界面是英文的
    assert chinese(message) == "连不上 api.telegram.org（这个文件太大了，Telegram 不让机器人发这么大的文件。）"


# ---- 工具本身 -------------------------------------------------------------------------


class FakeOutlet:
    """一个假的去处：记下发出去的文件。"""

    label = "Telegram"
    person = "Akxan (@akxan)"

    def __init__(self, uploads: Uploads = TELEGRAM_UPLOADS, fail: str = ""):
        self.uploads = uploads
        self.fail = fail
        self.sent: list[OutboundFile] = []

    def plan(self, name, size, media_type):
        return plan(self.uploads, self.label, name, size, media_type)

    def send(self, outgoing):
        if self.fail:
            raise ToolError(self.fail)
        self.sent.append(outgoing)


@pytest.fixture
def editor(tmp_path):
    workspace = tmp_path / "work"
    (workspace / "data").mkdir(parents=True)
    (workspace / ".env").write_text("ANTHROPIC_API_KEY=sk-secret\n", encoding="utf-8")
    return FileEditor(workspace, tmp_path / "backups", protected=[workspace / ".env", workspace / "data" / "channels.json"])


def call(tool, tool_input):
    """像 agent.py 一样：先 check，没问题再 run。"""
    refusal = tool.check(tool_input)
    return refusal if refusal else tool.run(tool_input)


def test_the_spec_is_the_same_in_and_out_of_a_chat(editor):
    # 网页上打开一段聊天通道里的会话，工具清单要和在聊天里时一字不差：说明书不能带着平台、上限这些
    assert make_send_tool(editor, None).spec == make_send_tool(editor, FakeOutlet()).spec
    assert make_send_tool(editor, None).portable == make_send_tool(editor, FakeOutlet(Uploads(file_bytes=1))).portable
    spec = make_send_tool(editor, None).spec
    assert spec["name"] == TOOL_NAME and set(spec["input_schema"]["properties"]) == {"path", "caption"}
    assert spec["input_schema"]["required"] == ["path"]


def test_outside_a_chat_it_says_so(editor):
    (editor.workspace / "a.png").write_bytes(PNG_BYTES)
    assert make_send_tool(editor, None).check({"path": "a.png"}) == NOT_IN_CHAT


def test_sending_an_image_and_a_document(editor):
    (editor.workspace / "shot.png").write_bytes(PNG_BYTES)
    (editor.workspace / "report.pdf").write_bytes(PDF_BYTES)
    outlet = FakeOutlet()
    tool = make_send_tool(editor, outlet)
    answer = call(tool, {"path": "shot.png", "caption": "  现在的屏幕  "})
    assert answer.startswith("已经把 shot.png（图片，作为照片显示在聊天里") and "Telegram" in answer
    assert call(tool, {"path": str(editor.workspace / "report.pdf")}).startswith("已经把 report.pdf（文件")
    photo, document = outlet.sent
    assert (photo.name, photo.data, photo.media_type, photo.kind, photo.caption) == ("shot.png", PNG_BYTES, "image/png", "photo", "现在的屏幕")
    assert (document.media_type, document.kind, document.caption) == ("application/octet-stream", "document", "")


def test_files_it_must_never_send(symlinks, editor):
    outlet = FakeOutlet()
    tool = make_send_tool(editor, outlet)
    (editor.workspace / "data" / "channels.json").write_text("{}", encoding="utf-8")
    (editor.workspace / "link").symlink_to(editor.workspace / ".env")
    assert tool.check({"path": ".env"}) == "这个文件保存着密钥，不能发出去。"
    assert tool.check({"path": "link"}) == "这个文件保存着密钥，不能发出去。"  # 符号链接展开以后才比
    assert tool.check({"path": "data/../data/channels.json"}) == "这个文件保存着密钥，不能发出去。"
    assert "不存在" in tool.check({"path": "nope.png"})
    assert "文件夹" in tool.check({"path": "data"}) and "zip -r" in tool.check({"path": "data"})
    assert "path" in tool.check({})
    assert "没有 extra 这个参数" in tool.check({"path": "a", "extra": 1})
    with pytest.raises(ToolError):
        tool.run({"path": ".env"})  # 就算没先 check，run 也不发
    assert outlet.sent == []


def test_a_file_changed_after_approval_is_not_sent(symlinks, editor):
    target = editor.workspace / "notes.txt"
    target.write_text("给你看的那一版", encoding="utf-8")
    outlet = FakeOutlet()
    tool = make_send_tool(editor, outlet)
    tool_input = {"path": "notes.txt"}
    assert tool.check(tool_input) is None  # 问你的时候是这个样子
    target.write_text("被别的程序换掉了，长度也不一样", encoding="utf-8")
    with pytest.raises(ToolError, match="变了"):
        tool.run(tool_input)
    # 换成指向别处的符号链接：也不发
    other = editor.workspace / "other.txt"
    other.write_text("x", encoding="utf-8")
    moved = {"path": "notes.txt"}
    tool.check(moved)
    target.unlink()
    target.symlink_to(other)
    with pytest.raises(ToolError, match="换了"):
        tool.run(moved)
    assert outlet.sent == []


def test_a_failed_send_reaches_the_model(editor):
    (editor.workspace / "a.png").write_bytes(PNG_BYTES)
    tool = make_send_tool(editor, FakeOutlet(fail="没有发出去：Telegram 拒绝了"))
    with pytest.raises(ToolError, match="Telegram 拒绝了"):
        tool.run({"path": "a.png"})


# ---- 问不问你 ---------------------------------------------------------------------------


def permissions_for(editor, mode: str) -> Permissions:
    permissions = Permissions(editor, private=[editor.workspace / "data"])
    permissions.mode = mode
    return permissions


def test_sending_asks_unless_everything_is_allowed(editor):
    (editor.workspace / "shot.png").write_bytes(PNG_BYTES)
    tool = make_send_tool(editor, FakeOutlet())
    tool_input = {"path": "shot.png", "caption": "屏幕"}
    question = permissions_for(editor, "ask").review(TOOL_NAME, tool_input, tool)
    # 卡片上：发到哪里、哪个文件、多大、怎么发、说明
    assert question.startswith("把一个文件发到 Telegram（和 Akxan (@akxan) 的私聊）：")
    assert "文件：shot.png" in question and "大小：1 KB（图片，会直接显示在聊天里）" in question and "说明：屏幕" in question
    assert "⚠" not in question
    assert not permissions_for(editor, "ask").auto_allows(TOOL_NAME, tool_input, tool)
    assert not permissions_for(editor, "edits").auto_allows(TOOL_NAME, tool_input, tool)  # 放开的是改文件，不是往外发
    assert permissions_for(editor, "all").auto_allows(TOOL_NAME, tool_input, tool)
    for mode in ("ask", "edits", "all"):  # 每一种模式下都是"要问"的操作，只是 all 不问就放行（界面上留下记录）
        assert permissions_for(editor, mode).review(TOOL_NAME, tool_input, tool) is not None


def test_the_assistants_own_data_asks_with_a_warning(editor):
    (editor.workspace / "data" / "settings.json").write_text("{}", encoding="utf-8")
    tool = make_send_tool(editor, FakeOutlet())
    tool_input = {"path": "data/settings.json"}
    question = permissions_for(editor, "edits").review(TOOL_NAME, tool_input, tool)
    assert "⚠ 这是助手自己保存的数据" in question
    assert not permissions_for(editor, "edits").auto_allows(TOOL_NAME, tool_input, tool)
    assert permissions_for(editor, "all").auto_allows(TOOL_NAME, tool_input, tool)  # 和读 data/ 一样：只有"全部自动允许"不问


def test_refused_sends_do_not_ask(editor):
    tool = make_send_tool(editor, FakeOutlet())
    for tool_input in ({"path": ".env"}, {"path": "nope.png"}, {}):
        assert permissions_for(editor, "ask").review(TOOL_NAME, tool_input, tool) is None
    assert permissions_for(editor, "ask").review(TOOL_NAME, {"path": "a.png"}, make_send_tool(editor, None)) is None


# ---- Telegram 的客户端 ----------------------------------------------------------------


@pytest.fixture
def fake():
    telegram = FakeTelegram(max_poll=0.2).start()
    yield telegram
    telegram.stop()


def outgoing(name="shot.png", data=PNG_BYTES, media_type="image/png", kind="photo", caption=""):
    return OutboundFile(name, data, media_type, kind, caption)


def test_telegram_sends_photos_animations_and_documents(fake):
    channel = TelegramChannel(TOKEN, fake.base_url)
    channel.send_file(OWNER, outgoing(caption="**现在**的屏幕 & <它>"))
    channel.send_file(OWNER, outgoing("moving.gif", GIF_BYTES, "image/gif"))
    channel.send_file(OWNER, outgoing("report.pdf", PDF_BYTES, "application/octet-stream", "document"))
    photo, gif, document = fake.uploads
    assert (photo["method"], photo["field"], photo["filename"], photo["content_type"], photo["data"]) == ("sendPhoto", "photo", "shot.png", "image/png", PNG_BYTES)
    assert photo["parse_mode"] == "HTML" and photo["caption"] == "<b>现在</b>的屏幕 &amp; &lt;它&gt;" and photo["chat_id"] == OWNER
    assert (gif["method"], gif["field"]) == ("sendAnimation", "animation")  # GIF 当动图发，聊天里会动
    assert (document["method"], document["field"], document["caption"]) == ("sendDocument", "document", "")
    assert fake.sent == []  # 说明放在 caption 里，没有另发一条


def test_telegram_sends_a_long_caption_as_a_message_after_the_file(fake):
    channel = TelegramChannel(TOKEN, fake.base_url)
    caption = "很长的说明。" * 300
    channel.send_file(OWNER, outgoing(caption=caption))
    (upload,) = fake.uploads
    assert upload["caption"] == ""
    (message,) = fake.sent
    assert message["message_id"] > upload["message_id"] and message["plain"].startswith("很长的说明。")


def test_a_photo_telegram_cannot_show_goes_as_a_document(fake):
    channel = TelegramChannel(TOKEN, fake.base_url)
    fake.fail_next["sendPhoto"] = {"error_code": 400, "description": "Bad Request: PHOTO_INVALID_DIMENSIONS"}
    channel.send_file(OWNER, outgoing("tall.png"))
    (upload,) = fake.uploads
    assert upload["method"] == "sendDocument" and upload["data"] == PNG_BYTES


def test_telegram_upload_errors_are_explained_without_the_token(fake):
    channel = TelegramChannel(TOKEN, fake.base_url)
    fake.fail_next["sendDocument"] = {"error_code": 413, "description": f"Request Entity Too Large for bot{TOKEN}"}
    with pytest.raises(ChannelError) as caught:
        channel.send_file(OWNER, outgoing("a.pdf", PDF_BYTES, "application/octet-stream", "document"))
    assert "太大了" in str(caught.value) and TOKEN not in str(caught.value)
    fake.fail_next["sendPhoto"] = {"error_code": 403, "description": f"Forbidden: bot{TOKEN} was blocked by the user"}
    with pytest.raises(ChannelError) as caught:
        channel.send_file(OWNER, outgoing())
    assert "停用" in str(caught.value) and TOKEN not in str(caught.value)
    # 连不上（端口没人听）：说明里只有主机名
    with pytest.raises(ChannelError) as caught:
        TelegramChannel(TOKEN, "http://127.0.0.1:9").send_file(OWNER, outgoing())
    assert "连不上 Telegram" in str(caught.value) and TOKEN not in str(caught.value)


def test_the_fake_telegram_rejects_what_the_real_one_would(fake):
    # 假 Telegram 也照着真的检查大小：测试里的"太大了"不是我们自己编的
    channel = TelegramChannel(TOKEN, fake.base_url)
    with pytest.raises(ChannelError, match="太大了"):
        channel.send_file(OWNER, outgoing("big.png", PNG_BYTES + b"\0" * 10_000_001, kind="photo"))


def test_telegrams_upload_limits_follow_its_documentation():
    assert TelegramChannel.capabilities.uploads == Uploads(file_bytes=50_000_000, photo_bytes=10_000_000, photo_types=IMAGE_TYPES, caption_chars=1024)


# ---- 整个程序：手机 → 智狐 → 手机 ----------------------------------------------------------


@pytest.fixture
def bot(make_config, monkeypatch):
    yield from rigs(make_config, monkeypatch)


def send(id: str, path: str, caption: str | None = None, before: str = "") -> dict:
    """一条"把文件发到聊天里"的回复（模型调用 send_file），前面可以先说一句话。"""
    tool_input = {"path": path, **({"caption": caption} if caption is not None else {})}
    blocks = ([text(before)] if before else []) + [tool_use(id, TOOL_NAME, tool_input)]
    return reply(*blocks, stop_reason="tool_use")


def result_of(request: dict) -> dict:
    (result,) = tool_results(request)
    content = result["content"]
    return {"text": content if isinstance(content, str) else "".join(part.get("text", "") for part in content), "error": bool(result.get("is_error"))}


def test_a_screenshot_is_approved_in_the_chat_and_arrives_as_a_photo(bot):
    s = bot(send("toolu_1", "screen.png", caption="**这是**现在的屏幕", before="截好了。"), reply(text("发过去了，看得到吗？")))
    (s.config.workspace / "screen.png").write_bytes(PNG_BYTES)
    start = s.say("截屏看下你在做啥")
    first = s.wait_reply(start, "截好了")
    ask = s.asked(start)

    # 确认卡片在聊天里：发到哪里、哪个文件、多大，看清楚再点
    assert ask["text"].startswith("<b>助手想要把一个文件发到 Telegram（和 Akxan (@akxan) 的私聊）：</b>")
    assert "文件：screen.png" in ask["plain"] and "大小：1 KB（图片，会直接显示在聊天里）" in ask["plain"] and "说明：**这是**现在的屏幕" in ask["plain"]
    assert s.tg.uploads == []  # 你点之前什么都没发
    s.press(ask)

    photo = s.tg.wait_upload()
    assert (photo["method"], photo["chat_id"], photo["filename"], photo["data"]) == ("sendPhoto", OWNER, "screen.png", PNG_BYTES)
    assert photo["caption"] == "<b>这是</b>现在的屏幕"
    s.wait_reply(start, "看得到吗")
    s.idle()
    assert first["message_id"] < ask["message_id"] < photo["message_id"]  # 聊天里的顺序和助手做的一样
    assert visible_end(s.edit_of(ask)) == "已允许"
    result = result_of(s.api.requests[1])
    assert not result["error"] and result["text"].startswith("已经把 screen.png（图片，作为照片显示在聊天里，1 KB）发到了 Telegram 的聊天里")

    # 网页上的会话里照样有这张工具卡片
    saved = s.app.store.load(s.session().conversation.id)
    (call,) = [event for event in saved.events if event["type"] == "tool_call"]
    assert call["name"] == TOOL_NAME and call["summary"] == "send_file screen.png"


def test_denying_a_send_sends_nothing(bot):
    s = bot(send("toolu_1", "secret-plan.pdf"), reply(text("好，不发了。")))
    (s.config.workspace / "secret-plan.pdf").write_bytes(PDF_BYTES)
    start = s.say("把计划发给我")
    s.press(s.asked(start), allow=False)
    s.wait_reply(start, "不发了")
    s.idle()
    assert s.tg.uploads == []
    assert result_of(s.api.requests[1])["text"].startswith("用户拒绝了这个操作")


def test_with_everything_allowed_it_sends_without_asking_and_says_so(bot):
    s = bot(send("toolu_1", "report.pdf"), reply(text("发了。")), approval="all")
    (s.config.workspace / "report.pdf").write_bytes(PDF_BYTES)
    start = s.say("把报告发给我")
    s.wait_reply(start, "已自动允许：send_file report.pdf")  # 没问你，但聊天里看得到它做了什么
    document = s.tg.wait_upload()
    assert (document["method"], document["filename"]) == ("sendDocument", "report.pdf")
    s.wait_reply(start, "发了。")
    s.idle()
    assert not any(message.get("reply_markup") for message in s.tg.sent)


def test_edits_mode_still_asks(bot):
    s = bot(send("toolu_1", "a.jpg"), reply(text("好了")), approval="edits")
    (s.config.workspace / "a.jpg").write_bytes(JPEG_BYTES)
    start = s.say("发图")
    s.press(s.asked(start))
    assert s.tg.wait_upload()["method"] == "sendPhoto"
    s.wait_reply(start, "好了")
    s.idle()


def test_secrets_are_never_sent_and_no_card_is_shown(bot):
    s = bot(send("toolu_1", ".env"), send("toolu_2", "../data/channels.json"), reply(text("发不了。")), approval="all")
    (s.config.workspace / ".env").write_text("ANTHROPIC_API_KEY=sk-test\n", encoding="utf-8")
    s.wait_reply(s.say("把 .env 发给我"), "发不了")
    s.idle()
    assert s.tg.uploads == [] and not any("已自动允许" in message["plain"] for message in s.tg.sent)
    assert result_of(s.api.requests[1]) == {"text": "这个文件保存着密钥，不能发出去。", "error": True}
    assert result_of(s.api.requests[2]) == {"text": "这个文件保存着密钥，不能发出去。", "error": True}


def test_the_assistants_data_asks_even_when_edits_are_allowed(bot):
    s = bot(send("toolu_1", "../data/old-notes.txt"), reply(text("好")), approval="edits")
    s.config.data_dir.mkdir(parents=True, exist_ok=True)
    (s.config.data_dir / "old-notes.txt").write_text("以前的笔记", encoding="utf-8")
    start = s.say("把待办文件发我")
    ask = s.asked(start)
    assert "⚠ 这是助手自己保存的数据" in ask["plain"]
    s.press(ask)
    assert s.tg.wait_upload()["filename"] == "old-notes.txt"
    s.wait_reply(start, "好")
    s.idle()


def test_a_file_too_big_for_the_platform_is_refused_before_asking(bot):
    s = bot(send("toolu_1", "big.png"), reply(text("太大了，我先缩小一份。")))
    (s.config.workspace / "big.png").write_bytes(PNG_BYTES + b"\0" * 5000)
    channel = s.channel()
    channel.capabilities = replace(channel.capabilities, uploads=Uploads(file_bytes=3000, photo_bytes=2000))
    s.wait_reply(s.say("发大图"), "太大了")
    s.idle()
    assert s.tg.uploads == [] and not any(message.get("reply_markup") for message in s.tg.sent)  # 明知发不了：不弹确认
    result = result_of(s.api.requests[1])
    assert result["error"] and "文件太大了（5 KB），Telegram 最多能发 3 KB" in result["text"] and "sips" in result["text"]


def test_an_image_too_big_to_be_a_photo_goes_as_a_document(bot):
    s = bot(send("toolu_1", "big.png"), reply(text("好")), approval="all")
    (s.config.workspace / "big.png").write_bytes(PNG_BYTES + b"\0" * 3000)
    channel = s.channel()
    channel.capabilities = replace(channel.capabilities, uploads=Uploads(file_bytes=10_000, photo_bytes=2000))
    s.wait_reply(s.say("发大图"), "好")
    s.idle()
    assert s.tg.wait_upload()["method"] == "sendDocument"
    assert "（文件，" in result_of(s.api.requests[1])["text"]


def test_a_platform_error_reaches_the_model_without_the_token(bot):
    s = bot(send("toolu_1", "a.pdf"), reply(text("没发出去")), approval="all")
    (s.config.workspace / "a.pdf").write_bytes(PDF_BYTES)
    s.tg.fail_next["sendDocument"] = {"error_code": 400, "description": f"Bad Request: chat not found (bot{TOKEN})"}
    s.wait_reply(s.say("发"), "没发出去")
    s.idle()
    result = result_of(s.api.requests[1])
    assert result["error"] and result["text"].startswith("没有发出去：Telegram 回答了一个错误") and TOKEN not in result["text"]
    assert TOKEN not in json.dumps(s.api.requests, ensure_ascii=False)


def test_stop_while_a_file_is_uploading(bot, monkeypatch):
    """你发 /stop 的时候文件正在上传（网络慢）：这一轮马上停下，不等上传结束；工具调用照样有结果，告诉模型"可能发出去了"。

    聊天里的回话（"好，正在停下""已中断。"）排在这个文件后面：发送队列是按顺序的，上传完了它们才到。"""
    import threading

    s = bot(send("toolu_1", "a.pdf"), reply(text("停下以后")), approval="all")
    (s.config.workspace / "a.pdf").write_bytes(PDF_BYTES)
    gate = threading.Event()
    channel = s.channel()
    original = channel.send_file

    def slow(chat, item):
        gate.wait(10)
        original(chat, item)

    monkeypatch.setattr(channel, "send_file", slow)
    s.say("发")
    until(lambda: s.session().running and s.session().ui.activity == "send_file a.pdf", what="开始发")
    stopping = s.say("/stop")
    until(lambda: not s.session().running, what="这一轮停下")
    assert len(s.api.requests) == 1 and not gate.is_set()  # 没等上传完，也没有再去问模型
    gate.set()
    s.wait_reply(stopping, "已中断。")
    s.idle()
    s.wait_reply(s.say("继续"), "停下以后")
    s.idle()
    (result,) = [block for message in s.api.requests[1]["messages"] if isinstance(message["content"], list) for block in message["content"] if block.get("type") == "tool_result"]
    assert result["tool_use_id"] == "toolu_1" and result["is_error"] is True
    assert "用户中断了发送" in json.dumps(result, ensure_ascii=False)


def test_a_file_still_waiting_in_the_queue_is_not_sent_after_stop():
    """排在发送队列里、还没轮到的文件：/stop 以后轮到它也不发（router.send_file 返回 skipped）。"""
    import threading

    from harness.channels.router import Outbox, Router

    router = Router.__new__(Router)
    router._lock = threading.Lock()
    sent: list[str] = []

    class Channel:
        def send_file(self, chat, item):
            sent.append(item.name)

    gate = threading.Event()
    outbox = Outbox("test")
    outbox.put(lambda: gate.wait(10))  # 前面有一条话卡着（网络慢）
    router._outboxes, router._channels = {"telegram": outbox}, {"telegram": Channel()}
    cancelled = threading.Event()
    answer: list[str] = []
    worker = threading.Thread(target=lambda: answer.append(router.send_file("telegram", OWNER, outgoing(), cancelled, 10)))
    worker.start()
    cancelled.set()
    worker.join(5)
    assert answer == ["pending"]  # 不知道它会不会发：先别再等了
    gate.set()
    assert outbox.flush(5)
    assert sent == []  # 轮到它的时候已经停了：不发
    # 没停的照常发，等它发完
    assert router.send_file("telegram", OWNER, outgoing("b.png"), threading.Event(), 10) == "sent" and sent == ["b.png"]
    outbox.close(1)


# ---- 它在哪儿有：工具清单在一段对话里不变 -------------------------------------------------------


def tool_names(request: dict) -> list[str]:
    return [tool["name"] for tool in request["tools"]]


def test_the_tool_is_only_offered_where_it_can_be_used(bot):
    s = bot(reply(text("网页上的回答")), reply(text("聊天里的回答")))
    # 网页上的普通对话：没有它
    assert s.browser.post("/api/message", {"text": "你好"})[0] == 200
    until(lambda: s.api.requests, what="网页的请求")
    assert TOOL_NAME not in tool_names(s.api.requests[0])
    # 聊天里：有
    s.wait_reply(s.say("你好"), "聊天里的回答")
    s.idle()
    assert TOOL_NAME in tool_names(s.api.requests[1])
    # 定时任务：没有
    run = s.app.unattended_agent(WebUI())
    assert run.agent.tools.get(TOOL_NAME) is None


def test_the_request_prefix_stays_the_same_across_turns_restarts_and_the_web(bot):
    s = bot(reply(text("一")), reply(text("二")))
    s.wait_reply(s.say("第一轮"), "一")
    s.idle()
    s.wait_reply(s.say("第二轮"), "二")
    s.idle()
    conversation_id = s.session().conversation.id
    first, second = s.api.requests
    assert TOOL_NAME in tool_names(first)
    assert (second["system"], second["tools"]) == (first["system"], first["tools"])

    s.close()
    again = bot(reply(text("三")), reply(text("网页上的四")))  # 重启
    again.wait_reply(again.say("第三轮"), "三")
    again.idle()
    third = again.api.requests[0]
    assert (third["system"], third["tools"]) == (first["system"], first["tools"])

    # 在网页上接着聊这段会话：工具清单一字不差（缓存照样命中），只是 send_file 发不出去
    assert again.browser.post("/api/conversation/open", {"id": conversation_id})[0] == 200
    assert again.browser.post("/api/message", {"text": "网页接着聊"})[0] == 200
    until(lambda: len(again.api.requests) == 2, what="网页的请求")
    fourth = again.api.requests[1]
    assert (fourth["system"], fourth["tools"]) == (first["system"], first["tools"])
    assert again.app.agent.tools.get(TOOL_NAME).check({"path": "a.png"}) == NOT_IN_CHAT

    # 网页上新开一段：不在聊天软件里，没有它。上面只等到了请求发出去，这一轮可能还在收尾（存盘、发状态），
    # 这时"新对话"会回答"上一轮还没有结束"（409）：等它结束
    until(lambda: again.browser.post("/api/new", {})[0] == 200, what="网页的这一轮结束、开始新对话")
    assert again.app.agent.tools.get(TOOL_NAME) is None
    assert TOOL_NAME not in [name for name, _ in again.app.tool_names()]


def test_in_the_web_it_answers_that_the_chat_is_not_here(bot):
    s = bot(reply(text("聊天里的回答")), send("toolu_1", "a.png"), reply(text("那我告诉你位置")))
    (s.config.workspace / "a.png").write_bytes(PNG_BYTES)
    s.wait_reply(s.say("你好"), "聊天里的回答")
    s.idle()
    conversation_id = s.session().conversation.id
    assert s.browser.post("/api/conversation/open", {"id": conversation_id})[0] == 200
    assert s.browser.post("/api/message", {"text": "把图发我"})[0] == 200
    until(lambda: len(s.api.requests) == 3, what="网页的第二次请求")
    assert result_of(s.api.requests[2]) == {"text": NOT_IN_CHAT, "error": True}
    assert not s.browser.of_type("confirm")  # 不弹确认卡片
    assert s.tg.uploads == []
