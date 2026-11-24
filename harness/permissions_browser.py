"""权限 · 浏览器：一次动作要不要问你、问的话卡片上写什么（规矩和为什么见 permissions.py 开头的表和"浏览器的规矩"那一段）。

  review_browser   入口：打开网址、点击、输入、按键各怎么问；不用问返回 None
  _always_ask      在任何模式下都要问的那几样（密码框、银行卡、别的网站嵌进来的框、不是网页的地址）：allowance 也用它
  CONSEQUENTIAL、consequential               名字像"购买""发送""删除"的按钮，点之前也问
  ROLES、_role、_element、_page、_warning     卡片上的几段话

为什么单独一个文件：浏览器的规矩最多，自成一块；它看的是 BrowserSession 动手之前交来的 Facts（browser/session_facts.py），
和别的工具没有关系。
"""

from __future__ import annotations

from .browser import urls
from .browser.session import Facts
from .i18n import k, t
from .permissions_cards import _preview

# ---- 浏览器 ------------------------------------------------------------------

# 名字里带这些词的按钮，点之前也问：买东西、付钱、发消息、删东西、同意条款，都是点一下就收不回来的。
# 只是补充：真正靠得住的是"会不会提交表单"（Facts.submits），很多这样的按钮不在表单里，就靠名字认
CONSEQUENTIAL = (
    "buy", "purchase", "pay", "checkout", "order", "send", "submit", "delete", "remove", "confirm", "transfer",
    "subscribe", "sign up", "register", "agree", "accept", "post", "publish",
    "购买", "支付", "付款", "下单", "结算", "发送", "提交", "删除", "确认", "转账", "订阅", "注册", "同意", "发布",
    "comprar", "pagar", "enviar", "eliminar", "borrar", "confirmar", "suscribir", "aceptar", "publicar",
)


def consequential(name: str) -> bool:
    lowered = name.lower()
    return any(word in lowered for word in CONSEQUENTIAL)


# 页面元素的角色（ARIA 的叫法）→ 给人看的说法
ROLES = {
    "button": k("按钮"), "link": k("链接"), "textbox": k("输入框"), "searchbox": k("搜索框"), "combobox": k("下拉框"),
    "listbox": k("列表框"), "checkbox": k("勾选框"), "radio": k("单选项"), "switch": k("开关"), "tab": k("标签"),
    "menuitem": k("菜单项"), "option": k("选项"), "slider": k("滑块"), "spinbutton": k("数字框"),
}


def _role(facts: Facts) -> str:
    return t(ROLES[facts.role]) if facts.role in ROLES else facts.role or "?"


def _always_ask(facts: Facts) -> bool:
    """在任何模式下都要问的：往密码框、银行卡、看不到里面的别家网站的框里输入；打开不是网页的地址。"""
    if facts.action in ("type", "form", "key") and (facts.password or facts.card or facts.cross_origin):
        return facts.action != "key" or facts.typing_keys
    if facts.action == "navigate" and facts.destination and facts.destination not in urls.HISTORY:
        return urls.scheme_of(facts.destination) not in urls.WEB_SCHEMES
    return False


def _element(facts: Facts) -> str:
    """要操作的那个元素，给人看的说法。"""
    if facts.cross_origin:
        return t("另一个网站嵌在页面里的框（看不到里面是什么）：{frame}", frame=facts.frame or "?")
    if not facts.found:
        return t("（没有选中输入框）")
    return f"{_role(facts)} “{facts.name}”" if facts.name else _role(facts)


def _page(facts: Facts) -> str:
    return urls.shown(facts.page) if facts.page else t("（还没有打开网页）")


def _warning(facts: Facts) -> str:
    if facts.password:
        return t("⚠ 这是一个密码框。只有在你确定这是你要登录的网站、而且是你让助手这么做的时候才允许。")
    if facts.card:
        return t("⚠ 这里看起来是填银行卡信息的地方（卡号、安全码、有效期）。确认这是你要付款的网站，再允许。")
    if facts.cross_origin:
        return t("⚠ 输入的地方是另一个网站嵌进来的框，智狐看不到它是什么（常见的是付款信息）。确认无误再允许。")
    return ""


def review_browser(facts: Facts) -> str | None:
    """浏览器的一次动作要不要问你（规矩见 permissions.py 开头的表）。返回要显示的说明，不用问返回 None。"""
    action = facts.action
    if action == "navigate":
        destination = facts.destination
        if not destination or destination in urls.HISTORY:
            return None  # 后退、前进、刷新：都是这个标签页去过的地方
        if urls.refusal(destination):
            return None  # 根本不会打开（工具会直接拒绝），不用问
        if urls.scheme_of(destination) not in urls.WEB_SCHEMES:
            return t("在浏览器里打开一个不是网页的地址（每次都会问）：\n\n    {url}", url=urls.shown(destination))
        if not facts.new_site:
            return None
        local = t("\n\n注意：这是这台电脑或者局域网里的地址。") if urls.is_local(destination) else ""
        return t(
            "在浏览器里打开另一个网站：\n\n    {url}\n\n这段对话里还没去过 {site}。允许以后，这段对话里再去这个网站就不再问。{local}",
            url=urls.shown(destination),
            site=urls.site_of(destination),
            local=local,
        )
    if facts.idle:
        return None  # 浏览器没开、还没有网页：点不了也输入不了，工具会直接报错
    if action == "click":
        if facts.stale:
            return None  # 编号已经作废：工具会直接报错，让模型重新读页面
        if facts.cross_origin:
            return t("点击网页上另一个网站嵌进来的框（看不到里面是什么，可能是付款或者登录的按钮）：\n\n  网页：{page}\n  框：{frame}", page=_page(facts), frame=facts.frame or "?")
        if not facts.reachable:
            return t("点击网页上的 {target}（没能看清点的是什么）。\n\n  网页：{page}", target=facts.text or "?", page=_page(facts))
        if facts.submits or (facts.role in ("button", "link") and consequential(facts.name)):
            return t("点击网页上的“{name}”（{role}）。这可能会提交表单、下单或者发出消息。\n\n  网页：{page}", name=facts.name or "?", role=_role(facts), page=_page(facts))
        return None
    if action in ("type", "form"):
        if action == "form" and facts.reachable and facts.found and not facts.editable and not facts.password and not facts.card:
            return None  # 选一个下拉框的选项、勾一个勾选框：只是改了表单里的一项，真要提交时还会问
        if facts.stale:
            return None
        shown = "•" * min(len(facts.text), 12) + t("（共 {n} 个字，已隐藏）", n=len(facts.text)) if facts.password else _preview(facts.text, 8)
        warning = _warning(facts)
        enter = t("\n\n输完以后会按回车（通常就是提交）。") if facts.enter else ""
        return t(
            "在网页上输入文字：\n\n  网页：{page}\n  输入到：{element}\n  内容：\n{content}{enter}{warning}",
            page=_page(facts),
            element=_element(facts) if facts.reachable else t("（当前选中的地方）"),
            content=shown,
            enter=enter,
            warning="\n\n" + warning if warning else "",
        )
    if action == "key":
        if facts.typing_keys or (facts.enter and (facts.editable or facts.cross_origin or not facts.reachable)):
            # 会打出字、粘贴：在输入框里就是输入；不在输入框里，字母键可能触发网页的快捷键（"e" 归档邮件……）。
            # 在输入框里按回车：通常就是提交
            warning = _warning(facts)
            return t(
                "在网页上按键：{keys}\n\n  网页：{page}\n  当前选中的是：{element}{warning}",
                keys=facts.text,
                page=_page(facts),
                element=_element(facts),
                warning="\n\n" + warning if warning else "",
            )
        if facts.enter and (facts.submits or consequential(facts.name)):
            return t("在网页上按回车，会按下“{name}”（{role}），可能会提交表单。\n\n  网页：{page}", name=facts.name or "?", role=_role(facts), page=_page(facts))
        return None
    return None
