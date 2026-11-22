// ---- 语音转文字 --------------------------------------------------------------
// 聊天里发来的语音，智狐先转成文字再回答（见 harness/voice/）。这一节管页面上和它有关的两样东西：
//   设置里的一节    用哪一种（自动 / OpenAI / 你自己运行的兼容服务 / 关闭）、OpenAI 的模型（后面写着每分钟多少钱）、
//                   兼容服务的地址和模型名（地址不合格时在原地说为什么），或者从本机、内网的自定义模型接口里选一个
//                   （state.voice.endpoints，有的时候才出现），现在实际用的是哪一种
//   消息上的小标记  语音转出来的那条消息上一个"语音转文字"，带着语音有多长（voiceTag，events.js 的 user 用它）
// 设置、价格、有没有 OpenAI 的密钥（只说有没有），都由助手程序在状态里给（state.voice），页面不另存一份价格。
// 选单一改就存；只有地址和模型名要按"保存"（打了一半的地址不能马上发出去）。

let voiceStatus = null;   // 状态里最新的那一份
let voiceDrawn = '';      // 上一次按什么画的（没变就不重画，保留焦点和输入框里写了一半的地址）
let voicePicked = '';     // 选了"你自己运行的兼容服务"、地址还没保存时，选单先停在这一项（保存了才算数）

const VOICE_STATE = {
  openai: k('（用 OpenAI）'),
  local: k('（用你自己运行的服务）'),
  off: k('（关着）'),
};

// 消息上的小标记：一个话筒、"语音转文字"、语音有多长（0:12）
function voiceTag(seconds) {
  const tag = el('span', 'msg-voice');
  tag.appendChild(icon('mic'));
  tag.appendChild(tel('span', 'msg-voice-label', k('语音转文字')));
  if (Number(seconds) > 0) tag.appendChild(el('span', 'msg-voice-time', durationText(Number(seconds))));
  return tag;
}

function renderVoice(status, force) {
  if (!status) return;
  voiceStatus = status;
  if (settings.open) drawVoiceSettings(force);
}

function voiceSay(text, kind) { say(text, kind); }  // 设置窗口开着：提示写在窗口里（浮在页面上的会被暗底挡住）

async function setVoice(changes, done) {
  const reply = await call('/api/voice/settings', changes);
  if (!reply.ok) {
    if (reply.data && reply.data.field) { voiceLocalNote(reply.error, true); return false; }  // 哪一格不合格：原因写在那一格下面
    voiceSay(reply.error, 'bad');
    return false;
  }
  voicePicked = '';
  voiceLocalNote('', false);
  voiceSay(done, 'good');
  // 新的状态在回答之前就到了（那时选单还停在没保存的那一项上）：按新的设置再画一遍
  drawVoiceSettings(true);
  return true;
}

// 兼容服务那几格下面的一行：说明，或者地址不合格的原因（写在原地，不弹出来）
function voiceLocalNote(text, bad) {
  const note = $('voice-local-note');
  note.classList.toggle('bad', Boolean(bad));
  if (bad) { delete note.dataset.i18n; note.textContent = text; }
  else retext(note, k('和 OpenAI 同一种接口的服务（比如 faster-whisper 的兼容服务器），不花钱；OpenAI 的密钥不会发给它。明文的 http:// 只能连这台电脑自己。'));
  $('voice-url').setAttribute('aria-invalid', String(Boolean(bad)));
}

function drawVoiceSettings(force) {
  const status = voiceStatus;
  if (!status) return;
  const print = JSON.stringify([status, language, voicePicked]);
  if (!force && print === voiceDrawn) return;
  voiceDrawn = print;

  const chosen = voicePicked || status.provider;
  const provider = $('voice-provider');
  provider.value = chosen;
  $('voice-card').classList.toggle('on', Boolean(status.active));
  retext($('voice-state'), status.active ? VOICE_STATE[status.active] : VOICE_STATE.off);

  // OpenAI 的模型：后面写着每分钟多少钱（价格是助手程序给的）
  const model = $('voice-model');
  const options = status.models.map((item) => {
    const option = el('option');
    option.value = item.id;
    retext(option, item.recommended ? k('{model} · 每分钟 {price}（推荐）') : k('{model} · 每分钟 {price}'), { model: item.id, price: costText(item.price) });
    return option;
  });
  model.replaceChildren(...options);
  model.value = status.model;
  $('voice-model-row').hidden = !(chosen === 'openai' || chosen === 'auto');

  // 兼容服务：地址和模型名。正在输入的那一格不动它
  $('voice-local').hidden = chosen !== 'local';
  drawVoiceEndpoints(status);
  const url = $('voice-url'), name = $('voice-local-model');
  if (document.activeElement !== url && url.dataset.saved !== status.url) { url.value = status.url || ''; url.dataset.saved = status.url || ''; }
  if (document.activeElement !== name && name.dataset.saved !== status.local_model) { name.value = status.local_model || ''; name.dataset.saved = status.local_model || ''; }
  name.placeholder = t('不填就用 {model}', { model: status.local_default || 'whisper-1' });  // 换语言时由 language.js 叫这里重画
  if (!$('voice-local-note').classList.contains('bad')) voiceLocalNote('', false);

  drawVoiceStatus(status, chosen);
  const price = (status.models.find((item) => item.id === status.model) || {}).price;
  retext($('voice-cost'), k('OpenAI 按语音的长短计费（{model} 每分钟约 {price}），每转一条都记在花费明细里（来源：语音转文字）；你自己运行的服务不花钱。最长转 {minutes} 分钟，更长的不转。暂时只支持 Telegram。'),
    { model: status.model, price: costText(price || 0), minutes: status.max_minutes || 10 });
}

// 现在实际会怎么做：用 OpenAI 的哪个模型、用你的服务、还是关着（关着的话为什么、怎么打开）
function drawVoiceStatus(status, chosen) {
  const box = $('voice-status');
  const parts = [];
  let bad = false;
  if (chosen === 'local' && voicePicked) {
    parts.push(tel('span', null, k('填上地址，按“保存”以后改用你自己运行的服务。')));
  } else if (status.active === 'openai') {
    parts.push(tel('span', null, k('收到语音时用 OpenAI 的 {model} 转成文字。'), { model: status.model }));
  } else if (status.active === 'local' && status.endpoint) {
    const found = (status.endpoints || []).find((item) => item.id === status.endpoint);
    parts.push(tel('span', null, k('收到语音时交给自定义模型接口「{name}」转成文字。'), { name: found ? found.name : status.endpoint }));
  } else if (status.active === 'local') {
    parts.push(tel('span', null, k('收到语音时交给 {url} 转成文字。'), { url: status.url }));
  } else if (chosen === 'local' && status.endpoint) {
    bad = true;
    parts.push(tel('span', null, k('选的那个自定义模型接口不在了（或者改成了外面的地址），现在是关着的。请另选一个，或者填上地址。')));
  } else if (chosen === 'off') {
    parts.push(tel('span', null, k('关着：收到语音时，智狐在聊天里告诉你怎么打开。')));
  } else if (!status.has_key) {
    bad = true;
    parts.push(tel('span', null, k('还没有填 OpenAI 的 API 密钥，所以现在是关着的。')));
    const fill = tel('button', 'mini', k('填写 API 密钥'));
    fill.type = 'button';
    fill.addEventListener('click', () => {
      const field = settings.querySelector('input[data-keep="key-openai"]');
      if (!field) return;
      field.closest('.key-row').scrollIntoView({ block: 'center', behavior: calm() ? 'auto' : 'smooth' });
      field.focus({ preventScroll: true });
    });
    parts.push(fill);
  }
  box.classList.toggle('bad', bad);
  box.replaceChildren(...parts);
  box.hidden = !parts.length;
}

$('voice-provider').addEventListener('change', async () => {
  const value = $('voice-provider').value;
  if (value === 'local' && !(voiceStatus && (voiceStatus.url || voiceStatus.endpoint))) {
    voicePicked = 'local';  // 还没有地址（也没选接口）：先把那几格亮出来，等你填好按"保存"
    drawVoiceSettings(true);
    $($('voice-url-row').hidden ? 'voice-endpoint' : 'voice-url').focus();
    return;
  }
  voicePicked = '';
  const done = { auto: k('语音转文字改成了自动：有 OpenAI 的密钥就用它。'), openai: k('语音转文字改用 OpenAI 了。'), local: k('语音转文字改用你自己运行的服务了。'), off: k('语音转文字已关闭。') }[value];
  if (!(await setVoice({ provider: value }, t(done)))) drawVoiceSettings(true);
});

$('voice-model').addEventListener('change', async () => {
  const value = $('voice-model').value;
  if (!(await setVoice({ model: value }, t('语音转文字改用 {model} 了。', { model: value })))) drawVoiceSettings(true);
});

async function saveVoiceLocal() {
  const url = $('voice-url'), name = $('voice-local-model'), endpoint = $('voice-endpoint').value;
  if (endpoint) {  // 用一个自定义接口：地址是它的，不用填
    const used = await setVoice({ provider: 'local', endpoint, local_model: name.value.trim() }, t('语音转文字改用你自己运行的服务了。'));
    if (used) name.dataset.saved = name.value.trim();
    return;
  }
  if (!url.value.trim()) { voiceLocalNote(t('用你自己运行的兼容服务，要先填上它的地址。'), true); url.focus(); return; }
  const saved = await setVoice({ provider: 'local', endpoint: '', url: url.value.trim(), local_model: name.value.trim() }, t('语音转文字改用你自己运行的服务了。'));
  if (saved) { url.dataset.saved = url.value.trim(); name.dataset.saved = name.value.trim(); }
}
$('voice-local-save').addEventListener('click', saveVoiceLocal);
for (const id of ['voice-url', 'voice-local-model']) {
  $(id).addEventListener('keydown', (key) => {
    if (key.key === 'Enter' && !key.isComposing) { key.preventDefault(); saveVoiceLocal(); }
  });
  $(id).addEventListener('input', () => { if ($('voice-local-note').classList.contains('bad')) voiceLocalNote('', false); });
}

// 从自定义模型接口里选（只有本机、内网的，助手程序挑好了给）。没有这样的接口时整行不出现，照旧填地址。
// 选了一个，地址那一格就收起来（用它的地址）；选"自己填地址"又放出来。正在选单上的那一项不被新的状态冲掉
function drawVoiceEndpoints(status) {
  const row = $('voice-endpoint-row'), select = $('voice-endpoint');
  const endpoints = status.endpoints || [];
  row.hidden = !endpoints.length && !status.endpoint;
  const keep = document.activeElement === select ? select.value : null;
  const own = el('option');
  own.value = '';
  retext(own, k('自己填地址'));
  const options = [own, ...endpoints.map((item) => {
    const option = el('option', null, item.name);
    option.value = item.id;
    return option;
  })];
  if (status.endpoint && !endpoints.some((item) => item.id === status.endpoint)) {  // 选的那个不在了：留着这一项，状态那一行说为什么
    const gone = el('option');
    gone.value = status.endpoint;
    retext(gone, k('（不在了）{name}'), { name: status.endpoint });
    options.push(gone);
  }
  select.replaceChildren(...options);
  select.value = keep !== null ? keep : status.endpoint || '';
  $('voice-url-row').hidden = Boolean(select.value);
}

$('voice-endpoint').addEventListener('change', () => {
  $('voice-url-row').hidden = Boolean($('voice-endpoint').value);
  if ($('voice-local-note').classList.contains('bad')) voiceLocalNote('', false);
});
