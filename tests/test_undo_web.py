"""撤回一轮改的文件：网页这一侧（file_changes、file_undo 事件，/api/changes/……，倒回最后一轮时一起撤回，告诉模型）。

和 test_rewind_web.py 一样：真实的 HTTP 服务器，测试代码扮演浏览器，模型是本地的假 API（不花钱），
工作目录和数据目录都在 tmp_path 里（make_config）。撤回本身的规矩（记账、留底、冲突、上限、哈希）在 test_undo.py 里逐个测；
这里测的是它们接到页面上以后的样子：
  - 一轮结束时那一行"改了 N 个文件"发没发、发在哪儿、存进会话文件的哪一部分（events 有，messages 没有）；
  - 接口怎么回答：格式不对 400，对不上（别的会话、不存在的一轮、要先确认的）409，正在进行一轮时 409；
  - 撤回、重做以后：发一个 file_undo、存盘、状态里最后一轮的 files 跟着变；
  - 下一条消息有没有告诉模型、只说一次、没发出去的下次再说；
  - 重新打开、程序重启以后照样能重做；
  - 倒回最后一轮时一起撤回（有冲突就先不倒回去），和更早那几轮的撤回记录不会跟着被截掉；
  - 清单被人改成指向 .env 时，接口照样不写它。
模型一律用文件工具改文件，"要不要每次都问"设成 edits（改工作目录里的文件不用问），不用一张张点确认卡片。
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest
import test_rewind_web
import test_webui
from fake_api import failure, reply, text, tool_use
from test_rewind_web import (
    content,
    dumps,
    kinds,
    last_id,
    replay_and_turn,
    target,
    texts,
    turn,
    wait_event,
)
from test_webui import (
    Browser,
    act,
    latest_state,
    open_conversation,
    request,
    saved_file,
    set_approval,
    without_states,
)

from harness.config import UNDO_TURN_FILES
from harness.undo import journal as journal_module
from harness.undo import store
from harness.undo.notes import SAID
from harness.undo.words import ASKING, BLOCKING
from harness.webui.server import WebApp

# 借用 test_rewind_web 的 fixture：网页服务器 + 假 API，s.web 是服务器上的 WebApp，助手的钟固定住
serve = test_webui.serve
rig = test_rewind_web.rig

EDIT = "str_replace_based_edit_tool"
MISSING_SET = "20200101-000000-abcdef"  # 格式正确、但哪儿都没有的一轮
NOT_FOUND = "找不到这一轮的改动记录。看一眼最新的内容再试。"
BAD = "参数不正确"
NONE_LEFT = "这一轮的改动已经全部撤回了。"
CONFIRM_FIRST = "这几个文件要先确认一下再撤回。"


def create(n: int, path: str, body: str) -> dict:
    return tool_use(f"toolu_{n}", EDIT, {"command": "create", "path": path, "file_text": body})


def replace(n: int, path: str, old: str, new: str) -> dict:
    return tool_use(f"toolu_{n}", EDIT, {"command": "str_replace", "path": path, "old_str": old, "new_str": new})


def edits(*calls: dict, answer: str = "改好了。") -> list[dict]:
    """一轮：模型在一步里依次调用这几次工具，然后答一句。"""
    return [reply(*calls, stop_reason="tool_use"), reply(text(answer))]


def started(rig, *replies, files: dict[str, str] | None = None):
    """起服务器，工作目录里先放好这几个文件，"要不要每次都问"设成 edits。"""
    s = rig(*replies)
    for name, body in (files or {}).items():
        work(s, name).write_text(body, encoding="utf-8", newline="")
    set_approval(s, "edits")
    return s


def work(s, name: str):
    return s.config.workspace / name


def last_line(s) -> dict:
    """页面收到的最后一行"改了 N 个文件"（file_changes 事件）。"""
    return s.browser.of_type("file_changes")[-1]


def post(s, action: str, set_id: object, **body) -> tuple[int, dict]:
    """页面上点"撤回"（undo）、"重做"（redo）：带着它看到的会话和那一轮的编号。"""
    return s.browser.post(f"/api/changes/{action}", {"conversation": latest_state(s.browser)["conversation"], "set": set_id, **body})


def diff(s, set_id: object, index: object) -> tuple[int, dict]:
    return s.browser.post("/api/changes/diff", {"set": set_id, "index": index})


def say(s, words: str) -> dict:
    """发一条消息，等这一轮结束，返回结束时的状态。

    和 test_webui.say 一样，只是按编号等：倒回去以后旧的事件会整段重放一遍（里面也有 turn_end），只看类型会等错。"""
    after = last_id(s)
    assert s.browser.post("/api/message", {"text": words})[0] == 200
    end = wait_event(s.browser, "turn_end", after)
    return [event for event in list(s.browser.events) if event["type"] == "state" and event["id"] < end["id"]][-1]


def changed(s, action: str, set_id: str, **body) -> tuple[dict, dict]:
    """撤回或重做，要做成。返回（页面收到的 file_undo 事件，它之后的状态）。只发一个 file_undo。
    点的是"仍然撤回"（force）的话，回答里照样列着那几个要确认的（页面在那一行上说明做了什么），这里不管它们。"""
    after = last_id(s)
    status, answer = post(s, action, set_id, **body)
    assert status == 200 and answer["ok"] is True and answer["blocked"] == [], answer
    assert body.get("force") or answer["asks"] == [], answer
    event = wait_event(s.browser, "file_undo", after)
    state = wait_event(s.browser, "state", event["id"])
    assert kinds(s.ui.events_after(after, timeout=0)).count("file_undo") == 1
    return event, state


def refused(answer: tuple[int, dict], error: str) -> None:
    """撤回、重做没做成：409，说明原因，没有要确认的、没有做不了的。"""
    assert answer == (409, {"error": error, "asks": [], "blocked": []}), answer


def note_in(request_body: dict) -> str:
    """请求里最后一条用户消息上附的"文件撤回过"的说明（没有是空的）。它要在用户的话前面。"""
    parts = [block.get("text", "") for block in request_body["messages"][-1]["content"]]
    found = [index for index, part in enumerate(parts) if "<file_changes>" in part]
    if not found:
        return ""
    assert len(found) == 1 and found[0] < len(parts) - 1, parts  # 只有一段，而且用户的话在最后
    return parts[found[0]]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---- 一轮结束时的那一行 ----------------------------------------------------------


def test_a_turn_that_changes_files_ends_with_one_line(rig):
    s = started(rig, *edits(replace(1, "a.txt", "one", "ONE"), create(2, "new/b.txt", "甲\n乙\n")), files={"a.txt": "one\n"})
    state = say(s, "改一下")

    # 一轮只有一行，在回答和运行记录（trace，见 harness/trace/）之后、turn_end 之前
    shown = without_states(s.browser)
    assert shown[-4:] == ["block_end", "trace", "file_changes", "turn_end"] and shown.count("file_changes") == 1
    line = last_line(s)
    assert store.ID.fullmatch(line["set"])
    assert content(line) == {
        "type": "file_changes",
        "set": line["set"],
        "files": [
            {"path": "a.txt", "kind": "modified", "added": 1, "removed": 1},  # 工作目录里的写相对路径
            {"path": os.path.join("new", "b.txt"), "kind": "created", "added": 2, "removed": 0},
        ],
        "untracked": 0,
    }  # 没执行命令：没有 commands
    assert state["rewind"]["files"] == 2  # 倒回最后一轮的确认卡片上可以勾"同时撤回这一轮改的 2 个文件"

    # 存进会话文件：events 里原样有这一行，messages（发给模型的历史）里一点痕迹都没有
    saved = saved_file(s.config, state["conversation"])
    assert [event for event in saved["events"] if event["type"] == "file_changes"] == [content(line)]
    assert line["set"] not in dumps(saved["messages"]) and "file_changes" not in dumps(saved["messages"])
    assert "file_changes" not in dumps(s.api.requests)
    assert (s.config.undo_dir / line["set"] / store.MANIFEST).is_file()  # 留底在数据目录的 backups/turns/ 里


@pytest.mark.parametrize(
    "call",
    [
        None,  # 没用工具
        tool_use("toolu_1", EDIT, {"command": "view", "path": "a.txt"}),  # 只是看
        replace(1, "a.txt", "没有这句", "x"),  # 出错了，什么都没改
        create(1, "a.txt", "one\n"),  # 写进去的和原来一字不差
    ],
    ids=["plain", "view", "failed", "same"],
)
def test_a_turn_that_changes_nothing_has_no_line(rig, call):
    s = started(rig, *(edits(call) if call else [reply(text("没用工具。"))]), files={"a.txt": "one\n"})
    state = say(s, "看看")
    assert "file_changes" not in s.browser.types() and state["rewind"]["files"] == 0
    assert work(s, "a.txt").read_text(encoding="utf-8") == "one\n"
    assert "file_changes" not in dumps(saved_file(s.config, state["conversation"]))
    assert not any(s.config.undo_dir.glob("*"))  # 这一轮的留底也删掉了


def test_a_turn_that_also_ran_a_command_says_so(rig):
    s = rig(
        reply(create(1, "a.txt", "甲\n"), tool_use("toolu_2", "run_command", {"command": "touch made.txt"}), stop_reason="tool_use"),
        reply(text("好了。")),
        reply(tool_use("toolu_3", "run_command", {"command": "touch other.txt"}), stop_reason="tool_use"),
        reply(text("又好了。")),
    )
    set_approval(s, "all")
    say(s, "建两个文件")
    line = last_line(s)
    # 命令建的 made.txt 不在里面（看不到命令改了什么），但这一行写明这一轮执行过命令
    assert line["commands"] is True and [item["path"] for item in line["files"]] == ["a.txt"]
    assert work(s, "made.txt").exists()

    say(s, "再建一个")  # 只执行了命令：文件工具什么都没改，没有那一行
    assert len(s.browser.of_type("file_changes")) == 1 and work(s, "other.txt").exists()


def test_a_turn_that_fails_halfway_still_lists_what_it_changed(rig):
    s = started(rig, reply(create(1, "a.txt", "甲\n"), stop_reason="tool_use"), failure(529, "overloaded_error", "Overloaded"))
    state = say(s, "建个文件")
    assert s.browser.of_type("notice")[-1]["level"] == "error"
    assert [item["path"] for item in last_line(s)["files"]] == ["a.txt"] and state["rewind"]["files"] == 1
    changed(s, "undo", last_line(s)["set"])
    assert not work(s, "a.txt").exists()


# ---- 看一个文件改了什么 -------------------------------------------------------


def test_the_diff_of_one_file(rig):
    s = started(rig, *edits(replace(1, "a.txt", "one", "ONE"), create(2, "b.txt", "新的\n")), reply(text("另一段。")), files={"a.txt": "one\ntwo\n"})
    say(s, "改一下")
    set_id = last_line(s)["set"]

    modified = {"ok": True, "binary": False, "more": 0, "rows": [["@", 1, 1, ""], ["-", 1, 0, "one"], ["+", 0, 1, "ONE"], [" ", 2, 2, "two"]]}
    assert diff(s, set_id, 0) == (200, modified)
    assert diff(s, set_id, 1) == (200, {"ok": True, "binary": False, "more": 0, "rows": [["@", 1, 1, ""], ["+", 0, 1, "新的"]]})
    changed(s, "undo", set_id)
    assert diff(s, set_id, 0) == (200, modified)  # 看的是"这一轮做了什么"：撤回以后也一样

    # 不是这段会话里的一轮、格式不对的编号：对不上（409）
    for bad in ("nonsense", MISSING_SET, 1, None, ""):
        assert diff(s, bad, 0) == (409, {"error": NOT_FOUND}), bad
    assert s.browser.post("/api/changes/diff", {}) == (409, {"error": NOT_FOUND})
    # 不是清单里的第几个
    for index in (2, -1, "0", True, None, 0.0):
        assert diff(s, set_id, index) == (409, {"error": BAD}), index
    # 换到别的会话以后，那一轮就看不了了（页面最多只能给现在这段会话里的编号）
    act(s, "/api/new")
    assert diff(s, set_id, 0) == (409, {"error": NOT_FOUND})


# ---- 撤回、重做 ------------------------------------------------------------------


def test_undoing_and_redoing_from_the_page(rig):
    s = started(rig, *edits(replace(1, "a.txt", "one", "ONE"), create(2, "new/b.txt", "新的\n")), files={"a.txt": "one\n"})
    state = say(s, "改一下")
    conversation_id, set_id = state["conversation"], last_line(s)["set"]
    a, b = work(s, "a.txt"), work(s, "new/b.txt")
    messages = saved_file(s.config, conversation_id)["messages"]
    mode = a.stat().st_mode & 0o7777
    other = Browser(s.url)  # 另一个开着的标签页
    other.wait_synced()

    # 只撤回第一个
    after = last_id(s)
    event, state = changed(s, "undo", set_id, files=[0])
    assert a.read_text(encoding="utf-8") == "one\n" and b.read_text(encoding="utf-8") == "新的\n"
    assert content(event) == {
        "type": "file_undo",
        "set": set_id,
        "action": "undo",
        "files": [{"index": 0, "n": 1, "replaced": {"sha256": sha(b"ONE\n"), "size": 4, "mode": mode, "blob": True}}],
        "paths": ["a.txt"],
    }
    assert state["rewind"]["files"] == 1  # 还有一个没撤回
    assert content(wait_event(other, "file_undo", after)) == content(event)  # 开着的每个页面都照着它更新那一行
    # 存了盘：events 末尾多了这个事件，messages 一个字都没动
    saved = saved_file(s.config, conversation_id)
    assert saved["events"][-1] == content(event) and saved["messages"] == messages

    # 不给 files：这一轮里还没撤回的全部（这里只剩下新建的那个：删掉，连同为它新建的文件夹）
    event, state = changed(s, "undo", set_id)
    assert [item["index"] for item in event["files"]] == [1] and event["paths"] == [os.path.join("new", "b.txt")]
    assert not b.exists() and not b.parent.exists() and a.read_text(encoding="utf-8") == "one\n"
    assert state["rewind"]["files"] == 0
    refused(post(s, "undo", set_id), NONE_LEFT)

    # 重做：撤回时换下来的放回去
    event, state = changed(s, "redo", set_id)
    assert content(event) == {"type": "file_undo", "set": set_id, "action": "redo", "files": [{"index": 0}, {"index": 1}], "paths": ["a.txt", os.path.join("new", "b.txt")]}
    assert a.read_text(encoding="utf-8") == "ONE\n" and b.read_text(encoding="utf-8") == "新的\n" and state["rewind"]["files"] == 2
    refused(post(s, "redo", set_id), "这一轮没有撤回过的文件，不用重做。")
    saved = saved_file(s.config, conversation_id)
    assert kinds(saved["events"]).count("file_undo") == 3 and saved["messages"] == messages


def test_bad_undo_requests_are_refused(rig):
    s = started(rig, *edits(replace(1, "a.txt", "one", "ONE")), reply(text("另一段。")), files={"a.txt": "one\n"})
    state = say(s, "改一下")
    conversation_id, set_id = state["conversation"], last_line(s)["set"]
    good = {"conversation": conversation_id, "set": set_id}
    log = s.ui.events_after(0, timeout=0)

    # 格式不对：400
    for bad in (
        {}, {"set": set_id}, {"conversation": conversation_id}, {**good, "conversation": 1}, {**good, "set": 1}, {**good, "set": None},
        {**good, "force": "yes"}, {**good, "force": 1}, {**good, "force": None}, {**good, "files": 0}, {**good, "files": "0"},
        {**good, "files": {"0": 0}}, {**good, "files": []}, {**good, "files": list(range(UNDO_TURN_FILES + 1))},
    ):  # fmt: skip
        for action in ("undo", "redo"):
            assert s.browser.post(f"/api/changes/{action}", bad) == (400, {"error": BAD}), bad
    # 格式对，但不是清单里的第几个、重复了：照样不做
    for files in ([1], [-1], ["0"], [True], [None], [0, 0]):
        refused(post(s, "undo", set_id, files=files), BAD)
    # 不存在的一轮
    for bad_set in ("nonsense", MISSING_SET, ""):
        refused(post(s, "undo", bad_set), NOT_FOUND)
    assert s.browser.post("/api/changes/nope", good)[0] == 404
    # 没带口令的、别的网站发起的：和别的接口一样到不了这里
    assert request(s.browser.host, "POST", "/api/changes/undo", good)[0] == 403
    evil = {"X-Harness-Token": s.browser.token, "Origin": "http://evil.example", "Content-Type": "application/json"}
    assert request(s.browser.host, "POST", "/api/changes/undo", good, evil)[0] == 403
    assert work(s, "a.txt").read_text(encoding="utf-8") == "ONE\n"
    assert s.ui.events_after(0, timeout=0) == log  # 一个事件都没发

    # 页面看到的会话已经不是现在开着的这一段：不做，先把最新的状态发过去
    after = last_id(s)
    status, answer = s.browser.post("/api/changes/undo", {**good, "conversation": MISSING_SET})
    assert (status, answer) == (409, {"error": "这一轮不在现在开着的这段对话里了。看一眼最新的内容再试。"})
    wait_event(s.browser, "state", after)
    act(s, "/api/new")  # 另一个标签页开了新对话：这个标签页还拿着旧的会话和那一轮
    assert s.browser.post("/api/changes/undo", good)[0] == 409
    assert work(s, "a.txt").read_text(encoding="utf-8") == "ONE\n" and "file_undo" not in s.browser.types()


def test_not_while_a_turn_is_running_or_a_channel_holds_the_conversation(rig):
    s = started(
        rig,
        *edits(replace(1, "a.txt", "one", "ONE")),
        reply(tool_use("toolu_2", "run_command", {"command": "true"}), stop_reason="tool_use"),
        reply(text("好了。")),
        files={"a.txt": "one\n"},
    )
    conversation_id = say(s, "改一下")["conversation"]
    set_id = last_line(s)["set"]
    s.browser.post("/api/message", {"text": "跑一下"})
    card = s.browser.wait_for("confirm")  # 命令在 edits 档也要问：这一轮卡在确认上，还没有结束

    for action in ("undo", "redo"):
        assert post(s, action, set_id) == (409, {"error": WebApp.BUSY})
    assert diff(s, set_id, 0)[0] == 200  # 看改了什么不用等：只是读留底
    assert work(s, "a.txt").read_text(encoding="utf-8") == "ONE\n"
    s.browser.post("/api/confirm", {"id": card["id"], "allow": True})
    s.browser.wait_for("turn_end")

    # 聊天通道正在这段会话里跑一轮：不做，也不把别人的认领放掉
    assert s.web.claims.take(conversation_id, "telegram:1") is None
    status, answer = post(s, "undo", set_id)
    assert status == 409 and "telegram" in answer["error"].lower(), answer
    assert s.web.claims.holder(conversation_id) == "telegram:1" and work(s, "a.txt").read_text(encoding="utf-8") == "ONE\n"
    s.web.claims.release(conversation_id, "telegram:1")
    changed(s, "undo", set_id)
    assert work(s, "a.txt").read_text(encoding="utf-8") == "one\n" and s.web.claims.holder(conversation_id) is None  # 做完放开


# ---- 冲突 ------------------------------------------------------------------------


def test_a_file_changed_after_the_turn_asks_first(rig):
    s = started(rig, *edits(replace(1, "a.txt", "one", "ONE")), files={"a.txt": "one\n"})
    say(s, "改一下")
    set_id = last_line(s)["set"]
    a = work(s, "a.txt")
    a.write_text("我自己又改了\n", encoding="utf-8", newline="")

    status, answer = post(s, "undo", set_id)
    assert status == 409 and answer["error"] == CONFIRM_FIRST and answer["blocked"] == []
    (ask,) = answer["asks"]
    assert {key: ask[key] for key in ("index", "path", "reasons")} == {"index": 0, "path": "a.txt", "reasons": ["changed"]}
    (why,) = ask["why"]
    assert why["key"] == ASKING["changed"] and why["text"] == ASKING["changed"] and "values" in why  # 带着原文：换语言时页面原地重译
    assert a.read_text(encoding="utf-8") == "我自己又改了\n" and "file_undo" not in s.browser.types()  # 什么都没写

    # 点"仍然撤回"：照做；之后重做放回去的是撤回时换下来的那个样子（你自己改的）
    changed(s, "undo", set_id, force=True)
    assert a.read_text(encoding="utf-8") == "one\n"
    changed(s, "redo", set_id)
    assert a.read_text(encoding="utf-8") == "我自己又改了\n"


def test_a_file_outside_the_workspace_asks_before_undoing(rig, tmp_path):
    outside = (tmp_path / "elsewhere" / "x.txt").resolve()  # 工作目录是 tmp_path/work（make_config）
    s = started(rig, *edits(create(1, str(outside), "外面\n")))
    s.browser.post("/api/message", {"text": "在外面建个文件"})
    card = s.browser.wait_for("confirm")  # 工作目录之外：edits 档也要问
    assert s.browser.post("/api/confirm", {"id": card["id"], "allow": True})[0] == 200
    s.browser.wait_for("turn_end")
    line = last_line(s)
    assert line["files"] == [{"path": str(outside), "kind": "created", "added": 1, "removed": 0, "place": "outside"}]  # 完整路径，标着在哪儿

    # 当初改它要你点头，撤回也一样：先说清楚是哪儿
    status, answer = post(s, "undo", line["set"])
    assert status == 409 and answer["error"] == CONFIRM_FIRST
    assert [(item["path"], item["reasons"], item["why"][0]["key"]) for item in answer["asks"]] == [(str(outside), ["outside"], ASKING["outside"])]
    assert outside.exists()
    changed(s, "undo", line["set"], force=True)
    assert not outside.exists() and not outside.parent.exists()


def test_a_later_turn_that_changed_the_same_file_is_named(rig):
    s = started(rig, *edits(replace(1, "a.txt", "one", "two")), *edits(replace(2, "a.txt", "two", "three")), files={"a.txt": "one\n"})
    say(s, "改成 two")
    first = last_line(s)["set"]
    say(s, "改成 three")
    second = last_line(s)["set"]
    assert first != second

    status, answer = post(s, "undo", first)
    assert status == 409 and answer["error"] == CONFIRM_FIRST
    (ask,) = answer["asks"]
    assert (ask["reasons"], ask["later"]) == (["later"], 1)
    assert ask["why"][0]["key"] == ASKING["later"] and ask["why"][0]["values"] == {"n": 1} and "1" in ask["why"][0]["text"]
    assert work(s, "a.txt").read_text(encoding="utf-8") == "three\n"
    changed(s, "undo", first, force=True)
    assert work(s, "a.txt").read_text(encoding="utf-8") == "one\n"  # 这一轮之前的样子：后面那一轮的改动也一起没了


# ---- 告诉模型 --------------------------------------------------------------------


def test_the_next_message_tells_the_model_once(rig):
    s = started(
        rig,
        *edits(replace(1, "a.txt", "one", "ONE"), create(2, "b.txt", "新的\n")),
        reply(text("知道了。")),
        reply(text("嗯。")),
        reply(text("又知道了。")),
        files={"a.txt": "one\n"},
    )
    say(s, "改一下")
    set_id = last_line(s)["set"]
    changed(s, "undo", set_id)
    assert len(s.api.requests) == 2  # 撤回本身不发请求

    say(s, "接着来")
    note = note_in(s.api.requests[2])
    assert note.startswith("<file_changes>") and note.endswith("</file_changes>")
    assert f"- a.txt：{SAID['undo']}" in note and f"- b.txt：{SAID['delete']}" in note
    assert s.api.requests[2]["messages"][-1]["content"][-1]["text"] == "接着来"

    say(s, "再来")  # 说过了就不再说；历史里那一条原样留着（只追加，不改前面的）
    assert note_in(s.api.requests[3]) == "" and dumps(s.api.requests[3]).count("<file_changes>") == 1

    changed(s, "redo", set_id, files=[1])
    say(s, "重做以后")
    note = note_in(s.api.requests[4])
    assert f"- b.txt：{SAID['redo']}" in note and "a.txt" not in note


@pytest.mark.parametrize("again", ["message", "retry"])
def test_a_note_on_a_message_that_failed_is_sent_again(rig, again):
    s = started(
        rig,
        *edits(replace(1, "a.txt", "one", "ONE")),
        failure(529, "overloaded_error", "Overloaded"),
        reply(text("知道了。")),
        reply(text("嗯。")),
        files={"a.txt": "one\n"},
    )
    say(s, "改一下")
    changed(s, "undo", last_line(s)["set"])
    say(s, "接着来")  # 模型答出第一步之前就失败了：这条消息从历史里撤回了，附在它前面的说明也没留住
    assert note_in(s.api.requests[2])

    if again == "message":
        say(s, "再试一次")
    else:
        turn(s, "retry", mark=4)  # 重新生成：同样的话再发一次
    assert note_in(s.api.requests[3]) == note_in(s.api.requests[2])
    say(s, "第三句")
    assert note_in(s.api.requests[4]) == ""


def test_the_note_goes_to_the_conversation_it_belongs_to(rig):
    s = started(rig, *edits(replace(1, "a.txt", "one", "ONE")), reply(text("另一段。")), reply(text("回来了。")), files={"a.txt": "one\n"})
    first = say(s, "改一下")["conversation"]
    changed(s, "undo", last_line(s)["set"])

    act(s, "/api/new")
    say(s, "另一段")
    assert note_in(s.api.requests[2]) == ""  # 别的会话的模型不用知道
    open_conversation(s, first)
    say(s, "回到这一段")
    assert "a.txt" in note_in(s.api.requests[3])


# ---- 重新打开、重启以后 ----------------------------------------------------------


def test_undo_survives_reopening_and_a_restart(rig):
    s = started(rig, *edits(replace(1, "a.txt", "one", "ONE"), create(2, "b.txt", "新的\n")), reply(text("另一段。")), files={"a.txt": "one\n"})
    conversation_id = say(s, "改一下")["conversation"]
    set_id = last_line(s)["set"]
    a, b = work(s, "a.txt"), work(s, "b.txt")
    line, (undo, _) = content(last_line(s)), changed(s, "undo", set_id, files=[1])
    assert not b.exists()

    def check(replayed: list[dict], state: dict) -> None:
        # 重放出来的有那一行和撤回的记录（页面照着它们推出 b.txt 是"已撤回"），状态里还剩一个没撤回
        assert [content(event) for event in replayed if event["type"] in ("file_changes", "file_undo")] == [line, content(undo)]
        assert kinds(replayed).index("file_undo") > kinds(replayed).index("file_changes")
        assert state["rewind"]["files"] == 1

    act(s, "/api/new")
    say(s, "另一段")
    check(*open_conversation(s, conversation_id))
    status, answer = post(s, "redo", set_id, files=[0])  # 第 0 个没撤回过：不用重做
    assert status == 409 and [item["reasons"] for item in answer["blocked"]] == [["applied"]]

    restarted = rig(reply(text("知道了。")))  # 同一个数据目录：相当于把程序关掉再打开
    check(*open_conversation(restarted, conversation_id))
    _, state = changed(restarted, "redo", set_id)
    assert b.read_text(encoding="utf-8") == "新的\n" and state["rewind"]["files"] == 2
    _, state = changed(restarted, "undo", set_id)  # 再撤回一次（第 2 次撤回 b.txt：留一份新的底）
    assert a.read_text(encoding="utf-8") == "one\n" and not b.exists() and state["rewind"]["files"] == 0
    saved = saved_file(restarted.config, conversation_id)
    assert kinds(saved["events"]).count("file_undo") == 3


# ---- 倒回最后一轮时一起撤回 -----------------------------------------------------


@pytest.mark.parametrize("action", ["retry", "edit"])
def test_rewinding_can_undo_the_files_first(rig, action):
    s = started(
        rig,
        *edits(replace(1, "a.txt", "one", "ONE"), create(2, "b.txt", "新的\n")),
        reply(text("这次不改了。")),
        reply(text("嗯。")),
        files={"a.txt": "one\n"},
    )
    state = say(s, "改一下")
    assert state["rewind"]["files"] == 2 and state["rewind"]["effects"]  # 改过文件：倒回去之前要先确认
    a, b = work(s, "a.txt"), work(s, "b.txt")
    body = {**target(state), "undo_files": True, **({"text": "别改了"} if action == "edit" else {})}

    # 没确认：先问，文件也不动
    status, answer = s.browser.post(f"/api/turn/{action}", body)
    assert status == 409 and answer["effects"] and "undo" not in answer
    assert a.read_text(encoding="utf-8") == "ONE\n" and b.exists()

    after = last_id(s)
    assert s.browser.post(f"/api/turn/{action}", {**body, "confirmed": True}) == (200, {"ok": True})
    # 回答之前就撤回好了：新的一轮开始之前，文件已经是这一轮之前的样子
    assert a.read_text(encoding="utf-8") == "one\n" and not b.exists()
    replayed, live = replay_and_turn(s.browser, after)
    assert texts(live) == ["这次不改了。"]
    # 这一轮连同它的那一行一起去掉了；一起撤回的不记 file_undo（那一轮已经不在对话里了）
    assert not {"file_changes", "file_undo"} & set(kinds(replayed) + kinds(live))
    saved = saved_file(s.config, state["conversation"])
    assert not {"file_changes", "file_undo"} & set(kinds(saved["events"]))
    assert latest_state(s.browser)["rewind"]["files"] == 0
    # 也不告诉模型：它本来就不知道那一轮改过什么
    if action == "retry":
        assert dumps(s.api.requests[2]) == dumps(s.api.requests[0])
    assert note_in(s.api.requests[2]) == ""
    say(s, "接着")
    assert note_in(s.api.requests[3]) == ""


def test_rewinding_without_undo_leaves_the_files(rig):
    s = started(rig, *edits(replace(1, "a.txt", "one", "ONE")), reply(text("新的回答。")), files={"a.txt": "one\n"})
    say(s, "改一下")
    _, live = turn(s, "retry", mark=0, confirmed=True)
    assert texts(live) == ["新的回答。"] and work(s, "a.txt").read_text(encoding="utf-8") == "ONE\n"  # 倒回去的只是对话


def test_a_conflict_stops_the_rewind_until_you_say_so(rig):
    s = started(rig, *edits(replace(1, "a.txt", "one", "ONE")), reply(text("新的回答。")), files={"a.txt": "one\n"})
    state = say(s, "改一下")
    a = work(s, "a.txt")
    a.write_text("我自己又改了\n", encoding="utf-8", newline="")
    requests, after, history = len(s.api.requests), last_id(s), list(s.app.agent.messages)

    status, answer = s.browser.post("/api/turn/retry", {**target(state), "confirmed": True, "undo_files": True})
    assert status == 409 and answer["error"] == CONFIRM_FIRST and answer["undo"]["blocked"] == []
    (ask,) = answer["undo"]["asks"]
    assert (ask["path"], ask["reasons"], ask["why"][0]["key"]) == ("a.txt", ["changed"], ASKING["changed"])
    # 什么都没做：文件没动，对话没倒回去，没发请求
    assert a.read_text(encoding="utf-8") == "我自己又改了\n" and s.app.agent.messages == history
    assert len(s.api.requests) == requests and "reset" not in kinds(s.ui.events_after(after, timeout=0))
    assert latest_state(s.browser)["rewind"]["files"] == 1

    _, live = turn(s, "retry", mark=0, confirmed=True, undo_files=True, undo_force=True)  # "仍然撤回并重新生成"
    assert texts(live) == ["新的回答。"] and a.read_text(encoding="utf-8") == "one\n"


def test_bad_undo_fields_in_a_rewind_are_refused(rig):
    s = started(rig, *edits(replace(1, "a.txt", "one", "ONE")), files={"a.txt": "one\n"})
    state = say(s, "改一下")
    for extra in ({"undo_files": "yes"}, {"undo_files": 1}, {"undo_force": "yes"}, {"undo_files": True, "undo_force": None}):
        assert s.browser.post("/api/turn/retry", {**target(state), "confirmed": True, **extra}) == (400, {"error": BAD}), extra
    assert work(s, "a.txt").read_text(encoding="utf-8") == "ONE\n"


def test_undoing_an_earlier_turn_survives_rewinding_the_last_one(rig):
    s = started(rig, *edits(create(1, "a.txt", "甲\n")), reply(text("聊点别的。")), reply(text("新的回答。")), reply(text("又一个回答。")))
    conversation_id = say(s, "建个文件")["conversation"]
    first = last_line(s)["set"]
    say(s, "聊点别的")
    changed(s, "undo", first)  # 第二轮之后才撤回第一轮的：这个 file_undo 记在第二轮的后面
    assert not work(s, "a.txt").exists()

    for action, extra in (("retry", {}), ("edit", {"text": "换个话题"})):
        replayed, live = turn(s, action, mark=4, **extra)  # 第二轮什么都没做，不用确认
        assert texts(live)
        # 撤回的记录没有跟着第二轮一起截掉：挪到了最后一条用户消息的前面（编辑时连那条消息一起去掉，它就在末尾）
        kept = [event for event in replayed if event["type"] in ("file_changes", "file_undo")]
        assert [(event["type"], event["set"]) for event in kept] == [("file_changes", first), ("file_undo", first)]
        assert kinds(replayed)[-2:] == (["file_undo", "user"] if action == "retry" else ["turn_end", "file_undo"])
        assert "file_undo" in kinds(saved_file(s.config, conversation_id)["events"])
    # 状态照样是"撤回过了"：再撤回说已经全部撤回，重做照样能做
    refused(post(s, "undo", first), NONE_LEFT)
    changed(s, "redo", first)
    assert work(s, "a.txt").read_text(encoding="utf-8") == "甲\n"


def test_a_turn_whose_files_cannot_be_undone_offers_no_undo_on_rewind(rig, monkeypatch):
    """这一轮改的文件当初都没留底（太大）：撤不回来，倒回去的确认卡片上就不放"同时撤回"（rewind.files 是 0）；
    页面照样带着 undo_files 来（比如状态还没更新），也只是照常倒回去，不会卡在"仍然撤回"上做不成。"""
    monkeypatch.setattr(journal_module, "UNDO_FILE_BYTES", 10)  # 超过 10 个字节的文件只改、不留底
    s = started(rig, *edits(replace(1, "big.txt", "one", "ONE")), reply(text("新的回答。")), files={"big.txt": "one\n" + "x" * 100 + "\n"})
    state = say(s, "改一下")
    assert last_line(s)["files"][0]["skipped"] == "big" and state["rewind"]["files"] == 0

    _, live = turn(s, "retry", mark=0, confirmed=True, undo_files=True)
    assert texts(live) == ["新的回答。"]
    assert work(s, "big.txt").read_text(encoding="utf-8").startswith("ONE\n")  # 撤不回来的就留着


# ---- 不能碰的文件 ----------------------------------------------------------------


def test_a_tampered_manifest_cannot_reach_the_env_file(rig):
    s = started(rig, *edits(replace(1, "a.txt", "one", "ONE")), reply(text("新的回答。")), files={"a.txt": "one\n"})
    state = say(s, "改一下")
    set_id = last_line(s)["set"]
    env = s.config.env_file
    env.write_text("FAKE_KEY=not-a-real-key\n", encoding="utf-8", newline="")  # 编造的密钥
    manifest = s.config.undo_dir / set_id / store.MANIFEST
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["files"][0]["path"] = str(env.resolve())
    manifest.write_text(json.dumps(data), encoding="utf-8", newline="")
    assert s.app.changes.store.manifest(set_id) is not None  # 改过以后格式照样对：拦住它的是"不能碰的文件"这一条

    for force in (False, True):
        status, answer = post(s, "undo", set_id, force=force)
        assert status == 409 and answer["error"] == "没有能撤回的文件。" and answer["asks"] == []
        (item,) = answer["blocked"]
        assert (item["path"], item["reasons"], item["why"][0]["key"]) == (".env", ["protected"], BLOCKING["protected"])
    # 倒回最后一轮时一起撤回也一样不写它：先在卡片上说清楚，对话也没倒回去
    status, answer = s.browser.post("/api/turn/retry", {**target(state), "confirmed": True, "undo_files": True})
    assert status == 409 and [item["reasons"] for item in answer["undo"]["blocked"]] == [["protected"]]
    assert len(s.api.requests) == 2
    assert env.read_text(encoding="utf-8") == "FAKE_KEY=not-a-real-key\n" and "file_undo" not in s.browser.types()
    assert work(s, "a.txt").read_text(encoding="utf-8") == "ONE\n"
    # 你点了"仍然撤回并重新生成"：做不了的跳过（一个都做不了也一样），照样倒回去；.env 一个字没动
    _, live = turn(s, "retry", mark=0, confirmed=True, undo_files=True, undo_force=True)
    assert texts(live) == ["新的回答。"]
    assert env.read_text(encoding="utf-8") == "FAKE_KEY=not-a-real-key\n"
