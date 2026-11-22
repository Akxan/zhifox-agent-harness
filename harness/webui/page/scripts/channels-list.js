// ---- 扩展 · 通道 · 列表 -----------------------------------------------------
// 通道一页的第一层：已经接上的通道（状态、绑定的账号、聊天里正在做什么）、聊天里等你批准的操作、可以接入的平台。
// 每一行有开关、重新配对、停下聊天里正在做的事，和别处一样的"在访达中显示 / 编辑 / 删除"（删除先在原地确认）。
// 后面一小节是主区域顶上的那条提示（chan-alert）：答的是同一批请求，不用打开对话框也能允许或拒绝。

function drawChanList(focus) {
  // "5 分钟前"这类时间每分钟要重写一次，所以把现在是第几分钟也算进"变了没有"里
  // 每一行写着这个通道的命令在沙箱的哪一档：跟随设置的，设置里那一档变了也要重画
  const print = JSON.stringify([chanState, chanDeleting, [...chanPending], language, Math.floor(Date.now() / 60000), sandboxInfo && sandboxInfo.level]);
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
  const sandbox = sandboxInfo ? sandboxResolved(item.sandbox || 'default') : 'off';
  if (sandbox !== 'off') {
    const level = sandboxLevel(sandbox);
    bits.push(t('沙箱：{level}', { level: sandboxValue(level && level.label) }));
  }
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
  // 设置里打开了沙箱，这个通道却自己选了"关"：标出来，免得以为它也在沙箱里
  if (sandboxInfo && sandboxInfo.level !== 'off' && item.sandbox === 'off') line.appendChild(el('span', 'chan-flag', t('不用沙箱')));
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
