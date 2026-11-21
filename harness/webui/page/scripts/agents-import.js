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
