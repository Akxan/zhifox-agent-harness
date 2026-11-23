"""AppPart：混进 App 的每一组方法（AppChats、AppTools……）能用到的 App 的东西，写给类型检查（mypy）看。

为什么要有这个文件，和 webui/webapp_types.py 是同一个道理：App 由几组方法拼成（见 app.py 开头），每一组写在自己的 app_xxx.py 里，
是一个只有方法的类。这些方法里到处是 self.config、self.chat、self._backend_for(...)：状态是 App 的字段（在 app.py），
方法可能写在 app.py，也可能写在另一组里（app_conversations.py 的 _continue 要用 app_tools.py 的 fresh_tools）。
单看 AppConversations 这个类，mypy 不知道 self 上有这些，只能报"没有这个属性"；用 Any 糊过去或者整个文件不查，又等于这些文件没人查。

办法：AppChats 这些类都继承 AppPart。它是一个 Protocol，列出"别的文件里定义、这一组要用"的状态和方法的类型：
  - 状态只写类型，值是 App 的字段（说明也写在那里）。mypy 会拿 App 的字段和这里的类型对一遍。
  - 方法只写签名，函数体是 ...。真正的实现在 app.py 或某一组里，mypy 会把实现当成对这一行的覆盖来检查：
    签名改了、这里没跟着改，会报 [override]；这里写了、哪儿都没实现，build_app 里建 App 的那一行会报 [abstract]
    （Protocol 里空的方法体算"还没实现"）。所以这里的签名不会悄悄和实现对不上。
  - 混进来的方法会整个换掉的状态（self.system_prompt = ...）要写一个值，只是往里加东西的（self.warnings.append(...)）不用。
    原因是 mypy 的一个规矩：Protocol 里只写类型、不写值的属性，算"等着子类来定义"的；这时混进来的那一组里的一次赋值，
    就成了那一组自己新定义了一个属性，类型照赋的值去猜（赋的是 None 就猜成 None，赋的是 [] 有时干脆报 Cannot determine type），
    和这里写的、和 App 的字段对不上。写上一个值，它就是"已经有了"的属性，那次赋值照这里写的类型检查。
    值本身用不上：运行时根本没有这个 Protocol（真正的初值是 App 的字段）。
  - 这些只在类型检查时存在：运行时 AppPart 是下面那个空的类，App 的行为、MRO 里的方法和不拆开的时候一模一样。

只列出被别的文件用到的：一组方法只在自己的文件里互相调用的，不用写进来；App 自己（app.py）用到各组的方法，
它本来就继承了它们，也不用写。新加一组方法（AppXxx）时：
  1. class AppXxx(AppPart)，加进 App 的基类；
  2. 用到了 App 的字段或者别的文件里的方法，mypy 报"没有这个属性"时，照着那个字段或者方法的定义在下面补一行
     （字段补类型、方法补签名，放进对应的那一节）；这一组会整个换掉的字段，照上面说的写一个值；
  3. 新的状态加成 App 的字段、写明是给谁用的。
"""

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
        """混进 App 的一组方法能用到的东西。值和说明在 App 的字段里，实现在各自写明的文件里。"""

        # ---- 状态（App 的字段，app.py）----
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

        # 混进来的方法会整个换掉的（见文件开头）：app_models.py 的 set_prompt
        system_prompt: str = ""

        # ---- app.py：你正看着的这一段 ----
        @property
        def agent(self) -> Agent: ...
        @property
        def conversation(self) -> Conversation: ...
        @property
        def persona(self) -> CustomAgent | None: ...
        def chats(self) -> list[Chat]: ...

        # ---- app_chat.py（AppChats）----
        def _fresh_desktop(self, chat: Chat) -> None: ...
        def _fresh_fetch(self, chat: Chat) -> None: ...
        def _fresh_browser(self, chat: Chat) -> None: ...

        # ---- app_conversations.py（AppConversations）----
        def _new(self, provider_id: str, model: str, persona: CustomAgent | None) -> Conversation: ...

        # ---- app_tools.py（AppTools）----
        def fresh_tools(self, chat: Chat | None = None) -> None: ...
        def conversation_tools(self, persona: CustomAgent | None, mcp_tools: Sequence[Any] = (), channel: str | None = None, chat: Chat | None = None) -> ToolRegistry: ...
        def _fetch_tool(self, session: Callable[[], FetchSession], turn: Callable[[], object]) -> FetchTool: ...
        def refresh_tools(self, chat: Chat | None = None) -> None: ...

        # ---- app_models.py（AppModels）----
        def _backend_for(self, provider_id: str, model: str, persona: CustomAgent | None = None, channel: str | None = None, chat: Chat | None = None) -> Any: ...
        def _remember_choice(self, provider_id: str, model: str) -> None: ...

        # ---- app_prices.py（AppPrices）----
        def prices(self) -> dict[str, dict]: ...

else:

    class AppPart:
        """运行时什么都没有：上面的那些只是写给 mypy 看的类型。"""
