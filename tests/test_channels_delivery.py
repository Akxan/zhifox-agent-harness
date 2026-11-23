"""往聊天里发东西失败了：分清暂时的和一直的、再试、留着等连上了补发、页面和 /status 上看得到、停下和退出不等
（harness/channels/outbox.py、base.retry_delay），以及 Telegram 断了以后马上显示"在线"（telegram.py 的 _poll）。

前面一节不联网：一个按剧本失败的假通道（Flaky）对着真的 Outbox。后面对着本地的假 Telegram（tests/fake_telegram.py 的
trouble：一个字都不回就关掉连接、500、429、403），把整个程序组装起来（test_channels.py 的 Rig），走真实的长轮询、
发送队列、agent 循环。等待一律用 until 等条件成立，不写固定的 sleep；重试的间隔在测试里调成几毫秒（fast），
要测"等着重试的时候"的才调长，然后看它是不是马上醒过来。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest
from fake_api import reply, text
from fake_telegram import TOKEN, FakeTelegram
from sample_files import PDF_BYTES
from test_channels import OWNER, rigs, run, stop_later, until, visible_end
from test_send_file import outgoing, result_of, send

from harness import config
from harness.channels import dingtalk, discord, feishu, qq, slack
from harness.channels.base import ChannelError, Status, pause, retry_delay, transient
from harness.channels.outbox import Job, Outbox
from harness.channels.session import delivery_lines
from harness.channels.telegram import BotAPI, TelegramChannel, TelegramError
from harness.config import CHANNEL_SEND_RETRIES, CHANNEL_SEND_RETRY_AFTER_MAX
from harness.net import http

CLOSED = "http://127.0.0.1:9"  # 这台电脑上没人在听的端口：连不上（和 test_channels.py 里一样）
RESET = ChannelError("连接被重置", status=0)
BLOCKED = ChannelError("Telegram 拒绝了：你可能把这个机器人停用（Block）了。", status=403)


@pytest.fixture
def fast(monkeypatch):
    """重试的间隔调成几毫秒；定时补发调到一分钟（这些测试不想让它插进来），要测它的自己再调。"""
    monkeypatch.setattr(config, "CHANNEL_SEND_RETRY_SECONDS", 0.005)
    monkeypatch.setattr(config, "CHANNEL_UNDELIVERED_RETRY_SECONDS", 60.0)


# ---- 哪种失败再试有用 -------------------------------------------------------------


def test_failures_are_sorted_into_passing_and_lasting():
    # 暂时的：没得到回答（连接被重置、超时、域名解析失败）、平台出了故障、被限流、请求超时
    for status in (0, 408, 429, 500, 502, 503, 504):
        assert transient(ChannelError("x", status=status)), status
    # 一直的：请求不对、token 不对、你把机器人停用了、找不到这个聊天、消息太长……再试也一样
    for status in (400, 401, 403, 404, 413, None):
        assert not transient(ChannelError("x", status=status)), status
    assert not transient(ValueError("不是通道的错"))
    # 平台用自己的说法说"太快了"，HTTP 状态却是 200：照样算暂时的
    assert transient(ChannelError("x", status=200, busy=True))
    # 对方说了过多久再来：听它的，但不会无限地等下去
    assert retry_delay(ChannelError("x", status=429, retry_after=2.5)) == 2.5
    assert retry_delay(ChannelError("x", status=429, retry_after=1e9)) == CHANNEL_SEND_RETRY_AFTER_MAX
    assert retry_delay(ChannelError("x", status=503)) == 0.0


def test_each_platform_fills_in_what_the_classification_needs():
    # 各平台的错误照着回答填上 HTTP 状态码和"太快了"：判断只在 retry_delay 一处
    assert transient(TelegramError("x", 502)) and not transient(TelegramError("x", 403)) and not transient(TelegramError("看不懂"))
    assert transient(TelegramError("x", 429, retry_after=3)) and retry_delay(TelegramError("x", 429, retry_after=3)) == 3
    assert transient(discord.DiscordError("x", 500, 0)) and not transient(discord.DiscordError("x", 403, 50007))
    assert transient(slack.SlackError("x", "ratelimited", 200)) and transient(slack.SlackError("x", "internal_error", 200))
    assert not transient(slack.SlackError("x", "channel_not_found", 200)) and not transient(slack.SlackError("x", "invalid_auth", 200))
    assert transient(feishu.FeishuError("x", 99991400, 200)) and not transient(feishu.FeishuError("x", 230002, 400))
    assert transient(feishu.FeishuError("飞书的回答看不懂（HTTP 502）", status=502))
    assert transient(qq.QQError("x", 200, 40034100)) and not transient(qq.QQError("x", 200, 40054013))
    assert transient(dingtalk.DingTalkError("x", "Throttling.TooFast", 200)) and not transient(dingtalk.DingTalkError("x", "Forbidden", 403))


def test_a_platform_that_cannot_be_reached_is_a_passing_failure():
    # 真的去连一个没人听的端口：每个平台的客户端都说"没得到回答"（status 0），发送队列就知道该再试
    attempts = [
        lambda: BotAPI(TOKEN, CLOSED).call("sendMessage", {"chat_id": "1", "text": "x"}),
        lambda: discord.RestAPI("x" * 59, CLOSED).call("POST", "/channels/1/messages", {"content": "x"}),
        lambda: slack.WebAPI("xoxb-1", "xapp-1", CLOSED).call("chat.postMessage", {"channel": "C1", "text": "x"}),
        lambda: feishu.OpenAPI("cli_x", "secret" * 4, CLOSED).call("POST", "/open-apis/im/v1/messages", {}),
        lambda: dingtalk.OpenAPI("ding", "secret" * 4, CLOSED).call("/v1.0/robot/oToMessages/batchSend", {}),
        lambda: qq.OpenAPI("1", "secret" * 4, CLOSED).call("POST", "/v2/users/1/messages", {}),
    ]
    for attempt in attempts:
        with pytest.raises(ChannelError) as caught:
            attempt()
        assert caught.value.status == 0 and transient(caught.value), caught.value
    # 得到了回答、但用不了的（网址写错了）不算
    with pytest.raises(http.NetError) as wrong:
        http.request("GET", "not a url")
    assert wrong.value.status is None


# ---- 发送队列本身（不联网） ---------------------------------------------------------------


class Flaky:
    """一个假的通道：send、send_file 按剧本失败（剧本里每一项是这一次要抛的错误，None 是这一次发成功；剧本用完了都成功）。"""

    platform = "telegram"

    def __init__(self, *script: ChannelError | None, state: str = "online"):
        self.script = list(script)
        self.attempts: list[str] = []
        self.sent: list[str] = []
        self.status = Status(state)
        self._lock = threading.Lock()

    def _try(self, what: str) -> None:
        with self._lock:
            self.attempts.append(what)
            failure = self.script.pop(0) if self.script else None
        if failure is not None:
            raise failure
        with self._lock:
            self.sent.append(what)

    def send(self, chat: str, text: str, markdown: bool = False) -> None:
        self._try(text)

    def send_file(self, chat: str, item) -> None:
        self._try(item.name)


def say(text: str, chat: str = "1") -> Job:
    return Job("say", chat, lambda channel: channel.send(chat, text))


def make_outbox(channel) -> tuple[Outbox, list[int]]:
    """一个对着 channel 的发送队列；第二个返回值记着它说"状态变了"的次数（页面会跟着刷新）。"""
    changes: list[int] = []
    holder = {"channel": channel}
    outbox = Outbox("telegram", lambda: holder["channel"], lambda: changes.append(1))
    outbox.holder = holder  # type: ignore[attr-defined]  # 测试里换通道（关掉、重新开）用
    return outbox, changes


def test_a_passing_failure_is_tried_again_with_growing_waits_and_the_order_is_kept(fast, caplog):
    caplog.set_level(logging.INFO, logger="harness.channels")
    channel = Flaky(RESET, RESET)
    outbox, _ = make_outbox(channel)
    outbox.put(say("第一段"))
    outbox.put(say("第二段"))
    assert outbox.flush(5)
    assert channel.attempts == ["第一段", "第一段", "第一段", "第二段"]  # 第一段试了三次才发出去，第二段一直等着它
    assert channel.sent == ["第一段", "第二段"]
    # 每一次失败日志里一行，间隔每次翻倍
    retries = [record.getMessage() for record in caplog.records if "秒后再试" in record.getMessage()]
    assert len(retries) == 2 and "0.005 秒后再试（第 1/4 次）" in retries[0] and "0.01 秒后再试（第 2/4 次）" in retries[1]
    assert outbox.summary() == {"waiting": 0, "lost": 0, "failed_at": None, "error": ""}
    outbox.close(1)


def test_rate_limiting_waits_as_long_as_the_platform_says(fast):
    channel = Flaky(ChannelError("发得太快了", status=429, retry_after=0.3))
    outbox, _ = make_outbox(channel)
    started = time.monotonic()
    outbox.put(say("你好"))
    assert outbox.flush(5)
    assert time.monotonic() - started >= 0.25 and channel.sent == ["你好"]  # 留一点余量：Windows 上计时一格是 15.6 毫秒
    outbox.close(1)


def test_a_lasting_failure_is_not_tried_again_and_is_counted(fast, caplog):
    caplog.set_level(logging.INFO, logger="harness.channels")
    channel = Flaky(BLOCKED)
    outbox, changes = make_outbox(channel)
    outbox.put(say("这段没人收得到"))
    outbox.put(say("下一段"))
    assert outbox.flush(5)
    assert channel.attempts == ["这段没人收得到", "下一段"]  # 只试了一次；后面的照常发
    summary = outbox.summary()
    assert (summary["waiting"], summary["lost"], summary["error"]) == (0, 1, BLOCKED.args[0]) and summary["failed_at"]
    assert changes  # 页面要知道
    assert [record.getMessage() for record in caplog.records if "发送失败" in record.getMessage()] == [f"telegram: 发送失败，不再重试：{BLOCKED}"]
    outbox.close(1)


def test_what_still_fails_waits_in_order_and_goes_out_when_the_channel_is_back(fast):
    channel = Flaky(*[RESET] * (CHANNEL_SEND_RETRIES + 1), state="connecting")
    outbox, changes = make_outbox(channel)
    waiting = SimpleNamespace(delivery="", problem="", handle="")

    def ask(channel) -> None:
        channel.send("1", "确认请求")
        waiting.handle = "42"

    outbox.put(say("回答"))
    outbox.put(Job("ask", "1", ask, waiting=waiting))
    outbox.put(say("别的聊天里的话", chat="2"))
    assert outbox.flush(5)
    # 回答试了 1 + 4 次还是不行：留着；同一个聊天里后面的确认请求不插队，也留着；别的聊天照常发
    assert channel.attempts == ["回答"] * (CHANNEL_SEND_RETRIES + 1) + ["别的聊天里的话"]
    assert channel.sent == ["别的聊天里的话"]
    summary = outbox.summary()
    assert (summary["waiting"], summary["lost"], summary["error"]) == (2, 0, RESET.args[0]) and summary["failed_at"]
    assert (outbox.waiting("1"), outbox.waiting("2")) == (2, 0)
    assert waiting.delivery == "waiting" and waiting.handle == ""  # 页面上写"还没发到聊天里"，网页上照样能答
    assert changes

    # 通道重新连上（router 收到"在线"就调 resume）：按顺序补发
    channel.status = Status("online")
    outbox.resume()
    until(lambda: channel.sent == ["别的聊天里的话", "回答", "确认请求"], what="补发")
    assert outbox.summary()["waiting"] == 0 and waiting.delivery == "" and waiting.handle == "42"
    outbox.close(1)


def test_while_the_channel_looks_online_it_tries_again_now_and_then(fast, monkeypatch):
    # Telegram 的长轮询一直通，只是发消息的那一下被重置了：状态一直是"在线"，没有"重新连上"可等
    monkeypatch.setattr(config, "CHANNEL_UNDELIVERED_RETRY_SECONDS", 0.1)
    channel = Flaky(*[RESET] * (CHANNEL_SEND_RETRIES + 1))
    outbox, _ = make_outbox(channel)
    outbox.put(say("回答"))
    until(lambda: channel.sent == ["回答"], what="定时补发")
    assert outbox.summary()["waiting"] == 0
    outbox.close(1)


def test_the_messages_kept_per_chat_are_bounded(fast, monkeypatch):
    monkeypatch.setattr(config, "CHANNEL_UNDELIVERED_MAX", 3)
    outbox, _ = make_outbox(None)  # 通道关着（或者正在换 token）：不干等，直接留着
    for number in range(5):
        outbox.put(say(f"第 {number + 1} 段"))
    assert outbox.flush(5)
    assert outbox.summary()["waiting"] == 3 and outbox.summary()["lost"] == 2  # 最早的两段挤掉了，算进"没能发出去"
    channel = Flaky()
    outbox.holder["channel"] = channel  # type: ignore[attr-defined]
    outbox.resume()
    until(lambda: channel.sent == ["第 3 段", "第 4 段", "第 5 段"], what="补发留着的三段")
    outbox.close(1)


def test_forgetting_a_chat_drops_what_it_kept(fast):
    outbox, _ = make_outbox(None)
    outbox.put(say("给以前那个主人的", chat="old"))
    outbox.put(say("给现在的主人的", chat="new"))
    assert outbox.flush(5) and outbox.summary()["waiting"] == 2
    outbox.forget(lambda chat: chat == "old")  # 换了主人、解除绑定：不再发到那个聊天里去
    assert (outbox.waiting("old"), outbox.waiting("new")) == (0, 1)
    outbox.close(1)


def test_status_goes_ahead_of_the_waiting_messages(fast):
    channel = Flaky(*[RESET] * (CHANNEL_SEND_RETRIES + 1), state="connecting")
    outbox, _ = make_outbox(channel)
    outbox.put(say("回答"))
    assert outbox.flush(5) and outbox.waiting("1") == 1
    # /status 的回答在发的那一刻才写，而且不排在留着的那些后面：它要说的正是"还有几条没发出去"
    outbox.put(Job("status", "1", lambda channel: channel.send("1", f"还有 {outbox.waiting('1')} 条")))
    assert outbox.flush(5)
    assert channel.sent == ["还有 1 条"] and outbox.waiting("1") == 1
    outbox.close(1)


def test_typing_is_dropped_while_messages_wait_and_can_be_queued_again(fast):
    channel = Flaky(*[RESET] * (CHANNEL_SEND_RETRIES + 1), state="connecting")
    outbox, _ = make_outbox(channel)
    typed: list[str] = []
    outbox.put(say("回答"))
    outbox.typing("1", lambda channel: typed.append("1"))
    assert outbox.flush(5)
    assert typed == []  # 前面的话都还没到，"正在输入"没意义
    channel.status = Status("online")
    outbox.resume()
    until(lambda: channel.sent == ["回答"], what="补发")
    outbox.typing("1", lambda channel: typed.append("1"))  # 上一次没发，也不能挡着以后的
    assert outbox.flush(5) and typed == ["1"]
    outbox.close(1)


def file_job(done: list, stopped: threading.Event | None = None) -> Job:
    return Job("file", "1", lambda channel: channel.send_file("1", outgoing("a.pdf")), cancelled=(stopped or threading.Event()).is_set,
               done=lambda state, error: done.append((state, error)))


def test_a_file_is_tried_again_but_never_kept_for_later(fast):
    channel = Flaky(*[RESET] * (CHANNEL_SEND_RETRIES + 1))
    outbox, _ = make_outbox(channel)
    done: list = []
    outbox.put(file_job(done))
    until(lambda: done, what="文件的结果")
    ((state, error),) = done
    assert state == "failed" and error is not None and transient(error)  # 模型会被如实告知：没有发出去
    assert channel.attempts == ["a.pdf"] * (CHANNEL_SEND_RETRIES + 1) and outbox.summary()["waiting"] == 0
    outbox.close(1)


def test_a_file_does_not_jump_ahead_of_the_words_waiting_before_it(fast):
    channel = Flaky(*[RESET] * (CHANNEL_SEND_RETRIES + 2), state="connecting")
    outbox, _ = make_outbox(channel)
    done: list = []
    outbox.put(say("截好了，发给你"))
    outbox.put(file_job(done))
    until(lambda: done, what="文件的结果")
    ((state, error),) = done
    assert state == "failed" and "前面还有 1 条消息没发到聊天里" in str(error)
    assert "a.pdf" not in channel.attempts  # 先试着补发前面那一句，补不出去就不发文件
    outbox.close(1)


@pytest.mark.parametrize(("failure", "outcome"), [(RESET, "pending"), (ChannelError("Telegram 出了故障", status=500), "skipped")], ids=["reset", "500"])
def test_a_stopped_file_is_not_tried_again(fast, monkeypatch, failure, outcome):
    monkeypatch.setattr(config, "CHANNEL_SEND_RETRY_SECONDS", 30.0)  # 等着重试的时候
    channel = Flaky(*[failure] * 10)
    outbox, _ = make_outbox(channel)
    done: list = []
    stopped = threading.Event()
    outbox.put(file_job(done, stopped))
    until(lambda: channel.attempts, what="第一次上传")
    began = time.monotonic()
    stopped.set()  # /stop
    until(lambda: done, what="不再等着重试")
    assert time.monotonic() - began < 5 and channel.attempts == ["a.pdf"]
    # 对方答了"不行"：就是没发出去；连接被重置、没得到回答：说不准它到了没有（模型会被告知"可能发出去了"）
    assert done == [(outcome, None)]
    outbox.close(1)


def test_closing_does_not_wait_for_the_backoff(fast, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="harness.channels")
    monkeypatch.setattr(config, "CHANNEL_SEND_RETRY_SECONDS", 30.0)
    channel = Flaky(*[RESET] * 10)
    outbox, _ = make_outbox(channel)
    outbox.put(say("回答"))
    until(lambda: channel.attempts, what="第一次发送")
    began = time.monotonic()
    outbox.close(5)
    assert time.monotonic() - began < 3  # 不等那 30 秒：再试一次，还是不行就写进日志
    assert len(channel.attempts) <= 3
    assert "退出时还有 1 条消息没发到聊天里" in caplog.text


def test_a_platforms_own_rate_limit_wait_ends_when_closing(fast):
    # 平台的代码被限流时等的那一下（base.pause）：在发送队列的线程里，退出时马上醒过来
    outbox, _ = make_outbox(Flaky())
    started = threading.Event()

    def slow(channel) -> None:
        started.set()
        pause(30)

    outbox.put(Job("say", "1", slow))
    assert started.wait(5)
    began = time.monotonic()
    outbox.close(5)
    assert time.monotonic() - began < 3


def test_status_lines_say_what_is_waiting():
    assert delivery_lines({"waiting": 0, "lost": 0, "failed_at": None, "error": ""}, 0) == []  # 都发出去了：一行都不写
    lines = delivery_lines({"waiting": 2, "lost": 1, "failed_at": time.time(), "error": "连不上 Telegram（Connection reset by peer）"}, 2)
    assert lines[0] == "还没发到这里的消息：2 条（连上以后会按顺序补发）" and lines[1] == "没能发出去的消息：1 条"
    assert lines[2].startswith("最后一次失败：") and lines[2].endswith("（连不上 Telegram（Connection reset by peer））")


# ---- 整个程序：手机 → 智狐 → 手机，中间的网络出毛病 -------------------------------------------------


@pytest.fixture
def bot(make_config, monkeypatch, fast):
    yield from rigs(make_config, monkeypatch)


def replies_with(s, words: str) -> list[dict]:
    return [message for message in s.tg.sent if words in message["plain"]]


def test_a_reply_hit_by_a_connection_reset_still_arrives(bot):
    # 2026-11-22 22:38 真实日志里丢掉的那一段回答：发的那一下连接被对方重置了
    s = bot(reply(text("这是回答")))
    s.tg.trouble("sendMessage", "reset", "reset")
    s.wait_reply(s.say("你好"), "这是回答")
    s.idle()
    assert len(replies_with(s, "这是回答")) == 1 and s.tg.calls.count("sendMessage") == 3
    assert s.platform()["delivery"]["waiting"] == 0 and s.platform()["delivery"]["lost"] == 0


def test_a_telegram_outage_is_waited_out(bot):
    s = bot(reply(text("故障过去了")))
    s.tg.trouble("sendMessage", 502, 500)
    s.wait_reply(s.say("你好"), "故障过去了")
    s.idle()
    assert s.tg.calls.count("sendMessage") == 3


def test_a_blocked_bot_is_not_asked_again_and_the_page_says_so(bot):
    s = bot(reply(text("你收不到这段")))
    s.tg.trouble("sendMessage", {"error_code": 403, "description": "Forbidden: bot was blocked by the user"})
    s.say("你好")
    until(lambda: s.platform()["delivery"]["lost"] == 1, what="页面上写着有一条没能发出去")
    delivery = s.platform()["delivery"]
    assert delivery["waiting"] == 0 and delivery["failed_at"] and "停用" in delivery["error"]
    s.idle()
    assert s.tg.calls.count("sendMessage") == 1 and replies_with(s, "你收不到这段") == []  # 一直的失败：不再试
    status = s.wait_reply(s.say("/status"), "现在：")["plain"]
    assert "没能发出去的消息：1 条" in status and "最后一次失败：" in status and "停用" in status


def test_a_reply_that_still_fails_waits_and_goes_out_when_telegram_is_back(bot):
    s = bot(reply(text("迟到的回答")))
    s.tg.trouble("sendMessage", *["reset"] * (CHANNEL_SEND_RETRIES + 1))
    s.say("你好")
    until(lambda: s.platform()["delivery"]["waiting"] == 1, what="页面上写着有一条在等着补发")
    assert "连不上 Telegram" in s.platform()["delivery"]["error"] and replies_with(s, "迟到的回答") == []
    # Telegram 那边的连接也被重置了一次：长轮询断开、马上重新连上（状态变回"在线"），留着的马上补发。
    # 这里没有人发消息（收到消息也会让它补发），定时补发调到了一分钟以后：只能是"重新连上"让它发的
    s.tg.trouble("getUpdates", "reset")
    until(lambda: s.channel().status.state == "connecting", what="长轮询被重置")
    s.wait_reply(0, "迟到的回答")
    assert s.channel().status.state == "online"
    until(lambda: s.platform()["delivery"]["waiting"] == 0, what="页面上的数字回到 0")


def test_status_in_the_chat_says_what_is_waiting_and_then_it_arrives_in_order(bot):
    s = bot(reply(text("第一段回答")), reply(text("第二段回答")))
    # 回答试了 5 次都失败、留着；你发 /status 时补发它的那一次也失败；/status 的回答自己发出去了
    s.tg.trouble("sendMessage", *["reset"] * (CHANNEL_SEND_RETRIES + 2))
    s.say("你好")
    until(lambda: s.session().snapshot()["undelivered"] == 1, what="留着一条")
    status = s.wait_reply(s.say("/status"), "现在：")["plain"]
    assert "还没发到这里的消息：1 条（连上以后会按顺序补发）" in status and "最后一次失败：" in status and "连不上 Telegram" in status
    # 再发一条：收得到你的消息，多半也发得出去，留着的先补发，再是新的回答
    s.say("还在吗")
    second = s.wait_reply(0, "第二段回答")
    first = s.wait_reply(0, "第一段回答")
    assert first["message_id"] < second["message_id"]
    s.idle()
    assert s.session().snapshot()["undelivered"] == 0


def test_an_approval_that_did_not_reach_the_chat_is_marked_and_can_be_answered_on_the_page(bot):
    s = bot(run("toolu_1", "echo 补发 > done.txt"), reply(text("命令执行完了")))
    s.tg.trouble("sendMessage", *["reset"] * (CHANNEL_SEND_RETRIES + 1))  # 确认请求那条发不出去
    s.say("跑个命令")
    (ask,) = until(lambda: [item for item in s.state()["channels"]["approvals"] if item["delivery"] == "waiting"], what="页面上：确认请求还没发到聊天里")
    assert "连不上 Telegram" in ask["problem"]
    assert s.tg.sent == []
    # 这一轮一直在等它：在网页上答复
    assert s.browser.post("/api/channel/confirm", {"platform": "telegram", "chat": OWNER, "confirm_id": ask["id"], "allow": True})[0] == 200
    until(lambda: (s.config.workspace / "done.txt").exists(), what="命令执行了")
    until(lambda: not s.session().running, what="这一轮结束")
    assert s.session().snapshot()["undelivered"] == 3  # 确认请求、改成"已在网页上允许"、回答：都排着，顺序不乱
    # 聊天里又来了一句（收得到，多半也发得出去）：按顺序补发——先是确认请求，马上改成结果，再是回答
    s.sentinel()
    card = s.tg.wait_sent(lambda message: bool(message.get("reply_markup")))
    assert visible_end(s.edit_of(card)) == "已在网页上允许"
    assert card["message_id"] < s.wait_reply(0, "命令执行完了")["message_id"]


def test_an_edit_that_reached_telegram_but_lost_its_answer_is_not_sent_again():
    # 改确认请求的那一下其实到了 Telegram、只是回答丢了：再改一次 Telegram 说 "message is not modified"，当作改好了
    fake = FakeTelegram(max_poll=0.2).start()
    try:
        channel = TelegramChannel(TOKEN, fake.base_url)
        handle = channel.ask(OWNER, SimpleNamespace(id=1, title="助手想要执行命令", detail="ls", where="工作目录：/tmp", allow="允许", deny="拒绝", hint=""))
        fake.trouble("editMessageText", "reset")
        with pytest.raises(TelegramError) as caught:
            channel.settle(OWNER, handle, "已允许")
        assert transient(caught.value)  # 连不上：原样抛出，发送队列会再改一次
        fake.fail_next["editMessageText"] = {"error_code": 400, "description": "Bad Request: message is not modified: specified new message content and reply markup are exactly the same"}
        before = len(fake.sent)
        channel.settle(OWNER, handle, "已允许")
        assert len(fake.sent) == before  # 不另发一条"已允许"
        channel.settle(OWNER, handle, "已允许")  # 改好了就不再记着它：再来一次什么都不做
        assert fake.calls.count("editMessageText") == 2
    finally:
        stop_later(fake.stop)


def test_a_file_that_still_fails_is_reported_to_the_model(bot):
    s = bot(send("toolu_1", "a.pdf"), reply(text("没发出去，我稍后再试")), approval="all")
    (s.config.workspace / "a.pdf").write_bytes(PDF_BYTES)
    s.tg.trouble("sendDocument", *[500] * (CHANNEL_SEND_RETRIES + 1))
    s.wait_reply(s.say("把报告发我"), "没发出去")
    s.idle()
    assert s.tg.uploads == [] and s.tg.calls.count("sendDocument") == CHANNEL_SEND_RETRIES + 1
    result = result_of(s.api.requests[1])
    assert result["error"] and result["text"] == "没有发出去：Telegram 回答了一个错误：Internal Server Error"


def test_a_file_hit_by_a_connection_reset_is_sent_on_the_next_try(bot):
    s = bot(send("toolu_1", "a.pdf"), reply(text("发好了")), approval="all")
    (s.config.workspace / "a.pdf").write_bytes(PDF_BYTES)
    s.tg.trouble("sendDocument", "reset")
    s.wait_reply(s.say("把报告发我"), "发好了")
    s.idle()
    assert len(s.tg.uploads) == 1 and not result_of(s.api.requests[1])["error"]


def test_stop_does_not_wait_while_a_file_waits_to_be_tried_again(bot, monkeypatch):
    s = bot(send("toolu_1", "a.pdf"), reply(text("停下以后")), approval="all")
    monkeypatch.setattr(config, "CHANNEL_SEND_RETRY_SECONDS", 30.0)  # 第一次失败以后要等 30 秒才再试
    (s.config.workspace / "a.pdf").write_bytes(PDF_BYTES)
    s.tg.trouble("sendDocument", *[500] * 10)
    s.say("把报告发我")
    until(lambda: s.tg.calls.count("sendDocument") == 1, what="第一次上传失败")
    began = time.monotonic()
    stopping = s.say("/stop")
    # 这一轮马上停下；发送队列也不再等着重试这个文件（不然"已中断。"要排在它后面等半分钟）
    s.wait_reply(stopping, "已中断。")
    assert time.monotonic() - began < 15
    s.idle()
    assert s.tg.calls.count("sendDocument") == 1 and s.tg.uploads == []
    s.wait_reply(s.say("继续"), "停下以后")
    (result,) = [block for message in s.api.requests[1]["messages"] if isinstance(message["content"], list) for block in message["content"] if block.get("type") == "tool_result"]
    assert "用户中断了发送" in json.dumps(result, ensure_ascii=False)


def test_shutting_down_does_not_wait_for_the_backoff(bot, monkeypatch):
    s = bot(reply(text("退出前的回答")))
    monkeypatch.setattr(config, "CHANNEL_SEND_RETRY_SECONDS", 30.0)
    s.tg.trouble("sendMessage", *["reset"] * 50)
    s.say("你好")
    until(lambda: s.tg.calls.count("sendMessage") >= 1, what="第一次发送")
    until(lambda: not s.session().running, what="这一轮结束")
    began = time.monotonic()
    s.close()
    assert time.monotonic() - began < 10  # 不等那 30 秒的重试


# ---- Telegram 断开以后马上显示"在线" ------------------------------------------------------


def test_telegram_says_online_right_after_connecting_and_after_a_reset():
    """以前刚连上、连接被重置以后的那一次 getUpdates 也是 25 秒的长轮询：没人说话时要等满 25 秒才显示"在线"
    （真实日志里每次都是 "connecting" 过 26 秒才 "online"），这期间留着的回答也一直没补发。"""
    fake = FakeTelegram(max_poll=30).start()  # 像真的 Telegram 一样，长轮询真的等满
    channel = TelegramChannel(TOKEN, fake.base_url, poll_seconds=25)
    states: list[str] = []
    channel.on_status = lambda: states.append(channel.status.state)
    try:
        channel.start(lambda event: None)
        until(lambda: channel.status.state == "online", timeout=10, what="马上显示在线")
        assert fake.poll_timeouts[0] == 0  # 第一次不让 Telegram 等
        until(lambda: len(fake.poll_timeouts) >= 2, what="接着是长轮询")
        assert fake.poll_timeouts[1] == 25

        fake.trouble("getUpdates", "reset")
        fake.user_says("你好")  # 让正在等着的那次长轮询回来，下一次就被重置
        until(lambda: states.count("connecting") == 2, timeout=10, what="连接被重置")
        until(lambda: states[-1] == "online", timeout=10, what="重新连上马上显示在线")
        # 第 1 次（0）、第 2 次（25，带回了"你好"）、被重置的那一次（假 Telegram 没记）、重新连上的那一次也不等
        until(lambda: len(fake.poll_timeouts) >= 3, what="重新连上的那一次")
        assert fake.poll_timeouts[:3] == [0, 25, 0]
    finally:
        channel.stop()
        stop_later(fake.stop)
