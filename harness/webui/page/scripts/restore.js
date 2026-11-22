// ---- 从备份恢复 ------------------------------------------------------------------
// 设置里"导出全部数据"旁边的"从备份恢复…"打开的窗口。一步一步来（restoreView.step）：
//   pick       选一个备份：点按钮选文件，或者直接拖进窗口
//   uploading  上传。用 XMLHttpRequest：能看到传了多少（fetch 看不到上传的进度），可以取消
//   preview    助手程序看过一遍以后的预览：导出的时间、两种方式（覆盖 / 合并）各会怎样、哪些密钥要重新填
//   confirm    覆盖之前在原地再问一次：它会换掉现在的数据
//   working    正在恢复：这时关不了窗口（停在一半，数据就乱了；助手程序那边要么整个做完，要么换回原样）
//   done       恢复好了：恢复前的自动备份在哪儿（在访达中显示），哪些密钥要去哪儿重新填
//   failed     出错了：原因写在窗口里，可以重新选一个
// 检查压缩包、每一样东西会怎样的那句话，都由助手程序算好（harness/restore.py），页面只照着画。
// 上传了、还没恢复的时候（uploading、preview、confirm），Esc、×、点外面、"取消"都先提醒一次，再来一次才放弃，
// 并请助手程序删掉上传的那一份。提示写在窗口里：浮在页面上的提示会被窗口的暗底挡住。
// 恢复好了以后，助手程序照着新的数据在原地重新开起来（harness/webui/restore_api.py）：页面收到 reset 和新的状态，
// 会话列表、设置、语言都自己跟着变，这个窗口留着告诉你结果。

const restoreDialog = $('restore-dialog');
let restoreView = { step: 'pick' };  // 现在在哪一步，和这一步要的东西（file、stage、preview、mode、result、error……）
let restoreInfo = null;   // /api/restore/info：网页上传最大多大、现在能不能恢复、自动备份放在哪儿
let restoreArmed = false; // 提醒过"再按一次就放弃"
let restoreXhr = null;    // 正在上传的请求（取消时 abort）
const RESTORE_HOLDING = ['uploading', 'preview', 'confirm'];  // 这几步关窗口要先问：上传的那一份还在助手程序那里

function restoreSay(text, tone) {
  const box = $('restore-message');
  if (text && typeof text === 'object') retext(box, text.key, text.values); else { delete box.dataset.i18n; box.textContent = text || ''; }
  box.classList.toggle('bad', tone === 'bad');
}

// 一行带图标的小说明（备份里没有导出时写的说明、现在不能恢复……）。tone 是 warn 或 bad
function restoreNote(key, values, tone, glyph) {
  const box = el('p', 'restore-note ' + (tone || ''));
  box.setAttribute('role', 'note');
  box.appendChild(icon(glyph || 'alert'));
  box.appendChild(tel('span', null, key, values));
  return box;
}

function restoreButton(label, kind, action, glyph) {
  const button = el('button', 'button' + (kind ? ' ' + kind : '') + (glyph ? ' with-icon' : ''));
  button.type = 'button';
  if (glyph) button.appendChild(icon(glyph));
  button.appendChild(tel('span', null, label));
  button.addEventListener('click', action);
  return button;
}

const restoreSize = (bytes) => formatSize(Number(bytes) || 0);

function restoreWhen(iso) {
  const moment = new Date(iso);
  if (Number.isNaN(moment.getTime())) return '';
  return moment.toLocaleString(LOCALES[language], { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

// ---- 画 ----

function drawRestore(focus) {
  const view = restoreView;
  const content = $('restore-body');
  const actions = $('restore-actions');
  const keep = content.contains(document.activeElement) || actions.contains(document.activeElement);
  const painter = { pick: drawRestorePick, uploading: drawRestoreUpload, preview: drawRestorePreview, confirm: drawRestorePreview,
                    working: drawRestoreWorking, done: drawRestoreDone, failed: drawRestoreFailed }[view.step];
  const { nodes, buttons, first } = painter(view);
  content.replaceChildren(...nodes);
  actions.replaceChildren(...buttons);
  $('close-restore').disabled = view.step === 'working';
  restoreDialog.classList.toggle('working', view.step === 'working');
  // 拿着焦点的那个元素刚被换掉了：交给这一步最要紧的那个按钮（不然焦点掉到窗口外面，Esc 会绕过 keydown 直接关窗口）
  if (focus || keep || !restoreDialog.contains(document.activeElement)) (first || $('close-restore')).focus({ preventScroll: true });
}

function drawRestorePick(view) {
  const drop = el('div', 'restore-drop' + (view.over ? ' over' : ''));
  drop.appendChild(el('span', 'restore-glyph')).appendChild(icon('upload'));
  drop.appendChild(tel('b', 'restore-drop-title', k('选一个备份')));
  drop.appendChild(tel('p', 'restore-drop-sub', k('“导出全部数据”存下的 zhifox-backup-….zip。也可以直接把它拖到这里。')));
  const choose = drop.appendChild(restoreButton(k('选择文件…'), 'primary', () => $('restore-file').click(), 'folder'));
  choose.dataset.focus = 'choose';
  const notes = el('ul', 'restore-points');
  for (const key of [k('先给你看备份里有什么、两种方式各会怎样，你确认了才恢复。'),
                     k('恢复之前，会把现在的数据自动备份一份，放在数据文件夹的 restore-backups 里。'),
                     k('现在填好的密钥不会被备份里的占位符盖掉。')]) {
    const item = notes.appendChild(el('li'));
    item.appendChild(icon('check'));
    item.appendChild(tel('span', null, key));
  }
  const nodes = [drop, notes];
  if (restoreInfo && restoreInfo.blocked) nodes.push(restoreNote(k('现在还不能恢复：{reason}'), { reason: restoreInfo.blocked }, 'warn'));
  return { nodes, buttons: [restoreButton(k('取消'), 'cancel', requestCloseRestore)], first: choose };
}

function restoreFileCard(view) {
  const card = el('div', 'restore-file');
  card.appendChild(el('span', 'restore-file-glyph')).appendChild(icon('file'));
  const words = card.appendChild(el('div', 'restore-file-words'));
  const name = words.appendChild(el('b', 'restore-file-name', view.name || (view.file && view.file.name) || 'backup.zip'));
  name.title = name.textContent;
  const preview = view.preview;
  const size = restoreSize(view.size || (view.file && view.file.size));
  const facts = words.appendChild(el('span', 'restore-file-facts'));
  if (preview && preview.exported_at) retext(facts, k('{size} · {time} 导出 · {n} 个文件'), { size, time: restoreWhen(preview.exported_at), n: preview.files.toLocaleString(LOCALES[language]) });
  else if (preview) retext(facts, k('{size} · {n} 个文件'), { size, n: preview.files.toLocaleString(LOCALES[language]) });
  else facts.textContent = size;
  return card;
}

function drawRestoreUpload(view) {
  const card = restoreFileCard(view);
  const bar = el('div', 'restore-bar');
  bar.setAttribute('role', 'progressbar');
  bar.setAttribute('aria-valuemin', '0');
  bar.setAttribute('aria-valuemax', '100');
  bar.setAttribute('aria-valuenow', String(Math.round((view.progress || 0) * 100)));
  tattr(bar, 'aria-label', k('上传备份'));
  const fill = bar.appendChild(el('i'));
  fill.style.transform = 'scaleX(' + (view.progress || 0) + ')';
  const said = el('p', 'restore-progress');
  said.id = 'restore-progress';
  if ((view.progress || 0) >= 1) retext(said, k('上传好了，正在检查压缩包……'));
  else retext(said, k('正在上传：{done} / {total}'), { done: restoreSize((view.progress || 0) * view.file.size), total: restoreSize(view.file.size) });
  const cancel = restoreButton(k('取消'), 'cancel', requestCloseRestore);
  return { nodes: [card, bar, said], buttons: [cancel], first: cancel };
}

// 上传时只改进度条和那一行字，不重画整个窗口（每秒好几次）
function showRestoreProgress(share) {
  restoreView.progress = share;
  const bar = $('restore-body').querySelector('.restore-bar');
  if (!bar) return;
  bar.firstChild.style.transform = 'scaleX(' + share + ')';
  bar.setAttribute('aria-valuenow', String(Math.round(share * 100)));
  const said = $('restore-progress');
  if (share >= 1) retext(said, k('上传好了，正在检查压缩包……'));
  else retext(said, k('正在上传：{done} / {total}'), { done: restoreSize(share * restoreView.file.size), total: restoreSize(restoreView.file.size) });
}

const RESTORE_MODES = [
  { id: 'overwrite', name: k('覆盖恢复'), what: k('数据文件夹变成备份时的样子：会话、记忆、设置……都换成备份里的，备份里没有的清空。') },
  { id: 'merge', name: k('合并恢复'), what: k('只把现在没有的加进来：会话按编号、记忆按文件名，现在有的都留着。设置、待办这类整份的文件也留着现在的。') },
];

function drawRestorePreview(view) {
  const preview = view.preview;
  const nodes = [restoreFileCard(view)];
  if (preview.foreign) nodes.push(restoreNote(k('压缩包里没有导出时写的说明，看不出是什么时候、从哪儿导出的。里面都是智狐认识的东西，所以也能恢复。'), null, 'warn'));
  // 两种方式：一组单选，像两张卡片
  const modes = el('div', 'restore-modes');
  modes.setAttribute('role', 'radiogroup');
  tattr(modes, 'aria-label', k('怎么恢复'));
  for (const mode of RESTORE_MODES) {
    const card = modes.appendChild(el('label', 'restore-mode' + (view.mode === mode.id ? ' on' : '')));
    const radio = card.appendChild(el('input'));
    radio.type = 'radio';
    radio.name = 'restore-mode';
    radio.value = mode.id;
    radio.checked = view.mode === mode.id;
    radio.disabled = view.step === 'confirm';
    radio.dataset.focus = 'mode:' + mode.id;
    const words = card.appendChild(el('span', 'restore-mode-words'));
    words.appendChild(tel('b', null, mode.name));
    words.appendChild(tel('small', null, mode.what));
    radio.addEventListener('change', () => {
      if (!radio.checked) return;
      restoreView.mode = mode.id;
      restoreTouched();
      drawRestore();
      const again = $('restore-body').querySelector('[data-focus="mode:' + mode.id + '"]');
      if (again) again.focus({ preventScroll: true });
    });
  }
  nodes.push(modes);
  const plan = preview.modes[view.mode];
  nodes.push(tel('h3', 'restore-heading', k('会怎样')));
  const list = el('ul', 'restore-steps');
  for (const step of plan.steps) {
    const row = list.appendChild(el('li', 'restore-step ' + step.action));
    row.appendChild(el('span', 'restore-step-mark')).appendChild(icon(step.action === 'clear' ? 'minus' : step.action === 'keep' ? 'lock' : step.action === 'merge' || step.action === 'add' ? 'plus' : 'refresh'));
    row.appendChild(tel('span', 'restore-step-name', step.label));
    const what = row.appendChild(el('span', 'restore-step-what'));
    if (step.said && step.said.key) retext(what, step.said.key, step.said.values); else what.textContent = (step.said && step.said.text) || '';
  }
  if (!plan.steps.length) list.appendChild(tel('li', 'restore-empty', k('没有要改的：备份里的东西现在都已经有了。')));
  nodes.push(list);
  const notRestored = preview.parts.filter((part) => part.restore === false);
  if (notRestored.length) nodes.push(restoreNote(k('备份里的这些不恢复：{names}（这台电脑上的程序自己会写）。'), { names: notRestored.map((part) => t(part.label)).join(t('、')) }, '', 'eye'));
  if (plan.keys.length) {
    nodes.push(tel('h3', 'restore-heading', k('密钥')));
    nodes.push(restoreKeys(plan.keys, false));
  }
  nodes.push(restoreNote(k('恢复之前，会先把现在的数据自动备份一份：{path}'), { path: restoreInfo ? restoreInfo.folder : 'restore-backups' }, '', 'shield'));
  if (view.blocked) nodes.push(restoreNote(k('现在还不能恢复：{reason}'), { reason: view.blocked }, 'warn'));

  let buttons, first;
  if (view.step === 'confirm') {
    const back = restoreButton(k('返回'), 'cancel', () => { restoreView.step = 'preview'; restoreSay(''); drawRestore(true); });
    const sure = restoreButton(k('确定覆盖'), 'danger', applyRestore, 'refresh');
    sure.dataset.focus = 'sure';
    buttons = [back, sure];
    first = back;  // 默认停在"返回"上：按错了 Enter 不会就这样覆盖
  } else {
    const cancel = restoreButton(k('取消'), 'cancel', requestCloseRestore);
    const go = restoreButton(view.mode === 'merge' ? k('合并进来') : k('覆盖现在的数据'), 'primary', () => {
      if (restoreView.mode === 'merge') { applyRestore(); return; }
      restoreView.step = 'confirm';
      restoreArmed = false;
      restoreSay({ key: k('现在的会话、记忆、设置……会换成备份里的。恢复之前会先自动备份一份。确定吗？') }, 'bad');
      drawRestore(true);
    }, view.mode === 'merge' ? 'plus' : 'refresh');
    go.dataset.focus = 'go';
    buttons = [cancel, go];
    first = go;  // 焦点停在要做的那件事上；方式用 Tab 或者方向键换
  }
  return { nodes, buttons, first };
}

// 存着密钥的配置文件：哪几处沿用现在的、哪几处要重新填、哪几处恢复以后就没了。done 为真时是恢复以后的说法
function restoreKeys(keys, done) {
  const list = el('ul', 'restore-keys');
  for (const item of keys) {
    const row = list.appendChild(el('li', 'restore-key ' + item.state));
    const head = row.appendChild(el('div', 'restore-key-head'));
    head.appendChild(icon(item.state === 'broken' ? 'alert' : item.missing.length ? 'key' : 'lock'));
    head.appendChild(el('code', 'restore-key-file', item.file));
    if (item.home) head.appendChild(tel('span', 'restore-key-home', k('在“{place}”里填'), { place: { key: item.home, values: {} } }));
    const lines = row.appendChild(el('div', 'restore-key-lines'));
    if (item.state === 'broken') lines.appendChild(tel('p', null, k('现在的这一份读不懂，没有动它：覆盖了，里面的密钥就再也找不回来了。')));
    if (item.kept) lines.appendChild(tel('p', null, k('{n} 处沿用现在的密钥。'), { n: item.kept }));
    for (const [paths, key] of [[item.missing, done ? k('这几处还是占位符，要重新填：') : k('这几处备份里没有密钥，恢复以后要重新填：')],
                                [item.dropped, k('现在有、备份里没有的这几处密钥，恢复以后就没了（自动备份里也是遮住的）：')]]) {
      if (!paths.length) continue;
      lines.appendChild(tel('p', null, key));
      const where = lines.appendChild(el('ul', 'restore-key-paths'));
      for (const path of paths) where.appendChild(el('li')).appendChild(el('code', null, path));
    }
  }
  return list;
}

function drawRestoreWorking() {
  const box = el('div', 'restore-working');
  box.appendChild(el('span', 'restore-spinner')).appendChild(icon('loop'));
  box.appendChild(tel('b', null, k('正在恢复……')));
  box.appendChild(tel('p', null, k('先把现在的数据备份一份，再换上备份里的。做完之前不要关掉这个页面。')));
  return { nodes: [box], buttons: [], first: null };
}

function drawRestoreDone(view) {
  const result = view.result;
  const box = el('div', 'restore-done');
  box.appendChild(el('span', 'restore-glyph good')).appendChild(icon('check'));
  box.appendChild(tel('b', 'restore-done-title', result.mode === 'merge' ? k('合并好了') : k('恢复好了')));
  box.appendChild(tel('p', null, k('智狐已经照着恢复的数据重新开起来了：会话、记忆、设置都换成了新的。')));
  const nodes = [box];
  let reveal = null;
  if (result.backup) {
    const row = el('div', 'restore-saved');
    const words = row.appendChild(el('div', 'restore-saved-words'));
    words.appendChild(tel('b', null, k('恢复之前的数据')));
    const where = words.appendChild(el('span', 'restore-saved-path', shortPath(result.backup)));
    where.title = result.backup;
    reveal = iconButton('folder', showLabel());
    reveal.dataset.focus = 'reveal';
    reveal.addEventListener('click', () => revealRestoreBackup(result.backup_name));
    row.appendChild(reveal);
    nodes.push(row);
  } else {
    nodes.push(restoreNote(k('恢复之前数据文件夹是空的，所以没有自动备份。'), null, '', 'eye'));
  }
  const keys = result.keys.filter((item) => item.missing.length || item.state === 'broken' || item.dropped.length);
  if (keys.length) {
    nodes.push(tel('h3', 'restore-heading', k('还要做的')));
    nodes.push(restoreKeys(keys, true));
  }
  const done = restoreButton(k('完成'), 'primary', () => restoreDialog.close());
  return { nodes, buttons: [done], first: done };
}

function drawRestoreFailed(view) {
  const nodes = [restoreNote(k('{error}'), { error: view.error }, 'bad', 'alert')];
  if (view.restarted) nodes.push(restoreNote(k('智狐已经照着原来的数据重新开起来了，什么都没有丢。'), null, '', 'shield'));
  const again = restoreButton(k('重新选一个'), 'primary', () => { restoreView = { step: 'pick' }; restoreSay(''); drawRestore(true); });
  return { nodes, buttons: [restoreButton(k('关闭'), 'cancel', () => restoreDialog.close()), again], first: again };
}

// ---- 做 ----

async function openRestore() {
  if (!state) return;
  if (settings.open) settings.close();  // 恢复以后设置里的每一项都可能变了：不叠在设置上面，免得底下是旧的
  restoreView = { step: 'pick' };
  restoreArmed = false;
  restoreSay('');
  delete restoreDialog.dataset.ready;
  drawRestore();
  restoreDialog.showModal();
  drawRestore(true);
  const reply = await call('/api/restore/info');
  restoreInfo = reply.ok ? reply.data : null;
  restoreDialog.dataset.ready = '';  // 知道上传最多多大了（页面测试等它）
  if (restoreDialog.open && restoreView.step === 'pick') drawRestore();
}

function failRestore(error, restarted) {
  restoreView = { step: 'failed', error, restarted: Boolean(restarted) };
  restoreArmed = false;
  restoreSay('');
  drawRestore(true);
  announce(error);
}

function uploadRestore(file) {
  if (!file || !RESTORE_HOLDING.concat(['pick', 'failed']).includes(restoreView.step) || restoreView.step === 'uploading') return;
  const most = restoreInfo ? restoreInfo.max_bytes : 0;
  if (most && file.size > most) {
    failRestore(t('这个文件有 {size}，网页上一次最多上传 {max}。更大的备份可以在终端里用 uv run harness restore 恢复。', { size: restoreSize(file.size), max: restoreSize(most) }));
    return;
  }
  discardRestore();  // 上一次上传了、还没恢复的那一份不要了
  restoreView = { step: 'uploading', file, progress: 0 };
  restoreArmed = false;
  restoreSay('');
  drawRestore(true);
  const xhr = new XMLHttpRequest();
  restoreXhr = xhr;
  xhr.open('POST', '/api/restore/upload');
  xhr.setRequestHeader('X-Harness-Token', token);
  xhr.setRequestHeader('Content-Type', 'application/zip');
  xhr.setRequestHeader('X-Restore-Name', encodeURIComponent(file.name));
  xhr.upload.addEventListener('progress', (event) => { if (restoreXhr === xhr && event.lengthComputable && event.total) showRestoreProgress(event.loaded / event.total); });
  xhr.addEventListener('load', () => {
    if (restoreXhr !== xhr) return;
    restoreXhr = null;
    let reply = {};
    try { reply = JSON.parse(xhr.responseText || '{}'); } catch (_) { reply = {}; }
    if (xhr.status !== 200) {
      failRestore(xhr.status === 403 && !reply.error ? t('链接无效或已经过期。请使用终端里显示的完整地址重新打开。') : reply.error || t('请求失败（{status}）', { status: xhr.status }));
      return;
    }
    restoreView = { step: 'preview', stage: reply.stage, name: reply.name || file.name, size: reply.size, preview: reply.preview, mode: 'overwrite', blocked: reply.blocked || '' };
    restoreArmed = false;
    restoreSay('');
    drawRestore(true);
    play($('restore-body'), [{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }], 240);
  });
  xhr.addEventListener('error', () => { if (restoreXhr === xhr) { restoreXhr = null; failRestore(t('连接不上助手程序')); } });
  xhr.send(file);
}

// 请助手程序删掉上传了、还没恢复的那一份（正在上传的就停下）
function discardRestore() {
  if (restoreXhr) { const xhr = restoreXhr; restoreXhr = null; xhr.abort(); }
  if (restoreView.stage) call('/api/restore/discard', { stage: restoreView.stage });
  restoreView.stage = '';
}

async function applyRestore() {
  const view = restoreView;
  if (!view.stage || view.step === 'working') return;
  const mode = view.mode;
  restoreView = { ...view, step: 'working' };
  restoreArmed = false;
  restoreSay('');
  drawRestore(true);
  const reply = await call('/api/restore/apply', { stage: view.stage, mode, confirm: true });
  if (reply.ok) {
    restoreView = { step: 'done', result: reply.data.result };
    restoreSay('');
    drawRestore(true);
    announce(mode === 'merge' ? t('合并好了') : t('恢复好了'));
    return;
  }
  const data = reply.data || {};
  if (data.restarted || data.gone || !data.error) { failRestore(reply.error, data.restarted); return; }
  // 现在不能恢复（对话、任务、通道在忙）：回到预览，上传的那一份还在，原因写在窗口底下
  restoreView = { ...view, step: 'preview' };
  drawRestore(true);
  restoreSay(reply.error, 'bad');
}

async function revealRestoreBackup(name) {
  const reply = await call('/api/restore/reveal', { name });
  if (!reply.ok) { restoreSay(reply.error, 'bad'); return; }
  restoreSay(revealedText(reply.data.path));
}

function restoreTouched() {
  if (restoreArmed) { restoreArmed = false; restoreSay(''); }
}

function requestCloseRestore() {
  if (restoreView.step === 'working') { restoreSay({ key: k('正在恢复，做完之前不能关。') }, 'bad'); return; }
  if (restoreView.step === 'confirm') { restoreView.step = 'preview'; restoreSay(''); drawRestore(true); return; }  // 原地的确认：先收起它，回到预览
  if (RESTORE_HOLDING.includes(restoreView.step) && !restoreArmed) {
    restoreArmed = true;
    restoreSay({ key: k('还没有恢复。再按一次就放弃，上传的备份也会删掉。') }, 'bad');
    return;
  }
  restoreDialog.close();
}

$('restore-open').addEventListener('click', openRestore);
$('close-restore').addEventListener('click', requestCloseRestore);
$('restore-file').addEventListener('change', () => {
  const input = $('restore-file');
  const file = input.files && input.files[0];
  input.value = '';  // 同一个文件再选一次也要触发 change
  uploadRestore(file);
});
// 拖进窗口：选文件那一步和出错以后都认（预览时拖进来一个新的，换成它）
restoreDialog.addEventListener('dragover', (event) => {
  if (!['pick', 'failed', 'preview'].includes(restoreView.step) || !event.dataTransfer || ![...event.dataTransfer.types].includes('Files')) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = 'copy';
  if (restoreView.step === 'pick' && !restoreView.over) { restoreView.over = true; $('restore-body').querySelector('.restore-drop').classList.add('over'); }
});
restoreDialog.addEventListener('dragleave', (event) => {
  if (restoreView.over && !restoreDialog.contains(event.relatedTarget)) { restoreView.over = false; const drop = $('restore-body').querySelector('.restore-drop'); if (drop) drop.classList.remove('over'); }
});
restoreDialog.addEventListener('drop', (event) => {
  if (!['pick', 'failed', 'preview'].includes(restoreView.step)) return;
  event.preventDefault();
  restoreView.over = false;
  const file = event.dataTransfer && event.dataTransfer.files[0];
  if (file) uploadRestore(file);
});
restoreDialog.addEventListener('keydown', (key) => {
  if (key.isComposing || key.keyCode === 229) return;
  if (key.key === 'Escape' && !key.defaultPrevented) { key.preventDefault(); key.stopPropagation(); requestCloseRestore(); }
});
// 保险：焦点不在窗口里时按 Esc，浏览器直接发 cancel，不经过上面的 keydown
restoreDialog.addEventListener('cancel', (event) => { event.preventDefault(); requestCloseRestore(); });
restoreDialog.addEventListener('close', () => {
  discardRestore();
  restoreView = { step: 'pick' };
  restoreArmed = false;
  if ($('restore-open').offsetParent) $('restore-open').focus({ preventScroll: true });
});
// 点窗口外面的暗处：按下和松开都在暗处才算
{
  let pressedOutside = false;
  restoreDialog.addEventListener('mousedown', (press) => { pressedOutside = press.target === restoreDialog; });
  restoreDialog.addEventListener('click', (click) => { if (click.target === restoreDialog && pressedOutside) requestCloseRestore(); });
}
