// ---- 添加 / 编辑 ----

function mcpInput(id, value, placeholder, extra) {
  const input = el('input', 'field' + (extra ? ' ' + extra : ''));
  input.id = id;
  input.type = 'text';
  input.value = value || '';
  input.spellcheck = false;
  input.autocomplete = 'off';
  if (placeholder) tattr(input, 'placeholder', placeholder);
  return input;
}

function mcpField(label, control, help) {
  const wrap = el('label', 'mcp-field');
  wrap.appendChild(tel('span', null, label));
  wrap.appendChild(control);
  if (help) wrap.appendChild(tel('span', 'mcp-help', help));
  return wrap;
}

// 环境变量或请求头的一行：名字、值、删掉这一行。item 是已有的那一项（{name, hint}），新加的是 null
function mcpPair(kind, item) {
  const row = el('div', 'mcp-pair');
  const name = row.appendChild(el('input', 'field'));
  name.type = 'text';
  name.spellcheck = false;
  name.autocomplete = 'off';
  name.value = item ? item.name : '';
  tattr(name, 'placeholder', kind === 'env' ? k('变量名') : k('请求头的名字'));
  tattr(name, 'aria-label', kind === 'env' ? k('环境变量的名字') : k('请求头的名字'));
  const value = row.appendChild(el('input', 'field'));
  value.type = 'password';  // 值多半是密钥：和设置面板里的 API 密钥一样，不在屏幕上显示出来
  value.autocomplete = 'new-password';
  value.spellcheck = false;
  if (item) {
    row.dataset.keep = item.name;  // 留空的话，交回去"保持原来的值"
    value.placeholder = item.hint ? t('已设置 {hint} · 留空表示不变', { hint: item.hint }) : t('已设置 · 留空表示不变');
    value.dataset.i18nPlaceholder = '';  // 这句由 syncMcpForm 换语言时重写
  } else {
    tattr(value, 'placeholder', k('值'));
  }
  tattr(value, 'aria-label', kind === 'env' ? k('环境变量的值') : k('请求头的值'));
  const remove = row.appendChild(iconButton('x', kind === 'env' ? k('删掉这个环境变量') : k('删掉这个请求头')));
  remove.addEventListener('click', () => {
    const box = row.parentElement, rows = [...box.children], at = rows.indexOf(row);
    row.remove();
    mcpDirty();
    const next = box.children[at] || box.children[at - 1];
    (next ? next.querySelector('input') : box.parentElement.querySelector('.mcp-pair-add')).focus();
  });
  return row;
}

function openMcpForm(original) {
  const server = original ? mcpFind(original) : null;
  if (original && (!server || !server.config)) return;
  const config = server ? server.config : null;
  mcpSay('');
  mcpView = { kind: 'form', original, name: original, transport: config ? config.transport : 'stdio', dirty: false, armed: false, saving: false };
  const view = mcpView;

  const head = el('div', 'mcp-head');
  const back = head.appendChild(el('button', 'mcp-back'));
  back.type = 'button';
  back.append(icon('chevron'), tel('span', null, original ? k('返回') : k('全部服务器')));
  back.addEventListener('click', mcpBack);
  head.appendChild(tel('h3', 'mcp-title', original ? k('编辑服务器') : k('添加 MCP 服务器')));

  const form = el('form', 'mcp-form');
  form.noValidate = true;
  form.addEventListener('submit', (submit) => { submit.preventDefault(); saveMcpForm(); });
  const name = mcpInput('mcp-f-name', original, k('比如 github'));
  name.maxLength = 64;
  name.dataset.focus = 'name';
  form.appendChild(mcpField(k('名字'), name, k('只用字母、数字、下划线、点和连字符。模型看到的工具名是 mcp__名字__工具。')));

  // 连接方式：两个选项，按 WAI-ARIA 的单选组做（方向键切换）
  const seg = el('div', 'mcp-seg');
  seg.setAttribute('role', 'radiogroup');
  tattr(seg, 'aria-label', k('连接方式'));
  const kinds = [['stdio', 'terminal', k('本地程序（stdio）')], ['http', 'globe', k('网址（HTTP）')]];
  for (const [value, iconName, label] of kinds) {
    const option = seg.appendChild(el('button'));
    option.type = 'button';
    option.dataset.transport = value;
    option.setAttribute('role', 'radio');
    option.append(icon(iconName), tel('span', null, label));
    option.addEventListener('click', () => setMcpTransport(value, true));
  }
  seg.addEventListener('keydown', (key) => {
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(key.key)) return;
    key.preventDefault();
    setMcpTransport(view.transport === 'stdio' ? 'http' : 'stdio', true);
  });
  const kind = el('div', 'mcp-field');
  kind.appendChild(tel('span', null, k('连接方式')));
  kind.appendChild(seg);
  form.appendChild(kind);

  // 本地程序
  const local = form.appendChild(el('div', 'mcp-form'));
  local.id = 'mcp-f-stdio';
  const command = mcpInput('mcp-f-command', config && config.transport === 'stdio' ? config.command : '', k('比如 npx、uvx、python'));
  local.appendChild(mcpField(k('命令'), command));
  const args = el('textarea', 'mcp-area');
  args.id = 'mcp-f-args';
  args.spellcheck = false;
  args.rows = 4;
  args.value = config && config.transport === 'stdio' ? config.args.join('\n') : '';
  args.placeholder = '-y\n@modelcontextprotocol/server-filesystem\n/path/to/folder';  // 例子是命令行参数，哪种语言都一样，不用翻译
  local.appendChild(mcpField(k('参数'), args, k('每行一个，不用加引号。命令不经过 shell：$HOME、* 这些写法不会被展开，可以写 ${VAR} 取环境变量 VAR 的值。')));
  const cwd = mcpInput('mcp-f-cwd', config && config.transport === 'stdio' ? config.cwd : '', k('可以不填'));
  local.appendChild(mcpField(k('工作目录'), cwd));
  const env = el('div', 'mcp-pairs');
  env.id = 'mcp-f-env';
  for (const item of config && config.transport === 'stdio' ? config.env : []) env.appendChild(mcpPair('env', item));
  const envField = el('div', 'mcp-field');
  envField.append(tel('span', null, k('环境变量')), env);
  const addEnv = envField.appendChild(tel('button', 'mcp-pair-add', k('+ 添加环境变量')));
  addEnv.type = 'button';
  addEnv.addEventListener('click', () => { const row = env.appendChild(mcpPair('env', null)); row.querySelector('input').focus(); mcpDirty(); });
  local.appendChild(envField);

  // 网址
  const remote = form.appendChild(el('div', 'mcp-form'));
  remote.id = 'mcp-f-http';
  const url = mcpInput('mcp-f-url', config && config.transport === 'http' ? config.url : '', k('比如 https://example.com/mcp'));
  url.type = 'url';
  const plain = tel('span', 'mcp-help warn', k('这个地址不是 HTTPS：请求头里的令牌会以明文在网络上传输。'));
  plain.hidden = true;
  const urlField = remote.appendChild(mcpField(k('地址'), url));
  urlField.appendChild(plain);
  url.addEventListener('input', () => { plain.hidden = !/^http:\/\/(?!(localhost|127\.|\[::1\]))/i.test(url.value.trim()); });
  url.dispatchEvent(new Event('input'));
  const headers = el('div', 'mcp-pairs');
  headers.id = 'mcp-f-headers';
  for (const item of config && config.transport === 'http' ? config.headers : []) headers.appendChild(mcpPair('header', item));
  const headerField = el('div', 'mcp-field');
  headerField.append(tel('span', null, k('请求头')), headers);
  const addHeader = headerField.appendChild(tel('button', 'mcp-pair-add', k('+ 添加请求头')));
  addHeader.type = 'button';
  addHeader.addEventListener('click', () => { const row = headers.appendChild(mcpPair('header', null)); row.querySelector('input').focus(); mcpDirty(); });
  headerField.appendChild(tel('span', 'mcp-help', k('要令牌的服务器：加一个 Authorization，值填 Bearer 你的令牌。智狐还不支持 MCP 的 OAuth 登录。')));
  remote.appendChild(headerField);

  const enabled = el('label', 'mcp-check');
  const box = enabled.appendChild(el('input'));
  box.type = 'checkbox';
  box.id = 'mcp-f-enabled';
  box.checked = server ? server.enabled : true;
  enabled.appendChild(tel('span', null, k('保存后马上启动')));
  form.appendChild(enabled);
  form.appendChild(mcpNote('calm', '', 'lock'));
  retext(form.lastChild.lastChild, k('密钥请放在环境变量或请求头里：它们保存在你电脑上的 data/mcp.json（只有你的账号能读），页面上只显示最后 4 位。保存一个本地程序，就是让智狐在这台电脑上运行这个命令，只添加你信任的服务器。'));

  const foot = form.appendChild(el('div', 'mcp-foot'));
  const message = foot.appendChild(el('p', 'mcp-help'));
  message.id = 'mcp-f-message';
  message.setAttribute('role', 'status');
  retext(message, ON_MAC ? k('⌘Enter 保存，Esc 返回') : k('Ctrl+Enter 保存，Esc 返回'));
  const cancel = foot.appendChild(tel('button', 'button', k('取消')));
  cancel.type = 'button';
  cancel.addEventListener('click', mcpBack);
  const save = foot.appendChild(tel('button', 'button primary', k('保存')));
  save.type = 'submit';
  save.id = 'mcp-f-save';

  form.addEventListener('input', mcpDirty);
  form.addEventListener('keydown', (key) => {
    if (key.key === 'Enter' && (ON_MAC ? key.metaKey : key.ctrlKey) && !key.isComposing) { key.preventDefault(); saveMcpForm(); }
  });
  mcpShow([head, form], 'form', 'name');
  setMcpTransport(view.transport, false);
  if (original) { name.focus(); name.select(); }
}

function setMcpTransport(value, user) {
  const view = mcpView;
  if (view.kind !== 'form') return;
  view.transport = value;
  for (const option of mcpPanel.querySelectorAll('.mcp-seg [role="radio"]')) {
    const on = option.dataset.transport === value;
    option.setAttribute('aria-checked', on ? 'true' : 'false');
    option.tabIndex = on ? 0 : -1;
    if (on && user) option.focus();
  }
  $('mcp-f-stdio').hidden = value !== 'stdio';
  $('mcp-f-http').hidden = value !== 'http';
  if (user) mcpDirty();
}

function mcpDirty() {
  const view = mcpView;
  if (view.kind !== 'form') return;
  view.dirty = true;
  if (view.armed) { view.armed = false; mcpFormSay(ON_MAC ? k('⌘Enter 保存，Esc 返回') : k('Ctrl+Enter 保存，Esc 返回')); }
}

function mcpFormSay(key, values, bad) {
  const message = $('mcp-f-message');
  if (!message) return;
  if (key) retext(message, key, values); else { delete message.dataset.i18n; message.textContent = values || ''; }
  message.classList.toggle('bad', Boolean(bad));
}

// 状态更新时表单不重画（你正在填），只把"已设置 …abcd"这类跟着语言走、又带着值的提示换掉
function syncMcpForm() {
  for (const row of mcpPanel.querySelectorAll('.mcp-pair[data-keep]')) {
    const server = mcpFind(mcpView.original), config = server && server.config;
    const items = config ? (config.transport === 'http' ? config.headers : config.env) : [];
    const item = items.find((one) => one.name === row.dataset.keep);
    const value = row.querySelectorAll('input')[1];
    value.placeholder = item && item.hint ? t('已设置 {hint} · 留空表示不变', { hint: item.hint }) : t('已设置 · 留空表示不变');
  }
}

// 离开表单之前：改过还没保存的，先提醒一次，再来一次才真的放弃。返回 true 表示可以离开
function leaveMcpForm() {
  const view = mcpView;
  if (view.kind !== 'form' || !view.dirty || view.armed || view.saving) return !view.saving;
  view.armed = true;
  mcpFormSay(k('还有没保存的修改，再按一次就放弃。'), null, true);
  return false;
}

function mcpPairsOf(box) {
  return [...box.querySelectorAll('.mcp-pair')].map((row) => {
    const [name, value] = row.querySelectorAll('input');
    const item = { name: name.value.trim() };
    if (value.value) item.value = value.value;
    else if (row.dataset.keep) item.keep = row.dataset.keep;
    return item;
  }).filter((item) => item.name || item.value);
}

async function saveMcpForm() {
  const view = mcpView;
  if (view.kind !== 'form' || view.saving) return;
  const name = $('mcp-f-name').value.trim();
  const form = { name, transport: view.transport, enabled: $('mcp-f-enabled').checked };
  if (view.transport === 'stdio') {
    form.command = $('mcp-f-command').value.trim();
    form.args = $('mcp-f-args').value.split('\n').map((line) => line.replace(/\r$/, '')).filter((line) => line.trim());
    form.cwd = $('mcp-f-cwd').value.trim();
    form.env = mcpPairsOf($('mcp-f-env'));
  } else {
    form.url = $('mcp-f-url').value.trim();
    form.headers = mcpPairsOf($('mcp-f-headers'));
  }
  // 明显没填的，在这里就说，不用等助手程序退回来
  const missing = !name ? ['mcp-f-name', k('先给这个服务器起个名字。')]
    : view.transport === 'stdio' && !form.command ? ['mcp-f-command', k('还没有填命令。')]
    : view.transport === 'http' && !form.url ? ['mcp-f-url', k('还没有填地址。')] : null;
  if (missing) { mcpFormSay(missing[1], null, true); $(missing[0]).focus(); return; }
  view.saving = true;
  $('mcp-f-save').disabled = true;
  mcpFormSay(k('正在保存……'));
  const reply = await call('/api/mcp/save', { original: view.original, server: form });
  if (mcpView !== view) return;
  view.saving = false;
  $('mcp-f-save').disabled = false;
  if (!reply.ok) { mcpFormSay(null, reply.error, true); $('mcp-f-save').focus(); return; }
  for (const field of mcpPanel.querySelectorAll('input[type="password"]')) field.value = '';  // 密钥交出去了，就不在页面上多留
  const saved = reply.data.name || name;
  const said = form.enabled ? t('已保存“{name}”，正在连接……', { name: saved }) : t('已保存“{name}”。', { name: saved });
  announce(said);
  openMcpDetail(saved);
  mcpSay(said, 'good');
}
