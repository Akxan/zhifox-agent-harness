"""从网址装 Skill：认出网址是哪一种，把文件下载下来，交给 bundle.py 检查。

认得这几种网址：

  GitHub 上的文件夹或整个仓库   https://github.com/anthropics/skills/tree/main/skills/pdf
                                 https://github.com/anthropics/skills
  GitHub 上的一个 SKILL.md       https://github.com/…/blob/main/skills/pdf/SKILL.md
                                 https://raw.githubusercontent.com/…/main/skills/pdf/SKILL.md
                                 （装的是它所在的整个文件夹：SKILL.md 里常常会提到同一个文件夹里的脚本）
  别的网址                       下载下来是 zip（包括 .skill）就当压缩包；是以 --- 开头的文字就当一个 SKILL.md

GitHub 的文件夹不是一个能直接下载的文件。这里下载的是整个仓库在那个分支上的压缩包
（codeload.github.com 提供，一个请求就够，不占 GitHub API 每小时 60 次的额度），再从里面取出那个文件夹。

下载本身也要小心，因为网址是从外面来的：

  - 只用 https，跳转也只跟到 https 的地址：明文的 http 在路上可能被人换掉内容
  - 有超时：对方一直不回话、或者一个字节一个字节地慢慢发，都不能把这里拖住
  - 有大小上限：先看对方说的大小，再边收边数，超过了马上停
"""

from __future__ import annotations

import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

from ..i18n import t
from .bundle import Files, from_zip
from .store import SKILL_FILE, SkillError

MAX_DOWNLOAD_BYTES = 20_000_000  # 下载的东西（压缩包或文件）最大这么大
TIMEOUT_SECONDS = 20  # 一次读写最多等这么久
DEADLINE_SECONDS = 90  # 整个下载最多花这么久
MAX_REDIRECTS = 5
USER_AGENT = "zhifox-agent-harness (skill installer)"
# 测试里换成一个信任本地自签名证书的 SSLContext。平时是 None：用系统信任的证书
SSL_CONTEXT: ssl.SSLContext | None = None


@dataclass
class Fetched:
    files: Files
    label: str  # 给人看的来源，比如 github.com/anthropics/skills/skills/pdf
    commit: str = ""  # GitHub 压缩包对应的提交（装的是哪个版本）


class NotFound(SkillError):
    """网址那头回答 404：没有这个东西（或者是没有权限看的私有仓库）。"""


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    """跟着跳转走，但只跟到 https 的地址，最多跳几次。"""

    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise SkillError(t("下载时被跳转到了一个不是 https 的地址，为了安全已经停止：{url}", url=newurl[:200]))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str) -> tuple[bytes, str]:
    """下载一个网址的内容。返回（内容，跳转之后最终的地址）。失败抛出 SkillError，说明写给用户看。"""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise SkillError(t("只能从 https:// 开头的网址下载。"))
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=SSL_CONTEXT), _HttpsOnly())
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    started = time.monotonic()
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > MAX_DOWNLOAD_BYTES:
                raise _too_big()
            chunks, received = [], 0
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                received += len(chunk)
                if received > MAX_DOWNLOAD_BYTES:
                    raise _too_big()
                if time.monotonic() - started > DEADLINE_SECONDS:
                    raise SkillError(t("下载太慢了，超过 {n} 秒还没有完成，已经停止。", n=DEADLINE_SECONDS))
                chunks.append(chunk)
            return b"".join(chunks), response.geturl()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise NotFound(t("这个网址找不到（404）。请检查网址；如果是 GitHub 上的私有仓库，智狐没有权限下载。")) from None
        raise SkillError(t("下载失败：服务器回答 {code}。", code=exc.code)) from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, SkillError):
            raise reason from None
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise SkillError(t("连接 {host} 超时了。请检查网络后再试。", host=parts.hostname)) from None
        if isinstance(reason, ssl.SSLError):
            raise SkillError(t("和 {host} 的加密连接没有建立起来（证书不对）。为了安全，没有下载。", host=parts.hostname)) from None
        raise SkillError(t("连不上 {host}：{error}", host=parts.hostname, error=reason)) from None
    except TimeoutError:
        raise SkillError(t("连接 {host} 超时了。请检查网络后再试。", host=parts.hostname)) from None
    except (OSError, ValueError) as exc:
        raise SkillError(t("下载失败：{error}", error=exc)) from None


def _too_big() -> SkillError:
    return SkillError(t("要下载的东西太大了：最多 {n} MB。可以先下载到电脑上，挑出需要的那个 Skill 再上传。", n=MAX_DOWNLOAD_BYTES // 1_000_000))


# ---- 认出网址 --------------------------------------------------------------


@dataclass
class GitHub:
    owner: str
    repo: str
    rest: list[str]  # 分支（可能带 /）加上路径：一开始分不清两者的分界在哪，下载时一个个试
    kind: str  # "repo" 整个仓库、"tree" 文件夹、"blob" 一个文件


def parse_github(url: str) -> GitHub | None:
    """GitHub 的网址拆成几部分。不是 GitHub 的仓库网址返回 None。"""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    segments = [unquote(piece) for piece in parts.path.split("/") if piece]
    if host in ("github.com", "www.github.com") and len(segments) >= 2:
        owner, repo = segments[0], segments[1].removesuffix(".git")
        if len(segments) == 2:
            return GitHub(owner, repo, [], "repo")
        if segments[2] in ("tree", "blob") and len(segments) >= 4:
            return GitHub(owner, repo, segments[3:], segments[2])
        return None
    if host == "raw.githubusercontent.com" and len(segments) >= 4:
        return GitHub(segments[0], segments[1], segments[2:], "blob")
    return None


def from_url(url: str) -> Fetched:
    """按网址下载 Skill 的文件。"""
    url = url.strip()
    if "://" not in url:
        url = "https://" + url  # 只写了 github.com/… 也认
    if urlsplit(url).scheme != "https":
        raise SkillError(t("只能从 https:// 开头的网址下载。"))
    github = parse_github(url)
    if github is not None:
        return _from_github(github)
    data, final = fetch(url)
    label = (urlsplit(final).hostname or "") + urlsplit(final).path
    return Fetched(files=_guess(data), label=label[:300])


def _guess(data: bytes) -> Files:
    """下载下来的是什么：zip 压缩包，或者一个 SKILL.md。"""
    if data.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        files, _ = from_zip(data)
        return files
    text = data.removeprefix(b"\xef\xbb\xbf")
    if text.lstrip(b" \t\r\n").startswith(b"---"):
        return {SKILL_FILE: (text, False)}
    raise SkillError(t("这个网址下载下来的既不是 SKILL.md，也不是 zip 压缩包。请换成 Skill 文件夹的链接、SKILL.md 的链接，或者 .zip / .skill 文件的链接。"))


def _from_github(github: GitHub) -> Fetched:
    owner, repo = github.owner, github.repo
    base = f"https://codeload.github.com/{quote(owner)}/{quote(repo)}/zip/"
    # 分支名可能带 /（比如 feature/pdf），和后面的路径分不清。从最短的试起：main、main/skills、……
    tries = [(["HEAD"], [])] if not github.rest else [(github.rest[:cut], github.rest[cut:]) for cut in range(1, min(len(github.rest), 4) + 1)]
    if github.kind == "blob" and github.rest[-1] != SKILL_FILE:
        raise SkillError(t("这个链接指向的是一个文件，不是 Skill。请换成 Skill 文件夹的链接，或者其中 SKILL.md 的链接。"))
    for number, (ref, path) in enumerate(tries, start=1):
        if github.kind == "blob":
            if not path:
                continue  # 全都当成了分支名，没有剩下文件名：这种分法不对
            path = path[:-1]  # SKILL.md 的链接：装它所在的文件夹
        try:
            data, _ = fetch(base + quote("/".join(ref), safe="/"))
        except NotFound:
            if number < len(tries):
                continue  # 没有这个分支：换一种分法再试
            raise
        scope = "/".join(path)
        files, comment = from_zip(data, scope=scope, strip_top=True)
        if not files:
            raise SkillError(t("仓库里没有找到 {path} 这个文件夹。", path=scope or "/"))
        label = f"github.com/{owner}/{repo}" + (f"/{scope}" if scope else "")
        commit = comment.strip() if len(comment.strip()) == 40 and all(char in "0123456789abcdef" for char in comment.strip()) else ""
        return Fetched(files=files, label=label, commit=commit)
    raise SkillError(t("这个网址找不到（404）。请检查网址；如果是 GitHub 上的私有仓库，智狐没有权限下载。"))
