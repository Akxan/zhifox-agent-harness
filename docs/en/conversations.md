**English** | [简体中文](../zh-CN/conversations.md)

[ZhiFox](../../README.md) › [Documentation](../../README.md#documentation) › Conversations

# Conversations

## How conversations are saved and restored

The model remembers nothing, so "continuing where you left off" is entirely the harness's job: it saves the last
conversation's history and sends it along with the next request. Each conversation is one JSON file in
`data/conversations/`, holding two copies of the content:

| Field | Used by | Notes |
|---|---|---|
| `messages` | The model | The history sent to the model, stored exactly in that provider's format. Handed back to the adapter (`restore`) when you open the conversation |
| `events` | The page | What the interface showed. Sent to the browser again when you open the conversation, so the page can redraw it |

Both interfaces save the same file the same way, after every turn. The terminal has no use for `events` itself (what it
prints is gone once printed), but it records them anyway, with the class the web UI uses (`WebUI` in
[webui/ui.py](../../harness/webui/ui.py)) and the same clean-up before saving. A conversation from the terminal therefore
opens in the web page as if it had happened there: your messages, the replies as Markdown, tool cards, approvals and
their outcome, the run trace, the **Changed N files** line. Only what a turn shows is recorded: the terminal's own
commands (`/help`, `/usage`…) aren't part of the conversation.

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

## Editing the last message and regenerating

Both are the same move: rewind the conversation a little, then run the turn again. **Regenerate** rewinds to just
after your last message. That message stays exactly as it was, with its time note and attachments, so the new request
starts byte for byte like the old one (and hits the cache); everything the model did in the turn (thinking, text, tool
calls and their results) is removed. **Edit** rewinds to just before your last message and sends the edited one as a
new turn.

Only the last turn can be rewound, because the history is append-only (see [The loop](architecture.md#the-loop)). Changing a turn in the middle would
invalidate the cache for everything after it, and every later Claude thinking block is signed against what came
before it. Cutting from the end leaves the prefix of every remaining block untouched.

**Where to rewind to.** When a turn starts, the web UI (and the terminal) records the adapter's position in the history
(`Backend.mark()`) on the turn's `user` event, and it is saved with the conversation, so `/retry` and `/edit` in the
terminal and **Regenerate** and **Edit** on the page work on a conversation whichever one it came from. GPT, Gemini
and DeepSeek count positions from the start of the conversation, including the messages compaction has replaced; the conversation file
records how many were replaced (`dropped`), so the recorded positions still match after reopening or restarting.
Before rewinding, the adapter is asked whether that position still holds the turn's user message (`Backend.turn_at`):

- It does: rewind there.
- It doesn't, and the history ends right there: the turn failed before the model answered, and the message was taken
  back (the page still shows it). Regenerate sends it again; its attachments come back from the media library.
- It doesn't, and the history goes on: the turn has been folded into a summary. It can't be rewound, and the buttons
  say why. Turns saved before this feature, and the turn a scheduled task ran by itself (the model got an instruction
  that differs from what the page shows), can't be rewound either.

**What can't be undone.** Only the conversation goes back. If the turn ran a command, edited a file, called an MCP
tool, operated the whole computer, or clicked or typed in the browser (directly or through a subagent), the page lists
those steps and asks once. When the whole computer or the browser was operated, the confirmation says so in a line of
its own: what was done in other apps, or submitted and sent on a web page, can't be taken back. Looking doesn't count
(screenshots, reading a page, opening an address; on the whole computer only screenshots, zooming in, asking where the
pointer is and waiting). Steps you denied don't count, and neither do failed calls to ZhiFox's own to-do, file, memory
and task tools: they check everything before writing, so a failure changed nothing. A tool ZhiFox doesn't recognize
counts as a change.

**Money.** What was spent stays spent: the spending record keeps every call, and this conversation's usage and cost in
the side panel aren't reduced. The context size updates with the next request.

**On the page.** The server truncates the history and the saved events together, saves the conversation straight away
(so search and the media library follow), then sends `reset` and replays the events that remain, the same way it
opens a conversation. Every open tab redraws in place without reloading. A request names the conversation and the
turn it means, so a tab that is behind is turned away instead of rewinding something else, and a tab that was in the
middle of an edit puts the unsent text back into its input box.

## Forking a conversation from a turn

Rewinding only reaches the last turn. To take a conversation in another direction from turn 3, fork it: the original
stays byte for byte as it was, and a new conversation picks up from that turn. Every finished turn has two **Fork**
buttons:

- **Fork from here (keep this turn)**, next to **Copy** under a reply: the new conversation holds everything up to the
  end of that turn. Whatever you say next takes it somewhere new.
- **Fork from this message (edit and resend)**, beside your message on hover (left of **Edit** on the last one): the new
  conversation stops just before that turn, and the message (text and attachments) is put back in the input box to
  change and send again. Forking at the first message gives an empty conversation that, like a new one, is only saved
  after its first turn.

The new conversation opens at once. Its title ends in "(fork)", a small fork icon marks it in the sidebar, and a line at
the top says where it came from; **Open the original** goes back and scrolls to that turn (once the original is deleted,
only the text is left).

**Why it doesn't break append-only.** Nothing in either history is edited. The new conversation's `messages` are a
prefix of the original's, cut where a turn starts, which is the same thing rewinding does: every block that remains has
exactly what came before it, so the cache still matches and Claude's thinking signatures still verify. It keeps the
provider, the model and the custom agent, and from then on it only appends.

**Where the cut goes** ([harness/fork.py](../../harness/fork.py)). It uses the positions recorded on `user` events (see
above): forking from a message cuts at that turn's own position, forking from a reply at the next turn's position (or
takes the whole history at the last turn). Whether a position still starts a turn is answered by the adapter's own rule
(`providers.saved_before`, the same bookkeeping as `Backend.turn_at`). For GPT, Gemini and DeepSeek, a cut inside the
part already folded into a summary is refused, as with rewinding, and those buttons are greyed out with the reason; a
cut after the summary keeps the summary and carries `dropped` over, so the new conversation's recorded positions match
too (you can edit or regenerate its last turn). A turn without a recorded position (one a scheduled task ran by itself,
or one saved by an older version) can't be forked. `events` are cut at the same place, so a turn's run trace, subagent
cards and approvals go with it.

**What stays behind.** The record of file changes (the **Changed N files** line and its undos) isn't copied: the
backups under `data/backups/turns/` are keyed by turn and there is only one copy of each file on disk, so if both
conversations could undo the same turn, an undo in one would leave the other claiming the files were never undone.
Undo stays with the original, and the new conversation's top line says so when those turns changed files. A fork of a
scheduled task's or a chat channel's conversation is an ordinary web conversation (no `task` or `channel` mark), so it
has no `send_file`. Money already spent stays spent; the new conversation's context size is estimated from its last
run trace.

**On the page.** Forking switches conversations, so it waits until your own turn is over. It only reads the original
from disk (under the conversation store's lock, so a half-written file is never seen) and doesn't claim it, so a
conversation a chat channel is using can be forked too. The request names the conversation and the turn, so a tab that
is behind is turned away. Forking is a web UI feature; a conversation saved from the terminal forks there like any
other.

## Undoing a turn's file changes

Rewinding brings back the conversation; this brings back the files. Every turn in which the file tool created, edited
or overwrote files ends with a compact line, **Changed 2 files +7 −1**. Its **Undo** button undoes the whole turn in
one click. Expanded, it lists each file (added, modified or overwritten, lines added and removed); click a file name for
a line-by-line preview of what the turn did, and undo files one at a time or **Undo all changes from this turn**.
**Redo** puts back what an undo replaced. In the terminal, `/undo` lists the latest turn's changes and asks before
undoing them. The terminal saves the same `file_changes` and `file_undo` events, so a terminal turn shows its line in
the web page too, and `/undo` knows the changes of a conversation opened with `/open`.

**How it's recorded** ([harness/undo/](../../harness/undo/)). When a turn starts, the web UI and the terminal open a journal
for it; the file tool asks before each write whether a journal is open (one line in
[tools/files.py](../../harness/tools/files.py)) and, on the first touch of a file in that turn, keeps a copy of how it was
(or notes that it didn't exist) in `data/backups/turns/<id>/`, then records how the turn left it. The journal lives on
the turn's thread (a `ContextVar`): your chat, scheduled tasks and chat channels run at the same time and share one file
tool, and only your chat turns are recorded (work handed to a subagent counts, since it runs on the same thread). At the
end of the turn a manifest is written once and never changed, and a `file_changes` event goes into the conversation's
events. The history sent to the model (`messages`) isn't touched. Limits live in `config.py`: 10 MB per file, 50 MB and
100 files per turn, kept for 30 days. A file over a limit is still edited; it just can't be undone, and the line says why.

**What isn't recorded.** Files changed by `run_command`. What a command changes can't be seen: `sed -i`, `git
checkout`, `npm install`, a script. Tracking it would mean snapshotting the whole working folder around every command,
slow in a big project, and a command can change things outside the folder too. So the line says plainly that files
changed by commands can't be undone automatically, and says it more loudly when the turn ran a command. ZhiFox also
doesn't make git snapshots of your repository: that would leave entries in your stash or commit history, mix with a
rebase or your staging area, still miss ignored files, and need git everywhere. If you use git, a commit of your own
is the most reliable undo point.

**Undo and redo** ([restore.py](../../harness/undo/restore.py)). Undo restores each file to its exact bytes and permissions
from before the turn and deletes files the turn created (and folders it created for them, once empty). It first keeps a
copy of the file as it is now, and **Redo** puts that copy back. Before writing anything, every file is checked:

- **Changed since?** Compared by content hash (not modification time). If the file changed after the turn (a later
  turn, you, or another program), or a later turn in the same conversation also changed it, the line explains it in
  place (for later turns: undoing restores the state from before this turn, and the later turns' changes go too) and
  waits for **Undo anyway**. Files outside the working folder, or in ZhiFox's own data folder, ask the same way.
- **Never written:** the files that hold keys or permission settings (the file tool's protected list), the backups
  folder itself, a path that has since become a link (undo writes only the location it recorded), and backups that are
  missing or don't match their hash.

Checking and writing hold the same lock as the file tool, so another conversation or a task can't slip in between.
Undo is only for you, from the page or `/undo`: the model has no tool for it. Each undo or redo is itself an event
(`file_undo`), saved with the conversation and replayed on refresh: the state of every file is worked out from those
events, and the manifest only records what the turn did. One fact lives in one place.

**Telling the model.** The model remembers the files it changed. After an undo it would either fail to find its text,
or rewrite the whole file from memory and put the undone change back. So the next message in that conversation carries
a short `<file_changes>` note listing what was undone or redone and asking it to look again before relying on them. It
rides on the message like the time and the Skill list, so the system prompt and the history stay as they are; it counts
as said only once that message stays in the history, and notes about a turn you then rewind are dropped (the model
never saw that turn).

**With rewinding.** When the last turn changed files, the rewind confirmation offers **Also undo the files this turn
changed** (unticked by default). Ticked, the files are undone first; if any changed since, the card explains it and the
button becomes **Undo and regenerate anyway**. Undos of earlier turns made after the last one survive rewinding it.

## Search

Search has two jobs: find things fast, and find them at all. The code is all in
[harness/search.py](../../harness/search.py), with no search library.

**Fast.** Search can't reread every conversation file on each keystroke. The web UI already reads every conversation
file once to list the titles (the first time the list or search is needed), and while doing so it extracts the
searchable text and keeps it in memory. That is the index. From then on, saving, renaming and deleting a conversation
updates the index, and searches run purely in memory.

The index holds: titles, your messages, the assistant's replies, its thinking, the one-line summary of each tool call
(the command that ran, the file path read or written, the web search query), and the first 600 characters of each tool
result. It leaves out the contents the assistant wrote into files, and error notices and notices such as "Stopped".

**At all.** People often remember only roughly what was said, so matching has five tiers, and stricter matches rank
higher:

| Tier | Rule | Example |
|---|---|---|
| Exact | The keyword appears as typed. Case-insensitive; full-width and half-width characters count as the same | `HARNESS` finds `harness` |
| Similar word | An English word of 5 or more letters may have a letter or two wrong. Used only when the word doesn't appear exactly anywhere | `hraness` finds `harness` |
| Characters in order | Every character of the keyword appears in order, with a few others in between. English keywords need at least 4 letters and must stay within one word; Chinese keywords need at least 2 characters | `周五报告` finds "周五下午交报告" |
| Pinyin | A keyword made of letters is read as pinyin: at least two whole syllables, matching Chinese characters next to each other. Case, tones and spaces or apostrophes between syllables don't matter, and every reading of a polyphonic character counts | `baogao`, `bao gao` and `BaoGao` find "报告"; `yinhang` finds "银行" and `xingzou` finds "行走" (行 reads both) |
| Initials | Each letter is the first letter of a character's pinyin (`zh`, `ch`, `sh` count as `z`, `c`, `s`). At least 3 letters, and only in titles | `zbmb` finds a conversation titled "周报模板" |

Separate keywords with spaces. All of them must match, in any order, and they may match in different places. Ranking
looks at the tier first: conversations where every keyword matched exactly come first, then those that needed a similar
word, then characters in order, then pinyin, and initials last. Within a tier, matches in the title rank above matches
in the content, and matches in your messages rank above matches in tool output. When relevance is equal, newer
conversations come first.

**Pinyin.** Many people type Chinese with a pinyin keyboard, so `baogao` (报告, "report") is quicker to type in the
search box than the characters. The two pinyin tiers have to stay precise, because a string of letters can also be an
English word, and they come with a few rules:

- They are tried only when the keyword doesn't appear as typed anywhere, like similar words. `name` splits into na + me
  (那么, a very common word), but if some conversation literally contains `name`, you are looking for the English word.
- They rank after the first three tiers, so everything the search found before still appears, in the same order. Pinyin
  matches are added after them.
- At least two syllables: one syllable such as `bao` fits dozens of characters, and one-syllable English words like
  `bang` or `long` would match Chinese text everywhere. Only whole syllables count, so `baog` doesn't find 报告 yet.
- A lone `a`, `o` or `e` after another syllable needs an apostrophe, as in standard pinyin (`tian'e` for 天鹅). Without
  that rule `lie` would be read as li + e. Other syllables that start with a vowel don't need one: `xian` finds 西安 and
  `fangan` finds both 方案 (fang'an) and 反感 (fan'gan).
- Initials need at least three letters and look only at titles. Two letters match too much (`bg` fits 不过, 不管,
  宾馆…), and in long text even three letters match nearly every conversation (`zgr` fits every 这个人). Titles are short,
  and they are what people remember.
- Keywords that are all letters are also tried joined as one phrase, with the spaces as syllable boundaries, when no
  conversation contains them all as typed: that is how `bao gao` finds 报告.

ZhiFox doesn't store pinyin for each conversation, since for thousands of long conversations that would take more
memory than the text itself. It turns the keyword into a regular expression instead, with each syllable replaced by
the set of characters that have that reading: `baogao` becomes `[包保报宝抱…][高告搞稿糕…]`. Python runs it in C, as fast
as the characters-in-order tier: tens of milliseconds for ten million characters. When the letters split more than one
way (`xian` is 先 or 西安), each split becomes a branch of the same expression. The readings come from the Unicode Han
Database (Unihan): a table of about 44,000 characters in
[harness/search_pinyin.txt](../../harness/search_pinyin.txt) (155 KB), generated once by
[tests/make_pinyin.py](../../tests/make_pinyin.py) (see [Third-party data](../../README.md#third-party-data)) and read only by the first
search that needs pinyin. The matching itself is in [harness/search_pinyin.py](../../harness/search_pinyin.py).

The server computes where to highlight the matches in the results, and there is a trap here. Browser strings count in
UTF-16, where an emoji takes two units; Python counts it as one. So the server converts the positions before sending
them, or every highlight after an emoji would be off.
