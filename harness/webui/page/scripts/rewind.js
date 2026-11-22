// ---- 编辑最后一条消息、重新生成 --------------------------------------------------
// 只有最后一轮可以（为什么见 harness/rewind.py 开头）：最后一条消息左边有一个"编辑"，最后一个回答下面那排按钮里
// 有一个"重新生成"；这一轮没有回答（出错了、被停下了），就在最后单独放一排"重新生成"。
// 能不能、为什么不能、这一轮做过哪些要先问你的事，都由助手程序算好放在状态的 rewind 里（见 server.py 的 _rewind_state），
// 页面只照着画。倒回去以后，助手程序发一个 reset、把留下的内容重放一遍（和打开一段会话一样），
// 所以页面不用自己去删倒回去的那些：开着的每个标签页都收到同样的事件，各自原地重画，不用刷新。
//   编辑：消息原地变成一个输入框，附件留着（可以去掉几个）。Enter 发送，Shift+Enter 换行。Esc 和"取消"按
//         "有没保存的修改"的规矩：改过了先提醒一次，再来一次才放弃。输入框是空的时候按 ↑ 也能打开它。
//   确认：这一轮执行过命令、改过文件……倒回去的只是对话，那些事不会撤销。所以先在原地列出来、说清楚，再点一次才做。
// 改到一半时对话变了（另一个标签页倒回过、开始了新的一轮），改了的文字放回下面的输入框，不会丢。

let turnEdit = null;     // 打开着的编辑框：{box, bubble, row, text, field, shown, removed, note, foot, turn, armed, confirming, sending}
let turnConfirm = null;  // 打开着的"重新生成之前确认一下"：{card, turn}
let turnSeq = 0;         // 给确认卡片的标题编号（aria-labelledby 要一个 id）

const rewindInfo = () => (state && state.rewind && typeof state.rewind === 'object' ? state.rewind : null);
const effectsOf = (info) => (info && Array.isArray(info.effects) ? info.effects.filter((line) => typeof line === 'string') : []);
// 这一轮操作过整台电脑、在浏览器里点过输入过：确认里为它们单独写一句（终端里是同样的两句，见 harness/cli.py 的 OPERATED）
const TURN_OPERATED = {
  computer: k('这一轮操作过整台电脑（点击、拖动、打字、按键）：在别的程序里已经做了的事，撤不回来。'),
  browser: k('这一轮在浏览器里点击、输入过：网页上已经提交、发出去的东西，撤不回来。'),
};
// 不能倒回去的原因（助手程序给的原文，换语言时照着重译）
const rewindReason = (info) => (info && info.reason && info.reason.key ? t(info.reason.key, info.reason.values) : String((info && info.reason && info.reason.text) || t('现在不能编辑或重新生成。')));

// 最后一条用户消息在页面上的样子：带附件的话，附件那一排紧挨在消息上面（见 handlers.user）；只发了附件的，只有那一排
function lastUserNodes() {
  const found = log.querySelectorAll(':scope > .msg.user, :scope > .msg-atts');
  const last = found[found.length - 1];
  if (!last) return null;
  if (last.classList.contains('msg-atts')) return { bubble: null, row: last, anchor: last };
  const row = last.previousElementSibling && last.previousElementSibling.classList.contains('msg-atts') ? last.previousElementSibling : null;
  return { bubble: last, row, anchor: last };
}

// 最后一个回答：最后一条用户消息之后、末尾那几行提示（"已中断"之类）之前的最后一块。这一轮是以出错收尾的，就没有。
// 开着的确认卡片也不算（它摆在最后）
function lastReplyAfter(anchor) {
  let node = log.lastElementChild;
  const skip = (item) => item.classList.contains('turn-confirm') || (item.classList.contains('notice') && !item.classList.contains('error'));
  while (node && node !== anchor && skip(node)) node = node.previousElementSibling;
  return node && node !== anchor && node.classList.contains('assistant') ? node : null;
}

function offButton(button, info) {
  button.classList.toggle('turn-off', !info.ok);
  if (info.ok) button.removeAttribute('aria-disabled'); else button.setAttribute('aria-disabled', 'true');
}

// 照着最新的状态把"编辑""重新生成"摆到对的地方。状态来了、一轮开始和结束、编辑框关上时都调一次，摆之前先把旧的拿掉
function renderTurnTools() {
  for (const node of log.querySelectorAll('.turn-edit-open, .turn-retry, .turn-actions')) node.remove();
  for (const node of log.querySelectorAll('.turn-last')) node.classList.remove('turn-last');
  const info = rewindInfo();
  if (turnEdit && !turnEdit.sending && (busy || !info || info.turn !== turnEdit.turn)) abandonTurnEdit();
  if (turnConfirm && (busy || !info || info.turn !== turnConfirm.turn)) closeTurnConfirm(false);
  if (!info || busy || replaying || turnEdit) return;
  const nodes = lastUserNodes();
  if (!nodes) return;

  const edit = iconButton('edit', k('编辑这条消息'));
  edit.classList.add('turn-edit-open');
  offButton(edit, info);
  edit.addEventListener('click', () => { if (!openTurnEdit()) explainTurn(); });
  nodes.anchor.appendChild(edit);

  const reply = lastReplyAfter(nodes.anchor);
  const actions = reply && reply.querySelector(':scope > .msg-actions');
  let retry;
  if (actions) {
    reply.classList.add('turn-last');
    retry = actions.appendChild(iconButton('refresh', k('重新生成')));
  } else {  // 这一轮没有回答：在最后单独放一排（确认卡片开着的话，放在它上面）
    const row = log.insertBefore(el('div', 'turn-actions'), turnConfirm ? turnConfirm.card : null);
    retry = row.appendChild(el('button', 'button with-icon turn-again'));
    retry.type = 'button';
    retry.append(icon('refresh'), tel('span', null, k('重新生成')));
  }
  retry.classList.add('turn-retry');
  offButton(retry, info);
  retry.addEventListener('click', () => retryTurn());
}

function explainTurn() {
  const info = rewindInfo();
  toast(busy ? t(BUSY_HINT) : rewindReason(info));
}

// 一张"倒回去之前确认一下"的卡片：这一轮做过的、可能改动了电脑的事，一行一件。go 是确认以后做的事，back 是不做了
function confirmCard(info, goLabel, go, back) {
  const card = el('div', 'turn-confirm');
  card.setAttribute('role', 'group');
  const head = card.appendChild(el('p', 'turn-confirm-head'));
  head.append(icon('alert'), tel('span', null, k('倒回去的只是对话')));
  card.appendChild(tel('p', 'turn-confirm-text', k('这一轮已经做过下面这些事，它们不会跟着撤销：执行过的命令、改过的文件、发出去的东西都还在。')));
  for (const kind of Array.isArray(info.operated) ? info.operated : []) {
    if (Object.hasOwn(TURN_OPERATED, kind)) card.appendChild(tel('p', 'turn-confirm-operated', TURN_OPERATED[kind]));
  }
  const list = card.appendChild(el('ul', 'turn-confirm-list'));
  for (const line of effectsOf(info)) list.appendChild(el('li', null, line));
  if (Number(info.more) > 0) list.appendChild(tel('li', 'more', k('……还有 {n} 项'), { n: Number(info.more) }));
  const actions = card.appendChild(el('div', 'turn-confirm-actions'));
  const yes = actions.appendChild(tel('button', 'button primary', goLabel));
  const no = actions.appendChild(tel('button', 'button', k('取消')));
  yes.type = no.type = 'button';
  yes.addEventListener('click', go);
  no.addEventListener('click', back);
  card.addEventListener('keydown', (key) => {
    if (key.key === 'Escape' && !key.isComposing) { key.preventDefault(); key.stopPropagation(); back(); }
  });
  head.id = 'turn-confirm-head-' + (++turnSeq);
  card.setAttribute('aria-labelledby', head.id);
  return { card, yes };
}

// ---- 重新生成 ----

async function retryTurn(confirmed) {
  const info = rewindInfo();
  if (!info || busy || !info.ok) { explainTurn(); return; }
  if (effectsOf(info).length && !confirmed) { openTurnConfirm(info); return; }
  if (sending) return;
  sending = true;
  const result = await call('/api/turn/retry', { conversation: state.conversation, turn: info.turn, confirmed: Boolean(confirmed) });
  sending = false;
  if (result.ok) { closeTurnConfirm(false); setStick(true); return; }
  if (Array.isArray(result.data.effects) && !confirmed) {  // 页面看到的晚了一步：这一轮其实做过要先问的事
    openTurnConfirm({ ...info, effects: result.data.effects, more: result.data.more, operated: result.data.operated });
    return;
  }
  toast(result.error);
}

function openTurnConfirm(info) {
  closeTurnConfirm(false);
  const { card, yes } = confirmCard(info, k('仍然重新生成'), () => retryTurn(true), () => closeTurnConfirm(true));
  log.appendChild(card);
  turnConfirm = { card, turn: info.turn };
  play(card, [{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }], 240);
  setStick(true);
  follow();
  yes.focus({ preventScroll: true });
  announce(t('倒回去的只是对话') + '：' + card.textContent);
}

// refocus：焦点放回"重新生成"上（按了 Esc、点了"取消"）
function closeTurnConfirm(refocus) {
  if (!turnConfirm) return;
  const hadFocus = turnConfirm.card.contains(document.activeElement);
  turnConfirm.card.remove();
  turnConfirm = null;
  if (hadFocus) {  // 卡片拿着焦点就被拿掉了：交给看得见的地方，不然 Esc 之类的按键就没人接了
    const retry = log.querySelector('.turn-retry');
    (refocus && retry ? retry : input).focus({ preventScroll: true });
  }
}

// ---- 编辑 ----

// 打开编辑框。能打开返回 true（不能的话调用的人去说明为什么）
function openTurnEdit() {
  const info = rewindInfo();
  const nodes = lastUserNodes();
  if (!info || !info.ok || busy || !nodes || turnEdit) return Boolean(turnEdit);
  closeTurnConfirm(false);
  // 语音转出来的消息前面有一个"语音转文字"的小标记（见 voice.js）：只拿原文
  const text = nodes.bubble ? (nodes.bubble.dataset.text !== undefined ? nodes.bubble.dataset.text : nodes.bubble.textContent) : '';
  const shown = nodes.row ? ((rowItems.get(nodes.row) || {}).shown || []) : [];
  const box = el('div', 'turn-editor');
  box.setAttribute('role', 'group');
  tattr(box, 'aria-label', k('编辑这条消息'));
  const atts = box.appendChild(el('div', 'turn-editor-atts'));
  const field = box.appendChild(el('textarea', 'turn-editor-input'));
  field.rows = 1;
  field.value = text;
  tattr(field, 'aria-label', k('改过的消息'));
  tattr(field, 'placeholder', shown.length ? k('只发附件也可以') : k('写点什么'));
  const foot = box.appendChild(el('div', 'turn-editor-foot'));
  const note = foot.appendChild(el('p', 'turn-editor-note'));
  note.setAttribute('role', 'status');
  note.setAttribute('aria-live', 'polite');
  const buttons = foot.appendChild(el('div', 'turn-editor-buttons'));  // 两个按钮放在一起：窄的时候整组换到下一行，不会拆开
  const cancel = buttons.appendChild(tel('button', 'button', k('取消')));
  const save = buttons.appendChild(tel('button', 'button primary', k('发送')));
  cancel.type = save.type = 'button';
  turnEdit = { box, bubble: nodes.bubble, row: nodes.row, text, field, shown, removed: new Set(), atts, note, foot, cancel, save, turn: info.turn, armed: false, confirming: null, sending: false };
  turnEditNote();
  renderTurnEditAtts();
  (nodes.row || nodes.bubble).before(box);
  if (nodes.row) nodes.row.hidden = true;
  if (nodes.bubble) nodes.bubble.hidden = true;
  for (const node of log.querySelectorAll('.turn-edit-open, .turn-retry, .turn-actions')) node.remove();
  sizeTurnEditField();
  field.addEventListener('input', () => { sizeTurnEditField(); if (turnEdit && turnEdit.armed) { turnEdit.armed = false; turnEditNote(); } });
  field.addEventListener('keydown', (key) => {
    if (key.key === 'Enter' && !key.shiftKey && !key.isComposing && key.keyCode !== 229) { key.preventDefault(); submitTurnEdit(false); }
  });
  box.addEventListener('keydown', (key) => {
    if (key.key === 'Escape' && !key.isComposing && !key.defaultPrevented) { key.preventDefault(); requestCloseTurnEdit(); }
  });
  cancel.addEventListener('click', requestCloseTurnEdit);
  save.addEventListener('click', () => submitTurnEdit(false));
  play(box, [{ opacity: 0, transform: 'scale(.97)' }, { opacity: 1, transform: 'none' }], 220);
  field.focus({ preventScroll: true });
  field.setSelectionRange(field.value.length, field.value.length);
  box.scrollIntoView({ block: 'nearest', behavior: calm() ? 'auto' : 'smooth' });
  return true;
}

// 编辑框底下那行字：平时是怎么用的提示，也用来提醒（还没发送）、说出错的原因
function turnEditNote(text, kind, values) {
  if (!turnEdit) return;
  turnEdit.note.className = 'turn-editor-note' + (kind ? ' ' + kind : '');
  retext(turnEdit.note, text || k('Enter 发送，Shift+Enter 换行，Esc 取消'), values);
}

function sizeTurnEditField() {
  const field = turnEdit && turnEdit.field;
  if (!field) return;
  field.style.height = 'auto';
  field.style.height = Math.min(field.scrollHeight, 320) + 'px';
  field.style.overflowY = field.scrollHeight > 320 ? 'auto' : 'hidden';
}

// 编辑框里的附件：和对话里的卡片一样能点开看，右上角的 × 把它从这条消息里去掉（发送之前都还能反悔：取消就行）
function renderTurnEditAtts() {
  if (!turnEdit) return;
  const { shown, removed, atts } = turnEdit;
  const kept = shown.map((summary, index) => ({ summary, index })).filter(({ index }) => !removed.has(index));
  atts.hidden = !kept.length;
  atts.replaceChildren(...kept.map(({ summary, index }) => {
    const card = attachmentCard(summary, false, null);
    const remove = card.appendChild(el('button', 'remove'));
    remove.type = 'button';
    remove.setAttribute('aria-label', t('移除 {name}', { name: summary.name }));
    remove.appendChild(icon('x'));
    remove.addEventListener('click', () => {
      removed.add(index);
      if (turnEdit.armed) { turnEdit.armed = false; turnEditNote(); }
      renderTurnEditAtts();
      turnEdit.field.focus({ preventScroll: true });
    });
    return card;
  }));
}

const turnEditChanged = () => Boolean(turnEdit) && (turnEdit.field.value !== turnEdit.text || turnEdit.removed.size > 0);

// Esc、"取消"：改过了先提醒一次，再来一次才放弃；正在确认的时候是"返回修改"
function requestCloseTurnEdit() {
  if (!turnEdit || turnEdit.sending) return;
  if (turnEdit.confirming) { backToTurnEdit(); return; }
  if (turnEditChanged() && !turnEdit.armed) {
    turnEdit.armed = true;
    turnEditNote(k('改过的内容还没发送。再按一次 Esc（或者再点一次“取消”），就放弃这些修改。'), 'warn');
    return;
  }
  closeTurnEdit(true);
}

function closeTurnEdit(refocus) {
  if (!turnEdit) return;
  const { box, bubble, row } = turnEdit;
  const hadFocus = box.contains(document.activeElement);
  turnEdit = null;
  box.remove();
  if (bubble) bubble.hidden = false;
  if (row) row.hidden = false;
  renderTurnTools();
  if (hadFocus || refocus) {
    const opener = log.querySelector('.turn-edit-open');
    (refocus && opener ? opener : input).focus({ preventScroll: true });
  }
}

// 对话在改的时候变了（另一个标签页倒回过、开始了新的一轮、换了一段会话）：编辑框收起来，改过的字放回下面的输入框
function abandonTurnEdit() {
  if (!turnEdit) return;
  const draft = turnEditChanged() ? turnEdit.field.value.trim() : '';
  const hadFocus = turnEdit.box.contains(document.activeElement);
  const { box, bubble, row } = turnEdit;
  turnEdit = null;
  box.remove();
  if (bubble) bubble.hidden = false;
  if (row) row.hidden = false;
  if (draft) {
    input.value = input.value.trim() ? input.value.replace(/\s+$/, '') + '\n\n' + draft : draft;
    resizeInput();
    refreshStatus();
    toast(t('对话刚刚变了，改到一半的消息放回了下面的输入框。'));
  }
  if (hadFocus) input.focus({ preventScroll: true });
}

async function submitTurnEdit(confirmed) {
  if (!turnEdit || turnEdit.sending) return;
  const info = rewindInfo();
  const text = turnEdit.field.value.trim();
  const keep = turnEdit.shown.map((_, index) => index).filter((index) => !turnEdit.removed.has(index));
  if (!text && !keep.length) { turnEditNote(k('消息不能是空的：写点什么，或者留下至少一个附件。'), 'error'); return; }
  if (!info || !info.ok) {
    if (info && info.reason && info.reason.key) turnEditNote(info.reason.key, 'error', info.reason.values);
    else turnEditNote(k('现在不能编辑或重新生成。'), 'error');
    return;
  }
  if (busy) { turnEditNote(BUSY_HINT, 'error'); return; }
  if (effectsOf(info).length && !confirmed) { confirmTurnEdit(info); return; }
  const edit = turnEdit;
  edit.sending = true;
  edit.field.disabled = edit.save.disabled = edit.cancel.disabled = true;
  if (edit.confirming) edit.confirming.card.querySelectorAll('button').forEach((button) => { button.disabled = true; });
  else turnEditNote(k('正在发送……'));
  const result = await call('/api/turn/edit', { conversation: state.conversation, turn: edit.turn, text, keep, confirmed: Boolean(confirmed) });
  if (result.ok) { setStick(true); return; }  // 编辑框随着重画（reset）一起没了，不用这里收
  edit.sending = false;
  if (turnEdit !== edit) return;
  edit.field.disabled = edit.save.disabled = edit.cancel.disabled = false;
  if (edit.confirming) edit.confirming.card.querySelectorAll('button').forEach((button) => { button.disabled = false; });
  if (Array.isArray(result.data.effects) && !confirmed) { confirmTurnEdit({ ...info, effects: result.data.effects, more: result.data.more, operated: result.data.operated }); return; }
  if (edit.confirming) backToTurnEdit();
  turnEditNote(result.error, 'error');
  edit.field.focus({ preventScroll: true });
}

// 确认就放在编辑框里，替掉下面那一排按钮
function confirmTurnEdit(info) {
  if (!turnEdit) return;
  const { card, yes } = confirmCard(info, k('仍然发送'), () => submitTurnEdit(true), backToTurnEdit);
  turnEdit.foot.hidden = true;
  turnEdit.field.disabled = true;
  turnEdit.box.appendChild(card);
  turnEdit.confirming = { card };
  play(card, [{ opacity: 0, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], 200);
  yes.focus({ preventScroll: true });
  card.scrollIntoView({ block: 'nearest', behavior: calm() ? 'auto' : 'smooth' });
}

function backToTurnEdit() {
  if (!turnEdit || !turnEdit.confirming || turnEdit.sending) return;
  turnEdit.confirming.card.remove();
  turnEdit.confirming = null;
  turnEdit.foot.hidden = false;
  turnEdit.field.disabled = false;
  turnEdit.field.focus({ preventScroll: true });
}

// 页面要整个重画了（reset：倒回去了、打开了另一段会话）：编辑框和确认卡片跟着没了。焦点在它们里面的话，交给输入框
function dropTurnTools() {
  if (turnEdit && !turnEdit.sending) abandonTurnEdit();
  turnEdit = null;
  closeTurnConfirm(false);
}

// 输入框是空的时候按 ↑：编辑最后一条消息（很多聊天软件都这样）。输入框里有字、有附件的时候，↑ 照常是移动光标
input.addEventListener('keydown', (key) => {
  if (key.key !== 'ArrowUp' || key.shiftKey || key.altKey || key.metaKey || key.ctrlKey || key.isComposing) return;
  if (input.value || pending.length) return;
  if (openTurnEdit()) key.preventDefault();
});
