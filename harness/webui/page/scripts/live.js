// ---- 几段对话同时进行：会话栏上的标记、后台那一段的"停止"、顶上那条"在等你确认"、提醒 ------------------
// 你正看着的那一段在跑时，打开别的会话、开新对话，它留在后台接着跑（见 harness/webui/sessions.py）。助手程序在状态里给：
//   live      会话编号 → running 在跑 / confirm 在等你确认 / error 在后台出了错 / done 在后台跑完了、你还没看
//   parallel  {max: 最多同时几段, running: 现在几段在跑}
// 这一块照着它们做四件事：
//   1. 会话栏上每一行的标记（liveTag）和后台那一段的"停止"（liveStop），会话列表画每一行时来取（见 conversations.js）。
//      你正看着的那一段不画：它就在眼前。
//   2. 主区域顶上那条提示（#chat-alert）：别的会话在等你确认时出现，点一下打开它（那张确认卡片就在里面，重放出来还在等你）。
//   3. 提醒：别的会话刚开始等你确认（最要紧）、刚跑完、刚出错时，页面在后台就弹系统通知（notify.js 的 systemNotify，
//      和对话里的通知是同一个开关、同一次授权）；页面就在眼前、你正看着别的会话时，等你确认的那一条在页面上也说一声。
//      标签页的标题和图标交给 notify.js（noteChatsWaiting、noteBackground）。只认实时的变化：刚连上时、换着看的那一下
//      收到的状态是"现在的样子"，不算新消息（和 notify.js 的"只给实时的事"是同一条规矩）。
//   4. 发消息之前看一眼上限（parallelFull）：满了不发，说明为什么（助手程序那边也会拒绝，规矩只在那边）。

let liveMarks = {};                       // 状态里的 live
let liveLimit = { max: 3, running: 0 };   // 状态里的 parallel
let liveSeen = null;                      // 上一份状态里的 live：比一比才知道哪一段"刚"开始等你。刚连上时是 null

const LIVE_WORDS = { running: k('正在进行'), confirm: k('等你确认'), error: k('出错了'), done: k('有新回复') };

// 这一行的标记。你正看着的那一段没有（它就在眼前）
const liveOf = (id) => (state && id !== state.conversation ? liveMarks[id] || '' : '');
const liveBusy = (mark) => mark === 'running' || mark === 'confirm';

// 除了你正看着的这一段，还有几段在跑
function othersRunning() {
  return Object.keys(liveMarks).filter((id) => liveBusy(liveOf(id))).length;
}

// 有没有哪一段在跑（你正看着的，或者后台的）：改记忆、改 Skill 这些要等它们都停下（助手程序那边的 WebApp._turn）
const anyRunning = () => busy || othersRunning() > 0;

// 你正看着的这一段（没在跑）现在发消息，会不会超过"最多同时几段"
const parallelFull = () => othersRunning() >= liveLimit.max;

function parallelText() {
  return t('已经有 {n} 段对话在同时进行，这是上限。等其中一段结束，或者先在会话栏上停下一段，再发。', { n: liveLimit.max });
}

// 会话栏上一行的小标记："正在进行"前面一个呼吸的点，"等你确认"是一块醒目的底色，"出错了""有新回复"各有各的颜色
function liveTag(id) {
  const mark = liveOf(id);
  if (!mark) return null;
  const tag = el('span', 'conv-live-tag ' + mark);
  tag.appendChild(el('i', 'conv-live-dot'));
  tag.appendChild(tel('span', null, LIVE_WORDS[mark]));
  return tag;
}

// 后台在跑的那一段的"停止"：放在"在访达中显示 / 编辑 / 删除"前面（规矩见 CLAUDE.md 的"每一样存在本地的东西，交互都一样"）
function liveStop(id) {
  if (!liveBusy(liveOf(id))) return null;
  const stop = iconButton('stop', k('停下这一段'));
  stop.classList.add('conv-stop');
  stop.addEventListener('click', async () => {
    stop.disabled = true;
    const reply = await call('/api/stop', { conversation: id });
    if (!reply.ok) { stop.disabled = false; toast(reply.error); }
  });
  return stop;
}

// 打开一段会话（点了提示、点了系统通知）。它正在后台跑着的话，就是换过去看它
function openLive(id) {
  if (!id || !state || id === state.conversation) return;
  jump = null;
  post('/api/conversation/open', { id });
  if (overlayNav.matches) setNav(false, false);
}

const liveTitle = (id) => {
  const item = state && state.conversations.find((entry) => entry.id === id);
  return item ? item.title : t('新对话');
};

// 每来一份状态调用一次（在画会话列表之前，见 events.js 的 state）
function renderLive(event) {
  liveMarks = event.live && typeof event.live === 'object' ? event.live : {};
  if (event.parallel) liveLimit = event.parallel;
  noticeLiveChanges();
  renderChatAlert();
}

// 刚出现的变化才提醒。刚连上、正在重放（换着看的那一下）时收到的状态是现在的样子：记下来，不提醒
function noticeLiveChanges() {
  const before = liveSeen;
  liveSeen = { ...liveMarks };
  const waiting = Object.keys(liveMarks).filter((id) => liveOf(id) === 'confirm');
  noteChatsWaiting(waiting.length > 0);
  if (!before || !isLive()) return;
  for (const id of Object.keys(liveMarks)) {
    const mark = liveOf(id);
    if (!mark || before[id] === mark) continue;
    const title = liveTitle(id);
    if (mark === 'confirm') {
      const said = t('「{title}」在等你确认', { title });
      announce(said);
      if (!document.hidden) toast(said);  // 页面在眼前、你正看着别的会话：在页面上说一声（顶上那条提示也一直在）
      systemNotify(said, t('点这里打开这段对话，在里面答复。'), { tag: 'harness-chat-confirm-' + id, urgent: true, open: () => openLive(id) });
    } else if (mark === 'error' || mark === 'done') {
      noteBackground(mark);
      const heading = mark === 'error' ? t('「{title}」出错了', { title }) : t('「{title}」回复完了', { title });
      systemNotify(heading, t('点这里打开这段对话。'), { tag: 'harness-chat-' + id, open: () => openLive(id) });
    }
  }
}

// 主区域顶上那条提示：有别的会话在等你确认。一段时说它的名字，几段时数一数；点一下打开第一段
function renderChatAlert() {
  const alert = $('chat-alert');
  const waiting = state ? state.conversations.filter((item) => liveOf(item.id) === 'confirm') : [];
  // 刚开始、还没存盘的那几段也在会话列表里（助手程序把它们列在最前面），所以按列表的顺序找就行
  alert.hidden = !waiting.length;
  if (!waiting.length) return;
  if (waiting.length === 1) retext($('chat-alert-text'), k('「{title}」在等你确认'), { title: waiting[0].title });
  else retext($('chat-alert-text'), k('{n} 段对话在等你确认'), { n: waiting.length });
  alert.dataset.id = waiting[0].id;
}

$('chat-alert').addEventListener('click', () => openLive($('chat-alert').dataset.id));
