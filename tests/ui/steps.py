"""在智狐的页面上常做的几步：发消息、等回复、换语言、打开会话栏和信息栏、打开某个窗口……

pilot.py 只会"点、按、读"，不知道页面长什么样；这里知道。页面的结构变了（换了 id、换了打开方式），
改这一处，所有测试都跟着对。写新测试时先看看这里有没有现成的一步。
"""

from __future__ import annotations

import json

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
    """用右上角的语言按钮换语言，等页面换好（<html lang> 变了）。"""
    page.click("#language")
    page.wait("document.getElementById('language-dialog').open", what="语言窗口打开")
    lang = {"zh": "zh-CN", "en": "en", "es": "es"}[code]
    page.click(f"#language-options .option[lang='{lang}']")
    page.wait(f"document.documentElement.lang === '{lang}' && !document.getElementById('language-dialog').open", what=f"换成 {code}")
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
    """打开"扩展"窗口里的一页（skills、mcp、channels、agents），等它画好。"""
    if not page.js(is_open("extensions")):
        open_dialog(page, "#extensions-open", "extensions")
    page.click(f"#tab-{tab}")
    page.wait(f"!document.getElementById('panel-{tab}').hidden && document.getElementById('panel-{tab}').children.length > 0", what=f"{tab} 这一页画好")
    idle(page)


def expand_capabilities(page: Page) -> None:
    """信息栏"智狐能做的事"：每一行都点开、每个"技术细节"都展开、"这段对话用不了的"也展开（查翻译、查文字有没有冲出去时用）。"""
    open_side(page)
    if page.js("document.getElementById('caps-rest-toggle').getAttribute('aria-expanded') !== 'true' && !document.getElementById('caps-rest').hidden"):
        page.js("document.getElementById('caps-rest-toggle').click()")
    page.js("document.querySelectorAll('#caps .caps-head[aria-expanded=false]').forEach((head) => head.click())")
    page.js("document.querySelectorAll('#caps details.caps-tech:not([open])').forEach((tech) => { tech.open = true; })")
    idle(page)
