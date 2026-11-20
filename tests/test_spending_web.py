"""网页界面上的花费、预算和导出：状态里的今天和本月、/api/ledger、/api/budget、/api/export。

用的是 test_webui.py 里的那一套：真实的服务器、本地的假 API，测试代码扮演浏览器。
"""

from __future__ import annotations

import http.client
import io
import json
import zipfile

import test_webui
from fake_api import reply, text
from test_webui import request

from harness.settings import Settings

serve = test_webui.serve  # pytest 的 fixture：放在这个模块里就能用，和 test_webui.py 用的是同一个


def say(s, message: str) -> dict:
    """发一条消息，等这一轮结束，返回最后的状态。"""
    assert s.browser.post("/api/message", {"text": message})[0] == 200
    s.browser.wait_for("turn_end")
    return s.browser.of_type("state")[-1]


def download(s, headers: dict | None = None) -> tuple[int, dict, bytes]:
    """像页面里的 fetch 一样请求导出，拿回原始的字节（压缩包不是文字，不能当 JSON 读）。"""
    connection = http.client.HTTPConnection(s.browser.host, timeout=10)
    sent = {"X-Harness-Token": s.browser.token, "Origin": f"http://{s.browser.host}", "Content-Type": "application/json"}
    connection.request("POST", "/api/export", body="{}", headers=headers if headers is not None else sent)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, dict(response.getheaders()), body


def test_state_shows_today_and_this_month_from_the_ledger(serve):
    s = serve(reply(text("你好！")), reply(text("又见面了。")))
    first = s.browser.of_type("state")[0]["spend"]
    assert first == {"day": first["day"], "today": 0, "month": 0, "budget": None, "over": False, "alert": None, "error": ""}

    after = say(s, "你好")
    spend = after["spend"]
    assert spend["today"] > 0 and spend["month"] == spend["today"]
    assert round(after["cost"], 4) == round(spend["today"], 4)  # 本次运行的估算和账本是同一笔钱

    # 程序重启（新的服务器、同一个数据目录）：本次运行的估算从零开始，今天和本月还在
    again = serve(reply(text("重启以后")), data_dir=s.config.data_dir)
    state = again.browser.of_type("state")[-1]
    assert state["cost"] == 0 and state["spend"]["today"] == spend["today"]


def test_the_spending_report(serve):
    s = serve(reply(text("你好！")))
    status, body = s.browser.post("/api/ledger")
    assert status == 200 and body["report"]["first"] is None and len(body["report"]["days"]) == 30

    say(s, "你好")
    report = s.browser.post("/api/ledger")[1]["report"]
    assert report["first"] == report["today"] == report["days"][-1]["day"]
    assert report["days"][-1]["requests"] == 1
    month = report["periods"]["month"]
    assert [(row["provider"], row["model"]) for row in month["models"]] == [("anthropic", "claude-opus-5-5")]
    assert [row["source"] for row in month["sources"]] == ["chat"]
    assert report["error"] == "" and report["skipped"] == 0


def test_setting_the_budget_and_the_alert_it_raises(serve):
    s = serve(reply(text("你好！")))
    spent = say(s, "你好")["spend"]["month"]

    status, body = s.browser.post("/api/budget", {"amount": 50})
    assert status == 200 and body["spend"]["budget"] == 50 and body["spend"]["over"] is False
    state = s.browser.wait_for("state")
    assert state["spend"]["budget"] == 50 and state["spend"]["over"] is False and state["spend"]["alert"] is None

    # 预算比这个月已经花的还少：马上算超了，记号写进设置（重启以后也不会再提醒一遍）
    assert s.browser.post("/api/budget", {"amount": spent / 2})[1]["spend"]["over"] is True  # 页面当场就能说"已经超了"
    state = s.browser.wait_for("state")
    assert state["spend"]["over"] is True and state["spend"]["alert"] == f"{state['spend']['day'][:7]}:{spent / 2}"
    saved = Settings(s.config.settings_file, "anthropic", "x")
    assert saved.budget == spent / 2 and saved.budget_alert["amount"] == spent / 2

    # 不设预算：明说 null
    assert s.browser.post("/api/budget", {"amount": None})[1]["spend"]["budget"] is None
    state = s.browser.wait_for("state")
    assert state["spend"]["budget"] is None and state["spend"]["over"] is False
    assert Settings(s.config.settings_file, "anthropic", "x").budget is None


def test_bad_budgets_are_refused(serve):
    s = serve()
    for bad in ({}, {"amount": 0}, {"amount": -5}, {"amount": "50"}, {"amount": True}, {"amount": 10**9}, {"amount": [1]}):
        status, body = s.browser.post("/api/budget", bad)
        assert status == 400 and "预算" in body["error"], bad
    assert s.app.settings.budget is None


def test_export_info_and_download(serve):
    s = serve(reply(text("你好！")))
    say(s, "你好")
    s.browser.post("/api/budget", {"amount": 50})  # 这样设置文件也有了
    # 万一有人把密钥文件放进了数据文件夹：不导出
    fake_key = "sk-" + "ant-api03-" + "SHOULDNOTLEAK" * 2  # 拼起来写：源文件里不出现一个像真密钥的整串
    (s.config.data_dir / ".env").write_text(f"ANTHROPIC_API_KEY={fake_key}\n", encoding="utf-8")
    s.config.env_file.write_text(f"ANTHROPIC_API_KEY={fake_key}\n", encoding="utf-8")

    status, info = s.browser.post("/api/export/info")
    assert status == 200 and info["files"] >= 3 and info["bytes"] > 0 and info["path"] == str(s.config.data_dir)

    status, headers, body = download(s)
    assert status == 200 and headers["Content-Type"] == "application/zip"
    assert headers["Content-Length"] == str(len(body)) and headers["Cache-Control"] == "no-store"
    assert headers["Content-Disposition"].startswith('attachment; filename="zhifox-backup-')
    archive = zipfile.ZipFile(io.BytesIO(body))
    names = archive.namelist()
    conversation = f"conversations/{s.app.conversation.id}.json"
    assert conversation in names and "ledger.jsonl" in names and "settings.json" in names and "README-backup.txt" in names
    assert json.loads(archive.read(conversation))["title"] == "你好"
    assert json.loads(archive.read("settings.json"))["budget"] == 50
    assert ".env" not in names and ".lock" not in names
    assert b"SHOULDNOTLEAK" not in body and b"test-key" not in body
    # 压缩包写在系统的临时文件里，导出以后数据文件夹里没有多出任何东西
    assert not [path for path in s.config.data_dir.rglob("*") if path.suffix in (".zip", ".tmp")]


def test_export_needs_the_token_and_the_right_origin(serve):
    s = serve()
    assert download(s, headers={})[0] == 403
    assert download(s, headers={"X-Harness-Token": "wrong"})[0] == 403
    assert download(s, headers={"X-Harness-Token": s.browser.token, "Origin": "http://evil.example"})[0] == 403
    assert request(s.browser.host, "GET", f"/api/export?token={s.browser.token}")[0] == 404  # 只有带口令请求头的 POST 才行
