"""运行记录（harness/trace/）的测试：一轮对话里每一次请求、每一次工具调用的时刻和数字，记得对不对、存得下来、不碰历史。

和别的测试一样，走的是真实的 SDK 连本地的假服务器（fake_api.py、fake_openai.py），不花钱。要验证的是：

  时间线    用一块假的表（Clock）代替 time.monotonic：只有测试让它走它才走——假 API 收到请求时走一段（"等模型开口"），
            界面收到一段文字时走一段（"模型在写"），待办工具干活时走一段，你考虑要不要允许时走一段。
            这样每一段的 start / end / first / asked / waited 和合计里的时间分配，都能算出确切的毫秒数来比。
            为什么不用真的时间：真的时间每次都不一样，只能断言"大于 0"，测不出算错了一半这种错。
  模型请求  哪一家、哪个模型、为什么停下、四种 token、钱（必须就是账本里记的那一笔：钱只在 ledger.py 算一次）、
            服务器上的联网搜索、压缩历史（GPT 这一类的写摘要，和 Claude 回复里的压缩块）、工具参数不是 JSON 时的重试。
  工具调用  给人看的名字（和信息栏是同一张表）、参数的一行说明、结果、问没问你（允许、拒绝、没问就放行和为什么）。
  子 Agent  它的请求和工具调用挂在那次 agent 调用下面，时刻在那一段里面，合计里也算着；它自己的记录不另外交出去。
  中断      停在确认上、模型出错：还开着的那一段按此刻收尾，标成 stopped / error。
  历史      记录从不进 messages：有没有运行记录，发给模型的请求一个字节都不差。
  存下来    网页、定时任务、聊天通道的每一轮都是会话 events 里的一个 trace 事件：存盘、重新打开、倒回（重新生成）都对得上。
  终端      /trace 写成几行字，跟着界面语言走。
  上限      段太多时只存前面的，omitted 说少了几段，合计照样全算。
"""

from __future__ import annotations

import json
import re

import pytest
import test_rewind_web
import test_task_runs
import test_webui
from conftest import RecordingUI
from fake_api import failure, reply, text, thinking, tool_use, web_search
from fake_openai import chat
from sample_files import PNG, PNG_BYTES
from test_agents import install
from test_channels import rigs
from test_compaction import BIG, SUMMARY_USAGE
from test_webui import latest_state, open_conversation, saved_file

from harness import i18n
from harness.cli import HELP, handle_command
from harness.providers import ProviderError
from harness.providers.base import Step, ToolCall, Usage
from harness.tools.todo import TodoStore
from harness.trace import Tracer
from harness.trace import recorder as trace_recorder
from harness.trace.text import lines
from harness.ui import TerminalUI

FILE_TOOL = "str_replace_based_edit_tool"
AT = 1763622180.125  # 这一轮"开始的时刻"（假的钟表时间），记录里原样写着
# 假的表每一步走多少秒。都选 2 的负几次方，二进制里是精确的，加起来不会有 0.30000000000000004 这种零头
LATENCY = 0.25  # 假 API 收到请求到开始回答：首字时间
STREAM = 0.5  # 界面收到一段思考或文字：模型在写
TOOL = 0.125  # 待办工具干活
PONDER = 2.0  # 你看着确认卡片想了一会儿
ONE_REQUEST = round((100 * 4 + 40 * 0.2 + 10 * 5 + 20 * 20) / 1_000_000, 6)  # 假 API 每次回复的用量，按 claude-opus-5-5 的价格
CJK = re.compile(r"[㐀-鿿＀-￯　-〿]")

# 借用别的测试文件里的组装（fixture）：网页服务器（test_webui）、带固定钟的网页服务器（test_rewind_web）、
# 定时任务（test_task_runs）、Telegram 聊天通道（test_channels）
serve = test_webui.serve
rig = test_rewind_web.rig
task_serve = test_task_runs.serve


@pytest.fixture
def bot(make_config, monkeypatch):
    yield from rigs(make_config, monkeypatch)


class Clock:
    """假的表（秒）：只有测试让它走，它才走。"""

    def __init__(self, now: float = 100.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TimedUI(RecordingUI):
    """测试用的界面，外加在几个时刻让假的表走一段：收到一段思考或文字（模型在写）、要你确认（你在想）。"""

    def __init__(self, clock: Clock, approve: bool = True, feedback: str = ""):
        super().__init__(approve, feedback)
        self.clock = clock

    def thinking(self, text: str) -> None:
        super().thinking(text)
        self.clock.advance(STREAM)

    def text(self, text: str) -> None:
        super().text(text)
        self.clock.advance(STREAM)

    def confirm(self, description: str, agent: str = "") -> tuple[bool, str]:
        self.clock.advance(PONDER)
        return super().confirm(description, agent)


class StopAtConfirmUI(TimedUI):
    """你看了一会儿确认卡片，然后点了"停止"（终端里是 Ctrl-C）。"""

    def confirm(self, description: str, agent: str = "") -> tuple[bool, str]:
        self.clock.advance(PONDER)
        raise KeyboardInterrupt


def timed(start, monkeypatch, *replies, ui_class=TimedUI, approve: bool = True, feedback: str = "", **options):
    """组装一个助手，换上假的表，把每一轮的运行记录收进 h.records（on_trace 每一轮调用一次）。"""
    clock = Clock()
    h = start(*replies, ui=ui_class(clock, approve, feedback), **options)
    h.clock = clock
    h.agent.trace.timer = clock
    h.agent.trace.wall = lambda: AT
    real = h.api.next_reply

    def slow(body: dict) -> dict | None:  # 假 API 收到请求以后、发出第一个字节之前：模型在"想"
        clock.advance(LATENCY)
        return real(body)

    h.api.next_reply = slow
    real_items = TodoStore.items

    def items(self, status: str = "pending") -> list[dict]:  # todo_list 干活的那一会儿
        clock.advance(TOOL)
        return real_items(self, status)

    monkeypatch.setattr(TodoStore, "items", items)
    h.records = []
    h.agent.on_trace = h.records.append
    return h


def tool_results(request: dict) -> list[dict]:
    """一个请求里，最后一条消息中的工具结果。"""
    return [block for block in request["messages"][-1]["content"] if block["type"] == "tool_result"]


def result_text(block: dict) -> str:
    content = block["content"]
    return content if isinstance(content, str) else "".join(part.get("text", "") for part in content)


def ledger_rows(config) -> list[dict]:
    return [json.loads(line) for line in config.ledger_file.read_text(encoding="utf-8").splitlines()]


def flat(spans: list[dict]) -> list[dict]:
    """整棵树按先后顺序摊平（子 Agent 的那几段跟在它那次调用后面）。"""
    out = []
    for span in spans:
        out.append(span)
        out += flat(span.get("children") or [])
    return out


def model_span(start: int, end: int, status: str, stop: str, cost: float, **extra) -> dict:
    """一段 Claude 的请求在记录里的样子（假 API 每次的用量都一样）。"""
    return {
        "kind": "model", "start": start, "end": end, "provider": "anthropic", "model": "claude-opus-5-5",
        "status": status, "stop": stop, "input": 100, "cache_read": 40, "cache_write": 10, "output": 20, "cost": cost, **extra,
    }  # fmt: skip


def assert_adds_up(record: dict, slack: int = 0) -> None:
    """合计里的时间分配（模型、工具、等你、其他）加起来是这一轮的总时长。"""
    totals = record["totals"]
    parts = totals["model"] + totals["tools"] + totals["waiting"] + totals["other"]
    assert abs(parts - record["wall"]) <= slack, (parts, record["wall"])


# ---- 时间线：用假的表算出每一个毫秒 -------------------------------------------------


def test_the_timeline_of_a_turn_to_the_millisecond(start, monkeypatch):
    h = timed(
        start, monkeypatch,
        reply(thinking("先看看待办"), tool_use("toolu_1", "todo_list", {}), stop_reason="tool_use"),
        reply(tool_use("toolu_2", "run_command", {"command": "echo hi"}), stop_reason="tool_use"),
        reply(text("你没有待办，命令也跑完了。")),
    )  # fmt: skip
    h.agent.run_turn("看看待办，再跑个命令")

    (record,) = h.records  # 一轮只交一次
    assert record is h.agent.trace.last  # 终端的 /trace 看的就是它
    (todos,) = tool_results(h.api.requests[1])
    (command,) = tool_results(h.api.requests[2])
    costs = [row["cost"] for row in ledger_rows(h.config)]
    assert costs == [ONE_REQUEST] * 3
    # 一步一步算：
    #   0     发出请求 1；假 API 过 250 毫秒开口（首字 250），思考写了 500 毫秒，750 时答完：要调用 todo_list
    #   750   todo_list 干了 125 毫秒
    #   875   请求 2：250 毫秒后直接是工具调用，一个字都没写（没有首字时间）
    #   1125  run_command 要先问你：你想了 2 秒，允许了；命令本身没让假的表走
    #   3125  请求 3：首字 250，正文写了 500 毫秒，3875 时这一轮结束
    assert record == {
        "version": 1,
        "at": AT,
        "wall": 3875,
        "ended": "done",
        "spans": [
            model_span(0, 750, "tools", "tool_use", costs[0], first=250, calls=1),
            {"kind": "tool", "start": 750, "end": 875, "name": "todo_list", "group": "todo", "icon": "todo", "label": "待办",
             "detail": "", "result": "ok", "size": len(result_text(todos))},
            model_span(875, 1125, "tools", "tool_use", costs[1], calls=1),
            {"kind": "tool", "start": 1125, "end": 3125, "name": "run_command", "group": "shell", "icon": "terminal",
             "label": "执行命令", "detail": "$ echo hi", "result": "ok", "size": len(result_text(command)),
             "approval": "allowed", "asked": 1125, "waited": 2000},
            model_span(3125, 3875, "done", "end_turn", costs[2], first=250),
        ],
        "totals": {
            "model": 750 + 250 + 750, "tools": 125, "waiting": 2000, "other": 0,
            "requests": 3, "tool_calls": 2, "errors": 0, "cost": round(sum(costs), 6), "unpriced": 0,
            "input": 300, "cache_read": 120, "cache_write": 30, "output": 60, "cache_hit": round(120 / 450, 4),
            "server_tools": {}, "asked": 1, "allowed": 1, "refused": 0, "auto": 0,
        },
    }  # fmt: skip
    assert_adds_up(record)
    # 记录是普通的 JSON：原样写得进会话文件
    assert json.loads(json.dumps(record)) == record


def test_each_turn_starts_its_own_clock(start, monkeypatch):
    # 第二轮的时刻从第二轮开始算，不接着第一轮的往下数；上一轮的段也不会留到下一轮
    h = timed(start, monkeypatch, reply(text("一")), reply(text("二")))
    h.agent.run_turn("第一轮")
    h.clock.advance(60)  # 两轮之间你过了一分钟才说话
    h.agent.run_turn("第二轮")
    first, second = h.records
    for record in (first, second):
        assert record["wall"] == 750 and [(span["start"], span["end"], span["first"]) for span in record["spans"]] == [(0, 750, 250)]
    assert h.agent.trace.last is second


def test_totals_add_up_to_the_wall_time_even_with_fractions_of_a_millisecond():
    # 合计说"model、tools、waiting、other 加起来是 wall"。每一项各自四舍五入成毫秒以后，零头可能对不上：
    # 模型 1.4 毫秒、工具 1.4 毫秒，一共 2.8 毫秒——各自写成 1、1，总共写成 3，"其他"是 0，加起来差 1。
    # 页面按这几项画一条分段的横条，加起来不等于总长，横条就画不满（或者画出头）
    clock = Clock(0.0)
    tracer = Tracer(timer=clock, wall=lambda: AT)
    tracer.turn_began()
    tracer.request_began("anthropic", "claude-opus-5-5")
    clock.advance(0.0014)
    tracer.request_ended(Step(status="tools", tool_calls=[ToolCall("t1", "todo_list", {})], usage=Usage(requests=1)), 0.0)
    tracer.tool_began(ToolCall("t1", "todo_list", {}), None)
    clock.advance(0.0014)
    tracer.tool_ended("[]", False)
    record = tracer.turn_ended("done")
    assert_adds_up(record)


# ---- 模型请求的各项 ---------------------------------------------------------------


def test_a_web_search_is_counted_on_its_request_and_in_the_totals(start, monkeypatch):
    h = timed(start, monkeypatch, reply(*web_search("srvtoolu_1", "今天的天气", [("https://example.com/w", "天气")]), text("晴。")))
    h.agent.run_turn("今天天气怎么样")
    (record,) = h.records
    (span,) = record["spans"]
    assert span["server_tools"] == {"web_search": 1} and record["totals"]["server_tools"] == {"web_search": 1}
    # 首字时间从"开始搜索"那一刻算：服务器端的工具开始了，也算模型开始输出了
    assert span["first"] == 250 and span["status"] == "done" and span["stop"] == "end_turn"
    assert "calls" not in span  # 搜索是服务器上做的，不是要这台电脑调用的工具


def test_a_retry_after_unparsable_tool_json_is_noted(start, monkeypatch):
    # 模型写出的工具参数不是合法的 JSON，SDK 解析不了：适配层原样再请求一次（claude.py 的 _send），记录里写着重试过一次
    h = timed(
        start, monkeypatch,
        reply(tool_use("toolu_1", "todo_list", {}, raw="{这不是 JSON"), stop_reason="tool_use"),
        reply(text("好的。")),
    )  # fmt: skip
    h.agent.run_turn("看看待办")
    (record,) = h.records
    (span,) = record["spans"]
    assert span["retries"] == 1 and span["status"] == "done" and len(h.api.requests) == 2
    # 两次 HTTP 请求在记录里是一段：重试是适配层里的事。时间从第一次发出去算起
    # 两次各等了 250 毫秒才开口（第一次开口就是坏的参数，没写出字），第二次的正文写了 500 毫秒
    assert (span["start"], span["end"], span["first"]) == (0, 1000, 500)
    assert record["totals"]["requests"] == 1
    assert span["cost"] == ledger_rows(h.config)[0]["cost"]  # 失败的那一次没有用量，账本也只有一笔


def test_a_compaction_block_in_a_claude_reply_is_marked(start, monkeypatch):
    # Claude 的压缩在服务器上做（见 providers/claude.py）：回复里多一个 compaction 块，这次请求标上 compaction
    h = timed(start, monkeypatch, reply({"type": "compaction", "content": "前面聊了很多。"}, text("接着说。")))
    h.agent.run_turn("继续")
    (span,) = h.records[0]["spans"]
    assert span["compaction"] is True and span["status"] == "done" and span["first"] == 250


def test_a_truncated_reply_and_its_skipped_tool_call(start, monkeypatch):
    h = timed(
        start, monkeypatch,
        reply(tool_use("toolu_1", "run_command", {"command": "touch created.txt"}), stop_reason="max_tokens"),
        reply(text("重来。")),
    )  # fmt: skip
    h.agent.run_turn("建个文件")
    model, tool, _ = h.records[0]["spans"]
    assert model["truncated"] is True and model["stop"] == "max_tokens" and model["status"] == "tools"
    # 参数不完整的调用没有执行，也没有问你：skipped，不是 error（不是工具出了错）
    assert tool["result"] == "skipped" and "approval" not in tool and "note" not in tool
    assert h.records[0]["totals"]["errors"] == 0 and h.ui.confirmations == []


def test_the_openai_format_compaction_is_its_own_request(start_openai):
    # GPT、Gemini、DeepSeek 的压缩是循环里单独的一步（compacted）：写摘要那次请求单独一段，标上 compaction，
    # 钱是账本记的那一笔；它不显示在页面上，所以没有首字时间
    h = start_openai(chat("一"), chat("二", usage=BIG), chat("前面聊了一和二。", usage=SUMMARY_USAGE), chat("三"))
    records = []
    h.agent.on_trace = records.append
    for words in ("第一轮", "第二轮", "第三轮"):
        h.agent.run_turn(words)
    summary, answer = records[-1]["spans"]
    rows = ledger_rows(h.config)
    assert (summary["provider"], summary["model"], summary["status"], summary["compaction"]) == ("deepseek", "deepseek-flash", "compacted", True)
    assert "first" not in summary and "stop" not in summary
    assert (summary["input"], summary["cache_read"], summary["output"]) == (1_000, 49_000, 3_000)
    assert summary["cost"] == rows[2]["cost"] and answer["cost"] == rows[3]["cost"]
    assert (answer["status"], answer["stop"], "compaction" in answer) == ("done", "stop", False)
    assert answer["first"] >= 0 and records[-1]["totals"]["requests"] == 2
    assert records[-1]["totals"]["cost"] == round(rows[2]["cost"] + rows[3]["cost"], 6)


def test_a_model_without_a_price_has_no_cost():
    # 没有价格的模型：这次请求的 cost 是 null，合计里数一次 unpriced；全都没有价格时合计的 cost 也是 null（不是 0）
    tracer = Tracer(timer=Clock(), wall=lambda: AT)
    tracer.turn_began()
    tracer.request_began("openai", "gpt-unknown")
    tracer.request_ended(Step(status="done", usage=Usage(requests=1, input_tokens=5)), None)
    record = tracer.turn_ended("done")
    assert record["spans"][0]["cost"] is None
    assert (record["totals"]["cost"], record["totals"]["unpriced"]) == (None, 1)


# ---- 工具调用：名字、结果、问没问你 ----------------------------------------------------


def test_tool_results_errors_and_unknown_tools(start, monkeypatch):
    h = timed(
        start, monkeypatch,
        reply(
            tool_use("toolu_1", "todo_update", {"id": 99, "done": True}),
            tool_use("toolu_2", "no_such_tool", {}),
            tool_use("toolu_3", FILE_TOOL, {"command": "view", "path": "pic.png"}),
            stop_reason="tool_use",
        ),
        reply(text("出了点问题。")),
    )  # fmt: skip
    (h.config.workspace / "pic.png").write_bytes(PNG_BYTES)
    h.agent.run_turn("把 99 号标成完成")
    _, missing, unknown, picture, _ = h.records[0]["spans"]
    first_line = result_text(tool_results(h.api.requests[1])[0]).splitlines()[0]
    assert (missing["result"], missing["note"], missing["label"], missing["group"]) == ("error", first_line, "待办", "todo")
    assert missing["detail"] == '{"id": 99, "done": true}'
    # 认不出来的工具：label 是空的（页面和终端显示它的真名），图标是一块拼图
    assert (unknown["label"], unknown["group"], unknown["icon"], unknown["result"]) == ("", "other", "puzzle", "error")
    assert "no_such_tool" in unknown["note"]
    # 结果里有图：只记几张，不存图本身
    assert (picture["label"], picture["group"], picture["icon"], picture["result"], picture["images"]) == ("读写文件", "files", "file", "ok", 1)
    assert picture["detail"] == "view pic.png" and "note" not in picture
    assert h.records[0]["totals"]["errors"] == 2 and h.records[0]["totals"]["tool_calls"] == 3
    assert PNG not in json.dumps(h.records[0])  # 图的内容（base64）不进记录


def test_refused_and_auto_allowed_calls(start, monkeypatch):
    # 只读的命令那一档：ls 没问就放行了（为什么：readonly），touch 要问，你拒绝了
    h = timed(
        start, monkeypatch,
        reply(
            tool_use("toolu_1", "run_command", {"command": "ls"}),
            tool_use("toolu_2", "run_command", {"command": "touch made.txt"}),
            stop_reason="tool_use",
        ),
        reply(text("好的，不建了。")),
        approve=False,
        feedback="不要建文件",
    )  # fmt: skip
    h.agent.permissions.mode = "readonly"
    h.agent.run_turn("看看有什么，再建个文件")
    (record,) = h.records
    _, listed, refused, _ = record["spans"]
    assert (listed["approval"], listed["why"], listed["result"]) == ("auto", "readonly", "ok")
    assert "asked" not in listed and "waited" not in listed  # 没问你，就没有"等你"
    assert (refused["approval"], refused["result"], refused["asked"], refused["waited"]) == ("refused", "error", refused["start"], 2000)
    assert "note" not in refused  # 你拒绝的不用再写原因：approval 已经说了（你的话在给模型的结果里，不进记录）
    assert "不要建文件" not in json.dumps(record, ensure_ascii=False)
    assert refused["end"] - refused["start"] == 2000
    totals = record["totals"]
    assert (totals["asked"], totals["allowed"], totals["refused"], totals["auto"], totals["errors"]) == (1, 0, 1, 1, 1)
    assert totals["waiting"] == 2000 and not (h.config.workspace / "made.txt").exists()
    assert_adds_up(record)


def test_everything_allowed_is_auto_for_the_mode(start, monkeypatch):
    h = timed(
        start, monkeypatch,
        reply(tool_use("toolu_1", "run_command", {"command": "touch made.txt"}), stop_reason="tool_use"),
        reply(text("建好了。")),
    )  # fmt: skip
    h.agent.permissions.mode = "all"
    h.agent.run_turn("建个文件")
    tool = h.records[0]["spans"][1]
    assert (tool["approval"], tool["why"], tool["result"]) == ("auto", "mode", "ok")
    assert h.ui.auto_whys == ["mode"]  # 和界面上标的是同一个原因
    assert (h.records[0]["totals"]["auto"], h.records[0]["totals"]["asked"]) == (1, 0)


# ---- 子 Agent -------------------------------------------------------------------------


def test_a_subagent_is_nested_under_its_call(start, monkeypatch):
    h = timed(
        start, monkeypatch,
        reply(tool_use("t1", "agent", {"name": "资料研究员", "task": "查一下 X 是什么"}), stop_reason="tool_use"),
        reply(tool_use("s1", FILE_TOOL, {"command": "view", "path": "."}), stop_reason="tool_use"),  # 子 Agent 的第一步
        reply(text("X 是一个东西。")),  # 子 Agent 的回答（它的文字不交给主界面逐字显示，所以表不走）
        reply(text("研究员说 X 是一个东西。")),
    )  # fmt: skip
    install(h)
    h.agent.run_turn("让资料研究员查一下 X")

    (record,) = h.records  # 子 Agent 那一轮的记录没有另外交出来，只挂在这里
    first, delegated, last = record["spans"]
    costs = [row["cost"] for row in ledger_rows(h.config)]
    sub_model = h.api.requests[1]["model"]
    assert delegated == {
        "kind": "tool", "start": 250, "end": 750, "name": "agent", "group": "agent", "icon": "bot", "label": "交给子 Agent",
        "detail": "资料研究员 · 查一下 X 是什么", "agent": "资料研究员", "result": "ok",
        "size": len(result_text(tool_results(h.api.requests[3])[0])),
        "children": [
            {**model_span(250, 500, "tools", "tool_use", costs[1], calls=1), "model": sub_model},
            {"kind": "tool", "start": 500, "end": 500, "name": FILE_TOOL, "group": "files", "icon": "file", "label": "读写文件",
             "detail": "view .", "result": "ok", "size": len(result_text(tool_results(h.api.requests[2])[0]))},
            {**model_span(500, 750, "done", "end_turn", costs[2], first=250), "model": sub_model},
        ],
    }  # fmt: skip
    assert (first["start"], first["end"], last["start"], last["end"]) == (0, 250, 750, 1500)
    # 子 Agent 的每一段都在那次调用的时间里面
    assert all(delegated["start"] <= child["start"] <= child["end"] <= delegated["end"] for child in delegated["children"])
    totals = record["totals"]
    assert (totals["requests"], totals["tool_calls"]) == (4, 2)  # 子 Agent 的两次请求、一次工具调用也算
    assert totals["cost"] == round(sum(costs), 6) and totals["input"] == 400
    # 那次 agent 调用的 500 毫秒全是子 Agent 的模型在答：算进"模型"，不重复算进"工具"
    assert (totals["model"], totals["tools"], totals["waiting"], totals["other"]) == (1500, 0, 0, 0)
    assert record["wall"] == 1500


def test_a_subagents_confirmation_counts_as_waiting(start, monkeypatch):
    h = timed(
        start, monkeypatch,
        reply(tool_use("t1", "agent", {"name": "代码审查员", "task": "跑一下测试"}), stop_reason="tool_use"),
        reply(tool_use("s1", "run_command", {"command": "echo hi"}), stop_reason="tool_use"),
        reply(text("跑完了。")),
        reply(text("好")),
    )  # fmt: skip
    install(h, "reviewer", tools="Bash")
    h.agent.run_turn("交给代码审查员")
    (record,) = h.records
    command = record["spans"][1]["children"][1]
    assert (command["name"], command["approval"], command["waited"]) == ("run_command", "allowed", 2000)
    assert (record["totals"]["waiting"], record["totals"]["asked"], record["totals"]["allowed"]) == (2000, 1, 1)
    assert_adds_up(record)


def test_stopping_while_a_subagent_works(start, monkeypatch):
    # 子 Agent 刚调用完一个工具，你点了"停止"：它做完的那几段照样挂在那次调用下面，那次调用本身标成 stopped
    h = timed(
        start, monkeypatch,
        reply(tool_use("t1", "agent", {"name": "资料研究员", "task": "查一下 X"}), stop_reason="tool_use"),
        reply(tool_use("s1", FILE_TOOL, {"command": "view", "path": "."}), stop_reason="tool_use"),
        reply(text("不会走到这里")),
    )  # fmt: skip
    install(h)
    h.ui.stop_after = "tool_call"
    with pytest.raises(KeyboardInterrupt):
        h.agent.run_turn("交给研究员")
    (record,) = h.records
    _, delegated = record["spans"]
    assert record["ended"] == "stopped"
    assert (delegated["result"], delegated["agent"]) == ("stopped", "资料研究员")
    assert [(child["kind"], child.get("status"), child.get("result")) for child in delegated["children"]] == [("model", "tools", None), ("tool", None, "ok")]
    assert record["totals"]["requests"] == 2 and len(h.api.requests) == 2
    assert_adds_up(record)


# ---- 这一轮是怎么结束的 -------------------------------------------------------------------


def test_a_refused_turn(start, monkeypatch):
    h = timed(start, monkeypatch, reply(stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": "x"}))
    h.agent.run_turn("一个会被拒绝的请求")
    (record,) = h.records
    (span,) = record["spans"]
    assert record["ended"] == "refused" and (span["status"], span["stop"]) == ("refused", "refusal")
    assert span["cost"] == ledger_rows(h.config)[0]["cost"] == ONE_REQUEST  # 被拒绝的那次也花了钱
    assert h.agent.messages == []  # 这一轮照常撤销了


def test_a_paused_search_and_the_step_limit(start, monkeypatch):
    # pause_turn：服务器上的搜索还没做完，原样再请求一次。两次请求各是一段
    h = timed(start, monkeypatch, reply(text("正在搜索"), stop_reason="pause_turn"), reply(text("搜完了。")))
    h.agent.run_turn("搜一下")
    assert [(span["status"], span["stop"]) for span in h.records[0]["spans"]] == [("paused", "pause_turn"), ("done", "end_turn")]
    assert h.records[0]["ended"] == "done"

    # 用完了步数
    looping = [reply(tool_use(f"toolu_{n}", "todo_list", {}), stop_reason="tool_use") for n in range(3)]
    h = timed(start, monkeypatch, *looping, max_steps=2)
    h.agent.run_turn("一直查")
    (record,) = h.records
    assert record["ended"] == "steps" and [span["kind"] for span in record["spans"]] == ["model", "tool", "model", "tool"]


# ---- 停下、出错 -----------------------------------------------------------------------


def test_stopping_while_waiting_for_confirmation(start, monkeypatch):
    h = timed(
        start, monkeypatch,
        reply(
            tool_use("toolu_1", "run_command", {"command": "touch a.txt"}),
            tool_use("toolu_2", "run_command", {"command": "touch b.txt"}),
            stop_reason="tool_use",
        ),
        ui_class=StopAtConfirmUI,
    )  # fmt: skip
    with pytest.raises(KeyboardInterrupt):
        h.agent.run_turn("建两个文件")
    (record,) = h.records  # 停下了也交一份记录
    assert record["ended"] == "stopped" and record["wall"] == 250 + 2000
    model, tool = record["spans"]  # 第二个调用还没开始，没有它的段
    assert model["status"] == "tools"
    assert (tool["start"], tool["end"], tool["result"], tool["approval"], tool["asked"], tool["waited"]) == (250, 2250, "stopped", "stopped", 250, 2000)
    assert "note" not in tool and "size" not in tool
    totals = record["totals"]
    assert (totals["waiting"], totals["asked"], totals["allowed"], totals["refused"], totals["errors"]) == (2000, 1, 0, 0, 0)
    assert_adds_up(record)


def test_stopping_while_the_model_is_writing(start, monkeypatch):
    class StopWhileWriting(TimedUI):
        def text(self, text: str) -> None:
            self.clock.advance(STREAM)
            raise KeyboardInterrupt

    h = timed(start, monkeypatch, reply(text("写到一半")), ui_class=StopWhileWriting)
    with pytest.raises(KeyboardInterrupt):
        h.agent.run_turn("你好")
    (record,) = h.records
    (span,) = record["spans"]
    assert record["ended"] == "stopped"
    assert (span["status"], span["start"], span["end"], span["first"]) == ("stopped", 0, 750, 250)
    assert "cost" not in span and "input" not in span  # 没答完：不知道用了多少，不写


def test_an_api_error_closes_the_open_request(start, monkeypatch):
    h = timed(
        start, monkeypatch,
        reply(tool_use("toolu_1", "todo_list", {}), stop_reason="tool_use"),
        failure(529, "overloaded_error", "Overloaded"),
    )  # fmt: skip
    with pytest.raises(ProviderError):
        h.agent.run_turn("看看待办")
    (record,) = h.records
    assert record["ended"] == "error"
    first, tool, failed = record["spans"]
    assert (first["status"], tool["result"]) == ("tools", "ok")
    assert (failed["status"], failed["start"], failed["end"]) == ("error", 375, 625)
    assert "cost" not in failed and record["totals"]["requests"] == 2
    assert record["totals"]["cost"] == first["cost"]  # 只算答完了的那一次


def test_an_error_before_any_reply_still_gives_a_record(start, monkeypatch):
    h = timed(start, monkeypatch)  # 没有预设回复：假 API 回答 500
    with pytest.raises(ProviderError):
        h.agent.run_turn("你好")
    (record,) = h.records
    (span,) = record["spans"]
    assert record["ended"] == "error" and span["status"] == "error" and record["wall"] == 250
    assert h.agent.messages == []  # 用户消息照常撤回了：记录不影响这件事


def test_a_failing_on_trace_does_not_break_the_turn(start):
    h = start(reply(text("你好！")))

    def broken(record: dict) -> None:
        raise RuntimeError("界面坏了")

    h.agent.on_trace = broken
    h.agent.run_turn("你好")  # 不抛出：运行记录只是给人看的
    assert h.agent.trace.last["ended"] == "done"
    assert h.agent.messages[-1] == {"role": "assistant", "content": [{"type": "text", "text": "你好！"}]}


# ---- 历史一个字都不变 -------------------------------------------------------------------


class NoTracer:
    """什么都不记的记录器：拿它和真的比，看运行记录对发出去的请求有没有任何影响。"""

    last = None

    def watch(self, ui):
        return ui

    def turn_ended(self, ended: str) -> dict:
        return {}

    def __getattr__(self, name: str):
        return lambda *args, **kwargs: None


def test_tracing_never_touches_the_history_or_the_requests(start):
    def replies():
        return (
            reply(thinking("想一想"), tool_use("toolu_1", "todo_list", {}), stop_reason="tool_use"),
            reply(tool_use("toolu_2", "run_command", {"command": "echo hi"}), stop_reason="tool_use"),
            reply(text("好了。")),
            reply(text("第二轮。")),
        )

    traced = start(*replies())
    records = []
    traced.agent.on_trace = records.append
    traced.agent.run_turn("第一轮")
    traced.agent.run_turn("第二轮")

    plain = start(*replies())  # 同一个数据目录、同一个工作目录、同一个钟：只差在有没有运行记录
    plain.agent.trace = NoTracer()
    plain.agent.run_turn("第一轮")
    plain.agent.run_turn("第二轮")

    assert len(records) == 2
    assert json.dumps(traced.api.requests, ensure_ascii=False) == json.dumps(plain.api.requests, ensure_ascii=False)
    assert json.dumps(traced.agent.messages, ensure_ascii=False) == json.dumps(plain.agent.messages, ensure_ascii=False)
    # 记录里的东西一样都没进请求
    sent = json.dumps(traced.api.requests, ensure_ascii=False)
    assert all(marker not in sent for marker in ('"spans"', '"totals"', "cache_hit", '"waited"'))


# ---- 网页：存进会话、重新打开、倒回 -----------------------------------------------------


def traces(events: list[dict]) -> list[dict]:
    return [event for event in events if event["type"] == "trace"]


def content(event: dict) -> dict:
    """事件的内容（编号是重新分配的，不参与比较）。"""
    return {key: value for key, value in event.items() if key != "id"}


def test_the_web_saves_and_replays_the_trace(serve):
    s = serve(
        reply(tool_use("toolu_1", "todo_list", {}), stop_reason="tool_use"),
        reply(text("你没有待办。")),
        reply(text("另一段。")),
    )  # fmt: skip
    state = test_webui.say(s, "我有什么待办？")
    conversation_id = state["conversation"]

    (live,) = traces(s.browser.events)
    kinds = [event["type"] for event in s.browser.events if event["type"] != "state"]
    # 在这一轮的最后（回答写完之后、"这一轮结束"之前）
    assert kinds[-3:] == ["block_end", "trace", "turn_end"]
    assert live["ended"] == "done" and [span["kind"] for span in live["spans"]] == ["model", "tool", "model"]
    assert live["spans"][1]["label"] == "待办" and live["totals"]["requests"] == 2
    assert live["totals"]["cost"] == round(sum(row["cost"] for row in ledger_rows(s.config)), 6)

    # 存进了会话文件的 events（不进 messages）
    saved = saved_file(s.config, conversation_id)
    assert [content(event) for event in traces(saved["events"])] == [content(live)]
    assert '"spans"' not in json.dumps(saved["messages"], ensure_ascii=False)

    # 换到别的会话，再打开它：记录原样重放出来
    test_webui.act(s, "/api/new")
    test_webui.say(s, "另一段")
    replayed, _ = open_conversation(s, conversation_id)
    assert [content(event) for event in traces(replayed)] == [content(live)]


def test_regenerating_drops_the_old_record_and_marks_the_new_one(rig):
    s = rig(reply(text("答一")), reply(text("旧答二")), reply(text("新答二")))
    # 用假的钟表时间给每一轮做个记号（at），好认出是哪一轮的记录
    s.app.agent.trace.wall = lambda: 101.0
    test_rewind_web.say(s, "问一")
    s.app.agent.trace.wall = lambda: 102.0
    conversation_id = test_rewind_web.say(s, "问二")["conversation"]
    s.app.agent.trace.wall = lambda: 103.0

    replayed, live = test_rewind_web.turn(s, "retry", mark=2)

    # 重放的只剩第一轮的记录；旧的第二轮跟着它的事件一起去掉了
    assert [event["at"] for event in traces(replayed)] == [101.0]
    (new,) = traces(live)
    assert new["at"] == 103.0 and new["again"] is True and new["ended"] == "done"
    assert "again" not in traces(replayed)[0]
    saved = saved_file(s.config, conversation_id)
    assert [(event["at"], event.get("again", False)) for event in traces(saved["events"])] == [(101.0, False), (103.0, True)]
    assert latest_state(s.browser)["rewind"]["mark"] == 2  # 记录不影响"最后一轮能不能倒回去"


def test_regenerating_a_failed_turn_is_marked_too(rig):
    # 上一次在模型答出第一步之前就失败了（用户消息被撤回了），你在页面上点"重新生成"：
    # 服务器用同样的文字再发一次（server._run_turn 走 run_turn 而不是 retry_turn）。对你来说这仍然是一次重新生成
    s = rig()  # 没有预设回复：假 API 返回 500
    test_rewind_web.say(s, "问一")
    (failed,) = traces(s.browser.events)
    assert failed["ended"] == "error" and failed["spans"][0]["status"] == "error"

    s.api.replies.append(reply(text("答一")))
    replayed, live = test_rewind_web.turn(s, "retry", mark=0)
    assert traces(replayed) == []  # 失败的那一轮的记录跟着它的事件一起去掉了
    (new,) = traces(live)
    assert new["ended"] == "done"
    assert new.get("again") is True


# ---- 定时任务、聊天通道 -----------------------------------------------------------------


def test_a_task_run_saves_its_own_trace(task_serve):
    s = task_serve(
        reply(tool_use("toolu_1", "todo_list", {}), stop_reason="tool_use"),
        reply(text("今天没有待办。")),
    )  # fmt: skip
    task_id = test_task_runs.agent_task(s)
    assert s.browser.post("/api/task/run", {"id": task_id}) == (200, {"ok": True, "queued": False})
    task = test_task_runs.finished(s, task_id, "done")

    conversation = s.app.store.load(task["runs"][0]["conversation"])
    (record,) = traces(conversation.events)
    model, tool, answer = record["spans"]
    assert record["ended"] == "done" and tool["name"] == "todo_list" and tool["label"] == "待办"
    rows = ledger_rows(s.config)
    assert [row["source"] for row in rows] == ["task", "task"]
    assert [model["cost"], answer["cost"]] == [row["cost"] for row in rows] == [ONE_REQUEST, ONE_REQUEST]
    assert record["totals"]["cost"] == task["runs"][0]["cost"]  # 和任务列表里这次运行的花费是同一个数
    assert_adds_up(record, slack=3)
    # 任务的记录不进你正在看的那段对话
    assert traces(s.ui.events_after(0, timeout=0)) == []


def test_a_channel_turn_saves_its_own_trace(bot):
    s = bot(
        reply(tool_use("toolu_1", "run_command", {"command": "ls"}), stop_reason="tool_use"),
        reply(text("看过了。")),
        approval="readonly",
    )  # fmt: skip
    s.wait_reply(s.say("看看有什么文件"), "看过了")
    s.idle()
    conversation = s.app.store.load(s.session().conversation.id)
    (record,) = traces(conversation.events)
    _, command, _ = record["spans"]
    assert (command["name"], command["approval"], command["why"], command["detail"]) == ("run_command", "auto", "readonly", "$ ls")
    assert [row["source"] for row in ledger_rows(s.config)] == ["channel", "channel"]
    assert record["totals"]["auto"] == 1 and record["ended"] == "done"
    # 运行记录只存进会话，不发到聊天里
    assert all('"spans"' not in message["plain"] and "运行记录" not in message["plain"] for message in s.tg.sent)


# ---- 终端的 /trace --------------------------------------------------------------------


def test_trace_lines_before_and_after_a_turn(start, monkeypatch):
    assert lines(None) == [("这次运行里还没有完成的一轮。说一句话，等它答完，再输入 /trace。", "info")]
    h = timed(
        start, monkeypatch,
        reply(tool_use("t1", "agent", {"name": "资料研究员", "task": "查一下 X"}), stop_reason="tool_use"),
        reply(tool_use("s1", FILE_TOOL, {"command": "view", "path": "."}), stop_reason="tool_use"),
        reply(text("X 是一个东西。")),
        reply(text("好。")),
    )  # fmt: skip
    install(h)
    h.agent.run_turn("交给研究员")
    shown = lines(h.records[0])
    head, split, counts, *rows = [text for text, _ in shown]
    assert head.startswith("这一轮的运行记录 · 一共 1.5 秒 · $") and head.endswith("答完了")
    assert split == "模型 1.5 秒 · 工具 0.00 秒 · 等你确认 0.00 秒 · 其他 0.00 秒"
    assert counts.startswith("4 次请求、2 次工具调用")
    # 每一段一行；子 Agent 的往里缩一格，前面有 └
    assert len(rows) == len(flat(h.records[0]["spans"])) == 6
    assert [row.lstrip().startswith("└") for row in rows] == [False, False, True, True, True, False]
    assert "交给子 Agent「资料研究员」" in rows[1] and "读写文件" in rows[3]
    assert [style for _, style in shown][3:] == ["plain", "plain", "info", "info", "info", "plain"]


def test_trace_lines_follow_the_interface_language(start, monkeypatch):
    h = timed(
        start, monkeypatch,
        reply(tool_use("toolu_1", "todo_list", {}), stop_reason="tool_use"),
        reply(tool_use("toolu_2", "run_command", {"command": "ls"}), stop_reason="tool_use"),
        reply(text("好")),
    )  # fmt: skip
    h.agent.run_turn("看看")
    i18n.choose("en")
    shown = "\n".join(text for text, _ in lines(h.records[0]) + lines(None))
    # 记录里没有用户和模型写的中文（工具没有中文参数），所以英文的界面上一个汉字都不该有
    assert not CJK.findall(shown), shown


def test_the_trace_command_in_the_terminal(start, capsys):
    ui = TerminalUI()
    h = start(reply(tool_use("toolu_1", "todo_list", {}), stop_reason="tool_use"), reply(text("没有待办。")), ui=ui)
    assert handle_command("/trace", h.app, ui) is True
    assert "还没有完成的一轮" in capsys.readouterr().out
    h.agent.run_turn("我有什么待办？")
    assert ui.last_trace is h.agent.trace.last  # 终端界面也接上了 on_trace（build_app 里接的）
    capsys.readouterr()
    handle_command("/trace", h.app, ui)
    out = capsys.readouterr().out
    assert "运行记录 · 一共" in out and "2 次请求、1 次工具调用" in out and "待办 · todo_list" in out
    assert "/trace" in HELP  # /help 里也列着它


# ---- 上限 -----------------------------------------------------------------------------


def test_too_many_spans_are_counted_not_stored(start, monkeypatch):
    monkeypatch.setattr(trace_recorder, "TRACE_MAX_SPANS", 3)
    looping = [reply(tool_use(f"toolu_{n}", "todo_list", {}), stop_reason="tool_use") for n in range(3)]
    h = timed(start, monkeypatch, *looping, reply(text("查完了。")))
    h.agent.run_turn("一直查")
    (record,) = h.records
    # 一共 7 段（4 次请求、3 次工具调用），只存前 3 段，少了的 4 段数在 omitted 里；合计照样全算
    assert [span["kind"] for span in record["spans"]] == ["model", "tool", "model"]
    assert record["omitted"] == 4
    assert (record["totals"]["requests"], record["totals"]["tool_calls"]) == (4, 3)
    assert record["totals"]["tools"] == 3 * 125 and record["wall"] == 4 * 250 + 500 + 3 * 125
    assert "……还有 4 段" in lines(record)[-1][0]


def test_the_cap_counts_nested_spans_too(start, monkeypatch):
    monkeypatch.setattr(trace_recorder, "TRACE_MAX_SPANS", 3)
    h = timed(
        start, monkeypatch,
        reply(tool_use("t1", "agent", {"name": "资料研究员", "task": "查一下 X"}), stop_reason="tool_use"),
        reply(tool_use("s1", FILE_TOOL, {"command": "view", "path": "."}), stop_reason="tool_use"),
        reply(text("X 是一个东西。")),
        reply(text("好。")),
    )  # fmt: skip
    install(h)
    h.agent.run_turn("交给研究员")
    (record,) = h.records
    # 按先后顺序：主助手的请求、agent 调用、子 Agent 的第一次请求；剩下的 3 段（子 Agent 的工具和请求、主助手的回答）没存
    assert [span["kind"] for span in flat(record["spans"])] == ["model", "tool", "model"]
    assert record["omitted"] == 3 and record["totals"]["requests"] == 4


# ---- 小工具 ---------------------------------------------------------------------------


def test_the_record_format_matches_the_documentation():
    # __init__.py 里写的格式：记录的几项、合计的几项。页面和终端照着它写，字段改了名字这里要跟着改
    tracer = Tracer(timer=Clock(), wall=lambda: AT)
    tracer.turn_began(again=True)
    record = tracer.turn_ended("done")
    assert set(record) == {"version", "at", "wall", "ended", "spans", "totals", "again"}
    assert set(record["totals"]) == {
        "model", "tools", "waiting", "other", "requests", "tool_calls", "errors", "cost", "unpriced",
        "input", "cache_read", "cache_write", "output", "cache_hit", "server_tools", "asked", "allowed", "refused", "auto",
    }  # fmt: skip
    assert record["totals"]["cache_hit"] is None  # 没有输入：说不上命中了多少
    assert record["version"] == 1 and record["again"] is True and record["spans"] == []
