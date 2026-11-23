"""一键导出：把数据文件夹打成一个 zip，给你留一份备份。

导出的是数据文件夹（config.data_dir）里的全部东西：会话、记忆、待办、设置、花费账本、定时任务、Skill、
媒体库……以后新加的功能，只要把数据放在这个文件夹里，就会自动跟着导出，不用改这里。

不导出的，以及做了处理的：
  - 密钥。API key 在 .env 里。从仓库里运行时它本来就不在数据文件夹里；装成工具时（见 install.py）它就在数据文件夹里，
    和别的文件放在一起。不管在不在，按名字（叫 .env、.env.xxx 或者 xxx.env）和按路径（Config.env_file，HARNESS_ENV_FILE
    换到哪儿都认得，见 plan 的 secrets）都跳过。
  - MCP 的配置（mcp.json）照样导出，但里面的密钥换成占位符 PLACEHOLDER：启动 MCP 服务器用的环境变量（env）、
    请求头（headers）里的每一个值，名字像密钥的字段（key、token、secret、password……）的值，命令行参数里
    跟在 --token 这类开关后面的值，以及网址里的密码、长得像密钥的字符串（sk-…、ghp_…）。名字（比如
    GITHUB_TOKEN）留着，恢复以后照着填回去就行。读不懂（不是合法的 JSON）的话整个文件不导出：
    宁可少导出一份，也不冒险把密钥带出去。以后再有存着密钥的配置文件，加进 MASKED_FILES。
  - 符号链接（替身）不导出：它可能指向数据文件夹外面的任何地方，比如你的 ~/.ssh。
  - 浏览器的配置文件夹（browser/profile，见 browser/）不导出：里面是你在智狐的浏览器里登录过的网站的 cookie，
    拿到它就能冒充你登录那些网站，和密钥一样不该被带出去；剩下的只是缓存，恢复以后重新登录就行。截图（browser/shots）照样导出。
  - 程序运行时自己用的东西不导出，也不提：占住数据目录的锁（.lock、*.lock）、写到一半的临时文件（*.tmp），
    检查新版本的缓存（updates.json，见 updates.py：只是上一次问 GitHub 的结果，删了下次再问）。

  - 恢复之前自动留的备份（restore-backups/，见 restore.py）不导出：每一份都是整个数据文件夹，带进去会越套越大。

压缩包的最外层就是数据文件夹里的内容，另外多两个文件：README-backup.txt 写给人看（导出的时间、怎么恢复、哪些文件做了处理），
backup-manifest.json 写给程序看（格式的版本、导出的时间、哪些文件遮了密钥，见 manifest）。

恢复（restore.py）只认 PARTS 里列的这些东西（白名单），别的名字一律拒绝，所以这两处写在一起：
以后新加的功能在数据文件夹里放了新的文件或文件夹，导出不用改，但要在 PARTS 里加一行，不然它的备份恢复不了
（tests/test_restore.py 会拿演示的数据目录对一遍）。恢复的办法：设置里的"从备份恢复…"，或者 harness restore（两份 README 里也写着）。

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

from . import pathnames
from .config import (
    APP_NAME,
    APP_NAME_LATIN,
    RESTORE_BACKUPS_DIR,
    SERVICE_STOP_REQUEST,
    UPDATE_CACHE,
    WEB_ADDRESS,
    WEB_TOKEN,
)
from .i18n import k, t
from .redact import mask_shapes
from .safe import to_json

# 存着密钥的配置文件（相对数据文件夹的路径）：导出时把密钥换成占位符。
# channels.json 里是聊天机器人的 token（见 channels/store.py），字段名就叫 token，配对码叫 secret，都会被换掉。
# endpoints.json 里是自定义模型接口的密钥（见 endpoints/store.py），字段名是 api_key。
# hooks.json 里钩子的参数可能带着令牌（--token xxx，见 hooks/config.py），按命令行参数的规矩遮住
MASKED_FILES = ("mcp.json", "channels.json", "endpoints.json", "hooks.json")
PLACEHOLDER = "<removed-from-backup>"  # 写成英文：它留在配置文件里，恢复以后谁来看都认得出
NOTE = "README-backup.txt"
MANIFEST = "backup-manifest.json"  # 写给程序看的说明（见 manifest）。恢复时读它，不写进数据文件夹
MANIFEST_FORMAT = 1  # 压缩包的格式：以后改了布局（不是加一种东西，是同一样东西换了放法）就加一，恢复时不认识的拒绝
# 整个不导出的文件夹（相对数据文件夹的路径）→ 原因（写进压缩包里的说明）
LEFT_OUT_FOLDERS = {
    "browser/profile": k("浏览器的配置文件夹：里面是登录过的网站的 cookie，和密钥一样不导出"),
    RESTORE_BACKUPS_DIR: k("恢复之前自动留的备份：每一份都是整个数据文件夹，带进去会越套越大"),
}
# 正在恢复时用的临时文件夹（见 restore.py）：写到一半的东西，不导出，也不提
STAGING_PREFIX = ".restore-"
# 本来就压缩过的格式，再压一遍只是白费时间：原样存进去
STORED = {".zip", ".gz", ".xz", ".bz2", ".7z", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".avif",
          ".pdf", ".mp3", ".m4a", ".mp4", ".mov", ".webm", ".docx", ".xlsx", ".pptx"}

# 名字里有这些字的字段，值当作密钥。宁可多遮几个（比如 author 也会被遮掉），也不要漏掉一个
SECRET_NAME = re.compile(r"key|token|secret|passw|credential|auth|cookie|bearer|session|private", re.IGNORECASE)
# 这几个字段里的每一个值都当作密钥（名字留着）：环境变量、请求头
SECRET_GROUPS = {"env", "environment", "headers"}
# 长得像密钥的字符串，不管它在哪个字段里：各家密钥的样子只在 redact.py 写一处（KEY_SHAPES 和配置文件用的 LOOSE_SHAPES）
_AUTH_HEADER = re.compile(r"\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)  # "Bearer xxx"：留下前面那个词
_URL_PASSWORD = re.compile(r"(://[^/\s:@]+:)[^/\s@]+@")  # https://用户:密码@host
_URL_SECRET = re.compile(r"([?&][^=&#\s]*(?:key|token|secret|passw|auth|sig)[^=&#\s]*=)[^&#\s]+", re.IGNORECASE)


# ---- 备份里有哪些东西（恢复的白名单）-----------------------------------------------


@dataclass(frozen=True)
class Part:
    """数据文件夹里的一样东西。导出时照常放进压缩包；恢复（restore.py）只认这些，别的名字一律拒绝。

    path 是相对数据文件夹的路径：一个文件，或者以 / 结尾的一个文件夹（里面的文件只查名字安不安全）。
    label 和 unit 是给人看的（预览里一行一样东西：名字，加上几个）；label 是空的就不单独列一行（skills.json 只是 Skill 的开关）。
    count：单个 JSON 文件里，哪个字段的长度是"有几个"（todos.json 的 items）。文件夹数文件的个数，by 是 folder 的数子文件夹。
    by：合并的时候按什么比。file 一个个文件（会话按编号、记忆按文件名、多媒体按内容）；folder 一整个子文件夹（一个 Skill
    是一个文件夹，不拆开和现在的拼在一起）；单个文件整个算一样（现在有就留着现在的）。
    restore 为假的认，但不写回去：日志是这台电脑上正开着的程序自己写的。
    """

    id: str
    path: str
    label: str = ""
    unit: str = ""
    count: str = ""
    by: str = "file"
    restore: bool = True

    @property
    def folder(self) -> bool:
        return self.path.endswith("/")


PARTS = (
    Part("conversations", "conversations/", k("会话"), k("{n} 段会话")),
    Part("memories", "memories/", k("记忆"), k("{n} 条记忆")),
    Part("todos", "todos.json", k("待办"), k("{n} 条待办"), count="items"),
    Part("tasks", "tasks.json", k("定时任务"), k("{n} 个定时任务"), count="tasks"),
    Part("settings", "settings.json", k("设置")),
    Part("prompt", "prompt.md", k("改过的提示词")),
    Part("ledger", "ledger.jsonl", k("花费账本")),
    Part("skills", "skills/", k("Skill"), k("{n} 个 Skill"), by="folder"),
    Part("skill_state", "skills.json"),
    Part("agents", "agents/", k("Agent"), k("{n} 个 Agent")),
    Part("mcp", "mcp.json", k("MCP 服务器"), k("{n} 个 MCP 服务器"), count="mcpServers"),
    Part("channels", "channels.json", k("聊天通道"), k("{n} 个聊天通道"), count="platforms"),
    Part("endpoints", "endpoints.json", k("自定义模型接口"), k("{n} 个自定义模型接口"), count="endpoints"),
    Part("hooks", "hooks.json", k("钩子"), k("{n} 个钩子"), count="hooks"),
    Part("commands", "commands.json", k("不用问的命令"), k("{n} 条命令"), count="commands"),
    Part("media", "media/", k("多媒体库"), k("{n} 个文件")),
    Part("backups", "backups/", k("改文件之前留的底"), k("{n} 个文件")),
    Part("browser_shots", "browser/shots/", k("浏览器的截图"), k("{n} 张截图")),
    Part("desktop_shots", "desktop/shots/", k("电脑的截图"), k("{n} 张截图")),
    Part("evals", "evals/", k("评测结果"), k("{n} 个文件")),
    Part("history", "input_history", k("终端里输入过的话")),
    Part("logs", "logs/", k("后台服务的日志"), restore=False),
)


def part_of(name: str) -> Part | None:
    """压缩包里的一项（相对数据文件夹的路径，分隔符是 /）属于哪一样东西。不属于任何一样返回 None。"""
    for part in PARTS:
        if (name.startswith(part.path) and len(name) > len(part.path)) if part.folder else name == part.path:
            return part
    return None


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
    return mask_shapes(text, PLACEHOLDER)


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


def is_secret_file(name: str) -> bool:
    return name == ".env" or name.startswith(".env.") or name.endswith(".env")


def _is_scratch(relative: str, name: str) -> bool:
    """程序运行时自己用的东西：锁、写到一半的临时文件、上一次导出时放进去的说明（解压回来以后就在数据文件夹里）。"""
    # .web-address、.web-token：网页的口令（serve --web 写的地址，后台服务存着的口令，见 webui/token.py），不带出去。
    # .service-stop：Windows 上请后台服务停下的那个文件（见 service/supervise.py），只在那一会儿有。
    # updates.json：上一次检查新版本的结果（见 updates.py），只是缓存：带到另一台电脑、另一个版本上没有意义，删了下次再问
    scratch = (".lock", NOTE, MANIFEST, WEB_ADDRESS, WEB_TOKEN, SERVICE_STOP_REQUEST, UPDATE_CACHE)
    return relative in scratch or name.endswith((".tmp", ".lock"))  # channels.json.lock：改设置时用的文件锁


def plan(data_dir: Path, secrets: Iterable[Path] = ()) -> Plan:
    """看一遍数据文件夹，定下要导出什么。secrets 是另外要挡住的文件（比如 .env 被放进了数据文件夹）。"""
    result = Plan()
    if not data_dir.is_dir():
        return result
    blocked = {os.path.realpath(path) for path in secrets}
    for folder, folders, names in os.walk(data_dir):
        here = Path(folder)
        for name in sorted(folders):
            # 链接（包括 Windows 的目录联接，is_symlink 认不出它，os.walk 会走进去）不进去，但要说一声没导出
            if pathnames.is_link(here / name):
                result.skipped.append(((here / name).relative_to(data_dir).as_posix() + "/", t("符号链接，可能指向数据文件夹外面")))
        folders[:] = sorted(name for name in folders if not pathnames.is_link(here / name))
        for name in list(folders):
            relative = (here / name).relative_to(data_dir).as_posix()
            if here == data_dir and name.startswith(STAGING_PREFIX):
                folders.remove(name)  # 正在恢复时的临时文件夹
            elif relative in LEFT_OUT_FOLDERS:
                folders.remove(name)
                result.skipped.append((relative + "/", t(LEFT_OUT_FOLDERS[relative])))
        for name in sorted(names):
            path = here / name
            relative = path.relative_to(data_dir).as_posix()
            if _is_scratch(relative, name):
                continue
            if is_secret_file(name) or os.path.realpath(path) in blocked:
                result.skipped.append((relative, t("存着密钥的文件")))
            elif pathnames.is_link(path):
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
        for name, text in ((NOTE, readme(work, data_dir, moment)), (MANIFEST, to_json(manifest(work, moment), readable=True, indent=2) + "\n")):
            info = zipfile.ZipInfo(name, moment.timetuple()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16  # 解压出来也只有自己的账号能读，和数据文件夹里别的文件一样
            archive.writestr(info, text.encode())
    return work


def manifest(work: Plan, moment: datetime) -> dict:
    """压缩包里写给程序看的那份说明（backup-manifest.json）。恢复时用 read_manifest 读回来：

      format     压缩包的格式（MANIFEST_FORMAT）
      app        ZhiFox（认出这是谁的备份）
      exported_at  导出的时刻，带着时区（2026-11-22T15:30:00+08:00）
      files、bytes  一共几个文件、多大（压缩之前）
      masked     哪些文件里的密钥换成了 PLACEHOLDER（恢复以后要重新填的就在这几个文件里）
      left_out   没有导出的（名字，文件夹以 / 结尾）：浏览器的配置文件夹、存着密钥的文件……
    """
    return {
        "format": MANIFEST_FORMAT,
        "app": APP_NAME_LATIN,
        "exported_at": moment.astimezone().isoformat(timespec="seconds"),
        "files": work.count,
        "bytes": work.size,
        "masked": sorted(work.masked),
        "left_out": [name for name, _ in work.skipped],
    }


def read_manifest(raw: bytes) -> dict | None:
    """读回 manifest 写的那份说明。读不懂、不是智狐写的返回 None；format 比这个版本认识的新，原样交回去，由调用的人拒绝。
    只留认识的字段、类型对的值：压缩包是从外面来的，里面写什么都有可能。"""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("app") != APP_NAME_LATIN or not isinstance(data.get("format"), int) or isinstance(data.get("format"), bool):
        return None
    try:
        moment = datetime.fromisoformat(data["exported_at"]) if isinstance(data.get("exported_at"), str) else None
    except ValueError:
        moment = None
    names = lambda value: [item for item in value if isinstance(item, str)][:1000] if isinstance(value, list) else []
    return {"format": data["format"], "exported_at": moment, "masked": names(data.get("masked")), "left_out": names(data.get("left_out"))}


def readme(work: Plan, data_dir: Path, moment: datetime) -> str:
    """压缩包里那份说明（用当前的界面语言写）。"""
    lines = [
        t("{name}数据备份", name=APP_NAME),
        t("导出时间：{time}", time=f"{moment:%Y-%m-%d %H:%M}"),
        t("数据文件夹：{path}", path=data_dir),
        t("共 {n} 个文件。", n=work.count),
        "",
        t("怎么恢复："),
        t("1. 在{name}的网页上打开“设置”，点“从备份恢复…”，选这个压缩包。先给你看里面有什么，你确认了才恢复；恢复之前会把现在的数据自动备份一份。", name=APP_NAME),
        t("2. 没有网页的时候（服务器上），先退出{name}，再运行 uv run harness restore 这个压缩包。", name=APP_NAME),
        t("3. 不要直接解压到数据文件夹里：密钥换成了占位符的那几个文件会把你现在填好的密钥盖掉。上面两种办法都会保留现在的密钥。"),
        "",
        t("这份备份不含 API 密钥：它们保存在 .env 文件里，这个文件从不导出。"),
    ]
    if work.masked:
        lines += ["", t("下面这些文件里的密钥换成了 {placeholder}，恢复以后要重新填写：", placeholder=PLACEHOLDER)]
        lines += [f"  {name}" for name in work.masked]
    if work.skipped:
        lines += ["", t("下面这些没有导出：")]
        lines += ["  " + t("{name}（{reason}）", name=name, reason=reason) for name, reason in work.skipped]
    return "\n".join(lines) + "\n"
