"""你自己加的"不用问的命令"：data/commands.json。

内置的只读命令（rules.py）只管"确定只是看"的那些。有的命令你天天用、也信得过，但它不是只读的
（npm test、make check、./scripts/deploy-status.sh），就可以加进这个清单：选了"只读的命令不用问"那一档时，
以这几个词开头的命令也不用问。

怎么比：命令和清单里的每一条都用同一个办法拆成词（words.py），命令开头的几个词和某一条一模一样就算，
后面跟什么参数都行（加了 npm test，npm test --watch 也算；npm testing 不算）。
拆词时的规矩照旧：有 ; && | > $( 这类东西的命令根本拆不出来，也就不会和任何一条对上；
就算对上了，碰到 .env、数据目录、~/.ssh 这些地方照样要问（见 paths.py）。
只在单独一条命令时算，不在管道里算：cat 脚本 | python 你允许过的东西，python 就会运行管道送进来的任何代码。
只在工作目录里算（见 paths.at_workspace）：npm test 跑的是这里的 package.json。

所以加进来之前想一想：这几个词开头、后面跟着任何参数的命令，你是不是都放心它不问就运行。
有几样根本加不进来，对上以后有几样参数这一次不算，都在 allowlist_guard.py（为什么、哪些，见那里开头）：
能运行任何命令的程序（bash、sudo、env、xargs……）、不写运行哪一个的运行器（npx、uv run、python）、
内置已经放开了只读用法的程序（find、sed、grep……）加不进来；后面的参数能让它运行别的命令（git pull --upload-pack、
make check --eval=…），或者指到工作目录之外已有的地方（npm test --prefix ../别的项目）时，这一次不算，照常问。

模型改不了这个清单：它在文件工具的 protected 名单里（和 .env、mcp.json 一样），也没有任何工具能改它。
只有你能在网页上（设置 → 不用问的命令）或者命令行里（harness commands …）改。
智狐开着的时候你用命令行改了，它下一次判断时就会读到新的（看文件的修改时间）。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from ..config import (
    ALLOWED_COMMANDS_DEFAULT,
    MAX_ALLOWED_COMMAND_CHARS,
    MAX_ALLOWED_COMMANDS,
)
from ..i18n import k, t
from ..safe import clean_text, private_dir, to_json, write_file
from .allowlist_guard import refusal
from .words import Glob, Unclear, stages

UNCLEAR = k("这一条看不懂：只能是一个程序加上几个参数，不能有 | ; & > < $ ` 这类符号和通配符（* ?），引号也要配对。")


class CommandListError(Exception):
    """改清单失败了。args[0] 是给人看的原因（t() 给的一句话）。"""


def check_entry(text: object) -> list[str]:
    """一条要加进清单的命令合不合格：合格的返回拆好的词，不合格抛出 CommandListError。"""
    if not isinstance(text, str) or not text.strip():
        raise CommandListError(t("写一条命令，比如 npm test。"))
    text = clean_text(text).strip()
    if len(text) > MAX_ALLOWED_COMMAND_CHARS:
        raise CommandListError(t("一条最长 {n} 个字。", n=MAX_ALLOWED_COMMAND_CHARS))
    try:
        found = stages(text)
    except Unclear:
        raise CommandListError(t(UNCLEAR)) from None
    if len(found) != 1:
        raise CommandListError(t(UNCLEAR))
    words = found[0]
    if any(isinstance(word, Glob) for word in words):  # 清单里是一条写死的命令：通配符展开成什么要看那一刻的文件夹，不认
        raise CommandListError(t(UNCLEAR))
    reason = refusal(words)  # 运行器、内置已经放开的程序、能运行别的命令的选项（见 allowlist_guard.py）
    if reason:
        raise CommandListError(reason)
    return words


class CommandList:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()  # 网页上的几个请求、几个同时运行的任务可能一起读、一起改
        self._stamp: tuple[int, int] | None = None
        self._entries: list[str] = list(ALLOWED_COMMANDS_DEFAULT)
        self._words: list[list[str]] = [check_entry(entry) for entry in self._entries]
        self.problem = ""  # 文件读不了、里面有看不懂的条目时，给人看的一句话（页面和命令行显示）

    # ---- 读 ------------------------------------------------------------------

    def entries(self) -> list[str]:
        with self._lock:
            self._reload()
            return list(self._entries)

    def words(self) -> list[list[str]]:
        """每一条拆成的词，给 classify 用（见 __init__.py）。"""
        with self._lock:
            self._reload()
            return [list(words) for words in self._words]

    def summary(self) -> dict:
        """给页面的：清单、文件在哪儿、上限，以及文件有问题时的那句话（带原文，换语言时能重译，见 webui/ui.py 的 said）。"""
        with self._lock:
            self._reload()
            return {"entries": list(self._entries), "file": str(self.path), "limit": MAX_ALLOWED_COMMANDS, "chars": MAX_ALLOWED_COMMAND_CHARS, "problem": self.problem}

    def _reload(self) -> None:
        """文件变了（你在命令行里改过、自己打开改过）就重新读。调用的人要先拿到 self._lock。"""
        try:
            info = os.stat(self.path)
        except FileNotFoundError:
            if self._stamp is not None:  # 文件被删了：回到默认的
                self._stamp, self.problem = None, ""
                self._entries = list(ALLOWED_COMMANDS_DEFAULT)
                self._words = [check_entry(entry) for entry in self._entries]
            return
        except OSError as exc:
            self.problem = t("读不了 {path}：{error}", path=self.path, error=exc.strerror or exc)
            return
        stamp = (info.st_mtime_ns, info.st_size)
        if stamp == self._stamp:
            return
        self._stamp = stamp
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            self.problem = t("{path} 坏了，没有用上里面的命令（{error}）。", path=self.path, error=exc)
            self._entries, self._words = [], []
            return
        raw = saved.get("commands") if isinstance(saved, dict) else None
        entries: list[str] = []
        words: list[list[str]] = []
        skipped: list[str] = []
        for item in raw if isinstance(raw, list) else []:
            try:
                found = check_entry(item)
            except CommandListError:
                skipped.append(str(item))
                continue
            if found not in words and len(entries) < MAX_ALLOWED_COMMANDS:
                entries.append(clean_text(item).strip())
                words.append(found)
        self._entries, self._words = entries, words
        self.problem = t("{path} 里有 {n} 条看不懂或者不许加，没有用上：{items}", path=self.path, n=len(skipped), items=t("、").join(skipped)) if skipped else ""

    # ---- 改 ------------------------------------------------------------------

    def add(self, text: object) -> str:
        """加一条，返回加进去的样子（去掉了两头的空白）。"""
        words = check_entry(text)
        entry = clean_text(str(text)).strip()
        with self._lock:
            self._reload()
            if words in self._words:
                raise CommandListError(t("这一条已经在清单里了。"))
            if len(self._entries) >= MAX_ALLOWED_COMMANDS:
                raise CommandListError(t("最多 {n} 条。先删掉几条不用的。", n=MAX_ALLOWED_COMMANDS))
            self._write([*self._entries, entry])
        return entry

    def replace(self, old: str, new: object) -> str:
        """把 old 那一条改成 new，位置不变。"""
        words = check_entry(new)
        entry = clean_text(str(new)).strip()
        with self._lock:
            self._reload()
            if old not in self._entries:
                raise CommandListError(t("这一条已经不在清单里了，可能刚在别处被删掉了。"))
            at = self._entries.index(old)
            if words in [item for index, item in enumerate(self._words) if index != at]:
                raise CommandListError(t("这一条已经在清单里了。"))
            self._write([*self._entries[:at], entry, *self._entries[at + 1 :]])
        return entry

    def remove(self, text: str) -> None:
        with self._lock:
            self._reload()
            if text not in self._entries:
                raise CommandListError(t("这一条已经不在清单里了，可能刚在别处被删掉了。"))
            self._write([entry for entry in self._entries if entry != text])

    def _write(self, entries: list[str]) -> None:
        """写回文件（先写临时文件再换掉，权限 600）。调用的人要先拿到 self._lock。"""
        private_dir(self.path.parent)
        try:
            write_file(self.path, to_json({"commands": entries}, readable=True, indent=2))
        except OSError as exc:
            raise CommandListError(t("没能保存：{error}", error=exc.strerror or exc)) from exc
        self._stamp = None  # 下一次照着文件重新读一遍（也就拿到了新的修改时间）
        self._reload()
