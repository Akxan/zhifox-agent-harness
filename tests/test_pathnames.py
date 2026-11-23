r"""pathnames.py 的测试：同一个文件的几种写法、Windows 上的花招、链接（目录联接）、换行。

分三部分：
  纯函数     trick、word_trick、key 这些只看字符串，带 windows=True 在 macOS、Linux 上也按 Windows 的规矩测
  接到各处    文件工具、只读命令的路径检查、记忆、Skill、Agent、多媒体编号、记忆工具：在哪个系统上都跑。
             要 Windows 的规矩时把 pathnames.WINDOWS 换成 True（各处不带 windows 参数时看的就是它）
  真的 Windows 目录联接、8.3 短名字、数据流、尾巴上的点：只在 Windows 上跑（skipif），上面有对应的、哪儿都能跑的版本
"""

from __future__ import annotations

import io
import os
import stat
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace

import pytest

from harness import memories, pathnames
from harness.agents.spec import name_problem as agent_name_problem
from harness.agents.store import AgentError, AgentStore
from harness.commands import Places, Unclear
from harness.imports.places import Env
from harness.media import ID
from harness.permissions import Permissions
from harness.skills import bundle
from harness.skills.spec import name_problem as skill_name_problem
from harness.skills.store import SkillError, SkillStore, locate
from harness.tools import memory as memory_tool
from harness.tools.base import ToolError
from harness.tools.files import TOOL_NAME as FILE_TOOL
from harness.tools.files import FileEditor
from harness.tools.memory import make_memory_tool

WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="要真的 Windows（NTFS 的目录联接、短名字、数据流）")


@pytest.fixture
def windows(monkeypatch):
    """各处按 Windows 的规矩看（pathnames.WINDOWS 为真），在 macOS、Linux 上测 Windows 的那一支。"""
    monkeypatch.setattr(pathnames, "WINDOWS", True)


# ---- 纯函数：花招 ---------------------------------------------------------------

TRICKS = [
    # 网络和设备路径
    r"\\server\share\x", "//server/share/x", r"\/server/share", r"/\server\share", r"\\?\C:\x", r"\\.\PhysicalDrive0",
    r"\\?\UNC\server\share\x", r"\??\C:\x", "//?/C:/x",
    # 盘符加相对路径、当前盘的根目录
    "C:x", "C:", r"c:..\x", r"\x", "/x", r"\Users\me\.env",
    # 数据流
    ".env::$DATA", "file.txt:secret", r"C:\proj\.env:$DATA", r"data\x.json:hidden",
    # 设备名（带扩展名、上标数字、空格也算）
    "CON", "nul", "aux.txt", "COM1", "lpt9", "COM¹", "lpt³.log", "nul .txt", r"C:\x\con\y", "CONIN$", "com0",
    # 8.3 短名字
    "PROGRA~1", "ENV~1", r"C:\PROGRA~1\x", "CHANNE~1.JSO", r"data\CONVER~1\c.json",
    # 尾巴上的点和空格
    ".env.", ".env ", ".env...", r"data.\channels.json", r"data \channels.json", r"C:\proj\.ENV.",
    # Windows 不许用的字符、控制字符
    "a<b", "a>b", "a|b", 'a"b', "a?b", "a*b", "a\x01b",
]

FINE = [
    r"C:\Users\me\proj\notes.txt", "C:/Users/me/file.txt", r"C:\Users\me/mixed\sep.txt", r"src\main.py", "src/main.py",
    r"..\x\y.txt", r".\a", r"~\notes.md", "HEAD.txt", "a.b.c", "foo~bar", "foo~1.tar.gz", "console.log", "connect.py",
    "資料/筆記.md", r"D:\a\b",
]


@pytest.mark.parametrize("raw", TRICKS)
def test_tricks_are_named_on_windows(raw):
    assert pathnames.trick(raw, windows=True)


@pytest.mark.parametrize("raw", FINE)
def test_ordinary_windows_paths_are_not_tricks(raw):
    assert pathnames.trick(raw, windows=True) is None


@pytest.mark.parametrize("raw", TRICKS)
def test_nothing_is_a_trick_elsewhere(raw):
    assert pathnames.trick(raw, windows=False) is None


@pytest.mark.parametrize("raw", [r"\\server\share", "//server/share/x", r"\\?\C:\x", r"\\.\COM1", r"\??\C:\x", "nul", r"src\CON", "COM¹.txt", "C:x", "c:"])
def test_words_that_touch_something_or_are_unclear(raw):
    assert pathnames.word_trick(raw, windows=True)
    assert pathnames.word_trick(raw, windows=False) is None


@pytest.mark.parametrize("raw", ["HEAD:src/x", "/C:text", r"\x", ".env::$DATA", ".env.", "HEAD~1", r"C:\x\y", "--file=x", "/s"])
def test_words_left_to_the_comparison(raw):
    """命令里的这些词照常比较：大小写、尾巴上的点、数据流由 key 统一；/s、findstr /C:文字 这种是普通的选项。"""
    assert pathnames.word_trick(raw, windows=True) is None


def test_short_names_and_device_names():
    assert pathnames.short_name("PROGRA~1", windows=True)
    assert pathnames.short_name(r"C:\x\ENV~1.", windows=True)
    assert pathnames.short_name("HEAD~1", windows=True)  # 长得一样：只有磁盘上真有这个名字才算数（见 Places）
    assert not pathnames.short_name("foo~bar", windows=True)
    assert not pathnames.short_name("PROGRA~1", windows=False)
    for name in ("con", "NUL.md", "COM¹", "lpt1.txt", "Aux"):
        assert pathnames.reserved(name, windows=True)
        assert not pathnames.reserved(name, windows=False)
    assert not pathnames.reserved("console", windows=True)
    assert not pathnames.reserved("commit", windows=True)


@pytest.mark.parametrize("name", ["a:b", "x.", "x ", "CON", "nul.txt", "PROGRA~1", "a*b", "a\x00b", "COM²"])
def test_name_tricks_apply_on_every_system(name):
    """压缩包、上传的文件夹里的名字：在 Windows 上解开会出事的，在哪儿都不收（name_trick 不看现在是什么系统）。"""
    assert pathnames.name_trick(name)


@pytest.mark.parametrize("name", [".", "..", "SKILL.md", "run.sh", "a.b.c", ".env", "con-fig.md"])
def test_ordinary_names(name):
    assert pathnames.name_trick(name) is None


# ---- 纯函数：比较用的写法 ---------------------------------------------------------


def test_one_windows_file_many_spellings():
    spellings = [r"C:\Proj\.ENV", "c:/proj/.env", r"C:\proj\.env.", r"C:\proj\.env::$DATA", r"C:\proj\.env ", r"C:\proj\.env...", r"c:\PROJ\.Env"]
    assert len({pathnames.key(spelling, windows=True) for spelling in spellings}) == 1
    assert pathnames.key(r"C:\x\...\y", windows=True) == pathnames.key(r"C:\x\y", windows=True)
    assert pathnames.key(r"\\Server\Share\x", windows=True).startswith("//server/share")


def test_keys_elsewhere_only_fold_case_and_unicode():
    assert pathnames.key("/w/.ENV", windows=False) == pathnames.key("/w/.env", windows=False)
    assert pathnames.key("/w/caf\u00e9", windows=False) == pathnames.key("/w/cafe\u0301", windows=False)
    assert pathnames.key("/w/.env.", windows=False) != pathnames.key("/w/.env", windows=False)  # 在 macOS、Linux 上是另一个文件
    assert pathnames.key(r"a\b", windows=False) == "a\\b"  # 反斜杠是文件名里的一个字符


def test_inside_and_within():
    assert pathnames.inside("/w/data/x", "/w/data")
    assert pathnames.inside("/w/data", "/w/data")
    assert not pathnames.inside("/w/database", "/w/data")
    assert pathnames.inside("c:/x", "c:/")
    # 按哪种规矩比，就给哪种系统的路径（Windows 上 Path("/w/x") 写出来是 \w\x）
    assert pathnames.within(PurePosixPath("/w/DATA/tasks.json"), PurePosixPath("/w/data"), windows=False)
    assert pathnames.within(PureWindowsPath(r"C:\w\Data.\tasks.json"), PureWindowsPath(r"C:\w\data"), windows=True)


def test_names_are_what_windows_opens():
    assert pathnames.names(r"C:\a.\b::$DATA/c ", windows=True) == ["a", "b", "c"]
    assert pathnames.names("/a./b", windows=False) == ["a.", "b"]


# ---- 纯函数：链接 -----------------------------------------------------------------


def _info(mode: int, attributes: int = 0, tag: int = 0) -> SimpleNamespace:
    return SimpleNamespace(st_mode=mode, st_file_attributes=attributes, st_reparse_tag=tag)


def test_junctions_and_symlinks_are_links_but_cloud_files_are_not():
    reparse = stat.FILE_ATTRIBUTE_REPARSE_POINT
    assert pathnames.linked(_info(stat.S_IFLNK | 0o777))
    assert pathnames.linked(_info(stat.S_IFDIR | 0o777, reparse, 0xA0000003))  # 目录联接：S_ISLNK 认不出它
    assert pathnames.linked(_info(stat.S_IFREG | 0o666, reparse, 0xA000000C))  # 符号链接
    assert not pathnames.linked(_info(stat.S_IFREG | 0o666, reparse, 0x9000001A))  # OneDrive 的占位文件：就是它自己
    assert not pathnames.linked(_info(stat.S_IFDIR | 0o777))
    assert not pathnames.linked(SimpleNamespace(st_mode=stat.S_IFREG))  # macOS、Linux 的 stat 没有这两个属性


def test_entries_are_looked_at_with_a_real_lstat_on_windows(tmp_path, symlinks):
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    (tmp_path / "link").symlink_to(tmp_path / "file.txt")
    entries = {entry.name: entry for entry in os.scandir(tmp_path)}
    assert pathnames.entry_linked(entries["link"], windows=True)
    assert not pathnames.entry_linked(entries["file.txt"], windows=True)
    info = pathnames.entry_info(entries["file.txt"], windows=True)
    assert os.path.samestat(info, os.stat(tmp_path / "file.txt"))


def test_open_without_o_nofollow_still_refuses_links(tmp_path, monkeypatch, symlinks):
    """Windows 上没有 O_NOFOLLOW：打开前后各看一眼，是链接、或者中间被换掉了，都不打开。"""
    monkeypatch.setattr(pathnames, "NOFOLLOW", 0)
    real, other = tmp_path / "real.txt", tmp_path / "other.txt"
    real.write_bytes(b"a\r\nb\x1a c")
    other.write_bytes(b"other")
    (tmp_path / "link.txt").symlink_to(other)
    with os.fdopen(pathnames.open_nofollow(real, os.O_RDONLY), "rb") as handle:
        assert handle.read() == b"a\r\nb\x1a c"
    with pytest.raises(OSError):
        pathnames.open_nofollow(tmp_path / "link.txt", os.O_RDONLY)
    # 检查之后、打开之前被换掉：打开前看到的是 other，打开的是 real
    seen, lstat = os.lstat(other), os.lstat
    monkeypatch.setattr(os, "lstat", lambda path, *args, **kwargs: seen if Path(path) == real else lstat(path, *args, **kwargs))
    with pytest.raises(OSError):
        pathnames.open_nofollow(real, os.O_RDONLY)


def test_open_without_o_nofollow_creates_new_files(tmp_path, monkeypatch):
    monkeypatch.setattr(pathnames, "NOFOLLOW", 0)
    target = tmp_path / "new.bin"
    with os.fdopen(pathnames.open_nofollow(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
        handle.write(b"\n\x1a\r\n")
    assert target.read_bytes() == b"\n\x1a\r\n"
    with pytest.raises(FileExistsError):
        pathnames.open_nofollow(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL)


# ---- 文件工具 ---------------------------------------------------------------------


@pytest.fixture
def editor(tmp_path):
    workspace = tmp_path / "work"
    (workspace / "data").mkdir(parents=True)
    (workspace / ".env").write_text("ANTHROPIC_API_KEY=sk-fake\n", encoding="utf-8")
    (workspace / "data" / "channels.json").write_text("{}\n", encoding="utf-8")
    protected = [workspace / ".env", workspace / "data" / "channels.json"]
    return FileEditor(workspace, tmp_path / "backups", protected=protected)


@pytest.mark.parametrize("path", [".ENV", ".Env", "DATA/Channels.JSON", "data/CHANNELS.json"])
def test_secret_files_in_another_case_are_still_secret(editor, path):
    """macOS 默认的磁盘不分大小写：.ENV 就是 .env。以前按字面比，view .ENV 能把密钥读出来。"""
    with pytest.raises(ToolError, match="密钥"):
        editor.run({"command": "view", "path": path})
    with pytest.raises(ToolError, match="密钥"):
        editor.run({"command": "create", "path": path, "file_text": "ANTHROPIC_BASE_URL=https://evil.invalid\n"})


def test_a_hard_link_to_a_secret_file_is_still_secret(editor):
    os.link(editor.workspace / ".env", editor.workspace / "notes.txt")
    with pytest.raises(ToolError, match="密钥"):
        editor.run({"command": "view", "path": "notes.txt"})


def test_a_missing_secret_file_cannot_be_planted_in_another_case(tmp_path):
    """.env 还不存在时也一样：先建一个 .ENV，macOS 上以后读 .env 的程序读到的就是它。"""
    workspace = tmp_path / "work"
    workspace.mkdir()
    editor = FileEditor(workspace, tmp_path / "backups", protected=[workspace / ".env"])
    with pytest.raises(ToolError, match="密钥"):
        editor.run({"command": "create", "path": ".ENV", "file_text": "ANTHROPIC_BASE_URL=https://evil.invalid\n"})
    assert not any(workspace.iterdir())


@pytest.mark.parametrize("path", [".env.", ".env ", ".env...", ".env::$DATA", "ENV~1", "nul", "CON.txt", r"\\server\share\x", "//server/share/x", r"\\?\C:\x", "C:x", r"\x", "data./channels.json", "a|b"])
def test_the_file_tool_refuses_windows_tricks(editor, windows, path):
    with pytest.raises(ToolError, match="这种写法不认"):
        editor.run({"command": "view", "path": path})
    with pytest.raises(ToolError, match="这种写法不认"):
        editor.run({"command": "create", "path": path, "file_text": "x"})


def test_the_file_tool_still_takes_ordinary_paths_on_windows(editor, windows):
    assert "已创建" in editor.run({"command": "create", "path": "src/Notes.txt", "file_text": "hi"})
    assert editor.run({"command": "view", "path": "src/Notes.txt"}) == "     1\thi"


def test_reading_private_data_in_another_case_still_asks(tmp_path):
    """"改工作目录里的文件不用问"那一档：DATA/tasks.json 就是 data/tasks.json（macOS），改它照样要问。
    以前按字面比，模型能不声不响地把一个定时任务改成"全部自动允许"。"""
    workspace = tmp_path / "work"
    (workspace / "data").mkdir(parents=True)
    editor = FileEditor(workspace, tmp_path / "backups")
    permissions = Permissions(editor, private=[workspace / "data"])
    permissions.mode = "edits"
    edit = {"command": "create", "path": "DATA/tasks.json", "file_text": "[]"}
    assert not permissions.auto_allows(FILE_TOOL, edit)
    assert permissions.auto_allows(FILE_TOOL, {"command": "create", "path": "notes.txt", "file_text": "x"})
    assert "助手自己保存的数据" in permissions.review(FILE_TOOL, {"command": "view", "path": "Data/conversations/x.json"})


# ---- 文件工具：换行原样保留 -----------------------------------------------------------


def test_str_replace_keeps_crlf_line_endings(editor):
    target = editor.workspace / "win.txt"
    target.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    assert "\r" not in editor.run({"command": "view", "path": "win.txt"})  # 模型看到的是普通的 \n
    editor.run({"command": "str_replace", "path": "win.txt", "old_str": "two\nthree", "new_str": "2\n3\n4"})
    assert target.read_bytes() == b"one\r\n2\r\n3\r\n4\r\n"
    editor.run({"command": "str_replace", "path": "win.txt", "old_str": "3\r\n4", "new_str": "three"})  # 模型自己带了 \r\n 也认
    assert target.read_bytes() == b"one\r\n2\r\nthree\r\n"
    editor.run({"command": "insert", "path": "win.txt", "insert_line": 1, "insert_text": "1.5\nmore"})
    assert target.read_bytes() == b"one\r\n1.5\r\nmore\r\n2\r\nthree\r\n"


def test_str_replace_keeps_lf_files_lf(editor):
    target = editor.workspace / "unix.txt"
    target.write_bytes(b"one\ntwo\n")
    editor.run({"command": "str_replace", "path": "unix.txt", "old_str": "two", "new_str": "2\nmore"})
    editor.run({"command": "insert", "path": "unix.txt", "insert_line": 0, "insert_text": "zero"})
    assert target.read_bytes() == b"zero\none\n2\nmore\n"


def test_mixed_line_endings_and_bom_are_left_alone(editor):
    target = editor.workspace / "mixed.txt"
    target.write_bytes("\ufeffa\r\nb\nc\r\n".encode())
    editor.run({"command": "str_replace", "path": "mixed.txt", "old_str": "b", "new_str": "B"})
    assert target.read_bytes() == "\ufeffa\r\nB\nc\r\n".encode()


def test_create_writes_exactly_what_the_model_sent(editor):
    editor.run({"command": "create", "path": "new.txt", "file_text": "a\nb\r\nc"})
    assert (editor.workspace / "new.txt").read_bytes() == b"a\nb\r\nc"


# ---- 只读的命令 ---------------------------------------------------------------------


@pytest.fixture
def places(tmp_path):
    """按 Windows 的规矩看的 Places（在哪个系统上都能跑：比较的是 pathnames.key 算出来的写法）。"""
    ws, home = tmp_path / "ws", tmp_path / "home"
    (ws / "data" / "conversations").mkdir(parents=True)
    (ws / ".env").write_text("K=1\n", encoding="utf-8")
    (ws / "data" / "channels.json").write_text("{}\n", encoding="utf-8")
    (ws / "data" / "conversations" / "c1.json").write_text("{}\n", encoding="utf-8")
    (ws / "src").mkdir()
    (ws / "src" / "a.txt").write_text("a\n", encoding="utf-8")
    home.mkdir()
    protected = [ws / ".env", ws / "data" / "channels.json"]
    places = Places(ws.resolve(), protected=protected, private=[ws / "data"], home=home, windows=True)
    places.check("src/a.txt")  # 普通的文件照常：免得临时文件夹的名字（test_…_secret…）让下面的测试白白通过
    return places


@pytest.mark.parametrize("word", [".env.", ".env ", ".ENV::$DATA", "DATA/Channels.JSON.", "data/conversations/c1.json:x", "Data./conversations/c1.json"])
def test_commands_see_through_windows_spellings(places, word):
    with pytest.raises(Unclear):
        places.check(word)


@pytest.mark.parametrize("word", [r"\\server\share\x", "//server/share", r"\\?\C:\x", r"\\.\PhysicalDrive0", "nul", "COM1.txt", r"src\CON", "C:x"])
def test_commands_never_touch_network_or_device_paths(places, monkeypatch, word):
    """光是展开一下（stat）就会去连那台主机、碰那个设备：必须在碰磁盘之前就停下。"""

    def touched(*args, **kwargs):
        raise AssertionError("碰了磁盘")

    monkeypatch.setattr(Path, "resolve", touched)
    with pytest.raises(Unclear):
        places.check(word)


def test_short_names_on_disk_are_unclear(places):
    # 不用 ENV~1：真的 Windows 上它多半就是 .env 的短名字（写它就是写 .env），那样查出来的是"保存密钥的文件"
    (places.cwd / "NOTES~1.TXT").write_text("可能是别的文件的另一个名字\n", encoding="utf-8")
    with pytest.raises(Unclear, match="短名字"):
        places.check("NOTES~1.TXT")
    places.check("HEAD~1")  # 磁盘上没有这个名字：git 的写法，什么都读不到
    places.check("src/a.txt")


def test_a_link_that_resolve_cannot_follow_is_unclear(places, monkeypatch, symlinks):
    """Windows 上 resolve 碰到绕成圈的链接不报错，原样交回来：交回来的还是链接，就当作看不清。"""
    (places.cwd / "loop").symlink_to(places.cwd / "loop")
    monkeypatch.setattr(Path, "resolve", lambda self, strict=False: self)  # 像 Windows 那样不报错
    with pytest.raises(Unclear, match="看不清 .* 指的是哪里（符号链接绕成了圈）"):
        places.check("loop")


def test_names_too_long_for_windows_are_unclear():
    assert pathnames.word_trick("a" * 256, windows=True)
    assert pathnames.word_trick("x/" + "a" * 255, windows=True) is None
    assert pathnames.word_trick("a" * 300, windows=False) is None


@pytest.mark.parametrize("word", [r"~\AppData\Roaming\gcloud\credentials.db", "AppData/Roaming/GitHub CLI/hosts.yml", r"C:\Users\me\AppData\Local\Microsoft\Credentials\x", r"x\PSReadLine\ConsoleHost_history.txt"])
def test_windows_key_folders(places, word):
    with pytest.raises(Unclear):
        places.check(word)


def test_microsoft_folders_only_count_on_windows(tmp_path):
    Places(tmp_path, windows=False).check("Microsoft/Protect/x")  # macOS、Linux 上这只是普通的文件夹名
    with pytest.raises(Unclear):
        Places(tmp_path, windows=True).check("Microsoft/Protect/x")


def test_a_folder_scan_finds_a_hard_link_with_a_real_lstat(places):
    """Windows 上 DirEntry.stat() 的 st_ino 是 0：grep -r 扫文件夹时要真的 lstat，才认得出密钥文件的另一个名字。"""
    os.link(places.cwd / ".env", places.cwd / "src" / "notes.txt")
    with pytest.raises(Unclear, match="另一个名字"):
        places.check_tree("src", contents=True)


# ---- 记忆、Skill、Agent、多媒体 --------------------------------------------------------


@pytest.fixture
def memory_root(tmp_path):
    root = tmp_path / "memories"
    root.mkdir()
    (root / "notes.md").write_bytes("记住的东西\n".encode())  # 按字节写：Windows 上 write_text 会写成 \r\n
    return root


@pytest.mark.parametrize("relative", ["notes.md.", "notes.md ", "notes.md::$DATA", "NOTES~1.MD", "CON", "nul.md", r"..\..\.env", "a\\..\\..\\b"])
def test_memory_paths_with_windows_tricks_are_refused(memory_root, windows, relative):
    with pytest.raises(memories.MemoryFileError):
        memories.read(memory_root, relative)
    with pytest.raises(memories.MemoryFileError):
        memories.delete(memory_root, relative)
    assert (memory_root / "notes.md").exists()


def test_memories_still_work_on_windows(memory_root, windows, monkeypatch):
    monkeypatch.setattr(pathnames, "NOFOLLOW", 0)  # Windows 没有 O_NOFOLLOW
    assert memories.read(memory_root, "notes.md")["text"] == "记住的东西\n"
    with pytest.raises(memories.MemoryFileError, match="Windows"):
        memories.save(memory_root, "notes.md", "notes.md.", "x", None, force=True)
    with pytest.raises(memories.MemoryFileError, match="Windows"):
        memories.save(memory_root, "notes.md", "CON.md", "x", None, force=True)
    assert memories.save(memory_root, "notes.md", "renamed.md", "新的\n", None, force=True) == "renamed.md"


def test_a_junction_inside_the_memory_folder_is_not_followed(memory_root, monkeypatch):
    """目录联接的 st_mode 是"文件夹"：每一层都要问 pathnames.linked，不能只看 S_ISLNK。"""
    (memory_root / "junction").mkdir()
    (memory_root / "junction" / "secret.md").write_text("外面的东西\n", encoding="utf-8")
    real = pathnames.linked
    monkeypatch.setattr(pathnames, "linked", lambda info: real(info) or stat.S_ISDIR(info.st_mode) and info.st_ino == os.lstat(memory_root / "junction").st_ino)
    with pytest.raises(memories.MemoryFileError, match="链接"):
        memories.read(memory_root, "junction/secret.md")


@pytest.mark.parametrize(
    "path",
    ["con.txt", "a/nul", "scripts/aux.py", "x:y", "SKILL.md.", "a /b.md", "PROGRA~1/x", "a*b", "..\\..\\x", "a\\..\\..\\b", "C:x", "c:/x", "COM¹.md"],
)
def test_archives_with_windows_names_are_refused_everywhere(path):
    with pytest.raises(SkillError):
        bundle.clean_path(path)


ARCHIVE_TRICKS = [
    "../x",  # 写到文件夹外面去（zip slip）
    "a/../../x",
    "./a",
    "a//b",
    "/etc/hosts",  # 绝对路径
    "C:x",  # 盘符
    "C:/x",
    "a\\b",  # 反斜杠：Windows 上的分隔符
    "a\tb",  # 控制字符
    "CON",  # 设备名
    "a/nul.txt",
    "a:b",  # 数据流
    "PROGRA~1/x",  # 8.3 短名字
    "notes.md.",  # 尾巴上的点：Windows 会去掉它，和 notes.md 撞名
    "a /b",  # 尾巴上的空格
    "",
    "x" * 1001,
    "y" * 256 + "/z",  # 一段名字太长
]


@pytest.mark.parametrize("name", ARCHIVE_TRICKS, ids=range(len(ARCHIVE_TRICKS)))
def test_archive_names_that_escape_or_turn_into_something_else(name):
    """压缩包里一项的名字（从备份恢复、装 Skill 共用这一处检查）：解开会写到外面、或者在 Windows 上变成别的东西的，在哪儿都不收。"""
    assert pathnames.archive_trick(name)
    with pytest.raises(SkillError):  # Skill 的安装照旧拒绝它们（clean_path 现在用的就是这一处）
        bundle.clean_path(name)


def test_archive_names_that_are_fine():
    for name in ("conversations/20261120-1403-abc.json", "memories/projects/智狐.md", "media/a.b.c.png", ".hidden"):
        assert pathnames.archive_trick(name) is None, name
        assert bundle.clean_path(name) == name


def test_archive_names_with_too_many_levels():
    deep = "/".join(["d"] * 33)
    assert pathnames.archive_trick(deep, 32) and pathnames.archive_trick(deep, 33) is None
    assert pathnames.archive_trick("a", 0)  # 一段也不许
    assert pathnames.archive_trick(None) and pathnames.archive_trick(3)  # 不是文字的一律不收


def test_a_zip_with_a_windows_trick_installs_nothing():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("SKILL.md", "---\nname: x\ndescription: y\n---\n")
        archive.writestr("scripts/aux.py", "print(1)\n")
    with pytest.raises(SkillError):
        bundle.from_zip(data.getvalue())
    with pytest.raises(SkillError):
        bundle.from_upload([{"path": "SKILL.md.", "data": ""}])


def test_skill_and_agent_names_that_are_windows_devices(windows):
    for name in ("con", "nul", "com1", "lpt9", "aux", "prn"):
        assert skill_name_problem(name) is not None
    assert agent_name_problem("COM¹") is not None
    assert agent_name_problem("NUL") is not None
    assert skill_name_problem("console") is None
    assert agent_name_problem("reviewer") is None


def test_device_names_are_fine_elsewhere(monkeypatch):
    monkeypatch.setattr(pathnames, "WINDOWS", False)
    assert skill_name_problem("con") is None
    assert agent_name_problem("nul") is None


def test_skill_folders_and_files_with_windows_tricks_are_refused(tmp_path, windows, monkeypatch):
    monkeypatch.setattr(pathnames, "NOFOLLOW", 0)
    store = SkillStore(tmp_path / "data")
    files = {"SKILL.md": (b"---\nname: pdf\ndescription: d\n---\nbody\r\n", False), "assets/x.bin": (b"\n\x1a\r\n", False)}
    assert store.install(files, "pdf", replace=False, source={"kind": "new"}) == "pdf"
    assert (store.root / "pdf" / "assets" / "x.bin").read_bytes() == b"\n\x1a\r\n"  # 按字节写：Windows 上不换换行
    assert store.get("pdf").name == "pdf"
    for name in ("pdf.", "pdf ", "pdf::$INDEX_ALLOCATION", "PDF~1"):
        with pytest.raises(SkillError):
            store.get(name)
    for relative in ("SKILL.md.", "SKILL.md::$DATA", "assets/X~1.BIN", "assets/con"):
        with pytest.raises(SkillError):
            locate(store.root / "pdf", relative)
    assert locate(store.root / "pdf", "assets/x.bin").name == "x.bin"


def test_agent_files_named_after_devices_are_refused(tmp_path, windows):
    store = AgentStore(tmp_path / "agents")
    for name in ("CON", "nul", "COM1"):
        with pytest.raises(AgentError):
            store.get(name)


@pytest.mark.parametrize(
    "media_id",
    ["20260101-120000-abcdef/0/0.", "20260101-120000-abcdef/0/0:x", r"20260101-120000-abcdef\0\0", r"..\..\x", r"\\server\share", "20260101-120000-abcdef/0/0 "],
)
def test_media_ids_leave_no_room_for_windows_tricks(media_id):
    assert ID.fullmatch(media_id) is None


@pytest.mark.parametrize("path", [r"/memories/\\server\share\x", "/memories/CON", "/memories/notes.md.", "/memories/notes.md::$DATA", r"/memories\..\..\.env", "/memories/C:x"])
def test_the_memory_tool_refuses_windows_tricks_before_the_sdk_sees_them(tmp_path, windows, path):
    tool = make_memory_tool(tmp_path / "data")
    with pytest.raises(ToolError, match="这种写法不认"):
        tool.run({"command": "create", "path": path, "file_text": "x"})
    tool.run({"command": "create", "path": "/memories/notes.md", "file_text": "x"})  # 普通的路径照常
    assert (tmp_path / "data" / "memories" / "notes.md").read_text(encoding="utf-8") == "x"


def test_the_sdk_memory_writes_always_ask_for_binary_mode(monkeypatch):
    """Windows 上 SDK 的 os.open 被换成 _BinaryOs.open：每次都带上 O_BINARY，别的照旧交给 os。"""
    calls = []
    monkeypatch.setattr(pathnames, "BINARY", 0x8000)
    monkeypatch.setattr(os, "open", lambda path, flags, mode=0o777, *, dir_fd=None: calls.append((path, flags, mode)) or 7)
    shim = memory_tool._BinaryOs()
    assert shim.open("x", os.O_WRONLY, 0o600) == 7
    assert calls == [("x", os.O_WRONLY | 0x8000, 0o600)]
    assert shim.O_CREAT == os.O_CREAT and shim.path is os.path  # 别的都是 os 自己的


def test_the_sdk_os_is_swapped_before_the_memory_tool_writes(tmp_path, monkeypatch):
    """SDK 在 make_memory_tool 里才导入（为了启动快，见 tools/memory.py）：换掉 SDK 里 os 的那一步照样在建工具之前做好，
    SDK 写记忆文件时的每一次 os.open 都带着 O_BINARY。在 macOS、Linux 上借一个假的旗子测这一支。"""
    import importlib

    real_binary = getattr(os, "O_BINARY", 0)  # 真的 Windows 上用它自己的旗子；别处借一个，交给系统之前再去掉
    bit = real_binary or 0x8000
    sdk = [importlib.import_module(name) for name in memory_tool._SDK_MODULES]
    for module in sdk:
        monkeypatch.setattr(module, "os", os)  # 测完换回原来的（换成 _BinaryOs 的话会一直留在这个进程里）
    monkeypatch.setattr(pathnames, "BINARY", bit)
    flags_seen = []
    real_open = os.open

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        flags_seen.append(flags)
        return real_open(path, flags if real_binary else flags & ~bit, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", recording_open)
    tool = make_memory_tool(tmp_path / "data")
    assert all(isinstance(module.os, memory_tool._BinaryOs) for module in sdk)
    tool.run({"command": "create", "path": "/memories/notes.md", "file_text": "第一行\n第二行\n"})
    assert flags_seen and all(flags & bit for flags in flags_seen), flags_seen
    assert (tmp_path / "data" / "memories" / "notes.md").read_bytes() == "第一行\n第二行\n".encode()


def test_import_places_are_shown_with_slashes_on_every_system():
    """给人看、页面送回来的位置一律用 /（Windows 上也是），查的时候才对得上。"""
    env = Env(home=PureWindowsPath(r"C:\Users\me"), workspace=PureWindowsPath(r"C:\work"), platform="win32")
    assert env.shown(PureWindowsPath(r"C:\Users\me\.claude\skills")) == "~/.claude/skills"
    assert env.shown(PureWindowsPath(r"C:\work\.claude\agents\tester.md")) == ".claude/agents/tester.md"
    assert env.shown(PureWindowsPath(r"D:\elsewhere\x")) == "D:/elsewhere/x"


# ---- 真的 Windows -------------------------------------------------------------------


def _junction(target: Path, link: Path) -> None:
    import _winapi  # 只有 Windows 有；建目录联接不要管理员权限

    _winapi.CreateJunction(str(target), str(link))


@WINDOWS_ONLY
def test_real_junctions_are_links(tmp_path):
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.md").write_text("外面\n", encoding="utf-8")
    root = tmp_path / "memories"
    root.mkdir()
    _junction(tmp_path / "outside", root / "door")
    assert pathnames.is_link(root / "door")
    assert not stat.S_ISLNK(os.lstat(root / "door").st_mode)  # 这就是为什么要 pathnames.linked
    assert memories.list_files(root) == []
    with pytest.raises(memories.MemoryFileError):
        memories.read(root, "door/secret.md")


@WINDOWS_ONLY
def test_export_does_not_walk_into_a_junction(tmp_path):
    from harness.export import plan

    data, outside = tmp_path / "data", tmp_path / "outside"
    data.mkdir()
    outside.mkdir()
    (outside / "id_rsa").write_text("PRIVATE KEY\n", encoding="utf-8")
    _junction(outside, data / "door")
    work = plan(data)
    assert ("door/" in [name for name, _ in work.skipped]) and not any("id_rsa" in name for _, name in work.files)


@WINDOWS_ONLY
def test_a_junction_to_the_data_folder_is_caught_by_a_folder_scan(tmp_path):
    ws = tmp_path / "ws"
    (ws / "data").mkdir(parents=True)
    (ws / "data" / "c1.json").write_text("{}\n", encoding="utf-8")
    (ws / "src").mkdir()
    _junction(ws / "data", ws / "src" / "door")
    places = Places(ws.resolve(), private=[ws / "data"], home=tmp_path / "home")
    with pytest.raises(Unclear):
        places.check_tree("src", contents=True)


@WINDOWS_ONLY
def test_real_windows_spellings_of_a_secret_file(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / ".env").write_text("K=1\n", encoding="utf-8")
    editor = FileEditor(workspace, tmp_path / "backups", protected=[workspace / ".env"])
    assert os.path.samestat(os.stat(workspace / ".env::$DATA"), os.stat(workspace / ".env"))  # 数据流就是这个文件
    assert os.path.samestat(os.stat(str(workspace / ".env") + "."), os.stat(workspace / ".env"))  # 尾巴上的点被去掉了
    for path in (".env.", ".env::$DATA", ".ENV"):
        with pytest.raises(ToolError):
            editor.run({"command": "view", "path": path})


@WINDOWS_ONLY
def test_a_real_short_name_is_refused(tmp_path):
    import ctypes

    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / ".env").write_text("K=1\n", encoding="utf-8")
    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(workspace / ".env"), buffer, 1024)
    short = Path(buffer.value).name
    if not short or short == ".env":
        pytest.skip("这个磁盘关掉了 8.3 短名字")
    editor = FileEditor(workspace, tmp_path / "backups", protected=[workspace / ".env"])
    with pytest.raises(ToolError):
        editor.run({"command": "view", "path": short})
    places = Places(workspace.resolve(), protected=[workspace / ".env"], home=tmp_path / "home")
    with pytest.raises(Unclear):
        places.check(short)


@WINDOWS_ONLY
def test_the_memory_tool_keeps_lf_on_windows(tmp_path):
    tool = make_memory_tool(tmp_path / "data")
    target = tmp_path / "data" / "memories" / "notes.md"
    tool.run({"command": "create", "path": "/memories/notes.md", "file_text": "# 记住的事\n- 一\n"})
    tool.run({"command": "insert", "path": "/memories/notes.md", "insert_line": 1, "insert_text": "- 二"})
    tool.run({"command": "str_replace", "path": "/memories/notes.md", "old_str": "- 一", "new_str": "- 三"})
    assert target.read_bytes() == "# 记住的事\n- 二\n- 三\n".encode()


@WINDOWS_ONLY
def test_line_endings_survive_on_windows(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    editor = FileEditor(workspace, tmp_path / "backups")
    (workspace / "unix.txt").write_bytes(b"a\nb\n")
    editor.run({"command": "str_replace", "path": "unix.txt", "old_str": "a", "new_str": "A"})
    assert (workspace / "unix.txt").read_bytes() == b"A\nb\n"  # 以前 Windows 上 write_text 会把它变成 \r\n
