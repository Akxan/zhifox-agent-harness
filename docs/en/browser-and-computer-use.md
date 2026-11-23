**English** | [简体中文](../zh-CN/browser-and-computer-use.md)

[ZhiFox](../../README.md) › [Documentation](../../README.md#documentation) › Browser and computer use

# Browser and computer use

Three ways the assistant reaches beyond files and commands, from the safest to the most powerful: reading one page by
its address, using websites in a Chrome of its own, and (an advanced option, off by default) operating your whole
desktop. For the browser and for computer use, how to turn it on comes first, then how it works.

## Reading a web page without a browser (`web_fetch`)

Claude reads the web through two tools that run on Anthropic's servers: web search and web fetch. GPT, Gemini, DeepSeek
and models on custom endpoints have nothing like that, and the browser needs Chrome and a switch in Settings. Very
often the model only wants to read the address you just pasted. For that ZhiFox has a small read-only tool of its own,
`web_fetch`, in [harness/fetch/](../../harness/fetch/): it downloads one `http` or `https` page, keeps the readable text and
hands it to the model. It doesn't run scripts and sends no cookies, no login and no key. **It is not a search**: the
model has to know the address (one you gave it, or a link on a page it has read).

| File | What it does |
|---|---|
| [guard.py](../../harness/fetch/guard.py) | Which addresses it never goes to, and what a URL means (only `http`/`https`; a user name or password in the address, or an odd numeric host like `2130706433` or `0x7f.1`, is refused) |
| [client.py](../../harness/fetch/client.py) | The request: resolve the name once, check every address, connect to the one it checked, follow redirects itself and check each hop again; limits on time, size and redirects |
| [charset.py](../../harness/fetch/charset.py) | Bytes into text: the BOM, the `Content-Type` header, the page's own `<meta charset>`, then UTF-8 or windows-1252, with the browser's aliases (gb2312 read as gb18030…) |
| [readable.py](../../harness/fetch/readable.py) | HTML into readable text with the standard library's `html.parser`: title and description first, then headings, paragraphs, lists, tables and `<pre>`, a capped number of links as "text (url)"; scripts, styles, navigation, footers and hidden elements are dropped (hidden text is a favourite hiding place for instructions aimed at the model) |
| [content.py](../../harness/fetch/content.py) | Picks a reader by type: web pages, plain text and JSON, Word / Excel / PowerPoint (the same reader as attachments). PDFs, images and other files are refused with a reason |
| [session.py](../../harness/fetch/session.py) | One run's state: the sites allowed in this conversation and how many pages this message has read |
| [tools/fetch.py](../../harness/tools/fetch.py) | What the model sees: a fixed description, one `url` parameter, and a result that starts by saying the page is third-party material, not instructions |

**Who gets it.** GPT, Gemini, DeepSeek and custom endpoints. Claude with web search on already has Anthropic's
`web_fetch`, so ZhiFox's one isn't sent to it (they share the name, so `WebFetch` in an Agent file means whichever one
the conversation has). With `HARNESS_WEB=0` Claude gets ZhiFox's one too. It is on by default; turn it off in
**Settings → Read web pages**. Like the browser, it belongs to a conversation from its start: switched off later, the
tool stays and answers that it's off, and a conversation that used it gets it back when reopened.

**The security boundary.** This is a hard rule with no switch; for pages on your own computer or network, use the
[browser](#browser-use-a-chrome-of-its-own).

- Every address a name resolves to must be public. Loopback (127.0.0.0/8, `::1`), private networks (10/8, 172.16/12,
  192.168/16, `fc00::/7`), link-local (169.254/16, where the cloud metadata address 169.254.169.254 lives, and
  `fe80::/10`), carrier-grade NAT (100.64/10), multicast, reserved and unspecified addresses (`0.0.0.0`, `::`) are
  refused. An IPv4 address hidden in IPv6 (`::ffff:127.0.0.1`, NAT64 `64:ff9b::/96`, 6to4 `2002::/16`) is judged by
  the IPv4 inside. If a name resolves to one public and one private address, nothing is fetched. The list is ZhiFox's
  own, not Python's `is_private`, whose answers changed between Python versions.
- The address that was checked is the address it connects to: the name is resolved once, so a second DNS answer
  (DNS rebinding) can't swap in 127.0.0.1. TLS still checks the certificate against the name, and the `Host` header
  carries the name.
- ZhiFox follows redirects itself, at most 5, and checks every hop from scratch: a public site can't bounce the request
  to `http://169.254.169.254/`.
- It doesn't go through `HTTP(S)_PROXY`: behind a proxy, the proxy resolves and connects, and the check would mean
  nothing.
- Limits, in [config.py](../../harness/config.py): 5 MB downloaded (counted after decompression, so a gzip bomb stops there),
  20 seconds for the whole fetch including redirects (a watchdog cuts off a server that sends one byte at a time),
  30,000 characters handed to the model (with a note saying how much was left out), and the per-message cap shared
  with Claude's web search (`HARNESS_MAX_SEARCHES`, 5 by default). The request is a plain `GET` with
  `User-Agent: ZhiFox/<version>`; no `Accept-Language` either, since that says something about you.

**Asking you.** An address can carry data out: a model misled by a page could fetch `evil.example/?data=your mail`.
So the first fetch from each site in a conversation asks you, and the card shows the full address. Once allowed, that
site doesn't ask again in the conversation (a redirect to another host doesn't allow that host); a new conversation
asks again. Allow everything doesn't ask; Allow read-only commands and Allow file edits still ask, because this sends
something out rather than reading your computer. Scheduled tasks and chat channels follow their own approval mode.
Text in the page that looks like a key is masked like any other tool result, and rewinding a turn counts a fetch as
"only looked".

## Browser use

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
- A note on what it costs (see [Cost](cost.md)).

The switch takes effect from the next conversation (a conversation that hasn't started yet picks it up right away).
Then ask for something like "Open the Python docs and tell me what's new in 3.13". While the assistant works:

- The top bar shows **ZhiFox is controlling the browser** (or **ZhiFox's browser is open** between steps) with a
  **Stop** button, which aborts every browser action in progress and closes the browser.
- Each browser step's tool card carries a small screenshot; click it for the full-size image. Full-size images are
  kept for 30 days in `data/browser/shots/`; after that the small one stays in the conversation.
- Approval cards say which site it wants to open, which field it will type into and what (a password only as dots),
  or which button may submit something (see [Permissions](tools-and-permissions.md#permissions)).

If a job needs an account, you can sign in yourself in ZhiFox's browser window (with **Hide the browser window** off);
the login stays in the profile until you clear it. Think before you do: from then on, a model misled by a page could
act as you on that site, within the approval rules. Custom agents get the browser with `tools: Browser`. The terminal
UI follows the same setting and asks its approvals in the terminal; it can't draw screenshots, so it only notes that
one was saved.

## Browser use: a Chrome of its own

Web search finds pages, but some jobs need the page itself: search a site, click through its results, fill in a form,
look at a layout. For that the assistant can drive a browser. Three decisions shape it: which browser, how ZhiFox talks
to it, and what the assistant may do there without asking (that last one is in [Permissions](tools-and-permissions.md#permissions)). The code
is in [harness/browser/](../../harness/browser/), one file per concern:

| File | What it does |
|---|---|
| [chrome.py](../../harness/browser/chrome.py) | Find Chrome (or Chromium, Edge, Brave), start it with ZhiFox's own profile, close it together with all its child processes |
| [cdp.py](../../harness/browser/cdp.py) | A hand-written Chrome DevTools Protocol client over a pipe: requests and replies matched by id, events, timeouts, cancelling one owner's requests |
| [manager.py](../../harness/browser/manager.py) | The one Chrome everybody shares: start it on first use, close it when idle and on exit, refuse downloads, dismiss page dialogs |
| [session.py](../../harness/browser/session.py) | One run's browser (`BrowserSession`): its own tabs, the sites allowed in this conversation, Stop, the events Chrome reports on its reader thread, and wrapping up each action's result. The actions are split by section into the files below, mixed into it |
| [session_tabs.py](../../harness/browser/session_tabs.py), [session_page.py](../../harness/browser/session_page.py) | Opening, attaching to and picking a tab, and the four tab actions; calling page.js, waiting for the page to settle, screenshots |
| [session_look.py](../../harness/browser/session_look.py), [session_input.py](../../harness/browser/session_input.py), [session_facts.py](../../harness/browser/session_facts.py) | Going to a page and reading it (navigate, screenshot, zoom, read_page, find, wait…); mouse and keyboard (click, drag, scroll, type, key, form_input); the look before acting that permissions.py decides on |
| [page.js](../../harness/browser/page.js) | The script put into each page: it turns what's on screen into numbered lines the model can read |
| [urls.py](../../harness/browser/urls.py), [keys.py](../../harness/browser/keys.py), [shots.py](../../harness/browser/shots.py) | Which addresses may be opened and what counts as one site; key names into keyboard events; screenshots on disk |
| [tools/browser.py](../../harness/tools/browser.py) | What the model sees: Anthropic's browser use toolset for Claude, twelve hand-written tools for the others |
| [webui/browser_api.py](../../harness/webui/browser_api.py) | **Settings → Browser**, the header indicator's Stop, the profile folder and the full-size screenshots |

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

**On Windows** there are no numbered descriptors to hand over. Chrome has a switch for that,
`--remote-debugging-io-pipes=<read>,<write>`, whose values are the two pipe handles. ZhiFox opens two pipes, marks only
Chrome's two ends as inheritable, writes their handle numbers into that switch, and starts Chrome with a `STARTUPINFO`
handle list that contains just those (plus the standard input and output), so no other handle ZhiFox has open leaks
into the browser. It is the same pipe and the same protocol, still with no port. The port alternative was rejected on
Windows for the same reason as elsewhere: while it is open, any program running as you, and any web page, can drive
the browser. Chrome and all its child processes are put in a Windows job object, so closing it (or ZhiFox crashing)
ends every one of them, the Windows counterpart of the process group used on macOS and Linux. Without Chrome, ZhiFox
uses Microsoft Edge, which every Windows 10 and 11 has.

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
`left_click`, `type`, `form_input`, `screenshot`, the tab actions…). It is the second kind of tool in [Three kinds of tools](tools-and-permissions.md#three-kinds-of-tools): Anthropic
wrote the spec and trained the model on it, and the actions run on your computer. Anthropic also scans the page content
sent back to the model for prompt injections and warns the model when it finds one. The toolset has a few rules of its
own ([claude.py](../../harness/providers/claude.py), [agent.py](../../harness/agent.py)): a result repeats the call's
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

Computer use of the whole desktop (screenshots of and control over your entire screen, not just one browser) is a
separate, advanced option with much stricter rules; see the next section.

The tunables are in [harness/config.py](../../harness/config.py): `BROWSER_VIEWPORT` (1280×800, also the screenshot size),
`BROWSER_SHOT_QUALITY` (JPEG, 60), `BROWSER_IDLE_MINUTES` (10), `BROWSER_SHOT_DAYS` (30), `BROWSER_CALL_TIMEOUT`,
`BROWSER_NAV_TIMEOUT`, `BROWSER_MAX_PAGE_CHARS` (50,000 characters per read) and `BROWSER_TOOLSET_MODELS`.

## Computer use

Computer use is an advanced option and off by default; read
[Computer use: your whole desktop](#computer-use-your-whole-desktop-advanced) before turning it on. The safest setup is a
virtual machine or a separate user account on your computer, signed in to none of your important accounts, with
ZhiFox running there. To turn it on, open **Settings → Computer use** and switch on **Let the assistant operate the
whole computer**. A panel explains the risks first; tick **I understand these risks** to confirm. The section also has:

- **Checks.** On macOS, whether Screen Recording and Accessibility are granted, and to which app (the one you started
  ZhiFox from: Terminal, iTerm, VS Code…), with the exact place in System Settings and an **Open System Settings**
  button that opens that pane (restart ZhiFox after granting). On Linux, whether `xdotool` and ImageMagick are
  installed, with the command to install them. On Wayland or without a display, why it can't work there.
- **Allow scheduled tasks and chat channels** (off by default). When on, a task run or a chat follows its own approval
  mode: each step waits for your approval, on the task's card or in the chat, unless that task or channel is set to
  **Allow everything**. The per-conversation "don't ask again" isn't offered there.
- **Blocked apps.** Names of apps ZhiFox must not screenshot or touch while they're in front, such as your password
  manager or your bank's app.
- Which providers can use it (Claude with Anthropic's toolset, GPT and Gemini with stand-ins, not DeepSeek), and what it
  costs (see [Cost](cost.md)).

The switch takes effect from the next conversation. Put the ZhiFox page and the app you want it to work in side by side,
then ask for something like "In TextEdit, write a short note that says hello and save it to the Desktop". While it
works:

- Every step shows an approval card. A screenshot's card has an extra button, **Allow, and don't ask about screenshots
  in this conversation**; the card for a click, a drag, a scroll, typing or a key has **Allow, and don't ask about
  clicks and typing in this conversation**, with a few lines on what that means. The two are separate choices, and
  each lasts for this conversation only; steps that then run unasked are marked **Allowed automatically (this
  conversation)**. To take it back, **Settings → Computer use** shows what this conversation has loosened, and **Ask at
  every step again** starts asking again (it only tightens, so it takes effect at once). Dangerous combinations (⌘Q on a Mac, Alt+F4 or Ctrl+Alt+Del elsewhere…) always ask and offer
  neither. Read the card: it says where the click lands and whose window that is, which app the keys go to, and warns
  about dangerous keys.
- In **Allow everything**, nothing asks except those dangerous combinations.
- The top bar shows **ZhiFox is controlling this computer** with a **Stop** button, which stops the step in progress
  and keeps the assistant off the computer for the rest of the turn.
- Each step's tool card carries a small screenshot; click it for the full-size image (kept for 7 days in
  `data/desktop/shots/`).

Custom agents get it with `tools: Computer`. The terminal UI follows the same setting and asks its approvals in the
terminal; it has no buttons for the per-conversation choices, so there only `/auto all` stops the questions (dangerous
combinations still ask).

## Computer use: your whole desktop (advanced)

Browser use stops at the edge of one browser that ZhiFox owns. Some jobs live elsewhere: a desktop app with no website,
a dialog in System Settings, a file to drag from Finder into another app. Computer use lets the assistant look at your
whole screen and work your mouse and keyboard. It is an advanced option, **off by default**, and it is the most
dangerous thing ZhiFox can do, so most of this section is about why it is built the way it is. The code is in
[harness/desktop/](../../harness/desktop/), one file per concern:

| File | What it does |
|---|---|
| [frame.py](../../harness/desktop/frame.py) | The three coordinate systems (the picture the model sees, screen points, screen pixels) and the conversions between them |
| [keys.py](../../harness/desktop/keys.py) | Key names the way models write them (`Return`, `ctrl+s`, `super`) turned into one form; which combinations are dangerous |
| [backend.py](../../harness/desktop/backend.py) | What a platform backend provides (screenshots, mouse, keyboard, which app is in front, bringing an app back to the front) and which one this computer uses |
| [macos.py](../../harness/desktop/macos.py) | macOS: `screencapture` and `sips` for screenshots, CoreGraphics `CGEvent` through `ctypes` for mouse and keyboard |
| [x11.py](../../harness/desktop/x11.py) | Linux on X11: `xdotool` for mouse and keyboard, ImageMagick (`import`, `convert`) for screenshots |
| [session.py](../../harness/desktop/session.py) | One run's computer use: coordinate mapping, the last look before acting, the conversation's "don't ask again" for screenshots and for clicks and typing (`trust_screenshots`, `trust_actions`), Stop |
| [manager.py](../../harness/desktop/manager.py) | One screen shared by everybody: only one run controls it at a time; the header indicator; the settings |
| [tools/computer.py](../../harness/tools/computer.py) | What the model sees: Anthropic's computer use toolset for Claude, nine hand-written stand-ins for GPT and Gemini |
| [webui/computer_api.py](../../harness/webui/computer_api.py) | **Settings → Computer use**, the indicator's Stop, the per-conversation "don't ask again" (screenshots, or clicks and typing), opening System Settings, full-size screenshots |

**The loop: screenshot, action, screenshot.** The model can't see your screen and can't touch your mouse. All it can
do is look at a picture and say what to do next, so the loop goes like this:

```
model: screenshot                              → ZhiFox captures the screen, shrinks it, hands it over
model: left_click {"coordinate": [665, 432]}   → pixels on that picture: ZhiFox converts them, asks you, clicks
model: screenshot                              → did it work?
… until the model thinks it's done
```

It is the same agent loop as everywhere else in ZhiFox; only what is seen and touched changes. Claude often sends a
short batch in one reply (click, type, screenshot). The calls run in order, each one asks you (unless you've chosen
otherwise, see below), and when one fails or
you deny it the rest of the batch isn't run and gets exactly `Not executed: an earlier computer action in this turn
failed.` (the computer toolset's own wording, one word longer than the browser's). The stand-ins for other models follow
the same rule, so a denied click is never followed by typing into whatever happens to have the focus.

**Coordinates: three systems.** A screenshot has to be shrunk before it goes to the model: images have size limits,
and every extra pixel costs tokens. ZhiFox scales it so the long edge is at most 1,568 pixels and the whole picture at
most 1.15 megapixels (the limits in Anthropic's own example; every provider accepts them). The model then answers in
pixels of *that* picture, so each coordinate is scaled back up before ZhiFox clicks. A Mac with a Retina display adds a
second layer: mouse events use *points*, while a raw screenshot has 2×2 pixels per point. For a MacBook screen of
1512×982 points:

```
screen:      1512 × 982 points   (3024 × 1964 pixels in a raw screenshot)
model sees:  1330 × 864          (scale 0.88, chosen on points, so a 1280×800 screen goes over 1:1)
model says:  (665, 432)
→ points:    (665 × 1512/1330, 432 × 982/864) = (756, 491)   ← where the mouse event goes
→ pixels:    (1512, 982)                                      ← the same spot in the raw capture (×2)
```

x and y are scaled separately, because rounding the shrunk size (1330.65 becomes 1330) makes the two ratios differ
slightly, enough to miss by a point or two near the bottom-right corner. Get this wrong and every click misses in the
same direction, the first symptom in the troubleshooting table of Anthropic's documentation. `zoom` crops a region at
full resolution and scales it to fit the usual screenshot size (at most 4×), but coordinates stay in the full
screenshot's space. If the screen size changes in the middle of a conversation (a new resolution, a display plugged in),
coordinates from the old picture are refused and the model is asked for a new screenshot. Only the main display is used.

**Why it is riskier than browser use.** Three differences:

- The browser is ZhiFox's own, signed in to nothing. The desktop is *yours*: your mail, your chats, your password
  manager, your bank's app.
- Any text on the screen can be aimed at the model: an email, a web page, a notification that pops up. A model misled
  by it can click and type anywhere on your computer.
- In the browser, ZhiFox reads the page's structure and can tell that a button submits a form. On the desktop there are
  only pixels, so ZhiFox can't judge what a click will do; only you can.

Anthropic's documentation recommends running computer use in a dedicated virtual machine or container with minimal
privileges, and asks applications to explain the risks and get consent first. ZhiFox does both.

**The safety model.**

- **Off by default, on deliberately.** Switching it on in Settings first opens an explanation of the risks, in place,
  with the recommendation to run ZhiFox in a virtual machine or a separate user account; the switch only takes effect
  after you tick **I understand these risks**. The server refuses to enable it without `acknowledge: true`.
- **By default, every step asks.** In **Ask every time**, **Allow read-only commands** and **Allow file edits**, every
  computer step asks: those modes loosen commands and files, not your screen. Only `wait` and `cursor_position` run
  without asking, because they neither look nor touch.
- **Loosening is your choice, per conversation, and only where you're watching.** Asking at every step sounds safest,
  but the model takes a screenshot after nearly every action and a simple job is dozens of clicks; asked that often,
  people end up clicking Allow without reading, and then the question protects nothing. So the approval card in the web
  chat offers two separate choices. A screenshot's or zoom's card has **Allow, and don't ask about screenshots in this
  conversation**. A card for a click (any button, double or triple), a drag, a mouse move, pressing or releasing the
  mouse button, a scroll, typing, a key or a held key has **Allow, and don't ask about clicks and typing in this
  conversation**, and under it the card spells out what that means: from now on ZhiFox clicks and types without asking,
  any text on the screen could steer the model, dangerous shortcuts still ask, and the Stop button at the top is always
  there. Each choice is remembered only on that conversation's own `DesktopSession` (`trust_screenshots`,
  `trust_actions`), because you gave it for one job you're watching, not for whatever comes next: a new conversation
  asks again. Subagents working inside that conversation share its screen session, so the choice covers them too. The
  terminal UI has no buttons, so there only **Allow everything** loosens it.
- **Allow everything means everything.** Wherever the approval mode in effect is **Allow everything** (the chat's mode,
  a scheduled task's own setting, a chat channel's), computer steps, screenshots and actions alike, run without asking.
  You choose that mode deliberately, past its warning, so ZhiFox doesn't overrule it here. Tasks and channels still
  need their own switch before they get computer tools at all (below).
- **What never loosens.** These hold in every mode and whatever you've allowed in the conversation, because they guard
  against what a single wrong step can't undo or what nobody checks once the questions stop:
  - Dangerous key combinations (`DANGEROUS` in [keys.py](../../harness/desktop/keys.py), per system) ask every time, with a
    warning on the card and with neither the "don't ask again" button nor the **Don't want to be asked every time?**
    link: on a Mac ⌘Q, ⌘⌥Esc, ⌘⇧Q (log out), ⌃⌘Q (lock the screen), ⌘W, ⌘⌫ and ⌘⇧⌫ (Finder's move to Trash and
    empty Trash); on Linux Alt+F4, Ctrl+Alt+Backspace, Ctrl+Q, Ctrl+W, Super+L; Ctrl+Alt+Del everywhere; and more.
    One of them can lose unsaved work, log you out or empty the Trash in a single keystroke.
  - Blocked apps are refused without asking (below): that list is your own hard line.
  - The last look before acting (below) still runs: keys go only to the app that was in front, and a click whose spot
    now shows another window isn't done. When nobody approves each step, this matters more, not less.
  - The **ZhiFox is controlling this computer** indicator and its **Stop** stay.
  - Every step that ran without asking is recorded: its tool card in the conversation says **Allowed automatically**
    (**Allowed automatically (this conversation)** when it ran on the conversation's choice).
- **The card says where.** The coordinates on the screenshot and on the screen, whose window is under the click
  (macOS), which app the keystrokes will go to, the keys and the text, and the warning for a dangerous combination.
  Text with a line break warns that a newline often means Enter (send, submit).
- **A last look before acting.** Clicking Allow on the ZhiFox page brings your browser to the front, so anything typed
  next would land in ZhiFox's own page. Keyboard actions (`type`, `key`, `hold_key`) therefore first bring back the app
  that was in front when ZhiFox asked, and refuse when they can't. Mouse actions check that the same app's window is
  still under the point; if something else covers it now (usually the ZhiFox page, raised by your own click), the step
  isn't done and the model is told to take a new screenshot. Keep the ZhiFox page side by side with the app it works in.
- **Blocked apps** (optional). A list of app names in Settings. When one of them is in front (or, on macOS, under a
  click), ZhiFox neither takes a screenshot nor acts, and doesn't ask. Names match without regard to case (on Linux,
  against the active window's class and title). A blocked app peeking out from behind another window can still show up
  in a screenshot.
- **Scheduled tasks and chat channels** may use it only when you also switch on **Allow scheduled tasks and chat
  channels** (off by default). Even then each step follows that run's own approval mode: it waits for your approval
  through the run's own route (the task's card, or the chat) unless that task or channel is set to **Allow
  everything**. They never get the per-conversation choice: nobody is necessarily watching the screen while a task runs
  at 3 a.m. or while you type into a chat on your phone, so their screen session isn't marked as attended, no card
  offers the button, and the server has no route that sets it for them.
- **One screen, one controller.** The first run to act claims the screen until its turn (or task run) ends. Another run
  trying in the meantime is told the computer is busy. A browser can give each run its own tabs; a screen can't be
  split.
- **Stop.** Whenever a run holds the screen, the header shows **ZhiFox is controlling this computer** with a **Stop**
  button. The action in progress stops at once (a hold or a drag still releases its key or mouse button), and that run
  can't use the computer again for the rest of its turn. A mouse button left pressed by `left_mouse_down` is released
  when the turn ends.
- **A record of every step.** Each action is a tool card with a small screenshot taken after it, drawn for you and
  never sent to the model. The full-size images are kept in `data/desktop/shots/` for `DESKTOP_SHOT_DAYS` (7) days,
  shorter than the browser's 30, because a picture of the whole screen can catch anything.
- In the result of its first screenshot the model is told which system it is on (⌘ shortcuts on macOS) and not to click
  ZhiFox's own page or change ZhiFox's settings. That depends on the model obeying; what actually protects you is that
  such a click asks you, unless you've chosen not to be asked, which is exactly the risk the card spells out when you
  do.

**Platforms.**

- **macOS.** Screenshots come from `screencapture -x -m` (no sound, main display) and are scaled and turned into JPEG by
  `sips`; mouse and keyboard are CoreGraphics `CGEvent`s posted through `ctypes`, so nothing new is installed. (Scrolling
  uses `CGEventCreateScrollWheelEvent2`, because the older function takes a variable number of arguments, which
  `ctypes` can't pass correctly on Apple silicon.) Which app owns which window comes from `CGWindowListCopyWindowInfo`;
  app names need no permission. Bringing an app back to the front goes from its process id to its `.app` and hands that
  to `open`, like clicking its Dock icon; AppleScript's `activate` would need the Automation permission. Two permissions
  are needed, and you grant them yourself: **Screen Recording** (System Settings → Privacy & Security → Screen & System
  Audio Recording; "Screen Recording" on older macOS) and **Accessibility** (System Settings → Privacy & Security →
  Accessibility). They belong to the app that launches ZhiFox: Terminal, iTerm, VS Code… ZhiFox only *checks* them
  (`CGPreflightScreenCaptureAccess` and `AXIsProcessTrusted`, which never show a dialog), never requests them or changes
  any setting, and doesn't even try a capture without the permission (`screencapture` would ask on its own). Settings
  shows each one as granted or missing, which app to grant it to, and an **Open System Settings** button that only
  opens that pane. Restart ZhiFox after granting.
- **Linux on X11.** Needs `xdotool` and ImageMagick (`import` and `convert`, or `magick`; `scrot` also works for the
  capture): `sudo apt install xdotool imagemagick`. ZhiFox detects them and says what's missing, but never installs
  anything. X11 needs no permissions, because programs on the same display can already capture and drive each other.
  Blocked apps are matched against the active window only: `xdotool` can't name the window under an arbitrary point.
- **Wayland: not supported.** Wayland was designed so that one program can't capture other programs' windows or send
  them input, the exact thing computer use does (XWayland doesn't help: `xdotool` can't reach native Wayland windows).
  Settings says so and suggests logging in to an X11 (Xorg) session. Windows isn't supported yet: Settings and the capability list say "Controlling the whole computer isn't supported on Windows yet".

**Two sets of specs, again.** Claude models in `COMPUTER_TOOLSET_MODELS` get Anthropic's
[computer use toolset](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool),
`computer_toolset_20260801`: generally available, no beta header, and one entry in `tools` with no name and no display
size (coordinates are always in the pixels of the screenshots ZhiFox sends back). It stands for 17 members:
`screenshot`, `zoom`, `left_click`, `right_click`, `middle_click`, `double_click`, `triple_click`, `left_click_drag`,
`mouse_move`, `left_mouse_down`, `left_mouse_up`, `cursor_position`, `scroll`, `type`, `key`, `hold_key` and `wait`.
The model calls a member by name with `toolset_name: "computer"`, every result repeats that `toolset_name`, and only
`screenshot` and `zoom` return an image. All three Claude models ZhiFox offers (Opus 5.5, Sonnet 5.5, Fable 5.2) support
it, and on the Claude API they accept *only* the toolset: the older `computer_20251124` tool is rejected with a 400. It
adds about 4,500 input tokens to every request (cached with the rest of the start), which is why its tools are only in
conversations started with computer use on. GPT and Gemini get nine hand-written function tools instead:
`computer_screenshot`, `computer_zoom`, `computer_click`, `computer_move`, `computer_drag`, `computer_scroll`,
`computer_type`, `computer_key` and `computer_wait`. Their screenshots go to the model as images in a user message
right after the tool results, because the Chat Completions format allows only text in a tool result. These models
weren't trained for it and click less precisely. DeepSeek can't take images at all, so it gets no computer tools (each
of them is marked `Tool.vision`, and the adapter leaves them out for a provider without image input): a model that
can't see the screenshot would be clicking blind. Settings says which providers can use it and how. A Claude model
outside the list would get the stand-ins.

**Frozen for a conversation, a session per run.** Like the browser tools, the computer tools belong to the
conversation's fixed start. Switching computer use on mid-conversation takes effect from the next conversation;
switching it off keeps the tools (the specs mustn't change), stops any action in progress, and answers further calls
with "off". A reopened conversation that used computer use keeps its tools, so its history still matches the specs.
Each run (your conversation, a task run, a channel conversation) has its own `DesktopSession` with its own "don't ask
again" choices (only the web chat's session is attended, so only it can get them), its own Stop and its own idea of
which screenshot the coordinates refer to.

**What it shares with browser use, and what it doesn't.** Shared: the toolset-versus-stand-in pattern (`Tool.toolset`,
`member`, `stands_in_for`), the per-conversation snapshot of tools, one session per run, the halt-on-failure batches,
looking at what a call will touch before asking (`FactsTool`), the screenshot store (`ShotStore`, in its own folder) and
its endpoint pattern, and the Stop wiring. Kept separate: the permission rules (much stricter), the settings, the
single-screen claim and a shorter screenshot retention.

**The web endpoints** ([harness/webui/computer_api.py](../../harness/webui/computer_api.py)), all behind the page's token like
every other request: `POST /api/computer/settings` (on and off, tasks and channels, blocked apps; switching it on
requires `acknowledge: true`, which the page sends only after you tick the box), `/api/computer/stop` (the indicator's
Stop), `/api/computer/trust` (`{"what": "screenshots" | "actions", "confirm": <id>}`: stop asking about screenshots, or
about clicks and typing, in the conversation you're looking at; it works only while computer use is on and only while
the web chat is waiting on that very approval card, and only for the word that card offered, so the choice is always
tied to a card you saw, and it never reaches a task or a channel), `/api/computer/untrust` (Settings' **Ask at every
step again**: takes back both "don't ask" choices for this conversation; it only tightens, so no card is needed),
`/api/computer/permission` (macOS only; the page sends `screen` or `input` and the server opens that System Settings
pane) and `/api/computer/shot` (a full-size screenshot by its 64-hex-character id only, answered as `image/jpeg` with
`Content-Disposition: attachment`, `nosniff` and `Content-Security-Policy: default-src 'none'; sandbox`, like the
browser's).

The tunables are in [harness/config.py](../../harness/config.py): `DESKTOP_MAX_EDGE` (1,568), `DESKTOP_MAX_PIXELS`
(1,150,000), `DESKTOP_SHOT_QUALITY` (JPEG, 70), `DESKTOP_THUMB_WIDTH` (320), `DESKTOP_SHOT_DAYS` (7),
`DESKTOP_SETTLE_SECONDS` (0.4), `DESKTOP_SCROLL_LINES` (3, macOS), `DESKTOP_MAX_WAIT` (30 seconds per `wait` or
`hold_key`), `DESKTOP_COMMAND_TIMEOUT` (15), `DESKTOP_MAX_TYPE_CHARS` (2,000 characters per `type`) and
`COMPUTER_TOOLSET_MODELS`.
