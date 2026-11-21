"""智狐能做的事（harness/capabilities.py）：把发给模型的工具，换成给人看的"功能"清单。

信息栏里的那一节、终端里的 /tools 都只照着这份清单画，所以这里要守住的是清单本身说的对不对：
  - 一个功能在不在、为什么不在、关着的怎么打开：只看这段对话真正发给模型的工具（App.toolbox）和设置。
    这家模型做不了的（DeepSeek 不能联网、看不了图）是"用不了"，设置里关着的是"关着"，
    对话开始以后才打开的是"下一段对话开始生效"（工具在一段对话开始时定下，之后不变）；
  - 问不问你：是拿几个有代表性的调用去问 permissions.py 得出来的。换一档确认方式，每一类操作的说法跟着变；
    "操作整台电脑"和"往密码框里输入"哪一档都问；
  - 给人看的话里不出现工具的名字（那些只在 tools 这一项技术细节里）；每个发给模型的工具恰好在一项里：不藏、不重复；
  - 页面拿到的每一句话都带着原文（key），换语言时在原地重译；MCP 服务器的名字是你起的，原样给。

模型是本地的假 API，电脑是 FakeDesktop，MCP 服务器是 tests/fake_mcp.py，不连任何真的服务、不花钱。
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import openai
import pytest
import test_mcp
import test_webui
from conftest import RecordingUI
from fake_api import reply, text
from fake_desktop import FakeDesktop, install
from fake_mcp import BASE_TOOLS, FakeMcpHttp
from fake_openai import FakeOpenAI
from test_mcp import FAKE, settle, stdio, write_config
from test_webui import latest_state

from harness import capabilities, i18n
from harness import commands as commands_module
from harness.agents import examples
from harness.app import build_app
from harness.cli import handle_command
from harness.desktop import backend as desktop_backend
from harness.permissions import MODES
from harness.safe import to_json
from harness.ui import TerminalUI
from harness.webui.capabilities_api import page_state

# 借用 test_mcp 的 fixture：每个测试结束时关掉启动过的 MCP 服务器，不留进程
close_managers = test_mcp.close_managers
serve = test_webui.serve  # 借用 test_webui.py 的网页服务器（pytest 按模块里的名字找 fixture）

BROWSER_TOOLSET = "browser_toolset_20260801"
COMPUTER_TOOLSET = "computer_toolset_20260801"
# 写给模型和程序看的名字：给人看的话里一个都不该出现（只能出现在 tools 那一项技术细节里）
RAW_NAMES = (
    "str_replace_based_edit_tool", "run_command", "todo_add", "todo_list", "task_add", "task_delete",
    "send_file", "web_search", "web_fetch", "toolset", "mcp__",
)

# 几条规则的名字（capabilities.py 里写的原文），按名字找规则
WRITE_INSIDE = "改工作目录里的文件"
READ_INSIDE = "读工作目录里的文件"
READ_OUTSIDE = "读工作目录之外的文件"
WRITE_OUTSIDE = "改工作目录之外的文件"
READONLY_COMMANDS = "只读的命令（ls、git status……）"
OTHER_COMMANDS = "别的命令"
REMINDERS = "添加只是提醒的任务"
AGENT_TASKS = "添加、删除让助手自己去做的任务"
SECRETS = "往密码框、银行卡里输入"
SCREENSHOTS = "截图"
INPUT = "点鼠标、打字、按键"


# ---- 帮手 -----------------------------------------------------------------------


@pytest.fixture
def start_gpt(make_config):
    """和 conftest 的 start_openai 一样，但用的是 GPT（看得懂图片，没有联网搜索）。"""
    servers: list[FakeOpenAI] = []

    def start(*replies, **config_overrides):
        api = FakeOpenAI(list(replies)).start()
        servers.append(api)
        config = make_config(default_provider="openai", default_model="gpt-5", **config_overrides)
        ui = RecordingUI()
        client = openai.OpenAI(api_key="test-key", base_url=api.base_url, max_retries=0)
        app = build_app(config, ui, client_factory=lambda provider: client)
        return SimpleNamespace(agent=app.agent, app=app, api=api, ui=ui, config=config)

    yield start
    for api in servers:
        api.stop()


class Outlet:
    """聊天通道里的一个聊天（send_file 发到这里）。清单只看它在不在，从来不真的发。"""

    label = "Telegram"
    person = "测试的主人"

    def plan(self, name, size, media_type):
        return "document", ""

    def send(self, outgoing):
        raise AssertionError("算清单的时候不该发任何文件")


def overview(s, **options) -> capabilities.Overview:
    return capabilities.overview(s.app, **options)


def item(found: capabilities.Overview, item_id: str) -> capabilities.Capability:
    return next(entry for entry in found.items if entry.id == item_id)


def outcomes(entry: capabilities.Capability) -> dict[str, str]:
    """一项的每一类操作 → 问不问你（free / ask / always / never）。"""
    assert entry.approval is not None, f"{entry.id} 没有写问不问你"
    return {str(rule.text): rule.outcome for rule in entry.approval.rules}


def actions(entry: capabilities.Capability) -> list[tuple[str, str]]:
    return [(action.kind, action.target) for action in entry.actions]


def with_browser(s, enabled: bool = True) -> None:
    """设置里开、关浏览器。对话还没开始的话，工具马上换上（见 App.refresh_tools）。"""
    s.app.settings.set_browser({"enabled": enabled})
    s.app.refresh_tools()


def with_computer(s, **settings) -> FakeDesktop:
    """打开"操作整台电脑"，换上假的电脑（conftest 让真的后端永远找不到）。"""
    fake = FakeDesktop()
    install(s.app.desktop, fake)
    s.app.settings.set_computer({"enabled": True, **settings})
    s.app.refresh_tools()
    return fake


def write_servers(make_config, servers: dict) -> None:
    """在 start() 之前写好 mcp.json（start 和 make_config 用的是同一个数据目录）。"""
    write_config(make_config().mcp_file, servers)


def make_skill(s, name: str, valid: bool = True) -> None:
    """直接在数据目录里放一个 Skill（data/skills/<名字>/SKILL.md）。valid 为假时少了 description，算有问题。"""
    folder = s.app.skills.root / name
    folder.mkdir(parents=True, exist_ok=True)
    description = "description: Fill PDF forms and extract their text\n" if valid else ""
    (folder / "SKILL.md").write_text(f"---\nname: {name}\n{description}---\n# 步骤\n\n第一步。\n", encoding="utf-8")


def add_agent(s, example_id: str) -> str:
    """把一个示例 Agent 装进 Agent 文件夹，返回它的名字。"""
    return s.app.agents.install(examples.find(example_id).text, replace=True)


def sent_names(tools) -> list[str]:
    """发给模型的名字，自己算一遍：工具包的成员算一个（包的类型），别的用自己的名字。"""
    names: list[str] = []
    for tool in tools:
        name = tool.spec["type"] if tool.toolset else tool.name
        if name not in names:
            names.append(name)
    return names


def assert_every_tool_listed_once(s, found: capabilities.Overview | None = None) -> None:
    """这段对话发给模型的每个工具都恰好在一项的 tools 里：不藏起来，也不在两项里重复；列出来的也都真的发了。"""
    found = found or overview(s)
    names = sent_names(s.app.toolbox()[0])
    for name in names:
        holders = [entry.id for entry in found.items if name in entry.tools]
        assert len(holders) == 1, f"{name} 应该恰好在一项里，实际在：{holders}"
    listed = [name for entry in found.items for name in entry.tools]
    assert sorted(listed) == sorted(names)


def human_texts(found: capabilities.Overview) -> list[str]:
    """清单里给人看的每一句话。"""
    texts = [str(note) for note in found.notes]
    for entry in found.items:
        texts += [str(entry.label), str(entry.description), str(entry.reason), str(entry.where_text)]
        texts += [str(action.label) for action in entry.actions]
        if entry.approval is not None:
            texts += [str(entry.approval.summary), str(entry.approval.note)]
            texts += [str(rule.text) for rule in entry.approval.rules]
    return texts


# ---- 这家模型做不做得了 ----------------------------------------------------------------


def test_claude_searches_the_web_on_anthropics_servers(start):
    """联网搜索不在这台电脑上做：清单要说清楚是在 Anthropic 的服务器上，查的东西会经过那里。"""
    web = item(overview(start()), "web")
    assert web.status == "on" and web.where == "provider"
    assert "Anthropic" in web.where_text
    assert web.tools == ["web_search", "web_fetch"]
    assert outcomes(web) == {"联网搜索、读网页": "free"}


def test_deepseek_cannot_search_and_says_how_to_get_it(start_openai):
    """DeepSeek 没有联网搜索：这是"这里用不了"，不是"关着"，原因要点名现在用的是哪一家，按钮带你去换模型。"""
    web = item(overview(start_openai()), "web")
    assert web.status == "unavailable" and web.tools == [] and web.approval is None
    assert "DeepSeek" in web.reason and "Claude" in web.reason
    assert actions(web)[0] == ("models", "")


def test_web_search_switched_off_at_startup(start):
    """HARNESS_WEB=0 关掉的联网搜索：页面上开不了，原因要说是启动时关掉的。"""
    web = item(overview(start(web=False)), "web")
    assert web.status == "unavailable" and "HARNESS_WEB=0" in web.reason and web.tools == []


def test_the_computer_needs_a_model_that_can_see(start_openai, start_gpt, tmp_path):
    """操作整台电脑全靠截图：看不了图的 DeepSeek 打开了也用不了，看得懂图的 GPT 就能用。"""
    deepseek = start_openai()
    with_computer(deepseek)
    computer = item(overview(deepseek), "computer")
    assert computer.status == "unavailable" and "看不了图片" in computer.reason and "DeepSeek" in computer.reason
    assert actions(computer)[0] == ("models", "") and computer.tools == []

    gpt = start_gpt(data_dir=tmp_path / "gpt")  # 自己的数据目录：设置文件里记着的不是上面那个 DeepSeek
    with_computer(gpt)
    computer = item(overview(gpt), "computer")
    assert computer.status == "on" and computer.approval is not None
    assert computer.tools and all(name.startswith("computer_") for name in computer.tools)


# ---- 浏览器 ---------------------------------------------------------------------------


def test_the_browser_off_points_to_its_setting(start):
    """关着的浏览器：告诉你在哪儿打开；对话还没开始的话，打开了这段对话就能用。"""
    browser = item(overview(start()), "browser")
    assert browser.status == "off" and browser.tools == [] and browser.approval is None
    assert actions(browser) == [("settings", "browser")]
    assert "这段对话就能用" in browser.reason


def test_the_browser_turned_on_before_the_first_message_is_in_this_conversation(start):
    """第一条消息之前打开的浏览器马上换上：Claude 用工具包，在请求里只占一项，清单里也只写包的类型。"""
    s = start()
    with_browser(s)
    browser = item(overview(s), "browser")
    assert browser.status == "on" and browser.tools == [BROWSER_TOOLSET]
    assert actions(browser) == [("settings", "browser")]


@pytest.mark.parametrize("which", ["start_openai", "start_gpt"])
def test_other_models_get_the_browser_stand_ins(which, request):
    """别家的模型用不了 Claude 的工具包，拿到的是我们自己写的 browser_* 函数：清单里写的就是它们。"""
    s = request.getfixturevalue(which)()
    with_browser(s)
    browser = item(overview(s), "browser")
    assert browser.status == "on" and browser.tools
    assert all(name.startswith("browser_") for name in browser.tools) and BROWSER_TOOLSET not in browser.tools


def test_the_browser_turned_on_mid_conversation_starts_next_time(start):
    """对话开始以后工具不变（变了缓存就作废）：中途打开的浏览器是"下一段对话开始生效"，按钮是开始新对话。"""
    s = start(reply(text("好")))
    s.agent.run_turn("你好")
    with_browser(s)
    browser = item(overview(s), "browser")
    assert browser.status == "pending" and browser.tools == []
    assert actions(browser) == [("new", "")] and "下一段对话" in browser.reason


def test_a_new_conversation_picks_up_the_browser_turned_on_earlier(start):
    """照着"开始新对话"的按钮点了：新的这一段（还没发消息）就该带上浏览器，清单不能还说"下一段对话开始生效"。"""
    s = start(reply(text("好")))
    s.agent.run_turn("你好")
    with_browser(s)
    s.app.new_conversation()
    assert item(overview(s), "browser").status == "on"


def test_the_browser_turned_off_mid_conversation_keeps_its_tools(start):
    """中途关掉的浏览器：工具还在这段对话里（调用时回答"关着"），清单要照实说，技术细节里也照样列出来。"""
    s = start(reply(text("好")))
    with_browser(s)
    s.agent.run_turn("你好")
    with_browser(s, enabled=False)
    browser = item(overview(s), "browser")
    assert browser.status == "off" and browser.tools == [BROWSER_TOOLSET]
    assert "工具还在" in browser.reason and actions(browser) == [("settings", "browser")]


# ---- 操作整台电脑 -----------------------------------------------------------------------


def test_the_computer_is_unavailable_where_there_is_no_desktop(start):
    """这台电脑上根本做不了（不支持的系统、Wayland）：是"用不了"，原因说清楚为什么，不给一个点了也没用的开关。"""
    s = start()
    computer = item(overview(s), "computer")
    assert computer.status == "unavailable" and "macOS" in computer.reason and computer.actions == []

    s.app.desktop._backend, s.app.desktop._reason, s.app.desktop._detected = None, desktop_backend.WAYLAND, True
    computer = item(overview(s), "computer")
    assert computer.status == "unavailable" and "Wayland" in computer.reason

    # Windows 上：直说"Windows 上暂时不支持"，不写 macOS 的权限、Linux 要装的程序
    s.app.desktop._backend, s.app.desktop._reason, s.app.desktop._detected = None, desktop_backend.WINDOWS, True
    computer = item(overview(s), "computer")
    assert computer.status == "unavailable" and computer.reason.startswith("Windows 上暂时不支持") and computer.actions == []


@pytest.mark.parametrize("mode", list(MODES))
def test_the_computer_asks_every_step_in_every_mode(start, mode):
    """操作整台电脑每一步都问，"全部自动允许"也一样：清单不能因为换了一档就说"直接做"。"""
    s = start()
    with_computer(s)
    s.agent.permissions.mode = mode
    computer = item(overview(s), "computer")
    assert computer.status == "on" and computer.tools == [COMPUTER_TOOLSET]
    assert computer.approval.level == "always"
    assert outcomes(computer) == {SCREENSHOTS: "always", INPUT: "always"}


def test_trusting_screenshots_frees_only_screenshots(start):
    """在确认卡片上选了"这段对话里截图不再问"：只有截图不问了，点鼠标、打字照样每一步都问。"""
    s = start()
    with_computer(s)
    s.app.chat_desktop.trust_screenshots = True
    computer = item(overview(s), "computer")
    assert outcomes(computer) == {SCREENSHOTS: "free", INPUT: "always"}
    assert computer.approval.level == "some"


def test_scheduled_runs_need_their_own_switch_for_the_computer(start):
    """定时任务和聊天通道要另外打开才能操作电脑：只开了对话里的，那次运行的清单要说缺的是哪个开关。"""
    s = start()
    with_computer(s)
    run = s.app.unattended_agent(RecordingUI())
    computer = item(overview(s, run=run), "computer")
    assert computer.status == "off" and "定时任务和聊天通道" in computer.reason
    assert actions(computer) == [("settings", "computer")]

    s.app.settings.set_computer({"enabled": True, "unattended": True})
    computer = item(overview(s, run=s.app.unattended_agent(RecordingUI(), "all")), "computer")
    assert computer.status == "on" and computer.approval.level == "always"  # 任务选了"全部自动允许"也照样问


# ---- MCP 服务器 -------------------------------------------------------------------------


def test_without_servers_there_is_one_mcp_row(start):
    """一个服务器都没接：只有一行"MCP 服务器"，关着，按钮带你去添加。"""
    found = overview(start())
    rows = [entry for entry in found.items if entry.id.startswith("mcp")]
    assert [entry.id for entry in rows] == ["mcp"]
    assert rows[0].status == "off" and actions(rows[0]) == [("extensions", "mcp")]


def test_a_connected_server_is_its_own_row(start, make_config):
    """每个服务器一行，名字原样显示（是你起的）；本机运行的第三方程序要写明在哪儿执行，工具几个、问不问你。"""
    write_servers(make_config, {"fake": stdio()})
    s = start()
    settle(s.app.mcp)
    found = overview(s)
    assert not [entry for entry in found.items if entry.id == "mcp"]
    row = item(found, "mcp:fake")
    assert row.status == "on" and row.label == "fake" and row.count == len(BASE_TOOLS)
    assert row.where == "mcp-local" and "第三方" in row.where_text
    assert sorted(row.tools) == sorted(f"mcp__fake__{tool['name']}" for tool in BASE_TOOLS)
    assert row.approval.level == "ask"  # 第三方的工具默认每次都问
    assert actions(row) == [("extensions", "mcp")]


def test_mcp_approval_follows_the_mode_and_your_per_tool_choices(start, make_config):
    """MCP 工具问不问：默认都问；你在"扩展 → MCP"里放开了几个，就是"有的问"；"全部自动允许"一个都不问。"""
    write_servers(make_config, {"fake": stdio()})
    s = start()
    settle(s.app.mcp)
    s.app.mcp.set_approval("fake", "echo", "auto")
    row = item(overview(s), "mcp:fake")
    assert row.approval.level == "some"
    assert sorted(outcomes(row).values()) == ["ask", "free"]

    s.agent.permissions.mode = "all"
    row = item(overview(s), "mcp:fake")
    assert row.approval.level == "free" and set(outcomes(row).values()) == {"free"}


def test_a_remote_server_runs_on_someone_elses_machine(start, make_config):
    """HTTP 的 MCP 服务器在别人的服务器上执行：参数会发到网上去，清单要和本机的分开说。"""
    remote = FakeMcpHttp("modern").start()
    try:
        write_servers(make_config, {"web": {"type": "http", "url": remote.url}})
        s = start()
        settle(s.app.mcp)
        row = item(overview(s), "mcp:web")
        assert row.status == "on" and row.where == "mcp-remote" and row.where_text != item(overview(s), "todo").where_text
    finally:
        remote.stop()


def test_a_server_connected_mid_conversation_is_pending(start, make_config):
    """对话开始以后才连上的服务器：工具不进这段对话，清单说"下一段对话开始可以用"，已经在用的那个照旧。"""
    write_servers(make_config, {"one": stdio()})
    s = start(reply(text("好")))
    m = s.app.mcp
    settle(m)
    s.agent.run_turn("你好")
    m.save({"name": "two", "transport": "stdio", "command": sys.executable, "args": [str(FAKE)]}, None)
    settle(m)
    found = overview(s)
    assert item(found, "mcp:one").status == "on"
    two = item(found, "mcp:two")
    assert two.status == "pending" and two.count == len(BASE_TOOLS) and two.tools == []
    assert_every_tool_listed_once(s, found)


def test_disabled_and_failed_servers_are_off_with_a_reason(start, make_config):
    """停用的、连不上的服务器：关着，原因写明是停用了还是连接失败（失败的带上为什么）。"""
    write_servers(make_config, {"paused": stdio(enabled=False), "typo": {"command": "definitely-not-a-command-xyz"}})
    s = start()
    settle(s.app.mcp)
    found = overview(s)
    paused, typo = item(found, "mcp:paused"), item(found, "mcp:typo")
    assert paused.status == "off" and "停用" in paused.reason and paused.tools == []
    assert typo.status == "off" and "连接失败" in typo.reason and "definitely-not-a-command-xyz" in typo.reason
    assert typo.approval is None and actions(typo) == [("extensions", "mcp")]


def test_a_removed_servers_tools_are_still_listed(start, make_config):
    """对话中途删掉一个服务器：它的工具还在这段对话的说明书里（调用时回答"被删掉了"），清单不能把它们藏起来。"""
    write_servers(make_config, {"one": stdio()})
    s = start(reply(text("好")))
    settle(s.app.mcp)
    s.agent.run_turn("你好")
    s.app.mcp.remove("one")
    assert any(tool.name.startswith("mcp__one__") for tool in s.app.toolbox()[0])
    assert_every_tool_listed_once(s)


# ---- Skill 和 Agent ---------------------------------------------------------------------


def test_skills_count_only_the_enabled_good_ones(start):
    """Skill 的数目只算启用了、也没有问题的：停用的、写坏了的模型看不到，清单里也不能算上。"""
    s = start()
    skill = item(overview(s), "skill")
    assert skill.status == "off" and skill.count == 0 and "还没有装" in skill.reason
    assert actions(skill) == [("extensions", "skills")]

    for name in ("pdf-tools", "csv-tools", "paused"):
        make_skill(s, name)
    make_skill(s, "broken", valid=False)
    s.app.skills.set_enabled("paused", False)
    skill = item(overview(s), "skill")
    assert skill.status == "on" and skill.count == 2 and "2" in skill.reason
    assert actions(skill) == [("extensions", "skills")]


def test_only_disabled_skills_are_off(start):
    """装了却都停用了：关着，原因说"都没有启用"，而不是"还没有装"。"""
    s = start()
    make_skill(s, "pdf-tools")
    s.app.skills.set_enabled("pdf-tools", False)
    skill = item(overview(s), "skill")
    assert skill.status == "off" and skill.count == 0 and "没有启用" in skill.reason


def test_a_broken_skill_is_not_called_disabled(start):
    """启用着、只是 SKILL.md 写坏了：原因不能说"没有启用"，那样你会去点启用，而该做的是把文件改好。"""
    s = start()
    make_skill(s, "broken", valid=False)
    skill = item(overview(s), "skill")
    assert skill.status == "off" and skill.count == 0
    assert "没有启用" not in skill.reason


def test_agents_count_only_the_good_ones(start):
    """自定义 Agent 的数目不算文件有问题的（交给它也只会失败）。"""
    s = start()
    agent = item(overview(s), "agent")
    assert agent.status == "off" and agent.count == 0 and actions(agent) == [("extensions", "agents")]

    add_agent(s, "researcher")
    add_agent(s, "writer")
    (s.config.agents_dir / "坏的.md").write_text("坏的", encoding="utf-8")
    agent = item(overview(s), "agent")
    assert agent.status == "on" and agent.count == 2 and "2" in agent.reason


def test_only_broken_agents_are_not_called_missing(start):
    """有 Agent 文件、只是都写坏了：原因不能说"还没有"，该告诉你的是它们有问题。"""
    s = start()
    s.config.agents_dir.mkdir(parents=True, exist_ok=True)
    (s.config.agents_dir / "坏的.md").write_text("坏的", encoding="utf-8")
    agent = item(overview(s), "agent")
    assert agent.status == "off" and agent.count == 0
    assert "还没有" not in agent.reason


# ---- 把文件发回聊天 ----------------------------------------------------------------------


def test_sending_files_is_only_for_chats(start):
    """网页上的普通对话没有 send_file：用不了，按钮带你去设置聊天通道。"""
    send = item(overview(start()), "send")
    assert send.status == "unavailable" and send.tools == [] and actions(send) == [("extensions", "channels")]


def test_a_channel_conversation_opened_on_the_web_cannot_send(start):
    """在网页上接着聊一段 Telegram 里的会话：send_file 还在（说明书不变），但发不出去，原因点名是哪个平台。"""
    s = start()
    s.app.conversation.channel = "telegram"
    s.app.refresh_tools()
    send = item(overview(s), "send")
    assert send.status == "unavailable" and "Telegram" in send.reason
    assert send.tools == ["send_file"] and send.approval is None


def test_a_chat_run_can_send_and_asks_first(start):
    """真的在聊天软件里的一轮：能发文件，每次发之前问你（"全部自动允许"那一档除外）。"""
    s = start()
    run = s.app.channel_agent(RecordingUI(), "ask", "anthropic", "claude-opus-5-5", outlet=Outlet())
    found = overview(s, run=run, in_chat=True)
    assert found.started is True  # 一次运行的工具在开始时就定下了
    send = item(found, "send")
    assert send.status == "on" and send.tools == ["send_file"]
    assert outcomes(send) == {"把一个文件发到聊天里": "ask"} and send.approval.level == "ask"

    run = s.app.channel_agent(RecordingUI(), "all", "anthropic", "claude-opus-5-5", outlet=Outlet())
    assert item(overview(s, run=run, in_chat=True), "send").approval.level == "free"


# ---- 用一个 Agent 当角色的对话 -------------------------------------------------------------


def persona_conversation(start, example_id: str):
    """用一个示例 Agent 当角色开始一段对话，发出第一条消息（工具在这时定下来）。返回（助手，角色的名字）。"""
    s = start(reply(text("好")))
    name = add_agent(s, example_id)
    s.app.new_conversation(name)
    s.agent.run_turn("你好")
    return s, name


def test_a_read_only_persona_can_only_read(start):
    """角色只放开了 Read：文件还在，但改文件根本不做（不是"先问你"）；别的功能点名是角色没放开。"""
    s, name = persona_conversation(start, "reviewer")
    found = overview(s)

    files = item(found, "files")
    assert files.status == "on"
    rules = outcomes(files)
    assert rules[WRITE_INSIDE] == "never" and rules[WRITE_OUTSIDE] == "never"
    assert rules[READ_INSIDE] == "free"
    assert str(files.approval.summary).startswith("只能读")

    for blocked in ("todo", "tasks", "memory", "shell", "web", "skill", "agent", "browser"):
        entry = item(found, blocked)
        assert entry.status == "unavailable" and name in entry.reason, blocked
    assert any(name in note for note in found.notes)
    assert_every_tool_listed_once(s, found)


def test_a_persona_without_tools_has_nothing(start):
    """角色写的是 tools: []：一个工具都没有，每一项都用不了，清单开头说明为什么。"""
    s, name = persona_conversation(start, "writer")
    found = overview(s)
    assert s.app.toolbox()[0] == []
    for entry in found.items:
        if entry.id != "mcp":
            assert entry.status == "unavailable" and entry.tools == [], entry.id
    assert any(name in note and "不用任何工具" in note for note in found.notes)


def test_a_persona_conversation_shows_its_own_tools_before_the_first_message(start):
    """刚用"代码审查员"开始一段对话、还没发消息：信息栏就该照着它放开的那些画，而不是上一段对话的全部工具。"""
    s = start()
    name = add_agent(s, "reviewer")
    s.app.new_conversation(name)
    assert [entry for entry, _ in s.agent.backend.tool_names()] == ["str_replace_based_edit_tool"]  # 发出去的会是这些
    found = overview(s)
    assert item(found, "shell").status == "unavailable"
    assert outcomes(item(found, "files"))[WRITE_INSIDE] == "never"
    assert sent_names(s.app.toolbox()[0]) == ["str_replace_based_edit_tool"]


def test_a_persona_without_tools_cannot_use_mcp_either(start):
    """tools: [] 的角色：MCP 那一行也该是"用不了"，否则你照着"添加服务器"去加了，这段对话里还是用不了。"""
    s, _ = persona_conversation(start, "writer")
    assert item(overview(s), "mcp").status == "unavailable"


# ---- 每一档确认方式 ---------------------------------------------------------------------

EXPECTED = {
    # 档位：{功能：(级别, {一类操作：问不问})}
    "ask": {
        "files": ("some", {WRITE_INSIDE: "ask", READ_INSIDE: "free", READ_OUTSIDE: "ask", WRITE_OUTSIDE: "ask"}),
        "shell": ("ask", {READONLY_COMMANDS: "ask", OTHER_COMMANDS: "ask"}),
        "tasks": ("some", {REMINDERS: "free", AGENT_TASKS: "ask"}),
    },
    "readonly": {
        "files": ("some", {WRITE_INSIDE: "ask", READ_INSIDE: "free", READ_OUTSIDE: "free", WRITE_OUTSIDE: "ask"}),
        "shell": ("some", {READONLY_COMMANDS: "free", OTHER_COMMANDS: "ask"}),
        "tasks": ("some", {REMINDERS: "free", AGENT_TASKS: "ask"}),
    },
    "edits": {
        "files": ("some", {WRITE_INSIDE: "free", READ_INSIDE: "free", READ_OUTSIDE: "ask", WRITE_OUTSIDE: "ask"}),
        "shell": ("ask", {READONLY_COMMANDS: "ask", OTHER_COMMANDS: "ask"}),
        "tasks": ("some", {REMINDERS: "free", AGENT_TASKS: "ask"}),
    },
    "all": {
        "files": ("free", {WRITE_INSIDE: "free", READ_INSIDE: "free", READ_OUTSIDE: "free", WRITE_OUTSIDE: "free"}),
        "shell": ("free", {READONLY_COMMANDS: "free", OTHER_COMMANDS: "free"}),
        "tasks": ("free", {REMINDERS: "free", AGENT_TASKS: "free"}),
    },
}


@pytest.mark.parametrize("mode", list(EXPECTED))
def test_each_mode_changes_what_is_asked(start, mode):
    """换一档确认方式，每一类操作问不问跟着变：说法是问 permissions.py 得来的，不是另抄一份规矩。"""
    s = start()
    s.agent.permissions.mode = mode
    found = overview(s)
    assert found.mode == mode
    for item_id, (level, rules) in EXPECTED[mode].items():
        entry = item(found, item_id)
        assert entry.approval.level == level, item_id
        assert outcomes(entry) == rules, item_id
    # 待办、记忆从来不问
    assert item(found, "todo").approval.level == "free" and item(found, "memory").approval.level == "free"


def test_everything_allowed_still_asks_for_passwords_and_the_computer(start):
    """"全部自动允许"也有放不开的：往密码框、银行卡里输入，和操作整台电脑。清单要照实说"哪一档都问"。"""
    s = start()
    with_browser(s)
    with_computer(s)
    s.agent.permissions.mode = "all"
    found = overview(s)
    browser = outcomes(item(found, "browser"))
    assert browser[SECRETS] == "always" and set(browser.values()) == {"free", "always"}
    assert item(found, "browser").approval.level == "some"
    assert item(found, "computer").approval.level == "always"


def test_the_browser_in_ask_mode(start):
    """浏览器在"每次都问"下：只是看不问，去新网站、输入、提交先问，密码哪一档都问。"""
    s = start()
    with_browser(s)
    browser = item(overview(s), "browser")
    assert outcomes(browser) == {"看网页、截图、滚动": "free", "去一个新的网站、输入文字、提交表单": "ask", SECRETS: "always"}
    assert browser.approval.level == "some"


def test_your_own_commands_are_mentioned_where_they_count(start):
    """你自己加的"不用问的命令"只在"只读的命令不用问"那一档里算：别的档要说明它们这时不算数。"""
    s = start()
    s.agent.permissions.commands.add("npm test")
    for mode, expected in (("readonly", "1 条命令也不用问"), ("ask", "只读的命令不用问"), ("edits", "只读的命令不用问")):
        s.agent.permissions.mode = mode
        assert expected in item(overview(s), "shell").approval.note, mode
    s.agent.permissions.mode = "all"
    assert item(overview(s), "shell").approval.note == ""  # 什么都不问了，不用再提
    assert actions(item(overview(s), "shell")) == [("approval", ""), ("commands", "")]


def test_on_windows_the_shell_says_every_command_asks_and_why(start, monkeypatch):
    """Windows 上命令是 PowerShell，一条都不算只读、你加的也不算（commands/__init__.py）：清单照实说"每条都先问"，并写明原因。"""
    monkeypatch.setattr(commands_module, "WINDOWS", True)
    s = start()
    s.agent.permissions.mode = "readonly"
    shell = item(overview(s), "shell")
    assert shell.approval.level == "ask" and "PowerShell" in shell.approval.note  # 每条都先问（"部分先问"是 some）
    s.agent.permissions.mode = "ask"
    assert item(overview(s), "shell").approval.note == ""  # 每次都问的那一档：本来就都问，不用多说
    s.agent.permissions.commands.add("npm test")
    assert "暂时也不起作用" in item(overview(s), "shell").approval.note


# ---- 对话中途改设置 ------------------------------------------------------------------------


def test_started_flips_after_the_first_message(start):
    """发出第一条消息，这段对话的工具就定下了：清单从"现在打开这段就能用"换成"下一段对话开始"。"""
    s = start(reply(text("好")))
    assert overview(s).started is False
    s.agent.run_turn("你好")
    found = overview(s)
    assert found.started is True
    assert "下一段对话" in item(found, "browser").reason


def test_the_computer_turned_on_mid_conversation_starts_next_time(start):
    """中途打开"操作整台电脑"：这段对话里没有它的工具，是"下一段对话开始生效"。"""
    s = start(reply(text("好")))
    s.agent.run_turn("你好")
    with_computer(s)
    computer = item(overview(s), "computer")
    assert computer.status == "pending" and computer.tools == [] and actions(computer) == [("new", "")]


def test_computer_tools_sent_without_a_desktop_are_still_listed(start):
    """设置里开着、这台电脑却做不了：工具照样发给了模型（每次请求多几千个 token），清单不能把它们藏起来。"""
    s = start()
    s.app.settings.set_computer({"enabled": True})
    s.app.refresh_tools()
    assert COMPUTER_TOOLSET in sent_names(s.app.toolbox()[0])
    assert_every_tool_listed_once(s)


# ---- 给页面的样子 -----------------------------------------------------------------------


def page_sentences(state: dict) -> list[tuple[str, dict]]:
    """状态里每一句给人看的话：（在哪儿，那句话）。"""
    found = [("notes", note) for note in state["notes"]]
    for entry in state["items"]:
        where = entry["id"]
        found += [(f"{where}.label", entry["label"]), (f"{where}.description", entry["description"]), (f"{where}.where_text", entry["where_text"])]
        if entry["reason"] is not None:
            found.append((f"{where}.reason", entry["reason"]))
        found += [(f"{where}.action", action["label"]) for action in entry["actions"]]
        approval = entry["approval"]
        if approval is not None:
            found.append((f"{where}.summary", approval["summary"]))
            found += [(f"{where}.rule", rule["text"]) for rule in approval["rules"]]
            if approval["note"] is not None:
                found.append((f"{where}.note", approval["note"]))
    return found


@pytest.fixture
def busy(start, make_config):
    """一个什么都有的助手：接了一个 MCP 服务器、开着浏览器和电脑、加了一条不用问的命令（清单里的话尽量都出现）。"""
    write_servers(make_config, {"fake": stdio(), "typo": {"command": "definitely-not-a-command-xyz"}})
    s = start()
    settle(s.app.mcp)
    with_browser(s)
    with_computer(s)
    s.agent.permissions.commands.add("npm test")
    s.agent.permissions.mode = "readonly"
    return s


def test_every_sentence_on_the_page_carries_its_original(busy):
    """页面换语言时照着原文在原地重译：每一句都要带着 key；只有 MCP 服务器的名字是你起的，原样给、没有 key。"""
    state = page_state(busy.app)
    assert state["mode"] == "readonly" and state["started"] is False
    sentences = page_sentences(state)
    assert len(sentences) > 50
    for where, sentence in sentences:
        assert isinstance(sentence, dict) and isinstance(sentence.get("text"), str) and sentence["text"], where
        if where in ("mcp:fake.label", "mcp:typo.label"):
            assert sentence == {"text": where.split(":")[1].split(".")[0]}  # 你起的名字：原样，没有 key
        else:
            assert isinstance(sentence.get("key"), str) and sentence["key"], where
    for entry in state["items"]:
        assert entry["status"] in ("on", "pending", "off", "unavailable")
        assert all(isinstance(name, str) for name in entry["tools"])
    assert json.loads(to_json(state)) == state  # 原样进得了事件流


def test_switching_language_retranslates_in_place(busy):
    """换语言：文字换成新的语言，原文（key）不变，页面靠它就地重译，不用再问一遍服务器。"""
    chinese = page_state(busy.app)
    try:
        i18n.choose("en")
        english = page_state(busy.app)
        files_label = str(i18n.t("读写文件"))
    finally:
        i18n.choose("zh")
    before, after = page_sentences(chinese), page_sentences(english)
    assert [where for where, _ in before] == [where for where, _ in after]
    for (where, old), (_, new) in zip(before, after, strict=True):
        assert old.get("key") == new.get("key") and old.get("values", {}).keys() == new.get("values", {}).keys(), where
    files = next(sentence for where, sentence in after if where == "files.label")
    assert files["text"] == files_label != "读写文件" and files["key"] == "读写文件"
    assert sum(old["text"] != new["text"] for (_, old), (_, new) in zip(before, after, strict=True)) > 10
    assert next(sentence for where, sentence in after if where == "mcp:fake.label") == {"text": "fake"}  # 名字不翻译


def test_the_web_state_has_capabilities_instead_of_tools(serve):
    """网页的状态里换成了"智狐能做的事"：页面只照着画，旧的 tools 列表不再发。"""
    s = serve()
    state = latest_state(s.browser)
    assert "tools" not in state
    found = state["capabilities"]
    assert {"mode", "started", "notes", "items"} <= set(found)
    files = next(entry for entry in found["items"] if entry["id"] == "files")
    assert files["status"] == "on" and files["label"]["key"] == "读写文件"


# ---- 终端里的 /tools --------------------------------------------------------------------


def test_the_terminal_lists_capabilities_with_names_only_in_the_details(start, capsys):
    """/tools 和信息栏是同一份清单：先写给人看的名字和状态，工具的名字只在"技术细节"那一行。"""
    ui = TerminalUI()
    s = start(ui=ui)
    capsys.readouterr()
    assert handle_command("/tools", s.app, ui) is True
    out = capsys.readouterr().out
    assert "读写文件" in out and "执行命令" in out and "[在用]" in out and "[关着]" in out and "[用不了]" in out
    assert "每次都问" in out  # 现在的确认方式
    lines = out.splitlines()
    for name in ("str_replace_based_edit_tool", "run_command", "todo_add", "task_add"):
        holders = [line for line in lines if name in line]
        assert holders and all("技术细节" in line for line in holders), name


# ---- 给人看的话里没有工具的名字；每个工具都恰好在一项里 ----------------------------------------


def configurations(start, start_openai, start_gpt, make_config, tmp_path):
    """几种不一样的助手：Claude、DeepSeek、GPT（浏览器、电脑、MCP 都有）、只读的角色、聊天通道里的一轮。返回（说明，助手，清单）。

    每一种用自己的数据目录：设置文件（浏览器、电脑开没开）和 mcp.json 不会从上一种带过来。"""

    def fresh(make, name: str, *replies):
        data_dir = tmp_path / name
        write_config(make_config(data_dir=data_dir).mcp_file, {"fake": stdio()})
        s = make(*replies, data_dir=data_dir)
        settle(s.app.mcp)  # 等它连好：不然两次取工具之间它连上了，前后对不上
        return s

    for label, make in (("Claude", start), ("DeepSeek", start_openai), ("GPT", start_gpt)):
        s = fresh(make, label)
        with_browser(s)
        with_computer(s)
        yield label, s, overview(s)
    reviewer = fresh(start, "reviewer", reply(text("好")))
    reviewer.app.new_conversation(add_agent(reviewer, "reviewer"))
    reviewer.agent.run_turn("看看代码")  # 角色的工具在第一条消息时定下来
    yield "代码审查员", reviewer, overview(reviewer)
    chat = fresh(start, "chat")
    run = chat.app.channel_agent(RecordingUI(), "ask", "anthropic", "claude-opus-5-5", outlet=Outlet())
    yield "聊天通道", chat, overview(chat, run=run, in_chat=True)


def test_human_text_never_names_tools(start, start_openai, start_gpt, make_config, tmp_path):
    """str_replace_based_edit_tool、run_command 这些是写给模型的名字，对你没有意义：只许出现在技术细节里。"""
    for label, _, found in configurations(start, start_openai, start_gpt, make_config, tmp_path):
        for sentence in human_texts(found):
            leaked = [name for name in RAW_NAMES if name in sentence]
            assert not leaked, f"{label}：{sentence!r} 里有 {leaked}"


def test_every_tool_is_in_exactly_one_item(start, start_openai, start_gpt, make_config, tmp_path):
    """发给模型的每个工具都恰好归在一项里：不会因为认不出就藏起来，也不会一个工具算进两个功能。"""
    for label, s, found in configurations(start, start_openai, start_gpt, make_config, tmp_path):
        if label == "聊天通道":
            continue  # 一次运行的工具不在 app.toolbox 里，下面单独对
        assert_every_tool_listed_once(s, found)
    claude = overview(start())
    assert not [entry for entry in claude.items if entry.id.startswith("other:")]  # 自己的工具都认得出来


def test_claude_toolsets_count_as_one_name(start):
    """Claude 的浏览器、电脑工具包在请求里各只占一项：清单里写包的类型，不把几十个成员（screenshot、left_click……）一个个列出来。"""
    s = start()
    with_browser(s)
    with_computer(s)
    found = overview(s)
    listed = [name for entry in found.items for name in entry.tools]
    assert listed.count(BROWSER_TOOLSET) == 1 and listed.count(COMPUTER_TOOLSET) == 1
    assert not {"screenshot", "left_click", "navigate", "find"} & set(listed)
    assert_every_tool_listed_once(s, found)


def test_a_chat_run_lists_exactly_its_own_tools(start):
    """聊天通道里的一轮有自己的那一套工具（多一个 send_file）：清单照着那一套列，一个不多一个不少。"""
    s = start()
    run = s.app.channel_agent(RecordingUI(), "ask", "anthropic", "claude-opus-5-5", outlet=Outlet())
    found = overview(s, run=run, in_chat=True)
    names = sent_names([tool for tool in run.agent.tools if run.agent.backend.sends(tool)])
    listed = [name for entry in found.items for name in entry.tools]
    assert sorted(listed) == sorted(names) and "send_file" in listed
