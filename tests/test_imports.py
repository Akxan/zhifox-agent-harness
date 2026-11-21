"""从别的 AI 工具导入（harness/imports/，以及用到它的 mcp/importers.py、skills/from_tools.py、agents/from_tools.py）。

每个测试在假的家目录（conftest 把 Path.home() 换成了 tmp_path/home）和临时的工作目录里，照各家文档写出它们的配置文件，
再看智狐读出来的样子。为什么这样测：
  - 导入读的是"别人写的文件"，各家写法五花八门（JSON、带注释的 JSON、TOML、YAML……）。只有照着真实的写法
    放一份文件，才知道换算有没有漏掉什么、有没有多放开什么。
  - 位置用 Env 指定（家目录、工作目录、系统、环境变量），不碰这台电脑真正的家目录，也能假装在 Windows 上。
  - 只比较有辨识度的一小段原文（"不用问就调用"这种）或者值，不比较整句中文：措辞以后可以改。

网页上的三个接口（/api/mcp/scan、/api/skill/scan、/api/agent/scan）在最后一节，用 test_webui 的 serve。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import test_mcp
import test_webui

from harness.agents import AgentStore
from harness.agents import from_tools as agent_imports
from harness.agents.spec import Spec
from harness.agents.spec import check as check_agent
from harness.imports import readers, yaml_lite
from harness.imports.agent_formats import Converted, convert
from harness.imports.agent_tools import translate
from harness.imports.overview import overview
from harness.imports.places import Env
from harness.imports.refs import Refs
from harness.imports.registry import AGENTS, KINDS, MCP, SKILLS, TOOLS, places
from harness.mcp import McpConfig, importers
from harness.mcp.config import model_key_references
from harness.skills import SkillStore
from harness.skills import from_tools as skill_imports
from harness.skills.installer import Installer

# 借用别的测试文件里的 fixture（pytest 按模块里的名字找）：serve 起一个网页服务器；
# close_managers 在测试结束时关掉启动过的 MCP 服务器（网页上导入之后会去启动它们）
serve = test_webui.serve
close_managers = test_mcp.close_managers


# ---- 准备 ----------------------------------------------------------------------


@pytest.fixture
def home() -> Path:
    path = Path.home()  # conftest 已经换成了 tmp_path/home
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def work(tmp_path) -> Path:
    path = tmp_path / "work"
    path.mkdir(exist_ok=True)
    return path


@pytest.fixture
def env(home, work) -> Env:
    # 写明是 Linux：Linux 上桌面应用的配置在 ~/.config 下面，测试的结果不随跑测试的电脑变
    return Env(home=home, workspace=work, platform="linux")


@pytest.fixture
def config(tmp_path) -> McpConfig:
    return McpConfig(tmp_path / "data" / "mcp.json")


def write(path: Path, content: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return path


def scan(env: Env, config: McpConfig) -> dict[str, dict]:
    """页面看到的 MCP 清单（importers.preview），按给人看的路径（~/.cursor/mcp.json）找。

    ~/.claude.json 里有两处（用户级的，和按项目记的那一处）：按路径找到的是先列出的用户级，另一处用编号找。
    """
    listed: dict[str, dict] = {}
    for source in importers.preview(importers.sources(env.workspace, env), config):
        listed.setdefault(source["path"], source)
    return listed


def items(source: dict) -> dict[str, dict]:
    return {item["name"]: item for item in source["servers"]}


def source_at(env: Env, shown: str) -> importers.Source:
    (found,) = [source for source in importers.sources(env.workspace, env) if source.shown == shown]
    return found


def entries(env: Env, shown: str) -> dict[str, object]:
    """某个文件里的服务器换成智狐的写法以后的样子（还没有经过 normalize）。"""
    return {name: value.entry for name, value in source_at(env, shown).servers.items()}


def note(item: dict, words: str) -> dict:
    """一个服务器的某一条提醒（按原文里一小段有辨识度的字找），返回它的值。没有、或者有好几条，测试就失败。"""
    found = [one["values"] for one in item["notes"] if words in one["key"]]
    assert len(found) == 1, (words, item["notes"])
    return found[0]


def has_note(item: dict, words: str) -> bool:
    return any(words in one["key"] for one in item["notes"])


# 几条提醒的原文里有辨识度的一小段（见 imports/refs.py、imports/mcp_shapes.py）
RENAMED = "写法换成了"  # ${env:X}、$X、{env:X} → ${X}
NEVER_ASK = "不用问就调用"  # autoApprove、alwaysAllow、trust……没有搬过来
DROPPED = "没有搬过来的设置"
FILLED = "换成了 {value}"  # ${workspaceFolder} 这些换成了这台电脑上的值
UNRESOLVED = "当场输入"  # ${input:…}、{file:…} 智狐做不到
MODEL_KEY = "模型密钥"


# ---- 各家 MCP 配置的写法 ------------------------------------------------------------


def test_cursor_env_references_editor_variables_and_auto_approve(env, config):
    write(env.home / ".cursor" / "mcp.json", {"mcpServers": {
        "github": {
            "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github", "${userHome}/repos"],
            "env": {"GITHUB_TOKEN": "${env:GITHUB_TOKEN}"}, "autoApprove": ["search_code"],
        },
    }})  # fmt: skip
    source = scan(env, config)["~/.cursor/mcp.json"]
    assert (source["tool"], source["tool_id"], source["level"], source["found"], source["problem"]) == ("Cursor", "cursor", "用户级", True, "")
    github = items(source)["github"]
    assert (github["target"], github["transport"], github["problem"], github["duplicate"]) == ("github", "stdio", "", False)
    assert note(github, RENAMED)["names"] == "GITHUB_TOKEN"
    assert note(github, NEVER_ASK) == {"tool": "Cursor", "fields": "autoApprove"}  # 那边"不用问"，这边照样问
    assert note(github, FILLED) == {"name": "${userHome}", "value": str(env.home)}
    assert entries(env, "~/.cursor/mcp.json")["github"] == {
        "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github", f"{env.home}/repos"], "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
    }  # fmt: skip


def test_windsurf_server_url_and_the_devin_transport_field(env, config):
    write(env.home / ".codeium" / "windsurf" / "mcp_config.json", {"mcpServers": {
        "docs": {"serverUrl": "https://docs.example/mcp", "headers": {"Authorization": "Bearer ${env:DOCS_TOKEN}"}},
    }})  # fmt: skip
    # Windsurf 归了 Devin 以后的新位置：transport 写的是一个词
    write(env.home / ".config" / "devin" / "mcp_config.json", {"mcpServers": {"remote": {"transport": "http", "url": "https://devin.example/mcp"}}})
    listed = scan(env, config)
    docs = items(listed["~/.codeium/windsurf/mcp_config.json"])["docs"]
    assert (docs["transport"], docs["problem"], docs["summary"]["url"]) == ("http", "", "https://docs.example/mcp")
    assert entries(env, "~/.codeium/windsurf/mcp_config.json")["docs"] == {
        "type": "http", "url": "https://docs.example/mcp", "headers": {"Authorization": "Bearer ${DOCS_TOKEN}"},
    }  # fmt: skip
    assert listed["~/.config/devin/mcp_config.json"]["tool"] == "Windsurf"
    assert entries(env, "~/.config/devin/mcp_config.json")["remote"] == {"type": "http", "url": "https://devin.example/mcp"}


def test_cline_url_without_type_is_sse_and_the_nested_transport(env, config):
    write(env.home / ".cline" / "data" / "settings" / "cline_mcp_settings.json", {"mcpServers": {
        "legacy": {"url": "https://legacy.example/sse"},  # Cline 里有 url 没写 type 的是 SSE
        "modern": {"type": "streamableHttp", "url": "https://modern.example/mcp"},
        "nested": {"transport": {"type": "stdio", "command": "node", "args": ["server.js"]}, "disabled": True, "alwaysAllow": ["read"]},
    }})  # fmt: skip
    # 以前的位置（VS Code 扩展的 globalStorage）也认
    write(env.home / ".config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json", {"mcpServers": {"old-place": {"command": "x"}}})
    listed = scan(env, config)
    cline = items(listed["~/.cline/data/settings/cline_mcp_settings.json"])
    assert cline["legacy"]["transport"] == "sse" and "SSE" in cline["legacy"]["problem"] and cline["legacy"]["target"] == ""
    assert (cline["modern"]["transport"], cline["modern"]["problem"]) == ("http", "")
    assert note(cline["nested"], NEVER_ASK) == {"tool": "Cline", "fields": "alwaysAllow"}
    found = entries(env, "~/.cline/data/settings/cline_mcp_settings.json")
    assert found["nested"] == {"command": "node", "args": ["server.js"], "enabled": False}  # disabled 换成了智狐的 enabled
    old = listed["~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json"]
    assert old["tool"] == "Cline" and [item["name"] for item in old["servers"]] == ["old-place"]


def test_roo_code_project_file(env, config, work):
    write(work / ".roo" / "mcp.json", {"mcpServers": {
        "remote": {"type": "streamable-http", "url": "https://roo.example/mcp", "alwaysAllow": ["search"]},
        "local": {"command": "node", "args": ["${workspaceFolder}/server.js"], "env": {"KEY": "${env:ROO_KEY}"}, "cwd": "${workspaceFolder}"},
    }})  # fmt: skip
    source = scan(env, config)[".roo/mcp.json"]
    assert (source["tool"], source["level"]) == ("Roo Code", "这个项目")
    assert note(items(source)["remote"], NEVER_ASK)["fields"] == "alwaysAllow"
    found = entries(env, ".roo/mcp.json")
    assert found["remote"] == {"type": "http", "url": "https://roo.example/mcp"}
    assert found["local"] == {"command": "node", "args": [f"{work}/server.js"], "env": {"KEY": "${ROO_KEY}"}, "cwd": str(work)}


@pytest.mark.parametrize(
    ("relative", "tool"),
    [
        (".kiro/settings/mcp.json", "Kiro"),
        (".aws/amazonq/mcp.json", "Amazon Q Developer"),
        (".junie/mcp/mcp.json", "Junie"),
        (".lmstudio/mcp.json", "LM Studio"),
        (".warp/.mcp.json", "Warp"),
        (".copilot/mcp-config.json", "GitHub Copilot"),
    ],
)
def test_claude_style_files_of_other_tools(env, config, relative, tool):
    # 这几家学的都是 Claude Desktop 的写法；有几家把工作目录叫 working_directory
    write(env.home / relative, {"mcpServers": {"srv": {"command": "srv-mcp", "args": ["--port", 8080], "working_directory": "/srv/app", "env": {"PORT": 8080}}}})
    source = scan(env, config)["~/" + relative]
    assert (source["tool"], source["level"]) == (tool, "用户级")
    srv = items(source)["srv"]
    assert (srv["problem"], srv["summary"]["cwd"]) == ("", "/srv/app")
    assert entries(env, "~/" + relative)["srv"]["cwd"] == "/srv/app"
    added, skipped = importers.apply(config, source_at(env, "~/" + relative).servers, ["srv"])
    assert (added, skipped) == (["srv"], [])
    saved = config.get("srv")
    assert (saved["cwd"], saved["args"], saved["env"]) == ("/srv/app", ["--port", "8080"], {"PORT": "8080"})  # 不带引号的数当成文字收下


VSCODE = """// VS Code 的 mcp.json 允许注释和多余的逗号
{
  "inputs": [{"type": "promptString", "id": "api-key", "description": "API key", "password": true}],
  /* 两个服务器：
     一个本地的，一个远程的 */
  "servers": {
    "local": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "some-server", "${workspaceFolder}", "https://example.com//path"],
      "env": {"API_KEY": "${input:api-key}", "TOKEN": "${env:VS_TOKEN}", "DROP": null},
      "envFile": "${workspaceFolder}/.env",
    },
    "remote": {"type": "http", "url": "https://vs.example/mcp", "headers": {"X-Key": "${input:api-key}"},},
  },
}
"""


def test_vscode_jsonc_inputs_and_workspace_folder(env, config, work):
    write(work / ".vscode" / "mcp.json", VSCODE)
    source = scan(env, config)[".vscode/mcp.json"]
    assert (source["tool"], source["level"], source["problem"]) == ("VS Code", "这个项目", "")
    local, remote = items(source)["local"], items(source)["remote"]
    assert note(local, UNRESOLVED) == {"what": "${input:api-key}", "tool": "VS Code"}  # 要你当场输入的：原样留着，提醒导入后填上
    assert note(local, FILLED) == {"name": "${workspaceFolder}", "value": str(work)}
    assert note(local, RENAMED)["names"] == "VS_TOKEN"
    assert note(local, "envFile")["file"] == "${workspaceFolder}/.env"
    assert note(local, DROPPED)["fields"] == "env.DROP"  # VS Code 里 null 表示"去掉这个变量"
    assert has_note(remote, UNRESOLVED) and remote["problem"] == ""
    assert entries(env, ".vscode/mcp.json")["local"] == {
        "command": "npx", "args": ["-y", "some-server", str(work), "https://example.com//path"],  # 字符串里的 // 不是注释
        "env": {"API_KEY": "${input:api-key}", "TOKEN": "${VS_TOKEN}"},
    }  # fmt: skip


def test_vscode_settings_json_mcp_servers(env, config):
    write(env.home / ".config/Code/User/settings.json", '{\n  // 编辑器的设置\n  "editor.fontSize": 14,\n  "mcp": {"servers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}},\n}\n')
    source = scan(env, config)["~/.config/Code/User/settings.json"]
    assert source["tool"] == "VS Code" and source["problem"] == ""
    assert entries(env, "~/.config/Code/User/settings.json") == {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}


def test_vscode_source_ids_are_numbered(env, config, work):
    # 同一个工具的几处用户级配置：编号依次加 -2、-3（页面导入时送回来的就是它）
    for path in (env.home / ".config/Code/User/mcp.json", env.home / ".config/Code - Insiders/User/mcp.json", env.home / ".config/Code/User/settings.json"):
        write(path, "{}")
    ids = {source.shown: source.id for source in importers.sources(work, env) if source.tool_id == "vscode"}
    assert ids == {
        "~/.config/Code/User/mcp.json": "vscode-user",
        "~/.config/Code - Insiders/User/mcp.json": "vscode-user-2",
        "~/.config/Code/User/settings.json": "vscode-user-3",
        ".vscode/mcp.json": "vscode-project",
    }


ZED = """{
  // Zed 的 settings.json 里还有很多别的设置
  "theme": "One Dark",
  "context_servers": {
    "legacy": {"command": {"path": "zed-mcp", "args": ["--stdio"], "env": {"Z": "1"}}},
    "modern": {"command": "uvx", "args": ["mcp-server-time"], "env": {}},
    "remote": {"url": "https://zed.example/mcp"},
    "from-extension": {"source": "extension", "settings": {"key": "v"}},
    "old-extension": {"settings": {}},
  },
}
"""


def test_zed_context_servers(env, config, work):
    write(env.home / ".config" / "zed" / "settings.json", ZED)
    write(work / ".zed" / "settings.json", '{"context_servers": {"project-one": {"command": "p"}}}')
    listed = scan(env, config)
    zed = items(listed["~/.config/zed/settings.json"])
    assert all(zed[name]["problem"] == "" for name in ("legacy", "modern", "remote"))
    for name in ("from-extension", "old-extension"):  # 扩展提供的：命令在扩展里，拿不到
        assert "Zed" in zed[name]["problem"] and zed[name]["target"] == "" and zed[name]["summary"] is None
    found = entries(env, "~/.config/zed/settings.json")
    assert found["legacy"] == {"command": "zed-mcp", "args": ["--stdio"], "env": {"Z": "1"}}  # 旧写法：command 是 {path, args, env}
    assert found["modern"] == {"command": "uvx", "args": ["mcp-server-time"]}
    assert found["remote"] == {"type": "http", "url": "https://zed.example/mcp"}
    assert listed[".zed/settings.json"]["level"] == "这个项目" and [item["name"] for item in listed[".zed/settings.json"]["servers"]] == ["project-one"]


CODEX = """model = "gpt-6"  # 别的设置

[mcp_servers.context7]
command = "npx"
args = ["-y", "@upstash/context7-mcp"]
env = { PLAIN = "1" }
env_vars = ["CONTEXT7_KEY", { name = "OTHER_KEY", source = "local" }]
startup_timeout_sec = 20

[mcp_servers.figma]
url = "https://mcp.figma.com/mcp"
bearer_token_env_var = "FIGMA_TOKEN"
http_headers = { "X-Static" = "static" }
env_http_headers = { "X-Team" = "FIGMA_TEAM" }

[mcp_servers.off]
command = "off-mcp"
enabled = false
"""


def test_codex_toml_env_var_names_become_references(env, config, work):
    write(env.home / ".codex" / "config.toml", CODEX)
    source = scan(env, config)["~/.codex/config.toml"]
    assert (source["tool"], source["level"], source["problem"]) == ("Codex", "用户级", "")
    listed = items(source)
    assert note(listed["context7"], "从环境变量里交给它")["names"] == "CONTEXT7_KEY, OTHER_KEY"
    assert note(listed["context7"], DROPPED)["fields"] == "startup_timeout_sec"
    assert note(listed["figma"], "从环境变量里交给它")["names"] == "FIGMA_TOKEN, FIGMA_TEAM"
    found = entries(env, "~/.codex/config.toml")
    # Codex 只写环境变量的名字：换成 ${名字}，智狐启动它时同样从环境变量里取
    assert found["context7"] == {"command": "npx", "args": ["-y", "@upstash/context7-mcp"], "env": {"CONTEXT7_KEY": "${CONTEXT7_KEY}", "OTHER_KEY": "${OTHER_KEY}", "PLAIN": "1"}}
    assert found["figma"] == {
        "type": "http", "url": "https://mcp.figma.com/mcp",
        "headers": {"X-Static": "static", "Authorization": "Bearer ${FIGMA_TOKEN}", "X-Team": "${FIGMA_TEAM}"},
    }  # fmt: skip
    assert found["off"] == {"command": "off-mcp", "enabled": False}


def test_codex_project_file_and_codex_home(home, work, tmp_path, config):
    write(work / ".codex" / "config.toml", '[mcp_servers.proj]\ncommand = "p"\n')
    write(tmp_path / "codex-home" / "config.toml", '[mcp_servers.moved]\ncommand = "m"\n')
    write(home / ".codex" / "config.toml", '[mcp_servers.ignored]\ncommand = "i"\n')
    # 设了 CODEX_HOME：Codex 的用户级配置在那里，~/.codex 不再看
    env = Env(home=home, workspace=work, platform="linux", environ={"CODEX_HOME": str(tmp_path / "codex-home")})
    codex = {source.level: source for source in importers.sources(work, env) if source.tool_id == "codex"}
    assert list(codex["user"].servers) == ["moved"] and codex["user"].path == tmp_path / "codex-home" / "config.toml"
    assert list(codex["project"].servers) == ["proj"] and codex["project"].shown == ".codex/config.toml"
    # 不是绝对路径的当作没设
    env = Env(home=home, workspace=work, platform="linux", environ={"CODEX_HOME": "relative/codex"})
    assert list(source_at(env, "~/.codex/config.toml").servers) == ["ignored"]


GOOSE = """# Goose 的配置
GOOSE_PROVIDER: anthropic
extensions:
  developer:
    type: builtin
    name: developer
    enabled: true
  github:
    type: stdio
    name: GitHub
    cmd: npx
    args: [-y, '@modelcontextprotocol/server-github']
    envs: {LOG_LEVEL: debug}
    env_keys: [GITHUB_PERSONAL_ACCESS_TOKEN]
    enabled: true
    timeout: 300
  remote:
    type: streamable_http
    uri: https://goose.example/mcp
    headers:
      X-Team: core
    enabled: false
  legacy:
    type: sse
    uri: https://goose.example/sse
  inline:
    type: inline_python
    code: print(1)
  odd:
    type: websocket
"""


def test_goose_extensions(env, config):
    write(env.home / ".config" / "goose" / "config.yaml", GOOSE)
    source = scan(env, config)["~/.config/goose/config.yaml"]
    assert (source["tool"], source["problem"]) == ("Goose", "")
    listed = items(source)
    assert set(listed) == {"github", "remote", "legacy", "odd"}  # builtin 和 inline_python 是 Goose 自带的，不是 MCP 服务器
    assert note(listed["github"], "钥匙串") == {"names": "GITHUB_PERSONAL_ACCESS_TOKEN", "tool": "Goose"}
    assert note(listed["github"], DROPPED)["fields"] == "timeout"
    assert "SSE" in listed["legacy"]["problem"] and "websocket" in listed["odd"]["problem"]
    found = entries(env, "~/.config/goose/config.yaml")
    assert found["github"] == {
        "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"LOG_LEVEL": "debug", "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN}"},
    }  # fmt: skip
    assert found["remote"] == {"type": "http", "url": "https://goose.example/mcp", "headers": {"X-Team": "core"}, "enabled": False}


CONTINUE = """name: My Config
version: 0.0.1
schema: v1
models: []
mcpServers:
  - name: Fetch
    command: uvx
    args:
      - mcp-server-fetch
    env:
      FETCH_KEY: ${{ secrets.FETCH_KEY }}
  - name: Remote Docs
    type: streamable-http
    url: https://continue.example/mcp
    requestOptions:
      headers:
        Authorization: Bearer ${{ secrets.DOCS_TOKEN }}
  - uses: anthropic/memory-mcp
  - command: npx
    args: [-y, "@modelcontextprotocol/server-everything"]
"""


def test_continue_config_yaml_list_and_secrets(env, config):
    write(env.home / ".continue" / "config.yaml", CONTINUE)
    source = scan(env, config)["~/.continue/config.yaml"]
    assert (source["tool"], source["problem"]) == ("Continue", "")
    listed = items(source)
    assert list(listed) == ["Fetch", "Remote Docs", "server", "everything"]  # 没写名字的：从包名里起一个
    assert note(listed["Fetch"], "secrets")["names"] == "FETCH_KEY"
    assert listed["Remote Docs"]["target"] == "Remote-Docs"  # 名字里的空格换成了连字符
    assert "Continue Hub" in listed["server"]["problem"] and listed["server"]["target"] == ""
    found = entries(env, "~/.continue/config.yaml")
    assert found["Fetch"] == {"command": "uvx", "args": ["mcp-server-fetch"], "env": {"FETCH_KEY": "${FETCH_KEY}"}}
    assert found["Remote Docs"] == {"type": "http", "url": "https://continue.example/mcp", "headers": {"Authorization": "Bearer ${DOCS_TOKEN}"}}


def test_continue_mcp_servers_folder_and_legacy_config_json(env, config, work):
    folder = work / ".continue" / "mcpServers"
    write(folder / "fetch.yaml", "name: Fetch block\nversion: 0.0.1\nschema: v1\nmcpServers:\n  - name: fetch\n    command: uvx\n    args: [mcp-server-fetch]\n")
    write(folder / "claude.json", {"mcpServers": {"from-json": {"command": "node", "env": {"K": "${{ secrets.K }}"}}}})  # 也可以放 Claude Desktop 写法的 JSON
    write(folder / "notes.txt", "不是配置")
    write(folder / ".hidden.yaml", "mcpServers: []")
    write(env.home / ".continue" / "config.json", {"experimental": {"modelContextProtocolServers": [{"transport": {"type": "stdio", "command": "uvx", "args": ["mcp-server-sqlite"]}}]}})
    found = {source.shown: source for source in importers.sources(work, env) if source.found}
    assert {shown: source.id for shown, source in found.items() if shown.startswith(".continue")} == {
        ".continue/mcpServers/claude.json": "continue-project:claude.json",
        ".continue/mcpServers/fetch.yaml": "continue-project:fetch.yaml",
    }  # 文件夹里每个文件各算一处，编号是"文件夹的编号:文件名"
    assert entries(env, ".continue/mcpServers/fetch.yaml") == {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}
    assert entries(env, ".continue/mcpServers/claude.json") == {"from-json": {"command": "node", "env": {"K": "${K}"}}}
    assert entries(env, "~/.continue/config.json") == {"sqlite": {"command": "uvx", "args": ["mcp-server-sqlite"]}}


OPENCODE = """{
  "$schema": "https://opencode.ai/config.json",
  // MCP 服务器
  "mcp": {
    "local": {"type": "local", "command": ["bunx", "my-mcp", "--flag"], "environment": {"A": "{env:A_KEY}"}, "enabled": false},
    "remote": {"type": "remote", "url": "https://oc.example/mcp", "headers": {"Authorization": "Bearer {env:OC_TOKEN}"}, "oauth": false},
    "toggle-only": {"enabled": true},
    "file-ref": {"type": "local", "command": ["x"], "environment": {"K": "{file:~/.secrets/k}"}},
    "empty": {"type": "local", "command": []},
  },
}
"""


def test_opencode_jsonc_mcp(env, config, work):
    write(work / "opencode.jsonc", OPENCODE)
    source = scan(env, config)["opencode.jsonc"]
    assert (source["tool"], source["level"], source["problem"]) == ("OpenCode", "这个项目", "")
    listed = items(source)
    assert "toggle-only" not in listed  # 只写了一个开关：服务器定义在别处
    assert note(listed["local"], RENAMED)["names"] == "A_KEY"
    assert note(listed["file-ref"], UNRESOLVED)["what"] == "{file:~/.secrets/k}"
    assert listed["empty"]["problem"] and listed["empty"]["target"] == ""
    found = entries(env, "opencode.jsonc")
    assert found["local"] == {"command": "bunx", "args": ["my-mcp", "--flag"], "env": {"A": "${A_KEY}"}, "enabled": False}
    assert found["remote"] == {"type": "http", "url": "https://oc.example/mcp", "headers": {"Authorization": "Bearer ${OC_TOKEN}"}}
    assert not has_note(listed["remote"], DROPPED)  # oauth: false 就是不用 OAuth，不算丢了什么


def test_opencode_v2_servers(env, config):
    write(env.home / ".config" / "opencode" / "opencode.json", {"mcp": {"servers": {"v2": {"type": "remote", "url": "https://v2.example/mcp"}}}})
    assert entries(env, "~/.config/opencode/opencode.json") == {"v2": {"type": "http", "url": "https://v2.example/mcp"}}


def test_gemini_http_url_and_dollar_references(env, config):
    write(env.home / ".gemini" / "settings.json", {"mcpServers": {
        "remote": {"httpUrl": "https://gemini.example/mcp", "headers": {"X-Key": "$GEMINI_SERVER_KEY"}, "trust": True, "timeout": 30000},
        "local": {"command": "node", "args": ["$HOME/server.js"], "env": {"T": "$G_TOKEN", "U": "${G_U}", "PRICE": "$$5"}},
    }})  # fmt: skip
    write(env.home / ".qwen" / "settings.json", {"mcpServers": {"qw": {"httpUrl": "https://qwen.example/mcp"}}})  # Qwen Code 是同一种写法
    listed = scan(env, config)
    gemini = items(listed["~/.gemini/settings.json"])
    assert gemini["remote"]["transport"] == "http" and note(gemini["remote"], NEVER_ASK) == {"tool": "Gemini CLI", "fields": "trust"}
    assert note(gemini["local"], RENAMED)["names"] == "HOME, G_TOKEN"
    found = entries(env, "~/.gemini/settings.json")
    assert found["remote"] == {"type": "http", "url": "https://gemini.example/mcp", "headers": {"X-Key": "${GEMINI_SERVER_KEY}"}}
    assert found["local"] == {"command": "node", "args": ["${HOME}/server.js"], "env": {"T": "${G_TOKEN}", "U": "${G_U}", "PRICE": "$$5"}}
    assert entries(env, "~/.qwen/settings.json") == {"qw": {"type": "http", "url": "https://qwen.example/mcp"}}


def test_a_gemini_url_without_type_is_tried_as_streamable_http_with_a_warning(env, config):
    # Gemini CLI 的文档说 url 是 SSE，可它的源码对只写了 url 的服务器先试 Streamable HTTP、不行才退回 SSE。
    # 智狐照着源码的第一步当它是 Streamable HTTP 导入（不然很多其实支持 Streamable HTTP 的服务器就导入不了），
    # 并在清单上提醒：连不上的话，它可能只支持 SSE。写明了 type: sse 的照样不能导入
    write(env.home / ".gemini" / "settings.json", {"mcpServers": {
        "bare": {"url": "http://localhost:8080/mcp"},
        "sse": {"url": "http://localhost:8080/sse", "type": "sse"},
        "typed": {"url": "http://localhost:8080/mcp", "type": "http"},
    }})  # fmt: skip
    listed = items(scan(env, config)["~/.gemini/settings.json"])
    assert listed["bare"]["transport"] == "http" and listed["bare"]["target"] == "bare"
    assert any("SSE" in item["key"] for item in listed["bare"]["notes"])
    assert listed["sse"]["transport"] == "sse" and "SSE" in listed["sse"]["problem"] and listed["sse"]["target"] == ""
    assert not any("SSE" in item["key"] for item in listed["typed"]["notes"])  # 写明了 type 的不用提醒


# ---- 换写法时的几条规矩 -------------------------------------------------------------


def test_refs_unit():
    env = Env(home=Path("/home/me"), workspace=Path("/home/me/project"), platform="linux")
    refs = Refs(env, "X", dollar=True, brace=True, editor=True, secrets=True)
    assert refs.text("${env:A}-$B-{env:C}-${{ secrets.D }}") == "${A}-${B}-${C}-${D}"
    assert refs.text("$$E and ${F} and ${F:-x}") == "$$E and ${F} and ${F:-x}"  # 已经是智狐的写法、$$ 都不动
    assert refs.text("${workspaceFolder}${/}${workspaceFolderBasename}") == "/home/me/project/project"
    assert refs.text("${input:pw} {file:./k} ${command:pick}") == "${input:pw} {file:./k} ${command:pick}"
    assert refs.text(5) == 5 and refs.pairs("x") == "x" and refs.items({"a": 1}) == {"a": 1}  # 写错类型的原样交给 normalize
    assert set(refs.renamed) == {"A", "B", "C"} and list(refs.from_secrets) == ["D"]
    assert list(refs.unresolved) == ["${input:pw}", "{file:./k}", "${command:pick}"]
    # 只认这个工具自己的写法：Cursor 的 $X 是普通的文字
    plain = Refs(env, "Cursor")
    assert plain.text("$B {env:C} ${{ secrets.D }}") == "$B {env:C} ${{ secrets.D }}"
    assert Refs(Env(home=Path("/h"), workspace=Path("/w"), platform="win32"), "VS Code", editor=True).text("a${/}b") == "a\\b"


def test_imported_servers_never_keep_auto_approval(env, config, work):
    # 一份克隆来的 .mcp.json 不该替你决定"不用问"：智狐自己的 approval 也不搬
    write(work / ".mcp.json", {"mcpServers": {"auto": {"command": "x", "approval": {"default": "auto", "tools": {"search": "auto"}}, "timeout": 30}}})
    item = items(scan(env, config)[".mcp.json"])["auto"]
    assert note(item, NEVER_ASK)["fields"] == "approval"
    assert "approval" not in entries(env, ".mcp.json")["auto"]
    added, _ = importers.apply(config, source_at(env, ".mcp.json").servers, ["auto"])
    assert added == ["auto"] and "approval" not in config.get("auto")
    assert config.approval("auto", "search") == "ask" and config.approval("auto", "other") == "ask"


def test_name_conflicts_and_duplicates(env, config, work):
    config.put("github", {"command": "something-else"})
    config.put("fs", {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]})
    write(env.home / ".cursor" / "mcp.json", {"mcpServers": {
        "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]},
        "fs": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"], "autoApprove": ["read"]},
        "my server / v2": {"command": "m"},
        "shared": {"command": "one"},
    }})  # fmt: skip
    write(work / ".cursor" / "mcp.json", {"mcpServers": {"shared": {"command": "two"}}})
    listed = scan(env, config)
    user, project = items(listed["~/.cursor/mcp.json"]), items(listed[".cursor/mcp.json"])
    assert (user["github"]["target"], user["github"]["duplicate"]) == ("github-2", False)  # 名字被占了：换一个
    assert user["fs"]["duplicate"] is True  # 连的是同一个服务器（"问不问"不算）：页面上默认不勾
    assert user["my server / v2"]["target"] == "my-server-v2"
    # 清单里两个来源的同名服务器也不会撞名
    assert (user["shared"]["target"], project["shared"]["target"]) == ("shared", "shared-2")
    added, skipped = importers.apply(config, source_at(env, "~/.cursor/mcp.json").servers, ["github", "my server / v2", "nowhere"])
    assert added == ["github-2", "my-server-v2"] and len(skipped) == 1
    assert config.get("github") == {"command": "something-else", "args": [], "env": {}, "enabled": True}  # 原来的没动


def test_secrets_never_reach_the_page(env, config, work):
    secrets = {
        "flag": "flag-secret-0123456789abcdef",
        "spaced": "spaced-secret-0123456789abcd",
        "password": "url-password-0123456789",
        "env": "env-secret-value-0123456789",
        "query": "query-secret-0123456789abc",
        "header": "header-secret-0123456789ab",
        "codex": "codex-header-secret-012345",
        "goose": "goose-env-secret-0123456789",
        "continue": "continue-secret-0123456789",
        "key_like": "ghp_" + "k" * 36,
    }
    write(env.home / ".cursor" / "mcp.json", {"mcpServers": {
        "local": {"command": "npx", "args": ["--token=" + secrets["flag"], "--api-key", secrets["spaced"], "https://me:" + secrets["password"] + "@x.example/", secrets["key_like"]],
                  "env": {"API": secrets["env"]}},
        "remote": {"url": "https://r.example/mcp?api_key=" + secrets["query"], "headers": {"Authorization": "Bearer " + secrets["header"]}},
    }})  # fmt: skip
    write(env.home / ".codex" / "config.toml", f'[mcp_servers.c]\nurl = "https://c.example/mcp"\nhttp_headers = {{ "X-Key" = "{secrets["codex"]}" }}\n')
    write(env.home / ".config" / "goose" / "config.yaml", f"extensions:\n  g:\n    type: stdio\n    cmd: g\n    envs:\n      SECRET: {secrets['goose']}\n")
    write(env.home / ".continue" / "config.yaml", f"mcpServers:\n  - name: c\n    type: streamable-http\n    url: https://c.example/mcp\n    requestOptions:\n      headers:\n        X-Key: {secrets['continue']}\n")
    listed = importers.preview(importers.sources(work, env), config)
    assert sum(len(source["servers"]) for source in listed) == 5
    shown = json.dumps(listed, ensure_ascii=False) + json.dumps(overview(env), ensure_ascii=False)
    for name, secret in secrets.items():
        assert secret[:-4] not in shown, name  # 最多露出最后 4 位（和设置面板里的密钥一样）
    # 遮住的只是给页面看的那一份：导入的是完整的配置
    importers.apply(config, source_at(env, "~/.cursor/mcp.json").servers, ["local"])
    assert config.get("local")["env"] == {"API": secrets["env"]} and secrets["flag"] in config.get("local")["args"][0]


MODEL_KEYS = [
    # (文件, 内容, 服务器的名字, 引用的密钥)
    (".mcp.json", {"mcpServers": {"a": {"command": "x", "env": {"K": "${ANTHROPIC_API_KEY}"}}}}, "a", "ANTHROPIC_API_KEY"),
    ("~/.cursor/mcp.json", {"mcpServers": {"b": {"url": "https://b.example/mcp", "headers": {"Authorization": "Bearer ${env:OPENAI_API_KEY}"}}}}, "b", "OPENAI_API_KEY"),
    ("~/.codex/config.toml", '[mcp_servers.c]\ncommand = "c"\nenv_vars = ["DEEPSEEK_API_KEY"]\n', "c", "DEEPSEEK_API_KEY"),
    ("~/.config/goose/config.yaml", "extensions:\n  d:\n    type: stdio\n    cmd: d\n    env_keys: [GEMINI_API_KEY]\n", "d", "GEMINI_API_KEY"),
    ("~/.gemini/settings.json", {"mcpServers": {"e": {"command": "e", "args": ["--key", "$ANTHROPIC_AUTH_TOKEN"]}}}, "e", "ANTHROPIC_AUTH_TOKEN"),
    ("opencode.json", {"mcp": {"f": {"type": "local", "command": ["f"], "environment": {"K": "{env:OPENAI_API_KEY}"}}}}, "f", "OPENAI_API_KEY"),
    ("~/.continue/config.yaml", "mcpServers:\n  - name: g\n    command: g\n    env:\n      K: ${{ secrets.ANTHROPIC_API_KEY }}\n", "g", "ANTHROPIC_API_KEY"),
    (".vscode/mcp.json", '{"servers": {"h": {"command": "h", "cwd": "${ANTHROPIC_API_KEY:-/tmp}"}}}', "h", "ANTHROPIC_API_KEY"),
]


@pytest.mark.parametrize(("file", "content", "name", "key"), MODEL_KEYS, ids=[case[2] + ":" + case[3] for case in MODEL_KEYS])
def test_model_key_references_are_listed_but_never_imported(env, config, work, file, content, name, key):
    path = env.home / file[2:] if file.startswith("~/") else work / file
    write(path, content)
    item = items(scan(env, config)[file])[name]
    assert MODEL_KEY in item["problem"] and key in item["problem"] and item["target"] == ""
    assert item["summary"] is not None  # 仍然看得到它的命令和地址：你知道没导入的是哪一个
    added, skipped = importers.apply(config, source_at(env, file).servers, [name])
    assert added == [] and len(skipped) == 1 and key in skipped[0]
    assert config.servers() == {} and not config.path.exists()  # 一个字都没有写进 mcp.json


def test_pasted_servers_with_model_key_references_are_refused(config):
    pasted = importers.parse_pasted('{"mcpServers": {"ok": {"command": "x"}, "bad": {"command": "y", "env": {"K": "${OPENAI_API_KEY}"}}}}')
    (listed,) = importers.preview([importers.pasted(pasted)], config)
    by_name = items(listed)
    assert by_name["ok"]["problem"] == "" and MODEL_KEY in by_name["bad"]["problem"] and by_name["bad"]["target"] == ""
    added, skipped = importers.apply(config, pasted, ["ok", "bad"])
    assert added == ["ok"] and len(skipped) == 1 and "OPENAI_API_KEY" in skipped[0]
    assert list(config.servers()) == ["ok"]


def test_model_key_references_unit():
    entry = {"command": "${A}", "args": ["${OPENAI_API_KEY}", "x"], "env": {"K": "pre ${ANTHROPIC_API_KEY} ${OPENAI_API_KEY}"}, "headers": {"H": "${DEEPSEEK_API_KEY:-no}"}}
    assert model_key_references(entry) == ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"]  # 按出现的顺序、不重复
    assert model_key_references({"command": "x", "env": {"ANTHROPIC_API_KEY": "literal"}, "extra": "${OPENAI_API_KEY}"}) == []  # 只看会交给它的值


# ---- 读不出来的文件 -----------------------------------------------------------------


def test_broken_files_explain_themselves_and_the_rest_still_lists(env, config, work):
    write(env.home / ".cursor" / "mcp.json", "{ not json")
    write(work / ".vscode" / "mcp.json", '{\n  // 注释不影响行号\n  "servers": {\n    "a": {"command": "x"}\n    "b": {"command": "y"}\n  }\n}\n')
    write(env.home / ".codex" / "config.toml", "[mcp_servers.x\ncommand = 1\n")
    write(env.home / ".config" / "goose" / "config.yaml", "extensions:\n  a: &anchor 1\n")
    write(work / ".continue" / "mcpServers" / "bad.yaml", "mcpServers:\n\t- name: x\n")
    write(work / ".continue" / "mcpServers" / "good.yaml", "mcpServers:\n  - name: good\n    command: g\n")
    write(env.home / ".gemini" / "settings.json", b"\xff\xfe\x00 not utf-8")
    write(env.home / ".kiro" / "settings" / "mcp.json", {"mcpServers": {"weird": "npx some-server"}})  # 服务器不是一个对象
    write(env.home / ".claude.json", {"mcpServers": {"fine": {"command": "fine"}}})
    listed = scan(env, config)
    for shown, words in (
        ("~/.cursor/mcp.json", "JSON"),
        (".vscode/mcp.json", "第 5 行"),  # 去掉注释后行号仍然对得上原来的文件
        ("~/.codex/config.toml", "TOML"),
        ("~/.config/goose/config.yaml", "第 2 行"),
        (".continue/mcpServers/bad.yaml", "YAML"),
        ("~/.gemini/settings.json", "UTF-8"),
    ):
        source = listed[shown]
        assert source["found"] is True and words in source["problem"] and source["servers"] == [], (shown, source["problem"])
    weird = items(listed["~/.kiro/settings/mcp.json"])["weird"]
    assert weird["problem"] and weird["target"] == "" and listed["~/.kiro/settings/mcp.json"]["problem"] == ""
    assert [item["name"] for item in listed[".continue/mcpServers/good.yaml"]["servers"]] == ["good"]
    assert [item["name"] for item in listed["~/.claude.json"]["servers"]] == ["fine"]
    tools = {item["id"]: item for item in overview(env)}
    assert tools["cursor"]["found"] is True and tools["cursor"]["problems"] == 1 and tools["cursor"]["counts"]["mcp"] == 0


def test_a_folder_where_a_file_should_be_is_not_found(env, config):
    (env.home / ".cursor" / "mcp.json").mkdir(parents=True)
    assert scan(env, config)["~/.cursor/mcp.json"]["found"] is False


BADLY_TYPED = [
    # 文件能读出来，只是某一项的类型写错了：应该只有这一个服务器有问题，别的照常列出来
    ("~/.codex/config.toml", '[mcp_servers.x]\nurl = "https://x.example/mcp"\nhttp_headers = "oops"\n'),
    ("~/.codex/config.toml", '[mcp_servers.x]\nurl = "https://x.example/mcp"\nenv_http_headers = ["A"]\n'),
    ("~/.codex/config.toml", '[mcp_servers.x]\ncommand = "x"\nenv_vars = 5\n'),
    ("~/.config/goose/config.yaml", "extensions:\n  x:\n    type: stdio\n    cmd: x\n    env_keys: 5\n"),
    ("~/.continue/config.yaml", "mcpServers:\n  - name: x\n    url: https://x.example/mcp\n    headers: [a]\n    requestOptions:\n      headers:\n        A: b\n"),
    ("~/.continue/config.yaml", "mcpServers:\n  - command: x\n    args: 5\n"),
    ("~/.config/opencode/opencode.json", {"mcp": {"x": {"type": "local", "command": ["x"], "args": 5}}}),
]


@pytest.mark.parametrize(("file", "content"), BADLY_TYPED, ids=[f"{case[0]}#{index}" for index, case in enumerate(BADLY_TYPED)])
def test_a_badly_typed_field_does_not_break_the_whole_scan(env, config, file, content):
    write(env.home / file[2:], content)
    write(env.home / ".cursor" / "mcp.json", {"mcpServers": {"fine": {"command": "fine"}}})
    listed = scan(env, config)
    assert [item["name"] for item in listed["~/.cursor/mcp.json"]["servers"]] == ["fine"]
    assert listed[file]["found"] is True  # 写坏了的那个文件也列出来（怎么处理那一项都行：说明问题，或者不搬那一项）


# ---- Windows、用户级和项目级 ---------------------------------------------------------


def test_windows_locations_and_backslash_paths(tmp_path, home, work, config):
    appdata = tmp_path / "AppData" / "Roaming"
    env = Env(home=home, workspace=work, platform="win32", environ={"APPDATA": str(appdata)})
    npx = "C:\\Program Files\\nodejs\\npx.cmd"
    args = ["-y", "@modelcontextprotocol/server-filesystem", "C:\\Users\\me\\Desktop", "D:\\data\\", "\\\\server\\share"]
    write(appdata / "Claude" / "claude_desktop_config.json", {"mcpServers": {"fs": {"command": npx, "args": args, "cwd": "C:\\work", "env": {"ROOT": "C:\\Users\\me"}}}})
    write(appdata / "Code" / "User" / "mcp.json", {"servers": {"vs": {"command": "C:\\tools\\vs.exe", "args": ["${workspaceFolder}${/}x"]}}})
    write(appdata / "Block" / "goose" / "config" / "config.yaml", "extensions:\n  gw:\n    type: stdio\n    cmd: C:\\Program Files\\goose\\g.exe\n    args: ['C:\\Users\\me\\a b', \"D:\\\\x\"]\n")
    write(home / ".config" / "goose" / "config.yaml", "extensions: {}\n")  # Linux 的位置：在 Windows 上不看
    found = {source.id: source for source in importers.sources(work, env) if source.found}
    assert set(found) == {"claude-desktop", "vscode-user", "goose-user"}
    assert found["claude-desktop"].path == appdata / "Claude" / "claude_desktop_config.json"
    assert found["vscode-user"].servers["vs"].entry["args"] == [f"{work}\\x"]  # ${/} 在 Windows 上是反斜杠
    assert found["goose-user"].servers["gw"].entry == {"command": "C:\\Program Files\\goose\\g.exe", "args": ["C:\\Users\\me\\a b", "D:\\x"]}
    # 反斜杠一个字节都不变：清单里看到的、导入的、写进 mcp.json 的都一样
    (listed,) = [source for source in importers.preview(list(found.values()), config) if source["id"] == "claude-desktop"]
    summary = items(listed)["fs"]["summary"]
    assert (summary["command"], summary["args"], summary["cwd"]) == (npx, args, "C:\\work")
    assert importers.apply(config, found["claude-desktop"].servers, ["fs"]) == (["fs"], [])
    saved = json.loads(config.path.read_text(encoding="utf-8"))["mcpServers"]["fs"]
    assert (saved["command"], saved["args"], saved["cwd"], saved["env"]) == (npx, args, "C:\\work", {"ROOT": "C:\\Users\\me"})


def test_windows_appdata_falls_back_to_the_home_folder(home, work):
    for environ in ({}, {"APPDATA": "relative\\path"}):  # 没设、不是绝对路径：用 ~/AppData/Roaming
        env = Env(home=home, workspace=work, platform="win32", environ=environ)
        assert env.appdata == home / "AppData" / "Roaming"
        assert env.shown(env.app_data("Claude") / "claude_desktop_config.json") == "~/AppData/Roaming/Claude/claude_desktop_config.json"


def test_mac_desktop_apps_live_in_application_support(home, work):
    env = Env(home=home, workspace=work, platform="darwin")
    shown = {source.id: source.shown for source in importers.sources(work, env)}
    assert shown["claude-desktop"] == "~/Library/Application Support/Claude/claude_desktop_config.json"
    assert shown["vscode-user"] == "~/Library/Application Support/Code/User/mcp.json"
    assert shown["zed-user"] == "~/.config/zed/settings.json"  # Zed 在 macOS 上也用 ~/.config


def test_user_and_project_files_are_both_listed(env, config, work):
    write(env.home / ".cursor" / "mcp.json", {"mcpServers": {"mine": {"command": "a"}}})
    write(work / ".cursor" / "mcp.json", {"mcpServers": {"theirs": {"command": "b"}}})
    cursor = [source for source in importers.preview(importers.sources(work, env), config) if source["tool_id"] == "cursor"]
    assert [(source["id"], source["level"], source["path"]) for source in cursor] == [
        ("cursor-user", "用户级", "~/.cursor/mcp.json"),
        ("cursor-project", "这个项目", ".cursor/mcp.json"),
    ]
    assert [[item["name"] for item in source["servers"]] for source in cursor] == [["mine"], ["theirs"]]


def test_running_in_the_home_folder_lists_each_place_once(home, config):
    write(home / ".cursor" / "mcp.json", {"mcpServers": {"mine": {"command": "a"}}})
    write(home / ".agents" / "skills" / "hello" / "SKILL.md", "---\nname: hello\ndescription: Says hello.\n---\nHi\n")
    env = Env(home=home, workspace=home, platform="linux")
    cursor = [source for source in importers.sources(home, env) if source.tool_id == "cursor"]
    assert [(source.id, source.shown, source.level, source.found) for source in cursor] == [("cursor-user", "~/.cursor/mcp.json", "user", True)]
    for kind in KINDS:
        marks = [(place.path, place.form) for _, place in places(env, kind)]
        assert len(marks) == len(set(marks)), kind
    # 同一个文件读两遍的只有 ~/.claude.json：两种读法（用户级的，和按项目记的那一处）
    paths = [place.path for _, place in places(env, MCP)]
    assert {path for path in paths if paths.count(path) > 1} == {home / ".claude.json"}
    assert [candidate.key for _, candidate in skill_imports.discover(home, env)] == ["~/.agents/skills/hello"]
    looked = {item["id"]: item["looked"] for item in overview(env)}
    assert looked["cursor"]["mcp"] == ["~/.cursor/mcp.json"] and looked["agents"]["skills"] == ["~/.agents/skills"]


def test_registry_is_consistent(env):
    assert len({tool.id for tool in TOOLS}) == len(TOOLS)
    for tool in TOOLS:
        assert tool.docs.startswith("https://") and tool.name
        assert {place.kind for place in tool.where(env)} == set(tool.kinds), tool.id  # 说能导入哪几样，就各有位置
    seen = [(place.path, place.form) for _, place in places(env, SKILLS)]
    assert len(seen) == len(set(seen))
    # 很多工具共用的 .agents/skills 只记在 Agent Skills 下面
    owners = {place.path: tool.id for tool, place in places(env, SKILLS)}
    assert owners[env.home / ".agents" / "skills"] == "agents" and owners[env.workspace / ".agents" / "skills"] == "agents"


def test_shown_paths(home, work, tmp_path):
    env = Env(home=home, workspace=work, platform="linux")
    assert env.shown(home / ".codex" / "config.toml") == "~/.codex/config.toml"
    assert env.shown(work / ".vscode" / "mcp.json") == ".vscode/mcp.json"
    assert env.shown(tmp_path / "elsewhere" / "x.json") == (tmp_path / "elsewhere" / "x.json").as_posix()
    inside = Env(home=home, workspace=home / "project", platform="linux")  # 工作目录在家目录里面：工作目录里的写相对路径
    assert inside.shown(home / "project" / ".mcp.json") == ".mcp.json" and inside.shown(home / ".mcp.json") == "~/.mcp.json"
    same = Env(home=home, workspace=home, platform="linux")
    assert same.shown(home / ".mcp.json") == "~/.mcp.json" and same.shown(home) == "~"


# ---- YAML 的小子集 -------------------------------------------------------------------


def test_yaml_nesting_lists_and_flow_collections():
    text = """# 开头的注释
---
a:
  b:
    c: 1
  d: [x, 'y', "z", 3, [nested], {k: v}]
e: {f: 1, "g h": two, i: [1, 2,]}
items:
- name: first
  value: 1
- name: second
  deeper:
    - - 1
      - 2
- plain
-
empty_flow: []
multi_line_flow: [1,
  2,
  3]
...
"""
    assert yaml_lite.loads(text) == {
        "a": {"b": {"c": 1}, "d": ["x", "y", "z", 3, ["nested"], {"k": "v"}]},
        "e": {"f": 1, "g h": "two", "i": [1, 2]},
        "items": [{"name": "first", "value": 1}, {"name": "second", "deeper": [[1, 2]]}, "plain", None],
        "empty_flow": [],
        "multi_line_flow": [1, 2, 3],
    }


def test_yaml_quotes_escapes_and_comments():
    text = """single: 'it''s # not a comment'
double: "tab\\tnew\\nline \\u00e9 \\x41 \\"q\\""
hash_in_value: http://x.example/#frag
trailing: value # comment
quoted_then_comment: "a" # comment
"quoted key": 1
colon_in_value: http://x.example/a:b
windows: C:\\Program Files\\x.exe
"""
    assert yaml_lite.loads(text) == {
        "single": "it's # not a comment",
        "double": 'tab\tnew\nline é A "q"',
        "hash_in_value": "http://x.example/#frag",
        "trailing": "value",
        "quoted_then_comment": "a",
        "quoted key": 1,
        "colon_in_value": "http://x.example/a:b",
        "windows": "C:\\Program Files\\x.exe",
    }


def test_yaml_block_scalars():
    text = "keep: |\n  line one\n    indented\n  line two\nfolded: >-\n  folded\n  text\n\n  next paragraph\nstrip: |-\n  no newline\nplus: |+\n  kept\n\nlast: end\n"
    assert yaml_lite.loads(text) == {
        "keep": "line one\n  indented\nline two\n",
        "folded": "folded text\nnext paragraph",
        "strip": "no newline",
        "plus": "kept\n\n",
        "last": "end",
    }


def test_yaml_scalars_follow_yaml_1_2():
    text = "t: true\nf: False\nn: null\ntilde: ~\nempty:\ni: 42\nneg: -3\nfl: 1.5\nexp: 1e3\nyes: yes\noff: off\nquoted_number: '1'\nquoted_bool: \"true\"\n"
    assert yaml_lite.loads(text) == {
        "t": True, "f": False, "n": None, "tilde": None, "empty": None, "i": 42, "neg": -3, "fl": 1.5, "exp": 1000.0,
        "yes": "yes", "off": "off",  # YAML 1.2：yes、off 是文字，不是布尔值
        "quoted_number": "1", "quoted_bool": "true",
    }  # fmt: skip
    assert yaml_lite.loads("") is None and yaml_lite.loads("# 只有注释\n") is None


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("a: &anchor 1\nb: *anchor\n", 1),  # 锚点和别名（"十亿笑声"）
        ("a: 1\nb: *alias\n", 2),
        ("a: !!python/object:os.system x\n", 1),  # 标签：有的库会照着它构造任意对象
        ("a: [1, &x 2]\n", 1),
        ("list:\n  - !tag value\n", 2),
        ("? complex\n: key\n", 1),
        ("a:\n\tb: 1\n", 2),  # 制表符缩进
        ("a: 1\nb: 2\na: 3\n", 3),  # 同一个名字写了两次
        ("a: {x: 1, x: 2}\n", 1),
        ("a: 1\nb: \"unterminated\n", 2),
        ("a: 'unterminated\n", 1),
        ("a: \"bad \\q escape\"\n", 1),
        ("a: 1\n---\nb: 2\n", 2),  # 一个文件里好几份文档
        ("a: one\n  two\n", 2),  # 跨行的普通文字
        ("a: 1\n   b: 2\n", 2),
        ("a: [1, 2\n", 1),
        ("a: \"x\" y\n", 1),
    ],
)
def test_yaml_refuses_what_it_does_not_support_with_a_line_number(text, line):
    with pytest.raises(yaml_lite.YamlError) as caught:
        yaml_lite.loads(text)
    assert caught.value.line == line and str(caught.value)


def test_yaml_nesting_has_a_limit():
    deep = "".join("  " * level + f"k{level}:\n" for level in range(yaml_lite.MAX_DEPTH + 5)) + "  " * (yaml_lite.MAX_DEPTH + 5) + "v: 1\n"
    with pytest.raises(yaml_lite.YamlError):
        yaml_lite.loads(deep)
    with pytest.raises(yaml_lite.YamlError):
        yaml_lite.loads("a: " + "[" * 200 + "]" * 200 + "\n")


# ---- 读文件 --------------------------------------------------------------------------


def test_strip_jsonc_keeps_strings_and_line_numbers():
    text = '{\n  // "注释里的引号"\n  "url": "https://a.example/x//y", /* 跨\n行的注释 */ "b": [1, 2,],\n  "s": "a\\"//b",\n  "c": "/* 不是注释 */",\n}\n'
    stripped = readers.strip_jsonc(text)
    assert stripped.count("\n") == text.count("\n")
    assert json.loads(stripped) == {"url": "https://a.example/x//y", "b": [1, 2], "s": 'a"//b', "c": "/* 不是注释 */"}
    assert readers.parse(text, "jsonc") == json.loads(stripped)


def test_readers_explain_what_is_wrong(tmp_path):
    with pytest.raises(readers.ReadError, match="第 4 行"):
        readers.parse('{\n  // c\n  "a": 1\n  "b": 2\n}', "jsonc")
    with pytest.raises(readers.ReadError, match="TOML"):
        readers.parse("a = ", "toml")
    with pytest.raises(readers.ReadError, match="第 2 行"):
        readers.parse("a: 1\nb: *x\n", "yaml")
    assert readers.parse('[mcp_servers.x]\ncommand = "y"\n', "toml") == {"mcp_servers": {"x": {"command": "y"}}}
    path = write(tmp_path / "bom.json", "﻿" + '{"a": 1}')
    assert readers.load(path, "json") == {"a": 1}  # 开头的 BOM 去掉
    with pytest.raises(readers.ReadError, match="太大"):
        readers.read_text(write(tmp_path / "big.json", "x" * 100), limit=10)
    with pytest.raises(readers.ReadError):
        readers.read_text(tmp_path)  # 文件夹
    with pytest.raises(readers.ReadError):
        readers.read_text(tmp_path / "missing.json")


# ---- Skill ---------------------------------------------------------------------------


def skill_text(name: str, description: str = "Does a thing. Use when asked.") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\nBody of {name}.\n"


@pytest.fixture
def skill_dirs(env, work, tmp_path) -> Env:
    """几个工具的 Skill 文件夹里各放一两个。"""
    home = env.home
    write(home / ".agents/skills/shared/SKILL.md", skill_text("shared"))
    write(home / ".codex/skills/cx/SKILL.md", skill_text("cx"))
    write(home / ".codex/skills/.system/sys/SKILL.md", skill_text("sys"))  # Codex 自带的，以点开头：不列出来
    write(work / ".github/skills/gh/SKILL.md", skill_text("gh"))
    write(home / ".cursor/skills/cur/SKILL.md", skill_text("cur"))
    write(home / ".cursor/skills/broken/SKILL.md", "没有 frontmatter")
    write(home / ".cursor/skills/loose.md", "不是文件夹")
    gem = home / ".gemini/skills/gem"
    write(gem / "SKILL.md", skill_text("gem"))
    write(gem / "scripts/run.sh", "echo hi")
    write(gem / ".secret", "隐藏文件")
    os.symlink(write(tmp_path / "outside.txt", "外面的秘密"), gem / "leak.txt")  # 文件夹里的链接：不跟
    return env


def test_skills_are_found_in_many_tools(skill_dirs, work):
    found = skill_imports.discover(work, skill_dirs)
    by_key = {candidate.key: (group, candidate) for group, candidate in found}
    assert set(by_key) == {
        "~/.agents/skills/shared", "~/.codex/skills/cx", ".github/skills/gh",
        "~/.cursor/skills/cur", "~/.cursor/skills/broken", "~/.gemini/skills/gem",
    }  # fmt: skip
    groups = {key: group for key, (group, _) in by_key.items()}
    assert groups["~/.agents/skills/shared"] == "Agent Skills · ~/.agents/skills"
    assert groups["~/.codex/skills/cx"] == "Codex · ~/.codex/skills"
    assert groups[".github/skills/gh"] == "GitHub Copilot · .github/skills"
    assert groups["~/.cursor/skills/cur"] == "Cursor · ~/.cursor/skills"
    assert groups["~/.gemini/skills/gem"] == "Gemini CLI · ~/.gemini/skills"
    assert {key: (candidate.tool, candidate.tool_id) for key, (_, candidate) in by_key.items()}["~/.codex/skills/cx"] == ("Codex", "codex")
    gem = by_key["~/.gemini/skills/gem"][1]
    assert gem.problems == [] and [path for path, _ in gem.listing] == ["SKILL.md", "scripts/run.sh"]
    assert by_key["~/.cursor/skills/broken"][1].problems  # 写坏了的：列出来，说明为什么装不了
    # 总览里的数目：有 SKILL.md 的文件夹（写坏了的也算一个）
    counts = skill_imports.count(skill_dirs)
    assert {tool: number for tool, (number, _) in counts.items() if number} == {"agents": 1, "codex": 1, "copilot": 1, "cursor": 2, "gemini": 1}
    assert sum(number for number, _ in counts.values()) == len(found)
    looked = skill_imports.places_looked(work, skill_dirs)
    assert {"~/.agents/skills", ".agents/skills", "~/.codex/skills", ".github/skills", "~/.cursor/skills", "~/.gemini/skills", "~/.claude/skills"} <= set(looked)


def test_skills_install_as_copies_and_remember_the_tool(skill_dirs, work, tmp_path):
    store = SkillStore(tmp_path / "data")
    installer = Installer(store)
    preview = installer.stage(skill_imports.discover(work, skill_dirs), {"kind": "import", "label": ""})
    assert {candidate["key"]: candidate["tool"] for candidate in preview["candidates"]}["~/.gemini/skills/gem"] == "Gemini CLI"
    results = installer.install(preview["stage"], [
        {"key": "~/.gemini/skills/gem", "name": "gem"},
        {"key": ".github/skills/gh", "name": "gh"},
        {"key": "~/.cursor/skills/broken", "name": "broken"},
    ])  # fmt: skip
    assert [result["ok"] for result in results] == [True, True, False] and results[2]["error"]
    gem = store.root / "gem"
    assert sorted(path.relative_to(gem).as_posix() for path in gem.rglob("*") if path.is_file()) == ["SKILL.md", "scripts/run.sh"]
    assert not (gem / "leak.txt").exists() and not (gem / ".secret").exists()  # 链接和隐藏文件都不要
    assert not any(path.is_symlink() for path in gem.rglob("*"))
    sources = {skill.name: skill.source for skill in store.list()}
    assert {key: sources["gem"][key] for key in ("kind", "tool", "label")} == {"kind": "import", "tool": "Gemini CLI", "label": "~/.gemini/skills/gem"}
    assert {key: sources["gh"][key] for key in ("kind", "tool", "label")} == {"kind": "import", "tool": "GitHub Copilot", "label": ".github/skills/gh"}
    assert (skill_dirs.home / ".gemini/skills/gem/leak.txt").is_symlink()  # 原来的一个都没动


def test_a_linked_skill_folder_is_followed_but_copied(env, work, tmp_path):
    real = tmp_path / "real-skills" / "linked"
    write(real / "SKILL.md", skill_text("linked"))
    (env.home / ".agents" / "skills").mkdir(parents=True)
    os.symlink(real, env.home / ".agents" / "skills" / "linked")  # 这些工具都允许 Skill 文件夹本身是一个链接
    store = SkillStore(tmp_path / "data")
    installer = Installer(store)
    preview = installer.stage(skill_imports.discover(work, env), {"kind": "import", "label": ""})
    (result,) = installer.install(preview["stage"], [{"key": "~/.agents/skills/linked", "name": "linked"}])
    assert result["ok"] and (store.root / "linked").is_dir() and not (store.root / "linked").is_symlink()


# ---- Agent ---------------------------------------------------------------------------


def converted(form: str, text: str, file: str = "agent.md", path: Path | None = None, tool: str = "X", suffixes: tuple[str, ...] = (".md",)) -> tuple[Spec, Converted]:
    """换成智狐的写法，并确认换出来的文件合格（agents/spec.py 的 check）。"""
    result = convert(form, text, file, path or Path("/nowhere") / file, tool, suffixes)
    assert result.problem is None, result.problem
    spec, problems = check_agent(result.text, None)
    assert problems == [], (problems, result.text)
    return spec, result


def change(result: Converted, words: str) -> dict:
    found = [item.values for item in result.changes if words in item.key]
    assert len(found) == 1, (words, [(item.key, item.values) for item in result.changes])
    return found[0]


def problem_of(form: str, text: str, file: str = "agent.md", path: Path | None = None, suffixes: tuple[str, ...] = (".md",)) -> str:
    result = convert(form, text, file, path or Path("/nowhere") / file, "X", suffixes)
    assert result.problem is not None
    return str(result.problem)


def test_translate_tool_names():
    result = translate(["read", "search", "execute", "github/*", "github/create_issue", "@kiro-server", "vscode/getProjectSetupInfo", "nonsense"], "copilot")
    assert result.tools == ["Read", "Bash", "mcp__github", "mcp__github__create_issue", "mcp__kiro-server"]
    assert result.dropped == ["vscode/getProjectSetupInfo", "nonsense"]  # 认不出来的不搬：只会更少，不会更多
    assert translate(["read", "*"], "copilot").tools is None  # "全部"：和叫它的那一方一样多
    assert translate(None, "gemini").tools is None and translate([], "gemini").tools == []
    assert translate(["Read", "Bash", "mcp__x__y"], "gemini").tools == ["Read", "Bash", "mcp__x__y"]  # 已经是智狐的叫法
    assert translate(["mcp_github_search"], "gemini").dropped == ["mcp_github_search"]  # 服务器和工具分不开，不搬


def test_copilot_agent():
    text = "---\nname: Security Auditor\ndescription: Audits code for security issues.\ntools: ['read', 'search', 'edit', 'execute', 'github/*', 'vscode/getProjectSetupInfo']\nmodel: Claude Sonnet 4.5 (copilot)\n---\nYou audit code.\n"
    spec, result = converted("copilot", text, "security-auditor.agent.md", suffixes=(".agent.md", ".md"))
    assert spec.name == "security-auditor"  # 显示的名字有空格：用文件名
    assert spec.tools == ["Read", "Edit", "Bash", "mcp__github"]
    assert spec.model == "" and change(result, "认不出来")["model"] == "Claude Sonnet 4.5 (copilot)"  # 认不出来的模型：和叫它的那一方一样
    assert "github/* → mcp__github" in change(result, "智狐的叫法")["pairs"]
    assert change(result, "智狐没有这些工具")["names"] == "vscode/getProjectSetupInfo"
    assert spec.body == "You audit code."


def test_gemini_and_qwen_agents():
    text = "---\nname: researcher\ndescription: Researches things.\ntools:\n  - read_file\n  - run_shell_command\n  - google_web_search\n  - web_fetch\n  - mcp_github_search\nmodel: gemini-2.5-pro\ntemperature: 0.2\nmax_turns: 10\n---\nResearch.\n"
    spec, result = converted("gemini", text)
    assert spec.tools == ["Read", "Bash", "WebSearch", "WebFetch"] and spec.model == "gemini/gemini-2.5-pro"
    assert change(result, "没有搬过来的字段")["fields"] == "temperature, max_turns"
    assert "远程" in problem_of("gemini", "---\nname: remote\ndescription: x\nkind: remote\nagent_card_url: https://x.example\n---\nx\n")
    qwen = "---\nname: qa\ndescription: QA.\ntools: [read_file, write_file, todo_write, task, skill]\napprovalMode: yolo\n---\nTest.\n"
    spec, result = converted("qwen", qwen, tool="Qwen Code")
    assert spec.tools == ["Read", "Edit", "TodoWrite", "Agent", "Skill"]
    assert change(result, "不用问就调用") == {"tool": "Qwen Code", "fields": "approvalMode"}


def test_cursor_agent():
    spec, result = converted("cursor", "---\ndescription: Reads only\nreadonly: true\nis_background: true\n---\nLook around.\n", "quiet.md", tool="Cursor")
    assert (spec.name, spec.tools) == ("quiet", ["Read", "WebSearch", "WebFetch"])  # 只读的：只能读文件和查资料
    assert change(result, "没有写 name")["name"] == "quiet" and change(result, "没有搬过来的字段")["fields"] == "is_background"
    spec, _ = converted("cursor", "---\nname: worker\ndescription: Does work\n---\nWork.\n", "w.md")
    assert spec.tools is None  # 没写 readonly：和叫它的那一方一样


def test_opencode_agent():
    text = "---\nname: ignored\ndescription: Reviews code\nmode: subagent\nmodel: google/gemini-2.5-pro\ntools:\n  write: false\n  edit: false\n---\nReview it.\n"
    spec, result = converted("opencode", text, "team/reviewer.md", tool="OpenCode")
    assert spec.name == "team-reviewer"  # OpenCode 的名字是文件的路径（子文件夹也算）
    assert "Edit" not in spec.tools and spec.tools == ["Read", "Bash", "WebSearch", "WebFetch", "TodoWrite", "Skill", "Agent"]
    assert spec.model == "gemini/gemini-2.5-pro" and change(result, "model 换成了")["new"] == "gemini/gemini-2.5-pro"
    assert change(result, "不合智狐的规矩") == {"old": "team/reviewer", "new": "team-reviewer"}
    spec, _ = converted("opencode", "---\ndescription: V2\npermissions:\n  - {action: bash, resource: '*', effect: deny}\n---\nx\n", "v2.md")
    assert "Bash" not in spec.tools and "Edit" in spec.tools
    spec, _ = converted("opencode", "---\ndescription: Old\npermission:\n  edit: deny\n---\nx\n", "old.md")
    assert "Edit" not in spec.tools and "Read" in spec.tools
    spec, _ = converted("opencode", "---\ndescription: Free\nmodel: anthropic/claude-sonnet-5-5#high\n---\nx\n", "free.md")
    assert spec.tools is None and spec.model == "anthropic/claude-sonnet-5-5"  # 没有关掉什么：和叫它的那一方一样


def test_factory_droid():
    spec, _ = converted("factory", "---\nname: ro\ndescription: Read only droid\ntools: read-only\nmodel: inherit\n---\nRead.\n")
    assert spec.tools == ["Read"] and spec.model == ""
    spec, _ = converted("factory", "---\nname: ex\ndescription: Execs\ntools: [Read, Execute, FetchUrl, Create]\n---\nRun.\n")
    assert spec.tools == ["Read", "Bash", "WebFetch", "Edit"]


def test_codex_agent_toml():
    text = 'name = "reviewer"\ndescription = "Reviews PRs"\ndeveloper_instructions = """\nReview carefully.\nTwice.\n"""\nmodel = "gpt-6-luna"\nsandbox_mode = "read-only"\n[features]\nshell_tool = false\n'
    spec, result = converted("codex", text, "reviewer.toml", tool="Codex", suffixes=(".toml",))
    assert (spec.name, spec.description, spec.body) == ("reviewer", "Reviews PRs", "Review carefully.\nTwice.")
    assert "Bash" not in spec.tools and "Read" in spec.tools  # 关掉了执行命令，在智狐里也不放开
    assert spec.model == "openai/gpt-6-luna" and change(result, "没有搬过来的字段")["fields"] == "sandbox_mode, features"
    spec, _ = converted("codex", 'description = "x"\ndeveloper_instructions = "Do it."\n', "named-by-file.toml", suffixes=(".toml",))
    assert spec.name == "named-by-file" and spec.tools is None
    assert "developer_instructions" in problem_of("codex", 'name = "x"\ndescription = "y"\n', "x.toml", suffixes=(".toml",))
    assert "TOML" in problem_of("codex", 'name = "broken\n', "x.toml", suffixes=(".toml",))


def test_kiro_json_agent_reads_its_prompt_from_beside_it(tmp_path):
    folder = tmp_path / "agents"
    write(folder / "p.md", "You are Kiro.")
    write(tmp_path / "outside.md", "外面的设定")
    data = {"description": "Kiro agent", "prompt": "file://./p.md", "tools": ["read", "write", "shell", "@github", "@git/status"], "allowedTools": ["read"], "model": "claude-sonnet-5-5"}
    agent = write(folder / "k.json", data)
    spec, result = converted("kiro", agent.read_text(), "k.json", agent, "Kiro", (".json", ".md"))
    assert (spec.name, spec.body, spec.model) == ("k", "You are Kiro.", "anthropic/claude-sonnet-5-5")
    assert spec.tools == ["Read", "Edit", "Bash", "mcp__github", "mcp__git__status"]
    assert change(result, "不用问就调用") == {"tool": "Kiro", "fields": "allowedTools"}  # "不用问就调用"的设置不搬
    assert change(result, "读进来")["file"] == "./p.md"
    for pointer in ("file://../outside.md", f"file://{tmp_path / 'outside.md'}"):  # 文件夹外面的：不读
        escape = write(folder / "escape.json", {"description": "x", "prompt": pointer})
        assert "outside.md" in problem_of("kiro", escape.read_text(), "escape.json", escape, (".json",))
    os.symlink(tmp_path / "outside.md", folder / "sneaky.md")  # 旁边的链接指向外面：也不读
    sneaky = write(folder / "sneaky.json", {"description": "x", "prompt": "file://./sneaky.md"})
    assert "sneaky.md" in problem_of("kiro", sneaky.read_text(), "sneaky.json", sneaky, (".json",))
    missing = write(folder / "missing.json", {"description": "x", "prompt": "file://./nope.md"})
    assert "nope.md" in problem_of("kiro", missing.read_text(), "missing.json", missing, (".json",))
    spec, _ = converted("kiro", "---\nname: md-agent\ndescription: Kiro md\ntools: [read, write]\n---\nBody.\n", "md-agent.md", tool="Kiro", suffixes=(".json", ".md"))
    assert spec.tools == ["Read", "Edit"]


def test_an_unreadable_kiro_tools_field_never_means_all_tools():
    # 只会更少，不会更多：tools 没写或者认不出来，就先一个工具都不放开，并说明一句
    for data in ({"name": "k", "description": "x", "prompt": "p", "tools": "fs_read"}, {"name": "k", "description": "x", "prompt": "p"}):
        spec, result = converted("kiro", json.dumps(data), "k.json", suffixes=(".json",))
        assert spec.tools == [] and any("导入后先不放开任何工具" in item.key for item in result.changes)


def test_amazon_q_agent():
    data = {"name": "q", "description": "Q agent", "prompt": "Be Q.", "tools": ["fs_read", "fs_write", "execute_bash", "@git", "use_aws"],
            "toolsSettings": {"fs_write": {"allowedPaths": ["x"]}}, "mcpServers": {"git": {"command": "git-mcp"}}}  # fmt: skip
    spec, result = converted("amazonq", json.dumps(data), "q.json", tool="Amazon Q Developer", suffixes=(".json",))
    assert spec.tools == ["Read", "Edit", "Bash", "mcp__git"] and change(result, "智狐没有这些工具")["names"] == "use_aws"
    assert change(result, "不用问就调用")["fields"] == "toolsSettings"


def test_claude_code_agents_are_copied_verbatim():
    text = "---\nname: cc\ndescription: Claude Code agent.\ntools: Read, Grep\npermissionMode: acceptEdits\nhooks: x\n---\nCC body.\n"
    spec, result = converted("claude", text)
    assert result.text == text and result.changes == []
    assert spec.extra == ["hooks", "permissionMode"]  # 智狐不用的字段原样留在文件里，界面上标出来


def test_malformed_agent_files_explain_themselves():
    assert "frontmatter" in problem_of("gemini", "no frontmatter at all\n")
    assert "3" in problem_of("gemini", "---\nname: x\n  bad: indent\n---\nx\n")  # 第几行按整个文件数（frontmatter 从第 2 行开始）
    assert "frontmatter" in problem_of("copilot", "---\n- a\n- b\n---\nbody\n")
    assert "JSON" in problem_of("kiro", "{ not json", "k.json", suffixes=(".json",))


@pytest.fixture
def agent_dirs(env, work) -> Env:
    """十种工具的 Agent 文件各放一两个，再加几个写坏了的。"""
    home = env.home
    write(work / ".github/agents/security-auditor.agent.md", "---\nname: Security Auditor\ndescription: Audits code.\ntools: ['read', 'execute', 'github/*']\n---\nAudit.\n")
    write(work / ".github/agents/team/helper.agent.md", "---\ndescription: In a subfolder.\n---\nHelp.\n")  # Copilot 会读子文件夹
    write(home / ".gemini/agents/researcher.md", "---\nname: researcher\ndescription: Researches.\ntools: [read_file, google_web_search]\n---\nResearch.\n")
    write(home / ".gemini/agents/sub/ignored.md", "---\nname: ignored\ndescription: x\n---\nx\n")  # Gemini CLI 不读子文件夹
    write(home / ".gemini/agents/notes.txt", "不是 Agent")
    write(home / ".gemini/agents/broken.md", "---\nname: broken\ndescription: [unclosed\n---\nx\n")
    write(home / ".qwen/agents/qa.md", "---\nname: qa\ndescription: QA.\ntools: [read_file]\n---\nTest.\n")
    write(home / ".cursor/agents/quiet.md", "---\ndescription: Reads only\nreadonly: true\n---\nLook.\n")
    write(work / ".opencode/agents/team/reviewer.md", "---\ndescription: Reviews\ntools:\n  write: false\n---\nReview.\n")
    write(home / ".factory/droids/ro.md", "---\nname: ro\ndescription: Read only droid\ntools: read-only\n---\nRead.\n")
    write(home / ".codex/agents/reviewer.toml", 'name = "reviewer"\ndescription = "Reviews PRs"\ndeveloper_instructions = "Review."\n')
    write(home / ".codex/agents/no-body.toml", 'name = "no-body"\ndescription = "x"\n')
    write(home / ".kiro/agents/k.json", {"description": "Kiro agent", "prompt": "file://./prompts/k.md", "tools": ["read"]})
    write(home / ".kiro/agents/prompts/k.md", "You are Kiro.")
    write(home / ".aws/amazonq/cli-agents/q.json", {"name": "q", "description": "Q", "prompt": "Be Q.", "tools": ["fs_read"]})
    write(home / ".claude/agents/cc.md", "---\nname: cc\ndescription: Claude Code agent.\npermissionMode: acceptEdits\n---\nCC.\n")
    return env


def test_agents_are_found_in_many_tools_and_install_cleanly(agent_dirs, work, tmp_path):
    store = AgentStore(tmp_path / "agents")
    found = {candidate["key"]: candidate for candidate in agent_imports.discover(work, store, agent_dirs)}
    assert set(found) == {
        ".github/agents/security-auditor.agent.md", ".github/agents/team/helper.agent.md",
        "~/.gemini/agents/researcher.md", "~/.gemini/agents/broken.md", "~/.qwen/agents/qa.md", "~/.cursor/agents/quiet.md",
        ".opencode/agents/team/reviewer.md", "~/.factory/droids/ro.md", "~/.codex/agents/reviewer.toml", "~/.codex/agents/no-body.toml",
        "~/.kiro/agents/k.json", "~/.aws/amazonq/cli-agents/q.json", "~/.claude/agents/cc.md",
    }  # fmt: skip
    assert found["~/.gemini/agents/broken.md"]["problems"] and found["~/.codex/agents/no-body.toml"]["problems"]  # 写坏了的照样列出来
    good = {key: candidate for key, candidate in found.items() if not candidate["problems"]}
    assert len(good) == 11 and not any(candidate["bad_name"] for candidate in good.values())
    sample = found["~/.codex/agents/reviewer.toml"]
    assert (sample["tool"], sample["tool_id"], sample["place"], sample["file"], sample["group"]) == ("Codex", "codex", "~/.codex/agents", "reviewer.toml", "Codex · ~/.codex/agents")
    # 名字来自文件的（OpenCode、没写 name 的 Copilot）：子文件夹也算进名字里
    assert found[".opencode/agents/team/reviewer.md"]["name"] == "team-reviewer" and found[".github/agents/team/helper.agent.md"]["name"] == "team-helper"
    assert found["~/.claude/agents/cc.md"]["extra"] == ["permissionMode"] and found["~/.claude/agents/cc.md"]["changes"] == []
    # 数目：每个工具几个文件（写坏了的也算）
    assert agent_imports.count(agent_dirs) == {
        "claude-code": (1, 0), "cursor": (1, 0), "copilot": (2, 0), "codex": (2, 0), "gemini": (2, 0),
        "opencode": (1, 0), "qwen": (1, 0), "kiro": (1, 0), "amazon-q": (1, 0), "factory": (1, 0),
    }  # fmt: skip

    results = agent_imports.import_picked(work, store, [{"key": key, "name": candidate["name"]} for key, candidate in good.items()], agent_dirs)
    assert all(result["ok"] for result in results), results
    installed = {agent.name: agent for agent in store.list()}
    assert set(installed) == {candidate["name"] for candidate in good.values()}
    assert all(not agent.problems for agent in installed.values())  # 装进来的每一个都合格
    assert installed["k"].spec.body == "You are Kiro." and installed["q"].spec.tools == ["Read"]
    copied = store.root / "cc.md"
    assert copied.read_text(encoding="utf-8") == (agent_dirs.home / ".claude/agents/cc.md").read_text(encoding="utf-8")  # Claude Code 的原样复制
    assert not any(path.is_symlink() for path in store.root.iterdir())
    (failed,) = agent_imports.import_picked(work, store, [{"key": "~/.gemini/agents/broken.md", "name": "broken"}], agent_dirs)
    assert failed["ok"] is False and failed["error"]


def test_agent_name_conflicts_rename_or_replace(agent_dirs, work, tmp_path):
    store = AgentStore(tmp_path / "agents")
    store.install("---\nname: reviewer\ndescription: 已经有的那个\n---\n原来的。\n")
    found = {candidate["key"]: candidate for candidate in agent_imports.discover(work, store, agent_dirs)}
    key = "~/.codex/agents/reviewer.toml"
    assert found[key]["exists"] is True and found["~/.qwen/agents/qa.md"]["exists"] is False
    (result,) = agent_imports.import_picked(work, store, [{"key": key, "name": "reviewer"}], agent_dirs)
    assert result["ok"] is False and "reviewer" in result["error"]
    assert store.get("reviewer").spec.description == "已经有的那个"
    (result,) = agent_imports.import_picked(work, store, [{"key": key, "name": "codex-reviewer"}], agent_dirs)
    assert result["ok"] and store.get("codex-reviewer").spec.name == "codex-reviewer" and not store.get("codex-reviewer").problems
    (result,) = agent_imports.import_picked(work, store, [{"key": key, "name": "reviewer", "replace": True}], agent_dirs)
    assert result["ok"] and store.get("reviewer").spec.description == "Reviews PRs"
    results = agent_imports.import_picked(work, store, [{"key": "~/.nowhere/x.md", "name": "x"}, {"key": key, "name": "bad name"}], agent_dirs)
    assert [result["ok"] for result in results] == [False, False]


def test_a_badly_typed_agent_field_does_not_break_the_whole_scan(env, work, tmp_path):
    write(env.home / ".config/opencode/agents/odd.md", "---\ndescription: x\npermissions: 5\n---\nbody\n")
    write(env.home / ".claude/agents/fine.md", "---\nname: fine\ndescription: ok\n---\nok\n")
    found = {candidate["key"] for candidate in agent_imports.discover(work, AgentStore(tmp_path / "agents"), env)}
    assert {"~/.claude/agents/fine.md", "~/.config/opencode/agents/odd.md"} <= found


# ---- 总览 ---------------------------------------------------------------------------


def test_overview_lists_every_tool(env, work):
    write(env.home / ".cursor/mcp.json", {"mcpServers": {"a": {"command": "a"}, "b": {"command": "b"}}})
    write(env.home / ".cursor/skills/s/SKILL.md", skill_text("s"))
    write(env.home / ".cursor/agents/x.md", "---\ndescription: x\n---\nx\n")
    write(env.home / ".kiro/settings/mcp.json", "{ broken")  # 只有一个写坏了的文件
    listed = overview(env)
    assert [item["id"] for item in listed] == [tool.id for tool in TOOLS]
    by_id = {item["id"]: item for item in listed}
    for tool in TOOLS:
        item = by_id[tool.id]
        assert (item["name"], item["docs"], item["kinds"]) == (tool.name, tool.docs, list(tool.kinds))
        assert set(item["counts"]) == set(tool.kinds) and set(item["looked"]) == set(tool.kinds)
    assert by_id["cursor"]["counts"] == {"mcp": 2, "skills": 1, "agents": 1} and by_id["cursor"]["found"] is True
    assert (by_id["kiro"]["found"], by_id["kiro"]["problems"], by_id["kiro"]["counts"]["mcp"]) == (True, 1, 0)
    assert by_id["zed"]["found"] is False and by_id["zed"]["problems"] == 0
    assert by_id["cursor"]["looked"]["mcp"] == ["~/.cursor/mcp.json", ".cursor/mcp.json"]
    assert "~/.continue/mcpServers/" in by_id["continue"]["looked"]["mcp"]  # 文件夹后面带一个 /
    assert by_id["agents"]["blurb"] and by_id["agents"]["kinds"] == [SKILLS]
    # 正在看的那一页已经数过的，不再数一遍
    known = overview(env, {MCP: {"cursor": (7, 0)}, AGENTS: {}})
    known_cursor = {item["id"]: item for item in known}["cursor"]
    assert known_cursor["counts"] == {"mcp": 7, "skills": 1, "agents": 0}


# ---- 网页上的接口 --------------------------------------------------------------------

WEB_SECRET = "web-secret-value-0123456789abcdef"
WEB_SECRET_2 = "codex-web-secret-0123456789abcd"


def tools_fixture(home: Path) -> None:
    """Cursor 和 Codex 各有 MCP 服务器、Skill 和 Agent。服务器先关着，导入后不会真的启动。"""
    write(home / ".cursor/mcp.json", {"mcpServers": {"gh": {"command": "npx", "args": ["-y", "server-github"], "env": {"GITHUB_TOKEN": WEB_SECRET}, "disabled": True}}})
    write(home / ".codex/config.toml", f'[mcp_servers.ctx]\ncommand = "npx"\nargs = ["-y", "ctx"]\nenv = {{ API = "{WEB_SECRET_2}" }}\nenabled = false\n')
    write(home / ".cursor/skills/cur/SKILL.md", skill_text("cur"))
    write(home / ".codex/skills/cx/SKILL.md", skill_text("cx"))
    write(home / ".cursor/agents/quiet.md", "---\ndescription: Reads only\nreadonly: true\n---\nLook.\n")
    write(home / ".codex/agents/reviewer.toml", 'name = "reviewer"\ndescription = "Reviews PRs"\ndeveloper_instructions = "Review."\n')


def by_id(listed: list[dict]) -> dict[str, dict]:
    return {item["id"]: item for item in listed}


def test_web_mcp_scan_and_import(serve):
    s = serve()
    tools_fixture(Path.home())
    status, body = s.browser.post("/api/mcp/scan", {})
    assert status == 200 and body["ok"] is True
    sources = by_id(body["sources"])
    assert [item["name"] for item in sources["cursor-user"]["servers"]] == ["gh"] and sources["cursor-user"]["path"] == "~/.cursor/mcp.json"
    assert [item["name"] for item in sources["codex-user"]["servers"]] == ["ctx"]
    tools = by_id(body["tools"])
    assert tools["cursor"]["counts"] == {"mcp": 1, "skills": 1, "agents": 1}
    assert tools["codex"]["counts"] == {"mcp": 1, "skills": 1, "agents": 1}
    assert len(body["tools"]) == len(TOOLS)
    dumped = json.dumps(body, ensure_ascii=False)
    assert WEB_SECRET[:-4] not in dumped and WEB_SECRET_2[:-4] not in dumped

    status, body = s.browser.post("/api/mcp/import", {"picks": [{"source": "cursor-user", "name": "gh"}, {"source": "codex-user", "name": "ctx"}]})
    assert status == 200 and body["added"] == ["gh", "ctx"] and body["skipped"] == []
    saved = json.loads(s.config.mcp_file.read_text(encoding="utf-8"))["mcpServers"]
    assert saved["gh"]["env"] == {"GITHUB_TOKEN": WEB_SECRET} and saved["gh"]["enabled"] is False  # 导入的是完整的配置
    assert saved["ctx"]["env"] == {"API": WEB_SECRET_2}


def test_web_skill_scan_and_install(serve):
    s = serve()
    tools_fixture(Path.home())
    status, preview = s.browser.post("/api/skill/scan", {})
    assert status == 200 and preview["source"]["kind"] == "import"
    found = {candidate["key"]: candidate for candidate in preview["candidates"]}
    assert set(found) == {"~/.cursor/skills/cur", "~/.codex/skills/cx"}
    assert (found["~/.cursor/skills/cur"]["tool"], found["~/.cursor/skills/cur"]["group"]) == ("Cursor", "Cursor · ~/.cursor/skills")
    assert by_id(preview["tools"])["codex"]["counts"]["skills"] == 1 and "~/.codex/skills" in preview["places"]
    status, body = s.browser.post("/api/skill/install", {"stage": preview["stage"], "picks": [{"key": "~/.codex/skills/cx", "name": "cx"}]})
    assert status == 200 and body["results"][0]["ok"]
    (skill,) = s.app.skills.list()
    assert (skill.name, skill.source["kind"], skill.source["tool"], skill.source["label"]) == ("cx", "import", "Codex", "~/.codex/skills/cx")
    assert WEB_SECRET[:-4] not in json.dumps(preview, ensure_ascii=False)


def test_web_agent_scan_and_import(serve):
    s = serve()
    tools_fixture(Path.home())
    status, body = s.browser.post("/api/agent/scan", {})
    assert status == 200
    found = {candidate["key"]: candidate for candidate in body["candidates"]}
    assert set(found) == {"~/.cursor/agents/quiet.md", "~/.codex/agents/reviewer.toml"}
    assert found["~/.cursor/agents/quiet.md"]["changes"] and found["~/.codex/agents/reviewer.toml"]["tool"] == "Codex"
    assert by_id(body["tools"])["cursor"]["counts"]["agents"] == 1 and "~/.codex/agents" in body["places"]
    assert "places" not in s.browser.post("/api/agent/options")[1]  # 去过的地方只在 scan 里给
    picks = [{"key": key, "name": candidate["name"]} for key, candidate in found.items()]
    status, body = s.browser.post("/api/agent/import", {"picks": picks})
    assert status == 200 and sorted(result["name"] for result in body["results"]) == ["quiet", "reviewer"]
    assert sorted(agent.name for agent in s.app.agents.list()) == ["quiet", "reviewer"]
    assert s.app.agents.get("quiet").spec.tools == ["Read", "WebSearch", "WebFetch"]


def test_web_skill_counts_are_the_same_on_every_import_page(serve):
    s = serve()
    write(Path.home() / ".cursor/skills/cur/SKILL.md", skill_text("cur"))
    (Path.home() / ".cursor/skills/not-a-skill").mkdir()
    on_skill_page = by_id(s.browser.post("/api/skill/scan", {})[1]["tools"])["cursor"]["counts"]["skills"]
    on_mcp_page = by_id(s.browser.post("/api/mcp/scan", {})[1]["tools"])["cursor"]["counts"]["skills"]
    assert on_skill_page == on_mcp_page


def test_pasting_a_model_key_reference_is_refused_on_the_web(serve):
    s = serve()
    pasted = json.dumps({"mcpServers": {"bad": {"command": "x", "env": {"K": "${ANTHROPIC_API_KEY}"}}}})
    status, body = s.browser.post("/api/mcp/paste", {"text": pasted, "names": None})
    assert status == 200 and MODEL_KEY in body["servers"][0]["problem"] and body["servers"][0]["target"] == ""
    status, body = s.browser.post("/api/mcp/paste", {"text": pasted, "names": ["bad"]})
    assert status == 200 and body["added"] == [] and len(body["skipped"]) == 1
    assert not s.config.mcp_file.exists()
