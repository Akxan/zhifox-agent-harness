"""评测任务的文件格式：一个任务一个 .toml 文件（Python 自带的 tomllib 就能读，不用装 YAML 的库）。

为什么是 TOML：要写多行的文字（文件内容、SKILL.md）、要写列表和小表格（检查项），JSON 写起来全是引号和 \\n，
Markdown 加 frontmatter 又装不下嵌套的检查项。TOML 两样都写得清楚，而且读它的库是 Python 自带的。

文件名去掉 .toml 就是任务的编号（files-create-note.toml → files-create-note），所以文件里不再写一遍。
开头是几个普通的键，后面是几张表；TOML 要求普通的键写在所有 [表] 之前。

    title = "新建一个笔记文件"               必填：一句话说清这个任务
    tags = ["files"]                        分组用：--tasks files 选中带这个标签的任务
    messages = ["帮我新建 notes.md ……"]     必填：用户依次发的话（不止一句就是多轮对话）
    approval = "edits"                      这一次用哪一档确认（ask / readonly / edits / all，见 permissions.py），默认 ask
    answer = "approve"                      要你确认时，模拟的用户怎么答：approve 允许（默认）/ deny 拒绝
    web = false                             要不要带上联网搜索（默认不带：花钱、结果每次不一样）
    requires = ["vision"]                   要模型能做什么：vision 看图、pdf 读 PDF、web 联网。做不到的模型跳过这个任务
    setup = [["git", "init", "-q"]]         准备工作目录时依次运行的命令（参数列表，不经过 shell；{python} 换成当前的 Python）

    [[files]]          工作目录里预先放的文件：path 和 text（文字）、base64（任意字节）、size（这么多字节的填充）三者之一
    [[attachments]]    随第一条消息一起发的附件：path 是文件名，内容的写法同上
    [[memories]]       预先存好的记忆：path 是 /memories 里的位置（user.md），text 是内容
    [[todos]]          预先有的待办：title，可以加 due、note、done
    [[skills]]         预先装好的 Skill：name 和 SKILL.md 的全文 text
    [[agents]]         预先有的自定义 Agent：name 和整个文件 text

    [budget]           估算花费用的假定用量：requests、context、output（见 config.py 的 EVAL_ASSUMED_*）

    [[graders]]        做完以后怎么打分，一项一项检查（种类和参数见 graders.py）
    type = "file_contains"
    path = "notes.md"
    text = "周五交周报"

    [[dry_run]]        演习时模型的回答，一次请求一项，按顺序：say 是它说的话，[[dry_run.tools]] 是它调用的工具
    say = "好的。"
    [[dry_run.tools]]
    name = "str_replace_based_edit_tool"
    input = { command = "create", path = "notes.md", file_text = "周五交周报\\n" }

演习的回答要是一份"标准答案"：照着它做，每一项检查都该通过（tests/test_evals.py 会对每个任务验一遍）。
标准答案过不了的检查，要么是检查写错了，要么是任务本身说不清楚，跑真的之前就要改。

检查文件只在读的时候做一次：哪里写错了，错误信息里写着是哪个文件、哪一项，免得跑到一半才发现。
"""

from __future__ import annotations

import base64
import binascii
import fnmatch
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ..config import EVAL_ASSUMED_CONTEXT, EVAL_ASSUMED_OUTPUT, EVAL_ASSUMED_REQUESTS
from ..i18n import t
from ..permissions import MODES
from . import graders

SUFFIX = ".toml"
ANSWERS = ("approve", "deny")
REQUIREMENTS = ("vision", "pdf", "web")
ID = re.compile(r"[a-z0-9][a-z0-9-]*")
MAX_FILE_BYTES = 5_000_000  # 预先放的一个文件最多这么大（size 那种填充也算）
KEYS = {
    "title", "tags", "messages", "approval", "answer", "web", "requires", "setup",
    "files", "attachments", "memories", "todos", "skills", "agents", "budget", "graders", "dry_run",
}


class TaskFileError(ValueError):
    """任务文件写得不对。错误信息可以直接给人看，开头写着是哪个文件。"""


@dataclass(frozen=True)
class Seed:
    """预先放好的一个文件（工作目录里的、附件、记忆）。"""

    path: str
    content: bytes


@dataclass(frozen=True)
class Call:
    """演习时模型的一次工具调用。"""

    name: str
    input: dict


@dataclass(frozen=True)
class Reply:
    """演习时模型的一次回答：先说的话，然后调用的工具（没有工具就是这一轮答完了）。"""

    say: str = ""
    tools: tuple[Call, ...] = ()


@dataclass(frozen=True)
class Check:
    """一项检查：种类和参数（见 graders.py）。"""

    type: str
    params: dict

    @property
    def costs(self) -> bool:
        """这一项要不要另外花钱（请模型当评分员）。"""
        return graders.KINDS[self.type].costs


@dataclass(frozen=True)
class Budget:
    requests: int = EVAL_ASSUMED_REQUESTS
    context: int = EVAL_ASSUMED_CONTEXT
    output: int = EVAL_ASSUMED_OUTPUT


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    messages: tuple[str, ...]
    path: Path
    tags: tuple[str, ...] = ()
    approval: str = "ask"
    answer: str = "approve"
    web: bool = False
    requires: tuple[str, ...] = ()
    setup: tuple[tuple[str, ...], ...] = ()
    files: tuple[Seed, ...] = ()
    attachments: tuple[Seed, ...] = ()
    memories: tuple[Seed, ...] = ()
    todos: tuple[dict, ...] = ()
    skills: tuple[tuple[str, str], ...] = ()  # （名字，SKILL.md 的全文）
    agents: tuple[tuple[str, str], ...] = ()  # （名字，整个文件）
    budget: Budget = field(default_factory=Budget)
    checks: tuple[Check, ...] = ()
    script: tuple[Reply, ...] = ()  # 演习时的标准答案

    @property
    def judged(self) -> int:
        """有几项检查要请模型评分（花钱）。"""
        return sum(1 for check in self.checks if check.costs)


# ---- 读文件 -----------------------------------------------------------------


def load_tasks(folder: Path) -> list[Task]:
    """读出文件夹里的全部任务（按编号排好）。有一个写错了就抛出 TaskFileError：宁可一个都不跑，也不要跑到一半才发现。"""
    if not folder.is_dir():
        raise TaskFileError(t("找不到放评测任务的文件夹：{path}", path=folder))
    return [load_task(path) for path in sorted(folder.glob("*" + SUFFIX))]


def load_task(path: Path) -> Task:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise TaskFileError(t("{file}：读不了（{error}）", file=path.name, error=exc)) from exc
    except tomllib.TOMLDecodeError as exc:
        raise TaskFileError(t("{file}：不是合法的 TOML（{error}）", file=path.name, error=exc)) from exc
    try:
        return _task(path, raw)
    except _Problem as problem:
        raise TaskFileError(f"{path.name}: {problem.args[0]}") from None


class _Problem(Exception):
    """文件里的一处问题（还不知道是哪个文件，由 load_task 补上）。"""


def _task(path: Path, raw: dict) -> Task:
    stem = path.name.removesuffix(SUFFIX)
    if not ID.fullmatch(stem):
        raise _Problem(t("文件名（去掉 .toml）就是任务的编号，只能有小写字母、数字和连字符"))
    unknown = sorted(set(raw) - KEYS)
    if unknown:
        raise _Problem(t("不认识这些项：{names}", names=", ".join(unknown)))
    title = _text(raw, "title", required=True)
    messages = _texts(raw, "messages")
    if not messages:
        raise _Problem(t("messages 至少要有一句话"))
    approval = _text(raw, "approval") or "ask"
    if approval not in MODES:
        raise _Problem(t("approval 只能是 {choices} 之一", choices=", ".join(MODES)))
    answer = _text(raw, "answer") or "approve"
    if answer not in ANSWERS:
        raise _Problem(t("answer 只能是 {choices} 之一", choices=", ".join(ANSWERS)))
    requires = _texts(raw, "requires")
    if any(item not in REQUIREMENTS for item in requires):
        raise _Problem(t("requires 只能是 {choices} 里的几个", choices=", ".join(REQUIREMENTS)))
    web = raw.get("web", False)
    if not isinstance(web, bool):
        raise _Problem(t("{key} 要写 true 或 false", key="web"))
    checks = tuple(_check(item, n) for n, item in enumerate(_tables(raw, "graders"), 1))
    if not checks:
        raise _Problem(t("至少要有一项检查（[[graders]]）"))
    script = tuple(_reply(item, n) for n, item in enumerate(_tables(raw, "dry_run"), 1))
    if not script:
        raise _Problem(t("要写演习时的标准答案（[[dry_run]]）：照着它做，每一项检查都该通过"))
    return Task(
        id=stem,
        title=title,
        messages=tuple(messages),
        path=path,
        tags=tuple(_texts(raw, "tags")),
        approval=approval,
        answer=answer,
        web=web,
        requires=tuple(requires),
        setup=tuple(_argv(item, "setup") for item in _list(raw, "setup")),
        files=tuple(_seed(item, "files") for item in _tables(raw, "files")),
        attachments=tuple(_seed(item, "attachments") for item in _tables(raw, "attachments")),
        memories=tuple(_seed(item, "memories") for item in _tables(raw, "memories")),
        todos=tuple(_todo(item) for item in _tables(raw, "todos")),
        skills=tuple(_named(item, "skills") for item in _tables(raw, "skills")),
        agents=tuple(_named(item, "agents") for item in _tables(raw, "agents")),
        budget=_budget(raw.get("budget")),
        checks=checks,
        script=script,
    )


def _text(raw: dict, key: str, required: bool = False) -> str:
    value = raw.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise _Problem(t("{key} 要写成一段不是空的文字", key=key))
    return value


def _texts(raw: dict, key: str) -> list[str]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise _Problem(t("{key} 要写成一串文字：[\"……\", \"……\"]", key=key))
    return value


def _list(raw: dict, key: str) -> list:
    value = raw.get(key, [])
    if not isinstance(value, list):
        raise _Problem(t("{key} 要写成一个列表", key=key))
    return value


def _tables(raw: dict, key: str) -> list[dict]:
    items = _list(raw, key)
    if not all(isinstance(item, dict) for item in items):
        raise _Problem(t("{key} 的每一项都要是一张表（[[{key}]]）", key=key))
    return items


def _argv(value: object, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(part, str) and part for part in value):
        raise _Problem(t("{key} 的每一条命令要写成参数列表：[\"git\", \"init\"]", key=key))
    return tuple(value)


def _path(value: object, key: str) -> str:
    """预先放的文件的位置：只能是相对路径，不能用 .. 跳出去（放进临时文件夹的东西，不能写到别处去）。"""
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise _Problem(t("{key} 里的 path 要写成用 / 分隔的相对路径", key=key))
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or ":" in path.parts[0]:
        raise _Problem(t("{key} 里的 path 只能是相对路径，不能用 ..：{path}", key=key, path=value))
    return str(path)


def _seed(item: dict, key: str) -> Seed:
    path = _path(item.get("path"), key)
    given = [name for name in ("text", "base64", "size") if name in item]
    if len(given) != 1 or set(item) - {"path", "text", "base64", "size"}:
        raise _Problem(t("{key} 的每一项写 path，再加上 text、base64、size 三者之一：{path}", key=key, path=path))
    if "text" in item:
        if not isinstance(item["text"], str):
            raise _Problem(t("{key} 里的 text 要是文字：{path}", key=key, path=path))
        content = item["text"].encode("utf-8")
    elif "base64" in item:
        try:
            content = base64.b64decode(str(item["base64"]), validate=True)
        except (binascii.Error, ValueError):
            raise _Problem(t("{key} 里的 base64 不对：{path}", key=key, path=path)) from None
    else:
        size = item["size"]
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_FILE_BYTES:
            raise _Problem(t("{key} 里的 size 要是 0 到 {n} 之间的整数：{path}", key=key, n=MAX_FILE_BYTES, path=path))
        content = b"\0" * size
    if len(content) > MAX_FILE_BYTES:
        raise _Problem(t("{key} 里的文件太大了（最多 {n} 字节）：{path}", key=key, n=MAX_FILE_BYTES, path=path))
    return Seed(path, content)


def _todo(item: dict) -> dict:
    if set(item) - {"title", "due", "note", "done"} or not isinstance(item.get("title"), str) or not item["title"].strip():
        raise _Problem(t("todos 的每一项要有 title，可以加 due、note、done"))
    if not isinstance(item.get("done", False), bool):
        raise _Problem(t("{key} 要写 true 或 false", key="done"))
    return dict(item)


def _named(item: dict, key: str) -> tuple[str, str]:
    name, text = item.get("name"), item.get("text")
    if set(item) - {"name", "text"} or not isinstance(name, str) or not name.strip() or not isinstance(text, str) or not text.strip():
        raise _Problem(t("{key} 的每一项要有 name 和 text", key=key))
    if "/" in name or "\\" in name or name.startswith("."):
        raise _Problem(t("{key} 里的 name 不能有斜杠，也不能以 . 开头：{name}", key=key, name=name))
    return name, text


def _budget(value: object) -> Budget:
    if value is None:
        return Budget()
    if not isinstance(value, dict) or set(value) - {"requests", "context", "output"}:
        raise _Problem(t("budget 里只能写 requests、context、output"))
    numbers = {}
    for key, number in value.items():
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise _Problem(t("budget 里的 {key} 要是正整数", key=key))
        numbers[key] = number
    return Budget(**numbers)


def _check(item: dict, n: int) -> Check:
    kind = item.get("type")
    if kind not in graders.KINDS:
        raise _Problem(t("第 {n} 项检查的 type 不认识：{type}。可以用：{known}", n=n, type=kind, known=", ".join(graders.KINDS)))
    params = {key: value for key, value in item.items() if key != "type"}
    problem = graders.KINDS[kind].problem(params)
    if problem:
        raise _Problem(t("第 {n} 项检查（{type}）：{problem}", n=n, type=kind, problem=problem))
    return Check(kind, params)


def _reply(item: dict, n: int) -> Reply:
    if set(item) - {"say", "tools"}:
        raise _Problem(t("dry_run 的第 {n} 项只能写 say 和 tools", n=n))
    say = item.get("say", "")
    if not isinstance(say, str):
        raise _Problem(t("dry_run 的第 {n} 项：say 要是文字", n=n))
    calls = []
    for tool in item.get("tools", []):
        if not isinstance(tool, dict) or set(tool) - {"name", "input"} or not isinstance(tool.get("name"), str) or not isinstance(tool.get("input", {}), dict):
            raise _Problem(t("dry_run 的第 {n} 项：每个工具要写 name 和 input（一张表）", n=n))
        calls.append(Call(tool["name"], tool.get("input", {})))
    if not say and not calls:
        raise _Problem(t("dry_run 的第 {n} 项是空的：至少要写 say 或 tools", n=n))
    return Reply(say, tuple(calls))


# ---- 挑任务 -----------------------------------------------------------------


def select(tasks: list[Task], patterns: str | None) -> list[Task]:
    """按 --tasks 挑出要跑的任务。逗号隔开几个：每一个是编号的通配符（files-*）或者一个标签（memory）。不给就是全部。"""
    if not patterns:
        return list(tasks)
    wanted = [part.strip() for part in patterns.split(",") if part.strip()]
    return [task for task in tasks if any(fnmatch.fnmatchcase(task.id, part) or part in task.tags for part in wanted)]
