// 智狐放进每个网页里的一小段脚本：把页面整理成模型读得懂的一行一行，给每个能操作的元素一个编号（ref_12），
// 之后模型说"点 ref_12"，就靠这里记下的编号找回那个元素。
//
// 它运行在一个"隔离世界"（isolated world）里，见 session_page.py：和网页自己的脚本共用同一个页面（DOM），
// 但各有各的全局变量。所以网页看不到这里记下的编号，也改不了这里的函数；网页改写了 Element.prototype 之类的东西，
// 这里用的仍然是浏览器原本的那一套。
//
// 只读"页面上显示出来的东西"：display:none、visibility:hidden、aria-hidden 的内容都跳过，正文里字号是零、透明、
// 挪到屏幕外、裁光的字也跳过（见"看得见的字"那一节）。网页可以在看不见的地方藏一段写给模型的话（"忽略之前的指示……"），
// 不显示的东西就不交给模型。
//
// 这里只返回数据，不做任何判断（要不要问用户由 Python 那边的 permissions.py 决定）。
//
// 整个文件是一个函数，装进隔离世界时由 session_page.py 调用一次（INSTALL），交进来 LIMITS：判断"看不见"用的两个数
// （tiny、offscreen），和读网页（fetch/hidden.py）是同一组，写在 config.py 里，这里不另写一份。
((LIMITS) => {
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
    const look = looker();
    const unseen = (node) => look.blank(node) || look.buried(node.parentElement, null);  // 和 get_page_text 一样的规矩
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
        if (text && parent && (filter === 'all' || inView(parent)) && !unseen(node)) push('  '.repeat(depth) + 'text ' + quote(text));
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

  // ---- 看得见的字：get_page_text 读的正文 ----------------------------------------
  //
  // 以前用 innerText：它只跳过 display:none 和 visibility:hidden，aria-hidden、字号是零、透明的字、挪到屏幕外、
  // clip / clip-path 裁光的字照样交给模型，网页就能在你看不见的地方给模型藏一段话（和读网页 web_fetch 是同一个道理，
  // 见 harness/fetch/hidden.py）。浏览器里有真的样子（getComputedStyle、每一段字实际画在哪儿），所以不用自己算 CSS，
  // 直接照浏览器算好的判断；哪几种算看不见、阈值多少和 hidden.py 一样，两个数（LIMITS）由 Python 从 config.py 交过来。
  //   整段（元素连同里面的一切）：aria-hidden="true"；clip: rect(…) 裁光（只对 absolute、fixed 的元素起作用，和浏览器一样）；
  //     clip-path: inset(…) 上下或左右裁光、circle(0)、ellipse(0 …)；放不下的会被裁掉、里面又不超过 LIMITS.tiny 像素宽或高的框
  //     （height:0;overflow:hidden、sr-only 一类）
  //   一段字：visibility 不是 visible；字号不超过 LIMITS.tiny 像素；字是透明的（color、-webkit-text-fill-color 的透明度是零），
  //     除非由别的画出来（background-clip:text 的渐变标题、阴影、描边）；画出来的每一块都不超过 LIMITS.tiny 像素宽或高
  //     （scale(0) 这类缩成零的）；每一块都在页面外面、而且挪出去至少 LIMITS.offscreen 像素（left:-9999px、text-indent:-9999px）
  // opacity:0 不算，和 web_fetch 一样，但理由不全一样：这里脚本已经跑过了，可"滚到这里才淡入"的内容（AOS、Webflow、Framer
  // 的滚动效果）在页面下面还是 opacity:0，你往下一滚就看得到；就算在这一屏里，入场动画也可能还没播完（AOS 的 delay 能到几秒）。
  // 算藏起来的话，这类网站下半页的正文都读不到。read_page 的 visible() 照旧看 opacity（它默认只读这一屏，这一屏的早就淡入了）。
  //
  // 分行、分段照 innerText 的规矩（HTML 规范的 rendered text collection steps）：块级元素前后换一行，<p> 前后空一行，
  // <br> 换行，表格的格子之间一个 Tab、行之间换行，不折叠空白的（<pre>）原样留着，别的连着的空白收成一个空格、行首行尾的去掉。
  // 和 innerText 一样不读 iframe 里面、::before 里的字、图片的 alt、输入框里的值。
  const TINY = Number(LIMITS.tiny);
  const OFFSCREEN = Number(LIMITS.offscreen);
  const CLIPS = new Set(['hidden', 'clip', 'auto', 'scroll']);
  const SILENT = new Set(['script', 'style', 'noscript', 'template', 'head', 'title', 'meta', 'link']);
  const PRESERVE = new Set(['pre', 'pre-wrap', 'break-spaces']);
  // 块级的（前后各换一行）。table-cell、table-row 不在里面：格子之间是 Tab，行之间换行
  const BLOCKS = new Set(['block', 'flow-root', 'flex', 'grid', 'table', 'list-item', 'table-caption', '-webkit-box']);

  // 透明度：浏览器算好的颜色是 rgb(…)、rgba(…, a)、或者 oklch(… / a) 这类
  function alpha(color) {
    if (!color || color === 'transparent') return color ? 0 : 1;
    const inside = /\(([^)]*)\)/.exec(color);
    if (!inside) return 1;
    const parts = inside[1].includes('/') ? [inside[1].split('/').pop()] : inside[1].split(',');
    if (!inside[1].includes('/') && parts.length < 4) return 1;
    const last = parts[parts.length - 1].trim();
    return last.endsWith('%') ? parseFloat(last) / 100 : parseFloat(last);
  }

  // 一次读页面里用的：每个元素的样子算一次（同一个元素里的几段字共用）
  function looker() {
    const styles = new Map();
    const gone = new Map();
    const style = (element) => {
      let found = styles.get(element);
      if (!found) styles.set(element, (found = getComputedStyle(element)));
      return found;
    };
    // 字由别的画出来：外层（包括自己）有 background-clip:text，或者这里的字有阴影、描边
    const painted = (element) => {
      const own = style(element);
      if ((own.textShadow && own.textShadow !== 'none') || parseFloat(own.webkitTextStrokeWidth) > 0) return true;
      for (let at = element; at; at = at.parentElement) {
        const clip = style(at).backgroundClip || style(at).webkitBackgroundClip || '';
        if (/\btext\b/.test(clip)) return true;
      }
      return false;
    };
    // 这个元素连同里面的一切看不见（不看外层：外层由调用的那一方管）
    const hides = (element) => {
      if (gone.has(element)) return gone.get(element);
      const own = style(element);
      let hidden = element.getAttribute('aria-hidden') === 'true' || clippedRect(own) || clippedPath(element, own);
      if (!hidden && own.display !== 'inline' && own.display !== 'contents') {
        // 放不下的会被裁掉（computed 里另一边的 visible 已经变成了 auto），里面又几乎没有地方：clientWidth、clientHeight 是边框以内的大小
        hidden = (CLIPS.has(own.overflowX) && element.clientWidth <= TINY) || (CLIPS.has(own.overflowY) && element.clientHeight <= TINY);
      }
      gone.set(element, hidden);
      return hidden;
    };
    // 外层（到 root 为止，不含 root）有没有把它整个藏起来：read_page 读到一段字时用（get_page_text 从上往下走，进门时就看过了）
    const buried = (element, root) => {
      for (let at = element; at && at !== root; at = at.parentElement) {
        if (at === document.body || at === document.documentElement) break;
        if (hides(at)) return true;
      }
      return false;
    };
    const range = document.createRange();
    // 这一段字（一个文字节点）看不见
    const blank = (node) => {
      const parent = node.parentElement;
      const own = style(parent);
      if (own.visibility !== 'visible' || parseFloat(own.fontSize) <= TINY) return true;
      if (alpha(own.webkitTextFillColor || own.color) === 0 && !painted(parent)) return true;
      // 收着的 <details> 只画出第一个 <summary>：直接写在 details 里的字没有自己的元素，checkVisibility、getClientRects 都认不出来
      for (let at = parent.closest('details:not([open])'); at; at = at.parentElement && at.parentElement.closest('details:not([open])')) {
        const summary = [...at.children].find((child) => child.localName === 'summary');
        if (!summary || !summary.contains(node)) return true;
      }
      if (parent.closest('select, option, optgroup')) return false;  // 收起来的下拉框里的选项没有画出来，innerText 照样读：和它一样
      if (parent.checkVisibility && !parent.checkVisibility()) return true;  // 收着的 <details>、content-visibility:hidden 里面
      range.selectNodeContents(node);
      const boxes = [...range.getClientRects()];
      if (!boxes.length) return true;  // 没有画出来（<details> 收着、canvas 里的备用内容……）
      if (boxes.every((box) => box.width <= TINY || box.height <= TINY)) return true;
      const width = document.documentElement.scrollWidth, height = document.documentElement.scrollHeight;
      return boxes.every((box) => {
        const left = box.left + scrollX, right = box.right + scrollX, top = box.top + scrollY, bottom = box.bottom + scrollY;
        return (right <= 0 && left <= -OFFSCREEN) || (bottom <= 0 && top <= -OFFSCREEN)
          || (left >= width && right >= width + OFFSCREEN) || (top >= height && bottom >= height + OFFSCREEN);
      });
    };
    return { style, hides, buried, blank };
  }

  // clip: rect(上, 右, 下, 左) 剩下的宽或高是零
  function clippedRect(style) {
    if (style.position !== 'absolute' && style.position !== 'fixed') return false;
    const sides = /^rect\((.*)\)$/.exec(style.clip || '');
    if (!sides) return false;
    const [top, right, bottom, left] = sides[1].split(/[\s,]+/).map((side) => (side === 'auto' ? NaN : parseFloat(side)));
    return bottom - top <= 0 || right - left <= 0;  // auto 是 NaN，比出来是 false
  }

  // clip-path 把它裁得一点都不剩：inset() 上下或左右加起来超过了它自己的大小，或者半径是零的圆、椭圆
  function clippedPath(element, style) {
    const path = style.clipPath || 'none';
    const inset = /^inset\(([^)]*)\)/.exec(path);
    if (inset) {
      const values = inset[1].split(' round ')[0].trim().split(/\s+/);
      const [top, right = top, bottom = top, left = right] = values;
      const size = (value, whole) => (value.endsWith('%') ? (parseFloat(value) / 100) * whole : parseFloat(value));
      const width = element.offsetWidth, height = element.offsetHeight;
      return size(top, height) + size(bottom, height) >= height || size(left, width) + size(right, width) >= width;
    }
    const round = /^(?:circle|ellipse)\(([^)]*)\)/.exec(path);
    return Boolean(round) && round[1].split(' at ')[0].trim().split(/\s+/).some((radius) => parseFloat(radius) === 0);
  }

  // 页面上显示出来的字，按 innerText 的规矩分行、分段（见这一节开头），看不见的跳过。读到比 limit 多一点就停
  function shownText(root, limit) {
    const look = looker();
    let out = '';
    let breaks = 0;         // 下一段字前面至少要换几行
    let space = false;      // 前面有一个还没写出来的空格（行末的空格不写）
    let fresh = true;       // 在一行的开头：前面的空白都不算
    let boxStart = false;   // 刚进一个 inline-block 这类自成一块的元素：里面开头的空白不算（外面的照样算）
    const write = (piece) => {
      if (breaks && out) out += '\n'.repeat(breaks);
      else if (space && !fresh) out += ' ';
      breaks = 0;
      space = false;
      boxStart = false;
      out += piece;
      fresh = piece.endsWith('\n');
    };
    const lineBreak = (count) => {
      breaks = Math.max(breaks, count);
      space = false;
      fresh = true;
    };
    const visitText = (node) => {
      const parent = node.parentElement;
      if (!parent || !node.data) return;
      const style = look.style(parent);
      const preserve = PRESERVE.has(style.whiteSpace);
      if (!node.data.trim()) {
        if (preserve) write(node.data);
        else if (!boxStart) space = true;
        return;
      }
      if (look.blank(node)) return;
      let data = node.data;
      if (style.textTransform === 'uppercase') data = data.toUpperCase();
      else if (style.textTransform === 'lowercase') data = data.toLowerCase();
      if (preserve) {
        write(data);
        return;
      }
      const lines = style.whiteSpace === 'pre-line' ? data.replace(/[ \t]+/g, ' ').split(/ ?\n ?/) : [data.replace(/[\t\n\f\r ]+/g, ' ')];
      lines.forEach((line, index) => {
        if (index) write('\n');
        if (line.startsWith(' ') && !boxStart) space = true;
        const core = line.replace(/^ +| +$/g, '');  // 只去掉普通的空格：&nbsp; 不折叠，留着
        if (core) write(core);
        if (core && line.endsWith(' ')) space = true;
      });
    };
    const visit = (node) => {
      if (out.length > limit) return;
      if (node.nodeType === Node.TEXT_NODE) {
        visitText(node);
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      const element = node;
      if (SILENT.has(element.localName)) return;
      const style = look.style(element);
      const display = style.display;
      if (display === 'none') return;
      const whole = element === document.body || element === document.documentElement;  // 整页不会被藏掉（和 web_fetch 一样）
      if (!whole && look.hides(element)) return;
      if (element.localName === 'br') {
        space = false;
        write('\n');
        return;
      }
      const shown = style.visibility === 'visible';  // visibility:hidden 的元素自己不换行，里面写了 visible 的字照样读
      const block = !shown ? 0 : element.localName === 'p' ? 2 : BLOCKS.has(display) ? 1 : 0;
      const box = display.startsWith('inline-');  // inline-block、inline-flex……：里面开头、结尾的空白不算
      if (block) lineBreak(block);
      if (box) boxStart = true;
      // 收着的 <details> 只画出第一个 <summary>（直接写在里面的字没有自己的元素，checkVisibility 认不出来）
      const closed = element.localName === 'details' && !element.open;
      const kids = closed ? [...element.children].filter((child) => child.localName === 'summary').slice(0, 1) : element.childNodes;
      if (style.contentVisibility !== 'hidden') for (const child of kids) visit(child);
      if (box) {
        boxStart = false;
        space = false;
      }
      if (shown && display === 'table-cell' && element.nextElementSibling) {
        space = false;
        write('\t');
        fresh = true;
      }
      if (shown && display === 'table-row') lineBreak(1);
      if (block) lineBreak(block);
    };
    visit(root);
    return out;
  }

  // 正文：文章、文档这类页面先找 <main>、<article>；没有就是整页显示出来的文字（只有看得见的，见上面）
  function text(options) {
    const limit = Number(options.limit) || 50000;
    const main = document.querySelector('main, article, [role="main"]');
    const inMain = main ? shownText(main, limit) : '';
    const source = main && inMain.trim().length > 200 ? null : document.body || document.documentElement;
    let all = (source ? shownText(source, limit) : inMain).replace(/\n{3,}/g, '\n\n').trim();
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
})
