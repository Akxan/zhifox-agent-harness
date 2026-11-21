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
