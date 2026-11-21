// ---- 扩展 · MCP · 粘贴 JSON、从其他工具导入 ---------------------------------
// 一次加好几个服务器的两种办法：粘贴一段 Claude Desktop 格式的 JSON，或者从别的工具（Claude Desktop、Claude Code、
// Cursor、VS Code、Codex、Gemini CLI、Goose……）的配置里挑。都是先请助手程序读出里面有哪些（/api/mcp/paste、
// /api/mcp/scan），列成一张勾选的清单（命令里的密钥遮住；从别家的写法换过来时改了什么、丢了什么也写在上面），
// 再把勾上的加进来。从其他工具导入那一页顶上的总览和最下面的"没找到的"是三页共用的（imports.js）。
// 文件最后把 MCP 这一页挂到扩展对话框上（打开时、Esc 先回到上一层、关上时清掉表单里刚填的密钥）。

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

// 导入清单里的一项：一个勾选框，加上它的名字、命令（密钥遮住）、要的环境变量，导入时要注意的地方，
// 以及从别家的写法换过来时改了什么、丢了什么（notes，原文和值，换语言时就地重译）
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
    const names = secrets.map((one) => one.name).join(', ');
    words.appendChild(tel('span', null, item.summary.transport === 'http' ? k('请求头：{names}') : k('环境变量：{names}'), { names }));
  }
  if (item.problem) words.appendChild(el('span', 'bad', item.problem));
  if (item.duplicate) words.appendChild(tel('span', 'warn', k('和已经有的一个服务器一模一样，默认不勾。')));
  if (item.target && item.target !== item.name) words.appendChild(tel('span', 'warn', k('这个名字已经有了，会以“{name}”导入。'), { name: item.target }));
  const notes = importNotes(item.notes);
  if (notes) words.appendChild(notes);
  return row;
}

// ---- 从其他工具导入 ----

async function openMcpImport() {
  mcpSay('');
  mcpView = { kind: 'import', sources: null, tools: [], picked: new Set(), loading: true };
  const view = mcpView;
  const head = el('div', 'mcp-head');
  const back = head.appendChild(el('button', 'mcp-back'));
  back.type = 'button';
  back.dataset.focus = 'back';
  back.append(icon('chevron'), tel('span', null, k('全部服务器')));
  back.addEventListener('click', mcpBack);
  head.appendChild(tel('h3', 'mcp-title', k('从其他工具导入')));
  const lead = tel('p', 'mcp-lead', k('只在你打开这一页时读取这些工具的配置文件，只读不写、什么都不运行。勾选的服务器会复制一份到智狐自己的配置里，之后两边互不影响；在别的工具里设成“不用问”的，到了智狐照样先问你。'));
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
  view.sources = reply.data.sources || [];
  view.tools = reply.data.tools || [];
  const nodes = [], groups = [];
  let total = 0;
  for (const source of view.sources) {
    // 没有这个文件的、文件里一个服务器都没配的，不占地方（去过哪些地方写在最下面的"没找到的"里）
    if (!source.found || (!source.problem && !source.servers.length)) continue;
    const group = el('div', 'mcp-pick-group');
    group.appendChild(importGroupHead(source.tool, source.tool_id, source.level, source.path));
    if (source.problem) group.appendChild(mcpNote('bad', source.problem));
    const list = group.appendChild(el('ul', 'mcp-picks'));
    for (const item of source.servers) {
      const key = source.id + '\n' + item.name;
      if (item.target && !item.duplicate) view.picked.add(key);
      total += 1;
      // 同一个名字可能在好几个来源里都有，所以勾选记的是"来源 + 名字"
      const proxy = { has: (name) => view.picked.has(source.id + '\n' + name), add: (name) => view.picked.add(source.id + '\n' + name), delete: (name) => view.picked.delete(source.id + '\n' + name) };
      list.appendChild(mcpPick(item, proxy, updateMcpImport));
    }
    groups.push(group);
  }
  const overview = importOverview(view.tools, 'mcp');
  if (overview) nodes.push(overview);
  if (groups.length) nodes.push(...groups);
  else {
    const empty = el('div', 'mcp-note calm');
    empty.setAttribute('role', 'note');
    empty.append(icon('spark'), tel('span', null, k('在别的工具里没有找到 MCP 服务器。配置放在别处的话，可以用“粘贴 JSON”。')));
    nodes.push(empty);
  }
  const missing = importMissing(view.tools, 'mcp', !groups.length);
  if (missing) nodes.push(missing);
  result.replaceChildren(...nodes);
  if (total) retext(message, total === 1 ? k('找到 1 个服务器。') : k('找到 {n} 个服务器。'), total === 1 ? undefined : { n: total });
  else { delete message.dataset.i18n; message.textContent = ''; }
  updateMcpImport();
  const first = result.querySelector('.mcp-picks input:not(:disabled)');
  if (first) first.focus();
  importArrive('mcp');
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
