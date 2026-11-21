"""聊天通道在网页这一侧：/api/channel/* 这几个接口、状态里的 channels、在文件管理器里显示设置文件，
以及同一段会话在网页和聊天里不能同时写（"认领"，见 harness/claims.py）。

和 test_channels.py 用的是同一套组装（Rig）：真的网页服务器 + 本地的假 Claude API + 本地的假 Telegram，
测试代码扮演浏览器（s.browser）和手机（s.say、s.press）。不连任何真的服务。
"""

from __future__ import annotations

import json
import time

import pytest
from fake_api import reply, text
from fake_telegram import TOKEN
from test_channels import BOT, OWNER, OWNER_NAME, rigs, run, until, visible_end

import harness.webui.server as server_module
from harness.channels.platforms import PLATFORMS
from harness.channels.store import ChannelStore
from harness.config import PAIRING_ATTEMPTS, PAIRING_SECONDS

# 真正的"在文件管理器里显示"。bot 这个 fixture 会把它们换成假的；测"没有文件管理器的服务器"时要用回真的
REAL_REVEAL = server_module.reveal_in_file_manager
REAL_HAS_FILE_MANAGER = server_module.has_file_manager
OK = (200, {"ok": True})
OPENED = (200, {"ok": True, "warning": ""})  # 会话的几个操作（打开、改名、删除）成功时的回答


@pytest.fixture
def bot(make_config, monkeypatch):
    yield from rigs(make_config, monkeypatch)


def post(s, action: str, **body) -> tuple[int, dict]:
    return s.browser.post(f"/api/channel/{action}", body)


def wait_event(s, kind: str, after: int) -> dict:
    """等一个编号在 after 之后、类型是 kind 的事件。（打开会话时会重放旧的事件，所以不能只看类型。）"""
    return until(lambda: next((event for event in s.browser.events if event["id"] > after and event["type"] == kind), None), what=f"{kind} 事件")


def last_event_id(s) -> int:
    return s.ui.events_after(0, timeout=0)[-1]["id"]


# ---- 连接、配对、确认方式、开关 -------------------------------------------------------


def test_connecting_a_bot_from_the_page(bot):
    s = bot(connected=False)
    channels = s.state()["channels"]
    assert channels["problem"] == "" and channels["file"] == str(s.config.channels_file)
    assert [item["id"] for item in channels["platforms"]] == [item["id"] for item in PLATFORMS]  # 页面上的卡片照着这张表画
    ready = {item["id"] for item in channels["platforms"] if item["stage"] == "ready"}
    assert ready == {"telegram", "discord", "slack", "feishu", "dingtalk", "qq"}
    assert {item["stage"] for item in channels["platforms"] if item["id"] not in ready} == {"public_url", "unsupported"}
    platform = s.platform()
    assert (platform["configured"], platform["status"]["state"], platform["chats"], platform["token_hint"]) == (False, "off", [], "")

    # 样子不对：不去问 Telegram 就拒绝；Telegram 不认的：说清楚
    status, body = post(s, "connect", platform="telegram", token="nonsense")
    assert status == 409 and "不像是一个 Telegram 机器人的 token" in body["error"]
    assert s.tg.calls == []
    rejected = "987654321:" + "Z" * 35
    status, body = post(s, "connect", platform="telegram", token=rejected)
    assert status == 409 and "不认这个 token" in body["error"] and rejected not in body["error"]
    assert not s.config.channels_file.exists()  # 不对的 token 不保存

    assert post(s, "connect", platform="telegram", token=f"  {TOKEN}\n") == (200, {"ok": True, "bot": {**BOT, "handle": "@zhifox_test_bot"}})
    until(lambda: s.platform()["status"]["state"] == "online", what="连上")
    platform = s.platform()
    assert platform["configured"] is True and platform["enabled"] is True and platform["approval"] == "ask"
    assert platform["token_hint"] == "…" + TOKEN[-4:] and platform["bot"] == {"username": "zhifox_test_bot", "name": "ZhiFox Test", "handle": "@zhifox_test_bot", "link": "", "install": ""}
    assert platform["owner"] is None and platform["pairing"] is None
    assert ChannelStore(s.config.channels_file).get("telegram")["credentials"].get("token", "") == TOKEN  # 存的是去掉空白的

    # token 只有最后 4 位出现在页面上：发给浏览器的每一个事件里都找不到它
    everything = json.dumps(s.browser.events, ensure_ascii=False)
    assert TOKEN not in everything and TOKEN.split(":", 1)[1] not in everything

    # 还不能用的平台
    calls = len(s.tg.calls)
    status, body = post(s, "connect", platform="whatsapp", token=TOKEN)
    assert status == 409 and "还不能用" in body["error"] and len(s.tg.calls) == calls


def test_pairing_from_the_page_and_cancelling(bot):
    s = bot(paired=False)
    status, body = post(s, "pair", platform="telegram")
    assert status == 200 and set(body) == {"ok", "code", "expires"}
    code = body["code"]
    assert len(code) == 6 and code.isdigit() and abs(body["expires"] - (time.time() + PAIRING_SECONDS)) < 5
    until(lambda: s.platform()["pairing"] == {"code": code, "expires": body["expires"], "attempts_left": PAIRING_ATTEMPTS,
                                              "link": f"https://t.me/zhifox_test_bot?start={code}"}, what="页面上的配对码")

    # 再点一次"配对"：换一个新的，旧的作废
    status, again = post(s, "pair", platform="telegram")
    assert status == 200 and s.hub.store.pairing("telegram")["secret"] == again["code"]

    assert post(s, "cancel", platform="telegram") == OK
    until(lambda: s.platform()["pairing"] is None, what="配对码消失")
    s.tg.user_says(again["code"])  # 取消以后再发配对码：不算数，也不回
    until(lambda: s.hub.store.get("telegram")["ignored"] == 1, what="忽略了这一条")
    s.hub.router.flush()
    assert s.tg.sent == [] and s.hub.store.get("telegram")["owner"] is None


def test_choosing_the_approval_mode_from_the_page(bot):
    s = bot()
    assert post(s, "approval", platform="telegram", mode="yolo")[0] == 400
    assert post(s, "approval", platform="telegram")[0] == 400
    assert post(s, "approval", platform="telegram", mode="edits") == OK
    until(lambda: s.platform()["approval"] == "edits", what="页面上的确认方式")
    assert ChannelStore(s.config.channels_file).get("telegram")["approval"] == "edits"
    assert s.state()["approval"] == "ask"  # 网页对话的那一档不跟着变


def test_turning_a_channel_off_stops_polling(bot):
    s = bot()
    channel = s.channel()
    assert post(s, "enable", platform="telegram", enabled="no")[0] == 400
    assert post(s, "enable", platform="telegram", enabled=False) == OK
    until(lambda: s.platform()["enabled"] is False and s.platform()["status"]["state"] == "off", what="页面上显示已关闭")
    until(lambda: not channel._thread.is_alive(), what="不再轮询")
    polls = len(s.tg.offsets)
    start = s.say("/help")
    time.sleep(0.5)  # 看看这段时间里有没有人去问 Telegram（开着的话 0.2 秒就会问一次）
    assert len(s.tg.offsets) == polls and len(s.tg.sent) == start  # 关掉了：消息留在 Telegram 那边，没人收

    assert post(s, "enable", platform="telegram", enabled=True) == OK
    s.wait_reply(start, "/status")  # 打开以后接着收：关着的时候发来的那一条也处理了
    until(lambda: s.platform()["status"]["state"] == "online", what="重新连上")


def test_unbinding_and_removing(bot):
    s = bot()
    s.sentinel()
    assert post(s, "unbind", platform="telegram") == OK
    until(lambda: s.platform()["owner"] is None, what="页面上的主人消失")
    before = len(s.tg.sent)
    s.tg.user_says("/help")  # 解除绑定以后，原来的主人也和陌生人一样，一个字都不回
    until(lambda: s.hub.store.get("telegram")["ignored"] == 1, what="忽略了这一条")
    s.hub.router.flush()
    assert len(s.tg.sent) == before

    assert post(s, "remove", platform="telegram") == OK
    until(lambda: s.platform()["configured"] is False and s.platform()["status"]["state"] == "off", what="页面上显示没设置")
    assert "telegram" not in json.loads(s.config.channels_file.read_text(encoding="utf-8"))["platforms"]
    assert s.hub.channel("telegram") is None and s.hub.router.sessions("telegram") == []
    for action, extra in (("pair", {}), ("approval", {"mode": "all"}), ("enable", {"enabled": True}), ("unbind", {})):
        status, body = post(s, action, platform="telegram", **extra)
        assert status == 409 and "还没有连接 Telegram" in body["error"], action


def test_bad_channel_requests_are_rejected(bot):
    s = bot()
    bad = [
        ("connect", {"platform": "telegram"}),
        ("connect", {"platform": "telegram", "token": ""}),
        ("connect", {"platform": "telegram", "token": "   "}),
        ("connect", {"platform": "telegram", "token": "x" * 401}),
        ("connect", {"platform": "telegram", "token": 5}),
        ("pair", {}),
        ("pair", {"platform": ""}),
        ("pair", {"platform": 5}),
        ("enable", {"platform": "telegram", "enabled": 1}),
        ("confirm", {"platform": "telegram", "chat": OWNER, "confirm_id": "1", "allow": True}),
        ("confirm", {"platform": "telegram", "chat": 111, "confirm_id": 1, "allow": True}),
        ("confirm", {"platform": "telegram", "chat": OWNER, "confirm_id": True, "allow": True}),
        ("confirm", {"platform": "telegram", "chat": OWNER, "confirm_id": 1, "allow": "yes"}),
        ("confirm", {"platform": "telegram", "chat": OWNER, "confirm_id": 1, "allow": True, "feedback": 3}),
        ("stop", {"platform": "telegram"}),
    ]
    for action, body in bad:
        assert s.browser.post(f"/api/channel/{action}", body) == (400, {"error": "参数不正确"}), (action, body)
    for action in ("explode", "", "connect/telegram"):
        assert s.browser.post(f"/api/channel/{action}", {"platform": "telegram"})[0] == 404, action

    # 参数对，但现在做不了：409，说清楚原因
    status, body = post(s, "pair", platform="whatsapp")
    assert status == 409 and "还不能用" in body["error"]
    status, body = post(s, "pair", platform="discord")
    assert status == 409 and "还没有连接 Discord" in body["error"]
    status, body = post(s, "confirm", platform="telegram", chat=OWNER, confirm_id=1, allow=True)
    assert status == 409 and "已经处理过了" in body["error"]
    status, body = post(s, "stop", platform="telegram", chat=OWNER)
    assert status == 409 and "没有在进行的事" in body["error"]
    # 和别的接口一样：没带口令的请求一律不收
    from test_webui import request

    assert request(s.browser.host, "POST", "/api/channel/remove", {"platform": "telegram"})[0] == 403
    assert s.hub.store.get("telegram")["credentials"].get("token", "") == TOKEN


def test_a_broken_settings_file_is_reported_and_left_alone(bot, make_config):
    config = make_config()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.channels_file.write_text("{这不是 JSON", encoding="utf-8")
    s = bot(connected=False)
    problem = s.state()["channels"]["problem"]
    assert problem and str(config.channels_file) in problem
    status, body = post(s, "connect", platform="telegram", token=TOKEN)
    assert status == 409 and body["error"] == problem
    assert config.channels_file.read_text(encoding="utf-8") == "{这不是 JSON"
    assert s.hub.channel("telegram") is None


def test_stopping_a_chat_from_the_page(bot):
    s = bot(run("toolu_1", "echo hi"))
    start = s.say("跑")
    ask = s.asked(start)
    # 网页对话的"停止"碰不到聊天里的这一轮
    assert s.browser.post("/api/stop", {}) == OK
    assert s.session().waiting is not None

    assert post(s, "stop", platform="telegram", chat=OWNER) == OK
    s.wait_reply(start, "已在网页上停下这一轮")
    s.wait_reply(start, "已中断。")
    assert visible_end(s.edit_of(ask)) == "已中断"
    s.idle()
    status, body = post(s, "stop", platform="telegram", chat=OWNER)
    assert status == 409 and "没有在进行的事" in body["error"]


# ---- 在文件管理器里显示 ---------------------------------------------------------------


def test_revealing_the_channels_file(bot, monkeypatch):
    s = bot()
    shown = []
    monkeypatch.setattr(server_module, "reveal_in_file_manager", shown.append)
    path = str(s.config.channels_file)
    assert s.browser.post("/api/reveal", {"what": "channels"}) == (200, {"ok": True, "path": path, "opened": True})
    assert shown == [s.config.channels_file]
    assert s.browser.post("/api/reveal", {"what": "channels.json"})[0] == 400  # 只认那几个名字，路径由服务器算


def test_revealing_on_a_server_without_a_file_manager(bot, monkeypatch):
    """服务器上没有图形界面：不去启动任何程序，回答里说"没打开"，页面改成告诉你文件在哪儿。"""
    s = bot()
    launched = []

    def no_programs(*args, **options):
        launched.append(args)
        raise AssertionError("没有文件管理器的电脑上不该启动任何程序")

    monkeypatch.setattr(server_module, "reveal_in_file_manager", REAL_REVEAL)
    monkeypatch.setattr(server_module, "has_file_manager", REAL_HAS_FILE_MANAGER)
    monkeypatch.setattr(server_module.sys, "platform", "linux")
    monkeypatch.setattr(server_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(server_module.subprocess, "Popen", no_programs)

    path = str(s.config.channels_file)
    assert s.browser.post("/api/reveal", {"what": "channels"}) == (200, {"ok": True, "path": path, "opened": False})
    # 还不存在的文件：说它所在的文件夹
    assert s.browser.post("/api/reveal", {"what": "tasks"}) == (200, {"ok": True, "path": str(s.config.data_dir), "opened": False})
    assert launched == []


# ---- 同一段会话：网页和聊天不能同时写 --------------------------------------------------------


def test_the_page_cannot_open_rename_or_delete_a_conversation_the_chat_is_using(bot):
    s = bot(reply(text("第一轮的回答")), run("toolu_1", "echo hi"), reply(text("第二轮完")))
    s.wait_reply(s.say("第一轮"), "第一轮的回答")
    s.idle()
    conversation_id = s.session().conversation.id
    start = s.say("第二轮")
    ask = s.asked(start)

    for path, body in (
        ("/api/conversation/open", {"id": conversation_id}),
        ("/api/conversation/rename", {"id": conversation_id, "title": "新名字"}),
        ("/api/conversation/delete", {"id": conversation_id}),
    ):
        status, answer = s.browser.post(path, body)
        assert status == 409 and "这段会话正在 Telegram 上进行" in answer["error"], path
    assert s.app.store.load(conversation_id).title == "第一轮"
    assert s.hub.holder(conversation_id) == "Telegram"

    s.press(ask)
    s.wait_reply(start, "第二轮完")
    s.idle()
    assert s.hub.holder(conversation_id) == ""  # 这一轮结束、存好了盘就放开
    assert s.browser.post("/api/conversation/open", {"id": conversation_id}) == OPENED


def test_the_page_follows_a_chat_turn_and_cannot_write_over_it(bot):
    s = bot(reply(text("聊天第一轮的回答")), run("toolu_1", "echo hi"), reply(text("聊天第二轮完")), reply(text("网页接着聊的回答")))
    s.wait_reply(s.say("第一轮"), "聊天第一轮的回答")
    s.idle()
    conversation_id = s.session().conversation.id
    assert s.browser.post("/api/conversation/open", {"id": conversation_id}) == OPENED

    start = s.say("第二轮")
    ask = s.asked(start)
    # 聊天那边正在这段会话里跑一轮：网页上发消息不等，直接说明
    status, body = s.browser.post("/api/message", {"text": "网页上插一句"})
    assert status == 409 and "这段会话正在 Telegram 上进行" in body["error"]

    mark = last_event_id(s)
    s.press(ask)
    s.wait_reply(start, "聊天第二轮完")
    s.idle()
    # 网页正开着这段会话：聊天那一轮存完盘，页面跟着重画，看得到聊天里刚说的
    wait_event(s, "reset", mark)
    until(lambda: any(event["id"] > mark and event["type"] == "user" and event.get("text") == "第二轮" for event in s.browser.events), what="页面重画")

    # 接着在网页上聊：带着聊天里那两轮的历史，不会把它们盖掉
    mark = last_event_id(s)
    assert s.browser.post("/api/message", {"text": "网页接着聊"})[0] == 200
    wait_event(s, "turn_end", mark)
    history = json.dumps(s.api.requests[-1]["messages"], ensure_ascii=False)
    assert "第二轮" in history and "聊天第二轮完" in history
    saved = s.app.store.load(conversation_id)
    assert saved.channel == "telegram"  # 网页上接着聊过，它还是 Telegram 上的那段
    assert [event["text"] for event in saved.events if event["type"] == "user"] == ["第一轮", "第二轮", "网页接着聊"]


def test_a_chat_turn_waits_for_a_web_turn_on_the_same_conversation(bot):
    s = bot(reply(text("聊天一的回答")), run("toolu_w", "echo web"), reply(text("网页的回答")), reply(text("聊天二的回答")))
    s.wait_reply(s.say("聊天一"), "聊天一的回答")
    s.idle()
    conversation_id = s.session().conversation.id
    assert s.browser.post("/api/conversation/open", {"id": conversation_id}) == OPENED
    mark = last_event_id(s)
    assert s.browser.post("/api/message", {"text": "网页跑命令"})[0] == 200
    confirm = wait_event(s, "confirm", mark)

    # 网页正在这段会话里跑一轮（停在确认上）：聊天里的消息收下了，等它跑完，不抢着去问模型
    start = s.say("聊天二")
    until(lambda: s.session().running, what="聊天收下了这条")
    time.sleep(0.3)
    assert len(s.api.requests) == 2

    assert s.browser.post("/api/confirm", {"id": confirm["id"], "allow": True}) == OK
    s.wait_reply(start, "聊天二的回答")
    s.idle()
    # 聊天那边先从磁盘读了最新的：网页上那一轮也在它的历史里
    history = json.dumps(s.api.requests[3]["messages"], ensure_ascii=False)
    assert "网页跑命令" in history and "网页的回答" in history
    saved = s.app.store.load(conversation_id)
    assert [event["text"] for event in saved.events if event["type"] == "user"] == ["聊天一", "网页跑命令", "聊天二"]


def test_a_conversation_deleted_on_the_page_is_replaced_in_the_chat(bot):
    s = bot(reply(text("最早那段的回答")), reply(text("新一段的回答")))
    s.wait_reply(s.say("最早那段"), "最早那段的回答")
    s.idle()
    first = s.session().conversation.id
    assert s.browser.post("/api/conversation/delete", {"id": first}) == OPENED

    start = s.say("还在吗")
    s.wait_reply(start, "之前那段会话已经在网页上删掉了")
    s.wait_reply(start, "新一段的回答")
    s.idle()
    second = s.session().conversation.id
    assert second != first and len(s.api.requests[1]["messages"]) == 1
    assert [item["id"] for item in s.app.store.summaries()] == [second]
    assert s.hub.store.conversation_of("telegram", OWNER) == second


def test_the_page_lists_chats_and_their_owner(bot):
    s = bot(run("toolu_1", "echo hi"), reply(text("好了")))
    start = s.say("跑")
    s.asked(start)
    chat = until(lambda: s.platform()["chats"] and s.platform()["chats"][0]["state"] == "waiting" and s.platform()["chats"][0], what="页面上的聊天")
    assert (chat["chat"], chat["name"], chat["title"], chat["conversation"]) == (OWNER, OWNER_NAME, "跑", "")  # 第一轮还没存盘
    assert chat["confirm"]["summary"].startswith("执行命令")
    assert s.platform()["owner"]["name"] == OWNER_NAME
    s.press(s.tg.wait_sent(lambda message: bool(message.get("reply_markup")), after=start))
    s.wait_reply(start, "好了")
    s.idle()
    chat = until(lambda: s.platform()["chats"][0]["state"] == "idle" and s.platform()["chats"][0], what="聊天空闲")
    assert chat["conversation"] == s.session().conversation.id and chat["confirm"] is None
    assert s.platform()["last_active"]


def test_a_failed_turn_does_not_keep_the_conversation_held(bot, monkeypatch):
    s = bot(reply(text("第一轮的回答")))
    s.wait_reply(s.say("第一轮"), "第一轮的回答")
    s.idle()
    conversation_id = s.session().conversation.id
    s.close()

    again = bot()  # 重启：这个聊天接着那段会话，助手要重新建

    def broken(*args, **options):
        raise RuntimeError("建助手时出错了")

    monkeypatch.setattr(again.app, "channel_agent", broken)
    again.wait_reply(again.say("第二轮"), "这一轮出错了")
    again.idle()
    assert again.hub.holder(conversation_id) == ""
    assert again.browser.post("/api/conversation/open", {"id": conversation_id}) == OPENED
