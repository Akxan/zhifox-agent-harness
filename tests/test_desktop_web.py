"""网页上的"操作整台电脑"：设置里的开关（打开要先确认）、"这段对话里截图不再问"、页面顶上的"停止"、截图的大图、
确认卡片上多给的那个选择、打开 macOS 系统设置里的那一页。

真实的 HTTP 服务器 + 假的模型（test_webui.py 的 serve），电脑换成 FakeDesktop，不碰真的屏幕。要守住的规矩：
  - 打开它是一件要想清楚的事：不带 acknowledge 的"打开"一律拒绝；
  - 截图的大图只认 64 个十六进制字符的编号，回答固定是 image/jpeg，带 attachment、nosniff、sandbox（和浏览器的一样）；
  - "打开系统设置"只认两个固定的词，地址由服务器定，命令用参数列表；测试里绝不真的打开任何东西；
  - 一轮结束，屏幕让出来；对话里的"停止"也停下电脑的动作。
"""

from __future__ import annotations

import http.client
import json
import time

import test_webui
from fake_api import reply, text, tool_use
from fake_desktop import FULL, FakeDesktop, install
from test_webui import latest_state

from harness.desktop import session as session_module
from harness.webui import computer_api

serve = test_webui.serve  # 借用 test_webui.py 的 fixture（pytest 按模块里的名字找 fixture）


def settings(s, **changes) -> tuple[int, dict]:
    return s.browser.post("/api/computer/settings", changes)


def web_of(s):
    """这个服务器的 WebApp（serve 没有直接给，从挂在 desktop 上的回调找到它）。"""
    return s.app.desktop.on_change.__self__


def wait_until(check, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout  # 新的状态是在回答之前发的，"浏览器"收到它可能还要一小会儿
    while not check():
        assert time.monotonic() < deadline
        time.sleep(0.02)


def test_the_state_says_where_computer_use_stands(serve):
    s = serve()
    computer = latest_state(s.browser)["computer"]
    assert computer["enabled"] is False and computer["unattended"] is False and computer["blocked"] == []
    assert computer["supported"] is False and computer["reason"] == "platform"  # 测试里永远找不到真的后端
    assert computer["active"] is False and computer["busy"] is False and computer["trusted"] is False
    assert computer["providers"] == {"anthropic": "toolset", "openai": "standin", "gemini": "standin", "deepseek": "none"}
    assert computer["shot_days"] == 7 and computer["toolset_tokens"] > 0 and computer["shot_tokens"] > 0
    assert isinstance(computer["can_open_settings"], bool)


def test_turning_it_on_needs_an_explicit_acknowledgement(serve):
    s = serve()
    status, answer = settings(s, enabled=True)
    assert status == 400 and "风险" in answer["error"]
    assert s.app.settings.computer["enabled"] is False
    status, answer = settings(s, enabled=True, acknowledge=True)
    assert status == 200 and answer["ok"]
    assert json.loads(s.config.settings_file.read_text(encoding="utf-8"))["computer"]["enabled"] is True
    wait_until(lambda: latest_state(s.browser)["computer"]["enabled"])
    assert {"name": "computer", "local": True} in latest_state(s.browser)["tools"]  # 还没开始的对话：马上换上了
    # 已经开着的时候改别的，不用再确认
    assert settings(s, unattended=True)[0] == 200 and s.app.settings.computer["unattended"] is True


def test_turning_it_on_mid_conversation_says_it_starts_next_time(serve):
    s = serve(reply(text("你好")))
    s.browser.post("/api/message", {"text": "嗨"})
    s.browser.wait_for("turn_end")
    status, answer = settings(s, enabled=True, acknowledge=True)
    assert status == 200 and "下一段新对话" in answer["warning"]
    assert not [tool for tool in latest_state(s.browser)["tools"] if tool["name"] == "computer"]


def test_turning_it_off_stops_whatever_is_running(serve, monkeypatch):
    s = serve()
    settings(s, enabled=True, acknowledge=True)
    calls = []
    monkeypatch.setattr(s.app.desktop, "stop_all", lambda: calls.append("stop_all"))
    assert settings(s, enabled=False)[0] == 200
    assert calls == ["stop_all"] and s.app.settings.computer["enabled"] is False


def test_blocked_apps_are_saved_and_checked(serve):
    s = serve()
    assert settings(s, blocked=["1Password", "  Mail  "])[0] == 200
    assert s.app.settings.computer["blocked"] == ["1Password", "Mail"]
    wait_until(lambda: latest_state(s.browser)["computer"]["blocked"] == ["1Password", "Mail"])
    for bad in ("1Password", ["x" * 101], [""], ["a\x00b"], [3], ["app"] * 51):
        status, _ = settings(s, blocked=bad)
        assert status == 400, bad
    assert settings(s, enabled="yes")[0] == 400
    assert settings(s, unattended=1)[0] == 400
    assert s.browser.post("/api/computer/settings", {})[0] == 400
    assert s.browser.post("/api/computer/nope", {})[0] == 404


def test_trusting_screenshots_is_only_for_this_conversation_and_only_when_on(serve):
    s = serve()
    status, _ = s.browser.post("/api/computer/trust", {})
    assert status == 409 and s.app.chat_desktop.trust_screenshots is False
    settings(s, enabled=True, acknowledge=True)
    assert s.browser.post("/api/computer/trust", {}) == (200, {"ok": True})
    assert s.app.chat_desktop.trust_screenshots is True
    wait_until(lambda: latest_state(s.browser)["computer"]["trusted"])
    assert s.browser.post("/api/new", {})[0] == 200  # 换一段对话：从头再问
    assert s.app.chat_desktop.trust_screenshots is False


def test_stop_reaches_the_manager(serve, monkeypatch):
    s = serve()
    calls = []
    monkeypatch.setattr(s.app.desktop, "stop_all", lambda: calls.append("stop_all"))
    assert s.browser.post("/api/computer/stop", {}) == (200, {"ok": True})
    assert calls == ["stop_all"]


def test_the_chat_stop_button_also_stops_the_computer(serve):
    s = serve()
    calls = []
    s.app.chat_desktop.stop = lambda: calls.append("stop")
    assert s.browser.post("/api/stop", {}) == (200, {"ok": True})
    assert calls == ["stop"]


def test_the_permission_pane_opens_only_two_fixed_pages_and_only_on_macos(serve, monkeypatch):
    s = serve()
    opened = []
    web_of(s).computer._open = lambda command, **options: opened.append((command, options))  # 测试里绝不真的打开系统设置
    monkeypatch.setattr(computer_api.sys, "platform", "darwin")
    assert s.browser.post("/api/computer/permission", {"pane": "screen"}) == (200, {"ok": True})
    assert s.browser.post("/api/computer/permission", {"pane": "input"}) == (200, {"ok": True})
    assert [command for command, _ in opened] == [
        ["/usr/bin/open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"],
        ["/usr/bin/open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
    ]
    assert all("ANTHROPIC_API_KEY" not in options["env"] for _, options in opened)  # 子进程不带各家的密钥
    for bad in ("/Applications/Calculator.app", "screen; rm -rf /", None, 3):
        assert s.browser.post("/api/computer/permission", {"pane": bad})[0] == 400
    monkeypatch.setattr(computer_api.sys, "platform", "linux")
    assert s.browser.post("/api/computer/permission", {"pane": "screen"})[0] == 400
    assert len(opened) == 2


def test_a_screenshot_is_served_like_a_media_original(serve):
    s = serve()
    shot = s.app.desktop.shots.save(FULL)
    host, token = s.browser.host, s.browser.token
    connection = http.client.HTTPConnection(host, timeout=10)
    headers = {"X-Harness-Token": token, "Origin": f"http://{host}", "Content-Type": "application/json"}
    connection.request("POST", "/api/computer/shot", body=json.dumps({"id": shot}), headers=headers)
    response = connection.getresponse()
    assert response.status == 200 and response.read() == FULL
    got = {name.lower(): value for name, value in response.getheaders()}
    connection.close()
    assert got["content-type"] == "image/jpeg"
    assert got["x-content-type-options"] == "nosniff"
    assert got["content-disposition"].startswith("attachment")
    assert got["content-security-policy"] == "default-src 'none'; sandbox"
    assert (s.app.desktop.shots.folder / f"{shot}.jpg").exists()
    assert s.app.browser.shots.read(shot) is None  # 和浏览器的截图分开存
    for bad in ("../../settings.json", "A" * 64, shot[:-1], 12, None):
        assert s.browser.post("/api/computer/shot", {"id": bad})[0] == 409
    status, answer = s.browser.post("/api/computer/shot", {"id": "f" * 64})
    assert status == 409 and "7 天" in answer["error"]  # 格式对、但没有这张（过期删掉了）


def test_a_screenshot_confirm_offers_to_stop_asking_and_the_step_shows_a_thumbnail(serve, monkeypatch):
    monkeypatch.setattr(session_module, "DESKTOP_SETTLE_SECONDS", 0)
    s = serve(
        reply(tool_use("s1", "screenshot", {}, toolset="computer"), tool_use("c1", "left_click", {"coordinate": [10, 20]}, toolset="computer"), stop_reason="tool_use"),
        reply(text("看完了")),
    )
    fake = FakeDesktop()
    install(s.app.desktop, fake)
    settings(s, enabled=True, acknowledge=True)
    s.browser.post("/api/message", {"text": "看看屏幕"})
    confirm = s.browser.wait_for("confirm")
    assert confirm["offer"] == "screenshots" and confirm["key"].startswith("截一张整个屏幕的图")
    # 页面上的"允许，这段对话里截图不再问"：先告诉服务器不再问，再允许这一次
    assert s.browser.post("/api/computer/trust", {})[0] == 200
    assert s.browser.post("/api/confirm", {"id": confirm["id"], "allow": True})[0] == 200
    shot = s.browser.wait_for("tool_result")
    assert shot["thumb"].startswith("data:image/jpeg;base64,") and len(shot["shot"]) == 64
    click = s.browser.wait_for("confirm")
    assert "offer" not in click  # 点击永远不能"不再问"
    assert s.browser.post("/api/confirm", {"id": click["id"], "allow": True})[0] == 200
    result = s.browser.wait_for("tool_result")
    assert result["thumb"] and not result["is_error"]
    s.browser.wait_for("turn_end")
    assert fake.events == [("click", 10, 20, "left", 1, ())]
    calls = [event for event in s.browser.of_type("tool_call")]
    assert [call["name"] for call in calls] == ["computer.screenshot", "computer.left_click"]
    assert calls[1]["summary"] == "computer.left_click (10, 20)"
    # 一轮结束：屏幕让出来了
    wait_until(lambda: not latest_state(s.browser)["computer"]["active"])
    assert s.app.desktop.free_for(s.app.desktop.session("一个定时任务"))
