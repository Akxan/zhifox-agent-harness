"""操作整台电脑的里层：坐标换算、按键、设置、几次运行怎么共用一个屏幕、每一个动作落到后端的哪个事件上。

后端换成 FakeDesktop（tests/fake_desktop.py）：它只记下"要发什么事件"，截图交回两张编造的图，绝不碰真的屏幕。
测的是"智狐让后端做了什么"，不是系统本身（真的后端只在虚拟屏幕上测，见 test_desktop_x11.py）。

要守住的几条：
  - 坐标：模型说的是缩小后的截图上的像素，点下去的是屏幕上的点。Retina 屏幕上截图原图又是点的两倍，换算不能错，
    错了每一下都会偏到同一个方向。
  - 停下：正在等的、正在打字的、按着键的，马上停；按下去的键、鼠标键一定松开。
  - 屏幕的大小变了、碰到不许碰的程序、问你时的那个程序不在原处了：这一步不做。
  - 打字之前把问你时最前面的那个程序换回来（你点"允许"时浏览器到了最前面），换不回来就不打。
"""

from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

import pytest
from fake_desktop import FULL, FakeDesktop, install

from harness.browser.shots import ShotStore
from harness.desktop import keys
from harness.desktop import session as session_module
from harness.desktop.backend import Shot
from harness.desktop.frame import Box, Frame, Screen, fit, thumb_size
from harness.desktop.manager import OFF, UNSUPPORTED, DesktopManager
from harness.desktop.session import STOPPED, Facts
from harness.settings import COMPUTER_DEFAULTS, computer_settings
from harness.tools.base import ToolError

RETINA = Screen(1512, 982, 3024, 1964)


@pytest.fixture(autouse=True)
def no_settle(monkeypatch):
    """动作做完后本来要等 0.4 秒再截预览图（让程序画完）。测试里不等。"""
    monkeypatch.setattr(session_module, "DESKTOP_SETTLE_SECONDS", 0)


def make(tmp_path, fake: FakeDesktop | None = None, **computer) -> tuple[DesktopManager, FakeDesktop]:
    settings = SimpleNamespace(computer={**COMPUTER_DEFAULTS, "enabled": True, **computer})
    manager = DesktopManager(tmp_path / "desktop", settings)
    fake = fake or FakeDesktop()
    install(manager, fake)
    return manager, fake


# ---- 坐标 ---------------------------------------------------------------------------


def test_fit_keeps_small_screens_and_shrinks_big_ones():
    assert fit(1280, 800) == (1280, 800)  # 102 万像素，在上限以内：原样交出去，坐标一比一
    assert fit(1512, 982) == (1330, 864)  # 常见的 MacBook：按总像素缩小
    assert fit(1920, 1080) == (1429, 804)
    width, height = fit(5120, 2880)
    assert width <= 1568 and width * height <= 1_150_000
    assert fit(4000, 200) == (1568, 78)  # 很长的屏幕：长边先到上限
    with pytest.raises(ValueError):
        fit(0, 800)


def test_retina_coordinates_map_to_points_both_ways():
    frame = Frame.of(RETINA)
    assert frame.size == (1330, 864)
    assert RETINA.density == 2.0
    assert frame.to_screen(665, 432) == (756, 491)
    assert frame.to_image(756, 491) == (665, 432)
    # 右下角最后一个像素落在屏幕最后一个点上，不会出界
    assert frame.to_screen(1329, 863) == (1511, 981)
    assert frame.to_image(5000, -20) == (1329, 0)  # 屏幕外面的按最近的边算


def test_coordinates_outside_the_screenshot_are_an_error():
    frame = Frame.of(RETINA)
    for point in ((1330, 10), (10, 864), (-1, 5)):
        with pytest.raises(ToolError, match="不在截图里"):
            frame.to_screen(*point)


def test_zoom_region_and_size():
    frame = Frame.of(RETINA)
    box = frame.region([100, 200, 400, 350])
    assert box == Box(113, 227, 342, 171)
    # 原图里那一块是 684×342 像素，放大到正好装进平时截图的大小（1330×864），宽高比不变
    assert frame.zoom_size(box) == (1330, 665)
    assert frame.region([-50, -50, 20, 20]) == Box(0, 0, 23, 23)  # 超出图的部分去掉
    for bad in ([1, 2, 3], [0, 0, 2, 2], [500, 500, 100, 100], "0,0,10,10", [0, 0, True, 10], [0, 0, float("nan"), 10]):
        with pytest.raises(ToolError):
            frame.region(bad)


def test_zoom_never_blows_a_tiny_region_up_more_than_four_times():
    frame = Frame.of(Screen(1280, 800, 1280, 800))
    assert frame.zoom_size(Box(0, 0, 10, 10)) == (40, 40)


def test_thumb_size():
    assert thumb_size(1330, 864, 320) == (320, 208)
    assert thumb_size(200, 100, 320) == (200, 100)


# ---- 按键 ---------------------------------------------------------------------------


def test_keys_are_parsed_into_canonical_chords():
    assert keys.parse("ctrl+s") == [keys.Chord(("ctrl",), "s")]
    assert keys.parse("Return") == [keys.Chord((), "return")]
    assert keys.parse("super+shift+Page_Down") == [keys.Chord(("shift", "cmd"), "pagedown")]
    assert keys.parse("cmd+alt+Escape") == [keys.Chord(("alt", "cmd"), "escape")]
    assert keys.parse("ctrl+a Delete") == [keys.Chord(("ctrl",), "a"), keys.Chord((), "delete")]
    assert keys.parse("A") == [keys.Chord(("shift",), "a")]  # 大写字母 = shift 加小写字母（和 xdotool 一样）
    assert keys.parse("shift") == [keys.Chord(("shift",), "")]
    assert keys.parse("ctrl+minus") == [keys.Chord(("ctrl",), "-")]
    assert keys.Chord(("ctrl", "shift"), "t").shown() == "ctrl+shift+t"


def test_bad_key_names_are_an_error_for_the_model():
    for bad in ("", "+", "hyper+a", "ctrl+中", "Enterr", None, 5, "a+b"):
        with pytest.raises(ToolError):
            keys.parse(bad)


def test_modifiers_for_clicks():
    assert keys.modifiers(None) == () and keys.modifiers("") == ()
    assert keys.modifiers("shift+ctrl") == ("ctrl", "shift")
    assert keys.modifiers("Command+option") == ("alt", "cmd")
    with pytest.raises(ToolError):
        keys.modifiers("shift+q")
    with pytest.raises(ToolError):
        keys.modifiers(3)


def test_dangerous_chords_are_recognised_per_platform():
    assert "⌘Q" in keys.danger(keys.parse("cmd+q"), "macos")
    assert "⌘Q" in keys.danger(keys.parse("super+q"), "macos")
    assert "强制退出" in keys.danger(keys.parse("cmd+alt+Escape"), "macos")
    assert "Ctrl+Alt+Del" in keys.danger(keys.parse("ctrl+alt+Delete"), "macos")
    assert "Ctrl+Alt+Del" in keys.danger(keys.parse("ctrl+alt+Delete"), "x11")
    assert "Alt+F4" in keys.danger(keys.parse("alt+F4"), "x11")
    assert keys.danger(keys.parse("alt+F4"), "macos") == ""  # 只在 Linux 上算
    assert keys.danger(keys.parse("cmd+q"), "x11") == ""
    assert keys.danger(keys.parse("ctrl+s"), "macos") == ""
    assert keys.danger(keys.parse("Return"), "x11") == ""
    assert "⌘Q" in keys.danger(keys.parse("Tab cmd+q"), "macos")  # 一串里有一组危险的就算


# ---- 设置 ---------------------------------------------------------------------------


def test_computer_settings_only_keep_what_is_valid():
    assert computer_settings(None) == COMPUTER_DEFAULTS == {"enabled": False, "unattended": False, "blocked": []}
    assert computer_settings({"enabled": "yes", "unattended": 1})["enabled"] is False
    cleaned = computer_settings({"enabled": True, "blocked": ["  1Password ", "1Password", "", 5, "Mail   App", "x" * 300]})
    assert cleaned["enabled"] is True
    assert cleaned["blocked"] == ["1Password", "Mail App", "x" * 100]
    assert len(computer_settings({"blocked": [f"app{n}" for n in range(80)]})["blocked"]) == 50
    assert computer_settings({"blocked": "1Password"})["blocked"] == []


def test_computer_settings_are_saved_only_when_changed(make_config):
    from harness.settings import Settings

    config = make_config()
    settings = Settings(config.settings_file, "anthropic", "claude-opus-5-5")
    settings.save()
    assert "computer" not in config.settings_file.read_text(encoding="utf-8")  # 没动过的人，设置文件和以前一模一样
    settings.set_computer({"enabled": True, "blocked": ["Mail"]})
    again = Settings(config.settings_file, "anthropic", "claude-opus-5-5")
    assert again.computer == {"enabled": True, "unattended": False, "blocked": ["Mail"]}


def test_shot_store_keeps_its_own_number_of_days(tmp_path):
    now = time.time()
    store = ShotStore(tmp_path / "shots", clock=lambda: now, days=7)
    old, recent = store.save(b"old-jpeg"), store.save(b"recent-jpeg")
    os.utime(tmp_path / "shots" / f"{old}.jpg", (now - 8 * 86400, now - 8 * 86400))
    os.utime(tmp_path / "shots" / f"{recent}.jpg", (now - 6 * 86400, now - 6 * 86400))
    assert store.prune(force=True) == 1
    assert store.read(old) is None and store.read(recent) == b"recent-jpeg"
    assert ShotStore(tmp_path / "other").days == 30  # 浏览器的默认值不变


# ---- 管理者：开关、后端、几次运行共用一个屏幕 ----------------------------------------------------------


def test_ready_checks_the_switch_the_platform_and_the_permissions(tmp_path):
    manager, fake = make(tmp_path)
    assert manager.ready("look") is fake
    manager.settings.computer["enabled"] = False
    with pytest.raises(ToolError) as off:
        manager.ready("look")
    assert str(off.value) == OFF
    manager.settings.computer["enabled"] = True
    fake.problems["input"] = "没有辅助功能的权限"
    assert manager.ready("look") is fake  # 只是不能动鼠标键盘：截图照样能截
    with pytest.raises(ToolError, match="辅助功能"):
        manager.ready("input")
    manager._backend, manager._reason = None, "wayland"
    with pytest.raises(ToolError) as wayland:
        manager.ready("wait")
    assert str(wayland.value) == UNSUPPORTED["wayland"]


def test_the_real_backend_is_never_found_in_tests(tmp_path):
    # conftest 把"这台电脑上用哪个后端"换成了"用不了"：哪个测试忘了换成假的，也碰不到真的屏幕
    manager = DesktopManager(tmp_path, SimpleNamespace(computer={**COMPUTER_DEFAULTS, "enabled": True}))
    assert manager.backend() == (None, "platform")
    with pytest.raises(ToolError):
        manager.ready("look")


def test_only_one_run_operates_the_screen_at_a_time(tmp_path):
    manager, _ = make(tmp_path)
    chat, task = manager.session("你的对话"), manager.session("一个定时任务")
    chat.run("screenshot")
    assert manager.status()["active"] and manager.status()["who"] == "你的对话"
    assert manager.free_for(chat) and not manager.free_for(task)
    with pytest.raises(ToolError, match="你的对话"):
        task.run("screenshot")
    assert task.facts("screenshot").ready is False  # 被别人占着：不用问你，工具会直接回答
    chat.release()  # 这一轮结束
    assert not manager.status()["active"]
    task.run("screenshot")
    assert manager.status()["who"] == "一个定时任务"
    task.close()
    assert manager.free_for(chat)


def test_stop_all_keeps_the_holder_off_the_screen_until_its_turn_ends(tmp_path):
    manager, _ = make(tmp_path)
    chat, other = manager.session("你的对话"), manager.session("一个定时任务")
    chat.run("screenshot")
    manager.stop_all()
    assert chat.stopped_by_user and not other.stopped_by_user
    assert not manager.status()["active"]  # 屏幕让出来了
    with pytest.raises(ToolError) as stopped:
        chat.run("click", point=(10, 10))
    assert str(stopped.value) == STOPPED
    with pytest.raises(ToolError):
        chat.run("screenshot")  # 这一轮里一直不行（不是只拒绝一次）
    assert chat.facts("screenshot").ready is False
    chat.release()  # 这一轮结束：下一轮可以重新开始
    chat.run("screenshot")


def test_status_says_what_the_page_needs(tmp_path):
    manager, _ = make(tmp_path, FakeDesktop(width=1512, height=982, density=2), blocked=["Mail"])
    status = manager.status()
    assert status["enabled"] and status["supported"] and status["platform"] == "macos"
    assert status["screen"] == [1512, 982] and status["image"] == [1330, 864]
    assert status["shot_tokens"] == 1330 * 864 // 750
    assert status["checks"] == {"screen": True, "input": True, "host": "终端"}
    assert status["blocked"] == ["Mail"] and status["active"] is False and status["busy"] is False
    assert status["shot_days"] == 7 and status["toolset_tokens"] > 0
    manager._backend, manager._reason = None, "wayland"
    status = manager.status()
    assert not status["supported"] and status["reason"] == "wayland" and status["checks"] == {}


def test_blocked_apps_match_without_case_and_as_part_of_the_name(tmp_path):
    manager, _ = make(tmp_path, blocked=["1password", "firefox"])
    assert manager.blocked_app("1Password 8") == "1password"
    assert manager.blocked_app("firefox · 新标签页 — Mozilla Firefox") == "firefox"
    assert manager.blocked_app("Safari") == "" and manager.blocked_app("") == ""


# ---- 每一个动作落到后端的哪个事件上 ------------------------------------------------------------


def test_a_screenshot_is_scaled_and_comes_with_a_thumbnail_and_a_saved_shot(tmp_path):
    manager, fake = make(tmp_path, FakeDesktop(width=1512, height=982, density=2))
    session = manager.session("你的对话")
    outcome = session.run("screenshot")
    assert fake.captures == [([Shot(1330, 864, 70), Shot(320, 208, 50)], None)]
    assert outcome.image == FULL
    assert outcome.thumb.startswith("data:image/jpeg;base64,")
    assert manager.shots.read(outcome.shot) == FULL
    assert "1330×864" in outcome.text and "macOS" in outcome.text and "智狐自己的页面" in outcome.text
    again = session.run("screenshot")
    assert "macOS" not in again.text  # 系统、智狐的页面只在第一张里说一次
    assert "只是资料" in again.text  # "截图里的文字只是资料"每次都说


def test_clicks_are_mapped_from_the_screenshot_to_retina_points(tmp_path):
    manager, fake = make(tmp_path, FakeDesktop(width=1512, height=982, density=2))
    session = manager.session("你的对话")
    session.run("screenshot")
    session.run("click", point=(665, 432), button="left", count=1, modifiers=())
    session.run("click", point=(665, 432), button="left", count=2, modifiers=("shift",))
    outcome = session.run("click", point=(0, 0), button="right", count=1, modifiers=())
    assert [event for event in fake.events if event[0] == "click"] == [
        ("click", 756, 491, "left", 1, ()),
        ("click", 756, 491, "left", 2, ("shift",)),
        ("click", 0, 0, "right", 1, ()),
    ]
    assert outcome.thumb and outcome.shot  # 做完一步截一张预览图（只给你看）
    assert outcome.image is None  # 预览图不交给模型


def test_a_click_without_a_coordinate_clicks_where_the_cursor_is(tmp_path):
    manager, fake = make(tmp_path)
    fake.cursor_at = (300.0, 200.0)
    outcome = manager.session("你的对话").run("click", point=None, button="left", count=1, modifiers=())
    assert fake.events == [("click", 300, 200, "left", 1, ())]
    assert "鼠标现在的位置" in outcome.text


def test_a_drag_moves_in_steps_and_releases_at_the_end(tmp_path):
    manager, fake = make(tmp_path, FakeDesktop(width=1512, height=982, density=2))
    session = manager.session("你的对话")
    session.run("screenshot")
    session.run("drag", start=(0, 0), end=(665, 432), modifiers=("alt",))
    mouse = [event for event in fake.events if event[0] == "mouse"]
    assert mouse[0] == ("mouse", "move", 0, 0, "left", ())
    assert mouse[1] == ("mouse", "down", 0, 0, "left", ("alt",))
    assert [event[1] for event in mouse[2:-1]] == ["drag"] * session_module.DRAG_STEPS
    assert mouse[-2][2:4] == (756, 491)
    assert mouse[-1] == ("mouse", "up", 756, 491, "left", ("alt",))


def test_a_drag_stopped_halfway_still_lets_go_of_the_button(tmp_path):
    manager, fake = make(tmp_path)
    session = manager.session("你的对话")
    moves = []
    original = fake.mouse

    def mouse(kind, x, y, button="left", modifiers=()):
        original(kind, x, y, button, modifiers)
        if kind == "drag":
            moves.append((x, y))
            if len(moves) == 3:
                session.stop()

    fake.mouse = mouse
    with pytest.raises(ToolError, match="停下"):
        session.run("drag", start=(0, 0), end=(1200, 0), modifiers=())
    last = fake.events[-1]
    assert last[:2] == ("mouse", "up") and last[2:4] == moves[-1]  # 停在哪儿就在哪儿松开，不拖到终点去放下


def test_move_press_release_and_cursor(tmp_path):
    manager, fake = make(tmp_path, FakeDesktop(width=1512, height=982, density=2))
    session = manager.session("你的对话")
    session.run("screenshot")
    session.run("move", point=(665, 432))
    session.run("press", point=None)
    assert session.held
    session.run("move", point=(10, 10))  # 左键按着：移动就是拖动
    session.run("release", point=None)
    assert not session.held
    assert [event[:4] for event in fake.events] == [
        ("mouse", "move", 756, 491),
        ("mouse", "down", 756, 491),
        ("mouse", "drag", 11, 11),
        ("mouse", "up", 11, 11),
    ]
    fake.cursor_at = (756.0, 491.0)
    assert session.run("cursor").text == "X=665, Y=432"  # 报告给模型的是截图上的坐标


def test_a_button_left_held_is_released_when_the_turn_ends(tmp_path):
    manager, fake = make(tmp_path)
    session = manager.session("你的对话")
    session.run("press", point=(5, 5))
    session.release()
    assert fake.events[-1][:2] == ("mouse", "up") and not session.held


def test_scroll_type_key_and_hold(tmp_path):
    manager, fake = make(tmp_path)
    session = manager.session("你的对话")
    session.run("scroll", point=(100, 50), direction="down", amount=3, modifiers=("ctrl",))
    session.run("type", text="你好 world")
    session.run("key", text="ctrl+s", repeat=2)
    session.run("hold", text="shift", duration=0)
    assert fake.events == [
        ("scroll", 100, 50, "down", 3, ("ctrl",)),
        ("type", "你好 world"),
        ("key", "ctrl+s", "tap"),
        ("key", "ctrl+s", "tap"),
        ("key", "shift", "down"),
        ("key", "shift", "up"),
    ]


def test_zoom_captures_the_region_at_full_resolution(tmp_path):
    manager, fake = make(tmp_path, FakeDesktop(width=1512, height=982, density=2))
    session = manager.session("你的对话")
    session.run("screenshot")
    outcome = session.run("zoom", region=[100, 200, 400, 350])
    assert fake.captures[-1] == ([Shot(1330, 665, 70), Shot(320, 160, 50)], Box(113, 227, 342, 171))
    assert outcome.image == FULL and "1330×864" in outcome.text  # 坐标仍然按整张截图算


def test_wait_does_not_touch_the_screen(tmp_path):
    manager, fake = make(tmp_path)
    outcome = manager.session("你的对话").run("wait", duration=0)
    assert outcome.text == "等了 0 秒。" and fake.events == [] and fake.captures == []


# ---- 停下 ---------------------------------------------------------------------------


def _in_thread(run) -> tuple[threading.Thread, list]:
    caught: list = []

    def target():
        try:
            run()
        except ToolError as exc:
            caught.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    return thread, caught


def test_stop_wakes_up_a_wait_at_once(tmp_path):
    manager, _ = make(tmp_path)
    session = manager.session("你的对话")
    started = time.monotonic()
    thread, caught = _in_thread(lambda: session.run("wait", duration=20))
    time.sleep(0.1)
    session.stop()
    thread.join(5)
    assert time.monotonic() - started < 3 and caught and "停下" in str(caught[0])


def test_stop_during_hold_key_still_lets_go_of_the_key(tmp_path):
    manager, fake = make(tmp_path)
    session = manager.session("你的对话")
    thread, caught = _in_thread(lambda: session.run("hold", text="shift", duration=20))
    time.sleep(0.1)
    session.stop()
    thread.join(5)
    assert caught and fake.events == [("key", "shift", "down"), ("key", "shift", "up")]


def test_stop_during_typing_stops_after_the_current_piece(tmp_path):
    manager, fake = make(tmp_path)
    session = manager.session("你的对话")
    fake.type_hook = lambda piece: session.stop()
    with pytest.raises(ToolError, match="停下"):
        session.run("type", text="abcdefghijkl")
    assert fake.typed == ["abcd"]


# ---- 动手之前再看一眼 ------------------------------------------------------------------


def test_a_changed_screen_size_asks_for_a_new_screenshot(tmp_path):
    manager, fake = make(tmp_path)
    session = manager.session("你的对话")
    session.run("screenshot")
    fake.size = Screen(1920, 1080, 1920, 1080)
    with pytest.raises(ToolError, match="屏幕的大小变了"):
        session.run("click", point=(10, 10), button="left", count=1, modifiers=())
    assert fake.events == []
    session.run("click", point=(10, 10), button="left", count=1, modifiers=())  # 之后按现在的大小算
    assert fake.events == [("click", 13, 13, "left", 1, ())]


def test_blocked_apps_are_never_looked_at_or_touched(tmp_path):
    manager, fake = make(tmp_path, blocked=["1Password"])
    session = manager.session("你的对话")
    fake.front_app = ("1Password 8", "7")
    with pytest.raises(ToolError, match="不许碰"):
        session.run("screenshot")
    assert fake.captures == []
    assert session.facts("screenshot").blocked == "1Password 8"
    fake.front_app = ("Safari", "8")
    fake.app_at_point = lambda x, y: "1Password 8"
    with pytest.raises(ToolError, match="不许碰"):
        session.run("click", point=(10, 10), button="left", count=1, modifiers=())
    assert session.facts("click", point=(10, 10)).blocked == "1Password 8"
    assert fake.events == []


def test_no_preview_is_taken_when_a_blocked_app_comes_to_the_front(tmp_path):
    manager, fake = make(tmp_path, blocked=["1Password"])
    original = fake.click

    def click(*args):
        original(*args)
        fake.front_app = ("1Password", "7")  # 这一下点开了不许碰的程序

    fake.click = click
    outcome = manager.session("你的对话").run("click", point=(1, 1), button="left", count=1, modifiers=())
    assert outcome.thumb == "" and fake.captures == []


def test_a_click_is_not_made_when_another_window_now_covers_the_spot(tmp_path):
    manager, fake = make(tmp_path)
    session = manager.session("你的对话")
    asked = session.facts("click", point=(10, 10))
    assert asked.app == "备忘录"
    fake.app_at_point = lambda x, y: "Google Chrome"  # 你点"允许"时浏览器跳到了最前面，挡住了那个位置
    with pytest.raises(ToolError, match="问用户的时候"):
        session.run("click", point=(10, 10), button="left", count=1, modifiers=(), asked=asked)
    assert fake.events == []


def test_typing_brings_back_the_app_that_was_in_front_when_asked(tmp_path):
    manager, fake = make(tmp_path)
    session = manager.session("你的对话")
    asked = session.facts("type", text="你好")
    assert (asked.app, asked.handle) == ("备忘录", "101")
    fake.front_app = ("Google Chrome", "9")  # 你在智狐的页面上点了"允许"
    session.run("type", text="你好", asked=asked)
    assert fake.activations == ["101"] and fake.events == [("type", "你好")]


def test_typing_is_refused_when_the_app_cannot_be_brought_back(tmp_path):
    manager, fake = make(tmp_path)
    session = manager.session("你的对话")
    asked = Facts("key", app="已经关掉的程序", handle="999")
    with pytest.raises(ToolError, match="没能把"):
        session.run("key", text="Return", repeat=1, asked=asked)
    assert fake.activations == ["999"] and fake.events == []


def test_typing_into_the_app_already_in_front_does_not_switch(tmp_path):
    manager, fake = make(tmp_path)
    session = manager.session("你的对话")
    asked = session.facts("key", text="Return")
    session.run("key", text="Return", repeat=1, asked=asked)
    assert fake.activations == [] and fake.events == [("key", "return", "tap")]


# ---- 问你之前看到的事实 ------------------------------------------------------------------


def test_facts_report_what_an_action_touches_without_doing_anything(tmp_path):
    manager, fake = make(tmp_path, FakeDesktop(width=1512, height=982, density=2))
    session = manager.session("你的对话")
    facts = session.facts("click", point=(665, 432), button="left", count=2, modifiers=("shift",))
    assert facts.ready and not facts.bad
    assert facts.on_screen == (756, 491) and facts.image == (1330, 864) and facts.app == "备忘录"
    assert facts.count == 2 and facts.modifiers == ("shift",)
    assert session.facts("click", point=(5000, 1)).bad  # 坐标不在截图里：工具会直接报错，不用问
    assert session.facts("key", text="cmd+q", repeat=1).danger.startswith("⚠ ⌘Q")
    assert session.facts("type", text="第一行\n第二行").newline
    assert session.facts("zoom", region=[1, 1, 2, 2]).bad
    assert fake.events == [] and fake.captures == []  # 只是看，什么都没做
    manager.settings.computer["enabled"] = False
    assert session.facts("screenshot").ready is False
