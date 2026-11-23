"""给外人看的几个文件：CONTRIBUTING.md、.github/ISSUE_TEMPLATE/ 里的表单和设置、.github/PULL_REQUEST_TEMPLATE.md。

为什么要测：issue 表单写错了，GitHub 不会告诉我们，只是"New issue"那一页上不出现这张表单（或者显示一行出错的提示），
要等到有人来报问题时才发现。所以这里照 GitHub 文档写明的键查一遍（文档的链接写在每个 .yml 的开头）：顶层要有哪几个键、
每种元素能写哪些 attributes 和 validations、id 的写法、下拉框的选项不能重复。

YAML 用仓库自己的 harness/imports/yaml_lite.py 读：不为了测试加 pyyaml 这个依赖。它不认的写法（锚点、别名、标签……）
表单里本来也用不上；写进去了，这里会先失败。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from harness.imports import yaml_lite

ROOT = Path(__file__).resolve().parent.parent
GITHUB = ROOT / ".github"
TEMPLATES = GITHUB / "ISSUE_TEMPLATE"
FORMS = ["bug_report.yml", "feature_request.yml"]

# 照 GitHub 的文档："Syntax for issue forms" 的顶层键（name、description、body 必须有），
# "Syntax for GitHub's form schema" 里每种元素的 attributes（必须有的、能写的）和 validations
TOP_REQUIRED = {"name", "description", "body"}
TOP_ALLOWED = TOP_REQUIRED | {"assignees", "labels", "title", "type", "projects"}
ELEMENT_KEYS = {"type", "id", "attributes", "validations"}
ELEMENTS: dict[str, tuple[set[str], set[str], set[str]]] = {
    # 种类: (必须有的 attributes, 能写的 attributes, 能写的 validations)
    "markdown": ({"value"}, {"value"}, set()),
    "input": ({"label"}, {"label", "description", "placeholder", "value"}, {"required", "min_length"}),
    "textarea": ({"label"}, {"label", "description", "placeholder", "value", "render"}, {"required", "min_length"}),
    "dropdown": ({"label", "options"}, {"label", "description", "multiple", "options", "default"}, {"required"}),
    "checkboxes": ({"label", "options"}, {"label", "description", "options"}, {"required"}),
    "upload": ({"label"}, {"label", "description"}, {"required", "accept"}),
}
ID = re.compile(r"[A-Za-z0-9_-]+")  # 文档："只能用字母、数字、- 和 _，在一张表单里不能重复"


def _load(name: str) -> dict:
    data = yaml_lite.loads((TEMPLATES / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict), name
    return data


def _fields(form: dict) -> dict[str, dict]:
    return {element["id"]: element for element in form["body"] if "id" in element}


def test_the_files_are_where_github_looks_for_them():
    for path in [ROOT / "CONTRIBUTING.md", GITHUB / "PULL_REQUEST_TEMPLATE.md", TEMPLATES / "config.yml"]:
        assert path.is_file(), path
    # 表单的扩展名必须是 .yml（文档："Issue templates created with issue forms need a .yml extension"）
    assert sorted(path.name for path in TEMPLATES.iterdir()) == sorted([*FORMS, "config.yml"])


@pytest.mark.parametrize("name", FORMS)
def test_issue_forms_use_only_documented_keys(name):
    form = _load(name)
    assert TOP_REQUIRED <= form.keys() <= TOP_ALLOWED, name
    assert isinstance(form["name"], str) and form["name"].strip()
    assert isinstance(form["description"], str) and form["description"].strip()
    assert isinstance(form["body"], list) and form["body"]
    ids: list[str] = []
    for element in form["body"]:
        assert isinstance(element, dict) and element.keys() <= ELEMENT_KEYS, element
        kind = element["type"]
        required, allowed, validations = ELEMENTS[kind]
        attributes = element["attributes"]
        assert required <= attributes.keys() <= allowed, (kind, attributes.keys())
        assert element.get("validations", {}).keys() <= validations, element
        if kind == "markdown":
            assert "id" not in element  # markdown 不提交，用不上 id
        else:
            assert ID.fullmatch(element.get("id", "")), element  # 每一项都给 id：链接里预填、以后对照回答都靠它
            ids.append(element["id"])
        if kind == "dropdown":
            options = attributes["options"]
            assert options and len(set(options)) == len(options), options  # 不能是空的，也不能重复
            assert all(isinstance(option, str) for option in options)
            if "default" in attributes:  # 有默认选项时不能有 "None"、"n/a"
                assert not {"None", "n/a"} & set(options)
        if kind == "checkboxes":
            for option in attributes["options"]:
                assert isinstance(option.get("label"), str) and option.keys() <= {"label", "required"}, option
    assert len(ids) == len(set(ids)), ids
    assert any(element["type"] != "markdown" for element in form["body"])


def test_forms_have_different_names():
    # 文档："Must be unique from all other templates"
    names = [_load(name)["name"] for name in FORMS]
    assert len(set(names)) == len(names)


def test_the_bug_report_asks_for_what_we_need_to_reproduce():
    fields = _fields(_load("bug_report.yml"))
    assert {"what-happened", "expected", "steps", "version", "install", "os", "provider", "doctor", "logs"} <= fields.keys()
    assert fields["doctor"]["attributes"]["render"] == "json"
    assert "paths" in fields["doctor"]["attributes"]["description"]  # 提醒先看一遍：没有密钥，但有路径
    assert {"macOS", "Windows", "Linux"} <= set(fields["os"]["attributes"]["options"])
    # 安全问题不该写在公开的 issue 里：这一项必须勾上才能提交
    security = fields["checks"]["attributes"]["options"][0]
    assert "security" in security["label"] and "SECURITY.md" in security["label"] and security["required"] is True


def test_the_feature_request_asks_which_part():
    fields = _fields(_load("feature_request.yml"))
    assert {"problem", "solution", "alternatives", "area"} <= fields.keys()
    assert len(fields["area"]["attributes"]["options"]) >= 5


def test_the_template_chooser_has_no_blank_issue_and_points_to_security():
    config = _load("config.yml")
    assert config.keys() <= {"blank_issues_enabled", "contact_links"}
    assert config["blank_issues_enabled"] is False
    links = config["contact_links"]
    for link in links:
        assert link.keys() == {"name", "url", "about"}, link
        assert link["url"].startswith("https://github.com/Akxan/zhifox-agent-harness")
    assert any("SECURITY.md" in link["url"] for link in links)
    assert any(link["url"].endswith("#readme") for link in links)
    # SECURITY.md 里真有链接指向的那一节
    anchor = next(link["url"] for link in links if "SECURITY.md" in link["url"]).partition("#")[2]
    assert f"## {anchor.replace('-', ' ')}" in (ROOT / "SECURITY.md").read_text(encoding="utf-8").lower()


def test_contributing_has_the_english_and_the_chinese_parts():
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "**English** | [简体中文](#简体中文)" in text
    english, marker, chinese = text.partition("\n## 简体中文\n")
    assert marker, "缺了 ## 简体中文 那一节"
    # 两部分讲的是同一件事：几条要紧的命令和链接两边都有
    for must in ["uv sync", "uv run pytest -q", "-n 0", "pytest -m ui", "tests/demo_web.py", "uvx ruff", "mypy==1.19.0",
                 "install.ps1", "en.json", "es.json", "CLAUDE.md", "SECURITY.md", "LICENSE", "bug_report.yml"]:
        assert must in english, must
        assert must in chinese, must


def test_the_pull_request_template_has_the_checklist():
    text = (GITHUB / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    checklist = [line for line in text.splitlines() if line.startswith("- [ ] ")]
    assert len(checklist) >= 7
    for must in ["uv run pytest -q", "ruff", "mypy", "README.zh-CN.md", "en.json", "es.json", "Windows", "data/"]:
        assert any(must in line for line in checklist), must


def test_both_readmes_point_to_contributing():
    assert "## Contributing" in (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## 参与贡献" in (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    for readme in ["README.md", "README.zh-CN.md"]:
        assert "(CONTRIBUTING.md)" in (ROOT / readme).read_text(encoding="utf-8"), readme
