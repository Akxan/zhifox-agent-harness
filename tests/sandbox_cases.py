"""真的沙箱要过的几关：macOS（test_sandbox_macos.py）和 Linux（test_sandbox_linux.py）跑的是同一组，只是后端不同。

每一关都在临时目录里：一个工作目录（里面有 data/ 和 .env），一个假的家目录（.ssh/id_rsa、notes.txt 代替真的），
工作目录外面的一个文件夹，和本机 127.0.0.1 上的一个小网页服务器（看"不能联网"挡不挡得住本机）。
命令走的是真的 Shell.run，和对话里执行命令是同一条路。
"""

from __future__ import annotations

import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from fake_sandbox import REAL_DETECT

from harness.sandbox import backend as backends
from harness.sandbox import session
from harness.sandbox.session import Layout, Sandbox
from harness.tools.base import ToolError
from harness.tools.shell import Shell


def use_real_backend(monkeypatch) -> None:
    """换回真的后端（conftest 默认换成了假的）。这台电脑上用不了就跳过（CI 里要求必须能用的，由各自的文件决定）。"""
    monkeypatch.setattr(backends, "detect", REAL_DETECT)


class _Hello(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"hello")

    def log_message(self, *args):
        pass


@pytest.fixture
def lab(tmp_path, monkeypatch):
    workspace = tmp_path / 'work "q" (x) 中文'
    (workspace / "data").mkdir(parents=True)
    (workspace / "data" / "settings.json").write_text("{}")
    (workspace / ".env").write_text("ANTHROPIC_API_KEY=sk-fake\n")
    (workspace / "prog").mkdir()
    (workspace / "prog" / "main.py").write_text("print(1)\n")
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("PRIVATE KEY\n")
    (home / "notes.txt").write_text("私人笔记\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "o.txt").write_text("外面\n")
    level = {"value": "workspace"}
    extra: list[dict] = []
    layout = Layout(
        workspace=workspace, private=(workspace / "data",), protected=(workspace / ".env",),
        extra=lambda: extra, default=lambda: level["value"], home=lambda: home,
    )
    # 智狐自己的程序：拿工作目录里的 prog/ 代替（真的那个在项目里，测试不碰它）
    monkeypatch.setattr(session, "program_paths", lambda: [workspace / "prog"])
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Hello)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    shell = Shell(workspace, Sandbox(layout))
    yield SimpleNamespace(workspace=workspace, home=home, outside=outside, level=level, extra=extra, shell=shell, port=server.server_address[1])
    server.shutdown()
    server.server_close()


def failed(output: str) -> bool:
    return "[退出码" in output


def check_writing_inside_works(lab):
    out = lab.shell.run("echo hi > made.txt && cat made.txt && mkdir -p a/b && touch a/b/c")
    assert out == "hi" and (lab.workspace / "made.txt").read_text() == "hi\n" and (lab.workspace / "a" / "b" / "c").exists()


def check_writing_outside_fails(lab):
    target = lab.outside / "x.txt"
    out = lab.shell.run(f"echo no > '{target}'")
    assert failed(out) and not target.exists()
    assert "[这条命令是在沙箱里运行的" in out  # 告诉模型是沙箱挡住的
    assert failed(lab.shell.run(f"rm '{lab.outside / 'o.txt'}'")) and (lab.outside / "o.txt").exists()


def check_reading_private_places_fails(lab):
    for path in (lab.home / ".ssh" / "id_rsa", lab.home / "notes.txt", lab.outside / "o.txt"):
        out = lab.shell.run(f"cat '{path}'")
        assert failed(out) and "PRIVATE" not in out and "私人" not in out and "外面" not in out, (path, out)


def check_data_and_keys_are_untouchable(lab):
    for command in ("cat .env", "cat data/settings.json", "ls data", "echo x >> .env", "echo x > data/new.json", "rm .env"):
        out = lab.shell.run(command)
        assert failed(out) and "sk-fake" not in out, (command, out)
    assert (lab.workspace / ".env").read_text() == "ANTHROPIC_API_KEY=sk-fake\n" and not (lab.workspace / "data" / "new.json").exists()


def check_a_missing_key_file_cannot_be_planted(lab):
    (lab.workspace / ".env").unlink()
    lab.shell.run("echo ANTHROPIC_BASE_URL=http://evil > .env")
    planted = lab.workspace / ".env"
    assert not planted.exists() or "evil" not in planted.read_text()


def check_zhifox_program_is_read_only(lab):
    out = lab.shell.run("cat prog/main.py && echo 'import os' >> prog/main.py")
    assert out.startswith("print(1)") and failed(out)
    assert (lab.workspace / "prog" / "main.py").read_text() == "print(1)\n"


def check_home_and_temp_are_its_own(lab):
    out = lab.shell.run('echo "$HOME|$TMPDIR" && touch "$HOME/.cache-ok" "$TMPDIR/t" && echo ok')
    home, temp = out.splitlines()[0].split("|")
    assert out.endswith("ok") and home != str(lab.home) and Path(home).is_dir() and Path(temp).is_dir()


def check_no_network_reaches_localhost(lab):
    if shutil.which("curl") is None:
        pytest.skip("这台电脑上没有 curl")
    url = f"http://127.0.0.1:{lab.port}/"
    blocked = lab.shell.run(f"curl -sS -m 5 {url}")
    assert failed(blocked) and "hello" not in blocked and "[这条命令是在沙箱里运行的" in blocked
    lab.level["value"] = "network"
    assert lab.shell.run(f"curl -sS -m 5 {url}") == "hello"  # "工作目录 + 联网"：本机也连得上


def check_an_extra_read_only_path(lab):
    lab.extra.append({"path": str(lab.outside), "write": False})
    assert lab.shell.run(f"cat '{lab.outside / 'o.txt'}'") == "外面"
    assert failed(lab.shell.run(f"touch '{lab.outside / 'new'}'"))
    lab.extra[0]["write"] = True
    assert not failed(lab.shell.run(f"touch '{lab.outside / 'new'}'")) and (lab.outside / "new").exists()


def check_leaving_the_sandbox_for_one_command(lab):
    assert lab.shell.run(f"cat '{lab.outside / 'o.txt'}'", unsandboxed=True) == "外面"  # 你在卡片上放行过（这里直接调用）
    stuck = Shell(lab.workspace, Sandbox(lab.shell.sandbox.layout, escape=lambda: False))
    with pytest.raises(ToolError, match="不能在沙箱之外运行"):
        stuck.run("true", unsandboxed=True)


def check_off_is_todays_behaviour(lab):
    lab.level["value"] = "off"
    assert lab.shell.run(f"cat '{lab.outside / 'o.txt'}'") == "外面"


CASES = [value for name, value in sorted(globals().items()) if name.startswith("check_")]
