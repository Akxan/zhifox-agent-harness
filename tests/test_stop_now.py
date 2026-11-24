"""点"停止"马上生效，模型一个字都不发的时候也一样（harness/providers/inflight.py）。

以前的"停止"要等模型再发来一点东西才生效：模型在想、在写一个长长的工具参数、请求还在排队的时候，按下去没反应。
这里让假服务器收下请求以后先压着不回答（tests/held.py）：发了回答的头、一个事件都不发；或者连头都不发。
然后从每一个能停下的地方停：网页的 /api/stop、终端的 Ctrl-C、定时任务的停止、聊天通道的 /stop、子 Agent、退出。
每次都看：这一轮在 BOUND 秒之内结束、假服务器看到连接断了、历史和事件和"被中断"一样、没有重试也没有换备用模型、
账本不记这一步、下一轮照常。最后是"停止"正好撞上回答结束的那一下。

客户端分两种：智狐自己建的（providers.make_client、endpoints/client.py，装着 inflight 的钩子），连回答的头还没到的请求
也能马上断开；测试里自己建的（没有钩子），头到了以后才找得到连接。两种都测。
"""

from __future__ import annotations

import json
import signal
import socket
import sys
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import anthropic
import pytest
import test_endpoints_chat
from fake_api import FakeAPI, reply, text, tool_use
from fake_openai import FakeOpenAI, chat
from held import held
from test_agents import delegate, install
from test_channels import bot  # noqa: F401  fixture
from test_task_runs import agent_task, finished
from test_task_runs import serve as task_serve  # noqa: F401  fixture（换个名字：和网页的那个同名）
from test_webui import last_results
from test_webui import serve as web_serve  # noqa: F401  fixture

from harness import console
from harness.app import build_app, run_turn
from harness.endpoints import EndpointStore
from harness.providers import find, inflight, make_client
from harness.settings import Settings
from harness.ui import TerminalUI
from harness.webui import lifecycle
from harness.webui.ui import WebUI

# 点了"停止"以后最多等多久：本机上是几十毫秒。以前要等到模型再开口（这里的假服务器永远不开口），
# 所以只要不是"等下去"就能分出来；留得宽一些给 GitHub 上忙的机器
BOUND = 2.0
SLOW = 30  # 等别的事（请求到了、一轮正常结束）最多等多久：只是防止卡死，等到了马上往下走
SONNET = "claude-sonnet-5-5"


# ---- 共用 ---------------------------------------------------------------------


@pytest.fixture
def rig(make_config, monkeypatch):
    """一个用网页的界面（WebUI：停止的旗子、on_stop 都是正式的）跑一轮的智狐，后面连着假的 Claude API。

    hooks=True：客户端用正式的 providers.make_client 建（装着钩子，连到假 API 靠 ANTHROPIC_BASE_URL）；
    False：测试自己建一个普通的。keep_alive：假 API 按 HTTP/1.1 回答，连接留给下一个请求（真的 API 就是这样）。"""
    started: list[FakeAPI] = []

    def start(*replies, hooks: bool = True, keep_alive: bool = True, **config_overrides):
        api = FakeAPI(list(replies), keep_alive=keep_alive).start()
        started.append(api)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", api.base_url)
        config = make_config(**config_overrides)
        ui = WebUI()

        def plain(provider):
            return anthropic.Anthropic(api_key="test-key", base_url=api.base_url, max_retries=0)

        app = build_app(config, ui, client_factory=make_client if hooks else plain)
        app.agent.clock = lambda: datetime(2026, 11, 24, 9, 0)
        return SimpleNamespace(app=app, agent=app.agent, api=api, ui=ui, config=config)

    yield start
    for api in started:
        api.stop()


def begin(s, words: str) -> threading.Thread:
    """像网页上发一条消息那样跑一轮（chat_api 也是这几步：begin_turn 放下旗子、记下位置，run_turn 接住中断），在另一个线程里。"""

    def work() -> None:
        s.ui.begin_turn(words, mark=s.agent.backend.mark())
        run_turn(s.agent, s.ui, words)
        s.ui.end_turn()

    thread = threading.Thread(target=work, name="turn", daemon=True)
    thread.start()
    return thread


def finish(thread: threading.Thread) -> None:
    thread.join(SLOW)
    assert not thread.is_alive(), "这一轮没有结束"


def stopped_quickly(thread: threading.Thread, stop, hold, hangups: int = 1) -> float:
    """按下 stop，返回这一轮过了多久结束。BOUND 秒之内没结束、假服务器没看到连接断开，都算失败。"""
    began = time.monotonic()
    stop()
    thread.join(BOUND)
    took = time.monotonic() - began
    assert not thread.is_alive(), f"点了停止 {BOUND} 秒，这一轮还没结束"
    assert hold.wait_hung_up(hangups, BOUND), "假服务器没有看到连接断开（服务器那边还在接着生成、计费）"
    return took


def types(ui: WebUI) -> list[str]:
    return [event["type"] for event in ui.events_after(0, timeout=0)]


def notices(ui: WebUI) -> list[str]:
    return [event.get("text", "") for event in ui.events_after(0, timeout=0) if event["type"] == "notice"]


def ledger(config) -> list[dict]:
    path = config.data_dir / "ledger.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def last_turn(ui: WebUI) -> list[dict]:
    """日志里最后一轮的事件（从最后一个 user 事件起）。"""
    return ui.last_turn_events()[1]


def interrupted_cleanly(s, before: int) -> None:
    """和以前在界面上看到旗子时停下一样：模型一步都没走完，这条用户消息撤回；页面上写"已中断。"；运行记录标着 stopped。"""
    assert s.agent.backend.mark() == before
    events = last_turn(s.ui)
    assert [event.get("text") for event in events if event["type"] == "notice"][-1] == "已中断。"
    assert [event["ended"] for event in events if event["type"] == "trace"] == ["stopped"]
    assert events[-1]["type"] == "turn_end"


def answered_normally(s) -> None:
    events = last_turn(s.ui)
    assert "已中断。" not in [event.get("text") for event in events if event["type"] == "notice"]
    assert [event["ended"] for event in events if event["type"] == "trace"] == ["done"]


# ---- 一、Claude：头发了 / 头没发 / 复用连接 / 没有钩子 -----------------------------------


@pytest.mark.parametrize("headers", [True, False], ids=["silent-after-headers", "no-headers"])
def test_stop_while_claude_is_silent_takes_effect_at_once(rig, headers):
    s = rig(held(reply(text("不会发出来")), headers=headers), reply(text("好的，换个话题。")))
    s.app.settings.set_fallbacks("anthropic", [SONNET])  # 有备用模型：停下以后也不该去试它
    thread = begin(s, "想一个很难的问题")
    assert s.api.hold.wait_held()

    stopped_quickly(thread, s.ui.request_stop, s.api.hold)

    interrupted_cleanly(s, before=0)
    assert len(s.api.requests) == 1  # 没有重试（SDK 的、我们自己的），也没有换备用模型
    assert all(request["model"] != SONNET for request in s.api.requests)
    assert not any("备用" in text for text in notices(s.ui))
    assert ledger(s.config) == []  # 用量要到回复的最后才知道：被停下的这一步不记账（和以前一样）

    # 下一轮照常：新的一条用户消息、请求的开头和上一次一样（被撤回的那条不在里面）
    finish(begin(s, "那算了"))
    answered_normally(s)
    assert [m["role"] for m in s.api.requests[1]["messages"]] == ["user"]
    assert s.api.requests[1]["messages"][0]["content"][-1]["text"] == "那算了"
    assert [m["role"] for m in s.agent.messages] == ["user", "assistant"]
    assert len(ledger(s.config)) == 1
    # 倒回用的位置也还对得上：最后一条 user 事件的 mark 就是这一轮的开头
    user = [event for event in s.ui.events_after(0, timeout=0) if event["type"] == "user"][-1]
    assert s.agent.backend.turn_at(user["mark"])


@pytest.mark.parametrize("headers", [True, False], ids=["silent-after-headers", "no-headers"])
def test_stop_on_a_reused_connection(rig, headers):
    # 第二步复用第一步的那条连接（keep-alive）：头没到的时候 trace 不说是哪一条，按"没有别人认领的"去断
    s = rig(reply(tool_use("t1", "todo_list", {}), stop_reason="tool_use"), held(reply(text("不会发出来")), headers=headers))
    thread = begin(s, "看看待办")
    assert s.api.hold.wait_held()

    stopped_quickly(thread, s.ui.request_stop, s.api.hold)

    assert s.api.peers[0] == s.api.peers[1]  # 真的是同一条连接
    # 第一步走完了，留在历史里；第二步没走完，什么都没留下。每个工具调用都有结果
    assert [m["role"] for m in s.agent.messages] == ["user", "assistant", "user"]
    assert last_results(s.agent)[0]["tool_use_id"] == "t1"
    assert notices(s.ui)[-1] == "已中断。"
    assert len(s.api.requests) == 2
    assert len(ledger(s.config)) == 1  # 走完的那一步照常记账，停下的那一步不记


def test_without_hooks_a_silent_stream_still_stops_at_once(rig):
    # 测试自己建的客户端没有钩子：回答的头到了以后，适配层拿到连接，照样马上断开
    s = rig(held(reply(text("不会发出来"))), reply(text("好。")), hooks=False)
    thread = begin(s, "想一想")
    assert s.api.hold.wait_held()
    until(lambda: _claims, "适配层拿到连接")  # 没有钩子：头到了、适配层拿到 SDK 的流以后才知道连接是哪一条

    stopped_quickly(thread, s.ui.request_stop, s.api.hold)
    interrupted_cleanly(s, before=0)
    finish(begin(s, "好了"))
    answered_normally(s)
    assert len(s.api.requests) == 2


_claims: list[object] = []  # 适配层认领过的连接（见 watch_claims）


@pytest.fixture(autouse=True)
def watch_claims(monkeypatch):
    """记下适配层认领过的连接（Call.claim）：没有钩子的客户端，要等它拿到连接以后再点停止才有意义。不改它做的事。"""
    _claims.clear()
    real = inflight.Call.claim

    def claim(self, stream):
        if inflight._is_stream(stream):
            _claims.append(stream)
        return real(self, stream)

    monkeypatch.setattr(inflight.Call, "claim", claim)


def test_without_hooks_no_headers_stops_as_soon_as_the_headers_come(rig):
    # 没有钩子、头还没到：找不到连接（这时只能立旗子）。头一到就断开，不读回复、不再请求
    s = rig(held(reply(text("不会发出来")), headers=False), hooks=False)
    thread = begin(s, "想一想")
    assert s.api.hold.wait_held()
    s.ui.request_stop()
    thread.join(0.2)
    assert thread.is_alive()  # 还在等头（这一种得等）
    s.api.hold.release()
    thread.join(BOUND)
    assert not thread.is_alive()
    interrupted_cleanly(s, before=0)
    assert len(s.api.requests) == 1


def test_stop_before_the_request_even_starts_sends_nothing(rig):
    # 停止落在"这一步刚要开始"的那一下：登记的那一刻界面就调用 abort，请求根本不发
    s = rig(reply(text("不会发出来")))
    s.ui.begin_turn("你好")
    s.ui.request_stop()
    with pytest.raises(KeyboardInterrupt):
        s.agent.run_turn("你好")
    assert s.api.requests == [] and s.agent.messages == []


# ---- 二、OpenAI 格式（自定义接口，用正式的 endpoints/client.py 建客户端）--------------------


@pytest.fixture
def openai_rig(make_config):
    servers: list = []

    def start(*replies, context: int | None = None):
        server = FakeOpenAI(list(replies), models=["qwen3:8b"], keep_alive=True).start()
        servers.append(server)
        config = make_config()
        model = {**test_endpoints_chat.QWEN, **({"context": context} if context else {})}
        EndpointStore(config.endpoints_file).save({"name": "Ollama", "base_url": server.base_url + "/v1", "api_key": "", "models": [model]})
        Settings(config.settings_file, "anthropic", "claude-opus-5-5").choose(test_endpoints_chat.PROVIDER, "qwen3:8b")
        ui = WebUI()
        app = build_app(config, ui, client_factory=make_client)
        app.agent.clock = lambda: datetime(2026, 11, 24, 9, 0)
        return SimpleNamespace(app=app, agent=app.agent, api=server, ui=ui, config=config)

    yield start
    for server in servers:
        server.stop()


@pytest.mark.parametrize("headers", [True, False], ids=["silent-after-headers", "no-headers"])
def test_stop_while_an_openai_format_model_is_silent(openai_rig, headers):
    s = openai_rig(held(chat("不会发出来"), headers=headers), chat("好。"))
    thread = begin(s, "想一个很难的问题")
    assert s.api.hold.wait_held()

    stopped_quickly(thread, s.ui.request_stop, s.api.hold)

    assert notices(s.ui)[-1] == "已中断。"
    assert [m["role"] for m in s.agent.messages] == ["system"]  # 这条用户消息撤回了
    assert len(s.api.requests) == 1
    finish(begin(s, "那算了"))
    assert [m["role"] for m in s.agent.messages] == ["system", "user", "assistant"]


def test_stop_while_the_summary_of_a_long_conversation_is_being_written(openai_rig):
    # 对话太长、先请模型写摘要（compaction.py）的那一次请求也是这一步的一部分：停下它不报"摘要没写成"，直接中断
    big = {"prompt_tokens": 9000, "completion_tokens": 10, "total_tokens": 9010}
    s = openai_rig(chat("第一轮", usage=big), held(chat("摘要")), chat("不会走到这里"), context=10000)
    finish(begin(s, "第一句"))
    thread = begin(s, "第二句")
    assert s.api.hold.wait_held()

    stopped_quickly(thread, s.ui.request_stop, s.api.hold)

    assert notices(s.ui)[-1] == "已中断。"
    assert not any("没能把较早的对话压缩成摘要" in text for text in notices(s.ui))
    assert [m["role"] for m in s.agent.messages] == ["system", "user", "assistant"]  # 没压缩、第二句撤回了
    assert len(s.api.requests) == 2


# ---- 三、每一个能停下的地方 ----------------------------------------------------------------


def test_web_stop_button(web_serve):  # noqa: F811
    s = web_serve(held(reply(text("不会发出来"))), reply(text("好。")))
    s.browser.post("/api/message", {"text": "想一想"})
    assert s.api.hold.wait_held()
    s.browser.wait_for("turn_start")
    until(lambda: _claims, "适配层拿到连接")

    began = time.monotonic()
    assert s.browser.post("/api/stop") == (200, {"ok": True})
    s.browser.wait_for("turn_end", timeout=BOUND)
    assert time.monotonic() - began < BOUND
    assert s.api.hold.wait_hung_up(1, BOUND)
    assert s.browser.of_type("notice")[-1]["text"] == "已中断。"
    assert s.app.agent.messages == [] and len(s.api.requests) == 1

    s.browser.post("/api/message", {"text": "好了"})
    s.browser.wait_for("turn_end")
    assert [m["role"] for m in s.app.agent.messages] == ["user", "assistant"]


def test_terminal_ctrl_c_on_macos_and_linux(make_config, monkeypatch):
    # macOS、Linux 上 Ctrl-C 是信号：主线程正卡在等网络，信号当场打断它（这一条以前就是好的，这里守住它）。
    # 一轮在主线程里跑，另一个线程等假服务器压上请求以后，给主线程发 SIGINT
    if not hasattr(signal, "pthread_kill"):
        pytest.skip("Windows 上没有 pthread_kill；那里的 Ctrl-C 走 console.on_ctrl_c，见下一个测试")
    if threading.current_thread() is not threading.main_thread() or signal.getsignal(signal.SIGINT) is not signal.default_int_handler:
        pytest.skip("要在主线程里、用 Python 默认的 Ctrl-C 处理才测得了")
    api = FakeAPI([held(reply(text("不会发出来"))), reply(text("好。"))], keep_alive=True).start()
    try:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", api.base_url)
        ui = TerminalUI()
        app = build_app(make_config(), ui)
        main = threading.main_thread().ident
        assert main is not None
        timings: list[float] = []

        def press_ctrl_c() -> None:
            if api.hold.wait_held():
                timings.append(time.monotonic())
                signal.pthread_kill(main, signal.SIGINT)

        threading.Thread(target=press_ctrl_c, daemon=True).start()
        run_turn(app.agent, ui, "想一想")  # 接住 KeyboardInterrupt，说"已中断。"
        assert timings and time.monotonic() - timings[0] < BOUND
        assert api.hold.wait_hung_up(1, BOUND)
        assert app.agent.messages == [] and len(api.requests) == 1
        run_turn(app.agent, ui, "好了")
        assert [m["role"] for m in app.agent.messages] == ["user", "assistant"]
    finally:
        api.stop()


def test_terminal_ctrl_c_on_windows_breaks_the_connection_from_the_console_thread(make_config, monkeypatch):
    # Windows 上 Ctrl-C 打断不了正在等网络的主线程：TerminalUI.on_stop 登记一个控制台的处理函数（console.on_ctrl_c），
    # 系统在另一个线程里调用它。这里换成假的登记：记下 callback，再像系统那样从另一个线程调用它（三个系统上都能跑）
    registered: list = []

    def fake_on_ctrl_c(callback):
        registered.append(callback)
        return lambda: registered.remove(callback)

    monkeypatch.setattr(console, "on_ctrl_c", fake_on_ctrl_c)
    api = FakeAPI([held(reply(text("不会发出来")), headers=False), reply(text("好。"))], keep_alive=True).start()
    try:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", api.base_url)
        ui = TerminalUI()
        app = build_app(make_config(), ui)
        thread = threading.Thread(target=lambda: run_turn(app.agent, ui, "想一想"), name="turn", daemon=True)
        thread.start()
        assert api.hold.wait_held()

        def console_thread() -> None:  # 系统在一个新的线程里调用处理函数，处理函数调用登记过的每一个
            for callback in registered[:]:
                callback()

        stopped_quickly(thread, lambda: threading.Thread(target=console_thread).start(), api.hold)
        assert registered == []  # 这一步结束就取消了登记
        assert app.agent.messages == [] and len(api.requests) == 1
    finally:
        api.stop()


def test_the_console_handler_itself():
    called: list[str] = []
    handle = console._handler(lambda: called.append("stop"))
    assert handle(console.CTRL_C_EVENT) == 0 and handle(console.CTRL_BREAK_EVENT) == 0  # 0：接着交给 Python 自己的处理函数
    assert handle(2) == 0  # 关窗口这类不是"请停下"
    assert called == ["stop", "stop"]
    broken = console._handler(lambda: 1 / 0)
    assert broken(console.CTRL_C_EVENT) == 0  # 出了错也不带出去（那是系统的线程）
    if sys.platform != "win32":
        assert console.on_ctrl_c(lambda: called.append("x"))() is None  # 别的系统上什么都不登记
    else:
        console.on_ctrl_c(lambda: None)()  # 真的登记、再取消（不真的按 Ctrl-C：会打断整个测试）
    assert called == ["stop", "stop"]


def test_task_stop(task_serve):  # noqa: F811
    s = task_serve(routes={"想一想": [held(reply(text("不会发出来")))]})
    task = agent_task(s, title="慢慢想", prompt="想一想")
    assert s.browser.post("/api/task/run", {"id": task}) == (200, {"ok": True, "queued": False})
    assert s.api.hold.wait_held()
    until(lambda: _claims, "适配层拿到连接")

    began = time.monotonic()
    assert s.browser.post("/api/task/stop", {"id": task}) == (200, {"ok": True})
    finished(s, task, "stopped")
    assert time.monotonic() - began < BOUND + 1  # 收尾还要存盘、记一次运行
    assert s.api.hold.wait_hung_up(1, BOUND)
    assert len(s.api.requests) == 1


def test_chat_channel_stop(bot):  # noqa: F811
    s = bot(held(reply(text("不会发出来"))), reply(text("好。")))
    s.say("想一想")
    assert s.api.hold.wait_held()
    until(lambda: _claims, "适配层拿到连接")

    began = time.monotonic()
    stopping = s.say("/stop")
    s.wait_reply(stopping, "已中断。", timeout=SLOW)
    assert time.monotonic() - began < BOUND + 1  # 还要经过假 Telegram 收发
    assert s.api.hold.wait_hung_up(1, BOUND)
    s.idle()
    assert len(s.api.requests) == 1
    s.wait_reply(s.say("好了"), "好。")  # 下一轮照常


def test_a_subagent_waiting_on_a_silent_model_stops_with_its_parent(rig):
    s = rig(delegate(), held(reply(text("不会发出来"))), reply(text("不会走到这里")))
    install(s)
    thread = begin(s, "交给研究员")
    assert s.api.hold.wait_held()

    stopped_quickly(thread, s.ui.request_stop, s.api.hold)

    # 主助手的这次 agent 调用照样有结果（被中断了），历史合法；子 Agent 的卡片写着 stopped
    results = last_results(s.agent)
    assert results[0]["tool_use_id"] == "t1" and "用户中断了操作" in results[0]["content"]
    done = [event for event in s.ui.events_after(0, timeout=0) if event["type"] == "subagent" and event.get("step") == "done"]
    assert done and done[-1]["status"] == "stopped"
    assert len(s.api.requests) == 2


def test_quitting_breaks_the_silent_request_and_saves(web_serve):  # noqa: F811
    s = web_serve(held(reply(text("不会发出来"))))
    s.browser.post("/api/message", {"text": "想一想"})
    assert s.api.hold.wait_held()
    until(lambda: _claims, "适配层拿到连接")

    began = time.monotonic()
    lifecycle.stop(s.server.web, SLOW)
    assert time.monotonic() - began < BOUND + 1
    assert s.api.hold.wait_hung_up(1, BOUND)
    assert s.server.web.sessions.running() == 0


# ---- 四、停止撞上回答正好结束 ----------------------------------------------------------


def test_stop_racing_a_normal_finish_never_corrupts_anything(rig):
    # 放行（回答马上发完）和停止同时发生：哪个先到都行，但历史、事件、账本、下一轮都不能坏。来回撞二十次
    rounds = 20
    s = rig(*[held(reply(text(f"第 {n} 个回答"))) for n in range(rounds)], reply(text("最后照常。")))
    for n in range(rounds):
        before = s.agent.backend.mark()
        thread = begin(s, f"第 {n} 句")
        assert s.api.hold.wait_held(n + 1)
        go = threading.Barrier(2)

        def release(go: threading.Barrier = go) -> None:
            go.wait()
            s.api.hold.release()

        racer = threading.Thread(target=release)
        racer.start()
        go.wait()
        s.ui.request_stop()
        racer.join()
        finish(thread)
        messages = s.agent.messages
        ended = [event["ended"] for event in last_turn(s.ui) if event["type"] == "trace"]
        if ended == ["stopped"]:
            interrupted_cleanly(s, before)  # 停在前面：这一轮整个撤回
        else:
            # 回答先收完了：它留在历史里（停止之后循环没有别的事要做，这一轮照常答完）
            assert ended == ["done"]
            assert [block.get("text") for block in messages[-1]["content"]] == [f"第 {n} 个回答"]
        assert [m["role"] for m in messages] == ["user", "assistant"] * (len(messages) // 2)
        assert types(s.ui)[-1] == "turn_end"
    finish(begin(s, "还在吗"))
    answered_normally(s)
    answered = sum(1 for m in s.agent.messages if m["role"] == "assistant")
    assert len(ledger(s.config)) == answered  # 记账的正好是答完了的那几步


# ---- 五、inflight 本身 ----------------------------------------------------------------


class FakeStream:
    """httpcore 的 NetworkStream 的样子：get_extra_info("socket") 给一个真的 socket。"""

    def __init__(self) -> None:
        self.sock, self.peer = socket.socketpair()

    def get_extra_info(self, name: str):
        return self.sock if name == "socket" else None

    def cut(self) -> bool:
        """对面是不是读到了连接断开（b""）。"""
        self.peer.settimeout(BOUND)
        try:
            return self.peer.recv(1) == b""
        except OSError:
            return True


def test_abort_only_converts_errors_that_came_after_it():
    call = inflight.Call()
    with pytest.raises(ValueError), call.request():
        raise ValueError("真的出错了")  # 没停下：原样抛出
    call.abort()
    with pytest.raises(inflight.Aborted), call.request():
        pass  # 停下以后：不再发
    assert issubclass(inflight.Aborted, KeyboardInterrupt) and not issubclass(inflight.Aborted, Exception)


def test_abort_cuts_the_claimed_connection_and_a_late_claim_is_cut_at_once():
    call = inflight.Call()
    early, late = FakeStream(), FakeStream()
    with pytest.raises(inflight.Aborted), call.request():
        call.claim(early)
        call.abort()
        assert early.cut()
        call.claim(late)  # 停下以后才知道的连接：马上断开
        assert late.cut()
        raise OSError("读到连接断了")  # 换成 Aborted


def test_after_a_request_finishes_its_connection_is_left_alone():
    # 正常读完以后连接回到池子里给下一个请求用：这时再停下，不去断它（"停止"撞上回答结束的那一下）
    wire = inflight.Wire()
    call = inflight.Call()
    call.on(wire)
    stream = FakeStream()
    with call.request():
        call.claim(stream)
    call.abort()
    stream.peer.setblocking(False)
    with pytest.raises(BlockingIOError):
        stream.peer.recv(1)  # 还连着，什么都没读到


def test_a_reused_connection_is_found_among_the_unclaimed_ones():
    wire = inflight.Wire()
    mine, theirs, idle = FakeStream(), FakeStream(), FakeStream()
    other = inflight.Call()
    other.on(wire)
    for stream in (mine, theirs, idle):
        wire.seen(stream)
    wire.claim(theirs, other)  # 别的请求正用着的，不断
    call = inflight.Call()
    call.on(wire)
    with pytest.raises(inflight.Aborted), call.request():
        call.abort()  # 头还没到、不知道是哪一条：没有别人认领的都断开
        raise OSError
    assert mine.cut() and idle.cut()
    theirs.peer.setblocking(False)
    with pytest.raises(BlockingIOError):
        theirs.peer.recv(1)


def test_trace_stops_new_work_and_lets_cleanup_finish():
    call = inflight.Call()
    call.traced("http11.send_request_headers.started", {})  # 没停下：什么都不做
    call.abort()
    with pytest.raises(inflight.Aborted):
        call.traced("connection.connect_tcp.started", {})
    with pytest.raises(inflight.Aborted):
        call.traced("http11.receive_response_body.failed", {"exception": OSError()})
    call.traced("http11.response_closed.started", {})  # 收尾照常做完，不然连接留在池子里
    call.traced("http11.response_closed.failed", {"exception": OSError()})
    call.traced("http11.receive_response_body.failed", {"exception": GeneratorExit()})  # 回复正在被关掉：不换掉它


def test_calling_nests_and_current_outside_is_harmless():
    assert not inflight.current().aborted
    with inflight.calling() as outer:
        assert inflight.current() is outer
        with inflight.calling() as inner:  # 子 Agent 的一步在主助手的一次工具调用里
            assert inflight.current() is inner
        assert inflight.current() is outer
    assert inflight.current() is not outer


def until(condition, what: str):
    deadline = time.monotonic() + SLOW
    while not condition():
        assert time.monotonic() < deadline, f"等不到{what}"
        time.sleep(0.01)


def test_clients_made_by_zhifox_carry_the_hooks(monkeypatch):
    # 正式建的客户端都装着钩子：头没到的请求也能马上断开。Claude 和自定义接口在上面真的停过一次；
    # 内置的 OpenAI 格式那几家地址写死在 PROVIDERS 里、连不到假服务器，这里只看装没装上
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    for provider_id in ("anthropic", "openai", "gemini", "deepseek"):
        provider = find(provider_id)
        assert provider is not None
        client = make_client(provider, timeout=5)
        http = client._client  # 只为看一眼：SDK 没有公开读它的办法（只在测试里）
        assert any(isinstance(getattr(hook, "__self__", None), inflight.Wire) for hook in http.event_hooks["request"]), provider_id
