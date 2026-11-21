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
