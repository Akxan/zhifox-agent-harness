// ---- 定时任务 · 提醒、通知、主区域顶部的那条提示 ----------------------------
// 到点的提醒卡片；有新的提醒、新的"等你确认"时在页面上弹一条提示（页面不在眼前时再弹一个系统通知，
// 弹不弹、浏览器允许了没有都由 notify.js 管，和对话里的通知是同一个开关、同一次授权）；
// 主区域顶上那条"有任务在运行"的提示；以及让"已经用了多久"每秒走一下的计时（只在有任务在跑、
// 或者正看着任务列表和详情时开着）。

function renderReminders() {
  const print = JSON.stringify([reminders, language]);
  if (print === remindersDrawn) return;
  remindersDrawn = print;
  const cards = reminders.map((item) => {
    const card = el('div', 'reminder');
    card.appendChild(icon('bell'));
    card.appendChild(el('b', null, item.title));
    if (item.text && item.text !== item.title) card.appendChild(el('p', null, item.text));
    const row = card.appendChild(el('div', 'row'));
    row.appendChild(el('span', null, momentText(item.at)));
    const done = row.appendChild(el('button', 'mini', t('知道了')));
    done.type = 'button';
    done.addEventListener('click', () => dismiss({ reminder_id: item.id }));
    return card;
  });
  if (reminders.length > 1) {
    const all = el('button', 'mini all', t('全部知道了'));
    all.type = 'button';
    all.addEventListener('click', () => dismiss({ all: true }));
    cards.push(all);
  }
  $('reminders').replaceChildren(...cards);
}

async function dismiss(data) {
  const reply = await call('/api/task/dismiss', data);
  if (!reply.ok) toast(reply.error);
}

// 新到的提醒、新出现的"等你确认"：页面上弹一条提示；页面不在眼前、又允许了系统通知的话，再弹一个系统通知
// （见 notify.js 的 systemNotify：点它回到页面；等你确认的那条点了还会打开任务列表，停在这个任务上）。
// 刚打开页面时重放出来的那些不算"新"，不弹。有任务在等你确认时，标签页的标题和图标也标出来（noteTasksWaiting）
function noticeNewThings() {
  const live = isLive();
  for (const item of reminders) {
    if (!knownReminders || !live || knownReminders.has(item.id)) continue;
    toast(t('提醒：{title}', { title: item.title }));
    systemNotify(t('提醒：{title}', { title: item.title }), item.text || '', { tag: 'harness-reminder-' + item.id });
  }
  knownReminders = new Set(reminders.map((item) => item.id));
  const waits = tasks.filter((task) => task.running && task.running.confirm);
  for (const task of waits) {
    if (!knownWaits || !live || knownWaits.has(task.running.confirm.id)) continue;
    announce(t('定时任务“{title}”在等你确认', { title: task.title }));
    systemNotify(t('定时任务“{title}”在等你确认', { title: task.title }), task.running.confirm.summary || '',
      { tag: 'harness-task-confirm-' + task.running.confirm.id, urgent: true, open: () => showTask(task.id) });
  }
  knownWaits = new Set(waits.map((task) => task.running.confirm.id));
  noteTasksWaiting(waits.length > 0);
}

// 任务列表里那句"开启系统通知"：有任务、浏览器还没问过、设置里的"系统通知"也开着时才有。问的是同一次授权（askNotify）
function renderNotifyOffer() {
  const box = $('task-notify');
  const show = notifyOn && notifyPermission() === 'default' && tasks.length > 0;
  box.hidden = !show;
  if (!show || box.dataset.language === language) return;
  box.dataset.language = language;
  const ask = el('button', 'mini', t('开启系统通知'));
  ask.type = 'button';
  ask.addEventListener('click', async () => {
    box.dataset.language = '';  // 问完以后重画（还是没允许的话，换一个按钮也一样能点）
    await askNotify();
  });
  box.replaceChildren(el('span', null, t('到点的时候，就算这个页面不在眼前，也可以弹出系统通知。')), ask);
}

// 主区域顶部的那条提示。只有一个任务在跑时说它的名字；好几个一起跑时数一数：几个在运行、几个在等你、几个在排队。
// 点一下打开任务列表，停在第一个等你确认的（没有的话，第一个在跑的）
function renderTaskAlert() {
  const alert = $('task-alert');
  const active = tasks.filter((task) => task.running);
  alert.hidden = !active.length;
  if (!active.length) return;
  const waiting = active.filter((task) => task.running.confirm);
  const queued = active.filter((task) => !task.running.confirm && task.running.activity && task.running.activity.kind === 'waiting');
  alert.classList.toggle('waiting', waiting.length > 0);
  let text;
  if (active.length === 1) {
    const only = active[0];
    text = waiting.length ? t('定时任务“{title}”在等你确认', { title: only.title })
      : queued.length ? t('定时任务“{title}”在排队：{what}', { title: only.title, what: activityText(only.running) })
        : t('定时任务“{title}”正在运行 · 第 {n} 步', { title: only.title, n: only.running.step });
  } else {
    const parts = [];
    if (active.length > queued.length) parts.push(t('{n} 个在运行', { n: active.length - queued.length }));
    if (waiting.length) parts.push(t('{n} 个在等你确认', { n: waiting.length }));
    if (queued.length) parts.push(t('{n} 个在排队', { n: queued.length }));
    text = t('定时任务：{list}', { list: parts.join(t('、')) });
  }
  $('task-alert-text').textContent = text;
  alert.dataset.id = (waiting[0] || active[0]).id;
}

$('task-alert').addEventListener('click', () => showTask($('task-alert').dataset.id));

// 打开左边栏的任务列表，焦点停在这个任务上（顶上那条提示、系统通知点回来都用它）
function showTask(id) {
  setNavTab('tasks');
  if (overlayNav.matches && body.classList.contains('nav-closed')) setNav(true, false);
  if (id) focusTask(id);
}

// 只在有东西要走字的时候开着：有任务在运行，或者正看着任务列表、任务详情
function syncTaskTimer() {
  const need = tasks.some((task) => task.running) || navTab === 'tasks' || taskDetail.open;
  if (need && !taskTimer) taskTimer = setInterval(onTaskTick, 1000);
  if (!need && taskTimer) { clearInterval(taskTimer); taskTimer = 0; }
}

function onTaskTick() {
  if (document.hidden) return;
  for (const node of document.querySelectorAll('[data-since]')) {
    node.textContent = t('已用 {time}', { time: durationText(Date.now() / 1000 - Number(node.dataset.since)) });
  }
  taskTicks += 1;
  if (taskTicks % 30 === 0) {  // "还有多久"每半分钟重写一次
    tasksDrawn = detailDrawn = '';
    renderTasks(tasks, reminders);
  }
}
