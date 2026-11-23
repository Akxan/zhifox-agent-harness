"""设置里"读网页"一节（page/scripts/fetch.js）：一个开关，默认开着，开、关都不用确认，改了马上存进设置文件。

怎么取、哪些地址不去、问不问你都在 harness/fetch/ 和 permissions.py（tests/test_fetch_*.py 测），这里只看页面：
开关和设置文件对得上、窗口里说一声、两套外观下文字看得清。整个设置窗口在三种语言、窄屏下的样子由 test_ui_language.py、
test_ui_layout.py 的"设置"那一项一起看。
"""

from __future__ import annotations

import json

import pytest

from . import steps
from .pilot import until
from .test_ui_page import CONTRAST


def _saved(page) -> dict:
    path = page.demo.root / "data" / "settings.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _settings(page) -> None:
    steps.open_dialog(page, "#settings-open", "settings")
    page.wait("!document.getElementById('fetch-section').hidden && document.getElementById('fetch-state').textContent.length > 0", what="设置里的读网页一节画好")
    page.reach("#fetch-enabled")


def _checked(page) -> str:
    return page.js("document.getElementById('fetch-enabled').getAttribute('aria-checked')")


def test_on_by_default_and_switching_is_saved(page):
    _settings(page)
    assert _checked(page) == "true" and page.text("#fetch-state") == "（开着）"
    page.click("#fetch-enabled")
    until(lambda: _saved(page).get("web_fetch") is False, what="关掉以后存下来")
    page.wait("document.getElementById('fetch-enabled').getAttribute('aria-checked') === 'false'")
    # 窗口里说一声（这段对话还没开始是 good；演示页面上已经聊起来的话，说"从下一段新对话开始生效"）
    page.wait("/good|bad/.test(document.getElementById('settings-message').className)", what="窗口里说一声")
    assert page.text("#fetch-state") == "（关着）"
    page.click("#fetch-enabled")
    until(lambda: "web_fetch" not in _saved(page), what="打开以后存下来（默认的就不写进文件）")
    page.wait("document.getElementById('fetch-enabled').getAttribute('aria-checked') === 'true'")
    assert page.text("#fetch-state") == "（开着）"
    assert page.js(steps.is_open("settings")), "开关不关上设置窗口"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_contrast_in_both_themes(page, theme):
    page.js(f"localStorage.setItem('harness.theme', {theme!r})")
    page.reload()
    page.wait(f"document.body.classList.contains('ready') && document.documentElement.dataset.theme === {theme!r}")
    steps.calm(page)
    _settings(page)
    steps.idle(page)
    selectors = ["#fetch-section h3", "#fetch-title", "#fetch-section .browser-note", "#fetch-section .hint"]
    for selector, ratio in page.js(f"({CONTRAST})({selectors!r})"):
        assert ratio is None or ratio >= 4.5, f"{theme} 下 {selector} 的对比度只有 {ratio}"
