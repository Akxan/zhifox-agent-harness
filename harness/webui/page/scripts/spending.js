// ---- 花费：今天、本月、明细、预算和导出 ----------------------------------
// 这一节管四处：信息栏费用卡片的下半部分（今天、本月、预算还剩多少）、"花费明细"窗口，
// 以及设置里的"每月预算"和"导出全部数据"。
//
// 数字都来自助手程序的花费账本（见 ledger.py）：每调用一次模型记一笔，重启也不清零。页面不自己算钱、不自己数日子：
//   - 今天、本月、超没超预算跟着状态走（state.spend），每调用一次模型就更新一次；
//   - 明细（最近 30 天每天多少、每个月、每个模型、每种来源）打开窗口时才去要（/api/ledger），
//     窗口开着的时候又调用了模型，就再要一次。
// 超过预算的提示只弹一次：助手程序记着"这个月为多少预算提醒过了"（state.spend.alert），页面记着自己为哪一次
// 弹过（存在浏览器里）。刷新页面不会再弹；超预算的时候页面没开着，下次打开时弹一次。弹过以后，
// 超没超预算还一直写在费用卡片上，底部的费用按钮上也有一个红点。

const spendDialog = $('spend-dialog'), spendBody = $('spend-body');
const SOURCES = { chat: k('对话'), task: k('定时任务'), subagent: k('子 Agent'), channel: k('聊天通道'), voice: k('语音转文字') };
const PERIODS = { month: k('本月'), recent: k('最近 {n} 天'), all: k('全部') };
let spendReport = null, spendProblem = '', spendLoading = false, spendAgain = false;
let spendPick = -1;        // 柱状图上选中的是哪一天（下标）；-1 表示还没选，默认是今天
let spendEnter = false;    // 下一次画出明细时播放入场动画（只在刚打开窗口时；之后的更新不再播）
let spendSeen = '';        // 上一次的 state.spend 和请求次数：变了才去重新要明细
let spendPeriod = recall('harness.spend-period');
if (!(spendPeriod in PERIODS)) spendPeriod = 'month';
let budgetShown = recall('harness.budget-alert') || '';  // 为哪一次超预算弹过提示
let exportInfo = null;     // 数据文件夹有多大（打开设置时问一次）

const callsText = (n) => (n === 1 ? t('1 次调用') : t('{n} 次调用', { n: n.toLocaleString(LOCALES[language]) }));
const tokensText = (n) => new Intl.NumberFormat(LOCALES[language], { notation: 'compact', maximumFractionDigits: 1 }).format(n);
// 语音转文字（见 voice.js）按分钟算钱、一个 token 都没有：不写"输入 0 · 输出 0 token"
const tokenMeta = (row) => (row.input + row.cache_read + row.cache_write + row.output ? t('输入 {input} · 输出 {output} token', { input: tokensText(row.input + row.cache_read + row.cache_write), output: tokensText(row.output) }) : '');
const dayDate = (text) => { const [year, month, day] = text.split('-').map(Number); return new Date(year, month - 1, day); };
const dayText = (text, long) => dayDate(text).toLocaleDateString(LOCALES[language], long ? { month: 'short', day: 'numeric', weekday: 'short' } : { month: 'short', day: 'numeric' });
const monthText = (text) => dayDate(text + '-01').toLocaleDateString(LOCALES[language], { year: 'numeric', month: 'long' });
// local：语音转文字用的你自己运行的兼容服务（见 voice.js），不是一家模型提供方
const providerLabel = (id) => { const found = state && state.providers.find((one) => one.id === id); return found ? found.label : id === 'local' ? t('你自己运行的服务') : id; };
const sourceText = (source) => (SOURCES[source] ? t(SOURCES[source]) : source);

// 信息栏：今天、本月；填了预算的话，再加一条进度和一句"还剩多少"
function renderSpend(spend) {
  $('spend').hidden = !spend;
  $('foot-cost').classList.toggle('over', Boolean(spend && spend.over));
  if (!spend) return;  // 助手程序是旧版本、还没有账本
  $('spend-today').textContent = costText(spend.today);
  $('spend-month').textContent = costText(spend.month);
  const meter = $('budget-meter'), note = $('budget-note');
  meter.hidden = !spend.budget;
  if (spend.budget) {
    const ratio = spend.month / spend.budget;
    meter.classList.toggle('near', !spend.over && ratio >= 0.8);
    meter.classList.toggle('over', spend.over);
    meter.firstElementChild.style.transform = 'scaleX(' + Math.min(1, ratio) + ')';
  }
  note.textContent = spend.error ? t('花费记录写不进磁盘：{error}。这次运行里的数还在，重启以后会少掉这一部分。', { error: spend.error })
    : !spend.budget ? ''
    : spend.over ? t('超出每月预算 {budget}，多花了 {over}', { budget: costText(spend.budget), over: costText(spend.month - spend.budget) })
    : t('每月预算 {budget}，还剩 {left}', { budget: costText(spend.budget), left: costText(spend.budget - spend.month) });
  note.classList.toggle('over', Boolean(spend.over || spend.error));
  note.hidden = !note.textContent;
  $('budget-clear').disabled = !spend.budget;
  noticeBudget(spend);
  // 明细窗口开着的时候又调用了模型（或者换了语言）：重新要一次明细
  const print = JSON.stringify([spend, state.usage.requests, language]);
  if (spendDialog.open && print !== spendSeen) loadSpend();
  spendSeen = print;
  if (settings.open) renderDataSize();
}

function noticeBudget(spend) {
  if (!spend.alert || !spend.over || spend.alert === budgetShown) return;
  budgetShown = spend.alert;
  remember('harness.budget-alert', budgetShown);
  const text = t('这个月已经花了 {spent}，超过了每月预算 {budget}。只是提醒，不会拦住任何操作。', { spent: costText(spend.month), budget: costText(spend.budget) });
  toast(text);
  systemNotify(t('超出每月预算'), text);  // 页面不在眼前、又允许了系统通知的时候（比如定时任务在夜里跑）
}

// ---- 花费明细窗口 ----

function openSpend() {
  spendReport = null;
  spendProblem = '';
  spendPick = -1;
  spendEnter = true;
  drawSpend();
  if (!spendDialog.open) spendDialog.showModal();
  loadSpend();
}

async function loadSpend() {
  if (spendLoading) { spendAgain = true; return; }  // 正在要：等这一次回来再要一次，不同时发好几个
  spendLoading = true;
  const reply = await call('/api/ledger');
  spendLoading = false;
  if (reply.ok) { spendReport = reply.data.report; spendProblem = ''; }
  else if (!spendReport) spendProblem = reply.error;  // 已经画着一份的话，留着它，不换成一句出错
  if (spendDialog.open) drawSpend();
  if (spendAgain) { spendAgain = false; loadSpend(); }
}

function drawSpend() {
  const focusKey = spendBody.contains(document.activeElement) ? document.activeElement.dataset.focus : null;
  const report = spendReport;
  let parts;
  if (spendProblem) parts = [spendEmpty('alert', spendProblem, true)];
  else if (!report) parts = [spendEmpty('chart', t('正在读取花费记录……'))];
  else if (!report.first) parts = [spendEmpty('chart', t('还没有花费记录。从现在起，每调用一次模型都会记一笔：花了多少、用的哪个模型、是对话还是定时任务。'))];
  else parts = spendContent(report, spendEnter);
  if (report) spendEnter = false;
  spendBody.replaceChildren(...parts);
  if (focusKey) {
    const again = spendBody.querySelector('[data-focus="' + focusKey + '"]');
    if (again) again.focus();
  }
}

function spendEmpty(name, text, failed) {
  const box = el('div', 'spend-empty');
  box.appendChild(icon(name));
  box.appendChild(el('p', null, text));
  if (failed) {
    const retry = box.appendChild(el('button', 'button', t('再试一次')));
    retry.type = 'button';
    retry.addEventListener('click', () => { spendProblem = ''; drawSpend(); loadSpend(); });
  }
  return box;
}

function spendContent(report, animate) {
  const parts = [];
  if (report.error) parts.push(el('div', 'memory-problem', t('花费记录写不进磁盘：{error}。这次运行里的数还在，重启以后会少掉这一部分。', { error: report.error })));
  parts.push(spendHero(report), spendChart(report, animate), spendMonths(report, animate), spendBreakdown(report, animate));
  const notes = [t('按每次调用当时的价格估算，实际以各家的账单为准。记录从 {day} 开始。', { day: dayDate(report.first).toLocaleDateString(LOCALES[language], { year: 'numeric', month: 'long', day: 'numeric' }) })];
  if (report.skipped) notes.push(t('账本里有 {n} 行读不懂（可能是写到一半时断电了），已经跳过。', { n: report.skipped }));
  parts.push(el('p', 'hint', notes.join(' ')));
  return parts;
}

// 最上面：本月花了多少（最大的那个数），今天，预算
function spendHero(report) {
  const hero = el('div', 'spend-hero');
  const figure = (label, value, className) => {
    const box = hero.appendChild(el('div'));
    box.appendChild(el('small', null, label));
    return box.appendChild(el('div', className, value));
  };
  const spend = state && state.spend;
  figure(t('本月'), costText(report.periods.month.cost), 'big');
  figure(t('今天'), costText(report.days[report.days.length - 1].cost), 'mid');
  if (spend && spend.budget) {
    figure(t('每月预算'), costText(spend.budget), 'mid' + (spend.over ? ' over' : ''));
  } else {
    const box = hero.appendChild(el('div'));
    box.appendChild(el('small', null, t('每月预算')));
    const set = box.appendChild(el('button', 'link', t('设一个预算')));
    set.type = 'button';
    set.addEventListener('click', () => { spendDialog.close(); openSettings(); $('budget-amount').focus(); });
  }
  return hero;
}

// 最近 30 天的柱状图。每根柱子是一个按钮：鼠标移上去、点一下、或者用左右方向键（Home / End 到头尾），
// 上面那行字就换成那一天的数。只有选中的那一根在 Tab 键的顺序里，整张图只占一个 Tab 位置
function spendChart(report, animate) {
  const days = report.days, last = days.length - 1;
  const section = el('section');
  const head = section.appendChild(el('h3', null, t('最近 {n} 天', { n: days.length })));
  head.appendChild(el('small', null, t('合计 {cost}', { cost: costText(report.periods.recent.cost) })));
  const readout = section.appendChild(el('p', 'chart-readout'));
  const chart = section.appendChild(el('div', 'chart' + (animate ? ' grow-in' : '')));
  chart.setAttribute('role', 'group');
  chart.setAttribute('aria-label', t('最近 {n} 天每天的花费', { n: days.length }));
  chart.style.setProperty('--days', days.length);
  const most = Math.max(0, ...days.map((day) => day.cost));
  chart.appendChild(el('span', 'chart-max', most ? costText(most) : ''));
  chart.appendChild(el('span', 'chart-grid'));
  chart.appendChild(el('span', 'chart-grid half'));
  if (spendPick < 0 || spendPick > last) spendPick = last;

  const describe = (day) => {
    const bits = [dayText(day.day, true), costText(day.cost), callsText(day.requests)];
    if (day.unpriced) bits.push(t('其中 {n} 次没有价格', { n: day.unpriced }));
    return bits;
  };
  const bars = days.map((day, index) => {
    const bar = el('button', 'spend-bar' + (index === last ? ' today' : ''));
    bar.type = 'button';
    bar.dataset.focus = 'bar-' + index;
    bar.setAttribute('aria-label', describe(day).join(' · '));
    bar.style.setProperty('--i', index);
    // 有花费的日子至少露出一小截，不然几分钱的那天和一分没花的那天看起来一样
    bar.appendChild(el('i')).style.setProperty('--h', most && day.cost ? Math.max(0.025, day.cost / most) : 0);
    return bar;
  });
  const pick = (index) => {
    spendPick = index;
    bars.forEach((bar, at) => { bar.classList.toggle('on', at === index); bar.tabIndex = at === index ? 0 : -1; });
    const [date, cost, ...rest] = describe(days[index]);
    readout.replaceChildren(document.createTextNode(date + ' · '), el('b', null, cost), document.createTextNode(' · ' + rest.join(' · ')));
  };
  bars.forEach((bar, index) => {
    bar.addEventListener('pointerenter', () => pick(index));
    bar.addEventListener('focus', () => pick(index));
    bar.addEventListener('click', () => pick(index));
  });
  chart.append(...bars);
  chart.addEventListener('keydown', (key) => {
    const next = { ArrowLeft: spendPick - 1, ArrowRight: spendPick + 1, Home: 0, End: last }[key.key];
    if (next === undefined) return;
    key.preventDefault();
    pick(Math.max(0, Math.min(last, next)));
    bars[spendPick].focus();
  });
  pick(spendPick);

  const axis = section.appendChild(el('div', 'chart-axis'));
  axis.appendChild(el('span', null, dayText(days[0].day)));
  axis.appendChild(el('span', null, dayText(days[Math.floor(last / 2)].day)));
  axis.appendChild(el('span', null, t('今天')));
  return section;
}

// 一行：名字、金额，下面一行小字，再下面一条按比例缩放的细条（share 是 0 到 1；不给就没有）
function spendRow(name, figure, meta, share, mono) {
  const row = el('li', 'spend-row' + (share === null ? ' muted' : ''));
  row.appendChild(el('span', 'name' + (mono ? ' mono' : ''), name)).title = name;
  row.appendChild(el('span', 'figure', figure));
  if (meta) row.appendChild(el('span', 'meta', meta));
  if (share !== null) {
    const bar = row.appendChild(el('span', 'share'));
    bar.setAttribute('aria-hidden', 'true');
    bar.appendChild(el('i')).style.setProperty('--w', Math.min(1, Math.max(0, share)));
  }
  return row;
}

function spendList(rows, animate) {
  const list = el('ul', 'spend-list' + (animate ? ' grow-in' : ''));
  list.append(...rows);
  return list;
}

function spendMonths(report, animate) {
  const section = el('section');
  section.appendChild(el('h3', null, t('每个月')));
  const most = Math.max(0, ...report.months.map((month) => month.cost));
  section.appendChild(spendList(report.months.map((month) =>
    spendRow(monthText(month.month), costText(month.cost), callsText(month.requests), most ? month.cost / most : 0)), animate));
  return section;
}

// 按模型、按来源分开看。上面三个按钮切换时间段（本月、最近 30 天、全部），三份数字一次都拿来了，切换时不用再要
function spendBreakdown(report, animate) {
  const section = el('section');
  section.appendChild(el('h3', null, t('按模型和来源')));
  const group = section.appendChild(el('div', 'segmented spend-period'));
  group.setAttribute('role', 'radiogroup');
  group.setAttribute('aria-label', t('时间段'));
  const keys = Object.keys(PERIODS);
  const choose = (key, focus) => {
    spendPeriod = key;
    remember('harness.spend-period', key);
    drawSpend();
    if (focus) spendBody.querySelector('[data-focus="period-' + key + '"]').focus();
  };
  for (const key of keys) {
    const button = group.appendChild(el('button', null, t(PERIODS[key], { n: report.days.length })));
    button.type = 'button';
    button.dataset.focus = 'period-' + key;
    button.setAttribute('role', 'radio');
    button.setAttribute('aria-checked', String(key === spendPeriod));
    button.tabIndex = key === spendPeriod ? 0 : -1;
    button.addEventListener('click', () => { if (key !== spendPeriod) choose(key, false); });
  }
  group.addEventListener('keydown', (key) => {
    const at = keys.indexOf(spendPeriod);
    const next = { ArrowRight: at + 1, ArrowDown: at + 1, ArrowLeft: at - 1, ArrowUp: at - 1, Home: 0, End: keys.length - 1 }[key.key];
    if (next === undefined) return;
    key.preventDefault();
    choose(keys[(next + keys.length) % keys.length], true);
  });

  const period = report.periods[spendPeriod];
  if (!period.requests) {
    section.appendChild(el('p', 'none', t('这段时间里没有调用过模型。')));
    return section;
  }
  if (period.models.length) {
    const top = Math.max(...period.models.map((row) => row.cost));
    section.appendChild(el('p', 'spend-sub', t('模型')));
    section.appendChild(spendList(period.models.map((row) =>
      spendRow(row.model, costText(row.cost), [providerLabel(row.provider), callsText(row.requests), tokenMeta(row)].filter(Boolean).join(' · '), top ? row.cost / top : 0, true)), animate));
  }
  const topSource = Math.max(...period.sources.map((row) => row.cost));
  section.appendChild(el('p', 'spend-sub', t('来源')));
  section.appendChild(spendList(period.sources.map((row) =>
    spendRow(sourceText(row.source), costText(row.cost), callsText(row.requests), topSource ? row.cost / topSource : 0)), animate));
  if (period.unpriced.length) {
    section.appendChild(el('p', 'spend-sub', t('没有价格的模型')));
    section.appendChild(spendList(period.unpriced.map((row) =>
      spendRow(row.model, t('未计入'), [providerLabel(row.provider), callsText(row.requests), tokenMeta(row)].filter(Boolean).join(' · '), null, true)), false));
    section.appendChild(el('p', 'hint', t('这些调用发生时，模型还没有价格，只记下了 token 数，没有算进上面的金额。在“设置”里给它填上价格以后，之后的调用就会计入。')));
  }
  return section;
}

$('spend-open').addEventListener('click', openSpend);
$('close-spend').addEventListener('click', () => spendDialog.close());

// ---- 设置里的"每月预算"和"导出全部数据" ----
// 预算是多少、有没有超，由助手程序判断（见 budget.py），这里只负责把你填的数交过去。
// 导出时用 fetch 拿到压缩包、变成 Blob，再交给一个带 download 属性的链接存盘：请求照样带着口令，
// 地址里不用露出口令，页面的安全策略也不用为下载放宽什么。

function fillDataSettings() {  // 每次打开设置时调用
  const spend = state && state.spend;
  $('budget-amount').value = spend && spend.budget ? String(spend.budget) : '';
  $('budget-clear').disabled = !(spend && spend.budget);
  exportInfo = null;
  renderDataSize();
  refreshDataSize();
}

async function saveBudget(amount) {
  const reply = await call('/api/budget', { amount });
  if (!reply.ok) { say(reply.error, 'bad'); return; }
  if (amount === null) { $('budget-amount').value = ''; say(t('已经不设每月预算了。'), 'good'); return; }
  // 设的比这个月已经花的还少：当场说一声（弹出的提示在设置面板后面，看不见）
  const spend = reply.data.spend;
  if (spend && spend.over) say(t('每月预算已保存：{amount}。这个月已经花了 {spent}，超过了它。', { amount: costText(amount), spent: costText(spend.month) }), 'bad');
  else say(t('每月预算已保存：{amount}。', { amount: costText(amount) }), 'good');
}

$('budget-save').addEventListener('click', () => {
  const field = $('budget-amount');
  if (field.value === '' && !field.validity.badInput) {
    say(t('先填一个金额。不想设预算的话，点“不设预算”。'), 'bad');
    field.focus();
    return;
  }
  const amount = Number(field.value);
  saveBudget(Number.isFinite(amount) && !field.validity.badInput ? amount : field.value);  // 不合格的原样交过去，由助手程序说明原因
});
$('budget-amount').addEventListener('keydown', (key) => {
  if (key.key === 'Enter' && !key.isComposing) { key.preventDefault(); $('budget-save').click(); }
});
$('budget-clear').addEventListener('click', () => saveBudget(null));

async function refreshDataSize() {
  const reply = await call('/api/export/info');
  exportInfo = reply.ok ? reply.data : null;
  renderDataSize();
}

function renderDataSize() {
  const box = $('export-size');
  box.textContent = !exportInfo ? ''
    : exportInfo.files === 1 ? t('数据文件夹 {size}，1 个文件', { size: formatSize(exportInfo.bytes) })
    : t('数据文件夹 {size}，{n} 个文件', { size: formatSize(exportInfo.bytes), n: exportInfo.files.toLocaleString(LOCALES[language]) });
  box.title = exportInfo ? exportInfo.path : '';
}

async function runExport() {
  const button = $('export-run'), mark = button.querySelector('use');
  if (button.disabled) return;
  button.disabled = true;
  button.classList.add('packing');
  mark.setAttribute('href', '#i-loop');
  say(t('正在打包数据文件夹……'));
  try {
    const response = await fetch('/api/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Harness-Token': token },
      body: '{}',
    });
    if (!response.ok) {
      const reply = await response.json().catch(() => ({}));
      say(response.status === 403 && !reply.error ? t('链接无效或已经过期。请使用终端里显示的完整地址重新打开。')
        : reply.error || t('请求失败（{status}）', { status: response.status }), 'bad');
      return;
    }
    const blob = await response.blob();
    const name = (/filename="([^"]+)"/.exec(response.headers.get('Content-Disposition') || '') || [])[1] || 'zhifox-backup.zip';
    const url = URL.createObjectURL(blob);
    const link = el('a');
    link.href = url;
    link.download = name;
    link.hidden = true;
    body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);  // 浏览器开始存盘以后就不需要它了；留一分钟，慢的机器也来得及
    say(exportInfo
      ? t('已导出 {name}：{before} 压缩成了 {after}。里面的 README-backup.txt 写着怎么恢复。', { name, before: formatSize(exportInfo.bytes), after: formatSize(blob.size) })
      : t('已导出 {name}（{after}）。里面的 README-backup.txt 写着怎么恢复。', { name, after: formatSize(blob.size) }), 'good');
  } catch (_) {
    say(t('连接不上助手程序'), 'bad');
  } finally {
    button.disabled = false;
    button.classList.remove('packing');
    mark.setAttribute('href', '#i-download');
  }
}

$('export-run').addEventListener('click', runExport);
