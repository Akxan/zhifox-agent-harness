"""演习：模型换成本地的假服务器，一分钱不花。harness eval run 默认就是演习。

假服务器就是测试用的那两个（tests/fake_api.py 是 Claude 的格式，tests/fake_openai.py 是 GPT、Gemini、DeepSeek 的格式）：
真正的 SDK 发出真正的请求，收到流式的回复，走的是和真的运行完全一样的代码，只是回复是写好的。
同一份假服务器不另写一份：它们在仓库的 tests/ 里，这里按文件路径把它们读进来。
所以演习只能在仓库里用（装成包、没有 tests/ 的地方会明说用不了）；真的运行不需要它们。

写好的回复有两种：
  - 标准答案（默认）：任务文件里的 [[dry_run]]，一次请求取一项。照着它做，每一项检查都该通过。
    通不过，说明检查写错了或者任务说不清楚——花钱跑真的之前就该发现；
  - 什么都不做（--null）：每一轮只回一句"好的。"。它应该几乎拿零分。拿了高分的任务，说明检查太松，
    一个什么都不做的模型也能通过，那这个任务量不出任何东西。

每个任务的每一遍各起一个假服务器（它按顺序发回复，几个任务共用一个就乱了套）。子 Agent 和主助手共用同一个，
所以标准答案按请求发生的先后写：主助手交给子 Agent → 子 Agent 的几步 → 主助手的最后一句。
请评分员（--judge）的那几次请求发给另一个假服务器，回答一律是"通过"。
"""

from __future__ import annotations

import importlib.util
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from ..config import PROJECT_ROOT
from ..i18n import t
from ..providers import PROVIDERS, find
from .spec import Reply, Task

FAKES = PROJECT_ROOT / "tests"
NULL_REPLY = "好的。"
VERDICT = {"pass": True, "reason": "演习：没有真的请模型评分。"}

_loaded: dict[str, ModuleType] = {}
_load_lock = threading.Lock()


class DryRunUnavailable(Exception):
    """找不到假服务器（不是在仓库里运行的）。"""


def _fake(name: str) -> ModuleType:
    """按文件路径读进 tests/ 里的一个假服务器。用一个不会和别的模块撞名的名字，免得和测试自己导入的那一份混在一起。"""
    with _load_lock:
        if name in _loaded:
            return _loaded[name]
        path = FAKES / f"{name}.py"
        if not path.is_file():
            raise DryRunUnavailable(t("演习要用仓库里的 {path}，这里找不到。请在智狐的仓库里运行（git clone 下来的那个文件夹）。", path=path))
        spec = importlib.util.spec_from_file_location(f"_zhifox_eval_{name}", path)
        assert spec is not None and spec.loader is not None  # 是 .py 文件、上面查过它在：总认得出该怎么读
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _loaded[name] = module
        return module


def script(task: Task, null: bool) -> list[Reply]:
    """这个任务的演习要发的回复。"""
    if null:
        return [Reply(say=NULL_REPLY) for _ in task.messages]
    return list(task.script)


def _anthropic_replies(replies: list[Reply]) -> list[dict]:
    fake = _fake("fake_api")
    out, n = [], 0
    for item in replies:
        blocks = [fake.text(item.say)] if item.say else []
        for call in item.tools:
            n += 1
            blocks.append(fake.tool_use(f"toolu_eval_{n}", call.name, call.input))
        out.append(fake.reply(*blocks, stop_reason="tool_use" if item.tools else "end_turn"))
    return out


def _openai_replies(replies: list[Reply]) -> list[dict]:
    fake = _fake("fake_openai")
    out, n = [], 0
    for item in replies:
        calls = []
        for call in item.tools:
            n += 1
            calls.append(fake.call(f"call_eval_{n}", call.name, json.dumps(call.input, ensure_ascii=False)))
        out.append(fake.chat(item.say, calls=calls))
    return out


@dataclass
class Connection:
    """一个任务的一遍要用的连接：给助手的（按提供方给一个 SDK 的客户端）、给评分员的（可能没有），用完关掉。"""

    client_factory: Callable[[Any], Any]
    judge_client: Any = None
    close: Callable[[], None] = lambda: None


class DryDriver:
    """演习用的"连接方式"：每个任务的每一遍起一个假服务器。null 为真时回复一律是"好的。"。"""

    dry = True

    def __init__(self, null: bool = False):
        self.null = null
        _fake("fake_api")  # 先读一次：找不到的话，在一个任务都还没开始的时候就说清楚
        _fake("fake_openai")

    def connect(self, task: Task, provider: str) -> Connection:
        # 两家的 SDK 用到时才导入：要大半秒，而 harness 的每个命令（连 --help）都会经过 evals/cli.py 带进这个文件
        import anthropic
        import openai

        replies = script(task, self.null)
        servers: list[Any] = []
        client: anthropic.Anthropic | openai.OpenAI
        if (find(provider) or PROVIDERS["anthropic"]).kind == "anthropic":  # 自定义接口也是 OpenAI 格式（见 harness/endpoints/）
            server = _fake("fake_api").FakeAPI(_anthropic_replies(replies)).start()
            client = anthropic.Anthropic(api_key="dry-run", base_url=server.base_url, max_retries=0)
        else:
            server = _fake("fake_openai").FakeOpenAI(_openai_replies(replies)).start()
            client = openai.OpenAI(api_key="dry-run", base_url=server.base_url, max_retries=0)
        servers.append(server)
        judge_client = None
        if task.judged:
            fake = _fake("fake_api")
            verdicts = [fake.reply(fake.text(json.dumps(VERDICT, ensure_ascii=False))) for _ in range(task.judged)]
            judge = fake.FakeAPI(verdicts).start()
            servers.append(judge)
            judge_client = anthropic.Anthropic(api_key="dry-run", base_url=judge.base_url, max_retries=0)

        def close() -> None:
            for server in servers:
                server.stop()

        return Connection(client_factory=lambda _provider: client, judge_client=judge_client, close=close)
