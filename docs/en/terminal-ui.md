**English** | [简体中文](../zh-CN/terminal-ui.md)

[ZhiFox](../../README.md) › [Documentation](../../README.md#documentation) › Terminal UI

# Terminal UI

The same assistant in a terminal: the same loop and the same saved conversations as the [web UI](web-ui.md), with slash
commands instead of buttons.

```bash
uv run harness
```

Once it starts, type to chat:

```
You> Remind me to hand in the report next Friday
🔧 todo_add {"title": "Hand in the report", "due": "2026-11-27"}
   ↳ {"id": 1, "title": "Hand in the report", "due": "2026-11-27", ...}
Got it: due Friday, November 27.

You> Which Python files are in this folder?
🔧 run_command $ find . -name "*.py"

⚠️  The assistant wants to run a command:

    $ find . -name "*.py"
Allow? y to allow, Enter to deny, or type a reason to deny >
```

| Command | What it does |
|---|---|
| `/help` | Show the list of commands |
| `/new` | Start a new conversation (memory and to-dos are kept; the one you leave is already saved) |
| `/history` | List recent conversations, numbered, with title, model and time (the ones from the web UI too); `/history some words` searches every conversation with the same search as the web UI |
| `/open` | Open a conversation and carry on with it: `/open 3` (a number from the last `/history`) or `/open` followed by a conversation ID. It prints the last few messages so you know where you are |
| `/todos` | Show open to-dos |
| `/tools` | Show what 智狐 can do: what's in use, whether it asks first, how to turn on what's off (the same list as the web UI's info panel) |
| `/trace` | Show the last turn's run trace: how many model calls, which tools, how long each step took and what it cost, with a small waterfall drawn in block characters (the same numbers as the web UI's run trace) |
| `/usage` | Show this run's token usage and estimated cost, plus what today and this month cost (and your monthly budget, if you set one) |
| `/model` | Show every provider's models: which one is in use (✓), what each can do, its price, and which providers have no key yet (the same catalog as the web UI's model switcher; switch with its model button, ⌘. / Ctrl+.) |
| `/mcp` | Show the connected MCP servers and their status (add and manage them in the web UI) |
| `/auto` | Show or change when the assistant asks first: `/auto ask`, `/auto readonly`, `/auto edits` or `/auto all` (see [Permissions](tools-and-permissions.md#permissions)) |
| `/retry` | Regenerate the last answer: your message goes out again exactly as it was |
| `/edit` | Edit your last message: it is filled into the input line for you to change; or type `/edit` followed by the new text. Like `/retry`, it asks first if that turn ran a command or changed a file |
| `/undo` | Undo the files the latest turn changed with the file tool: lists them (and anything that changed since) and asks before writing. Redo and undoing single files are in the web UI |
| `/doctor` | Run the self-check: Python, folder permissions, which providers have keys, the browser, the sandbox, the background service… what's OK, what's a problem and how to fix it (the same as `uv run harness doctor`, see [Troubleshooting](configuration.md#troubleshooting-harness-doctor)) |
| `/exit` | Quit (`/quit` and Ctrl-D also work) |

Press Ctrl-C while the model is replying to interrupt it.

**Conversations are saved.** Every turn is saved to `data/conversations/` the way the web UI saves it (an interrupted
turn too), so the web UI's sidebar lists terminal conversations, search finds them, and the page can open, continue,
edit, regenerate and fork them; `/history` and `/open` work the same on conversations from the web page. `/open`
follows the same rules as opening one in the page: it switches to the provider and model the conversation used, hands
its history back to the adapter, continues with its custom agent if it had one (and says so if that agent is gone), and
takes a fresh snapshot of the MCP tools. A conversation whose provider has no key yet isn't opened: you couldn't
continue it, and opening it would make that provider the one the next start uses.

Only one ZhiFox can write to a data folder at a time (see [Run it in the background](background-and-servers.md#run-it-in-the-background)), and
the terminal now takes that lock too. While the web UI or the background service is running, the terminal still chats
but says it won't save, `/history` still lists, and `/open` is off; while the terminal is open, `harness --web` and the
service say so and ask you to `/exit` it first. A failed save is reported and tried again before you leave the
conversation; `/new`, `/open` and `/exit` stop once to warn you before giving up what wasn't saved. A plain `kill`
(SIGTERM; Ctrl+Break on Windows) stops the current turn, saves and quits.

The terminal UI doesn't run scheduled tasks or chat channels. You can ask the assistant to add or delete tasks here too;
they run while the web UI or `harness serve` is running.
