// ---- 粘贴 JSON ----

function openMcpPaste() {
  mcpSay('');
  mcpView = { kind: 'paste', servers: null, picked: new Set(), loading: false };
  const head = el('div', 'mcp-head');
  const back = head.appendChild(el('button', 'mcp-back'));
  back.type = 'button';
  back.append(icon('chevron'), tel('span', null, k('全部服务器')));
  back.addEventListener('click', mcpBack);
  head.appendChild(tel('h3', 'mcp-title', k('粘贴 JSON')));
  const area = el('textarea', 'mcp-area paste');
  area.id = 'mcp-paste-text';
  area.spellcheck = false;
  area.dataset.focus = 'paste-text';
  area.placeholder = MCP_EXAMPLE;
  const field = mcpField(k('配置'), area, k('从 Claude Desktop、Claude Code、Cursor、VS Code 的配置里复制过来都行：整个文件、mcpServers 里面的那一段，或者单独一个服务器。'));
  const result = el('div');
  result.id = 'mcp-paste-result';
  const foot = el('div', 'mcp-foot');
  const message = foot.appendChild(el('p', 'mcp-help'));
  message.id = 'mcp-paste-message';
  message.setAttribute('role', 'status');
  const read = foot.appendChild(tel('button', 'button', k('读取')));
  read.type = 'button';
  read.id = 'mcp-paste-read';
  read.addEventListener('click', readMcpPaste);
  const add = foot.appendChild(tel('button', 'button primary', k('添加选中的')));
  add.type = 'button';
  add.id = 'mcp-paste-add';
  add.disabled = true;
  add.addEventListener('click', applyMcpPaste);
  area.addEventListener('input', () => {  // 改了内容：之前读出来的清单就不算数了
    if (!mcpView.servers) return;
    mcpView.servers = null;
    result.replaceChildren();
    updateMcpPaste();
  });
  area.addEventListener('keydown', (key) => {
    if (key.key === 'Enter' && (ON_MAC ? key.metaKey : key.ctrlKey) && !key.isComposing) { key.preventDefault(); mcpView.servers ? applyMcpPaste() : readMcpPaste(); }
  });
  mcpShow([head, field, result, foot], 'paste', 'paste-text');
  updateMcpPaste();
}

function updateMcpPaste() {
  const view = mcpView;
  if (view.kind !== 'paste') return;
  const count = view.picked.size;
  $('mcp-paste-add').disabled = !view.servers || !count || view.loading;
  $('mcp-paste-add').textContent = count ? t('添加选中的 {n} 个', { n: count }) : t('添加选中的');
  $('mcp-paste-read').disabled = view.loading;
  if (!view.servers) retext($('mcp-paste-message'), ON_MAC ? k('⌘Enter 读取') : k('Ctrl+Enter 读取'));
}

async function readMcpPaste() {
  const view = mcpView, text = $('mcp-paste-text').value;
  if (!text.trim()) { $('mcp-paste-message').textContent = t('先把配置粘贴进来。'); $('mcp-paste-text').focus(); return; }
  view.loading = true;
  updateMcpPaste();
  const reply = await call('/api/mcp/paste', { text });
  if (mcpView !== view) return;
  view.loading = false;
  if (!reply.ok) {
    view.servers = null;
    $('mcp-paste-result').replaceChildren(mcpNote('bad', reply.error));
    updateMcpPaste();
    return;
  }
  view.servers = reply.data.servers;
  view.picked = new Set(view.servers.filter((item) => item.target && !item.duplicate).map((item) => item.name));
  const list = el('ul', 'mcp-picks');
  list.append(...view.servers.map((item) => mcpPick(item, view.picked, updateMcpPaste)));
  $('mcp-paste-result').replaceChildren(list);
  $('mcp-paste-message').textContent = view.servers.length === 1 ? t('找到 1 个服务器。') : t('找到 {n} 个服务器。', { n: view.servers.length });
  updateMcpPaste();
  const first = list.querySelector('input:not(:disabled)');
  if (first) first.focus();
}

async function applyMcpPaste() {
  const view = mcpView;
  if (!view.servers || !view.picked.size) return;
  view.loading = true;
  updateMcpPaste();
  const reply = await call('/api/mcp/paste', { text: $('mcp-paste-text').value, names: [...view.picked] });
  if (mcpView !== view) return;
  view.loading = false;
  if (!reply.ok) { $('mcp-paste-message').textContent = reply.error; updateMcpPaste(); return; }
  mcpFinishImport(reply.data.added, reply.data.skipped);
}

// 导入、粘贴完成之后：回到列表，说清楚加了哪些、哪些没加
function mcpFinishImport(added, skipped) {
  mcpView = { kind: 'list' };
  mcpDrawn = '';
  drawMcpList(added.length ? 'open:' + added[0] : 'add');
  const said = (added.length ? t('已添加：{names}。正在连接……', { names: added.join(t('、')) }) : t('没有添加任何服务器。'))
    + (skipped.length ? ' ' + t('没有添加：{list}', { list: skipped.join(t('；')) }) : '');
  mcpSay(said, skipped.length ? 'bad' : 'good');
  announce(said);
}

// 导入清单里的一项：一个勾选框，加上它的名字、命令（密钥遮住）、要的环境变量，和导入时要注意的地方
function mcpPick(item, picked, changed) {
  const row = el('li');
  const label = row.appendChild(el('label', 'mcp-pick'));
  const box = label.appendChild(el('input'));
  box.type = 'checkbox';
  box.checked = picked.has(item.name);
  box.disabled = !item.target;
  box.addEventListener('change', () => { if (box.checked) picked.add(item.name); else picked.delete(item.name); changed(); });
  const words = label.appendChild(el('span', 'mcp-pick-words'));
  words.appendChild(el('b', null, item.name));
  if (item.summary) words.appendChild(el('span', 'mcp-command', mcpCommand(item.summary)));
  const secrets = item.summary ? (item.summary.transport === 'http' ? item.summary.headers : item.summary.env) : [];
  if (secrets.length) {
    const names = secrets.map((one) => one.name).join(t('、'));
    words.appendChild(el('span', null, item.summary.transport === 'http' ? t('请求头：{names}', { names }) : t('环境变量：{names}', { names })));
  }
  if (item.problem) words.appendChild(el('span', 'bad', item.problem));
  if (item.duplicate) words.appendChild(el('span', 'warn', t('和已经有的一个服务器一模一样，默认不勾。')));
  if (item.target && item.target !== item.name) words.appendChild(el('span', 'warn', t('这个名字已经有了，会以“{name}”导入。', { name: item.target })));
  return row;
}

// ---- 从 Claude 导入 ----

async function openMcpImport() {
  mcpSay('');
  mcpView = { kind: 'import', sources: null, picked: new Set(), loading: true };
  const view = mcpView;
  const head = el('div', 'mcp-head');
  const back = head.appendChild(el('button', 'mcp-back'));
  back.type = 'button';
  back.dataset.focus = 'back';
  back.append(icon('chevron'), tel('span', null, k('全部服务器')));
  back.addEventListener('click', mcpBack);
  head.appendChild(tel('h3', 'mcp-title', k('从 Claude Desktop / Claude Code 导入')));
  const lead = tel('p', 'mcp-lead', k('只在你打开这一页时读取它们的配置文件。勾选的服务器会复制一份到智狐自己的配置里，之后两边互不影响。'));
  const result = el('div');
  result.id = 'mcp-import-result';
  result.append(el('div', 'mcp-skeleton'), el('div', 'mcp-skeleton'), el('div', 'mcp-skeleton'));
  const foot = el('div', 'mcp-foot');
  const message = foot.appendChild(el('p', 'mcp-help'));
  message.id = 'mcp-import-message';
  message.setAttribute('role', 'status');
  retext(message, k('正在读取……'));
  const add = foot.appendChild(tel('button', 'button primary', k('导入选中的')));
  add.type = 'button';
  add.id = 'mcp-import-add';
  add.disabled = true;
  add.addEventListener('click', applyMcpImport);
  lead.style.marginBottom = '14px';
  mcpShow([head, lead, result, foot], 'import', 'back');

  const reply = await call('/api/mcp/scan');
  if (mcpView !== view) return;
  view.loading = false;
  if (!reply.ok) { result.replaceChildren(mcpNote('bad', reply.error)); message.textContent = ''; return; }
  view.sources = reply.data.sources;
  const nodes = [], missing = [];
  let total = 0;
  for (const source of view.sources) {
    if (!source.found) { missing.push(source.label); continue; }
    const group = el('div', 'mcp-pick-group');
    const title = group.appendChild(el('h4', null, source.label));
    title.appendChild(el('span', 'path', source.path));
    if (source.problem) group.appendChild(mcpNote('bad', source.problem));
    else if (!source.servers.length) group.appendChild(el('p', 'mcp-help', t('这里没有配置 MCP 服务器。')));
    const list = group.appendChild(el('ul', 'mcp-picks'));
    for (const item of source.servers) {
      const key = source.id + '\n' + item.name;
      if (item.target && !item.duplicate) view.picked.add(key);
      total += 1;
      // 同一个名字可能在好几个来源里都有，所以勾选记的是"来源 + 名字"
      const proxy = { has: (name) => view.picked.has(source.id + '\n' + name), add: (name) => view.picked.add(source.id + '\n' + name), delete: (name) => view.picked.delete(source.id + '\n' + name) };
      list.appendChild(mcpPick(item, proxy, updateMcpImport));
    }
    nodes.push(group);
  }
  if (!nodes.length) nodes.push(mcpNote('calm', t('没有找到 Claude Desktop 或 Claude Code 的 MCP 配置。也可以用“粘贴 JSON”。')));
  if (missing.length && nodes.length) nodes.push(el('p', 'mcp-help', t('没有找到：{list}', { list: missing.join(t('、')) })));
  result.replaceChildren(...nodes);
  message.textContent = total ? (total === 1 ? t('找到 1 个服务器。') : t('找到 {n} 个服务器。', { n: total })) : '';
  updateMcpImport();
  const first = result.querySelector('input:not(:disabled)');
  if (first) first.focus();
}

function updateMcpImport() {
  const view = mcpView;
  if (view.kind !== 'import') return;
  const count = view.picked.size;
  $('mcp-import-add').disabled = !count || view.loading;
  $('mcp-import-add').textContent = count ? t('导入选中的 {n} 个', { n: count }) : t('导入选中的');
}

async function applyMcpImport() {
  const view = mcpView;
  if (!view.picked.size) return;
  view.loading = true;
  updateMcpImport();
  const picks = [...view.picked].map((key) => { const [source, name] = key.split('\n'); return { source, name }; });
  const reply = await call('/api/mcp/import', { picks });
  if (mcpView !== view) return;
  view.loading = false;
  if (!reply.ok) { $('mcp-import-message').textContent = reply.error; updateMcpImport(); return; }
  mcpFinishImport(reply.data.added, reply.data.skipped);
}

// ---- 挂到"扩展"对话框上 ----

extensionTabs.mcp = {
  open() {
    mcpDrawn = '';
    if (mcpView.kind === 'list' || mcpView.kind === 'detail') drawMcp();
    call('/api/mcp/refresh');  // 配置文件被你在编辑器里改过的话，助手程序会照着同步，并发来新的状态
  },
  beforeClose() {
    if (mcpView.kind === 'form' && !leaveMcpForm()) return false;
    return true;
  },
};
extensionsDialog.addEventListener('keydown', (key) => {
  if (key.key !== 'Escape' || key.isComposing || key.keyCode === 229 || extensionTab !== 'mcp' || !mcpPanel.contains(document.activeElement) && document.activeElement !== extensionsDialog) return;
  if (mcpDeleting) {  // 这一下 Esc 只用来取消删除
    key.preventDefault();
    const name = mcpDeleting;
    mcpDeleting = null;
    drawMcpList('open:' + name);
  } else if (mcpView.kind !== 'list') {  // 在下一层：先回到上一层，不直接关掉对话框
    key.preventDefault();
    key.stopPropagation();
    mcpBack();
  }
});
extensionsDialog.addEventListener('close', () => {
  mcpView = { kind: 'list' };
  mcpDeleting = null;
  mcpDrawn = '';
  delete mcpPanel.dataset.view;
  mcpPanel.replaceChildren();  // 表单里可能有刚填的密钥：关掉就清掉
});
