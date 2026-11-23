"""文档的链接：两份 README、CONTRIBUTING.md、SECURITY.md、CHANGELOG.md 和 docs/ 下中英两套页面。

为什么要测：文档从两份几千行的 README 拆成了 docs/en/ 和 docs/zh-CN/ 下按主题分开的页面，页面之间、页面和代码之间
到处是相对链接和 #锚点。GitHub 不会告诉我们哪个链接断了：点过去只是一个 404，或者停在页面顶上（锚点不存在时不报错）。
改一个标题、挪一节、改一个文件名都可能悄悄弄断别处的链接，所以这里不联网，把每一个相对链接都对一遍：

- 指向的文件（或文件夹）真的在仓库里；
- `#锚点` 在目标文件里真的有：照 GitHub 给标题生成锚点的规矩算（github-slugger：转小写、去掉标点和符号、
  空格换成 -、汉字留着、重复的标题依次加 -1、-2）；
- docs/en/ 和 docs/zh-CN/ 是同样的几页，每一页的第一行链到另一种语言的同一页、也链回 README，README 的"文档"一节列着每一页；
- 每个代码块都关上了（漏一个 ``` 会把后面整页都吞进代码块里）。

外面的链接（http、https、mailto）不查：那要联网，而且不归我们管。
"""

from __future__ import annotations

import posixpath
import re
import unicodedata
from functools import cache
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parent.parent
LANGS = {"en": "README.md", "zh-CN": "README.zh-CN.md"}
INDEX = {"en": "## Documentation", "zh-CN": "## 文档"}  # README 里列出全部页面的那一节


def _documents() -> list[str]:
    names = ["README.md", "README.zh-CN.md", "CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md"]
    names += sorted(path.relative_to(ROOT).as_posix() for path in (ROOT / "docs").rglob("*.md"))
    return names


DOCUMENTS = _documents()
PAGES = {lang: sorted(path.name for path in (ROOT / "docs" / lang).glob("*.md")) for lang in LANGS}

FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
LINK = re.compile(r"\]\((?P<target><[^>]*>|[^)\s]+)(?:\s+\"[^\"]*\")?\)")  # [文字](目标) 和 ![图](目标)
DEFINITION = re.compile(r"^ {0,3}\[[^\]]+\]:\s*(?P<target>\S+)")  # [名字]: 目标
ATTRIBUTE = re.compile(r"\b(?P<name>src|srcset|href)=\"(?P<target>[^\"]+)\"")  # <img src>、<source srcset>、<a href>
EXPLICIT = re.compile(r"<a\s[^>]*\b(?:name|id)=\"([^\"]+)\"")  # 手写的 <a name="…"> 也算锚点
EXTERNAL = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)


def scan(lines: list[str]) -> tuple[list[bool], bool]:
    """每一行是不是正文（不在代码块里），以及最后还有没有没关上的代码块。

    规矩照 CommonMark：``` 后面的说明里不能再有反引号（所以一行开头的 ```` ``` ```` 是行内代码），关闭的那一行不能比开头短。
    """
    prose = []
    fence = ""
    for line in lines:
        match = FENCE.match(line)
        if match:
            run, rest = match.group(1), line[match.end():]
            if not fence and not (run[0] == "`" and "`" in rest):
                fence = run
                prose.append(False)
                continue
            if fence and run[0] == fence[0] and len(run) >= len(fence) and not rest.strip():
                fence = ""
                prose.append(False)
                continue
        prose.append(not fence)
    return prose, bool(fence)


def _without_code_spans(line: str) -> str:
    """去掉行内代码（`…`、``…``）：里面写的 [x](y) 是给人看的字，不是链接。"""
    return re.sub(r"(`+)(?!`).*?(?<!`)\1(?!`)", "", line)


def _plain(text: str) -> str:
    """标题画出来的样子：GitHub 用的是标题的文字，不含 Markdown 的记号。"""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # 链接和图片只留文字
    text = re.sub(r"<[^>]+>", "", text)  # HTML 标签
    text = text.replace("`", "")
    return re.sub(r"(\*\*|\*)(?=\S)(.+?)(?<=\S)\1", r"\2", text)  # **粗体**、*斜体*


def slug(text: str) -> str:
    """GitHub（github-slugger）的做法：转小写，只留字母、数字（各种文字的都算，所以汉字留着）、组合符号、-、_ 和空格，空格换成 -。"""
    kept = (ch for ch in _plain(text).strip().lower() if ch in " -_" or unicodedata.category(ch)[0] in "LMN")
    return "".join(kept).replace(" ", "-")


def anchors_of(lines: list[str]) -> set[str]:
    """一段 Markdown 里所有的锚点：标题按出现的顺序，重复的依次加 -1、-2（照 github-slugger 的 occurrences 写）。"""
    seen: dict[str, int] = {}
    prose, _open = scan(lines)
    for line, is_prose in zip(lines, prose):
        heading = HEADING.match(line) if is_prose else None
        if not heading:
            continue
        base = result = slug(heading.group(2))
        while result in seen:
            seen[base] += 1
            result = f"{base}-{seen[base]}"
        seen[result] = 0
    return set(seen) | {name for line in lines for name in EXPLICIT.findall(line)}


@cache
def _lines(name: str) -> tuple[str, ...]:
    return tuple((ROOT / name).read_text(encoding="utf-8").split("\n"))


@cache
def anchors(name: str) -> frozenset[str]:
    return frozenset(anchors_of(list(_lines(name))))


def targets(line: str) -> list[str]:
    """一行正文里的链接目标，不算行内代码里的。"""
    text = _without_code_spans(line)
    found = [match.group("target").strip("<>") for match in LINK.finditer(text)]
    for match in ATTRIBUTE.finditer(text):
        if match.group("name") == "srcset":  # "a.webp 1x, b.webp 2x"
            found += [part.split()[0] for part in match.group("target").split(",") if part.strip()]
        else:
            found.append(match.group("target"))
    definition = DEFINITION.match(text)
    if definition:
        found.append(definition.group("target"))
    return found


def links(name: str) -> list[tuple[int, str]]:
    """一个文件里的链接（行号，目标），不算代码块里的。"""
    lines = list(_lines(name))
    prose, _open = scan(lines)
    return [(number, target) for number, (line, is_prose) in enumerate(zip(lines, prose), 1) if is_prose for target in targets(line)]


def resolve(name: str, target: str) -> tuple[str, str]:
    """相对链接 → （仓库里的路径，锚点）。只写了锚点的，路径就是这个文件自己。"""
    path, _, anchor = target.partition("#")
    if not path:
        return name, unquote(anchor)
    return posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(path))), unquote(anchor)


def broken(name: str) -> list[str]:
    problems = []
    for number, target in links(name):
        if EXTERNAL.match(target):
            continue
        path, anchor = resolve(name, target)
        where = f"{name}:{number}: {target}"
        if path == ".." or path.startswith("../"):
            problems.append(f"{where} 指到了仓库外面")
        elif not (ROOT / path).exists():
            problems.append(f"{where}：{path} 不存在")
        elif target.partition("#")[0].endswith("/") and not (ROOT / path).is_dir():
            problems.append(f"{where}：写成了文件夹，{path} 却不是文件夹")
        elif anchor and not path.endswith(".md"):
            problems.append(f"{where}：给不是 Markdown 的文件写了锚点")
        elif anchor and anchor not in anchors(path):
            problems.append(f"{where}：{path} 里没有 #{anchor} 这个锚点")
    return problems


def test_the_slug_rules_match_githubs():
    # 照 GitHub 上真实画出来的锚点写的例子：标点去掉、反引号去掉、汉字和全角标点、中间的 · 去掉后剩两个空格、emoji
    assert slug("Troubleshooting: `harness doctor`") == "troubleshooting-harness-doctor"
    assert slug("Undoing a turn's file changes") == "undoing-a-turns-file-changes"
    assert slug("Local models and your own gateway (Ollama, LM Studio, vLLM, llama.cpp)") == (
        "local-models-and-your-own-gateway-ollama-lm-studio-vllm-llamacpp"
    )
    assert slug("读网页：不用浏览器（web_fetch）") == "读网页不用浏览器web_fetch"
    assert slug("在 Windows 上使用") == "在-windows-上使用"
    assert slug("ZhiFox · 智狐") == "zhifox--智狐"
    assert slug("🦊 **No agent framework**") == "-no-agent-framework"


def test_duplicate_headings_get_numbered_anchors():
    page = "# A\n\n## Setup\n\n```\n## Setup\n```\n\n## Setup\n\n## Setup-1\n\n## Setup\n\n<a name=\"old-name\"></a>\n"
    # 代码块里的不算；第二个 Setup 是 setup-1，所以后面真叫 Setup-1 的标题成了 setup-1-1，再下一个 Setup 是 setup-2
    assert anchors_of(page.split("\n")) == {"a", "setup", "setup-1", "setup-1-1", "setup-2", "old-name"}


def test_links_are_found_where_github_sees_them():
    assert targets('[a](x.md) and ![b](img/y.png "title") `[c](not-a-link.md)` <img src="z.webp">') == ["x.md", "img/y.png", "z.webp"]
    assert targets('<source srcset="a.webp 1x, b.webp 2x">') == ["a.webp", "b.webp"]
    assert targets("[1.0.0]: https://example.com/v1") == ["https://example.com/v1"]
    prose, still_open = scan(["```` ``` ```` is code in a line", "```python", "[x](inside.md)", "```", "after"])
    assert prose == [True, False, False, False, True] and not still_open
    assert scan(["```", "never closed"])[1]


@pytest.mark.parametrize("name", DOCUMENTS)
def test_every_relative_link_and_anchor_exists(name):
    assert links(name) or name == "CHANGELOG.md", f"{name} 里一个链接都没找到，多半是读法坏了"
    assert broken(name) == []


@pytest.mark.parametrize("name", DOCUMENTS)
def test_every_code_block_is_closed(name):
    assert not scan(list(_lines(name)))[1], f"{name} 的最后一个代码块没有关上"


def test_both_languages_have_the_same_pages():
    assert PAGES["en"], "docs/en/ 里没有页面"
    assert PAGES["en"] == PAGES["zh-CN"]


@pytest.mark.parametrize("lang", LANGS)
def test_each_page_starts_with_the_language_switch_and_links_back(lang):
    other = next(code for code in LANGS if code != lang)
    for page in PAGES[lang]:
        name = f"docs/{lang}/{page}"
        lines = _lines(name)
        first = {resolve(name, target)[0] for target in targets(lines[0])}
        assert f"docs/{other}/{page}" in first, f"{name} 的第一行应该是换语言的那一行，链到 docs/{other}/{page}"
        assert LANGS[lang] in {resolve(name, target)[0] for _number, target in links(name)}, f"{name} 没有链回 {LANGS[lang]}"
        prose, _open = scan(list(lines))
        titles = [line for line, is_prose in zip(lines, prose) if is_prose and line.startswith("# ")]  # 代码块里的 # 是注释
        assert len(titles) == 1, f"{name} 应该正好有一个一级标题：{titles}"


@pytest.mark.parametrize("lang", LANGS)
def test_the_readme_lists_every_page(lang):
    readme = LANGS[lang]
    lines = list(_lines(readme))
    start = lines.index(INDEX[lang])
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    listed = [resolve(readme, target)[0] for line in lines[start:end] for target in targets(line)]
    pages = [f"docs/{lang}/{page}" for page in PAGES[lang]]
    assert sorted(page for page in listed if page in pages) == pages, f"{readme} 的“{INDEX[lang]}”一节应该把每一页正好列一次"


def test_the_quick_start_anchor_the_release_notes_link_to_is_still_there():
    # tests/release_notes.py 写的发布说明链到 仓库主页#quick-start，也就是 README.md 的这一节
    assert "quick-start" in anchors("README.md")
    assert "快速开始" in anchors("README.zh-CN.md")


def test_the_address_written_into_installed_systemd_units_still_lands_somewhere():
    # 已经装好的 systemd 服务里写着 Documentation=仓库主页#run-it-in-the-background（harness/service/systemd.py）。
    # 那一节搬进了 docs/，README 的"文档"目录里留着一个同名的 <a name>，别人电脑上的旧地址照样跳到那一页的链接上
    from harness.install import REPOSITORY
    from harness.service import systemd

    address, _, anchor = systemd.DOCUMENTATION.partition("#")
    assert address == REPOSITORY
    assert anchor in anchors("README.md")
