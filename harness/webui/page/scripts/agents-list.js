// ---- 扩展 · Agent · 列表 ----------------------------------------------------
// Agent 一页的第一层：照着状态画列表、一个都没有时的说明和三个内置的示例（点一下就加进来），
// 以及每一行："用它开始新对话"，和别处一样的"在访达中显示 / 编辑 / 删除"（删除先在原地确认）。
// 各个子页面共用的"回到上一层"（没保存的先提醒一次），和子页面里的 Esc、⌘Enter，也在这里。

function renderAgents(list) {
  agents = Array.isArray(list) ? list.filter((item) => item && typeof item.name === 'string') : [];
  $('count-agents').textContent = agents.length ? agents.length.toLocaleString(LOCALES[language]) : '';
  if (agentDeleting && !agentByName(agentDeleting)) agentDeleting = null;
  if (!personaMenu.hidden) drawPersonaMenu();
  const view = agentView;
  if (view.kind === 'list') { drawAgentList(); return; }
  const item = agentByName(view.name);
  if (view.kind === 'detail' && view.data && !view.loading) {
    if (!item) {
      if (!view.gone) { view.gone = true; announce(t('这个 Agent 已经不在了。')); paintAgent(); }
      return;
    }
    if (view.gone || item.modified !== view.data.modified) {
      view.gone = false;
      view.said = { key: k('这个 Agent 刚刚在别处改过了，下面是最新的内容。') };
      loadAgent(view);  // 你在访达里改了它：重新读一遍
      return;
    }
  }
  if (view.kind === 'edit' && view.data && !view.saving && view.refresh) {
    if (!item && !view.gone) {
      view.gone = true;
      view.said = { key: k('这个 Agent 已经不在了：可能在别处被删掉或改了名字。你的修改还在框里，可以复制出来。'), bad: true };
      announce(problemText(view.said));
      view.refresh();
    } else if (item && item.modified !== view.data.modified && !view.stale) {
      // 不重新读：重新读会让"打开时的修改时间"跟着变新，保存时就发现不了冲突，别处刚改的东西会被一声不响地冲掉
      view.stale = true;
      view.said = { key: k('这个 Agent 的文件刚刚在别处被改过了。你的修改还在；保存时会先问你要不要覆盖。') };
      announce(problemText(view.said));
      view.refresh();
    }
  }
  if (['examples', 'review'].includes(view.kind) && view.node && !view.refresh) paintAgent('title');  // "已经有了"的标记跟着变
  if (view.language !== language && view.node) {
    view.language = language;
    if (view.refresh) { view.refresh(); if (view.redrawTools) view.redrawTools(); } else paintAgent();
  }
}

function drawAgentList() {
  if (agentView.kind !== 'list') return;
  const print = JSON.stringify([agents, agentDeleting, language, Boolean(agentOptions), busy]);
  if (print === agentsDrawn && agentPanel.firstElementChild) {
    if (agentFocus) { focusAgent(agentFocus); agentFocus = null; }
    return;
  }
  agentsDrawn = print;
  const keep = agentFocus || agentFocusedKey();
  agentFocus = null;
  const view = el('div', 'skill-view');
  if (agents === null) view.appendChild(skillWaiting(k('正在读取……')));
  else if (!agents.length) view.appendChild(agentEmpty());
  else {
    const fresh = knownAgents && isLive() ? agents.filter((agent) => !knownAgents.has(agent.name)).map((agent) => agent.name) : [];
    view.appendChild(agentToolbar());
    const list = view.appendChild(el('ul', 'skill-list'));
    list.setAttribute('aria-label', t('你的 Agent'));
    list.append(...agents.map((agent) => agentRow(agent, fresh.includes(agent.name))));
  }
  if (agents) knownAgents = agentNames();
  agentPanel.replaceChildren(view);
  if (keep) focusAgent(keep);
}

function agentToolbar() {
  const bar = el('div', 'skill-bar');
  const n = agents.length;
  bar.appendChild(tel('p', 'skill-count', n === 1 ? k('1 个 Agent') : k('{n} 个 Agent'), { n: n.toLocaleString(LOCALES[language]) }));
  bar.append(...agentAdders(true));
  const folder = iconButton('folder', byServer(k('在访达中打开 Agent 文件夹'), k('在文件资源管理器中打开 Agent 文件夹'), k('在文件管理器中打开 Agent 文件夹')));
  folder.classList.remove('small');
  folder.dataset.key = 'bar:folder';
  folder.addEventListener('click', () => revealAgent(null));
  bar.appendChild(folder);
  return bar;
}

function agentAdders(withExamples) {
  const buttons = [skillButton('plus', k('新建'), 'primary', 'bar:new', () => startAgentCreate())];
  if (withExamples) buttons.push(skillButton('spark', k('示例'), '', 'bar:examples', () => showAgentView({ kind: 'examples' })));
  buttons.push(skillButton('import', k('从 Claude Code 导入'), '', 'bar:import', startAgentImport));
  return buttons;
}

// 一个都还没有：讲清楚两种用法，再摆上三个示例
function agentEmpty() {
  const box = el('div', 'agents-empty');
  const words = box.appendChild(el('div'));
  words.appendChild(tel('h3', null, k('定制你自己的助手')));
  words.appendChild(tel('p', null, k('一个 Agent 是一份设定：它擅长什么、能用哪些工具、用哪个模型。写好之后有两种用法：')));
  const ways = box.appendChild(el('div', 'agents-ways'));
  const way = (iconName, title, text) => {
    const card = ways.appendChild(el('div', 'agents-way'));
    const head = card.appendChild(el('b'));
    head.append(icon(iconName), tel('span', null, title));
    card.appendChild(tel('p', null, text));
  };
  way('bot', k('主助手交给它去做'), k('主助手遇到对得上的事，会把任务交给它：它在自己的一段对话里查资料、读文件，只把结论交回来，主助手的对话不会被几十页原文塞满。你也可以直接说“让资料研究员查一下……”。'));
  way('chat', k('你直接和它对话'), k('点左边“新对话”旁边的箭头，选一个 Agent：这段对话从头到尾就是它——它的设定、它的工具、它的模型。'));
  box.appendChild(tel('p', 'agents-section-title', k('从示例开始')));
  box.appendChild(agentExampleCards());
  const bar = box.appendChild(el('div', 'skill-bar'));
  bar.append(...agentAdders(false));
  box.appendChild(tel('p', 'agents-hint', k('格式和 Claude Code 的 subagent 一样：一个带 frontmatter 的 Markdown 文件。在那边写好的，可以直接导入。')));
  return box;
}

// 三个示例：点一下就加进来（加过的标着"已添加"，点了直接打开它）
function agentExampleCards() {
  const list = el('ul', 'agents-examples');
  if (!agentOptions) { ensureAgentOptions(); list.appendChild(el('li')).appendChild(skillWaiting(k('正在读取……'))); return list; }
  for (const example of agentOptions.examples || []) {
    const card = list.appendChild(el('li')).appendChild(el('button', 'agents-example'));
    card.type = 'button';
    card.dataset.key = 'example:' + example.id;
    card.dataset.color = example.color || '';
    const have = agentNames().has(example.name);
    card.append(agentFace(example), el('b', null, example.name), tel('span', null, example.blurb));
    const add = card.appendChild(el('span', 'agents-add'));
    add.append(icon(have ? 'check' : 'plus'), tel('span', null, have ? k('已添加，点开看看') : k('添加')));
    card.addEventListener('click', () => (have ? openAgent(example.name) : addAgentExample(example, card)));
  }
  return list;
}

async function addAgentExample(example, card) {
  card.disabled = true;
  const reply = await call('/api/agent/example', { id: example.id });
  card.disabled = false;
  if (!reply.ok) { extensionSay(null, null, 'bad', reply.error); return; }
  announce(t('已添加 Agent：{name}', { name: reply.data.name }));
  openAgent(reply.data.name, false, k('已添加。主助手从下一条消息起就能把任务交给它；也可以先改成你自己的样子。'));
}

function buildAgentExamples() {
  const node = el('div', 'skill-view');
  node.appendChild(agentHead([k('从示例开始')], [k('几个写好的 Agent，加进来以后和你自己写的一样：能交给它、能用它开始对话，也能随便改。')]).head);
  node.appendChild(agentExampleCards());
  return node;
}

function agentRow(agent, fresh) {
  const bad = agent.problems.length > 0, name = agent.name;
  const row = el('li', 'skill-row agents-row' + (bad ? ' bad' : '') + (fresh ? ' enter' : ''));
  row.dataset.name = name;
  row.dataset.color = agent.color || '';

  // 删除前先在原地确认一下，和删除会话、Skill 一样，不弹对话框
  if (agentDeleting === name) {
    const box = row.appendChild(el('div', 'skill-confirm'));
    box.appendChild(el('span', null, t('删除 {name}？它的文件会删掉，不能撤销。用它开始过的会话还在。', { name })));
    const yes = box.appendChild(el('button', 'mini danger', t('删除')));
    const no = box.appendChild(el('button', 'mini cancel', t('取消')));
    yes.type = no.type = 'button';
    yes.dataset.key = 'yes:' + name;
    no.dataset.key = 'cancel:' + name;
    const cancel = () => { agentDeleting = null; agentFocus = 'trash:' + name; agentsDrawn = ''; drawAgentList(); };
    yes.addEventListener('click', async () => {
      yes.disabled = no.disabled = true;
      const index = agents.findIndex((one) => one.name === name);
      const rest = agents.filter((one) => one.name !== name);
      const after = rest.length ? rest[Math.min(index, rest.length - 1)].name : null;
      // 这一行先淡出去，同时请助手程序删掉。删不掉的话它再回来
      const [reply] = await Promise.all([call('/api/agent/delete', { name }), leave(row)]);
      agentDeleting = null;
      if (!reply.ok) { extensionSay(null, null, 'bad', reply.error); agentsDrawn = ''; agentFocus = 'trash:' + name; drawAgentList(); return; }
      agents = rest;
      agentsDrawn = '';
      agentFocus = after ? 'open:' + after : 'bar:new';
      drawAgentList();
      extensionSay(k('已删除 Agent：{name}'), { name }, 'good');
      announce(t('已删除 Agent：{name}', { name }));
    });
    no.addEventListener('click', cancel);
    box.addEventListener('keydown', (key) => {
      if (key.key !== 'Escape') return;
      key.preventDefault();  // 这一下 Esc 只用来取消删除，不关对话框
      key.stopPropagation();
      cancel();
    });
    return row;
  }

  const open = row.appendChild(el('button', 'skill-open'));
  open.type = 'button';
  open.dataset.key = 'open:' + name;
  open.title = agent.description || '';
  open.appendChild(agentFace(agent));
  const words = open.appendChild(el('span', 'skill-words'));
  const line = words.appendChild(el('span', 'skill-line'));
  line.appendChild(el('span', 'skill-name', name));
  if (bad) line.appendChild(el('span', 'skill-flag bad', t('需要修改')));
  else if (agent.warnings && agent.warnings.length) line.appendChild(el('span', 'skill-flag manual', t('有提醒')));
  const showProblem = bad && !agent.description;
  words.appendChild(el('span', 'skill-desc' + (showProblem ? ' bad' : ''), showProblem ? problemText(agent.problems[0]) : agent.description));
  words.appendChild(el('span', 'skill-meta', [agentModelText(agent.model, true), agentToolsText(agent.tools)].join(' · ')));
  open.addEventListener('click', () => openAgent(name));

  // "用它开始新对话"放在三个按钮前面；然后是和别处一样的三个：在访达中显示、编辑、删除。
  // 改和删不用等这一轮对话结束（服务器也不拦）；开始新对话要等，所以它在助手工作时藏起来
  const actions = row.appendChild(el('div', 'skill-actions'));
  const action = (iconName, title, label, extra, key) => {
    const node = actions.appendChild(el('button', 'icon-button small' + (extra ? ' ' + extra : '')));
    node.type = 'button';
    node.title = t(title);
    node.setAttribute('aria-label', t(label, { name }));
    node.dataset.key = key + ':' + name;
    node.appendChild(icon(iconName));
    return node;
  };
  const chat = action('chat', k('用它开始新对话'), k('用 {name} 开始新对话'), 'idle-only', 'chat');
  chat.disabled = bad;
  chat.addEventListener('click', () => startWithAgent(name));
  action('folder', showLabel(), showNamedLabel(), '', 'folder').addEventListener('click', () => revealAgent(name));
  action('edit', k('编辑'), k('编辑 Agent：{name}'), '', 'edit').addEventListener('click', () => openAgent(name, true));
  action('trash', k('删除'), k('删除 Agent：{name}'), 'danger-hover', 'trash').addEventListener('click', () => {
    agentDeleting = name;
    agentFocus = 'cancel:' + name;  // 默认停在"取消"上，免得手快连按两下回车就删掉了
    agentsDrawn = '';
    drawAgentList();
  });
  return row;
}

// 用一个 Agent 开始新对话：先关上扩展（要是有没保存的修改，会先提醒一次）
function startWithAgent(name) {
  if (busy) { extensionSay(BUSY_HINT, null, 'bad'); return; }
  if (extensionsDialog.open && !requestCloseExtensions()) return;
  startNew(name);
}

// 在文件管理器里显示一个 Agent 的文件（name 为 null 时打开整个 Agent 文件夹）。页面只给名字，路径由助手程序算
async function revealAgent(name, view) {
  const reply = await call('/api/agent/reveal', name ? { name } : {});
  const said = reply.ok ? { ...revealedSaid(reply.data.path, !name), good: true } : { text: reply.error, bad: true };
  if (view && agentView === view) { view.said = said; paintAgent(); }
  else extensionSay(said.key || null, said.values, said.bad ? 'bad' : 'good', said.text);
}

// 离开子页面，回到上一层。正在编辑、还没保存的，先提醒一次
function leaveAgentView() {
  const view = agentView;
  if (!agentMayLeave()) return;
  if (view.kind === 'edit') { openAgent(view.name); return; }
  const from = { detail: 'open:' + view.name, create: 'bar:new', examples: 'bar:examples', review: 'bar:import' }[view.kind];
  showAgentView({ kind: 'list' }, from);
}

// 编辑、新建里有没保存的东西：第一次只提醒，紧接着再来一次才真的放弃
function agentMayLeave() {
  const view = agentView;
  if (!['edit', 'create'].includes(view.kind) || !view.dirty || !view.dirty() || view.armed) return true;
  view.armed = true;
  view.said = { key: k('还有没保存的修改，再按一次就放弃。'), bad: true };
  paintAgent();
  return false;
}

agentPanel.addEventListener('keydown', (key) => {
  if (key.isComposing || key.keyCode === 229) return;  // 输入法正在组字时，Esc 和回车是给输入法的
  if (key.key === 'Escape' && agentView.kind !== 'list' && !key.defaultPrevented) {
    key.preventDefault();
    key.stopPropagation();
    leaveAgentView();
  } else if (key.key === 'Enter' && commandHeld(key) && agentView.save) {
    key.preventDefault();
    agentView.save();
  }
});
