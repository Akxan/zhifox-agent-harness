"""各个工具和权限规则的测试，不涉及模型。"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from functools import partial
from types import SimpleNamespace

import anthropic.lib.tools._beta_builtin_memory_tool as sdk_memory
import pytest

from harness.config import load_env_file
from harness.permissions import MODES, Permissions
from harness.tools import files
from harness.tools.base import ToolError, check_input
from harness.tools.files import FileEditor
from harness.tools.memory import make_memory_tool
from harness.tools.shell import Shell, make_shell_tool
from harness.tools.todo import TodoStore, format_todos, make_todo_tools

FILE_TOOL = "str_replace_based_edit_tool"


# ---- 待办 -----------------------------------------------------------------


def test_todo_lifecycle(tmp_path):
    store = TodoStore(tmp_path / "data" / "todos.json")
    store.add("没有期限的事")
    store.add("后天的事", due="2026-11-22")
    store.add("明天的事", due="2026-11-21 09:00")

    # 有截止时间的排前面，按时间先后
    assert [item["title"] for item in store.items()] == ["明天的事", "后天的事", "没有期限的事"]

    store.update(2, done=True)
    assert [item["id"] for item in store.items("pending")] == [3, 1]
    assert [item["id"] for item in store.items("done")] == [2]
    assert store.items("done")[0]["completed_at"] is not None

    store.update(3, due="")  # 空字符串清除截止时间
    assert next(item for item in store.items("all") if item["id"] == 3)["due"] is None

    store.delete(1)
    assert [item["id"] for item in store.items("all")] == [2, 3]
    # 重新打开文件，数据还在；编号不会重复使用
    assert TodoStore(store.path).add("新的")["id"] == 4
    assert "□ #4 新的" in format_todos(store.items())


def test_todo_rejects_bad_input(tmp_path):
    store = TodoStore(tmp_path / "todos.json")
    with pytest.raises(ToolError, match="格式不对"):
        store.add("x", due="下周五")
    with pytest.raises(ToolError, match="没有编号为 7"):
        store.update(7, done=True)
    assert store.items("all") == []


def test_todo_tools_validate_arguments(tmp_path):
    tools = {tool.name: tool for tool in make_todo_tools(TodoStore(tmp_path / "todos.json"))}
    assert '"id": 1' in tools["todo_add"].run({"title": "写周报", "due": None})  # null 等于没填
    with pytest.raises(ToolError, match="缺少必填参数 title"):
        tools["todo_add"].run({})
    with pytest.raises(ToolError, match="没有 priority 这个参数"):
        tools["todo_add"].run({"title": "x", "priority": "high"})
    with pytest.raises(ToolError, match="类型应该是 integer"):
        tools["todo_update"].run({"id": "1", "done": True})
    with pytest.raises(ToolError, match="类型应该是 integer"):
        tools["todo_update"].run({"id": True, "done": True})  # 布尔值不能冒充整数
    with pytest.raises(ToolError, match="只能是"):
        tools["todo_list"].run({"status": "later"})
    with pytest.raises(ToolError, match="没有提供要修改的内容"):
        tools["todo_update"].run({"id": 1})


def test_check_input_rejects_non_objects():
    schema = {"properties": {}, "required": []}
    with pytest.raises(ToolError, match="INVALID_JSON"):
        check_input(schema, "not an object")


# ---- 文件 -----------------------------------------------------------------


@pytest.fixture
def editor(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    return FileEditor(workspace, tmp_path / "data" / "backups", protected=[workspace / ".env"])


def test_file_create_view_edit(editor):
    target = editor.workspace / "notes" / "a.txt"
    assert "已创建" in editor.run({"command": "create", "path": "notes/a.txt", "file_text": "第一行\n第二行\n第三行"})
    assert target.read_text(encoding="utf-8") == "第一行\n第二行\n第三行"

    assert editor.run({"command": "view", "path": "notes/a.txt"}) == "     1\t第一行\n     2\t第二行\n     3\t第三行"
    assert editor.run({"command": "view", "path": "notes/a.txt", "view_range": [2, -1]}) == (
        "     2\t第二行\n     3\t第三行"
    )

    editor.run({"command": "str_replace", "path": "notes/a.txt", "old_str": "第二行", "new_str": "改过的第二行"})
    editor.run({"command": "insert", "path": "notes/a.txt", "insert_line": 0, "insert_text": "标题"})
    assert target.read_text(encoding="utf-8") == "标题\n第一行\n改过的第二行\n第三行"

    listing = editor.run({"command": "view", "path": "."})
    assert "notes/" in listing


def test_file_overwrite_keeps_a_backup(editor):
    editor.run({"command": "create", "path": "a.txt", "file_text": "旧内容"})
    assert "已覆盖" in editor.run({"command": "create", "path": "a.txt", "file_text": "新内容"})
    (backup,) = editor.backup_dir.iterdir()
    assert backup.read_text(encoding="utf-8") == "旧内容"
    assert (editor.workspace / "a.txt").read_text(encoding="utf-8") == "新内容"


def test_backups_of_same_named_files_never_replace_each_other(editor, monkeypatch):
    # 备份的文件名是"时间（精确到微秒）-原来的文件名"。不同目录里的两个同名文件在同一刻被覆盖
    # （你的对话和一个定时任务同时在改），名字就撞上了：不另起一个名字的话，后一份备份会把前一份顶掉，旧版本就找不回来了
    moment = datetime(2026, 11, 21, 9, 0, 0, 123456)
    monkeypatch.setattr(files, "datetime", SimpleNamespace(now=lambda: moment))  # 让每一次覆盖都发生在同一微秒
    for path, versions in (("甲/note.txt", ["甲 1", "甲 2", "甲 3"]), ("乙/note.txt", ["乙 1", "乙 2"])):
        for content in versions:
            editor.run({"command": "create", "path": path, "file_text": content})

    backups = sorted(editor.backup_dir.iterdir())
    assert len(backups) == 3 and all(path.name.startswith("20261121-090000-123456") for path in backups)
    assert all(path.name.endswith("note.txt") for path in backups)  # 名字里还看得出原来是哪个文件
    assert sorted(path.read_text(encoding="utf-8") for path in backups) == ["乙 1", "甲 1", "甲 2"]
    assert (editor.workspace / "甲" / "note.txt").read_text(encoding="utf-8") == "甲 3"


def test_file_errors(editor):
    editor.run({"command": "create", "path": "a.txt", "file_text": "重复 重复"})
    with pytest.raises(ToolError, match="出现了 2 次"):
        editor.run({"command": "str_replace", "path": "a.txt", "old_str": "重复", "new_str": "x"})
    with pytest.raises(ToolError, match="没有在文件里找到"):
        editor.run({"command": "str_replace", "path": "a.txt", "old_str": "不存在", "new_str": "x"})
    with pytest.raises(ToolError, match="不存在"):
        editor.run({"command": "view", "path": "missing.txt"})
    with pytest.raises(ToolError, match="insert_line"):
        editor.run({"command": "insert", "path": "a.txt", "insert_line": 9, "insert_text": "x"})
    with pytest.raises(ToolError, match="不支持的命令"):
        editor.run({"command": "delete", "path": "a.txt"})
    with pytest.raises(ToolError, match="缺少 path"):
        editor.run({"command": "view"})
    (editor.workspace / "image.bin").write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(ToolError, match="不是文本文件"):
        editor.run({"command": "view", "path": "image.bin"})
    assert (editor.workspace / "a.txt").read_text(encoding="utf-8") == "重复 重复"


def test_file_protects_the_key_file(editor):
    (editor.workspace / ".env").write_text("ANTHROPIC_API_KEY=secret", encoding="utf-8")
    for tool_input in (
        {"command": "view", "path": ".env"},
        {"command": "view", "path": "sub/../.env"},  # 绕路也不行
        {"command": "create", "path": ".env", "file_text": "x"},
    ):
        with pytest.raises(ToolError, match="密钥"):
            editor.run(tool_input)
    assert (editor.workspace / ".env").read_text(encoding="utf-8") == "ANTHROPIC_API_KEY=secret"


def test_paths_are_resolved_before_checking(editor, tmp_path):
    assert editor.in_workspace(editor.resolve("a/b.txt"))
    assert not editor.in_workspace(editor.resolve("../outside.txt"))
    assert not editor.in_workspace(editor.resolve(str(tmp_path / "other" / "x.txt")))
    # 指向外面的符号链接，按它真正指向的位置算
    (tmp_path / "secret.txt").write_text("x", encoding="utf-8")
    (editor.workspace / "link.txt").symlink_to(tmp_path / "secret.txt")
    assert not editor.in_workspace(editor.resolve("link.txt"))


def run_together(jobs: list) -> None:
    """每件事放进一个线程，同时开始，等它们都做完。线程里抛出的错误让测试失败（不然只会打印出来，测试照样通过）。"""
    errors: list = []

    def guarded(job) -> None:
        try:
            job()
        except BaseException as exc:  # noqa: BLE001 —— 什么错误都要带回主线程
            errors.append(exc)

    threads = [threading.Thread(target=guarded, args=(job,), daemon=True) for job in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert errors == []


def slowly(read):
    """把一个"读文件"的函数包一层：读完多停 20 毫秒再交回去。

    str_replace 和 insert 都是"读出来、改一处、整个写回去"。多停的这一会儿，就是别的线程读到同一份旧内容的机会：
    没有排队的话，几个线程都基于同一份旧内容改，后写的把先写的冲掉，每次都会丢；排着队的话只是慢一点。
    """

    def read_slowly(*args):
        content = read(*args)
        time.sleep(0.02)
        return content

    return read_slowly


def test_edits_to_one_file_from_several_threads_all_land(editor, monkeypatch):
    # 你的对话和几个定时任务各有一个 FileEditor，可能同时在改同一个文件（比如都往一份日志里加一行）
    task_editor = FileEditor(editor.workspace, editor.backup_dir)
    editor.run({"command": "create", "path": "plan.txt", "file_text": "\n".join(f"槽{n}" for n in range(6))})
    monkeypatch.setattr(FileEditor, "_read", slowly(FileEditor._read))

    edits = [{"command": "str_replace", "path": "plan.txt", "old_str": f"槽{n}", "new_str": f"改好的第 {n} 格"} for n in range(6)]
    edits += [{"command": "insert", "path": "plan.txt", "insert_line": 0, "insert_text": f"新加的第 {n} 行"} for n in range(4)]
    run_together([partial((editor if n % 2 else task_editor).run, edit) for n, edit in enumerate(edits)])

    lines = (editor.workspace / "plan.txt").read_text(encoding="utf-8").split("\n")
    assert sorted(lines) == sorted([f"改好的第 {n} 格" for n in range(6)] + [f"新加的第 {n} 行" for n in range(4)])


# ---- 权限 -----------------------------------------------------------------


def test_permission_rules(editor, tmp_path):
    permissions = Permissions(editor)
    (editor.workspace / "a.txt").write_text("x", encoding="utf-8")

    def ask(tool: str, **tool_input) -> str | None:
        return permissions.review(tool, tool_input)

    # 直接执行
    assert ask("todo_add", title="x") is None
    assert ask("memory", command="create", path="/memories/a.md", file_text="x") is None
    assert ask(FILE_TOOL, command="view", path="a.txt") is None
    assert ask(FILE_TOOL, command="view", path=".") is None
    # 先问用户
    assert "ls -la" in ask("run_command", command="ls -la")
    assert "工作目录之外" in ask(FILE_TOOL, command="view", path=str(tmp_path / "outside.txt"))
    assert "工作目录之外" in ask(FILE_TOOL, command="view", path="../outside.txt")
    assert "创建文件：new.txt" in ask(FILE_TOOL, command="create", path="new.txt", file_text="内容")
    assert "覆盖已有文件：a.txt" in ask(FILE_TOOL, command="create", path="a.txt", file_text="内容")
    assert "修改文件：a.txt" in ask(FILE_TOOL, command="str_replace", path="a.txt", old_str="x", new_str="y")
    assert "第 0 行之后插入" in ask(FILE_TOOL, command="insert", path="a.txt", insert_line=0, insert_text="y")
    # 工作目录之外的写入，显示完整路径
    assert str(tmp_path / "outside.txt") in ask(
        FILE_TOOL, command="create", path=str(tmp_path / "outside.txt"), file_text="x"
    )


def test_the_assistants_own_data_is_not_read_without_asking(tmp_path):
    # 在项目目录里运行时，数据目录（保存的会话、设置、待办）就在工作目录里面。把它当成普通的工作目录文件的话，
    # 模型不用问就能把以前所有会话的内容读走，发给现在用的这一家厂商
    workspace = tmp_path / "work"
    data = workspace / "data"
    (data / "conversations").mkdir(parents=True)
    (data / "conversations" / "20261120-140000-aaaaaa.json").write_text("{}", encoding="utf-8")
    (workspace / "notes.txt").write_text("x", encoding="utf-8")
    (workspace / "database").mkdir()  # 名字相近但不是数据目录
    editor = FileEditor(workspace, data / "backups", protected=[workspace / ".env"])
    permissions = Permissions(editor, private=[data])

    def ask(**tool_input) -> str | None:
        return permissions.review(FILE_TOOL, tool_input)

    # 工作目录里别的文件照常直接读
    assert ask(command="view", path="notes.txt") is None and ask(command="view", path=".") is None
    assert ask(command="view", path="database") is None and ask(command="view", path="data.txt") is None
    # 数据目录本身、里面的文件、绕着弯写的路径、指进去的符号链接：一律先问
    (workspace / "shortcut").symlink_to(data / "conversations")
    for inside in (
        "data",
        "data/conversations",
        "data/conversations/20261120-140000-aaaaaa.json",
        "data/settings.json",
        "notes/../data/todos.json",
        str(data / "conversations" / "20261120-140000-aaaaaa.json"),
        "shortcut/20261120-140000-aaaaaa.json",
    ):
        question = ask(command="view", path=inside)
        assert question is not None and "助手自己保存的数据" in question, inside
    # 往里写本来就要问
    assert "创建文件" in ask(command="create", path="data/x.json", file_text="{}")
    # 记忆走的是它自己的工具，不受影响；没有配置数据目录时（单独使用 Permissions）和以前一样
    assert permissions.review("memory", {"command": "view", "path": "/memories"}) is None
    assert Permissions(editor).review(FILE_TOOL, {"command": "view", "path": "data/settings.json"}) is None


def test_auto_allow_modes_decide_what_is_done_without_asking(tmp_path):
    workspace = tmp_path / "work"
    data = workspace / "data"
    data.mkdir(parents=True)
    (workspace / "a.txt").write_text("x", encoding="utf-8")
    editor = FileEditor(workspace, data / "backups", protected=[workspace / ".env"])
    permissions = Permissions(editor, private=[data])
    outside = str(tmp_path / "outside.txt")

    # 每一行：一次工具调用，以及它在三种模式下是不是"不用问就可以做"（ask、edits、all）
    cases = [
        ((FILE_TOOL, {"command": "create", "path": "new.txt", "file_text": "内容"}), (False, True, True)),
        ((FILE_TOOL, {"command": "str_replace", "path": "a.txt", "old_str": "x", "new_str": "y"}), (False, True, True)),
        ((FILE_TOOL, {"command": "insert", "path": "a.txt", "insert_line": 0, "insert_text": "y"}), (False, True, True)),
        ((FILE_TOOL, {"command": "create", "path": "sub/../b.txt", "file_text": "x"}), (False, True, True)),
        # 下面这些，"改文件不用问"并不包括
        ((FILE_TOOL, {"command": "create", "path": outside, "file_text": "x"}), (False, False, True)),  # 工作目录之外
        ((FILE_TOOL, {"command": "create", "path": "../outside.txt", "file_text": "x"}), (False, False, True)),
        ((FILE_TOOL, {"command": "create", "path": "data/settings.json", "file_text": "{}"}), (False, False, True)),  # 数据目录
        ((FILE_TOOL, {"command": "view", "path": outside}), (False, False, True)),  # 读工作目录之外的文件
        ((FILE_TOOL, {"command": "view", "path": "data/settings.json"}), (False, False, True)),  # 读数据目录
        (("run_command", {"command": "rm -rf build"}), (False, False, True)),  # 执行命令
    ]
    assert permissions.mode == "ask"  # 一开始总是"每次都问"
    for mode_index, mode in enumerate(MODES):
        permissions.mode = mode
        for (tool, tool_input), expected in cases:
            # 模式只决定"问不问"；这次操作需不需要确认、确认时给用户看什么，和模式无关
            assert permissions.review(tool, tool_input) is not None, (mode, tool_input)
            assert permissions.auto_allows(tool, tool_input) is expected[mode_index], (mode, tool_input)
    assert list(MODES) == ["ask", "edits", "all"]

    # 本来就不用问的操作，问"能不能自动放行"没有意义，但也不该出错
    permissions.mode = "edits"
    assert permissions.review(FILE_TOOL, {"command": "view", "path": "a.txt"}) is None
    assert permissions.auto_allows(FILE_TOOL, {"command": "view", "path": "a.txt"}) is False
    assert permissions.auto_allows(FILE_TOOL, "不是字典") is False
    assert permissions.auto_allows(FILE_TOOL, {"command": "create"}) is False  # 没有路径


def test_the_key_file_stays_off_limits_in_every_mode(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / ".env").write_text("ANTHROPIC_API_KEY=secret", encoding="utf-8")
    editor = FileEditor(workspace, tmp_path / "data" / "backups", protected=[workspace / ".env"])
    permissions = Permissions(editor, private=[tmp_path / "data"])
    # 自动允许放宽的只是"问不问"。保存密钥的文件是工具自己拒绝的，哪种模式下都读不了、改不了
    for mode in MODES:
        permissions.mode = mode
        for tool_input in ({"command": "view", "path": ".env"}, {"command": "create", "path": ".env", "file_text": "x"}):
            with pytest.raises(ToolError, match="密钥"):
                editor.run(tool_input)
    assert (workspace / ".env").read_text(encoding="utf-8") == "ANTHROPIC_API_KEY=secret"


def test_long_previews_are_shortened(editor):
    question = Permissions(editor).review(
        FILE_TOOL, {"command": "create", "path": "big.txt", "file_text": "\n".join(f"行{n}" for n in range(100))}
    )
    assert "行11" in question and "行12" not in question
    assert "共 100 行" in question


# ---- 命令 -----------------------------------------------------------------


def test_shell_runs_in_workspace_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("HARMLESS", "visible")
    tool = make_shell_tool(Shell(tmp_path))

    assert tool.run({"command": "pwd"}) == str(tmp_path.resolve())
    assert tool.run({"command": 'echo "[$ANTHROPIC_API_KEY] [$HARMLESS]"'}) == "[] [visible]"
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-secret"  # 只是不传给子进程，自己的环境不受影响
    assert tool.run({"command": "echo out; echo err >&2; exit 3"}) == "out\nerr\n[退出码 3]"
    assert tool.run({"command": "true"}) == "（没有输出）"


def test_shell_timeout_kills_the_command(tmp_path):
    tool = make_shell_tool(Shell(tmp_path))
    result = tool.run({"command": "echo started; sleep 30; echo finished", "timeout_seconds": 1})
    assert "started" in result and "finished" not in result
    assert "超过 1 秒" in result


def test_shell_clips_long_output(tmp_path):
    result = make_shell_tool(Shell(tmp_path)).run({"command": "yes | head -n 50000"})
    assert len(result) < 21_000
    assert "中间省略了" in result


# ---- 记忆 -----------------------------------------------------------------


def test_memory_persists_as_plain_files(tmp_path):
    tool = make_memory_tool(tmp_path / "data")
    assert tool.spec == {"type": "memory_20250818", "name": "memory"}

    tool.run({"command": "create", "path": "/memories/user.md", "file_text": "用户喜欢简短的回答"})
    assert (tmp_path / "data" / "memories" / "user.md").read_text(encoding="utf-8") == "用户喜欢简短的回答"

    # 换一个新的工具实例（相当于重启程序），内容还在
    again = make_memory_tool(tmp_path / "data")
    assert "user.md" in again.run({"command": "view", "path": "/memories"})
    assert "用户喜欢简短的回答" in again.run({"command": "view", "path": "/memories/user.md"})

    again.run({"command": "str_replace", "path": "/memories/user.md", "old_str": "简短", "new_str": "详细"})
    assert "详细" in again.run({"command": "view", "path": "/memories/user.md"})


def test_memory_cannot_escape_its_directory(tmp_path):
    tool = make_memory_tool(tmp_path / "data")
    (tmp_path / "outside.txt").write_text("x", encoding="utf-8")
    for path in ("/memories/../../outside.txt", "/etc/passwd", str(tmp_path / "outside.txt")):
        with pytest.raises(ToolError):
            tool.run({"command": "view", "path": path})
    with pytest.raises(ToolError):
        tool.run({"command": "create", "path": "/memories/../escaped.txt", "file_text": "x"})
    assert not (tmp_path / "data" / "escaped.txt").exists()


def test_memory_edits_from_the_chat_and_a_task_at_once_all_land(tmp_path, monkeypatch):
    # 你的对话和一个定时任务各组装了一份工具（两个 registry，两个记忆工具），改的却是同一个文件夹。
    # SDK 的 insert 也是"读出来、插一行、整个写回去"，同时插的话只会留下最后写的那一份
    chat, task = make_memory_tool(tmp_path / "data"), make_memory_tool(tmp_path / "data")
    chat.run({"command": "create", "path": "/memories/notes.md", "file_text": "# 记住的事\n"})
    monkeypatch.setattr(sdk_memory, "_read_file_content", slowly(sdk_memory._read_file_content))

    run_together([
        partial((chat if n % 2 else task).run, {"command": "insert", "path": "/memories/notes.md", "insert_line": 1, "insert_text": f"第 {n} 条"})
        for n in range(8)
    ])

    lines = (tmp_path / "data" / "memories" / "notes.md").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# 记住的事"
    assert sorted(lines[1:]) == sorted(f"第 {n} 条" for n in range(8))


# ---- 配置 -----------------------------------------------------------------


def test_env_file_loading(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {"HARNESS_T_C": "from-shell"})  # 用一份假的环境变量，不影响真实环境
    env_file = tmp_path / ".env"
    env_file.write_text(
        '# 注释\nHARNESS_T_A=plain\nexport HARNESS_T_B="quoted value"\nHARNESS_T_C=from-file\nHARNESS_T_EMPTY=\n',
        encoding="utf-8",
    )
    load_env_file(env_file)
    load_env_file(tmp_path / "missing.env")  # 文件不存在也不报错

    assert os.environ == {
        "HARNESS_T_A": "plain",
        "HARNESS_T_B": "quoted value",
        "HARNESS_T_C": "from-shell",  # 已有的环境变量不被覆盖；留空的 HARNESS_T_EMPTY 等于没填
    }
