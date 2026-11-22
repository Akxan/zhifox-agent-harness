"""从备份恢复时，存着密钥的配置文件怎么办：占位符绝不盖掉你现在的真密钥。

导出时，mcp.json、channels.json、endpoints.json、hooks.json（export.MASKED_FILES）里的密钥换成了 <removed-from-backup>
（export.PLACEHOLDER）：环境变量、请求头的值，名字像密钥的字段，--token 后面的参数，网址里的密码……
直接把备份里的这几个文件放回去，就是拿占位符盖掉你现在填好的密钥。所以恢复时一处一处对：

  备份里的某一处是占位符，现在的文件里"同一处"有一个真的值  →  留着现在的（kept）
  现在没有这一处（没有这个 MCP 服务器、这个环境变量……）     →  照样写进占位符，告诉你去哪儿重新填（missing）
  现在有、备份里却没有的密钥（覆盖以后就没了）               →  也告诉你（dropped）：它不会留在恢复前的自动备份里，那份也是遮过的

"同一处"按路径认：字典按键（mcpServers → github → env → GITHUB_TOKEN）；一串带编号或名字的东西按编号、名字
（endpoints.json 里的接口按 id）；别的列表（命令行参数）按第几项，而且除了密钥那几项以外两边要一模一样才算，
不然 ["--token", 占位符] 会把现在 ["--verbose", "--token", "x"] 里的 "--token" 当成密钥填进去。
只遮了一部分的值（https://用户:占位符@host、Bearer 占位符）按样子比：现在的值和备份里的只差在占位符那几处，才用现在的。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .export import PLACEHOLDER, mask

MISSING = object()  # 现在的文件里没有这一处
MAX_TEXT = 10_000  # 比"样子"的值最多这么长（防一个故意写得很长的值把正则拖慢）
Path = tuple[str, ...]


@dataclass
class Filled:
    """一个配置文件对完以后：哪几处留着现在的密钥，哪几处还是占位符要重新填，现在有、恢复以后没了的有哪几处。"""

    kept: list[Path] = field(default_factory=list)
    missing: list[Path] = field(default_factory=list)
    dropped: list[Path] = field(default_factory=list)


def holes(value: Any, path: Path = ()) -> list[Path]:
    """一份配置里哪几处是占位符。"""
    if isinstance(value, dict):
        return [hole for key, item in value.items() for hole in holes(item, (*path, str(key)))]
    if isinstance(value, list):
        keyed = _keyed(value)
        return [hole for index, item in enumerate(value) for hole in holes(item, (*path, _ident(item) if keyed else f"#{index + 1}"))]
    if isinstance(value, str) and PLACEHOLDER in value:
        return [path]
    return []


def fill(backup: Any, current: Any = MISSING) -> tuple[Any, Filled]:
    """备份里的一份配置（遮过的），填上现在那一份里同一处的密钥。返回（填好的，对的结果）。
    current 是现在的文件解析出来的样子；现在没有这个文件就不给。"""
    result = Filled()
    value = _fill(backup, current, (), result)
    if current is not MISSING:
        taken = set(result.kept)
        result.dropped = [path for path in holes(mask(current)) if path not in taken]
    return value, result


def label(path: Path) -> str:
    """给人看的位置：mcpServers › github › env › GITHUB_TOKEN。"""
    return " › ".join(part[:80] for part in path) or "…"


def _fill(backup: Any, current: Any, path: Path, result: Filled) -> Any:
    if isinstance(backup, dict):
        here = current if isinstance(current, dict) else {}
        return {key: _fill(item, here.get(key, MISSING), (*path, str(key)), result) for key, item in backup.items()}
    if isinstance(backup, list):
        here = current if isinstance(current, list) else []
        if _keyed(backup):
            found = {_ident(item): item for item in here if isinstance(item, dict) and _ident(item)}
            return [_fill(item, found.get(_ident(item), MISSING), (*path, _ident(item)), result) for item in backup]
        # 不带名字的一串（命令行参数）：除了占位符那几项，两边一模一样才认是同一串
        same = len(here) == len(backup) and all(_alike(mine, theirs) for mine, theirs in zip(backup, here, strict=True))
        return [_fill(item, here[index] if same else MISSING, (*path, f"#{index + 1}"), result) for index, item in enumerate(backup)]
    if isinstance(backup, str) and PLACEHOLDER in backup:
        if _fits(backup, current):
            result.kept.append(path)
            return current
        result.missing.append(path)
    return backup


def _keyed(items: list) -> bool:
    """这一串的每一项都是带着编号（id）或名字（name）的字典：按编号、名字认，不按第几个。"""
    return bool(items) and all(isinstance(item, dict) and _ident(item) for item in items)


def _ident(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    for key in ("id", "name"):
        if isinstance(item.get(key), str) and item[key] and PLACEHOLDER not in item[key]:
            return item[key]
    return ""


def _alike(backup: Any, current: Any) -> bool:
    """一串参数里的一项：占位符和一组东西（往里面再比）不算数，别的要一模一样。"""
    if isinstance(backup, str) and PLACEHOLDER in backup:
        return True
    if isinstance(backup, (dict, list)):
        return type(current) is type(backup)
    return backup == current and type(backup) is type(current)


def _fits(template: str, value: Any) -> bool:
    """现在的值能不能顶替备份里这一处：除了占位符那几处，两边一个字都不差。整个是占位符的，任何一个不空的值都行（数字也行：
    遮的时候数字也换成了占位符）。现在的值本身还是占位符（上一次恢复以后没填）的不算。"""
    if value is MISSING or value is None or isinstance(value, (bool, dict, list)):
        return False
    if not isinstance(value, str):
        return template == PLACEHOLDER and isinstance(value, (int, float))
    if not value or PLACEHOLDER in value or len(value) > MAX_TEXT:
        return False
    if template == PLACEHOLDER:
        return True
    pattern = "(.+)".join(re.escape(piece) for piece in template.split(PLACEHOLDER))
    return re.fullmatch(pattern, value, re.DOTALL) is not None
