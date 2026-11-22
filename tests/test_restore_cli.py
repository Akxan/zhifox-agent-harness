"""终端里的 harness restore（harness/restore_cli.py）：先打印预览，输入 yes 才恢复；没有终端时必须带 --yes；
智狐开着（数据目录被占着）的时候不恢复，说清楚是谁占着、去网页上恢复。"""

from __future__ import annotations

import argparse
import io
import json

from test_restore import handmade, pack, snapshot, write

from harness import instance, restore_cli
from harness.cli import main
from harness.export import PLACEHOLDER


def args(archive, merge: bool = False, yes: bool = False) -> argparse.Namespace:
    return argparse.Namespace(archive=archive, merge=merge, yes=yes)


def prepared(tmp_path, make_config):
    """一份备份（里面的记忆是"备份里的"），和一个已经改过的数据文件夹（记忆是"现在的"、多了一段会话）。"""
    config = make_config()
    data = config.data_dir
    write(data, "memories/notes.md", "备份里的")
    write(data, "mcp.json", json.dumps({"mcpServers": {"gh": {"command": "x", "env": {"GITHUB_TOKEN": "real-token-value"}}}}))
    backup = pack(data, tmp_path / "backup.zip")
    write(data, "memories/notes.md", "现在的")
    write(data, "conversations/new.json", "{}")
    return config, backup


def test_it_asks_and_restores_only_on_yes(tmp_path, make_config):
    config, backup = prepared(tmp_path, make_config)
    before = snapshot(config.data_dir)
    out = io.StringIO()
    asked = []
    assert restore_cli.run(args(backup), config, out, ask=lambda prompt: asked.append(prompt) or "算了", interactive=True) == 1
    assert asked and "yes" in asked[0]
    assert snapshot(config.data_dir) == before  # 别的回答都是取消，什么都没改
    printed = out.getvalue()
    assert "导出时间：2026-11-20 09:30" in printed and "覆盖" in printed and "记忆" in printed and "已取消" in printed

    out = io.StringIO()
    assert restore_cli.run(args(backup), config, out, ask=lambda prompt: " YES ", interactive=True) == 0
    assert (config.data_dir / "memories" / "notes.md").read_text(encoding="utf-8") == "备份里的"
    assert not (config.data_dir / "conversations").exists()
    env = json.loads((config.data_dir / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["gh"]["env"]
    assert env["GITHUB_TOKEN"] == "real-token-value"  # 占位符没有盖掉现在的密钥
    printed = out.getvalue()
    assert "恢复好了" in printed and "restore-backups" in printed
    assert list((config.data_dir / "restore-backups").glob("zhifox-before-restore-*.zip"))


def test_without_a_terminal_it_needs_yes(tmp_path, make_config):
    config, backup = prepared(tmp_path, make_config)
    before = snapshot(config.data_dir)
    out = io.StringIO()
    never = lambda prompt: (_ for _ in ()).throw(AssertionError("没有终端时不该问"))
    assert restore_cli.run(args(backup), config, out, ask=never, interactive=False) == 1
    assert "--yes" in out.getvalue() and snapshot(config.data_dir) == before
    assert restore_cli.run(args(backup, yes=True), config, io.StringIO(), ask=never, interactive=False) == 0
    assert (config.data_dir / "memories" / "notes.md").read_text(encoding="utf-8") == "备份里的"


def test_ctrl_d_is_a_no(tmp_path, make_config):
    config, backup = prepared(tmp_path, make_config)

    def interrupted(prompt):
        raise EOFError

    assert restore_cli.run(args(backup), config, io.StringIO(), ask=interrupted, interactive=True) == 1
    assert (config.data_dir / "memories" / "notes.md").read_text(encoding="utf-8") == "现在的"


def test_merge_keeps_what_is_there(tmp_path, make_config):
    config, backup = prepared(tmp_path, make_config)
    out = io.StringIO()
    assert restore_cli.run(args(backup, merge=True, yes=True), config, out, interactive=False) == 0
    assert (config.data_dir / "memories" / "notes.md").read_text(encoding="utf-8") == "现在的"
    assert (config.data_dir / "conversations" / "new.json").exists()
    assert "合并" in out.getvalue()


def test_keys_to_fill_in_again_are_listed(tmp_path, make_config):
    """新电脑上没有现在的密钥：预览和恢复以后都说清楚哪几处要重新填、在哪儿填。"""
    config = make_config()
    old = tmp_path / "old"
    write(old, "mcp.json", json.dumps({"mcpServers": {"gh": {"command": "x", "env": {"GITHUB_TOKEN": "real-token-value"}}}}))
    backup = pack(old, tmp_path / "backup.zip")
    out = io.StringIO()
    assert restore_cli.run(args(backup, yes=True), config, out, interactive=False) == 0
    printed = out.getvalue()
    assert "mcpServers › gh › env › GITHUB_TOKEN" in printed and "扩展 → MCP" in printed
    assert "还有密钥要重新填：mcp.json" in printed
    assert json.loads((config.data_dir / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["gh"]["env"]["GITHUB_TOKEN"] == PLACEHOLDER


def test_it_refuses_while_zhifox_holds_the_data_folder(tmp_path, make_config, capsys):
    config, backup = prepared(tmp_path, make_config)
    before = snapshot(config.data_dir)
    lock = instance.claim(config.data_dir, "web")
    try:
        assert restore_cli.run(args(backup, yes=True), config, io.StringIO(), interactive=False) == 1
    finally:
        lock.close()
    error = capsys.readouterr().err
    assert "网页界面在运行" in error and "从备份恢复" in error
    assert snapshot(config.data_dir) == before


def test_while_it_restores_nobody_else_can_start(tmp_path, make_config):
    """恢复的这一会儿占着数据目录的锁：这时候开智狐，会被告知 harness restore 正在恢复。"""
    config, backup = prepared(tmp_path, make_config)
    seen = []

    def ask(prompt):  # 问你要不要恢复的这一刻，锁已经占着了
        seen.append((instance.in_use(config.data_dir), instance.holder(config.data_dir), instance.busy_lines(config.data_dir)))
        return "yes"

    assert restore_cli.run(args(backup), config, io.StringIO(), ask=ask, interactive=True) == 0
    ((held, holder, lines),) = seen
    assert held is True and holder.kind == "restore"
    assert "harness restore 正在从备份恢复" in lines[0]
    assert not instance.in_use(config.data_dir)  # 恢复完放开了


def test_a_missing_file_and_a_bad_archive(tmp_path, make_config, capsys):
    config, _ = prepared(tmp_path, make_config)
    before = snapshot(config.data_dir)
    assert restore_cli.run(args(tmp_path / "no-such.zip", yes=True), config, io.StringIO(), interactive=False) == 2
    bad = handmade(tmp_path / "bad.zip", [("settings.json", "{}"), ("../evil.md", "x")])
    assert restore_cli.run(args(bad, yes=True), config, io.StringIO(), interactive=False) == 1
    assert "不安全的路径" in capsys.readouterr().err
    assert snapshot(config.data_dir) == before
    assert not (config.data_dir / "restore-backups").exists()


def test_the_command_is_registered(tmp_path, make_config, monkeypatch, capsys):
    config, backup = prepared(tmp_path, make_config)
    monkeypatch.setenv("HARNESS_DATA_DIR", str(config.data_dir))
    assert main(["restore", str(backup)]) == 1  # 测试里没有终端，又没有 --yes：不恢复
    assert main(["restore", str(backup), "--merge", "--yes"]) == 0
    assert (config.data_dir / "memories" / "notes.md").read_text(encoding="utf-8") == "现在的"
    assert main(["restore", str(backup), "--yes"]) == 0
    assert (config.data_dir / "memories" / "notes.md").read_text(encoding="utf-8") == "备份里的"
    assert len(list((config.data_dir / "restore-backups").glob("zhifox-before-restore-*.zip"))) >= 1  # 同一秒里的两次会编上号
