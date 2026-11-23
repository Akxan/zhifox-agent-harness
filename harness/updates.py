"""检查智狐有没有新的正式版本。默认关着。

为什么默认关着：这是智狐唯一一个不是你叫它做、它自己发出去的请求。别的请求都是你发起的（发一条消息、点"同步模型"、
连一个聊天通道），页面也不加载任何外部的东西。所以它要你自己在设置里打开（或者 harness updates on）。

打开以后做的事，一样不多：
  - 每 UPDATE_CHECK_HOURS 小时（一天）向 GitHub 的公开接口发一个 GET：
        GET https://api.github.com/repos/Akxan/zhifox-agent-harness/releases/latest
    不登录、不带 cookie，User-Agent 只写 ZhiFox（不写版本号、不写系统）。GitHub 看得到的只有你的 IP 地址和请求的时间：
    不发你的版本、设置、会话、密钥，也没有任何编号能把两次检查连起来。
  - 回答里只取 tag（v0.3.0）、发布的时间和附件的名字（看那一版有没有现成的 wheel）。草稿和预发布不算
    （releases/latest 本来就不给，这里再查一遍，tag 不是 0.3.0 这种纯数字的也不算）。
    发布页和 wheel 的地址由这里照着 tag 拼，不用回答里给的网址：页面上的链接永远指向 github.com 上的这个仓库。
  - 结果存在数据目录里（config.UPDATE_CACHE，一个小的 JSON）：什么时候问的、最新的正式版本、上一次没问成的原因。
    下次启动先看它：一天之内问过就不再问；没问成的隔 UPDATE_RETRY_HOURS 小时再试。它只是缓存：导出不带它，恢复也不写它。
  - 没问成（连不上、超时、被 GitHub 限流、回答看不懂）只写一行日志，设置里写"没能检查"，什么都不耽误。

谁来问（Updater）：
  网页界面、harness serve   lifecycle.start 开一个后台线程（Updater.start），到时间了问一次；lifecycle.stop 停下它
  终端界面                   启动时只看缓存；打开着、缓存又过期了，就在后台问一次（不耽误你打字），有新版本在下一次输入之前说一声
  harness doctor             只读缓存，永远不联网（见 doctor/environment.py）
  harness updates            看缓存、打开、关上、马上问一次（见 updates_cli.py）

有新版本时怎么升级（upgrade）：看智狐是怎么装的（config.FROM_SOURCE、install.origin），给出这一种装法的命令；
看不出来时给一条哪种装法都能用的，并且明说看不出来。这个文件里只放规矩，不碰网页：页面（webui/updates_api.py、
page/scripts/updates.js）、终端和自检都只照着说。
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import install, machine
from .config import (
    COMMAND,
    FROM_SOURCE,
    PROJECT_ROOT,
    UPDATE_CHECK_HOURS,
    UPDATE_MAX_BYTES,
    UPDATE_RETRY_HOURS,
    UPDATE_TIMEOUT,
    UPDATE_WAKE_SECONDS,
    Config,
)
from .i18n import Message, k, t
from .net import http
from .safe import private_dir, to_json, write_file
from .settings import Settings
from .version import version as installed_version

logger = logging.getLogger("harness.updates")

REPOSITORY = install.REPOSITORY  # https://github.com/Akxan/zhifox-agent-harness
SLUG = REPOSITORY.removeprefix("https://github.com/")  # Akxan/zhifox-agent-harness
# 发出去的请求头只有这几个（urllib 自己再加 Host、Accept-Encoding、Connection）。User-Agent 是 GitHub 要求必须有的，
# 只写一个名字：不写版本号（那会告诉 GitHub 你用的是哪一版），也不写系统
HEADERS = {"User-Agent": "ZhiFox", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
CACHE_FORMAT = 1  # 缓存文件的格式；读到不认识的当作没有缓存

# 正式版本的 tag：v0.3.0、0.3.0、v1.0（最多四段数字）。v0.3.0-rc.1、0.3.0b1、nightly 都不是
_STABLE = re.compile(r"[vV]?(\d{1,6}(?:\.\d{1,6}){0,3})")
# 现在装的版本：数字开头，后面可能还跟着别的（0.3.0rc1、0.3.0.dev2、0.2.0+local）
_INSTALLED = re.compile(r"[vV]?(\d{1,6}(?:\.\d{1,6}){0,3})(.*)", re.DOTALL)
# 跟在数字后面、表示"还不是正式版"的写法（PEP 440 的 a、b、rc、dev，和常见的 alpha、beta、pre）
_PRE = re.compile(r"[.\-_]?(a|b|c|rc|alpha|beta|pre|preview|dev)\d*", re.IGNORECASE)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

# 上一次没问成的原因（缓存里只存键和细节，给人看的话现翻译：换了界面语言也跟着换）
PROBLEMS = {
    "network": k("连不上 GitHub（{detail}）"),
    "limited": k("GitHub 暂时不让这台电脑再问了：不登录的话每个 IP 每小时只能问 60 次，同一个网络里别的程序也算在里面。过一会儿会再试"),
    "http": k("GitHub 回答了 HTTP {detail}"),
    "reply": k("GitHub 的回答看不懂"),
    "address": k("检查用的地址（HARNESS_UPDATE_API）不是 https://，没有去问"),
}

# 智狐现在是怎么开着的（upgrade 的 running）：升级完要怎样才用得上新版本
SERVICE = "service"  # 作为后台服务（harness service install 装的）
TERMINAL = "terminal"  # 在终端里开的（harness、harness --web）
UNKNOWN = ""  # 不知道（自检、harness updates：说的不是某一个开着的智狐）


# ---- 版本号 ----------------------------------------------------------------------


def stable(tag: object) -> tuple[int, ...] | None:
    """一个正式版本的 tag（v0.3.0、0.3.0）读成一串数字。不是正式版本的（v0.3.0-rc.1、0.3.0b1、nightly）返回 None。"""
    if not isinstance(tag, str):
        return None
    match = _STABLE.fullmatch(tag.strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def is_newer(candidate: str, current: str) -> bool:
    """candidate（一个正式版本）比 current（现在装的这一版）新吗。

    按数字一段一段比，不按字符串比：0.10.0 比 0.9.0 新，1.0 和 1.0.0 一样。current 是预发布或者开发中的版本
    （0.3.0rc1、0.3.0.dev2）时，同样数字的正式版本算更新的（PEP 440 也是这样排的）。
    读不懂的一律不算新：宁可不提醒，也不要每天提醒一个不存在的新版本。"""
    found = stable(candidate)
    match = _INSTALLED.fullmatch(current.strip()) if current else None
    if found is None or match is None:
        return False
    mine = tuple(int(part) for part in match.group(1).split("."))
    width = max(len(found), len(mine))
    theirs, ours = found + (0,) * (width - len(found)), mine + (0,) * (width - len(mine))
    if theirs != ours:
        return theirs > ours
    return bool(_PRE.match(match.group(2)))


# ---- 一个正式版本 ----------------------------------------------------------------


@dataclass(frozen=True)
class Release:
    version: str  # 0.3.0（tag 去掉前面的 v）
    tag: str  # v0.3.0：只由数字、点和开头的 v 组成（stable 查过），拼进网址是安全的
    published: str = ""  # 发布的时刻（GitHub 给的 2026-12-01T09:00:00Z）；看不懂就是空的
    wheel: bool = True  # 发布页上有没有现成的 wheel（install.wheel_name）

    @property
    def page(self) -> str:
        """这一版的发布页（发布说明、附件）。"""
        return f"{REPOSITORY}/releases/tag/{self.tag}"

    @property
    def wheel_url(self) -> str:
        return f"{REPOSITORY}/releases/download/{self.tag}/{install.wheel_name(self.version)}"

    @property
    def zip_url(self) -> str:
        """GitHub 给每个 tag 打的代码包：没有 wheel 时用它，也不用装 git。"""
        return f"{REPOSITORY}/archive/refs/tags/{self.tag}.zip"

    def saved(self) -> dict:
        return {"version": self.version, "tag": self.tag, "published": self.published, "wheel": self.wheel}

    @classmethod
    def load(cls, data: object) -> Release | None:
        """从缓存里读回来。文件可能被改坏了：tag 照样要是正式版本的样子，不然不认。"""
        if not isinstance(data, dict) or stable(data.get("tag")) is None:
            return None
        tag = str(data["tag"]).strip()
        published = data.get("published")
        return cls(tag.lstrip("vV"), tag, published if isinstance(published, str) and _DATE.fullmatch(published) else "", data.get("wheel") is not False)


class BadReply(Exception):
    """GitHub 的回答不是我们认识的样子。"""


def read_release(data: object) -> Release | None:
    """GitHub 的 releases/latest 回答里那一版。是草稿、预发布，或者 tag 不是正式版本的样子，返回 None（不算）。
    根本不是一个 release 的样子（没有 tag_name）抛出 BadReply。"""
    if not isinstance(data, dict) or not isinstance(data.get("tag_name"), str):
        raise BadReply
    if data.get("draft") is True or data.get("prerelease") is True:
        return None
    tag = data["tag_name"].strip()
    if stable(tag) is None:
        return None
    version = tag.lstrip("vV")
    published = data.get("published_at")
    assets = data.get("assets")
    names = {item.get("name") for item in assets if isinstance(item, dict)} if isinstance(assets, list) else set()
    return Release(version, tag, published if isinstance(published, str) and _DATE.fullmatch(published) else "", install.wheel_name(version) in names)


# ---- 缓存 ------------------------------------------------------------------------


@dataclass(frozen=True)
class Cache:
    checked_at: float | None = None  # 上一次去问的时刻（time.time()，问成没问成都算）
    latest: Release | None = None  # 最近一次问到的最新正式版本（没问成的那几次留着上一次的）
    problem: str = ""  # 上一次没问成的原因（PROBLEMS 的键）；问成了是空的
    detail: str = ""  # 原因的细节：系统给的连不上的说法、HTTP 状态码

    def saved(self) -> dict:
        return {"format": CACHE_FORMAT, "checked_at": self.checked_at, "latest": self.latest.saved() if self.latest else None,
                "problem": self.problem, "detail": self.detail}


def read_cache(path: Path) -> Cache:
    """读缓存。没有、读不了、看不懂（被改坏了、格式不认识）都当作没有：下一次到时间了再问。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return Cache()
    if not isinstance(data, dict) or data.get("format") != CACHE_FORMAT:
        return Cache()
    checked = data.get("checked_at")
    checked_at: float | None = None  # 被改坏了的（文字、NaN、几万年以后）当作没问过
    if isinstance(checked, (int, float)) and not isinstance(checked, bool) and math.isfinite(checked) and abs(checked) < 1e11:
        checked_at = float(checked)
    problem, detail = data.get("problem"), data.get("detail")
    return Cache(checked_at, Release.load(data.get("latest")), problem if isinstance(problem, str) and problem in PROBLEMS else "",
                 detail[:300] if isinstance(detail, str) else "")


def write_cache(path: Path, cache: Cache) -> None:
    """写缓存（权限 600）。数据目录还没有的话先建好，只有你的账号能进（和别处一样，见 safe.private_dir）。"""
    private_dir(path.parent)
    write_file(path, to_json(cache.saved(), readable=True, indent=2) + "\n")


def due(cache: Cache, now: float) -> bool:
    """到时间再问一次了吗：从来没问过、问过的时刻在"现在"之后（钟往回调过），或者隔得够久了（没问成的隔得短一些）。"""
    if cache.checked_at is None:
        return True
    elapsed = now - cache.checked_at
    hours = UPDATE_RETRY_HOURS if cache.problem else UPDATE_CHECK_HOURS
    return elapsed < 0 or elapsed >= hours * 3600


def problem_text(cache: Cache) -> Message | None:
    if not cache.problem:
        return None
    return t(PROBLEMS[cache.problem], detail=cache.detail)


# ---- 问一次 ----------------------------------------------------------------------


def latest_url(config: Config) -> str:
    return f"{config.update_api}/repos/{SLUG}/releases/latest"


def fetch(url: str) -> http.Reply:
    """真的发那一个请求。单独写成一个函数：测试换掉它，保证一个请求都到不了真的 GitHub（见 tests/conftest.py）。"""
    return http.request("GET", url, headers=HEADERS, timeout=UPDATE_TIMEOUT, limit=UPDATE_MAX_BYTES)


def ask(config: Config, previous: Cache, now: float) -> Cache:
    """问一次 GitHub，交回新的缓存。从不抛出异常：没问成的写进缓存的 problem，最近知道的那一版留着。"""
    url = latest_url(config)
    if not http.trusted(url):
        return Cache(now, previous.latest, "address")
    try:
        reply = fetch(url)
    except http.NetError as exc:
        reason = exc.args[0] if exc.args else ""
        # 连不上、超时的说法在 NetError 那句话的 {error} 里（系统给的，比如 timed out）；回答太大的那句没有，算看不懂
        if isinstance(reason, Message) and "error" in reason.values:
            return Cache(now, previous.latest, "network", str(reason.values["error"])[:300])
        return Cache(now, previous.latest, "reply")
    if reply.status == 404:
        return Cache(now, None)  # 一个正式版本都还没发布过（或者都删了）
    limited = reply.headers.get("x-ratelimit-remaining") == "0" or reply.status == 429
    if reply.status in (403, 429) and (limited or reply.retry_after()):
        return Cache(now, previous.latest, "limited")
    if not reply.ok:
        return Cache(now, previous.latest, "http", str(reply.status))
    try:
        release = read_release(reply.data)
    except BadReply:
        return Cache(now, previous.latest, "reply")
    # 草稿、预发布不算：最近知道的那一个正式版本照样留着（releases/latest 本来就不会给它们，这只是以防万一）
    return Cache(now, release if release is not None else previous.latest)


# ---- 给人看的状态 ----------------------------------------------------------------


@dataclass(frozen=True)
class Status:
    enabled: bool  # 打开了没有（设置里的 update_check）
    current: str  # 现在装的这一版（version.version()）
    checking: bool = False  # 正在问
    checked_at: float | None = None  # 上一次问的时刻
    latest: Release | None = None  # 最近知道的最新正式版本（不一定比现在的新）
    problem: Message | None = None  # 上一次没问成的原因

    @property
    def newer(self) -> Release | None:
        """有新版本的话是它。没打开时一律是 None：你关掉了，就不该再看到"有新版本"（缓存可能早就过时了）。"""
        if self.enabled and self.latest is not None and is_newer(self.latest.version, self.current):
            return self.latest
        return None


def status_of(settings: Settings, cache: Cache, current: str, checking: bool = False) -> Status:
    return Status(settings.update_check, current, checking, cache.checked_at, cache.latest, problem_text(cache))


def news_line(found: Release, current: str) -> Message:
    """终端启动时说的那一句（只有一行）。怎么升级要看是怎么装的，写起来好几行，所以请你去看 /doctor（或者 harness doctor）。"""
    return t("有新版本：智狐 {version}（现在是 {current}）。怎么升级见 /doctor，发布说明：{url}", version=found.version, current=current or "?", url=found.page)


# ---- 怎么升级 --------------------------------------------------------------------


@dataclass(frozen=True)
class Upgrade:
    """升级成某一版要做的事。commands 一条一行，照抄就能运行（三个系统上写法一样：每一条单独一行，
    不用 && 连起来，Windows PowerShell 5 不认 &&）；in_project 为真时要在智狐的文件夹里运行。"""

    lead: Message  # 第一句：在哪儿运行
    commands: list[str]
    in_project: bool = False
    manual: bool = False  # 运行之前先要你自己换一份新的代码（不是用 git 下载的文件夹）：lead 里写着怎么换
    before: list[Message] = field(default_factory=list)  # 运行之前（Windows 上先停下开着的智狐）
    notes: list[Message] = field(default_factory=list)  # 运行之后（重启才用得上新版本），和看不出是怎么装的时候的另一种办法


def upgrade(
    release: Release, running: str = UNKNOWN, *, source: bool = FROM_SOURCE, origin: str | None = None,
    git: bool | None = None, root: Path = PROJECT_ROOT, command: str = COMMAND, system: str | None = None,
) -> Upgrade:
    """升级成 release 这一版，在这台电脑上、按这一种装法要做的事。

    running：智狐现在是怎么开着的（SERVICE、TERMINAL，说不准是 UNKNOWN），决定最后那句"怎样才用得上新版本"。
    后面几个参数只给测试用：source（从仓库里运行，config.FROM_SOURCE）、origin（装成工具时从哪儿装的，install.origin）、
    git（仓库里有没有 .git）、root（仓库在哪儿）、command（智狐的命令怎么写）、system（哪种电脑，machine.system）。"""
    system = system or machine.system()
    windows = system == machine.WINDOWS
    shell = t("在 PowerShell 里运行：") if windows else t("在终端里运行：")
    before: list[Message] = []
    notes: list[Message] = []
    if source:
        has_git = (root / ".git").exists() if git is None else git
        if has_git:
            upgrade_plan = Upgrade(t("在智狐的文件夹（{path}）里运行：", path=root), ["git pull", "uv sync"], in_project=True)
        else:
            # 下载的 zip 解出来的文件夹：没有 git 可以拉，只能换一份新的代码
            lead = t("这个文件夹不是用 git 下载的（里面没有 .git）：从发布页下载 {version} 的源代码，解压到一个新的文件夹，"
                     "把原来文件夹里的 data/ 和 .env 搬过去，再在新的文件夹里运行：", version=release.version)
            upgrade_plan = Upgrade(lead, ["uv sync"], in_project=True, manual=True)
    else:
        found = install.origin() if origin is None else origin
        if found == install.GIT:
            line = f"uv tool upgrade {install.PROJECT_NAME}"
        elif found == install.GIT_PINNED:
            line = f"uv tool install --force git+{REPOSITORY}@{release.tag}"
        else:
            line = f"uv tool install --force {release.wheel_url if release.wheel else release.zip_url}"
        upgrade_plan = Upgrade(shell, [line])
        if found == install.UNKNOWN:
            notes.append(t("看不出当初是怎么装的，所以给的是哪种装法都能用的那一条：装成 {version} 这一版。"
                           "当初是从 git+https:// 装的、没写版本的话，也可以运行 uv tool upgrade {name}。", version=release.version, name=install.PROJECT_NAME))
        elif found == install.GIT_PINNED:
            notes.append(t("当初装的时候钉在了某一版上（@v…），uv tool upgrade 不会动它，所以换成新的这一版。"))
    if windows:
        # Windows 上正在运行的程序文件换不掉（uv 会报"拒绝访问"），所以要先停下开着的智狐
        if running == SERVICE:
            before.append(t("先停下后台服务：{command} service stop（Windows 上正在运行的程序换不掉）。", command=command))
            notes.append(t("装好以后再开起来：{command} service start。", command=command))
        elif running == TERMINAL:
            before.append(t("先退出开着的智狐（Windows 上正在运行的程序换不掉），装好以后再打开。"))
        else:
            before.append(t("先退出开着的智狐，装了后台服务的话用 {command} service stop 停下它（Windows 上正在运行的程序换不掉）。装好以后再打开，或者 {command} service start。", command=command))
    elif running == SERVICE:
        notes.append(t("智狐作为后台服务运行：装好以后运行 {command} service restart，新版本才会开起来。", command=command))
    elif running == TERMINAL:
        notes.append(t("装好以后退出现在开着的智狐、再重新打开，才会用上新版本。"))
    else:
        notes.append(t("装好以后，开着的智狐要重新打开才会用上新版本：后台服务用 {command} service restart，在终端里开的退出再打开。", command=command))
    return Upgrade(upgrade_plan.lead, upgrade_plan.commands, upgrade_plan.in_project, upgrade_plan.manual, before, [*upgrade_plan.notes, *notes])


# ---- 在后台问 --------------------------------------------------------------------


class Updater:
    """一个开着的智狐里管"检查新版本"的那一个：读写缓存、到时间了问一次、告诉界面结果。

    线程的规矩：self._lock 只保护下面这几个字段，拿着它的时候不发请求、不写文件、不调用 on_change
    （on_change 是网页的 publish_state，它要拿网页自己的锁）。同一时间最多一个在问（_checking）。"""

    def __init__(
        self, config: Config, settings: Settings, on_change: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.time, current: str | None = None,
    ):
        self.config = config
        self.settings = settings
        self.on_change = on_change or (lambda: None)
        self.clock = clock
        self.current = installed_version() if current is None else current
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)  # 叫醒后台线程：打开了、要马上问、要停下
        self._cache = read_cache(config.updates_file)
        self._checking = False
        self._forced = False  # 有人要马上问一次（刚打开、点了"现在检查"），不管到没到时间
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._news: Release | None = None  # 刚问到的、以前不知道的新版本（终端在下一次输入之前说一声，见 take_news）

    @property
    def enabled(self) -> bool:
        return self.settings.update_check

    def status(self) -> Status:
        with self._lock:
            return status_of(self.settings, self._cache, self.current, self._checking)

    def due(self) -> bool:
        with self._lock:
            return self.enabled and due(self._cache, self.clock())

    def take_news(self) -> Release | None:
        """后台刚问到的新版本（只交出一次）。终端在下一次输入之前说一声；网页靠状态，不用它。"""
        with self._lock:
            news, self._news = self._news, None
        return news if news is not None and self.enabled and is_newer(news.version, self.current) else None

    def set_enabled(self, enabled: bool) -> None:
        """打开或关上。打开时马上问一次（不管缓存新不新：你刚打开，想看的就是现在的结果）。写设置失败抛 OSError。"""
        self.settings.set_update_check(enabled)
        if enabled:
            self.check_soon()
        else:
            with self._lock:
                self._forced = False
                self._wake.notify_all()
        self.on_change()

    def check(self, force: bool = False) -> bool:
        """在调用它的线程里问一次（最多等 UPDATE_TIMEOUT 秒）。返回 True 表示真的去问了。
        没打开、还不到时间（force 为假时）、另一边正在问，都不问。"""
        with self._lock:
            if self._checking or not self.enabled or not (force or self._forced or due(self._cache, self.clock())):
                return False
            self._checking, self._forced = True, False
            previous = self._cache
        self.on_change()  # 页面上写"正在检查……"
        fresh = previous
        try:
            fresh = ask(self.config, previous, self.clock())
            if fresh.problem:
                logger.info("%s", t("没能检查新版本：{problem}", problem=problem_text(fresh)))
            try:
                write_cache(self.config.updates_file, fresh)
            except OSError as exc:  # 存不上也不要紧：这次的结果在内存里照样用，下次启动再问一次
                logger.warning("%s", t("检查新版本的结果没能存下来（{error}）", error=exc))
        finally:
            with self._lock:
                self._cache = fresh
                self._checking = False
                found = fresh.latest
                known = previous.latest.version if previous.latest else ""
                if found is not None and found.version != known and is_newer(found.version, self.current):
                    self._news = found
                self._wake.notify_all()
                stopping = self._stopping
        # 已经在退出（或者从备份恢复、换上了新的 WebApp）：结果照样存进了缓存，只是不再去刷新那个已经关掉的页面
        if not stopping:
            self.on_change()
        return True

    def check_soon(self) -> None:
        """马上问一次，但不在这个线程里等：后台线程开着就叫醒它，没开（终端、测试）就另开一个线程问完就走。"""
        with self._lock:
            if not self.enabled or self._stopping:
                return
            running = self._thread is not None and self._thread.is_alive()
            self._forced = True
            self._wake.notify_all()
        if not running:
            threading.Thread(target=self._check_once, daemon=True, name="update-check-now").start()

    def _check_once(self) -> None:
        try:
            self.check()
        except Exception:  # 什么错都接住：这只是一个提醒，不能让它在后台线程里炸出一堆输出
            logger.exception("%s", t("检查新版本时出了意料之外的错"))

    # ---- 后台线程（网页界面、harness serve：见 webui/lifecycle.py）----

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(target=self._run, daemon=True, name="update-check")
        self._thread.start()

    def stop(self, wait: float = 1.0) -> None:
        """停下后台线程。正在问的那一次不等它问完（最多等 wait 秒）：它是一个守护线程，程序退出时跟着结束。"""
        with self._lock:
            self._stopping = True
            self._forced = False
            self._wake.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(wait)

    def _run(self) -> None:
        while True:
            with self._lock:
                if self._stopping:
                    return
                wait = self._wait()
                if wait is None or wait > 0:
                    self._wake.wait(wait)  # 被叫醒（打开了、要马上问、要停下）或者到时间了，回到上面重新算
                    continue
            self._check_once()

    def _wait(self) -> float | None:
        """后台线程还要等多久再问（秒）。None：没打开、或者另一边正在问，等到被叫醒为止。调用的人拿着 self._lock。"""
        if not self.enabled or self._checking:
            return None
        if self._forced:
            return 0.0
        cache, now = self._cache, self.clock()
        if due(cache, now) or cache.checked_at is None:
            return 0.0
        hours = UPDATE_RETRY_HOURS if cache.problem else UPDATE_CHECK_HOURS
        return max(0.0, min(cache.checked_at + hours * 3600 - now, UPDATE_WAKE_SECONDS))
