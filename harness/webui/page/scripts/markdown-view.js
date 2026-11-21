// ---- 回复里的 Markdown ----------------------------------------------------
// 回复里的标题、列表、表格、代码块怎么解析、怎么建成元素，都在 markdown.js 里（服务器发页面时把它填在这个脚本
// 前面，它在 window.Markdown 上留下一个 createView）。这里只接上页面自己的几样东西：
//   · 图标和代码块右上角的"复制"按钮（按钮要用页面的 t()，换语言时跟着换）；
//   · 一帧最多画一次：模型一秒能吐几十小段，每段都重画一遍是白费力气。屏幕一帧只刷新一次，攒到下一帧一起画；
//   · 万一渲染出错（不该发生），退回到纯文字，回复不能因此看不见。
// 安全上的规矩不变：渲染器只用 createElement 和文字节点建元素，从不把模型的输出当作 HTML 解析。

const replies = new WeakMap();  // 回复的元素 → 它的记录（里面有 Markdown 原文）。搜索跳转时要对照原文

const markdownHooks = {
  icon,
  codeTools() {
    const button = iconButton('copy', k('复制代码'));
    button.classList.add('md-copy');
    return button;
  },
};

// record：这是一段回复（streaming.js 的 block）的话，它引用网页的记号也画出来（见 citations.js）
function markdownView(holder, record) {
  const hooks = record ? { ...markdownHooks, cite: (index) => citeMark(record, index) } : markdownHooks;
  try { return window.Markdown.createView(holder, hooks); } catch (problem) { console.error(problem); return null; }
}

// 按现在攒到的文字画一次（连同来源的记号）。final：这段回复已经完整了，没写完的代码块也按写完了画
function draw(record, final) {
  if (record.view) {
    try { record.view.update(citedSource(record), final); return; } catch (problem) {
      console.error(problem);
      record.view = null;
    }
  }
  record.holder.classList.add('md-plain');  // 纯文字的样子：保留换行
  record.holder.textContent = record.text;
}

function drawSoon(record) {
  if (record.queued) return;
  record.queued = true;
  requestAnimationFrame(() => {
    record.queued = false;
    if (block !== record) return;  // 这一段已经结束了，endBlock 里画过完整的样子
    draw(record, false);
    if (stick) chat.scrollTop = chat.scrollHeight;  // 刚画上去的内容可能让页面变高了：跟着最新内容的话，留在底部
  });
}

// 代码块的"复制"：所有代码块共用一个监听，复制的是代码原文（不含语言名和按钮）
log.addEventListener('click', (event) => {
  const button = event.target.closest ? event.target.closest('.md-copy') : null;
  const code = button && button.closest('.md-code') && button.closest('.md-code').querySelector('pre code');
  if (code) copy(code.textContent, button);
});
