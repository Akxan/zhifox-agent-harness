// ---- 扩展 · Skill · 从其他工具导入、装之前的确认 -----------------------------
// 网址、上传、从其他工具（Claude Code、Codex、Cursor、Gemini CLI、.agents/skills……）导入找到的 Skill 都先到这一页：
// 一个一张卡片，写着是什么、合不合格；勾上要装的，同名的选改名还是替换，然后一起装上（/api/skill/install）。
// 从其他工具导入时，顶上的总览、每一组的标题和最下面的"没找到的"是三页共用的（imports.js）。
// 文件最后把 Skill 这一页挂到扩展对话框上（打开时、关窗之前、关上时各做什么）。

async function startSkillImport() {
  showSkillView({ kind: 'review', origin: 'tools', loading: true, candidates: [], picks: {}, results: {}, places: [], tools: [], source: {} });
  const view = skillView;
  const mine = skillSeq;
  const reply = await call('/api/skill/scan');
  if (mine !== skillSeq || skillView !== view) return;
  if (!reply.ok) { view.loading = false; view.said = { text: reply.error, bad: true }; paintSkill(); return; }
  showSkillReview(reply.data, 'tools', null, view);
  importArrive('skills');
}

// ---- 装之前的确认 ----

// 同名的已经有了：默认的新名字，后面加 -2、-3……
function freeSkillName(name) {
  const taken = skillNames();
  for (let n = 2; n < 100; n += 1) {
    const suffix = '-' + n, candidate = name.slice(0, MAX_SKILL_NAME - suffix.length).replace(/-+$/, '') + suffix;
    if (!taken.has(candidate)) return candidate;
  }
  return name;
}

function showSkillReview(data, origin, back, reuse) {
  const candidates = Array.isArray(data.candidates) ? data.candidates : [];
  const picks = {};
  for (const candidate of candidates) {
    // 只认出一个、合格、又没有同名的，默认勾上。认出好几个的（一整个仓库、别的工具里的一堆）一个都不先勾：
    // 装哪几个由你来挑。同名的勾上之后，再问你改名还是替换
    const only = candidates.length === 1 && !candidate.problems.length && !candidate.exists;
    picks[candidate.key] = { on: only, mode: 'rename', name: candidate.exists ? freeSkillName(candidate.name) : candidate.name };
  }
  const view = { kind: 'review', origin, back, stage: data.stage, source: data.source || {}, places: data.places || [], tools: data.tools || [], candidates, picks, results: {}, installing: false, loading: false, said: null };
  if (reuse && skillView === reuse) { skillView = view; paintSkill('title'); }
  else showSkillView(view);
}

function buildSkillReview(view) {
  const node = el('div', 'skill-view');
  const source = view.source || {};
  const commit = source.commit ? ' @ ' + source.commit.slice(0, 7) : '';
  const tools = view.origin === 'tools';
  const lead = tools ? [k('只在你打开这一页时去看这些工具的 Skill 文件夹，只读不写、什么都不运行。')]
    : view.origin === 'upload' ? [k('上传的 {place}'), { place: source.label || '' }]
      : [k('来自 {place}'), { place: (source.label || source.url || '') + commit }];
  node.appendChild(skillHead(tools ? [k('从其他工具导入')] : [k('确认安装')], lead).head);
  if (view.loading) { node.appendChild(skillWaiting(k('正在查找别的工具里的 Skill……'))); return node; }
  const overview = tools && importOverview(view.tools, 'skills');
  if (overview) node.appendChild(overview);
  node.appendChild(skillTrust(tools));

  const list = view.candidates;
  if (!list.length) {
    node.appendChild(tel('p', 'hint', tools
      ? k('在别的工具里没有找到 Skill。放在别处的话，可以用“上传”。')
      : k('里面没有找到 SKILL.md：一个 Skill 至少要有这个文件。')));
  }
  const usable = list.filter((candidate) => !candidate.problems.length && !(view.results[candidate.key] && view.results[candidate.key].ok));
  if (usable.length > 3) {
    const all = node.appendChild(el('label', 'skill-all'));
    const box = all.appendChild(el('input'));
    box.type = 'checkbox';
    box.dataset.key = 'all';
    const on = usable.filter((candidate) => view.picks[candidate.key].on).length;
    box.checked = on === usable.length;
    box.indeterminate = on > 0 && on < usable.length;  // 勾了一部分：显示成一条横线，点一下全选
    box.disabled = view.installing;
    all.appendChild(tel('span', null, k('全选')));
    box.addEventListener('change', () => { for (const candidate of usable) view.picks[candidate.key].on = box.checked; paintSkill('all'); });
  }
  let group = null, cards = null;
  list.forEach((candidate, index) => {
    if (!cards || (candidate.group || '') !== group) {
      group = candidate.group || '';
      // 从其他工具导入的：标题是"哪个工具、哪个文件夹"（和 MCP、Agent 两页一样）；压缩包里的：标题是它所在的文件夹
      if (tools && candidate.tool) node.appendChild(importGroupHead(candidate.tool, candidate.tool_id, '', candidate.key.slice(0, candidate.key.lastIndexOf('/'))));
      else if (group) node.appendChild(el('h4', 'skill-group', group));
      cards = node.appendChild(el('ul', 'skill-cands'));
    }
    cards.appendChild(skillCandidate(view, candidate, index));
  });
  const missing = tools && importMissing(view.tools, 'skills', !list.length);
  if (missing) node.appendChild(missing);

  // 底下这一排贴着页面的下边：候选很多、要往下翻的时候，"安装"也一直看得见
  const foot = node.appendChild(el('div', 'skill-foot skill-stick'));
  const said = foot.appendChild(skillSaid(el('p'), view.said));
  said.setAttribute('role', 'status');
  const chosen = list.filter((candidate) => view.picks[candidate.key].on && !(view.results[candidate.key] && view.results[candidate.key].ok)).length;
  foot.appendChild(skillButton('', k('取消'), '', 'cancel', () => leaveSkillView()));
  if (list.length) {
    const label = view.installing ? [k('正在安装……')] : chosen > 1 ? [k('安装 {n} 个'), { n: chosen }] : [k('安装')];
    const install = foot.appendChild(skillButton('import', label[0], 'primary', 'install', () => installSkills(view)));
    if (label[1]) retext(install.lastChild, label[0], label[1]);
    install.disabled = view.installing || !chosen;
  }
  return node;
}

function skillCandidate(view, candidate, index) {
  const pick = view.picks[candidate.key], result = view.results[candidate.key];
  const bad = candidate.problems.length > 0, done = Boolean(result && result.ok);
  const card = el('li', 'skill-cand' + (pick.on && !bad && !done ? ' on' : '') + (done ? ' done' : ''));
  const id = 'skill-pick-' + index;
  const box = card.appendChild(el('input'));
  box.type = 'checkbox';
  box.id = id;
  box.checked = pick.on && !bad;
  box.disabled = bad || done || view.installing;
  box.dataset.key = 'pick:' + candidate.key;
  box.addEventListener('change', () => { pick.on = box.checked; paintSkill(); });

  const body = card.appendChild(el('div', 'skill-cand-body'));
  const label = body.appendChild(el('label', 'skill-line'));
  label.htmlFor = id;
  label.appendChild(el('span', 'skill-name', candidate.name));
  if (bad) label.appendChild(el('span', 'skill-flag bad', t('装不了')));
  else if (candidate.exists && !done) label.appendChild(el('span', 'skill-flag manual', t('同名')));
  if (candidate.original && candidate.original !== candidate.name) {
    body.appendChild(tel('span', 'skill-cand-was', k('SKILL.md 里写的 name 是 {original}，不合规范（只能有小写字母、数字和连字符），装的时候改成 {name}。'), { original: candidate.original, name: candidate.name }));
  }
  if (candidate.description) body.appendChild(el('span', 'skill-desc', candidate.description)).title = candidate.description;
  const files = fileCount(candidate.count || 0);
  const facts = [t(files[0], files[1]), formatSize(candidate.size || 0)];
  if (candidate.license) facts.push(t('许可：{license}', { license: candidate.license }));
  if (candidate.compatibility) facts.push(t('需要：{what}', { what: candidate.compatibility }));
  body.appendChild(el('span', 'skill-meta', facts.join(' · ')));
  if (bad) {
    const problems = body.appendChild(el('ul', 'skill-problems' + (candidate.problems.length === 1 ? ' single' : '')));
    for (const problem of candidate.problems) problems.appendChild(el('li', null, problemText(problem)));
  }
  if (candidate.files && candidate.files.length) {
    const details = body.appendChild(el('details'));
    details.appendChild(tel('summary', null, k('看看有哪些文件')));
    const listing = details.appendChild(el('ul', 'skill-filelist'));
    for (const item of candidate.files) {
      const li = listing.appendChild(el('li'));
      li.append(el('span', null, item.path), el('span', null, formatSize(item.size)));
    }
    if (candidate.count > candidate.files.length) listing.appendChild(el('li', null, t('……还有 {n} 个', { n: candidate.count - candidate.files.length })));
  }
  if (candidate.exists && !done && !bad && pick.on) {
    // 同名：问你改名还是替换。默认改名，名字里加上 -2
    const clash = body.appendChild(el('div', 'skill-clash'));
    clash.appendChild(el('span', null, t('已经有一个叫 {name} 的 Skill 了。', { name: candidate.name })));
    const options = clash.appendChild(el('div', 'skill-clash-options'));
    const radio = (mode, text) => {
      const wrap = options.appendChild(el('label'));
      const input = wrap.appendChild(el('input'));
      input.type = 'radio';
      input.name = 'skill-mode-' + index;
      input.checked = pick.mode === mode;
      input.disabled = view.installing;
      input.dataset.key = 'mode:' + mode + ':' + candidate.key;
      input.addEventListener('change', () => { pick.mode = mode; paintSkill(); });
      wrap.appendChild(tel('span', null, text));
      return wrap;
    };
    radio('rename', k('改名为'));
    const rename = options.lastChild.appendChild(el('input', 'field'));
    rename.type = 'text';
    rename.spellcheck = false;
    rename.value = pick.name;
    rename.disabled = pick.mode !== 'rename' || view.installing;
    rename.dataset.key = 'rename:' + candidate.key;
    rename.setAttribute('aria-label', t('{name} 的新名字', { name: candidate.name }));
    rename.addEventListener('input', () => {
      pick.name = rename.value;
      const typed = rename.value.trim();
      rename.setAttribute('aria-invalid', String(!SKILL_NAME.test(typed) || typed.length > MAX_SKILL_NAME || skillNames().has(typed)));
    });
    radio('replace', k('替换现有的（原来那个会被删掉）'));
  }
  if (result) body.appendChild(el('span', 'skill-cand-result ' + (result.ok ? 'ok' : 'bad'), result.ok ? t('已安装为 {name}', { name: result.name }) : result.error));
  return card;
}

async function installSkills(view) {
  if (skillView !== view || view.installing) return;
  if (busy) { view.said = { key: BUSY_HINT, bad: true }; paintSkill('install'); return; }
  const chosen = view.candidates.filter((candidate) => view.picks[candidate.key].on && !candidate.problems.length && !(view.results[candidate.key] && view.results[candidate.key].ok));
  const taken = skillNames(), names = new Set();
  const picks = [];
  for (const candidate of chosen) {
    const pick = view.picks[candidate.key];
    const replace = Boolean(candidate.exists && pick.mode === 'replace');
    const name = candidate.exists && !replace ? pick.name.trim() : candidate.name;
    // 装之前先在这里查一遍名字：改的名字不合规范、又撞上了别的、或者这一次里有两个同名的，都先说清楚
    if (!SKILL_NAME.test(name) || name.length > MAX_SKILL_NAME) { view.said = { key: k('{name} 不是合格的名字：只能有小写字母、数字和连字符。'), values: { name: name || '""' }, bad: true }; paintSkill('rename:' + candidate.key); return; }
    if ((!replace && taken.has(name)) || names.has(name)) { view.said = { key: k('已经有一个叫 {name} 的 Skill 了。'), values: { name }, bad: true }; paintSkill('rename:' + candidate.key); return; }
    names.add(name);
    picks.push({ key: candidate.key, name, replace });
  }
  if (!picks.length) return;
  view.installing = true;
  view.said = null;
  paintSkill('install');
  const reply = await call('/api/skill/install', { stage: view.stage, picks });
  if (skillView !== view) return;
  view.installing = false;
  if (!reply.ok) { view.said = { text: reply.error, bad: true }; paintSkill('install'); announce(reply.error); return; }
  const results = Array.isArray(reply.data.results) ? reply.data.results : [];
  for (const result of results) {
    view.results[result.key] = result;
    if (result.ok) view.picks[result.key].on = false;
  }
  const done = results.filter((result) => result.ok), failed = results.length - done.length;
  if (!failed) {
    const said = done.length === 1 ? [k('已安装 {name}。从下一条消息起，助手会用到它。'), { name: done[0].name }] : [k('已安装 {n} 个 Skill。从下一条消息起，助手会用到它们。'), { n: done.length }];
    showSkillView({ kind: 'list' }, 'open:' + done[0].name);
    extensionSay(said[0], said[1], 'good');
    announce(t(said[0], said[1]));
    return;
  }
  view.said = { key: k('有 {n} 个没装上，原因写在各自的卡片里。'), values: { n: failed }, bad: true };
  paintSkill('install');
  announce(t(view.said.key, view.said.values));
}

// ---- 挂到扩展对话框上 ----

extensionTabs.skills = {
  open() {
    // 你在访达里改了 Skill 文件夹，程序是不知道的。打开这一页时请它重新看一眼：变了会发来新的状态
    if (token) call('/api/skill/refresh');
    if (skillView.kind === 'list') { skillsDrawn = ''; drawSkillList(); }
  },
  // 关窗之前问一声（见 extensions.js 的 closeBlocker）：正在编辑、新建，还有没保存的修改，就先提醒一次、不关。
  // 这一页不在眼前的话，closeBlocker 会切过来；焦点等切过来之后再放回输入框
  beforeClose() {
    if (skillMayLeave()) return true;
    requestAnimationFrame(() => focusSkill(skillView.kind === 'create' ? 'name' : 'text'));
    return false;
  },
  close() {
    // 关掉对话框：下次打开从列表开始。还没回来的请求，回来了也不要了
    skillSeq += 1;
    skillView = { kind: 'list' };
    skillDeleting = null;
    skillsDrawn = '';
    drawSkillList();
  },
};
window.addEventListener('focus', () => { if (extensionsDialog.open && extensionTab === 'skills' && token && !document.hidden) call('/api/skill/refresh'); });
// 对话框关上了：下次打开从列表开始
extensionsDialog.addEventListener('close', () => extensionTabs.skills.close());
