"""装成应用：在真的 Chrome 里，页面的清单取得到、读得懂、没有错，Chrome 认为它能装成应用，清单里的图标都取得到、尺寸对；
安全策略只多放行了这几张图，别的图片照样拦下；应用窗口标题栏的颜色（theme-color）跟着外观面板里选的走。

用户在 Chrome 里给智狐做快捷方式，得到的是一块灰底上一个"智"字（见 harness/webui/manifest.py）。服务器那一边的检查在
tests/test_web_manifest.py；这里查的是"Chrome 自己怎么看"，用 DevTools 协议的 Page.getAppManifest 和
Page.getInstallabilityErrors：后者就是地址栏上要不要显示"安装"、"创建快捷方式"能不能装成应用的那套判断，
它会真的去下载清单里的图标（而且按页面的 img-src 检查：放行之前，它说的就是"找不到合适的图标"）。

隐身窗口里 Chrome 一律说"不能安装"，所以前两个测试的页面开在测试用 Chrome 的普通窗口里（默认的浏览器上下文），不用 page 那个
fixture（它开的是像隐身窗口一样的上下文）；页面里出的错照样一条都不能有。
"""

from __future__ import annotations

import contextlib
import json

import pytest

from . import steps
from .pilot import Page

ICONS_RESOLVE = """
(async (icons) => Promise.all(icons.map(async (icon) => {
  const reply = await fetch(icon.url);
  const bitmap = await createImageBitmap(await reply.blob());
  return [icon.url, reply.status, reply.headers.get('content-type'), bitmap.width + 'x' + bitmap.height, icon.sizes];
})))
"""


@pytest.fixture
def regular_page(demo, browser):
    """一个开在普通窗口里的标签页（不是隐身的），打开演示页面。测试结束时页面里不能有任何错误。"""
    demo.reset()
    connection = browser.connection
    target = connection.call("Target.createTarget", {"url": "about:blank"})["targetId"]
    session = connection.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
    opened = Page(connection, target, session)
    opened.demo = demo
    try:
        opened.goto(demo.url)
        opened.wait("document.body.classList.contains('ready')", what="页面亮相")
        yield opened
        errors = opened.unexpected_errors()
        assert errors == [], "页面里出了错（脚本异常、console.error 或者被安全策略拦下的东西）：\n" + "\n".join(errors)
    finally:
        with contextlib.suppress(Exception):
            opened.close()


def test_chrome_reads_the_manifest_and_can_install_the_page(regular_page):
    page, demo = regular_page, regular_page.demo
    reply = page.call("Page.getAppManifest", {}, timeout=20)
    assert reply["url"] == f"{demo.origin}/manifest.webmanifest?token={demo.token}"
    assert reply["errors"] == [], reply["errors"]
    parsed = reply["manifest"]
    assert parsed["name"] == "智狐"
    assert parsed["display"] == "kStandalone"
    assert parsed["startUrl"] == demo.url  # 从快捷方式打开的就是带着口令的那个地址
    assert parsed["id"] == demo.origin + "/"  # 不带口令：换了口令也还是同一个应用
    assert parsed["themeColor"] == parsed["backgroundColor"] == "rgba(238,236,231,1)"
    raw = json.loads(reply["data"])
    assert {(icon["sizes"], icon["purpose"]) for icon in raw["icons"]} >= {("512x512", "maskable"), ("192x192", "any")}

    # Chrome 自己的判断：能装成应用（它会去下载够大的图标；被安全策略拦下的话，这里是 no-acceptable-icon）
    installability = page.call("Page.getInstallabilityErrors", {}, timeout=30)["installabilityErrors"]
    assert installability == [], installability

    # 清单里的每张图标、页面头上的 apple-touch-icon 都取得到，是 PNG，尺寸和写的一样
    icons = parsed["icons"] + [{"url": page.js("document.querySelector('link[rel=apple-touch-icon]').href"), "sizes": "180x180"}]
    for url, status, kind, size, declared in page.js(f"({ICONS_RESOLVE})({json.dumps(icons)})"):
        assert (status, kind, size) == (200, "image/png", declared), url


def test_the_policy_still_blocks_every_other_image(regular_page):
    page = regular_page
    # 拦下的图片在控制台里报一条安全策略的错：这几条是故意的
    page.allow_errors("blocked-on-purpose")
    results = page.js(
        """
        (async () => {
          const violations = [];
          document.addEventListener('securitypolicyviolation', (event) => violations.push(event.violatedDirective + ' ' + event.blockedURI));
          const load = (src) => new Promise((resolve) => {
            const image = new Image();
            image.onload = () => resolve(['loaded', image.naturalWidth]);
            image.onerror = () => resolve(['blocked', 0]);
            image.src = src;
          });
          const token = new URLSearchParams(location.search).get('token');
          const outcome = {
            icon: await load('/icons/icon-32.png?token=' + token),
            page: await load('/?blocked-on-purpose&token=' + token),
            events: await load('/events?blocked-on-purpose&token=' + token),
            elsewhere: await load('http://example.invalid/blocked-on-purpose.png'),
            otherPort: await load('http://127.0.0.1:1/icons/icon-32.png?blocked-on-purpose'),
          };
          await new Promise((resolve) => setTimeout(resolve, 50));  // 违反策略的事件是异步发的
          return { outcome, violations };
        })()
        """
    )
    outcome = results["outcome"]
    assert outcome["icon"] == ["loaded", 32]  # 自带的图标放行
    for name in ("page", "events", "elsewhere", "otherPort"):  # 同一个服务器上的别的地址、别的网站、别的端口都拦下
        assert outcome[name] == ["blocked", 0], name
    assert len(results["violations"]) == 4 and all(line.startswith("img-src") for line in results["violations"]), results


def test_the_title_bar_color_follows_the_chosen_appearance(page):
    # 装成应用以后，窗口的标题栏是 theme-color 的颜色。默认浅色，系统是深色也一样；选了深色就跟着变深
    color = "document.querySelector('meta[name=theme-color]').content"
    page.emulate(prefers_color_scheme="dark")
    page.reload()
    page.wait("document.body.classList.contains('ready')")
    assert page.js(color) == "#eeece7"
    page.click("#theme")
    page.click("[data-theme-pick='dark']")
    page.wait("document.documentElement.dataset.theme === 'dark'", what="换成深色")
    # 就是深色外观的窗口底色（--bg，颜色只写在 tokens.css 里）
    page.wait(f"{color} === '#0b0c0f'", what="标题栏变成深色的底色")
    assert page.js(color) == page.js("getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()")
    # 跟随系统：系统是深色，还是深色；系统换成浅色，标题栏跟着变回来
    page.click("[data-theme-pick='auto']")
    page.emulate(prefers_color_scheme="light")
    page.wait(f"{color} === '#eeece7'", what="跟着系统变回浅色")
    page.key("Escape")
    steps.idle(page)
