// ---- 扩展 · Agent -----------------------------------------------------------
// 自定义 Agent：你定制的、擅长某一类事的助手（见 harness/agents/）。一个 Agent 是 data/agents/ 里的一个 Markdown 文件，
// 同一个文件有两种用法：主助手把任务交给它（子 Agent），或者你用它开始一段新对话（角色，见下一节"新对话旁边的选择"）。
// 这一页管这些文件：列出来、打开看、编辑（表单或者原文）、新建、删除、加内置的示例、从 Claude Code 导入。
//
// 列表跟着状态走（state.agents），打开一个、编辑时要的选项（能勾的工具、示例）另外请求。
// 一个文件合不合格、表单怎么写成文件，都由助手程序来算（/api/agent/parse、/api/agent/compose），页面不另写一份规矩：
// 页面上的检查只是"有没有填"这种马上能看出来的，真正的问题列表都是程序给的。
// 这一页有几种样子（view.kind）：list 列表、detail 打开的一个、edit 编辑、create 新建、examples 示例、review 从 Claude Code 导入。
// 版式和 Skill 一页一样，借用它的几个小工具（skillButton、skillSaid、skillWaiting）。
// 改 Agent 不用等这一轮对话结束（服务器也不拦）：正在进行的对话和子 Agent 用的是开始时读到的那一份。

const agentPanel = $('panel-agents');
// tools 一项里能勾的那几样（名字和 harness/agents/tools.py 的 BUILTIN 一样，哪几样能勾由程序在 options 里给）
const AGENT_TOOL_LABELS = {
  Read: k('读文件'), Edit: k('读写文件'), Bash: k('执行命令'), WebSearch: k('联网搜索'), WebFetch: k('读网页'),
  Memory: k('长期记忆'), TodoWrite: k('待办'), Schedule: k('定时任务'), Skill: k('读取你装的 Skill'), Agent: k('交给别的 Agent'),
  Browser: k('用智狐的浏览器'), Computer: k('操作整台电脑'),
};
const AGENT_COLOR_NAMES = {
  red: k('红色'), orange: k('橙色'), yellow: k('黄色'), green: k('绿色'), cyan: k('青色'), blue: k('蓝色'), purple: k('紫色'), pink: k('粉色'),
};
const MAX_AGENT_DESCRIPTION = 1024;  // 只用来显示计数；超了由程序说
let agents = null;          // 最新的列表（来自状态）：[{name, description, model, color, tools: {all, count}, problems, warnings, modified}]
let knownAgents = null;     // 上一次画列表时有哪些：新出现的那几个浮现一下
let agentsDrawn = '';
let agentView = { kind: 'list' };
let agentDeleting = null;   // 列表里正在原地确认删除的那一个
let agentFocus = null;      // 下一次画完之后焦点放到哪儿（data-key）
let agentSeq = 0;           // 每发一次请求加一。回来得晚的旧结果靠它认出来、丢掉
let agentOptions = null;    // /api/agent/options 的回答：能勾的工具（内置的、MCP 的）、颜色、示例、导入时去哪里找
let agentOptionsAsked = null;

const agentByName = (name) => (agents || []).find((one) => one.name === name) || null;
const agentNames = () => new Set((agents || []).map((one) => one.name));
// 一个 Agent 的"脸"：带颜色的小方块，里面是名字的第一个字（拉丁字母大写）
function agentFace(agent, size) {
  const first = Array.from(String((agent && agent.name) || '?').trim())[0] || '?';
  const face = el('span', 'agents-face' + (size ? ' ' + size : ''), first.toLocaleUpperCase(LOCALES[language]));
  face.dataset.color = (agent && agent.color) || '';
  face.setAttribute('aria-hidden', 'true');
  return face;
}
// 用哪个模型："提供方/模型" → "Claude (Anthropic) · claude-sonnet-5-5"；空的是"和叫它的一方一样"
function agentModelText(model, short) {
  if (!model) return t('跟随叫它的一方');
  const [provider, ...rest] = String(model).split('/');
  const id = rest.join('/');
  if (short) return id;
  const found = state && state.providers.find((one) => one.id === provider);
  return (found ? found.label : provider) + ' · ' + id;
}
function agentToolsText(tools) {
  if (!tools || tools.all) return t('全部工具');
  if (!tools.count) return t('不用工具');
  return tools.count === 1 ? t('1 样工具') : t('{n} 样工具', { n: tools.count.toLocaleString(LOCALES[language]) });
}
const agentToolLabel = (name) => (AGENT_TOOL_LABELS[name] ? t(AGENT_TOOL_LABELS[name]) : name);

function ensureAgentOptions() {
  if (agentOptions || agentOptionsAsked) return agentOptionsAsked;
  agentOptionsAsked = call('/api/agent/options').then((reply) => {
    agentOptionsAsked = null;
    if (!reply.ok) return;
    agentOptions = reply.data;
    const view = agentView;
    if (view.redrawTools) view.redrawTools();
    else if (['list', 'examples'].includes(view.kind)) { agentsDrawn = ''; paintAgent(); }
  });
  return agentOptionsAsked;
}

// 焦点：重画之前记下它在哪个 data-key 上，画完放回去
const agentFocusedKey = () => (agentPanel.contains(document.activeElement) && document.activeElement.dataset.key) || null;
function focusAgent(key, fallback) {
  if (!extensionsDialog.open || agentPanel.hidden) return;
  const find = (name) => name && [...agentPanel.querySelectorAll('[data-key]')].find((node) => node.dataset.key === name);
  const target = find(key) || find(fallback) || agentPanel.querySelector('.skill-open') || find('bar:new');
  if (target) target.focus();
}

function showAgentView(view, focus) {
  agentSeq += 1;  // 之前那种样子还没回来的请求，回来了也不要了
  agentView = view;
  agentDeleting = null;
  if (view.kind === 'list') { agentsDrawn = ''; agentFocus = focus || null; drawAgentList(); }
  else paintAgent(focus || (view.kind === 'create' ? 'name' : view.kind === 'edit' ? (view.mode === 'raw' ? 'raw' : 'name') : 'title'));
  const node = agentPanel.firstElementChild;
  if (node) play(node, [{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }], 220);
  agentPanel.scrollTop = 0;
}

// 照着 agentView 画一遍。有输入框的（编辑、新建），画过之后只更新提示（view.refresh），你打的字不动
function paintAgent(focus) {
  const view = agentView;
  if (view.kind === 'list') { drawAgentList(); return; }
  if (view.refresh && view.node && view.node.isConnected && focus === undefined) { view.refresh(); return; }
  const keep = focus || agentFocusedKey();
  const build = { detail: buildAgentDetail, edit: buildAgentEditor, create: buildAgentEditor, examples: buildAgentExamples, review: buildAgentReview }[view.kind];
  view.redrawTools = null;
  view.node = build(view);
  view.language = language;
  agentPanel.replaceChildren(view.node);
  if (view.refresh) view.refresh();
  focusAgent(keep, 'back');
}

// 子页面的头：返回、（它的脸）、标题、一句说明。title 是 [原文] 时按原文翻译，否则是数据（Agent 的名字）原样显示
function agentHead(title, lead, agent) {
  const head = el('div', 'skill-head agents-head');
  const back = iconButton('back', k('返回 Agent 列表'));
  back.classList.remove('small');
  back.dataset.key = 'back';
  back.addEventListener('click', () => leaveAgentView());
  head.appendChild(back);
  if (agent) head.appendChild(agentFace(agent, 'large'));
  const words = head.appendChild(el('div', 'skill-head-text'));
  const heading = words.appendChild(Array.isArray(title) ? tel('h3', 'skill-title plain', title[0], title[1]) : el('h3', 'skill-title plain', title));
  heading.tabIndex = -1;
  heading.dataset.key = 'title';
  if (Array.isArray(lead)) words.appendChild(tel('p', 'skill-lead', lead[0], lead[1]));
  else if (lead) words.appendChild(el('p', 'skill-lead', lead));
  return { head, words };
}

// ---- 列表 ----

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
  const folder = iconButton('folder', ON_MAC ? k('在访达中打开 Agent 文件夹') : k('打开 Agent 文件夹'));
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
  action('folder', SHOW_FILE, ON_MAC ? k('在访达中显示：{name}') : k('在文件夹中显示：{name}'), '', 'folder').addEventListener('click', () => revealAgent(name));
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
  const shown = shortPath(reply.data.path);
  const said = !reply.ok ? { text: reply.error, bad: true }
    : state && state.file_manager === false ? { key: k('这台电脑上没有文件管理器。文件在：{path}'), values: { path: String(reply.data.path || '') }, good: true }
    : name ? { key: ON_MAC ? k('已在访达中显示：{path}') : k('已在文件夹中显示：{path}'), values: { path: shown }, good: true }
      : { key: ON_MAC ? k('已在访达中打开：{path}') : k('已打开文件夹：{path}'), values: { path: shown }, good: true };
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
  } else if (key.key === 'Enter' && (ON_MAC ? key.metaKey : key.ctrlKey) && agentView.save) {
    key.preventDefault();
    agentView.save();
  }
});

// ---- 打开一个 ----

function openAgent(name, edit, said) {
  showAgentView({ kind: 'detail', name, data: null, loading: true, error: '', confirm: false, said: said ? { key: said, good: true } : null, edit: Boolean(edit) });
  loadAgent(agentView);
}

async function loadAgent(view) {
  const mine = ++agentSeq;
  view.loading = true;
  const reply = await call('/api/agent/read', { name: view.name });
  if (mine !== agentSeq || agentView !== view) return;
  view.loading = false;
  if (!reply.ok) { view.error = reply.error; view.data = null; paintAgent(); return; }
  view.error = '';
  view.data = reply.data;
  if (view.edit) { view.edit = false; startAgentEdit(view); return; }
  paintAgent();
}

function buildAgentDetail(view) {
  const node = el('div', 'skill-view skill-fill');
  const data = view.data;
  const item = data || agentByName(view.name) || { name: view.name };
  const { head } = agentHead(view.name, data ? data.description : '', item);
  node.appendChild(head);
  if (!data) {
    if (view.error) node.appendChild(el('p', 'skill-problems single', view.error));
    else node.appendChild(skillWaiting(k('正在读取……')));
    return node;
  }
  // 几项事实：用哪个模型、能用哪些工具
  const facts = node.appendChild(el('dl', 'agents-facts'));
  const fact = (label) => {
    const box = facts.appendChild(el('div', 'agents-fact'));
    box.appendChild(tel('dt', null, label));
    return box.appendChild(el('dd'));
  };
  const model = fact(k('模型'));
  model.textContent = agentModelText(data.model);
  const tools = fact(k('能用的工具'));
  const form = data.fields ? data.fields.tools : null;
  tools.textContent = agentToolsText(data.tools);
  if (form && !form.all && (form.picked.length || form.unknown.length)) {
    const chips = tools.appendChild(el('div', 'agents-tools'));
    for (const name of form.picked) chips.appendChild(el('span', null, agentToolLabel(name))).title = name;
  }
  const notes = [...(data.warnings || [])];
  if (data.extra && data.extra.length) notes.push({ key: k('智狐不用的字段（原样留在文件里）：{fields}'), values: { fields: data.extra.join(', ') } });
  if (notes.length) {
    const box = node.appendChild(el('ul', 'agents-warn' + (notes.length === 1 ? ' single' : '')));
    box.style.listStyle = notes.length === 1 ? 'none' : '';
    for (const note of notes) box.appendChild(el('li', null, problemText(note)));
  }
  if (view.gone || data.problems.length) {
    const problems = node.appendChild(el('ul', 'skill-problems' + (view.gone || data.problems.length === 1 ? ' single' : '')));
    problems.setAttribute('role', 'alert');
    if (view.gone) problems.appendChild(tel('li', null, k('这个 Agent 已经不在了：可能在别处被删掉或改了名字。')));
    else for (const problem of data.problems) problems.appendChild(el('li', null, problemText(problem)));
    if (!view.gone) problems.appendChild(tel('li', null, k('有问题的 Agent 不会告诉主助手，也不能用它开始对话。点“编辑”改好就行。')));
  }
  const body = node.appendChild(el('pre', 'agents-body'));
  body.tabIndex = 0;  // 能用键盘滚动
  tattr(body, 'aria-label', k('它的设定'));
  if (data.fields) body.textContent = data.fields.body;
  else if (data.text) body.textContent = data.text;
  else retext(body, k('这个文件读不出来（太大，或者不是 UTF-8 编码的文字）。可以在文件夹里用别的软件打开。'));

  const foot = node.appendChild(el('div', 'skill-foot'));
  const said = foot.appendChild(skillSaid(el('p'), view.confirm ? { key: k('删除之后，这个 Agent 的文件就没了，不能撤销。用它开始过的会话还在。'), bad: true } : view.said));
  said.setAttribute('role', 'status');
  if (view.confirm) {
    foot.appendChild(skillButton('', k('删除'), 'danger', 'confirm-delete', () => deleteViewedAgent(view)));
    foot.appendChild(skillButton('', k('取消'), '', 'confirm-cancel', () => { view.confirm = false; paintAgent('delete'); }));
    return node;
  }
  const start = foot.appendChild(skillButton('chat', k('用它开始新对话'), 'primary idle-only', 'start', () => startWithAgent(view.name)));
  start.disabled = view.gone || data.problems.length > 0;
  foot.appendChild(skillButton('edit', k('编辑'), '', 'edit', () => startAgentEdit(view))).disabled = view.gone || !data.editable;
  foot.appendChild(skillButton('folder', SHOW_FILE, '', 'reveal', () => revealAgent(view.name, view))).disabled = view.gone;
  foot.appendChild(skillButton('trash', k('删除'), '', 'delete', () => { view.confirm = true; paintAgent('confirm-cancel'); })).disabled = view.gone;
  return node;
}

async function deleteViewedAgent(view) {
  for (const button of view.node.querySelectorAll('.skill-foot button')) button.disabled = true;
  const reply = await call('/api/agent/delete', { name: view.name });
  if (agentView !== view) return;
  if (!reply.ok) { view.confirm = false; view.said = { text: reply.error, bad: true }; paintAgent('delete'); return; }
  const index = (agents || []).findIndex((one) => one.name === view.name);
  agents = (agents || []).filter((one) => one.name !== view.name);
  const after = agents.length ? agents[Math.min(Math.max(index, 0), agents.length - 1)].name : null;
  showAgentView({ kind: 'list' }, after ? 'open:' + after : 'bar:new');
  extensionSay(k('已删除 Agent：{name}'), { name: view.name }, 'good');
  announce(t('已删除 Agent：{name}', { name: view.name }));
}

// ---- 编辑和新建 ----
// 两种写法，上面一排切换：表单（名字、说明、模型、颜色、工具、设定），或者原文（整个文件）。
// 切换时请助手程序换算（表单 → 原文用 compose，原文 → 原文用 parse），保存的永远是一份原文：按表单保存的，
// 也是先由 compose 写成原文（只改你改过的那几项，文件里别的东西原样留着），再交给 save。

// 表单的样子 ← 程序给的 fields（新建时是空的）
function agentForm(fields) {
  const tools = fields ? fields.tools : { all: true, picked: [], unknown: [] };
  return {
    name: fields ? fields.name : '', description: fields ? fields.description : '', model: fields ? fields.model : '',
    color: fields ? fields.color : '', body: fields ? fields.body : '',
    all: tools.all, picked: [...tools.picked], unknown: [...tools.unknown],
  };
}
// 交给 compose 的那一份：tools 为 null 表示"和叫它的一方一样"
function agentFields(form) {
  return {
    name: form.name.trim(), description: form.description.trim(), model: form.model, color: form.color, body: form.body,
    tools: form.all ? null : [...form.picked, ...form.unknown],
  };
}

function startAgentEdit(view) {
  const data = view.data;
  if (!data) return;
  if (!data.editable) { view.said = { key: k('这个文件读不出来（太大，或者不是 UTF-8 编码的文字），在这里改会把它弄坏。请在文件夹里用别的软件编辑。'), bad: true }; paintAgent(); return; }
  const form = agentForm(data.fields);
  showAgentView({
    kind: 'edit', name: view.name, data, base: data.text, mode: data.fields ? 'form' : 'raw', form, raw: data.text,
    baseForm: JSON.stringify(agentFields(form)), baseRaw: data.text, touched: false,
    saving: false, force: false, armed: false, stale: false, gone: false, problems: [],
    said: data.fields ? null : { key: k('frontmatter 读不出来，只能在原文里改。改好以后可以切回表单。'), bad: true },
  });
  ensureAgentOptions();
}

function startAgentCreate() {
  const form = agentForm(null);
  showAgentView({
    kind: 'create', name: '', data: null, base: null, mode: 'form', form, raw: '', baseForm: JSON.stringify(agentFields(form)), baseRaw: '',
    touched: false, saving: false, force: false, armed: false, problems: [], said: null,
  });
  ensureAgentOptions();
}

async function switchAgentMode(view, mode) {
  if (view.mode === mode || view.switching || agentView !== view) return;
  view.switching = true;
  view.said = null;
  const reply = mode === 'raw'
    ? await call('/api/agent/compose', { fields: agentFields(view.form), base: view.base })
    : await call('/api/agent/parse', { text: view.raw });
  view.switching = false;
  if (agentView !== view) return;
  if (!reply.ok) { view.said = { text: reply.error, bad: true }; view.refresh(); return; }
  if (mode === 'raw') {
    view.raw = reply.data.text;
    if (!view.touched) view.baseRaw = view.raw;  // 没改过：换算出来的原文就是"没改过"的样子
  } else if (!reply.data.fields) {
    view.said = { key: k('frontmatter 读不出来（看看第一行是不是 ---），先在原文里改好再切回表单。'), bad: true };
    view.refresh();
    return;
  } else {
    view.form = agentForm(reply.data.fields);
    if (!view.touched) view.baseForm = JSON.stringify(agentFields(view.form));
  }
  view.mode = mode;
  view.problems = [];
  paintAgent('mode:' + mode);
}

function buildAgentEditor(view) {
  const creating = view.kind === 'create';
  const node = el('div', 'skill-view skill-fill' + (creating ? ' skill-create' : ''));
  const { head } = agentHead(creating ? [k('新建 Agent')] : view.name, creating
    ? [k('写下它叫什么、擅长什么、能用哪些工具，和它的设定。保存后，Agent 文件夹里会多一个同名的 .md 文件。')]
    : [k('改的是这个 Agent 的文件。按表单保存时只改你改过的那几项，文件里别的内容（注释、智狐不用的字段）原样留着。')], creating ? null : view.form);
  node.appendChild(head);

  // 表单 / 原文（单选的一组：读屏软件读得出"选中了哪个"）
  const modes = node.appendChild(el('div', 'agents-modes'));
  modes.setAttribute('role', 'radiogroup');
  tattr(modes, 'aria-label', k('写法'));
  for (const [mode, label] of [['form', k('表单')], ['raw', k('原文')]]) {
    const button = modes.appendChild(tel('button', 'agents-mode', label));
    button.type = 'button';
    button.setAttribute('role', 'radio');
    button.setAttribute('aria-checked', String(view.mode === mode));
    button.tabIndex = view.mode === mode ? 0 : -1;
    button.dataset.key = 'mode:' + mode;
    button.addEventListener('click', () => switchAgentMode(view, mode));
  }
  modes.addEventListener('keydown', (key) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(key.key)) return;
    key.preventDefault();
    switchAgentMode(view, view.mode === 'form' ? 'raw' : 'form');
  });

  const touch = () => {
    view.touched = true;
    // 提醒过"再按一次就放弃"之后又接着改了，就当没提醒过；上一次保存的结果也该收起来了
    if (view.armed || (view.said && view.said.bad && !view.stale && !view.gone)) { view.armed = false; view.said = null; }
    view.refresh();
  };
  let nameHelp = null, nameInput = null, count = null, description = null;
  if (view.mode === 'raw') {
    const field = node.appendChild(el('label', 'skill-field grow'));
    const top = field.appendChild(el('span', 'skill-field-head'));
    top.appendChild(tel('span', null, k('整个文件（Markdown）')));
    top.appendChild(tel('span', 'skill-help', SAVE_HINT));
    const area = field.appendChild(el('textarea', 'field skill-code'));
    area.spellcheck = false;
    area.value = view.raw;
    area.dataset.key = 'raw';
    area.addEventListener('input', () => { view.raw = area.value; touch(); });
  } else {
    const form = node.appendChild(el('div', 'skill-form'));
    const field = (parent, label, help, counter) => {
      const box = parent.appendChild(el('label', 'skill-field'));
      const top = box.appendChild(el('span', 'skill-field-head'));
      top.appendChild(tel('span', null, label));
      const counted = counter ? top.appendChild(el('span', 'skill-counter')) : null;
      return { box, count: counted, help: help ? tel('span', 'skill-help', help) : null };
    };
    const grid = form.appendChild(el('div', 'agents-grid'));
    const nameField = field(grid, k('名字'), k('字母（中文也可以）、数字、连字符和下划线，不能有空格。它也是文件名。'));
    nameInput = nameField.box.appendChild(el('input', 'field'));
    nameInput.type = 'text';
    nameInput.maxLength = 80;
    nameInput.spellcheck = false;
    nameInput.autocomplete = 'off';
    tattr(nameInput, 'placeholder', k('资料研究员'));
    nameInput.dataset.key = 'name';
    nameInput.value = view.form.name;
    nameField.box.appendChild(nameField.help);
    nameHelp = nameField.help;
    nameInput.addEventListener('input', () => { view.form.name = nameInput.value; touch(); });

    const modelField = field(grid, k('模型'), k('跟随叫它的一方：主助手用哪个，它就用哪个。'));
    const select = modelField.box.appendChild(el('select', 'field'));
    select.dataset.key = 'model';
    fillAgentModels(select, view.form.model);
    modelField.box.appendChild(modelField.help);
    select.addEventListener('change', () => { view.form.model = select.value; touch(); });

    const descriptionField = field(form, k('说明'), k('它擅长什么、什么时候该把事情交给它。主助手就是靠这一句决定交不交给它的。'), true);
    description = descriptionField.box.appendChild(el('textarea', 'field'));
    description.rows = 2;
    description.dataset.key = 'description';
    tattr(description, 'placeholder', k('查资料、读网页，交回一份带出处的摘要。需要查很多资料、只要结论的时候交给它。'));
    description.value = view.form.description;
    descriptionField.box.appendChild(descriptionField.help);
    count = descriptionField.count;
    description.addEventListener('input', () => { view.form.description = description.value; touch(); });

    // 颜色：九个小方块（不选 + 八种），方向键在它们之间移动
    const colorField = form.appendChild(el('div', 'skill-field'));
    colorField.appendChild(el('span', 'skill-field-head')).appendChild(tel('span', null, k('颜色')));
    const colors = colorField.appendChild(el('div', 'agents-colors'));
    colors.setAttribute('role', 'radiogroup');
    tattr(colors, 'aria-label', k('颜色'));
    const palette = ['', ...((agentOptions && agentOptions.colors) || Object.keys(AGENT_COLOR_NAMES))];
    const pick = (value) => {
      view.form.color = value;
      for (const swatch of colors.children) {
        const on = swatch.dataset.value === value;
        swatch.setAttribute('aria-checked', String(on));
        swatch.tabIndex = on ? 0 : -1;
      }
      touch();
    };
    for (const value of palette) {
      const swatch = colors.appendChild(el('button', 'agents-color' + (value ? '' : ' none')));
      swatch.type = 'button';
      swatch.setAttribute('role', 'radio');
      swatch.dataset.color = value;
      swatch.dataset.value = value;
      swatch.dataset.key = 'color:' + value;
      const label = value ? AGENT_COLOR_NAMES[value] || value : k('不选');
      tattr(swatch, 'title', label);
      tattr(swatch, 'aria-label', label);
      swatch.setAttribute('aria-checked', String(view.form.color === value));
      swatch.tabIndex = view.form.color === value ? 0 : -1;
      swatch.addEventListener('click', () => pick(value));
    }
    colors.addEventListener('keydown', (key) => {
      const at = palette.indexOf(view.form.color);
      const next = { ArrowRight: at + 1, ArrowDown: at + 1, ArrowLeft: at - 1, ArrowUp: at - 1, Home: 0, End: palette.length - 1 }[key.key];
      if (next === undefined) return;
      key.preventDefault();
      const value = palette[(next + palette.length) % palette.length];
      pick(value);
      colors.children[palette.indexOf(value)].focus();
    });

    const toolsField = form.appendChild(el('div', 'skill-field'));
    toolsField.appendChild(el('span', 'skill-field-head')).appendChild(tel('span', null, k('能用的工具')));
    const toolsHolder = toolsField.appendChild(el('div'));
    view.redrawTools = () => {
      if (!toolsHolder.isConnected) return;
      const keep = agentFocusedKey();
      toolsHolder.replaceChildren(agentToolsBox(view, touch));
      if (keep && keep.startsWith('tool')) focusAgent(keep);
    };
    toolsHolder.appendChild(agentToolsBox(view, touch));

    const bodyField = field(form, k('设定'), k('写给它看的：它是谁、怎么做事、交回什么。可以用 {workspace} 这些占位符（和提示词的写法一样）。'));
    bodyField.box.classList.add('grow');
    const steps = bodyField.box.appendChild(el('textarea', 'field skill-code agents-instructions'));
    steps.spellcheck = false;
    steps.dataset.key = 'body';
    tattr(steps, 'placeholder', k('你是一个细心的研究员。先把问题拆成几个小问题，每个至少找两个来源；交回时开头先给结论，每一条后面写上出处。'));
    steps.value = view.form.body;
    bodyField.box.appendChild(bodyField.help);
    steps.addEventListener('input', () => { view.form.body = steps.value; touch(); });
  }

  const problems = node.appendChild(el('ul', 'skill-problems'));
  problems.setAttribute('role', 'alert');
  const foot = node.appendChild(el('div', 'skill-foot'));
  const said = foot.appendChild(el('p'));
  said.setAttribute('role', 'status');
  foot.appendChild(skillButton('', k('取消'), '', 'cancel', () => leaveAgentView()));
  const save = foot.appendChild(skillButton(creating ? 'plus' : '', creating ? k('创建') : k('保存'), 'primary', 'save', () => saveAgent(view)));

  view.dirty = () => view.touched && (view.mode === 'raw' ? view.raw !== view.baseRaw : JSON.stringify(agentFields(view.form)) !== view.baseForm);
  view.save = () => saveAgent(view);
  view.refresh = () => {
    problems.hidden = !view.problems.length;
    problems.className = 'skill-problems' + (view.problems.length === 1 ? ' single' : '');
    problems.replaceChildren(...view.problems.map((problem) => el('li', null, problemText(problem))));
    if (nameInput) {
      const typed = nameInput.value.trim();
      const taken = typed && typed !== view.name && agentNames().has(typed);
      nameInput.setAttribute('aria-invalid', String(Boolean(taken)));
      nameHelp.classList.toggle('bad', Boolean(taken));
      retext(nameHelp, taken ? k('已经有一个叫 {name} 的 Agent 了。') : k('字母（中文也可以）、数字、连字符和下划线，不能有空格。它也是文件名。'), taken ? { name: typed } : undefined);
    }
    if (count) {
      const length = description.value.trim().length;
      count.textContent = length.toLocaleString(LOCALES[language]) + ' / ' + MAX_AGENT_DESCRIPTION.toLocaleString(LOCALES[language]);
      count.classList.toggle('over', length > MAX_AGENT_DESCRIPTION);
    }
    for (const button of modes.children) button.disabled = Boolean(view.switching || view.saving);
    skillSaid(said, view.said || { key: SAVE_HINT });
    retext(save.lastChild, view.saving ? (creating ? k('正在创建……') : k('正在保存……')) : view.force ? k('覆盖保存') : creating ? k('创建') : k('保存'));
    save.disabled = Boolean(view.saving || view.gone);
  };
  return node;
}

// 模型的下拉框：跟随，然后按提供方分组（同步到的模型列表，见设置）。文件里写的不在列表里的，单独放在最前面，不丢
function fillAgentModels(select, current) {
  const inherit = select.appendChild(el('option', null, t('跟随叫它的一方（inherit）')));
  inherit.value = '';
  const known = new Set();
  for (const provider of (state && state.providers) || []) {
    if (!provider.models.length) continue;
    const group = select.appendChild(el('optgroup'));
    group.label = provider.label + (provider.has_key ? '' : ' ' + t('（还没有密钥）'));
    for (const model of provider.models) {
      const value = provider.id + '/' + model;
      known.add(value);
      group.appendChild(el('option', null, model)).value = value;
    }
  }
  if (current && !known.has(current) && current !== 'inherit') {
    const written = el('option', null, t('文件里写的：{model}', { model: current }));
    written.value = current;
    select.insertBefore(written, inherit.nextSibling);
  }
  select.value = current === 'inherit' ? '' : current || '';
}

// 工具的清单。先选"和叫它的一方一样多"还是"只用勾选的"；勾选的按三组排：内置的、Skill、MCP（按服务器）。
// 勾了任何一项就自动换成"只用勾选的"。勾了"读写文件"，"读文件"跟着勾上（写文件的本来就能读）
function agentToolsBox(view, touch) {
  const form = view.form;
  const box = el('div', 'agents-tools-box');
  box.dataset.all = String(form.all);
  const choice = box.appendChild(el('div', 'agents-choice'));
  choice.setAttribute('role', 'radiogroup');
  tattr(choice, 'aria-label', k('能用的工具'));
  const redraw = () => { touch(); if (view.redrawTools) view.redrawTools(); };
  for (const [all, label] of [[true, k('和叫它的一方一样：主助手手里有的，它都能用')], [false, k('只用下面勾选的')]]) {
    const wrap = choice.appendChild(el('label'));
    const radio = wrap.appendChild(el('input'));
    radio.type = 'radio';
    radio.name = 'agent-tools-' + view.kind;
    radio.checked = form.all === all;
    radio.dataset.key = 'tools:' + all;
    radio.addEventListener('change', () => { form.all = all; redraw(); });
    wrap.appendChild(tel('span', null, label));
  }
  if (!agentOptions) { box.appendChild(skillWaiting(k('正在读取能用的工具……'))); return box; }
  const picked = new Set(form.picked);
  const toggle = (name, on) => {
    form.all = false;
    if (on) picked.add(name); else picked.delete(name);
    if (name === 'Edit' && on) picked.delete('Read');  // "读写"已经包括"读"，不用两样都记
    form.picked = [...picked];
    redraw();
  };
  const check = (parent, name, label, code, extra) => {
    const wrap = parent.appendChild(el('label', 'agents-check' + (extra || '')));
    const input = wrap.appendChild(el('input'));
    input.type = 'checkbox';
    input.checked = picked.has(name);
    input.dataset.key = 'tool:' + name;
    input.addEventListener('change', () => toggle(name, input.checked));
    const words = wrap.appendChild(el('span'));
    words.appendChild(el('b', null, label));
    if (code) words.appendChild(el('code', null, code));
    return input;
  };
  const group = (title) => {
    const holder = box.appendChild(el('div', 'agents-group'));
    holder.appendChild(tel('h4', null, title));
    return holder.appendChild(el('div', 'agents-checks'));
  };
  const builtin = group(k('内置的'));
  for (const name of agentOptions.builtin.filter((one) => one !== 'Skill')) {
    const input = check(builtin, name, agentToolLabel(name), name);
    if (name === 'Read' && picked.has('Edit')) { input.checked = true; input.disabled = true; }
  }
  if (agentOptions.builtin.includes('Skill')) {
    const skillsGroup = group(k('Skill'));
    const names = (skills || []).filter((skill) => skill.enabled && !skill.problems.length).map((skill) => skill.name);
    check(skillsGroup, 'Skill', agentToolLabel('Skill'), names.length ? names.join(' · ') : t('还没有能用的 Skill'));
  }
  // MCP：每个服务器一块，先是"整个服务器"，下面缩进一层是它的每个工具（勾了整个服务器，下面的就都算上了）
  const mcpHolder = box.appendChild(el('div', 'agents-group'));
  mcpHolder.appendChild(tel('h4', null, k('MCP')));
  const mcp = mcpHolder.appendChild(el('div'));
  const offered = new Set();
  for (const server of agentOptions.mcp || []) {
    offered.add(server.key);
    const block = mcp.appendChild(el('div', 'agents-server'));
    const whole = check(block, server.key, server.server, t('这个服务器的全部工具'));
    const tools = block.appendChild(el('div', 'agents-checks'));
    for (const tool of server.tools) {
      offered.add(tool.name);
      const input = check(tools, tool.name, tool.remote, tool.name);
      if (whole.checked) { input.checked = true; input.disabled = true; }
    }
  }
  // 文件里写着、但现在没有连上的 MCP 工具：照样列出来（勾着），取消勾选才会从文件里去掉
  const away = form.picked.filter((one) => one.startsWith('mcp__') && !offered.has(one));
  if (away.length) {
    const block = mcp.appendChild(el('div', 'agents-server')).appendChild(el('div', 'agents-checks'));
    for (const name of away) check(block, name, name, t('现在没有连上'));
  }
  if (!mcp.children.length) mcp.appendChild(tel('p', 'agents-hint', k('现在没有连上的 MCP 服务器。在“MCP”一页接好之后，这里就能勾它的工具。')));
  if (form.unknown.length) {
    const rest = box.appendChild(el('p', 'agents-unknown'));
    rest.appendChild(tel('span', null, k('文件里还写着这些，智狐没有，会原样留着：')));
    for (const name of form.unknown) rest.appendChild(el('span', null, name));
  }
  box.appendChild(tel('p', 'agents-hint', k('不管勾了什么，它都不会比叫它的那一方多：一个只许读文件的角色叫来的 Agent，勾了“读写文件”也改不了文件。改文件、执行命令照样会先问你。')));
  return box;
}

async function saveAgent(view) {
  if (agentView !== view || view.saving) return;
  const creating = view.kind === 'create';
  if (view.mode === 'form' && !view.form.name.trim()) { focusAgent('name'); return; }
  view.saving = true;
  view.armed = false;
  view.problems = [];
  view.refresh();
  let text = view.raw;
  if (view.mode === 'form') {
    const composed = await call('/api/agent/compose', { fields: agentFields(view.form), base: view.base });
    if (agentView !== view) return;
    if (!composed.ok) { view.saving = false; view.said = { text: composed.error, bad: true }; view.refresh(); return; }
    text = composed.data.text;
  }
  const reply = creating
    ? await call('/api/agent/create', { text })
    : await call('/api/agent/save', { name: view.name, text, modified: view.data.modified, force: view.force });
  if (agentView !== view) return;
  view.saving = false;
  if (!reply.ok) {
    if (reply.data && reply.data.conflict) view.force = true;  // 再点一次就覆盖
    view.problems = reply.data && Array.isArray(reply.data.problems) ? reply.data.problems : [];
    view.said = view.problems.length ? { key: k('还有问题，改好了再保存。'), bad: true } : { text: reply.error, bad: true };
    view.refresh();
    announce(view.problems.length ? t('还有问题，改好了再保存。') : reply.error);
    return;
  }
  announce(creating ? t('已新建 Agent：{name}', { name: reply.data.name }) : t('已保存。'));
  openAgent(reply.data.name, false, creating ? k('已新建。主助手从下一条消息起就能把任务交给它。') : k('已保存。'));
}

// ---- 从 Claude Code 导入 ----

async function startAgentImport() {
  showAgentView({ kind: 'review', loading: true, candidates: [], picks: {}, results: {}, places: [], installing: false, said: null });
  const view = agentView;
  const mine = agentSeq;
  const reply = await call('/api/agent/claude');
  if (mine !== agentSeq || agentView !== view) return;
  view.loading = false;
  if (!reply.ok) { view.said = { text: reply.error, bad: true }; paintAgent('title'); return; }
  view.candidates = Array.isArray(reply.data.candidates) ? reply.data.candidates : [];
  view.places = reply.data.places || [];
  for (const candidate of view.candidates) {
    // 只找到一个、合格、又没有同名的，默认勾上。找到好几个的，一个都不先勾：装哪几个由你来挑
    const only = view.candidates.length === 1 && !candidate.problems.length && !candidate.exists && !candidate.bad_name;
    view.picks[candidate.key] = { on: only, mode: 'rename', name: candidate.exists ? freeAgentName(candidate.name) : candidate.name };
  }
  paintAgent('title');
}

function freeAgentName(name) {
  const taken = agentNames();
  for (let n = 2; n < 100; n += 1) if (!taken.has(name + '-' + n)) return name + '-' + n;
  return name;
}

function buildAgentReview(view) {
  const node = el('div', 'skill-view');
  node.appendChild(agentHead([k('从 Claude Code 导入')], view.places.length ? [k('在这些地方找过：{places}'), { places: view.places.join('  ·  ') }] : null).head);
  if (view.loading) { node.appendChild(skillWaiting(k('正在查找 Claude Code 的 Agent……'))); return node; }
  const trust = node.appendChild(el('p', 'skill-trust calm'));
  trust.append(icon('copy'), tel('span', null, k('导入的是一份复制：之后在 Claude Code 那边改了、删了，都不会影响智狐这边。')));
  if (!view.candidates.length) {
    node.appendChild(tel('p', 'hint', k('没有找到 Claude Code 的 Agent。它们一般放在 ~/.claude/agents/ 或者项目里的 .claude/agents/ 下面，每个是一个 .md 文件。')));
  }
  const cards = node.appendChild(el('ul', 'skill-cands'));
  view.candidates.forEach((candidate, index) => cards.appendChild(agentCandidate(view, candidate, index)));
  const foot = node.appendChild(el('div', 'skill-foot skill-stick'));
  const said = foot.appendChild(skillSaid(el('p'), view.said));
  said.setAttribute('role', 'status');
  foot.appendChild(skillButton('', k('取消'), '', 'cancel', () => leaveAgentView()));
  const chosen = view.candidates.filter((candidate) => view.picks[candidate.key].on && !(view.results[candidate.key] && view.results[candidate.key].ok)).length;
  if (view.candidates.length) {
    const install = foot.appendChild(skillButton('import', view.installing ? k('正在导入……') : chosen > 1 ? k('导入 {n} 个') : k('导入'), 'primary', 'install', () => importAgents(view)));
    if (chosen > 1 && !view.installing) retext(install.lastChild, k('导入 {n} 个'), { n: chosen });
    install.disabled = view.installing || !chosen;
  }
  return node;
}

function agentCandidate(view, candidate, index) {
  const pick = view.picks[candidate.key], result = view.results[candidate.key];
  const bad = candidate.problems.length > 0, done = Boolean(result && result.ok);
  const card = el('li', 'skill-cand agents-cand' + (pick.on && !bad && !done ? ' on' : '') + (done ? ' done' : ''));
  card.dataset.color = candidate.color || '';
  const id = 'agent-pick-' + index;
  const box = card.appendChild(el('input'));
  box.type = 'checkbox';
  box.id = id;
  box.checked = pick.on && !bad;
  box.disabled = bad || done || view.installing;
  box.dataset.key = 'pick:' + candidate.key;
  box.addEventListener('change', () => { pick.on = box.checked; paintAgent(); });
  const body = card.appendChild(el('div', 'skill-cand-body'));
  const label = body.appendChild(el('label', 'skill-line'));
  label.htmlFor = id;
  label.appendChild(agentFace(candidate, 'small'));
  label.appendChild(el('span', 'skill-name', candidate.name));
  if (bad) label.appendChild(el('span', 'skill-flag bad', t('导入不了')));
  else if (candidate.exists && !done) label.appendChild(el('span', 'skill-flag manual', t('同名')));
  if (candidate.description) body.appendChild(el('span', 'skill-desc', candidate.description)).title = candidate.description;
  const facts = [candidate.place + '/' + candidate.file];
  if (!bad) facts.push(agentModelText(candidate.model, true), agentToolsText(candidate.tools));
  body.appendChild(el('span', 'skill-meta', facts.join(' · ')));
  const notes = [...candidate.problems, ...(candidate.warnings || [])];
  if (notes.length) {
    const problems = body.appendChild(el('ul', (bad ? 'skill-problems' : 'agents-warn') + (notes.length === 1 ? ' single' : '')));
    for (const problem of notes) problems.appendChild(el('li', null, problemText(problem)));
  }
  if (!done && !bad && pick.on && (candidate.exists || candidate.bad_name)) {
    // 同名、或者名字在智狐里不合格：问你用什么名字（同名的也可以选替换）
    const clash = body.appendChild(el('div', 'skill-clash'));
    clash.appendChild(el('span', null, candidate.bad_name ? t('这个名字在智狐里不能用，导入时换一个：') : t('已经有一个叫 {name} 的 Agent 了。', { name: candidate.name })));
    const options = clash.appendChild(el('div', 'skill-clash-options'));
    const radio = (mode, text) => {
      const wrap = options.appendChild(el('label'));
      const input = wrap.appendChild(el('input'));
      input.type = 'radio';
      input.name = 'agent-mode-' + index;
      input.checked = pick.mode === mode;
      input.disabled = view.installing;
      input.dataset.key = 'mode:' + mode + ':' + candidate.key;
      input.addEventListener('change', () => { pick.mode = mode; paintAgent(); });
      wrap.appendChild(tel('span', null, text));
      return wrap;
    };
    const rename = radio('rename', k('改名为')).appendChild(el('input', 'field'));
    rename.type = 'text';
    rename.spellcheck = false;
    rename.value = pick.name;
    rename.disabled = pick.mode !== 'rename' || view.installing;
    rename.dataset.key = 'rename:' + candidate.key;
    rename.setAttribute('aria-label', t('{name} 的新名字', { name: candidate.name }));
    rename.addEventListener('input', () => { pick.name = rename.value; });
    if (candidate.exists && !candidate.bad_name) radio('replace', k('替换现有的（原来那个会被删掉）'));
  }
  if (result) body.appendChild(el('span', 'skill-cand-result ' + (result.ok ? 'ok' : 'bad'), result.ok ? t('已导入为 {name}', { name: result.name }) : result.error));
  return card;
}

async function importAgents(view) {
  if (agentView !== view || view.installing) return;
  const picks = view.candidates
    .filter((candidate) => view.picks[candidate.key].on && !candidate.problems.length && !(view.results[candidate.key] && view.results[candidate.key].ok))
    .map((candidate) => {
      const pick = view.picks[candidate.key];
      const replace = Boolean(candidate.exists && !candidate.bad_name && pick.mode === 'replace');
      return { key: candidate.key, name: replace || !(candidate.exists || candidate.bad_name) ? candidate.name : pick.name.trim(), replace };
    });
  if (!picks.length) return;
  view.installing = true;
  view.said = null;
  paintAgent('install');
  const reply = await call('/api/agent/import', { picks });
  if (agentView !== view) return;
  view.installing = false;
  if (!reply.ok) { view.said = { text: reply.error, bad: true }; paintAgent('install'); announce(reply.error); return; }
  const results = Array.isArray(reply.data.results) ? reply.data.results : [];
  for (const result of results) {
    view.results[result.key] = result;
    if (result.ok) view.picks[result.key].on = false;
  }
  const done = results.filter((result) => result.ok), failed = results.length - done.length;
  if (!failed) {
    const said = done.length === 1 ? [k('已导入 {name}。'), { name: done[0].name }] : [k('已导入 {n} 个 Agent。'), { n: done.length }];
    showAgentView({ kind: 'list' }, 'open:' + done[0].name);
    extensionSay(said[0], said[1], 'good');
    announce(t(said[0], said[1]));
    return;
  }
  view.said = { key: k('有 {n} 个没导入，原因写在各自的卡片里。'), values: { n: failed }, bad: true };
  paintAgent('install');
  announce(t(view.said.key, view.said.values));
}

// ---- 挂到扩展对话框上 ----

extensionTabs.agents = {
  open() {
    // 你在访达里改了 Agent 文件夹，程序是不知道的。打开这一页时请它重新看一眼：变了会发来新的状态
    if (token) call('/api/agent/refresh');
    agentOptions = null;  // MCP 服务器可能刚连上：能勾的工具重新要一次
    ensureAgentOptions();
    if (agentView.kind === 'list') { agentsDrawn = ''; drawAgentList(); }
  },
  // 关窗之前问一声（见 closeBlocker）：正在编辑、新建，还有没保存的修改，就先提醒一次、不关
  beforeClose() {
    if (agentMayLeave()) return true;
    requestAnimationFrame(() => focusAgent(agentView.mode === 'raw' ? 'raw' : 'name'));
    return false;
  },
  close() {
    agentSeq += 1;
    agentView = { kind: 'list' };
    agentDeleting = null;
    agentsDrawn = '';
    drawAgentList();
  },
};
window.addEventListener('focus', () => { if (extensionsDialog.open && extensionTab === 'agents' && token && !document.hidden) call('/api/agent/refresh'); });
extensionsDialog.addEventListener('close', () => extensionTabs.agents.close());
