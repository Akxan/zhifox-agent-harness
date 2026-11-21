// ---- 会话列表 ------------------------------------------------------------
// 左边栏的会话列表：按时间分组、搜索（在助手程序那边搜，这里标出命中的字）、当前那一段的高亮，
// 每一行悬停时的"在访达中显示 / 重命名 / 删除"。

let filter = '';         // 搜索框里的字
let found = null;        // 搜索结果：{stamp, results}。没在搜索时是 null
let searchSeq = 0;       // 每发起一次搜索加一。回来得晚的旧结果靠它认出来、丢掉
let searchTimer = 0;
let listPrint = '';      // 上一次的会话列表。搜索时列表一变（改名、删除、聊了新的一轮），就重新搜一次
let renaming = null;     // 正在改名的会话：{id, draft}
let deleting = null;     // 正在确认删除的会话编号
let drawn = '';          // 上一次画出来的列表是什么样的；内容没变就不重画
let listShown = false;   // 列表是不是已经亮过相了（第一次出现时，各行依次浮现）
let focusCancel = false; // 刚点了删除：画完之后把焦点放到"取消"上

const DAY = 24 * 60 * 60 * 1000;
const startOfDay = (date) => new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();

function groupOf(seconds) {
  const days = Math.round((startOfDay(new Date()) - startOfDay(new Date(seconds * 1000))) / DAY);
  if (days <= 0) return t('今天');
  if (days === 1) return t('昨天');
  if (days < 7) return t('最近 7 天');
  if (days < 30) return t('最近 30 天');
  return t('更早');
}

function timeLabel(seconds) {
  const date = new Date(seconds * 1000), now = new Date();
  const pad = (number) => String(number).padStart(2, '0');
  if (startOfDay(date) === startOfDay(now)) {  // 今天的只写时间。英文习惯 12 小时制（7:16 PM），中文和西班牙语用 24 小时制
    return language === 'en' ? date.toLocaleTimeString('en', { hour: 'numeric', minute: '2-digit' }) : pad(date.getHours()) + ':' + pad(date.getMinutes());
  }
  // 日期交给浏览器按界面语言去写：中文是"11月20日"，英文是"Nov 20"，西班牙语是"20 nov"
  if (date.getFullYear() === now.getFullYear()) return date.toLocaleDateString(LOCALES[language], { month: 'short', day: 'numeric' });
  return date.toLocaleDateString(LOCALES[language], { year: 'numeric', month: 'numeric', day: 'numeric' });
}

// 把一段文字放进节点里，marks 指出的那几处（[[起, 止], ...]，由服务器算好）标出来。
// 标记是用 DOM 接口一个个创建的，不拼接 HTML。
function paint(node, text, marks) {
  let last = 0;
  for (const [from, to] of marks || []) {
    if (from < last || to > text.length) continue;  // 位置对不上就不标，不能因此把文字弄丢
    node.append(text.slice(last, from), el('mark', null, text.slice(from, to)));
    last = to;
  }
  node.append(text.slice(last));
  return node;
}

// 搜索在服务器上做（会话的正文不在页面里）。敲字的时候稍等一下再发，免得每敲一个字就搜一次。
function runSearch() {
  clearTimeout(searchTimer);
  const query = filter.trim();
  const mine = ++searchSeq;
  if (!query) { found = null; renderConversations(); return; }
  if (!found) renderConversations();  // 第一次搜：先显示"正在搜索"。之后的每次，新结果回来之前先留着旧的
  searchTimer = setTimeout(async () => {
    const reply = await call('/api/search', { query });
    if (mine !== searchSeq) return;  // 这期间又敲了字，这次的结果已经过时了
    found = { stamp: mine, results: reply.ok ? reply.data.results || [] : [], error: reply.ok ? '' : reply.error };
    renderConversations();
  }, 140);
}

// 把高亮块摆到当前会话那一行的位置上。jump 为真时直接到位，否则滑过去
function placePill(jump) {
  const active = rows.querySelector('.conv.active');
  if (!active || !active.offsetParent) {
    // 光把它变透明不够：它还占着原来的位置，列表下面会多出一段能滚动的空白。位置和高度也收回去
    pill.classList.remove('on');
    pill.classList.add('jump');
    pill.style.transform = 'translateY(0)';
    pill.style.height = '0';
    void pill.offsetWidth;
    pill.classList.remove('jump');
    return;
  }
  const sudden = jump || !pill.classList.contains('on') || !synced;  // 刚连上、还在补发旧内容时直接到位；之后换会话都滑过去
  if (sudden) pill.classList.add('jump');
  pill.style.transform = 'translateY(' + active.offsetTop + 'px)';
  pill.style.height = active.offsetHeight + 'px';  // 搜索结果那几行带着摘录，比平时高
  if (sudden) {
    void pill.offsetWidth;  // 读一下尺寸，让浏览器先把"直接到位"算完，再恢复滑动的过渡
    pill.classList.remove('jump');
  }
  pill.classList.add('on');
}

const KINDS = { user: k('你'), assistant: k('助手'), tool: k('工具'), thinking: k('思考'), result: k('结果') };
const ON_MAC = /Mac|iPhone|iPad/.test(navigator.platform);
const SHOW_FILE = ON_MAC ? k('在访达中显示') : k('在文件夹中显示');

// 在文件管理器里显示了一个文件之后说的那句话。助手程序所在的电脑没有文件管理器时（服务器上，状态里的 file_manager
// 是 false），什么都打不开，就改成告诉你文件在哪儿，给完整的路径
function revealedText(path) {
  const full = String(path || '');
  if (state && state.file_manager === false) return t('这台电脑上没有文件管理器。文件在：{path}', { path: full });
  const parts = full.split('/');
  const shown = (parts.length > 3 ? '…/' : '') + parts.slice(-3).join('/');
  return ON_MAC ? t('已在访达中显示：{path}', { path: shown }) : t('已在文件夹中显示：{path}', { path: shown });
}

// 会话列表里，在聊天通道里聊的那几段标着平台的名字（名字由助手程序给，见 channels.js）
const channelLabel = (id) => { const item = chanFind(id); return item ? item.label : id; };


// 请助手程序在系统的文件管理器里显示一段会话的文件（编号为空时显示保存会话的那个文件夹）。
// 页面只说"哪一段"，文件在哪儿由助手程序自己算，页面指定不了别的路径。
async function showFile(id) {
  const reply = await call('/api/conversation/reveal', { id });
  if (!reply.ok) { toast(reply.error); return; }
  toast(revealedText(reply.data.path));
}

function renderConversations() {
  if (!state) return;
  const all = state.conversations;
  if (renaming && !all.some((item) => item.id === renaming.id)) renaming = null;
  if (deleting && !all.some((item) => item.id === deleting)) deleting = null;

  const query = filter.trim();
  const list = JSON.stringify(all);
  if (query && found && list !== listPrint) runSearch();  // 列表变了，搜索结果可能也跟着变
  listPrint = list;

  // 状态隔一会儿就会更新一次（比如用量变了），列表多半没变。没变就不重画，
  // 免得鼠标正悬在某一行上、或者正在改名时，那一行被换成新的。
  const print = JSON.stringify([list, state.conversation, query, found && found.stamp, renaming && renaming.id, deleting, startOfDay(new Date()), language]);
  if (print === drawn) return;
  drawn = print;

  const nodes = [];
  let count = 0;
  const addRow = (item) => {
    const row = conversationRow(item);
    if (!listShown && count < 12) {  // 列表第一次出现时，前几行依次浮现
      row.classList.add('enter');
      row.style.animationDelay = count * 24 + 'ms';
      // 播完就把标记去掉，免得会话栏收起再展开时又播一遍
      row.addEventListener('animationend', () => { row.classList.remove('enter'); row.style.animationDelay = ''; }, { once: true });
    }
    count += 1;
    nodes.push(row);
  };

  if (!query) {
    // 平时：按时间分组
    let group = null;
    for (const item of all) {
      const name = groupOf(item.updated_at);
      if (name !== group) nodes.push(el('div', 'conv-group', group = name));
      addRow(item);
    }
    if (!all.length) nodes.push(el('div', 'conv-empty', t('还没有保存的会话。发出第一条消息后，它会出现在这里，下次打开还能接着聊。')));
  } else if (!found) {
    nodes.push(el('div', 'conv-group', t('正在搜索……')));
  } else if (found.error) {
    nodes.push(el('div', 'conv-empty', found.error));
  } else {
    // 搜索：先是每个关键词都原样找到的，然后是只对得上大概的
    const known = new Set(all.map((item) => item.id));
    const results = found.results.filter((item) => known.has(item.id));  // 刚被删掉的不再显示
    const exact = results.filter((item) => item.exact), near = results.filter((item) => !item.exact);
    if (exact.length) nodes.push(el('div', 'conv-group', exact.length === 1 ? t('找到 1 段会话') : t('找到 {n} 段会话', { n: exact.length })));
    exact.forEach(addRow);
    if (near.length) nodes.push(el('div', 'conv-group', exact.length ? t('相近的结果') : t('没有完全对得上的，这些比较接近')));
    near.forEach(addRow);
    if (!results.length) nodes.push(el('div', 'conv-empty', t('没有找到相关的会话。可以换个说法，或者只输入其中的几个字；多个关键词用空格隔开。')));
    if (found.results.length >= 50) nodes.push(el('div', 'conv-empty', t('只显示最相关的 50 段。')));
  }
  if (count) listShown = true;
  rows.replaceChildren(...nodes);
  $('nav-count').textContent = !all.length ? t('会话只保存在这台电脑上')
    : all.length === 1 ? t('1 段会话 · 只保存在这台电脑上') : t('{n} 段会话 · 只保存在这台电脑上', { n: all.length });
  placePill();

  const editor = rows.querySelector('.conv-edit');
  if (editor) {
    editor.focus();
    editor.setSelectionRange(editor.value.length, editor.value.length);
  } else if (focusCancel) {
    const cancel = rows.querySelector('.conv-confirm .cancel');
    if (cancel) cancel.focus();
  }
  focusCancel = false;
}

// 列表里的一行。item 平时是会话的基本信息；搜索时还带着命中的情况（title_marks、snippet）
function conversationRow(item) {
  const isCurrent = item.id === state.conversation;
  const row = el('div', 'conv' + (isCurrent ? ' active' : '') + (item.snippet ? ' hit' : ''));

  // 删除前先在原地确认一下，不弹对话框
  if (deleting === item.id) {
    const box = row.appendChild(el('div', 'conv-confirm'));
    box.appendChild(el('span', null, t('删除这段会话？')));
    const yes = box.appendChild(el('button', 'mini danger', t('删除')));
    const no = box.appendChild(el('button', 'mini cancel', t('取消')));
    yes.type = no.type = 'button';
    yes.addEventListener('click', async () => {
      deleting = null;
      await post('/api/conversation/delete', { id: item.id });
      renderConversations();
    });
    no.addEventListener('click', () => { deleting = null; renderConversations(); });
    box.addEventListener('keydown', (key) => {
      if (key.key !== 'Escape') return;
      key.preventDefault();  // 这一下 Esc 只用来取消删除，不再去收起窄屏上的会话栏
      deleting = null;
      renderConversations();
      search.focus();
    });
    return row;
  }

  // 改名：标题原地变成输入框。回车或点别处保存，Esc 取消
  if (renaming && renaming.id === item.id) {
    const editor = row.appendChild(el('input', 'conv-edit'));
    editor.type = 'text';
    editor.maxLength = 40;  // 标题最长保留 40 个字（服务器那边也是这样截的）
    editor.value = renaming.draft;
    editor.setAttribute('aria-label', t('会话标题'));
    const finish = async (save) => {
      if (!renaming || renaming.id !== item.id) return;  // 回车和失去焦点可能各触发一次，只处理第一次
      const title = renaming.draft.trim();
      renaming = null;
      if (save && title && title !== item.title) await post('/api/conversation/rename', { id: item.id, title });
      renderConversations();
    };
    editor.addEventListener('input', () => { if (renaming) renaming.draft = editor.value; });
    editor.addEventListener('keydown', (key) => {
      if (key.isComposing || key.keyCode === 229) return;  // 输入法正在组字
      if (key.key === 'Enter') { key.preventDefault(); finish(true); }
      if (key.key === 'Escape') { key.preventDefault(); finish(false); }
    });
    editor.addEventListener('blur', () => finish(true));
    return row;
  }

  const open = row.appendChild(el('button', 'conv-main'));
  open.type = 'button';
  open.title = item.title;
  if (isCurrent) open.setAttribute('aria-current', 'true');
  paint(open.appendChild(el('span', 'conv-title')), item.title, item.title_marks);
  const hit = item.snippet;
  if (hit) {  // 搜索结果：第二行是命中的那段文字，前面标明是谁说的
    const line = open.appendChild(el('span', 'conv-snippet'));
    line.appendChild(el('span', 'conv-kind', KINDS[hit.kind] ? t(KINDS[hit.kind]) : ''));
    paint(line, hit.text, hit.marks);
  } else {
    // 定时任务跑出来的会话标一下，免得以为是自己聊的；用 Agent 开始的，前面是那个 Agent 的名字（带着它的颜色）
    const meta = open.appendChild(el('span', 'conv-meta'));
    if (item.agent) meta.appendChild(el('span', 'persona-badge', item.agent)).dataset.color = (agentByName(item.agent) || {}).color || '';
    const origin = item.task ? t('定时任务') + ' · ' : item.channel ? channelLabel(item.channel) + ' · ' : '';  // 在聊天通道里聊的，标着是哪个平台
    meta.appendChild(document.createTextNode(origin + item.model + ' · ' + timeLabel(item.updated_at)));
  }
  open.addEventListener('click', () => {
    const target = hit ? { id: item.id, kind: hit.kind, find: hit.find } : null;
    if (isCurrent) { if (target) reveal(target); return; }
    if (busy) { toast(t(BUSY_HINT)); return; }
    jump = target;  // 打开之后滚到命中的位置（见 settle）
    post('/api/conversation/open', { id: item.id });
    if (overlayNav.matches) setNav(false, false);  // 窄屏上会话栏盖着对话，选完就收起来
  });

  const actions = row.appendChild(el('div', 'conv-actions'));
  const locate = actions.appendChild(iconButton('folder', SHOW_FILE));
  locate.addEventListener('click', () => showFile(item.id));
  const rename = actions.appendChild(iconButton('edit', k('重命名')));
  const remove = actions.appendChild(iconButton('trash', k('删除')));
  rename.classList.add('idle-only');  // 助手工作时这两个按钮藏起来（服务器这时也不让改）
  remove.classList.add('idle-only');
  rename.addEventListener('click', () => { deleting = null; renaming = { id: item.id, draft: item.title }; renderConversations(); });
  remove.addEventListener('click', () => { renaming = null; deleting = item.id; focusCancel = true; renderConversations(); });
  return row;
}

search.addEventListener('input', (typed) => {
  if (typed.isComposing) return;  // 输入法还在组字（比如拼音打到一半），等选定了字再搜
  filter = search.value;
  runSearch();
});
search.addEventListener('compositionend', () => { filter = search.value; runSearch(); });
search.addEventListener('keydown', (key) => {
  if (key.isComposing || key.keyCode === 229) return;  // 输入法正在组字
  if (key.key === 'Escape') {  // Esc：先清空搜索词，再按一次离开搜索框
    key.preventDefault();
    if (search.value) { search.value = filter = ''; runSearch(); } else search.blur();
  } else if (key.key === 'Enter') {  // 回车：打开第一条结果（结果还没回来的时候不动，免得打开的是上一次搜的）
    key.preventDefault();
    const first = found && found.stamp === searchSeq ? rows.querySelector('.conv-main') : null;
    if (first) first.click();
  }
});
$('search-key').textContent = ON_MAC ? '⌘F' : 'Ctrl F';
tattr($('open-folder'), 'title', k('打开保存会话的文件夹'));
$('open-folder').addEventListener('click', () => showFile(''));
