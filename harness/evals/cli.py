"""harness eval 这一组命令。

    harness eval tasks                          列出评测任务
    harness eval run                            演习（默认）：假模型按标准答案回答，不花钱，检查评测框架本身
    harness eval run --null                     演习，但模型什么都不做：看看打分是不是太松
    harness eval run --real --model anthropic/claude-sonnet-5-5
                                                真的运行：先估算要花多少钱，加 --yes 或者输入 yes 才开始
    harness eval list                           存着的运行
    harness eval show [编号]                    一次运行的每个任务、每一项检查（默认是最新的一次）
    harness eval compare [A] [B]                对比两次运行（默认是前一次和最新的一次）

花钱的规矩（CLAUDE.md 的第 3 条）在 _confirm：不写 --real 就不会发真的请求；写了，先把估算打印出来，
要么加了 --yes，要么在终端里亲手输入 yes，否则不跑；不是在终端里运行（被别的程序调用）又没有 --yes 的，一律不跑。
在 CI 里（环境变量 CI 有值，GitHub Actions 就是这样）真的运行一律拒绝：测试和 CI 永远不花钱。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import IO

from ..agents.spec import resolve_model
from ..config import (
    EFFORTS,
    EVAL_ASSUMED_CONTEXT,
    EVAL_ASSUMED_OUTPUT,
    EVAL_ASSUMED_REQUESTS,
    EVAL_JUDGE_MODEL,
    EVAL_MAX_REPEAT,
    EVAL_PARALLEL,
    EVAL_RESULTS_DIR,
    EVAL_TASKS_DIR,
    PROJECT_ROOT,
    Config,
)
from ..i18n import k, t
from ..ledger import Ledger
from ..prompt import DEFAULT_TEMPLATE, load_template
from ..providers import PROVIDERS, has_key
from . import results
from .compare import compare_lines
from .dryrun import DryDriver, DryRunUnavailable
from .estimate import Estimate, estimate_run
from .runner import CaseResult, Options, RealDriver, Target, run_all
from .spec import Task, TaskFileError, load_tasks, select
from .text import money, percent, seconds, table

YES = ("y", "yes", "是", "sí", "si")
MODES = {"dry": k("演习"), "null": k("演习（什么都不做）"), "": k("真的")}
STATUS_MARKS = {"skipped": "–", "error": "!", "timeout": "!", "stopped": "!"}


def add_parser(commands: argparse._SubParsersAction) -> None:
    """在 harness 的命令行里加上 eval 这一组命令。"""
    parser = commands.add_parser("eval", help=t("评测：让智狐做一组真实的小事并打分，看改了提示词、换了模型以后是变好还是变坏（默认是不花钱的演习）"))
    actions = parser.add_subparsers(dest="eval_action", required=True, metavar="ACTION")

    def task_options(action: argparse.ArgumentParser) -> None:
        action.add_argument("--tasks", help=t("只要这些任务：逗号隔开的编号（可以用 * 通配）或标签，比如 files-*,memory"))
        action.add_argument("--tasks-dir", type=Path, default=None, help=t("任务文件所在的文件夹（默认是仓库里的 evals/tasks/）"))

    task_options(actions.add_parser("tasks", help=t("列出评测任务")))
    run = actions.add_parser("run", help=t("跑一次评测（默认是演习，不花钱；--real 才真的调用模型）"))
    task_options(run)
    run.add_argument("--model", help=t("评哪个模型：提供方/模型（anthropic/claude-sonnet-5-5、deepseek/deepseek-flash），或者只写模型名。默认是你现在选的那个"))
    run.add_argument("--repeat", type=int, default=1, help=t("每个任务跑几遍（默认 1）。模型每次回答不完全一样，多跑几遍结果才可靠"))
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help=t("演习（默认）：模型换成本地的假服务器，按写好的标准答案回答，不花钱"))
    mode.add_argument("--null", action="store_true", help=t("演习，但模型什么都不做（只说一句“好的”）：看看打分是不是太松，不花钱"))
    mode.add_argument("--real", action="store_true", help=t("真的运行：向模型的 API 发请求，要花钱。先给你看估算，加 --yes 或者输入 yes 才开始"))
    run.add_argument("--yes", action="store_true", help=t("看过估算了，不用再问"))
    run.add_argument("--judge", action="store_true", help=t("也做请模型评分的那几项检查（要另外花钱，默认不做）"))
    run.add_argument("--judge-model", default=EVAL_JUDGE_MODEL, help=t("评分员用哪个 Claude 模型（默认 {model}）。不要用被评的那个模型", model=EVAL_JUDGE_MODEL))
    run.add_argument("--effort", choices=EFFORTS, default=None, help=t("Claude 想多深（默认和平时一样）"))
    run.add_argument("--prompt", help=t("用哪一版提示词：一个文件的路径，或者 builtin（内置的）。默认是你现在用的那一版"))
    run.add_argument("--parallel", type=int, default=EVAL_PARALLEL, help=t("同时跑几个任务（默认 {n}）", n=EVAL_PARALLEL))
    run.add_argument("--keep", action="store_true", help=t("跑完留着每个任务的临时文件夹，方便进去看它做了什么"))
    actions.add_parser("list", help=t("列出存着的运行"))
    show = actions.add_parser("show", help=t("看一次运行的结果：每个任务、每一项检查"))
    show.add_argument("run", nargs="?", default="latest", help=t("运行的编号（可以只写开头），或者 latest、previous。默认 latest"))
    compare = actions.add_parser("compare", help=t("对比两次运行：每个任务的得分、花费和用时变了多少"))
    compare.add_argument("a", nargs="?", default="previous", help=t("拿来比的那一次（默认 previous：最新的前一次）"))
    compare.add_argument("b", nargs="?", default="latest", help=t("要看的这一次（默认 latest：最新的一次）"))


def run(
    args: argparse.Namespace,
    config: Config,
    out: IO[str] | None = None,
    ask: Callable[[str], str] = input,
    interactive: bool | None = None,
) -> int:
    out = out or sys.stdout
    root = config.data_dir / EVAL_RESULTS_DIR
    action = args.eval_action
    try:
        if action == "tasks":
            return _tasks(args, out)
        if action == "run":
            return _run(args, config, root, out, ask, sys.stdin.isatty() if interactive is None else interactive)
        if action == "list":
            return _list(root, out)
        if action == "show":
            return _show(results.find_run(root, args.run), out)
        if action == "compare":
            a, b = results.find_run(root, args.a), results.find_run(root, args.b)
            if a.folder == b.folder:
                print(t("A 和 B 是同一次运行。"), file=sys.stderr)
                return 2
            for line in compare_lines(a, b):
                print(line, file=out)
            return 0
    except (TaskFileError, results.RunNotFound, DryRunUnavailable) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 2


def _load(args: argparse.Namespace) -> list[Task]:
    tasks = select(load_tasks(args.tasks_dir or EVAL_TASKS_DIR), args.tasks)
    if not tasks:
        raise TaskFileError(t("没有对得上 {pattern} 的任务。harness eval tasks 列出全部。", pattern=args.tasks or "*"))
    return tasks


# ---- tasks ------------------------------------------------------------------


def _tasks(args: argparse.Namespace, out: IO[str]) -> int:
    tasks = _load(args)
    rows = [[t("编号"), t("分组标签"), t("检查"), t("评测任务")]]
    for task in tasks:
        extra = t("，{n} 项要模型评分", n=task.judged) if task.judged else ""
        needs = f" [{', '.join(task.requires)}]" if task.requires else ""
        rows.append([task.id, ",".join(task.tags), f"{len(task.checks)}{extra}", task.title + needs])
    for line in table(rows):
        print(line, file=out)
    print(t("共 {n} 个任务。", n=len(tasks)), file=out)
    return 0


# ---- run --------------------------------------------------------------------


def _settings_of(config: Config) -> dict:
    """你的设置文件（只读）：评测抄它的价格、同步到的模型列表这些（见 runner.NOT_COPIED）。读不了就当没有。"""
    try:
        data = json.loads(config.settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _target(args: argparse.Namespace, saved: dict, config: Config) -> Target:
    if not args.model:
        provider = saved.get("provider") or config.default_provider
        model = saved.get("model") or config.default_model
        return Target(provider if provider in PROVIDERS else "anthropic", model)
    chosen, problem, _warning = resolve_model(args.model)
    if problem or not chosen:
        raise TaskFileError(t("--model 写得不对：{problem}", problem=problem or args.model))
    provider, _, model = chosen.partition("/")
    return Target(provider, model)


def _prompt(args: argparse.Namespace, config: Config) -> tuple[str | None, str, str]:
    """这次用哪一版提示词：（模板原文，None 是内置的；给人看的来源；模板原文，存进结果里）。"""
    if args.prompt == "builtin":
        return None, t("内置"), DEFAULT_TEMPLATE
    if args.prompt:
        path = Path(args.prompt).expanduser()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise TaskFileError(t("读不了提示词文件 {path}：{error}", path=path, error=exc)) from exc
        return text, str(path), text
    text = load_template(config)
    if text == DEFAULT_TEMPLATE:
        return None, t("内置"), text
    return text, str(config.prompt_file), text


def _git() -> str:
    """现在的代码是哪一版（提交的短编号；有没提交的改动时后面加 +）。不在 git 仓库里就是空的。"""
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], check=False, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=5)
        dirty = subprocess.run(["git", "status", "--porcelain"], check=False, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if commit.returncode != 0:
        return ""
    return commit.stdout.strip() + ("+" if dirty.stdout.strip() else "")


def _run(args: argparse.Namespace, config: Config, root: Path, out: IO[str], ask: Callable[[str], str], interactive: bool) -> int:
    tasks = _load(args)
    if not 1 <= args.repeat <= EVAL_MAX_REPEAT:
        raise TaskFileError(t("--repeat 要在 1 到 {n} 之间", n=EVAL_MAX_REPEAT))
    saved = _settings_of(config)
    target = _target(args, saved, config)
    prices = saved.get("prices") if isinstance(saved.get("prices"), dict) else {}
    template, prompt_label, prompt_text = _prompt(args, config)
    mode = "" if args.real else "null" if args.null else "dry"
    judge_model = args.judge_model if args.judge else None
    if judge_model and resolve_model(judge_model)[0].partition("/")[0] != "anthropic":
        raise TaskFileError(t("评分员只能用 Claude 的模型：{model}", model=judge_model))

    if mode:
        driver = DryDriver(null=mode == "null")
        ledger = None
    else:
        problem = _real_problem(target, judge_model)
        if problem:
            print(problem, file=sys.stderr)
            return 2
        if judge_model and judge_model == target.model:
            print(t("提醒：评分员和被评的是同一个模型（{model}），它往往偏爱自己写的回答。可以用 --judge-model 换一个。", model=judge_model), file=out)
        found = estimate_run(tasks, args.repeat, target, prices, results.list_runs(root), judge_model)
        if not _confirm(found, args, out, ask, interactive):
            return 2
        driver = RealDriver()
        ledger = Ledger(config.ledger_file)

    started = datetime.now()
    meta = {
        "id": results.run_id(started, target.model, mode),
        "mode": mode,
        "provider": target.provider,
        "model": target.model,
        "effort": args.effort or config.effort,
        "repeat": args.repeat,
        "tasks": [task.id for task in tasks],
        "judge_model": judge_model,
        "prompt": prompt_label,
        "prompt_sha": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:12],
        "git": _git(),
        "started_at": started.isoformat(timespec="seconds"),
        "status": "running",
    }
    stored = results.Run.create(root, meta, prompt_text)
    options = Options(effort=meta["effort"], template=template, settings=saved, judge_model=judge_model, keep=args.keep, ledger=ledger)
    print(t("开始评测（{mode}）：{model}，{n} 个任务 × {repeat} 遍，提示词：{prompt}", mode=t(MODES[mode]), model=target.label, n=len(tasks), repeat=args.repeat, prompt=prompt_label), file=out)

    def record(result: CaseResult) -> None:
        stored.add(result.row, result.trace, result.history)
        for line in _case_lines(result.row):
            print(line, file=out)
        out.flush()

    status = run_all(tasks, args.repeat, target, options, driver, record, args.parallel)
    stored.finish(status, datetime.now())
    for line in _summary_lines(stored):
        print(line, file=out)
    return 0 if status == "done" else 1


def _real_problem(target: Target, judge_model: str | None) -> str:
    """真的运行之前要满足的：不在 CI 里、有密钥。返回不满足的原因，满足就是空的。"""
    if os.environ.get("CI"):
        return t("在 CI 里不做真的评测（会花钱）。测试和 CI 只跑演习：harness eval run")
    provider = PROVIDERS.get(target.provider)
    if provider is None:
        return t("不认识这个模型提供方：{provider}", provider=target.provider)
    if not has_key(provider):
        return t("还没有设置 {provider} 的 API 密钥。在网页界面的设置里填，或者写进 .env 的 {key}。", provider=provider.label, key=provider.key_env)
    if judge_model and not has_key(PROVIDERS["anthropic"]):
        return t("请模型评分（--judge）要用 Claude，还没有设置 Anthropic 的 API 密钥。")
    return ""


def _confirm(found: Estimate, args: argparse.Namespace, out: IO[str], ask: Callable[[str], str], interactive: bool) -> bool:
    """把估算打印出来，问你要不要花这笔钱（规矩见文件开头）。"""
    for line in estimate_lines(found):
        print(line, file=out)
    if args.yes:
        return True
    if not interactive:
        print(t("没有加 --yes，所以没有运行。看过上面的估算、确认要花这笔钱的话，加上 --yes 再运行一次。"), file=out)
        return False
    try:
        answer = ask(t("确认要运行吗？输入 yes 继续，别的都是取消："))
    except (EOFError, KeyboardInterrupt):  # Ctrl-D、Ctrl-C 都当作"不"
        print(file=out)
        answer = ""
    if answer.strip().lower() in YES:
        return True
    print(t("已取消，没有发任何请求。"), file=out)
    return False


def estimate_lines(found: Estimate) -> list[str]:
    """估算给人看的样子：算法、几个数，以及"这会花真钱"。"""
    n = len(found.lines)
    lines = [t("预计花费：{model}，{n} 个任务 × {repeat} 遍", model=found.target.label, n=n, repeat=found.repeat)]
    assumed = n - found.measured
    if assumed:
        lines.append("  " + t(
            "{n} 个任务按假定的用量估算（任务文件的 [budget]，没写的按每遍调用 {requests} 次模型、每次带 {context} 个 token 的上下文、写 {output} 个 token）",
            n=assumed, requests=EVAL_ASSUMED_REQUESTS, context=f"{EVAL_ASSUMED_CONTEXT:,}", output=f"{EVAL_ASSUMED_OUTPUT:,}",
        ))
    if found.measured:
        lines.append("  " + t("{n} 个任务按上一次真的运行（同一个模型）实际花的钱估算", n=found.measured))
    tokens_in, tokens_out = found.tokens
    lines.append("  " + t("大约 {tokens_in} 个输入 token、{tokens_out} 个输出 token", tokens_in=f"{tokens_in:,}", tokens_out=f"{tokens_out:,}"))
    if found.judge_model:
        cost = money(found.judge_cost) if found.judge_cost is not None else t("算不出（没有价格）")
        lines.append("  " + t("评分员（{model}）：{n} 次评分，约 {cost}", model=found.judge_model, n=found.judge_calls, cost=cost))
    if found.priced:
        lines.append(t("大约 {low}，最多 {high}（缓存一点都没命中、每一步都用满假定的用量时）", low=money(found.low), high=money(found.high)))
    else:
        lines.append(t("这个模型没有价格，算不出要花多少钱（可以在网页界面的设置里填上它的价格）。上面的 token 数照样会用掉。"))
    provider = PROVIDERS.get(found.target.provider)
    lines.append(t("这会向 {provider} 的 API 真的发请求，花真的钱（记进花费账本，来源是 eval）。", provider=provider.label if provider else found.target.provider))
    return lines


def _case_lines(row: dict) -> list[str]:
    """跑完一遍，打印一行；没通过的检查在下面一项一行。"""
    name = f"{row['task']} #{row['rep'] + 1}"
    status = row.get("status")
    if status != "ok":
        return [f"{STATUS_MARKS.get(status, '!')} {name}  {status}: {row.get('error', '')}"]
    mark = "✓" if row.get("passed") else "✗"
    cost = money(row.get("cost")) if row.get("cost") is not None else t("没有价格")
    line = f"{mark} {name}  {row.get('checks_passed', 0)}/{row.get('checks', 0)}  {cost}  {seconds(row.get('seconds'))}"
    if row.get("outcome") not in ("", "done", None):
        line += f"  ({row['outcome']})"
    if row.get("kept"):
        line += f"  {row['kept']}"
    lines = [line]
    for grade in row.get("grades") or []:
        if grade["passed"] is False:
            lines.append(f"    ✗ {grade['label']}" + (f" — {grade['detail']}" if grade["detail"] else ""))
    return lines


def _summary_lines(run: results.Run) -> list[str]:
    summary = results.summary_of(run.rows)
    meta = run.meta
    noise = t("，误差约 ±{n} 个百分点", n=f"{summary.noise * 100:.0f}") if summary.noise else ""
    lines = [
        "",
        t("结果：{passed}/{ran} 遍全部检查都通过（{rate}{noise}），检查项通过 {checks}", passed=summary.passed, ran=summary.ran, rate=percent(summary.pass_rate), noise=noise, checks=percent(summary.check_rate)),
        t("花费 {cost}（评分员 {judge}），平均每遍 {seconds}，一共调用了 {requests} 次模型", cost=money(summary.cost), judge=money(summary.judge_cost), seconds=seconds(summary.seconds), requests=summary.requests),
    ]
    if meta.get("mode"):
        lines.append(t("这是演习：花费是按假服务器报的用量算的，实际一分钱没花。"))
    if summary.unpriced:
        lines.append(t("（没有价格、没算进花费的模型：{models}）", models=", ".join(summary.unpriced)))
    if summary.failed:
        lines.append(t("出错或超时 {n} 遍，不算进分数：这是管道的问题（连不上、密钥不对、超时……），不是模型做错了。", n=summary.failed))
    if summary.skipped:
        lines.append(t("跳过 {n} 遍：这个模型做不了，或者这台电脑上准备不了。", n=summary.skipped))
    if meta.get("status") == "stopped":
        lines.append(t("被中断了：已经跑完的存下了，没跑的没有跑。"))
    lines.append(t("结果存在 {path}", path=run.folder))
    lines.append(t("harness eval show {id} 看每一项检查；harness eval compare 和上一次对比。", id=meta["id"]))
    return lines


# ---- list / show ------------------------------------------------------------


def _list(root: Path, out: IO[str]) -> int:
    runs = results.list_runs(root)
    if not runs:
        print(t("还没有评测过。先试试不花钱的演习：harness eval run"), file=out)
        return 0
    rows = [[t("编号"), t("评测方式"), t("模型"), t("通过"), t("花费"), t("状态")]]
    for item in runs:
        summary = results.summary_of(item.rows)
        meta = item.meta
        rows.append([
            meta["id"], t(MODES.get(meta.get("mode", ""), MODES[""])), f"{meta.get('provider')}/{meta.get('model')}",
            f"{summary.passed}/{summary.ran}", money(summary.cost + summary.judge_cost), str(meta.get("status", "")),
        ])
    for line in table(rows):
        print(line, file=out)
    return 0


def _show(run: results.Run, out: IO[str]) -> int:
    meta = run.meta
    print(t("运行 {id}（{mode}）：{model} · 提示词 {prompt}（{sha}）· 代码 {git}", id=meta["id"], mode=t(MODES.get(meta.get("mode", ""), MODES[""])),
            model=f"{meta.get('provider')}/{meta.get('model')}", prompt=meta.get("prompt", ""), sha=meta.get("prompt_sha", ""), git=meta.get("git") or "—"), file=out)
    for name, entry in results.by_task(run.rows).items():
        summary = entry["summary"]
        mark = "✓" if summary.ran and summary.passed == summary.ran else "✗" if summary.ran else "–"
        print(file=out)
        print(f"{mark} {name}  {summary.passed}/{summary.ran}  {money(summary.cost)}  {seconds(summary.seconds)}  {entry['title']}", file=out)
        for check in entry["checks"]:
            sign = "–" if not check["judged"] else "✓" if check["passed"] == check["judged"] else "✗"
            counts = f" ({check['passed']}/{check['judged']})" if check["judged"] > 1 else ""
            detail = f" — {check['detail']}" if sign == "✗" and check["detail"] else ""
            print(f"    {sign} {check['label']}{counts}{detail}", file=out)
        for row in entry["rows"]:
            if row.get("status") != "ok":
                print(f"    {STATUS_MARKS.get(row.get('status'), '!')} #{row['rep'] + 1} {row.get('status')}: {row.get('error', '')}", file=out)
        trace = run.folder / results.TRACES / f"{name}_rep0.json"
        if trace.is_file():
            print("    " + t("记录：{path}", path=trace), file=out)
    for line in _summary_lines(run)[1:]:
        print(line, file=out)
    return 0
