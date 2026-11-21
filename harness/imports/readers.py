"""读别的工具的配置文件：JSON、带注释的 JSON（JSONC）、TOML、YAML。

每一种读法都只是"把文字变成字典和列表"，不运行任何东西：
  JSON    Python 自带的 json
  JSONC   VS Code、Zed、OpenCode 的配置允许 // 注释、/* */ 注释和末尾多一个逗号：先去掉它们，再按 JSON 读
  TOML    Python 3.11 起自带的 tomllib（Codex 的 config.toml）
  YAML    自己写的一个小子集（yaml_lite.py，Goose、Continue 用到的写法），为什么不用 YAML 库写在那里

读不出来时抛出 ReadError，里面是一句可以直接给人看的话（第几行、哪里不对），页面在那个文件下面显示它，
别的文件照常列出来：一个工具的配置写坏了，不该让别的工具的也导入不了。
"""

from __future__ import annotations

import json
import os
import stat
import tomllib
from pathlib import Path

from ..i18n import t
from ..safe import clean
from . import yaml_lite

MAX_READ_BYTES = 50_000_000  # ~/.claude.json 里还存着 Claude Code 的很多别的东西，可能不小；VS Code 的 settings.json 也是


class ReadError(Exception):
    """文件读不出来。信息可以直接给用户看。"""


def read_text(path: Path, limit: int = MAX_READ_BYTES) -> str:
    """读一个文本文件：先看是不是普通文件、大小在不在上限里，再按 UTF-8 读（开头的 BOM 去掉）。"""
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            raise ReadError(t("这不是一个普通的文件。"))
        if info.st_size > limit:
            raise ReadError(t("文件太大了"))
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        raise ReadError(t("读不出来：{error}", error=exc.strerror or exc)) from None
    try:
        return data.decode("utf-8").removeprefix("﻿")
    except UnicodeDecodeError:
        raise ReadError(t("这个文件不是 UTF-8 编码的文字。")) from None


def load(path: Path, kind: str) -> object:
    """按 kind（json、jsonc、toml、yaml）读一个配置文件，返回里面的内容。"""
    text = read_text(path)
    return parse(text, kind)


def parse(text: str, kind: str) -> object:
    try:
        if kind == "json":
            return clean(json.loads(text))
        if kind == "jsonc":
            return clean(json.loads(strip_jsonc(text)))
        if kind == "toml":
            return clean(tomllib.loads(text))
        if kind == "yaml":
            return clean(yaml_lite.loads(text))
    except json.JSONDecodeError as exc:
        raise ReadError(t("不是合法的 JSON：第 {line} 行第 {column} 列，{error}", line=exc.lineno, column=exc.colno, error=exc.msg)) from None
    except tomllib.TOMLDecodeError as exc:
        raise ReadError(t("不是合法的 TOML：{error}", error=exc)) from None
    except yaml_lite.YamlError as exc:
        raise ReadError(t("YAML 读不懂：第 {line} 行，{error}", line=exc.line, error=exc.args[0])) from None
    except (ValueError, RecursionError) as exc:
        raise ReadError(t("读不出来：{error}", error=exc)) from None
    raise ValueError(kind)


def strip_jsonc(text: str) -> str:
    """去掉 JSONC 里的 // 和 /* */ 注释、} 和 ] 前面多余的逗号。字符串里的 // 不动（网址里就有）。

    注释换成同样多的空格、换行原样留着，这样 JSON 报错时说的行号和列号仍然对得上原来的文件。
    """
    out: list[str] = []
    index, length = 0, len(text)
    in_string = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
        elif text.startswith("//", index):
            end = text.find("\n", index)
            end = length if end < 0 else end
            out.append(" " * (end - index))
            index = end
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            end = length if end < 0 else end + 2
            out.append("".join(part if part == "\n" else " " for part in text[index:end]))
            index = end
        else:
            out.append(char)
            index += 1
    return _drop_trailing_commas("".join(out))


def _drop_trailing_commas(text: str) -> str:
    """去掉 } 和 ] 前面多余的逗号（中间只隔着空白）。字符串里的不动。"""
    out = list(text)
    in_string = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            if char == "\\":
                index += 2
                continue
            if char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == ",":
            after = index + 1
            while after < len(text) and text[after] in " \t\r\n":
                after += 1
            if after < len(text) and text[after] in "}]":
                out[index] = " "
        index += 1
    return "".join(out)
