"""钩子在一次运行里怎么跑：找出这个时刻要跑哪几个、交给它们什么、照着它们的回答办。

两层：
  Hooks    整个程序一份（App.hooks）：配置（data/hooks.json）、每个钩子上一次跑得怎么样（页面上那一行写的）、试运行。
           你的对话、每一次定时任务、每个聊天同时都可能在跑钩子，共用的只有这两样，各有锁（配置的在 HookConfig 里）。
  HookRun  每次运行一份（和 Shell 一样，见 App._side_run）：agent.py 在几个记录点旁边各调用它一次。
           它有自己的 Launcher，所以"停止"只结束这次运行自己正在跑的那个钩子，碰不到别人的。
           子 Agent 用 child() 另拿一份：只跑工具前后的两个事件（它的"一轮"不是你发的消息，也不是你等的那个回答）。

agent.py 调用的几个方法，每个都在"这个时刻有没有要跑的钩子"上先看一眼，没有就什么都不做、马上返回，
所以没配钩子的人一点额外的开销都没有（看一眼 = 一次 stat，见 HookConfig.reload）：

  prompt(agent, text, attachments)   UserPromptSubmit：你的消息进历史之前。返回 Prompted（拦下了没有、附给模型的话）
  before_tool(agent, call, tool)     PreToolUse：确认过了、马上要执行的时候。返回 Checked（拦下的结果、要再问你的话）
  after_tool(agent, call, tool, text, is_error, seconds)   PostToolUse：工具做完了，返回接上补充之后的结果
  stopped(agent, ended)              Stop：这一轮答完了，只是通知
  watch(ui)                          包着界面交给适配层：记下模型这一次写的字（Stop 的 last_assistant_message）

每跑一个钩子，在运行记录上画一段（agent.trace.hook_began / hook_ended，见 trace/recorder.py），时刻和结果由记录器记，
这里不另算。钩子给你的话（systemMessage、出错的提醒）用 ui.info 说；子 Agent 的用 ui.notice，画进它那张卡片，
不混进它交回给主助手的话里。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from ..config import (
    HOOK_NOTE_CHARS,
    HOOK_TEST_OUTPUT_CHARS,
    HOOK_TIMEOUT,
    TRACE_SUMMARY_CHARS,
)
from ..display import describe_call, short
from ..i18n import t
from ..safe import to_json
from .config import (
    TOOL_EVENTS,
    HookConfig,
    matcher_items,
    model_key_references,
    runs_in,
)
from .protocol import NO_REASON, Outcome, cap, interpret
from .runner import Launcher, Ran, SandboxMissing, argv_of, hook_env, jailed

FINISHED = ("done", "truncated", "refused", "steps")  # Stop 只在这几种结束时跑：你点了停止、出错了、被钩子拦下的不跑
MAX_LAST_MESSAGE = 20_000  # Stop 的 last_assistant_message 最多这么多字


@dataclass
class Prompted:
    blocked: bool = False
    context: list[str] = field(default_factory=list)  # 附在消息里的说明，每个钩子一段（已经包好了标记）


@dataclass
class Checked:
    blocked: str = ""  # 拦下了：这是交给模型的结果（中文原文）
    shown: str = ""  # 拦下了：这是给你看的那一句（t() 译好的）
    ask: str = ""  # 要再问你一次：确认卡片上的话


class Hooks:
    def __init__(self, config: HookConfig, workspace: Path):
        self.config = config
        self.workspace = workspace
        self._lock = threading.Lock()
        self._last: dict[str, dict] = {}  # 名字 → 上一次跑得怎么样（只在内存里：重启以后从头记）

    def matching(self, event: str, source: str, call: Any = None, tool: Any = None) -> list[tuple[str, dict]]:
        """这个时刻要跑的钩子，按文件里的顺序。文件读不出来时一个都不跑（页面和命令行会说明原因）。"""
        self.config.reload()
        if self.config.problem:
            return []
        found = []
        for name, (entry, _) in self.config.hooks().items():
            if entry is None or not entry["enabled"] or entry["event"] != event or source not in runs_in(entry):
                continue
            if event in TOOL_EVENTS and not matches(entry.get("matcher", "*"), call, tool):
                continue
            found.append((name, entry))
        return found

    def execute(self, launcher: Launcher, name: str, entry: dict, fields: dict, test: bool = False, sandbox: Any = None) -> tuple[Ran, Outcome]:
        """跑一个钩子：JSON 从标准输入交给它，照着退出码和输出得出结论，记下"上一次"。
        sandbox 是这次运行的命令沙箱（和它的 run_command 同一个，见 runner.jailed）；没有就是 None。"""
        event = entry["event"]
        timeout = entry.get("timeout") or HOOK_TIMEOUT
        secrets = model_key_references([entry["command"], *entry.get("args", [])])
        if secrets:  # 保存时已经查过；你手动改了文件的话，运行之前再查一遍
            ran = Ran(None, "", "", 0.0, "start", t("引用了 {secret}：那是智狐自己用的密钥，不能交给钩子。", secret=secrets[0]))
        else:
            payload = to_json({"hook_event_name": event, "hook_name": name, **fields}).encode("utf-8")
            try:
                argv, env, level = jailed(sandbox, argv_of(entry["command"], entry.get("args", []), self.workspace), hook_env(event, name, self.workspace))
            except SandboxMissing as exc:
                ran = Ran(None, "", "", 0.0, "start", exc.args[0])
            else:
                ran = launcher.run(argv, payload, self.workspace, env, timeout)
                ran.sandbox = level
        outcome = interpret(event, ran, entry.get("onFailure", "continue"), timeout)
        self._remember(name, event, ran, outcome, test)
        return ran, outcome

    def _remember(self, name: str, event: str, ran: Ran, outcome: Outcome, test: bool) -> None:
        note = outcome.reason or str(outcome.problem) or outcome.message
        last = {"at": round(time.time(), 3), "ms": max(0, round(ran.seconds * 1000)), "event": event, "result": outcome.result, "exit": ran.exit}
        if note:
            last["note"] = short(note, TRACE_SUMMARY_CHARS)
        if test:
            last["test"] = True
        with self._lock:
            self._last[name] = last

    def last(self) -> dict[str, dict]:
        with self._lock:
            return {name: dict(value) for name, value in self._last.items()}

    def forget(self, name: str) -> None:
        with self._lock:
            self._last.pop(name, None)

    def rename(self, old: str, new: str) -> None:
        with self._lock:
            if old in self._last:
                self._last[new] = self._last.pop(old)

    # ---- 试运行 --------------------------------------------------------------

    def sample(self, entry: dict) -> dict:
        """试运行时交给钩子的一份示例（不含 hook_event_name、hook_name：那两项由 execute 填）。页面上可以先改再跑。"""
        fields: dict[str, Any] = {
            "session_id": "test",
            "cwd": str(self.workspace),
            "permission_mode": "ask",
            "source": "chat",
            "provider": "anthropic",
            "model": "claude-opus-5-5",
            "test": True,
        }
        event = entry["event"]
        if event in TOOL_EVENTS:
            fields.update(_sample_tool(entry.get("matcher", "*")))
        if event == "PostToolUse":
            fields.update(tool_response=_SAMPLE_RESPONSE, is_error=False, duration_ms=12)
        elif event == "UserPromptSubmit":
            fields.update(prompt="帮我看看工作目录里有哪些文件。", attachments=[])
        elif event == "Stop":
            fields.update(last_assistant_message="工作目录里有 3 个文件：README.md、notes.txt 和 data.csv。", ended="done", stop_hook_active=False)
        return fields

    def test(self, name: str, entry: dict, payload: dict | None = None, sandbox: Any = None) -> dict:
        """试运行一个钩子（页面上的"试运行"）：用示例（或者你改过的那一份）跑一次，交回经过和它会让智狐做什么。
        sandbox 是你的对话的那个沙箱：试运行和它在对话里跑的时候一样（沙箱开着就关在里面）。"""
        fields = {**self.sample(entry), **(payload or {})}
        fields.pop("hook_event_name", None)
        fields.pop("hook_name", None)
        ran, outcome = self.execute(Launcher(), name, entry, fields, test=True, sandbox=sandbox)
        return {
            "exit": ran.exit,
            "ms": max(0, round(ran.seconds * 1000)),
            "result": outcome.result,
            "stdout": cap(ran.stdout, HOOK_TEST_OUTPUT_CHARS),
            "stderr": cap(ran.stderr, HOOK_TEST_OUTPUT_CHARS),
            "verdict": verdict(entry["event"], name, outcome),
            "sandbox": ran.sandbox,
            "message": outcome.message,
            "ignored": [t(text) for text in outcome.ignored],
        }


# 试运行的示例：matcher 第一项是哪种功能，就给一个那种工具的调用（看不出来的给一条命令）
_SAMPLE_TOOLS = {
    "shell": ("run_command", {"command": "ls -la"}),
    "files": ("str_replace_based_edit_tool", {"command": "create", "path": "notes.txt", "file_text": "今天要做的事\n"}),
    "todo": ("todo_add", {"title": "买牛奶"}),
    "memory": ("memory", {"command": "view", "path": "/memories"}),
    "web": ("web_fetch", {"url": "https://example.com"}),
    "browser": ("browser.navigate", {"url": "https://example.com"}),
    "mcp": ("mcp__github__create_issue", {"title": "示例"}),
}
_SAMPLE_RESPONSE = "total 24\n-rw-r--r--  1 me  staff  120 README.md\n-rw-r--r--  1 me  staff   48 notes.txt"


def _sample_tool(matcher: str) -> dict:
    items = matcher_items(matcher)
    first = items[0] if items else "shell"
    if first in _SAMPLE_TOOLS:
        name, data = _SAMPLE_TOOLS[first]
        group = first
    elif "*" not in first and first not in GROUP_NAMES():
        name, data, group = first, {}, "other"
    else:
        (name, data), group = _SAMPLE_TOOLS["shell"], "shell"
    return {"tool_name": name, "tool_group": group, "tool_input": data, "tool_use_id": "toolu_test"}


def GROUP_NAMES() -> set[str]:
    from ..capabilities import KINDS, MCP_KIND

    return {kind.id for kind in KINDS} | {MCP_KIND.id, "other"}


def matches(matcher: str, call: Any, tool: Any) -> bool:
    """matcher 对不对得上这次调用：空的、* 是全部；否则其中一项是这个工具所属的功能（shell、files……），
    或者和它的名字一样（可以带 * 通配）。名字认三个：给人看的（browser.left_click）、模型调用时用的、工具自己的。"""
    items = matcher_items(matcher)
    if not items:
        return True
    from ..capabilities import label_of  # 用到时才导入，原因同上

    group = label_of(tool).group
    names = {name for name in (getattr(call, "shown", ""), getattr(call, "name", ""), getattr(tool, "name", "")) if name}
    return any(item == group or any(fnmatchcase(name, item) for name in names) for item in items)


def verdict(event: str, name: str, outcome: Outcome) -> str:
    """一个钩子的结论，换成"智狐会怎么办"的一句话（试运行时给你看）。"""
    if outcome.result == "stopped":
        return t("被停下了。")
    if outcome.result == "blocked":
        reason = _shown_reason(outcome)
        if event == "PreToolUse":
            return t("会拦下这次调用，不执行。模型看到的原因：{reason}", reason=reason)
        return t("会拦下这条消息，不发给模型。你会看到：{reason}", reason=reason)
    if outcome.result in ("error", "timeout"):
        return t("钩子出错了（{problem}）。不会拦下什么，照常继续。", problem=outcome.problem)
    if outcome.result == "ask":
        return t("会再问你一次要不要做（哪一档都问）：{reason}", reason=outcome.reason or t(NO_REASON))
    if event == "PreToolUse":
        return t("放行，照常执行。")
    if event == "PostToolUse":
        return t("会在工具结果后面接上：{note}", note=outcome.context) if outcome.context else t("没有补充，结果原样交给模型。")
    if event == "UserPromptSubmit":
        return t("会把这段话附在消息里交给模型：{note}", note=outcome.context) if outcome.context else t("放行，消息原样发给模型。")
    return t("只是通知，这一轮照常结束。")


class _Collect:
    """交给适配层的界面：和原来的一模一样，只是记下这一次请求里模型写的字（一轮答完时交给 Stop 钩子）。"""

    def __init__(self, ui: Any, into: list[str]):
        self._ui = ui
        self._into = into

    def text(self, text: str) -> None:
        if sum(map(len, self._into)) < MAX_LAST_MESSAGE:
            self._into.append(text)
        self._ui.text(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ui, name)


class HookRun:
    """一次运行用的钩子（见文件开头）。hooks 是 None 的什么都不做（测试里自己组装的 Agent，默认就是这样）。

    source：chat 你的对话（网页和终端）/ task 定时任务 / channel 聊天通道。钩子的 runsIn 按它挑，也写进交给钩子的 JSON。
    session：这次运行的会话编号（交给钩子的 session_id），开始时可能还不知道，所以是一个函数。
    agent：子 Agent 的名字（交给钩子的 agent_type）；主助手是空的。
    sandbox：这次运行的命令沙箱（它的 run_command 用的那一个，见 sandbox/）；钩子和命令关在同一个里。没有就返回 None。
    """

    def __init__(
        self, hooks: Hooks | None, source: str = "chat", session: Callable[[], str] = lambda: "", agent: str = "", sandbox: Callable[[], Any] = lambda: None
    ):
        self.hooks = hooks
        self.source = source
        self.session = session
        self.agent = agent
        self.sandbox = sandbox
        self.launcher = Launcher()
        self._said: list[str] = []

    def child(self, agent: str) -> HookRun:
        """给子 Agent 的一份：同一个来源、同一段会话、同一个沙箱（子 Agent 只会更少），自己的 Launcher（停下它只结束它自己的钩子）。"""
        return HookRun(self.hooks, self.source, self.session, agent, self.sandbox)

    def stop(self) -> None:
        self.launcher.stop()

    def watch(self, ui: Any) -> Any:
        if self.hooks is None:
            return ui
        self._said = []  # 新的一次请求：只留最后一次的（一轮最后的那次请求写的就是回答）
        return _Collect(ui, self._said)

    # ---- 四个时刻 ------------------------------------------------------------

    def prompt(self, agent: Any, text: str, attachments: Sequence[Any]) -> Prompted:
        if self.hooks is None or self.agent:
            return Prompted()
        found = self.hooks.matching("UserPromptSubmit", self.source)
        if not found:
            return Prompted()
        files = [{"name": item.name, "kind": item.kind, "media_type": item.media_type, "size": item.size} for item in attachments]
        fields = {**self._common(agent), "prompt": text, "attachments": files}
        prompted = Prompted()
        for name, entry in found:
            outcome = self._one(agent, name, entry, fields)
            if outcome.result == "blocked":
                self._say(agent, t("这条消息被你的钩子“{name}”拦下了，没有发给模型：{reason}", name=name, reason=_shown_reason(outcome)))
                return Prompted(blocked=True)
            if outcome.context:
                prompted.context.append(_wrap("hook_context", name, outcome.context))
        return prompted

    def before_tool(self, agent: Any, call: Any, tool: Any) -> Checked:
        if self.hooks is None:
            return Checked()
        found = self.hooks.matching("PreToolUse", self.source, call, tool)
        if not found:
            return Checked()
        fields = {**self._common(agent), **_tool_fields(call, tool)}
        asks: list[tuple[str, str]] = []
        for name, entry in found:
            outcome = self._one(agent, name, entry, fields)
            if outcome.result == "blocked":
                reason = outcome.reason or NO_REASON
                return Checked(
                    blocked=f"这次调用被用户的钩子“{name}”拦下了，没有执行。原因：{reason}",
                    shown=t("被你的钩子“{name}”拦下了，没有执行：{reason}", name=name, reason=_shown_reason(outcome)),
                )
            if outcome.result == "ask":
                asks.append((name, outcome.reason))
        if not asks:
            return Checked()
        name, reason = asks[0]
        return Checked(ask=t("你的钩子“{name}”要你先确认这一步：{reason}\n\n{call}", name=name, reason=reason or t(NO_REASON), call=describe_call(call.shown, call.input)))

    def after_tool(self, agent: Any, call: Any, tool: Any, text: str, is_error: bool, seconds: float) -> str:
        if self.hooks is None:
            return text
        found = self.hooks.matching("PostToolUse", self.source, call, tool)
        if not found:
            return text
        fields = {**self._common(agent), **_tool_fields(call, tool), "tool_response": text, "is_error": is_error, "duration_ms": max(0, round(seconds * 1000))}
        notes: list[str] = []
        room = HOOK_NOTE_CHARS  # 几个钩子加起来也不超过这么多字
        for name, entry in found:
            outcome = self._one(agent, name, entry, fields)
            if outcome.context and room > 0:
                note = cap(outcome.context, room)
                room -= len(note)
                notes.append(_wrap("hook_note", name, note))
        return "\n\n".join([text, *notes]) if notes else text

    def stopped(self, agent: Any, ended: str) -> None:
        if self.hooks is None or self.agent or ended not in FINISHED:
            return
        found = self.hooks.matching("Stop", self.source)
        if not found:
            return
        said = "".join(self._said)[:MAX_LAST_MESSAGE]
        fields = {**self._common(agent), "last_assistant_message": said, "ended": ended, "stop_hook_active": False}
        try:
            for name, entry in found:
                self._one(agent, name, entry, fields)
        except KeyboardInterrupt:
            return  # 这一轮已经答完了：停下的只是钩子

    # ---- 跑一个 --------------------------------------------------------------

    def _common(self, agent: Any) -> dict:
        """每个事件都有的几项（名字和 Claude Code 的一样，后面几项是智狐自己的）。"""
        fields = {
            "session_id": self.session(),
            "cwd": str(self.hooks.workspace) if self.hooks else "",
            "permission_mode": getattr(agent.permissions, "mode", "ask"),
            "source": self.source,
            "provider": agent.backend.provider,
            "model": agent.backend.model,
        }
        if self.agent:
            fields["agent_type"] = self.agent
        return fields

    def _one(self, agent: Any, name: str, entry: dict, fields: dict) -> Outcome:
        assert self.hooks is not None
        agent.trace.hook_began(entry["event"], name)
        try:
            ran, outcome = self.hooks.execute(self.launcher, name, entry, fields, sandbox=self.sandbox())
        except KeyboardInterrupt:  # 终端里按了 Ctrl-C：钩子已经结束掉了，这一轮照常停下
            agent.trace.hook_ended("stopped")
            raise
        note = outcome.reason or str(outcome.problem) or outcome.message
        agent.trace.hook_ended(outcome.result, ran.exit, note, len(outcome.context), ran.sandbox)
        if outcome.result == "stopped":
            raise KeyboardInterrupt  # 网页上点了停止（钩子已经被结束掉）：和别处一样，这一轮停下
        if outcome.message:
            self._say(agent, t("钩子“{name}”：{message}", name=name, message=outcome.message))
        if outcome.problem and outcome.result != "blocked":
            self._say(agent, t("钩子“{name}”出错了（{problem}），没有拦下什么，照常继续。", name=name, problem=outcome.problem))
        return outcome

    def _say(self, agent: Any, text: str) -> None:
        if self.agent:
            agent.ui.notice("info", text)  # 子 Agent：画进它的卡片，不混进它交回给主助手的话里
        else:
            agent.ui.info(text)


def _tool_fields(call: Any, tool: Any) -> dict:
    from ..capabilities import label_of  # 用到时才导入，原因见 GROUP_NAMES

    label = label_of(tool)
    fields = {"tool_name": call.shown, "tool_group": label.group, "tool_input": call.input, "tool_use_id": call.id}
    if label.server:
        fields["mcp_server"] = label.server
    return fields


def _wrap(tag: str, name: str, text: str) -> str:
    """钩子交给模型的话包一层标记，写明是哪个钩子说的：模型分得清哪些是工具的结果、哪些是用户的钩子补的。"""
    return f'<{tag} name="{name}">\n{text}\n</{tag}>'


def _shown_reason(outcome: Outcome) -> str:
    """拦下的原因给你看的样子：钩子自己写的原样；没写的、钩子坏了按设置当成拦下的，用界面的语言说。"""
    if outcome.problem:
        return t("钩子没能正常运行（{problem}），按设置当成拦下。", problem=outcome.problem)
    return outcome.reason if outcome.reason and outcome.reason != NO_REASON else t(NO_REASON)
