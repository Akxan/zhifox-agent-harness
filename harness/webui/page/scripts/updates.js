// ---- 检查新版本 ------------------------------------------------------------
// 设置里"检查新版本"一节，和顶栏齿轮右上角的小点。默认关着；打开以后助手程序每天问一次 GitHub。
// 规矩、问到了什么、怎么升级都在 harness/updates.py（状态里的 state.updates，见 harness/webui/updates_api.py）：
// 页面不自己去问 GitHub（页面不连任何外面的地址），也不自己算升级的命令，只照着画。
//   - 开关（/api/updates）：点了马上保存。打开时助手程序马上在后台问一次，"正在检查……"和结果随状态发过来
//   - 下面那一块：正在检查、已经是最新的、没能检查（原因）、还没检查过；有新版本时是那一版、发布说明的链接、
//     怎么升级（一条命令一行，可以复制），和"现在检查"（/api/updates/check，不用等到明天）
//   - 齿轮右上角的小点：有新版本时出现。它只是一个提醒：不闪、不跳，只在刚知道的那一下淡入一次
// 程序给的话都带着原文（said 的样子），这里的话用 tel()：换语言时就地重译；时间和日期照界面的语言写，换语言时随新的状态重画。

let updatesInfo = null;    // 最近一次状态里的 updates（旧的助手程序没有这一项：整节不显示）
let updatesSaving = false; // 正在保存开关：这期间开关不能再点
let updatesDrawn = '';     // 上一次画的是什么（没变就不重画，免得焦点和正在选的文字丢掉）

function renderUpdates(info) {
  updatesInfo = info && typeof info === 'object' ? info : null;
  const news = updatesInfo && updatesInfo.latest;
  const dot = $('settings-news');
  const appeared = Boolean(news) && dot.hidden;
  dot.hidden = !news;
  if (appeared && isLive()) play(dot, [{ opacity: 0, transform: 'scale(.4)' }, { opacity: 1, transform: 'none' }], 320);
  retitleSettingsButton(news || null);  // 齿轮的提示里也写上"有新版本"，和小点照着同一份（见 settings.js）
  $('updates-section').hidden = !updatesInfo;
  if (settings.open) drawUpdates();
}

// 程序给的一句话（{text, key, values}）：带着原文的用 tel() 画，换语言时就地重译
function updatesSaid(tag, className, said) {
  if (said && said.key) return tel(tag, className, said.key, said.values);
  return el(tag, className, said ? String(said.text || '') : '');
}

// 一个时刻（秒），照界面的语言写：11月23日 09:30、Nov 23, 09:30、23 nov, 09:30
function updatesWhen(seconds) {
  const moment = new Date(Number(seconds) * 1000);
  if (!seconds || Number.isNaN(moment.getTime())) return '';
  return moment.toLocaleString(LOCALES[language], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function updatesDay(iso) {
  const moment = new Date(iso);
  if (!iso || Number.isNaN(moment.getTime())) return '';
  return moment.toLocaleDateString(LOCALES[language], { year: 'numeric', month: 'short', day: 'numeric' });
}

function drawUpdates(force) {
  const info = updatesInfo;
  if (!info) return;
  const print = JSON.stringify([info, language, updatesSaving]);
  if (!force && print === updatesDrawn) return;
  updatesDrawn = print;
  const toggle = $('updates-enabled');
  toggle.setAttribute('aria-checked', String(Boolean(info.enabled)));
  toggle.disabled = updatesSaving;
  retext($('updates-state'), info.enabled ? k('（开着）') : k('（关着）'));
  retext($('updates-current'), k('现在用的是 {version}。'), { version: info.current || '?' });
  $('updates-current').hidden = Boolean(info.enabled && info.latest);  // 有新版本时那一块里已经写着"现在是 0.2.0"，不再说一遍
  const card = $('updates-card');
  // 要重画的那一块正拿着焦点的话，记下是哪一个按钮，画完放回去；它不在了就放到开关上（不然 Esc 会绕过去直接关掉设置窗口）
  const focused = card.contains(document.activeElement) ? document.activeElement.dataset.focus || '' : null;
  card.hidden = !info.enabled;
  card.classList.toggle('news', Boolean(info.enabled && info.latest));
  card.replaceChildren(...(!info.enabled ? [] : info.latest ? updatesNews(info) : updatesPlain(info)));
  if (focused !== null) {
    const again = focused && card.querySelector('[data-focus="' + focused + '"]');
    (again || toggle).focus({ preventScroll: true });
  }
}

// 没有新版本时：正在检查、已经是最新的、没能检查、还没检查过
function updatesPlain(info) {
  const kind = info.checking ? 'checking' : info.problem ? 'problem' : info.checked_at ? 'latest' : 'never';
  const row = el('div', 'updates-row ' + kind);
  const glyph = row.appendChild(el('span', 'updates-glyph'));
  glyph.appendChild(icon({ checking: 'refresh', problem: 'alert', latest: 'check', never: 'clock' }[kind]));
  const words = row.appendChild(el('div', 'updates-words'));
  const title = { checking: k('正在问 GitHub……'), problem: k('没能检查'), latest: k('已经是最新的版本'), never: k('还没有检查过') }[kind];
  words.appendChild(tel('b', null, title));
  if (kind === 'problem') words.appendChild(updatesSaid('p', null, info.problem));
  const when = updatesWhen(info.checked_at);
  if (when && kind !== 'checking') words.appendChild(tel('p', 'updates-when', kind === 'problem' ? k('上一次试的时候：{time}。过几个小时会再试。') : k('上一次检查：{time}'), { time: when }));
  row.appendChild(updatesCheckButton(info));
  return [row];
}

// 有新版本时：哪一版、什么时候发布的、发布说明，怎么升级（程序按这一种装法算好的命令）
function updatesNews(info) {
  const news = info.latest;
  const head = el('div', 'updates-row news');
  head.appendChild(el('span', 'updates-glyph')).appendChild(icon('spark'));
  const words = head.appendChild(el('div', 'updates-words'));
  words.appendChild(tel('b', null, k('有新版本：智狐 {version}'), { version: news.version }));
  const day = updatesDay(news.published);
  words.appendChild(day ? tel('p', null, k('现在是 {current} · {date} 发布'), { current: info.current || '?', date: day })
                        : tel('p', null, k('现在是 {current}'), { current: info.current || '?' }));
  const link = words.appendChild(el('a', 'updates-link'));
  link.href = news.page;  // 程序照着 tag 拼的，只会是 https://github.com/… 上这个仓库的发布页
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  link.dataset.focus = 'page';
  link.appendChild(tel('span', null, k('看这一版的发布说明')));
  link.appendChild(icon('external'));
  const parts = [head];
  const plan = info.upgrade;
  if (plan) {
    const steps = el('div', 'updates-steps');
    steps.appendChild(tel('h4', null, k('怎么升级')));
    for (const said of plan.before || []) {
      const warn = steps.appendChild(el('p', 'updates-before'));
      warn.appendChild(icon('alert'));
      warn.appendChild(updatesSaid('span', null, said));
    }
    steps.appendChild(updatesSaid('p', 'updates-lead', plan.lead));
    (plan.commands || []).forEach((command, index) => {
      const row = steps.appendChild(serviceCommand(String(command)));  // 和"后台运行"那一节同一种：一条命令、一个"复制命令"
      row.querySelector('button').dataset.focus = 'copy-' + index;
    });
    for (const said of plan.notes || []) steps.appendChild(updatesSaid('p', 'updates-note', said));
    parts.push(steps);
  }
  const foot = el('div', 'updates-foot');
  const when = updatesWhen(info.checked_at);
  foot.appendChild(when ? tel('span', 'updates-when', k('上一次检查：{time}'), { time: when }) : el('span', 'updates-when'));
  foot.appendChild(updatesCheckButton(info));
  parts.push(foot);
  return parts;
}

// "现在检查"。正在问的时候不用 disabled，而是 aria-disabled：点了它的人焦点就在它上面，整块重画以后要能把焦点放回来
// （disabled 的按钮拿不到焦点，焦点会掉到 body 上，Esc 就绕过去直接关掉了设置窗口）
function updatesCheckButton(info) {
  const button = el('button', 'mini updates-check');
  button.type = 'button';
  button.dataset.focus = 'check';
  button.setAttribute('aria-disabled', String(Boolean(info.checking)));
  button.appendChild(tel('span', null, info.checking ? k('正在检查……') : k('现在检查')));
  button.addEventListener('click', () => { if (!(updatesInfo && updatesInfo.checking)) checkUpdatesNow(); });
  return button;
}

async function saveUpdates(on) {
  updatesSaving = true;
  drawUpdates(true);
  const reply = await call('/api/updates', { enabled: on });
  updatesSaving = false;
  if (!reply.ok) { drawUpdates(true); say(reply.error, 'bad'); return; }
  // 不等状态：马上画成新的样子（打开时正在问；状态随后就到，和这里一样）
  if (updatesInfo) updatesInfo = { ...updatesInfo, enabled: on, checking: on, latest: on ? updatesInfo.latest : null };
  renderUpdates(updatesInfo);
  drawUpdates(true);
  say(on ? t('已打开：现在去问一次，以后每天问一次。') : t('已关掉：不再去问 GitHub。'), 'good');
}

async function checkUpdatesNow() {
  const reply = await call('/api/updates/check', {});
  if (!reply.ok) say(reply.error, 'bad');
}

$('updates-enabled').addEventListener('click', () => {
  if (updatesSaving || !updatesInfo) return;
  saveUpdates(!updatesInfo.enabled);
});
