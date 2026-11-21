// 智狐放进每个网页里的一小段脚本：把页面整理成模型读得懂的一行一行，给每个能操作的元素一个编号（ref_12），
// 之后模型说"点 ref_12"，就靠这里记下的编号找回那个元素。
//
// 它运行在一个"隔离世界"（isolated world）里，见 session.py：和网页自己的脚本共用同一个页面（DOM），
// 但各有各的全局变量。所以网页看不到这里记下的编号，也改不了这里的函数；网页改写了 Element.prototype 之类的东西，
// 这里用的仍然是浏览器原本的那一套。
//
// 只读"页面上显示出来的东西"：display:none、visibility:hidden、aria-hidden 的内容都跳过。
// 网页可以在看不见的地方藏一段写给模型的话（"忽略之前的指示……"），不显示的东西就不交给模型。
//
// 这里只返回数据，不做任何判断（要不要问用户由 Python 那边的 permissions.py 决定）。
(() => {
  if (globalThis.__zhifox) return 'ready';

  const refs = new Map();      // 'ref_12' → 元素（WeakRef：页面删掉了元素，这里不拦着它被回收）
  const ids = new WeakMap();   // 元素 → 'ref_12'：同一个元素第二次读到时用原来的编号
  let next = 1;                // 下一个编号。从 Python 那边接着数：页面跳转以后旧编号不会被新元素冒用

  const INTERACTIVE = new Set(['link', 'button', 'textbox', 'searchbox', 'checkbox', 'radio', 'combobox', 'listbox',
    'option', 'slider', 'spinbutton', 'switch', 'tab', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'treeitem']);
  const STRUCTURE = new Set(['heading', 'img', 'navigation', 'main', 'form', 'dialog', 'alertdialog', 'banner',
    'contentinfo', 'article', 'search', 'alert', 'table', 'iframe', 'region', 'progressbar']);
  // 名字取自里面的文字的那些：读出名字以后就不再往里走，免得同一段文字出现两遍
  const NAME_FROM_CONTENT = new Set(['link', 'button', 'heading', 'option', 'tab', 'menuitem', 'menuitemcheckbox',
    'menuitemradio', 'treeitem', 'switch', 'checkbox', 'radio']);
  const TEXT_INPUTS = new Set(['text', 'search', 'email', 'url', 'tel', 'password', 'number']);
  const CARD = /(card|cc)[-_ ]?(num|no\b|number)|cardnumber|credit|debit|cvv|cvc|csc|security[-_ ]?code|exp(iry|iration)|卡号|信用卡|银行卡|安全码|有效期|tarjeta|caducidad/i;

  const squash = (text, limit) => {
    const one = String(text || '').replace(/\s+/g, ' ').trim();
    return one.length > limit ? one.slice(0, limit - 1) + '…' : one;
  };
  const quote = (text) => '"' + text.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';

  function refOf(element) {
    let ref = ids.get(element);
    if (!ref) {
      ref = 'ref_' + next++;
      ids.set(element, ref);
      refs.set(ref, new WeakRef(element));
    }
    return ref;
  }

  function lookup(ref) {
    const held = refs.get(String(ref));
    const element = held && held.deref();
    return element && element.isConnected ? element : null;
  }

  // ---- 元素是什么 -----------------------------------------------------------

  function role(element) {
    const explicit = (element.getAttribute('role') || '').trim().split(/\s+/)[0];
    if (explicit && explicit !== 'presentation' && explicit !== 'none' && explicit !== 'generic') return explicit;
    const tag = element.localName;
    if (tag === 'a' || tag === 'area') return element.hasAttribute('href') ? 'link' : '';
    if (tag === 'button' || tag === 'summary') return 'button';
    if (tag === 'select') return element.multiple || element.size > 1 ? 'listbox' : 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'option') return 'option';
    if (tag === 'img') return element.getAttribute('alt') ? 'img' : '';
    if (/^h[1-6]$/.test(tag)) return 'heading';
    if (tag === 'input') {
      const type = (element.getAttribute('type') || 'text').toLowerCase();
      if (type === 'hidden') return '';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'range') return 'slider';
      if (type === 'number') return 'spinbutton';
      if (type === 'search') return 'searchbox';
      if (['button', 'submit', 'reset', 'image'].includes(type)) return 'button';
      if (type === 'file') return 'button';
      return 'textbox';
    }
    if (element.isContentEditable && element.contentEditable !== 'inherit') return 'textbox';
    return { nav: 'navigation', main: 'main', form: 'form', dialog: 'dialog', header: 'banner', footer: 'contentinfo',
      article: 'article', table: 'table', iframe: 'iframe', frame: 'iframe', search: 'search', progress: 'progressbar',
      section: element.getAttribute('aria-label') || element.getAttribute('aria-labelledby') ? 'region' : '' }[tag] || '';
  }

  function labelText(element) {
    const labels = element.labels ? [...element.labels] : [];
    return labels.map((label) => label.innerText || label.textContent).join(' ');
  }

  function name(element, kind) {
    const by = element.getAttribute('aria-labelledby');
    if (by) {
      const text = by.split(/\s+/).map((id) => element.ownerDocument.getElementById(id)).filter(Boolean)
        .map((node) => node.innerText || node.textContent).join(' ');
      if (text.trim()) return squash(text, 120);
    }
    const aria = element.getAttribute('aria-label');
    if (aria && aria.trim()) return squash(aria, 120);
    const tag = element.localName;
    if (tag === 'input' || tag === 'select' || tag === 'textarea') {
      const label = labelText(element);
      if (label.trim()) return squash(label, 120);
      const type = (element.getAttribute('type') || '').toLowerCase();
      if (['button', 'submit', 'reset'].includes(type)) return squash(element.value || type, 120);
      if (type === 'image') return squash(element.getAttribute('alt') || element.value || 'submit', 120);
      return squash(element.getAttribute('placeholder') || element.getAttribute('title') || element.getAttribute('name') || '', 120);
    }
    if (tag === 'img' || tag === 'area') return squash(element.getAttribute('alt') || '', 120);
    if (tag === 'iframe' || tag === 'frame') return squash(element.getAttribute('title') || element.getAttribute('name') || '', 120);
    if (NAME_FROM_CONTENT.has(kind)) {
      const text = element.innerText || element.textContent || '';
      if (text.trim()) return squash(text, 120);
      const image = element.querySelector('img[alt]');
      if (image) return squash(image.getAttribute('alt'), 120);
    }
    return squash(element.getAttribute('title') || '', 120);
  }

  function inputType(element) {
    return element.localName === 'input' ? (element.getAttribute('type') || 'text').toLowerCase() : '';
  }

  function isPassword(element) {
    const auto = (element.getAttribute('autocomplete') || '').toLowerCase();
    return inputType(element) === 'password' || auto.includes('current-password') || auto.includes('new-password');
  }

  function isCard(element) {
    const auto = (element.getAttribute('autocomplete') || '').toLowerCase();
    if (/\bcc-/.test(auto)) return true;
    const words = [element.getAttribute('name'), element.id, element.getAttribute('placeholder'),
      element.getAttribute('aria-label'), element.labels ? labelText(element) : ''].join(' ');
    return CARD.test(words);
  }

  function editable(element) {
    const tag = element.localName;
    if (tag === 'textarea') return !element.disabled && !element.readOnly;
    if (tag === 'input') {
      const type = inputType(element);
      const typed = TEXT_INPUTS.has(type) || ['date', 'time', 'datetime-local', 'month', 'week'].includes(type);
      return typed && !element.disabled && !element.readOnly;
    }
    if (element.isContentEditable) return true;
    return (element.getAttribute('role') || '') === 'textbox';
  }

  // 点它会不会"提交"：表单里的提交按钮（<button> 默认就是提交按钮）、<input type=submit|image>
  function control(element) {
    return element.closest('button, input, a[href], [role="button"], [role="link"], select, textarea, label, summary') || element;
  }

  function submits(element) {
    const target = control(element);
    const tag = target.localName;
    if (tag === 'button') return (target.getAttribute('type') || 'submit').toLowerCase() === 'submit' && Boolean(target.form);
    if (tag === 'input') return ['submit', 'image'].includes(inputType(target)) && Boolean(target.form);
    return false;
  }

  function visible(element) {
    if (element.closest('[aria-hidden="true"]')) return false;
    if (element.checkVisibility) return element.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
    const style = getComputedStyle(element);
    return style.display !== 'none' && style.visibility !== 'hidden' && element.getClientRects().length > 0;
  }

  // 这个元素在不在当前看得到的那一屏里（frame 里的元素按它自己那个窗口算）
  function inView(element) {
    const box = element.getBoundingClientRect();
    const view = element.ownerDocument.defaultView;
    return box.width > 0 && box.height > 0 && box.bottom > 0 && box.right > 0 && box.top < view.innerHeight && box.left < view.innerWidth;
  }

  function frameDocument(element) {
    try { return element.contentDocument; } catch (_) { return null; }  // 另一个网站的 frame：看不到里面
  }

  function details(element, kind) {
    const parts = [];
    if (kind === 'heading') parts.push('level=' + (Number(element.localName.slice(1)) || element.getAttribute('aria-level') || 2));
    if (['textbox', 'searchbox', 'combobox', 'spinbutton', 'slider'].includes(kind)) {
      if (isPassword(element)) parts.push('(密码框)');
      else if (isCard(element)) parts.push('(银行卡信息)');
      else {
        const value = element.localName === 'select' ? [...element.selectedOptions].map((option) => option.text).join(', ')
          : element.isContentEditable ? element.innerText : element.value;
        if (value) parts.push('value=' + quote(squash(value, 80)));
      }
    }
    if (['checkbox', 'radio', 'switch'].includes(kind)) parts.push(element.checked || element.getAttribute('aria-checked') === 'true' ? 'checked' : 'unchecked');
    if (element.disabled || element.getAttribute('aria-disabled') === 'true') parts.push('disabled');
    const expanded = element.getAttribute('aria-expanded');
    if (expanded) parts.push(expanded === 'true' ? 'expanded' : 'collapsed');
    if (kind === 'link') {
      const href = element.getAttribute('href') || '';
      try {
        const address = new URL(href, element.baseURI);
        parts.push('→ ' + squash(address.origin === location.origin ? address.pathname + address.search : address.href, 80));
      } catch (_) { /* 写坏了的地址：不写 */ }
    }
    if (kind === 'iframe' && !frameDocument(element)) parts.push('(另一个网站的内容，看不到里面：用截图和坐标)');
    if (deepActive() === element) parts.push('focused');
    return parts.join(' ');
  }

  function deepActive() {
    let active = document.activeElement;
    for (let guard = 0; active && guard < 20; guard += 1) {
      if (active.shadowRoot && active.shadowRoot.activeElement) active = active.shadowRoot.activeElement;
      else if ((active.localName === 'iframe' || active.localName === 'frame') && frameDocument(active)) active = frameDocument(active).activeElement;
      else break;
    }
    return active;
  }

  // ---- 读页面 ---------------------------------------------------------------

  function children(node) {
    if (node.shadowRoot) return [...node.shadowRoot.childNodes];
    if (node.localName === 'slot') return node.assignedNodes({ flatten: true });
    if (node.localName === 'iframe' || node.localName === 'frame') {
      const inner = frameDocument(node);
      return inner && inner.documentElement ? [inner.documentElement] : [];
    }
    return [...node.childNodes];
  }

  function line(element, kind, depth) {
    const label = name(element, kind);
    const extra = details(element, kind);
    return '  '.repeat(depth) + kind + (label ? ' ' + quote(label) : '') + ' [' + refOf(element) + ']' + (extra ? ' ' + extra : '');
  }

  function read(options) {
    next = Math.max(next, Number(options.start) || 1);
    const filter = options.filter || 'visible';
    const maxDepth = Math.max(1, Number(options.depth) || 15);
    const limit = Number(options.limit) || 50000;
    let root = document.documentElement;
    if (options.ref) {
      root = lookup(options.ref);
      if (!root) return { stale: true, next };
    }
    const out = [];
    let size = 0, truncated = false;
    const push = (text) => {
      if (truncated) return;
      if (size + text.length > limit) { truncated = true; return; }
      out.push(text);
      size += text.length + 1;
    };

    function walk(node, depth) {
      if (truncated) return;
      if (node.nodeType === Node.TEXT_NODE) {
        if (filter === 'interactive') return;
        const parent = node.parentElement;
        const text = squash(node.textContent, 300);
        if (text && parent && (filter === 'all' || inView(parent))) push('  '.repeat(depth) + 'text ' + quote(text));
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) {
        if (node.nodeType === Node.DOCUMENT_FRAGMENT_NODE) for (const child of node.childNodes) walk(child, depth);
        return;
      }
      const element = node;
      if (['script', 'style', 'noscript', 'template', 'head', 'meta', 'link'].includes(element.localName)) return;
      if (!visible(element)) return;
      const kind = role(element);
      const shown = kind && (INTERACTIVE.has(kind) || (filter !== 'interactive' && STRUCTURE.has(kind)) || (filter === 'interactive' && kind === 'iframe'));
      let inner = depth;
      if (shown && (filter === 'all' || inView(element))) {
        push(line(element, kind, depth));
        inner = depth + 1;
        if (inner >= maxDepth) return;
        if (NAME_FROM_CONTENT.has(kind)) {
          // 名字已经是里面的文字了，只再往里找能操作的东西（比如链接里的按钮，少见）
          for (const child of element.querySelectorAll('a[href], button, input, select, textarea, [role="button"], [role="link"]')) {
            if (child !== element && visible(child) && (filter === 'all' || inView(child))) {
              const childKind = role(child);
              if (childKind) push(line(child, childKind, inner));
            }
          }
          return;
        }
      }
      for (const child of children(element)) walk(child, inner);
    }

    walk(root, 0);
    return { text: out.join('\n'), truncated, next, url: location.href, title: document.title };
  }

  // 按一句描述找元素（"搜索框"、"add to cart button"）：把描述拆成词，看元素的角色、名字、几个属性里出现了几个
  const ROLE_WORDS = { link: ['link', 'links', '链接', 'enlace'], button: ['button', 'btn', '按钮', 'botón', 'boton'],
    textbox: ['field', 'input', 'box', 'textbox', '输入', '框', 'campo'], searchbox: ['search', '搜索', 'buscar'],
    checkbox: ['checkbox', 'check', '勾选', '复选'], combobox: ['select', 'dropdown', '下拉', 'menu'], heading: ['heading', 'title', '标题'] };

  function find(options) {
    next = Math.max(next, Number(options.start) || 1);
    const query = String(options.query || '').toLowerCase();
    const words = query.split(/[\s,，。.;；]+/).filter(Boolean);
    if (!words.length) return { text: '', next, count: 0 };
    const scored = [];
    const visit = (scope) => {
      for (const element of scope.querySelectorAll('*')) {
        if (element.shadowRoot) visit(element.shadowRoot);
        if ((element.localName === 'iframe' || element.localName === 'frame') && frameDocument(element)) visit(frameDocument(element));
        const kind = role(element);
        if (!kind || !(INTERACTIVE.has(kind) || STRUCTURE.has(kind)) || !visible(element)) continue;
        const haystack = [kind, name(element, kind), element.getAttribute('placeholder'), element.getAttribute('name'),
          element.id, element.getAttribute('type'), element.getAttribute('title')].join(' ').toLowerCase();
        let score = 0;
        for (const word of words) {
          if (haystack.includes(word)) score += 2;
          else if ((ROLE_WORDS[kind] || []).includes(word)) score += 1;
          else if (word.length > 1 && [...word].some((char) => char > '⹿' && haystack.includes(char))) score += 0.5;  // 中文：单个字也算一点
        }
        if (score > 0) scored.push({ element, kind, score: score + (inView(element) ? 0.25 : 0) });
      }
    };
    visit(document);
    scored.sort((a, b) => b.score - a.score);
    const top = scored.slice(0, Math.max(1, Number(options.limit) || 20));
    return { text: top.map((item) => line(item.element, item.kind, 0)).join('\n'), next, count: top.length };
  }

  // 正文：文章、文档这类页面先找 <main>、<article>；没有就是整页显示出来的文字（innerText 只含看得见的）
  function text(options) {
    const limit = Number(options.limit) || 50000;
    const main = document.querySelector('main, article, [role="main"]');
    const source = main && (main.innerText || '').trim().length > 200 ? main : document.body || document.documentElement;
    let all = (source.innerText || '').replace(/\n{3,}/g, '\n\n').trim();
    const truncated = all.length > limit;
    if (truncated) all = all.slice(0, limit);
    return { text: all, truncated, url: location.href, title: document.title };
  }

  // ---- 要操作的元素：在哪儿、是什么 -----------------------------------------

  // 一个点在页面上落在哪个元素上。穿过同一网站的 frame 和打开的 shadow root；落在别的网站的 frame 上就停下
  function atPoint(x, y) {
    let scope = document, offsetX = 0, offsetY = 0, hit = null;
    for (let guard = 0; guard < 20; guard += 1) {
      const found = scope.elementFromPoint(x - offsetX, y - offsetY);
      if (!found) break;
      hit = found;
      let deeper = found;
      while (deeper.shadowRoot) {
        const inner = deeper.shadowRoot.elementFromPoint(x - offsetX, y - offsetY);
        if (!inner || inner === deeper) break;
        deeper = inner;
      }
      hit = deeper;
      if ((hit.localName === 'iframe' || hit.localName === 'frame') && frameDocument(hit)) {
        const box = hit.getBoundingClientRect();
        offsetX += box.left + hit.clientLeft;
        offsetY += box.top + hit.clientTop;
        scope = frameDocument(hit);
        continue;
      }
      break;
    }
    return hit;
  }

  function facts(element) {
    if (!element) return { found: false };
    const frame = (element.localName === 'iframe' || element.localName === 'frame') && !frameDocument(element);
    const target = control(element);
    const kind = role(target) || role(element) || element.localName;
    return {
      found: true,
      tag: target.localName,
      role: kind,
      name: name(target, kind),
      type: inputType(target),
      editable: editable(target),
      password: isPassword(target),
      card: isCard(target),
      submits: submits(element),
      inForm: Boolean(target.form || target.closest('form')),
      crossOrigin: frame,
      frameUrl: frame ? element.src || '' : '',
      href: target.localName === 'a' ? target.href : '',
    };
  }

  function describe(options) {
    let element = null;
    if (options.ref) {
      element = lookup(options.ref);
      if (!element) return { found: false, stale: true, url: location.href };
    } else if (options.focus) {
      element = deepActive();
      if (element === document.body || element === document.documentElement) element = null;
    } else if (typeof options.x === 'number') {
      element = atPoint(options.x, options.y);
    }
    return { ...facts(element), url: location.href, title: document.title };
  }

  // 一个元素的中心点在窗口里的坐标（真正的点击由 Python 发鼠标事件，网页收到的是和真人一样的点击）
  function point(options) {
    const element = lookup(options.ref);
    if (!element) return { stale: true };
    const view = element.ownerDocument.defaultView;
    let box = element.getBoundingClientRect();
    if (!(box.top >= 0 && box.left >= 0 && box.bottom <= view.innerHeight && box.right <= view.innerWidth)) {
      element.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
      box = element.getBoundingClientRect();
    }
    const rects = [...element.getClientRects()].filter((rect) => rect.width > 0 && rect.height > 0);
    const rect = rects.find((one) => one.bottom > 0 && one.top < view.innerHeight) || rects[0] || box;
    let x = rect.left + rect.width / 2, y = rect.top + rect.height / 2;
    // 在同一网站的 frame 里：加上 frame 在外面那一层里的位置
    for (let frame = view.frameElement, guard = 0; frame && guard < 20; frame = frame.ownerDocument.defaultView.frameElement, guard += 1) {
      const outer = frame.getBoundingClientRect();
      x += outer.left + frame.clientLeft;
      y += outer.top + frame.clientTop;
    }
    if (!rect.width && !rect.height) return { hidden: true, x, y };
    const hit = atPoint(x, y);
    return { x: Math.round(x), y: Math.round(y), covered: Boolean(hit && hit !== element && !element.contains(hit) && !hit.contains(element)) };
  }

  function scrollTo(options) {
    const element = lookup(options.ref);
    if (!element) return { stale: true };
    element.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
    return { ok: true };
  }

  function focus(options) {
    const element = lookup(options.ref);
    if (!element) return { stale: true };
    element.focus({ preventScroll: false });
    return { ok: true, editable: editable(element) };
  }

  // 直接设一个表单元素的值（form_input）。下拉框按选项的值或文字找，勾选框给 true / false，输入框写进去。
  // 写完以后发 input 和 change 事件：网页（比如用 React 写的）是靠它们知道值变了的
  function setValue(options) {
    const element = lookup(options.ref);
    if (!element) return { stale: true };
    const value = options.value;
    const tag = element.localName, type = inputType(element);
    const fire = () => {
      element.dispatchEvent(new Event('input', { bubbles: true }));
      element.dispatchEvent(new Event('change', { bubbles: true }));
    };
    if (tag === 'select') {
      const wanted = String(value).trim().toLowerCase();
      const option = [...element.options].find((one) => one.value.toLowerCase() === wanted)
        || [...element.options].find((one) => one.text.trim().toLowerCase() === wanted)
        || [...element.options].find((one) => one.text.trim().toLowerCase().includes(wanted));
      if (!option) return { error: 'no-option', options: [...element.options].slice(0, 30).map((one) => one.text.trim()) };
      element.value = option.value;
      fire();
      return { ok: true, shown: option.text.trim() };
    }
    if (type === 'checkbox' || type === 'radio') {
      const want = value === true || ['true', '1', 'yes', 'on', 'checked'].includes(String(value).toLowerCase());
      if (element.checked !== want) element.click();
      return { ok: true, shown: element.checked ? 'checked' : 'unchecked' };
    }
    if (element.isContentEditable) {
      element.focus();
      return { typing: true };  // 可以编辑的普通元素：交给 Python 用键盘输入
    }
    if (tag === 'input' || tag === 'textarea') {
      if (element.disabled || element.readOnly) return { error: 'readonly' };
      element.focus();
      element.value = String(value);
      fire();
      return { ok: true };
    }
    return { error: 'not-a-field', role: role(element) };
  }

  function viewport() {
    const view = window.visualViewport;
    return { x: view ? view.pageLeft : scrollX, y: view ? view.pageTop : scrollY, width: innerWidth, height: innerHeight, ready: document.readyState };
  }

  globalThis.__zhifox = { read, find, text, describe, point, scrollTo, focus, setValue, viewport };
  return 'installed';
})()
