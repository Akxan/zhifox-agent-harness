// ---- 定时任务 ------------------------------------------------------------
// 左边栏的第二个标签。任务有两种：到点让助手去做一件事（会调用模型、花钱），或者只是提醒你（不花钱）。
// 任务在助手程序里排着、到点由它去跑（见 tasks.py），页面只负责看和改：列表、正在运行的那一个的进度、
// 等你确认的操作、到点的提醒、运行记录。和记忆一样，这些都跟着状态走（state.tasks、state.reminders），页面不轮询。
// 页面自己每秒走一下的只有"已经用了多久"这一个数字（改的是文字，不是动画），和每半分钟一次的"还有多久"。
//
// 这一节分成五个文件，这个是第一个：总说明、共用的状态、时间和花费的写法，以及左边栏的标签（会话、任务、多媒体）。
// 后面依次是 tasks-list.js 列表、tasks-alerts.js 提醒和通知、tasks-detail.js 详情、tasks-editor.js 新建和编辑。

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
