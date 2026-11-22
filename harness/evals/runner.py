"""跑评测：一个任务的一遍（run_case），和一整组（run_all）。

一个任务的一遍：
  1. 准备一个全新的临时文件夹（seed.py）：工作目录、数据目录、设置；
  2. 用 build_app 组装一个真正的智狐：同一个循环、同一套工具和权限、同一份系统提示词，
     界面换成记录员（recorder.py），确认的那一档用任务写的 approval；
  3. 把任务的每一句话依次发给它，和你在终端里打字一样（agent.run_turn）；
  4. 做完以后按任务的检查打分（graders.py），算出用了多少 token、花了多少钱、用了多久；
  5. 删掉临时文件夹（--keep 时留着，方便你进去看它到底做了什么）。

结果分成几种（row 的 status），只有 ok 的算进分数：
  ok       跑完了（模型拒绝、回答被截断、步数用完都算跑完了：那是模型的表现，照样打分）
  skipped  没有跑：这个模型做不了（看不了图），或者这台电脑上准备不了（没有 git）
  timeout  超过了 EVAL_TASK_SECONDS
  error    出错了：连不上、密钥不对、演习的标准答案不够用……这是"管道"的问题，不是模型做错了，
           混进分数里会让模型背锅，所以单独数
  stopped  你按了 Ctrl-C

花钱的事（真的运行）：每一次模型调用照常记进你的花费账本（ledger.py），来源写成 eval，
"本月花了多少"和预算提醒里都有它。演习花的不是真钱，不记。
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .. import i18n
from ..app import build_app
from ..attachments import AttachmentError
from ..attachments import from_bytes as attachment_from_bytes
from ..config import EVAL_MAX_STEPS, EVAL_REQUEST_SECONDS, EVAL_TASK_SECONDS, Config
from ..i18n import t
from ..ledger import Ledger
from ..pricing import estimate
from ..prompt import build_system_prompt, save_template
from ..providers import PROVIDERS, ProviderError, caps, find, make_client
from ..providers.base import Usage
from ..safe import to_json, write_file
from . import graders, seed
from .dryrun import Connection
from .graders import Outcome
from .judge import Judge
from .recorder import Recorder
from .spec import Task

SOURCE = "eval"  # 花费账本里的来源
MAX_TOKENS = 64_000  # 和平时一样（见 config.load_config）
# 你的设置里这几项不抄进评测用的设置：评测里浏览器、操作整台电脑都关着（任务也用不到，开着反而要多发几千个 token 的说明书），
# 预算的提醒是给你本人的
NOT_COPIED = ("browser", "computer", "budget", "budget_alert")


@dataclass(frozen=True)
class Target:
    """评测哪个模型。"""

    provider: str
    model: str

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass
class Options:
    effort: str  # Claude 想多深（和平时的 HARNESS_EFFORT 一样）
    template: str | None = None  # 用哪一版提示词（模板的原文）；None 是内置的
    settings: dict = field(default_factory=dict)  # 从你的设置里抄过来的（价格、模型列表……，见 NOT_COPIED）
    judge_model: str | None = None  # 评分员用哪个模型；None 表示不请评分员
    timeout: float = EVAL_TASK_SECONDS
    keep: bool = False  # 跑完留着临时文件夹
    ledger: Ledger | None = None  # 真的运行：花的钱记进这本账（你的 data/ledger.jsonl）
    # 你的 data/endpoints.json 原样的内容（自定义模型接口，见 harness/endpoints/）：--model custom-ollama/qwen3:8b 这样评一个
    # 本地模型时，每一遍的数据目录里都要有它，build_app 才认得这个接口。空的表示你没加过
    endpoints: bytes = b""


@dataclass
class CaseResult:
    row: dict  # 存进 results.jsonl 的一行（见 results.py）
    trace: list[dict] = field(default_factory=list)  # 这一遍发生的每一件事（见 recorder.py），第一项是系统提示词
    history: list = field(default_factory=list)  # 发给模型的原始对话历史（各家的格式）


class Driver(Protocol):
    """怎么连模型：RealDriver（各家真正的 API）或者 DryDriver（演习用的假服务器，见 dryrun.py）。"""

    dry: bool

    def connect(self, task: Task, provider: str) -> Connection: ...


class RealDriver:
    """真的运行：连各家真正的 API（密钥从环境变量里读，和平时一样，见 providers.make_client）。"""

    dry = False

    def connect(self, task: Task, provider: str) -> Connection:
        def factory(chosen: Any) -> Any:
            # 一次请求最多等 EVAL_REQUEST_SECONDS：连接挂住不动时，靠它让这个任务脱身。
            # 交给 make_client 去设（不在外面 with_options）：自定义接口的客户端那样一改，会照着环境变量重建，带上别家的设置
            return make_client(chosen, timeout=EVAL_REQUEST_SECONDS)

        judge = factory(PROVIDERS["anthropic"]) if task.judged else None
        return Connection(client_factory=factory, judge_client=judge)


class _Spend:
    """这一遍的每一次模型调用都经过这里：主助手的 on_call 接到它上面，子 Agent 记账用的也是它（Delegator 只用 recorder 这一个方法，
    和 Ledger.recorder 同名同参数）。数一数一共调用了几次；真的运行时记进你的花费账本，来源一律写成 eval。"""

    def __init__(self, ledger: Ledger | None):
        self.ledger = ledger
        self.calls = 0
        self._lock = threading.Lock()

    def recorder(self, prices: Callable[[], dict], source: str = "chat") -> Callable[[str, str, Usage, datetime], None]:
        def record(provider: str, model: str, usage: Usage, moment: datetime) -> None:
            with self._lock:
                self.calls += usage.requests or 1
            if self.ledger is not None:
                self.ledger.record(provider, model, usage, moment, prices(), source=SOURCE)

        return record


def unmet(task: Task, provider_id: str, model: str = "") -> str:
    """这个模型做不了这个任务的话，说明为什么；做得了返回空的。自定义接口按这个模型自己的能力算（见 providers.caps）。"""
    provider = find(provider_id)
    if provider is None:
        return t("不认识这个模型提供方：{provider}", provider=provider_id)
    accepts = caps(provider, model)
    for need in task.requires:
        if need == "vision" and not accepts.vision:
            return t("{provider} 看不了图片", provider=provider.label)
        if need == "pdf" and not accepts.pdf:
            return t("{provider} 读不了 PDF", provider=provider.label)
        if need == "web" and not provider.web:
            return t("{provider} 没有联网搜索", provider=provider.label)
    return ""


def _usage_row(usage: Usage, requests: int) -> dict:
    return {
        "requests": requests,
        "input": usage.input_tokens,
        "cache_read": usage.cache_read_tokens,
        "cache_write": usage.cache_write_tokens,
        "output": usage.output_tokens,
        "searches": usage.searches,
    }


def _write_settings(config: Config, target: Target, options: Options) -> None:
    """评测用的设置：抄你的（除了 NOT_COPIED 那几项），模型换成要评的那个，界面语言用现在的
    （build_app 会按设置文件换语言，那是整个程序共用的一个值，不能被评测换掉）。"""
    data = {key: value for key, value in options.settings.items() if key not in NOT_COPIED}
    data.update(provider=target.provider, model=target.model, language=i18n.current)
    write_file(config.settings_file, to_json(data, readable=True, indent=2))
    # 自定义模型接口（地址、可选的密钥、每个模型能做什么）也原样抄过去（write_file：权限 600，这一遍结束和临时文件夹一起删掉）。
    # 它和 .env 一样在文件工具的 protected 名单里，被评的模型读不到里面的密钥
    if options.endpoints:
        write_file(config.endpoints_file, options.endpoints)


def run_case(
    task: Task, rep: int, target: Target, options: Options, driver: Driver, watch: Callable[[Recorder], None] | None = None
) -> CaseResult:
    """跑一个任务的一遍。不会抛出异常：出了什么事都写进返回的那一行里。"""
    row: dict[str, Any] = {
        "task": task.id,
        "rep": rep,
        "title": task.title,
        "tags": list(task.tags),
        "provider": target.provider,
        "model": target.model,
        "status": "ok",
        "error": "",
    }
    problem = unmet(task, target.provider, target.model)
    if problem:
        return CaseResult({**row, "status": "skipped", "error": problem})
    root = Path(tempfile.mkdtemp(prefix="zhifox-eval-"))
    connection: Connection | None = None
    app = None
    recorder = Recorder(task.answer)
    try:
        try:
            prepared = seed.prepare(task, root)
        except seed.SetupError as exc:
            return _ended(row, "skipped", i18n.message_of(exc))
        config = Config(
            effort=options.effort,
            max_tokens=MAX_TOKENS,
            max_steps=EVAL_MAX_STEPS,
            workspace=prepared.workspace,
            data_dir=prepared.data_dir,
            env_file=prepared.workspace / ".env",
            web=task.web,
            default_provider=target.provider,
            default_model=target.model,
        )
        _write_settings(config, target, options)
        if options.template is not None:
            save_template(config, options.template)
        try:
            attachments = [attachment_from_bytes(item.path, item.content) for item in task.attachments]
        except AttachmentError as exc:
            return _ended(row, "error", str(exc))
        connection = driver.connect(task, target.provider)
        app = build_app(config, recorder, client_factory=connection.client_factory)
        problem = app.attachment_problem(attachments)
        if problem:
            return _ended(row, "skipped", problem)
        app.agent.permissions.mode = task.approval
        spend = _Spend(options.ledger)
        app.agent.on_call = spend.recorder(app.prices)  # 价格表里也有自定义接口的（见 App.prices）
        app.delegator.ledger = spend  # 子 Agent 的调用也经过它（见 _Spend）
        recorder.on_stop(app.shell.kill_running)  # 喊停的时候，正在运行的命令一起结束
        recorder.on_stop(app.mcp.cancel_running)
        if watch is not None:
            watch(recorder)
        row.update(_run_turns(task, app, recorder, attachments, options.timeout))
        outcome = Outcome(
            workspace=prepared.workspace,
            data_dir=prepared.data_dir,
            seeded=prepared.seeded,
            secret=prepared.secret,
            events=recorder.events,
            turns=recorder.turns,
            requests=app.agent.usage.requests,
            messages=list(task.messages),
        )
        judge = None
        if row["status"] == "ok" and options.judge_model and connection.judge_client is not None:
            judge = Judge(connection.judge_client, options.judge_model, app.prices())
        grades = graders.grade(task.checks, outcome, judge) if row["status"] == "ok" else []
        row.update(_score(grades))
        cost, unpriced = estimate(app.agent.usage_by_model, app.prices())
        row["usage"] = _usage_row(app.agent.usage, spend.calls)
        row["cost"] = None if unpriced else round(cost, 6)
        row["unpriced"] = unpriced
        row["tool_calls"] = len(outcome.calls)
        row["approvals"] = outcome.confirmations
        row["auto_allowed"] = sum(1 for event in recorder.events if event.get("role") == "auto_allowed")
        row["outcome"] = app.agent.outcome
        if judge is not None and judge.usage.requests:
            judge_cost, priced = judge.cost()
            row["judge"] = {"model": judge.model, "usage": _usage_row(judge.usage, judge.usage.requests), "cost": round(judge_cost, 6) if priced else None}
            if options.ledger is not None:
                options.ledger.record("anthropic", judge.model, judge.usage, datetime.now(), app.prices(), source=SOURCE)
        system = build_system_prompt(config, web=config.web and app.provider.web)
        return CaseResult(row, [{"role": "system", "content": system}, *recorder.events], app.agent.backend.export())
    except Exception as exc:  # noqa: BLE001 —— 评测框架自己的毛病也只算这一遍出错，别的任务照常跑
        return _ended(row, "error", f"{type(exc).__name__}: {exc}", recorder.events)
    finally:
        if app is not None:
            app.close()
        if connection is not None:
            connection.close()
        if options.keep:
            row["kept"] = str(root)
        else:
            shutil.rmtree(root, ignore_errors=True)


def _ended(row: dict, status: str, error: str, trace: list | None = None) -> CaseResult:
    """没有跑完就结束了的一遍（跳过、出错）。"""
    row.update(status=status, error=str(error))
    return CaseResult(row, list(trace or []))


def _run_turns(task: Task, app: Any, recorder: Recorder, attachments: list, timeout: float) -> dict:
    """把任务的话一句一句发给它，直到说完或者出事。返回 status、error、seconds。"""
    timer = threading.Timer(timeout, recorder.expire)
    timer.daemon = True
    status, error = "ok", ""
    started = time.monotonic()
    timer.start()
    try:
        for n, message in enumerate(task.messages):
            sent = attachments if n == 0 else []
            recorder.begin_turn(message, sent)
            app.agent.run_turn(message, sent)
            recorder.end_turn()
    except KeyboardInterrupt:
        status = "timeout" if recorder.timed_out else "stopped"
        error = t("超过了 {n} 秒", n=int(timeout)) if recorder.timed_out else t("被你中断了")
    except ProviderError as exc:
        status, error = "error", i18n.message_of(exc)
    except Exception as exc:  # noqa: BLE001 —— 循环里意料之外的错：这一遍算出错，写明原因
        status, error = "error", f"{type(exc).__name__}: {exc}"
    finally:
        timer.cancel()
        recorder.end_turn()
    return {"status": status, "error": str(error), "seconds": round(time.monotonic() - started, 2)}


def _score(grades: list[dict]) -> dict:
    """一遍的分数：通过的检查占判断了的检查的多少（没判断的不算），全部通过才算这个任务通过。"""
    judged = [grade for grade in grades if grade["passed"] is not None]
    passed = sum(1 for grade in judged if grade["passed"])
    return {
        "grades": grades,
        "checks": len(judged),
        "checks_passed": passed,
        "score": round(passed / len(judged), 4) if judged else None,
        "passed": bool(judged) and passed == len(judged),
    }


def run_all(
    tasks: list[Task],
    repeat: int,
    target: Target,
    options: Options,
    driver: Driver,
    record: Callable[[CaseResult], None],
    parallel: int,
) -> str:
    """跑一整组：每个任务跑 repeat 遍，同时最多 parallel 个。每跑完一遍就交给 record（马上存盘、打印一行），
    中途出了事，已经跑完的不会丢。返回 done，或者 stopped（你按了 Ctrl-C：正在跑的几个喊停、收尾存盘，还没开始的不跑了）。"""
    jobs = [(task, rep) for task in tasks for rep in range(repeat)]
    running: list[Recorder] = []
    lock = threading.Lock()

    def watch(recorder: Recorder) -> None:
        with lock:
            running.append(recorder)

    pool = ThreadPoolExecutor(max_workers=max(1, parallel), thread_name_prefix="eval")
    futures = [pool.submit(run_case, task, rep, target, options, driver, watch) for task, rep in jobs]
    status = "done"
    recorded: set = set()
    try:
        for future in as_completed(futures):
            recorded.add(future)
            record(future.result())
    except KeyboardInterrupt:
        status = "stopped"
        for future in futures:
            future.cancel()
        with lock:
            for recorder in running:
                recorder.request_stop()
        for future in futures:
            if future not in recorded and not future.cancelled():
                record(future.result())
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return status
