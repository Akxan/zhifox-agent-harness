<div align="center">

<img src="docs/assets/logo.svg" width="96" alt="ZhiFox logo: a small fox">

# ZhiFox · 智狐

**A hand-written AI agent harness: your own personal assistant, running on your computer.**

[![Tests](https://github.com/Akxan/zhifox-agent-harness/actions/workflows/tests.yml/badge.svg)](https://github.com/Akxan/zhifox-agent-harness/actions/workflows/tests.yml)
[![Release](https://img.shields.io/github/v/release/Akxan/zhifox-agent-harness?sort=semver)](https://github.com/Akxan/zhifox-agent-harness/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-22c55e.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab.svg?logo=python&logoColor=white)](https://www.python.org/)
[![No agent framework](https://img.shields.io/badge/agent%20framework-none-f0532e.svg)](docs/en/architecture.md)
[![Models](https://img.shields.io/badge/models-Claude%20%7C%20GPT%20%7C%20Gemini%20%7C%20DeepSeek-6e56cf.svg)](docs/en/models.md#supported-models)
[![Interfaces](https://img.shields.io/badge/UI-terminal%20%2B%20web-0ea5e9.svg)](docs/en/web-ui.md)
[![Languages](https://img.shields.io/badge/i18n-English%20%7C%20%E4%B8%AD%E6%96%87%20%7C%20Espa%C3%B1ol-f59e0b.svg)](docs/en/web-ui.md#three-interface-languages)
[![GitHub stars](https://img.shields.io/github/stars/Akxan/zhifox-agent-harness?style=social)](https://github.com/Akxan/zhifox-agent-harness/stargazers)
[![Last commit](https://img.shields.io/github/last-commit/Akxan/zhifox-agent-harness.svg)](https://github.com/Akxan/zhifox-agent-harness/commits/main)

**English** | [简体中文](README.zh-CN.md)

</div>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/screenshot-dark-en.webp">
    <img alt="The ZhiFox web UI: the assistant reads the to-do list through a tool call, plans the week in a Markdown table and asks for approval before running a shell command; the sidebar lists earlier conversations, including a Telegram chat, a scheduled task run and one started with a custom agent" src="docs/assets/screenshot-light-en.webp" width="900">
  </picture>
</p>

ZhiFox is an AI agent harness built from scratch in Python, with no agent framework: a local-first personal assistant
that runs on your own computer, with a terminal UI and a web UI. It works with Claude, GPT, Gemini and DeepSeek, and uses
tool use (function calling) to manage to-dos, remember things across conversations, read and write files, run shell
commands, search the web, use websites in a separate Chrome of its own (browser use), operate your whole desktop as an
advanced opt-in option (computer use) and run scheduled or recurring tasks, with a permission prompt before anything
that changes your computer or sends something out. It also reads the
screenshots, images, PDFs and Word, Excel or PowerPoint files you attach, and when its file tool opens an image on disk
(PNG, JPEG, GIF or WebP), the picture itself goes to the model, so it can look at a screenshot it just saved. It is also a teaching project: every part of
this LLM agent, from the loop to the provider adapters, is hand-written and lives in its own file.

"ZhiFox" is the English name; in the app itself the name stays "智狐" in every interface language. The repository is
`zhifox-agent-harness`, but the Python package and the command are still called `harness`, because the project is about
what a harness is. Web search is available with Claude only; the other models can read a page whose address they
have (`web_fetch`). Every web UI conversation is saved automatically on your
computer, and you can reopen it from the sidebar or search it by title and content.

## Highlights

- 🦊 **No agent framework.** The loop that turns a model into an agent is one file of about 180 lines,
  [harness/agent.py](harness/agent.py). Tool dispatch, permissions, provider adapters, storage and search are plain
  Python on top of the official `anthropic` and `openai` SDKs.
- 🔌 **Four providers, two adapters.** Claude goes through the native Anthropic Messages API; GPT, Gemini and DeepSeek
  share one OpenAI Chat Completions adapter.
- 🛠️ **Tools that do real work:** to-dos, long-term memory shared by all conversations, reading and writing files,
  shell commands, web search (Claude only), and reading a web page by its address with any provider.
- 🧭 **Browser use, in a browser of its own.** Turn it on in Settings and the assistant opens pages, reads them, clicks,
  types and takes screenshots in a separate Chrome that ZhiFox launches with its own profile: never your everyday
  browser or its logins. A hand-written DevTools Protocol client talks to it over a pipe, with no debugging port anyone
  else could attach to. Looking is free; typing, submitting a form and visiting a new site ask first, and password and
  card fields ask in every mode. Claude uses Anthropic's browser use toolset; GPT, Gemini and DeepSeek get hand-written
  browser tools.
- ↩️ **One-click undo of a turn's file edits.** Each turn that changed files ends with a line listing them, with a
  line-by-line preview. Undo a file or the whole turn, then redo; if a file changed since, ZhiFox explains and asks
  first. Files changed by shell commands are named as not undoable instead of pretending
  ([details](docs/en/conversations.md#undoing-a-turns-file-changes)).
- 🖱️ **Computer use, as an advanced option.** Off by default and switched on only after an explicit risk confirmation:
  the assistant looks at your whole screen and works the mouse and keyboard (macOS through `screencapture` and
  CoreGraphics via `ctypes`, Linux/X11 through `xdotool` and ImageMagick, no new dependencies). By default every action
  asks you. In the web chat, the approval card lets you stop being asked about screenshots, or about clicks and typing,
  for that one conversation, and **Allow everything** covers computer use too; dangerous shortcuts (⌘Q on a Mac,
  Alt+F4 or Ctrl+Alt+Del elsewhere) still ask every time, and blocked apps are never touched. Screenshots are shrunk to
  the model's limits and its coordinates mapped back to real screen points (Retina included). Claude uses Anthropic's
  computer use toolset; GPT and Gemini get hand-written stand-ins.
- 🧩 **Skills in the open [Agent Skills](https://agentskills.io) format.** Write one in the page, upload a folder, a `.zip`
  or `.skill` file, paste a GitHub link, or import the skills you already use in Claude Code, Codex, Cursor, Gemini CLI
  and [other tools](docs/en/extensions.md#importing-from-other-tools). The model sees only each skill's name and
  description until a task calls for it, and the scripts a skill brings still need your approval to run.
- 🤖 **Custom agents, used two ways.** One Markdown file, in the same format as a Claude Code subagent, describes an
  agent: what it's good at, which tools and model it may use, and its instructions. The main assistant can hand a task
  to it as a *subagent* (a fresh history of its own; only its conclusions come back), or you can start a conversation
  with it as the *persona*. An agent never gets more tools than whoever calls it, and its commands still ask you first.
- 🪟 **Several conversations at once.** While one conversation is working (or waiting for your **Allow**), open another
  or start a new one and keep going: the first carries on in the background. The sidebar marks each one (running, needs
  your approval, failed, new reply) with a **Stop** button next to it, and an approval waiting in the background shows at
  the top of the page and as a system notification. Up to three run at the same time
  ([details](docs/en/web-ui.md#several-conversations-at-once)).
- ⏰ **Scheduled and recurring tasks.** Free reminders, or instructions the assistant runs on its own at a set time:
  once, daily, weekly, monthly or every N minutes, in local time and safe across daylight saving. Runs happen alongside
  your conversation (up to three at once), ask for approval the way you choose for each task (Ask every time by
  default), catch up once after ZhiFox was closed, and record what each one cost.
- 📱 **Chat channels: talk to it from Telegram, Discord, Slack, Feishu, DingTalk or QQ.** Connect your own bot in a
  four-step wizard (or from the command line) and chat with ZhiFox from your phone, photos and files included, both
  ways: ask for a screenshot or a report and it arrives in the chat as a photo or a file (after you approve it). On
  Telegram you can also just hold to talk: the voice message is transcribed first and answered like typed text. No
  public URL or open port: ZhiFox long-polls Telegram and keeps an outgoing WebSocket open to the others (a
  hand-written RFC 6455 client; Feishu and DingTalk use their official SDKs, installed only if you want them). Only the
  account you pair with a 6-digit code is served, and every command or file change it wants to make arrives in the chat
  with Allow / Deny buttons where the platform has them (a "reply allow or deny" message where it doesn't), or on the
  web page, whichever you answer first. A dropped connection doesn't lose a reply: a failed send is retried, and if the
  platform stays unreachable it's kept and delivered in order once the channel is back, with the page showing what's
  waiting (see [When a message can't be delivered](docs/en/chat-channels.md#when-a-message-cant-be-delivered)).
  WhatsApp and WeCom need a public URL and come later.
- 🖧 **Runs on a server.** `harness serve` runs the chat channels and scheduled tasks with no browser, logs in a
  journald-friendly format, stops cleanly on SIGTERM, and comes with a systemd unit; set it up and pair it with
  `harness channels setup` / `pair`, then do server chores from your chat app.
- ♻️ **Always on, no terminal needed.** `harness service install` hands ZhiFox to launchd (macOS) or systemd (Linux):
  it starts at login, restarts itself after a crash, keeps the web page and your chat bot up with no terminal open, and
  `harness service open` opens the page; its token stays the same across restarts, so an open tab survives updates.
- 📎 **Attachments.** Paste a screenshot or drop in images, PDFs, Word, Excel, PowerPoint and text files. What a model
  can't take is converted first (SVG to PNG, Office files to text, GBK text to Unicode) or refused with a note on how to
  convert it, in all three interface languages.
- 🧠 **Memory you can manage.** A window at the top of the sidebar lists everything the assistant remembers; read,
  edit, rename or delete any memory, or show its file in Finder.
- ✍️ **Your own system prompt.** Edit the built-in prompt in Settings, with placeholders and a preview of exactly what
  the model receives; changes apply from the next conversation.
- 🧩 **MCP, written by hand.** Connect any [Model Context Protocol](https://modelcontextprotocol.io) server, local
  (stdio) or remote (Streamable HTTP), and its tools become the model's tools. The client speaks both the current
  stateless protocol (2026-07-28) and the older handshake-based one; every call asks you first unless you mark that
  tool "don't ask", and servers already set up in Claude Desktop, Claude Code, Cursor, VS Code, Codex, Gemini CLI, Goose
  and [twenty-odd other tools](docs/en/extensions.md#importing-from-other-tools) can be imported in a few clicks.
- 🪝 **Hooks, in the spirit of Claude Code's.** Your own scripts at fixed moments: before a tool runs (after approval),
  after it, when you send a message, when a turn ends. Same events, matcher idea, JSON on stdin and exit-code answers as
  Claude Code; a hook can block a step or ask you again, never relax an approval (see [Hooks](docs/en/sandbox-and-hooks.md#hooks-your-own-scripts-at-fixed-moments)).
- 🛡️ **Permission prompts.** Every shell command and every file write waits for your approval, and when you deny one
  you can tell the model why. A middle mode lets commands that only look (`ls`, `df -h`, `git status`, `journalctl`…)
  run without asking while anything that could change something still asks; the check behind it is deliberately
  conservative. Looser modes last only until the next restart, and `.env` is always off-limits.
- 🧱 **An optional sandbox for commands, as a second safeguard.** Turn it on and every command runs inside an
  operating-system sandbox (macOS `sandbox-exec` with a generated profile, Linux bubblewrap): it can read and write the
  working folder, can't see the rest of your home folder, ZhiFox's data or `.env`, and, at the strictest level, can't
  reach the network, `127.0.0.1` included. Even a command you allowed, written by a model misled by a web page, can't
  get out by itself; running one command outside the sandbox always asks you, in every mode. Per task and per chat
  channel too, and it refuses to run rather than quietly running unsandboxed where it can't work (Windows, no `bwrap`).
- 💾 **Local-first.** Conversations, memory, to-dos and settings are plain files on your disk. The web server listens
  on 127.0.0.1 only, and the page loads no external resources.
- 🔎 **Search across past conversations**, by title and content, tolerant of typos and skipped characters, and by
  pinyin (`baogao` finds 报告), without a search library.
- 🖥️ **One loop, two interfaces.** The terminal UI and the web UI drive the same loop and save to the same conversation
  store, so a conversation started in one carries on in the other. The web UI streams over SSE,
  renders replies as Markdown (code blocks, tables, lists) with a small hand-written renderer, has light and dark
  themes, and respects reduced-motion settings.
- 🌐 **Interface in English, 中文 and Español**, switchable mid-conversation without a page reload.
- 💸 **Cost you can see:** token usage and an estimated cost after every model call, built-in prices for Claude and
  DeepSeek (checked against the official pricing pages, DeepSeek's off-peak discount included), a spending record that
  survives restarts (today, this month, the last 30 days, by model and source), an optional monthly budget reminder,
  prompt caching, automatic compaction of long conversations with every provider, and per-message caps on web search.
- 🧪 **Tests without API keys.** The test suite drives the real SDKs against local fake servers, so running it is free.

## Quick start

You need macOS, Linux or Windows. There are two ways to get ZhiFox:

- **Install it with one command** to use it: no clone, no project folder, and a `harness` command that works in any
  folder.
- **Clone the repository** to read the code, change it, and run the tests and the evals (see
  [From a clone](#from-a-clone-for-development-and-learning)).

### Install with one command

First install [uv](https://docs.astral.sh/uv/getting-started/installation/), then ZhiFox. uv also downloads a suitable
Python (3.11 or later) if you don't have one, and installs ZhiFox in an environment of its own.

```bash
# macOS and Linux
curl -LsSf https://astral.sh/uv/install.sh | sh                   # install uv, then open a new terminal
uv tool install git+https://github.com/Akxan/zhifox-agent-harness
harness --web                                                     # start the web UI; it opens in your browser
```

```powershell
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # install uv, then open a new window
uv tool install git+https://github.com/Akxan/zhifox-agent-harness
harness --web
```

Installing from a `git+https://` address needs Git on the computer (uv runs it to download the code; you never use it
yourself). macOS offers to install it the first time; most Linux systems have it. A fresh Windows doesn't: install
[Git for Windows](https://git-scm.com/download/win) (or `winget install --id Git.Git -e`), or install from GitHub's
zip of the code instead, which needs no Git:

```powershell
uv tool install https://github.com/Akxan/zhifox-agent-harness/archive/refs/heads/main.zip
```

To update an install made from the zip, run that same line again with `--reinstall` added.

If the terminal then says `harness` isn't found, run `uv tool update-shell` and open a new terminal: it adds the folder
uv puts commands in (`~/.local/bin`) to your `PATH`.

Updating and removing, installing a released version, and where your data and keys are: see
[Installation and updates](docs/en/installation.md).

### From a clone (for development and learning)

```bash
git clone https://github.com/Akxan/zhifox-agent-harness.git
cd zhifox-agent-harness
uv sync                 # install dependencies
uv run harness --web    # start the web UI; it opens in your browser
```

Here everything stays in the project: data in `data/`, keys in `.env`, both ignored by git. On Windows, see
[Windows](docs/en/windows.md) for the PowerShell version. ZhiFox tells the two apart by looking at where its package is: next to
its own `pyproject.toml` means a clone; inside a `site-packages` folder means installed (see
[harness/install.py](harness/install.py)).

Either way, the first time, the middle of the page tells you there is no API key yet. Click **Open settings** in that
notice (or the button at the top right), enter a key for at least one provider, and start chatting. For the terminal
UI, run `harness` (from a clone: `uv run harness`) once a key is set.

## Documentation

Everything else is in topic pages under [docs/en/](docs/en/), in this order (the Chinese pages under
[docs/zh-CN/](docs/zh-CN/) are the same set):

1. [Installation and updates](docs/en/installation.md): updating and removing, installing a released version, where your data and keys are kept, the update check, and starting ZhiFox from another folder
2. [Web UI](docs/en/web-ui.md): everything on the page (sidebar, attachments, settings, memory, extensions, the capability list), several conversations at once, and how the page is built
3. [Terminal UI](docs/en/terminal-ui.md): the same assistant in a terminal, with slash commands
4. [Models and providers](docs/en/models.md): the four providers and their keys, local models and your own gateway, what differs between providers, compaction of long conversations, backup models
5. [Conversations](docs/en/conversations.md): saving and reopening, editing the last message, forking from a turn, undoing a turn's file edits, search
6. [Tools and permissions](docs/en/tools-and-permissions.md): the three kinds of tools, parallel reads, the shell's current folder, approval modes and the read-only command check, writing your own tool
7. [Sandbox, secret masking and hooks](docs/en/sandbox-and-hooks.md): the optional sandbox for commands (macOS, Linux), masking secrets in tool results, your own hooks
8. [Extensions: MCP, skills and custom agents](docs/en/extensions.md): MCP servers, skills, custom agents (subagents and personas), and importing them from other tools
9. [Browser and computer use](docs/en/browser-and-computer-use.md): reading a page with `web_fetch`, browser use in ZhiFox's own Chrome, and computer use of the whole desktop (advanced, off by default)
10. [Scheduled and recurring tasks](docs/en/scheduled-tasks.md): reminders and tasks the assistant runs on its own, and how a run works when nobody is watching
11. [Chat channels](docs/en/chat-channels.md): talking to ZhiFox from Telegram, Discord, Slack, Feishu, DingTalk or QQ, with the setup wizard, pairing, approvals in the chat and voice messages
12. <a name="run-it-in-the-background"></a>[Background service and servers](docs/en/background-and-servers.md): keeping ZhiFox running with launchd, systemd or Task Scheduler, and `harness serve` on a server
13. [Windows](docs/en/windows.md): what is different on Windows (PowerShell, Ctrl shortcuts, File Explorer) and what isn't supported there yet
14. [Data and privacy](docs/en/data-and-privacy.md): every file ZhiFox keeps and who can read it, what the web page may make your computer do, export, backup and restore
15. [Cost](docs/en/cost.md): what you pay for, the spending record and monthly budget, and what keeps the cost down
16. [Configuration and troubleshooting](docs/en/configuration.md): optional settings in `.env`, and `harness doctor` when something doesn't work
17. [Architecture](docs/en/architecture.md): what a harness is, which file does what, the agent loop and the run trace
18. [Testing](docs/en/testing.md): free tests against local fake servers, page tests in headless Chrome, CI on three systems, releases
19. [Evals: is the assistant actually any good?](docs/en/evals.md): measuring whether a real model does the right thing, with dry runs, real runs and the task format
20. [Limitations, roadmap and FAQ](docs/en/limitations.md): what doesn't work yet or hasn't been tested with real keys, what may come next, and frequently asked questions

## Keywords

AI agent · agent harness · AI agent from scratch · AI agent without a framework · personal AI assistant · local-first AI
assistant · Claude API · Anthropic SDK · OpenAI-compatible API · GPT · Gemini · DeepSeek · tool use · function calling ·
prompt caching · human-in-the-loop approval · agent loop · browser use · computer use · Chrome DevTools Protocol ·
Python · terminal chat · web UI

## Contributing

Bug reports, ideas and pull requests are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) covers setting up a clone, running
the tests for free (fake servers, no API key), and the rules a change keeps: tests with every change, three interface
languages, both READMEs and both languages of the docs, and macOS, Windows and Linux. Report bugs with the
[bug report form](https://github.com/Akxan/zhifox-agent-harness/issues/new?template=bug_report.yml).

## Security

Found a security problem? Please report it privately, not in a public issue: [SECURITY.md](SECURITY.md) explains how,
summarizes ZhiFox's security model, and lists what is out of scope.

## License

[MIT](LICENSE). You can use, modify and distribute it freely, as long as you keep the copyright notice.

### Third-party data

The pinyin table that conversation search uses ([harness/search_pinyin.txt](harness/search_pinyin.txt)) is generated
from the [Unicode Han Database (Unihan)](https://www.unicode.org/reports/tr38/), Unicode 18.0.0, © Unicode, Inc., by
[tests/make_pinyin.py](tests/make_pinyin.py). It is distributed under the
[Unicode License v3](https://www.unicode.org/license.txt), whose full text is at the top of the file.

## Star History

<a href="https://www.star-history.com/#Akxan/zhifox-agent-harness&Date">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=Akxan/zhifox-agent-harness&type=Date&theme=dark" />
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=Akxan/zhifox-agent-harness&type=Date" />
    <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=Akxan/zhifox-agent-harness&type=Date" />
  </picture>
</a>
