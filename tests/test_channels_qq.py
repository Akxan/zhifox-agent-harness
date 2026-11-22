"""QQ 通道的测试：access_token、REST 客户端、网关（和 Discord 共用的 gateway.py），以及一整条"手机 → 智狐 → 手机"的路。

对着本地的假 QQ（tests/fake_qq.py）：换 token、REST、网关都是真的 HTTP 和 WebSocket，走的是真实的代码路径。
组装见 tests/channel_rig.py。不连真正的 QQ，不花钱，AppID 和 AppSecret 都是编造的。

QQ 和别的平台不一样的几处，这里测得最细：
  - 没有按钮：确认请求是一条写着"回复“允许”或“拒绝”"的消息，答复靠回文字；结果另发一条
  - 被动回复：带着用户那条消息的编号，每条最多回 4 次，之后改成主动消息，一句都不能丢
  - access_token 两个小时过期，过期了（401、网关的 4004）要自己换一个新的，你不会察觉
"""

from __future__ import annotations

import base64
import io
import json
import logging
import random
import threading
import time
import zipfile
from datetime import datetime
from types import SimpleNamespace

import pytest
from channel_rig import rig_fixture
from fake_api import reply, text, tool_use
from fake_qq import (
    APP_ID,
    APP_SECRET,
    BOT_ID,
    BOT_NAME,
    GROUP,
    HINT,
    INTENTS,
    OWNER,
    OWNER_NAME,
    SHARE_URL,
    STRANGER,
    FakeQQ,
)
from sample_files import PNG_BYTES
from test_channels import result_text, run, stop_later, tool_results, until

from harness.channels import gateway, qq
from harness.channels.base import ChannelError
from harness.channels.gateway import FATAL, IDENTIFY_NEXT, RESUME_NEXT, SLOW_DOWN
from harness.channels.store import ChannelStore
from harness.config import PAIRING_ATTEMPTS
from harness.export import PLACEHOLDER, write_archive

MESSAGES = f"POST /v2/users/{OWNER}/messages"


@pytest.fixture
def fast(monkeypatch):
    """重连不用等：退避的时间、"会话失效以后先等 1 到 5 秒"都换成 0.01 秒（第一次心跳前的随机等待照旧）。"""
    monkeypatch.setattr(gateway, "backoff", lambda delay, ceiling: (0.01, delay))
    monkeypatch.setattr(gateway, "random", SimpleNamespace(random=random.random, uniform=lambda low, high: 0.01))


@pytest.fixture
def fake(fast):
    server = FakeQQ().start()
    yield server
    stop_later(server.stop)
    assert server.problems == []


@pytest.fixture
def bot(make_config, monkeypatch, fast):
    made: list[FakeQQ] = []

    def make() -> FakeQQ:
        made.append(FakeQQ().start())
        return made[0]

    yield from rig_fixture(make_config, monkeypatch, make)
    assert made[0].problems == []  # 真的 QQ 会拒绝的请求（往群里发、msg_id 不对……）一个都没有


def config_for(fake: FakeQQ) -> SimpleNamespace:
    return SimpleNamespace(qq_api=fake.base_url)  # check 只用到这一项


def tool(id: str, command: str) -> dict:
    return tool_use(id, "run_command", {"command": command})


# ---- 检查 AppID 和 AppSecret ----------------------------------------------------------


def test_good_credentials_give_the_bot(fake):
    bot = qq.check(config_for(fake), {"app_id": f"  {APP_ID}\n", "app_secret": f" {APP_SECRET} "})  # 粘贴时带上的空白去掉
    assert bot == {"id": BOT_ID, "username": BOT_NAME, "name": BOT_NAME, "at": False, "link": SHARE_URL}
    assert bot == fake.bot
    assert fake.requests == ["POST /app/getAppAccessToken", "GET /users/@me"]
    assert fake.refused == []


@pytest.mark.parametrize("app_id, secret, error", [
    ("", APP_SECRET, "AppID 应该是一串数字"),
    ("abc123", APP_SECRET, "AppID 应该是一串数字"),
    ("102 345", APP_SECRET, "AppID 应该是一串数字"),
    (APP_ID, "", "AppSecret 太短了"),
    (APP_ID, "short", "AppSecret 太短了"),
])
def test_malformed_credentials_are_rejected_without_asking_qq(fake, app_id, secret, error):
    with pytest.raises(ChannelError, match=error):
        qq.check(config_for(fake), {"app_id": app_id, "app_secret": secret})
    assert fake.requests == []


def test_a_wrong_secret_is_explained_without_showing_it(fake):
    wrong = "fakeWRONGsecret0000000000000000x"
    with pytest.raises(ChannelError) as caught:
        qq.check(config_for(fake), {"app_id": APP_ID, "app_secret": wrong})
    assert "不认这个 AppID 和 AppSecret" in str(caught.value) and wrong not in str(caught.value)
    assert fake.requests == ["POST /app/getAppAccessToken"]  # 换不到 token，就不再往下问了

    # QQ 的说明里万一带着 AppSecret（比如原样复述了请求），给人看的话里也换掉
    fake.fail_next["POST /app/getAppAccessToken"] = (200, {"code": 11255, "message": f"invalid request: clientSecret={APP_SECRET}"})
    with pytest.raises(ChannelError) as caught:
        qq.check(config_for(fake), {"app_id": APP_ID, "app_secret": APP_SECRET})
    assert "QQ 回答了一个错误" in str(caught.value) and APP_SECRET not in str(caught.value) and "<hidden>" in str(caught.value)

    # 连不上：说清楚是连不上
    with pytest.raises(ChannelError) as caught:
        qq.check(SimpleNamespace(qq_api="http://127.0.0.1:9"), {"app_id": APP_ID, "app_secret": APP_SECRET})
    assert "连不上 QQ" in str(caught.value) and APP_SECRET not in str(caught.value)


def test_connecting_qq_from_the_page(bot):
    s = bot(connected=False)
    platform = s.platform()
    assert (platform["configured"], platform["buttons"], platform["status"]["state"]) == (False, False, "off")  # 向导照着它说"回复文字"

    status, body = s.browser.post("/api/channel/connect", {"platform": "qq", "credentials": {"app_id": "QQ-bot", "app_secret": APP_SECRET}})
    assert status == 409 and "AppID 应该是一串数字" in body["error"]
    wrong = "fakeWRONGsecret0000000000000000x"
    status, body = s.browser.post("/api/channel/connect", {"platform": "qq", "credentials": {"app_id": APP_ID, "app_secret": wrong}})
    assert status == 409 and "不认这个 AppID 和 AppSecret" in body["error"] and wrong not in body["error"]
    assert not s.config.channels_file.exists()  # 不对的不保存

    status, body = s.browser.post("/api/channel/connect", {"platform": "qq", "credentials": {"app_id": APP_ID, "app_secret": f"{APP_SECRET}\n"}})
    assert (status, body) == (200, {"ok": True, "bot": {**s.fake.bot, "handle": BOT_NAME}})  # QQ 的机器人没有 @用户名
    until(lambda: s.platform()["status"]["state"] == "online", what="连上")
    platform = s.platform()
    assert platform["hints"] == {"app_id": APP_ID, "app_secret": "…" + APP_SECRET[-4:]}  # AppID 不是密钥，照常显示
    assert platform["token_hint"] == "…" + APP_SECRET[-4:]
    assert platform["bot"] == {"username": BOT_NAME, "name": BOT_NAME, "handle": BOT_NAME, "link": SHARE_URL, "install": ""}
    assert ChannelStore(s.config.channels_file).credentials("qq") == {"app_id": APP_ID, "app_secret": APP_SECRET}
    everything = json.dumps(s.browser.events, ensure_ascii=False)
    assert all(secret not in everything for secret in s.fake.secrets)  # AppSecret、换来的 access_token 都不在里面
    assert s.fake.identifies[0] == {"token": f"QQBot {s.fake.current}", "intents": INTENTS, "shard": [0, 1], "properties": {}}


# ---- 配对、陌生人、群、推了两次 ------------------------------------------------------------


def test_pairing_from_the_private_chat(bot):
    s = bot(paired=False)
    code = s.hub.pair("qq")["code"]
    until(lambda: (s.platform()["pairing"] or {}).get("code") == code, what="页面上的配对码")
    assert s.platform()["pairing"]["link"] == SHARE_URL  # "在 QQ 里打开"：打开和机器人的聊天，配对码要自己发

    start = s.say("你好")
    hint = s.wait_reply(start, "6 位配对码")
    assert (hint["chat"], hint["msg_id"], hint["msg_seq"]) == (OWNER, s.fake.last_message_id, 1)  # 被动回复
    wrong = "000000" if code != "000000" else "111111"
    s.wait_reply(s.say(wrong), f"还可以再试 {PAIRING_ATTEMPTS - 1} 次")
    paired = s.wait_reply(s.say(f"{code[:3]} {code[3:]}"), "配对成功")
    assert OWNER_NAME in paired["plain"] and "/new" in paired["plain"]
    assert "回复“允许”或“拒绝”" in paired["plain"] and "按钮" not in paired["plain"]  # QQ 上没有按钮
    until(lambda: (s.platform()["owner"] or {}).get("name") == OWNER_NAME, what="页面上的主人")
    assert "现在：空闲" in s.wait_reply(s.say("/status"), "现在：")["plain"]


def test_strangers_get_no_reply_and_group_messages_are_ignored(bot):
    s = bot()
    before = len(s.fake.sent)
    s.fake.user_says("你好", user=STRANGER)
    s.fake.user_says("/help", user=STRANGER)
    s.fake.user_says("123456", user=STRANGER)  # 不在配对的时候，猜配对码也不理
    s.fake.user_says("/status", private=False)  # 主人在群里 @ 机器人：群里的人都看得到，一律不理
    s.fake.user_says("你好", private=False, group=GROUP)
    s.sentinel()

    assert [message["chat"] for message in s.fake.sent[before:]] == [OWNER]  # 只有主人单聊里的那一条 /help 有回答
    assert s.api.requests == []
    assert [item.chat for item in s.hub.router.sessions("qq")] == [OWNER]
    until(lambda: s.platform()["ignored"] == 3, what="页面上的计数")  # 只数单聊里陌生人发来的
    assert not any(request.startswith("POST /v2/groups") for request in s.fake.requests)


def test_a_message_pushed_twice_is_handled_once(bot):
    s = bot(reply(text("只回答一次")))
    start = s.say("你好", twice=True)
    s.wait_reply(start, "只回答一次")
    s.idle()
    s.wait_reply(s.say("/help", twice=True), "直接发消息就行")
    s.sentinel()
    plains = [message["plain"] for message in s.fake.sent[start:]]
    assert plains.count("只回答一次") == 1 and sum("直接发消息就行" in plain for plain in plains) == 2  # 推了两次的 /help 只回一次，加上 sentinel 的那一次
    assert len(s.api.requests) == 1


# ---- 确认：只能回文字 --------------------------------------------------------------------


def test_a_confirmation_is_a_message_answered_with_words(bot):
    s = bot(run("toolu_1", "echo hi && pwd"), reply(text("**做完了**")))
    start = s.say("跑个命令")
    asked_by = s.fake.last_message_id
    ask = s.asked(start)
    # 标题、完整的命令、工作目录、怎么答复，全在一条消息里
    assert ask["plain"] == f"助手想要执行命令：\n\n$ echo hi && pwd\n\n工作目录：{s.config.workspace}\n\n{HINT}"
    assert (ask["msg_id"], ask["msg_seq"]) == (asked_by, 1)
    (pending,) = until(lambda: s.state()["channels"]["approvals"], what="页面上的确认请求")
    assert (pending["platform"], pending["chat"], pending["name"]) == ("qq", OWNER, OWNER_NAME)

    answer = s.say("允许")
    allowed_by = s.fake.last_message_id
    done = s.wait_reply(answer, "做完了")
    settled = s.wait_reply(answer, "已允许")
    assert settled["plain"] == "已允许"  # 消息改不了：结果另发一条
    assert s.fake.sent.index(settled) < s.fake.sent.index(done)
    assert done["plain"] == "做完了"  # Markdown 变成了纯文字
    assert (settled["msg_id"], settled["msg_seq"], done["msg_seq"]) == (allowed_by, 1, 2)  # 之后的话回的是"允许"那一条
    (result,) = tool_results(s.api.requests[1])
    assert "hi" in result_text(result) and str(s.config.workspace) in result_text(result) and not result.get("is_error")
    s.idle()
    until(lambda: s.state()["channels"]["approvals"] == [], what="页面上的确认请求消失")


def test_denying_with_words(bot):
    s = bot(run("toolu_a", "rm -rf build"), run("toolu_b", "rm -rf dist"), reply(text("好的，都不删了。")), reply(text("顺便的回答")))
    start = s.say("清理一下")
    first = s.asked(start)
    # 等着批准的时候又说了一句：告诉你它排在后面，怎么答复
    queued = s.wait_reply(s.say("顺便问一下"), "还有一个操作在等你批准")
    assert "回复“允许”或“拒绝”" in queued["plain"] and not s.fake.is_prompt(queued)
    after = s.say("拒绝")
    assert s.wait_reply(after, "已拒绝")["plain"] == "已拒绝"
    second = s.asked(s.fake.sent.index(first) + 1)
    assert "rm -rf dist" in second["plain"]
    s.say("No.")  # 不分大小写，标点去掉
    s.wait_reply(start, "好的，都不删了")
    s.wait_reply(start, "顺便的回答")  # 排着的那一条接着跑
    assert [message["plain"] for message in s.fake.sent if message["plain"].startswith("已")].count("已拒绝") == 2
    for request in s.api.requests[1:3]:
        (result,) = tool_results(request)
        assert result_text(result).startswith("用户拒绝了这个操作") and result["is_error"] is True
    s.idle()


def test_a_very_long_command_is_shown_in_full_before_the_hint(bot):
    command = "echo " + "ж" * 4500
    s = bot(run("toolu_1", command), reply(text("完")))
    start = s.say("跑一条很长的命令")
    ask = s.asked(start)
    pieces = s.fake.sent[start: s.fake.sent.index(ask) + 1]
    assert len(pieces) >= 3 and all(len(piece["plain"]) <= qq.SAFE_LIMIT for piece in pieces)
    assert pieces[0]["plain"].startswith("助手想要执行命令：\n\n$ echo ж")
    assert sum(piece["plain"].count("ж") for piece in pieces) == 4500  # 一个字都没少
    assert [s.fake.is_prompt(piece) for piece in pieces] == [False] * (len(pieces) - 1) + [True]  # 提示在最后
    assert [piece["msg_seq"] for piece in pieces] == list(range(1, len(pieces) + 1))
    s.say("好")
    s.wait_reply(start, "完")
    assert "ж" * 4500 in result_text(tool_results(s.api.requests[1])[0])
    s.idle()


def test_a_word_sent_before_the_prompt_arrives_does_not_approve_it(bot, monkeypatch):
    s = bot(
        reply(text("我先看看。"), tool("toolu_1", "touch approved.txt"), stop_reason="tool_use"),
        reply(text("完")), reply(text("嗯")),
    )
    channel = s.channel()
    original = channel.ask

    def slow_ask(chat, prompt):
        time.sleep(0.3)  # 网络慢、或者正被限流：确认请求晚一点才到手机上
        return original(chat, prompt)

    monkeypatch.setattr(channel, "ask", slow_ask)
    start = s.say("看看")
    s.wait_reply(start, "我先看看")
    until(lambda: s.session().waiting is not None, what="确认请求登记好")
    s.say("好")  # 回的是"我先看看。"这一句，这时手机上还没有确认请求
    s.asked(start)
    s.hub.router.flush()
    assert not (s.config.workspace / "approved.txt").exists()
    assert s.session().waiting is not None


def test_the_web_and_the_chat_race_for_the_same_confirmation(bot):
    s = bot(run("toolu_a", "echo a >> ran.txt"), run("toolu_b", "echo b >> ran.txt"), reply(text("完")), reply(text("多出来的一轮")))
    start = s.say("做两件事")
    first = s.asked(start)
    (pending,) = until(lambda: s.state()["channels"]["approvals"], what="页面上的确认请求")

    # 先在网页上允许：聊天里另发一条"已在网页上允许"；再答复就不算数了
    answer = {"platform": "qq", "chat": OWNER, "confirm_id": pending["id"], "allow": True}
    assert s.browser.post("/api/channel/confirm", answer) == (200, {"ok": True})
    assert s.wait_reply(s.fake.sent.index(first) + 1, "已在网页上允许")["plain"] == "已在网页上允许"
    status, body = s.browser.post("/api/channel/confirm", answer)
    assert status == 409 and "已经处理过了" in body["error"]

    # 两边同时答复：只有一边算数
    second = s.asked(s.fake.sent.index(first) + 1)
    (pending,) = until(lambda: [item for item in s.state()["channels"]["approvals"] if item["id"] != answer["confirm_id"]], what="第二个确认请求")
    web: list = []
    together = threading.Barrier(2)

    def click_on_the_page() -> None:
        together.wait()
        web.append(s.browser.post("/api/channel/confirm", {**answer, "confirm_id": pending["id"]}))

    racer = threading.Thread(target=click_on_the_page)
    racer.start()
    together.wait()
    s.say("允许")
    racer.join(10)
    s.wait_reply(start, "完")
    s.idle()
    web_won = web[0][0] == 200
    later = [message["plain"] for message in s.fake.sent[s.fake.sent.index(second) + 1:]]
    assert ("已在网页上允许" in later) == web_won and ("已允许" in later) != web_won
    assert (s.config.workspace / "ran.txt").read_text(encoding="utf-8") == "a\nb\n"  # 每条命令只执行了一次
    if web_won and "这个确认请求已经处理过了" not in later:
        # 网页先答、聊天里的"允许"在结果发出以后才到：已经没有在等的确认请求了，它成了一条普通的消息，交给模型
        assert "多出来的一轮" in later


def hold_the_settle(session, monkeypatch) -> tuple[threading.Event, threading.Event]:
    """让 agent 线程在"写出确认的结果"之前停住，等测试说可以了再写。返回（可以写了，写完了）。"""
    may_settle, settled = threading.Event(), threading.Event()
    original = session.ui._on_settled

    def held(*args) -> None:
        may_settle.wait(5)
        original(*args)
        settled.set()

    monkeypatch.setattr(session.ui, "_on_settled", held)
    return may_settle, settled


def test_a_chat_answer_after_the_web_answer_is_turned_away(bot, monkeypatch):
    s = bot(run("toolu_1", "echo a >> ran.txt"), reply(text("完")))
    start = s.say("跑")
    s.asked(start)
    may_settle, _ = hold_the_settle(s.session(), monkeypatch)
    (pending,) = until(lambda: s.state()["channels"]["approvals"], what="页面上的确认请求")
    assert s.browser.post("/api/channel/confirm", {"platform": "qq", "chat": OWNER, "confirm_id": pending["id"], "allow": True}) == (200, {"ok": True})
    # 网页上已经答了，聊天里还没说结果，这时你回了"允许"：告诉你已经处理过了，不会再执行一遍
    s.wait_reply(s.say("允许"), "这个确认请求已经处理过了")
    may_settle.set()
    assert s.wait_reply(start, "已在网页上允许")["plain"] == "已在网页上允许"
    s.wait_reply(start, "完")
    s.idle()
    assert (s.config.workspace / "ran.txt").read_text(encoding="utf-8") == "a\n"
    assert len(s.api.requests) == 2  # "允许"没有交给模型


def test_a_late_chat_answer_cannot_relabel_the_web_answer(bot, monkeypatch):
    """晚到的一方不能先把 waiting.by 改成自己、没答上再改回去：agent 的线程正好在这个间隙写结果的话，
    "已在网页上允许"就会被写成"已允许"（见 session.Session.answer）。"""
    s = bot(run("toolu_1", "echo a"), reply(text("完")))
    start = s.say("跑")
    s.asked(start)
    session = s.session()
    may_settle, settled = hold_the_settle(session, monkeypatch)
    original = session.ui.answer_confirm
    calls: list[int] = []

    def answer(confirm_id: int, allowed: bool, feedback: str) -> bool:
        calls.append(confirm_id)
        if len(calls) == 2:  # 聊天里晚到的那一次：by 已经被它改成了 chat，agent 线程恰好在这时写结果
            may_settle.set()
            settled.wait(5)
        return original(confirm_id, allowed, feedback)

    monkeypatch.setattr(session.ui, "answer_confirm", answer)
    (pending,) = until(lambda: s.state()["channels"]["approvals"], what="页面上的确认请求")
    assert s.browser.post("/api/channel/confirm", {"platform": "qq", "chat": OWNER, "confirm_id": pending["id"], "allow": True}) == (200, {"ok": True})
    s.wait_reply(s.say("允许"), "这个确认请求已经处理过了")
    may_settle.set()
    s.wait_reply(start, "完")
    s.idle()
    assert [message["plain"] for message in s.fake.sent if message["plain"] in ("已允许", "已在网页上允许")] == ["已在网页上允许"]


def test_stop_while_waiting_for_a_confirmation(bot):
    s = bot(run("toolu_1", "echo 不该运行 > ran.txt"), reply(text("停下以后的回答")))
    s.asked(s.say("跑个命令"))
    s.wait_reply(s.say("/new"), "这一轮还没结束")  # 正在进行的时候不能换一段新的
    stopping = s.say("/stop")
    s.wait_reply(stopping, "好，正在停下")
    s.wait_reply(stopping, "已中断。")
    assert any(message["plain"] == "已中断" for message in s.fake.sent[stopping:])  # 确认请求的结果另发一条
    s.idle()
    assert not (s.config.workspace / "ran.txt").exists()
    # 每个工具调用都有结果（被中断的也有），下一轮接着聊不会被 API 拒绝
    s.wait_reply(s.say("继续"), "停下以后的回答")
    history = s.api.requests[1]["messages"]
    results = [block for message in history if isinstance(message["content"], list) for block in message["content"] if block.get("type") == "tool_result"]
    assert [block["tool_use_id"] for block in results] == ["toolu_1"]
    s.idle()
    s.wait_reply(s.say("/stop"), "现在没有在进行的事")


# ---- 命令 ----------------------------------------------------------------------------


def test_help_and_status(bot):
    s = bot()
    help_message = s.wait_reply(s.say("/help"), "直接发消息就行")["plain"]
    for command in ("/new", "/stop", "/status", "/help"):
        assert command in help_message
    assert "回复“允许”或“拒绝”" in help_message and "按钮" not in help_message
    status = s.wait_reply(s.say("/status"), "现在：")["plain"]
    assert "确认方式：每次都问" in status and "会话：新对话" in status and "现在：空闲" in status
    assert s.api.requests == []  # 命令不经过模型，不花钱


def test_new_starts_another_conversation(bot):
    s = bot(reply(text("这是第一段的回答")), reply(text("这是第二段的回答")))
    s.wait_reply(s.say("第一段"), "这是第一段的回答")
    s.idle()
    first = s.session().conversation.id
    s.wait_reply(s.say("/new"), "开始一段新对话")
    s.wait_reply(s.say("第二段"), "这是第二段的回答")
    s.idle()
    second = s.session().conversation.id
    assert first != second
    assert len(s.api.requests[1]["messages"]) == 1  # 新的一段从头开始
    # 第一条消息前面附着的说明告诉模型：这是 QQ，要你回复"允许"才执行（没有按钮）
    opening = json.dumps(s.api.requests[0]["messages"][0], ensure_ascii=False)
    assert "这段对话在 QQ 里进行" in opening and "他回复“允许”才会执行" in opening
    assert {(item["title"], item.get("channel")) for item in s.app.store.summaries()} == {("第一段", "qq"), ("第二段", "qq")}


# ---- 被动回复和主动消息 ------------------------------------------------------------------


def five_messages() -> list[dict]:
    """全部自动允许时，一轮里说五句话：三段文字，两行"已自动允许"。"""
    return [
        reply(text("第一步"), tool("toolu_1", "echo 1"), stop_reason="tool_use"),
        reply(text("第二步"), tool("toolu_2", "echo 2"), stop_reason="tool_use"),
        reply(text("第三步")),
    ]


def test_more_than_four_replies_to_one_message_become_proactive(bot):
    s = bot(*five_messages(), reply(text("又能被动回复了")), approval="all")
    start = s.say("做三步")
    asked_by = s.fake.last_message_id
    s.wait_reply(start, "第三步")
    s.idle()
    turn = s.fake.sent[start:]
    assert [message["plain"].partition("：")[0] for message in turn] == ["第一步", "已自动允许", "第二步", "已自动允许", "第三步"]  # 一句都没丢
    assert [message["msg_id"] for message in turn] == [asked_by] * 4 + [None]  # 第 5 句改成主动消息
    assert [message["msg_seq"] for message in turn] == [1, 2, 3, 4, None]
    assert s.fake.rejected == []  # 智狐自己数着，没有去碰 QQ 的上限

    # 下一条消息：又可以被动回复了，从 1 数起
    start = s.say("再来")
    again = s.wait_reply(start, "又能被动回复了")
    assert (again["msg_id"], again["msg_seq"]) == (s.fake.last_message_id, 1)


def test_an_expired_message_id_falls_back_to_a_proactive_message(bot):
    s = bot(*five_messages(), approval="all")
    s.fake.passive_seconds = 0  # 在 QQ 看来，这条消息已经超过了 60 分钟
    start = s.say("做三步")
    s.wait_reply(start, "第三步")
    s.idle()
    turn = s.fake.sent[start:]
    assert len(turn) == 5 and all(message["msg_id"] is None for message in turn)
    assert turn[0]["plain"] == "第一步"
    (rejected,) = s.fake.rejected  # 只碰了一次壁：之后的直接发主动消息
    assert (rejected["code"], rejected["plain"], rejected["msg_seq"]) == (40034128, "第一步", 1)


@pytest.mark.parametrize("code", [40034128, 40034005, 304103, 40054005])
def test_passive_reply_errors_are_retried_as_proactive_messages(bot, code):
    s = bot(reply(text("照样收到了")))
    s.fake.fail_next[MESSAGES] = (400, {"code": code, "err_code": code, "message": "被动回复用不了"})
    answer = s.wait_reply(s.say("你好"), "照样收到了")
    assert (answer["msg_id"], answer["msg_seq"]) == (None, None)
    assert s.fake.requests.count(MESSAGES) == 2


def test_the_client_stops_replying_passively_after_its_own_window(bot, monkeypatch):
    monkeypatch.setattr(qq, "PASSIVE_SECONDS", 0)  # 智狐这边算着已经过了窗口：直接发主动消息，不去碰壁
    s = bot(reply(text("主动发的")))
    answer = s.wait_reply(s.say("你好"), "主动发的")
    assert answer["msg_id"] is None and s.fake.rejected == []


# ---- 排版和拆分 ------------------------------------------------------------------------


def test_markdown_arrives_as_plain_text(bot):
    markdown = "# 标题\n\n**粗体** 和 *斜体*，`code`，[链接](https://example.com)\n\n- 一\n- 二\n\n```python\nprint('hi')\n```"
    s = bot(reply(text(markdown)))
    answer = s.wait_reply(s.say("排版"), "标题")
    assert answer["plain"] == "标题\n\n粗体 和 斜体，code，链接（https://example.com）\n\n• 一\n• 二\n\nprint('hi')"
    assert "**" not in answer["plain"] and "```" not in answer["plain"]


def test_a_long_reply_is_split_and_nothing_is_lost(bot):
    paragraphs = [f"第{n}段：" + "长" * 1500 for n in range(1, 6)]
    s = bot(reply(text("\n\n".join(paragraphs))))
    start = s.say("写长一点")
    asked_by = s.fake.last_message_id
    s.wait_reply(start, "第5段")
    s.idle()
    pieces = s.fake.sent[start:]
    assert all(len(piece["plain"]) <= qq.SAFE_LIMIT for piece in pieces)
    assert [piece["plain"].strip() for piece in pieces] == paragraphs  # 在空行处断开，每段一条
    assert [piece["msg_id"] for piece in pieces] == [asked_by] * 4 + [None]  # 第 5 条主动发
    assert [piece["msg_seq"] for piece in pieces] == [1, 2, 3, 4, None]


# ---- access_token ----------------------------------------------------------------------


def test_the_access_token_is_reused_and_refreshed_when_it_stops_working(fake):
    api = qq.OpenAPI(APP_ID, APP_SECRET, fake.base_url)
    api.call("GET", "/users/@me")
    api.call("GET", "/gateway")
    assert fake.requests.count("POST /app/getAppAccessToken") == 1  # 两个小时内用同一个
    first = fake.current
    assert api.secrets == (APP_SECRET, first)

    fake.expire()  # 提前作废了：401，换一个新的再试一次，调用的人不会察觉
    assert api.call("GET", "/users/@me")["username"] == BOT_NAME
    assert fake.refused == ["GET /users/@me"] and fake.current != first
    assert fake.requests.count("POST /app/getAppAccessToken") == 2


def test_a_token_that_is_about_to_expire_is_replaced_first(fake):
    fake.token_seconds = 30  # 只剩 30 秒（不到一分钟）：每次用之前都换一个
    fake.expires_as_text = False  # 有的回答里 expires_in 是数字
    api = qq.OpenAPI(APP_ID, APP_SECRET, fake.base_url)
    api.call("GET", "/users/@me")
    api.call("GET", "/users/@me")
    assert fake.requests.count("POST /app/getAppAccessToken") == 2 and fake.refused == []


def test_rate_limiting_waits_and_tries_again(fake, monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(qq, "time", SimpleNamespace(time=time.time, sleep=waits.append))  # 不真的等
    api = qq.OpenAPI(APP_ID, APP_SECRET, fake.base_url)
    fake.fail_next["GET /users/@me"] = (429, {"code": 22009, "message": "请求过于频繁"})
    assert api.call("GET", "/users/@me")["id"] == BOT_ID
    assert waits == [1.0]  # 没说等多久：至少等一秒
    assert fake.requests.count("GET /users/@me") == 2


def test_an_expired_token_is_refreshed_without_you_noticing(bot):
    s = bot(reply(text("换了 token 也照常回答")))
    old = s.fake.current
    s.fake.expire()
    answer = s.wait_reply(s.say("你好"), "换了 token 也照常回答")
    assert answer["token"] == s.fake.current != old
    assert MESSAGES in s.fake.refused  # 第一次被 401，换了 token 再发
    assert s.channel().status.state == "online"


def test_close_4004_refreshes_the_token_and_logs_in_again(bot):
    s = bot(reply(text("重新登录以后的回答")))
    old = s.fake.current
    s.fake.expire()
    s.fake.close(4004, "invalid token")
    until(lambda: len(s.fake.identifies) == 2, what="重新 Identify")
    assert s.fake.identifies[-1]["token"] == f"QQBot {s.fake.current}" and s.fake.current != old
    assert s.fake.resumes == []  # token 不对：不是 Resume，而是重新登录
    until(lambda: s.channel().status.state == "online", what="又连上了")
    s.wait_reply(s.say("还在吗"), "重新登录以后的回答")


def test_a_rotated_secret_shows_an_error_and_stops_trying(fake):
    channel = qq.QQChannel(APP_ID, "fakeROTATEDsecret00000000000000x", fake.base_url)
    channel.start(lambda event: None)
    until(lambda: channel.status.state == "error", what="出错")
    assert "不认这个 AppID 和 AppSecret" in channel.status.message and "ROTATED" not in channel.status.message
    time.sleep(0.2)
    assert fake.requests == ["POST /app/getAppAccessToken"] and fake.connections == 0  # 再试也没用：不再去问
    channel.stop()
    channel.join(2)


def test_a_failed_token_renewal_while_logging_in_does_not_kill_the_channel(fake):
    # 快到两个小时、连接正好又断了：重新登录时要换 token，偏偏这一下没连上 QQ。收消息的线程不能就此退出、再也不连
    fake.token_seconds = 30  # 每个 token 都"只剩不到一分钟"：每次用之前都去换一个
    fake.scripts["POST /app/getAppAccessToken"] = [None, (500, {"code": 500, "message": "服务器忙"})]  # 第二次（Identify 时）出错
    channel = qq.QQChannel(APP_ID, APP_SECRET, fake.base_url)
    got: list = []
    channel.start(got.append)
    until(lambda: channel.status.state == "online", timeout=5, what="连上")
    assert len(fake.identifies) == 1
    fake.user_says("你好")
    until(lambda: got, what="收到消息")
    channel.stop()
    channel.join(2)


# ---- 网关：重连 --------------------------------------------------------------------------


def test_a_reconnect_request_resumes(bot):
    s = bot(reply(text("重连以后的回答")))
    s.fake.reconnect()
    until(lambda: s.fake.resumes, what="Resume")
    assert s.fake.resumes[0]["token"] == f"QQBot {s.fake.current}" and s.fake.resumes[0]["session_id"] == "qq-session-1"
    assert len(s.fake.identifies) == 1  # 没有重新登录
    until(lambda: s.channel().status.state == "online", what="又连上了")
    s.wait_reply(s.say("还在吗"), "重连以后的回答")


@pytest.mark.parametrize("resumable", [False, True])
def test_an_invalid_session(bot, resumable):
    s = bot(reply(text("会话失效以后的回答")))
    s.fake.invalid_session(resumable)
    if resumable:
        until(lambda: s.fake.resumes, what="Resume")
        assert len(s.fake.identifies) == 1
    else:
        until(lambda: len(s.fake.identifies) == 2, what="重新 Identify")
        assert s.fake.resumes == []
    until(lambda: s.channel().status.state == "online", what="又连上了")
    s.wait_reply(s.say("还在吗"), "会话失效以后的回答")


def test_a_dropped_connection_resumes_and_gets_what_it_missed(bot):
    s = bot(reply(text("断线时发的那条也收到了")))
    until(lambda: s.fake.heartbeats >= 1, what="心跳")
    s.fake.drop()
    start = s.say("断线的时候发的")  # 平台记着它，Resume 以后补发过来
    until(lambda: s.fake.resumes, what="Resume")
    assert len(s.fake.identifies) == 1
    s.wait_reply(start, "断线时发的那条也收到了")
    assert len(s.api.requests) == 1


def test_a_zombie_connection_is_noticed_and_resumed(bot):
    s = bot(reply(text("僵死以后的回答")))
    connections = s.fake.connections
    s.fake.ack = False  # 心跳没人回：连接多半已经断了，只是 TCP 还不知道
    until(lambda: s.fake.resumes, what="发现僵死、重连")
    s.fake.ack = True
    assert s.fake.connections > connections and len(s.fake.identifies) == 1
    until(lambda: s.channel().status.state == "online", what="又连上了")
    s.wait_reply(s.say("还在吗"), "僵死以后的回答")


def test_close_4914_stops_for_good(bot):
    s = bot()
    connections = s.fake.connections
    s.fake.close(4914, "bot offline")
    until(lambda: s.channel().status.state == "error", what="出错")
    assert "下线或者被封禁" in s.channel().status.message
    until(lambda: s.platform()["status"]["state"] == "error", what="页面上的状态")
    time.sleep(0.3)  # 退避只有 0.01 秒：要是还会重连，这时早就连上了
    assert s.fake.connections == connections


@pytest.mark.parametrize("code, outcome, says", [
    (4914, FATAL, "下线或者被封禁"),
    (4915, FATAL, "下线或者被封禁"),
    *[(code, FATAL, "拒绝了这次连接") for code in (4001, 4002, 4010, 4011, 4012, 4013, 4014)],
    (4008, SLOW_DOWN, "限流"),
    *[(code, IDENTIFY_NEXT, "") for code in (4004, 4006, 4007, 4900, 4905, 4913)],
    *[(code, RESUME_NEXT, "") for code in (4009, 1000, 1001, 1006, 4000)],
])
def test_classify_close_codes(code, outcome, says):
    got, why = qq.classify(code)
    assert got == outcome
    assert (says in why) if says else why == ""
    if outcome == FATAL and code not in (4914, 4915):
        assert str(code) in why  # 说出是哪个代码，查文档时用得上


# ---- 图片、文件、处理不了的 --------------------------------------------------------------


def test_an_image_reaches_the_model_as_an_image(bot):
    s = bot(reply(text("是一张很小的图")))
    start = s.say("这是什么", attachments=[("cat.png", "image/png", PNG_BYTES)])
    s.wait_reply(start, "是一张很小的图")
    content = s.api.requests[0]["messages"][-1]["content"]
    (image,) = [block for block in content if block["type"] == "image"]
    assert image["source"] == {"type": "base64", "media_type": "image/png", "data": base64.b64encode(PNG_BYTES).decode()}
    assert any(block["type"] == "text" and block["text"] == "这是什么" for block in content)
    s.idle()
    saved = s.app.store.load(s.session().conversation.id)
    (user,) = [event for event in saved.events if event["type"] == "user"]
    assert user["attachments"][0]["name"] == "cat.png" and user["attachments"][0]["kind"] == "image"


def test_a_file_reaches_the_model_as_text(bot):
    s = bot(reply(text("读完了")))
    start = s.say("", attachments=[("notes.txt", "file", "第一行\n第二行 ✓".encode())])  # 文件的 content_type 就是 "file"
    s.wait_reply(start, "读完了")
    sent = json.dumps(s.api.requests[0]["messages"][-1], ensure_ascii=False)
    assert "第二行 ✓" in sent and "notes.txt" in sent
    s.idle()


def test_voice_and_video_get_an_explanation(bot):
    s = bot()
    s.wait_reply(s.say("", attachments=[("voice.amr", "voice", b"#!AMR\n")]), "还处理不了语音：语音转文字暂时只支持 Telegram")
    s.wait_reply(s.say("", attachments=[("clip.mp4", "video/mp4", b"\x00\x00\x00\x18ftypmp42")]), "还处理不了视频")
    s.idle()
    assert s.api.requests == []


def test_a_message_becomes_an_inbound(fake):
    channel = qq.QQChannel(APP_ID, APP_SECRET, fake.base_url)
    fetched: list[str] = []
    channel._download = lambda url: fetched.append(url) or PNG_BYTES  # 不真的下载：只看去哪儿下
    data = {
        "id": "ROBOT1.0_unit-1",
        "author": {"user_openid": OWNER},
        "content": "  看看这张图 ",
        "attachments": [
            {"content_type": "image/png", "filename": "a.png", "size": 68, "url": "//multimedia.nt.qq.com.cn/download?appid=1&fileid=abc"},
            {"content_type": "file", "filename": "b.txt", "url": "https://multimedia.nt.qq.com.cn/download?fileid=def"},
            {"content_type": "image/png", "filename": "broken.png"},  # 没有网址：跳过
        ],
    }
    inbound = channel._message(data, private=True)
    assert (inbound.platform, inbound.chat, inbound.user, inbound.name, inbound.text, inbound.private) == ("qq", OWNER, OWNER, OWNER_NAME, "看看这张图", True)
    photo, document = inbound.files
    assert (photo.name, photo.media_type, photo.size, photo.kind) == ("a.png", "image/png", 68, "photo")
    assert (document.name, document.media_type, document.size, document.kind) == ("b.txt", "application/octet-stream", None, "document")
    photo.fetch()
    document.fetch()
    assert fetched == ["https://multimedia.nt.qq.com.cn/download?appid=1&fileid=abc", "https://multimedia.nt.qq.com.cn/download?fileid=def"]
    assert channel._replies[OWNER]["msg_id"] == "ROBOT1.0_unit-1"  # 之后的回复是被动回复，回的就是这一条

    assert channel._message(data, private=True) is None  # 同一条推了两次
    assert channel._message({"id": "ROBOT1.0_unit-2", "author": {"user_openid": OWNER}, "content": "   "}, private=True) is None  # 什么都没有
    assert channel._message({"id": "ROBOT1.0_unit-3", "author": {}, "content": "谁"}, private=True) is None  # 不知道是谁
    named = channel._message({"id": "ROBOT1.0_unit-4", "author": {"user_openid": OWNER, "username": "Akxan"}, "content": "嗨"}, private=True)
    assert named.name == "Akxan"
    group = channel._message({"id": "ROBOT1.0_unit-5", "author": {"member_openid": OWNER}, "group_openid": GROUP, "content": " 嗨"}, private=False)
    assert (group.chat, group.private, group.text) == (GROUP, False, "嗨")
    assert channel._replies[OWNER]["msg_id"] == "ROBOT1.0_unit-4"  # 群里的消息不能拿来被动回复单聊
    voice = channel._message({"id": "ROBOT1.0_unit-6", "author": {"user_openid": OWNER}, "attachments": [{"content_type": "voice", "url": "https://x/1.amr"}]}, private=True)
    assert voice.unsupported == "voice" and voice.files == []


def test_downloads_only_go_to_trusted_addresses(fake):
    channel = qq.QQChannel(APP_ID, APP_SECRET, fake.base_url)
    with pytest.raises(ChannelError, match="没能下载"):
        channel._download("http://multimedia.nt.qq.com.cn/download?fileid=abc")  # 明文的 http 只认这台电脑自己
    fake.files["x/a.txt"] = b"hello"
    assert channel._download(f"{fake.base_url}/cdn/x/a.txt") == b"hello"


# ---- 密钥 ------------------------------------------------------------------------------


def test_secrets_never_reach_errors_or_logs(bot, caplog):
    caplog.set_level(logging.DEBUG, logger="harness")
    s = bot(reply(text("第一句")), reply(text("第二句")))
    s.fake.expire()  # 换一次 token：两个 access_token 都要挡住
    token = s.fake.current
    s.fake.fail_next[MESSAGES] = (500, {"code": 5000, "message": f"internal error, secret={APP_SECRET} token={token}"})
    s.say("你好")  # 这一句的回答发不出去：日志里记一笔，里面没有密钥
    until(lambda: "发送失败" in caplog.text, what="日志里的发送失败")
    s.wait_reply(s.say("再试"), "第二句")
    s.idle()
    s.fake.close(4004, "invalid token")  # 再走一遍"token 不对、换一个、重新登录"
    until(lambda: len(s.fake.identifies) == 2, what="重新 Identify")
    assert "<hidden>" in caplog.text
    for secret in s.fake.secrets:
        assert secret not in caplog.text
        assert secret not in s.channel().status.message
    assert len(s.fake.secrets) >= 3  # AppSecret 和至少两个 access_token


def test_the_export_leaves_out_the_app_secret(tmp_path):
    data = tmp_path / "data"
    ChannelStore(data / "channels.json").connect("qq", {"app_id": APP_ID, "app_secret": APP_SECRET}, FakeQQ().bot)
    out = io.BytesIO()
    write_archive(data, out, datetime(2026, 11, 21, 9, 0))
    archive = zipfile.ZipFile(io.BytesIO(out.getvalue()))
    entry = json.loads(archive.read("channels.json"))["platforms"]["qq"]
    assert entry["credentials"] == {"app_id": APP_ID, "app_secret": PLACEHOLDER}  # AppID 不是密钥，留着好恢复
    everything = b"".join(archive.read(name) for name in archive.namelist())
    assert APP_SECRET.encode() not in everything
