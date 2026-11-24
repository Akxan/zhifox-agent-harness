**English** | [简体中文](../zh-CN/cost.md)

[ZhiFox](../../README.md) › [Documentation](../../README.md#documentation) › Cost

# Cost

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
  skipped when reading. A call you stop is not written: the API reports its usage only at the end of the reply, so
  whatever the model had read and written before Stop isn't counted (see [Limitations](limitations.md)).
- **Monthly budget.** Optional, in Settings. Passing it shows one reminder and never stops anything, including
  scheduled tasks.
- **Search caps.** For each message you send, Claude can search the web at most 5 times and fetch at most 5 pages; past
  that, it answers with what it has. The API enforces this cap. Each web search costs $0.01 on top of tokens. ZhiFox's
  own `web_fetch` (models without web tools) has the same cap of 5 pages per message, enforced by ZhiFox, and costs
  only the tokens of the text it hands over.
- **Caching.** Parts that are sent again and again cost far less once they hit the cache (for Claude, about a twentieth
  of the normal input price). The cache lasts only a few minutes, so when you come back to a long old conversation after
  a while, the first message is billed at the full price for the whole history. Opening an old conversation of more than
  30,000 tokens shows a notice about this.
- **Automatic compaction.** In very long conversations, earlier content is condensed into a summary, so every later
  turn sends far fewer tokens. Claude's API does this itself; for GPT, Gemini and DeepSeek, ZhiFox asks the same model
  to write the summary, and that call is included in the cost estimate (see
  [When a conversation gets too long](models.md#when-a-conversation-gets-too-long-compaction)).
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
- **Computer use.** With Claude, the computer use toolset adds about 4,500 input tokens to every request of a
  conversation started with computer use on (cached like the browser's). Each screenshot (at most 1,568 px on the long
  edge and 1.15 megapixels, so about 1,500 tokens; a 1512×982 screen becomes 1330×864) is billed as image input, and
  the model takes one after nearly every step, so a long session adds up. Like the rest of the history, earlier
  screenshots are resent on every turn (cached); ZhiFox never removes them from the history, because on the current
  Claude models that would invalidate the model's earlier thinking. Start a new conversation for a new job. Settings
  shows the estimate for your screen.
- **Voice messages.** Transcribing a voice message from Telegram is billed per minute of audio, not per token, and
  recorded in the spending record as source "voice" (the seconds are taken from OpenAI's reply when it gives them,
  otherwise from Telegram). Prices from [OpenAI's pricing page](https://developers.openai.com/api/docs/pricing)
  (Transcription, estimated cost), checked on 2026-11-22: `gpt-transcribe` (the default) $0.0045 per minute,
  `gpt-4o-mini-transcribe` $0.003, `gpt-4o-transcribe` $0.006, `whisper-1` $0.006. A one-minute voice note on the default
  model is under half a cent. Voice messages longer than 10 minutes aren't transcribed. A compatible service you run
  yourself costs nothing.
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
