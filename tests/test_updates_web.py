"""网页上的"检查新版本"（harness/webui/updates_api.py）和 harness serve、自检、导出怎么对待它：

  - 状态里的 updates：默认关着，什么都不问
  - /api/updates 打开（马上问一次，结果随状态发过来）、关上；/api/updates/check 马上再问一次
  - 网页和 harness serve 开着的时候，后台线程只在打开以后才问，一天一次
  - harness doctor 只读缓存，永远不联网
  - 导出不带缓存，恢复也不写它

用的是 test_webui.py 里的那一套（真实的服务器、测试代码扮演浏览器），问的是本地的假 GitHub。
"""

from __future__ import annotations

import io
import json
import threading
import time
import zipfile
from datetime import datetime

import pytest
import test_webui
from fake_api import reply, text
from fake_github import FakeGitHub
from test_updates import until

from harness import export, machine, updates
from harness.config import EXIT_OK
from harness.doctor import OK, UNUSED, environment
from harness.doctor import examine as doctor_examine
from harness.serve import serve as run_serve
from harness.settings import Settings
from harness.webui import lifecycle

serve = test_webui.serve  # pytest 的 fixture：放在这个模块里就能用


@pytest.fixture
def github():
    servers: list[FakeGitHub] = []

    def start(*args, **kwargs) -> FakeGitHub:
        server = FakeGitHub(*args, **kwargs).start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.stop()


def latest_state(s) -> dict:
    return s.browser.of_type("state")[-1]["updates"]


def wait_state(s, condition, what: str) -> dict:
    def found() -> dict | None:
        info = latest_state(s)
        return info if condition(info) else None

    return until(found, what=what)


def test_off_by_default_and_nothing_is_asked(serve, github):
    fake = github("99.0.0")
    s = serve(reply(text("你好")), update_api=fake.base_url)
    info = latest_state(s)
    assert info["enabled"] is False and info["latest"] is None and info["upgrade"] is None and info["checked_at"] is None
    assert info["current"]  # 现在这一版（从仓库里运行时读 pyproject.toml）
    checker = s.server.web.updates.updater
    checker.start()  # 和 lifecycle.start 一样开起后台线程：关着就只是等着
    time.sleep(0.3)
    checker.stop()
    assert fake.count == 0


def test_turning_it_on_asks_and_shows_the_new_version(serve, github):
    fake = github("99.0.0")
    s = serve(reply(text("你好")), update_api=fake.base_url)
    status, body = s.browser.post("/api/updates", {"enabled": True})
    assert status == 200 and body["ok"] is True
    info = wait_state(s, lambda info: info["latest"] is not None, "状态里出现新版本")
    assert fake.count == 1
    assert info["enabled"] is True and info["checking"] is False and info["problem"] is None
    assert info["latest"] == {"version": "99.0.0", "published": "2026-12-01T09:00:00Z", "page": "https://github.com/Akxan/zhifox-agent-harness/releases/tag/v99.0.0"}
    plan = info["upgrade"]
    assert plan["commands"] == ["git pull", "uv sync"] and plan["in_project"] is True  # 测试是从仓库里运行的
    assert plan["lead"]["key"] and plan["lead"]["text"]  # 带着原文：页面换语言时就地重译
    assert all(note["key"] for note in plan["notes"])
    assert Settings(s.config.settings_file, "anthropic", "x").update_check is True

    # 马上再问一次（不管缓存新不新）
    assert s.browser.post("/api/updates/check")[0] == 200
    until(lambda: fake.count == 2 and not latest_state(s)["checking"], what="又问了一次")

    # 关上：不再说有新版本，也不再问
    assert s.browser.post("/api/updates", {"enabled": False})[0] == 200
    info = wait_state(s, lambda info: not info["enabled"], "关上以后的状态")
    assert info["latest"] is None and info["upgrade"] is None
    assert s.browser.post("/api/updates/check")[0] == 409
    assert fake.count == 2


def test_a_failed_check_is_shown_and_translatable(serve, github):
    fake = github(status=403, headers={"X-RateLimit-Remaining": "0"})
    s = serve(reply(text("你好")), update_api=fake.base_url)
    s.browser.post("/api/updates", {"enabled": True})
    info = wait_state(s, lambda info: info["problem"] is not None, "没能检查")
    assert info["problem"]["key"] == updates.PROBLEMS["limited"] and info["latest"] is None
    assert s.browser.post("/api/language", {"language": "en"})[0] == 200
    info = wait_state(s, lambda info: info["problem"]["text"].startswith("GitHub won't"), "换成英文以后的原因")


def test_bad_requests(serve, github):
    fake = github()
    s = serve(reply(text("你好")), update_api=fake.base_url)
    assert s.browser.post("/api/updates", {"enabled": "yes"})[0] == 400
    assert s.browser.post("/api/updates", {})[0] == 400
    assert s.browser.post("/api/updates/nope")[0] == 404
    assert fake.count == 0


def test_a_background_service_is_told_to_restart(serve, github, monkeypatch):
    fake = github("99.0.0")
    s = serve(reply(text("你好")), update_api=fake.base_url)
    monkeypatch.setattr(machine, "system", lambda: machine.MAC)
    s.server.web.service = "launchd"
    s.browser.post("/api/updates", {"enabled": True})
    info = wait_state(s, lambda info: info["upgrade"] is not None, "怎么升级")
    assert "service restart" in info["upgrade"]["notes"][-1]["text"]


def test_the_lifecycle_starts_and_stops_the_checker(serve, github):
    fake = github("99.0.0")
    s = serve(reply(text("你好")), update_api=fake.base_url)
    web = s.server.web
    web.app.settings.set_update_check(True)
    lifecycle.start(web)
    try:
        until(lambda: fake.count == 1, what="开起来以后问了一次")
        time.sleep(0.3)
        assert fake.count == 1  # 一天只问一次
    finally:
        lifecycle.stop(web, 1)
    assert not web.updates.updater._thread.is_alive()


def _serve_for_a_while(config, until_asked: FakeGitHub | None = None) -> list[int]:
    """在一个线程里跑 harness serve：until_asked 给了的话等它被问了一次，没给就跑半秒。然后像收到退出信号那样停下。"""
    stop = threading.Event()
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(run_serve(config, stop)), daemon=True)
    thread.start()
    try:
        if until_asked is not None:
            until(lambda: until_asked.count == 1, what="serve 开起来以后问了一次")
        else:
            time.sleep(0.5)
    finally:
        stop.set()
        thread.join(15)
    return result


def test_harness_serve_checks_only_when_turned_on(make_config, github):
    fake = github("99.0.0")
    config = make_config(update_api=fake.base_url)
    assert _serve_for_a_while(config) == [EXIT_OK] and fake.count == 0  # 默认关着：一个请求都不发
    Settings(config.settings_file, "anthropic", "x").set_update_check(True)
    assert _serve_for_a_while(config, until_asked=fake) == [EXIT_OK] and fake.count == 1


# ---- 自检只读缓存 ----------------------------------------------------------------


def test_doctor_reads_only_the_cache(make_config, github):
    fake = github("99.0.0")
    config = make_config(update_api=fake.base_url)
    settings = Settings(config.settings_file, "anthropic", "x")
    check = environment.updates(config, settings)
    assert check.status == UNUSED and "updates on" in check.fix[0]

    settings.set_update_check(True)
    check = environment.updates(config, settings)
    assert check.status == OK and "还没有检查过" in check.detail
    # 缓存是一个月以前的（早就过期了）：自检照样只说缓存里的，不去问
    old = time.time() - 30 * 86400
    updates.write_cache(config.updates_file, updates.Cache(old, updates.Release("99.0.0", "v99.0.0", "", True)))
    check = environment.updates(config, settings)
    assert check.status == OK and "99.0.0" in check.detail
    assert any("git pull" in line for line in check.fix) and any("releases/tag/v99.0.0" in line for line in check.fix)
    assert check.facts["newer"] is True and check.facts["latest"] == "99.0.0"

    report = doctor_examine(config)
    (found,) = [item for item in report.checks() if item.id == "updates"]
    assert found.status == OK and report.sections[0].checks[2] is found  # 紧跟在版本那一行后面
    updates.write_cache(config.updates_file, updates.Cache(old, problem="network", detail="timed out"))
    assert "没能检查" in environment.updates(config, settings).detail
    assert fake.count == 0


# ---- 导出、恢复 ------------------------------------------------------------------


def test_the_cache_is_not_exported(tmp_path):
    data = tmp_path / "data"
    updates.write_cache(data / "updates.json", updates.Cache(time.time(), updates.Release("0.3.0", "v0.3.0", "", True)))
    (data / "todos.json").write_text('{"items": []}', encoding="utf-8")
    work = export.plan(data)
    names = [name for _, name in work.files]
    assert "updates.json" not in names and "todos.json" in names
    assert not any("updates.json" in name for name, _ in work.skipped)  # 只是缓存：不导出，也不提
    buffer = io.BytesIO()
    export.write_archive(data, buffer, datetime.now())
    with zipfile.ZipFile(buffer) as archive:
        assert "updates.json" not in archive.namelist()
        assert "updates.json" not in json.loads(archive.read(export.MANIFEST))["left_out"]
    assert export.part_of("updates.json") is None  # 恢复的白名单里没有它：恢复从不写它
