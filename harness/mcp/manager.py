"""管着所有 MCP 服务器：启动、停止、重启，记着每一个现在怎么样，替模型调用它们的工具。

每个服务器在自己的后台线程里连接。一个启动很慢（npx 第一次要下载）或者干脆坏掉的服务器，
只会让它自己那一行显示"连接中"或"失败"，不会卡住界面，也不会挡住你和助手的对话。

## 工具在一段对话里是"冻结"的

各家的 API 每次请求都要整份发送工具说明书，并且对话一开头就是它（见 providers/claude.py 的说明）。
一段对话进行到一半，说明书变了，Claude 的缓存就全部作废，它之前的思考内容也通不过校验。
而 MCP 服务器随时可能连上、断开、告诉我们"工具列表变了"。

所以：一段对话发出第一条消息的那一刻，把当时已经连上的服务器的工具拍一张"快照"（Snapshot），
这段对话从头到尾只用这张快照。之后连上的服务器、新出现的工具，从下一段新对话开始才有。
对话进行中，快照里某个服务器断了，调用它的工具会得到一个清楚的错误，模型会换个办法。
"""

from __future__ import annotations

import atexit
import hashlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..i18n import message_of, t
from ..safe import to_json
from ..tools.base import Tool, ToolError, ToolOutput
from . import bridge
from .client import McpClient
from .config import (
    DEFAULT_TIMEOUT,
    ConfigError,
    McpConfig,
    display,
    expand,
    from_form,
    transport_of,
)
from .http import HttpTransport
from .jsonrpc import Cancelled, McpError, TimedOut
from .log import ServerLog
from .stdio import StdioTransport

CONNECT_TIMEOUT = 60  # 从启动到握手完成最多等这么久（npx 第一次运行要下载，比较慢）
LIST_TIMEOUT = 30
MAX_SNAPSHOT_TOOLS = 100  # 一段对话最多带这么多个 MCP 工具。OpenAI 一次请求最多 128 个工具，还要给自己的工具留位置
CRASH_WINDOW = 600  # 这么多秒之内……
MAX_CRASHES = 3  # ……意外退出这么多次，就不再自动重启了（免得一个起来就崩的服务器没完没了地重启）
RESTART_DELAY = 2


@dataclass
class Snapshot:
    """一段对话开始时拍下的 MCP 工具。这段对话从头到尾只用它，见文件开头的说明。"""

    tools: list[Tool] = field(default_factory=list)
    names: dict[str, tuple[str, str]] = field(default_factory=dict)  # 给模型看的名字 → (服务器, 服务器那边的工具名)
    prints: dict[str, str] = field(default_factory=dict)  # 服务器 → 拍快照时它的工具列表的"指纹"
    dropped: int = 0  # 超过上限、没有放进来的工具有几个


class Server:
    """一个服务器现在的情况。"""

    def __init__(self, name: str):
        self.name = name
        self.entry: dict | None = None  # 配置（写错了的话是 None，原因在 problem 里）
        self.problem = ""
        self.status = "disabled"  # disabled 停用 / connecting 连接中 / ready 已连接 / failed 失败
        self.error: str = ""  # 失败的原因（用 t() 写的，页面换语言时能跟着换）
        self.last_words = ""  # 失败时服务器自己在 stderr 上写的最后一行，常常就是真正的原因
        self.client: McpClient | None = None
        self.transport = None
        self.tools: list[dict] = []
        self.rejected: list[dict] = []
        self.log = ServerLog()
        self.generation = 0  # 每（重新）启动一次加一。后台线程靠它发现"我连的那一次已经过时了"
        self.crashes: list[float] = []
        self.print = ""  # 工具列表的指纹：列表变了它就变
        self.revision = 0  # 这个服务器的任何东西变了都加一，页面靠它知道要不要重新取详情
        self.refreshing = False
        self.refresh_again = False


class McpManager:
    def __init__(self, config: McpConfig):
        self.config = config
        self.on_change: Callable[[], None] = lambda: None  # 有服务器的状态变了。网页界面用它刷新页面
        self.active = Snapshot()  # 当前这段对话用的快照（见 App._use_tools）
        self._servers: dict[str, Server] = {}
        self._lock = threading.RLock()
        self._running: set[threading.Event] = set()  # 正在进行的工具调用，点"停止"时逐个取消
        self._closed = False
        self._exit_hooked = False

    # ---- 启动、停止 ---------------------------------------------------------

    def start(self) -> None:
        self.sync()

    def sync(self, restart: frozenset[str] = frozenset()) -> None:
        """让运行中的服务器和配置文件对上：新加的启动，删掉的停掉，配置改了的（还有 restart 里点名的）重启。"""
        configured = self.config.servers()
        with self._lock:
            if self._closed:
                return
            for name in [name for name in self._servers if name not in configured]:
                self._stop(self._servers.pop(name))
            for name, (entry, problem) in configured.items():
                server = self._servers.get(name)
                if server is None:
                    server = self._servers[name] = Server(name)
                elif entry == server.entry and problem == server.problem and name not in restart:
                    continue
                server.entry, server.problem = entry, problem
                if name in restart:
                    server.crashes = []
                self._apply(server)
            self._servers = {name: self._servers[name] for name in configured}  # 和配置文件里的顺序一致
        self.on_change()

    def _apply(self, server: Server) -> None:
        """按配置让一个服务器进入它该在的状态。调用的人要先拿到锁。"""
        self._stop(server)
        server.revision += 1
        server.tools, server.rejected, server.print, server.last_words = [], [], "", ""
        if server.problem:
            server.status, server.error = "failed", server.problem
        elif server.entry is None or not server.entry.get("enabled", True):
            server.status, server.error = "disabled", ""
        else:
            self._launch(server)

    def _launch(self, server: Server) -> None:
        if not self._exit_hooked:  # 第一次启动服务器时登记：程序退出时，别把启动过的服务器进程留在后台
            atexit.register(self.shutdown)
            self._exit_hooked = True
        server.generation += 1
        server.status, server.error, server.last_words = "connecting", "", ""
        threading.Thread(target=self._connect, args=(server, server.generation, dict(server.entry or {})), daemon=True).start()

    def _stop(self, server: Server) -> None:
        """断开一个服务器（在后台关，关进程最多要等几秒）。调用的人要先拿到锁。"""
        server.generation += 1  # 还在连接中的那个线程会发现自己过时了
        transport, server.transport, server.client = server.transport, None, None
        if transport is not None:
            threading.Thread(target=transport.close, daemon=True).start()

    def _connect(self, server: Server, generation: int, entry: dict) -> None:
        """在后台线程里：启动传输、握手、取工具列表。"""
        transport = None
        try:
            real = expand(entry)  # ${变量名} 换成环境变量的值
            if transport_of(real) == "stdio":
                transport = StdioTransport(real["command"], real.get("args", []), real.get("env", {}), real.get("cwd"), server.log)
            else:
                transport = HttpTransport(real["url"], real.get("headers", {}), server.log)
            with self._lock:
                if generation != server.generation or self._closed:
                    return
                server.transport = transport
            client = McpClient(transport, server.log, on_tools_changed=lambda: self._tools_changed(server, generation))
            transport.on_closed = lambda reason: self._closed_unexpectedly(server, generation, reason)
            client.connect(CONNECT_TIMEOUT)
            tools, rejected = client.list_tools(LIST_TIMEOUT)
        except (McpError, ConfigError) as exc:
            self._failed(server, generation, transport, message_of(exc))
            return
        except Exception as exc:  # noqa: BLE001 —— 后台线程里漏出去的异常没人看得见，服务器就会一直显示"连接中"
            self._failed(server, generation, transport, t("意外的错误：{error}", error=f"{type(exc).__name__}: {exc}"))
            return
        with self._lock:
            if generation != server.generation:
                threading.Thread(target=transport.close, daemon=True).start()
                return
            server.client, server.tools, server.rejected = client, tools, rejected
            server.status, server.error = "ready", ""
            server.print = _fingerprint(tools)
            server.revision += 1
        self.on_change()

    def _failed(self, server: Server, generation: int, transport, error: str) -> None:
        with self._lock:
            current = generation == server.generation
            if current:
                server.status, server.error = "failed", error
                tail = server.log.tail(1)
                server.last_words = tail[0][:300] if tail else ""
                server.transport = server.client = None
                server.revision += 1
                server.log.note(t("连接失败：{error}", error=error))
        if transport is not None:
            transport.close()
        if current:
            self.on_change()

    def _closed_unexpectedly(self, server: Server, generation: int, reason: str) -> None:
        """stdio 的服务器进程自己退出了。规范建议客户端把它重新启动；连续崩溃的就不再重启了。"""
        with self._lock:
            if generation != server.generation or self._closed or server.status != "ready":
                return  # 还在连接中就退出了：由 _connect 报告失败，不在这里重启
            now = time.monotonic()
            server.crashes = [moment for moment in server.crashes if now - moment < CRASH_WINDOW] + [now]
            server.transport = server.client = None
            server.revision += 1
            if len(server.crashes) >= MAX_CRASHES:
                server.status = "failed"
                server.error = t("{reason}它在 {minutes} 分钟里已经意外退出了 {n} 次，不再自动重启。点“重启”可以再试。", reason=reason, minutes=CRASH_WINDOW // 60, n=len(server.crashes))
            else:
                server.status = "connecting"
                server.error = t("{reason}正在自动重启。", reason=reason)
                server.generation += 1
                timer = threading.Timer(RESTART_DELAY, self._relaunch, args=(server, server.generation))
                timer.daemon = True
                timer.start()
        self.on_change()

    def _relaunch(self, server: Server, generation: int) -> None:
        with self._lock:
            if generation != server.generation or self._closed:
                return
            self._launch(server)

    def _tools_changed(self, server: Server, generation: int) -> None:
        """服务器说它的工具列表变了：在后台重新取一遍（这时可能正在读消息的线程里，不能在这里等）。"""
        with self._lock:
            if server.refreshing:
                server.refresh_again = True  # 正在取：取完再取一次
                return
            server.refreshing = True
        threading.Thread(target=self._refresh, args=(server, generation), daemon=True).start()

    def _refresh(self, server: Server, generation: int) -> None:
        try:
            while True:
                with self._lock:
                    client = server.client if generation == server.generation else None
                    server.refresh_again = False
                if client is None:
                    return
                try:
                    tools, rejected = client.list_tools(LIST_TIMEOUT)
                except McpError as exc:
                    server.log.note(t("重新取工具列表失败：{error}", error=message_of(exc)))
                    return
                with self._lock:
                    if generation != server.generation:
                        return
                    server.tools, server.rejected = tools, rejected
                    server.print = _fingerprint(tools)
                    server.revision += 1
                    again = server.refresh_again
                self.on_change()
                if not again:
                    return
        finally:
            with self._lock:
                server.refreshing = False

    def shutdown(self) -> None:
        """程序退出：关掉所有服务器，同时关，最多等几秒。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            transports = [server.transport for server in self._servers.values() if server.transport is not None]
            for server in self._servers.values():
                server.generation += 1
                server.transport = server.client = None
        closers = [threading.Thread(target=transport.close, daemon=True) for transport in transports]
        for closer in closers:
            closer.start()
        for closer in closers:
            closer.join(6)

    # ---- 网页上的操作 -------------------------------------------------------

    def refresh(self) -> None:
        """配置文件在磁盘上被改过（你在编辑器里改的），就照着新的内容同步一遍。"""
        if self.config.reload():
            self.sync()

    def restart(self, name: str) -> None:
        self.config.reload()
        if name not in self.config.servers():
            raise ConfigError(t("没有找到服务器“{name}”，它可能已经被删掉了。", name=name))
        self.sync(restart=frozenset({name}))

    def save(self, form: object, original: str | None) -> str:
        """添加或者编辑一个服务器（页面上的表单）。返回保存后的名字。保存完马上按新的配置连接。"""
        old = self.config.get(original) if original else None
        if original and old is None:
            raise ConfigError(t("没有找到服务器“{name}”，它可能已经被删掉了。", name=original))
        name = form.get("name") if isinstance(form, dict) else None
        if not isinstance(name, str):
            raise ConfigError(t("参数不正确"))
        entry = from_form(form, old)
        self.config.put(name.strip(), entry, original)
        self.sync(restart=frozenset({name.strip()}))
        return name.strip()

    def remove(self, name: str) -> None:
        self.config.remove(name)
        self.sync()

    def set_enabled(self, name: str, enabled: bool) -> None:
        self.config.set_enabled(name, enabled)
        self.sync(restart=frozenset({name}) if enabled else frozenset())

    def set_approval(self, name: str, tool: str | None, value: str) -> None:
        """改"调用前问不问你"。不用重新连接；正在进行的对话里，下一次调用就按新的来。"""
        self.config.set_approval(name, tool, value)
        with self._lock:
            server = self._servers.get(name)
            if server is not None:
                server.entry = self.config.get(name)
                server.revision += 1
        self.on_change()

    # ---- 给页面看的 ---------------------------------------------------------

    def status(self, started: bool) -> list[dict]:
        """每个服务器的概况（发给页面的状态里用）。started：当前这段对话是不是已经开始了（快照已经拍了）。"""
        with self._lock:
            servers = list(self._servers.values())
            active = self.active
        listed = []
        for server in servers:
            entry = server.entry or {}
            in_use = sum(1 for _, (owner, _tool) in active.names.items() if owner == server.name) if started else 0
            listed.append(
                {
                    "name": server.name,
                    "enabled": server.entry is not None and entry.get("enabled", True),
                    "status": server.status,
                    "error": server.error,
                    "last_words": server.last_words,
                    "tools": len(server.tools),
                    "rejected": len(server.rejected),
                    "approval": (entry.get("approval") or {}).get("default", "ask"),
                    "revision": server.revision,
                    "log": server.log.revision,
                    "config": display(entry) if server.entry else None,
                    "in_use": in_use,
                    # 当前这段对话里用的，和现在服务器上的不一样了（刚连上、工具变了、断开了）：下一段新对话才会用上新的
                    "stale": started and active.prints.get(server.name, "") != (server.print if server.status == "ready" else ""),
                }
            )
        return listed

    def detail(self, name: str) -> dict:
        """一个服务器的详情：全部工具（带说明和"问不问你"）、日志。点开一个服务器时页面来取。"""
        with self._lock:
            server = self._servers.get(name)
            if server is None:
                raise ConfigError(t("没有找到服务器“{name}”，它可能已经被删掉了。", name=name))
            client, tools, rejected = server.client, list(server.tools), list(server.rejected)
            exposed = {remote: shown for shown, (owner, remote) in self.active.names.items() if owner == name}
        taken: set[str] = set()
        listed = []
        for tool in tools:
            hints = tool.get("annotations") or {}
            listed.append(
                {
                    "name": tool["name"],
                    "title": tool.get("title", ""),
                    "description": tool.get("description", ""),
                    "approval": self.config.approval(name, tool["name"]),
                    "exposed": exposed.get(tool["name"]) or bridge.exposed_name(name, tool["name"], taken),
                    # 服务器自己说的"只读""会删改东西"，没有经过验证，只用来显示，不影响要不要问你
                    "read_only": hints.get("readOnlyHint") is True,
                    "destructive": hints.get("destructiveHint") is True,
                }
            )
        return {
            "name": name,
            "tools": listed,
            "rejected": rejected,
            "log": server.log.lines(),
            "era": client.era if client else "",
            "version": client.version if client else "",
            "server_info": dict(client.server_info) if client else {},
            "instructions": client.instructions if client else "",
        }

    # ---- 给对话用的 ---------------------------------------------------------

    def snapshot(self) -> Snapshot:
        """拍一张快照：现在已经连上的服务器的全部工具，做成智狐的工具。按服务器的名字排好，每次的顺序都一样。"""
        with self._lock:
            ready = sorted((server for server in self._servers.values() if server.status == "ready"), key=lambda server: server.name)
            plan = [(server.name, list(server.tools), server.print) for server in ready]
        shot, taken = Snapshot(), set()
        for name, tools, fingerprint in plan:
            shot.prints[name] = fingerprint
            for tool in tools:
                if len(shot.tools) >= MAX_SNAPSHOT_TOOLS:
                    shot.dropped += 1
                    continue
                shown = bridge.exposed_name(name, tool["name"], taken)
                taken.add(shown)

                def run(arguments: dict, server: str = name, remote: str = tool["name"]) -> ToolOutput:
                    return self.call(server, remote, arguments)

                shot.tools.append(bridge.make_tool(name, tool, shown, run))
                shot.names[shown] = (name, tool["name"])
        return shot

    def resolve(self, shown: str) -> tuple[str, str] | None:
        """当前对话里的一个工具名，是哪个服务器的哪个工具。不是 MCP 工具就返回 None。"""
        return self.active.names.get(shown)

    def trusted(self, server: str, tool: str) -> bool:
        """你是不是把这个工具（或者整个服务器）设成了"不用问"。"""
        return self.config.approval(server, tool) == "auto"

    def call(self, server_name: str, tool: str, arguments: object) -> ToolOutput:
        """替模型调用一个工具。出了问题抛出 ToolError，原因会交给模型（所以是中文，见 bridge.chinese）。"""
        if not isinstance(arguments, dict):
            raise ToolError("参数必须是一个 JSON 对象。")
        with self._lock:
            server = self._servers.get(server_name)
            client = server.client if server is not None and server.status == "ready" else None
            timeout = (server.entry or {}).get("timeout", DEFAULT_TIMEOUT) if server is not None else DEFAULT_TIMEOUT
        if client is None:
            state = "已经被删掉了" if server is None else _STATE_FOR_MODEL.get(server.status, server.status)
            raise ToolError(f"MCP 服务器“{server_name}”{state}，这个工具现在用不了。可以请用户到“扩展 → MCP”里看看它的状态，或者换个办法。")
        stop = threading.Event()
        with self._lock:
            self._running.add(stop)
        try:
            result = client.call_tool(tool, arguments, timeout, stop)
        except Cancelled as exc:
            raise ToolError("用户中断了这次调用。") from exc
        except TimedOut as exc:
            raise ToolError(f"等了 {round(timeout)} 秒，MCP 服务器“{server_name}”还没有结果，已经取消了这次调用。") from exc
        except McpError as exc:
            raise ToolError(f"调用 MCP 服务器“{server_name}”的工具 {tool} 失败：{bridge.chinese(exc)}") from exc
        finally:
            with self._lock:
                self._running.discard(stop)
        return bridge.render(result)

    def cancel_running(self) -> None:
        """用户点了"停止"：取消正在进行的工具调用（等待中的那个线程会立刻放弃，并通知服务器）。"""
        with self._lock:
            for stop in self._running:
                stop.set()


_STATE_FOR_MODEL = {"connecting": "还在连接", "failed": "连接失败了", "disabled": "被用户停用了"}


def _fingerprint(tools: list[dict]) -> str:
    return hashlib.sha1(to_json(tools).encode()).hexdigest()[:12]
