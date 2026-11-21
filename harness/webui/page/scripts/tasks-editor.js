// ---- 定时任务 · 新建和编辑 --------------------------------------------------
// 编辑框：做什么（让助手去做，还是只提醒你）、什么时候（一次、每天、每周、每月、每隔一段时间）、要不要先问你。
// 接下来三次的时间请助手程序按同一套规则算好（/api/task/preview），页面不重复写一遍。改过还没保存就关，先提醒一次。
// 文件最后接上编辑框的按钮，并把左边栏的标签定到一开始的那一个（syncNavTabs，在 tasks.js）。

const localIso = (date) => date.getFullYear() + '-' + pad2(date.getMonth() + 1) + '-' + pad2(date.getDate()) + 'T' + pad2(date.getHours()) + ':' + pad2(date.getMinutes());

function openEditor(task, preset) {
  const base = task || { kind: 'agent', title: '', prompt: '', enabled: true, schedule: { type: 'daily', time: '09:00' }, ...(preset || {}) };
  const plan = base.schedule || {};
  editing = {
    id: task ? task.id : null, kind: base.kind === 'reminder' ? 'reminder' : 'agent', type: plan.type || 'daily', days: new Set(plan.days || [1, 2, 3, 4, 5]),
    approval: base.approval in APPROVAL ? base.approval : 'ask', armed: false, saving: false,
  };
  retext($('task-editor-title'), task ? k('编辑任务') : k('新建定时任务'));
  $('task-title').value = base.title || '';
  $('task-prompt').value = base.prompt || '';
  // 没写过的时间：一次性的默认是"明天的这个时候"，其余的默认早上 9 点
  const soon = localIso(new Date(Date.now() + 86400000));
  const [date, clock] = (plan.type === 'once' && plan.at ? plan.at : soon).split('T');
  $('task-date').value = date;
  $('task-time').value = plan.time || (plan.type === 'once' ? clock.slice(0, 5) : '09:00');
  $('task-day').value = plan.day || 1;
  const minutes = Number(plan.minutes) || 60;
  $('task-unit').value = minutes % 60 === 0 ? '60' : '1';
  $('task-every').value = minutes % 60 === 0 ? minutes / 60 : minutes;
  $('task-enabled').checked = base.enabled !== false;
  $('task-editor-message').textContent = '';
  $('task-editor-message').classList.remove('bad');
  drawEditor();
  editing.base = editorPrint();
  if (taskDetail.open) taskDetail.close();
  taskEditor.showModal();
  $('task-title').focus();
  previewSoon();
}

function editorSchedule() {
  const type = editing.type, time = $('task-time').value || '09:00';
  if (type === 'once') return { type, at: ($('task-date').value || '') + 'T' + time };
  if (type === 'daily') return { type, time };
  if (type === 'weekly') return { type, days: [...editing.days].sort(), time };
  if (type === 'monthly') return { type, day: Number($('task-day').value), time };
  return { type: 'interval', minutes: Number($('task-every').value) * Number($('task-unit').value) };
}

const editorPrint = () => JSON.stringify([editing.kind, $('task-title').value, $('task-prompt').value, editorSchedule(), $('task-enabled').checked, editing.approval]);

function drawEditor() {
  if (!editing) return;
  for (const choice of taskEditor.querySelectorAll('[data-kind]')) {
    const on = choice.dataset.kind === editing.kind;
    choice.setAttribute('aria-checked', String(on));
    choice.tabIndex = on ? 0 : -1;
  }
  for (const choice of taskEditor.querySelectorAll('[data-type]')) {
    const on = choice.dataset.type === editing.type;
    choice.setAttribute('aria-checked', String(on));
    choice.tabIndex = on ? 0 : -1;
  }
  for (const choice of taskEditor.querySelectorAll('[data-approval]')) {
    const on = choice.dataset.approval === editing.approval;
    choice.setAttribute('aria-checked', String(on));
    choice.tabIndex = on ? 0 : -1;
  }
  const agent = editing.kind === 'agent';
  // 只提醒的任务不调用模型、不用工具，没有什么可问的
  $('task-approval-field').hidden = !agent;
  $('task-approval-warning').hidden = !agent || editing.approval !== 'all';
  retext($('task-prompt-label'), agent ? k('让助手做什么') : k('提醒的内容（可以不写）'));
  tattr($('task-prompt'), 'placeholder', agent ? k('比如：看看我的待办，按轻重缓急给我写一份今天的安排。') : k('比如：起来走走，喝杯水。'));
  $('task-cost-note').hidden = !agent;
  $('task-date').hidden = editing.type !== 'once';
  $('task-days').hidden = editing.type !== 'weekly';
  $('task-day-box').hidden = editing.type !== 'monthly';
  $('task-every-box').hidden = editing.type !== 'interval';
  $('task-time').hidden = editing.type === 'interval';
  // 星期几：七个可以按下、弹起的按钮。重画时焦点放回原来那一个
  const days = $('task-days'), was = [...days.children].indexOf(document.activeElement);
  days.replaceChildren(...[1, 2, 3, 4, 5, 6, 7].map((number) => {
    const day = el('button', null, weekdayName(number));
    day.type = 'button';
    day.setAttribute('aria-pressed', String(editing.days.has(number)));
    day.addEventListener('click', () => {
      if (editing.days.has(number)) editing.days.delete(number); else editing.days.add(number);
      drawEditor();
      editorChanged();
    });
    return day;
  }));
  if (was >= 0) days.children[was].focus();
}

function editorChanged() {
  if (!editing) return;
  if (editing.armed) { editing.armed = false; editorMessage(''); }
  previewSoon();
}

function editorMessage(text, bad) {
  $('task-editor-message').textContent = text;
  $('task-editor-message').classList.toggle('bad', Boolean(bad));
}

// 保存之前，请助手程序按同一套规则算出接下来三次的时间（时区、夏令时、大小月都在那边算，页面不重复写一遍）
let previewTimer = 0, previewSeq = 0;
function previewSoon() { clearTimeout(previewTimer); previewTimer = setTimeout(preview, 250); }
async function preview() {
  if (!editing) return;
  const mine = ++previewSeq;
  const reply = await call('/api/task/preview', { schedule: editorSchedule() });
  if (mine !== previewSeq || !editing) return;
  const box = $('task-preview');
  box.classList.toggle('bad', !reply.ok);
  const next = reply.ok ? reply.data.next || [] : [];
  box.textContent = !reply.ok ? reply.error : next.length ? t('接下来：{times}', { times: next.map(momentText).join(' · ') }) : t('按这个时间不会再运行了。');
}

async function saveTask() {
  if (!editing || editing.saving) return;
  const title = $('task-title').value.trim(), prompt = $('task-prompt').value.trim();
  if (!title) { editorMessage(t('写个标题吧，列表里靠它认出这个任务。'), true); $('task-title').focus(); return; }
  if (editing.kind === 'agent' && !prompt) { editorMessage(t('写上要助手做什么。'), true); $('task-prompt').focus(); return; }
  if (editing.type === 'weekly' && !editing.days.size) { editorMessage(t('至少选一天。'), true); return; }
  editing.saving = true;
  $('task-save').disabled = true;
  const reply = await call('/api/task/save', {
    ...(editing.id ? { id: editing.id } : {}), title, kind: editing.kind, prompt, schedule: editorSchedule(), enabled: $('task-enabled').checked,
    approval: editing.kind === 'agent' ? editing.approval : 'ask',
  });
  if (!editing) return;
  editing.saving = false;
  $('task-save').disabled = false;
  if (!reply.ok) { editorMessage(reply.error, true); return; }
  const added = !editing.id;
  editing = null;
  taskEditor.close();
  setNavTab('tasks');
  toast(added ? t('已添加任务：{title}', { title }) : t('已保存任务：{title}', { title }));
}

// 关掉编辑框（×、取消、Esc、点外面）。改过还没保存的，先提醒一次
function requestCloseEditor() {
  if (editing && !editing.armed && editorPrint() !== editing.base) {
    editing.armed = true;
    editorMessage(t('还有没保存的修改，再按一次就放弃。'), true);
    return;
  }
  taskEditor.close();
}

for (const group of taskEditor.querySelectorAll('[role="radiogroup"]')) {
  // 单选的一组（到点以后、什么时候、要不要先问你）：点一下选中；方向键在选项之间移动（选中跟着走）
  group.addEventListener('click', (click) => {
    const choice = click.target.closest('button');
    if (!choice || !editing) return;
    if (choice.dataset.kind) editing.kind = choice.dataset.kind;
    else if (choice.dataset.approval) editing.approval = choice.dataset.approval;
    else editing.type = choice.dataset.type;
    drawEditor();
    editorChanged();
  });
  group.addEventListener('keydown', (key) => {
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(key.key) || !editing) return;
    key.preventDefault();
    const choices = [...group.querySelectorAll('button')];
    const at = choices.indexOf(group.querySelector('[aria-checked="true"]'));
    const next = choices[(at + (key.key === 'ArrowLeft' || key.key === 'ArrowUp' ? -1 : 1) + choices.length) % choices.length];
    next.click();
    next.focus();
  });
}
for (const id of ['task-title', 'task-prompt', 'task-date', 'task-time', 'task-day', 'task-every', 'task-unit', 'task-enabled']) {
  $(id).addEventListener(id === 'task-unit' || id === 'task-enabled' ? 'change' : 'input', editorChanged);
}
taskEditor.addEventListener('keydown', (key) => {
  if (key.isComposing || key.keyCode === 229) return;  // 输入法正在组字
  if (key.key === 'Escape') { key.preventDefault(); key.stopPropagation(); requestCloseEditor(); }
  else if (key.key === 'Enter' && commandHeld(key)) { key.preventDefault(); saveTask(); }
});
// 保险：换"什么时候"时，正拿着焦点的那个输入框可能被藏起来，焦点掉到窗口外面，Esc 就直接变成 cancel
taskEditor.addEventListener('cancel', (event) => {
  if (editing && !editing.armed && editorPrint() !== editing.base) { event.preventDefault(); requestCloseEditor(); }
});
taskEditor.addEventListener('close', () => {
  const id = editing && editing.id;
  editing = null;
  clearTimeout(previewTimer);
  $('task-preview').textContent = '';
  if (navTab === 'tasks' && (!document.activeElement || document.activeElement === body)) id ? focusTask(id) : $('task-new').focus();
});
$('task-save').addEventListener('click', saveTask);
$('task-cancel').addEventListener('click', requestCloseEditor);
$('close-task-editor').addEventListener('click', requestCloseEditor);
$('task-new').addEventListener('click', () => openEditor(null));
setNavTab(navTab);
syncNavTabs();
