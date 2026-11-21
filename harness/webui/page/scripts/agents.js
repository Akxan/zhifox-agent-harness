// ---- 扩展 · Agent -----------------------------------------------------------
// 自定义 Agent：你定制的、擅长某一类事的助手（见 harness/agents/）。一个 Agent 是 data/agents/ 里的一个 Markdown 文件，
// 同一个文件有两种用法：主助手把任务交给它（子 Agent），或者你用它开始一段新对话（角色，见 agent-picker.js）。
// 这一页管这些文件：列出来、打开看、编辑（表单或者原文）、新建、删除、加内置的示例、从其他工具导入。
//
// 列表跟着状态走（state.agents），打开一个、编辑时要的选项（能勾的工具、示例）另外请求。
// 一个文件合不合格、表单怎么写成文件，都由助手程序来算（/api/agent/parse、/api/agent/compose），页面不另写一份规矩：
// 页面上的检查只是"有没有填"这种马上能看出来的，真正的问题列表都是程序给的。
// 这一页有几种样子（view.kind）：list 列表、detail 打开的一个、edit 编辑、create 新建、examples 示例、review 从其他工具导入。
// 版式和 Skill 一页一样，借用它的几个小工具（skillButton、skillSaid、skillWaiting、problemText，都在 skills.js；shortPath 在 platform.js）。
// 改 Agent 不用等这一轮对话结束（服务器也不拦）：正在进行的对话和子 Agent 用的是开始时读到的那一份。
//
// 这一页分成五个文件，这个是第一个：总说明、共用的状态和小工具。后面依次是 agents-list.js 列表和示例、
// agents-detail.js 打开一个、agents-editor.js 编辑和新建、agents-import.js 从其他工具导入
// （最后在那里挂到扩展对话框上）。

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
