// ---- 回答引用的网页 ------------------------------------------------------
// Claude 联网搜索以后，回答里出自某个网页的那几句话后面，服务器会发一个 cite 事件（见 harness/webui/ui.py 的 cite、
// harness/citations.py）：这一处引的是第几个来源（marks），以及这段回复里第一次出现的来源（sources：网址、标题、网站）。
// 编号由服务器定，这里照着画。这一节管四件事：
//   · 记下来（noteCite）：记在正在写的那段回复（streaming.js 的 block）上：来源的表（编号 → 来源），每一处记号
//     插在原文的第几个字后面、引了哪几个来源、各自被引的那句原文；
//   · 记号（citeMark）：那段文字后面一个小小的上标编号，点它在新标签页里打开那个网页。鼠标停在上面、或者用键盘移到它上面，
//     浮出一张小卡片（citeCard）：网站、标题、被引的原文。触屏上没有"停在上面"，点了就直接打开；
//   · 来源列表（citeList）：整段回复结束时，在它下面列出"资料来源"，编号、标题、网站；
//   · 复制（citeCopyText）：回复上的"复制"复制的是纯文字：记号写成 [1]，末尾附上每个来源的标题和网址。
//     这样贴到别处，每句话出自哪里、网页在哪儿都还在（联网搜索的文档要求把回答给人看时带上原始来源）；
//     贴进认 Markdown 的地方，列表和网址也照样能用。
//
// 记号怎么进到 Markdown 里：原文（block.text）本身不动，搜索跳转、复制都照着它。画的时候另抄一份（citedSource），
// 在每一处插一个用两个私用区字符包着的编号，markdown.js 认得它，回过头调用这里的 citeMark 建出元素。
// 模型自己写出这两个字符的话（几乎不会），抄的时候先换成 �，冒充不了记号。
// 安全：标题、网站、原文都只用 textContent 放进页面；网址只认 http、https（citeHref），链接在新标签页里打开，
// 带 rel="noopener noreferrer"。会话文件里的 cite 事件可能被改坏，所以每一项都先检查一遍再用。

const CITE_OPEN = '', CITE_CLOSE = '';

// 网址只认 http、https（Markdown 的链接还认 mailto，来源不需要）。规则本身在 markdown.js 的 safeUrl 里，这里只是再收窄一点
function citeHref(url) {
  if (typeof url !== 'string' || !window.Markdown) return null;
  const href = window.Markdown.safeUrl(url);
  return href && /^https?:/i.test(href) ? href : null;
}

const citeString = (value) => (typeof value === 'string' ? value : '');

// 一批来源（事件里的 sources）检查过以后放进 map：编号 → {n, href, title, site}。编号重复的、网址不安全的不要
function keepCiteSources(map, list) {
  for (const source of Array.isArray(list) ? list : []) {
    const n = source && Number(source.n);
    const href = source && citeHref(source.url);
    if (!Number.isInteger(n) || n < 1 || !href || map.has(n)) continue;
    const site = citeString(source.site);
    map.set(n, { n, href, site, title: citeString(source.title) || site || href });
  }
  return map;
}

// 收到一个 cite 事件：记在正在写的那段回复上，下一帧连记号一起画
function noteCite(event) {
  if (!block || block.kind !== 'text') return;  // 记号总是跟在一段回复的文字后面；没有的话（不该发生）无处可放
  keepCiteSources(block.sources, event.sources);
  const refs = [];
  for (const mark of Array.isArray(event.marks) ? event.marks : []) {
    const n = mark && Number(mark.n);
    if (block.sources.has(n) && !refs.some((ref) => ref.n === n)) refs.push({ n, quote: citeString(mark.quote) });
  }
  if (!refs.length) return;
  block.marks.push({ at: block.text.length, refs });
  if (block.view) drawSoon(block);
}

// 记号插在哪儿：那段文字最后一个字的后面，跳过末尾的空白和换行（不然记号会跑到下一行的开头）。
// 有几种行不插，插了这一行就不是它原来的样子了：代码块的 ```、分隔线、表格的分隔行、Setext 标题的 ===。
// 表格的一行以 | 结尾时，插在最后那个 | 前面（插在后面会多出一格）。返回 -1 表示这一处不插
function citeSpot(source, at) {
  let end = Math.min(at, source.length);
  while (end > 0 && /\s/.test(source[end - 1])) end -= 1;
  if (!end) return -1;
  const start = source.lastIndexOf('\n', end - 1) + 1;
  const line = source.slice(start, end);
  if (/^\s*(?:`{3,}|~{3,})/.test(line) || /^ {0,3}([-*_])(?:[ \t]*\1){2,}$/.test(line) || /^ {0,3}=+$/.test(line)) return -1;
  if (line.includes('-') && /^[\s|:-]+$/.test(line)) return -1;
  if (/\|$/.test(line) && line.indexOf('|') < line.length - 1) return start + line.replace(/[ \t]*\|$/, '').length;
  return end;
}

// 每一处记号插在哪儿（按位置排好；不插的那几处去掉）：[[位置, 第几处], ...]
function citeSpots(record, source) {
  return (record.marks || [])
    .map((mark, index) => [citeSpot(source, mark.at), index])
    .filter(([at]) => at >= 0)
    .sort((a, b) => a[0] - b[0] || a[1] - b[1]);
}

// 给 markdown.js 画的那一份原文：插上记号。没有记号的回复原样交出去（只换掉冒充记号的字符）
function citedSource(record) {
  const source = record.text.replace(/[]/g, '�');
  if (!record.marks || !record.marks.length) return source;
  let out = '', from = 0;
  for (const [at, index] of citeSpots(record, source)) {
    out += source.slice(from, at) + CITE_OPEN + index + CITE_CLOSE;
    from = at;
  }
  return out + source.slice(from);
}

// 一处记号的元素（markdown.js 画到记号时调用）：一个上标，里面每个来源一个小编号，都是链接。
// 编号写在 data-n 上、由样式画出来（::after），不是文字节点：搜索跳转按页面上的字去对，编号夹在句子中间会对不上；
// 用鼠标选中一段话复制时也不会多出几个数字。读屏软件读的是 aria-label：编号、标题和网站
const citeInfo = new WeakMap();  // 记号的链接 → {source, quote}：浮出的卡片要用

function citeMark(record, index) {
  const mark = record.marks[index];
  if (!mark) return null;
  const holder = el('sup', 'cite-marks');
  for (const ref of mark.refs) {
    const source = record.sources.get(ref.n);
    if (!source) continue;
    const link = holder.appendChild(citeLink(source, 'cite-mark'));
    link.dataset.n = String(source.n);
    link.setAttribute('aria-label', source.n + '. ' + source.title + (source.site && source.site !== source.title ? ' — ' + source.site : ''));
    citeInfo.set(link, { source, quote: ref.quote });
  }
  return holder.childElementCount ? holder : null;
}

function citeLink(source, className) {
  const link = el('a', className);
  link.href = source.href;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  return link;
}

// 一段回复末尾的"资料来源"。sources 是编号 → 来源的 map
function citeList(sources) {
  const box = el('div', 'cite-sources');
  const head = box.appendChild(el('div', 'cite-head'));
  head.appendChild(icon('globe'));
  head.appendChild(tel('span', null, k('资料来源')));
  const list = box.appendChild(el('ol', 'cite-list'));
  for (const source of [...sources.values()].sort((a, b) => a.n - b.n)) {
    const link = list.appendChild(el('li', 'cite-item')).appendChild(citeLink(source, 'cite-link'));
    link.appendChild(el('span', 'cite-n', String(source.n)));
    link.appendChild(el('span', 'cite-title', source.title));
    link.appendChild(el('span', 'cite-site', source.site && source.site !== source.title ? source.site : ''));
    link.appendChild(icon('external', 'cite-out'));
  }
  return box;
}

// 一段回复写完了：有来源的话，在文字下面、按钮上面列出来。实时写完的淡入一下；重放的直接出现
function showCiteList(record) {
  if (!record.sources || !record.sources.size) return;
  const list = record.box.insertBefore(citeList(record.sources), record.box.querySelector('.msg-actions'));
  if (isLive()) play(list, [{ opacity: 0, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], 320);
}

// "复制"复制的文字：原文里每一处记号写成 [1]，末尾附上来源的标题和网址（为什么，见这一节开头）
function citeCopyText(record) {
  const source = record.text;
  let out = '', from = 0;
  for (const [at, index] of citeSpots(record, source)) {
    out += source.slice(from, at) + record.marks[index].refs.map((ref) => '[' + ref.n + ']').join('');
    from = at;
  }
  out = (out + source.slice(from)).trim();
  if (!record.sources || !record.sources.size) return out;
  const lines = [...record.sources.values()].sort((a, b) => a.n - b.n).map((item) => item.n + '. ' + item.title + ' — ' + item.href);
  return out + '\n\n' + t('资料来源：') + '\n' + lines.join('\n');
}

// ---- 浮出的小卡片 ----
// 整页只有一张，停在哪个记号上就填上哪个来源、摆到它上方（上方放不下就摆到下方），左右不出屏幕。
// 位置用 translate 一次摆好，出现和消失只动 opacity 和 transform；它不接鼠标（pointer-events: none），也不进读屏软件
// （记号的 aria-label 里已经有标题和网站）。对话区一滚动、按 Esc、鼠标或焦点离开，它就收起来
let citeCard = null, citeFor = null;

function citeCardOf() {
  if (citeCard) return citeCard;
  citeCard = el('div', 'cite-card');
  citeCard.setAttribute('aria-hidden', 'true');
  citeCard.append(el('div', 'cite-card-site'), el('div', 'cite-card-title'), el('div', 'cite-card-quote'));
  document.body.appendChild(citeCard);
  return citeCard;
}

function showCiteCard(link) {
  const info = citeInfo.get(link);
  if (!info) return;
  const card = citeCardOf();
  const [site, title, quote] = card.children;
  site.textContent = info.source.site || info.source.href;
  title.textContent = info.source.title;
  quote.textContent = info.quote ? '“' + info.quote + '”' : '';
  quote.hidden = !info.quote;
  citeFor = link;
  const spot = link.getBoundingClientRect(), gutter = 16;
  const width = card.offsetWidth, height = card.offsetHeight;
  const x = Math.max(gutter, Math.min(spot.left + spot.width / 2 - width / 2, window.innerWidth - width - gutter));
  const above = spot.top - height - 8;
  card.classList.toggle('below', above < gutter);
  card.style.translate = Math.round(x) + 'px ' + Math.round(above < gutter ? spot.bottom + 8 : above) + 'px';
  card.classList.add('open');
}

function hideCiteCard() {
  if (citeCard) citeCard.classList.remove('open');
  citeFor = null;
}

const citeMarkAt = (target) => (target && target.closest ? target.closest('.cite-mark') : null);
log.addEventListener('pointerover', (event) => {
  const link = citeMarkAt(event.target);
  if (link && event.pointerType !== 'touch' && link !== citeFor) showCiteCard(link);
});
log.addEventListener('pointerout', (event) => {
  if (citeMarkAt(event.target) && !citeMarkAt(event.relatedTarget)) hideCiteCard();
});
log.addEventListener('focusin', (event) => { const link = citeMarkAt(event.target); if (link) showCiteCard(link); });
log.addEventListener('focusout', (event) => { if (citeMarkAt(event.target)) hideCiteCard(); });
log.addEventListener('keydown', (event) => { if (event.key === 'Escape' && citeFor) hideCiteCard(); });
chat.addEventListener('scroll', () => { if (citeFor) hideCiteCard(); }, { passive: true });
