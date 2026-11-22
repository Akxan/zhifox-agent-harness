"""对比两次运行：harness eval compare A B。

先说两次运行各是什么（模型、提示词的指纹、代码的版本），再看总的：全部通过的任务、检查项的通过率、花费、用时，
然后一个任务一个任务地看，变坏了的任务下面列出是哪几项检查从通过变成了没通过，以及 B 那一次为什么没通过。

只比两次都跑了的任务：两次的任务不一样（加了新任务、只跑了一部分）时，总的数字只算共同的那些，并且说明。

读数字的时候记着误差：一个任务只跑一遍时，"这次过了、那次没过"可能只是运气（模型每次回答不完全一样）。
总的通过率旁边写着大约的误差；差得比它小，就多跑几遍（--repeat）再下结论。
"""

from __future__ import annotations

from ..i18n import t
from .results import Run, by_task, summary_of
from .text import money, percent, seconds, signed_money, table


def describe_run(label: str, run: Run) -> str:
    meta = run.meta
    mode = {"dry": t("演习"), "null": t("演习（什么都不做）")}.get(meta.get("mode", ""), "")
    parts = [f"{meta.get('provider')}/{meta.get('model')}", t("提示词 {sha}", sha=meta.get("prompt_sha", "?"))]
    if meta.get("git"):
        parts.append(t("代码 {commit}", commit=meta["git"]))
    if mode:
        parts.append(mode)
    return f"{label}  {meta['id']}  " + " · ".join(parts)


def compare_lines(a: Run, b: Run) -> list[str]:
    """对比的全部文字，一行一项。"""
    tasks_a, tasks_b = by_task(a.rows), by_task(b.rows)
    common = [task for task in tasks_a if task in tasks_b]
    lines = [describe_run("A", a), describe_run("B", b), ""]
    if bool(a.meta.get("mode")) != bool(b.meta.get("mode")):
        lines += [t("注意：一次是演习、一次是真的运行，花费和用时没有可比性。"), ""]
    only_a = sorted(set(tasks_a) - set(tasks_b))
    only_b = sorted(set(tasks_b) - set(tasks_a))
    if only_a or only_b:
        lines.append(t("只比两次都跑了的 {n} 个任务。只在 A 里的：{a}；只在 B 里的：{b}", n=len(common), a=", ".join(only_a) or "—", b=", ".join(only_b) or "—"))
        lines.append("")
    if not common:
        return [*lines, t("两次运行没有共同的任务，没法对比。")]

    rows_a = [row for task in common for row in tasks_a[task]["rows"]]
    rows_b = [row for task in common for row in tasks_b[task]["rows"]]
    sa, sb = summary_of(rows_a), summary_of(rows_b)

    def points(x: float | None, y: float | None) -> str:
        if x is None or y is None:
            return "—"
        return t("{n} 个百分点", n=f"{(y - x) * 100:+.0f}")

    noise = max(value for value in (sa.noise, sb.noise, 0.0) if value is not None)
    cost_a, cost_b = sa.cost + sa.judge_cost, sb.cost + sb.judge_cost
    change = f" ({(cost_b - cost_a) / cost_a * 100:+.0f}%)" if cost_a else ""
    overview = [
        ["", "A", "B", t("变化")],
        [t("全部通过的任务"), f"{sa.passed}/{sa.ran} ({percent(sa.pass_rate)})", f"{sb.passed}/{sb.ran} ({percent(sb.pass_rate)})",
         points(sa.pass_rate, sb.pass_rate) + (t("（误差约 ±{n}）", n=f"{noise * 100:.0f}") if noise else "")],
        [t("检查项通过率"), percent(sa.check_rate), percent(sb.check_rate), points(sa.check_rate, sb.check_rate)],
        [t("花费"), money(cost_a), money(cost_b), signed_money(cost_b - cost_a) + change],
        [t("平均每遍用时"), seconds(sa.seconds), seconds(sb.seconds), f"{sb.seconds - sa.seconds:+.1f} s"],
    ]
    if sa.failed or sb.failed or sa.skipped or sb.skipped:
        overview.append([t("出错或超时 / 跳过"), f"{sa.failed} / {sa.skipped}", f"{sb.failed} / {sb.skipped}", ""])
    lines += table(overview)
    if sa.unpriced or sb.unpriced:
        lines.append(t("（没有价格、没算进花费的模型：{models}）", models=", ".join(sorted(set(sa.unpriced) | set(sb.unpriced)))))
    lines += ["", t("每个任务（↑ 变好  ↓ 变坏）")]

    rows: list[list[str]] = []
    notes: dict[int, list[str]] = {}
    for task in common:
        ta, tb = tasks_a[task]["summary"], tasks_b[task]["summary"]
        rate_a, rate_b = ta.pass_rate, tb.pass_rate
        mark = " "
        if rate_a is not None and rate_b is not None:
            mark = "↑" if rate_b > rate_a else "↓" if rate_b < rate_a else " "
        elif rate_a is not None or rate_b is not None:
            mark = "?"
        rows.append([
            mark,
            task,
            f"{ta.passed}/{ta.ran} → {tb.passed}/{tb.ran}",
            f"{money(ta.cost / ta.cases if ta.cases else None)} → {money(tb.cost / tb.cases if tb.cases else None)}",
            f"{seconds(ta.seconds)} → {seconds(tb.seconds)}",
        ])
        flipped = _flipped(tasks_a[task]["checks"], tasks_b[task]["checks"])
        failure = _failure(tasks_b[task]["rows"])
        if flipped or (mark in ("↓", "?") and failure):
            notes[len(rows) - 1] = [*flipped, *([failure] if failure else [])]
    for n, line in enumerate(table(rows, indent="  ")):
        lines.append(line)
        lines += [f"        {note}" for note in notes.get(n, [])]
    return lines


def _flipped(checks_a: list[dict], checks_b: list[dict]) -> list[str]:
    """哪几项检查的通过率变了（检查项按顺序对应；任务文件改过、检查对不上的，按说明对）。"""
    out = []
    by_label = {check["label"]: check for check in checks_a}
    for check in checks_b:
        before = by_label.get(check["label"])
        if before is None or not before["judged"] or not check["judged"]:
            continue
        rate_a, rate_b = before["passed"] / before["judged"], check["passed"] / check["judged"]
        if rate_b < rate_a:
            out.append(f"✗ {check['label']}" + (f" — {check['detail']}" if check["detail"] else ""))
        elif rate_b > rate_a:
            out.append(f"✓ {check['label']}")
    return out


def _failure(rows: list[dict]) -> str:
    """这几遍里出错、超时的原因（有的话）。"""
    for row in rows:
        if row.get("status") in ("error", "timeout", "stopped"):
            return f"({row['status']}: {row.get('error', '')})"
    return ""
