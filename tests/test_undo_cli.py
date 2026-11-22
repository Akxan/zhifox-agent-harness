"""终端里的"改了 N 个文件"和 /undo（见 harness/undo/terminal.py）。

和 test_rewind_cli.py 一样：主循环 run_terminal 照常跑，输入换成写好的几行，模型是本地的假 API（不花钱），
工作目录是 tmp_path/work（make_config）。测试里不是交互式终端，所以"要继续吗"一律当作不继续、文件工具要改文件也没人点头，
先用 /auto edits 放开"改工作目录里的文件"；要测回答"继续"的情况，就把界面当作交互式的，并替用户答一句 y。
撤回本身的规矩在 test_undo.py 里测，网页那一侧在 test_undo_web.py 里测。
"""

from __future__ import annotations

import pytest
import test_rewind_cli
from fake_api import reply, text, tool_use

from harness.cli import HELP
from harness.undo import journal as journal_module
from harness.undo.notes import SAID

# 借用 test_rewind_cli 的 fixture：在终端里依次输入几行，跑完主循环，返回（假 API，屏幕上的输出，预先填好的文字）
terminal = test_rewind_cli.terminal

EDIT = "str_replace_based_edit_tool"
NOTHING = "这段对话里还没有可以撤回的文件改动。"


def create(n: int, path: str, body: str) -> dict:
    return tool_use(f"toolu_{n}", EDIT, {"command": "create", "path": path, "file_text": body})


def replace(n: int, path: str, old: str, new: str) -> dict:
    return tool_use(f"toolu_{n}", EDIT, {"command": "str_replace", "path": path, "old_str": old, "new_str": new})


def edits(*calls: dict, answer: str = "改好了。") -> list[dict]:
    """一轮：模型在一步里依次调用这几次工具，然后答一句。"""
    return [reply(*calls, stop_reason="tool_use"), reply(text(answer))]


@pytest.fixture
def work(tmp_path):
    """终端用的工作目录（和 make_config 里的是同一个），里面先放好 a.txt。"""
    folder = tmp_path / "work"
    folder.mkdir()
    (folder / "a.txt").write_text("one\n", encoding="utf-8")
    return folder


def note_in(request: dict) -> str:
    """请求里最后一条用户消息上附的"文件撤回过"的说明（没有是空的）。"""
    return "".join(block.get("text", "") for block in request["messages"][-1]["content"] if "<file_changes>" in block.get("text", ""))


def test_a_turn_that_changed_files_says_so_and_undo_asks_first(terminal, work):
    # 不是交互式终端：问"撤回吗"等于不撤回，什么都不动
    api, out, _ = terminal(["/auto edits", "改一下", "/undo"], *edits(replace(1, "a.txt", "one", "ONE"), create(2, "b.txt", "新的\n")))
    assert len(api.requests) == 2
    assert "改了 2 个文件：a.txt（修改 +1 −1）、b.txt（新增 +1 −0）。输入 /undo 可以撤回。" in out
    assert "这一轮还执行过命令" not in out
    # 先列出来要做什么，再问
    assert "撤回上一轮改的文件：" in out and "a.txt（恢复成那一轮之前的样子）" in out and "b.txt（删掉：这是那一轮新建的）" in out
    assert "b.txt（删掉：这是那一轮新建的）\n已取消。" in out and "已撤回" not in out
    assert (work / "a.txt").read_text(encoding="utf-8") == "ONE\n" and (work / "b.txt").read_text(encoding="utf-8") == "新的\n"


def test_agreeing_undoes_the_files_and_the_next_message_says_so(terminal, work):
    api, out, _ = terminal(
        ["改一下", "/undo", "接着聊", "再聊", "/undo"],
        *edits(replace(1, "a.txt", "one", "ONE"), create(2, "new/b.txt", "新的\n")),
        reply(text("知道了。")),
        reply(text("嗯。")),
        interactive=True,
    )
    assert (work / "a.txt").read_text(encoding="utf-8") == "one\n" and not (work / "new").exists()
    assert "已撤回 2 个文件。下一条消息会告诉助手这些文件变了。" in out
    # 下一条消息前面附一段说明（在用户的话前面），说过一次就不再说
    note = note_in(api.requests[2])
    assert f"- a.txt：{SAID['undo']}" in note and SAID["delete"] in note
    assert api.requests[2]["messages"][-1]["content"][-1]["text"] == "接着聊"
    assert note_in(api.requests[3]) == ""
    assert "这一轮的改动已经全部撤回了。" in out  # 最后那一次 /undo：没有可撤的了


def test_a_file_changed_after_the_edit_is_named_before_asking(terminal, work):
    # 同一轮里，文件工具改完以后，一条命令又改了它：撤回会把命令的改动一起冲掉，列出来的时候写明
    _, out, _ = terminal(
        ["/auto all", "改一下", "/undo"],
        *edits(replace(1, "a.txt", "one", "ONE"), tool_use("toolu_2", "run_command", {"command": "echo more >> a.txt"})),
        interactive=True,
    )
    assert "这一轮还执行过命令：命令改动的文件没法自动撤回。" in out
    assert "⚠ 这一轮之后它又被改过" in out and "已撤回 1 个文件。" in out
    assert (work / "a.txt").read_text(encoding="utf-8") == "one\n"


def test_undo_names_how_many_turns_ago(terminal, work):
    _, out, _ = terminal(["/auto edits", "改一下", "随便聊聊", "/undo"], *edits(replace(1, "a.txt", "one", "ONE")), reply(text("聊。")))
    assert "撤回 1 轮之前改的文件：" in out and "已取消。" in out
    assert (work / "a.txt").read_text(encoding="utf-8") == "ONE\n"


def test_nothing_to_undo(terminal, work):
    api, out, _ = terminal(["/undo", "问一", "/undo"], reply(text("答一")))
    assert len(api.requests) == 1
    assert out.count(NOTHING) == 2  # 还没说过话；说过一轮、但没改文件
    assert "改了" not in out


def test_files_that_cannot_be_undone_are_listed(terminal, work, monkeypatch):
    monkeypatch.setattr(journal_module, "UNDO_FILE_BYTES", 2)  # 超过 2 个字节的文件只改、不留底
    _, out, _ = terminal(["/auto edits", "改一下", "/undo"], *edits(replace(1, "a.txt", "one", "ONE")), interactive=True)
    assert "没有能撤回的文件：" in out and "✕ a.txt：" in out and "（不撤回）" in out
    assert "已撤回" not in out and (work / "a.txt").read_text(encoding="utf-8") == "ONE\n"


def test_help_lists_undo(terminal, work):
    _, out, _ = terminal(["/help"])
    assert "/undo" in HELP
    assert "/undo    撤回最近一轮改的文件" in out
