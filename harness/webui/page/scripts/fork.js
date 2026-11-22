// ---- 从某一轮分叉成一段新对话 ----------------------------------------------------
// "从第 3 轮换个思路重来"：原来那段一字不动，新开的一段从那里接着聊（规矩见 harness/fork.py）。每一轮结束以后
// （实时的、重放出来的都一样），这一轮上多两个"分叉"：
//   - 回答下面那排按钮里，紧挨着"复制"：在回答上分叉，新的一段带着这一轮；
//   - 你的消息左边（悬停才出现，和"编辑"同一个位置，最后一条消息有"编辑"时排在它左边）：在你的消息上分叉，
//     新的一段截到这一轮之前，这条消息（文字和附件）放回输入框，改了再发。
// 能不能分叉由助手程序说了算（状态里的 fork，见 harness/webui/fork_api.py）：没有记下位置的那一轮（定时任务自己跑的）、
// 压缩进了摘要的那几轮（位置比 floor 早），按钮是灰的，点一下说为什么。点了以后助手程序建好新的一段、直接换过去
// （reset、重放、状态，和打开一段会话一样），页面不用自己去画。
// 分叉出来的会话顶上有一行"从「原会话」第 3 轮的回答分叉出来"，点"打开原会话"回到原会话、滚到那一轮；原会话删掉了就只剩文字。
// 会话栏里分叉出来的那几段前面有一个小小的分叉图标。

const forkBanner = $('fork-origin');
const FORK_COMPACTED = k('这一轮已经压缩进了摘要（对话太长时较早的内容会写成摘要），不能在这里分叉了。');
const FORK_LABELS = { reply: k('从这里分叉（带上这一轮）'), message: k('从这条消息分叉（改了再发）') };
let forkFloor = null;   // 上一次照着画按钮的 floor（变了才把每个按钮重画一遍）
let forkShown = '';     // 顶上那一行上一次是照着什么画的
let forking = false;    // 分叉的请求还没回来（连点两下只发一次）

// 一条用户消息画出来了：记下它是哪一轮（事件编号、在历史里的位置），这一轮结束时照着它放按钮
function forkTurn(node, event) {
  node.dataset.forkTurn = String(event.id);
  if (Number.isInteger(event.mark) && event.mark >= 0) node.dataset.forkMark = String(event.mark);
}

// 一轮结束了：在它上面放两个"分叉"。重新生成的那一轮用的还是那条消息，已经有了的不再放一个
function forkTurnEnded() {
  const nodes = lastUserNodes();
  if (!nodes || !nodes.anchor.dataset.forkTurn) return;
  const anchor = nodes.anchor;
  if (!anchor.querySelector(':scope > .fork-msg')) anchor.appendChild(forkButton('message', anchor));
  const reply = lastReplyAfter(anchor);
  const actions = reply && reply.querySelector(':scope > .msg-actions');
  if (actions && !actions.querySelector('.fork-reply')) {
    const first = actions.firstElementChild;  // 复制
    actions.insertBefore(forkButton('reply', anchor), first ? first.nextSibling : null);
  }
}

function forkButton(at, anchor) {
  const button = iconButton('fork', FORK_LABELS[at]);
  button.classList.add('fork-open', at === 'reply' ? 'fork-reply' : 'fork-msg');
  button.dataset.forkAt = at;
  button.dataset.forkTurn = anchor.dataset.forkTurn;
  if (anchor.dataset.forkMark !== undefined) button.dataset.forkMark = anchor.dataset.forkMark;
  paintFork(button);
  button.addEventListener('click', () => forkAt(button));
  return button;
}

// 这一轮为什么不能分叉（空字符串是能）
function forkProblem(button) {
  const info = state && state.fork;
  if (!info) return '';
  if (button.dataset.forkMark === undefined) {
    const reason = info.unmarked || {};
    return reason.key ? t(reason.key, reason.values) : String(reason.text || '');
  }
  return Number(button.dataset.forkMark) < Number(info.floor) ? t(FORK_COMPACTED) : '';
}

// 分不了的画成灰的（还能点：点一下说为什么），和"编辑""重新生成"一样
function paintFork(button) {
  const off = Boolean(forkProblem(button));
  button.classList.toggle('turn-off', off);
  if (off) button.setAttribute('aria-disabled', 'true'); else button.removeAttribute('aria-disabled');
}

async function forkAt(button) {
  if (busy) { toast(t(BUSY_HINT)); return; }
  const problem = forkProblem(button);
  if (problem) { toast(problem); return; }
  if (forking || !state) return;
  forking = true;
  const at = button.dataset.forkAt;
  const result = await call('/api/fork', { conversation: state.conversation, turn: Number(button.dataset.forkTurn), at });
  forking = false;
  if (!result.ok) { toast(result.error); return; }
  // 新的一段已经换上了（页面正在重放它）。在消息上分叉的，那条消息放回输入框
  if (at === 'message') {
    toast(t('已经分叉成一段新对话，原来那段没有动。那条消息放回了输入框，改好了再发。'));
    await restoreForkDraft(result.data);
  } else {
    toast(t('已经分叉成一段新对话，原来那段没有动。接着说就是新的方向。'));
    input.focus({ preventScroll: true });
  }
}

// 在消息上分叉：文字接在输入框里已有的字后面（不覆盖你正在写的），附件从多媒体库取回原件、和你自己选的文件一样放进输入框
async function restoreForkDraft(data) {
  const text = typeof data.draft === 'string' ? data.draft : '';
  if (text) {
    input.value = input.value.trim() ? input.value.replace(/\s+$/, '') + '\n\n' + text : text;
    resizeInput();
    refreshStatus();
  }
  input.focus({ preventScroll: true });
  input.setSelectionRange(input.value.length, input.value.length);
  const ids = (Array.isArray(data.attachments) ? data.attachments : []).filter((id) => typeof id === 'string');
  if (!ids.length) return;
  const files = [], failed = [];
  for (const id of ids) {
    try {
      const { blob, name } = await fetchFile('/api/media/file', id, t('附件'));
      files.push(new File([blob], name, { type: blob.type }));
    } catch (problem) {
      failed.push(problem.message || String(problem));
    }
  }
  if (files.length) await addFiles(files);
  if (failed.length) toast(t('有 {n} 个附件没能放回输入框（{error}）。可以重新选一次文件。', { n: failed.length, error: failed[0] }));
}

// 状态来了：floor 变了（这一轮压缩过历史）就把每个按钮重画一遍；顶上那一行照着这段会话的来路画
function renderForkState() {
  const info = state && state.fork;
  const floor = info ? Number(info.floor) : 0;
  if (floor !== forkFloor) {
    forkFloor = floor;
    for (const button of log.querySelectorAll('.fork-open')) paintFork(button);
  }
  renderForkOrigin(info && info.origin);
}

function renderForkOrigin(origin) {
  const print = JSON.stringify([origin || null, language]);
  if (print === forkShown) return;
  const wasHidden = forkBanner.hidden;
  forkShown = print;
  if (!origin || typeof origin.id !== 'string') {
    forkBanner.hidden = true;
    forkBanner.replaceChildren();
    return;
  }
  const values = { title: String(origin.title || '') || t('新对话'), n: Number(origin.turn) || 1 };
  const words = tel('span', 'fork-origin-text', origin.at === 'message' ? k('从「{title}」第 {n} 轮之前分叉出来') : k('从「{title}」第 {n} 轮的回答分叉出来'), values);
  const parts = [icon('fork', 'fork-origin-icon'), words];
  if (origin.exists) {
    const open = tel('button', 'fork-origin-open', k('打开原会话'));
    open.type = 'button';
    open.appendChild(icon('chevron'));
    open.addEventListener('click', () => openForkOrigin(origin));
    parts.push(open);
  } else {
    parts.push(tel('span', 'fork-origin-gone', k('原会话已经删除了')));
  }
  if (origin.changes) {
    parts.push(tel('p', 'fork-origin-note', origin.exists ? k('这几轮改过文件：改动的记录留在原会话里，要撤回请到那里。') : k('这几轮改过文件：改动的记录跟着原会话删掉了，没法一键撤回。')));
  }
  forkBanner.replaceChildren(...parts);
  forkBanner.hidden = false;
  if (wasHidden && body.classList.contains('ready')) play(forkBanner, [{ opacity: 0, transform: 'translateY(-4px)' }, { opacity: 1, transform: 'none' }], 260);
}

function openForkOrigin(origin) {
  if (busy) { toast(t(BUSY_HINT)); return; }
  jump = { id: origin.id, turn: Number(origin.turn) || 1 };  // 打开以后滚到分叉的那一轮（见 settle.js 的 reveal）
  post('/api/conversation/open', { id: origin.id });
}

// 回到原会话：滚到第 n 轮你的那条消息（带附件的，连同那一排附件），亮一下
function revealTurn(n) {
  const anchor = log.querySelectorAll(':scope > [data-fork-turn]')[n - 1];
  if (!anchor) return;
  const above = anchor.previousElementSibling;
  const top = !anchor.classList.contains('msg-atts') && above && above.classList.contains('msg-atts') ? above : anchor;
  setStick(false);
  const spot = top.getBoundingClientRect(), view = chat.getBoundingClientRect();
  chat.scrollTop += spot.top - view.top - view.height / 3;
  setStick(chat.scrollHeight - chat.scrollTop - chat.clientHeight < 4);
  anchor.classList.remove('found');
  void anchor.offsetWidth;  // 让浏览器先认下"没有这个类"，再加回去，动画才会重新播
  anchor.classList.add('found');
  setTimeout(() => anchor.classList.remove('found'), 2300);
}

// 会话栏里分叉出来的那几段前面的小图标（读屏软件读作"分叉出来的"）
function forkMark() {
  const mark = el('span', 'conv-fork');
  mark.append(icon('fork'), tel('span', 'sr-only', k('分叉出来的')));
  tattr(mark, 'title', k('分叉出来的'));
  return mark;
}
