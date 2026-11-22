"""设置里的"自定义模型接口"：列表、添加和编辑的窗口（page/scripts/endpoints.js、endpoints-editor.js），以及换模型的面板里的那几组。

演示里预先加好了一个假的"本机 Ollama"（tests/demo_web.py，也是 tests/fake_openai.py）：qwen3:8b 能用工具、llava:13b 看得懂图片、
gemma2:2b 用不了工具，nomic-embed-text 是向量模型（同步时会被去掉）。"测试连接"真的从演示服务器发请求过去（页面自己不连）。
加的接口测试完都删掉，不留给后面的测试。
"""

from __future__ import annotations

import pytest

from . import checks, steps
from .pilot import PHONE_WIDTH
from .test_ui_page import CONTRAST

ROW = "#endpoints-list > li.endpoint-row"
SEEDED = ROW + "[data-id='custom-ollama']"


def _settings(page) -> None:
    steps.open_dialog(page, "#settings-open", "settings")
    page.wait(f"document.querySelector({SEEDED!r})", what="预先加好的 Ollama 那一行")
    page.js("document.getElementById('endpoints-section').scrollIntoView({ block: 'start' })")
    steps.idle(page)


def _editor(page, row: str = SEEDED) -> None:
    """打开一个接口的编辑窗口（点这一行）。"""
    page.click(f"{row} .endpoint-open")
    page.wait(steps.is_open("endpoint-dialog"), what="编辑窗口打开")
    steps.idle(page)


def _fill(page, selector: str, text: str) -> None:
    page.focus(selector)
    page.js(f"document.querySelector({selector!r}).select()")
    page.type(text)


def _seeded_url(page) -> str:
    return page.text(f"{SEEDED} .endpoint-url")


@pytest.fixture
def cleanup(page):
    """测试里加的接口测试完删掉（先换回 Claude：正在用的接口删不掉）。"""
    yield page
    page.demo.api("/api/stop")
    page.demo.api("/api/model", {"provider": "anthropic", "model": "claude-opus-5-5"})
    for group in page.demo.api("/api/model/catalog")[1]["catalog"]["groups"]:
        if group.get("custom") and group["id"] != "custom-ollama":
            page.demo.api("/api/endpoint/delete", {"id": group["id"]})


def test_add_an_endpoint_test_the_connection_and_switch_to_it(cleanup):
    page = cleanup
    _settings(page)
    url = _seeded_url(page)
    page.click("#endpoints-add")
    page.wait(steps.is_open("endpoint-dialog"), what="添加的窗口打开")
    steps.idle(page)
    assert page.text("#endpoint-title") == "添加模型接口"
    assert page.js("document.activeElement.id") == "endpoint-name"
    # "常用的几种"：点一下填好名字和地址
    page.click("#endpoint-presets .endpoint-preset:nth-child(2)")
    assert page.js("document.getElementById('endpoint-url').value") == "http://localhost:1234/v1"
    assert page.js("document.getElementById('endpoint-name').value") == "LM Studio"
    _fill(page, "#endpoint-name", "Office box")
    _fill(page, "#endpoint-url", url)
    # 测试连接：演示服务器替页面去问，认出来是 Ollama，模型列表填进来（向量模型去掉了）
    page.click("#endpoint-test")
    page.wait("document.getElementById('endpoint-result').classList.contains('good')", timeout=15, what="连上了")
    result = page.text("#endpoint-result")
    assert "找到 3 个模型" in result and "认出来是 Ollama" in result and "对话不出这台电脑" in result
    names = page.js("[...document.querySelectorAll('#endpoint-models .endpoint-model-name')].map((node) => node.textContent)")
    assert names == ["qwen3:8b", "llava:13b", "gemma2:2b"]
    # 每个模型能做什么是猜出来的（服务器自己说的），点一下就能改
    caps = "#endpoint-models [data-model='llava:13b'] .endpoint-cap"
    assert page.js(f"[...document.querySelectorAll({caps!r})].map((node) => node.getAttribute('aria-pressed'))") == ["true", "false", "false"]
    page.click(f"{caps}:nth-child(2)")
    assert page.js(f"document.querySelectorAll({caps!r})[1].getAttribute('aria-pressed')") == "true"
    assert page.js("document.activeElement.dataset.focus") == "tools:llava:13b"  # 重画以后焦点还在那个按钮上
    page.click("#endpoint-save")
    page.wait(f"!{steps.is_open('endpoint-dialog')} && document.querySelector('{ROW}[data-id=\"custom-office-box\"]')", timeout=10, what="保存好、列表里多了一行")
    assert page.js(steps.is_open("settings")), "关上编辑窗口以后还在设置里"
    assert "已保存接口“Office box”" in page.text("#settings-message")
    new_row = f"{ROW}[data-id='custom-office-box']"
    assert page.text(f"{new_row} .endpoint-name") == "Office box"
    assert "3 个模型" in page.text(f"{new_row} .endpoint-chips")
    steps.escape_closes(page, "settings")

    # 换模型的面板里：它是单独的一组，写着在这台电脑上，模型带着标签，不花钱的写"本地，不计费"
    steps.open_models(page)
    group = "document.getElementById('models-g-custom-office-box').closest('.models-group')"
    page.wait(f"{group}", what="换模型的面板里有这一组")
    assert page.js(f"{group}.querySelector('.models-where').textContent") == "这台电脑"
    llava = f"{group}.querySelector('[data-model=\"llava:13b\"]')"
    assert page.js(f"[...{llava}.querySelectorAll('.models-tags span')].map((node) => node.textContent)") == ["看得懂图片", "能用工具", "本地"]
    assert page.js(f"{llava}.querySelector('.models-price').textContent") == "本地，不计费"
    page.js(f"{llava}.click()")
    page.wait("document.getElementById('models-status').classList.contains('good')", timeout=10, what="换过去了")
    assert "llava:13b" in page.text("#model-label")
    steps.escape_closes_flyout(page, "models", "#model")
    # 聊一句：回答来自假的本机 Ollama
    steps.send(page, "你好")
    steps.reply(page, "对话不出你的电脑")


def test_a_plain_http_address_on_the_internet_is_refused_in_the_window(page):
    page.allow_errors("/api/endpoint/probe", "/api/endpoint/save")  # 这两个请求就是要被拒绝（409）的
    _settings(page)
    page.click("#endpoints-add")
    page.wait(steps.is_open("endpoint-dialog"))
    _fill(page, "#endpoint-name", "Gateway")
    _fill(page, "#endpoint-url", "http://llm.example.com/v1")
    page.click("#endpoint-test")
    page.wait("document.getElementById('endpoint-result').classList.contains('bad')", what="说明为什么不行")
    assert "https://" in page.text("#endpoint-result")
    page.click("#endpoint-save")
    page.wait("document.getElementById('endpoint-message').classList.contains('bad')", what="保存也不行，原因写在窗口里")
    assert "https://" in page.text("#endpoint-message")
    assert page.js(steps.is_open("endpoint-dialog"))


def test_the_editor_asks_before_throwing_away_changes(page):
    _settings(page)
    _editor(page)
    assert page.text("#endpoint-title") == "编辑“Ollama”"
    assert "custom-ollama/模型名" in page.text("#endpoint-id")
    # 没改过：Esc 直接关上，回到设置
    steps.escape_closes(page, "endpoint-dialog")
    assert page.js(steps.is_open("settings"))
    _editor(page)
    page.focus("#endpoint-name")
    page.type(" 2")
    for close in ("key", "#close-endpoint", "#endpoint-cancel"):
        if close == "key":
            page.key("Escape")
        else:
            page.click(close)
        page.wait("document.getElementById('endpoint-message').textContent.includes('还有没保存的修改')", what=f"{close} 先提醒")
        assert page.js(steps.is_open("endpoint-dialog"))
        page.focus("#endpoint-name")
        page.type("x")  # 接着改：提醒收起来，下一次关又要先提醒
        page.wait("!document.getElementById('endpoint-message').textContent")
    page.key("Escape")
    page.key("Escape")
    page.wait(f"!{steps.is_open('endpoint-dialog')}", what="第二次才放弃")
    assert page.js(steps.is_open("settings"))
    # 去掉一个模型、焦点在被去掉的按钮上：Esc 照样先提醒（cancel 事件也接着）
    _editor(page)
    page.click("#endpoint-models [data-model='gemma2:2b'] .endpoint-model-top button")
    page.wait("!document.querySelector(\"#endpoint-models [data-model='gemma2:2b']\")")
    page.key("Escape")
    page.wait("document.getElementById('endpoint-message').textContent.includes('还有没保存的修改')")
    page.key("Escape")
    page.wait(f"!{steps.is_open('endpoint-dialog')}")
    assert page.demo.api("/api/model/catalog")[1]["catalog"]  # 放弃了：服务器上什么都没改
    seeded = next(group for group in page.demo.api("/api/model/catalog")[1]["catalog"]["groups"] if group["id"] == "custom-ollama")
    assert [model["id"] for model in seeded["models"]] == ["qwen3:8b", "llava:13b", "gemma2:2b"]


@pytest.mark.parametrize("code", ["en", "es"])
def test_translated_narrow_and_dark(page, code):
    steps.language(page, code)
    page.size(PHONE_WIDTH, 800)
    page.click("#theme")
    page.click("[data-theme-pick='dark']")
    page.wait("document.documentElement.dataset.theme === 'dark'")
    page.key("Escape")
    steps.calm(page)
    _settings(page)
    assert checks.untranslated(page, ["#endpoints-section"]) == []
    assert checks.overflow(page, ["#endpoints-section"]) == []
    _editor(page)
    page.click("#endpoint-test")
    page.wait("document.getElementById('endpoint-result').classList.contains('good')", timeout=15)
    steps.idle(page)
    assert checks.untranslated(page, ["#endpoint-dialog"]) == []
    assert checks.overflow(page, ["#endpoint-dialog"]) == []
    selectors = ["#endpoint-title", ".endpoint-label", ".endpoint-cap", ".endpoint-number span", "#endpoint-result", ".endpoint-preset small", "#endpoint-defaults"]
    for selector, ratio in page.js(f"({CONTRAST})({selectors!r})"):
        assert ratio is None or ratio >= 4.5, f"深色下 {selector} 的对比度只有 {ratio}"
    steps.escape_closes(page, "endpoint-dialog")
    steps.escape_closes(page, "settings")
    steps.open_models(page)
    assert checks.untranslated(page, ["#models"]) == []
    assert checks.overflow(page, ["#models"]) == []
