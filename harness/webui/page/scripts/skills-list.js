// ---- 扩展 · Skill · 列表 ----------------------------------------------------
// Skill 一页的第一层：照着状态画列表（没变就不重画）、上面那一排"添加"的入口、一个都没有时的说明，
// 以及每一行：启用开关，和别处一样的"在访达中显示 / 编辑 / 删除"（删除先在原地确认）。
// 各个子页面共用的"回到上一层"（没保存的先提醒一次），和子页面里的 Esc、⌘Enter，也在这里。

function renderSkills(list) {
  skills = Array.isArray(list) ? list.filter((item) => item && typeof item.name === 'string') : [];
  $('count-skills').textContent = skills.length ? skills.length.toLocaleString(LOCALES[language]) : '';
  if (skillDeleting && !skills.some((item) => item.name === skillDeleting)) skillDeleting = null;
  const view = skillView;
  if (view.kind === 'list') { drawSkillList(); return; }
  const item = skills.find((one) => one.name === view.name);
  if (view.kind === 'detail' && view.data && !view.loading) {
    if (!item) {
      if (!view.gone) { view.gone = true; announce(t('这个 Skill 已经不在了。')); paintSkill(); }
      return;
    }
    if (view.gone || item.modified !== view.data.modified || item.files !== view.data.files || item.size !== view.data.size) {
      view.gone = false;
      view.said = { key: k('这个 Skill 刚刚在别处改过了，下面是最新的内容。') };
      loadSkill(view);  // 你在访达里改了它：重新读一遍
      return;
    }
    if (item.enabled !== view.data.enabled || JSON.stringify(item.problems) !== JSON.stringify(view.data.problems)) {
      Object.assign(view.data, { enabled: item.enabled, problems: item.problems });
      paintSkill();
      return;
    }
  }
  if (view.kind === 'edit' && view.data && !view.saving && view.refresh) {
    if (!item && !view.gone) {
      view.gone = true;
      view.said = { key: k('这个 Skill 已经不在了：可能在别处被删掉或改了名字。你的修改还在框里，可以复制出来。'), bad: true };
      announce(problemText(view.said));
      view.refresh();
    } else if (item && !view.data.missing && item.modified !== view.data.modified && !view.stale) {
      // 不重新读：重新读会让"打开时的修改时间"跟着变新，保存时就发现不了冲突，别处刚改的东西会被一声不响地冲掉
      view.stale = true;
      view.said = { key: k('SKILL.md 刚刚在别处被改过了。你的修改还在；保存时会先问你要不要覆盖。') };
      announce(problemText(view.said));
      view.refresh();
    }
  }
  // 换了语言：标签上的字跟着 data-i18n 已经换了。没有输入框的子页面整个重画一遍（换掉带名字的提示）；
  // 有输入框的只刷新提示和问题列表，你打的字不动
  if (view.language !== language && view.node) {
    view.language = language;
    if (view.refresh) view.refresh(); else paintSkill();
  }
}

function drawSkillList() {
  if (skillView.kind !== 'list') return;
  const print = JSON.stringify([skills, skillDeleting, language]);
  if (print === skillsDrawn && skillPanel.firstElementChild) {
    if (skillFocus) { focusSkill(skillFocus); skillFocus = null; }
    return;
  }
  skillsDrawn = print;
  const keep = skillFocus || focusedKey();
  skillFocus = null;
  const view = el('div', 'skill-view skill-list-view');
  if (skills === null) view.appendChild(skillWaiting(k('正在读取……')));
  else if (!skills.length) view.appendChild(skillEmpty());
  else {
    // 新出现的（刚装上的、刚在访达里放进来的）浮现一下。只看此刻发生的，刷新页面重放时不播
    const fresh = knownSkills && isLive() ? skills.filter((skill) => !knownSkills.has(skill.name)).map((skill) => skill.name) : [];
    view.appendChild(skillToolbar());
    const list = view.appendChild(el('ul', 'skill-list'));
    list.setAttribute('aria-label', t('装好的 Skill'));
    list.append(...skills.map((skill) => skillRow(skill, fresh.includes(skill.name))));
  }
  if (skills) knownSkills = skillNames();
  skillPanel.replaceChildren(view);
  if (keep) focusSkill(keep);
}

function skillToolbar() {
  const bar = el('div', 'skill-bar');
  const on = skills.filter((skill) => skill.enabled).length;
  bar.appendChild(tel('p', 'skill-count', skills.length === 1 ? k('1 个 Skill，{on} 个启用') : k('{n} 个 Skill，{on} 个启用'),
    { n: skills.length.toLocaleString(LOCALES[language]), on: on.toLocaleString(LOCALES[language]) }));
  bar.append(...skillAdders());
  const folder = iconButton('folder', byServer(k('在访达中打开 Skill 文件夹'), k('在文件资源管理器中打开 Skill 文件夹'), k('在文件管理器中打开 Skill 文件夹')));
  folder.classList.remove('small');
  folder.dataset.key = 'bar:folder';
  folder.addEventListener('click', () => revealSkill(null));
  bar.appendChild(folder);
  return bar;
}

// 四种"添加"的入口：列表上面一排，空的时候在说明下面
function skillAdders() {
  return [
    skillButton('plus', k('新建'), 'primary', 'bar:new', () => showSkillView({ kind: 'create', name: '', description: '', body: '' })),
    skillButton('link', k('从网址添加'), '', 'bar:url', () => showSkillView({ kind: 'url', url: '' })),
    skillButton('upload', k('上传'), '', 'bar:upload', () => showSkillView({ kind: 'upload' })),
    skillButton('import', k('从其他工具导入'), '', 'bar:import', startSkillImport),
  ];
}

// 一个都还没有：讲清楚 Skill 是什么、长什么样，再给几个入口
function skillEmpty() {
  const box = el('div', 'skill-empty');
  const words = box.appendChild(el('div'));
  words.appendChild(tel('h3', null, k('教助手按你的方法做事')));
  words.appendChild(tel('p', null, k('Skill 是一份说明书：一个文件夹，里面的 SKILL.md 写着做某一类事的步骤，还可以带上脚本、参考资料和模板。')));
  words.appendChild(tel('p', null, k('平时助手只看得到每个 Skill 的名字和一句说明；遇到对得上的事，才把完整的说明读进来照着做。所以装多少个都不会让对话变贵。')));
  const bar = words.appendChild(el('div', 'skill-bar'));
  bar.append(...skillAdders());
  const spec = words.appendChild(el('p', 'hint skill-spec'));
  spec.appendChild(tel('span', null, k('格式是开放的 Agent Skills 标准：在 Claude Code、Codex、Cursor、Gemini CLI、GitHub Copilot 这些工具里用的 Skill 可以直接装进来。')));
  spec.appendChild(document.createTextNode(' '));
  const link = spec.appendChild(tel('a', null, k('看看规范')));
  link.href = SKILL_SPEC;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';

  // 一个 Skill 文件夹的样子
  const tree = box.appendChild(el('pre', 'skill-tree'));
  const line = (...parts) => { for (const part of parts) tree.appendChild(part); tree.appendChild(document.createTextNode('\n')); };
  const text = (value) => document.createTextNode(value);
  line(el('b', null, 'weekly-report/'));
  line(text('├─ '), el('span', 'skill-key', 'SKILL.md'), text('      '), tel('i', null, k('说明，必须有')));
  line(text('├─ scripts/      '), tel('i', null, k('脚本')));
  line(text('└─ references/   '), tel('i', null, k('参考资料')));
  line(text(''));
  line(el('i', null, 'SKILL.md'));
  line(text('---'));
  line(el('span', 'skill-key', 'name'), text(': weekly-report'));
  line(el('span', 'skill-key', 'description'), text(': '), tel('i', null, k('做什么、什么时候用')));
  tree.appendChild(text('---'));
  return box;
}

function skillRow(skill, fresh) {
  const bad = skill.problems.length > 0, name = skill.name;
  const row = el('li', 'skill-row' + (skill.enabled ? '' : ' off') + (bad ? ' bad' : '') + (fresh ? ' enter' : ''));
  row.dataset.name = name;

  // 删除前先在原地确认一下，和删除会话、记忆一样，不弹对话框
  if (skillDeleting === name) {
    const box = row.appendChild(el('div', 'skill-confirm'));
    box.appendChild(el('span', null, t('删除 {name}？它的整个文件夹都会删掉，不能撤销。', { name })));
    const yes = box.appendChild(el('button', 'mini danger', t('删除')));
    const no = box.appendChild(el('button', 'mini cancel', t('取消')));
    yes.type = no.type = 'button';
    yes.dataset.key = 'yes:' + name;
    no.dataset.key = 'cancel:' + name;
    const cancel = () => { skillDeleting = null; skillFocus = 'trash:' + name; drawSkillList(); };
    yes.addEventListener('click', async () => {
      if (busy) { extensionSay(BUSY_HINT, null, 'bad'); return; }
      yes.disabled = no.disabled = true;
      const index = skills.findIndex((one) => one.name === name);
      const rest = skills.filter((one) => one.name !== name);
      const after = rest.length ? rest[Math.min(index, rest.length - 1)].name : null;
      // 这一行先淡出去，同时请助手程序删掉。删不掉的话（比如助手正在工作）它再回来
      const [reply] = await Promise.all([call('/api/skill/delete', { name }), leave(row)]);
      skillDeleting = null;
      if (!reply.ok) { extensionSay(null, null, 'bad', reply.error); skillsDrawn = ''; skillFocus = 'trash:' + name; drawSkillList(); return; }
      skills = rest;
      skillsDrawn = '';
      skillFocus = after ? 'open:' + after : 'bar:new';
      drawSkillList();
      extensionSay(k('已删除 Skill：{name}'), { name }, 'good');
      announce(t('已删除 Skill：{name}', { name }));
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
  open.title = skill.description || '';
  const glyph = open.appendChild(el('span', 'skill-glyph'));
  glyph.appendChild(icon(bad ? 'alert' : 'skill'));
  const words = open.appendChild(el('span', 'skill-words'));
  const line = words.appendChild(el('span', 'skill-line'));
  line.appendChild(el('span', 'skill-name', name));
  if (!skill.enabled) line.appendChild(el('span', 'skill-flag', t('停用中')));
  if (bad) line.appendChild(el('span', 'skill-flag bad', t('需要修改')));
  else if (skill.manual) line.appendChild(el('span', 'skill-flag manual', t('点名时才用')));
  const showProblem = bad && !skill.description;
  words.appendChild(el('span', 'skill-desc' + (showProblem ? ' bad' : ''), showProblem ? problemText(skill.problems[0]) : skill.description));
  const source = skillSource(skill.source);
  const files = fileCount(skill.files || 0);
  words.appendChild(el('span', 'skill-meta', [t(files[0], files[1]), formatSize(skill.size || 0), source && t(source[0], source[1])].filter(Boolean).join(' · ')));
  open.addEventListener('click', () => openSkill(name));

  // 启用开关一直看得见。开着的会告诉模型，关着的模型看不到、也读不了
  row.appendChild(skillSwitch(skill, 'switch:' + name));

  // 和会话行、记忆行一样的三个按钮：在访达中显示、编辑、删除。编辑和删除在助手工作时藏起来（服务器这时也不让改）。
  // 每个按钮的名字里带上是哪一个：读屏软件逐个读按钮时，要知道它操作的是哪个 Skill
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
  action('folder', showLabel(), showNamedLabel(), '', 'folder').addEventListener('click', () => revealSkill(name));
  action('edit', k('编辑'), k('编辑 Skill：{name}'), 'idle-only', 'edit').addEventListener('click', () => openSkill(name, true));
  action('trash', k('删除'), k('删除 Skill：{name}'), 'idle-only danger-hover', 'trash').addEventListener('click', () => {
    skillDeleting = name;
    skillFocus = 'cancel:' + name;  // 默认停在"取消"上，免得手快连按两下回车就删掉了
    drawSkillList();
  });
  return row;
}

// 启用开关（role=switch：读屏软件会读出"开"或"关"）
function skillSwitch(skill, key) {
  const toggle = el('button', 'skill-switch');
  toggle.type = 'button';
  toggle.setAttribute('role', 'switch');
  toggle.setAttribute('aria-checked', String(Boolean(skill.enabled)));
  toggle.setAttribute('aria-label', t('启用 {name}', { name: skill.name }));
  toggle.title = skill.enabled ? t('启用着：助手会用到它。点一下停用') : t('停用了：助手看不到它。点一下启用');
  toggle.dataset.key = key;
  toggle.addEventListener('click', () => toggleSkill(skill.name, toggle.getAttribute('aria-checked') !== 'true', toggle));
  return toggle;
}

async function toggleSkill(name, enabled, toggle) {
  if (busy) { extensionSay(BUSY_HINT, null, 'bad'); return; }
  toggle.setAttribute('aria-checked', String(enabled));  // 先拨过去，服务器不同意再拨回来
  const reply = await call('/api/skill/enable', { name, enabled });
  if (!reply.ok) {
    toggle.setAttribute('aria-checked', String(!enabled));
    extensionSay(null, null, 'bad', reply.error);
    return;
  }
  const said = enabled ? k('已启用 {name}。从下一条消息起，助手会用到它。') : k('已停用 {name}。从下一条消息起，助手不会再用它。');
  extensionSay(said, { name }, 'good');
  announce(t(said, { name }));
}

// 在文件管理器里显示一个 Skill 的文件夹（name 为 null 时打开整个 Skill 文件夹）。页面只给名字，路径由助手程序算
async function revealSkill(name, view) {
  const reply = await call('/api/skill/reveal', name ? { name } : {});
  const said = reply.ok ? { ...revealedSaid(reply.data.path, !name), good: true } : { text: reply.error, bad: true };
  if (view && skillView === view) { view.said = said; paintSkill(); }
  else extensionSay(said.key || null, said.values, said.bad ? 'bad' : 'good', said.text);
}

// 离开子页面，回到上一层。正在编辑、还没保存的，先提醒一次
function leaveSkillView() {
  const view = skillView;
  if (!skillMayLeave()) return;
  if (view.kind === 'edit') { openSkill(view.name); return; }
  if (view.kind === 'review' && view.back) { showSkillView(view.back); return; }
  const from = { detail: 'open:' + view.name, create: 'bar:new', url: 'bar:url', upload: 'bar:upload', review: 'bar:import' }[view.kind];
  showSkillView({ kind: 'list' }, from);
}

// 编辑、新建里有没保存的东西：第一次只提醒，紧接着再来一次才真的放弃
function skillMayLeave() {
  const view = skillView;
  if (!['edit', 'create'].includes(view.kind) || !view.dirty || !view.dirty() || view.armed) return true;
  view.armed = true;
  view.said = { key: k('还有没保存的修改，再按一次就放弃。'), bad: true };
  paintSkill();
  return false;
}

// 在子页面里按 Esc：回到上一层（不关对话框）。编辑框里按 ⌘Enter / Ctrl+Enter 保存
skillPanel.addEventListener('keydown', (key) => {
  if (key.isComposing || key.keyCode === 229) return;  // 输入法正在组字时，Esc 和回车是给输入法的
  if (key.key === 'Escape' && skillView.kind !== 'list' && !key.defaultPrevented) {
    key.preventDefault();
    key.stopPropagation();
    leaveSkillView();
  } else if (key.key === 'Enter' && commandHeld(key) && skillView.save) {
    key.preventDefault();
    skillView.save();
  }
});
