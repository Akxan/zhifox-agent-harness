"""终端界面也存会话、也能打开保存过的会话（见 harness/chat_cli.py、harness/history_cli.py）。

和 test_rewind_cli.py 一样：主循环 run_terminal 照常跑，输入换成写好的几行，模型是本地的假 API（不花钱）。
不同的是这里的终端可以在同一个数据目录上连着开好几次（上一次存的会话，下一次 /history 看得到），
也可以和网页界面（test_webui 的 serve）对着同一个数据目录：终端存的会话在网页上打开、接着聊、重新生成，反过来也一样。
测试里不是交互式终端，所以确认一律当作拒绝、"要继续吗"一律当作不继续；要测回答 y 的情况，用 interactive=True。
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import anthropic
import openai
import pytest
import test_webui
from fake_api import FakeAPI, reply, text, thinking, tool_use, web_cite, web_search
from fake_openai import FakeOpenAI, chat
from processes import WINDOWS, ask_to_exit, own_group
from test_rewind_web import replay_and_turn
from test_webui import open_conversation, say

import harness.ui as terminal_ui
from harness import cli, instance
from harness.app import App, build_app
from harness.cli import HELP
from harness.conversations import ConversationStore
from harness.doctor import environment as doctor_environment
from harness.settings import Settings
from harness.ui import TerminalUI

# 借用 test_webui 的 fixture：起一个网页服务器，后面连着假的 API，并让"浏览器"连上去
serve = test_webui.serve
NOW = datetime(2026, 11, 20, 14, 3)
ROOT = Path(__file__).resolve().parent.parent
MISSING = "20200101-000000-abcdef"  # 格式正确、但并不存在的会话编号


@pytest.fixture
def terminal(make_config, monkeypatch, capsys):
    """在终端里依次输入这几行，跑完主循环（写好的几行用完了就是按了 Ctrl-D）。返回这一次的情况（见 run 的返回值）。

    lines 里也可以放一个函数：轮到它时调用它，它返回的就是这一行（用来在中途看一眼、或者发一个信号）。
    replies 是 Claude 的假 API 按顺序给的回复，others 是 OpenAI 格式的那个（DeepSeek）的。
    config 不给的话用 make_config()：同一个测试里每次都是同一个数据目录。
    """
    servers: list = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def run(lines: list, *replies, others=(), config=None, interactive: bool = False):
        api = FakeAPI(list(replies)).start()
        other = FakeOpenAI(list(others), models=["deepseek-flash", "deepseek-v4-pro"]).start()
        servers.extend([api, other])

        def client_factory(provider):
            # 和真实程序一样：每次都按环境变量里当前的 key 创建连接
            key = os.environ.get(provider.key_env) or "missing"
            if provider.kind == "anthropic":
                return anthropic.Anthropic(api_key=key, base_url=api.base_url, max_retries=0)
            return openai.OpenAI(api_key=key, base_url=other.base_url, max_retries=0)

        apps: list[App] = []

        def assemble(config, ui):
            app = build_app(config, ui, client_factory=client_factory)
            app.agent.clock = lambda: NOW  # 钟固定住：两次请求"一字不差"才比得了
            apps.append(app)
            return app

        monkeypatch.setattr(cli, "build_app", assemble)
        typed = iter(lines)
        defaults: list[str] = []  # 每次读输入时预先填好的文字（/edit 把上一条消息填进去）

        def read_input(self, default: str = "") -> str | None:
            defaults.append(default)
            line = next(typed, None)  # None：用户按了 Ctrl-D
            return line() if callable(line) else line

        monkeypatch.setattr(TerminalUI, "read_input", read_input)
        if interactive:
            real_init = TerminalUI.__init__

            def init(self, *args, **kwargs):
                real_init(self, *args, **kwargs)
                self._interactive = True

            monkeypatch.setattr(TerminalUI, "__init__", init)
            monkeypatch.setattr(terminal_ui, "ask", lambda prompt: "y")
        config = config or make_config()
        code = cli.run_terminal(config)
        out = capsys.readouterr().out
        return SimpleNamespace(code=code, api=api, other=other, out=out, app=apps[0] if apps else None, config=config, defaults=defaults)

    yield run
    for server in servers:
        server.stop()


def until(condition, timeout: float = 30, what: str = "条件"):
    deadline = time.monotonic() + timeout
    while not (found := condition()):
        assert time.monotonic() < deadline, f"等不到{what}"
        time.sleep(0.02)
    return found


def saved(config) -> list[dict]:
    """磁盘上的每一段会话，按创建的先后（同一秒里开的两段，编号的先后是随机的，所以按 created_at 排）。"""
    found = [json.loads(path.read_text(encoding="utf-8")) for path in config.conversations_dir.glob("*.json")]
    return sorted(found, key=lambda conversation: conversation["created_at"])


def kinds(events: list[dict]) -> list[str]:
    return [event["type"] for event in events]


def of(events: list[dict], kind: str, key: str = "text") -> list:
    return [event.get(key) for event in events if event["type"] == kind]


def said(request: dict) -> list[str]:
    """请求里每条用户消息里用户自己写的那句话（工具结果不算）。"""
    return [
        message["content"][-1]["text"]
        for message in request["messages"]
        if message["role"] == "user" and message["content"][-1].get("type") == "text"
    ]


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


# ---- 每一轮都存盘 -------------------------------------------------------------------


def test_a_terminal_turn_is_saved_like_a_web_one(terminal):
    s = terminal(["说说榴莲\n（第二行不进标题）", "它闻起来怎么样"], reply(text("榴莲是**水果之王**。")), reply(text("很臭。")))
    assert s.code == 0
    (conversation,) = saved(s.config)
    # 和网页存的是同一种文件：标题照着第一句话起，messages 是发给模型的历史，events 是界面上显示过的内容
    assert (conversation["title"], conversation["provider"], conversation["model"]) == ("说说榴莲", "anthropic", "claude-opus-5-5")
    assert conversation["messages"] == s.app.agent.messages
    assert conversation["context_tokens"] > 0 and conversation["created_at"] <= conversation["updated_at"]
    events = conversation["events"]
    assert kinds(events) == ["user", "turn_start", "text", "block_end", "trace", "turn_end"] * 2
    # 你的消息上记着这一轮从历史的哪里开始（倒回去用的位置，见 harness/rewind.py）
    assert [event for event in events if event["type"] == "user"] == [
        {"type": "user", "text": "说说榴莲\n（第二行不进标题）", "mark": 0},
        {"type": "user", "text": "它闻起来怎么样", "mark": 2},
    ]
    assert of(events, "text") == ["榴莲是**水果之王**。", "很臭。"]  # 回答原样存着 Markdown，网页照着画
    # 搜索的索引跟着更新：这一次运行里马上能搜到，下一次启动（网页界面）从磁盘读也搜得到
    assert [item["id"] for item in s.app.store.search("水果之王")] == [conversation["id"]]
    fresh = ConversationStore(s.config.conversations_dir)
    assert [item["id"] for item in fresh.search("很臭")] == [conversation["id"]]
    assert [item["title"] for item in fresh.summaries()] == ["说说榴莲"]


def test_saving_does_not_read_every_other_conversation(terminal, monkeypatch):
    # 会话的列表和搜索要把每个文件读一遍（ConversationStore._load）。终端每一轮都存盘，却不一定用得着列表：
    # 存盘不等它，到第一次 /history 才扫
    for n in range(3):
        terminal([f"问{n}"], reply(text("答")))
    reads = []
    real = ConversationStore._read
    monkeypatch.setattr(ConversationStore, "_read", lambda self, conversation_id: (reads.append(conversation_id), real(self, conversation_id))[1])
    s = terminal(["再问一句"], reply(text("答")))
    assert reads == [] and len(saved(s.config)) == 4
    terminal(["/history"])
    assert len(reads) == 4


def test_new_starts_another_conversation_and_each_is_saved(terminal):
    s = terminal(["问一", "/new", "/new", "问二"], reply(text("答一")), reply(text("答二")))
    first, second = saved(s.config)
    assert (first["title"], second["title"]) == ("问一", "问二") and first["id"] != second["id"]
    assert of(second["events"], "user") == ["问二"] and len(s.api.requests[1]["messages"]) == 1  # 新的一段从头开始
    assert s.out.count("已开始新对话。") == 2  # 空的那一段什么都没聊：不存盘


def test_a_failed_first_message_is_not_saved(terminal):
    from fake_api import failure

    s = terminal(["问一"], failure(400, "invalid_request_error", "不行"))
    assert saved(s.config) == []  # 和网页一样：第一条消息就没答出来，当它没发生过


def test_approvals_and_tool_cards_are_recorded(terminal):
    # 不是交互式终端：没人能确认，这条命令被拒绝了。卡片和结果照样记下来
    s = terminal(
        ["建个文件"],
        reply(tool_use("toolu_1", "run_command", {"command": "touch made.txt"}), stop_reason="tool_use"),
        reply(text("好的，不建。")),
    )
    (conversation,) = saved(s.config)
    events = conversation["events"]
    assert kinds(events) == ["user", "turn_start", "tool_call", "confirm", "tool_result", "text", "block_end", "trace", "turn_end"]
    call, card = events[2], events[3]
    assert (call["name"], call["summary"], call["input"]) == ("run_command", "run_command $ touch made.txt", {"command": "touch made.txt"})
    # 卡片上的话带着原文（key、values）：以后换了语言再在网页上打开，照样能重译
    assert card["outcome"] == "denied" and "touch made.txt" in card["description"] and card["key"] and "values" in card
    assert not (s.config.workspace / "made.txt").exists()


def test_the_terminal_saves_the_same_events_the_web_does(terminal, serve):
    # 同一段剧本（思考、联网搜索带引用、一次工具调用、再答一段）分别在终端和网页上跑一遍：存下来的 events 一模一样
    # （运行记录里有时刻和耗时，只比种类）。终端在这一轮结束以后才存盘，所以多一个 turn_end；网页的那个下一轮存盘时才进文件
    def script() -> list[dict]:
        return [
            reply(
                thinking("先搜一下"),
                *web_search("srvtoolu_1", "榴莲", [("https://example.com/durian", "榴莲百科")]),
                text("榴莲", [web_cite("https://example.com/durian", "榴莲百科", "榴莲是一种热带水果", "idx-1")]),
                text("很臭。"),
                tool_use("toolu_1", "todo_list", {}),
                stop_reason="tool_use",
            ),
            reply(text("另外，你没有待办。")),
        ]

    s = terminal(["查一下榴莲"], *script())
    (mine,) = saved(s.config)
    web = serve(*script())
    say(web, "查一下榴莲")
    (theirs,) = [conversation for conversation in saved(web.config) if conversation["id"] != mine["id"]]
    assert kinds(mine["events"]) == kinds(theirs["events"]) + ["turn_end"]
    assert "cite" in kinds(mine["events"]) and kinds(mine["events"]).count("notice") == 2  # 搜索开始、搜到了几条：各一次，没有多记

    def content(events: list[dict]) -> list[dict]:
        return [event for event in events if event["type"] not in ("trace", "turn_end")]

    assert content(mine["events"]) == content(theirs["events"])


# ---- 网页打开终端存的会话，反过来也一样 -------------------------------------------------------


def test_the_web_opens_a_terminal_conversation_and_continues_it(terminal, serve):
    s = terminal(
        ["建个文件"],
        reply(tool_use("toolu_1", "run_command", {"command": "touch made.txt"}), stop_reason="tool_use"),
        reply(text("建好了：**made.txt**")),
        interactive=True,
    )
    (conversation,) = saved(s.config)
    assert (s.config.workspace / "made.txt").exists()
    web = serve(reply(text("还在。")))
    replayed, state = open_conversation(web, conversation["id"])
    # 页面先清空，再把终端里的这一轮画出来：你的消息、工具卡片、确认（带着结果）、回答、运行记录
    assert kinds(replayed) == [
        "user", "turn_start", "tool_call", "confirm", "confirm_done", "tool_result", "text", "block_end", "trace", "turn_end",
    ]  # fmt: skip
    card, done = replayed[3], replayed[4]
    assert (done["confirm"], done["allowed"], done["stopped"]) == (card["id"], True, False)
    assert of(replayed, "text") == ["建好了：**made.txt**"]
    assert (state["conversation"], state["title"]) == (conversation["id"], "建个文件")
    # 能编辑、重新生成（这一轮执行过命令：倒回去之前页面会问一次）
    assert state["rewind"]["ok"] is True and state["rewind"]["mark"] == 0 and state["rewind"]["effects"] == ["run_command $ touch made.txt"]
    # 接着聊：只在末尾追加，终端存下的历史一字不动地发过去
    say(web, "文件还在吗")
    request = web.api.requests[0]
    assert request["messages"][: len(conversation["messages"])] == conversation["messages"]
    assert said(request)[-1] == "文件还在吗"
    after = json.loads((s.config.conversations_dir / f"{conversation['id']}.json").read_text(encoding="utf-8"))
    assert after["events"][: len(conversation["events"])] == conversation["events"]


def test_the_web_can_regenerate_a_terminal_turn(terminal, serve):
    s = terminal(["问一", "问二"], reply(text("答一")), reply(text("答二")))
    (conversation,) = saved(s.config)
    web = serve(reply(text("新答二")))
    _, state = open_conversation(web, conversation["id"])
    assert state["rewind"]["ok"] is True and state["rewind"]["mark"] == 2
    target = {"conversation": conversation["id"], "turn": state["rewind"]["turn"]}
    after = web.ui.events_after(0, timeout=0)[-1]["id"]
    assert web.browser.post("/api/turn/retry", target) == (200, {"ok": True})
    _, live = replay_and_turn(web.browser, after)
    assert of(live, "text") == ["新答二"]
    request = web.api.requests[0]
    # 用户消息原样再发一次（连同终端当时的时间说明），旧的回答不在里面
    assert request["messages"] == s.api.requests[1]["messages"]
    assert said(request) == ["问一", "问二"] and "答二" not in dumps(request)


def test_a_web_conversation_opens_in_the_terminal(serve, terminal):
    web = serve(reply(text("网页上的回答")))
    first = say(web, "网页上问的")["conversation"]
    (conversation,) = saved(web.config)
    s = terminal(["/history", "/open 1", "接着问"], reply(text("终端的回答")))
    assert "1. 网页上问的" in s.out and "网页上的回答" in s.out  # 列出来；打开时把最后几条消息写出来
    request = s.api.requests[0]
    assert request["messages"][: len(conversation["messages"])] == conversation["messages"]  # 只在末尾追加
    (after,) = saved(web.config)
    assert after["id"] == first and of(after["events"], "user") == ["网页上问的", "接着问"]
    assert after["events"][: len(conversation["events"])] == conversation["events"]


# ---- /history 和 /open ---------------------------------------------------------------


def test_history_lists_searches_and_open_switches(terminal, monkeypatch):
    terminal(["说说榴莲"], reply(text("榴莲很臭，但是好吃。")))
    terminal(["说说山竹"], reply(text("山竹很甜。")))
    snapshots = []
    real = App.fresh_tools

    def fresh_tools(self, chat=None):
        snapshots.append(chat)
        return real(self, chat)

    monkeypatch.setattr(App, "fresh_tools", fresh_tools)
    s = terminal(["/history", "/history 好吃", "/open 1", "它为什么臭"], reply(text("因为含硫化合物。")))
    durian, mangosteen = saved(s.config)
    out = s.out
    # 最近的在前，编号、标题、模型、时间
    recent = out[out.index("1. 说说山竹") :]
    assert recent.index("1. 说说山竹") < recent.index("2. 说说榴莲")
    assert "Claude (Anthropic) · claude-opus-5-5" in out and re.search(r"\d{4}-\d\d-\d\d \d\d:\d\d", out)
    # 带上词就是搜索（和网页的搜索是同一个索引）：编号照着搜出来的这一份，命中的那一小段写在下面
    found = out[out.index("含有“好吃”的会话") : out.index("已打开")]
    assert found.index("1. 说说榴莲") < found.index("榴莲很臭，但是好吃。") and "山竹" not in found
    # 打开第 1 个（搜出来的榴莲）：写出最后几条消息，接着聊
    assert "你：说说榴莲" in out and "榴莲很臭，但是好吃。" in out
    assert len(snapshots) == 1  # 打开时拍了一张 MCP 快照（和网页上打开一样，见 App.fresh_tools）
    request = s.api.requests[0]
    assert said(request) == ["说说榴莲", "它为什么臭"]
    assert of(durian["events"], "user") == ["说说榴莲", "它为什么臭"] and len(durian["messages"]) == 4
    assert of(mangosteen["events"], "user") == ["说说山竹"]  # 另一段没被动过
    assert len(saved(s.config)) == 2  # 打开以前那一段空的新对话：什么都没聊，不存


def test_history_when_there_is_nothing_yet(terminal):
    s = terminal(["/history", "/history 榴莲", "/open 1"])
    assert "还没有保存过的会话" in s.out and "没有找到含有“榴莲”的会话" in s.out and "没有第 1 段" in s.out


def test_open_takes_an_id_and_explains_what_it_cannot_open(terminal):
    first = terminal(["问一"], reply(text("答一")))
    (conversation,) = saved(first.config)
    s = terminal(["/open", "/open 9", f"/open {MISSING}", "/open 1", "/open 1"])
    out = s.out
    assert "用法：/open 编号" in out and "没有第 9 段" in out and "这段会话不存在" in out
    assert out.count("已打开") == 1 and "这段会话已经开着了。" in out
    s = terminal([f"/open {conversation['id']}", "接着"], reply(text("好。")))  # 会话的编号也认
    assert "已打开" in s.out and said(s.api.requests[0]) == ["问一", "接着"]


def test_open_switches_to_the_conversations_own_provider(terminal, make_config, monkeypatch):
    # 和网页上打开一样：换回这段会话当时用的那一家、那个模型（App.open_conversation），历史交还给它，接着聊
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    first = terminal(["你是谁"], others=[chat("我是 DeepSeek。")], config=make_config(default_provider="deepseek", default_model="deepseek-flash"))
    (conversation,) = saved(first.config)
    assert conversation["provider"] == "deepseek"
    s = terminal(["/open 1", "接着说"], others=[chat("好的。")])
    assert s.api.requests == []  # Claude 那边一个请求都没收到
    assert s.app.agent.backend.provider == "deepseek" and "DeepSeek · deepseek-flash" in s.out
    before, after = first.other.requests[0], s.other.requests[0]
    assert after["model"] == "deepseek-flash"
    assert [message["role"] for message in after["messages"]] == ["system", "user", "assistant", "user"]
    assert after["messages"][:2] == before["messages"]
    # 没有这一家的密钥：不打开（打开了也聊不了，还会把"现在用的模型"记成它，下次启动终端就开不起来），说清楚去哪儿填
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    Settings(s.config.settings_file, s.config.default_provider, s.config.default_model).choose("anthropic", "claude-opus-5-5")
    settings = s.config.settings_file.read_bytes()
    s = terminal(["/history", "/open 1", "再说"], reply(text("这是 Claude。")))
    assert "没有打开：这段会话用的是 DeepSeek，还没有设置 DeepSeek 的 API 密钥" in s.out and "DEEPSEEK_API_KEY=" in s.out
    assert s.other.requests == [] and said(s.api.requests[0]) == ["再说"]  # 还在新的那一段里，用的还是 Claude
    assert s.config.settings_file.read_bytes() == settings


def test_a_persona_that_is_gone_falls_back_with_a_warning(terminal):
    first = terminal(["问一"], reply(text("答一")))
    path = next(first.config.conversations_dir.glob("*.json"))
    data = json.loads(path.read_text(encoding="utf-8"))
    data["agent"] = "审稿人"  # 这段会话是用一个 Agent 当角色开始的，这个 Agent 后来被删了
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    s = terminal(["/open 1", "接着"], reply(text("好。")))
    assert "Agent「审稿人」" in s.out and "用不了" in s.out  # 和网页一样：退回到普通的对话，说一声
    assert said(s.api.requests[0]) == ["问一", "接着"]


# ---- /retry 和 /edit 在打开的会话上 -------------------------------------------------------


def test_retry_and_edit_work_on_an_opened_conversation(terminal):
    first = terminal(["问一", "问二"], reply(text("答一")), reply(text("答二")))
    s = terminal(["/open 1", "/retry", "/edit 问三"], reply(text("新答二")), reply(text("答三")))
    retried, edited = s.api.requests
    assert dumps(retried) == dumps(first.api.requests[1])  # 重新生成：和当初那一次一字不差
    assert said(edited) == ["问一", "问三"] and "问二" not in dumps(edited)
    (conversation,) = saved(s.config)
    assert of(conversation["events"], "user") == ["问一", "问三"] and of(conversation["events"], "text") == ["答一", "答三"]
    assert conversation["messages"] == s.app.agent.messages


def test_editing_the_first_message_of_an_opened_conversation_renames_it(terminal):
    terminal(["说说榴莲"], reply(text("很臭。")))
    s = terminal(["/open 1", "/edit 说说山竹"], reply(text("很甜。")))
    (conversation,) = saved(s.config)
    assert conversation["title"] == "说说山竹" and of(conversation["events"], "user") == ["说说山竹"]
    assert ConversationStore(s.config.conversations_dir).search("榴莲") == []  # 倒回去的那句话也搜不到了


def test_edit_prefills_the_last_message_of_an_opened_conversation(terminal):
    terminal(["问一"], reply(text("答一")))
    s = terminal(["/open 1", "/edit", "问二"], reply(text("答二")), interactive=True)
    assert s.defaults[:3] == ["", "", "问一"]  # /edit 不带文字：把打开的这段会话的最后一条消息填在输入行里
    assert said(s.api.requests[0]) == ["问二"]


# ---- 撤回文件：改动的记录也存进会话 -------------------------------------------------------


def test_file_changes_and_undos_are_saved_for_the_web(terminal, serve):
    s = terminal(
        ["改一下", "/undo"],
        reply(tool_use("toolu_1", "str_replace_based_edit_tool", {"command": "create", "path": "b.txt", "file_text": "新的\n"}), stop_reason="tool_use"),
        reply(text("改好了。")),
        interactive=True,
    )
    assert not (s.config.workspace / "b.txt").exists()  # 撤回了
    (conversation,) = saved(s.config)
    events = conversation["events"]
    # "改了 1 个文件"那一行排在运行记录之后、这一轮结束之前（和网页一样），撤回记在最后
    assert kinds(events)[-4:] == ["trace", "file_changes", "turn_end", "file_undo"]
    changes, undo = events[-3], events[-1]
    assert [item["path"] for item in changes["files"]] == ["b.txt"] and undo["set"] == changes["set"] and undo["action"] == "undo"
    web = serve()
    replayed, state = open_conversation(web, conversation["id"])
    assert "file_changes" in kinds(replayed) and "file_undo" in kinds(replayed)
    assert state["rewind"]["files"] == 0  # 网页也知道这一轮改的文件已经撤回了


# ---- 存不上、退出、信号 -----------------------------------------------------------------


def flaky_save(monkeypatch, failures: int | None):
    """让存盘失败 failures 次（None 是一直失败）。"""
    real = ConversationStore.save
    left = [failures]

    def save(self, conversation, touch=True):
        if left[0] is None or left[0] > 0:
            if left[0] is not None:
                left[0] -= 1
            raise OSError("磁盘满了")
        return real(self, conversation, touch)

    monkeypatch.setattr(ConversationStore, "save", save)


def test_a_failed_save_is_reported_and_tried_again_on_exit(terminal, monkeypatch):
    flaky_save(monkeypatch, 1)
    s = terminal(["问一"], reply(text("答一")))
    assert s.code == 0 and "保存会话失败：磁盘满了" in s.out
    (conversation,) = saved(s.config)  # 按 Ctrl-D 退出之前又存了一次，这次存上了
    assert of(conversation["events"], "text") == ["答一"]


def test_leaving_an_unsaved_conversation_asks_once(terminal, monkeypatch):
    flaky_save(monkeypatch, None)
    s = terminal(["问一", "/new", "/new", "问二", "/exit"], reply(text("答一")), reply(text("答二")))
    assert s.code == 0 and saved(s.config) == []
    out = s.out
    # 第一次 /new：没走，说清楚；再来一次才放弃。/exit 一样（第二次是 Ctrl-D）
    assert out.count("还没能保存（磁盘满了），现在离开会丢掉它的内容") == 2
    assert out.count("已开始新对话。") == 1 and len(s.api.requests) == 2
    assert out.rstrip().endswith("再见。")


def test_sigterm_at_the_prompt_saves_and_quits(terminal, monkeypatch):
    flaky_save(monkeypatch, 1)  # 这一轮结束时没存上：收到 SIGTERM 退出之前再存一次
    previous = signal.getsignal(signal.SIGTERM)
    s = terminal(["问一", lambda: signal.raise_signal(signal.SIGTERM), "问二"], reply(text("答一")), reply(text("不该发出去")))
    assert s.code == 0 and len(s.api.requests) == 1 and s.out.rstrip().endswith("再见。")
    (conversation,) = saved(s.config)
    assert of(conversation["events"], "user") == ["问一"]
    assert signal.getsignal(signal.SIGTERM) is previous  # 退出以后换回原来的处理


def test_sigterm_during_a_turn_stops_it_and_saves_it(terminal, monkeypatch):
    real = TerminalUI.tool_result

    def then_sigterm(self, text, is_error, preview=None):
        real(self, text, is_error, preview)
        signal.raise_signal(signal.SIGTERM)  # 工具刚执行完：和在这一刻按 Ctrl-C 一样，这一轮停下

    monkeypatch.setattr(TerminalUI, "tool_result", then_sigterm)
    s = terminal(["我有什么待办", "不该发出去"], reply(tool_use("toolu_1", "todo_list", {}), stop_reason="tool_use"), reply(text("没有。")))
    assert len(s.api.requests) == 1 and "已中断。" in s.out
    (conversation,) = saved(s.config)
    assert kinds(conversation["events"]) == ["user", "turn_start", "tool_call", "tool_result", "trace", "notice", "turn_end"]
    assert [message["role"] for message in conversation["messages"]] == ["user", "assistant", "user"]  # 工具的结果照样补上了


@pytest.mark.skipif(WINDOWS, reason="Windows 上程序卡在读管道（input）的时候，Ctrl+Break 要等这一次读完才轮得到处理，这里测不了")
def test_a_real_terminal_process_saves_and_stops_on_sigterm(tmp_path):
    # 真的开一个 python -m harness（终端界面），标准输入是管道，模型是假的 Claude API（ANTHROPIC_BASE_URL）
    api = FakeAPI([reply(text("答一"))]).start()
    workspace = tmp_path / "cli-work"
    workspace.mkdir()
    data = tmp_path / "cli-data"
    env = {
        **os.environ,
        "HARNESS_DATA_DIR": str(data),
        "HARNESS_ENV_FILE": str(tmp_path / "no-such.env"),
        "HOME": str(tmp_path / "home"),
        "PYTHONPATH": str(ROOT),
        "PYTHONUNBUFFERED": "1",
        "ANTHROPIC_API_KEY": "test-key",
        "ANTHROPIC_BASE_URL": api.base_url,
        "NO_PROXY": "*",
        "no_proxy": "*",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "harness"], cwd=workspace, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", **own_group(),
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write("问一\n")
        process.stdin.flush()
        seen = []
        deadline = time.monotonic() + 30
        for line in process.stdout:  # 等它答完这一轮
            seen.append(line)
            if "答一" in line or time.monotonic() > deadline:
                break
        assert process.poll() is None, "".join(seen)
        assert instance.holder(data) == instance.Holder(process.pid, "terminal")  # 它占着数据目录
        until(lambda: any((data / "conversations").glob("*.json")), what="这一轮存盘")
        ask_to_exit(process)
        out, _ = process.communicate(timeout=15)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
        api.stop()
    assert process.returncode == 0, out
    assert out.rstrip().endswith("再见。"), out
    (conversation,) = [json.loads(path.read_text(encoding="utf-8")) for path in (data / "conversations").glob("*.json")]
    assert of(conversation["events"], "text") == ["答一"]
    assert not instance.in_use(data)


# ---- 同一个数据目录只给一个智狐用 ----------------------------------------------------------


def test_the_terminal_holds_the_data_folder_while_it_runs(terminal, make_config):
    config = make_config()
    seen = {}

    def look() -> str:
        seen["holder"] = instance.holder(config.data_dir)
        seen["serving"] = instance.serving(config.data_dir)
        seen["web"] = instance.claim(config.data_dir, "web")  # 网页界面这时开不起来
        seen["busy"] = "\n".join(instance.busy_lines(config.data_dir))
        seen["doctor"] = doctor_environment.occupancy(config).checks[0].detail
        return "/exit"

    terminal([look], config=config)
    assert seen["holder"] == instance.Holder(os.getpid(), "terminal") and seen["web"] is None
    assert seen["serving"] is False  # 终端不跑定时任务和聊天通道：harness channels、doctor 不能把它当成"智狐在运行"
    assert "终端界面正开着" in seen["busy"] and "/exit" in seen["busy"]
    assert "终端界面" in seen["doctor"]
    assert not instance.in_use(config.data_dir)  # 退出以后放开


def test_while_another_zhifox_holds_the_folder_nothing_is_written(terminal, make_config):
    # 后台服务开着（它那边可能有聊天通道正在往某段会话里写）：终端照常能聊，但不存盘，也不让 /open
    config = make_config()
    terminal(["说说榴莲"], reply(text("很臭。")), config=config)
    (before,) = saved(config)
    lock = instance.claim(config.data_dir, "service")
    try:
        s = terminal(["/history", "/open 1", "问一"], reply(text("答一")), config=config)
    finally:
        lock.close()
    assert saved(config) == [before]  # 一个字节都没写
    out = s.out
    assert "这段终端对话不会保存" in out and "后台服务" in out
    assert "1. 说说榴莲" in out  # /history 只是读，照样能看
    assert "数据目录正被另一个智狐用着，不能在这里打开会话" in out and "已打开" not in out
    assert "答一" in out  # 聊天照常


def test_help_lists_the_new_commands(terminal):
    s = terminal(["/help"])
    assert "/history" in HELP and "/open" in HELP and "/history" in s.out and "/open" in s.out
