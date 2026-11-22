// ---- 扩展 · 钩子 · 添加和编辑 -----------------------------------------------
// 一个钩子的表单：名字、什么时候（四个时刻）、对哪些工具（只有工具前后的两个时刻有：勾几种功能，或者写工具的名字）、
// 命令和参数（一项一行，不经过 shell）、最多等几秒、钩子自己出错时怎么办（只有能拦下的两个时刻有）、在哪些运行里生效。
// 检查由助手程序做（harness/hooks/config.py）：页面只先拦下明显没填的，别的错照着它的回答写在表单底下。
// 参数里遮住的部分（••••）原样交回去就是"没改"。改过还没保存就想离开，先提醒一次，再来一次才放弃。
// 最后把这一页挂到扩展对话框上。

const HOOK_FAILURES = { continue: k('照常继续（默认）'), block: k('当成拦下') };

function hooksInput(id, value, placeholder, extra) {
  const input = el('input', 'field' + (extra ? ' ' + extra : ''));
  input.id = id;
  input.type = 'text';
  input.value = value || '';
  input.spellcheck = false;
  input.autocomplete = 'off';
  if (placeholder) tattr(input, 'placeholder', placeholder);
  return input;
}

// 一个带标题的格子。label 原文；help 是原文（和 values）或者 null。返回外面那一层，控件放在 control 位置
function hooksField(label, control, help, values) {
  const wrap = el('label', 'hooks-field');
  wrap.appendChild(tel('span', null, label));
  wrap.appendChild(control);
  if (help) wrap.appendChild(tel('span', 'hooks-help', help, values));
  return wrap;
}

function hooksGroup(id, legend, help) {
  const set = el('fieldset', 'hooks-field hooks-set');
  set.id = id;
  set.appendChild(tel('legend', null, legend));
  const body = set.appendChild(el('div', 'hooks-picks'));
  if (help) set.appendChild(tel('span', 'hooks-help', help));
  return [set, body];
}

function hooksPick(value, label, checked, iconName) {
  const pick = el('label', 'hooks-pick');
  const box = pick.appendChild(el('input'));
  box.type = 'checkbox';
  box.value = value;
  box.checked = checked;
  if (iconName && document.getElementById('i-' + iconName)) pick.appendChild(icon(iconName));
  pick.appendChild(tel('span', null, label));
  return pick;
}

function openHooksForm(original) {
  const row = original ? hooksFind(original) : null;
  if (original && (!row || !row.hook)) return;
  const hook = row ? row.hook : null;
  hooksSay('');
  hooksView = { kind: 'form', original, name: original, event: hook ? hook.event : 'PreToolUse', dirty: false, armed: false, saving: false };
  const view = hooksView;

  const head = el('div', 'hooks-head');
  const back = head.appendChild(el('button', 'hooks-back'));
  back.type = 'button';
  back.append(icon('chevron'), tel('span', null, original ? k('返回') : k('全部钩子')));
  back.addEventListener('click', hooksBack);
  head.appendChild(tel('h3', 'hooks-title', original ? k('编辑钩子') : k('添加钩子')));

  const form = el('form', 'hooks-form');
  form.noValidate = true;
  form.addEventListener('submit', (submit) => { submit.preventDefault(); saveHooksForm(); });
  const name = hooksInput('hooks-f-name', original, k('比如 no-rm'));
  name.maxLength = 64;
  name.dataset.focus = 'name';
  form.appendChild(hooksField(k('名字'), name, k('只用字母、数字、下划线、点和连字符。')));

  // 什么时候：四个时刻。下面那句说明跟着选的这一个换
  const event = el('select', 'field');
  event.id = 'hooks-f-event';
  for (const value of hooksData.events) {
    const option = event.appendChild(tel('option', null, k('{label}（{event}）'), { label: { key: HOOK_EVENTS[value].label, values: {} }, event: value }));
    option.value = value;
  }
  event.value = view.event;
  const eventField = form.appendChild(hooksField(k('什么时候'), event));
  const eventHelp = eventField.appendChild(el('span', 'hooks-help'));
  eventHelp.id = 'hooks-f-event-help';
  event.addEventListener('change', () => setHooksEvent(event.value, true));

  // 对哪些工具：勾几种功能（和信息栏"智狐能做的事"是同一张表），或者写工具的名字；都不选就是全部工具
  const items = String(hook && hook.matcher || '').split(/[|,]/).map((item) => item.trim()).filter((item) => item && item !== '*');
  const groupIds = hooksData.groups.map((group) => group.id);
  const [matcher, picks] = hooksGroup('hooks-f-matcher', k('对哪些工具'), k('都不选、也不写，就是全部工具。'));
  for (const group of hooksData.groups) picks.appendChild(hooksPick(group.id, group.label, items.includes(group.id), group.icon));
  const tools = hooksInput('hooks-f-tools', items.filter((item) => !groupIds.includes(item)).join(' | '), null, 'mono');
  tools.placeholder = 'mcp__github__* | run_command';  // 工具的名字是英文的，哪种语言都一样，不用翻译
  matcher.insertBefore(hooksField(k('或者写工具的名字'), tools, k('可以用 * 通配，几个之间用 | 隔开。MCP 的工具叫 mcp__服务器__工具。')), matcher.lastChild);
  form.appendChild(matcher);

  const command = hooksInput('hooks-f-command', hook ? hook.command : '', k('比如 python3、node，或者脚本的完整路径'), 'mono');
  form.appendChild(hooksField(k('命令'), command, k('要运行的程序。钩子在工作目录里运行，从标准输入读到一份 JSON。')));
  const args = el('textarea', 'hooks-area');
  args.id = 'hooks-f-args';
  args.spellcheck = false;
  args.rows = 3;
  args.value = hook ? hook.args.join('\n') : '';
  args.placeholder = '/path/to/check.py';  // 例子是一条路径，哪种语言都一样，不用翻译
  form.appendChild(hooksField(k('参数'), args, k('每行一个，不用加引号。命令不经过 shell：$HOME、* 这些写法不会被展开。')));
  if (hook && hook.args.some((arg) => arg.includes('••••'))) {
    form.appendChild(hooksNote('calm', k('参数里像密钥的部分遮住了（••••）：那几行不改，保存时就用原来的值。'), null, 'lock'));
  }

  const timeout = el('input', 'field hooks-number');
  timeout.id = 'hooks-f-timeout';
  timeout.type = 'number';
  timeout.min = '1';
  timeout.max = String(hooksData.max_timeout);
  timeout.step = '1';
  timeout.inputMode = 'numeric';
  timeout.placeholder = String(hooksData.timeout);
  timeout.value = hook && hook.timeout ? String(hook.timeout) : '';
  form.appendChild(hooksField(k('最多等几秒'), timeout, k('不填就是 {n} 秒，最长 {max} 秒。到时间还没结束就结束掉它，算钩子出错。'), { n: hooksData.timeout, max: hooksData.max_timeout }));

  const failure = el('select', 'field');
  failure.id = 'hooks-f-failure';
  for (const [value, label] of Object.entries(HOOK_FAILURES)) {
    const option = failure.appendChild(tel('option', null, label));
    option.value = value;
  }
  failure.value = hook && hook.onFailure === 'block' ? 'block' : 'continue';
  const failureField = form.appendChild(hooksField(k('钩子自己出错时'), failure, k('启动不了、超时、退出码不是 0 也不是 2 时怎么办。管安全的钩子可以选“当成拦下”：它坏了的时候宁可不做。')));
  failureField.id = 'hooks-f-failure-field';

  const [runs, runPicks] = hooksGroup('hooks-f-runs', k('在哪儿生效'), k('默认三处都算：钩子只能让事情更严，没人看着的定时任务里反而最需要它。'));
  const on = hook ? hook.runsIn : hooksData.sources;
  for (const source of hooksData.sources) runPicks.appendChild(hooksPick(source, HOOK_SOURCES[source] || source, on.includes(source)));
  form.appendChild(runs);

  const enabled = el('label', 'hooks-check');
  const box = enabled.appendChild(el('input'));
  box.type = 'checkbox';
  box.id = 'hooks-f-enabled';
  box.checked = hook ? hook.enabled : true;
  enabled.appendChild(tel('span', null, k('保存后马上生效')));
  form.appendChild(enabled);
  form.appendChild(hooksNote('warn', k('保存一个钩子，就是让智狐在你选的时刻、在这台电脑上运行这个命令。只添加你信得过的命令。'), null, 'shield'));

  const foot = form.appendChild(el('div', 'hooks-foot'));
  const message = foot.appendChild(el('p', 'hooks-help'));
  message.id = 'hooks-f-message';
  message.setAttribute('role', 'status');
  retext(message, ON_MAC ? k('⌘Enter 保存，Esc 返回') : k('Ctrl+Enter 保存，Esc 返回'));
  const cancel = foot.appendChild(tel('button', 'button', k('取消')));
  cancel.type = 'button';
  cancel.id = 'hooks-f-cancel';
  cancel.addEventListener('click', hooksBack);
  const save = foot.appendChild(tel('button', 'button primary', k('保存')));
  save.type = 'submit';
  save.id = 'hooks-f-save';

  form.addEventListener('input', hooksDirty);
  form.addEventListener('change', hooksDirty);
  form.addEventListener('keydown', (key) => {
    if (key.key === 'Enter' && (ON_MAC ? key.metaKey : key.ctrlKey) && !key.isComposing) { key.preventDefault(); saveHooksForm(); }
  });
  hooksShow([head, form], 'form', 'name');
  setHooksEvent(view.event, false);
  if (original) { name.focus(); name.select(); }
}

// 换了"什么时候"：只有工具前后的两个时刻有"对哪些工具"，只有能拦下的两个有"钩子出错时"。
// 要藏起来的那一块正拿着焦点的话，先把焦点挪回下拉框（不然焦点落在看不见的地方，Esc 会绕过表单直接关掉对话框）
function setHooksEvent(value, user) {
  const view = hooksView;
  if (view.kind !== 'form') return;
  view.event = value;
  const matcher = $('hooks-f-matcher'), failure = $('hooks-f-failure-field');
  for (const [node, shown] of [[matcher, hooksIsTool(value)], [failure, HOOK_BLOCKING.includes(value)]]) {
    if (!shown && node.contains(document.activeElement)) $('hooks-f-event').focus();
    node.hidden = !shown;
  }
  retext($('hooks-f-event-help'), HOOK_EVENTS[value].help);
  if (user) hooksDirty();
}

function hooksDirty() {
  const view = hooksView;
  if (view.kind !== 'form') return;
  view.dirty = true;
  if (view.armed) { view.armed = false; hooksFormSay(ON_MAC ? k('⌘Enter 保存，Esc 返回') : k('Ctrl+Enter 保存，Esc 返回')); }
}

function hooksFormSay(key, values, bad) {
  const message = $('hooks-f-message');
  if (!message) return;
  if (key) retext(message, key, values); else { delete message.dataset.i18n; delete message.dataset.i18nValues; message.textContent = values || ''; }
  message.classList.toggle('bad', Boolean(bad));
}

// 离开表单之前：改过还没保存的，先提醒一次，再来一次才真的放弃。返回 true 表示可以离开
function leaveHooksForm() {
  const view = hooksView;
  if (view.kind !== 'form' || !view.dirty || view.armed || view.saving) return !view.saving;
  view.armed = true;
  hooksFormSay(k('还有没保存的修改，再按一次就放弃。'), null, true);
  return false;
}

const hooksChecked = (id) => [...$(id).querySelectorAll('input[type="checkbox"]:checked')].map((box) => box.value);

async function saveHooksForm() {
  const view = hooksView;
  if (view.kind !== 'form' || view.saving) return;
  const name = $('hooks-f-name').value.trim();
  const tools = $('hooks-f-tools').value.split(/[|,]/).map((item) => item.trim()).filter(Boolean);
  const timeoutText = $('hooks-f-timeout').value.trim(), timeout = timeoutText ? Number(timeoutText) : null;
  const hook = {
    event: view.event,
    matcher: hooksIsTool(view.event) ? [...hooksChecked('hooks-f-matcher'), ...tools].join('|') : '',
    command: $('hooks-f-command').value.trim(),
    args: $('hooks-f-args').value.split('\n').map((line) => line.replace(/\r$/, '')).filter((line) => line.trim()),
    timeout,
    enabled: $('hooks-f-enabled').checked,
    onFailure: HOOK_BLOCKING.includes(view.event) ? $('hooks-f-failure').value : 'continue',
    runsIn: hooksChecked('hooks-f-runs'),
  };
  // 明显没填的，在这里就说，不用等助手程序退回来
  const badTimeout = timeout !== null && !(Number.isFinite(timeout) && timeout >= 1 && timeout <= hooksData.max_timeout);
  const missing = !name ? ['hooks-f-name', k('先给这个钩子起个名字。')]
    : !hook.command ? ['hooks-f-command', k('还没有填要运行的命令。')]
    : badTimeout ? ['hooks-f-timeout', k('最多等几秒：填 1 到 {max} 之间的数，或者不填。')]
    : !hook.runsIn.length ? ['hooks-f-runs', k('至少选一处生效的地方。')] : null;
  if (missing) {
    hooksFormSay(missing[1], { max: hooksData.max_timeout }, true);
    const target = $(missing[0]);
    (target.matches('fieldset') ? target.querySelector('input') : target).focus();
    return;
  }
  view.saving = true;
  $('hooks-f-save').disabled = true;
  hooksFormSay(k('正在保存……'));
  const reply = await call('/api/hooks/save', { original: view.original, name, hook });
  if (hooksView !== view) return;
  view.saving = false;
  $('hooks-f-save').disabled = false;
  if (!reply.ok) { hooksFormSay(null, reply.error, true); $('hooks-f-save').focus(); return; }
  const saved = reply.data.name || name;
  const fresh = await call('/api/hooks/list');
  if (hooksView !== view) return;
  if (fresh.ok) hooksData = fresh.data;
  hooksView = { kind: 'list' };  // 已经存好了：不再算"没保存的修改"
  const said = t('已保存钩子“{name}”。', { name: saved });
  announce(said);
  if (hooksFind(saved)) openHooksDetail(saved); else drawHooksList('add');
  hooksSay(said, 'good');
}

// ---- 挂到"扩展"对话框上 ----

extensionTabs.hooks = {
  open() {
    hooksDrawn = '';
    if (hooksView.kind !== 'form') loadHooks();  // 每次打开都取一次：别处改过的（命令行、编辑器），"上一次"跑得怎么样
  },
  beforeClose() {
    if (hooksView.kind === 'form' && !leaveHooksForm()) return false;
    return true;
  },
};
extensionsDialog.addEventListener('keydown', (key) => {
  if (key.key !== 'Escape' || key.isComposing || key.keyCode === 229 || extensionTab !== 'hooks' || !hooksPanel.contains(document.activeElement) && document.activeElement !== extensionsDialog) return;
  if (hooksDeleting) {  // 这一下 Esc 只用来取消删除
    key.preventDefault();
    const name = hooksDeleting;
    hooksDeleting = null;
    drawHooksList('open:' + name);
  } else if (hooksView.kind !== 'list') {  // 在下一层：先回到上一层，不直接关掉对话框
    key.preventDefault();
    key.stopPropagation();
    hooksBack();
  }
});
extensionsDialog.addEventListener('close', () => {
  hooksView = { kind: 'list' };
  hooksDeleting = null;
  hooksDrawn = '';
  delete hooksPanel.dataset.view;
  hooksPanel.replaceChildren();
});
