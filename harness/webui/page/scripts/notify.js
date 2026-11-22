// ---- 系统通知：页面在后台时提醒你 ------------------------------------------------
// 你切到别的窗口干活时，智狐停下来等你点"允许"、一轮回复完了、出错了，页面上画得再清楚你也看不见。这一块管三件事：
//   1. 弹一个系统通知（浏览器的 Notification）：页面在后台（document.hidden）、设置里开着、浏览器也允许了才弹。
//      点通知把窗口切回来（window.focus()），滚到那张确认卡片、那条回复、那条出错的提示上，让它亮一下。
//      回到页面时，弹过的通知一起收掉：你已经在看了，它们留在通知中心里只会过时。
//   2. 标签页的标题前加一个标记，图标上加一个小点（装成应用的，程序坞 / 任务栏的图标上也加一个点）。等你确认的最显眼：
//      标题前写着"等你确认"，点是红的。回到页面就清掉。这一样不看设置里的开关：它不打扰人，也不用浏览器允许。
//   3. 设置里"系统通知"一节：开关（记在这个浏览器里，和外观、动效一样：浏览器允不允许通知本来就是每个浏览器各管各的）、
//      浏览器允许了没有、没允许时怎么允许。授权只在这里问（askNotify），定时任务列表里那句"开启系统通知"也是调它。
// 定时任务的提醒、超出预算、后台另一段对话的"等你确认""回复完了"（tasks-alerts.js、spending.js、live.js）也走这里的 systemNotify，
// 同一个开关管着。
//
// 只有实时发生的才算（isLive()，见 state.js）：刷新页面、打开旧会话、倒回一轮时重放出来的确认卡片和回复，不弹通知，
// 也不在标题上标"做完了""出错了"（只有一张重放出来、还真的在等你的确认卡片，会让标题写着"等你确认"：那是现在的样子，
// 不是新消息）。聊天通道、定时任务自己跑的那一轮不经过这个页面的事件流，各有各的提醒（聊天里、任务卡片上），走不到这里。
//
// 同一个智狐开在两个标签页（或者一个标签页加一个装成应用的窗口）里时，两边收到的是同一批事件。在后台的那一边不该在你
// 正看着另一边时弹通知，所以每个标签页用 BroadcastChannel 告诉别的标签页"我现在在不在眼前"，有一个在眼前就不弹。
//
// 图标为什么能换：页面的安全策略（webui/manifest.py 的 policy）里 img-src 本来就有 data:，标签页上的图标就是
// index.html 头上内嵌的那段 SVG（data: 地址）。这里把它解开、在右上角加一个圆点、再装回去，不放宽安全策略的任何一项。

const NOTIFY_KEY = 'harness.notify';  // 浏览器里记着的开关：'off' 是关了；没选过、'on' 都算开着（默认开）
const NOTIFY_ICON = '/icons/icon-192.png';  // 通知上的小狐狸：页面的 img-src 里本来就有这几张自带的图标（见 manifest.py）
const NOTE_CHARS = 160;  // 通知正文最多这么多字：系统的通知框只放得下两三行，后面的点通知回来看
const DOT_COLORS = { confirm: '#e5484d', error: '#e5484d', done: '#5a45e0' };  // 图标上的点：等你确认、出错是红的，做完了是强调色

const faviconLink = document.querySelector('link[rel="icon"]');
const plainFavicon = faviconLink ? faviconLink.getAttribute('href') || '' : '';
const dottedFavicons = new Map();  // 'confirm' / 'error' / 'done' → 带点的图标（data: 地址），第一次用时做好
const shownNotes = new Set();      // 弹出来、还没关掉的通知
const confirmNotes = new Map();    // 确认卡片的编号 → 为它弹的通知：答复了（也许是在另一个标签页里答的）就收掉

let notifyOn = recall(NOTIFY_KEY) !== 'off';
let plainTitle = document.title;   // 不带标记的标题（state 每次给，见 events.js）
let attention = '';                // 页面在后台时攒下的："error" 出错了、"done" 做完了。等你确认不记在这里，按现在的样子算
let turnFailed = false;            // 这一轮里出过错：一轮结束时不再说"回复完了"（出错的那一条已经说过了）
let tasksWaiting = false;          // 有定时任务在等你确认（tasks-alerts.js 告诉这里）
let chatsWaiting = false;          // 后台的另一段对话在等你确认（live.js 告诉这里）
let badged = false;                // 装成应用的图标上现在有没有点

const notifySupported = () => 'Notification' in window;
const notifyPermission = () => (notifySupported() ? Notification.permission : 'unsupported');  // granted / default / denied / unsupported

// ---- 别的标签页 ----------------------------------------------------------------
// 每个标签页一个编号；打开时说一声 hello，别的标签页回一句 here；页面在不在眼前变了说 state，关掉时说 bye。
// 都带着"我现在在不在眼前"。只是同一个网址（同一个智狐）的标签页之间说话，不出这台电脑的浏览器
const tabId = Math.random().toString(36).slice(2);
const peersInView = new Map();  // 别的标签页的编号 → 它现在在不在眼前
const notifyPeers = (() => { try { return new BroadcastChannel('harness.notify'); } catch (_) { return null; } })();

function tellPeers(kind) {
  if (!notifyPeers) return;
  try { notifyPeers.postMessage({ from: tabId, kind, seen: !document.hidden }); } catch (_) { /* 频道关了 */ }
}

if (notifyPeers) {
  notifyPeers.addEventListener('message', ({ data }) => {
    if (!data || typeof data.from !== 'string' || data.from === tabId) return;
    if (data.kind === 'bye') peersInView.delete(data.from);
    else peersInView.set(data.from, data.seen === true);
    if (data.kind === 'hello') tellPeers('here');  // 新开的标签页：告诉它有我，我在不在眼前
  });
  tellPeers('hello');
  window.addEventListener('pagehide', () => tellPeers('bye'));
  window.addEventListener('pageshow', (shown) => { if (shown.persisted) tellPeers('hello'); });  // 从"往返缓存"里回来的
}

const seenElsewhere = () => [...peersInView.values()].some(Boolean);

// ---- 弹通知 --------------------------------------------------------------------

// 页面在后台、开关开着、浏览器允许了、别的标签页也没被看着，才弹。options：
//   tag     同一个 tag 的新通知替换旧的（两个在后台的标签页弹同一件事，系统里也只有一条）
//   open    点通知、窗口切回来以后做什么（滚到那张卡片上……）
//   urgent  等你确认：留在屏幕上，直到你点它或者它被收掉（系统支持的话）
// 返回弹出来的通知，没弹返回 null
function systemNotify(title, body, options = {}) {
  if (!notifyOn || notifyPermission() !== 'granted' || !document.hidden || seenElsewhere()) return null;
  return showNote(title, body, options);
}

function showNote(title, body, { tag = '', open = null, urgent = false } = {}) {
  let note;
  try {
    note = new Notification(title, {
      body: body || '', tag, lang: root.lang, requireInteraction: urgent,
      icon: NOTIFY_ICON + '?token=' + encodeURIComponent(token),
    });
  } catch (_) {
    return null;  // 有的浏览器（比如安卓上的 Chrome）不许页面直接弹，就只靠标题和图标上的标记
  }
  shownNotes.add(note);
  note.onclick = (click) => {
    if (click && click.preventDefault) click.preventDefault();  // 不要浏览器自己的处理（有的会另开一个标签页）
    window.focus();
    note.close();
    if (open) open();
  };
  note.onclose = () => shownNotes.delete(note);
  return note;
}

function closeNotes() {
  for (const note of [...shownNotes]) {
    try { note.close(); } catch (_) { /* 已经没了 */ }
  }
  shownNotes.clear();
  confirmNotes.clear();
}

// 通知的正文：程序给的那句话（确认的文字本来就遮过密钥，见 harness/redact.py），空行和缩进收掉，太长截短
function noteText(text) {
  const lines = String(text || '').split('\n').map((line) => line.trim()).filter(Boolean);
  const joined = lines.join('\n');
  return joined.length > NOTE_CHARS ? joined.slice(0, NOTE_CHARS - 1).trimEnd() + '…' : joined;
}

// ---- 对话里的几件事（events.js 在对应的地方各调一行）----------------------------

// 出现了一张确认卡片
function noteConfirm(event, card) {
  if (!isLive()) { drawAttention(); return; }  // 重放出来的：不弹，标题按现在的样子标
  const said = event.key ? t(event.key, event.values) : String(event.description || '');
  const title = event.agent ? t('「{agent}」在等你确认', { agent: event.agent }) : t('智狐在等你确认');
  const note = systemNotify(title, noteText(said), { tag: 'harness-confirm-' + event.id, urgent: true, open: () => showInChat(card, true) });
  if (note) confirmNotes.set(event.id, note);
  drawAttention();
}

// 一张确认卡片答复了（在这里、在另一个标签页里，或者点了"停止"）：为它弹的通知已经没用了
function noteConfirmDone(id) {
  const note = confirmNotes.get(id);
  confirmNotes.delete(id);
  if (note) {
    try { note.close(); } catch (_) { /* 已经没了 */ }
  }
  drawAttention();
}

function noteTurnStart() {
  turnFailed = false;
}

// 一条出错的提示（这一轮出错了、存盘失败……）
function noteError(node) {
  if (!isLive()) return;
  turnFailed = true;
  if (!document.hidden) return;
  attention = 'error';
  drawAttention();
  systemNotify(t('智狐出错了'), noteText(node.textContent), { tag: 'harness-error', open: () => showInChat(node) });
}

// 一轮结束。stopped：是你在这个页面上点了"停止"，不用告诉你停下了
function noteTurnEnd(stopped) {
  const failed = turnFailed;
  turnFailed = false;
  if (!isLive() || stopped || failed || !document.hidden) { drawAttention(); return; }
  if (attention !== 'error') attention = 'done';
  drawAttention();
  const reply = lastReply();
  const text = reply ? replyText(reply) : '';
  systemNotify(t('智狐回复完了'), text ? noteText(text) : t('这一轮做完了。'), { tag: 'harness-turn', open: () => showInChat(reply || log.lastElementChild) });
}

// 这一轮最后的那条回复：从对话的末尾往前找，碰到你的消息（这一轮的开头）就停
function lastReply() {
  for (let node = log.lastElementChild; node; node = node.previousElementSibling) {
    if (node.matches('.msg.user, .msg-atts')) return null;
    if (node.matches('.msg.assistant')) return node;
  }
  return null;
}

// 回复画出来的文字（Markdown 的记号已经没了）：去掉引用的小编号、代码块上写着语言的那一条，段落、列表的每一项之间换行。
// 在一份不挂在页面上的副本里做，不碰页面、不用排版
function replyText(node) {
  const holder = node.querySelector('.msg-text');
  if (!holder) return '';
  const copy = holder.cloneNode(true);
  for (const extra of copy.querySelectorAll('.cite-marks, .md-code-bar')) extra.remove();
  for (const block of copy.querySelectorAll('p, li, h1, h2, h3, h4, h5, h6, pre, tr, blockquote')) block.append('\n');
  return copy.textContent.replace(/[ \t]+/g, ' ');
}

// 点了通知、窗口切回来以后：把那一块滚到对话区靠上三分之一处，让它亮一下（确认卡片再响一次它的环）。
// 和从搜索结果、多媒体库定位过去是同一种样子（见 settle.js 的 reveal）
function showInChat(node, ring) {
  if (!node || !log.contains(node)) return;  // 已经换了一段对话
  setStick(false);
  const spot = node.getBoundingClientRect(), view = chat.getBoundingClientRect();
  chat.scrollTop += spot.top - view.top - view.height / 3;
  setStick(chat.scrollHeight - chat.scrollTop - chat.clientHeight < 4);
  const mark = ring && !node.classList.contains('done') ? 'alert' : 'found';
  node.classList.remove(mark);
  void node.offsetWidth;  // 让浏览器先认下"没有这个类"，再加回去，动画才会重新播
  node.classList.add(mark);
  if (mark === 'found') setTimeout(() => node.classList.remove('found'), 2300);
}

// 定时任务有没有在等你确认（tasks-alerts.js 每画一次任务说一次）
function noteTasksWaiting(waiting) {
  if (tasksWaiting === waiting) return;
  tasksWaiting = waiting;
  drawAttention();
}

// 后台的另一段对话有没有在等你确认（live.js 每来一份状态说一次）。弹通知由 live.js 自己做（它知道是哪一段），这里只管标题和图标
function noteChatsWaiting(waiting) {
  if (chatsWaiting === waiting) return;
  chatsWaiting = waiting;
  drawAttention();
}

// 后台的另一段对话刚跑完（done）、出错了（error）：页面在后台的话，标签页上也标出来，和你正看着的这一段的一样
function noteBackground(kind) {
  if (!document.hidden) return;
  if (kind === 'error') attention = 'error';
  else if (attention !== 'error') attention = 'done';
  drawAttention();
}

// ---- 标题和图标 ----------------------------------------------------------------

// 标签页的标题（不带标记的那一截）。state 每次都给（会话的标题 · 智狐），换语言时也给一次
function setPageTitle(text) {
  plainTitle = text;
  drawAttention();
}

function attentionLevel() {
  if (!document.hidden) return '';
  if ((busy && !stopping && confirms.size > 0) || tasksWaiting || chatsWaiting) return 'confirm';
  return attention;
}

function drawAttention() {
  const level = attentionLevel();
  const mark = level === 'confirm' ? '● ' + t('等你确认') + ' · ' : level === 'error' ? '● ' + t('出错了') + ' · ' : level === 'done' ? '● ' : '';
  if (document.title !== mark + plainTitle) document.title = mark + plainTitle;
  const icon = level ? dottedFavicon(level) : plainFavicon;
  if (faviconLink && icon && faviconLink.getAttribute('href') !== icon) faviconLink.setAttribute('href', icon);
  drawBadge(Boolean(level));
}

// 标签页的图标右上角加一个圆点：白色的描边让它在深色、浅色的标签栏上都看得清
function dottedFavicon(level) {
  if (dottedFavicons.has(level)) return dottedFavicons.get(level);
  const head = 'data:image/svg+xml,';
  let made = '';
  try {
    if (plainFavicon.startsWith(head)) {
      const svg = decodeURIComponent(plainFavicon.slice(head.length));
      const end = svg.lastIndexOf('</svg>');
      const dot = "<circle cx='24.5' cy='7.5' r='6.5' fill='" + DOT_COLORS[level] + "' stroke='#fff' stroke-width='2'/>";
      if (end > 0) made = head + encodeURIComponent(svg.slice(0, end) + dot + svg.slice(end));
    }
  } catch (_) { /* 解不开就不换图标，只改标题 */ }
  dottedFavicons.set(level, made);
  return made;
}

// 装成应用以后（Chrome、Edge）程序坞、任务栏上的图标也带一个点。在普通的标签页里浏览器不理它
function drawBadge(on) {
  if (on === badged || !('setAppBadge' in navigator)) return;
  badged = on;
  try {
    const done = on ? navigator.setAppBadge() : navigator.clearAppBadge();
    if (done && done.catch) done.catch(() => {});
  } catch (_) { /* 不支持就算了 */ }
}

function onVisibility() {
  if (!document.hidden) {  // 回到页面了：攒下的标记、弹过的通知都清掉
    attention = '';
    closeNotes();
    notifyChanged();  // 也许刚在浏览器的网站设置里改了通知的权限：设置里那一节、任务列表里那句都重画
  }
  tellPeers('state');
  drawAttention();
}

document.addEventListener('visibilitychange', onVisibility);
window.addEventListener('focus', () => { if (!document.hidden && (attention || shownNotes.size)) onVisibility(); });

// ---- 设置里的"系统通知" ---------------------------------------------------------

// 请浏览器问你要不要允许通知。浏览器只在你点了什么的时候才肯问，所以只从按钮、开关的 click 里调用
async function askNotify() {
  if (!notifySupported()) return;
  try {
    // 老的 Safari 只认回调的写法（不返回 Promise），新的两种都认：两种都给，先到的那个画一次
    await Notification.requestPermission(() => notifyChanged());
  } catch (_) { /* 你关掉了询问 */ }
  notifyChanged();
}

// 开关或权限变了：设置里这一节、定时任务列表里那句"开启系统通知"都重画
function notifyChanged() {
  drawNotifySettings();
  renderNotifyOffer();
}

// 系统的通知设置在哪儿，按打开页面的这台电脑说（弹通知的是它）
const systemNotifyPlace = () => byBrowser(
  k('没看到的话：看看“系统设置 → 通知”里有没有允许你用的浏览器，专注模式（勿扰）是不是开着。'),
  k('没看到的话：看看“设置 → 系统 → 通知”里有没有允许你用的浏览器，勿扰模式是不是开着。'),
  k('没看到的话：看看桌面的通知设置里有没有允许你用的浏览器，勿扰模式是不是开着。'),
);

function drawNotifySettings() {
  const permission = notifyPermission();
  const toggle = $('notify-enabled');
  toggle.setAttribute('aria-checked', String(notifyOn && permission !== 'unsupported'));
  toggle.disabled = permission === 'unsupported';
  retext($('notify-state'), permission === 'unsupported' ? k('（这个浏览器不支持）') : !notifyOn ? k('（关着）')
    : permission === 'granted' ? k('（开着）') : permission === 'denied' ? k('（被浏览器拦下了）') : k('（等浏览器允许）'));
  const box = $('notify-permission');
  const kind = permission === 'unsupported' ? 'warn' : !notifyOn ? '' : permission === 'granted' ? 'good' : permission === 'denied' ? 'warn' : 'ask';
  box.hidden = !kind;
  if (!kind) return;
  box.className = 'notify-permission ' + kind;
  $('notify-permission-icon').setAttribute('href', kind === 'warn' ? '#i-alert' : kind === 'good' ? '#i-check' : '#i-bell');
  retext($('notify-permission-text'), {
    warn: permission === 'unsupported'
      ? k('这个浏览器不支持系统通知。页面在后台时，标签页的标题和图标上照样会标出来。')
      : k('浏览器拦下了这个网站的通知，页面没法再问一次。Chrome、Edge、Firefox：点地址栏左边的网站图标，把“通知”改成“允许”；Safari：在“设置 → 网站 → 通知”里把这个网站改成“允许”。改好以后刷新这个页面。'),
    good: k('浏览器已经允许了。'),
    ask: k('浏览器还没允许智狐弹通知。点“允许通知”，再在浏览器弹出的询问里选“允许”。'),
  }[kind]);
  const more = $('notify-permission-more');
  more.hidden = kind !== 'good';
  if (!more.hidden) retext(more, systemNotifyPlace());
  $('notify-ask').hidden = kind !== 'ask';
  $('notify-try').hidden = kind !== 'good';
  $('notify-permission-actions').hidden = kind === 'warn';  // 被拦下了、不支持：页面上没有能按的，只能照着说的去改
}

$('notify-enabled').addEventListener('click', () => {
  if (!notifySupported()) return;
  notifyOn = !notifyOn;
  remember(NOTIFY_KEY, notifyOn ? 'on' : 'off');
  if (notifyOn && notifyPermission() === 'default') askNotify();  // 刚打开、浏览器还没问过：趁这一下点击就问
  notifyChanged();
});
$('notify-ask').addEventListener('click', askNotify);
// 发一条试试：页面就在眼前也弹（平时不会），看看这台电脑的系统设置有没有把它拦下
$('notify-try').addEventListener('click', () => {
  showNote(t('智狐的通知长这样'), t('页面在后台时，等你确认、一轮回复完了、出错了，都会这样告诉你。点它回到智狐。'), { tag: 'harness-try' });
});

// 你在浏览器的网站设置里改了权限：支持的浏览器会说一声，这一节马上跟着变（不支持的，回到页面时也会重画，见 onVisibility）
if (navigator.permissions && navigator.permissions.query) {
  navigator.permissions.query({ name: 'notifications' }).then((status) => { status.onchange = notifyChanged; }, () => {});
}
drawNotifySettings();
