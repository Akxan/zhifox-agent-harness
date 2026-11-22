// ---- 改了哪些文件：每一轮末尾那一行、撤回、重做 -------------------------------------------
// 一轮里文件工具改了文件，这一轮的末尾就有一行"改了 N 个文件"（file_changes 事件，规矩见 harness/undo/__init__.py）。
// 收着的时候右边一个"撤回"，点一下撤回这一轮的全部改动；点开是每个文件：新建 / 修改 / 覆盖、加减了几行，
// 点文件名看改了什么（一行一行的预览由助手程序算好，页面只用 textContent 画），每个文件一个"撤回"，最下面"撤回这一轮的全部改动"。
// 撤回、重做本身也是一个事件（file_undo）：在发生的地方多一行"撤回了 a.txt 的改动"（带一个"重做"），原来那一行上的文件跟着变成"已撤回"。
// 每个文件现在的状态由这些事件推出来（和助手程序的 harness/undo/restore.py 的 fold 是同一个办法），刷新、换语言、另一个标签页都一样。
// 这一轮之后文件又被改过、后面的几轮也改过它、在工作目录之外……助手程序先不做，把原因交回来：在那一行里原地说清楚，
// 你点"仍然撤回"才做；做不了的（保存密钥的文件、太大没留底……）一起列出来，写着为什么。为什么的那句话是助手程序给的（带着原文，换语言时重译）。
// 命令改动的文件不在这里：一条命令改了什么看不清楚，没法自动撤回，那一行里写明。

const changeSets = new Map();  // 一轮改动的编号 → 那一行：{set, box, files, states, rows, title, total, all, footAll, body, toggle, warn, pending}
const undoLogs = [];           // 每一行"撤回了……"：{node, set, action, files, redo}
let changesSeq = 0;            // 给展开的那一块编号（aria-controls 要一个 id）

const CHANGE_KINDS = { created: k('新增'), modified: k('修改'), overwritten: k('覆盖') };
const CHANGE_PLACES = { outside: k('工作目录之外'), private: k('数据目录里') };
const CHANGE_SKIPPED = {
  big: k('太大，没有留底，撤不回来'), budget: k('这一轮留的底超了上限，撤不回来'), unreadable: k('当时读不出来，没有留底，撤不回来'),
};

// 页面要整个重画了（reset）：旧的那些行跟着没了
function dropFileChanges() {
  changeSets.clear();
  undoLogs.length = 0;
}

const changeCount = (value) => (Number.isInteger(value) && value >= 0 ? value : null);

function fileChangesLine(event) {
  endBlock();
  const files = (Array.isArray(event.files) ? event.files : [])
    .filter((file) => file && typeof file.path === 'string' && Object.hasOwn(CHANGE_KINDS, file.kind));
  const set = typeof event.set === 'string' ? event.set : '';
  const id = ++changesSeq;
  const box = el('div', 'changes');
  box.setAttribute('role', 'group');
  const head = box.appendChild(el('div', 'changes-head'));
  const toggle = head.appendChild(el('button', 'changes-toggle'));
  toggle.type = 'button';
  toggle.setAttribute('aria-expanded', 'false');
  toggle.setAttribute('aria-controls', 'changes-body-' + id);
  toggle.appendChild(icon('diff', 'changes-icon'));
  const title = toggle.appendChild(el('span', 'changes-title'));
  const total = toggle.appendChild(el('span', 'changes-total'));
  toggle.appendChild(icon('chevron', 'chev'));
  const all = head.appendChild(el('button', 'button changes-all'));
  all.type = 'button';

  const body = box.appendChild(el('div', 'changes-body'));
  body.id = 'changes-body-' + id;
  body.hidden = true;
  const list = body.appendChild(el('ul', 'changes-files'));
  const untracked = changeCount(event.untracked) || 0;
  if (untracked) body.appendChild(tel('p', 'changes-note', k('还有 {n} 个文件改了但没有记下（一轮最多记这么多），撤不回来。'), { n: untracked }));
  const honest = body.appendChild(el('p', 'changes-note' + (event.commands ? ' warn' : '')));
  if (event.commands) honest.appendChild(icon('alert'));
  honest.appendChild(tel('span', null, event.commands ? k('这一轮还执行过命令：命令改动的文件没法自动撤回。') : k('这里只有文件工具做的改动；执行命令改动的文件没法自动撤回。')));
  const warn = body.appendChild(el('div', 'changes-warn'));
  warn.hidden = true;
  const foot = body.appendChild(el('div', 'changes-foot'));
  const footAll = foot.appendChild(el('button', 'button changes-foot-all'));
  footAll.type = 'button';

  const entry = { id, set, box, files, states: files.map(() => ({ undone: false, by: null })), rows: [], title, total, all, footAll, body, toggle, warn, pending: false, trigger: null };
  entry.rows = files.map((file, index) => changeRow(entry, file, index));
  list.append(...entry.rows.map((row) => row.item));
  if (!set || !files.length) { all.hidden = true; foot.hidden = true; }
  toggle.addEventListener('click', () => setChangesOpen(entry, body.hidden));
  all.addEventListener('click', () => changeAll(entry, all));
  footAll.addEventListener('click', () => changeAll(entry, footAll));
  box.addEventListener('keydown', (key) => {
    if (key.key === 'Escape' && !key.isComposing && !warn.hidden) { key.preventDefault(); key.stopPropagation(); closeChangesWarn(entry, true); }
  });
  if (set) changeSets.set(set, entry);
  renderChanges(entry);
  add(box);
  if (files.length) announce(t('改了 {n} 个文件', { n: files.length }));
}

function changeRow(entry, file, index) {
  const item = el('li', 'changes-file');
  const row = item.appendChild(el('div', 'changes-row'));
  row.appendChild(tel('span', 'changes-kind ' + file.kind, CHANGE_KINDS[file.kind]));
  const name = row.appendChild(el('button', 'changes-name'));
  name.type = 'button';
  name.title = file.path;
  name.setAttribute('aria-expanded', 'false');
  const path = el('span', 'changes-path', file.path);
  path.id = 'changes-path-' + entry.id + '-' + index;
  name.append(icon('chevron', 'chev'), path);
  const added = changeCount(file.added), removed = changeCount(file.removed);
  if (added !== null && removed !== null) {
    const lines = row.appendChild(el('span', 'changes-lines'));
    lines.append(el('span', 'changes-plus', '+' + added), el('span', 'changes-minus', '−' + removed));
  }
  if (Object.hasOwn(CHANGE_PLACES, file.place)) row.appendChild(tel('span', 'changes-tag', CHANGE_PLACES[file.place]));
  const skipped = Object.hasOwn(CHANGE_SKIPPED, file.skipped);
  if (skipped) row.appendChild(tel('span', 'changes-tag warn', CHANGE_SKIPPED[file.skipped]));
  const done = row.appendChild(tel('span', 'changes-state', k('已撤回')));
  done.hidden = true;
  const act = row.appendChild(el('button', 'button changes-act'));
  act.type = 'button';
  act.dataset.path = path.id;  // 读屏软件念的是"撤回 a.txt"：按钮上的字加上文件名（见 labelChangeButton）
  act.hidden = skipped || !entry.set;  // 没留底的撤不回来：不给按钮，旁边写着为什么
  const diff = item.appendChild(el('div', 'changes-diff'));
  diff.hidden = true;
  const view = { item, name, act, done, diff, loaded: false };
  name.addEventListener('click', () => toggleDiff(entry, view, index));
  act.addEventListener('click', () => {
    entry.trigger = act;
    requestChange(entry, entry.states[index].undone ? 'redo' : 'undo', [index], false);
  });
  return view;
}

// 照着每个文件现在的状态，写那一行的标题、按钮
function renderChanges(entry) {
  const n = entry.files.length;
  const undone = entry.states.filter((state) => state.undone).length;
  if (!n) retext(entry.title, k('有文件改了，但没有记下'));
  else if (!undone) retext(entry.title, k('改了 {n} 个文件'), { n });
  else if (undone === n) retext(entry.title, k('撤回了这一轮改的 {n} 个文件'), { n });
  else retext(entry.title, k('改了 {n} 个文件，撤回了 {m} 个'), { n, m: undone });
  entry.box.classList.toggle('undone', n > 0 && undone === n);
  let plus = 0, minus = 0, counted = false;
  entry.files.forEach((file, index) => {
    const added = changeCount(file.added), removed = changeCount(file.removed);
    if (added === null || removed === null || entry.states[index].undone) return;
    plus += added; minus += removed; counted = true;
  });
  entry.total.replaceChildren(...(counted ? [el('span', 'changes-plus', '+' + plus), el('span', 'changes-minus', '−' + minus)] : []));
  const redoAll = n > 0 && undone === n;
  labelChangeButton(entry.all, redoAll ? 'redo' : 'undo', redoAll ? k('重做') : k('撤回'), redoAll ? k('重做这一轮的全部改动') : k('撤回这一轮的全部改动'));
  labelChangeButton(entry.footAll, redoAll ? 'redo' : 'undo', redoAll ? k('重做这一轮的全部改动') : k('撤回这一轮的全部改动'));
  entry.rows.forEach((row, index) => {
    const isUndone = entry.states[index].undone;
    row.item.classList.toggle('undone', isUndone);
    row.done.hidden = !isUndone;
    labelChangeButton(row.act, isUndone ? 'redo' : 'undo', isUndone ? k('重做') : k('撤回'));
  });
}

// 按钮上的图标和字。label 是读屏软件念的那句（不给就念按钮上的字）；按钮记着 data-path（文件名那个元素的 id）的话，
// 念的是"按钮上的字 + 文件名"（aria-labelledby 指着两个元素：字跟着换语言，文件名原样）
function labelChangeButton(button, kind, text, label) {
  const words = tel('span', null, text);
  button.replaceChildren(icon(kind), words);
  button.dataset.action = kind;
  if (label) tattr(button, 'aria-label', label);
  else if (button.dataset.path) {
    words.id = button.dataset.path + '-act';
    button.setAttribute('aria-labelledby', words.id + ' ' + button.dataset.path);
  }
}

function setChangesOpen(entry, open) {
  const hadFocus = !open && entry.body.contains(document.activeElement);
  entry.body.hidden = !open;
  entry.toggle.setAttribute('aria-expanded', String(open));
  entry.box.classList.toggle('open', open);
  if (open) play(entry.body, [{ opacity: 0, transform: 'translateY(-4px)' }, { opacity: 1, transform: 'none' }], 220);
  if (hadFocus) entry.toggle.focus({ preventScroll: true });  // 拿着焦点的元素被收起来了：交给看得见的开关
}

// 撤回（或者重做）这一轮的全部：还有没撤回的就撤回它们，都撤回了就是重做
function changeAll(entry, button) {
  entry.trigger = button;
  const undone = entry.states.filter((state) => state.undone).length;
  requestChange(entry, undone === entry.files.length ? 'redo' : 'undo', null, false);
}

async function requestChange(entry, action, indices, force) {
  if (busy) { toast(t(BUSY_HINT)); return; }
  if (entry.pending || !entry.set) return;
  entry.pending = true;
  entry.box.classList.add('pending');
  const request = { conversation: state && state.conversation, set: entry.set, force: Boolean(force) };
  if (indices) request.files = indices;
  const result = await call('/api/changes/' + action, request);
  entry.pending = false;
  entry.box.classList.remove('pending');
  const data = result.data || {};
  const asks = Array.isArray(data.asks) ? data.asks : [], blocked = Array.isArray(data.blocked) ? data.blocked : [];
  if (result.ok) {  // 做成了：那一行跟着 file_undo 事件变。做不了的几个（跳过了）在原地说一声
    if (blocked.length) showChangesWarn(entry, action, indices, { asks: [], blocked }, true);
    else closeChangesWarn(entry, false);
    return;
  }
  if (asks.length || blocked.length) { showChangesWarn(entry, action, indices, { asks, blocked }, false); return; }
  toast(result.error);
}

// 原地的提醒：要你确认的、做不了的，一个文件一行，写着为什么。done 是已经做完了、只是说一声哪几个没做成
function showChangesWarn(entry, action, indices, data, done) {
  setChangesOpen(entry, true);
  const warn = entry.warn;
  const redo = action === 'redo';
  warn.replaceChildren();
  const head = warn.appendChild(el('p', 'changes-warn-head'));
  head.append(icon('alert'), tel('span', null, done ? (redo ? k('这几个文件没能重做') : k('这几个文件没能撤回'))
    : redo ? k('重做之前先确认一下') : k('撤回之前先确认一下')));
  const list = warn.appendChild(el('ul', 'changes-warn-list'));
  const line = (item, blocked) => {
    const li = list.appendChild(el('li', blocked ? 'blocked' : ''));
    li.appendChild(el('span', 'changes-warn-path', String(item.path || '')));
    for (const why of Array.isArray(item.why) ? item.why : []) {
      li.appendChild(why && why.key ? tel('span', 'changes-warn-why', why.key, why.values) : el('span', 'changes-warn-why', String((why && why.text) || why || '')));
    }
    if (blocked) li.appendChild(tel('span', 'changes-warn-skip', redo ? k('不重做这一个') : k('不撤回这一个')));
  };
  data.asks.forEach((item) => line(item, false));
  data.blocked.forEach((item) => line(item, true));
  const asked = indices ? indices.length : entry.states.filter((state) => state.undone === redo).length;
  const doable = !done && (data.asks.length > 0 || data.blocked.length < asked);
  const actions = warn.appendChild(el('div', 'changes-warn-actions'));
  let first;
  if (doable) {
    first = actions.appendChild(tel('button', 'button primary', redo ? k('仍然重做') : k('仍然撤回')));
    first.addEventListener('click', () => requestChange(entry, action, indices, true));
    const cancel = actions.appendChild(tel('button', 'button', k('取消')));
    cancel.type = 'button';
    cancel.addEventListener('click', () => closeChangesWarn(entry, true));
  } else {
    first = actions.appendChild(tel('button', 'button', k('知道了')));
    first.addEventListener('click', () => closeChangesWarn(entry, true));
  }
  first.type = 'button';
  warn.hidden = false;
  play(warn, [{ opacity: 0, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], 200);
  first.focus({ preventScroll: true });
  warn.scrollIntoView({ block: 'nearest', behavior: calm() ? 'auto' : 'smooth' });
  announce(warn.textContent);
}

// refocus：焦点交回给点了的那个按钮（Esc、取消、知道了）
function closeChangesWarn(entry, refocus) {
  const warn = entry.warn;
  if (warn.hidden) return;
  const hadFocus = warn.contains(document.activeElement);
  warn.hidden = true;
  warn.replaceChildren();
  if (hadFocus || refocus) {  // 拿着焦点的按钮被藏起来了：交给看得见的地方，不然 Esc 之类的按键就没人接了
    const back = entry.trigger && entry.trigger.isConnected && !entry.trigger.closest('[hidden]') ? entry.trigger : entry.toggle;
    back.focus({ preventScroll: true });
  }
}

// ---- 一个文件改了什么 ----

async function toggleDiff(entry, view, index) {
  const open = view.diff.hidden;
  view.diff.hidden = !open;
  view.name.setAttribute('aria-expanded', String(open));
  view.item.classList.toggle('open', open);
  if (!open) return;
  play(view.diff, [{ opacity: 0, transform: 'translateY(-4px)' }, { opacity: 1, transform: 'none' }], 200);
  if (view.loaded) return;
  view.loaded = true;
  view.diff.replaceChildren(tel('p', 'changes-diff-note', k('正在读取……')));
  const result = await call('/api/changes/diff', { set: entry.set, index });
  if (!result.ok) {
    view.loaded = false;  // 下次点开再试一次
    view.diff.replaceChildren(el('p', 'changes-diff-note error', result.error));
    return;
  }
  renderDiff(view.diff, entry.files[index], result.data);
}

function renderDiff(box, file, data) {
  const parts = [tel('p', 'changes-diff-caption', k('这一轮的改动：{path}'), { path: file.path })];
  const rows = Array.isArray(data.rows) ? data.rows : [];
  if (data.binary) parts.push(tel('p', 'changes-diff-note', k('不是文本文件，没法按行预览。')));
  else if (data.unavailable) parts.push(tel('p', 'changes-diff-note', k('这个文件太大或者没有留底，没法预览改了什么。')));
  else if (!rows.length) parts.push(tel('p', 'changes-diff-note', k('按行比没有不同（只改了换行符或者文件末尾）。')));
  else {
    const code = el('div', 'changes-code');
    for (const row of rows) {
      if (!Array.isArray(row) || row.length < 4) continue;
      const [tag, before, after, text] = row;
      if (tag === '@') { code.appendChild(tel('div', 'changes-hunk', k('第 {n} 行'), { n: Number(after) || Number(before) || 1 })); continue; }
      const kind = tag === '+' ? 'add' : tag === '-' ? 'del' : 'same';
      const line = code.appendChild(el('div', 'changes-line ' + kind));
      line.append(el('span', 'changes-no', before ? String(before) : ''), el('span', 'changes-no', after ? String(after) : ''));
      const sign = line.appendChild(el('span', 'changes-sign', kind === 'add' ? '+' : kind === 'del' ? '−' : ''));
      sign.setAttribute('aria-hidden', 'true');
      if (kind !== 'same') line.appendChild(tel('span', 'sr-only', kind === 'add' ? k('加上的：') : k('删掉的：')));
      line.appendChild(el('span', 'changes-text', String(text)));
    }
    parts.push(code);
    if (Number(data.more) > 0) parts.push(tel('p', 'changes-diff-note', k('还有 {n} 行没有显示。'), { n: Number(data.more) }));
  }
  box.replaceChildren(...parts);
}

// ---- 撤回、重做的那一行 ----

function fileUndoLine(event) {
  endBlock();
  const entry = changeSets.get(event.set);
  const action = event.action === 'redo' ? 'redo' : 'undo';
  const files = (Array.isArray(event.files) ? event.files : [])
    .map((item) => (item && Number.isInteger(item.index) ? item.index : -1))
    .filter((index) => entry ? index >= 0 && index < entry.files.length : index >= 0);
  const paths = (Array.isArray(event.paths) ? event.paths : []).filter((path) => typeof path === 'string');
  const node = el('div', 'changes-log ' + action);
  node.appendChild(icon(action));
  const name = paths[0] || (entry && files.length ? entry.files[files[0]].path : '');
  const n = Math.max(files.length, paths.length);
  const words = action === 'redo'
    ? (n > 1 ? k('重做了 {name} 等 {n} 个文件的改动') : k('重做了 {name} 的改动'))
    : (n > 1 ? k('撤回了 {name} 等 {n} 个文件的改动') : k('撤回了 {name} 的改动'));
  const text = node.appendChild(tel('span', 'changes-log-text', words, { name, n }));
  text.title = paths.join('\n');
  const record = { node, set: event.set, action, files, redo: null };
  if (action === 'undo' && entry) {
    const redo = node.appendChild(el('button', 'button changes-log-redo'));
    redo.type = 'button';
    labelChangeButton(redo, 'redo', k('重做'), k('重做这几个文件的改动'));
    redo.addEventListener('click', () => { entry.trigger = redo; requestChange(entry, 'redo', files, false); });
    record.redo = redo;
  }
  if (entry) {
    for (const index of files) entry.states[index] = { undone: action === 'undo', by: record };
    renderChanges(entry);
    if (isLive()) {
      for (const index of files) play(entry.rows[index].done, [{ opacity: 0, transform: 'scale(.7)' }, { opacity: 1, transform: 'none' }], 260);
    }
  }
  undoLogs.push(record);
  refreshUndoLogs(event.set);
  add(node);
  announce(text.textContent);
}

// 每一行"撤回了……"上的"重做"：这几个文件都还是撤回着的、而且就是这一次撤回的，才放着
function refreshUndoLogs(set) {
  const entry = changeSets.get(set);
  for (const record of undoLogs) {
    if (record.set !== set || !record.redo) continue;
    const live = Boolean(entry) && record.files.length > 0 && record.files.every((index) => entry.states[index].undone && entry.states[index].by === record);
    if (!live && entry && record.redo.contains(document.activeElement)) entry.toggle.focus({ preventScroll: true });
    record.redo.hidden = !live;
  }
}
