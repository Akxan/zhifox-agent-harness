from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import anthropic
import openai
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fake_api import FakeAPI
from fake_openai import FakeOpenAI

from harness import i18n
from harness.app import build_app
from harness.config import Config
from harness.desktop import backend as desktop_backend
from harness.service import base as service_base
from harness.service import cli as service_cli


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
    monkeypatch.delenv("TELEGRAM_API_BASE", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    # 界面语言是整个程序共用的一个值（见 harness/i18n.py）。每个测试都从中文开始，测试里换了语言也会被换回来
    monkeypatch.setattr(i18n, "current", i18n.DEFAULT)
    # 操作整台电脑：测试永远碰不到真的屏幕、鼠标和键盘。"这台电脑上用哪个后端"一律回答"用不了"，
    # 要测的换成假的（fake_desktop.install）；只在虚拟屏幕（Xvfb）上测真的后端（test_desktop_x11.py 自己建它）
    monkeypatch.setattr(desktop_backend, "detect", lambda: (None, desktop_backend.PLATFORM))
    # 后台服务：测试永远不在这台电脑上真的装上、开起一个服务（会改动系统，还可能对着真正的数据再开一个智狐）。
    # launchctl、systemctl、journalctl 一律不许运行，浏览器也不许打开；要测的给一个假的系统（tests/test_service.py 的 FakeSystem）
    def forbidden(*args, **kwargs):
        raise AssertionError(f"测试里不许运行真的系统命令或者打开浏览器：{args}")

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
