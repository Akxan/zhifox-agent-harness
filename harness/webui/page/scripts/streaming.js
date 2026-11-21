// ---- 逐字输出的文字 ------------------------------------------------------

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
    // 回复按 Markdown 画：先把字攒在 text 里，一帧最多画一次（见"回复里的 Markdown"一节的 drawSoon）
    block = { kind, box, holder, label, text: '', node: null, view: null, queued: false };
    if (kind === 'thinking') block.node = holder.appendChild(document.createTextNode(''));
    else {
      const record = block;
      record.view = markdownView(holder);
      replies.set(box, record);
      // 复制的是 Markdown 原文：贴到别处（编辑器、另一个聊天窗口）还能保留格式
      const button = box.appendChild(el('div', 'msg-actions')).appendChild(iconButton('copy', k('复制')));
      button.addEventListener('click', () => copy(record.text.trim(), button));
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
    if (block.text.trim()) { draw(block, true); announce(block.holder.textContent.trim()); }
    else block.box.remove();  // 模型有时只输出几个换行，不值得占一块地方
  }
  block = null;
}
