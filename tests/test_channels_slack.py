"""Slack 通道的测试：mrkdwn 的排版、检查两个 token、Web API 客户端、Socket Mode，以及一整条"Slack → 智狐 → Slack"的路。

前面几节不用整个程序：排版（markup.to_mrkdwn、slack.from_slack）是纯函数；check 和 SlackChannel 直接对着本地的假 Slack
（tests/fake_slack.py）。后面几节把整个程序组装起来（见 tests/channel_rig.py）：真的网页服务器、本地的假 Claude API、
本地的假 Slack，测试代码扮演 Slack 那头的你（s.say、s.fake.press、s.fake.command）和浏览器（s.browser）。
Web API 和 Socket Mode 都是真的 HTTP 和 WebSocket，走的是真实的代码路径。不连真正的 Slack，不花钱。

等待一律用 until（条件一成立就往下走）；重连的间隔、"多久没动静就 ping"这些时间在测试里调短（monkeypatch），不真的等。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import random
import re
import threading
import zipfile
from datetime import datetime

import pytest
from channel_rig import rig_fixture
from fake_api import reply, text, tool_use
from fake_slack import (
    APP_ID,
    APP_TOKEN,
    BOT_ID,
    BOT_TOKEN,
    BOT_USER,
    CHANNEL,
    OWNER,
    OWNER_CHAT,
    OWNER_NAME,
    STRANGER,
    TEXT_LIMIT,
    FakeSlack,
    unescape,
    unescaped,
)
from sample_files import JPEG_BYTES
from test_channels import result_text, run, stop_later, tool_results, until

from harness.channels import markup, slack
from harness.channels.base import ChannelError, Prompt
from harness.channels.store import ChannelStore
from harness.export import PLACEHOLDER, write_archive


def fast(monkeypatch) -> None:
    """重连不用等 1 秒起翻倍的间隔；token 不对时隔 0.3 秒再试（真的是一分钟）。"""
    monkeypatch.setattr(slack, "backoff", lambda delay, ceiling: (0.05, min(delay * 2, ceiling)))
    monkeypatch.setattr(slack, "CHANNEL_RETRY_SECONDS", 0.3)


@pytest.fixture
def fake():
    """只有假 Slack，没有整个程序（测 check、WebAPI、SlackChannel 自己）。"""
    server = FakeSlack().start()
    yield server
    stop_later(server.stop)
    assert server.problems == []


@pytest.fixture
def bot(make_config, monkeypatch):
    fast(monkeypatch)
    made: list[FakeSlack] = []

    def make() -> FakeSlack:
        made.append(FakeSlack().start())
        return made[-1]

    yield from rig_fixture(make_config, monkeypatch, make)
    for server in made:
        # 真的 Slack 不会报错、但做错了的事：没转义的 mrkdwn、看不懂的回执、拿用过的地址来连
        assert server.problems == []
        assert all(sock.unmasked == 0 for sock in server._sockets)  # 客户端发的每一帧都带着掩码


@pytest.fixture
def logs(caplog):
    """harness 的日志。log.setup 关掉过往上传（test_serve.py 会调用它）的话，caplog 照样收得到。"""
    caplog.set_level(logging.DEBUG, logger="harness")
    harness_logger = logging.getLogger("harness")
    harness_logger.addHandler(caplog.handler)
    yield caplog
    harness_logger.removeHandler(caplog.handler)


def last_text(edit: dict) -> str:
    """改过的确认请求最后那一块（写着结果：已允许、已拒绝……）。"""
    return edit["blocks"][-1]["text"]["text"]


def command(s, words: str = "", **options) -> int:
    """用一次 /zhifox。返回用之前机器人已经发了几条（wait_reply 从那以后找）。"""
    count = len(s.fake.sent)
    s.fake.command(words, **options)
    return count


def texts_sent_to_model(request: dict) -> list[str]:
    content = request["messages"][-1]["content"]
    return [block["text"] for block in content if isinstance(block, dict) and block.get("type") == "text"]


# ---- 排版：Markdown → mrkdwn，Slack 的文字 → 平常的文字 -------------------------------------------


def test_markdown_becomes_slack_mrkdwn():
    source = (
        "# 标题 & 说明\n"
        "**粗体** 和 *斜体*，`a<b>`\n"
        "- 一项 [文档](https://example.com/a?b=1&c=2)\n"
        "```python\n"
        "if a < b and c > d: print('&')\n"
        "```"
    )
    assert markup.to_mrkdwn(source) == (
        "*标题 &amp; 说明*\n"
        "*粗体* 和 _斜体_，`a&lt;b&gt;`\n"
        "• 一项 <https://example.com/a?b=1&amp;c=2|文档>\n"
        "```\n"  # Slack 不认 ```python：语言名会被当成代码的第一行
        "if a &lt; b and c &gt; d: print('&amp;')\n"
        "```"
    )


def test_mrkdwn_keeps_links_whole_and_plain_text_alone():
    # 网址和文字里的 | 会把 <网址|文字> 提前截断：网址里换成 %7C，文字里换成一个长得像的字符
    assert markup.to_mrkdwn("[a|b](https://x.com/p|q)") == "<https://x.com/p%7Cq|a¦b>"
    # 变量名里的 _、只有半个的记号、乘号：原样（只转义 & < >）
    assert markup.to_mrkdwn("my_var_name 和 **没闭合 2*3*4 & x") == "my_var_name 和 **没闭合 2*3*4 &amp; x"
    # 没有收尾的代码块照样关上，不让后面的话全变成代码
    assert markup.to_mrkdwn("看：\n```\n<b>") == "看：\n```\n&lt;b&gt;\n```"


def test_any_text_becomes_mrkdwn_with_nothing_left_unescaped():
    pieces = ["a", "<", ">", "&", "*", "**", "_", "`", "```", "[x]", "(https://e.com/?a=1&b=<2>)", "|", "#", " ", "\n", "- ", "中文"]
    generator = random.Random(20261121)
    for _ in range(400):
        source = "".join(generator.choice(pieces) for _ in range(generator.randint(1, 40)))
        assert unescaped(markup.to_mrkdwn(source)) == [], source


def test_text_from_slack_is_unescaped_for_the_model():
    assert slack.from_slack("5 &lt; 6 &amp;&amp; 7 &gt; 3") == "5 < 6 && 7 > 3"
    assert slack.from_slack("&amp;lt; 是 HTML 里小于号的写法") == "&lt; 是 HTML 里小于号的写法"  # &amp; 最后才还原
    assert slack.from_slack("<https://example.com/a?b=1&amp;c=2>") == "https://example.com/a?b=1&c=2"
    assert slack.from_slack("<http://example.com|example.com>") == "example.com"  # 你打的 example.com，Slack 自动加了链接
    assert slack.from_slack("<mailto:a@b.com|a@b.com>") == "a@b.com"
    # 你给一段字加了链接：模型要看得到链接指向哪里，写成 Markdown 的样子
    assert slack.from_slack("看<https://example.com/doc|这份 A &amp; B 文档>") == "看[这份 A & B 文档](https://example.com/doc)"
    # @某人、#频道 是 Slack 自己的写法，原样留着
    assert slack.from_slack("<@U0AKXAN01> 在 <#C0GENERAL1|general>") == "<@U0AKXAN01> 在 <#C0GENERAL1|general>"


# ---- 检查 token ----------------------------------------------------------------


def test_check_returns_the_bot_and_a_link_to_its_messages(fake, make_config):
    config = make_config(**fake.config())
    assert slack.check(config, {"bot_token": f" {BOT_TOKEN}\n", "app_token": APP_TOKEN}) == fake.bot
    # 机器人是谁（auth.test、bots.info）用 Bot token 问；App token 只用来试着开一次 Socket Mode 的连接
    assert [(call["method"], call["verb"], call["token"]) for call in fake.calls] == [
        ("auth.test", "POST", "bot"), ("bots.info", "GET", "bot"), ("apps.connections.open", "POST", "app")]
    assert fake.calls[1]["args"] == {"bot": BOT_ID}
    assert fake.bot["link"] == f"https://slack.com/app_redirect?app={APP_ID}&team=T0ZHIFOX1"


@pytest.mark.parametrize(
    ("credentials", "needle"),
    [
        ({"bot_token": "xoxp" + "-a-user-token-0000000000", "app_token": APP_TOKEN}, "xoxb-"),  # 用户的 token，不是机器人的
        ({"bot_token": APP_TOKEN, "app_token": BOT_TOKEN}, "xoxb-"),  # 两个填反了
        ({"bot_token": BOT_TOKEN, "app_token": BOT_TOKEN}, "xapp-"),
        ({"bot_token": BOT_TOKEN, "app_token": ""}, "xapp-"),
    ],
)
def test_check_rejects_tokens_of_the_wrong_kind_without_asking_slack(fake, make_config, credentials, needle):
    with pytest.raises(ChannelError, match=needle) as caught:
        slack.check(make_config(**fake.config()), credentials)
    assert BOT_TOKEN not in str(caught.value) and APP_TOKEN not in str(caught.value)
    assert fake.calls == []


def test_check_explains_which_token_slack_rejected(fake, make_config):
    config = make_config(**fake.config())
    credentials = {"bot_token": BOT_TOKEN, "app_token": APP_TOKEN}

    errors: list[str] = []

    def rejected() -> slack.SlackError:
        with pytest.raises(slack.SlackError) as caught:
            slack.check(config, credentials)
        errors.append(str(caught.value))
        return caught.value

    fake.bot_tokens.clear()  # 应用重新安装过，Bot token 换了
    error = rejected()
    assert "不认这个 Bot token" in str(error) and error.error == "invalid_auth"
    assert [call["method"] for call in fake.calls] == ["auth.test"]  # 第一步就停下

    fake.bot_tokens.add(BOT_TOKEN)
    fake.app_tokens.clear()  # App-level token 删掉了（或者没加 connections:write）
    error = rejected()
    assert "不认这个 App-level token" in str(error) and "connections:write" in str(error)

    fake.app_tokens.add(APP_TOKEN)
    fake.fail_next["bots.info"] = (200, {"ok": False, "error": "missing_scope", "needed": "users:read", "provided": "chat:write"}, {})
    assert "users:read" in str(rejected())  # 说出缺的是哪一个权限
    assert not any(BOT_TOKEN in item or APP_TOKEN in item for item in errors)


def test_web_api_trouble_is_explained_without_the_tokens(fake):
    api = slack.WebAPI(BOT_TOKEN, APP_TOKEN, fake.base_url + "/api")
    fake.fail_next["chat.postMessage"] = (502, None, {})  # 一个不是 JSON 的回答（Slack 前面的代理出了问题）
    with pytest.raises(slack.SlackError, match="HTTP 502") as caught:
        api.call("chat.postMessage", {"channel": OWNER_CHAT, "text": "hi"})
    assert caught.value.status == 502
    fake.fail_next["chat.postMessage"] = (200, {"ok": False, "error": "channel_not_found"}, {})
    with pytest.raises(slack.SlackError, match="找不到这个聊天"):
        api.call("chat.postMessage", {"channel": OWNER_CHAT, "text": "hi"})
    # 出了毛病的代理把请求头弹了回来：错误名里带着 token，说明里换成 <hidden>
    fake.fail_next["chat.postMessage"] = (200, {"ok": False, "error": f"bad_header Bearer {BOT_TOKEN} {APP_TOKEN}"}, {})
    with pytest.raises(slack.SlackError, match="<hidden>") as caught:
        api.call("chat.postMessage", {"channel": OWNER_CHAT, "text": "hi"})
    assert BOT_TOKEN not in str(caught.value) and APP_TOKEN not in caught.value.error
    nowhere = slack.WebAPI(BOT_TOKEN, APP_TOKEN, "http://127.0.0.1:9/api")  # 没有人在听的端口
    with pytest.raises(slack.SlackError, match="连不上 Slack") as caught:
        nowhere.call("auth.test", {})
    assert BOT_TOKEN not in str(caught.value) and APP_TOKEN not in str(caught.value)


# ---- SlackChannel 自己：发消息、确认请求、下载 -------------------------------------------------


def test_every_piece_of_a_long_reply_fits_after_escaping(fake):
    channel = slack.SlackChannel(BOT_TOKEN, APP_TOKEN, fake.base_url + "/api")
    # & < > 排成 mrkdwn 以后变长好几倍：拆的时候按排好以后的长度算
    source = "x <&> " * 3000
    channel.send(OWNER_CHAT, source)
    plain = fake.sent[:]
    assert len(plain) > 1 and all(len(message["text"]) <= TEXT_LIMIT for message in plain)
    assert "".join(unescape(message["text"]) for message in plain).split() == source.split()

    # 一大段代码：每一段都是完整的代码块（拆开的地方关上、下一段重新打开）
    code = "```html\n" + "<p>&nbsp;</p>\n" * 1200 + "```"
    channel.send(OWNER_CHAT, code, markdown=True)
    pieces = fake.sent[len(plain):]
    assert len(pieces) > 1
    for message in pieces:
        assert len(message["text"]) <= TEXT_LIMIT
        assert message["text"].startswith("```\n") and message["text"].endswith("\n```")
    lines = [line for message in pieces for line in unescape(message["text"]).split("\n") if line != "```"]
    assert lines == ["<p>&nbsp;</p>"] * 1200


def test_a_prompt_with_special_characters_and_a_failed_update(fake):
    channel = slack.SlackChannel(BOT_TOKEN, APP_TOKEN, fake.base_url + "/api")
    prompt = Prompt(id=7, title="助手想要创建文件：a&b<c>.txt", detail="第一行 <b>\n```\n里面也有 ```", where="工作目录：/tmp/x&y",
                    allow="允许", deny="拒绝", hint="回复“允许”或“拒绝”。")
    handle = channel.ask(OWNER_CHAT, prompt)
    (message,) = fake.sent
    assert handle == message["ts"] and fake.is_prompt(message)
    assert message["text"] == "助手想要创建文件：a&amp;b&lt;c&gt;.txt"  # 通知里显示的那一句也要转义
    head, code, where, actions = message["blocks"]
    assert head["text"]["text"] == "*助手想要创建文件：a&amp;b&lt;c&gt;.txt*"
    # 代码里的 ``` 插了一个看不见的字符，代码块不会被它提前关上
    assert code["text"]["text"] == "```第一行 &lt;b&gt;\n`\u200b``\n里面也有 `\u200b`````"
    assert where["elements"][0]["text"] == "工作目录：/tmp/x&amp;y"
    assert [(button["action_id"], button["value"]) for button in actions["elements"]] == [("allow", "ok:7"), ("deny", "no:7")]

    # 改不了那条消息（比如被删掉了）：另发一条写明结果
    fake.fail_next["chat.update"] = (200, {"ok": False, "error": "message_not_found"}, {})
    channel.settle(OWNER_CHAT, handle, "已允许")
    assert fake.sent[-1]["text"] == "已允许" and fake.edits == []
    channel.settle(OWNER_CHAT, handle, "已允许")  # 已经处理过了：什么都不做
    assert len(fake.sent) == 2


def test_files_are_only_downloaded_from_slack():
    channel = slack.SlackChannel(BOT_TOKEN, APP_TOKEN, "http://127.0.0.1:9/api")
    # token 只带去 Slack 自己的域名，而且要加密；这几个连请求都不发
    for url in ("https://files.example.com/x.png", "https://files.slack.com.evil.example/x.png", "http://files.slack.com/x.png", "ftp://files.slack.com/x"):
        with pytest.raises(ChannelError):
            channel._download(url)


# ---- 整个程序：Slack → 智狐 → Slack ----------------------------------------------------------


def test_connecting_from_the_page_checks_both_tokens(bot):
    s = bot(connected=False)
    fake = s.fake

    def connect(**credentials) -> tuple[int, dict]:
        return s.browser.post("/api/channel/connect", {"platform": "slack", "credentials": credentials})

    # 样子不对：不去问 Slack 就拒绝
    status, body = connect(bot_token="xoxp" + "-a-user-token-0000000000", app_token=APP_TOKEN)
    assert status == 409 and "xoxb-" in body["error"]
    status, body = connect(bot_token=BOT_TOKEN, app_token=BOT_TOKEN)
    assert status == 409 and "xapp-" in body["error"] and BOT_TOKEN not in body["error"]
    assert fake.calls == []
    # Slack 不认的：说清楚是哪一个，不保存
    wrong_bot = "xoxb" + "-revoked-bot-token-0000"
    status, body = connect(bot_token=wrong_bot, app_token=APP_TOKEN)
    assert status == 409 and "Bot token" in body["error"] and wrong_bot not in body["error"] and APP_TOKEN not in body["error"]
    wrong_app = "xapp" + "-1-revoked-app-token-0000"
    status, body = connect(bot_token=BOT_TOKEN, app_token=wrong_app)
    assert status == 409 and "App-level token" in body["error"] and wrong_app not in body["error"] and BOT_TOKEN not in body["error"]
    assert not s.config.channels_file.exists()

    assert connect(bot_token=f"  {BOT_TOKEN}\n", app_token=APP_TOKEN) == (200, {"ok": True, "bot": {**fake.bot, "handle": "@zhifox"}})
    until(lambda: s.platform()["status"]["state"] == "online", what="连上")
    platform = s.platform()
    assert platform["hints"] == {"bot_token": "…6789", "app_token": "…GHIJ"} and platform["token_hint"] == "…6789"  # 只露出最后 4 位
    assert platform["bot"]["link"] == fake.bot["link"] and platform["commands"] == "/zhifox {name}" and platform["buttons"] is True
    assert ChannelStore(s.config.channels_file).get("slack")["credentials"] == {"bot_token": BOT_TOKEN, "app_token": APP_TOKEN}

    # 只换了 App token（Bot token 那一栏空着）：接着用存着的 Bot token，通道用新的 App token 重新连上
    rotated = "xapp" + "-1-rotated-app-token-" + "for-tests-KLMN"
    fake.app_tokens = {rotated}
    connections = fake.connections
    assert connect(bot_token="", app_token=rotated)[0] == 200
    assert ChannelStore(s.config.channels_file).get("slack")["credentials"] == {"bot_token": BOT_TOKEN, "app_token": rotated}
    until(lambda: fake.connections > connections and s.channel().status.state == "online", what="用新的 App token 重新连上")
    assert fake.methods("apps.connections.open")[-1]["last4"] == "KLMN"
    until(lambda: s.platform()["hints"]["app_token"] == "…KLMN", what="页面上的新提示")
    everything = json.dumps(s.browser.events, ensure_ascii=False)
    assert rotated not in everything and BOT_TOKEN not in everything


def test_a_message_gets_an_answer_in_mrkdwn(bot):
    s = bot(reply(text("**你好**，我是智狐。看 [文档](https://example.com/a?b=1&c=2)：5 < 6 & 7 > 3\n```python\nprint('<hi>')\n```")))
    answer = s.wait_reply(s.say("你好 & <再见>"), "我是智狐")
    assert answer["chat"] == OWNER_CHAT and answer["blocks"] is None
    assert answer["text"] == "*你好*，我是智狐。看 <https://example.com/a?b=1&amp;c=2|文档>：5 &lt; 6 &amp; 7 &gt; 3\n```\nprint('&lt;hi&gt;')\n```"
    assert answer["body"]["unfurl_links"] is False and answer["body"]["unfurl_media"] is False  # 不在聊天里展开链接的预览
    # 模型看到的是还原过的文字
    assert "你好 & <再见>" in texts_sent_to_model(s.api.requests[0])
    # Web API 一律用 Bot token；App token 只用来开 Socket Mode 的连接
    assert {call["token"] for call in s.fake.calls if call["method"] != "apps.connections.open"} == {"bot"}
    assert {call["token"] for call in s.fake.methods("apps.connections.open")} == {"app"}
    s.idle()


def test_pairing_from_the_dm(bot):
    s = bot(paired=False)
    s.fake.user_says("你好")  # 还没配对，也没在配对：不理
    until(lambda: s.hub.store.get("slack")["ignored"] == 1, what="忽略了这一条")
    code = s.hub.pair("slack")["code"]
    until(lambda: (s.platform()["pairing"] or {}).get("code") == code, what="页面上的配对码")
    assert s.platform()["pairing"]["link"] == s.fake.bot["link"]  # 页面上"打开 Slack"的按钮：打开和机器人的私聊

    hint = s.wait_reply(s.say("在吗"), "6 位配对码")
    assert hint["chat"] == OWNER_CHAT
    paired = s.wait_reply(s.say(code), "配对成功")
    assert OWNER_NAME in paired["plain"] and "/zhifox new" in paired["plain"]
    until(lambda: (s.platform()["owner"] or {}).get("name") == OWNER_NAME and s.platform()["pairing"] is None, what="页面上的主人")
    assert "现在：空闲" in s.wait_reply(command(s, "status"), "现在：")["plain"]


def test_strangers_channels_bots_and_edits_get_no_reply(bot):
    s = bot()
    before = len(s.fake.sent)
    s.fake.user_says("你好", user=STRANGER)
    s.fake.user_says("/help", user=STRANGER)
    s.fake.command("help", user=STRANGER)  # 陌生人在他和机器人的私聊里用命令：也不理
    s.fake.user_says("/help", private=False)  # 主人在频道里说话：频道里的人都看得到，一律不理
    s.fake.user_says("我自己发的", user=BOT_USER, bot_id=BOT_ID, chat=OWNER_CHAT)  # 机器人自己发的消息，Slack 也会推回来
    s.fake.user_says("别的应用发的", user="U0OTHERAPP", bot_id="B0OTHERAPP", chat=OWNER_CHAT)
    s.fake.user_says("改过的话", subtype="message_changed")
    s.fake.user_says("", subtype="message_deleted")
    s.fake.user_says("加入了频道", subtype="channel_join")
    s.sentinel()

    # 一个字都没回：只有主人私聊里的那一条 /help 有回答。陌生人在私聊里的三条记在页面上，别的在通道里就丢掉了
    assert [message["chat"] for message in s.fake.sent[before:]] == [OWNER_CHAT]
    assert s.api.requests == [] and s.fake.ephemeral == []
    assert [item.chat for item in s.hub.router.sessions("slack")] == [OWNER_CHAT]
    until(lambda: s.platform()["ignored"] == 3, what="页面上的计数")
    # 不理的也都回执了，Slack 不会再推一遍。回执走 WebSocket，回答走 Web API，两条路谁先到不一定：等回执都到齐
    until(lambda: not s.fake.unacked(), what="全部回执")


def test_a_retried_event_is_handled_once(bot):
    s = bot(reply(text("只回答一次")), reply(text("不该有第二次")))
    s.fake.resend_after = 0.2
    s.fake.lose_acks = 1  # 回执在路上丢了：Slack 过一会儿把同一个信封再推一遍
    start = len(s.fake.sent)
    envelope = s.fake.user_says("你好")
    s.fake.acked(envelope)
    assert s.fake.envelopes[envelope["envelope_id"]]["sends"] == 2
    assert s.fake.envelopes[envelope["envelope_id"]]["envelope"]["retry_attempt"] == 1
    s.wait_reply(start, "只回答一次")
    s.idle()
    # 断线时 Slack 换了一个信封、带着同一个事件再推一遍
    again = s.fake.user_says("你好", event_id=envelope["payload"]["event_id"])
    s.fake.acked(again)
    s.sentinel()
    assert len(s.api.requests) == 1
    assert not any("不该有第二次" in message["plain"] for message in s.fake.sent)


def test_every_envelope_is_acked_at_once_even_when_handling_it_is_slow(bot):
    s = bot(reply(text("慢慢来的回答")), reply(text("第二条的回答")))
    s.fake.hold("users.info")  # Slack 回答"这个人叫什么"很慢（比如正被限流）
    first = s.fake.user_says("你好")
    s.fake.acked(first, timeout=2)
    s.fake.wait_until(lambda: s.fake.methods("users.info"), what="问名字")  # 正在处理第一条，卡在问名字上
    # 这时来的信封也马上回执（Slack 只等 3 秒），不等前面的处理完
    second = s.fake.command("status")
    third = s.fake.user_says("还在吗")
    s.fake.acked(second, timeout=2)
    s.fake.acked(third, timeout=2)
    assert s.fake.sent == []
    s.fake.release("users.info")
    s.wait_reply(0, "慢慢来的回答")
    s.wait_reply(0, "现在：")  # 按收到的顺序处理
    s.wait_reply(0, "第二条的回答")
    s.idle()
    assert texts_sent_to_model(s.api.requests[1])[-1] == "还在吗"


# ---- 确认 ------------------------------------------------------------------


def test_allowing_with_the_button_runs_the_command(bot):
    s = bot(
        reply(text("我来看看。"), tool_use("toolu_1", "run_command", {"command": "echo '<b>&</b>' && pwd"}), stop_reason="tool_use"),
        reply(text("**一切正常**。")),
    )
    start = s.say("看看")
    first = s.wait_reply(start, "我来看看")
    ask = s.asked(start)
    assert s.fake.sent.index(first) < s.fake.sent.index(ask)  # 顺序和助手说的一样

    # 确认请求：粗体的标题、代码块里的完整命令、工作目录，下面两个按钮
    head, code, where, _ = ask["blocks"]
    assert head["text"] == {"type": "mrkdwn", "text": "*助手想要执行命令：*"}
    assert code["text"]["text"] == "```$ echo '&lt;b&gt;&amp;&lt;/b&gt;' &amp;&amp; pwd```"
    assert where == {"type": "context", "elements": [{"type": "mrkdwn", "text": f"工作目录：{s.config.workspace}"}]}
    allow, deny = s.fake.buttons(ask)
    confirm_id = int(allow["value"].removeprefix("ok:"))
    assert (allow["text"]["text"], allow["style"], deny["text"]["text"], deny["style"], deny["value"]) == ("允许", "primary", "拒绝", "danger", f"no:{confirm_id}")
    (pending,) = until(lambda: s.state()["channels"]["approvals"], what="页面上的确认请求")
    assert (pending["platform"], pending["chat"], pending["id"], pending["name"]) == ("slack", OWNER_CHAT, confirm_id, OWNER_NAME)

    s.fake.press(ask)
    final = s.wait_reply(start, "一切正常")
    assert final["text"] == "*一切正常*。"
    # 按钮去掉了，原来的内容留着，最后写着结果；答复成功时不另外说话
    edit = s.fake.edit_of(ask)
    assert edit["blocks"][:3] == ask["blocks"][:3] and len(edit["blocks"]) == 4 and last_text(edit) == "*已允许*"
    assert s.fake.ephemeral == []
    (result,) = tool_results(s.api.requests[1])
    assert "<b>&</b>" in result_text(result) and not result.get("is_error")
    s.idle()
    until(lambda: s.state()["channels"]["approvals"] == [], what="页面上的确认请求消失")


def test_denying_with_the_button_and_answering_with_words(bot):
    s = bot(run("toolu_a", "echo a"), run("toolu_b", "echo b"), run("toolu_c", "echo c"), reply(text("三件事都处理完了")))
    start = s.say("做三件事")
    first = s.asked(start)
    s.say("允许")
    second = s.asked(s.fake.sent.index(first) + 1)

    # 别人点按钮、在频道里点：都不算数
    s.fake.press(second, allow=True, user=STRANGER)
    s.fake.press(second, allow=True, chat=CHANNEL)
    s.sentinel()
    assert s.session().waiting is not None and not any(edit["ts"] == second["ts"] for edit in s.fake.edits)
    assert s.fake.ephemeral == []

    s.fake.press(second, allow=False)
    third = s.asked(s.fake.sent.index(second) + 1)
    s.say("Yes!")
    s.wait_reply(start, "三件事都处理完了")
    assert [last_text(s.fake.edit_of(ask)) for ask in (first, second, third)] == ["*已允许*", "*已拒绝*", "*已允许*"]
    assert result_text(tool_results(s.api.requests[1])[0]).strip() == "a"
    assert result_text(tool_results(s.api.requests[2])[0]).startswith("用户拒绝了这个操作")
    assert result_text(tool_results(s.api.requests[3])[0]).strip() == "c"
    s.idle()


def test_the_web_and_the_chat_race_for_the_same_confirmation(bot):
    s = bot(run("toolu_a", "echo a"), run("toolu_b", "echo b"), reply(text("完")))
    start = s.say("做两件事")
    first = s.asked(start)
    (pending,) = until(lambda: s.state()["channels"]["approvals"], what="页面上的确认请求")
    answer = {"platform": "slack", "chat": OWNER_CHAT, "confirm_id": pending["id"], "allow": True}
    assert s.browser.post("/api/channel/confirm", answer) == (200, {"ok": True})
    assert last_text(s.fake.edit_of(first)) == "*已在网页上允许*"

    # 再点 Slack 里的按钮：只有你看得到的一句"已经处理过了"
    s.fake.press(first)
    whisper = s.fake.wait_until(lambda: s.fake.ephemeral and s.fake.ephemeral[-1], what="只有你看得到的提示")
    assert whisper == {"chat": OWNER_CHAT, "user": OWNER, "text": "这个确认请求已经处理过了"}
    status, body = s.browser.post("/api/channel/confirm", answer)
    assert status == 409 and "已经处理过了" in body["error"]
    s.fake.press(first, user=STRANGER)  # 陌生人点一个处理过的按钮：连提示都没有

    # 两边同时点：只有一边算数，输的那一边被告知"已经处理过了"（网页上是 409，Slack 里是只有你看得到的一句）
    second = s.asked(s.fake.sent.index(first) + 1)
    confirm_id = int(s.fake.buttons(second)[0]["value"].removeprefix("ok:"))
    web: list[tuple[int, dict]] = []
    racer = threading.Thread(target=lambda: web.append(s.browser.post("/api/channel/confirm", {**answer, "confirm_id": confirm_id})))
    racer.start()
    s.fake.press(second)
    racer.join(10)
    web_won = web[0][0] == 200
    assert last_text(s.fake.edit_of(second)) == ("*已在网页上允许*" if web_won else "*已允许*")
    if web_won:
        s.fake.wait_until(lambda: len(s.fake.ephemeral) == 2, what="只有你看得到的提示")
    else:
        assert web[0][0] == 409
    s.wait_reply(start, "完")
    s.sentinel()
    assert len(s.fake.ephemeral) == (2 if web_won else 1) and len(s.fake.edits) == 2
    assert len(s.api.requests) == 3  # 每条命令只执行了一次
    s.idle()


# ---- /zhifox 命令 ------------------------------------------------------------


def test_slash_commands_in_the_dm(bot):
    s = bot(reply(text("第一段的回答")), reply(text("随便问问的回答")))
    for words in ("", "help", "HELP"):  # 光是 /zhifox 也是帮助
        message = s.wait_reply(command(s, words), "直接发消息就行")
        assert "/zhifox new  " in message["plain"] and "/zhifox stop  " in message["plain"] and "/zhifox status  " in message["plain"]
        assert "\n/new" not in message["plain"] and "允许 / 拒绝" in message["plain"]
    assert "现在：空闲" in s.wait_reply(command(s, "status"), "现在：")["plain"]

    s.wait_reply(s.say("第一段"), "第一段的回答")
    s.idle()
    first = s.session().conversation.id
    s.wait_reply(command(s, "new"), "开始一段新对话")
    s.wait_reply(command(s, "随便问问"), "随便问问的回答")  # 不是命令：当作一句普通的话交给模型
    s.idle()
    assert s.session().conversation.id != first
    assert len(s.api.requests[1]["messages"]) == 1 and "随便问问" in texts_sent_to_model(s.api.requests[1])
    assert s.fake.methods("conversations.open")[-1]["args"] == {"users": OWNER}  # 问 Slack：主人和机器人的私聊是哪一个

    # 在频道里、在别人的私聊里用：回答会被别人看到，不理
    before = len(s.fake.sent)
    s.fake.command("status", chat=CHANNEL)
    s.fake.command("status", chat="D0SOMEONE9")
    s.sentinel()
    assert len(s.fake.sent) == before + 1
    assert len(s.api.requests) == 2


def test_stop_from_slack_stops_a_running_command(bot):
    s = bot(run("toolu_1", "touch started && sleep 30"), approval="all")
    start = s.say("跑一个很久的命令")
    s.wait_reply(start, "已自动允许")
    until(lambda: (s.config.workspace / "started").exists(), what="命令开始运行")
    s.wait_reply(command(s, "new"), "这一轮还没结束")
    stopping = command(s, "stop")
    s.wait_reply(stopping, "好，正在停下")
    s.wait_reply(stopping, "已中断。")
    s.idle()
    assert len(s.api.requests) == 1
    s.wait_reply(command(s, "stop"), "现在没有在进行的事")


# ---- 排版、拆开、发来的文字 -----------------------------------------------------------


def test_a_long_reply_is_split_into_messages_slack_accepts(bot):
    long = "\n\n".join(f"第 {n} 段：if a < b && c > d {{ x &= 1 }} " * 12 for n in range(40))
    s = bot(reply(text(long)))
    start = s.say("写长一点")
    s.wait_reply(start, "第 39 段")
    pieces = s.fake.sent[start:]
    assert len(pieces) > 1 and all(len(message["text"]) <= TEXT_LIMIT for message in pieces)
    assert re.sub(r"\s+", "", "".join(message["plain"] for message in pieces)) == re.sub(r"\s+", "", long)
    s.idle()


def test_a_very_long_command_is_shown_in_full_over_several_sections(bot):
    command_line = "echo " + " ".join(f"part{n}<&>" for n in range(6000))  # 排成 mrkdwn 以后要好几十段
    s = bot(run("toolu_1", command_line), reply(text("不跑了")))
    start = s.say("跑一个很长的命令")
    ask = s.asked(start)
    shown = [message for message in s.fake.sent[start:] if message["blocks"]]
    assert len(shown) > 1 and shown[-1] is ask  # 一条消息放不下：前面几条只有代码，按钮在最后一条
    assert all(len(message["blocks"]) <= 50 for message in shown)
    sections = [block for message in shown for block in message["blocks"] if block["type"] == "section"][1:]  # 去掉标题
    assert all(len(block["text"]["text"]) <= 3000 for block in sections)
    code = "".join(unescape(block["text"]["text"].removeprefix("```").removesuffix("```")) for block in sections)
    assert code == "$ " + command_line
    s.fake.press(ask, allow=False)
    s.wait_reply(start, "不跑了")
    s.idle()


def test_incoming_text_and_links_are_unescaped_for_the_model(bot):
    s = bot(reply(text("好")))
    wire = "看 <https://example.com/a?b=1&amp;c=2|这个页面> 和 <https://example.com>：5 &lt; 6 &amp;&amp; 7 &gt; 3"
    s.wait_reply(s.say(wire, wire=True), "好")
    assert texts_sent_to_model(s.api.requests[0])[-1] == "看 [这个页面](https://example.com/a?b=1&c=2) 和 https://example.com：5 < 6 && 7 > 3"
    s.idle()


def test_a_title_with_special_characters_is_escaped_everywhere(bot):
    create = tool_use("toolu_f", "str_replace_based_edit_tool", {"command": "create", "path": "a&b<c>.txt", "file_text": "x < y"})
    s = bot(reply(create, stop_reason="tool_use"), reply(text("建好了")))
    start = s.say("建个文件")
    ask = s.asked(start)
    assert "a&amp;b&lt;c&gt;.txt" in ask["text"] and "a&b<c>.txt" in ask["plain"]
    s.fake.press(ask)
    s.wait_reply(start, "建好了")
    s.idle()
    assert (s.config.workspace / "a&b<c>.txt").read_text(encoding="utf-8") == "x < y"


# ---- 图片和文件 --------------------------------------------------------------


def test_an_image_reaches_the_model_and_is_downloaded_with_the_bot_token(bot):
    s = bot(reply(text("是一张很小的图")), reply(text("读完了")))
    start = s.say("这是什么", files=[("cat.jpg", "image/jpeg", JPEG_BYTES)])
    s.wait_reply(start, "是一张很小的图")
    content = s.api.requests[0]["messages"][-1]["content"]
    (image,) = [block for block in content if block["type"] == "image"]
    assert image["source"] == {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(JPEG_BYTES).decode()}
    assert "这是什么" in texts_sent_to_model(s.api.requests[0])
    assert [item["token"] for item in s.fake.downloads] == ["bot"]  # 下载 Slack 的私有文件要带 Bot token

    start = s.say("", files=[("notes.txt", "text/plain", "第一行\n第二行 ✓".encode())])  # 只有文件，没有字
    s.wait_reply(start, "读完了")
    assert "第二行 ✓" in json.dumps(s.api.requests[1]["messages"][-1], ensure_ascii=False)
    s.idle()


def test_an_external_file_is_never_downloaded(bot):
    s = bot(reply(text("只看到了文字")))
    start = s.say("看看这个", files=[("plan.pdf", "application/pdf", b"%PDF-1.4 not ours")], external=True)  # Google Drive 这类
    s.wait_reply(start, "只看到了文字")
    assert s.fake.downloads == []  # 不把 Bot token 带到别人的服务器上
    assert "plan.pdf" not in json.dumps(s.api.requests[0], ensure_ascii=False)
    s.idle()


def test_a_login_page_instead_of_the_file_is_explained(bot):
    s = bot()
    s.fake.files_read = False  # 建应用时漏了 files:read：Slack 回一个登录页面，而不是文件
    start = s.say("这是什么", files=[("notes.txt", "text/plain", b"hello")])
    message = s.wait_reply(start, "这个文件用不了")
    assert "files:read" in message["plain"]
    assert [item["token"] for item in s.fake.downloads] == ["bot"]
    s.idle()
    assert s.api.requests == []  # 登录页面没有被当成文件交给模型


# ---- 连接：换连接、断网、僵死、token 被收回 ------------------------------------------------------


@pytest.mark.parametrize("reason", ["refresh_requested", "warning"])
def test_slack_asking_for_a_new_connection_reconnects_at_once(bot, reason):
    s = bot(reply(text("换了连接以后的回答")))
    opened = len(s.fake.methods("apps.connections.open"))
    old = s.fake.socket()
    s.fake.disconnect(reason)
    until(lambda: s.fake.connections == 2, what="新的连接")
    assert len(s.fake.methods("apps.connections.open")) == opened + 1  # 每次都要一个新地址（旧的用过就作废了）
    until(lambda: old.close_code == 1000, what="好好地关掉旧的连接")
    s.wait_reply(s.say("还在吗"), "换了连接以后的回答")
    assert s.channel().status.state == "online"
    s.idle()


def test_socket_mode_turned_off_is_an_error_that_says_so(bot, monkeypatch):
    monkeypatch.setattr(slack, "CHANNEL_RETRY_SECONDS", 30)  # 停在"出错了"，等你去应用后台打开
    s = bot()
    s.fake.disconnect("link_disabled")
    until(lambda: s.platform()["status"]["state"] == "error", what="页面上的出错")
    assert "Socket Mode" in s.platform()["status"]["message"]
    assert s.fake.connections == 1


def test_a_dropped_connection_reconnects_and_nothing_is_lost(bot):
    s = bot(reply(text("断线以后的回答")))
    s.fake.drop()
    start = s.say("还在吗")  # 断着的时候发的：Slack 等新的连接连上再推
    until(lambda: s.fake.connections == 2, what="重连")
    s.wait_reply(start, "断线以后的回答")
    until(lambda: s.channel().status.state == "online", what="重新连上")
    s.idle()


def test_a_quiet_connection_is_pinged_and_kept(bot, monkeypatch):
    monkeypatch.setattr(slack, "CHANNEL_IDLE_SECONDS", 0.3)
    s = bot()
    sock = s.fake.socket()
    s.fake.wait_until(lambda: len(sock.pings) >= 3, what="ping 了好几次")  # 每次都回了 pong：连接是好的
    assert s.fake.connections == 1 and s.channel().status.state == "online"


def test_a_connection_that_stops_answering_is_replaced(bot, monkeypatch, logs):
    monkeypatch.setattr(slack, "CHANNEL_IDLE_SECONDS", 0.2)
    s = bot(reply(text("换了连接以后的回答")))
    s.fake.freeze()  # TCP 还连着，但对面什么都不回（连 pong 也没有）
    until(lambda: s.fake.connections == 2, what="换一条连接")
    s.wait_reply(s.say("还在吗"), "换了连接以后的回答")
    assert any("连接没有回应" in record.getMessage() for record in logs.records)
    s.idle()


def test_a_revoked_app_token_shows_an_error_until_it_is_back(bot):
    s = bot()
    s.fake.app_tokens.clear()  # 在应用后台把 App-level token 删掉了
    s.fake.disconnect()
    until(lambda: s.platform()["status"]["state"] == "error", what="页面上的出错")
    message = s.platform()["status"]["message"]
    assert "App-level token" in message and APP_TOKEN not in message
    s.fake.app_tokens.add(APP_TOKEN)
    until(lambda: s.platform()["status"]["state"] == "online", what="又连上了")


def test_rate_limited_messages_are_retried(bot):
    s = bot(reply(text("限流以后照样送到")))
    s.fake.fail_next["chat.postMessage"] = (429, {"ok": False, "error": "ratelimited"}, {"Retry-After": "0"})
    start = s.say("你好")
    s.wait_reply(start, "限流以后照样送到")
    posts = s.fake.methods("chat.postMessage")
    assert len(posts) == 2 and posts[0]["args"] == posts[1]["args"]
    s.idle()


# ---- 密钥 ------------------------------------------------------------------


def test_the_tokens_never_reach_the_page_the_logs_or_the_messages(bot, logs):
    s = bot(reply(text("好")))
    s.wait_reply(s.say("你好"), "好")
    s.idle()
    s.fake.fail_next["chat.postMessage"] = (200, {"ok": False, "error": "invalid_auth"}, {})  # 发不出去：日志里写一笔
    command(s, "status")
    until(lambda: any("不认这个 Bot token" in record.getMessage() for record in logs.records), what="日志里的发送失败")
    s.fake.fail_next["chat.postMessage"] = (200, {"ok": False, "error": f"echo {BOT_TOKEN}"}, {})  # 错误里带着 token 的
    command(s, "status")
    until(lambda: any("<hidden>" in record.getMessage() for record in logs.records), what="日志里遮住的 token")
    s.wait_reply(command(s, "status"), "现在：")
    s.fake.app_tokens.clear()  # 运行中 App token 被收回：页面上、日志里都说明
    s.fake.disconnect()
    until(lambda: s.platform()["status"]["state"] == "error", what="页面上的出错")
    assert any("App-level token" in record.getMessage() for record in logs.records)
    everything = "\n".join([
        logs.text,
        json.dumps(s.browser.events, ensure_ascii=False),
        json.dumps(s.hub.snapshot(), ensure_ascii=False),
        json.dumps([message["body"] for message in s.fake.sent], ensure_ascii=False),
    ])
    for secret in s.fake.secrets:
        assert secret not in everything


def test_an_export_leaves_both_tokens_out(tmp_path):
    data = tmp_path / "data"
    store = ChannelStore(data / "channels.json")
    store.connect("slack", {"bot_token": BOT_TOKEN, "app_token": APP_TOKEN}, FakeSlack().bot)
    out = io.BytesIO()
    write_archive(data, out, datetime(2026, 11, 21, 9, 0))
    archive = zipfile.ZipFile(io.BytesIO(out.getvalue()))
    entry = json.loads(archive.read("channels.json"))["platforms"]["slack"]
    assert entry["credentials"] == {"bot_token": PLACEHOLDER, "app_token": PLACEHOLDER}
    assert entry["bot"]["app"] == APP_ID and entry["bot"]["username"] == "zhifox"  # 别的照样留着，恢复以后只要重新填两个 token
    everything = b"".join(archive.read(name) for name in archive.namelist())
    assert BOT_TOKEN.encode() not in everything and APP_TOKEN.encode() not in everything
