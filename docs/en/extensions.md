**English** | [简体中文](../zh-CN/extensions.md)

[ZhiFox](../../README.md) › [Documentation](../../README.md#documentation) › Extensions: MCP, skills and custom agents

# Extensions: MCP, skills and custom agents

## MCP: tools written by someone else

The tools in [Three kinds of tools](tools-and-permissions.md#three-kinds-of-tools) are part of this program. [MCP](https://modelcontextprotocol.io) (Model Context Protocol) is an open
protocol for everything else: anyone can package a set of tools (GitHub, a database, a browser…) as an *MCP server*, and
any assistant that speaks MCP, a *client*, can connect to it and hand those tools to its model. ZhiFox is a client,
written by hand in [harness/mcp/](../../harness/mcp/) without the official SDK, one file per concern:

| File | What it does |
|---|---|
| [jsonrpc.py](../../harness/mcp/jsonrpc.py) | The envelope: requests, replies and notifications (JSON-RPC 2.0), and waiting for a reply with a timeout |
| [stdio.py](../../harness/mcp/stdio.py) | Local servers: start the program, one JSON message per line on its stdin and stdout, keep its stderr as a log |
| [http.py](../../harness/mcp/http.py) | Remote servers: every message is an HTTP POST, and the reply is JSON or a stream of server-sent events |
| [client.py](../../harness/mcp/client.py) | The protocol itself: find out which generation the server speaks, list its tools page by page, call them |
| [bridge.py](../../harness/mcp/bridge.py) | MCP tool ↔ ZhiFox tool: names, parameter schemas and results |
| [manager.py](../../harness/mcp/manager.py) | Starting, stopping and restarting servers, their status, and the per-conversation snapshot |
| [config.py](../../harness/mcp/config.py), [importers.py](../../harness/mcp/importers.py) | `data/mcp.json`, hiding secrets from the page, importing from other tools (see [Importing from other tools](#importing-from-other-tools)) |

A call travels like this:

```
model: "call mcp__github__search_issues with {...}"
  └─► agent.py        like any other tool: the permission check (asks you by default), then run it
        └─► manager.py      find the connection to the github server
              └─► client.py      wrap it in a tools/call request
                    └─► stdio.py / http.py   write it to the server's stdin, or POST it to its URL
                    ◄── the server's reply
        ◄── bridge.py       turn the text and images in the reply into the result the model sees
```

**Two generations of the protocol.** Up to version 2025-11-25, a connection began with a handshake: the client sent
`initialize` with the version it supports, the server answered with the version and capabilities it chose, the client
confirmed with `notifications/initialized`, and that agreement held for the whole connection. Since 2026-07-28 MCP is
*stateless*: there is no handshake, and every request carries its protocol version, the client's name and its
capabilities in `params._meta`, so the server can handle any request on its own. Servers of both kinds are in use, so
ZhiFox speaks both. It first asks `server/discover` the new way. An answer, or an error only new servers use (such as
"unsupported version"), means a new server. Any other error, or silence, means an old one, and ZhiFox falls back to the
handshake. Some local servers start slowly (`npx` downloads the package the first time), so after a few seconds of
silence ZhiFox sends `initialize` as well and goes with whichever succeeds first.

**Tools are frozen for a conversation.** Every request starts with the tool specs, and Claude caches requests by their
beginning: change one tool halfway through a conversation and the cache for the whole conversation is lost, and Claude's
earlier thinking no longer matches what came before it. MCP servers, though, connect, disconnect and change their tools
whenever they like. So when a conversation sends its first message, ZhiFox takes a snapshot of the tools of the servers
connected at that moment, and the conversation keeps that snapshot to the end. A server that connects or changes later
is used from the next conversation, and its row in the MCP tab says **Next conversation** until then. If a server in the
snapshot goes away, calling its tool returns an error that says so, and the model tries something else.

**Translating names, schemas and results.** Providers accept tool names of at most 64 characters made of letters,
digits, `_` and `-`, and two servers may both have a tool called `search`. ZhiFox names each one
`mcp__<server>__<tool>`, replaces other characters with `_`, and shortens long names with a short hash so they stay
unique. Each description starts by saying which server the tool comes from. Parameter schemas pass through, minus what
providers reject: Claude refuses `anyOf` at the top level, and Gemini refuses `$ref` and `$schema`, so for the
OpenAI-format providers references are expanded in place. In results, text goes to the model, images go to it as images
when the model can see them, embedded resources become text, a structured result is written out as JSON when the server
sent no text, and `isError` marks the result as failed so the model can correct itself.

**Stop, timeouts and crashes.** **Stop** cancels the call in progress: on stdio ZhiFox sends `notifications/cancelled`;
over HTTP it hangs up that request (and also sends the notification to old servers). A call that takes longer than
5 minutes is cancelled the same way (change it per server with `"timeout": seconds` in `data/mcp.json`). A local server
that crashes is restarted automatically, at most 3 times in 10 minutes. When a server announces that its tool list
changed (`notifications/tools/list_changed`), ZhiFox fetches the list again in the background.

## Skills

A model knows a little about everything but nothing about how *you* want a particular kind of task done. Repeating the
same instructions every time is tedious, and putting all of them in the system prompt makes every request carry every
instruction. A **skill** solves this. It is a folder whose `SKILL.md` holds the steps for one kind of task, optionally
with scripts, reference docs and templates next to it. The format is the open [Agent Skills
standard](https://agentskills.io/specification), which Claude, Claude Code and a growing number of other tools use, so a
skill written for any of them installs here unchanged:

```
weekly-report/
├── SKILL.md          ---
│                     name: weekly-report          lowercase letters, digits and hyphens; same as the folder
│                     description: Write the       what it does and when to use it (≤ 1024 characters)
│                       weekly report. Use when…
│                     ---
│                     (the instructions, in Markdown)
├── scripts/          optional: code the model can run
└── references/       optional: documents it reads only when needed
```

**Progressive disclosure.** A skill is loaded in three stages, each only when needed. (1) Every enabled skill's name and
description are always in front of the model, about 100 tokens each, so you can install dozens. (2) When a request
matches a description, the model calls the `skill` tool to read the full `SKILL.md`. (3) Other files are read, and
scripts run, only when the instructions call for them.

**Where the list goes, and why not the system prompt.** Within one conversation the system prompt and the tool specs
must stay byte-for-byte the same (prompt caching depends on it, and Claude's earlier thinking is bound to it), but you
can install or disable a skill at any moment. So the list travels in the user message, like the current time does: the
first message of a conversation carries a `<skills>` block with the skills available then, and before each later
message the current list is compared with the last one the model saw. If they differ, the new list is attached to that
message. "The last one the model saw" is found by reading the conversation history itself, so reopening an old
conversation or rolling back a failed turn needs no extra bookkeeping. The one exception is Claude's automatic
compaction: whatever came before the compaction is no longer sent to the model, so a list there doesn't count. Claude
Code does the same thing, passing the list of skills as an attachment in the messages rather than in its system prompt.
How to use skills is written in that block and in the `skill` tool's description, not in the system prompt: you can
rewrite the system prompt in Settings, and a custom prompt mustn't quietly switch skills off. Scheduled tasks get the
list too, in the first message of each run. See [harness/skills/catalog.py](../../harness/skills/catalog.py).

**Reading and running.** The `skill` tool is read-only. It reads only enabled skills, and checks every path one level at
a time, so it can't leave that skill's folder or follow a symlink out of it. That's why reading a skill doesn't ask you,
just like the memory tool. Scripts are run by the model with `run_command`, which asks every time like any other
command. A skill can't loosen that: Claude Code treats `allowed-tools` as "use these without asking", but ZhiFox only
displays the field. `${CLAUDE_SKILL_DIR}` in a skill is replaced with the skill's real folder, so Claude Code skills that
use it keep working. A skill marked `disable-model-invocation: true` is left out of the list (it's meant to be used only
when you ask for it by name), and the model can still read it when you do.

**Installing.** Only you can install, change or delete a skill, from the web UI; the model can't. ZhiFox keeps its own
folder, `data/skills/`, and never reads other tools' folders on its own: **Import from other tools** looks in
`~/.claude/skills/`, `~/.agents/skills/`, `~/.codex/skills/`, `.github/skills/` and the other documented places (see
[Importing from other tools](#importing-from-other-tools)) only when you click it, and copies the ones you tick, so later
changes on either side don't affect the other. Everything that comes from outside is shown to you before anything is
written (its name, description, files and source) and treated as hostile:

- Links must be https, and redirects are followed only to https. Downloads time out and stop at 20 MB. A GitHub folder
  link (`…/tree/main/skills/pdf`) downloads that branch's archive from `codeload.github.com` in one request, without
  using the GitHub API's hourly quota, and takes the folder out of it. A link to a `SKILL.md` installs the folder it's
  in.
- Archives are refused if any entry is an absolute path, contains `..`, is a symlink, a device or a pipe, or is
  encrypted. There are limits on the number of files (1,000) and the total unpacked size (50 MB), and on names that
  differ only in case (they would overwrite each other on macOS). Files are first written to a hidden folder, which is
  renamed into place at the end, so a failed install never leaves half a skill.
- A name that clashes with an installed skill asks you to rename (the `name` in `SKILL.md` is rewritten to match the
  new folder) or replace.

**The frontmatter parser.** `SKILL.md` starts with a few lines of YAML. Instead of adding a YAML library,
[harness/skills/frontmatter.py](../../harness/skills/frontmatter.py) reads the small subset the standard uses (plain, quoted
and multi-line values, a one-level `metadata` map, lists) and rejects everything else with a line number: anchors and
aliases (which can blow up exponentially), tags (which some libraries use to construct arbitrary objects), and a `: ` in
an unquoted value (real YAML parsers reject it too, and Claude Code then loads the skill with no fields at all). Values
stay text, so `version: 1.0` is `"1.0"`, not a number. Fields outside the standard, such as Claude Code's `argument-hint`
or `hooks`, are kept in the file untouched.

## Custom agents: subagents and personas

A skill teaches the assistant *how* to do a kind of task. An **agent** is a different assistant altogether: its own
instructions, its own tools and its own model. Each one is a Markdown file in `data/agents/`, in the same format as a
[Claude Code subagent](https://code.claude.com/docs/en/sub-agents), so files written there import unchanged:

```markdown
---
name: researcher                  letters (any script), digits, - and _; also the file name (researcher.md)
description: Researches and       what it's good at and when to hand work to it (≤ 1024 characters)
  reads web pages, then returns a summary with sources.
tools: Read, WebSearch, WebFetch  optional; leave it out to get everything the caller has, [] for none
model: inherit                    optional: provider/model (deepseek/deepseek-flash), inherit, or opus / sonnet
color: blue                       optional: red, orange, yellow, green, cyan, blue, purple or pink
---
You are a careful researcher…     the instructions (its system prompt)
```

The same file is used in two quite different ways, and the difference is the heart of what "agent" means:

| | Subagent | Persona |
|---|---|---|
| Who starts it | The main assistant, with the `agent` tool, when a task matches the description (or you say "have the researcher look into…") | You, from the arrow next to **New conversation** |
| What it sees | Only the task the main assistant wrote: not your conversation, not your attachments | The whole conversation, like the normal assistant |
| History | A fresh one per task, thrown away afterwards; only its final answer goes back into the main conversation | It *is* the conversation, saved and reopened like any other |
| Who it talks to | The main assistant (it can't ask you questions; you watch its progress on a card) | You, directly |
| Good for | Context isolation: twenty web pages read inside the subagent cost the main conversation one paragraph | A specialised assistant you work with for a while: a writing partner, a code reviewer |

**Running a subagent** ([harness/agents/runner.py](../../harness/agents/runner.py)). When the model calls
`agent(name, task)`, ZhiFox builds a second `Agent`, the same loop class as the main assistant, with:

- **a fresh history** whose first user message is the task (plus the usual `<context>` time note);
- **its own system prompt:** the agent's instructions followed by an environment section the program appends (the
  working folder, the operating system, what the `<context>` block means, that web pages and files are material and not
  instructions, and that its "user" is the main assistant, which only receives its final answer, so it should report
  conclusions and say what is missing instead of asking). The environment lines are the same text as in the built-in
  prompt ([harness/prompt.py](../../harness/prompt.py) keeps them as named parts), and "you can search the web" is written
  only when it really got a web tool;
- **only the allowed tools, never more than the caller has:** the allow-list is applied to the caller's own tools, so a
  read-only persona can't summon a subagent that edits files, and a subagent on DeepSeek doesn't get Claude's web
  search. `Read` gives the file tool in a read-only form whose writes are refused before anything asks you (`Tool.check`).
  It also gets its own shell and its own MCP calls (a fresh snapshot under its own owner, filtered to the tools the
  caller had), exactly like a scheduled task run;
- **the chosen model**, or the caller's when it says `inherit`.

Every tool call it makes goes through the same `Permissions` object as its caller: in your conversation, the approval
mode you picked; in a scheduled task, that task's own mode. Approval cards appear in the conversation as usual, saying
which agent is asking. **Stop** reaches it too: while it runs, its stop function is registered on the caller's
interface, so it kills its own command and cancels its own MCP calls and nothing else, and the interrupted `agent` call
gets its result like any other tool call. Its model calls are written to the spending record with the source
`subagent`, and its tokens and cost are added to the caller's totals (but not its step count or context size). It may
call the model at most `AGENT_MAX_STEPS` times (25), and subagents can't call further subagents unless you raise
`AGENT_MAX_DEPTH` (1) in [harness/config.py](../../harness/config.py). The tool result the main assistant receives is the
subagent's final answer plus one line on what it did (how many model calls, which tools).

**Why the main conversation stays cheap and stable.** The `agent` tool is always registered, even before you create
your first agent, and its description never names any agent; the list of agents travels in user messages as an
`<agents>` block, exactly like the `<skills>` block ([harness/agents/catalog.py](../../harness/agents/catalog.py); both use
[harness/catalog.py](../../harness/catalog.py)). So the main assistant's system prompt and tool specs stay byte-for-byte the
same across turns, before and after a delegation, and the prompt cache keeps working.

**Personas.** Starting a conversation with an agent swaps three things for that conversation: the system prompt (its
instructions with the same `{workspace}`-style placeholders as your own prompt, plus the appended environment section),
the tools (its allow-list applied to the normal set) and the model (if it names one; that becomes the current model,
as when you reopen an old conversation). The conversation file records the agent's name, the sidebar shows it as a
small colored badge and the top bar shows it as a chip. A persona can't change mid-conversation (the system prompt and
tools mustn't), so you start a new one instead; switching to another provider keeps the persona. Reopening the
conversation rebuilds the prompt from the agent's current file, like the built-in prompt; if the agent has since been
deleted, the conversation continues with the built-in prompt and you're told so.

**Managing agents.** **Extensions → Agent** lists them; each one can be edited in a form (name, description, a model
picked from your synced model lists, a color, a tool checklist grouped into built-in, Skill and MCP, and the
instructions) or as raw Markdown, and both are checked by the same parser as `SKILL.md`
([harness/skills/frontmatter.py](../../harness/skills/frontmatter.py)). Saving from the form rewrites only the fields you
changed, so comments and fields ZhiFox doesn't use (Claude Code's `permissionMode`, `hooks`…) stay as they were.
Claude Code's tool names work: `Write` and `MultiEdit` mean `Edit`, `Grep`, `Glob` and `LS` mean `Read`, `Task` means
`Agent`, and names ZhiFox doesn't have are kept and listed but ignored. ZhiFox's own `Browser` gives all the browser
tools, when the caller has them (the browser is on); a subagent then works in its caller's tabs, under its caller's
approvals. `Computer` likewise gives the computer use tools when the caller has them; the subagent shares its caller's
screen session, so its steps ask you (with its name on the card) exactly when its caller's would: what you've allowed
for screenshots or for clicks and typing in that conversation covers it too. **Import from other tools** reads
Claude Code's, GitHub Copilot's, Gemini CLI's, OpenCode's, Codex's and other tools' agent folders only when you click it,
rewrites the ones in another format the ZhiFox way, and copies what you tick (see
[Importing from other tools](#importing-from-other-tools)). Three
examples (a researcher with read-only files and web search, a read-only code reviewer, a writing assistant with no
tools) are one click away; none is installed by default. The model has no tool that creates or changes agents.

## Importing from other tools

You may already have MCP servers, skills and agents set up in Claude Code, Cursor, VS Code, Codex, Gemini CLI or another
AI tool. **Import from other tools** (one button on each of the Skills, MCP and Agents tabs) reads the places those
tools document, shows what it found, and copies the items you tick into ZhiFox's own folders. Paths starting with `~`
are in your home folder; the others are in the working folder (the project level):

| Tool | MCP servers | Skills | Agents |
|---|---|---|---|
| [Claude Code](https://code.claude.com/docs/en/mcp) | `~/.claude.json` (yours, and this project's entry), `.mcp.json` | `~/.claude/skills`, `.claude/skills` | `~/.claude/agents`, `.claude/agents` (copied as is) |
| [Claude Desktop](https://modelcontextprotocol.io/docs/develop/connect-local-servers) | `claude_desktop_config.json` (macOS `~/Library/Application Support/Claude/`, Windows `%APPDATA%\Claude\`, Linux `~/.config/Claude/`) | | |
| [Cursor](https://cursor.com/docs/context/mcp) | `~/.cursor/mcp.json`, `.cursor/mcp.json` | `~/.cursor/skills`, `.cursor/skills` | `~/.cursor/agents`, `.cursor/agents` |
| [VS Code](https://code.visualstudio.com/docs/agents/reference/mcp-configuration) | the user `mcp.json` (Code and Code - Insiders), `settings.json` → `mcp.servers`, `.vscode/mcp.json` | | |
| [GitHub Copilot](https://docs.github.com/en/copilot/reference/custom-agents-configuration) | `~/.copilot/mcp-config.json` | `~/.copilot/skills`, `.github/skills` | `~/.copilot/agents`, `.github/agents` (`*.agent.md`) |
| [Codex](https://learn.chatgpt.com/docs/extend/mcp) | `~/.codex/config.toml`, `.codex/config.toml` (TOML) | `~/.codex/skills`, `.codex/skills` | `~/.codex/agents`, `.codex/agents` (TOML) |
| [Gemini CLI](https://geminicli.com/docs/tools/mcp-server/) | `~/.gemini/settings.json`, `.gemini/settings.json` | `~/.gemini/skills`, `.gemini/skills` | `~/.gemini/agents`, `.gemini/agents` |
| [Agent Skills](https://agentskills.io/client-implementation/adding-skills-support) (the folder many tools share) | | `~/.agents/skills`, `.agents/skills` | |
| [Windsurf](https://docs.devin.ai/windsurf/plugins/cascade/mcp) | `~/.codeium/windsurf/mcp_config.json`, `~/.config/devin/mcp_config.json`, `.devin/mcp_config.json` | `~/.config/devin/skills`, `.windsurf/skills`, `.devin/skills` | |
| [Cline](https://docs.cline.bot/mcp/mcp-overview) | `~/.cline/data/settings/cline_mcp_settings.json`, and its two older locations | | |
| [Roo Code](https://roocodeinc.github.io/Roo-Code/features/mcp/using-mcp-in-roo) | `mcp_settings.json` in VS Code's globalStorage, `.roo/mcp.json` | `~/.roo/skills`, `.roo/skills` | |
| [Continue](https://docs.continue.dev/customize/deep-dives/mcp) | `~/.continue/config.yaml` (YAML), `~/.continue/config.json`, `~/.continue/mcpServers/`, `.continue/mcpServers/` | | |
| [Zed](https://zed.dev/docs/ai/mcp) | `settings.json` → `context_servers`, `.zed/settings.json` | | |
| [OpenCode](https://opencode.ai/docs/mcp-servers/) | `~/.config/opencode/opencode.json(c)`, `opencode.json(c)`, `.opencode/opencode.json(c)` | `~/.config/opencode/skills`, `.opencode/skills` | `~/.config/opencode/agents`, `.opencode/agents` |
| [Goose](https://goose-docs.ai/docs/guides/config-files) | `~/.config/goose/config.yaml` (YAML; Windows `%APPDATA%\Block\goose\config\`) | `.goose/skills` | |
| [Qwen Code](https://qwenlm.github.io/qwen-code-docs/en/users/features/mcp/) | `~/.qwen/settings.json`, `.qwen/settings.json` | `~/.qwen/skills`, `.qwen/skills` | `~/.qwen/agents`, `.qwen/agents` |
| [Kiro](https://kiro.dev/docs/mcp/configuration/) | `~/.kiro/settings/mcp.json`, `.kiro/settings/mcp.json` | `~/.kiro/skills`, `.kiro/skills` | `~/.kiro/agents`, `.kiro/agents` (JSON or Markdown) |
| [Amazon Q Developer](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/command-line-mcp-configuration.html) | `~/.aws/amazonq/mcp.json`, `.amazonq/mcp.json` | | `~/.aws/amazonq/cli-agents`, `.amazonq/cli-agents` (JSON) |
| [Junie](https://junie.jetbrains.com/docs/junie-cli-mcp-configuration.html) | `~/.junie/mcp/mcp.json`, `.junie/mcp/mcp.json` | `~/.junie/skills`, `.junie/skills` | |
| [Factory](https://docs.factory.com/cli/configuration/custom-droids) | | `~/.factory/skills`, `.factory/skills` | `~/.factory/droids`, `.factory/droids` |
| [Amp](https://ampcode.com/docs/customize/skills) | | `~/.config/amp/skills`, `~/.config/agents/skills` | |
| [Warp](https://docs.warp.dev/agents/capabilities/mcp) | `~/.warp/.mcp.json`, `.warp/.mcp.json`, `~/.agents/.mcp.json`, `.agents/.mcp.json` | | |
| [LM Studio](https://lmstudio.ai/docs/app/mcp) | `~/.lmstudio/mcp.json` | | |

The page opens on the tab you came from. At the top, every tool where something was found has a row with counts for the
three kinds: click the current kind to jump to that tool's group, or another kind to switch to that tab's import page.
Each group says which tool, which level (yours or this project's) and which file it came from; a file that can't be read
shows the reason (for example "not valid JSON: line 4, column 3") and the other files are listed as usual. At the
bottom, a collapsed **Not found** list names every tool ZhiFox knows for that kind, the places it looked, and a link to
the tool's documentation.

Each tool writes these files differently, so before checking an item with ZhiFox's own rules (the same ones as adding it
by hand), [harness/imports/](../../harness/imports/) rewrites it the ZhiFox way and lists what changed on the item:

- **MCP:** `servers` (VS Code), `context_servers` (Zed), TOML tables (Codex), the YAML `extensions` map (Goose), the
  YAML list of Continue and OpenCode's `command` array all become Claude Desktop's format. References to environment
  variables in any of the tools' styles (`${env:X}`, `$X`, `{env:X}`, `${{ secrets.X }}`, Codex's
  `bearer_token_env_var`, Goose's `env_keys`) become ZhiFox's `${X}`, read from the environment when the server starts;
  editor variables such as `${workspaceFolder}` are filled in; things only that tool can do (VS Code's `${input:…}`,
  an `envFile`) are flagged for you to edit afterwards. Settings that skip approval in the other tool (`autoApprove`,
  `alwaysAllow`, `trust`, and ZhiFox's own `approval` in a cloned `.mcp.json`) are never copied: an imported server
  asks before every call. Settings ZhiFox doesn't use are listed as "not brought over" instead of being kept silently.
  A server whose configuration refers to one of ZhiFox's model keys (`${ANTHROPIC_API_KEY}`) is shown but can't be
  ticked, and the server refuses it again on import.
- **Skills** are the same `SKILL.md` folders everywhere, so they are copied through the same hostile-archive checks as
  an upload: links and hidden files inside a skill are skipped, paths, sizes and file counts are capped.
- **Agents:** Claude Code's files are copied unchanged. Other tools' files are rewritten with only the fields ZhiFox
  uses: tool names are mapped (`execute` and `run_shell_command` → `Bash`, `read_file` → `Read`, `server/*` and
  `@server` → `mcp__server`…), tools ZhiFox doesn't have are dropped, a "read-only" or "no shell" setting stays
  restrictive, an unrecognized model becomes "same as the caller", Codex's `developer_instructions` and Kiro's `prompt`
  (including a `file://` next to the agent) become the body. A converted agent can only ever get fewer tools, never
  more.

Nothing is read until you open the page, nothing is run, and nothing is written outside ZhiFox's own data folder. The
page only sends back "which source, which name"; the server reads the file again when importing. The values of
environment variables and headers never reach the browser, only their last 4 characters. Adding a tool is one function
in [registry.py](../../harness/imports/registry.py), plus a reader in [mcp_shapes.py](../../harness/imports/mcp_shapes.py) or
[agent_formats.py](../../harness/imports/agent_formats.py) when its format is new. YAML is read by a small, safe subset parser
([yaml_lite.py](../../harness/imports/yaml_lite.py)) rather than a new dependency: anchors, aliases and tags are refused.
