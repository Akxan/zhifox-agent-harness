// ---- 读网页 --------------------------------------------------------------------
// 设置里"读网页"一节：一个开关（state.web_fetch），管的是给自己不能联网的模型用的 web_fetch（harness/fetch/）。
// 怎么取、哪些地址不去、问不问你都在程序里（fetch/、permissions.py），页面只管开和关，不另抄一份规矩。
// 默认开着；开、关都不用确认（开着也是每个新网站第一次读之前先问你）。改了马上保存（/api/web-fetch）：
// 这段对话还没开始的话马上换上，已经开始了的从下一段对话开始，服务器回答里的 warning 照着说。

let fetchSaving = false;  // 正在保存：这期间开关不能再点

function renderFetch(on) {
  if (typeof on !== 'boolean') { $('fetch-section').hidden = true; return; }  // 旧的助手程序没有这一项
  $('fetch-section').hidden = false;
  const toggle = $('fetch-enabled');
  toggle.setAttribute('aria-checked', String(on));
  toggle.disabled = fetchSaving;
  retext($('fetch-state'), on ? k('（开着）') : k('（关着）'));
}

async function saveFetch(on) {
  fetchSaving = true;
  $('fetch-enabled').disabled = true;
  const reply = await call('/api/web-fetch', { enabled: on });
  fetchSaving = false;
  $('fetch-enabled').disabled = false;
  if (!reply.ok) { say(reply.error, 'bad'); return; }
  renderFetch(on);  // 不等状态：马上画成新的样子（状态随后就到，和这里一样）
  if (reply.warning) say(reply.warning, 'bad');
  else say(on ? t('已打开：没有联网工具的模型能读你给的网址了，每个新网站第一次读之前先问你。') : t('已关掉：没有联网工具的模型不再读网页。'), 'good');
}

$('fetch-enabled').addEventListener('click', () => {
  if (fetchSaving) return;
  saveFetch($('fetch-enabled').getAttribute('aria-checked') !== 'true');
});
