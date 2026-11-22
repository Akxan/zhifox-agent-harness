// ---- 小工具 --------------------------------------------------------------
// 建元素（el）、画图标（icon、iconButton）、在浏览器里记一点小东西（remember、recall）、取一段话的第一行（firstLine）。

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

const SVG = 'http://www.w3.org/2000/svg';

function icon(name, className) {
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('class', 'icon' + (className ? ' ' + className : ''));
  svg.setAttribute('aria-hidden', 'true');
  svg.appendChild(document.createElementNS(SVG, 'use')).setAttribute('href', '#i-' + name);
  return svg;
}

function iconButton(name, label) {
  const button = el('button', 'icon-button small');
  button.type = 'button';
  tattr(button, 'title', label);
  tattr(button, 'aria-label', label);
  button.appendChild(icon(name));
  return button;
}

function remember(key, value) {
  try { localStorage.setItem(key, value); } catch (_) { /* 浏览器禁用了本地存储 */ }
}

function recall(key) {
  try { return localStorage.getItem(key); } catch (_) { return null; }
}

// 两段文字除了换行的写法以外一不一样。文本框（textarea）读出来的换行永远是 \n，可文件里可能是 \r\n
// （Windows 上用记事本写的记忆、提示词、SKILL.md）：直接比的话，打开编辑器什么都没改也算"改过了"，Esc 关不掉
const sameText = (a, b) => String(a ?? '').replace(/\r\n?/g, '\n') === String(b ?? '').replace(/\r\n?/g, '\n');

function firstLine(text) {
  const line = text.trim().split('\n')[0] || '';
  return line.length > 120 ? line.slice(0, 119) + '…' : line;
}
