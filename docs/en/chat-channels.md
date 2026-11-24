**English** | [简体中文](../zh-CN/chat-channels.md)

[ZhiFox](../../README.md) › [Documentation](../../README.md#documentation) › Chat channels

# Chat channels

Setting up a bot and using it comes first; [how it works](#chat-channels-talking-to-zhifox-from-your-chat-app) follows,
including voice messages and how the six platforms differ.

Open **Extensions → Channels** and pick a platform. A four-step wizard walks you through it:

1. **Create a bot.** The wizard shows the platform's steps (below), with links to its developer console.
2. **Paste the token** (or the ID and secret). ZhiFox checks it once with the platform and shows the bot's name. The
   secrets are stored only in `data/channels.json` (readable by your account only); the page shows their last 4
   characters from then on. Editing later, you only paste what changes: fields left empty keep their saved value.
3. **Pair.** A 6-digit code appears with a countdown. Send it to your bot from your own account. On Telegram, **Open in
   Telegram** sends it for you when you press Start; on the others the button opens the chat with the bot. The page
   moves on by itself once you're paired.
4. **Approval mode.** Ask every time (the default), Allow read-only commands, Allow file edits, or Allow everything, for
   this channel only. Allow read-only commands suits looking after a server from your phone: `df -h` or
   `systemctl status nginx` runs at once, a restart still asks (see
   [Read-only commands](tools-and-permissions.md#read-only-commands-and-why-the-check-is-conservative)). Allow everything comes with a warning:
   with it, one message in the chat can make the assistant run any command. The same step has the channel's **Command
   sandbox**: Follow settings (the default), Off, Working folder only or Working folder + network. For a server you look
   after from your phone, Working folder only is the recommended one (see
   [Sandbox for commands](sandbox-and-hooks.md#sandbox-for-commands-what-a-command-can-reach)). When the sandbox blocks a command that's
   really needed, the assistant asks you in the chat to let that one command out, every time; a channel set to Allow
   everything can't, because nobody would answer.

Setting up each platform (the wizard and `harness channels setup <platform>` print the same steps):

- **Telegram.** In [@BotFather](https://t.me/BotFather) send `/newbot`, pick a name and a username ending in `bot`,
  and copy the token (`123456789:AAH…`).
- **Discord.** In the [Developer Portal](https://discord.com/developers/applications): **New Application** → **Bot** →
  **Reset Token** and copy it (it's shown once). On **Installation**, tick **User Install**. After you paste the token,
  the pairing step gives you a link to add the bot to your own account (**Add to My Apps**); then open a DM with it.
  You don't need the Message Content intent: DMs with the bot are exempt.
- **Slack.** [Create an app](https://api.slack.com/apps?new_app=1) **From a manifest** and paste the one the wizard
  copies for you (bot scopes, `message.im`, interactivity, Socket Mode and the `/zhifox` command, all set; an app made
  from an older manifest needs the `files:write` scope added and the app reinstalled before it can send files). Then
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
and PowerPoint become text; videos aren't supported yet). On Telegram, voice messages and audio files are transcribed
first and answered like text (see [Voice messages](#voice-messages-telegram)): turn it on in **Settings → Voice to
text**. It works the other way too: ask for a
screenshot, a chart or the report it just wrote, and after you approve the card (file, size, where it goes) it arrives
in the chat as a photo or a file (except on QQ; see the limits in [Six platforms, three ways in](#six-platforms-three-ways-in)).
In the chat (on Slack, write
`/zhifox new` and so on):

| Command | What it does |
|---|---|
| `/new` | Start a new conversation (the previous one stays in the sidebar). `/new <agent name>` starts it with that custom agent as the persona |
| `/stop` | Stop the turn in progress at once (a running command and a reply the model is still thinking about included), and drop queued messages |
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
(`harness serve`, see [Deploying to a server](background-and-servers.md#deploying-to-a-server)). The terminal UI doesn't run them.

## Chat channels: talking to ZhiFox from your chat app

A chat channel is one more way into the same loop. Using Telegram as the example
([harness/channels/](../../harness/channels/)); the other platforms differ only in how messages arrive (see
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
   ([telegram.py](../../harness/channels/telegram.py)): `getMe`, `getUpdates`, `sendMessage`, `editMessageText`,
   `answerCallbackQuery`, `sendChatAction`, `getFile` and `setMyCommands`, with retries that back off, waiting when
   Telegram rate-limits, and the bot token removed from every error message.
2. **Only the owner.** A bot's name can be found by anyone, but this bot can run commands on your computer. So it is
   paired like a Bluetooth headset ([pairing.py](../../harness/channels/pairing.py)): the page (or `harness channels pair`)
   shows a random 6-digit code valid for 10 minutes; you send it to the bot from your own account; that account's id
   becomes the owner. Messages from anyone else are ignored without a reply, so the bot looks switched off. During the
   10 minutes of pairing, a stranger who sends something else gets one neutral hint, a wrong code gets "wrong code, N
   tries left", and after 5 wrong codes the code is cancelled, so it can't be guessed one by one. Group chats are ignored entirely (everyone in a group could talk to it).
3. **One conversation per chat, beside the web chat.** Each chat has a `Session`
   ([session.py](../../harness/channels/session.py)) with its own conversation, adapter, shell, MCP calls, browser tabs and approval mode
   (`App.channel_agent`, the same pattern as a scheduled task's run), and a queue: one turn at a time per chat, further
   messages wait (up to 5). Sessions don't take the web chat's lock, so `/stop` in Telegram stops only that chat, and
   the web page's Stop never reaches it. The conversation is saved like any other (tagged "Telegram" in the sidebar,
   spending recorded with the source `channel`) and continues after a restart. If the same conversation is open in the
   page, each turn first *claims* it ([claims.py](../../harness/claims.py)): while Telegram runs a turn the page can't send,
   open, rename or delete it, and when the turn ends the page reloads it, so neither side overwrites the other.
4. **Talking back.** The loop talks to a `ChannelUI` ([ui.py](../../harness/channels/ui.py)), the same event log the web UI
   uses (so the saved conversation replays in the page), which hands each finished paragraph to a per-platform send
   queue ([outbox.py](../../harness/channels/outbox.py), fed by [router.py](../../harness/channels/router.py)): network
   sends never block the loop, the order is kept, and a send that fails is retried or kept for later (see
   [When a message can't be delivered](#when-a-message-cant-be-delivered)).
   Replies are converted from Markdown to the small HTML subset Telegram accepts (only `& < >` need escaping, unlike
   MarkdownV2) and split under Telegram's 4,096-character limit, counted in UTF-16 the way Telegram counts, closing and
   reopening a code block that has to be split ([markup.py](../../harness/channels/markup.py)). If Telegram still rejects the
   formatting, the piece is resent as plain text. While a turn runs, "typing…" is refreshed every 4 seconds.
5. **Approvals.** When a step needs approval, the chat gets a message with the full command (in a code block, nothing
   shortened) and the working folder, and two buttons. A command too long for one message is sent in parts, with the
   buttons on the last one. The same request appears on the web page (a bar at the top of the main area and a card in
   **Extensions → Channels**); whichever answer arrives first counts, and the chat message is edited to say who decided
   ("Allowed on the web page"). You can also just reply "allow" or "deny" (the only way on platforms without buttons);
   typed answers count only once the request has actually reached the chat. In **Allow read-only commands**, **Allow file
   edits** or **Allow everything** mode, each step taken without asking is still reported in the chat ("Allowed
   automatically: $ …", or "Allowed automatically (read-only): $ df -h" for a read-only command).
6. **A persona per chat.** `/new researcher` starts a conversation with the custom agent *researcher* as its persona,
   the same as starting one from the page (its prompt, only the tools it allows, its model if it names one).
7. **Sending files back.** "Take a screenshot and show me" used to end with "I can't send images to Telegram": the
   assistant could take the screenshot and look at it, but had nothing to send it with. A chat conversation now has one
   more tool, `send_file` ([tools/send.py](../../harness/tools/send.py)): a path and an optional caption. Images (PNG, JPEG,
   GIF, WebP, recognised by their first bytes) go as photos where the platform shows them inline, everything else as a
   document, each platform within its documented size limits. It asks first like any step that sends something off the
   computer (see [Permissions](tools-and-permissions.md#permissions)), resolves paths exactly like the file tool, and never sends the files that
   hold secrets. The file is read again just before sending and compared with what the card showed (same place, size and
   format), and it goes through the chat's send queue, so it lands after what the assistant said before it. The
   platform half lives in [outbound.py](../../harness/channels/outbound.py) (which platform takes what) and each platform's
   `send_file`; uploads use a hand-written `multipart/form-data` encoder ([net/multipart.py](../../harness/net/multipart.py))
   on top of the same HTTPS helper (timeouts, size cap, secrets redacted from errors).
   **Where the tool exists** follows the rule that a conversation's tool list never changes: it belongs to conversations
   that *started* in a chat channel (`Conversation.channel`), in the chat and also when you continue one in the web page,
   where the spec is byte for byte the same (the cache still hits) and calling it answers "this conversation isn't in a
   chat app right now". The web chat and scheduled tasks never have it, so they don't pay for a spec they can't use. The
   spec names no platform and no limit for the same reason; when a file can't go, the result says why and what to do
   ("zip it first", "make a smaller copy"). The first message of a chat conversation (the `<channel>` note) also tells
   the model to send files rather than paths, which a phone can't open; the system prompt is untouched, because every
   conversation shares it.

### When a message can't be delivered

A chat platform is a server on the internet, and the connection to it drops now and then. In one user's logs the
connection to Telegram was reset by the other side about every ten minutes and came back within half a minute; a reply
that landed in such a window used to be lost, with nothing but a log line. Every send now goes through the platform's
send queue ([outbox.py](../../harness/channels/outbox.py)), which tells the two kinds of failure apart in one place
(`retry_delay` in [base.py](../../harness/channels/base.py); each platform only fills in the HTTP status it got, `0` for
no answer at all):

- **Passing failures are retried.** No answer (the connection was reset or refused, a timeout, a DNS failure), the
  platform failing (5xx) or rate limiting (429, or the platform's own "too fast" code): the queue waits and tries again,
  up to 4 times, 1, 2, 4 and 8 seconds apart (`CHANNEL_SEND_RETRIES`, `CHANNEL_SEND_RETRY_SECONDS` in `config.py`), or
  as long as the platform's `Retry-After` says, capped at 30 seconds. Later messages wait meanwhile, so a chat never
  sees them out of order.
- **Lasting failures aren't.** Any other 4xx (a malformed request, a bot you blocked, a message too long) or a bad token
  would fail the same way again: one log line, and the message is counted as not delivered.
- **Nothing is dropped silently.** A reply, an approval request, or the edit that writes the answer on an approval
  request, that still fails after the retries is kept in memory, per chat and in order (up to 50 per chat,
  `CHANNEL_UNDELIVERED_MAX`), and sent again as soon as the channel reports it's online again, when a new message comes
  in from that chat, or every 30 seconds while the channel looks online. Whatever that chat sends next waits behind
  them. An approval request that hasn't reached the chat says so on the web page ("Not delivered to the chat yet"),
  where you can answer it: the turn is waiting for it either way.
- **You can see it.** The channel's row in **Extensions → Channels** says how many messages are waiting, how many
  couldn't be delivered, and when and why the last failure happened; `/status` in the chat says the same (its answer
  isn't queued behind the waiting messages). `harness channels status` runs in another process and can't see the queue.
- **Files report back.** `send_file` gets the same retries but is never kept for later: the model waits for the
  result, so it's told "not sent: …", or, if the connection broke mid-upload and you stopped it, that the file may or
  may not have arrived.
- **Stopping doesn't wait.** `/stop` stops the turn at once, and a file waiting to be retried isn't tried again. On
  shutdown the queue doesn't wait out its backoff: every queued and kept message gets one more try, and whatever still
  fails is logged (memory doesn't survive the exit).
- **A rare duplicate rather than a lost reply.** If the request did reach the platform and only its answer was lost, a
  retry shows the message twice; if one piece of a long reply that's split into several messages fails, the pieces
  before it are sent again. That's the trade-off: a duplicate now and then is better than a reply that silently never
  arrives. Edits don't duplicate: editing to the same text again is harmless (Telegram answers "message is not
  modified", which counts as done).
- **"Online" right after a reset.** The first `getUpdates` after starting or after an error doesn't wait on Telegram's
  side (`timeout=0`), so the status is back to *Online* within a second or two instead of after a whole 25-second long
  poll. The logs used to show "connecting" for about 26 seconds after every reset; incoming messages weren't delayed
  (Telegram answers a long poll as soon as one arrives), but replies kept for later waited for that "online". Discord,
  Slack, QQ, Feishu and DingTalk already report online as soon as their handshake completes.

### Voice messages (Telegram)

Holding the microphone button in Telegram sends a voice message (an OGG/Opus file with its length); an audio file
(MP3, M4A…) arrives the same way. None of the models ZhiFox uses takes sound in a chat request, so the channel turns
it into text first ([harness/voice/](../../harness/voice/)):

```text
voice message ──► checks (switched on? at most 10 minutes? a format the service takes?) ──► download from Telegram
      ──► POST <service>/audio/transcriptions (multipart: file, model, response_format=json) ──► {"text": …, "usage": {"seconds": …}}
      ──► the chat gets "(Voice to text) …"  ──► the turn starts with "（语音转文字）…" as your message
```

- **Which service.** **Settings → Voice to text** picks one: *Automatic* (the default: OpenAI when an OpenAI API key is
  set, otherwise off), *OpenAI* (`POST https://api.openai.com/v1/audio/transcriptions`, model `gpt-transcribe`, which
  [OpenAI's speech-to-text guide](https://developers.openai.com/api/docs/guides/speech-to-text) recommends; the older
  `gpt-4o-transcribe`, `gpt-4o-mini-transcribe` and `whisper-1` can be chosen too, until OpenAI retires them on
  2027-02-26), *a compatible service you run yourself* (anything that speaks the same API, for example a
  faster-whisper server; its address and model name are set in the page) or *Off*. Without a usable one, the chat
  replies with how to turn it on.
- **Keys and addresses.** The OpenAI key comes from `.env` and is only ever sent to OpenAI's address, which can only be
  changed by environment variable (`OPENAI_BASE_URL`), like the chat platforms' API bases. The address of your own
  service can be set in the page because no key is ever sent there, only your audio; plain `http://` is allowed for
  this computer only (`localhost`, `127.0.0.1`, `::1`), anything else must be `https://`, and the address may not carry
  a user name or password. The key never reaches the browser (the page only learns whether one is set), the logs or an
  error message.
- **Or one of your custom endpoints.** Instead of typing an address, *a compatible service you run yourself* can use one
  of your [custom model endpoints](models.md#local-models-and-your-own-gateway-ollama-lm-studio-vllm-llamacpp) that is on this
  computer or your local network: ZhiFox uses its address and its own key, if it has one, sent only to that endpoint as
  in chat; the OpenAI key never goes there. Gateways outside your network aren't offered, because ZhiFox can't know what
  they charge per minute and couldn't record the cost. If the endpoint is deleted or moved outside your network, voice to
  text turns off (it never silently switches to another address) and the settings say why.
- **What the model sees.** The transcript, prefixed with "（语音转文字）" (written for the model, so it stays in
  Chinese like the rest of the prompts), so it knows the words were heard, not typed, and may contain mistakes. A
  caption on the voice message is appended. In the web page the same message shows a small *Voice to text* tag with
  the length of the recording; editing that message later gives you only the text.
- **Cost.** Transcription is billed by length (see [Cost](cost.md)). Every transcription is one line in the spending
  record with source `voice`; your own service is recorded at $0. Messages longer than 10 minutes
  (`VOICE_MAX_SECONDS`) or bigger than 20 MB (`VOICE_MAX_BYTES`, Telegram's download limit for bots; OpenAI takes up
  to 25 MB) are refused before anything is downloaded or sent, so they cost nothing.
- **Only Telegram for now.** Discord, Slack, Feishu, DingTalk and QQ deliver voice differently (as attachments with
  their own formats, or not at all to bots); there a voice message gets "voice to text only works on Telegram for now".

### Six platforms, three ways in

Every platform is a `Channel` subclass that turns the platform's events into the same `Inbound` / `Press` objects and
sends what the router hands it ([base.py](../../harness/channels/base.py)); the router, sessions, pairing, approvals and
the web page don't know which platform they're talking to. None of them needs a public URL, because ZhiFox always
opens the connection:

| Platform | How messages arrive | Approvals | Formatting | Notes |
|---|---|---|---|---|
| Telegram | Long polling (`getUpdates`) | Buttons | HTML subset | 4,096 UTF-16 units per message |
| Discord | Gateway WebSocket ([gateway.py](../../harness/channels/gateway.py)): Hello, Identify / Resume, heartbeats | Buttons (message components) | Markdown as is (`####` and deeper become bold) | Only the `DIRECT_MESSAGES` intent; reading DMs doesn't need the privileged Message Content intent. Slash commands registered for the bot's DM |
| Slack | Socket Mode WebSocket: each envelope acked at once | Block Kit buttons | mrkdwn (`*bold*`, `<url\|text>`, `& < >` escaped) | Slack intercepts every message starting with `/`, so commands are `/zhifox new`, `/zhifox stop`… |
| Feishu | Long connection through the official SDK `lark-oapi` | Interactive card buttons (falls back to a text prompt) | Markdown in a `post` message's `md` tag | Optional: `uv sync --extra feishu` |
| DingTalk | Stream mode through the official SDK `dingtalk-stream` | Reply "allow" / "deny" (buttons need a card template made in DingTalk's card platform) | DingTalk markdown | Optional: `uv sync --extra dingtalk`. Replies use the message's `sessionWebhook`, then the one-to-one send API once it expires |
| QQ | Gateway WebSocket (the same design as Discord's) with an `access_token` that's renewed | Reply "allow" / "deny" | Plain text | Up to 4 "passive" replies per message within 60 minutes, then proactive messages |

Sending files back (`send_file`) uses each platform's own upload API; the limits are the ones in its documentation:

| Platform | How a file is sent | Limits |
|---|---|---|
| Telegram | `sendPhoto` for images (`sendAnimation` for GIFs), `sendDocument` for everything else, uploaded as `multipart/form-data`; the caption goes in the message | Photos 10 MB (bigger images, or ones Telegram can't show, go as documents), files 50 MB, caption 1,024 characters (a longer one follows as a message) |
| Discord | The message itself with the file as `files[0]` and the message in `payload_json` | 20 MB per file; caption 2,000 characters |
| Slack | `files.getUploadURLExternal`, the bytes to the one-time upload URL (without the bot token), then `files.completeUploadExternal` into the DM | Needs the `files:write` scope (in the manifest; an app made from an older manifest needs it added and reinstalled) |
| Feishu | Upload to `im/v1/images` (images) or `im/v1/files` (with a `file_type` by extension), then an `image` or `file` message; the caption follows as a message | Images 10 MB, files 30 MB |
| DingTalk | Upload to `media/upload` (the older `oapi` API, same access token), then a `sampleImageMsg` or `sampleFile` one-to-one message | 20 MB; images JPEG, PNG, GIF; files only doc, docx, xlsx, pdf, zip, rar (the model is told to zip anything else) |
| QQ | Not supported: QQ's one-to-one rich media must be a public URL that QQ downloads, and ZhiFox has none (putting your file on the internet for it would hand it to anyone with the link) | The tool says so |

- **The gateway** ([gateway.py](../../harness/channels/gateway.py)) is written once for Discord and QQ: the first heartbeat
  waits `interval × random()`, a heartbeat that isn't acknowledged before the next one means the connection is dead
  (it is dropped without a close handshake and resumed), op 7 means "resume elsewhere", op 9 says whether the session
  can be resumed. Each platform supplies its own table of close codes: a bad token or a forbidden intent stops the
  channel instead of retrying, because Discord resets a bot's token after 1,000 logins in a day; reconnects back off up
  to 120 seconds for the same reason.
- **The WebSocket client** ([net/websocket.py](../../harness/net/websocket.py)) implements the part of RFC 6455 a client
  needs: the handshake with its `Sec-WebSocket-Accept` check, masking every frame it sends (and rejecting masked
  frames from the server), fragmented messages with control frames in between, ping/pong, the close handshake, closing
  with 1002/1007/1009 on protocol errors, invalid UTF-8 or oversized messages (refused as soon as the length is read),
  timeouts on everything, and HTTP CONNECT proxies from `HTTPS_PROXY`. Plain `ws://` is accepted only for this
  computer (the test fakes).
- **The official SDKs** are optional dependencies imported only inside [feishu.py](../../harness/channels/feishu.py) and
  [dingtalk.py](../../harness/channels/dingtalk.py), and only for receiving: sending, editing cards and downloading files
  are plain HTTPS calls, like the other platforms. The SDKs' rough edges (a module-level event loop, no public way to
  stop, retrying bad credentials forever, logging connection tickets) are worked around in one small `Listener` class
  each, which is also the seam the tests replace.
- **Each new platform** needs three things: a `Channel` subclass, an entry in `hub.FACTORIES` / `CHECKS` / `CLASSES`,
  and its row in [platforms.py](../../harness/channels/platforms.py) (stage `ready`, the fields to fill in) plus its setup
  steps in [guides.py](../../harness/channels/guides.py), which the page's wizard and `harness channels setup` both show.
