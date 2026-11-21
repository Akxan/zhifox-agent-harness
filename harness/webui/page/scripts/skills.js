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
