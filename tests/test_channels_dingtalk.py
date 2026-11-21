"""钉钉通道的测试：检查 Client ID 和 Client Secret、回复走 sessionWebhook 还是主动发、下载文件、确认请求，
以及一整条"手机 → 智狐 → 手机"的路。

对着本地的假钉钉（tests/fake_dingtalk.py）：REST 和 sessionWebhook 是真的 HTTP，Stream 连接平常换成 FakeStreamListener
（交给通道的消息和 SDK 交的一样）。组装见 tests/channel_rig.py，测试代码扮演手机上的你（s.say）和浏览器（s.browser）。
最后一节用真的 SDK（dingtalk-stream）连假钉钉的 Stream 网关：换票据、连 WebSocket、收消息、回执、停下；没装 SDK 就跳过。
不连真正的钉钉，不花钱。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import threading
import time
import zipfile

import pytest
from channel_rig import rig_fixture
from fake_api import reply, text
from fake_dingtalk import (
    CLIENT_ID,
    CLIENT_SECRET,
    OWNER,
    OWNER_NAME,
    STRANGER,
    TOPIC,
    FakeDingTalk,
)
from sample_files import JPEG_BYTES
from test_channels import result_text, run, stop_later, tool_results, until

from harness.channels import dingtalk, platforms
from harness.channels.base import ChannelError
from harness.channels.dingtalk import SAFE_LIMIT, DingTalkChannel, StreamListener
from harness.channels.store import ChannelStore
from harness.net import http

WRONG_SECRET = "WRONG-dingtalk-secret-0123456789"


@pytest.fixture
def fake():
    server = FakeDingTalk().start()
    yield server
    stop_later(server.stop)


@pytest.fixture
def bot(make_config, monkeypatch):
    yield from rig_fixture(make_config, monkeypatch, lambda: FakeDingTalk().start())


def sdk(monkeypatch, ready: bool) -> None:
    """当作钉钉的 SDK 装了（或者没装）。CI 上没装它；这里测的是我们自己的代码怎么对待"装没装"，不是 SDK。"""
    real = platforms.sdk_ready
    monkeypatch.setattr(platforms, "sdk_ready", lambda platform: ready if platform == "dingtalk" else real(platform))


def post(s, action: str, **body) -> tuple[int, dict]:
    return s.browser.post(f"/api/channel/{action}", body)


def said(s, words: str, after: int = 0) -> dict:
    """等机器人说一句正好是 words 的话（确认请求的结果：已允许、已拒绝……）。"""
    return s.fake.wait_sent(lambda message: message["plain"] == words, after=after)


# ---- 检查 Client ID 和 Client Secret --------------------------------------------------


def test_a_malformed_client_id_or_secret_is_rejected_without_asking_dingtalk(fake, make_config):
    config = make_config(dingtalk_api=fake.base_url)
    for client_id in ("ding", "ding fake client"):
        with pytest.raises(ChannelError, match="Client ID 看起来不对"):
            dingtalk.check(config, {"client_id": client_id, "client_secret": CLIENT_SECRET})
    with pytest.raises(ChannelError, match="太短"):
        dingtalk.check(config, {"client_id": CLIENT_ID, "client_secret": "short"})
    assert fake.requests == []


def test_check_explains_a_wrong_secret_and_returns_the_bot(fake, make_config):
    config = make_config(dingtalk_api=fake.base_url)
    with pytest.raises(ChannelError) as caught:
        dingtalk.check(config, {"client_id": CLIENT_ID, "client_secret": WRONG_SECRET})
    assert "不认这个 Client ID 和 Client Secret" in str(caught.value) and WRONG_SECRET not in str(caught.value)
    assert dingtalk.check(config, {"client_id": f" {CLIENT_ID}", "client_secret": f"{CLIENT_SECRET}\n"}) == fake.bot
    assert fake.requests == ["POST /v1.0/oauth2/accessToken"] * 2


# ---- 在页面上连接；没装 SDK；运行时密钥不对 -------------------------------------------------


def test_connecting_from_the_page(bot, monkeypatch):
    sdk(monkeypatch, True)
    s = bot(connected=False)
    entry = s.platform()
    assert entry["sdk"] == {"module": "dingtalk_stream", "package": "dingtalk-stream", "extra": "dingtalk", "ready": True, "install": "uv sync --extra dingtalk", "message": ""}
    assert [field["id"] for field in entry["fields"]] == ["client_id", "client_secret"] and entry["buttons"] is False  # 钉钉里回复文字来确认

    status, body = post(s, "connect", platform="dingtalk", credentials={"client_id": CLIENT_ID, "client_secret": WRONG_SECRET})
    assert status == 409 and "不认这个 Client ID 和 Client Secret" in body["error"] and WRONG_SECRET not in body["error"]
    assert not s.config.channels_file.exists()

    status, body = post(s, "connect", platform="dingtalk", credentials={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    assert status == 200 and body["bot"]["handle"] == CLIENT_ID
    until(lambda: s.platform()["status"]["state"] == "online", what="连上")
    assert s.platform()["hints"] == {"client_id": CLIENT_ID, "client_secret": "…" + CLIENT_SECRET[-4:]}
    assert ChannelStore(s.config.channels_file).credentials("dingtalk") == {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}


def test_without_the_sdk_connecting_is_refused_and_says_how_to_install_it(bot, monkeypatch):
    sdk(monkeypatch, False)
    s = bot(connected=False)
    entry = s.platform()["sdk"]
    assert entry["ready"] is False and entry["install"] == "uv sync --extra dingtalk"
    assert "dingtalk-stream" in entry["message"] and "uv sync --extra dingtalk" in entry["message"]
    status, body = post(s, "connect", platform="dingtalk", credentials={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    assert status == 409 and "uv sync --extra dingtalk" in body["error"]
    assert s.fake.requests == [] and not s.config.channels_file.exists()


def test_a_real_channel_without_the_sdk_shows_an_error(fake, monkeypatch):
    sdk(monkeypatch, False)
    channel = DingTalkChannel(CLIENT_ID, CLIENT_SECRET, fake.base_url)  # 没有换掉 Stream 连接：要用真的 SDK
    channel.start(lambda event: None)
    until(lambda: channel.status.state == "error", what="报错")
    assert "uv sync --extra dingtalk" in channel.status.message and fake.requests == []
    channel.stop()
    channel.join(2)


def test_a_secret_reset_on_dingtalk_shows_an_error_instead_of_retrying_silently(bot):
    bot.fake.client_secret = "the-new-secret-after-a-reset-0123"  # 在开放平台上重置过：存着的那个不对了
    s = bot(online=False)
    until(lambda: s.platform()["status"]["state"] == "error", what="页面上的错误")
    assert "不认这个 Client ID 和 Client Secret" in s.platform()["status"]["message"]
    assert s.fake.listener is None  # SDK 不会说密钥不对、只会一直重试：所以根本没去连
    assert set(s.fake.requests) == {"POST /v1.0/oauth2/accessToken"}


# ---- 配对、陌生人、群聊、重复推送 ---------------------------------------------------------


def test_pairing_in_a_private_chat(bot):
    s = bot(paired=False)
    code = s.hub.pair("dingtalk")["code"]
    paired = s.wait_reply(s.say(code), "配对成功")
    assert (paired["chat"], paired["via"]) == (OWNER, "webhook") and OWNER_NAME in paired["plain"]
    assert "回复“允许”或“拒绝”" in paired["plain"]  # 说明里写着确认请求怎么答（钉钉里没有按钮）
    owner = s.hub.store.get("dingtalk")["owner"]
    assert (owner["id"], owner["name"]) == (OWNER, OWNER_NAME)
    assert "现在：空闲" in s.wait_reply(s.say("/status"), "现在：")["plain"]


def test_strangers_and_groups_are_ignored_silently(bot):
    s = bot()
    before = len(s.fake.sent)
    s.fake.user_says("你好", user=STRANGER)
    s.fake.user_says("123456", user=STRANGER)
    s.fake.user_says("/help", private=False)  # 主人在群里 @ 了机器人：群里的人都看得到，一律不理
    s.sentinel()
    assert [message["chat"] for message in s.fake.sent[before:]] == [OWNER]
    assert s.api.requests == []
    until(lambda: s.platform()["ignored"] == 2, what="页面上的计数")
    assert [item.chat for item in s.hub.router.sessions("dingtalk")] == [OWNER]


def test_a_message_pushed_twice_is_handled_once(bot):
    s = bot(reply(text("只回答一次")))
    start = len(s.fake.sent)
    message = s.fake.user_says("你好")
    s.fake.push(message)  # 钉钉 3 秒内没收到回执，又推了一遍
    s.wait_reply(start, "只回答一次")
    s.sentinel()
    s.idle()
    assert len(s.api.requests) == 1
    assert sum("只回答一次" in item["plain"] for item in s.fake.sent) == 1


# ---- 确认：回复文字、网页 ---------------------------------------------------------------


def test_a_confirmation_shows_the_whole_command_and_is_answered_with_words(bot):
    s = bot(run("toolu_1", "echo hi"), reply(text("做完了")))
    start = s.say("跑个命令")
    ask = s.asked(start)
    assert (ask["via"], ask["msgtype"], ask["title"]) == ("webhook", "markdown", "助手想要执行命令：")
    assert ask["text"] == f"**助手想要执行命令：**\n\n```\n$ echo hi\n```\n\n工作目录：{s.config.workspace}\n\n回复“允许”或“拒绝”。"
    (pending,) = until(lambda: s.state()["channels"]["approvals"], what="页面上的确认请求")
    assert (pending["platform"], pending["chat"], pending["name"]) == ("dingtalk", OWNER, OWNER_NAME)

    s.say("好的")  # 三种语言里表示同意的话都认
    s.wait_reply(start, "做完了")
    assert said(s, "已允许", start)["msgtype"] == "text"  # 钉钉的消息改不了：另发一句结果
    (result,) = tool_results(s.api.requests[1])
    assert result_text(result).strip() == "hi"
    s.idle()


def test_denying_with_words(bot):
    s = bot(run("toolu_1", "rm -rf build"), reply(text("好的，不删了。")))
    start = s.say("清理一下")
    s.asked(start)
    s.say("拒绝")
    s.wait_reply(start, "好的，不删了")
    said(s, "已拒绝", start)
    (result,) = tool_results(s.api.requests[1])
    assert result_text(result).startswith("用户拒绝了这个操作") and result["is_error"] is True
    s.idle()


def test_a_long_command_is_shown_in_full_before_the_question(bot):
    command = "echo " + " ".join(f"part{n:04d}" for n in range(900))  # 比一条消息长
    s = bot(run("toolu_1", command), reply(text("完")))
    start = s.say("跑个很长的命令")
    ask = s.asked(start)
    pieces = s.fake.sent[start : s.fake.sent.index(ask) + 1]
    assert len(pieces) >= 2 and pieces[0]["text"].startswith("**助手想要执行命令：**")
    assert all(len(piece["text"]) <= SAFE_LIMIT for piece in pieces)
    shown = "".join(piece["text"] for piece in pieces)
    assert all(f"part{n:04d}" in shown for n in range(900))  # 命令一个字都没少
    s.say("允许")
    s.wait_reply(start, "完")
    s.idle()


def test_the_web_and_the_chat_race_for_the_same_confirmation(bot):
    s = bot(run("toolu_a", "echo a"), run("toolu_b", "echo b"), reply(text("完")))
    start = s.say("做两件事")
    first = s.asked(start)
    (pending,) = until(lambda: s.state()["channels"]["approvals"], what="页面上的确认请求")
    answer = {"platform": "dingtalk", "chat": OWNER, "confirm_id": pending["id"], "allow": True}

    # 先在网页上允许：钉钉里说一句"已在网页上允许"
    assert post(s, "confirm", **answer) == (200, {"ok": True})
    said(s, "已在网页上允许", start)

    # 反过来：先在钉钉里回了"允许"，网页上再点就是 409
    second = s.asked(s.fake.sent.index(first) + 1)
    confirm_id = s.session().waiting.id
    s.say("允许")
    said(s, "已允许", s.fake.sent.index(second))
    status, body = post(s, "confirm", **{**answer, "confirm_id": confirm_id})
    assert status == 409 and "已经处理过了" in body["error"]
    s.wait_reply(start, "完")
    assert len(s.api.requests) == 3  # 每条命令只执行了一次
    s.idle()


# ---- 命令 ----------------------------------------------------------------------


def test_help_status_new_and_stop(bot):
    s = bot(run("toolu_1", "echo 不该运行 > ran.txt"), reply(text("第二段的回答")))
    help_message = s.wait_reply(s.say("/help"), "直接发消息就行")
    assert help_message["msgtype"] == "text" and "/new" in help_message["plain"] and "回复“允许”或“拒绝”" in help_message["plain"]
    assert "现在：空闲" in s.wait_reply(s.say("/status"), "现在：")["plain"]
    assert s.api.requests == []  # 命令不经过模型

    start = s.say("跑个命令")
    s.asked(start)
    assert "现在：等你批准" in s.wait_reply(s.say("/status"), "现在：")["plain"]
    s.wait_reply(s.say("/new"), "这一轮还没结束")
    stopping = s.say("/stop")
    s.wait_reply(stopping, "好，正在停下")
    said(s, "已中断", start)
    s.idle()
    assert not (s.config.workspace / "ran.txt").exists()
    s.wait_reply(s.say("/stop"), "现在没有在进行的事")

    first = s.session().conversation.id
    s.wait_reply(s.say("/new"), "开始一段新对话")
    s.wait_reply(s.say("第二段"), "第二段的回答")
    s.idle()
    assert s.session().conversation.id != first and len(s.api.requests[-1]["messages"]) == 1


# ---- 回复走哪条路 ----------------------------------------------------------------


def test_a_reply_goes_through_the_session_webhook_as_markdown(bot):
    answer = "## 结论\n\n**一切正常**，详见[文档](https://example.com/a)。\n\n- 第一\n- 第二"
    s = bot(reply(text(answer)))
    message = s.wait_reply(s.say("看看"), "一切正常")
    assert (message["via"], message["msgtype"], message["chat"]) == ("webhook", "markdown", OWNER)
    assert message["text"] == answer and message["title"] == "结论"  # 标题是会话列表里显示的那一行：第一行的纯文字
    assert s.fake.batches == []
    s.idle()


def test_a_long_reply_is_split(bot):
    answer = "\n\n".join(f"第 {n} 段：" + "这是一段很长的回答，每一句都要完整地送到。" * 8 for n in range(1, 31))
    s = bot(reply(text(answer)))
    start = s.say("讲长一点")
    s.wait_reply(start, "第 30 段")
    s.idle()
    pieces = [message for message in s.fake.sent[start:] if message["msgtype"] == "markdown"]
    assert len(pieces) >= 2 and all(len(piece["text"]) <= SAFE_LIMIT for piece in pieces)
    assert "".join(piece["text"] for piece in pieces).replace("\n", "") == answer.replace("\n", "")


def test_an_expired_session_webhook_is_not_used_and_the_reply_is_sent_directly(bot):
    s = bot(reply(text("**过了很久**才写完的回答")))
    message = s.wait_reply(s.say("你好", expired=True), "过了很久")
    assert (message["via"], message["msgtype"], message["chat"], message["title"]) == ("batch", "markdown", OWNER, "过了很久才写完的回答")
    (batch,) = s.fake.batches
    assert (batch["robotCode"], batch["userIds"], batch["msgKey"]) == (CLIENT_ID, [OWNER], "sampleMarkdown")
    assert json.loads(batch["msgParam"]) == {"title": "过了很久才写完的回答", "text": "**过了很久**才写完的回答"}
    assert "POST /robot/sendBySession" not in s.fake.requests  # 写着过期了，就不去试
    s.idle()


def test_when_the_session_webhook_fails_the_reply_is_sent_directly(bot):
    s = bot(reply(text("换了一条路")))
    s.fake.webhooks_work = False  # 钉钉提前作废了它
    message = s.wait_reply(s.say("你好"), "换了一条路")
    assert message["via"] == "batch" and "POST /robot/sendBySession" in s.fake.requests
    notice = s.wait_reply(s.say("/status"), "现在：")  # 不是 Markdown 的话，主动发时是 sampleText
    assert notice["msgtype"] == "text" and s.fake.batches[-1]["msgKey"] == "sampleText"
    s.idle()


def test_only_dingtalk_hosts_are_trusted_for_the_session_webhook():
    assert dingtalk._webhook_ok("https://oapi.dingtalk.com/robot/sendBySession?session=abc")
    assert dingtalk._webhook_ok("http://127.0.0.1:8080/robot/sendBySession?session=abc")  # 本机：测试里的假钉钉
    for url in (
        "https://webhook.example.com/robot/sendBySession?session=abc",
        "https://oapi.dingtalk.com.evil.example/robot/sendBySession",  # 名字里带着 dingtalk.com，但不是它
        "https://evildingtalk.com/robot/sendBySession",
        "https://evil.example/?next=oapi.dingtalk.com",
        "http://oapi.dingtalk.com/robot/sendBySession",  # 明文的 http：只认本机
        "http://192.168.1.10/robot/sendBySession",
        "ftp://oapi.dingtalk.com/robot",
        "",
    ):
        assert not dingtalk._webhook_ok(url), url


def test_a_session_webhook_on_another_host_is_never_contacted(bot, monkeypatch):
    s = bot(reply(text("走了另一条路")))
    asked: list[str] = []
    real = http.request

    def recording(method, url, **options):
        asked.append(url)
        return real(method, url, **options)

    monkeypatch.setattr(http, "request", recording)
    message = s.wait_reply(s.say("你好", webhook="https://webhook.example.com/robot/sendBySession?session=stolen"), "走了另一条路")
    assert message["via"] == "batch"
    assert asked and all(url.startswith(s.fake.base_url) for url in asked)  # 一次都没有往别人的服务器上发
    s.idle()


# ---- 图片、文件、富文本、处理不了的 --------------------------------------------------------


def test_a_picture_reaches_the_model(bot):
    s = bot(reply(text("是一张很小的图")))
    s.wait_reply(s.say(picture=JPEG_BYTES), "是一张很小的图")
    content = s.api.requests[0]["messages"][-1]["content"]
    (image,) = [block for block in content if block["type"] == "image"]
    assert image["source"] == {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(JPEG_BYTES).decode()}
    assert len(s.fake.downloads) == 1  # 先用 downloadCode 换下载地址，再下载
    s.idle()


def test_a_text_file_reaches_the_model(bot):
    s = bot(reply(text("读完了")))
    s.wait_reply(s.say(file=("notes.txt", "第一行\n第二行 ✓".encode())), "读完了")
    sent = json.dumps(s.api.requests[0]["messages"][-1], ensure_ascii=False)
    assert "第二行 ✓" in sent and "notes.txt" in sent
    s.idle()


def test_a_rich_text_message_brings_its_text_and_pictures(bot):
    s = bot(reply(text("两张图都看到了")))
    s.wait_reply(s.say(rich=["看这两张图：", JPEG_BYTES, "第二张", JPEG_BYTES]), "两张图都看到了")
    content = s.api.requests[0]["messages"][-1]["content"]
    assert [block["type"] for block in content].count("image") == 2
    assert any(block["type"] == "text" and block["text"] == "看这两张图：第二张" for block in content)
    assert len(s.fake.downloads) == 2
    s.idle()


@pytest.mark.parametrize(("msgtype", "words"), [("audio", "还处理不了语音"), ("video", "还处理不了视频")])
def test_things_it_cannot_handle_get_an_explanation(bot, msgtype, words):
    s = bot()
    s.wait_reply(s.say(msgtype=msgtype), words)
    s.idle()
    assert s.api.requests == [] and s.fake.downloads == []


def test_an_unknown_kind_of_message_is_ignored(bot):
    s = bot()
    before = len(s.fake.sent)
    s.fake.user_says(msgtype="interactiveCard")
    s.sentinel()
    assert len(s.fake.sent) == before + 1  # 只有 /help 的回答


# ---- 密钥不外泄 -----------------------------------------------------------------


def test_secrets_and_the_session_webhook_never_leak(bot, caplog):
    caplog.set_level(logging.DEBUG, logger="harness")
    s = bot(reply(text("好")), reply(text("换了一条路")))
    s.wait_reply(s.say("你好"), "好")
    s.fake.webhooks_work = False  # 回复改成主动发：日志里写一句为什么，但不写 sessionWebhook 的地址
    s.wait_reply(s.say("再来"), "换了一条路")
    s.idle()
    token = s.fake.tokens[-1]
    s.fake.batch_error = (500, {"code": "ServiceUnavailable", "message": f"token {token} secret {CLIENT_SECRET}"})  # 钉钉在错误里带上了密钥
    s.say("/status")
    until(lambda: "发送失败" in caplog.text, what="日志里的发送失败")
    s.sentinel()
    everything = caplog.text + json.dumps(s.browser.events, ensure_ascii=False)
    for secret in s.fake.secrets:
        assert secret not in everything
    assert "sendBySession" not in caplog.text and "sessionWebhook 发不出去" in caplog.text
    assert "<hidden>" in caplog.text


def test_the_export_leaves_out_the_client_secret(tmp_path):
    from test_export import export_bytes

    from harness.export import PLACEHOLDER

    data = tmp_path / "data"
    store = ChannelStore(data / "channels.json")
    store.connect("dingtalk", {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}, FakeDingTalk().bot)
    raw, _ = export_bytes(data)
    archive = zipfile.ZipFile(io.BytesIO(raw))
    entry = json.loads(archive.read("channels.json"))["platforms"]["dingtalk"]
    assert entry["credentials"] == {"client_id": CLIENT_ID, "client_secret": PLACEHOLDER}
    assert CLIENT_SECRET.encode() not in b"".join(archive.read(name) for name in archive.namelist())


# ---- 真的 SDK：连假钉钉的 Stream 网关（装了 dingtalk-stream 才跑）--------------------------------


def test_the_real_sdk_receives_a_message_acknowledges_it_and_stops(fake, caplog):
    pytest.importorskip("dingtalk_stream")
    caplog.set_level(logging.DEBUG, logger="harness")
    listener = StreamListener(CLIENT_ID, CLIENT_SECRET, endpoint=fake.base_url)
    got: list[dict] = []
    states: list[tuple[str, str]] = []
    outcome: list[str] = []
    worker = threading.Thread(target=lambda: outcome.append(listener.run(got.append, lambda *state: states.append(state))), daemon=True)
    worker.start()
    fake.wait_until(lambda: fake.sockets, timeout=10)  # 换了票据、连上了 WebSocket
    (opened,) = fake.opens
    assert opened["clientId"] == CLIENT_ID and opened["subscriptions"] == [{"type": "CALLBACK", "topic": TOPIC}]
    assert ("online", "") in states
    assert fake.sockets[0].path == "/stream?ticket=" + fake.tickets[0]

    message = fake.message("你好")
    fake.stream_push(message, message_id="m1")
    until(lambda: got, what="SDK 交来消息")
    assert got == [message]  # 和钉钉文档里的 JSON 一样的字典
    ack = fake.wait_until(lambda: next((item for item in fake.acks if item.get("headers", {}).get("messageId") == "m1"), None))
    assert ack["code"] == 200 and json.loads(ack["data"]) == {"response": "OK"}  # 回执：钉钉 3 秒内收不到会再推一遍

    began = time.monotonic()
    listener.stop()  # SDK 会吞掉第一次 cancel：要真的停下
    worker.join(5)
    assert not worker.is_alive() and outcome == ["stopped"] and time.monotonic() - began < 3
    assert len(fake.opens) == 1  # 停下以后没有再去换票据
    assert fake.tickets[0] not in caplog.text and CLIENT_SECRET not in caplog.text  # SDK 的日志会打印票据：被挡住了
    # 是我们自己关的：SDK 以为断网了报的那两句错误不记进日志
    assert [record.getMessage() for record in caplog.records if record.name == "harness.channels.dingtalk.sdk"] == []


def test_the_whole_way_with_the_real_sdk(bot):
    pytest.importorskip("dingtalk_stream")
    bot.fake.use_sdk = True  # 通道用真的 StreamListener，经过假钉钉的 Stream 网关收消息
    s = bot(reply(text("从 Stream 来的回答")))
    assert isinstance(s.channel().listener, StreamListener)
    s.wait_reply(s.say("你好"), "从 Stream 来的回答")
    assert len(s.fake.acks) == 1 and s.fake.acks[0]["code"] == 200
    s.idle()


def test_with_the_real_sdk_a_secret_reset_while_connected_shows_an_error(bot):
    pytest.importorskip("dingtalk_stream")
    bot.fake.use_sdk = True
    s = bot()
    s.fake.wait_until(lambda: s.fake.sockets)
    s.fake.client_secret = "the-new-secret-after-a-reset-0123"  # 在开放平台上重置了 Secret，钉钉关上了连接
    s.fake.hang_up()
    # SDK 在它自己里面重连（不回到 _run 的检查）：换票据被拒绝，页面上要说清楚，而不是一直"正在重试"
    until(lambda: s.platform()["status"]["state"] == "error", what="页面上的错误")
    assert "不认这个 Client ID 和 Client Secret" in s.platform()["status"]["message"]
    assert len(s.fake.opens) == 1  # 第二次被拒绝了，没有记下
