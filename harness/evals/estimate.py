"""跑真的之前，估算要花多少钱。

算法写在明处，你能自己核对（价格来自 pricing.py，加上你在设置里填的）：

  每个任务跑一遍，假定调用 R 次模型，每次带着 C 个 token 的上下文、写 O 个 token（任务的 [budget]，没写就用
  config.py 的 EVAL_ASSUMED_*）：
    最多  = R × C × 输入价（缓存一点都没命中。Claude 写入缓存比普通输入贵，按贵的算）+ R × O × 输出价
    大约  = 第一次的 C 按写入缓存的价 + 后面 R-1 次的 C 里有 EVAL_CACHE_HIT 命中缓存 + R × O × 输出价
  跑过真的以后，有更准的数：同一个模型上一次真的跑这个任务实际花了多少（每一遍的平均），就用它当"大约"，
  那次花得最多的一遍再乘 EVAL_ESTIMATE_MARGIN 当"最多"。实际跑一次的数比任何假定都准（claude-api 的评测指南也这么说）。

  请评分员（--judge）的每一项另算：一次读 EVAL_JUDGE_INPUT、写 EVAL_JUDGE_OUTPUT 个 token，按评分员的模型的价格。

所以这只是估算：模型多走几步、多想一会儿就会多花。确认的时候给你看的是"最多"那个数。
DeepSeek 分高峰和闲时，这里一律按高峰价（贵的那个）估。没有价格的模型算不出钱，只列 token 数，并且明说。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import (
    EVAL_CACHE_HIT,
    EVAL_ESTIMATE_MARGIN,
    EVAL_JUDGE_INPUT,
    EVAL_JUDGE_OUTPUT,
)
from ..pricing import price_key, price_of
from .results import Run
from .runner import Target
from .spec import Task


@dataclass
class Line:
    """一个任务（跑 repeat 遍）的估算。"""

    task: str
    measured: bool  # 按上一次真的运行实际花的钱估的
    tokens_in: int
    tokens_out: int
    low: float | None  # 大约（美元）；没有价格时是 None
    high: float | None  # 最多


@dataclass
class Estimate:
    target: Target
    repeat: int
    lines: list[Line] = field(default_factory=list)
    judge_model: str | None = None
    judge_calls: int = 0
    judge_cost: float | None = 0.0

    @property
    def priced(self) -> bool:
        return all(line.high is not None for line in self.lines) and self.judge_cost is not None

    @property
    def low(self) -> float:
        return sum(line.low or 0.0 for line in self.lines) + (self.judge_cost or 0.0)

    @property
    def high(self) -> float:
        return sum(line.high or 0.0 for line in self.lines) + (self.judge_cost or 0.0)

    @property
    def measured(self) -> int:
        return sum(1 for line in self.lines if line.measured)

    @property
    def tokens(self) -> tuple[int, int]:
        return sum(line.tokens_in for line in self.lines), sum(line.tokens_out for line in self.lines)


def _measured(task: Task, target: Target, history: list[Run]) -> list[dict]:
    """最近一次真的运行（同一个模型）里这个任务跑完了的那几遍。"""
    for run in history:  # 新的在前
        meta = run.meta
        if meta.get("mode") or meta.get("provider") != target.provider or meta.get("model") != target.model:
            continue
        rows = [row for row in run.rows if row["task"] == task.id and row.get("status") == "ok" and row.get("cost") is not None]
        if rows:
            return rows
    return []


def estimate_run(
    tasks: list[Task], repeat: int, target: Target, prices: dict[str, dict], history: list[Run], judge_model: str | None = None
) -> Estimate:
    result = Estimate(target, repeat, judge_model=judge_model)
    price = price_of(price_key(target.model, target.provider), prices)  # 自定义接口上的模型按"接口/模型"找（见 pricing.price_key）
    for task in tasks:
        rows = _measured(task, target, history)
        if rows:
            costs = [row["cost"] for row in rows]
            usage = [row.get("usage") or {} for row in rows]
            tokens_in = sum(item.get("input", 0) + item.get("cache_read", 0) + item.get("cache_write", 0) for item in usage) // len(rows)
            tokens_out = sum(item.get("output", 0) for item in usage) // len(rows)
            result.lines.append(
                Line(task.id, True, tokens_in * repeat, tokens_out * repeat, sum(costs) / len(costs) * repeat, max(costs) * EVAL_ESTIMATE_MARGIN * repeat)
            )
            continue
        budget = task.budget
        tokens_in, tokens_out = budget.requests * budget.context, budget.requests * budget.output
        if price is None:
            result.lines.append(Line(task.id, False, tokens_in * repeat, tokens_out * repeat, None, None))
            continue
        write = max(price["input"], price["cache_write"])
        later = (budget.requests - 1) * budget.context
        low_in = budget.context * write + later * (EVAL_CACHE_HIT * price["cache_read"] + (1 - EVAL_CACHE_HIT) * write)
        output = tokens_out * price["output"]
        low = (low_in + output) / 1_000_000
        high = (tokens_in * write + output) / 1_000_000
        result.lines.append(Line(task.id, False, tokens_in * repeat, tokens_out * repeat, low * repeat, high * repeat))
    if judge_model:
        result.judge_calls = sum(task.judged for task in tasks) * repeat
        judge_price = price_of(judge_model, prices)
        if judge_price is None:
            result.judge_cost = None
        else:
            result.judge_cost = result.judge_calls * (EVAL_JUDGE_INPUT * judge_price["input"] + EVAL_JUDGE_OUTPUT * judge_price["output"]) / 1_000_000
    return result
