// ---- 扩展 · MCP · 列表 ------------------------------------------------------
// MCP 一页的第一层：接入的服务器和它们的状态、上面一排"添加 / 粘贴 JSON / 从其他工具导入"、一个都没有时的说明，
// 以及每一行：启用开关、重启，和别处一样的"在访达中显示 / 编辑 / 删除"（删除先在原地确认）。

function drawMcpList(focus) {
  const servers = mcpState.servers;
  const print = JSON.stringify([mcpState, mcpDeleting, [...mcpPending], language]);
  if (!focus && print === mcpDrawn && mcpPanel.dataset.view === 'list') return;
  mcpDrawn = print;
  const nodes = [mcpToolbar()];
  if (mcpState.problem) nodes.push(mcpNote('bad', spoken(mcpState.problem)));
  if (mcpState.dropped) {
    nodes.push(mcpNote('warn', t('这段对话只带上了前面的 MCP 工具，还有 {n} 个没有放进去：工具太多时模型不容易选对，有的厂商还限制了个数。可以停用一些用不上的服务器。', { n: mcpState.dropped })));
  }
  if (!servers.length) {
    nodes.push(mcpEmpty());
  } else {
    const list = el('ul', 'mcp-list');
    list.setAttribute('aria-label', t('MCP 服务器'));
    list.append(...servers.map(mcpRow));
    nodes.push(list);
    if (mcpState.started && servers.some((server) => server.stale)) {
      const note = mcpNote('calm', t('这段对话开始时用的是那一刻已经连上的工具，之后才连上或者有变化的，从下一段新对话开始才用得上（对话中途换工具会让模型的缓存失效）。'));
      const fresh = note.appendChild(tel('button', 'mcp-link', k('开一段新对话')));
      fresh.type = 'button';
      fresh.addEventListener('click', () => { if (!busy && requestCloseExtensions()) newButton.click(); });
      nodes.push(note);
    }
  }
  mcpShow(nodes, 'list', focus);
}

function mcpToolbar() {
  const bar = el('div', 'mcp-bar');
  bar.appendChild(el('p', 'mcp-lead', t('MCP 服务器给助手接上别人写好的工具：读写 GitHub、查数据库、操作浏览器……助手调用它们之前，默认每次都先问你。')));
  const tools = bar.appendChild(el('div', 'mcp-tools-bar'));
  const add = tools.appendChild(panelButton('plus', k('添加服务器'), 'primary'));
  add.dataset.focus = 'add';
  add.addEventListener('click', () => openMcpForm(null));
  const paste = tools.appendChild(panelButton('clipboard', k('粘贴 JSON')));
  paste.dataset.focus = 'paste';
  paste.addEventListener('click', openMcpPaste);
  const importer = tools.appendChild(panelButton('import', k('从其他工具导入')));
  importer.dataset.focus = 'import';
  importer.addEventListener('click', openMcpImport);
  const reveal = tools.appendChild(iconButton('folder', ON_MAC ? k('在访达中显示配置文件') : k('在文件夹中显示配置文件')));
  reveal.classList.remove('small');
  reveal.dataset.focus = 'reveal';
  reveal.addEventListener('click', revealMcpFile);
  return bar;
}

function mcpEmpty() {
  const box = el('div', 'mcp-empty');
  const mark = box.appendChild(el('div', 'mcp-empty-mark'));
  mark.appendChild(icon('plug'));
  box.appendChild(el('h3', null, t('还没有接入 MCP 服务器')));
  box.appendChild(el('p', null, t('MCP（Model Context Protocol）是一个公开的协议：别人把一组工具做成一个"MCP 服务器"，任何支持 MCP 的助手都能接上它、把这些工具交给模型用。本地的服务器是一个命令（比如 npx 启动的一个包），远程的是一个网址。')));
  box.appendChild(el('p', null, t('配置的写法和 Claude Desktop 一样，比如这一段让助手能读写一个文件夹：')));
  box.appendChild(el('pre', 'mcp-example', MCP_EXAMPLE));
  const row = box.appendChild(el('div', 'row'));
  const add = row.appendChild(panelButton('plus', k('添加服务器'), 'primary'));
  add.addEventListener('click', () => openMcpForm(null));
  const paste = row.appendChild(panelButton('clipboard', k('粘贴 JSON')));
  paste.addEventListener('click', openMcpPaste);
  const importer = row.appendChild(panelButton('import', k('从其他工具导入')));
  importer.addEventListener('click', openMcpImport);
  return box;
}

const mcpToolCount = (n) => (n === 1 ? t('1 个工具') : t('{n} 个工具', { n: n.toLocaleString(LOCALES[language]) }));

function mcpRow(server) {
  const row = el('li', 'mcp-server');
  row.dataset.name = server.name;
  const pending = mcpPending.has(server.name);

  // 删除前在原地确认一下，和删除会话、记忆一样，不弹对话框
  if (mcpDeleting === server.name) {
    const box = row.appendChild(el('div', 'mcp-confirm'));
    box.appendChild(el('span', null, t('删除“{name}”？它会从配置文件里去掉，正在运行的进程也会被关掉。', { name: server.name })));
    const yes = box.appendChild(el('button', 'mini danger', t('删除')));
    const no = box.appendChild(el('button', 'mini cancel', t('取消')));
    yes.type = no.type = 'button';
    no.dataset.focus = 'cancel:' + server.name;
    yes.disabled = no.disabled = pending;
    yes.addEventListener('click', () => deleteMcp(server.name, row));
    no.addEventListener('click', () => { mcpDeleting = null; drawMcpList('open:' + server.name); });
    return row;
  }

  const open = row.appendChild(el('button', 'mcp-open'));
  open.type = 'button';
  open.dataset.focus = 'open:' + server.name;
  open.appendChild(el('span', 'mcp-dot ' + server.status));
  const words = open.appendChild(el('span', 'mcp-words'));
  const line = words.appendChild(el('span', 'mcp-line'));
  line.appendChild(el('span', 'mcp-name', server.name));
  if (server.config) line.appendChild(el('span', 'mcp-kind', server.config.transport === 'http' ? 'HTTP' : 'stdio'));
  if (mcpState.started && server.stale && server.enabled) {
    const flag = line.appendChild(el('span', 'mcp-flag', t('新对话生效')));
    flag.title = t('这段对话开始时它还没有连上（或者它的工具后来变了），这段对话里用的还是开始那一刻的样子。开一段新对话就能用上现在的。');
  }
  const meta = words.appendChild(el('span', 'mcp-meta'));
  meta.appendChild(el('span', null, t(MCP_STATUS[server.status] || server.status) + ' · '));
  meta.appendChild(el('span', 'mcp-command', mcpCommand(server.config)));
  meta.title = mcpCommand(server.config);  // 命令常常很长，行里放不下的部分省略了，悬停时看全的
  if (server.error && server.status !== 'disabled') {
    const problem = words.appendChild(el('span', 'mcp-error', spoken(server.error)));
    if (server.last_words) problem.appendChild(el('span', 'mcp-said', t('服务器最后说：{line}', { line: server.last_words })));
  }
  open.addEventListener('click', () => openMcpDetail(server.name));

  const toggle = row.appendChild(mcpSwitch(server.enabled, k('启用服务器：{name}'), { name: server.name }));
  toggle.dataset.focus = 'enable:' + server.name;
  toggle.disabled = pending || !server.config;
  toggle.addEventListener('click', () => enableMcp(server.name, !server.enabled));

  // 最右边一格：平时是工具的个数；悬停时换成按钮。最后三个和会话行、记忆行一样（在访达中显示、编辑、删除），
  // 位置、大小、顺序都一样。名字里带上是哪一个服务器：读屏软件逐个读按钮时要知道它操作的是谁
  const slot = row.appendChild(el('div', 'mcp-slot'));
  if (server.status === 'ready') slot.appendChild(el('span', 'mcp-count', mcpToolCount(server.tools)));
  const actions = slot.appendChild(el('div', 'mcp-actions'));
  const button = (iconName, title, label, extra) => {
    const node = actions.appendChild(el('button', 'icon-button small' + (extra ? ' ' + extra : '')));
    node.type = 'button';
    node.title = t(title);
    node.setAttribute('aria-label', t(label, { name: server.name }));
    node.appendChild(icon(iconName));
    node.disabled = pending;
    return node;
  };
  const restart = button('refresh', k('重启'), k('重启服务器：{name}'));
  restart.dataset.focus = 'restart:' + server.name;
  restart.disabled = pending || !server.enabled;
  restart.addEventListener('click', () => restartMcp(server.name));
  const reveal = button('folder', SHOW_FILE, ON_MAC ? k('在访达中显示“{name}”的配置文件') : k('在文件夹中显示“{name}”的配置文件'));
  reveal.dataset.focus = 'reveal:' + server.name;
  reveal.disabled = false;
  reveal.addEventListener('click', revealMcpFile);
  const edit = button('edit', k('编辑'), k('编辑服务器：{name}'));
  edit.dataset.focus = 'edit:' + server.name;
  edit.disabled = pending || !server.config;
  edit.addEventListener('click', () => openMcpForm(server.name));
  const remove = button('trash', k('删除'), k('删除服务器：{name}'), 'danger-hover');
  remove.dataset.focus = 'delete:' + server.name;
  remove.addEventListener('click', () => { mcpDeleting = server.name; drawMcpList('cancel:' + server.name); });
  return row;
}

async function enableMcp(name, enabled) {
  const reply = await mcpAct(name, '/api/mcp/enable', { name, enabled });
  if (!reply.ok) { mcpSay(reply.error, 'bad'); return; }
  const said = enabled ? t('已启用“{name}”，正在连接……', { name }) : t('已停用“{name}”。', { name });
  mcpSay(said, 'good');
  announce(said);
}

async function restartMcp(name) {
  const reply = await mcpAct(name, '/api/mcp/restart', { name });
  if (!reply.ok) { mcpSay(reply.error, 'bad'); return; }
  mcpSay(t('正在重启“{name}”……', { name }));
}

async function deleteMcp(name, row) {
  const servers = mcpState.servers, index = servers.findIndex((server) => server.name === name);
  const next = servers[index + 1] || servers[index - 1];
  const [reply] = await Promise.all([mcpAct(name, '/api/mcp/delete', { name }), leave(row)]);
  mcpDeleting = null;
  if (!reply.ok) { mcpSay(reply.error, 'bad'); drawMcpList('open:' + name); return; }
  if (mcpView.kind !== 'list') mcpView = { kind: 'list' };
  mcpState = { ...mcpState, servers: mcpState.servers.filter((server) => server.name !== name) };  // 新状态马上就到，先在页面上拿掉
  drawMcpList(next ? 'open:' + next.name : 'add');
  const said = t('已删除“{name}”。', { name });
  mcpSay(said, 'good');
  announce(said);
}

async function revealMcpFile() {
  const reply = await call('/api/mcp/reveal');
  mcpSay(reply.ok ? revealedText(reply.data.path) : reply.error, reply.ok ? 'good' : 'bad');
}
