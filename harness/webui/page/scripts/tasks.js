  // ---- 定时任务 ------------------------------------------------------------
  // 左边栏的第二个标签。任务有两种：到点让助手去做一件事（会调用模型、花钱），或者只是提醒你（不花钱）。
  // 任务在助手程序里排着、到点由它去跑（见 tasks.py），页面只负责看和改：列表、正在运行的那一个的进度、
  // 等你确认的操作、到点的提醒、运行记录。和记忆一样，这些都跟着状态走（state.tasks、state.reminders），页面不轮询。
  // 页面自己每秒走一下的只有"已经用了多久"这一个数字（改的是文字，不是动画），和每半分钟一次的"还有多久"。

  const taskList = $('task-list'), taskEditor = $('task-editor'), taskDetail = $('task-detail');
  let tasks = [], reminders = [];
  let tasksDrawn = '', remindersDrawn = '', detailDrawn = '';
  let knownReminders = null, knownWaits = null;  // 上一次画的时候有哪些提醒、哪些等你确认的操作。新出现的才弹通知
  // 左边栏的标签：会话、任务、多媒体。navTab 是现在显示的那个；navChosen 是你上次选的那个。
  // 两个分开记：任务和多媒体的标签要等助手程序发来第一份状态才出现，刚打开页面时只能先显示"会话"，
  // 等你上次选的那个标签出现了再换过去（见 syncNavTabs）。只有你自己点的才记下来
  const NAV_TABS = ['chats', 'tasks', 'media'];
  let navTab = 'chats', navChosen = 'chats';
  try { if (NAV_TABS.includes(localStorage.getItem('harness.nav-tab'))) navChosen = localStorage.getItem('harness.nav-tab'); } catch (_) { /* 记不住就用默认的 */ }
  let removingTask = null, focusCancelTask = false;  // 正在原地确认删除的任务
  let detailId = null, detailConfirm = false, detailMessage = '';  // 详情框里看的是哪一个
  let editing = null;  // 编辑框：{id, kind, type, days, base, armed, saving}
  let taskTimer = 0, taskTicks = 0;

  const pad2 = (number) => String(number).padStart(2, '0');
  const RUN_STATUS = { running: k('正在运行'), waiting: k('等你确认'), queued: k('排队中'), done: k('完成'), failed: k('失败'), stopped: k('已停止'), missed: k('错过了') };
  // 助手程序说 waiting 有两种：排在你这一轮（或别的任务）后面，和停下来等你确认。看有没有待确认的操作来分
  const runStatus = (task, run) => (run.status === 'waiting' && !(task.running && task.running.confirm) ? 'queued' : run.status);

  // "09:00" 按界面语言写：英文是 9:00 AM，中文和西班牙语是 09:00
  function clockText(hhmm) {
    const [hours, minutes] = String(hhmm || '').split(':').map(Number);
    if (!Number.isFinite(hours) || !Number.isFinite(minutes)) return hhmm || '';
    if (language !== 'en') return pad2(hours) + ':' + pad2(minutes);
    return new Date(2000, 0, 1, hours, minutes).toLocaleTimeString('en', { hour: 'numeric', minute: '2-digit' });
  }

  // 一个时刻：今天、明天的写"今天 09:00"，别的写上日期和星期
  function momentText(seconds) {
    const date = new Date(seconds * 1000), now = new Date();
    const clock = clockText(pad2(date.getHours()) + ':' + pad2(date.getMinutes()));
    const tomorrow = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
    if (startOfDay(date) === startOfDay(now)) return t('今天 {time}', { time: clock });
    if (startOfDay(date) === startOfDay(tomorrow)) return t('明天 {time}', { time: clock });
    const options = { month: 'short', day: 'numeric', weekday: 'short' };
    if (date.getFullYear() !== now.getFullYear()) options.year = 'numeric';
    return date.toLocaleDateString(LOCALES[language], options) + ' ' + clock;
  }

  // "3 小时后""5 分钟前"：交给浏览器按界面语言写
  function fromNow(seconds) {
    const diff = seconds - Date.now() / 1000, size = Math.abs(diff);
    if (size < 60) return diff >= 0 ? t('马上') : t('刚刚');
    const words = new Intl.RelativeTimeFormat(LOCALES[language], { numeric: 'auto' });
    if (size < 3600) return words.format(Math.round(diff / 60), 'minute');
    if (size < 86400) return words.format(Math.round(diff / 3600), 'hour');
    return words.format(Math.round(diff / 86400), 'day');
  }

  function durationText(seconds) {
    const total = Math.max(0, Math.round(seconds)), minutes = Math.floor(total / 60);
    return minutes >= 60 ? Math.floor(minutes / 60) + ':' + pad2(minutes % 60) + ':' + pad2(total % 60) : minutes + ':' + pad2(total % 60);
  }

  const costText = (value) => new Intl.NumberFormat(LOCALES[language], {
    style: 'currency', currency: 'USD', currencyDisplay: 'narrowSymbol', minimumFractionDigits: 2, maximumFractionDigits: value && value < 0.1 ? 4 : 2,
  }).format(value || 0);

  // 1 = 周一 …… 7 = 周日。2024 年 1 月 1 日正好是星期一
  const weekdayName = (number) => new Date(2024, 0, number).toLocaleDateString(LOCALES[language], { weekday: 'short' });

  function scheduleText(schedule) {
    const plan = schedule || {};
    const time = clockText(plan.time);
    if (plan.type === 'daily') return t('每天 {time}', { time });
    if (plan.type === 'weekly') {
      const days = [...new Set(plan.days || [])].sort();
      if (days.length === 7) return t('每天 {time}', { time });
      if (days.join() === '1,2,3,4,5') return t('工作日 {time}', { time });
      if (days.join() === '6,7') return t('周末 {time}', { time });
      const names = new Intl.ListFormat(LOCALES[language], { style: 'short', type: 'conjunction' }).format(days.map(weekdayName));
      return t('每{days} {time}', { days: names, time });
    }
    if (plan.type === 'monthly') return t('每月 {day} 日 {time}', { day: plan.day, time });
    if (plan.type === 'interval') {
      const minutes = Number(plan.minutes) || 0;
      return minutes % 60 === 0 ? t('每 {n} 小时', { n: minutes / 60 }) : t('每 {n} 分钟', { n: minutes });
    }
    if (plan.type === 'once') {
      const at = Date.parse(plan.at);  // "2026-11-21T09:00"：不带时区的写法，按本地时间算
      return Number.isFinite(at) ? t('{when}，只一次', { when: momentText(at / 1000) }) : String(plan.at || '');
    }
    return '';
  }

  // 助手程序发来的 summary 都是写好的整句，已经说了是哪个工具（"run_command $ ls"、"执行命令：$ ls"），原样显示，
  // 不在前面再加一遍工具的名字（以前加过，就成了"run_command run_command $ ls"）
  function activityText(running) {
    const doing = running.activity || {};
    if (running.confirm) return t('等你确认：{what}', { what: running.confirm.summary || running.confirm.tool });
    if (doing.kind === 'tool') return doing.summary ? t('正在调用：{summary}', { summary: doing.summary }) : t('正在调用 {tool}', { tool: doing.tool });
    if (doing.kind === 'waiting') return doing.summary || t('排队中');  // 助手程序写好了在等什么（"排队中：同时最多运行 3 个任务"）
    if (doing.kind === 'thinking') return t('正在思考……');
    if (doing.kind === 'writing') return t('正在写回复……');
    return t('正在准备……');
  }

  // ---- 左边栏的标签 ----

  // 哪几个标签能用：助手程序发来的状态里带着任务（state.tasks），"任务"标签才出现；带着多媒体（state.media），
  // "多媒体"标签才出现。只剩"会话"一个的时候，整条标签栏都藏起来
  const shownTabs = () => NAV_TABS.filter((name) => !$('nav-tab-' + name).hidden);
  function syncNavTabs() {
    const visible = shownTabs();
    $('nav-tabs').hidden = visible.length < 2;
    if (!visible.includes(navTab)) setNavTab('chats');
    else if (navChosen !== navTab && visible.includes(navChosen)) setNavTab(navChosen);
  }

  // chosen：是你自己点的（记下来，下次打开还是它）
  function setNavTab(name, focus, chosen) {
    navTab = NAV_TABS.includes(name) && !$('nav-tab-' + name).hidden ? name : 'chats';
    if (chosen) {
      navChosen = navTab;
      try { localStorage.setItem('harness.nav-tab', navTab); } catch (_) { /* 记不住也没关系 */ }
    }
    for (const tab of NAV_TABS) {
      const on = tab === navTab;
      $('nav-tab-' + tab).setAttribute('aria-selected', on ? 'true' : 'false');
      $('nav-tab-' + tab).tabIndex = on ? 0 : -1;  // 标签页只有选中的那个在 Tab 键的顺序里，其余的用方向键去
      if (tab !== 'chats') body.classList.toggle(tab + '-tab', on);  // 样式靠它藏起会话的搜索框和列表
    }
    $('tasks-view').hidden = navTab !== 'tasks';
    $('media-view').hidden = navTab !== 'media';
    if (focus) $('nav-tab-' + navTab).focus();
    if (navTab === 'tasks') { tasksDrawn = ''; renderTasks(tasks, reminders); }
    if (navTab === 'media') showMediaTab();
    syncTaskTimer();
  }

  for (const tab of NAV_TABS) $('nav-tab-' + tab).addEventListener('click', () => setNavTab(tab, true, true));
  document.querySelector('.nav-tabs').addEventListener('keydown', (key) => {
    const visible = shownTabs(), at = visible.indexOf(navTab);
    const next = { ArrowRight: at + 1, ArrowLeft: at - 1, Home: 0, End: visible.length - 1 }[key.key];
    if (next === undefined) return;
    key.preventDefault();
    setNavTab(visible[(next + visible.length) % visible.length], true, true);
  });

  // ---- 列表 ----

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
    button('folder', SHOW_FILE, ON_MAC ? k('在访达中显示任务的文件') : k('在文件夹中显示任务的文件'), 'reveal').addEventListener('click', () => revealData('tasks'));
    button('edit', k('编辑'), k('编辑任务：{name}'), 'edit').addEventListener('click', () => openEditor(task));
    button('trash', k('删除'), k('删除任务：{name}'), 'delete', 'danger-hover').addEventListener('click', () => {
      removingTask = task.id;
      focusCancelTask = true;
      tasksDrawn = '';
      renderTasks(tasks, reminders);
    });
    return row;
  }

  // 一个任务"要不要先问你"的那一档，画成一个小标签。名字和对话里的三档是同一套（APPROVAL，在"要不要先问你"那一节）
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
        if (entry.auto) item.appendChild(el('span', 'task-log-auto', t('自动允许')));  // 按这个任务的那一档，没问你就放行了
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

  // ---- 任务详情 ----

  function openDetail(id) {
    detailId = id;
    detailConfirm = false;
    detailMessage = '';
    detailDrawn = '';
    drawDetail();
    if (!taskDetail.open) taskDetail.showModal();
    syncTaskTimer();
  }

  function drawDetail() {
    const task = tasks.find((item) => item.id === detailId);
    if (!task) {
      if (taskDetail.open && detailId) { detailId = null; taskDetail.close(); toast(t('这个任务已经不在了。')); }
      return;
    }
    const print = JSON.stringify([task, language, detailConfirm, detailMessage]);
    if (print === detailDrawn) return;
    detailDrawn = print;
    $('task-detail-title').textContent = task.title;
    $('task-detail-sub').textContent = (task.kind === 'agent' ? t('让助手去做') : t('只提醒我')) + ' · ' + scheduleText(task.schedule);

    const parts = [];
    const section = (title) => { const node = el('section'); node.appendChild(el('h3', null, title)); parts.push(node); return node; };
    if (task.running) section(t('正在运行')).appendChild(progressBlock(task));
    section(t('下次运行')).appendChild(el('p', 'task-next', !task.enabled ? t('已暂停，不会运行。')
      : task.next_run ? momentText(task.next_run) + ' · ' + fromNow(task.next_run) : t('不会再运行了')));
    if (task.kind === 'agent' && task.approval in APPROVAL) {
      const line = section(t('动手之前要不要先问你')).appendChild(el('p', 'task-mode-line'));
      line.appendChild(modeChip(task.approval));
      line.appendChild(el('span', null, task.approval === 'all' ? t('任务运行时多半没人看着：选这一项，执行命令、改文件都不会再等你批准。只给你完全信任的指令用。')
        : task.approval === 'edits' ? t('改工作目录里的文件直接做；执行命令等别的操作，停下来等你批准。')
          : t('要确认的操作会停下来，等你在任务列表里批准。')));
    }
    if (task.prompt) section(task.kind === 'agent' ? t('让助手做什么') : t('提醒的内容')).appendChild(el('pre', null, task.prompt));
    const history = section(t('运行记录'));
    if (!task.runs || !task.runs.length) history.appendChild(el('p', 'hint', t('还没有运行过。')));
    else {
      const list = history.appendChild(el('ol', 'runs'));
      for (const run of task.runs) list.appendChild(runItem(task, run));
    }
    const keep = taskDetail.contains(document.activeElement) ? document.activeElement.id || document.activeElement.dataset.role : '';
    $('task-detail-body').replaceChildren(...parts);
    drawDetailFoot(task);
    if (keep) { const again = $(keep) || taskDetail.querySelector(`[data-role="${keep}"]`); if (again) again.focus(); }
  }

  function runItem(task, run) {
    const item = el('li', 'run');
    const row = item.appendChild(el('div', 'row'));
    const status = runStatus(task, run);
    row.appendChild(el('span', 'chip ' + status, t(RUN_STATUS[status] || status)));
    row.appendChild(el('span', null, momentText(run.started_at)));
    if (run.catch_up) row.appendChild(el('span', 'chip', run.missed > 1 ? t('补跑（错过了 {n} 次）', { n: run.missed }) : t('补跑')));
    if (run.manual) row.appendChild(el('span', 'chip', t('手动运行')));
    if (run.finished_at && run.finished_at > run.started_at) row.appendChild(el('span', null, t('用时 {time}', { time: durationText(run.finished_at - run.started_at) })));
    if (run.steps) row.appendChild(el('span', null, t('{n} 步', { n: run.steps })));
    if (task.kind === 'agent' && typeof run.cost === 'number') row.appendChild(el('span', null, costText(run.cost)));
    if (run.conversation) {
      const open = row.appendChild(el('button', 'mini open', t('打开对话')));
      open.type = 'button';
      open.addEventListener('click', () => openRunConversation(run.conversation));
    }
    if (run.summary) item.appendChild(el('p', null, run.summary));
    if (run.error) item.appendChild(el('p', 'bad', run.error));
    return item;
  }

  async function openRunConversation(id) {
    if (busy) { toast(t(BUSY_HINT)); return; }
    const reply = await call('/api/conversation/open', { id });
    if (!reply.ok) { toast(reply.error); return; }
    taskDetail.close();
    setNavTab('chats', false, true);
    if (overlayNav.matches) setNav(false, false);
  }

  function drawDetailFoot(task) {
    const words = el('p', 'memory-message' + (detailMessage ? ' bad' : ''), detailConfirm ? t('删除这个任务？以前运行留下的会话不会删。') : detailMessage);
    words.setAttribute('role', 'status');
    const nodes = [words];
    const button = (id, label, iconName, className) => {
      const node = el('button', 'button' + (className ? ' ' + className : ''));
      node.id = id;
      node.type = 'button';
      if (iconName) node.appendChild(icon(iconName));
      node.appendChild(document.createTextNode(label));
      nodes.push(node);
      return node;
    };
    if (detailConfirm) {
      button('task-detail-yes', t('删除'), '', 'danger').addEventListener('click', (click) => deleteTask(task, click.currentTarget));
      button('task-detail-no', t('取消'), '', 'cancel').addEventListener('click', () => { detailConfirm = false; drawDetail(); $('task-detail-delete').focus(); });
    } else {
      const run = button('task-detail-run', t('立即运行'), 'play');
      run.disabled = Boolean(task.running);
      run.addEventListener('click', () => runTask(task));
      button('task-detail-toggle', task.enabled ? t('暂停') : t('恢复')).addEventListener('click', async () => {
        const reply = await call('/api/task/toggle', { id: task.id, enabled: !task.enabled });
        detailMessage = reply.ok ? '' : reply.error;
        drawDetail();
      });
      button('task-detail-edit', t('编辑'), 'edit').addEventListener('click', () => { taskDetail.close(); openEditor(task); });
      button('task-detail-delete', t('删除'), 'trash').addEventListener('click', () => {
        detailConfirm = true;
        drawDetail();
        $('task-detail-no').focus();  // 默认停在"取消"上
      });
    }
    $('task-detail-foot').replaceChildren(...nodes);
  }

  taskDetail.addEventListener('close', () => {
    const id = detailId;
    detailId = null;
    detailConfirm = false;
    detailDrawn = '';
    $('task-detail-body').replaceChildren();
    syncTaskTimer();
    if (id && navTab === 'tasks' && (!document.activeElement || document.activeElement === body)) focusTask(id);
  });
  $('close-task-detail').addEventListener('click', () => taskDetail.close());

  // ---- 新建、编辑任务 ----

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
    else if (key.key === 'Enter' && (ON_MAC ? key.metaKey : key.ctrlKey)) { key.preventDefault(); saveTask(); }
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
