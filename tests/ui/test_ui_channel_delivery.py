"""消息发不到聊天里的时候，页面上看得到（harness/channels/outbox.py、page/scripts/channels-list.js 的 chanDelivery）：

  - "扩展 · 通道"里 Telegram 那一行写着几条没能发出去、最后一次失败的时间和原因
  - 聊天里等你批准、却没发到聊天里的那张确认请求上写着"没能发到聊天里……请在这里答复"，主区域顶上那一条也说一声

演示里的假 Telegram 用 /control/trouble 让接下来两次发消息都回答 403（你把机器人停用了）：一直的失败，不再重试，
马上就是这个样子（暂时的失败要先重试十几秒，"还没发到"的样子由 tests/test_channels_delivery.py 在程序这一侧测）。
三种语言都看：没翻译的中文、390 像素宽时文字冲出去；浅色、深色都量对比度。
"""

from __future__ import annotations

import json
import time
import urllib.request

from fake_telegram import TOKEN

from . import checks, steps
from .pilot import PHONE_WIDTH

BLOCKED = {"error_code": 403, "description": "Forbidden: bot was blocked by the user"}
ROW = "#panel-channels li.chan-row[data-platform='telegram']"
# 会话栏里在 Telegram 里聊的有几段（一轮结束、存好盘，这一段才出现在这里）
TELEGRAM_ROWS = "[...document.querySelectorAll('#conversation-rows .conv .conv-meta')].filter((meta) => /telegram/i.test(meta.textContent)).length"


def _phone(page, action: str, body: dict) -> dict:
    """对演示里的假 Telegram 做一件事（say：主人发一条消息；trouble：接下来几次出错；calm：不再出错）。"""
    deadline = time.monotonic() + 10
    while not page.demo.telegram and time.monotonic() < deadline:
        time.sleep(0.05)  # 演示的输出里，假 Telegram 的地址在页面地址之后才打印
    assert page.demo.telegram, "没读到假 Telegram 的地址"
    request = urllib.request.Request(f"{page.demo.telegram}/control/{action}", data=json.dumps(body).encode(), method="POST")
    with urllib.request.urlopen(request, timeout=10) as answer:
        return json.loads(answer.read() or b"{}")


def _phone_log(page) -> list[str]:
    with urllib.request.urlopen(f"{page.demo.telegram}/control/log", timeout=10) as answer:
        return [message["text"] for message in json.loads(answer.read())["messages"]]


def _contrast(page, theme: str) -> None:
    for selector, ratio in checks.contrast(page, [f"{ROW} .chan-delivery", "#panel-channels .chan-ask-note"]):
        assert ratio is not None and ratio >= 4.5, f"{theme}下 {selector} 的对比度只有 {ratio}"


def _theme(page, theme: str) -> None:
    page.click("#theme")
    page.wait("document.getElementById('appearance').matches(':popover-open')", what="外观面板打开")
    page.click(f"[data-theme-pick='{theme}']")
    page.wait(f"document.documentElement.dataset.theme === '{theme}'", what=f"换成{theme}")
    page.key("Escape")
    steps.idle(page)


def test_messages_that_could_not_reach_the_chat_are_shown(page):
    steps.calm(page)
    assert page.demo.api("/api/channel/connect", {"platform": "telegram", "token": TOKEN})[0] == 200
    try:
        status, paired = page.demo.api("/api/channel/pair", {"platform": "telegram"})
        assert status == 200
        # 演示服务器是几个测试共用的：假 Telegram 里、会话栏里可能还有前面的测试留下的，只看这以后多出来的
        before = len(_phone_log(page))
        steps.open_nav(page)
        chats = page.js(TELEGRAM_ROWS)
        _phone(page, "say", {"text": paired["code"]})
        _phone(page, "say", {"text": "你好"})
        deadline = time.monotonic() + 30
        while not any("我是智狐" in said for said in _phone_log(page)[before:]):  # 演示的通道里单数轮打招呼
            assert time.monotonic() < deadline, "等不到打招呼的回答"
            time.sleep(0.1)
        # 还要等这一轮整个结束（存好盘，会话栏里多出这一段）：不然下一句会排队，机器人先回一句"前面还有 1 件事"，
        # 用掉一次下面安排的失败（机器忙的时候真的这样过）
        page.wait(f"{TELEGRAM_ROWS} > {chats}", timeout=30, what="打招呼的那一轮结束、存好盘")
        # 第二轮先说一句、再要执行一条命令（要你批准）：这两条都发不出去（403：不再重试）
        _phone(page, "trouble", {"method": "sendMessage", "failures": [BLOCKED, BLOCKED]})
        _phone(page, "say", {"text": "看看磁盘还剩多少"})

        steps.extensions_tab(page, "channels")
        page.wait(f"(document.querySelector({json.dumps(ROW + ' .chan-delivery')}) || {{}}).textContent?.includes('没能发到聊天里的消息：2 条')", timeout=30, what="那一行写着有两条没能发出去")
        delivery = page.text(f"{ROW} .chan-delivery")
        assert "最后一次失败：" in delivery and "停用" in delivery, delivery
        note = page.wait("(document.querySelector('#panel-channels .chan-ask-note') || {}).textContent", what="确认请求上写着没发到聊天里")
        assert note.startswith("没能发到聊天里（Telegram 拒绝了") and note.endswith("请在这里答复。"), note
        assert "没能发到聊天里" in page.text("#chan-alert-text")  # 主区域顶上那一条也说
        _contrast(page, "浅色")

        for code in ("en", "es"):
            steps.close_extensions(page)
            steps.language(page, code)  # 换语言：就地重译，助手程序也按新的语言重写原因
            page.size(PHONE_WIDTH, 844)
            steps.extensions_tab(page, "channels")
            page.wait(f"document.querySelector({json.dumps(ROW + ' .chan-delivery')}) && !/[\\u4e00-\\u9fff]/.test(document.querySelector('#panel-channels .chan-ask-note').textContent)", what=f"{code} 的样子")
            # 确认请求上那段"要做的事"（"执行命令：$ df -h …"）是 run_command 拼好的一段普通文字，页面原样显示：
            # 它前面的"执行命令："换语言时不跟着变，是这个功能以前就有的样子，和这里查的无关，先跳过
            problems = [item for item in checks.untranslated(page, ["#extensions", "#chan-alert"]) if "执行命令" not in item]
            assert problems == [], (code, problems)
            assert checks.overflow(page, ["#extensions"]) == [], code
        steps.close_extensions(page)
        _theme(page, "dark")
        steps.extensions_tab(page, "channels")
        _contrast(page, "深色")
    finally:
        page.js("localStorage.removeItem('harness.theme')")  # 外观记在浏览器里：不把深色留给下一个测试（语言由 demo.reset 换回去）
        _phone(page, "calm", {})
        page.demo.api("/api/channel/remove", {"platform": "telegram"})
