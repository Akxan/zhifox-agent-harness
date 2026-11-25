**English** | [简体中文](../zh-CN/sandbox-and-hooks.md)

[ZhiFox](../../README.md) › [Documentation](../../README.md#documentation) › Sandbox, secret masking and hooks

# Sandbox, secret masking and hooks

Safeguards on top of the approval prompts in [Tools and permissions](tools-and-permissions.md): an operating-system
sandbox for commands, masking keys that turn up in tool results, and hooks that run your own scripts at fixed moments.

## Sandbox

The sandbox for commands is off by default; what it does and why is in
[Sandbox for commands](#sandbox-for-commands-what-a-command-can-reach). To turn it on, open **Settings → Sandbox** and
pick a level for your conversations: **Working folder only** (no network; the one to use when you drive ZhiFox from your
phone) or **Working folder + network**. The section says which sandbox this computer uses (macOS `sandbox-exec`, Linux
bubblewrap) and, if it can't be used here, why and how to fix it; pick a level anyway and the warning turns red: every
command will be refused until it's fixed or the level is back to Off. **Manage paths** opens the list of extra paths a command may read,
or read and write, such as `~/.nvm` for the `node` that nvm installed. The level applies from the next command.
Scheduled tasks and chat channels follow it unless you pick another level in the task editor or the channel wizard.

While it's on, the approval card for each command has a line saying which level it runs in. When a command fails
because the sandbox blocked it, the assistant is told so; if it really needs to run that one command outside the
sandbox, you get a separate card with a warning, every time. On a server, the same settings are on the command line:
`harness sandbox`, `harness sandbox level workspace`, `harness sandbox allow <path> [--write]`, `harness sandbox forget
<path>`, `harness sandbox check` and `harness channels sandbox <platform> <level>`.

## Sandbox for commands: what a command can reach

`run_command` runs every command as you, so a command can reach whatever you can: your home folder, your SSH keys,
every other project, the network. The first safeguard is the approval card (see [Permissions](tools-and-permissions.md#permissions)): you see
each command and click Allow. But that safeguard gets loosened (**Allow read-only commands**, **Allow everything**), it
gets tapped through on a phone in a chat channel, and the model writing the command may have been steered by text
hidden in a web page or a file it just read (prompt injection). The sandbox is a second, independent safeguard: even
after a command has been allowed, while it runs it can only reach the places it should. Neither replaces the other:
loosening the approvals doesn't open the sandbox, and turning the sandbox on doesn't remove a single question. It is
optional and **off by default**. The code is in [harness/sandbox/](../../harness/sandbox/), one file per concern:

| File | What it does |
|---|---|
| [policy.py](../../harness/sandbox/policy.py) | The levels, and which paths a command may read, write or never touch. Both systems follow this one list |
| [session.py](../../harness/sandbox/session.py) | One run's sandbox: which level is in effect, whether this run may ask you to let a command out, its own HOME and temp folder, the wording on the approval card and in the note to the model |
| [macos.py](../../harness/sandbox/macos.py) | macOS: the SBPL profile for `sandbox-exec` |
| [linux.py](../../harness/sandbox/linux.py) | Linux: the arguments for bubblewrap (`bwrap`) |
| [windows.py](../../harness/sandbox/windows.py) | Windows: why there is no sandbox yet |
| [backend.py](../../harness/sandbox/backend.py) | Which of these this computer uses; the only place that looks at the operating system |
| [webui/sandbox_api.py](../../harness/webui/sandbox_api.py) | **Settings → Sandbox**: the level and the extra paths |

**What a sandbox is.** The operating system draws a line around one process and everything it starts: files outside
the line can't be read or written, and the network can't be reached. The kernel enforces that line, not ZhiFox, and that
is the point. ZhiFox could try to read each command and guess what it will touch, but any such check can be got around:
write it another way, put it in a script and run the script, `python -c`, base64. A rule enforced by the kernel applies
to whatever the command turns out to do.

**Three levels.** Choose one in **Settings → Sandbox**; it is the level for your conversations and the default for
scheduled tasks and chat channels:

| Level | What a command can do |
|---|---|
| **Off** (default) | Runs as before; only the approval prompt stands in the way |
| **Working folder only** (recommended for remote use) | Read and write the working folder; read the system folders programs need; nothing else in your home folder, no other drives, no `/tmp`; no network at all, not even `127.0.0.1` |
| **Working folder + network** | The same, plus the network: downloads, `git pull`, calling an API |

In detail, at either level:

- **Read and write:** the working folder; this run's own HOME and temp folder (below); the paths you added as
  read-write.
- **Read only:** the system folders programs need to run (each system lists its own: `/System`, `/usr`, `/bin`,
  `/Library`, `/Applications`, `/private/etc`, `/opt/homebrew`… on macOS; `/usr`, `/etc`, `/opt`, `/nix`… on Linux), and
  the paths you added as read-only.
- **Never, not even inside the working folder:** ZhiFox's data folder (`data/`: past conversations, settings, memory,
  bot tokens), `.env` with your API keys, and the other files the file tool protects (`mcp.json`, `channels.json`,
  `commands.json`). Also the places in your home folder that hold secrets: keys and password stores (`~/.ssh`,
  `~/.gnupg`, `~/.password-store`, the keyrings and keychains), cloud and container tools (`~/.aws`, `~/.azure`,
  `~/.config/gcloud`, `~/.kube`, `~/.docker`, `~/.oci`, doctl, `~/.boto`, `~/.s3cfg`, Cloudflare's `~/.cloudflared` and
  wrangler login, ngrok, `~/.fly`, MinIO's `~/.mc`, Terraform's `~/.terraform.d` and `~/.terraformrc`, `~/.vault-token`),
  git and code hosting (`~/.gitconfig`, `~/.config/git`, `~/.git-credentials`, `~/.config/gh`, hub, GitHub Copilot,
  `~/.netrc`), package managers (`~/.npmrc`, `~/.yarnrc.yml`, `~/.pypirc`, pip's `pip.conf`, `~/.m2/settings.xml` and
  `settings-security.xml`, `~/.gradle/gradle.properties`, Composer, `~/.cargo/credentials.toml`, `~/.gem/credentials`),
  and a few more (`~/.pgpass`, `~/.my.cnf`, 1Password CLI, rclone, Kaggle, the Hugging Face token, Codex's `~/.codex`).
  The full list, with where each one is documented, is in [harness/secret_places.py](../../harness/secret_places.py);
  the [read-only command check](tools-and-permissions.md#read-only-commands-and-why-the-check-is-conservative) uses the
  same list. Those home-folder places stay blocked unless you add that exact place (or something inside it) as an extra
  path. Why list them separately: the working folder may *be* your home folder (`harness serve --workspace ~` on a
  server), or you may add your whole home folder as an extra path; `~/.ssh` stays blocked anyway. A place that is a
  symlink (dotfiles managed with stow, say) is blocked where it really points too. Blocking them doesn't break programs
  inside the sandbox: they look under `$HOME` (git reads `$HOME/.gitconfig`), and HOME is the run's own folder (below).
  On Linux, one that doesn't exist yet is left alone rather than covered: there's nothing in it to read, and covering it
  would leave an empty read-only placeholder in your real home folder (after which `npm login` can't write `~/.npmrc` and
  Maven chokes on an empty `settings.xml`). The price is that when your whole home folder is writable, a command can
  create one of them, as with the startup files below. `~/.ssh` is the exception, covered with an empty folder even
  when it's missing: an `authorized_keys` planted there would let someone log in.
- **Read only, even where writing is allowed:** ZhiFox's own program (`harness/`, `.venv`, `pyproject.toml`,
  `uv.lock`, `.git`, `.python-version`, and the Python environment it runs from), and the files that run when you open a
  terminal or log in (`~/.bashrc`, `~/.zshrc`, `~/.profile` and friends, `~/.config/fish`, `~/.config/systemd`,
  `~/.config/autostart`, `~/Library/LaunchAgents`). By default the working folder is the project folder, so without this
  a sandboxed command could edit `harness/tools/shell.py`, and the next time ZhiFox starts, the edited code would run
  outside the sandbox. A changed `~/.zshrc` would do the same the next time you open a terminal.
- **Everything else** can't be reached: the rest of your home folder, other users, other drives, `/tmp`.

**Why HOME is an empty folder.** Commands still run as you, and programs such as git, npm and pip read their settings
from your home folder when they start (`~/.gitconfig`, `~/.npmrc`) and write caches there (`~/.npm`, `~/.cache`).
When the home folder can't be read, some of them simply quit: git stops with `fatal: unable to access
'/Users/you/.gitconfig': Operation not permitted`. So each run (your conversation, one task run, one chat) gets its own
empty HOME and TMPDIR inside the system's temp folder, shared by that run's commands and deleted when the run is gone.
git works (without your global name and email), and npm and pip download into caches there. `~` in a command means that
folder, so write the full path to reach a place you added. Variables that point into your real home folder
(`XDG_CONFIG_HOME`, `XDG_CACHE_HOME` and the other `XDG_*`, `SSH_AUTH_SOCK`) are removed. One more: `uv run` puts
ZhiFox's own `.venv/bin` first on `PATH`, and inside the sandbox it is taken off again. That is ZhiFox's program, not
your tools, and the Python behind it usually lives in your home folder (`~/.local/share/uv`) where the sandbox can't
start it; without it, `python3` is the system's or Homebrew's.

**Localhost counts as network.** At **Working folder only**, even `127.0.0.1` is out of reach. That's where
databases, Docker, other local services and ZhiFox's own web page listen, and a command that can talk to them can get
them to do what the sandbox wouldn't let it do itself. Unix sockets (Docker's socket, the ssh-agent) are blocked at
every level on macOS; on Linux they simply aren't inside the sandbox.

**Extra paths.** **Settings → Sandbox → Manage paths** adds read-only or read-write paths: a full path starting with
`/` or `~`, up to 50 of them (`SANDBOX_MAX_PATHS`), each up to 1,000 characters (`SANDBOX_PATH_CHARS`). Programs
installed in your home folder (nvm, pyenv, cargo's or uv's tools) can't be read in the sandbox by default: add their
folder as read-only, for example `~/.nvm`. Like every list of things stored on your computer, each row has **Show in
Finder**, **Edit** and **Delete** (confirmed in place). `data/` and `.env` can't be added: they'd have no effect.

**Scheduled tasks and chat channels.** Each agent task (in the task editor) and each chat channel (in the last step of
its wizard) has its own level, **Follow settings** by default, or **Off**, **Working folder only**, **Working folder +
network**. Tasks the assistant creates for you are always Follow settings; its tool has no way to change that, so
whether a task runs without the sandbox is your decision only. Your conversation follows Settings, and a change applies
from the next command. A channel's level is read again before every command; a task run keeps its own level from the
start of the run (Follow settings looks at Settings before every command). Subagents use exactly the sandbox of
whoever called them: they only ever get less.

**macOS: `sandbox-exec`.** macOS has a sandbox of its own (Seatbelt): App Store apps, Safari and many of the system's
services run inside it, and the profiles of those services are in `/System/Library/Sandbox/Profiles`. `sandbox-exec`
runs one command, and everything it starts, inside a profile written in SBPL (Sandbox Profile Language). ZhiFox writes
a profile for each command:

```
(version 1)
(deny default)                 ; nothing is allowed, then things are allowed one by one
(import "system.sb")           ; what any program needs: system libraries, /dev/null, the time zone…
(allow process-fork) (allow process-exec)
(allow signal (target same-sandbox))            ; can only signal processes in this sandbox
(allow file-read-metadata)                      ; whether a file exists and how big, never its content
(allow file-read* file-map-executable (subpath (param "SYSTEM_0")) …)              ; system folders
(allow file-read* file-write* file-map-executable (subpath (param "WRITE_0")) …)   ; working folder, HOME, temp
(deny file-write* (subpath (param "READONLY_0")) …)                                ; ZhiFox's program…
(deny file-read* file-write* (subpath (param "DENY_0")) …)                         ; data/, .env, ~/.ssh…
```

The deny rules come last because, when several rules match the same operation, the last one wins. Whatever isn't
listed stays denied: launching other apps (`open -a`), Apple Events that control other apps (`osascript -e 'tell
application …'`), the keychain, Unix sockets. At **Working folder + network** a few more lines allow IP connections
(127.0.0.1 included) and name lookups. Paths never appear in the profile's text: each is passed as a parameter
(`-D WRITE_0=/the/path`) and the profile says `(param "WRITE_0")`, so a quote, parenthesis, backslash or newline in a
file name can't change a rule, and there's no escaping to get wrong. Apple marks `sandbox-exec` as deprecated in its
man page, but it still works on current macOS, and the same machinery protects Apple's own services. If a future macOS
removes it, ZhiFox says the sandbox can't be used and refuses the commands (below). Inside the sandbox, a blocked
operation fails with `Operation not permitted`, and without the network you see `Could not resolve host` or
`Couldn't connect to server`. Programs with special privileges (setuid), such as `sudo` and `ps`, can't run there.
`/tmp` isn't writable either, because it's shared with every other program (the ssh-agent's socket lives there): use
`$TMPDIR`. To know whether `sandbox-exec` works at all, ZhiFox starts an empty sandbox and remembers the answer for a
minute (`SANDBOX_PROBE_TTL`); this fails, for example, when ZhiFox itself is already running inside another sandbox.

**Linux: bubblewrap (`bwrap`).** The Linux kernel has *namespaces*: a process can have its own view of the
filesystem, its own process numbers and its own network. Containers (Docker, Flatpak) are built on them, and bubblewrap
is the small tool Flatpak uses; it doesn't need root. It starts from an empty filesystem and mounts only what it's told
(`--ro-bind` read-only, `--bind` read and write), so everything else doesn't exist inside. The arguments:
`--unshare-pid` (its own process numbers: it can't see or kill anything outside, ZhiFox included), `--unshare-net` at
**Working folder only** (its own network with nothing but an empty localhost of its own, so both the internet and the
services listening on this computer's 127.0.0.1 are out of reach, while a server started inside the sandbox can be
reached from inside it), `--die-with-parent`, its own `/proc`, a minimal `/dev`, an empty `/tmp`; the system folders
read-only (`/bin`, `/lib` and friends recreated as links where they point into `/usr`); at **Working folder +
network**, the real `/etc/resolv.conf` (often a link into `/run` for systemd-resolved) so names resolve. Denied
folders are covered with an empty read-only `tmpfs` and denied files with `/dev/null`, mounted last so they cover what
came before. A missing `.env` in a writable place is covered too: otherwise a command could create one that ZhiFox
would read at its next start (bwrap leaves an empty file behind as the mount point, and an empty `.env` means the same
as none). Inside, a hidden place looks like `No such file or directory`, a read-only one like `Read-only file
system`, and a covered folder such as `data/` like an empty, read-only folder. Connecting to a service on this
computer gets `Connection refused`: the sandbox's own localhost has nothing listening. bwrap isn't installed by default, and ZhiFox never installs anything: without it the sandbox can't be used,
and ZhiFox gives the command for your distribution:

```bash
sudo apt install bubblewrap       # Ubuntu, Debian
sudo dnf install bubblewrap       # Fedora
sudo pacman -S bubblewrap         # Arch
```

No restart is needed afterwards. If bwrap is installed but can't create a sandbox, ZhiFox shows bwrap's own error. The
usual cause is that the system doesn't let normal users create user namespaces: on Ubuntu 24.04 and later AppArmor
restricts them (`kernel.apparmor_restrict_unprivileged_userns`), on Debian see `kernel.unprivileged_userns_clone`.

**Windows: not available yet**, by design rather than by accident. The options that need no new dependency were
weighed ([windows.py](../../harness/sandbox/windows.py) has the details): a job object (ZhiFox already uses one to end a
command with all its children) limits processes, memory and CPU time, not files or the network; a restricted token or
a low integrity level can't write most of your files but can still read them, `~/.ssh` included, and still reach the
network; an AppContainer comes closest (no access to your files, no network), but letting it read and write the working
folder means changing that folder's permissions (ACLs) on your disk, PowerShell can do little inside one, and
Python's `subprocess` can't start a process in one; Windows Sandbox is a virtual machine, only on Pro and Enterprise,
seconds to start, with no view of the working folder. So on Windows the sandbox says it can't be used, and to get one
you can run ZhiFox inside WSL (Linux on Windows) and install bubblewrap there. Other systems (FreeBSD…) have no sandbox
either.

**No silent fallback.** If the level in effect isn't Off and this computer can't sandbox (bwrap missing, Windows,
`sandbox-exec` failing), every command is refused before any approval card appears. The model gets the reason (in
Chinese, like every tool result) and is told to explain the fix to you, or to ask you to set the level to Off; Settings
shows the warning in red, and the task editor and the channel wizard warn too. A command is never run quietly outside
the sandbox: believing it's inside when it isn't is worse than having no sandbox at all.

**What you see.** With the sandbox on, the approval card adds a line with the level the command will run in and what
that means (read and write the working folder; network or not). A task or channel set to Off while the default is on
says so on its cards. If you never turned the sandbox on, the cards look exactly as before. In the capability list (and
the terminal's `/tools`), the **Run commands** row says which level the commands run in, or that the sandbox can't be
used here and commands will be refused; its rules include what happens to a command run outside the sandbox (asks
every time, or not allowed at all), and it has a **Sandbox** button that opens Settings at the right place.

**Letting one command out.** When the sandbox blocks a command that's really needed, the model can run it again with
`unsandboxed: true`. That one command:

- **asks you every time, in every approval mode, Allow everything included.** It gets a card of its own, with a
  warning that the command can read and write everything you can (home folder, keys, other drives) and use the
  network, and with the level the run normally uses. The card has neither the **Don't want to be asked every time?**
  link nor any "don't ask again": each one is looked at by itself;
- **can ask in your conversation** (web page and terminal), where you're watching;
- **can ask in a scheduled task or a chat channel only if that run's own approval mode isn't Allow everything.** With
  Allow everything you said "don't ask me", and most likely nobody is there to answer: a question would leave the run
  waiting, or teach you to tap Allow without reading. So it's refused, and the model is told to find a way inside the
  sandbox, or to ask you to change that task's or channel's sandbox level. In the other modes the run asks you anyway,
  so it asks about this one too;
- does nothing when the sandbox is off: the command is asked about like any other.

Why this way: what matters most about a second safeguard is that a model misled by a sentence in a web page can't get
itself out. The only way out is a person clicking on a card that shows that exact command, and in a run where you said
"don't ask", there is no door at all. The `unsandboxed` parameter is always in `run_command`'s description, sandbox on
or off, because a conversation's tool descriptions must not change (see [The loop](architecture.md#the-loop)); switching the sandbox
on or off mid-conversation changes only how commands run.

**When a command is blocked.** If a sandboxed command fails and its output carries one of the usual signs
(`Operation not permitted`, `Read-only file system`, `No such file or directory` on Linux, `Could not resolve host`…), a
note is added to the result for the model, in Chinese like every tool result: the command ran in the sandbox, at which
level, what that level allows, and what to do next (use files in the working folder, or, where it's allowed, retry with
`unsandboxed: true`).

**What it can't protect against.**

- **The working folder itself.** Inside it a command can delete or overwrite anything; the approvals and the file
  tool's backups are what cover that.
- **What gets left behind.** Scripts, git hooks and settings (`.git/hooks`, `.git/config`), `package.json` scripts,
  Makefiles written into the working folder run as you, outside the sandbox, whenever you run them later yourself.
- **The network level.** At **Working folder + network**, what's in the working folder can be sent anywhere.
- **What you let out.** A command you allowed to run outside the sandbox, and tasks or channels set to Off.
- **Bugs in the operating system.** The kernel enforces the line; a kernel bug can break it.
- **Names and sizes on macOS.** A command can still learn that a file exists and how big it is (`file-read-metadata`
  is allowed everywhere so that programs can resolve paths), never what's inside.
- **Resources.** The sandbox doesn't limit CPU, memory or disk space; a runaway command can still use them all (the
  command timeout and **Stop** still work).

**From the command line** (handy on a server with no web page):

```bash
uv run harness sandbox                     # the status: level, this computer's sandbox, extra paths
uv run harness sandbox level workspace     # off | workspace | network
uv run harness sandbox allow ~/.nvm        # an extra read-only path; add --write for read and write
uv run harness sandbox forget ~/.nvm
uv run harness sandbox check               # runs a few harmless probes inside the sandbox and reports what got through
uv run harness channels sandbox telegram workspace   # default | off | workspace | network for one channel
```

The commands are the same in PowerShell. Where things are stored: the level and extra paths in `data/settings.json`
(`"sandbox": {"level", "paths"}`), a task's level with the task in `data/tasks.json`, a channel's in
`data/channels.json`. The web endpoints ([harness/webui/sandbox_api.py](../../harness/webui/sandbox_api.py)), all behind the
page's token: `POST /api/sandbox/level`, `/api/sandbox/add`, `/api/sandbox/edit`, `/api/sandbox/delete` and
`/api/sandbox/reveal` (it shows only a path that is already on the list), `/api/channel/sandbox`, and the `sandbox`
field of `/api/task/save`. The tunables in [harness/config.py](../../harness/config.py): `SANDBOX_MAX_PATHS` (50),
`SANDBOX_PATH_CHARS` (1,000), `SANDBOX_PROBE_SECONDS` (10) and `SANDBOX_PROBE_TTL` (60).

## Masking secrets in tool results

A command's output, a file the assistant views, an MCP server's answer or a web page can carry a secret by accident:
`cat config.yaml`, `env`, `git remote -v` with a token in the URL, a log line with `Authorization: Bearer …`. Once it is in
the conversation it is sent to the model provider, written to the conversation file, shown on the page and in chats, and
exported, and the history is append-only, so it can't be taken back. So ZhiFox masks it the moment the result is created,
before it is added to the results, before your `PostToolUse` hooks see it and before the `tool_ended` recording point in
[harness/agent.py](../../harness/agent.py): the model, the history, the saved conversation, the page, the chat channels, the
run trace and your hooks all see the masked copy. Nothing earlier in
the history is touched. The masking lives in one module, [harness/redact.py](../../harness/redact.py), and catches three kinds of thing:

1. **Known key formats**: Anthropic, OpenAI, DeepSeek, Gemini/Google, GitHub, Slack, Telegram and Discord bot tokens, AWS
   access keys, Stripe live keys, JWTs and PEM private keys. The export ([harness/export.py](../../harness/export.py)) and the MCP
   settings page use the same table, so a key format is written down in one place.
2. **Known values**, compared by value and never logged: the variables in `tools/shell.SECRET_ENV_VARS`, anything in the
   environment or in `.env` whose name looks like a secret (`…_TOKEN`, `…_SECRET`, `…_PASSWORD`, `…_API_KEY`), the keys of
   your custom model endpoints, and the bot tokens hidden from the log. The mark says which variable it was
   (`‹已遮住：OPENAI_API_KEY 的值›`), never the value.
3. **Random-looking values after a hint**: `password=`, `token:`, `"api_key": "…"`, `--token …`, `Authorization: Bearer …`,
   and the password in `scheme://user:password@host`. A value counts only if it is long enough, has letters and digits and
   enough entropy (`REDACT_MIN_VALUE_CHARS`, `REDACT_MIN_ENTROPY` in [harness/config.py](../../harness/config.py)).

A secret becomes `‹已遮住：疑似 OpenAI 密钥›` ("masked: looks like an OpenAI key"), and the result ends with a note telling
the model how many were masked and not to write the marks back. Those marks are model-facing, so they stay in Chinese like
every other tool result. The run trace records how many were masked on each tool call (`redacted`, shown as
"masked 2 secrets" in the timeline and in `/trace`).

It is deliberately conservative: false positives break real work. Git commit hashes and UUIDs are never masked, and
neither are long base64 runs (`REDACT_DATA_RUN`, 200 characters) or `data:` URLs, such as an image's bytes. Code
(`request.headers.get`, `process.env.API_KEY`), placeholders (`${API_KEY}`, `<your-key>`, `****`) and paging tokens
(`next_page_token`) are left alone too. The file tool refuses to write a mark back into a file (`create`,
`str_replace` or `insert` whose text contains a mark that isn't already in the file), because that would replace the
real key with the mark. It asks the model to change only the other lines.

It is a safety net, not a wall: a model that deliberately works around it (re-encoding a key before printing it) won't be
caught. The real protections are still that `.env` and `data/` are off limits and that commands never see the providers'
keys. It is on by default; **Settings → Mask secrets** turns it off (asking once first), and the setting is
`"redact_secrets": false` in `data/settings.json` (`POST /api/redact {"enabled": …}`). The terminal, scheduled tasks, chat
channels and subagents all use the same switch.

## Hooks: your own scripts at fixed moments

Approval cards let you decide one step at a time. Some rules you'd rather write once and have applied every time:
"never delete anything in `~/Documents`", "run the formatter after every Python edit", "log every finished turn",
"send me a notification when a scheduled task is done". A **hook** is a small script of yours that ZhiFox runs at a
fixed moment and listens to. It follows [Claude Code's hooks](https://code.claude.com/docs/en/hooks): the same event
names, the same idea of a matcher, the same JSON on stdin, and the same answers through the exit code and a JSON object
on stdout. A Claude Code hook usually needs only its tool names changed (`run_command`, `str_replace_based_edit_tool`
instead of `Bash`, `Edit`).

| Event | When | What the hook can do |
|---|---|---|
| `PreToolUse` | A tool is about to run, **after** approval (you clicked Allow, or the mode doesn't ask) | Block it: the call still gets a result, and the model reads "this call was blocked by the user's hook …" with your reason. Or ask you once more, in any mode. It can't change the input, and it can't make a step that asks stop asking |
| `PostToolUse` | The tool has finished | Append a short note (at most `HOOK_NOTE_CHARS`, 2,000 characters) to this tool result, which the model sees. It sees the result exactly as the model will |
| `UserPromptSubmit` | You sent a message, before it enters the history | Block it (you see the reason; nothing is sent, nothing is paid), or attach context to this message for the model |
| `Stop` | A turn finished (answered, cut off, refused, out of steps) | Only a notification: run a formatter, write a log, send a notification. Not run when you pressed Stop, an error ended the turn, or a hook blocked the message |

**The protocol.** The hook receives one JSON object on stdin: `hook_event_name`, `hook_name`, `session_id`, `cwd`,
`permission_mode` (`ask` / `readonly` / `edits` / `all`), `source` (`chat`, `task` or `channel`), `provider`, `model`,
`agent_type` (only inside a subagent) and `test` (only for a test run); the tool events add `tool_name`, `tool_group`
(the capability it belongs to, as in the capability list), `tool_input`, `tool_use_id`, `mcp_server` and, for `run_command`,
`command_cwd` (the folder the command runs in, which an earlier `cd` may have changed; `cwd` stays the working folder); `PostToolUse`
adds `tool_response`, `is_error` and `duration_ms`; `UserPromptSubmit` adds `prompt` and `attachments`; `Stop` adds
`last_assistant_message`, `ended` and `stop_hook_active` (always false). Then:

- **exit 0**: fine. Stdout may be a JSON object with Claude Code's fields: `{"decision": "block", "reason": "…"}`,
  `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny" | "ask", "permissionDecisionReason": "…"}}`,
  `{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "…"}}` (or `UserPromptSubmit`), and
  `{"systemMessage": "…"}` for a line shown to you. Plain text on stdout counts as context only for `UserPromptSubmit`.
  `"allow"` is ignored: a hook can make approvals stricter, never looser. So are `updatedInput`, `updatedToolOutput` and
  `continue: false`; a test run tells you so.
- **exit 2**: block, with stderr as the reason (for `PostToolUse` it's appended to the result, for `Stop` it's shown to you).
- **anything else**, a timeout (`HOOK_TIMEOUT`, 30 seconds by default, up to `HOOK_MAX_TIMEOUT`), or a program that won't
  start: the hook failed. You get a notice and everything carries on, unless the hook says `"onFailure": "block"`, which
  turns a failure into a block for `PreToolUse` and `UserPromptSubmit`: a safety hook that breaks fails closed.

Several hooks on the same moment run one after another in file order (Claude Code runs them in parallel), and the
first one that blocks wins. Every hook run is a span in the [run trace](architecture.md#reading-a-turn-the-run-trace). When
[several reads in one step run at the same time](tools-and-permissions.md#several-reads-in-one-step-run-at-the-same-time), hooks still never
overlap: a call that any `PreToolUse` hook matches runs on its own, and `PostToolUse` runs once per call, in call order,
after that call's result.

**Where hooks run.** In your conversations, in scheduled tasks and in chat channels, all three by default; the
`runsIn` field narrows it. Why on by default in unattended runs: a hook can only make things stricter, so running it
more often never makes anything less safe, while a safety hook that silently didn't apply in a scheduled task would fail
exactly when nobody is watching. A subagent's tool calls run the tool hooks too (with `agent_type`); its "turn" isn't a
message you sent, so it runs no `UserPromptSubmit` or `Stop`. Regenerating a turn doesn't rerun `UserPromptSubmit`:
the message stays in the history as it was.

**The history stays append-only.** A hook's note goes into the tool result or the user message that is being created
at that moment, wrapped as `<hook_note name="…">` or `<hook_context name="…">`; nothing already in the history changes,
and the system prompt and the tool list don't change either.

**Safety.** Saving a hook means ZhiFox will run that command on your computer, so hooks follow the rules of local MCP
servers: only the page holding the run's token (**Extensions → Hooks**) or the command line (`harness hooks …`) can save
one; the command is started from an argument list in the working folder, never through a shell (a `command` with
spaces and no `args` is refused with an explanation); the model API keys and bot tokens are removed from its
environment, and a hook that references one (`$ANTHROPIC_API_KEY`, `${…}`, `%…%`, `$env:…`) is refused when saved and
again before it runs; the model has no tool that adds or changes hooks, and `data/hooks.json` is on the file tools'
protected list like `.env` and `mcp.json`; the file is mode 600, masked in exports, and the page shows arguments with
anything that looks like a token hidden. Each run's hook is its own: Stop in the chat ends only the chat's hook, and
stopping a task ends only that task's. When the [command sandbox](#sandbox-for-commands-what-a-command-can-reach) is on, a hook runs in the same
sandbox as that run's commands, at the same level: your script is yours, but the JSON it reads carries the model's tool
input and tool output, and a careless script (one that pastes an argument into a command) would otherwise lend its
hands to a model a web page has talked round. The hook's own program and script files (absolute paths) are readable
inside; anything else outside the working folder (a log file, the network for a notification) needs a path added in
the sandbox settings or the "Working folder + network" level. If the sandbox is on but unavailable on this computer,
the hook doesn't run at all (it counts as a failure, so `onFailure` applies), never quietly outside the sandbox.

```json
{
  "hooks": {
    "no-rm": {"event": "PreToolUse", "matcher": "shell", "command": "python3", "args": ["/Users/me/hooks/no_rm.py"],
              "timeout": 10, "onFailure": "block"},
    "fmt":   {"event": "PostToolUse", "matcher": "files", "command": "python3", "args": ["scripts/format.py"]},
    "done":  {"event": "Stop", "command": "python3", "args": ["/Users/me/hooks/notify.py"], "runsIn": ["task"]}
  }
}
```

The matcher is a capability name (`shell`, `files`, `browser`, `mcp`… as in the capability list), a tool's real name
(`run_command`, `browser.left_click`, `mcp__github__create_issue`), or a `*` glob (`mcp__github__*`), separated by `|`;
empty or `*` means every tool. No regular expressions: enough for real use, and no way to write one that hangs.
Unlike Claude Code's settings, which group hooks by event, the file is flat, one named hook per entry, so the page can
switch, edit and test each one. On the command line:

```bash
uv run harness hooks add no-rm --event PreToolUse --matcher shell --on-failure block -- python3 /Users/me/hooks/no_rm.py
uv run harness hooks list            # enable / disable / remove / test NAME
```

```powershell
uv run harness hooks add no-rm --event PreToolUse --matcher shell --on-failure block -- python C:\Users\me\hooks\no_rm.py
```
