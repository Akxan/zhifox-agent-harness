**English** | [简体中文](../zh-CN/data-and-privacy.md)

[ZhiFox](../../README.md) › [Documentation](../../README.md#documentation) › Data and privacy

# Data and privacy

Every file ZhiFox keeps and who can read it, what the web page may make your computer do, and how to export, back up and
restore it all.

The paths below are for a clone: `data/` and `.env` in the project. Installed with one command, `data/` is the folder
of your account from [Install with one command](installation.md#more-on-the-one-command-install) (`~/Library/Application Support/ZhiFox/`,
`~/.local/share/zhifox/`, `%LOCALAPPDATA%\ZhiFox\`), and `.env` is inside it; everything else is the same.

**Only your account can read it.** On macOS and Linux, `data/` and its folders are created with permissions 700 and the
files 600. Windows has no such permission bits (`chmod` there only toggles "read-only"), and a folder you create outside
your user folder (`C:\code\…`) normally inherits read access for every account on the computer. So on Windows ZhiFox
runs `icacls` once per folder ([winacl.py](../../harness/winacl.py)): it removes the inherited entries from `data\` and its
folders and grants full control to your account (by SID), SYSTEM and Administrators only, inherited by every file
created inside; `.env` gets the same three entries when a key is saved. SYSTEM and Administrators stay because they can
read everything anyway (as root can on the other systems) and removing them only breaks backup and antivirus tools. If
`icacls` fails (a FAT32 USB stick, a network drive), ZhiFox keeps running and writes a warning in the log.

**Saving a file another program has open.** ZhiFox saves every file by writing a temporary file and swapping it in in one
step (so a power cut never leaves half a file). On Windows that swap is refused while another program has the old file
open (an antivirus or the search indexer taking a look). That usually passes in a moment, so ZhiFox waits a little and
tries again, up to 8 times and a bit over a second in all (`WINDOWS_REPLACE_TRIES` in `config.py`), and only then reports
the error. macOS and Linux don't mind open files when swapping, so there's no retry there.

| Path | Contents |
|---|---|
| `.env` | Your API keys. When you save a key from the web UI, the file is made readable by your account only (on Windows by an ACL, see below) |
| `data/conversations/` | Every conversation (web UI, terminal UI, scheduled tasks, chat channels), one JSON file each |
| `data/media/` | The original of every attachment you sent, named by the SHA-256 of its content, so a file sent ten times is stored once. Originals that no conversation uses any more are removed when you delete a conversation, and at startup |
| `data/memories/` | The assistant's long-term memory and notes. You can read, edit and delete them in the web UI's info panel, or edit them by hand |
| `data/skills/` | Installed skills, one folder each. You can manage them under **Extensions → Skill** or by hand |
| `data/skills.json` | Which skills are disabled and where each one came from |
| `data/agents/` | Your custom agents, one Markdown file each (the Claude Code subagent format). You can manage them under **Extensions → Agent** or by hand |
| `data/todos.json` | To-dos |
| `data/tasks.json` | Scheduled tasks with the last 20 runs of each, and reminders you haven't dismissed yet. If it can't be read, it is kept as `tasks.broken.json` and ZhiFox starts with an empty list |
| `data/settings.json` | The model you chose, the synced model lists, prices you entered, the interface language, your monthly budget, the browser and computer use settings (`"computer": {"enabled", "unattended", "blocked"}`), the command sandbox (`"sandbox": {"level", "paths"}`), backup models (`"fallbacks": {provider: [models]}`), `"redact_secrets": false` if you turned off masking secrets in tool results, voice to text (`"voice": {"provider", "model", "url", "local_model", "endpoint"}`, written only once you change it), and `"update_check": true` if you turned on the [update check](installation.md#updates). No keys |
| `data/ledger.jsonl` | The spending record: one line per model call (date, provider, model, chat, scheduled task or subagent, tokens, estimated cost), and one per transcribed voice message (source `voice`, no tokens, with its length in `seconds`). Entries older than 90 days are merged into one line per day, model and source |
| `data/mcp.json` | The MCP servers you added, in Claude Desktop's format, plus ZhiFox's own fields (`enabled`, `approval`, `timeout`). May contain the servers' secrets, so it is readable by your account only, and the assistant's file tool can't open it |
| `data/hooks.json` | Your [hooks](sandbox-and-hooks.md#hooks-your-own-scripts-at-fixed-moments): name → event, matcher, program and arguments, time limit, `onFailure`, `runsIn`, `enabled`. Arguments may hold tokens, so it is readable by your account only, the assistant's file tool can't open it, and exports mask it |
| `data/endpoints.json` | Custom model endpoints: name, URL, the optional key, and each model's abilities, context limit and price. Readable by your account only; the assistant's file tool can't open it; the key is masked in exports and the web page only ever sees its last 4 characters |
| `data/channels.json` | Chat channels, per platform: the credentials (bot token, or app ID and secret; the secrets are secrets), the bot's name, the paired owner's account id and name, the approval mode, the sandbox level, an active pairing code, which conversation each chat continues, and how far Telegram's messages have been read. Readable by your account only; the assistant's file tool can't open it; masked in exports |
| `data/browser/profile/` | The profile of ZhiFox's own Chrome: cookies and logins of any site you signed in to there, its cache and history. Readable by your account only. **Settings → Browser → Clear** deletes it |
| `data/browser/shots/` | Full-size screenshots of browser steps, named by the SHA-256 of their content. Deleted after 30 days; the small previews stay in the conversation |
| `data/desktop/shots/` | Full-size screenshots of computer use steps (your whole screen), named by the SHA-256 of their content. Deleted after 7 days; the small previews stay in the conversation |
| `data/backups/` | The previous version of any file the assistant overwrote completely |
| `data/backups/turns/` | One folder per turn whose files the file tool changed: a manifest of the files and copies of how each was before (and after) the turn, plus the copies undo keeps for redo. Readable by your account only; deleted after 30 days |
| `data/input_history` | What you typed in the terminal UI (press the up arrow to bring it back) |
| `data/updates.json` | What the last [update check](installation.md#updates) found: when it asked, the newest release (version, tag, date, whether it has a wheel), and why the last attempt failed. Public information only. A cache: never exported, never written by a restore, safe to delete |
| `data/restore-backups/` | The automatic backup made before each restore from a backup (`zhifox-before-restore-<date>-<time>.zip`, secrets masked, the newest 5 kept). Never exported (see [Backup and restore](#backup-and-restore)) |
| `data/logs/` | The background service's log on macOS and Windows, `service.log`, plus up to three older ones (see [Run it in the background](background-and-servers.md#run-it-in-the-background)) |
| `data/.web-address`, `data/.web-token` | The background service's web token (kept across restarts, deleted when the service is uninstalled) and the page's address with it, written by `harness serve --web` when there's no terminal to print it in (the background service). Readable by your account only, deleted on exit, never exported |
| `data/.lock` | Held by the ZhiFox that is using this data folder, with its kind (web page, `serve`, background service) and process id, so a second one can say who's in the way. Never exported |

These are all ordinary files you can open. Both `data/` and `.env` are listed in `.gitignore`, so they never get
committed to git. The folders and conversation files in `data/` are readable and writable by your account only, so other
accounts on the same computer can't see them. After you save a key in the web UI, only its last 4 characters are shown;
the full key is never sent back to the browser.

Conversation content is sent to the API of the provider you chose, so the model can process it, and saved in
`data/conversations/` on your own computer. It is not uploaded anywhere else. Deleting a conversation in the sidebar
deletes its file too. The only request ZhiFox sends on its own, without you asking for something, is the
[update check](installation.md#updates), and it is off by default: once a day it asks GitHub for the latest release, which shows
GitHub your IP address and nothing else. With the browser on, the pages the assistant opens are fetched from those sites by ZhiFox's own
Chrome, and what it reads there goes to the model provider like any other tool result. With computer use on, every
screenshot taken (one you approved, one allowed for the conversation, or one under Allow everything) is a picture of
your whole screen, and it goes to the model provider too.

To look at a conversation's file directly, click **Show in Finder** on its row in the sidebar. The **Show in Finder**
buttons (on conversations, memories, tasks, to-dos, the prompt, MCP servers, skills and media items, plus the memory,
skill and browser profile folder buttons) are, with one exception described below, the only ones on the page that make your computer launch another program (the file manager),
so the server is extra careful with them: the page can only tell the server *which conversation*, *which memory* (a name
from the list, checked as described below), *which skill* (a single folder name that must exist inside `data/skills/`),
*which attachment* (an id of the form conversation/message/position, checked against the index; the file name comes from
the content's SHA-256), which of the assistant's own data files (`tasks`, `todos` or `prompt`), or, for the memory and
skill folders, `data/mcp.json`, `data/hooks.json` and the browser's profile folder, nothing at all; the server works out the path itself,
and the page has no way to make it open any other path. Downloading an attachment works the same way: the page sends
only the id, and the file comes back as a download (`Content-Disposition: attachment`,
`X-Content-Type-Options: nosniff`), so a text file that contains HTML is still plain text. A full-size browser screenshot is fetched the same
way: the page sends only its id (64 hexadecimal characters, which can't spell a path), and the answer is always
`image/jpeg` with the same headers plus `Content-Security-Policy: default-src 'none'; sandbox`; a full-size computer use
screenshot comes from `/api/computer/shot` under exactly the same rules. The app icons (`/icons/<name>`) are the only
other file bytes the server sends: six PNGs that ship with ZhiFox, picked by exact name from a fixed list (never a
path), always `image/png` with `nosniff` and `Content-Security-Policy: default-src 'none'; sandbox`; none of your data
is among them. Linux has no standard way to select a file in
the file manager, so there it opens the folder containing the file. One more button launches a program: on macOS,
**Open System Settings** in the computer use section runs `open` with one of two fixed `x-apple.systempreferences:`
addresses (Screen Recording or Accessibility). The page can only say `screen` or `input`; the address is the server's,
and it only opens the pane, never changes a setting.

Saving a local MCP server means ZhiFox will run that command on your computer, now and at every launch. The safeguards:
only the page holding this run's token can save one; the command is started from an argument list, never through a
shell, so nothing in an argument is treated as part of a command; the model has no tool that adds or changes servers,
and its file tool can't touch `data/mcp.json` in any mode; the programs don't receive your model API keys, and a
configuration that tries to pass one on (`${ANTHROPIC_API_KEY}`) is refused; the values of environment variables and
headers never reach the browser, only their last 4 characters; and other tools' configuration files are read only
when you open the import page, and only the servers you tick are copied, without their "don't ask" settings. Arguments of an MCP tool call go to that server, which
may be on the internet, so check them on the approval card.

Reading, editing and deleting a memory from the info panel are guarded the same way. The page sends back the name it was shown
(such as `projects/zhifox.md`), and the server walks from the memory folder down to that file one step at a time. It
refuses anything that would end up outside the folder: `..`, absolute paths, and symlinks that point elsewhere. It only
deletes plain files; a subfolder left empty by the deletion is removed too. A new name given while editing is a single
name (no `/`, not starting with a dot), so a memory can be renamed but never moved out of its folder, and the new text
is written to a temporary file first and swapped in, so a failed save never leaves half a file.

## Backup and restore

**Settings → Export all data** packs the data folder into `zhifox-backup-<date>-<time>.zip` and downloads it:
conversations, memories, to-dos, settings, the spending record, and anything else later features keep there. API keys
are never in it: they live in `.env`, which is never exported, whether it's next to the data folder (a clone) or
inside it (installed with one command), and any other `.env`-like file in the data folder is skipped too. `data/channels.json` is exported with every bot token and app secret, and any pairing code, replaced by `<removed-from-backup>` (app IDs are kept). If `data/mcp.json` exists, it is exported with every secret replaced the same way (environment
variables, headers, fields named like a key, token or password, the value after flags such as `--token`, passwords in
URLs, and strings shaped like an API key); the names are kept so you know what to fill in again. If that file isn't
valid JSON, it is left out entirely rather than risk exporting a secret. `data/hooks.json` gets the same treatment (a hook's
arguments may hold a token, such as `--token xxx`), and so does `data/endpoints.json`. Symbolic links are skipped too, since they
could point anywhere on your computer. A `README-backup.txt` inside the zip lists the date, how to restore, and what was
masked or skipped, and `backup-manifest.json` says the same for the program (format version, export time with time zone,
which files were masked). The zip is built in a temporary file outside the data folder and deleted once it has been sent.

**Restoring from the web UI: Settings → Restore from backup…**

1. Choose the zip (or drag it into the window). It is uploaded to ZhiFox, which checks it before anything else; a bad
   archive is refused with the reason, and nothing is written.
2. A preview shows when it was exported, what's in it (how many conversations, memories, to-dos, scheduled tasks,
   skills, agents, MCP servers…), what each way of restoring would do to each of them, and which secrets will need
   filling in again.
3. Pick a way and confirm (overwriting asks once more, in place):
   - **Overwrite** makes the data folder what it was in the backup: each kind of data is replaced by the backup's, and
     kinds the backup doesn't have are cleared. Files that hold secrets are the exception, see below.
   - **Merge** only adds: conversations by ID, memories and agents by file name, skills by folder, media by content;
     anything already here stays as it is. Whole files (settings, to-dos, scheduled tasks, the spending record, MCP,
     channels, hooks…) are kept when they exist and taken from the backup only when they don't: stitching two
     configurations together entry by entry would make one nobody ever used (a hook pointing at a script on the other
     computer, a channel paired to someone else).
4. Before writing anything, ZhiFox exports the current data folder to
   `data/restore-backups/zhifox-before-restore-<date>-<time>.zip` (secrets masked, like any export; the newest 5 are kept,
   and exports never include this folder). The result screen has a "Show in Finder" (File Explorer, file manager) button
   for it, and lists the secrets that are still placeholders.

**Your secrets are never overwritten by placeholders.** In `mcp.json`, `channels.json`, `endpoints.json` and
`hooks.json`, each `<removed-from-backup>` in the backup is matched to the same place in the current file (the same MCP
server's same environment variable, the endpoint with the same id, the same argument after `--token`, a URL that is the
same apart from its password): if the current file has a real value there, it is kept; if not, the placeholder is
written, and the preview and the result say where to fill it in (Extensions → MCP, Extensions → Channels, Settings →
Custom model endpoints, Extensions → Hooks). Secrets you have now that the backup has no place for (a server that only
exists here) are listed too, since they're gone after an overwrite. A current file that can't be read is left alone, and
so is a secrets file the backup doesn't contain, even when overwriting.

**The archive is untrusted input.** Only what an export writes is accepted, from one list shared with the export
(`export.PARTS`); anything else is refused and nothing is written: paths with `../`, absolute paths, drive letters and
Windows name tricks (`a:b`, `CON`, `PROGRA~1`, trailing dots, checked by `pathnames.archive_trick`), symbolic links and
device files, encrypted entries, two names that differ only in case, `.env`-like files, `browser/profile/`, `.web-token`,
`.lock`, and any name that isn't on the list. Sizes are capped (`RESTORE_MAX_*` in `config.py`: 8 GB unpacked in total,
2 GB per file, 200,000 entries; the web upload itself is capped at 4 GB and checked before uploading), and the sizes a
zip declares are checked against what is actually read, so a zip bomb stops at the cap. `README-backup.txt` and
`backup-manifest.json` are read, never written.

**When it refuses, and what happens afterwards.** Restoring waits for nothing: it is refused, with the reason, while your
conversation turn is running, a scheduled task is running, or a chat channel is in the middle of a turn. Otherwise ZhiFox
stops the scheduler, channels, MCP servers and its browser, writes the files (each kind of data is laid out in a staging
folder inside the data folder and then swapped in; any failure puts everything back), and starts again in place from the
restored data, the same way it starts up: same port, same link, same event stream, so the page just redraws (no reload,
no restart, and the interface language follows the restored settings).

**In a terminal** (a server without the web page): quit ZhiFox first, then

```bash
uv run harness restore zhifox-backup-….zip            # overwrite
uv run harness restore zhifox-backup-….zip --merge    # merge
uv run harness restore zhifox-backup-….zip --yes      # don't ask (required when there's no terminal)
```

```powershell
uv run harness restore .\zhifox-backup-….zip
uv run harness restore .\zhifox-backup-….zip --merge
```

It prints the same preview, asks you to type `yes`, makes the same automatic backup, and holds the data folder's lock
while it works, so ZhiFox can't start halfway through. If ZhiFox is running, it says which one and points you to its web
page. The terminal reads the file directly, so the 4 GB upload cap doesn't apply (the unpacked caps do).

**By hand, as a last resort:** quit ZhiFox, move the current data folder aside (the project's `data/`, or the folder of
your account when installed with one command; `harness doctor` shows which), unzip the archive into a new, empty `data/`
(`unzip zhifox-backup-….zip -d data`; in PowerShell `Expand-Archive .\zhifox-backup-….zip -DestinationPath data`), delete
`README-backup.txt` and `backup-manifest.json` from it, and start ZhiFox again. The files that held secrets now hold
placeholders: fill those secrets in again. Never unzip over a data folder you're using: the placeholders would overwrite
the keys you have.
