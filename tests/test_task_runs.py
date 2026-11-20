"""定时任务在网页界面里的运行：真实的 HTTP 服务器 + 本地的假 API，由测试代码扮演浏览器。

时间用的是假的钟（测试想让现在是几点就是几点），调度也不靠后台线程，而是测试自己调用 tick()
"看一眼时间表"，所以不用真的等到 9 点。任务一旦开始运行，走的就是真实的代码：执行线程、对话锁、
假 API 上的模型调用、工具、确认、存盘。

时间表本身怎么算、任务怎么保存、工具和权限规则，在 test_tasks.py。
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import anthropic
import pytest
from fake_api import FakeAPI, reply, text, thinking, tool_use
from test_webui import Browser

import harness.webui.server as server_module
from harness import i18n
from harness.app import build_app
from harness.webui.scheduler import PUBLISH_INTERVAL, Throttle
from harness.webui.server import create_server
from harness.webui.ui import WebUI

DAILY = {"type": "daily", "time": "09:00"}
ONE_REQUEST = round((100 * 4 + 40 * 0.2 + 10 * 5 + 20 * 20) / 1_000_000, 6)  # 假 API 每次回复的用量，按 claude-opus-5-5 的价格
RUN_FIELDS = {
    "id", "status", "started_at", "finished_at", "cost", "steps", "catch_up", "missed", "manual", "conversation", "summary",
    "error",
}
RUNNING_FIELDS = {"run_id", "started_at", "step", "max_steps", "cost", "activity", "log", "confirm"}


@pytest.fixture(autouse=True)
def zone():
    """固定用一个没有夏令时的时区，结果不随这台电脑变。"""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def at(*fields: int) -> float:
    return datetime(*fields).timestamp()


class Clock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def serve(make_config, monkeypatch):
    """启动一个网页服务器，后面连着假的 Claude API，任务用假的钟。同一个测试里调用两次，就相当于重启了一遍程序。"""
    started = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(server_module, "reveal_in_file_manager", lambda path: None)

    def serve(*replies, **config_overrides):
        api = FakeAPI(list(replies)).start()
        config = make_config(**config_overrides)
        ui = WebUI()

        def client_factory(provider):
            return anthropic.Anthropic(api_key=os.environ.get(provider.key_env) or "missing", base_url=api.base_url, max_retries=0)

        app = build_app(config, ui, client_factory=client_factory)
        clock = Clock(at(2026, 11, 21, 8, 0))
        app.tasks.clock = clock
        server, url = create_server(app, ui, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        browser = Browser(url)
        browser.wait_synced()
        s = SimpleNamespace(api=api, app=app, ui=ui, web=server.web, server=server, url=url, config=config, browser=browser, clock=clock)
        started.append(s)
        return s

    yield serve
    for s in started:
        s.web.tasks.close()
        s.web.stop()
        s.app.shell.kill_running()
        s.web.wait_idle(5)
        s.server.shutdown()
        s.server.server_close()
        s.api.stop()


def until(condition, timeout: float = 10, what: str = "条件"):
    """等到 condition() 返回真值，并返回它。"""
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value:
            return value
        assert time.monotonic() < deadline, f"等不到{what}"
        time.sleep(0.01)


def save(s, **fields) -> str:
    status, body = s.browser.post("/api/task/save", {"enabled": True, "prompt": "", **fields})
    assert status == 200 and set(body) == {"ok", "id"}, body
    return body["id"]


def agent_task(s, title: str = "每日安排", prompt: str = "看看我的待办，写一份今天的安排", schedule: dict = DAILY) -> str:
    return save(s, title=title, kind="agent", prompt=prompt, schedule=schedule)


def task_of(s, task_id: str) -> dict:
    return next(task for task in s.web.tasks.snapshot()[0] if task["id"] == task_id)


def finished(s, task_id: str, status: str) -> dict:
    """等到这个任务最近的一次运行以 status 结束，返回这个任务。"""
    def check():
        task = task_of(s, task_id)
        return task if task["running"] is None and task["runs"] and task["runs"][0]["status"] == status else None

    return until(check, what=f"运行结束（{status}）")


def paused(s, task_id: str) -> dict:
    """等到这个任务的运行停在一个要用户批准的操作上，返回它的 running。"""
    return until(lambda: (task_of(s, task_id)["running"] or {}).get("confirm") and task_of(s, task_id)["running"], what="确认")


def latest_state(s) -> dict:
    until(lambda: s.browser.of_type("state"), what="状态")
    return s.browser.of_type("state")[-1]


def say(s, message: str) -> None:
    assert s.browser.post("/api/message", {"text": message})[0] == 200
    s.browser.wait_for("turn_end")


# ---- 增删改查 ---------------------------------------------------------------


def test_tasks_are_saved_listed_and_previewed(serve):
    s = serve()
    task_id = save(s, title="喝水", kind="reminder", prompt="起来喝杯水", schedule=DAILY)

    state = until(lambda: latest_state(s)["tasks"] and latest_state(s), what="带着任务的状态")
    (task,) = state["tasks"]
    assert set(task) == {"id", "title", "kind", "prompt", "schedule", "enabled", "created_at", "next_run", "runs", "running"}
    assert (task["id"], task["title"], task["kind"], task["prompt"], task["schedule"]) == (task_id, "喝水", "reminder", "起来喝杯水", DAILY)
    assert task["next_run"] == at(2026, 11, 21, 9, 0) and task["created_at"] == s.clock.now
    assert task["runs"] == [] and task["running"] is None and state["reminders"] == []

    # 改：编号不变
    assert save(s, id=task_id, title="多喝水", kind="reminder", schedule={"type": "weekly", "days": [6], "time": "10:00"}) == task_id
    assert task_of(s, task_id)["next_run"] == at(2026, 11, 21, 10, 0)  # 今天是星期六

    # 保存之前先看一眼接下来的三次
    status, body = s.browser.post("/api/task/preview", {"schedule": {"type": "weekly", "days": [1], "time": "09:00"}})
    assert status == 200 and body == {"ok": True, "next": [at(2026, 11, 23, 9, 0), at(2026, 11, 30, 9, 0), at(2026, 12, 7, 9, 0)]}

    # 内容不对：400，原因说清楚；任务不存在：409
    status, body = s.browser.post("/api/task/save", {"title": "x", "kind": "reminder", "schedule": {"type": "daily", "time": "9"}})
    assert status == 400 and "09:00" in body["error"]
    once_past = {"title": "x", "kind": "reminder", "schedule": {"type": "once", "at": "2026-11-20T09:00"}}
    assert s.browser.post("/api/task/save", once_past) == (400, {"error": "这个时间已经过去了，请选一个以后的时间。"})
    assert s.browser.post("/api/task/preview", {"schedule": {"type": "once", "at": "2026-11-20T09:00"}})[0] == 400
    assert s.browser.post("/api/task/save", {"id": "ffffffff", "title": "x", "kind": "reminder", "schedule": DAILY})[0] == 409
    assert s.browser.post("/api/task/save", {"id": 5, "title": "x", "kind": "reminder", "schedule": DAILY})[0] == 400

    # 关掉、打开
    assert s.browser.post("/api/task/toggle", {"id": task_id, "enabled": "no"})[0] == 400
    assert s.browser.post("/api/task/toggle", {"id": task_id, "enabled": False}) == (200, {"ok": True})
    assert task_of(s, task_id)["enabled"] is False and task_of(s, task_id)["next_run"] is None
    assert s.browser.post("/api/task/toggle", {"id": task_id, "enabled": True}) == (200, {"ok": True})
    assert task_of(s, task_id)["next_run"] == at(2026, 11, 21, 10, 0)

    # 参数不全、不认识的操作
    for path, data in (("/api/task/run", {}), ("/api/task/stop", {"id": 1}), ("/api/task/dismiss", {}), ("/api/task/confirm", {"id": task_id})):
        assert s.browser.post(path, data)[0] == 400, path
    assert s.browser.post("/api/task/explode", {"id": task_id})[0] == 404

    # 删除
    assert s.browser.post("/api/task/delete", {"id": task_id}) == (200, {"ok": True})
    assert s.web.tasks.snapshot()[0] == []
    status, body = s.browser.post("/api/task/delete", {"id": task_id})
    assert status == 409 and "不存在" in body["error"]


def test_errors_follow_the_interface_language(serve):
    s = serve()
    assert s.browser.post("/api/language", {"language": "es"})[0] == 200
    status, body = s.browser.post("/api/task/save", {"title": "", "kind": "reminder", "schedule": DAILY})
    assert status == 400 and body["error"] == "La tarea necesita un nombre de hasta 80 caracteres."


# ---- 提醒 -------------------------------------------------------------------


def test_a_reminder_fires_on_time_once_and_can_be_dismissed(serve):
    s = serve()
    task_id = save(s, title="喝水", kind="reminder", prompt="起来喝杯水", schedule=DAILY)
    s.web.tasks.tick()  # 08:00：还没到
    assert s.web.tasks.snapshot()[1] == []

    s.clock.now = at(2026, 11, 21, 9, 0)
    s.web.tasks.tick()
    state = until(lambda: latest_state(s)["reminders"] and latest_state(s), what="提醒")
    (item,) = state["reminders"]
    assert item == {"id": item["id"], "task_id": task_id, "title": "喝水", "text": "起来喝杯水", "at": s.clock.now, "catch_up": False}
    (task,) = state["tasks"]
    (run,) = task["runs"]
    assert set(run) == RUN_FIELDS
    assert (run["status"], run["cost"], run["steps"], run["catch_up"], run["missed"], run["manual"]) == ("done", None, 0, False, 0, False)
    assert run["summary"] == "起来喝杯水" and run["started_at"] == run["finished_at"] == s.clock.now
    assert task["next_run"] == at(2026, 11, 22, 9, 0)

    s.web.tasks.tick()  # 再看一眼：同一次不会弹两遍
    assert len(s.web.tasks.snapshot()[1]) == 1
    assert s.api.requests == []  # 提醒不调用模型，不花钱

    assert s.browser.post("/api/task/dismiss", {"reminder_id": item["id"]}) == (200, {"ok": True})
    assert s.web.tasks.snapshot()[1] == []
    assert s.browser.post("/api/task/dismiss", {"reminder_id": item["id"]}) == (200, {"ok": True})  # 连点两下也没事

    # 立即提醒一次：不改原来的计划
    assert s.browser.post("/api/task/run", {"id": task_id}) == (200, {"ok": True})
    task = task_of(s, task_id)
    assert task["runs"][0]["manual"] is True and task["next_run"] == at(2026, 11, 22, 9, 0)
    s.clock.now = at(2026, 11, 22, 9, 0)
    s.web.tasks.tick()
    assert len(s.web.tasks.snapshot()[1]) == 2
    assert s.browser.post("/api/task/dismiss", {"all": True}) == (200, {"ok": True})
    assert s.web.tasks.snapshot()[1] == []


# ---- 让助手去做 -------------------------------------------------------------


def test_an_agent_task_runs_in_a_conversation_of_its_own(serve):
    s = serve(reply(text("你好呀。")), reply(thinking("先看看待办"), text("今天的安排：1. 交报告。")))
    say(s, "你好")
    current = s.app.conversation.id
    seen = len(s.browser.events)
    task_id = agent_task(s)

    s.clock.now = at(2026, 11, 21, 9, 0)
    s.web.tasks.tick()
    task = finished(s, task_id, "done")

    (run,) = task["runs"]
    assert set(run) == RUN_FIELDS
    assert (run["steps"], run["cost"], run["catch_up"], run["manual"], run["error"]) == (1, ONE_REQUEST, False, False, "")
    assert run["summary"] == "今天的安排：1. 交报告。" and run["finished_at"] is not None
    assert task["next_run"] == at(2026, 11, 22, 9, 0)

    # 发给模型的：全新的历史（没带着你刚才聊的），指令前面有一段说明
    request = s.api.requests[1]
    (message,) = request["messages"]
    words = message["content"][-1]["text"]
    assert words.startswith("<task>这一轮是定时任务“每日安排”（每天 09:00），到了预定的时间") and "不要停下来问他问题" in words
    assert words.endswith("</task>\n\n看看我的待办，写一份今天的安排")
    assert request["model"] == s.app.settings.model

    # 存成了一段会话：标题是任务名和时间，记着是哪个任务，事件能重放
    conversation = s.app.store.load(run["conversation"])
    assert conversation.title == "每日安排 · 2026-11-21 09:00" and conversation.task == task_id
    assert conversation.events[0] == {"type": "user", "text": "看看我的待办，写一份今天的安排"}  # 会话里显示的是你写的指令
    assert [event["type"] for event in conversation.events] == [
        "user", "turn_start", "thinking", "block_end", "text", "block_end", "turn_end",
    ]
    listed = next(item for item in latest_state(s)["conversations"] if item["id"] == run["conversation"])
    assert listed["task"] == task_id and listed["title"] == conversation.title
    (found,) = s.app.store.search("交报告")
    assert found["id"] == run["conversation"]

    # 你正在看的那段对话没有变，页面上也没有冒出任务的内容
    assert s.app.conversation.id == current
    assert {event["type"] for event in s.browser.events[seen:]} == {"state"}
    # 跑完之后日志里只留着最新的一张状态，不会越积越多
    assert [event["type"] for event in s.ui.events_after(0, timeout=0)].count("state") == 1

    # 打开这段会话：和打开别的会话一样，重放出来
    assert s.browser.post("/api/conversation/open", {"id": run["conversation"]})[0] == 200
    s.browser.wait_for("reset")
    assert s.browser.wait_for("user")["text"] == "看看我的待办，写一份今天的安排"


def test_a_due_task_waits_for_your_turn_and_then_holds_the_next_one(serve):
    s = serve(
        reply(tool_use("toolu_1", "run_command", {"command": "true"}), stop_reason="tool_use"),  # 你的这一轮
        reply(text("跑完了。")),
        reply(tool_use("toolu_2", "run_command", {"command": "touch report.txt"}), stop_reason="tool_use"),  # 任务
        reply(text("报告建好了。")),
    )
    task_id = agent_task(s)
    assert s.browser.post("/api/message", {"text": "跑一下"})[0] == 200
    yours = s.browser.wait_for("confirm")  # 你的这一轮停在确认上，拿着对话锁

    s.clock.now = at(2026, 11, 21, 9, 0)
    s.web.tasks.tick()
    task = until(lambda: task_of(s, task_id)["running"] and task_of(s, task_id), what="排队")
    running = task["running"]
    assert set(running) == RUNNING_FIELDS
    assert running["activity"] == {"kind": "waiting", "tool": "", "summary": "等你这一轮对话结束"}
    assert (running["step"], running["log"], running["confirm"]) == (0, [], None)
    assert task["runs"][0]["status"] == "waiting" and len(s.api.requests) == 1  # 还没开始

    assert s.browser.post("/api/confirm", {"id": yours["id"], "allow": True})[0] == 200
    s.browser.wait_for("turn_end")

    # 你这一轮一结束，任务就开始了；它也停在自己的确认上
    running = paused(s, task_id)
    assert running["confirm"]["tool"] == "run_command" and "touch report.txt" in running["confirm"]["summary"]
    assert running["activity"] == {"kind": "waiting", "tool": "run_command", "summary": "等你批准"}
    assert running["log"] == [{"tool": "run_command", "summary": "run_command $ touch report.txt", "ok": None}]
    assert (running["step"], running["max_steps"], running["cost"]) == (1, s.config.max_steps, ONE_REQUEST)
    assert task_of(s, task_id)["runs"][0]["status"] == "waiting"
    shown = until(lambda: (latest_state(s)["tasks"][0]["running"] or {}).get("confirm"), what="页面上的确认")
    assert shown == running["confirm"]
    assert len(s.browser.of_type("confirm")) == 1  # 任务的确认不出现在你的对话里

    # 任务拿着对话锁的时候：你发消息、开新对话都被拦下，并且说清楚是哪个任务
    status, body = s.browser.post("/api/message", {"text": "再来一句"})
    assert status == 409 and body["error"] == "定时任务“每日安排”正在运行。可以等它跑完，或者在“任务”里停下它。"
    assert s.browser.post("/api/new")[0] == 409
    assert s.browser.post("/api/model", {"provider": "anthropic", "model": "claude-sonnet-5-5"})[1]["error"] == body["error"]
    # 对话里的"停止"不是冲它来的
    assert s.browser.post("/api/stop") == (200, {"ok": True})
    assert task_of(s, task_id)["running"]["confirm"] is not None

    # 答复：编号不对的不算
    confirm_id = running["confirm"]["id"]
    assert s.browser.post("/api/task/confirm", {"id": task_id, "confirm_id": confirm_id + 1, "allow": True})[0] == 409
    assert s.browser.post("/api/task/confirm", {"id": task_id, "confirm_id": str(confirm_id), "allow": True})[0] == 400
    assert s.browser.post("/api/task/confirm", {"id": task_id, "confirm_id": confirm_id, "allow": True}) == (200, {"ok": True})
    task = finished(s, task_id, "done")
    assert (s.config.workspace / "report.txt").exists()
    run = task["runs"][0]
    assert (run["steps"], run["cost"]) == (2, round(2 * ONE_REQUEST, 6))
    assert s.browser.post("/api/task/confirm", {"id": task_id, "confirm_id": confirm_id, "allow": True})[0] == 409

    # 任务跑完，你可以接着聊了
    s.api.replies.append(reply(text("在。")))
    say(s, "在吗")


def test_unattended_runs_ask_even_when_everything_is_allowed(serve):
    s = serve(reply(tool_use("toolu_1", "run_command", {"command": "touch x.txt"}), stop_reason="tool_use"), reply(text("没建。")))
    assert s.browser.post("/api/approval", {"mode": "all"}) == (200, {"ok": True})
    task_id = agent_task(s)
    assert s.browser.post("/api/task/run", {"id": task_id}) == (200, {"ok": True})

    running = paused(s, task_id)  # 你选了"全部自动允许"，可没人看着的时候它还是停下来等你
    feedback = {"id": task_id, "confirm_id": running["confirm"]["id"], "allow": False, "feedback": " 先别动 "}
    assert s.browser.post("/api/task/confirm", feedback) == (200, {"ok": True})
    task = finished(s, task_id, "done")

    assert not (s.config.workspace / "x.txt").exists()
    result = s.api.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and result["content"] == "用户拒绝了这个操作。用户说：先别动"
    conversation = s.app.store.load(task["runs"][0]["conversation"])
    (confirm,) = [event for event in conversation.events if event["type"] == "confirm"]
    assert confirm["outcome"] == "denied"
    assert latest_state(s)["approval"] == "all"  # 对话用的那一份没被动过


def test_stopping_a_queued_task_and_a_running_one(serve):
    s = serve(
        reply(tool_use("toolu_1", "run_command", {"command": "true"}), stop_reason="tool_use"),  # 你的这一轮
        reply(text("好。")),
        reply(tool_use("toolu_2", "run_command", {"command": "sleep 30; touch late.txt"}), stop_reason="tool_use"),  # 任务
    )
    task_id = agent_task(s)
    assert s.browser.post("/api/message", {"text": "跑一下"})[0] == 200
    yours = s.browser.wait_for("confirm")
    assert s.browser.post("/api/task/run", {"id": task_id}) == (200, {"ok": True})
    assert task_of(s, task_id)["running"]["activity"]["kind"] == "waiting"

    # 还在排队：停下就不跑了
    assert s.browser.post("/api/task/stop", {"id": task_id}) == (200, {"ok": True})
    task = task_of(s, task_id)
    assert task["running"] is None and task["runs"][0]["status"] == "stopped"
    assert s.browser.post("/api/task/stop", {"id": task_id})[0] == 409  # 没有在运行
    assert s.browser.post("/api/confirm", {"id": yours["id"], "allow": True})[0] == 200
    s.browser.wait_for("turn_end")
    assert len(s.api.requests) == 2  # 任务一次都没调用模型

    # 正在跑、正在执行一条命令：停下时连命令一起结束
    assert s.browser.post("/api/task/run", {"id": task_id}) == (200, {"ok": True})
    running = paused(s, task_id)
    s.browser.post("/api/task/confirm", {"id": task_id, "confirm_id": running["confirm"]["id"], "allow": True})
    until(lambda: s.app.shell._running is not None, what="命令跑起来")
    began = time.monotonic()
    assert s.browser.post("/api/task/stop", {"id": task_id}) == (200, {"ok": True})
    task = finished(s, task_id, "stopped")
    assert time.monotonic() - began < 5
    assert not (s.config.workspace / "late.txt").exists()

    # 停下来之前做过的事照样存成会话；每个工具调用都有结果，这段会话还能接着聊
    conversation = s.app.store.load(task["runs"][0]["conversation"])
    (result,) = conversation.messages[-1]["content"]
    assert result["tool_use_id"] == "toolu_2" and result["is_error"] is True and "被用户停止" in result["content"]
    assert len(s.api.requests) == 3


def test_deleting_a_running_task_stops_it(serve):
    s = serve(reply(tool_use("toolu_1", "run_command", {"command": "true"}), stop_reason="tool_use"))
    task_id = agent_task(s)
    s.browser.post("/api/task/run", {"id": task_id})
    paused(s, task_id)
    assert s.browser.post("/api/task/delete", {"id": task_id}) == (200, {"ok": True})
    until(lambda: s.web.wait_idle(0.1), what="运行停下")
    assert s.web.tasks.snapshot()[0] == []
    (saved,) = [item for item in s.app.store.summaries() if item.get("task") == task_id]  # 做过的事还在会话里
    assert saved["title"].startswith("每日安排 · ")


def test_missed_occurrences_are_caught_up_once(serve):
    # 程序一直开着，但电脑睡了三天：醒来时只补一次，记下错过了几次
    s = serve()
    task_id = save(s, title="喝水", kind="reminder", schedule=DAILY)
    s.web.tasks.tick()
    s.clock.now = at(2026, 11, 24, 10, 0)
    s.web.tasks.tick()
    (item,) = s.web.tasks.snapshot()[1]
    assert item["catch_up"] is True
    run = task_of(s, task_id)["runs"][0]
    assert (run["catch_up"], run["missed"]) == (True, 4)  # 21、22、23、24 号的
    assert task_of(s, task_id)["next_run"] == at(2026, 11, 25, 9, 0)


def test_tasks_missed_while_the_program_was_closed_run_once_on_the_next_start(serve):
    first = serve()
    task_id = agent_task(first)
    first.web.tasks.close()

    # 三天后重新打开：错过的三次合成一次，标着是补跑的
    second = serve(reply(text("补上了。")))
    second.clock.now = at(2026, 11, 23, 12, 0)
    second.web.tasks.tick()
    task = finished(second, task_id, "done")
    run = task["runs"][0]
    assert (run["catch_up"], run["missed"], run["manual"]) == (True, 3, False)
    assert task["next_run"] == at(2026, 11, 24, 9, 0)
    words = second.api.requests[0]["messages"][0]["content"][-1]["text"]
    assert "原定 2026-11-21 09:00 运行，那时智狐没有开着，现在补上一次（这期间一共错过了 3 次，只补这一次）" in words
    second.web.tasks.tick()
    assert len(task_of(second, task_id)["runs"]) == 1 and len(second.api.requests) == 1


def test_a_one_time_task_runs_once(serve):
    s = serve(reply(text("该开会了。")))
    task_id = agent_task(s, title="开会", prompt="提醒我开会并列出议程", schedule={"type": "once", "at": "2026-11-21T09:00"})
    s.clock.now = at(2026, 11, 21, 9, 0, 30)
    s.web.tasks.tick()
    assert finished(s, task_id, "done")["next_run"] is None
    s.clock.now = at(2026, 11, 22, 9, 0)
    s.web.tasks.tick()
    assert len(task_of(s, task_id)["runs"]) == 1 and len(s.api.requests) == 1


def test_running_now_and_what_stops_it(serve, monkeypatch):
    s = serve(reply(text("好了。")))
    task_id = agent_task(s)
    planned = task_of(s, task_id)["next_run"]
    assert s.browser.post("/api/task/run", {"id": task_id}) == (200, {"ok": True})
    task = finished(s, task_id, "done")
    assert task["runs"][0]["manual"] is True and task["next_run"] == planned  # 立即运行不改原来的计划
    assert "用户刚刚在任务列表里点了“立即运行”" in s.api.requests[0]["messages"][0]["content"][-1]["text"]
    assert s.browser.post("/api/task/run", {"id": "ffffffff"})[0] == 409

    # 没有密钥：立即运行当场拒绝；到时间的那一次记成失败，并且往后排，不会每看一眼就重试一次
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    status, body = s.browser.post("/api/task/run", {"id": task_id})
    assert status == 409 and "API 密钥" in body["error"]
    s.clock.now = at(2026, 11, 21, 9, 0)
    s.web.tasks.tick()
    task = finished(s, task_id, "failed")
    assert task["runs"][0]["error"] == "还没有设置 Claude (Anthropic) 的 API 密钥，这次没有运行。"
    assert task["next_run"] == at(2026, 11, 22, 9, 0)
    # 换了语言，记录里的出错原因跟着换
    assert s.browser.post("/api/language", {"language": "en"})[0] == 200
    assert task_of(s, task_id)["runs"][0]["error"] == "No Claude (Anthropic) API key is set, so this run didn't happen."
    i18n.choose("zh")


def test_a_failed_run_is_recorded(serve):
    s = serve()  # 没有预设回复：假 API 回答 500
    task_id = agent_task(s)
    s.browser.post("/api/task/run", {"id": task_id})
    run = finished(s, task_id, "failed")["runs"][0]
    assert "500" in run["error"] and run["conversation"] == "" and run["cost"] == 0.0 and run["steps"] == 0
    assert s.app.store.summaries() == []  # 一点内容都没有：不存会话


def test_the_model_can_set_up_a_task_during_a_conversation(serve):
    s = serve(
        reply(tool_use("toolu_1", "task_add", {"kind": "reminder", "title": "喝水", "schedule": DAILY}), stop_reason="tool_use"),
        reply(text("好的，每天 9 点提醒你喝水。")),
    )
    say(s, "每天早上 9 点提醒我喝水")
    assert s.browser.of_type("confirm") == []
    assert [task["title"] for task in latest_state(s)["tasks"]] == ["喝水"]


def test_the_scheduler_thread_wakes_up_when_something_is_due(serve):
    s = serve()
    first = save(s, title="喝水", kind="reminder", schedule=DAILY)
    s.clock.now = at(2026, 11, 21, 9, 0)
    s.web.tasks.start()
    until(lambda: s.web.tasks.snapshot()[1], what="提醒弹出来")

    # 线程正睡着（下一次要等很久）：任务一变就把它叫醒，重新看一眼
    second = save(s, title="活动一下", kind="reminder", schedule={"type": "interval", "minutes": 5})
    s.clock.now += 5 * 60
    s.browser.post("/api/task/toggle", {"id": first, "enabled": True})
    until(lambda: task_of(s, second)["runs"], what="第二个提醒")
    s.web.tasks.close()


def test_quitting_stops_the_running_task_and_keeps_what_it_did(serve):
    s = serve(
        reply(tool_use("toolu_1", "run_command", {"command": "true"}), stop_reason="tool_use"),  # 你的这一轮
        reply(text("好。")),
        reply(tool_use("toolu_2", "run_command", {"command": "true"}), stop_reason="tool_use"),  # 第一个任务
    )
    plan, other = agent_task(s), agent_task(s, title="周报")
    assert s.browser.post("/api/message", {"text": "跑一下"})[0] == 200
    yours = s.browser.wait_for("confirm")
    s.browser.post("/api/task/run", {"id": plan})
    s.browser.post("/api/task/run", {"id": other})
    assert task_of(s, other)["running"]["activity"]["summary"] == "排在“每日安排”后面"
    s.browser.post("/api/confirm", {"id": yours["id"], "allow": True})
    paused(s, plan)

    server_module.shut_down(s.server, s.app)
    task = finished(s, plan, "stopped")
    assert task["runs"][0]["conversation"]  # 停下之前做过的事存了下来
    queued = finished(s, other, "stopped")["runs"][0]
    assert queued["error"] == "智狐退出了，这次没有运行。" and queued["conversation"] == ""


def test_progress_is_throttled_but_the_latest_always_goes_out():
    # 逐字输出时每个字都会来要求刷新一次。第一次马上发，间隔里的合成一次、到点补上，所以最后的样子一定发得出去
    assert PUBLISH_INTERVAL <= 0.25  # 每秒最多四次
    sent: list[float] = []
    throttle = Throttle(lambda: sent.append(time.monotonic()), interval=0.1)
    for _ in range(100):
        throttle()
    assert len(sent) == 1
    until(lambda: len(sent) == 2, what="补上的那一次")
    assert sent[1] - sent[0] >= 0.09
    throttle(now=True)  # 开始、结束、停在确认上：马上发，不等
    assert len(sent) == 3
