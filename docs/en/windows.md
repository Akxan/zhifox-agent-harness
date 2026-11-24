**English** | [简体中文](../zh-CN/windows.md)

[ZhiFox](../../README.md) › [Documentation](../../README.md#documentation) › Windows

# Windows

ZhiFox runs on Windows. Every push is tested on GitHub's `windows-latest` runner (Windows Server) as well as Ubuntu and
macOS; it hasn't been tried by hand on a Windows 10 or 11 desktop yet, so tell us if something there behaves
differently. To just use it, install it with one command (see [Install with one command](../../README.md#install-with-one-command);
data and keys then go to `%LOCALAPPDATA%\ZhiFox\`):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # install uv, then open a new PowerShell window
winget install --id Git.Git -e                                                        # Git, if you don't have it yet (uv uses it to download)
uv tool install git+https://github.com/Akxan/zhifox-agent-harness
harness --web
```

(Without Git, install the package attached to a [release](https://github.com/Akxan/zhifox-agent-harness/releases) (see
[Installing a released version](installation.md#more-on-the-one-command-install)), or
`uv tool install https://github.com/Akxan/zhifox-agent-harness/archive/refs/heads/main.zip` for the latest code.)
To work on the code, clone it instead:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # install uv, then open a new PowerShell window
git clone https://github.com/Akxan/zhifox-agent-harness.git
cd zhifox-agent-harness
uv sync
uv run harness --web
```

(No Git? Download the ZIP from GitHub and unpack it instead.) Everything else works as on the other two systems; these
are the differences:

| | On Windows |
|---|---|
| Keyboard shortcuts | Ctrl wherever a Mac says ⌘, Alt for ⌥ (the table below). The page decides by the computer the browser runs on |
| "Show in Finder" | **Show in File Explorer** (`explorer /select,`); on Linux it's "Show in file manager". The page decides by the computer ZhiFox runs on, since that's where the window opens |
| Commands (`run_command`) | Run in **PowerShell**: PowerShell 7 (`pwsh`) when it's installed, Windows PowerShell 5.1 otherwise. The tool's description and the system prompt say which, so the model writes `Get-ChildItem` rather than `ls -la`. Your `$PROFILE` is not loaded, your execution policy is left as it is, output is read as UTF-8, and Stop or a timeout ends the command with everything it started (a Windows job object). The folder carries over from one command to the next (`Set-Location` sticks, see [Commands remember their folder](tools-and-permissions.md#commands-remember-their-folder)) |
| **Allow read-only commands** | Doesn't skip any command yet: the read-only check understands bash, not PowerShell, so every command still asks, and your own "commands that don't ask" don't apply (the approval panel, that window and the capability list say so). The mode still reads files outside the working folder without asking |
| Files only you can read | Windows has no 600/700 permissions. ZhiFox removes the inherited access from `data\` and `.env` and leaves only your account, SYSTEM and Administrators (see [Data and privacy](data-and-privacy.md)) |
| Browser use | Chrome, or Microsoft Edge (every Windows 10/11 has it) when Chrome isn't installed. Still a pipe, never a port (see [Browser use](browser-and-computer-use.md#browser-use-a-chrome-of-its-own)) |
| Background service | **Task Scheduler** instead of launchd or systemd: the same `uv run harness service …` commands (see [Run it in the background](background-and-servers.md#run-it-in-the-background)) |
| Computer use | Not available yet. Settings and the capability list say so ("isn't supported on Windows yet") |

Keyboard shortcuts in the web UI:

| What | macOS | Windows and Linux |
|---|---|---|
| Send / new line | Enter / Shift+Enter | Enter / Shift+Enter |
| Search conversations | ⌘F or ⌘K | Ctrl+F or Ctrl+K |
| Settings | ⌘, | Ctrl+, |
| Switch model | ⌘. | Ctrl+. |
| Save in an editor (memory, task, skill, agent, MCP server, prompt) | ⌘Enter | Ctrl+Enter |
| Zoom an image in the viewer | ⌘+ / ⌘− / ⌘0, ⌘ + scroll | Ctrl++ / Ctrl+− / Ctrl+0, Ctrl + scroll |
| Close a panel or window (asks first if you have unsaved changes) | Esc | Esc |

Not supported on Windows yet:

- Computer use (no Windows backend for screenshots, mouse and keyboard yet).
- The command sandbox (see [Sandbox for commands](sandbox-and-hooks.md#sandbox-for-commands-what-a-command-can-reach)): Windows has no
  reliable built-in way yet, so with the sandbox turned on every command is refused and the page says why. To use it,
  run ZhiFox inside WSL with bubblewrap installed.
- Automatic approval of read-only commands, as above.
- A clean stop when you sign out or shut down: Windows ends the processes, so a turn in progress may not be saved. Ctrl-C
  and Ctrl+Break in the terminal, and `uv run harness service stop`, all stop it cleanly. Closing the terminal window
  ends it without that.
- The background service stops when you sign out (it runs as you, with no stored password).

In the terminal, Ctrl-C (and Ctrl+Break) stops a reply at once, even while the model is silent. Windows can't break
into a program waiting on the network the way macOS and Linux do, so ZhiFox registers a console handler that cuts the
connection to the model the moment you press it (`console.on_ctrl_c`, checked only in CI so far).
