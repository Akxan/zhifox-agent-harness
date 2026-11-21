// ---- 逐字输出的文字 ------------------------------------------------------
// 模型一个字一个字吐出来的思考和回复：开一段（stream）、接着往里写、写完收尾（endBlock）。
// 一段回复"这一小段写完了"（block_end）时先停一下（pauseBlock），紧接着又来文字的话还是同一段（见 pauseBlock 的说明）。
// 还有回复和代码块上的"复制"（copy）。回复引用的网页（记号、末尾的来源列表）在 citations.js 里。

async function copy(text, button) {
  // 过一会儿换回原来的说明（回复上是"复制"，代码块上是"复制代码"）。第一次点的时候记下来，连点两下也不会记错
  const label = button.dataset.copyLabel || (button.dataset.copyLabel = button.dataset.i18nTitle || k('复制'));
  try {
    await navigator.clipboard.writeText(text);
    button.replaceChildren(icon('check'));
    tattr(button, 'title', k('已复制'));
    setTimeout(() => { button.replaceChildren(icon('copy')); tattr(button, 'title', label); }, 1400);
  } catch (_) {
    toast(t('复制失败：浏览器没有允许访问剪贴板。'));
  }
}

// "正在输出"的样子：思考过程展开着、标着"正在思考"，回复的末尾有一个呼吸的光点。
// 只有此刻实时发生的输出才这样显示。
function setStreaming(on) {
  if (!block) return;
  block.box.classList.toggle('streaming', on);
  if (block.kind === 'thinking') {
    block.box.open = on;  // 思考完就折叠起来，想看可以再点开
    retext(block.label, on ? k('正在思考') : k('思考过程'));
  }
}

function stream(kind, text) {
  if (block && block.paused) {
    if (kind === 'text') resumeBlock(); else endBlock();
  }
  if (!block || block.kind !== kind) {
    endBlock();
    let box, holder, label = null;
    if (kind === 'thinking') {
      box = el('details', 'thinking');
      const summary = box.appendChild(el('summary'));
      summary.appendChild(icon('spark', 'spark'));
      label = summary.appendChild(tel('span', null, k('思考过程')));
      summary.appendChild(icon('chevron', 'chev'));
      holder = box.appendChild(el('div', 'thinking-body'));
    } else {
      box = el('div', 'msg assistant');
      holder = box.appendChild(el('div', 'msg-text md'));
    }
    // 思考过程是纯文字：整段放在同一个文本节点里，来一小段就接在后面。
    // 回复按 Markdown 画：先把字攒在 text 里，一帧最多画一次（见 markdown-view.js 的 drawSoon）
    // marks、sources：这段回复引用的网页（见 citations.js）；paused：停在一个 block_end 上（见 pauseBlock）
    block = { kind, box, holder, label, text: '', node: null, view: null, queued: false, marks: [], sources: new Map(), paused: false };
    if (kind === 'thinking') block.node = holder.appendChild(document.createTextNode(''));
    else {
      const record = block;
      record.view = markdownView(holder, record);
      replies.set(box, record);
      // 复制的是 Markdown 原文：贴到别处（编辑器、另一个聊天窗口）还能保留格式。引用了网页的，带上记号和来源（见 citeCopyText）
      const button = box.appendChild(el('div', 'msg-actions')).appendChild(iconButton('copy', k('复制')));
      button.addEventListener('click', () => copy(citeCopyText(record), button));
    }
    if (isLive()) setStreaming(true);
    add(box);
  }
  block.text += text;
  if (block.view) drawSoon(block); else block.node.appendData(text);
  follow();
}

function endBlock() {
  if (!block) return;
  setStreaming(false);
  if (block.kind === 'text') {
    if (block.text.trim()) { draw(block, true); showCiteList(block); announce(block.holder.textContent.trim()); }
    else block.box.remove();  // 模型有时只输出几个换行，不值得占一块地方
  }
  block = null;
}

// "这一小段写完了"（block_end）。思考过程到这里就结束了；回复先不收尾，只是不再显示"正在输出"：
// 紧接着要是又来一段回复、中间没有别的事件，它们是同一段。以前存下的联网搜索回答就是这样：API 按引用把一句话切成
// 好几个文字块，那时每块后面都跟着一个 block_end（现在 harness/providers/claude.py 的 _show 不再这样发），
// 重放时一块一块画成单独的段落，". Anuncia"、"," 各占一行，每块一个"复制"。
// 真正收尾在下一个别的事件到来的时候：工具调用、提示、确认、你的下一条消息、这一轮结束……的处理都会先调用 endBlock；
// 思考接在后面的话，stream 也会先收尾。状态（state）不是对话里的内容，不打断这一段。实时和重放走的是同一条路，画出来一样。
// 停下的时候还不按"写完了"的样子画：markdown.js 画过完整的一次以后就不再接着往下画了，接着来的字会变成另一段
function pauseBlock() {
  if (!block) return;
  if (block.kind !== 'text') { endBlock(); return; }
  setStreaming(false);
  block.paused = true;
}

function resumeBlock() {
  block.paused = false;
  if (isLive()) setStreaming(true);
}
