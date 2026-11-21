// ---- 换模型 ------------------------------------------------------------------
// 顶栏中间那个写着"Claude (Anthropic) · claude-opus-5-5"的按钮，点开是换模型的小面板（设置在旁边的齿轮里，见 settings.js）。
// 面板里按家分组：这一家有没有密钥（没有的给一个"填写 API 密钥 →"，打开设置里填这一家密钥的那一行）、能选的模型、
// 每个模型能做什么（能联网、看得懂图片……）和价格。这些都由助手程序算好（harness/model_catalog.py，
// 接口是 /api/model/catalog），页面只照着画，不自己判断。
//
// 换过去：同一家之内马上换，对话接着进行，结果写在面板底下（浮在页面上的提示会被面板盖住）；换到另一家要开始一段
// 新对话。这段对话已经聊过的话，先在面板里问一声（现在这段已经存好了，随时可以回来接着聊）。"会怎样"也是程序说的：
// 目录里每一家的 switch（continue 接着聊 / fresh 这段还没聊、直接换 / leave 先问一声）。
// 用 Agent 开始的对话，换模型不换角色（底下写一句）；系统提示词和工具在一段对话里不变，这由程序保证（见 App.select_model）。
//
// 键盘：↑↓ 在模型之间移动（Home / End 到头尾），回车换过去，直接打字就是搜索（模型多的时候搜索框一直在顶上），
// Esc 收起来、焦点回到按钮上（正在问"要不要开始新对话"时，Esc 先收起这一问）；开着的时候 Tab 只在面板里转。
// 快捷键 ⌘. / Ctrl+. 打开它（见 keyboard.js）。整个列表是一个菜单（role=menu）：每家一组（role=group），
// 模型是 menuitemradio（现在用的那个 aria-checked），"同步模型列表""填写 API 密钥"是 menuitem。
//
// 目录不在每次的状态里（OpenAI 的模型有上百个），面板打开时取一次；开着的时候状态变了（换了模型、填了密钥、
// 同步了列表、这一轮聊完了），再取一次（见 handlers.state 里的 refreshModels）。

const modelButton = $('model'), modelsPanel = $('models'), modelsMenu = $('models-menu');
const modelsFind = $('models-find'), modelsSearch = $('models-search');
const MODELS_SEARCH_FROM = 9;  // 一共有这么多个模型时，搜索框一直显示在顶上；少的时候直接打字，它才出来
const MODELS_KEY = () => forKeys('⌘.', 'Ctrl+.');  // 打开面板的快捷键（见 keyboard.js；Mac 以外写 Ctrl，见 platform.js）

let modelsCatalog = null;   // 最近一次取到的目录（格式见 harness/model_catalog.py 开头）
let modelsLoading = false;  // 正在取目录
let modelsAgain = false;    // 取的时候状态又变了：取完再取一次
let modelsAsking = null;    // 正在问"换到另一家会开始新对话"的那一个：{ provider, model }
let modelsSwitching = '';   // 正在换过去的那一个（请求还没回来）："家/模型"
let modelsChecked = '';     // 上一次画的时候打勾的是哪一个：换了的话，新的那个勾"落"一下
const modelsSyncing = new Set();  // 正在同步模型列表的那几家

const modelsFlyout = flyout(modelsPanel, modelButton, {
  sheet: true,
  trap: true,
  focus: () => modelsStop() || (modelsFind.hidden ? modelsMenu : modelsSearch),  // 目录还没取到时先放在列表上
  escape: () => {
    if (!modelsAsking) return false;
    stopAsking(true);  // 先收起"要不要开始新对话"这一问，焦点回到那一行上
    return true;
  },
  closed: () => {
    modelsAsking = null;
    $('models-confirm').hidden = true;
    modelsSearch.value = '';
  },
});

// 菜单里能用方向键走到的那几项（看得见的）
const modelsItems = () => [...modelsMenu.querySelectorAll('[role="menuitemradio"], [role="menuitem"]')];
modelsMenu.tabIndex = -1;  // 列表本身只在目录还没取到时临时拿一下焦点，Tab 走不到它
// 打开时焦点放在谁身上：现在用的那个模型，没有的话第一项
const modelsStop = () => modelsMenu.querySelector('[aria-checked="true"]') || modelsItems()[0];

// 按钮的提示里写上快捷键（换语言时重写一遍，见 applyLanguage）。按钮上的字是现在的模型；窄屏上字藏起来了，这句提示就是它的名字
function retitleModelButton() {
  modelButton.title = t('换模型（{key}）', { key: MODELS_KEY() });
}
retitleModelButton();

function usd(value) {
  const whole = Number.isInteger(value);
  return new Intl.NumberFormat(LOCALES[language], {
    style: 'currency', currency: 'USD', currencyDisplay: 'narrowSymbol',
    minimumFractionDigits: whole ? 0 : 2, maximumFractionDigits: whole ? 0 : 4,
  }).format(value);
}

// 面板底下的一句话（结果、出错）。key 是原文的话换语言时跟着换；程序给的出错原因原样显示
function modelsSay(text, values, kind) {
  const box = $('models-status');
  if (text && kind !== 'error') retext(box, text, values);
  else { delete box.dataset.i18n; delete box.dataset.i18nValues; box.textContent = text || ''; }
  box.className = 'models-status' + (kind ? ' ' + (kind === 'error' ? 'bad' : kind) : '');
  if (text) play(box, [{ opacity: 0, transform: 'translateY(-3px)' }, { opacity: 1, transform: 'none' }], 220);
}

async function refreshModels() {
  if (modelsLoading) { modelsAgain = true; return; }
  modelsLoading = true;
  const result = await call('/api/model/catalog');
  modelsLoading = false;
  if (result.ok && result.data && result.data.catalog) {
    // 和上一次一样就不重画（一轮对话里每调用一次模型都会发一次状态，面板开着时每次都来取）：免得悬停、焦点跟着闪
    const same = modelsCatalog && JSON.stringify(modelsCatalog) === JSON.stringify(result.data.catalog);
    modelsCatalog = result.data.catalog;
    if (modelsFlyout.isOpen() && !same) {
      drawModels();
      // 第一次打开时还没有目录，焦点先放在列表上（见 openModels）：画好了交给现在用的那个模型
      if (document.activeElement === modelsMenu) { const stop = modelsStop(); if (stop) stop.focus({ preventScroll: true }); }
    }
  } else if (modelsFlyout.isOpen() && !modelsCatalog) {
    modelsSay(result.error || t('没能读取模型列表。'), null, 'error');
  }
  if (modelsAgain) { modelsAgain = false; refreshModels(); }
}

function openModels() {
  if (!state) return;
  modelsSay('');
  drawModels();  // 先照上一次取到的画出来（第一次是"正在读取"），取到新的再换上
  modelsFlyout.open();
  refreshModels();
}

// 一句程序给的话（目录里的标签）：照原文译，换语言时跟着换。和信息栏里"智狐能做的事"同一个写法（capText）
const modelsText = (node, said) => capText(node, said);

function drawModels() {
  const catalog = modelsCatalog;
  const query = modelsSearch.value.trim().toLowerCase();
  const focusedKey = modelsPanel.contains(document.activeElement) ? document.activeElement.dataset.modelsFocus : '';
  const total = catalog ? catalog.groups.reduce((sum, group) => sum + group.models.length, 0) : 0;
  modelsFind.hidden = !(query || total >= MODELS_SEARCH_FROM || document.activeElement === modelsSearch);
  const nodes = [];
  if (!catalog) nodes.push(tel('p', 'models-empty', k('正在读取模型列表……')));
  for (const group of (catalog ? catalog.groups : [])) {
    const labelHit = group.label.toLowerCase().includes(query);
    const models = group.models.filter((model) => !query || labelHit || model.id.toLowerCase().includes(query));
    if (query && !models.length && !labelHit) continue;
    nodes.push(modelsGroup(group, models, query));
  }
  if (catalog && !nodes.length) nodes.push(tel('p', 'models-empty', k('没有找到“{query}”。'), { query: modelsSearch.value.trim() }));
  modelsMenu.replaceChildren(...nodes);
  markNextModel();
  drawModelsPersona(catalog);

  // 现在用的那个换了（刚换过去）：新的勾"落"一下。第一次画、重新打开时不播
  const checked = modelsMenu.querySelector('[aria-checked="true"]');
  const now = checked ? checked.dataset.modelsFocus : '';
  if (checked && modelsChecked && now !== modelsChecked) {
    play(checked.querySelector('.models-check .icon'), [{ opacity: 0, transform: 'scale(.3) rotate(-20deg)' }, { opacity: 1, transform: 'none' }], 360);
  }
  if (now) modelsChecked = now;

  // 画之前焦点在哪一项上，画完还放回那一项（状态更新、同步完列表都会重画）
  const again = focusedKey && modelsMenu.querySelector('[data-models-focus="' + CSS.escape(focusedKey) + '"]');
  const stop = again || modelsStop();
  for (const item of modelsItems()) item.tabIndex = item === stop ? 0 : -1;
  if (again) again.focus({ preventScroll: true });
  else if (focusedKey && focusedKey.startsWith('m:') && stop) stop.focus({ preventScroll: true });  // 那一项没了（比如搜不到了）：给现在用的那一个
}

function modelsGroup(group, models, query) {
  const section = el('div', 'models-group');
  section.setAttribute('role', 'group');
  section.setAttribute('aria-labelledby', 'models-g-' + group.id);
  const head = section.appendChild(el('div', 'models-head'));
  head.setAttribute('role', 'none');
  const name = head.appendChild(el('span', 'models-label', group.label));
  name.id = 'models-g-' + group.id;
  const key = head.appendChild(el('span', 'models-key' + (group.has_key ? ' set' : '')));
  if (group.has_key) retext(key, k('已设置 {hint}'), { hint: group.key_hint || '' });
  else retext(key, k('未设置'));
  if (!group.has_key) {
    // 没有密钥：打开设置里填这一家密钥的那一行（见 settings.js 的 openSettingsKeys）
    const link = head.appendChild(el('button', 'models-keylink'));
    link.type = 'button';
    link.setAttribute('role', 'menuitem');
    link.dataset.modelsFocus = 'key:' + group.id;
    link.append(tel('span', null, k('填写 API 密钥')), icon('chevron'));
    link.setAttribute('aria-label', t('填写 {provider} 的 API 密钥', { provider: group.label }));
    link.addEventListener('click', () => { modelsFlyout.close(false); openSettingsKeys(group.id); });
    return section;
  }
  const syncing = modelsSyncing.has(group.id);
  if (group.models.length) {
    const sync = head.appendChild(el('button', 'icon-button small models-sync'));
    sync.type = 'button';
    sync.setAttribute('role', 'menuitem');
    sync.dataset.modelsFocus = 'sync:' + group.id;
    sync.title = t('同步 {provider} 的模型列表', { provider: group.label });
    sync.setAttribute('aria-label', sync.title);
    sync.setAttribute('aria-busy', String(syncing));
    sync.appendChild(icon('refresh'));
    sync.addEventListener('click', () => syncModels(group));
  } else {
    // 有密钥，还没有模型列表（保存密钥时没同步成）：这一组只有"同步模型列表"一项
    const sync = section.appendChild(el('button', 'models-item models-action'));
    sync.type = 'button';
    sync.setAttribute('role', 'menuitem');
    sync.dataset.modelsFocus = 'sync:' + group.id;
    sync.setAttribute('aria-busy', String(syncing));
    const mark = sync.appendChild(el('span', 'models-check'));
    mark.appendChild(icon('refresh'));
    const words = sync.appendChild(el('span', 'models-words'));
    words.appendChild(tel('b', 'models-name', k('同步模型列表')));
    words.appendChild(tel('span', 'models-tags', k('还没有 {provider} 的模型列表，点一下向它要一份。'), { provider: group.label }));
    sync.addEventListener('click', () => syncModels(group));
  }
  for (const model of models) section.appendChild(modelsItem(group, model));
  return section;
}

function modelsItem(group, model) {
  const id = group.id + '/' + model.id;
  const item = el('button', 'models-item');
  item.type = 'button';
  item.setAttribute('role', 'menuitemradio');
  item.setAttribute('aria-checked', String(Boolean(model.current)));
  item.dataset.modelsFocus = 'm:' + id;
  item.dataset.provider = group.id;
  item.dataset.model = model.id;
  if (modelsAsking && modelsAsking.provider === group.id && modelsAsking.model === model.id) item.classList.add('models-asking');
  if (modelsSwitching === id) item.setAttribute('aria-busy', 'true');
  item.appendChild(el('span', 'models-check')).appendChild(icon('check'));
  const words = item.appendChild(el('span', 'models-words'));
  words.appendChild(el('b', 'models-name', model.id));
  const tags = words.appendChild(el('span', 'models-tags'));
  for (const tag of model.tags || []) tags.appendChild(modelsText(el('span'), tag));
  const price = item.appendChild(el('span', 'models-price'));
  if (model.price) {
    price.append(el('b', null, usd(model.price.input)), el('i', null, '/'), el('b', null, usd(model.price.output)));
    let title = t('输入 {input}、输出 {output}（美元 / 每百万 token）', { input: usd(model.price.input), output: usd(model.price.output) });
    if (model.offpeak) title += '\n' + t('闲时：输入 {input}、输出 {output}', { input: usd(model.offpeak.input), output: usd(model.offpeak.output) });
    price.title = title;
  } else {
    price.classList.add('none');
    retext(price, k('没有价格'));
    price.title = t('没有价格的模型不计入估算的费用。可以在设置里自己填。');
  }
  item.addEventListener('click', () => chooseModel(group, model));
  return item;
}

// 用 Agent 开始的对话：换模型不换角色
function drawModelsPersona(catalog) {
  const box = $('models-persona');
  const persona = catalog && catalog.persona;
  box.hidden = !persona;
  if (!persona) return;
  const parts = [tel('span', null, k('这段对话的角色是「{name}」。换模型不换角色：同一家之内接着聊，换到另一家，新的一段也由它开始。'), { name: persona.name })];
  if (persona.model) parts.push(document.createTextNode(' '), tel('span', null, k('它的设定里选的是 {model}。'), { model: persona.model }));
  box.replaceChildren(...parts);
}

// 搜索框里有字时，第一个找到的模型淡淡地亮着：这时按回车换的就是它
function markNextModel() {
  for (const item of modelsMenu.querySelectorAll('.models-next')) item.classList.remove('models-next');
  if (document.activeElement !== modelsSearch || !modelsSearch.value.trim()) return;
  const first = modelsMenu.querySelector('[role="menuitemradio"]');
  if (first) first.classList.add('models-next');
}

async function chooseModel(group, model) {
  if (modelsSwitching) return;
  if (model.current) { modelsFlyout.close(true); return; }  // 选的就是现在用的：收起来就好
  const asked = modelsAsking && modelsAsking.provider === group.id && modelsAsking.model === model.id;
  if (group.switch === 'leave' && !asked) { askBeforeLeaving(group, model); return; }
  stopAsking(false);
  modelsSwitching = group.id + '/' + model.id;
  modelsSay(k('正在换成 {model}……'), { model: model.id });
  drawModels();
  const result = await call('/api/model', { provider: group.id, model: model.id });
  modelsSwitching = '';
  if (modelsFlyout.isOpen()) drawModels();  // 收起那一行的"正在换"（没换成的话目录没变，下面不会重画）
  if (result.ok) {
    modelsSay(group.switch === 'continue' ? k('已换成 {model}，对话接着进行。') : k('已换成 {provider} · {model}，开始了一段新对话。'),
              { provider: group.label, model: model.id }, 'good');
  } else {
    modelsSay(result.error, null, 'error');
  }
  await refreshModels();
  if (!modelsFlyout.isOpen()) return;
  const row = modelsMenu.querySelector('[data-models-focus="' + CSS.escape('m:' + group.id + '/' + model.id) + '"]');
  if (row && !modelsPanel.contains(document.activeElement)) row.focus({ preventScroll: true });
}

// 换到另一家、这段对话已经聊过：在面板底下问一声，"换过去"拿着焦点（回车就是确认，Esc 收起这一问）
function askBeforeLeaving(group, model) {
  modelsAsking = { provider: group.id, model: model.id, label: group.label };
  retext($('models-confirm-text'), k('换到 {provider} 会开始一段新对话。现在这段已经保存，随时可以回来接着聊。'), { provider: group.label });
  $('models-confirm').hidden = false;
  modelsSay('');
  drawModels();
  $('models-confirm-yes').focus();
}

function stopAsking(refocus) {
  const asked = modelsAsking;
  modelsAsking = null;
  $('models-confirm').hidden = true;
  if (!asked) return;
  for (const item of modelsMenu.querySelectorAll('.models-asking')) item.classList.remove('models-asking');
  const row = modelsMenu.querySelector('[data-models-focus="' + CSS.escape('m:' + asked.provider + '/' + asked.model) + '"]');
  if (refocus && row) row.focus();
}

async function syncModels(group) {
  if (modelsSyncing.has(group.id)) return;
  modelsSyncing.add(group.id);
  modelsSay(k('正在同步 {provider} 的模型列表……'), { provider: group.label });
  drawModels();
  const result = await call('/api/models', { provider: group.id });
  modelsSyncing.delete(group.id);
  if (modelsFlyout.isOpen()) drawModels();  // 停下同步按钮的转圈（列表没变的话目录也没变，下面不会重画）
  if (result.ok) modelsSay(k('{provider} 的模型列表已更新。'), { provider: group.label }, 'good');
  else modelsSay(result.error, null, 'error');
  refreshModels();
}

modelButton.addEventListener('click', () => (modelsFlyout.isOpen() ? modelsFlyout.close(false) : openModels()));
modelButton.addEventListener('keydown', (key) => {
  if ((key.key === 'ArrowDown' || key.key === 'ArrowUp') && !modelsFlyout.isOpen()) { key.preventDefault(); openModels(); }
});
$('models-confirm-yes').addEventListener('click', () => {
  if (!modelsAsking || !modelsCatalog) return;
  const group = modelsCatalog.groups.find((one) => one.id === modelsAsking.provider);
  const model = group && group.models.find((one) => one.id === modelsAsking.model);
  if (model) chooseModel(group, model); else stopAsking(false);
});
$('models-confirm-no').addEventListener('click', () => stopAsking(true));

modelsSearch.addEventListener('input', () => { stopAsking(false); drawModels(); });
modelsSearch.addEventListener('focus', markNextModel);
modelsSearch.addEventListener('blur', () => setTimeout(() => {
  markNextModel();
  if (!modelsSearch.value.trim() && modelsCatalog && document.activeElement !== modelsSearch) drawModels();  // 搜索框空着、模型又不多：收起来
}, 0));
modelsSearch.addEventListener('keydown', (key) => {
  if (key.isComposing) return;
  const items = modelsItems();
  if (key.key === 'ArrowDown' || key.key === 'ArrowUp') {
    key.preventDefault();
    const target = key.key === 'ArrowDown' ? (modelsMenu.querySelector('.models-next') || items[0]) : items[items.length - 1];
    if (target) target.focus();
  } else if (key.key === 'Enter') {
    key.preventDefault();
    const first = modelsMenu.querySelector('.models-next') || modelsMenu.querySelector('[role="menuitemradio"]');
    if (first) first.click();
  }
});

modelsMenu.addEventListener('focusin', (focus) => {
  // 焦点走到哪一项，Tab 的停靠点就跟到哪一项（菜单整个只占一个 Tab 停靠点）
  if (!focus.target.dataset.modelsFocus) return;
  for (const item of modelsItems()) item.tabIndex = item === focus.target ? 0 : -1;
});
modelsMenu.addEventListener('keydown', (key) => {
  if (key.isComposing) return;
  const items = modelsItems();
  const at = items.indexOf(document.activeElement);
  const next = { ArrowDown: at + 1, ArrowUp: at - 1, Home: 0, End: items.length - 1 }[key.key];
  if (next !== undefined && items.length) {
    key.preventDefault();
    items[(next + items.length) % items.length].focus();
    return;
  }
  // 直接打字就是搜索：搜索框出来、接住这个字
  if (key.key.length === 1 && key.key !== ' ' && !key.ctrlKey && !key.metaKey && !key.altKey) {
    key.preventDefault();
    modelsFind.hidden = false;
    modelsSearch.focus();
    modelsSearch.value += key.key;
    modelsSearch.dispatchEvent(new Event('input'));
  }
});
