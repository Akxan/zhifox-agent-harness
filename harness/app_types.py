from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Sequence
    from typing import Any, Protocol

    from .agent import Agent
    from .agents import AgentStore, CustomAgent
    from .agents.runner import Delegator
    from .app_chat import Chat
    from .browser.manager import BrowserManager
    from .config import Config
    from .conversations import Conversation, ConversationStore
    from .desktop.manager import DesktopManager
    from .endpoints import EndpointStore
    from .fetch.client import Fetcher
    from .fetch.session import FetchSession
    from .hooks import Hooks
    from .ledger import Ledger
    from .mcp import McpManager
    from .permissions import Permissions
    from .providers import ClientFactory
    from .sandbox import Layout
    from .settings import Settings
    from .skills import SkillStore
    from .tools import ToolRegistry
    from .tools.fetch import FetchTool
    from .undo.changes import FileChanges

    class AppPart(Protocol):
        """混进 App 的一组方法能用到的东西。"""

        config: Config
        settings: Settings
        tools: ToolRegistry
        client_factory: ClientFactory
        store: ConversationStore
        ledger: Ledger
        skills: SkillStore
        mcp: McpManager
        agents: AgentStore
        delegator: Delegator
        browser: BrowserManager
        desktop: DesktopManager
        hooks: Hooks
        sandbox_layout: Layout
        endpoints: EndpointStore
        changes: FileChanges
        permissions: Permissions
        redact: Any
        fallbacks: Callable[[str, str], list[str]] | None
        fetcher: Fetcher
        chat: Chat
        warnings: list[str]
        _viewing: threading.Lock

        system_prompt: str = ""

        @property
        def agent(self) -> Agent: ...
        @property
        def conversation(self) -> Conversation: ...
        @property
        def persona(self) -> CustomAgent | None: ...
        def chats(self) -> list[Chat]: ...

        def _fresh_desktop(self, chat: Chat) -> None: ...
        def _fresh_fetch(self, chat: Chat) -> None: ...
        def _fresh_browser(self, chat: Chat) -> None: ...

        def _new(self, provider_id: str, model: str, persona: CustomAgent | None) -> Conversation: ...

        def fresh_tools(self, chat: Chat | None = None) -> None: ...
        def conversation_tools(self, persona: CustomAgent | None, mcp_tools: Sequence[Any] = (), channel: str | None = None, chat: Chat | None = None) -> ToolRegistry: ...
        def _fetch_tool(self, session: Callable[[], FetchSession], turn: Callable[[], object]) -> FetchTool: ...
        def refresh_tools(self, chat: Chat | None = None) -> None: ...

        def _backend_for(self, provider_id: str, model: str, persona: CustomAgent | None = None, channel: str | None = None, chat: Chat | None = None) -> Any: ...
        def _remember_choice(self, provider_id: str, model: str) -> None: ...

        def prices(self) -> dict[str, dict]: ...

else:

    class AppPart:
        """运行时什么都没有：上面的那些只是写给 mypy 看的类型。"""
