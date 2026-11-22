"""设置里"遮住密钥"一节（page/scripts/redact.js）：一个开关，默认开着，关掉之前在原地问一次，打开不用问。

认哪些、怎么遮都在 harness/redact.py（tests/test_redact.py 测），这里只看页面：开关和设置文件对得上、
问的那一块拿着焦点时 Esc 只收起它（不关掉设置窗口）、两套外观下文字看得清。
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
    page.wait("!document.getElementById('redact-section').hidden && document.getElementById('redact-state').textContent.length > 0", what="设置里的遮住密钥一节画好")
    page.reach("#redact-enabled")


def _checked(page) -> str:
    return page.js("document.getElementById('redact-enabled').getAttribute('aria-checked')")


def test_on_by_default_and_turning_off_asks_first(page):
    _settings(page)
    assert _checked(page) == "true"
    assert page.js("document.getElementById('redact-confirm').hidden")
    page.click("#redact-enabled")
    page.wait("!document.getElementById('redact-confirm').hidden", what="关掉之前先问")
    assert page.js("document.activeElement.id") == "redact-confirm-no", "焦点放在'保持开着'上：随手按回车不会关掉"
    assert _checked(page) == "true" and "redact_secrets" not in _saved(page), "问的时候还什么都没改"
    # 保持开着：收起来，焦点回到开关上
    page.click("#redact-confirm-no")
    page.wait("document.getElementById('redact-confirm').hidden")
    assert page.js("document.activeElement.id") == "redact-enabled"
    # 再问一次，这回按 Esc：只收起那一块，设置窗口还开着
    page.click("#redact-enabled")
    page.wait("!document.getElementById('redact-confirm').hidden")
    page.key("Escape")
    page.wait("document.getElementById('redact-confirm').hidden")
    assert page.js(steps.is_open("settings")), "Esc 只收起问的那一块"
    assert page.js("document.activeElement.id") == "redact-enabled"
    assert "redact_secrets" not in _saved(page)


def test_turning_off_and_on_is_saved(page):
    _settings(page)
    page.click("#redact-enabled")
    page.wait("!document.getElementById('redact-confirm').hidden")
    page.click("#redact-confirm-yes")
    until(lambda: _saved(page).get("redact_secrets") is False, what="关掉以后存下来")
    page.wait("document.getElementById('redact-enabled').getAttribute('aria-checked') === 'false' && document.getElementById('redact-confirm').hidden")
    page.wait("document.getElementById('settings-message').classList.contains('good')", what="窗口里说一声")
    assert page.text("#redact-state") == "（关着）"
    # 打开不用问
    page.click("#redact-enabled")
    until(lambda: "redact_secrets" not in _saved(page), what="打开以后存下来（默认的就不写进文件）")
    page.wait("document.getElementById('redact-enabled').getAttribute('aria-checked') === 'true'")
    assert page.js("document.getElementById('redact-confirm').hidden"), "打开不用问"
    assert page.text("#redact-state") == "（开着）"


def test_closing_settings_drops_the_question(page):
    _settings(page)
    page.click("#redact-enabled")
    page.wait("!document.getElementById('redact-confirm').hidden")
    page.click("#close-settings")
    page.wait(f"!{steps.is_open('settings')}")
    _settings(page)
    assert page.js("document.getElementById('redact-confirm').hidden"), "下次打开时不再挂着"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_contrast_in_both_themes(page, theme):
    page.js(f"localStorage.setItem('harness.theme', {theme!r})")
    page.reload()
    page.wait(f"document.body.classList.contains('ready') && document.documentElement.dataset.theme === {theme!r}")
    steps.calm(page)
    _settings(page)
    page.click("#redact-enabled")
    page.wait("!document.getElementById('redact-confirm').hidden")
    steps.idle(page)
    selectors = ["#redact-section h3", "#redact-title", "#redact-section .browser-note", "#redact-confirm-title span", "#redact-section .hint"]
    for selector, ratio in page.js(f"({CONTRAST})({selectors!r})"):
        assert ratio is None or ratio >= 4.5, f"{theme} 下 {selector} 的对比度只有 {ratio}"
    page.click("#redact-confirm-no")
