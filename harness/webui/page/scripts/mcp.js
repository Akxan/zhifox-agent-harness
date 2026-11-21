// ---- 扩展 · MCP -------------------------------------------------------------
// MCP 服务器给助手接上别人写好的工具。这一页列出接入的服务器和它们的状态（连接中、已连接、失败、停用），
// 可以添加、编辑、删除、启用停用、重启；点开一个，能看到它的工具（每个工具可以单独设成"不用问"）和日志；
// 还可以从 Claude Desktop / Claude Code 导入，或者粘贴一段 JSON。
//
// 列表跟着状态走：服务器连上了、断开了、工具变了，助手程序都会发一次状态。工具的说明和日志比较长，不放在状态里，
// 点开一个服务器时另外去取（它的 revision 或日志的编号变了就重新取）。
// 密钥（环境变量、请求头的值）从来不会到页面上来，状态里只有名字和最后 4 位。编辑时没有重新填的那一项，
// 交回去的是"保持原来的"（keep），值由助手程序自己从配置文件里拿。
//
// 这一页有几种视图：list 列表、detail 一个服务器的详情、form 添加或编辑、paste 粘贴 JSON、import 从 Claude 导入。
// 列表和详情跟着状态重画（内容没变就不动）；表单、粘贴、导入是你正在填的东西，搭好之后只在原地更新，不整个重画。

const mcpPanel = $('panel-mcp');
const MCP_STATUS = { connecting: k('连接中'), ready: k('已连接'), failed: k('连接失败'), disabled: k('已停用') };
const MCP_ERA = { modern: k('新一代（无状态，每个请求自带版本）'), legacy: k('老一代（先握手）') };
const MCP_EXAMPLE = '{\n  "mcpServers": {\n    "filesystem": {\n      "command": "npx",\n      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/folder"]\n    }\n  }\n}';
let mcpState = null;           // 状态里的 mcp：{servers, problem, file, started, dropped}
let mcpView = { kind: 'list' };
let mcpDrawn = '';
let mcpDeleting = null;        // 正在原地确认删除的那个服务器
const mcpPending = new Set();  // 正在等回答的操作（按服务器的名字）。这期间那一行的按钮不能再点
let mcpSeq = 0;                // 每取一次详情加一。回来得晚的旧详情靠它认出来、丢掉

const spoken = (said) => (!said ? '' : said.key ? t(said.key, said.values) : String(said.text || ''));
const mcpFind = (name) => (mcpState ? mcpState.servers.find((server) => server.name === name) : null);
const mcpCommand = (config) => (!config ? '' : config.transport === 'http' ? config.url : [config.command, ...(config.args || [])].join(' '));

function mcpSay(text, kind) {
  const box = $('extensions-message');
  delete box.dataset.i18n;  // Skill 一页在这里写过原文的话，换语言时会把它换回来、盖掉这句
  delete box.dataset.i18nValues;
  box.textContent = text;
  box.className = 'settings-message ' + (kind || '');
}

function mcpNote(kind, text, iconName) {
  const box = el('div', 'mcp-note ' + kind);
  box.setAttribute('role', kind === 'bad' ? 'alert' : 'note');
  box.appendChild(icon(iconName || (kind === 'calm' ? 'spark' : 'alert')));
  box.appendChild(el('span', null, text));
  return box;
}

function panelButton(iconName, text, extra) {
  const button = el('button', 'button mcp-button' + (extra ? ' ' + extra : ''));
  button.type = 'button';
  button.append(icon(iconName), tel('span', null, text));
  return button;
}

function mcpSwitch(on, label, values) {
  const button = el('button', 'mcp-switch');
  button.type = 'button';
  button.setAttribute('role', 'switch');
  button.setAttribute('aria-checked', on ? 'true' : 'false');
  button.setAttribute('aria-label', t(label, values));
  button.title = t(label, values);
  return button;
}

// 换一种视图：换掉整页的内容。是从别的视图换过来的话，回到顶上、内容淡入；焦点放到 focus 指定的那个元素上
function mcpShow(nodes, kind, focus) {
  const fresh = mcpPanel.dataset.view !== kind;
  const kept = !fresh && mcpPanel.contains(document.activeElement) ? document.activeElement.dataset.focus : null;
  const keptScroll = mcpPanel.scrollTop;
  mcpPanel.dataset.view = kind;
  const wrap = el('div', 'mcp-view');
  wrap.append(...nodes);
  mcpPanel.replaceChildren(wrap);
  if (fresh) {
    mcpPanel.scrollTop = 0;
    play(wrap, [{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }], 240);
  } else {
    mcpPanel.scrollTop = keptScroll;  // 状态更新时重画：别把正在看的位置弄丢
  }
  const target = focus || kept;
  if (target) {
    const again = mcpPanel.querySelector('[data-focus="' + CSS.escape(target) + '"]');
    if (again) again.focus({ preventScroll: !fresh });
  }
}

// 一个 POST 请求，等回答的这段时间把这个服务器标成"忙"（按钮暂时不能点）
async function mcpAct(name, path, data) {
  if (name) mcpPending.add(name);
  if (name) drawMcp();
  const reply = await call(path, data);
  if (name) mcpPending.delete(name);
  drawMcp();
  return reply;
}

function renderMcpState(data) {
  mcpState = data || null;
  $('count-mcp').textContent = data && data.servers.length ? data.servers.length.toLocaleString(LOCALES[language]) : '';
  if (extensionsDialog.open && extensionTab === 'mcp') drawMcp();
}

function drawMcp() {
  if (!mcpState) return;
  if (mcpView.kind === 'list') drawMcpList();
  else if (mcpView.kind === 'detail') drawMcpDetail();
  else if (mcpView.kind === 'form') syncMcpForm();
}

// ---- 列表 ----

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
  const importer = tools.appendChild(panelButton('import', k('从 Claude 导入')));
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
  const importer = row.appendChild(panelButton('import', k('从 Claude 导入')));
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

// ---- 详情 ----

function openMcpDetail(name) {
  const server = mcpFind(name);
  mcpView = { kind: 'detail', name, data: null, error: '', loading: true, revision: server ? server.revision : -1, log: server ? server.log : -1, open: [] };
  mcpDrawn = '';
  drawMcpDetail('back');
  loadMcpDetail();
}

async function loadMcpDetail() {
  const view = mcpView;
  if (view.kind !== 'detail') return;
  const mine = ++mcpSeq;
  view.loading = true;
  const reply = await call('/api/mcp/detail', { name: view.name });
  if (mine !== mcpSeq || mcpView !== view) return;  // 这期间离开了，或者又取了一次
  view.loading = false;
  if (reply.ok) { view.data = reply.data.server; view.error = ''; } else view.error = reply.error;
  drawMcpDetail();
}

function drawMcpDetail(focus) {
  const view = mcpView, server = mcpFind(view.name);
  if (!server) {  // 在别处被删掉了（或者你在编辑器里把它从配置文件里删了）
    mcpView = { kind: 'list' };
    mcpSay(t('服务器“{name}”已经不在了。', { name: view.name }), 'bad');
    drawMcpList('add');
    return;
  }
  if (!view.loading && (server.revision !== view.revision || server.log !== view.log)) {
    view.revision = server.revision;
    view.log = server.log;
    loadMcpDetail();  // 状态变了（连上了、工具变了、多了日志）：重新取一次，回来再画
  }
  const print = JSON.stringify([server, view.data, view.error, view.loading && !view.data, view.open, [...mcpPending], language, mcpState.started]);
  if (!focus && print === mcpDrawn && mcpPanel.dataset.view === 'detail:' + view.name) return;
  mcpDrawn = print;
  const logBox = mcpPanel.querySelector('.mcp-log');
  const atBottom = !logBox || logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight < 8;
  const pending = mcpPending.has(server.name), data = view.data;

  const head = el('div', 'mcp-head');
  const back = head.appendChild(el('button', 'mcp-back'));
  back.type = 'button';
  back.dataset.focus = 'back';
  back.append(icon('chevron'), tel('span', null, k('全部服务器')));
  back.addEventListener('click', mcpBack);
  const title = head.appendChild(el('h3', 'mcp-title'));
  title.appendChild(el('i', 'mcp-dot ' + server.status));
  title.appendChild(el('span', null, server.name));
  const restart = head.appendChild(panelButton('refresh', k('重启')));
  restart.dataset.focus = 'restart';
  restart.disabled = pending || !server.enabled;
  restart.addEventListener('click', () => restartMcp(server.name));
  const edit = head.appendChild(panelButton('edit', k('编辑')));
  edit.dataset.focus = 'edit';
  edit.disabled = !server.config;
  edit.addEventListener('click', () => openMcpForm(server.name));
  const toggle = head.appendChild(mcpSwitch(server.enabled, k('启用服务器：{name}'), { name: server.name }));
  toggle.dataset.focus = 'enable';
  toggle.disabled = pending || !server.config;
  toggle.addEventListener('click', () => enableMcp(server.name, !server.enabled));
  const nodes = [head];

  if (server.error && server.status !== 'disabled') {
    const note = mcpNote(server.status === 'failed' ? 'bad' : 'warn', spoken(server.error));
    if (server.last_words) note.lastChild.appendChild(el('span', 'mcp-said', t('服务器最后说：{line}', { line: server.last_words })));
    nodes.push(note);
  }

  // 连接：怎么连的、用的哪一代协议、服务器自报的名字
  const facts = mcpCard(t('连接'), t(MCP_STATUS[server.status] || server.status));
  const grid = facts.appendChild(el('dl', 'mcp-facts'));
  const fact = (term, value, mono) => {
    grid.appendChild(el('dt', null, term));
    grid.appendChild(el('dd', mono ? 'mono' : null, value));
  };
  const config = server.config;
  if (config && config.transport === 'stdio') {
    fact(t('方式'), t('本地程序（stdio）'));
    fact(t('命令'), mcpCommand(config), true);
    if (config.cwd) fact(t('工作目录'), config.cwd, true);
    if (config.env.length) fact(t('环境变量'), config.env.map((item) => item.name + (item.hint ? ' ' + item.hint : '')).join('\n'), true);
  } else if (config) {
    fact(t('方式'), t('网址（Streamable HTTP）'));
    fact(t('地址'), config.url, true);
    if (config.headers.length) fact(t('请求头'), config.headers.map((item) => item.name + (item.hint ? ' ' + item.hint : '')).join('\n'), true);
  }
  if (data && data.version) fact(t('协议'), data.version + ' · ' + t(MCP_ERA[data.era] || data.era));
  if (data && data.server_info && data.server_info.name) fact(t('服务器自报'), [data.server_info.name, data.server_info.version].filter(Boolean).join(' '));
  if (mcpState.started) fact(t('当前对话'), server.in_use ? t('用着它的 {n} 个工具', { n: server.in_use }) : t('没有用它（对话开始时它还没有连上）'));
  nodes.push(facts);

  nodes.push(mcpToolsCard(server, data, view));
  if (data && data.instructions) {
    const card = mcpCard(t('服务器写给模型的说明'), t('只给你看，没有交给模型'));
    card.appendChild(el('p', 'mcp-help', data.instructions));
    nodes.push(card);
  }

  // 日志：服务器自己写到 stderr 的，加上我们这边记的（启动、退出、出错）
  const logCard = mcpCard(t('日志'), t('最近 300 行'));
  const log = logCard.appendChild(el('pre', 'mcp-log'));
  log.tabIndex = 0;
  log.setAttribute('aria-label', t('服务器的日志'));
  const lines = data ? data.log : [];
  if (!lines.length) log.appendChild(el('span', null, view.loading && !data ? t('正在读取……') : t('还没有输出。')));
  for (const line of lines) {
    const time = el('time', null, new Date(line.time * 1000).toLocaleTimeString(LOCALES[language], { hour12: false }) + '  ');
    log.appendChild(time);
    log.appendChild(el('span', line.source === 'harness' ? 'ours' : null, line.text + '\n'));
  }
  nodes.push(logCard);
  if (view.error) nodes.splice(1, 0, mcpNote('bad', view.error));

  mcpShow(nodes, 'detail:' + view.name, focus);
  const fresh = mcpPanel.querySelector('.mcp-log');
  if (fresh && atBottom) fresh.scrollTop = fresh.scrollHeight;  // 正看着最后几行的话，跟着新的往下走
}

function mcpCard(title, sub) {
  const card = el('div', 'mcp-card');
  const head = card.appendChild(el('h4'));
  head.appendChild(el('span', null, title));
  if (sub) head.appendChild(el('span', 'sub', sub));
  return card;
}

// 工具，以及每一个调用前问不问你
function mcpToolsCard(server, data, view) {
  const tools = data ? data.tools : [];
  const card = mcpCard(t('工具'), data && server.status === 'ready' ? mcpToolCount(tools.length) : '');
  if (!data && view.loading) {
    card.append(el('div', 'mcp-skeleton'), el('div', 'mcp-skeleton'));
    return card;
  }
  if (server.status !== 'ready' || !tools.length) {
    card.appendChild(el('p', 'mcp-help', server.status === 'ready' ? t('这个服务器没有提供工具。') : t('连上之后，这里会列出它的工具。')));
  }
  if (server.config && (server.status === 'ready' || server.approval === 'auto')) {
    const all = card.appendChild(el('div', 'mcp-switch-row'));
    const words = all.appendChild(el('div', 'mcp-tool-words'));
    words.appendChild(el('span', 'mcp-tool-name', t('这个服务器的工具都不用问')));
    words.appendChild(el('span', 'mcp-help', t('打开后，模型调用它的工具不再先问你（下面单独关掉的除外）。只对你信任、而且不会改动重要东西的服务器这样做。')));
    const toggle = all.appendChild(mcpSwitch(server.approval === 'auto', k('“{name}”的工具都不用问', { name: server.name }), { name: server.name }));
    toggle.classList.add('warn');
    toggle.dataset.focus = 'approval-all';
    toggle.addEventListener('click', () => setMcpApproval(server.name, null, server.approval === 'auto' ? 'ask' : 'auto'));
  }
  if (tools.length) {
    const list = card.appendChild(el('ul', 'mcp-tool-list'));
    for (const tool of tools) {
      const row = list.appendChild(el('li', 'mcp-tool'));
      const words = row.appendChild(el('div', 'mcp-tool-words'));
      const name = words.appendChild(el('span', 'mcp-tool-name', tool.name));
      name.title = t('模型看到的名字：{name}', { name: tool.exposed });
      if (tool.read_only) name.appendChild(el('span', 'mcp-hint-tag', t('自称只读'))).title = t('这是服务器自己说的，没有经过验证，不影响要不要问你。');
      if (tool.destructive) name.appendChild(el('span', 'mcp-hint-tag risky', t('自称会改动东西'))).title = t('这是服务器自己说的，没有经过验证，不影响要不要问你。');
      const text = tool.description || tool.title;
      if (text) {
        const opened = view.open.includes(tool.name);
        const desc = words.appendChild(el('span', 'mcp-tool-desc' + (opened ? ' open' : ''), text));
        desc.addEventListener('click', () => {
          view.open = opened ? view.open.filter((item) => item !== tool.name) : [...view.open, tool.name];
          drawMcpDetail();
        });
      }
      const toggle = row.appendChild(mcpSwitch(tool.approval === 'auto', k('调用 {tool} 之前不用问我'), { tool: tool.name }));
      toggle.classList.add('warn');
      toggle.dataset.focus = 'tool:' + tool.name;
      toggle.addEventListener('click', () => setMcpApproval(server.name, tool.name, tool.approval === 'auto' ? 'ask' : 'auto'));
    }
  }
  if (data && data.rejected.length) {
    card.appendChild(mcpNote('warn', t('有 {n} 个工具没有收下：{list}', { n: data.rejected.length, list: data.rejected.map((item) => item.name + '（' + item.reason + '）').join(t('、')) })));
  }
  return card;
}

async function setMcpApproval(name, tool, approval) {
  const reply = await mcpAct(name, '/api/mcp/approval', { name, tool, approval });
  if (!reply.ok) { mcpSay(reply.error, 'bad'); return; }
  const said = approval === 'auto'
    ? (tool ? t('调用 {tool} 之前不再问你。', { tool }) : t('“{name}”的工具都不再问你。', { name }))
    : (tool ? t('调用 {tool} 之前会先问你。', { tool }) : t('“{name}”的工具调用前会先问你。', { name }));
  mcpSay(said, approval === 'auto' ? '' : 'good');
  announce(said);
  if (mcpView.kind === 'detail') loadMcpDetail();
}

// 回到上一层：表单回到它的服务器（编辑时）或者列表；别的都回到列表，焦点回到刚才那一行
function mcpBack() {
  const view = mcpView;
  if (view.kind === 'form' && !leaveMcpForm()) return;
  mcpSay('');
  if (view.kind === 'form' && view.original && mcpFind(view.original)) { openMcpDetail(view.original); return; }
  mcpView = { kind: 'list' };
  mcpDrawn = '';
  drawMcpList(view.name ? 'open:' + view.name : view.kind === 'paste' ? 'paste' : view.kind === 'import' ? 'import' : 'add');
}

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
