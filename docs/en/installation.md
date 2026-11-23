**English** | [简体中文](../zh-CN/installation.md)

[ZhiFox](../../README.md) › [Documentation](../../README.md#documentation) › Installation and updates

# Installation and updates

The install commands are in the [quick start](../../README.md#quick-start). This page has the rest: updating and
removing a one-command install, installing a released version, where your data and keys are kept, being told about new
versions, and starting ZhiFox from another folder.

## More on the one-command install

Updating and removing (the package is called `harness-assistant`; the command it installs is `harness`):

```bash
uv tool upgrade harness-assistant      # update to the latest version on GitHub
uv tool uninstall harness-assistant    # remove the program; your data folder stays where it is
```

If ZhiFox runs as a [background service](background-and-servers.md#run-it-in-the-background), run `harness service restart` after updating.
ZhiFox can also tell you when a new version is out, and which of these lines fits your install: see
[Updates](#updates) (off by default).

**Installing a released version.** The lines in the [quick start](../../README.md#install-with-one-command) install the latest code on `main`. Each
[release](https://github.com/Akxan/zhifox-agent-harness/releases) also has a ready-built package attached, which
installs exactly that version and needs no Git, on macOS, Linux and Windows alike:

```bash
uv tool install https://github.com/Akxan/zhifox-agent-harness/releases/download/v0.2.0/harness_assistant-0.2.0-py3-none-any.whl
```

With Git, `uv tool install git+https://github.com/Akxan/zhifox-agent-harness@v0.2.0` does the same from the release's
tag. An install made either way stays at its version (`uv tool upgrade` says there is nothing to upgrade): to move to
another release, run its line with `--force` added. `harness --version` shows which version you have (and, in a clone,
which commit).

**Where your data and keys are.** Installed this way, the program lives in uv's own folder, which `uv tool upgrade` and
`uv tool uninstall` replace and delete. So your conversations, memory and settings are kept outside it, in a folder of
your own account, and the API keys (`.env`) are in that same folder:

| System | Data folder, with `.env` inside it |
|---|---|
| macOS | `~/Library/Application Support/ZhiFox/` |
| Linux | `$XDG_DATA_HOME/zhifox/`, by default `~/.local/share/zhifox/` |
| Windows | `%LOCALAPPDATA%\ZhiFox\`, for example `C:\Users\you\AppData\Local\ZhiFox\` |

The folder is readable by your account only, as `data/` is in a clone (see [Data and privacy](data-and-privacy.md)). The
easiest way to add a key is the web UI's **Settings**, which writes `.env` there; you can also create the file
yourself with lines such as `ANTHROPIC_API_KEY=…` (see [.env.example](../../.env.example)). Being in the data folder doesn't
make `.env` any easier to reach: the assistant's file tool can't open it, the command sandbox can't reach it, and
**Export all data** leaves it out, the same as in a clone. `HARNESS_DATA_DIR` and `HARNESS_ENV_FILE` still move
either one somewhere else.

Why these folders: on Windows it's the *local* AppData, not the roaming one, because a roaming profile copies the whole
folder at every sign-in and sign-out, and ZhiFox's data (attachments, the browser profile, backups) is large and partly
tied to this computer. On Linux, `.env` stays in the data folder rather than going to `~/.config`, which many people
keep in a public dotfiles repository.

**Backing up.** **Settings → Export all data** makes a zip of the data folder without your keys (see
[Backup and restore](data-and-privacy.md#backup-and-restore)). Copying the whole folder also works, but that copy includes `.env`, so keep
it private.

**How it differs from a clone:**

- Commands drop the `uv run` in front: `harness --web`, `harness service install`, `harness channels setup telegram`.
  Wherever these pages write `uv run harness …`, type `harness …`.
- You don't need the `zhifox` shortcut from [Using it in another folder](#using-it-in-another-folder): `harness`
  already works in any folder, and the folder you start it in is the assistant's working folder.
- The data folder is the one in the table, not a project's `data/`, so a clone and an installed ZhiFox on the same
  computer don't share conversations or keys. To move from a clone, quit ZhiFox, then copy what's inside the clone's
  `data/`, and its `.env`, into that folder (or set `HARNESS_DATA_DIR` and `HARNESS_ENV_FILE` to the clone's).
- Feishu and DingTalk need their SDK, installed by reinstalling with the extra:
  `uv tool install --force "harness-assistant[feishu,dingtalk] @ git+https://github.com/Akxan/zhifox-agent-harness"`
  (the web page and `harness channels setup` print the exact line for what you have).
- [Evals](evals.md) need a clone: the tasks and the fake servers of the dry run live
  in the repository, not in the installed package. `harness eval` says so.

## Updates

How to update depends on how you got ZhiFox: in a clone, `git pull` and then `uv sync` in the project folder; installed
with one command, see [Install with one command](#more-on-the-one-command-install). A ZhiFox that is already running keeps the
old code until you restart it (`harness service restart` for the background service).

**Being told about new versions (off by default).** ZhiFox can check once a day whether a newer release is out. It stays
off until you turn it on, in **Settings → Check for updates** (the switch *Tell me when there's a new version*) or from
the command line:

```bash
harness updates on       # turn it on, and check once right away (in a clone: uv run harness updates on)
harness updates          # what the last check found (same as harness updates status); doesn't go online
harness updates check    # ask GitHub once, now
harness updates off
```

The lines are the same in PowerShell. Exactly what it sends: at most once every 24 hours (three hours after a failed
attempt), one request,

```
GET https://api.github.com/repos/Akxan/zhifox-agent-harness/releases/latest
```

with no login, no cookies, and a User-Agent of just `ZhiFox` (no version number, no system). GitHub sees your IP
address and the time of the request. Nothing else is sent: not your version, settings, conversations or keys, and no ID
that links one check to the next. It is the only request ZhiFox makes on its own; everything else goes out because you
asked for it (a message, **Sync models**, a chat channel you connected). That's why it's off until you say so. Drafts and
pre-releases are ignored, and turning it on checks right away.

Who checks: the web UI and `harness serve`, in a background thread. The terminal UI only reads the last result when it
starts; if the setting is on and that result is more than a day old, it checks in the background without delaying the
prompt, and tells you before your next input. `harness doctor` shows what the last check found and never goes online.
The result is kept in `updates.json` in the data folder (when it checked, the newest release, why the last attempt
failed). It's only a cache: **Export all data** leaves it out, a restore never writes it, and deleting it is harmless.

When a newer version is known, the settings gear gets a small dot, and **Settings → Check for updates** shows the
version, a link to its release page, and the commands for *this* install:

| How ZhiFox was installed | What it tells you to run |
|---|---|
| A clone (`git clone`) | `git pull`, then `uv sync`, in the project folder. A folder without `.git` (a downloaded zip) is told to download the new source instead |
| `uv tool install git+https://…`, no version | `uv tool upgrade harness-assistant` |
| `uv tool install git+https://…@v0.2.0`, pinned to a version | `uv tool install --force git+https://github.com/Akxan/zhifox-agent-harness@v0.3.0` |
| The release's wheel or a zip, or it can't tell | `uv tool install --force https://github.com/Akxan/zhifox-agent-harness/releases/download/v0.3.0/harness_assistant-0.3.0-py3-none-any.whl`, which works for any install. When it can't tell how you installed, it says so plainly and mentions `uv tool upgrade` as the other way |

It tells them apart by reading `direct_url.json`, the standard file (PEP 610) that uv and pip write into a package
installed from an address. Each command is on a line of its own, never joined with `&&` (Windows PowerShell 5 doesn't
accept it), so they're the same in zsh, bash and PowerShell. Running as a background service, it adds
`harness service restart`. On Windows it tells you to stop the running ZhiFox first (`harness service stop`, then
`harness service start` afterwards): Windows can't replace the files of a program that is running.

If a check fails (offline, a timeout, GitHub's limit of 60 requests an hour per IP address for requests without a login,
an answer it can't read), ZhiFox writes one line in its log, Settings says it couldn't check and why, and nothing else
is held up; it tries again three hours later. The rules are in [harness/updates.py](../../harness/updates.py). The address
can be changed only with the `HARNESS_UPDATE_API` environment variable (`https://` only, or plain `http://` to this
computer), which the tests and the demo point at a local fake: in `uv run python tests/demo_web.py`, turn the switch on
to see the new-version state without any network (`--release 0.9.0` picks the version the fake announces; by default
it's one minor version above yours).

## Using it in another folder

The assistant's working directory is the directory you start it from. Installed with one command, `harness` already
works in any folder: `cd` there and run `harness` (or `harness --workspace /some/folder`). The rest of this section is
for a clone. To have it work somewhere else, `cd` there first, then run:

```bash
uv run --project "/full/path/to/this/project" harness
```

The keys and data still come from the project's own `.env` and `data/`. To type just `zhifox` anywhere, add a shortcut
to your shell's startup file:

```bash
# macOS and Linux: ~/.zshrc (or ~/.bashrc), then open a new terminal
alias zhifox='uv run --project "/full/path/to/zhifox-agent-harness" harness'
```

```powershell
# Windows: your PowerShell profile. `notepad $PROFILE` opens it (create it first with
# `New-Item -ItemType File -Path $PROFILE -Force` if it doesn't exist), then open a new PowerShell window
function zhifox { uv run --project "C:\full\path\to\zhifox-agent-harness" harness @args }
```

Then `zhifox`, `zhifox --web` and `zhifox service status` work from any folder (`@args` passes the rest of the line on).
