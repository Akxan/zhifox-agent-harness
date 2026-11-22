"""语音转文字在页面上的样子：设置里的一节、语音转出来的那条消息上的小标记（见 harness/voice/、page/scripts/voice.js）。

要守住的：
  - OpenAI 的每个模型后面写着每分钟多少钱，价格是助手程序在状态里给的（页面不自己存一份）；
  - 演示里没有 OpenAI 的密钥：写着"现在是关着的"，旁边的按钮把你带到填密钥的那一格；
  - 选了"你自己运行的兼容服务"：先亮出地址、模型名，填好按"保存"才算数；地址不合格时原因写在原地（不弹出来），
    保存好以后写着交给哪个地址转；
  - 语音转出来的那条消息上有一个"语音转文字"的小标记，写着语音有多长；重放旧会话时不播入场动画；
    编辑这条消息时，编辑框里只有原文（没有那个小标记的字）；
  - 三种语言、窄屏下都不露中文、不冲出去（另外在 test_ui_language.py、test_ui_layout.py 的大扫除里也查）。

演示没有真的语音转文字服务，也不需要：这里只改设置、看页面，从来不转任何东西。
"""

from __future__ import annotations

import demo_scripts
import pytest

from . import checks, steps
from .pilot import PHONE_WIDTH

HEARD = "明天早上八点提醒我开周会，顺便把上周的周报找出来"  # demo_web.py 里那段语音转出来的文字


def open_settings(page) -> None:
    steps.open_dialog(page, "#settings-open", "settings")
    page.wait("document.querySelectorAll('#voice-model option').length > 0", what="语音转文字那一节画好")


def test_models_show_their_price_and_a_missing_key_points_to_the_key(page):
    open_settings(page)
    assert page.js("document.getElementById('voice-provider').value") == "auto"
    options = page.js("[...document.querySelectorAll('#voice-model option')].map((o) => o.value + '|' + o.textContent)")
    assert options[0].startswith("gpt-transcribe|") and "0.0045" in options[0] and "（推荐）" in options[0]
    assert any(item.startswith("gpt-4o-mini-transcribe|") and "0.003" in item for item in options)
    assert page.js("document.getElementById('voice-status').classList.contains('bad')")
    assert "还没有填 OpenAI 的 API 密钥" in page.text("#voice-status")
    assert page.text("#voice-state") == "（关着）"
    assert "暂时只支持 Telegram" in page.text("#voice-cost") and "来源：语音转文字" in page.text("#voice-cost")
    page.click("#voice-status button")
    page.wait("document.activeElement.dataset.keep === 'key-openai'", what="焦点到了 OpenAI 的密钥那一格")
    assert checks.native_selects(page) == []  # 两个下拉框都是 select.field，箭头由页面自己画


def test_choosing_your_own_service_checks_the_address_in_place(page):
    page.allow_errors("/api/voice/settings")  # 不合格的地址：服务器回答 400（页面上的提示是这里要看的）
    open_settings(page)
    steps.voice_local(page)
    assert "填上地址" in page.text("#voice-status")
    assert page.js("document.getElementById('voice-model-row').hidden")  # 自己的服务用不着 OpenAI 的模型
    page.type("http://192.168.1.5:8000/v1")
    page.click("#voice-local-save")
    page.wait("document.getElementById('voice-local-note').classList.contains('bad')", what="地址不合格，原因写在原地")
    assert "只能连这台电脑自己" in page.text("#voice-local-note")
    assert page.js("document.getElementById('voice-url').getAttribute('aria-invalid')") == "true"
    assert not page.js("document.getElementById('settings-message').classList.contains('good')")
    # 改成这台电脑自己的地址，按回车保存
    page.focus("#voice-url")
    page.js("document.getElementById('voice-url').select()")
    page.type("http://127.0.0.1:8000/v1")
    page.wait("!document.getElementById('voice-local-note').classList.contains('bad')", what="一改原因就收起来")
    page.key("Enter")
    page.wait("document.getElementById('voice-status').textContent.includes('http://127.0.0.1:8000/v1')", what="保存好了，写着交给哪个地址")
    assert page.text("#voice-state") == "（用你自己运行的服务）"
    assert page.text("#settings-message") == "语音转文字改用你自己运行的服务了。"
    assert page.js("document.getElementById('voice-provider').value") == "local"
    # 关上再打开：还是它（存在设置里，不是只停在页面上）
    steps.escape_closes(page, "settings")
    open_settings(page)
    assert page.js("document.getElementById('voice-provider').value") == "local"
    assert page.js("document.getElementById('voice-url').value") == "http://127.0.0.1:8000/v1"


def test_a_local_custom_endpoint_can_be_picked_instead_of_an_address(page):
    # 演示里预先加好了一个本机的 Ollama（自定义模型接口）：选它就不用再填地址
    open_settings(page)
    steps.voice_local(page)
    assert not page.js("document.getElementById('voice-endpoint-row').hidden"), "有本机的自定义接口：可以从里面选"
    options = page.js("[...document.querySelectorAll('#voice-endpoint option')].map((o) => o.value)")
    assert options[0] == "" and any(value.startswith("custom-") for value in options[1:])
    endpoint = next(value for value in options[1:] if value.startswith("custom-"))
    steps.pick(page, "#voice-endpoint", endpoint)
    page.wait("document.getElementById('voice-url-row').hidden", what="选了接口，地址那一格收起来")
    page.click("#voice-local-save")
    page.wait("document.getElementById('voice-status').textContent.includes('自定义模型接口')", what="写着交给哪个接口")
    assert page.text("#voice-state") == "（用你自己运行的服务）"
    saved = steps.saved_json(page, "settings.json")["voice"]
    assert (saved["provider"], saved["endpoint"]) == ("local", endpoint)
    # 改回自己填地址：地址那一格又出来
    steps.pick(page, "#voice-endpoint", "")
    page.wait("!document.getElementById('voice-url-row').hidden")


def test_an_unsaved_choice_goes_back_when_the_settings_reopen(page):
    open_settings(page)
    steps.voice_local(page)
    steps.escape_closes(page, "settings")
    open_settings(page)
    assert page.js("document.getElementById('voice-provider').value") == "auto"
    assert page.js("document.getElementById('voice-local').hidden")


def test_a_transcribed_message_carries_a_voice_tag(page):
    status, reply = page.demo.api("/api/conversation/open", {"id": demo_scripts.VOICE_ID})
    assert status == 200, reply
    page.wait("document.querySelector('#log .msg.user .msg-voice')", what="打开 Telegram 里的那段会话")
    assert page.text("#log .msg.user .msg-voice-label") == "语音转文字"
    assert page.text("#log .msg.user .msg-voice-time") == "0:07"
    assert page.js("document.querySelector('#log .msg.user').dataset.text") == HEARD
    assert page.js("document.querySelectorAll('#log .enter').length") == 0  # 重放旧会话：不播入场动画
    steps.language(page, "en")
    assert page.text("#log .msg.user .msg-voice-label") == "Voice to text"  # 换语言：原地换掉
    # 编辑这条消息：编辑框里只有原文
    page.click("#input")
    page.key("ArrowUp")
    page.wait("document.activeElement.classList.contains('turn-editor-input')", what="编辑框打开")
    assert page.js("document.activeElement.value") == HEARD


@pytest.mark.parametrize("code", ["zh", "en", "es"])
def test_the_section_fits_at_phone_width_in_every_language(page, code):
    page.allow_errors("/api/voice/settings")
    page.size(PHONE_WIDTH, 844)
    steps.calm(page)
    if code != "zh":
        steps.language(page, code)
    open_settings(page)
    page.js("document.getElementById('voice-section').scrollIntoView()")
    assert checks.overflow(page, ["#voice-section"]) == []
    steps.voice_local(page)
    page.type("http://192.168.1.5:8000/v1")
    page.click("#voice-local-save")
    page.wait("document.getElementById('voice-local-note').classList.contains('bad')", what="地址不合格的原因")
    assert checks.overflow(page, ["#voice-section"]) == []
    if code != "zh":
        assert checks.untranslated(page, ["#voice-section"]) == []
