// ---- 附件 ----------------------------------------------------------------
// 图片（截图可以直接粘贴）、PDF、Word / Excel / PowerPoint 和文本文件可以跟着一条消息发给模型。文件在浏览器里
// 先读好：图片太大的缩小一些、再做一张小缩略图，然后和消息一起发给助手程序。助手程序会再检查一遍（大小、格式、
// 当前的模型看不看得了），所以这里的检查只是为了早点告诉用户，不是唯一的关卡。
//
// 为什么要在这里缩小图片：Claude 看图片时，长边最多用到 2576 像素，更大的会被它自己缩小，
// 多传的那些像素只会让请求变大、让每一轮都多重发一遍。按 28×28 像素算一个 token，一张图最多约 4800 个。
//
// 各种文件怎么处理（四家模型的接口只收几种图片、PDF 和文字，别的都要先变成这几样）：
//   PNG、JPEG、WebP        原样发（太大的缩小）
//   GIF、BMP、HEIC、AVIF…  浏览器能打开的，画到画布上重新存成 PNG 或 JPEG
//   SVG                    画成 PNG；当前的模型看不了图的话，把它的源码当文本发
//   PDF                    原样发，Claude 和 GPT 能读
//   Word、Excel、PPT       原样发给助手程序，由它取出文字（documents.py），哪一家都能读
//   文本、代码             读成文字。不认识的扩展名看内容：是文字就收
//   其余（压缩包、音视频、旧版 Office…）  不收，告诉用户可以怎么转换

const ATTACH_LIMIT = 10;
const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/webp'];  // 不用重新存、每一家都收的图片格式
const IMAGE_BYTES = 7.5e6, PDF_BYTES = 25e6, DOCUMENT_BYTES = 25e6, TEXT_CHARS = 400000;  // 和助手程序那边的上限一致
const IMAGE_EDGE = 2576, THUMB_EDGE = 240, PREVIEW_EDGE = 1280;  // 发给模型的、列表里的、点开看的
const SVG_EDGE = 1568;  // SVG 画成多大：矢量图放大不会糊，小图标也画到这么大，模型才看得清
const TEXT_FILE = /\.(txt|md|markdown|csv|tsv|json|jsonl|xml|ya?ml|toml|ini|cfg|conf|log|py|js|mjs|cjs|ts|tsx|jsx|html?|css|scss|sh|bash|zsh|c|h|cc|cpp|hpp|java|kt|go|rs|rb|php|swift|sql|r|lua|pl|tex|srt|vtt|rtf|env\.example)$/i;
// 按扩展名认文件：浏览器给的类型（file.type）常常是空的，或者只是笼统的 application/octet-stream
const FAMILY = {
  document: /^(docx|docm|dotx|xlsx|xlsm|xltx|pptx|pptm|potx|ppsx)$/,
  image: /^(png|jpe?g|gif|webp|bmp|ico|avif|heic|heif|tiff?)$/,
  office: /^(doc|dot|xls|xlt|ppt|pps|pot|odt|ods|odp|pages|numbers|key|wps|wpt|et|dps|epub|mobi)$/,
  design: /^(psd|ai|sketch|fig|xd|cdr|eps|indd|dng|cr2|cr3|nef|arw|raf|orf|rw2)$/,
  archive: /^(zip|rar|7z|tar|gz|tgz|bz2|xz|zst|dmg|iso)$/,
  media: /^(mp3|wav|m4a|aac|flac|ogg|oga|opus|wma|aiff?|amr|mp4|m4v|mov|avi|mkv|webm|wmv|flv|3gp)$/,
};
const REFUSED = new Set(['office', 'design', 'archive', 'media', 'binary']);
const THUMB_URL = /^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$/;
const tray = $('tray'), fileInput = $('file-input');
let pending = [];   // 还没发出去的附件：{id, kind, name, media_type, size, data | text, thumb}
let attachSeq = 0;

const extension = (name) => ((/\.([^./\\]+)$/.exec(name || '') || [])[1] || '').toLowerCase();

function classify(file) {
  const ext = extension(file.name), type = file.type || '';
  if (type === 'image/svg+xml' || ext === 'svg') return 'svg';
  if (type === 'application/pdf' || ext === 'pdf') return 'pdf';
  if (FAMILY.document.test(ext)) return 'document';
  if (FAMILY.office.test(ext)) return 'office';
  if (FAMILY.design.test(ext)) return 'design';
  if (FAMILY.image.test(ext) || type.startsWith('image/')) return 'image';
  if (FAMILY.archive.test(ext) || /zip|rar|7z|x-tar|gzip|compressed/.test(type)) return 'archive';
  if (FAMILY.media.test(ext) || /^(audio|video)\//.test(type)) return 'media';
  if (type.startsWith('text/') || /json|xml|yaml|toml|javascript|x-sh/.test(type) || TEXT_FILE.test(file.name || '')) return 'text';
  return 'unknown';  // 看了内容再说：没有扩展名的 Makefile、.gitignore 这类其实是文本
}

// 读不了的文件，说清楚为什么、可以怎么办
function refusal(family, name) {
  const format = extension(name).toUpperCase();
  if (family === 'office') return t('读不了 {name}：模型打不开 {format} 格式。可以在原来的软件里另存为 PDF、Word（.docx）或 Excel（.xlsx）再发。', { name, format });
  if (family === 'design') return t('读不了 {name}：浏览器打不开 {format} 格式的图片。可以先另存为 PNG 或 JPEG，或者截个图再发。', { name, format });
  if (family === 'archive') return t('读不了压缩包 {name}。解压之后，把需要的文件直接发过来。', { name });
  if (family === 'media') return t('还读不了音频和视频：{name}。可以把文字稿或者截图发过来。', { name });
  return t('读不了这种文件：{name}。可以发图片、PDF、Word、Excel、PowerPoint 或文本文件。', { name });
}

function formatSize(bytes) {
  const units = [[1e6, ' MB'], [1e3, ' KB']];
  for (const [unit, label] of units) {
    if (bytes >= unit) return (bytes / unit).toLocaleString(LOCALES[language], { maximumFractionDigits: 1 }) + label;
  }
  return bytes.toLocaleString(LOCALES[language]) + ' B';
}

const currentProvider = () => state && state.providers.find((provider) => provider.id === state.provider);
const sees = () => { const current = currentProvider(); return !current || current.vision !== false; };
// 这个文件最后会作为哪一种附件发出去
const sendsAs = (family) => (family === 'svg' ? (sees() ? 'image' : 'text') : family === 'unknown' ? 'text' : family);

// 当前的模型能不能收这种附件。助手程序是旧版本、状态里没有这两项时，交给它自己去判断。
// 这两句和助手程序那边（attachments.unsupported）是同一句话，翻译表里只有一份
function canTake(kind) {
  const current = currentProvider();
  if (!current) return '';
  if (kind === 'image' && current.vision === false) return t('当前的模型（{provider}）看不了图片。可以在设置里换成 Claude、GPT 或 Gemini。', { provider: current.label });
  if (kind === 'pdf' && current.pdf === false) return t('当前的模型（{provider}）读不了 PDF。可以在设置里换成 Claude 或 GPT，或者把需要的文字复制进来。', { provider: current.label });
  return '';
}

const readDataUrl = (blob) => new Promise((done, fail) => {
  const reader = new FileReader();
  reader.onload = () => done(reader.result);
  reader.onerror = () => fail(reader.error);
  reader.readAsDataURL(blob);
});
const base64Of = async (blob) => String(await readDataUrl(blob)).split(',')[1] || '';
const toBlob = (canvas, type, quality) => new Promise((done) => canvas.toBlob(done, type, quality));

// 画到一张指定大小的画布上。存成 JPEG 时先铺一层白底，否则透明的地方会变成黑色
function drawScaled(source, width, height, opaque) {
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(width));
  canvas.height = Math.max(1, Math.round(height));
  const context = canvas.getContext('2d');
  if (opaque) { context.fillStyle = '#ffffff'; context.fillRect(0, 0, canvas.width, canvas.height); }
  context.drawImage(source, 0, 0, canvas.width, canvas.height);
  return canvas;
}

async function readImage(file, name) {
  let bitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch (_) {
    // 每个浏览器都打得开这几种，打不开就是文件坏了；别的（HEIC、TIFF 等）要看浏览器支不支持
    if (/^(png|jpe?g|gif|webp|bmp)$/.test(extension(name)) || IMAGE_TYPES.includes(file.type)) throw new Error(t('读不了这张图片：{name}。文件可能损坏了。', { name }));
    throw new Error(t('读不了 {name}：浏览器打不开 {format} 格式的图片。可以先另存为 PNG 或 JPEG，或者截个图再发。', { name, format: extension(name).toUpperCase() || file.type }));
  }
  try {
    // 照片格式重新存的时候用 JPEG（存成 PNG 会大好几倍）
    const photo = file.type === 'image/jpeg' || /^(heic|heif|avif|tiff?)$/.test(extension(name));
    return await encodeImage(bitmap, bitmap.width, bitmap.height, { file, name, photo });
  } finally {
    bitmap.close();
  }
}

// 把一张已经解码的图片（位图，或者画好的 SVG）变成要发出去的样子，外加缩略图和点开看的大图
async function encodeImage(source, width, height, { file, name, photo, flatten }) {
  const scale = Math.min(1, IMAGE_EDGE / Math.max(width, height));
  let blob = file, type = file && file.type;
  // 尺寸合适、格式也是每一家都收的，就原样发；否则画到画布上重新存一份。截图存 PNG（文字清楚），照片存 JPEG。
  // GIF 也重新存：GPT 不收动图，Gemini 不收 GIF，Claude 只看第一帧，存成 PNG 以后哪一家都能看（看到的都是第一帧）
  if (!file || scale < 1 || file.size > IMAGE_BYTES || !IMAGE_TYPES.includes(file.type)) {
    type = photo ? 'image/jpeg' : 'image/png';
    blob = await toBlob(drawScaled(source, width * scale, height * scale, flatten || photo), type, .9);
    if (!blob || blob.size > IMAGE_BYTES) {
      type = 'image/jpeg';
      blob = await toBlob(drawScaled(source, width * scale, height * scale, true), type, .85);
    }
    if (!blob || blob.size > IMAGE_BYTES) throw new Error(t('{name} 太大了：图片最大约 7 MB。', { name }));
  }
  const thumbScale = Math.min(1, THUMB_EDGE / Math.max(width, height));
  const thumb = drawScaled(source, width * thumbScale, height * thumbScale, true).toDataURL('image/jpeg', .8);
  // 点开看的大图。完整的原图只进发给模型的历史，页面以后拿不到，所以另存一张够看清楚的
  const previewScale = Math.min(1, PREVIEW_EDGE / Math.max(width, height));
  const preview = drawScaled(source, width * previewScale, height * previewScale, true).toDataURL('image/jpeg', .85);
  return { kind: 'image', media_type: type, size: blob.size, data: await base64Of(blob), thumb, preview };
}

// SVG 是用文字写的矢量图，模型的接口不收。看得了图的模型，先画成 PNG 再发；看不了的（比如 DeepSeek），
// 把源码当文本发过去，模型读得懂源码，大致知道画的是什么。画不出来（文件写错了）也退回去发源码。
// 浏览器不能直接把 SVG 文件解码成位图，要先放进 <img>：放在 <img> 里的 SVG 不运行脚本，也不加载外部的东西
async function readSvg(file, name) {
  const source = await readText(file, name, false);
  if (!sees()) return source;
  try { return await drawSvg(source.text, name); } catch (_) { return source; }
}

async function drawSvg(text, name) {
  const parsed = new DOMParser().parseFromString(text, 'image/svg+xml');
  const root = parsed.documentElement;
  if (!root || root.localName !== 'svg' || parsed.getElementsByTagName('parsererror').length) throw new Error('not an svg');
  // 按 viewBox（没有的话按写明的宽高）的比例，长边画到 SVG_EDGE。只写了宽高、没写 viewBox 的，补上 viewBox，
  // 不然把宽高改大以后，里面的图形还是原来那么小
  const length = (value) => (/^\s*[\d.]+(px)?\s*$/.test(value || '') ? parseFloat(value) : 0);
  const box = (root.getAttribute('viewBox') || '').trim().split(/[\s,]+/).map(Number);
  let width = length(root.getAttribute('width')), height = length(root.getAttribute('height'));
  if (box.length === 4 && box[2] > 0 && box[3] > 0) [width, height] = [box[2], box[3]];
  else if (width > 0 && height > 0) root.setAttribute('viewBox', `0 0 ${width} ${height}`);
  else [width, height] = [300, 150];  // 什么都没写：浏览器默认的大小
  const scale = SVG_EDGE / Math.max(width, height);
  width = Math.max(1, Math.round(width * scale));
  height = Math.max(1, Math.round(height * scale));
  root.setAttribute('width', width);
  root.setAttribute('height', height);
  const image = new Image();
  image.src = await readDataUrl(new Blob([new XMLSerializer().serializeToString(parsed)], { type: 'image/svg+xml' }));
  await image.decode();
  return encodeImage(image, width, height, { name, flatten: true });  // 铺白底：透明的地方有的模型会当成黑色
}

// 文本文件不一定是 UTF-8：Windows 的"记事本"常存成 UTF-16，老一些的中文文件常是 GBK（GB18030 包含它）。
// 依次试：带 BOM 的 UTF-16 → UTF-8 → GB18030。fatal：遇到不合法的字节就算失败，不悄悄变成乱码。
// partial：只读了开头一段，最后一个字可能被截断，不算错
function decodeText(bytes, partial) {
  if (bytes[0] === 0xFF && bytes[1] === 0xFE) return new TextDecoder('utf-16le').decode(bytes);
  if (bytes[0] === 0xFE && bytes[1] === 0xFF) return new TextDecoder('utf-16be').decode(bytes);
  if (bytes.includes(0)) return null;  // 文字里不会有 0 字节，有就是别的格式
  for (const encoding of ['utf-8', 'gb18030']) {
    try { return new TextDecoder(encoding, { fatal: true }).decode(bytes, { stream: !!partial }); } catch (_) { /* 换下一种 */ }
  }
  return null;
}

// sniffing：扩展名不认识，靠内容判断。要求更严一些：控制字符多的不算文字
async function readText(file, name, sniffing) {
  const head = decodeText(new Uint8Array(await file.slice(0, 65536).arrayBuffer()), true);
  const controls = head === null ? 0 : (head.match(/[\u0000-\u0008\u000E-\u001A\u001C-\u001F\u007F]/g) || []).length;
  if (head === null || (sniffing && controls > head.length / 100)) throw new Error(refusal('binary', name));
  if (file.size > TEXT_CHARS * 4) throw new Error(t('{name} 太长了：文本文件最多 40 万字。', { name }));  // 一个字最多 4 个字节
  const text = decodeText(new Uint8Array(await file.arrayBuffer()), false);
  if (text === null) throw new Error(refusal('binary', name));
  if (text.length > TEXT_CHARS) throw new Error(t('{name} 太长了：文本文件最多 40 万字。', { name }));
  return { kind: 'text', media_type: file.type || 'text/plain', size: text.length, text };
}

async function readAttachment(file, family) {
  const name = file.name || t('截图');
  if (family === 'image') return { name, ...(await readImage(file, name)) };
  if (family === 'svg') return { name, ...(await readSvg(file, name)) };
  if (family === 'pdf') {
    if (file.size > PDF_BYTES) throw new Error(t('{name} 太大了：PDF 最大 25 MB。', { name }));
    return { name, kind: 'pdf', media_type: 'application/pdf', size: file.size, data: await base64Of(file) };
  }
  if (family === 'document') {
    if (file.size > DOCUMENT_BYTES) throw new Error(t('{name} 太大了：Word、Excel、PowerPoint 文件最大 25 MB。', { name }));
    return { name, kind: 'document', media_type: file.type || 'application/octet-stream', size: file.size, data: await base64Of(file) };
  }
  return { name, ...(await readText(file, name, family === 'unknown')) };
}

// 一个个读进来。读的时候先摆一张"读取中"的卡片，读完换成真的；出错的那个说一声，不影响别的。
// 一次拖进好几个读不了的文件时，说明攒在一起最后一次显示，不然一条盖掉一条，只看得到最后一条
async function addFiles(files) {
  const problems = [];
  const say = (text) => { if (!problems.includes(text)) problems.push(text); };
  for (const file of files) {
    if (pending.length >= ATTACH_LIMIT) { say(t('一条消息最多带 {n} 个附件。', { n: ATTACH_LIMIT })); break; }
    const family = classify(file), name = file.name || t('截图');
    if (REFUSED.has(family)) { say(refusal(family, name)); continue; }
    const refused = canTake(sendsAs(family));
    if (refused) { say(refused); continue; }
    const item = { id: ++attachSeq, name, kind: sendsAs(family), loading: true };
    pending.push(item);
    renderTray(item.id);
    try {
      Object.assign(item, await readAttachment(file, family), { loading: false });
    } catch (problem) {
      pending = pending.filter((other) => other !== item);
      say(problem.message || String(problem));
    }
    renderTray();
  }
  if (problems.length) toast(problems.join('\n'));
  refreshStatus();
}

const OFFICE_LABEL = { docx: 'Word', docm: 'Word', dotx: 'Word', xlsx: 'Excel', xlsm: 'Excel', xltx: 'Excel', pptx: 'PowerPoint', pptm: 'PowerPoint', potx: 'PowerPoint', ppsx: 'PowerPoint' };

// 卡片上的第二行：类型和大小。文本附件记的是字数，不是字节数；Word 这些发出去以后，记的也是取出来的字数
function sizeLabel(item) {
  if (item.loading) return tel('span', 'att-size', k('读取中…'));
  const office = OFFICE_LABEL[extension(item.name)];
  if (item.kind === 'pdf') return el('span', 'att-size', 'PDF · ' + formatSize(item.size));
  if (item.kind === 'document') return el('span', 'att-size', (office || 'Office') + ' · ' + formatSize(item.size));
  if (item.kind === 'text') {
    const n = Number(item.size).toLocaleString(LOCALES[language]);
    return office ? tel('span', 'att-size', k('{type} · {n} 字'), { type: office, n }) : tel('span', 'att-size', k('{n} 字'), { n });
  }
  return el('span', 'att-size', formatSize(item.size));
}

// 一张附件卡片。open：点开它的时候做什么（打开查看器）；没有的话卡片只是显示，不能点
function attachmentCard(item, removable, open) {
  const picture = item.kind === 'image' && item.thumb && THUMB_URL.test(item.thumb);
  const card = el('div', 'att ' + (picture ? 'image' : 'file') + (item.loading ? ' busy-att' : '') + (open && !picture ? ' openable' : ''));
  card.title = item.name;
  if (picture) {
    const button = card.appendChild(el('button', 'att-open'));
    button.type = 'button';
    tattr(button, 'title', k('点击查看大图'));
    const image = button.appendChild(el('img'));
    image.alt = item.name;
    image.src = item.thumb;  // 只接受 data: 开头的图片（页面的安全策略也只允许这种）
    button.appendChild(el('span', 'sr-only', item.name));  // 文件名：读屏软件读它，搜索跳转也靠它找到这张图
    if (open) button.addEventListener('click', () => open(button));
    else button.disabled = true;
  } else {
    // 文件卡片：能点开的（对话里的 PDF、Word、文本……）整张卡片就是一个按钮
    const holder = open ? card.appendChild(el('button', 'att-open att-file')) : card;
    if (open) {
      holder.type = 'button';
      holder.addEventListener('click', () => open(holder));
    }
    holder.appendChild(icon(item.kind === 'image' ? 'image' : 'file'));
    const text = holder.appendChild(el('span', 'att-text'));
    text.appendChild(el('span', 'att-name', item.name));
    text.appendChild(sizeLabel(item));
  }
  if (removable) {
    const remove = card.appendChild(el('button', 'remove'));
    remove.type = 'button';
    remove.setAttribute('aria-label', t('移除 {name}', { name: item.name }));
    remove.appendChild(icon('x'));
    remove.addEventListener('click', () => {
      pending = pending.filter((other) => other.id !== item.id);
      renderTray();
      refreshStatus();
      input.focus();
    });
  }
  return card;
}

// 输入框上方那一排。fresh 是刚加进来的那一个：只有它播入场动画。
// 还没发出去的图片点开看的是手里的原图（这时还没有存进多媒体库，所以不能定位、不能在访达中显示）
function renderTray(fresh) {
  tray.hidden = !pending.length;
  const pictures = () => pending.filter((item) => item.kind === 'image' && item.data && !item.loading).map((item) => ({
    id: '', name: item.name, kind: 'image', size: item.size, media_type: item.media_type, thumb: item.thumb, preview: item.preview,
    local: { data: item.data }, original: true, available: true, download: item.name,
  }));
  tray.replaceChildren(...pending.map((item) => {
    const open = item.kind === 'image' && item.data ? (button) => {
      const list = pictures();
      openViewer(list, Math.max(0, list.findIndex((other) => other.local.data === item.data)), button);
    } : null;
    const card = attachmentCard(item, !item.loading, open);
    if (item.id === fresh) card.classList.add('enter');
    return card;
  }));
}

// 对话里：一条用户消息带的附件，排在消息上方。来自助手程序的记录（可能是旧会话里存下的），每一项都先检查。
// 每一张都能点开，在查看器里左右翻的是这一条消息里的附件。at：这条消息是什么时候发的（旧会话里没有）
const rowItems = new WeakMap();  // 一排附件卡片 → 这一排的附件概要
function attachmentRow(list, at) {
  const row = el('div', 'msg-atts');
  const shown = [];
  for (const item of list) {
    if (!item || typeof item !== 'object') continue;  // 和助手程序那边（attachments.from_saved）一样只数合格的，"第几张"两边才对得上
    const kind = ['image', 'pdf', 'text'].includes(item.kind) ? item.kind : 'text';
    const thumb = typeof item.thumb === 'string' ? item.thumb : '', preview = typeof item.preview === 'string' ? item.preview : '';
    const summary = { kind, name: String(item.name || ''), size: Number(item.size) || 0, thumb, preview, sha256: typeof item.sha256 === 'string' ? item.sha256 : '' };
    const index = shown.length;
    shown.push(summary);
    row.appendChild(attachmentCard(summary, false, (button) => openViewer(messageItems(row), index, button)));
  }
  rowItems.set(row, { shown, at: typeof at === 'number' && Number.isFinite(at) ? at : 0 });
  return row;
}

// 一排附件卡片在查看器里的样子。编号是"会话/第几排/第几张"，和多媒体库里的一样（见 harness/media.py），
// 所以点开以后能向助手程序要原件、能定位、能在访达中显示
function messageItems(row) {
  const { shown, at } = rowItems.get(row) || { shown: [], at: 0 };
  const turn = [...log.querySelectorAll(':scope > .msg-atts')].indexOf(row);
  const conversation = state ? state.conversation : '';
  return shown.map((summary, index) => {
    const office = OFFICE_LABEL[extension(summary.name)];
    const kind = summary.kind === 'text' && office ? 'document' : summary.kind;
    return {
      id: conversation && turn >= 0 ? conversation + '/' + turn + '/' + index : '',
      name: summary.name, kind, size: summary.size, chars: summary.kind === 'text', media_type: '',
      thumb: summary.thumb, preview: summary.preview,
      original: Boolean(summary.sha256), available: true,
      conversation, conversation_title: state && state.title ? state.title : t('新对话'), at, turn, index,
    };
  });
}

$('attach').addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => {
  const files = [...fileInput.files];
  fileInput.value = '';  // 清空，同一个文件删掉后还能再选一次
  addFiles(files);
});
// 粘贴：剪贴板里只有图片（比如刚截的图）就当附件；同时有文字的（从网页、文档里复制的）照常粘贴文字
input.addEventListener('paste', (paste) => {
  const files = [...(paste.clipboardData ? paste.clipboardData.files : [])];
  if (!files.length || paste.clipboardData.getData('text/plain')) return;
  paste.preventDefault();
  addFiles(files);
});
// 拖放：文件拖到页面上任何地方都行。浏览器默认会直接打开拖进来的文件、离开这个页面，这里要拦下来
let dragDepth = 0;
const carriesFiles = (drag) => drag.dataTransfer && [...drag.dataTransfer.types].includes('Files');
window.addEventListener('dragenter', (drag) => {
  if (!carriesFiles(drag)) return;
  drag.preventDefault();
  dragDepth += 1;
  composer.classList.add('dropping');
});
window.addEventListener('dragleave', (drag) => {
  if (!carriesFiles(drag)) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) composer.classList.remove('dropping');
});
window.addEventListener('dragover', (drag) => {
  if (!carriesFiles(drag)) return;
  drag.preventDefault();
  drag.dataTransfer.dropEffect = 'copy';
});
window.addEventListener('drop', (drag) => {
  if (!carriesFiles(drag)) return;
  drag.preventDefault();
  dragDepth = 0;
  composer.classList.remove('dropping');
  // 有对话框开着的时候（比如在"扩展"里上传 Skill），拖进来的文件不是给这条消息的：不当附件
  if (document.querySelector('dialog[open]')) return;
  addFiles([...drag.dataTransfer.files]);
});
