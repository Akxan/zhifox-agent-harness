// ---- 设置面板 ------------------------------------------------------------
// 右上角齿轮打开的设置面板：填或清除密钥、改这个模型的价格，以及在面板里说结果的 say()。面板里浏览器、操作整台电脑、
// 后台运行、预算和导出那几节，各自在 browser.js、computer.js、service.js、spending.js 里画。
// 换模型不在这里：顶上的模型按钮打开专门的面板（models.js）。这里的"当前模型"只写现在用的是哪个，给一个去那儿的按钮，
// 规矩（同一家接着聊、换到另一家开始新对话）只在那边和程序里各写一处。

let settingsDrawn = '';

function say(text, kind) {
  const box = $('settings-message');
  box.textContent = text;
  box.className = 'settings-message ' + (kind || '');
}

// 把最新的状态画到设置面板上。面板开着的时候状态可能更新，所以要保留用户正在输入的内容和焦点。
function renderSettings() {
  const print = JSON.stringify([state.providers, state.price, state.provider, state.model, language]);
  if (settings.dataset.ready && print === settingsDrawn) return;  // 和面板有关的内容没变，不用重画
  settingsDrawn = print;

  const typed = {};
  for (const field of settings.querySelectorAll('input[data-keep]')) typed[field.dataset.keep] = field.value;
  const focused = settings.contains(document.activeElement) ? document.activeElement.dataset.keep : null;
  settings.dataset.ready = '1';

  const current = state.providers.find((provider) => provider.id === state.provider);
  $('settings-model-name').textContent = (current ? current.label + ' · ' : '') + state.model;

  // 自定义模型接口不在这里：它们的密钥是可选的，有自己的一节（endpoints.js）
  $('keys').replaceChildren(...state.providers.filter((provider) => !provider.custom).map((provider) => {
    const row = el('div', 'key-row');
    const head = row.appendChild(el('div', 'key-head'));
    head.appendChild(el('strong', null, provider.label));
    head.appendChild(el('span', 'tag' + (provider.has_key ? ' set' : ''),
      provider.has_key ? t('已设置 {hint}', { hint: provider.key_hint || '' }) : t('未设置')));
    const models = provider.models.length;
    if (models) head.appendChild(el('span', 'tag', models === 1 ? t('1 个模型') : t('{n} 个模型', { n: models })));
    const link = head.appendChild(el('a', null, t('去申请')));
    link.href = provider.key_page;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.appendChild(icon('external'));

    const controls = row.appendChild(el('div', 'row'));
    const field = controls.appendChild(el('input', 'field'));
    field.type = 'password';
    field.autocomplete = 'off';
    field.spellcheck = false;
    field.placeholder = provider.has_key ? t('粘贴新的 API 密钥可以替换') : t('粘贴 API 密钥');
    field.setAttribute('aria-label', t('{provider} 的 API 密钥', { provider: provider.label }));
    field.dataset.keep = 'key-' + provider.id;
    field.value = typed[field.dataset.keep] || '';
    const save = controls.appendChild(el('button', 'button primary', t('保存')));
    const sync = controls.appendChild(el('button', 'button', t('同步模型')));
    const remove = controls.appendChild(el('button', 'button', t('删除')));
    save.type = sync.type = remove.type = 'button';
    sync.disabled = remove.disabled = !provider.has_key;

    save.addEventListener('click', async () => {
      const key = field.value.trim();
      if (!key) { say(t('请先粘贴 API 密钥。'), 'bad'); return; }
      field.value = '';  // 密钥一交出去就从输入框里清掉，不在页面上多留
      say(t('正在保存并同步模型列表……'));
      const result = await call('/api/key', { provider: provider.id, key });
      if (!result.ok && field.isConnected && !field.value) field.value = key;  // 没存成：放回去，改一改就能再试
      say(result.ok ? result.warning || t('{provider} 的密钥已保存，模型列表已同步。', { provider: provider.label }) : result.error,
          result.ok && !result.warning ? 'good' : 'bad');
    });
    field.addEventListener('keydown', (key) => {
      if (key.key === 'Enter' && !key.isComposing) { key.preventDefault(); save.click(); }
    });
    sync.addEventListener('click', async () => {
      say(t('正在同步 {provider} 的模型列表……', { provider: provider.label }));
      const result = await call('/api/models', { provider: provider.id });
      say(result.ok ? t('{provider} 的模型列表已更新。', { provider: provider.label }) : result.error, result.ok ? 'good' : 'bad');
    });
    // 删除密钥要点两下：第一下只是让按钮变成"确认删除"，过几秒不点就恢复原样
    let armed = false, armedAt = 0;
    remove.addEventListener('click', async () => {
      if (armed && performance.now() - armedAt < 500) return;  // 双击的第二下不算确认
      if (!armed) {
        armed = true;
        armedAt = performance.now();
        remove.textContent = t('确认删除');
        remove.classList.add('danger');
        setTimeout(() => { armed = false; remove.textContent = t('删除'); remove.classList.remove('danger'); }, 4000);
        return;
      }
      const result = await call('/api/key', { provider: provider.id, key: '' });
      say(result.ok ? t('{provider} 的密钥已删除。', { provider: provider.label }) : result.error, result.ok ? 'good' : 'bad');
    });
    return row;
  }));

  // 当前模型的价格
  const price = state.price || {};
  const fields = [['input', t('输入')], ['cache_read', t('命中缓存的输入')], ['output', t('输出')]];
  const inputs = {};
  const priceRow = $('price');
  priceRow.replaceChildren();
  for (const [key, label] of fields) {
    const wrap = priceRow.appendChild(el('label', null, label));
    const field = wrap.appendChild(el('input', 'field'));
    field.type = 'number';
    field.min = '0';
    field.step = 'any';
    field.dataset.keep = 'price-' + key;
    field.value = typed[field.dataset.keep] !== undefined ? typed[field.dataset.keep] : (price[key] !== undefined ? price[key] : '');
    inputs[key] = field;
  }
  const savePrice = priceRow.appendChild(el('button', 'button', t('保存价格')));
  savePrice.type = 'button';
  savePrice.addEventListener('click', async () => {
    const values = {};
    for (const [key] of fields) {
      if (inputs[key].value === '') continue;
      values[key] = Number(inputs[key].value);
    }
    const result = await call('/api/price', { model: state.model, price: values });
    say(result.ok ? t('{model} 的价格已保存。', { model: state.model }) : result.error, result.ok ? 'good' : 'bad');
  });
  const offpeak = state.price_offpeak;
  $('price-note').textContent = !state.price ? t('{model} 还没有价格，所以它的用量没有计入估算费用。填上后就会计入。', { model: state.model })
    : offpeak ? t('{model} 分时段计价：上面是高峰价（北京时间工作日 9:00–12:00、14:00–18:00），其余时间按闲时价估算（输入 ${input}、命中缓存 ${cached}、输出 ${output}，每百万 token）。每次调用按当时的时段算。自己填了价格以后，全天都按你填的算。',
      { model: state.model, input: offpeak.input, cached: offpeak.cache_read, output: offpeak.output })
    : t('这是 {model} 现在用来估算的价格，可以改成你实际的价格。', { model: state.model });

  if (focused) {
    const again = settings.querySelector('input[data-keep="' + focused + '"]');
    if (again) again.focus();
  }
}

function openSettings() {
  if (!state) return;
  delete settings.dataset.ready;  // 每次打开都从当前状态重新开始，不保留上次打开时填了一半的内容
  $('keys').replaceChildren();
  $('price').replaceChildren();
  say('');
  renderSettings();
  endpointDeleting = '';  // "自定义模型接口"一节（见 endpoints.js）：上次打开时停在一半的删除确认不留着
  drawEndpoints(true);
  renderFallbacks(true);  // "备用模型"一节（见 fallback.js）
  fillDataSettings();
  browserConfirming = false;
  drawBrowserSettings(true);  // 窗口还没打开：直接画（renderBrowser 只在窗口开着时画这一节）
  showComputerConfirm(false);
  drawComputerSettings(true);
  drawSandboxSettings(true);  // "沙箱"一节（见 sandbox.js）
  renderService();  // "后台运行"一节（见 service.js）
  settings.showModal();
}

// 顶栏上专门的"设置"按钮（齿轮）。以前设置藏在模型按钮里，看起来只是"现在用的模型"，用户找不到设置在哪儿
// （"我的设置在哪里 没看见啊"）；现在模型按钮只管换模型（models.js）。快捷键 ⌘, / Ctrl+, 见 keyboard.js
$('settings-open').addEventListener('click', openSettings);
// 齿轮的提示里写上快捷键：Mac 上是 ⌘,，别的系统是 Ctrl+,
retitleSettingsButton();

function retitleSettingsButton() {
  $('settings-open').title = t('设置（{key}）', { key: ON_MAC ? '⌘,' : 'Ctrl+,' });
}

// 打开设置，翻到"API 密钥"，焦点放在这一家的密钥输入框里（换模型的面板里"填写 API 密钥 →"用的）
function openSettingsKeys(providerId) {
  openSettings();
  const field = settings.querySelector('input[data-keep="key-' + CSS.escape(providerId) + '"]');
  if (!field) return;
  field.closest('.key-row').scrollIntoView({ block: 'center', behavior: calm() ? 'auto' : 'smooth' });
  field.focus({ preventScroll: true });
}

$('settings-model-switch').addEventListener('click', () => { settings.close(); openModels(); });
$('setup-open').addEventListener('click', openSettings);
$('close-settings').addEventListener('click', () => settings.close());
// 点面板外面的暗处也能关掉。按下和松开都要在暗处才算，免得在输入框里拖选文字时拖出去就关了
for (const dialog of [settings, approvalDialog, viewer, memoryDialog, extensionsDialog, taskEditor, taskDetail, promptDialog, spendDialog, commandsDialog, sandboxDialog]) {
  let pressedOutside = false;
  dialog.addEventListener('mousedown', (press) => { pressedOutside = press.target === dialog; });
  dialog.addEventListener('click', (click) => {
    if (click.target !== dialog || !pressedOutside) return;
    if (dialog === memoryDialog) requestCloseMemory(); else if (dialog === taskEditor) requestCloseEditor(); else if (dialog === promptDialog) requestClosePrompt(); else if (dialog === extensionsDialog) requestCloseExtensions(); else if (dialog === commandsDialog) requestCloseCommands(); else if (dialog === sandboxDialog) requestCloseSandboxPaths(); else dialog.close();  // 正在编辑、还没保存的，先提醒
  });
}
