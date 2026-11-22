"""钩子的配置：data/hooks.json。

    {
      "hooks": {
        "no-rm": {
          "event": "PreToolUse",
          "matcher": "shell",
          "command": "python3",
          "args": ["/Users/me/hooks/no_rm.py"],
          "timeout": 10,
          "onFailure": "block"
        },
        "fmt": {"event": "PostToolUse", "matcher": "files", "command": "python3", "args": ["scripts/format.py"]}
      }
    }

每一项是一个钩子，键是它的名字（你起的，页面上一行一个，开关、编辑、删除、看上次的结果都按它找）。字段：

  event      什么时候运行：PreToolUse / PostToolUse / UserPromptSubmit / Stop（见 __init__.py）
  matcher    只对哪些工具（只有 PreToolUse、PostToolUse 看它）：空的或 * 是全部；否则用 | 或 , 隔开几项，每一项是
             一种功能的名字（shell、files、browser、mcp……，和信息栏"智狐能做的事"是同一张表，见 capabilities.py）、
             工具的真名（run_command、mcp__github__create_issue、browser.left_click），可以带 * 通配（mcp__github__*）
  command    要运行的程序：python3、node、/Users/me/hooks/check.sh……
  args       参数，一项一个（不经过 shell：空格、引号、$HOME、* 都原样交给程序）
  timeout    最多等几秒（默认 config.HOOK_TIMEOUT，最长 HOOK_MAX_TIMEOUT）。超时的照样结束掉，算出错
  enabled    false 是先停用（默认开着）
  onFailure  钩子自己出了错（启动不了、超时、退出码不是 0 也不是 2）时怎么办：continue 当它没说话、照常继续（默认）；
             block 当成拦下（只对能拦下的 PreToolUse、UserPromptSubmit 有用）。管安全的钩子可以设成 block：它坏了的时候宁可不做
  runsIn     在哪些运行里生效：chat（你的对话，网页和终端都算）、task（定时任务）、channel（聊天通道），默认三处都算

和 Claude Code 的配置（settings.json 里的 hooks）是同一套概念：事件、matcher、命令、timeout、onFailure。
不同的是 Claude Code 按事件分组（hooks → 事件 → matcher 一组 → 几条命令），这里摊平成"名字 → 一个钩子"，
和 mcp.json 一样：页面上要一行一个地开关、编辑、看上次跑得怎么样，名字就是它的编号。另外两处有意不同：
  - 命令不经过 shell。Claude Code 的 command 是一行交给 shell 的命令；这里 command 只是程序，参数写在 args 里
    （和 Claude Code 的 args 写法一样）。所以一行里有空格的 command（"python3 check.py"）会被退回，说明要分开写；
  - matcher 不认正则表达式，只认名字和 * 通配：够用，而且写不出一个会卡死的正则。

不认识的字段原样留着（可能是以后的版本加的）。这个文件只有你能改：网页上的"扩展 → 钩子"、命令行的 harness hooks、
或者你自己用编辑器改。模型没有任何能改它的工具，文件工具在任何模式下都碰不了它（见 app.py 里的 protected）：
改了它，就能让智狐在你的电脑上运行任何命令。参数里可能有令牌，所以它和 mcp.json 一样只有你的账号能读（600），
导出时值也遮住（export.MASKED_FILES），页面上看到的参数遮住了像密钥的部分。
"""

from __future__ import annotations

import copy
import json
import re
import threading
from pathlib import Path

from ..config import HOOK_MAX, HOOK_MAX_TIMEOUT
from ..i18n import t
from ..mcp.config import MASK, mask_args, mask_text
from ..safe import clean, clean_text, private_dir, to_json, write_file
from ..tools.shell import SECRET_ENV_VARS

FILE_NAME = "hooks.json"
MAX_FILE_BYTES = 1_000_000
MAX_ARGS = 200
MAX_TEXT = 10_000  # 一个参数最长这么多字符
MAX_COMMAND = 1000
MAX_MATCHER = 500
# 事件：什么时候运行。顺序就是页面上下拉框里的顺序
EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop")
TOOL_EVENTS = ("PreToolUse", "PostToolUse")  # 只有这两个看 matcher
BLOCKING = ("PreToolUse", "UserPromptSubmit")  # 能拦下的事件（onFailure: block 只对它们有用）
SOURCES = ("chat", "task", "channel")
FAILURES = ("continue", "block")
_NAME = re.compile(r"[\w.\-]{1,64}")  # 和 MCP 服务器的名字一样：字母、数字（包括中文）、下划线、点、连字符
_ITEM = re.compile(r"[\w.\-*:]{1,200}")  # matcher 里的一项：名字、功能、带 * 的通配（工具包的成员写成 browser.left_click）
_KNOWN = {"event", "matcher", "command", "args", "timeout", "enabled", "onFailure", "runsIn"}


class HookError(Exception):
    """配置不对，或者保存不了。错误信息可以直接给用户看（t() 译好的）。"""


def check_name(name: object) -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name.strip()):
        raise HookError(t("钩子的名字只能用字母、数字、下划线、点和连字符，最长 64 个字符。"))
    return name.strip()


def matcher_items(matcher: str) -> list[str]:
    """matcher 拆成几项。空的、* 是"全部"，拆出来是空的列表。"""
    items = [item.strip() for item in re.split(r"[|,]", matcher) if item.strip()]
    return [] if not items or "*" in items else items


def model_key_references(texts: list[str]) -> list[str]:
    """这些文字里引用了哪些智狐自己的密钥（各家模型的 API 密钥、聊天机器人的 token），按出现的顺序、不重复。

    认的写法是几种 shell 取环境变量的样子：$NAME、${NAME}、${NAME:-…}（bash、zsh）、%NAME%（Windows 的 cmd）、
    $env:NAME（PowerShell）。钩子的环境里本来就去掉了这些变量（见 runner.py），这里再拦一道：写了引用，
    说明有人想把密钥交给这个命令（多半是从网上抄来的一段配置），保存时就退回，而不是让它悄悄拿到一个空值。
    """
    found: list[str] = []
    for text in texts:
        for name in SECRET_ENV_VARS:
            pattern = rf"\$\{{{name}(?::-[^}}]*)?\}}|\${name}(?![A-Za-z0-9_])|%{name}%|\$env:{name}(?![A-Za-z0-9_])"
            if re.search(pattern, text, flags=re.IGNORECASE) and name not in found:
                found.append(name)
    return found


def normalize(name: str, entry: object) -> dict:
    """检查一个钩子的配置，整理成统一的写法。不合格就抛出 HookError，说明哪里不对。"""
    if not isinstance(entry, dict):
        raise HookError(t("钩子“{name}”的配置应该是一个 JSON 对象。", name=name))
    event = entry.get("event")
    if event not in EVENTS:
        raise HookError(t("钩子“{name}”的 event 应该是 {events} 之一。", name=name, events=" / ".join(EVENTS)))
    out: dict = {"event": event}
    matcher = entry.get("matcher", "")
    if event in TOOL_EVENTS and matcher not in (None, ""):
        if not isinstance(matcher, str) or len(matcher) > MAX_MATCHER or "\x00" in matcher:
            raise HookError(t("钩子“{name}”的 matcher 应该是一段文字，最长 {n} 个字符。", name=name, n=MAX_MATCHER))
        bad = [item for item in re.split(r"[|,]", matcher) if item.strip() and not _ITEM.fullmatch(item.strip())]
        if bad:
            raise HookError(t("钩子“{name}”的 matcher 里，“{item}”看不懂：只能写功能的名字（比如 shell、files）、工具的名字和 *，用 | 隔开。", name=name, item=bad[0].strip()))
        out["matcher"] = "|".join(matcher_items(matcher)) or "*"
    elif event in TOOL_EVENTS:
        out["matcher"] = "*"
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND or "\x00" in command:
        raise HookError(t("钩子“{name}”的 command 应该是要运行的程序（比如 python3），最长 {n} 个字符。", name=name, n=MAX_COMMAND))
    out["command"] = command.strip()
    args = entry.get("args", [])
    if not isinstance(args, list) or len(args) > MAX_ARGS or not all(isinstance(arg, str) and len(arg) <= MAX_TEXT and "\x00" not in arg for arg in args):
        raise HookError(t("钩子“{name}”的 args 应该是一个列表，每一项是一段文字，最多 {n} 项。", name=name, n=MAX_ARGS))
    out["args"] = list(args)
    if not out["args"] and re.search(r"\s", out["command"]) and not Path(out["command"]).expanduser().is_file():
        raise HookError(
            t("钩子“{name}”的 command 里有空格：命令不经过 shell，程序写在 command 里，参数一项一个写在 args 里（比如 command 写 python3，args 写 check.py）。", name=name)
        )
    secrets = model_key_references([out["command"], *out["args"]])
    if secrets:
        raise HookError(t("钩子“{name}”引用了 {secret}：那是智狐自己用的密钥，不能交给钩子。钩子运行时也看不到它。", name=name, secret=secrets[0]))
    timeout = entry.get("timeout")
    if timeout is not None:
        if not isinstance(timeout, int | float) or isinstance(timeout, bool) or not 1 <= timeout <= HOOK_MAX_TIMEOUT:
            raise HookError(t("钩子“{name}”的 timeout 应该是 1 到 {n} 之间的秒数。", name=name, n=HOOK_MAX_TIMEOUT))
        out["timeout"] = timeout
    out["enabled"] = entry.get("enabled") is not False
    failure = entry.get("onFailure", "continue")
    if failure not in FAILURES:
        raise HookError(t("钩子“{name}”的 onFailure 应该是 continue 或 block。", name=name))
    if failure == "block" and event in BLOCKING:
        out["onFailure"] = "block"
    runs = entry.get("runsIn")
    if runs is not None:
        if not isinstance(runs, list) or not runs or not all(source in SOURCES for source in runs):
            raise HookError(t("钩子“{name}”的 runsIn 应该是 chat、task、channel 里的一项或几项。", name=name))
        if set(runs) != set(SOURCES):
            out["runsIn"] = [source for source in SOURCES if source in runs]
    out.update({key: copy.deepcopy(value) for key, value in entry.items() if key not in _KNOWN})
    return out


def runs_in(entry: dict) -> tuple[str, ...]:
    return tuple(entry.get("runsIn") or SOURCES)


# ---- 给页面看的样子 ----------------------------------------------------------------


def display(entry: dict) -> dict:
    """一个钩子给页面看的样子。参数里像密钥的部分遮住（--token=…、网址里的密码、常见格式的密钥，规则和 MCP 的一样）。"""
    return {
        "event": entry["event"],
        "matcher": entry.get("matcher", ""),
        "command": mask_text(entry["command"]),
        "args": mask_args(entry.get("args", [])),
        "timeout": entry.get("timeout"),
        "enabled": entry.get("enabled", True),
        "onFailure": entry.get("onFailure", "continue"),
        "runsIn": list(runs_in(entry)),
    }


def from_form(form: object, old: dict | None) -> dict:
    """页面上"添加 / 编辑钩子"的表单，变成一份配置。

    参数里被遮住的地方（••••）原样交回来，表示"这一处没改"：用原来那一项的值（和 MCP 的表单一样，见 mcp/config.py）。
    不认识的字段（old 里有、表单里没有的）原样留着。
    """
    if not isinstance(form, dict):
        raise HookError(t("参数不正确"))
    old = old or {}
    entry = {key: copy.deepcopy(value) for key, value in old.items() if key not in _KNOWN}
    entry["event"] = form.get("event")
    matcher, command = form.get("matcher", ""), form.get("command", "")
    if not isinstance(matcher, str) or not isinstance(command, str):
        raise HookError(t("参数不正确"))
    entry["matcher"] = matcher.strip()
    entry["command"] = _unmasked(command.strip(), old.get("command"), mask_text)
    args = form.get("args", [])
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise HookError(t("参数不正确"))
    before = old.get("args", []) if isinstance(old.get("args"), list) else []
    masked = mask_args(before)
    entry["args"] = [before[index] if index < len(before) and MASK in arg and arg == masked[index] else _no_mask(arg) for index, arg in enumerate(args)]
    for key in ("timeout", "onFailure", "runsIn"):
        if form.get(key) is not None:
            entry[key] = form[key]
    entry["enabled"] = form.get("enabled", True) is not False
    return entry


def _unmasked(new: str, old: object, mask) -> str:
    if isinstance(old, str) and MASK in new and new == mask(old):
        return old
    return _no_mask(new)


def _no_mask(text: str) -> str:
    if MASK in text:
        raise HookError(t("有一处还留着遮住的记号（••••）。改动过的地方请填上完整的值。"))
    return text


# ---- 整个文件 -------------------------------------------------------------------


class HookConfig:
    """data/hooks.json 的读和写。网页上的几个请求、命令行、几个同时进行的运行都可能在读它，所以都在一把锁里。

    每次要用之前看一眼文件的修改时间（reload）：你在编辑器里或者用 harness hooks 改了它，下一次就按新的来，不用重启。
    改之前也重新读一遍，不拿内存里的旧内容把你手动的修改盖掉。文件读不出来时（写坏了的 JSON）一个钩子都不运行，
    也不让保存（problem 说明原因），免得把你的文件覆盖掉。
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._raw: dict[str, object] = {}
        self._extra: dict = {}
        self.problem = ""
        self._stamp: tuple | None = None
        with self._lock:
            self._read()

    def _read(self) -> None:
        self._raw, self._extra, self.problem = {}, {}, ""
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            self._stamp = None
            return
        except OSError as exc:
            self._stamp = None
            self.problem = t("读不了钩子的配置文件 {path}：{error}", path=self.path, error=exc)
            return
        self._stamp = (stat.st_mtime_ns, stat.st_size)
        try:
            if stat.st_size > MAX_FILE_BYTES:
                raise ValueError(t("文件太大了"))
            data = clean(json.loads(self.path.read_text(encoding="utf-8")))
            if not isinstance(data, dict):
                raise TypeError(t("最外层应该是一个对象"))
            hooks = data.get("hooks", {})
            if not isinstance(hooks, dict):
                raise TypeError(t("hooks 应该是一个对象（名字 → 钩子）"))
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            self.problem = t("钩子的配置文件 {path} 读不出来（{error}）。修好或者删掉它之前，一个钩子都不运行，在这里做的修改也不会保存。", path=self.path, error=exc)
            return
        self._raw = dict(hooks)
        self._extra = {key: value for key, value in data.items() if key != "hooks"}

    def reload(self) -> None:
        """文件在磁盘上变了的话重新读一遍（只看修改时间和大小，很便宜：每次要运行钩子之前都调用）。"""
        with self._lock:
            try:
                stat = self.path.stat()
                stamp: tuple | None = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                stamp = None
            if stamp != self._stamp:
                self._read()

    def hooks(self) -> dict[str, tuple[dict | None, str]]:
        """全部钩子：{名字: (整理好的配置, 问题)}，按文件里的顺序。写错了的那些，配置是 None，问题说明哪里不对。"""
        with self._lock:
            out: dict[str, tuple[dict | None, str]] = {}
            for name, raw in self._raw.items():
                try:
                    out[name] = (normalize(check_name(name), raw), "")
                except HookError as exc:
                    out[name] = (None, exc.args[0] if exc.args else str(exc))
            return out

    def get(self, name: str) -> dict | None:
        entry, _ = self.hooks().get(name, (None, ""))
        return entry

    def put(self, name: str, entry: dict, original: str | None = None) -> dict:
        """添加或者替换一个钩子。original 是编辑前的名字（改名时和 name 不同）；None 表示新加的。返回整理好的配置。"""
        name = check_name(clean_text(name))
        with self._lock:
            self._fresh()
            normalized = normalize(name, entry)
            if name != original and name in self._raw:
                raise HookError(t("已经有一个叫“{name}”的钩子了，换个名字吧。", name=name))
            if original is not None and original not in self._raw:
                raise HookError(t("没有找到钩子“{name}”，它可能已经被删掉了。", name=original))
            if original is None and len(self._raw) >= HOOK_MAX:
                raise HookError(t("最多只能配置 {n} 个钩子。", n=HOOK_MAX))
            items = [(name, normalized) if key == original else (key, value) for key, value in self._raw.items()]  # 改名时位置不变
            if original is None:
                items.append((name, normalized))
            self._raw = dict(items)
            self._write()
            return normalized

    def remove(self, name: str) -> None:
        with self._lock:
            self._fresh()
            if name not in self._raw:
                raise HookError(t("没有找到钩子“{name}”，它可能已经被删掉了。", name=name))
            del self._raw[name]
            self._write()

    def set_enabled(self, name: str, enabled: bool) -> dict:
        with self._lock:
            self._fresh()
            raw = self._raw.get(name)
            if raw is None:
                raise HookError(t("没有找到钩子“{name}”，它可能已经被删掉了。", name=name))
            entry = normalize(name, raw)  # 写错了的配置在这里就报出来，不会被悄悄改掉
            entry["enabled"] = enabled
            saved = normalize(name, entry)
            self._raw[name] = saved
            self._write()
            return saved

    def _fresh(self) -> None:
        self._read()
        if self.problem:
            raise HookError(self.problem)

    def _write(self) -> None:
        private_dir(self.path.parent)
        write_file(self.path, to_json({"hooks": self._raw, **self._extra}, readable=True, indent=2) + "\n")
        stat = self.path.stat()
        self._stamp = (stat.st_mtime_ns, stat.st_size)
