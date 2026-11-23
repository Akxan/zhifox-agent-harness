"""设置里的"检查新版本"（page/scripts/updates.js）和齿轮上的小点。

规矩和升级的命令在 harness/updates.py（tests/test_updates.py、test_updates_web.py 测），这里只看页面：默认关着、什么都没有；
打开以后（演示问的是假的 GitHub，它说有一个更新的版本）小点、发布说明的链接、怎么升级的命令都画出来，开关和设置文件对得上；
关上就都没了；刷新以后小点还在；换语言就地重译；两套外观下文字看得清。
"""

from __future__ import annotations

import pytest

from . import checks, steps
from .pilot import until
from .test_ui_page import CONTRAST

DOT_SHOWN = "!document.getElementById('settings-news').hidden"


def _settings(page) -> None:
    steps.open_dialog(page, "#settings-open", "settings")
    page.wait("!document.getElementById('updates-section').hidden && document.getElementById('updates-state').textContent.length > 0", what="检查新版本那一节画好")
    page.reach("#updates-enabled")


def _saved(page) -> dict:
    path = page.demo.root / "data" / "settings.json"
    return steps.saved_json(page, "settings.json") if path.exists() else {}


def test_off_by_default(page):
    assert page.js(f"!({DOT_SHOWN})"), "关着的时候齿轮上没有小点"
    _settings(page)
    assert page.js("document.getElementById('updates-enabled').getAttribute('aria-checked')") == "false"
    assert page.js("document.getElementById('updates-card').hidden")
    assert page.text("#updates-state") == "（关着）"
    assert page.text("#updates-current").startswith("现在用的是 ")
    assert "update_check" not in _saved(page)


def test_turning_on_shows_the_new_version_and_turning_off_hides_it(page):
    _settings(page)
    page.click("#updates-enabled")
    until(lambda: _saved(page).get("update_check") is True, what="打开以后存下来")
    page.wait("document.querySelector('#updates-card.news .service-command')", what="假的 GitHub 说有新版本")
    page.wait(DOT_SHOWN, what="齿轮上出现小点")
    page.wait("document.getElementById('settings-message').classList.contains('good')", what="窗口里说一声")
    assert page.text("#updates-state") == "（开着）"
    head = page.text("#updates-card .updates-row.news b")
    assert head.startswith("有新版本：智狐 ")
    link = page.js("document.querySelector('#updates-card .updates-link').href")
    assert link.startswith("https://github.com/Akxan/zhifox-agent-harness/releases/tag/v")
    assert page.js("document.querySelector('#updates-card .updates-link').rel") == "noopener noreferrer"
    # 演示是从仓库里运行的：在智狐的文件夹里 git pull、uv sync，一条一行，各有一个"复制命令"
    assert page.js("[...document.querySelectorAll('#updates-card .service-command code')].map((code) => code.textContent)") == ["git pull", "uv sync"]
    assert "有新版本" in page.js("document.getElementById('settings-open').title")

    page.click("#updates-enabled")
    until(lambda: "update_check" not in _saved(page), what="关上以后存下来（默认的就不写进文件）")
    page.wait("document.getElementById('updates-card').hidden && document.getElementById('settings-news').hidden", what="关上以后都没了")
    assert "有新版本" not in page.js("document.getElementById('settings-open').title")


def test_check_now_keeps_focus_and_the_dot_survives_a_reload(page):
    _settings(page)
    steps.updates_news(page)
    page.click("#updates-card .updates-check")
    # 问的时候按钮变成"正在检查……"、问完了变回来：整块重画过，焦点还在这个按钮上（不会掉到 body 上，Esc 才不会直接关掉窗口）
    page.wait("document.activeElement && document.activeElement.classList.contains('updates-check') && document.activeElement.getAttribute('aria-disabled') === 'false'", what="问完了，焦点还在按钮上")
    steps.escape_closes(page, "settings")
    assert page.js(DOT_SHOWN)
    page.reload()
    page.wait("document.body.classList.contains('ready')")
    page.wait(DOT_SHOWN, what="刷新以后小点还在")


def test_switching_language_retranslates_in_place(page):
    _settings(page)
    steps.updates_news(page)
    # 设置窗口是模态的，盖住了顶栏的地球按钮：像在另一个标签页里换了语言那样，请助手程序换，这一页收到状态就地换过去
    for code, lang, expected in (("en", "en", "New version: 智狐 "), ("es", "es", "Versión nueva: 智狐 "), ("zh", "zh-CN", "有新版本：智狐 ")):
        assert page.demo.api("/api/language", {"language": code})[0] == 200
        page.wait(f"document.documentElement.lang === {lang!r}", what=f"换成 {code}")
        page.wait(f"document.querySelector('#updates-card .updates-row.news b').textContent.startsWith({expected!r})", what=f"{code} 的标题")
        if code != "zh":
            assert checks.untranslated(page, ["#updates-section"]) == []
        assert page.js("document.getElementById('settings').open"), "换语言时设置窗口还开着"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_contrast_in_both_themes(page, theme):
    page.js(f"localStorage.setItem('harness.theme', {theme!r})")
    page.reload()
    page.wait(f"document.body.classList.contains('ready') && document.documentElement.dataset.theme === {theme!r}")
    steps.calm(page)
    _settings(page)
    steps.updates_news(page)
    selectors = [
        "#updates-section h3", "#updates-title", "#updates-note", "#updates-current", "#updates-card .updates-words b",
        "#updates-card .updates-words p", "#updates-card .updates-link span", "#updates-card .updates-steps h4",
        "#updates-card .updates-lead", "#updates-card .updates-note", "#updates-card .service-command code", "#updates-card .updates-when",
    ]
    for selector, ratio in page.js(f"({CONTRAST})({selectors!r})"):
        assert ratio is None or ratio >= 4.5, f"{theme} 下 {selector} 的对比度只有 {ratio}"
