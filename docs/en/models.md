**English** | [简体中文](../zh-CN/models.md)

[ZhiFox](../../README.md) › [Documentation](../../README.md#documentation) › Models and providers

# Models and providers

## Supported models

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
You can enter keys in the web UI's Settings, or copy [.env.example](../../.env.example) to `.env` and write them there. Both
end up in the same place.

## Local models and your own gateway (Ollama, LM Studio, vLLM, llama.cpp)

Any server that speaks the OpenAI Chat Completions format can be added under **Settings → Custom model endpoints**: a
model running on this computer, a GPU box on your network, or your company's own gateway. With an endpoint on this
computer or your local network, your conversations never leave your network, and the model costs nothing (it is shown
as "Local, free"). Each endpoint gets its own group in the model switcher.

**Ollama**, in one step: with Ollama running (`ollama serve`, or the desktop app), click **Add endpoint**, pick
**Ollama** (`http://localhost:11434/v1`), click **Test connection**, then **Save**. ZhiFox lists your pulled models and
asks Ollama what each one can do (tools, images, context length).

**LM Studio**, in one step: in LM Studio's Developer page, click **Start Server**, then add an endpoint and pick
**LM Studio** (`http://localhost:1234/v1`), **Test connection**, **Save**.

vLLM (`http://localhost:8000/v1`) and llama.cpp's `llama-server` (`http://localhost:8080/v1`) work the same way.
The API key is optional; fill it in if your server or gateway needs one.

**Choosing a local model.** Reading and writing files, running commands and keeping to-dos all need a model that
supports tool calling (function calling). Qwen 3, Llama 3.1 and later, Mistral and GPT-OSS do; small or old models
(Llama 2, Gemma 2, Phi-3) usually don't. A model without tools can only answer in text: the first time the server
refuses tools, ZhiFox marks the model **No tools**, tells you in the conversation, and the capability list shows every
tool as unavailable with the reason. vLLM needs `--enable-auto-tool-choice --tool-call-parser …` and llama.cpp needs
`--jinja` for tools. ZhiFox's system prompt and tool descriptions alone take several thousand tokens, so give the model
a large enough context: Ollama defaults to 4096 and silently cuts the start of longer prompts; set
`OLLAMA_CONTEXT_LENGTH=32768` (or more) before starting it (in PowerShell: `$env:OLLAMA_CONTEXT_LENGTH=32768`), and enter the same number as the model's context limit in
the endpoint's settings, which is what ZhiFox uses to decide when to compact the conversation.

Security rules: a remote address must use `https://`; plain `http://` is accepted only for this computer (`localhost`,
`127.x`, `::1`) and private-network addresses (`10.x`, `172.16–31.x`, `192.168.x`, `100.64.x` as used by Tailscale,
`*.local`, `*.internal`, single-label names). All requests go out from ZhiFox's own process, never from the web page.
The key is stored in `data/endpoints.json` (readable by your account only, masked in exports, shown as its last 4
characters); it never goes into environment variables, so commands and MCP servers can't see it. Keys of the built-in
providers (`OPENAI_API_KEY`, `OPENAI_ORG_ID`, `OPENAI_CUSTOM_HEADERS`, …) are never sent to a custom endpoint, redirects
are not followed, and changing an endpoint's host means entering its key again. Agent files and other programs name an
endpoint's model as `custom-<id>/<model>`, for example `custom-ollama/qwen3:8b`.

## One loop, many models

The history in [The loop](architecture.md#the-loop) is in Claude's format. Other providers use different formats, but the loop (`agent.py`) doesn't care.
It only requires each adapter to do the following, as defined in [harness/providers/base.py](../../harness/providers/base.py):

| Method | What it does |
|---|---|
| `add_user` | Append a user message to the history |
| `step` | Call the model once, display the output as it streams in, append the reply to the history, and tell the loop which tools the model wants to call |
| `add_tool_results` | Append tool results to the history |
| `mark` / `rollback` | Remember a position in the history / go back to it (to undo a turn after an error or a refusal) |

There are only two adapters. [claude.py](../../harness/providers/claude.py) uses Anthropic's native API.
[openai_compat.py](../../harness/providers/openai_compat.py) uses the Chat Completions format that OpenAI defined; GPT, Gemini
and DeepSeek all support it at different server addresses, so one piece of code serves all three. The main differences:

| | Claude | GPT, Gemini, DeepSeek |
|---|---|---|
| System prompt | A separate parameter | The first message in the history |
| Tool calls | A "content block" in the reply | A `tool_calls` field on the assistant message; the arguments are a JSON string |
| Tool results | All results in one message | One message per result |
| Web search | Yes, run on Anthropic's servers | No. ZhiFox's own `web_fetch` reads a page by its address, but can't search (see [Reading a web page](browser-and-computer-use.md#reading-a-web-page-without-a-browser-web_fetch)) |
| Very long conversations | Anthropic's API condenses earlier content into a summary on its servers | ZhiFox asks the model for a summary and replaces the earlier content (see the next section) |
| Caching | We mark it in the request | Each provider handles it automatically |

To add another provider that supports the OpenAI format, add one entry to `PROVIDERS` in
[`harness/providers/__init__.py`](../../harness/providers/__init__.py).

Custom endpoints (see [Local models](#local-models-and-your-own-gateway-ollama-lm-studio-vllm-llamacpp)) use the same
adapter, with three differences. What a model can do (images, tools, PDF, context size) is recorded per model, not per
provider, since one Ollama may host a model with tools next to one without. Servers that implement the format only
partly are handled by stepping back once: if a server rejects `stream_options`, ZhiFox stops asking for usage; if it
rejects tools before any tool was used in the conversation, that conversation goes on without tools (the rejected
request was never processed, so the history is unchanged). Both are written to `data/endpoints.json`, so the next
conversation starts the right way. And switching between two models of one endpoint that differ in tool support starts
a new conversation, because a conversation's tool list never changes.

## When a conversation gets too long: compaction

Every turn resends the whole history, so the longer a conversation gets, the more each turn costs; once it grows past
what the model can read at once (its context window), requests are rejected and the conversation can't go on. The fix
is compaction: replace the earlier part with a summary and keep only the last few turns verbatim.

For Claude, Anthropic's API compacts on its servers (the request carries `context_management`; see
[claude.py](../../harness/providers/claude.py)), and on our side the history is still only appended to. The OpenAI format has
no such feature, so for GPT, Gemini and DeepSeek ZhiFox does it itself, in
[harness/providers/compaction.py](../../harness/providers/compaction.py):

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

The tunables are in [harness/config.py](../../harness/config.py): `ENABLE_OPENAI_COMPACTION` (set it to `False` to turn
compaction off), `CONTEXT_WINDOWS` (each model's window; 1M tokens for both DeepSeek models), `DEFAULT_CONTEXT_WINDOW`
(128K tokens for models not in the table, erring on the side of compacting early), `COMPACTION_THRESHOLD` (0.7) and
`COMPACTION_KEEP_TURNS` (2).

## When a model is temporarily unavailable: backup models

Sometimes a request fails for a reason that has nothing to do with you: the provider's servers are overloaded (Anthropic
answers 529 `overloaded_error`), something broke on their side (5xx), or you sent too many requests too quickly (429).
The SDK already retries these twice with a back-off; if it still fails, the turn used to stop with an error. Errors a
different model wouldn't fix (a wrong key, a malformed request, an OpenAI account whose quota has run out:
`insufficient_quota`) still stop the turn as before.

**What happens.** In **Settings → Backup models** you can list, in order, up to three other models **of the same
provider** (for example Claude Opus 5.5 → Claude Sonnet 5.5 → Claude Fable 5.2). When a request fails in one of the ways
above, the loop sends the very same request to the first backup it hasn't tried this turn, says so in the conversation
("Opus 5.5 is temporarily unavailable; the rest of this turn will use Sonnet 5.5."), and stays on it for the rest of the
turn. The next turn starts with your model again, which may have recovered by then, and the conversation keeps your
model as its model. Nothing is configured by default: switching models changes both the quality of the answer and the
price, so you decide whether to switch and to what. Scheduled tasks, chat channels and subagents follow the same setting.

**Why only the same provider.** A model from another provider can't continue this conversation. Its history is in a
different format (Claude's content blocks are not OpenAI-style messages), its system prompt differs (the "can search the
web" line is only written for Claude), and so do its tools (Claude gets Anthropic's browser and computer tool sets, the
others our own `browser_*` / `computer_*` stand-ins). Switching there halfway through a turn would mean throwing away the
tool calls and results the turn has already made and quietly changing the prompt and tools, which the
[append-only rule](architecture.md#the-loop) forbids. Within one provider, `App.select_model` already lets you switch mid-conversation:
the history, system prompt and tools stay exactly as they were and only the `model` field of the request changes.
Whether a backup can take over follows the same rule as switching in the model switcher (`providers.continues`: same
provider, and the same tool support, which matters only for custom endpoints), plus one more check: it must be able to
use the tool sets this conversation was started with (`Backend.can_switch_to`). A custom endpoint's backups are other
models on that same endpoint.

**What about Claude's thinking signatures?** Claude's thinking blocks are signed, and the signature records which model
wrote them. Changing the request's `model` is not an edit of the history, so a model switch never fails the request; a
block the new model can't read is left out by the API before the model sees it, and isn't billed. ZhiFox keeps sending
the history unchanged (stripping blocks yourself is what would break things). So when Opus 5.5 hands over to Sonnet 5.5,
the steps on Sonnet run without Opus's earlier reasoning (Sonnet 5.5 doesn't read Opus 5.5's blocks), but the request
succeeds; when the next turn goes back to Opus 5.5, it does read the blocks Sonnet 5.5 left (on the Claude API), so
nothing is lost.

**Where you see it.** The note in the conversation; in the [run trace](architecture.md#reading-a-turn-the-run-trace) the failed request
is marked as an error with the reason, and every request made on the backup is tagged "backup" with the model it stood in
for; the [spending record](cost.md) prices each request at the model that actually answered. The failed request left
nothing in the history (both adapters append only once a reply is complete); text that had already streamed onto the
page stays there, as with the existing retry when a tool call's arguments aren't valid JSON. The rules are in
[harness/fallback.py](../../harness/fallback.py); the loop part is `Agent._request` in [harness/agent.py](../../harness/agent.py).
