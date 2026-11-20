from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import anthropic
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fake_api import FakeAPI

from harness.agent import Agent
from harness.config import Config
from harness.permissions import Permissions
from harness.tools import build_registry
from harness.tools.files import FileEditor
from harness.tools.todo import TodoStore


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

    def stream_event(self, event) -> None:
        if event.type == "text":
            self.streamed.append(("text", event.text))
        elif event.type == "thinking":
            self.streamed.append(("thinking", event.thinking))

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
            "model": "claude-opus-5-5",
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


@pytest.fixture
def start(make_config, monkeypatch):
    """启动一个假 API，并组装出一个连着它的 agent。"""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    apis: list[FakeAPI] = []

    def start(*replies, ui=None, approve: bool = True, feedback: str = "", **config_overrides):
        api = FakeAPI(list(replies)).start()
        apis.append(api)
        config = make_config(**config_overrides)
        files = FileEditor(config.workspace, config.backup_dir, protected=[config.env_file])
        todos = TodoStore(config.todo_file)
        ui = ui or RecordingUI(approve, feedback)
        agent = Agent(
            client=anthropic.Anthropic(api_key="test-key", base_url=api.base_url, max_retries=0),
            config=config,
            tools=build_registry(config, files, todos),
            permissions=Permissions(files),
            ui=ui,
            system_prompt="测试用的系统提示词",
            clock=lambda: datetime(2026, 11, 20, 14, 3),
        )
        return SimpleNamespace(agent=agent, api=api, ui=ui, config=config, todos=todos)

    yield start
    for api in apis:
        api.stop()
