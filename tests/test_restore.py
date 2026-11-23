"""从备份恢复的测试（harness/restore.py、restore_keys.py）：导出再恢复一圈内容不变，合并只加现在没有的，
做坏的压缩包一律拒绝、什么都不写，占位符绝不盖掉现在的真密钥，恢复之前先自动备份一份。

压缩包都是现造的：正常的用 export.write_archive（和"导出全部数据"是同一个函数），做坏的用 zipfile 手写，
需要的话再直接改压缩包里的字节（谎报大小、加密位、CRC），模拟一个故意做坏的文件。
"""

from __future__ import annotations

import io
import json
import os
import stat
import struct
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from conftest import symlinks_work
from modes import has_mode

from harness import export, restore, restore_check, restore_keys
from harness.channels.store import ChannelStore
from harness.commands.allowlist import CommandList
from harness.config import Config
from harness.endpoints.store import EndpointStore
from harness.export import MANIFEST, NOTE, PLACEHOLDER
from harness.restore import RestoreError
from harness.skills.store import SkillStore
from harness.tasks import TaskStore
from harness.tools.todo import TodoStore

MOMENT = datetime(2026, 11, 20, 9, 30)
# 编造的、长得像真密钥的字符串（拼起来写，源文件里没有一个完整的"像密钥"的整串，免得被密钥扫描误报）
GITHUB_TOKEN = "ghp" + "_" + "REAL" * 9
BOT_TOKEN = "555000111:" + "Q" * 35
GATEWAY_KEY = "sk-" + "gateway-" + "REAL" * 6
SECRETS = [GITHUB_TOKEN, BOT_TOKEN, GATEWAY_KEY, "flag-secret-value", "hunter2-db-password", "header-secret-value"]

MCP = {
    "mcpServers": {
        "github": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github", "--token", "flag-secret-value", "--verbose"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": GITHUB_TOKEN},
        },
        "remote": {
            "url": "https://user:hunter2-db-password@mcp.example.com/sse",
            "headers": {"Authorization": "Bearer header-secret-value"},
        },
    }
}


# ---- 准备 --------------------------------------------------------------------


def write(data: Path, relative: str, content: str | bytes) -> Path:
    path = data.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8", newline="")
    return path


def lived_in(tmp_path: Path) -> Path:
    """一个用过一阵子的数据文件夹：每一样东西都有，能用真的 store 写的就用真的写（格式和程序里一模一样）。"""
    data = tmp_path / "data"
    config = Config(effort="medium", max_tokens=1000, max_steps=5, workspace=tmp_path, data_dir=data, env_file=tmp_path / ".env", web=False)
    TodoStore(config.todo_file).add("交报告", "2026-11-28")
    TaskStore(config.tasks_file).add({"title": "喝水", "kind": "reminder", "prompt": "起来喝杯水", "schedule": {"type": "daily", "time": "09:00"}})
    skills = SkillStore(data)
    skills.create("weekly-report", "写每周的周报", "# 周报\n1. 先问重点\n")
    skills.create("old-notes", "以前的笔记", "旧的")
    skills.set_enabled("old-notes", False)  # 写出 skills.json
    write(data, "skills/weekly-report/scripts/run.sh", "#!/bin/sh\necho hi\n")
    if os.name != "nt":
        (data / "skills" / "weekly-report" / "scripts" / "run.sh").chmod(0o700)
    CommandList(config.commands_file).add("npm test")
    store = ChannelStore(config.channels_file)
    store.connect("telegram", {"token": BOT_TOKEN}, {"id": "555000111", "username": "restore_test_bot", "name": "Restore"})
    EndpointStore(config.endpoints_file).save({
        "name": "公司网关", "base_url": "https://llm.example.com/v1", "api_key": GATEWAY_KEY,
        "models": [{"id": "gpt-x", "vision": True, "tools": True, "pdf": False, "context": 128000}],
    })
    for relative, content in {
        "conversations/20261120-0900-aaaaaa.json": '{"id": "20261120-0900-aaaaaa", "title": "你好"}',
        "conversations/20261119-0800-bbbbbb.json": '{"id": "20261119-0800-bbbbbb", "title": "昨天"}',
        "memories/用户偏好.md": "# 用户偏好\n- 喜欢简短的回答\n",
        "memories/projects/zhifox.md": "# 智狐\n",
        "settings.json": '{"provider": "anthropic", "model": "claude-opus-5-5", "language": "zh"}',
        "prompt.md": "你是我的助手。\r\n",  # Windows 记事本写的换行：恢复以后一个字节都不能变
        "ledger.jsonl": '{"day": "2026-11-20", "cost": 0.12}\n',
        "agents/资料研究员.md": "---\nname: 资料研究员\ndescription: 查资料\n---\n先查再说\n",
        "mcp.json": json.dumps(MCP, ensure_ascii=False),
        "hooks.json": json.dumps({"hooks": {"fmt": {"event": "PostToolUse", "command": "python3", "args": ["fmt.py", "--token", "hook-secret-value"]}}}),
        "media/" + "c" * 64 + ".png": b"\x89PNG\r\n\x1a\n" + bytes(range(256)),
        "backups/turns/abc123/manifest.json": "{}",
        "backups/20261120-notes.txt": "改之前的样子",
        "browser/shots/" + "a" * 64 + ".jpg": b"\xff\xd8\xff\xe0",
        "browser/profile/Default/Cookies": b"session=very-secret-cookie-value",
        "desktop/shots/" + "b" * 64 + ".jpg": b"\xff\xd8\xff\xe1",
        "evals/20261120-0930/summary.json": '{"score": 1}',
        "input_history": "你好\n/tools\n",
        "logs/service.log": "旧机器上的日志\n",
    }.items():
        write(data, relative, content)
    return data


def snapshot(data: Path) -> dict[str, bytes]:
    """数据文件夹里的每一个文件（相对路径 → 内容）。恢复前的自动备份和锁（.lock、channels.json.lock）不算：
    它们本来就会多出来，导出也不带锁。"""
    found: dict[str, bytes] = {}
    if not data.exists():
        return found
    for path in sorted(data.rglob("*")):
        relative = path.relative_to(data).as_posix()
        if relative.startswith(("restore-backups", ".restore-")) or relative.endswith(".lock") or not path.is_file():
            continue
        found[relative] = path.read_bytes()
    return found


def pack(data: Path, out: Path, secrets=()) -> Path:
    with open(out, "wb") as handle:
        export.write_archive(data, handle, MOMENT, secrets)
    return out


def handmade(path: Path, entries: list, note: bool = True) -> Path:
    """手写一个压缩包。entries 是 [(名字或 ZipInfo, 内容)]；名字照原样写进去（ZipInfo 在 Windows 上会把 \\ 换成 /，这里绕开它）。"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if note:
            archive.writestr(NOTE, "智狐数据备份\n")
        for name, content in entries:
            if isinstance(name, zipfile.ZipInfo):
                archive.writestr(name, content)
                continue
            info = zipfile.ZipInfo("placeholder", (2026, 11, 20, 9, 30, 0))
            info.filename = name
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, content)
    return path


def patch_central(raw: bytes, name: str, offset: int, fmt: str, value: int) -> bytes:
    """改压缩包中央目录里某一项的一个字段（offset 从这一项的签名 PK\\1\\2 数起）。zipfile 读的大小、旗子、CRC 都从这里来。"""
    at = 0
    target = name.encode()
    while True:
        at = raw.index(b"PK\x01\x02", at)
        length = struct.unpack_from("<H", raw, at + 28)[0]
        if raw[at + 46 : at + 46 + length] == target:
            patched = bytearray(raw)
            struct.pack_into(fmt, patched, at + offset, value)
            return bytes(patched)
        at += 4


# ---- 一圈下来内容不变 ----------------------------------------------------------


def test_overwrite_into_an_empty_folder_gives_back_every_file_but_the_secrets(tmp_path):
    """换电脑：新的数据文件夹是空的，恢复以后每一个文件都和导出时一模一样（字节、修改时间、能不能执行）。
    存着密钥的配置文件例外：新电脑上没有现在的密钥可以沿用，写进去的是占位符，keys 里一处一处列出来。"""
    data = lived_in(tmp_path)
    before = snapshot(data)
    backup = pack(data, tmp_path / "backup.zip")
    fresh = tmp_path / "new-computer" / "data"
    done = restore.apply(restore.check(backup), fresh, "overwrite", (), datetime(2026, 11, 22, 10, 0))
    done.commit()
    assert done.backup is None  # 新的数据文件夹是空的：不用先备份
    after = snapshot(fresh)
    masked = {"mcp.json", "channels.json", "endpoints.json", "hooks.json"}
    left_out = {"browser/profile/Default/Cookies", "logs/service.log"}  # cookie 不导出；日志是那台电脑自己的，不恢复
    assert set(after) == set(before) - left_out
    for relative in set(before) - masked - left_out:
        assert after[relative] == before[relative], relative
    everything = b"".join(after.values())
    for secret in [*SECRETS, "hook-secret-value", "very-secret-cookie-value"]:
        assert secret.encode() not in everything, secret
    assert json.loads(after["mcp.json"]) == export.mask(MCP)  # 别的都在，密钥那几处是占位符
    missing = {item["file"]: item["missing"] for item in done.keys}
    assert "mcpServers › github › env › GITHUB_PERSONAL_ACCESS_TOKEN" in missing["mcp.json"]
    assert set(missing) == masked
    assert {item["state"] for item in done.keys} == {"new"} and all(item["home"] for item in done.keys)
    # 修改时间照着导出时的（记忆的列表按它排）；zip 里的时间精确到 2 秒
    original = (data / "memories" / "用户偏好.md").stat().st_mtime
    assert abs((fresh / "memories" / "用户偏好.md").stat().st_mtime - original) <= 2
    if os.name != "nt":  # Skill 里的脚本照样能执行，别的文件只有自己能读
        assert has_mode(fresh / "skills" / "weekly-report" / "scripts" / "run.sh", 0o700)
    assert has_mode(fresh / "memories" / "用户偏好.md", 0o600)
    assert not any(path.name.startswith(".restore-") for path in fresh.iterdir())  # 临时文件夹收拾干净了


def test_overwrite_over_changed_data_goes_back_exactly_and_keeps_the_real_secrets(tmp_path):
    """同一台电脑回到昨天的样子：改过、删过、新加的都换回备份里的；密钥现在就在，所以存着密钥的文件内容也一样
    （它们是填好密钥以后重新写的，缩进可能和原来不同，所以按解析出来的内容比）。"""
    data = lived_in(tmp_path)
    before = snapshot(data)
    backup = pack(data, tmp_path / "backup.zip")
    (data / "conversations" / "20261119-0800-bbbbbb.json").unlink()
    write(data, "conversations/20261122-1000-cccccc.json", "{}")
    write(data, "memories/用户偏好.md", "改过了")
    write(data, "todos.json", '{"next_id": 1, "items": []}')
    (data / "prompt.md").unlink()
    write(data, "logs/service.log", "这台电脑上的日志\n")
    archive = restore.check(backup)
    work = restore.plan(archive, data, "overwrite")
    actions = {step.part.id: step.action for step in work.steps}
    assert actions["conversations"] == "replace" and actions["prompt"] == "add" and actions["mcp"] == "replace"
    assert "logs" not in actions  # 日志不恢复，也不清空
    done = restore.apply(archive, data, "overwrite", (), datetime(2026, 11, 22, 10, 0))
    done.commit()
    after = snapshot(data)
    assert after.pop("logs/service.log") == "这台电脑上的日志\n".encode()
    before.pop("logs/service.log")
    for name in ("mcp.json", "channels.json", "endpoints.json", "hooks.json"):
        assert json.loads(after.pop(name)) == json.loads(before.pop(name)), name
    assert after == before
    assert all(item["state"] == "filled" and not item["missing"] for item in done.keys)


def test_parts_the_backup_does_not_have_are_cleared_but_secret_configs_stay(tmp_path):
    """覆盖：数据文件夹变成备份时的样子，备份里没有的那几样清空。存着密钥的配置文件例外：备份里没有就留着现在的。"""
    data = tmp_path / "data"
    write(data, "settings.json", "{}")
    backup = pack(data, tmp_path / "backup.zip")
    write(data, "agents/新的.md", "x")
    write(data, "mcp.json", json.dumps(MCP))
    write(data, "browser/profile/Cookies", "登录过的网站")
    done = restore.apply(restore.check(backup), data, "overwrite", (), MOMENT)
    done.commit()
    assert not (data / "agents").exists()
    assert json.loads((data / "mcp.json").read_text(encoding="utf-8")) == MCP
    assert (data / "browser" / "profile" / "Cookies").read_text(encoding="utf-8") == "登录过的网站"  # 不在白名单里的从来不碰
    steps = {step["id"]: step["action"] for step in done.summary()["steps"]}
    assert steps["agents"] == "clear" and steps["mcp"] == "keep"


# ---- 合并 ----------------------------------------------------------------------


def test_merge_adds_what_is_missing_and_keeps_what_is_there(tmp_path):
    """合并：会话按编号、记忆按文件名、Skill 按整个文件夹比，撞上了留着现在的；
    整份的文件（设置、待办）现在有就留着现在的，没有才用备份里的。"""
    old = tmp_path / "old"
    write(old, "conversations/a.json", "备份里的 a")
    write(old, "conversations/b.json", "备份里的 b")
    write(old, "memories/notes.md", "备份里的笔记")
    write(old, "memories/projects/new.md", "备份里新的")
    write(old, "skills/weekly-report/SKILL.md", "备份里的周报")
    write(old, "skills/weekly-report/extra.md", "备份里多的一个文件")
    write(old, "skills/pdf-tools/SKILL.md", "备份里的 pdf")
    write(old, "settings.json", '{"language": "en"}')
    write(old, "todos.json", '{"next_id": 2, "items": [{"id": 1, "title": "备份里的"}]}')
    backup = pack(old, tmp_path / "backup.zip")

    data = tmp_path / "data"
    write(data, "conversations/a.json", "现在的 a")
    write(data, "memories/notes.md", "现在的笔记")
    write(data, "skills/weekly-report/SKILL.md", "现在的周报")
    write(data, "settings.json", '{"language": "zh"}')
    archive = restore.check(backup)
    steps = {step["id"]: step for step in restore.plan(archive, data, "merge").summary()["steps"]}
    assert (steps["conversations"]["added"], steps["conversations"]["kept"]) == (1, 1)
    assert (steps["memories"]["added"], steps["memories"]["kept"]) == (1, 1)
    assert (steps["skills"]["added"], steps["skills"]["kept"]) == (1, 1)
    assert steps["settings"]["action"] == "keep" and steps["todos"]["action"] == "add"

    done = restore.apply(archive, data, "merge", (), MOMENT)
    done.commit()
    read = lambda relative: data.joinpath(*relative.split("/")).read_text(encoding="utf-8")
    assert read("conversations/a.json") == "现在的 a" and read("conversations/b.json") == "备份里的 b"
    assert read("memories/notes.md") == "现在的笔记" and read("memories/projects/new.md") == "备份里新的"
    assert read("skills/weekly-report/SKILL.md") == "现在的周报"
    assert not (data / "skills" / "weekly-report" / "extra.md").exists()  # 一个 Skill 不拆开和现在的拼在一起
    assert read("skills/pdf-tools/SKILL.md") == "备份里的 pdf"
    assert read("settings.json") == '{"language": "zh"}'
    assert "备份里的" in read("todos.json")
    assert done.backup is not None and done.backup.is_file()  # 合并之前也先备份


def test_merge_compares_names_the_way_the_disk_does(tmp_path):
    """macOS、Windows 上 Notes.md 和 notes.md 是同一个文件：磁盘说"有"就算撞上了，留着现在的，不会悄悄盖掉。"""
    old = tmp_path / "old"
    write(old, "memories/Notes.md", "备份里的")
    backup = pack(old, tmp_path / "backup.zip")
    data = tmp_path / "data"
    write(data, "memories/notes.md", "现在的")
    case_blind = os.path.exists(data / "memories" / "NOTES.md")
    done = restore.apply(restore.check(backup), data, "merge", (), MOMENT)
    done.commit()
    assert (data / "memories" / "notes.md").read_text(encoding="utf-8") == "现在的"
    step = next(step for step in done.summary()["steps"] if step["id"] == "memories")
    assert (step["added"], step["kept"]) == ((0, 1) if case_blind else (1, 0))


def test_merge_rolls_back_what_it_added(tmp_path):
    old = tmp_path / "old"
    write(old, "conversations/b.json", "b")
    write(old, "memories/projects/new.md", "新的")
    write(old, "skills/pdf-tools/SKILL.md", "pdf")
    backup = pack(old, tmp_path / "backup.zip")
    data = tmp_path / "data"
    write(data, "conversations/a.json", "a")
    before = snapshot(data)
    done = restore.apply(restore.check(backup), data, "merge", (), MOMENT)
    assert (data / "skills" / "pdf-tools").is_dir()
    done.rollback()
    assert snapshot(data) == before
    assert not (data / "memories").exists() and not (data / "skills").exists()


# ---- 做坏的压缩包 --------------------------------------------------------------


def symlink_entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, (2026, 11, 20, 9, 30, 0))
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    return info


def fifo_entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, (2026, 11, 20, 9, 30, 0))
    info.external_attr = (stat.S_IFIFO | 0o600) << 16
    return info


MALICIOUS = {
    "上一层": [("../evil.md", "x")],
    "绕回上一层": [("memories/../../evil.md", "x")],
    "绝对路径": [("/etc/passwd", "x")],
    "盘符": [("C:/Windows/evil.txt", "x")],
    "盘符相对": [("C:evil", "x")],
    "反斜杠": [("..\\..\\evil", "x")],
    "设备名": [("memories/con.txt", "x")],
    "数据流": [("memories/a.md:hidden", "x")],
    "短名字": [("memories/PROGRA~1/x.md", "x")],
    "尾巴上的点": [("memories/notes.md.", "x")],
    "符号链接": [(symlink_entry("memories/link"), "/Users/me/.ssh/id_rsa")],
    "管道": [(fifo_entry("memories/pipe"), "")],
    "密钥文件": [(".env", "ANTHROPIC_API_KEY=x")],
    "藏起来的密钥文件": [("memories/.env.local", "OPENAI_API_KEY=x")],
    "浏览器的配置文件夹": [("browser/profile/Default/Cookies", "session=x")],
    "网页口令": [(".web-token", "x")],
    "网页地址": [(".web-address", "http://127.0.0.1:8765/?token=x")],
    "锁": [(".lock", "")],
    "不认识的文件": [("foo.txt", "x")],
    "文件当文件夹": [("settings.json/x", "x")],
    "文件夹当文件": [("conversations", "x")],
    "浏览器下面别的东西": [("browser/other.txt", "x")],
    "多套了一层": [("data/settings.json", "{}")],
    "大小写撞名": [("memories/A.md", "1"), ("memories/a.md", "2")],
    "Unicode 写法撞名": [("memories/\u00e9.md", "1"), ("memories/e\u0301.md", "2")],
    "既是文件又是文件夹": [("memories/a", "1"), ("memories/a/b.md", "2")],
    "遮过密钥的配置读不懂": [("mcp.json", "这不是 JSON")],
    "遮过密钥的配置不是对象": [("channels.json", "[1, 2]")],
    "更新版本的格式": [(MANIFEST, json.dumps({"format": 99, "app": "ZhiFox"})), ("settings.json", "{}")],
    "什么都没有": [],
}


@pytest.mark.parametrize("name", list(MALICIOUS), ids=list(MALICIOUS))
def test_bad_archives_are_refused_before_anything_is_written(tmp_path, name):
    data = lived_in(tmp_path)
    before = snapshot(data)
    bad = handmade(tmp_path / "bad.zip", [("settings.json", "{}"), *MALICIOUS[name]] if name not in ("更新版本的格式", "什么都没有") else MALICIOUS[name])
    with pytest.raises(RestoreError) as caught:
        restore.check(bad)
    assert str(caught.value)  # 有一句给人看的原因
    assert snapshot(data) == before
    assert not (data / "restore-backups").exists()


def test_the_reason_names_the_kind_of_problem(tmp_path):
    def reason(entries) -> str:
        with pytest.raises(RestoreError) as caught:
            restore.check(handmade(tmp_path / "bad.zip", entries))
        return str(caught.value)

    assert "符号链接" in reason([(symlink_entry("memories/link"), "x")])
    assert "密钥" in reason([(".env", "x")]) and "密钥" in reason([("memories/.env.local", "x")])
    assert "cookie" in reason([("browser/profile/Cookies", "x")])
    assert "不认识" in reason([("foo.txt", "x")]) and "foo.txt" in reason([("foo.txt", "x")])
    assert "不安全的路径" in reason([("../x", "x")])
    assert "更新版本" in reason([(MANIFEST, json.dumps({"format": 99, "app": "ZhiFox"})), ("settings.json", "{}")])


def test_files_other_systems_slip_in_are_skipped_not_refused(tmp_path):
    """Finder 打包时的 __MACOSX/、.DS_Store，Windows 的 Thumbs.db：不算"不认识的"，也不写进数据文件夹。"""
    junk = handmade(tmp_path / "junk.zip", [("settings.json", "{}"), ("__MACOSX/._settings.json", "x"), (".DS_Store", "x"),
                                             ("memories/.DS_Store", "x"), ("media/Thumbs.db", "x"), ("memories/", "")])
    archive = restore.check(junk)
    assert [entry.name for entry in archive.entries] == ["settings.json"]


def test_not_a_zip_at_all(tmp_path):
    path = tmp_path / "x.zip"
    path.write_bytes(b"PK\x03\x04 not a whole archive")  # 开头像 zip，后面不是
    with pytest.raises(RestoreError, match="zip"):
        restore.check(path)
    with pytest.raises(RestoreError):
        restore.check(tmp_path / "no-such.zip")


@pytest.mark.parametrize("limit", ["RESTORE_MAX_TOTAL_BYTES", "RESTORE_MAX_FILE_BYTES", "RESTORE_MAX_FILES", "RESTORE_MAX_DEPTH"])
def test_bombs_and_huge_archives_are_refused(tmp_path, monkeypatch, limit):
    """压缩炸弹：几 KB 的压缩包写着解开有几个 GB。按压缩包里写的大小、项数、层数先拦下（上限在 config.RESTORE_*）。"""
    # 两个文件一共 3000 字节：一个 2000 超过单个的上限，一共超过总的上限；压缩包里一共 3 项，最深的那个 5 层
    monkeypatch.setattr(restore_check, limit, {"RESTORE_MAX_TOTAL_BYTES": 2500, "RESTORE_MAX_FILE_BYTES": 1500, "RESTORE_MAX_FILES": 2, "RESTORE_MAX_DEPTH": 3}[limit])
    bomb = handmade(tmp_path / "bomb.zip", [("memories/a/b/c/zeros.md", "0" * 2000), ("memories/more.md", "0" * 1000)])
    assert bomb.stat().st_size < 1500  # 压缩以后很小，解开以后超了
    with pytest.raises(RestoreError):
        restore.check(bomb)


def test_a_lying_size_is_caught_while_unpacking_and_everything_goes_back(tmp_path):
    """压缩包里写的大小比实际的小（想骗过上面的上限）：解的时候按实际读到的核对，对不上就算损坏，换回原样。"""
    data = lived_in(tmp_path)
    before = snapshot(data)
    raw = pack(data, tmp_path / "backup.zip").read_bytes()
    lying = tmp_path / "lying.zip"
    lying.write_bytes(patch_central(raw, "memories/用户偏好.md", 24, "<I", 3))  # 中央目录里"解开以后的大小"改成 3 个字节
    archive = restore.check(lying)
    assert next(entry for entry in archive.entries if entry.name == "memories/用户偏好.md").size == 3
    with pytest.raises(RestoreError):
        restore.apply(archive, data, "overwrite", (), MOMENT)
    assert snapshot(data) == before
    assert not any(path.name.startswith(".restore-") for path in data.iterdir())


def test_a_damaged_entry_rolls_everything_back(tmp_path):
    data = lived_in(tmp_path)
    before = snapshot(data)
    raw = pack(data, tmp_path / "backup.zip").read_bytes()
    damaged = tmp_path / "damaged.zip"
    damaged.write_bytes(patch_central(raw, "agents/资料研究员.md", 16, "<I", 0xDEADBEEF))  # CRC 对不上
    with pytest.raises(RestoreError, match="资料研究员"):
        restore.apply(restore.check(damaged), data, "overwrite", (), MOMENT)
    assert snapshot(data) == before


def test_encrypted_entries_and_unknown_compression_are_refused(tmp_path):
    raw = handmade(tmp_path / "plain.zip", [("settings.json", "{}")]).read_bytes()
    for offset, value, words in ((8, 0x1, "加密"), (10, 99, "压缩方法")):
        bad = tmp_path / "bad.zip"
        bad.write_bytes(patch_central(raw, "settings.json", offset, "<H", value))
        with pytest.raises(RestoreError, match=words):
            restore.check(bad)


# ---- 中途出错 --------------------------------------------------------------------


@pytest.mark.parametrize("fail_at", [1, 4, 9], ids=["第一步", "中间", "后面"])
def test_a_failure_while_swapping_puts_everything_back(tmp_path, monkeypatch, fail_at):
    """一样一样换上去的时候，第 N 次改名失败（Windows 上文件被别的程序开着就会这样）：换过的换回去，数据文件夹原样不动。"""
    data = lived_in(tmp_path)
    backup = pack(data, tmp_path / "backup.zip")
    write(data, "memories/用户偏好.md", "改过了")
    write(data, "conversations/20261122-1000-cccccc.json", "{}")
    before = snapshot(data)
    real = os.rename
    calls = {"n": 0}

    def flaky(source, target):
        calls["n"] += 1
        if calls["n"] == fail_at:
            raise PermissionError(13, "被别的程序开着")
        return real(source, target)

    monkeypatch.setattr(restore.os, "rename", flaky)
    with pytest.raises(RestoreError, match="换回原来的样子"):
        restore.apply(restore.check(backup), data, "overwrite", (), MOMENT)
    monkeypatch.setattr(restore.os, "rename", real)
    assert snapshot(data) == before
    assert not any(path.name.startswith(".restore-") for path in data.iterdir())


def test_rollback_after_a_finished_restore_puts_the_old_data_back(tmp_path):
    """恢复好了，可是照着新的数据开不起来（网页上组装新的程序失败）：rollback 换回原来的样子。"""
    data = lived_in(tmp_path)
    backup = pack(data, tmp_path / "backup.zip")
    write(data, "memories/新的.md", "现在才有")
    (data / "todos.json").unlink()
    before = snapshot(data)
    done = restore.apply(restore.check(backup), data, "overwrite", (), MOMENT)
    assert not (data / "memories" / "新的.md").exists()
    done.rollback()
    assert snapshot(data) == before
    assert not done.staging.exists()


def test_links_in_the_way_are_refused(tmp_path, symlinks):
    """要换掉、要写进去的地方是链接（替身）：写进去就到了数据文件夹外面，所以拒绝，外面的东西一个字节都不动。"""
    data = tmp_path / "data"
    write(data, "settings.json", "{}")
    write(data, "memories/notes.md", "备份里的")
    backup = pack(data, tmp_path / "backup.zip")
    outside = tmp_path / "outside"
    write(outside, "private.md", "外面的")
    import shutil

    shutil.rmtree(data / "memories")
    (data / "memories").symlink_to(outside, target_is_directory=True)
    for mode in ("overwrite", "merge"):
        with pytest.raises(RestoreError, match="链接"):
            restore.apply(restore.check(backup), data, mode, (), MOMENT)
    assert sorted(path.name for path in outside.iterdir()) == ["private.md"]
    assert (data / "memories").is_symlink()


# ---- 密钥 ----------------------------------------------------------------------


def test_placeholders_never_overwrite_real_secrets(tmp_path):
    """备份里遮过的 mcp.json 恢复到现在有真密钥的电脑上：同一处留着现在的（环境变量、--token 后面的、请求头、网址里的密码），
    现在没有的那个服务器照样写进去、列在 missing 里，现在有、备份里没有的那个列在 dropped 里。"""
    old = tmp_path / "old"
    backup_mcp = json.loads(json.dumps(MCP))
    backup_mcp["mcpServers"]["slack"] = {"command": "slack-mcp", "env": {"SLACK_TOKEN": "xoxb-old-secret-value"}}
    write(old, "mcp.json", json.dumps(backup_mcp))
    backup = pack(old, tmp_path / "backup.zip")
    assert b"flag-secret-value" not in backup.read_bytes()

    data = tmp_path / "data"
    current = json.loads(json.dumps(MCP))
    current["mcpServers"]["github"]["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] = "ghp_" + "NOW" * 12
    current["mcpServers"]["github"]["args"][3] = "flag-now-value"
    current["mcpServers"]["only-here"] = {"command": "x", "env": {"ONLY_KEY": "only-here-secret"}}
    write(data, "mcp.json", json.dumps(current))
    done = restore.apply(restore.check(backup), data, "overwrite", (), MOMENT)
    done.commit()
    result = json.loads((data / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert result["github"]["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == "ghp_" + "NOW" * 12
    assert result["github"]["args"][3] == "flag-now-value"
    assert result["remote"] == MCP["mcpServers"]["remote"]  # 网址里的密码、Bearer 后面的，按样子对上了
    assert result["slack"]["env"]["SLACK_TOKEN"] == PLACEHOLDER
    assert "only-here" not in result
    (report,) = done.keys
    assert report["file"] == "mcp.json" and report["state"] == "filled" and report["kept"] == 4
    assert report["missing"] == ["mcpServers › slack › env › SLACK_TOKEN"]
    assert report["dropped"] == ["mcpServers › only-here › env › ONLY_KEY"]


def test_endpoints_are_matched_by_id_and_channels_by_platform(tmp_path):
    """endpoints.json 是一串带 id 的接口：顺序换了也按 id 对，密钥不会填到别的接口上。channels.json 按平台对。"""
    old = tmp_path / "old"
    endpoints = {"version": 1, "endpoints": [
        {"id": "a", "name": "A", "base_url": "https://a.example.com/v1", "api_key": "sk-old-a-secret", "models": []},
        {"id": "b", "name": "B", "base_url": "https://b.example.com/v1", "api_key": "sk-old-b-secret", "models": []},
    ]}
    write(old, "endpoints.json", json.dumps(endpoints))
    ChannelStore(old / "channels.json").connect("telegram", {"token": "111:" + "O" * 35}, {"id": "111", "username": "old_bot", "name": "Old"})
    backup = pack(old, tmp_path / "backup.zip")

    data = tmp_path / "data"
    now = {"version": 1, "endpoints": [dict(endpoints["endpoints"][1], api_key="key-for-b"), dict(endpoints["endpoints"][0], api_key="key-for-a")]}
    write(data, "endpoints.json", json.dumps(now))
    ChannelStore(data / "channels.json").connect("telegram", {"token": BOT_TOKEN}, {"id": "555", "username": "now_bot", "name": "Now"})
    done = restore.apply(restore.check(backup), data, "overwrite", (), MOMENT)
    done.commit()
    restored = {item["id"]: item["api_key"] for item in json.loads((data / "endpoints.json").read_text(encoding="utf-8"))["endpoints"]}
    assert restored == {"a": "key-for-a", "b": "key-for-b"}
    entry = ChannelStore(data / "channels.json").get("telegram")
    assert entry["credentials"]["token"] == BOT_TOKEN and entry["bot"]["username"] == "old_bot"  # 密钥是现在的，别的是备份里的


def test_an_unreadable_current_config_is_left_alone(tmp_path):
    """现在的 mcp.json 读不懂（手改坏了）：不动它。覆盖了就再也找不回里面的密钥（恢复前的自动备份也只能跳过它）。"""
    old = tmp_path / "old"
    write(old, "mcp.json", json.dumps(MCP))
    backup = pack(old, tmp_path / "backup.zip")
    data = tmp_path / "data"
    broken = '{"mcpServers": {"github": {"env": {"TOKEN": "' + GITHUB_TOKEN + '"'
    write(data, "mcp.json", broken)
    done = restore.apply(restore.check(backup), data, "overwrite", (), MOMENT)
    done.commit()
    assert (data / "mcp.json").read_text(encoding="utf-8") == broken
    assert done.keys == [{"file": "mcp.json", "home": restore.KEY_HOMES["mcp.json"], "state": "broken", "kept": 0, "missing": [], "dropped": []}]


def test_merge_keeps_the_current_secret_config_and_fills_in_a_missing_one(tmp_path):
    old = tmp_path / "old"
    write(old, "mcp.json", json.dumps(MCP))
    write(old, "hooks.json", json.dumps({"hooks": {"fmt": {"event": "Stop", "command": "python3", "args": ["--token", "hook-secret-value"]}}}))
    backup = pack(old, tmp_path / "backup.zip")
    data = tmp_path / "data"
    write(data, "mcp.json", json.dumps({"mcpServers": {}}))
    done = restore.apply(restore.check(backup), data, "merge", (), MOMENT)
    done.commit()
    assert json.loads((data / "mcp.json").read_text(encoding="utf-8")) == {"mcpServers": {}}
    hooks = json.loads((data / "hooks.json").read_text(encoding="utf-8"))
    assert hooks["hooks"]["fmt"]["args"] == ["--token", PLACEHOLDER]
    assert [(item["file"], item["state"], item["missing"]) for item in done.keys] == [("hooks.json", "new", ["hooks › fmt › args › #2"])]


def test_fill_keeps_a_value_only_where_it_is_the_same_place():
    backup = {"args": ["--token", PLACEHOLDER, "--verbose"], "env": {"KEY": PLACEHOLDER}, "port": PLACEHOLDER}
    value, found = restore_keys.fill(backup, {"args": ["--token", "now", "--verbose"], "env": {"KEY": "k"}, "port": 8080})
    assert value == {"args": ["--token", "now", "--verbose"], "env": {"KEY": "k"}, "port": 8080}  # 遮的时候数字也换成了占位符
    assert sorted(found.kept) == [("args", "#2"), ("env", "KEY"), ("port",)] and found.missing == found.dropped == []
    # 参数不一样长、或者占位符以外的那几项不一样：不是同一串，不沿用（不然会把 --token 当成密钥填进去）
    for args in (["--verbose", "--token", "now"], ["--token", "now"], ["--token", "now", "--quiet"]):
        value, found = restore_keys.fill({"args": ["--token", PLACEHOLDER, "--verbose"]}, {"args": args})
        assert value == {"args": ["--token", PLACEHOLDER, "--verbose"]} and found.missing == [("args", "#2")], args


def test_fill_matches_partly_masked_values_by_their_shape():
    template = f"https://user:{PLACEHOLDER}@host/sse"
    assert restore_keys.fill({"url": template}, {"url": "https://user:pw@host/sse"})[0] == {"url": "https://user:pw@host/sse"}
    value, found = restore_keys.fill({"url": template}, {"url": "https://user:pw@other/sse"})  # 换了主机：不是同一个
    assert value == {"url": template} and found.missing == [("url",)]
    assert restore_keys.fill({"h": f"Bearer {PLACEHOLDER}"}, {"h": "Bearer abc"})[0] == {"h": "Bearer abc"}
    # 现在的值本身还是占位符（上一次恢复以后没填）、空的、是一组东西：都不算
    for current in (PLACEHOLDER, "", {"a": 1}, ["x"], None, True):
        assert restore_keys.fill({"k": PLACEHOLDER}, {"k": current})[1].missing == [("k",)], current


def test_lists_with_ids_or_names_are_matched_by_them():
    backup = [{"name": "b", "key": PLACEHOLDER}, {"name": "a", "key": PLACEHOLDER}]
    value, found = restore_keys.fill(backup, [{"name": "a", "key": "ka"}, {"name": "c", "key": "kc"}])
    assert value == [{"name": "b", "key": PLACEHOLDER}, {"name": "a", "key": "ka"}]
    assert found.kept == [("a", "key")] and found.missing == [("b", "key")] and found.dropped == [("c", "key")]
    assert restore_keys.holes([{"id": "x", "k": PLACEHOLDER}, {"k": PLACEHOLDER}]) == [("#1", "k"), ("#2", "k")]  # 不是每一项都有名字：按第几个
    assert restore_keys.label(("mcpServers", "github", "env", "TOKEN")) == "mcpServers › github › env › TOKEN"
    assert restore_keys.label(()) == "…"


# ---- 恢复之前先备份 ------------------------------------------------------------


def test_the_data_is_backed_up_first_without_any_secret(tmp_path):
    """恢复之前把现在的数据导出一份放在 data/restore-backups/：和"导出全部数据"一样遮住密钥，
    放进数据文件夹里的 .env（不管叫什么名字，只要是配置里的那个文件）也不带。再导出时不带这个文件夹。"""
    data = lived_in(tmp_path)
    env = write(data, "my-keys.txt", "ANTHROPIC_API_KEY=sk-" + "ant-" + "REAL" * 8 + "\n")  # .env 被放进了数据文件夹，换了个名字
    backup = pack(data, tmp_path / "backup.zip", [env])
    write(data, "memories/现在才有.md", "恢复之前的")
    done = restore.apply(restore.check(backup), data, "overwrite", [env], datetime(2026, 11, 22, 10, 0, 5))
    done.commit()
    assert done.backup == data / "restore-backups" / "zhifox-before-restore-20261122-100005.zip"
    assert restore.BEFORE.fullmatch(done.backup.name) and has_mode(done.backup, 0o600)
    packed = zipfile.ZipFile(done.backup)
    assert "memories/现在才有.md" in packed.namelist()  # 恢复之前的样子都在里面
    everything = b"".join(packed.read(name) for name in packed.namelist())
    for secret in [*SECRETS, "hook-secret-value", "REAL" * 8, "very-secret-cookie-value"]:
        assert secret.encode() not in everything, secret
    assert env.exists()  # 不在白名单里的东西，恢复从来不碰
    again = io.BytesIO()
    export.write_archive(data, again, MOMENT, [env])
    assert not any(name.startswith("restore-backups") for name in zipfile.ZipFile(again).namelist())


def test_old_automatic_backups_are_pruned(tmp_path, monkeypatch):
    monkeypatch.setattr(restore, "RESTORE_KEEP_BACKUPS", 2)
    data = tmp_path / "data"
    write(data, "settings.json", "{}")
    backup = pack(data, tmp_path / "backup.zip")
    made = []
    for minute in range(3):
        done = restore.apply(restore.check(backup), data, "overwrite", (), datetime(2026, 11, 22, 10, minute))
        done.commit()
        os.utime(done.backup, (1_700_000_000 + minute, 1_700_000_000 + minute))  # 按修改时间排先后：改成过去的、一个比一个晚
        made.append(done.backup.name)
    assert sorted(path.name for path in (data / "restore-backups").iterdir()) == made[1:]


def test_same_second_backups_get_a_number(tmp_path):
    data = tmp_path / "data"
    write(data, "settings.json", "{}")
    moment = datetime(2026, 11, 22, 10, 0, 0)
    first = restore.backup_first(data, (), moment)
    second = restore.backup_first(data, (), moment)
    assert first.name == "zhifox-before-restore-20261122-100000.zip" and second.name == "zhifox-before-restore-20261122-100000-2.zip"
    assert restore.BEFORE.fullmatch(second.name)


def test_an_empty_data_folder_needs_no_backup(tmp_path):
    old = tmp_path / "old"
    write(old, "settings.json", "{}")
    backup = pack(old, tmp_path / "backup.zip")
    data = tmp_path / "data"
    done = restore.apply(restore.check(backup), data, "overwrite", (), MOMENT)
    done.commit()
    assert done.backup is None and done.summary()["backup"] is None
    assert not (data / "restore-backups").exists()


# ---- 预览 ----------------------------------------------------------------------


def test_the_preview_says_when_what_and_how(tmp_path):
    data = lived_in(tmp_path)
    backup = pack(data, tmp_path / "backup.zip")
    shown = restore.preview(restore.check(backup), tmp_path / "empty")
    assert shown["exported_at"] == MOMENT.astimezone().isoformat(timespec="seconds") and shown["foreign"] is False
    counts = {part["id"]: part["count"] for part in shown["parts"]}
    assert counts["conversations"] == 2 and counts["memories"] == 2 and counts["skills"] == 2 and counts["todos"] == 1
    assert counts["tasks"] == 1 and counts["mcp"] == 2 and counts["channels"] == 1 and counts["settings"] is None
    assert "skill_state" not in counts  # skills.json 只是 Skill 的开关，不单独列一行
    assert next(part for part in shown["parts"] if part["id"] == "logs")["restore"] is False
    assert "browser/profile/" in shown["left_out"]
    assert set(shown["modes"]) == {"overwrite", "merge"}
    assert {step["action"] for step in shown["modes"]["overwrite"]["steps"]} == {"add"}
    assert json.dumps(shown, ensure_ascii=False)  # 原样发给页面


def test_old_backups_without_a_manifest_use_the_note_time(tmp_path):
    """旧版本导出的备份没有 backup-manifest.json：README 那一项的时间就是导出的时刻。连 README 都没有的（自己打的包），说看不出来。"""
    with_note = handmade(tmp_path / "old.zip", [("settings.json", "{}")])
    archive = restore.check(with_note)
    note_info = zipfile.ZipFile(with_note).getinfo(NOTE)
    assert archive.exported_at == datetime(*note_info.date_time) and archive.foreign is False
    bare = restore.check(handmade(tmp_path / "bare.zip", [("settings.json", "{}")], note=False))
    assert bare.exported_at is None and bare.foreign is True


def test_an_unknown_mode_is_refused(tmp_path):
    archive = restore.check(handmade(tmp_path / "a.zip", [("settings.json", "{}")]))
    with pytest.raises(RestoreError):
        restore.plan(archive, tmp_path / "data", "replace-everything")


def test_every_place_the_program_keeps_data_is_restorable(tmp_path):
    """白名单（export.PARTS）要盖住程序放数据的每一个地方：不然新加的功能的备份恢复不了（会被当成"不认识的东西"整个拒绝）。
    Config 里每一个指向数据文件夹里的路径，要么是一样东西，要么是导出本来就不带的（口令、锁、恢复前的自动备份、检查新版本的缓存）。"""
    data = tmp_path / "data"
    config = Config(effort="medium", max_tokens=1000, max_steps=5, workspace=tmp_path, data_dir=data, env_file=tmp_path / ".env", web=False)
    never_exported = {config.web_address_file, config.updates_file}
    for name in dir(Config):
        value = getattr(config, name)
        if not isinstance(value, Path) or value == data or not value.is_relative_to(data) or value in never_exported:
            continue
        relative = value.relative_to(data).as_posix()
        folder = name.endswith("_dir")
        probe = relative + "/x.json" if folder else relative
        if relative in ("browser", "desktop"):  # 这两个文件夹里只认截图（浏览器的配置文件夹不导出）
            probe = relative + "/shots/x.jpg"
        assert export.part_of(probe) is not None, f"Config.{name}（{relative}）不在 export.PARTS 里"
    for relative in ("skills/x/SKILL.md", "skills.json", "input_history", "evals/run/summary.json", "logs/service.log", "logs/service.log.1"):
        assert export.part_of(relative) is not None, relative


def test_a_lived_in_folder_exports_into_a_restorable_backup(tmp_path):
    """用真的 store 写出来的数据文件夹导出一份：每一项都在白名单里，check 一项都不拒绝。"""
    data = lived_in(tmp_path)
    archive = restore.check(pack(data, tmp_path / "backup.zip"))
    names = {entry.name for entry in archive.entries}
    assert "skills.json" in names and "commands.json" in names and "tasks.json" in names and "todos.json" in names
    assert len(names) == len(snapshot(data)) - 1  # 只少了浏览器的 cookie


@pytest.mark.skipif(not symlinks_work(), reason="这台电脑上建不了符号链接（Windows 没开开发者模式）")
def test_links_in_the_data_folder_are_not_in_the_backup_so_not_restored(tmp_path):
    data = tmp_path / "data"
    write(data, "settings.json", "{}")
    outside = write(tmp_path / "outside", "secret.md", "外面的")
    (data / "memories").mkdir()
    (data / "memories" / "link.md").symlink_to(outside)
    archive = restore.check(pack(data, tmp_path / "backup.zip"))
    assert [entry.name for entry in archive.entries] == ["settings.json"]
