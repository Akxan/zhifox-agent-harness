"""浏览器的"问不问你"（permissions.py 的浏览器一节）。

规矩（为什么这样定见 permissions.py 开头）：
  - 读页面、截图、滚动、换标签页不问；
  - 输入文字、点提交表单的按钮、去一个这段对话里没去过的网站，默认要问；
  - 往密码框、银行卡、看不到里面的别家网站的框里输入，打开不是网页的地址：在任何模式下都要问，卡片上带警告；
  - file:、chrome:、devtools:、javascript: 这类地址根本不打开（不问，直接告诉模型）；
  - "允许过的网站"只在这段对话里算数。

这里直接给 Facts（"这一步碰的是什么"），测的是规则本身；Facts 是怎么从真的网页上看出来的，见 test_browser_live.py。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.browser.manager import BrowserManager
from harness.browser.session import Facts
from harness.permissions import Permissions, consequential, review_browser
from harness.tools.browser import BrowserTool
from harness.tools.files import FileEditor


@pytest.fixture
def permissions(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    return Permissions(FileEditor(workspace, tmp_path / "backups"))


def tool(facts: Facts) -> BrowserTool:
    return BrowserTool(spec={"type": "browser_toolset_20260801"}, run=lambda tool_input: "ok", toolset="browser", member=facts.action, facts=lambda tool_input: facts)


def asks(permissions: Permissions, facts: Facts) -> bool:
    question = permissions.review(facts.action, {}, tool(facts))
    return question is not None and not permissions.auto_allows(facts.action, {}, tool(facts))


PAGE = "https://shop.example/cart"


# ---- 不问的 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "facts",
    [
        Facts("other", page=PAGE),  # 读页面、截图、滚动……
        Facts("navigate", destination="back"),
        Facts("navigate", destination="https://shop.example/other", new_site=False, page=PAGE),
        Facts("click", page=PAGE, found=True, role="link", name="下一页"),
        Facts("click", page=PAGE, found=True, role="button", name="展开"),
        Facts("click", page=PAGE, stale=True),  # 编号作废了：工具会直接报错
        Facts("click", reachable=False, idle=True),  # 还没有网页：没什么可点
        Facts("type", reachable=False, idle=True),
        Facts("form", page=PAGE, found=True, role="combobox", name="尺码", text="L"),  # 选一个选项
        Facts("key", page=PAGE, found=True, role="link", text="Tab"),
        Facts("key", page=PAGE, found=True, editable=True, role="textbox", text="ArrowDown"),
        Facts("key", page=PAGE, found=True, role="link", text="Enter", enter=True),  # 在链接上回车 = 点链接
    ],
)
def test_reading_and_ordinary_clicks_do_not_ask(permissions, facts):
    assert review_browser(facts) is None
    assert permissions.review(facts.action, {}, tool(facts)) is None


# ---- 默认要问的 -------------------------------------------------------------------


def test_a_new_site_asks_and_says_what_allowing_means(permissions):
    facts = Facts("navigate", destination="https://evil.example/?q=secret", new_site=True, page=PAGE)
    question = review_browser(facts)
    assert "https://evil.example/?q=secret" in question and "https://evil.example" in question and "不再问" in question
    assert asks(permissions, facts)
    permissions.mode = "all"
    assert not asks(permissions, facts)  # 全部自动允许：新网站不再问


def test_a_local_address_is_pointed_out(permissions):
    question = review_browser(Facts("navigate", destination="http://192.168.1.1/admin", new_site=True))
    assert "局域网" in question


@pytest.mark.parametrize(
    "facts, word",
    [
        (Facts("click", page=PAGE, found=True, role="button", name="Go", submits=True, in_form=True), "Go"),
        (Facts("click", page=PAGE, found=True, role="button", name="立即购买"), "立即购买"),
        (Facts("click", page=PAGE, found=True, role="button", name="Send message"), "Send message"),
        (Facts("click", page=PAGE, found=True, role="link", name="Eliminar cuenta"), "Eliminar cuenta"),
    ],
)
def test_clicks_that_submit_or_commit_ask(permissions, facts, word):
    question = review_browser(facts)
    assert word in question and PAGE in question
    assert asks(permissions, facts)


def test_a_click_into_a_frame_from_another_site_asks(permissions):
    facts = Facts("click", page=PAGE, cross_origin=True, frame="https://pay.example/widget")
    assert "https://pay.example/widget" in review_browser(facts)


def test_a_click_we_could_not_look_at_asks(permissions):
    facts = Facts("click", reachable=False, text="(10, 20)")
    assert "(10, 20)" in review_browser(facts)


def test_typing_always_shows_what_and_where(permissions):
    facts = Facts("type", page=PAGE, found=True, editable=True, role="searchbox", name="搜索", text="蓝色雨伞")
    question = review_browser(facts)
    assert "蓝色雨伞" in question and "搜索" in question and PAGE in question
    assert asks(permissions, facts)
    permissions.mode = "edits"  # "改文件不用问"不放开浏览器
    assert asks(permissions, facts)
    permissions.mode = "all"
    assert not asks(permissions, facts)


def test_typing_and_pressing_enter_says_it_will_submit():
    facts = Facts("type", page=PAGE, found=True, editable=True, role="textbox", text="x", enter=True)
    assert "回车" in review_browser(facts)


def test_filling_a_text_field_asks_like_typing(permissions):
    facts = Facts("form", page=PAGE, found=True, editable=True, role="textbox", name="邮箱", text="me@example.com")
    assert "me@example.com" in review_browser(facts)


def test_keys_that_type_or_submit_ask(permissions):
    assert review_browser(Facts("key", page=PAGE, found=True, editable=True, role="textbox", text="a b c", typing_keys=True))
    assert review_browser(Facts("key", page=PAGE, found=False, text="e", typing_keys=True))  # 不在输入框里：可能是网页的快捷键
    assert review_browser(Facts("key", page=PAGE, found=True, editable=True, role="textbox", text="Enter", enter=True))
    assert review_browser(Facts("key", page=PAGE, found=True, role="button", name="Pay", submits=True, text="Enter", enter=True))


# ---- 任何模式下都要问的 ----------------------------------------------------------------


@pytest.mark.parametrize(
    "facts, warning",
    [
        (Facts("type", page=PAGE, found=True, editable=True, role="textbox", password=True, text="hunter2"), "密码框"),
        (Facts("form", page=PAGE, found=True, editable=True, role="textbox", card=True, text="4111 1111 1111 1111"), "银行卡"),
        (Facts("type", page=PAGE, cross_origin=True, frame="https://js.stripe.com/x", text="4242"), "另一个网站"),
        (Facts("key", page=PAGE, found=True, editable=True, password=True, text="h u n", typing_keys=True), "密码框"),
    ],
)
def test_passwords_cards_and_foreign_frames_ask_in_every_mode_with_a_warning(permissions, facts, warning):
    question = review_browser(facts)
    assert "⚠" in question and warning in question
    for mode in ("ask", "edits", "all"):
        permissions.mode = mode
        assert asks(permissions, facts), mode
    assert asks(permissions.unattended("all"), facts)  # 定时任务自己选了"全部自动允许"也一样


def test_a_password_is_never_shown_on_the_card():
    question = review_browser(Facts("type", page=PAGE, found=True, editable=True, password=True, text="hunter2"))
    assert "hunter2" not in question and "7 个字" in question


@pytest.mark.parametrize("address", ["about:blank", "data:text/html,<b>hi</b>", "mailto:someone@example.com"])
def test_addresses_that_are_not_web_pages_ask_every_time_in_every_mode(permissions, address):
    facts = Facts("navigate", destination=address)
    assert address in review_browser(facts)
    permissions.mode = "all"
    assert asks(permissions, facts)


@pytest.mark.parametrize("address", ["file:///etc/passwd", "chrome://settings", "devtools://devtools/x", "javascript:alert(1)", "view-source:https://a.example"])
def test_refused_addresses_are_not_asked_about_because_they_never_open(address):
    # 根本不会打开（工具会直接拒绝，见 test_browser_unit.py 的 urls 那一节），没有必要先打扰你
    assert review_browser(Facts("navigate", destination=address)) is None


def test_consequential_words_cover_the_three_languages():
    for name in ("Buy now", "立即支付", "Comprar ahora", "Delete account", "提交订单"):
        assert consequential(name)
    for name in ("Next", "下一页", "Siguiente", "Show more"):
        assert not consequential(name)


# ---- "允许过的网站"只在这段对话里算数 -------------------------------------------------------


def test_allowed_sites_belong_to_one_conversation(tmp_path):
    manager = BrowserManager(tmp_path / "browser", SimpleNamespace(browser={"enabled": True, "headless": True, "path": ""}))
    first, second = manager.session("chat"), manager.session("chat")
    assert first.facts("navigate", url="https://news.example/a").new_site
    first.allowed_sites.add("https://news.example")  # 这段对话里去过了（见 session._do_navigate）
    assert not first.facts("navigate", url="https://www.news.example/b").new_site  # www. 算同一个网站
    assert first.facts("navigate", url="https://docs.news.example/").new_site  # 子域名不算
    assert second.facts("navigate", url="https://news.example/a").new_site  # 另一段对话：从头再问
    assert not Path(tmp_path / "browser" / "profile").exists()  # 只是看一眼，没有为此打开浏览器
