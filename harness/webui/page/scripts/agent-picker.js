// ---- 新对话旁边的 Agent 选择 ----------------------------------------------------
// "新对话"右边那个小箭头：点开是你的 Agent，选一个就用它开始一段新对话（这段对话的角色，见 harness/agents/）。
// 是一个菜单（role=menu）：方向键上下移动，Home / End 到头尾，回车选，Esc 关上并回到箭头上，点别处也关。
// 顶上标题前面的那个小标签显示这段对话的角色，点一下打开它的设定。

const personaOpen = $('persona-open'), personaMenu = $('persona-menu');

function drawPersonaMenu() {
  const usable = (agents || []).filter((agent) => !agent.problems.length);
  const nodes = [tel('div', 'persona-menu-title', k('用一个 Agent 开始新对话'))];
  if (!usable.length) {
    nodes.push(tel('p', 'persona-empty', agents && agents.length ? k('现在的 Agent 都需要修改，修好之后就能用它开始对话。') : k('还没有 Agent。新建一个，或者从示例里挑一个。')));
  }
  for (const agent of usable) {
    const item = el('button', 'persona-item');
    item.type = 'button';
    item.setAttribute('role', 'menuitem');
    item.tabIndex = -1;
    item.dataset.color = agent.color || '';
    const words = el('span');
    words.append(el('b', null, agent.name), el('small', null, agent.description));
    item.append(agentFace(agent, 'small'), words);
    item.title = agent.description;
    item.addEventListener('click', () => { closePersonaMenu(false); startNew(agent.name); });
    nodes.push(item);
  }
  const manage = el('button', 'persona-item manage');
  manage.type = 'button';
  manage.setAttribute('role', 'menuitem');
  manage.tabIndex = -1;
  manage.append(icon(usable.length ? 'sliders' : 'plus'), tel('span', null, usable.length ? k('管理 Agent…') : k('添加 Agent…')));
  manage.addEventListener('click', () => { closePersonaMenu(false); openExtensions('agents'); });
  nodes.push(manage);
  const focused = personaMenu.contains(document.activeElement) ? [...personaMenu.querySelectorAll('.persona-item')].indexOf(document.activeElement) : -1;
  personaMenu.replaceChildren(...nodes);
  if (focused >= 0) { const items = personaMenu.querySelectorAll('.persona-item'); (items[Math.min(focused, items.length - 1)] || personaOpen).focus(); }
}

function openPersonaMenu(last) {
  if (busy) { toast(t(BUSY_HINT)); return; }
  drawPersonaMenu();
  personaMenu.hidden = false;
  personaOpen.setAttribute('aria-expanded', 'true');
  const items = personaMenu.querySelectorAll('.persona-item');
  items[last ? items.length - 1 : 0].focus();
}

function closePersonaMenu(focusButton) {
  if (personaMenu.hidden) return;
  personaMenu.hidden = true;
  personaOpen.setAttribute('aria-expanded', 'false');
  if (focusButton) personaOpen.focus();
}

personaOpen.addEventListener('click', () => (personaMenu.hidden ? openPersonaMenu(false) : closePersonaMenu(true)));
personaOpen.addEventListener('keydown', (key) => {
  if (key.key === 'ArrowDown' || key.key === 'ArrowUp') { key.preventDefault(); openPersonaMenu(key.key === 'ArrowUp'); }
});
personaMenu.addEventListener('keydown', (key) => {
  const items = [...personaMenu.querySelectorAll('.persona-item')];
  const at = items.indexOf(document.activeElement);
  const next = { ArrowDown: at + 1, ArrowUp: at - 1, Home: 0, End: items.length - 1 }[key.key];
  if (next !== undefined) { key.preventDefault(); items[(next + items.length) % items.length].focus(); return; }
  if (key.key === 'Escape') { key.preventDefault(); key.stopPropagation(); closePersonaMenu(true); return; }
  if (key.key === 'Tab') closePersonaMenu(false);  // 用 Tab 离开：菜单收起来，焦点照常往下走
});
document.addEventListener('pointerdown', (event) => {
  if (!personaMenu.hidden && !personaMenu.contains(event.target) && !personaOpen.contains(event.target)) closePersonaMenu(false);
});

// 顶上的角色名和空白页上的那一行。persona 来自状态：{name, color, missing}；普通的对话是 null
function renderPersona(persona) {
  const chip = $('persona'), hero = $('hero-persona');
  chip.hidden = hero.hidden = !persona;
  $('hero-lead').hidden = Boolean(persona);
  if (!persona) return;
  const agent = agentByName(persona.name);
  chip.dataset.color = hero.dataset.color = persona.color || '';
  chip.classList.toggle('missing', Boolean(persona.missing));
  $('persona-name').textContent = persona.name;
  chip.title = persona.missing
    ? t('这段对话是用「{name}」开始的，但它现在用不了。接着聊用的是内置的提示词和全部工具。', { name: persona.name })
    : t('这段对话的角色是「{name}」。点一下看看它的设定。', { name: persona.name });
  chip.setAttribute('aria-label', chip.title);
  const words = el('span', null, agent && agent.description ? agent.description : '');
  hero.replaceChildren(agentFace({ name: persona.name, color: persona.color }, 'small'), el('b', null, persona.name), words);
}

$('persona').addEventListener('click', () => {
  const persona = state && state.persona;
  if (!persona) return;
  openExtensions('agents');
  if (agentByName(persona.name)) openAgent(persona.name);
});
