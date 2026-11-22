// ---- 运行记录 · 窗口里画的东西 ------------------------------------------------
// 从上到下：
//   合计      这一轮一共多久（最大的那个数）、花了多少、缓存命中多少、几次请求和工具调用；时间怎么分的：一根分段的横条，
//             下面是图例（模型在答 / 工具在执行 / 你的钩子在跑 / 等你确认 / 别的零碎），每一项写着多久、占几成。
//             "你的钩子在跑"只在这一轮跑过钩子时才有（没配钩子的人不用看到一项永远是 0 的）。
//   瀑布图    每一段一行：左边是名字，右边是这一轮的时间轴，横条从这一段开始的时刻画到结束的时刻。模型请求的横条前面淡的那一截
//             是"还没吐出第一个字"；工具调用里画斜线的那一截是在等你确认。交给子 Agent 的那一步，它自己的请求和调用缩进画在下面。
//             你的钩子每跑一次也是一行（品红）：工具前后跑的缩进画在那次调用下面，你发消息时、一轮答完时跑的单独一行。
//             悬停一行、或者用 Tab / ↑↓ 选中一行，上面那一行字写着它的全部数字；点一下（回车）跳到下面那一段的详情。
//   每一段    逐段列出数字：哪个模型、多少 token、首字时间、停止原因、多少钱；工具的真名、参数、结果多大、问没问你、出错的原因。
// 颜色只是帮忙看：每一行都有图标和名字，等你的那一截有斜线，跳过的是虚线框，出错的标着"出错"，不靠颜色也分得清。
// 会动的只有横条：刚打开窗口时从左往右长出来（transform: scaleX），换语言时重画不再播；减少动态效果时一闪而过（reduce-motion.css）。

const TRACE_SPLIT = [
  ['model', k('模型在答')], ['tools', k('工具在执行')], ['hooks', k('你的钩子在跑')], ['waiting', k('等你确认')], ['other', k('别的零碎')],
];
// 钩子的时刻和结论怎么说，和"扩展 · 钩子"那一页一样：都在 hook-words.js
const TRACE_RESULT = { error: k('出错'), skipped: k('没有执行'), stopped: k('没有执行完') };
const TRACE_SKIPPED_WHY = k('前面的一步失败了，或者参数在生成中途被截断，这一步没有执行。');
// 执行命令时沙箱是哪一档（格式见 harness/trace/__init__.py 的 sandbox；终端 /trace 的说法在 harness/trace/text.py）
const TRACE_SANDBOX = { workspace: k('在沙箱里：只在工作目录里'), network: k('在沙箱里：工作目录 + 联网'), off: k('没有用沙箱'), outside: k('在沙箱之外运行（你放行的）') };
const TRACE_STATUS = { stopped: k('被打断'), error: k('出错了'), compacted: k('压缩历史'), paused: k('搜索还没做完'), refused: k('模型拒绝了') };
const traceCount = (n) => new Intl.NumberFormat(LOCALES[language]).format(traceNumber(n));
const tracePercent = (part, whole) => (whole > 0 ? Math.round((part / whole) * 100) : 0);

function traceWhen(record) {
  const at = new Date(record.at * 1000);
  const today = new Date().toDateString() === at.toDateString();
  const when = record.at ? at.toLocaleString(LOCALES[language], today ? { hour: 'numeric', minute: '2-digit' } : { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : '';
  return [when, t(TRACE_ENDED[record.ended]), record.again ? t('重新生成的') : ''].filter(Boolean).join(' · ');
}

// 每一段一行（子 Agent 的接在它那一步后面，往里缩一格），模型请求按"第几次"编号：主助手和每个子 Agent 各数各的
function traceLanes(spans, depth = 0, out = []) {
  let request = 0;
  for (const span of spans) {
    if (span.kind === 'model') request += 1;
    out.push({ span, depth, request: span.kind === 'model' ? request : 0, key: String(out.length) });
    if (span.children.length) traceLanes(span.children, depth + 1, out);
  }
  return out;
}

// 一段的名字：[原文, 填进去的值]。原文是 null 时，值就是要照原样写的真名（认不出来的工具）。
// 画在页面上用 tel()（记着原文和值：换语言时原地重译，Agent、MCP 服务器的名字算"你起的"），拼进一句话里用 t()
function traceName(lane) {
  const span = lane.span;
  if (span.kind === 'model') return span.compaction && span.status === 'compacted' ? [k('压缩历史')] : [k('第 {n} 次请求'), { n: lane.request }];
  if (span.kind === 'hook') return [k('钩子 · {name}'), { name: span.name || '' }];
  if (span.agent) return [k('交给「{agent}」'), { agent: span.agent }];
  if (!span.label) return [null, span.name || 'tool'];
  return span.server ? [k('{label} · {server}'), { label: { key: span.label, values: {} }, server: span.server }] : [span.label];
}
const traceTitle = (lane) => { const [key, values] = traceName(lane); return key === null ? values : t(key, values); };
const traceTitleEl = (tag, className, lane) => { const [key, values] = traceName(lane); return key === null ? el(tag, className, values) : tel(tag, className, key, values); };

function traceApproval(span) {
  if (span.approval === 'auto') return t(autoTag(span.why));
  if (span.approval === 'allowed') return t('你允许了（等了 {time}）', { time: traceTime(span.waited) });
  if (span.approval === 'refused') return t('你拒绝了（等了 {time}）', { time: traceTime(span.waited) });
  if (span.approval === 'stopped') return t('等你答复时停下了（等了 {time}）', { time: traceTime(span.waited) });
  return '';
}

function traceTokens(span) {
  const fed = traceNumber(span.input) + traceNumber(span.cache_read) + traceNumber(span.cache_write);
  const input = span.cache_read ? t('输入 {n} token（命中缓存 {cached}）', { n: traceCount(fed), cached: traceCount(span.cache_read) }) : t('输入 {n} token', { n: traceCount(fed) });
  return input + ' · ' + t('输出 {n} token', { n: traceCount(span.output) });
}

// 结果里遮住了几处像密钥的东西（见 harness/redact.py；终端 /trace 的说法在 harness/trace/text.py）
const traceRedacted = (span) => (span.redacted === 1 ? t('遮住了 1 处密钥') : t('遮住了 {n} 处密钥', { n: span.redacted }));

// 悬停、选中一行时上面那一行字：这一段的全部数字
function traceSays(lane) {
  const span = lane.span, parts = [traceTitle(lane)];
  if (span.kind === 'model') {
    parts.push(span.model || '', traceTime(span.end - span.start));
    if (span.first !== undefined) parts.push(t('首字 {time}', { time: traceTime(span.first) }));
    if ('cost' in span) parts.push(traceTokens(span), traceCost(span.cost));
    parts.push(span.stop || (TRACE_STATUS[span.status] ? t(TRACE_STATUS[span.status]) : ''));
  } else if (span.kind === 'hook') {
    parts.push(hookEventText(span.event), traceTime(span.end - span.start), t(HOOK_RESULTS[span.result] || HOOK_RESULTS.error));
    if (span.exit !== undefined) parts.push(t('退出码 {code}', { code: span.exit }));
    if (span.note) parts.push(span.note);
  } else {
    parts.push(((span.name || '') + ' ' + (span.detail || '')).trim(), traceTime(span.end - span.start), traceApproval(span));
    if (TRACE_SANDBOX[span.sandbox]) parts.push(t(TRACE_SANDBOX[span.sandbox]));
    if (span.redacted) parts.push(traceRedacted(span));
    parts.push(span.result === 'ok' ? t('结果 {n} 个字', { n: traceCount(span.size) }) : t(TRACE_RESULT[span.result] || k('没有执行完')));
  }
  return parts.filter(Boolean).join(' · ');
}

// ---- 合计 ----

function traceHero(record, animate) {
  const { totals } = record;
  const hero = el('section', 'trace-hero' + (animate ? ' grow-in' : ''));
  const figures = hero.appendChild(el('div', 'trace-figures'));
  const big = figures.appendChild(el('div', 'trace-big'));
  big.append(tel('small', null, k('一共')), el('b', null, traceTime(record.wall)));
  const stats = figures.appendChild(el('dl', 'trace-stats'));
  const stat = (label, value, className) => {
    const box = stats.appendChild(el('div', className));
    box.append(tel('dt', null, label), el('dd', null, value));
  };
  stat(k('花费'), traceCost(totals.cost), totals.cost === null ? 'muted' : '');
  stat(k('缓存命中'), totals.cache_hit === null ? '—' : Math.round(totals.cache_hit * 100) + '%');
  stat(k('请求'), traceCount(totals.requests));
  stat(k('工具调用'), traceCount(totals.tool_calls));

  // 时间怎么分的：几段加起来就是 wall（助手程序保证的，见 harness/trace/recorder.py 的 _totals）。
  // 这一轮没跑过钩子的，不列"你的钩子在跑"那一项
  const split = TRACE_SPLIT.filter(([name]) => name !== 'hooks' || totals.hooks > 0 || totals.hook_runs > 0);
  const whole = Math.max(1, split.reduce((sum, [name]) => sum + totals[name], 0));
  const bar = hero.appendChild(el('div', 'trace-split'));
  bar.setAttribute('role', 'img');
  let from = 0;
  const said = [];
  const legend = hero.appendChild(el('ul', 'trace-legend'));
  for (const [name, label] of split) {
    const size = (totals[name] / whole) * 100;
    if (size > 0) {
      const seg = bar.appendChild(el('i', 'seg-' + name));
      seg.style.setProperty('--from', from.toFixed(3));
      seg.style.setProperty('--size', size.toFixed(3));
    }
    from += size;
    const item = legend.appendChild(el('li', 'seg-' + name));
    item.append(el('i'), tel('span', null, label), el('b', null, traceTime(totals[name])), el('small', null, tracePercent(totals[name], whole) + '%'));
    said.push(t(label) + ' ' + traceTime(totals[name]));
  }
  bar.setAttribute('aria-label', t('时间怎么分的：{parts}', { parts: said.join(t('、')) }));

  const notes = [];
  if (totals.unpriced) notes.push(t('{n} 次请求的模型没有价格，没有算进花费。', { n: totals.unpriced }));
  if (totals.tools_used.web_search) notes.push(t('服务器上联网搜索了 {n} 次（按次另外收费）。', { n: totals.tools_used.web_search }));
  if (totals.hook_runs) {
    notes.push(totals.hooks_blocked
      ? t('跑了 {n} 次你的钩子，其中 {blocked} 次拦下了。', { n: totals.hook_runs, blocked: totals.hooks_blocked })
      : t('跑了 {n} 次你的钩子。', { n: totals.hook_runs }));
  }
  if (record.omitted) notes.push(t('这一轮的段太多，还有 {n} 段没有一段段存下来；合计里算着它们。', { n: record.omitted }));
  if (notes.length) hero.appendChild(el('p', 'trace-hint', notes.join(' ')));
  return hero;
}

// ---- 瀑布图 ----

// 时间轴上的刻度：1、2、5 乘以 10 的几次方里挑一个，让刻度有四五个
function traceTicks(wall) {
  if (wall <= 0) return [];
  const rough = wall / 4, power = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 5, 10].map((n) => n * power).find((n) => n >= rough) || rough;
  const ticks = [];
  for (let at = 0; at <= wall + 1e-6 && ticks.length < 12; at += step) ticks.push(at);
  return ticks;
}

const tracePlace = (node, from, size) => { node.style.setProperty('--from', from.toFixed(3)); node.style.setProperty('--size', size.toFixed(3)); };

function traceLane(lane, record, index) {
  const span = lane.span, wall = Math.max(1, record.wall);
  const row = el('button', 'trace-lane kind-' + span.kind + ' depth-' + Math.min(lane.depth, 3));
  row.type = 'button';
  row.dataset.lane = lane.key;
  row.style.setProperty('--i', String(Math.min(index, 24)));
  const status = span.kind === 'model' ? span.status : span.kind === 'hook' && span.result === 'timeout' ? 'error' : span.result;
  if (status === 'error' || status === 'stopped' || status === 'skipped' || (span.kind === 'hook' && status === 'blocked')) row.classList.add('is-' + status);
  if (span.approval) row.classList.add('was-asked');
  const name = row.appendChild(el('span', 'trace-lane-name'));
  name.append(icon(span.kind === 'model' ? 'spark' : span.icon), traceTitleEl('span', 'trace-lane-title', lane));
  // 用的是备用模型（原来那个暂时用不了，见 harness/fallback.py）：模型名后面标一句
  if (span.kind === 'model' && span.model) name.appendChild(el('span', 'trace-lane-sub', span.fallback ? t('{model}（备用）', { model: span.model }) : span.model));
  if (span.kind === 'hook' && span.event) name.appendChild(el('span', 'trace-lane-sub', span.event));
  const track = row.appendChild(el('span', 'trace-track'));
  const bar = track.appendChild(el('span', 'trace-bar'));
  const duration = Math.max(0, span.end - span.start);
  tracePlace(bar, (span.start / wall) * 100, (duration / wall) * 100);
  if (span.kind === 'model' && span.first !== undefined && duration > 0) {
    // 前面淡的那一截：发出请求、还没吐出第一个字。后面实的：一边想一边写
    tracePlace(bar.appendChild(el('i', 'trace-first')), 0, Math.min(100, (span.first / duration) * 100));
  }
  if (span.asked !== undefined && span.waited && duration > 0) {
    tracePlace(bar.appendChild(el('i', 'trace-wait')), Math.max(0, ((span.asked - span.start) / duration) * 100), Math.min(100, (span.waited / duration) * 100));
  }
  row.appendChild(el('span', 'trace-lane-time', traceTime(duration)));
  row.setAttribute('aria-label', traceSays(lane));
  return row;
}

function traceChart(record, lanes, animate) {
  const chart = el('section', 'trace-chart' + (animate ? ' grow-in' : ''));
  chart.appendChild(tel('h3', null, k('时间线')));
  const readout = chart.appendChild(el('p', 'trace-readout'));
  readout.setAttribute('aria-live', 'polite');
  const hint = () => retext(readout, k('把鼠标移到一行上，或者用 Tab、↑↓ 选中一行，这里写着它的数字；点一下看下面的详情。'));
  hint();
  const plot = chart.appendChild(el('div', 'trace-plot'));
  plot.setAttribute('role', 'group');
  tattr(plot, 'aria-label', k('时间线'));
  const axis = plot.appendChild(el('div', 'trace-axis'));
  axis.setAttribute('aria-hidden', 'true');
  const scale = axis.appendChild(el('span', 'trace-scale'));
  const ticks = traceTicks(record.wall);
  ticks.forEach((at, index) => {
    const tick = scale.appendChild(el('i', index === 0 ? 'first' : index === ticks.length - 1 ? 'last' : '', at === 0 ? '0' : traceTime(at)));
    tick.style.setProperty('--at', ((at / Math.max(1, record.wall)) * 100).toFixed(3));
  });
  lanes.forEach((lane, index) => plot.appendChild(traceLane(lane, record, index)));

  const laneOf = (node) => lanes[Number((node && node.closest && node.closest('.trace-lane') || {}).dataset?.lane)];
  const show = (target) => { const lane = laneOf(target); if (lane) readout.textContent = traceSays(lane); };
  plot.addEventListener('pointerover', (event) => show(event.target));
  plot.addEventListener('focusin', (event) => show(event.target));
  plot.addEventListener('pointerleave', () => { if (!plot.contains(document.activeElement)) hint(); });
  plot.addEventListener('focusout', (event) => { if (!plot.contains(event.relatedTarget)) hint(); });
  plot.addEventListener('click', (event) => { const lane = laneOf(event.target); if (lane) revealTraceStep(lane.key); });
  plot.addEventListener('keydown', (key) => {
    const rows = [...plot.querySelectorAll('.trace-lane')];
    const at = rows.indexOf(document.activeElement);
    if (at < 0) return;
    const next = { ArrowDown: at + 1, ArrowUp: at - 1, Home: 0, End: rows.length - 1 }[key.key];
    if (next === undefined) return;
    key.preventDefault();
    rows[Math.max(0, Math.min(rows.length - 1, next))].focus();
  });
  return chart;
}

// 点了瀑布图里的一行：滚到下面那一段的详情，让它亮一下
function revealTraceStep(key) {
  const step = traceBody.querySelector('.trace-step[data-step="' + key + '"]');
  if (!step) return;
  step.scrollIntoView({ block: 'nearest', behavior: calm() ? 'auto' : 'smooth' });
  step.classList.remove('flash');
  void step.offsetWidth;  // 先让浏览器认下"没有这个类"，再加回去，动画才会重新播
  step.classList.add('flash');
}

// ---- 每一段的详情 ----

function traceTag(text, kind) {
  return el('span', 'trace-tag' + (kind ? ' tag-' + kind : ''), text);
}

function traceStep(lane, number) {
  const span = lane.span;
  const item = el('li', 'trace-step kind-' + span.kind + ' depth-' + Math.min(lane.depth, 3));
  item.dataset.step = lane.key;
  item.appendChild(el('span', 'trace-step-n', String(number)));
  const main = item.appendChild(el('div', 'trace-step-main'));
  const head = main.appendChild(el('p', 'trace-step-head'));
  head.append(icon(span.kind === 'model' ? 'spark' : span.icon), traceTitleEl('b', null, lane), el('span', 'trace-step-time', traceTime(span.end - span.start)));
  main.appendChild(el('p', 'trace-step-when', t('从 {start} 到 {end}', { start: traceTime(span.start), end: traceTime(span.end) })));
  const tags = el('p', 'trace-tags');
  if (span.kind === 'model') {
    main.appendChild(el('p', 'trace-raw', [span.provider, span.model].filter(Boolean).join(' · ')));
    if ('cost' in span) main.appendChild(el('p', 'trace-step-meta', traceTokens(span)));
    const facts = [];
    if (span.first !== undefined) facts.push(t('首字 {time}', { time: traceTime(span.first) }));
    if (span.stop) facts.push(t('停止原因 {reason}', { reason: span.stop }));
    else if (TRACE_STATUS[span.status]) facts.push(t(TRACE_STATUS[span.status]));
    if ('cost' in span) facts.push(traceCost(span.cost));
    if (facts.length) main.appendChild(el('p', 'trace-step-meta', facts.join(' · ')));
    if (span.fallback) tags.appendChild(traceTag(t('备用模型：替 {model}', { model: span.fallback }), 'warn'));
    if (span.compaction) tags.appendChild(traceTag(t('压缩了较早的对话'), 'note'));
    if (span.retries) tags.appendChild(traceTag(t('重试了 {n} 次', { n: span.retries }), 'warn'));
    if (span.truncated) tags.appendChild(traceTag(t('回复被截断了'), 'warn'));
    if (span.calls) tags.appendChild(traceTag(t('要调用 {n} 个工具', { n: span.calls })));
    for (const [tool, count] of Object.entries(span.tools)) {
      tags.appendChild(traceTag(tool === 'web_search' ? t('联网搜索 {n} 次', { n: count }) : tool === 'web_fetch' ? t('读网页 {n} 次', { n: count }) : tool + ' × ' + count, 'note'));
    }
    if (span.note) main.appendChild(el('p', 'trace-note', span.note));  // 失败了、改用备用模型的那一次：为什么失败
  } else if (span.kind === 'hook') {
    main.appendChild(el('p', 'trace-step-meta', hookEventText(span.event)));
    tags.appendChild(traceTag(t(HOOK_RESULTS[span.result] || HOOK_RESULTS.error), hookResultKind(span.result)));
    if (span.exit !== undefined) tags.appendChild(traceTag(t('退出码 {code}', { code: span.exit })));
    if (span.added) tags.appendChild(traceTag(t('交给模型 {n} 个字', { n: traceCount(span.added) }), 'note'));
    if (span.note) main.appendChild(el('p', 'trace-hook-note' + (span.result === 'error' || span.result === 'timeout' ? ' bad' : ''), span.note));
  } else {
    const call = main.appendChild(el('p', 'trace-call'));
    call.append(el('code', 'trace-raw', span.name || ''), el('span', 'trace-detail', span.detail || ''));
    if (span.result === 'ok') {
      main.appendChild(el('p', 'trace-step-meta', t('结果 {n} 个字', { n: traceCount(span.size) }) + (span.images ? ' · ' + t('{n} 张图', { n: span.images }) : '')));
    }
    if (span.approval) tags.appendChild(traceTag(traceApproval(span), span.approval === 'auto' ? 'auto' : span.approval === 'allowed' ? 'ok' : 'warn'));
    if (TRACE_SANDBOX[span.sandbox]) tags.appendChild(traceTag(t(TRACE_SANDBOX[span.sandbox]), span.sandbox === 'outside' ? 'warn' : 'note'));
    if (span.redacted) tags.appendChild(traceTag(traceRedacted(span), 'note'));
    if (span.result && span.result !== 'ok') tags.appendChild(traceTag(t(TRACE_RESULT[span.result] || k('没有执行完')), span.result === 'error' ? 'bad' : 'warn'));
    if (span.result === 'skipped') main.appendChild(tel('p', 'trace-step-meta', TRACE_SKIPPED_WHY));
    if (span.note) main.appendChild(el('p', 'trace-note', span.note));
  }
  if (tags.childElementCount) main.appendChild(tags);
  return item;
}

function traceSteps(record, lanes) {
  const section = el('section', 'trace-section');
  const heading = section.appendChild(el('h3'));
  heading.append(tel('span', null, k('每一段')), el('small', null, t('{n} 段', { n: lanes.length })));
  const list = section.appendChild(el('ol', 'trace-steps'));
  lanes.forEach((lane, index) => list.appendChild(traceStep(lane, index + 1)));
  return section;
}

function traceFoot() {
  return tel('p', 'trace-foot', k('每一次请求都把整段对话重新发给模型，所以输入一次比一次多；命中缓存的那部分便宜得多。交给子 Agent 的那一步，它自己的请求和工具调用缩进画在下面：它们不进主助手的历史，主助手只收到它交回的结论。'));
}

function drawTrace() {
  const record = traceRecords.get(traceShown);
  if (!record) return;
  const focused = traceBody.contains(document.activeElement) ? document.activeElement.dataset.lane : undefined;
  const scroll = traceBody.scrollTop;
  $('trace-sub').textContent = traceWhen(record);
  const lanes = traceLanes(record.spans);
  traceBody.replaceChildren(traceHero(record, traceEnter), traceChart(record, lanes, traceEnter), traceSteps(record, lanes), traceFoot());
  traceEnter = false;
  traceBody.scrollTop = scroll;
  if (focused !== undefined) {
    const again = traceBody.querySelector('.trace-lane[data-lane="' + focused + '"]');
    if (again) again.focus({ preventScroll: true });
  }
}
