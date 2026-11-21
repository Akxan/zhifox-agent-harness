"""页面本身：打开时干干净净（没有报错、不加载外部的东西），外观（浅色、深色、跟随系统）和动效的设置，下拉框的箭头。"""

from __future__ import annotations

from . import checks, steps

# WCAG 的对比度：正文至少 4.5:1
CONTRAST = r"""
((selectors) => {
  const parse = (color) => (color.match(/[\d.]+/g) || []).map(Number);
  const luminance = ([r, g, b]) => {
    const linear = (c) => (c /= 255) <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
  };
  const backdrop = (node) => {
    for (let at = node; at; at = at.parentElement) {
      const [r, g, b, a = 1] = parse(getComputedStyle(at).backgroundColor);
      if (a > 0.9) return [r, g, b];
    }
    return [255, 255, 255];
  };
  return selectors.map((selector) => {
    const node = document.querySelector(selector);
    if (!node) return [selector, null];
    const ink = luminance(parse(getComputedStyle(node).color)), paper = luminance(backdrop(node));
    return [selector, Math.round(((Math.max(ink, paper) + 0.05) / (Math.min(ink, paper) + 0.05)) * 100) / 100];
  });
})
"""
TEXT = ["#hero-lead", ".conv-title", ".conv-meta", "#title", ".keys", "#foot-cost"]


def test_page_loads_without_errors_or_outside_resources(page):
    # 页面亮相了，状态也到了（右上角显示着模型），会话列表画出来了
    page.wait("document.getElementById('model-label').textContent.includes('claude')", what="状态到了")
    page.wait("document.querySelector('#conversation-rows .conv')", what="会话列表画好")
    # 这一套测试是按"有鼠标的电脑"写的（悬停才出现的按钮）：先确认浏览器也这么认为（见 pilot.Browser.ARGS）
    assert page.js("matchMedia('(hover: hover) and (pointer: fine)').matches")
    # 只从自己这里拿东西：页面的安全策略不允许外部资源，离线也要能用
    origin = page.js("location.origin")
    outside = page.js(f"performance.getEntriesByType('resource').map((entry) => entry.name).filter((name) => !name.startsWith({origin!r}) && !name.startsWith('data:'))")
    assert outside == []
    assert page.js("document.querySelectorAll('link[rel=stylesheet], script[src], iframe').length") == 0
    # 刷新一次：重放旧内容时同样不能出错（出了错 page 收尾时会报出来）
    page.reload()
    page.wait("document.body.classList.contains('ready')")


def test_light_by_default_dark_and_follow_system(page):
    # 默认是浅色，系统是深色也一样（用户定的："我想默认是白色"）
    page.emulate(prefers_color_scheme="dark")
    page.reload()
    page.wait("document.body.classList.contains('ready')")
    assert page.js("document.documentElement.dataset.theme") == "light"
    light_bg = page.js("getComputedStyle(document.body).backgroundColor")
    for selector, ratio in page.js(f"({CONTRAST})({TEXT!r})"):
        assert ratio is None or ratio >= 4.5, f"浅色下 {selector} 的对比度只有 {ratio}"

    page.click("#theme")
    page.wait("document.getElementById('appearance').matches(':popover-open')", what="外观面板打开")
    assert page.js("document.activeElement.dataset.themePick") == "light"  # 焦点放在当前选中的那一格上
    page.click("[data-theme-pick='dark']")
    page.wait("document.documentElement.dataset.theme === 'dark'", what="换成深色")
    steps.idle(page)
    assert page.js("getComputedStyle(document.body).backgroundColor") != light_bg
    for selector, ratio in page.js(f"({CONTRAST})({TEXT!r})"):
        assert ratio is None or ratio >= 4.5, f"深色下 {selector} 的对比度只有 {ratio}"
    # Esc 收起面板，焦点回到外观按钮上
    page.key("Escape")
    page.wait("!document.getElementById('appearance').matches(':popover-open') && document.activeElement.id === 'theme'")

    # 选择记在浏览器里，刷新以后还是深色
    page.reload()
    page.wait("document.body.classList.contains('ready')")
    assert page.js("document.documentElement.dataset.theme") == "dark"

    # 跟随系统：系统改成浅色，页面马上跟着变，不用刷新
    page.click("#theme")
    page.click("[data-theme-pick='auto']")
    page.wait("document.documentElement.dataset.theme === 'dark'")
    steps.idle(page)
    page.emulate(prefers_color_scheme="light")
    page.wait("document.documentElement.dataset.theme === 'light'", what="跟着系统变成浅色")


def test_switching_appearance_twice_quickly_does_not_throw(page):
    # 换外观时两个画面交叉淡化（视图过渡）。前一个过渡还没开始（不到一两帧）又换了一次，浏览器跳过前一个，
    # 它的 ready 就成了一个被拒绝的 Promise。页面没接住的话，控制台里是一条未处理的 AbortError。
    # 人很难点得这么快，但系统外观正好在这时变了、或者以后有代码连着换两次，都会走到这里（出错的话 page 收尾时会报出来）
    page.click("#theme")
    page.wait("document.getElementById('appearance').matches(':popover-open')")
    steps.idle(page)
    page.js("document.querySelector('[data-theme-pick=dark]').click(), document.querySelector('[data-theme-pick=light]').click()")
    page.wait("document.documentElement.dataset.theme === 'light'")
    page.wait("!document.getAnimations({ subtree: true }).some((a) => String(a.effect.pseudoElement || '').includes('view-transition'))", what="淡化播完")


def _animations_during_reply(page, text: str, expect: str) -> list[str]:
    """发一条消息，回复的整个过程中每有变化就看一眼：正在播、而且时长超过 1 毫秒的动画有哪些。"""
    page.js(
        "window.__seen = new Set();"
        "window.__watch = new MutationObserver(() => { for (const a of document.getAnimations()) {"
        "  const timing = a.effect.getComputedTiming();"
        "  if (a.playState === 'running' && timing.duration > 1) window.__seen.add((a.animationName || a.constructor.name) + ' on ' + (a.effect.target.id || a.effect.target.className)); } });"
        "window.__watch.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true });"
    )
    steps.send(page, text)
    steps.reply(page, expect)
    return page.js("window.__watch.disconnect(), [...window.__seen]")


def test_reduced_motion_follows_system_and_the_appearance_panel(page):
    page.emulate(prefers_reduced_motion="reduce")
    page.reload()
    page.wait("document.body.classList.contains('ready')")
    assert page.js("document.documentElement.dataset.motion") == "reduce"  # 默认跟随系统

    # 用户在外观面板里选"完整"：系统要求减少也照样播
    page.click("#theme")
    page.click("[data-motion-pick='full']")
    page.wait("document.documentElement.dataset.motion === 'full'")
    page.key("Escape")
    assert _animations_during_reply(page, "[ui:hello] 有动画", "收到第 1 条"), "完整的动效下，回复出现时应该有入场动画"

    # 选"减少"：系统没有要求也不播。回复整个过程中，没有一个超过 1 毫秒的动画。
    # 先等上一段的动画播完：已经在播的 CSS 过渡不会因为换了设置就停下（过渡的时长只在开始时读一次），那不是这里要查的
    steps.idle(page)
    page.emulate(prefers_reduced_motion="no-preference")
    page.click("#theme")
    page.click("[data-motion-pick='reduce']")
    page.wait("document.documentElement.dataset.motion === 'reduce'")
    page.key("Escape")
    assert _animations_during_reply(page, "[ui:hello] 没有动画", "收到第 2 条") == []


def test_dropdowns_draw_their_own_chevron(page):
    # 下拉框一律是 select.field，箭头由页面自己画（两道渐变），颜色跟着主题。
    # 新建任务的窗口里有一个（每隔几小时 / 几分钟）；设置里以前选模型的两个下拉框换成了换模型的面板（models.js）
    steps.nav_tab(page, "tasks")
    steps.open_dialog(page, "#task-new", "task-editor")
    assert checks.native_selects(page) == []
    light = page.js("getComputedStyle(document.getElementById('task-unit')).getPropertyValue('--chevron').trim()")
    steps.escape_closes(page, "task-editor")
    page.click("#theme")
    page.click("[data-theme-pick='dark']")
    page.wait("document.documentElement.dataset.theme === 'dark'")
    page.key("Escape")
    dark = page.js("getComputedStyle(document.getElementById('task-unit')).getPropertyValue('--chevron').trim()")
    assert light and dark and light != dark
    steps.open_dialog(page, "#settings-open", "settings")
    assert checks.native_selects(page) == []


def test_the_checks_themselves_catch_planted_problems(page):
    # 这些检查要是什么都查不出来，别的测试"没发现问题"就没有意义。在页面上故意放几个有问题的东西，它们必须被查出来
    page.js(
        "(() => { const stage = document.querySelector('.stage');"
        "  const leak = stage.appendChild(document.createElement('div'));"
        "  leak.id = 'planted-leak'; leak.style.cssText = 'width:100px;border:1px solid;white-space:nowrap';"
        "  leak.textContent = 'a very long line of text that cannot fit';"
        "  const cut = stage.appendChild(document.createElement('div'));"
        "  cut.id = 'planted-cut'; cut.style.cssText = 'width:100px;overflow:hidden;white-space:nowrap';"
        "  cut.textContent = 'another long line that is cut without an ellipsis';"
        "  const row = stage.appendChild(document.createElement('div'));"
        "  row.className = 'planted-row';"
        "  for (const name of ['edit', 'folder', 'trash']) { const b = row.appendChild(document.createElement('button'));"
        "    b.innerHTML = '<svg><use href=\"#i-' + name + '\"></use></svg>'; }"
        "  const words = stage.appendChild(document.createElement('button'));"
        "  words.id = 'planted-words'; words.textContent = '忘了翻译'; words.title = '这个也忘了';"
        "  stage.appendChild(document.createElement('select')).id = 'planted-select';"
        "})()"
    )
    leaks = checks.overflow(page, [".stage"])
    assert any("planted-leak" in item and "冲出了边框" in item for item in leaks)
    assert any("planted-cut" in item and "没有省略号" in item for item in leaks)
    assert checks.row_trio(page, ".planted-row")
    assert any("planted-select" in item for item in checks.native_selects(page))
    found = checks.untranslated(page, [".stage"])
    assert any("忘了翻译" in item for item in found) and any("这个也忘了" in item for item in found)
