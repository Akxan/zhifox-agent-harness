// ---- 记忆 ----------------------------------------------------------------
// 入口在左边栏顶部（"记忆"），点开是一个左右两栏的窗口：左边是全部记忆（可以按名字搜），右边是选中的那一条。
// 以前放在右边的信息栏里，记忆一多信息栏就被拉得很长。
// 助手的长期记忆是 data/memories/ 下的普通文件，模型用 memory 工具读写。这里把它们列出来：点一条看内容，
// 每一行悬停时有三个按钮（和会话行一样）：在文件管理器里显示这个文件、编辑（名字和内容）、删除。
// 列表跟着状态走：服务器每次发状态都重新看一遍文件夹，所以模型在这一轮里记下的东西，下一次刷新状态时就出现了，
// 页面不用自己去轮询。页面只拿到相对路径，查看和删除时原样交回去；文件到底在哪儿由服务器重新算，出不了那个文件夹。

const memoryList = $('memories'), memoryDialog = $('memory-dialog');
const MAX_MEMORIES = 500;   // 和助手程序那边列表的上限一致
let memories = [];          // 最新的记忆列表：[{path, size, modified}]
let knownMemories = null;   // 上一次画的时候有哪些。新出现的（刚记下的）那几条浮现出来
let memoriesDrawn = '';
let forgetting = null;      // 正在原地确认删除的那一条（路径）
let focusAfter = false;     // 画完之后把焦点放到哪一条上（路径；null 表示"打开文件夹"那个按钮；false 表示不用动）
let viewing = null;         // 对话框里正在看的那一条：{path, data, error, loading, gone, confirm, message, edit, wantEdit}
                            // edit：正在编辑时是 {force, saving, armed, base}，base 是开始编辑时的名字和内容（用来判断改没改过）
let viewSeq = 0;            // 每读一次加一。回来得晚的旧内容靠它认出来、丢掉
let viewDrawn = '';
let memoryQuery = '';       // 搜索框里的字（已经转成小写）
const narrowMemory = matchMedia('(max-width: 760px)');  // 窄屏：列表和内容一次只显示一边

whenServerKnown(() => retext($('memory-folder-label'), byServer(k('在访达中打开记忆文件夹'), k('在文件资源管理器中打开记忆文件夹'), k('在文件管理器中打开记忆文件夹'))));

// 窗口里没有浮在页面上的提示（被窗口的暗底挡着），所以说明写在窗口里：正看着一条的，写在它下面；否则写在列表下面
function memorySay(text, bad) {
  if (viewing && memoryDialog.open) { viewing.message = { text, bad: Boolean(bad) }; drawViewer(); return; }
  if (!memoryDialog.open) { toast(text); return; }
  const line = $('memory-list-message');
  line.textContent = text;
  line.classList.toggle('bad', Boolean(bad));
}

// "projects/zhifox.md" → ["projects", "zhifox.md"]
function splitPath(path) {
  const at = path.lastIndexOf('/');
  return at < 0 ? ['', path] : [path.slice(0, at), path.slice(at + 1)];
}

function memoryMeta(folder, size, modified) {
  return [folder, formatSize(size), timeLabel(modified)].filter(Boolean).join(' · ');
}

function renderMemories(list) {
  memories = Array.isArray(list) ? list.filter((item) => item && typeof item.path === 'string') : [];
  if (forgetting && !memories.some((item) => item.path === forgetting)) forgetting = null;
  syncViewer();
  // 状态隔一会儿就来一次，记忆多半没变。没变就不重画，免得正在确认删除、或者键盘正停在某一行上时被打断
  const print = JSON.stringify([memories, forgetting, language, startOfDay(new Date()), memoryQuery, viewing && viewing.path]);
  if (print === memoriesDrawn) {
    // 删掉一条之后，服务器的新状态常常比删除请求的回答先到，列表已经按它重画过了。焦点还是要挪过去
    if (focusAfter !== false) focusMemory(focusAfter);
    focusAfter = false;
    return;
  }
  memoriesDrawn = print;
  // 重画会换掉整个列表。焦点原来在哪一行上，画完还放回那一行
  const focused = memoryList.contains(document.activeElement) ? document.activeElement.closest('.memory') : null;
  const target = focusAfter !== false ? focusAfter : focused ? focused.dataset.path : false;
  focusAfter = false;

  const fresh = knownMemories && isLive() ? memories.filter((item) => !knownMemories.has(item.path)).map((item) => item.path) : [];
  knownMemories = new Set(memories.map((item) => item.path));
  const shown = memoryQuery ? memories.filter((item) => item.path.toLowerCase().includes(memoryQuery)) : memories;
  const items = shown.map((item) => memoryRow(item, fresh.includes(item.path)));
  if (!memories.length) items.push(el('li', 'none', t('还没有记忆。对助手说“记住……”，它会把要点写成文件放在这里，以后的每段对话都用得上。')));
  else if (!items.length) items.push(el('li', 'none', t('没有名字里带“{query}”的记忆。', { query: $('memory-search').value.trim() })));
  if (memories.length >= MAX_MEMORIES) items.push(el('li', 'none', t('只列出了前 {n} 个文件，其余的请在文件夹里查看。', { n: MAX_MEMORIES })));
  memoryList.replaceChildren(...items);
  $('memory-count').textContent = memories.length ? memories.length.toLocaleString(LOCALES[language]) : '';
  if (target !== false) focusMemory(target);
}

// 把焦点放到某一条上：正在确认删除的那一条放在"取消"上，别的放在这一行本身。
// 那一条已经不在了，就放到第一条上；一条都没有了，放到"打开文件夹"上
function focusMemory(path) {
  const row = [...memoryList.children].find((node) => node.dataset.path === path);
  const button = row ? row.querySelector('.cancel') || row.querySelector('.memory-open')
    : path !== null && memoryList.querySelector('.memory-open');
  (button || $('memory-folder')).focus();
}

// 删掉一条之后焦点该去的地方：下一条，没有的话上一条
function neighbor(path) {
  const index = memories.findIndex((item) => item.path === path);
  const rest = memories.filter((item) => item.path !== path);
  const next = index < 0 ? null : rest[Math.min(index, rest.length - 1)];
  return next ? next.path : null;
}

function memoryRow(item, fresh) {
  const [folder, name] = splitPath(item.path);
  const selected = Boolean(viewing && viewing.path === item.path);
  const row = el('li', 'memory' + (fresh ? ' enter' : '') + (selected ? ' selected' : ''));
  row.dataset.path = item.path;

  // 删除前先在原地确认一下，和删除会话一样，不弹对话框
  if (forgetting === item.path) {
    const box = row.appendChild(el('div', 'memory-confirm'));
    box.appendChild(el('span', null, t('删除这条记忆？')));
    const yes = box.appendChild(el('button', 'mini danger', t('删除')));
    const no = box.appendChild(el('button', 'mini cancel', t('取消')));
    yes.type = no.type = 'button';
    const cancel = () => { forgetting = null; focusAfter = item.path; renderMemories(memories); };
    yes.addEventListener('click', async () => {
      yes.disabled = no.disabled = true;
      const after = neighbor(item.path);
      // 这一行先淡出去，同时请助手程序删掉。删不掉的话（比如助手正在工作）它再回来
      const [reply] = await Promise.all([forget(item.path), leave(row)]);
      if (!reply.ok) { memorySay(reply.error, true); memoriesDrawn = ''; cancel(); return; }
      dropMemory(item.path, after);
    });
    no.addEventListener('click', cancel);
    box.addEventListener('keydown', (key) => {
      if (key.key !== 'Escape') return;
      key.preventDefault();  // 这一下 Esc 只用来取消删除，不再去收起窄屏上的信息栏
      cancel();
    });
    return row;
  }

  const open = row.appendChild(el('button', 'memory-open'));
  open.type = 'button';
  open.title = item.path;
  if (selected) open.setAttribute('aria-current', 'true');
  open.appendChild(icon('file'));
  const words = open.appendChild(el('span', 'memory-words'));
  words.appendChild(el('span', 'memory-name', name));
  words.appendChild(el('span', 'memory-meta', memoryMeta(folder, item.size, item.modified)));
  open.addEventListener('click', () => openMemory(item.path));

  // 和会话行一样的三个按钮。编辑和删除在助手工作时藏起来（服务器这时也不让改）。
  // 每个按钮的名字里带上是哪一条：读屏软件逐个读按钮时，要知道它操作的是哪一个文件
  const actions = row.appendChild(el('div', 'memory-actions'));
  const button = (iconName, title, label, extra) => {
    const node = actions.appendChild(el('button', 'icon-button small' + (extra ? ' ' + extra : '')));
    node.type = 'button';
    node.title = t(title);
    node.setAttribute('aria-label', t(label, { name }));
    node.appendChild(icon(iconName));
    return node;
  };
  button('folder', showLabel(), showNamedLabel()).addEventListener('click', () => showMemoryFile(item.path));
  button('edit', k('编辑'), k('编辑记忆：{name}'), 'idle-only').addEventListener('click', () => openMemory(item.path, true));
  button('trash', k('删除'), k('删除记忆：{name}'), 'idle-only danger-hover').addEventListener('click', () => {
    forgetting = item.path;
    focusAfter = item.path;
    renderMemories(memories);
  });
  return row;
}

// 一行淡出去。减少动态效果时直接结束
function leave(node) {
  if (calm() || !node.animate) return Promise.resolve();
  return node.animate([{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateX(12px)' }],
    { duration: 180, easing: EASE, fill: 'forwards' }).finished.catch(() => {});
}

// 请助手程序删掉一条记忆。返回 call 的结果；成功之后怎么收尾由调用的地方决定
async function forget(path) {
  const reply = await call('/api/memory/delete', { path });
  if (forgetting === path) forgetting = null;
  return reply;
}

// 删掉之后：先在页面上把它拿掉（服务器紧接着发来的状态里也没有它了，到时不会再重画），焦点移到旁边那一条
function dropMemory(path, after) {
  focusAfter = after;
  renderMemories(memories.filter((item) => item.path !== path));
  memorySay(t('已删除记忆：{name}', { name: splitPath(path)[1] }));
}

// ---- 看一条记忆 ----
// 和设置面板同一种对话框。正文用 textContent 放进去，文件里写的是什么就显示什么，不当作 HTML。

// edit：点的是铅笔，读出来之后直接进入编辑
function openMemory(path, edit) {
  if (viewing && viewing.path !== path && !leaveEdit(true)) return;  // 正在改另一条、还没保存：先提醒，不换
  viewing = { path, data: null, error: '', loading: true, gone: false, confirm: false, message: null, edit: null, wantEdit: Boolean(edit) };
  viewDrawn = '';
  $('memory-list-message').textContent = '';
  if (!memoryDialog.open) memoryDialog.showModal();
  $('memory-split').classList.add('showing');
  drawViewer();
  renderMemories(memories);  // 列表里标出选中的这一条
  // 窄屏上列表这时藏起来了，刚点的那一行接不住焦点（焦点会掉到窗口外面，Esc 之类的按键就到不了这里），放到"返回"上
  if (narrowMemory.matches) $('memory-back').focus();
  loadMemory();
}

// 从左边栏的入口打开：先不选哪一条，光标放在搜索框里
function openMemories() {
  viewing = null;
  viewDrawn = '';
  $('memory-split').classList.remove('showing');
  drawViewer();
  renderMemories(memories);
  memoryDialog.showModal();
  (memories.length ? $('memory-search') : $('memory-folder')).focus();
}

// 窄屏上从内容回到列表
function backToMemories() {
  if (viewing && viewing.edit && !leaveEdit()) return;
  const path = viewing && viewing.path;
  viewing = null;
  viewDrawn = '';
  $('memory-split').classList.remove('showing');
  drawViewer();
  renderMemories(memories);
  if (path) focusMemory(path);
}

async function loadMemory() {
  if (!viewing) return;
  const mine = ++viewSeq, path = viewing.path;
  viewing.loading = true;
  const reply = await call('/api/memory/read', { path });
  if (mine !== viewSeq || !viewing || viewing.path !== path) return;  // 这期间关掉了，或者又读了一次
  const first = !viewing.data;
  viewing.loading = false;
  if (reply.ok) {
    viewing.data = reply.data;
    viewing.error = '';
  } else {
    viewing.error = reply.error;
  }
  if (reply.ok && viewing.wantEdit) { viewing.wantEdit = false; startEdit(); return; }
  drawViewer();
  if (first && reply.ok) play($('memory-text'), [{ opacity: 0, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], 240);
}

// 对话框开着的时候，这条记忆在磁盘上变了：被助手改了，就重新读一遍；不在了（助手删了、改了名，或者你在文件夹里删了），就说一声。
// 这两种变化眼睛看得到，读屏软件的用户看不到，所以也播报一下
function syncViewer() {
  if (!viewing || !memoryDialog.open) return;
  if (viewing.edit && viewing.edit.saving) return;  // 正在保存：改了名字的话，旧名字马上就不在了，这不算"被删了"
  const item = memories.find((entry) => entry.path === viewing.path);
  if (viewing.edit && item && viewing.data && item.modified !== viewing.data.modified && !viewing.edit.stale) {
    // 不重新读：重新读会让"打开时的修改时间"跟着变新，保存时就发现不了冲突，助手刚写的东西会被一声不响地冲掉。
    // 保存的时候服务器会对不上，先问你要不要覆盖
    viewing.edit.stale = true;
    viewing.message = { key: k('这条记忆刚刚在别处被改过了（可能是助手改的）。你的修改还在；保存时会先问你要不要覆盖。') };
    announce(t('这条记忆刚刚在别处被改过了（可能是助手改的）。你的修改还在；保存时会先问你要不要覆盖。'));
    drawViewer();
    return;
  }
  if (!item) {
    if (!viewing.gone) {
      viewing.gone = true;
      viewing.confirm = false;
      announce(t('这条记忆已经不在了。'));
    }
  } else if (viewing.gone || (viewing.data && !viewing.loading && item.modified !== viewing.data.modified)) {
    viewing.gone = false;
    viewing.message = { key: k('这条记忆刚刚更新过，下面是最新的内容。') };
    announce(t('这条记忆刚刚更新过，下面是最新的内容。'));
    loadMemory();
  }
  drawViewer();
}

function drawViewer() {
  if (!viewing) { drawNothingViewed(); return; }
  $('memory-empty').hidden = true;
  const { path, data, error, gone, confirm, message, edit } = viewing;
  const waiting = viewing.loading && !data;
  // 每来一次状态都会走到这里。没有变化就不动，正文可能有几十万字
  const editing = edit && [edit.force, edit.saving, edit.armed];
  const print = JSON.stringify([path, data && data.modified, data && data.size, error, waiting, gone, confirm, message, editing, language]);
  if (print === viewDrawn) return;
  viewDrawn = print;

  const [folder, name] = splitPath(path);
  $('memory-name').textContent = name;
  $('memory-meta').textContent = data ? memoryMeta(folder && folder + '/', data.size, data.modified) : folder && folder + '/';
  const text = $('memory-text');
  $('memory-editor').hidden = !edit;
  text.hidden = Boolean(edit) || (!data && !waiting);
  text.classList.toggle('waiting', waiting || Boolean(data && !data.text));
  if (waiting) retext(text, k('正在读取……'));
  else if (data && !data.text) retext(text, k('这个文件是空的。'));
  else if (data) { delete text.dataset.i18n; text.textContent = data.text; }

  const problem = $('memory-problem');
  problem.hidden = !error && !gone;
  problem.textContent = error || (gone ? t('这条记忆已经不在了：可能被助手删掉或改了名字，也可能是在文件夹里被删掉了。') : '');
  $('memory-note').textContent = data && data.truncated ? t('文件比较长，这里只显示了开头的一部分。完整的内容请在记忆文件夹里打开。') : '';
  drawViewerFoot();
}

function drawNothingViewed() {
  viewDrawn = '';
  $('memory-name').textContent = '';
  $('memory-meta').textContent = '';
  $('memory-text').hidden = true;
  $('memory-editor').hidden = true;
  $('memory-problem').hidden = true;
  $('memory-note').textContent = '';
  $('memory-foot').replaceChildren();
  const empty = $('memory-empty');
  empty.hidden = false;
  retext(empty, memories.length ? k('在左边选一条记忆，就能在这里查看和编辑。') : k('还没有记忆。对助手说“记住……”，它会把要点写成文件放在这里，以后的每段对话都用得上。'));
}

// 对话框底部：平时是一句提示、"打开文件夹"和"删除"；点了删除，换成确认的问话和两个按钮
function drawViewerFoot() {
  const foot = $('memory-foot'), { confirm, message, gone, data, edit } = viewing;
  const words = el('p', 'memory-message' + (message && message.bad ? ' bad' : ''));
  words.setAttribute('role', 'status');
  if (confirm) words.textContent = t('删除之后，助手就不再记得这些内容了。这一步不能撤销。');
  else if (message) words.textContent = message.key ? t(message.key, message.values) : message.text;
  else if (edit) words.textContent = t(SAVE_HINT);
  const nodes = [words];
  if (edit) {
    // 编辑时：取消、保存。保存时发现这期间被别处改过，按钮变成"覆盖保存"
    const no = el('button', 'button cancel', t('取消'));
    const yes = el('button', 'button primary', edit.force ? t('覆盖保存') : edit.saving ? t('正在保存……') : t('保存'));
    no.id = 'memory-edit-cancel';
    yes.id = 'memory-save';
    no.type = yes.type = 'button';
    yes.disabled = edit.saving || gone;
    no.addEventListener('click', () => leaveEdit());
    yes.addEventListener('click', saveEdit);
    nodes.push(no, yes);
  } else if (confirm) {
    const yes = el('button', 'button danger', t('删除'));
    const no = el('button', 'button cancel', t('取消'));
    yes.type = no.type = 'button';
    yes.addEventListener('click', forgetViewed);
    no.addEventListener('click', () => { viewing.confirm = false; drawViewer(); $('memory-delete').focus(); });
    nodes.push(yes, no);
  } else {
    const folder = el('button', 'icon-button');
    folder.id = 'memory-reveal';
    folder.type = 'button';
    folder.title = t(showLabel());
    folder.setAttribute('aria-label', t(showLabel()));
    folder.appendChild(icon('folder'));
    folder.disabled = gone;
    folder.addEventListener('click', () => showMemoryFile(viewing.path));
    const change = el('button', 'button idle-only');
    change.id = 'memory-edit';
    change.type = 'button';
    change.disabled = gone || !data;
    change.append(icon('edit'), t('编辑'));
    change.addEventListener('click', startEdit);
    const remove = el('button', 'button');
    remove.id = 'memory-delete';
    remove.type = 'button';
    remove.disabled = gone || !data;  // 已经不在了、或者根本没读出来的，没有可删的
    remove.append(icon('trash'), t('删除'));
    remove.addEventListener('click', () => {
      // 助手工作时服务器不让删（见 memory_api.py 的 delete_memory）。在这里就说清楚，不用等它退回来
      if (busy) { viewing.message = { key: BUSY_HINT, bad: true }; drawViewer(); return; }
      viewing.confirm = true;
      viewing.message = null;
      drawViewer();
      foot.querySelector('.cancel').focus();  // 默认停在"取消"上，免得手快连按两下回车就删掉了
    });
    nodes.push(folder, change, remove);
  }
  // 重画会换掉这几个按钮。焦点原来在其中一个上的，画完放回同一个
  const keep = foot.contains(document.activeElement) ? document.activeElement.id : '';
  foot.replaceChildren(...nodes);
  if (keep && $(keep)) $(keep).focus();
}

const SAVE_HINT = forKeys(k('⌘Enter 保存，Esc 退出编辑'), k('Ctrl+Enter 保存，Esc 退出编辑'));

function startEdit() {
  if (!viewing || !viewing.data) return;
  if (busy) { viewing.message = { key: BUSY_HINT, bad: true }; drawViewer(); return; }
  if (!viewing.data.editable) {
    // 只读出了开头的、不是 UTF-8 的，在这里改了再存回去就把文件弄坏了
    viewing.message = { bad: true, key: viewing.data.truncated
      ? k('这条记忆太长了，这里只读出了开头，没法在这里改。请在记忆文件夹里用别的软件编辑。')
      : k('这个文件不是 UTF-8 编码的文字，在这里改会把它弄坏。请在记忆文件夹里用别的软件编辑。') };
    drawViewer();
    return;
  }
  const [folder, name] = splitPath(viewing.path);
  viewing.edit = { force: false, saving: false, armed: false, stale: false, base: { name, text: viewing.data.text } };
  viewing.confirm = false;
  viewing.message = null;
  $('memory-edit-folder').textContent = folder ? folder + '/' : '';
  $('memory-edit-name').value = name;
  const area = $('memory-edit-text');
  area.value = viewing.data.text;
  drawViewer();
  area.focus();
  area.setSelectionRange(0, 0);
  area.scrollTop = 0;
}

const editChanged = () => Boolean(viewing && viewing.edit) && (
  $('memory-edit-name').value.trim() !== viewing.edit.base.name || !sameText($('memory-edit-text').value, viewing.edit.base.text));

// 离开编辑（取消、Esc、关掉对话框）。改过还没保存的，先提醒一次，再来一次才真的放弃。
// 返回 true 表示已经离开了编辑
function leaveEdit(closing) {
  const edit = viewing && viewing.edit;
  if (!edit) return true;
  if (editChanged() && !edit.armed) {
    edit.armed = true;
    viewing.message = { key: k('还有没保存的修改，再按一次就放弃。'), bad: true };
    drawViewer();
    return false;
  }
  viewing.edit = null;
  viewing.message = null;
  if (!closing) {
    viewDrawn = '';
    drawViewer();
    $('memory-edit') && $('memory-edit').focus();
  }
  return true;
}

async function saveEdit() {
  const edit = viewing && viewing.edit;
  if (!edit || edit.saving) return;
  if (busy) { viewing.message = { key: BUSY_HINT, bad: true }; drawViewer(); return; }
  const name = $('memory-edit-name').value.trim();
  if (!name) { viewing.message = { key: k('文件名不能为空。'), bad: true }; drawViewer(); $('memory-edit-name').focus(); return; }
  const path = viewing.path;
  edit.saving = true;
  edit.armed = false;
  drawViewer();
  const reply = await call('/api/memory/save', { path, name, text: $('memory-edit-text').value, modified: viewing.data.modified, force: edit.force });
  if (!viewing || viewing.path !== path || viewing.edit !== edit) return;  // 这期间对话框关掉了
  edit.saving = false;
  if (!reply.ok) {
    if (reply.data && reply.data.conflict) edit.force = true;  // 再点一次就覆盖
    viewing.message = { text: reply.error, bad: true };
    drawViewer();
    $('memory-save') && $('memory-save').focus();
    return;
  }
  viewing.path = reply.data.path;
  viewing.edit = null;
  viewing.gone = false;
  viewing.message = { key: k('已保存。') };
  announce(t('已保存。'));
  viewDrawn = '';
  await loadMemory();
  $('memory-edit') && $('memory-edit').focus();
}

// 想关掉对话框（×、点外面、Esc）。正在编辑的，先离开编辑：有没保存的修改就先提醒
function requestCloseMemory() {
  if (leaveEdit(true)) memoryDialog.close();
}

memoryDialog.addEventListener('keydown', (key) => {
  if (key.key === 'Escape' && !key.isComposing && viewing && !viewing.edit && !viewing.confirm && narrowMemory.matches) {
    key.preventDefault();  // 窄屏上正看着一条：Esc 先回到列表，再按一次才关窗口
    key.stopPropagation();
    backToMemories();
    return;
  }
  if (!viewing || !viewing.edit || key.isComposing || key.keyCode === 229) return;  // 输入法正在组字时，Esc 和回车是给输入法的
  if (key.key === 'Escape') {
    // 编辑时按 Esc 先回到"看"的样子，不直接关掉对话框；拦在 keydown 上，浏览器就不会发出关闭对话框的 cancel
    key.preventDefault();
    key.stopPropagation();
    leaveEdit();
  } else if (key.key === 'Enter' && commandHeld(key)) {
    key.preventDefault();
    saveEdit();
  }
});
for (const field of ['memory-edit-name', 'memory-edit-text']) {
  // 提醒过"再按一次就放弃"之后又接着改了，就当没提醒过；"已保存""文件名不能为空"这些话也该收起来了
  $(field).addEventListener('input', () => {
    const edit = viewing && viewing.edit;
    if (!edit || (!edit.armed && !viewing.message)) return;
    edit.armed = false;
    if (!edit.stale && !edit.force) viewing.message = null;
    drawViewer();
  });
}

// 在文件管理器里显示一条记忆的文件。页面给的是列表里的相对路径，文件到底在哪儿由助手程序重新算
async function showMemoryFile(path) {
  const reply = await call('/api/memory/show', { path });
  const said = reply.ok ? revealedText(reply.data.path) : reply.error;
  memorySay(said, !reply.ok);
}

async function forgetViewed() {
  if (!viewing) return;
  const path = viewing.path, after = neighbor(path);
  for (const button of $('memory-foot').querySelectorAll('button')) button.disabled = true;
  const reply = await forget(path);
  if (!viewing || viewing.path !== path) return;
  if (!reply.ok) {
    viewing.confirm = false;
    viewing.message = { text: reply.error, bad: true };
    viewDrawn = '';
    drawViewer();
    $('memory-delete').focus();
    return;
  }
  viewing = null;
  $('memory-split').classList.remove('showing');
  drawViewer();
  dropMemory(path, after);
}

// 请助手程序在文件管理器里打开记忆文件夹。页面什么路径都不给，开哪个文件夹由助手程序自己定
async function openMemoryFolder() {
  const reply = await call('/api/memory/reveal');
  const said = reply.ok ? revealedText(reply.data.path, true) : reply.error;
  memorySay(said, !reply.ok);
}

// 你在访达里删了、改了记忆文件，助手程序是不知道的，列表就旧了。所以在页面刚连上、和你切回这个窗口的时候，
// 请它重新看一眼文件夹：变了的话它会发来新的状态（列表照常跟着状态走），没变就什么都不发。不是定时去问
let refreshing = false;
async function refreshMemories() {
  if (refreshing || document.hidden || !token) return;
  refreshing = true;
  await call('/api/memory/refresh');
  refreshing = false;
}
window.addEventListener('focus', refreshMemories);
document.addEventListener('visibilitychange', refreshMemories);

$('memory-folder').addEventListener('click', openMemoryFolder);
$('close-memory').addEventListener('click', requestCloseMemory);
memoryDialog.addEventListener('close', () => {
  viewing = null;
  viewSeq += 1;
  viewDrawn = '';
  $('memory-text').textContent = '';  // 放开可能很长的正文
  $('memory-edit-text').value = '';
  $('memory-list-message').textContent = '';
  $('memory-split').classList.remove('showing');
  memoriesDrawn = '';
  // 焦点回到左边栏的"记忆"入口。焦点这一刻可能还停在已经关上的窗口里（浏览器稍后才把它挪到 body 上），所以两种都算
  const lost = !document.activeElement || document.activeElement === body || memoryDialog.contains(document.activeElement);
  if (lost) $('memory-open').focus();
});
$('memory-open').addEventListener('click', openMemories);
// 保险：焦点不在窗口里的时候按 Esc，浏览器直接发 cancel（不经过上面的 keydown）。窄屏上正看着一条，同样先回到列表
memoryDialog.addEventListener('cancel', (event) => {
  if (viewing && viewing.edit && editChanged() && !viewing.edit.armed) { event.preventDefault(); leaveEdit(); return; }  // 有没保存的修改：先提醒
  if (!viewing || !narrowMemory.matches || viewing.confirm) return;
  event.preventDefault();
  if (viewing.edit) leaveEdit(); else backToMemories();
});
$('memory-back').addEventListener('click', backToMemories);
$('memory-search').addEventListener('input', (typed) => {
  if (typed.isComposing) return;  // 输入法还在组字
  memoryQuery = $('memory-search').value.trim().toLowerCase();
  renderMemories(memories);
});
$('memory-search').addEventListener('compositionend', () => { memoryQuery = $('memory-search').value.trim().toLowerCase(); renderMemories(memories); });
