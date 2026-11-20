from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import anthropic
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fake_api import FakeAPI

from harness.app import build_app
from harness.config import Config


class RecordingUI:
    """测试用的界面：不显示任何东西，只把发生的事记下来。确认请求按预设的答案回答。"""

    def __init__(self, approve: bool = True, feedback: str = ""):
        self.approve = approve
        self.feedback = feedback
        self.streamed: list[tuple[str, str]] = []
        self.calls: list[str] = []
        self.results: list[tuple[str, bool]] = []
        self.confirmations: list[str] = []
        self.infos: list[str] = []

    def thinking(self, text: str) -> None:
        self.streamed.append(("thinking", text))

    def text(self, text: str) -> None:
        self.streamed.append(("text", text))

    def notice(self, level: str, text: str) -> None:
        self.infos.append(text)

    def end_stream(self) -> None:
        pass

    def tool_call(self, name: str, tool_input: object) -> None:
        self.calls.append(name)

    def tool_result(self, text: str, is_error: bool) -> None:
        self.results.append((text, is_error))

    def confirm(self, description: str) -> tuple[bool, str]:
        self.confirmations.append(description)
        return self.approve, self.feedback

    def info(self, text: str) -> None:
        self.infos.append(text)


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
    # 测试看不到真实的 API key；测试里设置的 key 也会在测试结束后被清掉
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.setenv(name, "")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")


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
