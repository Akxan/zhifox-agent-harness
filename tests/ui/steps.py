"""在智狐的页面上常做的几步：发消息、等回复、换语言、打开会话栏和信息栏、打开某个窗口……

pilot.py 只会"点、按、读"，不知道页面长什么样；这里知道。页面的结构变了（换了 id、换了打开方式），
改这一处，所有测试都跟着对。写新测试时先看看这里有没有现成的一步。
"""

from __future__ import annotations

import json
import sys

from .pilot import Page

# 页面上没有正在播的动画了（无限循环的不算，比如背景的极光和"正在工作"的圈）
IDLE = (
    "document.getAnimations().every((a) => a.playState !== 'running' || !Number.isFinite(a.effect.getComputedTiming().endTime))"
)
NARROW_NAV = "matchMedia('(max-width: 960px)').matches"  # 和页面里的 overlayNav 一致：窄屏上会话栏是抽屉
NARROW_SIDE = "matchMedia('(max-width: 1180px)').matches"  # 信息栏也一样


def idle(page: Page, timeout: float = 5) -> None:
    page.wait(IDLE, timeout, "动画播完")


def calm(page: Page) -> None:
    """让系统"要求减少动态效果"（页面的动效默认跟随系统）：动画都一闪而过，不用一个个等它们播完。

    只给那些查静态样子的测试用（换了语言有没有漏翻译、窄屏上文字有没有冲出去）：打开、关上几十个窗口，
    每个都等 0.3 秒的动画，整个测试会慢很多。查动画本身、查交互的测试不要用它。
    """
    page.emulate(prefers_reduced_motion="reduce")
    page.wait("document.documentElement.dataset.motion === 'reduce'", what="动效变成减少")


def send(page: Page, text: str) -> None:
    """在输入框里打字，按回车发出去，等这条消息出现在对话里。"""
    page.click("#input")
    page.type(text)
    page.key("Enter")
    page.wait(f"[...document.querySelectorAll('#log .msg.user')].some((node) => node.textContent === {json.dumps(text)})", 10, "发出的消息出现在对话里")


def reply(page: Page, contains: str, timeout: float = 20) -> None:
    """等助手回复的话里出现 contains，并且这一轮结束了。"""
    page.wait(
        f"!document.body.classList.contains('busy') && [...document.querySelectorAll('#log .msg.assistant')].some((node) => node.textContent.includes({json.dumps(contains)}))",
        timeout,
        f"回复里出现“{contains}”",
    )


def pending_confirm(page: Page, timeout: float = 20) -> None:
    """等一张还没答复的确认卡片出现（按钮可以点）。"""
    page.wait("document.querySelector('#log .confirm:not(.done) .button.primary:not(:disabled)')", timeout, "确认卡片出现")


def language(page: Page, code: str) -> None:
    """用右上角的语言按钮（地球）换语言，等页面换好（<html lang> 变了、语言面板收起来了）。"""
    open_flyout(page, "#language", "languages")
    lang = {"zh": "zh-CN", "en": "en", "es": "es"}[code]
    page.click(f"#language-options .lang-option[lang='{lang}']")
    page.wait(f"document.documentElement.lang === '{lang}' && !{flyout_open('languages')}", what=f"换成 {code}")
    idle(page)


def flyout_open(panel: str) -> str:
    """顶栏上的小面板（外观、换模型、语言：popover，见 page/scripts/flyouts.js）开着没有。"""
    return f"document.getElementById({json.dumps(panel)}).matches(':popover-open')"


def open_flyout(page: Page, button: str, panel: str) -> None:
    """点顶栏上的按钮，等它的小面板弹出来、动画播完。"""
    page.click(button)
    page.wait(flyout_open(panel), what=f"{panel} 弹出来")
    idle(page)


def escape_closes_flyout(page: Page, panel: str, button: str) -> None:
    """按 Esc，等小面板收起来、焦点回到它的按钮上。"""
    page.key("Escape")
    page.wait(f"!{flyout_open(panel)} && document.activeElement === document.querySelector({json.dumps(button)})", what=f"{panel} 收起来")
    idle(page)


def open_models(page: Page) -> None:
    """点顶栏的模型按钮，等换模型的面板弹出来、目录画好。"""
    open_flyout(page, "#model", "models")
    page.wait("document.querySelector('#models-menu [role=menuitemradio]')", what="模型列出来")
    idle(page)


def open_nav(page: Page) -> None:
    """会话栏展开（窄屏上是抽屉，点左上角的按钮）。"""
    if page.js("document.body.classList.contains('nav-closed')"):
        page.click("#nav-open")
        page.wait("!document.body.classList.contains('nav-closed')")
    idle(page)


def close_nav(page: Page) -> None:
    if not page.js("document.body.classList.contains('nav-closed')"):
        page.click("#nav-close")
        page.wait("document.body.classList.contains('nav-closed')")
    idle(page)


def nav_tab(page: Page, name: str) -> None:
    """会话栏上面的标签：chats、tasks、media。"""
    open_nav(page)
    page.click(f"#nav-tab-{name}")
    page.wait(f"document.getElementById('nav-tab-{name}').getAttribute('aria-selected') === 'true'")
    idle(page)


def open_side(page: Page) -> None:
    """信息栏（用量、待办、工具）展开。"""
    if not page.js("document.body.classList.contains('side-open')"):
        page.click("#side-toggle")
        page.wait("document.body.classList.contains('side-open')")
    idle(page)


def close_side(page: Page) -> None:
    if page.js("document.body.classList.contains('side-open')"):
        page.click("#side-close" if page.js(NARROW_SIDE) else "#side-toggle")
        page.wait("!document.body.classList.contains('side-open')")
    idle(page)


def open_dialog(page: Page, button: str, dialog: str) -> None:
    """点 button，等 id 为 dialog 的窗口打开、动画播完。"""
    page.click(button)
    page.wait(f"document.getElementById({json.dumps(dialog)}).open", what=f"{dialog} 打开")
    idle(page)


def escape_closes(page: Page, dialog: str) -> None:
    """按 Esc，等窗口关上。"""
    page.key("Escape")
    page.wait(f"!document.getElementById({json.dumps(dialog)}).open", what=f"{dialog} 关上")
    idle(page)


def is_open(dialog: str) -> str:
    return f"document.getElementById({json.dumps(dialog)}).open"


def extensions_tab(page: Page, tab: str) -> None:
    """打开"扩展"窗口里的一页（skills、mcp、channels、agents、hooks），等它画好。"""
    if not page.js(is_open("extensions")):
        open_dialog(page, "#extensions-open", "extensions")
    page.click(f"#tab-{tab}")
    page.wait(f"!document.getElementById('panel-{tab}').hidden && document.getElementById('panel-{tab}').children.length > 0", what=f"{tab} 这一页画好")
    idle(page)


# 三页各自的"从其他工具导入"按钮
IMPORT_BUTTONS = {"mcp": "#panel-mcp [data-focus='import']", "skills": "#panel-skills [data-key='bar:import']", "agents": "#panel-agents [data-key='bar:import']"}


def import_view(page: Page, tab: str) -> None:
    """打开"扩展"里一页的"从其他工具导入"（mcp、skills、agents），等清单画好，把最下面的"没找到的"也点开。"""
    extensions_tab(page, tab)
    page.click(IMPORT_BUTTONS[tab])
    page.wait(f"document.querySelector('#panel-{tab} .import-overview')", what=f"{tab} 的导入页画好")
    page.js(f"document.querySelectorAll('#panel-{tab} details.import-missing').forEach((details) => {{ details.open = true; }})")
    idle(page)


def close_extensions(page: Page) -> None:
    """点右上角的 × 关掉"扩展"窗口（导入页里按 Esc 是先回到上一层，不是关窗口）。"""
    page.click("#close-extensions")
    page.wait(f"!{is_open('extensions')}", what="扩展窗口关上")
    idle(page)


def save_hook(page: Page, name: str, script: str, extra: tuple[str, ...] = (), **hook) -> dict:
    """存一个钩子（直接调接口，不经过表单）：script 是一小段 Python，写进演示的临时目录，用跑测试的这个 Python 运行
    （sys.executable：Windows 上也有，不靠 shell 脚本），extra 是脚本后面再跟的参数。hook 里给的字段盖过默认的
    （工具执行之前、对执行命令生效）。
    演示服务器每个测试开始前删掉 hooks.json（见 conftest.py 的 reset），所以不用自己收拾。"""
    folder = page.demo.root / "hook-scripts"
    folder.mkdir(exist_ok=True)
    path = folder / f"{name}.py"
    path.write_text(script, encoding="utf-8")
    body = {"event": "PreToolUse", "matcher": "shell", "command": sys.executable, "args": [str(path), *extra], "timeout": None,
            "enabled": True, "onFailure": "continue", "runsIn": ["chat", "task", "channel"], **hook}
    status, reply = page.demo.api("/api/hooks/save", {"original": None, "name": name, "hook": body})
    assert status == 200, reply
    return reply


def open_hook(page: Page, name: str) -> None:
    """在"扩展 · 钩子"里点开一个钩子，等它的详情（设置、试运行）画好。"""
    extensions_tab(page, "hooks")
    page.wait(f"document.querySelector('#panel-hooks li.hooks-row[data-name={json.dumps(name)}]')", what=f"钩子 {name} 出现在列表里")
    page.click(f"#panel-hooks li.hooks-row[data-name={json.dumps(name)}] .hooks-open")
    page.wait("document.querySelector('#panel-hooks .hooks-test')", what="钩子的详情打开")
    idle(page)


def open_trace(page: Page, index: int = -1) -> None:
    """点一轮上的"运行记录"（默认最后一轮），等它的窗口滑出来、横条长完（见 page/scripts/trace.js）。"""
    count = page.js("document.querySelectorAll('#log .trace-open').length")
    assert count, "对话里还没有运行记录的入口"
    page.click("#log .trace-open", index % count)
    page.wait(f"{is_open('trace-dialog')} && document.querySelector('#trace-body .trace-lane')", what="运行记录的窗口打开")
    idle(page)


def expand_capabilities(page: Page) -> None:
    """信息栏"智狐能做的事"：每一行都点开、每个"技术细节"都展开、"这段对话用不了的"也展开（查翻译、查文字有没有冲出去时用）。"""
    open_side(page)
    if page.js("document.getElementById('caps-rest-toggle').getAttribute('aria-expanded') !== 'true' && !document.getElementById('caps-rest').hidden"):
        page.js("document.getElementById('caps-rest-toggle').click()")
    page.js("document.querySelectorAll('#caps .caps-head[aria-expanded=false]').forEach((head) => head.click())")
    page.js("document.querySelectorAll('#caps details.caps-tech:not([open])').forEach((tech) => { tech.open = true; })")
    idle(page)


def pick(page: Page, select: str, value: str) -> None:
    """在一个下拉框（select.field）里选 value，和用户选了一样发出 change。不点开它：无界面的 Chrome 里原生的下拉列表点不了。"""
    page.js(f"(() => {{ const box = document.querySelector({json.dumps(select)}); box.value = {json.dumps(value)}; box.dispatchEvent(new Event('change', {{ bubbles: true }})); }})()")


def voice_local(page: Page) -> None:
    """设置 → 语音转文字：选"你自己运行的兼容服务"，等地址、模型名那几格出来（还没保存：关上设置再打开就回到原来的选择）。"""
    pick(page, "#voice-provider", "local")
    page.wait("!document.getElementById('voice-local').hidden && document.activeElement.id === 'voice-url'", what="兼容服务的那几格出来")
    idle(page)
