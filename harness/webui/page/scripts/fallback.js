// ---- 设置 · 备用模型 ------------------------------------------------------
// 设置面板里"备用模型"那一节：现在这个模型暂时用不了（过载、服务器出错、请求太频繁）时，这一轮接下来按顺序改用的
// 同一家的模型。什么时候换、为什么只能选同一家的、下一轮换回来，这些规矩只在 harness/fallback.py 里写一份；
// 这里只画现在这一家排好的名单，让你加一个、往前挪、去掉。每改一下就把整份名单交给 /api/fallbacks，
// 服务器检查完发来新的状态，再照着重画（state.fallbacks：{max, chosen: {提供方: [模型, …]}}）。
// 只照着状态画，不照着 /api/fallbacks 的回答画：回答和状态走的是两条路，谁先到不一定；照着回答画的话，
// 晚到的上一次的状态会把名单画回去。状态之间是按顺序到的，最后一个总是最新的。

let fallbackDrawn = '';
let fallbackShown = '[]';  // 现在画着的名单（JSON）
// 重画以后焦点放回哪儿：{model, action, expect}（刚点的那个按钮被换掉了，不放回去 Esc 会直接关掉面板）。
// expect 是存进去的那份名单：画的正是它的时候才把焦点放回去，中途画的别的（换了语言）不算
let fallbackFocus = null;

function fallbackState() {
  const raw = state && state.fallbacks && typeof state.fallbacks === 'object' ? state.fallbacks : {};
  const chosen = raw.chosen && Array.isArray(raw.chosen[state.provider]) ? raw.chosen[state.provider].filter((m) => typeof m === 'string') : [];
  return { max: Number.isFinite(raw.max) ? raw.max : 3, chosen };
}

function renderFallbacks(force) {
  if (!state) return;
  const provider = state.providers.find((item) => item.id === state.provider);
  const { max, chosen } = fallbackState();
  const offered = provider ? provider.models : [];
  const print = JSON.stringify([state.provider, state.model, chosen, offered, max, language]);
  if (!force && print === fallbackDrawn) return;
  fallbackDrawn = print;

  fallbackShown = JSON.stringify(chosen);
  retext($('fallback-state'), chosen.length ? k('（按顺序改用 {n} 个）') : k('（没有）'), { n: chosen.length });
  const list = $('fallback-list');
  const rows = chosen.map((model, index) => fallbackRow(model, index, chosen));
  list.replaceChildren(...rows);
  list.hidden = !rows.length;
  $('fallback-empty').hidden = rows.length > 0;

  const add = $('fallback-add');
  const left = offered.filter((model) => model !== state.model && !chosen.includes(model));
  const prompt = el('option', null, !offered.length ? t('先在上面同步这一家的模型列表')
    : chosen.length >= max ? t('最多 {n} 个备用模型', { n: max })
    : !left.length ? t('这一家没有别的模型可以选了') : t('添加备用模型…'));
  prompt.value = '';
  add.replaceChildren(prompt, ...left.map((model) => { const option = el('option', null, model); option.value = model; return option; }));
  add.value = '';
  add.disabled = !left.length || chosen.length >= max;

  if (fallbackFocus && fallbackFocus.expect === fallbackShown) {
    const { model, action } = fallbackFocus;
    fallbackFocus = null;
    const target = action === 'add' || !chosen.includes(model) ? add
      : list.querySelector('[data-model="' + CSS.escape(model) + '"] [data-action="' + action + '"]:not([disabled])') || add;
    target.focus({ preventScroll: true });
    if (action === 'add') {
      const row = list.querySelector('[data-model="' + CSS.escape(model) + '"]');
      if (row) play(row, [{ opacity: 0, transform: 'translateY(-4px)' }, { opacity: 1, transform: 'none' }], 220);
    }
  }
}

function fallbackRow(model, index, chosen) {
  const row = el('li', 'fallback-row');
  row.dataset.model = model;
  row.appendChild(el('span', 'fallback-n', String(index + 1)));
  const name = row.appendChild(el('span', 'fallback-name', model));
  name.title = model;
  if (model === state.model) row.appendChild(tel('span', 'tag', k('现在用的就是它')));
  const up = row.appendChild(iconButton('up', k('往前挪')));
  up.dataset.action = 'up';
  up.disabled = index === 0;
  up.addEventListener('click', () => {
    const next = chosen.slice();
    [next[index - 1], next[index]] = [next[index], next[index - 1]];
    saveFallbacks(next, { model, action: index - 1 === 0 ? 'remove' : 'up' });
  });
  const remove = row.appendChild(iconButton('x', k('不用这个备用模型')));
  remove.dataset.action = 'remove';
  remove.addEventListener('click', () => saveFallbacks(chosen.filter((item) => item !== model), { model: chosen[index + 1] || chosen[index - 1] || '', action: 'remove' }));
  return row;
}

async function saveFallbacks(models, focus) {
  fallbackFocus = { ...focus, expect: JSON.stringify(models) };
  const reply = await call('/api/fallbacks', { provider: state.provider, models });
  if (!reply.ok) {
    fallbackFocus = null;
    say(reply.error, 'bad');
    renderFallbacks(true);  // 下拉框回到"添加备用模型…"
    return;
  }
  say(models.length ? t('备用模型已保存。') : t('已经不用备用模型了。'), 'good');
  // 新的状态已经先到、画好了（或者名单根本没变，不会重画）：焦点不用再等着放回去
  if (fallbackShown === fallbackFocus?.expect) fallbackFocus = null;
}

$('fallback-add').addEventListener('change', (change) => {
  const model = change.target.value;
  if (!model) return;
  saveFallbacks([...fallbackState().chosen, model], { model, action: 'add' });
});
