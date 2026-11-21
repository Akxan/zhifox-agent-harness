// ---- 新建 ----

function buildSkillCreate(view) {
  const node = el('div', 'skill-view skill-fill skill-create');
  node.appendChild(skillHead([k('新建 Skill')], [k('写下名字、什么时候用和具体做法。保存后，Skill 文件夹里会多一个同名的文件夹，里面是一份 SKILL.md。')]).head);
  const form = node.appendChild(el('div', 'skill-form'));
  const field = (label, help, counter) => {
    const box = form.appendChild(el('label', 'skill-field'));
    const top = box.appendChild(el('span', 'skill-field-head'));
    top.appendChild(tel('span', null, label));
    const count = counter ? top.appendChild(el('span', 'skill-counter')) : null;
    return { box, count, help: help ? tel('span', 'skill-help', help) : null };
  };
  const nameField = field(k('名字'), k('小写字母、数字和连字符，比如 weekly-report。它也是文件夹的名字。'), true);
  const name = nameField.box.appendChild(el('input', 'field mono'));
  name.type = 'text';
  name.maxLength = 80;
  name.spellcheck = false;
  name.autocomplete = 'off';
  name.placeholder = 'weekly-report';
  name.dataset.key = 'name';
  name.value = view.name;
  nameField.box.appendChild(nameField.help);

  const descriptionField = field(k('说明'), k('写清楚它做什么、什么时候该用。助手就是靠这一句决定用不用它的。'), true);
  const description = descriptionField.box.appendChild(el('textarea', 'field'));
  description.rows = 3;
  description.dataset.key = 'description';
  tattr(description, 'placeholder', k('整理每周的工作周报。用户说“写周报”“总结这周做了什么”时使用。'));
  description.value = view.description;
  descriptionField.box.appendChild(descriptionField.help);

  const bodyField = field(k('做法'), k('写给助手看的步骤，用 Markdown。较长的参考资料可以以后放进文件夹里，在这里写明什么时候去读。'));
  bodyField.box.classList.add('grow');
  const steps = bodyField.box.appendChild(el('textarea', 'field skill-code'));
  steps.spellcheck = false;
  steps.dataset.key = 'body';
  tattr(steps, 'placeholder', k('# 周报  1. 先问清楚这周的重点  2. 按“完成、进行中、下周计划”三部分写  3. 每一条写成一句话'));
  steps.value = view.body;
  bodyField.box.appendChild(bodyField.help);

  const problems = node.appendChild(el('ul', 'skill-problems'));
  problems.setAttribute('role', 'alert');
  const foot = node.appendChild(el('div', 'skill-foot'));
  const said = foot.appendChild(el('p'));
  said.setAttribute('role', 'status');
  foot.appendChild(skillButton('', k('取消'), '', 'cancel', () => leaveSkillView()));
  const create = foot.appendChild(skillButton('plus', k('创建'), 'primary', 'create', () => createSkill(view)));

  view.dirty = () => Boolean(name.value.trim() || description.value.trim() || steps.value.trim());
  view.save = () => createSkill(view);
  view.fields = { name, description, steps };
  for (const input of [name, description, steps]) {
    input.addEventListener('input', () => {
      view.name = name.value; view.description = description.value; view.body = steps.value;
      if (view.armed || (view.said && view.said.bad)) { view.armed = false; view.said = null; }
      view.refresh();
    });
  }
  view.refresh = () => {
    const typed = name.value.trim();
    const wrong = typed && (!SKILL_NAME.test(typed) || typed.length > MAX_SKILL_NAME);
    const taken = typed && skillNames().has(typed);
    nameField.count.textContent = typed.length + ' / ' + MAX_SKILL_NAME;
    nameField.count.classList.toggle('over', typed.length > MAX_SKILL_NAME);
    name.setAttribute('aria-invalid', String(Boolean(wrong || taken)));
    nameField.help.classList.toggle('bad', Boolean(wrong || taken));
    retext(nameField.help, taken ? k('已经有一个叫 {name} 的 Skill 了。') : wrong ? k('只能有小写字母、数字和连字符（-），不能以连字符开头或结尾，也不能连着写两个连字符。') : k('小写字母、数字和连字符，比如 weekly-report。它也是文件夹的名字。'), taken ? { name: typed } : undefined);
    const length = description.value.trim().length;
    descriptionField.count.textContent = length.toLocaleString(LOCALES[language]) + ' / ' + MAX_SKILL_DESCRIPTION.toLocaleString(LOCALES[language]);
    descriptionField.count.classList.toggle('over', length > MAX_SKILL_DESCRIPTION);
    problems.hidden = !(view.problems && view.problems.length);
    problems.className = 'skill-problems' + (view.problems && view.problems.length === 1 ? ' single' : '');
    problems.replaceChildren(...(view.problems || []).map((problem) => el('li', null, problemText(problem))));
    skillSaid(said, view.said || { key: SAVE_HINT });
    retext(create.lastChild, view.saving ? k('正在创建……') : k('创建'));
    create.disabled = Boolean(view.saving || !typed || wrong || taken || !length || length > MAX_SKILL_DESCRIPTION);
  };
  return node;
}

async function createSkill(view) {
  if (skillView !== view || view.saving) return;
  const { name, description, steps } = view.fields;
  if (busy) { view.said = { key: BUSY_HINT, bad: true }; view.refresh(); return; }
  if (!name.value.trim()) { name.focus(); return; }
  if (!description.value.trim()) { view.said = { key: k('请写一句说明：它做什么、什么时候用。'), bad: true }; view.refresh(); description.focus(); return; }
  view.saving = true;
  view.problems = [];
  view.refresh();
  const reply = await call('/api/skill/create', { name: name.value.trim(), description: description.value.trim(), body: steps.value });
  if (skillView !== view) return;
  view.saving = false;
  if (!reply.ok) {
    view.problems = reply.data && Array.isArray(reply.data.problems) ? reply.data.problems : [];
    view.said = { text: reply.error, bad: true };
    view.refresh();
    announce(reply.error);
    return;
  }
  announce(t('已新建 Skill：{name}', { name: reply.data.name }));
  openSkill(reply.data.name, false, k('已新建。从下一条消息起，助手会用到它。'));
}

// ---- 从网址添加 ----

function buildSkillUrl(view) {
  const node = el('div', 'skill-view');
  node.appendChild(skillHead([k('从网址添加')], [k('贴上 Skill 的链接。智狐先把它下载下来给你看一眼，你确认了才安装。')]).head);
  const form = node.appendChild(el('form', 'skill-url'));
  form.noValidate = true;
  const input = form.appendChild(el('input', 'field'));
  input.type = 'url';
  input.inputMode = 'url';
  input.spellcheck = false;
  input.autocomplete = 'off';
  input.placeholder = 'https://github.com/…';
  tattr(input, 'aria-label', k('Skill 的网址'));
  input.dataset.key = 'url';
  input.value = view.url;
  const go = form.appendChild(skillButton('import', k('获取'), 'primary', 'go'));
  go.type = 'submit';
  form.addEventListener('submit', (event) => { event.preventDefault(); fetchSkill(view); });
  input.addEventListener('input', () => { view.url = input.value; if (view.said) { view.said = null; view.refresh(); } });
  const said = node.appendChild(el('p'));
  said.setAttribute('role', 'status');

  const examples = node.appendChild(el('div', 'skill-examples'));
  examples.appendChild(tel('span', null, k('试试：')));
  for (const url of SKILL_EXAMPLES) {
    const chip = examples.appendChild(el('button', 'skill-example', url.replace('https://', '')));
    chip.type = 'button';
    chip.dataset.key = 'example:' + url;
    chip.addEventListener('click', () => { input.value = view.url = url; fetchSkill(view); });
  }

  const kinds = node.appendChild(el('ul', 'skill-kinds'));
  for (const [title, sample, note] of [
    [k('GitHub 上的一个文件夹'), 'github.com/anthropics/skills/tree/main/skills/pdf', null],
    [k('整个仓库'), 'github.com/anthropics/skills', k('里面有好几个 Skill 时，由你来挑')],
    [k('一个 SKILL.md'), '…/skills/pdf/SKILL.md', k('装的是它所在的整个文件夹')],
    [k('压缩包'), '….zip  ·  ….skill', null],
  ]) {
    const item = kinds.appendChild(el('li', 'skill-kind'));
    item.appendChild(tel('b', null, title));
    item.appendChild(el('code', null, sample));
    if (note) item.appendChild(tel('span', null, note));
  }
  node.appendChild(skillTrust(false));
  node.appendChild(tel('p', 'hint', k('只支持 https 开头的网址和公开的仓库；一次最多下载 20 MB。')));
  view.refresh = () => {
    skillSaid(said, view.said || (view.loading ? { key: k('正在下载……') } : null));
    retext(go.lastChild, view.loading ? k('正在下载……') : k('获取'));
    go.disabled = Boolean(view.loading);
    form.setAttribute('aria-busy', String(Boolean(view.loading)));
  };
  return node;
}

async function fetchSkill(view) {
  const url = view.url.trim();
  if (view.loading) return;
  if (!url) { view.said = { key: k('请填写一个网址。'), bad: true }; view.refresh(); focusSkill('url'); return; }
  view.loading = true;
  view.said = null;
  view.refresh();
  const mine = ++skillSeq;
  const reply = await call('/api/skill/fetch', { url });
  if (mine !== skillSeq || skillView !== view) return;
  view.loading = false;
  if (!reply.ok) { view.said = { text: reply.error, bad: true }; view.refresh(); focusSkill('url'); announce(reply.error); return; }
  showSkillReview(reply.data, 'url', view);
}

// ---- 上传 ----

function buildSkillUpload(view) {
  const node = el('div', 'skill-view');
  node.appendChild(skillHead([k('上传')], [k('交给智狐一个 Skill 的文件夹、压缩包（.zip 或 .skill），或者一个 SKILL.md。装之前会先给你看一眼。')]).head);
  const drop = node.appendChild(el('div', 'skill-drop'));
  drop.appendChild(icon('upload'));
  drop.appendChild(tel('b', null, k('把文件或文件夹拖到这里')));
  drop.appendChild(tel('p', null, k('.zip、.skill、SKILL.md，或者整个 Skill 文件夹。一次最多 20 MB。')));
  const bar = drop.appendChild(el('div', 'skill-bar'));
  const file = el('input');
  file.type = 'file';
  file.accept = '.zip,.skill,.md,application/zip';
  file.hidden = true;
  const folder = el('input');
  folder.type = 'file';
  folder.webkitdirectory = true;
  folder.multiple = true;
  folder.hidden = true;
  const pickFile = bar.appendChild(skillButton('file', k('选择文件'), 'primary', 'pick-file', () => file.click()));
  const pickFolder = bar.appendChild(skillButton('folder', k('选择文件夹'), '', 'pick-folder', () => folder.click()));
  drop.append(file, folder);
  file.addEventListener('change', () => { const picked = [...file.files].map((one) => ({ file: one, path: one.name })); file.value = ''; uploadSkill(view, picked, false); });
  folder.addEventListener('change', () => { const picked = [...folder.files].map((one) => ({ file: one, path: one.webkitRelativePath || one.name })); folder.value = ''; uploadSkill(view, picked, true); });

  // 拖进来：进进出出子元素时会连着收到好几个 dragenter / dragleave，数着进出的次数，归零才算真的离开
  let depth = 0;
  drop.addEventListener('dragenter', (event) => { event.preventDefault(); depth += 1; drop.classList.add('over'); });
  drop.addEventListener('dragover', (event) => { event.preventDefault(); if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy'; });
  drop.addEventListener('dragleave', () => { depth = Math.max(0, depth - 1); if (!depth) drop.classList.remove('over'); });
  drop.addEventListener('drop', (event) => {
    event.preventDefault();
    event.stopPropagation();  // 不让对话区的"拖进来当附件"接到它
    depth = 0;
    drop.classList.remove('over');
    // 文件夹只能在 drop 这一刻拿到入口（webkitGetAsEntry），之后 DataTransfer 就空了，所以先同步取出来
    const entries = [...(event.dataTransfer.items || [])].map((item) => item.webkitGetAsEntry && item.webkitGetAsEntry()).filter(Boolean);
    const plain = [...(event.dataTransfer.files || [])];
    droppedSkillFiles(entries, plain).then(({ files, folder }) => uploadSkill(view, files, folder))
      .catch(() => { view.said = { key: k('没能读出拖进来的文件。'), bad: true }; view.refresh(); });
  });
  const said = node.appendChild(el('p'));
  said.setAttribute('role', 'status');
  node.appendChild(skillTrust(false));
  view.refresh = () => {
    skillSaid(said, view.said || (view.loading ? { key: k('正在读取……') } : null));
    pickFile.disabled = pickFolder.disabled = Boolean(view.loading);
    drop.setAttribute('aria-busy', String(Boolean(view.loading)));
  };
  return node;
}

// 拖进来的东西里有文件夹的话，一层一层读出里面的文件，记下每个文件相对于拖进来的那一层的路径
async function droppedSkillFiles(entries, plain) {
  if (!entries.length) return { files: plain.map((file) => ({ file, path: file.name })), folder: false };
  const files = [];
  let folder = false;
  const walk = async (entry, prefix) => {
    if (files.length > MAX_SKILL_FILES) throw new Error('too many');
    if (entry.isFile) {
      files.push({ file: await new Promise((done, fail) => entry.file(done, fail)), path: prefix + entry.name });
    } else if (entry.isDirectory) {
      folder = true;
      const reader = entry.createReader();
      for (;;) {  // readEntries 一次只给一批，要一直读到给出空的一批
        const batch = await new Promise((done, fail) => reader.readEntries(done, fail));
        if (!batch.length) break;
        for (const child of batch) await walk(child, prefix + entry.name + '/');
      }
    }
  };
  for (const entry of entries) await walk(entry, '');
  return { files, folder };
}

async function uploadSkill(view, picked, folder) {
  if (skillView !== view || view.loading) return;
  const files = picked.filter((item) => !SKILL_JUNK.test(item.path));
  if (!files.length) return;
  const total = files.reduce((sum, item) => sum + item.file.size, 0);
  let said = null;
  if (total > MAX_SKILL_UPLOAD) said = { key: k('太大了：一次最多上传 20 MB。可以只挑出需要的那个 Skill 的文件夹再上传。'), bad: true };
  else if (files.length > MAX_SKILL_FILES) said = { key: k('文件太多了：一次最多 {n} 个。'), values: { n: MAX_SKILL_FILES }, bad: true };
  else if (!folder && files.length > 1) said = { key: k('一次只能上传一个文件。要装整个文件夹的话，点“选择文件夹”，或者把文件夹拖进来。'), bad: true };
  if (said) { view.said = said; view.refresh(); return; }
  view.loading = true;
  view.said = null;
  view.refresh();
  const mine = ++skillSeq;
  let request;
  try {
    if (folder) {
      const items = [];
      for (const item of files) items.push({ path: item.path, data: await base64Of(item.file) });
      request = { kind: 'folder', label: files[0].path.split('/')[0] || files[0].file.name, files: items };
    } else {
      request = { kind: 'file', label: files[0].file.name, data: await base64Of(files[0].file) };
    }
  } catch (_) {
    if (mine !== skillSeq || skillView !== view) return;
    view.loading = false;
    view.said = { key: k('没能读出拖进来的文件。'), bad: true };
    view.refresh();
    return;
  }
  const reply = await call('/api/skill/upload', request);
  if (mine !== skillSeq || skillView !== view) return;
  view.loading = false;
  if (!reply.ok) { view.said = { text: reply.error, bad: true }; view.refresh(); announce(reply.error); return; }
  showSkillReview(reply.data, 'upload', view);
}
