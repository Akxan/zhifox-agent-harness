"""把别处已经配好的 MCP 服务器搬过来：从别的 AI 工具的配置里导入，或者粘贴一段 JSON。

智狐只用自己的 data/mcp.json，不会自己去读别的程序的配置。只有你在网页上点了"从其他工具导入"，
才去看 imports/registry.py 里登记的那些位置（Claude Desktop、Claude Code、Cursor、VS Code、Codex、Gemini CLI……），
把找到的服务器列给你看（密钥遮住）；你勾选了哪几个，就把哪几个复制一份过来。
复制完就和原来的文件没有关系了：那边再改，这边不会跟着变。

各家的写法不一样（TOML、YAML、servers、context_servers……），先由 imports/mcp_shapes.py 换成智狐的写法，
再用 config.py 的 normalize 检查。引用了智狐自己的模型密钥的（${ANTHROPIC_API_KEY}）列出来，但不导入。

页面只告诉服务器"要导入哪个来源的哪个名字"，配置的内容由这边重新读文件取得，不经过浏览器。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from ..i18n import t
from ..imports import readers
from ..imports.mcp_shapes import Found, read
from ..imports.places import Env
from ..imports.registry import LEVELS, MCP, places
from ..safe import clean
from ..skills.store import described
from .config import (
    ConfigError,
    McpConfig,
    check_name,
    display,
    model_key_references,
    normalize,
    same_server,
    transport_of,
)

MAX_PASTE_CHARS = 200_000
MAX_FOLDER_FILES = 50  # Continue 的 .continue/mcpServers/ 这种文件夹里，最多看这么多个文件
_FOLDER_READERS = {".yaml": "yaml", ".yml": "yaml", ".json": "jsonc"}


@dataclass
class Source:
    """一个配置文件（或者文件里的一处），和从里面读出来的服务器。"""

    id: str  # 页面导入时送回来的编号
    tool: str  # 工具的名字（产品名，不翻译）
    level: str  # registry 的 USER、PROJECT、LOCAL
    path: Path
    shown: str  # 给人看的路径：~/.cursor/mcp.json
    servers: dict[str, Found] = field(default_factory=dict)
    found: bool = False
    problem: str = ""
    tool_id: str = ""


def sources(workspace: Path, env: Env | None = None) -> list[Source]:
    """读出登记过的每一处配置里的 MCP 服务器。没有这个文件的，found 是 False。"""
    env = env or Env.here(workspace)
    found: list[Source] = []
    taken: dict[str, int] = {}
    for tool, place in places(env, MCP):
        base = place.id or f"{tool.id}-{place.level}"
        taken[base] = taken.get(base, 0) + 1
        source_id = base if taken[base] == 1 else f"{base}-{taken[base]}"
        if not place.folder:
            found.append(_read(Source(source_id, tool.name, place.level, place.path, env.shown(place.path), tool_id=tool.id), place.reader, place.form, env))
            continue
        try:
            with os.scandir(place.path) as listing:
                files = sorted(entry.name for entry in listing if os.path.splitext(entry.name)[1].lower() in _FOLDER_READERS and not entry.name.startswith("."))
        except OSError:
            files = []
        if not files:
            found.append(Source(source_id, tool.name, place.level, place.path, env.shown(place.path), tool_id=tool.id))
        for name in files[:MAX_FOLDER_FILES]:
            path = place.path / name
            reader = _FOLDER_READERS[os.path.splitext(name)[1].lower()]
            found.append(_read(Source(f"{source_id}:{name}", tool.name, place.level, path, env.shown(path), tool_id=tool.id), reader, place.form, env))
    return found


def _read(source: Source, reader: str, shape: str, env: Env) -> Source:
    try:
        if not source.path.is_file():
            return source
    except OSError:
        return source
    source.found = True
    try:
        source.servers = read(shape, readers.load(source.path, reader), env, source.tool)
    except readers.ReadError as exc:
        source.problem = str(exc)
    except (TypeError, AttributeError, ValueError, KeyError, RecursionError):  # 最外层的写法就不对（比如该是对象的地方是一个列表）
        source.problem = t("这个文件的写法认不出来，智狐读不懂它。")
    return source


def count(env: Env) -> dict[str, tuple[int, int]]:
    """每个工具里有几个服务器、几个文件读不出来（给导入页顶上的总览，见 imports/overview.py）。"""
    return counted(sources(env.workspace, env))


def counted(found: list[Source]) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for source in found:
        servers, problems = out.get(source.tool_id, (0, 0))
        out[source.tool_id] = (servers + len(source.servers), problems + bool(source.problem))
    return out


def pasted(servers: dict[str, object]) -> Source:
    """粘贴进来的那几个，当作一个来源（只用来生成清单）。"""
    return Source("paste", "", "", Path(), "", {name: Found(entry) for name, entry in servers.items()}, True)


def preview(found: list[Source], config: McpConfig) -> list[dict]:
    """给页面看的导入清单：每个来源里有哪些服务器，能不能导入，导入后叫什么名字。密钥都遮住。

    level 是原文（"用户级"这种），页面按当前的语言翻译；notes 是"改了什么、丢了什么"，也是原文和值。
    """
    existing = {name: entry for name, (entry, _) in config.servers().items()}
    taken = set(existing)
    listed = []
    for source in found:
        items = [_candidate(str(name), value, existing, taken) for name, value in source.servers.items()]
        listed.append({
            "id": source.id, "tool": source.tool, "tool_id": source.tool_id, "level": LEVELS.get(source.level, ""),
            "path": source.shown, "found": source.found, "problem": source.problem, "servers": items,
        })  # fmt: skip
    return listed


def _candidate(name: str, value: Found, existing: dict, taken: set[str]) -> dict:
    """一个可以导入的服务器在清单里的样子。target 是导入后用的名字（重名的话自动加上 -2、-3）。"""
    item: dict[str, object] = {"name": name, "target": "", "transport": "", "summary": None, "problem": "", "duplicate": False, "notes": [described(note) for note in value.notes]}
    raw = value.entry
    try:
        refused = check(name, value)
    except ConfigError as exc:
        item["problem"] = str(exc)
        item["transport"] = transport_of(raw) if isinstance(raw, dict) else ""
        return item
    entry = normalize(name, raw)
    item["transport"] = transport_of(entry)
    item["summary"] = display(entry)
    if refused:
        item["problem"] = str(refused)
        return item
    if any(same_server(entry, other) for other in existing.values() if other is not None):
        item["duplicate"] = True  # 和已经有的某一个连的是同一个服务器：默认不勾
    target = unique_name(clean_name(name), taken)
    item["target"] = target
    taken.add(target)
    return item


def check(name: str, value: Found) -> str:
    """一个要导入的服务器还有什么问题：写法不对的抛出 ConfigError；引用了模型密钥的返回原因（这时清单里还能显示它的命令）。"""
    if value.problem:
        raise ConfigError(value.problem)
    entry = normalize(name, value.entry)
    keys = model_key_references(entry)
    if keys:
        return t("配置里引用了 {names}：那是智狐自己用的模型密钥，不能交给 MCP 服务器，所以没有导入。", names=", ".join(keys))
    return ""


def apply(config: McpConfig, servers: dict[str, Found | object], names: list[str]) -> tuple[list[str], list[str]]:
    """把 servers 里名字在 names 中的那些加进配置。返回（加进去的新名字，没加的原因）。

    servers 的值是 Found（从别的工具读出来、已经换好写法的）或者原样的配置（粘贴的）。
    """
    added: list[str] = []
    skipped: list[str] = []  # 有 t() 写的，也有普通的字符串
    for name in names:
        if name not in servers:
            skipped.append(t("没有找到“{name}”。", name=name))
            continue
        value = given if isinstance(given := servers[name], Found) else Found(given)
        taken = set(config.servers())
        target = unique_name(clean_name(name), taken)
        try:
            refused = check(name, value)
            if refused:
                raise ConfigError(refused)
            config.put(target, normalize(target, value.entry))
        except ConfigError as exc:
            skipped.append(f"{name}：{exc}")
            continue
        added.append(target)
    return added, skipped


def clean_name(name: str) -> str:
    """别处的名字不一定合我们的规矩（可能有空格、斜杠）：不合规的字符换成连字符。"""
    name = re.sub(r"[^\w.\-]+", "-", name.strip()).strip("-.")[:64]
    try:
        return check_name(name)
    except ConfigError:
        return "server"


def unique_name(name: str, taken: set[str]) -> str:
    """名字被占用了，就依次试 name-2、name-3……"""
    if name not in taken:
        return name
    for number in range(2, 1000):
        candidate = f"{name[: 64 - len(str(number)) - 1]}-{number}"
        if candidate not in taken:
            return candidate
    raise ConfigError(t("名字“{name}”被占用了太多次。", name=name))


# ---- 粘贴一段 JSON ----------------------------------------------------------------


def parse_pasted(text: object) -> dict[str, object]:
    """从粘贴的文字里找出服务器的配置。接受这几种写法：

      {"mcpServers": {"名字": {...}}}     Claude Desktop、Claude Code、Cursor 的写法
      {"servers": {"名字": {...}}}        VS Code 的写法
      {"名字": {"command": ...}}          只有里面那一层
      "名字": {"command": ...}            从说明文档里复制来的一截（外面没有大括号）
      {"command": "npx", "args": [...]}   单独一个服务器，没有名字：从包名或网址里起一个
    """
    if not isinstance(text, str) or not text.strip():
        raise ConfigError(t("粘贴的内容是空的。"))
    if len(text) > MAX_PASTE_CHARS:
        raise ConfigError(t("粘贴的内容太长了。"))
    data = _loads_loosely(text.strip())
    if not isinstance(data, dict):
        raise ConfigError(t("粘贴的内容应该是一个 JSON 对象。"))
    for key in ("mcpServers", "servers"):
        if isinstance(data.get(key), dict):
            data = data[key]
            break
    else:
        if transport_of(data):
            return {guess_name(data): data}
    servers: dict[str, object] = {str(name): entry for name, entry in data.items() if isinstance(entry, dict)}
    if not servers:
        raise ConfigError(t("没有在粘贴的内容里找到 MCP 服务器的配置（应该有 command 或者 url）。"))
    return servers


def _loads_loosely(text: str) -> object:
    """尽量读懂一段手抄的 JSON：允许末尾多一个逗号，允许外面少了一对大括号。"""
    attempts = [text, "{" + text + "}"]
    for attempt in list(attempts):
        attempts.append(re.sub(r",\s*([}\]])", r"\1", attempt))  # 去掉 } 和 ] 前面多余的逗号
    problem: Exception | None = None
    for attempt in attempts:
        try:
            return clean(json.loads(attempt))
        except (ValueError, RecursionError) as exc:
            problem = problem or exc
    raise ConfigError(t("粘贴的内容不是合法的 JSON：{error}", error=problem))


def guess_name(entry: dict) -> str:
    """给一个没有名字的服务器起名字：@modelcontextprotocol/server-github → github，https://mcp.example.com/mcp → example。"""
    if transport_of(entry) == "stdio":
        for arg in entry.get("args") or []:
            if not isinstance(arg, str) or arg.startswith("-"):
                continue
            # 包名可能带作用域和版本（@scope/server-github@1.2.0），也可能是一个脚本的路径（/path/to/server.py）
            package = arg.partition("/")[2] if arg.startswith("@") else arg
            last = package.split("@")[0].rstrip("/").split("/")[-1]
            last = re.sub(r"\.(js|mjs|cjs|ts|py)$", "", last)
            last = re.sub(r"^(mcp[-_]server[-_]|server[-_])|([-_]mcp[-_]server|[-_]mcp|[-_]server)$", "", last)
            if last:
                return clean_name(last)
        return clean_name(Path(str(entry.get("command", "server"))).name)
    host = urlsplit(str(entry.get("url", ""))).hostname or "server"
    parts = [part for part in host.split(".") if part not in ("www", "mcp", "api", "com", "net", "org", "io", "dev", "ai", "app")]
    return clean_name(parts[0] if parts else host)
