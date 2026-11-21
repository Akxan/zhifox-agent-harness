"""终端里的 /retry 和 /edit（见 harness/cli.py 的 rewind_last）：和网页一样只倒最后一轮，这一轮做过可能改动电脑的事要先问。

主循环 run_terminal 照常跑：输入换成写好的几行，模型是本地的假 API（不花钱）。测试里不是交互式终端，
所以"要继续吗"一律当作不继续；要测回答"继续"的情况，就把界面当作交互式的，并替用户答一句 y。
"""

from __future__ import annotations

import json
from datetime import datetime

import anthropic
import pytest
from fake_api import FakeAPI, failure, reply, text, tool_use

import harness.ui as terminal_ui
from harness import cli
from harness.app import build_app
from harness.cli import HELP
from harness.ui import TerminalUI

NOW = datetime(2026, 11, 20, 14, 3)


@pytest.fixture
def terminal(make_config, monkeypatch, capsys):
    """在终端里依次输入这几行（最后按 Ctrl-D 退出），跑完主循环。返回（假 API，屏幕上的输出，每次读输入时预先填好的文字）。

    interactive：当作交互式终端，问"继续吗"时替用户回答 y。
    """
    apis: list[FakeAPI] = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def run(lines: list[str], *replies, interactive: bool = False):
        api = FakeAPI(list(replies)).start()
        apis.append(api)
        client = anthropic.Anthropic(api_key="test-key", base_url=api.base_url, max_retries=0)

        def assemble(config, ui):
            app = build_app(config, ui, client_factory=lambda provider: client)
            app.agent.clock = lambda: NOW  # 钟固定住：两次请求"一字不差"才比得了
            return app

        monkeypatch.setattr(cli, "build_app", assemble)
        typed = iter([*lines, None])  # None：用户按了 Ctrl-D
        defaults: list[str] = []

        def read_input(self, default: str = "") -> str | None:
            defaults.append(default)
            return next(typed)

        monkeypatch.setattr(TerminalUI, "read_input", read_input)
        if interactive:
            real_init = TerminalUI.__init__

            def init(self, *args, **kwargs):
                real_init(self, *args, **kwargs)
                self._interactive = True

            monkeypatch.setattr(TerminalUI, "__init__", init)
            monkeypatch.setattr(terminal_ui, "ask", lambda prompt: "y")
        assert cli.run_terminal(make_config()) == 0
        return api, capsys.readouterr().out, defaults

    yield run
    for api in apis:
        api.stop()


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def said(request: dict) -> list[str]:
    """请求里每条用户消息里用户自己写的那句话（工具结果不算）。"""
    return [
        message["content"][-1]["text"]
        for message in request["messages"]
        if message["role"] == "user" and message["content"][-1].get("type") == "text"
    ]


def test_retry_regenerates_the_last_answer(terminal):
    api, out, _ = terminal(["问一", "问二", "/retry"], reply(text("答一")), reply(text("旧答二")), reply(text("新答二")))
    assert len(api.requests) == 3
    assert dumps(api.requests[2]) == dumps(api.requests[1])  # 用户消息原样再发一次，旧的回答不在里面
    assert "旧答二" not in dumps(api.requests[2]) and "新答二" in out


def test_edit_replaces_the_last_message(terminal):
    api, out, _ = terminal(
        ["问一", "说说榴莲", "/edit   说说山竹 "], reply(text("答一")), reply(text("榴莲很臭。")), reply(text("山竹很甜。"))
    )
    edited = api.requests[2]
    assert dumps(edited["messages"][:2]) == dumps(api.requests[1]["messages"][:2])  # 前面的一条都没动
    assert said(edited) == ["问一", "说说山竹"] and "榴莲" not in dumps(edited)
    assert "山竹很甜。" in out


def test_retry_and_edit_follow_the_latest_turn(terminal):
    # 每次重新生成、编辑之后，"最后一轮"就是新的那一轮：可以接着再来
    api, _, _ = terminal(
        ["问一", "/retry", "/edit 新问", "/retry"],
        reply(text("一")), reply(text("一又")), reply(text("新答")), reply(text("新答又")),
    )  # fmt: skip
    first, retried, edited, again = api.requests
    assert dumps(retried) == dumps(first)
    assert said(edited) == ["新问"] and len(edited["messages"]) == 1  # 编辑第一条消息：前面什么都没有
    assert dumps(again) == dumps(edited)


def test_a_turn_that_changed_something_asks_first(terminal):
    # 这一轮记了一条待办（不用确认，但它改动了东西）。不是交互式终端，问"继续吗"等于不继续：什么都不做
    api, out, _ = terminal(
        ["记一下买牛奶", "/retry", "/edit 别记了"],
        reply(tool_use("toolu_1", "todo_add", {"title": "买牛奶"}), stop_reason="tool_use"),
        reply(text("记下了。")),
    )
    assert len(api.requests) == 2
    assert out.count("这一轮做过可能改动了电脑的事") == 2 and out.count("已取消。") == 2
    assert 'todo_add {"title": "买牛奶"}' in out and "这些已经做了的事不会撤销" in out


def test_agreeing_goes_ahead_and_edit_prefills_the_last_message(terminal):
    # 交互式终端：/edit 不带文字时，把上一条消息填在输入行里（default），改完回车；问"继续吗"时回答 y
    api, _, defaults = terminal(
        ["记一下买榴莲", "/edit", "记一下买山竹"],
        reply(tool_use("toolu_1", "todo_add", {"title": "买榴莲"}), stop_reason="tool_use"),
        reply(text("记下了榴莲。")),
        reply(text("记下了山竹。")),
        interactive=True,
    )
    assert defaults == ["", "", "记一下买榴莲", ""]
    assert len(api.requests) == 3 and said(api.requests[2]) == ["记一下买山竹"]
    assert "toolu_1" not in dumps(api.requests[2])


def test_a_denied_command_is_not_an_effect(terminal):
    # 不是交互式终端，命令没人确认，被拒绝了：没有执行，/retry 不用问
    api, out, _ = terminal(
        ["建个文件", "/retry"],
        reply(tool_use("toolu_1", "run_command", {"command": "touch made.txt"}), stop_reason="tool_use"),
        reply(text("好的，不建。")),
        reply(text("新的回答。")),
    )
    assert len(api.requests) == 3 and dumps(api.requests[2]) == dumps(api.requests[0])
    assert "这一轮做过可能改动了电脑的事" not in out and "新的回答。" in out


def test_a_failed_turn_is_sent_again(terminal):
    # 在模型答出第一步之前就失败了：消息已经撤回，/retry 就是同样的话再发一次
    api, out, _ = terminal(["问一", "/retry"], failure(529, "overloaded_error", "Overloaded"), reply(text("答一")))
    assert len(api.requests) == 2 and dumps(api.requests[1]) == dumps(api.requests[0])
    assert "答一" in out


def test_a_failed_regeneration_can_be_tried_again(terminal):
    # 重新生成失败了（模型一步都没答出来）：这条消息撤回了，下一次 /retry 是同样的话再发一次
    api, out, _ = terminal(
        ["问一", "/retry", "/retry"], reply(text("答一")), failure(529, "overloaded_error", "Overloaded"), reply(text("答一又"))
    )
    first, failed, again = api.requests
    assert dumps(failed) == dumps(first) and dumps(again) == dumps(first)
    assert "答一又" in out


def test_nothing_to_rewind(terminal):
    api, out, _ = terminal(
        ["/retry", "/edit 新的", "问一", "/edit", "/edit 问一", "/new", "/retry"], reply(text("答一"))
    )
    assert len(api.requests) == 1
    assert out.count("这段对话里还没有可以重新生成或编辑的消息。") == 3  # 还没说过话、/new 之后
    assert "用法：/edit 新的文字" in out  # 不是交互式终端，没法把上一条填进输入行
    assert "没有改动，已取消。" in out
    assert "/retry" in HELP and "/edit" in HELP
