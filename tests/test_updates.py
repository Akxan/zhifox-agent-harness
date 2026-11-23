"""检查新版本（harness/updates.py）：版本号怎么比、缓存什么时候算新、关着的时候一个请求都不发、打开着一天只问一次、
预发布不算、没问成（看不懂的回答、超时、被限流）只是安静地记下、怎么升级按装法和系统说对、命令行的 harness updates。

问的都是本地的假 GitHub（tests/fake_github.py）：conftest 的 no_real_github 保证一个请求都到不了真的 api.github.com。
钟是假的（Updater 的 clock），"过了一天"不用真的等。
"""

from __future__ import annotations

import io
import json
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fake_github import FakeGitHub
from modes import has_mode

from harness import cli, i18n, install, machine, updates, updates_cli
from harness.config import UPDATE_CHECK_HOURS, UPDATE_RETRY_HOURS
from harness.settings import Settings
from harness.updates import Cache, Release, Updater

DAY = UPDATE_CHECK_HOURS * 3600


def until(condition, timeout: float = 10, what: str = "条件"):
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"等了 {timeout:g} 秒，{what}还是不成立")
        time.sleep(0.02)


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


class Clock:
    """假的钟：now 是 time.time() 那样的秒数，tick 往前拨。"""

    def __init__(self, now: float = 1_795_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


def make(make_config, fake: FakeGitHub | None = None, *, enabled: bool = True, current: str = "0.2.0", clock: Clock | None = None, **overrides):
    config = make_config(**{"update_api": fake.base_url if fake else "https://api.github.com", **overrides})
    settings = Settings(config.settings_file, config.default_provider, config.default_model)
    if enabled:
        settings.set_update_check(True)
    changes: list[int] = []
    updater = Updater(config, settings, on_change=lambda: changes.append(1), clock=clock or Clock(), current=current)
    return SimpleNamespace(config=config, settings=settings, updater=updater, changes=changes, clock=updater.clock)


# ---- 版本号 ----------------------------------------------------------------------


@pytest.mark.parametrize(("candidate", "current", "newer"), [
    ("v0.3.0", "0.2.0", True),
    ("0.3.0", "0.2.0", True),
    ("v0.10.0", "0.9.0", True),  # 按数字比：按字符串比的话 "0.10.0" < "0.9.0"
    ("v0.9.0", "0.10.0", False),
    ("v1.0", "1.0.0", False),  # 1.0 和 1.0.0 是同一版
    ("v1.0.1", "1.0", True),
    ("v0.2.0", "0.2.0", False),
    ("v0.1.9", "0.2.0", False),
    ("v0.3.0", "0.3.0rc1", True),  # 装的是预发布：同样数字的正式版本更新
    ("v0.3.0", "0.3.0.dev2", True),
    ("v0.3.0", "0.3.0.post1", False),  # 正式版之后的小修补不比它旧
    ("v0.3.0", "0.3.0+local", False),
    ("v0.3.0-rc.1", "0.2.0", False),  # 预发布的 tag 不算
    ("0.3.0b1", "0.2.0", False),
    ("nightly", "0.2.0", False),
    ("v0.3.0", "", False),  # 不知道现在是哪一版：不提醒
    ("v0.3.0", "?", False),
])
def test_versions_are_compared_by_number(candidate, current, newer):
    assert updates.is_newer(candidate, current) is newer


def test_only_plain_numbered_tags_are_releases():
    assert updates.stable("v0.3.0") == (0, 3, 0)
    assert updates.stable(" V1.2 ") == (1, 2)
    for tag in ("v0.3.0-rc.1", "v1.2.3.4.5", "v0.3.0/../x", "v0.3.0?x", "", None, 3):
        assert updates.stable(tag) is None, tag


# ---- GitHub 的回答 ---------------------------------------------------------------


def test_reading_a_release():
    found = updates.read_release({
        "tag_name": "v0.3.0", "published_at": "2026-12-01T09:00:00Z", "html_url": "https://evil.example/",
        "assets": [{"name": install.wheel_name("0.3.0")}],
    })
    assert found == Release("0.3.0", "v0.3.0", "2026-12-01T09:00:00Z", True)
    # 链接永远照着 tag 拼，指向 github.com 上的这个仓库，不用回答里给的网址
    assert found.page == "https://github.com/Akxan/zhifox-agent-harness/releases/tag/v0.3.0"
    assert found.wheel_url == "https://github.com/Akxan/zhifox-agent-harness/releases/download/v0.3.0/harness_assistant-0.3.0-py3-none-any.whl"
    assert updates.read_release({"tag_name": "v0.3.0"}).wheel is False  # 附件里没有 wheel
    assert updates.read_release({"tag_name": "v0.3.0", "published_at": "<script>"}).published == ""


@pytest.mark.parametrize("data", [{"tag_name": "v0.3.0", "prerelease": True}, {"tag_name": "v0.3.0", "draft": True}, {"tag_name": "v0.4.0-beta"}])
def test_drafts_and_pre_releases_are_not_releases(data):
    assert updates.read_release(data) is None


@pytest.mark.parametrize("data", [None, [], "v0.3.0", {"name": "v0.3.0"}, {"tag_name": 3}])
def test_a_reply_that_is_not_a_release(data):
    with pytest.raises(updates.BadReply):
        updates.read_release(data)


# ---- 缓存 ------------------------------------------------------------------------


def test_the_cache_round_trips_and_is_private(tmp_path):
    path = tmp_path / "data" / "updates.json"
    cache = Cache(1_795_000_000.5, Release("0.3.0", "v0.3.0", "2026-12-01T09:00:00Z", False), "network", "timed out")
    updates.write_cache(path, cache)
    assert updates.read_cache(path) == cache
    assert has_mode(path, 0o600)
    assert json.loads(path.read_text(encoding="utf-8"))["format"] == updates.CACHE_FORMAT


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"format": 99, "checked_at": 1}', '{"format": 1, "checked_at": "yesterday", "latest": {"tag": "evil"}, "problem": "nope"}'])
def test_a_broken_cache_counts_as_none(tmp_path, content):
    path = tmp_path / "updates.json"
    path.write_text(content, encoding="utf-8")
    assert updates.read_cache(path) == Cache()


def test_when_the_cache_is_fresh():
    now = 1_795_000_000.0
    assert updates.due(Cache(), now)
    assert not updates.due(Cache(now - DAY + 60), now)
    assert updates.due(Cache(now - DAY), now)
    # 没问成的隔得短一些就再试，不用等满一天
    assert not updates.due(Cache(now - UPDATE_RETRY_HOURS * 3600 + 60, problem="network"), now)
    assert updates.due(Cache(now - UPDATE_RETRY_HOURS * 3600, problem="network"), now)
    # 钟往回调过（上一次问的时刻在"现在"之后）：再问一次，不要等到钟走回那个时刻
    assert updates.due(Cache(now + 3600), now)


# ---- 关着：一个请求都不发 ---------------------------------------------------------


def test_off_by_default_and_off_sends_nothing(make_config, github):
    fake = github()
    s = make(make_config, fake, enabled=False)
    assert s.settings.update_check is False
    assert not s.updater.due()
    assert s.updater.check() is False and s.updater.check(force=True) is False
    s.updater.check_soon()
    s.updater.start()  # 后台线程也一样：只是等着
    s.clock.tick(10 * DAY)
    time.sleep(0.3)
    s.updater.stop()
    assert fake.count == 0
    assert not s.config.updates_file.exists()
    assert "update_check" not in (json.loads(s.config.settings_file.read_text(encoding="utf-8")) if s.config.settings_file.exists() else {})
    assert s.updater.status().newer is None


def test_turned_off_hides_what_the_cache_knew(make_config, github):
    fake = github()
    s = make(make_config, fake)
    assert s.updater.check() is True
    assert s.updater.status().newer.version == "0.3.0"
    s.updater.set_enabled(False)
    assert s.updater.status().newer is None  # 关掉了就不再说"有新版本"
    assert json.loads(s.config.settings_file.read_text(encoding="utf-8")).get("update_check") is None


# ---- 打开着：一天问一次 ------------------------------------------------------------


def test_on_asks_once_per_interval(make_config, github):
    fake = github()
    s = make(make_config, fake)
    assert s.updater.check() is True
    assert s.updater.check() is False  # 刚问过
    s.clock.tick(DAY - 60)
    assert s.updater.check() is False
    assert fake.count == 1
    s.clock.tick(60)
    assert s.updater.check() is True
    assert fake.count == 2
    status = s.updater.status()
    assert status.newer == Release("0.3.0", "v0.3.0", "2026-12-01T09:00:00Z", True)
    assert status.checked_at == s.clock.now and status.problem is None
    # 存在缓存里：下次启动（新的 Updater）不用再问
    again = Updater(s.config, s.settings, clock=s.clock, current="0.2.0")
    assert again.status().newer is not None and not again.due()


def test_the_request_carries_nothing_personal(make_config, github):
    fake = github()
    s = make(make_config, fake)
    s.updater.check()
    sent = fake.requests[0]
    assert sent["path"] == "/repos/Akxan/zhifox-agent-harness/releases/latest"
    headers = sent["headers"]
    assert headers["user-agent"] == "ZhiFox"  # 不写版本号、不写系统
    assert not {"cookie", "authorization", "x-harness-token"} & set(headers)
    assert "0.2.0" not in json.dumps(headers)


def test_the_background_thread_asks_when_due_and_then_waits(make_config, github):
    fake = github()
    s = make(make_config, fake)
    s.updater.start()
    try:
        until(lambda: fake.count == 1 and not s.updater.status().checking, what="后台线程问了一次")
        time.sleep(0.3)
        assert fake.count == 1  # 问过了就等到明天
        s.updater.check_soon()  # 点了"现在检查"：叫醒它，马上再问
        until(lambda: fake.count == 2, what="叫醒以后马上再问")
    finally:
        s.updater.stop()
    assert s.updater._thread is not None and not s.updater._thread.is_alive()


def test_turning_on_checks_right_away(make_config, github):
    fake = github()
    s = make(make_config, fake, enabled=False)
    s.updater.set_enabled(True)  # 没开后台线程（终端、测试）：另开一个线程问完就走
    until(lambda: s.updater.status().newer is not None, what="打开以后马上问了一次")
    assert fake.count == 1
    assert json.loads(s.config.settings_file.read_text(encoding="utf-8"))["update_check"] is True


def test_news_is_told_once(make_config, github):
    fake = github()
    s = make(make_config, fake)
    s.updater.check()
    assert s.updater.take_news().version == "0.3.0"
    assert s.updater.take_news() is None
    s.clock.tick(DAY)
    s.updater.check()
    assert s.updater.take_news() is None  # 还是同一版：不再说
    fake.answer("0.4.0")
    s.clock.tick(DAY)
    s.updater.check()
    assert s.updater.take_news().version == "0.4.0"


def test_already_up_to_date(make_config, github):
    fake = github("0.2.0")
    s = make(make_config, fake)
    s.updater.check()
    status = s.updater.status()
    assert status.latest.version == "0.2.0" and status.newer is None and status.problem is None


def test_no_release_yet(make_config, github):
    fake = github(None)
    s = make(make_config, fake)
    s.updater.check()
    status = s.updater.status()
    assert status.latest is None and status.problem is None and status.checked_at is not None


def test_a_pre_release_is_ignored_and_the_known_release_stays(make_config, github):
    fake = github("0.3.0")
    s = make(make_config, fake)
    s.updater.check()
    fake.answer("0.4.0", prerelease=True)
    s.clock.tick(DAY)
    s.updater.check()
    assert s.updater.status().latest.version == "0.3.0"
    fake.answer("0.5.0", draft=True)
    s.clock.tick(DAY)
    s.updater.check()
    assert s.updater.status().latest.version == "0.3.0"


# ---- 没问成：安静地记下 ------------------------------------------------------------


@pytest.mark.parametrize(("answer", "problem"), [
    ({"raw": b"<html>not json</html>"}, "reply"),
    ({"raw": b'{"message": "hi"}'}, "reply"),
    ({"status": 403, "headers": {"X-RateLimit-Remaining": "0"}}, "limited"),
    ({"status": 429}, "limited"),
    ({"status": 403}, "http"),
    ({"status": 502}, "http"),
])
def test_failures_are_quiet(make_config, github, answer, problem, caplog):
    fake = github("0.3.0")
    s = make(make_config, fake)
    s.updater.check()  # 先问成一次：没问成的那几次，知道的那一版留着
    fake.answer("0.3.0", **answer)
    s.clock.tick(DAY)
    with caplog.at_level("INFO", logger="harness.updates"):
        assert s.updater.check() is True
    status = s.updater.status()
    assert updates.read_cache(s.config.updates_file).problem == problem
    assert status.problem is not None and status.latest.version == "0.3.0"
    assert any("没能检查新版本" in record.getMessage() for record in caplog.records)
    # 没问成的隔几个小时再试，不用等满一天
    s.clock.tick(UPDATE_RETRY_HOURS * 3600)
    assert s.updater.due()


def test_a_timeout_is_quiet(make_config, github, monkeypatch):
    fake = github("0.3.0", delay=1.0)
    monkeypatch.setattr(updates, "UPDATE_TIMEOUT", 0.2)
    s = make(make_config, fake)
    started = time.monotonic()
    assert s.updater.check() is True
    assert time.monotonic() - started < 5
    cache = updates.read_cache(s.config.updates_file)
    assert cache.problem == "network" and cache.detail
    assert str(s.updater.status().problem).startswith("连不上 GitHub")


def test_cannot_reach_it(make_config, github):
    fake = github()
    fake.stop()  # 没有人在听：连不上
    s = make(make_config, fake)
    s.updater.check()
    assert updates.read_cache(s.config.updates_file).problem == "network"


def test_a_plain_http_address_elsewhere_is_never_asked(make_config):
    s = make(make_config, None, update_api="http://updates.example.com")
    s.updater.check()
    assert updates.read_cache(s.config.updates_file).problem == "address"  # 没有发请求（发了的话 no_real_github 会让测试失败）


def test_problem_text_follows_the_language(make_config, github, monkeypatch):
    fake = github(status=502)
    s = make(make_config, fake)
    s.updater.check()
    monkeypatch.setattr(i18n, "current", "en")
    assert str(s.updater.status().problem) == "GitHub answered HTTP 502"


def test_cache_that_cannot_be_written_does_not_break_anything(make_config, github, caplog):
    fake = github()
    s = make(make_config, fake)
    s.config.updates_file.mkdir(parents=True)  # 写不了：那里是一个文件夹
    with caplog.at_level("WARNING", logger="harness.updates"):
        assert s.updater.check() is True
    assert s.updater.status().newer is not None
    assert not s.updater.due()  # 内存里照样记着，不会一直问


def test_only_one_check_at_a_time(make_config, github):
    fake = github("0.3.0", delay=0.5)
    s = make(make_config, fake)
    results: list[bool] = []
    threads = [threading.Thread(target=lambda: results.append(s.updater.check(force=True))) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, False, False, True] and fake.count == 1


# ---- 怎么升级 --------------------------------------------------------------------

RELEASE = Release("0.3.0", "v0.3.0", "2026-12-01T09:00:00Z", True)


def test_upgrading_a_clone(tmp_path):
    plan = updates.upgrade(RELEASE, updates.TERMINAL, source=True, git=True, root=tmp_path, command="uv run harness", system=machine.MAC)
    assert plan.commands == ["git pull", "uv sync"] and plan.in_project and not plan.manual
    assert str(tmp_path) in plan.lead
    assert plan.before == [] and "退出现在开着的智狐" in plan.notes[-1]


def test_upgrading_a_downloaded_folder(tmp_path):
    plan = updates.upgrade(RELEASE, updates.UNKNOWN, source=True, git=False, root=tmp_path, system=machine.LINUX)
    assert plan.commands == ["uv sync"] and plan.manual
    assert "没有 .git" in plan.lead and "0.3.0" in plan.lead


@pytest.mark.parametrize(("origin", "command"), [
    (install.GIT, "uv tool upgrade harness-assistant"),
    (install.GIT_PINNED, "uv tool install --force git+https://github.com/Akxan/zhifox-agent-harness@v0.3.0"),
    (install.WHEEL, f"uv tool install --force {RELEASE.wheel_url}"),
    (install.ARCHIVE, f"uv tool install --force {RELEASE.wheel_url}"),
    (install.UNKNOWN, f"uv tool install --force {RELEASE.wheel_url}"),
])
def test_upgrading_an_installed_tool(origin, command):
    plan = updates.upgrade(RELEASE, updates.TERMINAL, source=False, origin=origin, command="harness", system=machine.MAC)
    assert plan.commands == [command] and not plan.in_project
    assert plan.lead == "在终端里运行："
    told_both = any("uv tool upgrade harness-assistant" in note for note in plan.notes)
    assert told_both is (origin == install.UNKNOWN)  # 看不出是怎么装的：明说，并且给出另一种办法


def test_without_a_wheel_the_tag_zip_needs_no_git():
    plan = updates.upgrade(Release("0.3.0", "v0.3.0", "", False), source=False, origin=install.WHEEL, system=machine.LINUX)
    assert plan.commands == ["uv tool install --force https://github.com/Akxan/zhifox-agent-harness/archive/refs/tags/v0.3.0.zip"]


def test_a_background_service_is_restarted_after():
    plan = updates.upgrade(RELEASE, updates.SERVICE, source=False, origin=install.GIT, command="harness", system=machine.MAC)
    assert plan.before == [] and "harness service restart" in plan.notes[-1]
    plan = updates.upgrade(RELEASE, updates.UNKNOWN, source=False, origin=install.GIT, command="harness", system=machine.LINUX)
    assert "harness service restart" in plan.notes[-1] and "终端" in plan.notes[-1]  # 不知道是怎么开着的：两种都说


def test_on_windows_stop_first_because_running_files_cannot_be_replaced():
    plan = updates.upgrade(RELEASE, updates.SERVICE, source=False, origin=install.GIT, command="harness", system=machine.WINDOWS)
    assert plan.lead == "在 PowerShell 里运行："
    assert "harness service stop" in plan.before[0] and "harness service start" in plan.notes[-1]
    assert not any("restart" in note for note in plan.notes)
    plan = updates.upgrade(RELEASE, updates.TERMINAL, source=True, git=True, system=machine.WINDOWS)
    assert "先退出开着的智狐" in plan.before[0]
    # 一条命令一行，不用 && 连（Windows PowerShell 5 不认它）
    assert all("&&" not in line for line in plan.commands)


def test_upgrade_text_in_english(monkeypatch):
    monkeypatch.setattr(i18n, "current", "en")
    plan = updates.upgrade(RELEASE, updates.SERVICE, source=False, origin=install.UNKNOWN, command="harness", system=machine.WINDOWS)
    assert plan.lead == "Run in PowerShell:"
    assert all(not re.search(r"[㐀-鿿]", str(text).replace("智狐", "")) for text in (*plan.before, *plan.notes))


# ---- 是怎么装的（install.origin）---------------------------------------------------


@pytest.mark.parametrize(("content", "origin"), [
    ('{"url": "https://github.com/Akxan/zhifox-agent-harness", "vcs_info": {"vcs": "git", "commit_id": "abc"}}', install.GIT),
    ('{"url": "https://github.com/Akxan/zhifox-agent-harness", "vcs_info": {"vcs": "git", "requested_revision": "v0.2.0", "commit_id": "abc"}}', install.GIT_PINNED),
    ('{"url": "https://github.com/x/releases/download/v0.2.0/harness_assistant-0.2.0-py3-none-any.whl", "archive_info": {}}', install.WHEEL),
    ('{"url": "https://github.com/Akxan/zhifox-agent-harness/archive/refs/heads/main.zip", "archive_info": {"hash": "sha256=x"}}', install.ARCHIVE),
    ('{"url": "file:///home/me/zhifox", "dir_info": {"editable": true}}', install.UNKNOWN),
    ("not json", install.UNKNOWN),
    (None, install.UNKNOWN),
    ('{"vcs_info": {"vcs": "git"}}', install.UNKNOWN),
])
def test_how_it_was_installed(content, origin):
    assert install.origin(lambda: content) == origin


# ---- 终端和命令行 ----------------------------------------------------------------


def test_the_terminal_line():
    line = updates.news_line(RELEASE, "0.2.0")
    assert "0.3.0" in line and "0.2.0" in line and RELEASE.page in line and "\n" not in line


def run_cli(args: list[str], monkeypatch, fake: FakeGitHub | None = None) -> tuple[int, str, str]:
    if fake is not None:
        monkeypatch.setenv("HARNESS_UPDATE_API", fake.base_url)
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", err)
    code = cli.main(args)
    return code, out.getvalue(), err.getvalue()


def test_the_command_line(monkeypatch, github, tmp_path):
    fake = github("99.0.0")
    code, out, _ = run_cli(["updates"], monkeypatch, fake)
    assert code == 0 and "关着" in out and fake.count == 0  # 看一眼：不联网
    code, _, err = run_cli(["updates", "check"], monkeypatch, fake)
    assert code == 1 and "updates on" in err and fake.count == 0
    code, out, _ = run_cli(["updates", "on"], monkeypatch, fake)
    assert code == 0 and fake.count == 1 and "99.0.0" in out and "uv sync" in out
    code, out, _ = run_cli(["updates"], monkeypatch, fake)
    assert code == 0 and "99.0.0" in out and fake.count == 1  # 只读缓存
    code, out, _ = run_cli(["updates", "check"], monkeypatch, fake)
    assert code == 0 and fake.count == 2  # 马上再问一次，不管缓存新不新
    code, out, _ = run_cli(["updates", "off"], monkeypatch, fake)
    assert code == 0 and "已关掉" in out
    settings = json.loads((tmp_path / "harness-data" / "settings.json").read_text(encoding="utf-8"))
    assert "update_check" not in settings


def test_the_command_line_when_it_could_not_check(monkeypatch, github):
    fake = github(status=500)
    code, out, _ = run_cli(["updates", "on"], monkeypatch, fake)
    assert code == 0 and "没能检查" in out and "HTTP 500" in out


def test_updates_cli_status_lines(make_config, github):
    fake = github("0.2.0")
    s = make(make_config, fake)
    s.updater.check()
    out = io.StringIO()
    assert updates_cli.run(SimpleNamespace(updates_action=None), s.config, out=out, updater=s.updater) == 0
    assert "已经是最新的版本" in out.getvalue() and "上一次检查" in out.getvalue()


def test_nothing_new_is_said_on_a_terminal_without_the_setting(make_config):
    """终端启动时只看缓存：关着就什么都不说（也不问）。"""
    s = make(make_config, None, enabled=False)
    updates.write_cache(s.config.updates_file, Cache(s.clock.now, RELEASE))
    assert Updater(s.config, s.settings, current="0.2.0").status().newer is None


def test_the_cache_file_is_named_in_config(make_config):
    config = make_config()
    assert config.updates_file == Path(config.data_dir) / "updates.json"


# ---- 终端界面 --------------------------------------------------------------------


def _terminal(make_config, monkeypatch, fake: FakeGitHub, inputs) -> SimpleNamespace:
    """跑一遍终端界面：inputs 是一个个"用户输入"（函数：轮到它时才决定输入什么，None 表示按了 Ctrl-D）。"""
    from harness.ui import TerminalUI

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    config = make_config(update_api=fake.base_url)
    typed = iter(inputs)
    monkeypatch.setattr(TerminalUI, "read_input", lambda self: next(typed)())
    return SimpleNamespace(config=config, run=lambda: cli.run_terminal(config))


def test_the_terminal_says_what_the_cache_knows_without_asking(make_config, monkeypatch, github, capsys):
    fake = github("99.0.0")
    term = _terminal(make_config, monkeypatch, fake, [lambda: None])
    Settings(term.config.settings_file, "anthropic", "x").set_update_check(True)
    updates.write_cache(term.config.updates_file, Cache(time.time(), Release("99.0.0", "v99.0.0", "", True)))
    assert term.run() == 0
    out = capsys.readouterr().out
    assert "有新版本：智狐 99.0.0" in out and "/doctor" in out
    assert fake.count == 0  # 缓存是新的：不问


def test_the_terminal_checks_a_stale_cache_in_the_background(make_config, monkeypatch, github, capsys):
    fake = github("99.0.0")

    def after_the_check():
        until(lambda: fake.count == 1, what="后台问了一次")
        # 等后台那个线程做完（按名字认，见 updates.Updater.check_soon）：它先存缓存、最后才记下"有新版本"。
        # 原来固定等 0.2 秒，GitHub 的 Windows 机器上存缓存慢了一点，下一次输入时还没记下，测试就没看到那一句
        until(lambda: not any(thread.name == "update-check-now" for thread in threading.enumerate()), what="后台的检查做完")
        return ""

    term = _terminal(make_config, monkeypatch, fake, [after_the_check, lambda: None])
    Settings(term.config.settings_file, "anthropic", "x").set_update_check(True)
    assert term.run() == 0
    out = capsys.readouterr().out
    assert out.count("有新版本：智狐 99.0.0") == 1  # 在下一次输入之前说了一次
    assert fake.count == 1


def test_the_terminal_with_the_setting_off_never_asks(make_config, monkeypatch, github, capsys):
    fake = github("99.0.0")

    def wait_a_little():
        time.sleep(0.3)
        return ""

    term = _terminal(make_config, monkeypatch, fake, [wait_a_little, lambda: None])
    updates.write_cache(term.config.updates_file, Cache(time.time() - 30 * DAY, Release("99.0.0", "v99.0.0", "", True)))
    assert term.run() == 0
    assert "有新版本" not in capsys.readouterr().out and fake.count == 0
