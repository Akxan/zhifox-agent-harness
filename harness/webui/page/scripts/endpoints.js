// ---- 自定义模型接口 · 设置里的那一节 --------------------------------------------------------
// Ollama、LM Studio、vLLM、llama.cpp 的服务器、公司自己的网关……任何 OpenAI 格式的地址（背后是 harness/endpoints/）。
// 清单在状态里（state.endpoints，见 harness/webui/endpoints_api.py），页面照着画；加、改、删、测试连接都交给助手程序，
// 页面从来不直接连你填的地址（页面的安全策略也不许）。密钥只有"有没有"和最后 4 位。
// 每一行和别的本地的东西一样：悬停时右边是"在访达中显示 / 编辑 / 删除"（触屏上一直显示），删除先在原地确认（Esc 取消）。
// 点这一行的任何地方（或者"编辑"）打开编辑窗口（endpoints-editor.js）。窗口开着时，浮在页面上的提示会被暗底挡住，
// 所以结果写在设置面板底下（say，见 settings.js）。

let endpointsInfo = null;     // 状态里的 endpoints：{items, file, problem, limit, models_limit, default_context}
let endpointsDrawn = '';
let endpointDeleting = '';    // 正在原地确认删除的那一个（编号 custom-…）
let endpointBusy = false;     // 正在等助手程序回答
let endpointArrived = '';     // 刚保存的那一个：画出来时"落"一下

// 接口在哪儿（助手程序算好的，见 harness/endpoints/urls.py 的 where_is）
const ENDPOINT_WHERE = { local: k('这台电脑'), lan: k('内网'), remote: k('外面的服务器') };

// 状态里的清单变了：标题旁边写上有几个；设置开着就重画
function renderEndpoints(info) {
  endpointsInfo = info || null;
  const count = endpointsInfo ? endpointsInfo.items.length : 0;
  if (count) retext($('endpoints-state'), k('（{n} 个）'), { n: count }); else retext($('endpoints-state'), k('（还没有）'));
  if (settings.open) drawEndpoints();
}

function drawEndpoints(focus) {
  const info = endpointsInfo || { items: [], problem: null, limit: 0 };
  const print = JSON.stringify([info.items, info.problem, endpointDeleting, endpointBusy, language]);
  if (print === endpointsDrawn && !focus) return;
  endpointsDrawn = print;
  if (endpointDeleting && !info.items.some((item) => item.id === endpointDeleting)) endpointDeleting = '';
  const list = $('endpoints-list');
  const had = list.contains(document.activeElement) ? document.activeElement.dataset.focus : '';
  list.replaceChildren(...(info.items.length ? info.items.map(endpointRow) : [endpointEmpty()]));
  // 文件坏了、有接口读不懂：程序给的那句话（带着原文，换语言时跟着换）
  const problem = $('endpoints-problem');
  problem.hidden = !info.problem;
  if (info.problem && info.problem.key) retext(problem, info.problem.key, info.problem.values);
  else if (info.problem) problem.textContent = info.problem.text || '';
  $('endpoints-add').disabled = Boolean(info.limit) && info.items.length >= info.limit;
  // 刚保存的那一个：保存的回答可能比新的状态先到，那时列表里还没有它。等它出现了再"落"一下、把焦点给它
  const arrived = endpointArrived && list.querySelector('[data-id="' + CSS.escape(endpointArrived) + '"]');
  if (arrived) {
    if (!calm()) arrived.classList.add('arrive');
    endpointArrived = '';
  }
  const wanted = arrived ? 'open:' + arrived.dataset.id : focus || had;
  if (wanted) {
    const target = list.querySelector('[data-focus="' + CSS.escape(wanted) + '"]');
    if (target || !endpointArrived) (target || $('endpoints-add')).focus({ preventScroll: true });
  }
}

function endpointEmpty() {
  const empty = el('li', 'endpoint-empty');
  empty.append(icon('server'), tel('span', null, k('还没有。点“添加接口”，接上这台电脑上的 Ollama、LM Studio，或者公司的网关。')));
  return empty;
}

// 一行下面那几个小标签：在哪儿、几个模型、密钥的最后 4 位、正在用
function endpointChips(item) {
  const chips = el('span', 'endpoint-chips');
  chips.appendChild(tel('span', 'endpoint-chip ' + item.where, ENDPOINT_WHERE[item.where] || ENDPOINT_WHERE.remote));
  const count = item.models.length;
  chips.appendChild(count === 1 ? tel('span', 'endpoint-chip', k('1 个模型')) : tel('span', 'endpoint-chip', k('{n} 个模型'), { n: count }));
  if (item.has_key) chips.appendChild(tel('span', 'endpoint-chip', k('密钥 {hint}'), { hint: item.key_hint }));
  if (item.in_use) chips.appendChild(tel('span', 'endpoint-chip used', k('正在用')));
  return chips;
}

function endpointRow(item) {
  const row = el('li', 'endpoint-row');
  row.dataset.id = item.id;
  const glyph = row.appendChild(el('span', 'endpoint-glyph ' + item.where + (item.in_use ? ' used' : '')));
  glyph.setAttribute('aria-hidden', 'true');
  glyph.appendChild(icon('server'));
  if (endpointDeleting === item.id) {
    const box = row.appendChild(el('div', 'endpoint-confirm'));
    box.appendChild(item.in_use
      ? tel('span', null, k('正在用“{name}”上的模型：先在换模型的面板里换一个，再删除它。'), { name: item.name })
      : tel('span', null, k('删掉“{name}”？用它聊过的会话要重新添加一个同名的接口才能接着聊。'), { name: item.name }));
    const yes = box.appendChild(tel('button', 'mini danger', k('删除')));
    const no = box.appendChild(tel('button', 'mini cancel', k('取消')));
    yes.type = no.type = 'button';
    yes.disabled = endpointBusy || item.in_use;
    no.disabled = endpointBusy;
    no.dataset.focus = 'cancel:' + item.id;
    yes.addEventListener('click', () => removeEndpoint(item));
    no.addEventListener('click', () => { endpointDeleting = ''; drawEndpoints('delete:' + item.id); });
    box.addEventListener('keydown', (key) => {
      if (key.key !== 'Escape' || key.isComposing) return;
      key.preventDefault();
      key.stopPropagation();
      endpointDeleting = '';
      drawEndpoints('delete:' + item.id);
    });
    return row;
  }
  // 点这一行的任何地方都是"编辑"：一个透明的按钮铺满这一行，文字和小标签在它上面但不接点击
  const open = row.appendChild(el('button', 'endpoint-open'));
  open.type = 'button';
  open.dataset.focus = 'open:' + item.id;
  open.setAttribute('aria-label', t('编辑接口：{name}', { name: item.name }));
  open.addEventListener('click', () => openEndpointEditor(item.id));
  const words = row.appendChild(el('span', 'endpoint-words'));
  words.appendChild(el('span', 'endpoint-name', item.name));
  words.appendChild(el('span', 'endpoint-url', item.base_url));
  words.appendChild(endpointChips(item));
  const actions = row.appendChild(el('div', 'endpoint-actions'));
  const reveal = actions.appendChild(iconButton('folder', byServer(k('在访达中显示 endpoints.json'), k('在文件资源管理器中显示 endpoints.json'), k('在文件管理器中显示 endpoints.json'))));
  const edit = actions.appendChild(iconButton('edit', k('编辑')));
  const trash = actions.appendChild(iconButton('trash', k('删除')));
  trash.classList.add('danger-hover');
  reveal.dataset.focus = 'reveal:' + item.id;
  edit.dataset.focus = 'edit:' + item.id;
  trash.dataset.focus = 'delete:' + item.id;
  for (const button of [reveal, edit, trash]) button.disabled = endpointBusy;
  reveal.addEventListener('click', revealEndpoints);
  edit.addEventListener('click', () => openEndpointEditor(item.id));
  trash.addEventListener('click', () => { endpointDeleting = item.id; drawEndpoints('cancel:' + item.id); });
  return row;
}

async function removeEndpoint(item) {
  endpointBusy = true;
  drawEndpoints();
  const reply = await call('/api/endpoint/delete', { id: item.id });
  endpointBusy = false;
  if (!reply.ok) { drawEndpoints('cancel:' + item.id); say(reply.error, 'bad'); return; }
  endpointDeleting = '';
  say(t('已删除接口“{name}”。', { name: item.name }), 'good');
  announce(t('已删除接口“{name}”。', { name: item.name }));
  drawEndpoints();
  $('endpoints-add').focus({ preventScroll: true });
}

async function revealEndpoints() {
  const reply = await call('/api/endpoint/reveal', {});
  if (!reply.ok) { say(reply.error, 'bad'); return; }
  say(revealedText(reply.data.path));
}

// 打开设置，翻到这一节（换模型的面板里"编辑接口"用的）。id 给了就直接打开那一个的编辑窗口
function openSettingsEndpoint(id) {
  openSettings();
  $('endpoints-section').scrollIntoView({ block: 'start', behavior: calm() ? 'auto' : 'smooth' });
  if (id) openEndpointEditor(id);
}

$('endpoints-add').addEventListener('click', () => openEndpointEditor(null));
