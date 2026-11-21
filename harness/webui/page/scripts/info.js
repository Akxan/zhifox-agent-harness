// ---- 信息栏：费用、用量、待办、工具 --------------------------------------
// 右边的信息栏：这段对话的费用和用量（四类 token 的比例条）、待办、这段对话能用的工具。
// 费用卡片的下半部分（今天、本月、预算）在 spending.js。

const usageCells = {};
for (const [list, key, name, swatch] of [
  ['usage', 'input', k('输入 · 原价'), 'k1'], ['usage', 'cache_read', k('输入 · 命中缓存'), 'k2'],
  ['usage', 'cache_write', k('输入 · 写入缓存'), 'k3'], ['usage', 'output', k('输出'), 'k4'],
  ['usage-more', 'context', k('当前对话占用的上下文')], ['usage-more', 'requests', k('请求次数')],
  ['usage-more', 'searches', k('联网搜索次数')],
]) {
  const term = $(list).appendChild(el('dt'));
  if (swatch) term.appendChild(el('i', 'swatch ' + swatch));
  term.appendChild(tel('span', null, name));
  usageCells[key] = $(list).appendChild(el('dd', null, '0'));
}
const mixBars = [...$('mix').children].reverse();  // 页面上是从下往上叠的，这里按 输入、命中缓存、写入缓存、输出 的顺序

function renderUsage(event) {
  const usage = event.usage;
  for (const key of Object.keys(usageCells)) usageCells[key].textContent = usage[key].toLocaleString(LOCALES[language]);

  const parts = [usage.input, usage.cache_read, usage.cache_write, usage.output];
  const total = parts.reduce((sum, value) => sum + value, 0);
  let running = 0;
  parts.forEach((value, index) => {
    running += value;
    mixBars[index].style.transform = 'scaleX(' + (total ? running / total : 0) + ')';
  });

  // 金额按界面语言的习惯写（西班牙语是"0,0034 $"），小数点后固定 4 位
  const cost = new Intl.NumberFormat(LOCALES[language], { style: 'currency', currency: 'USD', currencyDisplay: 'narrowSymbol', minimumFractionDigits: 4, maximumFractionDigits: 4 }).format(event.cost);
  if ($('cost').textContent !== cost) {
    $('cost').textContent = cost;
    if (isLive()) play($('cost'), [{ opacity: .4, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], 320);
  }
  $('foot-cost').textContent = t('本次运行估算 {cost}', { cost });
  $('cost-note').textContent = event.unpriced.length
    ? t('没有计入 {models}：还没有设置价格，可以在“设置”里填写。', { models: event.unpriced.join(t('、')) })
    : t('按公开价格估算，实际以各家的账单为准。');
}

let knownTodos = null, todosDrawn = '', toolsDrawn = '';

// 待办的截止日期存的是 2026-11-27 这样的写法，显示时按界面语言写成"11月27日""Nov 27""27 nov"。
// 模型偶尔写的不是这种格式（比如"周五"），就照原样显示
function dueText(due) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(due);
  if (!match) return due;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  const sameYear = date.getFullYear() === new Date().getFullYear();
  return date.toLocaleDateString(LOCALES[language], sameYear ? { month: 'short', day: 'numeric' } : { year: 'numeric', month: 'short', day: 'numeric' });
}

// 待办：和会话、记忆一样，每一行悬停时有"在访达中显示 / 编辑 / 删除"；前面的圆圈点一下就是完成。
// 改动都请助手程序去做（/api/todo/*），它改完会发来新的状态，列表照常跟着状态走
let todoList = [], todoEditing = null, todoRemoving = null;

function renderTodos(todos) {
  todoList = Array.isArray(todos) ? todos : [];
  if (todoEditing && !todoList.some((item) => item.id === todoEditing.id)) todoEditing = null;
  if (todoRemoving && !todoList.some((item) => item.id === todoRemoving)) todoRemoving = null;
  const print = JSON.stringify([todoList, language, todoEditing && todoEditing.id, todoRemoving]);
  if (print === todosDrawn) return;
  todosDrawn = print;
  const fresh = knownTodos && isLive() ? todoList.filter((item) => !knownTodos.has(item.id)).map((item) => item.id) : [];
  knownTodos = new Set(todoList.map((item) => item.id));
  const box = $('todos'), active = document.activeElement;
  const keep = box.contains(active) && active.closest('.todo') ? [Number(active.closest('.todo').dataset.id), active.dataset.role] : null;
  box.replaceChildren(...(todoList.length ? todoList.map((item) => todoRow(item, fresh.includes(item.id))) : [el('li', 'none', t('没有未完成的待办'))]));
  if (keep) {
    const row = [...box.children].find((node) => Number(node.dataset.id) === keep[0]);
    const target = row && (row.querySelector(`[data-role="${keep[1]}"]`) || row.querySelector('.todo-mark'));
    if (target) target.focus();
  }
}

function todoRow(item, fresh) {
  const row = el('li', 'todo' + (fresh ? ' enter' : ''));  // 刚加上的那一条浮现出来
  row.dataset.id = String(item.id);
  if (todoRemoving === item.id) {  // 删除前在原地确认一下
    const box = row.appendChild(el('div', 'todo-confirm'));
    box.appendChild(el('span', null, t('删除这条待办？')));
    const yes = box.appendChild(el('button', 'mini danger', t('删除')));
    const no = box.appendChild(el('button', 'mini cancel', t('取消')));
    yes.type = no.type = 'button';
    no.dataset.role = 'cancel';
    yes.addEventListener('click', async () => {
      yes.disabled = no.disabled = true;
      const reply = await call('/api/todo/delete', { id: item.id });
      todoRemoving = null;
      todosDrawn = '';
      if (!reply.ok) { toast(reply.error); renderTodos(todoList); return; }
      renderTodos(todoList.filter((other) => other.id !== item.id));
      toast(t('已删除待办：{title}', { title: item.title }));
    });
    no.addEventListener('click', () => { todoRemoving = null; todosDrawn = ''; renderTodos(todoList); });
    box.addEventListener('keydown', (key) => { if (key.key === 'Escape') { key.preventDefault(); key.stopPropagation(); no.click(); } });
    setTimeout(() => { if (row.isConnected) no.focus(); });
    return row;
  }
  if (todoEditing && todoEditing.id === item.id) {  // 编辑：标题和截止日期，就地改
    const form = row.appendChild(el('div', 'todo-edit'));
    const title = form.appendChild(el('input', 'field'));
    title.type = 'text';
    title.maxLength = 200;
    title.value = todoEditing.title;
    tattr(title, 'aria-label', k('待办的内容'));
    const line = form.appendChild(el('div', 'row'));
    const due = line.appendChild(el('input', 'field'));
    due.type = 'date';
    due.value = todoEditing.due || '';
    tattr(due, 'aria-label', k('截止日期（可以不填）'));
    const save = line.appendChild(el('button', 'mini primary', t('保存')));
    const cancel = line.appendChild(el('button', 'mini', t('取消')));
    save.type = cancel.type = 'button';
    title.addEventListener('input', () => { todoEditing.title = title.value; });
    due.addEventListener('input', () => { todoEditing.due = due.value; });
    const finish = async (keep) => {
      if (!keep) { todoEditing = null; todosDrawn = ''; renderTodos(todoList); focusTodo(item.id); return; }
      if (!title.value.trim()) { title.focus(); return; }
      save.disabled = true;
      const reply = await call('/api/todo/update', { id: item.id, title: title.value.trim(), due: due.value || null });
      todoEditing = null;
      todosDrawn = '';
      if (!reply.ok) toast(reply.error);
      renderTodos(todoList);
      focusTodo(item.id);
    };
    save.addEventListener('click', () => finish(true));
    cancel.addEventListener('click', () => finish(false));
    form.addEventListener('keydown', (key) => {
      if (key.isComposing || key.keyCode === 229) return;  // 输入法正在组字
      if (key.key === 'Enter') { key.preventDefault(); finish(true); }
      if (key.key === 'Escape') { key.preventDefault(); key.stopPropagation(); finish(false); }
    });
    setTimeout(() => { if (row.isConnected) { title.focus(); title.select(); } });
    return row;
  }
  const mark = row.appendChild(el('button', 'todo-mark'));
  mark.type = 'button';
  mark.dataset.role = 'done';
  mark.title = t('标记为完成');
  mark.setAttribute('aria-label', t('完成：{title}', { title: item.title }));
  mark.addEventListener('click', () => completeTodo(item, row));
  row.appendChild(el('span', 'todo-text', item.title));
  if (item.due) row.appendChild(el('span', 'due', dueText(item.due))).title = item.due;
  const actions = row.appendChild(el('div', 'todo-actions'));
  const button = (iconName, title, label, role, extra) => {
    const node = actions.appendChild(el('button', 'icon-button small' + (extra ? ' ' + extra : '')));
    node.type = 'button';
    node.dataset.role = role;
    node.title = t(title);
    node.setAttribute('aria-label', t(label, { name: item.title }));
    node.appendChild(icon(iconName));
    return node;
  };
  button('folder', SHOW_FILE, ON_MAC ? k('在访达中显示待办的文件') : k('在文件夹中显示待办的文件'), 'reveal').addEventListener('click', () => revealData('todos'));
  button('edit', k('编辑'), k('编辑待办：{name}'), 'edit').addEventListener('click', () => {
    todoRemoving = null;
    todoEditing = { id: item.id, title: item.title, due: item.due || '' };
    todosDrawn = '';
    renderTodos(todoList);
  });
  button('trash', k('删除'), k('删除待办：{name}'), 'delete', 'danger-hover').addEventListener('click', () => {
    todoEditing = null;
    todoRemoving = item.id;
    todosDrawn = '';
    renderTodos(todoList);
  });
  return row;
}

function focusTodo(id) {
  const row = [...$('todos').children].find((node) => Number(node.dataset.id) === id);
  if (row) (row.querySelector('.todo-mark') || row).focus();
}

// 完成：先在这一行上打个勾、划掉，再请助手程序记下来；它发来的新状态里这一条就不在"未完成"里了
async function completeTodo(item, row) {
  row.classList.add('done');
  const [reply] = await Promise.all([call('/api/todo/update', { id: item.id, done: true }), new Promise((done) => setTimeout(done, calm() ? 0 : 420))]);
  if (!reply.ok) { row.classList.remove('done'); toast(reply.error); return; }
  await leave(row);
  todosDrawn = '';
  renderTodos(todoList.filter((other) => other.id !== item.id));
  toast(t('已完成：{title}', { title: item.title }));
}

// 在文件管理器里显示助手的一个数据文件。页面只说是哪一个（tasks / todos / prompt），在哪儿由助手程序定。
// say：说结果的地方。开着对话框的时候，浮在页面上的提示会被它挡住，所以由调用的地方给
async function revealData(what, say = toast) {
  const reply = await call('/api/reveal', { what });
  if (!reply.ok) { say(reply.error, true); return; }
  say(revealedText(reply.data.path));
}

function renderTools(tools) {
  const print = JSON.stringify([tools, language]);
  if (print === toolsDrawn) return;
  toolsDrawn = print;
  $('tools').replaceChildren(...tools.map((tool) => {
    const chip = el('li', 'tool-chip' + (tool.local ? '' : ' remote'));
    chip.title = tool.local ? t('在这台电脑上执行') : t('在厂商的服务器上执行');
    chip.appendChild(el('i', 'dot'));
    chip.appendChild(el('span', null, tool.name));
    return chip;
  }));
  $('legend-remote').hidden = tools.every((tool) => tool.local);
  // 用一个不用工具的 Agent（tools: []）开始的对话：一个工具都没有，说一句，图例也不用了
  $('tools-legend').hidden = !tools.length;
  if (!tools.length) $('tools').appendChild(tel('li', 'agents-hint', k('这段对话不用任何工具。')));
}
