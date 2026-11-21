<div align="center">

<img src="docs/assets/logo.svg" width="96" alt="ZhiFox logo: a small fox">

# ZhiFox · 智狐

**A hand-written AI agent harness: your own personal assistant, running on your computer.**

[![Tests](https://github.com/Akxan/zhifox-agent-harness/actions/workflows/tests.yml/badge.svg)](https://github.com/Akxan/zhifox-agent-harness/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-22c55e.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab.svg?logo=python&logoColor=white)](https://www.python.org/)
[![No agent framework](https://img.shields.io/badge/agent%20framework-none-f0532e.svg)](#how-it-works)
[![Models](https://img.shields.io/badge/models-Claude%20%7C%20GPT%20%7C%20Gemini%20%7C%20DeepSeek-6e56cf.svg)](#supported-models)
[![Interfaces](https://img.shields.io/badge/UI-terminal%20%2B%20web-0ea5e9.svg)](#web-ui)
[![Languages](https://img.shields.io/badge/i18n-English%20%7C%20%E4%B8%AD%E6%96%87%20%7C%20Espa%C3%B1ol-f59e0b.svg)](#three-interface-languages)
[![GitHub stars](https://img.shields.io/github/stars/Akxan/zhifox-agent-harness?style=social)](https://github.com/Akxan/zhifox-agent-harness/stargazers)
[![Last commit](https://img.shields.io/github/last-commit/Akxan/zhifox-agent-harness.svg)](https://github.com/Akxan/zhifox-agent-harness/commits/main)

**English** | [简体中文](README.zh-CN.md)

</div>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/screenshot-dark-en.webp">
    <img alt="The ZhiFox web UI: a conversation in which the assistant adds a to-do through a tool call, followed by an approval card asking whether to run a shell command" src="docs/assets/screenshot-light-en.webp" width="900">
  </picture>
</p>

ZhiFox is an AI agent harness built from scratch in Python, with no agent framework: a local-first personal assistant
that runs on your own computer, with a terminal UI and a web UI. It works with Claude, GPT, Gemini and DeepSeek, and uses
tool use (function calling) to manage to-dos, remember things across conversations, read and write files, run shell
commands, search the web, use websites in a separate Chrome of its own (browser use) and run scheduled or recurring
tasks, with a permission prompt before anything that changes your computer or sends something out. It also reads the
screenshots, images, PDFs and Word, Excel or PowerPoint files you attach. It is also a teaching project: every part of
this LLM agent, from the loop to the provider adapters, is hand-written and lives in its own file.

"ZhiFox" is the English name; in the app itself the name stays "智狐" in every interface language. The repository is
`zhifox-agent-harness`, but the Python package and the command are still called `harness`, because the project is about
what a harness is. Web search is available with Claude only. Every web UI conversation is saved automatically on your
computer, and you can reopen it from the sidebar or search it by title and content.

## Highlights

- 🦊 **No agent framework.** The loop that turns a model into an agent is one file of about 180 lines,
  [harness/agent.py](harness/agent.py). Tool dispatch, permissions, provider adapters, storage and search are plain
  Python on top of the official `anthropic` and `openai` SDKs.
- 🔌 **Four providers, two adapters.** Claude goes through the native Anthropic Messages API; GPT, Gemini and DeepSeek
  share one OpenAI Chat Completions adapter.
- 🛠️ **Tools that do real work:** to-dos, long-term memory shared by all conversations, reading and writing files,
  shell commands, and web search (Claude only).
- 🧭 **Browser use, in a browser of its own.** Turn it on in Settings and the assistant opens pages, reads them, clicks,
  types and takes screenshots in a separate Chrome that ZhiFox launches with its own profile: never your everyday
  browser or its logins. A hand-written DevTools Protocol client talks to it over a pipe, with no debugging port anyone
  else could attach to. Looking is free; typing, submitting a form and visiting a new site ask first, and password and
  card fields ask in every mode. Claude uses Anthropic's browser use toolset; GPT, Gemini and DeepSeek get hand-written
  browser tools.
- 🧩 **Skills in the open [Agent Skills](https://agentskills.io) format.** Write one in the page, upload a folder, a `.zip`
  or `.skill` file, paste a GitHub link, or copy your Claude Code skills over. The model sees only each skill's name and
  description until a task calls for it, and the scripts a skill brings still need your approval to run.
- 🤖 **Custom agents, used two ways.** One Markdown file, in the same format as a Claude Code subagent, describes an
  agent: what it's good at, which tools and model it may use, and its instructions. The main assistant can hand a task
  to it as a *subagent* (a fresh history of its own; only its conclusions come back), or you can start a conversation
  with it as the *persona*. An agent never gets more tools than whoever calls it, and its commands still ask you first.
- ⏰ **Scheduled and recurring tasks.** Free reminders, or instructions the assistant runs on its own at a set time:
  once, daily, weekly, monthly or every N minutes, in local time and safe across daylight saving. Runs happen alongside
  your conversation (up to three at once), ask for approval the way you choose for each task (Ask every time by
  default), catch up once after ZhiFox was closed, and record what each one cost.
- 📱 **Chat channels: talk to it from Telegram, Discord, Slack, Feishu, DingTalk or QQ.** Connect your own bot in a
  four-step wizard (or from the command line) and chat with ZhiFox from your phone, photos and files included. No
  public URL or open port: ZhiFox long-polls Telegram and keeps an outgoing WebSocket open to the others (a
  hand-written RFC 6455 client; Feishu and DingTalk use their official SDKs, installed only if you want them). Only the
  account you pair with a 6-digit code is served, and every command or file change it wants to make arrives in the chat
  with Allow / Deny buttons where the platform has them (a "reply allow or deny" message where it doesn't), or on the
  web page, whichever you answer first. WhatsApp and WeCom need a public URL and come later.
- 🖧 **Runs on a server.** `harness serve` runs the chat channels and scheduled tasks with no browser, logs in a
  journald-friendly format, stops cleanly on SIGTERM, and comes with a systemd unit; set it up and pair it with
  `harness channels setup` / `pair`, then do server chores from your chat app.
- 📎 **Attachments.** Paste a screenshot or drop in images, PDFs, Word, Excel, PowerPoint and text files. What a model
  can't take is converted first (SVG to PNG, Office files to text, GBK text to Unicode) or refused with a note on how to
  convert it, in all three interface languages.
- 🧠 **Memory you can manage.** A window at the top of the sidebar lists everything the assistant remembers; read,
  edit, rename or delete any memory, or show its file in Finder.
- ✍️ **Your own system prompt.** Edit the built-in prompt in Settings, with placeholders and a preview of exactly what
  the model receives; changes apply from the next conversation.
- 🧩 **MCP, written by hand.** Connect any [Model Context Protocol](https://modelcontextprotocol.io) server, local
  (stdio) or remote (Streamable HTTP), and its tools become the model's tools. The client speaks both the current
  stateless protocol (2026-07-28) and the older handshake-based one; every call asks you first unless you mark that
  tool "don't ask", and servers already set up in Claude Desktop or Claude Code can be imported in one click.
- 🛡️ **Permission prompts.** Every shell command and every file write waits for your approval, and when you deny one
  you can tell the model why. Looser modes last only until the next restart, and `.env` is always off-limits.
- 💾 **Local-first.** Conversations, memory, to-dos and settings are plain files on your disk. The web server listens
  on 127.0.0.1 only, and the page loads no external resources.
- 🔎 **Search across past conversations**, by title and content, tolerant of typos and skipped characters, without a
  search library.
- 🖥️ **One loop, two interfaces.** The terminal UI and the web UI drive the same loop. The web UI streams over SSE,
  renders replies as Markdown (code blocks, tables, lists) with a small hand-written renderer, has light and dark
  themes, and respects reduced-motion settings.
- 🌐 **Interface in English, 中文 and Español**, switchable mid-conversation without a page reload.
- 💸 **Cost you can see:** token usage and an estimated cost after every model call, built-in prices for Claude and
  DeepSeek (checked against the official pricing pages, DeepSeek's off-peak discount included), a spending record that
  survives restarts (today, this month, the last 30 days, by model and source), an optional monthly budget reminder,
  prompt caching, automatic compaction of long conversations with every provider, and per-message caps on web search.
- 🧪 **Tests without API keys.** The test suite drives the real SDKs against local fake servers, so running it is free.

## Quick start

You need macOS or Linux and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                 # install dependencies
uv run harness --web    # start the web UI; it opens in your browser
```

The first time, the middle of the page tells you there is no API key yet. Click **Open settings** in that notice (or
the button at the top right), enter a key for at least one provider, and start chatting. For the terminal UI, run
`uv run harness` once a key is set.

### Supported models

| Provider | Where to get an API key | Variable in `.env` | Tested |
|---|---|---|---|
| Claude (Anthropic) | [platform.claude.com](https://platform.claude.com) | `ANTHROPIC_API_KEY` | Yes, against the real API |
| GPT (OpenAI) | [platform.openai.com](https://platform.openai.com) | `OPENAI_API_KEY` | Not with a real key |
| Gemini (Google) | [aistudio.google.com](https://aistudio.google.com) | `GEMINI_API_KEY` | Not with a real key |
| DeepSeek | [platform.deepseek.com](https://platform.deepseek.com) | `DEEPSEEK_API_KEY` | Yes: key saving, model sync, and a chat with a text attachment |

GPT and Gemini were written against each provider's official documentation and tested with local fake servers, but
have not been used with a real key yet. If one of them fails the first time you use it, the likely cause is a detail of
that provider's API that its documentation doesn't mention.

Every provider bills API usage by volume, separately from its chat-app subscription, and usually needs prepaid credit.
You can enter keys in the web UI's Settings, or copy [.env.example](.env.example) to `.env` and write them there. Both
end up in the same place.

## Architecture

A model does one thing: it reads text and writes the next piece of text. It doesn't remember your last conversation,
and it can't take any action. A **harness** is the code wrapped around the model that makes up for this.
**Agent = model + harness.**

How the parts connect:

```mermaid
flowchart TB
    you(["You"])

    subgraph ui ["Interfaces"]
        term["Terminal UI<br/>harness/ui.py"]
        web["Web UI<br/>browser ⇄ local server over SSE<br/>harness/webui/"]
    end

    agent["Agent loop · harness/agent.py<br/>call the model → run the tools it asks for → repeat"]

    subgraph parts ["Harness parts"]
        prompt["System prompt<br/>prompt.py"]
        perms{{"Permissions<br/>permissions.py"}}
        tools["Tools · tools/<br/>to-dos · tasks · memory · files · shell · skills · browser · web search"]
        store[("Conversations + search index<br/>conversations.py · search.py")]
    end

    chrome(["ZhiFox's own Chrome<br/>CDP over a pipe · browser/"])

    subgraph providers ["Provider adapters · providers/"]
        claude["claude.py<br/>Anthropic Messages API"]
        compat["openai_compat.py<br/>OpenAI Chat Completions"]
    end

    apis(["Claude · GPT · Gemini · DeepSeek"])

    you --> term
    you --> web
    term --> agent
    web --> agent
    web --> store
    prompt --> agent
    agent --> claude
    agent --> compat
    claude --> apis
    compat --> apis
    agent -->|"tool call"| perms
    perms -.->|"risky step: asks you first"| you
    perms --> tools
    tools --> chrome
```

What happens during one turn, using a to-do request as the example:

```mermaid
sequenceDiagram
    autonumber
    actor You
    participant UI as Terminal or web UI
    participant Agent as Agent loop
    participant Model as Model API
    participant Tool as Tool
    You->>UI: Remind me to hand in the report next Friday
    UI->>Agent: user message
    loop until the model answers in plain text
        Agent->>Model: system prompt + tool specs + full history
        Model-->>Agent: tool_use todo_add (title, due)
        opt the step changes your computer, e.g. writing a file or running a command
            Agent->>You: approval card: Allow / Deny
            You-->>Agent: Allow
        end
        Agent->>Tool: run it
        Tool-->>Agent: result
        Note over Agent: append the tool call and its result to the history
    end
    Model-->>Agent: Got it: due Friday, November 27.
    Agent-->>UI: stream the reply
    UI-->>You: text appears as it arrives
```

### Which file does what

Each part of the harness has its own file or directory:

| Part | File | The problem it solves |
|---|---|---|
| Loop | [harness/agent.py](harness/agent.py) | A model replies once per call. The loop keeps calling it until the task is done |
| Tools | [harness/tools/](harness/tools/) | A model can't act. Tools act for it and report the result back |
| Provider adapters | [harness/providers/](harness/providers/) | Every provider's API has its own format. The adapters hide the differences, so the loop doesn't care which provider is behind it |
| Conversation history | Inside the adapters (the format differs per provider) | A model has no memory. Every request resends the whole conversation |
| History compaction | [harness/providers/compaction.py](harness/providers/compaction.py) | Resending the whole history every turn gets more expensive and finally exceeds the model's limit. When a GPT, Gemini or DeepSeek conversation gets too long, the model is asked to summarize the earlier part (Claude's API compacts on its own) |
| Conversation storage | [harness/conversations.py](harness/conversations.py) | History in memory is gone once the program closes. Saving it to a file lets you pick up where you left off |
| Conversation search | [harness/search.py](harness/search.py) | Once there are many conversations, you can't find anything. The text of every conversation is kept in memory and searched by title and content, allowing near matches |
| Attachments | [harness/attachments.py](harness/attachments.py), [harness/documents.py](harness/documents.py) | Models take only a few kinds of files. Every attachment is checked before anything is sent, and Word, Excel and PowerPoint files are turned into text |
| Media library | [harness/media.py](harness/media.py) | Keeps the original of every attachment (content-addressed), indexes which conversation and message each one came from, and recovers attachments from older conversations out of their history |
| Long-term memory | [harness/tools/memory.py](harness/tools/memory.py) | Conversations can't see each other. Memory is shared by all of them, and the assistant decides what to write there |
| MCP | [harness/mcp/](harness/mcp/) | Tools written by other people live in separate programs. A hand-written MCP client starts or connects to them, lists their tools, calls them for the model, and keeps each conversation's tool list fixed (see [MCP](#mcp-tools-written-by-someone-else)) |
| Browser | [harness/browser/](harness/browser/), [harness/tools/browser.py](harness/tools/browser.py), [harness/webui/browser_api.py](harness/webui/browser_api.py) | Web search finds pages, but can't use a site: search it, click through it, fill in a form. ZhiFox starts a separate Chrome of its own and drives it with a hand-written DevTools Protocol client over a pipe; the page is turned into numbered lines the model acts on (see [Browser use](#browser-use-a-chrome-of-its-own)) |
| Memory panel | [harness/memories.py](harness/memories.py) | You can't see what the assistant remembers. The web UI lists, shows, edits and deletes the memory files, and never lets a path the browser sends leave the memory folder |
| Skills | [harness/skills/](harness/skills/), [harness/tools/skill.py](harness/tools/skill.py) | A model doesn't know how *you* want a kind of task done. A skill is a folder of instructions (and maybe scripts) that the model reads only when a task matches it |
| Custom agents | [harness/agents/](harness/agents/), [harness/tools/agent.py](harness/tools/agent.py) | One assistant can't be good at everything, and a long research job floods the conversation with pages of material. An agent is a file with its own instructions, tools and model: the main assistant hands it a task and gets back only the conclusions (a subagent), or you talk to it directly (a persona) |
| Shared catalog | [harness/catalog.py](harness/catalog.py) | The lists of skills and agents change at any time, but the system prompt mustn't. Both lists travel in user messages, attached again only when they change |
| Scheduled tasks | [harness/tasks.py](harness/tasks.py), [harness/webui/scheduler.py](harness/webui/scheduler.py), [harness/tools/tasks.py](harness/tools/tasks.py) | A model only acts when you write to it. Tasks let it act at a set time: the schedules and their storage, the runner that works alongside your conversation and pauses for approval, and the tools that set one up from a sentence |
| System prompt | [harness/prompt.py](harness/prompt.py) | A model doesn't know who it is or what environment it is running in. The built-in prompt can be replaced by your own, and the line about web search is only given to providers that have it |
| Chat channels | [harness/channels/](harness/channels/) | You can only reach the assistant at the computer it runs on. A channel connects it to a chat app (Telegram, Discord, Slack, Feishu, DingTalk, QQ): one small client per platform, pairing so only you are served, one conversation per chat that runs beside the web chat, and approvals with buttons or by reply (see [Chat channels](#chat-channels-talking-to-zhifox-from-your-chat-app)) |
| WebSocket and HTTP | [harness/net/](harness/net/) | Discord, Slack and QQ push messages over a WebSocket the bot opens itself. A small RFC 6455 client on `socket` + `ssl` (masking, fragments, ping/pong, the close handshake, size limits, timeouts, HTTP proxies) and a JSON-over-HTTPS helper that keeps tokens out of errors and redirects |
| Headless mode | [harness/serve.py](harness/serve.py), [harness/log.py](harness/log.py), [harness/webui/lifecycle.py](harness/webui/lifecycle.py) | A server has no browser. `harness serve` starts the scheduler and the channels without one, logs for journald with secrets hidden, and shuts down cleanly on SIGTERM; the web UI and the headless mode start and stop the background parts in the same place |
| Conversation claims | [harness/claims.py](harness/claims.py) | One conversation can be open in the web page and in a chat app at once. A conversation is claimed for each turn, so two sides never write to it at the same time and overwrite each other's turn |
| Permissions | [harness/permissions.py](harness/permissions.py) | A model can make mistakes or be misled. Risky actions need your approval |
| Interfaces | [harness/ui.py](harness/ui.py) (terminal), [harness/webui/](harness/webui/) (web) | Show you what is happening and pass your input to the loop |
| Markdown in replies | [harness/webui/markdown.js](harness/webui/markdown.js) | Models answer in Markdown. A small hand-written renderer turns it into headings, lists, tables and code blocks while the reply streams, without ever treating model output as HTML |
| Settings and pricing | [harness/settings.py](harness/settings.py), [harness/pricing.py](harness/pricing.py) | Remember the model you chose and the keys you entered; turn tokens into money |
| Spending record and budget | [harness/ledger.py](harness/ledger.py), [harness/budget.py](harness/budget.py) | The estimate in memory is gone after a restart. Every model call is written down with its price at that moment, so you can see what a day or a month cost; a monthly budget reminds you once when you pass it |
| Export | [harness/export.py](harness/export.py) | Your data lives in one folder on one computer. One click packs it into a zip, with secrets left out |
| Interface languages | [harness/i18n.py](harness/i18n.py), [harness/locales/](harness/locales/) | Interface text in Chinese, English and Spanish, from one translation table shared by the web and terminal UIs |
| Assembly | [harness/app.py](harness/app.py), [harness/cli.py](harness/cli.py), [harness/config.py](harness/config.py) | Wire everything together and read the configuration |

Suggested reading order: `agent.py` → `tools/base.py` → `tools/todo.py` → `permissions.py` → `providers/base.py`.
Read the rest as your interest takes you.

## How it works

### The loop

```
You say something
  └─► send [system prompt + tool specs + full conversation history] to the model
        ├─ the model replies with text           → show it to you; the turn ends
        └─ the model says "I want to call tool X" → check permissions → run it → append the result
                                                     to the history → send it all again (loop)
```

The model decides what happens next, every time; the loop just carries it out. That is the difference between an agent
and an ordinary program: an ordinary program's steps are fixed in code, while an agent's steps are chosen by the model as
it goes.

After you tell Claude "Remind me to hand in the report next Friday", the conversation history looks like this
(abridged). The model doesn't actually "call" anything. It outputs a block in a fixed format saying it wants to call
`todo_add`; the harness sees that, runs the tool, and puts the result back into the history as a new message:

```json
[
  {"role": "user", "content": [
    {"type": "text", "text": "<context>Current time: 2026-11-20 14:03 Friday</context>"},
    {"type": "text", "text": "Remind me to hand in the report next Friday"}
  ]},
  {"role": "assistant", "content": [
    {"type": "thinking", "thinking": "Next Friday is November 27…", "signature": "..."},
    {"type": "tool_use", "id": "toolu_1", "name": "todo_add",
     "input": {"title": "Hand in the report", "due": "2026-11-27"}}
  ]},
  {"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "{\"id\": 1, \"title\": \"Hand in the report\", ...}"}
  ]},
  {"role": "assistant", "content": [
    {"type": "text", "text": "Got it: due Friday, November 27."}
  ]}
]
```

The example is translated for this page. Text written for the model stays in Chinese in the code, so the real context
note reads `<context>当前时间：2026-11-20 14:03 星期五</context>`. The current time travels with each user message rather
than in the system prompt, because it changes every time, and a changing system prompt would break the cache.

One rule holds throughout: **the history is only ever appended to; nothing earlier is changed.** The reasons are in the
comment in `AnthropicBackend.__init__` in [harness/providers/claude.py](harness/providers/claude.py). In short, changing
earlier content invalidates the cache (so requests cost more), and it makes the model's earlier thinking fail the API's
validation (so requests are rejected). The one exception is compacting very long GPT, Gemini and DeepSeek
conversations; see [When a conversation gets too long](#when-a-conversation-gets-too-long-compaction).

### One loop, many models

The history above is in Claude's format. Other providers use different formats, but the loop (`agent.py`) doesn't care.
It only requires each adapter to do the following, as defined in [harness/providers/base.py](harness/providers/base.py):

| Method | What it does |
|---|---|
| `add_user` | Append a user message to the history |
| `step` | Call the model once, display the output as it streams in, append the reply to the history, and tell the loop which tools the model wants to call |
| `add_tool_results` | Append tool results to the history |
| `mark` / `rollback` | Remember a position in the history / go back to it (to undo a turn after an error or a refusal) |

There are only two adapters. [claude.py](harness/providers/claude.py) uses Anthropic's native API.
[openai_compat.py](harness/providers/openai_compat.py) uses the Chat Completions format that OpenAI defined; GPT, Gemini
and DeepSeek all support it at different server addresses, so one piece of code serves all three. The main differences:

| | Claude | GPT, Gemini, DeepSeek |
|---|---|---|
| System prompt | A separate parameter | The first message in the history |
| Tool calls | A "content block" in the reply | A `tool_calls` field on the assistant message; the arguments are a JSON string |
| Tool results | All results in one message | One message per result |
| Web search | Yes, run on Anthropic's servers | No |
| Very long conversations | Anthropic's API condenses earlier content into a summary on its servers | ZhiFox asks the model for a summary and replaces the earlier content (see the next section) |
| Caching | We mark it in the request | Each provider handles it automatically |

To add another provider that supports the OpenAI format, add one entry to `PROVIDERS` in
[`harness/providers/__init__.py`](harness/providers/__init__.py).

### When a conversation gets too long: compaction

Every turn resends the whole history, so the longer a conversation gets, the more each turn costs; once it grows past
what the model can read at once (its context window), requests are rejected and the conversation can't go on. The fix
is compaction: replace the earlier part with a summary and keep only the last few turns verbatim.

For Claude, Anthropic's API compacts on its servers (the request carries `context_management`; see
[claude.py](harness/providers/claude.py)), and on our side the history is still only appended to. The OpenAI format has
no such feature, so for GPT, Gemini and DeepSeek ZhiFox does it itself, in
[harness/providers/compaction.py](harness/providers/compaction.py):

1. **When.** Every response reports how many tokens went in and came out, which is how much context the conversation
   now takes up. Once that passes 70% of the model's window, the next request is preceded by a compaction. The
   remaining 30% has to fit the next message, tool results and the model's reply.
2. **Where to cut.** Only between turns, that is, right before one of your messages. A tool call and its result always
   belong to the same turn, so they always end up on the same side of the cut, and the history never holds a call
   without its result (the API would reject that). The last two turns, counting the one in progress, are kept verbatim.
3. **Who writes the summary.** The same model you are talking to. The part before the cut is sent exactly as it was,
   followed by an instruction asking for a summary (the instruction lives in `compaction.py`). Sending it unchanged
   means it hits the cache: that part went out with every earlier request, so its beginning matches and is billed at
   the much lower cache price. The usage of this call is recorded like any other, so the cost estimate includes it.
4. **What replaces it.** The system prompt, a user message holding the summary and an assistant message acknowledging
   it (so the history still alternates between you and the model; some models refuse two user messages in a row),
   then the recent turns verbatim. Attachments in the summarized part aren't kept; one sentence mentions which files
   you sent. When the conversation gets too long again, it is compacted again, and the previous summary is folded into
   the new one.

This is the one exception to "the history is only ever appended to", so its boundaries are drawn tightly: only the
OpenAI-format adapter does it; only the part before the cut changes, never the system prompt or the recent turns; and
only between two requests. Because the history gets shorter, the positions that `mark` hands out are adjusted too, so
undoing a turn after an error still removes exactly that turn. The price is one cache miss: the first request after a
compaction starts differently, so it pays the full input price once, and the new beginning is cached from then on. In
return, the history stops growing without bound.

You see two notices: one while the model writes the summary, and one when it's done, telling you roughly how many
fewer tokens each turn now sends. If writing the summary fails (the network drops, the server is busy), the notice says
why, the turn goes on as usual, and the next turn tries again. In the saved conversation, `messages` (the copy for the
model) is the compacted history, so a reopened conversation continues from the summary, while `events` (the copy for
the page) keeps everything, so replay and search still see the whole conversation.

The tunables are in [harness/config.py](harness/config.py): `ENABLE_OPENAI_COMPACTION` (set it to `False` to turn
compaction off), `CONTEXT_WINDOWS` (each model's window; 1M tokens for both DeepSeek models), `DEFAULT_CONTEXT_WINDOW`
(128K tokens for models not in the table, erring on the side of compacting early), `COMPACTION_THRESHOLD` (0.7) and
`COMPACTION_KEEP_TURNS` (2).

### How conversations are saved and restored

The model remembers nothing, so "continuing where you left off" is entirely the harness's job: it saves the last
conversation's history and sends it along with the next request. Each conversation is one JSON file in
`data/conversations/`, holding two copies of the content:

| Field | Used by | Notes |
|---|---|---|
| `messages` | The model | The history sent to the model, stored exactly in that provider's format. Handed back to the adapter (`restore`) when you open the conversation |
| `events` | The page | What the interface showed. Sent to the browser again when you open the conversation, so the page can redraw it |

An attachment appears in `events` only as a summary: its name, size, a small thumbnail and the SHA-256 of its original
in `data/media/`. The full content is in `messages` (that's what the model reads) and in the media library. The media
library builds its index the same way search does: while the conversations are read and saved, so opening the Media tab
doesn't read every conversation file again.

There are two copies because they differ. The model's history contains thinking signatures, an ID for every tool call
and each provider's own message structure. The page has no use for these, but they must go back to the API exactly as
they were. The page, in turn, shows things the model never sees: approval cards (and whether you clicked Allow or Deny)
and notices such as "Stopped".

Claude's history must be sent back exactly as it was, or its earlier thinking fails validation. The system prompt
includes the working directory, so if you start ZhiFox in a different directory and reopen an old conversation, the
system prompt has changed and that thinking no longer matches. The request includes a parameter telling the API to drop
mismatched thinking instead of rejecting the request. The old conversation still continues; the model just can't see how
it reasoned at the time.

### Three interface languages

Every piece of interface text is written in Chinese where it appears and wrapped in `t()`. The Chinese sentence doubles
as its key in the translation table. The tables live in [harness/locales/](harness/locales/), one JSON file per language,
shaped like `{"Chinese original": "translation"}`:

```python
t("已中断。")                                                        # "Stopped." in the English UI
t("已换成 {provider} · {model}。对话继续。", provider="DeepSeek", model="deepseek-flash")
# → "Switched to DeepSeek · deepseek-flash. The conversation continues."
```

A sentence with no translation is shown in the original Chinese, so a missing translation never breaks anything; the
Chinese just shows through in that one place. [tests/test_i18n.py](tests/test_i18n.py) finds every string passed to
`t()` in the page and the program, and checks that each has an English and a Spanish translation with no placeholder
such as `{provider}` missing. Add a sentence without translating it and the test fails, listing the sentences.

The web UI and the program share the same table: when the program serves the page, it embeds the table in it, and the
page translates its own text. Switching language doesn't reload the page, because every string drawn on the page
remembers its original. On a switch, the page looks each one up again and replaces it in place. Text the program sends
to the page (approval card descriptions, notices) works the same way: besides the translated text, it carries the
original and the values filled into it. So text already on screen switches too, and so does a saved conversation you
reopen later in another language.

What is not translated: the model's answers (it answers in whatever language you write to it), your own messages,
conversation titles, and tool output (such as what a command prints). Tool specs and the text tools return to the model
also stay in Chinese; they are written for the model, which reads them fine. The name "智狐" is the same in all three
languages.

### Search

Search has two jobs: find things fast, and find them at all. The code is all in
[harness/search.py](harness/search.py), with no search library.

**Fast.** Search can't reread every conversation file on each keystroke. The web UI already reads every conversation
file once to list the titles (the first time the list or search is needed), and while doing so it extracts the
searchable text and keeps it in memory. That is the index. From then on, saving, renaming and deleting a conversation
updates the index, and searches run purely in memory.

The index holds: titles, your messages, the assistant's replies, its thinking, the one-line summary of each tool call
(the command that ran, the file path read or written, the web search query), and the first 600 characters of each tool
result. It leaves out the contents the assistant wrote into files, and error notices and notices such as "Stopped".

**At all.** People often remember only roughly what was said, so matching has three tiers, and stricter matches rank
higher:

| Tier | Rule | Example |
|---|---|---|
| Exact | The keyword appears as typed. Case-insensitive; full-width and half-width characters count as the same | `HARNESS` finds `harness` |
| Similar word | An English word of 5 or more letters may have a letter or two wrong. Used only when the word doesn't appear exactly anywhere | `hraness` finds `harness` |
| Characters in order | Every character of the keyword appears in order, with a few others in between. English keywords need at least 4 letters and must stay within one word; Chinese keywords need at least 2 characters | `周五报告` finds "周五下午交报告" |

Separate keywords with spaces. All of them must match, in any order, and they may match in different places. Ranking
looks at the tier first: conversations where every keyword matched exactly come first, then those that needed a similar
word, then those that needed characters in order. Within a tier, matches in the title rank above matches in the content,
and matches in your messages rank above matches in tool output. When relevance is equal, newer conversations come first.

The server computes where to highlight the matches in the results, and there is a trap here. Browser strings count in
UTF-16, where an emoji takes two units; Python counts it as one. So the server converts the positions before sending
them, or every highlight after an emoji would be off.

### One loop, two interfaces

The terminal UI and the web UI also drive the same loop. The loop only needs the interface to provide these methods:

| Method | When it's called |
|---|---|
| `thinking` / `text` | Each time the model produces a piece of thinking or of its answer |
| `tool_call` / `tool_result` | A tool starts / finishes |
| `confirm` | Your approval is needed; the loop waits here for the answer |
| `info` / `notice` | There is a message for you |

The terminal UI ([harness/ui.py](harness/ui.py)) turns these calls straight into text on screen. The web UI takes one
more step, because the loop and the browser are not in the same program:

```
agent loop ──calls──► WebUI ──events──► local server ──pushes──► page in the browser
                   (webui/ui.py)     (webui/server.py)          (webui/index.html)
```

`WebUI` records each call as an event in a log, the server pushes the events to the browser as a Server-Sent Events
(SSE) stream, and the page draws each one. When you click Allow or Stop, the browser sends a request to the server, which
passes it on to `WebUI`. The page keeps no copy of the conversation; after a refresh the server resends the log and the
page redraws from it.

Only content that is happening right now animates. New messages fade in, but content replayed after a refresh or when
opening an old conversation appears directly, rather than playing hundreds of entrance animations one by one. How does
the page tell the two apart? After resending the existing events, the server sends one extra marker named `synced`, and
everything after it is live. When you open an old conversation, everything from the "clear the page" event (`reset`) to
the next "state" event counts as replay.

Everything that moves on the page changes only two properties: position and scale (`transform`) and transparency
(`opacity`). The browser can hand these to its compositor thread without redoing layout, so animations stay smooth even
while the script is busy handling the model's streaming text.

When motion should be reduced (your system asks for reduced motion, or you chose Reduced in the appearance panel), all
animations are off and content simply appears. The page doesn't query the system setting directly. Before the page is
first painted, it combines the system setting with your choice once and writes the result to `<html data-motion>`; the
styles and scripts look only there. That is what lets your Full choice override the system setting, and it keeps a
refresh from starting an animation and then cutting it off. Light and dark themes work the same way
(`<html data-theme>`).

### Markdown in replies

Models like to answer in Markdown, and code, lists and tables are much easier to read once they're drawn. The page
can't load a Markdown library (it loads nothing from outside, and has to work offline), and a full one is thousands
of lines, so ZhiFox has a small renderer of its own: [harness/webui/markdown.js](harness/webui/markdown.js). It covers
what chat replies use: most of CommonMark, plus GitHub's tables, task lists and strikethrough. The server pastes the
file into the page under the same one-time `nonce` as the page's own script, so the security policy stays as strict
as before.

**Safety.** The renderer never builds or parses HTML. It first turns the text into plain objects (a syntax tree),
then creates every element itself with `createElement` and `createTextNode`, so `<script>` or `onerror=` in a reply is
only ever text. Links are made only for `http`, `https` and `mailto` and always open in a new tab with
`rel="noopener noreferrer"`; any other address (`javascript:`, `data:`, `file:`, a relative path) is shown as text.
Images are never loaded: you get their description and address, as a link when the address is safe.

**Two changes for Chinese.** A single line break inside a paragraph stays a line break (CommonMark turns it into a
space; older replies were plain text and relied on line breaks, and Chinese sentences shouldn't gain spaces). And
when deciding whether `**` opens or closes bold, CJK characters count as punctuation, otherwise the very common
`**注意：**这一点` would never turn bold.

**Streaming.** A reply arrives a few characters at a time, and re-parsing the whole reply for every piece would get
slower as it grows. So the page draws at most once per frame, and the renderer splits the text in two. Blocks that
are *settled* (a new block has started after them, on a complete line) are drawn once and never touched again; the
next parse starts after them. Only the last block or two are re-parsed, and compared with what is already drawn, so
only what changed is rebuilt: the next characters of an open code block are appended to its text, and a long list or
table that is still growing re-parses only its last item or row. A half-written marker on the last line (`-`, `1.`,
```` ``` ````, a link whose address is still arriving, a line that may be a table header) is held back until it is
clear what it will become, so raw syntax doesn't flash on screen. Replayed content (a refresh, an old conversation) is
drawn once, in its final form, with no animation.

Search still works: results come from the Markdown source, and when the hit includes syntax that isn't visible
(`**`, `` ` ``), the page drops those marks before looking for it on the page. A hit that only exists in the source
(such as a link's address) scrolls to the reply. The terminal shows the Markdown source as is.

### Scheduled tasks: running when nobody is watching

A task is a saved instruction plus a schedule ([harness/tasks.py](harness/tasks.py)). Running one reuses the same loop,
with three things handled differently ([harness/webui/scheduler.py](harness/webui/scheduler.py)):

1. **When.** A scheduler thread sleeps until the next task is due, at most 30 seconds at a time so that a changed clock
   or a computer waking from sleep is noticed soon, and is woken early whenever a task changes. The next time is worked
   out on the calendar in local time rather than by adding 24 hours, which is what keeps "every day at 9:00" at 9:00 on
   the wall clock across daylight saving. Each task's next time is saved in `data/tasks.json`; finding it in the past on
   the next start is how ZhiFox knows a run was missed.
2. **Side by side.** A run doesn't take your conversation's lock: you keep chatting, switching models and opening
   conversations while tasks run, and several tasks can run at once. That works because everything that can be
   *stopped* is per run (`App.unattended_agent`): its own shell, so stopping a run ends only that run's command, and
   the chat's Stop never reaches a task; its own MCP calls (each run's tool snapshot files its calls under that run,
   so Stop cancels only those); its own browser tabs (closed when the run ends); its own event log and approval card.
   What runs share is what lives on disk (to-dos, tasks, memory, conversations, files in the working folder), and
   each of those has a lock, so every change is made in one go and two runs can't overwrite each other's edit. At most `MAX_PARALLEL_TASKS` runs (3, in
   [config.py](harness/config.py)) go at once; more wait in line, marked "Queued", and start as soon as a slot frees
   up. The same task never runs twice at the same time.
3. **Nobody watching.** The run gets its own `Agent` with a new adapter and an empty history, and its own event log
   (a second `WebUI` whose events aren't pushed to the page), so it can't write into the conversation you're looking
   at. Its permissions are a copy set to *that task's* approval mode (`Permissions.unattended(mode)`), not the one you
   chose for the chat. When it reaches a step that needs approval, the loop waits in `confirm` exactly as it does in a
   conversation, and the waiting approval goes into the page state so you can answer it from the task.

Progress reaches the page in the same `state` event as everything else: at most four times a second while text streams
in, and immediately when a run starts, pauses for approval or ends. A stopped run still gives every tool call its
result (the same `finally` in `Agent._run_tools`), so its saved conversation can be continued like any other.

### Chat channels: talking to ZhiFox from your chat app

A chat channel is one more way into the same loop. Using Telegram as the example
([harness/channels/](harness/channels/)); the other platforms differ only in how messages arrive (see
[Six platforms, three ways in](#six-platforms-three-ways-in) below):

```
your phone ──message──► Telegram's servers ◄──long poll (getUpdates)── ZhiFox (your computer or server)
                                                                        │
       ┌────────────────────────────────────────────────────────────────┘
       ▼
is it the owner (the account you paired)?  no → ignore  ·  yes → run a turn of the agent loop in that chat's conversation
       │
       ▼
each finished paragraph goes back to the chat; a command or file change first arrives as a message with Allow / Deny
```

1. **No public URL.** ZhiFox asks Telegram for new messages instead of waiting for Telegram to call it: one HTTPS
   request (`getUpdates`) that Telegram holds open for up to 25 seconds and answers as soon as a message arrives. The
   `offset` it sends back means "everything before this is handled", and it is saved in `data/channels.json`, so a
   restart never handles a message twice. A home computer or a server behind a firewall works without an open port or
   a tunnel. (WhatsApp's Cloud API and WeCom can only *push* messages to a public URL, which is why they wait for
   phase 2.) The client is hand-written with the standard library
   ([telegram.py](harness/channels/telegram.py)): `getMe`, `getUpdates`, `sendMessage`, `editMessageText`,
   `answerCallbackQuery`, `sendChatAction`, `getFile` and `setMyCommands`, with retries that back off, waiting when
   Telegram rate-limits, and the bot token removed from every error message.
2. **Only the owner.** A bot's name can be found by anyone, but this bot can run commands on your computer. So it is
   paired like a Bluetooth headset ([pairing.py](harness/channels/pairing.py)): the page (or `harness channels pair`)
   shows a random 6-digit code valid for 10 minutes; you send it to the bot from your own account; that account's id
   becomes the owner. Messages from anyone else are ignored without a reply, so the bot looks switched off. During the
   10 minutes of pairing, a stranger who sends something else gets one neutral hint, a wrong code gets "wrong code, N
   tries left", and after 5 wrong codes the code is cancelled, so it can't be guessed one by one. Group chats are ignored entirely (everyone in a group could talk to it).
3. **One conversation per chat, beside the web chat.** Each chat has a `Session`
   ([session.py](harness/channels/session.py)) with its own conversation, adapter, shell, MCP calls, browser tabs and approval mode
   (`App.channel_agent`, the same pattern as a scheduled task's run), and a queue: one turn at a time per chat, further
   messages wait (up to 5). Sessions don't take the web chat's lock, so `/stop` in Telegram stops only that chat, and
   the web page's Stop never reaches it. The conversation is saved like any other (tagged "Telegram" in the sidebar,
   spending recorded with the source `channel`) and continues after a restart. If the same conversation is open in the
   page, each turn first *claims* it ([claims.py](harness/claims.py)): while Telegram runs a turn the page can't send,
   open, rename or delete it, and when the turn ends the page reloads it, so neither side overwrites the other.
4. **Talking back.** The loop talks to a `ChannelUI` ([ui.py](harness/channels/ui.py)), the same event log the web UI
   uses (so the saved conversation replays in the page), which hands each finished paragraph to a per-platform send
   queue ([router.py](harness/channels/router.py)): network sends never block the loop, and the order is kept.
   Replies are converted from Markdown to the small HTML subset Telegram accepts (only `& < >` need escaping, unlike
   MarkdownV2) and split under Telegram's 4,096-character limit, counted in UTF-16 the way Telegram counts, closing and
   reopening a code block that has to be split ([markup.py](harness/channels/markup.py)). If Telegram still rejects the
   formatting, the piece is resent as plain text. While a turn runs, "typing…" is refreshed every 4 seconds.
5. **Approvals.** When a step needs approval, the chat gets a message with the full command (in a code block, nothing
   shortened) and the working folder, and two buttons. A command too long for one message is sent in parts, with the
   buttons on the last one. The same request appears on the web page (a bar at the top of the main area and a card in
   **Extensions → Channels**); whichever answer arrives first counts, and the chat message is edited to say who decided
   ("Allowed on the web page"). You can also just reply "allow" or "deny" (the only way on platforms without buttons);
   typed answers count only once the request has actually reached the chat. In **Allow file edits** or **Allow everything** mode, each step taken
   without asking is still reported in the chat ("Auto-allowed: run_command $ …").
6. **A persona per chat.** `/new researcher` starts a conversation with the custom agent *researcher* as its persona,
   the same as starting one from the page (its prompt, only the tools it allows, its model if it names one).

#### Six platforms, three ways in

Every platform is a `Channel` subclass that turns the platform's events into the same `Inbound` / `Press` objects and
sends what the router hands it ([base.py](harness/channels/base.py)); the router, sessions, pairing, approvals and
the web page don't know which platform they're talking to. None of them needs a public URL, because ZhiFox always
opens the connection:

| Platform | How messages arrive | Approvals | Formatting | Notes |
|---|---|---|---|---|
| Telegram | Long polling (`getUpdates`) | Buttons | HTML subset | 4,096 UTF-16 units per message |
| Discord | Gateway WebSocket ([gateway.py](harness/channels/gateway.py)): Hello, Identify / Resume, heartbeats | Buttons (message components) | Markdown as is (`####` and deeper become bold) | Only the `DIRECT_MESSAGES` intent; reading DMs doesn't need the privileged Message Content intent. Slash commands registered for the bot's DM |
| Slack | Socket Mode WebSocket: each envelope acked at once | Block Kit buttons | mrkdwn (`*bold*`, `<url\|text>`, `& < >` escaped) | Slack intercepts every message starting with `/`, so commands are `/zhifox new`, `/zhifox stop`… |
| Feishu | Long connection through the official SDK `lark-oapi` | Interactive card buttons (falls back to a text prompt) | Markdown in a `post` message's `md` tag | Optional: `uv sync --extra feishu` |
| DingTalk | Stream mode through the official SDK `dingtalk-stream` | Reply "allow" / "deny" (buttons need a card template made in DingTalk's card platform) | DingTalk markdown | Optional: `uv sync --extra dingtalk`. Replies use the message's `sessionWebhook`, then the one-to-one send API once it expires |
| QQ | Gateway WebSocket (the same design as Discord's) with an `access_token` that's renewed | Reply "allow" / "deny" | Plain text | Up to 4 "passive" replies per message within 60 minutes, then proactive messages |

- **The gateway** ([gateway.py](harness/channels/gateway.py)) is written once for Discord and QQ: the first heartbeat
  waits `interval × random()`, a heartbeat that isn't acknowledged before the next one means the connection is dead
  (it is dropped without a close handshake and resumed), op 7 means "resume elsewhere", op 9 says whether the session
  can be resumed. Each platform supplies its own table of close codes: a bad token or a forbidden intent stops the
  channel instead of retrying, because Discord resets a bot's token after 1,000 logins in a day; reconnects back off up
  to 120 seconds for the same reason.
- **The WebSocket client** ([net/websocket.py](harness/net/websocket.py)) implements the part of RFC 6455 a client
  needs: the handshake with its `Sec-WebSocket-Accept` check, masking every frame it sends (and rejecting masked
  frames from the server), fragmented messages with control frames in between, ping/pong, the close handshake, closing
  with 1002/1007/1009 on protocol errors, invalid UTF-8 or oversized messages (refused as soon as the length is read),
  timeouts on everything, and HTTP CONNECT proxies from `HTTPS_PROXY`. Plain `ws://` is accepted only for this
  computer (the test fakes).
- **The official SDKs** are optional dependencies imported only inside [feishu.py](harness/channels/feishu.py) and
  [dingtalk.py](harness/channels/dingtalk.py), and only for receiving: sending, editing cards and downloading files
  are plain HTTPS calls, like the other platforms. The SDKs' rough edges (a module-level event loop, no public way to
  stop, retrying bad credentials forever, logging connection tickets) are worked around in one small `Listener` class
  each, which is also the seam the tests replace.
- **Each new platform** needs three things: a `Channel` subclass, an entry in `hub.FACTORIES` / `CHECKS` / `CLASSES`,
  and its row in [platforms.py](harness/channels/platforms.py) (stage `ready`, the fields to fill in) plus its setup
  steps in [guides.py](harness/channels/guides.py), which the page's wizard and `harness channels setup` both show.

### Three kinds of tools

The difference is who writes the spec and who runs the tool:

| Kind | Spec written by | Runs on | Examples here |
|---|---|---|---|
| Custom tools | Us | Your computer | To-dos ([todo.py](harness/tools/todo.py)), scheduled tasks ([tasks.py](harness/tools/tasks.py)), running commands ([shell.py](harness/tools/shell.py)), reading skills ([skill.py](harness/tools/skill.py)) |
| Anthropic-defined tools | Anthropic | Your computer | Reading and writing files ([files.py](harness/tools/files.py)), memory ([memory.py](harness/tools/memory.py)), the browser for Claude ([browser.py](harness/tools/browser.py)) |
| Server tools | Anthropic | Anthropic's servers | Web search ([web.py](harness/tools/web.py)) |

Claude learned the specs of the second kind during training, so for Claude a one-line declaration is enough. Other
providers' models haven't, so `files.py` and `memory.py` also carry a hand-written spec (`PORTABLE`) for them, and the
browser has a set of twelve hand-written stand-ins; the code that runs is the same. Tools of the third kind live on
Anthropic's servers, so other providers' models can't use them.

A fourth kind arrives through MCP: the spec is written by whoever made the MCP server, and the tool runs in their
program, on your computer or on a remote service. Every provider can use them.

### MCP: tools written by someone else

The tools above are part of this program. [MCP](https://modelcontextprotocol.io) (Model Context Protocol) is an open
protocol for everything else: anyone can package a set of tools (GitHub, a database, a browser…) as an *MCP server*, and
any assistant that speaks MCP, a *client*, can connect to it and hand those tools to its model. ZhiFox is a client,
written by hand in [harness/mcp/](harness/mcp/) without the official SDK, one file per concern:

| File | What it does |
|---|---|
| [jsonrpc.py](harness/mcp/jsonrpc.py) | The envelope: requests, replies and notifications (JSON-RPC 2.0), and waiting for a reply with a timeout |
| [stdio.py](harness/mcp/stdio.py) | Local servers: start the program, one JSON message per line on its stdin and stdout, keep its stderr as a log |
| [http.py](harness/mcp/http.py) | Remote servers: every message is an HTTP POST, and the reply is JSON or a stream of server-sent events |
| [client.py](harness/mcp/client.py) | The protocol itself: find out which generation the server speaks, list its tools page by page, call them |
| [bridge.py](harness/mcp/bridge.py) | MCP tool ↔ ZhiFox tool: names, parameter schemas and results |
| [manager.py](harness/mcp/manager.py) | Starting, stopping and restarting servers, their status, and the per-conversation snapshot |
| [config.py](harness/mcp/config.py), [importers.py](harness/mcp/importers.py) | `data/mcp.json`, hiding secrets from the page, importing from Claude Desktop and Claude Code |

A call travels like this:

```
model: "call mcp__github__search_issues with {...}"
  └─► agent.py        like any other tool: the permission check (asks you by default), then run it
        └─► manager.py      find the connection to the github server
              └─► client.py      wrap it in a tools/call request
                    └─► stdio.py / http.py   write it to the server's stdin, or POST it to its URL
                    ◄── the server's reply
        ◄── bridge.py       turn the text and images in the reply into the result the model sees
```

**Two generations of the protocol.** Up to version 2025-11-25, a connection began with a handshake: the client sent
`initialize` with the version it supports, the server answered with the version and capabilities it chose, the client
confirmed with `notifications/initialized`, and that agreement held for the whole connection. Since 2026-07-28 MCP is
*stateless*: there is no handshake, and every request carries its protocol version, the client's name and its
capabilities in `params._meta`, so the server can handle any request on its own. Servers of both kinds are in use, so
ZhiFox speaks both. It first asks `server/discover` the new way. An answer, or an error only new servers use (such as
"unsupported version"), means a new server. Any other error, or silence, means an old one, and ZhiFox falls back to the
handshake. Some local servers start slowly (`npx` downloads the package the first time), so after a few seconds of
silence ZhiFox sends `initialize` as well and goes with whichever succeeds first.

**Tools are frozen for a conversation.** Every request starts with the tool specs, and Claude caches requests by their
beginning: change one tool halfway through a conversation and the cache for the whole conversation is lost, and Claude's
earlier thinking no longer matches what came before it. MCP servers, though, connect, disconnect and change their tools
whenever they like. So when a conversation sends its first message, ZhiFox takes a snapshot of the tools of the servers
connected at that moment, and the conversation keeps that snapshot to the end. A server that connects or changes later
is used from the next conversation, and its row in the MCP tab says **Next conversation** until then. If a server in the
snapshot goes away, calling its tool returns an error that says so, and the model tries something else.

**Translating names, schemas and results.** Providers accept tool names of at most 64 characters made of letters,
digits, `_` and `-`, and two servers may both have a tool called `search`. ZhiFox names each one
`mcp__<server>__<tool>`, replaces other characters with `_`, and shortens long names with a short hash so they stay
unique. Each description starts by saying which server the tool comes from. Parameter schemas pass through, minus what
providers reject: Claude refuses `anyOf` at the top level, and Gemini refuses `$ref` and `$schema`, so for the
OpenAI-format providers references are expanded in place. In results, text goes to the model, images go to it as images
when the model can see them, embedded resources become text, a structured result is written out as JSON when the server
sent no text, and `isError` marks the result as failed so the model can correct itself.

**Stop, timeouts and crashes.** **Stop** cancels the call in progress: on stdio ZhiFox sends `notifications/cancelled`;
over HTTP it hangs up that request (and also sends the notification to old servers). A call that takes longer than
5 minutes is cancelled the same way (change it per server with `"timeout": seconds` in `data/mcp.json`). A local server
that crashes is restarted automatically, at most 3 times in 10 minutes. When a server announces that its tool list
changed (`notifications/tools/list_changed`), ZhiFox fetches the list again in the background.
### Skills

A model knows a little about everything but nothing about how *you* want a particular kind of task done. Repeating the
same instructions every time is tedious, and putting all of them in the system prompt makes every request carry every
instruction. A **skill** solves this. It is a folder whose `SKILL.md` holds the steps for one kind of task, optionally
with scripts, reference docs and templates next to it. The format is the open [Agent Skills
standard](https://agentskills.io/specification), which Claude, Claude Code and a growing number of other tools use, so a
skill written for any of them installs here unchanged:

```
weekly-report/
├── SKILL.md          ---
│                     name: weekly-report          lowercase letters, digits and hyphens; same as the folder
│                     description: Write the       what it does and when to use it (≤ 1024 characters)
│                       weekly report. Use when…
│                     ---
│                     (the instructions, in Markdown)
├── scripts/          optional: code the model can run
└── references/       optional: documents it reads only when needed
```

**Progressive disclosure.** A skill is loaded in three stages, each only when needed. (1) Every enabled skill's name and
description are always in front of the model, about 100 tokens each, so you can install dozens. (2) When a request
matches a description, the model calls the `skill` tool to read the full `SKILL.md`. (3) Other files are read, and
scripts run, only when the instructions call for them.

**Where the list goes, and why not the system prompt.** Within one conversation the system prompt and the tool specs
must stay byte-for-byte the same (prompt caching depends on it, and Claude's earlier thinking is bound to it), but you
can install or disable a skill at any moment. So the list travels in the user message, like the current time does: the
first message of a conversation carries a `<skills>` block with the skills available then, and before each later
message the current list is compared with the last one the model saw. If they differ, the new list is attached to that
message. "The last one the model saw" is found by reading the conversation history itself, so reopening an old
conversation or rolling back a failed turn needs no extra bookkeeping. The one exception is Claude's automatic
compaction: whatever came before the compaction is no longer sent to the model, so a list there doesn't count. Claude
Code does the same thing, passing the list of skills as an attachment in the messages rather than in its system prompt.
How to use skills is written in that block and in the `skill` tool's description, not in the system prompt: you can
rewrite the system prompt in Settings, and a custom prompt mustn't quietly switch skills off. Scheduled tasks get the
list too, in the first message of each run. See [harness/skills/catalog.py](harness/skills/catalog.py).

**Reading and running.** The `skill` tool is read-only. It reads only enabled skills, and checks every path one level at
a time, so it can't leave that skill's folder or follow a symlink out of it. That's why reading a skill doesn't ask you,
just like the memory tool. Scripts are run by the model with `run_command`, which asks every time like any other
command. A skill can't loosen that: Claude Code treats `allowed-tools` as "use these without asking", but ZhiFox only
displays the field. `${CLAUDE_SKILL_DIR}` in a skill is replaced with the skill's real folder, so Claude Code skills that
use it keep working. A skill marked `disable-model-invocation: true` is left out of the list (it's meant to be used only
when you ask for it by name), and the model can still read it when you do.

**Installing.** Only you can install, change or delete a skill, from the web UI; the model can't. ZhiFox keeps its own
folder, `data/skills/`, and never reads Claude Code's folders on its own: **Import from Claude Code** looks in
`~/.claude/skills/` and the project's `.claude/skills/` only when you click it, and copies the ones you tick, so later
changes on either side don't affect the other. Everything that comes from outside is shown to you before anything is
written (its name, description, files and source) and treated as hostile:

- Links must be https, and redirects are followed only to https. Downloads time out and stop at 20 MB. A GitHub folder
  link (`…/tree/main/skills/pdf`) downloads that branch's archive from `codeload.github.com` in one request, without
  using the GitHub API's hourly quota, and takes the folder out of it. A link to a `SKILL.md` installs the folder it's
  in.
- Archives are refused if any entry is an absolute path, contains `..`, is a symlink, a device or a pipe, or is
  encrypted. There are limits on the number of files (1,000) and the total unpacked size (50 MB), and on names that
  differ only in case (they would overwrite each other on macOS). Files are first written to a hidden folder, which is
  renamed into place at the end, so a failed install never leaves half a skill.
- A name that clashes with an installed skill asks you to rename (the `name` in `SKILL.md` is rewritten to match the
  new folder) or replace.

**The frontmatter parser.** `SKILL.md` starts with a few lines of YAML. Instead of adding a YAML library,
[harness/skills/frontmatter.py](harness/skills/frontmatter.py) reads the small subset the standard uses (plain, quoted
and multi-line values, a one-level `metadata` map, lists) and rejects everything else with a line number: anchors and
aliases (which can blow up exponentially), tags (which some libraries use to construct arbitrary objects), and a `: ` in
an unquoted value (real YAML parsers reject it too, and Claude Code then loads the skill with no fields at all). Values
stay text, so `version: 1.0` is `"1.0"`, not a number. Fields outside the standard, such as Claude Code's `argument-hint`
or `hooks`, are kept in the file untouched.

### Custom agents: subagents and personas

A skill teaches the assistant *how* to do a kind of task. An **agent** is a different assistant altogether: its own
instructions, its own tools and its own model. Each one is a Markdown file in `data/agents/`, in the same format as a
[Claude Code subagent](https://code.claude.com/docs/en/sub-agents), so files written there import unchanged:

```markdown
---
name: researcher                  letters (any script), digits, - and _; also the file name (researcher.md)
description: Researches and       what it's good at and when to hand work to it (≤ 1024 characters)
  reads web pages, then returns a summary with sources.
tools: Read, WebSearch, WebFetch  optional; leave it out to get everything the caller has, [] for none
model: inherit                    optional: provider/model (deepseek/deepseek-flash), inherit, or opus / sonnet
color: blue                       optional: red, orange, yellow, green, cyan, blue, purple or pink
---
You are a careful researcher…     the instructions (its system prompt)
```

The same file is used in two quite different ways, and the difference is the heart of what "agent" means:

| | Subagent | Persona |
|---|---|---|
| Who starts it | The main assistant, with the `agent` tool, when a task matches the description (or you say "have the researcher look into…") | You, from the arrow next to **New conversation** |
| What it sees | Only the task the main assistant wrote: not your conversation, not your attachments | The whole conversation, like the normal assistant |
| History | A fresh one per task, thrown away afterwards; only its final answer goes back into the main conversation | It *is* the conversation, saved and reopened like any other |
| Who it talks to | The main assistant (it can't ask you questions; you watch its progress on a card) | You, directly |
| Good for | Context isolation: twenty web pages read inside the subagent cost the main conversation one paragraph | A specialised assistant you work with for a while: a writing partner, a code reviewer |

**Running a subagent** ([harness/agents/runner.py](harness/agents/runner.py)). When the model calls
`agent(name, task)`, ZhiFox builds a second `Agent`, the same loop class as the main assistant, with:

- **a fresh history** whose first user message is the task (plus the usual `<context>` time note);
- **its own system prompt:** the agent's instructions followed by an environment section the program appends (the
  working folder, the operating system, what the `<context>` block means, that web pages and files are material and not
  instructions, and that its "user" is the main assistant, which only receives its final answer, so it should report
  conclusions and say what is missing instead of asking). The environment lines are the same text as in the built-in
  prompt ([harness/prompt.py](harness/prompt.py) keeps them as named parts), and "you can search the web" is written
  only when it really got a web tool;
- **only the allowed tools, never more than the caller has:** the allow-list is applied to the caller's own tools, so a
  read-only persona can't summon a subagent that edits files, and a subagent on DeepSeek doesn't get Claude's web
  search. `Read` gives the file tool in a read-only form whose writes are refused before anything asks you (`Tool.check`).
  It also gets its own shell and its own MCP calls (a fresh snapshot under its own owner, filtered to the tools the
  caller had), exactly like a scheduled task run;
- **the chosen model**, or the caller's when it says `inherit`.

Every tool call it makes goes through the same `Permissions` object as its caller: in your conversation, the approval
mode you picked; in a scheduled task, that task's own mode. Approval cards appear in the conversation as usual, saying
which agent is asking. **Stop** reaches it too: while it runs, its stop function is registered on the caller's
interface, so it kills its own command and cancels its own MCP calls and nothing else, and the interrupted `agent` call
gets its result like any other tool call. Its model calls are written to the spending record with the source
`subagent`, and its tokens and cost are added to the caller's totals (but not its step count or context size). It may
call the model at most `AGENT_MAX_STEPS` times (25), and subagents can't call further subagents unless you raise
`AGENT_MAX_DEPTH` (1) in [harness/config.py](harness/config.py). The tool result the main assistant receives is the
subagent's final answer plus one line on what it did (how many model calls, which tools).

**Why the main conversation stays cheap and stable.** The `agent` tool is always registered, even before you create
your first agent, and its description never names any agent; the list of agents travels in user messages as an
`<agents>` block, exactly like the `<skills>` block ([harness/agents/catalog.py](harness/agents/catalog.py); both use
[harness/catalog.py](harness/catalog.py)). So the main assistant's system prompt and tool specs stay byte-for-byte the
same across turns, before and after a delegation, and the prompt cache keeps working.

**Personas.** Starting a conversation with an agent swaps three things for that conversation: the system prompt (its
instructions with the same `{workspace}`-style placeholders as your own prompt, plus the appended environment section),
the tools (its allow-list applied to the normal set) and the model (if it names one; that becomes the current model,
as when you reopen an old conversation). The conversation file records the agent's name, the sidebar shows it as a
small colored badge and the top bar shows it as a chip. A persona can't change mid-conversation (the system prompt and
tools mustn't), so you start a new one instead; switching to another provider keeps the persona. Reopening the
conversation rebuilds the prompt from the agent's current file, like the built-in prompt; if the agent has since been
deleted, the conversation continues with the built-in prompt and you're told so.

**Managing agents.** **Extensions → Agent** lists them; each one can be edited in a form (name, description, a model
picked from your synced model lists, a color, a tool checklist grouped into built-in, Skill and MCP, and the
instructions) or as raw Markdown, and both are checked by the same parser as `SKILL.md`
([harness/skills/frontmatter.py](harness/skills/frontmatter.py)). Saving from the form rewrites only the fields you
changed, so comments and fields ZhiFox doesn't use (Claude Code's `permissionMode`, `hooks`…) stay as they were.
Claude Code's tool names work: `Write` and `MultiEdit` mean `Edit`, `Grep`, `Glob` and `LS` mean `Read`, `Task` means
`Agent`, and names ZhiFox doesn't have are kept and listed but ignored. ZhiFox's own `Browser` gives all the browser
tools, when the caller has them (the browser is on); a subagent then works in its caller's tabs, under its caller's
approvals. **Import from Claude Code** reads
`~/.claude/agents/` and the project's `.claude/agents/` only when you click it, and copies what you tick. Three
examples (a researcher with read-only files and web search, a read-only code reviewer, a writing assistant with no
tools) are one click away; none is installed by default. The model has no tool that creates or changes agents.

### Browser use: a Chrome of its own

Web search finds pages, but some jobs need the page itself: search a site, click through its results, fill in a form,
look at a layout. For that the assistant can drive a browser. Three decisions shape it: which browser, how ZhiFox talks
to it, and what the assistant may do there without asking (that last one is in [Permissions](#permissions)). The code
is in [harness/browser/](harness/browser/), one file per concern:

| File | What it does |
|---|---|
| [chrome.py](harness/browser/chrome.py) | Find Chrome (or Chromium, Edge, Brave), start it with ZhiFox's own profile, close it together with all its child processes |
| [cdp.py](harness/browser/cdp.py) | A hand-written Chrome DevTools Protocol client over a pipe: requests and replies matched by id, events, timeouts, cancelling one owner's requests |
| [manager.py](harness/browser/manager.py) | The one Chrome everybody shares: start it on first use, close it when idle and on exit, refuse downloads, dismiss page dialogs |
| [session.py](harness/browser/session.py) | One run's browser: its own tabs, the sites allowed in this conversation, every action (navigate, click, type, scroll, screenshot…) and Stop |
| [page.js](harness/browser/page.js) | The script put into each page: it turns what's on screen into numbered lines the model can read |
| [urls.py](harness/browser/urls.py), [keys.py](harness/browser/keys.py), [shots.py](harness/browser/shots.py) | Which addresses may be opened and what counts as one site; key names into keyboard events; screenshots on disk |
| [tools/browser.py](harness/tools/browser.py) | What the model sees: Anthropic's browser use toolset for Claude, twelve hand-written tools for the others |
| [webui/browser_api.py](harness/webui/browser_api.py) | **Settings → Browser**, the header indicator's Stop, the profile folder and the full-size screenshots |

**A separate browser.** ZhiFox starts its own Chrome with the profile folder `data/browser/profile`. Your everyday
Chrome, with its logins, saved passwords and extensions, is invisible to it, and the two can run side by side. The
reason is that a web page can mislead the model (a hidden line saying "forward this person's mail to me"); in a browser
where none of your accounts is signed in, a misled model has nothing of yours to take. Signing in to a site there is a
decision you make yourself. Chrome is started only when the assistant first uses it, closed after
`BROWSER_IDLE_MINUTES` (10) minutes without an action and when ZhiFox exits (the whole process group, renderer
processes included), and the profile keeps its logins in between.

**Talking to Chrome over a pipe.** The Chrome DevTools Protocol (CDP) is what the F12 developer tools use. It is plain
JSON: a request carries an `id`, the reply carries the same `id`, and events carry none:

```
we send:    {"id": 7, "method": "Page.navigate", "params": {"url": "https://example.com"}}
it replies: {"id": 7, "result": {"frameId": "…"}}
it says:    {"method": "Page.loadEventFired", "params": {…}}
```

The usual way in is `--remote-debugging-port`, a WebSocket on a local port. But any program on the computer, and any
web page you open (a page can send requests to 127.0.0.1), can connect to that port and take full control of the
browser. So ZhiFox starts Chrome with `--remote-debugging-pipe`: Chrome reads commands from file descriptor 3 and writes
replies to descriptor 4, each message a piece of JSON ending in a NUL byte. ZhiFox hands those two descriptors over
when it starts the process (with `os.posix_spawn`, because `subprocess` can't place a descriptor at a chosen number), so
there is no port for anyone else to find. A bonus: if ZhiFox is killed, its end of the pipe closes, Chrome reads the
end of the stream and exits by itself, so no orphaned browser is left behind. One background thread does the reading,
handing replies to whoever waits on that id and events to whoever subscribed. Each tab is reached through a session id
on the same connection ("flat" sessions). Every request has a timeout, and every request is filed under an owner, so
Stop cancels one run's requests and leaves everyone else's alone.

**Reading a page: lines with numbers.** A screenshot costs many tokens and a model can't reliably click on a picture,
so the main way to read a page is text. `page.js` runs in an *isolated world*: it shares the page's DOM but not its
JavaScript, so the page can neither see what it records nor replace the functions it uses. It walks the rendered page
and writes one line per meaningful element, giving each one that can be acted on a number:

```
banner
  link "Umbrellas" [ref_1] → /umbrellas
  searchbox "Search products" [ref_4]
  button "Search" [ref_5]
heading "Folding umbrella" [ref_6]
```

The model answers with something like `left_click {"target": {"type": "ref", "ref": "ref_4"}}`. ZhiFox finds that
element, measures where its centre is and sends real mouse events there (`Input.dispatchMouseEvent`), so the page sees
an ordinary click. Viewport coordinates work too, for what the text can't show (a canvas, a frame from another site).
Three details matter. Text that isn't displayed (`display: none`, `visibility: hidden`, `aria-hidden`) is skipped, so a
page can't hide instructions for the model in places you'd never see. The numbers keep counting up per tab instead of
starting over on each page, so after a navigation an old `ref_4` fails with "read the page again" instead of quietly
pointing at a different element of the new page. And a password field is listed without its value. (The element names
come from the page; the few words ZhiFox adds, such as "(password field)", are in Chinese, like everything written for
the model.)

**Two sets of specs for one browser.** Claude models listed in `BROWSER_TOOLSET_MODELS` (all the Claude models ZhiFox
offers) get Anthropic's [browser use toolset](https://platform.claude.com/docs/en/agents-and-tools/tool-use/browser-use-tool),
`browser_toolset_20260801`: a single entry in `tools` that stands for 27 members (`navigate`, `read_page`, `find`,
`left_click`, `type`, `form_input`, `screenshot`, the tab actions…). It is the second kind of tool above: Anthropic
wrote the spec and trained the model on it, and the actions run on your computer. Anthropic also scans the page content
sent back to the model for prompt injections and warns the model when it finds one. The toolset has a few rules of its
own ([claude.py](harness/providers/claude.py), [agent.py](harness/agent.py)): a result repeats the call's
`toolset_name`; the state of the tabs travels as a `browser_state` block (never on an error), and the four tab actions
return only that block; and when one call in a batch ("click the box, type, press Enter") fails or you deny it, the rest
of that turn's calls aren't run and get exactly `Not executed: an earlier action in this turn failed.` The price is
about 6,600 extra input tokens on every request (cached like the rest of the start), which is why the browser tools
are only in conversations started while the browser is on. GPT, Gemini and DeepSeek (and a Claude model without the
toolset) get twelve hand-written function tools instead: `browser_navigate`, `browser_read`, `browser_find`,
`browser_text`, `browser_click`, `browser_type`, `browser_select`, `browser_press`, `browser_scroll`, `browser_wait`,
`browser_screenshot` and `browser_tabs`, whose descriptions say that page content is third-party material. Both sets
drive the same `BrowserSession` and go through the same permission rules. A screenshot goes into the tool result only
for models whose adapter can pass images on; the small preview taken after each step is drawn on the tool card for
you and never sent to the model.

**Frozen for a conversation, like MCP.** The browser tools are part of the conversation's fixed start, so they follow
the same snapshot rule as MCP tools. Turning the browser on mid-conversation takes effect from the next conversation.
Turning it off mid-conversation keeps the tools (the specs mustn't change), and calling one answers "the browser is
off". An old conversation that used the browser keeps its browser tools when you reopen it, so the calls in its history
still match a spec.

**Each run gets its own tabs.** Your conversation, every scheduled task run and every chat channel conversation has
its own `BrowserSession` on the shared Chrome: its own tabs (`tab-1`, `tab-2`…, counted per session), its own list of
allowed sites and its own Stop. A task checking the weather while you research something can't see or click your tabs.
A task run closes its tabs when it ends, and starting a new conversation closes the previous one's. The chat's Stop, a
task's Stop and `/stop` in Telegram each abort only that run's current browser action; the Stop on the header
indicator aborts all of them and closes the browser (the next time each run tries the browser, it is told you closed
it).

**What a page can't make it do.** Downloads are refused (`Browser.setDownloadBehavior` set to `deny`) and the model is
told one was attempted: the assistant wants what's on the page, not files on your disk. A page's `alert`, `confirm` and
`prompt` dialogs are dismissed automatically (they would block the page) and reported to the model. File choosers are
intercepted, so a page can't open a file picker and ZhiFox never uploads your files. Every page read is labelled in the
tool result as material from a third-party site, not instructions. Chrome's environment has the model API keys removed,
like a shell command's.

Computer use of the whole desktop (screenshots of and control over your entire screen, not just one browser) would be a
later, separate option; it isn't built.

The tunables are in [harness/config.py](harness/config.py): `BROWSER_VIEWPORT` (1280×800, also the screenshot size),
`BROWSER_SHOT_QUALITY` (JPEG, 60), `BROWSER_IDLE_MINUTES` (10), `BROWSER_SHOT_DAYS` (30), `BROWSER_CALL_TIMEOUT`,
`BROWSER_NAV_TIMEOUT`, `BROWSER_MAX_PAGE_CHARS` (50,000 characters per read) and `BROWSER_TOOLSET_MODELS`.

### Permissions

| Action | Handling |
|---|---|
| To-dos, memory | Runs directly (only touches the assistant's own files in `data/`) |
| Reading an enabled skill | Runs directly (read-only, and it can't leave that skill's folder). A skill's scripts run through `run_command`, so they ask |
| Handing a task to a subagent (`agent`) | Runs directly (it only calls a model). Every tool call the subagent makes is reviewed by this same table, with its caller's approval mode, and the card says which agent is asking |
| Adding or deleting a reminder | Runs directly (at the set time it only shows a reminder: no model call, no cost) |
| Adding or deleting a task the assistant runs on its own | Asks you first, and shows the schedule and the instruction (it will spend money later, with nobody watching) |
| Reading files in the working directory | Runs directly |
| Reading files outside the working directory | Asks you first |
| Reading files in `data/` (saved conversations, settings) | Asks you first, even when `data/` is inside the working directory |
| Creating or changing any file | Asks you first, and shows what will be written |
| Running any shell command | Asks you first, and shows the full command |
| Web search | Runs directly (on Anthropic's servers, without touching your computer), with a cap on the number of searches |
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

An MCP tool is someone else's program. ZhiFox can't see what it does, and what it says about itself ("read-only")
isn't verified, so every call asks by default. In **Extensions → MCP** you can mark one tool, or a whole server, as
**don't ask**. That is your decision about one specific tool, so it is saved in `data/mcp.json` and applies in every
mode, and those calls are still marked **Allowed automatically** in the conversation. The one exception is a scheduled
task running on its own: it follows only its own approval mode, so an MCP call waits for your approval unless that
task is set to Allow everything.

Reading `data/` always asks because otherwise the model could quietly read every past conversation and send it to the
provider you're using now, whether because you asked "what files are in this folder?" or because a web page misled it.

The browser rules follow one question: can this step send something out? Looking can't, because the browser holds none
of your accounts. Typing can (the text may be your information), and so can submitting a form. So can opening a new
site, because an address carries data with it: `evil.example/?data=<your mail>`. A click on a link already on the page
doesn't ask: the site put that link there, so it can't carry anything the page doesn't already have. Password and card
fields are what a misled model would want most, so they ask even under **Allow everything**, as does typing into a
frame from another site (ZhiFox can't see what's inside; it's often a payment form). Sites allowed in a conversation
are remembered by that conversation's browser session only, so a new conversation asks again. Scheduled tasks and chat
channels apply the same rules with their own approval mode.

When you deny a step, you can type a sentence (for example, "Don't delete it, move it to the Trash instead"), and it is
passed on to the model.

If you'd rather not approve every step, you can loosen when ZhiFox asks. In the web UI, use the **Ask every time** button
below the input box; in the terminal, use `/auto`:

| Option | Terminal command | Effect |
|---|---|---|
| Ask every time (default) | `/auto ask` | Everything marked "Asks you first" above asks, every time |
| Allow file edits | `/auto edits` | Creating and changing files in the working directory no longer asks. Running commands, reading or writing outside the working directory, reading `data/` and browser steps still ask |
| Allow everything | `/auto all` | Nothing asks, except the browser steps marked "in every mode" above |

This only changes whether ZhiFox asks. Steps that ran without asking are marked **Allowed automatically** on their tool card in
the web UI, and noted as "(allowed automatically)" in the terminal. The choice isn't saved: every launch starts again at Ask
every time, so you can't turn it on and forget about it.

Scheduled tasks don't follow this choice. Each agent task has its own setting with the same three options, chosen in the
task editor and saved with the task (it defaults to Ask every time). Loosening the chat when you're watching says nothing
about a task that runs at 3 a.m. Tasks the assistant creates for you are always Ask every time, and its tool has no way
to change that: only you can, in the page. Otherwise a model misled by a web page could set itself a task that runs any
command, unasked, every night.

Scheduled tasks are the exception. When one runs on its own, every step marked "Asks you first" pauses the run until you
answer, whichever option you chose: loosening the prompts means "I'm here, go ahead", which isn't true at 3 a.m. (see
[Scheduled and recurring tasks](#scheduled-and-recurring-tasks)).

Two hard limits apply regardless of any prompt: the assistant can't read or write `.env` (which holds your API keys),
and the commands it runs can't see those keys either.

A risk to understand: the approval prompt is the only checkpoint between you and the model. Before approving a command,
check what it does, especially right after the assistant has read a web page or an unfamiliar file, because that
content may contain text designed to mislead the model.

## Using ZhiFox

### Web UI

```bash
uv run harness --web
```

This starts a small server on your computer and opens the chat page in your browser. The page has:

- **Sidebar (left).** All conversations, grouped by date. Click one to continue it. Hovering over a conversation shows
  three buttons: **Show in Finder** (**Show in folder** on Linux), which points straight at that conversation's file on
  your computer, **Rename** and **Delete**. Conversations save automatically at the end of every turn, with nothing to
  do by hand. The button at the top left hides the sidebar; clicking the bottom row opens the folder where conversations
  are saved.
- **Search** (top of the sidebar, ⌘F or Ctrl+F; ⌘K and Ctrl+K work too, and pressing ⌘F again while you're in the
  search box hands over to the browser's own find-in-page). Searches all saved conversations, titles and content, without needing
  an exact match. Separate keywords with spaces. Each result shows the passage that matched; open it (or press Enter to
  open the first) and the page scrolls straight to that spot.
- **Empty state.** Before there is any content, a greeting, the input box and a few clickable examples sit in the middle
  of the page.
- **Conversation.** Replies stream in as they're written and are shown as Markdown: headings, bold and italic, lists
  and task lists, quotes, tables (a wide table scrolls sideways inside its own frame) and code blocks with a language
  label, light syntax colors and a **Copy code** button. Links open in a new tab; images aren't loaded, you see their
  description and address instead. You can expand the model's thinking. Each tool call is a card you can open to see
  its parameters and result; a browser step also carries a small screenshot you can click to enlarge. Hovering over a reply shows a **Copy** button, which copies the reply's Markdown source.
  When you scroll up to read earlier content, new output doesn't pull you back to the bottom.
- **Approval card.** Appears when the assistant wants to write a file or run a command, with **Allow** and **Deny**
  buttons. When you deny, you can add a reason.
- **Input box.** Enter sends; Shift+Enter starts a new line. While the assistant works, the send button becomes
  **Stop**, which interrupts the reply and ends any command that is running. The estimated cost of this run is always
  shown below the input.
- **Attachments.** Click the paper clip left of the input, drop files anywhere on the page, or paste a screenshot
  straight into the input (⌘V / Ctrl+V). Before you send, attachments sit above the input as small cards you can
  remove; a message can carry up to 10, and you can send attachments without any text. The providers' APIs take only
  a few image types, PDFs and text, so everything else is converted first:

  | File | What is sent | Claude | GPT | Gemini | DeepSeek |
  |---|---|:-:|:-:|:-:|:-:|
  | PNG, JPEG, WebP | The image. A long edge over 2,576 px is scaled down in the browser; up to about 7 MB | ✅ | ✅ | ✅ | ❌ |
  | GIF, BMP, AVIF, HEIC, TIFF | Redrawn as PNG or JPEG in the browser (a GIF becomes its first frame). HEIC and TIFF only where the browser can open them, such as Safari | ✅ | ✅ | ✅ | ❌ |
  | SVG | Drawn as a PNG. With a model that can't see images, its source is sent as text | ✅ | ✅ | ✅ | ✅ as text |
  | PDF (up to 25 MB, no password) | The PDF itself: the model reads the text and what's drawn on the pages | ✅ | ✅ | ❌ | ❌ |
  | Word, Excel, PowerPoint (.docx, .xlsx, .pptx; up to 25 MB) | The text, taken out by ZhiFox: Word keeps headings, lists and tables; Excel gives every sheet as tab-separated rows with dates restored; PowerPoint gives every slide in order with its speaker notes. Pictures and layout are dropped, and the model is told so | ✅ | ✅ | ✅ | ✅ |
  | Text and code (up to 400,000 characters) | The text. UTF-8, UTF-16 and GBK are recognized; a file with an unknown extension is taken if its content is text | ✅ | ✅ | ✅ | ✅ |

  Anything else (archives, audio and video, old Office formats such as .doc, Pages, WPS, password-protected files) is
  turned away when you add it, with a note on how to convert it. If the current model can't take an attachment, ZhiFox
  says so right away and names models that can. The GPT and Gemini columns follow their official documentation and are
  tested against local fake servers, not real keys. Attachments are saved with the conversation, so their thumbnails
  come back when you reopen it. Click any attachment in a message to open it in the viewer described under **Media**.
- **Media** (the third tab in the sidebar, after Conversations and Tasks). Every screenshot, image, PDF,
  Word / Excel / PowerPoint and text file you have sent in any conversation, newest first and grouped by day: images as
  a grid of square thumbnails, files as rows with their type, size and conversation. Filter by type (the chips show how
  many of each), search file names and conversation titles, and keep scrolling to load more. A new attachment shows up
  as soon as you send it. Hovering an item shows **Show in Finder**, **Go to message** and **Download**.
  Clicking it opens the viewer, the same one message thumbnails open: zoom with the buttons, ⌘/Ctrl + / − / 0, a
  trackpad pinch or ⌘/Ctrl + scroll; drag (or scroll) to pan once zoomed in; double-click switches between fit to screen
  and actual size; ← / → step through the current filter (or through the attachments of that message), and on a phone
  you can swipe. From the viewer you can copy an image, download the original, show it in Finder, or **go to the
  message** that carried it: ZhiFox opens that conversation (not while a turn is running) and scrolls to the attachment,
  which lights up briefly. Text and code open in a monospace view you can copy from. PDFs and Office files get a card
  with a download button: the page's security policy blocks embedded PDFs, so they open in your own viewer.
  The originals are kept in `data/media/`, so Word, Excel and PowerPoint files are kept too, not just the text taken from
  them. Attachments sent before the media library existed are recovered from the conversation's history (images, PDFs and
  text); for old Office files only the extracted text is left, and the library says so. A single attachment can't be
  deleted, because it is part of the conversation's record; deleting the conversation deletes its originals unless
  another conversation uses the same file.
- **Settings** (the button at the top right that shows the current model). Add or delete each provider's API key, sync
  each provider's model list, choose and switch models, enter model prices, and **edit the system prompt** (the text
  that tells the assistant who it is and how to work). The prompt editor has a preview of exactly what the model receives
  and a list of placeholders such as `{name}` and `{workspace}` that are filled in before sending; other braces are left
  alone. Your version is saved as `data/prompt.md` (you can edit that file directly too) and applies from the next new
  conversation, because changing the prompt in the middle of one would break Claude's cache and its earlier reasoning;
  a conversation that hasn't started yet switches right away. **Restore default** goes back to the built-in prompt, which
  is also the way to pick up a newer built-in version after an upgrade. Three more sections at the bottom:
  - **Monthly budget** (US dollars, optional). When this month's spending passes it, ZhiFox reminds you once (a
    message on the page, and a system notification if the page is in the background and you allowed notifications).
    It never blocks anything. Raise the budget and pass the new amount, and you're reminded again; the reminder is
    remembered across restarts, so it doesn't repeat. If you set a budget below what you've already spent this month,
    the panel says so right away.
  - **Browser** (off by default): let the assistant use ZhiFox's own browser, hide its window, choose which Chrome,
    and the profile folder. See [Browser use](#browser-use).
  - **Export all data** downloads the whole data folder as one zip. It shows the folder's size first and the zip's size
    afterwards. See [Backup and restore](#backup-and-restore).
- **Info panel** (opened and closed by the right-most button at the top right). Estimated cost, the split across the four
  kinds of tokens (uncached input, cache hits, cache writes, output), open to-dos, and the tools the model can use. It
  updates after every model call. Under this run's estimate, the cost card shows what you've spent **today** and
  **this month** (from the spending record, so restarts don't reset it), how much of your monthly budget is left, and a
  red note once you're over it (the cost next to the input box gets a red dot too). **Spending** opens a window with
  the last 30 days as a bar chart (hover a bar, click it, or use the arrow keys and Home / End to read a day), a total
  for every month, and a breakdown by model and by source (chat or scheduled task) for this month, the last 30 days or
  all time. Calls to models without a price are listed separately with their tokens. Click a to-do's circle to complete
  it; hover over it for the same three buttons as every other row (**Show in Finder** for the to-do file, **edit** its
  text and due date in place, **delete** after an in-place confirm). Scheduled task rows and the prompt editor have a
  **Show in Finder** button for their files too.
- **Memory** (fixed at the top of the sidebar, with a count; it opens a two-pane window: the list on the left, searchable
  by name, and the selected memory on the right; on a narrow screen it shows one side at a time). Every file the
  assistant keeps in its long-term memory (`data/memories/`), with its
  folder, size and when it last changed. Click one to read it. Hover over a row (or move to it with Tab) for the same
  three buttons as a conversation row: **Show in Finder** selects that file in your file manager, the **pencil** opens
  it for editing (change its name and its text, ⌘Enter / Ctrl+Enter saves, Esc stops editing and warns you first if
  there are unsaved changes), and the **trash can** deletes it after you confirm in place. Editing and deleting wait
  until the current turn ends, since the assistant may be changing that memory. If the file changed while you were
  editing it, saving asks before overwriting. Files that are too long to load in full, or aren't UTF-8 text, can only be
  edited in the folder. **Open the memory folder in Finder** (**Open the memory folder** on Linux) at the bottom opens
  the whole folder. The list keeps
  itself up to date: a memory the assistant writes appears while the turn is still running, and changes you make in
  Finder appear as soon as you switch back to the page. Deleting waits until the assistant has finished its turn,
  because it may be in the middle of editing that very memory.
- **Extensions → Skill** (the puzzle-piece button at the top right). Your installed skills, each with its description,
  number of files and where it came from. Each row has a switch to enable or disable the skill, and on hover (or Tab) the
  same three buttons as a conversation row: **Show in Finder** (that skill's folder), the **pencil** (edit `SKILL.md`;
  the frontmatter is checked when you save, a changed `name` renames the folder too, and unsaved changes are guarded the
  same way as memory edits) and the **trash can** (delete, after confirming in place). Click a row to open it: the
  description and details, any problems that keep it from being used, and every file in the folder, each viewable as
  plain text. Above the list:
  - **New** writes a skill from a form (name, description, instructions) and checks the name as you type.
  - **Add from URL** takes a GitHub folder or repository link, a link to a `SKILL.md`, or a link to a `.zip` / `.skill`
    file. When a repository holds several skills, you pick which ones to install.
  - **Upload** takes a `.zip` or `.skill` file, a single `SKILL.md`, or a whole folder; you can also drag them onto the
    page.
  - **Import from Claude Code** lists the skills in `~/.claude/skills/` and the project's `.claude/skills/`; tick the
    ones to copy.
  Everything is shown to you before it's installed, with a reminder that the assistant follows a skill's instructions,
  so you should install only from sources you trust. Changes take effect from your next message. Like memory edits,
  changing, deleting or installing a skill waits until the current turn ends.
- **Extensions → Agent** (the puzzle-piece button at the top right). Your custom agents, each with its color, name,
  description, model and number of tools. On hover (or Tab) a row shows **Start a new conversation with it** followed by
  the same three buttons as a conversation row: **Show in Finder** (its `.md` file), **Edit** and **Delete** (confirmed
  in place). Click a row to see its model, tools and instructions. The editor has two modes, **Form** and **Source**
  (the whole file), and switching between them goes through ZhiFox, which writes the form into the file and reads the
  file back into the form, so the rules live in one place. Unsaved changes are guarded like memory edits. Above the
  list: **New**, **Examples** (the researcher, the code reviewer and the writing assistant) and **Import from Claude
  Code**. When there are no agents yet, the page explains the two ways to use one and shows the examples. Editing an
  agent doesn't have to wait for the current turn: a running conversation or subagent keeps the version it started with
  (see [Custom agents](#custom-agents-subagents-and-personas)).
- **Start with an agent.** The arrow next to **New conversation** opens a menu of your agents (arrow keys, Enter and Esc
  work); pick one to start a conversation with it as the persona. The top bar shows its name (click it to open the
  agent), and the conversation's row in the sidebar carries a small badge. When the main assistant hands a task to a
  subagent, the conversation shows a card **Handed to *name*: …** with what the subagent is doing right now (thinking,
  writing, calling a tool, waiting for you), each tool call it made, its answer rendered as Markdown, and its model,
  number of calls and cost.
- **Extensions → MCP** (the puzzle-piece button at the top right). The MCP servers ZhiFox connects to, each with its
  status (connecting, connected, failed with the reason and the last line the server printed, or disabled), its
  number of tools and an on/off switch. Hovering over a row (or moving to it with Tab) shows **Restart** and the same
  three buttons as a conversation row: **Show in Finder** (`data/mcp.json`), **Edit** and **Delete** (confirmed in
  place). Click a server to see how it's connected, which protocol generation it speaks, every tool with its
  description and a **don't ask** switch, and its log. **Add server** opens a form: a local program (command, one
  argument per line, working folder, environment variables) or a URL (with headers such as `Authorization`).
  Secret values are typed into password fields, and afterwards the page shows only their last 4 characters; leave one
  blank when editing to keep it. **Paste JSON** takes a configuration copied from Claude Desktop, Claude Code, Cursor
  or VS Code, and **Import from Claude** lists the servers configured in Claude Desktop and Claude Code (read only when
  you open that page) so you can tick the ones to copy. Changes apply from the next conversation (see
  [MCP](#mcp-tools-written-by-someone-else)).
- **Appearance and motion** (the sun/moon button at the top right). A small panel with two rows:
  - **Appearance:** Light by default. You can switch to Dark, or to Match system.
  - **Motion:** follows your system setting by default (on macOS, System Settings → Accessibility → Display → Reduce
    motion; on Windows, Settings → Accessibility → Visual effects → Animation effects). You can also choose **Reduced**
    (no sliding, fading or pulsing; content simply appears) or **Full** (every animation plays, even if your system asks
    for reduced motion).

  Click outside the panel, press Esc or click the button again to close it.
- **Language** (the globe button at the top right): 中文, English, Español. Switching doesn't reload the page: the
  conversation in progress, your scroll position and a half-written message all stay as they were, and the interface
  text changes in place. The choice is saved, and the terminal UI uses it too.

Appearance, motion, and whether the two side panels are open are remembered by your browser, not stored in `data/`.

**Switching models.** Within the same provider, the conversation continues. Switching to another provider starts a new
conversation, because providers' conversation formats aren't compatible. The model you choose is saved, and the next
launch (terminal included) uses it again.

**Lifecycle.** Refreshing the page doesn't lose the current conversation. Closing the page leaves the program running;
press Ctrl-C in the terminal to quit. If the assistant is in the middle of a reply, ZhiFox stops it and saves the turn
first (waiting at most 5 seconds), then exits. Only one web UI can run per data directory: if one is already running,
starting another tells you to use the one that's open.

After a restart you start in a new conversation; earlier ones are in the sidebar. Opening an old conversation switches
back to the provider and model it used, and from then on that model is your chosen model: new conversations and the next
launch use it until you change it in Settings. While the assistant is replying, you can't open, rename or delete
conversations; wait for the turn to finish or click **Stop**. Conversation titles keep at most 40 characters.

**Options.** `--port 9000` sets the port. Without it, ZhiFox uses 8765, or lets the system pick a free port if 8765 is
taken; a port you name explicitly must be free. `--no-browser` skips opening the browser.

The address shown in the terminal contains a `token`, a password for this run that changes on every launch. Don't send
the address to anyone, and don't post screenshots that show the address bar: whoever has it can use the page to make the
assistant run commands on your computer. The server only accepts connections from your own machine.

### Scheduled and recurring tasks

Tell the assistant "remind me to drink water every morning at 9" or "every weekday at 9, look at my to-dos and write a
plan for the day", or add a task by hand in the web UI. There are two kinds:

| Kind | What happens at the set time | Cost |
|---|---|---|
| **Reminder** | A reminder appears in the page with the task's title and text. No model is called | Free |
| **Agent** | The assistant carries out the task's instruction on its own, as if you had sent it as a message, calling the model and using its tools | Every run costs API credits, like a normal turn |

Schedules use your computer's local time: once at a date and time, every day, on chosen days of the week, on a day of
the month (the 31st means the last day in shorter months), or every N minutes (5 minutes to 7 days, counted from when the
task was created or last ran). Daylight saving is handled on the calendar: "every day at 9:00" stays at 9:00 on the wall
clock, a time skipped when the clocks spring forward runs right after the jump, and a time repeated when they fall back
runs only once. Before you save a task, the form shows the next three times it will run.

How agent tasks behave:

- **They run alongside you.** A task that comes due while you're chatting starts right away; you can keep chatting,
  switch models or open conversations while it runs, and several tasks can run at once. Each run has its own shell, so
  stopping one task ends only its command, and the Stop button in your conversation never touches a task. Up to three
  run at the same time; the rest wait in line, shown as **Queued**, and start as soon as one finishes. Changing the
  model or a task's settings affects only later runs.
- **They leave your conversation alone.** Each run starts with an empty history, uses the provider and model selected
  in Settings at the moment it starts, and is saved as a normal conversation named after the task and the time (for
  example "Daily plan · 2026-11-21 09:00"). It appears in the sidebar marked as coming from a task, and you can open
  it, replay it, search it and keep chatting in it.
- **Each task decides when to ask.** In the task editor, an agent task has the same three options as the chat's
  approval setting: **Ask every time** (the default), **Allow file edits** and **Allow everything**, the last with a
  warning, since a task usually runs while nobody is watching. A row shows a small badge when its task is not Ask
  every time, and the task details show the setting. Anything that still needs approval pauses the run until you
  answer: the waiting approval is shown with the task, where you can allow or deny it, while you go on chatting.
  Steps that ran without asking are marked **Allowed automatically** in the run's progress and in its saved
  conversation. The chat's own setting doesn't apply to tasks, and tasks the assistant creates are always Ask every
  time (only you can change that).
- **You can watch and stop them.** While a task runs you can see what it is doing (thinking, writing, which tool), how
  many model calls it has made, its cost so far and the tool calls so far. **Stop** works like the Stop button in a
  conversation and ends that run's command; a run still waiting in line is simply dropped. When several tasks are
  running, the pill at the top of the page counts them ("Scheduled tasks: 2 running, 1 waiting for you"); clicking it
  opens the task list at the first one that needs you.
- **Missed runs are caught up once.** If ZhiFox wasn't running at the scheduled time (or the computer was asleep), the
  task runs once the next time ZhiFox notices, however many times it was missed. The run is marked as a catch-up with
  the number of missed times, and the assistant is told when it should have run.
- **Every run is recorded.** Each task keeps its last 20 runs: start and end time, status, number of model calls,
  estimated cost, the beginning of the final answer, and the error if it failed. There is no spending cap.

**Tasks run only while the web UI (`uv run harness --web`) or the server mode (`uv run harness serve`) is running.**
The terminal UI doesn't run them; you can still ask the assistant there to add or delete tasks, and they run the next
time the web UI or the server mode is running. **Run now** runs a
task once without changing its schedule. When the assistant adds or deletes an agent task for you, it asks you first,
because that task will spend money later on its own; reminders don't ask.

### Chat channels

Open **Extensions → Channels** and pick a platform. A four-step wizard walks you through it:

1. **Create a bot.** The wizard shows the platform's steps (below), with links to its developer console.
2. **Paste the token** (or the ID and secret). ZhiFox checks it once with the platform and shows the bot's name. The
   secrets are stored only in `data/channels.json` (readable by your account only); the page shows their last 4
   characters from then on. Editing later, you only paste what changes: fields left empty keep their saved value.
3. **Pair.** A 6-digit code appears with a countdown. Send it to your bot from your own account. On Telegram, **Open in
   Telegram** sends it for you when you press Start; on the others the button opens the chat with the bot. The page
   moves on by itself once you're paired.
4. **Approval mode.** Ask every time (the default), Allow file edits, or Allow everything, for this channel only. Allow
   everything comes with a warning: with it, one message in the chat can make the assistant run any command.

Setting up each platform (the wizard and `harness channels setup <platform>` print the same steps):

- **Telegram.** In [@BotFather](https://t.me/BotFather) send `/newbot`, pick a name and a username ending in `bot`,
  and copy the token (`123456789:AAH…`).
- **Discord.** In the [Developer Portal](https://discord.com/developers/applications): **New Application** → **Bot** →
  **Reset Token** and copy it (it's shown once). On **Installation**, tick **User Install**. After you paste the token,
  the pairing step gives you a link to add the bot to your own account (**Add to My Apps**); then open a DM with it.
  You don't need the Message Content intent: DMs with the bot are exempt.
- **Slack.** [Create an app](https://api.slack.com/apps?new_app=1) **From a manifest** and paste the one the wizard
  copies for you (bot scopes, `message.im`, interactivity, Socket Mode and the `/zhifox` command, all set). Then
  **OAuth & Permissions → Install to Workspace** gives the Bot token (`xoxb-…`), and **Basic Information → App-Level
  Tokens → Generate** with the `connections:write` scope gives the App-level token (`xapp-…`). Chat in the app's
  **Messages** tab; commands are `/zhifox new`, `/zhifox stop`, `/zhifox status`, `/zhifox help`.
- **Feishu.** Run `uv sync --extra feishu` first. In the [Feishu open platform](https://open.feishu.cn/app), create a
  custom app (企业自建应用), copy the **App ID** and **App Secret**, add the **Bot** capability and the permissions to
  read the bot's one-to-one messages, send messages as the app, and read and upload images and files. Connect ZhiFox
  first (it must be running when you save the next setting), then under **Events & Callbacks** choose *receive events
  through a persistent connection* for both events and callbacks, add the event `im.message.receive_v1` and the
  callback `card.action.trigger`, and publish a version.
- **DingTalk.** Run `uv sync --extra dingtalk` first. In the [DingTalk developer console](https://open-dev.dingtalk.com)
  create an app (no company? create your own team in DingTalk; you become its admin), copy the **Client ID** and
  **Client Secret**, add the **Robot** capability with **Stream mode** as the message receiving mode, and publish the
  robot and the app. Free DingTalk organizations have a monthly cap on API calls.
- **QQ.** Sign in to the [QQ open platform](https://q.qq.com/qqbot/) with your QQ account and create a bot (personal
  accounts can have up to 5). Copy the **AppID** and **AppSecret** from its development settings, and choose WebSocket
  for events. As the bot's admin you can chat with it right away, without publishing it or using the sandbox.

After that, just message your bot. Photos and files go through the same checks as attachments in the page (Word, Excel
and PowerPoint become text; voice messages and videos aren't supported yet). In the chat (on Slack, write
`/zhifox new` and so on):

| Command | What it does |
|---|---|
| `/new` | Start a new conversation (the previous one stays in the sidebar). `/new <agent name>` starts it with that custom agent as the persona |
| `/stop` | Stop the turn in progress, including a running command, and drop queued messages |
| `/status` | Model, approval mode, current conversation, what it's doing now, today's and this month's spending |
| `/help` | How to use it |

The connected channel's row shows its status (online, connecting, error with the reason), the bound account, the last
time someone talked to it, and what each chat is doing (with a Stop button). Like every row of things stored on your
computer, it has **Show in Finder** (`channels.json`), **Edit** and **Delete** (confirmed in place), plus an on/off
switch and **Pair again** (pair a different account; the old one is no longer served). If a platform's SDK isn't installed,
its card and row say so, with the exact command to run. The platforms still to come are listed with what they'll
need: WhatsApp and WeCom can only push messages to a public URL, so they come later; personal WeChat has no official
bot API (the workarounds log in as you and break WeChat's terms, which can get the account banned), so it isn't
supported.

Channels run while ZhiFox runs as a long-lived program: the web UI (`harness --web`) or the server mode
(`harness serve`, below). The terminal UI doesn't run them.

### Browser use

Browser use is off by default. Open **Settings → Browser** and switch on **Let the assistant use a browser**. The
section also has:

- **Hide the browser window.** Off by default, so you can watch the assistant work in its own Chrome window. It is
  always on under `harness serve` and on Linux without a display.
- **Chrome.** The browser ZhiFox found (Google Chrome first, then Chromium, Edge or Brave). To use another, enter its
  full path and click **Save path**, or set `HARNESS_CHROME` in `.env`. A path that doesn't work is reported, not
  quietly replaced. If no Chrome is found, the section says so.
- **Profile folder** (`data/browser/profile`), with **Show in Finder** and **Clear** on hover. Clearing signs the
  browser out of every site and deletes its cache and history, after a confirm in place (and not while a step is
  running).
- A note on what it costs (see [Cost](#cost)).

The switch takes effect from the next conversation (a conversation that hasn't started yet picks it up right away).
Then ask for something like "Open the Python docs and tell me what's new in 3.13". While the assistant works:

- The top bar shows **ZhiFox is controlling the browser** (or **ZhiFox's browser is open** between steps) with a
  **Stop** button, which aborts every browser action in progress and closes the browser.
- Each browser step's tool card carries a small screenshot; click it for the full-size image. Full-size images are
  kept for 30 days in `data/browser/shots/`; after that the small one stays in the conversation.
- Approval cards say which site it wants to open, which field it will type into and what (a password only as dots),
  or which button may submit something (see [Permissions](#permissions)).

If a job needs an account, you can sign in yourself in ZhiFox's browser window (with **Hide the browser window** off);
the login stays in the profile until you clear it. Think before you do: from then on, a model misled by a page could
act as you on that site, within the approval rules. Custom agents get the browser with `tools: Browser`. The terminal
UI follows the same setting and asks its approvals in the terminal; it can't draw screenshots, so it only notes that
one was saved.

### Terminal UI

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
| `/new` | Start a new conversation (memory and to-dos are kept) |
| `/todos` | Show open to-dos |
| `/tools` | Show the tools the model can use |
| `/usage` | Show this run's token usage and estimated cost, plus what today and this month cost (and your monthly budget, if you set one) |
| `/model` | Show the model in use (switch models in the web UI's Settings) |
| `/mcp` | Show the connected MCP servers and their status (add and manage them in the web UI) |
| `/auto` | Show or change when the assistant asks first: `/auto ask`, `/auto edits` or `/auto all` (see [Permissions](#permissions)) |
| `/exit` | Quit (`/quit` and Ctrl-D also work) |

Press Ctrl-C while the model is replying to interrupt it.

The terminal UI doesn't run scheduled tasks or chat channels. You can ask the assistant to add or delete tasks here too;
they run while the web UI or `harness serve` is running.

### Using it in another folder

The assistant's working directory is the directory you start it from. To have it work somewhere else, `cd` there first,
then run:

```bash
uv run --project "/full/path/to/this/project" harness
```

The keys and data still come from the project's own `.env` and `data/`.

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
uv run harness channels approval telegram ask    # ask | edits | all (ask is the default)
uv run harness channels status
```

The same commands take `discord`, `slack`, `feishu`, `dingtalk` or `qq`. `setup` prints that platform's steps and asks
for each field (secrets aren't echoed); it also reads them from the environment (`DISCORD_BOT_TOKEN`,
`SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`, `FEISHU_APP_ID` and `FEISHU_APP_SECRET`, `DINGTALK_CLIENT_ID` and
`DINGTALK_CLIENT_SECRET`, `QQ_APP_ID` and `QQ_APP_SECRET`) or from standard input, one line per field. Feishu and
DingTalk need their SDK: `uv sync --extra feishu --extra dingtalk` (and pass the same `--extra` flags to later
`uv sync` runs, which otherwise remove them).

Then run it as a service. A systemd unit, `/etc/systemd/system/zhifox.service`:

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
approval shows the full command and the working folder before you tap Allow; **Allow everything** comes with a
warning and is a deliberate choice per channel; one turn at a time per chat, and `/stop` ends it, command included;
the bot tokens and app secrets live only in `channels.json` (mode 600), are never sent to the browser or the model,
are masked in exports and hidden in logs and error messages, and the assistant's file tool can't open that file in any
mode; and shell commands don't see `TELEGRAM_BOT_TOKEN`, the other platforms' secret variables or the API keys in their
environment. Your chat account becomes a key to the server, so protect it (two-step verification) and use a dedicated,
unprivileged user for the service.

## Data and privacy

| Path | Contents |
|---|---|
| `.env` | Your API keys. When you save a key from the web UI, the file is made readable by your account only |
| `data/conversations/` | Every web UI conversation, one JSON file each |
| `data/media/` | The original of every attachment you sent, named by the SHA-256 of its content, so a file sent ten times is stored once. Originals that no conversation uses any more are removed when you delete a conversation, and at startup |
| `data/memories/` | The assistant's long-term memory and notes. You can read, edit and delete them in the web UI's info panel, or edit them by hand |
| `data/skills/` | Installed skills, one folder each. You can manage them under **Extensions → Skill** or by hand |
| `data/skills.json` | Which skills are disabled and where each one came from |
| `data/agents/` | Your custom agents, one Markdown file each (the Claude Code subagent format). You can manage them under **Extensions → Agent** or by hand |
| `data/todos.json` | To-dos |
| `data/tasks.json` | Scheduled tasks with the last 20 runs of each, and reminders you haven't dismissed yet. If it can't be read, it is kept as `tasks.broken.json` and ZhiFox starts with an empty list |
| `data/settings.json` | The model you chose, the synced model lists, prices you entered, the interface language and your monthly budget. No keys |
| `data/ledger.jsonl` | The spending record: one line per model call (date, provider, model, chat, scheduled task or subagent, tokens, estimated cost). Entries older than 90 days are merged into one line per day, model and source |
| `data/mcp.json` | The MCP servers you added, in Claude Desktop's format, plus ZhiFox's own fields (`enabled`, `approval`, `timeout`). May contain the servers' secrets, so it is readable by your account only, and the assistant's file tool can't open it |
| `data/channels.json` | Chat channels, per platform: the credentials (bot token, or app ID and secret; the secrets are secrets), the bot's name, the paired owner's account id and name, the approval mode, an active pairing code, which conversation each chat continues, and how far Telegram's messages have been read. Readable by your account only; the assistant's file tool can't open it; masked in exports |
| `data/browser/profile/` | The profile of ZhiFox's own Chrome: cookies and logins of any site you signed in to there, its cache and history. Readable by your account only. **Settings → Browser → Clear** deletes it |
| `data/browser/shots/` | Full-size screenshots of browser steps, named by the SHA-256 of their content. Deleted after 30 days; the small previews stay in the conversation |
| `data/backups/` | The previous version of any file the assistant overwrote completely |
| `data/input_history` | What you typed in the terminal UI (press the up arrow to bring it back) |

These are all ordinary files you can open. Both `data/` and `.env` are listed in `.gitignore`, so they never get
committed to git. The folders and conversation files in `data/` are readable and writable by your account only, so other
accounts on the same computer can't see them. After you save a key in the web UI, only its last 4 characters are shown;
the full key is never sent back to the browser.

Conversation content is sent to the API of the provider you chose, so the model can process it, and saved in
`data/conversations/` on your own computer. It is not uploaded anywhere else. Deleting a conversation in the sidebar
deletes its file too. With the browser on, the pages the assistant opens are fetched from those sites by ZhiFox's own
Chrome, and what it reads there goes to the model provider like any other tool result.

To look at a conversation's file directly, click **Show in Finder** on its row in the sidebar. The **Show in Finder**
buttons (on conversations, memories, tasks, to-dos, the prompt, MCP servers, skills and media items, plus the memory,
skill and browser profile folder buttons) are the only ones on the page that make your computer launch another program (the file manager),
so the server is extra careful with them: the page can only tell the server *which conversation*, *which memory* (a name
from the list, checked as described below), *which skill* (a single folder name that must exist inside `data/skills/`),
*which attachment* (an id of the form conversation/message/position, checked against the index; the file name comes from
the content's SHA-256), which of the assistant's own data files (`tasks`, `todos` or `prompt`), or, for the memory and
skill folders, `data/mcp.json` and the browser's profile folder, nothing at all; the server works out the path itself,
and the page has no way to make it open any other path. Downloading an attachment works the same way: the page sends
only the id, and the file comes back as a download (`Content-Disposition: attachment`,
`X-Content-Type-Options: nosniff`), so a text file that contains HTML is still plain text. A full-size browser screenshot is fetched the same
way: the page sends only its id (64 hexadecimal characters, which can't spell a path), and the answer is always
`image/jpeg` with the same headers plus `Content-Security-Policy: default-src 'none'; sandbox`. Linux has no standard
way to select a file in the file manager, so there it opens the folder containing the file.

Saving a local MCP server means ZhiFox will run that command on your computer, now and at every launch. The safeguards:
only the page holding this run's token can save one; the command is started from an argument list, never through a
shell, so nothing in an argument is treated as part of a command; the model has no tool that adds or changes servers,
and its file tool can't touch `data/mcp.json` in any mode; the programs don't receive your model API keys, and a
configuration that tries to pass one on (`${ANTHROPIC_API_KEY}`) is refused; the values of environment variables and
headers never reach the browser, only their last 4 characters; and Claude's configuration files are read only when you
open the import page, and only the servers you tick are copied. Arguments of an MCP tool call go to that server, which
may be on the internet, so check them on the approval card.

Reading, editing and deleting a memory from the info panel are guarded the same way. The page sends back the name it was shown
(such as `projects/zhifox.md`), and the server walks from the memory folder down to that file one step at a time. It
refuses anything that would end up outside the folder: `..`, absolute paths, and symlinks that point elsewhere. It only
deletes plain files; a subfolder left empty by the deletion is removed too. A new name given while editing is a single
name (no `/`, not starting with a dot), so a memory can be renamed but never moved out of its folder, and the new text
is written to a temporary file first and swapped in, so a failed save never leaves half a file.

### Backup and restore

**Settings → Export all data** packs the data folder into `zhifox-backup-<date>-<time>.zip` and downloads it:
conversations, memories, to-dos, settings, the spending record, and anything else later features keep there. API keys
are never in it: they live in `.env`, which isn't in the data folder, and any `.env`-like file found there anyway is
skipped. `data/channels.json` is exported with every bot token and app secret, and any pairing code, replaced by `<removed-from-backup>` (app IDs are kept). If `data/mcp.json` exists, it is exported with every secret replaced the same way (environment
variables, headers, fields named like a key, token or password, the value after flags such as `--token`, passwords in
URLs, and strings shaped like an API key); the names are kept so you know what to fill in again. If that file isn't
valid JSON, it is left out entirely rather than risk exporting a secret. Symbolic links are skipped too, since they
could point anywhere on your computer. A `README-backup.txt` inside the zip lists the date, how to restore, and what was
masked or skipped. The zip is built in a temporary file outside the data folder and deleted once it has been sent.

To restore:

1. Quit ZhiFox (Ctrl-C in its terminal).
2. Unzip the archive into the data folder (by default the project's `data/`), replacing files with the same name:
   `unzip -o zhifox-backup-….zip -d data` (Finder's Archive Utility works too: copy the contents of the unzipped folder
   into `data/`). To get back exactly to the backup, move the current `data/` aside first and unzip into a new, empty
   `data/`.
3. Start ZhiFox again. If the backup had MCP servers, fill their secrets in again; if it had chat channels, paste their
   tokens or secrets again (**Extensions → Channels → Edit**, or `harness channels setup <platform>`).

Importing from the web UI isn't supported yet.

## Cost

APIs bill by the token (a Chinese character is roughly 1 to 2 tokens). Because every request resends the whole
conversation history, each step costs more the longer the conversation and the more steps it has taken. A task where
the assistant researches something on its own can take dozens of steps and cost much more than ordinary chat.

What the program does to keep cost under control:

- **Estimated cost.** The web UI's info panel and the terminal's `/usage` convert usage into US dollars. It's only an
  estimate; each provider's bill is what counts, and bills usually lag.
- **Spending record.** Every model call, from a chat, a scheduled task or a subagent, is written to `data/ledger.jsonl` with its
  price at that moment (DeepSeek's peak or off-peak rate included), so today's and this month's totals survive
  restarts. Changing a price later doesn't rewrite old entries; calls to a model without a price keep their tokens and
  count as $0 until it has one. Each call is appended as one line, so a crash can at most leave half a line, which is
  skipped when reading.
- **Monthly budget.** Optional, in Settings. Passing it shows one reminder and never stops anything, including
  scheduled tasks.
- **Search caps.** For each message you send, Claude can search the web at most 5 times and fetch at most 5 pages; past
  that, it answers with what it has. The API enforces this cap. Each web search costs $0.01 on top of tokens.
- **Caching.** Parts that are sent again and again cost far less once they hit the cache (for Claude, about a twentieth
  of the normal input price). The cache lasts only a few minutes, so when you come back to a long old conversation after
  a while, the first message is billed at the full price for the whole history. Opening an old conversation of more than
  30,000 tokens shows a notice about this.
- **Automatic compaction.** In very long conversations, earlier content is condensed into a summary, so every later
  turn sends far fewer tokens. Claude's API does this itself; for GPT, Gemini and DeepSeek, ZhiFox asks the same model
  to write the summary, and that call is included in the cost estimate (see
  [When a conversation gets too long](#when-a-conversation-gets-too-long-compaction)).
- **Attachments.** Images are billed by size: every 28×28-pixel patch is one token, so a full-size screenshot costs at
  most about 4,800 tokens (about $0.02 on Opus 5.5). A PDF page costs roughly 1,500–3,000 tokens; a Word, Excel or
  PowerPoint file costs what its text costs. Like the rest of the
  history, attachments are resent on every turn, and caching is what keeps that cheap.
- **Browser.** With Claude, the browser toolset adds about 6,600 input tokens to every request of a conversation
  started with the browser on. It sits in the cached start of the request, so after the first request it costs about
  a twentieth of that; still, it's why the browser is off by default and its tools are only in conversations that
  began with it on. Reading a page costs what its text costs (at most 50,000 characters per read). A screenshot
  (1280×800, JPEG) is about 1,400 tokens and goes to the model only when it asks for one; the small previews on tool
  cards are never sent.
- **Subagents.** Handing a task to a subagent costs its own model calls (at most 25 per task), recorded as source
  "subagent" and shown on its card. In exchange, the pages it reads stay in its history, not in the main conversation,
  so the main conversation's later turns don't keep paying for them.
- **Scheduled tasks.** Each run of an agent task costs about as much as sending its instruction as a message, and it
  runs whether or not you're there. There is no spending cap: a task every 5 minutes is 288 runs a day. The estimated
  cost of every run is recorded with the task. Reminders are free.
- You can click **Stop** at any time, or start a **New conversation** to clear the history.

Built-in prices, checked against the official pricing pages on 2026-11-20 (USD per million tokens):

| Model | Input | Cache write | Cache hit | Output |
|---|---|---|---|---|
| `claude-opus-5-5` | 4 | 5 | 0.2 | 20 |
| `claude-sonnet-5-5` | 2 | 2.5 | 0.1 | 10 |
| `claude-fable-5-2` | 8 | 10 | 0.2 | 40 |
| `deepseek-flash` (peak / off-peak) | 0.30 / 0.15 | none | 0.006 / 0.003 | 1.20 / 0.60 |
| `deepseek-v4-pro` (peak / off-peak) | 1.32 / 0.66 | none | 0.044 / 0.022 | 3.96 / 1.98 |

Claude's cache write price is for the 5-minute cache, which is the one ZhiFox uses; web search is another $10 per 1,000
searches. DeepSeek charges by time of day: peak hours are weekdays 9:00–12:00 and 14:00–18:00 Beijing time, and every
other hour, weekends included, costs half. ZhiFox checks the time on every model call and records off-peak usage
separately, so a conversation that runs across the boundary is priced correctly. DeepSeek also treats Chinese public
holidays as off-peak; those dates aren't built in, so a weekday holiday is estimated at the peak price (a little high,
never low). The old name `deepseek-v4-flash` is billed as `deepseek-flash`.

For other models, enter prices yourself in Settings (a price you enter applies all day). Models without a price show token counts only, and the page notes
that they aren't included in the estimate.

## Configuration

Set these in `.env`. All of them are optional:

| Variable | Default | Meaning |
|---|---|---|
| `HARNESS_MAX_SEARCHES` | `5` | Maximum web searches per message; `0` means no limit |
| `HARNESS_WEB` | `1` | Set to `0` to turn off web search completely |
| `HARNESS_EFFORT` | `medium` | How hard Claude thinks: `low` / `medium` / `high` / `xhigh` / `max` |
| `HARNESS_DATA_DIR` | The project's `data/` | Where memory, to-dos, settings and other data are stored |
| `HARNESS_PROVIDER`, `HARNESS_MODEL` | `anthropic`, `claude-opus-5-5` | Which model to use before you've chosen one in the interface. Once you have, your choice takes precedence |
| `HARNESS_CHROME` | Found automatically | Full path of the Chrome (or Chromium, Edge, Brave) to use for browser use. A path entered in **Settings → Browser** takes precedence |

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

Then import it in [`harness/tools/__init__.py`](harness/tools/__init__.py) and add a line `make_dice_tool(),` to the list
in `build_registry`. Restart, and ask the assistant to roll a die for you. Tools created with `function_tool` work with
all four providers' models. (The built-in tools write their specs in Chinese; yours can be in any language the model
reads.)

A few notes:

- The `description` is the only thing the model has to decide whether to call the tool. Besides what it does, say
  **when it should be used**.
- What a tool returns is text for the model to read. On failure, raise `ToolError("reason")` (also importable from
  `.base`); the model sees the reason and adjusts.
- If the new tool changes anything on your computer, add a rule for it in `review` in
  [harness/permissions.py](harness/permissions.py) so that it asks you first.

## Testing

```bash
uv run pytest
```

The tests need no API key and cost nothing. [tests/fake_api.py](tests/fake_api.py) and
[tests/fake_openai.py](tests/fake_openai.py) are two local fake servers that replay prepared replies in Claude's and
OpenAI's formats. That lets the real SDKs run the whole loop, and the tests check that the requests sent out are correct.
To see how the loop behaves in different situations (a tool fails, you deny a step, you interrupt, the model refuses),
read [tests/test_agent.py](tests/test_agent.py). The web UI tests are in [tests/test_webui.py](tests/test_webui.py),
where the test code plays the browser. Saving conversations and searching them are covered in
[tests/test_conversations.py](tests/test_conversations.py) and [tests/test_search.py](tests/test_search.py), and
compaction for GPT, Gemini and DeepSeek in [tests/test_compaction.py](tests/test_compaction.py); the spending record and
budget in [tests/test_ledger.py](tests/test_ledger.py) (DeepSeek's peak and off-peak prices with a fixed clock, a
half-written last line, merging old entries), the export in [tests/test_export.py](tests/test_export.py) (no secret
ends up in the zip), and their web endpoints in [tests/test_spending_web.py](tests/test_spending_web.py). Scheduled
tasks are covered in [tests/test_tasks.py](tests/test_tasks.py) (schedules, including month ends and daylight saving;
storage; tools and their permissions) and [tests/test_task_runs.py](tests/test_task_runs.py) (runs through the real
server with a fake clock, so no test waits for 9 a.m.: a run beside a chat turn, two runs side by side, the queue past
the limit, Stop reaching only its own run, and the three approval modes; the fake API routes each run's requests by
its instruction, so parallel runs stay deterministic).
MCP is tested against [tests/fake_mcp.py](tests/fake_mcp.py), a fake MCP server that speaks either protocol generation
over stdio or HTTP, in [tests/test_mcp.py](tests/test_mcp.py) and [tests/test_mcp_web.py](tests/test_mcp_web.py).
The media library is covered in
[tests/test_media.py](tests/test_media.py) (storing and deduplicating originals, recovering attachments from older
histories in both Claude's and OpenAI's formats, filtering and paging, ids that can't name a path, cleaning up originals
only when no conversation uses them) and in the media section of `tests/test_webui.py` (the endpoints and their headers).
Chat channels are tested against a local fake of each platform, while the test code plays your phone:
[tests/fake_telegram.py](tests/fake_telegram.py) (a Bot API that checks the HTML and the length of every message the
way Telegram does), [tests/fake_discord.py](tests/fake_discord.py) and [tests/fake_qq.py](tests/fake_qq.py) (REST plus a
gateway that can ask to reconnect, invalidate the session, stop acknowledging heartbeats or close with any code),
[tests/fake_slack.py](tests/fake_slack.py) (the Web API plus Socket Mode, re-sending envelopes that weren't acked), and
[tests/fake_feishu.py](tests/fake_feishu.py) / [tests/fake_dingtalk.py](tests/fake_dingtalk.py) (the HTTP APIs plus a
fake listener in place of the SDK). One rig ([tests/channel_rig.py](tests/channel_rig.py)) runs the whole program
against any of them: pairing (right, wrong, expired codes, strangers and groups ignored), duplicate deliveries,
splitting long replies, photos and documents, approvals answered by button, by text and from the web (racing each
other), per-channel approval modes, `/new` `/stop` `/status`, reconnecting and resuming, rate limits, and that no
token or secret appears in the page, the logs, error messages or the export ([tests/test_channels.py](tests/test_channels.py),
[tests/test_channels_web.py](tests/test_channels_web.py), `tests/test_channels_<platform>.py`,
[tests/test_channels_platforms.py](tests/test_channels_platforms.py)). When the SDKs are installed, the Feishu
dispatcher and the real DingTalk Stream client are also run against the fakes; otherwise those tests are skipped.
The WebSocket client and the HTTPS helper have their own tests against a local fake server and proxy
([tests/test_websocket.py](tests/test_websocket.py), [tests/test_net_http.py](tests/test_net_http.py)). The server mode and the CLI, including a real `harness serve`
process stopped with SIGTERM, are in [tests/test_serve.py](tests/test_serve.py).
Skills (the frontmatter parser, hostile archives, downloads against a local HTTPS server, how the list reaches the model
without changing the start of the request) are in [tests/test_skills.py](tests/test_skills.py).
Custom agents are in [tests/test_agents.py](tests/test_agents.py) (the file format and its checks, the tool allow-list
that never grows, storage and Claude Code import under a fake home folder, prompts, the `<agents>` catalog, delegation
against the fake API: a fresh history, the allowed tools, model overrides, approvals, Stop reaching the subagent's own
shell and MCP calls, step and depth limits, spending recorded as `subagent`, the main request's prefix staying
byte-identical; and personas saved and restored) and [tests/test_agents_web.py](tests/test_agents_web.py) (the
`/api/agent/…` endpoints, starting a conversation with an agent, and the subagent events, confirmations and Stop in
the web UI).
Browser use is tested in layers. The CDP client runs against in-process pipes, with a thread playing Chrome's end so a
test can split a message into pieces or send an event before a reply ([tests/test_browser_cdp.py](tests/test_browser_cdp.py)).
Launching (descriptors 3 and 4, no API keys in Chrome's environment, its own process group), closing the whole group,
idle closing with a fake clock and the session's basics run against [tests/fake_chrome.py](tests/fake_chrome.py), a fake
Chrome started through the real `chrome.launch()` that answers the pipe protocol from a script
([tests/test_browser_chrome.py](tests/test_browser_chrome.py), [tests/test_browser_unit.py](tests/test_browser_unit.py),
which also tries many ways of sneaking a `javascript:` address past the address rules). The permission rules are in
[tests/test_browser_permissions.py](tests/test_browser_permissions.py). The tool specs and results go through the loop
against the fake APIs in [tests/test_browser_tools.py](tests/test_browser_tools.py): one toolset entry and no stand-ins
for Claude (the reverse for the others), `toolset_name` and `browser_state` on results, the batch halt rule, and a
request start that stays byte-identical across turns. The Settings endpoints, the header indicator and the screenshot
endpoint's headers are in [tests/test_browser_web.py](tests/test_browser_web.py). Finally
[tests/test_browser_live.py](tests/test_browser_live.py) runs a real headless Chrome against a page served by
`http.server` on your machine (reading, clicking, typing, recognizing password fields and submit buttons, screenshots,
tabs); it is skipped when no Chrome is installed.

The Markdown renderer has its own tests in [tests/markdown.test.mjs](tests/markdown.test.mjs), run by Node's built-in
test runner (`node --test tests/markdown.test.mjs`, nothing to install); `uv run pytest` runs them too when Node is
available. They use a tiny fake document whose `innerHTML` and similar HTML-parsing APIs throw, and which only allows
a fixed list of tags and attributes. They cover every supported syntax, nesting, escaping, malicious input, streaming
in random-sized pieces (the result must equal rendering all at once), and 10,000-line inputs.

The rest of the page (the styles and scripts in `index.html`) has no automated tests. After every change it is checked
by hand in a real browser against the fake servers.

To click around and look at the interface without spending anything, run `uv run python tests/demo_web.py`. It starts
the real server and page backed by a local fake model with prepared replies, keeps its data in a temporary directory, and
doesn't touch your keys or `data/`. It also comes with a few fake MCP servers already connected (one of them broken,
one disabled) and a few made-up skills, and its **Import from Claude** (MCP) and **Import from Claude Code** (skills)
read made-up Claude configurations and a fake `~/.claude/skills` from the temporary directory, not yours.
A fresh demo directory also starts with an older conversation whose attachments predate the media library, so the
Media tab has something to show, including the "text only" Word file. The demo also starts a fake of every chat
platform: it prints a made-up token (or ID and secret) for each one to paste into the **Channels** wizard, and the
addresses of two bare-bones "phone" pages (one for Telegram, one with a platform picker for the others) where you can
send the pairing code, chat, and tap Allow / Deny. `--no-sdk` shows the page as if the Feishu and DingTalk SDKs
weren't installed. The browser is switched on in the demo: start a new conversation
with a message containing 演示商店 ("demo shop"), for example 用浏览器看看演示商店里有什么, and the assistant opens a small
shop page the demo serves on your machine in a real Chrome, asks before visiting it, reads it, clicks the search box,
types (asking again) and takes a screenshot. Add `--browser-window` to watch the Chrome window; without Chrome on the
computer, the assistant says it can't find one.

Every push to GitHub and every pull request runs the tests and the linter (ruff) on Ubuntu and macOS with Python 3.11
and 3.13 ([.github/workflows/tests.yml](.github/workflows/tests.yml)). The result is the Tests badge at the top of this
page.

## Limitations

- macOS and Linux only (running commands depends on bash).
- GPT and Gemini have not been tested with real keys (see [Supported models](#supported-models)).
- With GPT, Gemini and DeepSeek there is no web search.
- Compaction for GPT, Gemini and DeepSeek is done by ZhiFox itself and hasn't been tested with real keys. A summary
  inevitably loses detail. The cut can only fall between turns, so a single turn that by itself exceeds the model's
  limit (say, one that reads many large files) can't be compacted. Models not listed in `config.py` are all assumed
  to have a 128K-token window, so models with a larger window are compacted earlier than they need to be. After you
  reopen an old conversation, the first request has to measure its size before compaction can kick in.
- Attachments work in the web UI only, not in the terminal. Images with GPT and Gemini, and PDFs with GPT, have not
  been tested with real keys. Gemini and DeepSeek can't read PDFs.
- GPT goes through the Chat Completions API. A new OpenAI model that only supports OpenAI's other API won't work here.
- Only the three Claude models the program has been adapted to are listed; other Anthropic models accept slightly
  different parameters.
- Each command runs in a fresh shell, so `cd` and environment variables don't carry over to the next command.
- Conversations are saved only in the web UI. Terminal conversations are gone when you close the program.
- Search doesn't support pinyin (typing `baogao` won't find "报告"). It keeps the text of every conversation in memory,
  so a very large number of conversations (thousands of long ones) takes more memory. The conversation in progress isn't
  searchable until it has been saved.
- One conversation at a time: while one is replying, you can't switch to another, rename one or delete one.
- The web UI renders a subset of Markdown: CommonMark plus GitHub's tables, task lists and strikethrough. Images
  aren't loaded (you see their description and address), raw HTML is shown as text, links work only for http, https
  and mailto, and indented code blocks, reference-style links and footnotes aren't supported. Code coloring knows
  only comments, strings, numbers and keywords of common languages. The terminal shows the Markdown source as is.
- Scheduled tasks and chat channels run only while the web UI or `harness serve` is running; tasks at most three at once.
  Chat channels take text, photos and files (no voice or video yet); WhatsApp and WeCom aren't supported yet because
  they need a public URL. Discord, Slack, Feishu, DingTalk and QQ were built from their official documentation and
  are tested only against local fakes so far, not yet with real bots. Two runs (or a run and your
  conversation) editing the same lines of the same file don't merge their changes: each edit is applied whole, and the
  later one fails if the text it expected has changed, so the model reads the file again. Reminders appear only in the
  page; there are no system notifications.
- With the interface in English or Spanish, messages returned by tools (such as "没有编号为 9 的待办", "there is no to-do
  #9") and the tool descriptions in the terminal's `/tools` stay in Chinese, because they are written for the model.
  Conversations saved before you switched language keep the program's notices in the language used at the time.
- Skills: `allowed-tools` doesn't pre-approve anything (see [Skills](#skills)). Claude Code's argument placeholders
  (`$ARGUMENTS`, `$0`), `!` command blocks, `context: fork`, `hooks` and other Claude Code-only fields are kept in the
  file but ignored. Installing from a URL works only for public https links up to 20 MB, so for a private GitHub
  repository, download it and use Upload.
- MCP: only tools are supported, not MCP resources or prompts. OAuth sign-in for remote servers isn't supported; servers
  that accept a token work with an `Authorization: Bearer …` header. The deprecated HTTP+SSE transport isn't supported.
  A tool that needs to ask you something mid-call (elicitation) fails with an explanation, and ZhiFox never lets a
  server use its model (sampling). Images returned by MCP tools go to the model but aren't shown in the page.
  The instructions a server writes for the model are shown to you but not given to the model. A server that connects
  or changes during a conversation is used from the next one. Tested against fake servers only, not real third-party ones.
- Browser use works with Chromium-family browsers only (Chrome, Chromium, Edge, Brave). Nothing goes to or from your
  disk through it: downloads are refused, file pickers are suppressed, and the toolset's optional members (running
  JavaScript, uploading files) are left off. What's inside a frame from another site can't be read as text, only seen
  in a screenshot and clicked by coordinates. Page dialogs are dismissed, not answered. Claude's browser toolset has
  only been exercised against the local fake API, with no real Claude request yet, and the browser tools for GPT,
  Gemini and DeepSeek haven't been tried with real keys; the Chrome side is tested against a real headless Chrome.
- **Stop** in the web UI takes effect the next time the model produces output: during the few seconds when the model is
  still thinking and has produced nothing, it won't stop immediately.
- Three Claude features use beta API features: automatic retry with another model after a refusal, automatic history
  compaction, and dropping mismatched thinking when you continue an old conversation. The third is included in every
  Claude request. If every request fails with a 400 error, you can turn them off in [harness/config.py](harness/config.py)
  (one switch each).

## Roadmap

- Let the terminal UI save and open conversations too.
- Add a "don't ask again this session" option for common read-only commands (`ls`, `git status`).
- Add a locally run search tool for models that don't support web search.
- MCP resources and prompts, and OAuth sign-in for remote MCP servers.
- Computer use of the whole desktop (screenshots of and control over your entire screen), as an option beside the
  browser.
- Add pinyin matching to search.
- Add another interface language: write a file in `harness/locales/` modeled on `en.json`, then add it to `LANGUAGES` in
  `harness/i18n.py`.

## FAQ

### How is ZhiFox different from LangChain, LangGraph and other agent frameworks?

ZhiFox is not a framework or a library to build on. It is a small, complete assistant whose whole harness you can read.
Its only runtime dependencies are the official `anthropic` and `openai` SDKs and `prompt-toolkit` for the terminal; the
agent loop, tool dispatch, permission checks, provider adapters, storage and search are written by hand, one concern per
file. Use it to learn how an LLM agent works, or as a starting point you understand completely.

### Does my data leave my computer?

Only what the model needs to answer. Your messages, the conversation history and tool results (for example, the contents
of a file the assistant reads) go to the API of the provider you chose; with Claude, web searches also run on Anthropic's
servers. If you connect MCP servers, the arguments of each MCP tool call you approve go to that server. With browser
use on, the sites the assistant opens see an ordinary visit from ZhiFox's own Chrome, and what it reads there goes to
the provider. Saved
conversations, memory, to-dos, settings and keys stay in plain files on your disk and are not uploaded anywhere else.
The web UI's server accepts connections from your own machine only. See
[Data and privacy](#data-and-privacy).

### Which models does it support?

Claude (`claude-opus-5-5`, `claude-sonnet-5-5`, `claude-fable-5-2`) through Anthropic's native API, and GPT, Gemini and
DeepSeek through the OpenAI-compatible Chat Completions API, using the model lists you sync in Settings. Only Claude has
been tested in real chats so far. Web search is Claude-only; long conversations are compacted with every provider (by
Anthropic's API for Claude, by ZhiFox itself for the others). Any other provider that speaks the OpenAI format can be
added with one entry in `PROVIDERS`.

### Can it run commands without asking me?

Not by default. Every shell command and every file write waits for your approval. You can loosen this for the current
session with `/auto edits` or `/auto all` in the terminal, or the **Ask every time** button in the web UI; it resets on
every launch, and steps that ran without asking are marked as allowed automatically. Scheduled tasks have their own
setting, chosen per task in the task editor (Ask every time by default; tasks the assistant creates always ask). In
every mode the assistant can't touch `.env`, and the commands it runs can't see your API keys. See
[Permissions](#permissions).

### Can it do things on a schedule?

Yes, while the web UI or `harness serve` is running. Ask for "remind me at 3 p.m. to call the bank" (a free reminder) or "every Monday at
9, summarize my open to-dos" (the assistant does it, and each run costs API credits). Runs happen alongside your
conversation, pause for your approval before commands and file writes (unless you loosen that for the task), and are
caught up once if ZhiFox was closed. See
[Scheduled and recurring tasks](#scheduled-and-recurring-tasks).

### What does it cost to run?

ZhiFox itself is free and MIT-licensed. You pay each provider for API usage by the token, separately from any chat
subscription. Cost grows with conversation length and the number of steps, because each request resends the history.
The web UI and `/usage` show an estimated cost as you go; Claude's web search adds $0.01 per search. The tests and the
demo cost nothing. See [Cost](#cost).

### Can I use it from my phone?

Yes, through your own bot on Telegram, Discord, Slack, Feishu, DingTalk or QQ (see [Chat channels](#chat-channels)). Only the account you pair can use it,
and anything that would change your computer still asks you first, in the chat. On a server, run `harness serve` (see
[Deploying to a server](#deploying-to-a-server)).

### Does it run on Windows?

No. ZhiFox supports macOS and Linux only, because running commands depends on bash.

## Keywords

AI agent · agent harness · AI agent from scratch · AI agent without a framework · personal AI assistant · local-first AI
assistant · Claude API · Anthropic SDK · OpenAI-compatible API · GPT · Gemini · DeepSeek · tool use · function calling ·
prompt caching · human-in-the-loop approval · agent loop · browser use · Chrome DevTools Protocol · Python · terminal
chat · web UI

## License

[MIT](LICENSE). You can use, modify and distribute it freely, as long as you keep the copyright notice.

## Star History

<a href="https://www.star-history.com/#Akxan/zhifox-agent-harness&Date">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=Akxan/zhifox-agent-harness&type=Date&theme=dark" />
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=Akxan/zhifox-agent-harness&type=Date" />
    <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=Akxan/zhifox-agent-harness&type=Date" />
  </picture>
</a>
