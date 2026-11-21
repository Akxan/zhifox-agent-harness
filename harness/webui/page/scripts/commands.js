// ---- 不用问的命令 ------------------------------------------------------------------
// 设置里、"要不要先问你"的窗口里都能打开的那个窗口：你自己加的命令，在"自动允许只读命令"那一档里不再问。
// 清单由助手程序管（harness/commands/allowlist.py：怎么比、哪些不许加、存在哪儿），页面只是照着状态里的 commands 画，
// 加、改、删都交给它，它说不行就把原因写在窗口里（窗口开着时，浮在页面上的提示会被暗底挡住）。
// 每一行和别的本地的东西一样：悬停时右边是"在访达中显示 / 编辑 / 删除"，删除先在原地确认，编辑在原地改。
// 有没保存的修改（添加框里写了一半、正在改的那一条改过了）时，Esc、×、点外面、"完成"都先提醒一次，再来一次才放弃。

const commandsDialog = $('commands-dialog');
let commandsInfo = null;       // 状态里的 commands：{entries, file, limit, chars, problem}
let commandsDrawn = '';
let commandsEditing = null;    // 正在原地改的那一条：{ entry, draft }
let commandsDeleting = null;   // 正在原地确认删除的那一条
let commandsArmed = false;     // 提醒过"还有没保存的修改"
let commandsBusy = false;      // 正在等助手程序回答：这期间按钮不能再点

function commandsSay(text, tone) {
  const box = $('commands-message');
  box.textContent = text || '';
  box.classList.toggle('bad', tone === 'bad');
}

// 状态里的清单变了：设置里那一节写上有几条；窗口开着就重画
function renderCommands(info) {
  commandsInfo = info || null;
  const count = commandsInfo ? commandsInfo.entries.length : 0;
  if (count) retext($('commands-state'), k('（{n} 条）'), { n: count }); else retext($('commands-state'), k('（还没有）'));
  if (commandsDialog.open) drawCommands();
}

function drawCommands(focus) {
  const info = commandsInfo || { entries: [], problem: null, limit: 0 };
  const print = JSON.stringify([info.entries, info.problem, commandsEditing && commandsEditing.entry, commandsDeleting, commandsBusy, language]);
  if (print === commandsDrawn && !focus) return;
  commandsDrawn = print;
  // 正在改的那一条要是已经不在了（在别处被删掉、改掉了），就不再改它
  if (commandsEditing && !info.entries.includes(commandsEditing.entry)) commandsEditing = null;
  if (commandsDeleting && !info.entries.includes(commandsDeleting)) commandsDeleting = null;
  const list = $('commands-list');
  const had = list.contains(document.activeElement) ? document.activeElement.dataset.focus : '';
  list.replaceChildren(...(info.entries.length ? info.entries.map(commandRow) : [tel('li', 'commands-empty', k('还没有。比如可以加上 npm test、make check。'))]));
  $('commands-count').textContent = info.entries.length ? info.entries.length + ' / ' + info.limit : '';
  // 清单的文件坏了、里面有不许加的条目：程序给的那句话（带着原文，换语言时跟着换）
  const problem = $('commands-problem');
  problem.hidden = !info.problem;
  if (info.problem && info.problem.key) retext(problem, info.problem.key, info.problem.values);
  else if (info.problem) problem.textContent = info.problem.text || '';
  $('commands-add').disabled = $('commands-new').disabled = commandsBusy;
  // 焦点放回原来的地方；原来那个按钮没了（删掉了、换成了确认），就交给要它去的地方，或者添加框
  const wanted = focus || had;
  if (wanted) {
    const target = list.querySelector('[data-focus="' + CSS.escape(wanted) + '"]');
    (target || $('commands-new')).focus();
  }
}

function commandRow(entry) {
  const row = el('li', 'cmd-row');
  row.dataset.entry = entry;
  if (commandsDeleting === entry) {
    const box = row.appendChild(el('div', 'cmd-confirm'));
    box.appendChild(tel('span', null, k('删掉“{entry}”？以后它在“自动允许只读命令”那一档里照常要问。'), { entry }));
    const yes = box.appendChild(tel('button', 'mini danger', k('删除')));
    const no = box.appendChild(tel('button', 'mini cancel', k('取消')));
    yes.type = no.type = 'button';
    yes.disabled = no.disabled = commandsBusy;
    no.dataset.focus = 'cancel:' + entry;
    yes.addEventListener('click', () => removeCommand(entry));
    no.addEventListener('click', () => { commandsDeleting = null; drawCommands('edit:' + entry); });
    box.addEventListener('keydown', (key) => {
      if (key.key !== 'Escape' || key.isComposing) return;
      key.preventDefault();
      key.stopPropagation();
      commandsDeleting = null;
      drawCommands('delete:' + entry);
    });
    return row;
  }
  if (commandsEditing && commandsEditing.entry === entry) {
    const form = row.appendChild(el('div', 'cmd-edit'));
    const field = form.appendChild(el('input', 'field cmd-edit-field'));
    field.type = 'text';
    field.spellcheck = false;
    field.autocomplete = 'off';
    field.maxLength = commandsInfo ? commandsInfo.chars : 200;
    field.value = commandsEditing.draft;
    field.dataset.focus = 'field:' + entry;
    tattr(field, 'aria-label', k('改这条命令'));
    const save = form.appendChild(tel('button', 'mini primary', k('保存')));
    const cancel = form.appendChild(tel('button', 'mini cancel', k('取消')));
    save.type = cancel.type = 'button';
    save.disabled = cancel.disabled = field.disabled = commandsBusy;
    field.addEventListener('input', () => { commandsEditing.draft = field.value; commandsTouched(); });
    field.addEventListener('keydown', (key) => {
      if (key.isComposing || key.keyCode === 229) return;  // 输入法正在组字
      if (key.key === 'Enter') { key.preventDefault(); saveCommand(entry); }
      if (key.key === 'Escape') { key.preventDefault(); key.stopPropagation(); requestStopEditing(); }
    });
    save.addEventListener('click', () => saveCommand(entry));
    cancel.addEventListener('click', requestStopEditing);
    return row;
  }
  row.appendChild(el('code', 'cmd-text', entry));
  const actions = row.appendChild(el('div', 'cmd-actions'));
  const reveal = actions.appendChild(iconButton('folder', ON_MAC ? k('在访达中显示 commands.json') : k('在文件夹中显示 commands.json')));
  const edit = actions.appendChild(iconButton('edit', k('编辑')));
  const trash = actions.appendChild(iconButton('trash', k('删除')));
  trash.classList.add('danger-hover');
  reveal.dataset.focus = 'reveal:' + entry;
  edit.dataset.focus = 'edit:' + entry;
  trash.dataset.focus = 'delete:' + entry;
  for (const button of [reveal, edit, trash]) button.disabled = commandsBusy;
  reveal.addEventListener('click', revealCommands);
  edit.addEventListener('click', () => { commandsEditing = { entry, draft: entry }; commandsDeleting = null; drawCommands('field:' + entry); });
  trash.addEventListener('click', () => { commandsDeleting = entry; commandsEditing = null; drawCommands('cancel:' + entry); });
  return row;
}

// 有没保存的修改：添加框里写了东西，或者正在改的那一条和原来不一样
const commandsDirty = () => Boolean($('commands-new').value.trim()) || Boolean(commandsEditing && commandsEditing.draft.trim() !== commandsEditing.entry);

function commandsTouched() {
  if (commandsArmed) { commandsArmed = false; commandsSay(''); }
}

function requestStopEditing() {
  if (commandsEditing && commandsEditing.draft.trim() !== commandsEditing.entry && !commandsArmed) {
    commandsArmed = true;
    commandsSay(t('还有没保存的修改，再按一次就放弃。'), 'bad');
    return;
  }
  const entry = commandsEditing ? commandsEditing.entry : '';
  commandsEditing = null;
  commandsArmed = false;
  commandsSay('');
  drawCommands('edit:' + entry);
}

function requestCloseCommands() {
  if (commandsDirty() && !commandsArmed) {
    commandsArmed = true;
    commandsSay(t('还有没保存的修改，再按一次就放弃。'), 'bad');
    return;
  }
  commandsDialog.close();
}

function openCommands() {
  if (!state) return;
  for (const other of [settings, approvalDialog]) if (other.open) other.close();
  commandsEditing = commandsDeleting = null;
  commandsArmed = commandsBusy = false;
  commandsDrawn = '';
  $('commands-new').value = '';
  commandsSay('');
  drawCommands();
  commandsDialog.showModal();
  $('commands-new').focus();
}

// 加、改、删交给助手程序；它回答以后，状态里的清单会跟着变，列表照着重画
async function commandsCall(path, body) {
  commandsBusy = true;
  drawCommands();
  const reply = await call(path, body);
  commandsBusy = false;
  drawCommands();
  if (!reply.ok) commandsSay(reply.error, 'bad');
  return reply;
}

async function addCommand() {
  const field = $('commands-new');
  if (!field.value.trim()) { commandsSay(t('写一条命令，比如 npm test。'), 'bad'); field.focus(); return; }
  const reply = await commandsCall('/api/commands/add', { command: field.value });
  if (!reply.ok) { field.focus(); return; }
  field.value = '';
  commandsArmed = false;
  commandsSay(t('加好了：以“{entry}”开头的命令不再问。', { entry: reply.data.entry }));
  announce(t('加好了：以“{entry}”开头的命令不再问。', { entry: reply.data.entry }));
  field.focus();
}

async function saveCommand(entry) {
  if (!commandsEditing) return;
  const draft = commandsEditing.draft;
  if (draft.trim() === entry) { requestStopEditing(); return; }
  const reply = await commandsCall('/api/commands/edit', { old: entry, command: draft });
  if (!reply.ok) { drawCommands('field:' + entry); return; }
  commandsEditing = null;
  commandsArmed = false;
  commandsSay(t('改好了：{entry}', { entry: reply.data.entry }));
  drawCommands('edit:' + reply.data.entry);
}

async function removeCommand(entry) {
  const reply = await commandsCall('/api/commands/delete', { command: entry });
  if (!reply.ok) return;
  commandsDeleting = null;
  commandsSay(t('删掉了：{entry}', { entry }));
  drawCommands();
  $('commands-new').focus();
}

async function revealCommands() {
  const reply = await call('/api/commands/reveal', {});
  if (!reply.ok) { commandsSay(reply.error, 'bad'); return; }
  commandsSay(revealedText(reply.data.path));
}

$('commands-open').addEventListener('click', openCommands);
$('approval-commands').addEventListener('click', openCommands);
$('commands-add').addEventListener('click', addCommand);
$('commands-new').addEventListener('input', commandsTouched);
$('commands-new').addEventListener('keydown', (key) => {
  if (key.isComposing || key.keyCode === 229) return;
  if (key.key === 'Enter') { key.preventDefault(); addCommand(); }
});
$('close-commands').addEventListener('click', requestCloseCommands);
$('commands-done').addEventListener('click', requestCloseCommands);
commandsDialog.addEventListener('keydown', (key) => {
  if (key.isComposing || key.keyCode === 229) return;
  if (key.key === 'Escape' && !key.defaultPrevented) { key.preventDefault(); key.stopPropagation(); requestCloseCommands(); }
});
// 保险：焦点不在窗口里时按 Esc，浏览器直接发 cancel，不经过上面的 keydown
commandsDialog.addEventListener('cancel', (event) => { event.preventDefault(); requestCloseCommands(); });
