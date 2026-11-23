// 回复里的 Markdown：把模型写的标题、列表、表格、代码块……画成页面上的元素。
//
// 这个文件只管一件事：Markdown 原文 → 页面元素。页面（page/scripts/markdown-view.js）只用最下面的 createView。
//
// 为什么自己写，不用现成的库：页面不加载任何外部资源（安全策略不允许，离线也要能用），而完整的 Markdown 库
// 有几千行。我们只需要聊天回复里常见的那部分：CommonMark 的大部分，加上 GitHub 的表格、任务列表、删除线。
// 有意不做的：缩进四格的代码块（模型几乎总是用 ``` 围起来；旧的纯文本回复里有缩进的行，不该突然变成代码）、
// 引用式链接 [文字][编号]、HTML 块。
//
// 和 CommonMark 不一样的两处，都是为了中文：
//   1. 段落里的单个换行就是换行（CommonMark 当成空格）。以前的回复是纯文本，靠换行排版；中文句子之间也不该多出空格。
//   2. 判断 ** 和 ~~ 能不能开始、结束强调时，汉字和全角标点算作标点。否则 "**注意：**这一点" 这种中文里最常见的
//      写法加粗不了（CommonMark 规定：记号一边是标点、另一边是文字时，记号要挨着标点那边的空白才算数）。
//
// 安全：整个过程不拼接、也不解析任何 HTML。先把原文解析成普通的对象（语法树），再用 createElement、
// createTextNode 一个个建出元素，所以回复里的 <script>、onerror= 都只会原样显示成文字。
// 链接只认 http、https、mailto 三种，别的（javascript:、data:、file:、相对路径）按原文显示成文字。
// 图片一律不加载（页面的安全策略也不允许）：只显示说明文字和网址，网址安全的话可以点开。
//
// 来源的记号：联网搜索的回答里，页面在被引用的那段文字后面插一个"记号"——两个私用区的字符包着一个编号
// （CITE_OPEN 编号 CITE_CLOSE，见 page/scripts/citations.js）。这里认出它，交给页面的 hooks.cite(编号) 去建元素
// （这个文件自己不建链接）；代码里、链接文字里不放记号。记号只由页面插入：模型写出来的这两个字符，页面先换成了 �。
//
// 服务器发页面时，把这个文件原样填进页面里一个带 nonce 的 <script>（见 server_page.py 的 _send_page），
// 所以它和页面的其他脚本守同一套安全策略。测试（tests/markdown.test.mjs）用 Node 直接加载这个文件。
(function (scope) {
  'use strict';

  // ---- 行 ------------------------------------------------------------------

  // 一行开头缩进了几列（制表符按 4 列对齐）
  function indentOf(line) {
    let col = 0;
    for (let i = 0; i < line.length; i += 1) {
      if (line[i] === ' ') col += 1;
      else if (line[i] === '\t') col += 4 - (col % 4);
      else break;
    }
    return col;
  }

  // 去掉开头最多 n 列的缩进。制表符只去掉一部分时，剩下的列补成空格
  function unindent(line, n) {
    let col = 0, i = 0;
    while (i < line.length && col < n) {
      if (line[i] === ' ') { col += 1; i += 1; } else if (line[i] === '\t') {
        const width = 4 - (col % 4);
        if (col + width > n) return ' '.repeat(col + width - n) + line.slice(i + 1);
        col += width;
        i += 1;
      } else break;
    }
    return line.slice(i);
  }

  const isBlank = (line) => /^[ \t]*$/.test(line);
  const CITE_OPEN = '\uE000', CITE_CLOSE = '\uE001';
  const CITE_ANY = /\uE000\d*\uE001|[\uE000\uE001]/g;
  const unmark = (text) => text.replace(CITE_ANY, '');  // 代码里不放来源的记号
  const ATX = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$/;
  const HR = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
  const SETEXT = /^ {0,3}(=+|-+)[ \t]*$/;
  const QUOTE = /^ {0,3}>/;
  const ITEM = /^( {0,3})(?:([-+*])|(\d{1,9})([.)]))(?=[ \t]|$)/;
  const DELIMITER_ROW = /^ {0,3}\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$/;

  // ``` 或 ~~~ 开始的代码块。``` 后面那段说明里不能再有 `（否则是行内代码）
  function fenceStart(line) {
    const m = /^( {0,3})(`{3,}|~{3,})(.*)$/.exec(line);
    if (!m || (m[2][0] === '`' && m[3].includes('`'))) return null;
    return { indent: m[1].length, char: m[2][0], size: m[2].length, lang: m[3].trim().split(/\s+/)[0] || '' };
  }

  function fenceEnd(line, fence) {
    const m = /^ {0,3}(`+|~+)[ \t]*$/.exec(line);
    return Boolean(m) && m[1][0] === fence.char && m[1].length >= fence.size;
  }

  // 列表项的开头："- "、"* "、"1. "、"2) "……
  function itemStart(line) {
    const m = ITEM.exec(line);
    if (!m) return null;
    const after = m[0].length;  // 记号后面那一列
    let col = after, i = after;
    while (i < line.length && (line[i] === ' ' || line[i] === '\t')) {
      col += line[i] === '\t' ? 4 - (col % 4) : 1;
      i += 1;
    }
    const empty = i === line.length;
    // 记号后面空了五格以上：内容从记号后一格开始，多出来的缩进算内容的
    const wide = !empty && col - after > 4;
    return {
      ordered: m[3] !== undefined,
      mark: m[3] !== undefined ? m[4] : m[2],  // 同一个列表用同一种记号："-" 换成 "*" 就是另一个列表了
      start: m[3] !== undefined ? parseInt(m[3], 10) : 1,
      width: empty || wide ? after + 1 : col,  // 这一项里后面的行要缩进这么多列，才算这一项的
      empty,
      rest: empty ? '' : wide ? unindent(line.slice(after), 1) : line.slice(i),
    };
  }

  // 表格的一行拆成格子。\| 是格子里的竖线本身；`代码` 里的竖线也不拆（GitHub 会拆，但模型常这么写）
  function splitRow(line) {
    let text = line.trim();
    if (text.startsWith('|')) text = text.slice(1);
    if (text.endsWith('|') && !text.endsWith('\\|')) text = text.slice(0, -1);
    const cells = [];
    let cell = '', i = 0;
    while (i < text.length) {
      const c = text[i];
      if (c === '\\') {
        cell += text[i + 1] === '|' ? '|' : text.slice(i, i + 2);
        i += 2;
      } else if (c === '`') {
        let end = i;
        while (text[end] === '`') end += 1;
        const close = text.indexOf(text.slice(i, end), end);
        const stop = close < 0 ? end : close + end - i;
        cell += text.slice(i, stop).replace(/\\\|/g, '|');
        i = stop;
      } else if (c === '|') {
        cells.push(cell.trim());
        cell = '';
        i += 1;
      } else {
        cell += c;
        i += 1;
      }
    }
    cells.push(cell.trim());
    return cells;
  }

  // 第 i 行和下一行是不是一张表格的表头和分隔行（| --- | :-: |）
  function tableStart(lines, i) {
    const next = lines[i + 1];
    return next !== undefined && lines[i].includes('|') && next.includes('|') && DELIMITER_ROW.test(next) &&
      splitRow(lines[i]).length === splitRow(next).length;
  }

  // 这一行能不能打断上面的段落，自己开始一块新内容
  function interrupts(line) {
    if (fenceStart(line) || ATX.test(line) || HR.test(line) || QUOTE.test(line)) return true;
    const item = itemStart(line);
    return Boolean(item) && !item.empty && (!item.ordered || item.start === 1);
  }

  // ---- 块：段落、标题、代码、引用、列表、表格 ------------------------------
  //
  // 一块内容解析成一个普通对象：{type, key, shape, ...}。
  //   key：这一块的类型加上它的原文。逐字输出时每一帧都重新解析最后几块，key 没变的块就不用重画；
  //   shape：块的"外形"（列表是有序还是无序、表格有几列怎么对齐……）。外形没变、只是里面多了内容时，
  //          只更新里面变了的那几项，不整块重建。
  // 引用和列表里面又是一串块，递归地用同一个函数解析。

  function keyed(block, lines, from, to) {
    block.key = block.type + '\u0001' + (block.shape || '') + '\u0001' + lines.slice(from, to).join('\n');
    block.line = from;
    return block;
  }

  // final：原文已经完整了（不再有新的字进来）。这时没写完的代码块也按写完了处理。from：从第几行开始
  function parseBlocks(lines, final, from) {
    const blocks = [];
    let i = from || 0, blank = false, gap = false;
    while (i < lines.length) {
      const line = lines[i];
      if (isBlank(line)) { blank = blocks.length > 0; i += 1; continue; }
      if (blank) { gap = true; blank = false; }  // 两块之间隔着空行（列表据此决定是"紧凑"还是"宽松"）
      const from = i;
      let block, end = -1;
      const fence = fenceStart(line);
      let m;
      if (fence) {
        const body = [];
        let closed = false;
        for (i += 1; i < lines.length; i += 1) {
          if (fenceEnd(lines[i], fence)) { closed = true; i += 1; break; }
          body.push(fence.indent ? unindent(lines[i], fence.indent) : lines[i]);
        }
        // 没写完的代码块一直到原文末尾。原文以换行结尾时最后那个空行不算（不然代码块下面会多出一行，写完时又缩回去）
        if (!closed && body.length && body[body.length - 1] === '') body.pop();
        const done = closed || final;
        block = { type: 'code', lang: fence.lang, text: unmark(body.join('\n')), done, shape: fence.lang + '\u0002' + done };
      } else if ((m = ATX.exec(line))) {
        const text = (m[2] || '').replace(/(?:^|[ \t]+)#+$/, '');
        block = { type: 'heading', level: m[1].length, text, shape: String(m[1].length) };
        i += 1;
      } else if (HR.test(line)) {
        block = { type: 'hr' };
        i += 1;
      } else if (QUOTE.test(line)) {
        ({ block, i } = parseQuote(lines, i, final));
      } else if (itemStart(line)) {
        ({ block, i, end } = parseList(lines, i, final));
      } else if (tableStart(lines, i)) {
        ({ block, i } = parseTable(lines, i));
      } else {
        ({ block, i } = parseParagraph(lines, i));
      }
      blocks.push(keyed(block, lines, from, end < 0 ? i : end));
      if (end >= 0) i = end;  // 列表末尾的空行交回给这里，好判断后面是不是隔着空行
    }
    blocks.gap = gap;
    return blocks;
  }

  function parseParagraph(lines, i) {
    const text = [lines[i].trim()];
    for (i += 1; i < lines.length; i += 1) {
      const line = lines[i];
      if (isBlank(line)) break;
      const underline = SETEXT.exec(line);  // 段落下面一行 === 或 ---：这一段是标题
      if (underline) {
        const level = underline[1][0] === '=' ? 1 : 2;
        return { block: { type: 'heading', level, text: text.join('\n'), shape: String(level) }, i: i + 1 };
      }
      if (interrupts(line) || tableStart(lines, i)) break;
      text.push(line.trim());
    }
    return { block: { type: 'paragraph', text: text.join('\n'), shape: '' }, i };
  }

  function parseQuote(lines, i, final) {
    const inner = [];
    let fence = null;  // 引用里正开着一个代码块：这时没写 > 的行不能算"接着上一段"
    for (; i < lines.length; i += 1) {
      const line = lines[i];
      const m = /^ {0,3}>/.exec(line);
      let rest;
      if (m) {
        rest = line.slice(m[0].length);
        if (rest[0] === ' ' || rest[0] === '\t') rest = rest.slice(1);
      } else if (!isBlank(line) && inner.length && !isBlank(inner[inner.length - 1]) && !fence && !interrupts(line)) {
        rest = line;  // 懒惰续行：引用里的段落还没完，下一行即使没写 >，也算这一段的
      } else break;
      inner.push(rest);
      if (fence) { if (fenceEnd(rest, fence)) fence = null; } else fence = fenceStart(rest);
    }
    return { block: { type: 'quote', children: parseBlocks(inner, final), shape: '' }, i };
  }

  function parseList(lines, i, final) {
    const first = itemStart(lines[i]);
    const items = [];
    let loose = false, end = i;
    while (i < lines.length) {
      const head = itemStart(lines[i]);
      if (!head || head.ordered !== first.ordered || head.mark !== first.mark || HR.test(lines[i])) break;
      if (items.length && i > end) loose = true;  // 上一项和这一项之间隔着空行
      const from = i;
      const body = [head.rest];
      let fence = fenceStart(head.rest);
      for (i += 1, end = i; i < lines.length; i += 1) {
        const line = lines[i];
        let rest;
        if (isBlank(line)) rest = '';
        else if (indentOf(line) >= head.width) rest = unindent(line, head.width);
        else if (i === end && !fence && !isBlank(body[body.length - 1]) && !itemStart(line) && !interrupts(line)) rest = line;  // 懒惰续行
        else break;
        body.push(rest);
        if (rest) {
          end = i + 1;
          if (fence) { if (fenceEnd(rest, fence)) fence = null; } else fence = fenceStart(rest);
        }
      }
      body.length = end - from;  // 这一项末尾的空行不算这一项的
      const children = parseBlocks(body, final);
      if (children.gap) loose = true;
      // 任务列表："- [ ] 还没做"、"- [x] 做完了"
      let task = null;
      const lead = children[0];
      if (lead && lead.type === 'paragraph') {
        const box = /^\[([ xX])\](?:[ \t]+|$)/.exec(lead.text);
        if (box) {
          task = box[1] !== ' ';
          lead.text = lead.text.slice(box[0].length);
          lead.task = task;
          lead.shape = 'task' + task;
          lead.key = 'task' + task + lead.key;
        }
      }
      items.push(keyed({ type: 'item', children, task, shape: String(task) }, lines, from, end));
      i = Math.max(i, end);
    }
    const shape = [first.ordered, first.mark, first.start].join(' ');  // 宽松还是紧凑不算外形：变了只换一个类名
    return { block: { type: 'list', ordered: first.ordered, start: first.start, loose, items, shape }, i, end };
  }

  function parseTable(lines, i) {
    const head = splitRow(lines[i]);
    const align = splitRow(lines[i + 1]).map((cell) => {
      const left = cell.startsWith(':'), right = cell.endsWith(':');
      return left && right ? 'center' : right ? 'right' : left ? 'left' : '';
    });
    const { rows, i: end } = parseRows(lines, i + 2, align);
    return { block: { type: 'table', head, align, rows, shape: align.join(',') + '\u0002' + lines[i] }, i: end };
  }

  // 表格的内容行，一直读到表格结束。align 是每一列的对齐方式（列数也由它定）
  function parseRows(lines, i, align) {
    const rows = [];
    for (; i < lines.length; i += 1) {
      const line = lines[i];
      if (isBlank(line) || !line.includes('|') || fenceStart(line) || ATX.test(line) || QUOTE.test(line) || HR.test(line)) break;
      const cells = splitRow(line).slice(0, align.length);
      while (cells.length < align.length) cells.push('');
      rows.push(keyed({ type: 'row', cells, align }, lines, i, i + 1));
    }
    return { rows, i };
  }

  // ---- 行内：粗体、斜体、代码、链接…… ---------------------------------------
  //
  // 做法照着 CommonMark 的规范：从左到右扫一遍，文字攒起来；* _ ~ 这类记号先当文字放着，记下它们能不能开始、
  // 能不能结束一段强调；[ 也先记下，遇到 ] 时往回找配对的 [。最后再把记号两两配对，套成粗体、斜体。
  // 结果是一串普通对象：{t: 'text' | 'br' | 'code' | 'em' | 'strong' | 'del' | 'link' | 'img' | 'cite', ...}。

  const ESCAPABLE = '!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~';
  const WHITESPACE = /\s/;
  const PUNCTUATION = /[\p{P}\p{S}]/u;
  const WIDE = /[⺀-鿿가-힯豈-﫿＀-￯]/;  // 汉字、假名、谚文和全角符号
  const AUTOLINK = /<([A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>\uE000\uE001]*)>/y;
  const EMAIL = /<([A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*)>/y;
  const BARE_URL = /https?:\/\/[^\s<>"'`[\]（）【】《》「」，。；！？、\uE000\uE001]+/y;  // 网址到来源的记号为止
  const ENTITY = /&(?:#(\d{1,7})|#[xX]([\da-fA-F]{1,6})|([A-Za-z][A-Za-z0-9]{1,31}));/y;
  const NAMED = {
    amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', copy: '©', reg: '®', trade: '™', hellip: '…',
    mdash: '—', ndash: '–', lsquo: '‘', rsquo: '’', ldquo: '“', rdquo: '”', laquo: '«', raquo: '»', middot: '·',
    bull: '•', times: '×', divide: '÷', plusmn: '±', deg: '°', larr: '←', rarr: '→', uarr: '↑', darr: '↓', harr: '↔',
    euro: '€', pound: '£', yen: '¥', cent: '¢', sect: '§', para: '¶', ne: '≠', le: '≤', ge: '≥', infin: '∞', check: '✓',
  };
  const MAX_URL = 2048;     // 链接网址最多这么长。再长多半是写坏了的，不值得为它往后一直找
  const MAX_LOOKBACK = 1000; // 给一个结束记号往回找开始记号，最多看这么多个。防止故意构造的长串记号把页面卡住

  // 只认 http、https、mailto。浏览器的 URL 解析会去掉混在里面的空白和控制字符，所以 "java\tscript:" 也逃不过
  function safeUrl(raw) {
    const text = raw.trim();
    if (!text || text.length > MAX_URL) return null;
    let url;
    try { url = new URL(text); } catch (_) { return null; }
    return ['http:', 'https:', 'mailto:'].includes(url.protocol) ? url.href : null;
  }

  function decodeEntity(m) {
    if (m[3] !== undefined) return Object.hasOwn(NAMED, m[3]) ? NAMED[m[3]] : null;
    const code = m[1] !== undefined ? parseInt(m[1], 10) : parseInt(m[2], 16);
    return code === 0 || code > 0x10ffff || (code >= 0xd800 && code <= 0xdfff) ? '�' : String.fromCodePoint(code);
  }

  // 网址末尾的标点多半是句子的，不是网址的；右括号要看网址里的括号配不配得上
  function trimUrl(url) {
    for (;;) {
      const last = url[url.length - 1];
      if ('.,;:!?\'"*_~'.includes(last)) url = url.slice(0, -1);
      else if (last === ')' && url.split(')').length > url.split('(').length) url = url.slice(0, -1);
      else return url;
    }
  }

  // 从 at（指着 "(" ）开始读链接的目标："(网址)"、"(<网址>)"、"(网址 "标题")"。读不出来返回 null
  function linkTail(src, at) {
    if (src[at] !== '(') return null;
    let i = at + 1;
    const space = () => { while (i < src.length && (src[i] === ' ' || src[i] === '\t' || src[i] === '\n')) i += 1; };
    space();
    let url;
    if (src[i] === '<') {
      const end = src.indexOf('>', i + 1);
      if (end < 0 || end - i > MAX_URL || /[\n<]/.test(src.slice(i + 1, end))) return null;
      url = src.slice(i + 1, end);
      i = end + 1;
    } else {
      const start = i;
      let depth = 0;
      for (; i < src.length && i - start <= MAX_URL; i += 1) {
        const c = src[i];
        if (c === '\\' && ESCAPABLE.includes(src[i + 1])) i += 1;
        else if (c === '(') depth += 1;
        else if (c === ')') { if (!depth) break; depth -= 1; } else if (c <= ' ' || c === CITE_OPEN || c === CITE_CLOSE) break;
      }
      if (depth) return null;
      url = src.slice(start, i);
    }
    const gap = i;
    space();
    let title = '';
    const quote = src[i];
    if (i > gap && (quote === '"' || quote === "'" || quote === '(')) {
      const close = quote === '(' ? ')' : quote;
      let end = i + 1;
      while (end < src.length && end - i < MAX_URL && src[end] !== close) end += src[end] === '\\' ? 2 : 1;
      if (src[end] !== close) return null;
      title = src.slice(i + 1, end);
      i = end + 1;
      space();
    }
    if (src[i] !== ')') return null;
    const unescape = (text) => text.replace(/\\([!-/:-@[-`{-~])/g, '$1');
    return { url: unescape(url), title: unescape(title), end: i + 1 };
  }

  // 一串文字节点里相邻的文字并成一个，空的去掉
  function tidy(nodes) {
    const out = [];
    for (const { prev, next, ...node } of nodes) {  // 链表用的前后指针不带出去
      if (node.t === 'text') {
        if (!node.v) continue;
        const last = out[out.length - 1];
        if (last && last.t === 'text') { last.v += node.v; continue; }
      }
      out.push(node);
    }
    return out;
  }

  // 链表里 from 和 to 之间（不含两头）的节点拿出来，换成一个新节点
  function wrap(from, to, node) {
    const kids = [];
    for (let at = from.next; at !== to; at = at.next) kids.push(at);
    node.c = tidy(kids);
    node.prev = from;
    node.next = to;
    from.next = node;
    to.prev = node;
    return node;
  }

  function unlink(node) {
    node.prev.next = node.next;
    if (node.next) node.next.prev = node.prev;
  }

  // 把记号两两配对，套成斜体（一个）、粗体（两个）、删除线（~~）。只处理 bottom 之后的那些记号
  function pairUp(delims, bottom) {
    for (let c = bottom; c < delims.length; c += 1) {
      const closer = delims[c];
      if (!closer.close || !closer.n) continue;
      let o = c - 1, opener = null;
      for (let steps = 0; o >= bottom && steps < MAX_LOOKBACK; o -= 1, steps += 1) {
        const it = delims[o];
        if (!it.n || it.ch !== closer.ch || !it.open) continue;
        // "三的规则"：一边既能开始又能结束时，两边记号的个数加起来是 3 的倍数就不配（**a*、*a** 这类）
        if (closer.ch !== '~' && (it.close || closer.open) && (it.size + closer.size) % 3 === 0 && (it.size % 3 || closer.size % 3)) continue;
        opener = it;
        break;
      }
      if (!opener) continue;
      const use = closer.ch === '~' ? 2 : closer.n >= 2 && opener.n >= 2 ? 2 : 1;
      opener.n -= use;
      closer.n -= use;
      opener.node.v = opener.node.v.slice(use);
      closer.node.v = closer.node.v.slice(use);
      wrap(opener.node, closer.node, { t: use === 1 ? 'em' : closer.ch === '~' ? 'del' : 'strong' });
      for (let k = o + 1; k < c; k += 1) delims[k].n = 0;  // 夹在中间没配上对的记号，都只是普通文字了
      if (!opener.n) unlink(opener.node);
      if (!closer.n) unlink(closer.node);
      else c -= 1;  // 结束记号还有剩：再给它找一次
    }
    delims.length = bottom;
  }

  function parseInline(src) {
    const head = { t: 'head', next: null };
    let tail = head, text = '';
    const push = (node) => { node.prev = tail; node.next = null; tail.next = node; tail = node; return node; };
    const flush = () => { if (text) { push({ t: 'text', v: text }); text = ''; } };
    const delims = [];    // 记下的 * _ ~~
    const brackets = [];  // 记下的 [ 和 ![
    const noTicks = {};   // 某个长度的一串 ` 从哪往后再也没有了（找过一次就记住，不重复找）
    let i = 0;
    while (i < src.length) {
      const c = src[i];
      if (c === '\\') {
        const next = src[i + 1];
        if (next === '\n') { flush(); push({ t: 'br' }); i += 2; while (src[i] === ' ' || src[i] === '\t') i += 1; continue; }
        if (next !== undefined && ESCAPABLE.includes(next)) { text += next; i += 2; continue; }
      } else if (c === '\n') {
        text = text.replace(/[ \t]+$/, '');
        flush();
        push({ t: 'br' });
        for (i += 1; src[i] === ' ' || src[i] === '\t';) i += 1;
        continue;
      } else if (c === '`') {
        let end = i;
        while (src[end] === '`') end += 1;
        const size = end - i;
        let close = -1;
        if (!(noTicks[size] <= end)) {
          for (let at = src.indexOf('`', end); at >= 0; at = src.indexOf('`', at)) {
            let stop = at;
            while (src[stop] === '`') stop += 1;
            if (stop - at === size) { close = at; break; }
            at = stop;
          }
          if (close < 0) noTicks[size] = end;
        }
        if (close < 0) { text += src.slice(i, end); i = end; continue; }
        let code = unmark(src.slice(end, close)).replace(/\n/g, ' ');
        if (code.length >= 2 && code[0] === ' ' && code[code.length - 1] === ' ' && /[^ ]/.test(code)) code = code.slice(1, -1);
        flush();
        push({ t: 'code', v: code });
        i = close + size;
        continue;
      } else if (c === '*' || c === '_' || c === '~') {
        let end = i;
        while (src[end] === c) end += 1;
        const size = end - i;
        if (c === '~' && size !== 2) { text += src.slice(i, end); i = end; continue; }  // 单个 ~ 常用来写"5~10 分钟"，不当删除线
        const before = i > 0 ? src[i - 1] : ' ', after = end < src.length ? src[end] : ' ';
        const wide = c !== '_';
        const punct = (ch) => PUNCTUATION.test(ch) || (wide && WIDE.test(ch));
        const left = !WHITESPACE.test(after) && (!punct(after) || WHITESPACE.test(before) || punct(before));
        const right = !WHITESPACE.test(before) && (!punct(before) || WHITESPACE.test(after) || punct(after));
        // _ 夹在两个字母之间（snake_case）不算强调
        const open = c === '_' ? left && (!right || punct(before)) : left;
        const close = c === '_' ? right && (!left || punct(after)) : right;
        flush();
        const node = push({ t: 'text', v: src.slice(i, end) });
        if (open || close) delims.push({ node, ch: c, n: size, size, open, close });
        i = end;
        continue;
      } else if (c === '[' || (c === '!' && src[i + 1] === '[')) {
        flush();
        const image = c === '!';
        brackets.push({ node: push({ t: 'text', v: image ? '![' : '[' }), image, bottom: delims.length, active: true });
        i += image ? 2 : 1;
        continue;
      } else if (c === ']' && brackets.length) {
        const opener = brackets[brackets.length - 1];
        const target = opener.active ? linkTail(src, i + 1) : null;
        if (!target) { brackets.pop(); text += ']'; i += 1; continue; }
        const href = safeUrl(target.url);
        if (!href && !opener.image) { brackets.pop(); text += ']'; i += 1; continue; }  // 不安全的链接：原样显示成文字
        flush();
        pairUp(delims, opener.bottom);
        const end = push({ t: 'text', v: '' });
        const node = wrap(opener.node, end, opener.image
          ? { t: 'img', href, url: target.url }
          : { t: 'link', href, title: target.title });
        unlink(opener.node);
        unlink(end);
        tail = node;
        node.next = null;
        brackets.pop();
        if (!opener.image) for (const it of brackets) if (!it.image) it.active = false;  // 链接里不能再套链接
        i = target.end;
        continue;
      } else if (c === '<') {
        AUTOLINK.lastIndex = EMAIL.lastIndex = i;
        const m = AUTOLINK.exec(src), mail = m ? null : EMAIL.exec(src);
        const href = m ? safeUrl(m[1]) : mail ? safeUrl('mailto:' + mail[1]) : null;
        if (href) {
          flush();
          push({ t: 'link', href, c: [{ t: 'text', v: (m || mail)[1] }] });
          i += (m || mail)[0].length;
          continue;
        }
      } else if (c === CITE_OPEN || c === CITE_CLOSE) {
        // 来源的记号：CITE_OPEN 编号 CITE_CLOSE。不成对的半个记号（不该有）直接丢掉
        const close = c === CITE_OPEN ? src.indexOf(CITE_CLOSE, i + 1) : -1;
        const digits = close > i ? src.slice(i + 1, close) : '';
        if (/^\d{1,6}$/.test(digits)) {
          flush();
          push({ t: 'cite', v: Number(digits) });
          i = close + 1;
        } else i += 1;
        continue;
      } else if (c === '&') {
        ENTITY.lastIndex = i;
        const m = ENTITY.exec(src);
        const decoded = m ? decodeEntity(m) : null;
        if (decoded !== null) { text += decoded; i += m[0].length; continue; }
      } else if (c === 'h' && (i === 0 || !/[A-Za-z0-9]/.test(src[i - 1])) && (src.startsWith('http://', i) || src.startsWith('https://', i))) {
        BARE_URL.lastIndex = i;
        const m = BARE_URL.exec(src);
        const url = m ? trimUrl(m[0]) : '';
        const href = /^https?:\/\/./.test(url) ? safeUrl(url) : null;
        if (href) {
          flush();
          push({ t: 'link', href, c: [{ t: 'text', v: url }] });
          i += url.length;
          continue;
        }
      }
      text += c;
      i += 1;
    }
    flush();
    pairUp(delims, 0);
    const nodes = [];
    for (let at = head.next; at; at = at.next) nodes.push(at);
    return tidy(nodes);
  }

  // 一串行内节点里的纯文字（图片的说明文字用）
  function plain(nodes) {
    return nodes.map((node) => (node.t === 'text' || node.t === 'code' ? node.v : node.c ? plain(node.c) : '')).join('');
  }

  // ---- 代码着色 --------------------------------------------------------------
  // 只认几类最常见的记号：注释、字符串、数字、关键字。做不到逐字精确（那要给每种语言写一个完整的解析器），
  // 但足够让代码的结构一眼看得出来。结果同样只用 <span> 和文字节点建出来。认不出的语言就不着色。

  const KEYWORDS = {
    js: 'async await break case catch class const continue debugger default delete do else export extends false finally for from function get if import in instanceof let new null of return set static super switch this throw true try typeof undefined var void while with yield',
    py: 'and as assert async await break case class continue def del elif else except False finally for from global if import in is lambda match None nonlocal not or pass raise return self True try while with yield',
    sh: 'case do done elif else esac exit export fi for function if in local readonly return set shift source then unset until while',
    go: 'break case chan const continue default defer else fallthrough false for func go goto if import interface map nil package range return select struct switch true type var',
    rust: 'as async await break const continue crate dyn else enum Err extern false fn for if impl in let loop match mod move mut None Ok pub ref return self Self Some static struct super trait true type unsafe use where while',
    c: 'auto bool break case catch char class const constexpr continue default delete do double else enum explicit extern false final float for friend goto if inline int long namespace new nullptr operator override private protected public return short signed sizeof static struct switch template this throw true try typedef typename union unsigned using virtual void volatile while',
    java: 'abstract boolean break byte case catch char class const continue default do double else enum extends false final finally float for fun if implements import in instanceof int interface is let long new null object override package private protected public return short static super switch this throw throws true try val var void when while',
    sql: 'add all alter and as asc begin between by case commit create default delete desc distinct drop else end exists false foreign from full group having if in index inner insert into is join key left like limit not null offset on or order outer primary references returning right rollback select set table then true union unique update values view when where with',
    json: 'false null true',
    conf: 'false no null off on true yes',
  };
  const ALIASES = {
    js: 'js', javascript: 'js', jsx: 'js', mjs: 'js', cjs: 'js', node: 'js', ts: 'js', typescript: 'js', tsx: 'js',
    py: 'py', python: 'py', python3: 'py', py3: 'py', sh: 'sh', bash: 'sh', zsh: 'sh', shell: 'sh', console: 'sh',
    shellscript: 'sh', fish: 'sh', go: 'go', golang: 'go', rs: 'rust', rust: 'rust', c: 'c', h: 'c', cpp: 'c',
    'c++': 'c', cc: 'c', hpp: 'c', cs: 'c', csharp: 'c', objc: 'c', java: 'java', kotlin: 'java', kt: 'java',
    swift: 'java', scala: 'java', dart: 'java', php: 'java', sql: 'sql', mysql: 'sql', postgres: 'sql',
    postgresql: 'sql', sqlite: 'sql', json: 'json', jsonc: 'json', json5: 'json', yaml: 'conf', yml: 'conf',
    toml: 'conf', ini: 'conf', conf: 'conf', dockerfile: 'conf', makefile: 'conf', make: 'conf', css: 'css',
    scss: 'css', less: 'css', html: 'markup', xml: 'markup', svg: 'markup', vue: 'markup', diff: 'diff', patch: 'diff',
  };
  const SLASH = '\\/\\/[^\\n]*|\\/\\*[\\s\\S]*?(?:\\*\\/|$)';
  const HASH = '#[^\\n]*';
  const QUOTED = '"(?:[^"\\\\\\n]|\\\\.)*"|\'(?:[^\'\\\\\\n]|\\\\.)*\'';
  const NUMBER = '\\b(?:0[xX][\\da-fA-F_]+|\\d[\\d_]*(?:\\.\\d+)?(?:[eE][+-]?\\d+)?)\\b';
  const WORD = '[A-Za-z_$][\\w$]*';
  const GRAMMARS = {
    js: [SLASH, QUOTED + '|`(?:[^`\\\\]|\\\\[\\s\\S])*`'],
    py: [HASH, '"""[\\s\\S]*?(?:"""|$)|\'\'\'[\\s\\S]*?(?:\'\'\'|$)|' + QUOTED],
    sh: [HASH, QUOTED],
    go: [SLASH, QUOTED + '|`[^`]*`'],
    rust: [SLASH, '"(?:[^"\\\\]|\\\\.)*"|\'(?:[^\'\\\\\\n]|\\\\.)\''],  // 'a 是生命周期，不是字符串：单引号里只能有一个字
    c: [SLASH, QUOTED],
    java: [SLASH, '"""[\\s\\S]*?(?:"""|$)|' + QUOTED],
    sql: ['--[^\\n]*|\\/\\*[\\s\\S]*?(?:\\*\\/|$)', QUOTED],
    json: [SLASH, '"(?:[^"\\\\\\n]|\\\\.)*"'],
    conf: [HASH, QUOTED],
    css: ['\\/\\*[\\s\\S]*?(?:\\*\\/|$)', QUOTED],
    // 标记语言：不认数字，标签名都算关键字。注释的开头写成 \x3C!--：这个文件是原样填进页面的 <script> 里的，
    // 脚本里一出现 "<" 紧跟 "!--"，浏览器解析 HTML 时会另眼相看
    markup: ['\\x3C!--[\\s\\S]*?(?:-->|$)', QUOTED, '(?!)', '<\\/?[A-Za-z][\\w:-]*'],
  };
  const MAX_HIGHLIGHT = 30000;  // 再长的代码就不着色了：着色要建很多小元素，太长的只会拖慢页面
  const compiled = {};

  function grammarFor(lang) {
    const name = ALIASES[lang.toLowerCase()];
    if (!name || name === 'diff') return name ? { diff: true } : null;
    if (!compiled[name]) {
      const [comments, strings, numbers = NUMBER, words = WORD] = GRAMMARS[name];
      compiled[name] = {
        re: new RegExp('(' + comments + ')|(' + strings + ')|(' + numbers + ')|(' + words + ')', 'g'),
        // 标记语言里匹配到的"词"就是标签名，都着色；别的语言只给关键字着色
        words: name === 'markup' ? null : new Set((KEYWORDS[name] || '').split(' ').filter(Boolean).map((word) => (name === 'sql' ? word.toLowerCase() : word))),
        fold: name === 'sql',
        hash: name === 'sh' || name === 'conf',  // shell 里 # 只有在行首或空白后面才是注释（$# 、a#b 不是）
      };
    }
    return compiled[name];
  }

  // 返回 [[类名, 文字], ...]；类名是 '' 的是普通文字。认不出的语言、太长的代码返回 null
  function highlight(code, lang) {
    if (!lang || code.length > MAX_HIGHLIGHT) return null;
    const grammar = grammarFor(lang);
    if (!grammar) return null;
    if (grammar.diff) {
      return code.split('\n').map((line, index, all) => {
        const kind = /^(\+\+\+|---)/.test(line) ? 'tk-k' : line[0] === '+' ? 'tk-add' : line[0] === '-' ? 'tk-del' : line.startsWith('@@') ? 'tk-k' : '';
        return [kind, line + (index < all.length - 1 ? '\n' : '')];
      });
    }
    const out = [];
    let last = 0, m;
    const re = grammar.re;
    re.lastIndex = 0;
    while ((m = re.exec(code))) {
      let kind;
      if (m[1] !== undefined) {
        if (grammar.hash && m.index > 0 && !WHITESPACE.test(code[m.index - 1])) { re.lastIndex = m.index + 1; continue; }
        kind = 'tk-c';
      } else if (m[2] !== undefined) kind = 'tk-s';
      else if (m[3] !== undefined) kind = 'tk-n';
      else if (!grammar.words || grammar.words.has(grammar.fold ? m[4].toLowerCase() : m[4])) kind = 'tk-k';
      else continue;  // 普通的名字：留在普通文字里
      if (m.index > last) out.push(['', code.slice(last, m.index)]);
      out.push([kind, m[0]]);
      last = m.index + m[0].length;
    }
    if (last < code.length) out.push(['', code.slice(last)]);
    return out;
  }

  // ---- 画出来 ----------------------------------------------------------------
  // 每一块画成一个"条目"：{type, shape, key, el, slot, kids}。el 是这一块最外层的元素；
  // 列表、引用、表格这类容器还有 slot（里面的子块放在哪个元素里）和 kids（子块的条目）。
  // 只用 createElement、createTextNode、setAttribute（几个固定的属性名），不碰任何解析 HTML 的接口。

  function make(doc, tag, className, text) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text) node.appendChild(doc.createTextNode(text));
    return node;
  }

  function link(doc, href) {
    const a = make(doc, 'a');
    a.setAttribute('href', href);
    a.setAttribute('target', '_blank');
    a.setAttribute('rel', 'noopener noreferrer');
    return a;
  }

  // 网址太长时只显示开头（data: 图片的网址可以有几十万个字）
  const brief = (url) => {
    const shown = url.replace(/^https?:\/\//i, '');
    return shown.length > 64 ? shown.slice(0, 63) + '…' : shown;
  };

  function inline(parent, nodes, ctx, inLink) {
    const { doc } = ctx;
    for (const node of nodes) {
      if (node.t === 'text') parent.appendChild(doc.createTextNode(node.v));
      else if (node.t === 'br') parent.appendChild(make(doc, 'br'));
      else if (node.t === 'code') parent.appendChild(make(doc, 'code', null, node.v));
      else if (node.t === 'cite') {
        // 来源的记号由页面建（hooks.cite 返回一个元素，见 page/scripts/citations.js）。链接里不放：链接里不能再套链接
        const mark = !inLink && ctx.hooks.cite ? ctx.hooks.cite(node.v) : null;
        if (mark) parent.appendChild(mark);
      }
      else if (node.t === 'link') {
        if (inLink) { inline(parent, node.c, ctx, true); continue; }  // 链接里的链接：只留文字
        const a = parent.appendChild(link(doc, node.href));
        if (node.title) a.setAttribute('title', node.title);
        inline(a, node.c, ctx, true);
      } else if (node.t === 'img') {
        // 图片不加载，只显示说明文字和网址。网址安全的话整个做成链接，点开在新标签页里看
        const chip = parent.appendChild(node.href && !inLink ? link(doc, node.href) : make(doc, 'span'));
        chip.className = 'md-image';
        chip.setAttribute('title', node.href || brief(node.url));
        const picture = ctx.hooks.icon && ctx.hooks.icon('picture');
        if (picture) chip.appendChild(picture);
        const alt = plain(node.c).trim();
        if (alt) chip.appendChild(make(doc, 'span', 'md-image-alt', alt));
        if (node.url) chip.appendChild(make(doc, 'span', 'md-image-url', brief(node.url)));
      } else {
        const tag = parent.appendChild(make(doc, node.t));
        inline(tag, node.c, ctx, inLink);
      }
    }
  }

  function entry(block, el, slot, kids) {
    return { type: block.type, shape: block.shape, key: block.key, el, slot, kids };
  }

  function buildCode(block, ctx) {
    const { doc, hooks } = ctx;
    const box = make(doc, 'div', 'md-code');
    const bar = box.appendChild(make(doc, 'div', 'md-code-bar'));
    bar.appendChild(make(doc, 'span', 'md-code-lang', block.lang));
    const tools = hooks.codeTools && hooks.codeTools();
    if (tools) bar.appendChild(tools);
    const code = box.appendChild(make(doc, 'pre')).appendChild(make(doc, 'code'));
    const tokens = block.done ? highlight(block.text, block.lang) : null;
    let tail = null;
    if (tokens) {
      for (const [kind, text] of tokens) code.appendChild(kind ? make(doc, 'span', kind, text) : doc.createTextNode(text));
    } else {
      tail = code.appendChild(doc.createTextNode(block.text));  // 还在输出的代码先不着色，来一段接一段
    }
    const made = entry(block, box);
    made.code = { node: tail, text: block.text, lang: block.lang, done: block.done };
    return made;
  }

  function build(block, ctx) {
    const { doc } = ctx;
    switch (block.type) {
      case 'paragraph': {
        const p = make(doc, 'p');
        if (block.task !== undefined) {
          const box = p.appendChild(make(doc, 'input'));
          box.setAttribute('type', 'checkbox');
          box.setAttribute('disabled', '');
          if (block.task) box.setAttribute('checked', '');
        }
        inline(p, parseInline(block.text), ctx, false);
        return entry(block, p);
      }
      case 'heading': {
        const h = make(doc, 'h' + block.level);
        inline(h, parseInline(block.text), ctx, false);
        return entry(block, h);
      }
      case 'hr': return entry(block, make(doc, 'hr'));
      case 'code': return buildCode(block, ctx);
      case 'quote': {
        const quote = make(doc, 'blockquote');
        return entry(block, quote, quote, block.children.map((child) => append(quote, build(child, ctx))));
      }
      case 'list': {
        const list = make(doc, block.ordered ? 'ol' : 'ul', block.loose ? null : 'md-tight');
        if (block.ordered && block.start !== 1) list.setAttribute('start', String(block.start));
        return entry(block, list, list, block.items.map((item) => append(list, build(item, ctx))));
      }
      case 'item': {
        const li = make(doc, 'li', block.task === null ? null : 'md-task');
        return entry(block, li, li, block.children.map((child) => append(li, build(child, ctx))));
      }
      case 'table': {
        const box = make(doc, 'div', 'md-table');
        const table = box.appendChild(make(doc, 'table'));
        const row = table.appendChild(make(doc, 'thead')).appendChild(make(doc, 'tr'));
        block.head.forEach((text, index) => {
          const th = row.appendChild(make(doc, 'th', block.align[index] ? 'md-' + block.align[index] : null));
          th.setAttribute('scope', 'col');
          inline(th, parseInline(text), ctx, false);
        });
        const body = table.appendChild(make(doc, 'tbody'));
        const made = entry(block, box, body, block.rows.map((child) => append(body, build(child, ctx))));
        made.align = block.align;  // 接着写一张长表格时，新的行照这个对齐（见 createView）
        return made;
      }
      case 'row': {
        const tr = make(doc, 'tr');
        block.cells.forEach((text, index) => inline(tr.appendChild(make(doc, 'td', block.align[index] ? 'md-' + block.align[index] : null)), parseInline(text), ctx, false));
        return entry(block, tr);
      }
      default: return entry(block, make(doc, 'div'));
    }
  }

  function append(parent, made) {
    parent.appendChild(made.el);
    return made;
  }

  const childrenOf = (block) => block.children || block.items || block.rows || null;

  // 拿新解析出来的一块去对照已经画好的条目：一样就不动；外形一样就只更新里面变了的子块；
  // 还在输出的代码块只把新来的字接在后面；别的情况整块重画。返回更新后的条目（可能是新的）
  function patch(old, block, ctx) {
    if (old.key === block.key) return old;
    const kids = childrenOf(block);
    if (old.kids && kids && old.type === block.type && old.shape === block.shape) {
      patchKids(old.slot, old.kids, kids, ctx);
      if (block.type === 'list') tighten(old.el, !block.loose);
      old.key = block.key;
      return old;
    }
    const code = old.code;
    if (code && block.type === 'code' && !code.done && !block.done && code.lang === block.lang && block.text.startsWith(code.text)) {
      code.node.appendData(block.text.slice(code.text.length));
      code.text = block.text;
      old.key = block.key;
      return old;
    }
    const made = build(block, ctx);
    old.el.parentNode.replaceChild(made.el, old.el);
    return made;
  }

  // 对照一串子块。from：前面这么多个条目已经定稿，不参与对照，新解析出来的块从它后面开始排
  function patchKids(parent, kids, blocks, ctx, from) {
    const skip = from || 0;
    blocks.forEach((block, index) => {
      const at = skip + index;
      kids[at] = at < kids.length ? patch(kids[at], block, ctx) : append(parent, build(block, ctx));
    });
    while (kids.length > skip + blocks.length) parent.removeChild(kids.pop().el);
  }

  function tighten(list, tight) {
    if (tight) list.classList.add('md-tight'); else list.classList.remove('md-tight');
  }

  // ---- 逐字输出 --------------------------------------------------------------
  //
  // 回复是一小段一小段到的。每次都把整段回复重新解析、重新画一遍太浪费（长回复会越来越卡），所以分两部分：
  //   · 已经"定稿"的块：后面已经开始了一块新内容（而且新内容的第一行已经完整），前面这块就不会再变了。
  //     定稿的块画好之后再也不碰，下次解析从定稿的地方之后开始。
  //   · 最后那一两块还没定稿：每次重新解析，和已经画好的对照，只更新变了的部分（见 patch）。
  // 一段很长的列表、表格也只有最后一项、最后一行在变：只剩它一块没定稿时，除了最后一项，前面的项也先定稿，
  // 下次只从最后一项开始解析（列表从那一项的开头读起本来就是一个列表；表格则告诉解析器"接着读表格的行"）。
  //
  // 最后半行要是只有一个记号（"-"、"1."、"#"……），还看不出它会变成什么，先不画，免得一闪而过。
  // 同样先不画的：写到一半的 ``` 那一行（还不知道语言）、任务列表的方框（"- [x"），还没写完的链接和图片
  // （"[文字" 到 "[文字](网址" 这一段，不然会先露出方括号，网址再一个字一个字地冒出来）。
  // 以 | 开头、上面一行不是表格的那一行，可能是表头：等分隔行到了再画，免得先露出一排竖线。

  const MARKER_ONLY = /^[ \t>]*(?:[-+*#=_~`]{1,8}|\d{1,9}[.)]?|(?:[-+*]|\d{1,9}[.)])[ \t]+\[[ xX]?\]?)?[ \t]*$/;
  const DELIMITER_PART = /^[ \t>]*[-:|][-:| \t]*$/;
  const ROW_START = /^[ \t>]*\|/;
  const FENCE_PART = /^[ \t>]*(?:`{3,}|~{3,})/;
  const LINK_PART = /!?\[[^\]\n]*(?:\](?:\([^)\s]*)?)?$/;

  // 第 end 个字所在的那一行从哪儿开始
  const lineAt = (text, end) => text.lastIndexOf('\n', end - 1) + 1;

  // continuing：这段文字是接着一张已经画出来的表格写的，第一行是表格的内容行，不是表头
  function holdBack(text, continuing) {
    let cut = text.length;
    if (!text.endsWith('\n')) {
      const start = lineAt(text, cut), line = text.slice(start);
      const link = LINK_PART.exec(line);
      if (MARKER_ONLY.test(line) || FENCE_PART.test(line)) cut = start;
      else if (link) cut = start + link.index;
    }
    // 剩下的最后一行（写完没写完都算），或者最后一行是写到一半的分隔行时它上面那一行，可能是表头
    const complete = cut === 0 || text[cut - 1] === '\n';
    const end = complete ? cut - 1 : cut;
    if (end <= 0) return text.slice(0, cut);
    let start = lineAt(text, end);
    if (!complete && start > 0 && DELIMITER_PART.test(text.slice(start, end))) start = lineAt(text, start - 1);
    const above = start > 0 ? text.slice(lineAt(text, start - 1), start - 1) : '';
    if (ROW_START.test(text.slice(start, end)) && !above.includes('|') && !(continuing && start === 0)) cut = start;
    return text.slice(0, cut);
  }

  // align：接着读一张表格的内容行（每一列的对齐方式）。读到的行放在最前面的一块 {type: 'rows'} 里
  function parseDocument(text, final, align) {
    const lines = text.split('\n');
    const starts = [];
    let at = 0;
    lines.forEach((line, index) => {
      starts.push(at);
      at += line.length + 1;
      if (line.endsWith('\r')) lines[index] = line.slice(0, -1);
    });
    if (!align) return { blocks: parseBlocks(lines, final, 0), starts, count: lines.length };
    const { rows, i } = parseRows(lines, 0, align);
    const blocks = parseBlocks(lines, final, i);
    blocks.unshift({ type: 'rows', rows, line: 0 });
    return { blocks, starts, count: lines.length };
  }

  // 正在输出时，末尾跟着一个呼吸的小光点。它要紧跟在最后一个字后面，所以找到最深处的最后一块
  // （段落、标题、单元格、代码），光点画在它里面。还什么都没有时画在整段的开头
  const LEAVES = /^(P|H[1-6]|TD|TH|CODE)$/;
  const CONTAINERS = /^(UL|OL|LI|BLOCKQUOTE|DIV|TABLE|TBODY|THEAD|TR|PRE)$/;
  function lastLeaf(root) {
    for (let node = root; ;) {
      const last = node.lastElementChild;
      if (!last) return node.tagName === 'TBODY' ? null : node;  // 空的列表项也放得下光点；只有表头的表格不放
      if (LEAVES.test(last.tagName)) return last;
      if (!CONTAINERS.test(last.tagName)) return null;  // 分隔线
      node = last;
    }
  }

  // root 是放这段回复的元素；hooks 是页面提供的几样东西：icon(名字) 返回一个图标，codeTools() 返回放在代码块
  // 右上角的按钮（"复制"），cite(编号) 返回来源的记号（没有这一项的话，记号什么都不画）。
  // 返回的对象只有一个方法：update(全文, 是否已经完整)。
  function createView(root, hooks) {
    const ctx = { doc: root.ownerDocument, hooks: hooks || {} };
    let done = 0;      // 原文的前多少个字已经定稿
    let tail = [];     // 还没定稿的那几块的条目，按顺序排在 root 的最后
    let open = null;   // 正在写的一个长列表或长表格：{entry, frozen}，它的前 frozen 项已经定稿
    let caret = null;
    return {
      update(source, final) {
        const table = open && open.entry.type === 'table' ? open.entry : null;
        const text = final ? source.slice(done) : holdBack(source.slice(done), Boolean(table));
        const { blocks, starts, count } = parseDocument(text, final, table && table.align);
        let rest = blocks;
        if (open) {  // 最前面一块是那个长列表（长表格）接着写的部分
          const more = blocks[0];
          if (more && more.type === (table ? 'rows' : 'list')) {
            patchKids(open.entry.slot, open.entry.kids, table ? more.rows : more.items, ctx, open.frozen);
            if (more.loose) tighten(open.entry.el, false);
            rest = blocks.slice(1);
          } else {
            open = null;  // 接不上了（不会发生，以防万一）：那个列表就当它写完了
          }
        }
        patchKids(root, tail, rest, ctx);

        if (final) {
          done = source.length;
          tail = [];
          open = null;
        } else {
          let cut = -1;  // 这一次定稿到哪一行（这段文字里的行号）
          let settled = 0;
          for (let i = blocks.length - 1; i > 0; i -= 1) {
            if (blocks[i].line < count - 1) { settled = i; break; }  // 第 i 块的第一行已经完整：它前面的都定稿了
          }
          if (settled) {
            cut = blocks[settled].line;
            tail = tail.slice(open ? settled - 1 : settled);
            open = null;
          }
          const left = blocks.slice(settled);
          const parts = left.length === 1 ? left[0].items || left[0].rows : null;
          const last = parts && parts[parts.length - 1];
          if (last && parts.length > 1 && last.line < count - 1) {
            const entry = open ? open.entry : tail[0];
            open = { entry, frozen: entry.kids.length - 1 };
            tail = [];
            cut = last.line;
          }
          if (cut >= 0) done += starts[cut];
        }

        if (caret) caret.classList.remove('md-caret');
        caret = final ? null : lastLeaf(root);
        if (caret) caret.classList.add('md-caret');
      },
    };
  }

  const Markdown = { parse: (text) => parseDocument(text, true).blocks, parseInline, highlight, safeUrl, createView };
  if (typeof module === 'object' && module && typeof module.exports === 'object') module.exports = Markdown;
  else scope.Markdown = Object.freeze(Markdown);
})(this);
