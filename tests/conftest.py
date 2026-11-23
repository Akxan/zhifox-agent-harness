from __future__ import annotations

import functools
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import anthropic
import openai
import pytest

sys.path.insert(0, str(Path(__file__).parent))

import fake_sandbox
from fake_api import FakeAPI
from fake_openai import FakeOpenAI

from harness import commands as commands_module
from harness import i18n, providers, winacl
from harness.app import build_app
from harness.config import Config
from harness.desktop import backend as desktop_backend
from harness.fetch import client as fetch_client
from harness.imports import places as import_places
from harness.service import base as service_base
from harness.service import cli as service_cli

# ---- 符号链接 -------------------------------------------------------------------
# Windows 上建符号链接要管理员权限，或者打开了"开发者模式"；普通用户建的时候报 OSError（WinError 1314）。
# 这样的电脑上，建链接的测试跳过并写明原因，别的测试照常跑。macOS 和 Linux 上一直建得了，什么都不跳过。
# 不按"是不是 Windows"判断，而是真的建一次：开了开发者模式的 Windows 也能建，那就照常测。
# 硬链接（os.link）在 Windows 的 NTFS 上不要特别的权限，不受这里影响。
NO_SYMLINKS = "这台 Windows 电脑上不能建符号链接（要管理员权限或者开发者模式）"


@functools.cache
def symlinks_work() -> bool:
    """在一个新的临时文件夹里真的建一次链接，看这台电脑行不行。只试一次，之后用记下的结果。

    文件的链接和文件夹的链接各建一个：Windows 上这是两种不同的链接（文件夹的要 target_is_directory），
    测试里两种都用到了。
    """
    folder = Path(tempfile.mkdtemp(prefix="harness-symlink-probe-"))
    try:
        (folder / "file").write_text("x", encoding="utf-8")
        (folder / "dir").mkdir()
        (folder / "file-link").symlink_to(folder / "file")
        (folder / "dir-link").symlink_to(folder / "dir", target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
def symlinks():
    """要建符号链接的测试带上它：建不了链接的电脑上，整个测试跳过。

    好几个测试共用的准备工作（比如 test_skills 的 claude_dirs）不带它，而是只在 symlinks_work() 时建链接，
    再让真正用到这些链接的那几个测试带上它，免得不相干的测试也跟着跳过。
    """
    if not symlinks_work():
        pytest.skip(NO_SYMLINKS)


class RecordingUI:
    """测试用的界面：不显示任何东西，只把发生的事记下来。确认请求按预设的答案回答。"""

    def __init__(self, approve: bool = True, feedback: str = ""):
        self.approve = approve
        self.feedback = feedback
        self.streamed: list[tuple[str, str]] = []
        self.calls: list[str] = []
        self.results: list[tuple[str, bool]] = []
        self.previews: list[dict | None] = []  # 每个结果带的预览（浏览器的截图），大多数是 None
        self.confirmations: list[str] = []
        self.auto_allowed_count = 0  # 有几次是没问用户、自动放行的
        self.auto_whys: list[str] = []  # 每一次为什么没问（见 Permissions.allowance）
        self.infos: list[str] = []
        self.askers: list[str] = []  # 每一次确认是谁在问（子 Agent 的名字；主助手自己问时是空的）
        self.subagents: list[tuple[str, dict]] = []  # 子 Agent 的每一步进展：(step, data)
        self.cites: list[list[dict]] = []  # 每一次"这段文字引用了这些网页"（联网搜索的回答，见 harness/citations.py）
        self.stop_after: str | None = None  # 测试想在子 Agent 的某一步之后"点停止"时，写那一步的名字
        self._stop = False
        self._stop_callbacks: list = []

    def thinking(self, text: str) -> None:
        self.streamed.append(("thinking", text))

    def text(self, text: str) -> None:
        self.streamed.append(("text", text))

    def cite(self, sources: list[dict]) -> None:
        self.cites.append(sources)

    def notice(self, level: str, text: str) -> None:
        self.infos.append(text)

    def end_stream(self) -> None:
        pass

    def tool_call(self, name: str, tool_input: object) -> None:
        self.calls.append(name)

    def tool_result(self, text: str, is_error: bool, preview: dict | None = None) -> None:
        self.results.append((text, is_error))
        self.previews.append(preview)

    def confirm(self, description: str, agent: str = "") -> tuple[bool, str]:
        self.confirmations.append(description)
        self.askers.append(agent)
        return self.approve, self.feedback

    def auto_allowed(self, why: str = "mode") -> None:
        self.auto_allowed_count += 1
        self.auto_whys.append(why)

    def info(self, text: str) -> None:
        self.infos.append(text)

    def subagent(self, step: str, **data) -> None:
        if step != "done":
            self.check_stop()
        self.subagents.append((step, data))
        if self.stop_after == step:
            self.request_stop()

    def check_stop(self) -> None:
        if self._stop:
            raise KeyboardInterrupt

    def request_stop(self) -> None:
        self._stop = True
        for callback in list(self._stop_callbacks):
            callback()

    def on_stop(self, callback):
        self._stop_callbacks.append(callback)
        return lambda: self._stop_callbacks.remove(callback) if callback in self._stop_callbacks else None


@pytest.fixture
def make_config(tmp_path):
    def make(**overrides) -> Config:
        workspace = tmp_path / "work"
        workspace.mkdir(exist_ok=True)
        values = {
            "effort": "medium",
            "max_tokens": 64000,
            "max_steps": 10,
            "workspace": workspace,
            "data_dir": tmp_path / "data",
            "env_file": workspace / ".env",
            "web": True,
        }
        return Config(**{**values, **overrides})

    return make


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    """让测试和这台电脑的真实环境隔开。"""
    # 请求都发往本机的假 API，不能被代理设置带到别处去
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    # 光清掉上面这些还不够：环境变量里没有代理时，macOS 上会改去读"系统设置"里的代理，
    # 而且不会带上那边"忽略这些主机"的例外，连 127.0.0.1 也会被交给代理。NO_PROXY=* 让所有请求都直连
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    # 测试看不到真实的 API key；测试里设置的 key 也会在测试结束后被清掉
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.setenv(name, "")
    # 调用 load_config() 的测试（命令行）不能读到项目里真正的 .env：换成临时目录里一个不存在的文件
    monkeypatch.setenv("HARNESS_ENV_FILE", str(tmp_path / "no-such.env"))
    # 数据目录也一样：没设的话 load_config() 用的是仓库里真正的 data/（用户的会话、设置），测试里换个语言就写进去了。
    # 每个测试默认一个临时的（还不存在，用到才建）；要看"默认在哪儿"的测试自己 delenv
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "harness-data"))
    monkeypatch.delenv("TELEGRAM_API_BASE", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)  # 语音转文字的接口地址（见 Config.transcribe_api）
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    # Windows 上 os.path.expanduser 看的是 USERPROFILE（不是 HOME，也不经过 Path.home）：一起换掉，免得读到这台电脑真正的家目录
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    # "从其他工具导入"会看这几个环境变量（CODEX_HOME 这些）找别的工具的配置：清掉，只看假的家目录
    for name in import_places.VARIABLES:
        monkeypatch.delenv(name, raising=False)
    # 界面语言是整个程序共用的一个值（见 harness/i18n.py）。每个测试都从中文开始，测试里换了语言也会被换回来
    monkeypatch.setattr(i18n, "current", i18n.DEFAULT)
    # 自定义模型接口的登记也是整个程序共用的（build_app 交给 providers.use_custom）：每个测试都从"一个都没有"开始，
    # 上一个测试的接口不会留到下一个测试里
    monkeypatch.setattr(providers, "_custom", None)
    # 操作整台电脑：测试永远碰不到真的屏幕、鼠标和键盘。"这台电脑上用哪个后端"一律回答"用不了"，
    # 要测的换成假的（fake_desktop.install）；只在虚拟屏幕（Xvfb）上测真的后端（test_desktop_x11.py 自己建它）
    monkeypatch.setattr(desktop_backend, "detect", lambda: (None, desktop_backend.PLATFORM))
    # 读网页（harness/fetch/）：测试永远不解析真的域名、不连真的网站。要测的给 Fetcher 一个假的 resolve
    # （和只放行本地假网站的 policy，见 tests/fetch_rig.py）；真的地址规矩只拿地址本身测（tests/test_fetch_guard.py）
    def no_dns(host, port):
        raise OSError(f"测试里不许解析真的域名：{host}")

    monkeypatch.setattr(fetch_client, "system_resolve", no_dns)
    # 命令的沙箱：测试里默认用假的（能用，只记下"本该在沙箱里"，命令照原样运行），不建真的沙箱。
    # 真的 sandbox-exec、bwrap 只在 test_sandbox_macos.py、test_sandbox_linux.py 里测
    fake_sandbox.install(monkeypatch)
    # 后台服务：测试永远不在这台电脑上真的装上、开起一个服务（会改动系统，还可能对着真正的数据再开一个智狐）。
    # launchctl、systemctl、journalctl 一律不许运行，浏览器也不许打开；要测的给一个假的系统（tests/test_service.py 的 FakeSystem）
    def forbidden(*args, **kwargs):
        raise AssertionError(f"测试里不许运行真的系统命令或者打开浏览器：{args}")

    # Windows 上收紧访问权限（winacl.py）要运行 icacls：测试里换成什么都不做的假的（每建一个临时目录就运行一次太慢），
    # 真的 icacls 只在 tests/test_winacl.py 里、只在 Windows 上测
    monkeypatch.setattr(winacl, "runner", lambda args: (0, '"test\\me","S-1-5-21-1-2-3-1001"'))
    winacl.forget()
    # "只读的命令不用问"在 Windows 上整个关着（每条都问，见 harness/commands/__init__.py）。测拆词、名单的那几百个测试
    # 在 Windows 上照样要跑（路径的检查在那里才真正有 Windows 的样子），所以默认按 macOS、Linux 的规矩判断；
    # 测 Windows 上"一律不算只读"的测试自己把它换回 True（tests/test_commands.py 的 test_on_windows_nothing_is_readonly）
    monkeypatch.setattr(commands_module, "WINDOWS", False)
    monkeypatch.setattr(service_base, "run_command", forbidden)
    monkeypatch.setattr(service_base, "run_attached", forbidden)
    monkeypatch.setattr(service_cli, "_open_browser", forbidden)


@pytest.fixture
def start(make_config):
    """启动一个假 API，并组装出一个连着它的 agent。"""
    apis: list[FakeAPI] = []

    def start(*replies, ui=None, approve: bool = True, feedback: str = "", **config_overrides):
        api = FakeAPI(list(replies)).start()
        apis.append(api)
        config = make_config(**config_overrides)
        ui = ui or RecordingUI(approve, feedback)
        client = anthropic.Anthropic(api_key="test-key", base_url=api.base_url, max_retries=0)
        app = build_app(config, ui, client_factory=lambda provider: client)
        app.agent.clock = lambda: datetime(2026, 11, 20, 14, 3)
        return SimpleNamespace(agent=app.agent, app=app, api=api, ui=ui, config=config, todos=app.todos)

    yield start
    for api in apis:
        api.stop()


@pytest.fixture
def start_openai(make_config):
    """和 start 一样，但用的是 DeepSeek（OpenAI 格式），后面连着 OpenAI 格式的假服务器。

    start 和 start_openai 用的是同一个数据目录，所以在一个测试里先后调用两次，就相当于把程序重启了一遍。
    """
    servers: list[FakeOpenAI] = []

    def start(*replies, approve: bool = True, feedback: str = "", key: str = "test-key", **config_overrides):
        api = FakeOpenAI(list(replies)).start()
        servers.append(api)
        config = make_config(default_provider="deepseek", default_model="deepseek-flash", **config_overrides)
        ui = RecordingUI(approve, feedback)
        client = openai.OpenAI(api_key=key, base_url=api.base_url, max_retries=0)
        app = build_app(config, ui, client_factory=lambda provider: client)
        app.agent.clock = lambda: datetime(2026, 11, 20, 14, 3)
        return SimpleNamespace(agent=app.agent, app=app, api=api, ui=ui, config=config, todos=app.todos)

    yield start
    for api in servers:
        api.stop()
