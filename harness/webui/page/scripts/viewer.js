// ---- 查看器：放大、缩小、拖动、下载、复制、定位 ----------------------------
// 对话里的附件、输入框上方还没发出去的图片、左边栏的"多媒体"，点开都是这一个查看器。
//   图片    放大缩小（按钮、⌘/Ctrl 加 + − 0、触控板捏合或 ⌘/Ctrl 加滚轮、手机上两指捏合）；放大以后拖动、滚轮平移；
//           双击在"适合屏幕"和"原始大小"之间切换。全靠改 transform，浏览器在合成线程上完成，不重新排版。
//   文本    等宽字体显示全文，可以复制。
//   其余    PDF、Word、Excel、PowerPoint：一张说明卡片，可以下载。页面的安全策略不允许嵌入 PDF（见 server.py 的 CSP：
//           default-src 'none' 连 <iframe>、<embed> 都拦下了），所以不在这里打开。
// ←/→ 翻到上一个、下一个：从多媒体库点开的，翻的是当前筛选下的那些；从对话里点开的，翻的是这一条消息里的附件。
//
// 原图要从助手程序取（/api/media/file），再变成 data: 地址放进 <img>：页面的安全策略只允许 data: 图片，
// blob: 地址的图片会被拦下。等原图的时候先显示手头的预览图，到了再换上，看的位置不变。
// 下载用的是 blob: 地址：它不是图片、也不是请求，只是交给浏览器存成文件，安全策略管不着它。

const viewer = $('viewer'), viewerImage = $('viewer-image'), viewerStage = $('viewer-stage');
const viewerText = $('viewer-text'), viewerCard = $('viewer-card');
const ZOOM_STEP = 1.25;
let looking = null;       // 在看的是哪一组里的第几个：{list, at, opener, more, text, locating}。more：多媒体库还有下一页时，取下一页
let lookSeq = 0;          // 每换一项加一。回来得晚的、已经不在看的那一项的内容，靠它认出来、丢掉
const view = { width: 0, height: 0, fit: 1, scale: 1, x: 0, y: 0, original: false };  // 图片的原始大小、缩放和平移
let fetched = new Map();  // 这次打开查看器以来取过的内容（编号 → {blob, name}），来回翻不用重取。关上就放掉

const viewerItem = () => (looking ? looking.list[looking.at] : null);

// 出了错、做完了一件事要告诉你：查看器开着的时候写在它下面那一行里（浮在页面上的提示会被查看器挡住），关着的时候照常弹出来
let viewerSayTimer = 0;
function notify(text) {
  if (!viewer.open) { toast(text); return; }
  const box = $('viewer-status');
  box.textContent = text;
  box.hidden = false;
  clearTimeout(viewerSayTimer);
  viewerSayTimer = setTimeout(() => { box.hidden = true; }, Math.min(9000, Math.max(3500, text.length * 120)));
}

// 这一项在查看器里怎么显示：图片、文字，还是一张说明卡片
function viewKind(item) {
  if (item.available === false) return 'card';
  if (item.kind === 'image') return 'image';
  if (item.kind === 'text' || (item.kind === 'document' && !item.original)) return 'text';  // 旧会话里的 Word 这些只剩文字
  return 'card';
}

// 文件的标记：一块带颜色的小方块，写着 PDF、DOC、XLS……（这几个是格式的缩写，各种语言都这样写，不翻译）
function mediaBadge(item) {
  const ext = extension(item.name);
  const office = OFFICE_LABEL[ext];
  const [tone, label] = item.kind === 'pdf' ? ['pdf', 'PDF']
    : office === 'Word' ? ['word', 'DOC'] : office === 'Excel' ? ['sheet', 'XLS'] : office === 'PowerPoint' ? ['slide', 'PPT']
    : ['text', (ext || 'txt').slice(0, 4).toUpperCase()];
  if (item.kind === 'image') return el('span', 'media-badge media-text', 'IMG');
  return el('span', 'media-badge media-' + tone, label);
}

// 种类的说法：PDF、Word…；旧会话里的 Word 只剩文字，说明一下
function kindLabel(item) {
  if (item.shot !== undefined) return t('浏览器截图');
  if (item.kind === 'pdf') return 'PDF';
  if (item.kind === 'document') {
    const office = OFFICE_LABEL[extension(item.name)] || 'Office';
    return item.original ? office : t('{type} · 只有文字', { type: office });
  }
  if (item.kind === 'text') return t('文本');
  return t('图片');
}

const sizeText = (item) => (item.chars ? t('{n} 字', { n: Number(item.size).toLocaleString(LOCALES[language]) }) : formatSize(Number(item.size) || 0));

function labelViewer() {
  const mod = forKeys('⌘', 'Ctrl+');
  for (const [id, label, keys] of [['viewer-out', k('缩小'), mod + '−'], ['viewer-in', k('放大'), mod + '+'], ['viewer-level', k('在适合屏幕和原始大小之间切换'), mod + '0'],
                                     ['viewer-prev', k('上一个'), '←'], ['viewer-next', k('下一个'), '→'], ['close-viewer', k('关闭'), 'Esc']]) {
    $(id).title = t(label) + ' (' + keys + ')';
  }
}

function openViewer(list, at, opener, more) {
  if (!list.length) return;
  looking = { list, at: Math.min(Math.max(0, at), list.length - 1), opener: opener || document.activeElement, more: more || null, text: '', locating: false };
  labelViewer();
  if (!viewer.open) viewer.showModal();
  $('close-viewer').focus({ preventScroll: true });
  showItem(0);
}

// 名字、信息、按钮能不能点、右下角的说明。换语言时也重画这里（见 applyLanguage）
function drawViewerChrome() {
  const item = viewerItem();
  if (!item) return;
  const shown = viewKind(item);
  $('viewer-name').textContent = item.name;
  const dimensions = shown === 'image' && view.original && view.width ? view.width + ' × ' + view.height : '';
  const parts = [kindLabel(item), item.shot !== undefined ? '' : sizeText(item), dimensions, item.conversation_title || '', item.at ? momentText(item.at) : ''];
  $('viewer-meta').textContent = parts.filter(Boolean).join(' · ');
  $('viewer-zoom').hidden = shown !== 'image';
  const copy = $('viewer-copy');
  copy.hidden = shown === 'card';
  const copyLabel = shown === 'text' ? k('复制文字') : k('复制图片');
  tattr(copy, 'title', copyLabel);
  tattr(copy, 'aria-label', copyLabel);
  copy.disabled = shown === 'image' ? !(view.original && view.width) : !looking.text;
  const reveal = $('viewer-reveal');
  reveal.hidden = !item.id;
  reveal.disabled = !item.original;
  tattr(reveal, 'title', item.original ? showLabel() : k('这个附件没有单独存下原件：它是在有多媒体库之前发的，内容保存在它所在的会话的文件里。'));
  tattr(reveal, 'aria-label', showLabel());
  $('viewer-locate').hidden = !item.conversation;
  $('viewer-download').disabled = item.available === false;
  const total = looking.list.length, more = Boolean(looking.more && looking.more.ready());
  $('viewer-count').textContent = total > 1 || more ? (looking.at + 1) + ' / ' + total + (more ? '+' : '') : '';
  $('viewer-prev').disabled = looking.at <= 0;
  $('viewer-next').disabled = looking.at >= total - 1 && !more;
  // 附件是对话记录的一部分（对话历史只往后加，不改前面的），所以没有"删除这一个"的按钮，在这里说清楚
  if (item.shot !== undefined) retext($('viewer-note'), k('大图在这台电脑上保留 {days} 天，之后对话里还看得到小图。'), { days: item.source === 'computer' ? computerShotDays() : browserShotDays() });
  else retext($('viewer-note'), item.id ? k('附件不能单独删除：它是对话记录的一部分。删除它所在的会话时，原件会一起删除（别的会话也用到的除外）。')
    : k('还没有发出去。发出去以后，它会存进多媒体库。'));
}

// 换到 looking.at 那一项。direction：从哪边滑进来（1 是下一个，-1 是上一个，0 是刚打开）
async function showItem(direction) {
  const item = viewerItem();
  if (!item) return;
  const mine = ++lookSeq;
  looking.text = '';
  view.width = view.height = 0;
  view.original = false;
  viewerImage.hidden = viewerText.hidden = viewerCard.hidden = true;
  viewerImage.removeAttribute('src');
  viewerImage.classList.remove('smooth');
  $('viewer-spinner').hidden = true;
  viewerStage.classList.remove('zoomed');
  drawViewerChrome();
  const shown = viewKind(item);
  const target = shown === 'image' ? viewerImage : shown === 'text' ? viewerText : viewerCard;
  if (direction) play(target, [{ opacity: 0, translate: (direction * 28) + 'px 0' }, { opacity: 1, translate: '0 0' }], 260);
  if (shown === 'image') await showImage(item, mine);
  else if (shown === 'text') await showText(item, mine);
  else showCard(item, item.available === false ? t('这个文件的内容没有保存下来。') : '');
}

async function showImage(item, mine) {
  // 先放手头有的：还没发出去的是原图；对话里的有存下来的大图（旧会话里没有的话是缩略图）
  const quick = item.local ? 'data:' + item.media_type + ';base64,' + item.local.data
    : [item.preview, item.thumb].find((url) => typeof url === 'string' && url && THUMB_URL.test(url));
  viewerImage.alt = item.name;
  viewerImage.hidden = false;
  if (quick) await placeImage(quick, Boolean(item.local), mine);
  if (item.local || !(item.id || item.shot)) {
    if (!quick) showCard(item, t('这个文件的内容没有保存下来。'));
    return;
  }
  $('viewer-spinner').hidden = false;
  try {
    const got = await fetchItem(item);
    if (mine !== lookSeq) return;
    if (!got.url) got.url = await readDataUrl(got.blob);
    if (mine !== lookSeq) return;
    await placeImage(got.url, true, mine);
  } catch (problem) {
    if (mine !== lookSeq) return;
    if (quick) notify(t('原图取不到（{error}），这里显示的是预览图。', { error: problem.message || String(problem) }));
    else { viewerImage.hidden = true; showCard(item, problem.message || String(problem)); }
  } finally {
    if (mine === lookSeq) $('viewer-spinner').hidden = true;
  }
}

// 把一张图放上舞台。从预览图换成原图时，看的位置和大小不变：按两张图的宽度之比换算缩放
async function placeImage(url, original, mine) {
  viewerImage.src = url;
  try { await viewerImage.decode(); } catch (_) { return; }  // 解不出来（图片坏了）：留着原来的样子
  if (mine !== lookSeq) return;
  const before = view.width ? { width: view.width, scale: view.scale, fitted: Math.abs(view.scale - view.fit) < 0.001 } : null;
  view.width = viewerImage.naturalWidth;
  view.height = viewerImage.naturalHeight;
  view.original = original;
  Object.assign(viewerImage.style, { width: view.width + 'px', height: view.height + 'px', marginLeft: -view.width / 2 + 'px', marginTop: -view.height / 2 + 'px' });
  view.fit = fitScale();
  if (!before || before.fitted) { view.scale = view.fit; view.x = view.y = 0; }
  else view.scale = before.scale * before.width / view.width;
  applyView(false);
  drawViewerChrome();
}

// "适合屏幕"是多大：整张图放得下，四周留一点边；左右翻页的按钮压在两边，再多留一些。
// 原图不放大到超过 100%（小截图放大了只会糊）；预览图可以放大，先占好原图大概的位置
function fitScale() {
  const margin = viewerStage.clientWidth < 600 ? 24 : looking && looking.list.length > 1 ? 136 : 48;
  const room = Math.min((viewerStage.clientWidth - margin) / view.width, (viewerStage.clientHeight - 24) / view.height);
  return Math.max(0.01, view.original ? Math.min(1, room) : room);
}
const minScale = () => Math.min(view.fit, 1);
const maxScale = () => Math.max(8, view.fit * 4);

// 把缩放和平移写到图片上。图片比舞台小的那一边居中；比舞台大的那一边最多拖到边缘对齐，不会被拖出视野
function applyView(animate) {
  const spareX = Math.max(0, (view.width * view.scale - viewerStage.clientWidth) / 2);
  const spareY = Math.max(0, (view.height * view.scale - viewerStage.clientHeight) / 2);
  view.x = Math.min(spareX, Math.max(-spareX, view.x));
  view.y = Math.min(spareY, Math.max(-spareY, view.y));
  viewerImage.classList.toggle('smooth', Boolean(animate));
  viewerImage.style.transform = 'translate(' + view.x + 'px, ' + view.y + 'px) scale(' + view.scale + ')';
  viewerStage.classList.toggle('zoomed', spareX > 0 || spareY > 0);
  $('viewer-level').textContent = view.original ? Math.round(view.scale * 100) + '%' : '…';
  $('viewer-out').disabled = view.scale <= minScale() + 1e-6;
  $('viewer-in').disabled = view.scale >= maxScale() - 1e-6;
}

let zoomSayTimer = 0;
function sayZoom() {  // 读屏软件：停下来以后报一次现在的大小，不是每一帧都报
  clearTimeout(zoomSayTimer);
  zoomSayTimer = setTimeout(() => announce(t('缩放到 {n}%', { n: Math.round(view.scale * 100) })), 350);
}

// 以某一点为中心缩放（point 是相对舞台中心的位置；不给就是中心）：缩放前后，这一点下面的像素不动
function zoomAt(next, point, animate) {
  if (!view.width) return;
  const scale = Math.min(maxScale(), Math.max(minScale(), next));
  const [px, py] = point || [0, 0];
  const ratio = scale / view.scale;
  view.x = px - (px - view.x) * ratio;
  view.y = py - (py - view.y) * ratio;
  view.scale = scale;
  applyView(animate);
  sayZoom();
}

// 按一下放大或缩小 1.25 倍。路过"适合屏幕"和 100% 时停一下：这两个大小最常用
function zoomStep(up) {
  let next = view.scale * (up ? ZOOM_STEP : 1 / ZOOM_STEP);
  for (const stop of [view.fit, 1]) {
    if ((view.scale < stop - 1e-6 && next > stop) || (view.scale > stop + 1e-6 && next < stop)) next = stop;
  }
  zoomAt(next, null, true);
}

// 双击、点百分比、⌘0：在"适合屏幕"和"原始大小"之间切换（图片本来就不比屏幕大时，切到两倍）
function toggleZoom(point) {
  if (Math.abs(view.scale - view.fit) < 0.001) zoomAt(view.fit >= 0.999 ? 2 : 1, point, true);
  else zoomAt(view.fit, null, true);
}

const fromCenter = (event) => {
  const box = viewerStage.getBoundingClientRect();
  return [event.clientX - box.left - box.width / 2, event.clientY - box.top - box.height / 2];
};

// 拖动和捏合用的是 pointer 事件：鼠标、触控笔、手指一套写法。两个手指按着就是捏合
const touches = new Map();
let gesture = null, moved = false;

function beginGesture() {
  const points = [...touches.values()];
  viewerStage.classList.toggle('live', points.length > 0);
  viewerStage.classList.toggle('dragging', points.length === 1 && viewerStage.classList.contains('zoomed'));
  if (!points.length) { gesture = null; return; }
  // 起点要另外记一份：points 里的是跟着手指一起变的那一个，拿它当起点，算出来的位移永远是 0
  const center = points.length > 1 ? { x: (points[0].x + points[1].x) / 2, y: (points[0].y + points[1].y) / 2 } : { x: points[0].x, y: points[0].y };
  const distance = points.length > 1 ? Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y) : 0;
  gesture = { center, distance, scale: view.scale, x: view.x, y: view.y, box: viewerStage.getBoundingClientRect() };
}

viewerStage.addEventListener('pointerdown', (press) => {
  moved = false;
  if (viewerImage.hidden || !view.width || press.target.closest('.viewer-nav')) return;
  if (press.pointerType === 'mouse' && press.button !== 0) return;
  viewerStage.setPointerCapture(press.pointerId);
  touches.set(press.pointerId, { x: press.clientX, y: press.clientY, startX: press.clientX, startY: press.clientY });
  beginGesture();
});
viewerStage.addEventListener('pointermove', (move) => {
  if (!gesture || !touches.has(move.pointerId)) return;
  Object.assign(touches.get(move.pointerId), { x: move.clientX, y: move.clientY });
  const points = [...touches.values()];
  if (points.length > 1 && gesture.distance) {
    const center = { x: (points[0].x + points[1].x) / 2, y: (points[0].y + points[1].y) / 2 };
    const scale = Math.min(maxScale(), Math.max(minScale(), gesture.scale * Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y) / gesture.distance));
    const px = gesture.center.x - gesture.box.left - gesture.box.width / 2, py = gesture.center.y - gesture.box.top - gesture.box.height / 2;
    const ratio = scale / gesture.scale;
    view.scale = scale;
    view.x = px - (px - gesture.x) * ratio + (center.x - gesture.center.x);
    view.y = py - (py - gesture.y) * ratio + (center.y - gesture.center.y);
    moved = true;
  } else {
    const dx = move.clientX - gesture.center.x, dy = move.clientY - gesture.center.y;
    if (Math.hypot(dx, dy) > 4) moved = true;
    view.x = gesture.x + dx;
    view.y = gesture.y + dy;
  }
  applyView(false);
});
function endPointer(release) {
  const point = touches.get(release.pointerId);
  if (!point) return;
  touches.delete(release.pointerId);
  const pinched = gesture && gesture.distance;
  // 没放大的时候，用一个手指左右划：翻到上一个、下一个（手机上最顺手的翻法）
  const dx = point.x - point.startX, dy = point.y - point.startY;
  if (!touches.size && !pinched && release.pointerType !== 'mouse' && !viewerStage.classList.contains('zoomed') && Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) {
    step(dx < 0 ? 1 : -1);
  }
  beginGesture();
  if (pinched && touches.size < 2) sayZoom();
}
viewerStage.addEventListener('pointerup', endPointer);
viewerStage.addEventListener('pointercancel', endPointer);

// 滚轮：⌘/Ctrl 加滚轮，或者触控板上的捏合（浏览器把它报成按着 Ctrl 的滚轮）是缩放；放大以后，普通的滚轮是平移
viewerStage.addEventListener('wheel', (wheel) => {
  if (viewerImage.hidden || !view.width) return;
  wheel.preventDefault();  // 不让浏览器把整个页面缩放或滚动
  const unit = wheel.deltaMode === 1 ? 16 : wheel.deltaMode === 2 ? viewerStage.clientHeight : 1;
  if (wheel.ctrlKey || wheel.metaKey) {
    zoomAt(view.scale * Math.exp(-wheel.deltaY * unit * 0.0075), fromCenter(wheel), false);
  } else if (viewerStage.classList.contains('zoomed')) {
    view.x -= wheel.deltaX * unit;
    view.y -= wheel.deltaY * unit;
    applyView(false);
  }
}, { passive: false });

viewerImage.addEventListener('dblclick', (click) => toggleZoom(fromCenter(click)));
// 点图片外面的空白处关掉（拖动结束时松开的那一下不算）
viewerStage.addEventListener('click', (click) => {
  if (moved) { moved = false; return; }
  if (click.target === viewerStage) viewer.close();
});

window.addEventListener('resize', () => {
  if (!viewer.open || viewerImage.hidden || !view.width) return;
  const fitted = Math.abs(view.scale - view.fit) < 0.001;
  view.fit = fitScale();
  if (fitted) view.scale = view.fit;
  applyView(false);
});

async function showText(item, mine) {
  viewerText.hidden = false;
  viewerText.scrollTop = 0;
  viewerText.classList.add('waiting');
  retext(viewerText, k('读取中…'));
  try {
    const got = item.local && typeof item.local.text === 'string' ? { text: item.local.text } : await fetchItem(item);
    if (mine !== lookSeq) return;
    if (typeof got.text !== 'string') got.text = await got.blob.text();
    if (mine !== lookSeq) return;
    delete viewerText.dataset.i18n;
    viewerText.classList.remove('waiting');
    viewerText.replaceChildren();
    if (item.kind === 'document' && !item.original) {
      viewerText.appendChild(tel('span', 'viewer-banner', k('原文件没有保存下来（这条消息发在有多媒体库之前），这里是当时从里面取出的文字。下载得到的也是这些文字。')));
    }
    viewerText.appendChild(document.createTextNode(got.text));  // 文字原样放进去，里面写着 HTML 也只是文字
    looking.text = got.text;
    drawViewerChrome();
  } catch (problem) {
    if (mine !== lookSeq) return;
    viewerText.hidden = true;
    showCard(item, problem.message || String(problem));
  }
}

// PDF 和 Office 文件（以及取不到内容的）：一张说明卡片，下面是下载和定位
function showCard(item, problem) {
  const nodes = [mediaBadge(item), el('b', null, item.name), el('p', null, [kindLabel(item), sizeText(item)].join(' · '))];
  if (problem) nodes.push(el('p', null, problem));
  else if (item.kind === 'pdf') nodes.push(tel('p', null, k('页面的安全策略不允许在这里打开 PDF。下载以后，用系统自带的阅读器查看。')));
  else if (item.kind === 'document') nodes.push(tel('p', null, k('Word、Excel 和 PowerPoint 文件要下载以后，用对应的软件打开。')));
  const row = el('div', 'row');
  if (item.available !== false && !problem) {
    const save = row.appendChild(el('button', 'button primary'));
    save.type = 'button';
    save.append(icon('download'), tel('span', null, k('下载')));
    save.addEventListener('click', () => downloadItem(item, save));
  }
  if (item.conversation) {
    const go = row.appendChild(el('button', 'button'));
    go.type = 'button';
    go.append(icon('locate'), tel('span', null, k('定位到会话')));
    go.addEventListener('click', () => locateItem(item));
  }
  if (row.childElementCount) nodes.push(row);
  viewerCard.replaceChildren(...nodes);
  viewerCard.hidden = false;
}

// 向助手程序要一个附件的内容。还没发出去的，内容就在手里
async function fetchItem(item) {
  if (item.local) {
    if (typeof item.local.text === 'string') return { blob: new Blob([item.local.text], { type: 'text/plain' }), name: item.name };
    return { blob: blobFrom(item.local.data, item.media_type), name: item.download || item.name };
  }
  const key = item.shot ? 'shot:' + item.shot : item.id;  // 截图的大图另有接口：浏览器的、电脑的各一个（见那两节）
  if (fetched.has(key)) return fetched.get(key);
  let response;
  try {
    response = await fetch(item.shot ? (item.source === 'computer' ? '/api/computer/shot' : '/api/browser/shot') : '/api/media/file', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Harness-Token': token },
      body: JSON.stringify({ id: item.shot || item.id }),
    });
  } catch (_) {
    throw new Error(t('连接不上助手程序'));
  }
  if (!response.ok) {
    const reply = await response.json().catch(() => ({}));
    if (response.status === 403 && !reply.error) throw new Error(t('链接无效或已经过期。请使用终端里显示的完整地址重新打开。'));
    throw new Error(reply.error || t('请求失败（{status}）', { status: response.status }));
  }
  const blob = await response.blob();
  // 文件名以助手程序给的为准（比如旧会话里的 Word 下载下来是 .docx.txt）
  let name = item.download || item.name;
  const given = /filename\*=UTF-8''([^;]+)/.exec(response.headers.get('Content-Disposition') || '');
  if (given) { try { name = decodeURIComponent(given[1]); } catch (_) { /* 编码不对就用原来的名字 */ } }
  const got = { blob, name };
  fetched.set(key, got);
  while (fetched.size > 6) fetched.delete(fetched.keys().next().value);  // 只留最近的几个，大图很占内存
  return got;
}

function blobFrom(base64, type) {
  const raw = atob(base64);
  const bytes = new Uint8Array(raw.length);
  for (let index = 0; index < raw.length; index += 1) bytes[index] = raw.charCodeAt(index);
  return new Blob([bytes], { type });
}

// 存成文件：做一个指向这份内容的 blob: 地址，点一下带 download 的链接。查看器开着的时候链接放在查看器里面：
// 模态对话框外面的东西都是"惰性"的（inert），放在外面的话这一下可能点不动
function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = el('a');
  link.href = url;
  link.download = name;
  link.hidden = true;
  (viewer.open ? viewer : body).appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);  // 浏览器开始存了就用不着了；留一分钟，慢的电脑也来得及
}

async function downloadItem(item, button) {
  if (!item || item.available === false) return;
  if (button) button.disabled = true;
  try {
    const got = await fetchItem(item);
    saveBlob(got.blob, got.name || item.name);
    announce(t('开始下载 {name}', { name: got.name || item.name }));
  } catch (problem) {
    notify(problem.message || String(problem));
  } finally {
    if (button) button.disabled = false;
  }
}

function flashDone(button, iconName) {
  button.classList.add('done');
  button.replaceChildren(icon('check'));
  setTimeout(() => { button.classList.remove('done'); button.replaceChildren(icon(iconName)); }, 1400);
}

// 复制图片：剪贴板只收 PNG，所以把正在看的这张画到画布上、存成 PNG 再放进去
async function copyItem(button) {
  const item = viewerItem();
  if (!item) return;
  try {
    if (viewKind(item) === 'text') {
      await navigator.clipboard.writeText(looking.text || '');
      announce(t('已复制文字'));
    } else {
      if (!navigator.clipboard || !navigator.clipboard.write || !window.ClipboardItem) { notify(t('这个浏览器不支持复制图片。')); return; }
      const canvas = drawScaled(viewerImage, view.width, view.height, false);
      // 交给剪贴板的是一个"还在生成的 PNG"：生成要一点时间，先把这一下点击算在剪贴板头上，浏览器才肯放行
      await navigator.clipboard.write([new ClipboardItem({ 'image/png': toBlob(canvas, 'image/png') })]);
      announce(t('已复制图片'));
    }
    flashDone(button, 'copy');
  } catch (_) {
    notify(t('复制失败：浏览器没有允许访问剪贴板。'));
  }
}

async function revealItem(item) {
  if (!item || !item.id) return;
  const reply = await call('/api/media/reveal', { id: item.id });
  if (!reply.ok) { notify(reply.error); return; }
  notify(revealedText(reply.data.path));
}

// 定位到会话：打开这个附件所在的会话，滚到那一排附件、让那一张亮一下（见 reveal）。
// 正在进行一轮的时候不能换会话（和点会话列表一样）；就是当前这一段的话，直接滚过去
async function locateItem(item) {
  if (!item || !item.conversation) return;
  const target = { id: item.conversation, media: [item.turn, item.index] };
  if (state && item.conversation === state.conversation) {
    if (looking) looking.locating = true;
    if (viewer.open) viewer.close();
    if (overlayNav.matches) setNav(false, false);
    reveal(target);
    return;
  }
  if (busy) { notify(t(BUSY_HINT)); return; }
  // 先关上查看器再去打开会话：打开以后对话区重画、那一张卡片要拿到焦点，查看器还开着的话它外面的东西都是惰性的，焦点放不过去
  if (looking) looking.locating = true;
  if (viewer.open) viewer.close();
  if (overlayNav.matches) setNav(false, false);  // 窄屏上会话栏盖着对话，跳过去以后收起来
  jump = target;  // 打开之后滚到那一张（见 settle）
  const reply = await call('/api/conversation/open', { id: item.conversation });
  if (!reply.ok) { jump = null; toast(reply.error); }
}

async function step(delta) {
  if (!looking) return;
  const next = looking.at + delta;
  if (next >= looking.list.length && looking.more && looking.more.ready()) await looking.more();  // 多媒体库：取下一页再翻
  if (!looking || next < 0 || next >= looking.list.length) return;
  looking.at = next;
  showItem(delta);
}

$('viewer-prev').addEventListener('click', () => step(-1));
$('viewer-next').addEventListener('click', () => step(1));
$('viewer-in').addEventListener('click', () => zoomStep(true));
$('viewer-out').addEventListener('click', () => zoomStep(false));
$('viewer-level').addEventListener('click', () => toggleZoom(null));
$('viewer-copy').addEventListener('click', () => copyItem($('viewer-copy')));
$('viewer-download').addEventListener('click', () => downloadItem(viewerItem(), $('viewer-download')));
$('viewer-reveal').addEventListener('click', () => revealItem(viewerItem()));
$('viewer-locate').addEventListener('click', () => locateItem(viewerItem()));
$('close-viewer').addEventListener('click', () => viewer.close());

viewer.addEventListener('keydown', (key) => {
  if (key.key === 'ArrowLeft' || key.key === 'ArrowRight') {
    key.preventDefault();
    step(key.key === 'ArrowRight' ? 1 : -1);
    return;
  }
  if (viewerImage.hidden || !view.width) return;  // 下面这几个键只管图片；看文字时 ⌘+ 照常放大整个页面
  if (key.key === '+' || key.key === '=') { key.preventDefault(); zoomStep(true); }
  else if (key.key === '-' || key.key === '_' || key.key === '−') { key.preventDefault(); zoomStep(false); }
  else if (key.key === '0') { key.preventDefault(); toggleZoom(null); }
});

viewer.addEventListener('close', () => {
  lookSeq += 1;  // 还在路上的内容不要了
  clearTimeout(zoomSayTimer);
  viewerImage.removeAttribute('src');  // 放开图片和文字占的内存
  viewerText.textContent = '';
  viewerCard.replaceChildren();
  clearTimeout(viewerSayTimer);
  $('viewer-status').hidden = true;
  fetched = new Map();
  touches.clear();
  gesture = null;
  const was = looking;
  looking = null;
  if (!was || was.locating) return;  // 定位到会话：焦点交给对话里的那张卡片
  // 焦点回到点开它的那一项。那一项要是在这期间重画过（多媒体库刷新了），换成新画出来的那一个
  const item = was.list[was.at];
  const again = item && item.id ? $('media-list').querySelector('[data-media="' + CSS.escape(item.id) + '"]') : null;
  const opener = was.opener && was.opener.isConnected ? was.opener : again;
  if (opener) opener.focus({ preventScroll: true });
});
