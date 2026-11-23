**English** | [简体中文](../zh-CN/limitations.md)

[ZhiFox](../../README.md) › [Documentation](../../README.md#documentation) › Limitations, roadmap and FAQ

# Limitations, roadmap and FAQ

## Limitations

- On Windows: no computer use yet, read-only commands are not auto-allowed (every command asks), and signing out or
  shutting down ends ZhiFox without a clean stop (see [Windows](windows.md)).
- The background service (`harness service`) is tested against a fake `launchctl` and `systemctl` only; the tests never
  install a real one. On macOS, a project inside Documents, Desktop or Downloads may need a privacy permission first.
- GPT and Gemini have not been tested with real keys (see [Supported models](models.md#supported-models)).
- Voice to text has only been tested against a local fake of OpenAI's transcription endpoint, never the real API; in
  particular, OpenAI's docs list the OGG container but don't mention the Opus codec Telegram voice notes use.
- The evals (`harness eval`) have been run for real once on each of Claude Sonnet 5.5, Claude Opus 5.5 and DeepSeek
  Flash (see [Evals](evals.md)); one trial per task is too few for fine comparisons,
  and GPT and Gemini haven't been run. The served model
  isn't checked against the one asked for (a server-side fallback after a refusal shows up only as a note in the trace).
- With GPT, Gemini and DeepSeek there is no web search. They can read a page whose address they already have
  (`web_fetch`), but fetching is not searching. It doesn't run JavaScript, so pages that build their content with
  scripts come out empty or partial (use the browser for those), it reads no PDFs, and it doesn't use a proxy, so a
  site you can only reach through `HTTPS_PROXY` can't be read. It has only been tested against local fake sites, not
  real ones (TLS included).
- Custom model endpoints have been tested only against local fake servers that imitate Ollama, LM Studio and partial
  OpenAI-format servers, not a real Ollama, LM Studio, vLLM, llama.cpp or gateway. A server that doesn't report usage
  while streaming shows no token counts and its conversations aren't compacted. PDFs reach a custom endpoint only in
  OpenAI's own format, which local servers generally don't read.
- Compaction for GPT, Gemini and DeepSeek is done by ZhiFox itself; it has been tested with a real key only on
  DeepSeek (a lowered threshold, two compactions in a row, and the model still knew what was said before them). A summary
  inevitably loses detail. The cut can only fall between turns, so a single turn that by itself exceeds the model's
  limit (say, one that reads many large files) can't be compacted. Models not listed in `config.py` are all assumed
  to have a 128K-token window, so models with a larger window are compacted earlier than they need to be. After you
  reopen an old conversation, the first request has to measure its size before compaction can kick in.
- Attachments work in the web UI only, not in the terminal. Images with GPT and Gemini, and PDFs with GPT, have not
  been tested with real keys. Gemini and DeepSeek can't read PDFs.
- GPT goes through the Chat Completions API. A new OpenAI model that only supports OpenAI's other API won't work here.
- Only the three Claude models the program has been adapted to are listed; other Anthropic models accept slightly
  different parameters.
- Each command runs in a new shell. Only the folder carries over (`cd` sticks); environment variables, aliases and
  functions don't, and a reopened conversation starts again in the working folder instead of where it was.
- The terminal UI saves conversations only while it holds the data folder: with the web UI or the background service
  running, a terminal conversation isn't saved and `/open` is off, and with the terminal open, the web UI and the
  service can't start. `/open` prints the last few messages as plain text, not the whole conversation.
- Search keeps the text of every conversation in memory, so a very large number of conversations (thousands of long
  ones) takes more memory. The conversation in progress isn't searchable until it has been saved. Pinyin search matches
  whole syllables only: there is no fuzzy pinyin (`z`/`zh`, `n`/`l`), the last syllable can't be left unfinished, and
  initials are matched in titles only.
- At most three conversations run at the same time in the web UI (`MAX_PARALLEL_CHATS`), and one that is running can't
  be renamed or deleted until it finishes or you stop it. The terminal UI has one conversation.
- The web UI renders a subset of Markdown: CommonMark plus GitHub's tables, task lists and strikethrough. Images
  aren't loaded (you see their description and address), raw HTML is shown as text, links work only for http, https
  and mailto, and indented code blocks, reference-style links and footnotes aren't supported. Code coloring knows
  only comments, strings, numbers and keywords of common languages. The terminal shows the Markdown source as is.
- Scheduled tasks and chat channels run only while the web UI or `harness serve` is running; tasks at most three at once.
  Chat channels take text, photos and files (no video yet; voice messages only on Telegram, transcribed first) and can send photos and files back, except on QQ,
  which only takes media from a public URL; WhatsApp and WeCom aren't supported yet because
  they need a public URL. Discord, Slack, Feishu, DingTalk and QQ were built from their official documentation and
  are tested only against local fakes so far, not yet with real bots. Two runs (or a run and your
  conversation) editing the same lines of the same file don't merge their changes: each edit is applied whole, and the
  later one fails if the text it expected has changed, so the model reads the file again. Reminders appear only in the
  web page (with a system notification while the page is open in the background, see **System notifications** in [Web UI](web-ui.md)),
  not in chat apps.
- With the interface in English or Spanish, messages returned by tools (such as "没有编号为 9 的待办", "there is no to-do
  #9") stay in Chinese, because they are written for the model.
  Conversations saved before you switched language keep the program's notices in the language used at the time.
- Skills: `allowed-tools` doesn't pre-approve anything (see [Skills](extensions.md#skills)). Claude Code's argument placeholders
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
- Computer use has never been run against a real screen or with a real API key: only against the fake desktop backend
  and, in CI on Ubuntu, against a virtual X11 display. By design, the macOS capture and input paths have not been tried
  on a real display (only the read-only checks: screen size and the two permission checks); the direction of
  horizontal and vertical scrolling on macOS is unverified; GPT and Gemini's stand-ins haven't been tried with real
  keys. It uses the main display only; Wayland and Windows aren't supported; blocked apps are matched by name against
  the frontmost app (on macOS also the window under a click), so a blocked app partly visible behind another window can
  still appear in a screenshot. Typing on a non-US keyboard layout types the right text, but shortcuts are sent by key
  position (US layout). Long sessions accumulate screenshots in the history; start a new conversation for a new job.
- The command sandbox: the macOS backend (`sandbox-exec`) is tested for real on a Mac (writing inside and outside the
  working folder, reading a stand-in for `~/.ssh`, a local web server with and without the network); the Linux backend
  (bubblewrap) is tested only in CI on Ubuntu, not on other distributions or a desktop; Windows has no sandbox (see
  [Sandbox for commands](sandbox-and-hooks.md#sandbox-for-commands-what-a-command-can-reach) for why). Inside the sandbox, programs
  installed in your home folder need their folder added as an extra path, `~` is the run's own empty home folder,
  `sudo` and `ps` don't run on macOS, and localhost counts as network. It doesn't limit CPU, memory or disk, and it
  can't protect the working folder itself or what you later run from it.
- **Stop** in the web UI takes effect the next time the model produces output: during the few seconds when the model is
  still thinking and has produced nothing, it won't stop immediately.
- Three Claude features use beta API features: automatic retry with another model after a refusal, automatic history
  compaction, and dropping mismatched thinking when you continue an old conversation. The third is included in every
  Claude request. If every request fails with a 400 error, you can turn them off in [harness/config.py](../../harness/config.py)
  (one switch each).

## Roadmap

- Add a "don't ask again this session" option for common read-only commands (`ls`, `git status`).
- Add a locally run search tool for models that don't support web search.
- MCP resources and prompts, and OAuth sign-in for remote MCP servers.
- Computer use on more than the main display, and with a real Claude key on a test machine.
- A sandbox for commands on Windows (an AppContainer, if its folder permissions and PowerShell can be made to work).
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
servers. With another model, a page read with `web_fetch` is fetched from this computer: that site sees the address,
your IP address and `ZhiFox/<version>`, and nothing else (no cookies, no language, no key). If you connect MCP servers, the arguments of each MCP tool call you approve go to that server. With browser
use on, the sites the assistant opens see an ordinary visit from ZhiFox's own Chrome, and what it reads there goes to
the provider. With computer use on (off by default), the screenshots of your screen that you approve go to the
provider as well. With a custom endpoint on this computer or your local network (Ollama, LM Studio…), all of this stays
on your network. Saved
conversations, memory, to-dos, settings and keys stay in plain files on your disk and are not uploaded anywhere else.
If you turn on the [update check](installation.md#updates) (off by default), ZhiFox asks GitHub once a day for the latest release;
GitHub sees your IP address, and nothing else is sent. The web UI's server accepts connections from your own machine only. See
[Data and privacy](data-and-privacy.md).

### Which models does it support?

Claude (`claude-opus-5-5`, `claude-sonnet-5-5`, `claude-fable-5-2`) through Anthropic's native API, and GPT, Gemini and
DeepSeek through the OpenAI-compatible Chat Completions API, using the model lists you sync in Settings or the model
switcher. Only Claude has
been tested in real chats so far. Web search is Claude-only; long conversations are compacted with every provider (by
Anthropic's API for Claude, by ZhiFox itself for the others). Any other provider that speaks the OpenAI format can be
added with one entry in `PROVIDERS`, and any OpenAI-compatible server (Ollama, LM Studio, vLLM, llama.cpp, your own
gateway) can be added from Settings without touching code (see
[Local models](models.md#local-models-and-your-own-gateway-ollama-lm-studio-vllm-llamacpp)). Custom endpoints have been tested
against local fake servers only, not a real Ollama or LM Studio yet.

### Can it run commands without asking me?

Not by default. Every shell command and every file write waits for your approval. You can loosen this for the current
session with `/auto edits` or `/auto all` in the terminal, or the **Ask every time** button in the web UI; it resets on
every launch, and steps that ran without asking are marked as allowed automatically. Computer use (off by default) asks
before every click, keystroke and screenshot unless you choose otherwise: on the approval card, for the one
conversation you're watching, or with Allow everything; dangerous shortcuts (⌘Q on a Mac, Alt+F4 or Ctrl+Alt+Del
elsewhere) ask every time regardless. Scheduled tasks have their own
setting, chosen per task in the task editor (Ask every time by default; tasks the assistant creates always ask). In
every mode the assistant can't touch `.env`, and the commands it runs can't see your API keys. See
[Permissions](tools-and-permissions.md#permissions). For a second safeguard, turn on the
[sandbox for commands](sandbox-and-hooks.md#sandbox-for-commands-what-a-command-can-reach): even a command that ran without asking can then
reach only the working folder (and, if you choose, no network), and running one outside the sandbox asks every time.

### Can it do things on a schedule?

Yes, while the web UI or `harness serve` is running. Ask for "remind me at 3 p.m. to call the bank" (a free reminder) or "every Monday at
9, summarize my open to-dos" (the assistant does it, and each run costs API credits). Runs happen alongside your
conversation, pause for your approval before commands and file writes (unless you loosen that for the task), and are
caught up once if ZhiFox was closed. See
[Scheduled and recurring tasks](scheduled-tasks.md).

### What does it cost to run?

ZhiFox itself is free and MIT-licensed. You pay each provider for API usage by the token, separately from any chat
subscription. Cost grows with conversation length and the number of steps, because each request resends the history.
The web UI and `/usage` show an estimated cost as you go; Claude's web search adds $0.01 per search. The tests and the
demo cost nothing. See [Cost](cost.md).

### Can I use it from my phone?

Yes, through your own bot on Telegram, Discord, Slack, Feishu, DingTalk or QQ (see [Chat channels](chat-channels.md)). Only the account you pair can use it,
and anything that would change your computer still asks you first, in the chat; set the channel's sandbox to Working
folder only and even an allowed command stays inside the working folder, off the network. To keep the bot answering when no
terminal is open, run `uv run harness service install` (see [Run it in the background](background-and-servers.md#run-it-in-the-background)). On a
server, run `harness serve` (see [Deploying to a server](background-and-servers.md#deploying-to-a-server)).

### Does it run on Windows?

Yes. It is tested on every push on GitHub's Windows runner (not yet by hand on a Windows 10/11 desktop). Commands run in PowerShell, the background service uses Task
Scheduler, and the page says Ctrl and File Explorer. Computer use isn't available on Windows yet. See [Windows](windows.md).
