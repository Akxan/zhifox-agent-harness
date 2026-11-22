"""聊天通道（Telegram）的测试：设置文件、配对、排版、Bot API 客户端，以及一整条"手机 → 智狐 → 手机"的路。

前面几节是不联网的单元测试（store、pairing、markup），和对着本地假 Telegram（tests/fake_telegram.py）的
客户端测试。后面几节把整个程序组装起来：真的网页服务器（WebApp + ChannelHub）、本地的假 Claude API、
本地的假 Telegram，测试代码扮演手机上的你（fake.user_says、fake.press）和浏览器（Browser）。
走的都是真实的代码路径：长轮询、发送队列、agent 循环、确认、存盘。不连任何真的服务，不花钱。

网页这一侧的接口（/api/channel/*、会话的认领）在 test_channels_web.py；无界面模式和命令行在 test_serve.py。
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import random
import stat
import threading
import time

import anthropic
import pytest
from fake_api import FakeAPI, reply, text, tool_use
from fake_telegram import TOKEN, FakeTelegram, utf16, visible
from sample_files import JPEG_BYTES
from test_webui import Browser

import harness.webui.server as server_module
from harness import log
from harness.app import build_app
from harness.channels import markup, pairing
from harness.channels.base import ChannelError, Inbound, Press, Prompt
from harness.channels.router import ALLOW_WORDS, DENY_WORDS, pairing_reply
from harness.channels.store import ChannelStore
from harness.channels.telegram import (
    BotAPI,
    TelegramChannel,
    TelegramError,
    check_token,
    valid_token,
)
from harness.config import CHANNEL_QUEUE, PAIRING_ATTEMPTS, PAIRING_SECONDS
from harness.webui.server import create_server
from harness.webui.ui import WebUI

BOT = {"id": "123456789", "username": "zhifox_test_bot", "name": "ZhiFox Test"}  # 和假 Telegram 的 getMe 回答一样
OWNER = "111"  # fake.user_says 默认的发消息的人
OWNER_NAME = "Akxan (@akxan)"


def until(condition, timeout: float = 10, what: str = "条件"):
    """等到 condition() 返回真值，并返回它。不用固定的 sleep：条件一成立就往下走，测试快，也不会因为机器慢而偶尔失败。"""
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value:
            return value
        assert time.monotonic() < deadline, f"等不到{what}"
        time.sleep(0.01)


class Clock:
    """假的钟：测试想让现在是几点就是几点，配对码过期不用真的等十分钟。"""

    def __init__(self, now: float = 1_800_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def paired_store(path, clock=None) -> ChannelStore:
    """一个已经连上机器人、并且配对好了主人的设置文件。"""
    store = ChannelStore(path, clock=clock) if clock else ChannelStore(path)
    store.connect("telegram", {"token": TOKEN}, BOT)
    code = store.start_pairing("telegram")
    assert store.attempt("telegram", OWNER, OWNER_NAME, code["code"]) == "paired"
    return store


# ---- 设置文件 ----------------------------------------------------------------


def test_the_settings_file_is_private_and_written_atomically(tmp_path, monkeypatch):
    path = tmp_path / "data" / "channels.json"
    store = ChannelStore(path)
    assert store.problem == "" and not path.exists()  # 还没设置过：不建文件，也不算出错
    store.connect("telegram", {"token": TOKEN}, BOT)

    # token 是机器人的钥匙：文件只有自己的账号能读，所在的文件夹也只有自己能进
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["platforms"]["telegram"]["credentials"] == {"token": TOKEN} and saved["version"] == 1
    # 改设置时用来"别的程序先别动"的锁文件就在旁边；写到一半的临时文件一个都不留
    assert sorted(item.name for item in path.parent.iterdir()) == ["channels.json", "channels.json.lock"]

    # 写的时候出了错（比如磁盘满了）：原来的文件原封不动，内存里的也不变，不留下半截的临时文件
    before = path.read_bytes()

    def broken(*args):
        raise OSError("磁盘满了")

    monkeypatch.setattr("harness.safe.os.replace", broken)
    # 换成 ChannelError（带着原因）：页面上回答 409、说清楚为什么，而不是一个 500
    with pytest.raises(ChannelError, match="磁盘满了"):
        store.set_approval("telegram", "all")
    assert path.read_bytes() == before
    assert store.get("telegram")["approval"] == "ask"
    assert not list(path.parent.glob("*.tmp"))


def test_a_broken_settings_file_is_never_overwritten(tmp_path):
    path = tmp_path / "channels.json"
    path.write_text('{"platforms": {"telegram": {"token": "' + TOKEN + '"', encoding="utf-8")  # 只写了一半的 JSON
    before = path.read_bytes()
    store = ChannelStore(path)

    # 说清楚是哪个文件、为什么改不了，让你自己去修
    assert store.problem and str(path) in store.problem
    assert TOKEN not in store.problem
    changes = [
        lambda: store.connect("telegram", {"token": TOKEN}, BOT),
        lambda: store.set_enabled("telegram", False),
        lambda: store.set_approval("telegram", "edits"),
        lambda: store.remove("telegram"),
        lambda: store.unbind("telegram"),
        lambda: store.start_pairing("telegram"),
        lambda: store.cancel_pairing("telegram"),
        lambda: store.attempt("telegram", OWNER, OWNER_NAME, "123456"),
        lambda: store.remember_chat("telegram", OWNER, "20261121-0800-abc"),
        lambda: store.set_offset("telegram", 42),
    ]
    for change in changes:
        with pytest.raises(ChannelError):
            change()
    # 点多少下按钮，原来的文件都还是原来的样子（不会被一份空的设置盖掉）
    assert path.read_bytes() == before
    # 陌生人发来的消息：什么都不做，也不回话
    stranger = Inbound("telegram", "999", "999", "Stranger", text="123456")
    assert pairing_reply(store, stranger) == ("ignore", "")

    # 修好以后，refresh 就能读到，又可以改了
    path.write_text(json.dumps({"platforms": {"telegram": {"token": TOKEN, "approval": "edits"}}}), encoding="utf-8")
    assert store.refresh() is True
    assert store.problem == "" and store.get("telegram")["approval"] == "edits"
    store.set_enabled("telegram", False)
    assert store.get("telegram")["enabled"] is False


def test_hand_edited_values_of_the_wrong_type_are_dropped(tmp_path):
    path = tmp_path / "channels.json"
    path.write_text(
        json.dumps(
            {
                "platforms": {
                    "telegram": {
                        "token": TOKEN,
                        "approval": "yolo",  # 没有这一档：当成每次都问
                        "enabled": "no",  # 不是 false：照样当成开着
                        "owner": {"id": 111},  # 编号不是文字：不算有主人
                        "pairing": {"secret": "12345", "expires": 1e300},  # 不是 6 位数字：不算在配对
                        "offset": float("nan"),
                        "ignored": True,
                        "chats": {"111": "not-a-dict"},
                    },
                    "broken": "x",
                }
            }
        ),
        encoding="utf-8",
    )
    store = ChannelStore(path)
    entry = store.get("telegram")
    assert store.problem == ""
    assert (entry["approval"], entry["enabled"], entry["owner"], entry["pairing"]) == ("ask", True, None, None)
    assert (entry["offset"], entry["ignored"], entry["chats"]) == (0, 0, {})
    assert store.known() == ["telegram"]


def test_a_change_made_by_another_program_is_picked_up(tmp_path):
    """服务开着，你在命令行里生成配对码、换确认方式：两个 ChannelStore 对着同一个文件。"""
    path = tmp_path / "channels.json"
    service, cli = ChannelStore(path), ChannelStore(path)
    noticed = []
    service.on_change(lambda: noticed.append(True))

    cli.connect("telegram", {"token": TOKEN}, BOT)
    assert service.get("telegram")["credentials"].get("token", "") == ""  # 还没看
    assert service.refresh() is True and noticed == [True]
    assert service.get("telegram")["credentials"].get("token", "") == TOKEN
    assert service.refresh() is False and noticed == [True]  # 没变：不重读、不通知

    code = cli.start_pairing("telegram")
    service.refresh()
    assert service.pairing("telegram")["secret"] == code["code"]  # 命令行里生成的配对码，服务马上认得

    # 两边各改一处，谁也不会把对方的改动盖掉：每次改之前都先从磁盘读最新的
    service.set_approval("telegram", "edits")
    cli.set_enabled("telegram", False)
    fresh = ChannelStore(path).get("telegram")
    assert (fresh["approval"], fresh["enabled"], fresh["pairing"]["secret"]) == ("edits", False, code["code"])


def test_connecting_another_bot_forgets_the_owner(tmp_path):
    store = paired_store(tmp_path / "channels.json")
    store.remember_chat("telegram", OWNER, "20261121-0800-abc")
    store.set_offset("telegram", 77)

    # 同一个机器人换了个 token（在 @BotFather 那里重新生成过）：主人和聊天都还在
    store.connect("telegram", {"token": "123456789:" + "B" * 35}, BOT)
    entry = store.get("telegram")
    assert entry["owner"]["id"] == OWNER and entry["chats"] and entry["offset"] == 77

    # 换成了另一个机器人：原来的主人、聊天、读到的位置都作废，要重新配对
    store.connect("telegram", {"token": "987654321:" + "C" * 35}, {"id": "987654321", "username": "other_bot", "name": "Other"})
    entry = store.get("telegram")
    assert (entry["owner"], entry["chats"], entry["offset"], entry["pairing"]) == (None, {}, 0, None)
    assert store.summary("telegram")["bot"] == {"username": "other_bot", "name": "Other", "handle": "@other_bot", "link": "", "install": ""}


def test_the_summary_never_contains_the_token(tmp_path):
    store = paired_store(tmp_path / "channels.json")
    code = store.start_pairing("telegram")
    summary = store.summary("telegram")
    assert TOKEN not in json.dumps(summary)
    assert summary["token_hint"] == "…" + TOKEN[-4:]
    assert summary["owner"]["name"] == OWNER_NAME and "id" not in summary["owner"]  # 主人的账号编号也不给页面
    # 配对码要让你抄，所以照给
    assert summary["pairing"] == {"code": code["code"], "expires": code["expires"], "attempts_left": PAIRING_ATTEMPTS,
                                  "link": f"https://t.me/zhifox_test_bot?start={code['code']}"}  # 点开就替你把配对码发过去
    assert ChannelStore(tmp_path / "nothing.json").summary("telegram")["configured"] is False


# ---- 配对 ------------------------------------------------------------------


def test_pairing_codes_look_right():
    codes = {pairing.new_code() for _ in range(200)}
    assert all(pairing.valid(code) for code in codes) and len(codes) > 150  # 6 位数字，每次都不一样
    # 认得出这几种写法：照着页面抄的（中间带空格）、点了"在 Telegram 里打开"时 Telegram 替你发的
    for given in ("123456", " 123456 ", "123 456", "/start 123456", "/start  123 456"):
        assert pairing.find_code(given) == "123456", given
    for other in ("12345", "1234567", "abc123456", "123456 好的", "/start", "/new 123456", "123  456"):
        assert pairing.find_code(other) is None, other
    assert not pairing.valid("12345a") and not pairing.valid(123456) and not pairing.valid("١٢٣٤٥٦")


@pytest.mark.parametrize("written", ["{code}", "{a} {b}", "/start {code}"])
def test_the_right_code_makes_you_the_owner(tmp_path, written):
    clock = Clock()
    store = ChannelStore(tmp_path / "channels.json", clock=clock)
    store.connect("telegram", {"token": TOKEN}, BOT)
    store.remember_chat("telegram", "555", "old-conversation")  # 以前的主人的聊天
    code = store.start_pairing("telegram")["code"]
    assert store.pairing("telegram")["expires"] == clock.now + PAIRING_SECONDS
    message = written.format(code=code, a=code[:3], b=code[3:])
    clock.now += PAIRING_SECONDS - 1  # 快过期了，还算数

    assert store.attempt("telegram", OWNER, OWNER_NAME, message) == "paired"
    entry = store.get("telegram")
    assert entry["owner"] == {"id": OWNER, "name": OWNER_NAME, "paired_at": clock.now}
    assert entry["pairing"] is None and entry["chats"] == {}  # 配对码随即作废；以前的聊天不再算数
    assert store.is_owner("telegram", OWNER) and not store.is_owner("telegram", "999")
    # 配对码只能用一次
    assert store.attempt("telegram", "999", "Someone", message) == "ignore"


def test_the_right_code_in_full_width_digits_is_accepted(tmp_path):
    store = ChannelStore(tmp_path / "channels.json")
    store.connect("telegram", {"token": TOKEN}, BOT)
    code = store.start_pairing("telegram")["code"]
    full_width = code.translate({ord(digit): 0xFF10 + int(digit) for digit in "0123456789"})
    assert store.attempt("telegram", OWNER, OWNER_NAME, full_width) == "paired"


def test_wrong_codes_are_counted_and_too_many_lock_the_code(tmp_path):
    store = ChannelStore(tmp_path / "channels.json")
    store.connect("telegram", {"token": TOKEN}, BOT)
    code = store.start_pairing("telegram")["code"]
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"

    for attempt in range(1, PAIRING_ATTEMPTS):
        assert store.attempt("telegram", "999", "Guesser", wrong) == "wrong"
        assert store.summary("telegram")["pairing"]["attempts_left"] == PAIRING_ATTEMPTS - attempt
    # 第 PAIRING_ATTEMPTS 次猜错：配对码作废，猜对了也没用了
    assert store.attempt("telegram", "999", "Guesser", wrong) == "locked"
    assert store.pairing("telegram") is None
    assert store.attempt("telegram", OWNER, OWNER_NAME, code) == "ignore"
    assert store.get("telegram")["owner"] is None


def test_an_expired_code_is_not_accepted(tmp_path):
    clock = Clock()
    store = ChannelStore(tmp_path / "channels.json", clock=clock)
    store.connect("telegram", {"token": TOKEN}, BOT)
    code = store.start_pairing("telegram")["code"]
    clock.now += PAIRING_SECONDS
    assert store.pairing("telegram") is None and store.summary("telegram")["pairing"] is None
    assert store.attempt("telegram", OWNER, OWNER_NAME, code) == "ignore"
    assert store.get("telegram")["owner"] is None and store.get("telegram")["pairing"] is None
    assert store.get("telegram")["ignored"] == 1


def test_during_pairing_other_messages_get_one_hint_per_person(tmp_path):
    store = ChannelStore(tmp_path / "channels.json")
    store.connect("telegram", {"token": TOKEN}, BOT)

    # 没在配对：陌生人的消息一律不理，只是记个数
    assert store.attempt("telegram", "999", "Stranger", "你好") == "ignore"
    assert store.attempt("telegram", "999", "Stranger", "123456") == "ignore"
    assert store.get("telegram")["ignored"] == 2

    store.start_pairing("telegram")
    assert store.attempt("telegram", "999", "Stranger", "你好") == "hint"  # 提醒一次"请发配对码"
    assert store.attempt("telegram", "999", "Stranger", "在吗") == "ignore"  # 之后不再回
    assert store.attempt("telegram", "888", "Another", "hi") == "hint"  # 另一个人也提醒一次
    assert store.get("telegram")["ignored"] == 3
    assert store.pairing("telegram")["attempts"] == 0  # 不像配对码的话不算猜错


def test_pairing_replies_are_the_same_for_the_chat_and_the_command_line(tmp_path):
    store = ChannelStore(tmp_path / "channels.json")
    store.connect("telegram", {"token": TOKEN}, BOT)
    stranger = Inbound("telegram", "999", "999", "Stranger", text="hello")
    assert pairing_reply(store, stranger) == ("ignore", "")  # 不在配对的时候：一个字都不回

    code = store.start_pairing("telegram")["code"]
    outcome, hint = pairing_reply(store, stranger)
    assert outcome == "hint" and "6 位配对码" in hint
    wrong = Inbound("telegram", "999", "999", "Stranger", text="000000" if code != "000000" else "111111")
    outcome, answer = pairing_reply(store, wrong)
    assert outcome == "wrong" and f"还可以再试 {PAIRING_ATTEMPTS - 1} 次" in answer
    group = Inbound("telegram", "-100", OWNER, OWNER_NAME, text=code, private=False)
    assert pairing_reply(store, group) == ("ignore", "")  # 群里发的配对码不算：群里的人都看得到
    outcome, answer = pairing_reply(store, Inbound("telegram", OWNER, OWNER, OWNER_NAME, text=code))
    assert outcome == "paired" and "配对成功" in answer and OWNER_NAME in answer and "/stop" in answer


# ---- 排版 ------------------------------------------------------------------


def fences_balanced(piece: str) -> bool:
    return sum(1 for line in piece.splitlines() if markup.FENCE.match(line)) % 2 == 0


def test_long_text_is_split_by_utf16_length():
    emoji = "😀" * 3000  # 一个表情在 UTF-16 里占两个单位：按 len 数只有 3000，Telegram 数的是 6000
    pieces = markup.split(emoji, 4000)
    assert len(pieces) == 2 and all(markup.length(piece) <= 4000 for piece in pieces)
    assert "".join(pieces) == emoji  # 一个表情都没被切成两半

    prose = "\n\n".join(f"第 {n} 段。" + "内容" * 40 for n in range(200))
    pieces = markup.split(prose, 1000)
    assert all(markup.length(piece) <= 1000 for piece in pieces)
    assert all(piece.endswith("内容") for piece in pieces)  # 都在段落之间断开，不在一句话中间
    assert "\n\n".join(pieces) == prose

    assert markup.split("", 100) == [] and markup.split("\n \n", 100) == []
    assert markup.split("短的", 100) == ["短的"]
    assert markup.length("a😀") == 3


def test_a_split_code_block_is_closed_and_reopened():
    code = "```python\n" + "\n".join(f"print({n})" for n in range(400)) + "\n```\n后面的说明"
    pieces = markup.split(code, 500)
    assert len(pieces) > 5
    for piece in pieces:
        assert markup.length(piece) <= 500
        assert fences_balanced(piece), piece  # 每一段里的代码块都是完整的
    assert all(piece.startswith("```python\n") for piece in pieces[1:-1])  # 后面几段重新打开时带着语言
    assert pieces[-1].endswith("后面的说明")
    body = [line for piece in pieces for line in piece.splitlines() if not markup.FENCE.match(line)]
    assert body == [f"print({n})" for n in range(400)] + ["后面的说明"]  # 一行都没丢、没重复

    # 一行就比上限还长：硬切，切开的每一段仍然在代码块里
    long_line = "```\n" + "x" * 1200 + "\n```"
    pieces = markup.split(long_line, 500)
    assert all(markup.length(piece) <= 500 and fences_balanced(piece) for piece in pieces)
    assert "".join(line for piece in pieces for line in piece.splitlines() if not markup.FENCE.match(line)) == "x" * 1200


def test_html_escapes_the_three_special_characters():
    assert markup.to_html("a < b && c > d") == "a &lt; b &amp;&amp; c &gt; d"
    assert markup.to_html("`<script>` & **<b>**") == "<code>&lt;script&gt;</code> &amp; <b>&lt;b&gt;</b>"
    assert markup.to_html("```html\n<div>&nbsp;</div>\n```") == '<pre><code class="language-html">&lt;div&gt;&amp;nbsp;&lt;/div&gt;</code></pre>'


def test_markdown_becomes_the_telegram_subset():
    html = markup.to_html("# 标题\n- 一项\n  * 二项\n**粗** 和 *斜* 和 `码`\n[文档](https://example.com/a?x=1&y=2)\n---")
    assert html == (
        "<b>标题</b>\n• 一项\n  • 二项\n<b>粗</b> 和 <i>斜</i> 和 <code>码</code>\n"
        '<a href="https://example.com/a?x=1&amp;y=2">文档</a>\n---'
    )
    # 没收尾的代码块照样当代码块；语言名里的怪字符去掉（它会被写进 class 属性）
    assert markup.to_html('```py"><script>\nx = 1') == "<pre>x = 1</pre>"
    assert visible(markup.to_html('```py" onclick="x\ny\n```'), "HTML")[1] == ""


def test_unmatched_markers_stay_as_text():
    for raw in ("2 * 3 = 6", "**没收尾", "半个 ` 反引号", "snake_case_name 和 __init__", "a*b*c", "a * b *",
                "[没有网址]", "[js](javascript:alert(1))", "[本地](file:///etc/passwd)", "****"):
        html = markup.to_html(raw)
        shown, problem = visible(html, "HTML")
        assert problem == "" and shown == raw, raw  # 原样当文字，一个标签都没多出来
        assert "<a" not in html  # 只认 http 和 https 的链接


def test_any_text_becomes_html_that_telegram_accepts():
    """随机拼出几千段"像 Markdown 又不完全是"的文字：排出来的 HTML 都得是 Telegram 认的（标签成对、只用那几种）。"""
    pieces = ["**", "*", "`", "```", "```python", "[a](https://x.y/?q=1&r=<2>)", "[b](javascript:x)", "<", ">", "&",
              "&amp;", "# ", "- ", "\n", " ", "文字", "😀", "_", "x", "**粗**", "*斜*", '"', "'", "<b>", "</i>", "\\"]
    chooser = random.Random(20261121)
    for _ in range(3000):
        raw = "".join(chooser.choice(pieces) for _ in range(chooser.randint(1, 40)))
        html = markup.to_html(raw)
        shown, problem = visible(html, "HTML")
        assert problem == "", (raw, html, problem)
        assert utf16(shown) <= markup.length(raw)  # 排版只会让文字变短：按原文拆过的，排完也不会超长


def test_random_long_replies_split_into_valid_pieces():
    lines = ["普通的一句话。", "```bash", "```", "    缩进的代码", "- 列表 **粗**", "😀" * 30, "x" * 700, "", "# 标题"]
    chooser = random.Random(7)
    for _ in range(200):
        raw = "\n".join(chooser.choice(lines) for _ in range(chooser.randint(1, 120)))
        for piece in markup.split(raw, 600):
            assert markup.length(piece) <= 600 and fences_balanced(piece), piece
            assert visible(markup.to_html(piece), "HTML")[1] == "", piece  # 每一段排出来都是 Telegram 认的 HTML


def test_no_piece_is_only_an_empty_code_block():
    raw = "intro\n```\n" + "x" * 80 + "\n```   "
    for piece in markup.split(raw, 95):
        assert visible(markup.to_html(piece), "HTML")[0].strip(), markup.split(raw, 95)


# ---- Telegram 客户端 ---------------------------------------------------------


@pytest.fixture
def fake():
    telegram = FakeTelegram(max_poll=0.2).start()
    yield telegram
    stop_later(telegram.stop)


def test_check_token_rejects_a_malformed_token_without_asking_telegram(fake):
    for bad in ("", "not-a-token", "123:abc", "123456789:" + "a" * 20, f"{TOKEN} extra", "123456789:" + "é" * 35):
        with pytest.raises(ChannelError, match="不像是一个 Telegram 机器人的 token"):
            check_token(bad, fake.base_url)
    assert fake.calls == []  # 样子都不对，就不用去问 Telegram 了
    assert valid_token(TOKEN) and not valid_token(TOKEN + "\n")


def test_check_token_asks_get_me_and_explains_a_rejected_token(fake):
    assert check_token(f"  {TOKEN}\n", fake.base_url) == BOT  # 粘贴时带上的空白去掉
    assert fake.calls == ["getMe"]

    other = "987654321:" + "Z" * 35  # 样子对，但假 Telegram 不认（和 token 作废了一样）
    with pytest.raises(ChannelError) as caught:
        check_token(other, fake.base_url)
    assert "不认这个 token" in str(caught.value) and other not in str(caught.value)

    # 连不上：说清楚是连不上，网址（里面有 token）不出现在说明里
    with pytest.raises(ChannelError) as caught:
        check_token(TOKEN, "http://127.0.0.1:9")
    assert "连不上 Telegram" in str(caught.value) and TOKEN not in str(caught.value)


def test_errors_never_contain_the_token(fake):
    api = BotAPI(TOKEN, fake.base_url)
    # Telegram 的说明里万一带着网址（里面有 token），给人看的话和 description 里都换掉
    fake.fail_next["sendMessage"] = {"error_code": 400, "description": f"Bad Request: wrong url /bot{TOKEN}/sendMessage"}
    with pytest.raises(TelegramError) as caught:
        api.call("sendMessage", {"chat_id": "1", "text": "hi"})
    assert caught.value.code == 400
    assert TOKEN not in str(caught.value) and TOKEN not in caught.value.description and "<token>" in caught.value.description
    # 日志也挡着：BotAPI 一建好就把 token 登记成要遮住的密钥
    assert TOKEN not in log.redact(f"GET https://api.telegram.org/bot{TOKEN}/getMe")


def test_records_from_a_channel_still_stopping_do_not_bring_a_removed_platform_back(tmp_path):
    # 删掉一个平台的时候，它收消息的线程可能还没停下，还会记一笔"读到了哪儿"、"这个聊天接着哪段会话"、
    # "又忽略了一条"。这些记录只改已经有的那一份，不能新建一份空的：不然删掉的平台又回到了 channels.json 里
    # （GitHub 上偶尔出现过：删完以后文件里还有一份 credentials 是空的 telegram）
    store = ChannelStore(tmp_path / "channels.json")
    store.connect("telegram", {"token": "123:abc"}, {"id": 1, "username": "zhifox_bot"})
    store.remove("telegram")
    store.set_offset("telegram", 42)
    store.remember_chat("telegram", "111", "20261121-120000-abcdef")
    assert store.attempt("telegram", "222", "陌生人", "你好") == "ignore"
    assert "telegram" not in json.loads((tmp_path / "channels.json").read_text(encoding="utf-8"))["platforms"]
    assert store.known() == [] and not store.configured("telegram")


def test_long_polling_advances_and_remembers_the_offset(fake, tmp_path):
    store = paired_store(tmp_path / "channels.json")
    got = []

    def start_channel():
        entry = ChannelStore(store.path).get("telegram")  # 像重启后一样，从文件里读
        channel = TelegramChannel(TOKEN, fake.base_url, offset=entry["offset"], on_offset=lambda n: store.set_offset("telegram", n), poll_seconds=1)
        channel.start(got.append)
        return channel

    channel = start_channel()
    until(lambda: channel.status.state == "online", what="连上")
    fake.user_says("一")
    fake.user_says("二")
    until(lambda: len(got) == 2, what="两条消息")
    assert [event.text for event in got] == ["一", "二"]
    # 处理完一批就记下读到了哪儿：下一次 getUpdates 从第 3 条开始，Telegram 就会删掉前两条
    until(lambda: ChannelStore(store.path).get("telegram")["offset"] == 3, what="记下位置")
    until(lambda: 3 in fake.offsets, what="带着新位置去问")
    channel.stop()
    channel.join(2)
    assert channel.status.state == "off"

    # 重启：接着上次的位置读，读过的不会再处理一遍
    polls = len(fake.offsets)
    channel = start_channel()
    until(lambda: len(fake.offsets) > polls, what="重启后的第一次轮询")
    assert fake.offsets[polls] == 3
    fake.user_says("三")
    until(lambda: len(got) == 3, what="第三条")
    assert [event.text for event in got] == ["一", "二", "三"]
    channel.stop()
    channel.join(2)


def test_a_bad_token_while_polling_shows_an_error(fake):
    channel = TelegramChannel("987654321:" + "Z" * 35, fake.base_url, poll_seconds=1)
    changes = []
    channel.on_status = lambda: changes.append(channel.status.state)
    channel.start(lambda event: None)
    until(lambda: channel.status.state == "error", what="出错")
    assert "不认这个 token" in channel.status.message and "Z" * 35 not in channel.status.message
    assert changes[:2] == ["connecting", "error"]
    channel.stop()  # 正在等着重试（一分钟）的话，马上醒过来退出
    channel.join(2)
    assert channel._thread is not None and not channel._thread.is_alive()


def test_sending_retries_after_rate_limiting(fake):
    channel = TelegramChannel(TOKEN, fake.base_url)
    fake.fail_next["sendMessage"] = {"error_code": 429, "description": "Too Many Requests: retry after 1", "parameters": {"retry_after": 1}}
    started = time.monotonic()
    channel.send("111", "你好")
    assert time.monotonic() - started >= 0.9  # 按 Telegram 说的等了一秒
    assert fake.calls.count("sendMessage") == 2 and [message["plain"] for message in fake.sent] == ["你好"]


def test_html_that_telegram_rejects_is_resent_as_plain_text(fake):
    channel = TelegramChannel(TOKEN, fake.base_url)
    fake.fail_next["sendMessage"] = {"error_code": 400, "description": "Bad Request: can't parse entities: Unsupported start tag"}
    channel.send("111", "**加粗** 和 `代码`", markdown=True)
    (message,) = fake.sent
    assert "parse_mode" not in message and message["text"] == "**加粗** 和 `代码`"  # 去掉格式，原样发出去
    assert fake.calls.count("sendMessage") == 2

    # 别的错误不重发纯文字，照实报出来
    fake.fail_next["sendMessage"] = {"error_code": 403, "description": "Forbidden: bot was blocked by the user"}
    with pytest.raises(TelegramError, match="停用"):
        channel.send("111", "**hi**", markdown=True)
    assert len(fake.sent) == 1


def test_a_long_reply_is_split_into_messages_telegram_accepts(fake):
    channel = TelegramChannel(TOKEN, fake.base_url)
    reply_text = "\n\n".join(
        [f"## 第 {n} 部分\n" + "说明文字 & <尖括号> 😀 " * 40 + "\n```bash\n" + "\n".join(f"echo {n}-{i}" for i in range(60)) + "\n```" for n in range(12)]
    )
    assert utf16(reply_text) > 3 * 4096
    channel.send("111", reply_text, markdown=True)
    assert len(fake.sent) >= 4
    for message in fake.sent:
        assert message["parse_mode"] == "HTML" and utf16(message["plain"]) <= 4096  # 假 Telegram 已经检查过标签和长度
    shown = "\n".join(message["plain"] for message in fake.sent)
    for n in range(12):
        assert f"第 {n} 部分" in shown and all(f"echo {n}-{i}" in shown for i in range(60))


def test_a_confirmation_shows_the_whole_command_and_gets_two_buttons(fake):
    channel = TelegramChannel(TOKEN, fake.base_url)
    prompt = Prompt(id=7, title="助手想要执行命令：", detail="$ echo '<b>&</b>' > out.txt", where="工作目录：/srv/app",
                    allow="允许", deny="拒绝", hint="回复“允许”或“拒绝”。")
    handle = channel.ask("111", prompt)
    (message,) = fake.sent
    assert handle == str(message["message_id"])
    assert "<pre>$ echo '&lt;b&gt;&amp;&lt;/b&gt;' &gt; out.txt</pre>" in message["text"]
    assert message["plain"] == "助手想要执行命令：\n$ echo '<b>&</b>' > out.txt\n工作目录：/srv/app"
    assert [(button["text"], button["callback_data"]) for button in fake.buttons(message)] == [("允许", "ok:7"), ("拒绝", "no:7")]

    # 答复了：同一条消息去掉按钮、写上结果
    channel.settle("111", handle, "已允许")
    (edit,) = fake.edits
    assert edit["message_id"] == int(handle) and "reply_markup" not in edit
    assert visible(edit["text"], "HTML")[0].endswith("已允许")
    channel.settle("111", handle, "已允许")  # 已经写过结果的不再改
    assert len(fake.edits) == 1

    # 改不了（消息太旧）：另发一条说明结果
    second = channel.ask("111", prompt)
    fake.fail_next["editMessageText"] = {"error_code": 400, "description": "Bad Request: message can't be edited"}
    channel.settle("111", second, "已拒绝")
    assert fake.sent[-1]["plain"] == "已拒绝"


def test_a_very_long_command_is_shown_in_full_before_the_buttons(fake):
    channel = TelegramChannel(TOKEN, fake.base_url)
    detail = "\n".join(f"$ echo line-{n:04d} " + "x" * 60 for n in range(200))
    channel.ask("111", Prompt(id=3, title="助手想要执行命令：", detail=detail, where="工作目录：/w", allow="允许", deny="拒绝", hint=""))
    assert len(fake.sent) >= 3
    *before, last = fake.sent
    assert all(not fake.buttons(message) for message in before) and fake.buttons(last)  # 按钮在最后：看完了才轮到按
    assert before[0]["plain"].startswith("助手想要执行命令：") and last["plain"].endswith("工作目录：/w")
    shown = "\n".join(message["plain"] for message in fake.sent)
    assert all(f"line-{n:04d} " + "x" * 60 in shown for n in range(200))  # 一行都没省


def test_updates_become_messages_and_presses(fake):
    channel = TelegramChannel(TOKEN, fake.base_url)
    sender = {"id": 111, "is_bot": False, "first_name": "Akxan", "username": "akxan"}

    def message(**fields):
        return channel._event({"update_id": 1, "message": {"message_id": 5, "from": sender, "chat": {"id": 111, "type": "private"}, **fields}})

    inbound = message(text="你好")
    assert isinstance(inbound, Inbound) and (inbound.chat, inbound.user, inbound.name, inbound.text, inbound.private) == ("111", "111", "Akxan (@akxan)", "你好", True)
    assert message(text="hi", chat={"id": -5, "type": "group"}).private is False
    assert message(voice={"file_id": "v"}).audio is not None  # 语音交给语音转文字（见 test_voice.py）
    assert message(video_note={"file_id": "n"}).unsupported == "video_note"
    assert message(sticker={"file_id": "s"}).unsupported == "sticker"
    assert message(new_chat_members=[sender]) is None  # 进群这类系统消息
    assert channel._event({"update_id": 2, "message": {"from": {**sender, "is_bot": True}, "chat": {"id": 1}, "text": "x"}}) is None
    assert channel._event({"update_id": 3, "edited_message": {}}) is None

    photo = message(photo=[{"file_id": "small", "file_size": 10}, {"file_id": "big", "file_size": 99}], caption="看看")
    (file,) = photo.files
    assert (photo.text, file.kind, file.media_type, file.size, file.name) == ("看看", "photo", "image/jpeg", 99, "photo-5.jpg")
    assert file.thumb is not None
    document = message(document={"file_id": "d", "file_name": "报告.pdf", "mime_type": "application/pdf", "file_size": 3})
    assert [(item.name, item.kind, item.media_type) for item in document.files] == [("报告.pdf", "document", "application/pdf")]

    press = channel._event({"update_id": 4, "callback_query": {"id": "q1", "from": sender, "message": {"chat": {"id": 111}}, "data": "ok:12"}})
    assert isinstance(press, Press) and (press.chat, press.user, press.data) == ("111", "111", "ok:12")
    press.answer("已允许")
    assert fake.answers == [{"callback_query_id": "q1", "text": "已允许"}]


def test_downloading_a_file_checks_its_size(fake):
    api = BotAPI(TOKEN, fake.base_url)
    fake.files["doc-1"] = b"hello"
    assert api.download("doc-1") == b"hello"
    with pytest.raises(TelegramError, match="太大"):
        api.download("doc-1", limit=3)
    with pytest.raises(TelegramError):
        api.download("missing")


# ---- 整个程序：手机 → 智狐 → 手机 ----------------------------------------------------
# 下面的测试把程序整个组装起来（见 Rig），测试代码扮演手机上的你：s.say 发一条消息、s.press 点一个按钮，
# s.wait_reply 等机器人回话；浏览器那一侧用 s.browser（和 test_webui.py 里的是同一个）。


def fast_telegram(config, store):
    """和 hub.make_telegram 一样，只是长轮询的时间短一点（假 Telegram 那边最多也只等 0.2 秒），关的时候不用等。

    poll_seconds 不能小于 1：getUpdates 的 timeout 是按整数秒发的，0.5 会变成 0，长轮询就成了一刻不停的短轮询。
    """
    entry = store.get("telegram")
    return TelegramChannel(entry["credentials"]["token"], config.telegram_api, offset=entry["offset"],
                           on_offset=lambda offset: store.set_offset("telegram", offset), poll_seconds=1)


def stop_later(*actions) -> None:
    """在后台关掉几个本地服务器。shutdown 要等服务器的循环醒过来（最多半秒），没必要让每个测试都干等着。"""
    def run_all() -> None:
        for action in actions:
            with contextlib.suppress(Exception):  # 收尾时已经关掉了的，不用管
                action()

    threading.Thread(target=run_all, daemon=True).start()


class Rig:
    """一整套：真的网页服务器（WebApp，里面有 ChannelHub）+ 本地的假 Claude API + 本地的假 Telegram。

    connected：先在 channels.json 里写好 token（像在页面上连接过一样）；paired：再配对好主人（编号 111）。
    同一个测试里再建一个 Rig，用的是同一个数据目录和同一个假 Telegram，就相当于把程序重启了一遍。
    """

    def __init__(self, make_config, telegram: FakeTelegram, replies, approval="ask", paired=True, connected=True):
        self.tg = telegram
        self.api = FakeAPI(list(replies)).start()
        self.config = make_config(telegram_api=telegram.base_url)
        if connected:
            store = ChannelStore(self.config.channels_file)
            store.connect("telegram", {"token": TOKEN}, BOT)
            if paired and store.get("telegram")["owner"] is None:
                code = store.start_pairing("telegram")
                store.attempt("telegram", OWNER, OWNER_NAME, code["code"])
            store.set_approval("telegram", approval)
        self.ui = WebUI()
        base_url = self.api.base_url

        def client_factory(provider):
            return anthropic.Anthropic(api_key=os.environ.get(provider.key_env) or "missing", base_url=base_url, max_retries=0)

        self.app = build_app(self.config, self.ui, client_factory=client_factory)
        self.server, self.url = create_server(self.app, self.ui, port=0)
        self.web = self.server.web
        self.hub = self.web.channels
        self.hub.factories["telegram"] = fast_telegram
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.browser = Browser(self.url)
        self.browser.wait_synced()
        self.hub.start()
        self.closed = False
        if connected:
            until(lambda: self.channel() is not None and self.channel().status.state == "online", what="连上假 Telegram")

    def channel(self):
        return self.hub.channel("telegram")

    def say(self, message: str = "", **options) -> int:
        """主人（或者 options 里指定的人）发一条消息。返回发之前机器人已经发了几条，wait_reply 从那以后找。"""
        count = len(self.tg.sent)
        self.tg.user_says(message, **options)
        return count

    def wait_reply(self, after: int, contains: str, timeout: float = 10) -> dict:
        return self.tg.wait_sent(lambda message: contains in message["plain"], timeout=timeout, after=after)

    def asked(self, after: int) -> dict:
        """等一条带"允许 / 拒绝"按钮的确认请求，并且等智狐这边记下它已经发出去了（拿到了消息编号）。

        假 Telegram 记下这条消息的那一刻，"发出去了"的回答可能还在回来的路上；这时回的"允许"不算数
        （见 router._answerable）。真的手机上，你看到消息再打字，比这一点时间长得多。
        """
        message = self.tg.wait_sent(lambda message: bool(message.get("reply_markup")), after=after)
        until(lambda: any(session.waiting is not None and session.waiting.handle == str(message["message_id"]) for session in self.hub.router.sessions("telegram")) or bool(self.tg.edits and any(edit["message_id"] == message["message_id"] for edit in self.tg.edits)), what="确认请求记下了消息编号")
        return message

    def press(self, ask: dict, allow: bool = True) -> None:
        allow_button, deny_button = self.tg.buttons(ask)
        self.tg.press(ask, (allow_button if allow else deny_button)["callback_data"])

    def edit_of(self, ask: dict) -> dict:
        """确认请求答复以后，那条消息被改成的样子。"""
        return until(lambda: next((edit for edit in self.tg.edits if edit["message_id"] == ask["message_id"]), None), what="改掉按钮")

    def session(self):
        return until(lambda: self.hub.router.sessions("telegram"), what="聊天")[0]

    def idle(self) -> None:
        """等这个聊天里的事都做完（跑完的一轮存好了盘），排着要发的话也都发出去了。"""
        until(lambda: all(not item.running and not item.queued() and item._worker is None for item in self.hub.router.sessions("telegram")), what="聊天空闲")
        self.hub.router.flush()

    def sentinel(self) -> None:
        """主人发一个 /help 并等到回答：消息是按顺序处理、按顺序发的，所以在它之前的消息一定都处理完了。"""
        self.wait_reply(self.say("/help"), "/status")

    def state(self) -> dict:
        return until(lambda: self.browser.of_type("state"), what="状态")[-1]

    def platform(self) -> dict:
        return next(item for item in self.state()["channels"]["platforms"] if item["id"] == "telegram")

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        channel = self.channel()
        self.hub.close()  # 不再收消息；聊天里正在进行的停下、存好盘
        if channel is not None:
            channel.join(2)  # 重启时别让旧的那个还在收消息
        self.web.stop()
        self.app.shell.kill_running()
        self.web.wait_idle(5)

        def shutdown_server() -> None:
            self.server.shutdown()
            self.server.server_close()

        stop_later(shutdown_server, self.api.stop)
        # 不管测的是什么，机器人的 token 都不能出现在发给浏览器的任何事件里
        assert TOKEN not in json.dumps(self.browser.events, ensure_ascii=False)


def rigs(make_config, monkeypatch):
    """bot 这个 fixture 的内容（test_channels_web.py 也用它）：yield 一个 start(*回复, approval=..., paired=..., connected=...)，
    每调用一次组装出一个 Rig。同一个测试里调用两次就是重启。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    # 测试里绝不真的打开访达；当作这台电脑有文件管理器（没有的时候怎么回答，见 test_channels_web.py）
    monkeypatch.setattr(server_module, "reveal_in_file_manager", lambda path: None)
    monkeypatch.setattr(server_module, "has_file_manager", lambda: True)
    telegram = FakeTelegram(max_poll=0.2).start()
    started: list[Rig] = []

    def start(*replies, **options) -> Rig:
        rig = Rig(make_config, telegram, replies, **options)
        started.append(rig)
        return rig

    yield start
    try:
        for rig in reversed(started):
            rig.close()
    finally:
        stop_later(telegram.stop)


@pytest.fixture
def bot(make_config, monkeypatch):
    yield from rigs(make_config, monkeypatch)


def run(id: str, command: str) -> dict:
    """一条"执行命令"的回复（模型要调用 run_command）。"""
    return reply(tool_use(id, "run_command", {"command": command}), stop_reason="tool_use")


def tool_results(request: dict) -> list[dict]:
    """一个请求里最后那条用户消息中的工具结果。"""
    content = request["messages"][-1]["content"]
    return [block for block in content if isinstance(block, dict) and block.get("type") == "tool_result"]


def result_text(block: dict) -> str:
    content = block["content"]
    return content if isinstance(content, str) else "".join(part.get("text", "") for part in content)


def visible_end(edit: dict) -> str:
    """改过的确认请求的最后一行（写着结果：已允许、已拒绝……）。"""
    return visible(edit["text"], "HTML")[0].rsplit("\n", 1)[-1]


# ---- 命令和陌生人 ------------------------------------------------------------


def test_help_start_and_the_command_menu(bot):
    s = bot()
    until(lambda: s.tg.commands, what="命令菜单")
    assert [item["command"] for item in s.tg.commands] == ["new", "stop", "status", "help"]
    assert all(item["description"] for item in s.tg.commands)
    for command in ("/help", "/start", "/help@zhifox_test_bot"):  # 群里会写成 /help@机器人的名字
        message = s.wait_reply(s.say(command), "/stop")
        assert "直接发消息就行" in message["plain"] and "允许 / 拒绝" in message["plain"]
    assert s.api.requests == []  # 命令不经过模型，不花钱


def test_strangers_get_no_reply_at_all_and_groups_are_ignored(bot):
    s = bot()
    before = len(s.tg.sent)
    s.tg.user_says("你好", user=999, username="stranger")
    s.tg.user_says("/help", user=999, username="stranger")
    s.tg.user_says("123456", user=999, username="stranger")  # 不在配对的时候，猜配对码也不理
    s.tg.press({"message_id": 1, "chat_id": "999"}, "ok:1", user=999)  # 别人点按钮：也不算数
    s.tg.user_says("你好", chat=-100500, private=False)  # 主人在群里说话：群里的人都看得到，一律不理
    s.tg.user_says("/status", chat=-100500, private=False)
    s.sentinel()

    # 一个字都没回：机器人看起来就像没开着。只有主人私聊里的那一条 /help 有回答
    assert [message["chat_id"] for message in s.tg.sent[before:]] == [OWNER]
    assert s.api.requests == []
    assert [item.chat for item in s.hub.router.sessions("telegram")] == [OWNER]
    # 页面上看得到有人在试（只数私聊里陌生人发来的消息）
    until(lambda: s.platform()["ignored"] == 3, what="页面上的计数")
    assert s.hub.store.get("telegram")["ignored"] == 3


def test_pairing_from_the_chat(bot):
    s = bot(paired=False)
    s.tg.user_says("你好")  # 还没配对，也没在配对：不理
    until(lambda: s.hub.store.get("telegram")["ignored"] == 1, what="忽略了这一条")  # 等它处理完再开始配对
    code = s.hub.pair("telegram")["code"]
    until(lambda: (s.platform()["pairing"] or {}).get("code") == code, what="页面上的配对码")

    hint = s.wait_reply(s.say("你好"), "6 位配对码")
    assert hint["chat_id"] == OWNER
    after_hint = s.say("在吗")  # 只提醒一次
    wrong = "000000" if code != "000000" else "111111"
    s.wait_reply(s.say(wrong), f"还可以再试 {PAIRING_ATTEMPTS - 1} 次")
    assert len(s.tg.sent) == after_hint + 1  # "在吗"没有回答
    until(lambda: s.platform()["pairing"]["attempts_left"] == PAIRING_ATTEMPTS - 1, what="页面上的剩余次数")

    paired = s.wait_reply(s.say(f"{code[:3]} {code[3:]}"), "配对成功")  # 照着页面上的样子抄，中间带空格也认
    assert OWNER_NAME in paired["plain"] and "/new" in paired["plain"]
    until(lambda: (s.platform()["owner"] or {}).get("name") == OWNER_NAME and s.platform()["pairing"] is None, what="页面上的主人")
    assert s.hub.store.get("telegram")["ignored"] == 2  # 配对之前的"你好"、提醒过以后的"在吗"
    assert "现在：空闲" in s.wait_reply(s.say("/status"), "现在：")["plain"]  # 配好了：主人的消息照常处理


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
    assert len(s.api.requests[1]["messages"]) == 1  # 新的一段从头开始，不带着上一段的内容
    assert {(item["id"], item["title"], item.get("channel")) for item in s.app.store.summaries()} == {
        (first, "第一段", "telegram"),
        (second, "第二段", "telegram"),
    }
    assert s.hub.store.conversation_of("telegram", OWNER) == second


def test_stop_while_waiting_for_a_confirmation(bot):
    s = bot(run("toolu_1", "echo 不该运行 > ran.txt"), reply(text("停下以后的回答")))
    ask = s.asked(s.say("跑个命令"))

    s.wait_reply(s.say("/new"), "这一轮还没结束")  # 正在进行的时候不能换一段新的
    stopping = s.say("/stop")
    s.wait_reply(stopping, "好，正在停下")
    s.wait_reply(stopping, "已中断。")
    assert visible_end(s.edit_of(ask)) == "已中断"  # 按钮去掉了，写着结果
    s.idle()
    assert not (s.config.workspace / "ran.txt").exists()

    # 按钮已经没用了：再点也不会执行
    s.press(ask)
    until(lambda: s.tg.answers and s.tg.answers[-1]["text"] == "这个确认请求已经处理过了", what="按钮上的提示")

    # 每个工具调用都有结果（被中断的也有），下一轮接着聊不会被 API 拒绝
    s.wait_reply(s.say("继续"), "停下以后的回答")
    history = s.api.requests[1]["messages"]
    results = [block for message in history if isinstance(message["content"], list) for block in message["content"] if block.get("type") == "tool_result"]
    assert [block["tool_use_id"] for block in results] == ["toolu_1"]
    s.idle()
    s.wait_reply(s.say("/stop"), "现在没有在进行的事")


def test_stop_kills_a_running_command(bot):
    s = bot(run("toolu_1", "touch started && sleep 30"), approval="all")
    start = s.say("跑一个很久的命令")
    s.wait_reply(start, "已自动允许")  # 全部自动允许：没问就执行了，聊天里照样看得到它做了什么
    until(lambda: (s.config.workspace / "started").exists(), what="命令开始运行")
    began = time.monotonic()
    stopping = s.say("/stop")
    s.wait_reply(stopping, "好，正在停下")
    s.wait_reply(stopping, "已中断。")
    s.idle()
    assert time.monotonic() - began < 10  # sleep 30 被结束了，不用等它跑完
    assert len(s.api.requests) == 1


def test_read_only_commands_run_without_asking_in_a_readonly_channel(bot):
    s = bot(run("toolu_1", "ls"), run("toolu_2", "touch made.txt"), reply(text("看过了。")), approval="readonly")
    start = s.say("看看有什么文件，再建一个")
    s.wait_reply(start, "已自动允许（只读）：")  # 聊天里照样看得到它没问就做了什么，以及为什么
    ask = s.asked(start)  # 会改东西的照常问
    assert "touch made.txt" in ask["plain"]
    s.press(ask, allow=False)
    s.wait_reply(start, "看过了")
    s.idle()
    assert not (s.config.workspace / "made.txt").exists()
    status = s.wait_reply(s.say("/status"), "现在：")["plain"]
    assert "确认方式：只读的命令不用问" in status


def test_status_tells_what_is_going_on(bot):
    from harness.providers import PROVIDERS

    s = bot(run("toolu_1", "echo hi"), reply(text("好了")))
    status = s.wait_reply(s.say("/status"), "现在：")["plain"]
    assert f"模型：{PROVIDERS['anthropic'].label} · claude-opus-5-5" in status
    assert "确认方式：每次都问" in status and "会话：新对话" in status and "现在：空闲" in status
    assert "今天花了 $0.0000，本月 $0.0000" in status

    start = s.say("跑命令")
    ask = s.asked(start)
    status = s.wait_reply(s.say("/status"), "现在：")["plain"]
    assert "现在：等你批准" in status and "会话：跑命令" in status
    s.press(ask)
    s.wait_reply(start, "好了")
    s.idle()
    status = s.wait_reply(s.say("/status"), "现在：")["plain"]
    assert "现在：空闲" in status and "今天花了 $0.0000" not in status  # 花的钱记上了


# ---- 确认 ------------------------------------------------------------------


def test_allowing_with_the_button_runs_the_command(bot):
    s = bot(
        reply(text("我来看看。"), tool_use("toolu_1", "run_command", {"command": "echo '<b>&</b>' && pwd"}), stop_reason="tool_use"),
        reply(text("**一切正常**。")),
    )
    start = s.say("看看")
    first = s.wait_reply(start, "我来看看")  # 一段文字写完就发出去，不等整轮结束
    ask = s.asked(start)
    assert s.tg.sent.index(first) < s.tg.sent.index(ask)  # 顺序和助手说的一样

    # 确认请求上写着完整的命令（原样、在代码块里）和工作目录
    assert ask["text"].startswith("<b>助手想要执行命令：</b>")
    assert "<pre>$ echo '&lt;b&gt;&amp;&lt;/b&gt;' &amp;&amp; pwd</pre>" in ask["text"]
    assert ask["plain"].endswith(f"工作目录：{s.config.workspace}")
    allow, deny = s.tg.buttons(ask)
    confirm_id = int(allow["callback_data"].removeprefix("ok:"))
    assert (allow["text"], deny["text"], deny["callback_data"]) == ("允许", "拒绝", f"no:{confirm_id}")
    # 网页上也看得到这个等着批准的操作
    (pending,) = until(lambda: s.state()["channels"]["approvals"], what="页面上的确认请求")
    assert (pending["platform"], pending["chat"], pending["id"], pending["name"]) == ("telegram", OWNER, confirm_id, OWNER_NAME)
    assert "echo '<b>&</b>' && pwd" in pending["summary"]
    assert s.hub.snapshot()["platforms"][0]["chats"][0]["state"] == "waiting"

    s.press(ask)
    final = s.wait_reply(start, "一切正常")
    assert "<b>一切正常</b>" in final["text"]
    assert {"chat_id": OWNER, "action": "typing"} in s.tg.actions  # 这一轮进行的时候，聊天里显示"正在输入"
    until(lambda: s.tg.answers and s.tg.answers[-1]["text"] == "已允许", what="按钮上的提示")
    assert visible_end(s.edit_of(ask)) == "已允许"
    (result,) = tool_results(s.api.requests[1])
    assert "<b>&</b>" in result_text(result) and str(s.config.workspace) in result_text(result) and not result.get("is_error")
    s.idle()
    until(lambda: s.state()["channels"]["approvals"] == [], what="页面上的确认请求消失")


def test_denying_with_the_button_tells_the_model(bot):
    s = bot(run("toolu_1", "rm -rf build"), reply(text("好的，不删了。")), reply(text("顺便的回答")))
    start = s.say("清理一下")
    ask = s.asked(start)
    # 等着批准的时候又说了一句：告诉你它排在后面
    s.wait_reply(s.say("顺便问一下"), "还有一个操作在等你批准")
    s.press(ask, allow=False)
    s.wait_reply(start, "好的，不删了")
    s.wait_reply(start, "顺便的回答")  # 排着的那一条接着跑
    assert visible_end(s.edit_of(ask)) == "已拒绝"
    until(lambda: any(answer["text"] == "已拒绝" for answer in s.tg.answers), what="按钮上的提示")
    (result,) = tool_results(s.api.requests[1])
    assert result_text(result).startswith("用户拒绝了这个操作") and result["is_error"] is True
    s.idle()


def test_answering_with_words_instead_of_buttons(bot):
    s = bot(run("toolu_a", "echo a"), run("toolu_b", "echo b"), run("toolu_c", "echo c"), reply(text("三件事都处理完了")))
    start = s.say("做三件事")
    first = s.asked(start)
    s.say("允许")
    second = s.asked(s.tg.sent.index(first) + 1)
    s.say("Yes!")  # 不分大小写，标点去掉
    third = s.asked(s.tg.sent.index(second) + 1)
    s.say("拒绝")
    s.wait_reply(start, "三件事都处理完了")

    assert [visible_end(s.edit_of(ask)) for ask in (first, second, third)] == ["已允许", "已允许", "已拒绝"]
    assert [result_text(tool_results(request)[0]).strip() for request in s.api.requests[1:3]] == ["a", "b"]
    assert result_text(tool_results(s.api.requests[3])[0]).startswith("用户拒绝了这个操作")
    s.idle()


def test_the_allow_and_deny_words_do_not_overlap():
    assert not ALLOW_WORDS & DENY_WORDS
    assert {"允许", "yes", "ok", "sí", "vale"} <= ALLOW_WORDS and {"拒绝", "no", "deny", "denegar"} <= DENY_WORDS


def test_the_web_and_the_chat_race_for_the_same_confirmation(bot):
    s = bot(run("toolu_a", "echo a"), run("toolu_b", "echo b"), reply(text("完")))
    start = s.say("做两件事")
    first = s.asked(start)
    (pending,) = until(lambda: s.state()["channels"]["approvals"], what="页面上的确认请求")

    # 先在网页上点了允许：聊天里的那条消息改成"已在网页上允许"，再点聊天里的按钮就不算数了
    answer = {"platform": "telegram", "chat": OWNER, "confirm_id": pending["id"], "allow": True}
    assert s.browser.post("/api/channel/confirm", answer) == (200, {"ok": True})
    assert visible_end(s.edit_of(first)) == "已在网页上允许"
    s.press(first)
    until(lambda: s.tg.answers and s.tg.answers[-1]["text"] == "这个确认请求已经处理过了", what="按钮上的提示")
    status, body = s.browser.post("/api/channel/confirm", answer)
    assert status == 409 and "已经处理过了" in body["error"]

    # 两边同时点：只有一边算数，另一边被告知"已经处理过了"
    second = s.asked(s.tg.sent.index(first) + 1)
    confirm_id = int(s.tg.buttons(second)[0]["callback_data"].removeprefix("ok:"))
    answers_before = len(s.tg.answers)
    web_result = []
    racer = threading.Thread(target=lambda: web_result.append(s.browser.post("/api/channel/confirm", {**answer, "confirm_id": confirm_id})))
    racer.start()
    s.press(second)
    racer.join(10)
    chat_answer = until(lambda: s.tg.answers[answers_before:], what="按钮上的提示")[0]["text"]
    web_won = web_result[0][0] == 200
    assert web_won != (chat_answer == "已允许")
    assert chat_answer in ("已允许", "这个确认请求已经处理过了")
    assert visible_end(s.edit_of(second)) == ("已在网页上允许" if web_won else "已允许")
    s.wait_reply(start, "完")
    assert len(s.api.requests) == 3  # 每条命令只执行了一次
    s.idle()


def test_a_web_answer_is_labelled_even_when_the_turn_resumes_at_once(bot, monkeypatch):
    s = bot(run("toolu_1", "echo hi"), reply(text("好了")))
    start = s.say("跑")
    ask = s.asked(start)
    session = s.session()
    original = session.ui.answer_confirm

    def answer_then_pause(*args):
        accepted = original(*args)
        time.sleep(0.3)  # 答复交出去了，还没记下"是网页上答的"：agent 线程这时已经醒了、写好了结果
        return accepted

    monkeypatch.setattr(session.ui, "answer_confirm", answer_then_pause)
    confirm_id = int(s.tg.buttons(ask)[0]["callback_data"].removeprefix("ok:"))
    assert s.browser.post("/api/channel/confirm", {"platform": "telegram", "chat": OWNER, "confirm_id": confirm_id, "allow": True})[0] == 200
    s.wait_reply(start, "好了")
    assert visible_end(s.edit_of(ask)) == "已在网页上允许"


def test_a_word_sent_before_the_confirmation_arrives_does_not_approve_it(bot, monkeypatch):
    s = bot(
        reply(text("我先看看。"), tool_use("toolu_1", "run_command", {"command": "touch approved.txt"}), stop_reason="tool_use"),
        reply(text("完")), reply(text("嗯")),
    )
    channel = s.channel()
    original = channel.ask

    def slow_ask(chat, prompt):
        time.sleep(1)  # 网络慢、或者正被 Telegram 限流：确认请求晚一点才到手机上
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


def test_each_channel_has_its_own_approval_mode(bot):
    create = tool_use("toolu_f", "str_replace_based_edit_tool", {"command": "create", "path": "note.txt", "file_text": "记一笔"})
    s = bot(
        run("toolu_1", "echo one"), reply(text("一完成")),
        reply(create, stop_reason="tool_use"), run("toolu_2", "echo two"), reply(text("二完成")),
        run("toolu_3", "echo three"), reply(text("三完成")),
        run("toolu_w", "echo web"), reply(text("网页完成")),
    )
    # 网页对话里改成了"全部自动允许"：聊天里照样每次都问（各是各的一档）
    assert s.browser.post("/api/approval", {"mode": "all"}) == (200, {"ok": True})
    start = s.say("第一件")
    s.press(s.asked(start))
    s.wait_reply(start, "一完成")
    s.idle()

    # 这个通道改成"改工作目录里的文件不用问"：建文件不问，执行命令照样问
    assert s.browser.post("/api/channel/approval", {"platform": "telegram", "mode": "edits"}) == (200, {"ok": True})
    until(lambda: s.platform()["approval"] == "edits", what="页面上的确认方式")
    start = s.say("第二件")
    auto = s.wait_reply(start, "已自动允许")
    assert "note.txt" in auto["plain"]
    ask = s.asked(start)
    assert "echo two" in ask["plain"] and (s.config.workspace / "note.txt").read_text(encoding="utf-8") == "记一笔"
    s.press(ask)
    s.wait_reply(start, "二完成")
    s.idle()

    # 全部自动允许：命令也不问了，但聊天里看得到执行了什么
    assert s.browser.post("/api/channel/approval", {"platform": "telegram", "mode": "all"}) == (200, {"ok": True})
    start = s.say("第三件")
    assert "echo three" in s.wait_reply(start, "已自动允许")["plain"]
    s.wait_reply(start, "三完成")
    s.idle()
    assert not [message for message in s.tg.sent[start:] if message.get("reply_markup")]

    # 反过来也一样：通道是"全部自动允许"，网页对话改回"每次都问"以后，网页上照样要问
    assert s.browser.post("/api/approval", {"mode": "ask"}) == (200, {"ok": True})
    assert s.browser.post("/api/message", {"text": "网页上跑命令"})[0] == 200
    confirm = s.browser.wait_for("confirm")
    assert s.browser.post("/api/confirm", {"id": confirm["id"], "allow": True}) == (200, {"ok": True})
    s.browser.wait_for("turn_end")
    assert s.state()["approval"] == "ask" and s.hub.store.get("telegram")["approval"] == "all"


def test_messages_queue_up_behind_the_running_turn(bot):
    wait_for_go = "touch started; while [ ! -f go ]; do sleep 0.05; done"
    s = bot(run("toolu_1", wait_for_go), *[reply(text(f"第{n}条完成")) for n in range(1, 2 + CHANNEL_QUEUE)], approval="all")
    start = s.say("第1条")
    until(lambda: (s.config.workspace / "started").exists(), what="第一轮开始运行")
    for n in range(2, 2 + CHANNEL_QUEUE):
        s.wait_reply(s.say(f"第{n}条"), f"前面还有 {n - 1} 件事")
    chat = s.hub.snapshot()["platforms"][0]["chats"][0]
    assert (chat["state"], chat["queued"]) == ("running", CHANNEL_QUEUE)
    s.wait_reply(s.say("第7条"), "排着的消息太多了")  # 排满了：请你等一等，这一条不收

    (s.config.workspace / "go").touch()
    finished = [s.wait_reply(start, f"第{n}条完成") for n in range(1, 2 + CHANNEL_QUEUE)]
    positions = [s.tg.sent.index(message) for message in finished]
    assert positions == sorted(positions)
    s.idle()
    # 一条一条按顺序跑，每一轮都接着同一段会话；第 7 条没有跑
    assert len(s.api.requests) == 2 + CHANNEL_QUEUE
    asked = [[block["text"] for block in request["messages"][-1]["content"] if block["type"] == "text"] for request in s.api.requests[2:]]
    assert [texts[-1] for texts in asked] == [f"第{n}条" for n in range(2, 2 + CHANNEL_QUEUE)]
    assert "第7条" not in json.dumps(s.api.requests, ensure_ascii=False)


# ---- 图片、文件、处理不了的 ------------------------------------------------------


def test_a_photo_reaches_the_model_as_an_image(bot):
    s = bot(reply(text("是一张很小的图")))
    start = s.say("这是什么", photo=JPEG_BYTES)
    s.wait_reply(start, "是一张很小的图")
    content = s.api.requests[0]["messages"][-1]["content"]
    (image,) = [block for block in content if block["type"] == "image"]
    assert image["source"] == {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(JPEG_BYTES).decode()}
    assert any(block["type"] == "text" and block["text"] == "这是什么" for block in content)  # 图片的说明文字就是这条消息
    s.idle()
    # 保存的会话里有这个附件的概要（网页上打开时显示）
    saved = s.app.store.load(s.session().conversation.id)
    (user,) = [event for event in saved.events if event["type"] == "user"]
    assert user["attachments"][0]["name"].startswith("photo-") and user["attachments"][0]["kind"] == "image"


def test_the_web_preview_of_a_photo_is_sharp_and_the_model_gets_the_biggest(bot):
    # Telegram 把一张照片存成好几种尺寸。最小的那张只有 90 像素左右，拿来当网页上的预览图，放大以后是糊的（用户截图问过）。
    # 预览图用够大的那张里最小的一张（长边不小于 CHANNEL_THUMB_EDGE），模型拿到的是最大的那张
    sizes = [(edge, JPEG_BYTES + f"<{edge}>".encode()) for edge in (90, 320, 800, 1280)]
    s = bot(reply(text("看到了")))
    start = s.say("这是什么", photo_sizes=sizes)
    s.wait_reply(start, "看到了")
    content = s.api.requests[0]["messages"][-1]["content"]
    (image,) = [block for block in content if block["type"] == "image"]
    assert base64.b64decode(image["source"]["data"]).endswith(b"<1280>")
    s.idle()
    saved = s.app.store.load(s.session().conversation.id)
    (user,) = [event for event in saved.events if event["type"] == "user"]
    thumb = user["attachments"][0]["thumb"]
    assert thumb.startswith("data:image/jpeg;base64,") and base64.b64decode(thumb.split(",", 1)[1]).endswith(b"<800>")


def test_a_text_file_reaches_the_model_as_text(bot):
    s = bot(reply(text("读完了")))
    start = s.say("帮我看看", document=("notes.txt", "text/plain", "第一行\n第二行 ✓".encode()))
    s.wait_reply(start, "读完了")
    sent = json.dumps(s.api.requests[0]["messages"][-1], ensure_ascii=False)
    assert "第二行 ✓" in sent and "notes.txt" in sent
    s.idle()


def test_things_it_cannot_handle_get_an_explanation(bot):
    s = bot()
    s.wait_reply(s.say("", extra={"sticker": {"file_id": "sticker-1"}}), "还处理不了贴纸")
    s.wait_reply(s.say("", document=("data.bin", "application/octet-stream", b"\x00\x01\x02binary")), "这个文件用不了")
    s.idle()
    assert s.api.requests == []


# ---- 和网页同时进行、记账、存盘、重启 ------------------------------------------------


def test_a_chat_turn_runs_while_a_web_turn_waits_for_its_own_confirmation(bot):
    s = bot(run("toolu_w", "echo web"), reply(text("聊天里的回答")), reply(text("网页的回答")))
    assert s.browser.post("/api/message", {"text": "网页上跑命令"})[0] == 200
    confirm = s.browser.wait_for("confirm")

    # 网页上那一轮停在确认上，聊天里照样能聊完一轮：谁也不等谁
    s.wait_reply(s.say("你好"), "聊天里的回答")
    s.idle()
    assert s.web._turn.locked()  # 网页那一轮还在等
    assert s.browser.post("/api/confirm", {"id": confirm["id"], "allow": True}) == (200, {"ok": True})
    s.browser.wait_for("turn_end")

    # 两段各自的会话；花的钱都记在账上，来源分得清
    summaries = {item["title"]: item.get("channel") for item in s.app.store.summaries()}
    assert summaries == {"网页上跑命令": None, "你好": "telegram"}
    rows = [json.loads(line) for line in s.config.ledger_file.read_text(encoding="utf-8").splitlines()]
    assert sorted(row["source"] for row in rows) == ["channel", "chat", "chat"]


def test_a_chat_continues_its_conversation_after_a_restart(bot):
    s = bot(reply(text("记住了：蓝色")))
    s.wait_reply(s.say("我喜欢蓝色"), "记住了")
    s.idle()
    conversation_id = s.session().conversation.id
    (summary,) = s.app.store.summaries()
    assert (summary["id"], summary["title"], summary["channel"]) == (conversation_id, "我喜欢蓝色", "telegram")
    saved = json.loads((s.config.conversations_dir / f"{conversation_id}.json").read_text(encoding="utf-8"))
    assert saved["channel"] == "telegram"
    until(lambda: any(item.get("channel") == "telegram" for item in s.state()["conversations"]), what="页面上的会话列表")

    s.close()
    again = bot(reply(text("你喜欢蓝色")))  # 同一个数据目录：重启
    again.wait_reply(again.say("我喜欢什么颜色？"), "你喜欢蓝色")
    again.idle()
    assert again.session().conversation.id == conversation_id  # 接着同一段会话聊
    messages = again.api.requests[0]["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant", "user"]
    assert "我喜欢蓝色" in json.dumps(messages[0], ensure_ascii=False)
    (summary,) = again.app.store.summaries()
    assert summary["id"] == conversation_id
    events = again.app.store.load(conversation_id).events
    assert [event["text"] for event in events if event["type"] == "user"] == ["我喜欢蓝色", "我喜欢什么颜色？"]


# ---- 几处同时改的东西：去掉锁，这几个测试就会失败 ------------------------------------------


def test_two_programs_writing_the_settings_file_at_once_lose_nothing(tmp_path):
    # 服务器上：作为服务开着的智狐每聊一轮就记一次"这个聊天接着哪段会话"，同时你在命令行里改设置。
    # 两个 ChannelStore 各有各的线程锁（就像两个程序），只有文件锁和"改之前先从磁盘读最新的"护着它们。
    # 去掉 _file_lock，或者 _change 里不先 _load，两边会拿着旧的内容各写各的，后写的把先写的盖掉
    path = tmp_path / "channels.json"
    service, command_line = ChannelStore(path), ChannelStore(path)
    service.connect("telegram", {"token": TOKEN}, BOT)

    def write(store: ChannelStore, side: str) -> None:
        for n in range(15):
            store.remember_chat("telegram", f"{side}-{n}", f"20261121-0900{n:02d}-aaaaaa")

    workers = [threading.Thread(target=write, args=(store, f"{name}{k}")) for k in range(3) for name, store in (("s", service), ("c", command_line))]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    chats = ChannelStore(path).get("telegram")["chats"]
    assert len(chats) == 6 * 15  # 一条都没丢


def test_only_one_side_gets_a_conversation_at_a_time():
    # 网页上发消息和 Telegram 上来消息几乎同时：同一段会话只能有一边认领到，另一边被告知"正在那边进行"。
    # 去掉 Claims 里的锁，两边可能都以为自己拿到了，各跑一轮，后存盘的把先存的那一轮盖掉
    from harness.claims import Claims

    claims = Claims()
    for _ in range(50):
        winners: list[str] = []
        start = threading.Barrier(8)

        def grab(who: str, start: threading.Barrier = start, winners: list = winners) -> None:
            start.wait()
            if claims.take("20261121-090000-aaaaaa", who) is None:
                winners.append(who)

        racers = [threading.Thread(target=grab, args=(f"telegram:{n}",)) for n in range(8)]
        for racer in racers:
            racer.start()
        for racer in racers:
            racer.join()
        assert len(winners) == 1
        claims.release("20261121-090000-aaaaaa", winners[0])
    # 拿着的一方放开以后，等着的那一方马上拿到（不用等到超时）
    assert claims.take("c", "web") is None
    threading.Timer(0.05, lambda: claims.release("c", "web")).start()
    began = time.monotonic()
    assert claims.take("c", "telegram:1", timeout=5) is None and time.monotonic() - began < 2
