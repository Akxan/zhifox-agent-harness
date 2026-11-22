// ---- 扩展 · 钩子 · 列表 -----------------------------------------------------
// 钩子一页的第一层：上面一段说明和"它会在你的电脑上运行命令"的提醒、"添加钩子"，一个都没有时讲清楚四个时刻和一个小例子，
// 以及每一行：启用开关、试运行，和别处一样的"在访达中显示 / 编辑 / 删除"（删除先在原地确认）。点一行打开它（hooks-detail.js）。

function drawHooksList(focus) {
  // 指纹里带着"现在是第几分钟"：每一行写着"3 分钟前"，这句话过一会儿要重写
  const print = JSON.stringify([hooksData, hooksDeleting, [...hooksPending], language, Math.floor(Date.now() / 60000)]);
  if (!focus && print === hooksDrawn && hooksPanel.dataset.view === 'list') return;
  hooksDrawn = print;
  const rows = hooksData.hooks;
  const nodes = [hooksToolbar()];
  if (hooksData.problem) nodes.push(hooksSaidNote('bad', hooksData.problem));
  if (!rows.length) {
    nodes.push(hooksEmpty());
  } else {
    const list = el('ul', 'hooks-list');
    list.setAttribute('aria-label', t('钩子'));
    list.append(...rows.map(hooksRow));
    nodes.push(list);
  }
  hooksShow(nodes, 'list', focus);
}

function hooksToolbar() {
  const bar = el('div', 'hooks-bar');
  bar.appendChild(tel('p', 'hooks-lead', k('钩子在固定的时刻运行你写的命令：工具执行之前和之后、你发出一条消息时、一轮答完时。拦下危险的命令、改完文件自动格式化、记日志、发通知……写法和 Claude Code 的钩子一样。')));
  const tools = bar.appendChild(el('div', 'hooks-tools-bar'));
  const add = tools.appendChild(hooksButton('plus', k('添加钩子'), 'primary'));
  add.dataset.focus = 'add';
  add.addEventListener('click', () => openHooksForm(null));
  const reveal = tools.appendChild(iconButton('folder', byServer(k('在访达中显示配置文件'), k('在文件资源管理器中显示配置文件'), k('在文件管理器中显示配置文件'))));
  reveal.classList.remove('small');
  reveal.dataset.focus = 'reveal';
  reveal.addEventListener('click', revealHooksFile);
  const box = el('div', 'hooks-intro');
  box.append(bar, hooksNote('warn', k('钩子会以你的账号的权限在这台电脑上运行命令：能读、改、删你能碰到的任何文件。只添加你信得过、看得懂的命令。各家模型的密钥不会交给钩子；模型也没有任何能添加或修改钩子的工具。'), null, 'shield'));
  // 命令的沙箱（设置里）开着的时候，钩子和 run_command 关在同一个沙箱里（见 harness/hooks/runner.py 的 jailed）。
  // 开没开由助手程序说（列表里的 sandbox：你的对话此刻用的那一档）
  if (hooksData && hooksData.sandbox && hooksData.sandbox !== 'off') {
    box.appendChild(hooksNote('calm', k('命令的沙箱开着：钩子也和命令关在同一个沙箱里，只碰得到工作目录和你额外允许的路径（钩子自己的脚本可以读）。要写别处的日志、要联网发通知的，在设置的沙箱一节里加上路径，或者选“工作目录 + 联网”。'), null, 'lock'));
  }
  return box;
}

function hooksEmpty() {
  const box = el('div', 'hooks-empty');
  const mark = box.appendChild(el('div', 'hooks-empty-mark'));
  mark.appendChild(icon('hook'));
  box.appendChild(tel('h3', null, k('还没有钩子')));
  box.appendChild(tel('p', null, k('一个钩子就是"到了某个时刻，运行这个命令"。智狐用标准输入交给它一份 JSON（是哪个工具、什么参数、你说了什么……），看它的退出码和输出决定怎么办：退出码 2 表示拦下，标准错误就是原因。')));
  const events = box.appendChild(el('dl', 'hooks-events'));
  for (const [name, info] of Object.entries(HOOK_EVENTS)) {
    const term = events.appendChild(el('dt'));
    term.append(tel('b', null, info.label), el('code', null, name));
    events.appendChild(tel('dd', null, info.help));
  }
  box.appendChild(tel('p', null, k('比如这个 Python 脚本挂在"工具执行之前"、对"执行命令"生效，就能拦下所有带 rm -rf 的命令：')));
  box.appendChild(el('pre', 'hooks-example', HOOK_EXAMPLE));
  const row = box.appendChild(el('div', 'row'));
  const add = row.appendChild(hooksButton('plus', k('添加钩子'), 'primary'));
  add.addEventListener('click', () => openHooksForm(null));
  box.appendChild(tel('p', 'hooks-small', k('也可以在命令行里用 harness hooks add 添加，或者直接编辑数据文件夹里的 hooks.json。')));
  return box;
}

function hooksRow(row) {
  const item = el('li', 'hooks-row');
  item.dataset.name = row.name;
  const hook = row.hook, pending = hooksPending.has(row.name);

  // 删除前在原地确认一下，和删除会话、记忆一样，不弹对话框
  if (hooksDeleting === row.name) {
    const box = item.appendChild(el('div', 'hooks-confirm'));
    box.appendChild(el('span', null, t('删除钩子“{name}”？它会从配置文件里去掉，以后不再运行。', { name: row.name })));
    const yes = box.appendChild(el('button', 'mini danger', t('删除')));
    const no = box.appendChild(el('button', 'mini cancel', t('取消')));
    yes.type = no.type = 'button';
    no.dataset.focus = 'cancel:' + row.name;
    yes.disabled = no.disabled = pending;
    yes.addEventListener('click', () => deleteHook(row.name, item));
    no.addEventListener('click', () => { hooksDeleting = null; drawHooksList('open:' + row.name); });
    return item;
  }

  const open = item.appendChild(el('button', 'hooks-open'));
  open.type = 'button';
  open.dataset.focus = 'open:' + row.name;
  open.appendChild(el('span', 'hooks-dot ' + (!hook ? 'bad' : hook.enabled ? 'on' : 'off')));
  const words = open.appendChild(el('span', 'hooks-words'));
  const line = words.appendChild(el('span', 'hooks-line'));
  line.appendChild(el('span', 'hooks-name', row.name));
  if (hook) {
    const chip = line.appendChild(el('span', 'hooks-event', t(HOOK_EVENTS[hook.event].label)));
    chip.title = hook.event;
    const meta = words.appendChild(el('span', 'hooks-meta'));
    if (hooksIsTool(hook.event)) meta.appendChild(el('span', 'hooks-matcher', hooksMatcherText(hook.matcher) + ' · '));
    const command = meta.appendChild(el('span', 'hooks-command', hooksCommand(hook)));
    command.title = hooksCommand(hook);  // 命令常常很长，行里放不下的部分省略了，悬停时看全的
    const last = words.appendChild(el('span', 'hooks-last ' + hooksLastKind(row.last), hooksLastText(row.last)));
    if (row.last && row.last.note) last.title = row.last.note;
  } else {
    words.appendChild(hooksRetext(el('span', 'hooks-error'), row.problem));
  }
  open.addEventListener('click', () => openHooksDetail(row.name));

  const toggle = item.appendChild(hooksSwitch(Boolean(hook && hook.enabled), row.name));
  toggle.dataset.focus = 'enable:' + row.name;
  toggle.disabled = pending || !hook;
  toggle.addEventListener('click', () => enableHook(row.name, !(hook && hook.enabled)));

  // 最右边一格：平时写着上一次用了多久；悬停时换成按钮。最后三个和会话行、记忆行一样（在访达中显示、编辑、删除），
  // 位置、大小、顺序都一样；"试运行"排在它们前面。名字里带上是哪一个钩子：读屏软件逐个读按钮时要知道它操作的是谁
  const slot = item.appendChild(el('div', 'hooks-slot'));
  if (row.last) slot.appendChild(el('span', 'hooks-took', traceTime(row.last.ms)));
  const actions = slot.appendChild(el('div', 'hooks-actions'));
  const button = (iconName, title, label, extra) => {
    const node = actions.appendChild(el('button', 'icon-button small' + (extra ? ' ' + extra : '')));
    node.type = 'button';
    node.title = t(title);
    node.setAttribute('aria-label', t(label, { name: row.name }));
    node.appendChild(icon(iconName));
    node.disabled = pending;
    return node;
  };
  const test = button('play', k('试运行'), k('试运行钩子：{name}'));
  test.dataset.focus = 'test:' + row.name;
  test.disabled = pending || !hook;
  test.addEventListener('click', () => openHooksDetail(row.name, true));
  const reveal = button('folder', showLabel(), byServer(k('在访达中显示“{name}”的配置文件'), k('在文件资源管理器中显示“{name}”的配置文件'), k('在文件管理器中显示“{name}”的配置文件')));
  reveal.dataset.focus = 'reveal:' + row.name;
  reveal.disabled = false;
  reveal.addEventListener('click', revealHooksFile);
  const edit = button('edit', k('编辑'), k('编辑钩子：{name}'));
  edit.dataset.focus = 'edit:' + row.name;
  edit.disabled = pending || !hook;
  edit.addEventListener('click', () => openHooksForm(row.name));
  const remove = button('trash', k('删除'), k('删除钩子：{name}'), 'danger-hover');
  remove.dataset.focus = 'delete:' + row.name;
  remove.addEventListener('click', () => { hooksDeleting = row.name; drawHooksList('cancel:' + row.name); });
  return item;
}

async function enableHook(name, enabled) {
  const reply = await hooksAct(name, '/api/hooks/enable', { name, enabled });
  if (!reply.ok) { hooksSay(reply.error, 'bad'); return; }
  const row = hooksFind(name);
  if (row && row.hook) row.hook = reply.data.hook;  // 先照着回答改好，免得开关闪回去；列表紧接着重新取
  drawHooks('enable:' + name);
  const said = enabled ? t('已打开钩子“{name}”。', { name }) : t('已关掉钩子“{name}”。', { name });
  hooksSay(said, 'good');
  announce(said);
}

async function deleteHook(name, item) {
  const rows = hooksData.hooks, index = rows.findIndex((row) => row.name === name);
  const next = rows[index + 1] || rows[index - 1];
  const [reply] = await Promise.all([hooksAct(name, '/api/hooks/delete', { name }), leave(item)]);
  hooksDeleting = null;
  if (!reply.ok) { hooksSay(reply.error, 'bad'); drawHooksList('open:' + name); return; }
  if (hooksView.kind !== 'list') hooksView = { kind: 'list' };
  hooksData = { ...hooksData, hooks: hooksData.hooks.filter((row) => row.name !== name) };  // 先在页面上拿掉，列表紧接着重新取
  drawHooksList(next ? 'open:' + next.name : 'add');
  const said = t('已删除钩子“{name}”。', { name });
  hooksSay(said, 'good');
  announce(said);
}

async function revealHooksFile() {
  const reply = await call('/api/hooks/reveal');
  hooksSay(reply.ok ? revealedText(reply.data.path) : reply.error, reply.ok ? 'good' : 'bad');
}
