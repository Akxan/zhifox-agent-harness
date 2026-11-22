// ---- 沙箱 · 额外允许的路径 -------------------------------------------------------
// 设置里"沙箱"一节打开的那个窗口：沙箱开着时，命令除了工作目录还能读（或者读写）的那几个地方。
// 清单由助手程序管（harness/webui/sandbox_api.py：怎么检查、哪些加不进来、存在设置里），页面只照着 state.sandbox.paths 画，
// 加、改、删都交给它，它说不行就把原因写在窗口里（窗口开着时，浮在页面上的提示会被暗底挡住）。
// 每一行和别的本地的东西一样：悬停时右边是"在访达中显示 / 编辑 / 删除"，删除先在原地确认，编辑在原地改（路径和能不能写）。
// 有没保存的修改（添加框里写了一半、正在改的那一条改过了）时，Esc、×、点外面、"完成"都先提醒一次，再来一次才放弃。

const sandboxDialog = $('sandbox-dialog');
let sandboxPathsDrawn = '';
let sandboxEditing = null;    // 正在原地改的那一条：{ path, draft, write }
let sandboxDeleting = null;   // 正在原地确认删除的那一条（路径）
let sandboxArmed = false;     // 提醒过"还有没保存的修改"
let sandboxBusy = false;      // 正在等助手程序回答：这期间按钮不能再点

function sandboxPathsSay(text, tone) {
  const box = $('sandbox-message');
  box.textContent = text || '';
  box.classList.toggle('bad', tone === 'bad');
}

const sandboxEntries = () => (sandboxInfo ? sandboxInfo.paths : []);
const sandboxFind = (path) => sandboxEntries().find((item) => item.path === path) || null;

function drawSandboxPaths(focus) {
  const entries = sandboxEntries();
  const print = JSON.stringify([entries, sandboxEditing && [sandboxEditing.path, sandboxEditing.write], sandboxDeleting, sandboxBusy, language]);
  if (print === sandboxPathsDrawn && !focus) return;
  sandboxPathsDrawn = print;
  // 正在改、正在删的那一条要是已经不在了（在别处被删掉、改掉了），就不再改它
  if (sandboxEditing && !sandboxFind(sandboxEditing.path)) sandboxEditing = null;
  if (sandboxDeleting && !sandboxFind(sandboxDeleting)) sandboxDeleting = null;
  const list = $('sandbox-list');
  const had = list.contains(document.activeElement) ? document.activeElement.dataset.focus : '';
  list.replaceChildren(...(entries.length ? entries.map(sandboxRow) : [tel('li', 'sandbox-empty', k('还没有。比如可以把 ~/.nvm 加成只读，沙箱里就能用 nvm 装的 node。'))]));
  $('sandbox-count').textContent = entries.length ? entries.length + ' / ' + (sandboxInfo ? sandboxInfo.limit : 0) : '';
  $('sandbox-add').disabled = $('sandbox-new').disabled = $('sandbox-new-mode').disabled = sandboxBusy;
  if (sandboxInfo) $('sandbox-new').maxLength = sandboxInfo.chars;
  // 焦点放回原来的地方；原来那个按钮没了（删掉了、换成了确认），就交给要它去的地方，或者添加框
  const wanted = focus || had;
  if (wanted) {
    const target = list.querySelector('[data-focus="' + CSS.escape(wanted) + '"]');
    (target || $('sandbox-new')).focus();
  }
}

// 能读写的、只读的：一个小标签。能写的用琥珀色（沙箱里的命令能改里面的东西）
function sandboxTag(write) {
  return tel('span', 'sandbox-tag' + (write ? ' write' : ''), write ? k('可读写') : k('只读'));
}

function sandboxModeSelect(write, label) {
  const select = el('select', 'field sandbox-mode');
  select.append(tel('option', null, k('只读')), tel('option', null, k('可读写')));
  select.options[0].value = 'read';
  select.options[1].value = 'write';
  select.value = write ? 'write' : 'read';
  tattr(select, 'aria-label', label);
  return select;
}

function sandboxRow(item) {
  const path = item.path;
  const row = el('li', 'sandbox-row');
  row.dataset.path = path;
  if (sandboxDeleting === path) {
    const box = row.appendChild(el('div', 'sandbox-confirm'));
    box.appendChild(tel('span', null, k('不再允许“{path}”？沙箱里的命令以后读写不了它。'), { path }));
    const yes = box.appendChild(tel('button', 'mini danger', k('删除')));
    const no = box.appendChild(tel('button', 'mini cancel', k('取消')));
    yes.type = no.type = 'button';
    yes.disabled = no.disabled = sandboxBusy;
    no.dataset.focus = 'cancel:' + path;
    yes.addEventListener('click', () => removeSandboxPath(path));
    no.addEventListener('click', () => { sandboxDeleting = null; drawSandboxPaths('edit:' + path); });
    box.addEventListener('keydown', (key) => {
      if (key.key !== 'Escape' || key.isComposing) return;
      key.preventDefault();
      key.stopPropagation();
      sandboxDeleting = null;
      drawSandboxPaths('delete:' + path);
    });
    return row;
  }
  if (sandboxEditing && sandboxEditing.path === path) {
    const form = row.appendChild(el('div', 'sandbox-edit'));
    const field = form.appendChild(el('input', 'field sandbox-edit-field'));
    field.type = 'text';
    field.spellcheck = false;
    field.autocomplete = 'off';
    field.setAttribute('autocapitalize', 'off');
    field.maxLength = sandboxInfo ? sandboxInfo.chars : 1000;
    field.value = sandboxEditing.draft;
    field.dataset.focus = 'field:' + path;
    tattr(field, 'aria-label', k('改这条路径'));
    const mode = form.appendChild(sandboxModeSelect(sandboxEditing.write, k('沙箱里的命令能做什么')));
    mode.dataset.focus = 'mode:' + path;
    const save = form.appendChild(tel('button', 'mini primary', k('保存')));
    const cancel = form.appendChild(tel('button', 'mini cancel', k('取消')));
    save.type = cancel.type = 'button';
    save.disabled = cancel.disabled = field.disabled = mode.disabled = sandboxBusy;
    field.addEventListener('input', () => { sandboxEditing.draft = field.value; sandboxTouched(); });
    mode.addEventListener('change', () => { sandboxEditing.write = mode.value === 'write'; sandboxTouched(); });
    for (const control of [field, mode]) {
      control.addEventListener('keydown', (key) => {
        if (key.isComposing || key.keyCode === 229) return;  // 输入法正在组字
        if (key.key === 'Enter' && control === field) { key.preventDefault(); saveSandboxPath(path); }
        if (key.key === 'Escape') { key.preventDefault(); key.stopPropagation(); requestStopSandboxEdit(); }
      });
    }
    save.addEventListener('click', () => saveSandboxPath(path));
    cancel.addEventListener('click', requestStopSandboxEdit);
    return row;
  }
  const main = row.appendChild(el('div', 'sandbox-row-main'));
  const shown = main.appendChild(el('code', 'sandbox-path', path));
  shown.title = path;
  const bits = main.appendChild(el('span', 'sandbox-row-bits'));
  bits.appendChild(sandboxTag(item.write));
  if (!item.exists) bits.appendChild(tel('span', 'sandbox-missing', k('现在不存在')));
  const actions = row.appendChild(el('div', 'sandbox-actions'));
  const reveal = actions.appendChild(iconButton('folder', showLabel()));  // 访达 / 文件资源管理器 / 文件管理器（见 platform.js）
  const edit = actions.appendChild(iconButton('edit', k('编辑')));
  const trash = actions.appendChild(iconButton('trash', k('删除')));
  trash.classList.add('danger-hover');
  reveal.dataset.focus = 'reveal:' + path;
  edit.dataset.focus = 'edit:' + path;
  trash.dataset.focus = 'delete:' + path;
  for (const button of [reveal, edit, trash]) button.disabled = sandboxBusy;
  reveal.addEventListener('click', () => revealSandboxPath(path));
  edit.addEventListener('click', () => { sandboxEditing = { path, draft: path, write: item.write }; sandboxDeleting = null; drawSandboxPaths('field:' + path); });
  trash.addEventListener('click', () => { sandboxDeleting = path; sandboxEditing = null; drawSandboxPaths('cancel:' + path); });
  return row;
}

// 正在改的那一条和原来不一样（路径或者能不能写）
const sandboxEditChanged = () => {
  if (!sandboxEditing) return false;
  const item = sandboxFind(sandboxEditing.path);
  return sandboxEditing.draft.trim() !== sandboxEditing.path || Boolean(item && sandboxEditing.write !== item.write);
};
// 有没保存的修改：添加框里写了东西，或者正在改的那一条改过了
const sandboxDirty = () => Boolean($('sandbox-new').value.trim()) || sandboxEditChanged();

function sandboxTouched() {
  if (sandboxArmed) { sandboxArmed = false; sandboxPathsSay(''); }
}

function requestStopSandboxEdit() {
  if (sandboxEditChanged() && !sandboxArmed) {
    sandboxArmed = true;
    sandboxPathsSay(t('还有没保存的修改，再按一次就放弃。'), 'bad');
    return;
  }
  const path = sandboxEditing ? sandboxEditing.path : '';
  sandboxEditing = null;
  sandboxArmed = false;
  sandboxPathsSay('');
  drawSandboxPaths('edit:' + path);
}

function requestCloseSandboxPaths() {
  if (sandboxDirty() && !sandboxArmed) {
    sandboxArmed = true;
    sandboxPathsSay(t('还有没保存的修改，再按一次就放弃。'), 'bad');
    return;
  }
  sandboxDialog.close();
}

function openSandboxPaths() {
  if (!sandboxInfo) return;
  sandboxEditing = sandboxDeleting = null;
  sandboxArmed = sandboxBusy = false;
  sandboxPathsDrawn = '';
  $('sandbox-new').value = '';
  $('sandbox-new-mode').value = 'read';
  sandboxPathsSay('');
  drawSandboxPaths();
  if (settings.open) settings.close();  // 和"不用问的命令"一样：两个窗口不叠着开（Esc 和焦点都容易乱）
  sandboxDialog.showModal();
  $('sandbox-new').focus();
}

// 加、改、删交给助手程序；它回答以后，状态里的清单会跟着变，列表照着重画
async function sandboxCall(path, body) {
  sandboxBusy = true;
  drawSandboxPaths();
  const reply = await call(path, body);
  sandboxBusy = false;
  drawSandboxPaths();
  if (!reply.ok) sandboxPathsSay(reply.error, 'bad');
  return reply;
}

async function addSandboxPath() {
  const field = $('sandbox-new');
  if (!field.value.trim()) { sandboxPathsSay(t('写一个路径，比如 ~/.nvm 或者 /srv/app。'), 'bad'); field.focus(); return; }
  const write = $('sandbox-new-mode').value === 'write';
  const reply = await sandboxCall('/api/sandbox/add', { path: field.value, write });
  if (!reply.ok) { field.focus(); return; }
  field.value = '';
  $('sandbox-new-mode').value = 'read';
  sandboxArmed = false;
  const said = write ? t('加好了：沙箱里的命令能读写 {path}。', { path: reply.data.path }) : t('加好了：沙箱里的命令能读 {path}。', { path: reply.data.path });
  sandboxPathsSay(said);
  announce(said);
  field.focus();
}

async function saveSandboxPath(path) {
  if (!sandboxEditing) return;
  if (!sandboxEditChanged()) { requestStopSandboxEdit(); return; }
  const reply = await sandboxCall('/api/sandbox/edit', { old: path, path: sandboxEditing.draft, write: sandboxEditing.write });
  if (!reply.ok) { drawSandboxPaths('field:' + path); return; }
  sandboxEditing = null;
  sandboxArmed = false;
  sandboxPathsSay(t('改好了：{path}', { path: reply.data.path }));
  drawSandboxPaths('edit:' + reply.data.path);
}

async function removeSandboxPath(path) {
  const reply = await sandboxCall('/api/sandbox/delete', { path });
  if (!reply.ok) return;
  sandboxDeleting = null;
  sandboxPathsSay(t('删掉了：{path}', { path }));
  drawSandboxPaths();
  $('sandbox-new').focus();
}

async function revealSandboxPath(path) {
  const reply = await call('/api/sandbox/reveal', { path });
  if (!reply.ok) { sandboxPathsSay(reply.error, 'bad'); return; }
  sandboxPathsSay(revealedText(reply.data.path));
}

$('sandbox-paths-open').addEventListener('click', openSandboxPaths);
$('sandbox-add').addEventListener('click', addSandboxPath);
$('sandbox-new').addEventListener('input', sandboxTouched);
$('sandbox-new-mode').addEventListener('change', sandboxTouched);
$('sandbox-new').addEventListener('keydown', (key) => {
  if (key.isComposing || key.keyCode === 229) return;
  if (key.key === 'Enter') { key.preventDefault(); addSandboxPath(); }
});
$('close-sandbox-dialog').addEventListener('click', requestCloseSandboxPaths);
$('sandbox-done').addEventListener('click', requestCloseSandboxPaths);
sandboxDialog.addEventListener('keydown', (key) => {
  if (key.isComposing || key.keyCode === 229) return;
  if (key.key === 'Escape' && !key.defaultPrevented) { key.preventDefault(); key.stopPropagation(); requestCloseSandboxPaths(); }
});
// 保险：焦点不在窗口里时按 Esc，浏览器直接发 cancel，不经过上面的 keydown。点窗口外面的暗底，在 settings.js 里和别的窗口一起接
sandboxDialog.addEventListener('cancel', (event) => { event.preventDefault(); requestCloseSandboxPaths(); });
