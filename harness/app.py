"""组装：把配置、工具、权限、模型和循环拼成一个可以运行的助手。

终端版和网页版从这里拿到的是同一套东西，区别只在于传进来的界面（ui）不同。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .agent import Agent
from .config import Config
from .permissions import Permissions
from .prompt import build_system_prompt
from .providers import (
    PROVIDERS,
    ClientFactory,
    Provider,
    ProviderError,
    has_key,
    list_models,
    make_backend,
    make_client,
)
from .settings import Settings, save_api_key
from .tools import ToolRegistry, build_registry
from .tools.files import FileEditor
from .tools.shell import Shell
from .tools.todo import TodoStore


@dataclass
class App:
    config: Config
    settings: Settings
    agent: Agent
    todos: TodoStore
    tools: ToolRegistry
    shell: Shell
    system_prompt: str
    client_factory: ClientFactory

    @property
    def provider(self) -> Provider:
        """当前使用的是哪一家。"""
        return PROVIDERS[self.settings.provider]

    def select_model(self, provider_id: str, model: str) -> bool:
        """换模型。返回 True 表示对话可以接着进行，False 表示开始了一段新对话。

        同一家之内换模型，历史的格式不变，可以接着聊。换到另一家时，
        历史的格式不通用，只能从头开始（记忆和待办在磁盘上，不受影响）。
        """
        provider = PROVIDERS[provider_id]
        same_provider = provider_id == self.agent.backend.provider
        if same_provider:
            self.agent.backend.model = model
        else:
            self.agent.backend = make_backend(
                provider, model, self.config, self.tools, self.system_prompt, self.client_factory(provider)
            )
        self.settings.provider, self.settings.model = provider_id, model
        self.settings.save()
        return same_provider

    def set_api_key(self, provider_id: str, key: str) -> None:
        """保存（或删除）一家的 API key，并让它立即生效。"""
        provider = PROVIDERS[provider_id]
        save_api_key(self.config.env_file, provider.key_env, key)
        if provider_id == self.agent.backend.provider:
            self.agent.backend.client = self.client_factory(provider)  # 正在用这一家：换上带新 key 的连接

    def sync_models(self, provider_id: str) -> list[str]:
        """向一家的服务器询问有哪些模型，并把列表记下来。"""
        provider = PROVIDERS[provider_id]
        if not has_key(provider):
            raise ProviderError(f"还没有设置 {provider.label} 的 API key。")
        models = list_models(provider, self.client_factory(provider))
        self.settings.models[provider_id] = models
        self.settings.save()
        return models


def build_app(config: Config, ui: Any, client_factory: ClientFactory = make_client) -> App:
    settings = Settings(config.settings_file, config.default_provider, config.default_model)
    if settings.provider not in PROVIDERS:
        settings.provider, settings.model = "anthropic", "claude-opus-5-5"

    files = FileEditor(config.workspace, config.backup_dir, protected=[config.env_file])
    todos = TodoStore(config.todo_file)
    shell = Shell(config.workspace)
    tools = build_registry(config, files, todos, shell)
    system_prompt = build_system_prompt(config)

    provider = PROVIDERS[settings.provider]
    backend = make_backend(provider, settings.model, config, tools, system_prompt, client_factory(provider))
    agent = Agent(backend=backend, config=config, tools=tools, permissions=Permissions(files), ui=ui)
    return App(
        config=config,
        settings=settings,
        agent=agent,
        todos=todos,
        tools=tools,
        shell=shell,
        system_prompt=system_prompt,
        client_factory=client_factory,
    )


def run_turn(agent: Agent, ui: Any, text: str) -> None:
    """跑一轮对话。出错后程序不退出，把原因显示出来，可以接着聊。"""
    try:
        agent.run_turn(text)
    except KeyboardInterrupt:
        ui.info("已中断。")
    except ProviderError as exc:
        ui.error(str(exc))
