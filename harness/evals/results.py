"""评测的结果存在哪儿、长什么样。

    data/evals/                                 （在数据目录里，和会话、记忆一样不提交）
      20261121-153012-claude-opus-5-5/          一次运行一个文件夹：开始的时间加上模型（演习的后面带 -dry、-null）
        run.json                                这次运行是什么：哪个模型、哪些任务、跑几遍、哪一版提示词（指纹）、哪一版代码、
                                                开始和结束的时间、跑完没有，以及汇总（summary）
        results.jsonl                           一遍一行：每一项检查过没过、用了多少 token、花了多少钱、用了多久、出了什么事
        traces/<任务>_rep<第几遍>.json          这一遍发生的每一件事（见 recorder.py；第一项是系统提示词）
        traces/<任务>_rep<第几遍>.history.json  发给模型的原始对话历史（各家的格式），要追查细节时看
        prompt.md                               这次用的提示词模板

每跑完一遍就写一次（results.jsonl 整个重写，用 safe.write_file，要么写成、要么不动）：中途断了，已经跑完的不会丢。
文件夹和文件只有你自己的账号能读：记录里有你的工作目录里的内容（虽然是评测编的）、模型说的话。

汇总（summarize）只在这里算一次，打印、对比都用它，同一条规则不写两遍：
  passed / ran   全部检查都通过的遍数 / 跑完了的遍数（跳过的、出错的、超时的不算在里面，另外数）
  check_rate     通过的检查 / 判断了的检查
  noise          通过率大约的误差（±，按 1/√遍数 估的，95% 的把握）：两次运行的通过率差得比它还小，多半只是运气
  cost           被评的模型花的钱，加上评分员花的钱（分开记着）；有没有价格的模型另外列出来
"""

from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..i18n import t
from ..safe import loads_strict, private_dir, to_json, write_file

RUN_FILE = "run.json"
ROWS_FILE = "results.jsonl"
TRACES = "traces"
PROMPT_FILE = "prompt.md"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class RunNotFound(LookupError):
    pass


def run_id(started: datetime, model: str, mode: str) -> str:
    """一次运行的编号：开始的时间、模型，演习的带上 dry 或 null。只有字母、数字、点、连字符和下划线（三个系统上都能当文件夹名）。"""
    name = _UNSAFE.sub("_", model).strip("._") or "model"
    return f"{started:%Y%m%d-%H%M%S}-{name}" + (f"-{mode}" if mode else "")


class Run:
    """一次正在进行（或者已经结束）的运行，存在 folder 里。"""

    def __init__(self, folder: Path, meta: dict, rows: list[dict] | None = None):
        self.folder = folder
        self.meta = meta
        self.rows: list[dict] = list(rows or [])
        self._lock = threading.Lock()  # 几个任务同时跑完，"加一行、写回文件"要一个一个来

    @classmethod
    def create(cls, root: Path, meta: dict, prompt: str) -> Run:
        private_dir(root)
        base = meta["id"]
        folder = root / base
        n = 1
        while folder.exists():  # 同一秒开始了两次（几乎不会）：后面那个加个序号
            n += 1
            folder = root / f"{base}-{n}"
        private_dir(folder / TRACES)
        meta = {**meta, "id": folder.name}
        run = cls(folder, meta)
        write_file(folder / PROMPT_FILE, prompt)
        run._write_meta()
        write_file(folder / ROWS_FILE, "")
        return run

    def add(self, row: dict, trace: list[dict], history: list) -> None:
        """一遍跑完了：记下它的那一行和记录。"""
        stem = f"{row['task']}_rep{row['rep']}"
        with self._lock:
            if trace:
                write_file(self.folder / TRACES / f"{stem}.json", to_json(trace, readable=True, indent=1))
            if history:
                write_file(self.folder / TRACES / f"{stem}.history.json", to_json(history, readable=True, indent=1))
            self.rows.append(row)
            self.rows.sort(key=lambda item: (item["task"], item["rep"]))
            write_file(self.folder / ROWS_FILE, "".join(to_json(item, readable=True) + "\n" for item in self.rows))

    def finish(self, status: str, finished: datetime) -> None:
        with self._lock:
            self.meta.update(status=status, finished_at=finished.isoformat(timespec="seconds"), summary=summarize(self.rows))
            self._write_meta()

    def _write_meta(self) -> None:
        write_file(self.folder / RUN_FILE, to_json(self.meta, readable=True, indent=2))


def _load(folder: Path) -> Run | None:
    """读回一次运行。读不懂的（写到一半、被改坏了）返回 None；results.jsonl 里读不懂的行跳过。"""
    try:
        meta = loads_strict((folder / RUN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(meta, dict) or not isinstance(meta.get("id"), str):
        return None
    rows = []
    try:
        lines = (folder / ROWS_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            row = loads_strict(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(row, dict) and isinstance(row.get("task"), str) and isinstance(row.get("rep"), int):
            rows.append(row)
    return Run(folder, meta, rows)


def list_runs(root: Path) -> list[Run]:
    """存着的全部运行，新的在前。"""
    if not root.is_dir():
        return []
    runs = [run for folder in root.iterdir() if folder.is_dir() and (run := _load(folder)) is not None]
    return sorted(runs, key=lambda run: (run.meta.get("started_at", ""), run.meta["id"]), reverse=True)


def find_run(root: Path, ref: str) -> Run:
    """按编号找一次运行。可以只写编号的开头（唯一的话），也可以写 latest（最新的一次）、previous（它前面那一次）。"""
    runs = list_runs(root)
    if ref in ("latest", "previous"):
        index = 0 if ref == "latest" else 1
        if len(runs) > index:
            return runs[index]
        raise RunNotFound(t("还没有这么多次运行（一共 {n} 次）。", n=len(runs)))
    exact = [run for run in runs if run.meta["id"] == ref]
    found = exact or [run for run in runs if run.meta["id"].startswith(ref)]
    if len(found) == 1:
        return found[0]
    if not found:
        raise RunNotFound(t("找不到编号是 {ref} 的运行。harness eval list 列出全部。", ref=ref))
    raise RunNotFound(t("有 {n} 次运行的编号以 {ref} 开头，请写得长一点。", n=len(found), ref=ref))


# ---- 汇总 -------------------------------------------------------------------


@dataclass
class Summary:
    cases: int  # 一共几遍
    ran: int  # 跑完了的（status 是 ok）
    passed: int  # 其中全部检查都通过的
    skipped: int
    failed: int  # 出错、超时、被中断的（"管道"的问题，不算模型做错）
    checks: int  # 判断了的检查
    checks_passed: int
    cost: float  # 被评的模型花的钱（有价格的那些）
    judge_cost: float  # 评分员花的钱
    unpriced: list[str]  # 没有价格、没算进 cost 的模型
    seconds: float  # 跑完了的那些平均每遍用了多久
    tokens_in: int
    tokens_out: int
    requests: int

    @property
    def pass_rate(self) -> float | None:
        return self.passed / self.ran if self.ran else None

    @property
    def check_rate(self) -> float | None:
        return self.checks_passed / self.checks if self.checks else None

    @property
    def noise(self) -> float | None:
        """通过率大约的误差（95% 的把握，二项分布最坏的情况 1/√n）。只是粗略地提醒：遍数少的时候，差几个百分点说明不了什么。"""
        return 1 / math.sqrt(self.ran) if self.ran else None

    def as_dict(self) -> dict:
        return {**self.__dict__, "pass_rate": self.pass_rate, "check_rate": self.check_rate, "noise": self.noise}


def summarize(rows: list[dict]) -> dict:
    return summary_of(rows).as_dict()


def summary_of(rows: list[dict]) -> Summary:
    ran = [row for row in rows if row.get("status") == "ok"]
    unpriced: list[str] = []
    cost = judge_cost = 0.0
    tokens_in = tokens_out = requests = 0
    for row in rows:
        cost += row.get("cost") or 0.0
        judge = row.get("judge") or {}
        judge_cost += judge.get("cost") or 0.0
        for model in row.get("unpriced") or []:
            if model not in unpriced:
                unpriced.append(model)
        usage = row.get("usage") or {}
        tokens_in += usage.get("input", 0) + usage.get("cache_read", 0) + usage.get("cache_write", 0)
        tokens_out += usage.get("output", 0)
        requests += usage.get("requests", 0)
    return Summary(
        cases=len(rows),
        ran=len(ran),
        passed=sum(1 for row in ran if row.get("passed")),
        skipped=sum(1 for row in rows if row.get("status") == "skipped"),
        failed=sum(1 for row in rows if row.get("status") in ("error", "timeout", "stopped")),
        checks=sum(row.get("checks", 0) for row in ran),
        checks_passed=sum(row.get("checks_passed", 0) for row in ran),
        cost=round(cost, 6),
        judge_cost=round(judge_cost, 6),
        unpriced=unpriced,
        seconds=round(sum(row.get("seconds", 0.0) for row in ran) / len(ran), 2) if ran else 0.0,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        requests=requests,
    )


def by_task(rows: list[dict]) -> dict[str, dict]:
    """每个任务的几遍合在一起：跑完了几遍、通过了几遍、平均花多少钱、平均用多久、每一项检查通过了几遍。"""
    tasks: dict[str, dict] = {}
    for row in rows:
        entry = tasks.setdefault(row["task"], {"title": row.get("title", ""), "rows": []})
        entry["rows"].append(row)
    for entry in tasks.values():
        entry["summary"] = summary_of(entry["rows"])
        checks: dict[int, dict] = {}
        for row in entry["rows"]:
            if row.get("status") != "ok":
                continue
            for n, grade in enumerate(row.get("grades") or []):
                item = checks.setdefault(n, {"label": grade.get("label", ""), "passed": 0, "judged": 0, "detail": ""})
                if grade.get("passed") is not None:
                    item["judged"] += 1
                    item["passed"] += 1 if grade["passed"] else 0
                    if not grade["passed"] and not item["detail"]:
                        item["detail"] = grade.get("detail", "")
        entry["checks"] = [checks[n] for n in sorted(checks)]
    return tasks
