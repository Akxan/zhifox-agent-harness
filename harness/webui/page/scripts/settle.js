// ---- 空白页和收尾 --------------------------------------------------------

function greeting() {
  const hour = new Date().getHours();
  return hour < 5 ? k('夜深了，') : hour < 11 ? k('早上好，') : hour < 13 ? k('中午好，') : hour < 18 ? k('下午好，') : hour < 23 ? k('晚上好，') : k('夜深了，');
}

// 对话里还没有实质内容时，页面是"空白页"的样子：问候语和输入框摆在中间。
// 有了内容，输入框就滑到底部（只量一次位置，然后用 transform 滑过去）。
function setFresh(fresh) {
  if (main.classList.contains('fresh') === fresh) return;
  const apply = () => {
    if (fresh) retext($('greeting'), greeting());
    main.classList.toggle('fresh', fresh);
    empty.hidden = starters.hidden = !fresh;
  };
  if (!body.classList.contains('ready')) { apply(); return; }  // 页面还没亮相，不需要过渡
  const before = composer.getBoundingClientRect().top;
  apply();
  const shift = before - composer.getBoundingClientRect().top;
  if (Math.abs(shift) > 1) play(composer, [{ transform: 'translateY(' + shift + 'px)' }, { transform: 'none' }], 480);
  if (!fresh) play(viewport, [{ opacity: 0 }, { opacity: 1 }], 280);
}

const hasContent = () => Boolean(log.querySelector(':scope > :not(.notice)'));

// 一批旧内容画完之后的收尾：决定是不是空白页，滚到最新的位置，整段淡入一次。
function settle() {
  unsettled = false;
  setFresh(!hasContent());
  setStick(true);
  chat.scrollTop = chat.scrollHeight;
  if (body.classList.contains('ready') && log.childElementCount) {
    play(log, [{ opacity: 0, transform: 'translateY(8px)' }, { opacity: 1, transform: 'none' }], 340);
  }
  body.classList.add('ready');
  if (jump) {  // 这段会话是从搜索结果里点开的：滚到命中的那个位置
    const target = jump;
    jump = null;
    if (state && state.conversation === target.id) reveal(target);
  }
}

// 在对话里找到搜索命中的那一块，滚到屏幕中间，背后亮一下。
// 服务器告诉了我们命中的原文（find）和它属于哪一类内容（kind），这里按文字去对。
// 附件的文件名也算"你说的话"（搜索按文件名找到的，要能跳到那一排附件上）
const FOUND_IN = { user: '.msg.user, :scope > .msg-atts', assistant: '.msg.assistant', thinking: '.thinking', tool: '.call, :scope > .notice.tool', result: '.call' };
const squash = (text) => text.normalize('NFKC').toLowerCase().replace(/\s+/g, '');  // 比较时不管大小写、全角半角和空白
// 回复是按 Markdown 画出来的：服务器给的是原文，里面的 **、`、# 这些记号在页面上看不到。
// 原样对不上时，两边都去掉这些记号再比
const MARKUP = /[*_~`#>|\\]/g;
const bare = (text) => squash(text).replace(MARKUP, '');
const literal = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');  // 在正则表达式里按原样匹配

// 在一块内容里找到命中的那几个字，返回它们的位置（一个 Range）；找不到返回 null。
// 服务器给的原文里，连续的空白被压成了一个空格，页面上一行工具调用又是几段文字拼起来的，
// 所以词和词之间按"有没有空白都行"去对。
function locate(node, find) {
  const escaped = find.split(/\s+/).filter(Boolean).map(literal);
  if (!escaped.length) return null;
  const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
  const pieces = [];
  let text = '';
  for (let piece = walker.nextNode(); piece; piece = walker.nextNode()) {
    pieces.push([piece, text.length]);
    text += piece.data;
  }
  let match = new RegExp(escaped.join('\\s*')).exec(text);
  if (!match) {  // 命中的原文里夹着 Markdown 的记号：去掉记号，字和字之间允许隔着空白或记号
    const chars = [...find.replace(MARKUP, '').replace(/\s+/g, '')].map(literal);
    if (chars.length) match = new RegExp(chars.join('[\\s*_~`#>|\\\\]*')).exec(text);
  }
  if (!match) return null;
  const at = (index) => {  // 整段文字里的第 index 个字，落在哪个文本节点的第几个字
    let which = pieces.length - 1;
    while (which > 0 && pieces[which][1] > index) which -= 1;
    return [pieces[which][0], index - pieces[which][1]];
  };
  const range = document.createRange();
  range.setStart(...at(match.index));
  range.setEnd(...at(match.index + match[0].length));
  return range;
}

let highlightTimer = 0;

function reveal(target) {
  if (target.media) { revealAttachment(target.media); return; }
  const find = (target.find || '').trim();
  const wanted = squash(find), loose = bare(find);
  if (!wanted || !FOUND_IN[target.kind]) return;
  for (const node of log.querySelectorAll(':scope > ' + FOUND_IN[target.kind])) {
    // 回复还可以按原文对：命中的可能是页面上不显示的部分（比如链接的网址），那就只滚到这条回复
    const record = replies.get(node), shown = node.textContent;
    if (!squash(shown).includes(wanted) && !(record && ((loose && bare(shown).includes(loose)) || squash(record.text).includes(wanted)))) continue;
    if (node.tagName === 'DETAILS') node.open = true;  // 命中在折叠起来的内容里（思考过程、工具结果）：展开它
    setStick(false);  // 要停在命中的地方，不能被"跟着最新内容"拉回底部
    const range = locate(node, find);
    if (range) {
      // 工具结果放在一个自己会滚动的小框里：先把小框滚到命中的那一行
      const inner = range.startContainer.parentElement.closest('pre');
      if (inner && inner.scrollHeight > inner.clientHeight) {
        inner.scrollTop += range.getBoundingClientRect().top - inner.getBoundingClientRect().top - inner.clientHeight / 3;
      }
      // 回复里的代码块和表格太宽时在自己的框里横向滚动：也滚到命中的那几个字
      const wide = range.startContainer.parentElement.closest('.md-code pre, .md-table');
      if (wide && wide.scrollWidth > wide.clientWidth) {
        wide.scrollLeft += range.getBoundingClientRect().left - wide.getBoundingClientRect().left - wide.clientWidth / 3;
      }
    }
    // 再滚动对话区，把命中的地方放在靠上三分之一处。一条回复可能比屏幕还长，所以对的是那几个字，不是整块
    const spot = (range || node).getBoundingClientRect(), view = chat.getBoundingClientRect();
    chat.scrollTop += spot.top - view.top - view.height / 3;
    // 这段会话可能不满一屏，或者命中就在最后：那就还是"看着最新内容"，不用亮出"回到最新"的按钮
    setStick(chat.scrollHeight - chat.scrollTop - chat.clientHeight < 4);
    // 把那几个字标出来。用的是浏览器的"自定义高亮"：只登记一个范围，不往页面里加标签
    // 这次没有能标的字（比如命中的是回复里链接的网址，页面上看不到）：上一次标的那几个字也要收掉，不能留着误导人
    if (window.Highlight && window.CSS && CSS.highlights) {
      clearTimeout(highlightTimer);
      if (range) {
        CSS.highlights.set('found', new Highlight(range));
        highlightTimer = setTimeout(() => CSS.highlights.delete('found'), 8000);
      } else CSS.highlights.delete('found');
    }
    node.classList.remove('found');
    void node.offsetWidth;  // 让浏览器先认下"没有这个类"，再加回去，动画才会重新播
    node.classList.add('found');
    setTimeout(() => node.classList.remove('found'), 2300);
    return;
  }
}

// 多媒体库的"定位到会话"：数到这段会话里的第 turn 排附件卡片、第 index 张，滚到屏幕靠上三分之一处，让它亮一下。
// 焦点也放到它上面，用键盘的人接着按回车就能再点开它
function revealAttachment([turn, index]) {
  const row = log.querySelectorAll(':scope > .msg-atts')[turn];
  if (!row) { toast(t('在这段会话里没有找到这个附件。')); return; }
  const card = row.children[index] || row;
  setStick(false);
  const spot = card.getBoundingClientRect(), view = chat.getBoundingClientRect();
  chat.scrollTop += spot.top - view.top - view.height / 3;
  setStick(chat.scrollHeight - chat.scrollTop - chat.clientHeight < 4);
  const button = card.querySelector('button');
  if (button) button.focus({ preventScroll: true });
  card.classList.remove('found');
  void card.offsetWidth;  // 让浏览器先认下"没有这个类"，再加回去，动画才会重新播
  card.classList.add('found');
  setTimeout(() => card.classList.remove('found'), 2300);
}

function add(node) {
  if (!node.classList.contains('notice')) setFresh(false);
  if (isLive()) node.classList.add('enter');
  log.appendChild(node);
  follow();
  return node;
}
