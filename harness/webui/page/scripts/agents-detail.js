// ---- 扩展 · Agent · 打开一个 ------------------------------------------------
// 点开列表里的一个：向助手程序要这个文件读出来的样子（/api/agent/read），画出它的模型、能用的工具、
// 有什么问题和它的设定；底下是"用它开始新对话"、编辑、删除（删除先在原地确认）。

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
  foot.appendChild(skillButton('folder', showLabel(), '', 'reveal', () => revealAgent(view.name, view))).disabled = view.gone;
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
