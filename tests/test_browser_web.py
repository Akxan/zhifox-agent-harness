"""网页上的浏览器：设置里的开关、页面顶上的"正在被智狐控制"、截图的大图、配置文件夹。

真实的 HTTP 服务器 + 假的模型（test_webui.py 的 serve），浏览器本身换成假的（test_browser_tools.py 的 FakeSession），
不启动 Chrome。要守住的规矩：
  - 开关改的是设置文件，状态里跟着变；还没开始的对话马上换上工具，开始了的不动；
  - 截图的大图只认 64 个十六进制字符的编号，回答固定是 image/jpeg，带 attachment、nosniff、sandbox（和多媒体库的原件一样）；
  - "在访达中显示"只打开配置文件夹，路径由服务器定；清除配置文件夹在浏览器忙的时候不做；
  - 工具卡片上的小图写在 tool_result 事件里，格式不对的不放进去。
"""

from __future__ import annotations

import http.client
import json
import time

import test_webui
from fake_api import reply, text, tool_use
from modes import has_mode
from test_browser_tools import STATE, FakeSession
from test_webui import latest_state

from harness.browser.session import Outcome
from harness.webui.ui import WebUI, shot_fields

serve = test_webui.serve  # 借用 test_webui.py 的 fixture（pytest 按模块里的名字找 fixture）

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
THUMB = "data:image/jpeg;base64,/9j/4AAQ"


def capability(state: dict, name: str) -> dict:
    """信息栏"智狐能做的事"里的一项（见 harness/capabilities.py）。"""
    return next(item for item in state["capabilities"]["items"] if item["id"] == name)


def settings(s, **changes) -> tuple[int, dict]:
    return s.browser.post("/api/browser/settings", changes)


def test_the_state_says_where_the_browser_stands(serve):
    s = serve()
    browser = latest_state(s.browser)["browser"]
    assert browser["enabled"] is False and browser["running"] is False and browser["busy"] is False
    assert browser["profile"].endswith("browser/profile") and browser["error"] is None
    assert browser["shot_days"] > 0


def test_turning_it_on_saves_the_setting_and_gives_a_new_conversation_the_tools(serve):
    s = serve()
    status, answer = settings(s, enabled=True)
    assert status == 200 and answer["ok"]
    assert json.loads(s.config.settings_file.read_text(encoding="utf-8"))["browser"]["enabled"] is True
    deadline = time.monotonic() + 5  # 新的状态是在回答之前发的，"浏览器"收到它可能还要一小会儿
    while not latest_state(s.browser)["browser"]["enabled"]:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    state = latest_state(s.browser)
    assert state["browser"]["enabled"] is True
    assert capability(state, "browser")["status"] == "on"  # 还没开始的对话：马上换上了


def test_turning_it_on_mid_conversation_says_it_starts_next_time(serve):
    s = serve(reply(text("你好")))
    s.browser.post("/api/message", {"text": "嗨"})
    s.browser.wait_for("turn_end")
    status, answer = settings(s, enabled=True)
    assert status == 200 and "下一段新对话" in answer["warning"]
    deadline = time.monotonic() + 5  # 新的状态是在回答之前发的，"浏览器"收到它可能还要一小会儿
    while not latest_state(s.browser)["browser"]["enabled"]:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert capability(latest_state(s.browser), "browser")["status"] == "pending"  # 工具没换上，信息栏里写着"下一段对话开始生效"


def test_bad_settings_are_rejected(serve):
    s = serve()
    assert settings(s, enabled="yes")[0] == 400
    assert settings(s, path="/bin/\x00chrome")[0] == 400
    assert settings(s, path="x" * 2000)[0] == 400
    assert s.browser.post("/api/browser/settings", {})[0] == 400
    assert s.browser.post("/api/browser/nope", {})[0] == 404


def test_a_wrong_chrome_path_is_reported(serve):
    s = serve()
    status, answer = settings(s, enabled=True, path="/no/such/chrome")
    assert status == 200 and "找不到 Chrome" in answer["warning"]
    deadline = time.monotonic() + 5  # 新的状态是在回答之前发的，但"浏览器"收到它可能还要一小会儿
    while latest_state(s.browser)["browser"]["path"] != "/no/such/chrome":
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert latest_state(s.browser)["browser"]["chrome"] == ""


def test_stop_stops_every_run_and_closes_the_browser(serve, monkeypatch):
    s = serve()
    calls = []
    monkeypatch.setattr(s.app.browser, "stop_all", lambda: calls.append("stop_all"))
    assert s.browser.post("/api/browser/stop", {}) == (200, {"ok": True})
    assert calls == ["stop_all"]


def test_reveal_opens_only_the_profile_folder(serve):
    s = serve()
    status, answer = s.browser.post("/api/browser/reveal", {"path": "/etc"})  # 页面给的路径一律不看
    assert status == 200 and answer["path"] == str(s.app.browser.profile)
    assert s.revealed == [s.app.browser.profile] and s.app.browser.profile.is_dir()
    assert has_mode(s.app.browser.profile, 0o700)


def test_clearing_the_profile_removes_it_but_not_while_busy(serve):
    s = serve()
    profile = s.app.browser.profile
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Cookies").write_text("x", encoding="utf-8")
    s.app.browser._busy = 1
    status, answer = s.browser.post("/api/browser/clear", {})
    assert status == 409 and "正在使用" in answer["error"] and profile.exists()
    s.app.browser._busy = 0
    assert s.browser.post("/api/browser/clear", {}) == (200, {"ok": True})
    assert not profile.exists()


def test_a_screenshot_is_served_like_a_media_original(serve):
    s = serve()
    shot = s.app.browser.shots.save(JPEG)
    host, token = s.browser.host, s.browser.token
    connection = http.client.HTTPConnection(host, timeout=10)
    headers = {"X-Harness-Token": token, "Origin": f"http://{host}", "Content-Type": "application/json"}
    connection.request("POST", "/api/browser/shot", body=json.dumps({"id": shot}), headers=headers)
    response = connection.getresponse()
    assert response.status == 200 and response.read() == JPEG
    got = {name.lower(): value for name, value in response.getheaders()}
    connection.close()
    assert got["content-type"] == "image/jpeg"
    assert got["x-content-type-options"] == "nosniff"
    assert got["content-disposition"].startswith("attachment")
    assert got["content-security-policy"] == "default-src 'none'; sandbox"
    for bad in ("../../settings.json", "A" * 64, shot[:-1], 12, None):
        assert s.browser.post("/api/browser/shot", {"id": bad})[0] == 409
    assert s.browser.post("/api/browser/shot", {"id": "f" * 64})[0] == 409  # 格式对、但没有这张（过期删掉了）


def test_a_browser_step_shows_a_thumbnail_in_the_conversation(serve):
    s = serve(
        reply(tool_use("t1", "navigate", {"url": "https://example.com"}, toolset="browser"), stop_reason="tool_use"),
        reply(text("打开了")),
    )
    settings(s, enabled=True)
    fake = FakeSession(outcomes={"navigate": Outcome(text="已打开", state=STATE, thumb=THUMB, shot="b" * 64)})
    s.app.chat_browser = fake
    s.app.settings.browser["enabled"] = True
    s.app.agent.permissions.mode = "all"
    s.browser.post("/api/message", {"text": "打开 example.com"})
    call = s.browser.wait_for("tool_call")
    result = s.browser.wait_for("tool_result")
    assert call["name"] == "browser.navigate" and call["summary"].startswith("browser.navigate https://example.com")
    assert result["thumb"] == THUMB and result["shot"] == "b" * 64
    s.browser.wait_for("turn_end")
    # 存进会话里的也带着小图：打开旧会话时照样画得出来
    s.app.save_conversation(s.ui.export_events())
    saved = next(event for event in s.ui.export_events() if event["type"] == "tool_result")
    assert saved["thumb"] == THUMB


def test_only_well_formed_thumbnails_reach_the_page():
    assert shot_fields(None) == {}
    assert shot_fields({"thumb": THUMB, "shot": "c" * 64}) == {"thumb": THUMB, "shot": "c" * 64}
    assert shot_fields({"thumb": THUMB, "shot": "../x"}) == {"thumb": THUMB}
    assert shot_fields({"thumb": "javascript:alert(1)"}) == {}
    assert shot_fields({"thumb": "data:image/svg+xml;base64,PHN2Zz4="}) == {}
    assert shot_fields({"thumb": THUMB + "A" * 100_000}) == {}  # 太大的不放进事件
    ui = WebUI()
    ui.tool_result("好", False, {"thumb": THUMB, "shot": "d" * 64})
    assert ui.export_events()[-1]["thumb"] == THUMB
