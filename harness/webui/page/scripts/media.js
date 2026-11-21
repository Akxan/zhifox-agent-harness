  // ---- 多媒体 --------------------------------------------------------------
  // 左边栏的第三个标签：所有会话里发过的截图、图片、PDF、Word / Excel / PowerPoint 和文本文件（助手程序那边见 media.py）。
  // 列表由助手程序排好、筛好，一页一页地取（/api/media/list）：附件多了以后，页面不用一次拿着几千张缩略图。
  // 什么时候重取：状态里的 media.version 变了（发了新附件、删了会话、改了标题），而这一栏正开着；
  // 没开着就记一笔"过时了"，等打开时再取。页面不轮询。
  // 每一项鼠标移上去出现三个按钮：在访达中显示、定位到会话、下载（和会话、记忆的那一排一样）。没有删除：
  // 附件是对话记录的一部分，删掉它所在的会话时才一起删（查看器里有这句说明）。

  const MEDIA_FILTERS = [['all', k('所有')], ['image', k('图片')], ['pdf', 'PDF'], ['document', k('文档')], ['text', k('文本')]];
  const MEDIA_PAGE = 60;
  const mediaList = $('media-list'), mediaSearch = $('media-search'), mediaFilters = $('media-filters');
  // items：取回来的那些（助手程序给的顺序，最新的在前）；ordered：画出来的顺序（每天里先是图片、再是文件），查看器按它左右翻
  const library = { items: [], ordered: [], counts: null, next: null, kind: 'all', query: '', loading: false, error: '', version: null, stale: true, seq: 0, seen: null };
  let mediaDrawn = '', mediaTimer = 0;

  // 筛选按钮只建一次，之后只改上面的数字和选中的状态：重画会把焦点弄丢
  for (const [kind, label] of MEDIA_FILTERS) {
    const chip = mediaFilters.appendChild(el('button', 'media-filter'));
    chip.type = 'button';
    chip.setAttribute('role', 'radio');
    chip.dataset.kind = kind;
    chip.appendChild(kind === 'pdf' ? el('span', null, label) : tel('span', null, label));
    chip.appendChild(el('span', 'media-filter-count'));
    chip.addEventListener('click', () => chooseMediaKind(kind, false));
  }
  // 方向键在几个筛选按钮之间移动（WAI-ARIA 的单选按钮组）
  mediaFilters.addEventListener('keydown', (key) => {
    const chips = [...mediaFilters.children].filter((chip) => !chip.disabled);
    const at = chips.findIndex((chip) => chip.dataset.kind === library.kind);
    const next = { ArrowRight: at + 1, ArrowDown: at + 1, ArrowLeft: at - 1, ArrowUp: at - 1, Home: 0, End: chips.length - 1 }[key.key];
    if (next === undefined || !chips.length) return;
    key.preventDefault();
    chooseMediaKind(chips[(next + chips.length) % chips.length].dataset.kind, true);
  });

  function drawMediaFilters() {
    for (const chip of mediaFilters.children) {
      const kind = chip.dataset.kind, on = kind === library.kind;
      const count = library.counts ? Number(library.counts[kind]) || 0 : null;
      chip.setAttribute('aria-checked', String(on));
      chip.tabIndex = on ? 0 : -1;
      chip.lastChild.textContent = count === null ? '' : count > 999 ? '999+' : String(count);
      chip.disabled = count === 0 && !on && kind !== 'all';  // 一个都没有的类别点了也是空的
    }
  }

  function chooseMediaKind(kind, focus) {
    if (kind !== library.kind) {
      library.kind = kind;
      drawMediaFilters();
      loadMedia(true, true);
    }
    if (focus) mediaFilters.querySelector('[data-kind="' + kind + '"]').focus();
  }

  // 状态里的 media：{count, version}。助手程序是旧版本、状态里没有它时，这个标签不出现
  function syncMediaTab(info) {
    const supported = Boolean(info) && typeof info === 'object';
    if (supported) {
      const count = Number(info.count) || 0;
      $('media-count').hidden = !count;
      $('media-count').textContent = count > 999 ? '999+' : count ? String(count) : '';
      if (info.version !== library.version) {
        library.version = info.version;
        library.stale = true;
        if (navTab === 'media' && !$('nav-tab-media').hidden) loadMedia(true);
      }
    }
    if ($('nav-tab-media').hidden === supported) {
      $('nav-tab-media').hidden = !supported;
      syncNavTabs();  // 上次选的就是"多媒体"的话，这时换过去（见 setNavTab）
    }
  }

  function showMediaTab() {
    mediaDrawn = '';
    drawMediaFilters();
    if (library.stale || !library.counts) loadMedia(true); else drawMedia();
  }

  // 取一页。fresh：从头取（重取时把已经看到的那么多一起取回来，列表不会缩回第一页，滚动的位置也不跳）；
  // restart：换了筛选或搜索词，只要第一页。返回有没有取到新的
  async function loadMedia(fresh, restart) {
    if (!fresh && (library.loading || library.next === null)) return false;
    const mine = ++library.seq;
    const cursor = fresh ? 0 : library.next;
    const limit = fresh && !restart ? Math.min(500, Math.max(MEDIA_PAGE, library.items.length)) : MEDIA_PAGE;
    library.loading = true;
    library.stale = false;
    if (fresh && !library.items.length) drawMedia();  // 第一次：先画出"正在读"的样子
    else if (!fresh) drawMedia();
    const reply = await call('/api/media/list', { kind: library.kind, query: library.query.trim(), cursor, limit });
    if (mine !== library.seq) return false;  // 这期间又换了筛选或搜索词，这次的结果已经过时了
    library.loading = false;
    if (!reply.ok) {
      library.error = reply.error;
      library.stale = true;  // 下次打开这一栏再试一次
      drawMedia();
      return false;
    }
    library.error = '';
    const items = (Array.isArray(reply.data.items) ? reply.data.items : []).filter((item) => item && typeof item.id === 'string' && typeof item.name === 'string');
    if (fresh) library.items = items; else library.items.push(...items);
    library.counts = reply.data.counts && typeof reply.data.counts === 'object' ? reply.data.counts : null;
    library.next = Number.isInteger(reply.data.next) ? reply.data.next : null;
    drawMediaFilters();
    drawMedia();
    return items.length > 0;
  }

  // 按哪一天分组："今天""昨天"，再早的写日期和星期
  function dayLabel(seconds) {
    const date = new Date(seconds * 1000), now = new Date();
    const days = Math.round((startOfDay(now) - startOfDay(date)) / DAY);
    if (days <= 0) return t('今天');
    if (days === 1) return t('昨天');
    const options = { month: 'long', day: 'numeric', weekday: 'short' };
    if (date.getFullYear() !== now.getFullYear()) options.year = 'numeric';
    return date.toLocaleDateString(LOCALES[language], options);
  }

  function drawMedia() {
    if (navTab !== 'media') return;  // 看不见就不画，换到这个标签时再画
    const print = JSON.stringify([library.items.map((item) => [item.id, item.conversation_title, item.original, item.available, item.size, item.thumb ? item.thumb.length : 0]),
      library.loading && !library.items.length, library.loading && library.next !== null, library.error, library.query, library.kind, library.counts, library.next, language, startOfDay(new Date())]);
    if (print === mediaDrawn) return;
    mediaDrawn = print;
    // 重画之前记下焦点在哪一项的哪个按钮上，画完放回去（比如正用键盘在列表里走着，来了一个新附件）
    const active = document.activeElement;
    const focused = active && mediaList.contains(active) && active.dataset.media ? [active.dataset.media, active.dataset.action || ''] : null;
    const first = library.seen === null;  // 列表第一次出现：前几项依次浮现。之后只有新来的那几项浮现
    const seen = library.seen || new Set();
    let staggered = 0;
    const entering = (node, id) => {
      if (first && staggered < 12) {
        node.classList.add('enter');
        node.style.animationDelay = staggered * 22 + 'ms';
        staggered += 1;
      } else if (!first && !seen.has(id)) node.classList.add('enter');
      seen.add(id);
      return node;
    };

    const nodes = [];
    library.ordered = [];
    if (!library.items.length) nodes.push(mediaEmpty());
    else {
      const days = new Map();
      for (const item of library.items) {
        const key = startOfDay(new Date((Number(item.at) || 0) * 1000));
        if (!days.has(key)) days.set(key, { at: Number(item.at) || 0, pictures: [], files: [] });
        days.get(key)[item.kind === 'image' ? 'pictures' : 'files'].push(item);
      }
      for (const day of days.values()) {
        const group = el('section', 'media-day');
        const head = group.appendChild(el('h3', 'media-day-head'));
        head.appendChild(el('span', null, dayLabel(day.at)));
        if (day.pictures.length) {
          const grid = group.appendChild(el('ul', 'media-grid'));
          for (const item of day.pictures) grid.appendChild(entering(mediaTile(item), item.id));
        }
        if (day.files.length) {
          const rows = group.appendChild(el('ul', 'media-rows'));
          for (const item of day.files) rows.appendChild(entering(mediaRow(item), item.id));
        }
        library.ordered.push(...day.pictures, ...day.files);
        nodes.push(group);
      }
      if (library.next !== null) {
        // 还有下一页：滚到这里自动取（IntersectionObserver）；浏览器不支持的话，点这个按钮取
        const more = el('button', 'media-more mini');
        more.type = 'button';
        retext(more, library.loading ? k('读取中…') : k('显示更多'));
        more.addEventListener('click', () => loadMedia(false));
        nodes.push(more);
      }
    }
    if (library.items.length) library.seen = seen;  // 第一次真正画出附件时才算"出现过"，前面的"正在读"不算
    mediaList.replaceChildren(...nodes);
    if (mediaObserver) {
      mediaObserver.disconnect();
      const more = mediaList.querySelector('.media-more');
      if (more) mediaObserver.observe(more);
    }
    if (focused) {
      const again = mediaList.querySelector('[data-media="' + CSS.escape(focused[0]) + '"]' + (focused[1] ? '[data-action="' + focused[1] + '"]' : ':not([data-action])'));
      if (again) again.focus({ preventScroll: true });
    }
  }

  const mediaObserver = 'IntersectionObserver' in window
    ? new IntersectionObserver((entries) => { if (entries.some((entry) => entry.isIntersecting)) loadMedia(false); }, { root: mediaList, rootMargin: '0px 0px 320px 0px' })
    : null;

  // 空的时候说什么：正在读、出错了、一个附件都没有、搜不到、这一类没有
  function mediaEmpty() {
    if (library.loading && !library.error) {
      const grid = el('div', 'media-grid');
      grid.setAttribute('aria-hidden', 'true');
      for (let index = 0; index < 6; index += 1) grid.appendChild(el('div', 'media-skeleton'));
      const box = el('div', 'media-loading');
      box.append(el('div', 'media-day-head', ''), grid, tel('span', 'sr-only', k('读取中…')));
      return box;
    }
    const box = el('div', 'media-empty');
    if (library.error) {
      box.appendChild(tel('b', null, k('多媒体库没能读出来')));
      box.appendChild(el('p', null, library.error));
      const retry = box.appendChild(tel('button', 'mini', k('重试')));
      retry.type = 'button';
      retry.addEventListener('click', () => loadMedia(true));
      return box;
    }
    const query = library.query.trim();
    if (!query && library.kind === 'all') {
      const art = box.appendChild(el('div', 'media-art'));
      art.setAttribute('aria-hidden', 'true');
      art.append(el('i'), el('i'), el('i'));
      box.appendChild(tel('b', null, k('还没有附件')));
      box.appendChild(tel('p', null, k('你在任何一段会话里发过的截图、图片、PDF、Word / Excel / PowerPoint 和文本文件，都会收在这里：可以放大看、下载，也能一键回到当初发它的那条消息。')));
      box.appendChild(tel('p', null, k('在输入框里粘贴截图，或者点回形针添加文件，发出去以后就会出现在这里。')));
      if (body.classList.contains('ready')) box.classList.add('enter');
      return box;
    }
    if (query) {
      box.appendChild(tel('p', null, k('没有找到名字或会话标题里有“{query}”的文件。'), { query }));
      return box;
    }
    const none = { image: k('还没有发过图片。'), pdf: k('还没有发过 PDF。'), document: k('还没有发过 Word、Excel 或 PowerPoint 文件。'), text: k('还没有发过文本或代码文件。') };
    box.appendChild(tel('p', null, none[library.kind]));
    return box;
  }

  function mediaTile(item) {
    const tile = el('li', 'media-tile' + (item.available === false ? ' media-missing' : ''));
    const open = tile.appendChild(el('button', 'media-tile-open'));
    open.type = 'button';
    open.dataset.media = item.id;
    open.title = item.name;
    open.setAttribute('aria-label', t('{name}，来自“{title}”', { name: item.name, title: item.conversation_title || t('新对话') }));
    if (typeof item.thumb === 'string' && THUMB_URL.test(item.thumb)) {
      const image = open.appendChild(el('img'));
      image.alt = '';
      image.decoding = 'async';
      image.src = item.thumb;  // 只接受 data: 开头的图片（页面的安全策略也只允许这种）
    } else {
      open.appendChild(icon('image'));  // 旧会话里没有缩略图的
    }
    open.addEventListener('click', () => openFromLibrary(item.id, open));
    tile.appendChild(mediaActions(item, 'media-tile-actions'));
    return tile;
  }

  function mediaRow(item) {
    const row = el('li', 'media-row' + (item.available === false ? ' media-missing' : ''));
    const open = row.appendChild(el('button', 'media-row-open'));
    open.type = 'button';
    open.dataset.media = item.id;
    open.title = item.name;
    open.appendChild(mediaBadge(item));
    const words = open.appendChild(el('span', 'media-words'));
    words.appendChild(el('span', 'media-name', item.name));
    const size = item.available === false ? t('内容没有保存下来') : sizeText(item);
    words.appendChild(el('span', 'media-meta', [kindLabel(item), size, item.conversation_title || t('新对话')].join(' · ')));
    open.addEventListener('click', () => openFromLibrary(item.id, open));
    row.appendChild(mediaActions(item, 'media-row-actions'));
    return row;
  }

  // 一项右边（方格是下沿）的三个按钮：在访达中显示、定位到会话、下载。没有原件的（旧会话里的）不能在访达中显示
  function mediaActions(item, className) {
    const box = el('div', className);
    const reveal = box.appendChild(iconButton('folder', revealLabel()));
    if (!item.original) {
      reveal.disabled = true;
      tattr(reveal, 'title', k('这个附件没有单独存下原件：它是在有多媒体库之前发的，内容保存在它所在的会话的文件里。'));
    }
    const go = box.appendChild(iconButton('locate', k('定位到会话')));
    const save = box.appendChild(iconButton('download', k('下载')));
    save.disabled = item.available === false;
    for (const [button, action] of [[reveal, 'reveal'], [go, 'locate'], [save, 'download']]) {
      button.dataset.media = item.id;
      button.dataset.action = action;
    }
    reveal.addEventListener('click', () => revealItem(item));
    go.addEventListener('click', () => locateItem(item));
    save.addEventListener('click', () => downloadItem(item, save));
    return box;
  }

  // 从多媒体库点开：左右翻的是画出来的顺序；翻到最后一个、还有下一页时，先取下一页
  function openFromLibrary(id, opener) {
    const at = library.ordered.findIndex((item) => item.id === id);
    if (at < 0) return;
    const more = async () => {
      const current = viewerItem();
      await loadMedia(false);
      if (looking && looking.list !== library.ordered) {
        looking.list = library.ordered;  // 新的一页可能插进同一天的方格里，按"正在看的那一项"重新找到位置
        looking.at = Math.max(0, library.ordered.findIndex((item) => current && item.id === current.id));
      }
    };
    more.ready = () => library.next !== null && !library.loading;
    openViewer(library.ordered, at, opener, more);
  }

  // 搜索：文件名和会话标题。敲字的时候稍等一下再发，输入法组字的时候不发
  function queueMediaSearch() {
    clearTimeout(mediaTimer);
    mediaTimer = setTimeout(() => {
      if (library.query === mediaSearch.value) return;
      library.query = mediaSearch.value;
      loadMedia(true, true);
    }, 180);
  }
  mediaSearch.addEventListener('input', (typed) => { if (!typed.isComposing) queueMediaSearch(); });
  mediaSearch.addEventListener('compositionend', queueMediaSearch);
  mediaSearch.addEventListener('keydown', (key) => {
    if (key.isComposing || key.keyCode === 229) return;  // 输入法正在组字
    if (key.key === 'Escape') {  // Esc：先清空搜索词，再按一次离开搜索框
      key.preventDefault();
      if (mediaSearch.value) { mediaSearch.value = ''; queueMediaSearch(); } else mediaSearch.blur();
    } else if (key.key === 'Enter') {  // 回车：打开第一项
      key.preventDefault();
      const first = mediaList.querySelector('[data-media]:not([data-action])');
      if (first) first.click();
    }
  });
