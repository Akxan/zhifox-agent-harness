"""运行记录的记录器（格式见 __init__.py）。

agent 循环在几个时刻告诉它一声（"记录点"，agent.py 里每个只占一行），它记下时刻和数字，一轮结束时整理成一份记录：

    turn_began(again)                    一轮开始（again：这是"重新生成"）
      request_began(provider, model, fallback)  发出一次请求（fallback：用的是备用模型的话，它替的是哪个模型，见 fallback.py）
        first_output()                   模型的第一段输出到了（watch 包着界面交给适配层，看到第一段思考或文字时自动调用）
      request_ended(step, cost)          这次请求答完了（cost 是账本记的那一笔，见 ledger.py）
      request_failed(note)               这次请求失败了（模型暂时用不了），循环改用备用模型再发一次
      tool_began(call, tool)             开始一次工具调用
        approval_asked()                 要问你：开始等你答复
        approval_decided(decision, why)  你答复了，或者没问你就放行了
        adopt(child, agent)              这次调用交给了子 Agent：把它的那一轮挂在这次调用下面（见 agents/runner.py）
      tool_ended(text, is_error, images, ran)  这次调用有结果了
    turn_ended(ended) → 记录              一轮结束

中途被打断（停止、出错）时，还没结束的那一段在 turn_ended 里按"此刻"收尾，标成 stopped 或 error，
等你答复等到一半的，等了多久也照样算上。

为什么时间用 timer（默认 time.monotonic）而不是钟表时间：它只往前走，电脑中途对时、改了时区，算出来的长短也不会是负的。
钟表时间（wall，默认 time.time）只用来记"这一轮是什么时候开始的"。测试里两样都可以换成假的。

每个助手（主助手、每一次定时任务、每个聊天、每一次子 Agent）各有一个记录器，只在跑这个助手的那一个线程里用，所以不用加锁。
"""

from __future__ import annotations

import math
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import TRACE_MAX_SPANS, TRACE_SUMMARY_CHARS
from ..display import describe_call, short
from ..safe import clean_text

VERSION = 1
TOKENS = ("input", "cache_read", "cache_write", "output")
# 适配层发来的提示里，算"模型开始输出了"的只有这两种：服务器端的工具开始了、有结果了（联网搜索）。
# 别的提示（"正在压缩较早的对话……"、重试）是程序自己说的话，不是模型的输出
OUTPUT_NOTICES = frozenset({"tool", "result"})


@dataclass
class Span:
    """时间线上的一段：一次模型请求（kind 是 model）或者一次工具调用（tool）。时刻都是 timer 的读数。"""

    kind: str
    start: float
    end: float | None = None
    data: dict = field(default_factory=dict)  # 原样写进记录的那些字段（已经洗过、截短过）
    first: float | None = None  # 模型请求：第一段输出到的时刻
    asked: float | None = None  # 工具调用：开始等你答复的时刻
    waited: float = 0.0  # 工具调用：等你答复等了多久（秒）
    children: list[Span] = field(default_factory=list)  # 交给子 Agent 的那一次：它的请求和工具调用


class Tracer:
    def __init__(self, timer: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time):
        self.timer = timer
        self.wall = wall
        self.last: dict | None = None  # 最近一轮的记录（终端的 /trace 用）
        self.spans: list[Span] = []  # 这一轮到目前为止的每一段（子 Agent 的挂在它那次调用下面）
        self._started = timer()
        self._at = wall()
        self._again = False
        self._request: Span | None = None  # 正在进行的那次请求
        self._tool: Span | None = None  # 正在进行的那次工具调用

    # ---- 记录点 -----------------------------------------------------------

    def turn_began(self, again: bool = False) -> None:
        self.spans = []
        self._started = self.timer()
        self._at = self.wall()
        self._again = again
        self._request = self._tool = None

    def request_began(self, provider: str, model: str, fallback: str = "") -> None:
        span = Span("model", self.timer(), data={"provider": clean_text(provider)[:40], "model": clean_text(model)[:80]})
        if fallback:
            span.data["fallback"] = clean_text(fallback)[:80]
        self.spans.append(span)
        self._request = span

    def request_failed(self, note: str) -> None:
        """正在进行的那次请求失败了，循环要换一个模型再发（见 agent.py 的 _request）：这一段到这里结束，标成 error，
        写上出错的原因。没有用量、没有花费（失败的请求不计费，账本里也没有这一笔）。"""
        span, self._request = self._request, None
        if span is None:
            return
        span.end = self.timer()
        span.data["status"] = "error"
        span.data["note"] = short(next((line for line in clean_text(note).splitlines() if line.strip()), ""), TRACE_SUMMARY_CHARS)

    def first_output(self) -> None:
        span = self._request
        if span is not None and span.first is None:
            span.first = self.timer()

    def watch(self, ui: Any) -> Any:
        """包着界面交给适配层（backend.step）：第一段输出到的时候记一下"首字时间"，别的一样不动、原样转交。"""
        return _Watch(ui, self.first_output)

    def request_ended(self, step: Any, cost: float | None) -> None:
        span, self._request = self._request, None
        if span is None:
            return
        span.end = self.timer()
        usage, data = step.usage, span.data
        data["status"] = step.status
        if step.stop_reason:
            data["stop"] = clean_text(str(step.stop_reason))[:40]
        data.update(input=usage.input_tokens, cache_read=usage.cache_read_tokens, cache_write=usage.cache_write_tokens, output=usage.output_tokens)
        data["cost"] = round(cost, 6) if isinstance(cost, (int, float)) and math.isfinite(cost) else None
        if step.tool_calls:
            data["calls"] = len(step.tool_calls)
        if step.status == "compacted" or step.compacted:
            data["compaction"] = True
        if step.status == "compacted":
            span.first = None  # 写摘要的那次请求不显示在页面上，它的"第一个字"没有意义
        if step.retries:
            data["retries"] = int(step.retries)
        if step.truncated:
            data["truncated"] = True
        server = {clean_text(str(name))[:40]: int(count) for name, count in (step.server_tools or {}).items() if count}
        if usage.searches > server.get("web_search", 0):  # 账单上的次数为准（回复里的块万一被丢掉了一部分）
            server["web_search"] = usage.searches
        if server:
            data["server_tools"] = server

    def tool_began(self, call: Any, tool: Any) -> None:
        # 用到时才导入：capabilities 要导入大半个程序，agent.py 一开始就导入它的话会绕成一个圈
        from ..capabilities import label_of

        name = clean_text(call.shown)
        label = label_of(tool)
        described = clean_text(describe_call(name, call.input))
        detail = described[len(name) :].strip() if described.startswith(name) else described
        if detail == "{}":  # 没有参数的调用（todo_list）：不写一对空括号
            detail = ""
        data = {"name": name[:80], "group": label.group, "icon": label.icon, "label": label.label, "detail": short(detail, TRACE_SUMMARY_CHARS)}
        if label.server:
            data["server"] = clean_text(label.server)[:80]
        span = Span("tool", self.timer(), data=data)
        self.spans.append(span)
        self._tool = span

    def approval_asked(self) -> None:
        span = self._tool
        if span is not None and span.asked is None:
            span.asked = self.timer()

    def approval_decided(self, decision: str, why: str = "") -> None:
        span = self._tool
        if span is None:
            return
        if span.asked is not None:
            span.waited = max(0.0, self.timer() - span.asked)
        span.data["approval"] = decision
        if why:
            span.data["why"] = clean_text(why)[:20]

    def adopt(self, child: Tracer, agent: str = "") -> None:
        """子 Agent 刚跑完它的一轮（child 是它的记录器，它自己已经收好了尾）：把那一轮的每一段挂在正在进行的这次调用下面。
        两边的时刻是同一个 timer 读出来的（Delegator 让子 Agent 用叫它的那一方的 timer），所以画在同一条时间轴上。"""
        span = self._tool
        (span.children if span is not None else self.spans).extend(child.spans)
        if span is not None and agent:
            span.data["agent"] = clean_text(agent)[:60]

    def tool_ended(self, text: str, is_error: bool, images: int = 0, ran: bool = True) -> None:
        span, self._tool = self._tool, None
        if span is None:
            return
        span.end = self.timer()
        data = span.data
        data["result"] = "skipped" if not ran else "error" if is_error else "ok"
        data["size"] = len(text)
        if images:
            data["images"] = int(images)
        if is_error and ran and data.get("approval") != "refused":  # 你拒绝的那次不用再写原因：approval 已经说了
            data["note"] = short(next((line for line in text.splitlines() if line.strip()), ""), TRACE_SUMMARY_CHARS)

    def turn_ended(self, ended: str) -> dict:
        """一轮结束（ended 见 __init__.py 的格式说明）：还开着的那一段按此刻收尾，整理出这一轮的记录。"""
        now = self.timer()
        cut = "stopped" if ended == "stopped" else "error"
        if self._request is not None:
            self._request.end = now
            self._request.data["status"] = cut
            self._request = None
        if self._tool is not None:
            span = self._tool
            if span.asked is not None and "approval" not in span.data:
                span.waited = max(0.0, now - span.asked)
                span.data["approval"] = "stopped"
            span.end = now
            span.data["result"] = "stopped"
            self._tool = None
        wall = max(0.0, now - self._started)
        budget = [TRACE_MAX_SPANS]
        spans = self._export(self.spans, budget, now)
        record: dict[str, Any] = {
            "version": VERSION,
            "at": round(self._at, 3),
            "wall": _ms(wall),
            "ended": ended,
            "spans": spans,
            "totals": _totals(self.spans, wall, now),
        }
        if self._again:
            record["again"] = True
        omitted = _count(self.spans) - (TRACE_MAX_SPANS - budget[0])
        if omitted:
            record["omitted"] = omitted
        self.last = record
        return record

    # ---- 整理成记录 ---------------------------------------------------------

    def _at_ms(self, moment: float) -> int:
        return _ms(moment - self._started)

    def _export(self, spans: list[Span], budget: list[int], now: float) -> list[dict]:
        """每一段写成记录里的样子。budget 是还能写几段（整棵树一起算，按先后顺序，多出来的不写）。"""
        out = []
        for span in spans:
            if budget[0] <= 0:
                break
            budget[0] -= 1
            end = span.end if span.end is not None else now
            item: dict[str, Any] = {"kind": span.kind, "start": self._at_ms(span.start), "end": self._at_ms(end), **span.data}
            if span.first is not None:
                item["first"] = _ms(span.first - span.start)  # 从这次请求发出去算起
            if span.asked is not None:
                item["asked"] = self._at_ms(span.asked)
                item["waited"] = _ms(span.waited)
            if span.children:
                item["children"] = self._export(span.children, budget, now)
            out.append(item)
        return out


class _Watch:
    """交给适配层的界面：和原来的界面一模一样，只是第一段思考、文字（或者服务器端工具的提示）到的时候，记一下时刻。"""

    def __init__(self, ui: Any, seen: Callable[[], None]):
        self._ui = ui
        self._seen = seen

    def thinking(self, text: str) -> None:
        self._seen()
        self._ui.thinking(text)

    def text(self, text: str) -> None:
        self._seen()
        self._ui.text(text)

    def notice(self, level: str, text: str) -> None:
        if level in OUTPUT_NOTICES:
            self._seen()
        self._ui.notice(level, text)

    def __getattr__(self, name: str) -> Any:  # 别的方法（end_stream、info、cite……）原样交给原来的界面
        return getattr(self._ui, name)


def _ms(seconds: float) -> int:
    return max(0, round(seconds * 1000)) if math.isfinite(seconds) else 0


def _count(spans: list[Span]) -> int:
    return sum(1 + _count(span.children) for span in spans)


def _totals(spans: list[Span], wall: float, now: float) -> dict:
    """合计（格式见 __init__.py）。由这里算好，页面和终端照着写，不各自再算一遍。"""
    time_in = {"model": 0.0, "tools": 0.0, "waiting": 0.0}
    tokens = dict.fromkeys(TOKENS, 0)
    counts: Counter[str] = Counter()
    server: Counter[str] = Counter()
    money = {"cost": 0.0, "priced": 0, "unpriced": 0}

    def walk(items: list[Span]) -> None:
        for span in items:
            duration = max(0.0, (span.end if span.end is not None else now) - span.start)
            data = span.data
            if span.kind == "model":
                time_in["model"] += duration
                counts["requests"] += 1
                for name in TOKENS:
                    tokens[name] += int(data.get(name, 0))
                server.update(data.get("server_tools", {}))
                if "cost" in data:  # 没答完的请求没有这一项：不知道花了多少，不算"没有价格"
                    if data["cost"] is None:
                        money["unpriced"] += 1
                    else:
                        money["cost"] += data["cost"]
                        money["priced"] += 1
                continue
            counts["tool_calls"] += 1
            counts["errors"] += data.get("result") == "error"
            approval = data.get("approval")
            if approval:
                counts["auto" if approval == "auto" else "asked"] += 1
                counts[approval] += approval in ("allowed", "refused")
            # 这次调用自己花的时间：去掉等你答复的那段、去掉交给子 Agent 后它自己的请求和调用（那些算进各自的类别）
            inner = sum(max(0.0, (child.end if child.end is not None else now) - child.start) for child in span.children)
            time_in["tools"] += max(0.0, duration - span.waited - inner)
            time_in["waiting"] += span.waited
            walk(span.children)

    walk(spans)
    # 四项各自取整到毫秒的话，零头加起来会和总时长差一两毫秒（页面上那根分段的横条就画不满）。
    # 所以按"累计到这一项"取整再相减：每一项仍是整数毫秒，加起来正好是 wall
    split, running, before = {}, 0.0, 0
    for name, value in (*time_in.items(), ("other", max(0.0, wall - sum(time_in.values())))):
        running += value
        upto = min(_ms(running), _ms(wall))
        split[name] = max(0, upto - before)
        before += split[name]
    split["other"] += max(0, _ms(wall) - before)  # 时间都花在了前三项上（四舍五入以后差的那一点）
    fed = tokens["input"] + tokens["cache_read"] + tokens["cache_write"]
    priced = money["priced"] or not money["unpriced"]
    return {
        **split,
        "requests": counts["requests"],
        "tool_calls": counts["tool_calls"],
        "errors": counts["errors"],
        "cost": round(money["cost"], 6) if priced else None,
        "unpriced": money["unpriced"],
        **tokens,
        "cache_hit": round(tokens["cache_read"] / fed, 4) if fed else None,
        "server_tools": dict(server),
        "asked": counts["asked"],
        "allowed": counts["allowed"],
        "refused": counts["refused"],
        "auto": counts["auto"],
    }
