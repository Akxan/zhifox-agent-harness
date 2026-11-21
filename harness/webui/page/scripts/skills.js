  // ---- 扩展 · Skill ----------------------------------------------------------
  // Skill 是教助手做某一类事的"说明书"：一个文件夹，里面的 SKILL.md 写着步骤，还可以带着脚本和参考资料
  // （格式是开放的 Agent Skills 标准，见 harness/skills/）。这一页管 data/skills/ 里的 Skill：
  // 列出来，打开看、编辑、启用停用、删除；也可以新建、上传、从网址添加、从 Claude Code 导入。
  //
  // 列表跟着状态走：服务器每次发状态都重新看一遍 Skill 文件夹，页面不用自己去问。打开一个、下载预览这些另外请求。
  // 这一页有几种样子（view.kind）：
  //   list 列表   detail 打开的一个   edit 编辑 SKILL.md   create 新建   url 从网址添加   upload 上传   review 装之前的确认
  // 换样子不另外弹对话框（对话框里再叠一层，焦点和 Esc 都容易乱），而是在这一页里原地换内容，左上角有"返回"。
  // 列表和打开的一个照着数据整个重画；编辑、新建、网址这几种有你正在打的字，画好之后只更新提示，不重画输入框。

  const skillPanel = $('panel-skills');
  const SKILL_NAME = /^[a-z0-9]+(-[a-z0-9]+)*$/;  // 和 harness/skills/spec.py 的 NAME 一样
  const MAX_SKILL_NAME = 64, MAX_SKILL_DESCRIPTION = 1024;
  const MAX_SKILL_UPLOAD = 20e6;  // 和服务器那边一样：一次最多上传约 20 MB
  const MAX_SKILL_FILES = 1000;
  const SKILL_SPEC = 'https://agentskills.io/specification';
  const SKILL_EXAMPLES = ['https://github.com/anthropics/skills', 'https://github.com/anthropics/skills/tree/main/skills/pdf'];
  const SKILL_JUNK = /(^|\/)(__MACOSX|\.git)(\/|$)|(^|\/)(\.DS_Store|Thumbs\.db|desktop\.ini)$/;
  let skills = null;          // 最新的列表（来自状态）：[{name, description, enabled, problems, files, size, modified, source, manual}]
  let knownSkills = null;     // 上一次画列表时有哪些。新装上的那几个浮现出来
  let skillsDrawn = '';
  let skillView = { kind: 'list' };
  let skillDeleting = null;   // 列表里正在原地确认删除的那一个
  let skillFocus = null;      // 下一次画完之后焦点放到哪儿（data-key）
  let skillSeq = 0;           // 每发一次请求加一。回来得晚的旧结果靠它认出来、丢掉

  // 服务器给的问题是 {key, values}：原文和填进去的值，换语言时照着它们重新翻译
  const problemText = (problem) => (problem && typeof problem === 'object' ? t(problem.key, problem.values) : String(problem || ''));
  const shortPath = (path) => { const parts = String(path || '').split('/'); return (parts.length > 3 ? '…/' : '') + parts.slice(-3).join('/'); };
  const skillNames = () => new Set((skills || []).map((skill) => skill.name));

  // 一个 Skill 是从哪儿来的。返回 [原文, 值]，没有的话返回 null
  function skillSource(source) {
    if (!source || typeof source !== 'object') return null;
    const place = String(source.label || source.url || '');
    if (source.kind === 'new') return [k('在智狐里新建')];
    if (source.kind === 'claude-code') return [k('从 Claude Code 导入：{place}'), { place }];
    if (source.kind === 'upload') return place ? [k('上传的 {place}'), { place }] : [k('上传的文件')];
    if (source.kind === 'url') return [k('来自 {place}'), { place }];
    return null;
  }
  const fileCount = (n) => (n === 1 ? [k('1 个文件')] : [k('{n} 个文件'), { n: n.toLocaleString(LOCALES[language]) }]);

  // 一个带图标和文字的按钮。key 写在 data-key 上：重画之后靠它把焦点放回同一个按钮
  function skillButton(iconName, label, kind, key, onClick) {
    const button = el('button', 'button' + (kind ? ' ' + kind : ''));
    button.type = 'button';
    if (key) button.dataset.key = key;
    if (iconName) button.appendChild(icon(iconName));
    button.appendChild(tel('span', null, label));
    if (onClick) button.addEventListener('click', onClick);
    return button;
  }

  // 一行提示：key 是原文（换语言时跟着换），text 是服务器给的、已经译好的话
  function skillSaid(node, said) {
    node.className = 'skill-said' + (said && said.bad ? ' bad' : said && said.good ? ' good' : '');
    if (said && said.key) retext(node, said.key, said.values);
    else { delete node.dataset.i18n; delete node.dataset.i18nValues; node.textContent = said ? said.text || '' : ''; }
    return node;
  }

  // 焦点：重画之前记下它在哪个 data-key 上，画完放回去
  const focusedKey = () => (skillPanel.contains(document.activeElement) && document.activeElement.dataset.key) || null;
  function focusSkill(key, fallback) {
    if (!extensionsDialog.open || skillPanel.hidden) return;
    const find = (name) => name && [...skillPanel.querySelectorAll('[data-key]')].find((node) => node.dataset.key === name);
    const target = find(key) || find(fallback) || skillPanel.querySelector('.skill-open') || find('bar:new');
    if (target) target.focus();
  }

  // 换到另一种样子。focus 是画完之后焦点要去的地方（data-key）
  function showSkillView(view, focus) {
    skillSeq += 1;  // 之前那种样子还没回来的请求，回来了也不要了
    skillView = view;
    skillDeleting = null;
    if (view.kind === 'list') { skillsDrawn = ''; skillFocus = focus || null; drawSkillList(); }
    else paintSkill(focus || (view.kind === 'create' ? 'name' : view.kind === 'url' ? 'url' : view.kind === 'edit' ? 'text' : 'title'));
    const node = skillPanel.firstElementChild;
    if (node) play(node, [{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }], 220);
    skillPanel.scrollTop = 0;
  }

  // 子页面（除了列表）照着 skillView 画一遍。有输入框的那几种，画过之后只更新提示（view.refresh）
  function paintSkill(focus) {
    const view = skillView;
    if (view.kind === 'list') { drawSkillList(); return; }
    if (view.refresh && view.node && view.node.isConnected && focus === undefined) { view.refresh(); return; }
    const keep = focus || focusedKey();
    const build = { detail: buildSkillDetail, edit: buildSkillEditor, create: buildSkillCreate, url: buildSkillUrl, upload: buildSkillUpload, review: buildSkillReview }[view.kind];
    view.node = build(view);
    view.language = language;
    skillPanel.replaceChildren(view.node);
    if (view.refresh) view.refresh();
    focusSkill(keep, 'back');
  }

  // 子页面的头：返回按钮、标题、一句说明。title 是 [原文] 时按原文翻译，否则是数据（比如 Skill 的名字）原样显示
  function skillHead(title, lead) {
    const head = el('div', 'skill-head');
    const back = iconButton('back', k('返回 Skill 列表'));
    back.classList.remove('small');
    back.dataset.key = 'back';
    back.addEventListener('click', () => leaveSkillView());
    head.appendChild(back);
    const words = head.appendChild(el('div', 'skill-head-text'));
    const heading = words.appendChild(Array.isArray(title) ? tel('h3', 'skill-title plain', title[0], title[1]) : el('h3', 'skill-title', title));
    heading.tabIndex = -1;
    heading.dataset.key = 'title';
    if (Array.isArray(lead)) words.appendChild(tel('p', 'skill-lead', lead[0], lead[1]));
    else if (lead) words.appendChild(el('p', 'skill-lead', lead));
    return { head, words };
  }

  function skillTrust(calm) {
    const box = el('p', 'skill-trust' + (calm ? ' calm' : ''));
    box.append(icon(calm ? 'copy' : 'shield'), tel('span', null, calm
      ? k('导入的是一份复制：之后在 Claude Code 那边改了、删了，都不会影响智狐这边。')
      : k('只从你信任的地方安装。Skill 里的说明会被助手照着做，里面的脚本也可能被运行（运行之前仍然会问你）。')));
    return box;
  }

  function skillWaiting(key) {
    const box = el('div', 'skill-wait');
    box.setAttribute('role', 'status');
    const loop = document.createElementNS(SVG, 'svg');
    loop.setAttribute('class', 'loop');
    loop.setAttribute('aria-hidden', 'true');
    loop.appendChild(document.createElementNS(SVG, 'use')).setAttribute('href', '#i-loop');
    box.append(loop, tel('span', null, key));
    return box;
  }

  // ---- 列表 ----

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
    const folder = iconButton('folder', ON_MAC ? k('在访达中打开 Skill 文件夹') : k('打开 Skill 文件夹'));
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
      skillButton('import', k('从 Claude Code 导入'), '', 'bar:import', startSkillImport),
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
    spec.appendChild(tel('span', null, k('格式是开放的 Agent Skills 标准：在 Claude、Claude Code 里用的 Skill 可以直接装进来。')));
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
    action('folder', SHOW_FILE, ON_MAC ? k('在访达中显示：{name}') : k('在文件夹中显示：{name}'), '', 'folder').addEventListener('click', () => revealSkill(name));
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
    const shown = shortPath(reply.data.path);
    const said = !reply.ok ? { text: reply.error, bad: true }
      : state && state.file_manager === false ? { key: k('这台电脑上没有文件管理器。文件在：{path}'), values: { path: String(reply.data.path || '') }, good: true }
      : name ? { key: ON_MAC ? k('已在访达中显示：{path}') : k('已在文件夹中显示：{path}'), values: { path: shown }, good: true }
        : { key: ON_MAC ? k('已在访达中打开：{path}') : k('已打开文件夹：{path}'), values: { path: shown }, good: true };
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
    } else if (key.key === 'Enter' && (ON_MAC ? key.metaKey : key.ctrlKey) && skillView.save) {
      key.preventDefault();
      skillView.save();
    }
  });

  // ---- 打开一个 ----

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

  // ---- 从 Claude Code 导入 ----

  async function startSkillImport() {
    showSkillView({ kind: 'review', origin: 'claude', loading: true, candidates: [], picks: {}, results: {}, places: [], source: {} });
    const view = skillView;
    const mine = skillSeq;
    const reply = await call('/api/skill/claude');
    if (mine !== skillSeq || skillView !== view) return;
    if (!reply.ok) { view.loading = false; view.said = { text: reply.error, bad: true }; paintSkill(); return; }
    showSkillReview(reply.data, 'claude', null, view);
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
      // 只认出一个、合格、又没有同名的，默认勾上。认出好几个的（一整个仓库、Claude Code 里的一堆）一个都不先勾：
      // 装哪几个由你来挑。同名的勾上之后，再问你改名还是替换
      const only = candidates.length === 1 && !candidate.problems.length && !candidate.exists;
      picks[candidate.key] = { on: only, mode: 'rename', name: candidate.exists ? freeSkillName(candidate.name) : candidate.name };
    }
    const view = { kind: 'review', origin, back, stage: data.stage, source: data.source || {}, places: data.places || [], candidates, picks, results: {}, installing: false, loading: false, said: null };
    if (reuse && skillView === reuse) { skillView = view; paintSkill('title'); }
    else showSkillView(view);
  }

  function buildSkillReview(view) {
    const node = el('div', 'skill-view');
    const source = view.source || {};
    const commit = source.commit ? ' @ ' + source.commit.slice(0, 7) : '';
    const lead = view.origin === 'claude' ? (view.places.length ? [k('在这些地方找过：{places}'), { places: view.places.join('  ·  ') }] : null)
      : view.origin === 'upload' ? [k('上传的 {place}'), { place: source.label || '' }]
        : [k('来自 {place}'), { place: (source.label || source.url || '') + commit }];
    node.appendChild(skillHead(view.origin === 'claude' ? [k('从 Claude Code 导入')] : [k('确认安装')], lead).head);
    if (view.loading) { node.appendChild(skillWaiting(k('正在查找 Claude Code 的 Skill……'))); return node; }
    node.appendChild(skillTrust(view.origin === 'claude'));

    const list = view.candidates;
    if (!list.length) {
      node.appendChild(tel('p', 'hint', view.origin === 'claude'
        ? k('没有找到 Claude Code 的 Skill。它们一般放在 ~/.claude/skills/ 或者项目里的 .claude/skills/ 下面，每个是一个带 SKILL.md 的文件夹。')
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
        if (group) node.appendChild(el('h4', 'skill-group', group));
        cards = node.appendChild(el('ul', 'skill-cands'));
      }
      cards.appendChild(skillCandidate(view, candidate, index));
    });

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
    // 关窗之前问一声（见上面的 closeBlocker）：正在编辑、新建，还有没保存的修改，就先提醒一次、不关。
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
