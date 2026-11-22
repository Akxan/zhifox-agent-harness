// ---- 智狐能做的事 ----------------------------------------------------------
// 信息栏里的一节：这段对话里每一样功能（待办、读写文件、浏览器、每个 MCP 服务器……）在不在用、为什么、
// 会不会先问你、在哪儿执行、关着的怎么打开。清单由助手程序算好放在状态里（state.capabilities，格式见
// harness/capabilities.py 的开头），这里只负责画，不自己判断任何一条规矩：问不问你、为什么关着，都照着它写。
//
//   一行          图标、名字（Skill、Agent、MCP 后面带个数），下面一行：在用的写"问不问你"和它能做什么，别的写为什么
//   点开          它能做什么、问不问你（几类有代表性的操作各自怎样）、在哪儿执行、能去哪儿管，最后是收起来的"技术细节"
//   关着的        右边多一个按钮（"在设置里打开""开始新对话""管理"……），直接接到页面上已有的那几个窗口
//   这里用不了的  收在最下面"这段对话用不了的 N 项"里，免得把在用的挤下去；点开照样看得到原因
// 工具的真名（str_replace_based_edit_tool……）只在"技术细节"里。点开的那几行、展开的技术细节在重画以后照样开着。

const CAP_STATUS = {
  on: { label: k('在用'), icon: 'check' },
  pending: { label: k('下一段对话'), icon: 'clock' },
  off: { label: k('关着'), icon: 'minus' },
  unavailable: { label: k('用不了'), icon: 'ban' },
};
// 在用的那几行，下面一行开头写"问不问你"（级别由程序按现在的确认方式算好）
const CAP_LEVEL = {
  free: { label: k('直接做'), icon: 'check' },
  some: { label: k('部分先问'), icon: 'shield' },
  ask: { label: k('先问你'), icon: 'shield' },
  always: { label: k('每步都问'), icon: 'lock' },
};
const CAP_OUTCOME = {
  free: { label: k('直接做'), icon: 'check' },
  ask: { label: k('先问你'), icon: 'shield' },
  always: { label: k('每次都问'), icon: 'lock' },
  never: { label: k('不做'), icon: 'ban' },
};

let capsData = null;            // 状态里最新的那一份
let capsDrawn = '';             // 上一次按什么画的（没变就不重画，保留焦点）
let capsKnown = null;           // 上一次画的时候在用的那几项：新变成"在用"的那一行淡入一下（重放旧内容时不播）
let capsRestOpen = false;       // "这段对话用不了的 N 项"展开着没有
const capsOpen = new Set();     // 点开着的那几行（按编号）
const capsTech = new Set();     // 展开了"技术细节"的那几行

// 一句程序给的话：有原文的照原文译（换语言时跟着换），没有的（MCP 服务器的名字）原样显示
function capText(node, said) {
  if (said && said.key) retext(node, said.key, said.values);
  else { delete node.dataset.i18n; delete node.dataset.i18nValues; node.textContent = said ? said.text || '' : ''; }
  return node;
}

const capsPrint = () => JSON.stringify([capsData, language, [...capsOpen], [...capsTech], capsRestOpen]);

function renderCapabilities(data) {
  if (!data || !Array.isArray(data.items)) return;
  capsData = data;
  const print = capsPrint();
  if (print === capsDrawn) return;
  capsDrawn = print;
  const ids = new Set(data.items.map((item) => item.id));
  for (const set of [capsOpen, capsTech]) for (const id of [...set]) if (!ids.has(id)) set.delete(id);

  const on = data.items.filter((item) => item.status === 'on');
  const fresh = capsKnown && isLive() ? new Set(on.map((item) => item.id).filter((id) => !capsKnown.has(id))) : new Set();
  capsKnown = new Set(on.map((item) => item.id));

  const active = document.activeElement;
  const keep = $('caps').contains(active) ? active.dataset.capFocus || '' : '';
  const main = data.items.filter((item) => item.status !== 'unavailable');
  const rest = data.items.filter((item) => item.status === 'unavailable');
  $('caps-list').replaceChildren(...capRows(main, 'm', fresh));
  $('caps-rest-list').replaceChildren(...capRows(rest, 'r', fresh));

  retext($('caps-count'), k('{n} 项在用'), { n: on.length });
  retext($('caps-mode-name'), APPROVAL[data.mode] || APPROVAL.ask);
  $('caps-notes').replaceChildren(...(data.notes || []).map((note) => capText(el('p', 'caps-note'), note)));
  $('caps-rest').hidden = !rest.length;
  $('caps-rest-list').hidden = !capsRestOpen;
  $('caps-rest-toggle').setAttribute('aria-expanded', String(capsRestOpen));
  retext($('caps-rest-label'), k('这段对话用不了的 {n} 项'), { n: rest.length });

  if (keep) {
    const again = $('caps').querySelector(`[data-cap-focus="${CSS.escape(keep)}"]`);
    if (again) again.focus({ preventScroll: true });
  }
}

// 一串行。MCP 服务器一个一行，名字是你起的（files、notes……），单看名字不知道是什么：前面加一个小标题
function capRows(items, prefix, fresh) {
  const rows = [];
  items.forEach((item, index) => {
    const server = item.id.startsWith('mcp:');
    if (server && !(index && items[index - 1].id.startsWith('mcp:'))) rows.push(tel('li', 'caps-group', k('MCP 服务器')));
    rows.push(capRow(item, prefix + index, fresh.has(item.id)));
  });
  return rows;
}

// 一行。key 是这一行在页面上的编号（元素的 id 用它：MCP 服务器的名字什么字都可能有）
function capRow(item, key, fresh) {
  const open = capsOpen.has(item.id);
  const row = el('li', 'caps-item caps-' + item.status + (open ? ' caps-open' : ''));
  row.dataset.id = item.id;
  const head = row.appendChild(el('button', 'caps-head'));
  head.type = 'button';
  head.dataset.capFocus = item.id + '|head';
  head.setAttribute('aria-expanded', String(open));
  head.setAttribute('aria-controls', 'caps-body-' + key);
  head.appendChild(el('span', 'caps-tile')).appendChild(icon(item.icon));
  const words = head.appendChild(el('span', 'caps-words'));
  const line = words.appendChild(el('span', 'caps-line'));
  const name = line.appendChild(el('span', 'caps-name'));
  capText(name.appendChild(el('span', 'caps-label' + (item.label.key ? '' : ' caps-server'))), item.label);
  if (item.count) name.appendChild(el('span', 'caps-count', Number(item.count).toLocaleString(LOCALES[language])));
  line.appendChild(capChip(item));
  // 下面一行：在用的写它能做什么，别的写为什么是这样。收着时一行放不下就省略，点开以后整句折行显示（详情里不再重复）
  capText(words.appendChild(el('span', 'caps-brief')), capBrief(item));
  head.appendChild(icon('chevron', 'caps-chev'));
  head.addEventListener('click', () => toggleCap(item, row));

  // 关着的、下一段对话才有的：右边直接给一个能做点什么的按钮（打开设置、开始新对话、去扩展里管）
  if ((item.status === 'off' || item.status === 'pending') && item.actions.length) {
    row.classList.add('caps-has-quick');
    row.appendChild(capAction(item, item.actions[0], 0, 'caps-quick'));
  }

  const body = row.appendChild(el('div', 'caps-body'));
  body.id = 'caps-body-' + key;
  body.hidden = !open;
  if (open) fillCapBody(body, item);
  if (fresh) play(row, [{ opacity: 0, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], 320);
  return row;
}

const capBrief = (item) => (item.status === 'on' || !item.reason ? item.description : item.reason);

// 名字右边的小标签：在用的写"问不问你"，别的写状态。图标和文字一起说，不只靠颜色
function capChip(item) {
  const level = item.status === 'on' && item.approval ? CAP_LEVEL[item.approval.level] : null;
  const shown = level || CAP_STATUS[item.status] || CAP_STATUS.off;
  const chip = el('span', 'caps-chip ' + (level ? 'caps-level-' + item.approval.level : 'caps-state-' + item.status));
  chip.appendChild(icon(shown.icon));
  chip.appendChild(tel('span', null, shown.label));
  return chip;
}

function toggleCap(item, row) {
  const open = !capsOpen.has(item.id);
  if (open) capsOpen.add(item.id); else capsOpen.delete(item.id);
  const head = row.querySelector('.caps-head'), body = row.querySelector('.caps-body');
  head.setAttribute('aria-expanded', String(open));
  row.classList.toggle('caps-open', open);
  if (open) {
    fillCapBody(body, item);
    body.hidden = false;
    play(body, [{ opacity: 0, transform: 'translateY(-4px)' }, { opacity: 1, transform: 'none' }], 220);
  } else {
    body.hidden = true;
    body.replaceChildren();
  }
  capsDrawn = capsPrint();  // 只是这一行开合：不用整个重画
}

// 点开以后的详情。只在点开时才画（几十行详情一直挂在页面上没有意义）
function fillCapBody(body, item) {
  const parts = [];
  // 上面那一行写的是为什么（关着、用不了的），这里补上它能做什么；在用的那一行已经写了它能做什么，这里补上状态（"已启用 2 个"）
  if (capBrief(item) !== item.description) parts.push(capText(el('p', 'caps-desc'), item.description));
  else if (item.reason) {
    const state = el('p', 'caps-why caps-why-' + item.status);
    state.appendChild(icon((CAP_STATUS[item.status] || CAP_STATUS.off).icon));
    const words = state.appendChild(el('span'));
    words.appendChild(tel('b', null, (CAP_STATUS[item.status] || CAP_STATUS.off).label));
    words.appendChild(document.createTextNode(' · '));
    words.appendChild(capText(el('span'), item.reason));
    parts.push(state);
  }

  // 问不问你：一句总结，加上几类有代表性的操作各自怎样（这些都是程序拿现在的确认方式去问权限规则得来的）
  const approval = item.approval;
  if (approval) {
    const block = el('div', 'caps-approval');
    block.appendChild(tel('h3', 'caps-h', k('要不要先问你')));
    block.appendChild(capText(el('p', 'caps-summary'), approval.summary));
    if (approval.rules.length > 1 || (approval.rules[0] && approval.rules[0].outcome !== 'free')) {
      const rules = block.appendChild(el('ul', 'caps-rules'));
      for (const rule of approval.rules) {
        const shown = CAP_OUTCOME[rule.outcome] || CAP_OUTCOME.ask;
        const line = rules.appendChild(el('li', 'caps-rule caps-outcome-' + rule.outcome));
        capText(line.appendChild(el('span', 'caps-rule-text')), rule.text);
        const tag = line.appendChild(el('span', 'caps-outcome'));
        tag.appendChild(icon(shown.icon));
        tag.appendChild(tel('span', null, shown.label));
      }
    }
    if (approval.note) block.appendChild(capText(el('p', 'caps-fine'), approval.note));
    parts.push(block);
  }

  // 在哪儿执行（原来那条图例的意思：这台电脑上 / 厂商的服务器上 / 第三方的 MCP 服务器上）
  const where = el('p', 'caps-where');
  where.appendChild(el('i', 'caps-dot caps-dot-' + item.where));
  where.appendChild(capText(el('span'), item.where_text));
  parts.push(where);

  if (item.actions.length) {
    const actions = el('div', 'caps-actions');
    item.actions.forEach((action, index) => actions.appendChild(capAction(item, action, index, 'mini')));
    parts.push(actions);
  }

  // 技术细节：发给模型的名字。给想对照日志、写 Agent 文件（tools: …）的人看，默认收着
  if (item.tools.length) {
    const tech = el('details', 'caps-tech');
    tech.open = capsTech.has(item.id);
    const summary = tech.appendChild(tel('summary', null, k('技术细节')));
    summary.dataset.capFocus = item.id + '|tech';
    tech.appendChild(tel('p', 'caps-fine', k('发给模型的工具名')));
    const names = tech.appendChild(el('ul', 'caps-names'));
    for (const name of item.tools) names.appendChild(el('li', null, name));
    tech.addEventListener('toggle', () => {
      if (tech.open) capsTech.add(item.id); else capsTech.delete(item.id);
      capsDrawn = capsPrint();
    });
    parts.push(tech);
  }
  body.replaceChildren(...parts);
}

// 一个按钮：接到页面上已有的那几个窗口（设置、扩展、要不要先问你、不用问的命令、记忆、任务），或者开始新对话
function capAction(item, action, index, className) {
  const button = capText(el('button', className), action.label);
  button.type = 'button';
  button.dataset.capFocus = item.id + '|action' + index;
  button.addEventListener('click', () => runCapAction(item, action));
  return button;
}

function runCapAction(item, action) {
  if (overlaySide.matches) setSide(false, false);  // 窄屏上信息栏盖在对话上面：先收起来，打开的窗口或者新对话才看得见
  if (action.kind === 'settings') openSettingsAt(action.target);
  else if (action.kind === 'models') openModels();  // 换模型的面板（models.js）
  else if (action.kind === 'extensions') {
    openExtensions(action.target);
    // 某个 MCP 服务器的那一行：直接打开它的详情（它是那一页里已有的样子）
    if (action.target === 'mcp' && item.id.startsWith('mcp:') && typeof openMcpDetail === 'function') openMcpDetail(item.id.slice(4));
  } else if (action.kind === 'approval') openApproval();
  else if (action.kind === 'commands') openCommands();
  else if (action.kind === 'memory') openMemories();
  else if (action.kind === 'tasks') { setNav(true, false); setNavTab('tasks', true, true); }
  else if (action.kind === 'new') startNew();
}

// 打开设置，翻到要的那一节，焦点放在它的开关上（浏览器、操作整台电脑）
function openSettingsAt(target) {
  openSettings();
  const place = { browser: ['browser-section', 'browser-enabled'], computer: ['computer-section', 'computer-enabled'], sandbox: ['sandbox-section', 'sandbox-level'] }[target];
  if (!place) return;
  const [section, control] = place;
  $(section).scrollIntoView({ block: 'start', behavior: calm() ? 'auto' : 'smooth' });
  $(control).focus({ preventScroll: true });
}

$('caps-mode').addEventListener('click', openApproval);
$('caps-rest-toggle').addEventListener('click', () => {
  capsRestOpen = !capsRestOpen;
  $('caps-rest-toggle').setAttribute('aria-expanded', String(capsRestOpen));
  $('caps-rest-list').hidden = !capsRestOpen;
  if (capsRestOpen) play($('caps-rest-list'), [{ opacity: 0, transform: 'translateY(-4px)' }, { opacity: 1, transform: 'none' }], 220);
  capsDrawn = capsPrint();
});
