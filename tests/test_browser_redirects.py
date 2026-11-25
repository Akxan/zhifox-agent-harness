"""navigate 跟不跟跳转（harness/browser/redirects.py）：只跟到要去的那个网站、这段对话里允许过的网站和现在这一页的网站。

两条要守住的规矩：
  - 只有要去的那个地址的网站记进 allowed_sites，跳完落到的别的网站不算（就算拦不住，比如这个 Chrome 不认 Fetch 域）；
  - navigate 期间主框架要跳到一个没允许过的网站（服务器的 302、页面在加载完之前自己排好的跳转），拦下来不跟，告诉模型。
    每一个停下来的请求都要有答复，navigate 一结束就关掉 Fetch 域。

前半是 Guard 自己（不用 Chrome，读线程那一头用一个只会登记、触发事件的假连接）；后半对着 tests/fake_chrome.py 的 Fetch 域，
经过真正的 BrowserManager、BrowserSession。真的 Chrome 的在 test_browser_live.py。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import test_browser_unit
from test_browser_unit import wait_until

from harness.browser import redirects
from harness.browser.cdp import Cancelled
from harness.browser.redirects import Guard
from harness.tools import ToolError

MAIN, TAB = "S-T", "T"  # 标签页的 sessionId、主框架的编号
browser = test_browser_unit.browser  # 那边的 fixture（假 Chrome 的 BrowserManager）：放进这个模块，pytest 就认得


class Wires:
    """扮演 Connection 的 on()：记下谁在听什么，测试用 fire() 当作读线程交来一个事件。"""

    def __init__(self):
        self.listeners: dict[str, list] = {}

    def on(self, method, listener):
        self.listeners.setdefault(method, []).append(listener)
        return lambda: self.listeners[method].remove(listener)

    def fire(self, method: str, session: str = MAIN, **params) -> None:
        for listener in list(self.listeners.get(method, [])):
            listener({"frameId": TAB, **params}, session)


def paused(url: str, session: str = MAIN, frame: str = TAB, kind: str = "Document", redirected: str = "") -> tuple[dict, str]:
    params = {"requestId": "job-1", "request": {"url": url}, "frameId": frame, "resourceType": kind}
    if redirected:
        params["redirectedRequestId"] = redirected
    return params, session


def guard(*sites: str) -> Guard:
    return Guard(MAIN, TAB, {"https://a.example", *sites})


# ---- Guard：放不放 ------------------------------------------------------------


def test_the_site_being_opened_allowed_sites_and_the_current_page_pass():
    keeper = Guard(MAIN, TAB, {"https://a.example", "https://b.example", ""})
    assert keeper.allows(*paused("https://a.example/x", redirected="job-0"))
    assert keeper.allows(*paused("https://www.a.example/y", redirected="job-0"))  # www. 算同一个网站
    assert keeper.allows(*paused("https://b.example/", redirected="job-0"))
    assert keeper.refused == ""


def test_a_hop_to_any_other_site_is_refused_and_the_first_one_is_remembered():
    keeper = guard()
    assert not keeper.allows(*paused("https://evil.example/?data=mail", redirected="job-0"))
    assert not keeper.allows(*paused("https://docs.a.example/", redirected="job-0"))  # 子域名是另一个网站
    assert not keeper.allows(*paused("http://a.example/"))  # 换成明文也是
    assert keeper.refused == "https://evil.example/?data=mail"


def test_other_tabs_frames_inside_the_page_and_other_kinds_of_request_pass():
    keeper = guard()
    assert keeper.allows(*paused("https://evil.example/", session="S-OTHER"))
    assert keeper.allows(*paused("https://evil.example/", frame="IFRAME"))  # 页面里嵌的框：是页面自己的东西，和点链接一样
    assert keeper.allows(*paused("https://evil.example/", kind="Script"))
    assert keeper.allows(*paused("about:blank"))  # 不是网页的地址：没有网站可去
    assert keeper.refused == ""


def test_the_first_request_while_opening_is_the_address_itself_as_chrome_wrote_it():
    """Chrome 把网址整理过（IDN 换成 xn--、去掉默认端口……）：第一个请求就是要去的那个地址，不管它写成什么样都放，
    它的网站也算这次 navigate 要去的网站（跟着它在同一个网站里跳照常跟）。"""
    keeper = Guard(MAIN, TAB, {"https://例子.example"})
    with keeper.opening():
        assert keeper.allows(*paused("https://xn--fsqu00a.example/"))
        assert not keeper.allows(*paused("https://evil.example/", redirected="job-1"))  # 第一个之后照常查
    assert keeper.opened == "https://xn--fsqu00a.example"
    assert keeper.allows(*paused("https://xn--fsqu00a.example/next", redirected="job-1"))


def test_outside_opening_a_fresh_request_is_judged_like_any_other():
    keeper = guard()
    with keeper.opening():
        pass  # 同一页里的跳转（只改了 # 后面的部分）：Chrome 不发请求，回答以后才来的请求是页面自己发起的
    assert not keeper.allows(*paused("https://evil.example/"))
    assert keeper.opened == ""


# ---- Guard：等页面自己排好的跳转 -------------------------------------------------------


def listening(keeper: Guard) -> Wires:
    wires = Wires()
    keeper.listen(wires)
    return wires


def test_a_quiet_page_settles_at_once():
    keeper = guard()
    wires = listening(keeper)
    wires.fire("Page.frameStartedLoading")
    wires.fire("Page.frameStoppedLoading")
    started = time.monotonic()
    assert keeper.settle(time.monotonic() + 5, threading.Event()) is True
    assert time.monotonic() - started < 0.5


def test_a_navigation_scheduled_before_the_page_finished_is_waited_for():
    keeper = guard()
    wires = listening(keeper)
    wires.fire("Page.frameStartedLoading")
    wires.fire("Page.frameScheduledNavigation", delay=0, url="https://a.example/next")
    wires.fire("Page.frameStoppedLoading")
    done: list[bool] = []
    waiter = threading.Thread(target=lambda: done.append(keeper.settle(time.monotonic() + 5, threading.Event())))
    waiter.start()
    time.sleep(0.1)
    assert done == []  # 排好的跳转还没开始：接着等
    wires.fire("Page.frameStartedLoading")
    time.sleep(0.1)
    assert done == []  # 开始了，还没加载完
    wires.fire("Page.frameStoppedLoading")
    waiter.join(2)
    assert done == [True]


def test_events_from_other_frames_or_tabs_do_not_count():
    keeper = guard()
    wires = listening(keeper)
    wires.fire("Page.frameStartedLoading", session="S-OTHER")
    wires.fire("Page.frameScheduledNavigation", session="S-OTHER", delay=0)
    for listener in wires.listeners["Page.frameScheduledNavigation"]:
        listener({"frameId": "IFRAME", "delay": 0}, MAIN)
    assert keeper.settle(time.monotonic() + 5, threading.Event()) is True


def test_a_cancelled_or_far_off_schedule_is_not_waited_for():
    keeper = guard()
    wires = listening(keeper)
    wires.fire("Page.frameScheduledNavigation", delay=redirects.BROWSER_REDIRECT_WAIT + 5)  # 过几秒才跳：不等（navigate 返回以后的事）
    assert keeper.settle(time.monotonic() + 5, threading.Event()) is True
    wires.fire("Page.frameScheduledNavigation", delay=0)
    wires.fire("Page.frameClearedScheduledNavigation")  # 页面又取消了
    assert keeper.settle(time.monotonic() + 5, threading.Event()) is True


def test_a_requested_navigation_into_a_new_tab_is_not_waited_for():
    keeper = guard()
    wires = listening(keeper)
    wires.fire("Page.frameRequestedNavigation", url="https://a.example/", disposition="newTab")
    assert keeper.settle(time.monotonic() + 5, threading.Event()) is True


def test_a_schedule_that_never_starts_is_given_up_after_the_wait(monkeypatch):
    monkeypatch.setattr(redirects, "BROWSER_REDIRECT_WAIT", 0.2)
    keeper = guard()
    wires = listening(keeper)
    wires.fire("Page.frameScheduledNavigation", delay=0)
    started = time.monotonic()
    assert keeper.settle(time.monotonic() + 5, threading.Event()) is True
    assert 0.15 < time.monotonic() - started < 2


def test_a_page_that_keeps_reloading_itself_is_followed_only_a_few_times(monkeypatch):
    keeper = guard()
    wires = listening(keeper)
    for _ in range(redirects.BROWSER_REDIRECT_HOPS):
        wires.fire("Page.frameScheduledNavigation", delay=0)
        wires.fire("Page.frameStartedLoading")
        wires.fire("Page.frameStoppedLoading")
    wires.fire("Page.frameScheduledNavigation", delay=0)  # 第 HOPS+1 次：不再等
    started = time.monotonic()
    assert keeper.settle(time.monotonic() + 5, threading.Event()) is True
    assert time.monotonic() - started < 0.5


def test_settle_gives_up_at_the_deadline_and_wakes_up_when_stopped():
    keeper = guard()
    wires = listening(keeper)
    wires.fire("Page.frameStartedLoading")  # 一直在加载
    assert keeper.settle(time.monotonic() + 0.1, threading.Event()) is False
    halt = threading.Event()
    with ThreadPoolExecutor(1) as pool:
        waiting = pool.submit(keeper.settle, time.monotonic() + 30, halt)
        time.sleep(0.1)
        started = time.monotonic()
        halt.set()
        keeper.wake()
        with pytest.raises(Cancelled):
            waiting.result(3)
        assert time.monotonic() - started < 1


def test_unlisten_stops_hearing_events():
    keeper = guard()
    wires = listening(keeper)
    keeper.unlisten()
    assert all(not listeners for listeners in wires.listeners.values())


# ---- 对着假的 Chrome：navigate 整个走一遍 -----------------------------------------------


def calls(rig, method: str) -> list[dict]:
    return [message for message in rig.sent() if message["method"] == method]


def test_a_redirect_to_a_site_never_allowed_is_not_followed_or_remembered(browser):
    rig = browser(redirects={"https://a.example/go": "https://evil.example/?data=secret"})
    session = rig.manager.session("对话")
    outcome = session.run("navigate", url="https://a.example/go")
    assert "网站想跳到 https://evil.example/?data=secret，没有跟过去" in outcome.text
    assert "这段对话里还没去过 https://evil.example" in outcome.text
    assert outcome.state["tabs"][0]["url"] == "about:blank"  # 标签页留在原来那一页，没有一张写着那个网址的错误页
    assert session.allowed_sites == {"https://a.example"}
    assert [message["params"]["errorReason"] for message in calls(rig, "Fetch.failRequest")] == ["Aborted"]
    enabled = calls(rig, "Fetch.enable")
    assert [message["params"] for message in enabled] == [{"patterns": [{"resourceType": "Document", "requestStage": "Request"}]}]
    assert enabled[0]["sessionId"] == session.tabs["tab-1"].session
    assert len(calls(rig, "Fetch.disable")) == 1
    assert session.facts("navigate", url="https://evil.example/?data=secret").new_site is True  # 模型自己再去：照常问


def test_an_open_redirect_on_an_allowed_site_cannot_carry_a_made_up_address_away(browser):
    """允许过 a.example 以后，navigate 到它的"开放跳转"本身不用问；跳到 evil.example 那一下拦住。"""
    rig = browser(redirects={"https://a.example/redirect?to=evil": "https://evil.example/?data=mail"})
    session = rig.manager.session("对话")
    session.run("navigate", url="https://a.example/")
    assert session.facts("navigate", url="https://a.example/redirect?to=evil").new_site is False
    outcome = session.run("navigate", url="https://a.example/redirect?to=evil")
    assert "没有跟过去" in outcome.text and "标签页还在 https://a.example/" in outcome.text
    assert outcome.state["tabs"][0]["url"] == "https://a.example/"
    assert "https://evil.example" not in session.allowed_sites


def test_redirects_within_the_site_or_to_an_allowed_site_are_followed(browser):
    rig = browser(redirects={"https://a.example/old": "https://www.a.example/new", "https://a.example/out": "https://b.example/in"})
    session = rig.manager.session("对话")
    session.allowed_sites.add("https://b.example")  # 这段对话里早先允许过
    assert session.run("navigate", url="https://a.example/old").text == "已打开 https://www.a.example/new"
    assert session.run("navigate", url="https://a.example/out").text == "已打开 https://b.example/in"
    assert session.allowed_sites == {"https://a.example", "https://b.example"}
    assert calls(rig, "Fetch.failRequest") == []
    assert len(calls(rig, "Fetch.enable")) == len(calls(rig, "Fetch.disable")) == 2


def test_a_redirect_back_to_the_site_of_the_current_page_is_followed(browser):
    """直接 navigate 过去也不用问的网站（现在这一页的，见 session_facts.py 的 new_site），跳过去也照常跟。"""
    rig = browser(redirects={"https://a.example/back": "https://c.example/page"})
    session = rig.manager.session("对话")
    session.run("navigate", url="https://c.example/")
    session.allowed_sites.clear()  # 假装是点链接到的 c.example（没记进允许过的网站）
    session.allowed_sites.add("https://a.example")
    assert session.run("navigate", url="https://a.example/back").text == "已打开 https://c.example/page"


def test_a_page_sending_itself_to_another_site_while_loading_is_stopped(browser):
    rig = browser(page_redirects={"https://a.example/": "https://evil.example/?data=x"})
    session = rig.manager.session("对话")
    outcome = session.run("navigate", url="https://a.example/")
    lines = outcome.text.splitlines()
    assert lines[0] == "已打开 https://a.example/"
    assert lines[1].startswith("网站想跳到 https://evil.example/?data=x，没有跟过去")
    assert outcome.state["tabs"][0]["url"] == "https://a.example/"
    assert session.allowed_sites == {"https://a.example"}


def test_a_page_sending_itself_within_the_site_is_followed_to_where_it_lands(browser):
    rig = browser(page_redirects={"https://a.example/": "https://a.example/home"})
    session = rig.manager.session("对话")
    assert session.run("navigate", url="https://a.example/").text == "已打开 https://a.example/home"


def test_without_the_fetch_domain_the_landing_site_is_still_not_remembered(browser):
    """这个 Chrome 不认 Fetch 域（或者打开它出了错）：跳转拦不住，照常打开；可落到的网站不算允许过。"""
    rig = browser(
        errors={"Fetch.enable": {"code": -32601, "message": "'Fetch.enable' wasn't found"}},
        redirects={"https://a.example/go": "https://b.example/landing"},
    )
    session = rig.manager.session("对话")
    assert session.run("navigate", url="https://a.example/go").text == "已打开 https://b.example/landing"
    assert session.allowed_sites == {"https://a.example"}


def test_back_forward_and_reload_do_not_stop_anything(browser):
    stopped = [{"method": "Page.frameStoppedLoading", "params": {"frameId": "BLANK"}, "sessionId": "S-BLANK"}]
    history = {"currentIndex": 1, "entries": [{"id": 1, "url": "https://a.example/"}, {"id": 2, "url": "https://b.example/"}]}
    rig = browser(results={"Page.getNavigationHistory": history}, events={"Page.reload": stopped, "Page.navigateToHistoryEntry": stopped})
    session = rig.manager.session("对话")
    session.run("new_tab")
    session.run("navigate", url="reload")
    session.run("navigate", url="back")
    assert calls(rig, "Fetch.enable") == [] and calls(rig, "Fetch.disable") == []


def test_every_paused_request_is_answered_even_when_nobody_is_navigating(browser):
    """Fetch 关不掉的时候（Chrome 没回答 Fetch.disable）、别人的标签页：停下的请求也有人答，页面不会一直挂着。"""
    rig = browser()
    session = rig.manager.session("对话")
    session.run("new_tab")
    mine = session.tabs["tab-1"].session
    for request, cdp_session in (("lost-1", mine), ("lost-2", "S-NOBODY")):
        rig.manager.connection_if_running().call(
            "Fake.emit",
            {"events": [{"method": "Fetch.requestPaused", "params": {"requestId": request, "request": {"url": "https://evil.example/"}, "frameId": "BLANK", "resourceType": "Document"}, "sessionId": cdp_session}]},
            timeout=5,
        )
    wait_until(lambda: {message["params"]["requestId"] for message in calls(rig, "Fetch.continueRequest")} == {"lost-1", "lost-2"}, what="两个都放行")
    assert calls(rig, "Fetch.failRequest") == []


def test_the_fetch_domain_is_turned_off_when_navigate_fails_or_is_stopped(browser):
    rig = browser(errors={"Page.navigate": {"code": -32000, "message": "Cannot navigate to invalid URL"}})
    session = rig.manager.session("对话")
    with pytest.raises(ToolError, match="Cannot navigate"):
        session.run("navigate", url="https://a.example/")
    assert len(calls(rig, "Fetch.disable")) == 1
    assert session.allowed_sites == set()

    rig = browser(silent=["Page.navigate"])
    session = rig.manager.session("对话")
    with ThreadPoolExecutor(1) as pool:
        going = pool.submit(session.run, "navigate", url="https://a.example/")
        wait_until(lambda: calls(rig, "Page.navigate"), what="发出 Page.navigate")
        session.stop()
        with pytest.raises(ToolError, match="用户停下了"):
            going.result(3)
    assert len(calls(rig, "Fetch.disable")) == 1  # 停下了也关：不然这个标签页之后每一页都要停下来问一次
    # 守卫撤了：之后再停下的请求一律放行
    assert session.lets_through(*paused("https://evil.example/", session=session.tabs["tab-1"].session, frame=session.tabs["tab-1"].target)) is True
