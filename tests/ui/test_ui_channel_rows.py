"""会话列表里，在聊天通道里聊的那一段写着平台的名字（Telegram），刷新以后也一样。

以前页面收到状态时先画会话列表、后读通道的名单，名单还没读到，行上就写成了平台的编号（小写的 telegram），
要等会话列表因为别的事重画一次才变对（重新截 README 的截图时发现的）。
"""

from __future__ import annotations

import json
import time
import urllib.request

from fake_telegram import TOKEN

from . import steps


def _phone_says(page, text: str) -> None:
    """像主人在手机上给机器人发了一条消息（演示里的假 Telegram，见 tests/fake_telegram.py 的 /control/say）。"""
    deadline = time.monotonic() + 10
    while not page.demo.telegram and time.monotonic() < deadline:
        time.sleep(0.05)  # 演示的输出里，假 Telegram 的地址在页面地址之后才打印
    assert page.demo.telegram, "没读到假 Telegram 的地址"
    request = urllib.request.Request(page.demo.telegram + "/control/say", data=json.dumps({"text": text}).encode(), method="POST")
    with urllib.request.urlopen(request, timeout=10) as answer:
        assert answer.status == 200


def test_a_conversation_from_a_chat_channel_names_its_platform_after_a_reload(page):
    status, _ = page.demo.api("/api/channel/connect", {"platform": "telegram", "token": TOKEN})
    assert status == 200
    try:
        status, paired = page.demo.api("/api/channel/pair", {"platform": "telegram"})
        assert status == 200
        _phone_says(page, paired["code"])
        _phone_says(page, "[ui:hello] 从手机上问一句")
        steps.open_nav(page)
        row = "[...document.querySelectorAll('#conversation-rows .conv .conv-meta')].find((meta) => /telegram/i.test(meta.textContent))"
        page.wait(row, timeout=30, what="会话列表里出现在 Telegram 里聊的那一段")
        page.reload()
        page.wait("document.body.classList.contains('ready')", what="刷新完")
        steps.open_nav(page)
        shown = page.wait(f"({row} || {{}}).textContent", what="刷新后的那一行")
        assert shown.startswith("Telegram · "), f"应该写平台的名字，实际是：{shown!r}"
    finally:
        page.demo.api("/api/channel/remove", {"platform": "telegram"})


def test_a_click_on_a_channel_is_not_lost_when_its_state_changes_between_press_and_release(page):
    # 通道的状态一变（连上了、收到消息……）就重画整页。GitHub 的 Windows 机器上真丢过一下：刚接上 Telegram 时状态接连变了几次，
    # 点"打开"的按下和松开之间正好重画了一次，按下的那个按钮已经不在了，浏览器不算这是一次点击，向导没打开
    from fake_discord import TOKEN as DISCORD

    assert page.demo.api("/api/channel/connect", {"platform": "telegram", "token": TOKEN})[0] == 200
    try:
        steps.extensions_tab(page, "channels")
        button = "#panel-channels li.chan-row[data-platform='telegram'] .chan-open"
        page.wait(f"document.querySelector({button!r}) && document.getElementById('count-channels').textContent === '1'", what="Telegram 连上了")
        steps.idle(page)
        x, y = page.reach(button)
        page.mouse(x, y)
        page.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 1})
        # 按着的时候再接上一个：状态变了（标签上的数字马上跟着变，整页的重画等松开）
        assert page.demo.api("/api/channel/connect", {"platform": "discord", "token": DISCORD})[0] == 200
        page.wait("document.getElementById('count-channels').textContent === '2'", timeout=30, what="状态到了：又连上一个")
        page.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "buttons": 0, "clickCount": 1})
        page.wait("document.querySelector('#panel-channels .chan-stepper')", what="这一下点击算数了：向导打开")
        steps.close_extensions(page)
    finally:
        for platform in ("telegram", "discord"):
            page.demo.api("/api/channel/remove", {"platform": platform})
