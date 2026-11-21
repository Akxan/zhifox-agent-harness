"""几个面板和窗口：扩展的四页、空的样子、多媒体库和查看器、设置里换模型。"""

from __future__ import annotations

from . import steps


def test_extension_tabs_open_with_keyboard_and_mouse(page):
    steps.open_dialog(page, "#extensions-open", "extensions")
    # 演示里每一页都有东西：4 个 Skill、5 个 MCP 服务器、3 个 Agent；通道还没接上，是介绍的样子（空状态）
    expected = {
        "skills": "#panel-skills li.skill-row",
        "mcp": "#panel-mcp li.mcp-server",
        "channels": "#panel-channels .chan-hero",
        "agents": "#panel-agents li.agents-row",
    }
    for tab, selector in expected.items():
        steps.extensions_tab(page, tab)
        page.wait(f"document.querySelector({selector!r})", what=f"{tab} 这一页的内容")
        assert page.js(f"document.getElementById('tab-{tab}').getAttribute('aria-selected')") == "true"
        assert page.js("document.querySelectorAll('.tab-panel:not([hidden])').length") == 1
    assert page.js("document.querySelectorAll('#panel-skills li.skill-row').length") == 4
    assert page.js("document.querySelector('#panel-channels .chan-picks button')") is not None  # 空状态里能直接选一个平台连接
    # 方向键在标签之间走（WAI-ARIA 的标签页），Home / End 到头尾
    page.focus("#tab-agents")
    page.key("Home")
    page.wait("document.activeElement.id === 'tab-skills' && !document.getElementById('panel-skills').hidden")
    page.key("ArrowRight")
    page.wait("document.activeElement.id === 'tab-mcp' && !document.getElementById('panel-mcp').hidden")
    page.key("End")
    page.wait("document.activeElement.id === 'tab-agents' && !document.getElementById('panel-agents').hidden")
    steps.escape_closes(page, "extensions")


def test_empty_states(page):
    # 待办：一条都没有时写着"没有未完成的待办"
    steps.open_side(page)
    assert page.text("#todos li.none") == "没有未完成的待办"
    steps.close_side(page)
    # 搜索会话：找不到时说清楚，不是一片空白
    page.click("#search")
    page.type("一句谁都没说过的话qzx")
    page.wait("document.querySelector('#conversation-rows .conv-empty')", what="会话搜索的空状态")
    assert page.js("document.querySelectorAll('#conversation-rows .conv').length") == 0
    page.key("Escape")  # Esc 先清空搜索词
    page.wait("document.getElementById('search').value === '' && document.querySelector('#conversation-rows .conv')")
    # 记忆：搜不到的时候也说一句
    steps.open_dialog(page, "#memory-open", "memory-dialog")
    page.click("#memory-search")
    page.type("没有这条记忆qzx")
    page.wait("document.querySelectorAll('#memories > li.memory').length === 0 && document.querySelector('#memories > li.none')", what="记忆搜索的空状态")
    assert "没有这条记忆qzx" in page.text("#memories > li.none")
    page.key("Escape")  # 搜索框里有字时，Esc 先清空它（浏览器自己的做法），再按一次才关窗口
    page.wait("document.getElementById('memory-search').value === '' && document.querySelector('#memories > li.memory')")
    steps.escape_closes(page, "memory-dialog")
    # 多媒体：按名字搜不到
    steps.nav_tab(page, "media")
    page.click("#media-search")
    page.type("没有这个文件qzx")
    page.wait("document.querySelector('#media-list .media-empty')", what="多媒体搜索的空状态")
    assert "没有这个文件qzx" in page.text("#media-list .media-empty")


def test_media_library_opens_the_viewer(page):
    steps.nav_tab(page, "media")
    page.wait("document.querySelector('#media-list .media-tile-open img')", what="图片的方格")
    # 点图片的正中间（真的鼠标）：鼠标一移上去，下沿那排按钮就淡入了，但点在图片上还是打开这张图
    page.click("#media-list .media-tile-open")
    page.wait("document.getElementById('viewer').open", what="查看器打开")
    page.wait("document.getElementById('viewer-image').complete && document.getElementById('viewer-image').naturalWidth > 0", what="大图画出来")
    assert page.text("#viewer-name") == "旧截图.png"
    # 方向键看下一个（PDF），Esc 关掉
    page.key("ArrowRight")
    page.wait("document.getElementById('viewer-name').textContent === '旧报告.pdf'")
    steps.escape_closes(page, "viewer")
    # 文件那一行也一样：点名字打开
    page.click("#media-list .media-row-open .media-name")
    page.wait("document.getElementById('viewer').open")
    steps.escape_closes(page, "viewer")


def test_switch_to_another_provider_in_settings(page):
    # 设置里填一个（编造的）DeepSeek 密钥、换到 DeepSeek，接着就能聊（回答来自本地的假服务器）
    steps.open_dialog(page, "#model", "settings")
    page.click("#keys input[data-keep='key-deepseek']")
    page.type("fake-deepseek-key-0002")
    page.click("#keys .key-row:has(input[data-keep='key-deepseek']) .button.primary")
    page.wait("document.querySelector('#keys .key-row:has(input[data-keep=\\'key-deepseek\\']) .tag.set')", what="密钥保存好（显示最后 4 位）")
    assert "0002" in page.text("#keys .key-row:has(input[data-keep='key-deepseek']) .tag.set")
    assert page.js("document.querySelector(\"#keys input[data-keep='key-deepseek']\").value") == ""  # 完整的密钥不再留在页面上
    page.js("(() => { const pick = document.getElementById('pick-provider'); pick.value = 'deepseek'; pick.dispatchEvent(new Event('change')); })()")
    page.wait("[...document.getElementById('pick-model').options].some((option) => option.value === 'deepseek-flash')", what="DeepSeek 的模型列出来")
    page.js("document.getElementById('pick-model').value = 'deepseek-flash'")
    page.click("#apply-model")
    page.wait("!document.getElementById('settings').open && document.getElementById('model-label').textContent.includes('deepseek')", what="换到 DeepSeek")
    steps.send(page, "你好")
    steps.reply(page, "我是 DeepSeek")


def test_settings_say_how_to_keep_zhifox_running(page):
    """设置里"后台运行"一节：演示和 harness --web 一样是跟着终端开的，所以给出装成后台服务的命令，可以复制
    （见 page/scripts/service.js）。复制的结果写在设置面板里（窗口里的提示写在窗口里）。"""
    steps.open_dialog(page, "#model", "settings")
    page.wait("!document.getElementById('service-section').hidden", what="后台运行这一节")
    assert page.text("#service-card b") == "跟着启动它的终端"
    assert page.text("#service-card .service-command code") == "uv run harness service install"
    assert not page.js("document.getElementById('service-card').classList.contains('on')")
    page.js("document.querySelector('#service-card .service-command .button').scrollIntoView({ block: 'center' })")
    page.click("#service-card .service-command .button")
    # 无界面的 Chrome 里剪贴板可能不给用：复制成了、没成，都要在面板里说一声
    page.wait("['已复制。', '没能复制，请手动选中复制。'].includes(document.getElementById('settings-message').textContent)", what="复制的结果")
    steps.escape_closes(page, "settings")
