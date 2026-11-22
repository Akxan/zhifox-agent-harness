"""MCP 客户端：和一个 MCP 服务器"说上话"，列出它的工具，调用它的工具。

## 两代协议

MCP 在 2026-07-28 这一版有一次大改。这个客户端两代都会说（规范里叫 dual-era），因为现在外面的服务器两种都有：

  老一代（2025-11-25 及以前）  先握手：我们发 initialize（带上我们支持的版本和能力），服务器回答它选定的版本和
                               它的能力，我们再发一条 notifications/initialized，之后才能正常说话。
                               这一来一回定下的东西（版本、能力）在整个连接里都算数，是"有状态"的。
  新一代（2026-07-28）         没有握手，是"无状态"的：每个请求都在 params._meta 里自己带着协议版本、我们是谁、
                               我们有什么能力。服务器不记得上一个请求，所以随便哪个请求都能单独处理。
                               想事先知道服务器支持哪些版本，可以问一句 server/discover。

怎么知道对面是哪一代：先按新一代的规矩问一句 server/discover。
  - 回答了（或者回了一个只有新一代才有的错误码，比如"不支持这个版本"）：是新一代。
  - 回了别的错误、或者干脆不理我们（老一代的服务器在握手之前收到不认识的请求，有的报错、有的装没听见）：
    是老一代，改走 initialize 握手。
stdio 上，有的服务器启动很慢（npx 第一次要下载），一直不回答不一定是老一代。所以等一小会儿没回音，
就把 initialize 也发出去，两个请求谁先成功就按谁来（见 _race）。

## 这个客户端声明的能力

什么能力都不声明（capabilities 是空的）：不提供"根目录"（roots），不替服务器调用模型（sampling），
不帮服务器向你提问（elicitation）。所以服务器不会来要这些；老一代的服务器万一还是来要，我们回答"不支持"。
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable

from ..config import APP_NAME_LATIN
from ..i18n import t
from . import jsonrpc
from .http import SessionExpired, header_value
from .jsonrpc import McpError, RpcError, Slot
from .log import ServerLog

MODERN_VERSIONS = ("2026-07-28",)  # 新一代里我们认识的版本，越新越靠前
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")  # 老一代里我们认识的版本
CLIENT_INFO = {"name": APP_NAME_LATIN, "version": "0.1.0"}
PROBE_SECONDS = 10  # stdio 上等 server/discover 回音的时间，过了就把 initialize 也发出去
MAX_PAGES = 100  # 工具列表最多翻这么多页。服务器有 bug、一直给"下一页"时，不至于永远翻下去
MAX_TOOLS = 500  # 一个服务器最多收这么多个工具
_HEADER_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")  # 请求头名字里允许的字符（RFC 9110）


class McpClient:
    def __init__(self, transport, log: ServerLog, on_tools_changed: Callable[[], None] = lambda: None):
        self.transport = transport
        self.log = log
        self.on_tools_changed = on_tools_changed  # 服务器说"工具列表变了"时调用。在读消息的线程里，不能在里面等回答
        transport.on_notification = self._notified
        transport.on_request = self._asked
        self.era = ""  # "modern" 新一代，"legacy" 老一代
        self.version = ""  # 用的是哪个版本的协议
        self.capabilities: dict = {}  # 服务器说它有哪些能力
        self.server_info: dict = {}  # 服务器自报的名字和版本（没有经过验证，只用来显示）
        self.instructions = ""  # 服务器写给模型的使用说明（只显示给你看，不交给模型，原因见 README）
        self.param_headers: dict[str, list[tuple[tuple[str, ...], str]]] = {}  # HTTP：哪个工具的哪个参数要抄进请求头

    # ---- 连接 ---------------------------------------------------------------

    def connect(self, timeout: float) -> None:
        """启动传输，弄清对面是哪一代、用哪个版本，然后订阅"工具变了"的通知。"""
        self.transport.start()
        deadline = time.monotonic() + timeout
        if self.transport.name == "stdio":
            self._race(deadline)
        else:
            self._probe_http(deadline)
        self.log.note(t("已连接：{server}，协议 {version}（{era}）", server=self._server_label(), version=self.version, era=self.era))
        self._subscribe()

    def _meta(self, version: str | None = None) -> dict:
        """新一代协议每个请求都要带的"自我介绍"。"""
        return {
            "io.modelcontextprotocol/protocolVersion": version or self.version,
            "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
            "io.modelcontextprotocol/clientCapabilities": {},
        }

    def _discover_params(self) -> dict:
        return {"_meta": self._meta(MODERN_VERSIONS[0])}

    def _legacy_params(self) -> dict:
        return {"protocolVersion": LEGACY_VERSIONS[0], "capabilities": {}, "clientInfo": CLIENT_INFO}

    def _race(self, deadline: float) -> None:
        """stdio：先问 server/discover；一会儿没回音，再发 initialize，谁先成功按谁来。"""
        transport = self.transport
        transport.modern, transport.protocol_version = True, MODERN_VERSIONS[0]
        probe = transport.send("server/discover", self._discover_params())
        initialize: Slot | None = None
        if probe.done.wait(max(0.0, min(PROBE_SECONDS, deadline - time.monotonic()))):
            verdict = self._judge(probe)
            if verdict != "legacy":
                return
        while True:
            if probe.done.is_set() and probe.failure is None and self._judge(probe) == "modern":
                if initialize is not None:
                    transport.forget(initialize, cancel=False)  # 新一代的服务器会拒绝它，回答不用等了
                return
            if initialize is None:
                transport.modern, transport.protocol_version = False, ""
                initialize = transport.send("initialize", self._legacy_params())
            if initialize.done.is_set():
                if initialize.failure is None and "result" in (initialize.message or {}):
                    transport.forget(probe, cancel=False)
                    self._finish_legacy(initialize.outcome())
                    return
                if probe.done.is_set():
                    initialize.outcome()  # 两个都失败了：把 initialize 的原因报出来
            if time.monotonic() >= deadline:
                transport.forget(probe, cancel=True)
                transport.forget(initialize, cancel=True)
                if initialize.done.is_set():
                    initialize.outcome()  # initialize 早就失败了，只是一直在等 server/discover 的回音：报 initialize 的原因
                raise jsonrpc.TimedOut(t("服务器启动后一直没有回答（server/discover 和 initialize 都没有回音）。"))
            time.sleep(0.05)

    def _probe_http(self, deadline: float) -> None:
        """HTTP：先按新一代发 server/discover。回了 4xx、又不是新一代才有的错误，就是老一代。"""
        transport = self.transport
        transport.modern, transport.protocol_version = True, MODERN_VERSIONS[0]
        probe = transport.send("server/discover", self._discover_params(), timeout=max(1.0, deadline - time.monotonic()))
        try:
            jsonrpc.wait(probe, max(1.0, deadline - time.monotonic()))
        except jsonrpc.TimedOut:
            transport.forget(probe, cancel=True)
            raise
        except McpError:
            if isinstance(probe.failure, McpError) and getattr(probe.failure, "status", None) not in (400, 404, 405):
                raise  # 连不上、要登录：两代都一样过不去，不用再试 initialize
        if self._judge(probe) == "modern":
            return
        transport.modern, transport.protocol_version = False, ""
        self._finish_legacy(transport.request("initialize", self._legacy_params(), max(1.0, deadline - time.monotonic())))

    def _judge(self, probe: Slot) -> str:
        """看 server/discover 的结果判断对面是哪一代。是新一代的话，顺便记下版本和能力。返回 "modern" 或 "legacy"。"""
        if probe.failure is not None:
            if self.transport.name == "stdio" and not self.transport.alive:
                raise probe.failure  # 进程都退出了，不用再试
            return "legacy"
        message = probe.message or {}
        if "error" in message:
            problem = message["error"] if isinstance(message["error"], dict) else {}
            code = problem.get("code")
            if code not in jsonrpc.MODERN_ERRORS:
                return "legacy"
            if code == jsonrpc.UNSUPPORTED_VERSION:
                data = raw_data if isinstance(raw_data := problem.get("data"), dict) else {}
                return self._pick(data.get("supported"))
            raise RpcError(code, problem.get("message"), problem.get("data"))
        result = raw_result if isinstance(raw_result := message.get("result"), dict) else {}
        era = self._pick(result.get("supportedVersions"))
        if era == "modern":
            self.capabilities = raw_capabilities if isinstance(raw_capabilities := result.get("capabilities"), dict) else {}
            meta = raw_meta if isinstance(raw_meta := result.get("_meta"), dict) else {}
            info = meta.get("io.modelcontextprotocol/serverInfo")
            self.server_info = info if isinstance(info, dict) else {}
            self.instructions = raw_instructions if isinstance(raw_instructions := result.get("instructions"), str) else ""
        return era

    def _pick(self, supported: object) -> str:
        """从服务器支持的版本里挑一个我们也认识的。新一代优先；只有老一代的版本可选时走握手。"""
        versions = [item for item in supported if isinstance(item, str)] if isinstance(supported, list) else []
        for version in MODERN_VERSIONS:
            if version in versions:
                self.era, self.version = "modern", version
                self.transport.modern, self.transport.protocol_version = True, version
                return "modern"
        if any(version in versions for version in LEGACY_VERSIONS):
            return "legacy"
        raise McpError(t("服务器支持的协议版本（{versions}）智狐都不认识。", versions=", ".join(versions) or "—"))

    def _finish_legacy(self, result: dict) -> None:
        """老一代握手的后半段：检查服务器选的版本，记下它的能力，再发一条"我准备好了"。"""
        version = result.get("protocolVersion")
        if version not in LEGACY_VERSIONS:
            raise McpError(t("服务器要用 MCP 协议的 {version} 版，智狐不认识这个版本。", version=version))
        self.era, self.version = "legacy", str(version)
        self.capabilities = raw_capabilities if isinstance(raw_capabilities := result.get("capabilities"), dict) else {}
        self.server_info = raw_server_info if isinstance(raw_server_info := result.get("serverInfo"), dict) else {}
        self.instructions = raw_instructions if isinstance(raw_instructions := result.get("instructions"), str) else ""
        self.transport.protocol_version = self.version  # 老一代：握手之后的每个 HTTP 请求都带上选定的版本
        self.transport.notify("notifications/initialized")

    def _subscribe(self) -> None:
        """服务器说它的工具列表会变（listChanged），就请它变了之后通知我们。"""
        tools = self.capabilities.get("tools")
        if not (isinstance(tools, dict) and tools.get("listChanged")):
            return
        if self.era == "modern":
            self.transport.listen({"_meta": self._meta(), "notifications": {"toolsListChanged": True}})
        elif self.transport.name == "http":
            self.transport.listen({})  # 老一代的 HTTP：开一个 GET 连接等通知
        # 老一代的 stdio：通知会自己从 stdout 过来，什么都不用做

    def _server_label(self) -> str:
        name = self.server_info.get("name")
        version = self.server_info.get("version")
        return " ".join(str(part) for part in (name, version) if isinstance(part, str) and part) or "?"

    # ---- 请求 ---------------------------------------------------------------

    def _call(self, method: str, params: dict, timeout: float, stop: threading.Event | None = None, headers: dict | None = None) -> dict:
        """发一个请求。新一代给它带上 _meta；老一代的 HTTP 会话过期了就重新握手，再试一次。"""
        if self.era == "modern":
            params = {**params, "_meta": {**(params.get("_meta") or {}), **self._meta()}}
        try:
            return _complete(self.transport.request(method, params, timeout, stop, headers))
        except SessionExpired:
            self.log.note(t("会话过期了，重新握手。"))
            self.transport.session_id = None
            self.transport.protocol_version = ""
            self._finish_legacy(self.transport.request("initialize", self._legacy_params(), timeout))
            return _complete(self.transport.request(method, params, timeout, stop, headers))

    def ping(self, timeout: float = 10) -> float:
        """看看服务器还在不在，返回来回用了多少秒。新一代协议去掉了 ping，用 server/discover 代替。"""
        started = time.monotonic()
        if self.era == "modern":
            self._call("server/discover", {}, timeout)
        else:
            self._call("ping", {}, timeout)
        return time.monotonic() - started

    def list_tools(self, timeout: float) -> tuple[list[dict], list[dict]]:
        """列出服务器的全部工具（一页一页翻完）。返回（能用的工具，被拒绝的工具和原因）。"""
        tools: list[dict] = []
        rejected: list[dict] = []
        names: set[str] = set()
        cursor: object = None
        seen: set[str] = set()
        self.param_headers = {}
        for _ in range(MAX_PAGES):
            try:
                result = self._call("tools/list", {} if cursor is None else {"cursor": cursor}, timeout)
            except RpcError as exc:
                if exc.code == jsonrpc.METHOD_NOT_FOUND and cursor is None:
                    return [], []  # 这个服务器没有工具（它可能只提供资源或提示词）
                raise
            for raw in listed if isinstance(listed := result.get("tools"), list) else []:
                tool, problem = self._check_tool(raw)
                if problem:
                    rejected.append({"name": (tool.get("name") if isinstance(tool, dict) else None) or "?", "reason": problem})
                    self.log.note(t("没有收下工具 {name}：{reason}", name=rejected[-1]["name"], reason=problem))
                elif tool["name"] not in names and len(tools) < MAX_TOOLS:
                    names.add(tool["name"])
                    tools.append(tool)
            cursor = result.get("nextCursor")
            # 规范：只有没给 nextCursor（或者给的是 null）才是最后一页，空字符串也是一个合法的"下一页"
            if not isinstance(cursor, str):
                break
            if cursor in seen:
                self.log.note(t("服务器给的下一页和之前的重复了，不再往下翻。"))
                break
            seen.add(cursor)
        else:
            self.log.note(t("工具列表超过了 {n} 页，后面的没有再取。", n=MAX_PAGES))
        return tools, rejected

    def _check_tool(self, raw: object) -> tuple[dict, str]:
        """检查一个工具的定义。返回（整理好的工具，不收它的原因）；原因为空表示可以用。"""
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not raw["name"].strip():
            return (raw if isinstance(raw, dict) else {}), t("没有名字")
        name = raw["name"]
        if len(name) > 200:
            return raw, t("名字太长")
        schema = raw.get("inputSchema")
        if schema is None:
            schema = {"type": "object"}
        if not isinstance(schema, dict):
            return raw, t("参数说明（inputSchema）不是一个对象")
        tool = {
            "name": name,
            "title": raw["title"] if isinstance(raw.get("title"), str) else "",
            "description": raw["description"] if isinstance(raw.get("description"), str) else "",
            "inputSchema": schema,
            "annotations": raw["annotations"] if isinstance(raw.get("annotations"), dict) else {},
        }
        if self.transport.name == "http":
            headers, problem = _param_headers(schema)
            if problem:
                return tool, problem  # 规范要求：x-mcp-header 写得不对的工具整个不收
            if headers:
                self.param_headers[name] = headers
        return tool, ""

    def call_tool(self, name: str, arguments: dict, timeout: float, stop: threading.Event | None = None) -> dict:
        """调用一个工具，返回服务器给的结果（content、structuredContent、isError……）。"""
        headers: dict[str, str] = {}
        for path, header in self.param_headers.get(name, []):
            value: object = arguments
            for key in path:
                value = value.get(key) if isinstance(value, dict) else None
            if isinstance(value, bool):
                headers[f"Mcp-Param-{header}"] = "true" if value else "false"
            elif isinstance(value, int | str):
                headers[f"Mcp-Param-{header}"] = header_value(str(value))
        return self._call("tools/call", {"name": name, "arguments": arguments}, timeout, stop, headers)

    # ---- 服务器主动发来的 ----------------------------------------------------

    def _notified(self, message: dict) -> None:
        method = message["method"]
        params = raw_params if isinstance(raw_params := message.get("params"), dict) else {}
        if method == "notifications/tools/list_changed":
            self.log.note(t("服务器说它的工具列表变了。"))
            self.on_tools_changed()
        elif method == "notifications/message":  # 服务器的日志（老一代协议里常见）
            level, data = params.get("level", "info"), params.get("data", "")
            self.log.add(f"[{level}] {data if isinstance(data, str) else jsonrpc.brief(data)}", source="server")

    def _asked(self, message: dict) -> dict:
        """老一代的服务器反过来问我们。只回答 ping，别的一律"不支持"。"""
        if message["method"] == "ping":
            return {}
        raise RpcError(jsonrpc.METHOD_NOT_FOUND, f"{APP_NAME_LATIN} does not support {message['method']}")


def _complete(result: dict) -> dict:
    """新一代的回答里有 resultType。"complete" 是正常的结果；"input_required" 是服务器要我们补充信息（填表、登录），
    这一类来回我们还不支持。老一代的回答没有这个字段，按规范当作 "complete"。"""
    kind = result.get("resultType", "complete")
    if kind == "complete":
        return result
    if kind == "input_required":
        raise McpError(t("这个工具要你补充一些信息才能继续（比如填一个表单或者登录），智狐暂时还不支持这种来回，没法完成这次调用。"))
    raise McpError(t("服务器的回答类型是 {kind}，智狐不认识。", kind=jsonrpc.brief(kind)))


def _param_headers(schema: dict) -> tuple[list[tuple[tuple[str, ...], str]], str]:
    """找出参数说明里标了 x-mcp-header 的参数（HTTP 上要把它们的值抄进请求头）。返回（[(参数路径, 请求头名)], 问题）。

    规范要求：只能标在"从根上一路沿着 properties 走得到"的参数上，类型只能是字符串、整数或布尔，名字不能重复。
    """
    found: list[tuple[tuple[str, ...], str]] = []
    problem = ""

    def walk(node: object, path: tuple[str, ...] | None, depth: int) -> None:
        nonlocal problem
        if problem or depth > 32 or not isinstance(node, dict | list):
            return
        if isinstance(node, list):
            for item in node:
                walk(item, None, depth + 1)
            return
        if "x-mcp-header" in node:
            header = node["x-mcp-header"]
            if path is None or not path:
                problem = t("x-mcp-header 标在了不能标的地方")
            elif not isinstance(header, str) or not _HEADER_TOKEN.fullmatch(header):
                problem = t("x-mcp-header 的名字不合规")
            elif node.get("type") not in ("string", "integer", "boolean"):
                problem = t("x-mcp-header 只能标在字符串、整数或布尔类型的参数上")
            elif any(name.lower() == header.lower() for _, name in found):
                problem = t("x-mcp-header 的名字重复了")
            else:
                found.append((path, header))
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict) and path is not None:
                for name, child in value.items():
                    walk(child, (*path, name), depth + 1)
            elif key != "x-mcp-header":
                walk(value, None, depth + 1)

    walk(schema, (), 0)
    return ([] if problem else found), problem
