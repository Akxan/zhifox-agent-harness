"""花费账本和每月预算的测试：每一次模型调用按当时的价格记一笔，重启后还在，文件坏了一行也不影响别的。"""

from __future__ import annotations

import json
import stat
from datetime import UTC, date, datetime, timedelta

from fake_api import reply, text
from fake_openai import chat

from harness import budget
from harness.ledger import Ledger
from harness.providers.base import Usage
from harness.settings import Settings

MILLION = 1_000_000
# 2026-11-20 是星期五。DeepSeek 的高峰按 UTC 写：01:00–04:00、06:00–10:00。带着时区写，在哪台电脑上跑都一样
PEAK = datetime(2026, 11, 20, 2, 0, tzinfo=UTC)
OFFPEAK = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def usage(input=0, cache_read=0, cache_write=0, output=0, searches=0) -> Usage:
    return Usage(requests=1, input_tokens=input, cache_read_tokens=cache_read, cache_write_tokens=cache_write, output_tokens=output, searches=searches)


def rows(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---- 记一笔 -------------------------------------------------------------------


def test_each_call_is_written_with_its_price_at_that_moment(tmp_path):
    path = tmp_path / "data" / "ledger.jsonl"
    ledger = Ledger(path, today=date(2026, 11, 20))
    ledger.record("anthropic", "claude-opus-5-5", usage(input=MILLION, cache_read=MILLION, cache_write=MILLION, output=MILLION, searches=2), PEAK, {})

    [row] = rows(path)
    assert row == {
        "at": "2026-11-20T02:00:00+00:00",
        "day": "2026-11-20",
        "provider": "anthropic",
        "model": "claude-opus-5-5",
        "source": "chat",
        "requests": 1,
        "input": MILLION,
        "cache_read": MILLION,
        "cache_write": MILLION,
        "output": MILLION,
        "searches": 2,
        "cost": round(4 + 0.2 + 5 + 20 + 0.02, 6),
    }
    # 账本和设置一样是私人数据：只有自己的账号能读
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert ledger.day_total(date(2026, 11, 20)) == round(29.22, 6)
    assert ledger.month_total("2026-11") == round(29.22, 6) and ledger.month_total("2026-10") == 0
    assert ledger.error == ""


def test_deepseek_is_priced_at_peak_or_off_peak_by_the_moment_of_each_call(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl", today=date(2026, 11, 20))
    ledger.record("deepseek", "deepseek-flash", usage(input=MILLION, output=MILLION), PEAK, {})
    ledger.record("deepseek", "deepseek-flash", usage(input=MILLION, output=MILLION), OFFPEAK, {})
    # 换了名字的旧模型名，闲时也按新名字的闲时价算
    ledger.record("deepseek", "deepseek-v4-flash", usage(output=MILLION), OFFPEAK, {})

    peak, off, old = rows(tmp_path / "ledger.jsonl")
    assert peak["cost"] == round(0.30 + 1.20, 6) and "offpeak" not in peak
    assert off["cost"] == round(0.15 + 0.60, 6) and off["offpeak"] is True
    assert old["cost"] == 0.6 and old["offpeak"] is True
    assert ledger.day_total(date(2026, 11, 20)) == round(1.5 + 0.75 + 0.6, 6)
    # 你自己填的价格全天都按它算，不分高峰闲时
    custom = Ledger(tmp_path / "custom.jsonl", today=date(2026, 11, 20))
    custom.record("deepseek", "deepseek-flash", usage(input=MILLION), OFFPEAK, {"deepseek-flash": {"input": 1, "output": 2}})
    assert rows(tmp_path / "custom.jsonl")[0]["cost"] == 1


def test_models_without_a_price_keep_their_tokens_but_no_cost(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl", today=date(2026, 11, 20))
    ledger.record("openai", "gpt-9-mystery", usage(input=500, cache_read=100, output=70), PEAK, {})
    ledger.record("anthropic", "claude-sonnet-5-5", usage(output=MILLION), PEAK, {})

    unpriced = rows(tmp_path / "ledger.jsonl")[0]
    assert unpriced["cost"] is None and unpriced["input"] == 500 and unpriced["output"] == 70
    assert ledger.day_total(date(2026, 11, 20)) == 10  # 没价格的那一次不算钱

    period = ledger.report(date(2026, 11, 20), 30)["periods"]["month"]
    assert [row["model"] for row in period["models"]] == ["claude-sonnet-5-5"]
    assert period["unpriced"] == [
        {"provider": "openai", "model": "gpt-9-mystery", "requests": 1, "input": 500, "cache_read": 100, "cache_write": 0, "output": 70, "searches": 0, "cost": 0}
    ]
    assert period["requests"] == 2 and period["cost"] == 10
    assert ledger.report(date(2026, 11, 20), 30)["days"][-1]["unpriced"] == 1


def test_old_entries_keep_the_price_they_were_recorded_at(tmp_path):
    # 后来改了价格（或者给没价格的模型填上了价格），旧账不跟着变；新填的价格从下一笔开始算
    ledger = Ledger(tmp_path / "ledger.jsonl", today=date(2026, 11, 20))
    prices: dict[str, dict] = {}
    record = ledger.recorder(lambda: prices)
    record("openai", "gpt-9-mystery", usage(input=MILLION), PEAK)
    prices["gpt-9-mystery"] = {"input": 3, "output": 6}
    record("openai", "gpt-9-mystery", usage(input=MILLION), PEAK)

    first, second = rows(tmp_path / "ledger.jsonl")
    assert first["cost"] is None and second["cost"] == 3
    period = ledger.report(date(2026, 11, 20), 30)["periods"]["all"]
    # 同一个模型，有价格的部分和没价格的部分分开列，没价格的那次不会被当成 0 元算进金额
    assert [(row["model"], row["requests"], row["cost"]) for row in period["models"]] == [("gpt-9-mystery", 1, 3)]
    assert [(row["model"], row["requests"]) for row in period["unpriced"]] == [("gpt-9-mystery", 1)]


def test_sources_tell_chats_from_scheduled_tasks(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl", today=date(2026, 11, 20))
    ledger.recorder(dict)("anthropic", "claude-opus-5-5", usage(output=MILLION), PEAK)
    task = ledger.recorder(dict, source="task")  # 定时任务给自己的 agent 接上这一个
    task("anthropic", "claude-opus-5-5", usage(output=MILLION), PEAK)
    task("anthropic", "claude-opus-5-5", usage(output=MILLION), PEAK)

    assert [row["source"] for row in rows(tmp_path / "ledger.jsonl")] == ["chat", "task", "task"]
    sources = ledger.report(date(2026, 11, 20), 30)["periods"]["month"]["sources"]
    assert [(row["source"], row["requests"], row["cost"]) for row in sources] == [("task", 2, 40), ("chat", 1, 20)]


# ---- 汇总 ---------------------------------------------------------------------


def test_report_adds_up_by_day_month_and_model(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl", today=date(2026, 11, 20))
    today = date(2026, 11, 20)
    calls = [
        (datetime(2026, 11, 20, 9), "claude-opus-5-5", usage(output=MILLION)),  # 20 美元
        (datetime(2026, 11, 20, 18), "claude-sonnet-5-5", usage(output=MILLION)),  # 10
        (datetime(2026, 11, 3, 9), "claude-sonnet-5-5", usage(output=MILLION)),  # 10
        (datetime(2026, 10, 31, 23, 59), "claude-opus-5-5", usage(input=MILLION)),  # 4：上个月，也在最近 30 天里
        (datetime(2026, 9, 1, 8), "claude-opus-5-5", usage(input=MILLION)),  # 4：两个多月前
    ]
    for moment, model, used in calls:
        ledger.record("anthropic", model, used, moment, {})

    report = ledger.report(today, 30)
    assert report["today"] == "2026-11-20" and report["first"] == "2026-09-01"
    days = report["days"]
    assert len(days) == 30 and days[0]["day"] == "2026-10-22" and days[-1]["day"] == "2026-11-20"
    assert {day["day"]: day["cost"] for day in days if day["cost"]} == {"2026-10-31": 4, "2026-11-03": 10, "2026-11-20": 30}
    assert days[-1]["requests"] == 2
    assert [(month["month"], month["cost"]) for month in report["months"]] == [("2026-11", 40), ("2026-10", 4), ("2026-09", 4)]

    month, recent, everything = (report["periods"][name] for name in ("month", "recent", "all"))
    assert (month["cost"], recent["cost"], everything["cost"]) == (40, 44, 48)
    # 金额一样时，调用次数多的排前面
    assert [(row["model"], row["cost"], row["requests"]) for row in month["models"]] == [("claude-sonnet-5-5", 20, 2), ("claude-opus-5-5", 20, 1)]
    assert [(row["model"], row["cost"]) for row in everything["models"]] == [("claude-opus-5-5", 28), ("claude-sonnet-5-5", 20)]
    assert everything["requests"] == 5
    assert ledger.day_total(today) == 30 and ledger.month_total("2026-11") == 40


def test_an_empty_ledger_reports_zeros(tmp_path):
    report = Ledger(tmp_path / "nothing-yet.jsonl").report(date(2026, 11, 20), 7)
    assert report["first"] is None and report["months"] == [] and len(report["days"]) == 7
    assert all(day["cost"] == 0 for day in report["days"])
    assert report["periods"]["all"]["models"] == [] and report["skipped"] == 0
    assert not (tmp_path / "nothing-yet.jsonl").exists()  # 只是看一眼，不会凭空建一个文件


# ---- 重启、坏掉的文件 ----------------------------------------------------------


def test_the_ledger_is_still_there_after_a_restart(tmp_path):
    path = tmp_path / "ledger.jsonl"
    first = Ledger(path, today=date(2026, 11, 20))
    first.record("anthropic", "claude-opus-5-5", usage(output=MILLION), datetime(2026, 11, 20, 9), {})
    first.recorder(dict, source="task")("deepseek", "deepseek-flash", usage(output=MILLION), PEAK)

    again = Ledger(path, today=date(2026, 11, 20))
    assert again.day_total(date(2026, 11, 20)) == round(20 + 1.2, 6)
    assert again.report(date(2026, 11, 20), 30) == first.report(date(2026, 11, 20), 30)


def test_a_half_written_last_line_is_skipped_and_the_next_entry_starts_a_new_line(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path, today=date(2026, 11, 20))
    ledger.record("anthropic", "claude-opus-5-5", usage(output=MILLION), datetime(2026, 11, 20, 9), {})
    # 程序在写下一笔的途中崩溃了：文件末尾留下半行，连换行符都没有（还把一个汉字截成了两半）
    with path.open("ab") as handle:
        handle.write('{"at": "2026-11-20T10:00:00", "day": "2026-11-20", "model": "半'.encode()[:-1])

    again = Ledger(path, today=date(2026, 11, 20))
    assert again.skipped == 1 and again.day_total(date(2026, 11, 20)) == 20
    again.record("anthropic", "claude-sonnet-5-5", usage(output=MILLION), datetime(2026, 11, 20, 11), {})

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    assert len(lines) == 3 and json.loads(lines[2])["model"] == "claude-sonnet-5-5"  # 新的一笔另起一行，没和半行粘在一起
    third = Ledger(path, today=date(2026, 11, 20))
    assert third.skipped == 1 and third.day_total(date(2026, 11, 20)) == 30


def test_lines_that_do_not_make_sense_are_skipped(tmp_path):
    path = tmp_path / "ledger.jsonl"
    good = {"day": "2026-11-20", "provider": "anthropic", "model": "claude-opus-5-5", "source": "chat", "requests": 1, "output": 5, "cost": 1.5}
    bad = [
        "这不是 JSON",
        "[1, 2, 3]",
        json.dumps({**good, "day": "20/11/2026"}),
        json.dumps({**good, "output": -5}),
        json.dumps({**good, "output": "5"}),
        json.dumps({**good, "requests": True}),
        json.dumps({**good, "cost": -1}),
        json.dumps({**good, "cost": "1.5"}),
        json.dumps({**good, "model": ""}),
        json.dumps({**good, "provider": None}),
        '{"day": "2026-11-20", "provider": "a", "model": "m", "cost": NaN}',
        '{"day": "2026-11-20", "provider": "a", "model": "m", "cost": 1e999}',
    ]
    future = {**good, "region": "eu", "cost": 2}  # 以后的版本多记了一个字段：照样读得懂
    path.write_text("\n".join([json.dumps(good), *bad, "", json.dumps(future)]) + "\n", encoding="utf-8")

    ledger = Ledger(path, today=date(2026, 11, 20))
    assert ledger.skipped == len(bad)
    assert ledger.day_total(date(2026, 11, 20)) == 3.5


def test_a_ledger_that_cannot_be_written_does_not_stop_the_conversation(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.mkdir()  # 文件的位置被一个文件夹占了：怎么都写不进去
    ledger = Ledger(path, today=date(2026, 11, 20))
    ledger.record("anthropic", "claude-opus-5-5", usage(output=MILLION), datetime(2026, 11, 20, 9), {})
    assert ledger.error  # 原因留着，页面上会显示
    assert ledger.day_total(date(2026, 11, 20)) == 20  # 这次运行里照样算进去


# ---- 合并旧明细 ----------------------------------------------------------------


def test_old_details_are_merged_per_day_and_totals_do_not_change(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path, detail_days=1000, today=date(2026, 10, 1))  # 先攒一些明细：写的时候不合并
    for hour in (8, 9, 10):
        ledger.record("anthropic", "claude-opus-5-5", usage(input=100, output=MILLION), datetime(2026, 10, 1, hour), {})
    ledger.recorder(dict, source="task")("anthropic", "claude-opus-5-5", usage(output=MILLION), datetime(2026, 10, 1, 11))
    ledger.record("openai", "gpt-9-mystery", usage(input=10), datetime(2026, 10, 1, 12), {})
    ledger.record("anthropic", "claude-opus-5-5", usage(output=MILLION), datetime(2026, 11, 19, 9), {})
    with path.open("a", encoding="utf-8") as handle:
        handle.write("不认识的一行\n")
    before = Ledger(path, detail_days=10, today=date(2026, 10, 1)).report(date(2026, 11, 20), 60)
    assert len(rows_or_text(path)) == 7

    # 过了 10 天以上再启动：10 月 1 日的五笔明细合并成三行（对话、定时任务、没价格的各一行）
    after = Ledger(path, detail_days=10, today=date(2026, 11, 20))
    lines = rows_or_text(path)
    merged = [line for line in lines if isinstance(line, dict) and line["day"] == "2026-10-01"]
    assert len(merged) == 3 and all("at" not in line for line in merged)
    chat_row = next(line for line in merged if line["source"] == "chat" and line["cost"] is not None)
    assert chat_row["requests"] == 3 and chat_row["input"] == 300 and chat_row["cost"] == round(60 + 300 * 4 / MILLION, 6)
    assert next(line for line in merged if line["cost"] is None)["model"] == "gpt-9-mystery"
    assert "不认识的一行" in lines  # 读不懂的不删
    assert any(isinstance(line, dict) and line.get("at") == "2026-11-19T09:00:00" for line in lines)  # 新的明细原样留着
    assert after.report(date(2026, 11, 20), 60) == {**before, "skipped": 1}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    # 已经合并过的不会再动：再启动一次，文件一字不变
    snapshot = path.read_bytes()
    Ledger(path, detail_days=10, today=date(2026, 11, 21))
    assert path.read_bytes() == snapshot


def test_merging_also_happens_when_a_long_running_program_crosses_midnight(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path, detail_days=1, today=date(2026, 11, 18))
    ledger.record("anthropic", "claude-opus-5-5", usage(output=MILLION), datetime(2026, 11, 18, 9), {})
    ledger.record("anthropic", "claude-opus-5-5", usage(output=MILLION), datetime(2026, 11, 18, 10), {})
    assert len(rows(path)) == 2
    # 程序一直开着，两天后的第一笔记账时顺便合并
    ledger.record("anthropic", "claude-opus-5-5", usage(output=MILLION), datetime(2026, 11, 20, 9), {})
    old, new = rows(path)
    assert old["requests"] == 2 and "at" not in old and new["at"] == "2026-11-20T09:00:00"
    assert ledger.month_total("2026-11") == 60


def rows_or_text(path) -> list:
    out: list = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append(line)
    return out


# ---- 每月预算 ------------------------------------------------------------------


def test_the_budget_alert_fires_once_per_month_and_again_only_when_raised():
    month = "2026-11"
    assert not budget.should_alert(49.99, 50, None, month)
    assert not budget.should_alert(80, None, None, month)  # 没设预算，从不提醒
    assert budget.should_alert(50, 50, None, month)  # 正好花到预算也算超了
    assert not budget.should_alert(70, 50, {"month": month, "amount": 50}, month)  # 提醒过了
    assert not budget.should_alert(70, 40, {"month": month, "amount": 50}, month)  # 调低了：你已经知道超过了更高的那个数
    assert not budget.should_alert(70, 100, {"month": month, "amount": 50}, month)  # 调高了，还没花到新的数
    assert budget.should_alert(100, 100, {"month": month, "amount": 50}, month)  # 调高以后又超过了
    assert budget.should_alert(60, 50, {"month": "2026-10", "amount": 50}, month)  # 新的一个月重新开始

    assert budget.marker({"month": month, "amount": 50}, month) == "2026-11:50"
    assert budget.marker({"month": "2026-10", "amount": 50}, month) is None
    assert budget.marker(None, month) is None
    assert budget.month_of(date(2026, 1, 5)) == "2026-01"


def test_budget_values_are_checked():
    assert budget.valid_budget(None) and budget.valid_budget(50) and budget.valid_budget(0.5)
    for bad in (0, -1, float("nan"), float("inf"), True, "50", budget.MAX_BUDGET + 1, [50]):
        assert not budget.valid_budget(bad), bad


def test_the_budget_is_kept_in_settings(tmp_path):
    path = tmp_path / "settings.json"
    settings = Settings(path, "anthropic", "claude-opus-5-5")
    assert settings.budget is None and settings.budget_alert is None
    settings.set_budget(25.5)
    settings.set_budget_alert({"month": "2026-11", "amount": 25.5})
    again = Settings(path, "anthropic", "claude-opus-5-5")
    assert again.budget == 25.5 and again.budget_alert == {"month": "2026-11", "amount": 25.5}

    # 文件被改坏了的预算不认，当作没设
    data = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**data, "budget": -3, "budget_alert": {"month": 7}}), encoding="utf-8")
    broken = Settings(path, "anthropic", "claude-opus-5-5")
    assert broken.budget is None and broken.budget_alert is None


# ---- 接在 agent 上 -------------------------------------------------------------


def test_every_model_call_of_a_turn_lands_in_the_ledger(start):
    # 假 API 每次报告：输入 100（原价）+ 缓存命中 40 + 写入缓存 10，输出若干
    h = start(reply(text("你好！")))
    h.agent.run_turn("你好")

    [row] = rows(h.config.ledger_file)
    assert (row["provider"], row["model"], row["source"], row["day"]) == ("anthropic", "claude-opus-5-5", "chat", "2026-11-20")
    assert row["at"] == "2026-11-20T14:03:00"  # 和 agent 用的是同一个钟
    assert (row["input"], row["cache_read"], row["cache_write"]) == (100, 40, 10)
    expected = (100 * 4 + 40 * 0.2 + 10 * 5 + row["output"] * 20) / MILLION
    assert row["cost"] == round(expected, 6)
    spend = h.app.spending()
    assert spend["day"] == "2026-11-20" and spend["today"] == spend["month"] == round(expected, 6)
    assert spend["budget"] is None and spend["over"] is False and spend["alert"] is None and spend["error"] == ""


def test_deepseek_calls_through_the_agent_use_the_moment_of_the_call(start_openai):
    h = start_openai(chat("你好"), chat("又见面了"))
    h.agent.clock = lambda: PEAK
    h.agent.run_turn("你好")
    h.agent.clock = lambda: OFFPEAK
    h.agent.run_turn("再来一次")
    peak, off = rows(h.config.ledger_file)
    assert peak["provider"] == "deepseek" and "offpeak" not in peak and off["offpeak"] is True
    assert peak["cost"] > off["cost"] > 0
    # 本次运行的估算和账本按同一个时刻算价格：两边加起来一样
    from harness.pricing import estimate

    assert round(estimate(h.agent.usage_by_model, {})[0], 6) == round(peak["cost"] + off["cost"], 6)


def test_crossing_the_budget_is_remembered_so_it_is_only_flagged_once(start):
    h = start(reply(text("一")), reply(text("二")))
    h.agent.run_turn("你好")
    spent = h.app.spending()["month"]
    h.app.settings.set_budget(spent / 2)  # 预算比已经花的还少：马上算超了
    first = h.app.spending()
    assert first["over"] is True and first["alert"] == f"2026-11:{spent / 2}"
    assert Settings(h.config.settings_file, "anthropic", "x").budget_alert == {"month": "2026-11", "amount": spent / 2}  # 写进了设置，重启也记得

    h.app.settings.set_budget(spent * 10)  # 调高了，还没超
    assert h.app.spending()["over"] is False and h.app.spending()["alert"] == first["alert"]  # 记号不变：页面不会再弹
    h.app.settings.set_budget(spent * 1.5)  # 调到刚好会被下一次调用越过
    h.agent.run_turn("再说一句")
    again = h.app.spending()
    assert again["over"] is True and again["alert"] == f"2026-11:{spent * 1.5}"  # 新的记号：页面会再弹一次


def test_the_next_month_starts_from_zero(start):
    h = start(reply(text("一")))
    h.agent.run_turn("你好")
    h.agent.clock = lambda: datetime(2026, 12, 1, 9) + timedelta(minutes=1)
    spend = h.app.spending()
    assert spend["day"] == "2026-12-01" and spend["today"] == 0 and spend["month"] == 0
