  // ---- 小工具 --------------------------------------------------------------

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

  function firstLine(text) {
    const line = text.trim().split('\n')[0] || '';
    return line.length > 120 ? line.slice(0, 119) + '…' : line;
  }
