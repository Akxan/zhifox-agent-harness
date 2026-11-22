"""花费账本：每调用一次模型，记下这一次花了多少钱。存在 data/ledger.jsonl。

为什么要另记一份。信息栏里的"本次运行估算"只在内存里，程序一重启就从零开始，看不出这个月一共花了多少。
定时任务在你不看着的时候也会调用模型（而且按你的选择不设上限），就更需要一本不会丢的账。

每一行是一次调用：

    {"at": "2026-11-20T14:03:12", "day": "2026-11-20", "provider": "deepseek", "model": "deepseek-flash",
     "source": "chat", "requests": 1, "input": 100, "cache_read": 40, "cache_write": 0, "output": 20,
     "searches": 0, "cost": 0.0000542, "offpeak": true}

  day      本地日期。"今天""本月"都按你电脑上的日期算
  source   是谁调用的：chat 是你在对话，task 是定时任务（定时任务记账时传进来，见 Ledger.recorder），
           voice 是把聊天里的语音转成文字（见 voice/ 和 Ledger.record_audio：它按分钟算钱，token 都是 0，多记一个 seconds）
  cost     这一次的估算金额（美元）。价格按调用的那一刻算：DeepSeek 是高峰还是闲时就看那一刻
           （见 pricing.usage_key），以后你改了价格，记下的旧账不跟着变。
           没有价格的模型写 null：token 数照记，金额不算进去，页面上单独列出来
  offpeak  这一次按闲时价算的（只有 DeepSeek 有闲时）。只是给人看的，算钱时不再用它

为什么"一行一条、只往末尾追加"（JSONL），而不是每次把整个文件重写一遍：
  - 记一笔只往末尾写一行，账本再长，记一笔的代价都一样小；
  - 程序在写的途中崩溃，最多留下半行。读的时候跳过读不懂的行，之前的账一条不少；
  - 下一笔追加之前，如果文件末尾是半行（没有换行符），先补一个换行，新的一笔不会和那半行粘在一起。

账本不会无限变大：超过 LEDGER_DETAIL_DAYS 天的明细，按"哪一天、哪一家的哪个模型、哪种来源"合并成一行
（没有 "at"，requests 是合并了多少次），每天、每月、每个模型花了多少都不变。合并要重写整个文件，
所以用 safe.write_file（先写临时文件、再一步替换），启动时和过了午夜后的第一笔各检查一次，平时不做。
合并时读不懂的行原样留着：不认识的东西不删。

内存里只放按天汇总好的数（每天、每个模型、每种来源一份），不放每一笔明细，所以算"今天""本月"、
画最近 30 天的图都很快，占的内存也只和"天数 × 模型数"有关。

终端界面和网页界面不是同一个程序时（两个都开着），各自往同一个文件末尾追加，不会互相覆盖；
只是一边看不到另一边刚记的账，重启后就都有了。
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import LEDGER_DETAIL_DAYS
from .pricing import OFFPEAK, estimate, usage_key
from .providers.base import Usage
from .safe import clean_text, loads_strict, private_dir, to_json, write_file

_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
COUNTS = ("requests", "input", "cache_read", "cache_write", "output", "searches")

# 汇总时用的"组"：(哪一天, 哪一家, 哪个模型, 来源, 有没有价格)。
# 有没有价格也算在组里：同一个模型，前几天没价格、后来你填上了，两部分分开汇总，没价格的那部分不会被当成 0 元混进金额
Key = tuple[str, str, str, str, bool]


@dataclass
class Tally:
    """一组调用加起来的数：调用次数、各类 token、联网搜索次数、金额（美元）。"""

    requests: int = 0
    input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    searches: int = 0
    cost: float = 0.0

    def add(self, other: Tally) -> None:
        for name in COUNTS:
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.cost += other.cost

    def as_dict(self) -> dict:
        data = asdict(self)
        data["cost"] = round(self.cost, 6)
        return data


def _parse(line: str) -> tuple[Key, Tally, bool] | None:
    """读懂账本里的一行。返回（组，数，是不是一笔明细）；读不懂（半行、被改坏了）返回 None。

    只检查要用到的字段，多出来的字段不管：以后的版本多记了什么，这个版本照样读得懂。
    """
    try:
        row = loads_strict(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(row, dict):
        return None
    day, provider, model, source, cost = row.get("day"), row.get("provider"), row.get("model"), row.get("source", "chat"), row.get("cost")
    if not (isinstance(day, str) and _DAY.fullmatch(day) and isinstance(model, str) and model and isinstance(provider, str) and isinstance(source, str)):
        return None
    counts = [row.get(name, 0) for name in COUNTS]
    if not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in counts):
        return None
    if cost is not None and (isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0):
        return None
    tally = Tally(*counts, cost=float(cost or 0))
    return (day, provider, model, source, cost is not None), tally, "at" in row


def _row(key: Key, tally: Tally) -> dict:
    """一组汇总好的数，写回账本时的样子（合并以后的那种行，没有 "at"）。"""
    day, provider, model, source, priced = key
    data = tally.as_dict()
    return {"day": day, "provider": provider, "model": model, "source": source, **data, "cost": data["cost"] if priced else None}


class Ledger:
    def __init__(self, path: Path, detail_days: int = LEDGER_DETAIL_DAYS, today: date | None = None):
        self.path = path
        self.detail_days = detail_days
        # 网页界面里，对话和定时任务可能同时在调用模型；记账、合并、出报表都要拿着这把锁
        self._lock = threading.Lock()
        self._tallies: dict[Key, Tally] = {}
        self._day_cost: dict[str, float] = {}  # 每天花了多少：算"今天""本月"用，不用每次把所有组都加一遍
        self._oldest_detail: str | None = None  # 最早的一笔明细是哪天的：用来判断要不要合并
        self._checked_on: date | None = None  # 哪一天检查过要不要合并
        self.skipped = 0  # 读的时候跳过了几行读不懂的
        self.error = ""  # 最近一次写账本失败的原因（磁盘满了、没有权限……）；写成功了就是空的
        self._load()
        with self._lock:
            self._maybe_compact(today or date.today())

    # ---- 读 ----------------------------------------------------------------

    def _load(self) -> None:
        try:
            # errors="replace"：最后半行可能把一个汉字截成了两半，不能因此整个文件都读不了
            text = self.path.read_text(encoding="utf-8", errors="replace") if self.path.is_file() else ""
        except OSError as exc:
            self.error = str(exc) or type(exc).__name__
            return
        for line in text.splitlines():
            if not line.strip():
                continue
            parsed = _parse(line)
            if parsed is None:
                self.skipped += 1
                continue
            key, tally, detail = parsed
            self._add(key, tally, detail)

    def _add(self, key: Key, tally: Tally, detail: bool) -> None:
        """把一组数加进内存里的汇总。调用的人要先拿到锁（启动时读文件那会儿还没有别的线程，不用）。"""
        self._tallies.setdefault(key, Tally()).add(tally)
        self._day_cost[key[0]] = self._day_cost.get(key[0], 0.0) + tally.cost
        if detail and (self._oldest_detail is None or key[0] < self._oldest_detail):
            self._oldest_detail = key[0]

    # ---- 记一笔 ------------------------------------------------------------

    def recorder(self, prices: Callable[[], dict[str, dict]], source: str = "chat") -> Callable[[str, str, Usage, datetime], float | None]:
        """给 Agent.on_call 用的回调：agent 每调用完一次模型，就按当时的价格记一笔。

        prices 是"用户自己填的价格"（每次记账时现取，你在设置里改了价格，下一笔就按新的算）。
        source 是这些调用算谁的：对话是 chat；定时任务给它自己的那个 agent 接上 recorder(..., source="task")。
        回调交回这一笔的金额（见 record），运行记录（trace/）照着写，钱只在这里算一次。
        """
        return lambda provider, model, usage, moment: self.record(provider, model, usage, moment, prices(), source)

    def record(self, provider: str, model: str, usage: Usage, moment: datetime, prices: dict[str, dict], source: str = "chat") -> float | None:
        """记下一次模型调用。价格按 moment 这一刻算（见 pricing.usage_key）。返回这一笔的金额（美元，和写进账本的一样），
        没有价格的模型返回 None。

        写不进文件也不抛异常：对话不能因为记账失败而中断。这一笔照样算进内存里的汇总（这次运行里看得到），
        失败的原因放在 self.error 里，页面上会显示出来。
        """
        key = usage_key(model, moment, provider)  # 自定义接口的模型按"接口/模型"找价格（见 pricing.usage_key）
        cost, unpriced = estimate({key: usage}, prices)
        priced = not unpriced
        tally = Tally(
            requests=usage.requests,
            input=usage.input_tokens,
            cache_read=usage.cache_read_tokens,
            cache_write=usage.cache_write_tokens,
            output=usage.output_tokens,
            searches=usage.searches,
            cost=cost if priced else 0.0,
        )
        day = moment.date()
        group: Key = (day.isoformat(), clean_text(provider), clean_text(model), clean_text(source), priced)
        row = {"at": moment.isoformat(timespec="seconds"), **_row(group, tally)}
        if key.endswith(OFFPEAK):
            row["offpeak"] = True
        with self._lock:
            self._add(group, tally, detail=True)
            try:
                self._append(to_json(row) + "\n")
                self.error = ""
            except OSError as exc:
                self.error = str(exc) or type(exc).__name__
            self._maybe_compact(day)
        return row["cost"]

    def record_audio(self, provider: str, model: str, seconds: float, cost: float, moment: datetime, source: str = "voice") -> float:
        """记下一次语音转文字（见 voice/）。它不按 token 算钱，按语音的长短：金额由调用的人用 pricing.transcribe_cost 算好交进来。

        写成和别的调用一样的一行（requests 是 1，token 都是 0），多一个 seconds（这段语音有多少秒，只是给人看的，
        读账本时不用它）：今天、本月、按模型、按来源的汇总都照常算进去。写不进文件和 record 一样只记下原因。
        """
        tally = Tally(requests=1, cost=max(0.0, float(cost)))
        group: Key = (moment.date().isoformat(), clean_text(provider), clean_text(model), clean_text(source), True)
        row = {"at": moment.isoformat(timespec="seconds"), **_row(group, tally), "seconds": round(max(0.0, float(seconds)), 1)}
        with self._lock:
            self._add(group, tally, detail=True)
            try:
                self._append(to_json(row) + "\n")
                self.error = ""
            except OSError as exc:
                self.error = str(exc) or type(exc).__name__
            self._maybe_compact(moment.date())
        return row["cost"]

    def _append(self, line: str) -> None:
        """往文件末尾写一行。新文件只有自己的账号能读。"""
        private_dir(self.path.parent)
        # O_BINARY：Windows 上 os.open 默认是文字模式，会把 \n 写成 \r\n（别的系统上没有这个标记，是 0）
        descriptor = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            size = os.fstat(descriptor).st_size
            if size and _last_byte(descriptor, size) != b"\n":
                line = "\n" + line  # 上一次写到一半就断了：另起一行，不和那半行粘在一起
            data = line.encode()
            while data:  # 一行很短，一次就写完；万一没写完，接着写剩下的
                data = data[os.write(descriptor, data) :]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    # ---- 合并旧明细 --------------------------------------------------------

    def _maybe_compact(self, today: date) -> None:
        """一天检查一次：有超过 detail_days 天的明细，就合并。调用的人要先拿到锁。"""
        if self._checked_on == today:
            return
        self._checked_on = today
        cutoff = (today - timedelta(days=self.detail_days)).isoformat()
        if self._oldest_detail is None or self._oldest_detail >= cutoff:
            return
        try:
            self._compact(cutoff)
        except OSError as exc:
            self.error = str(exc) or type(exc).__name__  # 没合并成：文件原封不动，明天再试

    def _compact(self, cutoff: str) -> None:
        text = self.path.read_text(encoding="utf-8", errors="replace")
        merged: dict[Key, Tally] = {}
        kept: list[str] = []
        oldest: str | None = None
        for line in text.splitlines():
            if not line.strip():
                continue
            parsed = _parse(line)
            if parsed is None:
                kept.append(line)  # 读不懂的不删（也许是以后的版本写的）
                continue
            key, tally, detail = parsed
            if key[0] < cutoff:
                merged.setdefault(key, Tally()).add(tally)
            else:
                kept.append(line)
                if detail and (oldest is None or key[0] < oldest):
                    oldest = key[0]
        rows = [to_json(_row(key, merged[key])) for key in sorted(merged, key=lambda key: (key[0], key[1], key[2], key[3], not key[4]))]
        write_file(self.path, "".join(line + "\n" for line in rows + kept))
        self._oldest_detail = oldest

    # ---- 查 ----------------------------------------------------------------

    def day_total(self, day: date) -> float:
        """这一天一共花了多少（美元，只算有价格的调用）。"""
        with self._lock:
            return round(self._day_cost.get(day.isoformat(), 0.0), 6)

    def month_total(self, month: str) -> float:
        """这个月（"2026-11"）一共花了多少。"""
        with self._lock:
            return round(sum(cost for day, cost in self._day_cost.items() if day.startswith(month + "-")), 6)

    def report(self, today: date, chart_days: int) -> dict:
        """"花费明细"窗口要的全部数字：最近 chart_days 天每天的花费、每个月的、按模型和来源分开的。

        按模型和来源分开的有三份（本月、最近 chart_days 天、全部），页面上切换时不用再来要。
        """
        with self._lock:
            items = [(key, Tally(**asdict(tally))) for key, tally in self._tallies.items()]
            error, skipped = self.error, self.skipped
        start = (today - timedelta(days=chart_days - 1)).isoformat()
        end, month = today.isoformat(), f"{today:%Y-%m}"
        chart = {(today - timedelta(days=back)).isoformat(): [Tally(), 0] for back in range(chart_days - 1, -1, -1)}
        months: dict[str, Tally] = {}
        for (day, _, _, _, priced), tally in items:
            if day in chart:
                chart[day][0].add(tally)
                if not priced:
                    chart[day][1] += tally.requests
            months.setdefault(day[:7], Tally()).add(tally)
        return {
            "today": end,
            "first": min((key[0] for key, _ in items), default=None),
            "days": [{"day": day, **tally.as_dict(), "unpriced": unpriced} for day, (tally, unpriced) in chart.items()],
            "months": [{"month": name, **months[name].as_dict()} for name in sorted(months, reverse=True)],
            "periods": {
                "month": _breakdown(item for item in items if item[0][0][:7] == month),
                "recent": _breakdown(item for item in items if start <= item[0][0] <= end),
                "all": _breakdown(items),
            },
            "error": error,
            "skipped": skipped,
        }


def _breakdown(items: Iterable[tuple[Key, Tally]]) -> dict:
    """一段时间里的总数，以及按模型、按来源分开的数。没有价格的模型单独列出来（只有 token 数）。"""
    total = Tally()
    models: dict[tuple[str, str], Tally] = {}
    unpriced: dict[tuple[str, str], Tally] = {}
    sources: dict[str, Tally] = {}
    for (_, provider, model, source, priced), tally in items:
        total.add(tally)
        (models if priced else unpriced).setdefault((provider, model), Tally()).add(tally)
        sources.setdefault(source, Tally()).add(tally)

    def ranked(groups: dict, name: Callable[[object], dict]) -> list[dict]:
        order = sorted(groups.items(), key=lambda pair: (-pair[1].cost, -pair[1].requests, str(pair[0])))
        return [{**name(group), **tally.as_dict()} for group, tally in order]

    return {
        **total.as_dict(),
        "models": ranked(models, lambda group: {"provider": group[0], "model": group[1]}),
        "unpriced": ranked(unpriced, lambda group: {"provider": group[0], "model": group[1]}),
        "sources": ranked(sources, lambda group: {"source": group}),
    }


def _last_byte(descriptor: int, size: int) -> bytes:
    """文件的最后一个字节。macOS、Linux 用 pread（不挪读写的位置）；Windows 没有 pread，先挪过去再读：
    文件是追加着打开的，之后的 write 总是写在末尾，挪过的位置不影响它。"""
    if hasattr(os, "pread"):
        return os.pread(descriptor, 1, size - 1)
    os.lseek(descriptor, size - 1, os.SEEK_SET)
    return os.read(descriptor, 1)
