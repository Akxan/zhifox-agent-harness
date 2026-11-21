// ---- 提醒、通知、主区域顶部的那条提示 ----

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

// 新到的提醒、新出现的"等你确认"：页面上弹一条提示；页面不在眼前、又允许了系统通知的话，再弹一个系统通知。
// 刚打开页面时重放出来的那些不算"新"，不弹
function noticeNewThings() {
  const live = isLive();
  for (const item of reminders) {
    if (!knownReminders || !live || knownReminders.has(item.id)) continue;
    toast(t('提醒：{title}', { title: item.title }));
    systemNotify(t('提醒：{title}', { title: item.title }), item.text || '');
  }
  knownReminders = new Set(reminders.map((item) => item.id));
  const waits = tasks.filter((task) => task.running && task.running.confirm);
  for (const task of waits) {
    if (!knownWaits || !live || knownWaits.has(task.running.confirm.id)) continue;
    announce(t('定时任务“{title}”在等你确认', { title: task.title }));
    systemNotify(t('定时任务“{title}”在等你确认', { title: task.title }), task.running.confirm.summary || '');
  }
  knownWaits = new Set(waits.map((task) => task.running.confirm.id));
}

function systemNotify(title, text) {
  if (!('Notification' in window) || Notification.permission !== 'granted' || !document.hidden) return;  // 页面就在眼前时，页面里的提示就够了
  try { new Notification(title, { body: text }); } catch (_) { /* 有的浏览器不让在这里直接创建，就算了 */ }
}

function renderNotifyOffer() {
  const box = $('task-notify');
  const show = 'Notification' in window && Notification.permission === 'default' && tasks.length > 0;
  box.hidden = !show;
  if (!show || box.dataset.language === language) return;
  box.dataset.language = language;
  const ask = el('button', 'mini', t('开启系统通知'));
  ask.type = 'button';
  ask.addEventListener('click', async () => {
    try { await Notification.requestPermission(); } catch (_) { /* 用户关掉了询问 */ }
    box.dataset.language = '';
    renderNotifyOffer();
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

$('task-alert').addEventListener('click', () => {
  const id = $('task-alert').dataset.id;
  setNavTab('tasks');
  if (overlayNav.matches && body.classList.contains('nav-closed')) setNav(true, false);
  if (id) focusTask(id);
});

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
