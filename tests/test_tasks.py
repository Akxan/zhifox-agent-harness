"""定时任务的测试（不经过网页界面的部分）：时间表的检查、下一次的计算（包括月底和夏令时）、保存和读取、
补跑时数错过了几次、模型用的工具和它们的权限规则。

到时间之后怎么排队、怎么运行、怎么显示进度，在 test_task_runs.py。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from functools import partial

import pytest
from fake_api import reply, text, tool_use
from modes import has_mode

from harness import i18n, safe
from harness.i18n import Message
from harness.permissions import MODES, Permissions
from harness.tasks import (
    MAX_TASKS,
    TaskError,
    TaskRefused,
    TaskStore,
    check_schedule,
    check_task,
    count_due,
    describe,
    local_text,
    new_run,
    next_after,
    preview,
    upcoming,
)
from harness.tools.files import FileEditor


@pytest.fixture(autouse=True)
def zone():
    """把这个进程的本地时区换成指定的那个，测试结束时换回来。默认用一个没有夏令时的时区，结果不随这台电脑变。

    Windows 上没有 time.tzset，程序里换不了时区：默认的那一个就用这台电脑自己的（CI 的 Windows 是 UTC，也没有夏令时），
    要换成别的时区（测夏令时）的测试跳过。"""
    if not hasattr(time, "tzset"):
        def keep(name: str) -> None:
            if name != "Asia/Shanghai":
                pytest.skip(f"Windows 上不能在程序里换时区（没有 time.tzset），测不了 {name}")

        yield keep
        return
    old = os.environ.get("TZ")

    def use(name: str) -> None:
        os.environ["TZ"] = name
        time.tzset()

    use("Asia/Shanghai")
    yield use
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def at(*fields: int) -> float:
    """本地时间的某一刻，换成秒。"""
    return datetime(*fields).timestamp()


def shown(moment: float | None) -> str | None:
    return None if moment is None else local_text(moment)


def now() -> float:
    """测试里的"现在"：2026-11-20 14:03，星期五。要在换好时区之后才算（所以是函数，不是常量）。"""
    return at(2026, 11, 20, 14, 3)


class Clock:
    """假的钟：测试想让现在是几点就是几点。"""

    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


# ---- 时间表的检查 -----------------------------------------------------------


def test_valid_schedules_are_tidied_up():
    assert check_schedule({"type": "once", "at": "2026-11-21T09:00"}) == {"type": "once", "at": "2026-11-21T09:00"}
    assert check_schedule({"type": "daily", "time": "00:00"}) == {"type": "daily", "time": "00:00"}
    # 星期几去重、排好序；模型常把用不到的项填成 null，去掉
    assert check_schedule({"type": "weekly", "days": [5, 1, 3, 1], "time": "23:59", "at": None}) == {
        "type": "weekly", "days": [1, 3, 5], "time": "23:59",
    }
    assert check_schedule({"type": "monthly", "day": 31, "time": "09:00"}) == {"type": "monthly", "day": 31, "time": "09:00"}
    assert check_schedule({"type": "interval", "minutes": 5}) == {"type": "interval", "minutes": 5}
    assert check_schedule({"type": "interval", "minutes": 7 * 24 * 60})["minutes"] == 10080


@pytest.mark.parametrize(
    "schedule",
    [
        None,
        "daily 09:00",
        {},
        {"type": "yearly", "time": "09:00"},
        {"type": "daily"},
        {"type": "daily", "time": "9:00"},
        {"type": "daily", "time": "24:00"},
        {"type": "daily", "time": "09:60"},
        {"type": "daily", "time": "09:00:00"},
        {"type": "daily", "time": "09:00", "days": [1]},  # 多出来的项多半说明想要的是另一种
        {"type": "once", "at": "2026-11-21 09:00"},
        {"type": "once", "at": "2026-02-30T09:00"},
        {"type": "once", "at": "tomorrow"},
        {"type": "weekly", "days": [], "time": "09:00"},
        {"type": "weekly", "days": [0], "time": "09:00"},
        {"type": "weekly", "days": [8], "time": "09:00"},
        {"type": "weekly", "days": [True], "time": "09:00"},
        {"type": "weekly", "days": "1,3", "time": "09:00"},
        {"type": "monthly", "day": 0, "time": "09:00"},
        {"type": "monthly", "day": 32, "time": "09:00"},
        {"type": "monthly", "day": 1.0, "time": "09:00"},
        {"type": "interval", "minutes": 4},
        {"type": "interval", "minutes": 10081},
        {"type": "interval", "minutes": True},
        {"type": "interval", "minutes": 5.0},
    ],
)
def test_bad_schedules_are_refused_with_a_message_for_the_user(schedule):
    with pytest.raises(TaskError) as caught:
        check_schedule(schedule)
    assert isinstance(caught.value.args[0], Message)  # 用 t() 写的：页面换语言时能就地重译


def test_schedule_errors_follow_the_interface_language(monkeypatch):
    monkeypatch.setattr(i18n, "current", "en")
    with pytest.raises(TaskError, match="between 5 and 10080 minutes"):
        check_schedule({"type": "interval", "minutes": 1})
    monkeypatch.setattr(i18n, "current", "es")
    with pytest.raises(TaskError, match="HH:MM"):
        check_schedule({"type": "daily", "time": "nine"})


def test_tasks_are_checked():
    daily = {"type": "daily", "time": "09:00"}
    task = check_task({"title": "  喝水 \n 提醒 ", "kind": "reminder", "schedule": daily})
    assert task == {"title": "喝水 提醒", "kind": "reminder", "prompt": "", "schedule": daily, "enabled": True, "approval": "ask", "sandbox": "default"}
    assert check_task({"title": "安排", "kind": "agent", "prompt": " 看看待办 ", "schedule": daily, "enabled": False})["prompt"] == "看看待办"
    for bad in (
        {"title": "", "kind": "reminder", "schedule": daily},
        {"title": "长" * 81, "kind": "reminder", "schedule": daily},
        {"title": "x", "kind": "robot", "schedule": daily},
        {"title": "x", "kind": "agent", "prompt": "   ", "schedule": daily},  # 让助手去做，总得说做什么
        {"title": "x", "kind": "agent", "prompt": "长" * 10_001, "schedule": daily},
        {"title": "x", "kind": "reminder", "schedule": daily, "enabled": "yes"},
        {"title": "x", "kind": "reminder", "schedule": {"type": "daily"}},
        "not a task",
    ):
        with pytest.raises(TaskError):
            check_task(bad)


def test_tasks_say_how_much_to_ask_while_running():
    # agent 任务运行时多半没人看着，所以它自己记着"要确认的操作问不问你"，和对话里的三档同名、同义
    daily = {"type": "daily", "time": "09:00"}
    plan = {"title": "安排", "kind": "agent", "prompt": "看看待办", "schedule": daily}
    assert check_task(plan)["approval"] == "ask"  # 没说的话就是每次都问
    for mode in MODES:
        assert check_task({**plan, "approval": mode})["approval"] == mode
    for bad in ("", "always", "ALL", "yes", 1, True, ["all"], {"mode": "all"}):
        with pytest.raises(TaskError):
            check_task({**plan, "approval": bad})
    # 提醒不调用模型、不用工具，没有什么可问的：不管填了哪一档都存成 ask，文件里不留一个不起作用的"全部自动允许"
    for mode in MODES:
        assert check_task({"title": "喝水", "kind": "reminder", "schedule": daily, "approval": mode})["approval"] == "ask"


# ---- 下一次是什么时候 -------------------------------------------------------


def test_next_run_for_every_kind_of_schedule():
    # 一次性：没到就是那个时刻，过了就不会再有
    once = {"type": "once", "at": "2026-11-21T09:00"}
    assert shown(next_after(once, now())) == "2026-11-21 09:00"
    assert next_after(once, at(2026, 11, 21, 9, 0)) is None  # "严格晚于"：正好在那一刻也算过了
    # 每天：今天的还没到就是今天，过了就是明天
    assert shown(next_after({"type": "daily", "time": "15:00"}, now())) == "2026-11-20 15:00"
    assert shown(next_after({"type": "daily", "time": "09:00"}, now())) == "2026-11-21 09:00"
    assert shown(next_after({"type": "daily", "time": "14:03"}, now())) == "2026-11-21 14:03"
    # 每周：今天是星期五
    assert shown(next_after({"type": "weekly", "days": [1, 3, 5], "time": "09:00"}, now())) == "2026-11-23 09:00"
    assert shown(next_after({"type": "weekly", "days": [5], "time": "15:00"}, now())) == "2026-11-20 15:00"
    assert shown(next_after({"type": "weekly", "days": [5], "time": "09:00"}, now())) == "2026-11-27 09:00"
    assert shown(next_after({"type": "weekly", "days": [7], "time": "09:00"}, now())) == "2026-11-22 09:00"


def test_monthly_schedules_fall_back_to_the_last_day_of_short_months():
    end_of_month = {"type": "monthly", "day": 31, "time": "09:00"}
    times = upcoming(end_of_month, now(), count=5)
    assert [shown(moment) for moment in times] == [
        "2026-11-30 09:00",  # 11 月只有 30 天
        "2026-12-31 09:00",
        "2027-01-31 09:00",
        "2027-02-28 09:00",
        "2027-03-31 09:00",
    ]
    leap = {"type": "monthly", "day": 29, "time": "08:00"}
    assert shown(next_after(leap, at(2028, 2, 1))) == "2028-02-29 08:00"  # 闰年有 29 号
    assert shown(next_after(leap, at(2027, 2, 1))) == "2027-02-28 08:00"
    assert shown(next_after({"type": "monthly", "day": 20, "time": "15:00"}, now())) == "2026-11-20 15:00"


def test_intervals_count_from_their_anchor_and_stay_on_the_grid():
    every_90 = {"type": "interval", "minutes": 90}
    assert next_after(every_90, now(), anchor=now()) == now() + 5400
    assert next_after(every_90, now() + 5400, anchor=now()) == now() + 2 * 5400  # 正好在那一刻也算过了
    # 程序醒得晚了一点、或者关了很久：下一次仍然落在 anchor + n × 间隔 上，不会跟着往后推
    assert next_after(every_90, now() + 5400 + 17, anchor=now()) == now() + 2 * 5400
    assert next_after(every_90, now() + 10 * 5400 + 1, anchor=now()) == now() + 11 * 5400
    assert upcoming(every_90, now()) == [now() + 5400, now() + 2 * 5400, now() + 3 * 5400]


def test_daylight_saving_keeps_the_wall_clock_time(zone):
    zone("America/New_York")  # 2026-03-08 凌晨 2 点跳到 3 点；2026-11-01 凌晨 2 点退回 1 点
    nine = {"type": "daily", "time": "09:00"}
    before, after = next_after(nine, at(2026, 3, 7, 0, 0)), next_after(nine, at(2026, 3, 7, 10, 0))
    assert (shown(before), shown(after)) == ("2026-03-07 09:00", "2026-03-08 09:00")
    assert after - before == 23 * 3600  # 墙上的钟还是 9 点，中间只隔了 23 个小时

    # 跳过去的那个小时里的时刻不存在：顺延到跳过之后，那一天照样运行一次
    skipped = {"type": "daily", "time": "02:30"}
    first = next_after(skipped, at(2026, 3, 7, 12, 0))
    assert shown(first) == "2026-03-08 03:30"
    assert shown(next_after(skipped, first)) == "2026-03-09 02:30"

    # 走两遍的那个小时：只在第一遍运行，不会一晚上跑两次
    twice = {"type": "daily", "time": "01:30"}
    first = next_after(twice, at(2026, 10, 31, 12, 0))
    second = next_after(twice, first)
    assert shown(first) == "2026-11-01 01:30" and shown(second) == "2026-11-02 01:30"
    assert second - first == 25 * 3600
    # 每隔多久的任务数的是真实经过的时间，不看墙上的钟
    hourly = {"type": "interval", "minutes": 60}
    assert next_after(hourly, first, anchor=first) - first == 3600


def test_counting_missed_occurrences():
    daily = {"type": "daily", "time": "09:00"}
    due = at(2026, 11, 21, 9, 0)
    assert count_due(daily, due, due) == 1
    assert count_due(daily, due, at(2026, 11, 23, 8, 0)) == 2  # 21、22 号的，23 号的还没到
    assert count_due(daily, due, at(2026, 11, 23, 9, 0)) == 3
    assert count_due(daily, due, due - 1) == 0
    assert count_due({"type": "interval", "minutes": 30}, now(), now() + 95 * 60, anchor=now() - 1800) == 4
    assert count_due({"type": "once", "at": "2026-11-21T09:00"}, due, due + 10**6) == 1
    assert count_due(daily, due, due + 10 * 365 * 86400) == 1000  # 关了十年也只数到一千


def test_preview_and_descriptions():
    assert preview({"type": "daily", "time": "09:00"}, now()) == [
        at(2026, 11, 21, 9, 0), at(2026, 11, 22, 9, 0), at(2026, 11, 23, 9, 0),
    ]
    assert preview({"type": "once", "at": "2026-11-21T09:00"}, now()) == [at(2026, 11, 21, 9, 0)]
    with pytest.raises(TaskError, match="已经过去了"):
        preview({"type": "once", "at": "2026-11-19T09:00"}, now())

    assert describe({"type": "weekly", "days": [1, 3], "time": "09:00"}) == "每周一、周三 09:00"
    assert describe({"type": "weekly", "days": [1, 2, 3, 4, 5], "time": "09:00"}) == "工作日 09:00"
    assert describe({"type": "interval", "minutes": 90}) == "每 1 小时 30 分钟"
    assert describe({"type": "monthly", "day": 31, "time": "09:00"}).startswith("每月 31 日 09:00")
    i18n.choose("en")
    try:
        # 嵌在句子里的星期几也跟着换，不会半中半英
        assert describe({"type": "weekly", "days": [1, 3], "time": "09:00"}) == "Every Monday, Wednesday at 09:00"
    finally:
        i18n.choose("zh")


# ---- 保存和读取 -------------------------------------------------------------

DAILY = {"type": "daily", "time": "09:00"}


def reminder(title: str = "喝水", schedule: dict | None = None, **extra) -> dict:
    return {"title": title, "kind": "reminder", "prompt": "", "schedule": schedule or DAILY, **extra}


def test_tasks_are_saved_and_read_back(tmp_path):
    path = tmp_path / "data" / "tasks.json"
    store = TaskStore(path, clock=Clock(now()))
    task = store.add(reminder(prompt="起来喝杯水"))
    agent = store.add({"title": "安排", "kind": "agent", "prompt": "看看待办", "schedule": {"type": "interval", "minutes": 60}})

    assert task["next_run"] == at(2026, 11, 21, 9, 0) and task["created_at"] == now()
    assert agent["next_run"] == now() + 3600
    assert has_mode(path, 0o600)  # 只有自己的账号能读
    assert "起来喝杯水" in path.read_text(encoding="utf-8")  # 中文原样写进去，打开就能看懂

    again = TaskStore(path)  # 重新启动
    tasks, reminders = again.snapshot()
    assert [item["id"] for item in tasks] == [task["id"], agent["id"]] and reminders == []
    assert tasks[0] == task


def test_a_missing_or_broken_task_file(tmp_path):
    path = tmp_path / "tasks.json"
    assert TaskStore(path).snapshot() == ([], []) and not path.exists()  # 还没有文件：空的，不报错

    path.write_text("{ 写坏了", encoding="utf-8")
    store = TaskStore(path)
    assert store.snapshot() == ([], [])
    assert "tasks.broken.json" in store.problem  # 说清楚原来的文件放在哪儿
    assert (tmp_path / "tasks.broken.json").read_text(encoding="utf-8") == "{ 写坏了"  # 原样留着，没有被覆盖
    store.add(reminder())
    assert len(TaskStore(path).snapshot()[0]) == 1

    # 只坏了其中一个：丢掉坏的那个，别的照常用
    good = json.loads(path.read_text(encoding="utf-8"))
    good["tasks"].append({"id": "nothex!!", "title": "坏的"})
    good["tasks"].append({**good["tasks"][0], "id": "0000beef", "schedule": {"type": "daily", "time": "25:00"}})
    good["reminders"] = [{"id": "r", "task_id": "x", "title": "t", "text": "", "at": "昨天"}]
    path.write_text(json.dumps(good), encoding="utf-8")
    store = TaskStore(path)
    assert len(store.snapshot()[0]) == 1 and store.snapshot()[1] == [] and store.problem == ""


def test_a_run_left_unfinished_by_a_crash_is_marked_failed(tmp_path):
    path = tmp_path / "tasks.json"
    store = TaskStore(path, clock=Clock(now()))
    task = store.add({"title": "安排", "kind": "agent", "prompt": "看看待办", "schedule": DAILY})
    store.add_run(task["id"], new_run(now(), "running"))

    i18n.choose("en")
    try:
        (restarted,) = TaskStore(path).snapshot()[0]
        (run,) = restarted["runs"]
        assert run["status"] == "failed" and run["error"] == "智狐 quit before this run finished."
    finally:
        i18n.choose("zh")


def test_changes_made_by_another_program_are_picked_up(tmp_path):
    # 终端界面和网页界面可以同时开着，各自有一份。终端里加的任务，网页这边下次用的时候就能看到，不会被覆盖掉
    path = tmp_path / "tasks.json"
    web = TaskStore(path, clock=Clock(now()))
    web.add(reminder("网页加的"))
    terminal = TaskStore(path, clock=Clock(now()))
    terminal.add(reminder("终端加的"))
    web.add(reminder("网页又加的"))
    assert [task["title"] for task in TaskStore(path).snapshot()[0]] == ["网页加的", "终端加的", "网页又加的"]


def test_editing_toggling_and_deleting(tmp_path):
    clock = Clock(now())
    store = TaskStore(tmp_path / "tasks.json", clock=clock)
    with pytest.raises(TaskError, match="已经过去了"):
        store.add(reminder(schedule={"type": "once", "at": "2026-11-19T09:00"}))  # 多半是日期选错了

    every_hour = store.add(reminder("活动一下", {"type": "interval", "minutes": 60}))
    clock.now = now() + 600
    # 只改名称：时间表没变，下一次照旧（间隔不会因为改了个名字就重新数）
    renamed = store.update(every_hour["id"], reminder("站起来", {"type": "interval", "minutes": 60}))
    assert renamed["title"] == "站起来" and renamed["next_run"] == now() + 3600
    # 换了时间表：从现在重新算
    moved = store.update(every_hour["id"], reminder("站起来", {"type": "interval", "minutes": 30}))
    assert moved["next_run"] == now() + 600 + 1800
    # 新选的时间已经过了：什么都不改
    with pytest.raises(TaskError):
        store.update(every_hour["id"], reminder("站起来", {"type": "once", "at": "2026-11-01T09:00"}))
    assert store.get(every_hour["id"])["schedule"] == {"type": "interval", "minutes": 30}

    # 一次性的任务过了时间之后，改个名字不报错
    once = store.add(reminder("开会", {"type": "once", "at": "2026-11-21T09:00"}))
    clock.now = at(2026, 11, 22)
    assert store.update(once["id"], reminder("开周会", {"type": "once", "at": "2026-11-21T09:00"}))["next_run"] is None

    # 关掉：不再有下一次；重新打开：从现在往后算，关着的这段时间错过的不补
    assert store.set_enabled(every_hour["id"], False)["next_run"] is None
    clock.now = at(2026, 11, 25)
    reopened = store.set_enabled(every_hour["id"], True)
    assert reopened["next_run"] > clock.now and reopened["next_run"] - clock.now <= 1800

    store.delete(once["id"])
    assert [task["id"] for task in store.snapshot()[0]] == [every_hour["id"]]
    for action in (lambda: store.delete(once["id"]), lambda: store.set_enabled("00000000", True)):
        with pytest.raises(TaskRefused, match="不存在"):
            action()


def test_approval_is_saved_changed_and_defaults_to_ask_for_old_files(tmp_path):
    path = tmp_path / "tasks.json"
    store = TaskStore(path, clock=Clock(now()))
    plan = {"title": "安排", "kind": "agent", "prompt": "看看待办", "schedule": DAILY}
    task = store.add(plan)
    assert task["approval"] == "ask"

    # 你在页面上编辑任务、放宽了它：只改了这一项，时间表照旧
    loosened = store.update(task["id"], {**plan, "approval": "all"})
    assert loosened["approval"] == "all" and loosened["next_run"] == task["next_run"]
    assert TaskStore(path).get(task["id"])["approval"] == "all"  # 存进了文件
    # 改成只是提醒：那一档没有意义了，变回 ask
    assert store.update(task["id"], {**reminder("安排"), "approval": "all"})["approval"] == "ask"
    store.update(task["id"], {**plan, "approval": "edits"})

    # 有这一项之前存下的文件里没有它：读回来按"每次都问"算，不会因为升级了程序就放宽
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["tasks"][0]["approval"]
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (old,) = TaskStore(path).snapshot()[0]
    assert (old["id"], old["kind"], old["approval"]) == (task["id"], "agent", "ask")


def test_runs_recorded_from_many_threads_at_once_are_all_kept(tmp_path, monkeypatch):
    # 几个任务的运行同时进行：每一次运行开始、停在确认上、做完，都要记一笔（add_run、update_run），
    # 这些都是"改内存里的记录、整个写回文件"。没有锁的话会这样：A 改好内存、把整份内容转成了 JSON，还没写进文件；
    # B、C……各自改好、写好；A 这才把它那份旧的 JSON 写进去，盖掉了别人记的那些。
    # 内存里看着都在，重新启动以后就没了。这里让第一个写文件的线程停在"写之前"，等别的线程都做完。
    path = tmp_path / "tasks.json"
    store = TaskStore(path, clock=Clock(now()))
    task = store.add({"title": "安排", "kind": "agent", "prompt": "看看待办", "schedule": DAILY})
    started = [new_run(now() + n, "running") for n in range(6)]
    for run in started:
        store.add_run(task["id"], run)

    real_write = safe.write_file
    first_writing, others_done = threading.Event(), threading.Event()

    def write_file(target, content):
        if not first_writing.is_set():
            first_writing.set()
            others_done.wait(timeout=0.3)  # 有锁的话别的线程进不来，等到超时就接着写
        real_write(target, content)

    monkeypatch.setattr("harness.tasks.write_file", write_file)
    errors: list = []

    def in_thread(job) -> threading.Thread:
        def guarded() -> None:
            try:
                assert job() is True
            except BaseException as exc:  # noqa: BLE001 —— 什么错误都要带回主线程
                errors.append(exc)

        thread = threading.Thread(target=guarded, daemon=True)
        thread.start()
        return thread

    added = [new_run(now() + 100 + n, "running") for n in range(10)]
    first = in_thread(partial(store.add_run, task["id"], added[0]))
    assert first_writing.wait(timeout=2)
    others = [in_thread(partial(store.add_run, task["id"], run)) for run in added[1:]]
    others += [
        in_thread(partial(store.update_run, task["id"], run["id"], status="done", summary=f"第 {n} 次"))
        for n, run in enumerate(started)
    ]
    for thread in others:
        thread.join(timeout=5)
    others_done.set()
    first.join(timeout=5)

    assert errors == []
    for runs in (store.get(task["id"])["runs"], TaskStore(path).get(task["id"])["runs"]):  # 内存里的，和文件里的
        assert {run["id"] for run in runs} == {run["id"] for run in started + added}
        finished = {run["id"]: run["summary"] for run in runs if run["status"] == "done"}
        assert finished == {run["id"]: f"第 {n} 次" for n, run in enumerate(started)}


def test_there_is_a_limit_on_how_many_tasks(tmp_path):
    store = TaskStore(tmp_path / "tasks.json", clock=Clock(now()))
    for n in range(MAX_TASKS):
        store.add(reminder(f"第 {n} 个"))
    with pytest.raises(TaskRefused, match=f"最多 {MAX_TASKS} 个"):
        store.add(reminder("再来一个"))


def test_reminders_fire_and_are_dismissed(tmp_path):
    clock = Clock(at(2026, 11, 21, 9, 0))
    store = TaskStore(tmp_path / "tasks.json", clock=Clock(now()))
    task = store.add(reminder(prompt="起来喝杯水"))
    fired = store.remind(task["id"], clock.now, new_run(clock.now, "done"))
    assert (fired["title"], fired["text"], fired["at"]) == ("喝水", "起来喝杯水", clock.now)
    (saved,) = store.snapshot()[0]
    assert saved["next_run"] == at(2026, 11, 22, 9, 0)  # 下一次往后排
    assert saved["runs"][0]["status"] == "done" and saved["runs"][0]["summary"] == "起来喝杯水"
    store.remind(task["id"], clock.now + 86400, new_run(clock.now + 86400, "done"))
    assert len(store.snapshot()[1]) == 2
    store.dismiss(fired["id"])
    store.dismiss(fired["id"])  # 再关一次也不算错
    assert [item["id"] for item in store.snapshot()[1]] != [fired["id"]] and len(store.snapshot()[1]) == 1
    store.dismiss(None)
    assert store.snapshot()[1] == []


# ---- 模型用的工具，和它们的权限 ---------------------------------------------


def test_the_model_can_add_a_reminder_without_asking(start):
    h = start(
        reply(
            tool_use("toolu_1", "task_add", {"kind": "reminder", "title": "喝水", "schedule": {"type": "daily", "time": "09:00", "days": None}}),
            stop_reason="tool_use",
        ),
        reply(text("好的，每天 9 点提醒你喝水。")),
    )
    h.agent.run_turn("每天早上 9 点提醒我喝水")

    assert h.ui.confirmations == []  # 只是提醒：不花钱，不用问
    (task,) = h.app.tasks.snapshot()[0]
    assert (task["kind"], task["title"], task["schedule"]) == ("reminder", "喝水", DAILY)
    result = json.loads(h.ui.results[0][0])
    assert result["added"]["when"] == "每天 09:00" and result["added"]["next_run"].endswith("09:00")
    assert "网页界面" in result["note"]  # 告诉模型：只在网页界面开着的时候运行
    # 说明书里的时间表是一个对象，各家都能用
    spec = next(tool for tool in h.api.requests[0]["tools"] if tool["name"] == "task_add")
    assert spec["input_schema"]["properties"]["schedule"]["type"] == "object"


def test_adding_an_agent_task_needs_approval(start):
    agent_task = {"kind": "agent", "title": "每日安排", "prompt": "看看我的待办，写一份今天的安排", "schedule": DAILY}
    h = start(
        reply(tool_use("toolu_1", "task_add", agent_task), stop_reason="tool_use"),
        reply(text("没有加。")),
        approve=False,
    )
    h.agent.run_turn("每天 9 点帮我排一下当天的事")

    (question,) = h.ui.confirmations
    assert "每日安排" in question and "每天 09:00" in question and "花 API 费用" in question and "写一份今天的安排" in question
    assert h.app.tasks.snapshot()[0] == []  # 用户没点头：什么都没加

    h.ui.approve = True
    h.api.replies += [reply(tool_use("toolu_2", "task_add", agent_task), stop_reason="tool_use"), reply(text("加好了。"))]
    h.agent.run_turn("加吧")
    (task,) = h.app.tasks.snapshot()[0]
    assert task["kind"] == "agent"


def test_deleting_an_agent_task_needs_approval_but_a_reminder_does_not(start):
    h = start(approve=False)
    plan = h.app.tasks.add({"title": "每日安排", "kind": "agent", "prompt": "看看待办", "schedule": DAILY})
    water = h.app.tasks.add(reminder())
    h.api.replies += [
        reply(tool_use("toolu_1", "task_delete", {"id": water["id"]}), stop_reason="tool_use"),
        reply(tool_use("toolu_2", "task_delete", {"id": plan["id"]}), stop_reason="tool_use"),
        reply(tool_use("toolu_3", "task_delete", {"id": "ffffffff"}), stop_reason="tool_use"),
        reply(text("好了。")),
    ]
    h.agent.run_turn("把任务都删了")

    (question,) = h.ui.confirmations  # 只问了删 agent 任务的那一次
    assert "每日安排" in question and "不会再自动运行" in question
    assert [task["id"] for task in h.app.tasks.snapshot()[0]] == [plan["id"]]
    assert h.ui.results[1] == ("用户拒绝了这个操作。", True)
    assert h.ui.results[2][1] is True and "不存在" in h.ui.results[2][0] and "task_list" in h.ui.results[2][0]


def test_tool_errors_stay_in_chinese_for_the_model(start, monkeypatch):
    monkeypatch.setattr(i18n, "current", "en")  # 界面是英文的，但写给模型的话不翻译
    h = start(
        reply(tool_use("toolu_1", "task_add", {"kind": "reminder", "title": "x", "schedule": {"type": "daily", "time": "9点"}}),
              stop_reason="tool_use"),
        reply(tool_use("toolu_2", "task_list", {}), stop_reason="tool_use"),
        reply(text("格式不对。")),
    )
    h.agent.run_turn("每天 9 点提醒我")
    error, is_error = h.ui.results[0]
    assert is_error and error == "时间要写成 24 小时制的 时:分，比如 09:00 或 21:30。"
    assert json.loads(h.ui.results[1][0])["tasks"] == []


def test_tasks_the_model_adds_always_ask_before_acting(start):
    # 被网页上的一句话带偏的模型，不能给自己定一个"到点自动执行任何命令"的任务：它加的一律每次都问，
    # 工具也没有能改这一项的参数。放宽只能由用户自己在页面上编辑任务时做
    plan = {"kind": "agent", "title": "每日安排", "prompt": "看看我的待办，写一份今天的安排", "schedule": DAILY}
    h = start(
        reply(tool_use("toolu_1", "task_add", plan), stop_reason="tool_use"),
        reply(tool_use("toolu_2", "task_add", {**plan, "title": "不用问的安排", "approval": "all"}), stop_reason="tool_use"),
        reply(text("加好了一个。")),
    )
    h.agent.run_turn("每天 9 点帮我排一下当天的事，到时候什么都别问我")

    (task,) = h.app.tasks.snapshot()[0]  # 带着 approval 的那一次什么都没加
    assert (task["title"], task["approval"]) == ("每日安排", "ask")
    assert json.loads(h.ui.results[0][0])["added"]["approval"] == "ask"
    error, is_error = h.ui.results[1]
    assert is_error and "没有 approval 这个参数" in error
    spec = next(tool for tool in h.api.requests[0]["tools"] if tool["name"] == "task_add")
    assert "approval" not in spec["input_schema"]["properties"]

    # 用户自己在页面上放宽了：模型能看到是哪一档（好回答用户"它会不会问我"），但改不了。提醒没有这一项
    h.app.tasks.update(task["id"], {**plan, "approval": "all"})
    h.app.tasks.add(reminder())
    h.api.replies += [reply(tool_use("toolu_3", "task_list", {}), stop_reason="tool_use"), reply(text("有两个任务。"))]
    h.agent.run_turn("我有哪些任务？")
    agent_view, reminder_view = json.loads(h.ui.results[2][0])["tasks"]
    assert agent_view["approval"] == "all" and "approval" not in reminder_view


def test_a_task_run_asks_as_much_as_its_own_setting_says(tmp_path):
    workspace = tmp_path / "work"
    data = workspace / "data"
    data.mkdir(parents=True)
    editor = FileEditor(workspace, data / "backups", protected=[workspace / ".env"])
    chat = Permissions(editor, private=[data])
    chat.mode = "all"  # 对话里放开了。用户想的是"我看着它做"，不是"它半夜自己做的时候也别问"
    write = ("str_replace_based_edit_tool", {"command": "create", "path": "notes.txt", "file_text": "x"})
    private = ("str_replace_based_edit_tool", {"command": "create", "path": "data/settings.json", "file_text": "{}"})
    command = ("run_command", {"command": "rm -rf build"})

    # 每一档：改工作目录里的文件、改助手自己的数据目录、执行命令（会改东西的），哪些可以不问就做
    expected = {"ask": (False, False, False), "readonly": (False, False, False), "edits": (True, False, False), "all": (True, True, True)}
    assert list(expected) == list(MODES)
    for mode, allowed in expected.items():
        run = chat.unattended(mode)
        assert run is not chat and run.mode == mode
        for (tool, tool_input), auto in zip((write, private, command), allowed):
            # 规则和对话里的一样：要问的还是要问，给用户看的说明一字不差；只是"问不问"看任务自己的那一档
            question = run.review(tool, tool_input)
            assert question is not None and question == chat.review(tool, tool_input)
            assert run.auto_allows(tool, tool_input) is auto, (mode, tool_input)

    # 没说是哪一档、或者是不认识的写法：按"每次都问"算
    for odd in ((), ("",), ("ALL",), ("always",), (None,)):
        run = chat.unattended(*odd)
        assert run.mode == "ask" and not any(run.auto_allows(*case) for case in (write, private, command))

    # 是另外一份：用户换对话的那一档碰不到正在运行的任务，反过来也一样
    run = chat.unattended("edits")
    chat.mode = "ask"
    assert run.mode == "edits" and run.auto_allows(*write)
    chat.mode = "all"
    run.mode = "ask"
    assert chat.mode == "all" and chat.auto_allows(*command) and not run.auto_allows(*write)
