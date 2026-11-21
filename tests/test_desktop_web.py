"""网页上的"操作整台电脑"：设置里的开关（打开要先确认）、"这段对话里截图不再问""这段对话里的点击、打字不再问"、
页面顶上的"停止"、截图的大图、确认卡片上多给的那个选择、打开 macOS 系统设置里的那一页。

真实的 HTTP 服务器 + 假的模型（test_webui.py 的 serve），电脑换成 FakeDesktop，不碰真的屏幕。要守住的规矩：
  - 打开它是一件要想清楚的事：不带 acknowledge 的"打开"一律拒绝；
  - "这段对话里不再问"只能从正在等你答复、给了这个选择的那张卡片上打开（/api/computer/trust 要卡片的编号），
    危险的组合键的卡片不给；"全部自动允许"下电脑的每一步不弹卡片，记着"自动允许"；
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
from test_webui import capability, latest_state

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
    install(s.app.desktop, FakeDesktop())  # 信息栏里要看到"在用"：这台（假的）电脑上做得了
    status, answer = settings(s, enabled=True)
    assert status == 400 and "风险" in answer["error"]
    assert s.app.settings.computer["enabled"] is False
    status, answer = settings(s, enabled=True, acknowledge=True)
    assert status == 200 and answer["ok"]
    assert json.loads(s.config.settings_file.read_text(encoding="utf-8"))["computer"]["enabled"] is True
    wait_until(lambda: latest_state(s.browser)["computer"]["enabled"])
    computer = capability(latest_state(s.browser), "computer")
    assert computer["status"] == "on" and computer["tools"] == ["computer_toolset_20260801"]  # 还没开始的对话：马上换上了
    assert computer["approval"]["level"] == "ask"  # "每次都问"这一档：每一步都先问你（危险的组合键哪一档都问）
    # 已经开着的时候改别的，不用再确认
    assert settings(s, unattended=True)[0] == 200 and s.app.settings.computer["unattended"] is True


def test_turning_it_on_mid_conversation_says_it_starts_next_time(serve):
    s = serve(reply(text("你好")))
    install(s.app.desktop, FakeDesktop())
    s.browser.post("/api/message", {"text": "嗨"})
    s.browser.wait_for("turn_end")
    status, answer = settings(s, enabled=True, acknowledge=True)
    assert status == 200 and "下一段新对话" in answer["warning"]
    wait_until(lambda: latest_state(s.browser)["computer"]["enabled"])
    computer = capability(latest_state(s.browser), "computer")
    assert computer["status"] == "pending" and computer["tools"] == []  # 信息栏里写着"下一段对话开始生效"
    assert computer["actions"][0]["kind"] == "new"


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


def trust(s, what: str, confirm) -> tuple[int, dict]:
    return s.browser.post("/api/computer/trust", {"what": what, "confirm": confirm})


def test_trusting_is_only_from_the_card_that_offers_it_and_only_when_on(serve, monkeypatch):
    """"这段对话里不再问"只能从正在等你答复、而且给了这个选择的那张卡片上点出来：
    词不对是 400；关着、卡片不对（编号不对、答复过了、问的不是这一类）都是 409，什么都不放开。"""
    monkeypatch.setattr(session_module, "DESKTOP_SETTLE_SECONDS", 0)
    s = serve(reply(tool_use("s1", "screenshot", {}, toolset="computer"), stop_reason="tool_use"), reply(text("看完了")))
    install(s.app.desktop, FakeDesktop())
    assert trust(s, "screenshots", 1)[0] == 409  # 关着
    for bad in ({}, {"what": "everything", "confirm": 1}, {"what": ["actions"], "confirm": 1}):
        assert s.browser.post("/api/computer/trust", bad)[0] == 400
    settings(s, enabled=True, acknowledge=True)
    assert trust(s, "screenshots", 1)[0] == 409  # 没有在等的卡片
    s.browser.post("/api/message", {"text": "看看屏幕"})
    card = s.browser.wait_for("confirm")
    assert card["offer"] == "screenshots" and not card.get("always")
    for wrong in ((card["id"] + 1), str(card["id"]), True, None):
        assert trust(s, "screenshots", wrong)[0] == 409
    assert trust(s, "actions", card["id"])[0] == 409  # 这张卡片问的是截图，给的不是这个选择
    assert not s.app.chat_desktop.trust_actions and not s.app.chat_desktop.trust_screenshots
    assert trust(s, "screenshots", card["id"]) == (200, {"ok": True})
    assert s.app.chat_desktop.trust_screenshots and not s.app.chat_desktop.trust_actions
    wait_until(lambda: latest_state(s.browser)["computer"]["trusted"])
    assert s.browser.post("/api/confirm", {"id": card["id"], "allow": True})[0] == 200
    s.browser.wait_for("turn_end")
    assert trust(s, "screenshots", card["id"])[0] == 409  # 答复过了
    assert s.browser.post("/api/new", {})[0] == 200  # 换一段对话：从头再问
    assert s.app.chat_desktop.trust_screenshots is False


def test_trusting_clicks_and_typing_from_the_card_and_taking_it_back(serve, monkeypatch):
    """点击的卡片给的是"这段对话里的点击、打字不再问"：点了以后打字、按键不问（工具卡片上标着为什么），截图照样问；
    危险的组合键照样问、不给选择。设置里的"恢复每一步都问"能收回。"""
    monkeypatch.setattr(session_module, "DESKTOP_SETTLE_SECONDS", 0)
    s = serve(
        reply(
            tool_use("c1", "left_click", {"coordinate": [10, 20]}, toolset="computer"),
            tool_use("t1", "type", {"text": "你好"}, toolset="computer"),
            tool_use("s1", "screenshot", {}, toolset="computer"),
            tool_use("k1", "key", {"text": "cmd+q"}, toolset="computer"),
            stop_reason="tool_use",
        ),
        reply(text("做完了")),
    )
    fake = FakeDesktop()
    install(s.app.desktop, fake)
    settings(s, enabled=True, acknowledge=True)
    s.browser.post("/api/message", {"text": "点一下再打字"})
    click = s.browser.wait_for("confirm")
    assert click["offer"] == "actions" and not click.get("always") and click["key"].startswith("在电脑上点击鼠标")
    assert trust(s, "actions", click["id"]) == (200, {"ok": True})
    assert s.browser.post("/api/confirm", {"id": click["id"], "allow": True})[0] == 200
    auto = s.browser.wait_for("auto_allowed")
    assert auto["why"] == "trusted"  # 打字没问：页面上写"自动允许（这段对话里不再问）"
    shot = s.browser.wait_for("confirm")
    assert shot["offer"] == "screenshots"  # 截图另外选
    assert s.browser.post("/api/confirm", {"id": shot["id"], "allow": True})[0] == 200
    danger = s.browser.wait_for("confirm")
    assert danger.get("always") is True and "offer" not in danger  # ⌘Q：放开了也照样问，不给选择
    assert trust(s, "actions", danger["id"])[0] == 409
    assert s.browser.post("/api/confirm", {"id": danger["id"], "allow": False})[0] == 200
    s.browser.wait_for("turn_end")
    assert ("type", "你好") in fake.events and not [event for event in fake.events if event[0] == "key"]
    wait_until(lambda: latest_state(s.browser)["computer"]["trusted_actions"])
    assert s.browser.post("/api/computer/untrust", {}) == (200, {"ok": True})
    assert not s.app.chat_desktop.trust_actions and not s.app.chat_desktop.trust_screenshots
    wait_until(lambda: not latest_state(s.browser)["computer"]["trusted_actions"])


def test_everything_allowed_operates_without_cards_in_the_web_chat(serve, monkeypatch):
    """网页上选了"全部自动允许"：电脑的每一步都不弹卡片，工具卡片上标着"自动允许"（why 是 mode）。"""
    monkeypatch.setattr(session_module, "DESKTOP_SETTLE_SECONDS", 0)
    s = serve(
        reply(tool_use("s1", "screenshot", {}, toolset="computer"), tool_use("c1", "left_click", {"coordinate": [10, 20]}, toolset="computer"), stop_reason="tool_use"),
        reply(text("做完了")),
    )
    fake = FakeDesktop()
    install(s.app.desktop, fake)
    settings(s, enabled=True, acknowledge=True)
    assert s.browser.post("/api/approval", {"mode": "all"})[0] == 200
    s.browser.post("/api/message", {"text": "看看再点"})
    s.browser.wait_for("turn_end")
    events = [event for event in s.browser.events if event.get("type") in ("confirm", "auto_allowed")]
    assert [event["type"] for event in events] == ["auto_allowed", "auto_allowed"]
    assert all(event["why"] == "mode" for event in events)
    assert ("click", 10, 20, "left", 1, ()) in fake.events


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
    assert trust(s, "screenshots", confirm["id"])[0] == 200
    assert s.browser.post("/api/confirm", {"id": confirm["id"], "allow": True})[0] == 200
    shot = s.browser.wait_for("tool_result")
    assert shot["thumb"].startswith("data:image/jpeg;base64,") and len(shot["shot"]) == 64
    click = s.browser.wait_for("confirm")
    assert click["offer"] == "actions"  # 点击另有一个选择：放开截图不会连点击一起放开
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
