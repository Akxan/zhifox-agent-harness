"""把别处已经配好的 MCP 服务器搬过来：从 Claude Desktop、Claude Code 的配置里导入，或者粘贴一段 JSON。

智狐只用自己的 data/mcp.json，不会自己去读别的程序的配置。只有你在网页上点了"从 Claude 导入"，
才去读下面这几个文件，把找到的服务器列给你看（密钥遮住）；你勾选了哪几个，就把哪几个复制一份过来。
复制完就和原来的文件没有关系了：那边再改，这边不会跟着变。

  Claude Desktop             macOS  ~/Library/Application Support/Claude/claude_desktop_config.json
                             Linux  ~/.config/Claude/claude_desktop_config.json
  Claude Code（用户级）      ~/.claude.json 里最外层的 mcpServers
  Claude Code（这个目录）    ~/.claude.json 里 projects["工作目录"].mcpServers
  Claude Code（项目文件）    工作目录下的 .mcp.json

页面只告诉服务器"要导入哪个来源的哪个名字"，配置的内容由这边重新读文件取得，不经过浏览器。
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from ..i18n import k, t
from ..safe import clean
from .config import (
    ConfigError,
    McpConfig,
    check_name,
    display,
    normalize,
    same_server,
    transport_of,
)

MAX_READ_BYTES = 50_000_000  # ~/.claude.json 里还存着 Claude Code 的很多别的东西，可能不小
MAX_PASTE_CHARS = 200_000


@dataclass
class Source:
    id: str
    label: str  # 原文（k() 标记过），页面上译成当前语言
    path: Path
    servers: dict[str, object] = field(default_factory=dict)
    found: bool = False
    problem: str = ""


def sources(workspace: Path) -> list[Source]:
    """读出这几个地方配置的 MCP 服务器。没有这个文件的，found 是 False。"""
    home = Path.home()
    if sys.platform == "darwin":
        desktop = home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    else:
        desktop = home / ".config" / "Claude" / "claude_desktop_config.json"
    found = [
        _source("claude-desktop", k("Claude Desktop"), desktop, lambda data: data.get("mcpServers")),
        _source("claude-code-user", k("Claude Code（用户级）"), home / ".claude.json", lambda data: data.get("mcpServers")),
        _source(
            "claude-code-local",
            k("Claude Code（这个工作目录）"),
            home / ".claude.json",
            lambda data: ((data.get("projects") or {}).get(str(workspace)) or {}).get("mcpServers"),
        ),
        _source("claude-code-project", k("Claude Code（项目里的 .mcp.json）"), workspace / ".mcp.json", lambda data: data.get("mcpServers")),
    ]
    return found


def _source(id: str, label: str, path: Path, pick) -> Source:
    source = Source(id, label, path)
    try:
        if not path.is_file():
            return source
        source.found = True
        if path.stat().st_size > MAX_READ_BYTES:
            raise ValueError(t("文件太大了"))
        data = clean(json.loads(path.read_text(encoding="utf-8")))
        servers = pick(data) if isinstance(data, dict) else None
        source.servers = servers if isinstance(servers, dict) else {}
    except (OSError, ValueError, RecursionError, AttributeError) as exc:
        source.problem = t("读不出来：{error}", error=exc)
    return source


def preview(found: list[Source], config: McpConfig) -> list[dict]:
    """给页面看的导入清单：每个来源里有哪些服务器，能不能导入，导入后叫什么名字。密钥都遮住。"""
    existing = {name: entry for name, (entry, _) in config.servers().items()}
    taken = set(existing)
    listed = []
    for source in found:
        items = []
        for name, raw in source.servers.items():
            items.append(_candidate(str(name), raw, existing, taken))
        listed.append({"id": source.id, "label": source.label, "path": str(source.path), "found": source.found, "problem": source.problem, "servers": items})
    return listed


def _candidate(name: str, raw: object, existing: dict, taken: set[str]) -> dict:
    """一个可以导入的服务器在清单里的样子。target 是导入后用的名字（重名的话自动加上 -2、-3）。"""
    item = {"name": name, "target": "", "transport": "", "summary": None, "problem": "", "duplicate": False}
    try:
        entry = normalize(name, raw)
    except ConfigError as exc:
        item["problem"] = str(exc)
        item["transport"] = transport_of(raw) if isinstance(raw, dict) else ""
        return item
    item["transport"] = transport_of(entry)
    item["summary"] = display(entry)
    if any(same_server(entry, other) for other in existing.values() if other is not None):
        item["duplicate"] = True  # 和已经有的某一个连的是同一个服务器：默认不勾
    item["target"] = unique_name(clean_name(name), taken)
    taken.add(item["target"])
    return item


def apply(config: McpConfig, servers: dict[str, object], names: list[str]) -> tuple[list[str], list[str]]:
    """把 servers 里名字在 names 中的那些加进配置。返回（加进去的新名字，没加的原因）。"""
    added, skipped = [], []
    for name in names:
        if name not in servers:
            skipped.append(t("没有找到“{name}”。", name=name))
            continue
        taken = set(config.servers())
        target = unique_name(clean_name(name), taken)
        try:
            config.put(target, normalize(target, servers[name]))
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
            return {_guess_name(data): data}
    servers = {str(name): entry for name, entry in data.items() if isinstance(entry, dict)}
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


def _guess_name(entry: dict) -> str:
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
