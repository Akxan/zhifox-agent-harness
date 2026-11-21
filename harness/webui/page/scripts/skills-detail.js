// ---- 扩展 · Skill · 打开一个、编辑 SKILL.md ---------------------------------
// 点开列表里的一个：向助手程序要它的文件清单（/api/skill/read），一个文件一个文件地看（/api/skill/file），
// 也能在这里删掉整个 Skill。后面一小节是编辑它的 SKILL.md：原地换成编辑框，有没有问题由助手程序检查，
// 别处同时改过的话先说一声，再点一次才覆盖。

function openSkill(name, edit, said) {
  showSkillView({ kind: 'detail', name, data: null, loading: true, error: '', file: 'SKILL.md', fileData: null, confirm: false, said: said ? { key: said, good: true } : null, edit: Boolean(edit) });
  loadSkill(skillView);
}

async function loadSkill(view) {
  const mine = ++skillSeq;
  view.loading = true;
  const reply = await call('/api/skill/read', { name: view.name });
  if (mine !== skillSeq || skillView !== view) return;
  view.loading = false;
  if (!reply.ok) { view.error = reply.error; view.data = null; paintSkill(); return; }
  view.error = '';
  view.data = reply.data;
  if (view.edit) { view.edit = false; startSkillEdit(view); return; }
  if (view.file !== 'SKILL.md' && !reply.data.list.some((item) => item.path === view.file)) view.file = 'SKILL.md';
  paintSkill();
  if (view.file !== 'SKILL.md') loadSkillFile(view, view.file);
}

async function loadSkillFile(view, path) {
  view.file = path;
  view.fileData = path === 'SKILL.md' ? null : { loading: true };
  paintSkill();
  if (path === 'SKILL.md') return;
  const mine = ++skillSeq;
  const reply = await call('/api/skill/file', { name: view.name, path });
  if (mine !== skillSeq || skillView !== view || view.file !== path) return;
  view.fileData = reply.ok ? reply.data : { error: reply.error };
  paintSkill();
}

function buildSkillDetail(view) {
  const node = el('div', 'skill-view skill-fill');
  const data = view.data;
  const { head, words } = skillHead(view.name, data ? data.description : '');
  node.appendChild(head);
  if (!data) {
    if (view.error) node.appendChild(el('p', 'skill-problems single', view.error));
    else node.appendChild(skillWaiting(k('正在读取……')));
    return node;
  }
  head.appendChild(skillSwitch(data, 'switch'));
  const tags = words.appendChild(el('div', 'skill-tags'));
  const tag = (text, extra) => tags.appendChild(el('span', 'tag' + (extra ? ' ' + extra : ''), text));
  const source = skillSource(data.source);
  if (source) tag(t(source[0], source[1]));
  if (data.license) tag(t('许可：{license}', { license: data.license }));
  if (data.compatibility) tag(t('需要：{what}', { what: data.compatibility }));
  if (data.manual) tag(t('点名时才用：不会主动列给助手'));
  for (const [key, value] of Object.entries(data.metadata || {}).slice(0, 6)) tag(key + ': ' + value);
  if (data.allowed_tools && data.allowed_tools.length) {
    // Claude Code 里这一项的意思是"用这些工具不用再问"。智狐不这样做：Skill 想运行的命令照样要你确认
    tag(t('allowed-tools：{tools}（智狐照样会问你）', { tools: data.allowed_tools.join(' ') }));
  }
  if (data.extra && data.extra.length) tag(t('智狐不用的字段：{fields}', { fields: data.extra.join(', ') }));

  if (view.gone || data.problems.length) {
    const problems = node.appendChild(el('ul', 'skill-problems' + (view.gone || data.problems.length === 1 ? ' single' : '')));
    problems.setAttribute('role', 'alert');
    if (view.gone) problems.appendChild(tel('li', null, k('这个 Skill 已经不在了：可能在别处被删掉或改了名字。')));
    else for (const problem of data.problems) problems.appendChild(el('li', null, problemText(problem)));
    if (!view.gone) problems.appendChild(tel('li', null, k('有问题的 Skill 不会告诉助手。点“编辑”改好 SKILL.md 就行。')));
  }

  // 文件列表和选中的文件。SKILL.md 排第一，就是打开时看到的那一个
  const browser = node.appendChild(el('div', 'skill-browser'));
  const files = browser.appendChild(el('ul', 'skill-files'));
  files.setAttribute('aria-label', t('文件'));
  for (const item of data.list.length ? data.list : [{ path: 'SKILL.md', size: 0 }]) {
    const li = files.appendChild(el('li'));
    const button = li.appendChild(el('button', 'skill-file'));
    button.type = 'button';
    button.dataset.key = 'file:' + item.path;
    button.setAttribute('aria-current', String(item.path === view.file));
    button.append(el('span', null, item.path), el('span', 'size', formatSize(item.size)));
    button.addEventListener('click', () => { if (view.file !== item.path) loadSkillFile(view, item.path); });
  }
  const content = browser.appendChild(el('pre', 'skill-content'));
  content.tabIndex = 0;  // 能用键盘滚动
  content.setAttribute('aria-label', t('{path} 的内容', { path: view.file }));
  const file = view.fileData;
  if (view.file === 'SKILL.md') {
    if (data.missing) { content.classList.add('waiting'); retext(content, k('这个文件夹里还没有 SKILL.md。点“编辑”写一份。')); }
    else if (!data.editable && !data.text) { content.classList.add('waiting'); retext(content, k('SKILL.md 读不出来（太大，或者不是 UTF-8 编码的文字）。可以在文件夹里用别的软件打开。')); }
    else content.textContent = data.text;
  } else if (!file || file.loading) { content.classList.add('waiting'); retext(content, k('正在读取……')); }
  else if (file.error) { content.classList.add('waiting'); content.textContent = file.error; }
  else if (file.binary) { content.classList.add('waiting'); retext(content, k('这不是文字文件（{size}），没法在这里显示。可以在文件夹里打开它。'), { size: formatSize(file.size) }); }
  else content.textContent = file.text + (file.truncated ? '\n\n' + t('……文件比较长，这里只显示了开头。') : '');

  const foot = node.appendChild(el('div', 'skill-foot'));
  const said = foot.appendChild(skillSaid(el('p'), view.confirm ? { key: k('删除之后，这个 Skill 的整个文件夹都没了，不能撤销。'), bad: true } : view.said));
  said.setAttribute('role', 'status');
  if (view.confirm) {
    foot.appendChild(skillButton('', k('删除'), 'danger', 'confirm-delete', () => deleteViewedSkill(view)));
    foot.appendChild(skillButton('', k('取消'), '', 'confirm-cancel', () => { view.confirm = false; paintSkill('delete'); }));
  } else {
    const edit = foot.appendChild(skillButton('edit', k('编辑'), 'idle-only', 'edit', () => startSkillEdit(view)));
    edit.disabled = view.gone || (!data.editable && !data.missing);
    foot.appendChild(skillButton('folder', SHOW_FILE, '', 'reveal', () => revealSkill(view.name, view))).disabled = view.gone;
    const remove = foot.appendChild(skillButton('trash', k('删除'), 'idle-only', 'delete', () => {
      if (busy) { view.said = { key: BUSY_HINT, bad: true }; paintSkill(); return; }
      view.confirm = true;
      paintSkill('confirm-cancel');  // 默认停在"取消"上
    }));
    remove.disabled = view.gone;
  }
  return node;
}

async function deleteViewedSkill(view) {
  if (busy) { view.confirm = false; view.said = { key: BUSY_HINT, bad: true }; paintSkill('delete'); return; }
  for (const button of view.node.querySelectorAll('.skill-foot button')) button.disabled = true;
  const reply = await call('/api/skill/delete', { name: view.name });
  if (skillView !== view) return;
  if (!reply.ok) { view.confirm = false; view.said = { text: reply.error, bad: true }; paintSkill('delete'); return; }
  const index = (skills || []).findIndex((one) => one.name === view.name);
  skills = (skills || []).filter((one) => one.name !== view.name);
  const after = skills.length ? skills[Math.min(Math.max(index, 0), skills.length - 1)].name : null;
  showSkillView({ kind: 'list' }, after ? 'open:' + after : 'bar:new');
  extensionSay(k('已删除 Skill：{name}'), { name: view.name }, 'good');
  announce(t('已删除 Skill：{name}', { name: view.name }));
}

// ---- 编辑 SKILL.md ----

function startSkillEdit(view) {
  const data = view.data;
  if (!data) return;
  if (busy) { view.said = { key: BUSY_HINT, bad: true }; paintSkill(); return; }
  if (!data.editable) { view.said = { key: k('SKILL.md 读不出来（太大，或者不是 UTF-8 编码的文字），在这里改会把它弄坏。请在文件夹里用别的软件编辑。'), bad: true }; paintSkill(); return; }
  // 还没有 SKILL.md 的：先填一个架子
  const base = data.missing ? '---\nname: ' + view.name + '\ndescription: \n---\n\n' : data.text;
  showSkillView({ kind: 'edit', name: view.name, data, base, saving: false, force: false, armed: false, stale: false, gone: false, problems: [], said: null });
}

function buildSkillEditor(view) {
  const node = el('div', 'skill-view skill-fill');
  node.appendChild(skillHead(view.name, [k('改的是 SKILL.md 本身。开头两行 --- 之间是 frontmatter：name 和 description 必须有；改了 name，文件夹也会跟着改名。')]).head);
  const field = node.appendChild(el('label', 'skill-field grow'));
  const top = field.appendChild(el('span', 'skill-field-head'));
  top.appendChild(el('span', null, 'SKILL.md'));
  top.appendChild(tel('span', 'skill-help', SAVE_HINT));
  const area = field.appendChild(el('textarea', 'field skill-code'));
  area.spellcheck = false;
  area.value = view.base;
  area.dataset.key = 'text';
  const problems = node.appendChild(el('ul', 'skill-problems'));
  problems.setAttribute('role', 'alert');
  const foot = node.appendChild(el('div', 'skill-foot'));
  const said = foot.appendChild(el('p'));
  said.setAttribute('role', 'status');
  foot.appendChild(skillButton('', k('取消'), '', 'cancel', () => leaveSkillView()));
  const save = foot.appendChild(skillButton('', k('保存'), 'primary', 'save', () => saveSkillEdit(view)));
  view.dirty = () => area.value !== view.base;
  view.save = () => saveSkillEdit(view);
  view.area = area;
  area.addEventListener('input', () => {
    // 提醒过"再按一次就放弃"之后又接着改了，就当没提醒过；上一次保存的结果也该收起来了
    if (!view.armed && !view.said && !view.problems.length) return;
    view.armed = false;
    if (!view.stale && !view.force && !view.gone) view.said = null;
    view.refresh();
  });
  view.refresh = () => {
    problems.hidden = !view.problems.length;
    problems.className = 'skill-problems' + (view.problems.length === 1 ? ' single' : '');
    problems.replaceChildren(...view.problems.map((problem) => el('li', null, problemText(problem))));
    skillSaid(said, view.said);
    retext(save.lastChild, view.saving ? k('正在保存……') : view.force ? k('覆盖保存') : k('保存'));
    save.disabled = view.saving || view.gone;
  };
  // 光标放在开头，从第一行看起
  requestAnimationFrame(() => { if (document.activeElement === area) { area.setSelectionRange(0, 0); area.scrollTop = 0; } });
  return node;
}

async function saveSkillEdit(view) {
  if (skillView !== view || view.saving) return;
  if (busy) { view.said = { key: BUSY_HINT, bad: true }; view.refresh(); return; }
  view.saving = true;
  view.armed = false;
  view.refresh();
  const modified = view.data.missing ? null : view.data.modified;
  const reply = await call('/api/skill/save', { name: view.name, text: view.area.value, modified, force: view.force });
  if (skillView !== view) return;
  view.saving = false;
  if (!reply.ok) {
    if (reply.data && reply.data.conflict) view.force = true;  // 再点一次就覆盖
    view.problems = reply.data && Array.isArray(reply.data.problems) ? reply.data.problems : [];
    view.said = view.problems.length ? { key: k('SKILL.md 还有问题，改好了再保存。'), bad: true } : { text: reply.error, bad: true };
    view.refresh();
    announce(view.problems.length ? t('SKILL.md 还有问题，改好了再保存。') : reply.error);
    return;
  }
  announce(t('已保存。'));
  openSkill(reply.data.name, false, k('已保存。'));
}
