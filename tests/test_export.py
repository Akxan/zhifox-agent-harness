"""一键导出的测试：数据文件夹里的东西都在压缩包里，密钥一个都不在。"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime

from harness import export, i18n
from harness.export import NOTE, PLACEHOLDER, archive_name, mask, plan, write_archive

MOMENT = datetime(2026, 11, 20, 14, 3)
# 编造的、长得像真密钥的字符串。拼起来写，源文件里就不会出现一个"像密钥"的整串，免得被代码托管平台的密钥扫描误报
API_KEY = "sk-" + "ant-api03-" + "FAKE" * 6
GITHUB_TOKEN = "ghp" + "_" + "FAKE" * 9
SECRETS = [
    API_KEY,
    GITHUB_TOKEN,
    "hunter2-db-password",
    "header-secret-value",
    "query-secret-value",
    "flag-secret-value",
    "inline-secret-value",
    "nested-secret-value",
    "env-plain-value",
]

MCP = {
    "mcpServers": {
        "github": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github", "--token", "flag-secret-value", "--api-key=inline-secret-value", "--verbose"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": GITHUB_TOKEN, "LOG_LEVEL": "env-plain-value"},
            "enabled": True,
        },
        "remote": {
            "url": "https://user:hunter2-db-password@mcp.example.com/sse?api_key=query-secret-value&region=eu",
            "headers": {"Authorization": "Bearer header-secret-value", "X-Extra": "header-secret-value"},
        },
        "auth-helper": {  # 名字里有 auth：它的设置不该整个被遮掉，只遮里面的密钥
            "command": "uvx",
            "args": ["auth-helper"],
            "credentials": {"client": "public-client-id", "client_secret": "nested-secret-value"},
            "note": "key is " + API_KEY,
        },
    }
}


def make_data(root):
    """一个像样的数据文件夹：会话、记忆、待办、设置、账本、MCP 配置，再加上几样不该导出的。"""
    data = root / "data"
    for relative, content in {
        "conversations/20261120-1403-abc.json": '{"title": "你好"}',
        "memories/用户偏好.md": "# 用户偏好\n- 喜欢简短的回答\n",
        "memories/projects/zhifox.md": "# 智狐\n",
        "todos.json": "[]",
        "settings.json": '{"model": "claude-opus-5-5"}',
        "ledger.jsonl": '{"day": "2026-11-20"}\n',
        "media/photo.png": "PNG-ish",
        "mcp.json": json.dumps(MCP),
        # 下面这些不该出现在压缩包里
        ".lock": "",
        "settings.json.1a2b3c4d.tmp": "写到一半",
        ".env": f"ANTHROPIC_API_KEY={API_KEY}\n",
        "conversations/.env.local": f"OPENAI_API_KEY={API_KEY}\n",
        "old/prod.env": "SECRET=hunter2-db-password\n",
        NOTE: "上一次导出时放进来的说明",
    }.items():
        path = data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    outside = root / "outside"
    outside.mkdir()
    (outside / "id_rsa").write_text("PRIVATE KEY hunter2-db-password", encoding="utf-8")
    (data / "link-to-key").symlink_to(outside / "id_rsa")
    (data / "linked-folder").symlink_to(outside, target_is_directory=True)
    return data


def export_bytes(data, secrets=()) -> tuple[bytes, export.Plan]:
    out = io.BytesIO()
    work = write_archive(data, out, MOMENT, secrets)
    return out.getvalue(), work


def test_the_archive_holds_the_whole_data_folder_and_no_secrets(tmp_path):
    data = make_data(tmp_path)
    raw, work = export_bytes(data)
    archive = zipfile.ZipFile(io.BytesIO(raw))
    names = sorted(archive.namelist())
    assert names == sorted([
        "conversations/20261120-1403-abc.json",
        "memories/用户偏好.md",
        "memories/projects/zhifox.md",
        "todos.json",
        "settings.json",
        "ledger.jsonl",
        "media/photo.png",
        "mcp.json",
        NOTE,
    ])
    assert archive.testzip() is None
    assert archive.read("memories/用户偏好.md").decode() == "# 用户偏好\n- 喜欢简短的回答\n"
    assert archive.getinfo("media/photo.png").compress_type == zipfile.ZIP_STORED  # 图片本来就压缩过，原样存
    assert archive.getinfo("todos.json").compress_type == zipfile.ZIP_DEFLATED
    # 不管在哪个文件里、压没压缩，整个压缩包里都找不到任何一个密钥
    everything = b"".join(archive.read(name) for name in archive.namelist())
    for secret in SECRETS:
        assert secret.encode() not in everything, secret
    assert work.count == 8 and archive_name(MOMENT) == "zhifox-backup-20261120-1403.zip"
    skipped = dict(work.skipped)
    assert set(skipped) == {".env", "conversations/.env.local", "old/prod.env", "link-to-key", "linked-folder/"}


def test_mcp_secrets_are_replaced_and_everything_else_is_kept(tmp_path):
    data = make_data(tmp_path)
    raw, work = export_bytes(data)
    masked = json.loads(zipfile.ZipFile(io.BytesIO(raw)).read("mcp.json"))
    github, remote, helper = (masked["mcpServers"][name] for name in ("github", "remote", "auth-helper"))
    # 命令、开关、普通的参数原样留着；密钥换成占位符，名字留着，恢复时知道要填哪几项
    assert github["command"] == "npx" and github["enabled"] is True
    assert github["args"] == ["-y", "@modelcontextprotocol/server-github", "--token", PLACEHOLDER, "--api-key=" + PLACEHOLDER, "--verbose"]
    assert github["env"] == {"GITHUB_PERSONAL_ACCESS_TOKEN": PLACEHOLDER, "LOG_LEVEL": PLACEHOLDER}
    assert remote["headers"] == {"Authorization": PLACEHOLDER, "X-Extra": PLACEHOLDER}
    assert remote["url"] == f"https://user:{PLACEHOLDER}@mcp.example.com/sse?api_key={PLACEHOLDER}&region=eu"
    assert helper["command"] == "uvx" and helper["args"] == ["auth-helper"]
    assert helper["credentials"] == {"client": "public-client-id", "client_secret": PLACEHOLDER}
    assert helper["note"] == f"key is {PLACEHOLDER}"
    assert list(work.masked) == ["mcp.json"]
    # 原来的文件一点没动
    assert json.loads((data / "mcp.json").read_text(encoding="utf-8")) == MCP


def test_masking_rules():
    assert mask({"api_key": "abc", "token": 123, "password": None, "secret_enabled": True}) == {
        "api_key": PLACEHOLDER, "token": PLACEHOLDER, "password": None, "secret_enabled": True
    }
    assert mask({"tokens": ["a", "b"], "list": ["--password", "p", "--name", "n"]}) == {
        "tokens": [PLACEHOLDER, PLACEHOLDER], "list": ["--password", PLACEHOLDER, "--name", "n"]
    }
    assert mask("Authorization: bearer abcdefghijklmnop") == f"Authorization: bearer {PLACEHOLDER}"
    assert mask("plain words stay as they are") == "plain words stay as they are"
    assert mask({"environment": {"PATH": "/usr/bin"}}) == {"environment": {"PATH": PLACEHOLDER}}


def test_an_unreadable_mcp_config_is_left_out_entirely(tmp_path):
    data = make_data(tmp_path)
    (data / "mcp.json").write_text('{"mcpServers": {"x": {"env": {"KEY": "' + API_KEY + '"}}', encoding="utf-8")
    raw, work = export_bytes(data)
    archive = zipfile.ZipFile(io.BytesIO(raw))
    assert "mcp.json" not in archive.namelist()  # 读不懂就没法只遮住密钥：宁可不导出
    assert "mcp.json" in dict(work.skipped)
    assert "mcp.json" in archive.read(NOTE).decode()  # 说明里写着它没有导出、为什么


def test_the_configured_key_file_is_blocked_even_under_another_name(tmp_path):
    data = make_data(tmp_path)
    keys = data / "keys.txt"
    keys.write_text("ANTHROPIC_API_KEY=whatever-0000", encoding="utf-8")
    raw, work = export_bytes(data, secrets=[keys])
    assert "keys.txt" in dict(work.skipped)
    assert "keys.txt" not in zipfile.ZipFile(io.BytesIO(raw)).namelist()
    assert b"whatever-0000" not in raw


def test_the_note_explains_how_to_restore(tmp_path, monkeypatch):
    data = make_data(tmp_path)
    raw, _ = export_bytes(data)
    archive = zipfile.ZipFile(io.BytesIO(raw))
    note = archive.read(NOTE).decode()
    assert "2026-11-20 14:03" in note and str(data) in note
    assert "解压到数据文件夹里" in note and "Ctrl-C" in note
    assert PLACEHOLDER in note and "mcp.json" in note and "link-to-key" in note
    assert archive.getinfo(NOTE).external_attr >> 16 == 0o600
    # 上一次导出时放进来、又被解压回数据文件夹的那份说明，不会再导出一份（压缩包里只有这一次的）
    assert archive.namelist().count(NOTE) == 1 and "上一次导出时放进来的说明" not in note
    # 用界面的语言写
    monkeypatch.setattr(i18n, "current", "en")
    english = zipfile.ZipFile(io.BytesIO(export_bytes(data)[0])).read(NOTE).decode()
    assert "How to restore" in english and "link-to-key (" in english


def test_plan_reports_size_and_count_without_writing_anything(tmp_path):
    data = make_data(tmp_path)
    before = sorted(path.name for path in data.rglob("*"))
    work = plan(data)
    assert work.count == 8
    expected = sum((data / relative).stat().st_size for _, relative in work.files) + len(work.masked["mcp.json"])
    assert work.size == expected
    assert sorted(path.name for path in data.rglob("*")) == before
    assert plan(tmp_path / "no-such-folder").count == 0


def test_the_chat_bot_token_and_the_pairing_code_are_left_out(tmp_path):
    """channels.json 里有聊天机器人的 token（谁拿到它谁就能冒充这个机器人）和正在用的配对码：导出时都换成占位符。"""
    from harness.channels.store import ChannelStore

    data = tmp_path / "data"
    token = "555000111:" + "Q" * 35  # 编造的、样子像真的 token
    store = ChannelStore(data / "channels.json")
    store.connect("telegram", token, {"id": "555000111", "username": "backup_test_bot", "name": "Backup"})
    secret = store.start_pairing("telegram")["code"]
    store.remember_chat("telegram", "111", "20261121-0800-abc")
    assert (data / "channels.json.lock").exists()  # 改设置时用的文件锁就在旁边

    raw, work = export_bytes(data)
    archive = zipfile.ZipFile(io.BytesIO(raw))
    assert sorted(archive.namelist()) == sorted(["channels.json", NOTE])  # 锁文件不导出
    assert list(work.masked) == ["channels.json"]
    entry = json.loads(archive.read("channels.json"))["platforms"]["telegram"]
    assert entry["token"] == PLACEHOLDER and entry["pairing"]["secret"] == PLACEHOLDER
    # 别的设置照样留着，恢复以后只要重新填 token
    assert entry["bot"]["username"] == "backup_test_bot" and entry["approval"] == "ask" and entry["enabled"] is True
    assert entry["chats"]["111"]["conversation"] == "20261121-0800-abc"
    everything = b"".join(archive.read(name) for name in archive.namelist())
    assert token.encode() not in everything and b"Q" * 35 not in everything
    assert f'"{secret}"'.encode() not in everything
    assert "channels.json" in archive.read(NOTE).decode()  # 说明里写着哪个文件要重新填
    # 导出不改原来的文件
    assert ChannelStore(data / "channels.json").get("telegram")["token"] == token
