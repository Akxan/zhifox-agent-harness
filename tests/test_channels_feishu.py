"""飞书通道的测试：检查 App ID 和 App Secret、发消息和卡片、下载文件、问名字，以及一整条"手机 → 智狐 → 手机"的路。

对着本地的假飞书（tests/fake_feishu.py）：REST 是真的 HTTP，长连接换成 FakeLarkListener（交给通道的事件和 SDK 交的一样）。
组装见 tests/channel_rig.py，测试代码扮演手机上的你（s.say、s.fake.press）和浏览器（s.browser）。
最后一节用真的 SDK（lark-oapi）把飞书推来的 JSON 变成事件，看两边接得上；没装 SDK（比如 CI 上）就跳过。
不连真正的飞书，不花钱。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import threading
import zipfile

import pytest
from channel_rig import rig_fixture
from fake_api import reply, text
from fake_feishu import (
    APP_ID,
    APP_SECRET,
    BOT_NAME,
    OWNER,
    OWNER_CHAT,
    OWNER_NAME,
    STRANGER,
    FakeFeishu,
    card_elements,
)
from sample_files import JPEG_BYTES
from test_channels import result_text, run, stop_later, tool_results, until

from harness.channels import feishu, platforms
from harness.channels.base import ChannelError
from harness.channels.feishu import SAFE_LIMIT, FeishuChannel, LarkListener
from harness.channels.store import ChannelStore

WRONG_SECRET = "WRONGfeishuSECRET0123456789abcde"
TOKEN_URL = "POST /open-apis/auth/v3/tenant_access_token/internal"


@pytest.fixture
def fake():
    server = FakeFeishu().start()
    yield server
    stop_later(server.stop)


@pytest.fixture
def bot(make_config, monkeypatch):
    yield from rig_fixture(make_config, monkeypatch, lambda: FakeFeishu().start())


def sdk(monkeypatch, ready: bool) -> None:
    """当作飞书的 SDK 装了（或者没装）。CI 上没装它；这里测的是我们自己的代码怎么对待"装没装"，不是 SDK。"""
    real = platforms.sdk_ready
    monkeypatch.setattr(platforms, "sdk_ready", lambda platform: ready if platform == "feishu" else real(platform))


def post(s, action: str, **body) -> tuple[int, dict]:
    return s.browser.post(f"/api/channel/{action}", body)


def markdowns(card: dict) -> list[str]:
    return [element["content"] for element in card_elements(card) if element.get("tag") == "markdown"]


def answer_of(button: dict) -> str:
    return button["behaviors"][0]["value"]["answer"]


# ---- 检查 App ID 和 App Secret ------------------------------------------------------


def test_a_malformed_app_id_or_secret_is_rejected_without_asking_feishu(fake, make_config):
    config = make_config(feishu_api=fake.base_url)
    with pytest.raises(ChannelError, match="cli_"):
        feishu.check(config, {"app_id": "a5fake0test0app1", "app_secret": APP_SECRET})
    with pytest.raises(ChannelError, match="太短"):
        feishu.check(config, {"app_id": APP_ID, "app_secret": "short"})
    assert fake.requests == []


def test_a_wrong_secret_is_explained_without_repeating_it(fake, make_config):
    config = make_config(feishu_api=fake.base_url)
    with pytest.raises(ChannelError) as caught:
        feishu.check(config, {"app_id": APP_ID, "app_secret": WRONG_SECRET})
    assert "不认这个 App ID 和 App Secret" in str(caught.value) and WRONG_SECRET not in str(caught.value)
    assert fake.requests == [TOKEN_URL]


def test_check_returns_the_bot_and_explains_an_app_without_one(fake, make_config):
    config = make_config(feishu_api=fake.base_url)
    assert feishu.check(config, {"app_id": f" {APP_ID} ", "app_secret": f"{APP_SECRET}\n"}) == fake.bot  # 抄的时候多了空白也认
    fake.has_bot = False
    with pytest.raises(ChannelError, match="还没有机器人"):
        feishu.check(config, {"app_id": APP_ID, "app_secret": APP_SECRET})


# ---- 在页面上连接；没装 SDK -----------------------------------------------------------


def test_connecting_from_the_page(bot, monkeypatch):
    sdk(monkeypatch, True)
    s = bot(connected=False)
    entry = s.platform()
    assert entry["sdk"] == {"module": "lark_oapi", "package": "lark-oapi", "extra": "feishu", "ready": True, "install": "uv sync --extra feishu", "message": ""}
    assert [field["id"] for field in entry["fields"]] == ["app_id", "app_secret"] and entry["buttons"] is True

    status, body = post(s, "connect", platform="feishu", credentials={"app_id": APP_ID, "app_secret": WRONG_SECRET})
    assert status == 409 and "不认这个 App ID 和 App Secret" in body["error"] and WRONG_SECRET not in body["error"]
    assert not s.config.channels_file.exists()  # 不对的不保存

    status, body = post(s, "connect", platform="feishu", credentials={"app_id": APP_ID, "app_secret": APP_SECRET})
    assert status == 200 and body["bot"]["handle"] == BOT_NAME  # 飞书的机器人没有 @用户名，显示它的名字
    until(lambda: s.platform()["status"]["state"] == "online", what="连上")
    entry = s.platform()
    assert entry["hints"] == {"app_id": APP_ID, "app_secret": "…" + APP_SECRET[-4:]}  # App ID 不是密钥，照常显示
    assert entry["bot"]["link"] == f"https://applink.feishu.cn/client/bot/open?appId={APP_ID}"
    assert ChannelStore(s.config.channels_file).credentials("feishu") == {"app_id": APP_ID, "app_secret": APP_SECRET}


def test_without_the_sdk_connecting_is_refused_and_says_how_to_install_it(bot, monkeypatch):
    sdk(monkeypatch, False)
    s = bot(connected=False)
    entry = s.platform()["sdk"]
    assert entry["ready"] is False and entry["install"] == "uv sync --extra feishu"
    assert "lark-oapi" in entry["message"] and "uv sync --extra feishu" in entry["message"]
    status, body = post(s, "connect", platform="feishu", credentials={"app_id": APP_ID, "app_secret": APP_SECRET})
    assert status == 409 and "uv sync --extra feishu" in body["error"]
    assert s.fake.requests == [] and not s.config.channels_file.exists()  # 没去问飞书，也没保存


def test_a_real_channel_without_the_sdk_shows_an_error(monkeypatch):
    sdk(monkeypatch, False)
    channel = FeishuChannel(APP_ID, APP_SECRET, "http://127.0.0.1:9")  # 没有换掉长连接：要用真的 SDK
    channel.start(lambda event: None)
    until(lambda: channel.status.state == "error", what="报错")
    assert "uv sync --extra feishu" in channel.status.message
    channel.stop()
    channel.join(2)


# ---- 配对、陌生人、群聊、重复推送 ---------------------------------------------------------


def test_pairing_in_a_private_chat(bot):
    s = bot(paired=False)
    code = s.hub.pair("feishu")["code"]
    paired = s.wait_reply(s.say(code), "配对成功")
    assert paired["chat"] == OWNER_CHAT and OWNER_NAME in paired["plain"]  # 名字是从通讯录里问来的
    owner = s.hub.store.get("feishu")["owner"]
    assert (owner["id"], owner["name"]) == (OWNER, OWNER_NAME)
    assert "现在：空闲" in s.wait_reply(s.say("/status"), "现在：")["plain"]


def test_strangers_bots_and_groups_are_ignored_silently(bot):
    s = bot()
    before = len(s.fake.sent)
    s.fake.user_says("你好", user=STRANGER)
    s.fake.user_says("123456", user=STRANGER)
    s.fake.user_says("/help", private=False, chat="oc_group0000000000000000000000001")  # 主人在群里说话：一律不理
    s.fake.user_says("你好", sender_type="app")  # 别的应用（机器人）发的
    s.sentinel()
    assert [message["chat"] for message in s.fake.sent[before:]] == [OWNER_CHAT]  # 只有主人单聊里的 /help 有回答
    assert s.api.requests == []
    until(lambda: s.platform()["ignored"] == 2, what="页面上的计数")  # 只数单聊里陌生人发来的
    assert [item.chat for item in s.hub.router.sessions("feishu")] == [OWNER_CHAT]


def test_a_message_pushed_twice_is_handled_once(bot):
    s = bot(reply(text("只回答一次")))
    start = len(s.fake.sent)
    message_id = s.fake.user_says("你好")
    s.fake.user_says("你好", message_id=message_id)  # 飞书没收到回执，又推了一遍
    s.wait_reply(start, "只回答一次")
    s.sentinel()
    s.idle()
    assert len(s.api.requests) == 1
    assert sum("只回答一次" in message["plain"] for message in s.fake.sent) == 1


# ---- 确认：卡片上的按钮、回复文字、网页 ----------------------------------------------------


def test_allowing_with_the_card_button(bot):
    s = bot(run("toolu_1", "echo hi"), reply(text("做完了")))
    start = s.say("跑个命令")
    ask = s.asked(start)
    card = ask["card"]
    assert ask["chat"] == OWNER_CHAT and card["header"]["title"]["content"].startswith("助手想要") and card["header"]["template"] == "orange"
    assert markdowns(card)[:2] == ["```\n$ echo hi\n```", f"工作目录：{s.config.workspace}"]  # 完整的命令在代码块里，下面是工作目录
    allow, deny = s.fake.buttons(ask)
    confirm_id = int(answer_of(allow).removeprefix("ok:"))
    assert (allow["text"]["content"], allow["type"], deny["text"]["content"], deny["type"]) == ("允许", "primary", "拒绝", "danger")
    assert answer_of(deny) == f"no:{confirm_id}"

    assert s.fake.press(ask) == {"toast": {"type": "success", "content": "已允许"}}  # 飞书在屏幕上弹一下
    s.wait_reply(start, "做完了")
    edit = s.fake.edit_of(ask)
    assert not s.fake.buttons({"card": edit["card"]}) and edit["card"]["header"]["template"] == "grey"  # 按钮去掉了，卡片变灰
    assert markdowns(edit["card"])[-1] == "**已允许**" and markdowns(edit["card"])[0] == "```\n$ echo hi\n```"
    (result,) = tool_results(s.api.requests[1])
    assert result_text(result).strip() == "hi"
    s.idle()


def test_denying_with_the_card_button(bot):
    s = bot(run("toolu_1", "rm -rf build"), reply(text("好的，不删了。")))
    start = s.say("清理一下")
    ask = s.asked(start)
    assert s.fake.press(ask, allow=False) == {"toast": {"type": "success", "content": "已拒绝"}}
    s.wait_reply(start, "好的，不删了")
    assert markdowns(s.fake.edit_of(ask)["card"])[-1] == "**已拒绝**"
    (result,) = tool_results(s.api.requests[1])
    assert result_text(result).startswith("用户拒绝了这个操作") and result["is_error"] is True
    s.idle()


def test_answering_with_words_instead_of_buttons(bot):
    s = bot(run("toolu_a", "echo a"), run("toolu_b", "echo b"), reply(text("两件事都处理完了")))
    start = s.say("做两件事")
    first = s.asked(start)
    s.say("允许")
    second = s.asked(s.fake.sent.index(first) + 1)
    s.say("拒绝")
    s.wait_reply(start, "两件事都处理完了")
    assert [markdowns(s.fake.edit_of(ask)["card"])[-1] for ask in (first, second)] == ["**已允许**", "**已拒绝**"]
    assert result_text(tool_results(s.api.requests[1])[0]).strip() == "a"
    assert result_text(tool_results(s.api.requests[2])[0]).startswith("用户拒绝了这个操作")
    s.idle()


def test_a_stranger_pressing_the_button_does_nothing(bot):
    s = bot(run("toolu_1", "echo hi"), reply(text("做完了")))
    start = s.say("跑")
    ask = s.asked(start)
    assert s.fake.press(ask, user=STRANGER) == {}  # 别人点的：不算数，也不提示
    assert s.session().waiting is not None
    s.fake.press(ask)
    s.wait_reply(start, "做完了")
    s.idle()


def test_the_web_and_the_card_race_for_the_same_confirmation(bot):
    s = bot(run("toolu_a", "echo a"), run("toolu_b", "echo b"), reply(text("完")))
    start = s.say("做两件事")
    first = s.asked(start)
    (pending,) = until(lambda: s.state()["channels"]["approvals"], what="页面上的确认请求")
    assert (pending["platform"], pending["chat"], pending["name"]) == ("feishu", OWNER_CHAT, OWNER_NAME)

    # 先在网页上允许：卡片改成"已在网页上允许"，再点卡片上的按钮只弹一句"已经处理过了"
    answer = {"platform": "feishu", "chat": OWNER_CHAT, "confirm_id": pending["id"], "allow": True}
    assert post(s, "confirm", **answer) == (200, {"ok": True})
    assert markdowns(s.fake.edit_of(first)["card"])[-1] == "**已在网页上允许**"
    assert s.fake.press(first) == {"toast": {"type": "warning", "content": "这个确认请求已经处理过了"}}

    # 反过来：先点了卡片，网页上再点就是 409
    second = s.asked(s.fake.sent.index(first) + 1)
    assert s.fake.press(second) == {"toast": {"type": "success", "content": "已允许"}}
    confirm_id = int(answer_of(s.fake.buttons(second)[0]).removeprefix("ok:"))
    status, body = post(s, "confirm", **{**answer, "confirm_id": confirm_id})
    assert status == 409 and "已经处理过了" in body["error"]
    s.wait_reply(start, "完")
    assert len(s.api.requests) == 3  # 每条命令只执行了一次
    s.idle()


def test_when_the_card_cannot_be_sent_the_prompt_falls_back_to_text(bot):
    s = bot(run("toolu_1", "echo hi"), reply(text("做完了")))
    s.fake.fail("interactive")  # 卡片发不出去（比如机构的管理员关掉了卡片）
    start = s.say("跑个命令")
    ask = s.asked(start)
    assert ask["msg_type"] == "post"
    (row,) = ask["content"]["zh_cn"]["content"]
    assert row[0]["text"].startswith("**助手想要") and "```\n$ echo hi\n```" in row[0]["text"]
    assert ask["plain"].endswith("回复“允许”或“拒绝”。")
    s.say("允许")
    s.wait_reply(start, "做完了")
    assert s.fake.wait_sent(lambda message: message["plain"] == "已允许", after=s.fake.sent.index(ask))["msg_type"] == "text"  # 文字改不了：另发一条结果
    assert s.fake.edits == []
    s.idle()


def test_when_the_card_cannot_be_updated_the_outcome_is_sent_as_text(bot):
    s = bot(run("toolu_1", "echo hi"), reply(text("做完了")))
    start = s.say("跑")
    ask = s.asked(start)
    s.fake.fail("PATCH")
    s.fake.press(ask)
    s.wait_reply(start, "做完了")
    s.fake.wait_sent(lambda message: message["plain"] == "已允许" and message["msg_type"] == "text", after=start)
    s.idle()


# ---- 命令 ----------------------------------------------------------------------


def test_help_status_new_and_stop(bot):
    s = bot(run("toolu_1", "echo 不该运行 > ran.txt"), reply(text("第二段的回答")))
    help_message = s.wait_reply(s.say("/help"), "直接发消息就行")
    assert help_message["msg_type"] == "text" and "/new" in help_message["plain"] and "“允许 / 拒绝”按钮" in help_message["plain"]
    assert "现在：空闲" in s.wait_reply(s.say("/status"), "现在：")["plain"]
    assert s.api.requests == []  # 命令不经过模型

    ask = s.asked(s.say("跑个命令"))
    assert "现在：等你批准" in s.wait_reply(s.say("/status"), "现在：")["plain"]
    s.wait_reply(s.say("/new"), "这一轮还没结束")
    stopping = s.say("/stop")
    s.wait_reply(stopping, "好，正在停下")
    assert markdowns(s.fake.edit_of(ask)["card"])[-1] == "**已中断**"
    s.idle()
    assert not (s.config.workspace / "ran.txt").exists()
    # 卡片已经处理过了：再点只弹一句提示，不会执行
    assert s.fake.press(ask) == {"toast": {"type": "warning", "content": "这个确认请求已经处理过了"}}
    s.wait_reply(s.say("/stop"), "现在没有在进行的事")

    first = s.session().conversation.id
    s.wait_reply(s.say("/new"), "开始一段新对话")
    s.wait_reply(s.say("第二段"), "第二段的回答")
    s.idle()
    assert s.session().conversation.id != first and len(s.api.requests[-1]["messages"]) == 1


# ---- 回复的样子 ------------------------------------------------------------------


def test_a_markdown_reply_is_a_post_with_one_md_tag(bot):
    answer = "## 结论\n\n**一切正常**，看这里：[文档](https://example.com/a_b)\n\n- 第一\n- 第二\n\n```python\nprint('<b>&</b>')\n```"
    s = bot(reply(text(answer)))
    message = s.wait_reply(s.say("看看"), "一切正常")
    assert message["msg_type"] == "post"
    assert message["content"] == {"zh_cn": {"content": [[{"tag": "md", "text": answer}]]}}  # 原样交给飞书排版，一个字都不改
    s.idle()


def test_a_long_reply_is_split_into_posts_feishu_accepts(bot):
    answer = "\n\n".join(f"第 {n} 段：" + "这是一段很长的回答，每一句都要完整地送到。" * 12 for n in range(1, 41))
    s = bot(reply(text(answer)))
    start = s.say("讲长一点")
    s.wait_reply(start, "第 40 段")
    s.idle()
    pieces = [message for message in s.fake.sent[start:] if message["msg_type"] == "post"]
    assert len(pieces) >= 2 and s.fake.rejected == []
    texts = [message["content"]["zh_cn"]["content"][0][0]["text"] for message in pieces]
    assert all(len(piece.encode("utf-16-le")) // 2 <= SAFE_LIMIT for piece in texts)
    assert "".join(texts).replace("\n", "") == answer.replace("\n", "")  # 拆开了，一个字都没丢


# ---- 图片、文件、富文本、处理不了的 --------------------------------------------------------


def test_an_image_reaches_the_model(bot):
    s = bot(reply(text("是一张很小的图")))
    s.wait_reply(s.say(image=JPEG_BYTES), "是一张很小的图")
    content = s.api.requests[0]["messages"][-1]["content"]
    (image,) = [block for block in content if block["type"] == "image"]
    assert image["source"] == {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(JPEG_BYTES).decode()}
    ((_message_id, key, kind, token),) = s.fake.downloads
    assert kind == "image" and key.startswith("img_") and token in s.fake.tokens  # 带着 tenant_access_token 下载
    s.idle()


def test_a_text_file_reaches_the_model(bot):
    s = bot(reply(text("读完了")))
    s.wait_reply(s.say(file=("notes.txt", "第一行\n第二行 ✓".encode())), "读完了")
    sent = json.dumps(s.api.requests[0]["messages"][-1], ensure_ascii=False)
    assert "第二行 ✓" in sent and "notes.txt" in sent
    assert [item[2] for item in s.fake.downloads] == ["file"]
    s.idle()


def test_a_rich_text_message_brings_its_text_and_images(bot):
    s = bot(reply(text("看到了图和链接")))
    rich = {"title": "看这个", "content": [
        [{"tag": "text", "text": "说明在"}, {"tag": "a", "text": "这里", "href": "https://example.com/doc"}, {"tag": "at", "user_id": "@_user_1", "user_name": BOT_NAME}],
        [{"tag": "img", "image": JPEG_BYTES}],
    ]}
    s.wait_reply(s.say(post=rich), "看到了图和链接")
    content = s.api.requests[0]["messages"][-1]["content"]
    words = "".join(block["text"] for block in content if block["type"] == "text")
    assert "看这个\n说明在这里（https://example.com/doc）" in words
    assert [block["type"] for block in content].count("image") == 1
    s.idle()


@pytest.mark.parametrize(("kind", "said"), [("audio", "还处理不了语音"), ("media", "还处理不了视频"), ("sticker", "还处理不了贴纸")])
def test_things_it_cannot_handle_get_an_explanation(bot, kind, said):
    s = bot()
    s.wait_reply(s.say(kind=kind), said)
    s.idle()
    assert s.api.requests == [] and s.fake.downloads == []


# ---- 名字、token 过期 -------------------------------------------------------------


def test_the_name_comes_from_the_contacts_once_per_person(bot):
    s = bot(reply(text("一")), reply(text("二")))
    s.wait_reply(s.say("第一句"), "一")
    s.wait_reply(s.say("第二句"), "二")
    s.sentinel()
    s.idle()
    assert s.fake.lookups == [OWNER]  # 问过一次就记下来
    assert s.session().name == OWNER_NAME
    (chat,) = s.platform()["chats"]
    assert chat["name"] == OWNER_NAME  # 页面上的聊天列表里也是这个名字


def test_without_permission_to_read_contacts_the_id_is_shown(bot):
    bot.fake.contacts = False
    s = bot()
    s.sentinel()
    s.sentinel()
    assert s.fake.lookups == [OWNER]  # 问不到也只问一次
    assert s.session().name == f"飞书用户 {OWNER[-6:]}"


def test_an_expired_token_is_replaced_without_you_noticing(bot):
    s = bot(reply(text("第一次")), reply(text("看到了")))
    s.wait_reply(s.say("你好"), "第一次")
    s.idle()
    old = s.fake.tokens[-1]
    s.fake.expire_tokens()  # 飞书那边作废了，通道里记着的还没到期
    s.wait_reply(s.say(image=JPEG_BYTES), "看到了")  # 下载图片、发回答都换了新的 token 再试
    s.idle()
    assert len(s.fake.tokens) >= 2 and s.fake.tokens[-1] != old
    assert s.fake.rejected_tokens and [item[3] for item in s.fake.downloads][-1] == s.fake.tokens[-1]


# ---- 密钥不外泄 -----------------------------------------------------------------


def test_the_app_secret_and_the_tokens_never_leak(bot, caplog):
    caplog.set_level(logging.DEBUG, logger="harness")
    s = bot(reply(text("好")))
    s.wait_reply(s.say("你好"), "好")
    s.idle()
    token = s.fake.tokens[-1]
    s.fake.fail("text", code=99999, message=f"bad request: token {token}, secret {APP_SECRET}")  # 飞书在错误说明里带上了密钥
    s.say("/status")
    until(lambda: "发送失败" in caplog.text, what="日志里的发送失败")
    s.sentinel()
    everything = caplog.text + json.dumps(s.browser.events, ensure_ascii=False)
    for secret in s.fake.secrets:
        assert secret not in everything
    assert "<hidden>" in caplog.text  # 换成了占位符，还看得出这里本来有东西


def test_the_export_leaves_out_the_app_secret(tmp_path):
    from test_export import export_bytes

    from harness.export import PLACEHOLDER

    data = tmp_path / "data"
    store = ChannelStore(data / "channels.json")
    store.connect("feishu", {"app_id": APP_ID, "app_secret": APP_SECRET}, FakeFeishu().bot)
    raw, _ = export_bytes(data)
    archive = zipfile.ZipFile(io.BytesIO(raw))
    entry = json.loads(archive.read("channels.json"))["platforms"]["feishu"]
    assert entry["credentials"] == {"app_id": APP_ID, "app_secret": PLACEHOLDER}  # App ID 不是密钥：留着，恢复以后只要重填 Secret
    assert APP_SECRET.encode() not in b"".join(archive.read(name) for name in archive.namelist())


# ---- 真的 SDK：飞书推来的 JSON → 事件 → 通道（装了 lark-oapi 才跑）------------------------------


def test_the_sdk_turns_feishu_json_into_events_the_channel_understands(bot):
    pytest.importorskip("lark_oapi")
    import lark_oapi as lark

    s = bot(run("toolu_1", "echo hi"), reply(text("做完了")))
    handler = LarkListener.dispatcher(s.channel()._on_event)  # 真的 SDK 的分发器，接在真的通道上
    start = len(s.fake.sent)
    assert handler._do_without_validation(json.dumps(s.fake.event("跑个命令")).encode()) is None
    ask = s.asked(start)
    response = handler._do_without_validation(json.dumps(s.fake.card_action(ask, answer_of(s.fake.buttons(ask)[0]))).encode())
    assert json.loads(lark.JSON.marshal(response)) == {"toast": {"type": "success", "content": "已允许"}}  # 包成了 SDK 要的回答
    s.wait_reply(start, "做完了")
    assert any(block["type"] == "text" and block["text"] == "跑个命令" for block in s.api.requests[0]["messages"][0]["content"])
    s.idle()


def test_stopping_the_sdk_listener_before_it_runs_is_harmless(fake):
    pytest.importorskip("lark_oapi")
    listener = LarkListener(APP_ID, APP_SECRET, fake.base_url)
    listener.stop()  # 通道刚开就被关掉：SDK 还没开始连
    states: list = []
    outcome: list = []
    # 在另一个线程里跑（和真的一样）：run 会给它所在的线程建一个事件循环，最后关掉，别留在测试的主线程上
    worker = threading.Thread(target=lambda: outcome.append(listener.run(lambda kind, data: None, lambda *state: states.append(state))))
    worker.start()
    worker.join(10)
    assert outcome == ["stopped"] and states == [] and fake.requests == []
