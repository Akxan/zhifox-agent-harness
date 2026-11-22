// ---- 扩展 · 钩子 · 打开一个、试运行 -------------------------------------------
// 点开一个钩子：它的设置（什么时刻、对哪些工具、命令、超时、出错时怎么办、在哪些运行里生效）、上一次跑得怎么样，
// 和"试运行"：用一份示例 JSON（助手程序按它的时刻和 matcher 拼好的，可以先改一改）真的运行一次它的命令，
// 看它的退出码、输出，以及它会让智狐怎么办（拦下、放行、附一句话……这句话由助手程序照着钩子的回答说，页面不自己判断）。
// 各个子页面共用的"回到上一层"（hooksBack）也在这里。

function openHooksDetail(name, testing) {
  const row = hooksFind(name);
  if (!row) return;
  hooksSay('');
  hooksView = { kind: 'detail', name, payload: row.sample ? JSON.stringify(row.sample, null, 2) : '', run: null, error: '', running: false };
  hooksDrawn = '';
  drawHooksDetail(testing && row.hook ? 'run' : 'back');
  if (testing && row.hook) {
    const card = hooksPanel.querySelector('.hooks-test');
    if (card) card.scrollIntoView({ block: 'nearest', behavior: calm() ? 'auto' : 'smooth' });
  }
}

function hooksBack() {
  const view = hooksView;
  if (view.kind === 'form' && !leaveHooksForm()) return;
  hooksSay('');
  if (view.kind === 'form' && view.original && hooksFind(view.original)) { openHooksDetail(view.original); return; }
  hooksView = { kind: 'list' };
  hooksDrawn = '';
  drawHooksList(view.name ? 'open:' + view.name : 'add');
}

function hooksFact(list, label, value, className) {
  list.appendChild(tel('dt', null, label));
  const dd = list.appendChild(el('dd', className || null));
  if (typeof value === 'string') dd.textContent = value; else dd.appendChild(value);
  return dd;
}

function drawHooksDetail(focus) {
  const view = hooksView, row = hooksFind(view.name);
  if (!row) {  // 在别处被删掉了（或者你在编辑器里把它从配置文件里删了）
    hooksView = { kind: 'list' };
    hooksSay(t('钩子“{name}”已经不在了。', { name: view.name }), 'bad');
    drawHooksList('add');
    return;
  }
  // 试运行的示例你可能正在改：重画之前先把它记下来，别弄丢
  const typed = hooksPanel.querySelector('#hooks-payload');
  if (typed) view.payload = typed.value;
  const print = JSON.stringify([row, view.run, view.error, view.running, [...hooksPending], language]);
  if (!focus && print === hooksDrawn && hooksPanel.dataset.view === 'detail:' + view.name) return;
  hooksDrawn = print;
  const hook = row.hook, pending = hooksPending.has(row.name);

  const head = el('div', 'hooks-head');
  const back = head.appendChild(el('button', 'hooks-back'));
  back.type = 'button';
  back.dataset.focus = 'back';
  back.append(icon('chevron'), tel('span', null, k('全部钩子')));
  back.addEventListener('click', hooksBack);
  const title = head.appendChild(el('h3', 'hooks-title'));
  title.appendChild(el('i', 'hooks-dot ' + (!hook ? 'bad' : hook.enabled ? 'on' : 'off')));
  title.appendChild(el('span', 'hooks-title-name', row.name));
  const nodes = [head];
  if (!hook) {
    nodes.push(hooksSaidNote('bad', row.problem));
    nodes.push(hooksNote('calm', k('在编辑器里改好 hooks.json 以后，这里会自己更新。'), null, 'file'));
    hooksShow(nodes, 'detail:' + view.name, focus);
    return;
  }
  const edit = head.appendChild(hooksButton('edit', k('编辑')));
  edit.dataset.focus = 'edit';
  edit.disabled = pending;
  edit.addEventListener('click', () => openHooksForm(row.name));
  const toggle = head.appendChild(hooksSwitch(hook.enabled, row.name));
  toggle.dataset.focus = 'enable:' + row.name;
  toggle.disabled = pending;
  toggle.addEventListener('click', () => enableHook(row.name, !hook.enabled));

  // 它的设置
  const card = el('section', 'hooks-card');
  card.appendChild(tel('h4', null, k('设置')));
  const facts = card.appendChild(el('dl', 'hooks-facts'));
  const when = el('span');
  when.append(tel('span', null, HOOK_EVENTS[hook.event].label), el('code', 'hooks-raw', hook.event));
  hooksFact(facts, k('什么时候'), when);
  if (hooksIsTool(hook.event)) hooksFact(facts, k('对哪些工具'), hooksMatcherText(hook.matcher));
  hooksFact(facts, k('命令'), hooksCommand(hook), 'hooks-command mono');
  hooksFact(facts, k('最多等'), hook.timeout ? traceTime(hook.timeout * 1000) : t('{time}（默认）', { time: traceTime(hooksData.timeout * 1000) }));
  if (HOOK_BLOCKING.includes(hook.event)) hooksFact(facts, k('钩子出错时'), hook.onFailure === 'block' ? t('当成拦下') : t('照常继续'));
  hooksFact(facts, k('在哪儿生效'), hook.runsIn.map((source) => t(HOOK_SOURCES[source] || source)).join(t('、')));
  hooksFact(facts, k('上一次'), hooksLastText(row.last), 'hooks-last ' + hooksLastKind(row.last));
  if (row.last && row.last.note) hooksFact(facts, k('它说'), row.last.note, 'hooks-said hooks-user');  // 钩子自己写的话，原样
  card.appendChild(tel('p', 'hooks-small', HOOK_EVENTS[hook.event].help));
  nodes.push(card, hooksTestCard(row, view));
  hooksShow(nodes, 'detail:' + view.name, focus);
}

// 试运行：示例 JSON（可以改）、按钮、结果
function hooksTestCard(row, view) {
  const card = el('section', 'hooks-card hooks-test');
  const heading = card.appendChild(el('h4'));
  heading.append(tel('span', null, k('试运行')), tel('span', 'sub', k('交给它的 JSON（可以先改一改）')));
  card.appendChild(hooksNote('warn', k('试运行会真的在这台电脑上运行这个命令，和到了那个时刻一样（只是 JSON 里多一项 "test": true）。停用着的钩子也能试。'), null, 'alert'));
  const area = card.appendChild(el('textarea', 'hooks-area hooks-payload'));
  area.id = 'hooks-payload';
  area.dataset.focus = 'payload';
  area.spellcheck = false;
  area.rows = 10;
  area.value = view.payload;
  tattr(area, 'aria-label', k('交给钩子的 JSON'));
  area.addEventListener('input', () => { view.payload = area.value; });
  const bar = card.appendChild(el('div', 'hooks-test-bar'));
  const run = bar.appendChild(hooksButton('play', view.running ? k('正在运行……') : k('试运行'), 'primary'));
  run.dataset.focus = 'run';
  run.disabled = view.running;
  run.addEventListener('click', () => testHook(row.name));
  const reset = bar.appendChild(hooksButton('refresh', k('换回示例')));
  reset.dataset.focus = 'reset';
  reset.disabled = view.running;
  reset.addEventListener('click', () => {
    view.payload = row.sample ? JSON.stringify(row.sample, null, 2) : '';
    area.value = view.payload;
    area.focus();
  });
  const status = bar.appendChild(el('p', 'hooks-test-status'));
  status.setAttribute('role', 'status');
  if (view.error) { status.textContent = view.error; status.classList.add('bad'); }
  if (view.run) card.appendChild(hooksResult(view.run));
  return card;
}

// 试运行的结果：它会让智狐怎么办（最显眼的那一句）、退出码和用了多久、它的输出、它说了但这里不照办的
function hooksResult(run) {
  const box = el('div', 'hooks-result is-' + (run.result || 'error'));
  box.setAttribute('aria-live', 'polite');
  const verdict = box.appendChild(el('p', 'hooks-verdict'));
  verdict.append(el('span', 'hooks-chip ' + hooksLastKind({ result: run.result }), t(HOOK_RESULTS[run.result] || HOOK_RESULTS.error)), hooksRetext(el('span', 'hooks-verdict-text'), run.verdict));
  const facts = box.appendChild(el('p', 'hooks-run-facts'));
  facts.textContent = (run.exit === null || run.exit === undefined ? t('没有自己退出') : t('退出码 {code}', { code: run.exit })) + ' · ' + traceTime(run.ms);
  if (run.message) box.appendChild(tel('p', 'hooks-said', k('它给你的话：{message}'), { message: run.message }));
  for (const [label, text] of [[k('标准输出'), run.stdout], [k('标准错误'), run.stderr]]) {
    const part = box.appendChild(el('div', 'hooks-out-part'));
    part.appendChild(tel('h5', null, label));
    if (text) part.appendChild(el('pre', 'hooks-out', text));
    else part.appendChild(tel('p', 'hooks-small', k('（没有输出）')));
  }
  for (const said of run.ignored || []) box.appendChild(hooksSaidNote('calm', said));
  return box;
}

async function testHook(name) {
  const view = hooksView;
  if (view.kind !== 'detail' || view.running) return;
  const area = $('hooks-payload');
  if (area) view.payload = area.value;
  let payload;
  try {
    payload = view.payload.trim() ? JSON.parse(view.payload) : {};
  } catch (error) {
    view.error = t('这份 JSON 解析不了：{error}', { error: error.message });
    drawHooksDetail('run');
    return;
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    view.error = t('交给钩子的应该是一个 JSON 对象（{ … }）。');
    drawHooksDetail('run');
    return;
  }
  view.running = true;
  view.error = '';
  drawHooksDetail('run');
  const reply = await call('/api/hooks/test', { name, payload });
  if (hooksView !== view) return;  // 这期间离开了
  view.running = false;
  if (!reply.ok) { view.error = reply.error; drawHooksDetail('run'); return; }
  view.run = reply.data.run;
  const row = hooksFind(name);
  if (row) row.last = reply.data.last;
  drawHooksDetail('run');
  announce(hooksSaid(view.run.verdict));
}
