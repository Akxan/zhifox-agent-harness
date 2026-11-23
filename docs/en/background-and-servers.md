**English** | [简体中文](../zh-CN/background-and-servers.md)

[ZhiFox](../../README.md) › [Documentation](../../README.md#documentation) › Background service and servers

# Background service and servers

Keeping ZhiFox running when no terminal is open: as a service on your own computer, or with `harness serve` on a server.

## Run it in the background

Started with `uv run harness --web`, ZhiFox is a child of that terminal: close the terminal, or end the Claude Code
session that started it, and ZhiFox stops too, taking the web page and your chat bot with it. To keep it up all the
time, hand it to the program your operating system already uses for that job, its *service manager*: **launchd** on
macOS, **systemd** on Linux, **Task Scheduler** on Windows. One command does it:

```bash
uv run harness service install     # install and start it: from now on it starts at login and restarts itself after a crash
uv run harness service open        # open its web page (reads the address with its token for you)
uv run harness service status      # running or not, process id, port, folders, the log, recent warnings and errors
uv run harness service logs -f     # follow the log (Ctrl-C stops following); -n 200 shows more lines
uv run harness service stop        # stop it now (it starts again at your next login)
uv run harness service start       # start it again
uv run harness service restart     # restart it, for example after editing .env or updating ZhiFox
uv run harness service uninstall   # stop it and don't start it at login any more (data and logs stay)
```

From a clone, run them in the project folder; installed with one command, drop the `uv run` and run them anywhere
(`harness service install`). The service runs the same Python that runs ZhiFox (the clone's `.venv`, or the
environment uv made for the tool), so `uv tool upgrade harness-assistant` followed by `harness service restart`
switches it to the new version without reinstalling the service. `install` takes `--no-web` (chat channels and scheduled tasks only, no web page),
`--port 9000` (default 8765; when 8765 is taken, a free port is picked) and `--workspace /some/folder` (the assistant's
working folder; default: the current folder). Running `install` again is safe: the same settings change nothing, and new
settings stop the old service and start the new one. **Settings → Run in the background** on the web page shows which
way ZhiFox is running, with the install command to copy when it's tied to a terminal.

**Switching from a ZhiFox you started by hand.** Only one ZhiFox can use a data folder at a time (the terminal UI
counts too, since it saves conversations: type `/exit` there), so stop that one first, then install:

```bash
# In the terminal where `uv run harness --web` is running, press Ctrl-C.
# Started from a Claude Code session? End that session, or find the process and stop it:
pgrep -fl "harness --web"          # prints the process id
kill -INT <pid>                    # the same as pressing Ctrl-C: ZhiFox saves and exits
cd /path/to/zhifox-agent-harness
uv run harness service install
uv run harness service open
```

If the data folder is still in use, `install` refuses and says who holds it (what kind of ZhiFox and its process id),
rather than starting a service that would exit at once and be restarted every ten seconds. It works the other way
round too: running `uv run harness --web` while the service runs says that ZhiFox is running as a background service,
with `uv run harness service open` to reach it and `uv run harness service stop` to stop it first.

On Windows, stop a ZhiFox started by hand with Ctrl-C in its PowerShell window, or find it with
`Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object CommandLine -like '*harness*' | Select-Object ProcessId, CommandLine`.

### On Windows: Task Scheduler

`uv run harness service install` creates a task named `ZhiFox-<your user name>` in Task Scheduler (it's a hidden task:
**View → Show Hidden Tasks** to see it). It starts when you sign in, runs as you with no stored password and no
administrator rights, and has no time limit. All the commands above work the same way.

- **Restarting after a crash.** Task Scheduler's own "restart on failure" only fires when the task fails to *start*,
  not when the program exits with an error. So the task runs a small supervisor
  ([service/supervise.py](../../harness/service/supervise.py)) under `pythonw.exe` (no console window), and the supervisor
  runs ZhiFox: it starts it again 10 seconds after an error exit, and not after exit code 2 (a configuration problem
  that a restart can't fix; see the log). Task Scheduler's restart-on-failure stays on as a second line of defence.
- **Stopping cleanly.** Ending the task in Task Scheduler kills the process at once. `uv run harness service stop`
  instead leaves a stop request in the data folder; the supervisor closes ZhiFox's input, and ZhiFox stops what's running,
  saves, and exits. Only if that takes longer than 30 seconds does it fall back to ending the task.
- **The log** is `data\logs\service.log`, 1 MB per file with three older ones kept (`.1` to `.3`), the same rotation
  as on macOS. `uv run harness service logs -f` follows it without needing `tail`.
- **Environment.** A task can't set environment variables, so the data folder and the `.env` location are passed to the
  supervisor as arguments (only paths, never a key). The keys are read from `.env` when ZhiFox starts, as on the other
  systems. A path containing `%` can't be used (Task Scheduler expands `%NAME%`).
- **When it stops.** The task runs as you, so it stops when you sign out; signing out or shutting down ends it without
  a clean stop.

### How launchd and systemd keep it running

Both read one small settings file per program and look after that program:

| | macOS: launchd | Linux: systemd |
|---|---|---|
| The file | `~/Library/LaunchAgents/io.github.akxan.zhifox.plist`, a property list (XML) | `~/.config/systemd/user/zhifox.service`, a unit (INI style) |
| Start at login | `RunAtLoad`: started as soon as launchd reads the file, and it reads that folder at every login | `WantedBy=default.target` plus `systemctl --user enable`: started with your user session |
| Restart after a crash | `KeepAlive` with `SuccessfulExit: false`: restart unless it exited with code 0 | `Restart=on-failure`: the same rule |
| Pause between restarts | `ThrottleInterval`: 10 seconds | `RestartSec=10` |
| Logs | `StandardOutPath` and `StandardErrorPath`: `data/logs/service.log` | the journal: `journalctl --user -u zhifox` |
| Commands ZhiFox runs | `launchctl bootstrap gui/<uid> <plist>`, `bootout`, `kickstart -k`, `list` (before macOS 10.10: `load`, `unload`) | `systemctl --user daemon-reload`, `enable --now`, `stop`, `restart`, `disable --now`, `show` |

"Restart only after a failure" is what lets `stop` work: on SIGTERM, ZhiFox finishes what it's doing, saves and exits
with 0, so the manager doesn't bring it straight back, while a crash, a `kill -9` or an error exit is restarted. Exit
code 2 means a broken setting, such as a `channels.json` that can't be read; restarting can't fix that, so systemd
doesn't retry it (`RestartPreventExitStatus=2`), and `status` explains each exit code. A LaunchAgent runs while you're
logged in: it starts when you log in, not at boot. While the Mac sleeps, ZhiFox sleeps too, and it picks up waiting
messages when the Mac wakes.

The plist for a project in `/Users/you/zhifox` (condensed here; the real file has one key or value per line, sorted):

```xml
<dict>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HARNESS_DATA_DIR</key> <string>/Users/you/zhifox/data</string>
    <key>HARNESS_ENV_FILE</key> <string>/Users/you/zhifox/.env</string>
    <key>LANG</key>             <string>en_US.UTF-8</string>
    <key>PATH</key>             <string>(your terminal's PATH at install time)</string>
    <key>PYTHONUTF8</key>       <string>1</string>
  </dict>
  <key>KeepAlive</key>         <dict><key>SuccessfulExit</key><false/></dict>
  <key>Label</key>             <string>io.github.akxan.zhifox</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/you/zhifox/.venv/bin/python</string> <string>-P</string> <string>-m</string> <string>harness</string>
    <string>--workspace</string> <string>/Users/you/zhifox</string>
    <string>serve</string> <string>--web</string> <string>--service</string> <string>launchd</string>
  </array>
  <key>RunAtLoad</key>         <true/>
  <key>StandardErrorPath</key> <string>/Users/you/zhifox/data/logs/service.log</string>
  <key>StandardOutPath</key>   <string>/Users/you/zhifox/data/logs/service.log</string>
  <key>ThrottleInterval</key>  <integer>10</integer>
  <key>WorkingDirectory</key>  <string>/Users/you/zhifox</string>
</dict>
```

The unit for a project in `/home/you/zhifox`:

```ini
[Unit]
Description=ZhiFox (智狐)
Documentation=https://github.com/Akxan/zhifox-agent-harness#run-it-in-the-background

[Service]
Type=simple
WorkingDirectory=/home/you/zhifox
ExecStart="/home/you/zhifox/.venv/bin/python" "-P" "-m" "harness" "--workspace" "/home/you/zhifox" "serve" "--web" "--service" "systemd"
Environment="HARNESS_DATA_DIR=/home/you/zhifox/data"
Environment="HARNESS_ENV_FILE=/home/you/zhifox/.env"
Environment="LANG=en_US.UTF-8"
Environment="PATH=…"
Environment="PYTHONUTF8=1"
Restart=on-failure
RestartSec=10
RestartPreventExitStatus=2
KillMode=mixed
TimeoutStopSec=30

[Install]
WantedBy=default.target
```

- **What runs.** The project's own virtual environment: `.venv/bin/python -P -m harness … serve --web`. `uv` isn't
  involved at startup, so nothing is synced or downloaded when you log in. `-P` keeps the working folder off Python's
  import path, so a `json.py` lying in your workspace can't take the place of the standard library's. Each argument is
  its own list item, and nothing goes through a shell, so spaces in paths are fine. `--service` tells ZhiFox it is the
  background service: the data folder's lock says so, the web page shows it, and on macOS ZhiFox writes its own log
  file. `KillMode=mixed` sends SIGTERM to ZhiFox alone first, so it stops the commands it started itself and saves.
- **No secrets in these files.** They hold paths and switches only, with mode 644 as launchd and systemd expect. The
  API keys stay in `.env` (or the file `HARNESS_ENV_FILE` points to), which ZhiFox reads when it starts, and the bot
  tokens stay in `data/channels.json`.
- **Your terminal's environment isn't inherited.** launchd and systemd start ZhiFox without running your `.zshrc` or
  `.bashrc`. The service gets the data folder, the `.env` location, and `PATH` and `LANG` as they were when you
  installed. Anything else (`HTTPS_PROXY`, `HARNESS_MODEL`, an API key you only `export`ed) belongs in `.env`. `install`
  lists the ones set in your terminal but missing from `.env`, by name only, never their values.
- **The web page and its token.** The page still listens on 127.0.0.1 only. The background service keeps its token in
  `data/.web-token` (readable by your account only, never exported), so it survives restarts: after an update and
  `harness service restart`, an open tab keeps working once you reload it. The token guards against other accounts on
  this computer and other websites in your browser; a program running as you can read `data/` anyway, so keeping the token
  next to it gives that program nothing new. Uninstalling the service deletes the token, so the next install gets a new
  one (or delete the file and restart). `harness --web` in a terminal still makes a new token every start. With no terminal
  to print it in, the address is written to `data/.web-address` (your account only, deleted on exit), never to the log.
  `uv run harness service open` reads it and opens your browser, printing the address without the token; `open --print`
  prints the whole address, for a browser on another machine through an SSH tunnel.
- **Logs.** On macOS: `data/logs/service.log`, with the date on every line. ZhiFox starts a new file at 1 MB and keeps
  three old ones (`service.log.1` to `.3`), so the log stays under about 4 MB; launchd itself never trims it. On Windows:
  the same file and rotation, `data\logs\service.log`. On Linux: the journal, `journalctl --user -u zhifox -f`. Either
  way, `status` shows the last few warnings and errors. API keys
  and bot tokens are replaced with `<hidden>`, and message content is never logged.
- **macOS and protected folders.** If the project or the working folder is inside Documents, Desktop, Downloads or
  iCloud Drive, macOS may ask the first time whether python can open that folder (click OK), or simply block it
  (`Operation not permitted` in the log). `install` and `status` warn about it. The simplest fix is keeping the project
  somewhere else, such as `~/zhifox`. The alternative is adding the python binary that `status` names to **System
  Settings → Privacy & Security → Full Disk Access**, but that lets every program run with that python read all your
  files.
- **Linux without a login session.** By default systemd stops your user services when you log out. To keep ZhiFox
  running when nobody is logged in, and start it at boot: `sudo loginctl enable-linger $USER`. `status` says whether
  it's on. On a server with a dedicated account, the system unit in [Deploying to a server](#deploying-to-a-server) is
  the other way to do it.

## Deploying to a server

ZhiFox can live on a Linux server and take orders from your bot ("how much disk is left?", "any errors in the
nginx log since this morning?", "restart the worker"). The server needs Python 3.11+, [uv](https://docs.astral.sh/uv/)
and outbound HTTPS to the model provider and to your chat platform (`api.telegram.org`, `discord.com` and
`gateway.discord.gg`, `slack.com` and `wss-*.slack.com`, `open.feishu.cn`, `api.dingtalk.com`, `api.bot.qq.com`);
no inbound port.

```bash
git clone https://github.com/Akxan/zhifox-agent-harness.git ~/zhifox && cd ~/zhifox
uv sync
echo 'ANTHROPIC_API_KEY=sk-ant-…' >> .env && chmod 600 .env    # or another provider's key
uv run harness channels setup telegram     # paste the bot token (not echoed), or set TELEGRAM_BOT_TOKEN first
uv run harness channels pair telegram      # prints a 6-digit code; send it to your bot, wait for "Paired"
uv run harness channels approval telegram readonly    # ask | readonly | edits | all (ask is the default)
uv run harness commands add "npm test"     # optional: your own commands that don't ask in readonly mode
sudo apt install bubblewrap                # recommended: the sandbox for commands (dnf / pacman on other distributions)
uv run harness sandbox level workspace     # commands stay in the working folder, with no network
uv run harness sandbox check               # see what the sandbox lets through on this server
uv run harness channels status
```

The same commands take `discord`, `slack`, `feishu`, `dingtalk` or `qq`. `setup` prints that platform's steps and asks
for each field (secrets aren't echoed); it also reads them from the environment (`DISCORD_BOT_TOKEN`,
`SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`, `FEISHU_APP_ID` and `FEISHU_APP_SECRET`, `DINGTALK_CLIENT_ID` and
`DINGTALK_CLIENT_SECRET`, `QQ_APP_ID` and `QQ_APP_SECRET`) or from standard input, one line per field. Feishu and
DingTalk need their SDK: `uv sync --extra feishu --extra dingtalk` (and pass the same `--extra` flags to later
`uv sync` runs, which otherwise remove them).

Then run it as a service. If you log in to the server as yourself, `uv run harness service install` sets up a systemd
*user* unit for you (see [Run it in the background](#run-it-in-the-background); add `sudo loginctl enable-linger $USER`
so it keeps running after you log out). For a dedicated, unprivileged account, write a system unit,
`/etc/systemd/system/zhifox.service`:

```ini
[Unit]
Description=ZhiFox (chat channels and scheduled tasks)
After=network-online.target
Wants=network-online.target

[Service]
User=zhifox
# The working folder: where the assistant reads, writes and runs commands. Pick it on purpose
WorkingDirectory=/srv/app
ExecStart=/home/zhifox/.local/bin/uv run --project /home/zhifox/zhifox harness serve
# The API keys. ZhiFox also reads the project's .env itself; the bot token is in channels.json, not here
EnvironmentFile=/home/zhifox/zhifox/.env
Environment=HARNESS_DATA_DIR=/home/zhifox/zhifox-data
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now zhifox
journalctl -u zhifox -f        # logs; `-p warning` shows only warnings and errors
```

- **Working folder.** It's where the assistant's file tool and shell commands start, so it decides what "this folder"
  means in a chat. Set it with `WorkingDirectory=`, by `cd`-ing first, or with `harness --workspace /srv/app serve`.
- **Data.** `HARNESS_DATA_DIR` (default: the project's `data/`) holds conversations, memory, tasks and `channels.json`.
  Only one ZhiFox can use a data folder at a time. The `harness channels …` commands work while the service runs: they
  edit `channels.json` under a file lock, and the service notices within two seconds. When pairing while the service
  runs, the service receives the code; otherwise the command connects to Telegram itself until you're paired.
- **Logs.** Plain lines on stderr: no colour codes when it isn't a terminal, and a `<6>` / `<4>` / `<3>` priority prefix
  under journald so `journalctl -p` works. API keys and bot tokens are replaced with `<hidden>`, and message content
  is never logged.
- **Stopping.** `systemctl stop` sends SIGTERM: ZhiFox stops accepting messages, stops the turns and task runs in
  progress (each gets its result), saves the conversations (and how far it has read Telegram's messages) and exits with 0. It exits
  with 1 if the data folder is already in use or the port is taken, and with 2 if `channels.json` can't be read
  (fix or delete the file; `Restart=on-failure` would only retry).
- **The web UI on a server.** `harness serve --web` also starts the web UI, still on 127.0.0.1 only: it can run
  commands on the machine, so it is never exposed. Reach it through an SSH tunnel,
  `ssh -L 8765:127.0.0.1:8765 your-server`, then open the address with its token. Without a terminal the address
  isn't written to the log (anyone who can read the journal could use it); it's in `$HARNESS_DATA_DIR/.web-address`,
  readable by your account only. **Show in Finder** buttons tell you the file's full path instead, since a server has
  no file manager.
- **Blocked network?** Set `HTTPS_PROXY`: the HTTPS calls and the hand-written WebSocket client (Discord, Slack, QQ)
  both use it (HTTP proxies with `CONNECT`). For Telegram you can also point `TELEGRAM_API_BASE` at a self-hosted Bot
  API server. The Feishu SDK's long connection doesn't go through a proxy.
- **Updating.** `cd ~/zhifox && git pull && uv sync && sudo systemctl restart zhifox` (add `--extra feishu` /
  `--extra dingtalk` to `uv sync` if you use them). Data and `.env` aren't touched.

The security model for remote ops: only the paired account is served (strangers get no reply, groups are ignored, a
pairing code expires after 10 minutes or 5 wrong guesses); the default approval mode is **Ask every time**, and each
approval shows the full command and the working folder before you tap Allow; **Allow read-only commands** loosens only
commands that just look; **Allow everything** comes with a
warning and is a deliberate choice per channel; with the [sandbox](sandbox-and-hooks.md#sandbox-for-commands-what-a-command-can-reach) set
to Working folder only, even an allowed command can touch only the working folder, can't read ZhiFox's data, `.env` or
`~/.ssh`, and can't reach the network, and running one outside it asks you in the chat every time (never on a channel
set to Allow everything); one turn at a time per chat, and `/stop` ends it, command included;
the bot tokens and app secrets live only in `channels.json` (mode 600), are never sent to the browser or the model,
are masked in exports and hidden in logs and error messages, and the assistant's file tool can't open that file in any
mode; and shell commands don't see `TELEGRAM_BOT_TOKEN`, the other platforms' secret variables or the API keys in their
environment. Your chat account becomes a key to the server, so protect it (two-step verification) and use a dedicated,
unprivileged user for the service.
