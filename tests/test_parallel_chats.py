"""几段对话同时进行：网页这一侧（见 harness/webui/sessions.py、sessions_api.py）。

和 test_rewind_web.py 一样：真实的 HTTP 服务器，测试代码扮演浏览器，模型是本地的假 API（不花钱），
工作目录和数据目录都在 tmp_path 里。要守住的几样：
  - 你正看着的那一段在跑时，打开别的会话、开新对话，它留在后台接着跑；换回去看它，事件从头重放、流式输出接着来；
  - 每段各有各的：事件日志、会话文件、shell 和"停止"、确认卡片、电脑和浏览器的 session、改文件的记账；
  - 同一段不能同时跑两轮；同时在跑的最多 max_parallel_chats 段（重新生成也算）；
  - 事件流换着看时先发一个 reset（那一段的日志不是从 reset 开始的，就补一个不带编号的），刷新页面、断线重连也对得上；
  - 退出、从备份恢复、聊天通道认领着的时候照样守规矩。

几段同时跑时，"谁先发请求谁拿下一条回复"就说不准了，所以每段对话的回复按它第一句话里的记号分开
（fake_api.py 的 routes：请求里第一条消息含有这个记号的，从它自己的那一串里按顺序取）。
要让一轮停在中间，用一条要你确认的 run_command：它停在确认卡片上，等测试答复才往下走，比卡住假服务器可靠。

换着看另一段时事件流里会有一个不带编号的 reset，所以这里等事件不按编号等，而是按"浏览器收到的第几个"往后找（见 wait_new）。
s.ui 是一开始那一段的日志，换着看以后它可能已经在后台了：读正看着的那一段用 s.web.sessions.viewed，读某一段用 sessions.find。
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest
import test_restore_web
import test_rewind_web
import test_webui
from fake_api import failure, reply, text, tool_use
from fake_desktop import FakeDesktop, install
from test_rewind_web import dumps
from test_webui import MISSING, Browser, kinds, latest_state, saved_file

from harness.desktop import session as desktop_session
from harness.webui import lifecycle
from harness.webui.sessions import Session, Sessions
from harness.webui.ui import WebUI, latest_state_only

# 借用 test_rewind_web 的 fixture：网页服务器 + 假 API，s.web 是服务器上的 WebApp，助手的钟固定住
serve = test_webui.serve
rig = test_rewind_web.rig
temp_dir = test_restore_web.temp_dir  # 上传的备份写进这个测试自己的临时文件夹

# 每段对话第一句话里的记号：假 API 照着它把回复分给那一段（见文件开头）
A, B, C = "【甲】", "【乙】", "【丙】"
OK = (200, {"ok": True})
EDIT = "str_replace_based_edit_tool"


def ask(command: str = "echo hi", n: int = 1) -> dict:
    """一步：调用一次 run_command。"每次都问"（和"改文件不用问"）这一档下会停在确认卡片上。"""
    return reply(tool_use(f"toolu_{n}", "run_command", {"command": command}), stop_reason="tool_use")


def screenshot(*ids: str) -> dict:
    """一步：截几张屏幕的图（电脑工具包，见 tests/test_desktop_web.py）。"""
    return reply(*(tool_use(shot, "screenshot", {}, toolset="computer") for shot in ids), stop_reason="tool_use")


@pytest.fixture
def start(rig):
    """起服务器，每段对话的回复按记号分开（routes）。

    测试结束时把还在跑的每一段都停下、等它们收尾：停在确认卡片上的那几轮是后台线程，不停下会一直等，拖到下一个测试。
    从备份恢复以后换上了新的 WebApp，所以每次现找 s.server.web。"""
    made = []

    def start(routes: dict[str, list[dict]], **options):
        s = rig(**options)
        s.api.routes = {marker: list(items) for marker, items in routes.items()}
        made.append(s)
        return s

    yield start
    for s in made:
        web = s.server.web
        web.stop_all()
        web.wait_idle(10)


# ---- 等、读 ------------------------------------------------------------------------


def until(condition, what: str, timeout: float = 10):
    """等到 condition() 返回真值，并返回它（GitHub 的机器慢，不写固定的 sleep）。"""
    deadline = time.monotonic() + timeout
    while not (found := condition()):
        assert time.monotonic() < deadline, f"没有等到：{what}"
        time.sleep(0.01)
    return found


def seen(browser: Browser) -> int:
    """浏览器此刻已经收到了几个事件。之后的动作带来的事件从这个位置往后找：换着看时补的那个 reset 没有编号，只能按位置找。"""
    return len(browser.events)


def wait_new(browser: Browser, kind: str, start: int, check=lambda event: True, timeout: float = 10) -> int:
    """等浏览器收到的第 start 个以后、类型是 kind（而且 check 成立）的事件，返回它在 browser.events 里的位置。"""
    deadline = time.monotonic() + timeout
    while True:
        assert not browser.problems, "事件流不符合约定：" + "；".join(browser.problems)
        events = list(browser.events)
        found = next((index for index in range(start, len(events)) if events[index]["type"] == kind and check(events[index])), None)
        if found is not None:
            return found
        assert time.monotonic() < deadline, f"没有等到第 {start} 个以后的 {kind} 事件；之后收到的是 {kinds(events[start:])}"
        time.sleep(0.01)


def switched(browser: Browser, start: int) -> tuple[dict, list[dict], dict]:
    """动作之后页面换了内容（reset 到下一个状态之间算重放）：返回（那个 reset，中间重放的事件，重放完的那个状态）。"""
    reset = wait_new(browser, "reset", start)
    state = wait_new(browser, "state", reset + 1)
    events = list(browser.events)
    return events[reset], events[reset + 1 : state], events[state]


def replay_then_turn(browser: Browser, start: int) -> tuple[list[dict], list[dict]]:
    """倒回最后一轮（重新生成）以后：（reset 和下一个状态之间重放的事件，那个状态之后新的一轮直到 turn_end）。"""
    reset = wait_new(browser, "reset", start)
    state = wait_new(browser, "state", reset + 1)
    end = wait_new(browser, "turn_end", state + 1)
    events = list(browser.events)
    return events[reset + 1 : state], events[state + 1 : end + 1]


def post_ok(browser: Browser, path: str, data: dict | None = None) -> dict:
    status, answer = browser.post(path, data)
    assert status == 200, (path, answer)
    return answer


def send(s, words: str) -> int:
    """在正看着的那一段里发一条消息（要被收下），返回发之前浏览器收到了几个事件。"""
    start = seen(s.browser)
    post_ok(s.browser, "/api/message", {"text": words})
    return start


def talk(s, words: str) -> dict:
    """发一条消息，等这一轮结束（中间不停在确认上），返回它结束前的最后一个状态。

    页面收到 turn_end 以后马上就可能发下一个请求；这一段的锁紧接着就放开（见 sessions_api._settle），这里也等它放开，
    不赌那两行之间的时机。"""
    session = s.web.sessions.viewed
    start = send(s, words)
    end = wait_new(s.browser, "turn_end", start)
    until(lambda: not session.lock.locked(), "这一段的锁放开")
    return [event for event in s.browser.events[start:end] if event["type"] == "state"][-1]


def pause(s, words: str) -> dict:
    """发一条消息，等它停在确认卡片上，返回那张卡片（confirm 事件）。"""
    start = send(s, words)
    return s.browser.events[wait_new(s.browser, "confirm", start)]


def new_chat(s) -> dict:
    """点"新对话"，等页面换上新的一段，返回重放完的那个状态。"""
    start = seen(s.browser)
    post_ok(s.browser, "/api/new")
    return switched(s.browser, start)[2]


def open_chat(s, conversation_id: str) -> tuple[dict, list[dict], dict]:
    """打开一段会话（保存过的，或者正在后台跑着的），等页面换过去。返回值见 switched。"""
    start = seen(s.browser)
    post_ok(s.browser, "/api/conversation/open", {"id": conversation_id})
    return switched(s.browser, start)


def answer(s, confirm_id: int, allow: bool = True) -> None:
    assert s.browser.post("/api/confirm", {"id": confirm_id, "allow": allow}) == OK


def viewing(s) -> str:
    """正看着的那一段的会话编号（照着页面收到的最新状态）。"""
    return latest_state(s.browser)["conversation"]


def alive(s, conversation_id: str) -> Session:
    """名单里的那一段（要还活着）。"""
    session = s.web.sessions.find(conversation_id)
    assert session is not None, f"{conversation_id} 不在名单里"
    return session


def log_of(session: Session) -> list[dict]:
    """一段的事件日志（服务器上的，和页面收到的是同一份）。"""
    return session.ui.events_after(0, timeout=0)


def gone(s, conversation_id: str) -> None:
    """等后台那一段跑完、存好盘、从名单里拿掉（东西都在磁盘上了）。"""
    until(lambda: s.web.sessions.find(conversation_id) is None, f"{conversation_id} 在后台跑完、从名单里拿掉")


def texts(events: list[dict], kind: str) -> list[str]:
    return [event["text"] for event in events if event["type"] == kind]


def outcome(saved: dict) -> list[str]:
    """会话文件里每张确认卡片的结局（allowed / denied / stopped）。"""
    return [event["outcome"] for event in saved["events"] if event["type"] == "confirm"]


# ---- 1. 一段停在确认上，另一段照样能打开、能聊 ------------------------------------------


def test_while_one_turn_waits_in_the_background_another_conversation_is_opened_and_both_finish(start):
    s = start({
        B: [reply(text("乙的第一个回答")), reply(text("乙的第二个回答"))],
        A: [ask(), reply(text("甲做完了"))],
    })  # fmt: skip
    b = talk(s, B + "第一句")["conversation"]  # 先有一段存好了的 B
    a = new_chat(s)["conversation"]
    card = pause(s, A + "跑一条命令")  # A 停在确认卡片上

    # 打开 B：A 留在后台接着等；页面上换成 B（B 的日志是从它自己的 reset 开始的）→ B 的重放 → 状态
    reset, replayed, state = open_chat(s, b)
    assert reset["type"] == "reset"
    assert kinds(replayed) == ["user", "turn_start", "text", "block_end", "trace", "turn_end"]
    assert texts(replayed, "user") == [B + "第一句"] and A not in dumps(replayed)
    assert state["conversation"] == b and state["live"] == {a: "confirm"}
    assert s.web.sessions.viewed.id == b and alive(s, a).running and alive(s, a).ui.waiting_on(card["id"])

    # 在 B 里接着聊，跑完；A 一直等着
    talk(s, "第二句")
    assert alive(s, a).ui.waiting_on(card["id"])

    # 再答 A：只到 A，它在后台跑完、存好盘，从名单里拿掉；会话栏上标着"跑完了"
    mark = seen(s.browser)
    answer(s, card["id"])
    gone(s, a)
    done = s.browser.events[wait_new(s.browser, "state", mark, check=lambda event: event["live"].get(a) == "done")]
    assert done["conversation"] == b

    # 两段各存各的，事件不串
    saved_a, saved_b = saved_file(s.config, a), saved_file(s.config, b)
    assert texts(saved_a["events"], "user") == [A + "跑一条命令"] and texts(saved_a["events"], "text") == ["甲做完了"]
    assert outcome(saved_a) == ["allowed"] and B not in dumps(saved_a)
    assert [message["role"] for message in saved_a["messages"]] == ["user", "assistant", "user", "assistant"]
    assert texts(saved_b["events"], "user") == [B + "第一句", "第二句"]
    assert texts(saved_b["events"], "text") == ["乙的第一个回答", "乙的第二个回答"] and A not in dumps(saved_b)
    assert [message["role"] for message in saved_b["messages"]] == ["user", "assistant"] * 2
    # 发给模型的请求也不串：每个请求只带着其中一段的历史
    assert len(s.api.requests) == 4 and all((A in dumps(body)) != (B in dumps(body)) for body in s.api.requests)
    # 页面上看着 B 的时候，A 的事件一个都没收到
    assert "甲做完了" not in dumps(s.browser.events[mark:])

    # 打开 A：从磁盘读，"跑完了"的标记去掉
    _, replayed, state = open_chat(s, a)
    assert texts(replayed, "text") == ["甲做完了"] and a not in state["live"]


# ---- 2. 跑着的时候开新对话 --------------------------------------------------------


def test_a_new_conversation_while_one_runs_leaves_that_one_in_the_background(start):
    s = start({A: [ask(), reply(text("甲做完了"))]})
    a = viewing(s)
    card = pause(s, A + "跑一条命令")

    state = new_chat(s)
    fresh = s.web.sessions.viewed
    assert state["conversation"] == fresh.id != a and state["title"] == ""
    assert state["live"] == {a: "confirm"}  # 停在确认上：会话栏上标着"等你确认"
    assert state["parallel"] == {"max": 3, "running": 1}
    # 新的这一段是空的：自己的日志里只有 reset 和状态，助手的历史是空的，没在跑
    assert kinds(log_of(fresh)) == ["reset", "state"] and fresh.chat.agent.messages == [] and not fresh.running
    background = alive(s, a)
    assert background is not fresh and len(s.web.sessions.all()) == 2
    assert background.running and background.ui.waiting_on(card["id"]) and background.chat is not fresh.chat
    # A 的第一轮开始时就列进了会话栏（还没存盘），在后台也一直列着
    assert [item["id"] for item in state["conversations"]] == [a]

    answer(s, card["id"])
    gone(s, a)
    assert outcome(saved_file(s.config, a)) == ["allowed"]


def test_a_first_message_that_fails_in_the_background_stays_listed_and_marked(start):
    """一段新对话的第一条消息在后台失败了（你已经换去看别的了）：存盘时它"当没发生过"，磁盘上什么都没有。
    它不能就这样悄悄没了：留在名单里、会话栏上照样列着（标题是那句话），标着"出错了"；打开它看得到为什么，还能再发一次。"""
    s = start({A: [failure(400, "invalid_request_error", "甲的请求不对"), reply(text("甲这次答了"))]})
    gate = threading.Event()
    real = s.api.next_reply

    def held(body: dict) -> dict | None:
        if A in json.dumps((body.get("messages") or [None])[0], ensure_ascii=False):
            gate.wait(10)  # 甲的第一个请求先停在这里，等测试换去看别的
        return real(body)

    s.api.next_reply = held
    a = viewing(s)
    send(s, A + "第一句")
    until(lambda: len(s.api.requests) == 1, "甲的第一个请求到了假 API")
    new_chat(s)  # 甲在跑：留在后台
    gate.set()
    mark = seen(s.browser)
    failed = s.browser.events[wait_new(s.browser, "state", mark, check=lambda event: event["live"].get(a) == "error")]
    session = alive(s, a)  # 没存盘：还活着
    assert not session.running and not session.chat.conversation.saved and not (s.config.conversations_dir / f"{a}.json").exists()
    assert (a, A + "第一句") in [(item["id"], item["title"]) for item in failed["conversations"]]

    # 打开它：看得到那条消息和出错的原因；"出错了"的标记去掉
    _, replayed, state = open_chat(s, a)
    assert texts(replayed, "user") == [A + "第一句"] and "甲的请求不对" in dumps(replayed)
    assert a not in state["live"]
    # 再发一次（重新生成）：这次答了，存进它自己的文件
    retry = {"conversation": a, "turn": state["rewind"]["turn"]}
    start_at = seen(s.browser)
    post_ok(s.browser, "/api/turn/retry", retry)
    wait_new(s.browser, "turn_end", start_at)
    assert texts(saved_file(s.config, a)["events"], "text") == ["甲这次答了"]


# ---- 3. 换回正在后台跑着的那一段 --------------------------------------------------------


def test_switching_back_to_a_running_conversation_replays_it_and_then_streams_the_rest(start):
    s = start({A: [ask(), reply(text("甲做完了"))]})
    a = viewing(s)
    card = pause(s, A + "跑一条命令")
    new_chat(s)

    mark = seen(s.browser)
    reset, replayed, state = open_chat(s, a)
    # A 是一开始的那一段，日志不是从 reset 开始的：服务器替它补了一个不带编号的（浏览器记着的还是最后一个真实的编号）
    assert s.browser.events[mark] is reset and reset == {"type": "reset"}
    # 从头重放：还在等的那张卡片也在里面，它没有对应的 confirm_done
    assert kinds(replayed) == ["user", "turn_start", "tool_call", "confirm"]
    assert replayed[-1] == card
    assert state["conversation"] == a and a in state["live"]  # 正看着的那一段也在 live 里（页面不给它画标记）
    assert s.web.sessions.viewed.id == a and len(s.web.sessions.all()) == 1  # 那段空的新对话没在跑：丢掉了

    # 页面上的确认卡片照样能答，只到 A；之后的流式输出接着实时来，最后是状态、再是 turn_end
    mark = seen(s.browser)
    answer(s, card["id"])
    end = wait_new(s.browser, "turn_end", mark)
    live = s.browser.events[mark : end + 1]
    assert [kind for kind in kinds(live) if kind != "state"] == ["confirm_done", "tool_result", "text", "block_end", "trace", "turn_end"]
    assert kinds(live)[-2:] == ["state", "turn_end"]
    assert live[0]["confirm"] == card["id"] and live[0]["allowed"] is True
    assert texts(live, "text") == ["甲做完了"]
    assert outcome(saved_file(s.config, a)) == ["allowed"]


# ---- 4. 上限 ----------------------------------------------------------------------


def test_at_most_max_parallel_chats_run_at_once_and_retrying_counts_too(start):
    s = start(
        {
            C: [reply(text("丙一")), reply(text("丙二")), reply(text("丙三"))],
            A: [ask(), reply(text("甲做完了"))],
            B: [ask(), reply(text("乙做完了"))],
        },
        max_parallel_chats=2,
    )
    c = talk(s, C + "第一句")["conversation"]
    a = new_chat(s)["conversation"]
    card_a = pause(s, A + "跑一条命令")
    b = new_chat(s)["conversation"]
    card_b = pause(s, B + "跑一条命令")

    _, _, state = open_chat(s, c)
    assert state["parallel"] == {"max": 2, "running": 2} and state["live"] == {a: "confirm", b: "confirm"}
    status, body = s.browser.post("/api/message", {"text": "第二句"})
    assert status == 409 and "上限" in body["error"]
    # 重新生成也会开始新的一轮：同样被拒绝，而且什么都没倒回去
    retry = {"conversation": c, "turn": state["rewind"]["turn"]}
    status, body = s.browser.post("/api/turn/retry", retry)
    assert status == 409 and "上限" in body["error"]
    viewed = s.web.sessions.viewed
    assert texts(log_of(viewed), "text") == ["丙一"] and len(viewed.chat.agent.messages) == 2
    assert s.web.claims.holder(c) is None and not viewed.running and not viewed.lock.locked()  # 被拒绝的没有认领、没有拿着锁
    assert len(s.api.requests) == 3

    # 一段结束了，就能开始了
    mark = seen(s.browser)
    answer(s, card_a["id"])
    gone(s, a)
    wait_new(s.browser, "state", mark, check=lambda event: event["parallel"] == {"max": 2, "running": 1})
    until(lambda: not viewed.lock.locked(), "C 的锁放开")
    mark = seen(s.browser)
    post_ok(s.browser, "/api/turn/retry", retry)
    _, live = replay_then_turn(s.browser, mark)
    assert texts(live, "text") == ["丙二"]
    until(lambda: not viewed.lock.locked(), "C 的锁放开")
    talk(s, "第二句")
    assert texts(log_of(viewed), "text") == ["丙二", "丙三"]

    answer(s, card_b["id"])
    gone(s, b)


# ---- 5. 同一段不能同时跑两轮 --------------------------------------------------------


def test_the_same_conversation_never_runs_two_turns_at_once(start):
    s = start({A: [ask(), reply(text("甲做完了"))]})
    a = viewing(s)
    card = pause(s, A + "跑一条命令")
    status, body = s.browser.post("/api/message", {"text": "再来一句"})
    assert status == 409 and "上一轮对话还没有结束" in body["error"]

    # 留在后台、再换回来看它，也一样：发消息、重新生成都不行
    new_chat(s)
    _, replayed, _ = open_chat(s, a)
    status, body = s.browser.post("/api/message", {"text": "再来一句"})
    assert status == 409 and "上一轮对话还没有结束" in body["error"]
    user = next(event for event in replayed if event["type"] == "user")
    status, body = s.browser.post("/api/turn/retry", {"conversation": a, "turn": user["id"]})
    assert status == 409 and "上一轮对话还没有结束" in body["error"]
    assert len(s.api.requests) == 1 and alive(s, a).ui.waiting_on(card["id"])

    mark = seen(s.browser)
    answer(s, card["id"])
    wait_new(s.browser, "turn_end", mark)
    assert len(s.api.requests) == 2


# ---- 6. 后台的确认请求 --------------------------------------------------------------


def test_a_confirmation_in_the_background_is_marked_and_only_its_own_id_answers_it(start):
    two = reply(
        tool_use("toolu_1", "run_command", {"command": "echo 1"}),
        tool_use("toolu_2", "run_command", {"command": "echo 2"}),
        stop_reason="tool_use",
    )
    s = start({A: [two, reply(text("甲做完了"))], B: [ask(), reply(text("乙做完了"))]})
    a = viewing(s)
    first = pause(s, A + "跑两条命令")
    b = new_chat(s)["conversation"]
    card_b = pause(s, B + "跑一条命令")
    session_a, session_b = alive(s, a), alive(s, b)
    # 页面收到的状态里 A 标着"等你确认"。B 正看着：它开始等的时候不另发状态（卡片就在眼前，页面也不给它画标记），
    # 此刻算出来的才是 confirm
    assert latest_state(s.browser)["live"][a] == "confirm"
    assert s.web.sessions.live() == {a: "confirm", b: "confirm"}

    # 编号是全局唯一的：不对的编号谁都不收
    assert s.browser.post("/api/confirm", {"id": max(first["id"], card_b["id"]) + 99, "allow": True})[0] == 409
    assert session_a.ui.waiting_on(first["id"]) and session_b.ui.waiting_on(card_b["id"])

    # 答的是后台 A 的那张：只到 A。A 往下走，第一条命令跑完，又停在第二张卡片上；
    # 会话栏上的标记跟着变（在跑 → 等你确认），是 A 开始等、等完时自己发的状态（WebUI.on_waiting）
    mark = seen(s.browser)
    answer(s, first["id"])
    running = wait_new(s.browser, "state", mark, check=lambda event: event["live"].get(a) == "running")
    waiting = s.browser.events[wait_new(s.browser, "state", running + 1, check=lambda event: event["live"].get(a) == "confirm")]
    assert waiting["live"][b] == "confirm" and waiting["conversation"] == b
    second = [event for event in log_of(session_a) if event["type"] == "confirm"][-1]
    assert second["id"] != first["id"] and session_a.ui.waiting_on(second["id"])
    assert session_b.ui.waiting_on(card_b["id"])  # B 的卡片还在等
    # 事件不串：A 的第二张卡片不在 B 的页面上，B 的卡片也不在 A 的日志里
    assert second["id"] not in [event.get("id") for event in s.browser.events]
    assert card_b["id"] not in [event["id"] for event in log_of(session_a)]
    assert s.browser.post("/api/confirm", {"id": first["id"], "allow": True})[0] == 409  # 答复过了

    answer(s, second["id"], allow=False)
    gone(s, a)
    assert outcome(saved_file(s.config, a)) == ["allowed", "denied"]
    assert session_b.ui.waiting_on(card_b["id"])
    mark = seen(s.browser)
    answer(s, card_b["id"])
    wait_new(s.browser, "turn_end", mark)


# ---- 7. 停止只停那一段 --------------------------------------------------------------


def test_stop_only_stops_the_conversation_it_names(start):
    s = start({A: [ask(), reply(text("不该到这里"))], B: [ask(), reply(text("不该到这里"))]})
    a = viewing(s)
    pause(s, A + "跑一条命令")
    b = new_chat(s)["conversation"]
    card_b = pause(s, B + "跑一条命令")
    session_a, session_b = alive(s, a), alive(s, b)

    assert s.browser.post("/api/stop", {"conversation": 3})[0] == 400
    assert s.browser.post("/api/stop", {"conversation": MISSING})[0] == 409  # 没有活着的这一段

    # 停后台的 A：只有 A 结束（卡片记着"已中断"），B 还在等
    mark = seen(s.browser)
    assert s.browser.post("/api/stop", {"conversation": a}) == OK
    gone(s, a)
    # 从名单里拿掉在 turn_end 之前（见 sessions_api._settle），所以再等它的日志收尾
    until(lambda: kinds(log_of(session_a))[-1] == "turn_end", "A 的日志以 turn_end 收尾")
    done = [event for event in log_of(session_a) if event["type"] == "confirm_done"]
    assert [(event["allowed"], event["stopped"]) for event in done] == [(False, True)]
    assert session_b.ui.waiting_on(card_b["id"]) and session_b.running
    # 你自己停下的：会话栏上不标"跑完了"
    wait_new(s.browser, "state", mark, check=lambda event: a not in event["live"])
    assert outcome(saved_file(s.config, a)) == ["stopped"]
    assert s.browser.post("/api/stop", {"conversation": a})[0] == 409  # 已经不在了

    # 不带 conversation：停你正看着的 B
    mark = seen(s.browser)
    assert s.browser.post("/api/stop", {}) == OK
    wait_new(s.browser, "turn_end", mark)
    assert s.browser.events[wait_new(s.browser, "confirm_done", mark)]["stopped"] is True
    assert outcome(saved_file(s.config, b)) == ["stopped"]
    assert len(s.api.requests) == 2  # 停下之后谁都没再调用模型


def test_stop_ends_only_the_command_running_in_that_conversations_own_shell(start):
    def slow(name: str) -> list[dict]:
        return [reply(tool_use("toolu_1", "run_command", {"command": f"sleep 30; touch {name}.txt"}), stop_reason="tool_use")]

    s = start({A: slow("late_a"), B: slow("late_b")})
    post_ok(s.browser, "/api/approval", {"mode": "all"})  # 不问：命令直接跑起来
    a = viewing(s)
    send(s, A + "等一会儿")
    shell_a = alive(s, a).chat.shell
    until(lambda: shell_a._running is not None, "A 的命令跑起来")
    b = new_chat(s)["conversation"]
    send(s, B + "也等一会儿")
    session_b = alive(s, b)
    shell_b = session_b.chat.shell
    until(lambda: shell_b._running is not None, "B 的命令跑起来")
    assert shell_a is not shell_b  # 各用各的 shell

    began = time.monotonic()
    assert s.browser.post("/api/stop", {"conversation": a}) == OK
    gone(s, a)
    assert time.monotonic() - began < 5
    assert shell_a._running is None and shell_b._running is not None and session_b.running  # B 的命令还在跑
    (result,) = saved_file(s.config, a)["messages"][-1]["content"]
    assert result["is_error"] is True and "被用户停止" in result["content"]

    mark = seen(s.browser)
    assert s.browser.post("/api/stop") == OK
    wait_new(s.browser, "turn_end", mark)
    assert shell_b._running is None
    assert not any((s.config.workspace / name).exists() for name in ("late_a.txt", "late_b.txt"))


# ---- 8. 权限的信任不串 --------------------------------------------------------------


def test_trusting_screenshots_on_one_card_does_not_carry_over_to_another_conversation(start, monkeypatch):
    monkeypatch.setattr(desktop_session, "DESKTOP_SETTLE_SECONDS", 0)
    s = start({A: [screenshot("a1", "a2"), reply(text("甲看完了"))], B: [screenshot("b1", "b2"), reply(text("乙看完了"))]})
    install(s.app.desktop, FakeDesktop())
    post_ok(s.browser, "/api/computer/settings", {"enabled": True, "acknowledge": True})
    a = viewing(s)
    card_a = pause(s, A + "看看屏幕")
    b = new_chat(s)["conversation"]
    card_b = pause(s, B + "也看看屏幕")
    assert card_a["offer"] == card_b["offer"] == "screenshots"
    session_a, session_b = alive(s, a), alive(s, b)
    # 电脑和浏览器的 session 各有一个："这段对话里截图不再问"、允许过的网站都只记在自己的上面
    assert session_a.chat.desktop is not session_b.chat.desktop
    assert session_a.chat.browser is not session_b.chat.browser
    assert session_a.chat.browser.allowed_sites is not session_b.chat.browser.allowed_sites

    # 在 B 的卡片上点"这段对话里截图不再问"：只放开 B 的
    assert s.browser.post("/api/computer/trust", {"what": "screenshots", "confirm": card_b["id"]}) == OK
    assert session_b.chat.desktop.trust_screenshots is True
    assert session_a.chat.desktop.trust_screenshots is False and session_a.chat.desktop.trust_actions is False
    # B 的第二张截图不再问（标着 trusted），一轮跑完、让出屏幕
    mark = seen(s.browser)
    answer(s, card_b["id"])
    end = wait_new(s.browser, "turn_end", mark)
    turn = s.browser.events[mark:end]
    assert "confirm" not in kinds(turn) and [event["why"] for event in turn if event["type"] == "auto_allowed"] == ["trusted"]
    assert [event["is_error"] for event in turn if event["type"] == "tool_result"] == [False, False]

    # A 照样每一张都问：第一张答了以后，第二张又停在确认卡片上
    answer(s, card_a["id"])
    until(lambda: len([event for event in log_of(session_a) if event["type"] == "confirm"]) == 2, "A 的第二张截图也来问")
    second = [event for event in log_of(session_a) if event["type"] == "confirm"][-1]
    assert session_a.ui.waiting_on(second["id"]) and "auto_allowed" not in kinds(log_of(session_a))
    answer(s, second["id"])
    gone(s, a)
    assert outcome(saved_file(s.config, a)) == ["allowed", "allowed"]


# ---- 9. 屏幕只有一个 ----------------------------------------------------------------


def test_only_one_conversation_operates_the_screen_until_its_turn_ends(start, monkeypatch):
    monkeypatch.setattr(desktop_session, "DESKTOP_SETTLE_SECONDS", 0)
    shoot_then_ask = reply(
        tool_use("a1", "screenshot", {}, toolset="computer"),
        tool_use("toolu_2", "run_command", {"command": "echo hi"}),
        stop_reason="tool_use",
    )
    s = start({
        A: [shoot_then_ask, reply(text("甲做完了"))],
        B: [screenshot("b1"), reply(text("电脑正忙")), screenshot("b2"), reply(text("这回看到了"))],
    })  # fmt: skip
    install(s.app.desktop, FakeDesktop())
    post_ok(s.browser, "/api/computer/settings", {"enabled": True, "acknowledge": True})
    a = viewing(s)
    shot = pause(s, A + "截个图再跑命令")
    mark = seen(s.browser)
    answer(s, shot["id"])
    command = s.browser.events[wait_new(s.browser, "confirm", mark)]  # A 截好了图（占着屏幕），停在命令的确认上
    desktop_a = alive(s, a).chat.desktop

    b = new_chat(s)["conversation"]
    desktop_b = alive(s, b).chat.desktop
    assert not s.app.desktop.free_for(desktop_b) and s.app.desktop.free_for(desktop_a)
    # B 要截图：屏幕正被 A 占着，不问你，直接回答模型"正被……操作着"
    mark = seen(s.browser)
    send(s, B + "也截个图")
    end = wait_new(s.browser, "turn_end", mark)
    turn = s.browser.events[mark:end]
    assert "confirm" not in kinds(turn)
    (result,) = [event for event in turn if event["type"] == "tool_result"]
    assert result["is_error"] is True and "正被你的对话操作着" in result["text"]

    # A 这一轮结束、让出屏幕以后，B 就能用了
    answer(s, command["id"], allow=False)
    gone(s, a)
    assert s.app.desktop.free_for(desktop_b)
    until(lambda: not s.web.sessions.viewed.lock.locked(), "B 的锁放开")
    card = pause(s, "再截一次")
    mark = seen(s.browser)
    answer(s, card["id"])
    end = wait_new(s.browser, "turn_end", mark)
    (result,) = [event for event in s.browser.events[mark:end] if event["type"] == "tool_result"]
    assert result["is_error"] is False and result["thumb"].startswith("data:image/jpeg;base64,")


# ---- 10. 撤回和倒回在两段之间互不影响 ------------------------------------------------------


def test_undo_and_rewind_stay_inside_their_own_conversation(start):
    def create(n: int, path: str, body: str) -> dict:
        return tool_use(f"toolu_{n}", EDIT, {"command": "create", "path": path, "file_text": body})

    s = start({
        A: [reply(create(1, "a.txt", "甲\n"), tool_use("toolu_2", "run_command", {"command": "echo hi"}), stop_reason="tool_use"),
            reply(text("甲改好了"))],
        B: [reply(create(1, "b.txt", "乙\n"), stop_reason="tool_use"), reply(text("乙改好了"))],
    })  # fmt: skip
    post_ok(s.browser, "/api/approval", {"mode": "edits"})  # 改工作目录里的文件不问，命令照样问
    work = s.config.workspace
    a = viewing(s)
    card = pause(s, A + "建个文件再跑命令")  # a.txt 已经建好了，A 停在命令的确认上
    assert (work / "a.txt").exists()
    a_user = next(event for event in log_of(alive(s, a)) if event["type"] == "user")["id"]

    # A 停着的时候，B 改它自己的文件；这一轮结束那一行只列着 B 改的
    b = new_chat(s)["conversation"]
    mark = seen(s.browser)
    send(s, B + "也建个文件")
    end = wait_new(s.browser, "turn_end", mark)
    (line_b,) = [event for event in s.browser.events[mark:end] if event["type"] == "file_changes"]
    assert [item["path"] for item in line_b["files"]] == ["b.txt"] and "commands" not in line_b
    until(lambda: not s.web.sessions.viewed.lock.locked(), "B 的锁放开")

    # 不是你正看着的那一段：重新生成、撤回都对不上（409），A 一点都没被动
    status, body = s.browser.post("/api/turn/retry", {"conversation": a, "turn": a_user})
    assert status == 409 and "对话刚刚变过了" in body["error"]
    status, body = s.browser.post("/api/changes/undo", {"conversation": a, "set": line_b["set"]})
    assert status == 409 and "不在现在开着的这段对话里" in body["error"]
    assert alive(s, a).ui.waiting_on(card["id"]) and (work / "a.txt").exists()

    answer(s, card["id"], allow=False)
    gone(s, a)
    (line_a,) = [event for event in saved_file(s.config, a)["events"] if event["type"] == "file_changes"]
    assert [item["path"] for item in line_a["files"]] == ["a.txt"] and line_a["commands"] is True  # 各记各的账
    assert line_a["set"] != line_b["set"]

    # 在 B 里撤回：只撤 B 那一轮的；A 那一轮的编号在 B 里找不到
    status, body = s.browser.post("/api/changes/undo", {"conversation": b, "set": line_a["set"]})
    assert status == 409 and body["error"] == "找不到这一轮的改动记录。看一眼最新的内容再试。"
    assert s.browser.post("/api/changes/undo", {"conversation": b, "set": line_b["set"]}) == (200, {"ok": True, "asks": [], "blocked": []})
    assert not (work / "b.txt").exists() and (work / "a.txt").read_text(encoding="utf-8") == "甲\n"

    # 打开 A，撤回它自己的
    open_chat(s, a)
    assert s.browser.post("/api/changes/undo", {"conversation": a, "set": line_a["set"]}) == (200, {"ok": True, "asks": [], "blocked": []})
    assert not (work / "a.txt").exists()


# ---- 11. 退出 ----------------------------------------------------------------------


def test_quitting_stops_and_saves_every_conversation(start):
    s = start({A: [ask(), reply(text("不该到这里"))], B: [ask(), reply(text("不该到这里"))]})
    a = viewing(s)
    pause(s, A + "跑一条命令")  # A 会留在后台，停在确认上
    b = new_chat(s)["conversation"]
    pause(s, B + "也跑一条")  # B 正看着，也停在确认上

    lifecycle.stop(s.web, 5)

    assert s.web.sessions.running() == 0 and not any(session.running for session in s.web.sessions.all())
    assert s.web.sessions.find(a) is None  # A 在后台、存好了盘：拿掉了
    for conversation_id, words in ((a, A + "跑一条命令"), (b, B + "也跑一条")):
        saved = saved_file(s.config, conversation_id)
        assert texts(saved["events"], "user") == [words] and outcome(saved) == ["stopped"]
        (result,) = saved["messages"][-1]["content"]
        assert result["type"] == "tool_result" and result["is_error"] is True  # 每个工具调用都有结果，以后还能接着聊
    assert len(s.api.requests) == 2


# ---- 12. 聊天通道占着的 ------------------------------------------------------------


def test_a_conversation_held_by_a_chat_channel_cannot_be_opened_and_a_background_turn_keeps_its_claim(start):
    s = start({B: [reply(text("乙答"))], A: [ask(), reply(text("甲做完了"))]})
    b = talk(s, B + "第一句")["conversation"]
    a = new_chat(s)["conversation"]
    card = pause(s, A + "跑一条命令")
    assert s.web.claims.holder(a) == "web"
    new_chat(s)  # A 留在后台
    assert s.web.claims.holder(a) == "web"  # 在后台跑着也一直认领着：聊天通道这时不能往里写

    assert s.web.claims.take(b, "telegram:1") is None  # Telegram 那边正在 B 里跑一轮
    try:
        status, body = s.browser.post("/api/conversation/open", {"id": b})
        assert status == 409 and "Telegram" in body["error"]
        assert s.web.sessions.find(b) is None and s.web.sessions.viewed.id not in (a, b)
    finally:
        s.web.claims.release(b, "telegram:1")

    answer(s, card["id"])
    gone(s, a)
    assert s.web.claims.holder(a) is None  # 跑完放开
    _, replayed, _ = open_chat(s, b)  # 那边放开以后就能打开了
    assert texts(replayed, "text") == ["乙答"]


# ---- 13. 上限的检查：去掉锁就会失败 ----------------------------------------------------


class SlowToRead(Session):
    """读 running 时慢一点：去掉 Sessions.start 里的锁，"数一数在跑的有几段"和"记下这一段也在跑"之间就会挤进别的线程，
    十几段同时开始时就不止 limit 段了。"""

    @property
    def running(self) -> bool:
        time.sleep(0.001)
        return self._running

    @running.setter
    def running(self, value: bool) -> None:
        self._running = value


def test_the_limit_holds_when_many_conversations_start_at_the_same_moment():
    many = [SlowToRead(SimpleNamespace(conversation=SimpleNamespace(id=f"c{n}", saved=True)), WebUI()) for n in range(15)]
    sessions = Sessions(many[0])
    for session in many[1:]:
        sessions.add(session)
    barrier = threading.Barrier(len(many))
    started: list[Session] = []

    def go(session: Session) -> None:
        barrier.wait()
        if sessions.start(session, limit=3):
            started.append(session)

    threads = [threading.Thread(target=go, args=(session,), name=f"start-{n}") for n, session in enumerate(many)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(started) == 3 and sessions.running() == 3
    assert {id(session) for session in started} == {id(session) for session in many if session._running}
    # 一段结束了，就又能开始一段（只能一段）
    sessions.finish(started[0], "")
    first, second = [session for session in many if session not in started][:2]
    assert sessions.start(first, limit=3) and not sessions.start(second, limit=3)


# ---- 14. 从备份恢复 -----------------------------------------------------------------


def test_restoring_a_backup_names_the_running_conversations_and_starts_over_with_one(start, temp_dir):
    s = start({A: [ask(), reply(text("甲做完了"))], B: [ask(), reply(text("乙做完了"))]})
    stage = test_restore_web.staged(s)
    old = s.server.web
    card_a = pause(s, A + "跑一条命令")
    new_chat(s)  # A 留在后台
    blocked = post_ok(s.browser, "/api/restore/info")["blocked"]
    assert "「【甲】跑一条命令」正在进行一轮对话" in blocked
    card_b = pause(s, B + "也跑一条")
    blocked = post_ok(s.browser, "/api/restore/info")["blocked"]
    assert "「【甲】跑一条命令」等 2 段对话正在进行" in blocked
    status, body = s.browser.post("/api/restore/apply", {"stage": stage, "mode": "merge", "confirm": True})
    assert status == 409 and body["error"] == blocked and s.server.web is old

    answer(s, card_a["id"])
    answer(s, card_b["id"])
    assert old.wait_idle(10)
    until(lambda: not old._turn.locked(), "每一段的锁都放开")
    assert post_ok(s.browser, "/api/restore/info")["blocked"] == ""
    mark = seen(s.browser)
    post_ok(s.browser, "/api/restore/apply", {"stage": stage, "mode": "merge", "confirm": True})
    fresh = s.server.web
    assert fresh is not old and len(fresh.sessions.all()) == 1 and fresh.sessions.viewed.ui is old.ui
    # 页面接着读同一份日志（不用重连），收到新的 WebApp 发的 reset 和状态
    _, _, state = switched(s.browser, mark)
    assert state["parallel"] == {"max": 3, "running": 0}


# ---- 15. 刷新页面、断线重连 --------------------------------------------------------------


def test_reloading_the_page_shows_only_the_viewed_conversation_and_reconnecting_never_gets_half_a_log(start):
    s = start({A: [ask(), reply(text("甲做完了"))], B: [reply(text("乙答"))]})
    a = viewing(s)
    card = pause(s, A + "跑一条命令")
    new_chat(s)
    talk(s, B + "第一句")
    viewed = s.web.sessions.viewed

    # 刷新页面：从头补发正看着的 B 的日志（过时的状态略去），A 的内容一点都没有；会话栏上 A 标着"等你确认"
    reloaded = Browser(s.url)
    reloaded.wait_synced()
    assert reloaded.events == latest_state_only(log_of(viewed))
    assert kinds(reloaded.events)[0] == "reset" and card["id"] not in [event["id"] for event in reloaded.events]
    # （状态里的会话栏列着 A 的标题，那是该有的；别的事件里一点 A 的内容都没有）
    assert A not in dumps([event for event in reloaded.events if event["type"] != "state"])
    assert latest_state(reloaded)["live"][a] == "confirm"

    # 断线重连，报上来的是上一段（A）的编号：比 B 被看着的起点小，从头补发（B 的日志本来就从它的 reset 开始），不会只收到后半截
    resumed = Browser(s.url, last_event_id=card["id"])
    resumed.wait_synced()
    assert resumed.events == latest_state_only(log_of(viewed))

    # 换回 A（日志不是从 reset 开始的），拿 B 的编号重连：先收到一个不带编号的 reset，再从头补发 A 的日志
    b_last = s.browser.events[-1]["id"]
    open_chat(s, a)
    again = Browser(s.url, last_event_id=b_last)
    again.wait_synced()
    assert again.events[0] == {"type": "reset"}
    assert again.events[1:] == latest_state_only(log_of(s.web.sessions.viewed))
    assert card in again.events and "confirm_done" not in kinds(again.events)
    assert again.last_event_id == str(again.events[-1]["id"])  # 浏览器记着的是最后一个真实的编号


# ---- 16. 几个标签页 ----------------------------------------------------------------


def test_every_open_tab_follows_the_switch_to_another_conversation(start):
    s = start({A: [ask(), reply(text("甲做完了"))]})
    a = viewing(s)
    card = pause(s, A + "跑一条命令")
    new_chat(s)
    other = Browser(s.url)  # 另一个标签页
    other.wait_synced()

    first, second = seen(s.browser), seen(other)
    post_ok(s.browser, "/api/conversation/open", {"id": a})
    assert switched(s.browser, first) == switched(other, second)
    assert s.browser.events[first] == other.events[second] == {"type": "reset"}

    # 之后的实时事件两个都收到，一样的
    answer(s, card["id"])
    wait_new(s.browser, "turn_end", first)
    wait_new(other, "turn_end", second)
    assert s.browser.events[first:] == other.events[second:]
    assert texts(other.events[second:], "text") == ["甲做完了"]
