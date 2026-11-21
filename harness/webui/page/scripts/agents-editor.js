// ---- 扩展 · Agent · 编辑和新建 ----------------------------------------------
// 两种写法，上面一排切换：表单（名字、说明、模型、颜色、工具、设定），或者原文（整个文件）。
// 切换时请助手程序换算（表单 → 原文用 compose，原文 → 原文用 parse），保存的永远是一份原文：按表单保存的，
// 也是先由 compose 写成原文（只改你改过的那几项，文件里别的东西原样留着），再交给 save。
// 后半是表单里的两个大件：模型的下拉框（fillAgentModels）和工具的清单（agentToolsBox），最后是保存。

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
