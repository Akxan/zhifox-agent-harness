**English** | [简体中文](../zh-CN/configuration.md)

[ZhiFox](../../README.md) › [Documentation](../../README.md#documentation) › Configuration and troubleshooting

# Configuration and troubleshooting

## Configuration

Set these in `.env`. All of them are optional:

| Variable | Default | Meaning |
|---|---|---|
| `HARNESS_MAX_SEARCHES` | `5` | Maximum web searches per message, and page reads (Claude's web fetch, and ZhiFox's `web_fetch` for the other models); `0` means no limit |
| `HARNESS_WEB` | `1` | Set to `0` to turn off web search on Anthropic's servers completely. Claude then gets ZhiFox's `web_fetch` instead (switch that off in **Settings → Read web pages**) |
| `HARNESS_EFFORT` | `medium` | How hard Claude thinks: `low` / `medium` / `high` / `xhigh` / `max` |
| `HARNESS_DATA_DIR` | The project's `data/`; installed with one command, the folder of your account (see [Install with one command](installation.md#more-on-the-one-command-install)) | Where memory, to-dos, settings and other data are stored |
| `HARNESS_PROVIDER`, `HARNESS_MODEL` | `anthropic`, `claude-opus-5-5` | Which model to use before you've chosen one in the interface. Once you have, your choice takes precedence |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Where voice messages are sent for transcription with your OpenAI key (and where OpenAI's SDK sends chat requests). Only `https://`, or plain `http://` to this computer |
| `HARNESS_CHROME` | Found automatically | Full path of the Chrome (or Chromium, Edge, Brave) to use for browser use. A path entered in **Settings → Browser** takes precedence |
| `HARNESS_UPDATE_API` | `https://api.github.com` | Where the [update check](installation.md#updates) asks for the latest release (the tests and the demo point it at a local fake). Only `https://`, or plain `http://` to this computer |

## Troubleshooting: `harness doctor`

When something doesn't work, run the self-check first. It shows on one screen how ZhiFox is set up on this computer,
and it's the quickest way to tell us what's different on yours, especially on Windows, which is tested on GitHub's
runners but not yet on a real desktop. In the project folder:

```bash
# macOS and Linux (zsh, bash)
uv run harness doctor                       # a table to read
uv run harness doctor --json > doctor.json  # for scripts, or to attach when you ask someone for help
echo $?                                     # 1 if anything is a problem, 0 otherwise
```

```powershell
# Windows (PowerShell)
uv run harness doctor
uv run harness doctor --json | Out-File -Encoding utf8 doctor.json
$LASTEXITCODE
```

In the terminal UI, type `/doctor`. Each line is in one of three states: **OK**, **Problem**, or **Not used** (a feature
you haven't set up isn't a problem). Every problem comes with a fix written for your shell: zsh on macOS, bash on Linux,
PowerShell on Windows. A data folder others can read, for example, gets `chmod 700 '…/data'` on macOS and Linux, and on
Windows the same `icacls '…\data' /inheritance:r /grant:r '*S-1-5-21-…:(OI)(CI)F' …` command ZhiFox uses itself.

| Section | What it looks at |
|---|---|
| Environment | Python's version and path (and whether it's the project's `.venv`), ZhiFox's version and git commit, whether a newer release is out (from the last [update check](installation.md#updates)'s saved result, never online), the system, whether `uv` is on the PATH |
| Folders | The working folder is writable; `data/` and `.env` can be read by your account only: 700 and 600 on macOS and Linux (and owned by you, not by root after a `sudo` run), the access list on Windows (read with `icacls /save`, never changed) |
| In use | Whether another ZhiFox holds the data folder (the web UI, `harness serve`, the background service, `channels pair`) and its process ID |
| API keys | Which providers have a key and where it comes from (`.env`, an environment variable, the Anthropic login); an empty variable hiding the key in `.env`; a variable that overrides a different key in `.env`; the selected model; custom endpoints |
| This computer | Chrome found or not; whether the command sandbox works here (and what wants it: the chat, a task, a channel); computer use: the backend and, on macOS, the Screen Recording and Accessibility permissions (preflight checks only, nothing pops up); the background service: installed, running, crashed, or missing keys that are set only in your shell |
| Extensions | MCP servers, chat channels, hooks and scheduled tasks: how many and how many are on; MCP servers and hooks whose program isn't installed (`npx`, `uvx`…); channels not filled in, not paired, or missing their SDK; a tasks file that can't be read |
| Network | Proxies and changed API addresses (scheme, host and port only), a plain `http://` address outside your network; web search on or off |

It only looks. It changes no files, permissions or settings, starts nothing (no MCP server, no Chrome, no screenshot),
and sends nothing over the network. Keys are reported as present or missing, never the key or its last characters, and
the `--json` output has no keys, tokens or tokenized addresses. It does contain your paths, which usually include your
user name.
