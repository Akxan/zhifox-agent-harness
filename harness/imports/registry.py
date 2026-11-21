"""登记表：智狐认得哪些工具，它们把 MCP 服务器、Skill、Agent 放在哪儿，用什么写法。

一个工具一项：给人看的名字（产品名，不翻译）、官方文档、能导入哪几样，和一个"按这台电脑算出位置"的函数。
加一个新工具：在下面写一个这样的函数，加进 TOOLS；它的写法要是和已有的都不一样，再去 mcp_shapes.py
或 agent_formats.py 加一种读法。位置只写各家官方文档里写明的（文档没写、只在源码里的，在旁边注明）。

一处位置（Place）是：
  kind    哪一样：MCP、SKILLS、AGENTS
  path    文件（MCP）或者文件夹（Skill、Agent；Continue 的 .continue/mcpServers/ 也是文件夹）
  level   USER（你自己的，所有项目都用）、PROJECT（这个工作目录里的）、LOCAL（这个工作目录的，但记在你自己的配置里）
  form    怎么读：MCP 是 mcp_shapes.SHAPES 里的一种写法；Agent 是 agent_formats.FORMATS 里的一种；Skill 都一样
  reader  MCP 的文件格式：json、jsonc、toml、yaml（文件夹里按每个文件的扩展名定）
  suffixes Agent 文件的扩展名；deep：Agent 的文件夹里再往下看一层（文档说会读子文件夹的那几家）

好几个工具会读同一个地方（.agents/skills 是十几个工具共用的），这样的位置只登记一次，记在最先列出它的那一项下面；
在家目录里运行时，项目级和用户级是同一个文件夹，也只算一次（见 places）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..i18n import k
from .places import Env

MCP, SKILLS, AGENTS = "mcp", "skills", "agents"
KINDS = (MCP, SKILLS, AGENTS)
USER, PROJECT, LOCAL = "user", "project", "local"


@dataclass(frozen=True)
class Place:
    kind: str
    path: Path
    level: str
    form: str = ""
    reader: str = "json"
    suffixes: tuple[str, ...] = (".md",)
    deep: bool = False
    folder: bool = False  # MCP：path 是一个文件夹，里面每个配置文件各算一处
    id: str = ""  # MCP 来源的编号（页面导入时送回来的就是它）。不写的话按工具和 level 起


@dataclass(frozen=True)
class Tool:
    id: str
    name: str  # 产品名，任何语言下都这样写
    docs: str  # 官方文档：页面"没找到的"里给一个链接
    kinds: tuple[str, ...]
    where: Callable[[Env], list[Place]]
    blurb: str = ""  # 一句说明（k() 标出来的原文），没有就不写


def _mcp(path: Path, level: str, form: str, reader: str = "json", **more) -> Place:
    return Place(MCP, path, level, form, reader, **more)


def _skills(path: Path, level: str) -> Place:
    return Place(SKILLS, path, level)


def _agents(path: Path, level: str, form: str, suffixes: tuple[str, ...] = (".md",), deep: bool = False) -> Place:
    return Place(AGENTS, path, level, form, suffixes=suffixes, deep=deep)


def _pair(env: Env, folder: str, *rest: str) -> tuple[Path, Path]:
    """同一个相对位置的用户级和项目级：~/.cursor/mcp.json 和 <工作目录>/.cursor/mcp.json。"""
    return env.home.joinpath(folder, *rest), env.workspace.joinpath(folder, *rest)


# ---- 一个工具一个函数 ------------------------------------------------------------


def claude_code(env: Env) -> list[Place]:
    # https://code.claude.com/docs/en/mcp 、https://code.claude.com/docs/en/skills 、https://code.claude.com/docs/en/sub-agents
    home, work = env.home, env.workspace
    return [
        _mcp(home / ".claude.json", USER, "claude", id="claude-code-user"),
        _mcp(home / ".claude.json", LOCAL, "claude-local", id="claude-code-local"),
        _mcp(work / ".mcp.json", PROJECT, "claude", id="claude-code-project"),
        *(_skills(path, level) for path, level in zip(_pair(env, ".claude", "skills"), (USER, PROJECT), strict=True)),
        *(_agents(path, level, "claude", deep=True) for path, level in zip(_pair(env, ".claude", "agents"), (USER, PROJECT), strict=True)),
    ]


def claude_desktop(env: Env) -> list[Place]:
    # https://modelcontextprotocol.io/docs/develop/connect-local-servers
    return [_mcp(env.app_data("Claude") / "claude_desktop_config.json", USER, "claude", id="claude-desktop")]


def _common(env: Env, folder: str, *, mcp: tuple[str, ...] | None, shape: str = "claude", agents: str | None = None, skills: bool = True,
            suffixes: tuple[str, ...] = (".md",), deep: bool = False, reader: str = "json") -> list[Place]:  # fmt: skip
    """很多工具都是这样：~/.<名字>/ 和项目里的 .<名字>/ 下面各有一份 MCP 配置、skills/、agents/。"""
    found: list[Place] = []
    levels = (USER, PROJECT)
    if mcp is not None:
        found += [_mcp(path, level, shape, reader) for path, level in zip(_pair(env, folder, *mcp), levels, strict=True)]
    if skills:
        found += [_skills(path, level) for path, level in zip(_pair(env, folder, "skills"), levels, strict=True)]
    if agents:
        found += [_agents(path, level, agents, suffixes, deep) for path, level in zip(_pair(env, folder, "agents"), levels, strict=True)]
    return found


def cursor(env: Env) -> list[Place]:
    # https://cursor.com/docs/context/mcp 、https://cursor.com/docs/context/skills 、https://cursor.com/docs/subagents
    return _common(env, ".cursor", mcp=("mcp.json",), shape="cursor", agents="cursor")


def vscode(env: Env) -> list[Place]:
    # https://code.visualstudio.com/docs/agents/reference/mcp-configuration （用户级的 mcp.json 和 User 文件夹的位置：
    # https://code.visualstudio.com/docs/configure/profiles ；以前写在 settings.json 的 "mcp" 里，1.102 起搬进 mcp.json）
    user = env.app_data("Code") / "User"
    return [
        _mcp(user / "mcp.json", USER, "vscode", "jsonc"),
        _mcp(env.app_data("Code - Insiders") / "User" / "mcp.json", USER, "vscode", "jsonc"),
        _mcp(user / "settings.json", USER, "vscode-settings", "jsonc"),
        _mcp(env.workspace / ".vscode" / "mcp.json", PROJECT, "vscode", "jsonc"),
    ]


def copilot(env: Env) -> list[Place]:
    # https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers
    # https://docs.github.com/en/copilot/concepts/agents/about-agent-skills 、https://docs.github.com/en/copilot/reference/custom-agents-configuration
    # 项目里的 .mcp.json 和 Claude Code 是同一个文件，记在 Claude Code 下面
    home = env.variable("COPILOT_HOME") or env.home / ".copilot"
    return [
        _mcp(home / "mcp-config.json", USER, "claude"),
        _skills(home / "skills", USER),
        _skills(env.workspace / ".github" / "skills", PROJECT),
        _agents(home / "agents", USER, "copilot", (".agent.md", ".md"), deep=True),
        _agents(env.workspace / ".github" / "agents", PROJECT, "copilot", (".agent.md", ".md"), deep=True),
    ]


def windsurf(env: Env) -> list[Place]:
    # https://docs.devin.ai/windsurf/plugins/cascade/mcp （Windsurf 现在归 Devin，配置也搬到了 devin 的文件夹，旧的位置仍然认）
    # https://docs.devin.ai/desktop/cascade/skills
    devin = env.appdata / "devin" if env.windows else env.xdg_config / "devin"
    return [
        _mcp(env.home / ".codeium" / "windsurf" / "mcp_config.json", USER, "cursor"),
        _mcp(devin / "mcp_config.json", USER, "cursor"),
        _mcp(env.workspace / ".devin" / "mcp_config.json", PROJECT, "cursor"),
        _skills(devin / "skills", USER),
        _skills(env.workspace / ".windsurf" / "skills", PROJECT),
        _skills(env.workspace / ".devin" / "skills", PROJECT),
    ]


def cline(env: Env) -> list[Place]:
    # https://docs.cline.bot/mcp/mcp-overview （4.0 起和命令行版共用 ~/.cline 里的文件；以前的两个位置见源码
    # apps/vscode/src/hosts/vscode/mcp-settings-legacy-migration.ts）
    home = env.variable("CLINE_DIR") or env.home / ".cline"
    return [
        _mcp(home / "data" / "settings" / "cline_mcp_settings.json", USER, "cline"),
        _mcp(env.app_data("Code") / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json", USER, "cline"),
        _mcp(env.home / "Documents" / "Cline" / "MCP" / "cline_mcp_settings.json", USER, "cline"),
    ]


def roo(env: Env) -> list[Place]:
    # https://roocodeinc.github.io/Roo-Code/features/mcp/using-mcp-in-roo （全局文件的位置见源码 src/services/mcp/McpHub.ts）
    # https://roocodeinc.github.io/Roo-Code/features/skills
    return [
        _mcp(env.app_data("Code") / "User" / "globalStorage" / "rooveterinaryinc.roo-cline" / "settings" / "mcp_settings.json", USER, "roo"),
        _mcp(env.workspace / ".roo" / "mcp.json", PROJECT, "roo"),
        *(_skills(path, level) for path, level in zip(_pair(env, ".roo", "skills"), (USER, PROJECT), strict=True)),
    ]


def continue_dev(env: Env) -> list[Place]:
    # https://docs.continue.dev/customize/deep-dives/mcp （~/.continue/mcpServers/ 见源码 loadJsonMcpConfigs.ts）
    home = env.home / ".continue"
    return [
        _mcp(home / "config.yaml", USER, "continue", "yaml"),
        _mcp(home / "config.json", USER, "continue", "jsonc"),
        _mcp(home / "mcpServers", USER, "continue", "auto", folder=True),
        _mcp(env.workspace / ".continue" / "mcpServers", PROJECT, "continue", "auto", folder=True),
    ]


def zed(env: Env) -> list[Place]:
    # https://zed.dev/docs/ai/mcp （settings.json 在各系统上的位置见源码 crates/paths/src/paths.rs）
    user = env.appdata / "Zed" if env.windows else (env.home / ".config" / "zed" if env.mac else env.xdg_config / "zed")
    return [_mcp(user / "settings.json", USER, "zed", "jsonc"), _mcp(env.workspace / ".zed" / "settings.json", PROJECT, "zed", "jsonc")]


def gemini(env: Env) -> list[Place]:
    # https://geminicli.com/docs/tools/mcp-server/ 、https://geminicli.com/docs/cli/skills/ 、https://geminicli.com/docs/core/subagents/
    return _common(env, ".gemini", mcp=("settings.json",), shape="gemini", agents="gemini")


def qwen(env: Env) -> list[Place]:
    # https://qwenlm.github.io/qwen-code-docs/en/users/features/mcp/ 、…/skills/ 、…/sub-agents/
    return _common(env, ".qwen", mcp=("settings.json",), shape="gemini", agents="qwen")


def codex(env: Env) -> list[Place]:
    # https://learn.chatgpt.com/docs/extend/mcp 、https://learn.chatgpt.com/docs/agent-configuration/subagents
    # Skill：文档写的是 .agents/skills（记在 Agent Skills 下面）；~/.codex/skills 是旧的位置，源码里仍然读
    # （codex-rs/ext/skills/src/host_roots.rs），里面的 .system 是 Codex 自带的，以点开头，不会列出来
    home = env.variable("CODEX_HOME") or env.home / ".codex"
    work = env.workspace / ".codex"
    return [
        _mcp(home / "config.toml", USER, "codex", "toml"),
        _mcp(work / "config.toml", PROJECT, "codex", "toml"),
        _skills(home / "skills", USER),
        _skills(work / "skills", PROJECT),
        _agents(home / "agents", USER, "codex", (".toml",)),
        _agents(work / "agents", PROJECT, "codex", (".toml",)),
    ]


def opencode(env: Env) -> list[Place]:
    # https://opencode.ai/docs/mcp-servers/ 、https://opencode.ai/docs/config/ 、https://opencode.ai/docs/skills/ 、https://opencode.ai/docs/agents/
    user = (env.home / ".config" if env.windows or env.mac else env.xdg_config) / "opencode"
    work = env.workspace
    places = [_mcp(folder / name, level, "opencode", "jsonc") for folder, level in ((user, USER), (work, PROJECT), (work / ".opencode", PROJECT)) for name in ("opencode.json", "opencode.jsonc")]
    places += [_skills(user / "skills", USER), _skills(work / ".opencode" / "skills", PROJECT)]
    for name in ("agents", "agent"):  # agent/ 是旧的写法，新版照样读
        places += [_agents(user / name, USER, "opencode", deep=True), _agents(work / ".opencode" / name, PROJECT, "opencode", deep=True)]
    return places


def goose(env: Env) -> list[Place]:
    # https://goose-docs.ai/docs/guides/config-files 、https://goose-docs.ai/docs/guides/context-engineering/using-skills
    config = env.appdata / "Block" / "goose" / "config" if env.windows else env.xdg_config / "goose"
    return [_mcp(config / "config.yaml", USER, "goose", "yaml"), _skills(env.workspace / ".goose" / "skills", PROJECT)]


def lm_studio(env: Env) -> list[Place]:
    # https://lmstudio.ai/docs/app/mcp （"和 Cursor 的 mcp.json 一样"；文件的位置写在 https://lmstudio.ai/blog/mcp ）
    return [_mcp(env.home / ".lmstudio" / "mcp.json", USER, "cursor")]


def kiro(env: Env) -> list[Place]:
    # https://kiro.dev/docs/mcp/configuration/ 、https://kiro.dev/docs/skills/ 、https://kiro.dev/docs/custom-agents/
    return _common(env, ".kiro", mcp=("settings", "mcp.json"), agents="kiro", suffixes=(".json", ".md"))


def amazon_q(env: Env) -> list[Place]:
    # https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/command-line-mcp-configuration.html
    # https://aws.github.io/amazon-q-developer-cli/agent-format.html
    home, work = env.home / ".aws" / "amazonq", env.workspace / ".amazonq"
    return [
        _mcp(home / "mcp.json", USER, "claude"),
        _mcp(work / "mcp.json", PROJECT, "claude"),
        _agents(home / "cli-agents", USER, "amazonq", (".json",)),
        _agents(work / "cli-agents", PROJECT, "amazonq", (".json",)),
    ]


def junie(env: Env) -> list[Place]:
    # https://junie.jetbrains.com/docs/junie-cli-mcp-configuration.html 、https://junie.jetbrains.com/docs/agent-skills.html
    return _common(env, ".junie", mcp=("mcp", "mcp.json"))


def warp(env: Env) -> list[Place]:
    # https://docs.warp.dev/agents/capabilities/mcp （也读几个工具共用的 .agents/.mcp.json）
    return [
        *(_mcp(path, level, "claude") for path, level in zip(_pair(env, ".warp", ".mcp.json"), (USER, PROJECT), strict=True)),
        *(_mcp(path, level, "claude") for path, level in zip(_pair(env, ".agents", ".mcp.json"), (USER, PROJECT), strict=True)),
    ]


def factory(env: Env) -> list[Place]:
    # https://docs.factory.com/cli/configuration/skills 、https://docs.factory.com/cli/configuration/custom-droids
    found = _common(env, ".factory", mcp=None)
    return found + [_agents(path, level, "factory") for path, level in zip(_pair(env, ".factory", "droids"), (USER, PROJECT), strict=True)]


def amp(env: Env) -> list[Place]:
    # https://ampcode.com/docs/customize/skills
    config = env.home / ".config"
    return [_skills(config / "amp" / "skills", USER), _skills(config / "agents" / "skills", USER)]


def shared_agents(env: Env) -> list[Place]:
    # https://agentskills.io/client-implementation/adding-skills-support ：.agents/skills 是各家约好共用的位置
    return [_skills(path, level) for path, level in zip(_pair(env, ".agents", "skills"), (USER, PROJECT), strict=True)]


TOOLS: tuple[Tool, ...] = (
    Tool("claude-code", "Claude Code", "https://code.claude.com/docs/en/mcp", KINDS, claude_code),
    Tool("claude-desktop", "Claude Desktop", "https://modelcontextprotocol.io/docs/develop/connect-local-servers", (MCP,), claude_desktop),
    Tool("cursor", "Cursor", "https://cursor.com/docs/context/mcp", KINDS, cursor),
    Tool("vscode", "VS Code", "https://code.visualstudio.com/docs/agents/reference/mcp-configuration", (MCP,), vscode),
    Tool("copilot", "GitHub Copilot", "https://docs.github.com/en/copilot/reference/custom-agents-configuration", KINDS, copilot),
    Tool("codex", "Codex", "https://learn.chatgpt.com/docs/extend/mcp", KINDS, codex),
    Tool("gemini", "Gemini CLI", "https://geminicli.com/docs/tools/mcp-server/", KINDS, gemini),
    Tool(
        "agents", "Agent Skills", "https://agentskills.io/client-implementation/adding-skills-support", (SKILLS,), shared_agents,
        blurb=k("很多工具共用的位置（Codex、Cursor、Gemini CLI、GitHub Copilot、OpenCode、Goose、Amp……都读这里）"),
    ),
    Tool("windsurf", "Windsurf", "https://docs.devin.ai/windsurf/plugins/cascade/mcp", (MCP, SKILLS), windsurf),
    Tool("cline", "Cline", "https://docs.cline.bot/mcp/mcp-overview", (MCP,), cline),
    Tool("roo", "Roo Code", "https://roocodeinc.github.io/Roo-Code/features/mcp/using-mcp-in-roo", (MCP, SKILLS), roo),
    Tool("continue", "Continue", "https://docs.continue.dev/customize/deep-dives/mcp", (MCP,), continue_dev),
    Tool("zed", "Zed", "https://zed.dev/docs/ai/mcp", (MCP,), zed),
    Tool("opencode", "OpenCode", "https://opencode.ai/docs/mcp-servers/", KINDS, opencode),
    Tool("goose", "Goose", "https://goose-docs.ai/docs/guides/config-files", (MCP, SKILLS), goose),
    Tool("qwen", "Qwen Code", "https://qwenlm.github.io/qwen-code-docs/en/users/features/mcp/", KINDS, qwen),
    Tool("kiro", "Kiro", "https://kiro.dev/docs/mcp/configuration/", KINDS, kiro),
    Tool("amazon-q", "Amazon Q Developer", "https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/command-line-mcp-configuration.html", (MCP, AGENTS), amazon_q),
    Tool("junie", "Junie", "https://junie.jetbrains.com/docs/junie-cli-mcp-configuration.html", (MCP, SKILLS), junie),
    Tool("factory", "Factory", "https://docs.factory.com/cli/configuration/custom-droids", (SKILLS, AGENTS), factory),
    Tool("amp", "Amp", "https://ampcode.com/docs/customize/skills", (SKILLS,), amp),
    Tool("warp", "Warp", "https://docs.warp.dev/agents/capabilities/mcp", (MCP,), warp),
    Tool("lm-studio", "LM Studio", "https://lmstudio.ai/docs/app/mcp", (MCP,), lm_studio),
)

LEVELS = {USER: k("用户级"), PROJECT: k("这个项目"), LOCAL: k("这个项目（记在用户配置里）")}


def places(env: Env, kind: str) -> list[tuple[Tool, Place]]:
    """某一样（MCP、Skill、Agent）在这台电脑上要去看的全部位置，按登记的顺序，同一个位置只出现一次。"""
    seen: set[tuple] = set()
    found = []
    for tool in TOOLS:
        for place in tool.where(env):
            if place.kind != kind:
                continue
            try:
                resolved = place.path.resolve()
            except (OSError, RuntimeError):
                resolved = place.path
            mark = (resolved, place.form)
            if mark in seen:
                continue
            seen.add(mark)
            found.append((tool, place))
    return found


def tool(tool_id: str) -> Tool | None:
    return next((item for item in TOOLS if item.id == tool_id), None)
