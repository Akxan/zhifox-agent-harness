// ---- 遮住密钥 -------------------------------------------------------------------
// 设置里"遮住密钥"一节：一个开关（state.redact_secrets）。认哪些、怎么遮、遮成什么样都在 harness/redact.py，
// 页面只管开和关，不另抄一份规矩。默认开着；关掉之前在原地问一次（关掉以后密钥会原样进对话，进去了就删不掉），
// 打开不用问。改了马上保存（/api/redact），正在跑的这一轮，下一个工具的结果就按新的来。

let redactOn = true;
let redactSaving = false;  // 正在保存：这期间开关不能再点

function renderRedact(on) {
  if (typeof on !== 'boolean') { $('redact-section').hidden = true; return; }  // 旧的助手程序没有这一项
  $('redact-section').hidden = false;
  redactOn = on;
  const toggle = $('redact-enabled');
  toggle.setAttribute('aria-checked', String(on));
  toggle.disabled = redactSaving;
  retext($('redact-state'), on ? k('（开着）') : k('（关着）'));
  if (!on) showRedactConfirm(false);  // 已经关了（也许是在另一个标签页里关的）：不用再问
}

// 关掉之前的那一块提醒。要藏起来的那一块正拿着焦点的话，先把焦点挪回开关上（不然 Esc 会绕过去直接关掉设置窗口）
function showRedactConfirm(open) {
  const box = $('redact-confirm');
  if (box.hidden === !open) return;
  const hadFocus = box.contains(document.activeElement);
  box.hidden = !open;
  $('redact-enabled').setAttribute('aria-expanded', String(open));
  if (open) $('redact-confirm-no').focus();  // 焦点放在"保持开着"上：随手按回车不会关掉
  else if (hadFocus) $('redact-enabled').focus();
}

async function saveRedact(on) {
  redactSaving = true;
  $('redact-enabled').disabled = true;
  const reply = await call('/api/redact', { enabled: on });
  redactSaving = false;
  $('redact-enabled').disabled = false;
  if (!reply.ok) { say(reply.error, 'bad'); return; }
  showRedactConfirm(false);
  renderRedact(on);  // 不等状态：马上画成新的样子（状态随后就到，和这里一样）
  say(on ? t('已打开：工具结果里的密钥会先遮住，再交给模型。') : t('已关掉：工具结果会原样交给模型。'), 'good');
}

$('redact-enabled').addEventListener('click', () => {
  if (redactSaving) return;
  if (redactOn) showRedactConfirm($('redact-confirm').hidden);  // 关掉要先问；问着的时候再点一下开关，等于"保持开着"
  else saveRedact(true);
});
$('redact-confirm-no').addEventListener('click', () => showRedactConfirm(false));
$('redact-confirm-yes').addEventListener('click', () => saveRedact(false));
$('redact-confirm').addEventListener('keydown', (key) => {
  if (key.key === 'Escape') { key.preventDefault(); key.stopPropagation(); showRedactConfirm(false); }
});
settings.addEventListener('close', () => showRedactConfirm(false));  // 设置窗口关上：下次打开时不再挂着这一块
