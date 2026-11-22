"""各家 MCP 配置的写法 → 智狐的写法（和 Claude Desktop 一样，见 mcp/config.py），以及改了什么、丢了什么。

大多数工具学的是 Claude Desktop 的 {"mcpServers": {名字: {command, args, env} 或 {url, headers}}}，
只是细节各有各的：

  工具                      放服务器的地方               和 Claude Desktop 不一样的地方
  Cursor、Windsurf……        mcpServers                   ${env:X}；Windsurf 的网址叫 serverUrl
  Cline                     mcpServers                   有 url 没写 type 的是 SSE；也可以写成 {"transport": {…}}
  Roo Code                  mcpServers                   type 是 streamable-http 或 sse；${env:X}、${workspaceFolder}
  VS Code                   servers（settings.json 里是 mcp.servers）   ${input:…} 要你当场输入
  Gemini CLI、Qwen Code     mcpServers                   httpUrl 是 Streamable HTTP；$X；timeout 是毫秒
  Zed                       context_servers              旧写法 command 是 {path, args, env}；扩展提供的服务器没有命令
  Codex                     [mcp_servers.名字]（TOML）    env_vars、bearer_token_env_var 只写环境变量的名字
  Goose                     extensions（YAML）            cmd、envs、uri；env_keys 的值在钥匙串里；builtin 不是 MCP 服务器
  Continue                  mcpServers 是一个列表（YAML） 名字写在每一项里；请求头在 requestOptions.headers；${{ secrets.X }}
  OpenCode                  mcp（JSONC）                  type 是 local / remote；command 是一个列表；environment

这里只把写法换过来，合不合格仍由 mcp/config.py 的 normalize 判断。换的时候有几条一定守住：
  - 别家"不用问"的设置（autoApprove、alwaysAllow、trust……）不搬：导入的服务器在智狐里照样每次先问你。
    智狐自己的 approval 也不认（一份克隆来的 .mcp.json 不该替你决定不用问）。
  - 智狐用不上的设置不悄悄留在配置里，而是列出来告诉你"没有搬过来"。
  - 引用环境变量的写法换成 ${X}（见 refs.py）。

各家文档（写这些规矩时读过的）：
  Claude Desktop  https://modelcontextprotocol.io/docs/develop/connect-local-servers
  Claude Code     https://code.claude.com/docs/en/mcp
  Cursor          https://cursor.com/docs/context/mcp
  VS Code         https://code.visualstudio.com/docs/agents/reference/mcp-configuration
  GitHub Copilot  https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers
  Windsurf        https://docs.devin.ai/windsurf/plugins/cascade/mcp
  Cline           https://docs.cline.bot/mcp/mcp-overview （url 不写 type 时是 SSE：源码 config-loader.ts）
  Roo Code        https://roocodeinc.github.io/Roo-Code/features/mcp/using-mcp-in-roo
  Continue        https://docs.continue.dev/customize/deep-dives/mcp
  Zed             https://zed.dev/docs/ai/mcp
  Gemini CLI      https://geminicli.com/docs/tools/mcp-server/
  Qwen Code       https://qwenlm.github.io/qwen-code-docs/en/users/features/mcp/
  Codex           https://learn.chatgpt.com/docs/extend/mcp 、https://learn.chatgpt.com/docs/config-file/config-reference
  Goose           https://goose-docs.ai/docs/guides/config-files
  OpenCode        https://opencode.ai/docs/mcp-servers/
  Kiro            https://kiro.dev/docs/mcp/configuration/
  Amazon Q        https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/command-line-mcp-configuration.html
  Junie           https://junie.jetbrains.com/docs/junie-cli-mcp-configuration.html
  Warp            https://docs.warp.dev/agents/capabilities/mcp
  LM Studio       https://lmstudio.ai/docs/app/mcp （文件的位置写在 https://lmstudio.ai/blog/mcp ）
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..i18n import Message, k, t
from .places import Env
from .refs import Refs


@dataclass
class Found:
    """一个服务器换成智狐的写法以后的样子。"""

    entry: object  # 换好的配置（换不了的原样留着，normalize 会说哪里不对）
    notes: list[Message] = field(default_factory=list)  # 改了什么、丢了什么
    problem: Message | None = None  # 导入不了的原因（比 normalize 能说得更清楚的时候才有）


Shape = Callable[[object, Env, str], dict[str, Found]]

# 换写法时用掉了的字段（剩下的就是"没有搬过来"的）
_USED = {
    "type", "transportType", "transport", "command", "args", "env", "cwd", "working_directory",
    "url", "serverUrl", "httpUrl", "headers", "enabled", "disabled",
}  # fmt: skip
# 别家"不用问就调用"的设置：一个都不搬
_NEVER_ASK = {"autoApprove", "alwaysAllow", "trust", "approval", "default_tools_approval_mode"}
_HTTP = {"http", "streamable-http", "streamablehttp", "remote"}
_STDIO = {"stdio", "local"}


def standard(name: str, raw: object, refs: Refs, *, url_default: str = "http", prefer_url: bool = False) -> Found:
    """Claude Desktop 那一类写法（大多数工具都是它的变体）→ 智狐的写法。"""
    if not isinstance(raw, dict):
        return Found(raw)
    raw = dict(raw)
    nested = raw.get("transport")
    if isinstance(nested, dict):  # Cline：{"transport": {"type": …, "command": …}, "disabled": …}
        raw = {**nested, **{key: value for key, value in raw.items() if key != "transport"}}
    elif isinstance(nested, str):  # Devin：transport 写的是 http 或 sse
        raw.setdefault("type", nested)
    kind = raw.get("type", raw.get("transportType"))
    kind = kind.strip().lower().replace("_", "-") if isinstance(kind, str) else None
    url = next((raw[key] for key in ("httpUrl", "url", "serverUrl") if isinstance(raw.get(key), str)), None)
    command = raw.get("command")
    if isinstance(raw.get("httpUrl"), str):
        transport = "http"  # Gemini CLI：httpUrl 就是 Streamable HTTP
    elif kind in _STDIO:
        transport = "stdio"
    elif kind in _HTTP:
        transport = "http"
    elif kind == "sse":
        transport = "sse"
    elif kind:
        return Found(raw, problem=t("“{name}”用的是 {kind} 这种连接方式，智狐不支持。", name=name, kind=kind))
    elif url is not None and (prefer_url or command is None):
        transport = url_default
    elif command is not None:
        transport = "stdio"
    else:
        return Found(raw)  # 既没有命令也没有网址：normalize 会说清楚

    notes: list[Message] = []
    entry: dict = {}
    dropped: list[str] = []
    if transport == "stdio":
        entry["command"] = refs.text(command)
        if raw.get("args") is not None:
            entry["args"] = refs.items(raw["args"])
        env = raw.get("env")
        if isinstance(env, dict):
            dropped += [f"env.{key}" for key, value in env.items() if value is None]  # VS Code 里 null 表示"去掉这个变量"
            env = {key: value for key, value in env.items() if value is not None}
        if env not in (None, {}, ""):
            entry["env"] = refs.pairs(env)
        cwd = raw.get("cwd", raw.get("working_directory"))
        if cwd not in (None, ""):
            entry["cwd"] = refs.text(cwd)
    else:
        entry["type"] = transport
        entry["url"] = refs.text(url) if url is not None else raw.get("url")
        if raw.get("headers") not in (None, {}, ""):
            entry["headers"] = refs.pairs(raw["headers"])
    if raw.get("disabled") is True or raw.get("enabled") is False:
        entry["enabled"] = False

    never = [key for key in raw if key in _NEVER_ASK and _skips_asking(key, raw[key])]
    if never:
        notes.append(t("{tool} 里“不用问就调用”的设置（{fields}）没有搬过来：在智狐里调用这些工具之前照样先问你。", tool=refs.tool, fields=", ".join(never)))
    if isinstance(raw.get("envFile"), str):
        notes.append(t("envFile 指向的文件（{file}）里的变量没有搬过来：导入后编辑这个服务器，把要用的环境变量填上。", file=raw["envFile"]))
    dropped += [key for key in raw if key not in _USED and key not in _NEVER_ASK and key != "envFile"]
    if dropped:
        notes.append(t("智狐用不上、没有搬过来的设置：{fields}", fields=", ".join(dropped)))
    return Found(entry, refs.notes() + notes)


def _skips_asking(key: str, value: object) -> bool:
    """这一项是不是真的放开了什么。智狐自己的 approval 写的全是 ask 的，和没写一样，不用提醒。"""
    if key == "approval":
        if not isinstance(value, dict):
            return False
        tools = given if isinstance(given := value.get("tools"), dict) else {}
        return value.get("default") == "auto" or "auto" in tools.values()
    return value not in (None, False, [], {}, "", "prompt", "ask")


def _dig(data: object, path: tuple[str, ...]) -> dict:
    for key in path:
        data = data.get(key) if isinstance(data, dict) else None
    return data if isinstance(data, dict) else {}


def each(servers: dict, one: Callable[[str, dict], Found | None]) -> dict[str, Found]:
    """一个一个地换。一个服务器的配置写错了类型（该是列表的地方写成了一个数），只让它自己导入不了，别的照常列出来。"""
    out: dict[str, Found] = {}
    for name, raw in servers.items():
        name = str(name)
        if not isinstance(raw, dict):
            out[name] = Found(raw)  # normalize 会说"应该是一个对象"
            continue
        try:
            found = one(name, dict(raw))
        except (TypeError, AttributeError, ValueError, KeyError, IndexError):
            found = Found(raw, problem=t("“{name}”的配置里有几项的写法认不出来（比如该是列表的地方写成了一个数），智狐读不懂它。", name=name))
        if found is not None:
            out[name] = found
    return out


def mapping(path: tuple[str, ...], *, url_default: str = "http", prefer_url: bool = False, note: str = "", **styles: bool) -> Shape:
    """"一个表：名字 → 配置"的写法。path 是这个表在文件里的位置，styles 是引用环境变量的写法（见 refs.Refs）。
    note 是 k() 标出来的一句提醒，加在只写了 url、没写 type 的服务器上（Gemini CLI 的 url 见 SHAPES）。"""

    def read(data: object, env: Env, tool: str) -> dict[str, Found]:
        def one(name: str, raw: dict) -> Found:
            found = standard(name, raw, Refs(env, tool, **styles), url_default=url_default, prefer_url=prefer_url)
            if note and isinstance(raw.get("url"), str) and not raw.get("type") and not raw.get("httpUrl") and found.problem is None:
                found.notes.append(t(note, tool=tool))
            return found

        return each(_dig(data, path), one)

    return read


def claude_local(data: object, env: Env, tool: str) -> dict[str, Found]:
    """Claude Code 在 ~/.claude.json 里按项目记的服务器：projects["这个工作目录"].mcpServers。"""
    projects = _dig(data, ("projects",))
    project: dict = next((projects[key] for key in (str(env.workspace), env.workspace.as_posix()) if isinstance(projects.get(key), dict)), {})
    return mapping(("mcpServers",))(project, env, tool)


def zed(data: object, env: Env, tool: str) -> dict[str, Found]:
    def one(name: str, raw: dict) -> Found:
        command = raw.get("command")
        if isinstance(command, dict):  # 旧写法：{"command": {"path": …, "args": […], "env": {…}}}
            raw.update(command=command.get("path"), args=command.get("args", raw.get("args")), env=command.get("env", raw.get("env")))
        if raw.get("source") == "extension" or (raw.get("command") is None and raw.get("url") is None and "settings" in raw):
            return Found(raw, problem=t("“{name}”是 Zed 的扩展提供的服务器，命令在扩展里，智狐拿不到。", name=name))
        raw.pop("source", None)
        return standard(name, raw, Refs(env, tool))

    return each(_dig(data, ("context_servers",)), one)


def codex(data: object, env: Env, tool: str) -> dict[str, Found]:
    """Codex 的 [mcp_servers.名字]。密钥只写环境变量的名字（env_vars、bearer_token_env_var、env_http_headers），
    换成 ${名字} 的引用：智狐启动它时同样从环境变量里取。env_vars 是交给本地程序的，网址的服务器用不上。"""

    def one(name: str, raw: dict) -> Found:
        remote = isinstance(raw.get("url"), str)
        passed: list[str] = []  # 照样交给本地程序的环境变量的名字
        dropped: list[str] = []  # 网址的服务器用不上、丢掉的
        for item in _list(raw.pop("env_vars", None)):
            variable = item if isinstance(item, str) else item.get("name") if isinstance(item, dict) else None
            if isinstance(variable, str) and variable:
                (dropped if remote else passed).append(variable)
        headers = dict(_table(raw.pop("http_headers", None)))
        bearer = raw.pop("bearer_token_env_var", None)
        if isinstance(bearer, str) and bearer:
            headers["Authorization"] = "Bearer ${" + bearer + "}"
            passed.append(bearer)
        for header, variable in _table(raw.pop("env_http_headers", None)).items():
            if isinstance(variable, str) and variable:
                headers[str(header)] = "${" + variable + "}"
                passed.append(variable)
        if remote:
            raw.setdefault("type", "http")
            if headers:
                raw["headers"] = headers
        elif passed:
            raw["env"] = {**{variable: "${" + variable + "}" for variable in passed}, **_table(raw.get("env"))}
        found = standard(name, raw, Refs(env, tool))
        if passed:
            found.notes.insert(0, t("{tool} 从环境变量里交给它的 {names}，换成了 {syntax} 引用：智狐启动它时同样从环境变量里取。", tool=tool, names=", ".join(dict.fromkeys(passed)), syntax="${NAME}"))
        if dropped:
            found.notes.append(t("智狐用不上、没有搬过来的设置：{fields}", fields="env_vars"))
        return found

    return each(_dig(data, ("mcp_servers",)), one)


def _list(value: object) -> list:
    """该是列表的一项：没写是空的，写成了别的类型就算写错了（each 会接住）。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError(value)
    return value


def _table(value: object) -> dict:
    """该是"名字: 值"的一项：没写是空的，写成了别的类型就算写错了（each 会接住）。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError(value)
    return value


def goose(data: object, env: Env, tool: str) -> dict[str, Found]:
    """Goose 的 extensions。builtin、platform 是 Goose 自带的扩展，不是 MCP 服务器，不列出来。"""

    def one(name: str, raw: dict) -> Found | None:
        kind = raw.get("type")
        if kind in ("builtin", "platform", "frontend", "inline_python"):
            return None
        keys = [key for key in _list(raw.get("env_keys")) if isinstance(key, str) and key]
        envs = _table(raw.get("envs"))
        if kind == "stdio":
            converted = {"type": "stdio", "command": raw.get("cmd"), "args": raw.get("args"), "env": {**envs, **{key: "${" + key + "}" for key in keys}}, "cwd": raw.get("cwd")}
            used = {"type", "cmd", "args", "envs", "env_keys", "cwd", "enabled"}
        elif kind in ("streamable_http", "sse"):
            converted = {"type": "http" if kind == "streamable_http" else "sse", "url": raw.get("uri"), "headers": raw.get("headers")}
            used = {"type", "uri", "headers", "enabled"}
        else:
            return Found(raw, problem=t("“{name}”用的是 {kind} 这种连接方式，智狐不支持。", name=name, kind=str(kind)))
        if raw.get("enabled") is False:
            converted["enabled"] = False
        found = standard(name, {key: value for key, value in converted.items() if value is not None}, Refs(env, tool, dollar=kind != "stdio"))
        if keys and kind == "stdio":
            found.notes.insert(0, t("{names} 的值存在 {tool} 的钥匙串里，没有搬过来：智狐启动它时从同名的环境变量里取，也可以导入后编辑这个服务器填上。", names=", ".join(keys), tool=tool))
        dropped = [key for key in raw if key not in used and key not in ("name", "description", "display_name", "bundled")]
        if dropped:
            found.notes.append(t("智狐用不上、没有搬过来的设置：{fields}", fields=", ".join(dropped)))
        return found

    return each(_dig(data, ("extensions",)), one)


def continue_dev(data: object, env: Env, tool: str) -> dict[str, Found]:
    """Continue：config.yaml 和 .continue/mcpServers/ 里的文件，mcpServers 是一个列表，名字写在每一项里。
    那个文件夹里也可以放 Claude Desktop 写法的 JSON；旧的 config.json 放在 experimental.modelContextProtocolServers。"""
    from ..mcp.importers import (  # 放在这里，免得两个模块互相导入
        guess_name,
        unique_name,
    )

    if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict):
        return mapping(("mcpServers",), secrets=True)(data, env, tool)
    items = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(items, list):
        items = _dig(data, ("experimental",)).get("modelContextProtocolServers")
    named: dict[str, object] = {}
    for index, raw in enumerate(items if isinstance(items, list) else []):
        label = raw.get("name") if isinstance(raw, dict) else None
        if not isinstance(label, str) or not label.strip():
            inner = raw.get("transport") if isinstance(raw, dict) and isinstance(raw.get("transport"), dict) else raw
            try:
                label = guess_name(inner) if isinstance(inner, dict) else ""
            except (TypeError, AttributeError, ValueError):
                label = ""
        named[unique_name(label.strip() or f"server-{index + 1}", set(named))] = raw

    def one(name: str, raw: dict) -> Found:
        if isinstance(raw.get("transport"), dict):
            raw = {**raw.pop("transport"), **raw}
        raw.pop("name", None)
        if "uses" in raw:
            return Found(raw, problem=t("“{name}”用的是 Continue Hub 上的配置（{uses}），内容不在这台电脑上，智狐读不到。", name=name, uses=str(raw["uses"])[:200]))
        options = raw.pop("requestOptions", None)
        if isinstance(options, dict) and options.get("headers") is not None:
            raw["headers"] = {**_table(options["headers"]), **_table(raw.get("headers"))}
        for key in ("version", "schema"):
            raw.pop(key, None)
        return standard(name, raw, Refs(env, tool, secrets=True))

    return each(named, one)


def opencode(data: object, env: Env, tool: str) -> dict[str, Found]:
    """OpenCode 的 mcp：旧写法是 mcp.名字，新写法（V2）是 mcp.servers.名字。type 是 local 或 remote，
    command 是一个列表（第一项是程序），环境变量叫 environment，引用写成 {env:X}。"""
    servers = _dig(data, ("mcp",))
    inner = servers.get("servers")
    if isinstance(inner, dict) and inner and all(isinstance(value, dict) and "type" in value for value in inner.values()):
        servers = inner

    def one(name: str, raw: dict) -> Found | None:
        if set(raw) <= {"enabled"}:
            return None  # 只写了一个开关：这是在别处（比如组织的配置里）定义的服务器
        command = raw.get("command")
        if isinstance(command, list):
            if not command:
                return Found(raw, problem=t("“{name}”的 command 是空的。", name=name))
            raw["command"], raw["args"] = command[0], [*command[1:], *_list(raw.get("args"))]
        if "environment" in raw:
            raw["env"] = raw.pop("environment")
        if raw.get("oauth") is False:
            raw.pop("oauth")
        return standard(name, raw, Refs(env, tool, brace=True))

    return each(servers, one)


SHAPES: dict[str, Shape] = {
    "claude": mapping(("mcpServers",)),
    "claude-local": claude_local,
    "cursor": mapping(("mcpServers",), editor=True),  # Cursor、Windsurf 和别的照着 Cursor 写的
    "cline": mapping(("mcpServers",), url_default="sse"),
    "roo": mapping(("mcpServers",), editor=True),
    "vscode": mapping(("servers",), editor=True),
    "vscode-settings": mapping(("mcp", "servers"), editor=True),
    # Gemini CLI 的文档说 url 是 SSE、httpUrl 是 Streamable HTTP；它的源码对只写了 url 的服务器先试 Streamable HTTP，
    # 不行才退回 SSE（packages/core/src/tools/mcp-client.ts），新版还建议写 url 加 type。智狐只用 Streamable HTTP，
    # 所以照着源码的第一步当它是 Streamable HTTP，并提醒一句：连不上的话，它多半只支持 SSE
    "gemini": mapping(("mcpServers",), prefer_url=True, dollar=True, note=k("{tool} 对只写了 url 的服务器会先试 Streamable HTTP、不行再用 SSE；智狐只用 Streamable HTTP。连不上的话，这个服务器可能只支持已经废弃的 SSE。")),
    "zed": zed,
    "codex": codex,
    "goose": goose,
    "continue": continue_dev,
    "opencode": opencode,
}


def read(shape: str, data: object, env: Env, tool: str) -> dict[str, Found]:
    """按 shape 读出一个文件里的服务器：{名字: Found}。"""
    return SHAPES[shape](data, env, tool)
