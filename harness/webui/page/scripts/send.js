// ---- 发送消息 ------------------------------------------------------------
// 输入框跟着内容长高、发出一条消息（可以带附件）、开始一段新对话（可以用一个自定义 Agent 当角色）。

// 输入框跟着内容长高，到上限为止；超过上限才出现滚动条
const INPUT_LIMIT = 210;
function resizeInput() {
  input.style.height = 'auto';
  const wanted = input.scrollHeight;
  input.style.height = Math.min(wanted, INPUT_LIMIT) + 'px';
  input.style.overflowY = wanted > INPUT_LIMIT ? 'auto' : 'hidden';
}
if ('ResizeObserver' in window) {
  let inputWidth = 0;
  new ResizeObserver(() => {
    if (input.clientWidth === inputWidth) return;  // 只管宽度变化；高度是 resizeInput 自己改的
    inputWidth = input.clientWidth;
    resizeInput();
  }).observe(input);
}

async function sendMessage() {
  const text = input.value.trim();
  if (pending.some((item) => item.loading)) { toast(t('附件还在读取，请稍等一下再发送。')); return; }
  const files = pending.slice();
  if (busy && (text || files.length)) { toast(t(BUSY_HINT)); return; }
  if ((!text && !files.length) || busy || sending) return;
  // 整条消息（连同附件）最多约 64 MB，超过的助手程序会直接退回。base64 编码后的大小就是要发的字节数，文字按最坏情况算
  const bytes = files.reduce((sum, item) => sum + (item.data || '').length + (item.thumb || '').length + (item.preview || '').length + (item.text || '').length * 3, text.length * 3);
  if (bytes > 62e6) { toast(t('附件加起来太大了：一条消息连同附件最多约 64 MB。可以分成几条发。')); return; }
  sending = true;
  const attachments = files.map(({ kind, name, media_type, data, text: content, thumb, preview }) => ({ kind, name, media_type, data, text: content, thumb, preview }));
  const sent = await post('/api/message', files.length ? { text, attachments } : { text });
  sending = false;
  if (!sent) return;  // 没发出去（比如当前的模型看不了图片）：附件和文字都留着，改一改还能再发
  pending = pending.filter((item) => !files.includes(item));
  renderTray();
  sentAt = performance.now();
  input.value = '';
  resizeInput();
  refreshStatus();
  setStick(true);  // 发了新消息，就回到最新的位置
  follow();
}

$('form').addEventListener('submit', (submit) => {
  submit.preventDefault();
  if (!busy) { sendMessage(); return; }
  // 发送和停止是同一个按钮。双击发送时，第二下会落在刚变出来的"停止"上：刚发出的半秒内不算数
  if (performance.now() - sentAt < 500) return;
  stopping = true;
  refreshStatus();
  post('/api/stop');
});

input.addEventListener('keydown', (key) => {
  // 用输入法打中文时，按回车是"选定候选词"，不是发送，所以正在输入法组字时要忽略回车
  if (key.key !== 'Enter' || key.shiftKey || key.isComposing || key.keyCode === 229) return;
  key.preventDefault();
  sendMessage();  // 正忙的时候 sendMessage 什么都不做，避免误触变成"停止"
});
input.addEventListener('input', () => { resizeInput(); refreshStatus(); });

// agent：用哪个自定义 Agent 当这段对话的角色（见"新对话旁边的 Agent 选择"）；不给就是普通的新对话
function startNew(agent) {
  if (busy) { toast(t(BUSY_HINT)); return; }
  post('/api/new', typeof agent === 'string' ? { agent } : {});
  if (overlayNav.matches) setNav(false, false);  // 窄屏上会话栏盖着对话，点完就收起来
  input.focus();
}
newButton.addEventListener('click', () => startNew());
$('new-compact').addEventListener('click', () => startNew());

for (const card of document.querySelectorAll('.starter')) {
  card.addEventListener('click', () => {  // 只是把例子填进输入框，发不发由用户决定
    input.value = t(card.dataset.prompt);
    resizeInput();
    refreshStatus();
    input.focus();
  });
}
