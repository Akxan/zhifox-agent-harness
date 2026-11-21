"""MCP 服务器的配置：data/mcp.json。

格式和 Claude Desktop 的 claude_desktop_config.json 一样，所以那边的配置可以原样贴过来：

    {
      "mcpServers": {
        "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/me/Desktop"]},
        "github":     {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                       "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_…"}},
        "docs":       {"type": "http", "url": "https://example.com/mcp", "headers": {"Authorization": "Bearer …"}}
      }
    }

另外有几项是智狐自己的，别的程序不认识（它们会忽略）：

    "enabled": false                                        先不启动这个服务器
    "approval": {"default": "ask", "tools": {"search": "auto"}}  调用前问不问你：默认每次都问，可以按工具放开
    "timeout": 600                                          一次工具调用最多等几秒（默认 300）

值里可以写 ${变量名} 或 ${变量名:-默认值}，启动时换成环境变量的值（和 Claude Code 的 .mcp.json 一样），
这样密钥不用写在这个文件里。

这个文件里有密钥（环境变量、请求头里的令牌），所以：只有你自己的账号能读（权限 600）；发给页面的只有名字和
最后 4 位（见 display）；模型的文件工具在任何模式下都碰不了它（见 app.py 里的 protected）。
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

from ..i18n import t
from ..safe import clean, private_dir, to_json, write_file
from ..tools.shell import SECRET_ENV_VARS

FILE_NAME = "mcp.json"
MAX_FILE_BYTES = 2_000_000
MAX_SERVERS = 100
MAX_ARGS = 200
MAX_PAIRS = 100  # 环境变量、请求头最多各这么多个
MAX_TEXT = 10_000  # 一个参数、一个值最长这么多字符
MASK = "••••"  # 页面上遮住密钥时用的记号。原样交回来，表示"这一处没改，用原来的"
APPROVALS = ("ask", "auto")
DEFAULT_TIMEOUT = 300
_NAME = re.compile(r"[\w.\-]{1,64}")  # 服务器的名字：字母、数字（包括中文）、下划线、点、连字符
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,128}")
# 这些请求头由智狐按协议填，配置里不能改：改了的话服务器会收到对不上的请求
_RESERVED_HEADERS = {
    "accept", "connection", "content-length", "content-type", "host", "transfer-encoding",
    "mcp-method", "mcp-name", "mcp-protocol-version", "mcp-session-id",
}  # fmt: skip
_KNOWN = {"type", "command", "args", "env", "cwd", "url", "headers", "enabled", "approval", "timeout"}
_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
# 遮密钥用的几条规则：网址里的密码、网址参数里像密钥的值、--token=… 这样的命令行参数、常见的密钥前缀
_URL_PASSWORD = re.compile(r"(\b[a-zA-Z][\w+.\-]*://[^/\s:@]*:)([^@/\s]+)(@)")
_SECRET_WORD = r"(?:key|token|secret|passw(?:or)?d|pwd|auth|signature|sig|credential|cookie|session)"
_QUERY_SECRET = re.compile(rf"([?&][^=&\s#]*{_SECRET_WORD}[^=&\s#]*=)([^&\s#]+)", re.IGNORECASE)
_FLAG_WITH_VALUE = re.compile(rf"^(--?[\w\-]*{_SECRET_WORD}[\w\-]*=)(.+)$", re.IGNORECASE)
_SECRET_FLAG = re.compile(rf"^--?[\w\-]*{_SECRET_WORD}[\w\-]*$", re.IGNORECASE)
_KEY_LIKE = re.compile(r"\b(sk-[A-Za-z0-9_\-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|xox[abprs]-[\w\-]{10,}|AIza[\w\-]{30,})")


class ConfigError(Exception):
    """配置不对，或者保存不了。错误信息可以直接给用户看。"""


# ---- 一个服务器的配置 -------------------------------------------------------------


def transport_of(entry: dict) -> str:
    """一个服务器用哪种方式连接："stdio"、"http"，已经废弃的 "sse"，看不出来的是 ""。"""
    kind = entry.get("type")
    if kind in (None, "stdio") and isinstance(entry.get("command"), str):
        return "stdio"
    if kind in ("http", "streamable-http", "streamableHttp") or (kind is None and isinstance(entry.get("url"), str)):
        return "http"
    if kind == "sse":
        return "sse"
    return ""


def check_name(name: object) -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name.strip()):
        raise ConfigError(t("服务器的名字只能用字母、数字、下划线、点和连字符，最长 64 个字符。"))
    return name.strip()


def normalize(name: str, entry: object) -> dict:
    """检查一个服务器的配置，整理成统一的写法。不合格就抛出 ConfigError，说明哪里不对。

    不认识的字段原样留着：那可能是别的程序的设置，或者是以后的版本加的。
    """
    if not isinstance(entry, dict):
        raise ConfigError(t("“{name}”的配置应该是一个 JSON 对象。", name=name))
    kind = transport_of(entry)
    if kind == "sse":
        raise ConfigError(
            t("“{name}”用的是已经废弃的 HTTP+SSE 连接方式，智狐不支持。看看这个服务器有没有 Streamable HTTP 的地址（通常以 /mcp 结尾）。", name=name)
        )
    if not kind:
        raise ConfigError(t("“{name}”的配置里既没有 command（本地程序），也没有 url（网址）。", name=name))
    out: dict = {}
    if kind == "stdio":
        out["command"] = _text(entry["command"], name, "command", limit=1000)
        if not out["command"].strip():
            raise ConfigError(t("“{name}”的 command 是空的。", name=name))
        args = entry.get("args", [])
        if not isinstance(args, list) or len(args) > MAX_ARGS:
            raise ConfigError(t("“{name}”的 args 应该是一个列表，最多 {n} 项。", name=name, n=MAX_ARGS))
        out["args"] = [_text(_scalar(arg), name, "args") for arg in args]
        out["env"] = _pairs(entry.get("env"), name, "env", _ENV_NAME)
        if entry.get("cwd") not in (None, ""):
            out["cwd"] = _text(entry["cwd"], name, "cwd", limit=1000)
    else:
        out["type"] = "http"
        url = _text(entry.get("url"), name, "url", limit=4000).strip()
        parts = urlsplit(url)
        # 写成 ${变量名} 的地址要到启动时才知道是什么，那时 http.py 会再检查一次
        if (parts.scheme not in ("http", "https") or not parts.hostname) and not _REFERENCE.search(url):
            raise ConfigError(t("“{name}”的地址必须以 http:// 或 https:// 开头。", name=name))
        out["url"] = url
        out["headers"] = _pairs(entry.get("headers"), name, "headers", _HEADER_NAME)
        reserved = [header for header in out["headers"] if header.lower() in _RESERVED_HEADERS]
        if reserved:
            raise ConfigError(t("请求头 {header} 由智狐按协议自动填写，不能在这里设置。", header=reserved[0]))
    out["enabled"] = entry.get("enabled") is not False
    approval = entry.get("approval")
    if isinstance(approval, dict):
        default = approval.get("default") if approval.get("default") in APPROVALS else "ask"
        tools = approval.get("tools") if isinstance(approval.get("tools"), dict) else {}
        kept = {tool: value for tool, value in tools.items() if isinstance(tool, str) and value in APPROVALS and value != default}
        if default != "ask" or kept:
            out["approval"] = {"default": default, **({"tools": kept} if kept else {})}
    timeout = entry.get("timeout")
    if isinstance(timeout, int | float) and not isinstance(timeout, bool) and 1 <= timeout <= 3600:
        out["timeout"] = timeout
    out.update({key: copy.deepcopy(value) for key, value in entry.items() if key not in _KNOWN})
    return out


def _scalar(value: object) -> object:
    """别的程序的配置里，数字有时不带引号（"PORT": 8080）。当成文字收下。"""
    return str(value) if isinstance(value, int | float) and not isinstance(value, bool) else value


def _text(value: object, name: str, field: str, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise ConfigError(t("“{name}”的 {field} 应该是一段文字，最长 {n} 个字符。", name=name, field=field, n=limit))
    return value


def _pairs(value: object, name: str, field: str, pattern: re.Pattern) -> dict[str, str]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict) or len(value) > MAX_PAIRS:
        raise ConfigError(t("“{name}”的 {field} 应该是一个对象（名字: 值），最多 {n} 项。", name=name, field=field, n=MAX_PAIRS))
    out = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not pattern.fullmatch(key):
            raise ConfigError(t("“{name}”的 {field} 里，名字“{key}”不合规。", name=name, field=field, key=key))
        text = _text(_scalar(raw), name, f"{field}.{key}")
        if field == "headers" and ("\r" in text or "\n" in text):
            raise ConfigError(t("请求头 {key} 的值里不能有换行。", key=key))
        out[key] = text
    return out


def same_server(first: dict, second: dict) -> bool:
    """两份配置连的是不是同一个服务器（只比连接方式，不比"问不问你"这些智狐自己的设置）。"""

    def core(entry: dict) -> dict:
        return {key: value for key, value in entry.items() if key in _KNOWN - {"enabled", "approval", "timeout"} and value not in ({}, [])}

    return core(first) == core(second)


def expand(entry: dict) -> dict:
    """把配置里的 ${变量名}、${变量名:-默认值} 换成环境变量的值，得到真正用来启动的那一份。

    各家模型的密钥（ANTHROPIC_API_KEY 这些）不能这样引用：一份从网上抄来的配置要是写着
    "env": {"X": "${ANTHROPIC_API_KEY}"}，你的密钥就被交给了一个第三方的程序。
    """

    def one(text: str) -> str:
        def replace(match: re.Match) -> str:
            variable, default = match.group(1), match.group(2)
            if variable in SECRET_ENV_VARS:
                raise ConfigError(t("配置里不能引用 {name}：那是智狐自己用的模型密钥，不能交给 MCP 服务器。", name=variable))
            value = os.environ.get(variable)
            if value is None:
                if default is None:
                    raise ConfigError(t("配置里用到了环境变量 {name}，但它没有设置。", name=variable))
                return default
            return value

        return _REFERENCE.sub(replace, text)

    out = dict(entry)
    for key in ("command", "cwd", "url"):
        if isinstance(out.get(key), str):
            out[key] = one(out[key])
    if isinstance(out.get("args"), list):
        out["args"] = [one(arg) for arg in out["args"]]
    for key in ("env", "headers"):
        if isinstance(out.get(key), dict):
            out[key] = {name: one(value) for name, value in out[key].items()}
    return out


def model_key_references(entry: dict) -> list[str]:
    """这份配置引用了哪些智狐自己的模型密钥（${ANTHROPIC_API_KEY} 这些），按出现的顺序、不重复。

    启动时 expand 会拒绝它们；从别处导入、粘贴进来的时候就先查一遍，有的话这个服务器不加进来（见 importers.py）。
    """
    texts = [entry.get(key) for key in ("command", "cwd", "url")]
    texts += entry.get("args") if isinstance(entry.get("args"), list) else []
    for key in ("env", "headers"):
        if isinstance(entry.get(key), dict):
            texts += list(entry[key].values())
    found = (match.group(1) for text in texts if isinstance(text, str) for match in _REFERENCE.finditer(text))
    return list(dict.fromkeys(name for name in found if name in SECRET_ENV_VARS))


# ---- 给页面看的样子：密钥都遮住 ------------------------------------------------------


def hint(value: str) -> str:
    """一个密钥给页面看的样子：够长的露出最后 4 位（和设置面板里的 API 密钥一样），短的一位都不露。"""
    return f"…{value[-4:]}" if len(value) >= 16 else ""


def mask_text(text: str) -> str:
    """遮住一段文字里看起来像密钥的部分：网址里的密码、网址参数里的令牌、常见格式的密钥。"""
    text = _URL_PASSWORD.sub(lambda match: match.group(1) + MASK + match.group(3), text)
    text = _QUERY_SECRET.sub(lambda match: match.group(1) + MASK, text)
    return _KEY_LIKE.sub(MASK, text)


def mask_args(args: list[str]) -> list[str]:
    """命令行参数给页面看的样子。除了 mask_text 的规则，还遮住 --api-key=… 和 --token 后面的那一个参数。"""
    shown, secret_next = [], False
    for arg in args:
        if secret_next and not arg.startswith("-"):
            shown.append(MASK)
        else:
            flag = _FLAG_WITH_VALUE.match(arg)
            shown.append(flag.group(1) + MASK if flag else mask_text(arg))
        secret_next = bool(_SECRET_FLAG.match(arg))
    return shown


def display(entry: dict) -> dict:
    """一个服务器的配置给页面看的样子。环境变量和请求头只给名字和最后几位，不给完整的值。"""
    if transport_of(entry) == "stdio":
        return {
            "transport": "stdio",
            "command": mask_text(entry["command"]),
            "args": mask_args(entry.get("args", [])),
            "cwd": entry.get("cwd", ""),
            "env": [{"name": name, "hint": hint(value)} for name, value in entry.get("env", {}).items()],
        }
    return {
        "transport": "http",
        "url": mask_text(entry.get("url", "")),
        "headers": [{"name": name, "hint": hint(value)} for name, value in entry.get("headers", {}).items()],
    }


def from_form(form: object, old: dict | None) -> dict:
    """页面上的"添加 / 编辑服务器"表单，变成一份配置。

    密钥不会回到页面上，所以编辑时没有重新填的那些要从原来的配置里拿：
      - 环境变量和请求头的一行写着 {"name": "TOKEN", "keep": "原来的名字"}，表示值用原来那一项的（可以顺便改名）；
      - 参数和地址里被遮住的地方（••••）原样交回来，表示"这一处没改"。
    """
    if not isinstance(form, dict):
        raise ConfigError(t("参数不正确"))
    old = old or {}
    kind, name = form.get("transport"), str(form.get("name") or "")
    entry: dict = {key: copy.deepcopy(value) for key, value in old.items() if key not in ("type", "command", "args", "env", "cwd", "url", "headers")}
    if kind == "stdio":
        entry["command"] = _unmasked(_text(form.get("command", ""), name, "command", 1000).strip(), old.get("command"), mask_text)
        args = form.get("args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ConfigError(t("参数不正确"))
        before = old.get("args", []) if transport_of(old) == "stdio" else []
        masked = mask_args(before)
        entry["args"] = [
            before[index] if index < len(before) and MASK in arg and arg == masked[index] else _no_mask(arg)
            for index, arg in enumerate(args)
        ]
        entry["env"] = _merge_pairs(form.get("env"), old.get("env", {}))
        if isinstance(form.get("cwd"), str) and form["cwd"].strip():
            entry["cwd"] = form["cwd"].strip()
    elif kind == "http":
        entry["type"] = "http"
        entry["url"] = _unmasked(_text(form.get("url", ""), name, "url", 4000).strip(), old.get("url"), mask_text)
        entry["headers"] = _merge_pairs(form.get("headers"), old.get("headers", {}))
    else:
        raise ConfigError(t("参数不正确"))
    entry["enabled"] = form.get("enabled", True) is not False
    return entry


def _unmasked(new: str, old: object, mask) -> str:
    """一处可能被遮住的文字：和原来的遮住后一模一样，就用原来的。"""
    if isinstance(old, str) and MASK in new and new == mask(old):
        return old
    return _no_mask(new)


def _no_mask(text: str) -> str:
    if MASK in text:
        raise ConfigError(t("有一处还留着遮住的记号（••••）。改动过的地方请填上完整的值。"))
    return text


def _merge_pairs(rows: object, old: dict) -> dict[str, str]:
    if rows in (None, ""):
        return {}
    if not isinstance(rows, list):
        raise ConfigError(t("参数不正确"))
    out: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise ConfigError(t("参数不正确"))
        name = row["name"].strip()
        value, keep = row.get("value"), row.get("keep")
        if not name:
            if (isinstance(value, str) and value) or isinstance(keep, str):
                raise ConfigError(t("有一行填了值，但没有填名字。"))
            continue  # 整行都空着：当它不存在
        if isinstance(value, str) and value != "":
            pass
        elif isinstance(keep, str) and keep in old:
            value = old[keep]
        else:
            raise ConfigError(t("“{name}”还没有填值。", name=name))
        if name in out:
            raise ConfigError(t("“{name}”出现了两次。", name=name))
        out[name] = value
    return out


# ---- 整个文件 -------------------------------------------------------------------


class McpConfig:
    """data/mcp.json 的读和写。网页上好几个请求可能同时改它，所以"读、改、写"在一把锁里一口气做完。

    每次改之前都重新读一遍文件：你可能在编辑器里手动改过它，不能拿内存里的旧内容把你的修改盖掉。
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._raw: dict[str, object] = {}  # 文件里的 mcpServers，原样（包括写错了的那些）
        self._extra: dict = {}  # 文件里 mcpServers 以外的东西，写回去时原样保留
        self.problem = ""  # 文件读不出来的原因。不为空时不让保存，免得把你的文件覆盖掉
        self._stamp: tuple | None = None
        with self._lock:
            self._read()

    def _read(self) -> None:
        """读文件。调用的人要先拿到锁。"""
        self._raw, self._extra, self.problem = {}, {}, ""
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            self._stamp = None
            return
        except OSError as exc:
            self.problem = t("读不了配置文件 {path}：{error}", path=self.path, error=exc)
            return
        self._stamp = (stat.st_mtime_ns, stat.st_size)
        try:
            if stat.st_size > MAX_FILE_BYTES:
                raise ValueError(t("文件太大了"))
            data = clean(json.loads(self.path.read_text(encoding="utf-8")))
            if not isinstance(data, dict):
                raise TypeError(t("最外层应该是一个对象"))
            servers = data.get("mcpServers", {})
            if not isinstance(servers, dict):
                raise TypeError(t("mcpServers 应该是一个对象"))
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            self.problem = t("配置文件 {path} 读不出来（{error}）。修好或者删掉它之前，在这里做的修改都不会保存。", path=self.path, error=exc)
            return
        self._raw = dict(servers)
        self._extra = {key: value for key, value in data.items() if key != "mcpServers"}

    def reload(self) -> bool:
        """文件在磁盘上变了的话重新读一遍。返回是不是变了。"""
        with self._lock:
            try:
                stat = self.path.stat()
                stamp: tuple | None = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                stamp = None
            if stamp == self._stamp and not self.problem:
                return False
            self._read()
            return True

    def servers(self) -> dict[str, tuple[dict | None, str]]:
        """全部服务器：{名字: (整理好的配置, 问题)}。写错了的那些，配置是 None，问题说明哪里不对。"""
        with self._lock:
            out: dict[str, tuple[dict | None, str]] = {}
            for name, raw in self._raw.items():
                try:
                    out[name] = (normalize(check_name(name), raw), "")
                except ConfigError as exc:
                    out[name] = (None, str(exc))
            return out

    def get(self, name: str) -> dict | None:
        entry, _ = self.servers().get(name, (None, ""))
        return entry

    def approval(self, name: str, tool: str) -> str:
        """调用这个服务器的这个工具之前问不问你："ask" 或 "auto"。"""
        with self._lock:
            raw = self._raw.get(name)
        approval = raw.get("approval") if isinstance(raw, dict) else None
        if not isinstance(approval, dict):
            return "ask"
        tools = approval.get("tools") if isinstance(approval.get("tools"), dict) else {}
        value = tools.get(tool, approval.get("default"))
        return "auto" if value == "auto" else "ask"

    # ---- 修改 -------------------------------------------------------------

    def put(self, name: str, entry: dict, original: str | None = None) -> dict:
        """添加或者替换一个服务器。original 是编辑前的名字（改名时和 name 不同）；None 表示新加的。返回整理好的配置。"""
        name = check_name(name)
        with self._lock:
            self._fresh()
            normalized = normalize(name, entry)
            if name != original and name in self._raw:
                raise ConfigError(t("已经有一个叫“{name}”的服务器了，换个名字吧。", name=name))
            if original is not None and original not in self._raw:
                raise ConfigError(t("没有找到服务器“{name}”，它可能已经被删掉了。", name=original))
            if original is None and len(self._raw) >= MAX_SERVERS:
                raise ConfigError(t("最多只能配置 {n} 个服务器。", n=MAX_SERVERS))
            # 改名时保持它在列表里的位置
            items = [(name, normalized) if key == original else (key, value) for key, value in self._raw.items()]
            if original is None:
                items.append((name, normalized))
            self._raw = dict(items)
            self._write()
            return normalized

    def remove(self, name: str) -> None:
        with self._lock:
            self._fresh()
            if name not in self._raw:
                raise ConfigError(t("没有找到服务器“{name}”，它可能已经被删掉了。", name=name))
            del self._raw[name]
            self._write()

    def set_enabled(self, name: str, enabled: bool) -> dict:
        return self._change(name, lambda entry: entry.__setitem__("enabled", enabled))

    def set_approval(self, name: str, tool: str | None, value: str) -> dict:
        """改"问不问你"。tool 为 None 时改这个服务器的默认值；否则只改这一个工具（和默认值一样时就不单独记了）。"""

        def change(entry: dict) -> None:
            approval = entry.get("approval") if isinstance(entry.get("approval"), dict) else {}
            default = approval.get("default", "ask")
            tools = dict(approval.get("tools") or {})
            if tool is None:
                default = value
            else:
                tools[tool] = value
            entry["approval"] = {"default": default, "tools": {key: item for key, item in tools.items() if item != default}}

        return self._change(name, change)

    def _change(self, name: str, change) -> dict:
        with self._lock:
            self._fresh()
            raw = self._raw.get(name)
            if raw is None:
                raise ConfigError(t("没有找到服务器“{name}”，它可能已经被删掉了。", name=name))
            entry = normalize(name, raw)  # 写错了的配置在这里就会报出来，不会被悄悄改掉
            change(entry)
            self._raw[name] = normalize(name, entry)
            self._write()
            return self._raw[name]

    def _fresh(self) -> None:
        """改之前先确认手里的是文件现在的内容，并且这个文件是读得出来的。调用的人要先拿到锁。"""
        self._read()
        if self.problem:
            raise ConfigError(self.problem)

    def _write(self) -> None:
        private_dir(self.path.parent)
        write_file(self.path, to_json({"mcpServers": self._raw, **self._extra}, readable=True, indent=2) + "\n")
        stat = self.path.stat()
        self._stamp = (stat.st_mtime_ns, stat.st_size)
