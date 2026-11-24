"""CI 上把页面测试分到几台机器（tests/shards.py，HARNESS_TEST_SHARD=第几份/一共几份）：几份合起来一个不少、一个不重。

这是"分开跑以后没丢掉哪个测试"的保证，所以除了直接测 split，还真的起一个 pytest 子进程收集几遍，
对一对每一份收集到的名字（和 CI 上一样经过 conftest 的钩子、-m 筛选）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import shards

ROOT = Path(__file__).parent.parent


@pytest.mark.parametrize("total", [1, 2, 3, 7])
def test_the_shards_together_are_everything_exactly_once(total):
    items = [f"test_{number}" for number in range(23)]
    pieces = [shards.split(items, f"{index}/{total}")[0] for index in range(1, total + 1)]
    assert sorted(item for piece in pieces for item in piece) == sorted(items)  # 一个不少、一个不重
    assert max(map(len, pieces)) - min(map(len, pieces)) <= 1  # 一样多（最多差一个）
    for index, piece in enumerate(pieces, start=1):
        kept, others = shards.split(items, f"{index}/{total}")
        assert kept == piece and sorted(kept + others) == sorted(items)
        assert kept == [item for item in items if item in kept]  # 先后不变


def test_neighbours_go_to_different_shards():
    # 同一个测试换了参数（三种语言）挨在一起，耗时也差不多：轮流发，两份各分到一部分，不会全落在一台机器上
    items = ["layout[zh]", "layout[en]", "layout[es]", "slow[light]", "slow[dark]"]
    first, second = shards.split(items, "1/2")
    assert first == ["layout[zh]", "layout[es]", "slow[dark]"]
    assert second == ["layout[en]", "slow[light]"]


@pytest.mark.parametrize("spec", ["", "1", "0/2", "3/2", "a/b", "1/0", "-1/2", "1/2/3"])
def test_a_wrong_spec_is_refused_rather_than_running_less(spec):
    with pytest.raises(ValueError):
        shards.split(["a", "b"], spec)


def test_spaces_are_allowed():
    assert shards.parse(" 2 / 3 ") == (2, 3)


def _pytest(shard: str | None, *arguments: str) -> subprocess.CompletedProcess[str]:
    """在仓库里另起一个 pytest（shard 为 None 时不设 HARNESS_TEST_SHARD），返回它的结果。不检查退出码，由调用的人看。"""
    env = {key: value for key, value in os.environ.items() if key != shards.VARIABLE}
    if shard is not None:
        env[shards.VARIABLE] = shard
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *arguments],
        check=False, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )


def _collect(shard: str | None, *options: str) -> list[str]:
    """像 CI 那样收集一遍（不跑），返回测试的名字。"""
    done = _pytest(shard, "--collect-only", "-q", "-n", "0", *options)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    return [line for line in done.stdout.splitlines() if "::" in line]


def test_the_page_test_shards_of_a_real_collection_cover_every_page_test_once():
    # 和 CI 一样：先 -m ui 筛出页面测试，再分成两份
    everything = _collect(None, "-m", "ui", "tests/ui")
    first, second = _collect("1/2", "-m", "ui", "tests/ui"), _collect("2/2", "-m", "ui", "tests/ui")
    assert everything and first and second
    assert sorted(first + second) == sorted(everything)
    assert not set(first) & set(second)
    assert abs(len(first) - len(second)) <= 1  # 在 -m 之后才分，两份一样多


@pytest.mark.parametrize("workers", ["0", "2"])
def test_a_wrong_spec_stops_the_whole_run_with_a_plain_message(workers):
    # 退出码 4 是 pytest 的"用法不对"：一开始就停下、说清楚。不是 3（INTERNALERROR：xdist 的进程在收集时崩掉）。
    # -n 2 是 CI 上的跑法：不是只收集，而是真的分给几个进程去跑（只挑一个很快的测试）
    done = _pytest("3/2", "-q", "-n", workers, "tests/test_shards.py::test_spaces_are_allowed")
    output = done.stdout + done.stderr
    assert done.returncode == pytest.ExitCode.USAGE_ERROR, output[-2000:]
    assert shards.VARIABLE in output
    assert "INTERNALERROR" not in output
