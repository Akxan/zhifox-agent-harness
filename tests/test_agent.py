"""agent 循环的测试：真实的 SDK + 本地的假 API。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fake_api import reply, text, thinking, tool_use

from harness.providers import ProviderError
from harness.providers.claude import blocks_to_keep
from harness.ui import TerminalUI

FILE_TOOL = "str_replace_based_edit_tool"


def tool_results(request: dict) -> list[dict]:
    """一个请求里，最后一条消息中的工具结果。"""
    return [block for block in request["messages"][-1]["content"] if block["type"] == "tool_result"]


def test_plain_answer_sends_expected_request(start):
    h = start(reply(text("你好！")))
    h.agent.run_turn("你好")

    assert h.ui.streamed == [("text", "你好！")]
    (request,) = h.api.requests
    assert request["model"] == "claude-opus-5-5"
    assert request["stream"] is True
    assert request["max_tokens"] == 64000
    # 思考由模型自己决定；block_binding 那一项让"从磁盘读回来的旧会话"接着聊时不会因为思考内容对不上而报错
    assert request["thinking"] == {
        "type": "adaptive",
        "display": "summarized",
        "block_binding": {"prefix_mismatch_behavior": "drop_block"},
    }
    assert request["output_config"] == {"effort": "medium"}
    assert request["cache_control"] == {"type": "ephemeral"}
    assert request["fallbacks"] == "default"
    assert request["context_management"] == {"edits": [{"type": "compact_20260112"}]}
    assert h.api.headers[0]["anthropic-beta"] == (
        "thinking-binding-controls-2026-08-01,server-side-fallback-2026-07-01,compact-2026-01-12"
    )
    (system,) = request["system"]
    assert system["cache_control"] == {"type": "ephemeral"}  # 系统提示词上有缓存标记
    assert "个人助手" in system["text"] and str(h.config.workspace) in system["text"]
    # 联网工具带着每条消息的次数上限，由 API 强制执行
    web_tools = {tool["name"]: tool for tool in request["tools"] if tool["name"].startswith("web_")}
    assert web_tools["web_search"] == {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
    assert web_tools["web_fetch"]["max_uses"] == 5 and web_tools["web_fetch"]["max_content_tokens"] == 8000
    assert [tool["name"] for tool in request["tools"]] == [
        "todo_add",
        "todo_list",
        "todo_update",
        "todo_delete",
        "memory",
        FILE_TOOL,
        "run_command",
        "web_search",
        "web_fetch",
    ]
    assert request["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "<context>当前时间：2026-11-20 14:03 星期五</context>"},
                {"type": "text", "text": "你好"},
            ],
        }
    ]
    assert [message["role"] for message in h.agent.messages] == ["user", "assistant"]


def test_optional_features_can_be_off(start):
    h = start(reply(text("好")), default_model="claude-haiku-5-5", web=False)
    h.agent.run_turn("你好")

    (request,) = h.api.requests
    assert "fallbacks" not in request  # 这个模型不支持 fallback
    assert h.api.headers[0]["anthropic-beta"] == "thinking-binding-controls-2026-08-01,compact-2026-01-12"
    assert "web_search" not in [tool["name"] for tool in request["tools"]]


def test_tool_call_runs_and_result_goes_back(start):
    h = start(
        reply(
            thinking("用户想记一件事"),
            tool_use("toolu_1", "todo_add", {"title": "交报告", "due": "2026-11-27"}),
            stop_reason="tool_use",
        ),
        reply(text("记好了。")),
    )
    h.agent.run_turn("提醒我下周五交报告")

    assert [item["title"] for item in h.todos.items()] == ["交报告"]
    assert h.ui.calls == ["todo_add"]
    assert h.ui.confirmations == []  # 待办不需要确认

    first, second = h.api.requests
    assert [message["role"] for message in second["messages"]] == ["user", "assistant", "user"]
    # 模型的回复被原样发回去：思考块连同签名、工具调用块
    assistant = second["messages"][1]["content"]
    assert assistant[0] == {"type": "thinking", "thinking": "用户想记一件事", "signature": "sig-1"}
    assert assistant[1]["type"] == "tool_use"
    assert assistant[1]["id"] == "toolu_1"
    assert assistant[1]["input"] == {"title": "交报告", "due": "2026-11-27"}
    # 工具结果和调用一一对应
    (result,) = tool_results(second)
    assert result["tool_use_id"] == "toolu_1"
    assert "is_error" not in result
    assert '"title": "交报告"' in result["content"]
    # 只追加、不改动：前面的内容和上一次请求完全一样
    assert second["messages"][0] == first["messages"][0]
    assert second["system"] == first["system"]
    assert second["tools"] == first["tools"]


def test_history_is_append_only_across_turns(start):
    h = start(
        reply(tool_use("toolu_1", "todo_list", {}), stop_reason="tool_use"),
        reply(thinking("没有待办"), text("目前没有待办。")),
        reply(text("不客气。")),
    )
    h.agent.run_turn("我有什么待办？")
    h.agent.run_turn("谢谢")

    _, second, third = h.api.requests
    shared = len(second["messages"])
    assert third["messages"][:shared] == second["messages"]
    assert [message["role"] for message in third["messages"][shared:]] == ["assistant", "user"]
    assert third["system"] == second["system"]
    assert third["tools"] == second["tools"]


def test_denied_command_is_not_run(start):
    h = start(
        reply(tool_use("toolu_1", "run_command", {"command": "touch created.txt"}), stop_reason="tool_use"),
        reply(text("好的，不创建。")),
        approve=False,
        feedback="不要创建文件",
    )
    h.agent.run_turn("建个文件")

    assert not (h.config.workspace / "created.txt").exists()
    assert "touch created.txt" in h.ui.confirmations[0]
    (result,) = tool_results(h.api.requests[1])
    assert result["is_error"] is True
    assert "用户拒绝了这个操作" in result["content"]
    assert "不要创建文件" in result["content"]


def test_approved_command_runs_in_workspace(start):
    h = start(
        reply(tool_use("toolu_1", "run_command", {"command": "touch created.txt && pwd"}), stop_reason="tool_use"),
        reply(text("建好了。")),
    )
    h.agent.run_turn("建个文件")

    assert (h.config.workspace / "created.txt").exists()
    (result,) = tool_results(h.api.requests[1])
    assert result["content"].endswith("work")
    assert "is_error" not in result


def test_reading_asks_only_outside_workspace(start, tmp_path):
    h = start(
        reply(
            tool_use("toolu_1", FILE_TOOL, {"command": "view", "path": "notes.txt"}),
            tool_use("toolu_2", FILE_TOOL, {"command": "view", "path": str(tmp_path / "outside.txt")}),
            stop_reason="tool_use",
        ),
        reply(text("看完了。")),
    )
    (h.config.workspace / "notes.txt").write_text("里面的内容", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("外面的内容", encoding="utf-8")
    h.agent.run_turn("看看这两个文件")

    assert len(h.ui.confirmations) == 1
    assert "outside.txt" in h.ui.confirmations[0]
    inside, outside = tool_results(h.api.requests[1])
    assert "里面的内容" in inside["content"]
    assert "外面的内容" in outside["content"]


def test_writing_a_file_asks_first(start):
    h = start(
        reply(
            tool_use("toolu_1", FILE_TOOL, {"command": "create", "path": "a.txt", "file_text": "第一行"}),
            stop_reason="tool_use",
        ),
        reply(text("好的。")),
        approve=False,
    )
    h.agent.run_turn("写个文件")

    assert not (h.config.workspace / "a.txt").exists()
    assert "创建文件：a.txt" in h.ui.confirmations[0]
    assert "第一行" in h.ui.confirmations[0]


# ---- 自动允许 -------------------------------------------------------------
# 用户可以放宽"问不问"（见 permissions.py 的 MODES）。下面三个测试看的是循环里实际发生了什么。


def test_edits_mode_changes_workspace_files_without_asking(start, tmp_path):
    h = start(
        reply(
            tool_use("toolu_1", FILE_TOOL, {"command": "create", "path": "a.txt", "file_text": "第一行"}),
            tool_use("toolu_2", FILE_TOOL, {"command": "str_replace", "path": "a.txt", "old_str": "第一行", "new_str": "改过了"}),
            tool_use("toolu_3", "run_command", {"command": "touch b.txt"}),
            tool_use("toolu_4", FILE_TOOL, {"command": "create", "path": str(tmp_path / "outside.txt"), "file_text": "x"}),
            stop_reason="tool_use",
        ),
        reply(text("好的。")),
        approve=False,  # 凡是问到用户的，一律拒绝：这样"问没问"从结果上就看得出来
    )
    assert h.agent.permissions.mode == "ask"  # 每次启动都从"每次都问"开始
    h.agent.permissions.mode = "edits"
    h.agent.run_turn("改几个文件")

    # 工作目录里的文件：没有问就改了，并且告诉了界面"这两次是自动放行的"
    assert (h.config.workspace / "a.txt").read_text(encoding="utf-8") == "改过了"
    assert h.ui.auto_allowed_count == 2
    # 执行命令、写工作目录之外的文件：照常先问
    assert len(h.ui.confirmations) == 2
    assert "touch b.txt" in h.ui.confirmations[0] and "outside.txt" in h.ui.confirmations[1]
    assert not (h.config.workspace / "b.txt").exists() and not (tmp_path / "outside.txt").exists()
    assert [result.get("is_error", False) for result in tool_results(h.api.requests[1])] == [False, False, True, True]


def test_all_mode_asks_nothing_but_the_key_file_stays_off_limits(start):
    h = start(
        reply(
            tool_use("toolu_1", "run_command", {"command": "touch made.txt"}),
            tool_use("toolu_2", FILE_TOOL, {"command": "view", "path": ".env"}),
            tool_use("toolu_3", FILE_TOOL, {"command": "create", "path": ".env", "file_text": "KEY=replaced"}),
            stop_reason="tool_use",
        ),
        reply(text("好的。")),
        approve=False,
    )
    (h.config.workspace / ".env").write_text("ANTHROPIC_API_KEY=the-real-one", encoding="utf-8")
    h.agent.permissions.mode = "all"
    h.agent.run_turn("随便做")

    assert h.ui.confirmations == []  # 一次都没有问
    assert (h.config.workspace / "made.txt").exists()
    command, read, write = tool_results(h.api.requests[1])
    assert "is_error" not in command
    # 保存密钥的文件是工具自己拒绝的，和"问不问"无关：全部放开之后照样读不了、改不了
    for result in (read, write):
        assert result["is_error"] is True and "密钥" in result["content"] and "the-real-one" not in result["content"]
    assert (h.config.workspace / ".env").read_text(encoding="utf-8") == "ANTHROPIC_API_KEY=the-real-one"
    assert "the-real-one" not in json.dumps(h.api.requests, ensure_ascii=False)  # 密钥没有被发给模型


def test_the_assistants_own_data_is_only_opened_without_asking_when_everything_is_allowed(start, tmp_path):
    data_dir = tmp_path / "work" / "data"  # 像在项目目录里运行时那样：数据目录就在工作目录里面

    def turn(number: int) -> dict:
        return reply(
            tool_use(f"toolu_{number}a", FILE_TOOL, {"command": "view", "path": "data/old-conversation.json"}),
            tool_use(f"toolu_{number}b", FILE_TOOL, {"command": "create", "path": f"data/new-{number}.txt", "file_text": "x"}),
            stop_reason="tool_use",
        )

    h = start(turn(1), reply(text("好")), turn(2), reply(text("好")), turn(3), reply(text("好")), data_dir=data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "old-conversation.json").write_text("以前聊过的内容", encoding="utf-8")

    counted = {}
    for mode in ("ask", "edits", "all"):
        h.agent.permissions.mode = mode
        asked, allowed = len(h.ui.confirmations), h.ui.auto_allowed_count
        h.agent.run_turn("看看数据目录")
        counted[mode] = (len(h.ui.confirmations) - asked, h.ui.auto_allowed_count - allowed)
    # （问了几次，自动放行了几次）。"改文件不用问"说的是工作目录里用户自己的文件，不包括助手存的数据
    assert counted == {"ask": (2, 0), "edits": (2, 0), "all": (0, 2)}
    assert "助手自己保存的数据" in h.ui.confirmations[0]


def test_tool_errors_go_back_to_the_model(start):
    h = start(
        reply(
            tool_use("toolu_1", "todo_update", {"id": 99, "done": True}),
            tool_use("toolu_2", "todo_add", {"title": 5}),
            tool_use("toolu_3", "no_such_tool", {}),
            stop_reason="tool_use",
        ),
        reply(text("出了点问题。")),
    )
    h.agent.run_turn("把 99 号标成完成")

    missing, invalid, unknown = tool_results(h.api.requests[1])
    assert missing["is_error"] is True and "没有编号为 99 的待办" in missing["content"]
    assert invalid["is_error"] is True and "INVALID_JSON" in invalid["content"]
    assert unknown["is_error"] is True and "no_such_tool" in unknown["content"]
    assert h.todos.items("all") == []
    assert h.ui.streamed[-1] == ("text", "出了点问题。")  # 循环没有中断，模型继续回答了


def test_refusal_rolls_back_the_whole_turn(start):
    h = start(
        reply(text("你好。")),
        reply(stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": "x"}),
        reply(text("当然。")),
    )
    h.agent.run_turn("你好")
    before = list(h.agent.messages)
    h.agent.run_turn("一个会被拒绝的请求")

    assert h.agent.messages == before
    assert "拒绝" in h.ui.infos[-1] and "cyber" in h.ui.infos[-1]
    # 之后可以正常继续对话
    h.agent.run_turn("换个问题")
    assert [m["role"] for m in h.api.requests[2]["messages"]] == ["user", "assistant", "user"]


def test_truncated_tool_call_is_not_executed(start):
    h = start(
        reply(tool_use("toolu_1", "run_command", {"command": "touch created.txt"}), stop_reason="max_tokens"),
        reply(text("重来。")),
    )
    h.agent.run_turn("建个文件")

    assert not (h.config.workspace / "created.txt").exists()
    assert h.ui.confirmations == []
    (result,) = tool_results(h.api.requests[1])
    assert result["is_error"] is True and "截断" in result["content"]


def test_pause_turn_resends_without_extra_message(start):
    h = start(reply(text("正在搜索"), stop_reason="pause_turn"), reply(text("搜完了。")))
    h.agent.run_turn("搜一下")

    assert [message["role"] for message in h.api.requests[1]["messages"]] == ["user", "assistant"]
    assert [message["role"] for message in h.agent.messages] == ["user", "assistant", "assistant"]


def test_step_limit_stops_the_loop(start):
    looping = [reply(tool_use(f"toolu_{n}", "todo_list", {}), stop_reason="tool_use") for n in range(5)]
    h = start(*looping, max_steps=3)
    h.agent.run_turn("一直查")

    assert len(h.api.requests) == 3
    assert "3 次" in h.ui.infos[-1]
    assert h.agent.messages[-1]["role"] == "user"  # 历史以工具结果结尾，下一轮可以接着说


def test_api_error_withdraws_the_user_message(start):
    h = start()  # 没有预设回复：假 API 会返回 500
    with pytest.raises(ProviderError, match="500"):
        h.agent.run_turn("你好")
    assert h.agent.messages == []


def test_search_limit_can_be_changed_or_removed(start):
    h = start(reply(text("好")), max_searches=2)
    h.agent.run_turn("你好")
    assert [t.get("max_uses") for t in h.api.requests[0]["tools"] if t["name"].startswith("web_")] == [2, 2]

    h = start(reply(text("好")), max_searches=0)  # 0 表示不限制
    h.agent.run_turn("你好")
    assert all("max_uses" not in t for t in h.api.requests[0]["tools"])


def test_interrupt_during_tools_keeps_history_valid(start):
    class InterruptingUI(type(start().ui)):
        def confirm(self, description):
            raise KeyboardInterrupt

    h = start(
        reply(
            tool_use("toolu_1", "run_command", {"command": "touch a.txt"}),
            tool_use("toolu_2", "run_command", {"command": "touch b.txt"}),
            stop_reason="tool_use",
        ),
        reply(text("好的，停下了。")),
        ui=InterruptingUI(),
    )
    with pytest.raises(KeyboardInterrupt):
        h.agent.run_turn("建两个文件")

    assert list(h.config.workspace.iterdir()) == []
    # 两个工具调用都有对应的结果，历史仍然合法
    last = h.agent.messages[-1]["content"]
    assert [result["tool_use_id"] for result in last] == ["toolu_1", "toolu_2"]
    assert all(result["is_error"] for result in last)
    # 之后可以继续对话
    h.agent.run_turn("算了")
    assert [m["role"] for m in h.api.requests[1]["messages"]] == ["user", "assistant", "user", "user"]


def test_fallback_boundary_survives_the_round_trip(start):
    boundary = {
        "type": "fallback",
        "from": {"model": "claude-opus-5-5"},
        "to": {"model": "claude-opus-5"},
        "trigger": {"type": "refusal"},
    }
    h = start(
        reply(
            thinking("被拦下的模型的思考", signature="sig-old"),
            text("前半句"),
            boundary,
            thinking("后备模型的思考", signature="sig-new"),
            tool_use("toolu_1", "todo_list", {}),
            stop_reason="tool_use",
        ),
        reply(text("完成。")),
    )
    h.agent.run_turn("查一下待办")

    assistant = h.api.requests[1]["messages"][1]["content"]
    assert [block["type"] for block in assistant] == ["text", "fallback", "thinking", "tool_use"]
    assert assistant[1]["from"] == {"model": "claude-opus-5-5"}
    assert assistant[1]["to"] == {"model": "claude-opus-5"}
    assert assistant[2]["signature"] == "sig-new"
    # 历史里存的是普通的字典，样子和发给 API 的一模一样（键名是 "from"，不是 SDK 对象上的 from_），
    # 所以写进会话文件再读回来，还能原样发回去
    assert json.loads(json.dumps(h.agent.messages[1]["content"])) == assistant


def test_blocks_to_keep_drops_unfinished_work_before_fallback():
    def block(type: str, **fields) -> SimpleNamespace:
        return SimpleNamespace(type=type, **fields)

    content = [
        block("thinking"),
        block("text"),
        block("tool_use", id="t1"),
        block("server_tool_use", id="s1"),  # 没有结果
        block("server_tool_use", id="s2"),
        block("web_search_tool_result", tool_use_id="s2"),
        block("fallback"),
        block("thinking"),
        block("tool_use", id="t2"),
    ]
    kept = blocks_to_keep(content)
    assert [b.type for b in kept] == [
        "text",
        "server_tool_use",
        "web_search_tool_result",
        "fallback",
        "thinking",
        "tool_use",
    ]
    assert kept[1].id == "s2" and kept[-1].id == "t2"
    # 没有 fallback 时原样保留
    assert blocks_to_keep(content[:3]) == content[:3]


def test_terminal_ui_renders_a_turn(start, capsys):
    h = start(
        reply(
            thinking("先查一下"),
            tool_use("toolu_1", "run_command", {"command": "ls"}),
            stop_reason="tool_use",
        ),
        reply(text("无法执行。")),
        ui=TerminalUI(),
    )
    h.agent.run_turn("列出文件")

    out = capsys.readouterr().out
    assert "💭 先查一下" in out
    assert "🔧 run_command $ ls" in out
    assert "助手想要执行命令" in out
    assert "无法执行。" in out
    # 测试环境不是交互式终端，没有人能确认，所以命令一律被拒绝
    (result,) = tool_results(h.api.requests[1])
    assert result["is_error"] is True


def test_usage_is_accumulated(start):
    h = start(reply(tool_use("toolu_1", "todo_list", {}), stop_reason="tool_use"), reply(text("没有。")))
    h.agent.run_turn("待办？")

    usage = h.agent.usage
    assert (usage.requests, usage.input_tokens, usage.output_tokens) == (2, 200, 40)
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (80, 20)
    assert usage.context_tokens == 100 + 40 + 10 + 20  # 最近一次请求的输入加输出
    assert h.agent.usage_by_model["claude-opus-5-5"].requests == 2
