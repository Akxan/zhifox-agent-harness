// ---- 扩展 · 通道 -------------------------------------------------------------
// 在 Telegram、Discord、Slack、飞书、钉钉、QQ 这些聊天软件里和智狐说话（见 harness/channels/）。这一页有两种视图：
//   list    已经接上的通道（状态、绑定的账号、最后一次说话、聊天里正在做什么）、聊天里等你批准的操作、可以接入的平台
//   wizard  设置向导：1 建一个机器人 → 2 填 token（或者 ID 和密钥；助手程序去问平台认不认）→ 3 配对 → 4 选确认方式
// 列表跟着状态走（state.channels，格式见 channels/hub.py 的 snapshot），内容没变就不重画。
// 平台的名单、各自到了哪一步、要填哪几项（fields）、向导第一步的说明（guide）、确认请求是按钮还是回复文字（buttons），
// 都是助手程序给的（channels/platforms.py、guides.py），页面不另抄一份。
// 密钥只在你点"验证并连接"时发给助手程序一次，之后页面上只有最后 4 位；关掉对话框时输入框连同内容一起清掉。
// 聊天里等你批准的操作，除了这一页，主区域顶上也有一条提示（chan-alert），不用打开对话框就能答复。

const chanPanel = $('panel-channels');
const CHAN_STATUS = { online: k('在线'), connecting: k('连接中'), error: k('有问题'), off: k('已关闭') };
const CHAN_STAGE = { ready: k('现在能用'), soon: k('即将支持'), public_url: k('需要公网地址 · 第二阶段'), unsupported: k('不支持') };
const CHAN_STEPS = [k('建机器人'), k('填 token'), k('配对'), k('确认方式')];
const CHAN_MODES = APPROVAL_ORDER.map((mode) => ({ mode, text: APPROVAL_TEXT[mode] }));  // 几档的名字和说明在 approval-modes.js
let chanState = null;            // 状态里的 channels：{problem, file, platforms, approvals}
let chanView = { kind: 'list' };
let chanDrawn = '';
let chanDeleting = null;         // 正在原地确认删除的那个平台
const chanPending = new Set();   // 正在等回答的操作（按平台）。这期间那一行的按钮不能再点
let chanTicker = 0;              // 配对码的倒计时：每秒改一次文字（不是动画）
let chanAsked = null;            // 上一次看到的、聊天里等你批准的那些（新出现的才播报）

const chanFind = (id) => (chanState ? chanState.platforms.find((item) => item.id === id) : null);
const chanConnected = () => (chanState ? chanState.platforms.filter((item) => item.stage === 'ready' && item.configured) : []);
const chanKey = (ask) => ask.platform + ':' + ask.chat + ':' + ask.id;
const oneLine = (text) => String(text || '').replace(/\s+/g, ' ').trim();
const chanBot = (item) => (item && item.bot ? item.bot.handle || item.bot.name || '' : '');  // "@zhifox_bot"，或者没有 @用户名的平台上机器人的名字
const chanSdkMissing = (item) => Boolean(item && item.sdk && !item.sdk.ready);

// 一段说明里的 {code} 换成代码的样子（向导第一步、平台给的说明都这样写）。文字和代码都用 textContent 放进去
function chanRich(text, code) {
  const box = document.createDocumentFragment();
  String(text).split('{code}').forEach((part, index) => {
    if (index) box.appendChild(el('code', null, code || ''));
    if (part) box.appendChild(document.createTextNode(part));
  });
  return box;
}

// 一个"复制"按钮：成功或失败都在 help 那一行说一声（对话框里的提示写在对话框里）
function chanCopy(text, label, help, focus) {
  const button = chanButton('copy', label);
  if (focus) button.dataset.focus = focus;
  button.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(text); retext(help, k('已复制。')); help.className = 'chan-help good'; }
    catch (_) { retext(help, k('没能复制，请手动选中复制。')); help.className = 'chan-help bad'; }
  });
  return button;
}

// 平台要的官方 SDK 没装：说清楚怎么装（飞书、钉钉）。命令可以直接复制
function chanSdkNote(item, help) {
  const box = chanNote('bad', item.sdk.message, 'alert');
  const row = box.querySelector('span').appendChild(el('span', 'chan-sdk'));
  row.appendChild(el('code', null, item.sdk.install));
  row.appendChild(chanCopy(item.sdk.install, k('复制命令'), help, 'copy-install'));
  return box;
}

function chanNote(kind, text, iconName) {
  const box = el('div', 'chan-note ' + kind);
  box.setAttribute('role', kind === 'bad' ? 'alert' : 'note');
  box.appendChild(icon(iconName || (kind === 'calm' ? 'spark' : kind === 'good' ? 'check' : 'alert')));
  box.appendChild(el('span', null, text));
  return box;
}

function chanButton(iconName, text, extra) {
  const button = el('button', 'button chan-button' + (extra ? ' ' + extra : ''));
  button.type = 'button';
  if (iconName) button.appendChild(icon(iconName));
  button.appendChild(tel('span', null, text));
  return button;
}

// 一个打开外部网页的链接（BotFather、和机器人的聊天）。在新标签页里打开，不带上这个页面的地址（里面有口令）
function chanLink(href, text, extra) {
  const link = el('a', 'button chan-button' + (extra ? ' ' + extra : ''));
  if (/^https:\/\//.test(String(href))) link.href = href;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  link.append(icon('external'), tel('span', null, text));
  return link;
}

// 平台的标记：Telegram 是一架纸飞机，别的写名字的第一个字。状态圆点挂在右下角（只有接上了的才有）
function chanMark(item, status) {
  const mark = el('span', 'chan-mark');
  mark.dataset.platform = item.id;
  if (item.id === 'telegram') mark.appendChild(icon('send'));
  else mark.textContent = Array.from(item.label || '?')[0].toUpperCase();
  if (status) mark.appendChild(el('i', 'chan-dot ' + status));
  mark.setAttribute('aria-hidden', 'true');
  return mark;
}

// 换一种视图：换掉整页的内容。从别的视图换过来的话，回到顶上、内容淡入；焦点放到 focus 指定的那个元素上
function chanShow(nodes, kind, focus) {
  const fresh = chanPanel.dataset.view !== kind;
  const kept = !fresh && chanPanel.contains(document.activeElement) ? document.activeElement.dataset.focus : null;
  const keptScroll = chanPanel.scrollTop;
  chanPanel.dataset.view = kind;
  const wrap = el('div', 'chan-view');
  wrap.append(...nodes);
  chanPanel.replaceChildren(wrap);
  if (fresh) {
    chanPanel.scrollTop = 0;
    play(wrap, [{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }], 240);
  } else {
    chanPanel.scrollTop = keptScroll;  // 状态更新时重画：别把正在看的位置弄丢
  }
  const target = focus || kept;
  if (target) {
    const again = chanPanel.querySelector('[data-focus="' + CSS.escape(target) + '"]');
    if (again) again.focus({ preventScroll: !fresh });
  }
}

// 一个 POST 请求，等回答的这段时间把这个平台标成"忙"（按钮暂时不能点）
async function chanAct(platform, path, data) {
  chanPending.add(platform);
  if (chanView.kind === 'list') drawChanList();
  const reply = await call(path, { platform, ...data });
  chanPending.delete(platform);
  drawChannels();
  return reply;
}

function renderChannelsState(data) {
  chanState = data && Array.isArray(data.platforms) ? data : null;
  const connected = chanConnected().length;
  $('count-channels').textContent = connected ? connected.toLocaleString(LOCALES[language]) : '';
  renderChanAlert();
  if (extensionsDialog.open && extensionTab === 'channels') drawChannels();
}

function drawChannels() {
  if (!chanState) return;
  if (chanView.kind === 'list') drawChanList();
  else drawChanWizard();
}

// ---- 列表 ----

function drawChanList(focus) {
  // "5 分钟前"这类时间每分钟要重写一次，所以把现在是第几分钟也算进"变了没有"里
  const print = JSON.stringify([chanState, chanDeleting, [...chanPending], language, Math.floor(Date.now() / 60000)]);
  if (!focus && print === chanDrawn && chanPanel.dataset.view === 'list') return;
  chanDrawn = print;
  const nodes = [chanToolbar()];
  if (chanState.problem) nodes.push(chanNote('bad', chanState.problem));
  if (chanState.approvals.length) nodes.push(chanAsks());
  const connected = chanConnected();
  if (connected.length) {
    const section = el('section', 'chan-section');
    section.appendChild(el('h3', null, t('已经接上的通道')));
    const list = section.appendChild(el('ul', 'chan-list'));
    list.setAttribute('aria-label', t('已经接上的通道'));
    list.append(...connected.map(chanRow));
    nodes.push(section);
  } else {
    nodes.push(chanHero());
  }
  const section = el('section', 'chan-section');
  section.appendChild(el('h3', null, t('可以接入的平台')));
  const grid = section.appendChild(el('ul', 'chan-grid'));
  grid.setAttribute('aria-label', t('可以接入的平台'));
  grid.append(...chanState.platforms.map(chanCard));
  nodes.push(section);
  chanShow(nodes, 'list', focus);
}

function chanToolbar() {
  const bar = el('div', 'chan-bar');
  bar.appendChild(el('p', 'chan-lead', t('在手机上的聊天软件里和智狐说话，就像在这里一样。只有你配对过的账号能用；要执行命令、改文件时，照样先发到聊天里问你。')));
  const reveal = bar.appendChild(iconButton('folder', ON_MAC ? k('在访达中显示 channels.json') : k('在文件夹中显示 channels.json')));
  reveal.classList.remove('small');
  reveal.dataset.focus = 'reveal';
  reveal.addEventListener('click', revealChannels);
  return bar;
}

function chanHero() {
  const box = el('div', 'chan-hero');
  const words = box.appendChild(el('div'));
  words.appendChild(el('h3', null, t('在手机上和智狐聊天')));
  words.appendChild(el('p', null, t('接上一个聊天机器人（Telegram、Discord、Slack、飞书、钉钉或者 QQ），出门在外也能让智狐查资料、看日程、在这台电脑（或者服务器）上做事。不需要公网地址：是智狐主动连到平台去收你的消息，不用开端口。')));
  words.appendChild(el('p', null, t('只有你自己的账号能用：先用一个 6 位数字配对，别人发来的消息一律不理。要执行命令、改文件时，聊天里会先问你，你答复了才做。')));
  // 现在能用的平台各一个按钮：第一个（Telegram，最快建好）是主按钮
  const row = words.appendChild(el('div', 'row chan-picks'));
  chanState.platforms.filter((item) => item.stage === 'ready').forEach((item, index) => {
    const pick = row.appendChild(el('button', 'button chan-button chan-pick' + (index === 0 ? ' primary' : '')));
    pick.type = 'button';
    pick.dataset.focus = 'connect:' + item.id;
    pick.append(chanMark(item), el('span', null, item.label));
    pick.setAttribute('aria-label', t('连接 {platform}', { platform: item.label }));
    pick.addEventListener('click', () => openChanWizard(item.id, 1));
  });
  const cli = words.appendChild(el('p', 'chan-cli'));
  cli.appendChild(document.createTextNode(t('在没有浏览器的服务器上：') + ' '));
  cli.appendChild(el('code', null, 'harness channels setup telegram'));
  cli.appendChild(document.createTextNode(' ' + t('（或者 discord、slack、feishu、dingtalk、qq）')));
  const phone = box.appendChild(el('div', 'chan-phone'));
  phone.setAttribute('aria-hidden', 'true');
  for (let index = 0; index < 4; index += 1) phone.appendChild(el('i'));
  return box;
}

function chanAsks() {
  const box = el('div', 'chan-asks');
  for (const ask of chanState.approvals) {
    const card = box.appendChild(el('div', 'chan-ask'));
    card.setAttribute('role', 'group');
    const head = card.appendChild(el('div', 'chan-ask-head'));
    head.appendChild(icon('alert'));
    const title = t('{platform} · {name} 在等你批准', { platform: ask.label, name: ask.name });
    head.appendChild(el('span', null, title));
    card.setAttribute('aria-label', title);
    card.appendChild(el('pre', null, ask.summary));
    const row = card.appendChild(el('div', 'row'));
    const allow = row.appendChild(el('button', 'mini primary', t('允许')));
    const deny = row.appendChild(el('button', 'mini', t('拒绝')));
    allow.type = deny.type = 'button';
    allow.dataset.focus = 'allow:' + chanKey(ask);
    deny.dataset.focus = 'deny:' + chanKey(ask);
    allow.addEventListener('click', () => chanConfirm(ask, true, allow, deny));
    deny.addEventListener('click', () => chanConfirm(ask, false, allow, deny));
  }
  return box;
}

async function chanConfirm(ask, allow, ...buttons) {
  for (const button of buttons) button.disabled = true;
  const reply = await call('/api/channel/confirm', { platform: ask.platform, chat: ask.chat, confirm_id: ask.id, allow });
  if (!reply.ok) {
    for (const button of buttons) button.disabled = false;
    if (extensionsDialog.open) extensionSay(null, null, 'bad', reply.error); else toast(reply.error);
    return;
  }
  const said = allow ? t('已允许。聊天里那条消息上也写上了。') : t('已拒绝。聊天里那条消息上也写上了。');
  if (extensionsDialog.open) extensionSay(null, null, 'good', said); else toast(said);
  announce(said);
}

function chanMeta(item) {
  const status = item.enabled ? item.status.state : 'off';
  const bits = [t(CHAN_STATUS[status] || status)];
  if (item.owner) bits.push(t('主人：{name}', { name: item.owner.name }));
  bits.push(t(APPROVAL[item.approval] || APPROVAL.ask));
  if (item.ignored) bits.push(item.ignored === 1 ? t('忽略了 1 条陌生人的消息') : t('忽略了 {n} 条陌生人的消息', { n: item.ignored.toLocaleString(LOCALES[language]) }));
  return bits.join(' · ');
}

function chanRow(item) {
  const row = el('li', 'chan-row');
  row.dataset.platform = item.id;
  const pending = chanPending.has(item.id);
  const status = item.enabled ? item.status.state : 'off';

  // 删除前在原地确认一下，和删除会话、记忆、MCP 服务器一样，不弹对话框
  if (chanDeleting === item.id) {
    const box = row.appendChild(el('div', 'chan-confirm'));
    box.appendChild(el('span', null, t('删除 {platform} 的设置？机器人的 token 和绑定的账号都会忘掉，以前聊过的会话还在会话列表里。', { platform: item.label })));
    const yes = box.appendChild(el('button', 'mini danger', t('删除')));
    const no = box.appendChild(el('button', 'mini cancel', t('取消')));
    yes.type = no.type = 'button';
    no.dataset.focus = 'cancel:' + item.id;
    yes.disabled = no.disabled = pending;
    yes.addEventListener('click', () => deleteChannel(item, row));
    no.addEventListener('click', () => { chanDeleting = null; drawChanList('open:' + item.id); });
    return row;
  }

  const main = row.appendChild(el('div', 'chan-main'));
  const open = main.appendChild(el('button', 'chan-open'));
  open.type = 'button';
  open.dataset.focus = 'open:' + item.id;
  open.appendChild(chanMark(item, status));
  const words = open.appendChild(el('span', 'chan-words'));
  const line = words.appendChild(el('span', 'chan-line'));
  line.appendChild(el('span', 'chan-name', item.label));
  if (item.bot) line.appendChild(el('span', 'chan-bot', chanBot(item)));
  if (chanSdkMissing(item)) line.appendChild(el('span', 'chan-flag', t('要装 SDK')));
  else if (item.pairing) line.appendChild(el('span', 'chan-flag calm', t('正在配对')));
  else if (!item.owner) line.appendChild(el('span', 'chan-flag', t('还没配对')));
  if (item.approval === 'all') line.appendChild(el('span', 'chan-flag', t('全部自动允许')));
  const meta = words.appendChild(el('span', 'chan-meta', chanMeta(item)));
  meta.title = meta.textContent;
  if (item.enabled && item.status.message && status !== 'online') words.appendChild(el('span', 'chan-error', item.status.message));
  open.setAttribute('aria-label', t('设置 {platform}：{state}', { platform: item.label, state: chanMeta(item) }));
  open.addEventListener('click', () => openChanWizard(item.id));

  const toggle = main.appendChild(el('button', 'chan-switch'));
  toggle.type = 'button';
  toggle.setAttribute('role', 'switch');
  toggle.setAttribute('aria-checked', item.enabled ? 'true' : 'false');
  toggle.title = t('打开或关掉 {platform}', { platform: item.label });
  toggle.setAttribute('aria-label', toggle.title);
  toggle.dataset.focus = 'enable:' + item.id;
  toggle.disabled = pending;
  toggle.addEventListener('click', () => enableChannel(item, !item.enabled));

  // 最右边一格：平时是最后一次说话的时间；悬停时换成按钮。最后三个和会话行、记忆行一样（在访达中显示、编辑、删除），
  // 位置、大小、顺序都一样。名字里带上是哪一个平台：读屏软件逐个读按钮时要知道它操作的是谁
  const slot = main.appendChild(el('div', 'chan-slot'));
  slot.appendChild(el('span', 'chan-when', item.last_active ? fromNow(item.last_active) : t('还没说过话')));
  const actions = slot.appendChild(el('div', 'chan-actions'));
  const button = (iconName, title, label, extra) => {
    const node = actions.appendChild(el('button', 'icon-button small' + (extra ? ' ' + extra : '')));
    node.type = 'button';
    node.title = t(title);
    node.setAttribute('aria-label', t(label, { platform: item.label }));
    node.appendChild(icon(iconName));
    node.disabled = pending;
    return node;
  };
  const pair = button('link', k('重新配对'), k('重新配对：{platform}'));
  pair.dataset.focus = 'pair:' + item.id;
  pair.addEventListener('click', () => openChanWizard(item.id, 3, true));
  const reveal = button('folder', SHOW_FILE, ON_MAC ? k('在访达中显示 {platform} 的设置文件') : k('在文件夹中显示 {platform} 的设置文件'));
  reveal.dataset.focus = 'reveal:' + item.id;
  reveal.disabled = false;
  reveal.addEventListener('click', revealChannels);
  const edit = button('edit', k('编辑'), k('编辑 {platform} 的设置'));
  edit.dataset.focus = 'edit:' + item.id;
  edit.addEventListener('click', () => openChanWizard(item.id));
  const remove = button('trash', k('删除'), k('删除 {platform} 的设置'), 'danger-hover');
  remove.dataset.focus = 'delete:' + item.id;
  remove.addEventListener('click', () => { chanDeleting = item.id; drawChanList('cancel:' + item.id); });

  // 这个通道里正在进行的对话：在做什么，旁边一个"停止"
  const busyChats = item.chats.filter((chat) => chat.state !== 'idle');
  if (busyChats.length) {
    const chats = row.appendChild(el('ul', 'chan-chats'));
    for (const chat of busyChats) {
      const entry = chats.appendChild(el('li', 'chan-chat' + (chat.state === 'waiting' ? ' waiting' : '')));
      entry.appendChild(el('i'));
      const doing = chat.state === 'waiting' ? t('等你批准')
        : t('正在进行第 {n} 步', { n: Math.max(chat.step, 1) }) + (chat.activity ? ' · ' + chat.activity : '');
      const text = entry.appendChild(el('span', null, t('{name}：{doing}', { name: chat.name, doing }) + (chat.queued ? ' · ' + t('还排着 {n} 条', { n: chat.queued }) : '')));
      text.title = text.textContent;
      const stop = entry.appendChild(el('button', 'mini', t('停止')));
      stop.type = 'button';
      stop.dataset.focus = 'stop:' + item.id + ':' + chat.chat;
      stop.addEventListener('click', async () => {
        stop.disabled = true;
        const reply = await call('/api/channel/stop', { platform: item.id, chat: chat.chat });
        if (!reply.ok) { stop.disabled = false; extensionSay(null, null, 'bad', reply.error); }
        else extensionSay(k('已停下。聊天里也说了一声。'), null, 'good');
      });
    }
  }
  return row;
}

function chanCard(item) {
  const ready = item.stage === 'ready';
  const card = el('li', 'chan-card ' + (ready ? 'ready' : 'dim'));
  const head = card.appendChild(el('div', 'chan-card-head'));
  head.appendChild(chanMark(item));
  head.appendChild(el('span', 'chan-card-name', item.label));
  const missing = ready && chanSdkMissing(item);
  const stage = head.appendChild(el('span', 'chan-stage ' + (missing ? 'public_url' : item.stage), missing ? t('要装 SDK') : t(CHAN_STAGE[item.stage] || item.stage)));
  stage.title = missing ? item.sdk.message : stage.textContent;
  card.appendChild(el('p', 'chan-needs', item.needs));
  if (ready) {
    const action = card.appendChild(item.configured ? chanButton('sliders', k('设置')) : chanButton('send', k('接入'), 'primary'));
    action.dataset.focus = 'card:' + item.id;
    action.setAttribute('aria-label', item.configured ? t('设置 {platform}', { platform: item.label }) : t('连接 {platform}', { platform: item.label }));
    action.addEventListener('click', () => openChanWizard(item.id, item.configured ? null : 1));
  }
  return card;
}

async function enableChannel(item, enabled) {
  const reply = await chanAct(item.id, '/api/channel/enable', { enabled });
  if (!reply.ok) { extensionSay(null, null, 'bad', reply.error); return; }
  const key = enabled ? k('已打开 {platform}，正在连接……') : k('已关掉 {platform}：机器人不再收消息。');
  extensionSay(key, { platform: item.label }, 'good');
  announce(t(key, { platform: item.label }));
}

async function deleteChannel(item, row) {
  const [reply] = await Promise.all([chanAct(item.id, '/api/channel/remove', {}), leave(row)]);
  chanDeleting = null;
  if (!reply.ok) { extensionSay(null, null, 'bad', reply.error); drawChanList('open:' + item.id); return; }
  drawChanList('card:' + item.id);
  extensionSay(k('已删除 {platform} 的设置。'), { platform: item.label }, 'good');
  announce(t('已删除 {platform} 的设置。', { platform: item.label }));
}

async function revealChannels() {
  const reply = await call('/api/reveal', { what: 'channels' });
  extensionSay(null, null, reply.ok ? 'good' : 'bad', reply.ok ? revealedText(reply.data.path) : reply.error);
}

// ---- 主区域顶上的那条提示：聊天里有操作在等你批准 ----

function renderChanAlert() {
  const box = $('chan-alert');
  const asks = chanState ? chanState.approvals : [];
  const ask = asks[0];
  box.hidden = !ask;
  const keys = asks.map(chanKey);
  if (chanAsked !== null && keys.some((key) => !chanAsked.includes(key))) {
    const fresh = asks.find((item) => !chanAsked.includes(chanKey(item)));
    announce(t('{platform} 上的对话在等你批准：{what}', { platform: fresh.label, what: oneLine(fresh.summary) }));
  }
  chanAsked = synced ? keys : null;  // 刚连上时补发的旧状态不播报
  if (!ask) return;
  const text = t('{platform} 上的对话在等你批准：{what}', { platform: ask.label, what: oneLine(ask.summary) });
  $('chan-alert-text').textContent = asks.length > 1 ? text + ' ' + t('（还有 {n} 个）', { n: asks.length - 1 }) : text;
  box.setAttribute('aria-label', text);
  $('chan-alert-open').title = t('打开“通道”看完整的内容');
  if (box.dataset.key !== chanKey(ask)) {  // 换成了另一个请求：两个按钮重新能点
    box.dataset.key = chanKey(ask);
    $('chan-alert-allow').disabled = $('chan-alert-deny').disabled = false;
  }
}

function alertAsk() {
  const key = $('chan-alert').dataset.key;
  return chanState ? chanState.approvals.find((ask) => chanKey(ask) === key) : null;
}
$('chan-alert-open').addEventListener('click', () => openExtensions('channels'));
$('chan-alert-allow').addEventListener('click', () => { const ask = alertAsk(); if (ask) chanConfirm(ask, true, $('chan-alert-allow'), $('chan-alert-deny')); });
$('chan-alert-deny').addEventListener('click', () => { const ask = alertAsk(); if (ask) chanConfirm(ask, false, $('chan-alert-allow'), $('chan-alert-deny')); });

// ---- 设置向导 ----
// 向导里你正在填的东西（token、选的确认方式）记在 chanView 里：状态更新、换语言时重画，填的内容不丢。

function openChanWizard(platform, step, repair) {
  const item = chanFind(platform);
  if (!item) return;
  const start = step || (item.configured ? (item.owner ? 4 : 3) : 1);
  // values：第 2 步里正在填的那几项（字段名 → 文字）；show：哪几个密钥框现在显示着明文
  chanView = {
    kind: 'wizard', platform, step: start, values: {}, show: {}, checking: false, error: '', connected: '',
    mode: item.approval || 'ask', pairedAt: item.owner ? item.owner.paired_at : null, pairing: false, armed: '',
  };
  chanDrawn = '';
  drawChanWizard('step');
  if (start === 3 && (repair || !item.owner) && !item.pairing) chanPair();
}

const chanTyped = (view) => Object.values(view.values || {}).some((value) => String(value).trim());

// 有没有填了、还没保存的东西：填了 token（或者 ID、密钥）没点"验证"，或者换了确认方式没点"完成"
function chanDirty() {
  if (chanView.kind !== 'wizard') return false;
  const item = chanFind(chanView.platform);
  if (chanView.step === 2 && chanTyped(chanView)) return true;
  return chanView.step === 4 && item && chanView.mode !== item.approval;
}

// 想离开向导（返回列表、关掉对话框）。有没保存的东西：先提醒一次，再来一次才放弃
function leaveChanWizard(why) {
  if (chanDirty() && chanView.armed !== why) {
    chanView.armed = why;
    chanView.error = '';
    drawChanWizard();
    const key = chanView.step === 2 ? k('填的东西还没保存。再点一次就放弃它。') : k('确认方式还没保存。再点一次就放弃这次的修改。');
    const help = chanPanel.querySelector('.chan-foot .chan-help');
    if (help) { retext(help, key); help.className = 'chan-help bad'; }
    return false;
  }
  return true;
}

function chanBack() {
  if (!leaveChanWizard('back')) return;
  const platform = chanView.platform;
  chanView = { kind: 'list' };
  clearInterval(chanTicker);
  chanTicker = 0;
  drawChanList('open:' + platform);
  if (!chanPanel.querySelector('[data-focus="open:' + CSS.escape(platform) + '"]')) {
    const card = chanPanel.querySelector('[data-focus="card:' + CSS.escape(platform) + '"]');
    if (card) card.focus();
  }
}

function chanGo(step) {
  chanView.step = step;
  chanView.armed = '';
  chanView.error = '';
  drawChanWizard('step');
  const item = chanFind(chanView.platform);
  if (step === 3 && item && !item.owner && !item.pairing) chanPair();
}

function drawChanWizard(focus) {
  const view = chanView, item = chanFind(view.platform);
  if (!item) { chanView = { kind: 'list' }; drawChanList(); return; }
  // 输入框正拿着焦点时，记下是哪一个、光标在哪儿，重画以后放回去
  const field = document.activeElement && chanPanel.contains(document.activeElement) && document.activeElement.classList.contains('chan-input') ? document.activeElement : null;
  const caret = field ? [field.id, field.selectionStart, field.selectionEnd] : null;
  const lengths = Object.fromEntries(Object.entries(view.values).map(([name, value]) => [name, value.length]));  // 填的内容本身不进"变了没有"
  const print = JSON.stringify([{ ...view, values: lengths }, item, language]);
  if (!focus && print === chanDrawn && chanPanel.dataset.view === 'wizard') { syncChanTicker(); return; }
  chanDrawn = print;

  const head = el('div', 'chan-head');
  const back = head.appendChild(el('button', 'chan-back'));
  back.type = 'button';
  back.dataset.focus = 'back';
  back.append(icon('chevron'), tel('span', null, k('通道')));
  back.addEventListener('click', chanBack);
  const title = head.appendChild(el('h3', 'chan-title'));
  title.appendChild(chanMark(item, item.configured ? (item.enabled ? item.status.state : 'off') : ''));
  title.appendChild(el('span', null, item.configured ? t('{platform} 的设置', { platform: item.label }) : t('连接 {platform}', { platform: item.label })));

  const stepper = el('div', 'chan-stepper');
  const steps = stepper.appendChild(el('ol', 'chan-steps'));
  steps.setAttribute('aria-label', t('设置的步骤'));
  CHAN_STEPS.forEach((name, index) => {
    const number = index + 1;
    const done = number < view.step || (number === 2 && item.configured) || (number === 3 && item.owner);
    const entry = steps.appendChild(el('li'));
    const button = entry.appendChild(el('button', 'chan-step' + (done && number !== view.step ? ' done' : '')));
    button.type = 'button';
    button.dataset.focus = 'step:' + number;
    const badge = button.appendChild(el('b'));
    if (done && number !== view.step) badge.appendChild(icon('check')); else badge.textContent = String(number);
    const words = number === 2 && item.step ? item.step : t(name);  // 第 2 步叫"填 token"还是"填 ID 和密钥"，看平台
    button.appendChild(el('span', null, words));
    button.setAttribute('aria-label', t('第 {n} 步：{name}', { n: number, name: words }));
    if (number === view.step) button.setAttribute('aria-current', 'step');
    // 前两步随时可以回去；配对和确认方式要等 token 填好（连上了机器人）以后才能去
    button.disabled = number === view.step || view.checking || (number > 2 && !item.configured);
    button.addEventListener('click', () => { if (leaveChanWizard('step:' + number)) chanGo(number); });
  });
  const bar = stepper.appendChild(el('i', 'chan-progress'));
  bar.style.transform = 'scaleX(' + view.step / CHAN_STEPS.length + ')';

  const pane = el('div', 'chan-pane');
  const foot = el('div', 'chan-foot');
  const help = foot.appendChild(el('p', 'chan-help'));
  help.setAttribute('role', 'status');
  [chanStepOne, chanStepTwo, chanStepThree, chanStepFour][view.step - 1](item, pane, foot, help);
  if (view.error) { help.textContent = view.error; help.className = 'chan-help bad'; }
  chanShow([head, stepper, pane, foot], 'wizard', focus === 'step' ? null : focus);
  const again = caret ? document.getElementById(caret[0]) : null;
  if (again && chanPanel.contains(again)) { again.focus(); again.setSelectionRange(caret[1], caret[2]); }
  if (focus === 'step') {  // 刚换到这一步：焦点放在这一步最该先做的那件事上
    const first = chanPanel.querySelector('.chan-input:not(:disabled), .chan-mode[aria-checked="true"], .chan-foot .button.primary:not(:disabled), .chan-foot .button:not(:disabled)');
    if (first) first.focus({ preventScroll: true });
  }
  syncChanTicker();
}

// 第 1 步：照着平台给的步骤（channels/guides.py）建一个机器人。和命令行里 harness channels setup 印的是同一份
function chanStepOne(item, pane, foot, help) {
  pane.appendChild(el('h4', null, t('先在 {platform} 里建一个机器人', { platform: item.label })));
  if (chanSdkMissing(item)) pane.appendChild(chanSdkNote(item, help));
  const steps = pane.appendChild(el('ol', 'chan-howto'));
  (item.guide || []).forEach((step, index) => {
    const entry = steps.appendChild(el('li'));
    entry.appendChild(chanRich(step.text, step.code));
    if (step.link || step.copy) {
      const tools = entry.appendChild(el('span', 'chan-how-tools'));
      if (step.link) tools.appendChild(chanLink(step.link, step.link_label || k('打开')));
      if (step.copy) tools.appendChild(chanCopy(step.copy, step.copy_label || k('复制'), help, 'copy:' + index));
    }
  });
  pane.appendChild(chanNote('calm', t('机器人是你自己的：别人也许能搜到它，但只有配对过的账号能让它做事。智狐是主动连到 {platform} 去收消息的，不需要公网地址，家里的电脑、内网里的服务器都能用。', { platform: item.label })));
  const next = foot.appendChild(chanButton(null, item.step ? k('准备好了，下一步') : k('有 token 了，下一步'), 'primary'));
  next.dataset.focus = 'next';
  next.addEventListener('click', () => chanGo(2));
}

// 第 2 步：填 token（或者 ID 和密钥）。要填哪几项、哪几项是密钥，是平台给的（item.fields）。
// 已经连着的平台，没重填的那几项接着用存着的（见 channels/hub.merge），所以只换一个 token 不用把别的也重填一遍
function chanStepTwo(item, pane, foot, help) {
  const view = chanView;
  const fields = item.fields || [];
  const secret = fields.some((field) => field.secret);
  pane.appendChild(el('h4', null, fields.length > 1 ? t('把这几项粘贴到这里') : t('把 token 粘贴到这里')));
  if (chanSdkMissing(item)) pane.appendChild(chanSdkNote(item, help));
  const label = (submitting) => submitting || !item.configured ? k('验证并连接') : k('下一步');
  let submit = null;
  const grid = pane.appendChild(el('div', 'chan-fields'));
  fields.forEach((field, index) => {
    const id = 'chan-field-' + field.id;
    const box = grid.appendChild(el('label', 'chan-field'));
    box.htmlFor = id;
    box.appendChild(el('span', null, field.label));
    const row = box.appendChild(el('span', 'chan-token'));
    const input = row.appendChild(el('input', 'field chan-input'));
    input.id = id;
    input.type = field.secret && !view.show[field.id] ? 'password' : 'text';
    input.autocomplete = 'off';
    input.spellcheck = false;
    input.maxLength = 400;
    input.value = view.values[field.id] || '';
    input.dataset.focus = 'field:' + field.id;
    const hint = item.hints ? item.hints[field.id] : '';
    input.placeholder = hint ? (field.secret ? t('已经保存了一个（{hint}）。要换就粘贴新的', { hint }) : hint) : field.placeholder || '';
    if (secret) input.setAttribute('aria-describedby', 'chan-token-help');
    input.disabled = view.checking;
    input.addEventListener('input', () => {
      view.values[field.id] = input.value;
      view.armed = '';
      view.error = '';
      if (submit) submit.replaceChildren(tel('span', null, label(chanTyped(view))));
    });
    input.addEventListener('keydown', (key) => {
      if (key.key !== 'Enter' || key.isComposing || key.keyCode === 229) return;
      key.preventDefault();
      const following = grid.querySelectorAll('.chan-input')[index + 1];  // 回车：先跳到下一格，最后一格才提交
      if (following) following.focus(); else if (submit) submit.click();
    });
    if (field.secret) {
      const shown = Boolean(view.show[field.id]);
      // 按钮上的字带着字段的名字（读屏软件要知道是哪一个框）。换语言时整个向导会重画，所以直接写译好的
      const eye = row.appendChild(el('button', 'icon-button'));
      eye.type = 'button';
      eye.title = shown ? t('隐藏 {field}', { field: field.label }) : t('显示 {field}', { field: field.label });
      eye.setAttribute('aria-label', eye.title);
      eye.appendChild(icon('eye'));
      eye.setAttribute('aria-pressed', shown ? 'true' : 'false');
      eye.setAttribute('aria-controls', id);
      eye.dataset.focus = 'eye:' + field.id;
      eye.addEventListener('click', () => { view.show[field.id] = !shown; drawChanWizard('eye:' + field.id); });
    }
  });
  if (secret) {
    const note = pane.appendChild(el('p', 'chan-help', t('这些是机器人的钥匙。它们只存在这台电脑的 channels.json 里（只有你的账号能读），不会发给模型，之后页面上也只显示最后 4 位。')));
    note.id = 'chan-token-help';
  }
  if (item.configured && item.bot) pane.appendChild(chanNote('good', t('现在连着 {bot}（密钥 {hint}）。不换机器人的话，直接下一步。', { bot: chanBot(item), hint: item.token_hint })));
  const previous = foot.appendChild(chanButton(null, k('上一步')));
  previous.dataset.focus = 'previous';
  previous.disabled = view.checking;
  previous.addEventListener('click', () => { if (leaveChanWizard('step:1')) chanGo(1); });
  submit = foot.appendChild(chanButton(null, view.checking ? k('正在验证……') : label(chanTyped(view)), 'primary'));
  submit.dataset.focus = 'submit';
  submit.disabled = view.checking;
  submit.addEventListener('click', () => chanConnect(item));
  if (view.checking) retext(help, k('正在问 {platform} 认不认……'), { platform: item.label });
  else if (!view.armed) retext(help, k('只检查这一次：智狐拿它去问 {platform}“这是哪个机器人”，认的话就记下来。'), { platform: item.label });
}

async function chanConnect(item) {
  const view = chanView;
  const given = Object.fromEntries(Object.entries(view.values).map(([name, value]) => [name, String(value).trim()]).filter(([, value]) => value));
  if (!Object.keys(given).length) {
    if (item.configured) { chanGo(item.owner ? 4 : 3); return; }
    view.error = (item.fields || []).length > 1 ? t('先把这几项粘贴进来。') : t('先把 token 粘贴进来。');
    drawChanWizard('step');
    return;
  }
  view.checking = true;
  view.error = '';
  drawChanWizard('submit');
  const reply = await call('/api/channel/connect', { platform: item.id, credentials: given });
  if (chanView !== view) return;  // 等回答的时候你已经离开了这个向导
  view.checking = false;
  if (!reply.ok) {
    view.error = reply.error;
    drawChanWizard('step');
    return;
  }
  view.values = {};
  view.show = {};
  const before = chanBot(item);
  view.connected = reply.data.bot ? reply.data.bot.handle || reply.data.bot.name : '';
  announce(t('已连接 {bot}。', { bot: view.connected }));
  const fresh = chanFind(item.id);
  view.pairedAt = fresh && fresh.owner ? fresh.owner.paired_at : null;
  chanGo(3);
  // 换成了另一个机器人：原来的绑定作废了（见 store.connect），要重新配对。状态马上就到，等它到了再看要不要生成配对码
  if (fresh && fresh.owner && view.connected && before && before !== view.connected) chanPair();
}

async function chanPair() {
  const view = chanView;
  if (view.kind !== 'wizard' || view.pairing) return;
  view.pairing = true;
  view.error = '';
  const item = chanFind(view.platform);
  view.pairedAt = item && item.owner ? item.owner.paired_at : null;
  drawChanWizard();
  const reply = await call('/api/channel/pair', { platform: view.platform });
  if (chanView !== view) return;
  view.pairing = false;
  if (!reply.ok) view.error = reply.error;
  drawChanWizard(reply.ok ? 'copy' : null);
}

function chanStepThree(item, pane, foot, help) {
  const view = chanView;
  const bot = chanBot(item);
  const paired = item.owner && item.owner.paired_at !== view.pairedAt;  // 打开这一步以后才配对上的
  if (view.connected) pane.appendChild(chanNote('good', t('已连接 {bot}。', { bot: view.connected })));
  if (paired) {
    const box = pane.appendChild(el('div', 'chan-paired'));
    box.setAttribute('role', 'status');
    const check = box.appendChild(el('span', 'chan-check'));
    check.appendChild(icon('check'));
    const words = box.appendChild(el('div'));
    words.appendChild(el('b', null, t('配对成功：{name}', { name: item.owner.name })));
    words.appendChild(el('span', null, t('以后只有这个账号能通过 {bot} 使用智狐，别人发的消息一律不理。', { bot })));
    if (chanPanel.dataset.paired !== String(item.owner.paired_at)) {  // 刚配对上：对勾弹一下，读屏软件也说一声
      chanPanel.dataset.paired = String(item.owner.paired_at);
      requestAnimationFrame(() => play(check, [{ transform: 'scale(.4)', opacity: 0 }, { transform: 'scale(1.12)', opacity: 1, offset: 0.7 }, { transform: 'none', opacity: 1 }], 520));
      announce(t('配对成功：{name}', { name: item.owner.name }));
    }
    const next = foot.appendChild(chanButton(null, k('下一步'), 'primary'));
    next.dataset.focus = 'next';
    next.addEventListener('click', () => chanGo(4));
    requestAnimationFrame(() => { if (chanPanel.contains(document.activeElement) && document.activeElement.dataset.focus === 'copy') next.focus(); });
    return;
  }
  const code = item.pairing;
  if (code) {
    pane.appendChild(el('h4', null, t('用你自己的账号，把这 6 位数字发给 {bot}', { bot })));
    const digits = pane.appendChild(el('div', 'chan-code'));
    digits.setAttribute('role', 'img');
    digits.setAttribute('aria-label', t('配对码：{code}', { code: code.code.split('').join(' ') }));
    const fresh = digits.dataset.code !== code.code && chanPanel.dataset.code !== code.code;
    chanPanel.dataset.code = code.code;
    Array.from(code.code).forEach((digit, index) => {
      if (index === 3) digits.appendChild(el('span', 'chan-gap'));
      const box = digits.appendChild(el('span', 'chan-digit', digit));
      if (fresh) play(box, [{ opacity: 0, transform: 'translateY(-10px)' }, { opacity: 1, transform: 'none' }], 300 + index * 60);
    });
    const timer = pane.appendChild(el('div', 'chan-timer'));
    const bar = timer.appendChild(el('i'));
    const total = 600, left = Math.max(0, code.expires - Date.now() / 1000);
    bar.style.setProperty('--from', String(Math.min(1, left / total)));
    bar.style.setProperty('--left', String(Math.min(1, left / total)));
    bar.style.animationDuration = left + 's';
    const meta = pane.appendChild(el('div', 'chan-code-meta'));
    const clock = meta.appendChild(el('span', 'chan-left'));
    clock.dataset.expires = String(code.expires);
    clock.textContent = t('还有 {time} 过期', { time: durationText(left) });
    if (code.attempts_left < 5) meta.appendChild(el('span', null, t('还能猜错 {n} 次', { n: code.attempts_left })));
    const tools = pane.appendChild(el('div', 'chan-code-tools'));
    const copy = tools.appendChild(chanButton('copy', k('复制配对码')));
    copy.dataset.focus = 'copy';
    copy.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(code.code); retext(help, k('已复制。')); help.className = 'chan-help good'; }
      catch (_) { retext(help, k('没能复制，请照着上面的数字输入。')); help.className = 'chan-help bad'; }
    });
    // Discord 要先把机器人加到你的账号，才能和它私聊（链接是助手程序给的，见 channels/discord.check）
    if (item.bot && item.bot.install) {
      const install = tools.appendChild(chanLink(item.bot.install, k('把机器人加到你的账号')));
      install.dataset.focus = 'install';
    }
    // 打开和机器人的聊天。Telegram 的这个链接还会替你发一句 /start 123456，点"开始"就配对上了（链接是助手程序算的，见 store.pair_link）
    if (code.link) {
      const open = tools.appendChild(chanLink(code.link, k('在 {platform} 里打开')));
      retext(open.querySelector('span'), k('在 {platform} 里打开'), { platform: item.label });
      open.dataset.focus = 'open-chat';
    }
    const how = chanNote('calm', (item.pair ? item.pair.replace('{bot}', bot) + ' ' : '') + t('配对上以后这里会自己往下走。别人发来的配对码不对的话，猜错 5 次这个码就作废了。'));
    pane.appendChild(how);
    const skip = foot.appendChild(chanButton(null, k('以后再配')));
    skip.dataset.focus = 'skip';
    skip.addEventListener('click', async () => {
      await call('/api/channel/cancel', { platform: item.id });
      chanGo(4);
    });
    const again = foot.appendChild(chanButton('refresh', k('换一个配对码')));
    again.dataset.focus = 'again';
    again.addEventListener('click', chanPair);
    retext(help, k('等你从 {platform} 发过来……'), { platform: item.label });
    return;
  }
  // 没有正在进行的配对：要么还没生成（马上就有），要么过期了、猜错太多作废了，要么已经配对过、想换一个账号
  if (view.pairing) {
    pane.appendChild(el('h4', null, t('正在生成配对码……')));
    return;
  }
  if (item.owner) {
    pane.appendChild(el('h4', null, t('已经配对过了')));
    pane.appendChild(el('p', null, t('现在的主人是 {name}（{when}配对的）。重新配对的话，配好以后只有新的那个账号能用，原来的那个不再理会。', { name: item.owner.name, when: fromNow(item.owner.paired_at) })));
    const repair = foot.appendChild(chanButton('link', k('重新配对')));
    repair.dataset.focus = 'repair';
    repair.addEventListener('click', chanPair);
    const next = foot.appendChild(chanButton(null, k('下一步'), 'primary'));
    next.dataset.focus = 'next';
    next.addEventListener('click', () => chanGo(4));
    return;
  }
  pane.appendChild(el('h4', null, t('配对码过期了')));
  pane.appendChild(el('p', null, t('配对码十分钟内有效；有人猜错 5 次，它也会作废。生成一个新的再试。')));
  const regenerate = foot.appendChild(chanButton('refresh', k('生成新的配对码'), 'primary'));
  regenerate.dataset.focus = 'regenerate';
  regenerate.addEventListener('click', chanPair);
}

function chanStepFour(item, pane, foot, help) {
  const view = chanView;
  pane.appendChild(el('h4', null, t('在聊天里，动手之前要不要先问你')));
  pane.appendChild(el('p', null, item.buttons
    ? t('这一档只管这个通道，和网页上对话里选的、定时任务选的都无关。要问你的时候，聊天里会发来一条消息，写着完整的命令和工作目录，下面是“允许 / 拒绝”两个按钮；网页上也能答复，先到的算数。')
    : t('这一档只管这个通道，和网页上对话里选的、定时任务选的都无关。要问你的时候，聊天里会发来一条消息，写着完整的命令和工作目录，回复“允许”或“拒绝”就行；网页上也能答复，先到的算数。')));
  const group = pane.appendChild(el('div', 'chan-modes'));
  group.setAttribute('role', 'radiogroup');
  group.setAttribute('aria-label', t('确认方式'));
  const choose = (mode, focus) => { view.mode = mode; view.armed = ''; drawChanWizard(focus ? 'mode:' + mode : null); };
  for (const option of CHAN_MODES) {
    const button = group.appendChild(el('button', 'chan-mode'));
    button.type = 'button';
    button.setAttribute('role', 'radio');
    button.dataset.mode = option.mode;
    button.dataset.focus = 'mode:' + option.mode;
    const on = view.mode === option.mode;
    button.setAttribute('aria-checked', on ? 'true' : 'false');
    button.tabIndex = on ? 0 : -1;  // 单选组只有选中的那个在 Tab 键的顺序里，其余的用方向键去
    button.append(el('b', null, t(APPROVAL[option.mode])), el('span', null, t(option.text)), icon('check'));
    button.addEventListener('click', () => choose(option.mode, true));
  }
  group.addEventListener('keydown', (key) => {
    const order = CHAN_MODES.map((option) => option.mode), at = order.indexOf(view.mode);
    const next = { ArrowDown: at + 1, ArrowRight: at + 1, ArrowUp: at - 1, ArrowLeft: at - 1, Home: 0, End: order.length - 1 }[key.key];
    if (next === undefined) return;
    key.preventDefault();
    choose(order[(next + order.length) % order.length], true);
  });
  if (view.mode === 'all') {
    pane.appendChild(chanNote('bad', t('确认是你和模型之间唯一的一道关卡。全部自动允许以后，聊天里的一句话就能让助手在这台电脑上执行任何命令，不再问你；网页或文件里藏着误导模型的话，它照做的时候也没人拦着。只在你清楚风险的时候用。')));
  }
  if (!item.owner) pane.appendChild(chanNote('warn', t('还没有配对：在配对之前，谁发来的消息都不会处理。')));
  const done = foot.appendChild(chanButton('check', k('完成'), 'primary'));
  done.dataset.focus = 'done';
  done.addEventListener('click', () => chanFinish(item, done));
  if (!view.armed) retext(help, item.approval === view.mode ? k('现在用的就是这一档。') : k('点“完成”才会改。'));
}

async function chanFinish(item, button) {
  const view = chanView;
  if (view.mode !== item.approval) {
    button.disabled = true;
    const reply = await call('/api/channel/approval', { platform: item.id, mode: view.mode });
    if (chanView !== view) return;
    if (!reply.ok) { button.disabled = false; view.error = reply.error; drawChanWizard('done'); return; }
  }
  chanView = { kind: 'list' };
  clearInterval(chanTicker);
  chanTicker = 0;
  drawChanList('open:' + item.id);
  const bot = chanBot(item);
  const key = item.owner ? k('{platform} 准备好了：给 {bot} 发一条消息试试。') : k('{platform} 的设置已保存。配对以后就能用了。');
  extensionSay(key, { platform: item.label, bot }, 'good');
  announce(t(key, { platform: item.label, bot }));
}

// 配对码的倒计时：只在向导的第 3 步、配对码显示着的时候走。改的是文字（和减少动画时那根线停在哪儿），不是动画
function syncChanTicker() {
  const need = chanView.kind === 'wizard' && chanView.step === 3 && chanPanel.querySelector('.chan-left');
  if (need && !chanTicker) chanTicker = setInterval(onChanTick, 1000);
  if (!need && chanTicker) { clearInterval(chanTicker); chanTicker = 0; }
}

function onChanTick() {
  const clock = chanPanel.querySelector('.chan-left');
  if (!clock) { syncChanTicker(); return; }
  const left = Number(clock.dataset.expires) - Date.now() / 1000;
  if (left <= 0) { chanDrawn = ''; drawChannels(); return; }  // 过期了：助手程序不会为此发状态，自己画成"过期了"
  clock.textContent = t('还有 {time} 过期', { time: durationText(left) });
  const bar = chanPanel.querySelector('.chan-timer i');
  if (bar) bar.style.setProperty('--left', String(Math.min(1, left / 600)));
}

// ---- 挂到"扩展"对话框上 ----

extensionTabs.channels = {
  open() {
    chanDrawn = '';
    drawChannels();
  },
  beforeClose() {
    return chanView.kind !== 'wizard' || leaveChanWizard('close');
  },
};
extensionsDialog.addEventListener('keydown', (key) => {
  if (key.key !== 'Escape' || key.isComposing || key.keyCode === 229 || extensionTab !== 'channels' || !chanPanel.contains(document.activeElement) && document.activeElement !== extensionsDialog) return;
  if (chanDeleting) {  // 这一下 Esc 只用来取消删除
    key.preventDefault();
    const platform = chanDeleting;
    chanDeleting = null;
    drawChanList('open:' + platform);
  } else if (chanView.kind === 'wizard') {  // 在向导里：先回到列表，不直接关掉对话框
    key.preventDefault();
    key.stopPropagation();
    chanBack();
  }
});
extensionsDialog.addEventListener('close', () => {
  chanView = { kind: 'list' };
  chanDeleting = null;
  chanDrawn = '';
  clearInterval(chanTicker);
  chanTicker = 0;
  delete chanPanel.dataset.view;
  chanPanel.replaceChildren();  // 向导里可能有刚填的 token：关掉就清掉
});
