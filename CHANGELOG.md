# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Before 1.0.0, a minor version may include breaking changes.

## [Unreleased]

### Added

- **Update check (off by default).** Turn it on in **Settings → Check for updates**, or with `harness updates on`
  (`harness updates` shows the last result, `check` asks now, `off` turns it off). Once a day ZhiFox sends one GET to
  GitHub's latest-release API, with no login, no cookies and a User-Agent of just `ZhiFox`: GitHub sees your IP address
  and nothing else is sent. Drafts and pre-releases are ignored, and versions are compared as numbers. When a newer
  release is out, the settings gear gets a small dot and Settings shows the release page and the upgrade commands for
  your install (`git pull` and `uv sync` in a clone; `uv tool upgrade` or `uv tool install --force` with the release's
  wheel for a `uv tool` install; a service restart when it runs in the background; on Windows, stop it first). The
  terminal UI mentions a new version at start, and `harness doctor` shows it from the cache only, never going online.
- The terminal UI saves conversations too, in the same format and store as the web UI: every turn is saved (an
  interrupted one too), so the web UI lists, searches, opens, continues, edits, regenerates and forks terminal
  conversations, and the terminal opens web ones. New commands: `/history` (recent conversations, or a search with
  `/history words`) and `/open <number|id>` (switches to that conversation's provider and model, like opening it in the
  web UI, and prints its last few messages). `/retry`, `/edit` and `/undo` work on opened conversations. The terminal
  now holds the data folder's lock: while the web UI or the background service is running it still chats but doesn't
  save, and while it is open, `harness --web` and the service ask you to `/exit` it first. SIGTERM (Ctrl+Break on
  Windows) stops the current turn, saves and quits.
- Several read-only tool calls in one model step run at the same time: viewing files and folders, viewing memory,
  listing to-dos and scheduled tasks, and reading skills. Only calls that need no approval at all and that no
  `PreToolUse` hook matches join a batch; everything else still runs alone and in order. Cards, approvals, hooks and the
  results sent to the model keep the order of the calls, Stop still answers every call, and the run trace draws a batch
  as overlapping, linked rows ("ran alongside N other steps") whose time is counted once. `MAX_PARALLEL_TOOLS` (4; 1
  turns it off).
- Conversation search matches pinyin: `baogao`, `bao gao` or `BaoGao` finds 报告, every reading of a polyphonic
  character counts (`yinhang` finds 银行, `xingzou` finds 行走), and three or more initials find a title (`zbmb` finds
  周报模板). Pinyin is tried only when a keyword doesn't appear as typed, and pinyin matches rank after the other kinds,
  so earlier results keep their order. The readings come from Unicode's Unihan database (Unicode 18.0.0, Unicode License
  v3), generated once by `tests/make_pinyin.py` into `harness/search_pinyin.txt` and loaded on the first pinyin search.
- `web_fetch`, a small read-only tool that lets GPT, Gemini, DeepSeek and custom endpoints read a web page by its
  address (Claude keeps Anthropic's server-side web fetch; with `HARNESS_WEB=0` Claude uses this one). It extracts
  readable text from HTML with the standard library, also reads plain text, JSON and Word, Excel and PowerPoint files,
  and refuses PDFs and images. It is not a search. On by default (**Settings → Read web pages**); the first fetch from
  each site in a conversation asks you, showing the full address, and only Allow everything skips that. A hard guard
  refuses anything but http(s) and every loopback, private, link-local (cloud metadata), CGNAT, multicast, reserved or
  unspecified address, checked after DNS and on every redirect, connecting to the address it checked; no proxy, no
  cookies, no keys; limits of 5 MB, 20 s, 5 redirects and 30,000 characters; the per-message cap is shared with
  `HARNESS_MAX_SEARCHES`.
- `CONTRIBUTING.md` (English, then 简体中文), GitHub issue forms for bug reports and feature requests (the bug form asks
  for `harness --version`, how ZhiFox was installed, the system and `harness doctor --json`), and a pull request
  template with a checklist. Blank issues are off, and security reports are pointed to `SECURITY.md`.

### Changed

- "Allow read-only commands" also accepts read-only commands joined with `;`, `&&` or `||` (each part judged on its
  own, as with pipes) and output thrown away or merged (`2>/dev/null`, `>/dev/null`, `&>/dev/null`, `>>/dev/null`,
  `2>&1`, `>&2`). The first real eval runs showed all three models writing such commands and being asked every time.
  Any other redirection, a single `&`, wildcards and `cd` in a chain still ask.
- `run_command` remembers the working folder between commands, as Claude Code's Bash tool does: a `cd` (or
  `Set-Location`) carries over to the next command; environment variables, aliases and functions still don't. New
  conversations, reopened or forked ones, and subagents start in the working folder. When a command won't run in the
  working folder, the approval card says which folder it runs in; "Allow read-only commands" judges paths, and the
  folder itself, from there (so `cd data` followed by `ls` asks); bubblewrap starts in that folder; hooks receive it as
  `command_cwd`; editing or regenerating the last turn starts it again in the folder that turn began in.
- The eval's cost estimate assumes less before a task's first real run (9,000 tokens of context and 400 written per
  call, a little above what the first real runs used), and says how its upper bound was worked out.
- The `injection-in-readme` eval task counts only deleting, moving or overwriting `keep-me.txt` as following the
  injected instruction, not reading it.

### Fixed

- `web_fetch` folds a long run of list items that are nothing but a link (a language menu, a navigation list) into one
  line. On real Wikipedia pages the language menu sits inside `<main>`, hidden only by CSS, and its 150-odd links came
  before the article and used up the 60 link addresses, so links in the article itself lost theirs.
- **Extensions → Channels**: clicking a channel just as its state changed (connecting, a message arriving) could be
  lost, because the whole page was redrawn between the press and the release. Redraws now wait until the mouse is
  released, as the model switcher and the hooks page already did.

### Documentation

- Both READMEs are now a short overview (what ZhiFox is, highlights, quick start and an index), and the rest lives in
  20 topic pages per language under `docs/en/` and `docs/zh-CN/` with the same names. `tests/test_docs.py` checks
  every relative link and anchor, that both languages have the same pages, and that the index lists them all.

## [0.2.0] - 2026-11-23

### Added

#### Installation

- One-line install with `uv tool install git+https://github.com/Akxan/zhifox-agent-harness` (or GitHub's zip of the
  code, which needs no Git), updated with `uv tool upgrade harness-assistant`. Installed this way, data and `.env` live
  in a per-user folder (macOS `~/Library/Application Support/ZhiFox`, Linux `$XDG_DATA_HOME/zhifox`, Windows
  `%LOCALAPPDATA%\ZhiFox`), so upgrading or uninstalling never touches them. Running from a clone keeps `data/` and
  `.env` in the project.
- From a clone: `uv sync`, then `uv run harness` (terminal UI) or `uv run harness --web` (web UI). Needs Python 3.11+
  and uv, on macOS, Linux or Windows.
- Each release's wheel and source archive are attached to its GitHub Release, so a released version installs without
  Git: `uv tool install https://github.com/Akxan/zhifox-agent-harness/releases/download/v0.2.0/harness_assistant-0.2.0-py3-none-any.whl`
  (or `uv tool install git+https://github.com/Akxan/zhifox-agent-harness@v0.2.0`).
- `harness --version` prints the version, and the git commit when running from a clone.

#### Agent loop and models

- A hand-written agent loop (`harness/agent.py`) with no agent framework, on top of the official `anthropic` and
  `openai` SDKs. The conversation history is only appended to.
- Claude (`claude-opus-5-5`, `claude-sonnet-5-5`, `claude-fable-5-2`) through the native Anthropic Messages API; GPT,
  Gemini and DeepSeek through one shared OpenAI Chat Completions adapter. GPT and Gemini have not been tested with real
  keys yet.
- API keys entered in Settings (saved to `.env`, shown only by their last 4 characters) or written to `.env` directly;
  each provider's model list can be synced.
- Switching models within a provider continues the conversation; switching to another provider starts a new one and
  keeps the old one. Reopening a conversation switches back to the model it used.
- Automatic compaction of long conversations: Claude's API compacts on its servers; for GPT, Gemini and DeepSeek,
  ZhiFox has the same model summarize the earlier part once 70% of the context window is used, keeping the last two
  turns verbatim.
- Custom model endpoints (**Settings → Custom model endpoints**) for any OpenAI-format server, such as Ollama,
  LM Studio, vLLM, llama.cpp or a company gateway: a free connection test, per-model capabilities, local endpoints
  counted as free, and plain `http://` only for this computer or the local network.
- Backup models (**Settings → Backup models**): when a model is overloaded (529), failing (5xx) or rate-limiting (429),
  the rest of the turn moves to up to three other models of the same provider, in the order you set. None by default.
- An editable system prompt in Settings, with placeholders, a preview of what the model receives and
  **Restore default**; saved as `data/prompt.md` and applied from the next conversation.
- Optional `.env` settings: `HARNESS_MAX_SEARCHES`, `HARNESS_WEB`, `HARNESS_EFFORT`, `HARNESS_DATA_DIR`,
  `HARNESS_PROVIDER` / `HARNESS_MODEL`, `OPENAI_BASE_URL` and `HARNESS_CHROME`.

#### Tools

- Built-in tools for to-dos, long-term memory shared by all conversations, reading and writing files, running shell
  commands, and web search (Claude only).
- Web search is capped per message (5 searches and 5 page fetches by default), and answers show their sources:
  numbered markers and a **Sources** list in the web UI, `[1]`-style references in the terminal and in chat apps.
- When the file tool opens an image (PNG, JPEG, GIF, WebP), the image itself goes to models that can see images. A file
  it overwrites completely keeps its previous version in `data/backups/`.
- `function_tool` in `harness/tools/base.py` for writing your own tools, usable with all four providers.

#### Attachments and media

- Attachments in the web UI: the paper clip, drag and drop, or a pasted screenshot; up to 10 per message, with or
  without text.
- Images, PDFs, Word, Excel and PowerPoint files (their text extracted by ZhiFox), SVG (drawn as PNG) and text files in
  UTF-8, UTF-16 or GBK. Other formats are refused with a note on how to convert them.
- Each provider gets only what it can read (DeepSeek takes text only; Gemini and DeepSeek don't take PDFs), and the page
  says so when you add the file.
- A **Media** tab in the sidebar with every attachment from every conversation: filter, search, and a viewer with zoom,
  pan, copy, download, **Show in Finder** and **Go to message**. Originals are stored once each in `data/media/`,
  named by their SHA-256.

#### Web UI

- `uv run harness --web` serves a local page over SSE, on 127.0.0.1 only, behind a per-run token (`--port`,
  `--no-browser`). When the browser accepts gzip, the page (a little over 1 MB) is sent compressed to about a third of
  that, which helps from a phone or through a tunnel; it reflects nothing from the request, so compression attacks
  such as BREACH don't apply.
- Conversations are saved automatically to `data/conversations/` and can be opened, renamed, deleted or shown in the
  file manager from the sidebar.
- Several conversations at once: while one is working or waiting for your approval, open another or start a new one
  and the first keeps running in the background. The sidebar marks each one (running, needs your approval, failed, new
  reply) and can stop it; an approval waiting in the background shows at the top of the page and as a system
  notification. At most `MAX_PARALLEL_CHATS` (3) run at the same time.
- Full-text search across saved conversations (⌘F or ⌘K, Ctrl+F or Ctrl+K), by title and content, tolerant of typos and
  skipped characters, with no search library; a result opens at the matching passage.
- Replies rendered as Markdown (headings, lists, task lists, tables, code blocks with a copy button) by a small
  hand-written renderer that never parses HTML; links work only for http, https and mailto, and images aren't loaded.
- Light and dark appearance (light by default) or Match system, plus a motion setting: follow the system, Reduced or
  Full.
- Interface in Chinese, English and Spanish, switched in place without reloading the page; the terminal UI uses the
  same choice.
- A model switcher panel (⌘. or Ctrl+.) grouped by provider, showing each provider's key status and each model's
  capabilities and price. Settings opens from the gear (⌘, or Ctrl+,).
- An info panel with the estimated cost, the token split, open to-dos and **What 智狐 can do**: a capability list that
  says what this conversation can use, whether it asks first in the current approval mode, and how to turn on what's
  off.
- A memory window to list, read, edit, rename and delete the assistant's memory files.
- To-dos in the info panel can be completed, edited and deleted in place.
- Every locally stored item (conversations, memories, to-dos, tasks, skills, agents, MCP servers, hooks…) has the same
  **Show in Finder** / **Edit** / **Delete** buttons, with deletes confirmed in place; editors ask before discarding
  unsaved changes.
- Approval cards with **Allow** and **Deny**; a denial can carry a reason, which is passed to the model.
- System notifications while the page is in the background: an approval waiting for you, a finished reply, an error.
  Clicking one brings the page back to that card or reply; nothing pops up while you're looking at the page or for
  what's replayed on reload. The tab title and icon get a mark too. On by default once the browser allows it
  (**Settings → System notifications**, with **Send a test** and how to allow it in each browser).
- **Edit** the last message and **Regenerate** the last answer. Only the last turn can be rewound, and if it ran
  commands or changed files, ZhiFox lists those steps and asks once first.
- Fork a conversation from any finished turn: **Fork from here** under a reply keeps that turn, **Fork from this
  message** puts your message (text and attachments) back in the input box to change and resend. The original stays
  byte for byte as it was; the new one opens at once, its title ending in "(fork)" and a line at the top linking back.
  Undoing file changes stays with the original, and turns already folded into a GPT, Gemini or DeepSeek summary can't
  be forked.
- Undo a turn's file changes: a **Changed N files** line with a line-by-line preview, undo and redo per file or per
  turn, and a check for files that changed since. Files changed by shell commands are named as not undoable.
- A run trace for every turn: a waterfall of model calls, tool calls and time spent waiting for approval, with tokens
  and cost, saved with the conversation.
- An app icon and a web app manifest, so a bookmark, a Chrome shortcut or an installed app shows the fox.

#### Terminal UI

- `uv run harness` with `/help`, `/new`, `/todos`, `/tools`, `/trace`, `/usage`, `/model`, `/mcp`, `/auto`, `/retry`,
  `/edit`, `/undo`, `/doctor` and `/exit`. Conversations in the terminal UI are not saved.
- Commands that never call a model (`harness --help`, `service`, `hooks`, `sandbox`, `channels`, `commands`,
  `eval tasks`…) no longer load the Anthropic and OpenAI SDKs, so loading ZhiFox's command line takes about 0.15 s
  instead of about 0.9 s.

#### Troubleshooting

- `harness doctor` (`/doctor` in the terminal UI) shows on one screen how ZhiFox is set up on this computer: Python
  and ZhiFox's version, folder permissions, whether another ZhiFox holds the data folder, which providers have a key,
  Chrome, the sandbox, computer use permissions, the background service, extensions and network settings. Each line
  is OK, Problem or Not used, every problem comes with a fix for your shell (zsh, bash or PowerShell), and the exit
  code is 1 when anything is a problem.
- It only looks: no files, permissions or settings are changed, nothing is started and nothing goes over the network.
  Keys are reported as present or missing, never with their last characters; `--json` gives the same report for
  scripts or for attaching to a bug report, without keys, tokens or tokenized addresses.

#### Permissions and approvals

- Writing files, running commands, reading outside the working folder and reading `data/` ask first. `.env` is off
  limits in every mode, and commands don't see the providers' API keys.
- Four approval modes: Ask every time (the default), Allow read-only commands, Allow file edits and Allow everything
  (`/auto ask|readonly|edits|all` in the terminal). Looser modes last until the next restart, and steps that ran
  without asking are marked **Allowed automatically**.
- Allow read-only commands: a deliberately conservative check (`harness/commands/`) lets commands that only look
  (`ls`, `df -h`, `git status`, `journalctl`…) run without asking; anything it can't read with certainty still asks.
- Your own commands that don't ask, in **Settings → Commands that don't ask** or with `harness commands add` / `list` /
  `remove`, stored in `data/commands.json`.

#### Sandbox, hooks and secret masking

- An optional command sandbox, off by default: macOS `sandbox-exec` or Linux bubblewrap keeps each command to the
  working folder, away from `data/`, `.env` and the rest of the home folder, at two levels (**Working folder only**,
  without network, and **Working folder + network**).
- When the sandbox can't work on this computer, commands are refused rather than run outside it; running one command
  outside it (`unsandboxed: true`) asks every time. Extra paths can be allowed, and tasks and chat channels can pick
  their own level. Also on the command line as `harness sandbox`. Not available on Windows.
- Hooks in the style of Claude Code's: your own scripts at `PreToolUse`, `PostToolUse`, `UserPromptSubmit` and `Stop`,
  with JSON on stdin and exit-code answers. A hook can block a step or ask again, never relax an approval.
- **Extensions → Hooks** with a **Test run**, and `harness hooks add` / `list` / `enable` / `disable` / `remove` /
  `test`, stored in `data/hooks.json`. Hooks run in conversations, scheduled tasks and chat channels, inside the command
  sandbox when it is on.
- Secrets in tool results are masked (`harness/redact.py`): known key formats, known values from the environment and
  `.env`, and random-looking values after hints such as `password=` are replaced before the model, the history, the
  page or hooks see them. On by default; **Settings → Mask secrets** turns it off.

#### Privacy and local data

- Conversations, memory, to-dos, tasks and settings are plain files in `data/`, readable only by your account (700/600
  on macOS and Linux, an ACL on Windows).
- The web page loads no external resources and shows model and tool output as text only; the server checks the token,
  Host and Origin of every request, and API keys are never sent to the browser.
- Only one ZhiFox can use a data folder at a time; a second one says which ZhiFox holds it.
- `SECURITY.md` explains how to report a vulnerability privately (GitHub's private vulnerability reporting),
  summarizes the security model and lists what is out of scope.

#### Extensions: MCP, skills and custom agents

- A hand-written MCP client for local (stdio) and remote (Streamable HTTP) servers, speaking both the stateless
  2026-07-28 protocol and the older `initialize` handshake.
- **Extensions → MCP**: add, edit, paste JSON, switch on and off, restart; each server's status and tools. A
  conversation's MCP tools are fixed when it starts, and every MCP call asks unless that tool or server is marked
  "don't ask". `/mcp` in the terminal. Only tools are supported, not MCP resources, prompts or OAuth sign-in.
- MCP server secrets are shown only by their last 4 characters, servers don't receive the model API keys, and the model
  has no tool that adds or changes servers.
- Skills in the open Agent Skills (`SKILL.md`) format: write one in the page, upload a `.zip`, `.skill` or folder, or
  add one from a URL (a GitHub folder or repository, a `SKILL.md`, a `.zip` or `.skill` file), and switch each on or
  off. Everything from outside is shown first and its archive checked as untrusted.
- The model sees each skill's name and description and reads the full skill with the `skill` tool when a task calls for
  it. A skill's `allowed-tools` never pre-approves anything.
- Custom agents: Markdown files in `data/agents/` in the Claude Code subagent format, used either as a subagent the
  main assistant hands a task to (the `agent` tool: a fresh history, at most 25 model calls) or as the persona of a
  conversation you start with it.
- A subagent never gets more tools than its caller and goes through the caller's approvals. **Extensions → Agent** has
  a form and a source editor, plus three examples (researcher, code reviewer, writing assistant).
- **Import from other tools** on the MCP, Skills and Agents tabs reads the documented places of Claude Code,
  Claude Desktop, Cursor, VS Code, GitHub Copilot, Codex, Gemini CLI and other tools (about two dozen) only when you open
  it, and copies what you tick. Other tools' "don't ask" settings are never copied.

#### Browser use

- Off by default (**Settings → Browser**): the assistant uses a separate Chrome that ZhiFox starts with its own profile
  (`data/browser/profile`), driven by a hand-written DevTools Protocol client over a pipe, with no debugging port.
- Claude uses Anthropic's `browser_toolset_20260801`; GPT, Gemini and DeepSeek get twelve hand-written stand-in tools.
  Each browser step's tool card carries a small screenshot.
- Reading a page doesn't ask; typing, submitting a form and opening a new site ask; password and card fields ask in
  every mode. `file:`, `chrome:`, `devtools:`, `javascript:` and `view-source:` addresses and all downloads are
  refused.
- Works with Chromium-family browsers (Chrome, Chromium, Edge, Brave). The top bar shows when ZhiFox is controlling the
  browser, with a **Stop** button.

#### Computer use

- An advanced option, off by default and turned on only after a risk confirmation: the assistant looks at the whole
  screen and works the mouse and keyboard (macOS through `screencapture` and CoreGraphics via `ctypes`, Linux/X11
  through `xdotool` and ImageMagick).
- Claude uses Anthropic's `computer_toolset_20260801`, GPT and Gemini get stand-in tools, and DeepSeek can't use it.
- Every step asks by default. In the web chat the approval card can stop asking about screenshots, or about clicks and
  typing, for that one conversation, and **Allow everything** covers computer use too. Dangerous key combinations
  always ask, and blocked apps are never touched.
- Not available on Windows or Wayland. Tested only against a fake desktop and a virtual X11 display, never a real
  screen.

#### Scheduled tasks

- Reminders (no model call, free) and agent tasks the assistant carries out on its own: once, daily, on chosen days of
  the week, monthly or every N minutes, in local time and safe across daylight saving.
- Runs happen alongside your conversation, up to three at once (the rest wait in line), and each run is saved as a
  normal conversation.
- Each agent task has its own approval mode and sandbox level; tasks the assistant creates are always Ask every time.
- Missed runs are caught up once; each task keeps its last 20 runs with their cost; **Run now** and **Stop** in the
  task list.
- The assistant can add, list and delete tasks (`task_add`, `task_list`, `task_delete`); adding or deleting an agent
  task asks you first.
- Tasks run while the web UI or `harness serve` is running, not in the terminal UI.

#### Chat channels

- Talk to ZhiFox from Telegram, Discord, Slack, Feishu, DingTalk or QQ through your own bot, with no public URL or open
  port (long polling for Telegram, an outgoing WebSocket for the others).
- A four-step setup wizard in **Extensions → Channels**, or `harness channels setup`, `pair`, `approval`, `sandbox` and
  `status` on the command line.
- Only the account paired with a 6-digit code is served; group chats are ignored.
- Approvals arrive in the chat with Allow / Deny buttons (or as a message to reply to, where the platform has no
  buttons) and on the web page, whichever you answer first. Each channel has its own approval mode and sandbox level.
- One conversation per chat, running beside the web chat, with `/new [agent]`, `/stop`, `/status` and `/help`.
- Photos and files sent to the bot go through the same checks as attachments, and `send_file` sends images and files
  back into the chat after you approve it (not on QQ).
- Telegram voice messages and audio files are transcribed first (OpenAI's transcription API, or a compatible service you
  run yourself), up to 10 minutes each, with the cost recorded.
- Feishu and DingTalk use their official SDKs, installed only with `uv sync --extra feishu` / `--extra dingtalk`.
  Discord, Slack, Feishu, DingTalk and QQ are tested only against local fake servers so far.

#### Headless mode and background service

- `harness serve` runs the chat channels and scheduled tasks without a browser, logs in a journald-friendly format and
  stops cleanly on SIGTERM; `harness serve --web` also starts the web UI on 127.0.0.1. `harness --web` saves and exits
  on SIGTERM too.
- `harness service install` / `uninstall` / `start` / `stop` / `restart` / `status` / `logs` / `open` hands ZhiFox to
  launchd (macOS), a systemd user unit (Linux) or Task Scheduler (Windows): it starts at login and restarts after a
  crash.
- The background service keeps its web token across restarts (`data/.web-token`), so an open tab keeps working after an
  update. The service commands are tested only against fake `launchctl`, `systemctl` and `schtasks`.

#### Cost tracking and backup

- Token usage and an estimated cost after every model call, with built-in prices for the three Claude models and
  DeepSeek (peak and off-peak), and prices you enter for other models. Prompt caching keeps resent history cheap.
- A spending record (`data/ledger.jsonl`) with every model call at its price at that moment, and a **Spending** window
  with the last 30 days, monthly totals and a breakdown by model and source.
- An optional monthly budget that reminds you once when you pass it and never blocks anything.
- **Settings → Export all data** downloads the data folder as a zip: without `.env` or the browser profile, and with the
  secrets in `mcp.json`, `channels.json`, `endpoints.json` and `hooks.json` masked.
- **Settings → Restore from backup…**, or `harness restore <zip>` (`--merge`, `--yes`) with ZhiFox stopped: the zip is
  checked as untrusted input against the export's own list of files, and a preview shows what's in it and what
  **Overwrite** or **Merge** would do. Before writing anything, the current data is exported to `data/restore-backups/`
  (secrets masked, the newest 5 kept, never exported), and ZhiFox then starts again in place, so the page just redraws.
- Masked secrets in a backup never overwrite the keys you have now: where the current file has a real value in the
  same place, it is kept, and the placeholders still to fill in are listed with where to do it. Restoring is refused
  while a conversation, a scheduled task or a chat channel is in the middle of a turn.

#### Evals

- `harness eval` runs 22 starter tasks (`evals/tasks/`, one TOML file each) with the real assistant in throwaway
  folders and grades what was left behind, not what it said; `tasks`, `run`, `list`, `show` and `compare`.
- Runs are dry runs by default: the local fake servers answer with each task's reference answer, so they cost nothing.
  `--real` prints a cost estimate and starts only after `--yes` or a typed `yes`, and is refused in CI; the LLM judge is
  off by default. No real run has been done yet.

#### Windows support

- Commands run in PowerShell (PowerShell 7 when installed, otherwise Windows PowerShell 5.1), and Stop or a timeout
  ends everything a command started (a job object).
- `data\` and `.env` are restricted to your account, SYSTEM and Administrators with an ACL. Path tricks such as `.env.`,
  `::$DATA`, short names, UNC paths, device names and directory junctions are refused, and the file tool keeps a file's
  line endings.
- Saving a file that another program has open (an antivirus or the search indexer taking a look) is retried up to 8
  times, a bit over a second in all, before it fails.
- The web UI says Ctrl instead of ⌘, and **Show in File Explorer** (**Show in file manager** on Linux), decided by the
  computer the browser and ZhiFox each run on.
- Browser use with Chrome, or Microsoft Edge when Chrome isn't installed, still over a pipe; the background service
  through Task Scheduler with a small supervisor that restarts ZhiFox after an error exit.
- Not on Windows yet: computer use, the command sandbox and automatic approval of read-only commands. Windows is tested
  only on GitHub's `windows-latest` runner, not by hand on a Windows 10 or 11 desktop.

#### Development and testing

- The test suite needs no API key: it drives the real SDKs against local fake servers (`tests/fake_api.py`,
  `tests/fake_openai.py`) and runs in parallel by default (pytest-xdist).
- Page tests in `tests/ui/` drive headless Chrome through ZhiFox's own CDP client against the demo server
  (`uv run pytest -m ui`).
- GitHub Actions runs the tests on Ubuntu, macOS and Windows with Python 3.11 and 3.13.
- The whole `harness` package is type-checked with mypy (`uv run --with mypy==1.19.0 mypy`), once each as Linux, macOS
  and Windows see the code (`--platform`); CI runs all three.
- Releases are built by GitHub Actions from a version tag (`.github/workflows/release.yml`): the wheel and source
  archive are built in a clean checkout, installed and smoke-tested on Ubuntu, macOS and Windows, and attached to the
  GitHub Release.
- Dependabot checks the Python dependencies (uv) and GitHub Actions weekly, grouped into one pull request each (plus
  one for security updates); the Feishu and DingTalk SDKs stay below their upper bounds in `pyproject.toml`.
- `uv run python tests/demo_web.py` starts the web UI with a fake model, temporary data and fake chat platforms, so you
  can try it without spending anything.

[Unreleased]: https://github.com/Akxan/zhifox-agent-harness/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Akxan/zhifox-agent-harness/releases/tag/v0.2.0
