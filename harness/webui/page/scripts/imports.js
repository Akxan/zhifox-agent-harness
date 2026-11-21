// ---- 扩展 · 从其他工具导入：三页共用的部分 -----------------------------------
// Skill、MCP、Agent 三页各有一个"从其他工具导入"。点了才去读别的工具（Claude Code、Cursor、VS Code、Codex、
// Gemini CLI……）的文件，打开的是那一页自己的清单：勾选、同名时改名还是替换、怎么装，都是各页原来那一套
// （skills-review.js、mcp-import.js、agents-import.js）。这一节只画三页共用的几块：
//   顶上的总览     在哪些工具里找到了东西、每样各几个。点这一样的数字，滚到那个工具的那一组；
//                  点另外两样，换到那一页去导入（那一页还有没保存的修改的话，只换过去，不动它）
//   每一组的标题   哪个工具、哪一级（用户级、这个项目）、哪个文件
//   "没找到的"     智狐认得、但在这台电脑上没找到的工具：去过哪些地方、官方文档在哪（收起来的）
// 哪个工具把东西放在哪儿、各找到几个，都由助手程序算好发过来（scan 接口回答里的 tools，见 harness/imports/），
// 页面不自己判断。工具的名字是产品名，任何语言下都照原样写。

const IMPORT_KINDS = {
  mcp: { tab: 'mcp', one: k('1 个 MCP 服务器'), many: k('{n} 个 MCP 服务器'), go: k('到 MCP 一页去导入'),
    missing: k('智狐认得这些工具，但在下面这些地方没有找到它们的 MCP 服务器。配置放在别处的话，可以用“粘贴 JSON”。'),
    idle: () => ['list', 'detail', 'import'].includes(mcpView.kind), start: () => openMcpImport() },
  skills: { tab: 'skills', one: k('1 个 Skill'), many: k('{n} 个 Skill'), go: k('到 Skill 一页去导入'),
    missing: k('智狐认得这些工具，但在下面这些地方没有找到它们的 Skill。放在别处的话，可以用“上传”。'),
    idle: () => ['list', 'detail', 'review'].includes(skillView.kind) && !skillView.installing, start: () => startSkillImport() },
  agents: { tab: 'agents', one: k('1 个 Agent'), many: k('{n} 个 Agent'), go: k('到 Agent 一页去导入'),
    missing: k('智狐认得这些工具，但在下面这些地方没有找到它们的 Agent。'),
    idle: () => ['list', 'detail', 'review', 'examples'].includes(agentView.kind) && !agentView.installing, start: () => startAgentImport() },
};

// 工具的标记：名字的头两个字母（Claude Code → CC，Cursor → Cu）。只是装饰，读屏软件读的是名字
function importMark(name) {
  const words = String(name || '?').split(/\s+/).filter(Boolean);
  const text = words.length > 1 ? words[0][0] + words[1][0] : (words[0] || '?').slice(0, 2);
  const mark = el('span', 'import-mark', text.charAt(0).toUpperCase() + text.slice(1));
  mark.setAttribute('aria-hidden', 'true');
  return mark;
}

// 顶上的总览：找到了东西的工具，一个一行（窄的时候自动折行）
function importOverview(tools, kind) {
  const found = (tools || []).filter((tool) => tool.found);
  if (!found.length) return null;
  const box = el('div', 'import-overview');
  box.setAttribute('role', 'group');
  tattr(box, 'aria-label', k('在这些工具里找到了'));
  box.appendChild(tel('h4', 'import-caption', k('在这些工具里找到了')));
  const list = box.appendChild(el('ul', 'import-tools'));
  for (const tool of found) list.appendChild(importToolRow(tool, kind));
  return box;
}

function importToolRow(tool, kind) {
  const row = el('li', 'import-tool');
  row.dataset.tool = tool.id;
  row.appendChild(importMark(tool.name));
  const words = row.appendChild(el('div', 'import-tool-words'));
  words.appendChild(el('b', 'import-tool-name', tool.name));
  const chips = words;  // 名字和几样的数目排在一行里，放不下就折到下一行
  for (const one of Object.keys(IMPORT_KINDS)) {
    const n = (tool.counts || {})[one] || 0;
    if (!n) continue;
    const spec = IMPORT_KINDS[one];
    const chip = chips.appendChild(el('button', 'import-chip' + (one === kind ? ' here' : '')));
    chip.type = 'button';
    chip.dataset.kind = one;
    chip.appendChild(tel('span', null, n === 1 ? spec.one : spec.many, n === 1 ? undefined : { n: n.toLocaleString(LOCALES[language]) }));
    if (one === kind) {
      chip.setAttribute('aria-current', 'true');
      chip.addEventListener('click', () => importJump(tool.id, kind));
    } else {
      chip.appendChild(icon('chevron', 'import-go'));
      tattr(chip, 'title', spec.go);
      chip.addEventListener('click', () => importSwitch(one, tool.id));
    }
  }
  if (tool.problems) chips.appendChild(tel('span', 'import-chip bad', tool.problems === 1 ? k('1 个文件读不出来') : k('{n} 个文件读不出来'), tool.problems === 1 ? undefined : { n: tool.problems }));
  return row;
}

// 点这一样的数字：滚到那个工具的第一组，焦点放在组的标题上（读屏软件会读出是哪个工具、哪个文件）
function importJump(toolId, kind) {
  const panel = $('panel-' + IMPORT_KINDS[kind].tab);
  const head = panel.querySelector('.import-group[data-tool="' + CSS.escape(toolId) + '"]');
  if (!head) return;
  head.scrollIntoView({ block: 'start', behavior: calm() ? 'auto' : 'smooth' });
  head.focus({ preventScroll: true });
  play(head, [{ opacity: 0.35 }, { opacity: 1 }], 420);
}

// 点另外一样：换到那一页去导入，清单画好以后滚到这个工具的那一组（importArrive）。
// 那一页正在编辑、还没保存的话，只换过去让你看到，不替你丢掉
let importHeading = null;  // {kind, tool}：换过去以后要滚到哪个工具
function importSwitch(kind, toolId) {
  const spec = IMPORT_KINDS[kind];
  showExtensionTab(spec.tab, false);
  if (!spec.idle()) { extensionSay(k('这一页还有没保存的修改：先保存或者取消，再从其他工具导入。'), null, 'bad'); return; }
  importHeading = { kind, tool: toolId };
  spec.start();
}

// 那一页的清单画好了（三页各自在画完之后调用）：是从总览换过来的，就滚到那个工具的那一组
function importArrive(kind) {
  const heading = importHeading;
  if (!heading || heading.kind !== kind) return;
  importHeading = null;
  requestAnimationFrame(() => importJump(heading.tool, kind));
}

// 一组的标题：哪个工具、哪一级、哪个文件（或者文件夹）。data-tool 是总览里"滚到这里"要找的
function importGroupHead(tool, toolId, level, path) {
  const head = el('h4', 'import-group');
  head.dataset.tool = toolId || '';
  head.tabIndex = -1;
  head.appendChild(el('b', 'import-group-tool', tool));
  if (level) head.appendChild(tel('span', 'import-level', level));
  head.appendChild(el('span', 'path', path));
  return head;
}

// 一项的"换了什么、丢了什么"：程序给的是原文和值（{key, values}），换语言时就地重译
function importNotes(notes) {
  if (!notes || !notes.length) return null;
  const list = el('ul', 'import-notes');
  for (const note of notes) list.appendChild(tel('li', null, note.key, note.values));
  return list;
}

// 最下面的"没找到的"：支持这一样、但这一样一个都没找到的工具（收起来的；一个都没找到时打开着）
function importMissing(tools, kind, open) {
  const missing = (tools || []).filter((tool) => tool.kinds.includes(kind) && !((tool.counts || {})[kind]));
  if (!missing.length) return null;
  const details = el('details', 'import-missing');
  details.open = Boolean(open);
  const summary = details.appendChild(el('summary'));
  summary.dataset.key = 'missing';
  summary.appendChild(tel('span', null, k('没找到的（{n} 个工具）'), { n: missing.length }));
  details.appendChild(tel('p', 'import-missing-lead', IMPORT_KINDS[kind].missing));
  const list = details.appendChild(el('ul', 'import-missing-list'));
  for (const tool of missing) {
    const row = list.appendChild(el('li'));
    const link = row.appendChild(el('a', 'import-missing-name'));
    link.href = tool.docs;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.append(el('span', null, tool.name), icon('external'));
    if (tool.blurb) row.appendChild(tel('span', 'import-missing-blurb', tool.blurb));
    const where = row.appendChild(el('span', 'import-missing-where'));
    for (const path of (tool.looked || {})[kind] || []) where.appendChild(el('code', 'path', path));
  }
  return details;
}
