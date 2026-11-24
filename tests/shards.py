"""把一次测试分成几份，分给 CI 上几台机器同时跑（HARNESS_TEST_SHARD=第几份/一共几份，比如 1/2）。

为什么要分：页面测试（tests/ui/）每个都要开一个 Chrome 标签页实际操作，GitHub 的 macOS 机器只有 3 个核，
两百多个页面测试在一台上要十分钟以上。分到两台机器上同时跑，各跑一半。pytest-xdist 的 -n 只能在一台机器里分进程，
跨机器要自己分，这里就是那个"自己分"：不引入插件，十几行。

怎么分：按收集到的先后轮流发（第 0 个给第 1 份，第 1 个给第 2 份……）。
- 同一份代码在哪台机器上收集到的测试和先后都一样，所以每个测试正好落在一份里，几份合起来一个不少、一个不重（tests/test_shards.py 查）。
- 同一个文件里挨着的测试（常常是同一个测试换了参数：三种语言、深浅两种外观）耗时差不多，轮流发正好把它们均匀拆开，
  比按文件分（一个文件十几个慢测试全落在一台上）匀得多。代价是一个文件自己的演示服务器（module 级的 fixture）两份里各开一次，
  几秒钟的事。
- 在 -m 这些筛选之后才分（conftest 里的钩子是 trylast），只在留下来的测试里轮流发，几份才一样多。

没有设 HARNESS_TEST_SHARD 时什么都不做：本机照旧一次跑全部。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TypeVar

VARIABLE = "HARNESS_TEST_SHARD"
SPEC = re.compile(r"\s*(\d+)\s*/\s*(\d+)\s*")

T = TypeVar("T")


def parse(spec: str) -> tuple[int, int]:
    """"2/3" → (2, 3)：第几份（从 1 数起）、一共几份。写错了抛 ValueError，宁可整轮不跑，也不悄悄少跑。"""
    match = SPEC.fullmatch(spec)
    if not match:
        raise ValueError(f"{VARIABLE} 要写成 第几份/一共几份（比如 1/2），现在是 {spec!r}")
    index, total = int(match.group(1)), int(match.group(2))
    if not 1 <= index <= total:
        raise ValueError(f"{VARIABLE}={spec!r}：第几份要在 1 到 {total} 之间")
    return index, total


def split(items: Sequence[T], spec: str) -> tuple[list[T], list[T]]:
    """按 spec 分：返回（这一份要跑的，别的几份跑的）。两样合起来就是 items，先后不变。"""
    index, total = parse(spec)
    kept: list[T] = []
    others: list[T] = []
    for position, item in enumerate(items):
        (kept if position % total == index - 1 else others).append(item)
    return kept, others
