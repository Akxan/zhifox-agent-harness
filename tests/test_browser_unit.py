"""浏览器的几块小零件和管理者的测试：按键、网址、截图的存放、BrowserManager，以及 BrowserSession 里最简单的几样。

  - keys、urls、shots 是纯函数和本地文件，直接测。网址那一段特别多测了各种"绕过去"的写法（大小写、前面加空格、
    中间插 Tab……），因为"哪些地址根本不打开"是一条安全规矩，不能被一个大写字母绕开。
  - BrowserManager 和 BrowserSession 对着 tests/fake_chrome.py（经过真正的 chrome.launch() 启动的假 Chrome），
    时间用假的钟（Clock），不真的等十分钟。settings 只需要一个 .browser 字典，用 SimpleNamespace 就够了。
  - 页面里的脚本（page.js）、点击、输入这些要真的 Chrome 才有意义，在别处测。

每个测试结束时关掉它启动过的假 Chrome，不留进程。
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import fake_chrome
import pytest
from modes import has_mode

import harness.browser.manager as manager_module
from harness.browser import chrome, keys, urls
from harness.browser.cdp import CDPClosed
from harness.browser.manager import BrowserManager
from harness.browser.session import Facts, Target
from harness.browser.shots import PRUNE_EVERY, ShotStore
from harness.config import BROWSER_IDLE_MINUTES, BROWSER_SHOT_DAYS, BROWSER_VIEWPORT
from harness.tools import ToolError


def wait_until(check, timeout: float = 5, what: str = "条件") -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, f"等不到{what}"
        time.sleep(0.01)


# ---- 按键 ---------------------------------------------------------------------


def one(text: str) -> list[dict]:
    """一组键要发的全部事件。"""
    chords = keys.parse(text)
    assert len(chords) == 1
    return keys.events(chords[0])


def test_enter_types_a_carriage_return():
    down, up = one("Enter")
    assert down == {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "modifiers": 0, "text": "\r"}
    assert up == {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "modifiers": 0}


def test_ctrl_a_is_select_all_and_types_nothing():
    events = one("ctrl+a")
    assert [(event["type"], event["key"], event["modifiers"]) for event in events] == [
        ("rawKeyDown", "Control", 2),
        ("rawKeyDown", "a", 2),
        ("keyUp", "a", 2),
        ("keyUp", "Control", 0),  # 松开以后不再按着
    ]
    assert events[1]["commands"] == ["selectAll"]  # CDP 的键盘事件不会自己变成编辑命令，要点名
    assert not any("text" in event for event in events)  # 不是在输入框里打一个 a
    assert one("cmd+a")[1]["commands"] == ["selectAll"]
    assert one("Ctrl+V")[1]["commands"] == ["paste"]


def test_modifiers_go_down_in_order_and_come_up_in_reverse():
    events = one("ctrl+shift+t")
    assert [(event["type"], event["key"], event["modifiers"]) for event in events] == [
        ("rawKeyDown", "Control", 2),
        ("rawKeyDown", "Shift", 10),
        ("rawKeyDown", "t", 10),
        ("keyUp", "t", 10),
        ("keyUp", "Shift", 2),
        ("keyUp", "Control", 0),
    ]
    assert "commands" not in events[2]


def test_shift_with_a_letter_types_the_capital():
    down = one("shift+a")[1]
    assert down["type"] == "keyDown" and down["text"] == "A" and down["modifiers"] == 8
    assert one("shift+1")[1]["text"] == "1"  # 不是字母的不变


def test_several_keys_one_after_another():
    chords = keys.parse("Backspace Backspace")
    assert len(chords) == 2
    for chord in chords:
        down = keys.events(chord)[0]
        assert down["key"] == "Backspace" and down["type"] == "rawKeyDown" and "text" not in down
    assert not keys.types_text(chords)


def test_key_names_ignore_case_and_accept_xdotool_names():
    for name, key in [("ENTER", "Enter"), ("Return", "Enter"), ("esc", "Escape"), ("BackSpace", "Backspace"), ("Page_Down", "PageDown"), ("up", "ArrowUp"), ("F5", "F5")]:
        assert one(name)[0]["key"] == key, name
    assert one("F5")[0]["windowsVirtualKeyCode"] == 116
    assert one("space")[0]["text"] == " "
    assert (one("7")[0]["code"], one("-")[0]["code"], one("-")[0]["windowsVirtualKeyCode"]) == ("Digit7", "Minus", 189)
    assert one("中")[0]["text"] == "中"  # 键盘上没有这个键，照样打得出来


@pytest.mark.parametrize("text", ["Foo", "ctrl+Foo", "a+b", "+", "", "   "])
def test_unknown_or_malformed_keys_are_a_tool_error(text):
    with pytest.raises(ToolError):
        keys.parse(text)


def test_only_modifiers_is_allowed():
    chord = keys.parse("ctrl+shift")[0]
    assert chord.key is None and chord.modifiers == 10
    assert [event["type"] for event in keys.events(chord)] == ["rawKeyDown", "rawKeyDown", "keyUp", "keyUp"]


@pytest.mark.parametrize(
    "text, typing, enter",
    [
        ("a", True, False),
        ("shift+a", True, False),
        ("space", True, False),
        ("ctrl+v", True, False),  # 粘贴、剪切也算"打字"
        ("cmd+x", True, False),
        ("Enter", False, True),  # 回车另算
        ("ctrl+Enter", False, True),
        ("Return", False, True),
        ("ctrl+a", False, False),
        ("ctrl+c", False, False),
        ("alt+a", False, False),
        ("Tab", False, False),
        ("shift", False, False),
        ("ArrowDown ArrowDown Enter", False, True),
        ("Backspace x", True, False),
    ],
)
def test_types_text_and_presses_enter(text, typing, enter):
    chords = keys.parse(text)
    assert keys.types_text(chords) is typing
    assert keys.presses_enter(chords) is enter


# ---- 网址 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "given, expected",
    [
        ("back", "back"),
        (" Back ", "back"),
        ("RELOAD", "reload"),
        ("forward", "forward"),
        ("", ""),
        (None, ""),
        ("example.com/docs", "https://example.com/docs"),
        ("localhost:8000", "https://localhost:8000"),  # 会被解析成"协议是 localhost"
        ("example.com:443/x", "https://example.com:443/x"),
        ("127.0.0.1:8080/a", "https://127.0.0.1:8080/a"),
        ("//cdn.example.com/a.js", "https://cdn.example.com/a.js"),
        ("  https://example.com  ", "https://example.com"),
        ("http://example.com", "http://example.com"),
        ("about:blank", "about:blank"),
        ("data:text/html,hi", "data:text/html,hi"),
        ("javascript:alert(1)", "javascript:alert(1)"),  # 不在这里拦（refusal 管）
    ],
)
def test_normalize(given, expected):
    assert urls.normalize(given) == expected


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "FILE:///etc/passwd",
        "fIlE:/etc/passwd",
        "chrome://settings",
        "Chrome://settings/passwords",
        "chrome-extension://abcdef/page.html",
        "chrome-untrusted://terminal/",
        "devtools://devtools/bundled/inspector.html",
        "javascript:alert(1)",
        " JavaScript:alert(1)",
        "\tjavascript:alert(1)",
        "java\tscript:alert(1)",  # 浏览器会把网址里的 Tab、换行去掉
        "java\nscript:alert(1)",
        "\x00javascript:alert(1)",
        "JAVASCRIPT:alert(document.cookie)",
        "view-source:https://example.com",
        "filesystem:https://example.com/temporary/x",
    ],
)
def test_refused_schemes_cannot_be_dressed_up(url):
    assert urls.refusal(url) is not None
    normalized = urls.normalize(url)
    assert normalized.startswith("https://") or urls.refusal(normalized) is not None  # 整理过以后也一样拦得住


@pytest.mark.parametrize("url", ["about:blank", "data:text/html,<p>hi</p>", "https://example.com", "http://localhost:8000/x", "mailto:a@example.com"])
def test_other_addresses_are_not_refused(url):
    assert urls.refusal(url) is None  # 不是"一律不打开"（http 以外的会每次都问，那是 permissions.py 的事）


def test_web_addresses_without_a_host_or_too_long_are_refused():
    assert "网站的名字" in urls.refusal("https://")
    assert "网站的名字" in urls.refusal("http:///path")
    assert "太长" in urls.refusal("https://example.com/" + "x" * urls.MAX_URL_CHARS)


@pytest.mark.parametrize(
    "url, site",
    [
        ("https://www.example.com/a?b=c", "https://example.com"),
        ("https://example.com", "https://example.com"),
        ("https://EXAMPLE.com./x", "https://example.com"),
        ("http://example.com", "http://example.com"),  # http 和 https 不算同一个
        ("https://docs.example.com", "https://docs.example.com"),  # 子域名算不同的网站
        ("https://example.com:8443/x", "https://example.com:8443"),
        ("https://user:pass@example.com/", "https://example.com"),
        ("about:blank", ""),
        ("data:text/html,hi", ""),
        ("https://", ""),
    ],
)
def test_site_of(url, site):
    assert urls.site_of(url) == site


def test_subdomains_and_schemes_are_different_sites():
    assert urls.site_of("https://docs.example.com") != urls.site_of("https://example.com")
    assert urls.site_of("https://www.example.com") == urls.site_of("https://example.com")
    assert urls.site_of("http://example.com") != urls.site_of("https://example.com")


@pytest.mark.parametrize("url", ["https://example.com:99999/", "https://example.com:abc/", urls.normalize("javascript:1+alert(1)")])
def test_site_of_a_bad_port_does_not_raise(url):
    assert isinstance(urls.site_of(url), str)


@pytest.mark.parametrize(
    "url, local",
    [
        ("http://localhost:3000", True),
        ("http://app.localhost/", True),
        ("http://printer.local", True),
        ("http://127.0.0.1:8080", True),
        ("http://10.0.0.5", True),
        ("http://192.168.1.1", True),
        ("http://172.16.0.1", True),
        ("http://169.254.169.254/latest", True),  # 链路本地（云服务器的元数据地址就在这里）
        ("http://[::1]:8000/", True),
        ("https://example.com", False),
        ("https://8.8.8.8", False),
        ("about:blank", False),
    ],
)
def test_is_local(url, local):
    assert urls.is_local(url) is local


def test_shown_strips_control_characters_and_truncates():
    assert urls.shown("https://a.example/\x1b[31mred\n") == "https://a.example/[31mred"
    long = urls.shown("https://example.com/" + "x" * 500, limit=50)
    assert len(long) == 50 and long.endswith("…")


# ---- 截图 ---------------------------------------------------------------------


def test_shots_are_named_by_their_content_and_kept_private(tmp_path):
    store = ShotStore(tmp_path / "shots")
    shot = store.save(b"jpeg-bytes")
    assert shot == hashlib.sha256(b"jpeg-bytes").hexdigest()
    assert store.read(shot) == b"jpeg-bytes"
    assert store.save(b"jpeg-bytes") == shot  # 同一张只存一份
    assert [path.name for path in (tmp_path / "shots").iterdir()] == [f"{shot}.jpg"]
    assert has_mode(tmp_path / "shots", 0o700)
    assert has_mode(tmp_path / "shots" / f"{shot}.jpg", 0o600)


@pytest.mark.parametrize("shot_id", ["", "../../etc/passwd", "A" * 64, "g" * 64, "a" * 63, "a" * 65, "a" * 64 + ".jpg", "a" * 64 + "\n"])
def test_reading_a_shot_only_accepts_64_hex_characters(tmp_path, shot_id):
    store = ShotStore(tmp_path / "shots")
    store.save(b"x")
    assert store.read(shot_id) is None


def test_a_missing_shot_or_a_symlink_or_a_folder_reads_as_none(symlinks, tmp_path):
    store = ShotStore(tmp_path / "shots")
    store.save(b"real")
    assert store.read("0" * 64) is None
    secret = tmp_path / "secret.txt"
    secret.write_text("别给页面看", encoding="utf-8")
    linked = "1" * 64
    (tmp_path / "shots" / f"{linked}.jpg").symlink_to(secret)
    assert store.read(linked) is None  # 符号链接不跟过去
    folder = "2" * 64
    (tmp_path / "shots" / f"{folder}.jpg").mkdir()
    assert store.read(folder) is None


def test_saving_where_it_cannot_write_returns_an_empty_id(tmp_path):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("", encoding="utf-8")
    assert ShotStore(blocker / "shots").save(b"x") == ""


def test_prune_removes_only_old_shots_and_at_most_once_an_hour(tmp_path):
    now = [time.time()]
    store = ShotStore(tmp_path / "shots", clock=lambda: now[0])
    old, new = store.save(b"old"), store.save(b"new")  # 第一次 save 顺手清过一次（那时什么都没有）
    long_ago = now[0] - (BROWSER_SHOT_DAYS + 1) * 86400
    os.utime(tmp_path / "shots" / f"{old}.jpg", (long_ago, long_ago))
    assert store.prune() == 0  # 一小时之内不再清
    assert store.read(old) == b"old"
    assert store.prune(force=True) == 1
    assert store.read(old) is None and store.read(new) == b"new"
    os.utime(tmp_path / "shots" / f"{new}.jpg", (long_ago, long_ago))
    now[0] += PRUNE_EVERY + 1
    store.save(b"newest")  # 过了一小时：存新的时候顺手清掉旧的
    assert store.read(new) is None and store.read(hashlib.sha256(b"newest").hexdigest()) == b"newest"


def test_prune_without_a_folder_is_fine(tmp_path):
    assert ShotStore(tmp_path / "never-made").prune(force=True) == 0


# ---- BrowserManager（对着假 Chrome） ------------------------------------------------


class Clock:
    """假的钟：测试想让时间过去多久，就过去多久。"""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def browser(tmp_path, monkeypatch):
    """一个用假 Chrome 的 BrowserManager。make(剧本……) 返回一个 rig：manager、启动过的 Chrome、假 Chrome 收到的命令。"""
    monkeypatch.setattr(manager_module, "CALM_SECONDS", 0.05)
    monkeypatch.setattr(manager_module, "IDLE_CHECK_SECONDS", 3600)  # 后台的闲置检查不要在测试中途冒出来
    monkeypatch.setattr(chrome, "needs_headless", lambda: False)  # 和这台电脑有没有屏幕无关
    rigs: list[SimpleNamespace] = []

    def make(clock=None, headless: bool = True, path: str | None = None, **script) -> SimpleNamespace:
        record = tmp_path / f"chrome-{len(rigs)}.jsonl"
        script.setdefault("record", str(record))
        argument = fake_chrome.script_arg(tmp_path, **script)
        launched: list[chrome.Chrome] = []

        def launcher(executable, profile, headless, on_close=None):
            launched.append(chrome.launch(executable, profile, headless, on_close=on_close, extra_args=[argument]))
            return launched[-1]

        executable = str(fake_chrome.executable(tmp_path)) if path is None else path
        settings = SimpleNamespace(browser={"enabled": True, "headless": headless, "path": executable})
        manager = BrowserManager(tmp_path / "browser", settings, launcher=launcher, clock=clock or Clock())
        changes: list[int] = []
        manager.on_change = lambda: changes.append(1)
        rig = SimpleNamespace(
            manager=manager,
            launched=launched,
            settings=settings,
            changes=changes,
            executable=executable,
            sent=lambda: fake_chrome.records(record),
        )
        rigs.append(rig)
        return rig

    yield make
    for rig in rigs:
        rig.manager.close()
        for started in rig.launched:
            started.kill()  # 对整个进程组
            wait_until(lambda running=started: not running.alive(), what="假 Chrome 退出")  # 顺便收掉，不留僵尸
            started.connection.close()


def emit(rig, *events: dict) -> None:
    """让假 Chrome 发出这些事件。调用返回时，它们已经被读线程处理过了（见 fake_chrome 的 Fake.emit）。"""
    rig.manager.connection_if_running().call("Fake.emit", {"events": list(events)}, timeout=5)


def test_chrome_starts_only_when_first_needed(browser):
    rig = browser()
    session = rig.manager.session("对话")
    assert rig.manager.status()["running"] is False
    # 动手之前看一眼（permissions.py 要用）不会为此去打开浏览器
    assert session.facts("click", target=Target(ref="ref_1")) == Facts("click", reachable=False, idle=True)
    assert rig.manager.connection_if_running() is None
    assert rig.launched == []
    connection, generation = rig.manager.connect()
    assert generation == 1 and len(rig.launched) == 1
    again, generation = rig.manager.connect()
    assert again is connection and generation == 1 and len(rig.launched) == 1  # 开着就用开着的
    assert rig.manager.status()["running"] is True
    assert rig.launched[0].headless is True


def test_launch_denies_downloads_and_listens_for_new_tabs(browser):
    rig = browser()
    rig.manager.connect()
    sent = rig.sent()
    assert [message["method"] for message in sent[:4]] == ["Browser.getVersion", "Browser.setDownloadBehavior", "Target.setDiscoverTargets", "Target.getTargets"]
    assert sent[1]["params"] == {"behavior": "deny", "eventsEnabled": True}
    assert sent[2]["params"] == {"discover": True}
    # Chrome 自己开的那个空白页留给第一个要标签页的 session，只能拿一次
    assert rig.manager.claim_blank() == "BLANK"
    assert rig.manager.claim_blank() == ""


def test_an_idle_browser_closes_after_the_configured_minutes(browser):
    clock = Clock()
    rig = browser(clock=clock)
    rig.manager.connect()
    clock.advance(BROWSER_IDLE_MINUTES * 60)
    assert rig.manager.check_idle() is False  # 刚好到点还不算
    clock.advance(1)
    assert rig.manager.check_idle() is True
    assert rig.manager.status()["running"] is False
    assert not rig.launched[0].alive()
    assert rig.manager.check_idle() is False  # 已经关了：不再关一次


def test_a_busy_browser_is_never_idle(browser):
    clock = Clock()
    rig = browser(clock=clock)
    rig.manager.connect()
    rig.manager.began()
    clock.advance(BROWSER_IDLE_MINUTES * 60 * 3)
    assert rig.manager.check_idle() is False
    rig.manager.ended()  # 从做完的那一刻重新算
    clock.advance(BROWSER_IDLE_MINUTES * 60)
    assert rig.manager.check_idle() is False
    clock.advance(1)
    assert rig.manager.check_idle() is True


def test_close_quits_chrome_and_tells_the_page_once(browser):
    rig = browser()
    rig.manager.connect()
    before = len(rig.changes)
    rig.manager.close()
    assert not rig.launched[0].alive()
    assert rig.sent()[-1]["method"] == "Browser.close"
    assert len(rig.changes) == before + 1
    rig.manager.close()  # 已经关了：什么都不做，也不再通知
    assert len(rig.changes) == before + 1
    _, generation = rig.manager.connect()  # 再用时重新打开，算第二次启动
    assert generation == 2 and len(rig.launched) == 2


def test_a_crashed_chrome_is_noticed_and_reopened_next_time(browser):
    rig = browser()
    connection, _ = rig.manager.connect()
    before = len(rig.changes)
    with pytest.raises(CDPClosed):
        connection.call("Fake.exit", timeout=5)
    assert rig.manager.status()["running"] is False
    wait_until(lambda: len(rig.changes) > before, what="页面收到通知")  # 读线程发现断了，另起一个线程收拾
    assert rig.manager.connection_if_running() is None
    _, generation = rig.manager.connect()
    assert generation == 2


def test_a_missing_chrome_is_explained(browser, tmp_path):
    missing = str(tmp_path / "no-such-chrome")
    rig = browser(path=missing)
    with pytest.raises(ToolError, match="找不到 Chrome"):
        rig.manager.connect()
    status = rig.manager.status()
    assert status["error"] == f"设置里填的 Chrome 路径不对：{missing}"
    assert status["chrome"] == "" and status["path"] == missing and status["running"] is False
    assert rig.launched == []


def test_a_chrome_that_does_not_answer_properly_is_killed(browser):
    rig = browser(errors={"Browser.getVersion": {"code": -32000, "message": "坏掉了"}})
    with pytest.raises(ToolError, match="浏览器启动失败"):
        rig.manager.connect()
    assert "坏掉了" in rig.manager.status()["error"]
    wait_until(lambda: not rig.launched[0].alive(), what="坏掉的 Chrome 被结束")
    assert rig.manager.connection_if_running() is None


def test_a_crashed_chrome_leaves_no_pipes_open(browser):
    rig = browser()
    before = fake_chrome.open_pipes()
    connection, _ = rig.manager.connect()
    with pytest.raises(CDPClosed):
        connection.call("Fake.exit", timeout=5)
    wait_until(lambda: rig.manager._chrome is None, what="收拾完")
    time.sleep(0.05)
    assert fake_chrome.open_pipes() == before


def test_a_chrome_that_fails_to_start_is_reaped_and_its_pipes_closed(browser):
    rig = browser(errors={"Browser.getVersion": {"code": -32000, "message": "坏掉了"}})
    before = fake_chrome.open_pipes()
    with pytest.raises(ToolError):
        rig.manager.connect()
    pid = rig.launched[0].pid
    wait_until(lambda: rig.launched[0].connection.closed, what="连接断开")  # 进程已经死了
    time.sleep(0.05)
    leaked = fake_chrome.open_pipes() - before
    try:
        os.waitpid(pid, os.WNOHANG)  # 能收到的话，说明智狐自己没有收它（是个僵尸）
        reaped = False
    except ChildProcessError:
        reaped = True
    assert reaped and not leaked


def test_a_launcher_that_cannot_start_chrome_becomes_a_tool_error(tmp_path):
    def broken(*args, **kwargs):
        raise OSError("权限不够")

    settings = SimpleNamespace(browser={"enabled": True, "headless": True, "path": str(fake_chrome.executable(tmp_path))})
    manager = BrowserManager(tmp_path / "browser", settings, launcher=broken)
    with pytest.raises(ToolError, match="浏览器启动失败"):
        manager.connect()
    assert manager.status()["error"] == "Chrome 启动失败：权限不够"


def test_status_shows_what_the_page_needs(browser, tmp_path):
    rig = browser(headless=False)
    assert rig.manager.status() == {
        "enabled": True,
        "running": False,
        "busy": False,
        "headless": False,
        "headless_setting": False,
        "headless_forced": False,
        "chrome": rig.executable,
        "path": rig.executable,
        "profile": str(tmp_path / "browser" / "profile"),
        "profile_exists": False,
        "error": "",
        # 页面上的说明用的几个数，都从 config.py 来（页面不另记一份）
        "shot_days": BROWSER_SHOT_DAYS,
        "idle_minutes": BROWSER_IDLE_MINUTES,
        "shot_tokens": BROWSER_VIEWPORT[0] * BROWSER_VIEWPORT[1] // 750,
    }
    rig.manager.connect()
    status = rig.manager.status()
    assert status["running"] is True and status["profile_exists"] is True
    rig.manager.headless_only = True  # 在服务器上（harness serve）：只能无界面
    status = rig.manager.status()
    assert (status["headless"], status["headless_setting"], status["headless_forced"]) == (True, False, True)
    rig.settings.browser["enabled"] = False
    assert rig.manager.status()["enabled"] is False


def test_the_busy_indicator_does_not_flicker_between_quick_actions(browser, monkeypatch):
    rig = browser()
    monkeypatch.setattr(manager_module, "CALM_SECONDS", 0.3)
    rig.manager.began()
    assert len(rig.changes) == 1 and rig.manager.status()["busy"] is True
    for _ in range(3):  # 一串连着的动作（点、输入、回车……）
        rig.manager.ended()
        rig.manager.began()
    assert len(rig.changes) == 1
    rig.manager.ended()
    assert rig.manager.status()["busy"] is True  # 刚做完：还显示"正在操作"
    wait_until(lambda: len(rig.changes) == 2, what="安静下来")
    assert rig.manager.status()["busy"] is False
    time.sleep(0.4)
    assert len(rig.changes) == 2


def test_clear_profile_waits_until_the_browser_is_not_busy(browser):
    rig = browser()
    rig.manager.connect()
    rig.manager.began()
    with pytest.raises(OSError, match="正在使用中"):
        rig.manager.clear_profile()
    assert rig.manager.profile.exists() and rig.manager.status()["running"] is True
    rig.manager.ended()
    (rig.manager.profile / "Cookies").write_text("登录状态", encoding="utf-8")
    rig.manager.clear_profile()
    assert not rig.manager.profile.exists()
    assert not rig.launched[0].alive()  # 先关掉浏览器，再删
    rig.manager.clear_profile()  # 没有可删的也没关系


def test_clear_profile_removes_a_symlink_but_not_what_it_points_to(symlinks, browser, tmp_path):
    rig = browser()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("别删我", encoding="utf-8")
    rig.manager.profile.parent.mkdir(parents=True)
    rig.manager.profile.symlink_to(elsewhere, target_is_directory=True)
    rig.manager.clear_profile()
    assert not rig.manager.profile.is_symlink() and not rig.manager.profile.exists()
    assert (elsewhere / "keep.txt").read_text(encoding="utf-8") == "别删我"


# ---- BrowserSession 里简单的几样（对着假 Chrome） --------------------------------------


def test_two_sessions_get_their_own_tabs_and_numbering(browser):
    rig = browser()
    mine, theirs = rig.manager.session("对话"), rig.manager.session("任务")
    first = mine.run("new_tab")
    assert first.tabs_only and first.state == {
        "tabs": [{"tab_id": "tab-1", "title": "", "url": "about:blank", "active": True}],
        "state_changes": [{"type": "tab_opened", "tab_id": "tab-1"}],
    }
    other = theirs.run("new_tab")
    assert [tab["tab_id"] for tab in other.state["tabs"]] == ["tab-1"]  # 各数各的
    assert mine.tabs["tab-1"].target == "BLANK"  # 第一个用浏览器的拿走 Chrome 自己开的空白页
    assert theirs.tabs["tab-1"].target == "T1"
    assert not mine.owns("T1") and not theirs.owns("BLANK")
    mine.run("new_tab")
    assert [tab["tab_id"] for tab in theirs.run("list_tabs").state["tabs"]] == ["tab-1"]  # 看不到别人的
    with pytest.raises(ToolError, match="没有 tab-2"):
        theirs.run("switch_tab", tab_id="tab-2")


def test_stop_wakes_a_waiting_action_at_once_and_the_next_action_works(browser):
    rig = browser()
    session = rig.manager.session("对话")
    with ThreadPoolExecutor(1) as pool:
        started = time.monotonic()
        waiting = pool.submit(session.run, "wait", duration=20)
        wait_until(lambda: session.busy, what="开始等")
        session.stop()
        with pytest.raises(ToolError, match="用户停下了"):
            waiting.result(3)
        assert time.monotonic() - started < 3
    assert session.run("wait", duration=0).text == "等了 0 秒。"


def test_stop_cancels_a_chrome_command_of_this_session_only(browser):
    rig = browser(silent=["Page.enable"])  # 连上标签页之后的第一个命令 Chrome 一直不回答
    stuck, other = rig.manager.session("对话"), rig.manager.session("任务")
    rig.manager.connect()
    with ThreadPoolExecutor(2) as pool:
        hanging = pool.submit(stuck.run, "new_tab")
        wait_until(lambda: any(message["method"] == "Page.enable" for message in rig.sent()), what="卡在 Page.enable")
        waiting = pool.submit(other.run, "wait", duration=0.5)
        wait_until(lambda: other.busy, what="另一个开始等")
        started = time.monotonic()
        stuck.stop()
        with pytest.raises(ToolError, match="用户停下了"):
            hanging.result(3)
        assert time.monotonic() - started < 1  # 不用等到 BROWSER_CALL_TIMEOUT
        assert waiting.result(3).text == "等了 0.5 秒。"  # 别的运行照常


def test_close_closes_only_this_sessions_tabs(browser):
    rig = browser()
    mine, theirs = rig.manager.session("对话"), rig.manager.session("任务")
    mine.run("new_tab")
    mine.run("new_tab")
    theirs.run("new_tab")
    mine.close()
    closed = [message["params"]["targetId"] for message in rig.sent() if message["method"] == "Target.closeTarget"]
    assert closed == ["BLANK", "T1"]
    remaining = rig.manager.connection_if_running().call("Target.getTargets", timeout=5)["targetInfos"]
    assert [info["targetId"] for info in remaining] == ["T2"]
    assert mine.tabs == {} and mine not in rig.manager._sessions
    assert [tab["tab_id"] for tab in theirs.run("list_tabs").state["tabs"]] == ["tab-1"]


def test_after_chrome_restarts_old_tabs_are_gone_and_the_model_is_told(browser):
    rig = browser()
    session = rig.manager.session("对话")
    session.run("new_tab")
    rig.manager.close()  # 比如闲置太久被关掉了
    outcome = session.run("list_tabs")
    assert "关掉又重新打开了" in outcome.text
    assert outcome.state == {"tabs": []}
    assert rig.manager.generation == 2
    assert session.run("new_tab").state["tabs"][0]["tab_id"] == "tab-2"  # 编号不从头数：旧的 tab-1 不会指到新的页面


def test_stop_all_stops_everyone_closes_chrome_and_says_so_once(browser):
    rig = browser()
    mine, theirs = rig.manager.session("对话"), rig.manager.session("任务")
    mine.run("new_tab")
    with ThreadPoolExecutor(1) as pool:
        waiting = pool.submit(theirs.run, "wait", duration=20)
        wait_until(lambda: theirs.busy, what="开始等")
        rig.manager.stop_all()
        with pytest.raises(ToolError, match="用户停下了"):
            waiting.result(3)
    assert mine.closed_by_user and theirs.closed_by_user
    assert rig.manager.status()["running"] is False
    with pytest.raises(ToolError, match="停下并关掉了浏览器"):
        mine.run("list_tabs")
    outcome = mine.run("list_tabs")  # 说过一次就够了：之后照常（浏览器重新打开）
    assert "关掉又重新打开了" in outcome.text
    with pytest.raises(ToolError, match="停下并关掉了浏览器"):
        theirs.run("wait", duration=0)


def test_navigate_opens_the_page_remembers_the_site_and_keeps_a_screenshot(browser):
    rig = browser()
    session = rig.manager.session("对话")
    before = session.facts("navigate", url="example.com/docs")
    assert (before.destination, before.new_site, before.reachable) == ("https://example.com/docs", True, False)
    outcome = session.run("navigate", url="example.com/docs")
    assert outcome.text == "已打开 https://example.com/docs"
    assert outcome.state["tabs"] == [{"tab_id": "tab-1", "title": "", "url": "https://example.com/docs", "active": True}]
    assert session.manager.shots.read(outcome.shot) == fake_chrome.JPEG
    assert outcome.thumb.startswith("data:image/jpeg;base64,")
    assert session.allowed_sites == {"https://example.com"}
    assert session.facts("navigate", url="https://www.example.com/other").new_site is False  # 去过的网站不再问
    assert session.facts("navigate", url="https://docs.example.com").new_site is True


def test_a_refused_address_never_starts_chrome(browser):
    rig = browser()
    session = rig.manager.session("对话")
    for url in ("file:///etc/passwd", " JavaScript:alert(1)", "chrome://settings"):
        with pytest.raises(ToolError, match="不能打开"):
            session.run("navigate", url=url)
    assert rig.launched == []


def test_facts_for_an_address_with_a_bad_port_do_not_raise(browser):
    rig = browser()
    found = rig.manager.session("对话").facts("navigate", url="https://example.com:99999/")
    assert found.action == "navigate"


def test_a_popup_opened_by_my_page_becomes_my_tab(browser):
    rig = browser()
    mine, theirs = rig.manager.session("对话"), rig.manager.session("任务")
    mine.run("new_tab")
    theirs.run("new_tab")
    emit(rig, {"method": "Target.targetCreated", "params": {"targetInfo": {"targetId": "POPUP", "type": "page", "openerId": "BLANK", "url": ""}}})
    assert mine.owns("POPUP") and not theirs.owns("POPUP")
    state = mine.run("list_tabs").state
    assert [tab["tab_id"] for tab in state["tabs"]] == ["tab-1", "tab-2"]
    assert {"type": "tab_opened", "tab_id": "tab-2"} in state["state_changes"]
    # 没有打开它的页面，或者不是标签页的：谁的都不算
    emit(
        rig,
        {"method": "Target.targetCreated", "params": {"targetInfo": {"targetId": "ORPHAN", "type": "page"}}},
        {"method": "Target.targetCreated", "params": {"targetInfo": {"targetId": "WORKER", "type": "service_worker", "openerId": "BLANK"}}},
    )
    assert not mine.owns("ORPHAN") and not mine.owns("WORKER") and not theirs.owns("ORPHAN")


def test_a_tab_closed_by_hand_is_dropped_and_reported(browser):
    rig = browser()
    session = rig.manager.session("对话")
    session.run("new_tab")
    session.run("new_tab")
    emit(rig, {"method": "Target.targetDestroyed", "params": {"targetId": "BLANK"}})
    # 读线程只把这件事放进收件箱（它不能拿 session 的锁），下一个动作时才记进来
    wait_until(lambda: not session.owns("BLANK"), what="标签页不再算这个 session 的")
    outcome = session.run("list_tabs")
    assert list(session.tabs) == ["tab-2"]
    assert "tab-1 被关掉了" in outcome.text
    assert [tab["tab_id"] for tab in outcome.state["tabs"]] == ["tab-2"]


def test_a_refused_download_is_reported_to_the_session_whose_page_asked(browser):
    rig = browser()
    mine, theirs = rig.manager.session("对话"), rig.manager.session("任务")
    mine.run("new_tab")
    theirs.run("new_tab")
    emit(rig, {"method": "Browser.downloadWillBegin", "params": {"frameId": "BLANK", "url": "https://example.com/file.zip", "guid": "g"}})
    changes = mine.run("list_tabs").state["state_changes"]
    assert changes == [{"type": "download_failed", "download_id": "dl-1", "url": "https://example.com/file.zip", "error": "智狐的浏览器不下载文件"}]
    assert "state_changes" not in theirs.run("list_tabs").state


@pytest.mark.parametrize("kind, accept", [("alert", True), ("beforeunload", True), ("confirm", False), ("prompt", False)])
def test_page_dialogs_are_dismissed_and_reported(browser, kind, accept):
    rig = browser()
    session = rig.manager.session("对话")
    session.run("new_tab")
    tab = session.tabs["tab-1"]
    emit(rig, {"method": "Page.javascriptDialogOpening", "params": {"type": kind, "message": "真的\n  要离开吗？"}, "sessionId": tab.session})

    def answered():
        return [message for message in rig.sent() if message["method"] == "Page.handleJavaScriptDialog"]

    wait_until(answered, what="对话框被处理")  # 在另一个线程里回答（读线程里不能等 Chrome）
    assert answered()[0]["params"] == {"accept": accept}
    assert answered()[0]["sessionId"] == tab.session
    assert f"页面弹出了一个对话框（{kind}）：“真的 要离开吗？”" in session.run("list_tabs").text


def test_closing_my_own_tab_works_even_when_chrome_reports_it_right_away(browser):
    rig = browser()
    session = rig.manager.session("对话")
    session.run("new_tab")
    session.run("new_tab")
    outcome = session.run("close_tab", tab_id="tab-2")
    assert [tab["tab_id"] for tab in outcome.state["tabs"]] == ["tab-1"]
    assert "被关掉了" not in outcome.text  # 是它自己关的，不该说成"可能是用户手动关的"


def test_dialog_from_someone_elses_tab_does_not_reach_me(browser):
    rig = browser()
    mine, theirs = rig.manager.session("对话"), rig.manager.session("任务")
    mine.run("new_tab")
    theirs.run("new_tab")
    emit(rig, {"method": "Page.javascriptDialogOpening", "params": {"type": "alert", "message": "hi"}, "sessionId": theirs.tabs["tab-1"].session})
    assert "对话框" not in mine.run("list_tabs").text
    assert "对话框" in theirs.run("list_tabs").text


def test_actions_in_one_session_run_one_at_a_time(browser):
    rig = browser()
    session = rig.manager.session("对话")
    order: list[str] = []
    gate = threading.Event()
    original = session._do_wait

    def slow_wait(duration, tab_id=None):
        order.append("start")
        gate.wait(2)
        order.append("end")
        return original(0)

    session._do_wait = slow_wait
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(session.run, "wait", duration=0)
        wait_until(lambda: order == ["start"], what="第一个开始")
        second = pool.submit(session.run, "wait", duration=0)
        time.sleep(0.1)
        assert order == ["start"]  # 第二个在等第一个做完
        gate.set()
        first.result(3)
        second.result(3)
    assert order == ["start", "end", "start", "end"]


def test_profile_folder_lives_under_the_given_folder(browser, tmp_path):
    rig = browser()
    rig.manager.connect()
    assert rig.manager.profile == tmp_path / "browser" / "profile"
    assert Path(rig.manager.status()["profile"]).is_dir()
    argv = rig.manager.connection_if_running().call("Fake.argv", timeout=5)["argv"]
    assert f"--user-data-dir={tmp_path / 'browser' / 'profile'}" in argv
    assert "--headless=new" in argv


def test_the_page_is_told_about_changes_without_holding_the_managers_lock(browser):
    """刷新页面要算状态（status 要拿 manager 的锁），而别的线程可能正拿着"发状态"的锁在等它：
    通知页面的时候手里不能拿着这把锁，不然两边一等就谁也动不了了（网页上表现为整个程序卡死）。"""
    rig = browser()
    answered: list[bool] = []

    def from_another_thread() -> None:
        worker = threading.Thread(target=rig.manager.status, daemon=True)
        worker.start()
        worker.join(2)
        answered.append(not worker.is_alive())

    rig.manager.on_change = from_another_thread
    rig.manager.connect()  # 打开：通知一次
    rig.manager.began()  # 忙起来：通知一次
    rig.manager.close()  # 关掉：通知一次
    assert len(answered) >= 3 and all(answered)
