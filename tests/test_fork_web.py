"""从某一轮分叉成一段新对话：网页这一侧（POST /api/fork、状态里的 fork）。

和 test_rewind_web.py 一样：真实的 HTTP 服务器，测试代码扮演浏览器，模型是本地的假 API（不花钱）。
页面发请求时带着它看到的那一轮：会话的编号（conversation）、那一轮的 user 事件的编号（turn）、在哪儿分叉（at）。
要对得上的几样：原会话的文件一个字节都不变；新的一段换上了（reset、重放、状态），接着聊时发给模型的历史就是原来的前缀；
标题和来路；多媒体和搜索的索引；压缩进摘要的、没记位置的、正在进行一轮的不能分叉。截到哪的细节在 test_fork.py 里测。
"""

from __future__ import annotations

import json

import test_rewind_web
import test_webui
from fake_api import reply, text, thinking, tool_use
from sample_files import PNG_BYTES, image, text_file
from test_rewind_web import dumps, last_id, texts, wait_event
from test_webui import (
    latest_state,
    media_file,
    open_conversation,
    request,
    saved_file,
    say,
    send_files,
)

from harness.providers import compaction

serve = test_webui.serve
rig = test_rewind_web.rig
MISSING = "20200101-000000-abcdef"


def user_ids(s) -> list[int]:
    """页面上现在每一轮的 user 事件的编号（服务器日志里的，和页面拿到的一样）。"""
    return [event["id"] for event in s.ui.user_events()]


def body(s, at: str, index: int) -> dict:
    """页面点第 index 轮（从 0 数）上的"分叉"时发的请求。"""
    return {"conversation": latest_state(s.browser)["conversation"], "turn": user_ids(s)[index], "at": at}


def fork_at(s, at: str, index: int) -> tuple[dict, list[dict], dict]:
    """在第 index 轮上分叉，等页面换到新的一段。返回（回答，reset 之后重放的事件，重放完的那个状态）。"""
    after = last_id(s)
    status, answer = s.browser.post("/api/fork", body(s, at, index))
    assert status == 200, answer
    reset = wait_event(s.browser, "reset", after)
    state = wait_event(s.browser, "state", reset["id"])
    return answer, [event for event in s.browser.events if reset["id"] < event["id"] < state["id"]], state


def talk(s, words: str) -> dict:
    """发一条消息，等这一轮结束，返回结束时的状态。分叉以后事件流里重放过一遍旧的，所以按编号等（不能用 say）。"""
    after = last_id(s)
    assert s.browser.post("/api/message", {"text": words})[0] == 200
    end = wait_event(s.browser, "turn_end", after)
    return next(event for event in reversed(list(s.browser.events)) if event["type"] == "state" and event["id"] < end["id"])


def done(s, path: str, payload: dict) -> dict:
    """做一个马上就能完成的操作（删除一段会话……），返回它之后的状态。"""
    after = last_id(s)
    assert s.browser.post(path, payload)[0] == 200
    return wait_event(s.browser, "state", after)


def refused(s, payload: dict, status: int = 409) -> str:
    code, answer = s.browser.post("/api/fork", payload)
    assert code == status, answer
    return answer["error"]


def file_of(s, conversation_id: str):
    return s.config.conversations_dir / f"{conversation_id}.json"


def listed(state: dict) -> dict[str, dict]:
    return {item["id"]: item for item in state["conversations"]}


# ---- 在回答上分叉、在消息上分叉 --------------------------------------------------------


def test_forking_at_a_reply_opens_a_new_conversation_and_leaves_the_old_one_alone(rig):
    s = rig(reply(thinking("想", signature="sig-keep"), text("答一")), reply(text("答二")), reply(text("答三")), reply(text("新方向")))
    say(s, "问一")
    say(s, "问二")
    source = say(s, "问三")["conversation"]
    before = file_of(s, source).read_bytes()

    answer, replayed, state = fork_at(s, "reply", 1)

    fork_id = answer["conversation"]
    assert fork_id == state["conversation"] != source and (answer["draft"], answer["attachments"]) == ("", [])
    # 页面换到了新的一段：重放的是前两轮（第三轮不在），第一轮的运行记录跟着过来
    assert texts(replayed, "user") == ["问一", "问二"] and texts(replayed) == ["答一", "答二"]
    assert "trace" in [event["type"] for event in replayed]
    assert state["title"] == "问一（分叉）"
    assert state["fork"]["origin"] == {"id": source, "turn": 2, "at": "reply", "title": "问一", "changes": False, "exists": True}
    assert listed(state)[fork_id]["forked"] is True and "forked" not in listed(state)[source]
    # 新的一段存好了：messages 是原来那段的前缀（切在第三轮的开头），同一家同一个模型
    saved, original = saved_file(s.config, fork_id), saved_file(s.config, source)
    assert dumps(saved["messages"]) == dumps(original["messages"][:4]) and texts(saved["events"], "user") == ["问一", "问二"]
    assert (saved["provider"], saved["model"]) == (original["provider"], original["model"])
    assert saved["forked_from"] == {"id": source, "turn": 2, "at": "reply", "title": "问一", "changes": False}
    # 原来那段一个字节都没变
    assert file_of(s, source).read_bytes() == before
    # 最后一轮照样能重新生成：事件里记着的位置在新的一段里也对得上
    assert state["rewind"]["ok"] is True and state["rewind"]["mark"] == 2

    # 接着聊：发给模型的历史就是原来的前两轮，一字不差（第一轮思考的签名原样发回去），然后才是新的一条
    talk(s, "换个方向")
    sent = s.api.requests[-1]
    assert dumps(sent["messages"][:4]) == dumps(s.api.requests[2]["messages"][:4])
    assert sent["messages"][1]["content"][0]["signature"] == "sig-keep" and "答三" not in dumps(sent)
    assert file_of(s, source).read_bytes() == before
    # 搜索：前两轮的字两段都搜得到，第三轮的只在原来那段里，新说的只在新的一段里
    assert {item["id"] for item in s.app.store.search("问二")} == {source, fork_id}
    assert [item["id"] for item in s.app.store.search("答三")] == [source]
    assert [item["id"] for item in s.app.store.search("新方向")] == [fork_id]


def test_forking_at_a_message_hands_the_message_back(rig):
    s = rig(reply(text("答一")), reply(text("两个都看到了")))
    say(s, "问一")
    picture, notes = image(), text_file("笔记.md", "第一行\n第二行")
    assert send_files(s, picture, notes, text="看看这些")[0] == 200
    s.browser.wait_for("turn_end")
    source = latest_state(s.browser)["conversation"]
    before = file_of(s, source).read_bytes()

    answer, replayed, state = fork_at(s, "message", 1)

    # 截到第二轮之前：只剩第一轮；那条消息的文字和附件交回页面，放进输入框
    assert texts(replayed, "user") == ["问一"] and state["fork"]["origin"]["at"] == "message"
    assert answer["draft"] == "看看这些" and len(answer["attachments"]) == 2
    assert all(media_id.startswith(source + "/0/") for media_id in answer["attachments"])
    # 附件的编号指着原会话里的那两个：页面照着它取回原件（换到新的一段以后照样取得到）
    status, _, content = media_file(s, answer["attachments"][0])
    assert status == 200 and content == PNG_BYTES
    status, _, content = media_file(s, answer["attachments"][1])
    assert status == 200 and content.decode() == "第一行\n第二行"
    # 多媒体库：原会话的两个附件还在原会话里；新的一段截在它们之前，一个都没有
    assert saved_file(s.config, answer["conversation"])["events"][0]["text"] == "问一"
    assert s.app.store.media().hashes(answer["conversation"]) == set()
    assert len(s.app.store.media().hashes(source)) == 2
    assert file_of(s, source).read_bytes() == before


def test_forked_attachments_are_listed_in_the_media_library_for_both(rig):
    s = rig(reply(text("看到了")), reply(text("二")))
    assert send_files(s, image(), text="看图")[0] == 200
    s.browser.wait_for("turn_end")
    source = say(s, "问二")["conversation"]
    answer, _, _ = fork_at(s, "reply", 0)
    # 新的一段带着第一轮的那张图：多媒体库里两段会话各有一个，原件是同一份
    index = s.app.store.media()
    assert index.hashes(answer["conversation"]) == index.hashes(source) and len(index.hashes(source)) == 1
    # 删掉原会话，原件还留着：新的一段还在用它
    done(s, "/api/conversation/delete", {"id": source})
    (digest,) = index.hashes(answer["conversation"])
    assert s.app.media.find(digest) is not None


def test_forking_at_the_first_message_starts_empty_and_is_saved_after_its_first_turn(rig):
    s = rig(reply(text("答一")), reply(text("重来的回答")))
    source = say(s, "问一")["conversation"]
    answer, replayed, state = fork_at(s, "message", 0)
    fork_id = answer["conversation"]
    assert replayed == [] and answer["draft"] == "问一"
    # 和新对话一样先只在内存里：会话栏里列出来（带着分叉的标记），磁盘上还没有
    assert listed(state)[fork_id]["title"] == "问一（分叉）" and listed(state)[fork_id]["forked"] is True
    assert not file_of(s, fork_id).exists() and state["fork"]["origin"]["id"] == source
    talk(s, "换个问法")
    saved = saved_file(s.config, fork_id)
    assert saved["title"] == "问一（分叉）" and saved["forked_from"]["at"] == "message" and len(saved["messages"]) == 2
    assert len(s.api.requests[-1]["messages"]) == 1  # 发给模型的只有新说的这一句


def test_forking_a_fork_and_deleting_the_original(rig):
    s = rig(reply(text("答一")), reply(text("答二")))
    say(s, "问一")
    source = say(s, "问二")["conversation"]
    first, _, _ = fork_at(s, "reply", 0)
    second, _, state = fork_at(s, "reply", 0)  # 从分叉出来的那一段再分叉：标题不再叠一个"（分叉）"
    assert state["title"] == "问一（分叉）" and state["fork"]["origin"]["id"] == first["conversation"]
    # 原会话改了名：顶上那一行跟着写新的名字（文件里记着的还是分叉时的标题）
    origin = done(s, "/api/conversation/rename", {"id": first["conversation"], "title": "改过名的那一段"})["fork"]["origin"]
    assert origin["title"] == "改过名的那一段" and origin["exists"] is True
    assert saved_file(s.config, second["conversation"])["forked_from"]["title"] == "问一（分叉）"
    # 原会话删掉了：只剩分叉时记下的标题
    origin = done(s, "/api/conversation/delete", {"id": first["conversation"]})["fork"]["origin"]
    assert origin["title"] == "问一（分叉）"
    assert origin["id"] == first["conversation"] and origin["exists"] is False  # 原会话删掉了：页面只显示文字
    assert second["conversation"] != source


# ---- 不能分叉的 ---------------------------------------------------------------------


def test_bad_requests_are_refused(rig):
    s = rig(reply(text("答一")))
    say(s, "问一")
    good = body(s, "reply", 0)
    host, token = s.browser.host, s.browser.token
    assert request(host, "POST", "/api/fork", good)[0] == 403  # 没带口令
    assert request(host, "POST", "/api/fork", good, {"X-Harness-Token": token, "Origin": "http://evil.example"})[0] == 403
    for broken in ({**good, "at": "middle"}, {**good, "turn": True}, {**good, "turn": "1"}, {**good, "conversation": 3}, {"turn": good["turn"], "at": "reply"}):
        assert refused(s, broken, 400) == "参数不正确"
    # 编号对不上：另一段会话、这一段里没有的一轮
    assert "对话刚刚变过了" in refused(s, {**good, "conversation": MISSING})
    assert "对话刚刚变过了" in refused(s, {**good, "turn": good["turn"] + 1000})
    assert latest_state(s.browser)["conversation"] == good["conversation"]  # 什么都没换


def test_turns_folded_into_a_summary_cannot_be_forked(rig, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    s = rig(reply(text("一")))
    store = s.app.store
    old = store.new("deepseek", "deepseek-flash")
    old.title = "压缩过的"
    history = [{"role": "system", "content": "提示词"}]
    for number in "一二三":
        history += [{"role": "user", "content": f"第{number}轮"}, {"role": "assistant", "content": f"答{number}"}]
    folded = compaction.compacted(history, 5, "用户问过第一轮和第二轮。")
    old.messages, old.dropped = folded[1:], len(history) - len(folded)
    for number, mark in zip("一二三", (1, 3, 5), strict=True):
        old.events += [{"type": "user", "text": f"第{number}轮", "mark": mark}, {"type": "turn_start"}, {"type": "text", "text": f"答{number}"}, {"type": "turn_end"}]
    store.save(old)
    before = file_of(s, old.id).read_bytes()

    _, state = open_conversation(s, old.id)
    assert state["fork"]["floor"] == 5  # 比它早的两轮压缩进了摘要：页面把那两轮的按钮画成灰的
    for at in ("reply", "message"):
        assert "压缩进了摘要" in refused(s, body(s, at, 0))
    answer, replayed, state = fork_at(s, "reply", 2)
    saved = saved_file(s.config, answer["conversation"])
    assert saved["dropped"] == 2 and compaction.is_summary(saved["messages"][0]) and len(saved["messages"]) == 4
    assert texts(replayed, "user") == ["第一轮", "第二轮", "第三轮"]
    assert s.app.agent.backend.turn_at(5) and state["rewind"]["ok"] is True  # 位置照样对得上
    assert file_of(s, old.id).read_bytes() == before and s.other_api.requests == []


def test_a_turn_without_a_mark_cannot_be_forked(rig):
    # 定时任务自己跑的那一轮没有记位置；之后你在网页上接着聊的那一轮有。新的一段是普通的网页会话，不带任务的标记
    s = rig(reply(text("任务的回答")), reply(text("接着聊的回答")))
    store = s.app.store
    old = store.new("anthropic", "claude-opus-5-5")
    old.title, old.task = "每日安排", "t1"
    old.messages = [{"role": "user", "content": [{"type": "text", "text": "（任务的指令）"}]}, {"role": "assistant", "content": [{"type": "text", "text": "任务的回答"}]}]
    old.events = [{"type": "user", "text": "看看今天的安排"}, {"type": "turn_start"}, {"type": "text", "text": "任务的回答"}, {"type": "turn_end"}]
    store.save(old)
    open_conversation(s, old.id)
    for at in ("reply", "message"):
        assert "没有记下它在对话历史里的位置" in refused(s, body(s, at, 0))
    talk(s, "接着问")
    answer, _, state = fork_at(s, "reply", 1)
    saved = saved_file(s.config, answer["conversation"])
    assert "task" not in saved and "channel" not in saved and "task" not in listed(state)[answer["conversation"]]
    assert texts(saved["events"], "user") == ["看看今天的安排", "接着问"]


def test_no_fork_while_a_turn_is_running(rig):
    s = rig(reply(text("答一")), reply(tool_use("toolu_1", "run_command", {"command": "echo hi"}), stop_reason="tool_use"), reply(text("不执行了")))
    say(s, "问一")
    payload = body(s, "reply", 0)
    s.browser.post("/api/message", {"text": "执行一条命令"})
    confirm = s.browser.wait_for("confirm")
    assert "请先等当前这一轮对话结束" in refused(s, payload)  # 你的这一轮还没结束：先等它（换会话也是这样）
    assert s.browser.post("/api/confirm", {"id": confirm["id"], "allow": False}) == (200, {"ok": True})
    s.browser.wait_for("turn_end")
    fork_at(s, "reply", 0)


def test_a_conversation_held_by_a_chat_channel_can_still_be_forked(rig):
    # 分叉只是读：聊天通道正占着它也可以（不认领它，读的是磁盘上的样子）
    s = rig(reply(text("答一")))
    source = say(s, "问一")["conversation"]
    assert s.web.claims.take(source, "telegram:1") is None
    try:
        answer, _, _ = fork_at(s, "reply", 0)
        assert s.web.claims.holder(source) == "telegram:1" and answer["conversation"] != source
    finally:
        s.web.claims.release(source, "telegram:1")


def test_an_unsaved_conversation_is_not_forked(rig):
    s = rig(reply(text("答一")))
    say(s, "问一")
    payload = body(s, "reply", 0)
    s.web._unsaved = "磁盘满了"
    try:
        assert "没能保存" in refused(s, payload)
    finally:
        s.web._unsaved = ""


def test_file_changes_stay_with_the_original(rig):
    # 改文件的记录（file_changes、file_undo）不跟过来：撤回只在原会话里做（为什么见 harness/fork.py）
    s = rig(reply(text("一")))
    store = s.app.store
    old = store.new("anthropic", "claude-opus-5-5")
    old.title = "改过文件"
    old.messages = [
        {"role": "user", "content": [{"type": "text", "text": "建个文件"}]}, {"role": "assistant", "content": [{"type": "text", "text": "建好了"}]},
        {"role": "user", "content": [{"type": "text", "text": "再说"}]}, {"role": "assistant", "content": [{"type": "text", "text": "好"}]},
    ]  # fmt: skip
    old.events = [
        {"type": "user", "text": "建个文件", "mark": 0}, {"type": "turn_start"}, {"type": "text", "text": "建好了"},
        {"type": "file_changes", "set": "a" * 32, "files": [{"path": "a.txt", "kind": "created", "added": 1, "removed": 0}]}, {"type": "turn_end"},
        {"type": "file_undo", "set": "a" * 32, "action": "undo", "files": [{"index": 0, "done": "delete"}], "paths": ["a.txt"]},
        {"type": "user", "text": "再说", "mark": 2}, {"type": "turn_start"}, {"type": "text", "text": "好"}, {"type": "turn_end"},
    ]  # fmt: skip
    store.save(old)
    open_conversation(s, old.id)
    answer, replayed, state = fork_at(s, "reply", 1)
    kinds = [event["type"] for event in replayed]
    assert "file_changes" not in kinds and "file_undo" not in kinds and texts(replayed, "user") == ["建个文件", "再说"]
    assert state["fork"]["origin"]["changes"] is True
    assert "file_changes" not in json.dumps(saved_file(s.config, answer["conversation"]), ensure_ascii=False)
