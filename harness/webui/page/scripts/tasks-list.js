// ---- 定时任务 · 列表 --------------------------------------------------------
// "任务"标签里的列表：正在运行的在最前，然后按下次运行的时间排，暂停的在最后。每一行写着什么时候跑、上一次怎么样，
// 悬停时是"立即运行"，和别处一样的"在访达中显示 / 编辑 / 删除"（删除先在原地确认）。
// 正在运行的那一个不显示这几个按钮，改成画出进度、在做什么，停止和等你确认的操作都在进度里。

// 先是正在运行的，然后是开着的（按下次运行的时间），最后是暂停的
function taskOrder(a, b) {
  const rank = (task) => (task.running ? 0 : task.enabled && task.next_run ? 1 : task.enabled ? 2 : 3);
  return rank(a) - rank(b) || (a.next_run || 0) - (b.next_run || 0) || a.title.localeCompare(b.title);
}

function renderTasks(list, fired) {
  if ($('nav-tab-tasks').hidden === Array.isArray(list)) { $('nav-tab-tasks').hidden = !Array.isArray(list); syncNavTabs(); }
  tasks = (Array.isArray(list) ? list : []).filter((task) => task && typeof task.id === 'string').sort(taskOrder);
  reminders = (Array.isArray(fired) ? fired : []).filter((item) => item && typeof item.id === 'string');
  if (removingTask && !tasks.some((task) => task.id === removingTask)) removingTask = null;
  noticeNewThings();
  renderReminders();
  renderTaskAlert();
  renderNotifyOffer();
  const waiting = tasks.filter((task) => task.running && task.running.confirm).length;
  const badge = reminders.length + waiting;
  $('tasks-badge').hidden = !badge;
  $('tasks-badge').textContent = badge ? String(badge) : '';
  if (taskDetail.open) drawDetail();
  syncTaskTimer();

  const print = JSON.stringify([tasks, removingTask, language, navTab]);
  if (print === tasksDrawn) return;
  tasksDrawn = print;
  if (navTab !== 'tasks') return;  // 看不见就不画，换到这个标签时再画
  // 重画会换掉整个列表。焦点原来在哪一行的哪个按钮上，画完放回去
  const active = document.activeElement;
  const row = taskList.contains(active) ? active.closest('.task') : null;
  const keep = row ? [row.dataset.id, active.dataset.role || ''] : null;
  taskList.replaceChildren(...(tasks.length ? tasks.map(taskRow) : [emptyTasks()]));
  const target = keep && [...taskList.children].find((node) => node.dataset.id === keep[0]);
  if (focusCancelTask && target) { focusCancelTask = false; (target.querySelector('.cancel') || target).focus(); }
  else if (target) (target.querySelector(`[data-role="${keep[1]}"]`) || target.querySelector('.task-main') || $('task-new')).focus();
  else if (keep) $('task-new').focus();
}

function taskRow(task) {
  const running = task.running;
  const queued = running && !running.confirm && running.activity && running.activity.kind === 'waiting';
  const row = el('li', 'task ' + (task.kind === 'agent' ? 'kind-agent' : 'kind-reminder') + (task.enabled ? '' : ' off') + (running ? ' running' : '') + (running && running.confirm ? ' waiting' : '') + (queued ? ' queued' : ''));
  row.dataset.id = task.id;

  if (removingTask === task.id) {  // 删除前先在原地确认，和会话、记忆一样
    const box = row.appendChild(el('div', 'task-confirm-row'));
    box.appendChild(el('span', null, t('删除“{title}”？以前运行留下的会话不会删。', { title: task.title })));
    const yes = box.appendChild(el('button', 'mini danger', t('删除')));
    const no = box.appendChild(el('button', 'mini cancel', t('取消')));
    yes.type = no.type = 'button';
    no.dataset.role = 'cancel';
    yes.addEventListener('click', () => deleteTask(task, yes, no));
    no.addEventListener('click', () => { removingTask = null; tasksDrawn = ''; renderTasks(tasks, reminders); focusTask(task.id); });
    box.addEventListener('keydown', (key) => { if (key.key === 'Escape') { key.preventDefault(); key.stopPropagation(); no.click(); } });
    return row;
  }

  const main = row.appendChild(el('button', 'task-main'));
  main.type = 'button';
  main.dataset.role = 'main';
  main.appendChild(icon(task.kind === 'agent' ? 'bot' : 'bell'));
  const words = main.appendChild(el('span', 'task-words'));
  const head = words.appendChild(el('span', 'task-title-line'));
  head.appendChild(el('span', 'task-title', task.title));
  if (task.kind === 'agent' && task.approval in APPROVAL && task.approval !== 'ask') head.appendChild(modeChip(task.approval));
  words.appendChild(el('span', 'task-meta', taskMeta(task)));
  const last = !running && task.runs && task.runs[0];
  if (last) words.appendChild(lastRunLine(task, last));
  main.addEventListener('click', () => openDetail(task.id));

  if (running) {
    row.appendChild(progressBlock(task));
    return row;  // 正在运行的不显示右边的三个按钮：停止、确认都在下面的进度里
  }
  const actions = row.appendChild(el('div', 'task-actions'));
  const button = (iconName, title, label, role, extra) => {
    const node = actions.appendChild(el('button', 'icon-button small' + (extra ? ' ' + extra : '')));
    node.type = 'button';
    node.dataset.role = role;
    node.title = t(title);
    node.setAttribute('aria-label', t(label, { name: task.title }));
    node.appendChild(icon(iconName));
    return node;
  };
  button('play', k('立即运行'), k('立即运行：{name}'), 'run').addEventListener('click', () => runTask(task));
  button('folder', showLabel(), byServer(k('在访达中显示任务的文件'), k('在文件资源管理器中显示任务的文件'), k('在文件管理器中显示任务的文件')), 'reveal').addEventListener('click', () => revealData('tasks'));
  button('edit', k('编辑'), k('编辑任务：{name}'), 'edit').addEventListener('click', () => openEditor(task));
  button('trash', k('删除'), k('删除任务：{name}'), 'delete', 'danger-hover').addEventListener('click', () => {
    removingTask = task.id;
    focusCancelTask = true;
    tasksDrawn = '';
    renderTasks(tasks, reminders);
  });
  return row;
}

// 一个任务"要不要先问你"的那一档，画成一个小标签。名字和对话里的几档是同一套（APPROVAL，在 approval-modes.js）
function modeChip(mode) {
  const chip = el('span', 'chip task-mode ' + mode);
  chip.appendChild(icon('shield'));
  chip.appendChild(el('span', null, t(APPROVAL[mode])));
  return chip;
}

function taskMeta(task) {
  let next;
  if (!task.enabled) next = t('已暂停');
  else if (task.next_run) next = t('下次 {when}', { when: momentText(task.next_run) }) + ' · ' + fromNow(task.next_run);
  else next = t('不会再运行了');
  return scheduleText(task.schedule) + ' · ' + next;
}

function lastRunLine(task, run) {
  const line = el('span', 'task-last');
  const status = runStatus(task, run);
  line.appendChild(el('span', 'chip ' + status, t(RUN_STATUS[status] || status)));
  const bits = [momentText(run.started_at)];
  if (run.catch_up) bits.push(run.missed > 1 ? t('补跑（错过了 {n} 次）', { n: run.missed }) : t('补跑'));
  if (run.manual) bits.push(t('手动运行'));
  if (task.kind === 'agent' && typeof run.cost === 'number') bits.push(costText(run.cost));
  line.appendChild(el('span', null, bits.join(' · ')));
  return line;
}

// 正在运行的那一个：进度条（不知道一共几步，所以是来回走的那种）、第几步、在做什么、用了多久、花了多少、用过的工具
function progressBlock(task) {
  const running = task.running;
  const box = el('div', 'task-progress');
  const bar = box.appendChild(el('div', 'task-bar'));
  bar.setAttribute('role', 'progressbar');
  bar.setAttribute('aria-label', t('任务进度'));
  bar.setAttribute('aria-valuetext', activityText(running));
  bar.appendChild(el('i'));
  box.appendChild(el('p', 'task-activity', activityText(running)));
  const figures = box.appendChild(el('p', 'task-figures'));
  figures.appendChild(el('span', null, running.max_steps ? t('第 {n} 步（最多 {max} 步）', { n: running.step, max: running.max_steps }) : t('第 {n} 步', { n: running.step })));
  const clock = figures.appendChild(el('span', null, t('已用 {time}', { time: durationText(Date.now() / 1000 - running.started_at) })));
  clock.dataset.since = String(running.started_at);
  if (task.kind === 'agent') figures.appendChild(el('span', null, costText(running.cost)));
  if (running.log && running.log.length) {
    const log = box.appendChild(el('ol', 'task-log'));
    log.setAttribute('aria-label', t('用过的工具'));
    for (const entry of running.log.slice(-6)) {
      const item = log.appendChild(el('li', entry.ok === true ? 'ok' : entry.ok === false ? 'bad' : ''));
      item.appendChild(el('span', 'mark', entry.ok === true ? '✓' : entry.ok === false ? '✕' : '…'));
      item.appendChild(el('span', null, entry.summary || entry.tool));
      if (entry.auto) item.appendChild(el('span', 'task-log-auto', t(autoTag(entry.why))));  // 按这个任务的那一档，没问你就放行了
    }
  }
  if (running.confirm) {
    const ask = box.appendChild(el('div', 'task-ask'));
    ask.appendChild(el('span', null, t('任务停下来等你确认这一步：')));
    ask.appendChild(el('code', null, running.confirm.summary || running.confirm.tool));
    const choices = ask.appendChild(el('div', 'row'));
    const allow = choices.appendChild(el('button', 'mini primary', t('允许')));
    const deny = choices.appendChild(el('button', 'mini', t('拒绝')));
    allow.type = deny.type = 'button';
    allow.dataset.role = 'allow';
    deny.dataset.role = 'deny';
    allow.addEventListener('click', () => answerTask(task, running.confirm.id, true, allow, deny));
    deny.addEventListener('click', () => answerTask(task, running.confirm.id, false, allow, deny));
  }
  const end = box.appendChild(el('div', 'row'));
  const stop = end.appendChild(el('button', 'mini', t('停止')));
  stop.type = 'button';
  stop.dataset.role = 'stop';
  stop.addEventListener('click', async () => {
    stop.disabled = true;
    const reply = await call('/api/task/stop', { id: task.id });
    if (!reply.ok) { toast(reply.error); stop.disabled = false; }
  });
  return box;
}

function focusTask(id) {
  const row = [...taskList.children].find((node) => node.dataset.id === id);
  (row ? row.querySelector('.task-main') : $('task-new')).focus();
}

function emptyTasks() {
  const item = el('li', 'task-empty');
  item.appendChild(el('p', null, t('到点让助手自动做一件事，或者按时提醒你。可以只做一次，也可以每天、每周、每月重复。')));
  const examples = [
    { title: k('今天的安排'), kind: 'agent', prompt: k('看看我的待办，按轻重缓急给我写一份今天的安排。'), schedule: { type: 'weekly', days: [1, 2, 3, 4, 5], time: '09:00' } },
    { title: k('写日记'), kind: 'reminder', prompt: k('花五分钟写写今天的日记。'), schedule: { type: 'daily', time: '21:30' } },
  ];
  for (const example of examples) {
    const button = item.appendChild(el('button', 'task-example'));
    button.type = 'button';
    button.appendChild(el('b', null, t(example.title)));
    button.appendChild(el('span', null, scheduleText(example.schedule) + ' · ' + (example.kind === 'agent' ? t('让助手去做') : t('只提醒我'))));
    button.addEventListener('click', () => openEditor(null, { ...example, title: t(example.title), prompt: t(example.prompt) }));
  }
  return item;
}

// 任务和你的对话同时进行，不用等你这一轮结束；同时运行的任务到了上限时才排队（助手程序在回答里说 queued）
async function runTask(task) {
  const reply = await call('/api/task/run', { id: task.id });
  if (!reply.ok) { toast(reply.error); return; }
  toast(task.kind === 'reminder' ? t('已经提醒了：{title}', { title: task.title })
    : reply.data.queued ? t('排好了：同时运行的任务到了上限，前面的跑完一个就开始“{title}”。', { title: task.title }) : t('开始运行：{title}', { title: task.title }));
}

async function answerTask(task, confirmId, allow, ...buttons) {
  for (const button of buttons) button.disabled = true;
  const reply = await call('/api/task/confirm', { id: task.id, confirm_id: confirmId, allow });
  if (!reply.ok) { toast(reply.error); for (const button of buttons) button.disabled = false; }
}

async function deleteTask(task, ...buttons) {
  for (const button of buttons) button.disabled = true;
  const reply = await call('/api/task/delete', { id: task.id });
  removingTask = null;
  tasksDrawn = '';
  if (!reply.ok) { toast(reply.error); renderTasks(tasks, reminders); focusTask(task.id); return; }
  renderTasks(tasks.filter((other) => other.id !== task.id), reminders);
  toast(t('已删除任务：{title}', { title: task.title }));
  if (taskDetail.open && detailId === task.id) taskDetail.close();
}
