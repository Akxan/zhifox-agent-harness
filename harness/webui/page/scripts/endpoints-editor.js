// ---- 自定义模型接口 · 添加和编辑的窗口 ---------------------------------------------------------
// 叠在设置面板上面打开（设置还在底下，关上以后回到那一行）。上面是名字、地址、可选的密钥和"常用的几种"（点一下填好名字和地址），
// 中间是"测试连接""同步模型"，下面是这个接口上的模型：每个模型能做什么（看得懂图片 / 能用工具 / 能读 PDF，点一下切换）、
// 上下文上限、价格。
//
// 测试连接、同步模型都交给助手程序（/api/endpoint/probe）：它检查地址（外面的一律 https://，明文的 http:// 只许本机和内网）、
// 拿着密钥去问。页面自己从来不连那个地址，也拿不到保存着的密钥：编辑时密钥一栏空着，不改就交回 {keep: true}。
// "同步"以后哪些模型留下、你改过的设置怎么办，规矩在助手程序里（endpoints/spec.merged），页面照着它给的列表换上。
// 格式（地址、模型名、价格）也由它检查，不对的话把原因写在窗口底下（浮在页面上的提示会被暗底挡住）。
//
// 有没保存的修改时，Esc、×、点外面、"取消"都先提醒一次，再来一次才放弃。

const endpointDialog = $('endpoint-dialog');
let endpointDraft = null;  // 正在编辑的：{ id（新建的是空的）, name, hasKey, keyHint, where, models, base, armed, busy }
const ENDPOINT_PRESETS = [  // 几种常见的服务器默认的地址（两份 README 里也写着）
  { name: 'Ollama', url: 'http://localhost:11434/v1' },
  { name: 'LM Studio', url: 'http://localhost:1234/v1' },
  { name: 'vLLM', url: 'http://localhost:8000/v1' },
  { name: 'llama.cpp', url: 'http://localhost:8080/v1' },
];
const ENDPOINT_SERVERS = { ollama: 'Ollama', lmstudio: 'LM Studio' };  // 测试连接时认出来的服务器
// 每个模型能做什么：字段、图标、名字
const ENDPOINT_CAPS = [['vision', 'eye', k('看得懂图片')], ['tools', 'terminal', k('能用工具')], ['pdf', 'file', k('能读 PDF')]];

function endpointSay(text, tone) {
  const box = $('endpoint-message');
  box.textContent = text || '';
  box.classList.toggle('bad', tone === 'bad');
}

function endpointResult(text, tone) {
  const box = $('endpoint-result');
  box.textContent = text || '';
  box.className = 'endpoint-result' + (tone ? ' ' + tone : '');
  if (text) play(box, [{ opacity: 0, transform: 'translateY(-3px)' }, { opacity: 1, transform: 'none' }], 220);
}

// 程序给的一个模型（{id, vision, tools, pdf, context?, price?}）→ 窗口里改着的样子（数字先当文字留着，保存时再交回去）
function draftModel(model) {
  const price = model.price || null;
  return {
    id: model.id, vision: Boolean(model.vision), tools: model.tools !== false, pdf: Boolean(model.pdf),
    context: model.context ? String(model.context) : '',
    input: price ? String(price.input) : '', output: price ? String(price.output) : '',
    cache: price && price.cache_read !== undefined ? price.cache_read : null,
  };
}

// 窗口里的样子 → 交给程序的表单
function modelForm(model) {
  const number = (text) => (text.trim() === '' ? null : Number(text));
  const filled = model.input.trim() !== '' || model.output.trim() !== '';
  const price = filled ? { input: number(model.input), output: number(model.output) } : null;
  if (price && model.cache !== null) price.cache_read = model.cache;
  return { id: model.id, vision: model.vision, tools: model.tools, pdf: model.pdf, context: number(model.context), price };
}

function endpointKey() {
  const typed = $('endpoint-key').value.trim();
  if (typed) return typed;
  return endpointDraft && endpointDraft.hasKey && !$('endpoint-key-clear').checked ? { keep: true } : '';
}

function endpointForm(withModels) {
  const form = { name: $('endpoint-name').value, base_url: $('endpoint-url').value, api_key: endpointKey() };
  if (withModels) form.models = endpointDraft.models.map(modelForm);
  return form;
}

// "指纹"：和刚打开时不一样，就是有没保存的修改
const endpointPrint = () => JSON.stringify([$('endpoint-name').value, $('endpoint-url').value, $('endpoint-key').value, $('endpoint-key-clear').checked, endpointDraft ? endpointDraft.models : []]);
const endpointDirty = () => Boolean(endpointDraft) && endpointPrint() !== endpointDraft.base;

function openEndpointEditor(id) {
  const info = endpointsInfo || { items: [] };
  const item = id ? info.items.find((one) => one.id === id) : null;
  if (id && !item) { say(t('这个接口已经不在了，可能刚被删掉。'), 'bad'); return; }
  endpointDraft = {
    id: item ? item.id : '', slug: item ? item.slug : '', name: item ? item.name : '',
    hasKey: Boolean(item && item.has_key), keyHint: item ? item.key_hint : '', where: item ? item.where : '',
    models: item ? item.models.map(draftModel) : [], armed: false, busy: '',
  };
  $('endpoint-name').value = item ? item.name : '';
  $('endpoint-url').value = item ? item.base_url : '';
  $('endpoint-key').value = '';
  $('endpoint-key-clear').checked = false;
  $('endpoint-model-new').value = '';
  endpointSay('');
  endpointResult('');
  drawEndpointEditor();
  endpointDraft.base = endpointPrint();
  endpointDialog.showModal();
  (item ? $('endpoint-url') : $('endpoint-name')).focus();
}

function drawEndpointEditor(focus) {
  const draft = endpointDraft;
  if (!draft) return;
  const info = endpointsInfo || {};
  if (draft.id) retext($('endpoint-title'), k('编辑“{name}”'), { name: draft.name }); else retext($('endpoint-title'), k('添加模型接口'));
  // 编号：Agent 文件、命令行（以后的评测）都用"编号/模型名"指定它
  const ident = $('endpoint-id');
  ident.hidden = !draft.id;
  if (draft.id) retext(ident, k('编号是 {id}：Agent 文件和命令行里写成 {id}/模型名。改名字不会改它。'), { id: draft.id });
  tattr($('endpoint-key'), 'placeholder', draft.hasKey ? k('已保存一个密钥；留空就不改') : k('没有就空着'));
  $('endpoint-key-clear-box').hidden = !draft.hasKey;
  retext($('endpoint-defaults'), k('价格不填：本机和内网的接口算不花钱，外面的网关不计入估算。上下文不填按 {n} 个 token 算；Ollama 默认只给 4096，要用 OLLAMA_CONTEXT_LENGTH 调大，不然太长的对话会被它悄悄截掉开头。'), { n: Number(info.default_context || 0).toLocaleString(LOCALES[language]) });
  drawEndpointPresets();
  drawEndpointModels(focus);
  for (const [button, kind] of [[$('endpoint-test'), 'test'], [$('endpoint-sync'), 'sync']]) {
    button.disabled = Boolean(draft.busy);
    button.setAttribute('aria-busy', String(draft.busy === kind));
  }
  $('endpoint-save').disabled = Boolean(draft.busy);
}

function drawEndpointPresets() {
  const url = $('endpoint-url').value.trim().replace(/\/+$/, '');
  $('endpoint-presets').replaceChildren(...ENDPOINT_PRESETS.map((preset) => {
    const button = el('button', 'endpoint-preset');
    button.type = 'button';
    button.setAttribute('aria-pressed', String(url === preset.url));
    button.append(el('span', null, preset.name), el('small', null, preset.url.replace('http://', '')));
    button.addEventListener('click', () => usePreset(preset));
    return button;
  }));
}

// 点一下"常用的几种"：填好地址；名字空着（或者还是另一个预设的名字）的话也填上
function usePreset(preset) {
  const name = $('endpoint-name');
  if (!name.value.trim() || ENDPOINT_PRESETS.some((one) => one.name === name.value.trim())) name.value = preset.name;
  $('endpoint-url').value = preset.url;
  endpointTouched();
  drawEndpointPresets();
  endpointResult(t('地址填好了。确认 {name} 开着，再点“测试连接”。', { name: preset.name }));
}

function drawEndpointModels(focus) {
  const draft = endpointDraft;
  const list = $('endpoint-models');
  const had = list.contains(document.activeElement) ? document.activeElement.dataset.focus : '';
  const nodes = draft.models.map((model, index) => endpointModelRow(model, index));
  if (!nodes.length) nodes.push(tel('li', 'endpoint-models-empty', k('还没有模型。点“同步模型”向它要一份，或者在下面自己填模型名。')));
  list.replaceChildren(...nodes);
  const limit = (endpointsInfo && endpointsInfo.models_limit) || 0;
  $('endpoint-count').textContent = draft.models.length ? draft.models.length + (limit ? ' / ' + limit : '') : '';
  const wanted = focus || had;
  if (wanted) {
    const target = list.querySelector('[data-focus="' + CSS.escape(wanted) + '"]');
    (target || $('endpoint-model-new')).focus({ preventScroll: Boolean(target) });
  }
  if (draft.arrived) {
    const row = list.querySelector('[data-model="' + CSS.escape(draft.arrived) + '"]');
    if (row && !calm()) row.classList.add('arrive');
    draft.arrived = '';
  }
}

function endpointModelRow(model, index) {
  const row = el('li', 'endpoint-model');
  row.dataset.model = model.id;
  const top = row.appendChild(el('div', 'endpoint-model-top'));
  top.appendChild(el('code', 'endpoint-model-name', model.id));
  const remove = top.appendChild(el('button', 'icon-button small danger-hover'));
  remove.type = 'button';
  remove.appendChild(icon('x'));
  remove.title = t('去掉 {model}', { model: model.id });
  remove.setAttribute('aria-label', remove.title);
  remove.dataset.focus = 'remove:' + model.id;
  remove.addEventListener('click', () => {
    const next = endpointDraft.models[index + 1] || endpointDraft.models[index - 1];
    endpointDraft.models.splice(index, 1);
    endpointTouched();
    drawEndpointModels(next ? 'remove:' + next.id : 'add');
  });
  const caps = row.appendChild(el('div', 'endpoint-caps'));
  caps.setAttribute('role', 'group');
  caps.setAttribute('aria-label', t('{model} 能做什么', { model: model.id }));
  for (const [field, glyph, label] of ENDPOINT_CAPS) {
    const toggle = caps.appendChild(el('button', 'endpoint-cap'));
    toggle.type = 'button';
    toggle.setAttribute('aria-pressed', String(model[field]));
    toggle.dataset.focus = field + ':' + model.id;
    toggle.append(icon(glyph), tel('span', null, label));
    toggle.addEventListener('click', () => {
      model[field] = !model[field];
      endpointTouched();
      drawEndpointModels(field + ':' + model.id);
    });
  }
  const numbers = row.appendChild(el('div', 'endpoint-numbers'));
  const info = endpointsInfo || {};
  for (const [field, label, hint] of [
    ['context', k('上下文上限（token）'), String(info.default_context || '')],
    ['input', k('输入（美元 / 百万）'), t('默认')],
    ['output', k('输出（美元 / 百万）'), t('默认')],
  ]) {
    const box = numbers.appendChild(el('label', 'endpoint-number'));
    box.appendChild(tel('span', null, label));
    const field_ = box.appendChild(el('input', 'field'));
    field_.type = 'number';
    field_.min = '0';
    field_.step = field === 'context' ? '1' : 'any';
    field_.inputMode = field === 'context' ? 'numeric' : 'decimal';
    field_.placeholder = hint;
    field_.value = model[field];
    field_.dataset.focus = field + '-field:' + model.id;
    field_.addEventListener('input', () => { model[field] = field_.value; endpointTouched(); });
  }
  return row;
}

function addEndpointModel() {
  const field = $('endpoint-model-new');
  const name = field.value.trim();
  if (!name) { endpointSay(t('先写一个模型名，比如 qwen3:8b。'), 'bad'); field.focus(); return; }
  if (endpointDraft.models.some((model) => model.id === name)) {
    endpointSay(t('“{model}”已经在列表里了。', { model: name }), 'bad');
    drawEndpointModels('remove:' + name);
    return;
  }
  const blank = (endpointsInfo && endpointsInfo.blank) || {};
  endpointDraft.models.push(draftModel({ ...blank, id: name }));
  endpointDraft.arrived = name;
  field.value = '';
  endpointTouched();
  drawEndpointModels('add');
}

// 改了什么：之前提醒过"有没保存的修改"的，这一下算你接着改，不算放弃
function endpointTouched() {
  if (endpointDraft && endpointDraft.armed) { endpointDraft.armed = false; endpointSay(''); }
}

// 测试连接（sync 为假）、同步模型（为真）。请求从助手程序发出去
async function probeEndpoint(sync) {
  const draft = endpointDraft;
  if (!draft || draft.busy) return;
  draft.busy = sync ? 'sync' : 'test';
  endpointResult(sync ? t('正在向它要模型列表……') : t('正在连接……'));
  drawEndpointEditor();
  const reply = await call('/api/endpoint/probe', { id: draft.id || undefined, form: endpointForm(sync) });
  if (endpointDraft !== draft) return;  // 等回答的时候窗口被关掉了
  draft.busy = '';
  drawEndpointEditor();
  if (!reply.ok) { endpointResult(reply.error, 'bad'); return; }
  const data = reply.data;
  draft.where = data.where;
  // 结果分几行：找到了什么、认出来是哪种服务器、对话会去哪儿
  const server = ENDPOINT_SERVERS[data.server] ? t('认出来是 {server}。', { server: ENDPOINT_SERVERS[data.server] }) : '';
  const where = { local: t('这台电脑上的接口：对话不出这台电脑。'), lan: t('内网里的接口：对话不出你的网络。'), remote: t('外面的服务器：对话会发到那里去。') }[data.where] || '';
  const lines = (...parts) => parts.filter(Boolean).join('\n');
  if (!data.listed) {
    endpointResult(lines(t('连上了，但它不提供模型列表：请在下面自己填模型名。'), server, where), 'good');
    return;
  }
  // 同步：换成程序给的列表（你改过的设置它留着）。测试连接时列表还空着，也顺手填上
  const fill = sync || !draft.models.length;
  if (fill) {
    const known = new Set(draft.models.map((model) => model.id));
    draft.models = data.models.map(draftModel);
    draft.arrived = (draft.models.find((model) => !known.has(model.id)) || {}).id || '';
    endpointTouched();
    drawEndpointModels();
  }
  const count = data.models.length;
  const found = sync ? t('模型列表已同步：{n} 个，你改过的设置都留着。', { n: count })
    : fill ? t('连上了：找到 {n} 个模型，已经填进下面的列表。', { n: count }) : t('连上了：它有 {n} 个模型。', { n: count });
  endpointResult(lines(found, server, where), 'good');
}

async function saveEndpoint() {
  const draft = endpointDraft;
  if (!draft || draft.busy) return;
  draft.busy = 'save';
  endpointSay('');
  drawEndpointEditor();
  const reply = await call('/api/endpoint/save', { id: draft.id || undefined, form: endpointForm(true) });
  if (endpointDraft !== draft) return;
  draft.busy = '';
  drawEndpointEditor();
  if (!reply.ok) { endpointSay(reply.error, 'bad'); return; }
  const saved = reply.data.endpoint;
  endpointArrived = saved.id;
  endpointDraft = null;
  endpointDialog.close();
  say(t('已保存接口“{name}”。换模型的面板里可以换到它的模型。', { name: saved.name }), 'good');
  drawEndpoints(true);
}

function requestCloseEndpoint() {
  if (endpointDirty() && !endpointDraft.armed) {
    endpointDraft.armed = true;
    endpointSay(t('还有没保存的修改，再按一次就放弃。'), 'bad');
    return;
  }
  endpointDraft = null;
  endpointDialog.close();
}

for (const id of ['endpoint-name', 'endpoint-url', 'endpoint-key']) {
  $(id).addEventListener('input', () => { endpointTouched(); if (id === 'endpoint-url') drawEndpointPresets(); });
}
$('endpoint-key-clear').addEventListener('change', endpointTouched);
$('endpoint-test').addEventListener('click', () => probeEndpoint(false));
$('endpoint-sync').addEventListener('click', () => probeEndpoint(true));
$('endpoint-model-add').addEventListener('click', addEndpointModel);
$('endpoint-model-new').dataset.focus = 'add';
$('endpoint-model-new').addEventListener('keydown', (key) => {
  if (key.isComposing || key.keyCode === 229) return;  // 输入法正在组字
  if (key.key === 'Enter') { key.preventDefault(); addEndpointModel(); }
});
$('endpoint-save').addEventListener('click', saveEndpoint);
$('endpoint-cancel').addEventListener('click', requestCloseEndpoint);
$('close-endpoint').addEventListener('click', requestCloseEndpoint);
endpointDialog.addEventListener('keydown', (key) => {
  if (key.isComposing || key.keyCode === 229) return;
  if (key.key === 'Escape' && !key.defaultPrevented) { key.preventDefault(); key.stopPropagation(); requestCloseEndpoint(); }
  else if (key.key === 'Enter' && commandHeld(key)) { key.preventDefault(); saveEndpoint(); }  // ⌘Enter / Ctrl+Enter（见 platform.js）
});
// 保险：去掉一个模型时，拿着焦点的那个按钮没了，焦点掉到窗口外面，Esc 就直接变成 cancel
endpointDialog.addEventListener('cancel', (event) => { event.preventDefault(); requestCloseEndpoint(); });
endpointDialog.addEventListener('close', () => {
  endpointDraft = null;
  // 回到设置里的那一行（刚保存的、刚编辑的），没有的话回到"添加接口"
  if (settings.open && !settings.contains(document.activeElement)) $('endpoints-add').focus({ preventScroll: true });
});
// 点窗口外面的暗处：按下和松开都在暗处才算（免得在输入框里拖选文字时拖出去就关了）
{
  let pressedOutside = false;
  endpointDialog.addEventListener('mousedown', (press) => { pressedOutside = press.target === endpointDialog; });
  endpointDialog.addEventListener('click', (click) => { if (click.target === endpointDialog && pressedOutside) requestCloseEndpoint(); });
}
