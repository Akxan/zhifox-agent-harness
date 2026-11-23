**English** | [简体中文](../zh-CN/testing.md)

[ZhiFox](../../README.md) › [Documentation](../../README.md#documentation) › Testing

# Testing

How the test suite runs for free (the real SDKs against local fake servers), what each part covers, and how a release is
made.

```bash
uv run pytest          # in parallel, one process per CPU core (pytest-xdist): about a minute for everything
uv run pytest -n 0     # one test at a time, e.g. while debugging or to see print output
uvx ruff@0.17.6 check harness tests                # lint
uv run --with mypy==1.19.0 mypy                    # type check (settings in pyproject.toml)
uv run --with mypy==1.19.0 mypy --platform win32   # the same, as Windows sees the code (also linux, darwin)
```

The same commands work in zsh, bash and PowerShell. Neither checker is a project dependency: `uvx` and `uv run --with`
borrow a pinned version for one run. mypy runs inside the project's environment (`uv run --with`, not `uvx`) so it sees
the types that ship with the Anthropic and OpenAI SDKs. Code that differs between systems checks `sys.platform`, which
mypy understands, so `--platform` checks the Windows-only branches from a Mac and the other way round; CI checks all
three. New code should pass, and a `# type: ignore` has to name its error code and say why on the same line.
The whole `harness` package is checked, with no module left out. The groups of web app methods that live in their own
files (`XxxActions` in `harness/webui/*_api.py`, mixed into `WebApp`) inherit `WebAppPart` from
[harness/webui/webapp_types.py](../../harness/webui/webapp_types.py): a protocol that exists only for the type checker and
lists the shared state and the methods from other files that they use.

The tests need no API key and cost nothing. They run in parallel by default (`addopts` in
[pyproject.toml](../../pyproject.toml)); each worker process gets its own fake servers and temporary folders, and the page tests
start their own demo server and Chrome per worker, so nothing is shared between them. [tests/fake_api.py](../../tests/fake_api.py) and
[tests/fake_openai.py](../../tests/fake_openai.py) are two local fake servers that replay prepared replies in Claude's and
OpenAI's formats. That lets the real SDKs run the whole loop, and the tests check that the requests sent out are correct.
To see how the loop behaves in different situations (a tool fails, you deny a step, you interrupt, the model refuses),
read [tests/test_agent.py](../../tests/test_agent.py). The web UI tests are in [tests/test_webui.py](../../tests/test_webui.py),
where the test code plays the browser; the app icon's manifest and PNGs in
[tests/test_web_manifest.py](../../tests/test_web_manifest.py) (token, Host and Origin, the fixed list of names with no way to
reach another file, headers, the security policy, and the maskable fox staying inside its safe circle). Saving conversations and searching them are covered in
[tests/test_conversations.py](../../tests/test_conversations.py), [tests/test_search.py](../../tests/test_search.py) and
[tests/test_search_pinyin.py](../../tests/test_search_pinyin.py) (pinyin, polyphonic characters, ranking, a 2,000-conversation
speed check, and the table generator on a small Unihan sample, with no download), and
compaction for GPT, Gemini and DeepSeek in [tests/test_compaction.py](../../tests/test_compaction.py); the spending record and
budget in [tests/test_ledger.py](../../tests/test_ledger.py) (DeepSeek's peak and off-peak prices with a fixed clock, a
half-written last line, merging old entries), the export in [tests/test_export.py](../../tests/test_export.py) (no secret
ends up in the zip), restoring in [tests/test_restore.py](../../tests/test_restore.py),
[tests/test_restore_cli.py](../../tests/test_restore_cli.py) and [tests/test_restore_web.py](../../tests/test_restore_web.py) (a round
trip, merge conflicts, every kind of bad archive refused with nothing written, placeholders never overwriting a current
secret, the automatic backup holding no secret, the in-place restart), and their web endpoints in [tests/test_spending_web.py](../../tests/test_spending_web.py). The run
trace is in [tests/test_trace.py](../../tests/test_trace.py) (a fake clock, so every step's start, end, time to first token
and wait for approval are compared to the millisecond; the cost is the ledger's entry; allowed, declined and
auto-allowed calls; subagents nested under their step; how an open step is closed when you stop or an error hits; the
requests sent are byte for byte the same with and without tracing; saving, reopening and regenerating; scheduled tasks
and chat channels; the terminal's `/trace`; hook spans), and its waterfall on the page in
[tests/ui/test_ui_trace.py](../../tests/ui/test_ui_trace.py). Hooks are tested in [tests/test_hooks.py](../../tests/test_hooks.py)
with tiny Python scripts in a temporary folder, run with `sys.executable` so the same tests run on Windows: the checks
on `data/hooks.json` (no shell, no references to model keys, time limits), the environment without keys, the working
folder, the JSON on stdin, every exit code and JSON answer, timeouts, a hook that floods its output, Stop ending only
that run's hook, each event's semantics through the real loop (blocking after approval, asking again, `allow` never
skipping a question, notes appended only to the new tool result, a blocked message never reaching the API), scheduled
tasks, chat channels and subagents, the trace spans, the protected file and the masked export; the page and the command
line in [tests/test_hooks_web.py](../../tests/test_hooks_web.py) and [tests/ui/test_ui_hooks.py](../../tests/ui/test_ui_hooks.py). The model
catalog is in [tests/test_model_catalog.py](../../tests/test_model_catalog.py): the JSON the page gets (keys only as their
last 4 characters, tags, entered and built-in prices, off-peak prices), the switch endpoint agreeing with it (the same
provider continues, another provider starts anew and keeps the old conversation, refused while a chat channel holds the
conversation, readable but not switchable during a turn, the persona kept), and the terminal's `/model`. Scheduled
tasks are covered in [tests/test_tasks.py](../../tests/test_tasks.py) (schedules, including month ends and daylight saving;
storage; tools and their permissions) and [tests/test_task_runs.py](../../tests/test_task_runs.py) (runs through the real
server with a fake clock, so no test waits for 9 a.m.: a run beside a chat turn, two runs side by side, the queue past
the limit, Stop reaching only its own run, and the four approval modes; the fake API routes each run's requests by
its instruction, so parallel runs stay deterministic).
MCP is tested against [tests/fake_mcp.py](../../tests/fake_mcp.py), a fake MCP server that speaks either protocol generation
over stdio or HTTP, in [tests/test_mcp.py](../../tests/test_mcp.py) and [tests/test_mcp_web.py](../../tests/test_mcp_web.py).
Custom model endpoints run against `fake_openai.py` set up to imitate Ollama, LM Studio, or a server with no model
list, no tools or no `stream_options`: [tests/test_endpoints.py](../../tests/test_endpoints.py) (the URL rules, the form
checks, storage, guessing abilities, probing, redirects not followed, the key masked in exports),
[tests/test_endpoints_chat.py](../../tests/test_endpoints_chat.py) (a full turn and tool calls, stepping back without tools
or `stream_options`, no other provider's key or header reaching the endpoint, per-model context and prices, switching
rules, the catalog and the capability list) and [tests/test_endpoints_web.py](../../tests/test_endpoints_web.py) (the web
endpoints, a kept key never following the address elsewhere, the key absent from everything sent to the browser).
The media library is covered in
[tests/test_media.py](../../tests/test_media.py) (storing and deduplicating originals, recovering attachments from older
histories in both Claude's and OpenAI's formats, filtering and paging, ids that can't name a path, cleaning up originals
only when no conversation uses them) and in the media section of `tests/test_webui.py` (the endpoints and their headers).
Editing and regenerating are covered in [tests/test_rewind.py](../../tests/test_rewind.py) (in both formats: a request that
starts byte for byte like the original, no orphan tool results, earlier thinking blocks unchanged, reopening after a
restart, compaction, which steps count as changes), [tests/test_rewind_web.py](../../tests/test_rewind_web.py) (the
endpoints and their refusals, the confirmation, the event stream after a rewind in two tabs, the saved file, search and
the media library) and [tests/test_rewind_cli.py](../../tests/test_rewind_cli.py) (`/retry` and `/edit`).
Saving and opening conversations in the terminal is covered in
[tests/test_terminal_conversations.py](../../tests/test_terminal_conversations.py): the saved file and the search index after
a terminal turn, the web UI opening, continuing and regenerating a terminal conversation (and the terminal opening a
web one), `/history`, `/open` across providers, `/retry` and `/edit` after `/open`, failed saves, SIGTERM at the prompt
and mid-turn (in-process, and against a real `python -m harness` on macOS and Linux), and the data folder's lock.
Undoing file changes is covered in [tests/test_undo.py](../../tests/test_undo.py) and
[tests/test_undo_diff.py](../../tests/test_undo_diff.py) (temporary working folders: copies, every kind of edit, undo and
redo, hash conflicts, protected paths and links, size limits, reopening after a save, threads that aren't recorded),
[tests/test_undo_web.py](../../tests/test_undo_web.py) (the endpoints, events and saved file, the note to the model, rewinding
with undo), [tests/test_undo_cli.py](../../tests/test_undo_cli.py) (`/undo`) and
[tests/ui/test_ui_file_changes.py](../../tests/ui/test_ui_file_changes.py) (the page in three languages, narrow and dark).
Chat channels are tested against a local fake of each platform, while the test code plays your phone:
[tests/fake_telegram.py](../../tests/fake_telegram.py) (a Bot API that checks the HTML and the length of every message the
way Telegram does, and serves voice messages and audio files), [tests/fake_transcribe.py](../../tests/fake_transcribe.py)
(OpenAI's transcription endpoint: checks the form fields, the file name and the key, and can answer any error; voice
to text is tested in [tests/test_voice.py](../../tests/test_voice.py) and never calls the real API), [tests/fake_discord.py](../../tests/fake_discord.py) and [tests/fake_qq.py](../../tests/fake_qq.py) (REST plus a
gateway that can ask to reconnect, invalidate the session, stop acknowledging heartbeats or close with any code),
[tests/fake_slack.py](../../tests/fake_slack.py) (the Web API plus Socket Mode, re-sending envelopes that weren't acked), and
[tests/fake_feishu.py](../../tests/fake_feishu.py) / [tests/fake_dingtalk.py](../../tests/fake_dingtalk.py) (the HTTP APIs plus a
fake listener in place of the SDK). One rig ([tests/channel_rig.py](../../tests/channel_rig.py)) runs the whole program
against any of them: pairing (right, wrong, expired codes, strangers and groups ignored), duplicate deliveries,
splitting long replies, photos and documents, approvals answered by button, by text and from the web (racing each
other), per-channel approval modes, `/new` `/stop` `/status`, reconnecting and resuming, rate limits, sending files back
(every fake accepts the platform's uploads and checks its limits; photo or document, captions, too-big files refused
before asking, secrets never sent, `data/` asking, `/stop` during an upload, and the request prefix staying byte for byte
the same across turns, restarts and the web page: [tests/test_send_file.py](../../tests/test_send_file.py)), and that no
token or secret appears in the page, the logs, error messages or the export ([tests/test_channels.py](../../tests/test_channels.py),
[tests/test_channels_web.py](../../tests/test_channels_web.py), `tests/test_channels_<platform>.py`,
[tests/test_channels_platforms.py](../../tests/test_channels_platforms.py)). Sends that fail are tested by making the
fake Telegram drop the connection without answering, or answer 500, 429 or 403, for the next few calls: retried in
order, lasting failures not retried, kept and delivered once the channel is back online, shown on the page and in
`/status`, `/stop` and shutdown not waiting for the backoff, `send_file` reporting its failure to the model, and the
status back to *Online* right after a reset ([tests/test_channels_delivery.py](../../tests/test_channels_delivery.py),
[tests/ui/test_ui_channel_delivery.py](../../tests/ui/test_ui_channel_delivery.py)). When the SDKs are installed, the Feishu
dispatcher and the real DingTalk Stream client are also run against the fakes; otherwise those tests are skipped.
The WebSocket client and the HTTPS helper have their own tests against a local fake server and proxy
([tests/test_websocket.py](../../tests/test_websocket.py), [tests/test_net_http.py](../../tests/test_net_http.py)). The server mode and the CLI, including a real `harness serve`
process stopped with SIGTERM, are in [tests/test_serve.py](../../tests/test_serve.py).
The background service is in [tests/test_service.py](../../tests/test_service.py): the generated plist (checked with
`plistlib` and `plutil -lint`) and unit, quoting of awkward paths, the exact `launchctl` and `systemctl` command lines
against a fake, installing twice, refusing a busy data folder, status parsing, `open` and the logs, plus one real
`serve --service launchd` process writing its own log file. No test ever runs the real `launchctl` or `systemctl`, or
opens a browser: `tests/conftest.py` makes them fail.
The self-check is in [tests/test_doctor.py](../../tests/test_doctor.py): when each line is OK, a problem or not used; a made-up
key in a temporary `.env` (plus a bot token, an endpoint key, MCP and hook secrets and a proxy password) appearing
nowhere in the table or the JSON, not even its last 4 characters; a service that can't be checked; the fixes for zsh,
bash and PowerShell (with `machine.system` swapped and a fake `icacls`); the exit code; and a snapshot showing it changed
nothing on disk. The real `icacls /save` read is tested only on Windows ([tests/test_winacl.py](../../tests/test_winacl.py)).
Skills (the frontmatter parser, hostile archives, downloads against a local HTTPS server, how the list reaches the model
without changing the start of the request) are in [tests/test_skills.py](../../tests/test_skills.py).
Importing from other tools is in [tests/test_imports.py](../../tests/test_imports.py): a fixture file for every supported
source in a fake home folder (JSON, JSONC with comments, Codex's TOML, Goose's and Continue's YAML, Markdown agents with
different frontmatter, Kiro's and Codex's agent files), the rewriting of each format, masking, refusing model-key
references, never carrying "don't ask" settings over, malformed files, Windows locations and backslash paths, project vs
user level, and the YAML subset parser; [tests/ui/test_ui_imports.py](../../tests/ui/test_ui_imports.py) clicks through the
three import pages.
Custom agents are in [tests/test_agents.py](../../tests/test_agents.py) (the file format and its checks, the tool allow-list
that never grows, storage under a fake home folder, prompts, the `<agents>` catalog, delegation
against the fake API: a fresh history, the allowed tools, model overrides, approvals, Stop reaching the subagent's own
shell and MCP calls, step and depth limits, spending recorded as `subagent`, the main request's prefix staying
byte-identical; and personas saved and restored) and [tests/test_agents_web.py](../../tests/test_agents_web.py) (the
`/api/agent/…` endpoints, starting a conversation with an agent, and the subagent events, confirmations and Stop in
the web UI).
The read-only check has a table of about nine hundred commands, each with the verdict it should get and why: quoting
and escapes, every operator, option abbreviations and BSD late options, symlinks and hard links into protected
places, case and Unicode variants, recursive commands over folders that hold secrets, and every bypass found while
building it ([tests/test_commands.py](../../tests/test_commands.py)). The mode itself (what it lets through in a
conversation, a scheduled task and a chat channel, the "why" on each auto-allowed step, your own list on the page and
on the command line, and the model never reaching that list) is in [tests/test_readonly_mode.py](../../tests/test_readonly_mode.py),
[tests/test_task_runs.py](../../tests/test_task_runs.py) and [tests/test_channels.py](../../tests/test_channels.py); the three
selectors listing the same four modes, and the commands dialog, in [tests/ui/test_ui_approval_modes.py](../../tests/ui/test_ui_approval_modes.py).
Browser use is tested in layers. The CDP client runs against in-process pipes, with a thread playing Chrome's end so a
test can split a message into pieces or send an event before a reply ([tests/test_browser_cdp.py](../../tests/test_browser_cdp.py)).
Launching (descriptors 3 and 4, no API keys in Chrome's environment, its own process group), closing the whole group,
idle closing with a fake clock and the session's basics run against [tests/fake_chrome.py](../../tests/fake_chrome.py), a fake
Chrome started through the real `chrome.launch()` that answers the pipe protocol from a script
([tests/test_browser_chrome.py](../../tests/test_browser_chrome.py), [tests/test_browser_unit.py](../../tests/test_browser_unit.py),
which also tries many ways of sneaking a `javascript:` address past the address rules). The permission rules are in
[tests/test_browser_permissions.py](../../tests/test_browser_permissions.py). The tool specs and results go through the loop
against the fake APIs in [tests/test_browser_tools.py](../../tests/test_browser_tools.py): one toolset entry and no stand-ins
for Claude (the reverse for the others), `toolset_name` and `browser_state` on results, the batch halt rule, and a
request start that stays byte-identical across turns. The Settings endpoints, the header indicator and the screenshot
endpoint's headers are in [tests/test_browser_web.py](../../tests/test_browser_web.py). Finally
[tests/test_browser_live.py](../../tests/test_browser_live.py) runs a real headless Chrome against a page served by
`http.server` on your machine (reading, clicking, typing, recognizing password fields and submit buttons, screenshots,
tabs); it is skipped when no Chrome is installed.
Computer use never touches your real screen, mouse or keyboard in a test. `tests/conftest.py` makes the real backend
undetectable in every test, and the tests that need a desktop install [tests/fake_desktop.py](../../tests/fake_desktop.py),
a fake backend that records every event and answers screenshots with a made-up desktop picture
([tests/fixtures/](../../tests/fixtures/)); its screen size and Retina factor can be set. Against it, `tests/test_desktop_*.py`
check that every member and stand-in maps to the right events and coordinates (Retina included), that every step asks
by default while the conversation's "don't ask again" (screenshots, or clicks and typing) and Allow everything let
steps run unasked, that dangerous keys still ask with a warning, that tasks and channels never get the trust, Stop, a
run's own session and the one-screen claim, the tasks-and-channels switch, the endpoints and their headers, and that
the request start stays byte-identical across turns. [tests/test_desktop_x11.py](../../tests/test_desktop_x11.py) runs the
real X11 backend against a virtual display (Xvfb) it starts itself, and is skipped wherever Xvfb, `xdotool` or ImageMagick is missing, which includes
every Mac.
The command sandbox is tested in two layers. Everywhere, `tests/conftest.py` swaps the real backend for
[tests/fake_sandbox.py](../../tests/fake_sandbox.py), which records what would have been sandboxed (and can pretend the
sandbox isn't available), so the rules can be checked without one: the paths in a policy, the SBPL profile and the bwrap
arguments (odd file names passed through untouched, `data/` and `.env` always denied, the network on or off, no rule
ever written without a path), the approval card, the escape asking in every mode and refused in a task or channel set
to Allow everything, refusing rather than running unsandboxed, per-task and per-channel levels, and the settings
endpoints. Then the real thing: on a Mac, `sandbox-exec` runs commands in a temporary working folder and checks that
writing inside works, writing outside fails, reading a stand-in for `~/.ssh` fails, and `curl` reaches a local
`http.server` on 127.0.0.1 only at Working folder + network; on Linux the same checks run against bubblewrap, and are
skipped where `bwrap` isn't installed or can't create a sandbox. The page parts (the Settings section, the paths
dialog, the task and channel selectors) are in the page tests below.

The Markdown renderer has its own tests in [tests/markdown.test.mjs](../../tests/markdown.test.mjs), run by Node's built-in
test runner (`node --test tests/markdown.test.mjs`, nothing to install); `uv run pytest` runs them too when Node is
available. They use a tiny fake document whose `innerHTML` and similar HTML-parsing APIs throw, and which only allows
a fixed list of tags and attributes. They cover every supported syntax, nesting, escaping, malicious input, streaming
in random-sized pieces (the result must equal rendering all at once), and 10,000-line inputs.

Static checks in [tests/test_page.py](../../tests/test_page.py) run without a browser: every part of the page is listed
exactly once in the manifest, no part contains `</script`, `</style`, `<!--` or a placeholder that would break the
inlining, no two functions share a name, and every script part, as well as the assembled script, passes
`node --check` (two parts that both declare the same `const` each pass alone, but break the whole page once joined).

The page itself (the markup, styles and scripts in [harness/webui/page/](../../harness/webui/page/)) is tested in a real
headless Chrome by the suite in
[tests/ui/](../../tests/ui/). `uv run pytest` includes it; `uv run pytest -m ui` runs only it and `-m "not ui"` skips it. It is
skipped when no Chrome is installed, and CI requires it. It takes about a minute and adds no dependencies: it drives
Chrome with ZhiFox's own hand-written CDP client (no Playwright, Selenium or npm), starts `tests/demo_web.py` once as a
subprocess with a temporary data directory, and opens every test in a fresh, incognito-like browser context. Clicks,
hovers and key presses are real input events that wait until the target has stopped moving and isn't covered, and every
wait is on a condition in the page, never a fixed sleep. Any script exception, `console.error` or blocked resource (CSP)
fails the test. It covers: loading with no errors and no outside requests; replies streaming in, then replaying after a
reload without entry animations; approval cards (allow, deny with a reason, Esc never answering) and the two-step
"Allow everything"; light by default, dark, following the system, and text contrast; reduced motion from the system and
from the Appearance panel; switching between Chinese, English and Spanish in place (the conversation, scroll position,
half-written message and open panel all stay) and a sweep of every panel and dialog for untranslated Chinese; text
overflow at 390 px wide in all three languages; the Show in Finder / Edit / Delete buttons on every kind of stored item
(conversations, to-dos, tasks, memories, skills, MCP servers, agents, channels, the sandbox's extra paths) on hover and keyboard focus, and
in-place delete confirmations; the unsaved-changes guards (Esc, ×, Cancel, clicking outside, Esc with the focus outside
the dialog); dropdown arrows drawn by the page; the Extensions tabs and empty states; the media library and viewer;
the model switcher (what it lists, switching within a provider at once, asking in place before leaving a conversation
for another provider, the link to a missing key, the keyboard, translating in place, rising from the bottom on a phone,
just appearing with reduced motion) and saving a key in Settings, then switching to that provider; the language panel
(each language in its own words, the keyboard, rising from the bottom on a phone); the gear and ⌘, / Ctrl+, opening
Settings while the model button doesn't; the browser tool's thumbnails; the web app manifest as Chrome itself reads it
(`Page.getAppManifest` with no errors, `Page.getInstallabilityErrors` empty, every icon fetched at its stated size, the
security policy still blocking every other image) and the title bar color following the chosen appearance; the
sandbox section in Settings (the level, the warning when this computer can't sandbox, the extra paths dialog) and the
sandbox choice in the task editor and the channel wizard; and editing and regenerating the last turn
(where the buttons sit, ↑ in the empty input box, Esc warning before throwing away an edit, the confirmation when the
turn ran a command, a turn that failed getting its own Regenerate, a second tab following along and keeping its
unsent edit). The demo answers these tests from scripts
in [tests/demo_scripts.py](../../tests/demo_scripts.py), chosen by a `[ui:name]` tag in a conversation's first message, so the
tests don't depend on each other's order. When a test fails, a screenshot, the console log and the page's HTML are saved
(the path is printed; CI uploads them as an artifact). The checks in [tests/ui/checks.py](../../tests/ui/checks.py) are
heuristics with lists of selectors to extend when a new panel arrives. What they can't judge (visual polish, how an
animation feels, real touch screens) is still checked by hand in a real browser against the demo.

To click around and look at the interface without spending anything, run `uv run python tests/demo_web.py`. It starts
the real server and page backed by a local fake model with prepared replies, keeps its data in a temporary directory, and
doesn't touch your keys or `data/`. It also comes with a few fake MCP servers already connected (one of them broken,
one disabled) and a few made-up skills, and **Import from other tools** reads made-up Claude Desktop, Claude Code,
Cursor, VS Code, Codex (TOML), Goose (YAML) and GitHub Copilot files, plus a deliberately broken Kiro file, from the
temporary directory, not yours.
A fresh demo directory also starts with an older conversation whose attachments predate the media library, so the
Media tab has something to show, including the "text only" Word file. The demo also starts a fake of every chat
platform: it prints a made-up token (or ID and secret) for each one to paste into the **Channels** wizard, and the
addresses of two bare-bones "phone" pages (one for Telegram, one with a platform picker for the others) where you can
send the pairing code, chat, and tap Allow / Deny. `--no-sdk` shows the page as if the Feishu and DingTalk SDKs
weren't installed. The browser is switched on in the demo: start a new conversation
with a message containing 演示商店 ("demo shop"), for example 用浏览器看看演示商店里有什么, and the assistant opens a small
shop page the demo serves on your machine in a real Chrome, asks before visiting it, reads it, clicks the search box,
types (asking again) and takes a screenshot. Add `--browser-window` to watch the Chrome window; without Chrome on the
computer, the assistant says it can't find one. Computer use is switched on in the demo too, backed by the fake desktop
(never your real screen): a message containing 演示电脑 ("demo computer") starts a scripted step with screenshots,
approval cards with their "don't ask again" buttons, a dangerous-key warning and the header indicator. A
conversation whose first message contains a tag such as `[ui:approve]` follows the matching script in `tests/demo_scripts.py` (the page tests use them): `[ui:approve]`
asks to run a command, `[ui:long]` writes a long reply, `[ui:todo]` adds a to-do with a very long title, `[ui:hello]`
greets you, and `[ui:flaky]` fails the first attempt at every message (so you can try Regenerate on a failed turn).
`--script NAME` makes every conversation follow that script even without a tag; the README screenshots were taken with
`--script readme`, which answers in English or Chinese depending on the question.

Every push to GitHub and every pull request runs the tests and the linter (ruff) on Ubuntu, macOS and Windows with
Python 3.11 and 3.13 ([.github/workflows/tests.yml](../../.github/workflows/tests.yml)), page tests included, using the Chrome
that comes with GitHub's runners, and the type checker (mypy) once for each of the three systems. On Ubuntu the workflow also installs Xvfb, `xdotool` and ImageMagick, so the computer
use integration test runs there against a virtual display, and bubblewrap, so the sandbox's real Linux backend is tested
there too. The result is the Tests badge at the top of the [README](../../README.md).

Tests that only make sense on one system say so when they're skipped: a launchd plist or a systemd unit can't be
installed from a Windows path, there are no file descriptors 3 and 4 or process groups on Windows, `icacls`, job objects
and the real PowerShell run only on Windows, and creating symbolic links on Windows needs administrator rights or
Developer Mode (those tests skip there with that reason). Everything else runs on all three.

## Releases

The version number lives only in `pyproject.toml`, and it changes only when a version is released, together with
`CHANGELOG.md`: `uv version 0.3.0` (which updates `uv.lock` too), and the `[Unreleased]` section renamed to
`## [0.3.0] - date`, with its link at the bottom of the file. [tests/test_release.py](../../tests/test_release.py) fails when
the version in `pyproject.toml` has no dated section and link, so a forgotten changelog shows up in the ordinary test run
rather than at release time.

Once that commit is on `main` and its tests are green, pushing the tag `v0.3.0` runs
[.github/workflows/release.yml](../../.github/workflows/release.yml):

1. **build** checks the tag against `pyproject.toml` and `CHANGELOG.md` with
   [tests/release_notes.py](../../tests/release_notes.py), which also writes the release notes, then builds the wheel and the
   source archive in a clean checkout.
2. **smoke** installs each of them on Ubuntu, macOS and Windows, in an environment with nothing else in it, and runs
   [tests/smoke_release.py](../../tests/smoke_release.py): every file under `harness/` made it into the package, the web page
   assembles, and `harness --version`, `--help` and `harness doctor --json` work.
3. **GitHub Release** creates the release with those notes and attaches both files.
4. **PyPI** publishes them with PyPI's Trusted Publishing, so no token is stored anywhere. It is skipped until the
   repository is set up for it (a trusted publisher on pypi.org, a `pypi` environment and the repository variable
   `PYPI_PUBLISH` set to `true`); ZhiFox isn't on PyPI yet.

The actions in this workflow are pinned to commit hashes rather than to tags such as `v4`, because the workflow can
write releases and publish packages; the job that can publish runs none of the repository's code.

The build and the smoke test also run on every push, as the `package` jobs of the ordinary tests, so a package that
doesn't install or start on one of the systems shows up long before a release.
