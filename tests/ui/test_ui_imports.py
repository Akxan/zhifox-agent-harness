"""从其他工具导入：Skill、MCP、Agent 三页的"从其他工具导入"。

演示（tests/demo_web.py）把家目录换成了临时目录，里面放了几种工具的编造配置：Claude Desktop、Claude Code、Cursor、
VS Code（带注释的 JSON）、Codex（TOML）、Goose（YAML）、一个故意写坏了的 Kiro 配置，还有 Codex、Gemini CLI、
.agents/skills 里的 Skill，GitHub Copilot、Gemini CLI、Codex 的 Agent。这里像人一样点：顶上的总览列出找到的工具和
每样各几个、点数字换页或者滚到那一组、写坏了的文件说清楚、引用模型密钥的勾不上、密钥不出现在页面上、
"没找到的"收着但能点开、勾一个导入进来。导入的东西测试结束时删掉：演示服务器是整个测试会话共用的，
别的测试还要数 Skill、MCP、Agent 有几个。
"""

from __future__ import annotations

import json

from . import steps

# 演示里编造的几个密钥：页面上（包括 HTML 的属性里）一个都不能出现
SECRETS = ("fake-github-token-for-the-demo-0001", "fake-sentry-token-for-the-demo-0002", "demo-secret-token-1234abcd")


def _open_mcp_import(page) -> None:
    steps.extensions_tab(page, "mcp")
    page.click("#panel-mcp [data-focus='import']")
    page.wait("document.querySelector('#mcp-import-result .import-overview')", what="MCP 的导入页画好")
    steps.idle(page)


def _pick(name: str) -> str:
    """MCP 导入清单里名叫 name 的那一项（JavaScript 表达式）。"""
    return f"[...document.querySelectorAll('#panel-mcp .mcp-pick')].find((row) => row.querySelector('b').textContent === {json.dumps(name)})"


def test_mcp_import_lists_every_tool_and_imports_only_the_picked(page):
    _open_mcp_import(page)
    found = set(page.js("[...document.querySelectorAll('#panel-mcp .import-tool')].map((row) => row.dataset.tool)"))
    assert {"claude-code", "claude-desktop", "cursor", "vscode", "codex", "goose", "kiro"} <= found
    # 每个工具后面写着三样各找到几个：MCP 这一页的那一样是当前的，另外两样点了换页
    codex = "#panel-mcp .import-tool[data-tool='codex']"
    assert page.js(f"document.querySelector(\"{codex} .import-chip.here\").dataset.kind") == "mcp"
    assert page.js(f"[...document.querySelectorAll(\"{codex} .import-chip:not(.here)\")].map((chip) => chip.dataset.kind)") == ["skills", "agents"]
    # 写坏了的 Kiro 配置：总览里标出来，那一组里写明哪一行不对，别的工具照常列出来
    assert page.js("!!document.querySelector(\"#panel-mcp .import-tool[data-tool='kiro'] .import-chip.bad\")")
    kiro = page.js("document.querySelector(\"#panel-mcp .import-group[data-tool='kiro']\").parentElement.querySelector('.mcp-note.bad').textContent")
    assert "JSON" in kiro
    # 换了写法的说清楚：Cursor 的 ${env:X}、"不用问"的设置没有搬过来；VS Code 的 ${input:…} 要你自己填
    linear = _pick("linear")
    notes = page.js(f"[...{linear}.querySelectorAll('.import-notes li')].map((li) => li.textContent)")
    assert any("LINEAR_TOKEN" in note for note in notes) and any("autoApprove" in note for note in notes)
    assert page.js(f"{_pick('postgres')}.textContent.includes('${{input:pg-password}}')")
    # 引用了智狐自己的模型密钥的：列出来，但勾不上
    assert page.js(f"{_pick('steal-key')}.querySelector('input').disabled")
    assert "ANTHROPIC_API_KEY" in page.js(f"{_pick('steal-key')}.querySelector('.bad').textContent")
    html = page.html()
    assert not [secret for secret in SECRETS if secret in html], "密钥出现在页面上了"

    # "没找到的"：收着；点开能看到智狐认得、但这里没有的工具，去过的地方，官方文档的链接在新窗口打开
    assert page.js("document.querySelector('#panel-mcp details.import-missing').open") is False
    page.click("#panel-mcp details.import-missing summary")
    page.wait("document.querySelector('#panel-mcp details.import-missing').open")
    missing = page.js("[...document.querySelectorAll('#panel-mcp .import-missing-name')].map((link) => link.textContent)")
    assert "Zed" in missing and "Continue" in missing and "Cursor" not in missing
    assert page.js("[...document.querySelectorAll('#panel-mcp .import-missing-name')].every((link) => link.target === '_blank' && link.rel.includes('noopener') && link.href.startsWith('https://'))")
    assert page.js("[...document.querySelectorAll('#panel-mcp .import-missing-where .path')].some((path) => path.textContent === '.zed/settings.json')")

    # 只勾 Cursor 的 linear，导入：回到列表，它在里面
    page.js("document.querySelectorAll('#panel-mcp .mcp-picks input:checked').forEach((box) => box.click())")
    page.wait("document.getElementById('mcp-import-add').disabled", what="全都不勾以后不能导入")
    try:
        page.click("#panel-mcp .mcp-pick-group:has(.import-group[data-tool='cursor']) input[type='checkbox']")
        page.wait("!document.getElementById('mcp-import-add').disabled")
        page.click("#mcp-import-add")
        page.wait("document.querySelector(\"#panel-mcp li.mcp-server[data-name='linear']\")", what="导入的服务器出现在列表里")
        assert not page.exists("#panel-mcp li.mcp-server[data-name='steal-key']")
    finally:
        page.demo.api("/api/mcp/delete", {"name": "linear"})


def test_overview_chips_switch_pages_and_a_skill_imports(page):
    _open_mcp_import(page)
    # 在 MCP 这一页点 Codex 的"1 个 Skill"：换到 Skill 那一页的导入，滚到 Codex 那一组
    page.click("#panel-mcp .import-tool[data-tool='codex'] .import-chip[data-kind='skills']")
    page.wait("!document.getElementById('panel-skills').hidden && document.querySelector(\"#panel-skills .import-group[data-tool='codex']\")", what="换到 Skill 的导入页")
    assert page.js("document.getElementById('tab-skills').getAttribute('aria-selected')") == "true"
    page.wait("document.activeElement.matches(\"#panel-skills .import-group[data-tool='codex']\")", what="焦点停在 Codex 那一组的标题上")
    # 当前这一样的数字：点了滚到那一组（这里是很多工具共用的 .agents/skills）
    page.click("#panel-skills .import-tool[data-tool='agents'] .import-chip.here")
    page.wait("document.activeElement.matches(\"#panel-skills .import-group[data-tool='agents']\")")
    assert page.text("#panel-skills .import-group[data-tool='agents'] .path") == "~/.agents/skills"
    # 勾上 Codex 的 release-notes，装上：列表里写着它是从 Codex 导入的
    try:
        page.click("#panel-skills input[data-key='pick:~/.codex/skills/release-notes']")
        page.click("#panel-skills [data-key='install']")
        page.wait("document.querySelector(\"#panel-skills li.skill-row[data-name='release-notes']\")", what="装好的 Skill 出现在列表里")
        assert "Codex" in page.text("#panel-skills li.skill-row[data-name='release-notes']")
    finally:
        page.demo.api("/api/skill/delete", {"name": "release-notes"})


def test_agents_from_other_tools_say_what_was_changed(page):
    steps.extensions_tab(page, "agents")
    page.click("#panel-agents [data-key='bar:import']")
    page.wait("document.querySelector(\"#panel-agents .import-group[data-tool='copilot']\")", what="Agent 的导入页画好")
    steps.idle(page)
    groups = page.js("[...document.querySelectorAll('#panel-agents .import-group')].map((head) => head.dataset.tool)")
    assert {"claude-code", "copilot", "gemini", "codex"} <= set(groups)
    card = "#panel-agents .agents-cand:has(input[data-key='pick:.github/agents/security-auditor.agent.md'])"
    changes = page.js(f"[...document.querySelectorAll(\"{card} .import-notes li\")].map((li) => li.textContent)")
    assert any("execute" in change or "read → Read" in change for change in changes)  # 工具换成了智狐的叫法
    assert any("vscode/getProjectSetupInfo" in change for change in changes)  # 智狐没有的不搬
    assert any("Claude Sonnet 4.5" in change for change in changes)  # 认不出来的模型改成跟着叫它的那一方
    try:
        page.click(f"{card} input[type='checkbox']")
        page.click("#panel-agents [data-key='install']")
        page.wait("document.querySelector(\"#panel-agents li.agents-row[data-name='security-auditor']\")", what="导入的 Agent 出现在列表里")
    finally:
        page.demo.api("/api/agent/delete", {"name": "security-auditor"})


def test_the_import_views_work_with_the_keyboard(page):
    _open_mcp_import(page)
    # 总览里的数字是按钮：Tab 能到，回车换页
    page.focus("#panel-mcp .import-tool[data-tool='gemini'] .import-chip[data-kind='agents']")
    page.key("Enter")
    page.wait("!document.getElementById('panel-agents').hidden && document.querySelector(\"#panel-agents .import-group[data-tool='gemini']\")", what="回车换到 Agent 的导入页")
    page.wait("document.activeElement.matches(\"#panel-agents .import-group[data-tool='gemini']\")")
    # MCP 的导入页里按 Esc 先回到服务器列表，不直接关掉窗口
    steps.extensions_tab(page, "mcp")
    page.focus("#panel-mcp .mcp-back")
    page.key("Escape")
    page.wait("document.getElementById('extensions').open && document.querySelector('#panel-mcp li.mcp-server')", what="回到服务器列表")
