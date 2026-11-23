"""发布：tests/release_notes.py 的几样检查、.github/workflows/release.yml 的写法、harness --version。

最要紧的是第一个测试：pyproject.toml 里现在的版本号，CHANGELOG.md 里必须有写着日期的一节和它的链接。
所以只在发布的时候才改版本号（uv version 0.3.0），同时把 CHANGELOG 的 Unreleased 改成那一版；忘了哪一样，平常的测试就会失败，
不用等到推了 tag、发布的工作流停下来才发现。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import release_notes

from harness import cli, version
from harness.config import APP_NAME
from harness.install import REPOSITORY

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"

CHANGELOG = """# Changelog

## [Unreleased]

## [0.3.0] - 2027-01-05

### Added

- Something new.

## [0.2.0] - 2026-11-22

### Added

- The first release.

[Unreleased]: https://github.com/Akxan/zhifox-agent-harness/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/Akxan/zhifox-agent-harness/releases/tag/v0.3.0
[0.2.0]: https://github.com/Akxan/zhifox-agent-harness/releases/tag/v0.2.0
"""


def _project(tmp_path: Path, version: str, changelog: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "harness-assistant"\nversion = "{version}"\n', encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    return tmp_path


def test_the_version_in_pyproject_is_ready_to_release():
    current = release_notes.project_version()
    text = release_notes.notes(f"v{current}")  # 有哪一样对不上就抛 NotReady
    assert f"{REPOSITORY}/releases/download/v{current}/{release_notes.wheel_name(current)}" in text
    assert f"git+{REPOSITORY}@v{current}" in text
    assert "README.zh-CN.md" in text  # 英文为主，附一句中文
    assert version.version() == current  # 程序报的版本号和 pyproject.toml 是同一个


def test_a_section_stops_at_the_next_version_and_at_the_links():
    assert release_notes.section(CHANGELOG, "0.3.0") == ("2027-01-05", "### Added\n\n- Something new.")
    assert release_notes.section(CHANGELOG, "0.2.0") == ("2026-11-22", "### Added\n\n- The first release.")
    assert release_notes.section(CHANGELOG, "0.4.0") == ("", "")
    assert release_notes.links(CHANGELOG)["0.2.0"] == f"{REPOSITORY}/releases/tag/v0.2.0"


def test_notes_carry_this_versions_changes_and_install_lines(tmp_path):
    text = release_notes.notes("v0.3.0", _project(tmp_path, "0.3.0", CHANGELOG))
    assert "**ZhiFox (智狐) 0.3.0**, released 2027-01-05." in text
    assert "- Something new." in text and "The first release" not in text
    assert f"uv tool install {REPOSITORY}/releases/download/v0.3.0/harness_assistant-0.3.0-py3-none-any.whl" in text
    assert f"{REPOSITORY}/blob/v0.3.0/CHANGELOG.md" in text


@pytest.mark.parametrize(
    ("tag", "changelog", "reason"),
    [
        ("v0.2.0", CHANGELOG, "doesn't match the version in pyproject.toml"),  # tag 和版本号不一样
        ("0.3.0", CHANGELOG, "doesn't match"),  # 少了 v
        ("v0.3.0", CHANGELOG.replace("## [0.3.0] - 2027-01-05", "## [0.3.0]"), "has no release date"),
        ("v0.3.0", CHANGELOG.replace("- Something new.", "").replace("### Added\n\n\n", ""), "has no section"),
        ("v0.3.0", CHANGELOG.replace("## [0.3.0] - 2027-01-05", "## [Next]"), "has no section"),
        ("v0.3.0", CHANGELOG.replace("[0.3.0]: https://github.com/Akxan/zhifox-agent-harness/releases/tag/v0.3.0\n", ""), "should end with the link"),
        ("v0.3.0", CHANGELOG.replace("releases/tag/v0.3.0", "commits/main"), "should end with the link"),
    ],
    ids=["other-version", "no-v", "no-date", "empty-section", "no-section", "no-link", "wrong-link"],
)
def test_what_isnt_ready_is_refused(tmp_path, tag, changelog, reason):
    with pytest.raises(release_notes.NotReady, match=reason):
        release_notes.notes(tag, _project(tmp_path, "0.3.0", changelog))


def test_wrapped_changelog_lines_become_one_line_each():
    # GitHub Release 的说明把单个换行画成真的换行；CHANGELOG 里折开的一条要接回去，新的一条、标题、代码块不动
    text = "#### Web UI\n\n- First item that is\n  wrapped twice\n  here.\n- Second item.\n  ```\n  keep  this\n  ```\n"
    assert release_notes.unwrap(text) == "#### Web UI\n\n- First item that is wrapped twice here.\n- Second item.\n  ```\n  keep  this\n  ```"


def test_the_command_line_says_why_and_exits_1(capsys):
    assert release_notes.main(["v9.9.9"]) == 1
    assert "Not releasing: the tag v9.9.9" in capsys.readouterr().err
    assert release_notes.main([]) == 2


def test_the_wheel_name_follows_the_package_name():
    assert release_notes.wheel_name("1.2.3") == "harness_assistant-1.2.3-py3-none-any.whl"


def test_the_release_workflow_runs_the_checks_and_pins_its_actions():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert 'tests/release_notes.py "$GITHUB_REF_NAME"' in text
    assert text.count('tests/smoke_release.py "$version"') == 3  # wheel 两个 Python 版本，加上源码包
    assert "uv build --no-sources" in text
    assert "permissions: {}" in text  # 默认什么都不给
    # 能拿到 PyPI 凭据的只有 pypi 那一项，而且它不签出代码、不运行仓库里的东西
    assert len(re.findall(r"^\s*id-token: write", text, re.MULTILINE)) == 1  # 只数真正的设置，不数开头注释里提到的
    pypi = text[text.index("\n  pypi:") :]
    assert "actions/checkout" not in pypi and "uv run" not in pypi
    assert "if: vars.PYPI_PUBLISH == 'true'" in pypi
    # 每一个 action 都钉在 40 位的提交编号上，后面写着是哪一版
    uses = re.findall(r"uses: (\S+)(.*)", text)
    assert uses
    for action, comment in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", action), action
        assert re.search(r"# v\d+\.\d+\.\d+", comment), action


def test_the_ordinary_ci_builds_and_tries_the_package_on_three_systems():
    # 平常每次推送也打包、试装（tests.yml 的 package），装不上的问题不用等到发布时才发现
    text = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    package = text[text.index("\n  package:") :]
    assert "os: [ubuntu-latest, macos-latest, windows-latest]" in package
    assert "uv build --no-sources" in package
    assert package.count("tests/smoke_release.py") == 2  # wheel 和源码包


def test_version_option_prints_the_name_and_the_version(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--version"])
    assert stopped.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith(f"{APP_NAME} {version.version()}")


def test_an_installed_copy_shows_just_the_version(monkeypatch):
    monkeypatch.setattr(version, "FROM_SOURCE", False)
    monkeypatch.setattr(version, "commit", lambda: "abc1234")  # 装成工具时包旁边没有 .git；就算有也不看
    assert version.shown() == f"{APP_NAME} {version.version()}"


def test_a_clone_shows_the_commit_too(monkeypatch):
    monkeypatch.setattr(version, "FROM_SOURCE", True)
    monkeypatch.setattr(version, "commit", lambda: "abc1234")
    assert version.shown() == f"{APP_NAME} {version.version()} · 提交 abc1234"
    monkeypatch.setattr(version, "commit", lambda: "")  # 下载的 ZIP：不是 git 仓库
    assert version.shown() == f"{APP_NAME} {version.version()}"
