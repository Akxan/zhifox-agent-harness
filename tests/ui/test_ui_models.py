"""换模型的面板：顶栏中间的模型按钮弹出来的那一个（page/scripts/models.js，目录由 harness/model_catalog.py 算）。

用户的原话："现在的设置和模型切换的入口是一样的，能否分开？"所以模型按钮打开的是这个面板，不是设置（设置在齿轮里）。
这里查：面板里写着什么（按家分组、有没有密钥、能做什么、价格、打勾的是哪一个）；同一家马上换、对话接着进行；
换到另一家而这段聊过了，先在面板里问一声；没有密钥的那一家直接带你去设置里填；键盘（方向键、回车、打字就搜、
Esc、Tab 不出去、快捷键）；换语言时就地重译；窄屏上从底下升起来，点暗底只收起面板，不会点到下面。

演示里只有 Claude 有（编造的）密钥。要 DeepSeek 的测试自己存一个编造的（存在演示服务器的临时目录里，换到那一家的
回答来自本地的假服务器）；GPT 一直没有密钥，用来查"没有密钥"的样子。每个测试开始前，conftest 把模型换回 claude-opus-5-5。
"""

from __future__ import annotations

from . import checks, steps
from .pilot import PHONE_WIDTH

CURRENT = "document.querySelector('#models-menu [aria-checked=true]').dataset.model"
ROW = "#models-menu [data-model='{model}']"


def _mac(page) -> bool:
    return page.js("/Mac|iPhone|iPad/.test(navigator.platform)")


def _tags(page, model: str) -> list[str]:
    """一个模型的标签（中间的点是样式画的，不在文字里）。"""
    return page.js(f"[...document.querySelectorAll({ROW.format(model=model) + ' .models-tags span'!r})].map((node) => node.textContent)")


def _deepseek_key(page) -> None:
    status, reply = page.demo.api("/api/key", {"provider": "deepseek", "key": "fake-deepseek-key-0002"})
    assert status == 200, reply


def test_the_model_button_opens_the_switcher_with_everything_on_it(page):
    page.click("#model")
    page.wait(steps.flyout_open("models"), what="换模型的面板弹出来")
    page.wait("document.querySelector('#models-menu [role=menuitemradio]')", what="模型列出来")
    steps.idle(page)
    assert not page.js(steps.is_open("settings")), "模型按钮打开的应该是换模型的面板，不是设置"
    assert page.js("document.getElementById('model').getAttribute('aria-expanded')") == "true"
    # 焦点在现在用的那个模型上：键盘打开的，直接就能用方向键选
    assert page.js("document.activeElement.dataset.model") == "claude-opus-5-5"
    assert page.js(CURRENT) == "claude-opus-5-5"
    # 按家分组，顺序和程序里的一样；每一家写着有没有密钥
    labels = page.js("[...document.querySelectorAll('#models-menu .models-label')].map((node) => node.textContent)")
    # 自定义模型接口排在内置的几家后面（演示里预先加好了一个假的本机 Ollama，见 test_ui_endpoints.py）
    assert labels == ["Claude (Anthropic)", "GPT (OpenAI)", "Gemini (Google)", "DeepSeek", "Ollama"]
    assert page.js("document.querySelector('#models-g-anthropic').parentElement.querySelector('.models-key.set') !== null")
    # 每个模型：能做什么、价格（程序给的）
    opus = ROW.format(model="claude-opus-5-5")
    assert _tags(page, "claude-opus-5-5") == ["能联网", "看得懂图片", "能读 PDF"]
    assert page.text(f"{opus} .models-price") == "$4/$20"
    assert "美元 / 每百万 token" in page.js(f"document.querySelector({opus + ' .models-price'!r}).title")
    # 没有密钥的那一家：一个"填写 API 密钥"，没有模型
    gpt = "document.getElementById('models-g-openai').closest('.models-group')"
    assert page.js(f"{gpt}.querySelector('.models-keylink') !== null && {gpt}.querySelectorAll('[role=menuitemradio]').length === 0")
    assert page.js(f"{gpt}.querySelector('.models-key').textContent") == "未设置"
    steps.escape_closes_flyout(page, "models", "#model")
    assert page.js("document.getElementById('model').getAttribute('aria-expanded')") == "false"


def test_a_model_of_the_same_provider_is_switched_at_once_and_the_conversation_goes_on(page):
    steps.send(page, "[ui:hello] 先聊一句")
    steps.reply(page, "收到第 1 条")
    before = page.js("document.querySelectorAll('#log > *').length")
    steps.open_models(page)
    page.click(ROW.format(model="claude-sonnet-5-5"))
    # 同一家：不用问，马上换；结果写在面板里（浮在页面上的提示会被面板盖住）
    page.wait("document.getElementById('models-status').classList.contains('good')", what="换好了")
    assert page.js("document.getElementById('models-confirm').hidden")
    assert page.text("#models-status") == "已换成 claude-sonnet-5-5，对话接着进行。"
    page.wait(f"{CURRENT} === 'claude-sonnet-5-5'", what="勾挪到新的那个上")
    assert "claude-sonnet-5-5" in page.text("#model-label")
    assert page.js(steps.flyout_open("models")), "换完以后面板还开着，看得到结果"
    # 对话还在：一条都没少（程序在对话里另外记一句"已换成……"）
    assert page.js("document.querySelectorAll('#log .msg.user').length") == 1
    assert page.js("document.querySelectorAll('#log > *').length") >= before
    steps.escape_closes_flyout(page, "models", "#model")


def test_another_provider_asks_first_when_the_conversation_has_content(page):
    _deepseek_key(page)
    steps.send(page, "[ui:hello] 这段要留着")
    steps.reply(page, "收到第 1 条")
    steps.open_models(page)
    deepseek = ROW.format(model="deepseek-flash")
    page.click(deepseek)
    # 先在面板里问一声，"换过去"拿着焦点；还没换
    page.wait("!document.getElementById('models-confirm').hidden", what="问要不要开始新对话")
    assert page.text("#models-confirm-text") == "换到 DeepSeek 会开始一段新对话。现在这段已经保存，随时可以回来接着聊。"
    assert page.js("document.activeElement.id") == "models-confirm-yes"
    assert page.js(f"document.querySelector({deepseek!r}).classList.contains('models-asking')")
    assert "claude-opus-5-5" in page.text("#model-label")
    # Esc 先收起这一问（面板还开着），焦点回到那一行
    page.key("Escape")
    page.wait("document.getElementById('models-confirm').hidden", what="收起这一问")
    assert page.js(steps.flyout_open("models"))
    assert page.js("document.activeElement.dataset.model") == "deepseek-flash"
    assert page.js("document.querySelectorAll('#log .msg.user').length") == 1
    # 再点一次、确认：换过去，开始一段新对话；原来那段还在会话列表里
    page.click(deepseek)
    page.wait("!document.getElementById('models-confirm').hidden")
    page.click("#models-confirm-yes")
    page.wait("document.getElementById('model-label').textContent.includes('deepseek-flash')", what="换到 DeepSeek")
    page.wait("document.querySelectorAll('#log .msg.user').length === 0", what="新的一段是空的")
    page.wait("document.getElementById('models-status').classList.contains('good')")
    assert page.text("#models-status") == "已换成 DeepSeek · deepseek-flash，开始了一段新对话。"
    page.wait(f"{CURRENT} === 'deepseek-flash'")
    steps.escape_closes_flyout(page, "models", "#model")
    steps.open_nav(page)
    assert page.js("[...document.querySelectorAll('#conversation-rows .conv-title')].some((node) => node.textContent.includes('这段要留着'))")


def test_a_fresh_conversation_switches_provider_without_asking(page):
    _deepseek_key(page)
    steps.open_models(page)
    page.click(ROW.format(model="deepseek-v4-pro"))
    page.wait("document.getElementById('model-label').textContent.includes('deepseek-v4-pro')", what="直接换过去")
    assert page.js("document.getElementById('models-confirm').hidden"), "这段还没聊，没什么会丢的，不用问"
    # DeepSeek 看不懂图片：标签里写"只认文字"，还有分时段计价（悬停的说明里写着闲时价）
    row = ROW.format(model="deepseek-v4-pro")
    page.wait(f"document.querySelector({row!r}).getAttribute('aria-checked') === 'true'")
    assert _tags(page, "deepseek-v4-pro") == ["只认文字", "分时段计价"]
    assert "闲时" in page.js(f"document.querySelector({row + ' .models-price'!r}).title")
    steps.escape_closes_flyout(page, "models", "#model")


def test_a_provider_without_a_key_links_to_its_key_in_settings(page):
    steps.open_models(page)
    page.click("#models-menu .models-group:has(#models-g-openai) .models-keylink")
    page.wait(f"!{steps.flyout_open('models')} && {steps.is_open('settings')}", what="面板收起、设置打开")
    page.wait("document.activeElement === document.querySelector(\"#keys input[data-keep='key-openai']\")", what="焦点在 GPT 的密钥输入框里")
    steps.escape_closes(page, "settings")


def test_the_keyboard_moves_filters_picks_and_stays_inside(page):
    mac = _mac(page)
    page.focus("#input")
    page.key("meta+." if mac else "ctrl+.")
    page.wait(steps.flyout_open("models"), what="快捷键打开换模型的面板")
    page.wait("document.activeElement.dataset.model === 'claude-opus-5-5'", what="焦点在现在用的那个上")
    assert page.js("document.getElementById('model').title") == ("换模型（⌘.）" if mac else "换模型（Ctrl+.）")
    # ↑↓ 移动，End / Home 到头尾
    page.key("ArrowDown")
    assert page.js("document.activeElement.dataset.model") == "claude-sonnet-5-5"
    page.key("ArrowUp")
    assert page.js("document.activeElement.dataset.model") == "claude-opus-5-5"
    page.key("End")
    last = page.js("document.activeElement")
    assert last is not None and page.js("document.getElementById('models-menu').contains(document.activeElement)")
    page.key("Home")
    assert page.js("document.activeElement.closest('.models-group').querySelector('#models-g-anthropic') !== null")
    # Tab 不出面板
    for _ in range(6):
        page.key("Tab")
        assert page.js("document.getElementById('models').contains(document.activeElement)"), "Tab 走出了换模型的面板"
    page.key("shift+Tab")
    assert page.js("document.getElementById('models').contains(document.activeElement)")
    # 直接打字就是搜索：搜索框出来、接住字，只剩对得上的；回车换过去
    page.focus(ROW.format(model="claude-opus-5-5"))
    for letter in "fab":
        page.key(letter)
    page.wait("document.activeElement.id === 'models-search' && document.getElementById('models-search').value === 'fab'", what="打的字进了搜索框")
    assert not page.js("document.getElementById('models-find').hidden")
    assert page.js("[...document.querySelectorAll('#models-menu [role=menuitemradio]')].map((node) => node.dataset.model)") == ["claude-fable-5-2"]
    assert page.js("document.querySelector('#models-menu .models-next').dataset.model") == "claude-fable-5-2"
    page.key("Enter")
    page.wait("document.getElementById('model-label').textContent.includes('claude-fable-5-2')", what="回车换过去")
    # 找不到的：说一句，不是空白
    page.focus("#models-search")
    page.js("(() => { const box = document.getElementById('models-search'); box.value = 'zzz'; box.dispatchEvent(new Event('input')); })()")
    page.wait("document.querySelector('#models-menu .models-empty')", what="找不到时说一句")
    assert page.text("#models-menu .models-empty") == "没有找到“zzz”。"
    # Esc 收起来，焦点回到按钮上；再打开时搜索清空了
    steps.escape_closes_flyout(page, "models", "#model")
    page.key("ArrowDown")  # 焦点在按钮上时按 ↓ 也能打开
    page.wait(steps.flyout_open("models"))
    page.wait("document.getElementById('models-search').value === '' && document.querySelector('#models-menu [role=menuitemradio]')")
    page.key("meta+." if mac else "ctrl+.")  # 再按一次快捷键收起来
    page.wait(f"!{steps.flyout_open('models')}", what="快捷键收起")


def test_the_switcher_is_translated_in_place(page):
    steps.calm(page)
    steps.open_models(page)
    # 面板开着的时候换了语言（比如在另一个标签页里换的）：就地重译，面板不收起来
    page.demo.api("/api/language", {"language": "es"})
    page.wait("document.documentElement.lang === 'es'", what="换成西班牙语")
    page.wait("document.getElementById('models-title').textContent === 'Cambiar de modelo'")
    assert page.js(steps.flyout_open("models"))
    assert _tags(page, "claude-opus-5-5") == ["Busca en la web", "Entiende imágenes", "Lee PDF"]
    assert checks.untranslated(page, ["#models"]) == []
    page.demo.api("/api/language", {"language": "en"})
    page.wait("document.getElementById('models-title').textContent === 'Switch model'")
    assert checks.untranslated(page, ["#models"]) == []
    assert page.js("document.getElementById('model').title").startswith("Switch model (")


def test_on_a_phone_the_switcher_rises_from_the_bottom(page):
    page.size(PHONE_WIDTH, 844)
    steps.open_models(page)
    box = page.js("(() => { const r = document.getElementById('models').getBoundingClientRect(); return [r.left, r.right, r.bottom]; })()")
    width, height = page.js("[document.documentElement.clientWidth, innerHeight]")
    assert page.js("document.getElementById('models').classList.contains('as-sheet')")
    assert abs(box[0]) <= 1 and abs(box[1] - width) <= 1 and abs(box[2] - height) <= 1, f"面板应该贴着底边、占满宽度：{box}"
    assert checks.overflow(page, ["#models"]) == []
    # 点上面的暗底：只收起面板，不会顺带点到下面被盖住的按钮（左上角打开会话栏的那个）
    nav_closed = page.js("document.body.classList.contains('nav-closed')")
    x, y = page.js("(() => { const r = document.getElementById('nav-open').getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()")
    page.click_at(x, y)
    page.wait(f"!{steps.flyout_open('models')}", what="点暗底收起")
    steps.idle(page)
    assert page.js("document.body.classList.contains('nav-closed')") == nav_closed, "点暗底时点到了下面的按钮"


def test_with_reduced_motion_the_switcher_just_appears(page):
    steps.calm(page)
    page.click("#model")
    page.wait(steps.flyout_open("models"))
    # 入场动画一闪而过（减少动态效果时，所有动画都是 0.01 毫秒）
    durations = page.js("document.getElementById('models').getAnimations().map((a) => a.effect.getComputedTiming().duration)")
    assert all(duration <= 1 for duration in durations), durations
