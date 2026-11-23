from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .browser.session import Facts as BrowserFacts
from .capabilities_approval import Probe, _approval
from .capabilities_context import _Context, _item, _missing, _sample
from .capabilities_kinds import Action, Approval, Capability, technical_names
from .channels import platforms
from .desktop import backend as desktop_backend
from .desktop.keys import DANGEROUS
from .desktop.session import Facts as DesktopFacts
from .i18n import k, t
from .providers import caps
from .tools import send as send_tool
from .tools.base import Tool
from .tools.browser import TOOLSET as BROWSER
from .tools.browser import BrowserTool
from .tools.computer import TOOLSET as COMPUTER
from .tools.computer import DesktopTool
from .tools.fetch import TOOL_NAME as FETCH
from .tools.fetch import FetchFacts, FetchTool
from .tools.send import SendFacts, SendTool

# 这台电脑上做不了"操作整台电脑"的原因（和页面设置里那一节的说法一样，见 page/scripts/computer.js）
_NO_DESKTOP = {
    desktop_backend.WAYLAND: k("这台电脑用的是 Wayland 桌面：Wayland 不允许一个程序截别的程序的图、替别人点鼠标，所以用不了。登录时换成 X11（Xorg）桌面就可以。"),
    desktop_backend.NO_DISPLAY: k("这台电脑没有图形界面（比如在服务器上），没有屏幕可以操作。"),
    desktop_backend.WINDOWS: k("Windows 上暂时不支持操作整台电脑：现在只能在 macOS 和 Linux（X11 桌面）上用。"),
    desktop_backend.PLATFORM: k("只能在 macOS 和 Linux（X11 桌面）上用，这台电脑的系统不支持。"),
}


def _web(context: _Context) -> Capability:
    """联网：Claude 开着联网搜索时是 Anthropic 服务器上的搜索和读网页；别的时候（别家的模型、HARNESS_WEB=0）是智狐自己的读网页
    （tools/fetch.py：只读你给的网址，不能搜索），设置里能开关。两种不会同时在一段对话里（见 Backend.sends）。"""
    here = context.here("web")
    provider = context.provider
    if here and all(tool.run is None for tool in here):
        where = {"where": "provider", "where_text": t("在 {provider} 的服务器上执行", provider="Anthropic")}
        rules = [context.rule(k("联网搜索、读网页"), [Probe("web_search", {"query": "…"})], k("搜索、读网页直接做"), k("搜索前会问你"))]
        return _item("web", "on", approval=_approval(rules), tools=technical_names(here), **where)
    searches = provider.web and context.app.config.web  # 这家模型本来能在服务器上搜索（那就不会走到下面）
    if not here and searches:
        return _missing(context, "web", _sample("web_search"), _sample(FETCH))
    if not here and context.blocked(_sample(FETCH)):
        return _item("web", "unavailable", context.persona_reason())
    return _fetch(context, here, provider)


def _fetch(context: _Context, here: list[Tool], provider: Any) -> Capability:
    """智狐自己的读网页：设置里的开关、这段对话里有没有（开始时定下），合起来是哪种状态（和浏览器一样，见 _switched）。"""

    def approval() -> Approval:
        def look(facts: FetchFacts) -> Probe:
            return Probe(FETCH, {"url": facts.url}, FetchTool(spec={"name": FETCH}, stands_in_for=FETCH, facts=lambda _: facts))

        known = [look(FetchFacts(url="https://example.com/", site="https://example.com"))]
        new = [look(FetchFacts(url="https://example.com/", site="https://example.com", new_site=True))]
        rules = [
            context.rule(k("读这段对话里读过的网站"), known, k("读过的网站直接读"), k("读网页前会问你")),
            context.rule(k("读一个新的网站"), new, k("新网站直接读"), k("每个新网站第一次读之前会问你")),
        ]
        note = k("这台电脑、局域网里的地址一律不读；网页里的脚本不执行，不带 Cookie。")
        return _approval(rules, note=note)

    where = t("在这台电脑上，直接连那个网站（不经过浏览器）")
    item = _switched(context, "web", here, context.app.settings.web_fetch, "fetch", approval, where_text=where)
    if item.status == "on":
        if provider.web and not context.app.config.web:
            item.reason = t("联网搜索在启动时关掉了（HARNESS_WEB=0），只能读你给的网址。")
        else:
            item.reason = t("只能读你给的网址，不能搜索：只有 Claude 能联网搜索，现在用的是 {provider}。", provider=provider.label)
            item.actions.append(Action("models", "", t("换模型")))  # 要搜索的话：换成 Claude
    return item


def _switched(context: _Context, kind_id: str, here: list[Tool], enabled: bool, target: str, approval: Callable[[], Approval], **fields: Any) -> Capability:
    """浏览器和操作整台电脑：设置里的开关、这段对话里有没有（开始时定下），两样合起来是哪种状态。"""
    settings = Action("settings", target, t("在设置里打开"))
    if here and enabled:
        return _item(kind_id, "on", approval=approval(), tools=technical_names(here), actions=[Action("settings", target, t("设置"))], **fields)
    if here:
        reason = t("设置里关掉了。这段对话里它的工具还在，调用时会回答它关着。")
        return _item(kind_id, "off", reason, tools=technical_names(here), actions=[settings], **fields)
    if enabled:
        return _item(kind_id, "pending", t("设置里已经打开，下一段对话开始生效。"), actions=[Action("new", "", t("开始新对话"))], **fields)
    if context.unattended and kind_id == "computer" and context.app.desktop.enabled:
        return _item(kind_id, "off", t("设置里没有允许定时任务和聊天通道使用它。"), actions=[settings], **fields)
    reason = t("设置里关着。打开以后，下一段对话开始可以用。") if context.started else t("设置里关着。打开以后这段对话就能用。")
    return _item(kind_id, "off", reason, actions=[settings], **fields)


def _browser(context: _Context) -> Capability:
    here = context.here("browser")
    sample = BrowserTool(spec={"name": "browser_probe"}, stands_in_for=BROWSER)
    if not here and context.blocked(sample):
        return _item("browser", "unavailable", context.persona_reason())

    def approval() -> Approval:
        def look(facts: BrowserFacts) -> Probe:
            return Probe(BROWSER, {}, BrowserTool(spec={"name": "browser_probe"}, stands_in_for=BROWSER, facts=lambda _: facts))

        reading = [look(BrowserFacts("other"))]
        acting = [
            look(BrowserFacts("navigate", destination="https://example.com/", new_site=True)),
            look(BrowserFacts("type", found=True, editable=True, role="textbox", name="q", text="…")),
            look(BrowserFacts("click", found=True, role="button", name="…", submits=True)),
        ]
        secret = [
            look(BrowserFacts("type", found=True, editable=True, role="textbox", password=True, text="…")),
            look(BrowserFacts("type", found=True, editable=True, role="textbox", card=True, text="…")),
        ]
        rules = [
            context.rule(k("看网页、截图、滚动"), reading, k("看网页、截图直接做"), k("看网页前会问你")),
            context.rule(k("去一个新的网站、输入文字、提交表单"), acting, k("去新网站、输入、提交直接做"), k("去新网站、输入、提交前会问你")),
            context.rule(k("往密码框、银行卡里输入"), secret, k("密码和银行卡直接输入"), k("密码和银行卡每次都问")),
        ]
        note = k("这段对话里允许过的网站，再去不再问。网页想下载的文件一律不下载。")
        return _approval(rules, note=note)

    where = t("在这台电脑上，智狐自己的 Chrome 里")
    return _switched(context, "browser", here, context.app.browser.enabled, "browser", approval, where_text=where)


def _computer(context: _Context) -> Capability:
    here = context.here("computer")
    provider = context.provider
    found, why = context.app.desktop.backend()
    if found is None:
        # 设置里开着的话，工具照样发给了模型（调用时回答"这台电脑上用不了"），技术细节里照样列出来
        return _item("computer", "unavailable", t(_NO_DESKTOP.get(why, _NO_DESKTOP[desktop_backend.PLATFORM])), tools=technical_names(here))
    if provider.kind != "anthropic" and not caps(provider, context.agent.backend.model).vision:
        reason = t("{provider} 看不了图片，用不了这个功能。", provider=provider.label)
        return _item("computer", "unavailable", reason, actions=[Action("models", "", t("换模型"))])
    sample = DesktopTool(spec={"name": "computer_probe"}, stands_in_for=COMPUTER)
    if not here and context.blocked(sample):
        return _item("computer", "unavailable", context.persona_reason())

    def approval() -> Approval:
        attended = not context.unattended
        looks, acts = context.trusted

        def look(action: str, trusted: bool = False, **fields: Any) -> Probe:
            facts = DesktopFacts(action, trusted=trusted, attended=attended, **fields)
            return Probe(COMPUTER, {}, DesktopTool(spec={"name": "computer_probe"}, stands_in_for=COMPUTER, facts=lambda _: facts))

        # 第一条是点击、打字：一句话的总结先说它（"点击、打字直接做；危险的组合键每次都问"）。危险的组合键单独一条：
        # 不管选了哪一档、放开了什么都要问，所以这里永远不会是"都直接做"
        acting = [look("click", acts, point=(10, 10)), look("type", acts, text="…")]
        danger = [look("key", acts, text="cmd+q", danger=DANGEROUS[0][3])]
        rules = [
            context.rule(k("点鼠标、打字、按键"), acting, k("点击、打字不再问（这段对话里）") if acts else k("点击、打字直接做"), k("点击、打字每一步都问")),
            context.rule(k("截图"), [look("screenshot", looks)], k("截图不再问（这段对话里）") if looks else k("截图直接做"), k("截图每次都问")),
            context.rule(k("危险的组合键（退出程序、退出登录……）"), danger, "", k("危险的组合键每次都问")),
        ]
        if attended:
            note = k(
                "确认卡片上可以选这段对话里截图不再问、点击和打字不再问。不再问以后，屏幕上的任何文字都可能带着它去点、去打字；"
                "页面顶上的“停止”随时能叫停。只是等一等、看鼠标在哪儿的不问。"
            )
        else:
            note = k("定时任务和聊天通道按它自己的那一档问，没有“这段对话里不再问”。只是等一等、看鼠标在哪儿的不问。")
        return _approval(rules, all_ask=k("每一步都先问你"), note=note)

    enabled = context.app.desktop.unattended if context.unattended else context.app.desktop.enabled
    return _switched(context, "computer", here, enabled, "computer", approval)


def _send(context: _Context) -> Capability:
    here = context.here("send")
    channels = [Action("extensions", "channels", t("设置聊天通道"))]
    if not here:
        if context.channel and context.blocked(SendTool(spec={"name": send_tool.TOOL_NAME})):
            return _item("send", "unavailable", context.persona_reason())
        return _item("send", "unavailable", t("只有在聊天通道（Telegram、飞书……）里聊天时才有。"), actions=channels)
    if not context.in_chat:
        reason = t("这段会话是在 {platform} 里开始的。在网页上接着聊时发不出去，文件就在这台电脑上。", platform=platforms.label(context.channel))
        return _item("send", "unavailable", reason, tools=technical_names(here))
    workspace: Path = context.permissions.editor.workspace
    facts = SendFacts(workspace / "zhifox-probe.txt", "zhifox-probe.txt", 1, kind="document", label="Telegram", person="…")
    probe = Probe(send_tool.TOOL_NAME, {"path": "zhifox-probe.txt"}, SendTool(spec={"name": send_tool.TOOL_NAME}, facts=lambda _: facts))
    rules = [context.rule(k("把一个文件发到聊天里"), [probe], k("发文件直接做"), k("每次发之前都先问你"))]
    note = k("保存密钥的文件（.env 这类）在任何一档下都不发。")
    return _item("send", "on", approval=_approval(rules, note=note), tools=technical_names(here))
