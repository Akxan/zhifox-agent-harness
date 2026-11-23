"""同一步里几个只读的工具调用一起跑（harness/parallel.py，和 agent.py 的 _run_tools）。

真实的 SDK 连本地的假 API（fake_api.py、fake_openai.py），不花钱。要验证的是：

  一起跑      几次读文件、列待办真的是同时跑的：每一次都在一个 Barrier 上等着别的几个。一个一个跑的话谁也等不到
              （等到超时，结果是"工具内部出错"），所以这几个测试不靠"快了多少"来判断，机器再慢也不会误判
  顺序        交给模型的结果、界面上的卡片都按调用的顺序，哪怕后面的先做完；界面收到的仍是"调用、结果、调用、结果"
  分段        要问你的（读工作目录之外的文件）、这一档放行的、不是只读的（加待办）单独、按顺序、在跑这一轮的线程里跑，
              前后两段各自一起跑；问你的时候没有别的调用在跑
  停下        一批跑到一半停下：每个调用照样有结果（历史仍然合法），还没开始的不再开始
  出错        一个调用里的工具出了错，别的照常交回结果
  每一次都有  钩子（PostToolUse）、遮密钥一次一次地做；有 PreToolUse 钩子要看的调用不一起跑
  ContextVar  线程里看得到这一轮的 ContextVar（撤回文件的记账，见 undo/journal.py）
  运行记录    同一批的几段横条叠在一起、标着同一个 batch；合计里工具的时间按"一共占了多久"算，加起来仍是这一轮的总时长；
              终端的 /trace 写着"同时执行"
  名单        说"能一起跑"的工具，rewind.py 都认为它"什么都没改"
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable

from conftest import RecordingUI
from fake_api import reply, text, tool_use
from fake_openai import call, chat
from test_hooks import add, calls_of, script
from test_redact import ANTHROPIC

from harness import parallel, rewind
from harness.providers.base import Step, ToolCall, Usage
from harness.tools.files import FileEditor
from harness.tools.todo import TodoStore
from harness.trace import Tracer
from harness.trace.text import lines
from harness.undo import journal

FILE_TOOL = "str_replace_based_edit_tool"
WAIT = 10  # 秒。等别的线程最多等这么久（一个一个跑的话就是等到这么久然后失败；正常情况下一眨眼就过去了）
INTERRUPTED = "用户中断了操作，这个工具没有执行完。"


def view(tool_id: str, path: str) -> dict:
    return tool_use(tool_id, FILE_TOOL, {"command": "view", "path": path})


def results_of(request: dict) -> list[dict]:
    """一个请求里，最后一条消息中的工具结果。"""
    return [block for block in request["messages"][-1]["content"] if block["type"] == "tool_result"]


def must(done: bool) -> None:
    """线程里等一件事：等不到就抛出去（变成这次调用"工具内部出错"的结果，测试看得到）。"""
    if not done:
        raise AssertionError("等太久了：它们没有同时跑")


class Reads:
    """换掉文件工具真正读文件的那一步（FileEditor._view）：记下谁在哪个线程里读、最多有几个同时在读，
    读之前按文件名做测试要的事（在 Barrier 上等别的几个、等一个 Event……）。"""

    def __init__(self, monkeypatch, before: dict[str, Callable[[], object]] | None = None):
        self.before = before or {}
        self.lock = threading.Lock()
        self.threads: dict[str, str] = {}  # 文件名 → 在哪个线程里读的
        self.finished: list[str] = []  # 读完的先后
        self.running = 0
        self.most = 0
        real = FileEditor._view
        reads = self

        def fake(editor: FileEditor, path, view_range):
            with reads.lock:
                reads.running += 1
                reads.most = max(reads.most, reads.running)
                reads.threads[path.name] = threading.current_thread().name
            try:
                action = reads.before.get(path.name)
                if action is not None:
                    action()
                return real(editor, path, view_range)
            finally:
                with reads.lock:
                    reads.running -= 1
                    reads.finished.append(path.name)

        monkeypatch.setattr(FileEditor, "_view", fake)

    def in_pool(self, name: str) -> bool:
        return self.threads[name].startswith(parallel.THREAD_NAME)


def files(h, *names: str) -> None:
    for name in names:
        (h.config.workspace / name).write_text(f"这是 {name}", encoding="utf-8")


class SequenceUI(RecordingUI):
    """记下界面收到的先后：("call", 名字) / ("result", 文字) / ("confirm", 说明) / ("auto", 为什么)。"""

    def __init__(self, approve: bool = True, feedback: str = ""):
        super().__init__(approve, feedback)
        self.sequence: list[tuple[str, str]] = []

    def tool_call(self, name: str, tool_input: object) -> None:
        super().tool_call(name, tool_input)
        self.sequence.append(("call", name))

    def tool_result(self, text: str, is_error: bool, preview: dict | None = None) -> None:
        super().tool_result(text, is_error, preview)
        self.sequence.append(("result", text))

    def confirm(self, description: str, agent: str = "") -> tuple[bool, str]:
        self.sequence.append(("confirm", description))
        return super().confirm(description, agent)

    def auto_allowed(self, why: str = "mode") -> None:
        super().auto_allowed(why)
        self.sequence.append(("auto", why))


# ---- 一起跑 ------------------------------------------------------------------------


def test_reads_in_one_step_run_at_the_same_time(start, monkeypatch):
    h = start(reply(view("t1", "a.txt"), view("t2", "b.txt"), view("t3", "c.txt"), stop_reason="tool_use"), reply(text("看完了。")))
    files(h, "a.txt", "b.txt", "c.txt")
    meet = threading.Barrier(3, timeout=WAIT)  # 三个都到了才放行：一个一个跑的话，第一个就在这里等到超时
    reads = Reads(monkeypatch, {name: meet.wait for name in ("a.txt", "b.txt", "c.txt")})
    h.agent.run_turn("看看这三个文件")

    results = results_of(h.api.requests[1])
    assert [result["tool_use_id"] for result in results] == ["t1", "t2", "t3"]
    assert all("is_error" not in result for result in results), results
    assert ["这是 a.txt" in results[0]["content"], "这是 b.txt" in results[1]["content"], "这是 c.txt" in results[2]["content"]] == [True] * 3
    assert reads.most == 3
    assert all(reads.in_pool(name) for name in ("a.txt", "b.txt", "c.txt"))
    assert h.ui.confirmations == [] and h.ui.auto_allowed_count == 0


def test_todo_list_task_list_memory_and_skill_reads_join_in_too(start, monkeypatch):
    h = start(
        reply(
            view("t1", "a.txt"),
            tool_use("t2", "todo_list", {}),
            tool_use("t3", "task_list", {}),
            tool_use("t4", "memory", {"command": "view", "path": "/memories"}),
            tool_use("t5", "skill", {"name": "notes"}),
            stop_reason="tool_use",
        ),
        reply(text("都看过了。")),
    )
    files(h, "a.txt")
    h.app.skills.create("notes", "Take notes the way the user likes. Use when the user wants notes.", "# 笔记\n")
    meet = threading.Barrier(2, timeout=WAIT)
    Reads(monkeypatch, {"a.txt": meet.wait})
    real_items = TodoStore.items
    seen: dict[str, str] = {}

    def items(self, status: str = "pending") -> list[dict]:
        seen["todo_list"] = threading.current_thread().name
        meet.wait()  # 和读 a.txt 的那一个碰头
        return real_items(self, status)

    monkeypatch.setattr(TodoStore, "items", items)
    h.agent.run_turn("看看我都有什么")

    results = results_of(h.api.requests[1])
    assert [result["tool_use_id"] for result in results] == ["t1", "t2", "t3", "t4", "t5"]
    assert all("is_error" not in result for result in results), results
    assert seen["todo_list"].startswith(parallel.THREAD_NAME)
    assert "已载入 Skill「notes」" in results[4]["content"]
    assert [span.get("batch") for span in h.agent.trace.last["spans"] if span["kind"] == "tool"] == [1] * 5


def test_results_keep_the_order_of_the_calls_even_when_later_ones_finish_first(start, monkeypatch):
    ui = SequenceUI()
    h = start(
        reply(view("t1", "a.txt"), view("t2", "b.txt"), view("t3", "c.txt"), stop_reason="tool_use"),
        reply(text("看完了。")),
        ui=ui,
    )
    files(h, "a.txt", "b.txt", "c.txt")
    b_done, c_done = threading.Event(), threading.Event()
    # a 等 b 读完，b 等 c 读完：读完的先后正好和调用的顺序反过来
    reads = Reads(monkeypatch, {"a.txt": lambda: must(b_done.wait(WAIT)), "b.txt": lambda: must(c_done.wait(WAIT))})
    real = FileEditor._view

    def view_then_tell(editor, path, view_range):
        try:
            return real(editor, path, view_range)
        finally:
            {"b.txt": b_done, "c.txt": c_done}.get(path.name, threading.Event()).set()

    monkeypatch.setattr(FileEditor, "_view", view_then_tell)
    h.agent.run_turn("看看这三个文件")

    assert reads.finished == ["c.txt", "b.txt", "a.txt"]
    # 交给模型的结果：按调用的顺序，内容对得上各自的调用
    results = results_of(h.api.requests[1])
    assert [result["tool_use_id"] for result in results] == ["t1", "t2", "t3"]
    assert [name in result["content"] for name, result in zip(("a.txt", "b.txt", "c.txt"), results)] == [True] * 3
    # 界面收到的仍是一张卡片接着它自己的结果：页面靠这个顺序把结果画进卡片，倒回一轮时也靠它认哪一步做过什么
    kinds = [kind for kind, _ in ui.sequence]
    assert kinds == ["call", "result"] * 3
    assert ["a.txt" in ui.sequence[1][1], "b.txt" in ui.sequence[3][1], "c.txt" in ui.sequence[5][1]] == [True] * 3


def test_the_openai_format_gets_the_results_in_order_too(start_openai, monkeypatch):
    h = start_openai(
        chat(calls=[call("c1", FILE_TOOL, json.dumps({"command": "view", "path": "a.txt"})), call("c2", FILE_TOOL, json.dumps({"command": "view", "path": "b.txt"}))]),
        chat("看完了。"),
    )
    files(h, "a.txt", "b.txt")
    meet = threading.Barrier(2, timeout=WAIT)
    reads = Reads(monkeypatch, {"a.txt": meet.wait, "b.txt": meet.wait})
    h.agent.run_turn("看看这两个文件")

    tool_messages = [message for message in h.api.requests[1]["messages"] if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == ["c1", "c2"]
    assert "这是 a.txt" in tool_messages[0]["content"] and "这是 b.txt" in tool_messages[1]["content"]
    assert not any(message["content"].startswith("[出错]") for message in tool_messages)
    assert reads.most == 2


# ---- 分段：要问你的、这一档放行的、会改东西的，单独跑 --------------------------------------


def test_a_call_that_asks_runs_alone_and_splits_the_step(start, monkeypatch, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("外面的内容", encoding="utf-8")
    reads: Reads

    class WatchingUI(SequenceUI):
        def confirm(self, description: str, agent: str = "") -> tuple[bool, str]:
            assert reads.running == 0, "问你的时候不该有别的调用在跑"
            return super().confirm(description, agent)

    ui = WatchingUI()
    h = start(
        reply(view("t1", "a.txt"), view("t2", "b.txt"), view("t3", str(outside)), view("t4", "c.txt"), view("t5", "d.txt"), stop_reason="tool_use"),
        reply(text("看完了。")),
        ui=ui,
    )
    files(h, "a.txt", "b.txt", "c.txt", "d.txt")
    first, second = threading.Barrier(2, timeout=WAIT), threading.Barrier(2, timeout=WAIT)
    reads = Reads(monkeypatch, {"a.txt": first.wait, "b.txt": first.wait, "c.txt": second.wait, "d.txt": second.wait})
    h.agent.run_turn("看看这几个文件")

    results = results_of(h.api.requests[1])
    assert [result["tool_use_id"] for result in results] == ["t1", "t2", "t3", "t4", "t5"]
    assert all("is_error" not in result for result in results), results
    # 前两个一起跑、读外面那个的单独跑（先问你）、后两个又一起跑
    assert [reads.in_pool(name) for name in ("a.txt", "b.txt", "outside.txt", "c.txt", "d.txt")] == [True, True, False, True, True]
    assert len(ui.confirmations) == 1 and "outside.txt" in ui.confirmations[0]
    assert [kind for kind, _ in ui.sequence] == ["call", "result", "call", "result", "call", "confirm", "result", "call", "result", "call", "result"]
    spans = [span for span in h.agent.trace.last["spans"] if span["kind"] == "tool"]
    assert [span.get("batch") for span in spans] == [1, 1, None, 2, 2]
    assert spans[2]["approval"] == "allowed"


def test_a_read_the_mode_lets_through_runs_alone_after_its_record(start, monkeypatch, tmp_path):
    # "只读的命令不用问"：读工作目录之外的文件不问你，但它本来是要问的，所以照旧单独跑，
    # 先在它的卡片上记下"自动允许"再读（一起跑的话结果先出来、记录后到）
    outside = tmp_path / "outside.txt"
    outside.write_text("外面的内容", encoding="utf-8")
    ui = SequenceUI()
    h = start(reply(view("t1", "a.txt"), view("t2", str(outside)), view("t3", "b.txt"), stop_reason="tool_use"), reply(text("好")), ui=ui)
    h.app.permissions.mode = "readonly"
    files(h, "a.txt", "b.txt")
    reads = Reads(monkeypatch)
    h.agent.run_turn("看看")

    assert ui.confirmations == [] and ui.auto_whys == ["readonly"]
    assert [kind for kind, _ in ui.sequence] == ["call", "result", "call", "auto", "result", "call", "result"]
    # 一个一个隔开了，谁都凑不成一批
    assert not any(reads.in_pool(name) for name in ("a.txt", "outside.txt", "b.txt"))
    assert all("batch" not in span for span in h.agent.trace.last["spans"])


def test_a_call_that_changes_something_runs_alone_even_without_asking(start, monkeypatch):
    # 加待办不用问，但它会改东西：照旧单独、在这一轮的线程里跑。它前后的读各自一起跑
    h = start(
        reply(view("t1", "a.txt"), view("t2", "b.txt"), tool_use("t3", "todo_add", {"title": "买牛奶"}), view("t4", "c.txt"), view("t5", "d.txt"), stop_reason="tool_use"),
        reply(text("好")),
    )
    files(h, "a.txt", "b.txt", "c.txt", "d.txt")
    first, second = threading.Barrier(2, timeout=WAIT), threading.Barrier(2, timeout=WAIT)
    reads = Reads(monkeypatch, {"a.txt": first.wait, "b.txt": first.wait, "c.txt": second.wait, "d.txt": second.wait})
    added: list[tuple[str, bool]] = []
    real_add = TodoStore.add

    def add_todo(self, title, due=None, note=None):
        with reads.lock:
            added.append((threading.current_thread().name, reads.running == 0))
        return real_add(self, title, due, note)

    monkeypatch.setattr(TodoStore, "add", add_todo)
    h.agent.run_turn("看看，再记一条")

    results = results_of(h.api.requests[1])
    assert all("is_error" not in result for result in results), results
    assert [item["title"] for item in h.todos.items()] == ["买牛奶"]
    ((thread, alone),) = added
    assert not thread.startswith(parallel.THREAD_NAME) and alone, "加待办的时候没有读在跑：前一批已经做完，后一批还没开始"
    assert [reads.in_pool(name) for name in ("a.txt", "b.txt", "c.txt", "d.txt")] == [True] * 4


def test_one_read_alone_is_not_a_batch(start, monkeypatch):
    h = start(reply(view("t1", "a.txt"), stop_reason="tool_use"), reply(text("好")))
    files(h, "a.txt")
    reads = Reads(monkeypatch)
    h.agent.run_turn("看看")
    assert not reads.in_pool("a.txt")
    assert "batch" not in h.agent.trace.last["spans"][1]


def test_setting_the_limit_to_one_turns_it_off(start, monkeypatch):
    h = start(reply(view("t1", "a.txt"), view("t2", "b.txt"), stop_reason="tool_use"), reply(text("好")), max_parallel_tools=1)
    files(h, "a.txt", "b.txt")
    reads = Reads(monkeypatch)
    h.agent.run_turn("看看")
    assert not reads.in_pool("a.txt") and not reads.in_pool("b.txt")
    assert "is_error" not in results_of(h.api.requests[1])[1]


def test_at_most_the_limit_run_at_once(start, monkeypatch):
    names = [f"f{n}.txt" for n in range(6)]
    h = start(reply(*(view(f"t{n}", name) for n, name in enumerate(names)), stop_reason="tool_use"), reply(text("好")), max_parallel_tools=3)
    files(h, *names)
    meet = threading.Barrier(3, timeout=WAIT)  # 三个一组碰头：线程池只有三个线程，正好两组
    reads = Reads(monkeypatch, {name: meet.wait for name in names})
    h.agent.run_turn("看看")
    assert reads.most == 3
    assert all("is_error" not in result for result in results_of(h.api.requests[1]))


# ---- 停下、出错 ----------------------------------------------------------------------


def test_stopping_in_the_middle_of_a_batch_still_answers_every_call(start, monkeypatch):
    class StopAfterFirstResult(RecordingUI):
        """第一张卡片有了结果以后你点了"停止"：网页的界面就是这样，发完这个结果再看一眼停没停（见 webui/ui.py 的 tool_result）。"""

        def tool_result(self, text: str, is_error: bool, preview: dict | None = None) -> None:
            super().tool_result(text, is_error, preview)
            raise KeyboardInterrupt

    names = ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt", "f.txt"]
    h = start(reply(*(view(f"t{n}", name) for n, name in enumerate(names, 1)), stop_reason="tool_use"), reply(text("停下了。")), ui=StopAfterFirstResult(), max_parallel_tools=2)
    files(h, *names)
    release = threading.Event()
    # 除了第一个，读的时候都卡住，等测试放行：线程池只有两个线程，所以 d、e、f 在停下之前一个都开始不了
    reads = Reads(monkeypatch, {name: lambda: release.wait(WAIT) for name in names[1:]})
    try:
        try:
            h.agent.run_turn("看看这六个文件")
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("这一轮应该被停下")
    finally:
        release.set()

    # 历史仍然合法：六个调用都有结果，按顺序；第一个是真的结果，别的是"被中断了"
    last = h.agent.messages[-1]["content"]
    assert [block["tool_use_id"] for block in last] == ["t1", "t2", "t3", "t4", "t5", "t6"]
    assert "这是 a.txt" in last[0]["content"] and "is_error" not in last[0]
    assert all(block["is_error"] and block["content"] == INTERRUPTED for block in last[1:])
    # 还没开始的不再开始。停下之前最多开始了 b、c（池子里只有两个线程，b、c 读的时候卡着，d 要等它们之一读完，
    # 而放行是在取消之后）；a 读得快、停得也快的话，b 说不定还没被线程拿走就取消了，所以这里只看上限。已经开始的读完就扔掉
    deadline = time.monotonic() + WAIT
    while reads.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "a.txt" in reads.threads and set(reads.threads) <= {"a.txt", "b.txt", "c.txt"}, reads.threads
    # 运行记录：第一次调用画出来了；停下的那一轮照常交出记录
    record = h.agent.trace.last
    assert record["ended"] == "stopped"
    assert [span["result"] for span in record["spans"] if span["kind"] == "tool"] == ["ok"]
    # 之后可以接着聊
    h.agent.run_turn("算了")
    assert h.api.requests[-1]["messages"][-1]["role"] == "user"


def test_an_error_in_one_tool_does_not_lose_the_others(start, monkeypatch):
    h = start(reply(view("t1", "a.txt"), view("t2", "broken.txt"), view("t3", "c.txt"), stop_reason="tool_use"), reply(text("好")))
    files(h, "a.txt", "broken.txt", "c.txt")
    meet = threading.Barrier(2, timeout=WAIT)

    def explode() -> None:
        raise RuntimeError("磁盘坏了")

    Reads(monkeypatch, {"a.txt": meet.wait, "broken.txt": explode, "c.txt": meet.wait})
    h.agent.run_turn("看看")

    first, broken, third = results_of(h.api.requests[1])
    assert "这是 a.txt" in first["content"] and "is_error" not in first
    assert broken["is_error"] and broken["content"] == "工具内部出错：RuntimeError: 磁盘坏了"
    assert "这是 c.txt" in third["content"] and "is_error" not in third
    spans = [span for span in h.agent.trace.last["spans"] if span["kind"] == "tool"]
    assert [span["result"] for span in spans] == ["ok", "error", "ok"]


# ---- 每一次调用都有的：钩子、遮密钥、ContextVar ---------------------------------------------


def test_hooks_after_each_call_and_masking_still_happen_once_per_call(start, monkeypatch):
    h = start(reply(view("t1", "a.txt"), view("t2", "b.txt"), stop_reason="tool_use"), reply(text("好")))
    for name in ("a.txt", "b.txt"):
        (h.config.workspace / name).write_text(f"{name} 的密钥 {ANTHROPIC}\n", encoding="utf-8")
    note = json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "补一句"}})
    argv, log = script(h.config.workspace.parent / "hooks", "note", stdout=note)
    add(h, "note", "PostToolUse", argv, matcher="files")
    meet = threading.Barrier(2, timeout=WAIT)
    reads = Reads(monkeypatch, {"a.txt": meet.wait, "b.txt": meet.wait})
    h.agent.run_turn("看看")

    assert reads.in_pool("a.txt") and reads.in_pool("b.txt")
    for result, name in zip(results_of(h.api.requests[1]), ("a.txt", "b.txt")):
        content = result["content"]
        assert name in content and ANTHROPIC not in content and "‹已遮住：疑似 Anthropic 密钥›" in content
        assert content.count('<hook_note name="note">') == 1 and content.index("<hook_note") > content.index(name)
    # 钩子按调用的顺序一个一个跑，看到的是遮好的那一份
    seen = calls_of(log)
    assert [entry["payload"]["tool_use_id"] for entry in seen] == ["t1", "t2"]
    assert all(ANTHROPIC not in entry["payload"]["tool_response"] for entry in seen)
    spans = [span for span in h.agent.trace.last["spans"] if span["kind"] == "tool"]
    assert [(span["batch"], span["redacted"], [child["event"] for child in span["children"]]) for span in spans] == [(1, 1, ["PostToolUse"])] * 2


def test_a_call_your_pre_tool_hook_looks_at_runs_alone(start, monkeypatch):
    # PreToolUse 钩子可能要你再确认一次（确认只能一个一个来），它要看的也是"马上要执行这一次"的那一刻：这样的调用照旧单独跑
    h = start(reply(view("t1", "a.txt"), view("t2", "b.txt"), tool_use("t3", "todo_list", {}), tool_use("t4", "task_list", {}), stop_reason="tool_use"), reply(text("好")))
    files(h, "a.txt", "b.txt")
    argv, log = script(h.config.workspace.parent / "hooks", "look")
    add(h, "look", "PreToolUse", argv, matcher="files")
    reads = Reads(monkeypatch)
    real_items = TodoStore.items
    meet = threading.Barrier(2, timeout=WAIT)
    threads: dict[str, str] = {}

    def items(self, status: str = "pending") -> list[dict]:
        threads["todo_list"] = threading.current_thread().name
        meet.wait()
        return real_items(self, status)

    real_snapshot = h.app.tasks.snapshot

    def snapshot():
        threads["task_list"] = threading.current_thread().name
        meet.wait()
        return real_snapshot()

    monkeypatch.setattr(TodoStore, "items", items)
    monkeypatch.setattr(h.app.tasks, "snapshot", snapshot)
    h.agent.run_turn("看看")

    assert not reads.in_pool("a.txt") and not reads.in_pool("b.txt")
    assert [entry["payload"]["tool_use_id"] for entry in calls_of(log)] == ["t1", "t2"]
    # 钩子不看的那两个照样一起跑
    assert all(name.startswith(parallel.THREAD_NAME) for name in threads.values())
    assert all("is_error" not in result for result in results_of(h.api.requests[1]))


def test_the_threads_see_the_turns_context_vars(start, monkeypatch):
    # 撤回文件的记账挂在这一轮线程的 ContextVar 上（undo/journal.py）。新开的线程默认看不到它，所以每个调用在一份复制的 context 里跑
    h = start(reply(view("t1", "a.txt"), view("t2", "b.txt"), stop_reason="tool_use"), reply(text("好")))
    files(h, "a.txt", "b.txt")
    marker = object()
    seen: list[object] = []
    meet = threading.Barrier(2, timeout=WAIT)

    def look() -> None:
        seen.append(journal._CURRENT.get())
        meet.wait()

    reads = Reads(monkeypatch, {"a.txt": look, "b.txt": look})
    token = journal._CURRENT.set(marker)  # 只是一个记号：这里只看线程里拿不拿得到它（这一轮不改文件，不会真的拿它记账）
    try:
        h.agent.run_turn("看看")
    finally:
        journal._CURRENT.reset(token)
    assert reads.in_pool("a.txt") and reads.in_pool("b.txt")
    assert seen == [marker, marker]


# ---- 运行记录 ------------------------------------------------------------------------


def adds_up(record: dict) -> bool:
    totals = record["totals"]
    return totals["model"] + totals["tools"] + totals["hooks"] + totals["waiting"] + totals["other"] == record["wall"]


def test_a_batch_shows_as_overlapping_spans_and_the_totals_still_add_up(start, monkeypatch):
    h = start(reply(view("t1", "a.txt"), view("t2", "b.txt"), view("t3", "c.txt"), stop_reason="tool_use"), reply(text("好")))
    files(h, "a.txt", "b.txt", "c.txt")
    meet = threading.Barrier(3, timeout=WAIT)

    def meet_then_read() -> None:
        meet.wait()
        time.sleep(0.05)  # 三个都过了 Barrier 以后各读 50 毫秒：它们的横条一定叠在一起

    Reads(monkeypatch, {name: meet_then_read for name in ("a.txt", "b.txt", "c.txt")})
    h.agent.run_turn("看看")

    record = h.agent.trace.last
    spans = [span for span in record["spans"] if span["kind"] == "tool"]
    assert [span["batch"] for span in spans] == [1, 1, 1]
    assert max(span["start"] for span in spans) < min(span["end"] for span in spans), spans
    # 工具的时间是这三段一共占了多久，不是各自加起来（加起来就超过了它们实际花的时间）。
    # 差 2 毫秒以内算对：合计按"累计到这一项"取整（见 recorder._totals），每一段的 start、end 又各自取整成毫秒
    union = max(span["end"] for span in spans) - min(span["start"] for span in spans)
    assert abs(record["totals"]["tools"] - union) <= 2
    assert union + 2 < sum(span["end"] - span["start"] for span in spans)
    assert record["totals"]["tool_calls"] == 3
    assert adds_up(record)


def test_the_recorder_counts_a_batch_by_the_time_it_covered():
    class Clock:
        now = 100.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    tracer = Tracer(timer=clock, wall=lambda: 1.0)
    tracer.turn_began()
    tracer.request_began("anthropic", "claude-opus-5-5")
    clock.now = 100.5
    calls = [ToolCall(f"t{n}", "todo_list", {}) for n in range(3)]
    tracer.request_ended(Step(status="tools", tool_calls=calls, usage=Usage(requests=1)), 0.0)
    batch = tracer.batch_began()
    # 三段同时跑：[0.5, 1.5]、[0.5, 0.9]、[0.6, 1.1]（从这一轮开始算的秒）；第一段的 PostToolUse 钩子在 [1.5, 1.6] 跑
    clock.now = 101.5
    tracer.tool_began(calls[0], None, start=100.5, batch=batch)
    tracer.hook_began("PostToolUse", "note")
    clock.now = 101.6
    tracer.hook_ended("ok")
    tracer.tool_ended("[]", False, end=101.5)
    for tool_call, (began, ended) in zip(calls[1:], ((100.5, 100.9), (100.6, 101.1))):
        tracer.tool_began(tool_call, None, start=began, batch=batch)
        tracer.tool_ended("[]", False, end=ended)
    # 后面一个单独跑的：[1.6, 1.7]
    tracer.tool_began(ToolCall("t9", "todo_list", {}), None)
    clock.now = 101.7
    tracer.tool_ended("[]", False)
    record = tracer.turn_ended("done")

    assert batch == 1 and tracer.batch_began() == 2  # 这一轮里的第几批（从 1 数）
    tools = [span for span in record["spans"] if span["kind"] == "tool"]
    assert [(span["start"], span["end"], span.get("batch")) for span in tools] == [(500, 1500, 1), (500, 900, 1), (600, 1100, 1), (1600, 1700, None)]
    assert tools[0]["children"][0]["start"] == 1500
    # 那一批占了 [0.5, 1.6]，其中 [1.5, 1.6] 是钩子：工具 1000 毫秒（不是 1000 + 400 + 500）、钩子 100；后面单独那个 100
    assert record["totals"]["tools"] == 1000 + 100 and record["totals"]["hooks"] == 100
    assert record["totals"]["model"] == 500 and record["wall"] == 1700 and adds_up(record)


def test_the_terminal_trace_says_which_steps_ran_together(start, monkeypatch):
    h = start(reply(view("t1", "a.txt"), view("t2", "b.txt"), tool_use("t3", "todo_add", {"title": "x"}), stop_reason="tool_use"), reply(text("好")))
    files(h, "a.txt", "b.txt")
    h.agent.run_turn("看看")
    rows = [row for row, _ in lines(h.agent.trace.last)]
    tool_rows = [row for row in rows if FILE_TOOL in row or "todo_add" in row]
    assert [("和另外 1 步同时执行" in row) for row in tool_rows] == [True, True, False]


# ---- 哪些工具能一起跑 ---------------------------------------------------------------------


def test_every_tool_that_may_run_alongside_others_only_looks(start):
    # 能一起跑的一定"什么都没改"（rewind.py 倒回一轮之前不用为它问你）。反过来不一定：浏览器里滚动、截图也算"只是看"，
    # 可它们共用一个浏览器、谁先谁后结果不一样，所以不能一起跑（见 parallel.py 开头）
    h = start()
    samples = {
        FILE_TOOL: [{"command": "view", "path": "a.txt"}, {"command": "create", "path": "a.txt", "file_text": ""}, {"command": "str_replace", "path": "a.txt"}],
        "memory": [{"command": "view", "path": "/memories"}, {"command": "create", "path": "/memories/x.md"}, {"command": "delete", "path": "/memories/x.md"}],
        "todo_list": [{}],
        "task_list": [{}],
        "skill": [{"name": "x"}],
        "todo_add": [{"title": "x"}],
        "run_command": [{"command": "ls"}],
        "task_add": [{}],
        "agent": [{"name": "x", "task": "y"}],
    }
    together = set()
    for tool in h.agent.tools:
        for sample in samples.get(tool.name, [{}]):
            if tool.parallel is not None and tool.parallel(sample):
                together.add((tool.name, sample.get("command", "")))
                assert not rewind.changes(tool.name, sample), (tool.name, sample)
    assert together == {(FILE_TOOL, "view"), ("memory", "view"), ("todo_list", ""), ("task_list", ""), ("skill", "")}
