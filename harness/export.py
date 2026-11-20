"""一键导出：把数据文件夹打成一个 zip，给你留一份备份。

导出的是数据文件夹（config.data_dir）里的全部东西：会话、记忆、待办、设置、花费账本、定时任务、Skill、
媒体库……以后新加的功能，只要把数据放在这个文件夹里，就会自动跟着导出，不用改这里。

不导出的，以及做了处理的：
  - 密钥。API key 在 .env 里，本来就不在数据文件夹里；万一被放了进来（叫 .env、.env.xxx 或者 xxx.env），跳过。
  - MCP 的配置（mcp.json）照样导出，但里面的密钥换成占位符 PLACEHOLDER：启动 MCP 服务器用的环境变量（env）、
    请求头（headers）里的每一个值，名字像密钥的字段（key、token、secret、password……）的值，命令行参数里
    跟在 --token 这类开关后面的值，以及网址里的密码、长得像密钥的字符串（sk-…、ghp_…）。名字（比如
    GITHUB_TOKEN）留着，恢复以后照着填回去就行。读不懂（不是合法的 JSON）的话整个文件不导出：
    宁可少导出一份，也不冒险把密钥带出去。以后再有存着密钥的配置文件，加进 MASKED_FILES。
  - 符号链接（替身）不导出：它可能指向数据文件夹外面的任何地方，比如你的 ~/.ssh。
  - 程序运行时自己用的东西不导出，也不提：占住数据目录的锁（.lock）、写到一半的临时文件（*.tmp）。

压缩包的最外层就是数据文件夹里的内容，另外多一个 README-backup.txt：导出的时间、怎么恢复、哪些文件做了处理。
恢复的办法：先退出智狐，再把压缩包解压到数据文件夹里（两份 README 里也写着）。

压缩包写在系统的临时文件里，不放在内存里，数据文件夹再大也不怕；由 server.py 发给浏览器，发完就删。
"""

from __future__ import annotations

import json
import os
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from .config import APP_NAME
from .i18n import t
from .safe import to_json

MASKED_FILES = ("mcp.json",)  # 存着密钥的配置文件（相对数据文件夹的路径）：导出时把密钥换成占位符
PLACEHOLDER = "<removed-from-backup>"  # 写成英文：它留在配置文件里，恢复以后谁来看都认得出
NOTE = "README-backup.txt"
# 本来就压缩过的格式，再压一遍只是白费时间：原样存进去
STORED = {".zip", ".gz", ".xz", ".bz2", ".7z", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".avif",
          ".pdf", ".mp3", ".m4a", ".mp4", ".mov", ".webm", ".docx", ".xlsx", ".pptx"}

# 名字里有这些字的字段，值当作密钥。宁可多遮几个（比如 author 也会被遮掉），也不要漏掉一个
SECRET_NAME = re.compile(r"key|token|secret|passw|credential|auth|cookie|bearer|session|private", re.IGNORECASE)
# 这几个字段里的每一个值都当作密钥（名字留着）：环境变量、请求头
SECRET_GROUPS = {"env", "environment", "headers"}
# 长得像密钥的字符串，不管它在哪个字段里
SECRET_SHAPES = [
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"),  # OpenAI、Anthropic、DeepSeek 这类
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),  # Slack
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),  # Google
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS
]
_AUTH_HEADER = re.compile(r"\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)  # "Bearer xxx"：留下前面那个词
_URL_PASSWORD = re.compile(r"(://[^/\s:@]+:)[^/\s@]+@")  # https://用户:密码@host
_URL_SECRET = re.compile(r"([?&][^=&#\s]*(?:key|token|secret|passw|auth|sig)[^=&#\s]*=)[^&#\s]+", re.IGNORECASE)


# ---- 把密钥换成占位符 ------------------------------------------------------------


def mask(value: Any, hide: bool = False) -> Any:
    """把一份配置（解析好的 JSON）里的密钥换成 PLACEHOLDER。hide 为真时，里面的每一个值都换掉。"""
    if isinstance(value, dict):
        return {key: mask(item, hide or _hides(key, item)) for key, item in value.items()}
    if isinstance(value, list):
        return [mask(item, True) for item in value] if hide else _mask_arguments(value)
    if value is None or isinstance(value, bool):
        return value  # 开关和空值不是密钥，留着更好恢复
    if hide:
        return PLACEHOLDER
    return _mask_text(value) if isinstance(value, str) else value


def _hides(key: object, value: Any) -> bool:
    """这个字段的值要不要整个换掉。名字像密钥、值又是一组设置（dict）的，不整个换，往里面一层层看：
    比如 MCP 服务器的名字叫 "auth-helper"，它的设置里大部分都不是密钥。"""
    name = str(key).lower()
    return name in SECRET_GROUPS or (bool(SECRET_NAME.search(name)) and not isinstance(value, dict))


def _mask_arguments(items: list) -> list:
    """一串命令行参数：--token xxx、--api-key=xxx 这样的，把 xxx 换掉。别的一项一项照常检查。"""
    out: list = []
    hide_next = False
    for item in items:
        if hide_next and isinstance(item, str) and not item.startswith("-"):
            out.append(PLACEHOLDER)
            hide_next = False
            continue
        hide_next = False
        if isinstance(item, str) and item.startswith("-"):
            flag, equals, _ = item.partition("=")
            if SECRET_NAME.search(flag):
                if equals:
                    out.append(flag + "=" + PLACEHOLDER)
                    continue
                hide_next = True
        out.append(mask(item))
    return out


def _mask_text(text: str) -> str:
    text = _URL_PASSWORD.sub(lambda match: match.group(1) + PLACEHOLDER + "@", text)
    text = _URL_SECRET.sub(lambda match: match.group(1) + PLACEHOLDER, text)
    text = _AUTH_HEADER.sub(lambda match: match.group(1) + " " + PLACEHOLDER, text)
    for shape in SECRET_SHAPES:
        text = shape.sub(PLACEHOLDER, text)
    return text


# ---- 要导出哪些文件 --------------------------------------------------------------


@dataclass
class Plan:
    """一次导出要做的事：哪些文件原样放进去、哪些换掉了密钥、哪些不导出（和原因）。"""

    files: list[tuple[Path, str]] = field(default_factory=list)  # （路径，在压缩包里的名字）
    masked: dict[str, bytes] = field(default_factory=dict)  # 在压缩包里的名字 → 换掉密钥以后的内容
    skipped: list[tuple[str, str]] = field(default_factory=list)  # （名字，原因）
    size: int = 0  # 要导出的内容一共多少字节（压缩之前）

    @property
    def count(self) -> int:
        return len(self.files) + len(self.masked)


def _is_secret_file(name: str) -> bool:
    return name == ".env" or name.startswith(".env.") or name.endswith(".env")


def _is_scratch(relative: str, name: str) -> bool:
    """程序运行时自己用的东西：锁、写到一半的临时文件、上一次导出时放进去的说明（解压回来以后就在数据文件夹里）。"""
    return relative in (".lock", NOTE) or name.endswith(".tmp")


def plan(data_dir: Path, secrets: Iterable[Path] = ()) -> Plan:
    """看一遍数据文件夹，定下要导出什么。secrets 是另外要挡住的文件（比如 .env 被放进了数据文件夹）。"""
    result = Plan()
    if not data_dir.is_dir():
        return result
    blocked = {os.path.realpath(path) for path in secrets}
    for folder, folders, names in os.walk(data_dir):
        here = Path(folder)
        for name in sorted(folders):
            if (here / name).is_symlink():  # os.walk 不会进去，但要说一声没导出
                result.skipped.append(((here / name).relative_to(data_dir).as_posix() + "/", t("符号链接，可能指向数据文件夹外面")))
        folders[:] = sorted(name for name in folders if not (here / name).is_symlink())
        for name in sorted(names):
            path = here / name
            relative = path.relative_to(data_dir).as_posix()
            if _is_scratch(relative, name):
                continue
            if _is_secret_file(name) or os.path.realpath(path) in blocked:
                result.skipped.append((relative, t("存着密钥的文件")))
            elif path.is_symlink():
                result.skipped.append((relative, t("符号链接，可能指向数据文件夹外面")))
            elif relative in MASKED_FILES:
                _plan_masked(result, path, relative)
            else:
                try:
                    result.size += path.stat().st_size
                except OSError as exc:
                    result.skipped.append((relative, t("读不了：{error}", error=exc.strerror or exc)))
                    continue
                result.files.append((path, relative))
    return result


def _plan_masked(result: Plan, path: Path, relative: str) -> None:
    """存着密钥的配置文件：读出来、把密钥换掉，导出换过的那一份。读不懂的不导出。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        result.skipped.append((relative, t("读不了：{error}", error=exc.strerror or exc)))
        return
    except (ValueError, RecursionError):
        result.skipped.append((relative, t("里面可能有密钥，但它不是合法的 JSON，没法只遮住密钥，所以整个没有导出")))
        return
    content = (to_json(mask(data), readable=True, indent=2) + "\n").encode()
    result.masked[relative] = content
    result.size += len(content)


# ---- 写压缩包 --------------------------------------------------------------------


def archive_name(moment: datetime) -> str:
    return f"zhifox-backup-{moment:%Y%m%d-%H%M}.zip"


def write_archive(data_dir: Path, out: IO[bytes], moment: datetime, secrets: Iterable[Path] = ()) -> Plan:
    """把数据文件夹写成一个 zip，写进 out。返回这次导出的 Plan（包括写的时候才发现读不了、只好跳过的）。"""
    work = plan(data_dir, secrets)
    # strict_timestamps=False：1980 年以前的修改时间（zip 格式表示不了）按 1980 年算，而不是整个导出失败
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, strict_timestamps=False) as archive:
        written: list[tuple[Path, str]] = []
        for path, relative in work.files:
            stored = path.suffix.lower() in STORED
            try:
                archive.write(path, relative, compress_type=zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED)
            except OSError as exc:  # 看的时候还在、写的时候没了，或者没有权限读
                work.skipped.append((relative, t("读不了：{error}", error=exc.strerror or exc)))
                continue
            written.append((path, relative))
        work.files = written
        for relative, content in work.masked.items():
            info = zipfile.ZipInfo.from_file(data_dir / relative, relative, strict_timestamps=False)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
        note = zipfile.ZipInfo(NOTE, moment.timetuple()[:6])
        note.compress_type = zipfile.ZIP_DEFLATED
        note.external_attr = 0o600 << 16  # 解压出来也只有自己的账号能读，和数据文件夹里别的文件一样
        archive.writestr(note, readme(work, data_dir, moment).encode())
    return work


def readme(work: Plan, data_dir: Path, moment: datetime) -> str:
    """压缩包里那份说明（用当前的界面语言写）。"""
    lines = [
        t("{name}数据备份", name=APP_NAME),
        t("导出时间：{time}", time=f"{moment:%Y-%m-%d %H:%M}"),
        t("数据文件夹：{path}", path=data_dir),
        t("共 {n} 个文件。", n=work.count),
        "",
        t("怎么恢复："),
        t("1. 先退出{name}（在它的终端里按 Ctrl-C）。", name=APP_NAME),
        t("2. 把这个压缩包解压到数据文件夹里，覆盖同名的文件。想完全回到备份时的样子，先把现在的数据文件夹改个名字放到一边，再解压到一个新建的空文件夹里，用它当数据文件夹。"),
        t("3. 重新启动{name}。", name=APP_NAME),
        "",
        t("这份备份不含 API 密钥：它们保存在 .env 文件里，不在数据文件夹里。"),
    ]
    if work.masked:
        lines += ["", t("下面这些文件里的密钥换成了 {placeholder}，恢复以后要重新填写：", placeholder=PLACEHOLDER)]
        lines += [f"  {name}" for name in work.masked]
    if work.skipped:
        lines += ["", t("下面这些没有导出：")]
        lines += ["  " + t("{name}（{reason}）", name=name, reason=reason) for name, reason in work.skipped]
    return "\n".join(lines) + "\n"
