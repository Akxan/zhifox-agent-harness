"""在 Windows、Linux 上用：页面上不出现 ⌘、⌥、"访达"、Finder，快捷键写 Ctrl，"在……中显示"写对文件管理器的名字。

页面上有两种"是哪种电脑"，看的不是同一台（见 harness/webui/page/scripts/platform.js）：
  浏览器那台      快捷键（⌘ 还是 Ctrl）。这里用 CDP 的 Emulation.setUserAgentOverride 换掉 navigator.platform
  助手程序那台    文件管理器的名字（访达 / 文件资源管理器 / 文件管理器）、"操作整台电脑"举的例子。这里另外启动几个
                  带着 --platform 的演示服务器（demo_web.py 换掉 machine.system，只在演示里，正式程序永远照 sys.platform 说）
所以还有一条 Mac 的浏览器 + Linux 的服务器（在 Mac 上通过 SSH 隧道看服务器上的智狐）：快捷键是 ⌘，文件管理器是"文件管理器"。

不该有的字（checks.containing）查整个页面，看不见的也算（关着的窗口、悬停才出现的按钮）；"在……中显示"的按钮
（checks.reveal_labels）只看画出来的：关着的窗口里的，打开的时候才按当前的语言重画。有的列表要打开那一处才画，
所以每种语言都把那些地方打开一遍（_walk）。换语言用的是右上角的语言按钮，在原地换。
"""

from __future__ import annotations

import contextlib
import re

import pytest
from fake_telegram import TOKEN

from . import checks, steps
from .conftest import Demo, finish_page, show_demo

# 浏览器报的系统：navigator.platform，和用户代理里括号中的那一段
BROWSERS = {
    "windows": ("Win32", "Windows NT 10.0; Win64; x64"),
    "linux": ("Linux x86_64", "X11; Linux x86_64"),
    "mac": ("MacIntel", "Macintosh; Intel Mac OS X 10_15_7"),
}
# 每种电脑上的文件管理器，三种语言里怎么叫（"在……中显示"的按钮上一定有它）
FILE_MANAGER = {
    "windows": {"zh": "文件资源管理器", "en": "File Explorer", "es": "Explorador de archivos"},
    "linux": {"zh": "文件管理器", "en": "file manager", "es": "gestor de archivos"},
    "mac": {"zh": "访达", "en": "Finder", "es": "Finder"},
}
MAC_WORDS = ["⌘", "⌥", "访达", "Finder"]  # 不是 Mac 的电脑上，一个都不该出现
# 另外两种电脑上文件管理器的说法：Windows 上不该写 Linux 的，Linux 上不该写 Windows 的
WINDOWS_WORDS = ["文件资源管理器", "File Explorer", "Explorador de archivos"]
LINUX_WORDS = ["在文件管理器中", "in the file manager", "en el gestor de archivos"]
NOT_ON = {"windows": MAC_WORDS + LINUX_WORDS, "linux": MAC_WORDS + WINDOWS_WORDS, "mac": WINDOWS_WORDS + LINUX_WORDS}


@pytest.fixture(scope="module")
def demos(tmp_path_factory, browser, artifacts):
    """当作在 Mac、Windows、Linux 上的演示服务器，用到哪个才启动哪个（每个要几秒）。这个文件里的测试共用。
    测试是分到几个进程里并行跑的：一个进程只跑到其中一两个测试时，也只启动它们要的那一两个。"""
    started: dict[str, Demo] = {}

    def demo(system: str) -> Demo:
        if system not in started:
            started[system] = Demo(tmp_path_factory.mktemp(f"ui-demo-{system}"), ("--platform", system))
        return started[system]

    yield demo
    for system, server in started.items():
        server.stop()
        (artifacts / f"demo-server-{system}.log").write_text("".join(server.output), encoding="utf-8")


@pytest.fixture
def visit(request, browser, artifacts, demos):
    """visit(浏览器的系统, 服务器的系统)：开一个新标签页，浏览器报那种系统，打开那个演示服务器。"""
    opened = []

    def visit(computer: str, server: str):
        demo = demos(server)
        demo.reset()
        page = browser.new_page()
        page.demo = demo
        opened.append(page)
        platform, inside = BROWSERS[computer]
        agent = re.sub(r"\([^)]*\)", f"({inside})", page.js("navigator.userAgent"), count=1)
        page.call("Emulation.setUserAgentOverride", {"userAgent": agent, "platform": platform})
        show_demo(page)
        assert page.js("navigator.platform") == platform
        return page

    yield visit
    for page in opened:
        finish_page(request, page, artifacts)


@contextlib.contextmanager
def stocked(page):
    """每一种本地的东西都至少有一行（加一条待办、接上假的 Telegram、加一条不用问的命令），用完收掉。"""
    steps.send(page, "[ui:todo] 加一条待办")
    steps.reply(page, "记下了")
    assert page.demo.api("/api/channel/connect", {"platform": "telegram", "token": TOKEN})[0] == 200
    assert page.demo.api("/api/commands/add", {"command": "npm test"})[0] == 200
    try:
        yield page
    finally:
        page.demo.api("/api/channel/remove", {"platform": "telegram"})
        page.demo.api("/api/commands/delete", {"command": "npm test"})
        for todo in page.js("[...document.querySelectorAll('#todos > li.todo')].map((row) => Number(row.dataset.id))"):
            page.demo.api("/api/todo/delete", {"id": todo})


def _walk(page, look) -> None:
    """把有"在……中显示"按钮、有快捷键提示的地方一处处打开（有的列表要打开才画），每打开一处调用一次 look(名字)。"""
    look("主页面")
    steps.nav_tab(page, "tasks")
    look("定时任务")
    steps.nav_tab(page, "media")
    page.click("#media-list button[class*='open']")
    page.wait(steps.is_open("viewer"), what="查看器打开")
    steps.idle(page)
    look("查看器")
    steps.escape_closes(page, "viewer")
    steps.nav_tab(page, "chats")
    steps.open_side(page)
    look("信息栏")
    steps.close_side(page)

    steps.open_dialog(page, "#memory-open", "memory-dialog")
    page.wait("document.querySelectorAll('#memories > li').length > 0", what="记忆列表")
    page.click("#memories > li .memory-open")
    page.wait("document.getElementById('memory-text').textContent.length > 0", what="一条记忆的内容")
    page.click("#memory-edit")
    page.wait("document.getElementById('memory-save')", what="开始编辑")
    look("记忆（编辑）")
    page.key("Escape")  # 没改过：回到"看"
    page.wait("document.getElementById('memory-reveal')", what="回到看的样子")
    look("记忆（看一条）")
    steps.escape_closes(page, "memory-dialog")

    steps.open_dialog(page, "#settings-open", "settings")
    look("设置")
    page.click("#commands-open")
    page.wait(f"{steps.is_open('commands-dialog')} && document.querySelector('#commands-list li.cmd-row')", what="不用问的命令")
    steps.idle(page)
    look("不用问的命令")
    steps.escape_closes(page, "commands-dialog")
    if page.js(steps.is_open("settings")):
        steps.escape_closes(page, "settings")

    steps.open_models(page)  # 换模型的面板：底下写着打开它的快捷键（⌘. / Ctrl+.）
    look("换模型")
    steps.escape_closes_flyout(page, "models", "#model")

    for tab in ("skills", "mcp", "channels", "agents"):
        steps.extensions_tab(page, tab)
        look(f"扩展 · {tab}")
    steps.extensions_tab(page, "skills")
    page.click("#panel-skills li.skill-row .skill-open")
    page.wait("document.querySelector('#panel-skills [data-key=\"reveal\"]')", what="打开一个 Skill")
    look("扩展 · 一个 Skill")
    page.key("Escape")  # 子页面里的 Esc 先回到列表
    page.wait("document.querySelector('#panel-skills li.skill-row')", what="回到 Skill 列表")
    steps.escape_closes(page, "extensions")


def _sweep(page, system: str) -> dict[str, list[str]]:
    """三种语言各走一遍，记下每处的问题：还写着 Mac 的字，"在……中显示"的按钮没写对文件管理器的名字。"""
    found: dict[str, list[str]] = {}
    for code in ("zh", "en", "es"):
        steps.language(page, code)
        expected = FILE_MANAGER[system][code]

        def look(where: str, code=code, expected=expected) -> None:
            problems = checks.containing(page, NOT_ON[system])
            problems += [f"{item['where']}: {item['label']!r} 里没有“{expected}”" for item in checks.reveal_labels(page) if expected not in item["label"]]
            if problems:
                found[f"{code} · {where}"] = problems

        _walk(page, look)
        # 快捷键写 Ctrl
        assert page.text("#search-key") == "Ctrl F"
        assert "Ctrl+," in page.js("document.getElementById('settings-open').title")
        assert "Alt+F4" in page.text("#computer-risk-keys")
    return found


@pytest.mark.parametrize("system", ["windows", "linux"])
def test_no_mac_words_on_windows_or_linux_in_any_language(visit, system):
    page = visit(system, system)
    steps.calm(page)
    with stocked(page):
        found = _sweep(page, system)
    assert found == {}, f"{system} 上还有这些地方不对：\n" + "\n".join(f"{where}:\n  " + "\n  ".join(items) for where, items in found.items())


def test_the_shortcuts_use_ctrl_on_windows(visit):
    page = visit("windows", "windows")
    steps.calm(page)
    # Ctrl+K、Ctrl+F：搜索会话
    page.click("#title")
    page.key("ctrl+k")
    page.wait("document.activeElement === document.getElementById('search')", what="Ctrl+K 到了搜索框")
    page.key("Escape")
    page.click("#title")
    page.key("ctrl+f")
    page.wait("document.activeElement === document.getElementById('search')", what="Ctrl+F 到了搜索框")
    page.key("Escape")
    # Ctrl+,：打开设置；⌘, 在 Windows 上不算（Windows 键 + 逗号是系统的）
    page.click("#title")
    page.key("meta+,")
    assert not page.js(steps.is_open("settings"))
    page.key("ctrl+,")
    page.wait(steps.is_open("settings"), what="Ctrl+, 打开了设置")
    steps.escape_closes(page, "settings")
    # 编辑记忆时 Ctrl+Enter 保存
    steps.open_nav(page)
    steps.open_dialog(page, "#memory-open", "memory-dialog")
    page.click("#memories > li .memory-open")
    page.wait("document.getElementById('memory-text').textContent.length > 0", what="一条记忆的内容")
    page.click("#memory-edit")
    page.wait("document.getElementById('memory-save')", what="开始编辑")
    assert "Ctrl+Enter" in page.text("#memory-foot .memory-message")
    page.focus("#memory-edit-text")
    page.js("document.getElementById('memory-edit-text').setSelectionRange(1e9, 1e9)")
    page.type("（Windows 上加的一行）")
    page.key("ctrl+Enter")
    page.wait("document.getElementById('memory-text').textContent.includes('（Windows 上加的一行）')", what="Ctrl+Enter 保存了")
    # 显示了文件之后说的那句话也写"文件资源管理器"
    page.click("#memory-reveal")
    page.wait("document.querySelector('#memory-foot .memory-message').textContent.startsWith('已在文件资源管理器中显示：')", what="显示之后的那句话")
    steps.escape_closes(page, "memory-dialog")


def test_computer_use_says_plainly_that_windows_is_not_supported_yet(visit):
    """Windows 上还不能操作整台电脑：信息栏和设置里都直说，不给 macOS 的权限说明、Linux 要装的程序。"""
    page = visit("windows", "windows")
    steps.calm(page)
    steps.expand_capabilities(page)
    assert "Windows 上暂时不支持操作整台电脑" in page.text("#caps")
    steps.close_side(page)
    steps.open_dialog(page, "#settings-open", "settings")
    page.wait("document.querySelector('#computer-checks .computer-check')", what="设置里“操作整台电脑”的检查")
    checks_text = page.text("#computer-checks")
    assert "Windows 上暂时不支持操作整台电脑" in checks_text
    assert "系统设置" not in checks_text and "xdotool" not in checks_text
    # 设置开着的时候换语言（比如在别的标签页里换的）：这一节在原地换成英文
    assert page.demo.api("/api/language", {"language": "en"})[0] == 200
    page.wait("document.documentElement.lang === 'en'", what="换成英文")
    page.wait("document.getElementById('computer-checks').textContent.includes(\"isn't supported on Windows yet\")", what="设置里的说明换成英文")


def test_mac_wording_is_unchanged_on_a_mac(visit):
    page = visit("mac", "mac")
    steps.calm(page)
    for code in ("zh", "en"):
        steps.language(page, code)
        labels = checks.reveal_labels(page)
        assert labels and [item for item in labels if FILE_MANAGER["mac"][code] not in item["label"]] == []
        assert checks.containing(page, NOT_ON["mac"]) == []
        assert page.text("#search-key") == "⌘F"
        assert "⌘," in page.js("document.getElementById('settings-open').title")
        assert "⌘Q" in page.text("#computer-risk-keys")


def test_a_mac_browser_on_a_linux_server_keeps_the_command_key(visit):
    """在 Mac 上通过 SSH 隧道打开 Linux 服务器上的智狐：键是在 Mac 上按的（⌘），文件管理器开在服务器上（"文件管理器"），
    "操作整台电脑"操作的也是服务器那台（Alt+F4）。"""
    page = visit("mac", "linux")
    labels = checks.reveal_labels(page)
    assert labels and [item for item in labels if "在文件管理器中" not in item["label"]] == []
    assert page.text("#search-key") == "⌘F"
    assert "⌘," in page.js("document.getElementById('settings-open').title")
    assert "Alt+F4" in page.text("#computer-risk-keys")
