"""发布时用：从 CHANGELOG.md 里取出这一版的那一节，前面加上怎么装这一版，就是 GitHub Release 上的说明。

    uv run python tests/release_notes.py v0.2.0 > notes.md

.github/workflows/release.yml 在推了 v0.2.0 这样的 tag 以后运行它。下面任何一样对不上就失败（退出码 1、写明哪一样），
这一版就不打包、不发布，免得发出去一个说明和内容对不上的版本：
  - tag 就是 v 加上 pyproject.toml 里的版本号（版本号只写在 pyproject.toml，见 harness/version.py）；
  - CHANGELOG.md 里有 `## [0.2.0] - 2026-11-22` 这样的一节：写着发布的日期，下面不是空的；
  - CHANGELOG.md 末尾有这一版的链接 `[0.2.0]: https://github.com/…/releases/tag/v0.2.0`。
tests/test_release.py 拿仓库里现在的版本号检查同样几样，所以改了版本号、忘了写 CHANGELOG，平常的测试就会失败，不用等到发布那一刻。

说明写给 GitHub 上的人看：英文为主，附一句中文（CLAUDE.md 第 1 条）。
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

from harness.install import PROJECT_NAME, REPOSITORY

ROOT = Path(__file__).resolve().parent.parent
_HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\](?: - (?P<date>\d{4}-\d{2}-\d{2}))?\s*$")
_LINK = re.compile(r"^\[(?P<name>[^\]]+)\]: (?P<url>\S+)\s*$")


class NotReady(Exception):
    """这一版还不能发布，原因写在消息里（英文：CI 的日志是给所有人看的）。"""


def project_version(root: Path = ROOT) -> str:
    """pyproject.toml 里的版本号。"""
    with open(root / "pyproject.toml", "rb") as file:
        return str(tomllib.load(file)["project"]["version"])


def wheel_name(version: str) -> str:
    """hatchling 打出来的 wheel 的文件名：包名里的 - 换成 _（PEP 427），纯 Python、哪个系统都能用。"""
    return f"{PROJECT_NAME.replace('-', '_')}-{version}-py3-none-any.whl"


def section(changelog: str, version: str) -> tuple[str, str]:
    """CHANGELOG 里这一版的（日期，正文）。没有这一节时日期和正文都是空字符串；有这一节、没写日期时日期是空的。"""
    lines = changelog.splitlines()
    for start, line in enumerate(lines):
        found = _HEADING.match(line)
        if found and found["version"] == version:
            body: list[str] = []
            for rest in lines[start + 1 :]:
                if _HEADING.match(rest) or _LINK.match(rest):  # 下一版的一节，或者文件末尾的链接
                    break
                body.append(rest)
            return found["date"] or "", "\n".join(body).strip()
    return "", ""


def links(changelog: str) -> dict[str, str]:
    """CHANGELOG 末尾的链接：[0.2.0] → 网址。"""
    return {found["name"]: found["url"] for line in changelog.splitlines() if (found := _LINK.match(line))}


def unwrap(markdown: str) -> str:
    """把 CHANGELOG 里折成几行的一条接回一行。

    CHANGELOG.md 按 120 个字符折行，在仓库里看没问题（Markdown 文件里单个换行只算空格）；GitHub Release 的说明却把单个换行
    画成真的换行，一句话会断在中间。所以缩进的续行（不是新的一条、不是代码块）接到上一行后面，别的原样不动。"""
    lines: list[str] = []
    fenced = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        continued = line.startswith("  ") and not line.lstrip().startswith(("- ", "* ", "```")) and not fenced
        if continued and lines and lines[-1].strip():
            lines[-1] += " " + line.strip()
        else:
            lines.append(line)
    return "\n".join(lines)


def notes(tag: str, root: Path = ROOT) -> str:
    """这一版在 GitHub Release 上的说明。哪一样对不上就抛 NotReady。"""
    version = project_version(root)
    if tag != f"v{version}":
        raise NotReady(f"the tag {tag} doesn't match the version in pyproject.toml ({version}); the tag should be v{version}")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    date, body = section(changelog, version)
    if not body:
        raise NotReady(f"CHANGELOG.md has no section for {version} (a heading like '## [{version}] - YYYY-MM-DD' with the changes under it)")
    if not date:
        raise NotReady(f"the {version} section in CHANGELOG.md has no release date: write the heading as '## [{version}] - YYYY-MM-DD'")
    expected = f"{REPOSITORY}/releases/tag/{tag}"
    if links(changelog).get(version) != expected:
        raise NotReady(f"CHANGELOG.md should end with the link '[{version}]: {expected}'")
    wheel = f"{REPOSITORY}/releases/download/{tag}/{wheel_name(version)}"
    return "\n".join(
        [
            f"**ZhiFox (智狐) {version}**, released {date}.",
            "",
            "## Install",
            "",
            "With [uv](https://docs.astral.sh/uv/getting-started/installation/), on macOS, Linux or Windows (no Git needed):",
            "",
            "```",
            f"uv tool install {wheel}",
            "harness --web",
            "```",
            "",
            (
                f"With Git you can also install from the tag: `uv tool install git+{REPOSITORY}@{tag}`. "
                "If ZhiFox is already installed, add `--force` to replace it; your data folder and keys stay where they are. "
                f"`harness --version` shows which version you have. Everything else is in the [README]({REPOSITORY}#quick-start)."
            ),
            "",
            (
                f"中文：安装方法同上（Windows 用 PowerShell 也是这两行）。完整的中文说明见 [README.zh-CN.md]({REPOSITORY}/blob/{tag}/README.zh-CN.md)，"
                "这一版改了什么见下面（英文）。"
            ),
            "",
            "## What's in this release",
            "",
            unwrap(body),
            "",
            f"See also [CHANGELOG.md]({REPOSITORY}/blob/{tag}/CHANGELOG.md).",
            "",
        ]
    )


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: release_notes.py vX.Y.Z", file=sys.stderr)
        return 2
    try:
        text = notes(argv[0])
    except NotReady as problem:
        print(f"Not releasing: {problem}", file=sys.stderr)
        return 1
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
