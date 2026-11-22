// ---- 运行记录 --------------------------------------------------------------
// 每一轮结束时，助手程序发来这一轮的运行记录（trace 事件，格式见 harness/trace/__init__.py）：调用了几次模型、
// 用了哪些工具、每一段从什么时候到什么时候、多少 token、多少钱、问没问你、等你等了多久。
// 数字全由助手程序算好（合计也是），页面只照着画，不自己算钱、不自己数时间：
//   - 入口：这一轮最后那个回答下面的那排按钮里，多一个"运行记录"（写着这一轮用了多久、花了多少）；这一轮最后不是回答
//     （出错了、被停下了、最后一步是工具），就在这一轮最后单独放一行。刷新、打开旧会话时它们照样重放出来，不播入场动画；
//   - 点开是一个从右边滑出来的窗口（窄屏上从底下升起来）：合计、一张瀑布图、逐段的数字，画法在 trace-chart.js。
// 记录里的文字只用 textContent 放进页面；会话文件可能被手动改过，所以先检查一遍（traceClean），不认得的字段不要，
// 该是数的不是数就当 0。

const traceDialog = $('trace-dialog'), traceBody = $('trace-body'), traceStatus = $('trace-status');
const traceRecords = new Map();  // 页面上每个"运行记录"按钮对应的那一份记录（按钮上记着编号）
let traceSeq = 0;
let traceShown = 0;      // 窗口里正在看的是哪一份（编号）；0 是没在看
let traceOpener = null;  // 打开窗口的那个按钮：关上时焦点回到它
let traceEnter = false;  // 下一次画的时候播放入场动画（只在刚打开窗口时；换语言时重画不再播）

// 一轮是怎么结束的（和终端 /trace 的说法一样，见 harness/trace/text.py）
const TRACE_ENDED = { done: k('答完了'), truncated: k('回复被截断了'), refused: k('模型拒绝了'), steps: k('用完了步数'), stopped: k('你停下了它'), error: k('出错了') };
const TRACE_TEXT = ['provider', 'model', 'status', 'stop', 'name', 'group', 'label', 'detail', 'result', 'approval', 'why', 'note', 'server', 'agent', 'sandbox', 'fallback'];
const TRACE_COUNTS = ['start', 'end', 'first', 'input', 'cache_read', 'cache_write', 'output', 'calls', 'retries', 'size', 'images', 'redacted', 'asked', 'waited'];
const TRACE_TOTALS = ['model', 'tools', 'waiting', 'other', 'requests', 'tool_calls', 'errors', 'unpriced', 'input', 'cache_read', 'cache_write', 'output', 'asked', 'allowed', 'refused', 'auto'];

const traceNumber = (value) => { const n = Number(value); return typeof value !== 'boolean' && Number.isFinite(n) && n >= 0 ? n : 0; };
const traceMoney = (value) => (value === null || value === undefined || !Number.isFinite(Number(value)) || Number(value) < 0 ? null : Number(value));
const traceCounts = (value) => Object.fromEntries(Object.entries(value && typeof value === 'object' && !Array.isArray(value) ? value : {})
  .filter(([, count]) => traceNumber(count) > 0).slice(0, 8).map(([name, count]) => [String(name).slice(0, 40), traceNumber(count)]));

// 一段：只留认得的字段。时刻夹在这一轮里面（0 到 wall），结束不早于开始
function traceSpan(raw, wall, depth) {
  if (!raw || typeof raw !== 'object' || (raw.kind !== 'model' && raw.kind !== 'tool')) return null;
  const span = { kind: raw.kind };
  for (const name of TRACE_TEXT) if (typeof raw[name] === 'string') span[name] = raw[name].slice(0, 400);
  for (const name of TRACE_COUNTS) if (raw[name] !== undefined && raw[name] !== null) span[name] = traceNumber(raw[name]);
  span.start = Math.min(span.start || 0, wall);
  span.end = Math.min(Math.max(span.end || 0, span.start), wall);
  if ('cost' in raw) span.cost = traceMoney(raw.cost);
  span.compaction = raw.compaction === true;
  span.truncated = raw.truncated === true;
  span.tools = traceCounts(raw.server_tools);
  // 图标的名字只认页面里有的那几个（拼进 <use href="#i-…">）
  span.icon = typeof raw.icon === 'string' && /^[a-z-]{1,24}$/.test(raw.icon) && document.getElementById('i-' + raw.icon) ? raw.icon : 'puzzle';
  span.children = depth < 3 && Array.isArray(raw.children) ? raw.children.map((child) => traceSpan(child, wall, depth + 1)).filter(Boolean) : [];
  return span;
}

// 一份记录。读不懂（不是对象、没有段）的返回 null，这一轮就不放入口
function traceClean(event) {
  if (!event || !Array.isArray(event.spans)) return null;
  const wall = traceNumber(event.wall);
  const raw = event.totals && typeof event.totals === 'object' ? event.totals : {};
  const totals = Object.fromEntries(TRACE_TOTALS.map((name) => [name, traceNumber(raw[name])]));
  totals.cost = traceMoney(raw.cost);
  totals.cache_hit = raw.cache_hit === null || raw.cache_hit === undefined ? null : Math.min(1, traceNumber(raw.cache_hit));
  totals.tools_used = traceCounts(raw.server_tools);
  const spans = event.spans.slice(0, 1000).map((span) => traceSpan(span, wall, 0)).filter(Boolean);
  if (!spans.length) return null;
  return {
    at: traceNumber(event.at), wall, ended: Object.hasOwn(TRACE_ENDED, event.ended) ? event.ended : 'error',
    again: event.again === true, omitted: traceNumber(event.omitted), spans, totals,
    raw: event,  // "复制为 JSON"用：原样的那一份（去掉事件的编号和类型）
  };
}

// 时长：不到 1 秒写毫秒，不到 1 分钟写秒（一位小数），再长写几分几秒
function traceTime(ms) {
  const value = traceNumber(ms);
  const format = (n, digits) => new Intl.NumberFormat(LOCALES[language], { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(n);
  if (value < 1000) return t('{n} 毫秒', { n: format(Math.round(value), 0) });
  if (value < 60000) return t('{n} 秒', { n: format(value / 1000, value < 10000 ? 1 : 0) });
  return t('{m} 分 {s} 秒', { m: Math.floor(value / 60000), s: Math.round((value % 60000) / 1000) });
}
const traceCost = (cost) => (cost === null ? t('没有价格') : costText(cost));

// ---- 入口：每一轮上的"运行记录" ----

// 按钮上的字：这一轮用了多久、花了多少（换语言时重写，见 relabelTraces）
function labelTraceButton(button, record) {
  const words = el('span', 'trace-open-words', traceTime(record.wall) + (record.totals.cost === null ? '' : ' · ' + costText(record.totals.cost)));
  button.replaceChildren(icon('trace'), words);
  const name = t('运行记录：{what}', { what: words.textContent });
  button.setAttribute('aria-label', name);
  button.title = name;
}

function traceButton(id, record) {
  const button = el('button', 'trace-open');
  button.type = 'button';
  button.dataset.trace = String(id);
  labelTraceButton(button, record);
  button.addEventListener('click', () => openTrace(id, button));
  return button;
}

// 收到一份记录：放在这一轮最后那个回答的按钮里；最后不是回答的，在这一轮最后单独放一行
function traceTurn(event) {
  endBlock();  // 一轮的内容到这里都写完了（运行记录总在最后），正停着的那段回复收尾
  const record = traceClean(event);
  if (!record) return;
  const id = ++traceSeq;
  traceRecords.set(id, record);
  const nodes = lastUserNodes();
  const reply = nodes ? lastReplyAfter(nodes.anchor) : null;
  const actions = reply && reply.querySelector(':scope > .msg-actions');
  const button = traceButton(id, record);
  if (actions) actions.insertBefore(button, actions.querySelector('.turn-retry'));
  else add(el('div', 'turn-trace')).appendChild(button);
  if (isLive()) button.classList.add('trace-new');
}

// 换语言：每个入口上的时长和金额按新的语言重写（数字的写法也跟着变），窗口开着的话重画
function relabelTraces() {
  for (const button of log.querySelectorAll('.trace-open')) {
    const record = traceRecords.get(Number(button.dataset.trace));
    if (record) labelTraceButton(button, record);
  }
  if (traceDialog.open) drawTrace();
}

// 页面清空（打开另一段会话、倒回最后一轮）：旧的记录不再有入口，窗口开着就关上
function forgetTraces() {
  traceRecords.clear();
  if (traceDialog.open) traceDialog.close();
}

// ---- 窗口 ----

function openTrace(id, opener) {
  if (!traceRecords.has(id)) return;
  traceShown = id;
  traceOpener = opener || null;
  traceEnter = true;
  traceStatus.textContent = '';
  drawTrace();
  if (!traceDialog.open) traceDialog.showModal();
  traceBody.scrollTop = 0;
  const first = traceBody.querySelector('.trace-lane');
  (first || $('close-trace')).focus({ preventScroll: true });
}

// "复制为 JSON"：原样的那一份记录。成功、失败都写在窗口里（浮在页面上的提示会被窗口的暗底挡住）
async function copyTrace() {
  const record = traceRecords.get(traceShown);
  if (!record) return;
  const { id: _id, type: _type, ...data } = record.raw;
  const button = $('trace-copy');
  try {
    await navigator.clipboard.writeText(JSON.stringify(data, null, 2));
    button.replaceChildren(icon('check'));
    retext(traceStatus, k('已复制这一轮的运行记录（JSON）。'));
    setTimeout(() => button.replaceChildren(icon('copy')), 1400);
  } catch (_) {
    retext(traceStatus, k('复制失败：浏览器没有允许访问剪贴板。'));
  }
}

traceDialog.addEventListener('close', () => {
  traceShown = 0;
  const back = traceOpener && traceOpener.isConnected ? traceOpener : input;
  traceOpener = null;
  back.focus({ preventScroll: true });
});
$('close-trace').addEventListener('click', () => traceDialog.close());
$('trace-copy').addEventListener('click', copyTrace);
// 点窗口外面的暗处也能关上。按下和松开都要在暗处才算（和别的窗口一样，见 settings.js）
let tracePressedOutside = false;
traceDialog.addEventListener('mousedown', (press) => { tracePressedOutside = press.target === traceDialog; });
traceDialog.addEventListener('click', (click) => { if (click.target === traceDialog && tracePressedOutside) traceDialog.close(); });
